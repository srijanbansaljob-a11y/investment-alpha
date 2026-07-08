"""
scripts/rebalance_check.py — Manual rebalance tool (winners-only, 50% trim)

Triggered by /rebalance Discord command via repository_dispatch.
Posts suggestions ONLY for profitable positions (up > REBAL_MIN_PROFIT_PCT).
Each suggestion proposes a 50% partial trim — you keep upside exposure while
freeing cash to deploy into new opportunities.

Philosophy:
  - NEVER suggest selling losers to rebalance (could lock in temporary losses)
  - ONLY trim winners (locking in half a gain is always rational)
  - User taps the button — nothing executes automatically
  - If already at or below exposure target: posts an all-clear card

Triggered two ways:
  1. /rebalance command  →  repository_dispatch { command: "rebalance_suggest" }
  2. trim_half button    →  repository_dispatch { command: "trim_half", ticker: "AAPL", portfolio: "pipeline" }
"""

import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from broker import discord_notify as dn
from broker.alpaca_client import get_client, get_positions, get_account_summary, place_market_order

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Config knobs
MIN_PROFIT_PCT   = getattr(config, "REBAL_MIN_PROFIT_PCT",  5.0)   # only trim positions up > this %
EXPOSURE_BUFFER  = getattr(config, "REBAL_EXPOSURE_BUFFER", 0.03)  # within 3% of target = no action needed

_GREEN  = 0x2ECC71
_ORANGE = 0xE67E22
_BLUE   = 0x3498DB
_GREY   = 0x95A5A6


def _get_regime_info() -> tuple[str, float]:
    """Return (label, target_pct) from daily_sentiment_data.json."""
    try:
        candidate = Path(__file__).parent.parent / "screener" / "daily_sentiment_data.json"
        if not candidate.exists():
            return "", getattr(config, "MAX_INVESTED_DEFAULT", 0.80)
        data = json.loads(candidate.read_text(encoding="utf-8"))
        label = ((data.get("macro_score") or {}).get("label", "") or "").upper().strip()
        # Strip emoji prefix if present (e.g. "🟢 STRONG BULL" → "STRONG BULL")
        for prefix in ["🟢 ", "🟡 ", "🟠 ", "🔴 "]:
            label = label.replace(prefix, "")
        label_map = {
            "STRONG BULL": "STRONG BULL", "MOD BULL": "MOD BULL",
            "NEUTRAL": "NEUTRAL", "BEARISH": "BEARISH", "BULL": "MOD BULL",
        }
        matched    = label_map.get(label, "")
        pcts       = getattr(config, "MAX_INVESTED_PCTS", {})
        target_pct = pcts.get(matched, getattr(config, "MAX_INVESTED_DEFAULT", 0.80))
        return label, target_pct
    except Exception:
        return "", getattr(config, "MAX_INVESTED_DEFAULT", 0.80)


def cmd_rebalance_suggest(payload: dict) -> None:
    """
    Find profitable positions and suggest 50% partial trims via Discord buttons.
    Called when user types /rebalance.
    """
    portfolio = payload.get("portfolio", "pipeline")
    client    = get_client(portfolio)
    positions = get_positions(client)
    acct      = get_account_summary(client)
    equity    = acct.get("equity", 0)
    invested  = sum(float(p.get("market_value", 0)) for p in positions.values())

    regime_label, target_pct = _get_regime_info()
    invested_pct  = invested / equity if equity > 0 else 0
    target_dollars = equity * target_pct
    excess_dollars = invested - target_dollars

    # ── Summary card always posts first ────────────────────────────────────
    color  = _GREEN if excess_dollars <= 0 else _ORANGE
    status = (
        f"✅ **Exposure is within target** — {invested_pct*100:.0f}% invested "
        f"(regime limit: {target_pct*100:.0f}% in **{regime_label or 'UNKNOWN'}**).\n"
        f"No trimming needed. `/rebalance` is most useful when you're over the regime limit."
    ) if excess_dollars <= equity * EXPOSURE_BUFFER else (
        f"You are **{invested_pct*100:.0f}%** invested.\n"
        f"Regime target (**{regime_label or 'UNKNOWN'}**): **{target_pct*100:.0f}%**.\n"
        f"Need to free ≈**${excess_dollars:,.0f}** to reach target.\n\n"
        f"Scroll down for suggested trims — **winners only**, partial 50% positions only.\n"
        f"Nothing executes without your tap."
    )

    dn.post_message([{
        "title": "⚖️ Rebalance Check",
        "description": status,
        "color": color,
        "fields": [
            {"name": "Equity",           "value": f"${equity:,.0f}",            "inline": True},
            {"name": "Invested",         "value": f"${invested:,.0f} ({invested_pct*100:.0f}%)", "inline": True},
            {"name": "Regime target",    "value": f"{target_pct*100:.0f}% (${target_dollars:,.0f})", "inline": True},
        ],
        "footer": {"text": f"Investment Alpha — /rebalance | {portfolio} account"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }])

    if excess_dollars <= equity * EXPOSURE_BUFFER:
        return   # within buffer — no trim suggestions needed

    # ── Find winners only ────────────────────────────────────────────────────
    winners = [
        (ticker, pos) for ticker, pos in positions.items()
        if float(pos.get("unrealized_plpc", 0)) * 100 >= MIN_PROFIT_PCT
    ]
    winners.sort(key=lambda kv: -float(kv[1].get("unrealized_plpc", 0)))  # biggest gain first

    if not winners:
        dn.post_message([{
            "title": "⚖️ No Trim Candidates",
            "description": (
                f"No positions are up more than **{MIN_PROFIT_PCT:.0f}%** right now.\n\n"
                f"**Recommendation:** wait for the market to recover, or let existing positions "
                f"close naturally via stop-loss or time-stop. Don't sell at a loss just to rebalance."
            ),
            "color": _GREY,
            "footer": {"text": "Investment Alpha — rebalance (winners only)"},
        }])
        return

    freed = 0.0
    for ticker, pos in winners:
        mv      = float(pos.get("market_value", 0))
        pl      = float(pos.get("unrealized_pl", 0))
        plpc    = float(pos.get("unrealized_plpc", 0)) * 100
        price   = float(pos.get("current_price", 0))
        qty     = float(pos.get("qty", 0))
        trim_qty = math.floor(qty / 2)
        trim_val = mv / 2

        if trim_qty < 1:
            continue

        after_invested = (invested - trim_val) / equity * 100

        embed = {
            "title": f"📈 TRIM SUGGESTION — Sell 50% of {ticker}",
            "description": (
                f"**{ticker}** is up **{plpc:+.1f}%** — trimming half locks in gains while "
                f"keeping you in the position for further upside.\n\n"
                f"Selling **{trim_qty} shares** (50% of {int(qty)}) frees ≈**${trim_val:,.0f}** "
                f"and brings exposure to ≈**{after_invested:.0f}%**.\n\n"
                f"**The remaining {int(qty) - trim_qty} shares stay in place — you don't lose the position.**"
            ),
            "color": _GREEN,
            "fields": [
                {"name": "Current Price",    "value": f"${price:.2f}",              "inline": True},
                {"name": "Total position",   "value": f"${mv:,.0f} ({int(qty)} sh)", "inline": True},
                {"name": "P&L",             "value": f"${pl:+,.0f} ({plpc:+.1f}%)", "inline": True},
                {"name": "Shares to sell",   "value": f"{trim_qty} (50%)",           "inline": True},
                {"name": "Cash freed",       "value": f"≈${trim_val:,.0f}",          "inline": True},
                {"name": "Exposure after",   "value": f"≈{after_invested:.0f}%",     "inline": True},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"Investment Alpha — partial trim | {portfolio} account (paper)"},
        }
        comps = [{
            "type": 1,
            "components": [
                {
                    "type": 2, "style": 3,
                    "label": f"✅ Trim 50% of {ticker} ({trim_qty} shares)",
                    "custom_id": f"ia|trim_half|{ticker}|{portfolio}",
                },
                {
                    "type": 2, "style": 2,
                    "label": "⏭️ Skip — keep full position",
                    "custom_id": f"ia|reject|{ticker}|rebal_trim",
                },
            ],
        }]
        dn.post_message([embed], components=comps)
        freed += trim_val

        if freed >= excess_dollars:
            break   # posted enough suggestions to cover the gap


def cmd_trim_half(payload: dict) -> None:
    """
    Execute a 50% partial trim. Called when user taps 'Trim 50%' button.
    Sells floor(qty/2) shares of the position.
    """
    ticker    = (payload.get("ticker") or "").upper()
    portfolio = payload.get("portfolio", "pipeline")
    msg_id    = payload.get("message_id", "")

    if not ticker:
        dn.post_message([{"title": "❌ Error", "description": "No ticker specified.", "color": 0xE74C3C}])
        return

    client    = get_client(portfolio)
    positions = get_positions(client)

    if ticker not in positions:
        log.warning("trim_half: %s not found in %s positions", ticker, portfolio)
        dn.post_message([{
            "title": f"⚠️ {ticker} — Position Not Found",
            "description": "Position may have already been closed. No order placed.",
            "color": _ORANGE,
        }])
        # Update the original message footer
        if msg_id:
            dn.finalize_message(msg_id, f"⚠️ Position no longer exists — no order placed")
        return

    pos       = positions[ticker]
    qty       = float(pos.get("qty", 0))
    trim_qty  = math.floor(qty / 2)
    price_now = float(pos.get("current_price", 0))
    plpc      = float(pos.get("unrealized_plpc", 0)) * 100

    if trim_qty < 1:
        dn.post_message([{
            "title": f"⚠️ {ticker} — Can't Trim",
            "description": f"Position is only {qty} shares — can't sell half (would be < 1 share). Use `/sell` to close the full position.",
            "color": _ORANGE,
        }])
        return

    log.info("Trimming %d shares of %s (50%% of %.0f total) on %s account", trim_qty, ticker, qty, portfolio)
    result = place_market_order(client, ticker, trim_qty, "sell")
    ok     = result.get("status") not in ("failed",)

    if ok:
        dn.post_message([{
            "title": f"✅ Partial SELL submitted — {ticker}",
            "description": (
                f"Sold **{trim_qty} shares** (50% of position) at ≈${price_now:.2f}.\n"
                f"**{int(qty) - trim_qty} shares remain** in your {portfolio} account.\n"
                f"P&L at trim: **{plpc:+.1f}%**"
            ),
            "color": _GREEN,
            "fields": [
                {"name": "Shares sold",      "value": str(trim_qty),              "inline": True},
                {"name": "Shares remaining", "value": str(int(qty) - trim_qty),   "inline": True},
                {"name": "Order status",     "value": result.get("status", "?"),  "inline": True},
            ],
            "footer": {"text": f"Investment Alpha — partial trim | {portfolio} (paper)"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }])
    else:
        dn.post_message([{
            "title": f"❌ Partial SELL FAILED — {ticker}",
            "description": f"Error: {result.get('error', 'unknown')}",
            "color": 0xE74C3C,
        }])

    # Update the original Discord message footer
    if msg_id:
        label = (
            f"✅ Trimmed — {trim_qty} shares of {ticker} sold at ≈${price_now:.2f}"
            if ok else
            f"❌ Trim FAILED — {result.get('error', 'unknown')}"
        )
        dn.finalize_message(msg_id, label)


# ── Entry point (called by remote_commands dispatch) ─────────────────────────

HANDLERS = {
    "rebalance_suggest": cmd_rebalance_suggest,
    "trim_half":         cmd_trim_half,
}

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--command",   default="rebalance_suggest")
    parser.add_argument("--ticker",    default="")
    parser.add_argument("--portfolio", default="pipeline")
    parser.add_argument("--message-id", default="")
    args = parser.parse_args()

    payload = {
        "command":    args.command,
        "ticker":     args.ticker,
        "portfolio":  args.portfolio,
        "message_id": args.message_id,
    }
    handler = HANDLERS.get(args.command)
    if handler:
        handler(payload)
    else:
        print(f"Unknown command: {args.command}. Options: {list(HANDLERS)}")
