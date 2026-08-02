"""
scripts/backfill_shadow_history.py — Replay the learner over real history.

THE QUESTION IT ANSWERS
-----------------------
The learner needs 12 weekly snapshots before it will move a weight, so live
data takes three months to become useful. But the same calculation can be run
over the PAST: compute factor scores as they would have looked on each of the
last N weekly dates, measure what actually happened over the fixed forward
window, and see what the learner would have concluded.

That tells you today whether the mechanism reaches sane answers on real market
data — which factors ranked your universe well, how consistently, and whether
any would have crossed the significance bar.

HONEST LIMITATIONS — read before believing the output
-----------------------------------------------------
1. PRICE FACTORS ONLY. Momentum, trend and volatility can be reconstructed
   from price history. Quality, valuation and sentiment need point-in-time
   fundamentals (what a company's ROE looked like THAT day, not as restated
   today), which yfinance cannot provide. So this validates roughly 60% of
   the live model's weight.
2. IN-SAMPLE. You are examining a period you can already see. A factor that
   worked over the past year may not work next year. This shows the mechanism
   produces sensible conclusions from real data; it does not prove the
   conclusions will hold.
3. SURVIVORSHIP. The universe is today's constituents run backwards —
   companies delisted during the period are absent.

It does NOT write to data/shadow_log.json. Output goes to a separate file so
the live learning history is never contaminated with reconstructed data.

USAGE
-----
    python scripts/backfill_shadow_history.py                  # 52 weeks
    python scripts/backfill_shadow_history.py --weeks 104
    python scripts/backfill_shadow_history.py --universe live  # 580 tickers
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from pipeline import learning as lrn
from pipeline.shadow import HORIZON_DAYS

log = logging.getLogger("backfill")

OUT_FILE = config.DATA_DIR / "shadow_backfill.json"


def _download(tickers, start, end):
    import yfinance as yf
    log.info("Downloading %d tickers, %s → %s", len(tickers), start, end)
    data = yf.download(list(set(tickers)), start=start, end=end,
                       auto_adjust=True, progress=False, threads=True)
    close = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data
    return close.dropna(how="all")


def _pct_rank(s: pd.Series) -> pd.Series:
    return s.rank(pct=True).fillna(0.5)


def score_as_of(close: pd.DataFrame, asof) -> pd.DataFrame:
    """
    Factor scores using ONLY data available at `asof` — the same construction
    as pipeline/scoring.py for the price-based factors.
    """
    hist = close.loc[:asof]
    if len(hist) < 260:
        return pd.DataFrame()

    rows = []
    for t in hist.columns:
        s = hist[t].dropna()
        if len(s) < 260:
            continue
        px = float(s.iloc[-1])
        # Skip-month momentum, matching config.SKIP_MONTH_MOMENTUM
        m3  = s.iloc[-21] / s.iloc[-63]  - 1
        m6  = s.iloc[-21] / s.iloc[-126] - 1
        m12 = s.iloc[-21] / s.iloc[-252] - 1
        sma50, sma200 = s.iloc[-50:].mean(), s.iloc[-200:].mean()
        logret = np.log(s / s.shift(1)).dropna()
        vol = float(logret.iloc[-60:].std() * np.sqrt(252))
        if not np.isfinite(vol) or vol <= 0:
            continue
        rows.append({"ticker": t, "price": px, "m3": m3, "m6": m6, "m12": m12,
                     "pct_sma50": (px - sma50) / sma50,
                     "pct_sma200": (px - sma200) / sma200, "vol": vol})
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["momentum"]   = (_pct_rank(df.m3) + _pct_rank(df.m6) + _pct_rank(df.m12)) / 3
    df["trend"]      = (_pct_rank(df.pct_sma50) + _pct_rank(df.pct_sma200)) / 2
    df["volatility"] = _pct_rank(df.vol)
    return df


def forward_return(close: pd.DataFrame, ticker: str, asof, horizon: int) -> float | None:
    """Return over exactly `horizon` calendar days after asof — same window
    for every observation, which is what makes the ICs comparable."""
    try:
        s = close[ticker].dropna()
        p0 = s.loc[:asof]
        if p0.empty:
            return None
        entry = float(p0.iloc[-1])
        fwd = s.loc[asof:asof + timedelta(days=horizon)]
        if len(fwd) < 2:
            return None
        return (float(fwd.iloc[-1]) - entry) / entry
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Replay the learner over real history")
    ap.add_argument("--weeks", type=int, default=52)
    ap.add_argument("--universe", choices=["live", "legacy"], default="legacy",
                    help="legacy = 119 mega-caps (fast); live = config.ALL_TICKERS")
    ap.add_argument("--sample", type=int, default=150,
                    help="stocks kept per weekly snapshot")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if args.universe == "live":
        tickers = list(config.ALL_TICKERS)
    else:
        from backtest.backtest import BACKTEST_TICKERS
        tickers = list(BACKTEST_TICKERS)

    end   = datetime.now().date()
    start = end - timedelta(days=args.weeks * 7 + 420)   # +history for SMA200/12m
    close = _download(tickers, start.isoformat(), end.isoformat())
    if close.empty:
        log.error("No price data — aborting")
        sys.exit(1)

    # Weekly dates, oldest first, leaving room for the forward window
    idx = close.index
    last_usable = idx[-1] - pd.Timedelta(days=HORIZON_DAYS)
    weekly = [d for d in pd.date_range(end=last_usable, periods=args.weeks, freq="7D")
              if d >= idx[0]]

    observations, snapshots = [], []
    for d in weekly:
        asof = idx[idx <= d]
        if len(asof) == 0:
            continue
        asof = asof[-1]
        scored = score_as_of(close, asof)
        if scored.empty:
            continue
        scored = scored.sort_values("momentum", ascending=False)
        if len(scored) > args.sample:
            keep = np.linspace(0, len(scored) - 1, args.sample).astype(int)
            scored = scored.iloc[keep]

        n_ok = 0
        for _, r in scored.iterrows():
            fwd = forward_return(close, r.ticker, asof, HORIZON_DAYS)
            if fwd is None:
                continue
            observations.append({
                "ticker": r.ticker,
                "scores": {"momentum": round(float(r.momentum), 4),
                           "trend": round(float(r.trend), 4),
                           "volatility": round(float(r.volatility), 4)},
                "actual_return": round(float(fwd), 6),
                "regime": "bull",     # historical regime not reconstructed
                "date": asof.date().isoformat(),
            })
            n_ok += 1
        snapshots.append({"date": asof.date().isoformat(), "stocks": n_ok})
        log.info("  %s: %d stocks scored + evaluated", asof.date(), n_ok)

    if not observations:
        log.error("No observations built — aborting")
        sys.exit(1)

    periods = sorted({o["date"] for o in observations})
    print("\n" + "=" * 70)
    print("  LEARNER REPLAY OVER REAL HISTORY")
    print(f"  {len(periods)} weekly snapshots · {len(observations)} observations · "
          f"{HORIZON_DAYS}-day forward window")
    print("=" * 70)

    per_period = {}
    for p in periods:
        obs_p = [o for o in observations if o["date"] == p]
        for f, d in lrn._spearman_ic(obs_p).items():
            per_period.setdefault(f, []).append(d["ic"])

    print(f"\n  {'factor':<12}{'mean IC':>10}{'weeks':>8}{'t-stat':>9}   verdict")
    print("  " + "-" * 62)
    qualifying = {}
    for f, series in sorted(per_period.items()):
        mean_ic, t = lrn._ic_tstat(series)
        ok = np.isfinite(t) and abs(t) >= lrn.IC_TSTAT_MIN
        if ok:
            qualifying[f] = (mean_ic, t)
        print(f"  {f:<12}{mean_ic:>+10.4f}{len(series):>8}{t:>9.2f}   "
              f"{'REAL — would move the weight' if ok else 'noise — ignored'}")

    if qualifying:
        base = dict(config.FACTOR_WEIGHTS_WITH_SENTIMENT)
        after = lrn._drift(base, {f: v[0] for f, v in qualifying.items()},
                           confidence={f: v[1] for f, v in qualifying.items()},
                           baseline=base)
        print("\n  Weight change the learner would apply (one week):")
        for f in qualifying:
            print(f"    {f:<12}{base[f]*100:>6.1f}% → {after[f]*100:.1f}%  "
                  f"({(after[f]-base[f])*100:+.2f}pp)")
    else:
        print("\n  No factor cleared the bar — the learner would hold weights steady.")

    print("\n  CAVEATS")
    print("  · price factors only — quality/valuation/sentiment need point-in-time")
    print("    fundamentals that yfinance cannot supply (~40% of the live model)")
    print("  · in-sample: this period is already visible; it does not predict")
    print("  · survivorship: universe is today's constituents run backwards")
    print("=" * 70)

    OUT_FILE.write_text(json.dumps(
        {"generated": datetime.now().isoformat(), "weeks": len(periods),
         "snapshots": snapshots, "observations": len(observations),
         "ic_by_factor": {f: {"series": [round(x, 4) for x in s],
                              "mean_ic": round(lrn._ic_tstat(s)[0], 4),
                              "t_stat": round(lrn._ic_tstat(s)[1], 2)}
                          for f, s in per_period.items()}},
        indent=2), encoding="utf-8")
    print(f"\n  Detail saved → {OUT_FILE.name}  (separate from the live shadow log)")


if __name__ == "__main__":
    main()
