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

_SHADOW_LOG      = _DATA_DIR / "shadow_log.json"
_CONGRESS_PIPE   = _DATA_DIR / "congressional_cache_pipeline.json"


def _shadow_snapshot_for(ticker: str, entry_date: str | None) -> dict | None:
    """
    What the model knew about this ticker when it bought.

    Returns the shadow entry from the snapshot closest to (but not after) the
    entry date, so the recorded factor scores are the ones that informed the
    decision rather than today's re-scored values.
    """
    try:
        raw = _SHADOW_LOG.read_bytes().rstrip(b"\x00")
        entries = json.loads(raw)
    except Exception:
        return None
    if not isinstance(entries, list):
        return None
    candidates = [e for e in entries
                  if not entry_date or (e.get("date") or "") <= entry_date]
    for entry in sorted(candidates, key=lambda e: e.get("date") or "", reverse=True):
        for s in entry.get("stocks", []):
            if s.get("ticker") == ticker:
                return {"snapshot_date": entry.get("date"),
                        "regime": entry.get("regime", "unknown"),
                        "rank": s.get("rank"), "composite": s.get("composite"),
                        "scores": s.get("scores") or {}}
    return None


def _get_signals(ticker: str, entry_date: str | None = None) -> dict:
    """
    What the model believed about a ticker at entry.

    REWIRED 2026-08-02: this read screener/daily_sentiment_data.json, which is
    no longer produced — so EVERY trade was logged with regime "UNKNOWN" and
    all signals False. Factor analysis then had no explanatory variables at
    all, which is why its signal table compared empty groups and reported a
    "-92pp edge" for signals no trade possessed.

    Source is now the pipeline's own shadow log, which is richer than the
    screener ever was: it stores all six factor sub-scores per ticker per
    snapshot, so "did high-momentum entries win more often?" becomes an
    answerable question rather than a boolean guess.
    """
    signals = {"regime": "UNKNOWN", "bucket": "unknown", "score": 0,
               "insider_buy": False, "congress_buy": False,
               "earnings_beat": False, "rs_vs_spy": 0}

    snap = _shadow_snapshot_for(ticker, entry_date)
    if snap:
        signals["regime"] = str(snap.get("regime", "unknown")).upper().strip()
        signals["score"]  = snap.get("composite") or 0
        signals["scores"] = snap.get("scores") or {}
        signals["rank_at_entry"]   = snap.get("rank")
        signals["scored_on"]       = snap.get("snapshot_date")
        # Bucket by where the composite sat in the model's own scale.
        c = signals["score"] or 0
        signals["bucket"] = ("high" if c >= 0.60 else
                             "mid" if c >= 0.50 else "low")

    # Alternative signals from the PIPELINE's own caches (the pipeline-scoped
    # congressional cache first — the shared one was screener-era).
    insider  = _load_json(_INSIDER_CACHE).get(ticker, {})
    congress = (_load_json(_CONGRESS_PIPE).get(ticker)
                or _load_json(_CONGRESS_CACHE).get(ticker, {}))
    signals["insider_buy"]  = isinstance(insider, dict)  and insider.get("signal", 0) >= 1
    signals["congress_buy"] = isinstance(congress, dict) and (
        congress.get("recent_buys", 0) > 0 or congress.get("signal", 0) >= 1)

    # Regime fallback: the run summary, then the durable ledger.
    if signals["regime"] in ("UNKNOWN", ""):
        for path, key in ((_DATA_DIR / "pipeline_run_latest.json", "regime"),
                          (_DATA_DIR / "portfolio_state.json", "regime")):
            try:
                blob = _load_json(path)
                val = blob.get(key)
                if isinstance(val, dict):
                    val = val.get("label")
                if val:
                    signals["regime"] = str(val).upper().strip()
                    break
            except Exception:
                continue

    return signals


# ── Exit detection ─────────────────────────────────────────────────────────

_ADMIN_EXITS_FILE = _DATA_DIR / "admin_exits.json"


def _admin_entry(admin: dict, ticker: str, exit_date: str) -> dict | None:
    """
    Look up an administrative-exit tag for (ticker, date).

    Two key forms are supported:
      "TICKER"             — most recent administrative exit for that name
      "TICKER@YYYY-MM-DD"  — a specific one

    The date-scoped form exists because a ticker can be administratively
    exited more than once: EIX left in the 2026-07-27 screener wind-down AND
    again in the 2026-07-31 pipeline liquidation. A ticker-only key would tag
    whichever date it happened to hold and silently miss the other.
    """
    scoped = admin.get(f"{ticker}@{exit_date}")
    if scoped and scoped.get("date") == exit_date:
        return scoped
    plain = admin.get(ticker)
    if plain and plain.get("date") == exit_date:
        return plain
    return None


def _admin_exits() -> dict:
    """
    {ticker: {"date", "reason"}} for exits that were ADMINISTRATIVE, not model
    decisions — e.g. the 2026-07-31 full liquidation to rebuild the book on
    fixed code.

    Why this matters (audit follow-up): outcomes are detected by diffing
    position snapshots, so the logger cannot tell "the model exited this" from
    "the owner liquidated everything". Untagged, a bulk liquidation injects a
    dozen fake trades with real P&L into the evidence base that decides whether
    the strategy works — and pushes MIN_FEEDBACK_OBSERVATIONS toward its
    threshold with noise. Tagged records stay in the file for the audit trail
    but carry exclude_from_learning=True.
    """
    try:
        if _ADMIN_EXITS_FILE.exists():
            raw = _ADMIN_EXITS_FILE.read_bytes().rstrip(b"\x00")
            return json.loads(raw) if raw else {}
    except Exception as exc:
        log.warning("Could not read admin_exits.json (%s)", exc)
    return {}


def _refresh_stale_signals(outcomes: list) -> int:
    """
    Re-derive signals for records logged while the screener source was dead.

    Every outcome written before 2026-08-02 has regime "UNKNOWN" and all
    signals False, because _get_signals read a file that no longer exists.
    Those records are otherwise fine — the P&L is real — so rather than
    discard them, re-enrich from the shadow log using each trade's entry date.

    Idempotent: only touches records whose regime is still UNKNOWN.
    """
    fixed = 0
    for rec in outcomes:
        sig = rec.get("signals") or {}
        if str(sig.get("regime", "")).upper() not in ("", "UNKNOWN"):
            continue
        fresh = _get_signals(rec.get("ticker"), rec.get("entry_date"))
        if str(fresh.get("regime", "")).upper() in ("", "UNKNOWN") and not fresh.get("scores"):
            continue        # nothing better available
        rec["signals"] = fresh
        rec["signals_refreshed"] = True
        fixed += 1
        log.info("  SIGNALS %s (%s): regime=%s bucket=%s%s",
                 rec.get("ticker"), rec.get("entry_date"), fresh.get("regime"),
                 fresh.get("bucket"),
                 f" scored_on={fresh['scored_on']}" if fresh.get("scored_on") else "")
    return fixed


def _retag_admin_exits(outcomes: list) -> int:
    """
    Repair pass: tag any ALREADY-LOGGED outcome that matches admin_exits.json.

    Why this is idempotent and runs every time (2026-07-31): the scheduled
    workflow ran this logger from a commit that predated admin tagging, and
    recorded 7 liquidation exits as genuine model trades — BMY +15.3%,
    MRK +14.1%, MTCH +12.2% — inflating the win rate with P&L that says
    nothing about the strategy. Tagging only at insert time loses that race
    whenever the cloud runs older code or admin_exits.json lands afterwards.
    Reconciling on every run closes it permanently.

    Returns the number of records repaired.
    """
    admin = _admin_exits()
    if not admin:
        return 0
    fixed = 0
    for rec in outcomes:
        entry = _admin_entry(admin, rec.get("ticker"), rec.get("exit_date"))
        if not entry:
            continue
        if rec.get("exclude_from_learning"):
            continue
        rec["exit_type"]             = "administrative"
        rec["exclude_from_learning"] = True
        rec["admin_reason"]          = entry.get("reason", "administrative")
        rec["retagged"]              = True
        fixed += 1
        log.info("  RETAG %s (%s): was counted as a model trade at %+.2f%% — "
                 "now excluded from learning",
                 rec["ticker"], rec["exit_date"], rec.get("pnl_pct", 0))
    return fixed


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

    BUG FIX 2026-07-25: this previously broke out of the loop on the very
    first snapshot. Snapshots are iterated newest-first, and the newest one is
    by definition the snapshot in which the ticker has ALREADY disappeared —
    that absence is how the exit was detected in the first place. So the
    `else: break` fired immediately and the function returned None every time,
    making entry_date None and duration_days 0 for all four logged trades.

    Correct walk has two phases:
      1. Skip the trailing snapshots where the ticker is absent (post-exit).
      2. Once found, keep walking back while it is still held, recording the
         earliest date seen. Stop at the first gap — that is the entry.
    """
    try:
        snaps = sorted(_SNAP_DIR.glob("positions_*.json"), reverse=True)
        first_seen = None
        seen_yet = False
        for snap_path in snaps:
            try:
                snap = json.loads(snap_path.read_text(encoding="utf-8"))
            except Exception:
                continue                     # skip unreadable snapshot, keep walking
            held = ticker in (snap.get(portfolio) or {})
            if held:
                seen_yet = True
                first_seen = snap.get("date") or first_seen
            elif seen_yet:
                break                        # gap after the holding period = entry boundary
            # not held and not seen yet -> still in the post-exit tail, keep going
        return first_seen
    except Exception:
        return None


# ── Main ───────────────────────────────────────────────────────────────────

def _run_repair_only(reason: str) -> None:
    """
    Run the administrative-exit repair pass on its own.

    The repair diffs nothing and needs no snapshots — it only reconciles
    already-logged outcomes against admin_exits.json. It therefore must NOT be
    skipped when snapshot files are missing.

    BUG FIX 2026-08-01: the repair lived at the end of main(), after two early
    returns for missing snapshots. The day after a liquidation the snapshot for
    "today" didn't exist yet, main() returned at that guard, and the 7
    mislogged liquidation exits stayed counted as model trades — the repair
    reported success while never having run.
    """
    log.info("%s — running administrative-exit repair only", reason)
    outcomes = _load_outcomes()
    repaired  = _retag_admin_exits(outcomes)
    refreshed = _refresh_stale_signals(outcomes)
    if repaired or refreshed:
        _save_outcomes(outcomes)
    if refreshed:
        log.info("Signals re-derived for %d record(s) logged while the screener "
                 "source was dead", refreshed)
    model = [o for o in outcomes if not o.get("exclude_from_learning")]
    log.info("Repair pass: %d retagged (total %d outcomes, %d count as MODEL trades)",
             repaired, len(outcomes), len(model))


def main():
    today     = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    if today.weekday() == 0:        # Monday → look back to Friday
        yesterday = today - timedelta(days=3)

    prev_path = _SNAP_DIR / f"positions_{yesterday.isoformat()}.json"
    curr_path = _SNAP_DIR / f"positions_{today.isoformat()}.json"

    if not prev_path.exists():
        _run_repair_only(f"No yesterday snapshot ({prev_path.name})")
        return
    if not curr_path.exists():
        _run_repair_only(f"No today snapshot ({curr_path.name}) — "
                         "run snapshot_positions.py for new exits")
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

            # Pass entry_date so the recorded scores are what the model knew
            # when it BOUGHT, not a re-score using today's data.
            signals = _get_signals(ticker, entry_date)

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

            # Administrative exits (owner liquidation, migrations) are recorded
            # for the audit trail but must NOT count as model evidence.
            admin = _admin_entry(_admin_exits(), ticker, today_iso)
            if admin:
                record["exit_type"]            = "administrative"
                record["exclude_from_learning"] = True
                record["admin_reason"]          = admin.get("reason", "administrative")
                log.info("  %s tagged ADMINISTRATIVE (%s) — excluded from learning",
                         ticker, record["admin_reason"])

            outcomes.append(record)
            new_count += 1
            log.info(
                "  Logged exit: %s (%s) %s — P&L %.1f%% — "
                "insider=%s congress=%s earnings=%s regime=%s",
                ticker, portfolio, ex["exit_type"], ex["pnl_pct"],
                signals["insider_buy"], signals["congress_buy"],
                signals["earnings_beat"], signals["regime"],
            )

    # Repair pass — see _retag_admin_exits for why this runs unconditionally.
    repaired  = _retag_admin_exits(outcomes)
    _refresh_stale_signals(outcomes)

    _save_outcomes(outcomes)
    model = [o for o in outcomes if not o.get("exclude_from_learning")]
    log.info("Done — %d new exits logged, %d retagged administrative "
             "(total %d outcomes, %d count as MODEL trades)",
             new_count, repaired, len(outcomes), len(model))


if __name__ == "__main__":
    main()
