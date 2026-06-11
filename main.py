"""
main.py - Investment Alpha Pipeline Entry Point

Usage:
    python main.py                          # Full pipeline (analysis only)
    python main.py --tickers AAPL MSFT      # Specific tickers
    python main.py --top 10                 # Select top N stocks
    python main.py --refresh                # Force re-download (ignore cache)
    python main.py --debug                  # Verbose stage-by-stage output
    python main.py --dry-run                # Skip saving output files

    python main.py --execute                # Run pipeline AND place paper trades
    python main.py --execute --broker-dry-run  # Log trades but do not submit

    python main.py --skip-regime            # Skip regime detection, use BULL defaults
    python main.py --skip-stop-loss         # Skip weekly stop-loss check
    python main.py --force-regime bear      # Override regime (bull/neutral/bear)

    uvicorn api.server:app --reload         # Start FastAPI server
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import config

from pipeline import (features, filters, ingestion, output, portfolio,
                      scoring, selection, signals)
from pipeline import regime as regime_module
from pipeline import sentiment as sentiment_module
from pipeline import insider as insider_module
from pipeline import congressional as congressional_module
from pipeline import feedback as feedback_module
from pipeline import performance_tracker as perf_tracker
from broker import executor as broker_executor
from broker import alpaca_client as alpaca
from broker import stop_loss as stop_loss_module


def setup_logging(debug=False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )
    if not debug:
        for noisy in ("yfinance", "urllib3", "requests", "peewee"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def run_pipeline(
    tickers=None,
    top_n=None,
    force_refresh=False,
    dry_run=False,
    execute=False,
    broker_dry_run=True,
    skip_regime=False,
    skip_stop_loss=False,
    force_regime=None,
):
    """
    Execute all pipeline stages sequentially.

    Pre-flight (before data ingestion):
      1. Regime detection  -- classifies BULL/NEUTRAL/BEAR, sets active_top_n
      2. Stop-loss check   -- exits any positions that breached their threshold

    Then the 8 core stages run with regime-aware top_n.
    """
    log = logging.getLogger("main")
    t_start = time.time()

    # ================================================================
    # PRE-FLIGHT: Regime Detection
    # ================================================================
    regime_result = None

    if force_regime:
        # Manual override from CLI (--force-regime bear)
        regime_label = force_regime.lower()
        regime_result = {
            "regime":           regime_label,
            "vix_current":      None,
            "spx_price":        None,
            "spx_200ma":        None,
            "spx_vs_200ma_pct": None,
            "active_top_n":     config.REGIME_TOP_N.get(regime_label, config.TOP_N_STOCKS),
            "active_stop_loss": config.STOP_LOSS_PCT.get(regime_label, config.STOP_LOSS_PCT["bull"]),
            "notes":            f"Forced via --force-regime {regime_label}",
        }
        log.info("Regime FORCED: %s (top_n=%d)", regime_label.upper(),
                 regime_result["active_top_n"])

    elif not skip_regime and config.REGIME_ENABLED:
        log.info("PRE-FLIGHT: Running regime detection...")
        regime_result = regime_module.run()
        log.info("Regime: %s | VIX=%.1f | SPX vs 200MA=%.1f%% | top_n=%d",
                 regime_result["regime"].upper(),
                 regime_result["vix_current"] or 0,
                 regime_result["spx_vs_200ma_pct"] or 0,
                 regime_result["active_top_n"])
    else:
        log.info("Regime detection skipped -- using BULL defaults")
        regime_result = {
            "regime": "bull",
            "active_top_n": config.TOP_N_STOCKS,
            "active_stop_loss": config.STOP_LOSS_PCT["bull"],
            "notes": "Skipped",
        }

    # Resolve top_n: explicit CLI arg overrides regime
    effective_top_n = top_n if top_n is not None else regime_result["active_top_n"]

    # ================================================================
    # PRE-FLIGHT: Stop-Loss Check
    # ================================================================
    stop_loss_result = None

    if not skip_stop_loss and config.STOP_LOSS_ENABLED:
        log.info("PRE-FLIGHT: Running stop-loss check (regime=%s)...",
                 regime_result["regime"].upper())
        stop_loss_result = stop_loss_module.check_and_execute(
            regime=regime_result["regime"],
            dry_run=not execute,    # only execute real exits if --execute flag is set
        )
        if stop_loss_result["triggered"]:
            log.warning("Stop-loss triggered for: %s", stop_loss_result["triggered"])
        else:
            log.info("Stop-loss check: no positions triggered (%d checked)",
                     len(stop_loss_result["checked"]))
    else:
        log.info("Stop-loss check skipped")

    # ================================================================
    # MAIN PIPELINE
    # ================================================================
    log.info("=" * 60)
    log.info("  INVESTMENT ALPHA PIPELINE - START")
    log.info("  Universe : %d tickers",
             len(tickers) if tickers else len(config.ALL_TICKERS))
    log.info("  Regime   : %s | Top-N: %d", regime_result["regime"].upper(), effective_top_n)
    log.info("  Execute  : %s  (broker_dry_run=%s)", execute, broker_dry_run)
    log.info("=" * 60)

    # Stage 1: Ingestion
    ing_result = ingestion.run(tickers=tickers, force_refresh=force_refresh)
    if ing_result["status"] == "failed":
        log.error("Pipeline aborted: Stage 1 (Ingestion) failed")
        return {"error": "ingestion_failed"}

    # Stage 2: Features
    feat_result = features.run(ing_result)
    if feat_result["status"] == "failed":
        log.error("Pipeline aborted: Stage 2 (Features) failed")
        return {"error": "features_failed"}

    # Stage 2B: Sentiment (Phase 3 — analyst revisions embedded in features)
    # analyst_score is computed in features.py from yfinance targetMeanPrice + rec
    # sentiment_module.run() is a status check only — no external API call needed
    sent_result = sentiment_module.run(tickers)
    log.info("Sentiment: %s (source: %s)",
             sent_result["status"], sent_result.get("signal_source", "n/a"))

    # Stage 2C: Insider signals (Phase 3 — open-market purchases only, $500k+)
    # Controlled by INSIDER_ENABLED in config; skip if disabled to save time
    import config as _cfg
    if not getattr(_cfg, "INSIDER_ENABLED", True):
        insider_result = {"status": "disabled", "insider_signals": {}, "tickers_fetched": 0}
    else:
        insider_result = insider_module.run(tickers)
    if insider_result["status"] in ("success", "partial") and not feat_result["features"].empty:
        feat_df = feat_result["features"]
        insider_signals = insider_result.get("insider_signals", {})
        feat_df["insider_signal"] = feat_df["ticker"].map(insider_signals).fillna(0)
        feat_result["features"] = feat_df
        log.info("Insider signals injected: %d tickers (open-market purchases only)",
                 insider_result["tickers_fetched"])

    # Stage 2D: Congressional signals (STOCK Act disclosures)
    if not getattr(config, "CONGRESSIONAL_ENABLED", False):
        congressional_result = {
            "status": "disabled",
            "congressional_signals": {},
            "tickers_fetched": 0,
        }
    else:
        congressional_result = congressional_module.run(tickers)
    if (congressional_result["status"] in ("success", "partial")
            and not feat_result["features"].empty):
        feat_df = feat_result["features"]
        cong_signals = congressional_result.get("congressional_signals", {})
        feat_df["congressional_signal"] = feat_df["ticker"].map(cong_signals).fillna(0)
        feat_result["features"] = feat_df
        log.info("Congressional signals injected: %d tickers (STOCK Act disclosures)",
                 congressional_result["tickers_fetched"])

    # Stage 3: Scoring
    score_result = scoring.run(feat_result)
    if score_result["status"] == "failed":
        log.error("Pipeline aborted: Stage 3 (Scoring) failed")
        return {"error": "scoring_failed"}

    # Stage 4: Filtering
    filter_result = filters.run(score_result)
    if filter_result["ticker_count_out"] == 0:
        log.error("Pipeline aborted: Stage 4 removed all stocks")
        return {"error": "all_filtered"}

    # Stage 5: Selection (regime-aware top_n)
    sel_result = selection.run(
        filter_result,
        top_n=effective_top_n,
        regime_result=regime_result,
    )
    if sel_result["status"] == "failed":
        log.error("Pipeline aborted: Stage 5 (Selection) failed")
        return {"error": "selection_failed"}

    # Stage 5B: Shadow portfolio snapshot — top-30 with factor scores.
    # Feeds the weekly learning loop with ~3x more observations than
    # bought-only feedback, including the stocks the model skipped.
    try:
        from pipeline import shadow as shadow_module
        shadow_module.record(filter_result, regime_result=regime_result, top_k=30)
    except Exception as exc:
        log.warning("Shadow snapshot failed (non-fatal): %s", exc)

    # Stage 6: Portfolio (score-weighted or equal, entry_price recorded)
    port_result = portfolio.run(sel_result, regime_result=regime_result)
    if port_result["status"] == "failed":
        log.error("Pipeline aborted: Stage 6 (Portfolio) failed")
        return {"error": "portfolio_failed"}

    # Stage 7: Signals (writes entry_price + entry_date to state file)
    sig_result = signals.run(port_result, sel_result, regime_result=regime_result)

    # Stage 8: Output
    all_results = {
        "ingestion":     ing_result,
        "features":      feat_result,
        "sentiment":     sent_result,
        "insider":       insider_result,
        "congressional": congressional_result,
        "scoring":       score_result,
        "filters":       filter_result,
        "selection":     sel_result,
        "portfolio":     port_result,
        "signals":       sig_result,
        "regime":        regime_result,
        "stop_loss":     stop_loss_result,
    }

    if not dry_run:
        out_result = output.run(all_results)
        all_results["output"] = out_result
    else:
        log.info("Dry-run mode: skipping file output")
        out_result = {"status": "skipped"}

    # ================================================================
    # BROKER EXECUTION (optional - requires --execute flag)
    # ================================================================
    broker_result = None
    if execute:
        log.info("=" * 60)
        log.info("  BROKER: Connecting to Alpaca paper trading...")

        selected_df = sel_result.get("selected", pd.DataFrame())
        price_map = (
            dict(zip(selected_df["ticker"], selected_df["current_price"]))
            if not selected_df.empty else {}
        )
        enriched_signals = []
        for s in sig_result.get("all_signals", []):
            s = s.copy()
            if not s.get("current_price"):
                s["current_price"] = price_map.get(s["ticker"])
            enriched_signals.append(s)

        broker_result = broker_executor.execute_signals(
            signals=enriched_signals,
            dry_run=broker_dry_run,
        )
        all_results["broker"] = broker_result
        log.info("  Broker status  : %s", broker_result.get("status"))
        log.info("  Orders placed  : %d",
                 broker_result.get("summary", {}).get("orders_placed", 0))

    # ================================================================
    # FEEDBACK LOOP: Update factor weights based on last month's performance
    # Runs after every successful pipeline completion (not dry-run)
    # ================================================================
    feedback_record = None
    if not dry_run:
        try:
            log.info("Running monthly feedback loop (adaptive factor weights)...")
            feedback_record = feedback_module.run(dry_run=False)
            if feedback_record:
                log.info("  Feedback: avg_return=%.1f%%  hit_rate=%.0f%%",
                         feedback_record.get("avg_return", 0) * 100,
                         feedback_record.get("hit_rate", 0) * 100)
        except Exception as e:
            log.warning("Feedback loop failed (non-fatal): %s", e)

    # ================================================================
    # PERFORMANCE TRACKER: Paper-trading snapshot (runs during validation window)
    # ================================================================
    if not dry_run and getattr(config, "PAPER_TRADING_VALIDATION", False):
        try:
            log.info("Paper trading: recording performance snapshot...")
            snap = perf_tracker.run()
            if snap and snap.get("status") != "no_positions":
                log.info(
                    "  Portfolio value: €%.2f | Return: %+.2f%% | Alpha vs benchmark: %+.2f%%",
                    snap.get("total_portfolio_value", 0),
                    snap.get("total_return_pct", 0),
                    snap.get("alpha_pct", 0) or 0,
                )
                pt = snap.get("paper_trading", {})
                if not pt.get("in_validation", True):
                    log.warning(
                        "PAPER TRADING VALIDATION COMPLETE (%d days). "
                        "Review performance_log.json and promote strategy if criteria met.",
                        pt.get("days_elapsed", 0),
                    )
        except Exception as e:
            log.warning("Performance tracker failed (non-fatal): %s", e)

    elapsed = time.time() - t_start
    log.info("=" * 60)
    log.info("  PIPELINE COMPLETE in %.1fs", elapsed)
    log.info("  Regime   : %s", regime_result["regime"].upper())
    if not dry_run:
        log.info("  JSON      : %s", out_result.get("json_path", ""))
        log.info("  Excel     : %s", out_result.get("excel_path", ""))
        log.info("  Dashboard : %s", out_result.get("dashboard_path", ""))
    log.info("  Stocks  : %s", [p["ticker"] for p in port_result.get("portfolio", [])])
    log.info("  Signals : %s", sig_result.get("signal_summary", {}))
    log.info("=" * 60)

    final_json = None
    if not dry_run and out_result.get("json_path"):
        try:
            with open(out_result["json_path"]) as f:
                final_json = json.load(f)
        except Exception:
            pass

    return {
        "pipeline_status": out_result.get("status", "skipped"),
        "elapsed_seconds": round(elapsed, 1),
        "regime":          regime_result,
        "stop_loss":       stop_loss_result,
        "output_files": {
            "json":      str(out_result.get("json_path", "")),
            "excel":     str(out_result.get("excel_path", "")),
            "dashboard": str(out_result.get("dashboard_path", "")),
        },
        "broker_result":  broker_result,
        "final_output":   final_json,
        "_stage_results": all_results,
    }


def print_summary(result):
    regime = result.get("regime", {})
    regime_label = regime.get("regime", "unknown").upper()
    regime_note  = regime.get("notes", "")

    final = result.get("final_output")
    if not final:
        print("\n  No final output (dry-run or error)")
        return

    print("\n" + "=" * 60)
    print("  INVESTMENT ALPHA - RUN SUMMARY")
    print("=" * 60)
    print("  Timestamp     :", final.get("timestamp"))
    print("  Market Regime :", regime_label, "--", regime_note[:60])
    vix = regime.get("vix_current")
    spx_pct = regime.get("spx_vs_200ma_pct")
    if vix:
        print(f"  VIX           : {vix:.1f}  |  SPX vs 200MA: {spx_pct:+.1f}%")
    print("  Universe Size :", final.get("universe_size", 0), "tickers screened")
    print("  Elapsed       :", result["elapsed_seconds"], "s")

    sl = result.get("stop_loss")
    if sl and sl.get("triggered"):
        print("  STOP-LOSS     : TRIGGERED for", sl["triggered"])
    elif sl:
        print(f"  Stop-loss     : {len(sl.get('checked',[]))} checked, none triggered")

    print()
    print("  TOP HOLDINGS:")
    for s in final.get("top_10_stocks", []):
        print("    #{}  {:<6}  score={:.4f}  {}".format(
            s["rank"], s["ticker"], s["composite_score"], s["name"][:35]))
    print()
    print("  TRADE SIGNALS:")
    for s in final.get("trade_signals", []):
        print("    [{:<4}] {:<6}  {}".format(
            s["action"], s["ticker"], s.get("entry_rationale", "")[:60]))
    print()
    risk = final.get("risk_summary", {})
    print("  RISK SUMMARY:")
    print("    Max Drawdown Est :", risk.get("max_drawdown_estimate", "N/A"))
    print("    Volatility Level :", risk.get("volatility_level", "N/A"))
    print("    Notes            :", risk.get("notes", "")[:80])
    print()

    broker = result.get("broker_result")
    if broker:
        print("  BROKER EXECUTION:")
        print("    Status        :", broker.get("status"))
        print("    Dry run       :", broker.get("dry_run"))
        print("    Market open   :", broker.get("market_open"))
        s = broker.get("summary", {})
        print("    Buys          :", s.get("buys", 0))
        print("    Holds         :", s.get("holds", 0))
        print("    Exits         :", s.get("exits", 0))
        print("    Orders placed :", s.get("orders_placed", 0))
        acct = broker.get("account_before", {})
        print("    Account equity: ${:,.2f}".format(acct.get("equity", 0)))
        print()

    print("  OUTPUT FILES:")
    for k, v in result.get("output_files", {}).items():
        if v:
            print("    {:<10}: {}".format(k, v))
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Investment Alpha - Quantitative Stock Screening Pipeline",
    )
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Specific tickers to screen (default: full universe)")
    parser.add_argument("--top", type=int, default=None,
                        help="Number of stocks to select (overrides regime; default: regime-adaptive)")
    parser.add_argument("--refresh", action="store_true",
                        help="Force re-download of all data (ignore cache)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run pipeline but skip saving output files")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose debug logging")
    parser.add_argument("--json-only", action="store_true",
                        help="Print final JSON to stdout")
    parser.add_argument("--execute", action="store_true",
                        help="Connect to Alpaca and execute paper trades after pipeline")
    parser.add_argument("--broker-dry-run", action="store_true", default=False,
                        help="With --execute: log trades but do not submit orders")
    # New Phase 1 flags
    parser.add_argument("--skip-regime", action="store_true",
                        help="Skip regime detection; use BULL defaults")
    parser.add_argument("--skip-stop-loss", action="store_true",
                        help="Skip weekly stop-loss check")
    parser.add_argument("--force-regime",
                        choices=["bull", "neutral", "bear"], default=None,
                        help="Override market regime (ignores live VIX/SPX data)")

    args = parser.parse_args()
    setup_logging(debug=args.debug)

    result = run_pipeline(
        tickers=args.tickers,
        top_n=args.top,
        force_refresh=args.refresh,
        dry_run=args.dry_run,
        execute=args.execute,
        broker_dry_run=args.broker_dry_run,
        skip_regime=args.skip_regime,
        skip_stop_loss=args.skip_stop_loss,
        force_regime=args.force_regime,
    )

    if args.json_only:
        print(json.dumps(result.get("final_output", {}), indent=2))
    else:
        print_summary(result)


if __name__ == "__main__":
    main()
