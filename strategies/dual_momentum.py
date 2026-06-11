"""
strategies/dual_momentum.py — Monthly Asset-Class Compass (advisory only)

Gary Antonacci's dual momentum, the simplest whole-portfolio risk switch:

  1. RELATIVE momentum: rank US stocks (SPY) vs international (VEU) vs
     bonds (AGG) by 12-month total return.
  2. ABSOLUTE momentum: if even the winner is below T-bills (BIL), the
     correct position is cash/defensive — nothing has positive expectancy.

This module is ADVISORY ONLY — it posts a monthly compass card to Discord.
It never trades. Its job is to warn you when the whole equity complex loses
absolute momentum, which historically precedes the deep drawdowns your
stock-level stops can't fully protect against.

Run monthly via strategies.yml:  python strategies/dual_momentum.py
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from broker import discord_notify as dn

log = logging.getLogger(__name__)

ASSETS = {
    "SPY": "US stocks",
    "VEU": "International stocks",
    "AGG": "US bonds",
    "BIL": "T-bills (cash)",
}
LOOKBACK_MONTHS = 12

_GREEN, _ORANGE, _RED = 0x2ECC71, 0xE67E22, 0xE74C3C


def compute() -> dict | None:
    import yfinance as yf
    try:
        raw = yf.download(list(ASSETS), period="14mo", auto_adjust=True, progress=False)["Close"]
        returns = {}
        for t in ASSETS:
            s = raw[t].dropna()
            if len(s) < 200:
                continue
            returns[t] = round((float(s.iloc[-1]) / float(s.iloc[0]) - 1) * 100, 2)
        if "SPY" not in returns or "BIL" not in returns:
            return None
        risk_assets = {t: r for t, r in returns.items() if t in ("SPY", "VEU", "AGG")}
        winner = max(risk_assets, key=risk_assets.get)
        absolute_ok = returns[winner] > returns["BIL"]
        return {
            "returns": returns,
            "winner": winner,
            "absolute_ok": absolute_ok,
            "stance": ("risk-on" if (absolute_ok and winner in ("SPY", "VEU"))
                       else "defensive"),
        }
    except Exception as exc:
        log.error("Dual momentum data fetch failed: %s", exc)
        return None


def post_card() -> None:
    if not getattr(config, "DM_ENABLED", True):
        log.info("Dual momentum disabled (DM_ENABLED=False)")
        return
    r = compute()
    if r is None:
        dn.post_message([{
            "title": "🧭 Dual Momentum — data unavailable",
            "description": "Could not compute this month's compass. Will retry next run.",
            "color": _ORANGE,
        }])
        return

    lines = []
    for t, label in ASSETS.items():
        ret = r["returns"].get(t)
        marker = " ← **winner**" if t == r["winner"] else ""
        lines.append(f"**{t}** ({label}): {ret:+.1f}% (12-mo){marker}")

    if r["stance"] == "risk-on":
        verdict = (f"✅ **RISK-ON.** {r['winner']} leads and beats T-bills — the equity "
                   f"strategy is swimming with the tide. No action needed.")
        color = _GREEN
    else:
        verdict = ("⚠️ **DEFENSIVE.** Equities have lost absolute momentum vs T-bills. "
                   "Historically this is when deep drawdowns happen. Consider: smaller "
                   "position count, tighter stops, or letting cash build on the next "
                   "rebalance. (Advisory only — nothing is traded automatically.)")
        color = _RED

    dn.post_message([{
        "title": f"🧭 Monthly Asset Compass — {r['stance'].upper()}",
        "description": "\n".join(lines) + "\n\n" + verdict,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Dual momentum (Antonacci) — advisory, never trades"},
    }])
    log.info("Dual momentum: %s (winner=%s)", r["stance"], r["winner"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    post_card()
