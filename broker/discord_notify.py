"""
broker/discord_notify.py — Discord Bot API helper

Posts messages WITH interactive components (Approve/Reject buttons) via the
bot token. Plain webhooks cannot carry buttons — only bot/application
messages can — which is why this module exists alongside the webhook
alerts in monitor.py.

Also provides:
  - recent_alert_exists()  — stateless dedupe by reading channel history
                             (replaces pending_actions.json, which doesn't
                             persist between GitHub Actions runs)
  - edit_message()         — strip buttons / append result after a decision
  - interaction follow-up  — answer deferred slash commands

Env vars required (GitHub Secrets in the cloud, .env locally):
  DISCORD_BOT_TOKEN   — from the Discord Developer Portal (Bot tab)
  DISCORD_CHANNEL_ID  — right-click your alerts channel → Copy Channel ID
"""

import os
import json
import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"

BOT_TOKEN  = os.getenv("DISCORD_BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "").strip()


def _headers() -> dict:
    return {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json",
    }


def bot_configured() -> bool:
    """True when bot token + channel are available (buttons possible)."""
    return bool(BOT_TOKEN and CHANNEL_ID)


# ── Button builders ────────────────────────────────────────────────────────
# custom_id format: ia|<action>|<ticker>|<trigger>
# Kept under Discord's 100-char custom_id limit.

def approval_buttons(ticker: str, trigger: str) -> list:
    """Approve-sell / Reject button row for a flagged trade."""
    return [{
        "type": 1,  # action row
        "components": [
            {
                "type": 2, "style": 3,  # green
                "label": f"✅ Approve SELL {ticker}",
                "custom_id": f"ia|approve_sell|{ticker}|{trigger}",
            },
            {
                "type": 2, "style": 4,  # red
                "label": "❌ Reject (keep position)",
                "custom_id": f"ia|reject|{ticker}|{trigger}",
            },
        ],
    }]


# ── Messaging ──────────────────────────────────────────────────────────────

def post_message(embeds: list, components: list | None = None) -> dict | None:
    """POST a bot message to the alerts channel. Returns message dict or None."""
    if not bot_configured():
        log.warning("Bot token/channel not configured — cannot post bot message")
        return None
    payload = {"embeds": embeds}
    if components:
        payload["components"] = components
    try:
        r = requests.post(
            f"{API}/channels/{CHANNEL_ID}/messages",
            headers=_headers(), json=payload, timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.error("Discord bot post failed: %s", exc)
        return None


def edit_message(message_id: str, embeds: list | None = None,
                 components: list | None = None, channel_id: str | None = None) -> bool:
    """PATCH an existing bot message (e.g. remove buttons after a decision)."""
    cid = channel_id or CHANNEL_ID
    payload = {}
    if embeds is not None:
        payload["embeds"] = embeds
    if components is not None:
        payload["components"] = components
    try:
        r = requests.patch(
            f"{API}/channels/{cid}/messages/{message_id}",
            headers=_headers(), json=payload, timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        log.error("Discord edit_message failed: %s", exc)
        return False


def get_message(message_id: str, channel_id: str | None = None) -> dict | None:
    cid = channel_id or CHANNEL_ID
    try:
        r = requests.get(
            f"{API}/channels/{cid}/messages/{message_id}",
            headers=_headers(), timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.error("Discord get_message failed: %s", exc)
        return None


# ── Stateless dedupe (channel history = the state) ────────────────────────

def recent_alert_exists(ticker: str, trigger: str, limit: int = 50) -> bool:
    """
    True if an alert for this ticker+trigger was already posted TODAY (UTC).
    Reads recent channel messages — works across independent GitHub Actions
    runs without any shared file.
    """
    if not bot_configured():
        return False
    try:
        r = requests.get(
            f"{API}/channels/{CHANNEL_ID}/messages",
            headers=_headers(), params={"limit": limit}, timeout=10,
        )
        r.raise_for_status()
        today = datetime.now(timezone.utc).date().isoformat()
        marker = f"ia|approve_sell|{ticker}|{trigger}"
        for msg in r.json():
            if not msg.get("timestamp", "").startswith(today):
                continue
            # Match on the button custom_id (most precise) …
            for row in msg.get("components", []):
                for c in row.get("components", []):
                    if c.get("custom_id", "").startswith(f"ia|approve_sell|{ticker}|"):
                        return True
            # … or on embed title for already-decided (button-less) alerts
            for e in msg.get("embeds", []):
                t = e.get("title", "")
                if ticker in t and trigger.replace("_", " ").upper()[:9] in t.upper():
                    return True
        return False
    except Exception as exc:
        log.warning("Dedupe check failed (alert may duplicate): %s", exc)
        return False


# ── Interaction follow-ups (deferred slash commands) ───────────────────────

def edit_interaction_response(application_id: str, interaction_token: str,
                              embeds: list, components: list | None = None) -> bool:
    """Replace the 'thinking…' placeholder of a deferred slash command."""
    payload = {"embeds": embeds}
    if components is not None:
        payload["components"] = components
    try:
        r = requests.patch(
            f"{API}/webhooks/{application_id}/{interaction_token}/messages/@original",
            json=payload, timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        log.error("Interaction follow-up failed: %s", exc)
        return False
