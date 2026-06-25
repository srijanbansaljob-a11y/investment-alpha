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
    from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, QueryOrderStatus
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
    else:
        api_key    = os.getenv("ALPACA_API_KEY", "").strip()
        secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
        label = "Pipeline"

    if not api_key or api_key.startswith("PASTE_"):
        raise ValueError(
            f"ALPACA_API_KEY{'_SCREENER' if portfolio == 'screener' else ''} not set. "
            f"Open your .env file and paste your Alpaca {label} paper trading key."
        )
    if not secret_key or secret_key.startswith("PASTE_"):
        raise ValueError(
            f"ALPACA_SECRET_KEY{'_SCREENER' if portfolio == 'screener' else ''} not set. "
            f"Open your .env file and paste your Alpaca {label} paper trading secret."
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

def place_market_order(
    client: TradingClient,
    ticker: str,
    qty: float,
    side: str,               # "buy" or "sell"
    dry_run: bool = False,
) -> dict:
    """
    Place a market order for a given ticker and quantity.

    Args:
        client:  Authenticated TradingClient
        ticker:  Stock symbol e.g. "AAPL"
        qty:     Number of shares (fractional supported by Alpaca)
        side:    "buy" or "sell"
        dry_run: If True, log the order but don't submit it

    Returns dict with order details.
    """
    side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

    if dry_run:
        log.info(f"  [DRY-RUN] Would place {side.upper()} {qty:.4f} shares of {ticker}")
        return {
            "ticker":  ticker,
            "side":    side,
            "qty":     qty,
            "status":  "dry_run",
            "order_id": None,
        }

    try:
        req = MarketOrderRequest(
            symbol=ticker,
            qty=round(qty, 4),
            side=side_enum,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(req)
        log.info(f"  ✅ Order placed: {side.upper()} {qty:.4f} × {ticker} | ID: {order.id}")
        return {
            "ticker":    ticker,
            "side":      side,
            "qty":       qty,
            "status":    str(order.status),
            "order_id":  str(order.id),
            "submitted_at": str(order.submitted_at),
        }
    except Exception as e:
        log.error(f"  ❌ Order failed: {side.upper()} {ticker} — {e}")
        return {
            "ticker":  ticker,
            "side":    side,
            "qty":     qty,
            "status":  "failed",
            "error":   str(e),
        }


def close_position(
    client: TradingClient,
    ticker: str,
    dry_run: bool = False,
) -> dict:
    """
    Close (sell) the entire position in a ticker.
    Safer than calculating qty manually — Alpaca handles it.
    """
    if dry_run:
        log.info(f"  [DRY-RUN] Would close entire position in {ticker}")
        return {"ticker": ticker, "status": "dry_run"}

    try:
        order = client.close_position(ticker)
        log.info(f"  ✅ Position closed: {ticker} | ID: {order.id}")
        return {
            "ticker":   ticker,
            "status":   str(order.status),
            "order_id": str(order.id),
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