"""
scripts/trade_outcome_logger.py — Detect closed positions and log trade outcomes

Runs after market close (strategies.yml, daily step).
Algorithm:
  1. Load yesterday's position snapshot from data/position_snapshots/
  2. Load today's snapshot (written by snapshot_positions.py earlier)
  3. For each position in yesterday that is gone or reduced today → exit detected
  4. Record: ticker, P&L%, duration, regime, bucket, signals active at exit
  5. Append to data/trade_outcomes.json

Used by factor_analysis.py to compute per-signal win rates over time.

Note on signals at entry vs exit:
  We capture signals as-of today (exit date). This isn't perfect for
  long-duration trades but gives a useful starting correlation dataset.
  A future v2 will snapshot signals at entry time too.
"""

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_DATA_DIR       = Path(__file__).parent.parent / "data"
_SNAP_DIR       = _DATA_DIR / "position_snapshots"
_OUTCOMES_FILE  = _DATA_DIR / "trade_outcomes.json"
_SCREENER_DATA  = Path(__file__).parent.parent / "screener" / "daily_sentiment_data.json"
_INSIDER_CACHE  = _DATA_DIR / "insider_cache.json"
_CONGRESS_CACHE = _DATA_DIR / "congressional_cache.json"


# ── Persistence helpers ────────────────────────────────────────────────────

def _load_outcomes() -> list:
    try:
        if _OUTCOMES_FILE.exists():
            return json.loads(_OUTCOMES_FILE.read_text(encoding="utf-8")).get("outcomes", [])
    except Exception:
        pass
    return []


def _save_outcomes(outcomes: list):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _OUTCOMES_FILE.write_text(
        json.dumps(
            {"outcomes": outcomes,
             "last_updated": datetime.now(timezone.utc).isoformat()},
            indent=2, default=str,
        ),
        encoding="utf-8",
    )


def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


# ── Signal enrichment ──────────────────────────────────────────────────────

def _get_signals(ticker: str) -> dict:
    """
    Build a signals dict for a ticker from today's screener output + cache files.
    Keys: regime, bucket, score, insider_buy, congress_buy, earnings_beat, rs_vs_spy
    """
    signals = {"regime": "UNKNOWN", "bucket": "unknown", "score": 0,
               "insider_buy": False, "congress_buy": False,
               "earnings_beat": False, "rs_vs_spy": 0}

    screener = _load_json(_SCREENER_DATA)
    macro    = screener.get("macro_score") or {}
    signals["regime"] = str(macro.get("label", "UNKNOWN")).upper().strip()
    # Strip emoji prefix
    for prefix in ("🟢 ", "🟡 ", "🟠 ", "🔴 "):
        signals["regime"] = signals["regime"].replace(prefix, "")

    for stock in screener.get("stocks", []):
        if stock.get("ticker") == ticker:
            bd = stock.get("breakdown") or {}
            signals["bucket"]        = stock.get("strategy_bucket", "unknown")
            signals["score"]         = stock.get("total_score", 0)
            signals["insider_buy"]   = bool(bd.get("insider_buy"))
            signals["congress_buy"]  = bool(bd.get("congress_buy"))
            signals["earnings_beat"] = bool(bd.get("earnings_beat"))
            signals["rs_vs_spy"]     = bd.get("rs_vs_spy", 0) or 0
            return signals  # found in screener data

    # Fallback: check cache files directly (ticker may not be in screener universe today)
    insider  = _load_json(_INSIDER_CACHE).get(ticker, {})
    congress = _load_json(_CONGRESS_CACHE).get(ticker, {})
    signals["insider_buy"]  = isinstance(insider, dict)  and insider.get("signal", 0) >= 1
    signals["congress_buy"] = isinstance(congress, dict) and congress.get("recent_buys", 0) > 0

    return signals


# ── Exit detection ─────────────────────────────────────────────────────────

def _detect_exits(prev: dict, curr: dict) -> list[dict]:
    """
    Compare yesterday → today to find full or partial exits.
    Returns list of exit dicts.
    """
    exits = []
    for ticker, pos in prev.items():
        if ticker not in curr:
            # Full exit
            exits.append({
                "ticker":      ticker,
                "exit_type":   "full",
                "entry_price": pos["avg_entry_price"],
                "exit_price":  pos["current_price"],
                "qty":         pos["qty"],
                "pnl_pct":     pos["unrealized_plpc"] * 100,
                "pnl_dollars": pos["unrealized_pl"],
            })
        else:
            qty_sold = pos["qty"] - curr[ticker]["qty"]
            if qty_sold >= 0.5:
                exits.append({
                    "ticker":      ticker,
                    "exit_type":   "partial",
                    "entry_price": pos["avg_entry_price"],
                    "exit_price":  pos["current_price"],
                    "qty_sold":    qty_sold,
                    "pnl_pct":     pos["unrealized_plpc"] * 100,
                    "pnl_dollars": pos["unrealized_pl"] * (qty_sold / pos["qty"])
                                   if pos["qty"] > 0 else 0,
                })
    return exits


def _find_entry_date(ticker: str, portfolio: str) -> str | None:
    """
    Walk back through snapshots to find the first date the ticker appeared.
    Returns ISO date string or None.
    """
    try:
        snaps = sorted(_SNAP_DIR.glob("positions_*.json"), reverse=True)
        first_seen = None
        for snap_path in snaps:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            if ticker in snap.get(portfolio, {}):
                first_seen = snap["date"]
            else:
                break   # ticker absent in earlier snap = this was entry date
        return first_seen
    except Exception:
        return None


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    today     = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    if today.weekday() == 0:        # Monday → look back to Friday
        yesterday = today - timedelta(days=3)

    prev_path = _SNAP_DIR / f"positions_{yesterday.isoformat()}.json"
    curr_path = _SNAP_DIR / f"positions_{today.isoformat()}.json"

    if not prev_path.exists():
        log.info("No yesterday snapshot (%s) — skipping", prev_path.name)
        return
    if not curr_path.exists():
        log.info("No today snapshot (%s) — run snapshot_positions.py first", curr_path.name)
        return

    prev_snap = json.loads(prev_path.read_text(encoding="utf-8"))
    curr_snap = json.loads(curr_path.read_text(encoding="utf-8"))

    outcomes  = _load_outcomes()
    today_iso = today.isoformat()
    new_count = 0

    for portfolio in ("screener", "pipeline"):
        exits = _detect_exits(
            prev_snap.get(portfolio, {}),
            curr_snap.get(portfolio, {}),
        )
        for ex in exits:
            ticker = ex["ticker"]
            # Skip already-logged exits for same ticker/date/portfolio
            if any(
                o["ticker"] == ticker and o["exit_date"] == today_iso
                and o["portfolio"] == portfolio
                for o in outcomes
            ):
                log.debug("Already logged %s %s %s — skip", ticker, portfolio, today_iso)
                continue

            entry_date    = _find_entry_date(ticker, portfolio)
            duration_days = 0
            if entry_date:
                try:
                    duration_days = (today - datetime.fromisoformat(entry_date).date()).days
                except Exception:
                    pass

            signals = _get_signals(ticker)

            record = {
                "ticker":        ticker,
                "portfolio":     portfolio,
                "exit_date":     today_iso,
                "entry_date":    entry_date,
                "duration_days": duration_days,
                "exit_type":     ex["exit_type"],
                "entry_price":   round(ex["entry_price"], 4),
                "exit_price":    round(ex["exit_price"], 4),
                "pnl_pct":       round(ex["pnl_pct"], 2),
                "pnl_dollars":   round(ex["pnl_dollars"], 2),
                "signals":       signals,
                "win":           ex["pnl_pct"] > 0,
            }
            outcomes.append(record)
            new_count += 1
            log.info(
                "  Logged exit: %s (%s) %s — P&L %.1f%% — "
                "insider=%s congress=%s earnings=%s regime=%s",
                ticker, portfolio, ex["exit_type"], ex["pnl_pct"],
                signals["insider_buy"], signals["congress_buy"],
                signals["earnings_beat"], signals["regime"],
            )

    _save_outcomes(outcomes)
    log.info("Done — %d new exits logged (total %d outcomes)", new_count, len(outcomes))


if __name__ == "__main__":
    main()
