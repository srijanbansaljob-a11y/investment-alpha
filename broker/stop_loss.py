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
    """Fetch latest close prices for a list of tickers via yfinance."""
    if not tickers:
        return {}
    try:
        raw = yf.download(tickers, period="2d", auto_adjust=True, progress=False)
        prices = {}
        if len(tickers) == 1:
            close = raw["Close"].squeeze()
            prices[tickers[0]] = float(close.iloc[-1])
        else:
            for t in tickers:
                try:
                    prices[t] = float(raw["Close"][t].dropna().iloc[-1])
                except Exception:
                    logger.warning("Could not get price for %s", t)
        return prices
    except Exception as exc:
        logger.error("yfinance price fetch failed: %s", exc)
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
    Compute Average True Range (ATR) for a ticker over `period` days.
    Uses 60 days of OHLC data to ensure enough history after any gaps.
    Returns the ATR in price units, or None on error.
    """
    try:
        raw = yf.download(ticker, period="60d", auto_adjust=True, progress=False)
        if raw.empty or len(raw) < period + 1:
            return None
        high  = raw["High"].squeeze()
        low   = raw["Low"].squeeze()
        close = raw["Close"].squeeze()
        prev_close = close.shift(1)
        tr = (
            (high - low).abs()
            .combine((high - prev_close).abs(), max)
            .combine((low  - prev_close).abs(), max)
        )
        atr = float(tr.rolling(period).mean().iloc[-1])
        return round(atr, 4) if not (atr != atr) else None  # NaN guard
    except Exception as exc:
        logger.debug("ATR compute error for %s: %s", ticker, exc)
        return None


def _exit_via_alpaca(ticker: str) -> bool:
    """
    Close a full position via Alpaca.
    Returns True on success, False on error.
    """
    try:
        from broker.alpaca_client import get_trading_client
        client = get_trading_client()
        client.close_position(ticker)
        logger.info("Alpaca: closed position %s", ticker)
        return True
    except Exception as exc:
        logger.error("Alpaca close_position(%s) failed: %s", ticker, exc)
        return False


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

    state = _load_portfolio_state()
    if not state:
        logger.info("Portfolio state empty – nothing to check")
        return {"checked": [], "triggered": [], "skipped": [], "log": []}

    # Resolve regime
    if regime is None:
        regime = state.get("regime", "bull").lower()
    regime = regime.lower()
    if regime not in config.STOP_LOSS_PCT:
        logger.warning("Unknown regime '%s', defaulting to bull", regime)
        regime = "bull"

    stop_multiplier = config.STOP_LOSS_PCT[regime]
    use_atr = getattr(config, "USE_ATR_STOP_LOSS", False)
    atr_multipliers = getattr(config, "ATR_STOP_MULTIPLIER", {"bull": 2.5, "neutral": 2.0, "bear": 1.5})
    atr_period      = getattr(config, "ATR_PERIOD", 14)
    atr_mult        = atr_multipliers.get(regime, 2.0)
    logger.info(
        "Stop-loss check | regime=%s | mode=%s | dry_run=%s",
        regime.upper(), f"ATR({atr_period})×{atr_mult}" if use_atr else f"fixed {stop_multiplier:.0%}", dry_run,
    )

    # Extract held positions from state
    portfolio = state.get("portfolio", [])
    if not portfolio:
        logger.info("No positions in portfolio state")
        return {"checked": [], "triggered": [], "skipped": [], "log": []}

    held = []
    skipped = []
    for pos in portfolio:
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
        logger.info("No positions with entry_price – nothing to check")
        return {"checked": [], "triggered": [], "skipped": skipped, "log": []}

    # Fetch current prices
    tickers = [h["ticker"] for h in held]
    prices = _get_current_prices(tickers)

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

        # ── Stop price: ATR-based or fixed % ─────────────────────────────
        atr_value  = None
        stop_method = "fixed_pct"
        if use_atr:
            atr_value = _compute_atr(ticker, period=atr_period)
            if atr_value is not None and atr_value > 0:
                stop_price  = entry_price - (atr_mult * atr_value)
                stop_method = f"ATR({atr_period})×{atr_mult}"
            else:
                # Fallback to fixed % if ATR unavailable
                logger.warning("%s: ATR unavailable, falling back to fixed %% stop", ticker)
                stop_price = entry_price * stop_multiplier
        else:
            stop_price = entry_price * stop_multiplier

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
