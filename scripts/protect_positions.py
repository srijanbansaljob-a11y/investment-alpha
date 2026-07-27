"""
scripts/protect_positions.py — Attach protective stops to positions already held

WHY THIS EXISTS
---------------
Audited 2026-07-27: all 12 open pipeline positions had ZERO resting protective
orders at Alpaca. The pipeline's executor sent plain market orders (no bracket),
so nothing held an exit at the broker. The stop levels existed only inside our
own monitoring code — which does nothing if the monitor fails, if the workflow
breaks, or if the market gaps overnight.

WHY NOT JUST SELL AND REBUY WITH A BRACKET
-------------------------------------------
Because you don't have to, and it costs real money:
  - a standalone SELL STOP attaches to a position you already hold
  - no round trip => no crossing the spread twice on every name
  - no time out of the market between the sell and the rebuy
  - cost basis, holding period and open P&L are all preserved
  - the pipeline would not rebuy the same names anyway — it buys TODAY's
    top-N, which differs from what you currently hold

THE FRACTIONAL CONSTRAINT
-------------------------
Alpaca rejects stop orders on fractional quantities. Positions opened by the
old fractional sizing (e.g. HST 538.0941) therefore get a stop on the WHOLE
share portion only — 538 shares — leaving 0.0941 shares unprotected. That
remainder is ~0.02% of the position: immaterial. New positions are sized in
whole shares (see broker/executor.py calc_shares) so this does not recur.

USAGE
-----
    python scripts/protect_positions.py                 # PREVIEW only (default)
    python scripts/protect_positions.py --execute       # actually submit stops
    python scripts/protect_positions.py --portfolio screener
    python scripts/protect_positions.py --include-protected   # re-stop everything

Safe by default: does nothing without --execute.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from broker.alpaca_client import (
    get_client, get_positions, place_stop_order, is_market_open,
)
from broker.stop_loss import compute_stop_price, compute_take_profit

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("protect")


def _resting_stops(client) -> set:
    """Symbols that already have an open stop/limit order resting at Alpaca."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        return {o.symbol for o in orders}
    except Exception as exc:
        log.warning("Could not read open orders (%s) — assuming none rest", exc)
        return set()


def _regime() -> str:
    try:
        from pipeline import regime as regime_module
        return (regime_module.run().get("regime") or "bull").lower()
    except Exception as exc:
        log.warning("Regime lookup failed (%s) — defaulting to 'bull'", exc)
        return "bull"


def main():
    ap = argparse.ArgumentParser(description="Attach protective stops to held positions")
    ap.add_argument("--execute", action="store_true",
                    help="Actually submit the stop orders (default is preview only)")
    ap.add_argument("--portfolio", default="pipeline", choices=["pipeline", "screener"])
    ap.add_argument("--include-protected", action="store_true",
                    help="Also place stops on symbols that already have resting orders")
    ap.add_argument("--from-entry", action="store_true",
                    help="Anchor stops to entry price instead of trailing to the "
                         "current price (wider stops; gives back gains on winners)")
    args = ap.parse_args()

    client    = get_client(args.portfolio)
    positions = get_positions(client)
    if not positions:
        log.info("No open positions in %s — nothing to protect.", args.portfolio)
        return

    protected = _resting_stops(client)
    regime    = _regime()
    log.info("Portfolio: %s · regime: %s · market %s",
             args.portfolio, regime.upper(),
             "OPEN" if is_market_open(client) else "CLOSED")
    log.info("%d positions · %d already have resting orders\n", len(positions), len(protected))

    header = (f"{'TKR':<6}{'qty':>10}{'whole':>7}{'entry':>9}{'last':>9}"
              f"{'STOP':>9}{'risk%':>7}{'target':>9}  status")
    print(header)
    print("-" * len(header))

    plan, skipped = [], 0
    for ticker, p in sorted(positions.items()):
        qty, entry, last = p["qty"], p["avg_entry_price"], p["current_price"]
        whole = int(qty)

        if ticker in protected and not args.include_protected:
            print(f"{ticker:<6}{qty:>10.4f}{whole:>7}{entry:>9.2f}{last:>9.2f}"
                  f"{'—':>9}{'—':>7}{'—':>9}  already protected")
            skipped += 1
            continue
        if whole < 1:
            print(f"{ticker:<6}{qty:>10.4f}{whole:>7}{entry:>9.2f}{last:>9.2f}"
                  f"{'—':>9}{'—':>7}{'—':>9}  sub-1 share, cannot stop")
            skipped += 1
            continue

        # Anchor the stop to the HIGHER of entry and current price.
        #
        # An entry-anchored stop goes stale on a winner. Measured live on this
        # book: MRK entered at 113.80 and trades at 131.50, so entry - 2.5xATR
        # sits 19.8% below the current price — stopping out there hands back
        # the entire 15% gain and then some. Anchoring to the current price
        # instead caps the give-back at the same ATR distance used for a fresh
        # entry (~6%), and taking the max() means the stop can only ever ratchet
        # UP, never down. That is the standard trailing-stop discipline.
        stop_e, method, atr = compute_stop_price(ticker, entry, regime)
        stop_c, _mc, _ac    = compute_stop_price(ticker, last, regime)
        if not args.from_entry and stop_c > stop_e:
            stop, method = stop_c, method + " [trailed to current]"
            anchor = last
        else:
            stop, anchor = stop_e, entry
        target, _mon, _tm = compute_take_profit(ticker, anchor, regime, atr)

        # A stop must sit BELOW the current price or Alpaca rejects it (and it
        # would fire instantly). If the position is already under its computed
        # stop, that is a decision for you to make, not an order to auto-place.
        if stop >= last:
            print(f"{ticker:<6}{qty:>10.4f}{whole:>7}{entry:>9.2f}{last:>9.2f}"
                  f"{stop:>9.2f}{'—':>7}{'—':>9}  ⚠ ALREADY BELOW STOP — review manually")
            skipped += 1
            continue

        risk_pct = (last - stop) / last * 100
        plan.append((ticker, whole, stop))
        print(f"{ticker:<6}{qty:>10.4f}{whole:>7}{entry:>9.2f}{last:>9.2f}"
              f"{stop:>9.2f}{risk_pct:>6.1f}%{(target or 0):>9.2f}  {method}")

    print("-" * len(header))
    if not plan:
        log.info("Nothing to do (%d skipped).", skipped)
        return

    risk = sum(w * (positions[t]["current_price"] - s) for t, w, s in plan)
    log.info("%d stops to place · %d skipped", len(plan), skipped)
    log.info("Total capital at risk between here and the stops: $%s", f"{risk:,.2f}")

    if not args.execute:
        print("\nPREVIEW ONLY — nothing submitted.")
        print("Re-run with --execute to place these stop orders.")
        return

    print()
    log.info("Submitting %d stop orders (GTC)...", len(plan))
    ok = 0
    for ticker, whole, stop in plan:
        res = place_stop_order(client, ticker, whole, stop, dry_run=False)
        if res.get("status") not in ("failed", "skipped_sub_one_share"):
            ok += 1
    log.info("Done — %d/%d stops resting at Alpaca.", ok, len(plan))
    log.info("Verify with: /status in Discord, or the Alpaca orders page.")


if __name__ == "__main__":
    main()
