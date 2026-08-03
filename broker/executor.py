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
    """
    Whole-share quantity for a target dollar value.

    CHANGED 2026-07-27: previously returned a fractional quantity rounded to 4
    decimals. Alpaca REJECTS bracket orders (and standalone stop orders) on
    fractional quantities, which meant every position the pipeline opened was
    structurally incapable of carrying a broker-side stop. Verified live: all
    12 open pipeline positions were fractional and had zero resting protective
    orders.

    Flooring to whole shares costs at most one share of precision per position
    (well under 1% of a typical position) and buys the ability to attach a real
    stop at the broker. That trade is worth taking.
    """
    if current_price <= 0:
        return 0.0
    return float(int(target_value / current_price))


# ── Alpaca-first Reconciliation ────────────────────────────────────────────

def _check_drawdown_pause(equity: float) -> tuple[bool, str]:
    """
    Portfolio-level circuit breaker: stop opening NEW positions once equity is
    DRAWDOWN_PAUSE_PCT below its high-water mark; resume when it recovers to
    within DRAWDOWN_RESUME_PCT. Exits and protective stops are unaffected —
    this only stops you buying into a decline.

    OWNERSHIP (2026-07-31): this gate previously lived ONLY inside
    strategies/mean_reversion.py::_check_cash_management. When the sleeve was
    paused (MR_ENABLED=False) the drawdown pause silently stopped being
    enforced anywhere, even though config still declared it. Buy-gating belongs
    to the executor, so it lives here now — one owner, applied to the strategy
    that actually trades.

    State: data/portfolio_peak.json (git-tracked, so the high-water mark
    survives stateless cloud runs).

    Returns (ok_to_buy, reason).
    """
    if not getattr(config, "CASH_MGMT_ENABLED", True) or equity <= 0:
        return True, ""

    import json as _json
    pause_pct  = getattr(config, "DRAWDOWN_PAUSE_PCT", 0.08)
    resume_pct = getattr(config, "DRAWDOWN_RESUME_PCT", 0.05)
    path = Path(getattr(config, "DATA_DIR", "data")) / "portfolio_peak.json"

    data = {}
    try:
        if path.exists():
            raw = path.read_bytes().rstrip(b"\x00")
            data = _json.loads(raw) if raw else {}
    except Exception as exc:
        log.warning("  Drawdown: could not read peak file (%s) — treating today as peak", exc)

    peak    = float(data.get("peak_equity") or equity)
    paused  = bool(data.get("paused", False))
    if equity > peak:
        peak = equity
    drawdown = (peak - equity) / peak if peak > 0 else 0.0

    if paused and drawdown < resume_pct:
        paused = False
        log.info("  Drawdown recovered to %.1f%% — new buys re-enabled.", drawdown * 100)
    elif not paused and drawdown >= pause_pct:
        paused = True
        log.warning("  DRAWDOWN PAUSE TRIGGERED: equity $%s is %.1f%% below the $%s peak.",
                    "{:,.0f}".format(equity), drawdown * 100, "{:,.0f}".format(peak))

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps({
            "peak_equity": round(peak, 2),
            "paused":      paused,
            "drawdown_pct": round(drawdown * 100, 2),
            "updated":     datetime.now().isoformat(),
        }, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("  Drawdown: could not persist peak state (%s)", exc)

    if paused:
        return False, ("drawdown pause active — equity {:.1f}% below its ${:,.0f} peak; "
                       "new buys blocked until it recovers above -{:.0f}%"
                       .format(drawdown * 100, peak, resume_pct * 100))
    return True, ""


def _blocked_from_buying(sig: dict, cooldown_tickers: set) -> str | None:
    """
    Reasons a reconcile HOLD→BUY upgrade must NOT happen (audit P0-4).

    Before this guard, the reconciler silently defeated two safety rules:
      - earnings blackout: signals.py downgrades a pre-earnings BUY to HOLD;
        the reconciler saw "HOLD but not held" and upgraded it right back.
      - re-entry cooldown: a stopped-out name still in the top-N arrives as
        HOLD (so signals.py never set cooldown_blocked), its position is gone
        from Alpaca, and the reconciler re-bought it instantly.

    Returns a reason string if blocked, None if the upgrade may proceed.
    """
    if sig.get("earnings_blocked"):
        return "earnings_blackout"
    if sig.get("cooldown_blocked"):
        return "reentry_cooldown"
    if sig["ticker"] in cooldown_tickers:
        return "reentry_cooldown_broker"
    return None


def _cooldown_tickers(client=None) -> set:
    """
    Union of names stopped out within REENTRY_COOLDOWN_DAYS from BOTH sources:
      - stop_loss_log.json (local checker scans)
      - Alpaca filled SELL STOP orders (broker-side GTC stops fill without
        ever touching the local log — the gap that let a stopped-out name
        be re-bought on the very next run)
    """
    days = int(getattr(config, "REENTRY_COOLDOWN_DAYS", 0) or 0)
    if days <= 0:
        return set()
    out = set()
    try:
        from pipeline.signals import _recent_stop_exits
        out |= set(_recent_stop_exits(days).keys())
    except Exception as exc:
        log.warning("  Cooldown: could not read stop log (%s)", exc)
    try:
        if client is not None:
            out |= set(alpaca.get_recent_stop_fills(client, days=days).keys())
    except Exception as exc:
        log.warning("  Cooldown: could not read broker stop fills (%s)", exc)
    return out


def _reconcile_signals(
    signals: list,
    live_positions: dict,
    equity: float,
    cooldown_tickers: set | None = None,
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

    cooldown_tickers = cooldown_tickers or set()
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
            block = _blocked_from_buying(sig, cooldown_tickers)
            if block:
                log.info(
                    "  RECONCILE %-6s: HOLD not in Alpaca, but upgrade BLOCKED (%s) "
                    "-- staying flat this run", ticker, block,
                )
                corrected.append(dict(sig, reconcile_reason="upgrade_blocked_" + block))
                continue
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
    regime: str = "bull",
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

    # -- Kill switch 1: local config flag --
    if not getattr(config, "EXECUTION_ENABLED", True) and not dry_run:
        log.warning("  EXECUTION_ENABLED=False -- forcing dry-run; NO orders will be placed")
        dry_run = True

    # -- Kill switch 2: /pausetrading (Cloudflare KV) --
    # Until 2026-08-01 nothing in this path read the flag, so the documented
    # kill switch did not stop the pipeline. It does now.
    if not dry_run:
        try:
            from broker.kv_lock import is_trading_paused
            paused = is_trading_paused()
        except Exception as exc:
            log.warning("  Could not import pause check (%s)", exc)
            paused = None
        if paused is True:
            log.warning("  🔴 TRADING PAUSED via /pausetrading -- forcing dry-run; "
                        "NO orders will be placed. Use /resumetrading to re-enable.")
            dry_run = True
        elif paused is None:
            log.warning("  ⚠️  Could not verify the /pausetrading kill switch "
                        "(Cloudflare KV unreachable or CF_* credentials not set). "
                        "Proceeding — but the remote kill switch is NOT protecting "
                        "this run. Set EXECUTION_ENABLED=False in config.py to stop "
                        "execution locally.")

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

    # ── Cooldown set (stop log + broker-side stop fills, audit P0-4) ──────
    cooldown_tickers = _cooldown_tickers(client)
    if cooldown_tickers:
        log.info("  Re-entry cooldown active for: %s", sorted(cooldown_tickers))

    # ── Alpaca-first reconciliation ───────────────────────────────────────
    if getattr(config, "ALPACA_RECONCILE_ON_EXECUTE", True):
        log.info("\n  [RECONCILE] Checking live Alpaca positions vs pipeline signals...")
        signals = _reconcile_signals(signals, current_positions, equity,
                                     cooldown_tickers=cooldown_tickers)

    # Broker-side stop fills also block plain BUY signals (signals.py can
    # only see the local stop log; the broker's GTC fills happen without it).
    for s in signals:
        if s["action"] == "BUY" and s["ticker"] in cooldown_tickers:
            s["cooldown_blocked"] = True

    orders       = []
    exit_signals = [s for s in signals if s["action"] == "EXIT"]
    buy_signals  = [s for s in signals if s["action"] == "BUY"]
    hold_signals = [s for s in signals if s["action"] == "HOLD"]

    # ── Step 1: (REMOVED — audit P0-1) ────────────────────────────────────
    # This used to be a blanket alpaca.cancel_open_orders(client), which
    # cancelled EVERY resting protective stop on the account and never put
    # them back. Cancellation is now per-ticker, inside sell_with_cleanup,
    # and protection is restored for all positions by the stop-reconcile
    # pass at the end of this function.

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
            order = alpaca.sell_with_cleanup(client, ticker, dry_run=dry_run)
            order["action"]    = "EXIT"
            order["rationale"] = sig.get("entry_rationale", reason)
            if not dry_run:
                if order.get("filled_avg_price"):
                    log.info("      -> filled @ $%.2f", order["filled_avg_price"])
                else:
                    log.warning("      -> NOT CONFIRMED FILLED (status=%s) — verify in Alpaca", order.get("status"))
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

    # ── Exposure cap ──────────────────────────────────────────────────────
    # Until now the pipeline bought until CASH ran out, with no ceiling on how
    # much of the account could be at risk. Live check on 2026-07-27 found the
    # account at 98.3% invested with $1,902 cash — no dry powder, no brake if
    # the model is wrong. The regime caps existed in the mean-reversion sleeve
    # and in the health checker's WARNING, but nothing enforced them here.
    exposure_caps = getattr(config, "PIPELINE_MAX_INVESTED_PCT", None) or {
        "bull": 0.60, "neutral": 0.40, "bear": 0.20,
    }
    regime_key = (regime or "bull").lower()
    if regime_key not in exposure_caps:
        regime_key = "bull"
    max_invested = exposure_caps.get(regime_key, 0.70)
    invested_value = max(0.0, equity - float(account_before["cash"]))
    exposure_budget = max(0.0, (equity * max_invested) - invested_value)

    log.info("  Exposure cap     : %.0f%% (%s regime) — invested $%s of $%s equity (%.1f%%)",
             max_invested * 100, regime_key.upper(),
             "{:,.0f}".format(invested_value), "{:,.0f}".format(equity),
             (invested_value / equity * 100) if equity else 0)

    # ── Drawdown circuit breaker (independent of the exposure cap) ────────
    # The cap asks "how much may I hold?"; this asks "should I be adding at
    # all right now?". Both must pass before any BUY.
    dd_ok, dd_reason = _check_drawdown_pause(equity)
    if not dd_ok:
        log.warning("  🛑 NO BUYS THIS RUN — %s", dd_reason)
        exposure_budget = 0.0

    if exposure_budget <= 0:
        log.warning("  Already at or above the %.0f%% exposure cap — NO new buys "
                    "will be placed until positions close.", max_invested * 100)
    else:
        log.info("  Room for new buys: $%s (cap) vs $%s (cash) — lower of the two applies",
                 "{:,.0f}".format(exposure_budget), "{:,.0f}".format(available_cash))

    # ── Re-check the kill switch immediately before the first order ──────
    # Cloudflare KV is eventually consistent: a /pausetrading write takes up
    # to 60 seconds to become visible everywhere. Measured 2026-08-02 — the
    # pause was invisible for ~3 minutes to a GitHub Actions runner reading
    # from a different edge location. A full pipeline run takes several
    # minutes, so a pause pressed at run start would otherwise be honoured
    # only on the NEXT run, after this one had already traded. Re-reading here
    # closes most of that window at the cost of one HTTP call.
    if buy_signals and not dry_run:
        try:
            from broker.kv_lock import is_trading_paused
            if is_trading_paused() is True:
                log.warning("  🔴 TRADING PAUSED (detected on re-check before the "
                            "first order) — abandoning %d buy(s). No orders placed.",
                            len(buy_signals))
                for s in buy_signals:
                    orders.append({"ticker": s["ticker"], "action": "BUY",
                                   "status": "skipped_trading_paused"})
                buy_signals = []
        except Exception as exc:
            log.warning("  Pause re-check failed (%s) — proceeding", exc)

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

        if target_qty == 0 and existing_qty == 0:
            # Price too high for this weight's dollar target — surface it
            # instead of silently logging "at_target" (audit P1-3).
            log.warning("    BUY %-6s: target $%.0f < 1 share @ $%.2f -- "
                        "skipped (too expensive for weight)%s",
                        ticker, target_value, price, reason_note)
            orders.append({"ticker": ticker, "action": "BUY",
                           "status": "skipped_too_expensive"})
            continue

        if abs(delta_qty) < 0.0001:
            log.info(
                "    BUY %-6s: already at target (%.4f shares), no action%s",
                ticker, existing_qty, reason_note,
            )
            orders.append({"ticker": ticker, "action": "BUY",
                           "status": "at_target", "qty": existing_qty})
            continue

        if delta_qty > 0:
            # Whole shares only (audit P0-5): a fractional top-up can't carry
            # a broker-side stop, and all protection now comes from the
            # stop-reconcile pass which places whole-share stops.
            delta_qty = float(int(delta_qty))
            if delta_qty < 1:
                log.info("    BUY %-6s: delta rounds below 1 share -- no action%s",
                         ticker, reason_note)
                orders.append({"ticker": ticker, "action": "BUY",
                               "status": "at_target", "qty": existing_qty})
                continue
            cost = delta_qty * price
            if cost > available_cash * getattr(config, "CASH_BUFFER_MULTIPLIER", 1.0):
                log.warning(
                    "    BUY %-6s: insufficient cash (need $%.2f, have $%.2f)%s",
                    ticker, cost, available_cash, reason_note,
                )
                orders.append({"ticker": ticker, "action": "BUY",
                               "status": "skipped_insufficient_cash"})
                continue

            # Exposure cap — the second, independent brake. Cash alone is not a
            # risk limit: an account can be 100% invested and still have cash.
            if cost > exposure_budget:
                if not dd_ok:
                    log.info("    BUY %-6s: skipped — %s%s", ticker, dd_reason, reason_note)
                    orders.append({"ticker": ticker, "action": "BUY",
                                   "status": "skipped_drawdown_pause"})
                    continue
                log.warning(
                    "    BUY %-6s: would breach the %.0f%% exposure cap "
                    "(need $%.2f, %.0f%% budget remaining $%.2f)%s",
                    ticker, max_invested * 100, cost, max_invested * 100,
                    exposure_budget, reason_note,
                )
                orders.append({"ticker": ticker, "action": "BUY",
                               "status": "skipped_exposure_cap"})
                continue

            # Plain market buy (audit P0-5/P1-1): brackets are gone. A bracket
            # only protected new whole-share buys, silently dropped its legs on
            # fractional deltas, and its resting legs conflicted with every
            # other sell path. ALL protection now comes from the single
            # stop-reconcile pass at the end of this run — one mechanism.
            log.info(
                "    BUY %-6s  delta=+%.0f sh  price=$%.2f  cost=$%.2f"
                "  weight=%.0f pct%s",
                ticker, delta_qty, price, cost, weight * 100, reason_note,
            )
            order = alpaca.place_market_order(
                client, ticker, delta_qty, "buy", dry_run=dry_run,
            )
            order["action"]       = "BUY"
            order["target_value"] = round(cost, 2)
            order["weight"]       = weight
            order["rationale"]    = sig.get("entry_rationale", reconcile_reason)
            if order.get("status") not in ("failed",):
                exposure_budget -= cost
            if not dry_run:
                if order.get("filled_avg_price"):
                    log.info("      -> filled @ $%.2f", order["filled_avg_price"])
                else:
                    log.warning("      -> NOT CONFIRMED FILLED (status=%s) — verify in Alpaca", order.get("status"))
            orders.append(order)
            available_cash -= cost

        else:
            # delta_qty < 0: trim overweight position
            trim_qty = abs(delta_qty)
            log.info(
                "    TRIM %-6s  delta=%.4f  price=$%.2f  (overweight trim)%s",
                ticker, -delta_qty, price, reason_note,
            )
            # sell_with_cleanup cancels the ticker's resting stop first
            # (audit P0-2); the reconcile pass below re-attaches it at the
            # reduced quantity.
            order = alpaca.sell_with_cleanup(client, ticker, qty=trim_qty,
                                             dry_run=dry_run)
            order["action"]    = "TRIM"
            order["rationale"] = reconcile_reason or "weight_drift_trim"
            if not dry_run:
                if order.get("filled_avg_price"):
                    log.info("      -> filled @ $%.2f", order["filled_avg_price"])
                else:
                    log.warning("      -> NOT CONFIRMED FILLED (status=%s) — verify in Alpaca", order.get("status"))
            orders.append(order)
            available_cash += trim_qty * price

    # ── Stop reconcile: every held position ends the run protected ────────
    # THE single stop mechanism (audit P0-1/P1-1). Places a stop where one is
    # missing, ratchets existing stops up on winners, re-syncs quantity after
    # trims. Runs in dry-run mode too so the preview shows what would happen.
    stop_reconcile = None
    try:
        from broker.stop_loss import reconcile_protective_stops
        log.info("\n  [PROTECT] Reconciling protective stops for all positions...")
        stop_reconcile = reconcile_protective_stops(client, regime_key, dry_run=dry_run)
    except Exception as exc:
        log.error("  ⚠️  Stop reconcile FAILED (%s) — positions may be "
                  "unprotected, run scripts/protect_positions.py", exc)

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
        "skipped_exposure_cap", "skipped_too_expensive", "skipped_drawdown_pause",
        "skipped_trading_paused",
    }
    orders_submitted = [o for o in orders if o.get("status") not in terminal_statuses
                        and o.get("status") != "failed"]
    summary = {
        "signals_processed": len(signals),
        "exits":             len(exit_signals),
        "holds":             len(hold_signals),
        "buys":              len(buy_signals),
        "orders_placed":     sum(1 for o in orders
                                 if o.get("status") not in terminal_statuses),
        "orders_failed":     sum(1 for o in orders if o.get("status") == "failed"),
        # Submitted but NOT confirmed filled within the poll window (status
        # still "accepted"/"pending_new" etc.) — worth flagging separately
        # from a genuine fill, since we don't yet know the real price.
        "orders_unconfirmed": sum(1 for o in orders_submitted
                                  if not o.get("filled_avg_price")) if not dry_run else 0,
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
    if summary["orders_unconfirmed"]:
        log.warning("    Orders unconfirmed (submitted, no fill price yet): %d",
                    summary["orders_unconfirmed"])

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
        "stop_reconcile": stop_reconcile,
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
