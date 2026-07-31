# UAT Checklist — Bridging the Audit Gap (2026-07-27)

The audit's P0/P1 fixes are implemented and all 17 risk-path tests pass. But
tests prove the logic; only supervised live drills prove the *system*. This
checklist converts "code fixed" into "system trusted". Work top to bottom;
each drill has a pass condition. Do not tick a box on faith.

**Rule:** you run every order-placing step yourself. Nothing here trades on
its own. All drills are on the paper account.

---

## Drill 0 — Push and see CI green  *(5 min, market closed OK)*

```powershell
git add -A
git commit -m "fix: audit P0-1..P0-5 + P1 — single sell path, stop reconciler, Alpaca-first exits, cooldown from broker fills; tests + CI"
git push
```

- [ ] `Risk-Path Tests` workflow runs on GitHub and is green.

## Drill 1 — NUE stop  ✅ **FIRED 2026-07-27 17:51 UTC** — partially passed

The forced stop filled: **39 shares @ $245.08** against a $245.29 stop (21¢
slippage — normal). The mechanism works end to end: broker-side GTC stop
triggered without any of our code running.

- [x] Position reduced at Alpaca; fill price ≈ stop level
- [x] Stop order consumed cleanly, no orphan orders

**It also exposed a real gap — the fractional stub.** Alpaca stops only accept
whole shares, so 39 of 39.4973 sold and **0.4973 shares ($121.50) remain, with
no stop**. That is why the invariant now reads 11 stops vs 12 positions.

- [ ] **Verify the fix handles it:** the stop reconciler skips sub-1-share
      positions (`whole < 1: continue`) — correct, since Alpaca cannot protect
      them — but the stub still counts as a position. Decide: either close the
      stub manually (recommended — $121 of unmanaged exposure), or accept
      stubs and count the invariant as "stops == positions with ≥1 share".
      Once positions are whole-share (P0-5), new stubs stop appearing.
- [ ] **Cooldown check (the important one):** run `python main.py` (dry).
      NUE was stopped out, so if it's still in the top-N its signal must show
      the block — look for `upgrade_blocked_reentry_cooldown_broker` or
      `cooldown_blocked`. Before this session's fix, the next execute run would
      have bought NUE straight back. `get_recent_stop_fills` now sees this exact
      fill (verified reading it live: `NUE 39 @ $245.08`).

## Drill 2 — Dry-run rebalance shows exits again  *(10 min, any time)*

```powershell
python main.py
```

- [ ] Stage 7 log says `held-position source = Alpaca (11 positions)` (12 minus NUE)
- [ ] Positions no longer in the top-N produce **EXIT** signals (this was
      impossible from the cloud before P0-3 — the account grew to 12 names because of it)
- [ ] `[PROTECT]` section lists a dry-run stop plan for every position
- [ ] `data/portfolio_state.json` is written (the durable ledger)

## Drill 3 — Supervised execute, market open  *(the big one, ~20 min)*

Watch it live the first time. This exercises: targeted cancels, sell_with_cleanup,
whole-share deltas, exposure cap, and the stop reconciler.

```powershell
python main.py --execute
```

Pass conditions — verify in the Alpaca web console afterwards:

- [ ] EXIT orders filled (each log line shows `cancelled N resting order(s) first`)
- [ ] No order was rejected with "insufficient qty available" (P0-2 fixed)
- [ ] **Count of open SELL STOP orders == count of open positions** (P0-1 fixed —
      this is THE invariant; check it every time you touch anything)
- [ ] Stops on winners sit at trailed (max(entry,current)-anchored) levels, not entry-anchored
- [ ] All new buys are whole-share
- [ ] `stop_reconcile` block appears in the run summary with `failed: []`

## Drill 4 — Discord surfaces  *(10 min)*

- [ ] `/stoploss mode:check` shows `[resting @ broker]` levels matching the Alpaca console
- [ ] `/stoploss mode:execute` with nothing breached: replies "N checked, 0 breached" listing positions
- [ ] `/status` reflects the post-rebalance book
- [ ] Approve-sell button on a monitor alert (if one fires): sell fills, no rejection

## Drill 5 — Kill switch  *(5 min — never tested end-to-end, audit "still open" item)*

- [ ] `/pausetrading` → then `python main.py --execute` locally → executor logs
      the kill switch/skip and places **zero** orders
- [ ] `/resumetrading` → normal behaviour returns

## Drill 6 — One unattended weekly cycle

Let the Monday 09:45 scheduled proposal + button flow run without touching anything.

- [ ] Proposal posts to Discord with sane content (exits included now)
- [ ] After you tap Execute: same pass conditions as Drill 3
- [ ] `data/portfolio_state.json` committed by the workflow (check the bot commit)

---

## After all six drills

You have a system whose order paths are tested, exercised, and observable.
Then, in order:

1. **Screener retirement** (audit §4): you liquidate the screener account in the
   Alpaca UI (Step 1), then the code steps 2→5.
2. **Feature freeze** until 30 closed trades (audit §7.4). Track them in
   `data/trade_outcomes.json`.
3. Revisit exposure caps and the promotion-to-real-money gate (audit §7.7) with
   actual win-rate data.

## Standing invariants (check after ANY future change to broker/)

1. Open SELL STOPs == open positions **holding ≥1 whole share**. (Alpaca can't
   place a stop on a fractional stub; the reconciler skips them by design.
   Stubs should be closed manually — they're unmanaged exposure.)
2. Every sell in the codebase goes through `sell_with_cleanup` (test enforces).
3. No blanket `cancel_orders()` anywhere in an execute path (test enforces).
4. A stopped-out name is not re-bought within REENTRY_COOLDOWN_DAYS (test + Drill 1).
5. Cloud run and local run produce the same signals for the same inputs.
