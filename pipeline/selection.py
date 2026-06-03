"""
pipeline/selection.py - Stage 5: Ranking & Selection

Ranks filtered stocks by composite score (descending).
Selects top N stocks. N comes from regime_result["active_top_n"] when
regime detection is enabled, otherwise falls back to config.TOP_N_STOCKS.

BULL   regime: top 10 stocks selected
NEUTRAL regime: top 8 stocks selected
BEAR   regime: top 5 stocks selected
"""

import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)


def run(filter_result, top_n=None, regime_result=None):
    """
    Stage 5: Rank and select top N stocks.

    Args:
        filter_result:  Output dict from filters.run()
        top_n:          Hard override (ignores config and regime)
        regime_result:  Output dict from pipeline/regime.py (optional)
                        If provided, uses regime_result["active_top_n"]

    Returns dict with keys:
        stage, status, selected (DataFrame), top_10_stocks (list), ticker_count
    """
    log.info("\n" + "=" * 50)
    log.info("STAGE 5: Ranking & Selection")
    log.info("=" * 50)

    # Resolve top_n: explicit arg > regime > config
    if top_n is not None:
        n_select = top_n
        log.info("  top_n source: explicit arg (%d)", n_select)
    elif regime_result and isinstance(regime_result, dict) and "active_top_n" in regime_result:
        n_select = regime_result["active_top_n"]
        regime_label = regime_result.get("regime", "unknown").upper()
        log.info("  top_n source: regime=%s -> %d stocks", regime_label, n_select)
    else:
        n_select = config.TOP_N_STOCKS
        log.info("  top_n source: config.TOP_N_STOCKS (%d)", n_select)

    df = filter_result.get("filtered", pd.DataFrame())
    if df.empty:
        log.error("Stage 5: No filtered stocks to select from")
        return {"stage": "selection", "status": "failed",
                "selected": pd.DataFrame(), "top_10_stocks": [], "ticker_count": 0}

    # Rank by composite score
    ranked = df.sort_values("composite_score", ascending=False).reset_index(drop=True)
    ranked["rank"] = ranked.index + 1

    selected = ranked.head(n_select).copy()

    if len(selected) < n_select:
        log.warning("Stage 5: Only %d stocks available (requested %d)", len(selected), n_select)
        status = "partial"
    else:
        status = "success"

    # JSON-serializable list
    top_10_stocks = []
    for _, row in selected.iterrows():
        top_10_stocks.append({
            "rank":            int(row["rank"]),
            "ticker":          row["ticker"],
            "name":            row.get("name", row["ticker"]),
            "sector":          row.get("sector", "Unknown"),
            "composite_score": round(float(row["composite_score"]), 4),
            "sub_scores": {
                "momentum":   round(float(row["score_momentum"]), 4),
                "trend":      round(float(row["score_trend"]), 4),
                "quality":    round(float(row["score_quality"]), 4),
                "volatility": round(float(row["score_volatility"]), 4),
            },
            "current_price": round(float(row["current_price"]), 2),
            "above_sma200":  bool(row["above_sma200"]),
            "ret_12m":  round(float(row["ret_12m"]), 4) if pd.notna(row.get("ret_12m")) else None,
            "vol_60d":  round(float(row["vol_60d"]), 4) if pd.notna(row.get("vol_60d")) else None,
        })

    log.info("Stage 5 complete -- selected top %d from %d candidates", len(selected), len(ranked))
    for item in top_10_stocks:
        log.info("  #%d %s  score=%.4f", item["rank"], item["ticker"], item["composite_score"])

    return {
        "stage":         "selection",
        "status":        status,
        "selected":      selected,
        "top_10_stocks": top_10_stocks,
        "ticker_count":  len(selected),
        "n_requested":   n_select,
    }


if __name__ == "__main__":
    import json, logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(message)s")
    from pipeline import ingestion, features, scoring, filters

    TEST_TICKERS = ["AAPL","MSFT","GOOGL","AMZN","NVDA","META","JPM","JNJ","V","UNH"]
    ing    = ingestion.run(tickers=TEST_TICKERS)
    feat   = features.run(ing)
    scored = scoring.run(feat)
    filt   = filters.run(scored)

    # Test 1: default
    r = run(filt)
    print("Default top_n:", r["ticker_count"])

    # Test 2: regime override (neutral -> 8)
    mock_regime = {"regime": "neutral", "active_top_n": 8, "active_stop_loss": 0.88}
    r2 = run(filt, regime_result=mock_regime)
    print("Regime neutral top_n:", r2["ticker_count"])

    # Test 3: explicit override
    r3 = run(filt, top_n=3)
    print("Explicit top_n=3:", r3["ticker_count"])
    print("\nStage 5 test complete")
