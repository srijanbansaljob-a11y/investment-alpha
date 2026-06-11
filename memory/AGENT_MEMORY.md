# 🧠 Agent Memory — Investment Alpha Trading System
# Last Updated: 2026-06-10 (Session 006 — Discord approval system, learning overhaul, strategy sleeves)
# Purpose: Persistent context for the trading agent to learn from user decisions and constraints

---

## 👤 USER PROFILE

- **Name**: Srijan
- **Email**: srijanbansal@gmail.com
- **Experience Level**: Building quantitative trading system from scratch
- **Goal**: Learn and iterate — questions drive system evolution

---

## 💼 PORTFOLIO CONFIGURATION

| Parameter | Value | Decision Date | Reasoning |
|---|---|---|---|
| Starting Capital | €1,000 | 2026-04-30 | Validation/paper trading phase before scaling |
| Currency | EUR | 2026-04-30 | User base currency |
| Number of Positions | 5 (not 10) | 2026-04-30 | Reduced from 10 due to small portfolio size |
| Position Size | €200 each | 2026-04-30 | 10% → 20% per position at this scale |
| Cash Buffer | €0 (tight at this size) | 2026-04-30 | Monitor carefully |
| Rebalancing Frequency | Quarterly (not monthly) | 2026-04-30 | Reduced from monthly to cut friction at small scale |
| Drift Threshold | ±5% | 2026-04-30 | Wider than standard ±2% to avoid over-trading |

---

## 🔁 REBALANCING PARAMETERS (LEARNED)

- **Monthly rebalancing** typically requires **10–14 trades/month** at full scale
- At €1,000 scale → **3–5 trades per rebalancing cycle** (wider drift bands)
- **Monthly gross dollar traded**: ~25–35% of AUM = ~€250–€350 at this size
- **Average trade size**: €25–€50 at current AUM
- **Commission requirement**: MUST be €0 — even €1 flat fee = 3–4% friction per trade

---

## 🏦 BROKER REQUIREMENTS (LEARNED)

| Requirement | Reason |
|---|---|
| Zero commission | €25–50 avg trade size makes any fee prohibitive |
| Fractional shares | €100–200 positions can't buy whole shares of most stocks |
| API access | Required for automated pipeline execution |
| EUR-denominated | User base currency |

**Recommended brokers**:
- **Alpaca** — zero commission, fractional shares, full API (best for automation)
- **Trading212** — EU-based, zero commission, fractional shares
- **DEGIRO** — EU-based, low cost, limited fractional support

---

## 📈 SCORING ENGINE (SYSTEM CONFIG — Phase 4)

**Composite Score** (6-factor, adaptive weights via feedback.py):
```
Score = 0.28×Momentum + 0.20×Trend + 0.18×Quality + 0.14×Valuation + 0.10×Sentiment
      - 0.10×Volatility + 0.05×EarningsSurprise (PEAD)
```
Weights adapt via Spearman-rank feedback after ≥25 accumulated position-month observations.
Learned weights saved to `data/learned_weights.json` and loaded by scoring.py.

**Phase 4 Factors active**:
- Momentum: ret_3m, ret_6m, ret_12m, rel_strength_12m, **52-week high proximity** (George & Hwang 2004)
- Trend: SMA50, SMA200, RSI(14) centred, MACD histogram
- Quality: ROE, earnings_growth, gross_margin, FCF_yield, **ROA (ROIC proxy)**, **accruals_ratio** (lower=better), **asset_growth** (lower=better, Cooper 2008), **op_margin_change**
- Valuation: pe_vs_sector, ev_vs_sector (relative to sector median)
- Sentiment: analyst_score (target price upside + rec), insider_signal (40% blend), congressional_signal (30% blend when enabled)
- Earnings Surprise: PEAD sub-score from last earnings_surprise_pct (Post-Earnings Announcement Drift)
- Volatility: vol_60d (subtracted from composite, higher vol = penalised)

**Allocation mode**: `inv_vol` — inverse-volatility weighting (compounders get more weight)

**Filters applied**:
- 200MA: soft boundary — >3% below = hard exclude; 0–3% below = 15% score penalty
- Exclude top 20% most volatile stocks
- Exclude low liquidity stocks
- Sector cap (SECTOR_MAX_STOCKS per sector)

**Regime detection** (runs pre-flight):
- Primary: VIX + SPX 200MA → Bull / Neutral / Bear
- Secondary: Yield curve (10Y-3M spread < -0.5pp) → downgrade one level
- Secondary: Credit spreads (HYG/LQD 20d momentum < -3%) → downgrade one level

**Stop-loss**: ATR-based — `stop = entry − (ATR_STOP_MULTIPLIER[regime] × ATR_14)`
- Bull: 2.5× ATR | Neutral: 2.0× ATR | Bear: 1.5× ATR
- Falls back to fixed % if ATR unavailable

---

## 🚫 RISK RULES (LEARNED + SYSTEM)

- No commission broker mandatory at this AUM
- Fractional share support mandatory
- Minimum hold period consideration: 30–60 days (tax efficiency)
- Do NOT rebalance unless drift > ±5% from target weight
- Paper trade first — validate signals before live execution

---

## 📅 SCALING ROADMAP (PROJECTED)

| Milestone | Portfolio Size | Action |
|---|---|---|
| Now | €1,000 | Paper trade / validate signals, 5 positions |
| Month 6 | ~€1,587 | Review signal accuracy, consider 5→7 positions |
| Month 12 | ~€2,159 | 8 positions viable, tighten drift threshold to ±3% |
| Month 24 | ~€4,661 | Full 10-stock model, monthly rebalancing viable |

---

## 🗂️ KEY DECISIONS LOG

| Date | Decision | Rationale |
|---|---|---|
| 2026-04-30 | Start with €1,000 | Test and validate system before scaling |
| 2026-04-30 | 5 positions instead of 10 | Better manageability at small AUM |
| 2026-04-30 | Quarterly rebalancing | Minimize friction; at €25–50/trade any commission is fatal |
| 2026-04-30 | Drift threshold ±5% | Avoid over-trading at small scale |
| 2026-04-30 | Must use zero-commission broker | Even €1 = 3–4% drag per trade |
| 2026-04-30 | Fractional shares required | Can't buy whole shares of NVDA/MSFT at €200 position |
| 2026-04-30 | Memory system created | `memory/AGENT_MEMORY.md` + `SESSION_LOG.md` to persist context across devices and sessions |
| 2026-04-30 | Chat history not reliable for continuity | Cowork is local per machine; memory files in OneDrive are the source of truth |
| 2026-04-30 | Agent must read memory files first | Every session starts with reading AGENT_MEMORY.md before any workflow execution |
| 2026-04-30 | Save session habit established | At end of each session say "Save this session to memory" to keep logs current |
| 2026-05-01 | Phase 4 alpha improvements implemented | 10 improvements from top-0.1%-investor evaluation — see SESSION 003 |
| 2026-05-01 | Paper trading validation: 3 months from 2026-05-01 | Validate signal quality before going live — performance tracked automatically |
| 2026-05-01 | Allocation switched to inv_vol | Low-volatility compounders get more weight; better Sharpe than equal-weight |
| 2026-05-01 | ATR stops replace fixed % stops | Volatility-adaptive stops avoid being stopped out in high-vol names prematurely |
| 2026-05-01 | Feedback guard: 25 obs minimum | Prevents overfitting weights to noise from 1-2 months of data |
| 2026-05-01 | PAPER_TRADING_START_DATE = 2026-05-01 | Hardcoded in config.py; performance_tracker.py counts from this date |
| 2026-05-15 | Alpaca-first reconciliation added to executor.py | Alpaca is now the source of truth; latest_portfolio.json used for entry tracking only |
| 2026-05-15 | Congressional signal added (pipeline/congressional.py) | STOCK Act data via Capitol Trades API; 30% weight in sentiment blend; 24h cache |
| 2026-05-15 | Sentiment blend updated: analyst(30%) + insider(40%) + congressional(30%) | Only activates when both insider and congressional signals present |
| 2026-05-15 | 38 dead/delisted tickers removed from ALL_TICKERS | Universe: 618 → 580; saves ~2 min per run; eliminates recurring 404 errors |
| 2026-05-15 | ALPACA_RECONCILE_ON_EXECUTE, ALPACA_WEIGHT_DRIFT_THRESHOLD=0.03, MANUAL_POSITION_ACTION="keep" added to config.py | Controls reconciliation behavior on --execute runs |
| 2026-05-15 | CONGRESSIONAL_ENABLED=True, CONGRESSIONAL_LOOKBACK_DAYS=90, CONGRESSIONAL_MIN_TRADE_USD=50000 added to config.py | Congressional signal config |
| 2026-05-15 | DATA_DIR = BASE_DIR / "data" added to config.py | Proper path for insider_cache.json and congressional_cache.json |

---

## 💻 SYSTEM PORTABILITY & MEMORY (LEARNED)

- Project files live on **OneDrive** → sync automatically across PCs
- Claude Cowork chat history is **local to each machine** — does NOT fully sync (research preview limitation)
- **Solution**: `memory/AGENT_MEMORY.md` + `memory/SESSION_LOG.md` replace chat history as source of truth
- On a new PC: open Cowork → connect OneDrive folder → say *"Read memory/AGENT_MEMORY.md and SESSION_LOG.md and continue from where we left off"*
- To move to new PC manually (non-OneDrive): copy project folder via USB/zip

**New PC Setup Checklist**:
- [ ] Install Claude Desktop from claude.ai/download
- [ ] Sign in with same account (srijanbansal@gmail.com)
- [ ] Connect OneDrive project folder in Cowork
- [ ] Run `pip install -r requirements.txt`
- [ ] Verify `.env` API keys still work
- [ ] Re-run `setup_task_scheduler.ps1` for scheduled tasks
- [ ] Re-enable Cowork plugins from marketplace

---

## 📝 OPEN QUESTIONS / NEXT STEPS

- [ ] Which broker will Srijan use? (Alpaca recommended for API pipeline)
- [ ] Paper trade or live from day 1?
- [ ] Which market/exchange? US stocks (NYSE/NASDAQ) or EU stocks?
- [ ] Tax jurisdiction considerations for EUR portfolio?
- [ ] Define universe of stocks to screen (S&P 500? Euro Stoxx? Mixed?)

---

## 🔄 HOW TO USE THIS FILE

The trading agent should:
1. **READ this file first** before executing any workflow stage
2. **Apply constraints** defined here (position size, drift threshold, broker, etc.)
3. **Append new decisions** to the "Key Decisions Log" after each session
4. **Update Open Questions** as they get resolved
5. **Never override** a logged decision without user confirmation

---
*This file is auto-updated by the agent. Do not delete.*

- **2026-05-02**: OneDrive null-byte corruption fixed permanently — `stop_loss.py` now auto-strips nulls and self-restores from timestamped backups. No more pipeline crashes from corrupted `latest_portfolio.json`.
- **2026-05-02**: Dashboard Paper Trading Progress section added to every run — shows Day X/90, P&L per position, stop-loss distance bars, Sharpe, alpha vs SPY, and factor weight accumulation tracker.
- **2026-05-02**: Confirmed all Phase 4 features live and working: soft 200MA penalty, ATR stops, feedback guard, yield curve + credit spread regime downgrade.
- **2026-05-02**: Paper trading baseline established — Day 1 of 90, portfolio €1,000, BULL regime, all 10 positions HOLD.
- **2026-05-15**: Alpaca-first reconciliation live — `executor.py` now reconciles vs live Alpaca positions on every `--execute` run before placing orders. Delta-aware BUY logic prevents double-buying partial fills.
- **2026-05-15**: Congressional signal live — `pipeline/congressional.py` fetches STOCK Act disclosures via Capitol Trades API; blended into sentiment at 30% alongside insider(40%) and analyst(30%).
- **2026-05-15**: 38 dead tickers pruned from `ALL_TICKERS` — universe is 580 (was 618). Saves ~2 min per run.
- **2026-05-15**: `DATA_DIR` added to config.py; new config flags for reconciliation and congressional signal.
