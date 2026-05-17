"""
London Open **breakout** (distinct from London Close mean reversion).

Asian range → **close** outside range during open impulse → COT/calendar as filters.
CBDR (prior NY 14:00–20:00) supplies **LoD / HoD** projection at 1σ and 2σ band multiples.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from . import _bias_path  # noqa: F401
from .cbdr_context import cbdr_block_for_london_open
from .market_context import calendar_context, cot_context_for_pair

import asian_range
import config
import session
from ingestion.cot import get_latest_cot
from signals.regime import classify_regime, RegimeOutput
from utils import HAS_MT5


def _asian_block(pair: str, now_local: datetime) -> dict[str, Any]:
    # Dev environments without MetaTrader5 (Linux/Mac) skip price-feed paths
    # rather than raising every run. The Windows trading box has HAS_MT5=True.
    if not HAS_MT5:
        return {"ok": False, "skipped": True, "reason": "no_mt5_dev_env", "source": None}
    try:
        rng = asian_range.get_today_asian_range(pair, on=now_local)
        width = float(rng["high"]) - float(rng["low"])
        return {
            "ok": True,
            "high": rng["high"],
            "low": rng["low"],
            "mid": rng["mid"],
            "range_width_price": width,
            "target_distance_hints": {
                "1.5x_range": 1.5 * width,
                "2.0x_range": 2.0 * width,
            },
            "source": "mt5_session_clock_asian",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "source": "mt5_unavailable_or_range_failed"}


def _breakout_rules(
    asian: dict[str, Any],
    cbdr_payload: dict[str, Any],
) -> dict[str, Any]:
    rules: dict[str, Any] = {
        "entry_trigger": "bar_close_outside_asian_range",
        "confirmation": (
            "Use completed candle **close** beyond Asian high/low — "
            "do not enter on wicks alone (reduces false breakouts)."
        ),
        "long_condition": "last_closed_close > asian_high",
        "short_condition": "last_closed_close < asian_low",
        "stop_loss_options": [
            "opposite_side_of_asian_range",
            "asian_midpoint_tighter_risk",
        ],
        "profit_taking_hints": asian.get("target_distance_hints")
        if "target_distance_hints" in asian
        else "needs_range_width",
        "cbdr_confluence": (
            "Full CBDR + 1σ/2σ HoD/LoD prices live under top-level ``cbdr_lod_hod`` when ``ok``."
            if cbdr_payload.get("ok")
            else cbdr_payload.get("reason")
                 or cbdr_payload.get("error", "cbdr_unavailable")
        ),
    }
    return rules


def london_open_plan(
    pair: str,
    base_lot_size: float = 1.0,
    *,
    now_utc: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Hunt setup for London Open **range breakout** + CBDR **LoD/HoD** context.

    ``setup_armed`` / ``trade``: filters + session say you may **scan** for a breakout.
    ``breakout_confirmed`` stays ``None`` until your bar engine sets long/short from **closes**.
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
    asian = _asian_block(pair, now_local)
    in_impulse = session.allow_london_open_impulse(now_utc)

    row = get_latest_cot(pair)
    regime: RegimeOutput | None = None
    cbdr_payload: dict[str, Any] = {"ok": False, "error": "no_cot_yet"}

    if row is not None:
        regime = classify_regime(row)
        cbdr_payload = cbdr_block_for_london_open(
            pair, now_utc, direction_bias=regime.direction
        )

    base: dict[str, Any] = {
        "strategy_kind": "london_open_breakout",
        "edge": (
            "Asian consolidation builds a range; London open impulse breaks it — "
            "stops cluster above/below range (liquidity). CBDR projects intraday HoD/LoD "
            "extensions at 1σ and 2σ of the prior NY dealers band."
        ),
        "pair": pair.upper(),
        "as_of_utc": now_utc.isoformat(),
        "as_of_local": now_local.isoformat(),
        "session_clock_tz": config.config.get("session_clock", {}).get("tz_name"),
        "asian_range": asian,
        "breakout_mechanics": _breakout_rules(asian, cbdr_payload),
        "cbdr_lod_hod": cbdr_payload,
        "session_in_open_impulse_window": in_impulse,
        "calendar": cal,
        "cot_filter": cot_snap,
        "breakout_confirmed": None,
        "requires_intraday_bars": True,
    }

    if row is None:
        base.update(
            setup_armed=False,
            trade=False,
            reason="no_cot_data",
        )
        return base

    assert regime is not None

    if cal_cfg.get("block_if_high_impact_in_window", True) and cal.get(
        "blackout_new_positions_suggested"
    ):
        base.update(
            cot_regime=_regime_dict(regime),
            setup_armed=False,
            trade=False,
            reason="calendar_blackout_suggested",
        )
        return base

    if not in_impulse:
        base.update(
            cot_regime=_regime_dict(regime),
            setup_armed=False,
            trade=False,
            reason="outside_london_open_impulse_window",
        )
        return base

    if not regime.is_active():
        base.update(
            cot_regime=_regime_dict(regime),
            setup_armed=False,
            trade=False,
            reason=regime.state.value,
        )
        return base

    # In crowded / mixed regimes we de-risk via size, not via a tighter stop:
    # tighter stops in squeeze regimes get bled out by the noise around the
    # structural level (opposite side of the Asian range). Keep the stop
    # where price structure dictates and shrink the lot instead.
    regime_size_haircut = 0.7 if regime.state.value in ("SQUEEZE_RISK", "NEUTRAL") else 1.0
    lot_size = round(base_lot_size * regime.position_multiplier * regime_size_haircut, 2)

    base.update(
        cot_regime=_regime_dict(regime),
        setup_armed=True,
        trade=True,
        reason="armed_for_breakout_scan",
        direction_bias=regime.direction,
        lot_size=lot_size,
        signal_strength=float(row.get("signal_strength", 0.0)),
    )
    return base


def _regime_dict(regime: RegimeOutput) -> dict[str, Any]:
    return {
        "state": regime.state.value,
        "direction_bias": regime.direction,
        "position_multiplier": regime.position_multiplier,
        "context": regime.signal_context,
    }
