"""
pipeline/regime.py - Market Regime Classifier

Fetches SPX and VIX data via yfinance and classifies the current market
regime as BULL, NEUTRAL, or BEAR.

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

import yfinance as yf
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

logger = logging.getLogger(__name__)

SPX_MA_DAYS = 200
SPX_BEAR_THRESHOLD = -0.05


def _get_yield_curve_signal():
    """10Y - 3M Treasury spread. Negative = inverted = recession warning."""
    try:
        t10 = yf.download("^TNX", period="5d", progress=False, auto_adjust=True)
        t3m = yf.download("^IRX", period="5d", progress=False, auto_adjust=True)
        if t10.empty or t3m.empty:
            return None
        spread = float(t10["Close"].squeeze().iloc[-1]) - float(t3m["Close"].squeeze().iloc[-1])
        logger.debug("Yield curve spread (10Y-3M): %.4f", spread)
        return round(spread, 4)
    except Exception as exc:
        logger.debug("Yield curve fetch error: %s", exc)
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

    # Fetch VIX
    try:
        vix_raw = yf.download(config.VIX_TICKER, period="5d", auto_adjust=True, progress=False)
        if vix_raw.empty:
            return _safe_fallback("No VIX data returned")
        vix_current = float(vix_raw["Close"].squeeze().iloc[-1])
    except Exception as exc:
        return _safe_fallback("VIX fetch error: " + str(exc))

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
