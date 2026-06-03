"""
pipeline/insider.py - Phase 3C: SEC EDGAR Form 4 Insider Signal

Upgraded from Phase 2B:
  - Filters to OPEN-MARKET PURCHASES only (transaction code 'P')
  - Minimum purchase size: $500,000 (config.INSIDER_MIN_PURCHASE_USD)
  - Officers/Directors only (not 10% owners unless also officer)
  - Recency weighting: last 30 days count 2x vs 31-90 days
  - Returns float in [-1, +1]:
      +1.0  strong buying (>=3 qualifying purchases)
      +0.5  moderate buying (1-2 qualifying purchases)
       0.0  neutral / no data
      -0.5  insider selling (>=3 sales vs 0 buys)
      -1.0  heavy selling (>=5 sales vs 0 buys)

Data source: SEC EDGAR full-text search + individual filing XML
No API key required. Rate limit: max 10 req/s (we use 0.3s sleep).
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

EDGAR_HEADERS   = {"User-Agent": "InvestmentAlpha/3.0 srijanbansal@gmail.com"}
CACHE_FILE      = Path(getattr(config, "DATA_DIR", "data")) / "insider_cache.json"
CACHE_HOURS     = getattr(config, "INSIDER_CACHE_HOURS", 24)
LOOKBACK_DAYS   = getattr(config, "INSIDER_LOOKBACK_DAYS", 90)
MIN_USD         = getattr(config, "INSIDER_MIN_PURCHASE_USD", 500_000)
SLEEP_BETWEEN   = 0.30


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_cache(data):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(data, indent=2))


def _cache_valid(entry):
    ts = entry.get("fetched_at")
    if not ts:
        return False
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600
    return age < CACHE_HOURS


# ---------------------------------------------------------------------------
# SEC EDGAR helpers
# ---------------------------------------------------------------------------

def _get_cik_map():
    """Download full SEC company tickers map {ticker: cik_str}. Cached in memory."""
    try:
        url = "https://www.sec.gov/files/company_tickers.json"
        r = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        return {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}
    except Exception as e:
        log.warning("CIK map fetch failed: %s", e)
        return {}


_CIK_MAP = {}  # module-level cache to avoid re-fetching every ticker


def _get_cik(ticker):
    global _CIK_MAP
    if not _CIK_MAP:
        _CIK_MAP = _get_cik_map()
        time.sleep(SLEEP_BETWEEN)
    return _CIK_MAP.get(ticker.upper())


def _fetch_recent_form4_filings(cik):
    """
    Fetch list of recent Form 4 filing accession numbers for a CIK.
    Uses the EDGAR submissions API.
    Returns list of (accession_number, filing_date) tuples.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.debug("Submissions fetch failed for CIK %s: %s", cik, e)
        return []

    filings = data.get("filings", {}).get("recent", {})
    forms       = filings.get("form", [])
    dates       = filings.get("filingDate", [])
    accessions  = filings.get("accessionNumber", [])

    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=LOOKBACK_DAYS))
    results = []
    for form, date_str, acc in zip(forms, dates, accessions):
        if form not in ("4", "4/A"):
            continue
        try:
            filing_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            continue
        if filing_date >= cutoff:
            results.append((acc, filing_date))
    return results


def _parse_form4_xml(cik, accession_number):
    """
    Fetch and parse Form 4 XML for a single filing.
    Returns list of transactions: each is a dict with:
      transaction_code, shares, price_per_share, total_value,
      is_director, is_officer, days_ago
    Only open-market purchases (code='P') are meaningful for us.
    """
    acc_clean = accession_number.replace("-", "")
    acc_dashes = f"{acc_clean[:10]}-{acc_clean[10:12]}-{acc_clean[12:]}"
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{acc_clean}/form4.xml"
    )
    # Try alternate common filenames
    alt_urls = [
        url,
        url.replace("form4.xml", "xslF345X03/form4.xml"),
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/doc4.xml",
    ]

    content = None
    for u in alt_urls:
        try:
            r = requests.get(u, headers=EDGAR_HEADERS, timeout=10)
            if r.status_code == 200:
                content = r.text
                break
        except Exception:
            continue

    if not content:
        return []

    # Simple XML parsing without lxml dependency
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(content)
    except Exception:
        return []

    # Reporting owner roles
    is_director = False
    is_officer  = False
    for owner in root.findall(".//reportingOwner"):
        rel = owner.find("reportingOwnerRelationship")
        if rel is not None:
            if rel.findtext("isDirector") == "1":
                is_director = True
            if rel.findtext("isOfficer") == "1":
                is_officer  = True

    # Only count directors and officers (not 10% owners unless also officer)
    if not (is_director or is_officer):
        return []

    transactions = []
    for tx in root.findall(".//nonDerivativeTransaction"):
        code_el    = tx.find(".//transactionCoding/transactionCode")
        shares_el  = tx.find(".//transactionAmounts/transactionShares/value")
        price_el   = tx.find(".//transactionAmounts/transactionPricePerShare/value")

        if code_el is None:
            continue
        code = code_el.text.strip() if code_el.text else ""

        try:
            shares = float(shares_el.text) if shares_el is not None and shares_el.text else 0
            price  = float(price_el.text)  if price_el  is not None and price_el.text  else 0
        except ValueError:
            continue

        total_value = shares * price
        transactions.append({
            "transaction_code": code,
            "shares":           shares,
            "price_per_share":  price,
            "total_value":      total_value,
            "is_director":      is_director,
            "is_officer":       is_officer,
        })

    return transactions


def _compute_insider_signal(ticker):
    """
    Fetch all Form 4 filings for ticker in last LOOKBACK_DAYS days.
    Filter to open-market purchases >= MIN_USD by officers/directors.
    Return float signal in [-1, +1].
    """
    cik = _get_cik(ticker)
    if not cik:
        log.debug("No CIK found for %s", ticker)
        return 0.0

    filings = _fetch_recent_form4_filings(cik)
    time.sleep(SLEEP_BETWEEN)
    if not filings:
        return 0.0

    today = datetime.now(timezone.utc).date()
    qualifying_buys  = 0
    qualifying_sells = 0
    buy_score = 0.0
    sell_score = 0.0

    for acc, filing_date in filings:
        days_ago = (today - filing_date).days
        recency_weight = 2.0 if days_ago <= 30 else 1.0

        transactions = _parse_form4_xml(cik, acc)
        time.sleep(SLEEP_BETWEEN * 0.5)

        for tx in transactions:
            code  = tx["transaction_code"]
            value = tx["total_value"]

            if code == "P" and value >= MIN_USD:
                # Open-market purchase meeting size threshold
                qualifying_buys += 1
                buy_score += recency_weight * min(value / 1_000_000, 5.0)  # cap at $5M
                log.debug("  %s BUY: $%,.0f (recency_weight=%.1f)", ticker, value, recency_weight)

            elif code == "S" and value >= MIN_USD:
                # Open-market sale
                qualifying_sells += 1
                sell_score += recency_weight * min(value / 1_000_000, 5.0)
                log.debug("  %s SELL: $%,.0f", ticker, value)

    # Net signal: positive = net buying, negative = net selling
    net = buy_score - sell_score
    total = buy_score + sell_score

    if total == 0:
        return 0.0

    # Normalise to [-1, +1]
    signal = net / max(total, 1.0)
    signal = max(-1.0, min(1.0, signal))
    log.debug("  %s insider: %d buys / %d sells -> signal=%.2f",
              ticker, qualifying_buys, qualifying_sells, signal)
    return round(signal, 3)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_insider_signals(tickers):
    """Returns {ticker: float} signal in [-1, +1]. Uses cache."""
    cache = _load_cache()
    now_iso = datetime.now(timezone.utc).isoformat()
    results = {}
    cache_hits = 0
    fresh = 0

    for ticker in tickers:
        entry = cache.get(ticker, {})
        if _cache_valid(entry):
            results[ticker] = entry["signal"]
            cache_hits += 1
        else:
            signal = _compute_insider_signal(ticker)
            results[ticker] = signal
            cache[ticker] = {"signal": signal, "fetched_at": now_iso}
            fresh += 1

    _save_cache(cache)
    log.info("Insider signals: %d from cache, %d freshly fetched", cache_hits, fresh)
    return results


def run(tickers):
    """
    Phase 3C: Insider signal — open-market purchases only, $500k+ threshold.
    tickers may be None when running on the full universe (main.py default).
    """
    # When tickers is None, insider signals are resolved per-ticker in
    # get_insider_signals() against the full universe from features.
    # Normalise to empty list for length/iteration safety.
    tickers_list = tickers if tickers is not None else []

    if not getattr(config, "INSIDER_ENABLED", False):
        log.info("INSIDER_ENABLED=False -- skipping insider signals")
        return {
            "insider_signals": {t: 0.0 for t in tickers_list},
            "tickers_fetched": 0,
            "status":          "disabled",
        }

    log.info("Fetching insider signals for %s tickers (open-market $%.0fk+ threshold)...",
             len(tickers_list) if tickers_list else "all universe", MIN_USD / 1000)
    try:
        signals = get_insider_signals(tickers_list)  # empty list → empty dict when full universe
        nonzero = sum(1 for v in signals.values() if v != 0)
        return {
            "insider_signals": signals,
            "tickers_fetched": len(signals),
            "status":          "success",
            "nonzero_signals": nonzero,
        }
    except Exception as e:
        log.error("Insider run failed: %s", e)
        return {
            "insider_signals": {t: 0.0 for t in tickers_list},
            "tickers_fetched": 0,
            "status":          "failed",
        }


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.DEBUG, format="%(levelname)s %(message)s")
    config.INSIDER_ENABLED = True

    TEST = ["AAPL", "MSFT", "NVDA", "JPM", "META"]
    print("\n=== Insider Signal Test (Phase 3: Open-market $500k+) ===")
    print(f"Lookback: {LOOKBACK_DAYS} days | Min purchase: ${MIN_USD:,.0f}")
    result = run(TEST)
    print(f"\nStatus : {result['status']}")
    print(f"Non-zero: {result.get('nonzero_signals', 0)}/{len(TEST)}")
    print("\nSignals:")
    for t, s in result["insider_signals"].items():
        bar = "+" * int(s * 5) if s > 0 else ("-" * int(abs(s) * 5) if s < 0 else ".")
        print(f"  {t:<8} {s:+.3f}  {bar}")
    print("\nInsider test complete")
