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

    def test_pause_is_rechecked_before_the_first_order(self):
        """
        Cloudflare KV is eventually consistent. Measured 2026-08-02: a
        /pausetrading write was invisible to the GitHub Actions runner for
        ~3 minutes. A pipeline run takes several minutes, so checking only at
        the start means a pause pressed during the run is honoured on the NEXT
        run — after this one has already traded.
        """
        src = _src(ROOT / "broker" / "executor.py")
        self.assertGreaterEqual(
            src.count("is_trading_paused"), 2,
            "the kill switch is read only once, at run start — a pause pressed "
            "while the pipeline is scoring would not stop this run's orders")
        # The re-check must sit between the BUY header and the buy loop itself,
        # so it runs after scoring (minutes of wall clock) and before any order.
        buy_section = src.split("[BUY] Processing")[1].split("for sig in buy_signals:")[0]
        self.assertIn("is_trading_paused", buy_section,
                      "no pause re-check between the BUY stage and the order loop")

    def test_blocked_run_is_not_reported_as_executed(self):
        """
        Observed live 2026-08-02: with trading paused, /pipeline mode:execute
        replied "🚀 Pipeline — EXECUTED · Execution complete — see what actually
        happened" and listed "EXIT MATX — submitted, fill NOT confirmed".
        Zero orders reached Alpaca. The report claimed action that never
        occurred — the same class of bug as a control that claims to work and
        doesn't, only inverted.
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location("rc", ROOT / "broker" / "remote_commands.py")
        rc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rc)
        summary = {"regime": {"label": "BULL"}, "dry_run": True,
                   "orders": [{"ticker": "MATX", "action": "EXIT", "status": "dry_run"},
                              {"ticker": "SWK", "action": "BUY", "status": "dry_run"}]}
        rendered = " ".join(f["name"] + " " + str(f["value"])
                            for f in rc._format_pipeline_fields(summary, execute=True))
        self.assertIn("NO ORDERS PLACED", rendered,
                      "a blocked run does not say so")
        self.assertNotIn("submitted", rendered,
                         "a blocked run still describes orders as 'submitted'")

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

    def test_kill_switch_does_not_expire(self):
        """A kill switch that un-presses itself is worse than none.
        The worker set expirationTtl: 7 days — pause silently lifted after a
        week, with no notification."""
        worker = _src(ROOT / "worker" / "index.js")
        block = worker.split('KV.put("trading_paused"')[1][:300]
        self.assertNotIn("expirationTtl", block,
                         "trading_paused has a TTL — the pause will expire on its own "
                         "(ARCHITECTURE §2.2)")

    def test_trading_workflows_pass_cf_credentials(self):
        """
        The executor can only honour /pausetrading if the workflow gives it
        Cloudflare credentials. Found 2026-08-01: command.yml and
        pipeline_scheduled.yml — the two workflows that place orders — passed
        none, so the kill switch was inert in the cloud even after being wired.
        """
        for wf in ("command.yml", "pipeline_scheduled.yml"):
            src = _src(ROOT / ".github" / "workflows" / wf)
            for var in ("CF_ACCOUNT_ID", "CF_KV_NAMESPACE", "CF_API_TOKEN"):
                self.assertIn(var, src,
                              f"{wf} does not pass {var} — the /pausetrading kill "
                              "switch cannot be verified and execution proceeds")

    def test_kill_switch_covers_every_auto_execution_path(self):
        """
        A kill switch must stop EVERYTHING that can trade without a human.
        There are two such paths in the worker: the take-profit auto-seller
        (cron) and the TradingView webhook. On 2026-08-01 the webhook checked
        nothing, while the /pausetrading message claimed to cover it.
        """
        worker = _src(ROOT / "worker" / "index.js")
        # Split on the DEFINITION, not the router's call site (which appears
        # later in the file and would yield an empty body).
        marker = "async function handleTradingViewWebhook"
        self.assertIn(marker, worker, "webhook handler not found")
        body = worker.split(marker, 1)[1].split("\nasync function ")[0]
        self.assertIn("trading_paused", body,
                      "TradingView webhook does not check the kill switch — an "
                      "external alert could trade while trading is paused "
                      "(ARCHITECTURE §2.2)")
        # There must be at least two independent readers: cron TP monitor + webhook
        self.assertGreaterEqual(
            worker.count('KV.get("trading_paused")'), 2,
            "fewer kill-switch checks than auto-execution paths")

    def test_status_surfaces_pause_state(self):
        src = _src(ROOT / "broker" / "remote_commands.py")
        body = src.split("def cmd_status(")[1].split("\ndef ")[0]
        self.assertIn("is_trading_paused", body,
                      "/status does not show whether trading is paused — the kill "
                      "switch state is unobservable (ARCHITECTURE §4)")


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


class TestPerformanceReportingIsReal(unittest.TestCase):
    """
    ARCHITECTURE §4: numbers shown to the user must come from the broker.

    Until 2026-08-01 performance_tracker simulated a EUR 1,000 portfolio —
    total_capital fell back to 1000 because config.TOTAL_CAPITAL never
    existed, share counts were 1000 x weight / entry_price, and realised P&L
    came from a log that broker-side stops never write to. It reported
    "EUR 990.65, alpha -4.14%" for a $113,884 account.
    """

    def test_snapshot_reads_the_broker(self):
        src = _src(ROOT / "pipeline" / "performance_tracker.py")
        self.assertIn("get_account_summary", src,
                      "performance tracker does not read real account equity")
        self.assertIn("get_positions", src,
                      "performance tracker does not read real broker positions")

    def test_no_hardcoded_starting_capital(self):
        src = _src(ROOT / "pipeline" / "performance_tracker.py")
        self.assertNotIn('getattr(config, "TOTAL_CAPITAL", 1000)', src,
                         "starting capital falls back to a hardcoded 1000 — the "
                         "report describes a portfolio that does not exist")

    def test_rolling_metrics_exclude_other_baselines(self):
        """Sharpe/drawdown across a baseline change reads a unit switch as a
        return. That produced a fabricated Sharpe of 1.537."""
        src = _src(ROOT / "pipeline" / "performance_tracker.py")
        self.assertIn("baseline_date", src,
                      "snapshots are not tagged with their baseline, so rolling "
                      "metrics can mix incomparable series")

    def test_report_currency_matches_account(self):
        body = _src(ROOT / "pipeline" / "performance_tracker.py")
        report = body.split("def print_report")[1].split("\ndef ")[0]
        self.assertNotIn("€{snapshot", report,
                         "report hardcodes EUR while the account is USD")


class TestEvidenceThresholds(unittest.TestCase):
    """
    ARCHITECTURE §4: a claim must be backed by evidence that actually exists.

    All three of these reported confidently on 2026-08-01:
      - the learner "evaluated 30 observations" (10 stocks x 3 weeks = 3 facts)
        and drifted the weights that choose stocks;
      - factor analysis reported a "-92pp edge" for signals present on ZERO
        trades, under a footer advising weight tuning;
      - a "92% win rate" that was mostly a screener account wind-down.
    """

    def test_learner_counts_independent_periods(self):
        src = _src(ROOT / "pipeline" / "learning.py")
        self.assertIn("MIN_PERIODS", src,
                      "learner has no independent-period guard — rows within one "
                      "week are correlated and must not count as separate evidence")
        self.assertIn("n_periods", src)
        import importlib.util
        spec = importlib.util.spec_from_file_location("lrn", ROOT / "pipeline" / "learning.py")
        lrn = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lrn)
        self.assertGreaterEqual(lrn.MIN_PERIODS, 8,
                                "fewer than ~8 weekly snapshots cannot distinguish a "
                                "factor from a week's weather")

    def test_factor_analysis_refuses_zero_sample_edges(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fa", ROOT / "scripts" / "factor_analysis.py")
        fa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fa)
        # A signal present on no trades must not produce an edge.
        d = fa._signal_comparison(
            [{"signals": {}, "win": True, "pnl_pct": 5.0} for _ in range(13)],
            "insider_buy")
        self.assertEqual(d["with_n"], 0)
        self.assertIsNone(d["edge"],
                          "edge computed from an empty group — this produced the "
                          "bogus '-92pp' verdict against insider signals")
        self.assertFalse(d["sufficient"])

    def test_only_one_module_writes_factor_weights(self):
        """
        ARCHITECTURE §1: factor weights have exactly one owner.

        learning.py calls itself "v2 of the feedback loop", but v1 was never
        retired — BOTH wrote data/learned_weights.json, the file scoring.py
        prefers over config, with incompatible rules:
          learning.py  >=12 weekly cross-sections, |t|>=2.5, +/-6pp leash
          feedback.py  25 position-months, no significance test, no leash
        feedback.py runs on every --execute and learning.py weekly, so the
        weaker rule could silently overwrite the stronger one.
        """
        fb = _code_lines(ROOT / "pipeline" / "feedback.py")
        offenders = [f"feedback.py:{i}: {l.strip()}"
                     for i, l in enumerate(fb, 1)
                     if "LEARNED_WEIGHTS_FILE.write" in l]
        self.assertEqual(offenders, [],
                         "feedback.py writes the factor weights owned by "
                         "learning.py\n" + "\n".join(offenders))

    def test_no_scipy_dependency_anywhere_in_pipeline(self):
        """scipy is not installed locally; importing it made the feedback loop
        run in the cloud and fail on the owner's machine every run."""
        offenders = []
        for p in _py_files():
            for i, line in enumerate(_code_lines(p), 1):
                if "scipy" in line:
                    offenders.append(f"{p.relative_to(ROOT).as_posix()}:{i}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "scipy imported — creates a cloud/local divergence\n"
                         + "\n".join(offenders))

    def test_learned_weights_not_gitignored(self):
        ignored = _src(ROOT / ".gitignore")
        active = [l.strip() for l in ignored.splitlines()
                  if l.strip() and not l.strip().startswith("#")]
        self.assertNotIn("data/learned_weights.json", active,
                         "learned weights are gitignored — the cloud would score "
                         "with drifted weights while local runs use config defaults "
                         "(ARCHITECTURE §2.3)")

    def test_decision_review_denominator_is_scoreable_only(self):
        src = _src(ROOT / "pipeline" / "postmortem.py")
        self.assertIn('"scored"', src,
                      "decision review divides wins by ALL reviewed decisions, "
                      "including buy approvals that can never be scored — that is "
                      "how '4/18' appeared next to 9 listed decisions")

    def test_shadow_records_broad_cross_section(self):
        """
        The learner needs stocks the model DISLIKED. Recording only the
        post-filter shortlist meant 10 near-identical names and an IC that
        measured nothing.
        """
        src = _src(ROOT / "pipeline" / "shadow.py")
        self.assertIn("scored_result", src,
                      "shadow.record still samples the filtered shortlist only")
        main = _src(ROOT / "main.py")
        self.assertIn("scored_result=score_result", main,
                      "main.py does not pass the scored universe to shadow.record")

    def test_shadow_uses_fixed_horizon(self):
        src = _src(ROOT / "pipeline" / "shadow.py")
        self.assertIn("HORIZON_DAYS", src,
                      "shadow evaluation has no fixed horizon — returns spanned "
                      "38-51 days and were correlated as if comparable")
        # Check CODE only: the comment explaining the old bug legitimately
        # contains the old call, and matching raw text flags it as a relapse.
        code = "\n".join(_code_lines_from_text(src))
        self.assertNotIn("yf.download(tickers, period=", code,
                         "still measuring 'snapshot to today' instead of a fixed window")
        self.assertIn("start=start.isoformat()", code,
                      "evaluation does not anchor to the snapshot date")

    def test_learner_has_no_scipy_dependency(self):
        src = _src(ROOT / "pipeline" / "learning.py")
        self.assertNotIn("from scipy", src,
                         "learner imports scipy, which is not installed — it would "
                         "crash the moment it had enough data")
        self.assertIn("def _spearman", src)

    def test_learner_tests_significance_across_periods(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("lrn", ROOT / "pipeline" / "learning.py")
        lrn = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lrn)
        # A consistent small edge should register; noise should not.
        _, t_real  = lrn._ic_tstat([0.05, 0.06, 0.04, 0.05, 0.07, 0.05,
                                    0.04, 0.06, 0.05, 0.05, 0.06, 0.04])
        _, t_noise = lrn._ic_tstat([0.30, -0.28, 0.25, -0.31, 0.29, -0.27,
                                    0.26, -0.30, 0.28, -0.25, 0.27, -0.29])
        self.assertGreater(abs(t_real), lrn.IC_TSTAT_MIN,
                           "a consistent edge fails the significance bar — the guard "
                           "is so strict it is an off switch")
        self.assertLess(abs(t_noise), lrn.IC_TSTAT_MIN,
                        "large but sign-flipping ICs pass as skill")

    def test_drift_step_is_material_when_evidence_qualifies(self):
        """
        A guard that passes but does nothing is an off switch in disguise.
        The step used to be scaled by the raw IC (~0.04), so a factor that
        cleared significance moved its weight 0.02pp/week — 28.0% to 28.0%.
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location("lrn", ROOT / "pipeline" / "learning.py")
        lrn = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lrn)
        base = dict(config.FACTOR_WEIGHTS_WITH_SENTIMENT)
        after = lrn._drift(base, {"momentum": 0.04},
                           confidence={"momentum": lrn.IC_TSTAT_MIN}, baseline=base)
        step = abs(after["momentum"] - base["momentum"])
        self.assertGreater(step, 0.002,
                           "qualifying evidence moves the weight by less than "
                           "0.2pp — the learner cannot act even when it is right")

    def test_drift_is_leashed_to_baseline(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("lrn", ROOT / "pipeline" / "learning.py")
        lrn = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lrn)
        base = dict(config.FACTOR_WEIGHTS_WITH_SENTIMENT)
        w = dict(base)
        for _ in range(200):    # a sustained false positive, forever
            w = lrn._drift(w, {"valuation": -0.07},
                           confidence={"valuation": -3.0}, baseline=base)
        drift = abs(w["valuation"] - base["valuation"])
        self.assertLessEqual(round(drift, 4), lrn.MAX_DRIFT_FROM_BASELINE + 1e-6,
                             "a sustained false positive can drag a weight "
                             "arbitrarily far from the chosen baseline")

    def test_signal_enrichment_uses_pipeline_not_screener(self):
        """
        Every trade was logged regime=UNKNOWN with all signals False, because
        _get_signals read screener/daily_sentiment_data.json — a file the
        pipeline does not produce and which does not exist. Factor analysis
        therefore had no explanatory variables at all.
        """
        src = _src(ROOT / "scripts" / "trade_outcome_logger.py")
        code = "\n".join(_code_lines_from_text(src))
        self.assertIn("_SHADOW_LOG", code,
                      "signal enrichment does not read the pipeline's shadow log")
        self.assertNotIn("_SCREENER_DATA)", code,
                         "still reading the retired screener data file")

    def test_signals_are_captured_as_of_entry(self):
        """Scores must reflect what the model saw when it BOUGHT, not today."""
        src = _src(ROOT / "scripts" / "trade_outcome_logger.py")
        self.assertIn("def _shadow_snapshot_for", src)
        self.assertIn("entry_date", src.split("def _get_signals")[1][:400],
                      "_get_signals ignores entry date — it would attribute "
                      "today's scores to a trade opened weeks ago")

    def test_universe_has_no_known_dead_tickers(self):
        dead = {"BK", "CTRA", "HOLX", "SEE", "CFLT", "EXAS"}
        still = dead & set(config.ALL_TICKERS)
        self.assertEqual(still, set(),
                         f"delisted tickers back in the universe: {still} — they "
                         "fail on every run and bury real errors in log noise")

    def test_performance_report_separates_open_from_closed(self):
        src = _src(ROOT / "pipeline" / "performance_tracker.py")
        self.assertIn("positions_in_profit", src,
                      "unrealised open-position count is still labelled 'win rate'")
        self.assertIn("_closed_trade_stats", src,
                      "no real win rate sourced from closed model trades")


class TestScreenerStaysRetired(unittest.TestCase):
    """
    The screener was retired 2026-08-03 in the order the audit prescribed:
    liquidate the account, make the pipeline self-sufficient for the KV data
    the Worker needs, remove the surfaces, then delete. These guard the last
    step from being quietly undone.
    """

    def test_no_live_code_imports_screener(self):
        offenders = []
        for p in _py_files():
            rel = p.relative_to(ROOT).as_posix()
            if rel.startswith("screener/"):
                continue
            for i, line in enumerate(_code_lines(p), 1):
                if "from screener" in line or "import screener" in line:
                    offenders.append(f"{rel}:{i}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "live code imports the retired screener\n" + "\n".join(offenders))

    def test_kv_publisher_exists(self):
        """Deleting the screener without this breaks /brief, the webhook regime
        gate and /buy stop targets — the exact failure the audit warned of."""
        self.assertTrue((ROOT / "scripts" / "publish_pipeline_kv.py").exists(),
                        "the pipeline KV publisher is missing; the Worker's "
                        "regime_signal / stock_buckets / pipeline_summary keys "
                        "would go stale")

    def test_screener_command_deregistered(self):
        src = _src(ROOT / "scripts" / "register_discord_commands.py")
        code = "\n".join(_code_lines_from_text(src))
        self.assertNotIn('"name": "screener"', code,
                         "/screener is still registered but its data source is gone")

    def test_screener_request_is_refused_not_redirected(self):
        """
        The sharpest hazard of the retirement. get_client("screener") used to
        fall back to PIPELINE credentials when the screener keys were missing.
        Once those secrets were deleted (2026-08-03), any surviving caller
        asking for the retired account would have been handed the LIVE one —
        a sell meant for a dormant book hitting the real one. Refuse, never
        substitute.
        """
        from broker.alpaca_client import get_client
        for bad in ("screener", "SCREENER", " Screener "):
            with self.assertRaises(ValueError, msg=f"get_client({bad!r}) did not refuse"):
                get_client(bad)

    def test_worker_has_no_screener_credential_fallback(self):
        # Check the actual property ACCESS, not the bare name: the file's
        # comments legitimately explain the removed fallback, and Python-style
        # comment stripping does not understand JavaScript's `//`.
        src = _src(ROOT / "worker" / "index.js")
        offenders = [f"line {i}: {l.strip()}" for i, l in enumerate(src.splitlines(), 1)
                     if "env.ALPACA_KEY_SCREENER" in l or "env.ALPACA_SECRET_SCREENER" in l]
        self.assertEqual(offenders, [],
                         "worker still reads screener credentials — with those "
                         "secrets deleted it would silently use the live account\n"
                         + "\n".join(offenders))

    def test_health_check_is_pipeline_only(self):
        src = _src(ROOT / "scripts" / "health_check.py")
        code = "\n".join(_code_lines_from_text(src))
        self.assertNotIn('"both"', code,
                         "health check still loops over a retired second account")


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
