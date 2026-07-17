# Data Sourcing Audit & Improvement Plan

Scope: every data point used in trade decisions across the two engines — **screener** (`screener/daily_sentiment_runner.py`, 3x/day, ~100 tickers) and **pipeline** (`pipeline/*.py`, monthly, ~575 tickers) — plus the shared broker/strategy layer.

Full raw catalog (every metric, formula, exact API call) lives in the research below this doc's summary tables. This file is the decision layer: what to fix, in what order, and why.

---

## 0. Bugs found during the audit (fix regardless of sourcing strategy)

These aren't "better source" questions — they're correctness/drift risks in the current wiring.

| # | Issue | Impact | Fix effort |
|---|---|---|---|
| B1 | `data/insider_cache.json` is read by the screener for its +5pt bonus, but **nothing in the screener's own workflow populates it** — only the monthly pipeline run does (`pipeline/insider.py`). If the pipeline hasn't run recently, the bonus silently never fires. | Insider signal is dead most of the time in the screener | Low — call `pipeline/insider.py`'s fetch logic from the screener workflow, or share a cron |
| B2 | `data/congressional_cache.json` is written by **two different scripts with incompatible schemas**: `scripts/fetch_congressional_trades.py` (Quiver Quant, writes `{recent_buys, last_buy_date, buyers}`) and `pipeline/congressional.py` (Capitol Trades, writes `{signal: float}`). Same filename, different shape. | Whichever runs last clobbers the other; the screener's `+5/buy` bonus could silently read `0` if the pipeline's schema wins | Low — rename to separate files (`congressional_cache_screener.json` / `congressional_cache_pipeline.json`) |
| B3 | Stop-loss math is implemented twice (`broker/stop_loss.py` and `broker/monitor.py:_get_stop_price`), same config keys, separate code | Edit one, forget the other → stop levels drift apart | Low — extract to one shared function |
| B4 | Two different "yield curve" definitions across engines: screener uses 10Y-2Y (FRED `T10Y2Y`), pipeline uses 10Y-3M (`^TNX - ^IRX`) | Not wrong per se (both are valid curve definitions) but means "yield curve" means two different things depending which engine you're reading | Low — document intentionally, or standardize on one |
| B5 | `strategies/mean_reversion.py` reads the **screener's** regime label (`daily_sentiment_data.json`) to gate exposure, even for pipeline-driven positions | Implicit cross-engine dependency; if screener hasn't run, mean-reversion gating goes stale | Low-Med |

Recommend clearing B1–B3 first — they're cheap and they're actual dead/broken code paths, not sourcing-purity debates.

---

## 1. Composite metrics to decompose into primitives

For each: current call → primitive components → why decomposing helps (verifiability, staleness control, consistency, or just because we already fetch the inputs elsewhere and are paying for two calls instead of one).

| Metric | Current source (pre-baked) | Primitive components | Where components already live in our code | Priority |
|---|---|---|---|---|
| **Market Cap** | Finnhub `marketCapitalization` / Yahoo `marketCap` (both engines) | `shares_outstanding × current_price` | `sharesOutstanding` is already in the same Yahoo/Finnhub payload; price is already fetched for every other signal | High — free, one-line change, removes a vendor round-trip |
| **Beta** | Finnhub/Yahoo `beta` field | OLS slope of stock daily returns vs SPY daily returns, trailing 1–2yr | Stock OHLCV and SPY OHLCV are both already pulled for ADX/momentum in the same script run | Medium — needs a small regression helper, but no new API calls |
| **Forward P/E, Trailing P/E, EV/EBITDA** | Finnhub `peNormalizedAnnual` / Yahoo `forwardPE`/`trailingPE`/`enterpriseToEbitda` | `price / EPS(fwd or ttm)`; `EV / EBITDA` where `EV = market_cap + total_debt - cash` | EPS fields are already fetched alongside the pre-baked ratio and currently unused for this purpose | Medium |
| **Yield Curve (screener)** | FRED pre-built `T10Y2Y` series (one number) | `DGS10 - DGS2` (two FRED series) | Pipeline already does the equivalent correctly (`^TNX - ^IRX`) — port the pattern | Low — mirrors existing code |
| **ROE, ROA, Debt/Equity, Gross/Operating Margin, FCF** (pipeline) | Yahoo `.info` pre-computed ratios | `net_income/equity`, `net_income/assets`, `total_debt/total_equity`, `gross_profit/revenue`, `operating_income/revenue`, `operating_cf - capex` | Pipeline *already* pulls raw balance-sheet/income-statement line items for a different subset of factors (`fetch_extended_fundamentals`) — just extend that to cover all of these consistently | Medium-High — biggest consistency win, touches several factors at once |
| **Earnings Surprise %** | Screener computes it correctly (`actual`/`estimate` from Finnhub); pipeline pulls Yahoo's pre-baked `surprisePercent` | `(actual - estimate) / abs(estimate)` | Screener's implementation is already the reference pattern — copy it into the pipeline | Low |
| **Fear & Greed Index** | CNN black-box composite (7 undisclosed sub-weights) | We already independently compute 2 of its 7 known sub-components (VIX, breadth) elsewhere in the same regime score | Not a clean decomposition (CNN doesn't publish the other 5 components' raw data cheaply) — recommend **down-weighting or dropping** this component rather than rebuilding it, since it double-counts VIX/breadth we already score separately | Medium (design decision, not just a data swap) |
| **52w-high/low, 52-week change, avg volume** | Correct on Alpaca path; falls back to vendor pre-baked Yahoo fields when Alpaca is thin (and the volume window is mismatched — 3mo vendor field vs our intended 20d) | Already computed correctly from raw bars on the primary path | Fix the fallback to compute from Yahoo's raw historical bars instead of trusting Yahoo's pre-baked field, and fix the volume window mismatch | Low |

---

## 2. Vendor/source quality upgrades (not decomposition — swapping the data provider itself)

| Metric | Current vendor | Issue | Better alternative | Priority |
|---|---|---|---|---|
| Congressional trades (screener) | Quiver Quant (paid tiers, 3rd-party scrape) | Not the primary source | House Clerk (`disclosures-clerk.house.gov`) + Senate eFD (`efdsearch.senate.gov`) — both publish structured/scrapeable filings directly, free, authoritative | Medium — more parsing work, no API cost |
| Congressional trades (pipeline) | Capitol Trades (undocumented API, no auth) | Fragile — no SLA, could disappear or rate-limit any time | Same House Clerk/Senate eFD sources; unify both engines onto one fetcher | Medium |
| Insider trades | Already SEC EDGAR Form 4 (primary source) — this one's already correct | — | No change needed, this is the reference pattern for "source from origin" | — |
| VIX / VIX3M | Yahoo unofficial quote endpoint (`query1.finance.yahoo.com`) — undocumented, can break without notice | Works today but no SLA | CBOE publishes VIX/VIX3M official EOD data free; Alpaca doesn't carry index data directly but CBOE's own CSV feed does | Low-Medium |
| Fear & Greed scrape fallback | Regex-scraping `feargreedmeter.com` HTML | Fragile, breaks on any HTML change | If keeping F&G at all, use CNN's endpoint only and drop the HTML-scrape fallback (fail closed / neutral score instead) | Low |
| Analyst consensus / price targets | Finnhub/Yahoo aggregated consensus | This is inherently a vendor aggregate — no "primary source" exists (individual analyst notes aren't public) | Acceptable as-is; just standardize on one vendor across both engines instead of Finnhub-primary/Yahoo-fallback with different field semantics | Low |

---

## 3. Suggested phasing

**Phase 1 — Bug fixes (B1–B5).** Cheap, no new data, removes silent failures. ~1 session.

**Phase 2 — Free decompositions (market cap, screener yield curve, earnings surprise consistency, 52w-high fallback fix).** No new API calls, just rearranging fields we already fetch. ~1 session.

**Phase 3 — Medium decompositions (beta regression, P/E from price/EPS, EV/EBITDA from components).** Small new helper functions, no new vendors. ~1–2 sessions.

**Phase 4 — Fundamentals consistency (ROE/ROA/margins/FCF from raw statements across the board).** Biggest single win for "we're trusting a black box when we already have the primitives," touches `pipeline/ingestion.py` and `pipeline/features.py` together. ~2 sessions.

**Phase 5 — Vendor swaps (congressional → House Clerk/Senate eFD, VIX → CBOE official).** New parsers, no new decomposition math, mostly about resilience/authority of source rather than math correctness. ~2-3 sessions, lowest urgency.

**Fear & Greed** is a standalone design decision (drop vs keep vs down-weight) — flagged separately since it's not a mechanical fix.

---

## Decisions so far

- **Fear & Greed Index**: drop the component entirely, redistribute its 15pts to the transparently-computed signals (VIX, ADX, breadth, etc.) — flagged for Phase 1/2 work.
- **Congressional data vendor** (Quiver Quant vs Capitol Trades vs primary House Clerk/Senate eFD): decide later. For now, Phase 1 only fixes the cache-schema collision (B2); vendor choice untouched.

---

## Appendix — Full raw catalog

Every metric used in trade decisions, its exact formula as implemented, and its exact current data source. Organized by engine/layer.

### A. SCREENER — Macro Regime Score (`compute_macro_score`, 0–115 pts)

| # | Metric | Formula | Current source | Composite? |
|---|--------|---------|-----------------|------------|
|1| VIX Level (20pt) | Bucketed: `<15→20, <18→18, <22→14, <28→8, <35→4, else 1` | `get_yahoo_quote("^VIX")` → `query1.finance.yahoo.com/v7/finance/quote?symbols=^VIX`, field `regularMarketPrice` | Vendor index level, not decomposable |
|2| VIX Term Structure (10pt) | `ratio = vix_spot / vix3m`, bucketed | `get_vix_term_structure()`: Yahoo `^VIX3M` quote | Computed in-code from 2 vendor levels — good |
|3| Fear & Greed Index (15pt) | Bucketed: `40-65→15, 30-40→11, 65-75→9, 20-30→6, >75→4, else 2` | CNN `production.dataviz.cnn.io/index/fearandgreed/graphdata`; scrape fallback `feargreedmeter.com` | Vendor black-box composite of 7 sub-indicators, 2 of which (VIX, breadth) we already compute independently → **decision: drop, redistribute weight** |
|4| ADX(14) on SPY (20pt) | Bucketed on (adx, trend) pairs | Alpaca `GET /v2/stocks/bars?symbols=SPY&timeframe=1Day`; Yahoo v8 chart fallback | Computed in-code (Wilder smoothing) from raw OHLC — good |
|5| SPY vs 200-day MA (20pt) | `(price-ma200)/ma200*100`, bucketed | Same Alpaca/Yahoo OHLCV as #4 | Computed in-code from raw closes — good |
|6| Sector Breadth (15pt) | % of 11 sector ETFs with `close > 200MA`, bucketed | Alpaca batch bars for XLK/XLF/XLE/XLV/XLI/XLP/XLU/XLB/XLRE/XLY/XLC; Yahoo fallback | Computed in-code — good |
|7| Yield Curve 10Y-2Y (10pt) | Bucketed on spread | FRED `fredgraph.csv?id=T10Y2Y` (pre-built spread) | **Vendor-precomputed** — should be `DGS10 - DGS2` computed in-code |
|8| Equity Put/Call Ratio (5pt) | Bucketed: `<0.5→max ... else 0` | FRED `fredgraph.csv?id=CPCE` | Vendor-aggregated market stat, no finer primitive realistically available |

Regime label: `≥75→STRONG BULL, ≥55→MOD BULL, ≥40→NEUTRAL, else BEARISH`.

### B. SCREENER — Per-Stock Composite Score (`score_stock()`, weights: analyst 30 / momentum 25 / news 20 / macro 15 / valuation 10)

| Metric | Formula | Current source | Composite? |
|---|---|---|---|
| Analyst Consensus (30pt) | Bucketed rec + analyst count + upside adjustments | Finnhub `/stock/recommendation` (raw counts, bucket computed in-code — good) + `/stock/price-target`; Yahoo fallback `recommendationKey`/`targetMeanPrice` (pre-baked) | Mixed |
| Momentum (25pt) | MA50/200 position, 52w range, week52 change, vol ratio | Alpaca bars computed in-code (good) primary; Yahoo pre-baked `fiftyTwoWeekHigh/Low`, `52WeekChange` fallback | Mixed — fallback path is vendor pre-baked |
| News Sentiment (20pt) | Keyword match ±1.5/word on last 5 headlines, clipped [-10,10] | Yahoo News search `v1/finance/search?q={ticker}&newsCount=5` | Fully custom scoring on raw headlines |
| Macro Alignment (15pt) | `(macro_total/100)*15` | Derived from section A | Derived |
| Valuation (10pt) | Bucketed on `forward_pe` | Finnhub `peNormalizedAnnual` / Yahoo `forwardPE` | **Vendor pre-baked** — EPS fetched separately but unused for this |
| Insider Buy Bonus (+5pt) | `if signal≥1: +5` | `data/insider_cache.json` — **never populated by screener workflow** (bug B1) | Cross-system dependency |
| Congressional Buy Bonus (+5/buy, cap 10) | `min(recent_buys*5, 10)` | `data/congressional_cache.json` via Quiver Quant `api.quiverquant.com/beta/bulk/congresstrading` | Vendor-aggregated disclosures, count computed in-code |
| Earnings Beat Bonus (+8pt) | `if surprise_pct>10: +8` | Computed in-code: `(actual-estimate)/abs(estimate)*100` from Finnhub `/stock/earnings` | Computed from primitives — good, reference pattern |
| RS vs SPY (+8/-5pt) | `stock.ret_20d - spy.ret_20d` | Computed in-code both sides | Computed from primitives — good |

`classify_stock()` also uses **market_cap** and **beta** — both vendor pre-baked (Finnhub `marketCapitalization`, Yahoo `marketCap`/`beta`) despite shares_outstanding, price, and SPY OHLCV all being available in-code already.

### C. SCREENER — Position Sizing / Stops (`score_stock()` tail)

| Metric | Formula |
|---|---|
| ATR-14 % | Computed in-code from raw Alpaca daily bars (standard TR formula) — good |
| Stop-loss % | `clamp(atr_pct * stop_mult[regime], 2%, 8%)` |
| Take-profit (hard) | `clamp(atr_pct * ceil_mult[regime], 8%, 35%)` |
| Take-profit (monitor alert) | `80% of hard ceiling, clamped [6%,28%]` |

Pushed to Cloudflare KV (`regime_to_kv.py`) for the TradingView-webhook Worker — a third decision surface consuming `atr_pct`, `stop_pct`, `tp_monitor_pct`, `tp_alpaca_pct`, `conviction_ok=score≥55`.

### D. PIPELINE — Market Regime (`pipeline/regime.py`)

| Metric | Formula | Source |
|---|---|---|
| VIX current | Last close | `yf.download("^VIX", period="5d")` |
| SPX vs 200MA | `(price-ma200)/ma200` | `yf.download("^GSPC", period="300d")` |
| Regime classification | `vix≥28→bear; spx<-5%→bear; vix≥20 or below_200ma→neutral; else bull` | Derived |
| Yield Curve 10Y-3M (downgrade trigger) | `Close(^TNX) - Close(^IRX)` | `yf.download("^TNX")`, `yf.download("^IRX")` — **computed in-code from 2 primitives, the correct pattern** |
| Credit Spread momentum (downgrade trigger) | `(HYG/LQD)[-1]/(HYG/LQD)[0]-1` over 30d | `yf.download("HYG")`, `yf.download("LQD")` — computed in-code, doesn't exist in screener at all |

Regime drives `active_top_n` and `active_stop_loss` (`STOP_LOSS_PCT`: bull 0.85 / neutral 0.88 / bear 0.90).

### E. PIPELINE — Feature Engineering (`pipeline/features.py`)

| Metric | Formula | Source | Composite? |
|---|---|---|---|
| SMA50/200, RSI14, MACD | Standard formulas on raw closes | `yf.download(tickers)["Close"]` batch | Computed |
| ret_3m/6m/12m, rel_strength_12m, proximity_52w_high, vol_60d, avg_dollar_vol | Standard formulas on raw closes/volume | Same batch download + `^GSPC` for RS | Computed |
| roe, debt_to_equity, earnings_growth, revenue_growth, margins, FCF, roa | Pass-through | `yf.Ticker().info` fields | **Vendor pre-baked** despite raw statement items fetched elsewhere in same file for other factors |
| market_cap | Pass-through | `info.get("marketCap")` | **Vendor pre-baked**, never `sharesOutstanding × price` |
| fcf_yield | `free_cashflow/market_cap` | Computed, but on a pre-baked market_cap | Computed on top of vendor field |
| pe_vs_sector, ev_vs_sector | Ratio vs sector median | `info.forwardPE/trailingPE/enterpriseToEbitda` (vendor ratios), sector median computed in-code | Mixed |
| analyst_upside, analyst_score | `upside=min(target/price-1,cap)`, `rec_norm=(5-rec)/4` | `info.targetMeanPrice`, `info.recommendationMean` | Computed on top of vendor consensus — no finer primitive exists |
| accruals_ratio | `(net_income_common - operating_cf)/market_cap` | `info.netIncomeToCommon`, `info.operatingCashflow` ÷ vendor market_cap | Mostly computed |
| asset_growth | `(assets_y0-assets_y1)/abs(assets_y1)` | `t_obj.balance_sheet.loc["Total Assets"]` — raw statement line | Computed — good |
| op_margin_change | `(opinc_y0/rev_y0)-(opinc_y1/rev_y1)` | `t_obj.income_stmt` raw lines | Computed — good |
| earnings_surprise_pct | Pass-through | `t_obj.earnings_history["surprisePercent"]` | **Vendor pre-baked** — inconsistent with screener's own in-code computation of the same metric |

### F. PIPELINE — Scoring (`pipeline/scoring.py`)

All sub-scores are percentile ranks of the features in section E — this normalization is itself "computed," not vendor-fetched. Composite = weighted blend of momentum/trend/quality/volatility/valuation/sentiment sub-scores; weights come from `config.py` or `data/learned_weights.json` if present (feedback-loop override).

### G. PIPELINE — Insider Signal (`pipeline/insider.py`) — reference pattern, already sourced correctly

SEC EDGAR: `sec.gov/files/company_tickers.json` (CIK lookup) → `data.sec.gov/submissions/CIK{cik}.json` (Form 4 filings) → `sec.gov/Archives/edgar/data/{cik}/{accession}/form4.xml` (raw transaction XML). `total_value = shares × price` computed in-code from primary-source fields. This is the model for how the other vendor-swap items in section 2 of the plan should end up.

### H. PIPELINE vs SCREENER — Congressional Signal (two incompatible implementations)

| | Pipeline (`pipeline/congressional.py`) | Screener (`scripts/fetch_congressional_trades.py`) |
|---|---|---|
| Vendor | Capitol Trades (no auth, undocumented) | Quiver Quant (`QUIVER_API_KEY`) |
| Amount | `_parse_trade_size()`: disclosure range → midpoint | Raw transaction fields, just counted |
| Signal | `+1.0` if ≥3 net buys, `+0.5` if 1-2, `-0.5` net selling | `recent_buys` count, screener applies its own bonus scaling |
| Cache | `data/congressional_cache.json` `{signal: float}` | `data/congressional_cache.json` `{recent_buys, last_buy_date, buyers}` — **same filename, different schema (bug B2)** |

### I. PIPELINE — Filters / Selection / Portfolio Construction

| Metric | Formula | Config |
|---|---|---|
| MA200 hard/soft exclude | Hard exclude `<-3%`, soft penalty `×0.85` if `-3%` to `0%` | `MA200_HARD_EXCLUDE=-0.03` |
| High-volatility exclude | Exclude top 20% by `vol_60d` | `VOLATILITY_CUTOFF=0.80` |
| Low-liquidity exclude | `avg_dollar_vol < MIN_AVG_VOLUME` | `MIN_AVG_VOLUME=500,000` |
| Meme-stock filter | `reddit_mentions>500` | **Disabled, and the column is never populated anywhere — dead filter** |
| Soft sector cap | 40% score penalty if sector exceeds cap | `SECTOR_MAX_WEIGHT=0.30` |
| Position weighting | equal / score-weighted / inverse-vol (current: inv_vol), capped 20%/position | `MAX_POSITION_WEIGHT=0.20` |
| Earnings blackout | Block BUY if `days_to_earnings≤5` | `yf.Ticker().calendar` |
| Re-entry cooldown | Block re-buy within 5 days of stop-out | `config.STOP_LOSS_LOG_FILE` |

### J–L. BROKER — Price Layer, Stop-Loss, Monitor

- `broker/market_data.py`: Alpaca (SDK, IEX feed) → Finnhub `/quote` → yfinance, 3-deep fallback chain for prices/bars/ATR.
- `broker/stop_loss.py`: ATR-based stop (`entry - ATR_MULT[regime]*atr14`) preferred over fixed-pct; entry price from Alpaca's actual `avg_entry_price` (good — uses broker's real cost basis, not internal state).
- `broker/monitor.py`: **Reimplements the same stop-loss math independently** (bug B3) — drift risk. Also computes profit-target (`≥20% gain`) and sharp intraday move alerts (`≥5%` from today's open).

### M-N. STRATEGIES — Mean Reversion & Dual Momentum

- Mean reversion: RSI(2)<10 entry, price>SMA200 trend filter, SMA5 snap-back exit, 15-day time stop, drawdown pause (8%/5% pause/resume from Alpaca account equity), regime-gated exposure cap that **reads the screener's regime label even for pipeline-driven logic** (bug B5).
- Dual momentum: 12-month total return on SPY/VEU/AGG/BIL via `yf.download(period="14mo")`, relative + absolute momentum gates. Advisory only, never trades automatically.

