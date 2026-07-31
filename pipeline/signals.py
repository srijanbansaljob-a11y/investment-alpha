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
_EARNINGS_CACHE: dict[str, int | None] = {}  # ticker → days_to_earnings, cleared each process


def _days_to_earnings(ticker: str) -> int | None:
    """
    Return number of calendar days until the stock's next earnings date,
    or None if unavailable. Uses yfinance Ticker.calendar.
    Cached in _EARNINGS_CACHE for the duration of this process run
    (prevents 3x repeated API calls per ticker from signals.py).
    """
    if ticker in _EARNINGS_CACHE:
        return _EARNINGS_CACHE[ticker]
    result = None
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar
        if cal is not None and not cal.empty and hasattr(cal, "columns"):
            from datetime import date
            today = date.today()
            for col in cal.columns:
                try:
                    edate = pd.Timestamp(col).date()
                    days  = (edate - today).days
                    if days >= 0:
                        result = days
                        break
                except Exception:
                    continue
    except Exception:
        pass
    _EARNINGS_CACHE[ticker] = result
    return result


def _recent_stop_exits(cooldown_days):
    """
    Tickers stopped out within the last `cooldown_days` (from stop_loss_log.json).
    Prevents the pre-flight stop-loss exiting a name and the same run re-buying it.
    Returns {ticker: iso_timestamp}.
    """
    from datetime import timedelta
    out = {}
    log_path = getattr(config, "STOP_LOSS_LOG_FILE", None)
    if not log_path or not Path(log_path).exists():
        return out
    try:
        raw = Path(log_path).read_bytes().rstrip(b"\x00")
        events = json.loads(raw)
    except Exception:
        return out
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
    for ev in events if isinstance(events, list) else []:
        if not ev.get("breached"):
            continue
        ts = ev.get("timestamp")
        try:
            when = datetime.fromisoformat(ts)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if when >= cutoff:
            tkr = ev.get("ticker")
            if tkr and (tkr not in out or ts > out[tkr]):
                out[tkr] = ts
    return out


# Durable ledger (audit P0-3): outputs/ is gitignored, so on a fresh GitHub
# Actions checkout latest_portfolio.json does not exist — every cloud run was
# stateless, generated zero EXIT signals, and the reconciler then kept the
# dropped names as "manual positions". The cloud could add but never remove
# (hence 12 live positions against top_n=10). The ledger lives in data/,
# which the workflows already `git add data/*.json` and commit.
LEDGER_FILE = config.DATA_DIR / "portfolio_state.json"


def _read_json_nullsafe(path: Path) -> dict:
    """Binary read + strip OneDrive null-byte corruption."""
    raw = Path(path).read_bytes().rstrip(b"\x00")
    return json.loads(raw)


def _load_prior_portfolio():
    """
    Prior pipeline-owned portfolio, by ticker. Tries the durable ledger
    (data/portfolio_state.json, git-tracked → exists in the cloud) first,
    then the legacy outputs/latest_portfolio.json.
    """
    for path in (LEDGER_FILE, config.PORTFOLIO_STATE_FILE):
        try:
            if not Path(path).exists():
                continue
            data = _read_json_nullsafe(path)
            prior = {item["ticker"]: item for item in data.get("portfolio", [])}
            log.info("Stage 7: Loaded prior portfolio from %s -- %d stocks: %s",
                     Path(path).name, len(prior), list(prior.keys()))
            return prior
        except Exception as exc:
            log.warning("Stage 7: Could not read %s (%s)", Path(path).name, exc)
    log.info("Stage 7: No prior portfolio state found")
    return {}


def _load_live_holdings():
    """
    Live Alpaca positions {ticker: {"qty", "avg_entry_price"}} — the SOURCE OF
    TRUTH for what is held (audit P0-3). Returns None when Alpaca is
    unreachable so callers can fall back to the ledger.
    """
    try:
        from broker.alpaca_client import get_client, get_positions
        positions = get_positions(get_client())
        return {t: {"qty": p["qty"], "avg_entry_price": p["avg_entry_price"]}
                for t, p in positions.items()}
    except Exception as exc:
        log.warning("Stage 7: Alpaca unreachable (%s) -- falling back to ledger "
                    "for held-position detection", exc)
        return None


def _sleeve_tickers() -> set:
    """Mean-reversion sleeve holdings — same account, NOT pipeline-managed.
    Excluded from EXIT generation so the pipeline never sells sleeve names."""
    try:
        path = config.DATA_DIR / "sleeve_mr.json"
        if path.exists():
            return set(_read_json_nullsafe(path).keys())
    except Exception:
        pass
    return set()


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

    # ── What is actually held? Alpaca first (audit P0-3) ──────────────────
    # BUY/HOLD/EXIT is decided against LIVE broker positions, not a state
    # file that only exists on one machine. The ledger supplies entry_date
    # memory and pipeline-ownership tagging (held ∧ not-in-ledger = manual).
    prior = _load_prior_portfolio()
    live  = _load_live_holdings()
    current_tickers = {p["ticker"] for p in portfolio}
    if live is not None:
        held_tickers = set(live.keys())
        log.info("Stage 7: held-position source = Alpaca (%d positions)", len(held_tickers))
    else:
        held_tickers = set(prior.keys())
        log.info("Stage 7: held-position source = ledger fallback (%d positions)", len(held_tickers))
    prior_tickers = held_tickers

    score_idx = selected_df.set_index("ticker") if not selected_df.empty else pd.DataFrame()
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    regime_label = (regime_result or {}).get("regime", "unknown")

    # Re-entry cooldown: names stopped out within the last N days are not re-bought
    cooldown_days = int(getattr(config, "REENTRY_COOLDOWN_DAYS", 0) or 0)
    recent_stops  = _recent_stop_exits(cooldown_days) if cooldown_days > 0 else {}

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

        # Entry price: real broker cost basis when held (Alpaca avg_entry_price
        # is the truth); ledger memory for entry_date; current price for new buys.
        if is_new:
            entry_price = item.get("entry_price", item.get("current_price"))
            entry_date  = today_iso
        else:
            prior_item  = prior.get(ticker, {})
            entry_price = (live or {}).get(ticker, {}).get("avg_entry_price") \
                          or prior_item.get("entry_price") \
                          or item.get("current_price")
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

        # Re-entry cooldown: don't re-buy a name stopped out in the last N days
        cooldown_blocked = False
        if action == "BUY" and ticker in recent_stops:
            cooldown_blocked = True
            log.info("  COOLDOWN: %s stopped out recently (<%dd) - BUY suppressed this run",
                     ticker, cooldown_days)

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
            "cooldown_blocked": cooldown_blocked,
        }
        trade_signals.append(signal)
        log.info("  %-4s %-6s  score=%.4f  entry=%.2f  %s",
                 action, ticker, item["score"],
                 entry_price or 0, rationale[:55])

    # --- EXIT (Alpaca-first, audit P0-3) ---
    # held ∧ not selected → EXIT, with two exclusions:
    #   - mean-reversion sleeve names (same account, not pipeline-managed)
    #   - manual positions (held but never in the pipeline ledger) when
    #     MANUAL_POSITION_ACTION="keep" — those stay the reconciler's business
    sleeve        = _sleeve_tickers()
    manual_action = str(getattr(config, "MANUAL_POSITION_ACTION", "keep")).lower()
    for ticker in sorted(prior_tickers - current_tickers):
        if ticker in sleeve:
            log.info("  SKIP  %-6s  (mean-reversion sleeve -- not pipeline-managed)", ticker)
            continue
        pipeline_owned = ticker in prior
        if not pipeline_owned and manual_action != "exit":
            log.info("  KEEP  %-6s  (held but not in pipeline ledger -- manual position)", ticker)
            continue
        prior_item = prior.get(ticker, {})
        exit_signals.append({
            "ticker":          ticker,
            "name":            prior_item.get("name", ticker),
            "action":          "EXIT",
            "weight":          0.0,
            "composite_score": prior_item.get("score"),
            "entry_price":     (live or {}).get(ticker, {}).get("avg_entry_price")
                               or prior_item.get("entry_price"),
            "entry_date":      prior_item.get("entry_date"),
            "entry_rationale": ("Dropped from selection -- no longer in top-ranked universe."
                                if pipeline_owned else
                                "Manual position exited (MANUAL_POSITION_ACTION=exit)."),
            "risk_note":       "Close position at next rebalancing date.",
            "signals":         {"trend": "exit", "momentum": "exit"},
        })
        log.info("  EXIT  %-6s  (%s)", ticker,
                 "held, no longer selected" if pipeline_owned else "manual, purity mode")

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
    _save_portfolio_state(trade_signals, summary, regime_result, live_holdings=live)

    return {
        "stage":          "signal_generation",
        "status":         "success",
        "trade_signals":  trade_signals,
        "exit_signals":   exit_signals,
        "all_signals":    all_signals,
        "signal_summary": summary,
    }


def _save_portfolio_state(trade_signals, summary, regime_result=None,
                          live_holdings=None):
    """
    Persist portfolio state to BOTH locations (audit P0-3):
      - outputs/latest_portfolio.json — legacy path, local convenience
      - data/portfolio_state.json    — DURABLE ledger, git-tracked, committed
        by the workflows' `git add data/*.json` step, so cloud runs are no
        longer stateless

    Ledger honesty: blocked names (earnings blackout / cooldown) that are not
    actually held are excluded — recording them as owned made never-bought
    stocks look like positions. Written atomically (temp file + os.replace)
    to survive OneDrive mid-sync corruption.
    """
    import os, tempfile
    held = set((live_holdings or {}).keys())
    entries = []
    for s in trade_signals:  # only BUY/HOLD positions
        blocked = s.get("earnings_blocked") or s.get("cooldown_blocked")
        if blocked and s["ticker"] not in held:
            continue  # not owned, not being bought — keep it out of the ledger
        entries.append({
            "ticker":     s["ticker"],
            "name":       s["name"],
            "action":     s["action"],
            "weight":     s["weight"],
            "score":      s["composite_score"],
            "entry_price": s["entry_price"],
            "entry_date":  s["entry_date"],
            "signals":    s["signals"],
        })
    state = {
        "schema_version": getattr(config, "STATE_SCHEMA_VERSION", 2),
        "run_date":  summary["run_date"],
        "regime":    (regime_result or {}).get("regime", "unknown"),
        "regime_detail": regime_result or {},
        "signal_summary": summary,
        "portfolio": entries,
    }
    payload = json.dumps(state, indent=2, default=str)
    for path in (config.PORTFOLIO_STATE_FILE, LEDGER_FILE):
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload); f.flush(); os.fsync(f.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            log.info("Portfolio state saved (atomic) -> %s", path)
        except Exception as exc:
            log.error("Failed to save portfolio state to %s: %s", path, exc)


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
