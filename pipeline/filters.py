"""
pipeline/filters.py - Stage 4: Hard Filtering Rules

Five exclusion filters applied BEFORE ranking.

Filter 1 - Below 200-day MA: exclude if price < SMA(200)
Filter 2 - High Volatility: exclude top 20% by 60-day vol
Filter 3 - Low Liquidity: exclude if avg volume < MIN_AVG_VOLUME
Filter 4 - Sector Cap: keep at most SECTOR_MAX_STOCKS per sector (Phase 1)
Filter 5 - Meme Stock stub: exclude high Reddit-mention stocks (Phase 2)
"""

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)


def _apply_sector_cap(df, top_n, max_sector_weight=0.30):
    """
    Soft sector cap: instead of hard-excluding stocks by sector count,
    apply a score penalty to stocks that would push any sector above
    max_sector_weight of the final portfolio.

    Approach:
      1. Simulate greedy selection of top_n stocks by score.
      2. If adding a stock would push its sector above max_sector_weight
         of the portfolio, apply a 40% score penalty and re-sort.
      3. After selection, remaining stocks (not in top_n) are "soft-excluded"
         with a note — they are NOT hard removed from the filtered universe,
         just ranked lower.

    This preserves the best stock from every sector and avoids
    the 89% universe collapse caused by the old hard cap.
    """
    import numpy as np
    df = df.copy().sort_values("composite_score", ascending=False).reset_index(drop=True)

    sector_counts = {}
    selected_tickers = []
    penalised = set()
    excluded = []

    # Iteratively pick stocks, applying soft penalty for sector crowding
    remaining = df.copy()
    while len(selected_tickers) < top_n and not remaining.empty:
        # Pick the top scorer
        top_row = remaining.iloc[0]
        ticker  = top_row["ticker"]
        sector  = str(top_row.get("sector", "Unknown") or "Unknown")

        current_sector_count = sector_counts.get(sector, 0)
        projected_weight = (current_sector_count + 1) / top_n

        if projected_weight > max_sector_weight and ticker not in penalised:
            # Apply soft penalty and re-sort — give other sectors a chance
            mask = remaining["ticker"] == ticker
            remaining.loc[mask, "composite_score"] *= 0.60
            penalised.add(ticker)
            remaining = remaining.sort_values("composite_score", ascending=False).reset_index(drop=True)
            continue

        # Accept this stock
        selected_tickers.append(ticker)
        sector_counts[sector] = current_sector_count + 1
        remaining = remaining[remaining["ticker"] != ticker].reset_index(drop=True)

    selected = df[df["ticker"].isin(selected_tickers)].copy()
    not_selected = df[~df["ticker"].isin(selected_tickers)].copy()

    for _, row in not_selected.iterrows():
        sector = str(row.get("sector", "Unknown") or "Unknown")
        excluded.append({
            "ticker": row["ticker"],
            "reason": "Soft sector weight cap (>30% sector exposure)",
            "composite_score": row["composite_score"],
        })

    # Restore original scores for selected stocks (penalty was only for ordering)
    selected = df[df["ticker"].isin(selected_tickers)].copy()
    return selected, excluded


def run(scoring_result):
    """
    Stage 4: Filter stocks.

    Args:
        scoring_result: Output dict from scoring.run()

    Returns:
        dict with keys: stage, status, filtered, excluded,
                        filter_funnel, ticker_count_in, ticker_count_out
    """
    log.info("\n" + "=" * 50)
    log.info("STAGE 4: Filtering")
    log.info("=" * 50)

    df = scoring_result.get("scored", pd.DataFrame()).copy()
    if df.empty:
        log.error("Stage 4: No scored data to filter")
        return {"stage": "filtering", "status": "failed", "filtered": pd.DataFrame()}

    total_in = len(df)
    funnel = [{"step": "Universe (post-scoring)", "count": total_in, "removed": 0}]
    excluded_records = []

    # --- Filter 1: Below 200-day MA (Phase 4: soft boundary) ---
    # Hard exclude: stocks >HARD_EXCLUDE% below 200MA
    # Soft penalty: stocks within SOFT_ZONE% below 200MA get a composite score penalty
    hard_threshold = getattr(config, "MA200_HARD_EXCLUDE", -0.03)   # default -3%
    soft_penalty   = getattr(config, "MA200_SOFT_PENALTY", 0.85)     # default 15% penalty
    pct_col = "pct_above_sma200"

    if pct_col in df.columns:
        hard_exclude = df[pct_col] < hard_threshold
        soft_zone    = (df[pct_col] >= hard_threshold) & (df[pct_col] < 0)
        # Apply soft penalty (score reduction, not exclusion)
        n_soft = int(soft_zone.sum())
        if n_soft > 0:
            df.loc[soft_zone, "composite_score"] = (
                df.loc[soft_zone, "composite_score"] * soft_penalty
            ).round(4)
            log.info("  Filter 1 (Soft 200MA zone): penalized %d stocks (within 3%% below 200MA)", n_soft)
        n_below = int(hard_exclude.sum())
        for _, row in df[hard_exclude].iterrows():
            excluded_records.append({
                "ticker": row["ticker"],
                "reason": (
                    f"Hard below 200-day MA "
                    f"(price={row['current_price']:.2f}, {row[pct_col]*100:.1f}% below SMA200)"
                ),
                "composite_score": row["composite_score"],
            })
        df = df[~hard_exclude]
    else:
        # Fallback: original binary exclude
        below_ma = df["above_sma200"] == False  # noqa: E712
        n_below = int(below_ma.sum())
        for _, row in df[below_ma].iterrows():
            excluded_records.append({
                "ticker": row["ticker"],
                "reason": f"Below 200-day MA (price={row['current_price']:.2f} < SMA200={row['sma_200']:.2f})",
                "composite_score": row["composite_score"],
            })
        df = df[~below_ma]
    funnel.append({"step": "After MA200 filter", "count": len(df), "removed": n_below})
    log.info("  Filter 1 (200MA hard exclude): removed %d, %d remain", n_below, len(df))

    # --- Filter 2: High Volatility (top 20%) ---
    if "vol_60d" in df.columns and df["vol_60d"].notna().sum() > 0:
        vol_cutoff = df["vol_60d"].quantile(config.VOLATILITY_CUTOFF)
        high_vol = df["vol_60d"] > vol_cutoff
        n_vol = int(high_vol.sum())
        for _, row in df[high_vol].iterrows():
            excluded_records.append({
                "ticker": row["ticker"],
                "reason": (
                    f"High volatility "
                    f"(vol_60d={row['vol_60d']:.1%} > cutoff={vol_cutoff:.1%})"
                ),
                "composite_score": row["composite_score"],
            })
        df = df[~high_vol]
        funnel.append({"step": "After volatility filter", "count": len(df), "removed": n_vol})
        log.info("  Filter 2 (High Vol): removed %d (cutoff=%.1f%%), %d remain",
                 n_vol, vol_cutoff * 100, len(df))
    else:
        log.warning("  Filter 2 (High Vol): no vol_60d data, skipping")

    # --- Filter 3: Low Liquidity ---
    vol_col = None
    for col in ("avg_dollar_vol", "avg_volume", "volume"):
        if col in df.columns and df[col].notna().sum() > 0:
            vol_col = col
            break

    if vol_col:
        low_liq = df[vol_col] < config.MIN_AVG_VOLUME
        n_liq = int(low_liq.sum())
        for _, row in df[low_liq].iterrows():
            excluded_records.append({
                "ticker": row["ticker"],
                "reason": (
                    f"Low liquidity "
                    f"({vol_col}={row[vol_col]:,.0f} < {config.MIN_AVG_VOLUME:,.0f})"
                ),
                "composite_score": row["composite_score"],
            })
        df = df[~low_liq]
        funnel.append({"step": "After liquidity filter", "count": len(df), "removed": n_liq})
        log.info("  Filter 3 (Low Liq): removed %d, %d remain", n_liq, len(df))
    else:
        log.warning("  Filter 3 (Low Liquidity): no volume column found, skipping")

    # --- Filter 4: Meme Stock Exclusion (Phase 2 stub) ---
    if config.MEME_FILTER_ENABLED and "reddit_mentions" in df.columns:
        meme = df["reddit_mentions"] > config.REDDIT_MENTION_THRESHOLD
        n_meme = int(meme.sum())
        for _, row in df[meme].iterrows():
            excluded_records.append({
                "ticker": row["ticker"],
                "reason": (
                    f"Meme stock "
                    f"(reddit_mentions={row['reddit_mentions']:.0f} > "
                    f"{config.REDDIT_MENTION_THRESHOLD})"
                ),
                "composite_score": row["composite_score"],
            })
        df = df[~meme]
        funnel.append({"step": "After meme filter", "count": len(df), "removed": n_meme})
        log.info("  Filter 4 (Meme): removed %d, %d remain", n_meme, len(df))
    else:
        if config.MEME_FILTER_ENABLED:
            log.warning("  Filter 4 (Meme): enabled but no reddit_mentions column - skipping")

    # --- Filter 5: Sector Cap ---
    if config.SECTOR_CAP_ENABLED and "sector" in df.columns:
        df = df.sort_values("composite_score", ascending=False)
        top_n = getattr(config, "TOP_N_STOCKS", 10)
        max_sw = getattr(config, "SECTOR_MAX_WEIGHT", 0.30)
        df, sector_excluded = _apply_sector_cap(df, top_n, max_sector_weight=max_sw)
        n_sector = len(sector_excluded)
        excluded_records.extend(sector_excluded)
        funnel.append({
            "step": "After soft sector cap (max {:.0f}% per sector)".format(max_sw * 100),
            "count": len(df),
            "removed": n_sector,
        })
        log.info("  Filter 5 (Soft Sector Cap, max %.0f%%/sector): soft-excluded %d, %d remain",
                 max_sw * 100, n_sector, len(df))
    elif config.SECTOR_CAP_ENABLED:
        log.warning("  Filter 5 (Sector Cap): enabled but no sector column - skipping")

    # --- Final result ---
    excluded_df = pd.DataFrame(excluded_records) if excluded_records else pd.DataFrame()
    top_n = config.TOP_N_STOCKS
    status = "success" if len(df) >= top_n else ("partial" if len(df) > 0 else "failed")

    log.info("Stage 4 complete -- %d in -> %d passed, %d excluded | status=%s",
             total_in, len(df), len(excluded_records), status)

    return {
        "stage":            "filtering",
        "status":           status,
        "filtered":         df,
        "excluded":         excluded_df,
        "filter_funnel":    funnel,
        "ticker_count_in":  total_in,
        "ticker_count_out": len(df),
    }


# --- Quick Test ---
if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(message)s")

    from pipeline import ingestion, features, scoring

    print("\n=== Stage 4 Test: Filtering ===")
    TEST_TICKERS = [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META",
        "JPM", "JNJ", "V", "UNH", "XOM", "BAC",
    ]

    ing    = ingestion.run(tickers=TEST_TICKERS)
    feat   = features.run(ing)
    scored = scoring.run(feat)
    result = run(scored)

    print(f"\nStatus         : {result['status']}")
    print(f"Tickers in     : {result['ticker_count_in']}")
    print(f"Tickers passed : {result['ticker_count_out']}")

    print("\n--- Filter Funnel ---")
    for step in result["filter_funnel"]:
        print(f"  {step['step']:<52} {step['count']:>3} stocks  (removed: {step['removed']})")

    if not result["excluded"].empty:
        print("\n--- Excluded ---")
        print(result["excluded"][["ticker", "reason"]].to_string(index=False))

    if not result["filtered"].empty:
        print("\n--- Passed Tickers ---")
        cols = [c for c in ["ticker", "sector", "composite_score"]
                if c in result["filtered"].columns]
        print(result["filtered"][cols].sort_values(
            "composite_score", ascending=False).to_string(index=False))

    print("\nStage 4 test complete")
