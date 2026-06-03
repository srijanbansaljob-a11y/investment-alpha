"""
broker/monitor.py — Intraday Portfolio Monitor

Runs continuously during market hours (or on a schedule), checking your
Alpaca positions every N minutes against three triggers:

  1. STOP-LOSS breach    → price falls below ATR-based stop level
  2. PROFIT TARGET hit   → position is up >= PROFIT_TARGET_PCT from entry
  3. SHARP INTRADAY MOVE → stock moves ±INTRADAY_MOVE_ALERT_PCT from today's open

When a breach is detected:
  - Posts a rich Discord embed alert immediately
  - Logs the pending action to data/pending_actions.json
  - Waits AUTO_EXECUTE_DELAY_MINUTES for you to override
  - Auto-executes (market sell via Alpaca) if no override received

Sharp intraday moves are ALERT-ONLY — they never trigger auto-execute,
since big intraday swings are often reversals worth watching, not acting on.

Usage
-----
  python broker/monitor.py              # start continuous monitoring loop
  python broker/monitor.py --dry-run    # monitor and alert but never execute
  python broker/monitor.py --once       # one check cycle and exit
  python broker/monitor.py --override TICKER  # cancel a pending auto-execute
  python broker/monitor.py --test       # send a test Discord message and exit

Setup
-----
  1. Create a free Discord server
  2. In a channel: Edit Channel → Integrations → Webhooks → New Webhook → Copy URL
  3. Add to your .env file:
       DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN
  4. Run: python broker/monitor.py --test   (verify Discord works)
  5. Run: python broker/monitor.py          (start monitoring)

Override a pending auto-execute
--------------------------------
  Option A (CLI):  python broker/monitor.py --override AAPL
  Option B (file): Open data/pending_actions.json, set "override": true for the ticker

Config (all in config.py)
--------------------------
  DISCORD_WEBHOOK_URL       — webhook URL (from .env)
  PROFIT_TARGET_PCT         — default 0.20 (20% gain triggers alert)
  INTRADAY_MOVE_ALERT_PCT   — default 0.05 (±5% intraday triggers alert)
  AUTO_EXECUTE_DELAY_MINUTES — default 5 (minutes before auto-sell)
  MONITOR_INTERVAL_SECONDS  — default 300 (check every 5 minutes)
"""

import sys
import json
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from broker.alpaca_client import get_client, get_positions, is_market_open
from broker.stop_loss import _compute_atr, _load_portfolio_state

log = logging.getLogger(__name__)

# ── Config (with sensible defaults if not in config.py) ───────────────────
DISCORD_WEBHOOK_URL      = getattr(config, "DISCORD_WEBHOOK_URL", "")
PROFIT_TARGET_PCT        = getattr(config, "PROFIT_TARGET_PCT", 0.20)
INTRADAY_MOVE_PCT        = getattr(config, "INTRADAY_MOVE_ALERT_PCT", 0.05)
AUTO_EXECUTE_DELAY_MIN   = getattr(config, "AUTO_EXECUTE_DELAY_MINUTES", 5)
MONITOR_INTERVAL_SEC     = getattr(config, "MONITOR_INTERVAL_SECONDS", 300)

PENDING_ACTIONS_FILE     = config.DATA_DIR / "pending_actions.json"

# Discord embed color palette
_RED    = 0xE74C3C   # stop-loss
_ORANGE = 0xE67E22   # sharp intraday move
_GREEN  = 0x2ECC71   # profit target
_BLUE   = 0x3498DB   # info / execution confirmation


# ── Discord Helpers ────────────────────────────────────────────────────────

def _make_embed(
    title: str,
    description: str,
    color: int,
    fields: list,
    footer: str = "Investment Alpha Monitor",
) -> dict:
    """Build a Discord embed dict."""
    return {
        "title":       title,
        "description": description,
        "color":       color,
        "fields": [
            {
                "name":   f["name"],
                "value":  f["value"],
                "inline": f.get("inline", True),
            }
            for f in fields
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": footer},
    }


def _send_discord(embeds: list, dry_run: bool = False) -> bool:
    """POST embeds to the configured Discord webhook. Returns True on success."""
    if dry_run:
        log.info("[DRY-RUN] Would send Discord alert: %s",
                 [e.get("title", "?") for e in embeds])
        return True
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL not set — alert logged to console only")
        for e in embeds:
            log.warning("  ALERT: %s — %s", e.get("title"), e.get("description"))
        return False
    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"embeds": embeds},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.error("Discord webhook POST failed: %s", exc)
        return False


def send_test_message() -> bool:
    """Send a test embed to verify Discord is configured correctly."""
    embed = _make_embed(
        title="✅ Investment Alpha Monitor — Connected",
        description=(
            "Discord alerts are working. "
            "You will be notified here for stop-losses, profit targets, and sharp intraday moves."
        ),
        color=_BLUE,
        fields=[
            {"name": "Stop-loss",         "value": "ATR-based (regime-adjusted)",         "inline": True},
            {"name": "Profit target",     "value": f"+{PROFIT_TARGET_PCT*100:.0f}%",       "inline": True},
            {"name": "Intraday alert",    "value": f"±{INTRADAY_MOVE_PCT*100:.0f}% today", "inline": True},
            {"name": "Auto-execute delay","value": f"{AUTO_EXECUTE_DELAY_MIN} minutes",    "inline": True},
            {"name": "Check interval",    "value": f"Every {MONITOR_INTERVAL_SEC//60} min","inline": True},
            {"name": "Override command",
             "value": "`python broker/monitor.py --override TICKER`",
             "inline": False},
        ],
    )
    return _send_discord([embed], dry_run=False)


# ── Pending Actions ────────────────────────────────────────────────────────

def _load_pending() -> dict:
    """Load data/pending_actions.json (null-byte safe)."""
    if not PENDING_ACTIONS_FILE.exists():
        return {}
    try:
        raw = PENDING_ACTIONS_FILE.read_bytes().rstrip(b"\x00")
        return json.loads(raw)
    except Exception:
        return {}


def _save_pending(pending: dict) -> None:
    PENDING_ACTIONS_FILE.write_text(json.dumps(pending, indent=2))


def _register_pending(
    pending: dict,
    ticker: str,
    trigger: str,
    current_price: float,
    level: float,
    pnl_pct: float,
    alert_only: bool = False,
) -> None:
    """
    Add a pending action entry. If ticker already has a non-overridden entry,
    do nothing (prevents duplicate alerts on consecutive checks).
    alert_only=True sets override=True immediately (no auto-execute).
    """
    if ticker in pending and not pending[ticker].get("override") and not pending[ticker].get("executed"):
        return  # already queued, don't overwrite
    execute_after = datetime.now(timezone.utc) + timedelta(minutes=AUTO_EXECUTE_DELAY_MIN)
    pending[ticker] = {
        "ticker":        ticker,
        "trigger":       trigger,
        "price":         round(current_price, 4),
        "level":         round(level, 4),
        "pnl_pct":       round(pnl_pct, 2),
        "queued_at":     datetime.now(timezone.utc).isoformat(),
        "execute_after": execute_after.isoformat(),
        "override":      alert_only,   # True = alert only, no auto-execute
        "executed":      False,
    }
    _save_pending(pending)


def override_ticker(ticker: str) -> None:
    """Cancel a pending auto-execute for a ticker."""
    pending = _load_pending()
    t = ticker.upper()
    if t in pending and not pending[t].get("executed"):
        pending[t]["override"] = True
        _save_pending(pending)
        print(f"✅ Override set for {t} — auto-execute cancelled.")
        print(f"   The position will NOT be auto-sold.")
    else:
        print(f"ℹ️  No active pending action found for {t}")


# ── Stop Price (mirrors stop_loss.py logic) ────────────────────────────────

def _get_stop_price(ticker: str, entry_price: float) -> float | None:
    """
    Compute the ATR-based stop price for a ticker.
    Falls back to fixed-percentage stop if ATR is unavailable.
    Returns None if stop-loss is disabled.
    """
    if not getattr(config, "STOP_LOSS_ENABLED", True):
        return None

    # Read current regime from portfolio state
    try:
        state = _load_portfolio_state()
        regime = state.get("regime", "bull").lower()
    except Exception:
        regime = "bull"

    use_atr = getattr(config, "USE_ATR_STOP_LOSS", True)
    atr_mults = getattr(config, "ATR_STOP_MULTIPLIER", {"bull": 2.5, "neutral": 2.0, "bear": 1.5})
    atr_period = getattr(config, "ATR_PERIOD", 14)
    atr_mult = atr_mults.get(regime, 2.0)

    if use_atr:
        atr = _compute_atr(ticker, period=atr_period)
        if atr and atr > 0:
            return entry_price - (atr_mult * atr)

    # Fallback to fixed percentage
    stop_pct = config.STOP_LOSS_PCT.get(regime, 0.85)
    return entry_price * stop_pct


# ── Intraday Open Prices ───────────────────────────────────────────────────

def _get_today_opens(tickers: list) -> dict:
    """
    Fetch today's opening price for each ticker using 5-minute bars.
    Returns {ticker: open_price} — value is None if data unavailable.
    """
    if not tickers:
        return {}
    try:
        raw = yf.download(
            tickers,
            period="1d",
            interval="5m",
            progress=False,
            auto_adjust=True,
        )
        result = {}
        if len(tickers) == 1:
            col = raw["Open"].squeeze()
            result[tickers[0]] = float(col.iloc[0]) if not col.empty else None
        else:
            for t in tickers:
                try:
                    col = raw["Open"][t].dropna()
                    result[t] = float(col.iloc[0]) if not col.empty else None
                except Exception:
                    result[t] = None
        return result
    except Exception as exc:
        log.warning("Could not fetch today's opens via yfinance: %s", exc)
        return {t: None for t in tickers}


# ── Core Check Loop ────────────────────────────────────────────────────────

def check_positions(client, dry_run: bool = False) -> int:
    """
    Single monitoring pass: evaluate all open Alpaca positions against
    the three triggers and fire Discord alerts for any breaches found.

    Returns the number of new alerts sent.
    """
    positions = get_positions(client)
    if not positions:
        log.info("No open positions to monitor")
        return 0

    tickers = list(positions.keys())
    today_opens = _get_today_opens(tickers)
    pending = _load_pending()
    new_alerts = []
    num_sent = 0

    for ticker, pos in positions.items():
        current    = pos["current_price"]
        entry      = pos["avg_entry_price"]
        unrealized = pos["unrealized_pl"]       # dollar P&L
        total_plpc = pos["unrealized_plpc"]     # fractional P&L from entry
        today_open = today_opens.get(ticker)

        # Helper to check if this ticker already has an active (non-executed, non-overridden) alert
        def _already_pending(trigger_types=None):
            a = pending.get(ticker, {})
            if a.get("executed") or a.get("override"):
                return False
            if trigger_types:
                return a.get("trigger") in trigger_types
            return bool(a)

        # ── Trigger 1: Stop-Loss ───────────────────────────────────────
        stop_price = _get_stop_price(ticker, entry)
        if stop_price and current <= stop_price:
            if not _already_pending(["stop_loss"]):
                loss_pct = (current - entry) / entry * 100
                log.warning(
                    "STOP-LOSS BREACHED: %s | current=%.2f | stop=%.2f | loss=%.1f%%",
                    ticker, current, stop_price, loss_pct,
                )
                embed = _make_embed(
                    title=f"🛑 STOP-LOSS BREACHED — {ticker}",
                    description=(
                        f"**{ticker}** has dropped below its ATR-based stop level.\n"
                        f"A **market SELL** will be submitted in **{AUTO_EXECUTE_DELAY_MIN} minutes** "
                        f"unless you override."
                    ),
                    color=_RED,
                    fields=[
                        {"name": "Current Price",     "value": f"${current:.2f}",          "inline": True},
                        {"name": "Stop Level",        "value": f"${stop_price:.2f}",        "inline": True},
                        {"name": "Loss from Entry",   "value": f"{loss_pct:+.1f}%",         "inline": True},
                        {"name": "Entry Price",       "value": f"${entry:.2f}",             "inline": True},
                        {"name": "Unrealised P&L",    "value": f"${unrealized:,.2f}",        "inline": True},
                        {"name": "Auto-execute in",   "value": f"{AUTO_EXECUTE_DELAY_MIN} min", "inline": True},
                        {
                            "name":   "⚠️ To cancel auto-sell:",
                            "value":  f"`python broker/monitor.py --override {ticker}`",
                            "inline": False,
                        },
                    ],
                )
                new_alerts.append(embed)
                _register_pending(pending, ticker, "stop_loss", current, stop_price, loss_pct)
                num_sent += 1
            continue  # stop-loss takes priority — skip other checks

        # ── Trigger 2: Profit Target ───────────────────────────────────
        gain_frac = (current - entry) / entry
        if gain_frac >= PROFIT_TARGET_PCT:
            if not _already_pending(["profit_target"]):
                gain_pct = gain_frac * 100
                log.info("PROFIT TARGET: %s | gain=%.1f%% | P&L=$%.2f", ticker, gain_pct, unrealized)
                embed = _make_embed(
                    title=f"🎯 PROFIT TARGET HIT — {ticker}",
                    description=(
                        f"**{ticker}** is up **{gain_pct:.1f}%** from your entry — "
                        f"at or above the **{PROFIT_TARGET_PCT*100:.0f}% target**.\n"
                        f"A **market SELL** will be submitted in **{AUTO_EXECUTE_DELAY_MIN} minutes** "
                        f"unless you override."
                    ),
                    color=_GREEN,
                    fields=[
                        {"name": "Current Price",    "value": f"${current:.2f}",     "inline": True},
                        {"name": "Entry Price",      "value": f"${entry:.2f}",        "inline": True},
                        {"name": "Total Gain",       "value": f"+{gain_pct:.1f}%",    "inline": True},
                        {"name": "Unrealised P&L",   "value": f"${unrealized:,.2f}",  "inline": True},
                        {"name": "Target was",       "value": f"+{PROFIT_TARGET_PCT*100:.0f}%", "inline": True},
                        {"name": "Auto-execute in",  "value": f"{AUTO_EXECUTE_DELAY_MIN} min", "inline": True},
                        {
                            "name":   "⚠️ To cancel auto-sell:",
                            "value":  f"`python broker/monitor.py --override {ticker}`",
                            "inline": False,
                        },
                    ],
                )
                new_alerts.append(embed)
                _register_pending(
                    pending, ticker, "profit_target",
                    current, entry * (1 + PROFIT_TARGET_PCT), gain_pct,
                )
                num_sent += 1

        # ── Trigger 3: Sharp Intraday Move (alert-only, no auto-execute) ─
        if today_open and today_open > 0:
            intraday_move = (current - today_open) / today_open
            if abs(intraday_move) >= INTRADAY_MOVE_PCT:
                move_dir = "up" if intraday_move > 0 else "down"
                trigger_name = f"sharp_move_{move_dir}"
                if not _already_pending(["sharp_move_up", "sharp_move_down"]):
                    move_pct = intraday_move * 100
                    emoji = "📈" if intraday_move > 0 else "📉"
                    log.info(
                        "SHARP INTRADAY MOVE (%s): %s | %.1f%% from today's open",
                        move_dir.upper(), ticker, move_pct,
                    )
                    embed = _make_embed(
                        title=f"{emoji} SHARP INTRADAY MOVE — {ticker}",
                        description=(
                            f"**{ticker}** has moved **{move_pct:+.1f}%** from today's open. "
                            f"This may indicate breaking news, earnings surprise, or unusual volume.\n"
                            f"**No auto-execute** — this is an informational alert."
                        ),
                        color=_ORANGE,
                        fields=[
                            {"name": "Current Price",   "value": f"${current:.2f}",         "inline": True},
                            {"name": "Today's Open",    "value": f"${today_open:.2f}",       "inline": True},
                            {"name": "Intraday Move",   "value": f"{move_pct:+.1f}%",        "inline": True},
                            {"name": "Total P&L",       "value": f"{total_plpc*100:+.1f}% from entry", "inline": True},
                            {"name": "Unrealised P&L",  "value": f"${unrealized:,.2f}",      "inline": True},
                            {
                                "name":   "Suggested action",
                                "value":  "Review news for this ticker. No action required — monitor is watching.",
                                "inline": False,
                            },
                        ],
                        footer="Investment Alpha Monitor — Alert Only (no auto-execute for sharp moves)",
                    )
                    new_alerts.append(embed)
                    _register_pending(
                        pending, ticker, trigger_name,
                        current, today_open, move_pct,
                        alert_only=True,   # never auto-execute sharp moves
                    )
                    num_sent += 1

    # Send all alerts in batches of 10 (Discord hard limit per request)
    for i in range(0, len(new_alerts), 10):
        batch = new_alerts[i : i + 10]
        _send_discord(batch, dry_run=dry_run)
        if i + 10 < len(new_alerts):
            time.sleep(1)  # Discord rate-limit: small pause between batches

    return num_sent


def process_auto_executions(client, dry_run: bool = False) -> list:
    """
    Check pending_actions.json for entries past their execute_after deadline.
    Auto-execute (sell) any that haven't been overridden.

    Returns list of tickers that were executed.
    """
    pending = _load_pending()
    now = datetime.now(timezone.utc)
    executed_tickers = []

    for ticker, action in list(pending.items()):
        # Skip if already handled or overridden
        if action.get("executed") or action.get("override"):
            continue
        # Sharp moves are alert-only — never auto-execute
        if action.get("trigger") in ("sharp_move_up", "sharp_move_down"):
            continue

        execute_after = datetime.fromisoformat(action["execute_after"])
        if now < execute_after:
            remaining_sec = (execute_after - now).total_seconds()
            log.info(
                "  %s — auto-execute in %.0f min (trigger=%s)",
                ticker, remaining_sec / 60, action["trigger"],
            )
            continue

        # Deadline passed — execute
        queued_mins = (now - datetime.fromisoformat(action["queued_at"])).total_seconds() / 60
        log.warning(
            "AUTO-EXECUTING: %s (trigger=%s, %.1f min since alert)",
            ticker, action["trigger"], queued_mins,
        )

        success = False
        if not dry_run:
            try:
                alpaca_client_obj = client  # already authenticated TradingClient
                alpaca_client_obj.close_position(ticker)
                success = True
                log.info("  Alpaca close_position(%s): OK", ticker)
            except Exception as exc:
                log.error("  Alpaca close_position(%s) FAILED: %s", ticker, exc)
        else:
            log.info("  [DRY-RUN] Would close position: %s", ticker)
            success = True

        action["executed"]       = True
        action["executed_at"]    = now.isoformat()
        action["execute_success"] = success
        action["dry_run"]        = dry_run

        # Discord confirmation
        status_emoji = "✅" if success else "❌"
        embed = _make_embed(
            title=f"{status_emoji} AUTO-EXECUTED — {ticker}",
            description=(
                f"**{ticker}** position has been "
                f"{'closed (paper order submitted)' if not dry_run else 'closed [DRY RUN — no real order]'}."
            ),
            color=_BLUE if success else _ORANGE,
            fields=[
                {"name": "Trigger",         "value": action["trigger"].replace("_", " ").title(), "inline": True},
                {"name": "Status",          "value": "✅ Executed" if success else "❌ Failed",    "inline": True},
                {"name": "Price at alert",  "value": f"${action['price']:.2f}",                   "inline": True},
                {"name": "Queued",          "value": f"{queued_mins:.1f} min ago",                "inline": True},
            ],
        )
        _send_discord([embed], dry_run=False)  # always send confirmations, even in dry-run mode
        executed_tickers.append(ticker)

    if executed_tickers:
        _save_pending(pending)

    return executed_tickers


# ── Entry Point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Investment Alpha — Intraday Portfolio Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python broker/monitor.py                  # start monitoring loop
  python broker/monitor.py --dry-run        # monitor without auto-executing
  python broker/monitor.py --test           # verify Discord webhook works
  python broker/monitor.py --override AAPL  # cancel pending sell for AAPL
  python broker/monitor.py --once           # single check, then exit
        """,
    )
    parser.add_argument(
        "--dry-run",  action="store_true",
        help="Monitor and send alerts but never submit orders to Alpaca",
    )
    parser.add_argument(
        "--override", metavar="TICKER",
        help="Cancel the pending auto-execute for TICKER",
    )
    parser.add_argument(
        "--test",     action="store_true",
        help="Send a test Discord message and exit",
    )
    parser.add_argument(
        "--once",     action="store_true",
        help="Run exactly one check cycle, then exit (useful for external schedulers)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── Override mode ─────────────────────────────────────────────────────
    if args.override:
        override_ticker(args.override)
        return

    # ── Test mode ─────────────────────────────────────────────────────────
    if args.test:
        print("Sending test Discord message...")
        ok = send_test_message()
        if ok:
            print("✅ Test message sent! Check your Discord channel.")
        else:
            print("❌ Failed. Check that DISCORD_WEBHOOK_URL is set correctly in .env")
        return

    # ── Startup banner ────────────────────────────────────────────────────
    if not DISCORD_WEBHOOK_URL:
        print("\n⚠️  WARNING: DISCORD_WEBHOOK_URL not set in .env")
        print("   Alerts will be logged to console only.")
        print("   Set it up: https://support.discord.com/hc/en-us/articles/228383668\n")

    print(f"\n{'='*58}")
    print(f"  Investment Alpha — Intraday Monitor")
    print(f"{'='*58}")
    print(f"  Profit target    : +{PROFIT_TARGET_PCT*100:.0f}% from entry")
    print(f"  Sharp move alert : ±{INTRADAY_MOVE_PCT*100:.0f}% from today's open")
    print(f"  Auto-execute     : {AUTO_EXECUTE_DELAY_MIN} min after alert "
          f"{'[DISABLED — dry-run mode]' if args.dry_run else ''}")
    print(f"  Check interval   : every {MONITOR_INTERVAL_SEC // 60} min")
    print(f"  Discord          : {'✅ configured' if DISCORD_WEBHOOK_URL else '⚠️  NOT configured'}")
    print(f"{'='*58}\n")
    print("Press Ctrl+C to stop.\n")

    # ── Connect to Alpaca ─────────────────────────────────────────────────
    try:
        client = get_client()
        log.info("Connected to Alpaca paper trading ✅")
    except Exception as exc:
        log.error("Cannot connect to Alpaca: %s", exc)
        sys.exit(1)

    # ── Main loop ─────────────────────────────────────────────────────────
    check_count = 0
    try:
        while True:
            now = datetime.now(timezone.utc)

            # Don't poll when market is closed (check every 10 min instead)
            market_open = is_market_open(client)
            if not market_open:
                log.info("Market closed — next check in 10 min")
                if args.once:
                    break
                time.sleep(600)
                continue

            check_count += 1
            log.info("━━━ Check #%d at %s UTC ━━━", check_count, now.strftime("%H:%M:%S"))

            try:
                # 1. Check positions and fire alerts
                n_alerts = check_positions(client, dry_run=args.dry_run)
                if n_alerts:
                    log.info("  %d new alert(s) sent", n_alerts)
                else:
                    log.info("  All positions within bounds ✓")

                # 2. Process pending auto-executions
                executed = process_auto_executions(client, dry_run=args.dry_run)
                if executed:
                    log.info("  Auto-executed: %s", executed)

            except Exception as exc:
                log.error("Check cycle error: %s", exc, exc_info=True)

            if args.once:
                break

            log.info("  Next check in %d min...\n", MONITOR_INTERVAL_SEC // 60)
            time.sleep(MONITOR_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\n\nMonitor stopped by user.")


if __name__ == "__main__":
    main()
