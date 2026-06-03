"""
pipeline/signals.py - Stage 7: Trade Signal Generation

Generates BUY / HOLD / EXIT signals by comparing new portfolio
against the prior month's saved portfolio state.

Signal logic:
  BUY  -- ticker is NEW in this month's portfolio (not in prior state)
  HOLD -- ticker was in prior portfolio AND is still selected this month
  EXIT -- ticker was in prior portfolio but did NOT make this month's selection

State file (latest_portfolio.json) now stores:
  - entry_price: price when first entered (BUY date)
  - entry_date:  ISO date string of first entry
  - regime:      market regime at time of run

These fields are consumed by broker/stop_loss.py for weekly stop-loss checks.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)

EARNINGS_BLACKOUT_DAYS = 5   # block new BUY within this many trading days of earnings


def _days_to_earnings(ticker: str) -> int | None:
    """
    Return number of calendar days until the stock's next earnings date,
    or None if unavailable. Uses yfinance Ticker.calendar.
    Cached in module-level dict for the duration of this run.
    """
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar
        if cal is None or cal.empty:
            return None
        # calendar is a DataFrame with columns as dates, index as event types
        # Earnings Date is typically in the columns
        if hasattr(cal, "columns"):
            from datetime import date, datetime as dt
            today = date.today()
            for col in cal.columns:
                try:
                    edate = pd.Timestamp(col).date()
                    days  = (edate - today).days
                    if days >= 0:
                        return days
                except Exception:
                    continue
        return None
    except Exception:
        return None


def _load_prior_portfolio():
    """Load prior run's portfolio from state file. Returns {} if none exists."""
    path = config.PORTFOLIO_STATE_FILE
    if not path.exists():
        log.info("Stage 7: No prior portfolio state -- all signals will be BUY")
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        prior = {item["ticker"]: item for item in data.get("portfolio", [])}
        log.info("Stage 7: Loaded prior portfolio -- %d stocks: %s",
                 len(prior), list(prior.keys()))
        return prior
    except Exception as exc:
        log.warning("Stage 7: Could not read prior portfolio state (%s) -- first run", exc)
        return {}


def _entry_rationale(row):
    """Generate concise entry rationale from factor scores."""
    if row.empty:
        return "Selected by composite factor score."
    parts = []
    if row.get("score_momentum", 0) >= 0.70:
        ret = row.get("ret_12m", 0) or 0
        parts.append(f"strong {ret*100:.0f}% 12M momentum")
    elif row.get("score_momentum", 0) >= 0.50:
        parts.append("positive momentum trend")
    if row.get("score_trend", 0) >= 0.60:
        pct = row.get("pct_above_sma200", 0) or 0
        parts.append(f"price {pct*100:.1f}% above 200-day MA")
    if row.get("score_quality", 0) >= 0.65:
        roe = row.get("roe", 0) or 0
        parts.append(f"high quality (ROE={roe*100:.1f}%)")
    if not parts:
        parts.append("balanced factor profile")
    return "; ".join(parts).capitalize() + "."


def _risk_note(row):
    """Generate a risk note per stock."""
    if row.empty:
        return "Standard position risk applies."
    notes = []
    vol = row.get("vol_60d", 0) or 0
    if vol > 0.30:
        notes.append(f"elevated volatility ({vol*100:.0f}% annualized)")
    de = row.get("debt_to_equity")
    if pd.notna(de) and de > 80:
        notes.append(f"high leverage (D/E={de:.0f}%)")
    if row.get("rsi_14", 50) > 70:
        notes.append("RSI overbought -- watch for pullback")
    if not notes:
        notes.append("within normal risk parameters")
    return "Monitor: " + "; ".join(notes) + "."


def run(portfolio_result, selection_result, regime_result=None):
    """
    Stage 7: Trade Signal Generation.

    Args:
        portfolio_result:  Output from portfolio.run()
        selection_result:  Output from selection.run()
        regime_result:     Optional output from pipeline/regime.py

    Returns dict with keys:
        stage, status, trade_signals, exit_signals, all_signals, signal_summary
    """
    log.info("\n" + "=" * 50)
    log.info("STAGE 7: Trade Signal Generation")
    log.info("=" * 50)

    portfolio   = portfolio_result.get("portfolio", [])
    selected_df = selection_result.get("selected", pd.DataFrame())

    if not portfolio:
        log.error("Stage 7: Empty portfolio -- no signals to generate")
        return {
            "stage": "signal_generation", "status": "failed",
            "trade_signals": [], "exit_signals": [], "all_signals": [],
            "signal_summary": {},
        }

    prior = _load_prior_portfolio()
    current_tickers = {p["ticker"] for p in portfolio}
    prior_tickers   = set(prior.keys())

    score_idx = selected_df.set_index("ticker") if not selected_df.empty else pd.DataFrame()
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    regime_label = (regime_result or {}).get("regime", "unknown")

    trade_signals = []
    exit_signals  = []

    # --- BUY / HOLD ---
    for item in portfolio:
        ticker = item["ticker"]
        is_new = ticker not in prior_tickers
        action = "BUY" if is_new else "HOLD"

        row = score_idx.loc[ticker] if ticker in score_idx.index else pd.Series(dtype=float)
        rationale = _entry_rationale(row)
        risk      = _risk_note(row)

        # Preserve entry_price and entry_date from prior state on HOLD
        if is_new:
            entry_price = item.get("entry_price", item.get("current_price"))
            entry_date  = today_iso
        else:
            prior_item  = prior[ticker]
            entry_price = prior_item.get("entry_price", item.get("current_price"))
            entry_date  = prior_item.get("entry_date", today_iso)

        # --- Earnings blackout: block BUY within 5 trading days of earnings ---
        earnings_blocked = False
        if action == "BUY" and getattr(config, "EARNINGS_BLACKOUT_ENABLED", True):
            days_out = _days_to_earnings(ticker)
            if days_out is not None and days_out <= EARNINGS_BLACKOUT_DAYS:
                action = "HOLD"   # downgrade to HOLD — don't open new position
                earnings_blocked = True
                log.info("  EARNINGS BLACKOUT: %s reports in %d days — BUY downgraded to HOLD",
                         ticker, days_out)

        signal = {
            "ticker":          ticker,
            "name":            item["name"],
            "action":          action,
            "weight":          item["weight"],
            "composite_score": item["score"],
            "entry_price":     round(float(entry_price), 4) if entry_price is not None else None,
            "entry_date":      entry_date,
            "entry_rationale": rationale,
            "risk_note":       ("EARNINGS BLACKOUT: reports within " + str(_days_to_earnings(ticker) or "?") + " days — hold off entry. " + risk) if earnings_blocked else risk,
            "signals":         item["signals"],
            "earnings_blocked": earnings_blocked,
        }
        trade_signals.append(signal)
        log.info("  %-4s %-6s  score=%.4f  entry=%.2f  %s",
                 action, ticker, item["score"],
                 entry_price or 0, rationale[:55])

    # --- EXIT ---
    for ticker in prior_tickers - current_tickers:
        prior_item = prior[ticker]
        exit_signals.append({
            "ticker":          ticker,
            "name":            prior_item.get("name", ticker),
            "action":          "EXIT",
            "weight":          0.0,
            "composite_score": prior_item.get("score"),
            "entry_price":     prior_item.get("entry_price"),
            "entry_date":      prior_item.get("entry_date"),
            "entry_rationale": "Dropped from selection -- no longer in top-ranked universe.",
            "risk_note":       "Close position at next rebalancing date.",
            "signals":         {"trend": "exit", "momentum": "exit"},
        })
        log.info("  EXIT  %-6s  (was in prior portfolio)", ticker)

    all_signals = trade_signals + exit_signals

    summary = {
        "total_signals": len(all_signals),
        "buy":           sum(1 for s in all_signals if s["action"] == "BUY"),
        "hold":          sum(1 for s in all_signals if s["action"] == "HOLD"),
        "exit":          sum(1 for s in all_signals if s["action"] == "EXIT"),
        "run_date":      today_iso,
        "regime":        regime_label,
    }

    log.info("Stage 7 complete -- BUY:%d  HOLD:%d  EXIT:%d",
             summary["buy"], summary["hold"], summary["exit"])

    # --- Persist state for next run and stop_loss.py ---
    _save_portfolio_state(trade_signals, summary, regime_result)

    return {
        "stage":          "signal_generation",
        "status":         "success",
        "trade_signals":  trade_signals,
        "exit_signals":   exit_signals,
        "all_signals":    all_signals,
        "signal_summary": summary,
    }


def _save_portfolio_state(trade_signals, summary, regime_result=None):
    """Write latest_portfolio.json with entry_price, entry_date, and regime."""
    state = {
        "run_date":  summary["run_date"],
        "regime":    (regime_result or {}).get("regime", "unknown"),
        "regime_detail": regime_result or {},
        "signal_summary": summary,
        "portfolio": [
            {
                "ticker":     s["ticker"],
                "name":       s["name"],
                "action":     s["action"],
                "weight":     s["weight"],
                "score":      s["composite_score"],
                "entry_price": s["entry_price"],
                "entry_date":  s["entry_date"],
                "signals":    s["signals"],
            }
            for s in trade_signals  # only BUY/HOLD positions
        ],
    }
    try:
        config.PORTFOLIO_STATE_FILE.write_text(json.dumps(state, indent=2))
        log.info("Portfolio state saved -> %s", config.PORTFOLIO_STATE_FILE)
    except Exception as exc:
        log.error("Failed to save portfolio state: %s", exc)


if __name__ == "__main__":
    import json, logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(message)s")
    from pipeline import ingestion, features, scoring, filters, selection, portfolio

    TEST_TICKERS = ["AAPL","MSFT","GOOGL","AMZN","NVDA","META","JPM","JNJ","V","UNH"]
    ing   = ingestion.run(tickers=TEST_TICKERS)
    feat  = features.run(ing)
    sc    = scoring.run(feat)
    filt  = filters.run(sc)
    sel   = selection.run(filt, top_n=5)
    port  = portfolio.run(sel)
    result = run(port, sel)

    print("\nStatus :", result["status"])
    print("Summary:", result["signal_summary"])

    print("\n--- Trade Signals ---")
    for s in result["trade_signals"]:
        print(f"  [{s['action']}] {s['ticker']:<6} entry_price={s['entry_price']}  entry_date={s['entry_date']}")

    # Verify state file has entry_price
    import config as _cfg
    state = json.loads(_cfg.PORTFOLIO_STATE_FILE.read_text())
    assert all("entry_price" in p for p in state["portfolio"]), "Missing entry_price in state"
    assert all("entry_date" in p for p in state["portfolio"]), "Missing entry_date in state"
    print("\nState file validated: entry_price and entry_date present")

    # Test HOLD/EXIT logic
    print("\n--- Testing HOLD/EXIT with simulated prior state ---")
    mock_state = {
        "portfolio": [
            {"ticker": "AAPL", "name": "Apple", "score": 0.45, "weight": 0.2,
             "entry_price": 150.0, "entry_date": "2025-01-01", "action": "BUY"},
            {"ticker": "TSLA", "name": "Tesla", "score": 0.38, "weight": 0.2,
             "entry_price": 200.0, "entry_date": "2025-01-01", "action": "BUY"},
        ]
    }
    _cfg.PORTFOLIO_STATE_FILE.write_text(json.dumps(mock_state))
    result2 = run(port, sel)
    print(f"BUY:{result2['signal_summary']['buy']}  HOLD:{result2['signal_summary']['hold']}  EXIT:{result2['signal_summary']['exit']}")
    assert result2["signal_summary"]["hold"] >= 1, "AAPL should be HOLD"
    assert result2["signal_summary"]["exit"] >= 1, "TSLA should be EXIT"

    # Check entry_price preserved on HOLD
    aapl_signal = next(s for s in result2["trade_signals"] if s["ticker"] == "AAPL")
    assert aapl_signal["entry_price"] == 150.0, f"Expected 150.0 got {aapl_signal['entry_price']}"
    print("HOLD entry_price preserved correctly (AAPL entry_price=150.0)")
    print("\nAll Stage 7 checks passed")
