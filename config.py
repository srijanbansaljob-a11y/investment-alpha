"""
config.py - Central configuration for the Investment Alpha pipeline.

All tunable parameters live here. Change a value here and the entire
pipeline adjusts automatically. Never hardcode values in pipeline modules.

VERSION HISTORY
  v1.0 - original 4-factor pipeline
  v2.0 - added regime detection, stop-loss, sector cap, score-weighted
          allocation, sentiment/insider stubs, skip-month momentum.
          Phase 1 fully live; Phase 2 flags default to False until
          API keys are added to .env.

HOW TO ENABLE PHASE 2 FEATURES
  1. Register at finnhub.io (free) - add FINNHUB_API_KEY to .env
  2. Set SENTIMENT_ENABLED = True
  3. Optionally set MEME_FILTER_ENABLED = True  (no key needed)
  4. Optionally set INSIDER_ENABLED = True       (no key needed)
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
CACHE_DIR    = BASE_DIR / "cache"
DATA_DIR     = BASE_DIR / "data"
OUTPUT_DIR   = BASE_DIR / "outputs"
PIPELINE_DIR = BASE_DIR / "pipeline"

CACHE_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

PORTFOLIO_STATE_FILE = OUTPUT_DIR / "latest_portfolio.json"
STOP_LOSS_LOG_FILE   = OUTPUT_DIR / "stop_loss_log.json"

# ── Logging / Cache ────────────────────────────────────────────────────────
LOG_LEVEL            = "INFO"          # DEBUG for verbose output
CACHE_MAX_AGE_HOURS  = 8              # refresh cache if older than this
HISTORY_DAYS         = 400            # alias for PRICE_HISTORY_DAYS (used by ingestion.py)

# ── Factor Weights ─────────────────────────────────────────────────────────
# Phase 3: 6-factor model adds valuation (lower P/E & EV/EBITDA = better)
# Weights are STARTING points; pipeline/feedback.py adjusts them monthly
# based on which factors correctly predicted returns (gradual drift +/-5%)

FACTOR_WEIGHTS = {
    # 4-factor baseline (no sentiment data available)
    "momentum":   0.30,   # was 0.40 -- reduced to make room for valuation
    "trend":      0.25,   # was 0.30
    "quality":    0.20,
    "valuation":  0.15,   # NEW: P/E + EV/EBITDA vs sector peers
    "volatility": 0.10,
}

FACTOR_WEIGHTS_WITH_SENTIMENT = {
    # 6-factor model (auto-activates when sentiment column present)
    "momentum":   0.28,   # skip-month 3/6/12M + rel strength
    "trend":      0.20,   # SMA, RSI, MACD
    "quality":    0.18,   # ROE, FCF yield, gross margin, D/E
    "valuation":  0.14,   # P/E vs sector, EV/EBITDA vs sector
    "sentiment":  0.10,   # analyst target price upside + revision trend
    "volatility": 0.10,   # subtracted (higher vol = penalty)
}

# Learned weights file (written by pipeline/feedback.py after each run)
# If this file exists, its weights OVERRIDE the above defaults
LEARNED_WEIGHTS_FILE = "data/learned_weights.json"

# ── Momentum ────────────────────────────────────────────────────────────────
SKIP_MONTH_MOMENTUM  = True   # use 2-12M window (skip most-recent month)
MOMENTUM_SKIP_DAYS   = 21     # business days to skip (approx 1 month)
MOMENTUM_3M          = 63     # trading days in 3 months
MOMENTUM_6M          = 126    # trading days in 6 months
MOMENTUM_12M         = 252    # trading days in 12 months

# ── Technical Indicators ─────────────────────────────────────────────────────
SMA_SHORT            = 50     # short SMA window
SMA_LONG             = 200    # long SMA window (200-day MA)
RSI_PERIOD           = 14     # RSI lookback
MACD_FAST            = 12     # MACD fast EMA
MACD_SLOW            = 26     # MACD slow EMA
MACD_SIGNAL          = 9      # MACD signal line
MIN_HISTORY_DAYS     = 60     # minimum price history required for a ticker

# ── Portfolio Construction ─────────────────────────────────────────────────
TOP_N_STOCKS        = 10
ALLOCATION_MODE     = "inv_vol"  # "equal" | "score_weighted" | "inv_vol" (inverse-volatility)
MAX_POSITION_WEIGHT = 0.20       # cap any single position at 20%

# ── Sector Cap ─────────────────────────────────────────────────────────────
SECTOR_CAP_ENABLED       = True
SECTOR_MAX_STOCKS        = 3            # legacy — kept for display only
SECTOR_MAX_WEIGHT        = 0.30         # soft cap: no sector > 30% of portfolio

# ── Earnings Blackout ────────────────────────────────────────────────────────
EARNINGS_BLACKOUT_ENABLED = True        # block new BUY within N days of earnings
EARNINGS_BLACKOUT_DAYS    = 5           # calendar days before earnings date

# ── Market Regime ─────────────────────────────────────────────────────────
REGIME_ENABLED      = True
REGIME_VIX_NEUTRAL  = 20    # VIX above this -> neutral
REGIME_VIX_BEAR     = 28    # VIX above this -> bear
REGIME_TOP_N = {
    "bull":    10,
    "neutral":  8,
    "bear":     5,
}
# Yield curve & credit spread (Phase 4) — enrich regime signal
YIELD_CURVE_ENABLED         = True
YIELD_CURVE_BEAR_THRESHOLD  = -0.50   # 10Y-2Y (%) < this = inverted = downgrade regime
CREDIT_SPREAD_ENABLED       = True
CREDIT_SPREAD_BEAR_PCT      = -0.03   # HYG/LQD 20d momentum < this = spreads widening = downgrade

# ── Stop-Loss ──────────────────────────────────────────────────────────────
STOP_LOSS_ENABLED       = True
STOP_LOSS_LOOKBACK_DAYS = 30    # how far back to look for entry_price
# ATR-based stops (Phase 4) — volatility-scaled, avoids noise-triggered exits
USE_ATR_STOP_LOSS       = True  # if False, falls back to fixed-pct stops below
ATR_PERIOD              = 14    # 14-day ATR lookback
ATR_STOP_MULTIPLIER = {
    "bull":    2.5,   # stop = entry - 2.5 × ATR  (~1.5–2 std devs for avg stock)
    "neutral": 2.0,   # tighter in neutral regime
    "bear":    1.5,   # tightest in bear (protect capital aggressively)
}
# Fallback fixed-pct stops (used only when USE_ATR_STOP_LOSS = False)
STOP_LOSS_PCT = {
    "bull":    0.85,   # 15% loss
    "neutral": 0.88,   # 12% loss
    "bear":    0.90,   # 10% loss
}

# ── 200-day MA Filter — Soft Boundary (Phase 4) ───────────────────────────
# Instead of hard exclude at SMA200, apply a penalty for stocks near the boundary
MA200_SOFT_ZONE    = 0.03   # stocks within 3% BELOW 200MA: penalize, don't exclude
MA200_SOFT_PENALTY = 0.85   # composite score × this factor for soft-zone stocks
MA200_HARD_EXCLUDE = -0.03  # stocks MORE than 3% below 200MA: still hard-excluded

# ── Sentiment (Phase 2 - Finnhub) ─────────────────────────────────────────
SENTIMENT_ENABLED    = True
FINNHUB_API_KEY      = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_SENTIMENT_DAYS = 7
SENTIMENT_CACHE_HOURS  = 6

# ── Meme Filter (Phase 2 - ApeWisdom) ─────────────────────────────────────
MEME_FILTER_ENABLED     = False
REDDIT_MENTION_THRESHOLD = 500   # exclude if mentions > this

# ── Extended Fundamentals (Phase 4) ──────────────────────────────────────
# Fetches balance_sheet + income_stmt for: asset growth, ROIC, op-margin change
# First run adds ~60-90s; results cached for CACHE_MAX_AGE_HOURS
EXTENDED_FUNDAMENTALS_ENABLED = True

# ── Feedback Loop — Minimum Sample Guard (Phase 4) ───────────────────────
# Don't adjust factor weights until this many position-month observations are
# accumulated across all runs. Prevents overfitting to noise on small samples.
MIN_FEEDBACK_OBSERVATIONS = 25

# ── Paper Trading Validation (Phase 4 — 3-month trial) ───────────────────
PAPER_TRADING_VALIDATION    = True   # enables performance_tracker logging
PAPER_TRADING_START_DATE    = "2026-05-01"
PAPER_TRADING_MONTHS        = 3
BENCHMARK_TICKER            = "SPY"  # benchmark for alpha calculation

# ── Insider Trading Filter (Phase 3 - SEC EDGAR open-market only) ─────────
INSIDER_ENABLED           = True    # Phase 3: upgraded to open-market purchases only
INSIDER_LOOKBACK_DAYS     = 90
INSIDER_MIN_PURCHASE_USD  = 500_000  # only count purchases >= $500k
INSIDER_CACHE_HOURS       = 24

# ── Valuation Factor (Phase 3) ──────────────────────────────────────────────
VALUATION_ENABLED         = True
VALUATION_PE_WEIGHT       = 0.50    # weight of P/E vs sector within valuation score
VALUATION_EVEBITDA_WEIGHT = 0.50    # weight of EV/EBITDA vs sector

# ── Analyst Revision Sentiment (Phase 3 - replaces Finnhub keywords) ───────
ANALYST_REVISION_ENABLED  = True    # use analyst target price upside + revision
ANALYST_UPSIDE_CAP        = 0.60    # cap upside at 60% to avoid outliers

# ── Alpaca Paper Trading ───────────────────────────────────────────────────
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Alpaca-first reconciliation (Task 1) — sync live positions before trading
ALPACA_RECONCILE_ON_EXECUTE  = True    # always compare Alpaca vs pipeline before orders
ALPACA_WEIGHT_DRIFT_THRESHOLD = 0.03  # rebalance if position weight drifts >3% from target
MANUAL_POSITION_ACTION        = "keep" # "keep" = log & ignore | "exit" = sell manual buys

# ── Intraday Monitor (broker/monitor.py) ──────────────────────────────────
# Runs continuously during market hours, checking positions every N minutes.
# Setup: add DISCORD_WEBHOOK_URL to .env, then:
#   python broker/monitor.py --test   (verify Discord is configured)
#   python broker/monitor.py          (start the monitoring loop)
DISCORD_WEBHOOK_URL        = os.getenv("DISCORD_WEBHOOK_URL", "")
PROFIT_TARGET_PCT          = 0.20    # alert + queue auto-sell when position up 20%
INTRADAY_MOVE_ALERT_PCT    = 0.05    # alert (no auto-execute) if ±5% move from today's open
AUTO_EXECUTE_DELAY_MINUTES = 5       # minutes before auto-executing a queued stop/profit sell
MONITOR_INTERVAL_SECONDS   = 300     # polling interval: 5 minutes (300 seconds)

# ── Congressional Signal (Task 2) ──────────────────────────────────────────
CONGRESSIONAL_ENABLED       = True    # fetch STOCK Act disclosures from Capitol Trades API
CONGRESSIONAL_LOOKBACK_DAYS = 90      # how far back to look for trades
CONGRESSIONAL_MIN_TRADE_USD = 50_000  # minimum disclosed trade size to count
CONGRESSIONAL_CACHE_HOURS   = 24      # cache TTL (same pattern as insider.py)

# ── Data Ingestion ─────────────────────────────────────────────────────────
PRICE_HISTORY_DAYS  = 400   # enough for 200-day MA + 12M momentum
MIN_PRICE           = 5.0   # exclude penny stocks
MIN_AVG_VOLUME      = 500_000
BENCHMARK_TICKER    = "SPY"
VIX_TICKER          = "^VIX"
SPX_TICKER          = "^GSPC"

# ── Scoring / Filtering Thresholds ────────────────────────────────────────
RSI_OVERBOUGHT    = 75
RSI_OVERSOLD      = 30
VOLATILITY_CUTOFF = 0.80   # exclude top 20% most volatile stocks

# ── Signal Label Thresholds (used by portfolio.py for trend/momentum labels) ──
TREND_BULLISH_THRESHOLD   = 0.65   # score_trend >= this → "bullish"
TREND_BEARISH_THRESHOLD   = 0.35   # score_trend <= this → "bearish"
MOMENTUM_STRONG_THRESHOLD = 0.65   # score_momentum >= this → "strong"
MOMENTUM_WEAK_THRESHOLD   = 0.35   # score_momentum <= this → "weak"

# ── Universe ───────────────────────────────────────────────────────────────
SP500_TICKERS = [
    "MMM","AOS","ABT","ABBV","ACN","ADBE","AMD","AES","AFL","A",
    "APD","ABNB","AKAM","ALB","ARE","ALGN","ALLE","LNT","ALL","GOOGL",
    "GOOG","MO","AMZN","AMCR","AEE","AAL","AEP","AXP","AIG","AMT",
    "AWK","AMP","AME","AMGN","APH","ADI","AON","APA","AAPL",
    "AMAT","APTV","ACGL","ADM","ANET","AJG","AIZ","T","ATO","ADSK",
    "ADP","AZO","AVB","AVY","AXON","BKR","BALL","BAC","BK","BBWI",
    "BAX","BDX","WRB","BBY","BIO","TECH","BIIB","BLK","BX","BA",
    "BSX","BMY","AVGO","BR","BRO","BLDR","BG","CDNS","CZR","CPT",
    "CPB","COF","CAH","KMX","CCL","CARR","CAT","CBOE","CBRE","CDW",
    "CE","COR","CNC","CNP","CF","CHRW","CRL","SCHW","CHTR","CVX",
    "CMG","CB","CHD","CI","CINF","CTAS","CSCO","C","CFG","CLX",
    "CME","CMS","KO","CTSH","CL","CMCSA","CAG","COP","ED",
    "STZ","CEG","COO","CPRT","GLW","CTVA","CSGP","COST","CTRA","CCI",
    "CSX","CMI","CVS","DHR","DRI","DVA","DE","DAL","XRAY",
    "DVN","DXCM","FANG","DLR","DG","DLTR","D","DPZ","DOV",
    "DOW","DHI","DTE","DUK","DD","EMN","ETN","EBAY","ECL","EIX",
    "EW","EA","ELV","LLY","EMR","ENPH","ETR","EOG","EPAM","EQT",
    "EFX","EQIX","EQR","ESS","EL","EG","ES","EXC","EXPE","EXPD",
    "EXR","XOM","FFIV","FDS","FICO","FAST","FRT","FDX","FIS","FITB",
    "FSLR","FE","FMC","F","FTNT","FTV","FOXA",
    "BEN","FCX","GRMN","IT","GE","GEHC","GEV","GEN","GNRC","GD",
    "GIS","GM","GPC","GILD","GPN","GL","GDDY","GS","HAL","HIG",
    "HAS","HCA","DOC","HSIC","HSY","HPE","HLT","HOLX","HD",
    "HON","HRL","HST","HWM","HPQ","HUBB","HUM","HBAN","HII","IBM",
    "IEX","IDXX","ITW","INCY","IR","PODD","INTC","ICE","IFF","IP",
    "INTU","ISRG","IVZ","INVH","IQV","IRM","JBHT","JBL","JKHY",
    "J","JNJ","JCI","JPM","KVUE","KDP","KEY","KEYS",
    "KMB","KIM","KMI","KLAC","KHC","KR","LHX","LH","LRCX","LW",
    "LVS","LDOS","LEN","LIN","LYV","LKQ","LMT","L","LOW","LULU",
    "LYB","MTB","MPC","MKTX","MAR","MLM","MAS","MA",
    "MTCH","MKC","MCD","MCK","MDT","MRK","META","MET","MTD","MGM",
    "MCHP","MU","MSFT","MAA","MRNA","MHK","MOH","TAP","MDLZ","MPWR",
    "MNST","MCO","MS","MOS","MSI","MSCI","NDAQ","NTAP","NFLX","NEM",
    "NWSA","NWS","NEE","NKE","NI","NDSN","NSC","NTRS","NOC","NCLH",
    "NRG","NUE","NVDA","NVR","NXPI","ORLY","OXY","ODFL","OMC","ON",
    "OKE","ORCL","OTIS","PCAR","PKG","PLTR","PH","PAYX","PAYC","PYPL",
    "PNR","PEP","PFE","PCG","PM","PSX","PNW","PNC","POOL",
    "PPG","PPL","PFG","PG","PGR","PLD","PRU","PEG","PTC",
    "PSA","PHM","QRVO","PWR","QCOM","DGX","RL","RJF","RTX","O",
    "REG","REGN","RF","RSG","RMD","RVTY","ROK","ROL","ROP","ROST",
    "RCL","SPGI","CRM","SBAC","SLB","STX","SEE","SRE","NOW","SHW",
    "SPG","SWKS","SJM","SW","SNA","SOLV","SO","LUV","SWK","SBUX",
    "STT","STLD","STE","SYK","SMCI","SYF","SNPS","SYY","TMUS","TROW",
    "TTWO","TPR","TRGP","TGT","TEL","TDY","TFX","TER","TSLA","TXN",
    "TMO","TJX","TSCO","TT","TDG","TRV","TRMB","TFC","TYL","TSN",
    "USB","UBER","UDR","ULTA","UNP","UAL","UPS","URI","UNH","UHS",
    "VLO","VTR","VLTO","VRSN","VRSK","VZ","VRTX","VTRS","VICI","V",
    "VST","VMC","WAB","WMT","WBD","WM","WAT","WEC",
    "WFC","WELL","WST","WDC","WHR","WMB","WTW","GWW","WYNN","XEL",
    "XYL","YUM","ZBRA","ZBH","ZTS",
]

CUSTOM_TICKERS = [
    "NVDA","MSFT","AAPL","GOOGL","META","AMZN","TSLA","AVGO","AMD","NFLX",
    "CRM","ADBE","ORCL","INTC","QCOM","TXN","MU","AMAT","LRCX","KLAC",
    "JPM","BAC","GS","MS","BLK","V","MA","PYPL","AXP","BX",
    "UNH","LLY","JNJ","PFE","MRK","ABBV","AMGN","GILD","TMO","DHR",
    "XOM","CVX","COP","SLB","OXY","PSX","VLO","MPC","HAL","BKR",
    "COST","WMT","TGT","HD","LOW","MCD","SBUX","NKE","TJX",
    "NEE","DUK","SO","D","EXC","SRE","AEP","PCG","ED","ETR",
    "BA","LMT","RTX","NOC","GD","GE","HON","MMM","CAT","DE",
]

# ── Mid-Cap Universe (S&P 400 components, ~$2B-$15B market cap) ────────────
# Adds breadth and true alpha opportunities beyond mega-cap consensus
MIDCAP_TICKERS = [
    # Technology mid-caps
    "GDDY","FIVN","PCOR","APPF","CFLT","MDB","GTLB",
    "DDOG","ESTC","NET","CRWD","S","TENB","RPD","QLYS","VRNS","SAIL",
    "PAYC","PCTY","HUBS","SPSC","EGHT","NCNO","BRZE","AMPL",
    # Healthcare mid-caps
    "RVMD","RARE","IONS","ACAD","INVA","NUVL","KRYS","RXRX","TGTX","IMVT",
    "EXAS","NTRA","SDGR","OMCL","LIVN","GKOS","ATRC","NVCR","NVAX",
    # Financial mid-caps
    "EWBC","GBCI","SFNC","IBOC","CVBF","HOPE","BANF","CCBG",
    "PIPR","SFBS","NBTB","EFSC","FBIZ","TCBK","BSVN","HAFC",
    # Industrial mid-caps
    "AIXI","MATX","GATX","GNRC","AAON","IESC","HRI","TREX","IBP",
    "EXPO","SSD","BWXT","DRS","KTOS","AVAV","RGP",
    # Consumer mid-caps
    "WING","TXRH","FRPT","CHEF","RRGB","SHAK","NATH","JACK",
    "BOOT","GOOS","OXM","CATO","DXLG","PRPL","LESL","POOL","SBH",
    # Energy mid-caps
    "SM","GPOR","REX","TALO","SGU","APA","CHRD",
    # REIT mid-caps
    "STAG","IIPR","COLD","EXR","CUZ","NXRT","UDR","CSR","ELME",
]

# deduplicate while preserving order
_seen = set()
ALL_TICKERS = []
for t in SP500_TICKERS + CUSTOM_TICKERS + MIDCAP_TICKERS:
    if t not in _seen:
        _seen.add(t)
        ALL_TICKERS.append(t)

# ── Regime safety ──────────────────────────────────────────────────────────
# Regime to assume when market data is UNAVAILABLE (yfinance outage etc).
# Used to be BULL — a data failure silently meant maximum risk-on. Now NEUTRAL.
REGIME_FALLBACK = "neutral"

# ── Mean-reversion sleeve (strategies/mean_reversion.py) ───────────────────
MR_ENABLED       = True    # daily scan posts BUY/EXIT proposals to Discord
MR_SLEEVE_PCT    = 0.10    # fraction of equity allocated to the sleeve
MR_MAX_POSITIONS = 5       # sleeve slots (per-trade = MR_SLEEVE_PCT / slots)
MR_RSI_ENTRY     = 10      # RSI(2) below this = oversold dip in an uptrend
MR_MAX_HOLD_DAYS = 10      # time stop (trading days)
# MR_UNIVERSE   = [...]    # optional override; defaults to liquid mega-caps

# ── Dual momentum compass (strategies/dual_momentum.py) ────────────────────
DM_ENABLED = True          # monthly advisory card (never trades)

# ── Stop-loss post-mortem (pipeline/postmortem.py) ─────────────────────────
STOP_TUNING_AUTO = False   # suggestions only; you change multipliers manually

# ── Output ─────────────────────────────────────────────────────────────────
OUTPUT_JSON_FILE  = OUTPUT_DIR / "trading_output.json"
OUTPUT_HTML_FILE  = OUTPUT_DIR / "trading_output.html"
OUTPUT_EXCEL_FILE = OUTPUT_DIR / "trading_output.xlsx"

# ── Debug / Dry-run ────────────────────────────────────────────────────────
DRY_RUN    = True   # set False to execute real (paper) trades
DEBUG_MODE = False
LOG_LEVEL  = "DEBUG" if DEBUG_MODE else "INFO"

# ── Technical Indicator Parameters ────────────────────────────────────────
# Used by pipeline/features.py
SMA_SHORT        = 50
SMA_LONG         = 200
RSI_PERIOD       = 14
MACD_FAST        = 12
MACD_SLOW        = 26
MACD_SIGNAL      = 9
MIN_HISTORY_DAYS = 252   # minimum days of price history required

# Momentum lookback windows (in calendar days)
MOMENTUM_3M  = 63
MOMENTUM_6M  = 126
MOMENTUM_12M = 252

# ── Cache ──────────────────────────────────────────────────────────────────
CACHE_MAX_AGE_HOURS = 4   # refresh cache if older than this
# Backward compat alias: PRICE_HISTORY_DAYS was previously HISTORY_DAYS
HISTORY_DAYS = PRICE_HISTORY_DAYS

# ── Scoring Thresholds (for signal labelling in portfolio.py) ──────────────
MOMENTUM_STRONG_THRESHOLD = 0.6   # normalized score above this -> "strong"
TREND_BULLISH_THRESHOLD   = 0.5   # normalized score above this -> "bullish"

# ── Executor ───────────────────────────────────────────────────────────────
EQUAL_WEIGHT = 1.0 / TOP_N_STOCKS   # 10% per position in equal-weight mode


# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Investment Alpha Config v2.0 ===")
    print(f"Universe size      : {len(ALL_TICKERS)} tickers")
    print(f"Top-N stocks       : {TOP_N_STOCKS}")
    print(f"Allocation mode    : {ALLOCATION_MODE}")
    print(f"Skip-month momentum: {SKIP_MONTH_MOMENTUM}")
    print(f"Sector cap         : {SECTOR_CAP_ENABLED} (max {SECTOR_MAX_STOCKS}/sector)")
    print(f"Regime detection   : {REGIME_ENABLED}")
    print(f"  VIX thresholds   : neutral>{REGIME_VIX_NEUTRAL}, bear>{REGIME_VIX_BEAR}")
    print(f"  Top-N by regime  : {REGIME_TOP_N}")
    print(f"Stop-loss          : {STOP_LOSS_ENABLED}")
    print(f"  Thresholds       : {STOP_LOSS_PCT}")
    print(f"Sentiment (Phase2) : {SENTIMENT_ENABLED}")
    print(f"  Finnhub key set  : {bool(FINNHUB_API_KEY)}")
    print(f"Meme filter (Ph2)  : {MEME_FILTER_ENABLED}")
    print(f"Insider filter(Ph2): {INSIDER_ENABLED}")
    print(f"Alpaca key set     : {bool(ALPACA_API_KEY)}")
    print(f"Dry run            : {DRY_RUN}")
    print(f"Output dir         : {OUTPUT_DIR}")
