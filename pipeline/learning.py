"""
pipeline/learning.py — Weekly Learning Update (v2 of the feedback loop)

Upgrades over the original monthly feedback.py:
  1. SHADOW OBSERVATIONS — learns from the top-30 ranked stocks every run
     (via pipeline/shadow.py), not just the 10 bought. ~3× more data,
     no selection bias.
  2. REGIME-CONDITIONAL WEIGHTS — separate learned weights for BULL /
     NEUTRAL / BEAR. Momentum that works in a bull tape and fails in a bear
     tape no longer averages out to "meh".
  3. EWMA INFORMATION COEFFICIENT — factor predictiveness is tracked as an
     exponentially weighted average (recent months matter more), instead of
     jumping ±5% on each month's noisy correlation.
  4. WEEKLY CADENCE with a smaller step size (run by learning.yml).

Compatibility: scoring.py keeps reading the flat data/learned_weights.json.
This module maintains the rich store in data/learned_weights_v2.json and
EXPORTS the current regime's weights to the flat file — zero changes needed
in scoring.py.

Usage:
    python pipeline/learning.py            # evaluate shadow, update, export
    python pipeline/learning.py --dry-run  # show changes, save nothing
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from pipeline import shadow
from pipeline.feedback import WEIGHT_BOUNDS, INVERTED_FACTORS, load_weights

log = logging.getLogger(__name__)

V2_FILE      = config.DATA_DIR / "learned_weights_v2.json"
FLAT_FILE    = Path(getattr(config, "LEARNED_WEIGHTS_FILE", config.BASE_DIR / "data" / "learned_weights.json"))

EWMA_ALPHA   = 0.30   # weight of the newest IC reading
DRIFT_RATE   = 0.02   # weekly step (gentler than the old monthly 5%)
MIN_OBS      = 15     # observations needed per regime before weights move


def _default_weights() -> dict:
    return dict(load_weights())


def _load_v2() -> dict:
    if V2_FILE.exists():
        try:
            raw = V2_FILE.read_bytes().rstrip(b"\x00")
            return json.loads(raw)
        except Exception:
            pass
    base = _default_weights()
    return {
        "regimes": {r: {"weights": dict(base), "ewma_ic": {}, "n_obs": 0}
                    for r in ("bull", "neutral", "bear")},
        "history": [],
    }


def _spearman_ic(observations: list) -> dict:
    """Spearman IC per factor across the observation set."""
    from scipy.stats import spearmanr
    factor_scores, returns = {}, []
    for o in observations:
        returns.append(o["actual_return"])
        for f, s in o["scores"].items():
            factor_scores.setdefault(f, []).append(s)
    ics = {}
    for f, scores in factor_scores.items():
        if len(scores) != len(returns) or len(returns) < 5:
            continue
        try:
            corr, _ = spearmanr(scores, returns)
            if corr == corr:
                ics[f] = round(float(corr), 4)
        except Exception:
            continue
    return ics


def _drift(weights: dict, ewma_ic: dict) -> dict:
    """Small weekly drift toward factors with positive EWMA IC."""
    new = dict(weights)
    for f, ic in ewma_ic.items():
        if f not in new:
            continue
        eff = -ic if f in INVERTED_FACTORS else ic
        new[f] = new[f] + DRIFT_RATE * new[f] * np.sign(eff) * min(abs(ic), 1.0)
    for f in new:
        lo, hi = WEIGHT_BOUNDS.get(f, (0.01, 0.50))
        new[f] = round(max(lo, min(hi, new[f])), 4)
    pos = [f for f in new if f != "volatility"]
    total = sum(new[f] for f in pos)
    if total > 0:
        scale = (1.0 - new.get("volatility", 0.10)) / total
        for f in pos:
            new[f] = round(new[f] * scale, 4)
    return new


def run(dry_run: bool = False) -> dict:
    # 1. Evaluate any shadow snapshots that have matured
    observations = shadow.evaluate()
    log.info("Shadow observations: %d total", len(observations))

    store = _load_v2()
    report = {"updated_regimes": [], "exported": None, "obs_total": len(observations)}

    # 2. Per-regime IC + EWMA + drift
    for regime in ("bull", "neutral", "bear"):
        obs_r = [o for o in observations if o["regime"] == regime]
        node = store["regimes"][regime]
        node["n_obs"] = len(obs_r)
        if len(obs_r) < MIN_OBS:
            log.info("  %s: %d obs (< %d) — weights unchanged", regime, len(obs_r), MIN_OBS)
            continue
        ics = _spearman_ic(obs_r)
        for f, ic in ics.items():
            prev = node["ewma_ic"].get(f)
            node["ewma_ic"][f] = round(
                ic if prev is None else EWMA_ALPHA * ic + (1 - EWMA_ALPHA) * prev, 4
            )
        old_w = dict(node["weights"])
        node["weights"] = _drift(node["weights"], node["ewma_ic"])
        changed = {f: (old_w.get(f), node["weights"].get(f))
                   for f in node["weights"] if old_w.get(f) != node["weights"].get(f)}
        report["updated_regimes"].append(regime)
        log.info("  %s: %d obs, IC=%s, weight changes=%s", regime, len(obs_r), ics, changed)

    # 3. Export current regime's weights to the flat file scoring.py reads
    try:
        from pipeline import regime as regime_module
        current = regime_module.run().get("regime", "neutral")
    except Exception:
        current = "neutral"
    export = store["regimes"][current]["weights"]
    report["exported"] = {"regime": current, "weights": export}

    store["history"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "obs_total": len(observations),
        "updated": report["updated_regimes"],
        "exported_regime": current,
    })
    store["history"] = store["history"][-100:]

    if dry_run:
        log.info("[DRY RUN] Would save v2 store and export %s weights: %s", current, export)
        return report

    V2_FILE.parent.mkdir(parents=True, exist_ok=True)
    V2_FILE.write_text(json.dumps(store, indent=2), encoding="utf-8")
    FLAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    FLAT_FILE.write_text(json.dumps(export, indent=2), encoding="utf-8")
    log.info("Saved v2 store; exported %s weights -> %s", current.upper(), FLAT_FILE)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Weekly learning update")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = run(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
