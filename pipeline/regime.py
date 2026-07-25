"""
pipeline/regime.py - Market Regime Classifier

Classifies the current market regime as BULL, NEUTRAL, or BEAR from SPX
(yfinance — 200-day MA computed in-code), VIX (CBOE's own daily-prices CSV,
the authoritative primary source since VIX is CBOE's index; yfinance
fallback), and the 10Y-3M treasury spread (FRED's DGS10/DGS3MO
constant-maturity series, the authoritative source for treasury yields;
yfinance ^TNX/^IRX fallback). See _get_vix_current / _get_fred_yield.

Rules:
  BULL    : SPX > 200-day MA  AND  VIX < REGIME_VIX_NEUTRAL
  NEUTRAL : SPX > 200-day MA  AND  VIX >= REGIME_VIX_NEUTRAL
         OR SPX <= 200-day MA AND  VIX < REGIME_VIX_BEAR
  BEAR    : VIX >= REGIME_VIX_BEAR  OR  SPX well below 200-day MA

Phase 4: Yield curve (10Y-3M) and credit spread (HYG/LQD) signals
can downgrade regime by one level when bearish.
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timezone

import requests
import yfinance as yf
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

logger = logging.getLogger(__name__)

SPX_MA_DAYS = 200
SPX_BEAR_THRESHOLD = -0.05

_CBOE_VIX_CSV = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
_FRED_REST_URL = "https://api.stlouisfed.org/fred/series/observations"
_FRED_CSV_URL  = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def _get_vix_current():
    """
    Latest VIX close. CBOE's own daily-prices CSV is the authoritative primary
    source (VIX is CBOE's index — no one else "computes" it, they just relay
    it), so it's tried first. Falls back to yfinance if CBOE is unreachable.
    """
    try:
        r = requests.get(_CBOE_VIX_CSV, timeout=10)
        r.raise_for_status()
        lines = [ln for ln in r.text.strip().splitlines() if ln.strip()]
        last_row = lines[-1].split(",")  # DATE,OPEN,HIGH,LOW,CLOSE
        vix = float(last_row[4])
        logger.debug("VIX from CBOE: %.2f (as of %s)", vix, last_row[0])
        return vix
    except Exception as exc:
        logger.debug("CBOE VIX fetch failed (%s) — falling back to yfinance", exc)

    try:
        vix_raw = yf.download(config.VIX_TICKER, period="5d", auto_adjust=True, progress=False)
        if not vix_raw.empty:
            vix = float(vix_raw["Close"].squeeze().iloc[-1])
            logger.debug("VIX from yfinance fallback: %.2f", vix)
            return vix
    except Exception as exc:
        logger.debug("yfinance VIX fetch also failed: %s", exc)
    return None


def _get_fred_yield(series_id: str) -> float | None:
    """
    Latest value for a FRED constant-maturity treasury series (e.g. DGS10,
    DGS3MO) — the authoritative source for treasury yields. Tries the official
    REST API first (needs config.FRED_API_KEY, free 2-min signup at
    fred.stlouisfed.org/docs/api/api_key.html); falls back to FRED's
    unauthenticated CSV endpoint (no key needed, same data) if no key is set
    or the API call fails.
    """
    api_key = getattr(config, "FRED_API_KEY", "")
    if api_key:
        try:
            r = requests.get(
                _FRED_REST_URL,
                params={
                    "series_id": series_id, "api_key": api_key, "file_type": "json",
                    "sort_order": "desc", "limit": 5,
                },
                timeout=10,
            )
            r.raise_for_status()
            for obs in r.json().get("observations", []):
                val = obs.get("value")
                if val not in (None, ".", ""):
                    return float(val)
        except Exception as exc:
            logger.debug("FRED REST API failed for %s (%s) — trying CSV fallback", series_id, exc)

    try:
        r = requests.get(_FRED_CSV_URL, params={"id": series_id}, timeout=10)
        r.raise_for_status()
        lines = [ln for ln in r.text.strip().splitlines() if ln.strip()]
        for line in reversed(lines[1:]):  # skip header, walk back to last non-missing value
            _, _, val = line.partition(",")
            val = val.strip()
            if val and val != ".":
                return float(val)
    except Exception as exc:
        logger.debug("FRED CSV fetch failed for %s: %s", series_id, exc)
    return None


def _get_yield_curve_signal():
    """
    10Y - 3M Treasury spread. Negative = inverted = recession warning.
    FRED's DGS10/DGS3MO constant-maturity series are the primary source (see
    _get_fred_yield); falls back to yfinance's ^TNX/^IRX index tickers only if
    FRED is unreachable entirely.
    """
    t10 = _get_fred_yield("DGS10")
    t3m = _get_fred_yield("DGS3MO")
    if t10 is not None and t3m is not None:
        spread = round(t10 - t3m, 4)
        logger.debug("Yield curve spread (10Y-3M, FRED): %.4f", spread)
        return spread

    logger.debug("FRED yield data unavailable — falling back to yfinance ^TNX/^IRX")
    try:
        t10_yf = yf.download("^TNX", period="5d", progress=False, auto_adjust=True)
        t3m_yf = yf.download("^IRX", period="5d", progress=False, auto_adjust=True)
        if t10_yf.empty or t3m_yf.empty:
            return None
        spread = float(t10_yf["Close"].squeeze().iloc[-1]) - float(t3m_yf["Close"].squeeze().iloc[-1])
        logger.debug("Yield curve spread (10Y-3M, yfinance fallback): %.4f", spread)
        return round(spread, 4)
    except Exception as exc:
        logger.debug("Yield curve fetch error (all sources failed): %s", exc)
        return None


def _get_credit_spread_signal():
    """HYG/LQD 20-day momentum as credit spread proxy. Negative = widening = risk-off."""
    try:
        hyg = yf.download("HYG", period="30d", progress=False, auto_adjust=True)["Close"].squeeze()
        lqd = yf.download("LQD", period="30d", progress=False, auto_adjust=True)["Close"].squeeze()
        if hyg.empty or lqd.empty or len(hyg) < 5:
            return None
        ratio = (hyg / lqd).dropna()
        momentum = float(ratio.iloc[-1]) / float(ratio.iloc[0]) - 1
        logger.debug("Credit spread momentum (HYG/LQD): %.6f", momentum)
        return round(momentum, 6)
    except Exception as exc:
        logger.debug("Credit spread fetch error: %s", exc)
        return None


def _safe_fallback(reason):
    """
    Return a NEUTRAL regime so the rest of the pipeline keeps running.

    NOTE: this used to default to BULL, meaning a yfinance outage silently
    reported maximum-risk-on. Data failure now means caution: NEUTRAL
    position counts and NEUTRAL stops until data returns.
    (Exception: REGIME_ENABLED=False is a deliberate user choice → BULL.)
    """
    fallback = "bull" if reason == "REGIME_ENABLED=False" else \
               getattr(config, "REGIME_FALLBACK", "neutral")
    logger.warning("Regime fallback to %s: %s", fallback.upper(), reason)
    return {
        "regime":                 fallback,
        "vix_current":            None,
        "spx_price":              None,
        "spx_200ma":              None,
        "spx_vs_200ma_pct":       None,
        "yield_curve_spread":     None,
        "credit_spread_momentum": None,
        "active_top_n":           config.REGIME_TOP_N[fallback],
        "active_stop_loss":       config.STOP_LOSS_PCT[fallback],
        "timestamp":              datetime.now(timezone.utc).isoformat(),
        "notes":                  "Fallback - " + reason,
    }


def run():
    """Classify market regime. Returns dict with regime, metrics, and active parameters."""
    if not config.REGIME_ENABLED:
        logger.info("Regime detection disabled - defaulting to BULL")
        return _safe_fallback("REGIME_ENABLED=False")

    # Fetch SPX
    try:
        spx_raw = yf.download(config.SPX_TICKER, period="300d", auto_adjust=True, progress=False)
        if spx_raw.empty or len(spx_raw) < SPX_MA_DAYS:
            return _safe_fallback("Insufficient SPX data")
        spx_close = spx_raw["Close"].squeeze()
        spx_price = float(spx_close.iloc[-1])
        spx_200ma = float(spx_close.rolling(SPX_MA_DAYS).mean().iloc[-1])
        spx_vs_200ma_pct = (spx_price - spx_200ma) / spx_200ma
    except Exception as exc:
        return _safe_fallback("SPX fetch error: " + str(exc))

    # Fetch VIX (CBOE official CSV primary, yfinance fallback — see _get_vix_current)
    vix_current = _get_vix_current()
    if vix_current is None:
        return _safe_fallback("No VIX data returned (CBOE + yfinance both failed)")

    # Primary classification: VIX + SPX 200MA
    above_200ma = spx_vs_200ma_pct > 0
    far_below   = spx_vs_200ma_pct < SPX_BEAR_THRESHOLD

    if vix_current >= config.REGIME_VIX_BEAR or far_below:
        regime = "bear"
        if vix_current >= config.REGIME_VIX_BEAR:
            notes = "VIX=%.1f >= %s bear threshold" % (vix_current, config.REGIME_VIX_BEAR)
        else:
            notes = "SPX %.1f%% below 200MA" % (spx_vs_200ma_pct * 100)
    elif vix_current >= config.REGIME_VIX_NEUTRAL or not above_200ma:
        regime = "neutral"
        if vix_current >= config.REGIME_VIX_NEUTRAL:
            notes = "VIX=%.1f >= %s neutral threshold" % (vix_current, config.REGIME_VIX_NEUTRAL)
        else:
            notes = "SPX below 200MA (%.1f%%)" % (spx_vs_200ma_pct * 100)
    else:
        regime = "bull"
        notes = "SPX %.1f%% above 200MA, VIX=%.1f (benign)" % (spx_vs_200ma_pct * 100, vix_current)

    # Secondary: yield curve + credit spread downgrade
    downgrade_reasons = []
    yc_spread = None
    cs_mom    = None

    if getattr(config, "YIELD_CURVE_ENABLED", False):
        yc_spread = _get_yield_curve_signal()
        yc_threshold = getattr(config, "YIELD_CURVE_BEAR_THRESHOLD", -0.50)
        if yc_spread is not None and yc_spread < yc_threshold:
            downgrade_reasons.append(
                "yield curve inverted (%.2fpp < %.2fpp threshold)" % (yc_spread, yc_threshold)
            )

    if getattr(config, "CREDIT_SPREAD_ENABLED", False):
        cs_mom = _get_credit_spread_signal()
        cs_threshold = getattr(config, "CREDIT_SPREAD_BEAR_PCT", -0.03)
        if cs_mom is not None and cs_mom < cs_threshold:
            downgrade_reasons.append(
                "credit spreads widening (%.2f%% < %.1f%% threshold)" % (cs_mom * 100, cs_threshold * 100)
            )

    if downgrade_reasons and regime != "bear":
        _DOWNGRADE = {"bull": "neutral", "neutral": "bear"}
        old_regime = regime
        regime = _DOWNGRADE[regime]
        notes += " | DOWNGRADED %s->%s: %s" % (old_regime, regime, "; ".join(downgrade_reasons))
        logger.info("Regime downgraded %s->%s: %s", old_regime.upper(), regime.upper(),
                    "; ".join(downgrade_reasons))

    result = {
        "regime":                 regime,
        "vix_current":            round(vix_current, 2),
        "spx_price":              round(spx_price, 2),
        "spx_200ma":              round(spx_200ma, 2),
        "spx_vs_200ma_pct":       round(spx_vs_200ma_pct * 100, 2),
        "yield_curve_spread":     yc_spread if getattr(config, "YIELD_CURVE_ENABLED", False) else None,
        "credit_spread_momentum": cs_mom    if getattr(config, "CREDIT_SPREAD_ENABLED", False) else None,
        "active_top_n":           config.REGIME_TOP_N[regime],
        "active_stop_loss":       config.STOP_LOSS_PCT[regime],
        "timestamp":              datetime.now(timezone.utc).isoformat(),
        "notes":                  notes,
    }

    logger.info("Regime: %s | VIX=%.1f | SPX vs 200MA: %.1f%% | top_n=%d",
                regime.upper(), vix_current, spx_vs_200ma_pct * 100, result["active_top_n"])
    return result


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("\n=== Market Regime Classifier ===")
    result = run()
    print(json.dumps(result, indent=2))
    print("\nRegime  :", result["regime"].upper())
    print("VIX     :", result["vix_current"])
    print("SPX     :", result["spx_price"], "(200MA:", result["spx_200ma"], ")")
    print("vs 200MA:", result["spx_vs_200ma_pct"], "%")
    print("Top-N   :", result["active_top_n"])
    print("Notes   :", result["notes"])
