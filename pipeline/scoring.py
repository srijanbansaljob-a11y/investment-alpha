"""
pipeline/scoring.py — Stage 3: Scoring Engine

Normalizes all factors and computes a composite score per ticker.

4-factor model (default):
  Score = 0.4*Momentum + 0.3*Trend + 0.2*Quality - 0.1*Volatility

5-factor model (auto-activates when 'sentiment' column is present in features):
  Score = 0.35*Momentum + 0.25*Trend + 0.20*Quality - 0.10*Volatility + 0.10*Sentiment

Normalization: percentile rank (0 to 1), robust to outliers.
Missing values filled with 0.50 (neutral) before scoring.

Sub-score construction:
  - Momentum   : avg percentile rank of ret_3m, ret_6m, ret_12m, rel_strength_12m
  - Trend      : avg percentile rank of pct_above_sma50, pct_above_sma200, rsi_14, macd_hist
  - Quality    : avg percentile rank of roe, earnings_growth, gross_margin, inv(debt_to_equity)
  - Volatility : percentile rank of vol_60d (higher vol = subtracted from score)
  - Sentiment  : avg of normalized sentiment_score + normalized reddit_mentions (Phase 2)
"""

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)


def _pct_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    """
    Percentile rank a series → values in [0, 1].
    NaN values are filled with 0.5 (neutral) after ranking.
    ascending=True  → higher value = higher score (default)
    ascending=False → lower value = higher score (e.g. debt, volatility)
    """
    ranked = series.rank(pct=True, na_option="keep", ascending=ascending)
    return ranked.fillna(0.5)


def compute_sub_scores(features: pd.DataFrame) -> pd.DataFrame:
    """
    Add normalized sub-score columns to the features DataFrame.

    Returns a copy with added columns:
      score_momentum, score_trend, score_quality, score_volatility,
      score_sentiment (if sentiment column present), composite_score.

    Auto-selects 5-factor weights when 'sentiment' column is present
    and SENTIMENT_ENABLED is True; otherwise uses 4-factor weights.
    """
    df = features.copy()

    # ── Momentum Sub-score ─────────────────────────────────────────────
    # Phase 4: adds 52-week high proximity (George & Hwang 2004)
    m_3m  = _pct_rank(df["ret_3m"])
    m_6m  = _pct_rank(df["ret_6m"])
    m_12m = _pct_rank(df["ret_12m"])
    rs_col = "rel_strength_12m" if "rel_strength_12m" in df.columns else "rel_strength_3m"
    m_rs   = _pct_rank(df[rs_col]) if rs_col in df.columns else pd.Series(0.5, index=df.index)
    momentum_components = [m_3m, m_6m, m_12m, m_rs]
    if "proximity_52w_high" in df.columns and df["proximity_52w_high"].notna().sum() > 1:
        # proximity is ≤ 0; closer to 0 = at 52w high = strong momentum → ascending=False
        m_52w = _pct_rank(df["proximity_52w_high"], ascending=False)
        momentum_components.append(m_52w)
        log.info("  Momentum: 52w-high proximity active (%d tickers)", df["proximity_52w_high"].notna().sum())
    df["score_momentum"] = (sum(momentum_components) / len(momentum_components)).round(4)

    # ── Trend Sub-score ───────────────────────────────────────────────
    t_sma50  = _pct_rank(df["pct_above_sma50"])
    t_sma200 = _pct_rank(df["pct_above_sma200"])
    t_macd   = _pct_rank(df["macd_hist"])

    # RSI: best between 40-65, score falls off outside that range
    rsi = df["rsi_14"].fillna(50)
    rsi_score  = 1 - (abs(rsi - 52.5) / 52.5).clip(0, 1)
    rsi_ranked = rsi_score.rank(pct=True).fillna(0.5)

    df["score_trend"] = ((t_sma50 + t_sma200 + rsi_ranked + t_macd) / 4).round(4)

    # ── Quality Sub-score ────────────────────────────────────────────
    # Phase 4: ROE, earnings growth, FCF yield, gross margin, low D/E,
    #          ROA (ROIC proxy), accruals, asset growth, margin expansion
    q_roe  = _pct_rank(df["roe"])
    q_eg   = _pct_rank(df["earnings_growth"])
    q_de   = _pct_rank(df["debt_to_equity"], ascending=False)
    quality_components = [q_roe, q_eg, q_de]
    if "gross_margin" in df.columns and df["gross_margin"].notna().sum() > 0:
        quality_components.append(_pct_rank(df["gross_margin"]))
    if "fcf_yield" in df.columns and df["fcf_yield"].notna().sum() > 0:
        quality_components.append(_pct_rank(df["fcf_yield"]))
    # Phase 4 additions
    if "roa" in df.columns and df["roa"].notna().sum() > 1:
        quality_components.append(_pct_rank(df["roa"]))
        log.info("  Quality: ROA (ROIC proxy) active")
    if "accruals_ratio" in df.columns and df["accruals_ratio"].notna().sum() > 1:
        quality_components.append(_pct_rank(df["accruals_ratio"], ascending=False))  # lower = better
        log.info("  Quality: Accruals ratio active")
    if "asset_growth" in df.columns and df["asset_growth"].notna().sum() > 1:
        quality_components.append(_pct_rank(df["asset_growth"], ascending=False))    # lower growth = better
        log.info("  Quality: Asset growth active")
    if "op_margin_change" in df.columns and df["op_margin_change"].notna().sum() > 1:
        quality_components.append(_pct_rank(df["op_margin_change"]))                 # expanding = better
        log.info("  Quality: Op margin change active")
    df["score_quality"] = (sum(quality_components) / len(quality_components)).round(4)

    # ── Earnings Surprise Sub-score (Phase 4 — PEAD) ─────────────────
    has_earnings_surprise = (
        "earnings_surprise_pct" in df.columns
        and df["earnings_surprise_pct"].notna().sum() > 1
    )
    if has_earnings_surprise:
        df["score_earnings_surprise"] = _pct_rank(df["earnings_surprise_pct"]).round(4)
        log.info("  Earnings surprise (PEAD): active (%d tickers)", df["earnings_surprise_pct"].notna().sum())

    # ── Valuation Sub-score (Phase 3) ────────────────────────────────
    # Lower P/E and EV/EBITDA vs sector peers = higher score (ascending=False)
    # pe_vs_sector and ev_vs_sector are negative when stock is cheaper than sector
    has_valuation = (
        getattr(config, "VALUATION_ENABLED", True)
        and "pe_vs_sector" in df.columns
        and df["pe_vs_sector"].notna().sum() > 0
    )
    if has_valuation:
        val_components = []
        # PEG-adjusted PE vs sector (preferred — growth-adjusted)
        if "pe_peg_adjusted" in df.columns and df["pe_peg_adjusted"].notna().sum() > 1:
            val_components.append(_pct_rank(df["pe_peg_adjusted"], ascending=False) * 0.50)
            # Raw PE vs sector at 25% weight (complement)
            if df["pe_vs_sector"].notna().sum() > 1:
                val_components.append(_pct_rank(df["pe_vs_sector"], ascending=False) * 0.25)
        elif df["pe_vs_sector"].notna().sum() > 1:
            # Fallback: raw PE vs sector at full weight if no PEG data
            val_components.append(_pct_rank(df["pe_vs_sector"], ascending=False) * 0.75)
        if "ev_vs_sector" in df.columns and df["ev_vs_sector"].notna().sum() > 1:
            val_components.append(_pct_rank(df["ev_vs_sector"], ascending=False) * 0.25)
        if val_components:
            df["score_valuation"] = (sum(val_components)).clip(0, 1).round(4)
        else:
            has_valuation = False
    log.info("  Valuation factor: %s (PEG-adjusted PE: %s)",
             "active" if has_valuation else "skipped (no data)",
             "yes" if "pe_peg_adjusted" in df.columns and df["pe_peg_adjusted"].notna().sum() > 1 else "no")

    # ── Volatility Sub-score ─────────────────────────────────────────
    df["score_volatility"] = _pct_rank(df["vol_60d"]).round(4)

    # ── Sentiment Sub-score (Phase 3+5: analyst + insider + congressional) ──
    # Primary:   analyst_score       — target price upside + recommendation
    # Secondary: insider_signal      — SEC EDGAR open-market purchases
    # Tertiary:  congressional_signal — STOCK Act disclosures (config.CONGRESSIONAL_ENABLED)
    has_sentiment = (
        getattr(config, "SENTIMENT_ENABLED", False)
        and "analyst_score" in df.columns
        and df["analyst_score"].notna().sum() > 0
    )
    if has_sentiment:
        df["score_sentiment"] = _pct_rank(df["analyst_score"]).round(4)
        has_insider = (
            "insider_signal" in df.columns
            and df["insider_signal"].notna().sum() > 0
        )
        has_congressional = (
            getattr(config, "CONGRESSIONAL_ENABLED", False)
            and "congressional_signal" in df.columns
            and df["congressional_signal"].notna().sum() > 0
        )
        if has_insider and has_congressional:
            # 3-way blend: analyst 30% + insider 40% + congressional 30%
            s_insider = _pct_rank(df["insider_signal"])
            s_cong    = _pct_rank(df["congressional_signal"])
            df["score_sentiment"] = (
                0.30 * df["score_sentiment"] + 0.40 * s_insider + 0.30 * s_cong
            ).round(4)
            log.info("  Sentiment blend: analyst=0.30 insider=0.40 congressional=0.30")
        elif has_insider:
            s_insider = _pct_rank(df["insider_signal"])
            df["score_sentiment"] = (
                0.60 * df["score_sentiment"] + 0.40 * s_insider
            ).round(4)
            log.info("  Sentiment blend: analyst=0.60 insider=0.40")
        elif has_congressional:
            s_cong = _pct_rank(df["congressional_signal"])
            df["score_sentiment"] = (
                0.70 * df["score_sentiment"] + 0.30 * s_cong
            ).round(4)
            log.info("  Sentiment blend: analyst=0.70 congressional=0.30")
        log.info("  Sentiment: analyst revision scores active (%d tickers)",
                 df["analyst_score"].notna().sum())

    # ── Load factor weights (learned or default) ──────────────────────
    # pipeline/feedback.py writes learned_weights.json after each run
    # If it exists, use learned weights instead of config defaults
    import json as _json
    learned_path = Path(getattr(config, "LEARNED_WEIGHTS_FILE", "data/learned_weights.json"))
    w = None
    if learned_path.exists():
        try:
            w = _json.loads(learned_path.read_text())
            log.info("  Loaded LEARNED weights from %s", learned_path)
        except Exception:
            w = None

    if w is None:
        # Fall through to config defaults
        if has_sentiment or has_valuation:
            w = getattr(config, "FACTOR_WEIGHTS_WITH_SENTIMENT", config.FACTOR_WEIGHTS)
        else:
            w = config.FACTOR_WEIGHTS

    # Determine model name for reporting
    factors_active = ["momentum", "trend", "quality", "volatility"]
    if has_valuation:          factors_active.append("valuation")
    if has_sentiment:          factors_active.append("sentiment")
    if has_earnings_surprise:  factors_active.append("earnings_surprise")
    model_name = f"{len(factors_active)}-factor"
    log.info("  Scoring model: %s  weights: %s", model_name,
             {k: round(v, 3) for k, v in w.items() if k in factors_active})

    # ── Composite Score ──────────────────────────────────────────────
    composite = (
        w.get("momentum",   0.30) * df["score_momentum"]
      + w.get("trend",      0.25) * df["score_trend"]
      + w.get("quality",    0.20) * df["score_quality"]
      - w.get("volatility", 0.10) * df["score_volatility"]
    )
    if has_valuation and "score_valuation" in df.columns:
        composite += w.get("valuation", 0.15) * df["score_valuation"]
    if has_sentiment and "score_sentiment" in df.columns:
        composite += w.get("sentiment", 0.10) * df["score_sentiment"]
    # Phase 4: earnings surprise blended into sentiment bucket (0.05 weight)
    if has_earnings_surprise and "score_earnings_surprise" in df.columns:
        composite += w.get("earnings_surprise", 0.05) * df["score_earnings_surprise"]

    df["composite_score"] = composite.round(4)
    df["scoring_model"]   = model_name

    return df


def run(features_result: dict) -> dict:
    """
    Stage 3: Scoring Engine.

    Args:
        features_result: Output dict from features.run()

    Returns:
        {
            "stage": "scoring",
            "status": "success" | "failed",
            "scored": pd.DataFrame,   # features + sub-scores + composite
            "ticker_count": int,
            "score_summary": dict,    # min/max/mean composite score
            "scores_list": list,      # JSON-serializable per-ticker scores
        }
    """
    log.info(f"\n{'='*50}")
    log.info("STAGE 3: Scoring Engine")
    log.info(f"{'='*50}")

    features = features_result.get("features", pd.DataFrame())
    if features.empty:
        log.error("Stage 3: No features data — cannot score")
        return {"stage": "scoring", "status": "failed", "scored": pd.DataFrame()}

    scored = compute_sub_scores(features)

    summary = {
        "min":  round(float(scored["composite_score"].min()), 4),
        "max":  round(float(scored["composite_score"].max()), 4),
        "mean": round(float(scored["composite_score"].mean()), 4),
        "std":  round(float(scored["composite_score"].std()), 4),
    }

    # JSON-serializable score list (spec format)
    has_sent_col = "score_sentiment" in scored.columns
    scores_list = []
    for _, row in scored.iterrows():
        entry = {
            "ticker": row["ticker"],
            "scores": {
                "momentum":   row["score_momentum"],
                "trend":      row["score_trend"],
                "quality":    row["score_quality"],
                "volatility": row["score_volatility"],
            },
            "composite_score": row["composite_score"],
            "scoring_model":   row.get("scoring_model", "4-factor"),
        }
        if has_sent_col:
            entry["scores"]["sentiment"] = row["score_sentiment"]
        scores_list.append(entry)

    log.info("Stage 3 complete -- scored %d tickers", len(scored))
    log.info("  Composite score range: [%s, %s], mean=%s",
             summary["min"], summary["max"], summary["mean"])

    return {
        "stage":         "scoring",
        "status":        "success",
        "scored":        scored,
        "ticker_count":  len(scored),
        "score_summary": summary,
        "scores_list":   scores_list,
    }


# ── Quick Test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(message)s")
    from pipeline import ingestion, features, sentiment

    print("\n=== Stage 3 Test: 5-Factor Scoring Engine ===")
    TEST_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "JPM", "JNJ", "V", "UNH"]

    ing  = ingestion.run(tickers=TEST_TICKERS)
    feat = features.run(ing)

    # Inject sentiment to trigger 5-factor model
    sent = sentiment.run(TEST_TICKERS)
    if sent["status"] in ("success", "partial"):
        feat_df = feat["features"]
        feat_df["sentiment"]       = feat_df["ticker"].map(sent["sentiment_scores"]).fillna(0.0)
        feat_df["reddit_mentions"] = feat_df["ticker"].map(sent["reddit_mentions"]).fillna(0)
        feat["features"] = feat_df
        print("Sentiment injected:", sent["tickers_fetched"], "tickers")

    result = run(feat)

    print(f"\nStatus         : {result['status']}")
    print(f"Tickers scored : {result['ticker_count']}")
    print(f"Score summary  : {result['score_summary']}")

    if not result["scored"].empty:
        df = result["scored"].sort_values("composite_score", ascending=False)
        model = df["scoring_model"].iloc[0] if "scoring_model" in df.columns else "4-factor"
        print(f"\nScoring model  : {model}")
        cols = ["ticker", "score_momentum", "score_trend", "score_quality", "score_volatility"]
        if "score_sentiment" in df.columns:
            cols.append("score_sentiment")
        cols.append("composite_score")
        print("\n--- Sub-scores + Composite ---")
        print(df[cols].to_string(index=False))

        print("\n--- Validation ---")
        assert result["scored"]["composite_score"].between(-0.5, 1.5).all(), "FAIL: Scores out of range"
        assert all(0 <= s <= 1 for s in result["scored"]["score_momentum"]), "FAIL: Momentum out of [0,1]"
        assert all(0 <= s <= 1 for s in result["scored"]["score_trend"]),    "FAIL: Trend out of [0,1]"
        assert all(0 <= s <= 1 for s in result["scored"]["score_quality"]),  "FAIL: Quality out of [0,1]"
        print("All validation checks passed")

        print("\n--- JSON spec output (first 2) ---")
        print(json.dumps(result["scores_list"][:2], indent=2))

    print("\nStage 3 test complete")
