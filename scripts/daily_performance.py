"""
scripts/daily_performance.py — Daily P&L tracker for both Alpaca accounts

Runs at 8 AM ET (screener_daily.yml, 8AM step only).
Posts a Discord card:
  - Screener + Pipeline daily P&L vs yesterday's close
  - SPY benchmark return for alpha comparison
  - Open P&L, invested %, buying power
  - Win rate from closed trades (data/trade_outcomes.json)

Pushes `performance_snapshot` key to Cloudflare KV so the Worker
can include live P&L in the /brief morning report.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from broker.alpaca_client import get_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
CF_API    = "https://api.cloudflare.com/client/v4"


# ── Cloudflare KV ─────────────────────────────────────────────────────────

def _kv_put(key: str, value: str) -> bool:
    acct = os.getenv("CF_ACCOUNT_ID", "").strip()
    ns   = os.getenv("CF_KV_NAMESPACE", "").strip()
    tok  = os.getenv("CF_API_TOKEN", "").strip()
    if not all([acct, ns, tok]):
        return False
    try:
        url = f"{CF_API}/accounts/{acct}/storage/kv/namespaces/{ns}/values/{key}"
        r = requests.put(
            url,
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "text/plain"},
            params={"expiration_ttl": 90000},
            data=value,
            timeout=10,
        )
        return r.status_code in (200, 201)
    except Exception as e:
        log.warning("KV write failed for %s: %s", key, e)
        return False


# ── Alpaca helpers ─────────────────────────────────────────────────────────

def _get_portfolio_stats(portfolio: str) -> dict:
    """Return daily P&L, open P&L, equity, buying_power for one account."""
    try:
        client = get_client(portfolio)
        acct   = client.get_account()

        equity       = float(acct.equity)
        last_equity  = float(getattr(acct, "last_equity", None) or equity)
        daily_pl     = equity - last_equity
        daily_pl_pct = (daily_pl / last_equity * 100) if last_equity > 0 else 0.0

        positions  = client.get_all_positions()
        open_pl    = sum(float(p.unrealized_pl) for p in positions)
        invested   = sum(float(p.market_value)  for p in positions)
        pos_count  = len(positions)

        return {
            "equity":       round(equity, 2),
            "last_equity":  round(last_equity, 2),
            "daily_pl":     round(daily_pl, 2),
            "daily_pl_pct": round(daily_pl_pct, 3),
            "open_pl":      round(open_pl, 2),
            "buying_power": round(float(acct.buying_power), 2),
            "positions":    pos_count,
            "invested":     round(invested, 2),
            "invested_pct": round((invested / equity * 100) if equity > 0 else 0.0, 1),
        }
    except Exception as e:
        log.warning("Could not fetch %s account: %s", portfolio, e)
        return {"error": str(e)}


def _get_spy_return() -> float | None:
    """Get SPY 1-day % return via Alpaca data API."""
    key    = (os.getenv("ALPACA_API_KEY_SCREENER") or os.getenv("ALPACA_API_KEY", "")).strip()
    secret = (os.getenv("ALPACA_SECRET_KEY_SCREENER") or os.getenv("ALPACA_SECRET_KEY", "")).strip()
    if not key:
        return None
    try:
        r = requests.get(
            "https://data.alpaca.markets/v2/stocks/bars",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            params={"symbols": "SPY", "timeframe": "1Day", "limit": 2, "feed": "iex"},
            timeout=10,
        )
        if r.ok:
            bars = (r.json().get("bars") or {}).get("SPY", [])
            if len(bars) >= 2:
                return round((bars[-1]["c"] - bars[-2]["c"]) / bars[-2]["c"] * 100, 3)
    except Exception as e:
        log.warning("SPY fetch failed: %s", e)
    return None


def _get_win_stats() -> dict:
    """Read trade_outcomes.json for aggregate win-rate stats."""
    path = _DATA_DIR / "trade_outcomes.json"
    try:
        if path.exists():
            outcomes = json.loads(path.read_text(encoding="utf-8")).get("outcomes", [])
            if outcomes:
                wins   = [o for o in outcomes if o.get("win")]
                losses = [o for o in outcomes if not o.get("win")]
                avg_w  = sum(o.get("pnl_pct", 0) for o in wins)   / len(wins)   if wins   else 0
                avg_l  = sum(o.get("pnl_pct", 0) for o in losses) / len(losses) if losses else 0
                wr     = len(wins) / len(outcomes) * 100
                return {
                    "total":      len(outcomes),
                    "wins":       len(wins),
                    "win_rate":   round(wr, 1),
                    "avg_win":    round(avg_w, 2),
                    "avg_loss":   round(avg_l, 2),
                    "expectancy": round(wr / 100 * avg_w + (1 - wr / 100) * avg_l, 2),
                }
    except Exception as e:
        log.warning("Could not read trade_outcomes.json: %s", e)
    return {"total": 0}


# ── Discord post ───────────────────────────────────────────────────────────

def _post_discord(sc: dict, pc: dict, spy_ret, win: dict):
    bot_token  = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.getenv("DISCORD_CHANNEL_ID", "").strip()
    if not bot_token or not channel_id:
        log.warning("No DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID — skipping Discord post")
        return

    sc_ok = "error" not in sc
    pc_ok = "error" not in pc

    def _pnl(val, pct=None):
        s = f"${val:+,.0f}"
        return s + f" ({pct:+.1f}%)" if pct is not None else s

    total_daily = (sc.get("daily_pl", 0) if sc_ok else 0) + (pc.get("daily_pl", 0) if pc_ok else 0)
    color = 0x2ECC71 if total_daily >= 0 else 0xE74C3C
    today = datetime.now(timezone.utc).strftime("%a %b %d, %Y")

    fields = []
    if sc_ok:
        fields += [
            {"name": "📊 Screener — Daily",    "value": _pnl(sc["daily_pl"], sc["daily_pl_pct"]), "inline": True},
            {"name": "📊 Screener — Open P&L", "value": _pnl(sc["open_pl"]),                       "inline": True},
            {"name": "📊 Screener — Invested", "value": f"{sc['invested_pct']:.0f}% | {sc['positions']} pos | BP ${sc['buying_power']:,.0f}", "inline": True},
        ]
    if pc_ok:
        fields += [
            {"name": "🔄 Pipeline — Daily",    "value": _pnl(pc["daily_pl"], pc["daily_pl_pct"]), "inline": True},
            {"name": "🔄 Pipeline — Open P&L", "value": _pnl(pc["open_pl"]),                       "inline": True},
            {"name": "🔄 Pipeline — Invested", "value": f"{pc['invested_pct']:.0f}% | {pc['positions']} pos | BP ${pc['buying_power']:,.0f}", "inline": True},
        ]

    if spy_ret is not None:
        alpha_parts = []
        if sc_ok: alpha_parts.append(f"Screener α {sc['daily_pl_pct'] - spy_ret:+.1f}%")
        if pc_ok: alpha_parts.append(f"Pipeline α {pc['daily_pl_pct'] - spy_ret:+.1f}%")
        fields.append({
            "name":   f"📈 SPY {spy_ret:+.2f}%",
            "value":  "  ·  ".join(alpha_parts) or "—",
            "inline": False,
        })

    if win.get("total", 0) > 0:
        fields.append({
            "name": f"🎯 Win Rate — {win['wins']}/{win['total']} closed",
            "value": (
                f"**{win['win_rate']:.0f}%** · avg win {win['avg_win']:+.1f}% · "
                f"avg loss {win['avg_loss']:+.1f}% · expectancy {win['expectancy']:+.1f}%"
            ),
            "inline": False,
        })

    r = requests.post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
        json={"embeds": [{
            "title":     f"📊 Daily Performance — {today}",
            "color":     color,
            "fields":    fields,
            "footer":    {"text": "Investment Alpha · Screener + Pipeline · Paper trading"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]},
        timeout=10,
    )
    if r.ok:
        log.info("Performance card posted to Discord ✓")
    else:
        log.warning("Discord post failed %d: %s", r.status_code, r.text[:200])


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    log.info("=== Daily Performance Tracker ===")
    sc      = _get_portfolio_stats("screener")
    pc      = _get_portfolio_stats("pipeline")
    spy_ret = _get_spy_return()
    win     = _get_win_stats()

    snapshot = {
        "date":      datetime.now(timezone.utc).date().isoformat(),
        "screener":  sc,
        "pipeline":  pc,
        "spy_daily": spy_ret,
        "win_stats": win,
    }

    # Save to disk
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    snap_path = _DATA_DIR / "performance_snapshot.json"
    snap_path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    log.info("Snapshot → %s", snap_path)

    # Push to KV for Worker /brief
    ok = _kv_put("performance_snapshot", json.dumps(snapshot, default=str))
    log.info("KV write: %s", "OK" if ok else "skipped (no CF creds)")

    # Post Discord card
    _post_discord(sc, pc, spy_ret, win)

    log.info(
        "Done — SPY %s | Screener $%+,.0f | Pipeline $%+,.0f",
        f"{spy_ret:+.2f}%" if spy_ret is not None else "n/a",
        sc.get("daily_pl", 0) if "error" not in sc else 0,
        pc.get("daily_pl", 0) if "error" not in pc else 0,
    )


if __name__ == "__main__":
    main()
