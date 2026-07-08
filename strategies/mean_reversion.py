"""
strategies/mean_reversion.py — Short-Term Pullback Sleeve (paper, approval-only)

THE EDGE: liquid stocks in long-term uptrends that suffer short sharp dips
tend to snap back within days. This is the classic complement to your core
momentum book — it makes money in choppy tape where momentum bleeds.

MECHANICS (entry):
    - Stock in MR_UNIVERSE (liquid mega/large caps)
    - Above its 200-day SMA  (only buy dips in uptrends)
    - RSI(2) < MR_RSI_ENTRY (default 10) — a violent short-term dip
    - Not already held (core portfolio or sleeve)
MECHANICS (exit):
    - Close > 5-day SMA (the snap-back happened), or
    - Held > MR_MAX_HOLD_DAYS (time stop, default 10 trading days)

SIZING: sleeve gets MR_SLEEVE_PCT of equity (default 10%), equally split
across MR_MAX_POSITIONS slots (default 5 → ~2% of equity per trade).

CONTROL: this module only POSTS PROPOSALS to Discord with ✅/❌ buttons.
Buys/sells happen exclusively through your button press
(remote_commands.cmd_approve_buy / cmd_approve_sell with trigger mr_exit).

State: data/sleeve_mr.json (committed back to the repo by the workflow).
Run daily after the close via strategies.yml:
    python strategies/mean_reversion.py
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from broker import discord_notify as dn
from broker import market_data

log = logging.getLogger(__name__)

SLEEVE_FILE = config.DATA_DIR / "sleeve_mr.json"

# Liquid, optionable mega/large caps — fast to scan daily
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "AMD",
    "JPM", "BAC", "WFC", "GS", "V", "MA", "AXP",
    "UNH", "JNJ", "LLY", "ABBV", "PFE", "TMO",
    "XOM", "CVX", "COP", "HD", "LOW", "COST", "WMT", "TGT", "MCD", "NKE",
    "DIS", "NFLX", "CRM", "ORCL", "ADBE", "INTC", "QCOM", "TXN", "MU",
    "CAT", "DE", "BA", "GE", "HON", "UPS", "RTX", "LMT",
    "T", "VZ", "PG", "KO", "PEP", "ABT", "MRNA", "GILD",
]

_GREEN, _ORANGE, _BLUE = 0x2ECC71, 0xE67E22, 0x3498DB


# ── Sleeve state ───────────────────────────────────────────────────────────

def load_sleeve() -> dict:
    if not SLEEVE_FILE.exists():
        return {}
    try:
        raw = SLEEVE_FILE.read_bytes().rstrip(b"\x00")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _save_sleeve(s: dict) -> None:
    SLEEVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SLEEVE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def add_to_sleeve(ticker: str, price: float, qty: float) -> None:
    s = load_sleeve()
    s[ticker.upper()] = {
        "entry_price": round(price, 4),
        "qty": qty,
        "entry_date": datetime.now(timezone.utc).date().isoformat(),
    }
    _save_sleeve(s)


def remove_from_sleeve(ticker: str) -> None:
    s = load_sleeve()
    if ticker.upper() in s:
        del s[ticker.upper()]
        _save_sleeve(s)


# ── Indicators ─────────────────────────────────────────────────────────────

def _rsi(closes, period: int = 2) -> float | None:
    """Wilder's RSI on a pandas Series of closes."""
    if closes is None or len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    val = float((100 - 100 / (1 + rs)).iloc[-1])
    return round(val, 2) if val == val else None


# ── Signal scan ────────────────────────────────────────────────────────────

def scan() -> dict:
    """One daily scan. Posts entry/exit proposals to Discord. Returns summary."""
    if not getattr(config, "MR_ENABLED", True):
        log.info("Mean-reversion sleeve disabled (MR_ENABLED=False)")
        return {"entries": 0, "exits": 0}

    universe = getattr(config, "MR_UNIVERSE", DEFAULT_UNIVERSE)
    rsi_entry = getattr(config, "MR_RSI_ENTRY", 10)
    max_pos = getattr(config, "MR_MAX_POSITIONS", 5)
    max_hold = getattr(config, "MR_MAX_HOLD_DAYS", 10)

    # Current holdings (core + sleeve) — never double-buy
    held_core = set()
    alpaca_positions = {}
    alpaca_ok = False
    try:
        from broker.alpaca_client import get_client, get_positions
        alpaca_positions = get_positions(get_client())
        held_core = set(alpaca_positions.keys())
        alpaca_ok = True
    except Exception as exc:
        log.warning("Could not fetch Alpaca positions (%s) — proposals still posted", exc)
    sleeve = load_sleeve()

    # ── Auto-reconcile: remove sleeve entries no longer held in Alpaca ────
    # Catches positions closed manually (e.g. /sell) that bypassed the
    # "Approve SELL" Discord button, so the strategy stops re-proposing exits.
    if alpaca_ok:
        ghost_tickers = [t for t in list(sleeve.keys()) if t not in alpaca_positions]
        for ticker in ghost_tickers:
            log.warning(
                "Sleeve reconcile: %s found in sleeve_mr.json but NOT in Alpaca — "
                "removing silently (likely sold outside MR flow).", ticker
            )
            remove_from_sleeve(ticker)
            sleeve.pop(ticker, None)

    n_entries = n_exits = 0

    # ── Exits first (free up slots) ────────────────────────────────────
    today = datetime.now(timezone.utc).date()
    for ticker, pos in list(sleeve.items()):
        closes = market_data.get_daily_closes(ticker, days=30)
        if closes is None or len(closes) < 6:
            continue
        price = float(closes.iloc[-1])
        sma5 = float(closes.rolling(5).mean().iloc[-1])
        held_days = (today - datetime.fromisoformat(pos["entry_date"]).date()).days
        snap_back = price > sma5
        time_stop = held_days > max_hold * 1.5  # calendar≈trading buffer
        if not (snap_back or time_stop):
            continue
        pnl = (price - pos["entry_price"]) / pos["entry_price"] * 100
        reason = "snapped back above 5-day MA" if snap_back else f"time stop ({held_days}d held)"
        embed = {
            "title": f"🔁 MR SLEEVE EXIT — {ticker}",
            "description": (f"**{ticker}** {reason}.\nEntry ${pos['entry_price']:.2f} → "
                            f"now ${price:.2f} (**{pnl:+.1f}%**).\n"
                            f"Tap to close the sleeve position — nothing happens without you."),
            "color": _GREEN if pnl > 0 else _ORANGE,
            "fields": [
                {"name": "Strategy", "value": "Mean reversion", "inline": True},
                {"name": "Held", "value": f"{held_days}d", "inline": True},
                {"name": "P&L", "value": f"{pnl:+.1f}%", "inline": True},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Investment Alpha — MR sleeve"},
        }
        if not dn.recent_alert_exists(ticker, "mr_exit"):
            dn.post_message([embed], components=dn.approval_buttons(ticker, "mr_exit"))
            n_exits += 1

    # ── Entries ────────────────────────────────────────────────────────
    open_slots = max_pos - len(sleeve)
    if open_slots <= 0:
        log.info("Sleeve full (%d/%d) — no entry proposals", len(sleeve), max_pos)
        return {"entries": 0, "exits": n_exits}

    candidates = []
    for ticker in universe:
        if ticker in held_core or ticker in sleeve:
            continue
        closes = market_data.get_daily_closes(ticker, days=250)
        if closes is None or len(closes) < 200:
            continue
        price = float(closes.iloc[-1])
        sma200 = float(closes.rolling(200).mean().iloc[-1])
        if price <= sma200:
            continue
        rsi2 = _rsi(closes, 2)
        if rsi2 is None or rsi2 >= rsi_entry:
            continue
        dip_3d = (price / float(closes.iloc[-4]) - 1) * 100 if len(closes) >= 4 else 0
        candidates.append({"ticker": ticker, "price": price, "rsi2": rsi2,
                           "above_200ma": (price / sma200 - 1) * 100, "dip_3d": dip_3d})

    candidates.sort(key=lambda c: c["rsi2"])  # most oversold first
    for c in candidates[:open_slots]:
        if dn.recent_alert_exists(c["ticker"], "mr"):
            continue
        embed = {
            "title": f"📉→📈 MR SLEEVE BUY SIGNAL — {c['ticker']}",
            "description": (
                f"**{c['ticker']}** is in a long-term uptrend but just had a sharp dip — "
                f"the classic snap-back setup.\n"
                f"Sized ≈{getattr(config, 'MR_SLEEVE_PCT', 0.10)*100/ max_pos:.0f}% of equity. "
                f"**Nothing happens without your tap.**"
            ),
            "color": _BLUE,
            "fields": [
                {"name": "Price", "value": f"${c['price']:.2f}", "inline": True},
                {"name": "RSI(2)", "value": f"{c['rsi2']:.0f} (<{rsi_entry})", "inline": True},
                {"name": "vs 200MA", "value": f"+{c['above_200ma']:.1f}%", "inline": True},
                {"name": "3-day move", "value": f"{c['dip_3d']:+.1f}%", "inline": True},
                {"name": "Exit plan", "value": "Close > 5-day MA, or 10-day time stop", "inline": False},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Investment Alpha — MR sleeve (paper)"},
        }
        comps = [{
            "type": 1,
            "components": [
                {"type": 2, "style": 3, "label": f"✅ Approve BUY {c['ticker']}",
                 "custom_id": f"ia|approve_buy|{c['ticker']}|mr"},
                {"type": 2, "style": 4, "label": "❌ Skip",
                 "custom_id": f"ia|reject|{c['ticker']}|mr"},
            ],
        }]
        dn.post_message([embed], components=comps)
        n_entries += 1

    log.info("MR scan: %d entry proposal(s), %d exit proposal(s), sleeve %d/%d",
             n_entries, n_exits, len(sleeve), max_pos)
    return {"entries": n_entries, "exits": n_exits}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(scan(), indent=2))
