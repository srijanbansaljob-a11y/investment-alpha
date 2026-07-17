"""
=============================================================================
  NIGHTLY UPDATER
  Run after market close each day (4:30–5:00 PM ET recommended).
  Reads trade_log.csv and fills in forward prices for open signals.

  For each logged signal it checks:
    - 1 trading day after entry  → fills price_1d, return_1d
    - 3 trading days after entry → fills price_3d, return_3d
    - 5 trading days after entry → fills price_5d, return_5d, outcome_3d

  outcome_3d: "WIN" if return_3d > +1%, "LOSS" if < -1%, else "FLAT"

USAGE:
  python nightly_updater.py          # Update all open signals
  python nightly_updater.py --dry    # Print what would change, don't write
=============================================================================
"""

import csv
import os
import sys
import time
import argparse
from datetime import datetime, date, timedelta

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG  = os.path.join(OUTPUT_DIR, "trade_log.csv")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
}

FIELDNAMES = [
    "date", "ticker", "conviction", "total_score",
    "analyst_pts", "momentum_pts", "news_pts", "macro_pts", "valuation_pts",
    "entry_price", "regime_label", "regime_score", "strategy_bucket",
    "near_earnings", "days_to_earnings", "vol_ratio", "upside_pct", "beta",
    "adx", "spy_pct_from_200ma", "breadth_pct",
    "price_1d", "price_3d", "price_5d",
    "return_1d", "return_3d", "return_5d", "outcome_3d",
]


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session

SESSION = make_session()


def get_current_price(ticker: str) -> float | None:
    """Fetch current/last price for a ticker via Yahoo Finance."""
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}"
    try:
        r      = SESSION.get(url, timeout=8)
        quotes = r.json().get("quoteResponse", {}).get("result", [])
        if quotes:
            return quotes[0].get("regularMarketPrice")
    except Exception as e:
        print(f"  ⚠  Price fetch failed for {ticker}: {e}")
    return None


def trading_days_since(entry_date_str: str) -> int:
    """
    Approximate trading days elapsed since entry date.
    Counts Mon–Fri only. Does not account for public holidays
    (close enough for our purposes).
    """
    entry = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
    today = date.today()
    if today <= entry:
        return 0
    days = 0
    current = entry + timedelta(days=1)
    while current <= today:
        if current.weekday() < 5:   # Mon–Fri
            days += 1
        current += timedelta(days=1)
    return days


def compute_return(entry_price_str: str, current_price: float) -> float | None:
    """Return % change from entry to current price."""
    try:
        entry = float(entry_price_str)
        if entry > 0:
            return round((current_price - entry) / entry * 100, 2)
    except (ValueError, TypeError):
        pass
    return None


def classify_outcome(return_pct: float | None) -> str:
    if return_pct is None:
        return ""
    if return_pct > 1.0:
        return "WIN"
    elif return_pct < -1.0:
        return "LOSS"
    return "FLAT"


def run_update(dry_run: bool = False):
    if not os.path.exists(TRADE_LOG):
        print(f"trade_log.csv not found at {TRADE_LOG}")
        print("Run daily_sentiment_runner.py first to generate signals.")
        return

    with open(TRADE_LOG, newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("trade_log.csv is empty — nothing to update.")
        return

    today_str = date.today().isoformat()
    updated   = 0
    skipped   = 0

    print(f"\n{'='*55}")
    print(f"  📊 NIGHTLY UPDATER  —  {today_str}")
    print(f"{'='*55}")
    print(f"  Rows in log: {len(rows)}\n")

    for row in rows:
        ticker      = row.get("ticker", "")
        entry_date  = row.get("date", "")
        entry_price = row.get("entry_price", "")

        if not ticker or not entry_date or not entry_price:
            skipped += 1
            continue

        td = trading_days_since(entry_date)

        # Check which slots still need filling
        needs_1d = td >= 1 and not row.get("price_1d")
        needs_3d = td >= 3 and not row.get("price_3d")
        needs_5d = td >= 5 and not row.get("price_5d")

        if not (needs_1d or needs_3d or needs_5d):
            skipped += 1
            continue

        # Fetch price once per ticker
        price = get_current_price(ticker)
        if price is None:
            print(f"  ✗ {ticker:<6} ({entry_date}) — price unavailable")
            skipped += 1
            time.sleep(0.2)
            continue

        ret_pct = compute_return(entry_price, price)
        label   = ""

        if needs_1d:
            row["price_1d"]  = round(price, 2)
            row["return_1d"] = ret_pct
            label += "1d "

        if needs_3d:
            row["price_3d"]  = round(price, 2)
            row["return_3d"] = ret_pct
            label += "3d "

        if needs_5d:
            row["price_5d"]  = round(price, 2)
            row["return_5d"] = ret_pct
            row["outcome_3d"] = classify_outcome(row.get("return_3d") or ret_pct)
            label += "5d+outcome "

        direction = "▲" if (ret_pct or 0) > 0 else "▼"
        outcome   = row.get("outcome_3d") or ""
        outcome_tag = f"  → {outcome}" if outcome else ""

        print(f"  {direction} {ticker:<6} ({entry_date}) "
              f"entry ${entry_price} → now ${price:.2f} "
              f"({ret_pct:+.2f}%)  [{label.strip()}]{outcome_tag}")

        updated += 1
        time.sleep(0.18)

    if dry_run:
        print(f"\n  ⚡ DRY RUN — {updated} rows would be updated, {skipped} skipped")
        return

    # Write updated rows back
    with open(TRADE_LOG, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n  ✅ Updated {updated} rows | Skipped {skipped} rows")
    print(f"  File: {TRADE_LOG}")

    # Quick win/loss summary
    outcomes = [r.get("outcome_3d") for r in rows if r.get("outcome_3d")]
    if outcomes:
        wins   = outcomes.count("WIN")
        losses = outcomes.count("LOSS")
        flat   = outcomes.count("FLAT")
        total  = len(outcomes)
        wr     = wins / total * 100 if total else 0
        print(f"\n  📊 OUTCOMES SO FAR ({total} resolved)")
        print(f"     WIN: {wins}  LOSS: {losses}  FLAT: {flat}  "
              f"Win Rate: {wr:.1f}%")

    print(f"{'='*55}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nightly trade log price updater")
    parser.add_argument("--dry", action="store_true",
                        help="Show what would be updated without writing")
    args = parser.parse_args()
    run_update(dry_run=args.dry)
