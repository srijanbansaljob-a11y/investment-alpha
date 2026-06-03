"""
pipeline/feedback.py - Phase 3D: Self-Learning Adaptive Factor Weights

After each monthly run, this module:

1. Loads the previous month's portfolio signals (from latest_portfolio.json)
2. Fetches actual 1-month returns for each BUY/HOLD position
3. For each factor, computes: did higher factor score -> higher actual return?
   (Spearman rank correlation between factor score and realised return)
4. Adjusts factor weights by +/- 5% in direction of each factor's correlation
   (GRADUAL DRIFT method -- safe, interpretable, no overfitting)
5. Saves updated weights to data/learned_weights.json
6. Prints a monthly performance attribution report

Usage:
  python pipeline/feedback.py               # run after monthly rebalance
  python pipeline/feedback.py --dry-run     # show what weights would change to, don't save
  python pipeline/feedback.py --reset       # delete learned_weights.json, revert to config defaults

The learned_weights.json is loaded by scoring.py on every subsequent run.
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

PORTFOLIO_STATE_FILE  = Path(getattr(config, "OUTPUT_DIR", "outputs")) / "latest_portfolio.json"
LEARNED_WEIGHTS_FILE  = Path(getattr(config, "LEARNED_WEIGHTS_FILE", "data/learned_weights.json"))
PERFORMANCE_LOG_FILE  = Path(getattr(config, "DATA_DIR", "data")) / "performance_log.json"

# Drift rate: how much weights shift each month (5% of the factor's weight)
DRIFT_RATE     = 0.05
# Min/max bounds per factor weight
WEIGHT_BOUNDS  = {
    "momentum":   (0.15, 0.45),
    "trend":      (0.10, 0.40),
    "quality":    (0.10, 0.35),
    "valuation":  (0.05, 0.30),
    "sentiment":  (0.03, 0.20),
    "volatility": (0.05, 0.20),
}
# Volatility is subtracted, so correlation logic is inverted
INVERTED_FACTORS = {"volatility"}


# ---------------------------------------------------------------------------
# Weight I/O
# ---------------------------------------------------------------------------

def load_weights():
    """Load current weights: learned if available, else config defaults."""
    if LEARNED_WEIGHTS_FILE.exists():
        try:
            w = json.loads(LEARNED_WEIGHTS_FILE.read_text())
            log.info("Loaded learned weights from %s", LEARNED_WEIGHTS_FILE)
            return w
        except Exception as e:
            log.warning("Failed to load learned weights: %s -- using config defaults", e)

    # Fall back to config
    w = dict(getattr(config, "FACTOR_WEIGHTS_WITH_SENTIMENT", config.FACTOR_WEIGHTS))
    log.info("Using config default weights")
    return w


def save_weights(weights, dry_run=False):
    """Save updated weights to JSON file."""
    if dry_run:
        log.info("[DRY RUN] Would save weights: %s", weights)
        return
    LEARNED_WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEARNED_WEIGHTS_FILE.write_text(json.dumps(weights, indent=2))
    log.info("Saved learned weights -> %s", LEARNED_WEIGHTS_FILE)


# ---------------------------------------------------------------------------
# Performance fetch
# ---------------------------------------------------------------------------

def fetch_actual_returns(positions, lookback_days=30):
    """
    For each position in the portfolio state, fetch the actual return
    over the past `lookback_days` days.

    Args:
        positions: list of dicts with 'ticker', 'entry_price', 'action'
        lookback_days: how many days ago the positions were entered

    Returns:
        dict {ticker: actual_return_float}
    """
    tickers = [p["ticker"] for p in positions if p.get("action") in ("BUY", "HOLD")]
    if not tickers:
        return {}

    log.info("Fetching actual returns for %d positions...", len(tickers))
    start = (datetime.now(timezone.utc).date() - timedelta(days=lookback_days + 5)).isoformat()

    try:
        data = yf.download(tickers, start=start, progress=False, auto_adjust=True)
        if isinstance(data.columns, pd.MultiIndex):
            close = data["Close"]
        else:
            close = data[["Close"]].rename(columns={"Close": tickers[0]})
    except Exception as e:
        log.error("Failed to fetch return data: %s", e)
        return {}

    returns = {}
    for p in positions:
        ticker = p["ticker"]
        if ticker not in close.columns:
            continue
        series = close[ticker].dropna()
        if len(series) < 2:
            continue
        entry_price = p.get("entry_price")
        if entry_price and entry_price > 0:
            current = float(series.iloc[-1])
            ret = (current - entry_price) / entry_price
        else:
            # Fallback: use first available price in window vs latest
            ret = (float(series.iloc[-1]) - float(series.iloc[0])) / float(series.iloc[0])
        returns[ticker] = round(ret, 6)
        log.debug("  %s: %.1f%% actual return", ticker, ret * 100)

    return returns


# ---------------------------------------------------------------------------
# Factor attribution
# ---------------------------------------------------------------------------

def compute_factor_correlations(positions, actual_returns):
    """
    For each factor, compute Spearman rank correlation between
    factor score and actual return.

    Args:
        positions: list of portfolio position dicts (must have 'signals' key with scores)
        actual_returns: {ticker: float} actual returns

    Returns:
        dict {factor_name: spearman_correlation}
    """
    from scipy.stats import spearmanr

    # Extract factor scores from the portfolio state
    factor_data = {}
    return_data  = []

    for p in positions:
        ticker = p["ticker"]
        if ticker not in actual_returns:
            continue
        scores = p.get("scores", {})
        if not scores:
            continue
        return_data.append(actual_returns[ticker])
        for factor, score in scores.items():
            factor_data.setdefault(factor, []).append(score)

    if len(return_data) < 3:
        log.warning("Too few data points for correlation (%d) -- skipping weight update", len(return_data))
        return {}

    correlations = {}
    for factor, scores in factor_data.items():
        if len(scores) != len(return_data):
            continue
        try:
            corr, pval = spearmanr(scores, return_data)
            correlations[factor] = round(float(corr), 4)
            log.info("  Factor %-12s: corr=%.3f  pval=%.3f  %s",
                     factor, corr, pval,
                     "* predictive" if abs(corr) > 0.2 else "")
        except Exception:
            correlations[factor] = 0.0

    return correlations


def update_weights_gradual_drift(current_weights, correlations):
    """
    Apply gradual drift:
    - Factor with positive correlation (correctly predicted returns): weight += DRIFT_RATE * weight
    - Factor with negative correlation (incorrectly predicted): weight -= DRIFT_RATE * weight
    - Volatility is inverted (negative score is good, so invert sign)
    - Clamp each weight to WEIGHT_BOUNDS
    - Renormalise so all weights (except volatility) sum to 1.0

    Returns updated weights dict.
    """
    new_weights = dict(current_weights)

    for factor, corr in correlations.items():
        if factor not in new_weights:
            continue
        # Invert correlation for subtracted factors (volatility)
        effective_corr = -corr if factor in INVERTED_FACTORS else corr
        delta = DRIFT_RATE * new_weights[factor] * np.sign(effective_corr) * min(abs(corr), 1.0)
        new_weights[factor] = new_weights[factor] + delta

    # Apply bounds
    for factor in new_weights:
        lo, hi = WEIGHT_BOUNDS.get(factor, (0.01, 0.50))
        new_weights[factor] = round(max(lo, min(hi, new_weights[factor])), 4)

    # Renormalise: positive factors sum to 1.0 (excluding volatility which is subtracted)
    pos_factors = [f for f in new_weights if f != "volatility"]
    total_pos = sum(new_weights[f] for f in pos_factors)
    if total_pos > 0:
        vol_w = new_weights.get("volatility", 0.10)
        scale = (1.0 - vol_w) / total_pos
        for f in pos_factors:
            new_weights[f] = round(new_weights[f] * scale, 4)

    return new_weights


# ---------------------------------------------------------------------------
# Performance log
# ---------------------------------------------------------------------------

def count_accumulated_observations() -> int:
    """
    Return the total number of position-month observations recorded
    in performance_log.json across all historical runs.
    """
    if not PERFORMANCE_LOG_FILE.exists():
        return 0
    try:
        data = json.loads(PERFORMANCE_LOG_FILE.read_text())
        return sum(entry.get("positions", 0) for entry in data)
    except Exception:
        return 0


def log_performance(month_str, positions, actual_returns, correlations,
                    old_weights, new_weights):
    """Append this month's results to the rolling performance log."""
    record = {
        "month":               month_str,
        "timestamp":           datetime.now(timezone.utc).isoformat(),
        "positions":           len(positions),
        "avg_return":          round(np.mean(list(actual_returns.values())), 4) if actual_returns else 0,
        "hit_rate":            round(sum(1 for r in actual_returns.values() if r > 0) / max(len(actual_returns), 1), 4),
        "factor_correlations": correlations,
        "weights_before":      old_weights,
        "weights_after":       new_weights,
    }

    existing = []
    if PERFORMANCE_LOG_FILE.exists():
        try:
            existing = json.loads(PERFORMANCE_LOG_FILE.read_text())
        except Exception:
            pass

    existing.append(record)
    PERFORMANCE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PERFORMANCE_LOG_FILE.write_text(json.dumps(existing, indent=2))
    log.info("Performance log updated -> %s (%d months)", PERFORMANCE_LOG_FILE, len(existing))
    return record


def print_report(record, old_weights, new_weights):
    """Print human-readable monthly attribution report."""
    print("\n" + "=" * 60)
    print("  INVESTMENT ALPHA -- MONTHLY PERFORMANCE ATTRIBUTION")
    print("=" * 60)
    print(f"  Month         : {record['month']}")
    print(f"  Positions     : {record['positions']}")
    print(f"  Avg Return    : {record['avg_return']*100:+.2f}%")
    print(f"  Hit Rate      : {record['hit_rate']*100:.0f}% of positions were profitable")
    print()
    print("  FACTOR ATTRIBUTION (did this factor predict returns?)")
    print("  " + "-" * 50)
    corrs = record.get("factor_correlations", {})
    for factor, corr in sorted(corrs.items(), key=lambda x: abs(x[1]), reverse=True):
        bar_len = int(abs(corr) * 20)
        bar = ("+" if corr > 0 else "-") * bar_len
        signal = "predictive" if abs(corr) > 0.3 else ("mild" if abs(corr) > 0.15 else "weak")
        print(f"  {factor:<12}: {corr:+.3f}  {bar:<20} {signal}")
    print()
    print("  WEIGHT CHANGES")
    print("  " + "-" * 50)
    weight_applied = record.get("weight_update_applied", True)
    total_obs      = record.get("total_observations")
    min_obs_req    = record.get("min_observations_req")
    if not weight_applied and total_obs is not None:
        print(f"  [ACCUMULATING] {total_obs}/{min_obs_req} observations — weights locked until threshold met")
    for f in sorted(set(list(old_weights.keys()) + list(new_weights.keys()))):
        old = old_weights.get(f, 0)
        new = new_weights.get(f, 0)
        change = new - old
        arrow = "^" if change > 0.001 else ("v" if change < -0.001 else "=")
        print(f"  {f:<12}: {old:.3f} -> {new:.3f}  {arrow}  ({change:+.4f})")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(dry_run=False, reset=False):
    """
    Run the feedback loop:
    1. Load prior portfolio state
    2. Fetch actual returns
    3. Compute factor correlations
    4. Update weights via gradual drift
    5. Save and report
    """
    if reset:
        if LEARNED_WEIGHTS_FILE.exists():
            LEARNED_WEIGHTS_FILE.unlink()
            print(f"Deleted {LEARNED_WEIGHTS_FILE} -- weights reset to config defaults")
        return

    # Load portfolio state
    if not PORTFOLIO_STATE_FILE.exists():
        log.error("No portfolio state file found at %s -- run main.py first", PORTFOLIO_STATE_FILE)
        return

    try:
        state = json.loads(PORTFOLIO_STATE_FILE.read_text())
    except Exception as e:
        log.error("Failed to read portfolio state: %s", e)
        return

    positions = state.get("portfolio", [])
    if not positions:
        log.warning("No positions in portfolio state -- nothing to evaluate")
        return

    run_date = state.get("run_date", "unknown")
    month_str = run_date[:7] if run_date != "unknown" else datetime.now().strftime("%Y-%m")
    log.info("Evaluating portfolio from run date: %s (%d positions)", run_date, len(positions))

    # Fetch actual returns since last run
    actual_returns = fetch_actual_returns(positions, lookback_days=30)
    if not actual_returns:
        log.warning("Could not fetch actual returns -- skipping weight update")
        return

    # Load current weights
    old_weights = load_weights()

    # Compute factor correlations
    # The portfolio state needs scores per position -- check if available
    # signals.py saves 'signals' dict; we need individual factor scores
    # If not available, use simplified attribution based on return vs composite score
    has_factor_scores = any("scores" in p for p in positions)

    if has_factor_scores:
        correlations = compute_factor_correlations(positions, actual_returns)
    else:
        # Simplified: use composite score as proxy
        log.info("Factor-level scores not in state -- using composite score correlation")
        scores  = [p.get("score", 0.5) for p in positions if p["ticker"] in actual_returns]
        returns = [actual_returns[p["ticker"]] for p in positions if p["ticker"] in actual_returns]
        if len(scores) >= 3:
            from scipy.stats import spearmanr
            corr, _ = spearmanr(scores, returns)
            correlations = {f: round(float(corr) * 0.5, 4) for f in old_weights}
            log.info("  Composite score correlation: %.3f", corr)
        else:
            correlations = {}

    if not correlations:
        log.warning("No factor correlations computed -- weights unchanged")
        return

    # ── Minimum sample size guard ────────────────────────────────────────
    # Count observations already in the log (BEFORE adding this month's).
    # We need MIN_FEEDBACK_OBSERVATIONS total before touching weights, so that
    # gradual drift isn't driven by noise from 1-2 months of data.
    min_obs = getattr(config, "MIN_FEEDBACK_OBSERVATIONS", 25)
    prior_obs = count_accumulated_observations()
    new_obs   = prior_obs + len(actual_returns)
    weight_update_allowed = new_obs >= min_obs

    if not weight_update_allowed:
        log.info(
            "Insufficient observations for weight update: %d accumulated + %d this month = %d "
            "(need %d). Logging performance but keeping current weights.",
            prior_obs, len(actual_returns), new_obs, min_obs,
        )

    # Compute new weights (for logging purposes), but only apply if guard passes
    new_weights = (
        update_weights_gradual_drift(old_weights, correlations)
        if weight_update_allowed
        else dict(old_weights)   # no change
    )

    # Log and report
    record = log_performance(month_str, positions, actual_returns,
                             correlations, old_weights, new_weights)
    record["weight_update_applied"] = weight_update_allowed
    record["total_observations"]    = new_obs
    record["min_observations_req"]  = min_obs
    print_report(record, old_weights, new_weights)

    if weight_update_allowed:
        save_weights(new_weights, dry_run=dry_run)
        log.info("Weights updated after %d accumulated observations (threshold: %d)",
                 new_obs, min_obs)
    else:
        log.info("Weights NOT saved (accumulating data, %d/%d observations met)",
                 new_obs, min_obs)

    return record


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Investment Alpha -- Monthly Feedback Loop")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show weight changes but do not save")
    parser.add_argument("--reset", action="store_true",
                        help="Delete learned weights, revert to config defaults")
    args = parser.parse_args()

    run(dry_run=args.dry_run, reset=args.reset)
