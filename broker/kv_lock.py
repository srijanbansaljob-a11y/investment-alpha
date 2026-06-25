"""
broker/kv_lock.py — Lightweight Cloudflare KV execution lock

Prevents two executors (GitHub Actions cloud + local run_weekly_execute.bat)
from submitting orders to the same Alpaca account simultaneously.

The lock is a KV key:  execution_lock
Value: JSON with who set it and when.
TTL:   configurable, defaults to 3600s (1 hour) so stale locks self-clear.

Usage:
    from broker.kv_lock import acquire_lock, release_lock, check_lock

    if not acquire_lock("execution_lock", owner="weekly_rebalance"):
        sys.exit("Execution already in progress — try again later")
    try:
        ... run orders ...
    finally:
        release_lock("execution_lock")

Requires env vars (same as regime_to_kv.py):
    CF_ACCOUNT_ID, CF_KV_NAMESPACE, CF_API_TOKEN

If any of these are missing, all functions are no-ops (lock is skipped
gracefully — avoids breaking local runs that have no CF credentials).
"""

import json
import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

CF_API = "https://api.cloudflare.com/client/v4"
LOCK_TTL = 3600  # 1 hour — stale locks auto-expire


def _cf_creds() -> tuple[str, str, str] | None:
    """Return (account_id, namespace_id, token) or None if not configured."""
    a = os.getenv("CF_ACCOUNT_ID", "").strip()
    n = os.getenv("CF_KV_NAMESPACE", "").strip()
    t = os.getenv("CF_API_TOKEN", "").strip()
    if not all([a, n, t]):
        return None
    return a, n, t


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "text/plain"}


def check_lock(key: str = "execution_lock") -> dict | None:
    """
    Return the lock value dict if the lock is held, or None if free/unavailable.
    Dict has keys: owner, acquired_at.
    """
    creds = _cf_creds()
    if not creds:
        return None
    account_id, namespace_id, token = creds
    url = f"{CF_API}/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}"
    try:
        r = requests.get(url, headers=_headers(token), timeout=8)
        if r.status_code == 200:
            return json.loads(r.text)
        if r.status_code == 404:
            return None  # not locked
        log.warning("KV lock check failed HTTP %s", r.status_code)
        return None
    except Exception as e:
        log.warning("KV lock check error: %s", e)
        return None


def acquire_lock(key: str = "execution_lock", owner: str = "executor",
                 ttl: int = LOCK_TTL) -> bool:
    """
    Try to acquire the lock. Returns True if acquired, False if already held.
    Uses a read-then-write pattern (not atomic, but sufficient for single-owner use).
    """
    creds = _cf_creds()
    if not creds:
        log.debug("CF creds not configured — skipping lock (no-op)")
        return True  # no credentials = no lock enforcement; proceed
    account_id, namespace_id, token = creds

    existing = check_lock(key)
    if existing:
        log.warning(
            "Execution lock held by '%s' since %s — refusing to proceed.",
            existing.get("owner", "?"), existing.get("acquired_at", "?"),
        )
        return False

    value = json.dumps({
        "owner":       owner,
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    })
    url = f"{CF_API}/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}"
    try:
        r = requests.put(
            url, headers=_headers(token),
            params={"expiration_ttl": ttl},
            data=value, timeout=8,
        )
        if r.status_code in (200, 201):
            log.info("Execution lock acquired (owner=%s, ttl=%ds)", owner, ttl)
            return True
        log.warning("Lock acquire failed HTTP %s: %s", r.status_code, r.text[:200])
        return True  # KV failure → allow execution rather than permanently blocking
    except Exception as e:
        log.warning("Lock acquire error: %s — proceeding anyway", e)
        return True  # network failure → allow execution


def release_lock(key: str = "execution_lock") -> None:
    """Release the lock. Safe to call even if not held."""
    creds = _cf_creds()
    if not creds:
        return
    account_id, namespace_id, token = creds
    url = f"{CF_API}/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}"
    try:
        r = requests.delete(url, headers=_headers(token), timeout=8)
        if r.status_code in (200, 204):
            log.info("Execution lock released")
        else:
            log.warning("Lock release HTTP %s", r.status_code)
    except Exception as e:
        log.warning("Lock release error: %s", e)
