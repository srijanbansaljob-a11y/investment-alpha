# Investment Alpha — Agent Briefing
# Read this first, then docs/ARCHITECTURE.md (the ownership rules and
# invariants any change must respect), then memory/AGENT_MEMORY.md for
# decision history.
# Last verified against the running system: 2026-07-31.

## What this is

A 7-factor quantitative stock-picking pipeline trading a **paper** Alpaca
account. It scores ~580 US stocks (S&P 500 + mid-cap) on momentum, quality,
valuation, trend, sentiment, volatility and PEAD, selects the regime-adjusted
top-N, sizes by inverse volatility, and places orders with a protective stop
resting at the broker for every position.

**Current status**: paper only, ~$114k equity, 4 closed trades. The strategy
has no proven edge yet. Treat every parameter as provisional.

**Screener: RETIRED 2026-08-03.** The `screener/` half of the repo, its three
workflows, the `/screener` command and the second Alpaca account are all gone.
The account was liquidated first, then the pipeline was made self-sufficient
for the data the screener used to publish (regime and stop/target levels now
come from `scripts/publish_pipeline_kv.py`), then the code was deleted — in
that order, per `docs/FABLE_AUDIT_2026-07-27.md` §4. If you find a reference
to a `screener/*` file, it is a leftover: the file does not exist.

---

## Non-negotiable rules for any agent working here

1. **Never place, cancel, or modify a live order.** Write preview-first
   scripts; the owner runs the `--execute` step. This includes "just testing".
2. **Every sell goes through `broker.alpaca_client.sell_with_cleanup()`.**
   A position with a resting GTC stop has its shares held against that order —
   a bare `close_position` is rejected by Alpaca. Never call it directly.
3. **Never add a second stop mechanism.** `stop_loss.reconcile_protective_stops()`
   is the only thing that places, moves, or removes protective stops. No
   bracket legs on buys.
4. **Never blanket-cancel orders.** `cancel_open_orders()` is deprecated and
   kills every protective stop on the account. Use `cancel_orders_for_symbol()`.
5. **Alpaca is the source of truth for what is held.** Files are caches.
6. **Run `python -m unittest discover -s tests` before you claim anything works.**
   30 tests guard the rules above (risk paths + architecture); CI runs them on
   every push. A failure in `test_architecture.py` usually means "route this
   through the existing owner", not "edit the test".
7. **Nothing auto-executes.** Every trade needs an owner button press or an
   owner-run command. Preserve this.
8. **Follow the change protocol** in `docs/ARCHITECTURE.md` §5 for anything
   touching `broker/`, `config.py`, order routing, or a Discord surface:
   name the owner → check blast radius → state the invariant at risk → add a
   test → update docs → exercise once on paper.

---

## The invariant

> **Count of open SELL STOP orders == count of open positions.**

Check it after any change to `broker/`. If it doesn't hold, positions are
unprotected and that is the highest-priority bug in the repo.

---

## Architecture

```
main.py                    # entry point: python main.py [--execute]
config.py                  # ALL tunable parameters — never hardcode elsewhere
pipeline/
  ingestion → features → scoring → filters → selection → portfolio → signals → output
  regime.py                # BULL/NEUTRAL/BEAR from VIX + SPX 200MA + yield curve + credit
  sentiment/insider/congressional.py   # alternative signals blended into sentiment
  feedback.py, learning.py, shadow.py  # adaptive weights (locked until 25+ observations)
  performance_tracker.py, postmortem.py
broker/
  alpaca_client.py         # THE broker interface: sell_with_cleanup, get_resting_stops,
                           #   cancel_orders_for_symbol, get_recent_stop_fills
  executor.py              # signals → orders; reconcile → exits → buys → stop reconcile
  stop_loss.py             # compute_stop_price/take_profit + reconcile_protective_stops
  monitor.py               # 15-min alerts (reads RESTING stops, never recomputed)
  remote_commands.py       # Discord slash commands / buttons (runs in GitHub Actions)
strategies/mean_reversion.py   # PAUSED (MR_ENABLED=False) — needs a real
                               #   capital carve-out first, see ARCHITECTURE §3
strategies/dual_momentum.py    # monthly advisory card, never trades
worker/index.js            # Cloudflare Worker: Discord front door
tests/test_risk_paths.py   # regression guards for order-placing logic
tests/test_architecture.py # guards the ownership rules / invariants
docs/ARCHITECTURE.md       # THE contract: single owners, invariants, protocol
docs/FABLE_AUDIT_2026-07-27.md   # findings + rationale for the current design
docs/UAT_CHECKLIST.md      # drills that must pass before real money
memory/                    # AGENT_MEMORY.md (decisions), SESSION_LOG.md (diary)
```

**State files**
- `data/portfolio_state.json` — durable ledger, **git-tracked**, so cloud runs
  aren't stateless. Supplies entry_date + pipeline-ownership tagging.
- `outputs/latest_portfolio.json` — legacy local mirror; gitignored.
- Holdings themselves always come from Alpaca.

---

## How execution works (`--execute`)

1. Kill switch (`EXECUTION_ENABLED`) + KV execution lock + market-open guard
2. Cooldown set built from the stop log **and** Alpaca's filled stop orders
3. Reconcile signals vs live Alpaca positions (HOLD→BUY upgrades are blocked
   for earnings-blackout and cooldown names)
4. EXITs first (each cancels that ticker's orders, then sells) — frees cash
5. BUYs: whole shares, delta-aware, gated by cash **and** the regime exposure cap
6. **Stop reconcile**: every held position ends with exactly one correct GTC
   stop, anchored to `max(entry, current)` so stops ratchet up and never down

---

## Key parameters (config.py)

- `PIPELINE_MAX_INVESTED_PCT` = bull 95% / neutral 75% / bear 40% — the single
  cap table; `MAX_INVESTED_PCTS` is derived from it. Raised 2026-07-27 on the
  reasoning that real broker-side stops bound downside better than idle cash.
  **Revisit at 30+ closed trades** — cash also hedges *model* risk.
- Stops: 2.5×ATR(14) in bull, clamped to a 3–12% band.
- Take-profit: ATR-based, 8–35% band — used as a monitor **alert only**, never
  a resting order (a hard ceiling amputates momentum's right tail).
- `REBALANCE_RANK_BUFFER=3`, `REENTRY_COOLDOWN_DAYS=5`, `MAX_POSITION_WEIGHT=0.20`,
  `EARNINGS_BLACKOUT_DAYS=5`, `MIN_FEEDBACK_OBSERVATIONS=25`.

---

## Technical constraints

- **CI runs Python 3.11** — match it locally; order-routing code shouldn't run
  on an untested interpreter.
- **Repo currently lives in OneDrive**, which corrupts files with trailing null
  bytes. Read JSON as `path.read_bytes().rstrip(b"\x00")`, write atomically,
  always `encoding="utf-8"`. Moving the repo out of OneDrive is a pending fix.
- `alpaca-py` (not `alpaca-trade-api`). yfinance for prices/fundamentals;
  `broker/market_data.py` gives real-time via Alpaca IEX → Finnhub → yfinance.
- Credentials in `.env` — never print them.

---

## Commands

```bash
python main.py                     # dry run, no trades
python main.py --execute           # places paper orders  (owner runs this)
python -m unittest discover -s tests -v
python scripts/protect_positions.py            # preview stop attachment
python broker/stop_loss.py                     # weekly check (dry by default)
python scripts/health_check.py
```

---

## Working agreements

- **Feature freeze until 30 closed trades.** Reliability and observability
  changes only. The machinery-to-evidence ratio is this project's main risk.
- One session = one concern. The worst regressions came from broad sessions
  touching order routing incidentally.
- Finish by updating `memory/AGENT_MEMORY.md` (decisions) and
  `memory/SESSION_LOG.md` (what happened).
- Keep this file true. A stale briefing makes every future session start wrong.
