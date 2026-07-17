"""
pipeline/congressional.py - Congressional Trading Signal (STOCK Act)

Under the STOCK Act (2012), US senators and representatives must disclose
trades within 45 days. This module fetches those disclosures via the free
Capitol Trades API and converts them into a buy/sell signal that has shown
5-10% annual alpha, especially from Intelligence and Armed Services committee members.

Data source: Capitol Trades API (free, no key required)
  https://api.capitoltrades.com/trades?pageSize=100&issuer={ticker}

Signal scoring (float in [-1, +1]):
  +1.0 : 3+ members bought in last 90 days, each > $50k
  +0.5 : 1-2 members bought net positive
   0.0 : no trades, or buys and sells cancel out
  -0.5 : net selling

Cache: data/congressional_cache_pipeline.json (24-hour TTL, same pattern as insider.py)
Enable via: config.CONGRESSIONAL_ENABLED = True

NOTE: this used to share data/congressional_cache.json with the screener's
scripts/fetch_congressional_trades.py — same filename, incompatible schemas
({signal: float} here vs {recent_buys, last_buy_date, buyers} there). Whichever
ran last would silently clobber the other. Renamed to keep pipeline self-
contained regardless of whether the screener's fetcher runs.
"""

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)

_BASE_URL     = "https://api.capitoltrades.com/trades"
_HEADERS      = {
    "User-Agent": "InvestmentAlpha/3.0 srijanbansal@gmail.com",
    "Accept":     "application/json",
}
CACHE_FILE    = Path(getattr(config, "DATA_DIR", "data")) / "congressional_cache_pipeline.json"
CACHE_HOURS   = getattr(config, "CONGRESSIONAL_CACHE_HOURS", 24)
LOOKBACK_DAYS = getattr(config, "CONGRESSIONAL_LOOKBACK_DAYS", 90)
MIN_TRADE_USD = getattr(config, "CONGRESSIONAL_MIN_TRADE_USD", 50_000)
SLEEP_BETWEEN = 0.50   # polite delay between API calls for the free endpoint


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            raw = CACHE_FILE.read_bytes().rstrip(b"\x00")
            return json.loads(raw)
        except Exception:
            pass
    return {}


def _save_cache(data: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _cache_valid(entry: dict) -> bool:
    ts = entry.get("fetched_at")
    if not ts:
        return False
    age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600
    return age_h < CACHE_HOURS


# ── Trade size parser ─────────────────────────────────────────────────────────

def _parse_trade_size(size_str: str) -> float:
    """
    Convert STOCK Act disclosure size range to a representative dollar value.
    Examples:
      "$15,001 - $50,000"   -> 32500   (midpoint)
      "$50,001 - $100,000"  -> 75000
      "> $1,000,000"        -> 1000000 (lower bound)
      "$100,000"            -> 100000
    """
    if not size_str:
        return 0.0
    cleaned = str(size_str).replace("$", "").replace(",", "").strip()
    if " - " in cleaned:
        parts = cleaned.split(" - ")
        try:
            lo = float(parts[0].strip())
            hi = float(parts[1].strip())
            return (lo + hi) / 2.0
        except ValueError:
            pass
    if cleaned.startswith(">"):
        try:
            return float(cleaned[1:].strip())
        except ValueError:
            pass
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


# ── Capitol Trades API fetch ──────────────────────────────────────────────────

def _fetch_congressional_trades(ticker: str) -> list:
    """
    Fetch congressional trades for a ticker via Capitol Trades API.
    Paginates until all results within LOOKBACK_DAYS are retrieved.

    Returns list of dicts: {date, type, amount_usd, chamber}
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).date()
    trades = []
    page   = 1

    while True:
        params = {"pageSize": 100, "page": page, "issuer": ticker}
        try:
            r = requests.get(_BASE_URL, params=params, headers=_HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 404:
                log.debug("Capitol Trades: ticker %s not found (404)", ticker)
            else:
                log.debug("Capitol Trades HTTP error for %s: %s", ticker, e)
            break
        except Exception as e:
            log.debug("Capitol Trades error for %s: %s", ticker, e)
            break

        records = data.get("data", [])
        if not records:
            break

        for rec in records:
            date_str = rec.get("txDate") or rec.get("date") or ""
            try:
                tx_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if tx_date < cutoff:
                continue

            tx_type = (rec.get("type") or "").lower()
            if tx_type not in ("buy", "purchase", "sell", "sale"):
                continue

            size_str = rec.get("size") or rec.get("amount") or ""
            amount   = _parse_trade_size(str(size_str))
            if amount == 0:
                raw_val = rec.get("value")
                if isinstance(raw_val, (int, float)):
                    amount = float(raw_val)

            politician = rec.get("politician") or {}
            chamber    = (
                politician.get("chamber")
                or politician.get("type")
                or rec.get("chamber")
                or "unknown"
            ).lower()

            trades.append({
                "date":       str(tx_date),
                "type":       "buy" if tx_type in ("buy", "purchase") else "sell",
                "amount_usd": amount,
                "chamber":    chamber,
            })

        meta        = data.get("paginationMeta") or data.get("meta") or {}
        total_pages = int(meta.get("totalPages") or meta.get("total_pages") or 1)
        if page >= total_pages:
            break
        page += 1
        time.sleep(SLEEP_BETWEEN)

    return trades


# ── Signal computation ────────────────────────────────────────────────────────

def _compute_congressional_signal(ticker: str) -> float:
    """
    Aggregate qualifying trades and return a signal in [-1, +1].

    Qualifying = amount >= MIN_TRADE_USD (default $50k).
    Scoring:
      +1.0  3+ net buy events
      +0.5  1-2 net buy events
       0.0  no qualifying trades, or perfectly mixed
      -0.5  net selling
    """
    trades = _fetch_congressional_trades(ticker)
    if not trades:
        return 0.0

    qualifying_buys  = 0
    qualifying_sells = 0

    for t in trades:
        if t["amount_usd"] < MIN_TRADE_USD:
            continue
        if t["type"] == "buy":
            qualifying_buys += 1
        else:
            qualifying_sells += 1

    net = qualifying_buys - qualifying_sells

    if qualifying_buys >= 3 and net > 0:
        signal = +1.0
    elif qualifying_buys >= 1 and net > 0:
        signal = +0.5
    elif qualifying_buys == 0 and qualifying_sells == 0:
        signal = 0.0
    elif net < 0:
        signal = -0.5
    else:
        signal = 0.0

    log.debug("  %s congressional: %d buys / %d sells -> signal=%.1f",
              ticker, qualifying_buys, qualifying_sells, signal)
    return signal


# ── Public API ────────────────────────────────────────────────────────────────

def get_congressional_signals(tickers: list) -> dict:
    """Returns {ticker: float} signal in [-1, +1]. Uses 24-hour disk cache."""
    cache      = _load_cache()
    now_iso    = datetime.now(timezone.utc).isoformat()
    results    = {}
    cache_hits = 0
    fresh      = 0

    for ticker in tickers:
        entry = cache.get(ticker, {})
        if _cache_valid(entry):
            results[ticker] = entry["signal"]
            cache_hits += 1
        else:
            signal           = _compute_congressional_signal(ticker)
            results[ticker]  = signal
            cache[ticker]    = {"signal": signal, "fetched_at": now_iso}
            fresh           += 1
            if fresh % 10 == 0:
                _save_cache(cache)   # incremental save to survive interruption
            time.sleep(SLEEP_BETWEEN)

    _save_cache(cache)
    log.info("Congressional signals: %d from cache, %d freshly fetched",
             cache_hits, fresh)
    return results


def run(tickers) -> dict:
    """
    Stage 2D: Congressional trading signal.

    Returns:
        {
            "congressional_signals": {ticker: float},
            "tickers_fetched": int,
            "status": "success" | "disabled" | "failed",
            "nonzero_signals": int,
        }
    """
    tickers_list = tickers if tickers is not None else []

    if not getattr(config, "CONGRESSIONAL_ENABLED", False):
        log.info("CONGRESSIONAL_ENABLED=False -- skipping congressional signals")
        return {
            "congressional_signals": {t: 0.0 for t in tickers_list},
            "tickers_fetched":       0,
            "status":                "disabled",
        }

    log.info(
        "Fetching congressional trades for %d tickers "
        "(STOCK Act, last %d days, $%dk+ threshold)...",
        len(tickers_list) if tickers_list else 0,
        LOOKBACK_DAYS,
        MIN_TRADE_USD // 1000,
    )
    try:
        signals = get_congressional_signals(tickers_list)
        nonzero = sum(1 for v in signals.values() if v != 0)
        return {
            "congressional_signals": signals,
            "tickers_fetched":       len(signals),
            "status":                "success",
            "nonzero_signals":       nonzero,
        }
    except Exception as e:
        log.error("Congressional run failed: %s", e)
        return {
            "congressional_signals": {t: 0.0 for t in tickers_list},
            "tickers_fetched":       0,
            "status":                "failed",
        }


# ── Quick test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.DEBUG, format="%(levelname)s %(message)s")
    config.CONGRESSIONAL_ENABLED = True

    TEST = ["AAPL", "MSFT", "NVDA", "JPM", "LMT", "RTX", "GD", "NOC"]
    print("\n=== Congressional Signal Test (STOCK Act, $50k+, last 90 days) ===")
    result = run(TEST)
    print("\nStatus  : " + result["status"])
    print("Non-zero: " + str(result.get("nonzero_signals", 0)) + "/" + str(len(TEST)))
    print("\nSignals:")
    for t, s in result["congressional_signals"].items():
        if s > 0:
            bar = "+" * int(abs(s) * 5)
        elif s < 0:
            bar = "-" * int(abs(s) * 5)
        else:
            bar = "."
        print("  " + t.ljust(8) + " " + "{:+.1f}".format(s) + "  " + bar)
    print("\nCongressional test complete")
