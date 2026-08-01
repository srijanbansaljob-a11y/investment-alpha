"""
tests/test_architecture.py — Enforces docs/ARCHITECTURE.md.

test_risk_paths.py asks "does the logic work?". This file asks "is the system
still shaped the way we decided?" — the question nobody was asking while three
stop mechanisms and four sell paths accumulated.

These are static checks over source text. They are deliberately blunt: a test
that occasionally needs its allowlist updated is far cheaper than architecture
drift nobody notices for two months.

If a test here fails, the fix is usually NOT to edit the test. It's to route
your change through the existing owner (docs/ARCHITECTURE.md §1).
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

PY_DIRS = ["pipeline", "broker", "strategies", "scripts"]


def _py_files():
    for d in PY_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            if "__pycache__" not in p.parts:
                yield p


def _src(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _code_lines_from_text(text):
    """Same filtering as _code_lines, over a string rather than a file."""
    out = []
    in_doc = False
    for line in text.splitlines():
        s = line.strip()
        if s.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or s.startswith("#") or s.startswith('"""'):
            continue
        code = line.split("#", 1)[0]
        code = re.sub(r'"[^"]*"', '""', code)
        code = re.sub(r"'[^']*'", "''", code)
        out.append(code)
    return out


def _code_lines(path):
    """
    Source lines with comments, docstrings, and STRING LITERAL contents
    dropped — so a log message that merely mentions `close_position(...)`
    isn't mistaken for a call to it.
    """
    out = []
    in_doc = False
    for line in _src(path).splitlines():
        s = line.strip()
        if s.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or s.startswith("#") or s.startswith('"""'):
            continue
        code = line.split("#", 1)[0]
        code = re.sub(r'"[^"]*"', '""', code)
        code = re.sub(r"'[^']*'", "''", code)
        out.append(code)
    return out


class TestSingleOwnerSell(unittest.TestCase):
    """ARCHITECTURE §1: sell_with_cleanup is the ONE sell path."""

    # The owner itself, plus the low-level primitive it wraps.
    ALLOWED = {"broker/alpaca_client.py"}

    def test_no_direct_close_position_calls(self):
        offenders = []
        for p in _py_files():
            rel = p.relative_to(ROOT).as_posix()
            if rel in self.ALLOWED:
                continue
            for i, line in enumerate(_code_lines(p), 1):
                if re.search(r"(?<!def )close_position\s*\(", line) and "sell_with_cleanup" not in line:
                    offenders.append(f"{rel}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], "Direct close_position() call — route it through "
                                        "sell_with_cleanup() (ARCHITECTURE §1)\n" + "\n".join(offenders))

    def test_no_raw_sell_orders_outside_owner(self):
        offenders = []
        for p in _py_files():
            rel = p.relative_to(ROOT).as_posix()
            if rel in self.ALLOWED:
                continue
            for i, line in enumerate(_code_lines(p), 1):
                if re.search(r"place_market_order\([^)]*[\"']sell[\"']", line):
                    offenders.append(f"{rel}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], "Raw sell order outside the owner (ARCHITECTURE §1)\n"
                                        + "\n".join(offenders))


class TestSingleOwnerStops(unittest.TestCase):
    """ARCHITECTURE §1: reconcile_protective_stops owns protective stops."""

    ALLOWED = {"broker/stop_loss.py",             # the owner
               "broker/alpaca_client.py",         # the primitive
               "scripts/protect_positions.py",    # owner-run preview tool
               "scripts/force_stop_test.py"}      # deliberate UAT drill (owner-run)

    def test_place_stop_order_only_in_owner(self):
        offenders = []
        for p in _py_files():
            rel = p.relative_to(ROOT).as_posix()
            if rel in self.ALLOWED:
                continue
            for i, line in enumerate(_code_lines(p), 1):
                if "place_stop_order(" in line:
                    offenders.append(f"{rel}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], "Stop placed outside reconcile_protective_stops "
                                        "(ARCHITECTURE §1)\n" + "\n".join(offenders))

    def test_no_bracket_orders_on_entry(self):
        """ARCHITECTURE §6: brackets are a deliberate non-goal."""
        offenders = []
        for p in _py_files():
            rel = p.relative_to(ROOT).as_posix()
            if rel == "broker/alpaca_client.py":
                continue   # the capability may exist; nothing may USE it
            for i, line in enumerate(_code_lines(p), 1):
                if re.search(r"take_profit_price\s*=|OrderClass\.BRACKET", line):
                    offenders.append(f"{rel}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], "Bracket order re-introduced (ARCHITECTURE §6)\n"
                                        + "\n".join(offenders))


class TestNoBlanketCancel(unittest.TestCase):
    """ARCHITECTURE §1: cancel_orders_for_symbol, never account-wide cancel."""

    def test_no_blanket_cancel_calls(self):
        offenders = []
        for p in _py_files():
            rel = p.relative_to(ROOT).as_posix()
            if rel == "broker/alpaca_client.py":
                continue
            for i, line in enumerate(_code_lines(p), 1):
                if "cancel_open_orders(" in line or re.search(r"\.cancel_orders\(\)", line):
                    offenders.append(f"{rel}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], "Blanket order cancel — kills every protective stop "
                                        "(ARCHITECTURE §1)\n" + "\n".join(offenders))


class TestSingleSourceOfTruth(unittest.TestCase):
    """ARCHITECTURE §1: one cap table, one regime vocabulary, no hardcoding."""

    def test_exposure_caps_derived_not_duplicated(self):
        for k_4tier, k_3tier in [("STRONG BULL", "bull"), ("MOD BULL", "bull"),
                                 ("NEUTRAL", "neutral"), ("BEARISH", "bear")]:
            self.assertEqual(config.MAX_INVESTED_PCTS[k_4tier],
                             config.PIPELINE_MAX_INVESTED_PCT[k_3tier],
                             f"{k_4tier} drifted from PIPELINE_MAX_INVESTED_PCT[{k_3tier}] "
                             "— caps must be DERIVED, not maintained twice")

    def test_no_duplicate_config_keys(self):
        import collections
        names = collections.Counter(
            re.findall(r"^([A-Z_][A-Z0-9_]+)\s*=", _src(ROOT / "config.py"), re.M))
        dups = {k: v for k, v in names.items() if v > 1}
        self.assertEqual(dups, {}, f"Duplicate (silently last-wins) config keys: {dups}")

    def test_exposure_percentages_not_hardcoded_in_messages(self):
        """A cap printed as a literal goes stale the moment config changes."""
        offenders = []
        pattern = re.compile(r"[\"'][^\"']*\b(60|75|80|95)%\s*(of equity|invested|cap)")
        for p in _py_files():
            for i, line in enumerate(_code_lines(p), 1):
                if pattern.search(line):
                    offenders.append(f"{p.relative_to(ROOT).as_posix()}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], "Hardcoded exposure % in a user-facing string — "
                                        "read it from config at send time (ARCHITECTURE §4)\n"
                                        + "\n".join(offenders))


class TestCapitalAllocation(unittest.TestCase):
    """ARCHITECTURE §3: a second strategy needs its own partitioned budget."""

    def test_mr_sleeve_paused_until_carve_out_exists(self):
        src = _src(ROOT / "strategies" / "mean_reversion.py")
        gates_on_own_budget = "MR_SLEEVE_PCT" in src and re.search(
            r"sleeve_invested|sleeve_value|_sleeve_exposure", src)
        if not gates_on_own_budget:
            self.assertFalse(
                getattr(config, "MR_ENABLED", False),
                "MR_ENABLED=True but the sleeve still gates on ACCOUNT-WIDE exposure "
                "instead of its own MR_SLEEVE_PCT budget. Give it a real carve-out "
                "first — see ARCHITECTURE §3. Flipping the flag alone reintroduces "
                "the bug where the sleeve either never trades or eats the "
                "pipeline's headroom.")


class TestRiskGatesHaveAnOwner(unittest.TestCase):
    """
    ARCHITECTURE §1: a declared risk control must be enforced by a component
    that actually runs.

    This test exists because of a real near-miss on 2026-07-31: the drawdown
    pause lived ONLY inside the mean-reversion sleeve. Pausing the sleeve left
    config declaring an 8% circuit breaker that no code path read — a risk
    control that existed on paper only. Any gate config declares must be
    enforced by the executor (the thing that places orders).
    """

    def test_drawdown_pause_enforced_by_executor(self):
        src = _src(ROOT / "broker" / "executor.py")
        self.assertIn("DRAWDOWN_PAUSE_PCT", src,
                      "DRAWDOWN_PAUSE_PCT is declared in config but the executor "
                      "doesn't read it — the circuit breaker is enforced NOWHERE "
                      "(ARCHITECTURE §1)")
        self.assertIn("_check_drawdown_pause", src)

    def test_exposure_cap_enforced_by_executor(self):
        src = _src(ROOT / "broker" / "executor.py")
        self.assertIn("PIPELINE_MAX_INVESTED_PCT", src,
                      "Executor no longer enforces the exposure cap — it would buy "
                      "until cash ran out (ARCHITECTURE §1)")

    def test_declared_gates_are_not_orphaned_in_paused_strategies(self):
        """Any risk knob in config must be read by something that runs."""
        paused = not getattr(config, "MR_ENABLED", False)
        if not paused:
            return
        mr_src = _src(ROOT / "strategies" / "mean_reversion.py")
        live_src = "\n".join(_src(p) for p in _py_files()
                             if "mean_reversion" not in p.name)
        for knob in ("DRAWDOWN_PAUSE_PCT", "DRAWDOWN_RESUME_PCT"):
            if knob in mr_src:
                self.assertIn(knob, live_src,
                              f"{knob} is only read by the PAUSED mean-reversion sleeve — "
                              "the control it describes is not enforced anywhere "
                              "(ARCHITECTURE §1)")


class TestKillSwitchIsWired(unittest.TestCase):
    """
    ARCHITECTURE §2.2: the kill switch must stop the thing that places orders.

    Found 2026-08-01 while preparing the UAT drill: `/pausetrading` wrote the
    KV key `trading_paused`, and NOTHING in the Python order path read it. The
    executor checked only EXECUTION_ENABLED and `execution_lock` — a different
    key for concurrency. Pressing the documented kill switch and then running
    the pipeline would have placed every order.
    """

    def test_executor_reads_the_pause_flag(self):
        src = _src(ROOT / "broker" / "executor.py")
        self.assertIn("is_trading_paused", src,
                      "executor does not read the /pausetrading kill switch — "
                      "the switch stops nothing (ARCHITECTURE §2.2)")

    def test_pause_check_distinguishes_unknown_from_running(self):
        src = _src(ROOT / "broker" / "kv_lock.py")
        self.assertIn("def is_trading_paused", src)
        self.assertIn("return None", src.split("def is_trading_paused")[1][:1600],
                      "is_trading_paused must return None when it cannot verify, so "
                      "callers can distinguish 'confirmed running' from 'unknown'")

    def test_worker_and_python_use_the_same_key(self):
        worker = _src(ROOT / "worker" / "index.js")
        kv     = _src(ROOT / "broker" / "kv_lock.py")
        self.assertIn("trading_paused", worker)
        self.assertIn("trading_paused", kv,
                      "Python reads a different KV key than /pausetrading writes")


class TestStopLevelsComeFromBroker(unittest.TestCase):
    """ARCHITECTURE §1/§4: display the level that will actually fire."""

    def test_user_facing_surfaces_read_resting_stops(self):
        for rel in ["broker/monitor.py", "broker/remote_commands.py"]:
            src = _src(ROOT / rel)
            self.assertTrue(
                "get_resting_stops" in src or "_resting_stops" in src,
                f"{rel} no longer reads resting broker stops — it would show a "
                "recomputed level that disagrees with what Alpaca will execute "
                "(ARCHITECTURE §4)")

    def test_every_command_showing_a_stop_reads_the_broker(self):
        """
        Per-command check. Module-level presence isn't enough: on 2026-08-01
        cmd_status still recomputed entry-anchored levels while _stoploss_scan
        next door read the broker, so /status quietly reported stops BELOW the
        real ones for APA, DAL, HST and TRV.
        """
        src = _src(ROOT / "broker" / "remote_commands.py")
        for cmd in ("cmd_status", "cmd_stoploss_check", "cmd_stoploss_execute"):
            marker = f"def {cmd}("
            if marker not in src:
                continue
            body = src.split(marker)[1].split("\ndef ")[0]
            if "stop" not in body.lower():
                continue
            self.assertTrue(
                "_resting_stops" in body or "_stoploss_scan" in body,
                f"{cmd} displays a stop level without reading the resting broker "
                "order — it will drift below reality once stops ratchet up "
                "(ARCHITECTURE §4)")


class TestCloudParity(unittest.TestCase):
    """ARCHITECTURE §2.3: nothing critical may live only in gitignored paths."""

    def test_durable_ledger_not_gitignored(self):
        ignored = _src(ROOT / ".gitignore")
        self.assertNotIn("data/portfolio_state.json", ignored,
                         "The durable ledger is gitignored — cloud runs go stateless "
                         "and stop generating EXIT signals (this was audit P0-3)")

    def test_signals_prefers_live_broker_positions(self):
        src = _src(ROOT / "pipeline" / "signals.py")
        self.assertIn("_load_live_holdings", src,
                      "signals.py no longer derives holdings from Alpaca (ARCHITECTURE §1)")


class TestNoAutoExecution(unittest.TestCase):
    """ARCHITECTURE §2.2: nothing trades without an explicit human action."""

    def test_monitor_does_not_auto_execute(self):
        src = _src(ROOT / "broker" / "monitor.py")
        body = src.split("def process_auto_executions")[1].split("\ndef ")[0]
        code = [l for l in _code_lines_from_text(body) if l.strip()]
        self.assertTrue(any(l.strip() == "return []" for l in code),
                        "monitor.process_auto_executions must stay a no-op — silent "
                        "auto-selling contradicts the approval-first design "
                        "(ARCHITECTURE §2.2)")
        for banned in ("submit_order", "close_position(", "sell_with_cleanup("):
            self.assertNotIn(banned, "\n".join(code),
                             f"process_auto_executions places orders again ({banned}) — "
                             "this is the auto-sell countdown that was deliberately removed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
