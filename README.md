# Investment Alpha

A personal quantitative trading system built from scratch — regime-gated, dual-workflow, fully automated. Currently running on paper trading via Alpaca Markets.

---

## What it does

The system runs two parallel workflows, both controlled through a Discord bot and gated by a shared market regime score.

```
        REGIME ENGINE  (auto, 3x daily)
  Scores: VIX · Fear & Greed · ADX · SPY vs 200MA · Sector Breadth
  Output: STRONG BULL / MOD BULL / NEUTRAL / BEARISH  (0–100 score)
                        |
            +-----------+-----------+
            v                       v
   SCREENER (daily)         PIPELINE (monthly)
   100 large-cap stocks     575 US stocks
   Tactical entries         Full rebalance
   Separate Alpaca acct     Separate Alpaca acct
```

### Workflow 1 — Daily Screener (Tactical)

Runs automatically 3× per day via GitHub Actions. Fetches price and fundamental data, classifies each stock into a strategy bucket (`momentum`, `breakout`, `mean_reversion`, `catalyst`, `defensive`, `watch`, `avoid`), applies the regime gate, scores the survivors 0–100, and stores the top 5 in Cloudflare KV.

You then:
1. `/screener` — see today's top picks with conviction flags
2. `/buy symbol:C` — preview a bracket order (price, shares, stop −5%, target +12%)
3. Confirm → order placed on the Screener Alpaca account, stops auto-managed

### Workflow 2 — Monthly Pipeline (Strategic)

Scores 575 stocks on a 6-factor model and proposes a full portfolio rebalance. The regime determines how many positions to hold (5 in bear, up to 10 in strong bull) and how wide the stops are (ATR × 1.5 to 2.5).

Factors:
| Factor | Weight | Notes |
|---|---|---|
| Momentum | 28% | Price momentum across multiple timeframes |
| Trend | 20% | MA alignment, ADX |
| Quality | 18% | Profitability, balance sheet |
| Valuation | 14% | PE, forward PE, price-to-book |
| Sentiment | 10% | Analyst revisions (70%) + congressional trades (30%) |
| Volatility | 10% | Penalty — rewards lower-vol stocks |

---

## Regime Engine

Six components feed the regime score (0–100):

| Component | Weight | What it measures |
|---|---|---|
| VIX Level | 20pt | Market fear — low VIX = calm |
| VIX Term Structure | 10pt | Short vs long-term fear curve |
| Fear & Greed Index | 15pt | CNN sentiment (0 = fear, 100 = greed) |
| ADX on SPY | 20pt | Trend strength of S&P 500 |
| SPY vs 200-day MA | 20pt | Is S&P above its long-term average? |
| Sector Breadth | 15pt | % of 11 sectors above their 200MA |

Score thresholds unlock different strategy buckets:

| Score | Label | Permitted strategies | Position size |
|---|---|---|---|
| ≥ 75 | STRONG BULL | momentum, breakout, mean_reversion, catalyst | 5% per trade |
| 55–74 | MOD BULL | momentum, mean_reversion, catalyst | 3% per trade |
| 40–54 | NEUTRAL | mean_reversion, defensive | 3% per trade |
| < 40 | BEARISH | defensive only | 1.5% per trade |

---

## Discord Commands

All interaction happens through slash commands:

| Command | What it does |
|---|---|
| `/regime` | Live regime score, VIX, SPY vs 200MA, sector breadth |
| `/screener` | Today's top 5 picks with conviction badges |
| `/buy symbol:X` | Preview bracket order, confirm to place |
| `/sell symbol:X` | See P&L, confirm to close |
| `/status` | All open positions, P&L, cost basis |
| `/chart symbol:X` | Price chart (or `portfolio` for equity curve vs SPY) |
| `/monitor` | Immediate stop-loss check across all positions |
| `/stoploss mode:check` | Stop distances, no orders placed |
| `/stoploss mode:execute` | Exit breached positions (confirm button) |
| `/pipeline mode:dry` | Run full 575-stock analysis, signals only |
| `/pipeline mode:execute` | Rebalance portfolio (confirm button) |
| `/strategy` | Live strategy config — factor weights, universe, sleeves |
| `/help` | Full system guide |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Quant models | Python (screener, pipeline, regime, backtest) |
| Automation | GitHub Actions (free tier — no server costs) |
| Discord bot | Cloudflare Workers (edge function, sub-millisecond response) |
| State / cache | Cloudflare KV |
| Paper trading | Alpaca Markets API (two separate accounts) |
| Market data | Alpaca (price/OHLCV) + Finnhub (fundamentals) |
| TradingView | Pine Script + webhook → Worker → Alpaca |

---

## Project structure

```
├── screener/
│   ├── daily_sentiment_runner.py   # Regime scoring + stock classification
│   ├── regime_to_kv.py             # Push results to Cloudflare KV
│   ├── nightly_updater.py          # Update paper trade P&L
│   └── weekly_analysis.py          # Correlation + weight analysis
├── pipeline/
│   ├── scoring.py                  # 6-factor model
│   ├── regime.py                   # Pipeline-side regime calculation
│   ├── sentiment.py                # Analyst revisions + congressional trades
│   ├── portfolio.py                # Position sizing + rebalance logic
│   ├── postmortem.py               # Stop-exit analysis
│   └── learning.py                 # Weight auto-adjustment
├── worker/
│   └── index.js                    # Cloudflare Worker: Discord bot + TradingView webhook
├── broker/
│   ├── alpaca_client.py            # Alpaca API wrapper
│   └── discord_notify.py           # Discord webhook notifications
├── .github/workflows/
│   ├── screener_daily.yml          # Runs 3× daily (8AM, 11AM, 3:30PM ET)
│   ├── monitor.yml                 # Position monitoring
│   └── command.yml                 # Dispatched by Discord bot
└── scripts/
    └── register_discord_commands.py
```

---

## Disclaimer

This is a personal learning project. Nothing here is financial advice. All trading is paper (simulated) — no real money is involved. Past backtested performance does not predict future results.
