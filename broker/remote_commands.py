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
                pipeline_dry|pipeline_execute|approve_sell|approve_buy|reject",
    "ticker": "AAPL",                  # approval buttons only
    "trigger": "stop_loss",            # approval buttons only
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


def _full_regime() -> dict:
    """Full pipeline regime (VIX + SPX 200MA + yield curve + credit spreads).
    Falls back to a light SPY-only check, then NEUTRAL."""
    try:
        from pipeline import regime as regime_module
        return regime_module.run()
    except Exception as exc:
        log.warning("Full regime failed (%s) — using light fallback", exc)
    try:
        import yfinance as yf
        spy = yf.download("SPY", period="300d", auto_adjust=True, progress=False)["Close"].squeeze()
        price, sma50, sma200 = float(spy.iloc[-1]), float(spy.rolling(50).mean().iloc[-1]), float(spy.rolling(200).mean().iloc[-1])
        regime = "bull" if price > sma50 > sma200 else ("bear" if price < sma200 else "neutral")
        return {"regime": regime, "notes": "light fallback: SPY vs 50/200 SMA only"}
    except Exception as exc:
        log.warning("Light regime failed too (%s) — NEUTRAL", exc)
        return {"regime": "neutral", "notes": "all regime data unavailable — defaulting to caution"}


def _detect_regime() -> str:
    return _full_regime().get("regime", "neutral")


# ── Decision journal (persisted via workflow commit-back) ──────────────────

def _journal(event: dict) -> None:
    """Append a decision/action record to data/decision_journal.json.
    The command workflow commits this file back to the repo after each run,
    so your approvals/rejections accumulate into a learning dataset."""
    try:
        path = config.DATA_DIR / "decision_journal.json"
        entries = []
        if path.exists():
            raw = path.read_bytes().rstrip(b"\x00")
            if raw:
                entries = json.loads(raw)
        event["timestamp"] = datetime.now(timezone.utc).isoformat()
        entries.append(event)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("Decision journal write failed: %s", exc)


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
    r = _full_regime()
    regime = r.get("regime", "neutral")
    color = {"bull": _GREEN, "neutral": _ORANGE, "bear": _RED}[regime]

    def fmt(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "n/a"

    fields = [
        {"name": "VIX", "value": fmt(r.get("vix_current")), "inline": True},
        {"name": "SPX vs 200MA", "value": fmt(r.get("spx_vs_200ma_pct"), "%"), "inline": True},
        {"name": "SPX", "value": fmt(r.get("spx_price")), "inline": True},
        {"name": "Yield curve (10Y−3M)", "value": fmt(r.get("yield_curve_spread"), "pp"), "inline": True},
        {"name": "Credit spread mom.", "value": fmt(r.get("credit_spread_momentum")), "inline": True},
        {"name": "Why", "value": r.get("notes", "—"), "inline": False},
    ]
    _reply(payload, [_embed(
        f"🧭 Market Regime: {regime.upper()}",
        "Regime measures the **structural trend** (200-day MA, volatility, credit), "
        "not today's move — a red day inside an uptrend is still BULL. "
        "Data failures now fall back to NEUTRAL, never BULL.",
        color, fields,
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
        _journal({"decision": "stoploss_execute", "ticker": t,
                  "order_status": r.get("status"), "regime": regime})
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
    _journal({"decision": "pipeline_execute", "success": ok})
    _reply(payload, [_embed(
        "🚀 Pipeline — EXECUTED" + ("" if ok else " (FAILED)"),
        f"```\n{out}\n```", _GREEN if ok else _RED,
    )])


def cmd_strategy(payload):
    """Self-describing strategy card — generated LIVE from config.py and the
    learned-weights files, so it always reflects the system as it runs today."""
    # Active factor weights (learned beat defaults)
    weights = None
    source = "config defaults"
    lw = Path(getattr(config, "LEARNED_WEIGHTS_FILE", "data/learned_weights.json"))
    if not lw.is_absolute():
        lw = config.BASE_DIR / lw
    if lw.exists():
        try:
            weights = json.loads(lw.read_bytes().rstrip(b"\x00"))
            source = "learned (adaptive)"
        except Exception:
            pass
    if weights is None:
        weights = getattr(config, "FACTOR_WEIGHTS_WITH_SENTIMENT", config.FACTOR_WEIGHTS)
    w_lines = " · ".join(f"{k} {v*100:.0f}%" for k, v in sorted(weights.items(), key=lambda x: -x[1]))

    # Learning state
    v2 = config.DATA_DIR / "learned_weights_v2.json"
    learn_line = "Weekly per-regime learning: not yet started (needs first Saturday run)"
    if v2.exists():
        try:
            store = json.loads(v2.read_bytes().rstrip(b"\x00"))
            obs = {r: n["n_obs"] for r, n in store.get("regimes", {}).items()}
            learn_line = ("Weekly per-regime learning active — observations: "
                          + ", ".join(f"{r} {n}" for r, n in obs.items()))
        except Exception:
            pass

    top_n = getattr(config, "REGIME_TOP_N", {})
    atr_m = getattr(config, "ATR_STOP_MULTIPLIER", {})
    desc = (
        f"**1️⃣ Core: {len(weights)}-factor monthly rotation** "
        f"(universe: {len(getattr(config, 'ALL_TICKERS', []))} US stocks)\n"
        f"Each stock is scored on: **{w_lines}**\n"
        f"_Weights source: {source}. Volatility is a penalty; sentiment blends analyst revisions"
        + (" 70% + congressional trades 30%" if getattr(config, "CONGRESSIONAL_ENABLED", False) else "")
        + "._\n"
        f"**Filters:** drop if >{abs(getattr(config, 'MA200_HARD_EXCLUDE', -0.03))*100:.0f}% below 200-day MA "
        f"(soft penalty within {getattr(config, 'MA200_SOFT_ZONE', 0.03)*100:.0f}%), "
        f"sector ≤{getattr(config, 'SECTOR_MAX_WEIGHT', 0.30)*100:.0f}% of portfolio, "
        f"no buys within {getattr(config, 'EARNINGS_BLACKOUT_DAYS', 5)}d of earnings.\n"
        f"**Picks:** top {top_n.get('bull','10')} (bull) / {top_n.get('neutral','8')} (neutral) / "
        f"{top_n.get('bear','5')} (bear) by composite score, sized inverse-volatility.\n"
        f"**Stops:** ATR({getattr(config, 'ATR_PERIOD', 14)}) × "
        f"{atr_m.get('bull','2.5')}/{atr_m.get('neutral','2.0')}/{atr_m.get('bear','1.5')} "
        f"(bull/neutral/bear) — alerts with ✅/❌ buttons, never auto-sold.\n\n"
    )
    if getattr(config, "MR_ENABLED", False):
        desc += (
            f"**2️⃣ Mean-reversion sleeve** ({getattr(config, 'MR_SLEEVE_PCT', 0.10)*100:.0f}% of equity, "
            f"max {getattr(config, 'MR_MAX_POSITIONS', 5)} slots)\n"
            f"Buys RSI(2)<{getattr(config, 'MR_RSI_ENTRY', 10)} dips in stocks above their 200-day MA; "
            f"exits on close > 5-day MA or {getattr(config, 'MR_MAX_HOLD_DAYS', 10)}-day time stop. "
            f"Every entry/exit is a button proposal.\n\n"
        )
    if getattr(config, "DM_ENABLED", False):
        desc += ("**3️⃣ Dual-momentum compass** (monthly, advisory)\n"
                 "SPY vs VEU vs AGG vs T-bills, 12-month returns — warns when equities lose "
                 "absolute momentum.\n\n")
    desc += (
        f"**🧠 How it learns:** shadow-logs the top-30 every run (not just buys); {learn_line}; "
        f"stop exits get a 30-day post-mortem (too tight → ATR suggestion); "
        f"your ✅/❌ decisions are scored vs the model every Saturday.\n"
        f"**Regime now:** falls back to NEUTRAL on data failure (never BULL)."
    )
    _reply(payload, [_embed("📜 Current Strategy — live from config", desc, _BLUE,
                            footer="Auto-generated from config.py + learned weights — always current")])


def _render_stock_chart(ticker: str) -> tuple[bytes, str] | None:
    """6-month price chart with SMA50/200, entry + stop if held. Returns (png, caption)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from broker import market_data

    df = market_data.get_daily_bars(ticker, days=250)
    if df is None or len(df) < 30:
        return None
    closes = df["Close"]
    sma50 = closes.rolling(50).mean()
    sma200 = closes.rolling(200).mean()

    entry = stop = None
    try:
        client = get_client()
        pos = get_positions(client).get(ticker)
        if pos:
            entry = pos["avg_entry_price"]
            stop, _ = _stop_price(ticker, entry, _detect_regime())
    except Exception:
        pass

    n = min(len(closes), 126)  # ~6 months shown
    x = range(n)
    fig, ax = plt.subplots(figsize=(10, 5), dpi=110)
    ax.plot(x, closes.iloc[-n:].values, color="#3498DB", lw=1.8, label="Close")
    if sma50.notna().any():
        ax.plot(x, sma50.iloc[-n:].values, color="#E67E22", lw=1.2, label="SMA50")
    if sma200.notna().any():
        ax.plot(x, sma200.iloc[-n:].values, color="#95A5A6", lw=1.2, label="SMA200")
    if entry:
        ax.axhline(entry, color="#2ECC71", ls="--", lw=1.2, label=f"Entry ${entry:.2f}")
    if stop:
        ax.axhline(stop, color="#E74C3C", ls="--", lw=1.2, label=f"Stop ${stop:.2f}")
    last = float(closes.iloc[-1])
    ax.set_title(f"{ticker} — ${last:.2f}", fontsize=13, weight="bold")
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.25)
    ax.set_xticks([])
    fig.tight_layout()
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    cap = f"${last:.2f}"
    if entry:
        cap += f" · {((last-entry)/entry*100):+.1f}% from entry"
    return buf.getvalue(), cap


def _render_portfolio_chart() -> tuple[bytes, str] | None:
    """Equity curve vs SPY (normalised) + per-position P&L bars."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    client = get_client()
    equity = None
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        hist = client.get_portfolio_history(
            GetPortfolioHistoryRequest(period="3M", timeframe="1D"))
        eq = [e for e in (hist.equity or []) if e]
        if len(eq) >= 5:
            equity = eq
    except Exception as exc:
        log.warning("Portfolio history failed: %s", exc)

    positions = get_positions(client)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), dpi=110,
                                   gridspec_kw={"height_ratios": [2, 1]})
    caption = ""
    if equity:
        base = equity[0]
        ax1.plot(range(len(equity)), [e / base * 100 for e in equity],
                 color="#2ECC71", lw=2, label="Portfolio")
        try:
            import yfinance as yf
            spy = yf.download("SPY", period="3mo", auto_adjust=True,
                              progress=False)["Close"].squeeze().dropna()
            ax1.plot([i * (len(equity) - 1) / max(len(spy) - 1, 1) for i in range(len(spy))],
                     (spy / spy.iloc[0] * 100).values, color="#95A5A6", lw=1.3, label="SPY")
        except Exception:
            pass
        total_ret = (equity[-1] / base - 1) * 100
        caption = f"3-month equity: {total_ret:+.1f}% (${equity[-1]:,.0f})"
        ax1.set_title(f"Portfolio vs SPY — 3 months (indexed to 100) · {total_ret:+.1f}%",
                      fontsize=12, weight="bold")
        ax1.legend(fontsize=9)
        ax1.grid(alpha=0.25)
        ax1.set_xticks([])
    else:
        ax1.text(0.5, 0.5, "Equity history unavailable", ha="center", va="center")
        ax1.set_xticks([]); ax1.set_yticks([])

    if positions:
        items = sorted(positions.items(), key=lambda kv: kv[1]["unrealized_plpc"])
        names = [t for t, _ in items]
        pls = [p["unrealized_plpc"] * 100 for _, p in items]
        colors = ["#E74C3C" if v < 0 else "#2ECC71" for v in pls]
        ax2.barh(names, pls, color=colors)
        ax2.set_title("Open positions — unrealised P&L %", fontsize=11)
        ax2.grid(alpha=0.25, axis="x")
    else:
        ax2.text(0.5, 0.5, "No open positions", ha="center", va="center")
        ax2.set_xticks([]); ax2.set_yticks([])
    fig.tight_layout()
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue(), caption


def cmd_chart(payload):
    target = (payload.get("ticker") or "portfolio").upper().strip()
    if target in ("PORTFOLIO", "ALL", "PF"):
        result = _render_portfolio_chart()
        title = "📈 Portfolio Chart"
    else:
        result = _render_stock_chart(target)
        title = f"📈 {target} Chart"
    if result is None:
        _reply(payload, [_embed("❌ Chart failed",
                                f"No price data found for `{target}`. Check the ticker symbol.", _RED)])
        return
    png, caption = result
    embed = _embed(title, caption, _BLUE)
    embed["image"] = {"url": "attachment://chart.png"}
    posted = dn.post_image([embed], png)
    # Resolve the deferred slash response
    if posted:
        _reply(payload, [_embed(title, "Chart posted below ⬇️", _BLUE)])
    else:
        _reply(payload, [_embed("❌ Chart upload failed", "Bot couldn't attach the image — check DISCORD_BOT_TOKEN.", _RED)])


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
    trigger = payload.get("trigger", "")
    if not ticker:
        _reply(payload, [_embed("❌ Error", "No ticker in approval payload.", _RED)])
        return
    client = get_client()
    positions = get_positions(client)
    if ticker not in positions:
        _finalize_alert(payload, f"⚠️ No open {ticker} position — nothing sold")
        dn.post_message([_embed(f"⚠️ {ticker}", "Position no longer exists — no order placed.", _ORANGE)])
        return
    price_now = positions[ticker]["current_price"]
    r = close_position(client, ticker)
    ok = r.get("status") not in ("failed",)
    _journal({
        "decision": "approve_sell", "ticker": ticker, "trigger": trigger,
        "price_at_decision": price_now, "order_status": r.get("status"),
        "pnl_pct_at_decision": round(positions[ticker]["unrealized_plpc"] * 100, 2),
    })
    if trigger == "mr_exit":
        try:
            from strategies.mean_reversion import remove_from_sleeve
            remove_from_sleeve(ticker)
        except Exception as exc:
            log.warning("Sleeve state update failed: %s", exc)
    _finalize_alert(
        payload,
        f"✅ Approved by you — sell submitted {datetime.now(timezone.utc).strftime('%H:%M UTC')}" if ok
        else "❌ Approved but order FAILED",
        {"name": "Order", "value": f"{r.get('status')} (id: {r.get('order_id', 'n/a')})", "inline": False},
    )
    dn.post_message([_embed(
        f"{'✅' if ok else '❌'} SELL {'submitted' if ok else 'FAILED'} — {ticker}",
        f"Trigger: {trigger.replace('_', ' ') or '?'}\nStatus: **{r.get('status')}**"
        + (f"\nError: {r.get('error')}" if r.get("error") else ""),
        _GREEN if ok else _RED,
    )])


def cmd_approve_buy(payload):
    """Sleeve BUY approved (mean-reversion proposals). Sized from sleeve budget."""
    ticker = payload.get("ticker", "").upper()
    if not ticker:
        _reply(payload, [_embed("❌ Error", "No ticker in buy payload.", _RED)])
        return
    client = get_client()
    if ticker in get_positions(client):
        _finalize_alert(payload, f"⚠️ {ticker} already held — no additional buy")
        return

    from broker.alpaca_client import place_market_order
    from broker import market_data
    acct = get_account_summary(client)
    sleeve_pct = getattr(config, "MR_SLEEVE_PCT", 0.10)
    max_pos = getattr(config, "MR_MAX_POSITIONS", 5)
    notional = acct["equity"] * sleeve_pct / max_pos
    prices = market_data.get_latest_prices([ticker])
    price = prices.get(ticker)
    if not price:
        _finalize_alert(payload, f"❌ Could not price {ticker} — no order placed")
        return
    qty = round(notional / price, 4)
    r = place_market_order(client, ticker, qty, "buy")
    ok = r.get("status") not in ("failed", None)
    _journal({
        "decision": "approve_buy", "ticker": ticker, "trigger": payload.get("trigger", "mr"),
        "price_at_decision": price, "qty": qty, "order_status": r.get("status"),
    })
    if ok:
        try:
            from strategies.mean_reversion import add_to_sleeve
            add_to_sleeve(ticker, price, qty)
        except Exception as exc:
            log.warning("Sleeve state update failed: %s", exc)
    _finalize_alert(
        payload,
        f"✅ Approved — BUY {qty} {ticker} submitted" if ok else "❌ Approved but order FAILED",
        {"name": "Order", "value": f"{r.get('status')} (id: {r.get('order_id', 'n/a')})", "inline": False},
    )
    dn.post_message([_embed(
        f"{'✅' if ok else '❌'} BUY {'submitted' if ok else 'FAILED'} — {ticker}",
        f"Mean-reversion sleeve · {qty} shares @ ~${price:.2f} (≈${notional:,.0f})\n"
        f"Status: **{r.get('status')}**" + (f"\nError: {r.get('error')}" if r.get("error") else ""),
        _GREEN if ok else _RED,
    )])


def cmd_reject(payload):
    ticker = payload.get("ticker", "").upper()
    if ticker:
        prices = {}
        try:
            from broker import market_data
            prices = market_data.get_latest_prices([ticker])
        except Exception:
            pass
        _journal({
            "decision": "reject", "ticker": ticker,
            "trigger": payload.get("trigger", ""),
            "price_at_decision": prices.get(ticker),
        })
    # The worker already updated the message UI — journal is the real work here.


# ── Weekly rebalance approval ───────────────────────────────────────────────

def cmd_approve_rebalance(payload):
    """
    Owner tapped 'Approve' on the weekly rebalance proposal.
    Loads data/proposed_rebalance.json (written by post_weekly_proposal.py),
    validates it hasn't expired, then executes the pipeline with --execute.
    """
    from datetime import datetime, timezone

    proposal_path = config.DATA_DIR / "proposed_rebalance.json"
    if not proposal_path.exists():
        _reply(payload, [_embed(
            "❌ No proposal found",
            "Could not find `data/proposed_rebalance.json`. "
            "The weekly analysis may not have run yet, or the file was not committed.",
            _RED,
        )])
        return

    try:
        proposal = json.loads(proposal_path.read_bytes().rstrip(b"\x00"))
    except Exception as e:
        _reply(payload, [_embed("❌ Proposal unreadable", f"```\n{e}\n```", _RED)])
        return

    # Expiry check
    expires_at = proposal.get("expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at)
            if datetime.now(timezone.utc) > expiry:
                _reply(payload, [_embed(
                    "⏰ Proposal expired",
                    f"This proposal expired at {expiry.strftime('%H:%M UTC')}. "
                    "The next proposal will arrive Monday morning.",
                    _ORANGE,
                )])
                return
        except Exception:
            pass  # if we can't parse expiry, proceed anyway

    # Check proposal was from a successful run
    if not proposal.get("run_ok", True):
        _reply(payload, [_embed(
            "⚠️ Pipeline failed — cannot execute",
            "The pipeline analysis that generated this proposal reported a failure. "
            "Check the GitHub Actions log and re-run manually before approving.",
            _RED,
        )])
        return

    regime    = proposal.get("regime", "unknown")
    summary   = proposal.get("signal_summary") or {}
    n_buy     = summary.get("buy", 0)
    n_exit    = summary.get("exit", 0)
    proposed  = proposal.get("proposed_at", "?")[:10]

    _journal({"decision": "approve_rebalance", "regime": regime,
              "buy": n_buy, "exit": n_exit, "proposed_at": proposed})

    # Immediate acknowledgement (execution takes time)
    _reply(payload, [_embed(
        "✅ Rebalance approved — executing now",
        f"Regime: **{regime.upper()}** | {n_buy} BUY · {n_exit} EXIT\n"
        "Orders are being placed via Alpaca paper account. "
        "Results will post here when done (~30-60 seconds).",
        _GREEN,
    )])

    # Execute — this is the actual trade submission
    ok, out = _run_pipeline(execute=True)
    _journal({"decision": "approve_rebalance_executed", "success": ok})

    dn.post_message([_embed(
        "🚀 Rebalance executed" if ok else "❌ Rebalance execution FAILED",
        f"```\n{out[-2000:]}\n```",
        _GREEN if ok else _RED,
        footer="Pipeline Alpaca account · Paper trading",
    )])


def cmd_reject_rebalance(payload):
    """Owner tapped Reject — log and do nothing (worker already updated the UI)."""
    _journal({"decision": "reject_rebalance", "reason": "manual reject via Discord"})


# ── Entry point ────────────────────────────────────────────────────────────

COMMANDS = {
    "status":              cmd_status,
    "regime":              cmd_regime,
    "strategy":            cmd_strategy,
    "chart":               cmd_chart,
    "monitor_check":       cmd_monitor_check,
    "stoploss_check":      cmd_stoploss_check,
    "stoploss_execute":    cmd_stoploss_execute,
    "pipeline_dry":        cmd_pipeline_dry,
    "pipeline_execute":    cmd_pipeline_execute,
    "approve_sell":        cmd_approve_sell,
    "approve_buy":         cmd_approve_buy,
    "reject":              cmd_reject,
    "approve_rebalance":   cmd_approve_rebalance,
    "reject_rebalance":    cmd_reject_rebalance,
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
        dn.post_message([_embed("Unknown command", f"`{command}` is not recognised.", _RED)])
        sys.exit(1)

    log.info("Dispatching command: %s", command)
    try:
        handler(payload)
    except Exception as exc:
        log.error("Command %s failed: %s", command, exc, exc_info=True)
        _reply(payload, [_embed(f"{command} failed", f"```\n{exc}\n```", _RED)])
        sys.exit(1)


if __name__ == "__main__":
    main()
# build: 2026-06-10 wave-2
