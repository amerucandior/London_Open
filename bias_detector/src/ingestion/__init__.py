"""Ingestion used by London_Open strategies: COT + economic calendar."""

from .calendar import fetch_calendar, fetch_upcoming_events, FREDClient
from .cot import (
    COTStrategyContext,
    fetch_cot,
    fetch_cot_all_pairs,
    get_cot_strategy_context,
    get_latest_cot,
    list_cot_pairs,
)

__all__ = [
    "fetch_calendar",
    "fetch_upcoming_events",
    "FREDClient",
    "COTStrategyContext",
    "fetch_cot",
    "fetch_cot_all_pairs",
    "get_cot_strategy_context",
    "get_latest_cot",
    "list_cot_pairs",
]
