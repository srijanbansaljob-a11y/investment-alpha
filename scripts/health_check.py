"""
scripts/health_check.py — Daily system QA / integrity checker

WHY THIS EXISTS
---------------
GitHub Actions already alerts when a workflow *crashes*. It does not alert when
a workflow runs "successfully" while producing wrong output. Every significant
bug found on 2026-07-25 was of the second kind — silent:

  * Position sizing used `buying_power` (2.7x equity on margin accounts), so
    "5% per position" bought 13.5% of equity. The screener account sat at 108%
    invested with -$8,766 cash. Nothing alerted.
  * Stop-loss maths existed in three independent copies that could drift apart.
  * pipeline/congressional.py and scripts/fetch_congressional_trades.py wrote
    incompatible schemas to the same cache filename; whichever ran last won.
  * trade_outcome_logger._find_entry_date() returned None every time, so every
    logged trade had duration 0 — the dataset built to validate the strategy
    was itself quietly broken for ~2 weeks.

This script asserts the things that must be true and shouts when they are not.
It is the difference between "no news is good news" and "no news means nobody
is looking".

CHECKS
------
  ACCOUNT      cash >= 0, no margin usage, exposure within regime cap,
               account not restricted/blocked
  RISK         every open position resolves to a sane stop price
  RECONCILE    broker positions vs local state agree
  FRESHNESS    today's snapshot written, screener/regime data not stale
  DATA SANITY  factor values within plausible bounds (no NaN floods,
               no ROE > 500%, no negative-equity ratios leaking through)
  TRADE LOG    logged outcomes have entry dates and plausible durations

SEVERITY
--------
  RED    → something is wrong that can lose money. Exits non-zero so the
           GitHub Actions job FAILS and the failure alert fires.
  AMBER  → degraded but not dangerous. Reported, exit 0.
  GREEN  → all good.

Usage:
    python scripts/health_check.py                 # both accounts
    python scripts/health_check.py --portfolio pipeline
    python scripts/health_check.py --no-discord    # console only
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_SNAP_DIR = _DATA_DIR / "position_snapshots"

RED, AMBER, GREEN = "RED", "AMBER", "GREEN"
_SEVERITY_ORDER = {GREEN: 0, AMBER: 1, RED: 2}
_EMOJI = {GREEN: "✅", AMBER: "⚠️", RED: "🚨"}
_COLOUR = {GREEN: 0x2ECC71, AMBER: 0xE67E22, RED: 0xE74C3C}


class Findings:
    """Collects check results and tracks the worst severity seen."""

    def __init__(self):
        self.items = []      # (severity, check_name, message)

    def add(self, severity, check, message):
        self.items.append((severity, check, message))
        if severity != GREEN:
            log.warning("[%s] %s — %s", severity, check, message)
        else:
            log.info("[GREEN] %s — %s", check, message)

    @property
    def worst(self):
        if not self.items:
            return GREEN
        return max((s for s, _, _ in self.items), key=lambda s: _SEVERITY_ORDER[s])

    def by_severity(self, severity):
        return [(c, m) for s, c, m in self.items if s == severity]


# ── Helpers ────────────────────────────────────────────────────────────────

def _load_json(path: Path):
    try:
        if path.exists():
            raw = path.read_bytes().rstrip(b"\x00")
            return json.loads(raw) if raw else None
    except Exception as exc:
        log.debug("Could not read %s: %s", path, exc)
    return None


def _regime_label_and_cap():
    """Current pipeline regime → (label, max_invested_pct). Falls back safely."""
    try:
        from pipeline import regime as regime_module
        result = regime_module.run()
        raw = (result.get("regime") or "").lower().strip()
        mapping = {"bull": "MOD BULL", "neutral": "NEUTRAL", "bear": "BEARISH"}
        label = mapping.get(raw, "")
    except Exception as exc:
        log.warning("Regime lookup failed (%s) — using default cap", exc)
        label = ""
    pcts = getattr(config, "MAX_INVESTED_PCTS", {})
    cap = pcts.get(label, getattr(config, "MAX_INVESTED_DEFAULT", 0.80))
    return label or "UNKNOWN", cap


# ── Checks ─────────────────────────────────────────────────────────────────

def check_account(f: Findings, portfolio: str, regime_label: str, cap: float):
    """Cash, margin usage, exposure vs regime cap, account status."""
    try:
        from broker.alpaca_client import get_client, get_positions, get_account_summary
        client = get_client(portfolio)
        acct = get_account_summary(client)
        positions = get_positions(client)
    except Exception as exc:
        f.add(RED, f"{portfolio}:account", f"Could not reach Alpaca: {exc}")
        return None

    equity = acct.get("equity", 0.0)
    cash = acct.get("cash", 0.0)
    invested = sum(p.get("market_value", 0.0) for p in positions.values())
    inv_pct = (invested / equity) if equity > 0 else 0.0

    status = str(acct.get("status", "")).upper()
    if "ACTIVE" not in status:
        f.add(RED, f"{portfolio}:status", f"Account status is {status!r}, expected ACTIVE")
    else:
        f.add(GREEN, f"{portfolio}:status", f"Account ACTIVE · equity ${equity:,.0f}")

    # THE 108% BUG GUARD — negative cash means positions are financed on margin.
    if cash < 0:
        f.add(RED, f"{portfolio}:margin",
              f"NEGATIVE CASH ${cash:,.0f} — positions are on margin. "
              f"Invested {inv_pct*100:.0f}% of ${equity:,.0f} equity.")
    elif inv_pct > 1.0:
        f.add(RED, f"{portfolio}:margin",
              f"Invested {inv_pct*100:.0f}% of equity (>100%) — leverage in use.")
    else:
        f.add(GREEN, f"{portfolio}:margin", f"No margin · cash ${cash:,.0f}")

    # Exposure vs regime cap: over the cap is not dangerous by itself (it blocks
    # new buys rather than forcing sales), so AMBER not RED.
    if inv_pct > cap:
        f.add(AMBER, f"{portfolio}:exposure",
              f"{inv_pct*100:.0f}% invested vs {cap*100:.0f}% cap ({regime_label}) — "
              f"new entries blocked until positions close or regime improves.")
    else:
        f.add(GREEN, f"{portfolio}:exposure",
              f"{inv_pct*100:.0f}% invested (cap {cap*100:.0f}%, {regime_label})")

    return {"equity": equity, "cash": cash, "positions": positions,
            "invested": invested, "inv_pct": inv_pct}


def check_stops(f: Findings, portfolio: str, positions: dict):
    """Every open position must resolve to a sane stop price."""
    if not positions:
        f.add(GREEN, f"{portfolio}:stops", "No open positions")
        return
    try:
        from broker.stop_loss import compute_stop_price
    except Exception as exc:
        f.add(RED, f"{portfolio}:stops", f"Cannot import stop-loss module: {exc}")
        return

    unresolved, absurd = [], []
    for ticker, pos in positions.items():
        entry = pos.get("avg_entry_price") or 0.0
        current = pos.get("current_price") or 0.0
        if entry <= 0:
            unresolved.append(f"{ticker}(no entry price)")
            continue
        try:
            stop, _method, _atr = compute_stop_price(ticker, entry, "neutral")
        except Exception as exc:
            unresolved.append(f"{ticker}({exc})")
            continue
        if stop is None or stop <= 0:
            unresolved.append(f"{ticker}(stop={stop})")
        elif stop >= entry:
            absurd.append(f"{ticker}(stop ${stop:.2f} >= entry ${entry:.2f})")
        elif current > 0 and stop < entry * 0.5:
            absurd.append(f"{ticker}(stop ${stop:.2f} is >50% below entry)")

    if unresolved:
        f.add(RED, f"{portfolio}:stops",
              f"{len(unresolved)} position(s) have NO usable stop: {', '.join(unresolved[:5])}")
    elif absurd:
        f.add(RED, f"{portfolio}:stops",
              f"{len(absurd)} implausible stop level(s): {', '.join(absurd[:5])}")
    else:
        f.add(GREEN, f"{portfolio}:stops", f"All {len(positions)} positions have valid stops")


def check_reconciliation(f: Findings, portfolio: str, positions: dict):
    """Broker truth vs today's local snapshot."""
    today = datetime.now(timezone.utc).date().isoformat()
    snap = _load_json(_SNAP_DIR / f"positions_{today}.json")
    if snap is None:
        f.add(AMBER, f"{portfolio}:reconcile",
              f"No snapshot for {today} yet (written after close) — cannot reconcile")
        return
    local = set((snap.get(portfolio) or {}).keys())
    broker = set(positions.keys())
    only_broker, only_local = broker - local, local - broker
    if only_broker or only_local:
        parts = []
        if only_broker:
            parts.append(f"in broker not local: {', '.join(sorted(only_broker))}")
        if only_local:
            parts.append(f"in local not broker: {', '.join(sorted(only_local))}")
        f.add(AMBER, f"{portfolio}:reconcile", " · ".join(parts))
    else:
        f.add(GREEN, f"{portfolio}:reconcile", f"{len(broker)} positions match snapshot")


def check_freshness(f: Findings):
    """Snapshots and cached screener/regime data must be recent."""
    snaps = sorted(_SNAP_DIR.glob("positions_*.json")) if _SNAP_DIR.exists() else []
    if not snaps:
        f.add(RED, "freshness:snapshots", "No position snapshots exist at all")
    else:
        newest = snaps[-1].stem.replace("positions_", "")
        try:
            age_days = (datetime.now(timezone.utc).date()
                        - datetime.fromisoformat(newest).date()).days
        except Exception:
            age_days = 99
        # Allow for weekends/holidays: >4 days means something stopped running.
        if age_days > 4:
            f.add(RED, "freshness:snapshots",
                  f"Newest snapshot is {newest} ({age_days}d old) — daily job may have stopped")
        elif age_days > 1:
            f.add(AMBER, "freshness:snapshots", f"Newest snapshot {newest} ({age_days}d old)")
        else:
            f.add(GREEN, "freshness:snapshots", f"Newest snapshot {newest}")

    perf = _load_json(_DATA_DIR / "performance_snapshot.json")
    if perf and perf.get("date"):
        try:
            age = (datetime.now(timezone.utc).date()
                   - datetime.fromisoformat(perf["date"]).date()).days
            if age > 4:
                f.add(AMBER, "freshness:performance", f"Performance snapshot {age}d old")
            else:
                f.add(GREEN, "freshness:performance", f"Performance data from {perf['date']}")
        except Exception:
            pass


def check_trade_log(f: Findings):
    """Outcome log integrity — this is the dataset used to validate the strategy."""
    data = _load_json(_DATA_DIR / "trade_outcomes.json")
    outcomes = (data or {}).get("outcomes", [])
    if not outcomes:
        f.add(AMBER, "tradelog:count", "No closed trades logged yet")
        return

    n = len(outcomes)
    missing_entry = [o for o in outcomes if not o.get("entry_date")]
    zero_dur = [o for o in outcomes if not o.get("duration_days")]
    bad_pnl = [o for o in outcomes if abs(o.get("pnl_pct") or 0) > 200]

    # This is exactly the bug fixed 2026-07-25 — guard so it cannot recur silently.
    if missing_entry:
        share = len(missing_entry) / n
        sev = RED if share > 0.5 else AMBER
        f.add(sev, "tradelog:entry_dates",
              f"{len(missing_entry)}/{n} trades missing entry_date "
              f"— duration stats unreliable (regression of the 2026-07 bug?)")
    else:
        f.add(GREEN, "tradelog:entry_dates", f"All {n} trades have entry dates")

    if zero_dur and len(zero_dur) == n:
        f.add(RED, "tradelog:duration", f"All {n} trades show duration 0 — logger likely broken")
    if bad_pnl:
        f.add(AMBER, "tradelog:pnl",
              f"{len(bad_pnl)} trade(s) with |P&L| > 200% — check for data errors")

    # Sample-size context for validation confidence
    if n < 30:
        f.add(AMBER, "tradelog:sample",
              f"{n} closed trades — below ~30 needed for meaningful win-rate inference")
    else:
        f.add(GREEN, "tradelog:sample", f"{n} closed trades logged")


def check_factor_sanity(f: Findings):
    """Cached fundamentals must be within plausible bounds."""
    cache_dir = getattr(config, "CACHE_DIR", None)
    if not cache_dir or not Path(cache_dir).exists():
        f.add(AMBER, "factors:cache", "No fundamentals cache present")
        return
    files = sorted(Path(cache_dir).glob("fundamentals_*.parquet"))
    if not files:
        f.add(AMBER, "factors:cache", "No fundamentals cache file found")
        return
    try:
        import pandas as pd
        df = pd.read_parquet(files[-1])
    except Exception as exc:
        f.add(AMBER, "factors:cache", f"Could not read fundamentals cache: {exc}")
        return

    issues = []
    n = len(df)
    if n == 0:
        f.add(RED, "factors:cache", "Fundamentals cache is empty")
        return

    for col, lo, hi in [("roe", -5.0, 5.0), ("roa", -2.0, 2.0),
                        ("gross_margins", -1.0, 1.0), ("operating_margins", -5.0, 1.0)]:
        if col in df.columns:
            bad = df[(df[col].notna()) & ((df[col] < lo) | (df[col] > hi))]
            if len(bad):
                issues.append(f"{col}: {len(bad)} out of range")
            null_share = df[col].isna().mean()
            if null_share > 0.5:
                issues.append(f"{col}: {null_share*100:.0f}% null")

    # Negative-equity leakage guard (MTCH-style)
    if "debt_to_equity" in df.columns:
        neg = df[(df["debt_to_equity"].notna()) & (df["debt_to_equity"] < 0)]
        if len(neg):
            issues.append(f"debt_to_equity: {len(neg)} negative (negative-equity leak)")

    if issues:
        f.add(AMBER, "factors:sanity", f"{n} rows · " + " · ".join(issues[:4]))
    else:
        f.add(GREEN, "factors:sanity", f"{n} rows, values within expected bounds")


# ── Discord ────────────────────────────────────────────────────────────────

def post_to_discord(f: Findings, summary_lines: list):
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    channel = os.getenv("DISCORD_CHANNEL_ID", "").strip()
    if not token or not channel:
        log.warning("No Discord credentials — console output only")
        return

    worst = f.worst
    reds = f.by_severity(RED)
    ambers = f.by_severity(AMBER)
    greens = f.by_severity(GREEN)

    fields = []
    if reds:
        fields.append({
            "name": f"🚨 Critical ({len(reds)})",
            "value": "\n".join(f"**{c}** — {m}" for c, m in reds)[:1020],
            "inline": False,
        })
    if ambers:
        fields.append({
            "name": f"⚠️ Warnings ({len(ambers)})",
            "value": "\n".join(f"**{c}** — {m}" for c, m in ambers)[:1020],
            "inline": False,
        })
    if summary_lines:
        fields.append({"name": "📊 Snapshot", "value": "\n".join(summary_lines)[:1020], "inline": False})
    fields.append({
        "name": "Checks run",
        "value": f"{len(greens)} passed · {len(ambers)} warned · {len(reds)} failed",
        "inline": False,
    })

    headline = {
        GREEN: "All systems healthy",
        AMBER: "Running with warnings",
        RED:   "ACTION NEEDED — critical issue detected",
    }[worst]

    try:
        r = requests.post(
            f"https://discord.com/api/v10/channels/{channel}/messages",
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            json={"embeds": [{
                "title": f"{_EMOJI[worst]} System Health — {headline}",
                "color": _COLOUR[worst],
                "fields": fields,
                "footer": {"text": "Investment Alpha · daily health check"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }]},
            timeout=10,
        )
        if r.ok:
            log.info("Health card posted to Discord ✓")
        else:
            log.warning("Discord post failed %d: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("Discord post error: %s", exc)


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Daily system health check")
    parser.add_argument("--portfolio", default="both",
                        choices=["both", "pipeline", "screener"])
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    log.info("=== System Health Check ===")
    f = Findings()

    regime_label, cap = _regime_label_and_cap()
    log.info("Regime: %s · exposure cap %.0f%%", regime_label, cap * 100)

    portfolios = ["pipeline", "screener"] if args.portfolio == "both" else [args.portfolio]
    summary_lines = []

    for pf in portfolios:
        state = check_account(f, pf, regime_label, cap)
        if state:
            check_stops(f, pf, state["positions"])
            check_reconciliation(f, pf, state["positions"])
            summary_lines.append(
                f"**{pf.title()}** ${state['equity']:,.0f} equity · "
                f"{state['inv_pct']*100:.0f}% invested · "
                f"${state['cash']:,.0f} cash · {len(state['positions'])} positions"
            )

    check_freshness(f)
    check_trade_log(f)
    check_factor_sanity(f)

    worst = f.worst
    log.info("--- Result: %s (%d checks) ---", worst, len(f.items))

    if not args.no_discord:
        post_to_discord(f, summary_lines)

    # Save machine-readable result
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        (_DATA_DIR / "health_check_latest.json").write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": worst,
            "regime": regime_label,
            "findings": [{"severity": s, "check": c, "message": m} for s, c, m in f.items],
        }, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not save health result: %s", exc)

    # RED exits non-zero so the workflow fails and the alert fires.
    if worst == RED:
        log.error("HEALTH CHECK FAILED — see critical items above")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
