"""Shared calendar + COT helpers for London Open / London Close strategies."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from . import _bias_path  # noqa: F401  — registers path before ingestion imports

from ingestion.calendar import fetch_upcoming_events, FREDClient
from ingestion.cot import get_cot_strategy_context, list_cot_pairs


def pair_to_calendar_currencies(pair: str) -> set[str]:
    """Map ``EURUSD``-style symbol to FF calendar currency codes."""
    p = pair.upper().replace("/", "").replace("-", "")
    if len(p) >= 6:
        return {p[:3], p[3:6]}
    return {"USD", "EUR"}


def upcoming_relevant_events(
    pair: str,
    *,
    hours_ahead: int = 8,
    min_impact: str = "High",
    fred_client: Optional[FREDClient] = None,
) -> pd.DataFrame:
    """
    High-(or-medium-) impact calendar rows in the next window affecting ``pair``'s currencies.
    """
    cur = pair_to_calendar_currencies(pair)
    df = fetch_upcoming_events(
        hours_ahead=hours_ahead,
        min_impact=min_impact,  # type: ignore[arg-type]
        fred_client=fred_client,
    )
    if df.empty:
        return df
    mask = df["currency"].isin(cur) | (df["currency"].str.upper() == "ALL")
    return df.loc[mask].reset_index(drop=True)


def calendar_context(
    pair: str,
    *,
    hours_ahead: int = 8,
    min_impact: str = "High",
    now_utc: Optional[datetime] = None,
    fred_client: Optional[FREDClient] = None,
) -> dict[str, Any]:
    """
    Summary for strategy gates: upcoming prints, blackout suggestion, row count.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    df = upcoming_relevant_events(
        pair, hours_ahead=hours_ahead, min_impact=min_impact, fred_client=fred_client
    )
    # Blackout: any High impact in window with parseable time soon
    blackout_suggested = not df.empty and min_impact in ("High", "Medium")
    return {
        "as_of_utc": now_utc.isoformat(),
        "pair": pair.upper(),
        "upcoming_event_count": len(df),
        "blackout_new_positions_suggested": blackout_suggested,
        "upcoming": df.to_dict("records") if len(df) <= 12 else df.head(12).to_dict("records"),
    }


def cot_context_for_pair(pair: str) -> dict[str, Any]:
    """Latest COT strategy snapshot (weekly bias + MR hints) as a plain dict."""
    ctx = get_cot_strategy_context(pair)
    if ctx is None:
        return {"ok": False, "reason": "no_cot_data", "pair": pair.upper()}
    d = {
        "ok": True,
        "pair": ctx.pair,
        "as_of_report_date": ctx.as_of_report_date,
        "lag_days": ctx.lag_days,
        "quality_flag": ctx.quality_flag,
        "usable": ctx.usable,
        "weekly_bias": ctx.weekly_bias,
        "mean_reversion_hint": ctx.mean_reversion_hint,
        "cci_z": ctx.cci_z,
        "lev_z": ctx.lev_z,
        "dealer_net": ctx.dealer_net,
        "dealer_net_pct": ctx.dealer_net_pct,
        "regime_confirmed": ctx.regime_confirmed,
        "signal_strength": ctx.signal_strength,
        "is_roll_week": ctx.is_roll_week,
        "is_roll_transition": ctx.is_roll_transition,
    }
    return d


def supported_pairs() -> list[str]:
    return list_cot_pairs()
