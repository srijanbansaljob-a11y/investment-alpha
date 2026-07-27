"""
scripts/force_stop_test.py — Deliberately trigger a stop to prove the chain works

WHAT THIS IS FOR
----------------
Task #29 has sat open for weeks: `/stoploss mode:execute` has never been
exercised end-to-end. Waiting for a real 6-8% drawdown to test it is not a
plan. This moves ONE stop up to just below the current price so it fires
within minutes, letting you watch the whole chain on paper money:

    stop fires -> position closes at Alpaca -> snapshot picks up the exit
    -> trade_outcome_logger records it -> factor analysis sees a closed trade

PAPER MONEY ONLY. This script refuses to run against a live account.

WHAT IT DOES
------------
  1. Finds the target position (default: worst unrealised performer)
  2. Cancels its existing protective stop
  3. Places a new stop `--offset` dollars below the current price
  4. Prints what to watch for

The position WILL be sold. That is the entire point — you are buying a real
end-to-end test with a small, deliberate, paper-money loss.

USAGE
-----
    python scripts/force_stop_test.py                    # PREVIEW (default)
    python scripts/force_stop_test.py --execute
    python scripts/force_stop_test.py --ticker NUE --offset 0.50 --execute

AFTERWARDS
----------
    /stoploss mode:check pipeline   -> should show 11 positions
    /status pipeline                -> NUE gone, cash up
    python scripts/protect_positions.py   -> re-stop anything left unprotected
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from broker.alpaca_client import (
    get_client, get_positions, place_stop_order, is_market_open, PAPER_BASE_URL,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("stoptest")


def _resting_stops(client) -> dict:
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
    return {o.symbol: o for o in orders
            if str(o.order_type).lower().endswith("stop") and getattr(o, "stop_price", None)}


def main():
    ap = argparse.ArgumentParser(description="Force a stop-loss to trigger (paper only)")
    ap.add_argument("--execute", action="store_true", help="Actually do it (default: preview)")
    ap.add_argument("--ticker", default=None,
                    help="Which position to use (default: worst unrealised performer)")
    ap.add_argument("--offset", type=float, default=0.50,
                    help="Place the stop this many dollars BELOW the current price (default 0.50)")
    ap.add_argument("--portfolio", default="pipeline", choices=["pipeline", "screener"])
    args = ap.parse_args()

    client = get_client(args.portfolio)

    # Hard safety rail: paper endpoints only.
    base = getattr(client, "_base_url", "") or PAPER_BASE_URL
    if "paper" not in str(base).lower():
        log.error("REFUSING TO RUN — this is not a paper account (%s). "
                  "This script deliberately loses money.", base)
        sys.exit(1)

    positions = get_positions(client)
    if not positions:
        log.info("No open positions — nothing to test with.")
        return

    if args.ticker:
        ticker = args.ticker.upper()
        if ticker not in positions:
            log.error("%s is not an open position. Held: %s", ticker, ", ".join(sorted(positions)))
            sys.exit(1)
    else:
        ticker = min(positions, key=lambda t: positions[t]["unrealized_plpc"])
        log.info("No --ticker given; using worst performer.")

    p        = positions[ticker]
    last     = p["current_price"]
    qty      = int(p["qty"])
    new_stop = round(last - args.offset, 2)

    resting = _resting_stops(client)
    existing = resting.get(ticker)

    print()
    print(f"  Target        : {ticker}  ({p['unrealized_plpc']*100:+.2f}% unrealised)")
    print(f"  Position      : {p['qty']:.4f} shares  (stop covers {qty} whole)")
    print(f"  Current price : ${last:,.2f}")
    print(f"  Existing stop : " + (f"${float(existing.stop_price):,.2f}  (id {existing.id})"
                                   if existing else "none resting"))
    print(f"  NEW test stop : ${new_stop:,.2f}   <-- ${args.offset:.2f} below current")
    print(f"  Market open   : {is_market_open(client)}")
    print()
    est_loss = (p["avg_entry_price"] - new_stop) * qty
    print(f"  This SELLS the position when price ticks to ${new_stop:,.2f}.")
    print(f"  Approx realised P&L vs entry: ${-est_loss:,.2f}  (paper money)")
    print()

    if not args.execute:
        print("  PREVIEW ONLY — nothing changed.")
        print("  Re-run with --execute to arm the test.")
        return

    if not is_market_open(client):
        log.warning("Market is CLOSED — the stop will rest until the next session, "
                    "then likely fire at the open. Continuing anyway.")

    if existing:
        try:
            client.cancel_order_by_id(existing.id)
            log.info("Cancelled existing stop for %s (id %s)", ticker, existing.id)
        except Exception as exc:
            log.error("Could not cancel existing stop (%s) — aborting so we don't "
                      "end up with two stops on one position.", exc)
            sys.exit(1)

    res = place_stop_order(client, ticker, qty, new_stop, dry_run=False)
    if res.get("status") == "failed":
        log.error("Failed to place test stop: %s", res.get("error"))
        sys.exit(1)

    print()
    log.info("ARMED — %s stop now at $%.2f (order %s)", ticker, new_stop, res.get("order_id"))
    print()
    print("  Watch for, in order:")
    print("    1. Alpaca fills the stop (usually within minutes while open)")
    print("    2. /status pipeline      -> position gone, cash increased")
    print("    3. /stoploss mode:check  -> one fewer position")
    print("    4. After the close: scripts/snapshot_positions.py + trade_outcome_logger.py")
    print("       record the exit -> shows up in the weekly factor analysis")
    print()
    print("  To ABORT before it fires:")
    print(f"    cancel order {res.get('order_id')} in Alpaca, then re-run")
    print("    python scripts/protect_positions.py --execute")


if __name__ == "__main__":
    main()
