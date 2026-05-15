"""
London Close **mean reversion** (distinct from London Open breakout).

Trend exhaustion / position squaring into 16:00–17:00 **local** (session_clock),
often fading the London leg — **not** the same edge as the open impulse breakout.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from signals import RegimeOutput, RegimeState
from . import _bias_path  # noqa: F401
from .market_context import calendar_context, cot_context_for_pair

import config
import session


def london_close_plan(
    pair: str,
    *,
    now_utc: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Playbook for the **London close** MR leg: pre-close watch, close window, flat-by time.

    ``dominant_london_trend`` is only a **slow** hint (COT weekly_bias); your execution
    layer should infer intraday trend from structure (e.g. VWAP, swing) after the open move.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    now_local = session.to_local(now_utc)
    cal_cfg = config.config.get("calendar", {})
    cal = calendar_context(
        pair,
        hours_ahead=int(cal_cfg.get("risk_hours_ahead", 8)),
        min_impact=str(cal_cfg.get("min_impact", "High")),
        now_utc=now_utc,
    )
    cot_snap = cot_context_for_pair(pair)

    pre = session.allow_london_close_pre(now_utc)
    close_win = session.allow_london_close_window(now_utc)
    past_flat = session.past_lc_flat_cutoff(now_utc)

    weekly = cot_snap.get("weekly_bias") if cot_snap.get("ok") else None
    mr = cot_snap.get("mean_reversion_hint") if cot_snap.get("ok") else None

    mechanics = {
        "logic": (
            "After London trend leg, institutions unwind into the close — "
            "fade / reversion toward intraday mean."
        ),
        "timing": {
            "pre_close_watch_local": "session_clock.london_close_pre (default 15:30–16:00)",
            "close_window_local": "session_clock.london_close (default 16:00–17:00)",
            "flat_positions_by_local": "session_clock.lc_flat_by_local (default 21:00)",
        },
        "entry_style": "counter-trend retracement starting in pre-close window; tight stop.",
        "target_levels": [
            "session_vwap",
            "session_midpoint",
            "asian_range_boundary_or_mid",
        ],
        "risk": "Short-duration; do not hold past flat-by time — overnight erodes this edge.",
    }

    base: dict[str, Any] = {
        "strategy_kind": "london_close_mean_reversion",
        "edge": (
            "Mechanical session-end behaviour (squaring / rebalancing) vs open-breakout liquidity."
        ),
        "pair": pair.upper(),
        "as_of_utc": now_utc.isoformat(),
        "as_of_local": now_local.isoformat(),
        "session_clock_tz": config.config.get("session_clock", {}).get("tz_name"),
        "session_in_pre_close_watch": pre,
        "session_in_close_window": close_win,
        "past_lc_flat_cutoff": past_flat,
        "calendar": cal,
        "cot_support": cot_snap,
        "dominant_london_trend_hint": {
            "weekly_bias_from_cot": weekly,
            "note": "Intraday trend vs open move must come from price structure in your feed.",
        },
        "mean_reversion_hint": mr,
        "close_mechanics": mechanics,
    }

    if cal_cfg.get("block_if_high_impact_in_window", True) and cal.get(
        "blackout_new_positions_suggested"
    ):
        base.update(
            playbook="calendar_blackout_suggested",
            engage_mean_reversion_leg=False,
            monitor_pre_close_structure=False,
        )
        return base

    if past_flat:
        base.update(
            playbook="past_flat_cutoff_no_new_lc_risk",
            engage_mean_reversion_leg=False,
            monitor_pre_close_structure=False,
        )
        return base

    if not cot_snap.get("ok"):
        base.update(
            playbook="no_cot_data",
            engage_mean_reversion_leg=False,
            monitor_pre_close_structure=pre or close_win,
        )
        return base

    if not cot_snap.get("usable"):
        base.update(
            playbook="cot_stale_or_roll",
            engage_mean_reversion_leg=False,
            monitor_pre_close_structure=pre or close_win,
        )
        return base

    engage_mr = close_win and RegimeOutput.is_active(RegimeState.NO_SIGNAL)
    monitor_struct = (pre or close_win) and not cal.get("blackout_new_positions_suggested")

    if engage_mr:
        playbook = f"mean_reversion_{mr}"
    elif close_win:
        playbook = "close_window_monitor_reversion_structure"
    elif pre:
        playbook = "pre_close_watch_for_countertrend"
    else:
        playbook = "outside_london_close_windows"

    base.update(
        playbook=playbook,
        engage_mean_reversion_leg=engage_mr,
        monitor_pre_close_structure=monitor_struct,
    )
    return base
