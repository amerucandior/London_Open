"""
src/ingestion/__init__.py
─────────────────────────
FX Bias Detector — ingestion package.
"""

from .calendar  import fetch_calendar, fetch_upcoming_events
from .cot       import fetch_cot, fetch_cot_all_pairs, get_latest_cot
from .sentiment import fetch_sentiment, fetch_sentiment_all_pairs, get_sentiment_signal

__all__ = [
    "fetch_calendar",
    "fetch_upcoming_events",
    "fetch_cot",
    "fetch_cot_all_pairs",
    "get_latest_cot",
    "fetch_sentiment",
    "fetch_sentiment_all_pairs",
    "get_sentiment_signal",
]