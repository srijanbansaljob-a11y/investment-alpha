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


# ── TTM fundamentals from quarterly statements ────────────────────────────
#
# WHY THIS EXISTS (2026-07):
# Fundamentals used to come from yfinance's `.info` dict — pre-computed ratios
# (returnOnEquity, operatingMargins, freeCashflow, ...) whose methodology Yahoo
# does not document. Two problems that made that untenable:
#
#   1. MIXED VINTAGES. `.info` ratios refresh ~quarterly, but asset_growth and
#      op_margin_change were built from ANNUAL statements. On the same run for
#      the same stock, some quality inputs were ~4 months old and others ~10
#      months old, blended into one score.
#
#   2. UNVERIFIABLE NUMBERS. Measured 2026-07-17, Yahoo's info.freeCashflow
#      matched neither TTM nor the latest annual figure:
#          CAT : ours 7.90B  | Yahoo 3.78B  | annual 7.45B
#          XOM : ours 18.79B | Yahoo 11.63B | annual 23.61B
#          AAPL: ours 129.17B| Yahoo 101.09B| annual 98.77B
#      Our TTM reconciles exactly with yfinance's own 'Free Cash Flow' row
#      (OCF + capex), so it is reproducible; Yahoo's is a black box.
#
# So: compute everything ourselves from quarterly statements on one cadence.
#   - FLOW items (revenue, income, cash flow) → sum of last 4 quarters (TTM)
#   - STOCK items (assets, equity, debt, shares) → balance-sheet point-in-time;
#     averaged over 4 quarters for return ratios (standard ROE/ROA convention)
#
# NOTE ON GAAP vs ADJUSTED: TTM sums are GAAP as reported, so a one-time charge
# lands in the number. Example: MRK's quarter ending 2026-03 carried a −$3.20B
# operating loss (IPR&D writeoff), giving TTM op margin 19.7% vs Yahoo's
# smoothed 38.6%. Ours reflects what actually happened; Yahoo's is adjusted by
# an undisclosed method. We keep GAAP — it is auditable and consistent.
#
# Every helper returns None rather than guessing when data is missing, and
# callers fall back to `.info`. This matters for FINANCIALS: banks (e.g. JPM)
# do not report 'Gross Profit' or 'Operating Income' at all, so those metrics
# are legitimately None for them — never substitute a same-shaped row like
# 'Operating Revenue', which silently yields a nonsense 100% margin.

_TTM_QUARTERS = 4


def _stmt_row(df, *names):
    """First matching row from a statement as newest-first floats, else None."""
    if df is None or getattr(df, "empty", True):
        return None
    for name in names:
        if name in df.index:
            series = df.loc[name].dropna()
            if len(series):
                try:
                    return [float(x) for x in series.values]
                except (TypeError, ValueError):
                    return None
    return None


def _ttm_sum(values, quarters: int = _TTM_QUARTERS):
    """Trailing-twelve-month total for a FLOW item. None if <4 quarters exist."""
    if not values or len(values) < quarters:
        return None
    return sum(values[:quarters])


def _avg_balance(values, quarters: int = _TTM_QUARTERS):
    """Average of a point-in-time STOCK item across available quarters."""
    if not values:
        return None
    window = values[: min(quarters, len(values))]
    return sum(window) / len(window) if window else None


def _safe_div(numerator, denominator):
    """Divide, returning None on missing/zero denominator."""
    if numerator is None or denominator in (None, 0):
        return None
    try:
        return numerator / denominator
    except ZeroDivisionError:
        return None


def compute_ttm_fundamentals(t_obj, current_price=None) -> dict:
    """
    Build TTM fundamentals for one ticker from its quarterly statements.

    Returns a dict whose keys mirror the `.info`-derived names they replace.
    Any metric that cannot be computed from available data is None so the
    caller can fall back to `.info` rather than fabricating a value.
    """
    out = {
        "roe": None, "roa": None, "gross_margins": None, "operating_margins": None,
        "debt_to_equity": None, "free_cashflow": None, "market_cap": None,
        "shares_outstanding": None, "net_income_common": None,
        "operating_cashflow": None, "ttm_revenue": None,
        "ttm_asof": None, "ttm_quarters": 0, "ttm_complete": False,
    }

    try:
        q_inc = t_obj.quarterly_income_stmt
        q_bal = t_obj.quarterly_balance_sheet
        q_cf  = t_obj.quarterly_cashflow
    except Exception:
        return out

    # Flow items (income statement / cash flow) — summed over 4 quarters
    revenue   = _stmt_row(q_inc, "Total Revenue")
    gross     = _stmt_row(q_inc, "Gross Profit")
    op_income = _stmt_row(q_inc, "Operating Income")     # no fallback — see note above
    net_inc   = _stmt_row(q_inc, "Net Income Common Stockholders", "Net Income")
    ocf       = _stmt_row(q_cf,  "Operating Cash Flow")
    capex     = _stmt_row(q_cf,  "Capital Expenditure")  # negative in yfinance

    # Stock items (balance sheet) — point-in-time
    assets = _stmt_row(q_bal, "Total Assets")
    equity = _stmt_row(q_bal, "Stockholders Equity", "Total Equity Gross Minority Interest")
    debt   = _stmt_row(q_bal, "Total Debt")
    shares = _stmt_row(q_bal, "Ordinary Shares Number")

    ttm_revenue = _ttm_sum(revenue)
    ttm_net_inc = _ttm_sum(net_inc)
    ttm_gross   = _ttm_sum(gross)
    ttm_op_inc  = _ttm_sum(op_income)
    ttm_ocf     = _ttm_sum(ocf)
    ttm_capex   = _ttm_sum(capex)

    # NEGATIVE-EQUITY GUARD. Companies that have bought back more stock than
    # they have retained earnings can carry negative book equity (e.g. MTCH at
    # −$0.22B, 2026-03). ROE and D/E are mathematically meaningless there, and
    # actively dangerous: scoring.py ranks debt_to_equity ascending, so a
    # −1822% D/E would rank as the LOWEST-leverage name in the universe —
    # exactly backwards. Yahoo's `.info` returns None in this case; we match
    # that so these names are excluded from the ranking rather than topping it.
    avg_equity = _avg_balance(equity)
    equity_usable = avg_equity is not None and avg_equity > 0

    # Return ratios use AVERAGE balance over the period (standard convention:
    # a TTM flow divided by a single point-in-time balance mismatches periods).
    out["roe"] = _safe_div(ttm_net_inc, avg_equity) if equity_usable else None
    out["roa"] = _safe_div(ttm_net_inc, _avg_balance(assets))

    out["gross_margins"]     = _safe_div(ttm_gross,  ttm_revenue)
    out["operating_margins"] = _safe_div(ttm_op_inc, ttm_revenue)

    # Leverage is a same-date balance-sheet comparison — use latest quarter.
    # UNITS: emitted as a PERCENT (79.55 means 0.7955x), matching yfinance's
    # debtToEquity convention. This is deliberate — pipeline/signals.py tests
    # `debt_to_equity > 80` for its high-leverage warning, so switching to a
    # plain ratio here would silently disable that check. scoring.py only
    # percentile-ranks the column, so it is unaffected either way.
    # Negative equity → None (see guard above), never a misleading negative.
    if debt and equity and equity[0] and equity[0] > 0:
        ratio = _safe_div(debt[0], equity[0])
        out["debt_to_equity"] = ratio * 100 if ratio is not None else None

    # capex is reported negative, so addition subtracts it
    if ttm_ocf is not None and ttm_capex is not None:
        out["free_cashflow"] = ttm_ocf + ttm_capex

    # Market cap from PRIMITIVES: shares outstanding x live price, rather than
    # Yahoo's pre-baked marketCap. fcf_yield and accruals_ratio both divide by
    # this, so a stale vendor value silently propagates into two more factors.
    if shares:
        out["shares_outstanding"] = shares[0]
        if current_price:
            out["market_cap"] = shares[0] * current_price

    out["net_income_common"]  = ttm_net_inc
    out["operating_cashflow"] = ttm_ocf
    out["ttm_revenue"]        = ttm_revenue

    # Provenance so staleness is visible downstream instead of assumed
    try:
        if q_inc is not None and not q_inc.empty and len(q_inc.columns):
            out["ttm_asof"]      = str(q_inc.columns[0].date())
            out["ttm_quarters"]  = min(len(q_inc.columns), _TTM_QUARTERS)
            out["ttm_complete"]  = len(q_inc.columns) >= _TTM_QUARTERS
    except Exception:
        pass

    return out


# ── Fundamental Data ──────────────────────────────────────────────────────

def fetch_fundamental_data(
    tickers: list[str] | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Fetch fundamental data for each ticker.

    Quality ratios (roe, roa, margins, debt_to_equity, free_cashflow) and
    market_cap are computed as TTM from QUARTERLY statements — see
    compute_ttm_fundamentals() for why, and for the negative-equity and
    financials caveats. yfinance `.info` is used only as a per-field fallback
    when a statement line is genuinely unavailable, and still supplies fields
    with no statement equivalent (sector, analyst targets, forward PE, etc).

    Returns one row per ticker with columns:
        ticker, roe, roa, debt_to_equity, earnings_growth, trailing_pe,
        market_cap, shares_outstanding, avg_volume, sector, industry, name,
        ttm_asof, ttm_complete

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

            # ── TTM fundamentals from quarterly statements ──────────────
            # Preferred over `.info` pre-computed ratios: single cadence,
            # reproducible math, GAAP as-reported. Falls back per-field to
            # `.info` when a statement line is unavailable (e.g. banks have
            # no 'Gross Profit'/'Operating Income'; some ADRs file annually).
            price_now = info.get("currentPrice") or info.get("regularMarketPrice")
            ttm = compute_ttm_fundamentals(t_obj, current_price=price_now)

            def _prefer(ttm_key, info_key):
                """TTM value when computable, else Yahoo's .info value."""
                val = ttm.get(ttm_key)
                return val if val is not None else info.get(info_key)

            records.append({
                "ticker":              ticker,
                "name":                info.get("longName", ticker),
                "sector":              info.get("sector", "Unknown"),
                "industry":            info.get("industry", "Unknown"),
                "market_cap":          _prefer("market_cap", "marketCap"),
                "avg_volume":          info.get("averageVolume", None),
                # Quality factors
                "trailing_pe":         info.get("trailingPE", None),
                "forward_pe":          info.get("forwardPE", None),
                "ev_to_ebitda":        info.get("enterpriseToEbitda", None),
                "price_to_book":       info.get("priceToBook", None),
                "roe":                 _prefer("roe", "returnOnEquity"),
                "roa":                 _prefer("roa", "returnOnAssets"),
                "debt_to_equity":      _prefer("debt_to_equity", "debtToEquity"),
                "earnings_growth":     info.get("earningsGrowth", None),
                "revenue_growth":      info.get("revenueGrowth", None),
                "gross_margins":       _prefer("gross_margins", "grossMargins"),
                "operating_margins":   _prefer("operating_margins", "operatingMargins"),
                "free_cashflow":       _prefer("free_cashflow", "freeCashflow"),
                # Phase 4: Accruals ratio inputs
                "net_income_common":   _prefer("net_income_common", "netIncomeToCommon"),
                "operating_cashflow":  _prefer("operating_cashflow", "operatingCashflow"),
                # Provenance — lets us see staleness/coverage instead of assuming
                "shares_outstanding":  ttm.get("shares_outstanding") or info.get("sharesOutstanding"),
                "ttm_asof":            ttm.get("ttm_asof"),
                "ttm_complete":        ttm.get("ttm_complete", False),
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
      - total_assets_y0 / total_assets_y1  → asset_growth
      - op_income_y0 / revenue_y0          → op_margin_current
      - op_income_y1 / revenue_y1          → op_margin_prior
      - invested_capital                   → for ROIC computation

    QUARTERLY year-over-year basis since 2026-07 (was annual). y0 = latest
    quarter, y1 = same quarter one year earlier — not the previous quarter,
    so seasonal businesses aren't penalised for normal post-holiday declines.
    Falls back to the old annual comparison when fewer than 5 quarterly
    columns are available; `assets_basis` / `margin_basis` record which was
    actually used, and `stmt_asof` records the period end date.

    Measured impact (2026-07-17) — annual data was materially stale:
        AAPL asset_growth: +12.03% quarterly YoY vs −1.57% on annual
        XOM  asset_growth:  +2.77% quarterly YoY vs −0.99% on annual

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
               "invested_capital": None, "effective_tax_rate": None,
               # provenance: which basis actually produced each factor
               "assets_basis": None, "margin_basis": None, "stmt_asof": None}
        try:
            t_obj = yf.Ticker(ticker)

            # ── QUARTERLY basis (was ANNUAL until 2026-07) ─────────────────
            # These feed asset_growth and op_margin_change. Sourcing them from
            # annual statements meant they only moved once a year: as of
            # 2026-07 AAPL's latest annual column was FY ending 2025-09-27,
            # ~10 months stale, while the `.info` ratios in the same quality
            # score were current to 2026-03-28. Same score, 6-month vintage gap.
            #
            # Comparison is YEAR-OVER-YEAR (latest quarter vs the SAME quarter
            # a year earlier, i.e. index 0 vs index 4) rather than consecutive
            # quarters — retail/seasonal businesses would otherwise show a
            # fake "collapse" every post-holiday quarter.
            #
            # y0/y1 field names kept for backward compatibility with
            # features.py, which computes:
            #     asset_growth     = (y0 - y1) / abs(y1)
            #     op_margin_change = (op_y0/rev_y0) - (op_y1/rev_y1)
            # Those formulas are period-agnostic, so only the inputs change.
            YOY_LAG = 4   # quarters back for the year-ago comparison

            q_bal = t_obj.quarterly_balance_sheet
            if q_bal is not None and not q_bal.empty:
                assets = _stmt_row(q_bal, "Total Assets")
                if assets and len(assets) > YOY_LAG:
                    rec["total_assets_y0"] = assets[0]
                    rec["total_assets_y1"] = assets[YOY_LAG]
                invested = _stmt_row(q_bal, "Invested Capital")
                if invested:
                    rec["invested_capital"] = invested[0]

            q_inc = t_obj.quarterly_income_stmt
            if q_inc is not None and not q_inc.empty:
                # No 'Operating Revenue' fallback — banks lack Operating Income
                # and that substitution yields a nonsense 100% margin.
                op_inc = _stmt_row(q_inc, "Operating Income")
                if op_inc and len(op_inc) > YOY_LAG:
                    rec["op_income_y0"] = op_inc[0]
                    rec["op_income_y1"] = op_inc[YOY_LAG]
                revenue = _stmt_row(q_inc, "Total Revenue")
                if revenue and len(revenue) > YOY_LAG:
                    rec["revenue_y0"] = revenue[0]
                    rec["revenue_y1"] = revenue[YOY_LAG]
                try:
                    rec["stmt_asof"] = str(q_inc.columns[0].date())
                except Exception:
                    pass

            # ANNUAL FALLBACK: yfinance often exposes only ~5 quarterly columns,
            # so the year-ago quarter may be missing. Rather than drop the factor
            # entirely, fall back to the old annual comparison for those tickers.
            if rec["total_assets_y0"] is None:
                bs = t_obj.balance_sheet
                if bs is not None and not bs.empty and "Total Assets" in bs.index and len(bs.columns) >= 2:
                    rec["total_assets_y0"] = float(bs.loc["Total Assets"].iloc[0])
                    rec["total_assets_y1"] = float(bs.loc["Total Assets"].iloc[1])
                    rec["assets_basis"] = "annual"
            else:
                rec["assets_basis"] = "quarterly_yoy"

            if rec["op_income_y0"] is None or rec["revenue_y0"] is None:
                inc = t_obj.income_stmt
                if inc is not None and not inc.empty and len(inc.columns) >= 2:
                    if "Operating Income" in inc.index:
                        rec["op_income_y0"] = float(inc.loc["Operating Income"].iloc[0])
                        rec["op_income_y1"] = float(inc.loc["Operating Income"].iloc[1])
                    if "Total Revenue" in inc.index:
                        rec["revenue_y0"] = float(inc.loc["Total Revenue"].iloc[0])
                        rec["revenue_y1"] = float(inc.loc["Total Revenue"].iloc[1])
                    rec["margin_basis"] = "annual"
            else:
                rec["margin_basis"] = "quarterly_yoy"

            # Tax rate from info (no statement-level equivalent exposed)
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
