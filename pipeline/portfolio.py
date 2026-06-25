"""
pipeline/portfolio.py - Stage 6: Portfolio Construction

Supports two allocation modes (config.ALLOCATION_MODE):
  "equal"          - 10% per stock (1/N), capped at MAX_POSITION_WEIGHT
  "score_weighted" - weight proportional to composite score, capped at MAX_POSITION_WEIGHT

Each position includes entry_price (current_price at construction time),
which is stored in latest_portfolio.json and used by stop_loss.py.
"""

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)


def _trend_label(score):
    if score >= config.TREND_BULLISH_THRESHOLD:
        return "bullish"
    elif score >= 0.40:
        return "neutral"
    return "bearish"


def _momentum_label(score):
    if score >= config.MOMENTUM_STRONG_THRESHOLD:
        return "strong"
    elif score >= 0.40:
        return "moderate"
    return "weak"


def _compute_weights(selected_df, mode, max_weight):
    """
    Return a list of weights (floats) aligned with selected_df rows.

    Modes:
      "equal"          — 1/N per stock
      "score_weighted" — proportional to composite score
      "inv_vol"        — inverse-volatility weighting (Phase 4)
                         Gives more weight to low-volatility compounders.
                         weight_i = (1/vol_i) / sum(1/vol_j)
                         Falls back to equal weight if vol data is missing.
    """
    n = len(selected_df)
    if n == 0:
        return []

    if mode == "inv_vol":
        vols = selected_df["vol_60d"].values.astype(float)
        # Replace NaN / zero vols with median to avoid division errors
        median_vol = float(np.nanmedian(vols)) if not np.all(np.isnan(vols)) else 0.20
        vols = np.where(np.isnan(vols) | (vols <= 0), median_vol, vols)
        # Floor vols so a stale/near-zero vol can't blow up 1/vol and dominate
        vol_floor = float(getattr(config, "VOL_FLOOR", 0.05))
        vols = np.maximum(vols, vol_floor)
        inv_vols = 1.0 / vols
        raw = inv_vols / inv_vols.sum()
        # Cap and renormalise
        weights = list(raw)
        for _ in range(20):
            capped = [min(w, max_weight) for w in weights]
            overflow = sum(w - max_weight for w in weights if w > max_weight)
            uncapped_idx = [i for i, w in enumerate(weights) if w < max_weight]
            if overflow <= 0 or not uncapped_idx:
                weights = capped
                break
            extra = overflow / len(uncapped_idx)
            for i in uncapped_idx:
                capped[i] += extra
            weights = capped

    elif mode == "score_weighted":
        scores = selected_df["composite_score"].values.astype(float)
        total = scores.sum()
        if total <= 0:
            weights = [1.0 / n] * n
        else:
            raw = scores / total
            weights = list(raw)
            for _ in range(20):
                capped = [min(w, max_weight) for w in weights]
                overflow = sum(w - max_weight for w in weights if w > max_weight)
                uncapped_idx = [i for i, w in enumerate(weights) if w < max_weight]
                if overflow <= 0 or not uncapped_idx:
                    weights = capped
                    break
                extra = overflow / len(uncapped_idx)
                for i in uncapped_idx:
                    capped[i] += extra
                weights = capped

    else:
        # Equal weight (default fallback)
        raw = 1.0 / n
        weights = [min(raw, max_weight)] * n

    # Normalize to sum to 1.0
    total = sum(weights)
    weights = [round(w / total, 6) for w in weights]
    return weights


def run(selection_result, regime_result=None):
    """
    Stage 6: Portfolio Construction.

    Args:
        selection_result: Output from selection.run()
        regime_result:   Optional output from pipeline/regime.py (for metadata)

    Returns dict with keys:
        stage, status, portfolio (list), portfolio_df (DataFrame), portfolio_metrics (dict)
    """
    log.info("\n" + "=" * 50)
    log.info("STAGE 6: Portfolio Construction")
    log.info("=" * 50)

    selected = selection_result.get("selected", pd.DataFrame())
    if selected.empty:
        log.error("Stage 6: No selected stocks -- cannot build portfolio")
        return {"stage": "portfolio_construction", "status": "failed", "portfolio": []}

    n = len(selected)
    mode = config.ALLOCATION_MODE
    max_w = config.MAX_POSITION_WEIGHT
    log.info("  Mode: %s | Max weight: %.0f%% | Stocks: %d", mode, max_w * 100, n)

    weights = _compute_weights(selected, mode, max_w)

    portfolio = []
    for i, (_, row) in enumerate(selected.iterrows()):
        weight = weights[i]
        trend_lbl    = _trend_label(float(row["score_trend"]))
        momentum_lbl = _momentum_label(float(row["score_momentum"]))

        # Expected return proxy: 12M return if available, else 6M
        exp_ret = row.get("ret_12m")
        if pd.isna(exp_ret):
            exp_ret = row.get("ret_6m", np.nan)

        # entry_price is the current price at time of construction
        # stop_loss.py will compare live price against this
        entry_price = float(row["current_price"])

        portfolio.append({
            "rank":            int(row["rank"]),
            "ticker":          row["ticker"],
            "name":            row.get("name", row["ticker"]),
            "sector":          row.get("sector", "Unknown"),
            "weight":          weight,
            "weight_pct":      f"{weight * 100:.1f}%",
            "score":           round(float(row["composite_score"]), 4),
            "current_price":   round(entry_price, 2),
            "entry_price":     round(entry_price, 2),   # stored for stop-loss use
            "expected_return_proxy": round(float(exp_ret), 4) if pd.notna(exp_ret) else None,
            "risk_proxy_vol":  round(float(row["vol_60d"]), 4) if pd.notna(row.get("vol_60d")) else None,
            "signals": {
                "trend":       trend_lbl,
                "momentum":    momentum_lbl,
                "above_ma200": bool(row["above_sma200"]),
            },
        })

    # Portfolio-level aggregates
    exp_rets = [p["expected_return_proxy"] for p in portfolio if p["expected_return_proxy"] is not None]
    vols     = [p["risk_proxy_vol"] for p in portfolio if p["risk_proxy_vol"] is not None]
    total_w  = sum(p["weight"] for p in portfolio)

    regime_label = (regime_result or {}).get("regime", "unknown")

    metrics = {
        "total_weight":           round(total_w, 6),
        "stock_count":            n,
        "allocation_mode":        mode,
        "regime":                 regime_label,
        "avg_expected_return":    round(float(np.mean(exp_rets)), 4) if exp_rets else None,
        "avg_volatility":         round(float(np.mean(vols)), 4) if vols else None,
        "portfolio_vol_estimate": round(float(np.mean(vols)) * 0.6, 4) if vols else None,
        "sectors":                list(selected["sector"].unique()) if "sector" in selected.columns else [],
    }

    log.info("Stage 6 complete -- %d stocks | mode=%s | total_weight=%.4f",
             n, mode, total_w)
    if metrics["avg_expected_return"]:
        log.info("  Avg expected return proxy: %.1f%%", metrics["avg_expected_return"] * 100)
    if metrics["avg_volatility"]:
        log.info("  Avg individual vol:        %.1f%%", metrics["avg_volatility"] * 100)

    return {
        "stage":             "portfolio_construction",
        "status":            "success",
        "portfolio":         portfolio,
        "portfolio_df":      pd.DataFrame(portfolio),
        "portfolio_metrics": metrics,
    }


if __name__ == "__main__":
    import json, logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(message)s")
    from pipeline import ingestion, features, scoring, filters, selection

    TEST_TICKERS = ["AAPL","MSFT","GOOGL","AMZN","NVDA","META","JPM","JNJ","V","UNH"]
    ing  = ingestion.run(tickers=TEST_TICKERS)
    feat = features.run(ing)
    sc   = scoring.run(feat)
    filt = filters.run(sc)
    sel  = selection.run(filt, top_n=5)
    result = run(sel)

    print("\nStatus:", result["status"])
    print("Stocks:", result["portfolio_metrics"]["stock_count"])
    print("Mode  :", result["portfolio_metrics"]["allocation_mode"])
    print("Total weight:", result["portfolio_metrics"]["total_weight"])

    for p in result["portfolio"]:
        print(f"  {p['ticker']:<6} weight={p['weight_pct']:>6}  entry_price={p['entry_price']:.2f}  trend={p['signals']['trend']}")

    total_w = sum(p["weight"] for p in result["portfolio"])
    assert abs(total_w - 1.0) < 0.001, f"Weights don't sum to 1.0: {total_w}"
    assert all("entry_price" in p for p in result["portfolio"]), "Missing entry_price"
    print("\nAll portfolio checks passed")
