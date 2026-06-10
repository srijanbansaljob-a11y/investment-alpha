"""
scripts/register_discord_commands.py — one-time slash command registration

Registers /status /regime /monitor /stoploss /pipeline /help with Discord.
Run once after creating the bot (and again only if commands change):

    python scripts/register_discord_commands.py

Requires in .env (or environment):
    DISCORD_APP_ID     — Developer Portal → General Information → Application ID
    DISCORD_BOT_TOKEN  — Developer Portal → Bot → Token
"""

import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

APP_ID = os.getenv("DISCORD_APP_ID", "").strip()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

if not APP_ID or not BOT_TOKEN:
    sys.exit("Set DISCORD_APP_ID and DISCORD_BOT_TOKEN in .env first.")

COMMANDS = [
    {
        "name": "status",
        "description": "Portfolio status: positions, P&L, stop distances, regime (~1 min)",
    },
    {
        "name": "regime",
        "description": "Current market regime: BULL / NEUTRAL / BEAR (~1 min)",
    },
    {
        "name": "monitor",
        "description": "Run an immediate position check — alerts post here if anything triggers (~2 min)",
    },
    {
        "name": "stoploss",
        "description": "Check stop-loss levels, or exit breached positions",
        "options": [{
            "name": "mode", "type": 3, "required": True,
            "description": "check = report only · execute = sell breached positions (asks to confirm)",
            "choices": [
                {"name": "check (no orders)", "value": "check"},
                {"name": "execute (places orders, asks to confirm)", "value": "execute"},
            ],
        }],
    },
    {
        "name": "pipeline",
        "description": "Run the full quant pipeline",
        "options": [{
            "name": "mode", "type": 3, "required": True,
            "description": "dry = signals only · execute = rebalance portfolio (asks to confirm)",
            "choices": [
                {"name": "dry run (signals only)", "value": "dry"},
                {"name": "execute (rebalances, asks to confirm)", "value": "execute"},
            ],
        }],
    },
    {
        "name": "help",
        "description": "Show all Investment Alpha commands and what they do",
    },
]

resp = requests.put(
    f"https://discord.com/api/v10/applications/{APP_ID}/commands",
    headers={"Authorization": f"Bot {BOT_TOKEN}"},
    json=COMMANDS,
    timeout=15,
)
if resp.ok:
    print(f"✅ Registered {len(resp.json())} slash commands:")
    for c in resp.json():
        print(f"   /{c['name']} — {c['description']}")
    print("\nGlobal commands can take up to ~1 hour to appear in your server.")
else:
    print(f"❌ Registration failed ({resp.status_code}): {resp.text}")
