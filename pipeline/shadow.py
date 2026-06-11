"""
pipeline/shadow.py — Shadow Portfolio Logger

THE PROBLEM IT SOLVES: feedback.py only learns from the ~10 stocks the model
BUYS each month. That's a tiny sample (25-observation threshold takes 3
months) and it's selection-biased — the model never learns about the stocks
it wrongly skipped.

THE FIX: after every pipeline run, snapshot the TOP-30 ranked stocks with
their full factor scores — bought or not. A month later, evaluate() fetches
what actually happened to all 30. Result: ~3× more observations per run,
including the counterfactuals ("ranks 11–30 outperformed ranks 1–10" is a
learnable signal).

Storage: data/shadow_log.json (committed back to the repo by workflows).

API:
    record(filter_result, regime_result, top_k=30)   # call after Stage 4/5
    evaluate(min_age_days=25) -> list[observation]   # called by learning.py
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)

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


def record(filter_result: dict, regime_result: dict | None = None, top_k: int = 30) -> int:
    """Snapshot the top_k ranked stocks (with factor scores) from this run."""
    df = filter_result.get("filtered")
    if df is None or df.empty:
        log.warning("Shadow record skipped — no filtered data")
        return 0

    ranked = df.sort_values("composite_score", ascending=False).head(top_k)
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
        "date":      datetime.now(timezone.utc).date().isoformat(),
        "regime":    (regime_result or {}).get("regime", "unknown"),
        "evaluated": False,
        "stocks":    snapshot,
    })
    _save(entries)
    log.info("Shadow portfolio: recorded top-%d snapshot (%d total runs logged)",
             len(snapshot), len(entries))
    return len(snapshot)


def evaluate(min_age_days: int = 25) -> list:
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
            raw = yf.download(tickers, period="5d", auto_adjust=True, progress=False)["Close"]
            for s in entry["stocks"]:
                t, p0 = s["ticker"], s.get("price")
                if not p0:
                    continue
                try:
                    series = raw[t].dropna() if len(tickers) > 1 else raw.squeeze().dropna()
                    s["actual_return"] = round((float(series.iloc[-1]) - p0) / p0, 6)
                except Exception:
                    continue
            entry["evaluated"] = True
            entry["evaluated_at"] = today.isoformat()
            changed = True
            log.info("Shadow evaluated: %s (%d stocks, %d days old)",
                     entry["date"], len(tickers), age)
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
