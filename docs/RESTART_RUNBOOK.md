# Clean Restart Runbook — 2026-07-31

**Goal:** liquidate the legacy book, rebuild it on fixed code, and prove every
order path works — while keeping the weekly horizon and the approval gate.

**Decided:** weekly cadence (unchanged — daily churn would measure a different,
worse strategy). Approval gate stays ON (no autonomous trading, so no need for
the daily-loss / max-orders guards autonomy would require).

**Rule throughout:** you run every `--execute` step. Preview first, always.

---

## Why liquidate at all

- **All legacy positions are fractional** — Alpaca stops are whole-share only,
  so they can never be fully protected. Only a rebuild fixes this.
- **The evidence is contaminated.** The existing book and the 13 logged
  outcomes came from a system with no cloud exits, no resting stops, and
  bypassed blackout/cooldown. They measure a system that no longer exists.
- **It exercises the untested path**: BUY from zero → whole shares → reconciler
  attaches stops → invariant holds.

---

## Step 0 — Confirm CI is green  *(2 min)*

GitHub → Actions → the `Risk-Path Tests` run for commit `e3ed6ee` should be
green. If it isn't, stop and fix that first — everything below assumes the
fixes are sound.

```powershell
python -m unittest discover -s tests    # 33 tests, expect OK
```

## Step 1 — Preview the liquidation  *(read-only)*

```powershell
python scripts/liquidate_all.py
```

Read the table. Note which positions carry a resting stop (`stop?` column) and
the total P&L you're choosing to realise.

## Step 2 — Prove the sell path on ONE position  *(market must be open)*

This is the important one. It's the first time `sell_with_cleanup` touches a
real broker.

```powershell
python scripts/liquidate_all.py --one            # preview
python scripts/liquidate_all.py --one --execute  # sell it
```

`--one` picks the smallest position **that has a resting stop**, so the
cancel-then-confirm-then-sell chain is genuinely exercised.

**Pass conditions:**
- `cancelled=1` in the output — the stop was cancelled first
- Status is `filled`, **not** rejected with "insufficient qty available"
- Verification block shows no orphan order left for that ticker

If it fails here, stop and tell me the error. That is exactly why this step is
separate.

## Step 3 — Liquidate the rest

```powershell
python scripts/liquidate_all.py --execute
```

The script automatically:
- snapshots the pre-liquidation book to `data/pre_liquidation_snapshot_*.json`
- writes `data/admin_exits.json` so these exits are tagged **administrative**
  and excluded from factor analysis (otherwise 11 fake "trades" with real P&L
  would pollute the evidence that decides whether your strategy works)
- verifies the end state

**Pass condition: 0 positions AND 0 open orders.** Orphan stops after a close
would be a bug worth catching now.

```powershell
python scripts/liquidate_all.py --verify    # re-check any time
```

## Step 4 — Reset the measurement window

In `config.py`, set the paper-trading clock to today so the validation period
measures the fixed system only:

```python
PAPER_TRADING_START_DATE = "2026-07-31"   # was 2026-05-01
```

## Step 5 — Rebuild: dry run first

```powershell
python main.py
```

**Check before going further:**
- Stage 7 logs `held-position source = Alpaca (0 positions)`
- Every selected name is a **BUY** (nothing held, so no HOLD/EXIT — correct)
- The `[PROTECT]` section shows a dry-run stop plan
- Any name stopped out in the last 5 days (NUE 7/27, EIX 7/31) is **blocked by
  cooldown** if it appears in the top-N — that's the P0-4 fix proving itself

## Step 6 — Rebuild: execute  *(market open, watch it live)*

```powershell
python main.py --execute
```

**Pass conditions — verify in the Alpaca console afterwards:**
- All buys are **whole-share** quantities
- Exposure stays under the 95% bull cap
- **Count of SELL STOP orders == count of positions** ← the invariant
- Run summary shows `stop_reconcile` with `failed: []`
- No order rejected

## Step 7 — Discord surfaces  *(10 min)*

- `/status` — reflects the new book
- `/stoploss mode:check` — levels tagged `[resting @ broker]`, matching Alpaca
- `/stoploss mode:execute` — with nothing breached, replies "N checked, 0 breached"

## Step 8 — Kill switch  *(never tested end-to-end)*

- `/pausetrading` → then `python main.py --execute` → executor must place
  **zero** orders and say why
- `/resumetrading` → normal behaviour returns

## Step 9 — Backtest for edge evidence  *(the real timeline compressor)*

Paper trading tests plumbing. **Backtesting tests edge.** `backtest/backtest.py`
covers 2015–2024 with Sharpe, annualised return and max drawdown — ten years of
evidence in minutes, versus three months of waiting.

```powershell
python backtest/backtest.py
```

Treat the output sceptically: confirm the backtest logic matches the live
scoring path before trusting the numbers. If they diverge, the backtest is
measuring a different strategy than the one that trades.

---

## After this

- Weekly cadence resumes: Monday 09:45 ET proposal → you tap Execute.
- Watch the first few weekly runs for **EXIT signals** — the cloud has never
  produced one before, so this will be new behaviour, not a bug.
- Count only **model** trades toward the 30-trade threshold. Administrative
  exits are tagged and excluded automatically.
- Feature freeze holds until 30 model trades (`docs/ARCHITECTURE.md` §6).

## Standing check after ANY change

**Open SELL STOP orders == open positions holding ≥1 whole share.**
