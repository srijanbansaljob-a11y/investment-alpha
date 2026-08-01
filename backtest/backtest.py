"""
backtest/backtest.py - Phase 3F: Vectorised Monthly Backtest 2015-2024

Simulates the Investment Alpha pipeline month-by-month using historical data.
For each month: score all tickers on data available at that time, select top N,
hold for one month, record return. Compare vs SPY buy-and-hold benchmark.

Metrics reported:
  - Cumulative return (strategy vs SPY)
  - Annualised return and Sharpe ratio
  - Maximum drawdown
  - Hit rate (% of monthly selections that beat benchmark)
  - Factor attribution: which factor contributed most to alpha

Usage:
  python backtest/backtest.py                        # 2015-2024, top 10
  python backtest/backtest.py --start 2018 --top 10  # custom range
  python backtest/backtest.py --output results.xlsx   # save to Excel

Note: Uses simplified scoring (price-based factors only) since fundamental
data is point-in-time and not reliably available historically via yfinance.
Momentum and trend factors are fully historical. Quality uses latest
fundamentals as a proxy (conservative -- understates true historical edge).
"""

import argparse
import logging
import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)

# ── Universe for backtest (manageable subset for speed) ──────────────────
# Using SP500 core + custom list -- ~100 tickers for backtest speed
BACKTEST_TICKERS = list(dict.fromkeys([
    # Mega-cap tech
    "AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","AVGO","ORCL","CRM",
    "ADBE","INTC","QCOM","TXN","AMD","MU","AMAT","KLAC","LRCX","ADI",
    # Financials
    "JPM","BAC","GS","MS","WFC","BLK","V","MA","AXP","COF",
    "USB","TFC","PNC","SCHW","ICE","CME","SPGI","MCO","FDS","MSCI",
    # Healthcare
    "UNH","JNJ","LLY","PFE","MRK","ABBV","TMO","DHR","ABT","BMY",
    "AMGN","GILD","BIIB","REGN","VRTX","ISRG","EW","ZBH","BAX","BDX",
    # Energy
    "XOM","CVX","COP","EOG","SLB","MPC","PSX","VLO","OXY","HAL",
    # Consumer
    "COST","WMT","HD","MCD","SBUX","NKE","TGT","LOW","TJX","AMZN",
    "PG","KO","PEP","PM","MO","CL","EL","CHD","CLX","KMB",
    # Industrials
    "BA","LMT","RTX","HON","GE","CAT","DE","MMM","UPS","FDX",
    "CSX","NSC","UNP","WM","RSG","CARR","OTIS","ETN","EMR","ROK",
    # Utilities / Real Estate
    "NEE","DUK","SO","D","AEP","AMT","PLD","EQIX","CCI","SPG",
]))


def download_history(tickers, start, end):
    """Download OHLCV for all tickers in one batch. Returns Close DataFrame."""
    log.info("Downloading price history: %d tickers, %s to %s", len(tickers), start, end)
    all_tickers = list(set(tickers + ["SPY", "^VIX"]))
    try:
        data = yf.download(all_tickers, start=start, end=end,
                           auto_adjust=True, progress=False, threads=True)
    except Exception as e:
        log.error("Download failed: %s", e)
        return pd.DataFrame(), pd.DataFrame()

    if isinstance(data.columns, pd.MultiIndex):
        close  = data["Close"]
        volume = data.get("Volume", pd.DataFrame())
    else:
        close  = data[["Close"]] if "Close" in data.columns else data
        volume = pd.DataFrame()

    log.info("Downloaded %d tickers, %d trading days", close.shape[1], len(close))
    return close, volume


def score_month(close_slice, volume_slice, rebal_date, top_n=10):
    """
    Score all tickers on data available up to rebal_date.
    Uses momentum + trend factors (price-based, fully historical).
    Returns sorted DataFrame of (ticker, score) pairs.
    """
    records = []
    spy_col = "SPY" if "SPY" in close_slice.columns else None
    spy_12m = None
    if spy_col:
        spy_series = close_slice[spy_col].dropna()
        if len(spy_series) >= 273:
            spy_12m = (spy_series.iloc[-21] / spy_series.iloc[-252]) - 1

    for ticker in close_slice.columns:
        if ticker in ("SPY", "^VIX"):
            continue
        series = close_slice[ticker].dropna()
        if len(series) < 252:
            continue

        price = float(series.iloc[-1])

        # Momentum (skip-month: t-21 to t-252)
        m_3m  = (series.iloc[-21] / series.iloc[-63])  - 1 if len(series) >= 63  else np.nan
        m_6m  = (series.iloc[-21] / series.iloc[-126]) - 1 if len(series) >= 126 else np.nan
        m_12m = (series.iloc[-21] / series.iloc[-252]) - 1 if len(series) >= 252 else np.nan
        rel_str = (m_12m - spy_12m) if (not np.isnan(m_12m) and spy_12m is not None) else 0.0

        # Trend
        sma50  = series.iloc[-50:].mean()
        sma200 = series.iloc[-200:].mean()
        above_200 = price > sma200
        pct_vs_200 = (price - sma200) / sma200 if sma200 > 0 else 0

        # Volatility (60-day annualised)
        log_rets = np.log(series / series.shift(1)).dropna()
        vol_60d = float(log_rets.iloc[-60:].std() * np.sqrt(252)) if len(log_rets) >= 60 else np.nan

        if not above_200 or np.isnan(vol_60d):
            continue  # basic filter: must be above 200-day MA

        records.append({
            "ticker":    ticker,
            "m_3m":      m_3m  if not np.isnan(m_3m)  else 0.0,
            "m_6m":      m_6m  if not np.isnan(m_6m)  else 0.0,
            "m_12m":     m_12m if not np.isnan(m_12m) else 0.0,
            "rel_str":   rel_str,
            "pct_vs200": pct_vs_200,
            "vol_60d":   vol_60d,
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # Percentile rank each factor
    def pr(col, asc=True):
        return df[col].rank(pct=True, ascending=asc).fillna(0.5)

    df["s_momentum"] = (pr("m_3m") + pr("m_6m") + pr("m_12m") + pr("rel_str")) / 4
    df["s_trend"]    = pr("pct_vs200")
    df["s_vol"]      = pr("vol_60d", asc=False)   # lower vol = higher score

    # Try to load learned weights; fall back to defaults
    import json
    wfile = Path(getattr(config, "LEARNED_WEIGHTS_FILE", "data/learned_weights.json"))
    if wfile.exists():
        try:
            w = json.loads(wfile.read_text())
        except Exception:
            w = config.FACTOR_WEIGHTS
    else:
        w = config.FACTOR_WEIGHTS

    df["score"] = (
        w.get("momentum",   0.30) * df["s_momentum"]
      + w.get("trend",      0.25) * df["s_trend"]
      - w.get("volatility", 0.10) * df["s_vol"]
    )

    # Volatility filter: exclude top 20% most volatile
    vol_cutoff = df["vol_60d"].quantile(0.80)
    df = df[df["vol_60d"] <= vol_cutoff]

    return df.sort_values("score", ascending=False).head(top_n)


# Set from CLI so the stop band can be swept without editing config.py.
# Measured 2026-08-01: stops contribute +2.7%/yr CAGR and halve max drawdown
# (18.4% / -11.0% with, vs 15.7% / -24.5% without), but stop out ~52% of
# positions. This makes the band the highest-leverage parameter in the system.
_STOP_OVERRIDES: dict = {}


def _stop_price_for(series: pd.Series, entry: float) -> float | None:
    """
    Stop level for a backtest entry, mirroring broker/stop_loss.compute_stop_price:
    2.5 x ATR(14), clamped to [STOP_PCT_FLOOR, STOP_PCT_CAP].

    APPROXIMATION, stated plainly: only adjusted CLOSE is downloaded here, so
    ATR is estimated from close-to-close moves. True range includes the
    intraday high-low span, so this proxy UNDERSTATES ATR by roughly a third,
    producing slightly tighter stops than live. Fills are also assumed at the
    stop price — a real gap-down fills worse. Both effects mean the modelled
    stop is mildly optimistic; treat stopped returns as a best case.
    """
    if len(series) < 15 or entry <= 0:
        return None
    tr = series.diff().abs().iloc[-14:]
    atr = float(tr.mean())
    if not np.isfinite(atr) or atr <= 0:
        return None
    mult  = _STOP_OVERRIDES.get("mult")  or getattr(config, "ATR_STOP_MULTIPLIER", {}).get("bull", 2.5)
    floor = _STOP_OVERRIDES.get("floor") or getattr(config, "STOP_PCT_FLOOR", 0.03)
    cap   = _STOP_OVERRIDES.get("cap")   or getattr(config, "STOP_PCT_CAP",   0.12)
    raw_pct = (mult * atr) / entry
    return entry * (1 - min(max(raw_pct, floor), cap))


def _position_return(series: pd.Series, rebal_date, next_date,
                     use_stops: bool) -> tuple[float, bool]:
    """
    Return for one holding over one month. If use_stops, walk the daily closes
    and exit at the stop the first day it is breached (returns stopped=True).

    Without this, the backtest holds every position to month-end — a materially
    different return distribution from the live system, whose stops demonstrably
    fire (NUE and EIX both stopped out on 2026-07-27/31).
    """
    hist = series.loc[:rebal_date]
    if hist.empty:
        raise IndexError("no history")
    entry = float(hist.iloc[-1])
    fwd = series.loc[rebal_date:next_date]
    if len(fwd) < 2:
        raise IndexError("no forward data")

    if use_stops:
        stop = _stop_price_for(hist, entry)
        if stop:
            breached = fwd.iloc[1:][fwd.iloc[1:] <= stop]
            if not breached.empty:
                return (stop - entry) / entry, True

    exit_px = float(fwd.iloc[-1])
    return (exit_px - entry) / entry, False


def run_backtest(start_year=2015, end_year=None, top_n=10, tickers=None,
                 cost_bps=None, use_stops=True, end_date=None):
    """
    Run the full monthly backtest.

    Args:
        end_year / end_date: end_date wins. Default is TODAY — the previous
            default stopped at 2024-12-31 and silently discarded the most
            recent ~19 months, which is both the largest genuinely
            out-of-sample block available and the regime about to be traded.
        cost_bps: one-way trading cost in basis points applied to turnover
            (spread + slippage). Alpaca charges no commission, but crossing
            the spread is a real cost a zero-cost backtest hides.
        use_stops: apply the live 3-12% ATR stop band intramonth.

    Returns dict with performance metrics and monthly return series.
    """
    if tickers is None:
        tickers = BACKTEST_TICKERS
    if cost_bps is None:
        cost_bps = getattr(config, "BACKTEST_COST_BPS", 10.0)

    start = f"{start_year}-01-01"
    if end_date:
        end = str(end_date)
    elif end_year:
        end = f"{end_year}-12-31"
    else:
        end = datetime.now().strftime("%Y-%m-%d")

    # Need extra history for SMA200 and 12M momentum before backtest start
    data_start = f"{start_year - 2}-01-01"
    close, volume = download_history(tickers, data_start, end)

    if close.empty:
        log.error("No data downloaded -- backtest aborted")
        return {}

    spy = close["SPY"].dropna() if "SPY" in close.columns else None

    # Generate monthly rebalance dates (first trading day of each month)
    all_dates = close.loc[start:end].index
    monthly_dates = pd.DatetimeIndex(
        pd.Series(all_dates).groupby(
            pd.Series(all_dates).dt.to_period("M")
        ).first().values
    )

    strategy_returns = []
    spy_returns      = []
    monthly_picks    = []
    prev_picks       = set()
    total_cost_drag  = 0.0
    total_stopped    = 0

    log.info("Running backtest: %d monthly rebalances | costs %.0fbps/side | stops %s",
             len(monthly_dates) - 1, cost_bps, "ON" if use_stops else "OFF")

    for i in range(len(monthly_dates) - 1):
        rebal_date = monthly_dates[i]
        next_date  = monthly_dates[i + 1]

        # Data available up to rebal_date (no look-ahead)
        close_slice = close.loc[:rebal_date]

        # Score and select
        selected = score_month(close_slice, None, rebal_date, top_n)
        if selected.empty:
            continue

        picks = selected["ticker"].tolist()

        # Compute next-month return for each pick
        pick_returns = []
        stopped_this_month = 0
        for ticker in picks:
            if ticker not in close.columns:
                continue
            series = close[ticker].dropna()
            try:
                ret, stopped = _position_return(series, rebal_date, next_date, use_stops)
                pick_returns.append(ret)
                stopped_this_month += int(stopped)
            except (IndexError, KeyError):
                continue

        if not pick_returns:
            continue

        strat_ret = np.mean(pick_returns)  # equal-weight portfolio return

        # ── Transaction costs ────────────────────────────────────────────
        # Turnover = fraction of the book replaced this month. Each replaced
        # name pays the spread twice (exit the old, enter the new). Stopped
        # positions pay an extra exit on top.
        held = set(picks)
        turnover = len(held - prev_picks) / max(len(held), 1) if prev_picks else 1.0
        cost = turnover * 2 * (cost_bps / 10_000.0)
        cost += (stopped_this_month / max(len(picks), 1)) * (cost_bps / 10_000.0)
        strat_ret -= cost
        total_cost_drag += cost
        total_stopped   += stopped_this_month
        prev_picks = held

        # SPY return same period
        spy_ret = 0.0
        if spy is not None:
            try:
                s_start = float(spy.loc[:rebal_date].iloc[-1])
                s_end   = float(spy.loc[next_date:].iloc[0])
                spy_ret = (s_end - s_start) / s_start
            except (IndexError, KeyError):
                pass

        strategy_returns.append(strat_ret)
        spy_returns.append(spy_ret)
        monthly_picks.append({
            "date":          rebal_date.strftime("%Y-%m"),
            "picks":         picks,
            "strat_return":  round(strat_ret, 5),
            "spy_return":    round(spy_ret, 5),
            "excess_return": round(strat_ret - spy_ret, 5),
        })

        if (i + 1) % 12 == 0:
            log.info("  %s: cumulative strategy %.1f%% vs SPY %.1f%%",
                     rebal_date.strftime("%Y-%m"),
                     (np.prod([1+r for r in strategy_returns]) - 1) * 100,
                     (np.prod([1+r for r in spy_returns]) - 1) * 100)

    if not strategy_returns:
        log.error("No monthly returns computed -- check data")
        return {}

    # ── Performance Metrics ──────────────────────────────────────────────
    s_arr = np.array(strategy_returns)
    b_arr = np.array(spy_returns)
    n     = len(s_arr)
    years = n / 12

    def cumulative_return(rets):
        return float(np.prod(1 + np.array(rets)) - 1)

    def annualised_return(rets, yrs):
        return float((1 + cumulative_return(rets)) ** (1 / max(yrs, 0.1)) - 1)

    def sharpe(rets, rf=0.045):
        # Monthly risk-free ≈ 4.5% annual
        rf_m = (1 + rf) ** (1/12) - 1
        excess = np.array(rets) - rf_m
        return float(excess.mean() / max(excess.std(), 1e-9) * np.sqrt(12))

    def max_drawdown(rets):
        cumulative = np.cumprod(1 + np.array(rets))
        peak = np.maximum.accumulate(cumulative)
        dd   = (cumulative - peak) / peak
        return float(dd.min())

    def hit_rate(strat, bench):
        return float(np.mean(np.array(strat) > np.array(bench)))

    # ── In-sample / out-of-sample split ──────────────────────────────────
    # A single full-period number is where overfitting hides. Split at 60% of
    # the timeline: the model was designed with the earlier period visible, so
    # the later block is the closer thing to an honest forward test.
    split = int(n * 0.6)
    oos = {}
    if split >= 12 and (n - split) >= 12:
        s_is,  s_oos = s_arr[:split], s_arr[split:]
        b_is,  b_oos = b_arr[:split], b_arr[split:]
        oos = {
            "split_month_index":       split,
            "is_months":               split,
            "oos_months":              n - split,
            "is_annualised":           round(annualised_return(s_is, split / 12) * 100, 2),
            "oos_annualised":          round(annualised_return(s_oos, (n - split) / 12) * 100, 2),
            "is_spy_annualised":       round(annualised_return(b_is, split / 12) * 100, 2),
            "oos_spy_annualised":      round(annualised_return(b_oos, (n - split) / 12) * 100, 2),
            "is_sharpe":               round(sharpe(s_is), 3),
            "oos_sharpe":              round(sharpe(s_oos), 3),
            "is_avg_alpha":            round(float((s_is - b_is).mean()) * 100, 4),
            "oos_avg_alpha":           round(float((s_oos - b_oos).mean()) * 100, 4),
        }

    metrics = {
        "start_year":        start_year,
        "end_year":          end_year,
        "period":            f"{monthly_picks[0]['date']} to {monthly_picks[-1]['date']}",
        "months":            n,
        "top_n":             top_n,
        "universe_size":     len(tickers),
        "cost_bps_per_side": cost_bps,
        "stops_applied":     use_stops,
        "total_cost_drag_pct": round(total_cost_drag * 100, 2),
        "positions_stopped":   total_stopped,
        "oos": oos,
        # Strategy
        "strategy_cumulative_return":  round(cumulative_return(s_arr) * 100, 2),
        "strategy_annualised_return":  round(annualised_return(s_arr, years) * 100, 2),
        "strategy_sharpe":             round(sharpe(s_arr), 3),
        "strategy_max_drawdown":       round(max_drawdown(s_arr) * 100, 2),
        "strategy_monthly_vol":        round(float(s_arr.std() * np.sqrt(12)) * 100, 2),
        # Benchmark
        "spy_cumulative_return":       round(cumulative_return(b_arr) * 100, 2),
        "spy_annualised_return":       round(annualised_return(b_arr, years) * 100, 2),
        "spy_sharpe":                  round(sharpe(b_arr), 3),
        "spy_max_drawdown":            round(max_drawdown(b_arr) * 100, 2),
        # Alpha
        "hit_rate_vs_spy":             round(hit_rate(s_arr, b_arr) * 100, 2),
        "avg_monthly_alpha":           round(float((s_arr - b_arr).mean()) * 100, 4),
        "information_ratio":           round(
            float((s_arr - b_arr).mean()) / max(float((s_arr - b_arr).std()), 1e-9) * np.sqrt(12), 3
        ),
        "monthly_picks":               monthly_picks,
    }

    return metrics


def print_results(metrics):
    """Print formatted backtest report."""
    print("\n" + "=" * 65)
    print("  INVESTMENT ALPHA -- BACKTEST RESULTS")
    print(f"  {metrics.get('period', metrics['start_year'])}  |  "
          f"Monthly rebalance  |  Top {metrics['top_n']} stocks")
    print("=" * 65)
    print(f"\n  {'Metric':<35} {'Strategy':>10} {'SPY':>10}")
    print("  " + "-" * 55)
    rows = [
        ("Cumulative Return",          f"{metrics['strategy_cumulative_return']:>+.1f}%",  f"{metrics['spy_cumulative_return']:>+.1f}%"),
        ("Annualised Return (CAGR)",    f"{metrics['strategy_annualised_return']:>+.1f}%",  f"{metrics['spy_annualised_return']:>+.1f}%"),
        ("Sharpe Ratio",               f"{metrics['strategy_sharpe']:>10.3f}",  f"{metrics['spy_sharpe']:>10.3f}"),
        ("Max Drawdown",               f"{metrics['strategy_max_drawdown']:>+.1f}%",  f"{metrics['spy_max_drawdown']:>+.1f}%"),
        ("Annualised Volatility",       f"{metrics['strategy_monthly_vol']:>.1f}%",   "  n/a"),
    ]
    for label, strat, spy in rows:
        print(f"  {label:<35} {strat:>10} {spy:>10}")
    print("  " + "-" * 55)
    print(f"  {'Hit Rate vs SPY':<35} {metrics['hit_rate_vs_spy']:>9.1f}%")
    print(f"  {'Avg Monthly Alpha':<35} {metrics['avg_monthly_alpha']:>+9.2f}%")
    print(f"  {'Information Ratio':<35} {metrics['information_ratio']:>10.3f}")
    print(f"\n  Period           : {metrics.get('period', 'n/a')}  ({metrics['months']} months)")
    print(f"  Universe size    : {metrics['universe_size']} tickers")
    print(f"  Trading costs    : {metrics['cost_bps_per_side']:.0f}bps/side "
          f"(total drag {metrics['total_cost_drag_pct']:+.1f}%)")
    print(f"  Stops applied    : {metrics['stops_applied']} "
          f"({metrics['positions_stopped']} positions stopped out)")

    # ── Out-of-sample: the number that actually matters ──────────────────
    oos = metrics.get("oos") or {}
    if oos:
        print("\n  " + "-" * 55)
        print("  IN-SAMPLE vs OUT-OF-SAMPLE  (overfitting check)")
        print(f"  {'':<22}{'in-sample':>14}{'out-of-sample':>16}")
        print(f"  {'Months':<22}{oos['is_months']:>14}{oos['oos_months']:>16}")
        print(f"  {'CAGR':<22}{oos['is_annualised']:>13.1f}%{oos['oos_annualised']:>15.1f}%")
        print(f"  {'SPY CAGR':<22}{oos['is_spy_annualised']:>13.1f}%{oos['oos_spy_annualised']:>15.1f}%")
        print(f"  {'Sharpe':<22}{oos['is_sharpe']:>14.3f}{oos['oos_sharpe']:>16.3f}")
        print(f"  {'Avg monthly alpha':<22}{oos['is_avg_alpha']:>13.2f}%{oos['oos_avg_alpha']:>15.2f}%")
        decay = oos["is_avg_alpha"] - oos["oos_avg_alpha"]
        if oos["oos_avg_alpha"] <= 0:
            print("\n  ⚠  Alpha DISAPPEARS out of sample — the in-sample edge is "
                  "not evidence.")
        elif decay > 0.15:
            print(f"\n  ⚠  Alpha decays {decay:.2f}%/month out of sample — treat the "
                  "full-period figure as optimistic.")
        else:
            print("\n  Alpha persists out of sample — the more credible result.")

    # Grade on the OUT-OF-SAMPLE numbers where available; the full-period
    # figure includes the years the model was shaped around.
    sr    = oos.get("oos_sharpe", metrics["strategy_sharpe"])
    alpha = oos.get("oos_avg_alpha", metrics["avg_monthly_alpha"])
    basis = "out-of-sample" if oos else "full-period"
    if sr >= 1.0 and alpha > 0.2:
        grade = "STRONG"
    elif sr >= 0.7 and alpha > 0.1:
        grade = "MODERATE"
    elif sr >= 0.4:
        grade = "WEAK"
    else:
        grade = "NO DEMONSTRATED EDGE"

    print(f"\n  Assessment ({basis}): {grade}")
    print("\n  READ BEFORE ACTING ON THESE NUMBERS")
    print("  1. SURVIVORSHIP BIAS — the universe is today's constituents run")
    print("     backwards. Firms delisted, acquired or bankrupted in the period")
    print("     are absent, which removes losers only. Published estimates put")
    print("     this at 1-4%/yr; compare that against the alpha above before")
    print("     concluding anything.")
    print("  2. PRICE FACTORS ONLY — momentum, trend and volatility. Quality,")
    print("     valuation, sentiment and PEAD are NOT tested here (they need")
    print("     point-in-time fundamentals, which yfinance cannot supply), so")
    print("     roughly 35-40% of the live model's weight is unvalidated.")
    print("  3. Monthly rebalance; the live system runs weekly.")
    print("  4. Stops use a close-based ATR proxy and assume fills AT the stop.")
    print("=" * 65)


def save_results_excel(metrics, output_path):
    """Save backtest results to Excel."""
    try:
        import openpyxl
    except ImportError:
        log.warning("openpyxl not installed -- skipping Excel export")
        return

    summary = {k: v for k, v in metrics.items() if k != "monthly_picks"}
    picks_df = pd.DataFrame(metrics.get("monthly_picks", []))

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        pd.DataFrame([summary]).T.rename(columns={0: "Value"}).to_excel(
            writer, sheet_name="Summary")
        if not picks_df.empty:
            picks_df.to_excel(writer, sheet_name="Monthly Returns", index=False)

    log.info("Backtest results saved -> %s", output_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Investment Alpha Backtest")
    parser.add_argument("--start", type=int, default=2015)
    parser.add_argument("--end",   type=int, default=None,
                        help="End YEAR (default: run to the latest available data)")
    parser.add_argument("--end-date", default=None,
                        help="End DATE YYYY-MM-DD (overrides --end)")
    parser.add_argument("--top",   type=int, default=10)
    parser.add_argument("--universe", choices=["live", "legacy"], default="live",
                        help="live = config.ALL_TICKERS (what the pipeline actually "
                             "trades); legacy = the 119 mega-cap list")
    parser.add_argument("--cost-bps", type=float, default=None,
                        help="One-way cost in bps (default 10). Use 0 to disable.")
    parser.add_argument("--no-stops", action="store_true",
                        help="Hold to month-end instead of applying ATR stops")
    parser.add_argument("--stop-floor", type=float, default=None,
                        help="Override STOP_PCT_FLOOR, e.g. 0.05 for a 5%% floor")
    parser.add_argument("--stop-cap", type=float, default=None,
                        help="Override STOP_PCT_CAP, e.g. 0.15 for a 15%% cap")
    parser.add_argument("--stop-mult", type=float, default=None,
                        help="Override the ATR multiplier (default 2.5)")
    parser.add_argument("--output", default=None, help="Save results to Excel file")
    args = parser.parse_args()

    _STOP_OVERRIDES.update({k: v for k, v in {
        "floor": args.stop_floor, "cap": args.stop_cap, "mult": args.stop_mult,
    }.items() if v is not None})
    if _STOP_OVERRIDES:
        print(f"Stop band overrides: {_STOP_OVERRIDES}")

    universe = (list(config.ALL_TICKERS) if args.universe == "live"
                else BACKTEST_TICKERS)
    end_label = args.end_date or (args.end or "latest available")

    print(f"\nRunning backtest {args.start}-{end_label}, top {args.top} stocks")
    print(f"Universe: {args.universe} ({len(universe)} tickers) | "
          f"stops {'OFF' if args.no_stops else 'ON'}")
    print("Downloading historical data — this can take several minutes...\n")

    metrics = run_backtest(
        start_year=args.start, end_year=args.end, end_date=args.end_date,
        top_n=args.top, tickers=universe,
        cost_bps=args.cost_bps, use_stops=not args.no_stops,
    )

    if metrics:
        print_results(metrics)
        if args.output:
            save_results_excel(metrics, args.output)
        else:
            # Default: save to outputs folder
            out = Path(getattr(config, "OUTPUT_DIR", "outputs")) / "backtest_results.xlsx"
            out.parent.mkdir(parents=True, exist_ok=True)
            save_results_excel(metrics, str(out))
            print(f"\n  Results saved -> {out}")
    else:
        print("Backtest failed -- check logs")
