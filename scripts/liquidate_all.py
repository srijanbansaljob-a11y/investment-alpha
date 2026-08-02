"""
scripts/liquidate_all.py — Controlled full liquidation for a clean restart.

WHY THIS EXISTS
---------------
The 11 open pipeline positions were all opened by pre-audit code: every one is
FRACTIONAL, so none can ever be fully covered by a broker-side stop (Alpaca
stops are whole-share only). More importantly, they and the 13 logged outcomes
were produced by a system with known structural faults — no cloud exits, no
resting stops, blackout and cooldown bypassed. As evidence about whether the
strategy works, that data is contaminated: it measures a system that no longer
exists.

Liquidating and rebuilding on fixed code buys a clean measurement baseline and
exercises the one path never yet run end to end.

SAFETY
------
  * Preview by default. Nothing is sold without --execute.
  * Refuses to run against a non-paper account.
  * --one sells a SINGLE position first, so the sell path is proven once
    rather than failing eleven times.
  * Every sell goes through sell_with_cleanup() — cancels the ticker's resting
    stop, waits for Alpaca to confirm, then closes. A bare close is rejected
    while shares are held against the stop.
  * Writes data/admin_exits.json so trade_outcome_logger tags these as
    ADMINISTRATIVE and the factor analysis excludes them from model evidence.
  * Verifies the end state: 0 positions AND 0 orphan orders.

USAGE
-----
    python scripts/liquidate_all.py                  # preview everything
    python scripts/liquidate_all.py --one            # preview single smallest
    python scripts/liquidate_all.py --one --execute  # sell ONE (do this first)
    python scripts/liquidate_all.py --execute        # sell the rest
    python scripts/liquidate_all.py --verify         # check the end state only
"""

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from broker.alpaca_client import (
    get_client, get_positions, get_account_summary, is_market_open,
    get_open_orders, get_resting_stops, sell_with_cleanup,
)

log = logging.getLogger("liquidate")

ADMIN_EXITS_FILE = config.DATA_DIR / "admin_exits.json"
REASON = "full_liquidation_clean_restart_on_fixed_code"


def _record_admin_exits(tickers, reason=REASON):
    """Tag these tickers so their exits never count as model evidence."""
    existing = {}
    try:
        if ADMIN_EXITS_FILE.exists():
            raw = ADMIN_EXITS_FILE.read_bytes().rstrip(b"\x00")
            existing = json.loads(raw) if raw else {}
    except Exception:
        existing = {}
    today = date.today().isoformat()
    for t in tickers:
        # Date-scoped key: a ticker can be administratively exited more than
        # once (EIX was, on 2026-07-27 and again on 2026-07-31). A plain
        # ticker key would hold only the latest and silently untag the rest.
        existing[f"{t}@{today}"] = {"date": today, "reason": reason}
    ADMIN_EXITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ADMIN_EXITS_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    log.info("Tagged %d ticker(s) as administrative exits -> %s",
             len(tickers), ADMIN_EXITS_FILE.name)


def _snapshot(positions, account):
    """Preserve the pre-liquidation book as history."""
    path = config.DATA_DIR / f"pre_liquidation_snapshot_{date.today().isoformat()}.json"
    payload = {
        "captured":  date.today().isoformat(),
        "reason":    REASON,
        "equity":    account.get("equity"),
        "cash":      account.get("cash"),
        "positions": {
            t: {"qty": p["qty"], "avg_entry_price": p["avg_entry_price"],
                "current_price": p["current_price"], "market_value": p["market_value"],
                "unrealized_pl": p["unrealized_pl"],
                "unrealized_plpc": round(p["unrealized_plpc"] * 100, 2)}
            for t, p in positions.items()
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("Pre-liquidation snapshot saved -> %s", path.name)
    return path


def verify(client) -> bool:
    """End state must be: no positions, no leftover orders."""
    positions = get_positions(client)
    orders    = get_open_orders(client)
    print("\n" + "=" * 58)
    print("  POST-LIQUIDATION VERIFICATION")
    print("=" * 58)
    print(f"  Open positions : {len(positions)}  {'OK' if not positions else '<-- expected 0'}")
    print(f"  Open orders    : {len(orders)}  {'OK' if not orders else '<-- ORPHANS, see below'}")
    for o in orders:
        print(f"     ORPHAN: {o.symbol} {str(o.side).split('.')[-1]} "
              f"{str(o.type).split('.')[-1]} qty={o.qty} id={o.id}")
    acct = get_account_summary(client)
    print(f"  Equity         : ${acct['equity']:,.2f}")
    print(f"  Cash           : ${acct['cash']:,.2f}")
    clean = not positions and not orders
    print("\n  RESULT: " + ("CLEAN — ready to rebuild" if clean
                            else "NOT CLEAN — investigate before rebuilding"))
    print("=" * 58)
    return clean


def main():
    ap = argparse.ArgumentParser(description="Full liquidation for a clean restart")
    ap.add_argument("--execute", action="store_true", help="actually sell (default: preview)")
    ap.add_argument("--one", action="store_true",
                    help="only the smallest position — prove the path once first")
    ap.add_argument("--ticker", help="liquidate one specific ticker")
    ap.add_argument("--verify", action="store_true", help="check end state and exit")
    ap.add_argument("--portfolio", default="pipeline", choices=["pipeline", "screener"])
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    client = get_client(args.portfolio)
    acct   = get_account_summary(client)

    # Paper-only lock
    base = getattr(config, "ALPACA_BASE_URL", "")
    if "paper" not in base:
        log.error("ABORT: ALPACA_BASE_URL is not a paper endpoint (%s)", base)
        sys.exit(1)

    if args.verify:
        sys.exit(0 if verify(client) else 1)

    positions = get_positions(client)
    if not positions:
        log.info("No open positions — nothing to liquidate.")
        verify(client)
        return

    resting = get_resting_stops(client)

    # Selection
    if args.ticker:
        tk = args.ticker.upper()
        if tk not in positions:
            log.error("%s is not held.", tk); sys.exit(1)
        targets = [tk]
    elif args.one:
        # Pick the smallest position that HAS a resting stop. The whole point
        # of the single-position run is to prove cancel-then-sell works; a
        # stub with no stop would exercise none of it and give false comfort.
        with_stop = {t: p for t, p in positions.items() if t in resting}
        pool = with_stop or positions
        targets = [min(pool, key=lambda t: pool[t]["market_value"])]
        log.info("Single-position mode: %s (smallest position WITH a resting stop — "
                 "this is what exercises the cancel-then-sell path)", targets[0])
    else:
        targets = sorted(positions)

    market = is_market_open(client)
    print("\n" + "=" * 78)
    print(f"  LIQUIDATION {'PREVIEW' if not args.execute else 'EXECUTION'} "
          f"[{args.portfolio}] · market {'OPEN' if market else 'CLOSED'}")
    print("=" * 78)
    print(f"  {'TKR':<6} {'qty':>10} {'entry':>9} {'last':>9} {'value':>11} "
          f"{'P&L':>9} {'stop?':>6}")
    print("  " + "-" * 74)
    total_val = total_pl = 0.0
    for t in targets:
        p = positions[t]
        total_val += p["market_value"]; total_pl += p["unrealized_pl"]
        print(f"  {t:<6} {p['qty']:>10.4f} {p['avg_entry_price']:>9.2f} "
              f"{p['current_price']:>9.2f} {p['market_value']:>11,.2f} "
              f"{p['unrealized_plpc']*100:>8.1f}% {'yes' if t in resting else 'NO':>6}")
    print("  " + "-" * 74)
    print(f"  {len(targets)} position(s) · value ${total_val:,.2f} · "
          f"realising P&L ${total_pl:+,.2f}")
    print(f"  Account equity ${acct['equity']:,.2f} · cash ${acct['cash']:,.2f}")

    if not args.execute:
        print("\n  PREVIEW ONLY — nothing sold.")
        print("  Prove the path on one position first:")
        print("      python scripts/liquidate_all.py --one --execute")
        print("  Then the remainder:")
        print("      python scripts/liquidate_all.py --execute")
        print("=" * 78)
        return

    if not market:
        log.error("ABORT: market is CLOSED. Market orders can't fill — re-run during "
                  "regular trading hours.")
        sys.exit(1)

    # Snapshot + tag BEFORE selling, so the record survives even if a sell fails
    _snapshot(positions, acct)
    _record_admin_exits(targets)

    print("\n  Selling via sell_with_cleanup (cancel resting stop -> confirm -> close)...")
    ok_count = 0
    for t in targets:
        r = sell_with_cleanup(client, t, dry_run=False)
        status = r.get("status", "?")
        good = status not in ("failed",)
        ok_count += good
        print(f"    {'OK ' if good else 'ERR'} {t:<6} cancelled={r.get('cancelled_orders', 0)} "
              f"status={status}"
              + (f" filled@${r['filled_avg_price']:.2f}" if r.get("filled_avg_price") else "")
              + (f" ERROR: {r.get('error')}" if r.get("error") else ""))

    print(f"\n  {ok_count}/{len(targets)} sold successfully.")
    verify(client)

    print("\n  NEXT:")
    if args.one:
        print("    Path proven. Liquidate the rest:  python scripts/liquidate_all.py --execute")
    else:
        print("    1. Set PAPER_TRADING_START_DATE in config.py to today")
        print("    2. Rebuild:  python main.py            (dry run — review the plan)")
        print("    3. Then:     python main.py --execute  (places orders)")
        print("    4. Confirm the invariant: stops == positions")


if __name__ == "__main__":
    main()
