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
        "options": [{
            "name": "portfolio", "type": 3, "required": True,
            "description": "Which account to check",
            "choices": [
                {"name": "Screener", "value": "screener"},
                {"name": "Pipeline", "value": "pipeline"},
            ],
        }],
    },
    {
        "name": "regime",
        "description": "Current market regime: BULL / NEUTRAL / BEAR (~1 min)",
    },
    {
        "name": "monitor",
        "description": "Run an immediate position check — alerts post here if anything triggers (~2 min)",
        "options": [{
            "name": "portfolio", "type": 3, "required": True,
            "description": "Which account to monitor",
            "choices": [
                {"name": "Screener", "value": "screener"},
                {"name": "Pipeline", "value": "pipeline"},
            ],
        }],
    },
    {
        "name": "stoploss",
        "description": "Check stop-loss levels, or exit breached positions",
        "options": [
            {
                "name": "mode", "type": 3, "required": True,
                "description": "check = report only · execute = sell breached positions",
                "choices": [
                    {"name": "check (no orders)", "value": "check"},
                    {"name": "execute (places orders, asks to confirm)", "value": "execute"},
                ],
            },
            {
                "name": "portfolio", "type": 3, "required": True,
                "description": "Which account to check",
                "choices": [
                    {"name": "Screener", "value": "screener"},
                    {"name": "Pipeline", "value": "pipeline"},
                ],
            },
        ],
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
        "name": "strategy",
        "description": "Describe the current strategies and how stocks are picked (live from config)",
    },
    {
        "name": "chart",
        "description": "Price chart for a stock or the whole portfolio",
        "options": [{
            "name": "symbol", "type": 3, "required": True,
            "description": "Ticker (e.g. AAPL) or 'portfolio' for equity curve + P&L",
        }],
    },
    {
        "name": "screener",
        "description": "Latest screener results: top picks, regime, stock buckets (from KV cache)",
    },
    {
        "name": "help",
        "description": "Show all Investment Alpha commands and what they do",
    },
    {
        "name": "buy",
        "description": "Buy a stock — previews order (size, stop, target) and asks to confirm before executing",
        "options": [
            {
                "name": "symbol", "type": 3, "required": True,
                "description": "Ticker to buy (e.g. AAPL, C, REGN)",
            },
            {
                "name": "portfolio", "type": 3, "required": True,
                "description": "Which portfolio account to buy in",
                "choices": [
                    {"name": "Screener", "value": "screener"},
                    {"name": "Pipeline", "value": "pipeline"},
                ],
            },
        ],
    },
    {
        "name": "sell",
        "description": "Sell (close) an existing position — shows current P&L and asks to confirm",
        "options": [
            {
                "name": "symbol", "type": 3, "required": True,
                "description": "Ticker to sell (e.g. AAPL, C, REGN)",
            },
            {
                "name": "portfolio", "type": 3, "required": True,
                "description": "Which portfolio account to sell from",
                "choices": [
                    {"name": "Screener", "value": "screener"},
                    {"name": "Pipeline", "value": "pipeline"},
                ],
            },
            {
                "name": "qty", "type": 4, "required": False,
                "description": "Shares to sell (leave blank to sell entire position)",
            },
        ],
    },
    {
        "name": "pausetrading",
        "description": "Pause auto-trading: disables webhook buys + take-profit auto-sells. Manual /buy /sell still work.",
    },
    {
        "name": "resumetrading",
        "description": "Resume auto-trading after /pausetrading — re-enables webhook buys and take-profit auto-sells.",
    },
    {
        "name": "brief",
        "description": "Morning brief: positions, regime, top picks + buy buttons. Trigger fresh screener on demand.",
        "options": [{
            "name": "mode",
            "type": 3,
            "required": False,
            "description": "cached = show latest data instantly · fresh = re-run screener first (~5 min)",
            "choices": [
                {"name": "cached — instant from KV (default)", "value": "cached"},
                {"name": "fresh — re-run screener then show picks (~5 min)", "value": "fresh"},
            ],
        }],
    },
]

resp = requests.put(
    f"https://discord.com/api/v10/applications/{APP_ID}/commands",
    headers={"Authorization": f"Bot {BOT_TOKEN}"},
    json=COMMANDS,
    timeout=15,
)
if resp.ok:
    print(f"\u2705 Registered {len(resp.json())} slash commands:")
    for c in resp.json():
        print(f"   /{c['name']} \u2014 {c['description']}")
    print("\nGlobal commands can take up to ~1 hour to appear in your server.")
else:
    print(f"\u274c Registration failed ({resp.status_code}): {resp.text}")
