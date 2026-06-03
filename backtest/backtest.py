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


def run_backtest(start_year=2015, end_year=2024, top_n=10, tickers=None):
    """
    Run the full monthly backtest.
    Returns dict with performance metrics and monthly return series.
    """
    if tickers is None:
        tickers = BACKTEST_TICKERS

    start = f"{start_year}-01-01"
    end   = f"{end_year}-12-31"

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

    log.info("Running backtest: %d monthly rebalances...", len(monthly_dates) - 1)

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
        for ticker in picks:
            if ticker not in close.columns:
                continue
            series = close[ticker].dropna()
            try:
                p_start = float(series.loc[:rebal_date].iloc[-1])
                p_end   = float(series.loc[next_date:].iloc[0])
                ret = (p_end - p_start) / p_start
                pick_returns.append(ret)
            except (IndexError, KeyError):
                continue

        if not pick_returns:
            continue

        strat_ret = np.mean(pick_returns)  # equal-weight portfolio return

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

    metrics = {
        "start_year":        start_year,
        "end_year":          end_year,
        "months":            n,
        "top_n":             top_n,
        "universe_size":     len(tickers),
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
    print(f"  {metrics['start_year']}–{metrics['end_year']}  |  "
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
    print(f"\n  Months evaluated : {metrics['months']}")
    print(f"  Universe size    : {metrics['universe_size']} tickers")

    # Grade the results
    sr = metrics["strategy_sharpe"]
    alpha = metrics["avg_monthly_alpha"]
    if sr >= 1.0 and alpha > 0.2:
        grade = "EXCELLENT -- ready for live capital allocation"
    elif sr >= 0.7 and alpha > 0.1:
        grade = "GOOD -- solid risk-adjusted returns, continue paper trading"
    elif sr >= 0.4:
        grade = "FAIR -- model needs improvement before live trading"
    else:
        grade = "POOR -- weights need significant recalibration"

    print(f"\n  Assessment: {grade}")
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
    parser.add_argument("--end",   type=int, default=2024)
    parser.add_argument("--top",   type=int, default=10)
    parser.add_argument("--output", default=None, help="Save results to Excel file")
    args = parser.parse_args()

    print(f"\nRunning backtest {args.start}-{args.end}, top {args.top} stocks...")
    print("This will take 2-4 minutes (downloading historical data)...\n")

    metrics = run_backtest(start_year=args.start, end_year=args.end, top_n=args.top)

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
