"""
pipeline/ingestion.py — Stage 1: Data Ingestion

Responsibilities:
  - Download OHLCV price data for all tickers using yfinance (batch mode)
  - Download fundamental data per ticker
  - Cache results to disk (parquet) — skip re-download if cache is fresh
  - Return structured DataFrames + a Stage 1 status dict
  - Fail gracefully per ticker — never crash the whole pipeline on one bad ticker

Key fixes vs archive stock_screener.py:
  - Uses yfinance batch download (not one-by-one with sleep)
  - Disk cache prevents re-downloading on every run
  - Does NOT rely on Wikipedia scrape for ticker list (uses config.py)
  - Logs failed tickers clearly
"""

import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

# Add parent to path so pipeline modules can find config
sys.path.insert(0, str(Path(__file__).parent.parent))
import config

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL))
log = logging.getLogger(__name__)


# ── Cache helpers ─────────────────────────────────────────────────────────

def _cache_path(name: str) -> Path:
    return config.CACHE_DIR / f"{name}.parquet"


def _cache_is_fresh(path: Path) -> bool:
    """Return True if cache file exists and is younger than CACHE_MAX_AGE_HOURS."""
    if not path.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age < timedelta(hours=config.CACHE_MAX_AGE_HOURS)


def _save_cache(df: pd.DataFrame, name: str) -> None:
    path = _cache_path(name)
    df.to_parquet(path)
    log.info(f"  Cached → {path.name} ({len(df)} rows)")


def _load_cache(name: str) -> pd.DataFrame:
    path = _cache_path(name)
    df = pd.read_parquet(path)
    log.info(f"  Loaded from cache → {path.name} ({len(df)} rows)")
    return df


# ── Price Data ────────────────────────────────────────────────────────────

def fetch_price_data(
    tickers: list[str] | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Download OHLCV data for all tickers.

    Returns a MultiIndex DataFrame: columns = (field, ticker)
    e.g. prices["Close"]["AAPL"] gives AAPL daily close prices.

    Uses batch yfinance download — much faster than one ticker at a time.
    Cached to disk for CACHE_MAX_AGE_HOURS.
    """
    tickers = tickers or config.ALL_TICKERS
    cache_name = f"prices_{len(tickers)}t"

    if not force_refresh and _cache_is_fresh(_cache_path(cache_name)):
        log.info("Stage 1 [prices]: Using cached price data")
        return _load_cache(cache_name)

    log.info(f"Stage 1 [prices]: Downloading {len(tickers)} tickers from yfinance...")
    end_date  = datetime.today()
    start_date = end_date - timedelta(days=config.HISTORY_DAYS)

    # Batch in chunks of 100 to avoid yfinance timeouts on large universes
    CHUNK_SIZE = 100
    chunks = [tickers[i:i+CHUNK_SIZE] for i in range(0, len(tickers), CHUNK_SIZE)]
    frames = []
    for i, chunk in enumerate(chunks):
        log.info("  Price download: chunk %d/%d (%d tickers)...", i+1, len(chunks), len(chunk))
        try:
            chunk_data = yf.download(
                tickers=chunk,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                auto_adjust=True,
                progress=False,
                threads=True,
                group_by="column",
            )
            if not chunk_data.empty:
                frames.append(chunk_data)
        except Exception as e:
            log.warning("  Chunk %d failed: %s -- skipping", i+1, e)
        if i < len(chunks) - 1:
            time.sleep(1.0)   # brief pause between chunks

    if not frames:
        log.error("Stage 1 [prices]: all chunks failed!")
        return pd.DataFrame()

    # Concatenate chunks along columns
    if len(frames) == 1:
        raw = frames[0]
    else:
        raw = pd.concat(frames, axis=1)
        # Remove duplicate columns if any ticker appeared in multiple chunks
        raw = raw.loc[:, ~raw.columns.duplicated()]

    if raw.empty:
        log.error("Stage 1 [prices]: yfinance returned empty DataFrame!")
        return pd.DataFrame()

    log.info(f"Stage 1 [prices]: Downloaded {raw.shape[1]} columns, {len(raw)} days")
    _save_cache(raw, cache_name)
    return raw


# ── Fundamental Data ──────────────────────────────────────────────────────

def fetch_fundamental_data(
    tickers: list[str] | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Fetch fundamental data for each ticker using yfinance .info.

    Returns one row per ticker with columns:
        ticker, roe, debt_to_equity, earnings_growth, trailing_pe,
        market_cap, avg_volume, sector, industry, name

    Fetched one ticker at a time (no batch API for fundamentals).
    Cached to disk. Failed tickers are logged and skipped.
    """
    tickers = tickers or config.ALL_TICKERS
    cache_name = f"fundamentals_{len(tickers)}t"

    if not force_refresh and _cache_is_fresh(_cache_path(cache_name)):
        log.info("Stage 1 [fundamentals]: Using cached fundamental data")
        return _load_cache(cache_name)

    log.info(f"Stage 1 [fundamentals]: Fetching fundamentals for {len(tickers)} tickers...")
    records = []
    failed = []

    for i, ticker in enumerate(tickers):
        try:
            t_obj = yf.Ticker(ticker)
            info  = t_obj.info

            # ── Phase 4: Earnings surprise from earnings_history ───────
            earnings_surprise_pct = None
            try:
                eh = t_obj.earnings_history
                if eh is not None and not eh.empty and "surprisePercent" in eh.columns:
                    latest = eh["surprisePercent"].dropna()
                    if not latest.empty:
                        earnings_surprise_pct = float(latest.iloc[-1])
            except Exception:
                pass

            records.append({
                "ticker":              ticker,
                "name":                info.get("longName", ticker),
                "sector":              info.get("sector", "Unknown"),
                "industry":            info.get("industry", "Unknown"),
                "market_cap":          info.get("marketCap", None),
                "avg_volume":          info.get("averageVolume", None),
                # Quality factors
                "trailing_pe":         info.get("trailingPE", None),
                "forward_pe":          info.get("forwardPE", None),
                "ev_to_ebitda":        info.get("enterpriseToEbitda", None),
                "price_to_book":       info.get("priceToBook", None),
                "roe":                 info.get("returnOnEquity", None),
                "roa":                 info.get("returnOnAssets", None),
                "debt_to_equity":      info.get("debtToEquity", None),
                "earnings_growth":     info.get("earningsGrowth", None),
                "revenue_growth":      info.get("revenueGrowth", None),
                "gross_margins":       info.get("grossMargins", None),
                "operating_margins":   info.get("operatingMargins", None),
                "free_cashflow":       info.get("freeCashflow", None),
                # Phase 4: Accruals ratio inputs
                "net_income_common":   info.get("netIncomeToCommon", None),
                "operating_cashflow":  info.get("operatingCashflow", None),
                # Phase 4: EPS momentum
                "forward_eps":         info.get("forwardEps", None),
                "trailing_eps":        info.get("trailingEps", None),
                # Phase 4: PEAD — earnings surprise signal
                "earnings_surprise_pct": earnings_surprise_pct,
                # Analyst revision sentiment (Phase 3)
                "analyst_target_price":    info.get("targetMeanPrice", None),
                "analyst_target_low":      info.get("targetLowPrice", None),
                "analyst_target_high":     info.get("targetHighPrice", None),
                "analyst_recommendation":  info.get("recommendationMean", None),  # 1=strong buy, 5=sell
                "analyst_count":           info.get("numberOfAnalystOpinions", None),
                "current_price":           info.get("currentPrice", None),
            })
        except Exception as e:
            failed.append(ticker)
            log.debug(f"  Failed {ticker}: {e}")

        # Progress every 50 tickers
        if (i + 1) % 50 == 0:
            log.info(f"  Fundamentals: {i+1}/{len(tickers)} done, {len(failed)} failed so far")

        # Polite delay to avoid rate limiting
        time.sleep(0.1)

    df = pd.DataFrame(records)
    if failed:
        log.warning(f"Stage 1 [fundamentals]: {len(failed)} tickers failed — {failed[:10]}{'...' if len(failed)>10 else ''}")
    log.info(f"Stage 1 [fundamentals]: {len(df)} tickers fetched successfully")

    _save_cache(df, cache_name)
    return df


# ── Extended Fundamentals (Phase 4) ──────────────────────────────────────

def fetch_extended_fundamentals(
    tickers: list[str] | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Fetch balance sheet + income statement data for Phase 4 factors:
      - total_assets_current / total_assets_prior  → asset_growth
      - op_income_current / revenue_current        → op_margin_current
      - op_income_prior   / revenue_prior          → op_margin_prior
      - invested_capital                           → for ROIC computation

    Returns one row per ticker with extended columns.
    Cached separately from main fundamentals.
    Silently skips tickers where data is unavailable.
    """
    if not getattr(config, "EXTENDED_FUNDAMENTALS_ENABLED", True):
        log.info("Stage 1 [extended]: Disabled — skipping")
        return pd.DataFrame()

    tickers = tickers or config.ALL_TICKERS
    cache_name = f"extended_{len(tickers)}t"

    if not force_refresh and _cache_is_fresh(_cache_path(cache_name)):
        log.info("Stage 1 [extended]: Using cached extended fundamentals")
        return _load_cache(cache_name)

    log.info("Stage 1 [extended]: Fetching balance sheet + income stmt for %d tickers...", len(tickers))
    records = []

    for i, ticker in enumerate(tickers):
        rec = {"ticker": ticker,
               "total_assets_y0": None, "total_assets_y1": None,
               "op_income_y0": None, "op_income_y1": None,
               "revenue_y0": None, "revenue_y1": None,
               "invested_capital": None, "effective_tax_rate": None}
        try:
            t_obj = yf.Ticker(ticker)

            # Balance sheet — annual (2 years needed)
            bs = t_obj.balance_sheet
            if bs is not None and not bs.empty:
                if "Total Assets" in bs.index and len(bs.columns) >= 2:
                    rec["total_assets_y0"] = float(bs.loc["Total Assets"].iloc[0])
                    rec["total_assets_y1"] = float(bs.loc["Total Assets"].iloc[1])
                if "Invested Capital" in bs.index:
                    rec["invested_capital"] = float(bs.loc["Invested Capital"].iloc[0])

            # Income statement — annual (2 years)
            inc = t_obj.income_stmt
            if inc is not None and not inc.empty:
                if "Operating Income" in inc.index and len(inc.columns) >= 2:
                    rec["op_income_y0"] = float(inc.loc["Operating Income"].iloc[0])
                    rec["op_income_y1"] = float(inc.loc["Operating Income"].iloc[1])
                if "Total Revenue" in inc.index and len(inc.columns) >= 2:
                    rec["revenue_y0"] = float(inc.loc["Total Revenue"].iloc[0])
                    rec["revenue_y1"] = float(inc.loc["Total Revenue"].iloc[1])

            # Tax rate from info
            info = t_obj.info
            rec["effective_tax_rate"] = info.get("effectiveTaxRate", None)

        except Exception as e:
            log.debug("  Extended fetch failed %s: %s", ticker, e)

        records.append(rec)

        if (i + 1) % 50 == 0:
            log.info("  Extended: %d/%d done", i + 1, len(tickers))
        time.sleep(0.05)  # gentle rate limit

    df = pd.DataFrame(records)
    _save_cache(df, cache_name)
    log.info("Stage 1 [extended]: Done — %d tickers", len(df))
    return df


# ── Market Index Data ─────────────────────────────────────────────────────

def fetch_index_data(force_refresh: bool = False) -> pd.Series:
    """
    Fetch S&P 500 index (^GSPC) close prices for relative strength calculation.
    Returns a pd.Series of daily close prices indexed by date.
    """
    cache_name = "index_sp500"
    if not force_refresh and _cache_is_fresh(_cache_path(cache_name)):
        log.info("Stage 1 [index]: Using cached index data")
        df = _load_cache(cache_name)
        return df["close"]

    log.info("Stage 1 [index]: Downloading S&P 500 index data...")
    end_date = datetime.today()
    start_date = end_date - timedelta(days=config.HISTORY_DAYS)

    raw = yf.download(
        "^GSPC",
        start=start_date.strftime("%Y-%m-%d"),
        end=end_date.strftime("%Y-%m-%d"),
        auto_adjust=True,
        progress=False,
    )

    if raw.empty:
        log.error("Stage 1 [index]: Failed to download index data")
        return pd.Series(dtype=float)

    # Flatten MultiIndex if present
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [col[0].lower() for col in raw.columns]
    else:
        raw.columns = [c.lower() for c in raw.columns]

    df_to_cache = pd.DataFrame({"close": raw["close"]})
    _save_cache(df_to_cache, cache_name)
    log.info(f"Stage 1 [index]: {len(raw)} days of index data")
    return df_to_cache["close"]


# ── Stage 1 Orchestrator ──────────────────────────────────────────────────

def run(
    tickers: list[str] | None = None,
    force_refresh: bool = False,
) -> dict:
    """
    Run Stage 1: Data Ingestion.

    Returns:
        {
            "stage": "data_ingestion",
            "status": "success" | "partial" | "failed",
            "data_sources": [...],
            "prices": pd.DataFrame,       # MultiIndex OHLCV
            "fundamentals": pd.DataFrame, # One row per ticker
            "index": pd.Series,           # S&P 500 daily close
            "ticker_count": int,
            "date_range": {"start": str, "end": str},
        }
    """
    tickers = tickers or config.ALL_TICKERS
    log.info(f"\n{'='*50}")
    log.info(f"STAGE 1: Data Ingestion — {len(tickers)} tickers")
    log.info(f"{'='*50}")

    results = {
        "stage": "data_ingestion",
        "status": "failed",
        "data_sources": [],
    }

    # 1. Price data
    prices = fetch_price_data(tickers, force_refresh=force_refresh)
    if not prices.empty:
        results["data_sources"].append("price_api_yfinance")
        results["prices"] = prices

    # 2. Fundamental data
    fundamentals = fetch_fundamental_data(tickers, force_refresh=force_refresh)
    if not fundamentals.empty:
        results["data_sources"].append("fundamental_api_yfinance")

    # 3. Extended fundamentals (Phase 4): balance sheet + income stmt
    extended = fetch_extended_fundamentals(tickers, force_refresh=force_refresh)
    if not extended.empty and not fundamentals.empty:
        # Merge extended columns into fundamentals on ticker
        ext_cols = [c for c in extended.columns if c != "ticker"]
        fundamentals = fundamentals.merge(
            extended[["ticker"] + ext_cols], on="ticker", how="left"
        )
        results["data_sources"].append("extended_fundamentals_yfinance")
        log.info("Stage 1 [extended]: Merged %d extended columns into fundamentals", len(ext_cols))

    if not fundamentals.empty:
        results["fundamentals"] = fundamentals

    # 4. Index data
    index = fetch_index_data(force_refresh=force_refresh)
    if not index.empty:
        results["data_sources"].append("index_sp500")
        results["index"] = index

    # Determine status
    if prices.empty and fundamentals.empty:
        results["status"] = "failed"
    elif prices.empty or fundamentals.empty:
        results["status"] = "partial"
    else:
        results["status"] = "success"
        if isinstance(prices.index, pd.DatetimeIndex) and len(prices) > 0:
            results["date_range"] = {
                "start": str(prices.index[0].date()),
                "end":   str(prices.index[-1].date()),
            }
        results["ticker_count"] = len(tickers)

    log.info(f"Stage 1 complete — status: {results['status']}")
    log.info(f"  Sources: {results['data_sources']}")
    return results


# ── Quick Test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n=== Stage 1 Test: 10 tickers ===")
    TEST_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "JPM", "JNJ", "V", "UNH"]

    result = run(tickers=TEST_TICKERS, force_refresh=True)

    print(f"\nStatus       : {result['status']}")
    print(f"Data sources : {result['data_sources']}")

    if "prices" in result:
        p = result["prices"]
        print(f"Price shape  : {p.shape}")
        print(f"Date range   : {result.get('date_range', {})}")
        # Show Close prices for last row
        if isinstance(p.columns, pd.MultiIndex):
            closes = p["Close"].iloc[-1]
            print(f"\nLatest closes (last trading day):")
            print(closes.dropna().round(2).to_string())

    if "fundamentals" in result:
        f = result["fundamentals"]
        print(f"\nFundamentals shape: {f.shape}")
        print(f"Columns: {list(f.columns)}")
        print(f"\nSample (ROE, D/E, EPS growth):")
        print(f[["ticker","name","roe","debt_to_equity","earnings_growth"]].to_string(index=False))

    if "index" in result:
        idx = result["index"]
        print(f"\nIndex data: {len(idx)} days, latest close = {idx.iloc[-1]:.2f}")

    print(f"\n{'='*50}")
    print("Stage 1 API output:")
    api_out = {k: v for k, v in result.items() if k not in ("prices","fundamentals","index")}
    print(json.dumps(api_out, indent=2))
    print("\n✅ Stage 1 test complete")
