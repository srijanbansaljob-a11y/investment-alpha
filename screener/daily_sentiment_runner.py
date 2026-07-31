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
  6. Finnhub                  → Fundamentals: PE, analyst rec, earnings, beta, market cap (primary)
     Yahoo Finance            → Fundamentals fallback (blocked on GitHub Actions — avoid in prod)
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
from datetime import datetime, date, timedelta
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

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
JSON_OUTPUT = os.environ.get(
    "SCREENER_OUTPUT_FILE",
    os.path.join(OUTPUT_DIR, "daily_sentiment_data.json"),
)
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

FINNHUB_KEY       = os.getenv("FINNHUB_API_KEY", "").strip()
FINNHUB_API       = "https://finnhub.io/api/v1"
FINNHUB_AVAILABLE = bool(FINNHUB_KEY)

# ── Phase 2: Alternative signal caches (loaded once per run) ──────────────────
import pathlib as _pathlib

_SCRIPT_DIR      = _pathlib.Path(__file__).parent
_DATA_DIR        = _SCRIPT_DIR.parent / "data"
_INSIDER_CACHE   = None   # populated by _load_signal_caches()
_CONGRESS_CACHE  = None

def _load_signal_caches() -> None:
    """Load insider + congressional caches from data/ (first call only)."""
    global _INSIDER_CACHE, _CONGRESS_CACHE
    if _INSIDER_CACHE is not None:
        return
    try:
        p = _DATA_DIR / "insider_cache.json"
        _INSIDER_CACHE = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception as e:
        print(f"  ⚠  Could not load insider_cache.json: {e}")
        _INSIDER_CACHE = {}
    try:
        p = _DATA_DIR / "congressional_cache.json"
        _CONGRESS_CACHE = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception as e:
        print(f"  ⚠  Could not load congressional_cache.json: {e}")
        _CONGRESS_CACHE = {}


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
    Uses a 120-day calendar buffer so 260 trading-day requests never fall short.
    """
    if not ALPACA_AVAILABLE:
        return []
    from datetime import timedelta
    start_date = (date.today() - timedelta(days=days + 120)).isoformat()
    try:
        r = SESSION.get(
            f"{ALPACA_DATA}/v2/stocks/bars",
            headers=_alpaca_headers(),
            params={
                "symbols":    symbol,
                "timeframe":  "1Day",
                "start":      start_date,
                "limit":      days + 120,
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

    # 20-day return for RS vs SPY (Phase 3b)
    spy_return_20d = None
    if len(closes) >= 21:
        c_20 = closes[-21]
        spy_return_20d = round((current - c_20) / c_20 * 100, 2) if c_20 > 0 else None

    return {
        "adx":            adx_result.get("adx"),
        "plus_di":        adx_result.get("plus_di"),
        "minus_di":       adx_result.get("minus_di"),
        "spy_trend":      adx_result.get("trend"),
        "spy_price":      round(current, 2),
        "ma_200":         round(ma200, 2),
        "pct_from_200ma": round(pct_from_200ma, 2),
        "return_20d":     spy_return_20d,
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
        start_date = (date.today() - timedelta(days=380)).isoformat()
        try:
            r = SESSION.get(
                f"{ALPACA_DATA}/v2/stocks/bars",
                headers=_alpaca_headers(),
                params={
                    "symbols":    ",".join(etf_symbols),
                    "timeframe":  "1Day",
                    "start":      start_date,
                    "limit":      380,
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

                # ATR-14 (Average True Range over last 14 bars)
                # TR = max(H-L, |H-prev_C|, |L-prev_C|)
                atr_14 = None
                if len(bars) >= 15:
                    trs = []
                    for j in range(len(bars) - 14, len(bars)):
                        h, l, c_prev = bars[j]["h"], bars[j]["l"], bars[j-1]["c"]
                        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
                    atr_14 = sum(trs) / 14
                # ATR as % of current price (for regime-based target calculation)
                current_close = closes[-1] if closes else None
                atr_pct = round((atr_14 / current_close) * 100, 3) if (atr_14 and current_close) else None

                # 20-day price return (for RS vs SPY, Phase 3b)
                return_20d = None
                if len(closes) >= 21:
                    c_20 = closes[-21]
                    c_now = closes[-1]
                    return_20d = round((c_now - c_20) / c_20 * 100, 2) if c_20 > 0 else None

                results[sym] = {
                    "ma50":          round(ma50, 4)  if ma50  else None,
                    "ma200":         round(ma200, 4) if ma200 else None,
                    "52w_high":      round(hi52, 4),
                    "52w_low":       round(lo52, 4),
                    "week52_change": week52_chg,
                    "avg_volume":    avg_vol_20,
                    "atr_14":        round(atr_14, 4) if atr_14 else None,
                    "atr_pct":       atr_pct,
                    "return_20d":    return_20d,
                }
        except Exception as e:
            print(f"  ⚠  Alpaca bars batch {i//batch_size+1} failed: {e}")
        time.sleep(0.15)
    return results

# ─── STOCK QUOTES ─────────────────────────────────────────────────────────────

def _get_finnhub_fundamentals(tickers: list) -> dict:
    """
    Finnhub replacement for Yahoo Finance fundamentals.
    Works reliably on GitHub Actions (API-key auth, no IP blocking).

    Fetches per ticker:
      /stock/metric        → market cap, beta, PE, 52w high/low
      /stock/recommendation → analyst consensus + analyst count
      /stock/price-target   → mean analyst price target

    Plus one batch call:
      /calendar/earnings   → upcoming earnings dates for all tickers

    Rate limit: 60 calls/min (free). Uses 5-worker thread pool.
    """
    if not FINNHUB_AVAILABLE:
        print("  ⚠  FINNHUB_API_KEY not set — fundamentals unavailable")
        return {}

    results = {t: {"short_name": t} for t in tickers}

    # ── Step 1: Earnings calendar (one call covers all tickers) ──────────────
    try:
        today  = date.today()
        from_d = today.isoformat()
        to_d   = (today + timedelta(days=90)).isoformat()
        r = SESSION.get(
            f"{FINNHUB_API}/calendar/earnings",
            params={"from": from_d, "to": to_d, "token": FINNHUB_KEY},
            timeout=15,
        )
        if r.ok:
            ticker_set = set(tickers)
            for ev in r.json().get("earningsCalendar", []):
                sym = ev.get("symbol")
                if sym in ticker_set and "earnings_date" not in results[sym]:
                    try:
                        ts = int(datetime.fromisoformat(ev["date"]).timestamp())
                        results[sym]["earnings_date"] = ts
                    except Exception:
                        pass
            print(f"  ✓ Finnhub earnings calendar fetched ({to_d})")
    except Exception as e:
        print(f"  ⚠  Finnhub earnings calendar failed: {e}")

    # ── Step 2: Per-ticker: metrics + recommendations + price target ──────────
    def fetch_ticker_fundamentals(ticker):
        out = results[ticker]

        # Metrics: market cap, beta, PE, 52w range
        try:
            r = SESSION.get(
                f"{FINNHUB_API}/stock/metric",
                params={"symbol": ticker, "metric": "all", "token": FINNHUB_KEY},
                timeout=10,
            )
            if r.ok:
                m = r.json().get("metric", {})
                mc = m.get("marketCapitalization")   # Finnhub reports in $M
                out["market_cap"] = mc * 1_000_000 if mc else None
                out["beta"]       = m.get("beta")
                out["pe_ratio"]   = m.get("peBasicExclExtraTTM")
                out["forward_pe"] = m.get("peNormalizedAnnual")
                out["eps_fwd"]    = m.get("epsNormalizedAnnual")
                out["_yf_52h"]    = m.get("52WeekHigh")
                out["_yf_52l"]    = m.get("52WeekLow")
        except Exception as e:
            print(f"  ⚠  Finnhub metric {ticker}: {e}")

        time.sleep(0.15)  # ~6-7 calls/s across 5 workers → well under 60/min

        # Analyst recommendations (most recent period)
        try:
            r = SESSION.get(
                f"{FINNHUB_API}/stock/recommendation",
                params={"symbol": ticker, "token": FINNHUB_KEY},
                timeout=10,
            )
            if r.ok:
                recs = r.json()
                if recs:
                    latest     = recs[0]
                    strong_buy = latest.get("strongBuy", 0)
                    buy        = latest.get("buy", 0)
                    hold       = latest.get("hold", 0)
                    sell       = latest.get("sell", 0)
                    strong_sell = latest.get("strongSell", 0)
                    total      = strong_buy + buy + hold + sell + strong_sell
                    out["num_analysts"] = total
                    if total > 0:
                        bull_pct = (strong_buy + buy) / total
                        bear_pct = (sell + strong_sell) / total
                        if strong_buy / total > 0.4:
                            out["recommend"] = "strong_buy"
                        elif bull_pct > 0.6:
                            out["recommend"] = "buy"
                        elif bear_pct > 0.4:
                            out["recommend"] = "sell"
                        elif bear_pct > 0.6:
                            out["recommend"] = "strong_sell"
                        else:
                            out["recommend"] = "hold"
        except Exception as e:
            print(f"  ⚠  Finnhub rec {ticker}: {e}")

        time.sleep(0.15)

        # Earnings surprise (Phase 2c) — most recent quarter vs estimate
        try:
            r = SESSION.get(
                f"{FINNHUB_API}/stock/earnings",
                params={"symbol": ticker, "limit": 4, "token": FINNHUB_KEY},
                timeout=10,
            )
            if r.ok:
                earns = r.json()
                if earns:
                    latest = earns[0]
                    actual   = latest.get("actual")
                    estimate = latest.get("estimate")
                    if actual is not None and estimate and abs(estimate) > 0.01:
                        surprise_pct = (actual - estimate) / abs(estimate) * 100
                        out["earnings_surprise_pct"] = round(surprise_pct, 1)
        except Exception as e:
            print(f"  ⚠  Finnhub earnings {ticker}: {e}")

        time.sleep(0.15)

        # Analyst price target (for upside calculation)
        try:
            r = SESSION.get(
                f"{FINNHUB_API}/stock/price-target",
                params={"symbol": ticker, "token": FINNHUB_KEY},
                timeout=10,
            )
            if r.ok:
                pt = r.json()
                out["analyst_target"] = pt.get("targetMean")
        except Exception as e:
            print(f"  ⚠  Finnhub price-target {ticker}: {e}")

    print(f"  → Fetching Finnhub fundamentals for {len(tickers)} tickers (threaded)...")
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fetch_ticker_fundamentals, t): t for t in tickers}
        done = 0
        for f in as_completed(futures):
            done += 1
            if done % 25 == 0:
                print(f"  → Finnhub: {done}/{len(tickers)} tickers done")
    print(f"  ✓ Finnhub fundamentals complete")
    return results


def _get_yahoo_fundamentals(tickers: list) -> dict:
    """
    Yahoo Finance fallback — only used when Finnhub is unavailable.
    NOTE: Blocked on GitHub Actions IPs. Use Finnhub in production.
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
    Finnhub for fundamentals (PE, analyst rec, earnings, beta, market cap).

    Data flow:
      1. Finnhub fundamentals → PE, analyst rec, earnings, beta, market cap (primary)
         Yahoo Finance         → same fields, fallback when Finnhub unavailable
      2. Alpaca snapshots     → price, change_pct, volume (real-time)
      3. Alpaca bars          → MA50, MA200, 52w high/low, avg_volume, week52_change
      4. Merge all → unified quote dict

    Falls back to Yahoo for price data if Alpaca credentials not set.
    """
    price_source = 'Alpaca real-time' if ALPACA_AVAILABLE else 'Yahoo Finance (no Alpaca key)'
    fund_source  = 'Finnhub' if FINNHUB_AVAILABLE else 'Yahoo Finance (no Finnhub key)'
    print(f"  → Fetching quotes for {len(tickers)} tickers "
          f"[price: {price_source} | fundamentals: {fund_source}]...")

    # Step 1: Fundamentals — Finnhub preferred, Yahoo fallback
    if FINNHUB_AVAILABLE:
        fundamentals = _get_finnhub_fundamentals(tickers)
    else:
        print("  ⚠  Falling back to Yahoo Finance for fundamentals (may fail on GitHub Actions)")
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
                # ATR from bar history — used for dynamic bracket targets at buy time
                "atr_14":         bars.get("atr_14"),
                "atr_pct":        bars.get("atr_pct"),
                # 20-day return for RS vs SPY (Phase 3b)
                "return_20d":     bars.get("return_20d"),
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



# ─── Phase 3a: YIELD CURVE (FRED T10Y2Y) ─────────────────────────────────────

def _get_yield_curve() -> dict:
    """
    Fetch 10Y-2Y Treasury spread from FRED public CSV endpoint (no API key needed).
    Returns {"spread": float, "status": str} or {} on failure.
    Positive spread = normal (bullish), negative = inverted (bearish).
    Feature flag: config.YIELD_CURVE_ENABLED
    """
    try:
        import config as _cfg
        if not getattr(_cfg, "YIELD_CURVE_ENABLED", True):
            return {}
    except ImportError:
        pass

    try:
        r = SESSION.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv",
            params={"id": "T10Y2Y"},
            timeout=12,
        )
        if not r.ok:
            return {}
        lines = [l for l in r.text.strip().splitlines() if l and not l.startswith("DATE")]
        if not lines:
            return {}
        # Last non-null value
        for line in reversed(lines):
            parts = line.split(",")
            if len(parts) == 2 and parts[1].strip() not in ("", "."):
                spread = float(parts[1].strip())
                status = (
                    "steep_normal"   if spread > 1.0  else
                    "normal"         if spread > 0.25 else
                    "flat"           if spread > 0.0  else
                    "mild_inversion" if spread > -0.5 else
                    "deep_inversion"
                )
                return {"spread": round(spread, 3), "status": status, "date": parts[0]}
    except Exception as e:
        print(f"  ⚠  Yield curve (FRED): {e}")
    return {}


# ─── Phase 3c: EQUITY PUT/CALL RATIO (FRED CPCE) ─────────────────────────────

def _get_putcall_ratio() -> dict:
    """
    Fetch CBOE equity put/call ratio from FRED (series CPCE, daily, no API key).
    < 0.5 = call-heavy (bullish), > 1.2 = put-heavy (fearful/bearish).
    Feature flag: config.PUTCALL_ENABLED
    """
    try:
        import config as _cfg
        if not getattr(_cfg, "PUTCALL_ENABLED", True):
            return {}
    except ImportError:
        pass

    try:
        r = SESSION.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv",
            params={"id": "CPCE"},
            timeout=12,
        )
        if not r.ok:
            return {}
        lines = [l for l in r.text.strip().splitlines() if l and not l.startswith("DATE")]
        if not lines:
            return {}
        for line in reversed(lines):
            parts = line.split(",")
            if len(parts) == 2 and parts[1].strip() not in ("", "."):
                ratio = float(parts[1].strip())
                return {"ratio": round(ratio, 3), "date": parts[0]}
    except Exception as e:
        print(f"  ⚠  Put/call ratio (FRED CPCE): {e}")
    return {}


# ─── MACRO REGIME SCORE (6 components, 100 pts) ───────────────────────────────

def compute_macro_score(market: dict, fear_greed: dict,
                        spy_tech: dict = None,
                        vix_term: dict = None,
                        breadth:  dict = None,
                        yield_curve: dict = None,
                        putcall: dict = None) -> dict:
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

    # ── 7. Yield Curve 10Y-2Y (Phase 3a) ─────────────────────────────────
    try:
        import config as _cfg
        _yc_max = getattr(_cfg, "YIELD_CURVE_MAX_SCORE", 10)
    except ImportError:
        _yc_max = 10

    if yield_curve and yield_curve.get("spread") is not None and getattr(
            __import__("builtins"), "__dict__", {}).get("__import__", __import__)("config" if False else "builtins"):
        pass
    # simpler inline check:
    _yc_enabled = True
    try:
        import config as _c2
        _yc_enabled = getattr(_c2, "YIELD_CURVE_ENABLED", True)
    except ImportError:
        pass
    if _yc_enabled and yield_curve and yield_curve.get("spread") is not None:
        sp = yield_curve["spread"]
        scores["yield_curve"] = (
            _yc_max       if sp > 1.0   else
            int(_yc_max * 0.8) if sp > 0.25 else
            int(_yc_max * 0.6) if sp > 0.0  else
            int(_yc_max * 0.3) if sp > -0.5 else
            0
        )
        details.update({"yield_curve_spread": sp, "yield_curve_status": yield_curve.get("status")})
    elif _yc_enabled:
        scores["yield_curve"] = int(_yc_max * 0.6)   # flat/unavailable → neutral

    # ── 8. Equity Put/Call Ratio (Phase 3c) ────────────────────────────────
    _pc_enabled = True
    _pc_max = 5
    try:
        import config as _c3
        _pc_enabled = getattr(_c3, "PUTCALL_ENABLED", True)
        _pc_max     = getattr(_c3, "PUTCALL_MAX_SCORE", 5)
    except ImportError:
        pass
    if _pc_enabled and putcall and putcall.get("ratio") is not None:
        pc = putcall["ratio"]
        scores["putcall"] = (
            _pc_max       if pc < 0.5  else
            int(_pc_max * 0.8) if pc < 0.7  else
            int(_pc_max * 0.6) if pc < 0.9  else
            int(_pc_max * 0.4) if pc < 1.2  else
            0
        )
        details.update({"putcall_ratio": pc, "putcall_date": putcall.get("date")})
    elif _pc_enabled:
        scores["putcall"] = int(_pc_max * 0.6)   # unavailable → neutral

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
        "spy_return_20d":       (spy_tech or {}).get("return_20d"),
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

    # When market_cap == 0, Yahoo Finance was unreachable. Our universe is S&P 500
    # so assume large-cap when data is unavailable rather than excluding everything.
    large_cap    = market_cap > 10e9 if market_cap else True
    # well_covered: treat as covered when num_analysts == 0 (API unreachable vs truly uncovered)
    well_covered = num_analysts >= 15 if num_analysts else True
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
    # Only apply analyst-count threshold when we actually received coverage data.
    # If num_analysts == 0 it means the API was unreachable, not that the stock is uncovered.
    sparse_coverage = num_analysts > 0 and num_analysts < 5
    if weak_rec or sparse_coverage or (forward_pe and forward_pe > 150):
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

    # ── Phase 2: Alternative signals (additive bonuses, feature-flagged) ──────
    try:
        import config as _cfg
    except ImportError:
        _cfg = None

    _load_signal_caches()

    # Phase 2a — Insider buying bonus
    if getattr(_cfg, "INSIDER_SIGNAL_ENABLED", True):
        _insider = (_INSIDER_CACHE or {}).get(ticker, {})
        if isinstance(_insider, dict) and _insider.get("signal", 0) >= 1:
            _bonus = getattr(_cfg, "INSIDER_SINGLE_BUY_BONUS", 5)
            breakdown["insider_buy"] = _bonus
            signals.append(f"🔍 Insider buying signal (+{_bonus}pts)")

    # Phase 2b — Congressional trading bonus
    if getattr(_cfg, "CONGRESS_SIGNAL_ENABLED", True):
        _congress = (_CONGRESS_CACHE or {}).get(ticker, {})
        _buys = _congress.get("recent_buys", 0) if isinstance(_congress, dict) else 0
        if _buys > 0:
            _bonus = min(_buys * getattr(_cfg, "CONGRESS_BUY_BONUS", 5), 10)
            breakdown["congress_buy"] = _bonus
            s = "s" if _buys > 1 else ""
            signals.append(f"🏛️ Congressional buy ({_buys} member{s}, +{_bonus}pts)")

    # Phase 2c — Post-earnings momentum bonus
    if getattr(_cfg, "EARNINGS_SIGNAL_ENABLED", True):
        _surprise = quote.get("earnings_surprise_pct")
        _threshold = getattr(_cfg, "EARNINGS_BEAT_THRESHOLD_PCT", 10)
        if _surprise is not None and _surprise > _threshold:
            _bonus = getattr(_cfg, "EARNINGS_BEAT_BONUS", 8)
            breakdown["earnings_beat"] = _bonus
            signals.append(f"📈 Earnings beat +{_surprise:.0f}% vs estimate (+{_bonus}pts)")

    # Phase 3b — Relative strength vs SPY (20-day momentum comparison)
    if getattr(_cfg, "RS_SPY_ENABLED", True):
        _stock_ret = quote.get("return_20d")
        _spy_ret   = macro_score.get("spy_return_20d")
        if _stock_ret is not None and _spy_ret is not None:
            _rs = _stock_ret - _spy_ret
            _max_bonus  = getattr(_cfg, "RS_SPY_MAX_BONUS", 8)
            _max_penalty = getattr(_cfg, "RS_SPY_MAX_PENALTY", -5)
            if _rs > 10:
                _score = _max_bonus
                signals.append(f"🚀 Strong RS vs SPY: +{_rs:.1f}% outperformance (+{_score}pts)")
            elif _rs > 5:
                _score = int(_max_bonus * 0.7)
                signals.append(f"📊 RS vs SPY: +{_rs:.1f}% outperformance (+{_score}pts)")
            elif _rs > 0:
                _score = int(_max_bonus * 0.3)
            elif _rs > -5:
                _score = int(_max_penalty * 0.5)
                signals.append(f"📉 Lagging SPY by {abs(_rs):.1f}% ({_score}pts)")
            else:
                _score = _max_penalty
                signals.append(f"🔻 Significant underperformance vs SPY: {_rs:.1f}% ({_score}pts)")
            if _score != 0:
                breakdown["rs_vs_spy"] = _score

    total_score = sum(breakdown.values())
    if   total_score >= 75: conviction = "🔥 STRONG BUY"
    elif total_score >= 60: conviction = "✅ BUY"
    elif total_score >= 45: conviction = "👀 WATCH"
    elif total_score >= 30: conviction = "⚪ NEUTRAL"
    else:                   conviction = "❌ AVOID"

    classification = classify_stock(ticker, quote)

    # ── Dynamic stop / take-profit targets (ATR-based, regime-adjusted) ──────
    # These are stored in KV so the Worker can use them when placing bracket orders.
    # Multipliers by regime:
    #   Stop loss:       Strong Bull 2.0×, Mod Bull 1.5×, Neutral 1.25×, Bearish 1.0×
    #   TP Alpaca ceil:  Strong Bull 4.0×, Mod Bull 3.0×, Neutral 2.0×,  Bearish 1.5×
    #   TP monitor:      80% of Alpaca ceiling (fires BEFORE Alpaca, triggers 2-min window)
    # Floor / cap applied after to keep values realistic.
    regime_label = macro_score.get("label", "NEUTRAL")
    atr_pct = quote.get("atr_pct")  # filled in by get_alpaca_bars()

    _stop_mult  = {"STRONG BULL": 2.0, "MODERATE BULL": 1.5, "NEUTRAL": 1.25, "BEARISH": 1.0}
    _ceil_mult  = {"STRONG BULL": 4.0, "MODERATE BULL": 3.0, "NEUTRAL": 2.0,  "BEARISH": 1.5}
    stop_m = _stop_mult.get(regime_label, 1.5)
    ceil_m = _ceil_mult.get(regime_label, 3.0)

    if atr_pct:
        raw_stop   = atr_pct * stop_m
        raw_ceil   = atr_pct * ceil_m
        stop_pct   = round(max(2.0, min(raw_stop, 8.0)), 2)   # floor 2%, cap 8%
        tp_alpaca  = round(max(8.0, min(raw_ceil, 35.0)), 2)  # floor 8%, cap 35%
        tp_monitor = round(max(6.0, min(raw_ceil * 0.8, 28.0)), 2)  # 80% of ceiling
    else:
        # Fallback to fixed values when ATR unavailable
        stop_pct   = 5.0
        tp_alpaca  = 12.0
        tp_monitor = 9.0

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
        # ── Dynamic order targets ──────────────────────────────────────────
        "atr_pct":          atr_pct,
        "stop_pct":         stop_pct,    # ATR-based stop loss %
        "tp_monitor_pct":   tp_monitor,  # monitor alert fires here (2-min window)
        "tp_alpaca_pct":    tp_alpaca,   # Alpaca hard ceiling (bracket take_profit)
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
            print(f"  {breadth['above_count']}/{breadth['total_checked']} "
                  f"ETFs above 200MA — {breadth['label']}")
    else:
        print("\n⚡ Quick mode — technicals skipped")

    print("\n📐 STEP 5b — Yield Curve + Put/Call")
    _yield_curve = _get_yield_curve() if not quick else {}
    _putcall     = _get_putcall_ratio() if not quick else {}
    if _yield_curve:
        print(f"  10Y-2Y Spread: {_yield_curve['spread']:+.3f}% ({_yield_curve['status']})")
    else:
        print("  Yield curve unavailable — neutral score applied")
    if _putcall:
        print(f"  Equity P/C Ratio: {_putcall['ratio']} ({_putcall.get('date', 'latest')})")
    else:
        print("  Put/call ratio unavailable — neutral score applied")

    macro = compute_macro_score(market, fear_greed, spy_tech, vix_term, breadth,
                                yield_curve=_yield_curve, putcall=_putcall)
    print(f"\n🎯 REGIME: {macro['label']}  ({macro['total']}/100)")
    print(f"   Active strategies: {', '.join(macro['permitted_strategies'])}")

    step = 4 if quick else 6
    print(f"\n🔄 STEP {step} — Sector Rotation")
    sectors = get_sector_rotation()

    step = 5 if quick else 7
    print(f"\n🎯 STEP {step} — Scoring {len(tickers)} Stocks")
    quotes        = get_stock_quotes(tickers)
    scored_stocks = []

    for ticker in tickers:
        quote = quotes.get(ticker, {})
        if "error" in quote:
            continue
        news        = get_yahoo_news(ticker) if not quick else []
        stock_score = score_stock(ticker, quote, news, macro)
        scored_stocks.append(stock_score)
        if not quick:
            time.sleep(0.1)

    scored_stocks.sort(key=lambda x: x["total_score"], reverse=True)

    logged = log_signals(scored_stocks, macro)
    print(f"\n📝 {logged} new BUY+ signals logged — trade_log.csv")

    # Phase 4 — VIX spike opportunity fund check
    _vix_spot = market.get("VIX", {}).get("price") or 0
    print(f"\n🔍 Phase 4 — VIX check ({_vix_spot:.1f})")
    _maybe_post_vix_panic(_vix_spot, scored_stocks)

    results = {
        "date": today_str, "generated_at": datetime.now().strftime("%H:%M:%S ET"),
        "market": market, "fear_greed": fear_greed, "macro_score": macro,
        "spy_technicals": spy_tech, "vix_term": vix_term, "breadth": breadth,
        "sectors": sectors, "stocks": scored_stocks, "score_weights": SCORE_WEIGHTS,
        "sources_used": [
            "CNN Fear & Greed Index", "Yahoo Finance JSON + v8 OHLCV API",
            "CBOE VIX + VIX3M", "Sector ETF breadth (11 ETFs vs 200MA)",
            "SPY ADX-14 + 200MA position",
        ],
    }
    with open(JSON_OUTPUT, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"✅ JSON saved — {JSON_OUTPUT}")
    print_summary(results)
    return results



# ─── Phase 4: VIX SPIKE OPPORTUNITY FUND ─────────────────────────────────────

_VIX_PANIC_FILE = _DATA_DIR / "vix_panic_state.json"  # _DATA_DIR defined in Phase 2 block

def _load_vix_panic_state() -> dict:
    try:
        if _VIX_PANIC_FILE.exists():
            return json.loads(_VIX_PANIC_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"in_panic": False}


def _save_vix_panic_state(state: dict) -> None:
    try:
        _VIX_PANIC_FILE.parent.mkdir(parents=True, exist_ok=True)
        _VIX_PANIC_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠  Could not save VIX panic state: {e}")


def _post_vix_discord(embeds: list) -> None:
    """Post embeds to Discord via webhook (uses DISCORD_WEBHOOK_URL env var)."""
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("  ⚠  DISCORD_WEBHOOK_URL not set — skipping VIX panic Discord post")
        return
    try:
        r = SESSION.post(webhook, json={"embeds": embeds}, timeout=10)
        if r.ok:
            print(f"  ✓ VIX alert posted to Discord")
        else:
            print(f"  ⚠  Discord post failed: {r.status_code}")
    except Exception as e:
        print(f"  ⚠  Discord post error: {e}")


def _maybe_post_vix_panic(vix: float, scored_stocks: list) -> None:
    """
    Phase 4 — VIX Spike Opportunity Fund.
    When VIX spikes above VIX_PANIC_THRESHOLD (default 30), post a Discord alert
    with the top picks from the screener — these are the dip-buying opportunities.
    When VIX recovers below VIX_ALLCLEAR_THRESHOLD (default 20), post all-clear.
    Tracks state in data/vix_panic_state.json to avoid repeated alerts.
    """
    try:
        import config as _cfg
        if not getattr(_cfg, "VIX_PANIC_ENABLED", True):
            return
        panic_threshold  = getattr(_cfg, "VIX_PANIC_THRESHOLD", 30)
        allclear_threshold = getattr(_cfg, "VIX_ALLCLEAR_THRESHOLD", 20)
        top_n            = getattr(_cfg, "VIX_PANIC_TOP_N", 3)
    except ImportError:
        panic_threshold, allclear_threshold, top_n = 30, 20, 3

    state = _load_vix_panic_state()

    if vix >= panic_threshold and not state.get("in_panic"):
        # ── New panic spike → post buy opportunity alert ──────────────────
        print(f"\n🚨 VIX SPIKE DETECTED ({vix:.1f} >= {panic_threshold}) -- posting opportunity alert...")
        top_picks = [s for s in scored_stocks if s.get("total_score", 0) >= 50][:top_n]

        fields = []
        for s in top_picks:
            fields.append({
                "name": f"{s['ticker']} — {s.get('conviction', '')}",
                "value": (
                    f"Score: **{s.get('total_score', 0)}** | "
                    f"Price: ${s.get('price', 0):.2f} | "
                    f"Upside: {s.get('upside_pct', 0):+.0f}%\n"
                    f"Bucket: {s.get('strategy_bucket', 'N/A')}"
                ),
                "inline": False,
            })

        embeds = [{
            "title": f"🚨 VIX SPIKE — Opportunity Fund Alert (VIX = {vix:.1f})",
            "description": (
                f"VIX has spiked to **{vix:.1f}** (threshold: {panic_threshold}).\n"
                f"This is a potential **buying opportunity** for quality stocks at a discount.\n"
                f"Consider deploying reserved cash into the top screener picks below."
            ),
            "color": 0xE74C3C,
            "fields": fields if fields else [{"name": "No high-conviction picks today", "value": "Run a fresh screener for latest data", "inline": False}],
            "footer": {"text": "Investment Alpha — Phase 4 VIX Opportunity Fund | Paper trading only"},
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }]
        _post_vix_discord(embeds)
        _save_vix_panic_state({"in_panic": True, "panic_vix": round(vix, 2),
                               "panic_at": datetime.now().isoformat()})

    elif vix < allclear_threshold and state.get("in_panic"):
        # ── VIX recovered → post all-clear ───────────────────────────────
        print(f"\n✅ VIX recovered ({vix:.1f} < {allclear_threshold}) -- posting all-clear...")
        embeds = [{
            "title": f"✅ VIX All-Clear (VIX = {vix:.1f})",
            "description": (
                f"VIX has recovered to **{vix:.1f}** (all-clear: < {allclear_threshold}).\n"
                f"Market stress is easing. Positions opened during the spike ({state.get('panic_vix', '?')}) "
                f"should be showing gains. Consider reviewing your stop-losses."
            ),
            "color": 0x2ECC71,
            "footer": {"text": "Investment Alpha — Phase 4 VIX Opportunity Fund"},
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }]
        _post_vix_discord(embeds)
        _save_vix_panic_state({"in_panic": False, "recovered_vix": round(vix, 2),
                               "recovered_at": datetime.now().isoformat()})

    else:
        print(f"  VIX = {vix:.1f} — {'in panic mode (alert already sent)' if state.get('in_panic') else 'normal range'}")


# ─── SUMMARY ──────────────────────────────────────────────────────────────────────────────────────

def print_summary(results: dict):
    print(f"\n{'='*62}")
    print(f"  📅 {results['date']}  |  {results['generated_at']}")
    print(f"{'='*62}")

    m      = results["macro_score"]
    fg     = results["fear_greed"]
    market = results["market"]
    spy    = results.get("spy_technicals", {})
    vt     = results.get("vix_term", {})
    br     = results.get("breadth", {})

    print(f"\n🌍 REGIME: {m['label']}  ({m['total']}/100)")
    parts = [f"{k}={v}" for k, v in m.get("components", {}).items()]
    print(f"   {' | '.join(parts)}")
    vix = market.get("VIX", {}).get("price", "N/A")
    sp  = market.get("SP500", {}).get("change_pct", "N/A")
    print(f"   VIX: {vix}", end="")
    if vt:
        print(f" → VIX3M: {vt.get('vix3m')} ({vt.get('structure','')})", end="")
    print(f" | F&G: {fg.get('score')} ({fg.get('label')})")
    if spy.get("adx"):
        print(f"   ADX-14: {spy['adx']} ({spy.get('spy_trend')}) | "
              f"SPY vs 200MA: {spy.get('pct_from_200ma', 0):+.1f}%")
    if br:
        print(f"   Breadth: {br.get('above_count')}/{br.get('total_checked')} "
              f"above 200MA ({br.get('label')})")
    if isinstance(sp, float):
        print(f"   S&P 500: {sp:+.2f}%")
    print(f"   Permitted: {', '.join(m.get('permitted_strategies', []))}")

    sectors  = results.get("sectors", [])
    if sectors:
        print(f"\n🔄 SECTORS")
        for s in sectors[:3]:
            print(f"   ▲ {s.get('sector',''):<14} {s.get('etf',''):<5} {s.get('change_pct',0):+.2f}%")
        for s in sectors[-2:]:
            print(f"   ▼ {s.get('sector',''):<14} {s.get('etf',''):<5} {s.get('change_pct',0):+.2f}%")

    stocks    = results.get("stocks", [])
    permitted = m.get("permitted_strategies", [])
    strong    = [s for s in stocks if "STRONG BUY" in s.get("conviction", "")]
    buy       = [s for s in stocks if s.get("conviction", "") == "✅ BUY"]

    print(f"\n🎯 TOP PICKS  ({len(strong)} Strong Buy / {len(buy)} Buy)")
    print(f"  {'#':<3} {'Ticker':<7} {'Name':<22} {'Score':<6} {'Conv.':<14} "
          f"{'Chg%':<7} {'Upside':<8} {'Bucket':<15} OK?")
    print(f"  {'-'*90}")
    for i, s in enumerate(stocks[:15], 1):
        upside    = f"+{s['upside_pct']:.1f}%" if s.get('upside_pct', 0) > 0 else f"{s.get('upside_pct',0):.1f}%"
        bucket    = s.get("strategy_bucket", "watch")
        regime_ok = "✅" if bucket in permitted else "🚫"
        near_earn = " ⚠EARN" if s.get("near_earnings") else ""
        print(f"  {i:<3} {s['ticker']:<7} {s.get('name','')[:21]:<22} "
              f"{s['total_score']:<6} {s['conviction']:<14} "
              f"{s.get('change_pct',0):+.2f}%  {upside:<8} {bucket:<15} {regime_ok}{near_earn}")
        for sig in s.get("signals", [])[:2]:
            print(f"      └─ {sig}")

    print(f"\n{'='*62}")
    print(f"  ⚠  Not financial advice. Run 8–9 AM ET before market open.")
    print(f"{'='*62}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily Market Sentiment Engine v2.0")
    parser.add_argument("--quick",  action="store_true",
                        help="Skip SPY technicals, VIX term structure, breadth")
    parser.add_argument("--stocks", nargs="+", metavar="TICKER",
                        help="Score specific tickers only")
    args    = parser.parse_args()
    tickers = args.stocks if args.stocks else None
    run_daily_analysis(tickers=tickers, quick=args.quick)
