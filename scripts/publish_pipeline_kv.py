"""
scripts/publish_pipeline_kv.py — Publish pipeline state to Cloudflare KV.

REPLACES screener/regime_to_kv.py.

WHY THIS EXISTS
---------------
The Cloudflare Worker (Discord front door) reads three KV keys that only the
screener ever wrote:

    regime_signal    — gates the TradingView webhook, shown in /brief
    stock_buckets    — per-ticker stop/target percentages
    screener_summary — top picks + ATR targets for /buy previews

Deleting the screener without replacing them would silently degrade the
Worker: /brief loses its regime, and /buy previews lose their dynamic stop
and take-profit levels — falling back to nothing. That is the failure the
2026-07-27 audit warned about when it insisted retirement happen in order
(docs/FABLE_AUDIT_2026-07-27.md §4).

Everything here comes from the pipeline's own modules:
    regime        → pipeline/regime.py
    stops/targets → broker/stop_loss.compute_stop_price / compute_take_profit
    picks         → data/pipeline_run_latest.json (last run's ranked output)

KEY NAMING
----------
`regime_signal` and `stock_buckets` are neutral names and keep them. The
screener-branded `screener_summary` is written under the new key
`pipeline_summary`, AND under the old name during the transition so a Worker
that has not yet been redeployed keeps working. Drop the legacy write once
the Worker is confirmed reading the new key.

USAGE
-----
    python scripts/publish_pipeline_kv.py            # publish
    python scripts/publish_pipeline_kv.py --dry-run  # print, publish nothing
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

log = logging.getLogger("publish_kv")

CF_API = "https://api.cloudflare.com/client/v4"
LEGACY_SUMMARY_KEY = "screener_summary"   # remove once the Worker is redeployed


def _creds():
    a = os.getenv("CF_ACCOUNT_ID", "").strip()
    n = os.getenv("CF_KV_NAMESPACE", "").strip()
    t = os.getenv("CF_API_TOKEN", "").strip()
    return (a, n, t) if all([a, n, t]) else None


def _put(key: str, value: dict, dry_run: bool = False) -> bool:
    payload = json.dumps(value, default=str)
    if dry_run:
        log.info("[DRY] %s ← %d bytes", key, len(payload))
        print(f"\n--- {key} ---\n{json.dumps(value, indent=2, default=str)[:900]}")
        return True
    creds = _creds()
    if not creds:
        log.error("CF_ACCOUNT_ID / CF_KV_NAMESPACE / CF_API_TOKEN not set — cannot publish")
        return False
    account_id, namespace_id, token = creds
    url = f"{CF_API}/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}"
    try:
        r = requests.put(url, headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "text/plain"},
                         data=payload.encode("utf-8"), timeout=15)
        if r.status_code == 200:
            log.info("  ✅ %s published (%d bytes)", key, len(payload))
            return True
        log.error("  ❌ %s failed HTTP %s: %s", key, r.status_code, r.text[:200])
    except Exception as exc:
        log.error("  ❌ %s error: %s", key, exc)
    return False


def build_payloads() -> tuple[dict, dict, dict]:
    """Assemble the three KV payloads from pipeline sources."""
    from pipeline import regime as regime_module
    from broker.stop_loss import compute_stop_price, compute_take_profit

    reg = regime_module.run()
    label = str(reg.get("regime", "unknown")).upper()

    # Worker expects the screener's 4-tier vocabulary in `label`; map from the
    # pipeline's 3-tier so existing Worker logic keeps working unchanged.
    label_map = {"BULL": "MOD BULL", "NEUTRAL": "NEUTRAL", "BEAR": "BEARISH"}
    regime_data = {
        "label":         label_map.get(label, label),
        "pipeline_label": label,
        "total":         reg.get("spx_vs_200ma_pct"),
        "vix":           reg.get("vix_current"),
        "spx_vs_200ma_pct": reg.get("spx_vs_200ma_pct"),
        "notes":         reg.get("notes", ""),
        # Which strategies the Worker may permit. The pipeline is long-only
        # momentum/quality, so the permitted set follows the regime directly.
        "permitted_strategies": (["momentum", "quality", "defensive"] if label == "BULL"
                                 else ["quality", "defensive"] if label == "NEUTRAL"
                                 else ["defensive"]),
        "source":        "pipeline",
        "pushed_at":     datetime.now(timezone.utc).isoformat(),
    }

    # Last run's ranked output — the pipeline's equivalent of "top picks".
    summary_path = config.DATA_DIR / "pipeline_run_latest.json"
    picks_raw = []
    try:
        blob = json.loads(summary_path.read_bytes().rstrip(b"\x00"))
        picks_raw = blob.get("top_holdings") or []
    except Exception as exc:
        log.warning("Could not read %s (%s) — publishing regime only",
                    summary_path.name, exc)

    regime_key = label.lower()
    stock_buckets, top_picks = {}, []
    for s in picks_raw:
        ticker = s.get("ticker")
        price  = s.get("current_price") or 0
        score  = s.get("composite_score") or 0
        stop_pct = tp_pct = None
        if ticker and price:
            try:
                stop, _m, atr = compute_stop_price(ticker, price, regime_key)
                stop_pct = round((price - stop) / price * 100, 2)
                tp, _mon, _tm = compute_take_profit(ticker, price, regime_key, atr)
                if tp:
                    tp_pct = round((tp - price) / price * 100, 2)
            except Exception:
                pass
        entry = {
            "bucket":         "momentum",
            "score":          round(score * 100, 1),   # Worker expects a 0-100 scale
            "regime_ok":      True,
            "near_earnings":  False,
            "atr_pct":        None,
            "stop_pct":       stop_pct,
            "tp_monitor_pct": round(tp_pct * 0.8, 2) if tp_pct else None,
            "tp_alpaca_pct":  tp_pct,
        }
        stock_buckets[ticker] = entry
        top_picks.append({"ticker": ticker, **entry,
                          "conviction_ok": score >= 0.55})

    summary = {
        "date":                  datetime.now(timezone.utc).date().isoformat(),
        "regime_label":          regime_data["label"],
        "regime_score":          regime_data.get("total"),
        "permitted_strategies":  regime_data["permitted_strategies"],
        "top_picks":             sorted(top_picks, key=lambda x: -x["score"])[:5],
        "high_conviction_count": sum(1 for p in top_picks if p["conviction_ok"]),
        "total_scored":          len(stock_buckets),
        "source":                "pipeline",
    }
    return regime_data, stock_buckets, summary


def main():
    ap = argparse.ArgumentParser(description="Publish pipeline state to Cloudflare KV")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-legacy", action="store_true",
                    help="skip the legacy screener_summary write")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    regime_data, stock_buckets, summary = build_payloads()
    log.info("Regime %s · %d picks · %d scored",
             regime_data["label"], len(summary["top_picks"]), summary["total_scored"])

    ok = True
    ok &= _put("regime_signal",    regime_data,   args.dry_run)
    ok &= _put("stock_buckets",    stock_buckets, args.dry_run)
    ok &= _put("pipeline_summary", summary,       args.dry_run)
    if not args.no_legacy:
        # Transitional: keeps a not-yet-redeployed Worker working.
        ok &= _put(LEGACY_SUMMARY_KEY, summary, args.dry_run)

    if not ok:
        sys.exit(1)
    log.info("Published from the PIPELINE — screener/regime_to_kv.py is no longer needed.")


if __name__ == "__main__":
    main()
