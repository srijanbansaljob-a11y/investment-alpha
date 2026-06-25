# Investment Alpha — Architecture & Corner-Case Review

**Reviewer role:** Product analyst / systems review
**Date:** 2026-06-25
**Scope:** End-to-end workflow of the live trading system (pipeline → execution → automation → learning)
**Method:** Read of `main.py`, `config.py`, the `pipeline/`, `broker/`, and `screener/` modules, all 8 GitHub Actions workflows, the Windows Task Scheduler setup, and the live state files (`outputs/latest_portfolio.json`, `data/pending_actions.json`).

---

## 1. How the system actually runs today

There are **two independent "brains" controlling the same Alpaca account**, and they do not share state:

**Brain A — the local pipeline (your PC).**
`main.py` runs the 8-stage pipeline. Scheduled monthly by Task Scheduler as **analysis-only** (`main.py`, no `--execute`). Trades only happen when you manually run `run_monthly_execute.bat` (which prompts `YES`). Weekly `stop_loss.py` runs locally from Task Scheduler. Source of truth for Brain A: `outputs/latest_portfolio.json`.

**Brain B — the cloud (GitHub Actions).**
8 workflows post to Discord and act on approvals: intraday monitor (every 15 min), daily summary, mean-reversion + dual-momentum sleeves, weekly learning, the screener (3×/day), nightly updater, and the Discord command handler. Source of truth for Brain B: **the live Alpaca API** (it deliberately ignores the local state file).

The core structural risk is that **these two brains can issue orders to the same account with no shared lock, and they disagree about what "entry price" and "current regime" mean.** Most of the high-severity findings below trace back to this split.

---

## 2. Severity summary

| # | Corner case | Severity | Where |
|---|---|---|---|
| C1 | `entry_price` resets to current price on every run → stop-loss baseline drifts up/down and slow bleeds never trigger | **Critical** | `portfolio.py` + `output.py` |
| C2 | Two writers clobber the state file; the winner drops `entry_date` and `regime` → weekly stop-loss silently always runs in BULL (loosest) mode | **Critical** | `output.py` vs `signals.py` |
| C3 | Scheduled monthly job is analysis-only but rewrites state → state describes a portfolio that was never bought; diverges from real Alpaca holdings | **Critical** | Task Scheduler + `signals.py` |
| H1 | No cross-brain trade lock → local stop-loss and cloud monitor can both sell the same position | High | architecture |
| H2 | Fractional-share orders placed while market closed are rejected by Alpaca (can't be queued) | High | `executor.py` / `alpaca_client.py` |
| H3 | `pending_actions.json` is git-ignored → cloud auto-execute queue never persists between 15-min runs | High | `monitor.yml` + `.gitignore` |
| H4 | Local pipeline still depends on yfinance, which is blocked on GitHub Actions and rate-limits locally | High | `regime.py`, `signals.py`, `ingestion.py` |
| M1 | Duplicate keys in `config.py` silently override each other | Medium | `config.py` |
| M2 | Stop-loss only checks tickers in the state file, not actual Alpaca holdings → orphaned manual/dropped positions are never protected locally | Medium | `stop_loss.py` |
| M3 | Whipsaw: pre-flight stop-loss can exit a name the same run then re-buy it | Medium | `main.py` |
| M4 | Non-atomic state writes + OneDrive sync = corruption (already partly seen) | Medium | `signals.py`, `output.py` |
| M5 | `_days_to_earnings()` called up to 3× per ticker; multiplies a flaky network call | Medium | `signals.py` |
| L1 | Cash guard allows buying at 105% of cash | Low | `executor.py` |
| L2 | DST/UTC-vs-ET guards inconsistent across workflows | Low | workflows |
| L3 | Inverse-vol weighting trusts `vol_60d`; a stale/zero vol can over-concentrate (cap mitigates) | Low | `portfolio.py` |

---

## 3. Critical findings (fix these first)

### C1 — `entry_price` is reset to the current price on every run
**Evidence.** `portfolio.py` line 149: `entry_price = float(row["current_price"])` — every position is stamped with *today's* price each run. The live `latest_portfolio.json` confirms it: every holding has `entry_price == current_price` (NEM 109.5/109.5, VTRS 15.69/15.69).

**Why it matters.** `stop_loss.py` computes the stop as `entry_price × multiplier` (or `entry_price − ATR×k`). If `entry_price` is re-anchored to the latest price on every pipeline run, a position that is slowly bleeding **never breaches its stop** — the floor keeps following the price down. This defeats the single most important risk control in the system. The real fill price lives in Alpaca (`avg_entry_price`) and is being ignored by the local stop path.

**Fix.**
- Make `entry_price` *sticky*: only set it on a true BUY (new entry). On HOLD, carry the prior `entry_price` forward. `signals.py` already does this correctly — the problem is C2 below overwrites it.
- Better: for anything execution-related, **read `avg_entry_price` from Alpaca** as the cost basis rather than trusting the file. The local stop-loss should pull cost basis from `get_positions()` the same way the cloud monitor does.

### C2 — Two modules write `latest_portfolio.json`; the wrong one wins
**Evidence.** Both `pipeline/signals.py` (`_save_portfolio_state`, line 252) and `pipeline/output.py` (`save_portfolio_state`, line 133) write `config.PORTFOLIO_STATE_FILE`. In `main.py`, Stage 7 (signals) runs *before* Stage 8 (output), so **output.py wins**. Output's writer dumps only `{"portfolio": portfolio}` — it drops `entry_date`, top-level `run_date`, and `regime`. The live file proves it: `keys: ['portfolio']`, `entry_date` absent, `regime` absent.

**Why it matters (two real downstream failures):**
1. `stop_loss.py` does `regime = state.get("regime", "bull")`. Because the winning writer never persists `regime`, the weekly stop-loss **always defaults to BULL** — the loosest 15% stop — even in a neutral or bear tape. Your regime-adaptive stops are effectively disabled.
2. Output's writer takes `entry_price` straight from `portfolio.py` (= current price), so it also overwrites the sticky `entry_price` that signals.py just preserved — this is the mechanism that causes C1 to actually bite.

**Fix.** Have exactly **one** state writer. Delete `output.save_portfolio_state()` (or make it write a separate display-only file), and let `signals.py` own `latest_portfolio.json` with the full schema (`run_date`, `regime`, per-position `entry_price`/`entry_date`). Add a schema-version field and have readers assert on it.

### C3 — The scheduled monthly job rewrites state but never trades
**Evidence.** `setup_task_scheduler.ps1` registers the monthly task as `main.py` with **no `--execute`**. `run_monthly_execute.bat` (the one that trades) is manual and prompts for `YES`. But every analysis-only run still calls Stage 7/8 and rewrites `latest_portfolio.json` with a brand-new target list and fresh entry prices.

**Why it matters.** Once a month the state file is silently replaced with a portfolio you may never have bought, stamped with today's date as the "entry." Meanwhile Alpaca still holds last month's actual positions. From that moment the local stop-loss is checking the wrong tickers at the wrong entry prices. This is the concrete cause of the divergence already visible between `CLAUDE.md` (MRK/HST/MATX…), the state file (NEM/VTRS…), and whatever Alpaca actually holds.

**Fix.** Separate "analysis" output from "held positions." Analysis-only runs should write `outputs/proposed_portfolio.json`; `latest_portfolio.json` (held state) should only change when an execution actually fills, reconciled against Alpaca. Equivalently: stop deriving held-state from the analysis pipeline at all and rebuild it from `get_positions()` each time.

---

## 4. High-severity findings

### H1 — No shared lock between local and cloud execution
Only `command.yml` and `strategies.yml` share a concurrency group (`discord-commands`). The local `main.py --execute`, the local weekly `stop_loss.py`, and the cloud `monitor.py` auto-execute path have **no mutual exclusion**. Two independent processes can submit to the same Alpaca account in the same minute (e.g., local stop-loss sells HAS while a Discord "approve sell HAS" runs in the cloud). Alpaca will reject the second as "no position," but you can also get partial double-handling and confusing logs.
**Fix.** Centralize all order submission behind one path. If both must exist, add a lightweight distributed lock (a `lock` key in Alpaca's account via a tiny order tag, or a lock file the cloud reads from a committed location / KV) and make every executor check it.

### H2 — Fractional orders can't be queued while the market is closed
`executor.py` logs *"Market is CLOSED — orders queued as DAY orders for next open"* and proceeds. But position sizing produces fractional quantities (`calc_shares` rounds to 4 dp), and **Alpaca rejects fractional-share orders outside regular hours** — they cannot be queued. The Task Scheduler monthly window is 08:30 local; if that's pre-open ET, a manual execute there would silently fail most BUYs.
**Fix.** Either (a) gate `--execute` to only submit when `is_market_open()` is true (queue a reminder otherwise), or (b) round to whole shares when the market is closed, or (c) use notional orders only during RTH. At minimum, surface rejected fractional orders loudly instead of counting them as placed.

### H3 — The cloud auto-execute queue can't persist
`monitor.yml` runs `monitor.py --once` every 15 min on a fresh runner. The 5-minute `AUTO_EXECUTE_DELAY_MINUTES` means a breach is queued into `data/pending_actions.json` with `execute_after = now+5min` — then the process exits. The next run is a **fresh checkout**, and `pending_actions.json` is in `.gitignore`, so the queued action is gone. The delayed auto-execute effectively never fires in the cloud; only same-process (local long-running monitor) queue+execute works. The sample file shows a same-run execute, consistent with this.
**Fix.** Persist the pending queue somewhere durable (Cloudflare KV — you already use it; or a committed file; or Alpaca itself as a stop order). If "alert-only, never auto-sell" is the intended cloud behavior, remove the auto-execute delay logic from the cloud path so it isn't misleading.

### H4 — yfinance dependency in the always-on paths
Git history shows Yahoo is blocked on GitHub Actions, which is why the screener migrated to Alpaca/Finnhub. But the **main pipeline still uses yfinance** for regime (`regime.py`), earnings dates (`signals._days_to_earnings`), and ingestion. That's tolerable while these run locally, but: (a) regime detection silently falls back to NEUTRAL on any yfinance hiccup (good fail-safe, but means a flaky network quietly changes position counts and stops), and (b) `_days_to_earnings` failures silently disable the earnings blackout (returns `None` → no block).
**Fix.** Route the main pipeline's price/regime/earnings reads through the same `broker/market_data.py` fallback chain you already built. Log when a fallback is used so a silent data outage is visible, not invisible.

---

## 5. Medium-severity findings

**M1 — Duplicate config keys silently override.** `config.py` defines several keys twice and the second wins: `CACHE_MAX_AGE_HOURS` (8 → **4**), `MIN_HISTORY_DAYS` (60 → **252**), `MOMENTUM_STRONG_THRESHOLD` (0.65 → **0.6**), `TREND_BULLISH_THRESHOLD` (0.65 → **0.5**), plus `HISTORY_DAYS`/`MOMENTUM_*` repeats. Anyone editing the first (documented) block will think they changed behavior when they didn't. *Fix:* de-duplicate; keep one block; add a unit test that asserts no key is defined twice.

**M2 — Local stop-loss only sees the state file's tickers.** `stop_loss.py` iterates `state["portfolio"]`. Anything held in Alpaca but absent from the last analysis target (manual buys, names dropped at rebalance but not yet sold) is **never stop-checked locally**. *Fix:* drive the stop-loss off `get_positions()` (Alpaca truth), using `avg_entry_price` as the basis — same source the cloud monitor uses. This also fixes C1/C2 for the stop path.

**M3 — Rebalance whipsaw.** In `main.py`, the pre-flight stop-loss can EXIT a name, then Stage 5 selection can re-pick the same name in the new top-N and BUY it back in the same run. *Fix:* add a short re-entry cooldown (e.g., don't re-buy a ticker stopped out within N days), tracked in state.

**M4 — Non-atomic writes + OneDrive.** `signals.py`/`output.py` write JSON with a direct `open(...,'w')`/`write_text`, and the repo lives in OneDrive. You've already hit null-byte corruption (hence the `rstrip(b'\x00')` repair). *Fix:* write to a temp file in the same directory and `os.replace()` (atomic) into place; keep the existing backup-restore as a fallback.

**M5 — Repeated earnings-date calls.** `signals.py` calls `_days_to_earnings(ticker)` once in the blackout check and **again twice** inside the `risk_note` f-string for blocked names — 2–3 network round-trips per ticker for the same value. *Fix:* compute once, reuse; cache per run.

---

## 6. Low-severity / polish

- **L1 — Cash guard slack.** `executor.py` skips a buy only if `cost > available_cash * 1.05`, i.e. it will spend up to 105% of cash. On a paper margin account this is harmless; on anything cash-only it risks rejects. Tighten to `1.0` (or explicitly model buying power).
- **L2 — Time-guard inconsistency.** Most workflows guard against the EDT/EST double-cron with a `TZ=America/New_York` hour check, but `strategies.yml` and the dual-momentum day check use `date -u` (UTC). Near month boundaries / late UTC this can fire on the wrong calendar day. Standardize all guards on ET.
- **L3 — Inverse-vol concentration.** Weighting trusts `vol_60d`; a stale or near-zero vol would blow up `1/vol`. The 20%/cap-and-renormalize loop mitigates it, but add a floor on `vol` (e.g., `max(vol, 5%)`) and an explicit check that final weights sum to 1 and none exceed the cap.
- **Observability gap.** Failures fail *quietly* in several places (regime → NEUTRAL, earnings → no block, price providers → empty dict, feedback/perf tracker wrapped in `except: log.warning`). For a system trading real-ish money unattended, add a single "data health" line per run and a Discord alert when any fallback triggers, so silent degradation becomes visible.

---

## 7. Recommended order of work

1. **C2 then C1** — collapse to one state writer that preserves `regime` + sticky `entry_price`; or better, repoint the stop-loss at Alpaca cost basis. This restores regime-aware stops and fixes the bleed-through-stop bug together. (Small, high payoff.)
2. **C3** — split proposed vs held state so the monthly analysis run stops corrupting held-position state.
3. **M2 + H1** — make Alpaca the single source of truth for *held positions* and funnel all order submission through one locked path.
4. **H2, H3, H4** — closed-market fractional handling, durable pending queue, and route data reads through the fallback layer.
5. **M1, M4, M5, L1–L3** — config hygiene, atomic writes, and observability.

The unifying theme: **the model pipeline and the brokerage account are two databases that have drifted apart.** Almost every critical issue dissolves once "what we hold" is read from Alpaca on demand and the file-based state is demoted to analysis history only.
