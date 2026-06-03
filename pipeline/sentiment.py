"""
pipeline/sentiment.py - Phase 3B: Analyst Revision Sentiment

Phase 3 replaces Finnhub keyword scoring + Reddit mentions with
institutional-grade analyst revision signals:

  analyst_score = blend of:
    - Analyst target price upside: (target_mean / current_price) - 1
    - Analyst recommendation normalised: 1=strong buy->1.0, 5=sell->0.0

This data is fetched in pipeline/ingestion.py (yfinance .info) and
computed directly in pipeline/features.py as 'analyst_score'.

This module is a lightweight compatibility shim so main.py's
sentiment injection block still works, but instead of fetching
external APIs it simply reads from the features dataframe.

If SENTIMENT_ENABLED=True, scoring.py will use 'analyst_score' from
features automatically.

Legacy Finnhub / Reddit code is preserved below but disabled.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)


def run(tickers: list, features_df=None) -> dict:
    """
    Phase 3: Sentiment is derived from analyst revisions embedded
    in the features dataframe (fetched via yfinance in ingestion).

    Returns a status dict. The actual signal is in features_df['analyst_score'].
    main.py no longer needs to inject separate sentiment columns because
    features.py already computes analyst_score directly.

    Args:
        tickers: list of tickers (for logging)
        features_df: optional features DataFrame (unused, for compat)

    Returns:
        dict with status indicating the signal source
    """
    if not getattr(config, "SENTIMENT_ENABLED", False):
        log.info("SENTIMENT_ENABLED=False -- sentiment skipped")
        return {
            "sentiment_scores":  {},
            "reddit_mentions":   {},
            "tickers_fetched":   0,
            "status":            "disabled",
            "signal_source":     "none",
        }

    # Phase 3: analyst_score is already in features dataframe
    # No external API call needed here
    log.info("Sentiment: Phase 3 analyst revision mode (data from ingestion/features)")
    return {
        "sentiment_scores":  {},   # no longer used -- analyst_score is in features df
        "reddit_mentions":   {},   # dropped in Phase 3
        "tickers_fetched":   len(tickers) if tickers is not None else 0,
        "status":            "success",
        "signal_source":     "analyst_revisions",
    }


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(message)s")

    TEST_TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "JPM", "META"]
    print("\n=== Sentiment Module Test (Phase 3: Analyst Revisions) ===")
    result = run(TEST_TICKERS)
    print(f"Status       : {result['status']}")
    print(f"Signal source: {result['signal_source']}")
    print(f"Note: analyst_score is computed inside pipeline/features.py")
    print("      using yfinance targetMeanPrice + recommendationMean")
    print("\nTo verify analyst scores, run: python pipeline/features.py")
