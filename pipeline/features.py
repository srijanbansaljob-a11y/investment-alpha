"""
pipeline/features.py — Stage 2: Feature Engineering

Computes all factors from raw price + fundamental data.

Technical:
  - SMA(50), SMA(200)
  - RSI(14)
  - MACD line, signal, histogram

Momentum (Phase 2: skip-month when SKIP_MONTH_MOMENTUM=True):
  - 3M return  : price[-21]/price[-63]-1  (skips most recent month)
  - 6M return  : price[-21]/price[-126]-1
  - 12M return : price[-21]/price[-252]-1
  - Relative strength vs S&P 500 index (12M skip-month)
  - [Phase 4] 52-week high proximity (George & Hwang 2004)

Fundamental:
  - ROE (return on equity)
  - Debt/Equity ratio
  - Earnings growth
  - FCF yield  (free cash flow / market cap)     [Phase 2]
  - Gross margin                                  [Phase 2]
  - [Phase 4] ROA (return on assets — ROIC proxy)
  - [Phase 4] Accruals ratio (net_income - op_cashflow) / market_cap
  - [Phase 4] Asset growth (low growth = higher future returns)
  - [Phase 4] Operating margin change (expanding margin = quality signal)
  - [Phase 4] Earnings surprise % (PEAD — Post-Earnings Announcement Drift)

Returns a single flat DataFrame — one row per ticker, all factors as columns.
Tickers with insufficient history are dropped with a warning (not crashed).
"""

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)


# ── Technical Indicators ──────────────────────────────────────────────────

def _sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def _rsi(series: pd.Series, period: int = 14) -> float:
    """Compute RSI for the most recent period. Returns scalar."""
    delta = series.diff().dropna()
    if len(delta) < period:
        return np.nan
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean().iloc[-1]
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _macd(series: pd.Series, fast: int, slow: int, signal: int) -> dict:
    """Compute MACD line, signal, histogram. Returns dict of latest values."""
    if len(series) < slow + signal:
        return {"macd_line": np.nan, "macd_signal": np.nan, "macd_hist": np.nan}
    ema_fast   = series.ewm(span=fast,   adjust=False).mean()
    ema_slow   = series.ewm(span=slow,   adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    macd_sig   = macd_line.ewm(span=signal, adjust=False).mean()
    macd_hist  = macd_line - macd_sig
    return {
        "macd_line":   round(float(macd_line.iloc[-1]),   4),
        "macd_signal": round(float(macd_sig.iloc[-1]),    4),
        "macd_hist":   round(float(macd_hist.iloc[-1]),   4),
    }


def _momentum_return(series: pd.Series, days: int, skip_days: int = 0) -> float:
    """
    N-day price return (not annualized). Returns scalar.

    skip_days: number of recent days to skip before measuring start
               (skip-month momentum: skip_days=21 avoids short-term mean reversion)

    Standard  : end = series[-1],           start = series[-(days+1)]
    Skip-month: end = series[-(skip_days)], start = series[-(days+skip_days)]
    """
    total_needed = days + skip_days + 1
    if len(series) < total_needed:
        return np.nan
    if skip_days > 0:
        end   = series.iloc[-(skip_days)]
        start = series.iloc[-(days + skip_days)]
    else:
        end   = series.iloc[-1]
        start = series.iloc[-(days + 1)]
    if start == 0 or pd.isna(start) or pd.isna(end):
        return np.nan
    return round((end - start) / start, 6)


def _realized_volatility(series: pd.Series, days: int = 60) -> float:
    """Annualized realized volatility over last N days."""
    if len(series) < days + 1:
        return np.nan
    log_returns = np.log(series / series.shift(1)).dropna()
    recent = log_returns.iloc[-days:]
    return round(float(recent.std() * np.sqrt(252)), 6)


def _52w_high_proximity(series: pd.Series) -> float:
    """
    Proximity to 52-week high. Returns scalar in [-1, 0].
    0 = at the 52-week high (strongest signal).
    -0.30 = 30% below 52-week high.
    George & Hwang (2004): proximity predicts future returns.
    """
    if len(series) < 252:
        window = series
    else:
        window = series.iloc[-252:]
    high_52w = window.max()
    current  = series.iloc[-1]
    if high_52w <= 0 or pd.isna(high_52w):
        return np.nan
    return round(float((current - high_52w) / high_52w), 6)   # 0 = at high, negative = below


def _avg_dollar_volume(close: pd.Series, volume: pd.Series, days: int = 30) -> float:
    """Average daily dollar volume over last N days."""
    if len(close) < days or len(volume) < days:
        return np.nan
    dv = (close * volume).iloc[-days:]
    return round(float(dv.mean()), 2)


def _compute_valuation_vs_sector(fund_df: pd.DataFrame) -> pd.DataFrame:
    """
    For each ticker, compute how cheap/expensive it is RELATIVE to its sector peers.

    pe_vs_sector    = ticker_forward_pe  / sector_median_forward_pe  - 1
                      Negative = cheaper than sector (good), positive = more expensive
    ev_vs_sector    = ticker_ev_ebitda   / sector_median_ev_ebitda   - 1
                      Same direction

    Returns fund_df with two new columns added: pe_vs_sector, ev_vs_sector
    We use forward_pe preferred; fall back to trailing_pe if missing.
    """
    df = fund_df.copy()

    # Use forward PE preferentially, fall back to trailing
    # Guard against missing columns (e.g. stale cache or old data)
    if "forward_pe" not in df.columns:
        df["forward_pe"] = np.nan
    if "trailing_pe" not in df.columns:
        df["trailing_pe"] = np.nan
    if "ev_to_ebitda" not in df.columns:
        df["ev_to_ebitda"] = np.nan
    if "sector" not in df.columns:
        df["sector"] = "Unknown"
    df["_pe"] = df["forward_pe"].combine_first(df["trailing_pe"])

    for metric, col_out in [("_pe", "pe_vs_sector"), ("ev_to_ebitda", "ev_vs_sector")]:
        if metric not in df.columns:
            df[col_out] = np.nan
            continue

        sector_medians = (
            df[df[metric] > 0]
            .groupby("sector")[metric]
            .median()
        )
        df[col_out] = df.apply(
            lambda row: (
                (row[metric] / sector_medians[row["sector"]]) - 1
                if row["sector"] in sector_medians and not pd.isna(row[metric]) and row[metric] > 0
                else np.nan
            ),
            axis=1,
        )

    df.drop(columns=["_pe"], inplace=True, errors="ignore")

    # ── PEG-adjusted PE vs sector ────────────────────────────────────────
    # Divide the PE-vs-sector ratio by earnings growth rate so that
    # high-growth stocks are not unfairly penalised for high PE.
    # Formula: peg_adj = pe_vs_sector / max(earnings_growth, 0.05)
    # A stock with PE 25% above sector but 30% earnings growth looks
    # CHEAP on this metric, not expensive.
    if "earnings_growth" in df.columns and "pe_vs_sector" in df.columns:
        def _peg_adjust(row):
            pev = row.get("pe_vs_sector")
            eg  = row.get("earnings_growth")
            if pd.isna(pev):
                return np.nan
            # Use earnings growth floor of 5% to avoid division by tiny/negative
            growth = max(float(eg), 0.05) if (eg is not None and not pd.isna(eg)) else 0.05
            return pev / growth

        df["pe_peg_adjusted"] = df.apply(_peg_adjust, axis=1)
    else:
        df["pe_peg_adjusted"] = np.nan

    return df


# ── Feature Engineering Orchestrator ─────────────────────────────────────

def run(ingestion_result: dict) -> dict:
    """
    Stage 2: Feature Engineering.

    Args:
        ingestion_result: Output dict from ingestion.run()

    Returns:
        {
            "stage": "feature_engineering",
            "status": "success" | "partial" | "failed",
            "features": pd.DataFrame,   # one row per ticker, all factors
            "ticker_count": int,
            "failed_tickers": list,
        }
    """
    log.info(f"\n{'='*50}")
    log.info("STAGE 2: Feature Engineering")
    log.info(f"{'='*50}")

    prices_raw    = ingestion_result.get("prices", pd.DataFrame())
    fundamentals  = ingestion_result.get("fundamentals", pd.DataFrame())
    index_series  = ingestion_result.get("index", pd.Series(dtype=float))

    if prices_raw.empty:
        log.error("Stage 2: No price data — cannot compute features")
        return {"stage": "feature_engineering", "status": "failed", "features": pd.DataFrame()}

    # Extract per-ticker close, volume from MultiIndex DataFrame
    # yfinance batch download → columns = (field, ticker)
    if isinstance(prices_raw.columns, pd.MultiIndex):
        close_df  = prices_raw["Close"]
        volume_df = prices_raw.get("Volume", pd.DataFrame())
    else:
        # Single ticker or already flat
        close_df  = prices_raw[["Close"]] if "Close" in prices_raw.columns else prices_raw
        volume_df = prices_raw[["Volume"]] if "Volume" in prices_raw.columns else pd.DataFrame()

    tickers   = list(close_df.columns)
    records   = []
    failed    = []

    # Pre-compute sector-relative valuation ratios across the full universe
    # This must happen before the per-ticker loop so sector medians are correct
    if not fundamentals.empty and getattr(config, "VALUATION_ENABLED", True):
        fundamentals_val = _compute_valuation_vs_sector(fundamentals)
        fund_idx = fundamentals_val.set_index("ticker")
        log.info("Stage 2: Sector-relative valuation computed (pe_vs_sector, ev_vs_sector)")
    else:
        fund_idx = fundamentals.set_index("ticker") if not fundamentals.empty else pd.DataFrame()

    # Skip-month flag: skip 21 trading days (1 month) when computing momentum
    skip_days = 21 if getattr(config, "SKIP_MONTH_MOMENTUM", False) else 0
    if skip_days:
        log.info("Stage 2: Skip-month momentum enabled (skip_days=%d)", skip_days)

    # Index return for relative strength (12M skip-month if enabled)
    index_12m_ret = _momentum_return(index_series, config.MOMENTUM_12M, skip_days) if not index_series.empty else np.nan

    log.info(f"Stage 2: Computing features for {len(tickers)} tickers...")

    for ticker in tickers:
        try:
            close  = close_df[ticker].dropna()
            volume = volume_df[ticker].dropna() if not volume_df.empty and ticker in volume_df.columns else pd.Series(dtype=float)

            # Minimum history check
            if len(close) < config.MIN_HISTORY_DAYS:
                log.debug(f"  {ticker}: insufficient history ({len(close)} days), skipping")
                failed.append(ticker)
                continue

            # ── Technical ─────────────────────────────────────────────
            sma50_val  = _sma(close, config.SMA_SHORT).iloc[-1]
            sma200_val = _sma(close, config.SMA_LONG).iloc[-1]
            rsi_val    = _rsi(close, config.RSI_PERIOD)
            macd_vals  = _macd(close, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
            current_price = float(close.iloc[-1])

            # Price vs MAs
            pct_above_sma50  = (current_price - sma50_val)  / sma50_val  if not pd.isna(sma50_val)  else np.nan
            pct_above_sma200 = (current_price - sma200_val) / sma200_val if not pd.isna(sma200_val) else np.nan
            above_sma200     = bool(current_price > sma200_val) if not pd.isna(sma200_val) else False

            # ── Momentum (skip-month when enabled) ────────────────────
            ret_3m  = _momentum_return(close, config.MOMENTUM_3M,  skip_days)
            ret_6m  = _momentum_return(close, config.MOMENTUM_6M,  skip_days)
            ret_12m = _momentum_return(close, config.MOMENTUM_12M, skip_days)

            # Relative strength vs index (12M skip-month)
            rel_strength_12m = (
                (ret_12m - index_12m_ret)
                if not (pd.isna(ret_12m) or pd.isna(index_12m_ret))
                else np.nan
            )

            # Phase 4: 52-week high proximity (George & Hwang 2004)
            proximity_52w_high = _52w_high_proximity(close)

            # ── Risk / Volatility ──────────────────────────────────────
            vol_60d = _realized_volatility(close, 60)
            avg_dv  = _avg_dollar_volume(close, volume, 30) if not volume.empty else np.nan

            # ── Fundamental ───────────────────────────────────────────
            if ticker in fund_idx.index:
                row = fund_idx.loc[ticker]
                roe             = row.get("roe", np.nan)
                debt_to_equity  = row.get("debt_to_equity", np.nan)
                earnings_growth = row.get("earnings_growth", np.nan)
                market_cap      = row.get("market_cap", np.nan)
                name            = row.get("name", ticker)
                sector          = row.get("sector", "Unknown")
                # Quality factors (from ingestion)
                gross_margin    = row.get("gross_margins", np.nan)
                operating_margin= row.get("operating_margins", np.nan)
                free_cashflow   = row.get("free_cashflow", np.nan)
                fcf_yield       = (free_cashflow / market_cap) if (
                    not pd.isna(free_cashflow) and not pd.isna(market_cap) and market_cap > 0
                ) else np.nan
                # Valuation vs sector (Phase 3)
                pe_vs_sector    = row.get("pe_vs_sector",   np.nan)
                ev_vs_sector    = row.get("ev_vs_sector",   np.nan)
                forward_pe      = row.get("forward_pe",     np.nan)
                ev_to_ebitda    = row.get("ev_to_ebitda",   np.nan)
                # Analyst revision sentiment (Phase 3)
                analyst_target  = row.get("analyst_target_price", np.nan)
                analyst_rec     = row.get("analyst_recommendation", np.nan)
                analyst_count   = row.get("analyst_count", np.nan)
                # Analyst upside = (target / current) - 1, capped
                upside_cap      = getattr(config, "ANALYST_UPSIDE_CAP", 0.60)
                analyst_upside  = (
                    min((analyst_target / current_price) - 1, upside_cap)
                    if not pd.isna(analyst_target) and current_price > 0
                    else np.nan
                )
                # Analyst score: combine upside with recommendation (inverted: 1=buy,5=sell)
                # Normalise rec: 1->1.0, 3->0.5, 5->0.0
                analyst_rec_norm = (5 - analyst_rec) / 4 if not pd.isna(analyst_rec) else np.nan
                analyst_score    = np.nanmean([analyst_upside, analyst_rec_norm]) if (
                    not (pd.isna(analyst_upside) and pd.isna(analyst_rec_norm))
                ) else np.nan

                # ── Phase 4: New quality factors ──────────────────────
                # ROA as ROIC proxy (returnOnAssets is cleaner than ROE at isolating ops)
                roa = row.get("roa", np.nan)

                # Accruals ratio = (net_income - op_cashflow) / market_cap
                # Low accruals = cash-backed earnings = higher quality
                net_income_val = row.get("net_income_common", np.nan)
                op_cf_val      = row.get("operating_cashflow", np.nan)
                accruals_ratio = (
                    (float(net_income_val) - float(op_cf_val)) / float(market_cap)
                    if not any(pd.isna(x) or x == 0 for x in [net_income_val, op_cf_val, market_cap])
                    else np.nan
                )

                # Asset growth (Cooper et al. 2008: high asset growth → lower future returns)
                ta_y0 = row.get("total_assets_y0", np.nan)
                ta_y1 = row.get("total_assets_y1", np.nan)
                asset_growth = (
                    (float(ta_y0) - float(ta_y1)) / abs(float(ta_y1))
                    if not any(pd.isna(x) or x == 0 for x in [ta_y0, ta_y1])
                    else np.nan
                )

                # Operating margin change (expanding margin = quality compounder signal)
                op_inc_y0 = row.get("op_income_y0", np.nan)
                op_inc_y1 = row.get("op_income_y1", np.nan)
                rev_y0    = row.get("revenue_y0", np.nan)
                rev_y1    = row.get("revenue_y1", np.nan)
                if not any(pd.isna(x) or x == 0 for x in [op_inc_y0, rev_y0, op_inc_y1, rev_y1]):
                    op_margin_y0    = float(op_inc_y0) / float(rev_y0)
                    op_margin_y1    = float(op_inc_y1) / float(rev_y1)
                    op_margin_change = op_margin_y0 - op_margin_y1   # positive = expanding
                else:
                    op_margin_change = np.nan

                # Earnings surprise % (PEAD — Post-Earnings Announcement Drift)
                earnings_surprise_pct = row.get("earnings_surprise_pct", np.nan)

            else:
                roe = debt_to_equity = earnings_growth = market_cap = np.nan
                fcf_yield = gross_margin = operating_margin = np.nan
                pe_vs_sector = ev_vs_sector = forward_pe = ev_to_ebitda = np.nan
                analyst_upside = analyst_rec_norm = analyst_score = analyst_count = np.nan
                roa = accruals_ratio = asset_growth = op_margin_change = np.nan
                earnings_surprise_pct = np.nan
                name = ticker
                sector = "Unknown"

            records.append({
                # Identity
                "ticker":          ticker,
                "name":            name,
                "sector":          sector,
                # Price
                "current_price":   round(current_price, 2),
                # Technical
                "sma_50":          round(float(sma50_val), 2) if not pd.isna(sma50_val) else np.nan,
                "sma_200":         round(float(sma200_val), 2) if not pd.isna(sma200_val) else np.nan,
                "pct_above_sma50": round(float(pct_above_sma50), 4) if not pd.isna(pct_above_sma50) else np.nan,
                "pct_above_sma200":round(float(pct_above_sma200), 4) if not pd.isna(pct_above_sma200) else np.nan,
                "above_sma200":    above_sma200,
                "rsi_14":          rsi_val,
                **macd_vals,
                # Momentum (skip-month)
                "ret_3m":              ret_3m,
                "ret_6m":              ret_6m,
                "ret_12m":             ret_12m,
                "rel_strength_12m":    round(float(rel_strength_12m), 6) if not pd.isna(rel_strength_12m) else np.nan,
                "proximity_52w_high":  proximity_52w_high,   # Phase 4
                "skip_month":          skip_days > 0,
                # Volatility / Liquidity
                "vol_60d":         vol_60d,
                "avg_dollar_vol":  avg_dv,
                # Fundamental quality
                "roe":             float(roe) if not pd.isna(roe) else np.nan,
                "debt_to_equity":  float(debt_to_equity) if not pd.isna(debt_to_equity) else np.nan,
                "earnings_growth": float(earnings_growth) if not pd.isna(earnings_growth) else np.nan,
                "market_cap":      float(market_cap) if not pd.isna(market_cap) else np.nan,
                "gross_margin":    float(gross_margin) if not pd.isna(gross_margin) else np.nan,
                "operating_margin":float(operating_margin) if not pd.isna(operating_margin) else np.nan,
                "fcf_yield":       float(fcf_yield) if not pd.isna(fcf_yield) else np.nan,
                # Valuation vs sector (Phase 3) — negative = cheaper than peers = good
                "pe_vs_sector":    float(pe_vs_sector)  if not pd.isna(pe_vs_sector)  else np.nan,
                "ev_vs_sector":    float(ev_vs_sector)  if not pd.isna(ev_vs_sector)  else np.nan,
                "forward_pe":      float(forward_pe)    if not pd.isna(forward_pe)    else np.nan,
                "ev_to_ebitda":    float(ev_to_ebitda)  if not pd.isna(ev_to_ebitda)  else np.nan,
                # Analyst revision sentiment (Phase 3)
                "analyst_upside":  float(analyst_upside) if not pd.isna(analyst_upside) else np.nan,
                "analyst_score":   float(analyst_score)  if not pd.isna(analyst_score)  else np.nan,
                "analyst_count":   float(analyst_count)  if not pd.isna(analyst_count)  else np.nan,
                # Phase 4: New quality factors
                "roa":                  float(roa)                  if not pd.isna(roa)                  else np.nan,
                "accruals_ratio":       float(accruals_ratio)       if not pd.isna(accruals_ratio)       else np.nan,
                "asset_growth":         float(asset_growth)         if not pd.isna(asset_growth)         else np.nan,
                "op_margin_change":     float(op_margin_change)     if not pd.isna(op_margin_change)     else np.nan,
                "earnings_surprise_pct":float(earnings_surprise_pct) if not pd.isna(earnings_surprise_pct) else np.nan,
            })

        except Exception as e:
            log.warning(f"  {ticker}: feature computation failed — {e}")
            failed.append(ticker)

    features_df = pd.DataFrame(records)
    log.info(f"Stage 2 complete — {len(features_df)} tickers with features, {len(failed)} failed")

    return {
        "stage":          "feature_engineering",
        "status":         "success" if len(features_df) > 0 else "failed",
        "features":       features_df,
        "ticker_count":   len(features_df),
        "failed_tickers": failed,
    }


# ── Quick Test ─────────────────────────────────────────────────────────────

