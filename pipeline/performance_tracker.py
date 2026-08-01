"""
pipeline/performance_tracker.py — 3-Month Paper Trading Validator

Tracks live portfolio performance against SPY benchmark during the
paper trading validation period (PAPER_TRADING_START_DATE to +3 months).

What it records on each run:
  - Daily/weekly portfolio value snapshot
  - Cumulative return vs SPY benchmark
  - Per-position P&L (entry → current, unrealised)
  - Closed position P&L (entry → exit, realised) from stop_loss_log.json
  - Sharpe ratio (rolling, annualised)
  - Maximum drawdown
  - Factor attribution: which sub-scores predicted best
  - Win rate, average gain vs average loss

Data is persisted in: data/paper_trading_log.json
Summary report is printed to console and optionally saved as CSV.

Usage:
  python pipeline/performance_tracker.py           # update + print report
  python pipeline/performance_tracker.py --csv     # also export CSV snapshot
  python pipeline/performance_tracker.py --html    # export HTML dashboard
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)

# ── File paths ─────────────────────────────────────────────────────────────
DATA_DIR              = Path(getattr(config, "DATA_DIR", "data"))
OUTPUT_DIR            = Path(getattr(config, "OUTPUT_DIR", "outputs"))
PORTFOLIO_STATE_FILE  = OUTPUT_DIR / "latest_portfolio.json"
STOP_LOSS_LOG_FILE    = OUTPUT_DIR / "stop_loss_log.json"
PAPER_LOG_FILE        = DATA_DIR   / "paper_trading_log.json"
BENCHMARK_TICKER      = getattr(config, "BENCHMARK_TICKER", "SPY")
START_DATE            = getattr(config, "PAPER_TRADING_START_DATE", None)
VALIDATION_MONTHS     = getattr(config, "PAPER_TRADING_MONTHS", 3)


# ── Helpers ────────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.warning("Could not read %s: %s", path, e)
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def _fetch_prices(tickers: list, start: str) -> pd.DataFrame:
    """
    Download adjusted close prices from start date to today.
    Returns DataFrame with columns = tickers, index = dates.
    """
    if not tickers:
        return pd.DataFrame()
    try:
        raw = yf.download(tickers, start=start, progress=False, auto_adjust=True)
        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"]
        else:
            close = raw[["Close"]].rename(columns={"Close": tickers[0]})
        close.columns = [str(c) for c in close.columns]
        return close.dropna(how="all")
    except Exception as e:
        log.error("Price download failed: %s", e)
        return pd.DataFrame()


def _sharpe(returns: np.ndarray, periods_per_year: int = 52) -> float:
    """Annualised Sharpe ratio (assumes weekly returns, rf=0)."""
    if len(returns) < 4:
        return float("nan")
    mean  = np.mean(returns)
    std   = np.std(returns, ddof=1)
    if std == 0:
        return float("nan")
    return round(float(mean / std * np.sqrt(periods_per_year)), 4)


def _max_drawdown(values: list) -> float:
    """Max peak-to-trough drawdown as a positive fraction."""
    arr = np.array(values, dtype=float)
    if len(arr) < 2:
        return 0.0
    running_max = np.maximum.accumulate(arr)
    drawdowns   = (arr - running_max) / running_max
    return round(float(drawdowns.min()), 6)  # negative, so min = worst


# ── Core snapshot function ──────────────────────────────────────────────────

def take_snapshot() -> dict:
    """
    Capture a performance snapshot of the current portfolio.

    Returns a dict with all metrics for this run, appended to paper_trading_log.json.
    """
    ts = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()

    # ── Real account, not a simulation (fixed 2026-08-01) ────────────────
    # This used to invent a €1,000 book: total_capital fell back to 1000
    # (config.TOTAL_CAPITAL never existed), share counts were derived as
    # 1000 x weight / entry_price, and realised P&L was read from the
    # stop-loss log — which broker-side stops never write to. The result was
    # a report claiming "€990.65, alpha -4.14%" for a $113,884 account:
    # confident, precise, and about a portfolio that does not exist.
    # Equity from the broker is the only honest basis.
    try:
        from broker.alpaca_client import get_client, get_account_summary, get_positions
        _client   = get_client()
        acct      = get_account_summary(_client)
        live_pos  = get_positions(_client)
    except Exception as exc:
        log.error("Performance tracker: Alpaca unreachable (%s) — no snapshot taken. "
                  "Refusing to report simulated numbers.", exc)
        return {"timestamp": ts, "status": "broker_unavailable", "error": str(exc)}

    equity   = float(acct.get("equity", 0) or 0)
    cash     = float(acct.get("cash", 0) or 0)
    currency = acct.get("currency", "USD")

    if not live_pos and equity <= 0:
        log.warning("No positions and no equity -- snapshot empty")
        return {"timestamp": ts, "status": "no_positions"}

    state    = _load_json(PORTFOLIO_STATE_FILE, {})
    run_date = state.get("run_date", START_DATE or today)
    regime   = state.get("regime", "unknown")

    # ── Baseline equity: what the account was worth when tracking began ──
    # Stored once so returns are measured against a fixed starting point
    # rather than a moving one. Git-tracked so cloud runs agree.
    baseline_file = config.DATA_DIR / "perf_baseline.json"
    baseline = _load_json(baseline_file, {})
    if not baseline.get("baseline_equity") or baseline.get("start_date") != START_DATE:
        baseline = {"start_date": START_DATE, "baseline_equity": round(equity, 2),
                    "seeded_at": ts}
        _save_json(baseline_file, baseline)
        log.info("Performance baseline seeded: %s %.2f on %s",
                 currency, equity, START_DATE)
    total_capital = float(baseline["baseline_equity"])

    # ── Per-position, straight from the broker ───────────────────────────
    positions_out = []
    portfolio_value = 0.0
    for ticker, p in sorted(live_pos.items()):
        cur_value  = float(p["market_value"])
        unreal_pnl = float(p["unrealized_pl"])
        positions_out.append({
            "ticker":         ticker,
            "qty":            float(p["qty"]),
            "weight":         round(cur_value / equity, 6) if equity else 0,
            "entry_price":    round(float(p["avg_entry_price"]), 4),
            "current_price":  round(float(p["current_price"]), 4),
            "cost_basis":     round(float(p["cost_basis"]), 2),
            "current_value":  round(cur_value, 2),
            "unrealised_pnl": round(unreal_pnl, 2),
            "unrealised_pct": round(float(p["unrealized_plpc"]) * 100, 2),
        })
        portfolio_value += cur_value

    # Realised P&L = everything the equity change isn't explained by open
    # positions. No deposits/withdrawals on a paper account, so this is exact
    # and needs no log file that stops may or may not have written to.
    unrealised_total = sum(p["unrealised_pnl"] for p in positions_out)
    total_value      = equity
    realised_pnl     = equity - total_capital - unrealised_total
    closed_positions = []

    total_return = (total_value - total_capital) / total_capital if total_capital else 0.0

    # ── Benchmark (SPY) return since start ───────────────────────────────
    benchmark_return = None
    if START_DATE:
        bench_df = _fetch_prices([BENCHMARK_TICKER], START_DATE)
        if not bench_df.empty and BENCHMARK_TICKER in bench_df.columns:
            spy_series = bench_df[BENCHMARK_TICKER].dropna()
            if len(spy_series) >= 2:
                benchmark_return = round(
                    float(spy_series.iloc[-1]) / float(spy_series.iloc[0]) - 1, 6
                )

    alpha = (total_return - benchmark_return) if benchmark_return is not None else None

    # ── Load prior snapshots for rolling metrics ─────────────────────────
    prior_log = _load_json(PAPER_LOG_FILE, [])

    # Only snapshots from the CURRENT baseline period are comparable.
    # Before 2026-08-01 this mixed the old simulated ~1,000 series with real
    # account equity of ~114,000. The scale jump was read as a return, giving
    # a Sharpe of 1.537 and a drawdown of -2.09% that described nothing.
    # A snapshot is comparable only if it carries the same baseline_date.
    comparable = [s for s in prior_log
                  if s.get("baseline_date") == baseline.get("start_date")
                  and s.get("total_portfolio_value") is not None]
    prior_values = [s["total_portfolio_value"] for s in comparable]
    prior_values.append(total_value)
    if len(prior_log) > len(comparable):
        log.info("Rolling metrics use %d comparable snapshot(s); %d older "
                 "snapshot(s) from a previous baseline excluded",
                 len(prior_values), len(prior_log) - len(comparable))

    # Weekly returns (approx: each snapshot is ~weekly)
    if len(prior_values) >= 2:
        weekly_rets = np.diff(prior_values) / np.array(prior_values[:-1])
    else:
        weekly_rets = np.array([])

    sharpe  = _sharpe(weekly_rets)
    max_dd  = _max_drawdown(prior_values)

    # ── Win rate across realised positions ───────────────────────────────
    wins   = sum(1 for p in positions_out if p["unrealised_pct"] > 0)
    losses = sum(1 for p in positions_out if p["unrealised_pct"] <= 0)
    win_rate = wins / max(wins + losses, 1)

    avg_gain = float(np.mean([p["unrealised_pct"] for p in positions_out if p["unrealised_pct"] > 0])) \
               if wins > 0 else 0.0
    avg_loss = float(np.mean([p["unrealised_pct"] for p in positions_out if p["unrealised_pct"] <= 0])) \
               if losses > 0 else 0.0

    # ── Validate paper trading is still within window ────────────────────
    in_validation = True
    days_elapsed  = None
    days_remaining = None
    if START_DATE:
        start_dt   = datetime.fromisoformat(START_DATE).date()
        end_dt     = start_dt + timedelta(days=VALIDATION_MONTHS * 30)
        today_dt   = datetime.now(timezone.utc).date()
        days_elapsed   = (today_dt - start_dt).days
        days_remaining = max(0, (end_dt - today_dt).days)
        in_validation  = today_dt <= end_dt

    snapshot = {
        "timestamp":             ts,
        "run_date":              run_date,
        "regime":                regime,
        "currency":              currency,
        "equity":                round(equity, 2),
        "cash":                  round(cash, 2),
        "invested_pct":          round((equity - cash) / equity * 100, 2) if equity else 0,
        "position_count":        len(positions_out),
        "total_capital":         total_capital,
        "baseline_date":         baseline.get("start_date"),
        "portfolio_value":       round(portfolio_value, 2),
        "realised_pnl":          round(realised_pnl, 2),
        "total_portfolio_value": round(total_value, 2),
        "total_return_pct":      round(total_return * 100, 4),
        "benchmark_return_pct":  round(benchmark_return * 100, 4) if benchmark_return is not None else None,
        "alpha_pct":             round(alpha * 100, 4) if alpha is not None else None,
        "sharpe_ratio":          sharpe,
        "max_drawdown_pct":      round(max_dd * 100, 4),
        "win_rate_pct":          round(win_rate * 100, 2),
        "avg_gain_pct":          round(avg_gain, 2),
        "avg_loss_pct":          round(avg_loss, 2),
        "positions":             positions_out,
        "closed_positions":      closed_positions,
        "paper_trading": {
            "in_validation":   in_validation,
            "days_elapsed":    days_elapsed,
            "days_remaining":  days_remaining,
            "start_date":      START_DATE,
            "end_date":        (start_dt + timedelta(days=VALIDATION_MONTHS * 30)).isoformat()
                               if START_DATE else None,
        },
    }

    # ── Append to log ────────────────────────────────────────────────────
    prior_log.append(snapshot)
    _save_json(PAPER_LOG_FILE, prior_log)
    log.info("Snapshot saved -> %s (%d total snapshots)", PAPER_LOG_FILE, len(prior_log))

    return snapshot


# ── Report printing ─────────────────────────────────────────────────────────

def print_report(snapshot: dict) -> None:
    """Print a human-readable performance report to stdout."""
    if snapshot.get("status") in ("broker_unavailable", "no_positions"):
        print(f"\n  Performance snapshot skipped: {snapshot['status']}")
        return
    pt  = snapshot.get("paper_trading", {})
    cur = "$" if snapshot.get("currency", "USD") == "USD" else "€"
    print("\n" + "=" * 65)
    print("  INVESTMENT ALPHA — PAPER TRADING PERFORMANCE REPORT")
    print("=" * 65)
    print(f"  As of          : {snapshot['timestamp'][:10]}")
    print(f"  Paper trading  : day {pt.get('days_elapsed', '?')} of "
          f"{VALIDATION_MONTHS * 30}  ({pt.get('days_remaining', '?')} days remaining)")
    print(f"  Regime         : {snapshot.get('regime', '?').upper()}")
    print()
    print(f"  Baseline equity   : {cur}{snapshot['total_capital']:,.2f}"
          f"  (from {snapshot.get('baseline_date', '?')})")
    print(f"  Account equity    : {cur}{snapshot['total_portfolio_value']:,.2f}  "
          f"({snapshot['total_return_pct']:+.2f}%)")
    print(f"  Cash / invested   : {cur}{snapshot.get('cash', 0):,.2f}  /  "
          f"{snapshot.get('invested_pct', 0):.1f}% invested "
          f"({snapshot.get('position_count', 0)} positions)")
    if snapshot.get("benchmark_return_pct") is not None:
        print(f"  {BENCHMARK_TICKER} return       : {snapshot['benchmark_return_pct']:+.2f}%")
    if snapshot.get("alpha_pct") is not None:
        alpha_pct = snapshot["alpha_pct"]
        arrow = "▲" if alpha_pct > 0 else "▼"
        print(f"  Alpha vs {BENCHMARK_TICKER}    : {arrow} {alpha_pct:+.2f}%")
    print()
    print(f"  Sharpe ratio      : {snapshot.get('sharpe_ratio', float('nan')):.3f}")
    print(f"  Max drawdown      : {snapshot.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Win rate          : {snapshot.get('win_rate_pct', 0):.0f}%  "
          f"(avg gain {snapshot.get('avg_gain_pct', 0):+.2f}% / "
          f"avg loss {snapshot.get('avg_loss_pct', 0):+.2f}%)")
    print()
    print("  POSITIONS")
    print("  " + "-" * 60)
    for pos in snapshot.get("positions", []):
        pnl_arrow = "▲" if pos["unrealised_pct"] > 0 else "▼"
        print(f"  {pos['ticker']:<6}  entry={pos['entry_price']:.2f}  "
              f"current={pos['current_price']:.2f}  "
              f"P&L={pnl_arrow} {pos['unrealised_pct']:+.2f}%  "
              f"({cur}{pos['unrealised_pnl']:+.2f})")
    if snapshot.get("closed_positions"):
        print()
        print("  CLOSED (stop-loss exits)")
        print("  " + "-" * 60)
        for pos in snapshot["closed_positions"]:
            print(f"  {pos['ticker']:<6}  entry={pos['entry_price']:.2f}  "
                  f"exit={pos['exit_price']:.2f}  P&L={cur}{pos['pnl']:+.2f}")
    print("=" * 65)

    if not pt.get("in_validation", True):
        print("\n  ⚠  VALIDATION PERIOD COMPLETE")
        print("     Review factor weights and promote to live trading if performance criteria met.")


def export_csv(snapshot: dict, output_path: Path = None) -> Path:
    """Export position-level data as CSV for external analysis."""
    rows = []
    for pos in snapshot.get("positions", []):
        rows.append({
            "date":            snapshot["timestamp"][:10],
            "ticker":          pos["ticker"],
            "weight_pct":      round(pos["weight"] * 100, 2),
            "entry_price":     pos["entry_price"],
            "current_price":   pos["current_price"],
            "unrealised_pct":  pos["unrealised_pct"],
            "unrealised_pnl":  pos["unrealised_pnl"],
            "composite_score": pos.get("score", ""),
            "trend":           pos.get("signals", {}).get("trend", ""),
            "momentum":        pos.get("signals", {}).get("momentum", ""),
        })
    df = pd.DataFrame(rows)
    if output_path is None:
        output_path = OUTPUT_DIR / f"paper_performance_{snapshot['timestamp'][:10]}.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    log.info("CSV exported -> %s", output_path)
    return output_path


def export_html(snapshot: dict, output_path: Path = None) -> Path:
    """
    Generate a self-contained HTML dashboard for the current snapshot.
    Shows portfolio metrics, position table, and a simple return chart.
    """
    prior_log = _load_json(PAPER_LOG_FILE, [])
    dates  = [s["timestamp"][:10] for s in prior_log if s.get("total_portfolio_value")]
    values = [s["total_portfolio_value"] for s in prior_log if s.get("total_portfolio_value")]
    bench  = [s.get("benchmark_return_pct") for s in prior_log if s.get("total_portfolio_value")]

    alpha_pct = snapshot.get("alpha_pct", 0) or 0
    alpha_color = "#27ae60" if alpha_pct >= 0 else "#e74c3c"

    rows_html = ""
    for pos in snapshot.get("positions", []):
        color = "#27ae60" if pos["unrealised_pct"] >= 0 else "#e74c3c"
        rows_html += (
            f"<tr><td>{pos['ticker']}</td>"
            f"<td>{pos['weight']*100:.1f}%</td>"
            f"<td>€{pos['entry_price']:.2f}</td>"
            f"<td>€{pos['current_price']:.2f}</td>"
            f"<td style='color:{color}'>{pos['unrealised_pct']:+.2f}%</td>"
            f"<td style='color:{color}'>€{pos['unrealised_pnl']:+.2f}</td>"
            f"<td>{pos.get('score','')}</td></tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Investment Alpha — Paper Trading Dashboard</title>
  <style>
    body {{ font-family: sans-serif; background:#f5f5f5; padding:24px; color:#222; }}
    h1 {{ color:#2c3e50; }}
    .card {{ background:#fff; border-radius:8px; padding:20px; margin:16px 0;
             box-shadow:0 1px 4px rgba(0,0,0,.1); }}
    .metric {{ display:inline-block; margin:0 24px 12px 0; }}
    .metric .val {{ font-size:2em; font-weight:bold; }}
    .metric .lbl {{ font-size:.85em; color:#888; }}
    table {{ border-collapse:collapse; width:100%; }}
    th, td {{ text-align:left; padding:8px 12px; border-bottom:1px solid #eee; }}
    th {{ background:#f0f0f0; font-weight:600; }}
    .alpha {{ font-size:1.4em; font-weight:bold; color:{alpha_color}; }}
  </style>
</head>
<body>
  <h1>Investment Alpha — Paper Trading Dashboard</h1>
  <p>As of <strong>{snapshot['timestamp'][:10]}</strong> &nbsp;|&nbsp;
     Regime: <strong>{snapshot.get('regime','?').upper()}</strong> &nbsp;|&nbsp;
     Day {snapshot['paper_trading'].get('days_elapsed','?')} of {VALIDATION_MONTHS*30}</p>

  <div class="card">
    <div class="metric">
      <div class="val">€{snapshot['total_portfolio_value']:,.2f}</div>
      <div class="lbl">Portfolio Value</div>
    </div>
    <div class="metric">
      <div class="val" style="color:{'#27ae60' if snapshot['total_return_pct']>=0 else '#e74c3c'}">
        {snapshot['total_return_pct']:+.2f}%</div>
      <div class="lbl">Total Return</div>
    </div>
    <div class="metric">
      <div class="val alpha">{alpha_pct:+.2f}%</div>
      <div class="lbl">Alpha vs {BENCHMARK_TICKER}</div>
    </div>
    <div class="metric">
      <div class="val">{snapshot.get('sharpe_ratio', float('nan')):.2f}</div>
      <div class="lbl">Sharpe Ratio</div>
    </div>
    <div class="metric">
      <div class="val">{snapshot.get('max_drawdown_pct', 0):.2f}%</div>
      <div class="lbl">Max Drawdown</div>
    </div>
    <div class="metric">
      <div class="val">{snapshot.get('win_rate_pct', 0):.0f}%</div>
      <div class="lbl">Win Rate</div>
    </div>
  </div>

  <div class="card">
    <h2>Positions</h2>
    <table>
      <thead><tr>
        <th>Ticker</th><th>Weight</th><th>Entry</th><th>Current</th>
        <th>Return</th><th>P&L</th><th>Score</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</body>
</html>"""

    if output_path is None:
        output_path = OUTPUT_DIR / f"paper_dashboard_{snapshot['timestamp'][:10]}.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    log.info("HTML dashboard -> %s", output_path)
    return output_path


# ── Main ───────────────────────────────────────────────────────────────────

def run(export_csv_flag=False, export_html_flag=False) -> dict:
    """
    Main entry: take snapshot, print report, optionally export files.
    Returns the snapshot dict.
    """
    log.info("Performance tracker: taking snapshot...")
    snapshot = take_snapshot()
    if snapshot.get("status") == "no_positions":
        log.warning("No positions found — is portfolio state populated?")
        return snapshot

    print_report(snapshot)

    if export_csv_flag:
        path = export_csv(snapshot)
        print(f"\n  CSV exported: {path}")
    if export_html_flag:
        path = export_html(snapshot)
        print(f"  HTML dashboard: {path}")

    return snapshot


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Investment Alpha — Paper Trading Performance Tracker")
    parser.add_argument("--csv",  action="store_true", help="Export position snapshot as CSV")
    parser.add_argument("--html", action="store_true", help="Export HTML dashboard")
    args = parser.parse_args()

    run(export_csv_flag=args.csv, export_html_flag=args.html)
