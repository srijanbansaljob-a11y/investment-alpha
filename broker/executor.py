"""
broker/executor.py — Translate Pipeline Signals → Alpaca Paper Orders

Logic:
  0. Alpaca-first reconciliation: compare live positions vs pipeline target
  1. Process EXIT signals first  → close those positions fully
  2. Process BUY signals         → delta-aware: buy only the gap vs current holding
  3. Skip HOLD signals           → positions stay untouched
  4. Log every action + confirm order IDs

Safety rules:
  - Never over-buys: BUY orders subtract shares already held (fractional fills,
    manual positions, or prior partial orders handled automatically)
  - Sells always before buys (ensures cash is available)
  - dry_run=True logs everything but places nothing
  - latest_portfolio.json is entry-price/date tracking ONLY — not truth of what's held
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

import config
from broker import alpaca_client as alpaca

log = logging.getLogger(__name__)


# ── Position Sizing ────────────────────────────────────────────────────────

def calc_shares(target_value: float, current_price: float) -> float:
    """Round fractional share quantity to 4 decimal places."""
    if current_price <= 0:
        return 0.0
    return round(target_value / current_price, 4)


# ── Alpaca-first Reconciliation ────────────────────────────────────────────

def _reconcile_signals(
    signals: list,
    live_positions: dict,
    equity: float,
) -> list:
    """
    Override pipeline signals based on what Alpaca actually holds.

    Alpaca is the source of truth. The pipeline state file diverges when:
      - A prior order was skipped (insufficient cash, partial fill)
      - The user manually bought or sold shares in Alpaca
      - latest_portfolio.json is stale after a crash or OneDrive sync

    Corrections applied:
      - HOLD signal + position absent from Alpaca    → force BUY
      - Position exists but weight drifted > threshold → queue delta rebalance (BUY)
      - Ticker in Alpaca but NOT in target portfolio  → EXIT or KEEP per config flag
    """
    if not getattr(config, "ALPACA_RECONCILE_ON_EXECUTE", True):
        return signals

    drift_threshold = getattr(config, "ALPACA_WEIGHT_DRIFT_THRESHOLD", 0.03)
    manual_action   = getattr(config, "MANUAL_POSITION_ACTION", "keep").lower()

    target_active  = {s["ticker"] for s in signals if s["action"] in ("BUY", "HOLD")}
    explicit_exits = {s["ticker"] for s in signals if s["action"] == "EXIT"}
    live_tickers   = set(live_positions.keys())

    corrected        = []
    corrections_made = 0

    for sig in signals:
        ticker = sig["ticker"]
        action = sig["action"]

        if action == "EXIT":
            corrected.append(sig)
            continue

        pos_exists    = ticker in live_positions
        target_weight = sig.get("weight", 0)

        if action == "HOLD" and not pos_exists:
            log.warning(
                "  RECONCILE %-6s: HOLD but not in Alpaca "
                "-> upgrading to BUY (missed entry or manual close)",
                ticker,
            )
            corrected.append(dict(sig, action="BUY", reconcile_reason="missing_from_alpaca"))
            corrections_made += 1
            continue

        if pos_exists and target_weight > 0 and equity > 0:
            current_weight = live_positions[ticker]["market_value"] / equity
            drift = current_weight - target_weight
            if abs(drift) > drift_threshold:
                direction = "over" if drift > 0 else "under"
                log.warning(
                    "  RECONCILE %-6s: %sweight by %+.1f pct "
                    "(current=%.1f pct, target=%.1f pct) -> queuing delta rebalance",
                    ticker, direction,
                    drift * 100, current_weight * 100, target_weight * 100,
                )
                corrected.append(
                    dict(sig, action="BUY",
                         reconcile_reason="weight_drift_" + "{:.3f}".format(drift))
                )
                corrections_made += 1
                continue

        corrected.append(sig)

    # Tickers in Alpaca but not targeted by model and not explicitly exited
    manual = live_tickers - target_active - explicit_exits
    for ticker in sorted(manual):
        pos = live_positions[ticker]
        if manual_action == "exit":
            log.warning(
                "  RECONCILE %-6s: manual position ($%.0f) -> adding EXIT "
                "(MANUAL_POSITION_ACTION=exit)",
                ticker, pos["market_value"],
            )
            corrected.append({
                "ticker":           ticker,
                "action":           "EXIT",
                "weight":           0,
                "current_price":    pos.get("current_price", 0),
                "reconcile_reason": "manual_position_exit",
            })
            corrections_made += 1
        else:
            log.warning(
                "  RECONCILE %-6s: manual position ($%.0f) retained "
                "(MANUAL_POSITION_ACTION=keep) -- not managed by model",
                ticker, pos["market_value"],
            )

    if corrections_made:
        log.info("  RECONCILE: %d correction(s) applied to signals", corrections_made)
    else:
        log.info("  RECONCILE: Alpaca positions match pipeline -- no corrections needed")

    return corrected


# ── Main Executor ──────────────────────────────────────────────────────────

def execute_signals(
    signals: list,
    dry_run: bool = True,
) -> dict:
    """
    Execute trade signals against the Alpaca paper trading account.

    When ALPACA_RECONCILE_ON_EXECUTE=True (default), live Alpaca positions are
    compared against the target portfolio before any orders are placed.
    Alpaca is the source of truth — not latest_portfolio.json.

    BUY signals are delta-aware: only the gap between current holdings and the
    target quantity is traded, preventing double-buys on partial fills.

    Args:
        signals:  List from pipeline/signals.py — each has ticker, action, weight,
                  current_price
        dry_run:  If True (default), log all actions but place no real orders.

    Returns:
        {"status", "dry_run", "executed_at", "account_before", "account_after",
         "open_positions", "orders", "summary"}
    """
    log.info("\n" + "=" * 50)
    log.info("BROKER: Executing signals  [dry_run=%s]", dry_run)
    log.info("=" * 50)

    if dry_run:
        log.info("  WARNING: DRY-RUN MODE -- no real orders will be placed")

    # ── Connect + preflight ───────────────────────────────────────────────
    try:
        client = alpaca.get_client()
    except ValueError as e:
        log.error("  Cannot connect to Alpaca: %s", e)
        return {"status": "failed", "error": str(e)}

    # -- Kill switch --
    if not getattr(config, "EXECUTION_ENABLED", True) and not dry_run:
        log.warning("  EXECUTION_ENABLED=False -- forcing dry-run; NO orders will be placed")
        dry_run = True

    # -- Execution lock (prevents simultaneous local + cloud runs) --
    lock_acquired = True
    if not dry_run:
        try:
            from broker.kv_lock import acquire_lock
            lock_acquired = acquire_lock(owner="executor")
        except Exception as _le:
            log.warning("  Lock check skipped (%s) — proceeding", _le)
        if not lock_acquired:
            return {
                "status":      "skipped_lock_held",
                "dry_run":     dry_run,
                "executed_at": datetime.now().isoformat(),
                "orders":      [],
                "summary":     {"orders_placed": 0, "reason": "execution_lock_held"},
            }

    # -- Market-closed guard --
    # Fractional-share orders are REJECTED outside regular trading hours and
    # cannot be queued. Rather than submit orders that silently fail (and get
    # counted as "placed"), skip execution entirely when the market is closed.
    market_open = alpaca.is_market_open(client)
    if not market_open and not dry_run and getattr(config, "EXECUTION_REQUIRE_MARKET_OPEN", True):
        log.warning("  Market is CLOSED -- skipping execution (fractional orders can't be "
                    "queued). Re-run during regular trading hours.")
        return {
            "status":      "skipped_market_closed",
            "dry_run":     dry_run,
            "market_open": market_open,
            "executed_at": datetime.now().isoformat(),
            "orders":      [],
            "summary":     {"orders_placed": 0, "orders_failed": 0,
                            "reason": "market_closed"},
        }

    account_before    = alpaca.get_account_summary(client)
    current_positions = alpaca.get_positions(client)
    equity            = account_before["equity"]

    log.info("  Account equity   : $%s", "{:,.2f}".format(equity))
    log.info("  Cash available   : $%s", "{:,.2f}".format(account_before["cash"]))
    log.info("  Open positions   : %d", len(current_positions))
    log.info("  Signals received : %d", len(signals))

    # ── Alpaca-first reconciliation ───────────────────────────────────────
    if getattr(config, "ALPACA_RECONCILE_ON_EXECUTE", True):
        log.info("\n  [RECONCILE] Checking live Alpaca positions vs pipeline signals...")
        signals = _reconcile_signals(signals, current_positions, equity)

    orders       = []
    exit_signals = [s for s in signals if s["action"] == "EXIT"]
    buy_signals  = [s for s in signals if s["action"] == "BUY"]
    hold_signals = [s for s in signals if s["action"] == "HOLD"]

    # ── Step 1: Cancel stale open orders ─────────────────────────────────
    if not dry_run:
        alpaca.cancel_open_orders(client)

    # ── Step 2: EXIT first (free cash before buying) ──────────────────────
    log.info("\n  [EXIT] Processing %d exits...", len(exit_signals))
    for sig in exit_signals:
        ticker = sig["ticker"]
        if ticker in current_positions:
            pos = current_positions[ticker]
            reason = sig.get("reconcile_reason", "pipeline_signal")
            log.info(
                "    EXIT %-6s  qty=%.4f  value=$%.2f  reason=%s",
                ticker, pos["qty"], pos["market_value"], reason,
            )
            order = alpaca.close_position(client, ticker, dry_run=dry_run)
            order["action"]    = "EXIT"
            order["rationale"] = sig.get("entry_rationale", reason)
            orders.append(order)
        else:
            log.info("    EXIT %-6s  (no open position -- nothing to close)", ticker)
            orders.append({
                "ticker": ticker, "action": "EXIT",
                "status": "no_position", "qty": 0,
            })

    # ── Step 3: HOLD — log only, no action ───────────────────────────────
    log.info("\n  [HOLD] %d positions held (no action)", len(hold_signals))
    for sig in hold_signals:
        ticker = sig["ticker"]
        pos    = current_positions.get(ticker, {})
        log.info("    HOLD %-6s  current_value=$%.2f",
                 ticker, pos.get("market_value", 0))
        orders.append({
            "ticker": ticker, "action": "HOLD",
            "status": "held", "qty": pos.get("qty", 0),
        })

    # ── Step 4: BUY — delta-aware (only buy/sell the gap) ─────────────────
    log.info("\n  [BUY] Processing %d buys...", len(buy_signals))

    if not dry_run:
        refreshed      = alpaca.get_account_summary(client)
        available_cash = float(refreshed["cash"])
    else:
        freed_cash     = sum(
            current_positions.get(s["ticker"], {}).get("market_value", 0)
            for s in exit_signals
        )
        available_cash = account_before["cash"] + freed_cash

    log.info("  Estimated cash for buys: $%s", "{:,.2f}".format(available_cash))

    for sig in buy_signals:
        ticker           = sig["ticker"]
        weight           = sig.get("weight", config.EQUAL_WEIGHT)
        target_value     = equity * weight
        reconcile_reason = sig.get("reconcile_reason", "")
        reason_note      = "  [" + reconcile_reason + "]" if reconcile_reason else ""

        # Re-entry cooldown: signals.py flags names stopped out within N days.
        if sig.get("cooldown_blocked"):
            log.info("    BUY %-6s: re-entry cooldown active -- skipping%s", ticker, reason_note)
            orders.append({"ticker": ticker, "action": "BUY",
                           "status": "skipped_cooldown"})
            continue

        price = sig.get("current_price")
        if not price:
            log.warning("    BUY %-6s: no price -- skipping%s", ticker, reason_note)
            orders.append({"ticker": ticker, "action": "BUY",
                           "status": "skipped_no_price"})
            continue

        target_qty   = calc_shares(target_value, price)
        existing_qty = current_positions.get(ticker, {}).get("qty", 0)
        delta_qty    = round(target_qty - existing_qty, 4)

        if abs(delta_qty) < 0.0001:
            log.info(
                "    BUY %-6s: already at target (%.4f shares), no action%s",
                ticker, existing_qty, reason_note,
            )
            orders.append({"ticker": ticker, "action": "BUY",
                           "status": "at_target", "qty": existing_qty})
            continue

        if delta_qty > 0:
            # Need to buy more shares
            cost = delta_qty * price
            if cost > available_cash * getattr(config, "CASH_BUFFER_MULTIPLIER", 1.0):
                log.warning(
                    "    BUY %-6s: insufficient cash (need $%.2f, have $%.2f)%s",
                    ticker, cost, available_cash, reason_note,
                )
                orders.append({"ticker": ticker, "action": "BUY",
                               "status": "skipped_insufficient_cash"})
                continue

            log.info(
                "    BUY %-6s  delta=+%.4f  price=$%.2f  cost=$%.2f"
                "  weight=%.0f pct%s",
                ticker, delta_qty, price, cost, weight * 100, reason_note,
            )
            order = alpaca.place_market_order(client, ticker, delta_qty, "buy",
                                              dry_run=dry_run)
            order["action"]       = "BUY"
            order["target_value"] = round(cost, 2)
            order["weight"]       = weight
            order["rationale"]    = sig.get("entry_rationale", reconcile_reason)
            orders.append(order)
            available_cash -= cost

        else:
            # delta_qty < 0: trim overweight position
            trim_qty = abs(delta_qty)
            log.info(
                "    TRIM %-6s  delta=%.4f  price=$%.2f  (overweight trim)%s",
                ticker, -delta_qty, price, reason_note,
            )
            order = alpaca.place_market_order(client, ticker, trim_qty, "sell",
                                              dry_run=dry_run)
            order["action"]    = "TRIM"
            order["rationale"] = reconcile_reason or "weight_drift_trim"
            orders.append(order)
            available_cash += trim_qty * price

    # ── Account state AFTER ────────────────────────────────────────────────
    if not dry_run:
        account_after   = alpaca.get_account_summary(client)
        final_positions = alpaca.get_positions(client)
    else:
        account_after          = account_before.copy()
        account_after["_note"] = "dry_run -- values unchanged"
        final_positions        = current_positions

    # ── Summary ────────────────────────────────────────────────────────────
    terminal_statuses = {
        "dry_run", "held", "no_position", "at_target",
        "skipped_no_price", "skipped_insufficient_cash", "skipped_cooldown",
    }
    summary = {
        "signals_processed": len(signals),
        "exits":             len(exit_signals),
        "holds":             len(hold_signals),
        "buys":              len(buy_signals),
        "orders_placed":     sum(1 for o in orders
                                 if o.get("status") not in terminal_statuses),
        "orders_failed":     sum(1 for o in orders if o.get("status") == "failed"),
        "dry_run":           dry_run,
        "market_open":       market_open,
        "executed_at":       datetime.now().isoformat(),
    }

    log.info("\n  Execution summary:")
    log.info("    Exits         : %d", summary["exits"])
    log.info("    Holds         : %d", summary["holds"])
    log.info("    Buys          : %d", summary["buys"])
    log.info("    Orders placed : %d", summary["orders_placed"])
    log.info("    Orders failed : %d", summary["orders_failed"])

    # Release execution lock now that orders are submitted
    if lock_acquired and not dry_run:
        try:
            from broker.kv_lock import release_lock
            release_lock()
        except Exception as _le:
            log.warning("  Lock release skipped (%s)", _le)

    return {
        "status":         "success" if summary["orders_failed"] == 0 else "partial",
        "dry_run":        dry_run,
        "executed_at":    summary["executed_at"],
        "market_open":    market_open,
        "account_before": account_before,
        "account_after":  account_after,
        "open_positions": final_positions,
        "orders":         orders,
        "summary":        summary,
    }


# ── Quick Test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s  %(message)s")

    print("\n=== Executor Test -- DRY RUN ===")

    mock_signals = [
        {
            "ticker": "GOOGL", "action": "BUY", "weight": 0.10,
            "current_price": 350.34,
            "entry_rationale": "Strong momentum, above 200MA",
        },
        {
            "ticker": "AMZN", "action": "BUY", "weight": 0.10,
            "current_price": 261.12,
            "entry_rationale": "Strong 6M momentum",
        },
        {
            "ticker": "AAPL", "action": "BUY", "weight": 0.10,
            "current_price": 267.61,
            "entry_rationale": "Above 200MA, quality score",
        },
    ]

    result = execute_signals(mock_signals, dry_run=True)

    print("\nStatus    : " + result["status"])
    print("Dry run   : " + str(result["dry_run"]))
    print("Market    : " + ("OPEN" if result["market_open"] else "CLOSED"))
    print("\nAccount before:")
    print("  Equity : $" + "{:,.2f}".format(result["account_before"]["equity"]))
    print("  Cash   : $" + "{:,.2f}".format(result["account_before"]["cash"]))
    print("\nOrders (dry run):")
    for o in result["orders"]:
        print("  [" + o["action"] + "] " + o["ticker"].ljust(6) + "  status=" + str(o["status"]))
    print("\nSummary: " + str(result["summary"]))
    print("\nExecutor dry-run test complete")
