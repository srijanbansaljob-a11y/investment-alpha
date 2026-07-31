# Investment Alpha — Architecture Contract

**Read before changing anything. Referenced from CLAUDE.md, enforced by
`tests/test_architecture.py` in CI.**

This document exists because the 2026-07-27 audit found five money-losing bugs
that had one shared cause: **more than one component believed it owned the same
responsibility.** Three stop mechanisms, four sell paths, two exposure-cap
tables, two regime vocabularies. Every individual change had been reasonable.
The architecture drifted anyway, because nothing was watching the whole.

The rules below are not style preferences. Each one is a bug that already
happened, written down so it can't happen twice.

---

## 1. Single-owner rules

Every capability has exactly ONE owner. If you need its behaviour, call the
owner — do not reimplement it, do not add a parallel path "just for this case".

| Capability | The one owner | Never do this instead |
|---|---|---|
| Selling / closing / trimming | `alpaca_client.sell_with_cleanup()` | Call `close_position()` or submit a sell order directly. Shares are held against resting stops; the order is rejected |
| Placing / moving / removing protective stops | `stop_loss.reconcile_protective_stops()` | Bracket legs on buys, ad-hoc `place_stop_order` in a new script, a second trailing job |
| Cancelling orders | `alpaca_client.cancel_orders_for_symbol()` | `cancel_orders()` — it kills every protective stop on the account |
| What is currently held | Live Alpaca positions | Trust a JSON file. Files are caches; `data/portfolio_state.json` supplies entry_date + ownership only |
| Stop *levels* to display or act on | `alpaca_client.get_resting_stops()` | Recompute an entry-anchored level and present it as the stop. The broker's resting order is what actually fires |
| Exposure caps | `config.PIPELINE_MAX_INVESTED_PCT` | A second table. `MAX_INVESTED_PCTS` is DERIVED from it and must stay derived |
| Regime classification | `pipeline/regime.py`, 3-tier `bull/neutral/bear` | Introduce a 4-tier or differently-named taxonomy in a new surface |
| Tunable parameters | `config.py` | Hardcode a threshold, percentage, or lookback in a module |

**The test to apply before adding code:** *"Does something here already own
this?"* If yes, extend the owner. If the owner is awkward to call from where
you are, that's a signal to fix the owner — not to fork it.

---

## 2. Standing invariants

Check these after any change to `broker/`. CI checks what it can statically;
the rest are UAT drills (`docs/UAT_CHECKLIST.md`).

1. **Open SELL STOP orders == open positions holding ≥1 whole share.**
   The headline invariant. If it fails, positions are unprotected and that
   outranks every other bug in the repo.
2. **Nothing auto-executes.** Every order needs an owner button press or an
   owner-run command. No countdown timers, no auto-sell.
3. **Cloud and local runs produce identical signals for identical inputs.**
   If a code path depends on a gitignored file, it is broken in the cloud —
   that was P0-3, and it silently disabled all exits for weeks.
4. **A stopped-out name is not re-bought within `REENTRY_COOLDOWN_DAYS`**,
   whether the stop fired locally or at the broker.
5. **Every parameter shown to the user is read from `config.py` at display
   time**, never duplicated into a message string.

---

## 3. Capital allocation model

The account is ONE pot. Any strategy that can spend from it must have an
explicit, partitioned budget — otherwise strategies silently compete and the
account-level cap becomes meaningless.

**Current state (2026-07-31):** the pipeline is the only active strategy and
may use up to `PIPELINE_MAX_INVESTED_PCT` of equity.

**The mean-reversion sleeve is PAUSED** (`MR_ENABLED=False`) precisely because
it violated this rule: it owned a nominal 10% carve-out (`MR_SLEEVE_PCT`) but
gated itself on *account-wide* exposure against the *account-wide* cap. Result:
with the pipeline at 90%+ the sleeve could never trade and posted an identical
"exposure limit reached" notice every weekday; once the cap rose to 95% the
failure inverted, and any sleeve buy would silently consume the pipeline's
headroom.

**Before any second strategy is re-enabled, all three must be true:**

1. It gates on its OWN invested capital vs its OWN budget — never the account total.
2. `PIPELINE_MAX_INVESTED_PCT` reserves that budget, so the two cannot overlap.
3. Its positions are tagged and excluded from the pipeline's EXIT generation
   (already implemented via `_sleeve_tickers()` in `pipeline/signals.py`),
   and included in stop reconciliation.

Flipping `MR_ENABLED = True` without items 1 and 2 reintroduces the bug.

---

## 4. Message accuracy contract

Discord is the only surface you see. A message that is stale, or that describes
a different component's mechanics, is worse than no message — it manufactures
false confidence. The audit found `/stoploss` reporting stop levels $17 below
where the broker would actually sell.

- **Quote live values, not remembered ones.** Percentages, caps, and levels
  are read from `config` or from Alpaca at send time.
- **One vocabulary.** The regime is `BULL / NEUTRAL / BEAR` everywhere. Do not
  print `MOD BULL` in one surface and `BULL` in another for the same state.
- **Say which component you mean.** "Positions exit on stops or time-outs"
  was wrong at account level — time-outs are a sleeve mechanic only.
- **Never notify about something that cannot be acted on.** A recurring alert
  with no possible action trains you to ignore the channel where real stop-loss
  alerts arrive. Rate-limit standing conditions to weekly (see
  `_should_post_skip_notice`).
- **Absence of an alert must never be indistinguishable from a broken job.**
  Every scheduled workflow posts either a result or a failure alert.

---

## 5. Change protocol

For any change touching `broker/`, `config.py`, order routing, or a Discord
surface:

1. **Name the owner.** Which single component owns this capability? Am I
   extending it, or forking it? (Forking needs a written reason here.)
2. **Check the blast radius.** `grep` for other readers of any value or file
   you change. A cap raised in one table while another still holds the old
   number is the P1-2 bug.
3. **State the invariant it could break** from §2, and how you verified it.
4. **Add or extend a test** in `tests/`. If the change can place, cancel, or
   skip an order, this is mandatory, not optional.
5. **Update the docs you just made untrue** — CLAUDE.md, this file,
   `memory/AGENT_MEMORY.md`.
6. **Exercise it once on paper** before calling it done (`docs/UAT_CHECKLIST.md`).
   "Merged" is not "working": `/stoploss execute` was marked done for weeks
   and was structurally incapable of executing.

---

## 6. Deliberate non-goals

Recorded so they aren't re-litigated, or "fixed" by a future session:

- **No resting take-profit orders.** TP is a monitor alert only. A hard sell
  ceiling on a momentum book amputates the right tail, where momentum P&L
  lives — and a resting TP holds shares, recreating the sell-conflict bug.
- **No bracket orders on entry.** One stop mechanism, applied after fills.
- **No auto-execution**, however convenient.
- **No new factors, signals, or strategies until 30 closed pipeline trades.**
  The system already has 7 factors, 4 alternative signals, 2 learning loops and
  13 workflows supporting 4 closed trades. The machinery-to-evidence ratio is
  the project's main risk; deletion is a feature.
- **No features added to `screener/`** — it is being retired
  (`docs/FABLE_AUDIT_2026-07-27.md` §4).
