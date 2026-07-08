"""
scripts/fetch_congressional_trades.py — Congressional trading signal fetcher

Pulls recent congressional stock purchases from Quiver Quantitative's free API
and writes data/congressional_cache.json for use by the screener's Phase 2b signal.

SETUP:
  1. Register at https://www.quiverquant.com/ (free account)
  2. Get your API key from the dashboard
  3. Add to GitHub Secrets:  QUIVER_API_KEY = <your key>
  4. Add to .env for local runs: QUIVER_API_KEY=<your key>

SCHEDULE: Run daily (already wired into screener_daily.yml).

OUTPUT FORMAT (data/congressional_cache.json):
  {
    "AAPL": {
      "recent_buys": 3,
      "last_buy_date": "2025-06-20",
      "buyers": ["Nancy Pelosi", "Dan Crenshaw"],
      "fetched_at": "2025-07-08T08:00:00"
    },
    ...
  }
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

# ── Paths & Config ────────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).parent.parent
DATA_DIR    = BASE_DIR / "data"
OUTPUT_FILE = DATA_DIR / "congressional_cache.json"

try:
    sys.path.insert(0, str(BASE_DIR))
    import config
    LOOKBACK_DAYS = getattr(config, "CONGRESS_BUY_LOOKBACK_DAYS", 60)
    ENABLED       = getattr(config, "CONGRESS_SIGNAL_ENABLED", True)
except Exception:
    LOOKBACK_DAYS = 60
    ENABLED       = True

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

QUIVER_KEY = os.getenv("QUIVER_API_KEY", "").strip()

# ── HTTP Session ──────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"Token {QUIVER_KEY}",
    "Accept":        "application/json",
})
retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
SESSION.mount("https://", HTTPAdapter(max_retries=retry))

QUIVER_BASE = "https://api.quiverquant.com/beta"


def _load_existing() -> dict:
    """Load existing cache to preserve any hand-edited entries."""
    if OUTPUT_FILE.exists():
        try:
            return json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def fetch_congressional_trades() -> dict:
    """
    Fetch bulk congressional trades from Quiver Quant.
    Returns raw list of trade dicts.
    """
    print("  → Fetching congressional trades from Quiver Quantitative...")
    try:
        r = SESSION.get(f"{QUIVER_BASE}/bulk/congresstrading", timeout=20)
        if r.status_code == 401:
            print("  ❌ QUIVER_API_KEY invalid or not set — skipping congressional signal")
            return []
        if r.status_code == 402:
            print("  ⚠  Quiver Quant free tier limit reached — using cached data")
            return []
        r.raise_for_status()
        trades = r.json()
        print(f"  ✓ Fetched {len(trades)} congressional trade records")
        return trades
    except Exception as e:
        print(f"  ⚠  Congressional fetch failed: {e}")
        return []


def build_cache(trades: list, lookback_days: int) -> dict:
    """
    Filter to BUY transactions within lookback window and aggregate by ticker.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date()
    cache  = {}

    for trade in trades:
        # Quiver Quant field names
        ticker      = (trade.get("Ticker") or trade.get("ticker") or "").upper().strip()
        transaction = (trade.get("Transaction") or trade.get("transaction") or "").lower()
        date_str    = trade.get("Date") or trade.get("date") or ""
        rep         = trade.get("Representative") or trade.get("representative") or "Unknown"

        if not ticker or not date_str:
            continue

        # Only count purchases (not sales or exchanges)
        if "purchase" not in transaction and "buy" not in transaction:
            continue

        try:
            trade_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue

        if trade_date < cutoff:
            continue

        if ticker not in cache:
            cache[ticker] = {
                "recent_buys":   0,
                "last_buy_date": date_str[:10],
                "buyers":        [],
                "fetched_at":    datetime.now(timezone.utc).isoformat(),
            }

        cache[ticker]["recent_buys"] += 1
        if date_str[:10] > cache[ticker]["last_buy_date"]:
            cache[ticker]["last_buy_date"] = date_str[:10]
        if rep not in cache[ticker]["buyers"]:
            cache[ticker]["buyers"].append(rep)

    # Sort buyers list for cleaner diffs
    for v in cache.values():
        v["buyers"] = sorted(v["buyers"])

    return cache


def run():
    if not ENABLED:
        print("Congressional signal disabled (CONGRESS_SIGNAL_ENABLED=False) — skipping")
        return

    if not QUIVER_KEY:
        print("⚠  QUIVER_API_KEY not set — congressional cache will remain empty")
        print("   Register free at https://www.quiverquant.com/ and add QUIVER_API_KEY to GitHub Secrets")
        return

    trades = fetch_congressional_trades()
    if not trades:
        print("  No trades returned — keeping existing cache")
        return

    cache = build_cache(trades, LOOKBACK_DAYS)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")

    buys = [(t, d["recent_buys"]) for t, d in cache.items()]
    buys.sort(key=lambda x: -x[1])
    print(f"\n✅ Congressional cache updated — {len(cache)} tickers with recent buys (last {LOOKBACK_DAYS}d):")
    for ticker, count in buys[:10]:
        buyers = cache[ticker]["buyers"]
        names  = ", ".join(buyers[:2]) + ("…" if len(buyers) > 2 else "")
        print(f"   {ticker:<6} {count} buy(s) — {names}")
    if len(buys) > 10:
        print(f"   … and {len(buys)-10} more")


if __name__ == "__main__":
    run()
