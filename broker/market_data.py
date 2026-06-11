"""
broker/market_data.py — Real-time price layer

Replaces lagged yfinance data for all INTRADAY decisions with a fallback chain:

  1. Alpaca Market Data API  — real-time IEX feed, free with your keys
  2. Finnhub /quote          — real-time-ish, free 60 calls/min (key already in .env)
  3. yfinance                — 15-min delayed, last resort

yfinance remains the right tool for the nightly 618-ticker universe scan
(end-of-day data — lag irrelevant). This module is for the monitor, stops,
ATR and strategy sleeves where freshness matters.

Public API:
    get_latest_prices(tickers)  -> {ticker: price}
    get_today_opens(tickers)    -> {ticker: open_price or None}
    compute_atr(ticker, period) -> float | None     (daily bars)
    get_daily_closes(ticker, days) -> pd.Series | None
"""

import os
import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "").strip()


# ── Alpaca data client (lazy singleton) ────────────────────────────────────

_data_client = None

def _alpaca_data():
    global _data_client
    if _data_client is None:
        from alpaca.data.historical import StockHistoricalDataClient
        api_key = os.getenv("ALPACA_API_KEY", "").strip()
        secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
        if not api_key or not secret:
            raise ValueError("Alpaca keys not set")
        _data_client = StockHistoricalDataClient(api_key, secret)
    return _data_client


# ── Latest prices ──────────────────────────────────────────────────────────

def get_latest_prices(tickers: list) -> dict:
    """Real-time(ish) last trade price per ticker. Falls through providers."""
    if not tickers:
        return {}
    # 1. Alpaca latest trades (single batched call)
    try:
        from alpaca.data.requests import StockLatestTradeRequest
        req = StockLatestTradeRequest(symbol_or_symbols=tickers)
        trades = _alpaca_data().get_stock_latest_trade(req)
        out = {t: float(trades[t].price) for t in tickers if t in trades}
        if out:
            missing = [t for t in tickers if t not in out]
            if missing:
                out.update(_finnhub_prices(missing))
            return out
    except Exception as exc:
        log.warning("Alpaca latest trades failed (%s) — falling back", exc)
    # 2. Finnhub
    out = _finnhub_prices(tickers)
    if out:
        return out
    # 3. yfinance
    return _yf_prices(tickers)


def _finnhub_prices(tickers: list) -> dict:
    if not FINNHUB_KEY:
        return {}
    out = {}
    for t in tickers[:50]:  # stay under rate limit
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": t, "token": FINNHUB_KEY}, timeout=5,
            )
            c = r.json().get("c")
            if c:
                out[t] = float(c)
        except Exception:
            continue
    return out


def _yf_prices(tickers: list) -> dict:
    try:
        import yfinance as yf
        raw = yf.download(tickers, period="2d", auto_adjust=True, progress=False)
        out = {}
        close = raw["Close"]
        if len(tickers) == 1:
            s = close.squeeze().dropna()
            if not s.empty:
                out[tickers[0]] = float(s.iloc[-1])
        else:
            for t in tickers:
                try:
                    out[t] = float(close[t].dropna().iloc[-1])
                except Exception:
                    continue
        return out
    except Exception as exc:
        log.error("All price providers failed: %s", exc)
        return {}


# ── Today's opens ──────────────────────────────────────────────────────────

def get_today_opens(tickers: list) -> dict:
    """Today's official open per ticker (Alpaca daily bar → Finnhub → yfinance)."""
    if not tickers:
        return {}
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        req = StockBarsRequest(symbol_or_symbols=tickers, timeframe=TimeFrame.Day, start=start)
        bars = _alpaca_data().get_stock_bars(req)
        out = {}
        for t in tickers:
            try:
                blist = bars[t]
                out[t] = float(blist[0].open) if blist else None
            except Exception:
                out[t] = None
        if any(v is not None for v in out.values()):
            return out
    except Exception as exc:
        log.warning("Alpaca opens failed (%s) — falling back", exc)
    # Finnhub quote has 'o' = today's open
    out = {}
    if FINNHUB_KEY:
        for t in tickers[:50]:
            try:
                r = requests.get(
                    "https://finnhub.io/api/v1/quote",
                    params={"symbol": t, "token": FINNHUB_KEY}, timeout=5,
                )
                o = r.json().get("o")
                out[t] = float(o) if o else None
            except Exception:
                out[t] = None
        if any(v is not None for v in out.values()):
            return out
    # yfinance last resort
    try:
        import yfinance as yf
        raw = yf.download(tickers, period="1d", interval="5m", progress=False, auto_adjust=True)
        out = {}
        if len(tickers) == 1:
            col = raw["Open"].squeeze()
            out[tickers[0]] = float(col.iloc[0]) if not col.empty else None
        else:
            for t in tickers:
                try:
                    col = raw["Open"][t].dropna()
                    out[t] = float(col.iloc[0]) if not col.empty else None
                except Exception:
                    out[t] = None
        return out
    except Exception:
        return {t: None for t in tickers}


# ── Daily bars / ATR ───────────────────────────────────────────────────────

def get_daily_bars(ticker: str, days: int = 60):
    """Daily OHLC DataFrame (Alpaca first, yfinance fallback). None on failure."""
    import pandas as pd
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        start = datetime.now(timezone.utc) - timedelta(days=days * 1.6)
        req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=start)
        bars = _alpaca_data().get_stock_bars(req)
        rows = [{"High": float(b.high), "Low": float(b.low), "Close": float(b.close),
                 "Open": float(b.open)} for b in bars[ticker]]
        if rows:
            return pd.DataFrame(rows)
    except Exception as exc:
        log.debug("Alpaca bars failed for %s: %s", ticker, exc)
    try:
        import yfinance as yf
        raw = yf.download(ticker, period=f"{days}d", auto_adjust=True, progress=False)
        if not raw.empty:
            df = raw[["High", "Low", "Close", "Open"]].copy()
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            return df.reset_index(drop=True)
    except Exception:
        pass
    return None


def get_daily_closes(ticker: str, days: int = 250):
    """Daily close Series, oldest→newest. None on failure."""
    df = get_daily_bars(ticker, days=days)
    return df["Close"] if df is not None and not df.empty else None


def compute_atr(ticker: str, period: int = 14) -> float | None:
    """Average True Range over `period` days from real-time-capable daily bars."""
    df = get_daily_bars(ticker, days=max(60, period * 3))
    if df is None or len(df) < period + 1:
        return None
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = (
        (high - low).abs()
        .combine((high - prev_close).abs(), max)
        .combine((low - prev_close).abs(), max)
    )
    atr = float(tr.rolling(period).mean().iloc[-1])
    return round(atr, 4) if atr == atr else None  # NaN guard
