"""
screener/regime_to_kv.py — Push regime signal to Cloudflare KV

Run at the end of the daily screener workflow so the Cloudflare Worker
can gate TradingView webhooks based on today's regime.

Also writes a stock_buckets snapshot so the Worker can validate that
a TradingView signal matches the stock's permitted strategy bucket.

Usage:
    python screener/regime_to_kv.py --regime-file screener/outputs/regime_today.json

Env vars (GitHub Secrets):
    CF_ACCOUNT_ID   — Cloudflare account ID
    CF_KV_NAMESPACE — KV namespace ID (investment-alpha-kv)
    CF_API_TOKEN    — Cloudflare API token with Workers KV Edit permission
"""

import os
import sys
import json
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

CF_API = "https://api.cloudflare.com/client/v4"


def kv_put(account_id: str, namespace_id: str, token: str, key: str, value: str,
           ttl_seconds: int = 90000) -> bool:
    """Write a key to Cloudflare KV. TTL ~25h (one trading day + buffer)."""
    url = f"{CF_API}/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "text/plain"}
    try:
        r = requests.put(url, headers=headers, params={"expiration_ttl": ttl_seconds},
                         data=value, timeout=10)
        if r.status_code in (200, 201):
            log.info("KV  OK  %s (%d chars)", key, len(value))
            return True
        log.error("KV FAIL  %s => HTTP %s: %s", key, r.status_code, r.text[:200])
        return False
    except Exception as exc:
        log.error("KV FAIL  %s => %s", key, exc)
        return False


def parse_screener_output(output_file: Path) -> tuple:
    """
    Parse daily_sentiment_runner.py JSON output.
    Returns (regime_data, stock_buckets, screener_summary).
    """
    with open(output_file) as f:
        data = json.load(f)

    regime_data = data.get("macro_score", {})
    regime_data["pushed_at"] = datetime.now(timezone.utc).isoformat()

    permitted = set(regime_data.get("permitted_strategies", []))
    stock_buckets = {}
    top_picks = []

    for stock in data.get("stocks", []):
        ticker = stock.get("ticker", "")
        bucket = stock.get("strategy_bucket", "watch")
        score = stock.get("total_score", 0)
        regime_ok = bucket in permitted or bucket == "defensive"
        near_earnings = stock.get("near_earnings", False)
        stock_buckets[ticker] = {
            "bucket": bucket,
            "score": score,
            "regime_ok": regime_ok,
            "near_earnings": near_earnings,
        }
        if regime_ok and score >= 55 and not near_earnings:
            top_picks.append({"ticker": ticker, "score": score, "bucket": bucket})

    screener_summary = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "regime_label": regime_data.get("label", "UNKNOWN"),
        "regime_score": regime_data.get("total", 0),
        "permitted_strategies": list(permitted),
        "top_picks": sorted(top_picks, key=lambda x: -x["score"])[:10],
        "total_scored": len(stock_buckets),
    }

    return regime_data, stock_buckets, screener_summary


def run(args):
    account_id   = os.getenv("CF_ACCOUNT_ID", "").strip()
    namespace_id = os.getenv("CF_KV_NAMESPACE", "").strip()
    token        = os.getenv("CF_API_TOKEN", "").strip()

    if not all([account_id, namespace_id, token]):
        log.error("Missing CF_ACCOUNT_ID / CF_KV_NAMESPACE / CF_API_TOKEN")
        log.error("Add these as GitHub Secrets and to your .env for local runs.")
        log.error("CF_ACCOUNT_ID : Cloudflare dashboard -> right sidebar")
        log.error("CF_KV_NAMESPACE: Workers & Pages -> KV -> namespace ID")
        log.error("CF_API_TOKEN  : Cloudflare -> My Profile -> API Tokens -> Create Token")
        sys.exit(1)

    output_file = Path(args.regime_file)
    if not output_file.exists():
        log.error("Screener output not found: %s", output_file)
        log.error("Run: python screener/daily_sentiment_runner.py --save-json")
        sys.exit(1)

    log.info("Parsing: %s", output_file)
    regime_data, stock_buckets, screener_summary = parse_screener_output(output_file)

    log.info("Regime : %s (score=%s)", regime_data.get("label"), regime_data.get("total"))
    log.info("Permitted strategies : %s", regime_data.get("permitted_strategies"))
    log.info("Stock bucket map     : %d tickers", len(stock_buckets))
    log.info("Top picks today      : %d", len(screener_summary["top_picks"]))

    ok1 = kv_put(account_id, namespace_id, token, "regime_signal", json.dumps(regime_data))
    ok2 = kv_put(account_id, namespace_id, token, "stock_buckets", json.dumps(stock_buckets))
    ok3 = kv_put(account_id, namespace_id, token, "screener_summary", json.dumps(screener_summary))

    if ok1 and ok2 and ok3:
        log.info("All 3 KV keys updated — Worker ready to gate today's orders")
    else:
        log.error("Some KV writes failed — Worker may use stale data")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Push screener regime to Cloudflare KV")
    parser.add_argument(
        "--regime-file",
        default="screener/outputs/daily_sentiment_data.json",
        help="Path to daily_sentiment_runner.py JSON output",
    )
    run(parser.parse_args())
