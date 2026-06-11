# 📋 Session Log — Investment Alpha
# Purpose: Running log of every agent session, decisions made, and insights learned

---

## SESSION 001 — 2026-04-30

**Topics Covered**:
1. Monthly rebalancing trade count estimation
   - Full 10-stock portfolio: 10–14 trades/month
   - Low turnover scenario: 6–10 trades
   - High turnover scenario: 14–18 trades
   - Monthly gross $ traded: ~25–35% of AUM

2. Dollar volume by AUM scaling
   - $100K AUM → ~$30K/month traded
   - $500K AUM → ~$150K/month traded
   - $1M AUM → ~$300K/month traded

3. €1,000 portfolio configuration
   - Problem: fractional shares needed, zero-commission mandatory
   - Solution: 5 positions at €200 each, quarterly rebalancing
   - ±5% drift threshold to avoid over-trading
   - Average trade size: €25–€50 at this AUM

**Decisions Made**:
- Starting capital: €1,000
- 5 positions (not 10)
- Quarterly rebalancing
- Zero-commission broker required

**Open Items from this session**:
- Broker selection pending
- Paper vs live trading — not yet decided
- Stock universe not yet defined

---

<!-- New sessions will be appended below -->

---

## SESSION 002 — 2026-04-30

**Topics Covered**:

1. **Monthly rebalancing trade count & dollar volume**
   - Full 10-stock portfolio: 10–14 trades/month average
   - Low turnover (1–2 changes): 6–10 trades
   - Moderate turnover (2–3 changes): 10–14 trades ← most likely
   - High turnover (4+ changes): 14–18 trades
   - Transaction cost drag at 30% turnover: ~25bps/year on $1M AUM
   - Drift threshold: ±1.5–2% recommended to avoid over-trading at scale

2. **AUM scaling table for dollar volume**
   - $100K → ~$30K/month traded
   - $500K → ~$150K/month traded
   - $1M → ~$300K/month traded
   - $5M → ~$1.5M/month traded
   - $10M → ~$3M/month traded

3. **€1,000 portfolio deep-dive**
   - Each position = €100 (10-stock model) — too small for whole shares of most stocks
   - Fractional shares mandatory (NVDA ~$900, MSFT ~$420, AAPL ~$195)
   - Even €1 flat commission = 3–4% friction per trade — fatal at this size
   - Recommended: reduce to 5 positions at €200 each
   - Quarterly rebalancing (not monthly) to minimize friction
   - Drift threshold widened to ±5%
   - Average trade size: €25–€50
   - Recommended brokers: Alpaca (best for API), Trading212, DEGIRO

4. **Scaling projection**
   - Month 6: ~€1,587 → consider 5→7 positions
   - Month 12: ~€2,159 → 8 positions viable
   - Month 24: ~€4,661 → full 10-stock model viable

5. **Memory/context persistence system created**
   - Built `memory/AGENT_MEMORY.md` — structured persistent context file
   - Built `memory/SESSION_LOG.md` — running session diary (this file)
   - Added agent instruction to `RUN_INSTRUCTIONS.md` to read memory first
   - Memory files live in OneDrive → available on all devices automatically

6. **Moving agent to another PC**
   - Project files: auto-sync via OneDrive ✅
   - Claude account + project instructions: auto-sync via Anthropic login ✅
   - Python packages: manual — run `pip install -r requirements.txt` ❌
   - API keys (.env): syncs via OneDrive, verify they work ✅
   - Scheduled tasks: manual — re-run `setup_task_scheduler.ps1` ❌
   - Cowork plugins: manual — re-enable from marketplace ❌

7. **Chat history limitation discovered**
   - Cowork (desktop app) stores chat history locally — does NOT fully sync across devices
   - Old conversations visible on original machine only
   - This is expected behaviour — Cowork is still in research preview
   - **Solution**: memory files in OneDrive replace chat history as source of truth
   - On new PC: connect folder → say "Read memory/AGENT_MEMORY.md and SESSION_LOG.md and continue"

**Decisions Made**:
- Memory system established as primary continuity mechanism
- Session save habit: say "Save this session to memory" at end of each conversation
- Agent must always read memory files before executing any workflow

**New Constraints Added to AGENT_MEMORY.md**:
- System portability section added
- New PC setup checklist added
- 5 new entries in Key Decisions Log

**Open Items Carried Forward**:
- Broker selection still pending (Alpaca recommended)
- Paper vs live trading — not yet decided
- Stock universe not yet defined (S&P 500? Euro Stoxx? Mixed?)
- Tax jurisdiction for EUR portfolio not yet addressed

---

## SESSION 003 — 2026-05-01

**Context**: Project transferred to new PC. Agent restored from memory files, performed health check, fixed transfer issues, then implemented 10 Phase 4 alpha improvements.

**Transfer Issues Fixed**:
1. `config.py` had 1,098 null bytes from OneDrive sync corruption → stripped in binary mode
2. `config.py` had duplicate fragment (lines 322–346) → removed with Edit tool
3. `.bat` files were using wrong Python interpreter → hardcoded `C:\Users\srija\AppData\Local\Python\pythoncore-3.14-64\python.exe`
4. `alpaca-trade-api` was installed but code uses `alpaca-py` → installed correct SDK
5. `requests` was missing from `requirements.txt` → added

**Phase 4 Improvements Implemented** (all 10 from top-0.1%-investor evaluation):

| # | Improvement | File(s) | Status |
|---|---|---|---|
| 1 | Skip-month momentum (already in rel_strength) | features.py | ✅ existing |
| 2 | 52-week high proximity (George & Hwang 2004) | features.py, scoring.py | ✅ implemented |
| 3 | PEAD — earnings surprise sub-score | ingestion.py, features.py, scoring.py | ✅ implemented |
| 4 | ROIC, accruals, asset growth, margin expansion | ingestion.py, features.py, scoring.py | ✅ implemented |
| 5 | Inverse-volatility allocation (`inv_vol` mode) | portfolio.py, config.py | ✅ implemented |
| 6 | Soft 200MA boundary (hard: -3%, soft: -3% to 0, penalty: 15%) | filters.py, config.py | ✅ implemented |
| 7 | Yield curve + credit spread regime downgrade | regime.py, config.py | ✅ implemented |
| 8 | ATR-based stop-losses (2.5× / 2.0× / 1.5×) | broker/stop_loss.py, config.py | ✅ implemented |
| 9 | Feedback guard: 25 obs minimum before weight update | pipeline/feedback.py, config.py | ✅ implemented |
| 10 | Performance tracker: 3-month paper trading validator | pipeline/performance_tracker.py (new) | ✅ built |

**New Files Created**:
- `pipeline/performance_tracker.py` — daily/weekly portfolio snapshots, Sharpe, drawdown, alpha vs SPY, HTML dashboard, CSV export. Auto-runs after each `main.py` cycle when `PAPER_TRADING_VALIDATION=True`.

**Config Changes (config.py)**:
- `ALLOCATION_MODE = "inv_vol"`
- `YIELD_CURVE_ENABLED = True`, `YIELD_CURVE_BEAR_THRESHOLD = -0.50`
- `CREDIT_SPREAD_ENABLED = True`, `CREDIT_SPREAD_BEAR_PCT = -0.03`
- `USE_ATR_STOP_LOSS = True`, `ATR_PERIOD = 14`, `ATR_STOP_MULTIPLIER = {bull:2.5, neutral:2.0, bear:1.5}`
- `MA200_SOFT_ZONE = 0.03`, `MA200_SOFT_PENALTY = 0.85`, `MA200_HARD_EXCLUDE = -0.03`
- `EXTENDED_FUNDAMENTALS_ENABLED = True`
- `MIN_FEEDBACK_OBSERVATIONS = 25`
- `PAPER_TRADING_VALIDATION = True`, `PAPER_TRADING_START_DATE = "2026-05-01"`, `PAPER_TRADING_MONTHS = 3`
- `BENCHMARK_TICKER = "SPY"`

**Decisions Made**:
- Paper trading validation starts 2026-05-01, runs for 3 months
- System will self-evaluate factor correlations monthly but will NOT update weights until 25 position-month observations are accumulated (≈ 5 positions × 5 months)
- After 3-month validation, review alpha, Sharpe, and max drawdown vs SPY before going live

**Open Items Carried Forward**:
- Broker selection still pending (Alpaca paper account recommended first)
- Stock universe: US (S&P 500 + mid-cap) — confirmed in config as ALL_TICKERS (618 tickers)
- Tax jurisdiction for EUR portfolio not yet addressed
- Go/no-go review scheduled for ~2026-08-01 (3 months from today)

---

## SESSION 004 — 2026-05-02

**Context**: Continuation from SESSION 003. All Phase 4 improvements confirmed live. Focus was on running the pipeline end-to-end, diagnosing crashes, and improving the HTML dashboard.

**Issues Diagnosed & Fixed**:

1. **OneDrive null-byte corruption (recurring)** — `latest_portfolio.json` repeatedly getting corrupted with trailing `\x00` bytes after OneDrive sync. Fixed permanently in `broker/stop_loss.py` → `_load_portfolio_state()` now:
   - Reads file in binary mode and strips null bytes before JSON parsing
   - If still corrupt, auto-restores from the most recent timestamped `portfolio_YYYYMMDD_HHMMSS.json` backup
   - No longer crashes the pipeline — silently self-heals

2. **main.py "closes after 10 seconds"** — Diagnosed: not a crash, script completes in ~7.6s and the Command Prompt window closes automatically when double-clicked. Fix: run from an already-open terminal (`cmd` in address bar → `python main.py`).

**New Feature: Dashboard Paper Trading Progress Section**

Added to `pipeline/output.py` → `save_dashboard()`:
- `_load_paper_trading_data()` — reads latest snapshot from `data/paper_trading_log.json`
- `_load_stop_loss_data()` — reads `outputs/stop_loss_log.json` for stop prices
- New CSS classes: `.prog-bar-*`, `.kpi-row`, `.kpi-box`, `.pos-gain/loss`, `.sl-bar-*`, `.accum-bar-*`
- New HTML section rendered at bottom of every dashboard:
  - Progress bar: Day X of 90 with % complete
  - 4 KPI boxes: Portfolio Value (€), Total Return %, Alpha vs SPY %, Sharpe + Max DD
  - Position P&L table: Entry / Current / P&L% / P&L€ / Stop-Loss Distance bar per ticker
  - Factor weight learning bar: accumulated obs / 25 needed, with status message

**Pipeline Run Results (2026-05-02)**:
- Regime: BULL | VIX=17.0 | SPX +7.5% above 200MA
- 579/618 tickers processed (37 dead tickers generate harmless 404s)
- All 10 positions: HOLD (MATX, FDX, DVN, BIIB, HAS, VZ, INVA, INCY, EQT, DD)
- No stop-losses triggered
- Paper trading: Day 1 of 90 — portfolio at €1,000, 0% return (baseline)
- Runtime: ~8 seconds (cached data)

**Confirmed Working (Phase 4 stack)**:
- Soft 200MA boundary: stocks -3% to 0% below MA penalised 15%, hard excluded below -3%
- ATR stops: stop = entry − (2.5× ATR₁₄) in BULL regime
- Feedback guard: weight updates blocked until 25 obs accumulated (currently 0/25)
- Regime yield curve + credit spread downgrade: both signals healthy today

**How to Run**:
- Open terminal in project folder (`cmd` in address bar of File Explorer)
- `python main.py` — full pipeline, dry run, ~8 seconds
- `python main.py --execute` — live mode (once Alpaca connected)
- `python broker/stop_loss.py` — weekly stop-loss check standalone
- Dashboard auto-saved to `outputs/dashboard_YYYYMMDD_HHMMSS.html` every run

**Open Items Carried Forward**:
- Broker: Alpaca paper account connected and live (10 positions open since 2026-05-01)
- Dead tickers: pruned this session (see SESSION 005)
- Go/no-go review scheduled ~2026-08-01

---

## SESSION 005 — 2026-05-15

**Context**: Day 14 of 90 paper trading validation. AUM ~$102,000. BULL regime. Implemented the three priority tasks from CLAUDE.md.

**TASK 1 — Alpaca-first Portfolio Reconciliation (broker/executor.py)**

Rewrote `broker/executor.py` to make Alpaca the source of truth on every `--execute` run.

Key changes:
- New `_reconcile_signals()` function called before any orders are placed:
  - HOLD signal + position absent from Alpaca → upgrades to BUY (catches missed entries, partial fills, manual closes)
  - Position exists but weight drifted >3% from target → flags for delta rebalance
  - Ticker in Alpaca but not in target → EXIT or KEEP depending on `MANUAL_POSITION_ACTION` config
- BUY loop is now delta-aware: computes `delta_qty = target_qty - existing_qty`; buys the gap or trims the excess rather than always buying the full target amount
- TRIM action added: when overweight by >3%, sells the excess shares as a SELL order
- `latest_portfolio.json` is now entry-price/date tracking only — NOT the source of truth for held positions

New config flags added to `config.py`:
- `ALPACA_RECONCILE_ON_EXECUTE = True`
- `ALPACA_WEIGHT_DRIFT_THRESHOLD = 0.03`
- `MANUAL_POSITION_ACTION = "keep"`

**TASK 2 — Congressional Trading Signal (pipeline/congressional.py)**

Created `pipeline/congressional.py` — structurally identical to `insider.py`.

Key implementation:
- Fetches STOCK Act disclosures from Capitol Trades API: `https://api.capitoltrades.com/trades`
- Filters to last 90 days, trades ≥ $50k
- Signal scoring: 3+ buys = +1.0; 1-2 buys = +0.5; mixed = 0.0; net selling = -0.5
- 24-hour cache in `data/congressional_cache.json` (binary null-byte safe read, same OneDrive corruption fix)
- `run(tickers)` returns `{congressional_signals: dict, tickers_fetched: int, status: str}`

Integration points updated:
- `main.py`: imported `congressional_module`; added Stage 2D block to inject `congressional_signal` column into features DataFrame
- `pipeline/scoring.py`: sentiment blend now 3-way when both insider and congressional present: analyst=0.30 + insider=0.40 + congressional=0.30; falls back to 2-way (analyst=0.60 + insider=0.40) if only insider active

**TASK 3 — Dead Ticker Pruning (config.py)**

Removed 38 confirmed-delisted/acquired tickers from `ALL_TICKERS`:

SP500 removed (17): ANSS, PXD, HES, MRO, CMA, DAY, DFS, FI, FLT, JNPR, IPG, K, MMC, WBA, WRK, PTVE, FOX
MIDCAP removed (21): JAMF, PING, ZI, COUP, AVLR, AXNX, CADE, HTLF, HMST, AMNB, BECN, AZEK, PGTI, CEIX, DINE, NAPA, GES, CIVI, MNRL, HMLP, ROIC

Universe: 618 → 580 tickers. Verified: none of the removed tickers appear in ALL_TICKERS.
Saves ~2 minutes per full run by eliminating 404 errors on every yfinance fetch.

**Other config.py additions**:
- `DATA_DIR = BASE_DIR / "data"` — proper Path object for cache files (was defaulting to relative string "data")
- `DATA_DIR.mkdir(exist_ok=True)` — auto-creates the data/ directory

**Decisions Made**:
- Alpaca reconciliation always-on (`ALPACA_RECONCILE_ON_EXECUTE=True`)
- Manual positions kept not exited by default (`MANUAL_POSITION_ACTION="keep"`) — model purity can be tightened later
- Congressional signal enabled (`CONGRESSIONAL_ENABLED=True`) — adds alpha from STOCK Act data
- Ticker pruning confirmed and executed

**Open Items Carried Forward**:
- Monitor congressional signal quality over first 30 days (may need to tune MIN_TRADE_USD)
- Weight drift threshold (3%) may need empirical tuning after first full rebalance cycle
- Go/no-go live trading review scheduled ~2026-08-01 (Day 90 = 2026-07-29)

---

## SESSION 005 — 2026-05-25 (Intraday Monitor)

**Questions Answered**:
- "Should I run it today?" — NO. Today is Memorial Day (US markets closed). Run Tuesday 2026-05-26.
- "Isn't weekly monitoring dangerous?" — YES. Stop-loss levels can breach intraday. Built dedicated monitoring system.

**NEW: broker/monitor.py — Intraday Portfolio Monitor**

Persistent process that runs during market hours, checking Alpaca positions every 5 minutes.

Three triggers:
1. **Stop-loss breach** — price < ATR-based stop (same formula as stop_loss.py). Auto-executes.
2. **Profit target hit** — position up ≥ 20% from entry. Auto-executes.
3. **Sharp intraday move** — ±5% from today's open. **Alert-only, no auto-execute** (may be a reversal).

5-minute auto-execute window:
- On breach: sends Discord alert immediately → logs to data/pending_actions.json with execute_after timestamp
- If no override received within 5 minutes → market sell order submitted to Alpaca
- Override: `python broker/monitor.py --override TICKER` or edit pending_actions.json

Discord alerts (free, no bot needed):
- Rich embeds with color-coded severity (red=stop, green=profit, orange=sharp move)
- Sent via Discord webhook (no bot account required)
- Setup: Create Discord server → channel → Integrations → Webhooks → Copy URL → add to .env

Config additions (config.py):
```python
DISCORD_WEBHOOK_URL        = os.getenv("DISCORD_WEBHOOK_URL", "")  # from .env
PROFIT_TARGET_PCT          = 0.20   # 20% gain triggers alert + sell queue
INTRADAY_MOVE_ALERT_PCT    = 0.05   # ±5% intraday triggers alert (no auto-execute)
AUTO_EXECUTE_DELAY_MINUTES = 5      # minutes before auto-executing queued sell
MONITOR_INTERVAL_SECONDS   = 300    # check every 5 minutes
```

Usage:
```bash
python broker/monitor.py --test     # verify Discord webhook
python broker/monitor.py            # start monitoring loop
python broker/monitor.py --dry-run  # monitor without executing
python broker/monitor.py --override AAPL   # cancel queued sell for AAPL
```

Files created/modified:
- `broker/monitor.py` — NEW: full monitoring loop (get_positions → check triggers → Discord → auto-execute)
- `config.py` — added DISCORD_WEBHOOK_URL + 4 monitoring params
- `data/pending_actions.json` — auto-created at runtime, tracks queued executions

**Decisions Made**:
- Sharp intraday moves are ALERT-ONLY: large single-day moves are often reversals, not exits
- Auto-execute delay is 5 min: short enough to limit slippage, long enough to cancel a mistake
- Uses existing ATR-stop logic from stop_loss.py (DRY — no duplicate code)
- Discord chosen over email/SMS: free, cross-platform, works on phone + desktop

**Next Steps**:
1. Create Discord server + webhook (5 minutes) — instructions in broker/monitor.py docstring
2. Add DISCORD_WEBHOOK_URL to .env
3. Test: `python broker/monitor.py --test`
4. On trading days: run `python broker/monitor.py` in a terminal window during market hours

**Open Items Carried Forward**:
- Monitor congressional signal quality over first 30 days
- Go/no-go live trading review ~2026-08-01 (Day 90 = 2026-07-29)
- Consider automating monitor startup via Windows Task Scheduler (starts at market open 9:30 ET)

---

## SESSION 006 — 2026-06-10 — Discord Approval System + Learning Overhaul + Strategy Sleeves

**What was done** (two waves, both deployed via Cowork):

*Wave 1 — Discord button approval system:*
- monitor.py is now ALERT-ONLY: the 5-min auto-sell countdown was REMOVED (GitHub Actions is stateless, countdown unenforceable; approval-first design). Stop-loss/profit-target alerts carry ✅ Approve / ❌ Reject buttons.
- New broker/discord_notify.py (bot-API messages with buttons, channel-history dedupe replacing pending_actions.json in cloud).
- New broker/remote_commands.py (cloud dispatcher; ALL state from Alpaca API).
- New worker/ (Cloudflare Worker: Ed25519 verification, OWNER_ID lock, GitHub repository_dispatch bridge). Deployed at investment-alpha-bot.srijan-alpha.workers.dev.
- Slash commands: /status /regime /monitor /stoploss /pipeline /help (scripts/register_discord_commands.py). Execute-class commands require a confirm button.
- Workflows: command.yml (repository_dispatch), daily_summary.yml (9AM ET, DST-safe), monitor.yml cron widened to 13:00–21:45 UTC (DST gap fixed).
- Fixed latent bug: stop_loss.py imported nonexistent get_trading_client (→ get_client).
- End-to-end verified: /status works from Discord; routing tests pass (stranger blocked, reject never trades, execute needs confirm).

*Wave 2 — correctness + learning + strategies:*
- regime.py _safe_fallback now returns NEUTRAL (was BULL — data outage meant silent max-risk-on). REGIME_FALLBACK in config.
- /regime card shows inputs (VIX, SPX vs 200MA, yield curve, credit) + why.
- New broker/market_data.py: real-time prices/opens/ATR via Alpaca IEX → Finnhub → yfinance. monitor.py + stop_loss.py rewired (fixes 15-min Yahoo lag for intraday decisions).
- New pipeline/shadow.py: logs top-30 with factor scores every run (3× observations, kills selection bias). Hooked into main.py Stage 5B.
- New pipeline/learning.py: weekly, per-regime weights (bull/neutral/bear), EWMA IC, gentle 2% drift; exports active regime's weights to learned_weights.json (scoring.py unchanged).
- New pipeline/postmortem.py: stop-loss post-mortem (recovered = too tight → ATR multiplier suggestions, STOP_TUNING_AUTO=False) + decision journal review (human vs model scoring).
- Decision journal: every ✅/❌ logged to data/decision_journal.json, committed back to repo by workflows.
- New strategies/mean_reversion.py: RSI(2)<10 dips in uptrends, 10% sleeve, 5 slots, button-approved buys (approve_buy flow added to worker + remote_commands), exits on 5-day MA snap-back or time stop.
- New strategies/dual_momentum.py: monthly SPY/VEU/AGG vs BIL compass, advisory only.
- New workflows: strategies.yml (daily 21:30 UTC), learning.yml (Sat 12:00 UTC); both + command.yml commit data/*.json state back to the repo.

**Key decisions**:
- Nothing trades without an explicit owner button press. No auto-execution anywhere.
- Data failure = NEUTRAL regime, never BULL.
- GitHub repo doubles as the persistence layer for journals/sleeve state (commit-back pattern).
- TradersPost evaluated as off-the-shelf alternative; rejected in favor of custom Discord buttons ($0, full control).

**Watch-outs**:
- OneDrive sync corrupts/truncates files (null bytes AND truncation observed this session). Always verify after writes.
- Discord interaction tokens expire ~15 min: long pipeline runs fall back to fresh channel messages.

**Next steps**: push to GitHub, redeploy worker, watch first MR sleeve proposals and Saturday learning report. Go/no-go live review ~2026-08-01.
