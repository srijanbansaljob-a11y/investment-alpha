# Investment Alpha — Workflow & Messaging Audit

**Date:** 2026-07-27
**Scope:** every Discord surface, every scheduled job, and the screener→pipeline coupling.
**Context:** user is moving to **pipeline-only** and preparing for real money.

---

## 1. Headline findings

| # | Finding | Severity | Why it matters |
|---|---------|----------|----------------|
| F1 | **`main.py` runs in ZERO workflows** | **P0** | The core pipeline only executes when you manually fire `/pipeline`. Grep of `.github/workflows/` for `main.py` returns no matches. The system that picks your stocks has no heartbeat. |
| F2 | **Worker defaults every order path to `screener`** | **P0** | `placeBracketOrder`, `buildBuyPreview`, `/buy`, `/sell`, `/brief`, `/status` fallback, TradingView webhook. Once screener is retired, any missed default routes real orders to a dead account. |
| F3 | **`monitor.yml` has no failure alert** | **P1** | Runs every 15 min and is the stop-loss safety net. If it starts failing, alerts stop and you hear nothing. Silence is indistinguishable from "nothing triggered". |
| F4 | **`command.yml` has no failure alert** | **P1** | If the job dies before Python runs (checkout/deps/timeout), the deferred Discord interaction hangs forever with no error. |
| F5 | **`strategies.yml` has no failure alert** | **P1** | Mean-reversion proposals + position snapshots + trade outcome logging all die silently. |
| F6 | **`pipeline/learning.py` posts nothing** | **P1** | Rewrites the factor weights that choose your stocks, weekly, with zero notification. Model mutates silently. `learning.yml` also has no failure alert. |
| F7 | `scripts/daily_performance.py` posts nothing | P2 | Writes P&L data consumed elsewhere; no direct visibility. |
| F8 | `/help` will be stale after screener retirement | P2 | Documents commands that will no longer exist. |

---

## 2. Slash command inventory

16 commands registered in `scripts/register_discord_commands.py`.

| Command | Handled by | Portfolio default | Status |
|---------|-----------|-------------------|--------|
| `/status` | GH Actions → `cmd_status` | Python `pipeline` ✅ | OK |
| `/regime` | GH Actions → `cmd_regime` | n/a | OK |
| `/monitor` | GH Actions → `cmd_monitor_check` | required arg | OK |
| `/stoploss` | GH Actions → `cmd_stoploss_*` | required arg | `check` OK; **`execute` never UAT'd** (task #29) |
| `/pipeline` | GH Actions → `cmd_pipeline_*` | pipeline only | ✅ fixed this session (structured embed + fill prices) |
| `/strategy` | GH Actions → `cmd_strategy` | n/a | OK |
| `/chart` | GH Actions → `cmd_chart` | n/a | OK |
| `/screener` | Worker (KV read) | screener | **retire** |
| `/help` | Worker | n/a | update after retirement |
| `/buy` | Worker | **`screener`** ⚠️ | **F2** |
| `/sell` | Worker | **`screener`** ⚠️ | **F2** |
| `/brief` | Worker | **hardcoded `screener`** ⚠️ | **F2** |
| `/pausetrading` | Worker (KV) | n/a | OK |
| `/resumetrading` | Worker (KV) | n/a | OK |
| `/health` | GH Actions → `cmd_health` | both | ✅ calibrated this session |
| `/rebalance` | Worker → GH Actions | pipeline | OK |

**Error handling:** `remote_commands.main()` catches handler exceptions and replies to Discord with the traceback — good. The gap is *infrastructure* failure before Python starts (see F4).

---

## 3. Scheduled job inventory

| Workflow | Cron | Posts result? | Failure alert? |
|----------|------|---------------|----------------|
| `command.yml` | dispatch | via `remote_commands` | ❌ **F4** |
| `daily_summary.yml` | 9:00 ET weekdays | ✅ status embed | ❌ |
| `health_check.yml` | 8:30 ET weekdays | ✅ | ✅ |
| `learning.yml` | Sat 12:00 UTC | ❌ **F6** | ❌ **F6** |
| `monitor.yml` | */15, 13–21 UTC weekdays | ✅ alerts only | ❌ **F3** |
| `screener_daily.yml` | 3×/day | ✅ | ✅ |
| `screener_nightly.yml` | 16:30 ET | ✅ | ✅ |
| `screener_weekly.yml` | Sun 18:00 ET | ✅ | ✅ (push race fixed this session) |
| `strategies.yml` | 16:30 ET weekdays | ✅ MR proposals | ❌ **F5** |
| `weekly_rebalance.yml` | Mon | ✅ | ✅ |
| `weekly_report.yml` | Sat | ✅ factor analysis | ✅ |
| **`main.py` (pipeline)** | **none** | — | — **F1** |

---

## 4. Screener coupling map

Retiring screener is **not a delete**. These pipeline-facing surfaces read screener-produced data:

| Consumer | Reads | Impact if screener removed today |
|----------|-------|----------------------------------|
| Worker `/buy` bracket orders | KV `screener_summary` (ATR stop + TP targets) | **Loses dynamic stops/targets** — falls back to static or none |
| Worker `/brief` | KV `screener_summary`, `regime` | Loses picks + regime display |
| Worker `/screener` | KV `screener_summary` | Command dies |
| `scripts/trade_outcome_logger.py` `_get_signals()` | `screener/daily_sentiment_data.json` | **Blinds factor learning for pipeline trades too** — signal enrichment returns empty |
| `scripts/rebalance_check.py` | same file | Degrades rebalance suggestions |
| `scripts/snapshot_positions.py` | screener Alpaca account | Snapshot loses a portfolio (harmless) |
| `scripts/daily_performance.py` | screener Alpaca account | Same |
| `scripts/health_check.py` | screener Alpaca account | Same |

**Live account state:** the screener Alpaca account still holds 8 positions at **−$8,766 cash (108% invested, on margin)**. Code changes do not unwind this. It must be liquidated manually or left to close out.

### Recommended phased retirement

- **Phase A (now):** flip *all* Worker defaults `screener` → `pipeline`. Keep `screener_daily.yml` running purely as a **data feed** (KV targets + signal enrichment). No trading decisions route to screener.
- **Phase B:** unwind the screener account positions so the margin balance clears.
- **Phase C:** port ATR/TP target generation and signal enrichment to pipeline-native sources, so nothing reads `screener_summary` or `daily_sentiment_data.json`.
- **Phase D:** delete screener workflows, code, `/screener` command; update `/help`.

Do **not** jump to Phase D first — that is the ordering that breaks `/buy` stops and factor learning.

---

## 5. Remediation implemented this pass

1. Worker defaults flipped to `pipeline` (F2).
2. Failure alerting added to `monitor.yml`, `command.yml`, `strategies.yml`, `learning.yml`, `daily_summary.yml` (F3, F4, F5).
3. `pipeline/learning.py` now posts a weight-change summary to Discord (F6).
4. Scheduled pipeline dry run added with an execute button — human stays in the loop (F1).

## 6. Still open

- Task #29: `/stoploss mode:execute` has never been exercised end-to-end. **Mandatory before real money.**
- Kill switch (`/pausetrading`) never tested end-to-end.
- Screener account margin balance (−$8,766) unresolved.
- Phases B–D of screener retirement.
