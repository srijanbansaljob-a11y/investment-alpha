"""
broker/stop_loss.py - Weekly Stop-Loss Checker

Reads latest_portfolio.json for currently held positions (entry_price +
entry_date), fetches live prices via yfinance, and exits any position
that has breached its regime-based stop-loss threshold.

Stop-loss thresholds (from config):
  BULL    : exit if current_price < entry_price * 0.85  (15% loss)
  NEUTRAL : exit if current_price < entry_price * 0.88  (12% loss)
  BEAR    : exit if current_price < entry_price * 0.90  (10% loss)

Usage
-----
  # Dry run (default - just prints what would be exited):
  python broker/stop_loss.py

  # Live execution (places real paper orders via Alpaca):
  python broker/stop_loss.py --execute

  # Use a specific regime override:
  python broker/stop_loss.py --regime bear

Schedule weekly (every Monday before market open) via Task Scheduler.
"""

import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone

import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────

def _load_portfolio_state() -> dict:
    """Load latest_portfolio.json; return empty dict if not found.
    Auto-repairs OneDrive null-byte corruption silently.
    """
    path = config.PORTFOLIO_STATE_FILE
    if not path.exists():
        logger.warning("No portfolio state file at %s", path)
        return {}
    try:
        raw = path.read_bytes().rstrip(b'\x00')
        return json.loads(raw)
    except json.JSONDecodeError:
        # Corruption beyond null bytes — try restoring from newest timestamped backup
        import glob
        backups = sorted(glob.glob(str(config.OUTPUT_DIR / "portfolio_*.json")))
        for bk in reversed(backups):
            try:
                raw = Path(bk).read_bytes().rstrip(b'\x00')
                obj = json.loads(raw)
                path.write_bytes(raw)
                logger.warning("latest_portfolio.json was corrupt — restored from %s", Path(bk).name)
                return obj
            except Exception:
                continue
        logger.error("All portfolio backups corrupt — starting fresh")
        return {}


def _get_current_prices(tickers: list) -> dict:
    """Latest prices via the real-time layer (Alpaca IEX → Finnhub → yfinance)."""
    if not tickers:
        return {}
    try:
        from broker import market_data
        return market_data.get_latest_prices(tickers)
    except Exception as exc:
        logger.error("Price fetch failed: %s", exc)
        return {}


def _log_exit(log_entries: list) -> None:
    """Append stop-loss exit events to STOP_LOSS_LOG_FILE."""
    log_path = config.STOP_LOSS_LOG_FILE
    existing = []
    if log_path.exists():
        with open(log_path, "r") as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                existing = []
    existing.extend(log_entries)
    with open(log_path, "w") as f:
        json.dump(existing, f, indent=2)
    logger.info("Stop-loss log updated: %s entries total", len(existing))


def _compute_atr(ticker: str, period: int = 14) -> float | None:
    """
    Average True Range via the real-time data layer (Alpaca daily bars,
    yfinance fallback inside market_data). Kept here for backward
    compatibility — monitor.py and remote_commands.py import it.
    """
    try:
        from broker import market_data
        return market_data.compute_atr(ticker, period=period)
    except Exception as exc:
        logger.debug("ATR compute error for %s: %s", ticker, exc)
        return None


def _exit_via_alpaca(ticker: str) -> bool:
    """
    Close a full position via Alpaca.
    Returns True on success, False on error.
    """
    try:
        from broker.alpaca_client import get_client, sell_with_cleanup
        client = get_client()
        r = sell_with_cleanup(client, ticker, dry_run=False)
        ok = r.get("status") not in ("failed",)
        if ok:
            logger.info("Alpaca: closed position %s (cancelled %d resting order(s) first)",
                        ticker, r.get("cancelled_orders", 0))
        else:
            logger.error("Alpaca close of %s failed: %s", ticker, r.get("error"))
        return ok
    except Exception as exc:
        logger.error("Alpaca close_position(%s) failed: %s", ticker, exc)
        return False


def _load_alpaca_holdings() -> dict | None:
    """
    Live positions from Alpaca as {ticker: avg_entry_price}.

    Returns:
        dict  — the live holdings. An EMPTY dict means the account is genuinely
                flat, which is authoritative information, not a failure.
        None  — Alpaca could not be reached; caller may fall back to the file.

    BUG FIX 2026-07-31: this used to return {} in BOTH cases, and the caller
    treated falsy as "unavailable". So a legitimately empty account silently
    fell back to a stale state file and the checker evaluated positions that
    had been sold weeks earlier — logging phantom stop-loss breaches for names
    the account did not hold (observed live: NEM and FDX "triggered" on an
    empty book right after a full liquidation). Those phantom breaches also
    feed the re-entry cooldown, so they would have blocked real buys.

    Alpaca's avg_entry_price is the REAL cost basis — the correct baseline for
    a stop-loss. The state file's entry_price can be stale, so we prefer this.
    """
    try:
        from broker.alpaca_client import get_client, get_positions
        positions = get_positions(get_client())
        out = {}
        for tkr, p in positions.items():
            basis = p.get("avg_entry_price") or p.get("cost_basis")
            if basis:
                out[tkr] = float(basis)
        return out
    except Exception as exc:
        logger.warning("Could not load Alpaca holdings (%s) — using state file", exc)
        return None


# ── Shared stop-price calculation ───────────────────────────────────────────
#
# Single source of truth for stop-loss price math. Both this module's weekly
# batch checker and broker/monitor.py's 15-min intraday check call this —
# previously each reimplemented the same ATR/fixed-pct logic independently,
# which meant an edit to one could silently drift from the other.

def compute_stop_price(ticker: str, entry_price: float, regime: str = "bull") -> tuple[float, str, float | None]:
    """
    Compute the stop-loss price for a position.

    Returns (stop_price, stop_method, atr_value). atr_value is None when the
    fixed-percentage fallback was used (ATR disabled or unavailable).

    2026-07-27: the raw ATR result is now clamped to
    [STOP_PCT_FLOOR, STOP_PCT_CAP] below entry. Previously `entry - mult*ATR`
    was unbounded, so a very quiet stock could yield a ~1% stop (hit by noise)
    and a very volatile one a 20%+ stop (no real protection). This mirrors the
    floor/cap guard already proven in the screener.
    """
    regime = (regime or "bull").lower()
    if regime not in config.STOP_LOSS_PCT:
        regime = "bull"

    use_atr = getattr(config, "USE_ATR_STOP_LOSS", False)
    atr_multipliers = getattr(config, "ATR_STOP_MULTIPLIER", {"bull": 2.5, "neutral": 2.0, "bear": 1.5})
    atr_period = getattr(config, "ATR_PERIOD", 14)
    atr_mult = atr_multipliers.get(regime, 2.0)

    floor_pct = getattr(config, "STOP_PCT_FLOOR", 0.03)
    cap_pct   = getattr(config, "STOP_PCT_CAP",   0.12)

    if use_atr:
        atr_value = _compute_atr(ticker, period=atr_period)
        if atr_value is not None and atr_value > 0 and entry_price > 0:
            raw_stop = entry_price - (atr_mult * atr_value)
            raw_pct  = (entry_price - raw_stop) / entry_price
            clamped  = min(max(raw_pct, floor_pct), cap_pct)
            stop     = entry_price * (1 - clamped)
            method   = f"ATR({atr_period})x{atr_mult}"
            if abs(clamped - raw_pct) > 1e-9:
                which = "floor" if clamped > raw_pct else "cap"
                method += f" [{which} {clamped*100:.0f}%]"
                logger.info("%s: ATR stop %.1f%% clamped to %.1f%% by %s",
                            ticker, raw_pct * 100, clamped * 100, which)
            return stop, method, atr_value
        logger.warning("%s: ATR unavailable, falling back to fixed %% stop", ticker)

    stop_multiplier = config.STOP_LOSS_PCT.get(regime, 0.85)
    return entry_price * stop_multiplier, "fixed_pct", None


def compute_take_profit(ticker: str, entry_price: float, regime: str = "bull",
                        atr_value: float | None = None) -> tuple[float | None, float | None, str]:
    """
    Compute the take-profit ceiling and the earlier monitor-alert level.

    Returns (ceiling_price, monitor_price, method). Both are None when
    take-profit is disabled or no ATR is available.

    Tiered like the screener: `ceiling` is the hard limit Alpaca enforces as
    the bracket's take_profit leg; `monitor` fires earlier (default 80% of the
    way there) so you get a Discord alert and a chance to decide before the
    automatic exit triggers.
    """
    if not getattr(config, "TAKE_PROFIT_ENABLED", True) or entry_price <= 0:
        return None, None, "disabled"

    regime = (regime or "bull").lower()
    mults  = getattr(config, "ATR_TAKE_PROFIT_MULTIPLIER",
                     {"bull": 5.0, "neutral": 4.0, "bear": 3.0})
    mult   = mults.get(regime, 4.0)
    floor_pct = getattr(config, "TAKE_PROFIT_PCT_FLOOR", 0.08)
    cap_pct   = getattr(config, "TAKE_PROFIT_PCT_CAP",   0.35)
    ratio     = getattr(config, "TAKE_PROFIT_MONITOR_RATIO", 0.80)

    if atr_value is None:
        atr_value = _compute_atr(ticker, period=getattr(config, "ATR_PERIOD", 14))

    if atr_value is None or atr_value <= 0:
        return None, None, "atr_unavailable"

    raw_pct = (mult * atr_value) / entry_price
    clamped = min(max(raw_pct, floor_pct), cap_pct)
    ceiling = entry_price * (1 + clamped)
    monitor = entry_price * (1 + clamped * ratio)
    return ceiling, monitor, f"ATRx{mult} [{clamped*100:.0f}%]"


# ── Stop reconciler — THE single stop mechanism (audit P0-1 / P1-1) ────────
#
# One rule: every held position ends every execute run with exactly one
# correct GTC SELL STOP resting at the broker. Stops are computed from
# max(entry, current) so they ratchet UP on winners (trailing discipline,
# same logic proven in scripts/protect_positions.py) and are NEVER lowered.
# This replaces bracket legs on buys AND the old blanket cancel/re-place:
# buys go out as plain market orders, then this pass attaches protection.
# Take-profit is a monitor ALERT, not a resting limit order — a hard sell
# ceiling on a momentum book amputates the right tail, and a resting TP
# would hold shares and re-create the P0-2 sell-conflict.

def reconcile_protective_stops(client=None, regime: str = "bull",
                               dry_run: bool = True) -> dict:
    """
    Ensure every held position has one correct resting stop.

    For each Alpaca position:
      - no resting stop            → place one (whole shares)
      - resting stop below desired → cancel & replace (ratchet up)
      - resting qty out of sync    → cancel & replace at current whole qty
      - resting stop >= desired    → leave alone (never lower a stop)

    Returns {"placed": [...], "replaced": [...], "kept": [...], "failed": [...]}.
    """
    from broker.alpaca_client import (
        get_client, get_positions, get_resting_stops,
        cancel_orders_for_symbol, place_stop_order,
    )
    if client is None:
        client = get_client()
    regime = (regime or "bull").lower()
    positions = get_positions(client)
    resting   = get_resting_stops(client)

    out = {"placed": [], "replaced": [], "kept": [], "failed": []}
    for ticker, pos in sorted(positions.items()):
        qty   = float(pos["qty"])
        whole = int(qty)
        if whole < 1:
            continue
        entry   = float(pos.get("avg_entry_price") or 0)
        current = float(pos.get("current_price") or 0)
        anchor  = max(entry, current) if entry and current else (entry or current)
        if not anchor:
            out["failed"].append({"ticker": ticker, "reason": "no_price"})
            continue

        desired, method, _atr = compute_stop_price(ticker, anchor, regime)
        desired = round(desired, 2)
        existing = resting.get(ticker)

        if existing:
            ex_stop = existing["stop_price"]
            qty_ok  = abs(existing["qty"] - whole) < 1
            if ex_stop >= desired - 0.01 and qty_ok:
                out["kept"].append({"ticker": ticker, "stop": ex_stop})
                continue
            reason = "ratchet_up" if ex_stop < desired - 0.01 else "qty_sync"
            logger.info("  %s: replacing stop $%.2f -> $%.2f (%s, %s)",
                        ticker, ex_stop, max(desired, ex_stop), reason, method)
            if not dry_run:
                cancel_orders_for_symbol(client, ticker)
            # Never lower: qty_sync keeps the higher of the two levels
            new_stop = max(desired, ex_stop)
            r = place_stop_order(client, ticker, whole, new_stop, dry_run=dry_run)
            (out["replaced"] if r.get("status") not in ("failed",) else out["failed"]).append(
                {"ticker": ticker, "old": ex_stop, "new": new_stop,
                 "reason": reason, "status": r.get("status")})
        else:
            logger.info("  %s: no resting stop — placing $%.2f (%s)", ticker, desired, method)
            r = place_stop_order(client, ticker, whole, desired, dry_run=dry_run)
            (out["placed"] if r.get("status") not in ("failed",) else out["failed"]).append(
                {"ticker": ticker, "stop": desired, "status": r.get("status")})

    n_prot = len(out["placed"]) + len(out["replaced"]) + len(out["kept"])
    logger.info("Stop reconcile%s: %d/%d positions protected "
                "(%d placed, %d replaced, %d kept, %d failed)",
                " [DRY]" if dry_run else "", n_prot, len(positions),
                len(out["placed"]), len(out["replaced"]), len(out["kept"]),
                len(out["failed"]))
    if len(positions) and n_prot < len(positions):
        logger.warning("⚠️  %d position(s) remain UNPROTECTED — investigate: %s",
                       len(positions) - n_prot,
                       [f["ticker"] for f in out["failed"]])
    return out


# ── Core Logic ─────────────────────────────────────────────────────────────

def check_and_execute(regime: str = None, dry_run: bool = True) -> dict:
    """
    Check all held positions against stop-loss thresholds.

    Parameters
    ----------
    regime   : override regime string ("bull"|"neutral"|"bear").
               If None, reads regime from portfolio state or defaults to "bull".
    dry_run  : if True, only logs what would happen; no Alpaca calls.

    Returns
    -------
    dict with keys:
        checked   : list of tickers evaluated
        triggered : list of tickers that hit stop-loss
        skipped   : list of tickers with no entry_price (can't evaluate)
        log       : list of detailed event dicts
    """
    if not config.STOP_LOSS_ENABLED:
        logger.info("Stop-loss disabled (STOP_LOSS_ENABLED=False)")
        return {"checked": [], "triggered": [], "skipped": [], "log": []}

    state = _load_portfolio_state() or {}

    # Resolve regime
    if regime is None:
        regime = state.get("regime", "bull").lower()
    regime = regime.lower()
    if regime not in config.STOP_LOSS_PCT:
        logger.warning("Unknown regime '%s', defaulting to bull", regime)
        regime = "bull"

    use_atr = getattr(config, "USE_ATR_STOP_LOSS", False)
    atr_period = getattr(config, "ATR_PERIOD", 14)
    atr_mult   = getattr(config, "ATR_STOP_MULTIPLIER", {"bull": 2.5, "neutral": 2.0, "bear": 1.5}).get(regime, 2.0)
    stop_multiplier = config.STOP_LOSS_PCT[regime]
    logger.info(
        "Stop-loss check | regime=%s | mode=%s | dry_run=%s",
        regime.upper(), f"ATR({atr_period})×{atr_mult}" if use_atr else f"fixed {stop_multiplier:.0%}", dry_run,
    )

    # Holdings: Alpaca is the source of truth (real avg_entry_price). Fall back
    # to the state file only when Alpaca is unavailable.
    state_by_ticker = {p.get("ticker"): p for p in state.get("portfolio", []) if p.get("ticker")}
    alpaca_holdings = _load_alpaca_holdings()

    held = []
    skipped = []
    # `is not None` — an empty dict is Alpaca telling us the account is FLAT,
    # which is authoritative. Only a genuine connection failure (None) may fall
    # back to the state file. See _load_alpaca_holdings.
    if alpaca_holdings is not None:
        if not alpaca_holdings:
            logger.info("Holdings source: Alpaca — account is FLAT (0 positions), "
                        "nothing to check")
            return {"checked": [], "triggered": [], "skipped": [], "log": []}
        logger.info("Holdings source: Alpaca (%d positions, real avg_entry_price)",
                    len(alpaca_holdings))
        for ticker, entry_price in alpaca_holdings.items():
            sd = state_by_ticker.get(ticker, {})
            held.append({"ticker": ticker, "entry_price": float(entry_price),
                         "entry_date": sd.get("entry_date", "unknown")})
    else:
        logger.info("Holdings source: state file (Alpaca unavailable)")
        for pos in state.get("portfolio", []):
            ticker = pos.get("ticker")
            entry_price = pos.get("entry_price")
            if not ticker:
                continue
            if entry_price is None:
                logger.warning("%s has no entry_price – skipping stop-loss check", ticker)
                skipped.append(ticker)
                continue
            held.append({"ticker": ticker, "entry_price": float(entry_price),
                         "entry_date": pos.get("entry_date", "unknown")})

    if not held:
        logger.info("No positions to check (no Alpaca holdings, empty/no state)")
        return {"checked": [], "triggered": [], "skipped": skipped, "log": []}

    # Fetch current prices
    tickers = [h["ticker"] for h in held]
    prices = _get_current_prices(tickers)

    # Resting broker-side stops are the SOURCE OF TRUTH for stop levels
    # (audit P1-1). Recomputed levels are advisory fallbacks for positions
    # that have no resting protection.
    resting_stops = {}
    try:
        from broker.alpaca_client import get_client as _gc, get_resting_stops as _grs
        resting_stops = _grs(_gc())
    except Exception as exc:
        logger.warning("Could not read resting stops (%s) — using computed levels", exc)

    checked = []
    triggered = []
    log_entries = []
    ts = datetime.now(timezone.utc).isoformat()

    for pos in held:
        ticker = pos["ticker"]
        entry_price = pos["entry_price"]
        current_price = prices.get(ticker)

        if current_price is None:
            logger.warning("No current price for %s – skipping", ticker)
            skipped.append(ticker)
            continue

        # ── Stop price: resting broker order first, computed fallback ────
        if ticker in resting_stops:
            stop_price  = resting_stops[ticker]["stop_price"]
            stop_method = "resting@broker"
            atr_value   = None
        else:
            stop_price, stop_method, atr_value = compute_stop_price(ticker, entry_price, regime)
            stop_method += " [NOT at broker]"

        loss_pct = (current_price - entry_price) / entry_price * 100
        breached  = current_price < stop_price

        checked.append(ticker)

        event = {
            "ticker":        ticker,
            "entry_price":   round(entry_price, 4),
            "entry_date":    pos["entry_date"],
            "current_price": round(current_price, 4),
            "stop_price":    round(stop_price, 4),
            "atr_value":     round(atr_value, 4) if atr_value is not None else None,
            "stop_method":   stop_method,
            "loss_pct":      round(loss_pct, 2),
            "regime":        regime,
            "breached":      breached,
            "executed":      False,
            "dry_run":       dry_run,
            "timestamp":     ts,
        }

        if breached:
            atr_info = f" | ATR={atr_value:.4f}×{atr_mult}" if atr_value else ""
            logger.warning(
                "STOP-LOSS TRIGGERED: %s | entry=%.2f | current=%.2f | "
                "stop=%.2f [%s%s] | loss=%.1f%%",
                ticker, entry_price, current_price, stop_price, stop_method, atr_info, loss_pct,
            )
            triggered.append(ticker)

            if not dry_run:
                if ticker in resting_stops:
                    # The broker's own GTC stop is at/above this price and will
                    # (or already did) fire — selling locally too would double-
                    # sell or race the broker. Detection only (audit P1-1).
                    logger.info("  %s: resting broker stop $%.2f handles the exit — "
                                "no local order placed", ticker, stop_price)
                    event["executed"] = False
                    event["handled_by"] = "broker_resting_stop"
                else:
                    success = _exit_via_alpaca(ticker)
                    event["executed"] = success
            else:
                logger.info("  [DRY RUN] Would exit %s at %.2f", ticker, current_price)
                event["executed"] = False
        else:
            logger.info(
                "OK: %s | entry=%.2f | current=%.2f | stop=%.2f | loss=%.1f%%",
                ticker, entry_price, current_price, stop_price, loss_pct,
            )

        log_entries.append(event)

    # Persist log
    if log_entries:
        _log_exit(log_entries)

    summary = {
        "checked":   checked,
        "triggered": triggered,
        "skipped":   skipped,
        "log":       log_entries,
    }

    logger.info(
        "Stop-loss complete: %d checked, %d triggered, %d skipped",
        len(checked), len(triggered), len(skipped),
    )
    return summary


# ── Standalone entry point ─────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stop-loss checker")
    parser.add_argument("--execute",  action="store_true",
                        help="Execute real orders via Alpaca (default: dry run)")
    parser.add_argument("--regime",   type=str, default=None,
                        choices=["bull", "neutral", "bear"],
                        help="Override regime instead of reading from state")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    print("\n=== Stop-Loss Checker ===")
    result = check_and_execute(
        regime=args.regime,
        dry_run=not args.execute,
    )
    print(f"\nChecked   : {result['checked']}")
    print(f"Triggered : {result['triggered']}")
    print(f"Skipped   : {result['skipped']}")
    if result["triggered"]:
        print(f"\n{'EXITS NEEDED' if args.execute else 'DRY RUN - Would exit'}:")
        for e in result["log"]:
            if e["breached"]:
                print(
                    f"  {e['ticker']:6s}  entry={e['entry_price']:.2f}  "
                    f"current={e['current_price']:.2f}  "
                    f"loss={e['loss_pct']:.1f}%  executed={e['executed']}"
                )
