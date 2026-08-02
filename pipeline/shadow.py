"""
pipeline/shadow.py — Shadow Portfolio Logger

THE PROBLEM IT SOLVES: feedback.py only learns from the ~10 stocks the model
BUYS each month. That's a tiny sample (25-observation threshold takes 3
months) and it's selection-biased — the model never learns about the stocks
it wrongly skipped.

THE FIX: after every pipeline run, snapshot ~150 stocks spread across the
FULL scored universe — winners and losers, bought or not. A month later,
evaluate() measures what each did over a FIXED window. Result: an information
coefficient computed on genuine dispersion rather than on ten near-identical
names the model already liked.

Two corrections made 2026-08-01, both of which had quietly made the learner
uninformative:
  * it recorded the post-filter shortlist (10 stocks after the sector cap),
    so there were no low-scoring names to contrast against;
  * it measured "snapshot to today", so returns spanned 38-51 days and were
    correlated as though comparable.

Storage: data/shadow_log.json (committed back to the repo by workflows).

API:
    record(filter_result, regime_result, scored_result=...)  # after Stage 3/4
    evaluate(min_age_days=HORIZON_DAYS) -> list[observation] # by learning.py
"""

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)

# Every observation is measured over this same elapsed window, so that a
# factor's information coefficient answers one consistent question:
# "did the stocks I rated highly outperform over the NEXT ~month?"
# Roughly one month of calendar days ≈ 21 trading days, matching the
# pipeline's intended multi-week holding period.
HORIZON_DAYS = 30

SHADOW_LOG_FILE = config.DATA_DIR / "shadow_log.json"

FACTOR_COLS = {
    "momentum":   "score_momentum",
    "trend":      "score_trend",
    "quality":    "score_quality",
    "valuation":  "score_valuation",
    "sentiment":  "score_sentiment",
    "volatility": "score_volatility",
}


def _load() -> list:
    if not SHADOW_LOG_FILE.exists():
        return []
    try:
        raw = SHADOW_LOG_FILE.read_bytes().rstrip(b"\x00")
        return json.loads(raw) if raw else []
    except Exception as exc:
        log.warning("Shadow log unreadable (%s) — starting fresh", exc)
        return []


def _save(entries: list) -> None:
    SHADOW_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    SHADOW_LOG_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def record(filter_result: dict, regime_result: dict | None = None, top_k: int = 30,
           scored_result: dict | None = None, sample_size: int = 150,
           always_keep_top: int = 20) -> int:
    """
    Snapshot a BROAD cross-section of this run's scored universe.

    WHY THE CHANGE (2026-08-01): this used to read filter_result["filtered"],
    which is the post-selection shortlist — 10 stocks after the sector cap cut
    573 down. The learner therefore only ever saw the names the model already
    liked, across a momentum-score range of roughly 0.45-0.81. Asking "among my
    ten favourites, did the slightly-more-favourite ones do better?" is a far
    weaker question than "does my scoring rank the market?", and an information
    coefficient computed on ten similar stocks is mostly noise.

    Now it samples `sample_size` names spread evenly across the FULL scored
    universe (so the IC sees genuine dispersion — winners and losers alike),
    while always retaining the top `always_keep_top` because that is the end of
    the ranking the portfolio actually acts on.

    Falls back to the filtered shortlist if no scored universe is supplied, so
    older callers keep working.
    """
    df = (scored_result or {}).get("scored")
    source = "scored universe"
    if df is None or getattr(df, "empty", True):
        df = filter_result.get("filtered")
        source = "filtered shortlist (narrow — IC will be weak)"
    if df is None or df.empty:
        log.warning("Shadow record skipped — no data")
        return 0

    df = df[df["composite_score"].notna()]
    if "current_price" in df.columns:
        df = df[df["current_price"].notna()]
    ordered = df.sort_values("composite_score", ascending=False).reset_index(drop=True)

    n = len(ordered)
    if n <= sample_size:
        ranked = ordered
    else:
        # Evenly spaced across the ranking preserves the full score range,
        # which is what a rank correlation needs. Top names always included.
        import numpy as _np
        idx = sorted(set(range(min(always_keep_top, n))) |
                     set(_np.linspace(0, n - 1, sample_size).astype(int).tolist()))
        ranked = ordered.iloc[idx]
    snapshot = []
    for rank, (_, row) in enumerate(ranked.iterrows(), start=1):
        scores = {}
        for factor, col in FACTOR_COLS.items():
            if col in row.index and row[col] == row[col]:  # not NaN
                scores[factor] = round(float(row[col]), 4)
        snapshot.append({
            "ticker":    row["ticker"],
            "rank":      rank,
            "composite": round(float(row["composite_score"]), 4),
            "price":     round(float(row["current_price"]), 4) if "current_price" in row.index else None,
            "scores":    scores,
        })

    entries = _load()
    entries.append({
        "date":        datetime.now(timezone.utc).date().isoformat(),
        "regime":      (regime_result or {}).get("regime", "unknown"),
        "evaluated":   False,
        "universe_n":  int(n),
        "source":      source,
        "stocks":      snapshot,
    })
    _save(entries)
    scores = [s["composite"] for s in snapshot]
    log.info("Shadow: recorded %d of %d scored stocks from the %s "
             "(score range %.3f–%.3f) · %d snapshots logged",
             len(snapshot), n, source,
             min(scores) if scores else 0, max(scores) if scores else 0, len(entries))
    return len(snapshot)


def evaluate(min_age_days: int = HORIZON_DAYS) -> list:
    """
    For every unevaluated snapshot >= min_age_days old, fetch realised
    returns and mark evaluated. Returns flat observation list:
        [{ticker, scores{...}, actual_return, regime, date}, ...]
    Includes previously evaluated entries' stored results, so the learner
    always sees the full history.
    """
    import yfinance as yf

    entries = _load()
    today = datetime.now(timezone.utc).date()
    changed = False

    for entry in entries:
        if entry.get("evaluated"):
            continue
        entry_date = datetime.fromisoformat(entry["date"]).date()
        age = (today - entry_date).days
        if age < min_age_days:
            continue

        tickers = [s["ticker"] for s in entry["stocks"] if s.get("price")]
        if not tickers:
            entry["evaluated"] = True
            changed = True
            continue
        try:
            # FIXED HORIZON (2026-08-01). This used to fetch `period="5d"` and
            # take the latest close, so the "return" ran from the snapshot to
            # TODAY — whatever age that happened to be. Three snapshots
            # evaluated on the same day produced 51-, 42- and 38-day returns,
            # and the IC correlated one score set against three different
            # questions. Every observation is now measured over the same
            # window: HORIZON_DAYS calendar days after the snapshot.
            start = entry_date
            end   = entry_date + timedelta(days=HORIZON_DAYS + 7)  # pad for weekends
            raw = yf.download(tickers, start=start.isoformat(), end=end.isoformat(),
                              auto_adjust=True, progress=False)["Close"]
            target = entry_date + timedelta(days=HORIZON_DAYS)
            for s in entry["stocks"]:
                t, p0 = s["ticker"], s.get("price")
                if not p0:
                    continue
                try:
                    series = raw[t].dropna() if len(tickers) > 1 else raw.squeeze().dropna()
                    # Last close at or before the target date — same elapsed
                    # window for every stock in every snapshot.
                    upto = series[series.index.date <= target]
                    if upto.empty:
                        continue
                    s["actual_return"] = round((float(upto.iloc[-1]) - p0) / p0, 6)
                    s["horizon_days"]  = HORIZON_DAYS
                except Exception:
                    continue
            entry["evaluated"]     = True
            entry["evaluated_at"]  = today.isoformat()
            entry["horizon_days"]  = HORIZON_DAYS
            changed = True
            log.info("Shadow evaluated: %s (%d stocks over a fixed %d-day window)",
                     entry["date"], len(tickers), HORIZON_DAYS)
        except Exception as exc:
            log.warning("Shadow evaluation failed for %s: %s", entry["date"], exc)

    if changed:
        _save(entries)

    observations = []
    for entry in entries:
        if not entry.get("evaluated"):
            continue
        for s in entry["stocks"]:
            if "actual_return" in s and s.get("scores"):
                observations.append({
                    "ticker": s["ticker"],
                    "scores": s["scores"],
                    "actual_return": s["actual_return"],
                    "regime": entry.get("regime", "unknown"),
                    "date": entry["date"],
                })
    return observations


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    obs = evaluate()
    print(f"\nShadow observations available: {len(obs)}")
    by_regime = {}
    for o in obs:
        by_regime[o["regime"]] = by_regime.get(o["regime"], 0) + 1
    for r, n in by_regime.items():
        print(f"  {r}: {n}")
