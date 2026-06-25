# Investment Alpha — Improvement Plan

**Date:** 2026-06-25
**Companion to:** `ARCHITECTURE_REVIEW_CORNER_CASES.md`
**Context change:** rebalance + execution moved from **monthly → weekly, automatic** (Task Scheduler, Mondays 10:00 AM). This plan is sequenced for that new reality.

---

## Why the weekly change raises the stakes

Going weekly-automatic changes the risk profile in three ways, and the plan below is ordered around it:

1. **State drift now happens 4× as often.** The bugs where the "what I own" file diverges from Alpaca (C1/C2/C3) used to corrupt once a month; now it's weekly. These move to the top.
2. **Trades fire with no human in the loop.** The old `YES` prompt is gone for the scheduled run. So the *safety rails inside the code* (reconciliation, stop-loss, order verification) have to be trustworthy on their own.
3. **Turnover and cost go up.** A full top-10 reshuffle every week churns positions. We add a turnover guard so the model only trades when conviction actually changed, not on weekly noise.

**Recommended gate:** complete **Phase 0 + Phase 1** before relying on the unattended Monday auto-execute. Until then, run `run_weekly_execute.bat` manually (it keeps the `YES` prompt) so you can eyeball each week.

---

## Phase A — Weekly approval flow (your requested gate) — build first

**Goal:** the weekly run happens automatically, but **no trade executes without your tap.** You also get a heads-up on Discord before each run.

**Current safe state (already applied):** the Monday 10:00 AM task is set to **analysis-only** (`main.py`, no `--execute`). It cannot trade on its own. Until the flow below is built, you approve by reviewing the output and running `run_weekly_execute.bat` (which keeps the `YES` prompt).

**Target flow:**

1. **Pre-run reminder.** A scheduled Discord post (proposed: **Monday 9:00 AM ET**, ~1 hr before the run, and/or Sunday evening): *"⏰ Weekly rebalance runs at 10:00 AM and will post trades here for your approval. Nothing trades without your tap."* — implemented as a tiny webhook post (own Task Scheduler entry or a GitHub Actions cron).
2. **Proposal post.** Monday 10:00 AM the analysis runs and posts the BUY/HOLD/EXIT list to Discord with **Approve / Reject** buttons (reuse the existing button infra used by the monitor and sleeves). The proposal is saved to `outputs/proposed_portfolio.json` (and/or Cloudflare KV) so the approved order set is exactly what you saw.
3. **On Approve.** The Cloudflare worker (`worker/index.js`) fires `repository_dispatch` → `command.yml` runs a new `approve_rebalance` command that executes the *saved* proposal via the Alpaca-first executor (reconciled against live positions).
4. **On Reject / no response.** Nothing trades. Proposal expires after a set window (e.g. end of trading day).

**Pieces to build:**
- `broker/remote_commands.py`: add a "post weekly rebalance proposal with approval buttons" action and an `approve_rebalance` handler that loads the saved proposal and calls the executor.
- `worker/index.js`: add the `approve_rebalance` button → `repository_dispatch` mapping (it already does this pattern for other buttons).
- Persist the proposal (KV or committed `proposed_portfolio.json`) so approval executes precisely what was shown.
- Pre-run reminder: small webhook post on its own schedule.

**Note:** this depends on **Phase 1** (single source of truth + Alpaca reconciliation) to be fully trustworthy — approval should execute against real holdings, not the stale file. Build Phase A's plumbing, but don't rely on unattended approve-to-execute until Phase 1 lands.

---

## Phase 0 — Safety rails before the first unattended run (½ day)

These are small and make the automatic run observably safe.

| Item | Problem | Fix |
|---|---|---|
| **0.1 Market-open guard** (H2) | Fractional orders are rejected when the market is closed and counted as "placed." | In `executor.execute_signals`, if `not market_open and not dry_run`: either round to whole shares, or skip + log a loud `MARKET_CLOSED_SKIP` and post a Discord notice. Never report a rejected order as filled. (Scheduling at 10:00 AM already avoids the common case; this covers holidays/half-days.) |
| **0.2 Turnover guard** | Weekly full rebalance churns on noise. | In `signals.py`, only emit BUY/EXIT when rank/score change exceeds a threshold (e.g. a held name stays HOLD unless it falls out of the top-N by more than a buffer, like rank > N+3). Add `REBALANCE_RANK_BUFFER` to config. |
| **0.3 Kill switch + heartbeat** | Unattended trading needs an off switch and proof it ran. | Add `EXECUTION_ENABLED` flag in config (default True) checked at the top of `executor`. Post a one-line Discord summary every run ("Weekly rebalance: 2 buys, 1 exit, equity $X") so a silent failure is visible. |

---

## Phase 1 — Fix the state-divergence core (1–2 days) — **highest payoff**

This is the root cause behind C1, C2, C3, and M2. The theme: **stop trusting the file for "what I hold"; read it from Alpaca. Keep the file as analysis history only.**

| Item | Problem (review ref) | Fix |
|---|---|---|
| **1.1 Single state writer** | C2 — two writers clobber the file; the winner drops `regime` + `entry_date`. | Delete `output.save_portfolio_state()`. Let **only** `signals._save_portfolio_state()` own `latest_portfolio.json`, writing the full schema (`run_date`, `regime`, per-position `entry_price`, `entry_date`). Add a `schema_version` field; have readers assert it. |
| **1.2 Sticky entry price** | C1 — `entry_price` is reset to today's price every run, so stops never trip on a slow bleed. | In `portfolio.py`/`signals.py`, set `entry_price` **only on a true BUY**; carry it forward on HOLD (signals.py already has this logic — 1.1 stops it being overwritten). |
| **1.3 Alpaca = source of truth for holdings** | C3 + M2 — analysis-only runs rewrite "held" state; manual/dropped positions go unprotected. | Split the files: analysis writes `outputs/proposed_portfolio.json`; "held" state is read live from Alpaca `get_positions()` (using `avg_entry_price` as the real cost basis). `stop_loss.py` and the executor both read holdings from Alpaca, not the file. |
| **1.4 Regime persisted + read** | C2 side-effect — stop-loss always defaults to BULL (loosest stop). | Once 1.1 lands, `state["regime"]` is present; confirm `stop_loss.py` reads it (and falls back to the live `regime.run()` result, not hard-coded "bull"). |

**Outcome:** regime-aware stops actually work, slow bleeds get stopped out, and the three lists (notes / file / Alpaca) reconcile to one truth.

---

## Phase 2 — Execution safety for unattended trading (1 day)

| Item | Problem (ref) | Fix |
|---|---|---|
| **2.1 One locked execution path** | H1 — local run + cloud monitor can both submit to the same account. | Funnel all order submission through one module. Add a simple lock (Cloudflare KV key — you already use KV — or an Alpaca-side marker) that every executor checks and sets. Local weekly run and cloud auto-execute must respect the same lock. |
| **2.2 Order verification** | Unattended orders need confirmation they filled. | After submitting, poll order status (or next-run reconcile) and record fill price/qty back to state. Treat "submitted" ≠ "filled." |
| **2.3 Re-entry cooldown** | M3 — a name can be stopped out then re-bought same run. | Track `last_exit_date` per ticker; block re-buy within N days (config `REENTRY_COOLDOWN_DAYS`). |
| **2.4 Cash guard** | L1 — buys allowed at 105% of cash. | Tighten the `* 1.05` slack to `1.0`, or size against `buying_power` explicitly. |

---

## Phase 3 — Data reliability & queue durability (1 day)

| Item | Problem (ref) | Fix |
|---|---|---|
| **3.1 Route data through the fallback layer** | H4 — main pipeline still uses yfinance for regime/earnings/ingestion; silent fallback to NEUTRAL / no-blackout. | Point `regime.py`, `signals._days_to_earnings`, and ingestion price reads at `broker/market_data.py` (Alpaca → Finnhub → yfinance). Log every fallback so a data outage is visible, not silent. |
| **3.2 Durable pending queue** | H3 — `pending_actions.json` is git-ignored; cloud auto-execute queue evaporates each run. | Persist the queue in Cloudflare KV (or as a stop order on Alpaca itself). If the cloud path is meant to be alert-only, remove the misleading 5-minute auto-execute timer from it. |
| **3.3 Atomic writes** | M4 — non-atomic JSON writes on OneDrive cause null-byte corruption. | Write to a temp file in the same dir + `os.replace()`; keep the existing backup-restore as a fallback. |

---

## Phase 4 — Hygiene & observability (½ day)

| Item | Problem (ref) | Fix |
|---|---|---|
| **4.1 De-duplicate config** | M1 — keys defined twice; second silently wins (`CACHE_MAX_AGE_HOURS`, `MIN_HISTORY_DAYS`, `MOMENTUM_STRONG_THRESHOLD`, `TREND_BULLISH_THRESHOLD`, …). | Keep one definition each; add a test asserting no duplicate keys. |
| **4.2 Cache earnings lookups** | M5 — `_days_to_earnings` called up to 3×/ticker. | Compute once per ticker per run, reuse. |
| **4.3 Standardize time guards** | L2 — `strategies.yml` uses UTC day vs ET elsewhere. | Use `TZ=America/New_York` consistently in all workflow guards. |
| **4.4 Vol floor** | L3 — inverse-vol weighting can over-concentrate on a near-zero vol. | Floor `vol` (e.g. `max(vol, 0.05)`); assert final weights sum to 1 and none exceed the cap. |
| **4.5 Data-health line** | Silent failures throughout. | One summary log + Discord line per run noting any fallback/skip. |

---

## Suggested schedule

| Week | Focus | Result |
|---|---|---|
| 1 | Phase 0 + Phase 1 | Unattended weekly run is safe; stops work; one source of truth. **Gate to enable auto-execute.** |
| 2 | Phase 2 + Phase 3 | No double-trading, durable queues, reliable data. |
| 3 | Phase 4 + verification | Clean config, observability, backtest of the weekly turnover guard. |

## Verification (do for each phase)

- **Dry-run diff:** run `run_weekly.bat` before/after a change and diff `outputs/proposed_portfolio.json` to confirm only intended behavior changed.
- **State assertion test:** a small script that loads `latest_portfolio.json`, checks `schema_version`, `regime`, and that every position has `entry_date` + `entry_price`.
- **Reconciliation test:** force a mismatch (manually add a position in Alpaca paper) and confirm the executor's reconcile does the right thing.
- **Stop-loss test:** set a fake low `entry_price` and confirm a breach triggers in the correct regime.
- Keep the `YES`-prompt manual run (`run_weekly_execute.bat`) as the fallback until Phase 1 is verified.

---

## Implementation status (updated 2026-06-25)

DONE and unit-tested in this pass (synthetic-data tests, all passing):

- Phase 1.1 — single state writer. `output.save_portfolio_state()` is now a no-op;
  `signals._save_portfolio_state()` is the sole writer.
- Phase 1.2 — sticky entry_price (preserved on HOLD; only set on a true BUY).
- Phase 1.4 — regime + entry_date + schema_version now persisted in state.
- Phase 1.3 — stop_loss.py reads holdings from Alpaca (real avg_entry_price),
  falls back to the state file.
- Phase 0.1 — market-closed guard in executor (skips instead of fake-filling).
- Phase 0.3 — EXECUTION_ENABLED kill switch.
- Phase 0.2 — turnover guard (rank hysteresis) in selection.py.
- Phase 2.3 — re-entry cooldown (reads stop_loss_log.json; suppresses re-buys).
- Phase 2.4 — cash guard tightened (CASH_BUFFER_MULTIPLIER, default 1.0).
- Phase 4.1 — config.py duplicate keys removed.
- Phase 4.4 — inverse-vol weighting floor (VOL_FLOOR).
- Phase 3.3 — atomic state writes (temp file + os.replace) vs OneDrive corruption.

New config flags: EXECUTION_ENABLED, EXECUTION_REQUIRE_MARKET_OPEN,
CASH_BUFFER_MULTIPLIER, REBALANCE_RANK_BUFFER, REENTRY_COOLDOWN_DAYS, VOL_FLOOR,
STATE_SCHEMA_VERSION.

Verification: every changed module compiles; core behaviors covered by
synthetic-data tests (vol floor / cap, turnover carryover, sticky entry price,
cooldown block, state schema+regime, market-closed skip, cooldown skip).

NOT YET DONE (need your environment / a focused follow-up):

- Phase A — Discord approval buttons for the rebalance + pre-run reminder.
  Requires editing broker/remote_commands.py and worker/index.js and DEPLOYING
  the Cloudflare worker + Discord bot (needs your secrets). Cannot be tested
  from here. Interim safety is already in place: the weekly task is analysis-only,
  so nothing trades unattended; you approve via run_weekly_execute.bat.
- Phase 2.1 — single locked execution path (KV lock) across local + cloud.
- Phase 3.1 — route main pipeline data reads through broker/market_data.py.
- Phase 3.2 — durable pending-actions queue (KV).
- Phase 4.2 — minor: cache earnings-date lookups (M5).
