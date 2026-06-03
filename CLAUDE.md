# Investment Alpha — Claude Code Briefing
# Read this first before doing anything.

## What this project is

A 7-factor quantitative stock-picking pipeline that:
- Scores 618 US stocks (S&P 500 + mid-cap) across momentum, quality, valuation, trend, sentiment, volatility, and PEAD
- Selects the top 10 stocks monthly, sizes positions using inverse-volatility weighting
- Executes paper trades via Alpaca paper trading API
- Tracks performance over a 3-month validation period
- Gradually learns which factors work best by adjusting weights after 25+ observations

**AUM**: ~$102,000 paper money on Alpaca  
**Portfolio**: 10 stocks, monthly rebalance, weekly stop-loss check  
**Regime**: BULL (VIX=18.8, SPX +9.8% above 200MA)  
**Paper trading started**: 2026-05-01 (day 14 of 90)

---

## Current portfolio (as of 2026-05-15)

All 10 positions are live in Alpaca paper account:

| # | Ticker | Company | Entry | Weight |
|---|---|---|---|---|
| 1 | MRK | Merck | $113.41 | 12.1% |
| 2 | HST | Host Hotels | $21.54 | 10.9% |
| 3 | MATX | Matson | $182.25 | 8.3% |
| 4 | INVA | Innoviva | $22.86 | 12.6% |
| 5 | BMY | Bristol-Myers | $56.77 | 12.0% |
| 6 | HAS | Hasbro | $95.65 | 9.2% |
| 7 | CHRD | Chord Energy | $142.54 | 6.7% |
| 8 | GM | General Motors | $77.75 | 9.3% |
| 9 | EIX | Edison Intl | $70.73 | 12.6% |
| 10 | DAL | Delta Air Lines | $71.55 | 6.7% |

---

## Project structure

```
main.py                    # entry point — run with: python main.py [--execute]
config.py                  # all tunable parameters
pipeline/
  ingestion.py             # Stage 1: downloads prices + fundamentals (yfinance)
  features.py              # Stage 2: computes 20+ signals per stock
  scoring.py               # Stage 3: 7-factor weighted composite score
  filters.py               # Stage 4: 200MA, volatility, liquidity, sector cap
  selection.py             # Stage 5: picks top N by score
  portfolio.py             # Stage 6: inverse-vol position sizing
  signals.py               # Stage 7: BUY/HOLD/EXIT vs prior portfolio
  output.py                # Stage 8: saves JSON, Excel, HTML dashboard
  regime.py                # BULL/NEUTRAL/BEAR from VIX + SPX 200MA + yield curve
  feedback.py              # adaptive weight learning (needs 25 obs minimum)
  performance_tracker.py   # paper trading P&L snapshots
  sentiment.py             # analyst revision scores (via yfinance)
  insider.py               # SEC EDGAR Form 4 open-market purchases
broker/
  executor.py              # orchestrates Alpaca order execution
  alpaca_client.py         # Alpaca SDK wrapper
  stop_loss.py             # ATR-based weekly stop-loss checker
memory/
  AGENT_MEMORY.md          # persistent project memory
  SESSION_LOG.md           # session-by-session diary
outputs/                   # timestamped JSON, Excel, HTML dashboards
data/                      # cache files, learned weights, paper trading log
```

---

## Alpaca credentials

Stored in `.env` file. Paper trading account. Keys already configured and tested — connection verified working.

```
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

Do NOT print the .env file to screen — keys should stay private.

---

## Three tasks to implement (in priority order)

### TASK 1 — Alpaca-first portfolio reconciliation (MOST IMPORTANT)

**Problem**: `broker/executor.py` uses `outputs/latest_portfolio.json` as its memory of what's held. But Alpaca is the real truth. They diverge when:
- Cash runs out and buys are skipped (happened on first run)
- User manually buys/sells stocks in Alpaca
- Orders partially fill

**Fix — implement in `broker/executor.py`**:

At the start of every `--execute` run, before generating any signals:
1. Fetch actual live positions from Alpaca (`client.get_all_positions()`)
2. Compare against the pipeline's target portfolio (this run's top 10)
3. Generate corrective orders:
   - In Alpaca but NOT in target → EXIT (sell)
   - In target but NOT in Alpaca → BUY (regardless of what state file says)
   - In both but weight has drifted >3% from target → rebalance (trim or add)
4. Manual positions (in Alpaca but not selected by the model) → log a WARNING and respect a config flag:
   - `MANUAL_POSITION_ACTION = "keep"` — leave it, just log it
   - `MANUAL_POSITION_ACTION = "exit"` — sell it to maintain model purity

The state file (`latest_portfolio.json`) should still be written but used ONLY for entry price/date tracking, not as the source of truth for what's held.

Config flags to add to `config.py`:
```python
ALPACA_RECONCILE_ON_EXECUTE = True      # always sync with Alpaca before trading
ALPACA_WEIGHT_DRIFT_THRESHOLD = 0.03   # rebalance if weight drifts >3%
MANUAL_POSITION_ACTION = "keep"         # "keep" or "exit" manual positions
```

### TASK 2 — Congressional trading signal

**Background**: Under the STOCK Act (2012), all US senators and representatives must disclose trades within 45 days. Data is public and has shown 5-10% annual alpha, especially from members of Intelligence and Armed Services committees.

**Implement as `pipeline/congressional.py`**:

Structure it identically to `pipeline/insider.py`. Fetch from:
- Senate: `https://efts.senate.gov/LATEST/search-index?q=%22stock%22&dateRange=custom&startDate={90_days_ago}&endDate={today}&senator=&committee=`
- Or use the Capitol Trades API (free, cleaner): `https://api.capitoltrades.com/trades?pageSize=100&issuer={ticker}`

Signal scoring (return float in [-1, +1]):
- +1.0 : 3+ senators bought in last 90 days, >$50k each
- +0.5 : 1-2 senators bought
- 0.0  : no signal or mixed
- -0.5 : net selling

Blend into the existing sentiment score in `scoring.py` at 30% weight alongside the insider signal (which currently has 40% weight within sentiment). Keep it behind a config flag `CONGRESSIONAL_ENABLED = True`.

Cache results in `data/congressional_cache.json` with 24-hour TTL (same pattern as insider.py).

### TASK 3 — Dead ticker pruning

Remove 37 confirmed-delisted/acquired tickers from `ALL_TICKERS` in `config.py`. They generate 404 errors on every run and waste ~2 minutes of fetch time.

Tickers to remove:
`ANSS, PXD, HES, MRO, CMA, DAY, DFS, FI, FLT, JNPR, IPG, K, MMC, WBA, WRK, JAMF, PING, NAPA, CADE, PGTI, AMNB, AXNX, BECN, HTLF, GES, DINE, AVLR, CEIX, AZEK, ZI, COUP, HMST, MNRL, HMLP, ROIC, CIVI, PTVE, FOX`

---

## Key technical constraints

- **Python**: 3.14 on Windows (`C:\Users\srija\AppData\Local\Python\pythoncore-3.14-64\python.exe`)
- **OneDrive sync corruption**: Files sometimes get trailing null bytes (`\x00`). Always read JSON files in binary mode and strip nulls: `data = path.read_bytes().rstrip(b'\x00'); json.loads(data)`
- **yfinance**: Used for all price/fundamental data. No paid API keys needed.
- **Alpaca SDK**: `alpaca-py` (not `alpaca-trade-api`). Use `TradingClient` from `alpaca.trading.client`.
- **File encoding**: Always use `encoding="utf-8"` when writing files.
- **No f-strings with backslashes inside expressions** — causes SyntaxError in some Python versions. Use string concatenation or `.format()` instead.

---

## How to run

```bash
# Dry run (no trades)
python main.py

# Live execution (places orders on Alpaca paper account)
python main.py --execute

# Weekly stop-loss check
python broker/stop_loss.py

# Test Alpaca connection
python -c "from dotenv import load_dotenv; load_dotenv(); from alpaca.trading.client import TradingClient; import os; c=TradingClient(os.getenv('ALPACA_API_KEY'),os.getenv('ALPACA_SECRET_KEY'),paper=True); print(c.get_account().status)"
```

---

## Memory system

After completing tasks, update:
- `memory/AGENT_MEMORY.md` — add decisions to KEY DECISIONS LOG, update last-updated date
- `memory/SESSION_LOG.md` — append a new SESSION entry with what was done

---

## Phase history

- **Phase 1-2**: Basic 4-factor pipeline (momentum, trend, quality, volatility)
- **Phase 3**: Added valuation + analyst sentiment + SEC insider signals
- **Phase 4** (complete): Yield curve regime downgrade, ATR stops, inverse-vol sizing, soft 200MA boundary, feedback learning guard, performance tracker, PEG-adjusted valuation, soft sector cap, earnings blackout
- **Next (Tasks 1-3 above)**: Alpaca reconciliation, congressional signal, dead ticker cleanup
