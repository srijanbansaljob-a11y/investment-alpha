"""
tests/test_learning_endtoend.py — Does the learner actually learn?

The other test files check shape ("is the guard present?"). This one checks
BEHAVIOUR: given a world where momentum genuinely predicts returns and
valuation genuinely does not, does the chain

    shadow observations → per-period IC → t-stat across periods
                        → significance gate → weight drift

reach the right conclusion, in the right direction, at a useful size?

Everything is synthetic and deterministic (fixed seed), so this runs in
milliseconds and needs no network, no broker and no waiting 12 weeks.

Why it exists: two guards in this module looked rigorous and did nothing —
a significance bar so strict it was an off switch, and a drift step so small
a qualifying factor moved 28.0% → 28.0%. Neither was visible by reading the
code; both showed up the moment behaviour was simulated.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def _load_learning():
    spec = importlib.util.spec_from_file_location("lrn", ROOT / "pipeline" / "learning.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_observations(weeks: int, n_stocks: int = 150, seed: int = 42,
                      momentum_edge: float = 0.9, noise: float = 1.0,
                      regime: str = "bull") -> list:
    """
    Build a synthetic world.

    Each week, every stock gets random factor scores in [0,1]. Its forward
    return is driven by `momentum_edge` × (momentum score) plus noise —
    so momentum genuinely ranks, and valuation/sentiment genuinely don't.
    """
    rng = np.random.default_rng(seed)
    obs = []
    for w in range(weeks):
        date = f"2026-{(w // 4) + 1:02d}-{(w % 4) * 7 + 1:02d}"
        for _ in range(n_stocks):
            mom = float(rng.random())
            val = float(rng.random())
            sen = float(rng.random())
            ret = momentum_edge * (mom - 0.5) * 0.02 + rng.normal(0, noise) * 0.02
            obs.append({
                "ticker": f"T{rng.integers(0, 9999)}",
                "scores": {"momentum": mom, "valuation": val, "sentiment": sen},
                "actual_return": float(ret),
                "regime": regime,
                "date": date,
            })
    return obs


class TestLearnerFindsRealEdge(unittest.TestCase):
    """A factor that genuinely ranks stocks should be detected and rewarded."""

    def setUp(self):
        self.lrn = _load_learning()

    def test_detects_momentum_ignores_the_rest(self):
        obs = make_observations(weeks=20)
        periods = sorted({o["date"] for o in obs})
        per_period = {}
        for p in periods:
            for f, d in self.lrn._spearman_ic([o for o in obs if o["date"] == p]).items():
                per_period.setdefault(f, []).append(d["ic"])

        verdicts = {}
        for f, series in per_period.items():
            mean_ic, t = self.lrn._ic_tstat(series)
            verdicts[f] = (mean_ic, t, abs(t) >= self.lrn.IC_TSTAT_MIN)

        self.assertTrue(verdicts["momentum"][2],
                        f"momentum has a real edge but was not detected: {verdicts['momentum']}")
        self.assertGreater(verdicts["momentum"][0], 0,
                           "momentum edge detected with the wrong sign")
        for f in ("valuation", "sentiment"):
            self.assertFalse(verdicts[f][2],
                             f"{f} has NO edge but was reported as real: {verdicts[f]}")

    def test_weight_moves_toward_the_predictive_factor(self):
        obs = make_observations(weeks=20)
        periods = sorted({o["date"] for o in obs})
        series = []
        for p in periods:
            ics = self.lrn._spearman_ic([o for o in obs if o["date"] == p])
            series.append(ics["momentum"]["ic"])
        mean_ic, t = self.lrn._ic_tstat(series)

        base = dict(config.FACTOR_WEIGHTS_WITH_SENTIMENT)
        after = self.lrn._drift(base, {"momentum": mean_ic},
                                confidence={"momentum": t}, baseline=base)
        self.assertGreater(after["momentum"], base["momentum"],
                           "momentum predicted returns but its weight did not rise")
        self.assertGreater(after["momentum"] - base["momentum"], 0.002,
                           "weight moved by less than 0.2pp — too small to matter")

    def test_no_edge_world_moves_nothing(self):
        """The most important negative case: pure noise must not teach anything."""
        obs = make_observations(weeks=20, momentum_edge=0.0)
        periods = sorted({o["date"] for o in obs})
        fired = []
        for f in ("momentum", "valuation", "sentiment"):
            series = []
            for p in periods:
                ics = self.lrn._spearman_ic([o for o in obs if o["date"] == p])
                if f in ics:
                    series.append(ics[f]["ic"])
            _, t = self.lrn._ic_tstat(series)
            if abs(t) >= self.lrn.IC_TSTAT_MIN:
                fired.append(f)
        self.assertEqual(fired, [],
                         f"learner found 'edges' in a world with none: {fired}")

    def test_short_history_never_qualifies(self):
        """Even a strong true edge must wait for enough weeks."""
        obs = make_observations(weeks=3, momentum_edge=2.0)
        periods = sorted({o["date"] for o in obs})
        series = [self.lrn._spearman_ic([o for o in obs if o["date"] == p])["momentum"]["ic"]
                  for p in periods]
        self.assertLess(len(periods), self.lrn.MIN_PERIODS,
                        "test setup no longer exercises the short-history path")
        # The period guard in run() blocks this regardless of t-stat.
        self.assertLess(len(series), self.lrn.MIN_PERIODS)


class TestInvertedFactorDirection(unittest.TestCase):
    """Volatility is SUBTRACTED in scoring, so its drift must invert."""

    def setUp(self):
        self.lrn = _load_learning()

    def test_volatility_drift_is_inverted(self):
        base = dict(config.FACTOR_WEIGHTS_WITH_SENTIMENT)
        # Positive IC on volatility = high-vol stocks outperformed = the
        # volatility PENALTY was wrong = its weight should FALL.
        after = self.lrn._drift(base, {"volatility": 0.05},
                                confidence={"volatility": 3.0}, baseline=base)
        self.assertLess(after["volatility"], base["volatility"],
                        "volatility weight rose when high-vol stocks outperformed — "
                        "the inversion for subtracted factors is broken")


if __name__ == "__main__":
    unittest.main(verbosity=2)
