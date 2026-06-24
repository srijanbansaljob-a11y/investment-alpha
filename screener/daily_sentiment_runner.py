"""
=============================================================================
  WORLD-CLASS DAILY MARKET SENTIMENT ENGINE  v2.0
  For Day Traders | Fundamental + Sentiment + Technical Regime Focus
  Run each morning before market open (8:00–9:15 AM ET recommended)
=============================================================================

SOURCES:
  1. CNN Fear & Greed Index  → CNN Business
  2. VIX + VIX3M             → Yahoo Finance (term structure)
  3. SPY OHLCV               → Yahoo Finance v8 (ADX, 200MA)
  4. Sector ETF breadth      → Yahoo Finance v8 (% above 200MA)
  5. Alpaca Data API          → Real-time price, volume, MA50/MA200, 52w range (primary)
  6. Yahoo Finance            → Fundamentals only: PE, analyst rec, earnings, beta (fallback for price)
  7. Yahoo Finance News       → Headline sentiment

REGIME SCORE (6 components, 100 pts):
  VIX Level          20 pts
  VIX Term Structure 10 pts  ← NEW
  Fear & Greed       15 pts
  ADX on SPY         20 pts  ← NEW
  SPY vs 200MA       20 pts  ← NEW
  Sector Breadth     15 pts  ← NEW

OUTPUT:
  - daily_sentiment_data.json  → feeds Excel dashboard
  - trade_log.csv              → outcome tracking for weekly learning loop
  - sentiment_history.csv      → regime score history

USAGE:
  python daily_sentiment_runner.py              # Full run
  python daily_sentiment_runner.py --quick      # Skip technicals (faster)
  python daily_sentiment_runner.py --stocks AAPL NVDA MSFT
=============================================================================
"""

import json
import sys
import time
import csv
import os
import argparse
from datetime import datetime, date
from typing import Optional

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas not installed. Run: pip install pandas")
    sys.exit(1)


# ─── CONFIG ───────────────────────────────────────────────────────────────────

OUTPUT_DIR  = os.path.dirname(os.path.abspath(__file__))
JSON_OUTPUT = os.path.join(OUTPUT_DIR, "daily_sentiment_data.json")
LOG_FILE    = os.path.join(OUTPUT_DIR, "sentiment_history.csv")
TRADE_LOG   = os.path.join(OUTPUT_DIR, "trade_log.csv")

# Score weights — updated by weekly_analysis.py as the model learns
SCORE_WEIGHTS = {
    "analyst":        30,
    "momentum":       25,
    "news_sentiment": 20,
    "macro_alignment":15,
    "valuation":      10,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
}

# Alpaca Data API — real-time prices, bars, snapshots
# Reads from environment (set in GitHub Secrets or .env file)
ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "").strip()
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "").strip()
ALPACA_DATA   = "https://data.alpaca.markets"

def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Accept": "application/json",
    }

ALPACA_AVAILABLE = bool(ALPACA_KEY and ALPACA_SECRET)

DEFAULT_TICKERS = [
    # ── Mega-Cap Tech ──────────────────────────────────────────────────────
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
    "AVGO", "ORCL", "AMD", "CRM", "ADBE", "QCOM", "TXN", "IBM",
    "CSCO", "NOW", "AMAT", "MU", "INTC", "ARM",
    # ── Cybersecurity / Cloud ──────────────────────────────────────────────
    "PANW", "CRWD", "NET", "DDOG", "SNOW", "ZS",
    # ── Financials ────────────────────────────────────────────────────────
    "JPM", "GS", "BAC", "MS", "V", "MA", "BLK", "C", "WFC",
    "AXP", "COF", "SCHW", "PYPL",
    # ── Healthcare / Biotech ──────────────────────────────────────────────
    "UNH", "LLY", "PFE", "MRNA", "JNJ", "ABBV", "MRK", "ABT",
    "TMO", "AMGN", "GILD", "ISRG", "REGN", "VRTX", "BMY",
    # ── Consumer Discretionary & Staples ──────────────────────────────────
    "COST", "WMT", "HD", "MCD", "NKE", "SBUX", "TGT", "LOW",
    "PG", "KO", "PEP", "NFLX", "DIS", "CMCSA",
    # ── Energy ────────────────────────────────────────────────────────────
    "XOM", "CVX", "OXY", "COP", "SLB", "MPC",
    # ── Industrials / Defense ─────────────────────────────────────────────
    "CAT", "BA", "RTX", "LMT", "HON", "GE", "DE", "UPS", "FDX",
    # ── High-Momentum Growth ───────────────────────────────────────────────
    "SMCI", "MSTR", "PLTR", "HOOD", "SOFI",
    "COIN", "UBER", "DASH", "ABNB", "SPOT", "RBLX", "RDDT",
    "MELI", "NU", "AFRM", "IONQ",
]
# Note: Sector ETFs (XLK, XLF, etc.) are NOT in this list — they are fetched
# separately via SECTOR_ETFS for the regime/breadth calculation only.

SECTOR_ETFS = {
    "Technology":    "XLK",
    "Financials":    "XLF",
    "Energy":        "XLE",
    "Healthcare":    "XLV",
    "Industrials":   "XLI",
    "ConsumerStap":  "XLP",
    "Utilities":     "XLU",
    "Materials":     "XLB",
    "RealEstate":    "XLRE",
    "ConsumerDisc":  "XLY",
    "Communication": "XLC",
}


# ─── HTTP SESSION ─────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://",  HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session

SESSION = make_session()


# ─── HISTORICAL OHLCV ─────────────────────────────────────────────────────────

def get_historical_ohlcv(symbol: str, days: int = 220) -> list:
    """
    Fetch daily OHLCV via Yahoo Finance v8 API (no key required).
    Returns list of (timestamp, high, low, close) tuples for trading days only.
    """
    end_ts   = int(time.time())
    start_ts = end_ts - (days * 86400)
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={start_ts}&period2={end_ts}&interval=1d")
    try:
        r      = SESSION.get(url, timeout=12)
        data   = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return []
        chart  = result[0]
        quotes = chart.get("indicators", {}).get("quote", [{}])[0]
        ts_list = chart.get("timestamp", [])
        highs   = quotes.get("high",  [])
        lows    = quotes.get("low",   [])
        closes  = quotes.get("close", [])
        return [
            (t, h, l, c)
            for t, h, l, c in zip(ts_list, highs, lows, closes)
            if h and l and c
        ]
    except Exception:
        return []


def get_historical_closes(symbol: str, days: int = 220) -> list:
    """Returns just closing prices."""
    return [c for _, _, _, c in get_historical_ohlcv(symbol, days)]


# ─── ADX CALCULATION (pure Python, no numpy required) ────────────────────────

def _wilder_smooth(values: list, period: int) -> list:
    """Wilder's smoothing: seed with SMA then exponential rollover."""
    out = [None] * len(values)
    if len(values) < period:
        return out
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def compute_adx(ohlc_rows: list, period: int = 14) -> dict:
    """
    Compute ADX-14, +DI, -DI and trend direction from OHLCV rows.
    Requires at least period*3 bars of data.
    """
    if len(ohlc_rows) < period * 3:
        return {"adx": None, "plus_di": None, "minus_di": None, "trend": "unknown"}

    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, len(ohlc_rows)):
        _, h, l, c         = ohlc_rows[i]
        _, ph, pl, pc      = ohlc_rows[i - 1]
        tr       = max(h - l, abs(h - pc), abs(l - pc))
        up_move  = h - ph
        dn_move  = pl - l
        plus_dms.append(up_move  if (up_move > dn_move  and up_move > 0) else 0.0)
        minus_dms.append(dn_move if (dn_move > up_move  and dn_move > 0) else 0.0)
        trs.append(tr)

    s_tr    = _wilder_smooth(trs,      period)
    s_plus  = _wilder_smooth(plus_dms, period)
    s_minus = _wilder_smooth(minus_dms, period)

    dxs = []
    for atr, pdm, mdm in zip(s_tr, s_plus, s_minus):
        if None in (atr, pdm, mdm) or atr == 0:
            dxs.append(None)
            continue
        pdi  = 100 * pdm / atr
        mdi  = 100 * mdm / atr
        denom = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / denom if denom > 0 else 0)

    valid_dxs  = [d for d in dxs if d is not None]
    adx_series = _wilder_smooth(valid_dxs, period)
    adx        = next((v for v in reversed(adx_series) if v is not None), None)

    # Final +DI / -DI
    last_pdi = last_mdi = None
    for atr, pdm, mdm in zip(reversed(s_tr), reversed(s_plus), reversed(s_minus)):
        if None in (atr, pdm, mdm) or atr == 0:
            continue
        last_pdi = round(100 * pdm / atr, 1)
        last_mdi = round(100 * mdm / atr, 1)
        break

    closes = [c for _, _, _, c in ohlc_rows if c]
    trend  = "flat"
    if len(closes) >= 50:
        ma50  = sum(closes[-50:]) / 50
        trend = "up" if closes[-1] > ma50 else "down"

    return {
        "adx":      round(adx, 1) if adx else None,
        "plus_di":  last_pdi,
        "minus_di": last_mdi,
        "trend":    trend,
    }


# ─── SPY TECHNICAL INDICATORS ─────────────────────────────────────────────────

def _get_alpaca_ohlcv(symbol: str, days: int = 260) -> list:
    """
    Fetch daily OHLCV bars from Alpaca for a single symbol.
    Returns list of (timestamp, high, low, close) tuples — same format
    as get_historical_ohlcv() so compute_adx() can consume it directly.
    """
    if not ALPACA_AVAILABLE:
        return []
    from datetime import timedelta
    start_date = (date.today() - timedelta(days=days + 30)).isoformat()
    try:
        r = SESSION.get(
            f"{ALPACA_DATA}/v2/stocks/bars",
            headers=_alpaca_headers(),
            params={
                "symbols":    symbol,
                "timeframe":  "1Day",
                "start":      start_date,
                "limit":      days + 30,
                "feed":       "iex",
                "adjustment": "split",
            },
            timeout=15,
        )
        if not r.ok:
            return []
        bars = r.json().get("bars", {}).get(symbol, [])
        return [(b["t"], b["h"], b["l"], b["c"]) for b in bars if b.get("c")]
    except Exception as e:
        print(f"  ⚠  Alpaca OHLCV for {symbol} failed: {e}")
        return []


def get_spy_technical_indicators() -> dict:
    """ADX-14 + SPY position relative to 200-day SMA. Uses Alpaca, falls back to Yahoo."""
    print("  → Computing SPY technicals (ADX-14, 200MA)...")
    rows = _get_alpaca_ohlcv("SPY", days=260)
    if len(rows) < 200:
        print("  ⚠  Alpaca SPY history insufficient — trying Yahoo...")
        rows = get_historical_ohlcv("SPY", days=260)
    if len(rows) < 200:
        print("  ⚠  Insufficient SPY history — skipping")
        return {}

    adx_result     = compute_adx(rows, period=14)
    closes         = [c for _, _, _, c in rows if c]
    ma200          = sum(closes[-200:]) / 200
    current        = closes[-1]
    pct_from_200ma = (current - ma200) / ma200 * 100

    return {
        "adx":            adx_result.get("adx"),
        "plus_di":        adx_result.get("plus_di"),
        "minus_di":       adx_result.get("minus_di"),
        "spy_trend":      adx_result.get("trend"),
        "spy_price":      round(current, 2),
        "ma_200":         round(ma200, 2),
        "pct_from_200ma": round(pct_from_200ma, 2),
    }


# ─── VIX TERM STRUCTURE ───────────────────────────────────────────────────────

def get_vix_term_structure(vix_spot: float) -> dict:
    """
    Fetch VIX3M and compute VIX/VIX3M ratio.
    ratio > 1.0 = backwardation (near-term fear > medium-term) → bearish signal
    ratio < 0.90 = steep contango → calm market
    """
    print("  → Fetching VIX term structure (VIX3M)...")
    try:
        url    = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=%5EVIX3M"
        r      = SESSION.get(url, timeout=8)
        quotes = r.json().get("quoteResponse", {}).get("result", [])
        if not quotes:
            return {}
        vix3m = quotes[0].get("regularMarketPrice")
        if not vix3m or vix3m <= 0:
            return {}
        ratio = vix_spot / vix3m
        return {
            "vix":       round(vix_spot, 2),
            "vix3m":     round(vix3m, 2),
            "ratio":     round(ratio, 3),
            "structure": (
                "steep_contango" if ratio < 0.90 else
                "contango"       if ratio < 1.00 else
                "backwardation"
            ),
        }
    except Exception as e:
        print(f"  ⚠  VIX3M unavailable: {e}")
        return {}


# ─── SECTOR BREADTH ───────────────────────────────────────────────────────────

def get_sector_breadth() -> dict:
    """
    % of 11 sector ETFs trading above their 200-day MA.
    Proxy for market-wide participation.
    >70% = broad bull  |  50–70% = selective  |  <50% = narrowing / weak
    Uses Alpaca bars (single batch call) with Yahoo Finance as fallback.
    """
    print("  → Computing sector breadth (11 ETFs vs 200MA)...")
    etf_symbols = list(SECTOR_ETFS.values())
    above_200   = 0
    detail      = {}

    # ── Try Alpaca first (single batch call, no rate-limit risk) ──────────
    if ALPACA_AVAILABLE:
        from datetime import timedelta
        start_date = (date.today() - timedelta(days=260)).isoformat()
        try:
            r = SESSION.get(
                f"{ALPACA_DATA}/v2/stocks/bars",
                headers=_alpaca_headers(),
                params={
                    "symbols":    ",".join(etf_symbols),
                    "timeframe":  "1Day",
                    "start":      start_date,
                    "limit":      260,
                    "feed":       "iex",
                    "adjustment": "split",
                },
                timeout=20,
            )
            if r.ok:
                bars_data = r.json().get("bars", {})
                for etf in etf_symbols:
                    bars = bars_data.get(etf, [])
                    closes = [b["c"] for b in bars if b.get("c")]
                    if len(closes) < 200:
                        continue
                    ma200    = sum(closes[-200:]) / 200
                    current  = closes[-1]
                    is_above = current > ma200
                    if is_above:
                        above_200 += 1
                    detail[etf] = {
                        "above_200ma":  is_above,
                        "pct_from_200": round((current - ma200) / ma200 * 100, 1),
                    }
        except Exception as e:
            print(f"  ⚠  Alpaca breadth fetch failed: {e}")

    # ── Fall back to Yahoo per-ETF if Alpaca returned nothing ─────────────
    if not detail:
        print("  → Falling back to Yahoo Finance for breadth...")
        for etf in etf_symbols:
            closes = get_historical_closes(etf, days=210)
            if len(closes) < 200:
                continue
            ma200    = sum(closes[-200:]) / 200
            current  = closes[-1]
            is_above = current > ma200
            if is_above:
                above_200 += 1
            detail[etf] = {
                "above_200ma":  is_above,
                "pct_from_200": round((current - ma200) / ma200 * 100, 1),
            }
            time.sleep(0.12)

    checked = len(detail)
    if checked == 0:
        return {}

    pct = above_200 / checked
    return {
        "pct_above_200ma": round(pct, 3),
        "above_count":     above_200,
        "total_checked":   checked,
        "label": (
            "Strong (>70%)"   if pct > 0.70 else
            "Moderate (>50%)" if pct > 0.50 else
            "Weak (<50%)"
        ),
        "etf_detail": detail,
    }


# ─── FEAR & GREED ─────────────────────────────────────────────────────────────

def get_fear_greed() -> dict:
    result = {"score": None, "label": "Unknown", "prev_close": None,
              "prev_week": None, "prev_month": None, "source": "feargreedmeter.com"}
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        r   = SESSION.get(url, timeout=8)
        if r.status_code == 200:
            fg = r.json().get("fear_and_greed", {})
            result.update({
                "score":      round(fg.get("score", 0), 1),
                "label":      fg.get("rating", "Unknown").replace("_", " ").title(),
                "prev_close": round(fg.get("previous_close", 0), 1),
                "prev_week":  round(fg.get("previous_1_week", 0), 1),
                "prev_month": round(fg.get("previous_1_month", 0), 1),
                "source":     "CNN Business (live)",
            })
    except Exception as e:
        result["error"] = str(e)

    if result["score"] is None:
        try:
            import re
            r = SESSION.get("https://feargreedmeter.com/", timeout=8)
            m = re.search(r'"score"\s*:\s*(\d+\.?\d*)', r.text)
            if m:
                result["score"]  = float(m.group(1))
                result["source"] = "feargreedmeter.com (scraped)"
        except:
            pass
    return result


# ─── MARKET OVERVIEW ──────────────────────────────────────────────────────────

def get_yahoo_quote(symbol: str) -> dict:
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
    try:
        r      = SESSION.get(url, timeout=8)
        quotes = r.json().get("quoteResponse", {}).get("result", [])
        if quotes:
            q = quotes[0]
            return {
                "symbol":         q.get("symbol"),
                "price":          q.get("regularMarketPrice"),
                "change_pct":     round(q.get("regularMarketChangePercent", 0), 2),
                "volume":         q.get("regularMarketVolume"),
                "avg_volume":     q.get("averageDailyVolume3Month"),
                "52w_high":       q.get("fiftyTwoWeekHigh"),
                "52w_low":        q.get("fiftyTwoWeekLow"),
                "market_cap":     q.get("marketCap"),
                "pe_ratio":       q.get("trailingPE"),
                "forward_pe":     q.get("forwardPE"),
                "eps_ttm":        q.get("epsTrailingTwelveMonths"),
                "eps_fwd":        q.get("epsForward"),
                "short_name":     q.get("shortName"),
                "earnings_date":  q.get("earningsTimestampStart"),
                "analyst_target": q.get("targetMeanPrice"),
                "analyst_low":    q.get("targetLowPrice"),
                "analyst_high":   q.get("targetHighPrice"),
                "recommend":      q.get("recommendationKey"),
                "num_analysts":   q.get("numberOfAnalystOpinions"),
                "beta":           q.get("beta"),
            }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}
    return {"symbol": symbol, "error": "no data"}


def get_market_overview() -> dict:
    print("  → Fetching market indices & VIX...")
    symbols = {
        "SP500":  "^GSPC",
        "NASDAQ": "^IXIC",
        "DOW":    "^DJI",
        "VIX":    "^VIX",
        "TNX":    "^TNX",
        "DXY":    "DX-Y.NYB",
        "GOLD":   "GC=F",
        "CRUDE":  "CL=F",
    }
    results = {}
    for name, sym in symbols.items():
        q = get_yahoo_quote(sym)
        results[name] = {
            "symbol":     sym,
            "price":      q.get("price"),
            "change_pct": q.get("change_pct"),
        }
        time.sleep(0.15)
    return results


# ─── SECTOR ROTATION ──────────────────────────────────────────────────────────

def get_sector_rotation() -> list:
    print("  → Analyzing sector rotation...")
    sym_str = ",".join(SECTOR_ETFS.values())
    url     = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={sym_str}"
    sectors = []
    try:
        r       = SESSION.get(url, timeout=10)
        quotes  = r.json().get("quoteResponse", {}).get("result", [])
        etf_map = {v: k for k, v in SECTOR_ETFS.items()}
        for q in quotes:
            sym = q.get("symbol")
            sectors.append({
                "sector":     etf_map.get(sym, sym),
                "etf":        sym,
                "change_pct": round(q.get("regularMarketChangePercent", 0), 2),
                "price":      q.get("regularMarketPrice"),
            })
        sectors.sort(key=lambda x: x["change_pct"], reverse=True)
    except Exception as e:
        sectors = [{"error": str(e)}]
    return sectors



# ─── ALPACA DATA API ──────────────────────────────────────────────────────────

def get_alpaca_snapshots(tickers: list) -> dict:
    """
    Fetch real-time snapshots from Alpaca for a batch of tickers.
    Returns dict keyed by ticker with price, change_pct, volume, vol_ratio.
    Falls back gracefully if Alpaca unavailable or ticker not found.

    Snapshot fields used:
      latestTrade.p       → current price (real-time)
      dailyBar            → today's OHLCV
      prevDailyBar        → previous close (for change_pct calculation)
    """
    if not ALPACA_AVAILABLE:
        return {}

    results = {}
    batch_size = 100  # Alpaca allows up to 1000, 100 is safe
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        sym_str = ",".join(batch)
        url = f"{ALPACA_DATA}/v2/stocks/snapshots"
        try:
            r = SESSION.get(
                url,
                headers=_alpaca_headers(),
                params={"symbols": sym_str, "feed": "iex"},
                timeout=12,
            )
            if r.status_code == 403:
                print("  ⚠  Alpaca data: subscription required for SIP feed, using IEX")
            if not r.ok:
                continue
            data = r.json()
            for sym, snap in data.items():
                daily  = snap.get("dailyBar") or {}
                prev   = snap.get("prevDailyBar") or {}
                latest = snap.get("latestTrade") or {}

                price    = latest.get("p") or daily.get("c") or 0
                prev_c   = prev.get("c") or 0
                today_v  = daily.get("v") or 0

                # 20-day avg volume: Alpaca doesn't return it in snapshot,
                # so we estimate from recent bars; approximated here as 3-month avg
                # (filled properly by get_alpaca_bars if called after)
                change_pct = ((price - prev_c) / prev_c * 100) if prev_c > 0 else 0

                results[sym] = {
                    "_source": "alpaca",
                    "price":      round(price, 4),
                    "change_pct": round(change_pct, 2),
                    "volume":     today_v,
                    "avg_volume": None,   # filled by get_alpaca_bars or Yahoo fallback
                    "vol_ratio":  None,   # filled after avg_volume known
                    "open":       daily.get("o"),
                    "high":       daily.get("h"),
                    "low":        daily.get("l"),
                    "prev_close": prev_c,
                }
        except Exception as e:
            print(f"  ⚠  Alpaca snapshot batch {i//batch_size+1} failed: {e}")
        time.sleep(0.1)
    return results


def get_alpaca_bars(tickers: list, days: int = 220) -> dict:
    """
    Fetch daily bars from Alpaca for a list of tickers.
    Returns dict keyed by ticker with computed:
      ma50, ma200, 52w_high, 52w_low, week52_change, avg_volume (20d)

    Called after get_alpaca_snapshots() to fill in the technical fields.
    Only runs for tickers that passed the initial snapshot filter.
    """
    if not ALPACA_AVAILABLE:
        return {}

    from datetime import timedelta
    start_date = (date.today() - timedelta(days=days + 30)).isoformat()  # buffer for weekends
    results = {}
    batch_size = 50  # smaller batches for historical data
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        try:
            r = SESSION.get(
                f"{ALPACA_DATA}/v2/stocks/bars",
                headers=_alpaca_headers(),
                params={
                    "symbols":   ",".join(batch),
                    "timeframe": "1Day",
                    "start":     start_date,
                    "limit":     days + 30,
                    "feed":      "iex",
                    "adjustment":"split",
                },
                timeout=20,
            )
            if not r.ok:
                continue
            data = r.json().get("bars", {})
            for sym, bars in data.items():
                if not bars or len(bars) < 20:
                    continue
                closes  = [b["c"] for b in bars]
                volumes = [b["v"] for b in bars]

                # Moving averages
                ma50  = sum(closes[-50:])  / min(50,  len(closes[-50:]))  if len(closes) >= 50  else None
                ma200 = sum(closes[-200:]) / min(200, len(closes[-200:])) if len(closes) >= 200 else None

                # 52-week range (use up to 252 bars)
                year_closes = closes[-252:] if len(closes) >= 252 else closes
                hi52 = max(b["h"] for b in bars[-252:]) if len(bars) >= 252 else max(b["h"] for b in bars)
                lo52 = min(b["l"] for b in bars[-252:]) if len(bars) >= 252 else min(b["l"] for b in bars)

                # 52-week price return
                week52_chg = None
                if len(closes) >= 252:
                    c252 = closes[-252]
                    c_now = closes[-1]
                    week52_chg = round((c_now - c252) / c252, 4) if c252 > 0 else None

                # 20-day avg volume
                avg_vol_20 = int(sum(volumes[-20:]) / min(20, len(volumes[-20:]))) if volumes else None

                results[sym] = {
                    "ma50":          round(ma50, 4)  if ma50  else None,
                    "ma200":         round(ma200, 4) if ma200 else None,
                    "52w_high":      round(hi52, 4),
                    "52w_low":       round(lo52, 4),
                    "week52_change": week52_chg,
                    "avg_volume":    avg_vol_20,
                }
        except Exception as e:
            print(f"  ⚠  Alpaca bars batch {i//batch_size+1} failed: {e}")
        time.sleep(0.15)
    return results

# ─── STOCK QUOTES ─────────────────────────────────────────────────────────────

def _get_yahoo_fundamentals(tickers: list) -> dict:
    """
    Yahoo Finance quote fetch — used ONLY for fundamental data that Alpaca
    does not provide: PE ratio, analyst recommendations, earnings date, beta,
    market cap, analyst price target.

    Also serves as the complete price fallback when Alpaca is unavailable.
    """
    results    = {}
    batch_size = 20
    for i in range(0, len(tickers), batch_size):
        batch   = tickers[i:i + batch_size]
        sym_str = ",".join(batch)
        url     = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={sym_str}"
        try:
            r      = SESSION.get(url, timeout=10)
            quotes = r.json().get("quoteResponse", {}).get("result", [])
            for q in quotes:
                sym = q.get("symbol")
                vol     = q.get("regularMarketVolume") or 0
                avg_vol = q.get("averageDailyVolume3Month") or 1
                results[sym] = {
                    # Fundamentals (Alpaca doesn't carry these)
                    "market_cap":     q.get("marketCap"),
                    "pe_ratio":       q.get("trailingPE"),
                    "forward_pe":     q.get("forwardPE"),
                    "eps_fwd":        q.get("epsForward"),
                    "analyst_target": q.get("targetMeanPrice"),
                    "recommend":      q.get("recommendationKey"),
                    "num_analysts":   q.get("numberOfAnalystOpinions"),
                    "short_name":     q.get("shortName", sym),
                    "earnings_date":  q.get("earningsTimestampStart"),
                    "beta":           q.get("beta"),
                    # Price fallback fields (used only when Alpaca unavailable)
                    "_yf_price":      q.get("regularMarketPrice"),
                    "_yf_change_pct": round(q.get("regularMarketChangePercent", 0), 2),
                    "_yf_volume":     vol,
                    "_yf_avg_vol":    avg_vol,
                    "_yf_52h":        q.get("fiftyTwoWeekHigh"),
                    "_yf_52l":        q.get("fiftyTwoWeekLow"),
                    "_yf_ma50":       q.get("fiftyDayAverage"),
                    "_yf_ma200":      q.get("twoHundredDayAverage"),
                    "_yf_wk52chg":    q.get("52WeekChange"),
                }
        except Exception as e:
            for t in batch:
                results[t] = {"error": str(e)}
        time.sleep(0.2)
    return results


def get_stock_quotes(tickers: list) -> dict:
    """
    Primary quote fetcher — Alpaca for real-time price/technicals,
    Yahoo Finance for fundamentals only.

    Data flow:
      1. Alpaca snapshots  → price, change_pct, volume (real-time)
      2. Yahoo fundamentals → PE, analyst rec, earnings, beta, market cap
      3. Alpaca bars       → MA50, MA200, 52w high/low, avg_volume, week52_change
      4. Merge all three → unified quote dict

    Falls back to Yahoo for price data if Alpaca credentials not set.
    """
    print(f"  → Fetching quotes for {len(tickers)} tickers "
          f"[{'Alpaca real-time' if ALPACA_AVAILABLE else 'Yahoo Finance (no Alpaca key)'}]...")

    # Step 1: Fundamentals from Yahoo (always, Alpaca doesn't have these)
    fundamentals = _get_yahoo_fundamentals(tickers)

    # Step 2: Real-time prices from Alpaca (or Yahoo fallback)
    alpaca_snaps = {}
    alpaca_bars  = {}
    if ALPACA_AVAILABLE:
        alpaca_snaps = get_alpaca_snapshots(tickers)
        # Only fetch bars for tickers we got snapshots for (avoid wasted calls)
        snap_tickers = [t for t in tickers if t in alpaca_snaps]
        if snap_tickers:
            print(f"  → Fetching {len(snap_tickers)}-ticker bar history from Alpaca (MA50/MA200)...")
            alpaca_bars = get_alpaca_bars(snap_tickers)
    else:
        print("  ⚠  ALPACA_API_KEY not set — using Yahoo Finance for price data")

    # Step 3: Merge into unified quote dict
    results = {}
    for sym in tickers:
        fund  = fundamentals.get(sym, {})
        snap  = alpaca_snaps.get(sym, {})
        bars  = alpaca_bars.get(sym, {})

        if fund.get("error") and not snap:
            results[sym] = {"error": fund.get("error", "no data")}
            continue

        if ALPACA_AVAILABLE and snap:
            # Alpaca price data + computed technicals from bars
            avg_vol  = bars.get("avg_volume") or fund.get("_yf_avg_vol") or 1
            volume   = snap.get("volume") or 0
            vol_ratio = round(volume / avg_vol, 2) if avg_vol else 1.0

            results[sym] = {
                "_source":        "alpaca",
                "price":          snap.get("price"),
                "change_pct":     snap.get("change_pct"),
                "volume":         volume,
                "avg_volume":     avg_vol,
                "vol_ratio":      vol_ratio,
                "52w_high":       bars.get("52w_high") or fund.get("_yf_52h"),
                "52w_low":        bars.get("52w_low")  or fund.get("_yf_52l"),
                "ma50":           bars.get("ma50")     or fund.get("_yf_ma50"),
                "ma200":          bars.get("ma200")    or fund.get("_yf_ma200"),
                "week52_change":  bars.get("week52_change") or fund.get("_yf_wk52chg"),
                # Fundamentals (from Yahoo)
                "market_cap":     fund.get("market_cap"),
                "pe_ratio":       fund.get("pe_ratio"),
                "forward_pe":     fund.get("forward_pe"),
                "eps_fwd":        fund.get("eps_fwd"),
                "analyst_target": fund.get("analyst_target"),
                "recommend":      fund.get("recommend"),
                "num_analysts":   fund.get("num_analysts"),
                "short_name":     fund.get("short_name", sym),
                "earnings_date":  fund.get("earnings_date"),
                "beta":           fund.get("beta"),
            }
        else:
            # Yahoo fallback for everything
            avg_vol  = fund.get("_yf_avg_vol") or 1
            volume   = fund.get("_yf_volume") or 0
            results[sym] = {
                "_source":        "yahoo",
                "price":          fund.get("_yf_price"),
                "change_pct":     fund.get("_yf_change_pct"),
                "volume":         volume,
                "avg_volume":     avg_vol,
                "vol_ratio":      round(volume / avg_vol, 2) if avg_vol else 1.0,
                "52w_high":       fund.get("_yf_52h"),
                "52w_low":        fund.get("_yf_52l"),
                "ma50":           fund.get("_yf_ma50"),
                "ma200":          fund.get("_yf_ma200"),
                "week52_change":  fund.get("_yf_wk52chg"),
                "market_cap":     fund.get("market_cap"),
                "pe_ratio":       fund.get("pe_ratio"),
                "forward_pe":     fund.get("forward_pe"),
                "eps_fwd":        fund.get("eps_fwd"),
                "analyst_target": fund.get("analyst_target"),
                "recommend":      fund.get("recommend"),
                "num_analysts":   fund.get("num_analysts"),
                "short_name":     fund.get("short_name", sym),
                "earnings_date":  fund.get("earnings_date"),
                "beta":           fund.get("beta"),
            }

    return results


# ─── NEWS SENTIMENT ───────────────────────────────────────────────────────────

def get_yahoo_news(ticker: str) -> list:
    url = (f"https://query1.finance.yahoo.com/v1/finance/search"
           f"?q={ticker}&newsCount=5&enableFuzzyQuery=false")
    headlines = []
    try:
        r    = SESSION.get(url, timeout=6)
        data = r.json()
        for item in data.get("news", []):
            headlines.append({
                "title":     item.get("title", ""),
                "publisher": item.get("publisher", ""),
                "time":      item.get("providerPublishTime", 0),
                "url":       item.get("link", ""),
            })
    except:
        pass
    return headlines[:5]


def score_headline_sentiment(headlines: list) -> float:
    BULLISH = [
        "beat", "beats", "surge", "upgrade", "upgrades", "raised", "record",
        "bullish", "buy", "outperform", "strong", "growth", "profit",
        "guidance raised", "breakout", "expansion", "partnership", "deal",
        "contract", "approval", "fda", "positive", "momentum", "rally",
        "recovery", "rebound", "higher", "overweight",
    ]
    BEARISH = [
        "miss", "misses", "drop", "downgrade", "downgrades", "cut", "loss",
        "disappoints", "warning", "risk", "concern", "investigation", "lawsuit",
        "fraud", "recall", "layoff", "guidance cut", "sell", "underperform",
        "lower", "decline", "falling", "bankruptcy", "default", "negative", "weak",
    ]
    score = 0.0
    for h in headlines:
        title = h.get("title", "").lower()
        for w in BULLISH:
            if w in title: score += 1.5
        for w in BEARISH:
            if w in title: score -= 1.5
    return max(-10, min(10, round(score, 1)))


# ─── MACRO REGIME SCORE (6 components, 100 pts) ───────────────────────────────

def compute_macro_score(market: dict, fear_greed: dict,
                        spy_tech: dict = None,
                        vix_term: dict = None,
                        breadth:  dict = None) -> dict:
    """
    6-component macro regime score.
    Also outputs permitted_strategies — the execution gate.

    Score  >= 75 → STRONG BULL   → momentum + breakout + mean_reversion + catalyst
    Score  55–74 → MODERATE BULL → momentum + mean_reversion + catalyst
    Score  40–54 → NEUTRAL       → mean_reversion + defensive
    Score  < 40  → BEARISH       → defensive only
    """
    scores  = {}
    details = {}
    vix     = market.get("VIX", {}).get("price") or 20

    # ── 1. VIX Level (20 pts) ──────────────────────────────────────────
    if   vix < 15:  scores["vix"] = 20
    elif vix < 18:  scores["vix"] = 18
    elif vix < 22:  scores["vix"] = 14
    elif vix < 28:  scores["vix"] = 8
    elif vix < 35:  scores["vix"] = 4
    else:           scores["vix"] = 1
    details["vix"] = round(vix, 2)

    # ── 2. VIX Term Structure (10 pts) ─────────────────────────────────
    if vix_term and vix_term.get("ratio"):
        r = vix_term["ratio"]
        scores["vix_term"] = (
            10 if r < 0.90 else    # steep contango → calm
            8  if r < 0.95 else
            6  if r < 1.00 else    # flat / mild contango
            3  if r < 1.05 else    # mild backwardation
            0                      # backwardation → institutional fear
        )
        details.update({
            "vix3m":      vix_term.get("vix3m"),
            "vix_ratio":  vix_term.get("ratio"),
            "vix_struct": vix_term.get("structure"),
        })
    else:
        scores["vix_term"] = 5   # neutral when unavailable

    # ── 3. Fear & Greed (15 pts) ───────────────────────────────────────
    fg = fear_greed.get("score") or 50
    scores["fear_greed"] = (
        15 if 40 <= fg <= 65 else  # ideal momentum zone
        11 if 30 <= fg < 40  else
        9  if 65 < fg <= 75  else
        6  if 20 <= fg < 30  else
        4  if fg > 75        else  # extreme greed → distribution
        2                          # extreme fear → panic
    )
    details["fg"] = fg

    # ── 4. ADX on SPY (20 pts) — trend strength ────────────────────────
    if spy_tech and spy_tech.get("adx") is not None:
        adx   = spy_tech["adx"]
        trend = spy_tech.get("spy_trend", "flat")
        if   adx > 30 and trend == "up":    scores["adx"] = 20
        elif adx > 25 and trend == "up":    scores["adx"] = 16
        elif adx > 20 and trend == "up":    scores["adx"] = 12
        elif adx < 20:                      scores["adx"] = 8    # ranging
        elif adx > 25 and trend == "down":  scores["adx"] = 4    # strong downtrend
        else:                               scores["adx"] = 6
        details.update({
            "adx":       adx,
            "spy_trend": trend,
            "plus_di":   spy_tech.get("plus_di"),
            "minus_di":  spy_tech.get("minus_di"),
        })
    else:
        scores["adx"] = 10

    # ── 5. SPY vs 200MA (20 pts) — bull/bear regime ────────────────────
    if spy_tech and spy_tech.get("pct_from_200ma") is not None:
        pct = spy_tech["pct_from_200ma"]
        scores["spy_200ma"] = (
            20 if pct > 5   else   # well above 200MA → bull
            16 if pct > 1   else
            8  if pct > -2  else   # just below → caution
            3                      # well below → bear
        )
        details.update({
            "spy_pct_from_200ma": pct,
            "spy_ma200":          spy_tech.get("ma_200"),
        })
    else:
        scores["spy_200ma"] = 10

    # ── 6. Sector Breadth (15 pts) — participation width ───────────────
    if breadth and breadth.get("pct_above_200ma") is not None:
        pct_b = breadth["pct_above_200ma"]
        scores["breadth"] = (
            15 if pct_b > 0.70 else   # broad participation
            11 if pct_b > 0.55 else
            7  if pct_b > 0.45 else
            3                          # narrow / internally weak
        )
        details.update({
            "breadth_pct":   round(pct_b * 100, 1),
            "breadth_label": breadth.get("label"),
        })
    else:
        scores["breadth"] = 7

    total = sum(scores.values())

    if   total >= 75: label = "🟢 STRONG BULL";   permitted = ["momentum", "breakout", "mean_reversion", "catalyst"]
    elif total >= 55: label = "🟡 MODERATE BULL"; permitted = ["momentum", "mean_reversion", "catalyst"]
    elif total >= 40: label = "🟠 NEUTRAL";        permitted = ["mean_reversion", "defensive"]
    else:             label = "🔴 BEARISH";        permitted = ["defensive"]

    return {
        "total":                total,
        "label":                label,
        "permitted_strategies": permitted,
        "components":           scores,
        "details":              details,
        "vix_value":            vix,
        "fg_value":             fg,
        "tnx_value":            market.get("TNX", {}).get("price"),
    }


# ─── STOCK CLASSIFICATION ─────────────────────────────────────────────────────

def classify_stock(ticker: str, quote: dict) -> dict:
    """
    Classify each stock into a strategy bucket.

    Buckets:
      momentum      → High-beta, high-PE, strong momentum, strong rec
      breakout      → Volume surge on price expansion from range
      mean_reversion → Large-cap, liquid, covered, stable price
      catalyst      → Near earnings + upgrade + rising volume
      defensive     → Low-beta, large-cap, stable, low PE
      avoid         → Poor rec, thin coverage, or extreme valuation
      watch         → Everything else
    """
    price        = quote.get("price") or 0
    change_pct   = quote.get("change_pct") or 0
    vol_ratio    = quote.get("vol_ratio") or 1.0
    forward_pe   = quote.get("forward_pe") or 0
    num_analysts = quote.get("num_analysts") or 0
    market_cap   = quote.get("market_cap") or 0
    rec          = (quote.get("recommend") or "hold").lower()
    beta         = quote.get("beta") or 1.0
    high52       = quote.get("52w_high") or price
    low52        = quote.get("52w_low")  or price

    near_earnings    = False
    days_to_earnings = None
    earn_ts = quote.get("earnings_date")
    if earn_ts:
        try:
            earn_dt          = datetime.fromtimestamp(earn_ts)
            days_to_earnings = (earn_dt - datetime.now()).days
            near_earnings    = 0 <= days_to_earnings <= 14
        except:
            pass

    large_cap    = market_cap > 10e9
    well_covered = num_analysts >= 15
    strong_rec   = rec in ["strong_buy", "buy", "outperform", "overweight"]
    weak_rec     = rec in ["sell", "strong_sell", "underperform"]

    info = {"near_earnings": near_earnings, "days_to_earnings": days_to_earnings,
            "vol_ratio": round(vol_ratio, 2)}

    # MA200 filter — stocks in a structural downtrend get downgraded
    ma200 = quote.get("ma200")
    ma50  = quote.get("ma50")
    below_200ma = (ma200 and price > 0 and price < ma200 * 0.97)  # >3% below 200MA
    below_50ma  = (ma50  and price > 0 and price < ma50)

    # Avoid
    if weak_rec or num_analysts < 5 or (forward_pe and forward_pe > 150):
        return {**info, "bucket": "avoid"}
    if below_200ma and not near_earnings:
        # Stock in structural downtrend → watch (not avoid, may recover)
        return {**info, "bucket": "watch", "below_200ma": True}

    # Catalyst: near earnings with rising volume and strong rec
    if near_earnings and strong_rec and vol_ratio > 1.2:
        return {**info, "bucket": "catalyst"}

    # Breakout: volume expansion + price expansion near 52w high
    if vol_ratio > 1.8 and abs(change_pct) > 1.5 and price > 0 and high52 > low52:
        pos_in_range = (price - low52) / (high52 - low52)
        if pos_in_range > 0.70:
            return {**info, "bucket": "breakout"}

    # Momentum: high-beta, high-PE growth stock with positive signal
    if (beta or 1.0) > 1.2 and forward_pe > 28 and strong_rec and change_pct > 0.3:
        return {**info, "bucket": "momentum"}

    # Defensive: low-beta, large-cap, covered, stable
    if (beta or 1.0) < 0.85 and large_cap and well_covered and abs(change_pct) < 0.8:
        return {**info, "bucket": "defensive"}

    # Mean reversion: large-cap, liquid, well-covered
    if large_cap and well_covered:
        return {**info, "bucket": "mean_reversion"}

    return {**info, "bucket": "watch"}


# ─── STOCK CONVICTION SCORER ──────────────────────────────────────────────────

def score_stock(ticker: str, quote: dict, news: list, macro_score: dict) -> dict:
    """
    Score each stock 0–100 using SCORE_WEIGHTS.
    Weights update over time via weekly_analysis.py.
    """
    W        = SCORE_WEIGHTS
    breakdown = {}
    signals   = []

    price  = quote.get("price") or 0
    target = quote.get("analyst_target") or price
    rec    = (quote.get("recommend") or "none").lower()
    n_anal = quote.get("num_analysts") or 0

    # ── Analyst Consensus (default 30 pts) ────────────────────────────
    rec_map = {
        "strong_buy": 1.00, "buy": 0.80, "outperform": 0.73, "overweight": 0.73,
        "hold": 0.47, "neutral": 0.47, "underperform": 0.20,
        "sell": 0.10, "strong_sell": 0.00,
    }
    base   = rec_map.get(rec, 0.40)
    upside = ((target - price) / price * 100) if price > 0 else 0

    analyst_raw = base
    if n_anal >= 20:   analyst_raw = min(1.0, analyst_raw + 0.10)
    elif n_anal >= 10: analyst_raw = min(1.0, analyst_raw + 0.03)
    if upside > 25:    analyst_raw = min(1.0, analyst_raw + 0.10)
    elif upside > 15:  analyst_raw = min(1.0, analyst_raw + 0.07)
    elif upside > 5:   analyst_raw = min(1.0, analyst_raw + 0.03)
    elif upside < -5:  analyst_raw = max(0, analyst_raw - 0.13)

    breakdown["analyst"] = round(analyst_raw * W["analyst"])
    if upside > 15:
        signals.append(f"+{upside:.0f}% analyst upside ({n_anal} analysts)")

    # Momentum (default 25 pts) — multi-signal: MA trend + today's move + volume + 52w position
    chg_pct   = quote.get("change_pct") or 0
    vol_ratio = quote.get("vol_ratio") or 1.0
    ma50      = quote.get("ma50")
    ma200     = quote.get("ma200")
    week52_chg = quote.get("week52_change")   # ~12M return, from Yahoo
    high52    = quote.get("52w_high") or price
    low52     = quote.get("52w_low")  or price

    mom_raw = 0.40  # neutral base (slightly lower — MAs must confirm)

    # ── Trend structure: where price sits relative to MAs ──
    above_200 = ma200 and price > ma200
    above_50  = ma50  and price > ma50
    if above_200 and above_50:
        mom_raw += 0.15; signals.append("Price above 200MA + 50MA ✓")
    elif above_200:
        mom_raw += 0.07; signals.append("Price above 200MA")
    elif not above_200 and ma200:
        mom_raw -= 0.15; signals.append(f"Below 200MA (structural downtrend)")

    # MA50 trend extension — further above = stronger trend, more reward
    # (Changed from proximity logic which penalised momentum names running
    #  far above MA50. For this universe, extension = continuation signal.)
    if ma50 and price > 0:
        pct_above_50 = (price - ma50) / ma50 * 100
        if   pct_above_50 > 20: mom_raw += 0.10; signals.append(f"Strong trend: {pct_above_50:.0f}% above 50MA")
        elif pct_above_50 > 10: mom_raw += 0.08
        elif pct_above_50 >  5: mom_raw += 0.06
        elif pct_above_50 >  0: mom_raw += 0.03
        elif pct_above_50 < -5: mom_raw -= 0.08;  signals.append(f"Weak: {pct_above_50:.0f}% below 50MA")

    # ── 52-week range position (0 = at low, 1 = at high) ──
    if high52 > low52 and price > 0:
        pos52 = (price - low52) / (high52 - low52)
        if   pos52 > 0.80: mom_raw += 0.10; signals.append(f"Near 52w high ({pos52*100:.0f}th pct)")
        elif pos52 > 0.60: mom_raw += 0.05
        elif pos52 < 0.25: mom_raw -= 0.08; signals.append(f"Near 52w low ({pos52*100:.0f}th pct)")

    # ── 52-week return — extended thresholds for high-momentum names ──
    # Old max bucket was >30%. Stocks up 100%+ (NVDA, MSTR type) were scored
    # the same as a stock up 35%. Added >50% and >100% buckets.
    if week52_chg is not None:
        if   week52_chg > 1.00: mom_raw += 0.10; signals.append(f"52w return +{week52_chg*100:.0f}% (100%+)")
        elif week52_chg > 0.50: mom_raw += 0.08; signals.append(f"52w return +{week52_chg*100:.0f}%")
        elif week52_chg > 0.25: mom_raw += 0.06; signals.append(f"52w return +{week52_chg*100:.0f}%")
        elif week52_chg > 0.10: mom_raw += 0.03
        elif week52_chg < -0.15: mom_raw -= 0.10; signals.append(f"52w return {week52_chg*100:.0f}%")

    # ── Today's price move ──
    if   chg_pct > 3:   mom_raw += 0.12; signals.append(f"Strong day +{chg_pct}%")
    elif chg_pct > 1:   mom_raw += 0.07; signals.append(f"Positive day +{chg_pct}%")
    elif chg_pct > 0:   mom_raw += 0.03
    elif chg_pct < -3:  mom_raw -= 0.12; signals.append(f"Selling pressure {chg_pct}%")
    elif chg_pct < -1:  mom_raw -= 0.07

    # ── Volume confirmation ──
    if   vol_ratio > 2.0: mom_raw += 0.10; signals.append(f"Volume surge {vol_ratio:.1f}x avg")
    elif vol_ratio > 1.5: mom_raw += 0.05
    elif vol_ratio < 0.5: mom_raw -= 0.05

    breakdown["momentum"] = round(max(0, min(1.0, mom_raw)) * W["momentum"])

    # News Sentiment (default 20 pts)
    news_raw  = score_headline_sentiment(news)
    news_norm = (news_raw + 10) / 20
    breakdown["news_sentiment"] = round(news_norm * W["news_sentiment"])
    if news_raw > 3:    signals.append("Bullish news flow")
    elif news_raw < -3: signals.append("Negative news flow")

    # Macro Alignment (default 15 pts)
    breakdown["macro_alignment"] = round((macro_score.get("total", 50) / 100) * W["macro_alignment"])

    # Valuation (default 10 pts)
    fwd_pe  = quote.get("forward_pe")
    val_raw = 0.50
    if fwd_pe:
        if   fwd_pe < 12:  val_raw = 1.00; signals.append(f"Cheap valuation FwdPE {fwd_pe:.1f}")
        elif fwd_pe < 18:  val_raw = 0.85
        elif fwd_pe < 28:  val_raw = 0.70
        elif fwd_pe < 45:  val_raw = 0.50
        elif fwd_pe < 70:  val_raw = 0.30
        else:              val_raw = 0.15
    breakdown["valuation"] = round(val_raw * W["valuation"])

    total_score = sum(breakdown.values())
    if   total_score >= 75: conviction = "🔥 STRONG BUY"
    elif total_score >= 60: conviction = "✅ BUY"
    elif total_score >= 45: conviction = "👀 WATCH"
    elif total_score >= 30: conviction = "⚪ NEUTRAL"
    else:                   conviction = "❌ AVOID"

    classification = classify_stock(ticker, quote)
    return {
        "ticker":           ticker,
        "name":             quote.get("short_name", ticker),
        "price":            price,
        "change_pct":       chg_pct,
        "conviction":       conviction,
        "total_score":      total_score,
        "breakdown":        breakdown,
        "signals":          signals[:4],
        "upside_pct":       round(upside, 1),
        "analyst_rec":      rec,
        "n_analysts":       n_anal,
        "target_price":     round(target, 2) if target else None,
        "vol_ratio":        round(vol_ratio, 2),
        "forward_pe":       fwd_pe,
        "strategy_bucket":  classification["bucket"],
        "near_earnings":    classification["near_earnings"],
        "days_to_earnings": classification.get("days_to_earnings"),
        "beta":             quote.get("beta"),
    }


# ─── OUTCOME LOGGING ──────────────────────────────────────────────────────────

def log_signals(scored_stocks: list, macro_score: dict) -> int:
    today   = date.today().isoformat()
    details = macro_score.get("details", {})
    fieldnames = [
        "date", "ticker", "conviction", "total_score",
        "analyst_pts", "momentum_pts", "news_pts", "macro_pts", "valuation_pts",
        "entry_price", "regime_label", "regime_score", "strategy_bucket",
        "near_earnings", "days_to_earnings", "vol_ratio", "upside_pct", "beta",
        "adx", "spy_pct_from_200ma", "breadth_pct",
        "price_1d", "price_3d", "price_5d",
        "return_1d", "return_3d", "return_5d", "outcome_3d",
    ]
    existing_today = set()
    if os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("date") == today:
                    existing_today.add(row.get("ticker"))
    write_header = not os.path.exists(TRADE_LOG)
    logged = 0
    with open(TRADE_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for s in scored_stocks:
            if s.get("total_score", 0) < 55 or s["ticker"] in existing_today:
                continue
            bd = s.get("breakdown", {})
            writer.writerow({
                "date": today, "ticker": s["ticker"],
                "conviction": s.get("conviction", ""),
                "total_score": s.get("total_score", 0),
                "analyst_pts": bd.get("analyst", 0),
                "momentum_pts": bd.get("momentum", 0),
                "news_pts": bd.get("news_sentiment", 0),
                "macro_pts": bd.get("macro_alignment", 0),
                "valuation_pts": bd.get("valuation", 0),
                "entry_price": s.get("price", 0),
                "regime_label": macro_score.get("label", ""),
                "regime_score": macro_score.get("total", 0),
                "strategy_bucket": s.get("strategy_bucket", ""),
                "near_earnings": s.get("near_earnings", False),
                "days_to_earnings": s.get("days_to_earnings", ""),
                "vol_ratio": s.get("vol_ratio", 0),
                "upside_pct": s.get("upside_pct", 0),
                "beta": s.get("beta", ""),
                "adx": details.get("adx", ""),
                "spy_pct_from_200ma": details.get("spy_pct_from_200ma", ""),
                "breadth_pct": details.get("breadth_pct", ""),
                "price_1d": "", "price_3d": "", "price_5d": "",
                "return_1d": "", "return_3d": "", "return_5d": "",
                "outcome_3d": "",
            })
            logged += 1
    return logged


# ─── EARNINGS CALENDAR ────────────────────────────────────────────────────────

def get_earnings_this_week(tickers: list) -> list:
    upcoming = []
    today    = datetime.now()
    week_end = today.timestamp() + (7 * 86400)
    for ticker in tickers:
        q       = get_yahoo_quote(ticker)
        earn_ts = q.get("earnings_date")
        if earn_ts and today.timestamp() <= earn_ts <= week_end:
            earn_dt = datetime.fromtimestamp(earn_ts)
            upcoming.append({
                "ticker": ticker, "name": q.get("short_name", ticker),
                "date": earn_dt.strftime("%Y-%m-%d"), "day": earn_dt.strftime("%A"),
            })
    return upcoming


# ─── MAIN RUNNER ──────────────────────────────────────────────────────────────

def run_daily_analysis(tickers: list = None, quick: bool = False) -> dict:
    today_str = date.today().strftime("%Y-%m-%d")
    print(f"\n{'='*62}")
    print(f"  🏦  DAILY SENTIMENT ENGINE v2.0  —  {today_str}")
    print(f"{'='*62}\n")

    if tickers is None:
        tickers = list(DEFAULT_TICKERS)  # ETFs are no longer in DEFAULT_TICKERS

    print("📊 STEP 1 — Market Overview")
    market   = get_market_overview()
    vix_spot = market.get("VIX", {}).get("price") or 20

    print("\n😨 STEP 2 — Fear & Greed Index")
    fear_greed = get_fear_greed()
    print(f"  F&G: {fear_greed.get('score')} — {fear_greed.get('label')}")

    spy_tech = vix_term = breadth = {}

    if not quick:
        print("\n📈 STEP 3 — SPY Technical Indicators")
        spy_tech = get_spy_technical_indicators()
        if spy_tech.get("adx"):
            print(f"  ADX-14: {spy_tech['adx']} | Trend: {spy_tech['spy_trend']} "
                  f"| SPY vs 200MA: {spy_tech.get('pct_from_200ma', 0):+.1f}%")

        print("\n📉 STEP 4 — VIX Term Structure")
        vix_term = get_vix_term_structure(vix_spot)
        if vix_term:
            print(f"  VIX: {vix_term['vix']} | VIX3M: {vix_term['vix3m']} "
                  f"| Ratio: {vix_term['ratio']} ({vix_term['structure']})")
        else:
            print("  VIX3M unavailable — neutral score applied")

        print("\n🌐 STEP 5 — Sector Breadth")
        breadth = get_sector_breadth()
        if breadth:
    