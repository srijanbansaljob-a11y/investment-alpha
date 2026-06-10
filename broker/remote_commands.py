"""
broker/remote_commands.py — Cloud Command Dispatcher

Run by .github/workflows/command.yml when a Discord slash command or button
press arrives (via the Cloudflare Worker → repository_dispatch).

KEY DESIGN RULES (from the BA analysis):
  1. ALL portfolio state comes from the Alpaca API — never from local JSON
     files, which don't exist on the GitHub runner.
  2. NOTHING executes without an explicit approve/confirm from the owner.
     This script only ever runs as the result of your button press or
     confirmed slash command.
  3. Output is summarised to fit Discord's embed limits (4096-char desc).

Payload (JSON via --payload or COMMAND_PAYLOAD env var):
  {
    "command": "status|regime|monitor_check|stoploss_check|stoploss_execute|
                pipeline_dry|pipeline_execute|approve_sell|reject",
    "ticker": "AAPL",                  # approve_sell / reject only
    "trigger": "stop_loss",            # approve_sell / reject only
    "message_id": "...",               # original alert message (to edit)
    "channel_id": "...",
    "application_id": "...",           # deferred slash commands
    "interaction_token": "..."
  }
"""

import os
import sys
import json
import logging
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from broker.alpaca_client import (
    get_client, get_positions, get_account_summary, is_market_open,
    close_position,
)
from broker.stop_loss import _compute_atr
from broker import discord_notify as dn

log = logging.getLogger(__name__)

_RED, _ORANGE, _GREEN, _BLUE, _GREY = 0xE74C3C, 0xE67E22, 0x2ECC71, 0x3498DB, 0x95A5A6

MAX_DESC = 4000  # safety margin under Discord's 4096


def _embed(title, description, color, fields=None, footer="Investment Alpha"):
    return {
        "title": title,
        "description": description[:MAX_DESC],
        "color": color,
        "fields": fields or [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": footer},
    }


def _reply(payload: dict, embeds: list):
    """Send result back: edit the deferred slash-command response if we have
    an interaction token, otherwise post a fresh channel message."""
    token = payload.get("interaction_token")
    app_id = payload.get("application_id")
    if token and app_id:
        if dn.edit_interaction_response(app_id, token, embeds):
            return
        log.warning("Interaction token expired — falling back to channel post")
    dn.post_message(embeds)


# ── Stop level (Alpaca-sourced, no local files) ────────────────────────────

def _stop_price(ticker: str, entry: float, regime: str) -> tuple[float, str]:
    atr_mults = getattr(config, "ATR_STOP_MULTIPLIER", {"bull": 2.5, "neutral": 2.0, "bear": 1.5})
    mult = atr_mults.get(regime, 2.0)
    if getattr(config, "USE_ATR_STOP_LOSS", True):
        atr = _compute_atr(ticker, period=getattr(config, "ATR_PERIOD", 14))
        if atr and atr > 0:
            return entry - mult * atr, f"ATR×{mult}"
    pct = config.STOP_LOSS_PCT.get(regime, 0.85)
    return entry * pct, f"fixed {pct:.0%}"


def _detect_regime() -> str:
    """Lightweight regime read: SPY vs 50/200-day SMA (no local state needed)."""
    try:
        import yfinance as yf
        spy = yf.download("SPY", period="300d", auto_adjust=True, progress=False)["Close"].squeeze()
        price, sma50, sma200 = float(spy.iloc[-1]), float(spy.rolling(50).mean().iloc[-1]), float(spy.rolling(200).mean().iloc[-1])
        if price > sma50 > sma200:
            return "bull"
        if price < sma200:
            return "bear"
        return "neutral"
    except Exception as exc:
        log.warning("Regime detect failed (%s) — defaulting to neutral", exc)
        return "neutral"


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_status(payload):
    client = get_client()
    acct = get_account_summary(client)
    positions = get_positions(client)
    regime = _detect_regime()

    lines = []
    for t, p in sorted(positions.items()):
        stop, method = _stop_price(t, p["avg_entry_price"], regime)
        dist = (p["current_price"] - stop) / p["current_price"] * 100
        lines.append(
            f"**{t}** {p['qty']:.1f} sh @ ${p['avg_entry_price']:.2f} → ${p['current_price']:.2f} "
            f"({p['unrealized_plpc']*100:+.1f}% / ${p['unrealized_pl']:,.0f}) · stop ${stop:.2f} ({dist:.1f}% away)"
        )
    desc = "\n".join(lines) if lines else "_No open positions._"
    fields = [
        {"name": "Equity", "value": f"${acct['equity']:,.2f}", "inline": True},
        {"name": "Cash", "value": f"${acct['cash']:,.2f}", "inline": True},
        {"name": "Regime", "value": regime.upper(), "inline": True},
        {"name": "Market", "value": "🟢 Open" if is_market_open(client) else "🔴 Closed", "inline": True},
        {"name": "Positions", "value": str(len(positions)), "inline": True},
    ]
    _reply(payload, [_embed("📊 Portfolio Status", desc, _BLUE, fields)])


def cmd_regime(payload):
    regime = _detect_regime()
    color = {"bull": _GREEN, "neutral": _ORANGE, "bear": _RED}[regime]
    _reply(payload, [_embed(
        f"🧭 Market Regime: {regime.upper()}",
        "Based on SPY vs 50/200-day moving averages.",
        color,
    )])


def cmd_monitor_check(payload):
    from broker.monitor import check_positions
    client = get_client()
    n = check_positions(client)
    _reply(payload, [_embed(
        "🔍 Monitor Check Complete",
        f"**{n}** new alert(s) raised." if n else "All positions within bounds ✓",
        _ORANGE if n else _GREEN,
    )])


def _stoploss_scan():
    """Evaluate every Alpaca position against its stop. Returns (rows, breached)."""
    client = get_client()
    positions = get_positions(client)
    regime = _detect_regime()
    rows, breached = [], []
    for t, p in sorted(positions.items()):
        stop, method = _stop_price(t, p["avg_entry_price"], regime)
        hit = p["current_price"] <= stop
        rows.append(
            f"{'🛑' if hit else '✅'} **{t}** ${p['current_price']:.2f} vs stop ${stop:.2f} "
            f"[{method}] ({p['unrealized_plpc']*100:+.1f}%)"
        )
        if hit:
            breached.append(t)
    return client, rows, breached, regime


def cmd_stoploss_check(payload):
    _, rows, breached, regime = _stoploss_scan()
    desc = "\n".join(rows) if rows else "_No open positions._"
    color = _RED if breached else _GREEN
    title = f"🛑 Stop-Loss Check — {len(breached)} BREACHED" if breached else "✅ Stop-Loss Check — all clear"
    _reply(payload, [_embed(title, desc, color, [
        {"name": "Regime", "value": regime.upper(), "inline": True},
        {"name": "Note", "value": "No orders placed. Use `/stoploss mode:execute` to exit breached positions.", "inline": False},
    ])])


def cmd_stoploss_execute(payload):
    client, rows, breached, regime = _stoploss_scan()
    if not breached:
        _reply(payload, [_embed("✅ Stop-Loss Execute", "No positions are below their stop — nothing to exit.", _GREEN)])
        return
    results = []
    for t in breached:
        r = close_position(client, t)
        ok = r.get("status") not in ("failed",)
        results.append(f"{'✅' if ok else '❌'} {t}: {r.get('status')}" + (f" — {r.get('error')}" if r.get("error") else ""))
    _reply(payload, [_embed(
        f"🛑 Stop-Loss Executed — {len(breached)} position(s)",
        "\n".join(results) + "\n\n" + "\n".join(rows),
        _RED,
    )])


def _run_pipeline(execute: bool) -> tuple[bool, str]:
    cmd = [sys.executable, "main.py"] + (["--execute"] if execute else [])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3000,
            cwd=str(Path(__file__).parent.parent),
        )
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.returncode != 0 else "")
        # keep the tail — rankings/summary print last
        return proc.returncode == 0, out[-MAX_DESC:]
    except subprocess.TimeoutExpired:
        return False, "Pipeline timed out after 50 minutes."


def cmd_pipeline_dry(payload):
    ok, out = _run_pipeline(execute=False)
    _reply(payload, [_embed(
        "📈 Pipeline — Dry Run" + ("" if ok else " (FAILED)"),
        f"```\n{out}\n```", _BLUE if ok else _RED,
        [{"name": "Next step", "value": "Happy with the signals? Run `/pipeline mode:execute`.", "inline": False}] if ok else [],
    )])


def cmd_pipeline_execute(payload):
    ok, out = _run_pipeline(execute=True)
    _reply(payload, [_embed(
        "🚀 Pipeline — EXECUTED" + ("" if ok else " (FAILED)"),
        f"```\n{out}\n```", _GREEN if ok else _RED,
    )])


# ── Button decisions ───────────────────────────────────────────────────────

def _finalize_alert(payload, verdict: str, extra_field: dict | None = None):
    """Edit the original alert: strip buttons, stamp the decision."""
    mid, cid = payload.get("message_id"), payload.get("channel_id")
    if not mid:
        return
    msg = dn.get_message(mid, cid)
    if not msg:
        return
    embeds = msg.get("embeds", [])
    if embeds:
        embeds[0]["footer"] = {"text": verdict}
        if extra_field:
            embeds[0].setdefault("fields", []).append(extra_field)
    dn.edit_message(mid, embeds=embeds, components=[], channel_id=cid)


def cmd_approve_sell(payload):
    ticker = payload.get("ticker", "").upper()
    if not ticker:
        _reply(payload, [_embed("❌ Error", "No ticker in approval payload.", _RED)])
        return
    client = get_client()
    if ticker not in get_positions(client):
        _finalize_alert(payload, f"⚠️ No open {ticker} position — nothing sold")
        dn.post_message([_embed(f"⚠️ {ticker}", "Position no longer exists — no order placed.", _ORANGE)])
        return
    r = close_position(client, ticker)
    ok = r.get("status") not in ("failed",)
    _finalize_alert(
        payload,
        f"✅ Approved by you — sell submitted {datetime.now(timezone.utc).strftime('%H:%M UTC')}" if ok
        else "❌ Approved but order FAILED",
        {"name": "Order", "value": f"{r.get('status')} (id: {r.get('order_id', 'n/a')})", "inline": False},
    )
    dn.post_message([_embed(
        f"{'✅' if ok else '❌'} SELL {'submitted' if ok else 'FAILED'} — {ticker}",
        f"Trigger: {payload.get('trigger', '?').replace('_', ' ')}\nStatus: **{r.get('status')}**"
        + (f"\nError: {r.get('error')}" if r.get("error") else ""),
        _GREEN if ok else _RED,
    )])


def cmd_reject(payload):
    _finalize_alert(payload, "❌ Rejected by you — position kept")


# ── Entry point ────────────────────────────────────────────────────────────

COMMANDS = {
    "status":            cmd_status,
    "regime":            cmd_regime,
    "monitor_check":     cmd_monitor_check,
    "stoploss_check":    cmd_stoploss_check,
    "stoploss_execute":  cmd_stoploss_execute,
    "pipeline_dry":      cmd_pipeline_dry,
    "pipeline_execute":  cmd_pipeline_execute,
    "approve_sell":      cmd_approve_sell,
    "reject":            cmd_reject,
}


def main():
    parser = argparse.ArgumentParser(description="Investment Alpha — remote command dispatcher")
    parser.add_argument("--payload", default=os.getenv("COMMAND_PAYLOAD", "{}"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")

    payload = json.loads(args.payload)
    command = payload.get("command", "")
    handler = COMMANDS.get(command)
    if not handler:
        log.error("Unknown command: %r", command)
        dn.post_message([_embed("❌ Unknown command", f"`{command}` is not recognised.", _RED)])
        sys.exit(1)

    log.info("Dispatching command: %s", command)
    try:
        handler(payload)
    except Exception as exc:
        log.error("Command %s failed: %s", command, exc, exc_info=True)
        _reply(payload, [_embed(f"❌ {command} failed", f"```\n{exc}\n```", _RED)])
        sys.exit(1)


if __name__ == "__main__":
    main()
