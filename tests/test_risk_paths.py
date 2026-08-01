"""
tests/test_risk_paths.py — Regression tests for the 2026-07-27 audit fixes.

Every test here corresponds to a numbered audit finding (docs/
FABLE_AUDIT_2026-07-27.md). These are the paths that place, cancel, or skip
orders — the ones where a silent regression costs money. Run:

    python -m unittest discover -s tests -v

No network, no Alpaca calls: everything external is monkeypatched.
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import config                                    # noqa: E402
from broker.executor import (                    # noqa: E402
    _reconcile_signals, _blocked_from_buying, calc_shares,
)
import broker.stop_loss as stop_loss             # noqa: E402
import broker.alpaca_client as ac                # noqa: E402


class TestReconcileGuards(unittest.TestCase):
    """Audit P0-4: reconciler must not defeat blackout/cooldown."""

    def test_earnings_blocked_hold_not_upgraded(self):
        sigs = [{"ticker": "XYZ", "action": "HOLD", "weight": 0.10,
                 "earnings_blocked": True}]
        out = _reconcile_signals(sigs, live_positions={}, equity=100_000)
        self.assertEqual(out[0]["action"], "HOLD",
                         "earnings blackout bypassed by reconciler")

    def test_cooldown_blocked_hold_not_upgraded(self):
        sigs = [{"ticker": "NUE", "action": "HOLD", "weight": 0.10}]
        out = _reconcile_signals(sigs, live_positions={}, equity=100_000,
                                 cooldown_tickers={"NUE"})
        self.assertEqual(out[0]["action"], "HOLD",
                         "stopped-out name re-bought despite cooldown")

    def test_legitimate_upgrade_still_works(self):
        sigs = [{"ticker": "ABC", "action": "HOLD", "weight": 0.10}]
        out = _reconcile_signals(sigs, live_positions={}, equity=100_000)
        self.assertEqual(out[0]["action"], "BUY")

    def test_blocked_from_buying_reasons(self):
        self.assertEqual(_blocked_from_buying({"ticker": "A", "earnings_blocked": True}, set()),
                         "earnings_blackout")
        self.assertEqual(_blocked_from_buying({"ticker": "A", "cooldown_blocked": True}, set()),
                         "reentry_cooldown")
        self.assertEqual(_blocked_from_buying({"ticker": "A"}, {"A"}),
                         "reentry_cooldown_broker")
        self.assertIsNone(_blocked_from_buying({"ticker": "A"}, set()))


class TestStopMath(unittest.TestCase):
    """Stop/take-profit clamp math (verified correct in the audit — keep it so)."""

    def setUp(self):
        self._orig = stop_loss._compute_atr

    def tearDown(self):
        stop_loss._compute_atr = self._orig

    def test_quiet_stock_clamps_to_floor(self):
        stop_loss._compute_atr = lambda t, period=14: 0.30   # 0.75% raw stop
        stop, method, _ = stop_loss.compute_stop_price("QUIET", 100.0, "bull")
        self.assertAlmostEqual(stop, 100.0 * (1 - config.STOP_PCT_FLOOR), places=2)
        self.assertIn("floor", method)

    def test_wild_stock_clamps_to_cap(self):
        stop_loss._compute_atr = lambda t, period=14: 9.0    # 22.5% raw stop
        stop, method, _ = stop_loss.compute_stop_price("WILD", 100.0, "bull")
        self.assertAlmostEqual(stop, 100.0 * (1 - config.STOP_PCT_CAP), places=2)
        self.assertIn("cap", method)

    def test_take_profit_clamps(self):
        ceil_q, _, _ = stop_loss.compute_take_profit("Q", 100.0, "bull", 0.30)
        ceil_w, _, _ = stop_loss.compute_take_profit("W", 100.0, "bull", 9.0)
        self.assertAlmostEqual(ceil_q, 100.0 * (1 + config.TAKE_PROFIT_PCT_FLOOR), places=2)
        self.assertAlmostEqual(ceil_w, 100.0 * (1 + config.TAKE_PROFIT_PCT_CAP), places=2)


class TestStopReconciler(unittest.TestCase):
    """Audit P0-1/P1-1: the single stop mechanism — ratchet up, never down."""

    def setUp(self):
        self._orig = (ac.get_positions, ac.get_resting_stops,
                      ac.place_stop_order, ac.cancel_orders_for_symbol,
                      stop_loss._compute_atr)
        stop_loss._compute_atr = lambda t, period=14: 1.0    # 2.5 pt stop distance
        self.placed = []
        ac.place_stop_order = (lambda c, t, q, s, dry_run=True:
                               self.placed.append((t, q, round(s, 2))) or {"status": "dry_run"})
        ac.cancel_orders_for_symbol = lambda c, t: 0

    def tearDown(self):
        (ac.get_positions, ac.get_resting_stops,
         ac.place_stop_order, ac.cancel_orders_for_symbol,
         stop_loss._compute_atr) = self._orig

    def test_ratchet_up_on_winner_never_down_on_loser(self):
        ac.get_positions = lambda c: {
            "WIN":  {"qty": 100.0, "avg_entry_price": 50.0, "current_price": 60.0},
            "FLAT": {"qty": 50.0,  "avg_entry_price": 30.0, "current_price": 29.0},
        }
        ac.get_resting_stops = lambda c: {
            "WIN":  {"stop_price": 47.50, "qty": 100, "order_id": "x"},
            "FLAT": {"stop_price": 28.50, "qty": 50,  "order_id": "y"},
        }
        res = stop_loss.reconcile_protective_stops(client=object(), regime="bull",
                                                   dry_run=True)
        # WIN: anchor 60 → desired 57.50 > resting 47.50 → replaced upward
        self.assertTrue(any(t == "WIN" and s == 57.50 for t, q, s in self.placed))
        # FLAT: anchor 30 → desired 27.50 < resting 28.50 → kept (never lowered)
        self.assertEqual([k["ticker"] for k in res["kept"]], ["FLAT"])

    def test_unprotected_positions_get_stops(self):
        ac.get_positions = lambda c: {
            "A": {"qty": 10.0, "avg_entry_price": 100.0, "current_price": 100.0},
            "B": {"qty": 5.9,  "avg_entry_price": 20.0,  "current_price": 21.0},
        }
        ac.get_resting_stops = lambda c: {}
        res = stop_loss.reconcile_protective_stops(client=object(), regime="bull",
                                                   dry_run=True)
        self.assertEqual(len(res["placed"]), 2)
        # B: 5.9 shares → whole-share stop for 5
        self.assertIn(("B", 5), [(t, q) for t, q, s in self.placed if t == "B"])


class TestEmptyAccountIsAuthoritative(unittest.TestCase):
    """
    An empty Alpaca account must NOT be treated as "Alpaca unavailable".

    Found live 2026-07-31, immediately after a full liquidation: with 0
    positions, _load_alpaca_holdings() returned {} (falsy), the caller fell
    back to a stale state file, and the checker logged phantom stop-loss
    breaches for NEM and FDX — names the account did not hold. Those phantom
    breaches feed the re-entry cooldown, so they would have blocked legitimate
    buys on the very next run.
    """

    def setUp(self):
        self._orig = stop_loss._load_alpaca_holdings

    def tearDown(self):
        stop_loss._load_alpaca_holdings = self._orig

    def test_flat_account_returns_no_checks_not_state_file_fallback(self):
        stop_loss._load_alpaca_holdings = lambda: {}          # genuinely flat
        result = stop_loss.check_and_execute(regime="bull", dry_run=True)
        self.assertEqual(result["checked"], [],
                         "flat account fell back to the state file and evaluated "
                         "positions that are not held")
        self.assertEqual(result["triggered"], [],
                         "phantom stop-loss triggered on an empty account")

    def test_unavailable_broker_is_distinguishable_from_flat(self):
        src = (ROOT / "broker" / "stop_loss.py").read_text(encoding="utf-8")
        self.assertIn("return None", src,
                      "_load_alpaca_holdings must return None on failure so an "
                      "empty account ({}) stays distinguishable from an outage")
        self.assertIn("alpaca_holdings is not None", src,
                      "caller must branch on `is not None`, not truthiness")


class TestAdminExitsExcludedFromEvidence(unittest.TestCase):
    """
    Administrative exits (owner liquidation) must never count as model trades.

    Real incident 2026-07-31: the scheduled workflow ran the outcome logger
    from a commit predating admin tagging and recorded 7 liquidation exits as
    genuine wins (BMY +15.3%, MRK +14.1%, ...). Tagging only at insert time
    loses that race; the repair pass must be able to fix records after the fact.
    """

    def test_retag_repairs_already_logged_records(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "tol", ROOT / "scripts" / "trade_outcome_logger.py")
        tol = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tol)

        tol._admin_exits = lambda: {"BMY": {"date": "2026-07-31", "reason": "liquidation"}}
        outcomes = [
            {"ticker": "BMY", "exit_date": "2026-07-31", "pnl_pct": 15.31, "exit_type": "full"},
            {"ticker": "AAPL", "exit_date": "2026-07-31", "pnl_pct": 4.0, "exit_type": "full"},
            {"ticker": "BMY", "exit_date": "2026-06-01", "pnl_pct": 2.0, "exit_type": "full"},
        ]
        fixed = tol._retag_admin_exits(outcomes)
        self.assertEqual(fixed, 1)
        self.assertTrue(outcomes[0]["exclude_from_learning"])
        self.assertFalse(outcomes[1].get("exclude_from_learning"),
                         "unrelated ticker wrongly tagged")
        self.assertFalse(outcomes[2].get("exclude_from_learning"),
                         "same ticker on a DIFFERENT date wrongly tagged")
        self.assertEqual(tol._retag_admin_exits(outcomes), 0, "repair pass is not idempotent")

    def test_factor_analysis_drops_administrative(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fa", ROOT / "scripts" / "factor_analysis.py")
        fa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fa)
        kept = fa._model_outcomes([
            {"ticker": "A", "win": True, "exclude_from_learning": True},
            {"ticker": "B", "win": True},
        ])
        self.assertEqual([o["ticker"] for o in kept], ["B"])


class TestSizing(unittest.TestCase):
    def test_calc_shares_whole(self):
        self.assertEqual(calc_shares(11_000, 100.0), 110.0)
        self.assertEqual(calc_shares(500, 615.64), 0.0)   # too expensive → 0, surfaced by executor

    def test_executor_floors_buy_deltas(self):
        src = (ROOT / "broker" / "executor.py").read_text(encoding="utf-8")
        self.assertIn("delta_qty = float(int(delta_qty))", src,
                      "whole-share delta floor removed (audit P0-5)")


class TestNoRegressionsInSellPaths(unittest.TestCase):
    """Audit P0-1/P0-2: structural guards against re-introducing the bugs."""

    def test_executor_never_blanket_cancels(self):
        src = (ROOT / "broker" / "executor.py").read_text(encoding="utf-8")
        live_calls = [l for l in src.splitlines()
                      if re.match(r"^\s*alpaca\.cancel_open_orders", l)]
        self.assertEqual(live_calls, [],
                         "blanket cancel_open_orders back in executor (audit P0-1)")

    def test_executor_runs_stop_reconciler(self):
        src = (ROOT / "broker" / "executor.py").read_text(encoding="utf-8")
        self.assertIn("reconcile_protective_stops", src)

    def test_remote_commands_use_sell_with_cleanup(self):
        src = (ROOT / "broker" / "remote_commands.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"= close_position\(", src), [],
                         "bare close_position sell path back in remote_commands (audit P0-2)")

    def test_worker_closes_cancel_orders_first(self):
        src = (ROOT / "worker" / "index.js").read_text(encoding="utf-8")
        bare = re.findall(r"positions/\$\{ticker\}`,\s*\{\s*method:\s*\"DELETE\"", src)
        self.assertEqual(bare, [],
                         "worker DELETE /positions without cancel_orders=true (audit P0-2)")


class TestConfigIntegrity(unittest.TestCase):
    def test_no_duplicate_config_keys(self):
        src = (ROOT / "config.py").read_text(encoding="utf-8")
        import collections
        names = collections.Counter(re.findall(r"^([A-Z_][A-Z0-9_]+)\s*=", src, re.M))
        dups = {k: v for k, v in names.items() if v > 1}
        self.assertEqual(dups, {}, f"duplicate (last-wins) config keys: {dups}")

    def test_exposure_caps_single_sourced(self):
        self.assertEqual(config.MAX_INVESTED_PCTS["MOD BULL"],
                         config.PIPELINE_MAX_INVESTED_PCT["bull"])
        self.assertEqual(config.MAX_INVESTED_PCTS["NEUTRAL"],
                         config.PIPELINE_MAX_INVESTED_PCT["neutral"])
        self.assertEqual(config.MAX_INVESTED_PCTS["BEARISH"],
                         config.PIPELINE_MAX_INVESTED_PCT["bear"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
