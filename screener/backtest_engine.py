#!/usr/bin/env python3
"""
screener/backtest_engine.py — Walk-Forward Momentum Backtest

PURPOSE
-------
Validate the 6-signal price momentum scorer against historical data.
NOT a full system backtest — fundamental signals (analyst, sentiment,
valuation) require historical data we don't have. This tests whether
the price-based momentum component genuinely predicts 1-month returns.

ANTI-OVERFITTING MEASURES
--------------------------
1. Fixed weights — taken directly from live config, NEVER optimised on
   backtest data. The weights were set before running this script.
2. Walk-forward validation — clear in-sample / out-of-sample split.
   OOS period is never touched during development; results reported
   separately so you can see the IS→OOS degradation explicitly.
3. Transaction costs — 0.1% per trade (0.2% round trip) baked in.
4. Information Coefficient — rank correlation of score vs actual
   1-month return. This is independent of portfolio construction and
   directly measures whether the signal predicts returns at all.
5. Consistency test — rolling 6-month win rates, not just total return.
   A lucky 3-month run won't hide a bad signal.
6. Survivorship bias warning — universe is today's DEFAULT_TICKERS,
   which excludes historical failures. Results are optimistic.
7. Regime simplification — can only reconstruct SPY vs 200MA historically,
   not the full 6-component regime. Labelled explicitly in output.
8. IS vs OOS degradation table — the primary overfitting diagnostic.

USAGE
-----
  python screener/backtest_engine.py
  python screener/backtest_engine.py --years 3 --oos-months 12
  python screener/backtest_engine.py --top-n-bull 7 --top-n-neutral 5 --top-n-bear 3
  python screener/backtest_engine.py --costs 0.002 --save-json screener/outputs/backtest.json
"""

import os
import sys
import json
import time
import math
import argparse
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

warnings.filterwarnings("ignore")

# ── Paths ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# ── Constants ────────────────────────────────────────────────────────────────

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "").strip()
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "").strip()
ALPACA_DATA   = "https://data.alpaca.markets"
BENCHMARK     = "SPY"

# ── Signal weights (FIXED — identical to live screener, never tuned here) ───
# Changing these and re-running is data snooping. Don't do it.
MOMENTUM_SIGNAL_WEIGHTS = {
    "ma_structure":    0.15,   # MA trend alignment (price vs MA50 vs MA200)
    "ma50_distance":   0.08,   # distance from MA50 (over-extension penalty)
    "week52_position": 0.10,   # position in 52-week high/low range
    "week52_return":   0.10,   # 52-week price return
    "daily_move":      0.12,   # 5-day momentum
    "volume_ratio":    0.10,   # volume vs 20-day average
}
MAX_SCORE = sum(MOMENTUM_SIGNAL_WEIGHTS.values())

# ── Universe (same as live screener — excludes sector ETFs, crypto proxies) ─
DEFAULT_TICKERS = [
    # ── Mega-Cap Tech ──────────────────────────────────────────────────────
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
    "AVGO", "ORCL", "AMD", "CRM", "ADBE", "QCOM", "TXN", "IBM",
    "CSCO", "NOW", "AMAT", "MU", "INTC", "ARM",
    # ── Cybersecurity / Cloud ──────────────────────────────────────────────
    "PANW", "CRWD", "NET", "DDOG", "SNOW", "ZS",
    # ── Financials ────────────────────────────────────────────────────────
    "JPM", "GS", "BAC", "MS", "V", "MA", "BLK", "C", "WFC",
    "AXP", "COF", "SCHW", "PYPL",
    # ── Healthcare / Biotech ──────────────────────────────────────────────
    "UNH", "LLY", "PFE", "MRNA", "JNJ", "ABBV", "MRK", "ABT",
    "TMO", "AMGN", "GILD", "ISRG", "REGN", "VRTX", "BMY",
    # ── Consumer Discretionary & Staples ──────────────────────────────────
    "COST", "WMT", "HD", "MCD", "NKE", "SBUX", "TGT", "LOW",
    "PG", "KO", "PEP", "NFLX", "DIS", "CMCSA",
    # ── Energy ────────────────────────────────────────────────────────────
    "XOM", "CVX", "OXY", "COP", "SLB", "MPC",
    # ── Industrials / Defense ─────────────────────────────────────────────
    "CAT", "BA", "RTX", "LMT", "HON", "GE", "DE", "UPS", "FDX",
    # ── High-Momentum Growth ───────────────────────────────────────────────
    "SMCI", "MSTR", "PLTR", "HOOD", "SOFI",
    "COIN", "UBER", "DASH", "ABNB", "SPOT", "RBLX", "RDDT",
    "MELI", "NU", "AFRM", "IONQ",
]


# ── Alpaca bar fetcher ───────────────────────────────────────────────────────

def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Accept": "application/json",
    }


def fetch_bars(tickers: list, start: str, end: str, batch_size: int = 30) -> dict:
    """
    Fetch daily split/dividend-adjusted OHLCV bars from Alpaca.
    Returns {ticker: [(date_str, o, h, l, close, volume), ...]} sorted by date.
    """
    import urllib.request
    import urllib.parse

    all_bars: dict = {}
    headers = _alpaca_headers()

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        params = {
            "symbols":    ",".join(batch),
            "timeframe":  "1Day",
            "start":      start,
            "end":        end,
            "adjustment": "all",
            "feed":       "iex",
            "limit":      10000,
        }
        base_url = f"{ALPACA_DATA}/v2/stocks/bars?{urllib.parse.urlencode(params)}"
        page_token = None

        while True:
            url = base_url + (f"&page_token={urllib.parse.quote(page_token)}" if page_token else "")
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                for sym, bars in data.get("bars", {}).items():
                    if sym not in all_bars:
                        all_bars[sym] = []
                    for b in bars:
                        all_bars[sym].append((
                            b["t"][:10],
                            float(b["o"]), float(b["h"]),
                            float(b["l"]), float(b["c"]),
                            float(b["v"]),
                        ))
                page_token = data.get("next_page_token")
                if not page_token:
                    break
            except Exception as exc:
                print(f"  ⚠  Bar fetch error ({batch}): {exc}")
                break

        time.sleep(0.3)  # stay well within Alpaca rate limits

    # Sort each ticker's bars by date ascending
    for sym in all_bars:
        all_bars[sym].sort(key=lambda x: x[0])

    return all_bars


# ── Signal computation ───────────────────────────────────────────────────────

def compute_momentum_score(closes: list, volumes: list) -> float | None:
    """
    Replicate the live screener's 6-signal momentum score from raw price/volume.
    Uses ONLY data up to the rebalance date (no lookahead).
    Returns 0-100 score, or None if insufficient history.
    """
    if len(closes) < 220:
        return None

    w      = MOMENTUM_SIGNAL_WEIGHTS
    score  = 0.0
    window = closes[-220:]
    vol_w  = volumes[-220:]
    price  = window[-1]
    ma50   = sum(window[-50:]) / 50
    ma200  = sum(window) / 220

    # 1. MA trend structure — alignment of price, MA50, MA200
    if price > ma50 > ma200:
        score += w["ma_structure"] * 1.0
    elif price > ma200:
        score += w["ma_structure"] * 0.5
    elif price > ma50:
        score += w["ma_structure"] * 0.3
    else:
        score -= w["ma_structure"] * 0.5

    # 2. Distance from MA50 — reward trend extension, not proximity.
    # Original logic penalised >15% above MA50 (mean-reversion instinct).
    # For a momentum universe this is wrong: stocks 20%+ above MA50 are in
    # strong uptrends and tend to continue. Further above = more reward.
    pct50 = (price - ma50) / ma50 if ma50 else 0
    if pct50 > 0.20:
        score += w["ma50_distance"] * 1.0   # strong trend extension
    elif pct50 > 0.10:
        score += w["ma50_distance"] * 0.8
    elif pct50 > 0.05:
        score += w["ma50_distance"] * 0.6
    elif pct50 > 0:
        score += w["ma50_distance"] * 0.3
    else:
        score -= w["ma50_distance"] * 0.7   # below MA50 — weak

    # 3. 52-week range position (higher = stronger trend)
    hi52 = max(window)
    lo52 = min(window)
    rng  = hi52 - lo52
    pos  = (price - lo52) / rng if rng > 0 else 0.5
    score += w["week52_position"] * (pos * 2 - 1)  # maps [0,1] → [-1,+1]

    # 4. 52-week return (captures trend persistence).
    # Original max bucket was >30%. NVDA/MSTR/PLTR type stocks at +100-200%
    # were scored identically to a stock up +35%. Added >50% and >100% buckets.
    ret52 = (price - window[0]) / window[0] if window[0] else 0
    if ret52 > 1.00:
        score += w["week52_return"] * 1.0   # >100% — high-momentum names
    elif ret52 > 0.50:
        score += w["week52_return"] * 0.85
    elif ret52 > 0.25:
        score += w["week52_return"] * 0.65
    elif ret52 > 0.10:
        score += w["week52_return"] * 0.40
    elif ret52 > 0:
        score += w["week52_return"] * 0.15
    elif ret52 > -0.10:
        score -= w["week52_return"] * 0.30
    else:
        score -= w["week52_return"] * 0.80

    # 5. 5-day momentum (short-term trend confirmation)
    prev5 = window[-6] if len(window) >= 6 else window[0]
    move5 = (price - prev5) / prev5 if prev5 else 0
    if move5 > 0.03:
        score += w["daily_move"] * 1.0
    elif move5 > 0:
        score += w["daily_move"] * 0.5
    elif move5 > -0.03:
        score -= w["daily_move"] * 0.3
    else:
        score -= w["daily_move"] * 0.7

    # 6. Volume ratio vs 20-day average (confirms conviction)
    avg_vol   = sum(vol_w[-20:]) / 20 if len(vol_w) >= 20 else 1
    vol_ratio = vol_w[-1] / avg_vol if avg_vol > 0 else 1
    if vol_ratio > 1.5:
        score += w["volume_ratio"] * 1.0
    elif vol_ratio > 1.0:
        score += w["volume_ratio"] * 0.5
    else:
        score += w["volume_ratio"] * 0.1

    return round((score / MAX_SCORE) * 100, 1)


def detect_regime(spy_closes: list) -> str:
    """
    Simplified historical regime proxy: SPY price vs 50/200-day MA.

    NOTE: The live system uses a 6-component regime score (VIX, Fear&Greed,
    ADX-SPY, sector breadth, VIX term structure). Reconstructing those
    historically would require data we don't have and would risk lookahead
    bias (e.g. using today's VIX surface to classify past dates). This
    proxy is transparent and reproducible, but it will misclassify some
    periods. Expect regime_avg returns to look better than they are in
    real operation with the live regime gate.
    """
    if len(spy_closes) < 200:
        return "neutral"
    ma200 = sum(spy_closes[-200:]) / 200
    ma50  = sum(spy_closes[-50:]) / 50
    price = spy_closes[-1]
    if price > ma50 > ma200:
        return "bull"
    elif price < ma200:
        return "bear"
    return "neutral"


# ── Walk-forward engine ──────────────────────────────────────────────────────

def _month_start_indices(dates: list) -> list:
    """Indices of the first trading day of each calendar month."""
    starts, prev_ym = [], None
    for i, d in enumerate(dates):
        ym = d[:7]
        if ym != prev_ym:
            starts.append(i)
            prev_ym = ym
    return starts


def run_backtest(
    all_bars:   dict,
    tickers:    list,
    top_n:      dict,       # {"bull": 7, "neutral": 5, "bear": 3}
    cost_pct:   float,      # one-way transaction cost fraction
    oos_months: int,        # months to hold out as OOS
    warmup_days: int = 220,
) -> dict:
    """
    Simulate monthly rebalancing with walk-forward IS/OOS split.

    The simulation never looks ahead — scores at month M use only bars
    up to and including the last day of month M.
    """
    spy_bars = all_bars.get(BENCHMARK, [])
    if not spy_bars:
        raise ValueError("SPY bars missing — cannot run backtest.")

    all_dates = [b[0] for b in spy_bars]
    spy_close = {b[0]: b[4] for b in spy_bars}

    # Build per-ticker close/volume lookups
    tc, tv = {}, {}
    for sym in tickers:
        bars = all_bars.get(sym, [])
        tc[sym] = {b[0]: b[4] for b in bars}
        tv[sym] = {b[0]: b[5] for b in bars}

    month_starts = _month_start_indices(all_dates)
    if len(month_starts) < 4:
        raise ValueError("Need at least 4 months of data.")

    # Split — last `oos_months` are OOS, everything before is IS
    oos_start = max(0, len(month_starts) - oos_months)
    periods = {
        "in_sample":      month_starts[:oos_start],
        "out_of_sample":  month_starts[oos_start:],
    }

    print(f"\n  Walk-forward split:")
    if periods["in_sample"]:
        i0, i1 = periods["in_sample"][0], periods["in_sample"][-1]
        print(f"  In-sample:       {all_dates[i0]} → {all_dates[i1]}")
    if periods["out_of_sample"]:
        i0 = periods["out_of_sample"][0]
        print(f"  Out-of-sample:   {all_dates[i0]} → {all_dates[-1]}")

    results = {}
    for label, starts in periods.items():
        if len(starts) < 2:
            results[label] = {}
            continue

        portfolio_value = 100.0
        equity_curve    = []
        trades          = []
        monthly_returns = []
        regime_returns  = defaultdict(list)
        ic_scores       = []
        holdings        = {}

        for mi in range(len(starts) - 1):
            rebal_i   = starts[mi]
            next_i    = starts[mi + 1]
            rebal_d   = all_dates[rebal_i]
            next_d    = all_dates[next_i]

            # ── Regime (using all history up to rebal date, no lookahead) ──
            spy_hist = [spy_close[d] for d in all_dates[:rebal_i + 1] if d in spy_close]
            regime   = detect_regime(spy_hist)

            # ── Score each ticker using history up to rebal date only ──────
            scored = {}
            for sym in tickers:
                hist_dates = [d for d in all_dates[:rebal_i + 1] if d in tc[sym]]
                closes  = [tc[sym][d] for d in hist_dates]
                volumes = [tv[sym][d] for d in hist_dates]
                sc = compute_momentum_score(closes, volumes)
                if sc is not None:
                    scored[sym] = sc

            if not scored:
                continue

            # ── Select top N by regime ────────────────────────────────────
            n        = top_n.get(regime, top_n["neutral"])
            selected = sorted(scored, key=lambda s: scored[s], reverse=True)[:n]

            # ── Transaction cost based on turnover ────────────────────────
            old_set  = set(holdings)
            new_set  = set(selected)
            turnover = len(old_set.symmetric_difference(new_set)) / max(len(new_set), 1)
            cost     = turnover * cost_pct

            holdings = {sym: 1.0 / len(selected) for sym in selected}

            # ── Compute 1-month portfolio return ──────────────────────────
            port_ret    = 0.0
            actual_rets = {}
            valid       = 0
            for sym in selected:
                p0 = tc.get(sym, {}).get(rebal_d)
                p1 = tc.get(sym, {}).get(next_d)
                if p0 and p1 and p0 > 0:
                    r = (p1 - p0) / p0
                    port_ret += r * holdings[sym]
                    actual_rets[sym] = r
                    valid += 1

            if valid == 0:
                continue

            port_ret -= cost  # apply transaction cost

            # SPY benchmark for same period
            spy_p0 = spy_close.get(rebal_d)
            spy_p1 = spy_close.get(next_d)
            spy_ret = (spy_p1 - spy_p0) / spy_p0 if spy_p0 and spy_p1 else 0

            portfolio_value *= (1 + port_ret)
            monthly_returns.append(port_ret)
            regime_returns[regime].append(port_ret)
            equity_curve.append((next_d, round(portfolio_value, 4)))

            # ── Information Coefficient (Spearman rank correlation) ───────
            # This is the cleanest test of whether the score actually predicts
            # returns, independent of portfolio construction choices.
            syms = list(actual_rets)
            if len(syms) >= 3:
                score_rank  = sorted(range(len(syms)), key=lambda i: scored[syms[i]])
                return_rank = sorted(range(len(syms)), key=lambda i: actual_rets[syms[i]])
                n_ic = len(syms)
                d2   = sum((sr - rr) ** 2 for sr, rr in zip(score_rank, return_rank))
                ic   = 1 - (6 * d2) / (n_ic * (n_ic ** 2 - 1))
                ic_scores.append(ic)

            trades.append({
                "date":     rebal_d,
                "regime":   regime,
                "selected": selected,
                "port_ret": round(port_ret * 100, 2),
                "spy_ret":  round(spy_ret  * 100, 2),
                "alpha":    round((port_ret - spy_ret) * 100, 2),
            })

        results[label] = _compute_metrics(
            equity_curve, monthly_returns, regime_returns, ic_scores, trades
        )

    return results


def _compute_metrics(
    equity_curve, monthly_returns, regime_returns, ic_scores, trades
) -> dict:
    if not monthly_returns:
        return {}

    total_ret = (equity_curve[-1][1] / 100.0 - 1) * 100 if equity_curve else 0
    n_months  = len(monthly_returns)
    ann_ret   = ((1 + total_ret / 100) ** (12 / n_months) - 1) * 100 if n_months else 0

    avg  = sum(monthly_returns) / n_months
    var  = sum((r - avg) ** 2 for r in monthly_returns) / n_months
    std  = math.sqrt(var)
    sharpe = (avg / std * math.sqrt(12)) if std > 0 else 0

    peak, max_dd = 100.0, 0.0
    for _, v in equity_curve:
        peak  = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)

    calmar   = (ann_ret / 100) / max_dd if max_dd > 0 else 0
    beats    = sum(1 for t in trades if t["alpha"] > 0)
    win_rate = beats / len(trades) * 100 if trades else 0

    # Rolling 6-month consistency (fraction of 6-month windows that beat SPY)
    alphas = [t["alpha"] for t in trades]
    rolling_wins = []
    if len(alphas) >= 6:
        for i in range(len(alphas) - 5):
            w6 = alphas[i:i + 6]
            rolling_wins.append(sum(1 for a in w6 if a > 0) / 6)
    consistency = sum(rolling_wins) / len(rolling_wins) * 100 if rolling_wins else None

    avg_ic = sum(ic_scores) / len(ic_scores) if ic_scores else 0

    regime_avg = {
        r: round(sum(v) / len(v) * 100, 2)
        for r, v in regime_returns.items()
    }

    return {
        "n_months":                        n_months,
        "total_return_pct":                round(total_ret, 2),
        "annualised_return_pct":           round(ann_ret, 2),
        "sharpe_ratio":                    round(sharpe, 3),
        "max_drawdown_pct":                round(max_dd * 100, 2),
        "calmar_ratio":                    round(calmar, 3),
        "win_rate_vs_spy_pct":             round(win_rate, 1),
        "rolling_6m_consistency_pct":      round(consistency, 1) if consistency is not None else None,
        "avg_information_coefficient":     round(avg_ic, 3),
        "regime_avg_monthly_return_pct":   regime_avg,
        "monthly_trades":                  trades,
        "equity_curve":                    equity_curve,
    }


# ── Report ───────────────────────────────────────────────────────────────────

SEP  = "─" * 64
SEP2 = "═" * 64

CAVEATS = """
⚠  BEFORE ACTING ON THESE NUMBERS — READ THE CAVEATS:

  1. SURVIVORSHIP BIAS: Universe = today's tickers. Stocks that went
     bust or were delisted are not in this test. Results are optimistic;
     real live performance will be lower.

  2. PARTIAL SIGNAL ONLY: This tests the 6 price-based momentum signals.
     The live screener also uses analyst recommendations (30%), news
     sentiment (20%), and valuation (10%) — those can't be historically
     reconstructed without a data vendor. The full system may have more
     or less edge than this backtest suggests.

  3. SIMPLIFIED REGIME: SPY vs 50/200 MA only — not the full 6-component
     regime score (VIX, Fear&Greed, ADX, breadth). Some regime periods
     will be misclassified. Regime-stratified returns are illustrative.

  4. TRANSACTION COSTS ARE FLAT: Real slippage is higher for small/mid
     cap names, especially in low-liquidity periods.

  5. MONTHLY REBALANCE: The live system may trade differently depending
     on execution triggers. This simulation assumes frictionless monthly
     rebalancing on the first trading day of each month.

  Treat backtest results as a directional check, not a performance forecast.
"""


def print_report(results: dict) -> None:
    print(f"\n{SEP2}")
    print("  INVESTMENT ALPHA — MOMENTUM BACKTEST")
    print(f"  Signals tested: MA structure, MA50 distance, 52w position,")
    print(f"  52w return, 5-day momentum, volume ratio")
    print(SEP2)
    print(CAVEATS)

    for label, m in results.items():
        tag = "📊 IN-SAMPLE" if label == "in_sample" else "🎯 OUT-OF-SAMPLE  ← the one that matters"
        print(f"\n{SEP}")
        print(f"  {tag}")
        print(SEP)
        if not m:
            print("  Not enough data for this period.")
            continue

        print(f"  Months:                 {m['n_months']}")
        print(f"  Total return:           {m['total_return_pct']:+.1f}%")
        print(f"  Annualised return:      {m['annualised_return_pct']:+.1f}%")
        print(f"  Sharpe ratio:           {m['sharpe_ratio']:.2f}  (>1.0 = good, >0.5 = acceptable)")
        print(f"  Max drawdown:           {m['max_drawdown_pct']:.1f}%")
        print(f"  Calmar ratio:           {m['calmar_ratio']:.2f}  (ann_ret / max_dd)")
        print(f"  Win rate vs SPY:        {m['win_rate_vs_spy_pct']:.0f}%  (months above SPY)")
        if m.get("rolling_6m_consistency_pct") is not None:
            pct = m["rolling_6m_consistency_pct"]
            tag2 = "consistent" if pct > 60 else "inconsistent — may be noise"
            print(f"  6-month consistency:    {pct:.0f}%  ({tag2})")

        ic = m["avg_information_coefficient"]
        ic_label = (
            "strong predictive signal" if ic > 0.10 else
            "moderate signal"           if ic > 0.05 else
            "weak / marginal signal"    if ic > 0.0  else
            "⚠ NEGATIVE — score inversely predicts returns"
        )
        print(f"  Avg IC (rank corr):     {ic:+.3f}  → {ic_label}")

        print(f"\n  Monthly return by regime (simplified SPY-vs-MA proxy):")
        for regime, avg in sorted(m["regime_avg_monthly_return_pct"].items()):
            bar = ("+" if avg >= 0 else "") + "█" * max(0, int(abs(avg) * 5))
            print(f"    {regime:10s}  {avg:+.2f}%   {bar}")

        # Print last 6 months of trades
        trades = m.get("monthly_trades", [])
        if trades:
            print(f"\n  Recent monthly results (last 6):")
            print(f"  {'Date':12s} {'Regime':10s} {'Port':>8s} {'SPY':>8s} {'Alpha':>8s}")
            for t in trades[-6:]:
                print(f"  {t['date']:12s} {t['regime']:10s} {t['port_ret']:>+7.2f}% {t['spy_ret']:>+7.2f}% {t['alpha']:>+7.2f}%")

    # ── IS vs OOS degradation — the primary overfitting diagnostic ───────────
    is_m  = results.get("in_sample",     {})
    oos_m = results.get("out_of_sample", {})
    if is_m and oos_m:
        print(f"\n{SEP}")
        print("  OVERFITTING DIAGNOSTIC — IS vs OOS degradation")
        print(SEP)
        ret_deg = is_m["annualised_return_pct"] - oos_m["annualised_return_pct"]
        sr_deg  = is_m["sharpe_ratio"]           - oos_m["sharpe_ratio"]
        ic_deg  = is_m["avg_information_coefficient"] - oos_m["avg_information_coefficient"]
        print(f"  Annualised return:  IS {is_m['annualised_return_pct']:+.1f}%  →  OOS {oos_m['annualised_return_pct']:+.1f}%  (Δ {ret_deg:+.1f}pp)")
        print(f"  Sharpe ratio:       IS {is_m['sharpe_ratio']:.2f}         →  OOS {oos_m['sharpe_ratio']:.2f}           (Δ {sr_deg:+.2f})")
        print(f"  IC (rank corr):     IS {is_m['avg_information_coefficient']:+.3f}      →  OOS {oos_m['avg_information_coefficient']:+.3f}        (Δ {ic_deg:+.3f})")
        print()
        if oos_m["sharpe_ratio"] > 1.0 and abs(sr_deg) < 0.5:
            verdict = "✅ Strong OOS Sharpe with modest degradation — signal is likely real."
        elif oos_m["sharpe_ratio"] > 0.5:
            verdict = "✓  Acceptable OOS Sharpe — moderate edge, continue monitoring."
        elif ret_deg > 15 or sr_deg > 1.0:
            verdict = "⚠  Large IS→OOS degradation — likely overfitting or regime shift. Do NOT size up."
        else:
            verdict = "⚡ Weak OOS Sharpe — limited or no edge in price signals alone."
        print(f"  {verdict}")

    print(f"\n{SEP2}\n")


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Walk-forward momentum backtest for Investment Alpha screener"
    )
    parser.add_argument("--years",         type=int,   default=3,
                        help="Years of history to fetch (default: 3)")
    parser.add_argument("--oos-months",    type=int,   default=12,
                        help="Out-of-sample holdout months (default: 12)")
    parser.add_argument("--top-n-bull",    type=int,   default=7)
    parser.add_argument("--top-n-neutral", type=int,   default=5)
    parser.add_argument("--top-n-bear",    type=int,   default=3)
    parser.add_argument("--costs",         type=float, default=0.001,
                        help="One-way transaction cost fraction (default: 0.001 = 0.1%%)")
    parser.add_argument("--save-json",     type=str,   default=None,
                        help="Save full results to this JSON path")
    args = parser.parse_args()

    if not ALPACA_KEY or not ALPACA_SECRET:
        sys.exit(
            "❌ Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env or environment variables."
        )

    top_n = {
        "bull":    args.top_n_bull,
        "neutral": args.top_n_neutral,
        "bear":    args.top_n_bear,
    }

    end   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=args.years * 366)).strftime("%Y-%m-%d")

    all_syms = DEFAULT_TICKERS + [BENCHMARK]
    print(f"\n🔄 Fetching {args.years}Y daily bars ({start} → {end})")
    print(f"   Symbols: {len(all_syms)} ({len(DEFAULT_TICKERS)} stocks + SPY benchmark)")
    all_bars = fetch_bars(all_syms, start, end)
    print(f"   Retrieved data for {len(all_bars)} symbols.")

    missing = [s for s in all_syms if s not in all_bars]
    if missing:
        print(f"   ⚠  Missing data for: {missing}")

    print(f"\n🧮 Running walk-forward simulation...")
    print(f"   Top N — bull: {top_n['bull']}, neutral: {top_n['neutral']}, bear: {top_n['bear']}")
    print(f"   Transaction cost: {args.costs * 100:.2f}% per trade ({args.costs * 200:.2f}% round trip)")
    print(f"   OOS holdout: {args.oos_months} months")

    results = run_backtest(
        all_bars    = all_bars,
        tickers     = DEFAULT_TICKERS,
        top_n       = top_n,
        cost_pct    = args.costs,
        oos_months  = args.oos_months,
    )

    print_report(results)

    if args.save_json:
        out_path = Path(args.save_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Strip large equity curve from JSON to keep file small
        export = {}
        for label, m in results.items():
            export[label] = {k: v for k, v in m.items() if k not in ("equity_curve", "monthly_trades")}
        out_path.write_text(json.dumps(export, indent=2))
        print(f"Summary saved → {out_path}")


if __name__ == "__main__":
    main()
