"""Ingestion used by London_Open strategies: COT + economic calendar."""

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


def __getattr__(name: str):
    if name in {"fetch_calendar", "fetch_upcoming_events", "FREDClient"}:
        from .economic_calendar import FREDClient, fetch_calendar, fetch_upcoming_events

        exports = {
            "fetch_calendar": fetch_calendar,
            "fetch_upcoming_events": fetch_upcoming_events,
            "FREDClient": FREDClient,
        }
        return exports[name]

    if name in {
        "COTStrategyContext",
        "fetch_cot",
        "fetch_cot_all_pairs",
        "get_cot_strategy_context",
        "get_latest_cot",
        "list_cot_pairs",
    }:
        from .cot import (
            COTStrategyContext,
            fetch_cot,
            fetch_cot_all_pairs,
            get_cot_strategy_context,
            get_latest_cot,
            list_cot_pairs,
        )

        exports = {
            "COTStrategyContext": COTStrategyContext,
            "fetch_cot": fetch_cot,
            "fetch_cot_all_pairs": fetch_cot_all_pairs,
            "get_cot_strategy_context": get_cot_strategy_context,
            "get_latest_cot": get_latest_cot,
            "list_cot_pairs": list_cot_pairs,
        }
        return exports[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
