# Investment Alpha — Full System Audit (Fable, 2026-07-27)

**Audience:** Claude Opus, tasked with executing the fixes below. Also the owner (Srijan).
**Scope:** pipeline-only future. Screener is being phased out entirely — do not invest in screener code beyond what the phase-out plan requires.
**Method:** full read of pipeline/broker/config/workflows, unit tests of risk-critical functions (run in a sandbox, results reproduced below), and read-only live verification against both Alpaca paper accounts. **No orders were placed, cancelled, or modified during this audit.**

**Ground rules for Opus:**
1. Never place/cancel/modify orders as part of implementing fixes. Scripts that touch orders must be preview-first and run by the owner.
2. Fix P0s in the order listed — several interact.
3. Every fix ships with a runnable test (the pure functions are trivially testable; see §7 for the harness pattern).
4. Do not add features. This codebase's problem is too many layers, not too few.

---

## 1. Verified live state (read-only, 2026-07-27)

| | Pipeline account | Screener account |
|---|---|---|
| Equity | $113,845 | $109,406 |
| Cash | $1,902 (98.3% invested) | **−$8,766 (108% invested, on margin)** |
| Positions | 12 (all fractional qty) | 8 (all whole, all green except VRTX −0.3%) |
| Open orders | 12 GTC SELL STOPs (one per position) | 0 — zero protection |

Stop coverage on pipeline: 98–100% of shares per position (DE only 80.5% — 0.73 fractional shares of a 3.73-share position are uncoverable; immaterial). NUE's forced-stop test order rests at $245.29 and has not yet fired.

Note the pipeline holds **12 positions against a top_n of 10**. That is not drift — it is Finding P0-3.

---

## 2. P0 findings — each one can lose money or silently remove protection

### P0-1. The next `--execute` run cancels all 12 protective stops and never puts them back
`broker/executor.py:262` → `alpaca.cancel_open_orders(client)` → `alpaca_client.py:404` `client.cancel_orders()` cancels **every** open order on the account, including the 12 resting GTC stops placed by `protect_positions.py`. Brackets are attached only to **new BUY** orders; HOLD positions get nothing re-placed (verified: `place_stop_order` is never called from executor). One press of the Discord "Execute rebalance" button and the entire book is naked again — exactly the state this week's work was meant to fix, undone silently.

**Fix:** replace the blanket cancel with per-intent cancellation: (a) before an EXIT/TRIM on ticker X, cancel only X's open orders; (b) leave other tickers' protective stops alone; (c) after all fills, run a **stop-reconciliation pass** over every held position — cancel-and-replace so each position ends the run with exactly one correct resting stop. Extract the placement logic from `scripts/protect_positions.py` into `broker/stop_loss.py` as `reconcile_protective_stops(client, regime)` and call it at the end of `execute_signals()`. This also fixes P0-2 and P1-1 structurally.

### P0-2. Every sell path is now broken by the resting stops (and none of them knows it)
Alpaca holds shares against open sell orders. With a GTC stop resting on all 12 names, any *other* sell of those shares is rejected ("insufficient qty available"). Paths that call `close_position` **without cancelling the ticker's resting stop first**:
- `broker/stop_loss.py:113` `_exit_via_alpaca` (weekly `--execute` check)
- `broker/remote_commands.py` `cmd_stoploss_execute` (~line 366) — the exact command flagged as "never UAT'd" in the prior audit; it is not merely untested, it **will fail**
- `broker/remote_commands.py` `cmd_approve_sell` (~line 768) — the approve-button on monitor alerts
- `executor.py` EXIT/TRIM paths — currently "work" only because P0-1's blanket cancel nukes the stops first

**Fix:** one helper, used by every sell path: `sell_with_cleanup(client, ticker, qty=None)` → cancel open orders for that symbol, wait for cancellation to confirm, then close/trim. Grep for every `close_position(` call site and route it through this.

### P0-3. Cloud runs are stateless → EXIT signals can never fire from the cloud
`outputs/` is gitignored, so `outputs/latest_portfolio.json` does not exist on a fresh GitHub Actions checkout. Consequences on every scheduled/Discord-triggered run (`pipeline_scheduled.yml`, `command.yml` → `/pipeline execute`):
- `signals.py` finds no prior portfolio → every selected name is BUY, and **no EXIT is ever generated** (exits come only from prior-state diff, `signals.py:257`).
- Names that fell out of the top-N are then classified by the reconciler as "manual positions" and **kept** (`MANUAL_POSITION_ACTION="keep"`).
- Net effect: the cloud pipeline can add but never remove. This is why the account holds 12 names with top_n=10. Rotation — the core of a momentum strategy — does not function except on your laptop.

**Fix (choose the Alpaca-first option, consistent with the codebase's stated philosophy):** derive "prior portfolio" from live Alpaca positions instead of a file. EXIT := held at Alpaca ∧ not in this run's selection ∧ not tagged manual. Entry price/date already comes from Alpaca (`avg_entry_price`) in stop_loss.py; do the same in signals.py. Keep the state file only as a cache of entry_dates/rationales, never as the truth. Alternative (weaker): commit the state file — but that recreates the bot-commit race the gitignore comment complains about.

### P0-4. Earnings blackout and re-entry cooldown are both defeated by the reconciler (unit-tested, confirmed)
`signals.py` downgrades a blackout BUY to HOLD; `_reconcile_signals` (`executor.py:98`) sees HOLD + not-in-Alpaca and upgrades it straight back to BUY. Tested:

```
sigs = [{"ticker":"XYZ","action":"HOLD","weight":0.10,"earnings_blocked":True}]
_reconcile_signals(sigs, live_positions={}, equity=100000)
→ action == "BUY"   # blackout bypassed
```

Cooldown has the same hole: `signals.py:232` only sets `cooldown_blocked` when the action is already BUY. A stopped-out name still in the top-N arrives as HOLD, gets upgraded to BUY by the reconciler, and `cooldown_blocked` was never set — instant re-buy of a name you were just stopped out of, the exact behaviour REENTRY_COOLDOWN_DAYS exists to prevent. (This will fire in practice: NUE's test stop will execute, NUE is still in the book/selection, and the next execute run will buy it back.)

**Fix:** in `_reconcile_signals`, before upgrading HOLD→BUY, honour `earnings_blocked` and re-check the cooldown log (`_recent_stop_exits` should move to a shared module so executor can call it). Blocked upgrades log a SKIPPED_BLACKOUT / SKIPPED_COOLDOWN status.

### P0-5. Top-up buys on existing fractional positions silently lose their bracket (unit-tested, confirmed)
All 12 live positions are fractional. Any delta-aware top-up (weight drift, reconcile) produces a fractional qty → `place_market_order` detects it and submits **without protective legs** (`alpaca_client.py:222`, warning only). Combined with P0-1, a routine rebalance leaves those shares permanently unprotected.

**Fix:** floor `delta_qty` to whole shares in the executor (`calc_shares` already floors targets; the delta arithmetic reintroduces fractions via `existing_qty`). Longer-term the cleaner design is: stop using brackets at all — buy plain market orders, then let the single stop-reconciliation pass (P0-1 fix) attach/refresh one stop per position. One mechanism instead of three.

---

## 3. P1 findings — trader's review of the algo logic

### P1-1. Three stop systems with three different answers
1. **Resting GTC stops at Alpaca** (trailed to max(entry, current) — the truth).
2. **Weekly `stop_loss.py check_and_execute`** — recomputes entry-anchored stops from `avg_entry_price`; for MRK that is $17+ below the resting stop. Its execute path would sell at levels the broker would never let it reach (and fails anyway, P0-2).
3. **`monitor.py` 15-min alerts** — also recomputes entry-anchored (`_get_stop_price`, line 233→`compute_stop_price(entry)`), so it will alert on MRK at ~$105 while the broker sells at $123.12. Alerts and reality disagree in the dangerous direction. (`remote_commands._stoploss_scan` was already fixed to read resting stops — monitor.py was not.)

**Fix:** the broker's resting order is the single truth. Monitor reads `_resting_stops()` (move it to `broker/alpaca_client.py`). The weekly job stops being a checker and becomes the **trailing pass**: recompute stop from max(entry, current), and where the new stop is higher than the resting one, cancel-and-replace. That converts the known "static stops go stale on winners" weakness (MRK +15.2%, MTCH +10.9%, EIX +11.2% are all drifting away from their stops) into the system's maintenance loop. Delete `check_and_execute`'s execute path entirely — with real stops at the broker, a local price-checking executioner is redundant and a double-sell risk.

### P1-2. The two exposure-cap systems disagree again
`PIPELINE_MAX_INVESTED_PCT` was raised to 95/75/40 (executor enforces). `MAX_INVESTED_PCTS` (80/60/40/20, screener labels) is still what `scripts/health_check.py` warns against and what the **mean-reversion sleeve** gates on (`strategies/mean_reversion.py:181`). The config comment claiming "warning and enforcement now agree" is now false: health check will warn at 60% while the executor buys to 95%, and the MR sleeve stops at 80% while trading the same account. **Fix:** one cap table, one regime taxonomy (see P1-5), everything reads it.

### P1-3. Trader's view on the parameters themselves
- **95% bull cap with 12 correlated long equities and 4 closed trades of evidence** — acceptable for paper only. The config caveat is good; make it a hard gate: add `LIVE_TRADING_BLOCKED_UNTIL_N_TRADES = 30` honored by the executor when the base URL is not paper, so the promotion decision can't be drifted into.
- **Stops 3–12% band, 2.5×ATR, multi-week holds** — internally consistent. But the floor/cap asymmetry with take-profit (8–35%) gives a theoretical worst R:R of 12% risk vs 8% reward on a high-vol name whose TP clamps to floor while stop clamps to cap. Rare but possible; consider deriving TP floor as `max(8%, 1.5 × stop_pct)`.
- **Monitor's PROFIT_TARGET_PCT=20% vs bracket TP 8–35%** — two different profit concepts alert/act at different levels. After P0-5's "no more brackets" simplification, keep the TP as a monitor alert only (a hard limit-sell ceiling on a momentum strategy amputates the right tail — with a 35% cap you'd have sold every big winner early; momentum P&L lives in the tail).
- **Inverse-vol weighting + separate 20% cap + regime top_n** — fine, standard. `calc_shares` returning 0 for a high-priced stock at small weight is silently logged "at_target" (tested: $500 target @ $615 → 0 shares); log it as SKIPPED_TOO_EXPENSIVE instead so it's visible.
- **Regime classifier** — reasonable design (VIX + 200MA + inversion/credit downgrades, NEUTRAL fallback). Static VIX thresholds (20/28) will misfire in a structurally higher-vol tape; percentile-based thresholds are the eventual upgrade, not urgent.
- **Turnover guard (rank buffer 3) + weekly cadence on a monthly-style signal** — the pipeline was designed monthly (CLAUDE.md) but runs weekly. Rank-buffer hysteresis helps, but once P0-3 restores exits, watch realized turnover: skip-month momentum has ~monthly half-life and weekly rebalancing of it mostly trades noise and pays spread. Consider: weekly run, but exits only allowed if rank > N+buffer for 2 consecutive weeks.
- **Feedback/learning** — `feedback.py` correctly locks weights until 25 observations. Verify `pipeline/learning.py` (shadow-based weekly writer of the same `learned_weights.json`) respects an equivalent guard; my grep found none. A weekly silent rewrite of factor weights from ~4 trades of ground truth is curve-fitting to noise. Until 30+ closed trades, learning should log deltas without applying them.
- **MR sleeve** — a second strategy trading the same account, gated by the stale cap table (P1-2), holding names your stop-reconciler will also see. Either give it its own tag/sub-accounting and integrate it with the stop reconciler, or pause it until the core loop is trustworthy. Recommend pause: it multiplies every P0 above.

### P1-4. Order lifecycle gaps
- `_wait_for_fill` polls 15s then gives up; "orders_unconfirmed" is surfaced but nothing follows up. Add a post-run verification step (or fold into the stop-reconcile pass): any order still non-terminal → Discord warning with order ID.
- `kv_lock` acquire/release: if the run crashes between acquire and release the lock leaks (release is only in the happy path — wrap in try/finally; confirm TTL exists in `broker/kv_lock.py`).

### P1-5. Two regime taxonomies
Pipeline: bull/neutral/bear. Screener/health/MR/worker: STRONG BULL/MOD BULL/NEUTRAL/BEARISH, mapped via a lookup. With the screener retiring, standardise on the pipeline's 3-tier everywhere (config, health_check, worker embeds, MR sleeve) and delete the mapping.

---

## 4. Screener phase-out plan (concrete, ordered, for Opus)

The prior audit's Phase A (worker defaults → pipeline) is done and verified in `worker/index.js`. Remaining coupling and kill order:

**Step 1 — liquidate the screener account (owner action, not code).** 8 positions, 7 green (+6.9% to +20.6%), account on margin at 108%. Nothing in the phase-out is blocked on code here: owner closes all 8 positions in the Alpaca UI, clearing the −$8,766 margin balance. Do this first — it is pure risk with zero remaining purpose.

**Step 2 — make the pipeline self-sufficient for the data screener currently feeds it:**
- **Regime → Cloudflare KV:** today `screener_daily.yml` runs `screener/daily_sentiment_runner.py` (2,111 lines) 3×/day partly to push regime to KV for the worker (`/brief`, buy previews). Write `scripts/publish_regime_to_kv.py` (~50 lines): call `pipeline/regime.run()`, PUT to KV via `screener/regime_to_kv.py`'s existing HTTP code (lift the function, not the module). Schedule in a new light workflow or append to `daily_summary.yml`.
- **Signal enrichment for trade outcomes:** `scripts/trade_outcome_logger.py::_get_signals()` reads `screener/daily_sentiment_data.json` — retiring it blinds factor learning for pipeline trades. Repoint it to the pipeline's own run artifacts (`data/pipeline_run_latest.json` top_holdings sub-scores, or the shadow log `data/shadow_log.json`, which already stores factor scores per ticker per week).
- **`scripts/rebalance_check.py`** fallback read of the same file → point at pipeline outputs or delete the fallback.
- **`scripts/{daily_performance,snapshot_positions,health_check}.py`** — drop the screener portfolio loop (`portfolio="both"` → `"pipeline"`).

**Step 3 — remove surfaces:** delete `/screener`, `/buy`/`/sell` `portfolio:screener` choices in `scripts/register_discord_commands.py` and the worker handlers; update `/help`; remove screener branches in `remote_commands.py` (`port_label` logic) and `alpaca_client.get_client(portfolio=...)` screener branch.

**Step 4 — delete:** `screener/` directory, `.github/workflows/screener_daily.yml`, `screener_nightly.yml`, `screener_weekly.yml`, `ALPACA_*_SCREENER` secrets (GitHub + worker + .env), `MAX_INVESTED_PCTS` 4-tier table (P1-2/P1-5 unification lands here), `Archive/stock_screener.py`.

**Step 5 — docs:** rewrite CLAUDE.md (see P2-1) so no future session re-learns the screener existed.

Order matters: 2 before 3 before 4 (the prior audit's warning stands — deleting first breaks stops enrichment and learning).

---

## 5. P2 findings — hygiene

- **P2-1. CLAUDE.md is dangerously stale.** Says 10 stocks/monthly/~$102k/portfolio list from May, "Tasks 1–3 to implement" that are long done, Python 3.14 (CI uses 3.11). Every future agent session starts by reading wrong facts. Rewrite it to describe the current system and point at `memory/AGENT_MEMORY.md` for history.
- **P2-2. Config duplicates persist.** Despite two comments claiming "duplicates removed 2026-06", 12 keys are still defined twice (tested): `LOG_LEVEL, MOMENTUM_3M/6M/12M, SMA_SHORT/LONG, RSI_PERIOD, MACD_FAST/SLOW/SIGNAL, YIELD_CURVE_ENABLED, BENCHMARK_TICKER`. Values currently agree except `LOG_LEVEL` (last-wins). Delete the second block (lines ~513–531).
- **P2-3. The repo lives inside OneDrive** (`D:\Office Transfer\OneDrive...\Investment Aplha`). The codebase carries null-byte-corruption workarounds in at least four modules because of this. Move the working clone to a non-synced path (e.g. `C:\dev\investment-alpha`); GitHub is the backup. Also fix the folder-name typo ("Aplha") while moving.
- **P2-4. Version skew:** local Python 3.14 vs CI 3.11. Pin local to 3.11 (or bump CI) — order-routing code should not run on an interpreter it's never tested on.
- **P2-5. Secrets hygiene:** `.env` and `Discord Bot dets.txt` are correctly gitignored but sit in a OneDrive-synced folder in plaintext; the `.env` also contains an `ANTHROPIC_API_KEY` that the trading system doesn't use — remove it. `AI_Agent_Conversation_Notepad.docx`, dashboards, and ~60 timestamped output files are local clutter; archive them.
- **P2-6. `pipeline/output_patch.py`** — a "patch" module alongside `output.py` is layer-cruft; merge or delete.

---

## 6. What was tested (evidence log)

| Test | Method | Result |
|---|---|---|
| Syntax/compile, all 47 .py files | `py_compile` | PASS |
| Live pipeline account state | read-only Alpaca API | 12 pos, 12 GTC stops resting, coverage 98–100% (DE 80.5%), NUE test stop armed @ $245.29 |
| Live screener account state | read-only Alpaca API | 8 pos, 0 stops, −$8,766 cash, 108% invested |
| Stop clamp math | mocked ATR, entry $100 | quiet stock → $97.00 (3% floor) ✓, wild → $88.00 (12% cap) ✓ |
| Take-profit clamp math | mocked ATR | $108.00 floor ✓ / $135.00 cap ✓ |
| Earnings-blackout bypass (P0-4) | direct call to `_reconcile_signals` | **CONFIRMED — HOLD upgraded to BUY** |
| Cooldown bypass (P0-4) | same | **CONFIRMED** |
| Fractional top-up loses bracket (P0-5) | `place_market_order` dry-run, qty 12.7963 | **CONFIRMED — submitted without legs** |
| Blanket cancel + no re-protect (P0-1) | code inspection: `cancel_orders()` unfiltered; `place_stop_order` absent from executor | **CONFIRMED** |
| Sell paths vs resting stops (P0-2) | code inspection of all 4 `close_position` call sites | **CONFIRMED — none cancels the ticker's stop first** |
| Stateless cloud runs (P0-3) | `git ls-files` (state file untracked) + workflow commit steps + signals.py exit logic | **CONFIRMED**; corroborated by 12 live positions vs top_n 10 |
| calc_shares zero-qty edge | direct call, $500 @ $615.64 | 0 shares, silently "at_target" |
| Config duplicates | regex count of module-level assignments | 12 keys defined twice |
| `main.py` full dry-run | not run from sandbox | yfinance bulk fetch impractical here; run locally after P0 fixes: `python main.py --top 5 --tickers AAPL MSFT ...` |

---

## 7. How to develop this system from now on (process recommendations)

**1. One writer per concern.** Orders: every sell goes through one `sell_with_cleanup`; every stop through one reconciler. State: Alpaca is the only truth for holdings; files are caches. Most of this audit's P0s are three code paths each believing they own the same responsibility.

**2. "Shipped" means exercised, not merged.** `/stoploss execute` was marked done for weeks, never fired once, and turns out to be structurally broken (P0-2). Keep the UAT list (task #29 pattern) and require: every path that can place or cancel an order gets fired once against paper, deliberately, before it's called done. The NUE forced-stop test is exactly the right instinct — make that the norm for every order path.

**3. Add the 30-minute test suite.** Everything in §6 that I tested took minutes because the functions are pure. Create `tests/` with those cases (clamps, reconciler flags, calc_shares, signal diff), run via a GitHub Actions job on push. It would have caught P0-4 and P0-5 at commit time.

**4. Feature freeze until the loop is trusted.** The system currently has: 7 factors, 2 sentiment feeds, congressional + insider signals, shadow learning, feedback learning, an MR sleeve, dual-momentum advisory, VIX panic alerts, 13 workflows, a Cloudflare worker, and 4 closed trades. The ratio of machinery to evidence is the core PM problem — every new layer added a place for this audit's bugs to hide. Rule: until 30 closed pipeline trades, only reliability/observability changes. Then evaluate factors with data, and delete the ones that don't pay.

**5. Session hygiene for AI-assisted development.** (a) Keep CLAUDE.md truthful — a stale briefing makes every future session start wrong (P2-1). (b) End sessions by writing decisions to `memory/`, which you already do. (c) One session = one concern; the largest regressions here came from broad sessions touching order routing incidentally. (d) Use Opus for anything touching `broker/`; cheaper models only for docs/analysis.

**6. Environment.** Move the repo out of OneDrive (P2-3), pin Python to CI's version (P2-4), and after the screener deletion, the codebase should fit in one head: target < 8 workflows and < 10k lines. Deletion is a feature.

**7. Promotion gate to real money** (write it down now, decide later): ≥30 closed trades, ≥8 weeks of the *fixed* system running unattended, win rate and alpha vs SPY measured by `performance_tracker`, every P0 above closed and its test green, kill switch (`/pausetrading`) exercised once. Until all five: paper.

---

## 8. Suggested execution order for Opus

1. P0-2 `sell_with_cleanup` helper + route all 4 call sites (unblocks everything else safely)
2. P0-1 targeted cancels + `reconcile_protective_stops()` at end of execute (subsumes P1-1's trailing pass)
3. P0-4 reconciler honours blackout/cooldown (+ tests)
4. P0-5 whole-share deltas (+ test)
5. P0-3 Alpaca-first exits (biggest change — do it after the order plumbing is safe)
6. P1-2/P1-5 single cap table + single regime taxonomy
7. Monitor reads resting stops (P1-1 remainder)
8. Screener phase-out Steps 2→5 (§4) — owner does Step 1 in the Alpaca UI now
9. P2 hygiene (CLAUDE.md rewrite, config dedupe, repo move)

Each numbered item = one commit/PR with its test. After items 1–5, run one full supervised paper rebalance with the market open and verify: exits fill, stops re-rest on every position, count of stops == count of positions.
