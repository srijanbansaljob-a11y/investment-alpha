"""
broker/alpaca.py — Alpaca Paper Trading Connection

Handles all communication with the Alpaca API:
  - Loading credentials from .env (never hardcoded)
  - Verifying the connection and account status
  - Fetching account equity, cash, and current positions
  - Placing and checking market orders
  - Cancelling open orders

PAPER TRADING ONLY — always uses paper-api.alpaca.markets endpoint.
The live endpoint is intentionally not included to prevent accidents.
"""

import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

sys.path.append(str(Path(__file__).parent.parent))  # append not insert — avoids shadowing alpaca-py

# Load .env file
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # dotenv optional — env vars may be set directly

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest, GetOrdersRequest, StopOrderRequest,
        TakeProfitRequest, StopLossRequest,
    )
    from alpaca.trading.enums import (
        OrderSide, TimeInForce, OrderStatus, QueryOrderStatus, OrderClass,
    )
    from alpaca.data.historical import StockHistoricalDataClient
except ImportError:
    raise ImportError(
        "alpaca-py not installed. Run: pip install alpaca-py"
    )

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────
PAPER_BASE_URL = "https://paper-api.alpaca.markets"


# ── Client Factory ─────────────────────────────────────────────────────────

def get_client(portfolio: str = "pipeline") -> TradingClient:
    """
    Create and return an authenticated Alpaca TradingClient.

    Args:
        portfolio: "screener" → uses ALPACA_API_KEY_SCREENER / ALPACA_SECRET_KEY_SCREENER
                   "pipeline" (default) → uses ALPACA_API_KEY / ALPACA_SECRET_KEY

    Reads credentials from environment variables (set via .env file).
    Raises clear errors if keys are missing or invalid.
    """
    portfolio = (portfolio or "pipeline").lower().strip()
    if portfolio == "screener":
        api_key    = os.getenv("ALPACA_API_KEY_SCREENER", "").strip()
        secret_key = os.getenv("ALPACA_SECRET_KEY_SCREENER", "").strip()
        label = "Screener"
        # Graceful fallback: if screener-specific keys aren't set, use pipeline keys
        # (happens on local runs where .env only has one set of credentials)
        if not api_key or api_key.startswith("PASTE_"):
            api_key    = os.getenv("ALPACA_API_KEY", "").strip()
            secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
            import logging as _log
            _log.getLogger(__name__).warning(
                "ALPACA_API_KEY_SCREENER not set — falling back to pipeline keys for screener portfolio"
            )
            label = "Screener (using pipeline keys)"
    else:
        api_key    = os.getenv("ALPACA_API_KEY", "").strip()
        secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
        label = "Pipeline"

    if not api_key or api_key.startswith("PASTE_"):
        raise ValueError(
            f"ALPACA_API_KEY not set. Open your .env file and paste your Alpaca paper trading key."
        )
    if not secret_key or secret_key.startswith("PASTE_"):
        raise ValueError(
            f"ALPACA_SECRET_KEY not set. Open your .env file and paste your Alpaca paper trading secret."
        )

    client = TradingClient(
        api_key=api_key,
        secret_key=secret_key,
        paper=True,   # ALWAYS paper — safety lock
    )
    return client


# ── Account Info ───────────────────────────────────────────────────────────

def get_account_summary(client: TradingClient) -> dict:
    """
    Fetch and return a clean summary of the paper trading account.
    """
    acct = client.get_account()
    return {
        "account_number":  acct.account_number,
        "status":          str(acct.status),
        "currency":        acct.currency,
        "equity":          float(acct.equity),
        "cash":            float(acct.cash),
        "buying_power":    float(acct.buying_power),
        "portfolio_value": float(acct.portfolio_value),
        "daytrade_count":  acct.daytrade_count,
        "pattern_day_trader": acct.pattern_day_trader,
        "trading_blocked": acct.trading_blocked,
        "account_blocked": acct.account_blocked,
    }


def get_positions(client: TradingClient) -> dict:
    """
    Fetch current open positions.
    Returns dict keyed by ticker: {qty, market_value, cost_basis, unrealized_pl, side}
    """
    positions = client.get_all_positions()
    result = {}
    for p in positions:
        result[p.symbol] = {
            "ticker":         p.symbol,
            "qty":            float(p.qty),
            "side":           str(p.side),
            "market_value":   float(p.market_value),
            "cost_basis":     float(p.cost_basis),
            "unrealized_pl":  float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc),
            "current_price":  float(p.current_price),
            "avg_entry_price": float(p.avg_entry_price),
        }
    return result


# ── Order Management ───────────────────────────────────────────────────────

_FILL_TERMINAL_STATUSES = {
    "filled", "canceled", "expired", "rejected", "done_for_day", "replaced",
}
_FILL_POLL_TIMEOUT_SEC   = 15
_FILL_POLL_INTERVAL_SEC  = 0.5


def _wait_for_fill(client: TradingClient, order_id, timeout: float = _FILL_POLL_TIMEOUT_SEC):
    """
    Poll Alpaca for an order's terminal status after submission.

    Market orders during regular hours normally fill within 1-2 seconds, but
    submit_order() only ever returns the *acceptance* status ("accepted" /
    "pending_new") — never the fill. Without this poll, "order placed" was
    being logged and counted as success even when the order hadn't actually
    filled yet, which meant we never knew the real execution price and could
    not distinguish a genuine fill from an order still sitting open.

    Returns the final order object (whatever status it reached — caller
    decides what to do with a non-terminal result), or None on error.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = client.get_order_by_id(order_id)
        except Exception as e:
            log.warning("  Could not poll order %s: %s", order_id, e)
            return last
        if str(last.status).lower() in _FILL_TERMINAL_STATUSES:
            return last
        time.sleep(_FILL_POLL_INTERVAL_SEC)
    return last  # timed out — return whatever the last poll showed


def place_market_order(
    client: TradingClient,
    ticker: str,
    qty: float,
    side: str,               # "buy" or "sell"
    dry_run: bool = False,
    stop_price: float | None = None,
    take_profit_price: float | None = None,
) -> dict:
    """
    Place a market order for a given ticker and quantity, then poll until it
    fills (or times out) so we can report the actual execution price.

    Args:
        client:  Authenticated TradingClient
        ticker:  Stock symbol e.g. "AAPL"
        qty:     Number of shares — MUST be a whole number when bracket legs
                 are requested (Alpaca rejects brackets on fractional qty)
        side:    "buy" or "sell"
        dry_run: If True, log the order but don't submit it
        stop_price:        optional protective stop, submitted as a bracket leg
        take_profit_price: optional profit ceiling, submitted as a bracket leg

    When both stop_price and take_profit_price are supplied on a BUY, the order
    is sent as an Alpaca BRACKET so the exit legs rest at the broker. That
    matters: a stop held only in our own monitoring code does nothing if the
    monitor fails or the market gaps while nobody is watching.

    Returns dict with order details, including filled_qty / filled_avg_price
    when a fill was confirmed within the poll window.
    """
    side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

    # Brackets are only valid on entries, and only on whole share counts.
    want_bracket = (
        side.lower() == "buy"
        and stop_price is not None
        and take_profit_price is not None
    )
    if want_bracket and float(qty) != int(float(qty)):
        log.warning(
            "  %s: qty %.4f is fractional — Alpaca rejects bracket orders on "
            "fractional quantities. Submitting WITHOUT protective legs.",
            ticker, qty,
        )
        want_bracket = False

    if dry_run:
        extra = (f"  stop=${stop_price:.2f} tp=${take_profit_price:.2f}"
                 if want_bracket else "  (no bracket)")
        log.info(f"  [DRY-RUN] Would place {side.upper()} {qty:.4f} shares of {ticker}{extra}")
        return {
            "ticker":  ticker,
            "side":    side,
            "qty":     qty,
            "status":  "dry_run",
            "order_id": None,
            "filled_qty": None,
            "filled_avg_price": None,
            "bracket": want_bracket,
            "stop_price": stop_price if want_bracket else None,
            "take_profit_price": take_profit_price if want_bracket else None,
        }

    try:
        if want_bracket:
            req = MarketOrderRequest(
                symbol=ticker,
                qty=int(float(qty)),
                side=side_enum,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=round(float(stop_price), 2)),
                take_profit=TakeProfitRequest(limit_price=round(float(take_profit_price), 2)),
            )
        else:
            req = MarketOrderRequest(
                symbol=ticker,
                qty=round(qty, 4),
                side=side_enum,
                time_in_force=TimeInForce.DAY,
            )
        order = client.submit_order(req)
        log.info(f"  Order submitted: {side.upper()} {qty:.4f} x {ticker} | ID: {order.id}"
                 + (f" | BRACKET stop=${stop_price:.2f} tp=${take_profit_price:.2f}"
                    if want_bracket else " | no protective legs"))

        final = _wait_for_fill(client, order.id) or order
        status = str(final.status)
        filled_qty       = float(final.filled_qty) if getattr(final, "filled_qty", None) else 0.0
        filled_avg_price = float(final.filled_avg_price) if getattr(final, "filled_avg_price", None) else None

        if filled_avg_price:
            log.info(
                f"  ✅ FILLED: {side.upper()} {filled_qty:.4f} x {ticker} "
                f"@ ${filled_avg_price:.2f}  (status={status})"
            )
        else:
            log.warning(
                f"  ⚠️  {ticker} order {order.id} not confirmed filled within "
                f"{_FILL_POLL_TIMEOUT_SEC}s (status={status}) -- check Alpaca directly"
            )

        return {
            "ticker":    ticker,
            "side":      side,
            "qty":       qty,
            "status":    status,
            "order_id":  str(order.id),
            "submitted_at": str(order.submitted_at),
            "filled_qty": filled_qty,
            "filled_avg_price": filled_avg_price,
            "bracket": want_bracket,
            "stop_price": stop_price if want_bracket else None,
            "take_profit_price": take_profit_price if want_bracket else None,
        }
    except Exception as e:
        log.error(f"  ❌ Order failed: {side.upper()} {ticker} — {e}")
        return {
            "ticker":  ticker,
            "side":    side,
            "qty":     qty,
            "status":  "failed",
            "error":   str(e),
            "filled_qty": None,
            "filled_avg_price": None,
            "bracket": False,
        }


def place_stop_order(
    client: TradingClient,
    ticker: str,
    qty: float,
    stop_price: float,
    dry_run: bool = False,
) -> dict:
    """
    Submit a standalone protective SELL STOP against a position already held.

    This is how an existing, unprotected position gets a broker-side stop
    WITHOUT selling and rebuying it — no spread cost, no time out of the
    market, no change to the cost basis.

    Alpaca does not accept stop orders on fractional quantities, so callers
    should pass the whole-share portion (e.g. 538 of a 538.0941 holding). The
    fractional remainder stays unprotected, which is immaterial at that size.
    """
    whole = int(float(qty))
    if whole < 1:
        return {"ticker": ticker, "status": "skipped_sub_one_share", "qty": qty}

    if dry_run:
        log.info(f"  [DRY-RUN] Would place SELL STOP {whole} x {ticker} @ ${stop_price:.2f}")
        return {"ticker": ticker, "qty": whole, "stop_price": round(stop_price, 2),
                "status": "dry_run", "order_id": None}

    try:
        req = StopOrderRequest(
            symbol=ticker,
            qty=whole,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,   # rests until hit or cancelled
            stop_price=round(float(stop_price), 2),
        )
        order = client.submit_order(req)
        log.info(f"  ✅ STOP resting: SELL {whole} x {ticker} @ ${stop_price:.2f} | ID: {order.id}")
        return {"ticker": ticker, "qty": whole, "stop_price": round(stop_price, 2),
                "status": str(order.status), "order_id": str(order.id)}
    except Exception as e:
        log.error(f"  ❌ Stop order failed: {ticker} — {e}")
        return {"ticker": ticker, "qty": whole, "stop_price": round(stop_price, 2),
                "status": "failed", "error": str(e)}


def close_position(
    client: TradingClient,
    ticker: str,
    dry_run: bool = False,
) -> dict:
    """
    Close (sell) the entire position in a ticker, then poll until it fills so
    we can report the real exit price.
    Safer than calculating qty manually — Alpaca handles it.
    """
    if dry_run:
        log.info(f"  [DRY-RUN] Would close entire position in {ticker}")
        return {"ticker": ticker, "status": "dry_run", "filled_qty": None, "filled_avg_price": None}

    try:
        order = client.close_position(ticker)
        log.info(f"  Close submitted: {ticker} | ID: {order.id}")

        final = _wait_for_fill(client, order.id) or order
        status = str(final.status)
        filled_qty       = float(final.filled_qty) if getattr(final, "filled_qty", None) else 0.0
        filled_avg_price = float(final.filled_avg_price) if getattr(final, "filled_avg_price", None) else None

        if filled_avg_price:
            log.info(f"  ✅ CLOSED: {ticker}  qty={filled_qty:.4f} @ ${filled_avg_price:.2f}  (status={status})")
        else:
            log.warning(
                f"  ⚠️  {ticker} close {order.id} not confirmed filled within "
                f"{_FILL_POLL_TIMEOUT_SEC}s (status={status}) -- check Alpaca directly"
            )

        return {
            "ticker":   ticker,
            "status":   status,
            "order_id": str(order.id),
            "filled_qty": filled_qty,
            "filled_avg_price": filled_avg_price,
        }
    except Exception as e:
        log.error(f"  ❌ Close failed: {ticker} — {e}")
        return {"ticker": ticker, "status": "failed", "error": str(e)}


def cancel_open_orders(client: TradingClient) -> int:
    """Cancel all open orders. Returns count cancelled."""
    try:
        cancelled = client.cancel_orders()
        count = len(cancelled) if cancelled else 0
        log.info(f"  Cancelled {count} open orders")
        return count
    except Exception as e:
        log.warning(f"  Could not cancel open orders: {e}")
        return 0


def is_market_open(client: TradingClient) -> bool:
    """Return True if the US market is currently open."""
    try:
        clock = client.get_clock()
        return clock.is_open
    except Exception:
        return False


# ── Connection Test ────────────────────────────────────────────────────────

def test_connection() -> dict:
    """
    Full connection test — verifies credentials and returns account status.
    Call this before running any trades.
    """
    log.info("Testing Alpaca paper trading connection...")
    try:
        client  = get_client()
        account = get_account_summary(client)
        clock   = client.get_clock()
        positions = get_positions(client)

        result = {
            "connected":       True,
            "account_status":  account["status"],
            "portfolio_value": account["portfolio_value"],
            "cash":            account["cash"],
            "buying_power":    account["buying_power"],
            "equity":          account["equity"],
            "trading_blocked": account["trading_blocked"],
            "market_open":     clock.is_open,
            "next_open":       str(clock.next_open),
            "next_close":      str(clock.next_close),
            "open_positions":  len(positions),
            "positions":       positions,
        }
        log.info(f"  ✅ Connected — equity=${account['equity']:,.2f}  cash=${account['cash']:,.2f}")
        log.info(f"  Market open: {clock.is_open}  |  Open positions: {len(positions)}")
        return result

    except ValueError as e:
        log.error(f"  ❌ Credential error: {e}")
        return {"connected": False, "error": str(e)}
    except Exception as e:
        log.error(f"  ❌ Connection failed: {e}")
        return {"connected": False, "error": str(e)}


# ── Quick Test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("\n=== Alpaca Paper Trading — Connection Test ===\n")
    result = test_connection()

    if result["connected"]:
        print(f"✅ Connected successfully!")
        print(f"   Portfolio Value : ${result['portfolio_value']:>12,.2f}")
        print(f"   Cash Available  : ${result['cash']:>12,.2f}")
        print(f"   Buying Power    : ${result['buying_power']:>12,.2f}")
        print(f"   Market Open     : {result['market_open']}")
        print(f"   Next Open       : {result['next_open']}")
        print(f"   Open Positions  : {result['open_positions']}")
        if result["positions"]:
            print(f"\n   Current Positions:")
            for t, p in result["positions"].items():
                print(f"     {t:<6}  qty={p['qty']:.2f}  value=${p['market_value']:,.2f}  P&L=${p['unrealized_pl']:,.2f}")
    else:
        print(f"\u274c Connection failed: {result.get('error')}")