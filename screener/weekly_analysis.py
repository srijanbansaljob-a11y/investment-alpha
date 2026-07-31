"""
=============================================================================
  WEEKLY LEARNING ANALYSIS
  Run every Sunday evening (or Monday morning before the daily run).
  Reads trade_log.csv, correlates score components with 3d/5d returns,
  and outputs weight suggestions for daily_sentiment_runner.py.

  HOW THE LEARNING LOOP WORKS:
    1. daily_sentiment_runner.py logs signals to trade_log.csv each morning
    2. nightly_updater.py fills in forward prices each evening
    3. THIS SCRIPT runs weekly and outputs weight change suggestions
    4. YOU review the suggestions and decide whether to apply them
    5. If approved, update SCORE_WEIGHTS in daily_sentiment_runner.py

  Requires at least 20 resolved signals (outcome_3d filled) for meaningful
  analysis. Results stabilise after ~50 signals (4–6 weeks of trading).

USAGE:
  python weekly_analysis.py             # Full report to console
  python weekly_analysis.py --apply     # Also write approved weights to config
  python weekly_analysis.py --min 10    # Lower minimum signal threshold
=============================================================================
"""

import csv
import json
import os
import sys
import argparse
from datetime import datetime, date
from collections import defaultdict

OUTPUT_DIR        = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG         = os.path.join(OUTPUT_DIR, "trade_log.csv")
WEIGHTS_FILE      = os.path.join(OUTPUT_DIR, "weight_suggestions.json")
RUNNER_FILE       = os.path.join(OUTPUT_DIR, "daily_sentiment_runner.py")

# Current weights (must match SCORE_WEIGHTS in daily_sentiment_runner.py)
CURRENT_WEIGHTS = {
    "analyst":        30,
    "momentum":       25,
    "news_sentiment": 20,
    "macro_alignment":15,
    "valuation":      10,
}
TOTAL_WEIGHT = 100


# ─── DATA LOADING ─────────────────────────────────────────────────────────────

def load_resolved_signals(min_signals: int = 20) -> list[dict]:
    """Load rows where 5d return (outcome_3d) is filled."""
    if not os.path.exists(TRADE_LOG):
        print(f"  ✗ trade_log.csv not found at {TRADE_LOG}")
        print("    Run daily_sentiment_runner.py first.")
        return []

    resolved = []
    with open(TRADE_LOG, newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("outcome_3d"):
                continue
            try:
                resolved.append({
                    "date":           row["date"],
                    "ticker":         row["ticker"],
                    "conviction":     row.get("conviction", ""),
                    "total_score":    float(row.get("total_score") or 0),
                    "analyst_pts":    float(row.get("analyst_pts") or 0),
                    "momentum_pts":   float(row.get("momentum_pts") or 0),
                    "news_pts":       float(row.get("news_pts") or 0),
                    "macro_pts":      float(row.get("macro_pts") or 0),
                    "valuation_pts":  float(row.get("valuation_pts") or 0),
                    "return_3d":      float(row.get("return_3d") or 0),
                    "return_5d":      float(row.get("return_5d") or 0),
                    "outcome_3d":     row.get("outcome_3d", ""),
                    "regime_label":   row.get("regime_label", ""),
                    "strategy_bucket":row.get("strategy_bucket", ""),
                    "near_earnings":  row.get("near_earnings", ""),
                    "entry_price":    float(row.get("entry_price") or 0),
                    "price_5d":       float(row.get("price_5d") or 0),
                })
            except (ValueError, KeyError):
                continue

    if len(resolved) < min_signals:
        print(f"\n  ⚠  Only {len(resolved)} resolved signals found "
              f"(need {min_signals} for reliable analysis).")
        print(f"     Keep running the daily engine — analysis improves over time.")
        if len(resolved) == 0:
            return []
        print(f"     Showing preliminary results with {len(resolved)} signals:\n")

    return resolved


# ─── STATISTICS ───────────────────────────────────────────────────────────────

def mean(values: list) -> float:
    return sum(values) / len(values) if values else 0.0


def pearson_correlation(xs: list, ys: list) -> float:
    """Compute Pearson correlation coefficient. Pure Python, no numpy."""
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = mean(xs), mean(ys)
    num   = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx    = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy    = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    return round(num / (dx * dy), 3)


def win_rate(signals: list) -> float:
    wins = sum(1 for s in signals if s["outcome_3d"] == "WIN")
    return round(wins / len(signals) * 100, 1) if signals else 0.0


def avg_return(signals: list, field: str = "return_3d") -> float:
    vals = [s[field] for s in signals if s.get(field) is not None]
    return round(mean(vals), 2) if vals else 0.0


# ─── WEIGHT SUGGESTIONS ───────────────────────────────────────────────────────

def suggest_weights(correlations: dict) -> dict:
    """
    Translate correlations into new weight suggestions.

    Logic:
      - Start from current weights
      - Components with higher correlation get more weight
      - Components with near-zero or negative correlation get less weight
      - Total must still sum to TOTAL_WEIGHT (100)
      - Max single-step change: ±8 pts per component
      - Minimum weight per component: 5 pts (don't eliminate any component)

    This is conservative by design. You should see 2–4 weeks of data
    before trusting any suggestion.
    """
    component_map = {
        "analyst":        "analyst_pts",
        "momentum":       "momentum_pts",
        "news_sentiment": "news_pts",
        "macro_alignment":"macro_pts",
        "valuation":      "valuation_pts",
    }

    # Normalise correlations to 0–1 scale for redistribution
    corrs = {k: correlations.get(v, 0.0) for k, v in component_map.items()}

    # Shift negative correlations to zero before redistribution
    # (we don't want to assign negative weight)
    adjusted = {k: max(0.0, v) for k, v in corrs.items()}
    total_adj = sum(adjusted.values())

    if total_adj == 0:
        # All correlations at or below zero — keep current weights
        return {k: v for k, v in CURRENT_WEIGHTS.items()}

    # Proportional target weights
    targets = {k: (adjusted[k] / total_adj) * TOTAL_WEIGHT for k in adjusted}

    # Blend: 60% current + 40% target (conservative update)
    blended = {
        k: CURRENT_WEIGHTS[k] * 0.60 + targets[k] * 0.40
        for k in CURRENT_WEIGHTS
    }

    # Cap max change at ±8 pts per component
    capped = {}
    for k in blended:
        delta = blended[k] - CURRENT_WEIGHTS[k]
        delta = max(-8, min(8, delta))
        capped[k] = max(5, round(CURRENT_WEIGHTS[k] + delta))  # min 5 pts

    # Normalise to exactly 100
    total_capped = sum(capped.values())
    diff         = TOTAL_WEIGHT - total_capped
    if diff != 0:
        # Add/subtract from the largest component
        biggest = max(capped, key=capped.get)
        capped[biggest] += diff

    return capped


# ─── REGIME ANALYSIS ──────────────────────────────────────────────────────────

def analyse_by_regime(signals: list) -> dict:
    """Break down win rate and avg return by regime label."""
    groups = defaultdict(list)
    for s in signals:
        label = s.get("regime_label", "Unknown")
        groups[label].append(s)
    result = {}
    for label, group in sorted(groups.items()):
        result[label] = {
            "count":      len(group),
            "win_rate":   win_rate(group),
            "avg_return": avg_return(group, "return_3d"),
        }
    return result


def analyse_by_bucket(signals: list) -> dict:
    """Break down by strategy bucket."""
    groups = defaultdict(list)
    for s in signals:
        bucket = s.get("strategy_bucket", "unknown")
        groups[bucket].append(s)
    result = {}
    for bucket, group in sorted(groups.items()):
        result[bucket] = {
            "count":      len(group),
            "win_rate":   win_rate(group),
            "avg_return": avg_return(group, "return_3d"),
        }
    return result


# ─── APPLY WEIGHTS ────────────────────────────────────────────────────────────

def apply_weights_to_runner(new_weights: dict):
    """
    Write new SCORE_WEIGHTS into daily_sentiment_runner.py.
    Creates a backup first.
    """
    if not os.path.exists(RUNNER_FILE):
        print(f"  ✗ Cannot find {RUNNER_FILE}")
        return False

    with open(RUNNER_FILE, "r") as f:
        content = f.read()

    # Build replacement block
    new_block = "SCORE_WEIGHTS = {\n"
    for k, v in new_weights.items():
        new_block += f'    "{k}": {v},\n'
    new_block += "}"

    # Find and replace existing SCORE_WEIGHTS block
    import re
    pattern = r"SCORE_WEIGHTS\s*=\s*\{[^}]+\}"
    if not re.search(pattern, content):
        print("  ✗ Could not locate SCORE_WEIGHTS in daily_sentiment_runner.py")
        return False

    # Backup
    backup_path = RUNNER_FILE.replace(".py", "_backup.py")
    with open(backup_path, "w") as f:
        f.write(content)
    print(f"  📦 Backup saved → {backup_path}")

    new_content = re.sub(pattern, new_block, content)
    with open(RUNNER_FILE, "w") as f:
        f.write(new_content)

    print(f"  ✅ SCORE_WEIGHTS updated in daily_sentiment_runner.py")
    return True


# ─── MAIN REPORT ──────────────────────────────────────────────────────────────

def run_analysis(min_signals: int = 20, apply: bool = False):
    today_str = date.today().isoformat()
    print(f"\n{'='*60}")
    print(f"  🧠  WEEKLY LEARNING ANALYSIS  —  {today_str}")
    print(f"{'='*60}\n")

    signals = load_resolved_signals(min_signals)
    if not signals:
        return

    n = len(signals)
    print(f"  Resolved signals: {n}")
    print(f"  Date range: {signals[0]['date']} → {signals[-1]['date']}")

    # ── Overall performance ────────────────────────────────────────────
    wr_3d  = win_rate(signals)
    ar_3d  = avg_return(signals, "return_3d")
    ar_5d  = avg_return(signals, "return_5d")
    wins   = sum(1 for s in signals if s["outcome_3d"] == "WIN")
    losses = sum(1 for s in signals if s["outcome_3d"] == "LOSS")
    flat   = sum(1 for s in signals if s["outcome_3d"] == "FLAT")

    print(f"\n  📈 OVERALL PERFORMANCE")
    print(f"     3-day win rate:   {wr_3d:.1f}%  ({wins}W / {losses}L / {flat}F)")
    print(f"     Avg 3d return:    {ar_3d:+.2f}%")
    print(f"     Avg 5d return:    {ar_5d:+.2f}%")

    # ── Component correlations ─────────────────────────────────────────
    components = {
        "analyst_pts":   [s["analyst_pts"]   for s in signals],
        "momentum_pts":  [s["momentum_pts"]  for s in signals],
        "news_pts":      [s["news_pts"]       for s in signals],
        "macro_pts":     [s["macro_pts"]      for s in signals],
        "valuation_pts": [s["valuation_pts"]  for s in signals],
    }
    returns_3d = [s["return_3d"] for s in signals]

    print(f"\n  🔬 SCORE COMPONENT CORRELATIONS (with 3d return)")
    print(f"     {'Component':<22} {'Corr':<8} {'Current Wt':<12} {'Signal'}")
    print(f"     {'-'*56}")

    corr_results = {}
    for comp, vals in components.items():
        corr = pearson_correlation(vals, returns_3d)
        corr_results[comp] = corr
        curr_wt = CURRENT_WEIGHTS.get(comp.replace("_pts", ""), "?")
        if   corr > 0.30:  signal = "⬆ Strong predictor"
        elif corr > 0.15:  signal = "↑ Moderate predictor"
        elif corr > 0.00:  signal = "→ Weak predictor"
        elif corr > -0.10: signal = "≈ Noise (near zero)"
        else:              signal = "⬇ Negative signal"
        print(f"     {comp:<22} {corr:+.3f}   {str(curr_wt)+'pts':<12} {signal}")

    # ── Weight suggestions ─────────────────────────────────────────────
    new_weights = suggest_weights(corr_results)

    print(f"\n  ⚖  WEIGHT SUGGESTIONS")
    print(f"     {'Component':<22} {'Current':<10} {'Suggested':<12} {'Delta'}")
    print(f"     {'-'*52}")
    any_change = False
    for k, new_v in new_weights.items():
        curr_v = CURRENT_WEIGHTS[k]
        delta  = new_v - curr_v
        arrow  = "▲" if delta > 0 else ("▼" if delta < 0 else "─")
        marker = f"  {arrow} {delta:+d}" if delta != 0 else "  ─"
        if delta != 0:
            any_change = True
        print(f"     {k:<22} {curr_v}pts      {new_v}pts      {marker}")

    if not any_change:
        print(f"     ✓ Current weights look good — no changes suggested")

    # ── Regime breakdown ──────────────────────────────────────────────
    print(f"\n  🌍 PERFORMANCE BY REGIME")
    print(f"     {'Regime':<28} {'N':<6} {'Win%':<8} {'Avg 3d'}")
    print(f"     {'-'*52}")
    for label, stats in analyse_by_regime(signals).items():
        short_label = label[:27]
        print(f"     {short_label:<28} {stats['count']:<6} "
              f"{stats['win_rate']:<8.1f} {stats['avg_return']:+.2f}%")

    # ── Strategy bucket breakdown ──────────────────────────────────────
    print(f"\n  🎯 PERFORMANCE BY STRATEGY BUCKET")
    print(f"     {'Bucket':<18} {'N':<6} {'Win%':<8} {'Avg 3d'}")
    print(f"     {'-'*40}")
    for bucket, stats in analyse_by_bucket(signals).items():
        print(f"     {bucket:<18} {stats['count']:<6} "
              f"{stats['win_rate']:<8.1f} {stats['avg_return']:+.2f}%")

    # ── Earnings filter check ──────────────────────────────────────────
    near_earn = [s for s in signals if str(s.get("near_earnings", "")).lower() == "true"]
    no_earn   = [s for s in signals if str(s.get("near_earnings", "")).lower() != "true"]
    if near_earn and no_earn:
        print(f"\n  📅 NEAR-EARNINGS FILTER CHECK")
        print(f"     Near earnings (n={len(near_earn)}):  "
              f"win rate {win_rate(near_earn):.1f}%  avg {avg_return(near_earn, 'return_3d'):+.2f}%")
        print(f"     No earnings   (n={len(no_earn)}):  "
              f"win rate {win_rate(no_earn):.1f}%  avg {avg_return(no_earn, 'return_3d'):+.2f}%")
        if win_rate(near_earn) > win_rate(no_earn) + 10:
            print(f"     → Earnings catalyst filter is adding value ✅")
        elif win_rate(near_earn) < win_rate(no_earn) - 10:
            print(f"     → Near-earnings signals underperforming — consider tightening filter ⚠")

    # ── Save suggestions ──────────────────────────────────────────────
    suggestion = {
        "generated":        today_str,
        "signal_count":     n,
        "overall_win_rate": wr_3d,
        "avg_return_3d":    ar_3d,
        "correlations":     {k: round(v, 3) for k, v in corr_results.items()},
        "current_weights":  CURRENT_WEIGHTS,
        "suggested_weights":new_weights,
        "changes":          {k: new_weights[k] - CURRENT_WEIGHTS[k] for k in new_weights
                             if new_weights[k] != CURRENT_WEIGHTS[k]},
    }

    with open(WEIGHTS_FILE, "w") as f:
        json.dump(suggestion, f, indent=2)
    print(f"\n  💾 Suggestions saved → {WEIGHTS_FILE}")

    # ── Discord-ready summary ─────────────────────────────────────────
    print(f"\n  📱 DISCORD SUMMARY (copy-paste)")
    print(f"  {'-'*56}")
    print(f"  **Weekly Learning Report — {today_str}**")
    print(f"  Signals resolved: {n}  |  Win rate: {wr_3d:.1f}%  |  Avg 3d: {ar_3d:+.2f}%")
    top_corr  = max(corr_results, key=corr_results.get)
    low_corr  = min(corr_results, key=corr_results.get)
    print(f"  Best predictor: **{top_corr}** (r={corr_results[top_corr]:+.3f})")
    print(f"  Weakest signal: **{low_corr}** (r={corr_results[low_corr]:+.3f})")
    if any_change:
        changes_str = ", ".join(
            f"{k}: {CURRENT_WEIGHTS[k]}→{new_weights[k]}"
            for k in new_weights if new_weights[k] != CURRENT_WEIGHTS[k]
        )
        print(f"  Suggested weight changes: {changes_str}")
        print(f"  Run `python weekly_analysis.py --apply` to apply")
    else:
        print(f"  Weights: no changes suggested this week ✓")
    print(f"  {'-'*56}")

    # ── Apply weights if requested ────────────────────────────────────
    if apply and any_change:
        print(f"\n  ✍  Applying weight suggestions to daily_sentiment_runner.py...")
        apply_weights_to_runner(new_weights)
    elif apply and not any_change:
        print(f"\n  No weight changes to apply.")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Weekly learning analysis — correlates score components with trade outcomes"
    )
    parser.add_argument("--apply", action="store_true",
                        help="Apply suggested weights to daily_sentiment_runner.py")
    parser.add_argument("--min", type=int, default=20, metavar="N",
                        help="Minimum resolved signals required (default: 20)")
    args = parser.parse_args()
    run_analysis(min_signals=args.min, apply=args.apply)
