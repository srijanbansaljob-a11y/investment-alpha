# Investment Alpha — Phase 3 Run Instructions

> ⚡ **AGENT: Before executing any workflow, read `memory/AGENT_MEMORY.md` first.**
> This file contains user-specific constraints, portfolio config, and learned decisions
> that override system defaults. Then append your session summary to `memory/SESSION_LOG.md`.

**Last updated:** April 2026  
**Pipeline version:** Phase 3 (6-Factor Model)  
**Universe:** ~618 tickers (S&P 500 + Mid-Cap + Custom)

---

## What This Pipeline Does

Runs a fully systematic, multi-stage quantitative stock selection workflow each morning:

1. Downloads price + fundamental data (yfinance, cached)
2. Computes technical, momentum, quality, valuation, and analyst sentiment factors
3. Scores every stock on a 6-factor model
4. Filters out stocks below 200-day MA, high volatility, low liquidity
5. Selects top 10 holdings, adjusts for market regime (Bull/Bear/Neutral)
6. Outputs portfolio weights, trade signals, and Excel report
7. Optionally submits paper trades to Alpaca

---

## Scoring Model (Phase 3 — 6-Factor)

```
Score = 0.28 × Momentum  +  0.20 × Trend  +  0.18 × Quality
      + 0.14 × Valuation  +  0.10 × Sentiment  −  0.10 × Volatility
```

**Factor definitions:**
- **Momentum** — skip-month 3M/6M/12M returns + relative strength vs S&P 500
- **Trend** — % above SMA-50/200, RSI(14) score, MACD histogram
- **Quality** — ROE, earnings growth, FCF yield, gross margin, low debt/equity
- **Valuation** — P/E vs sector median, EV/EBITDA vs sector median (cheaper = better)
- **Sentiment** — analyst target price upside (capped 60%) + recommendation score
- **Volatility** — 60-day realised volatility (penalised, not rewarded)

Weights auto-adapt monthly via `pipeline/feedback.py` (gradual ±5% drift based on which factors predicted returns correctly). Learned weights stored in `data/learned_weights.json`.

---

## Daily Run (Standard)

### Option A: Run from Command Prompt (recommended)

```cmd
cd "C:\Users\srija\OneDrive - Valentia Partners\Desktop\Investment dashboard\Investment Alpha\Investment Aplha"
run_pipeline.bat
```

### Option B: Run manually in Python

```cmd
cd "C:\Users\srija\OneDrive - Valentia Partners\Desktop\Investment dashboard\Investment Alpha\Investment Aplha"
python main.py
```

This uses the full S&P 500 + mid-cap universe (~618 tickers). Allow 15–25 minutes on first run (fundamentals fetch); subsequent runs use cache and take ~3–5 minutes.

---

## Command-Line Flags

| Flag | Purpose |
|------|---------|
| `--tickers AAPL MSFT ...` | Override universe with specific tickers |
| `--top N` | Select top N stocks (default: 10, regime-adjusted) |
| `--refresh` | Force re-download all data (ignore cache) |
| `--skip-regime` | Skip VIX/regime detection, use BULL defaults |
| `--skip-stop-loss` | Skip weekly stop-loss check (use on first run) |
| `--force-regime bull/bear/neutral` | Override regime classification |
| `--execute` | Submit paper trades to Alpaca after pipeline |
| `--broker-dry-run` | With `--execute`: log trades but don't submit |
| `--dry-run` | Skip feedback weight update |
| `--json-only` | Print final JSON to stdout, suppress summary |
| `--debug` | Verbose logging |

**Quick test (12 tickers, no stop-loss check):**
```cmd
python main.py --tickers AAPL MSFT GOOGL AMZN NVDA META JPM JNJ V UNH XOM BAC --skip-stop-loss --dry-run
```

---

## First-Time Setup

1. Install dependencies:
   ```cmd
   pip install yfinance pandas numpy scipy openpyxl requests python-dotenv fastapi uvicorn alpaca-trade-api
   ```

2. Create `.env` file in the project folder:
   ```
   ALPACA_API_KEY=your_key_here
   ALPACA_SECRET_KEY=your_secret_here
   ALPACA_BASE_URL=https://paper-api.alpaca.markets
   ```

3. Run with `--skip-stop-loss` on first run (no prior positions to check):
   ```cmd
   python main.py --skip-stop-loss
   ```

---

## Output Files

All outputs saved to `outputs/` folder:

| File | Contents |
|------|---------|
| `trading_output.json` | Full API-ready JSON (portfolio, signals, risk summary) |
| `trading_output.xlsx` | Excel report with portfolio table |
| `trading_output.html` | HTML dashboard (open in browser) |
| `latest_portfolio.json` | Current holdings state (used by stop-loss checker) |
| `stop_loss_log.json` | Log of all stop-loss events |

---

## Market Regime Logic

Checked automatically at startup using live VIX and S&P 500 data:

| Regime | Condition | Top-N selected |
|--------|-----------|----------------|
| **BULL** | VIX < 20 and SPX > 200-day MA | 10 stocks |
| **NEUTRAL** | VIX 20–28 or SPX near 200MA | 8 stocks |
| **BEAR** | VIX > 28 or SPX below 200MA | 5 stocks |

In bear regime, stop-loss thresholds tighten to 10% (vs 15% in bull).

---

## Stop-Loss Checker

Runs automatically before the main pipeline. Compares current prices against `latest_portfolio.json` entry prices. Triggers EXIT signal if any position drops below threshold.

To run manually as a standalone check:
```cmd
python -m broker.stop_loss
```

---

## Feedback Loop (Monthly)

Run after each month-end to update factor weights based on actual returns:

```cmd
python -m pipeline.feedback
```

To preview without saving:
```cmd
python -m pipeline.feedback --dry-run
```

To reset weights back to config defaults:
```cmd
python -m pipeline.feedback --reset
```

Learned weights are saved to `data/learned_weights.json` and loaded automatically by the scoring engine on the next run.

---

## Backtest

Run the 2015–2024 vectorised backtest to validate factor weights historically:

```cmd
python backtest/backtest.py
```

Results saved to `outputs/backtest_results.xlsx`. Key metrics reported: CAGR, Sharpe ratio, max drawdown, hit rate vs SPY.

---

## Insider Signal (SEC EDGAR)

Enabled by default (`INSIDER_ENABLED = True` in config.py). Fetches Form 4 filings from SEC EDGAR for each ticker, filtering to open-market purchases only ($500k+ threshold, officers/directors only). Adds a blended signal to the sentiment score.

**Note:** This adds ~30–60 seconds per run due to SEC API rate limits. To disable for faster runs, set `INSIDER_ENABLED = False` in config.py.

---

## Task Scheduler (Automated Daily Run)

Set up in Windows Task Scheduler to run `run_pipeline.bat` daily at 6:30 AM (before market open):

- Trigger: Daily at 06:30
- Action: `run_pipeline.bat`
- Start in: project folder path

The `.bat` file sets `PYTHONPYCACHEPREFIX=/tmp/pycache` to avoid OneDrive stale cache issues and logs output to `outputs/run_log.txt`.

---

## Key Config Settings (config.py)

```python
# Factor weights (auto-updated by feedback.py)
FACTOR_WEIGHTS = {
    "momentum": 0.30, "trend": 0.25, "quality": 0.20,
    "valuation": 0.15, "volatility": 0.10
}

# Universe size: ~618 tickers total
# SP500_TICKERS + CUSTOM_TICKERS + MIDCAP_TICKERS

CACHE_MAX_AGE_HOURS  = 8      # re-download if cache older than 8h
HISTORY_DAYS         = 400    # price history window
INSIDER_ENABLED      = True   # SEC EDGAR Form 4 signal
VALUATION_ENABLED    = True   # P/E + EV/EBITDA vs sector
SENTIMENT_ENABLED    = True   # Analyst revision scores
```

---

## Troubleshooting

**Pipeline runs but selects 0 stocks:**  
Usually means all stocks failed the 200-day MA filter in a bear market. Use `--force-regime neutral` to relax criteria.

**`forward_pe` / `analyst_score` columns missing:**  
Cache is stale from before Phase 3. Delete cache files in `cache/` and re-run with `--refresh`.

**Alpaca connection error:**  
Check `.env` file has correct API keys. Verify paper trading URL: `https://paper-api.alpaca.markets`.

**`ModuleNotFoundError`:**  
Run from the project root directory, not a subdirectory.

**OneDrive sync conflicts / stale .pyc:**  
Always run with `set PYTHONPYCACHEPREFIX=/tmp/pycache` or use the `.bat` file which sets this automatically.

---

## Architecture Summary

```
main.py
  ├── pipeline/regime.py        ← Market regime (VIX + SPX)
  ├── broker/stop_loss.py       ← Weekly stop-loss check
  ├── pipeline/ingestion.py     ← Stage 1: OHLCV + fundamentals (yfinance)
  ├── pipeline/features.py      ← Stage 2: Technical + momentum + quality + valuation
  ├── pipeline/sentiment.py     ← Stage 2B: Analyst revision scores
  ├── pipeline/insider.py       ← Stage 2C: SEC EDGAR open-market purchases
  ├── pipeline/scoring.py       ← Stage 3: 6-factor composite score
  ├── pipeline/filters.py       ← Stage 4: MA / vol / liquidity / sector cap
  ├── pipeline/selection.py     ← Stage 5: Rank + select top N
  ├── pipeline/portfolio.py     ← Stage 6: Weights + expected return / risk
  ├── pipeline/signals.py       ← Stage 7: BUY / HOLD / EXIT signals
  ├── pipeline/output.py        ← Stage 8: JSON + Excel + HTML
  ├── pipeline/feedback.py      ← Monthly weight adaptation
  ├── broker/alpaca.py          ← Alpaca connection
  ├── broker/executor.py        ← Order execution
  └── backtest/backtest.py      ← 2015–2024 historical validation
```
