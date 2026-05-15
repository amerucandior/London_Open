"""
src/signals/regime.py
──────────────────────
Regime classification and position sizing for the FX bias detector.

Consumes output of fetch_cot() and produces:
    - A RegimeState enum (BULL / BEAR / NEUTRAL / SQUEEZE_RISK / NO_SIGNAL)
    - A float position_multiplier (0.0 → 2.0)
    - A dict signal_context for logging/audit

Design principle:
    COT is a FILTER, not an entry trigger.
    This module gates your London Open breakout — it does not fire trades.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import pandas as pd
import structlog

log = structlog.get_logger(__name__)

# ── Regime States ─────────────────────────────────────────────────────────────

class RegimeState(Enum):
    BULL         = "BULL"          # All signals aligned bullish — take long breakouts only
    BEAR         = "BEAR"          # All signals aligned bearish — take short breakouts only
    NEUTRAL      = "NEUTRAL"       # Mixed signals — reduce size, avoid counter-trend
    SQUEEZE_RISK = "SQUEEZE_RISK"  # Crowded positioning — tighten stops, reduce size
    NO_SIGNAL    = "NO_SIGNAL"     # Roll week / stale data / missing features


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class RegimeConfig:
    # CCI thresholds. Positioning has a sweet spot — too neutral = no edge,
    # too extreme = crowded mean-revert risk. Strength is U-shaped in |cci_z|.
    cci_strong:  float = 1.0   # |cci_z| above this = strong directional regime
    cci_weak:    float = 0.5   # |cci_z| below this = neutral zone
    cci_taper:   float = 1.5   # |cci_z| above this = trim continuation scaling
    cci_crowded: float = 1.75  # |cci_z| above this = asset-manager crowded → fade

    # Leveraged money
    lev_extreme: float = 2.0   # |lev_z| above this = crowded / squeeze risk
    lev_warning: float = 1.5   # |lev_z| above this = elevated crowding

    # Flow alignment
    min_signal_strength: float = 0.20  # Below this threshold = weak signal, reduce size

    # Position sizing multipliers
    scale_strong_confirmed: float = 1.5   # Strong regime + aligned flow
    scale_confirmed:        float = 1.25  # Confirmed but not strong
    scale_neutral:          float = 0.75  # Neutral regime
    scale_squeeze:          float = 0.5   # Crowded positioning
    scale_conflict:         float = 0.6   # Flows conflicting
    scale_no_signal:        float = 0.0   # Kill switch

    # Concentration penalty
    conc_penalty:           float = 0.6   # Multiply by this when is_high_conc=True


# ── Output ────────────────────────────────────────────────────────────────────

@dataclass
class RegimeOutput:
    state:               RegimeState
    position_multiplier: float
    direction:           int          # +1 = bias long, -1 = bias short, 0 = flat
    signal_context:      dict = field(default_factory=dict)

    def allows_long(self)  -> bool:
        return self.state in (RegimeState.BULL,) and self.direction == 1

    def allows_short(self) -> bool:
        return self.state in (RegimeState.BEAR,) and self.direction == -1

    def is_active(self) -> bool:
        return self.state not in (RegimeState.NO_SIGNAL,) and self.position_multiplier > 0


# ── Core Classifier ───────────────────────────────────────────────────────────

def classify_regime(
    cot_row: pd.Series,
    config: Optional[RegimeConfig] = None,
) -> RegimeOutput:
    """
    Classify the current COT regime from the latest COT record.

    Parameters
    ----------
    cot_row : pd.Series
        Single row from fetch_cot() output — typically get_latest_cot()
    config  : RegimeConfig, optional
        Override defaults for live tuning

    Returns
    -------
    RegimeOutput with state, position_multiplier, direction, and audit dict
    """
    cfg = config or RegimeConfig()

    # ── Gate 1: Data Quality ──────────────────────────────────────────────────
    quality_flag   = int(cot_row.get("quality_flag", 2))
    is_roll_week   = bool(cot_row.get("is_roll_week", False))
    is_roll_trans  = bool(cot_row.get("is_roll_transition", True))

    if quality_flag == 2 or is_roll_trans:
        return RegimeOutput(
            state=RegimeState.NO_SIGNAL,
            position_multiplier=0.0,
            direction=0,
            signal_context={"reason": "stale_data_or_roll", "quality_flag": quality_flag}
        )

    # ── Extract Features ──────────────────────────────────────────────────────
    cci_z              = float(cot_row.get("cci_z", 0.0))
    lev_z              = float(cot_row.get("lev_z", 0.0))
    is_crowded_long    = bool(cot_row.get("is_crowded_long", False))
    is_crowded_short   = bool(cot_row.get("is_crowded_short", False))
    is_high_conc       = bool(cot_row.get("is_high_conc", False))
    regime_confirmed   = bool(cot_row.get("regime_confirmed", False))
    alignment_dir      = int(cot_row.get("alignment_direction", 0))
    signal_strength    = float(cot_row.get("signal_strength", 0.0))
    flow_aligned       = bool(cot_row.get("flow_aligned", False))
    position_scale     = float(cot_row.get("position_scale", 1.0))

    context = {
        "cci_z": round(cci_z, 3),
        "lev_z": round(lev_z, 3),
        "regime_confirmed": regime_confirmed,
        "alignment_direction": alignment_dir,
        "signal_strength": round(signal_strength, 3),
        "flow_aligned": flow_aligned,
        "is_crowded_long": is_crowded_long,
        "is_crowded_short": is_crowded_short,
        "is_high_conc": is_high_conc,
        "quality_flag": quality_flag,
    }

    # ── Gate 2: Squeeze Risk — highest priority after data quality ────────────
    # Crowded positioning = high reversal risk regardless of direction.
    # Trigger on either leveraged-money extreme (`lev_z`) OR asset-manager
    # extreme (`cci_z`). Asset-manager crowding is slower but the dominant
    # reversal driver in FX — missing it means doubling up at tops/bottoms.
    cci_crowded_long  = cci_z >=  cfg.cci_crowded
    cci_crowded_short = cci_z <= -cfg.cci_crowded
    crowded_long  = is_crowded_long  or cci_crowded_long
    crowded_short = is_crowded_short or cci_crowded_short

    if crowded_long or crowded_short:
        squeeze_direction = -1 if crowded_long else 1  # Fade the crowd
        multiplier = cfg.scale_squeeze
        if is_high_conc:
            multiplier *= cfg.conc_penalty

        log.warning("regime.squeeze_risk",
                    lev_z=round(lev_z, 3),
                    cci_z=round(cci_z, 3),
                    crowded_long=crowded_long,
                    crowded_short=crowded_short,
                    trigger="cci" if (cci_crowded_long or cci_crowded_short)
                            and not (is_crowded_long or is_crowded_short) else "lev")

        return RegimeOutput(
            state=RegimeState.SQUEEZE_RISK,
            position_multiplier=round(multiplier, 3),
            direction=squeeze_direction,
            signal_context={**context, "reason": "crowded_positioning"}
        )

    # ── Gate 3: Weak Signal Threshold ─────────────────────────────────────────
    if signal_strength < cfg.min_signal_strength or abs(cci_z) < cfg.cci_weak:
        return RegimeOutput(
            state=RegimeState.NEUTRAL,
            position_multiplier=cfg.scale_neutral,
            direction=0,
            signal_context={**context, "reason": "weak_signal"}
        )

    # ── Gate 4: Conflicting Flows ─────────────────────────────────────────────
    if not flow_aligned and not regime_confirmed:
        return RegimeOutput(
            state=RegimeState.NEUTRAL,
            position_multiplier=cfg.scale_conflict,
            direction=0,
            signal_context={**context, "reason": "conflicting_flows"}
        )

    # ── Gate 5: Direction Classification ─────────────────────────────────────
    # CCI is the primary directional anchor
    # Alignment direction from flow must agree with CCI to confirm
    if cci_z > 0:
        bias_direction = 1   # Dealers net-long vs specs (bullish)
    else:
        bias_direction = -1  # Dealers net-short vs specs (bearish)

    # Flow and CCI must agree — otherwise neutral
    if alignment_dir != 0 and alignment_dir != bias_direction:
        return RegimeOutput(
            state=RegimeState.NEUTRAL,
            position_multiplier=cfg.scale_neutral,
            direction=0,
            signal_context={**context, "reason": "cci_flow_disagreement"}
        )

    # ── Gate 6: Regime Strength Classification ────────────────────────────────
    # U-shape in |cci_z|: peak scaling sits in the "confirmed but not yet crowded"
    # band [cci_strong, cci_taper). Above cci_taper we step back to base
    # confirmed scaling — the crowded gate above already kicked in at cci_crowded.
    abs_cci = abs(cci_z)

    if regime_confirmed and cfg.cci_strong <= abs_cci < cfg.cci_taper:
        state      = RegimeState.BULL if bias_direction == 1 else RegimeState.BEAR
        multiplier = position_scale * cfg.scale_strong_confirmed

    elif regime_confirmed and abs_cci >= cfg.cci_taper:
        # Crowding-adjacent: keep direction but trim leverage back to base.
        state      = RegimeState.BULL if bias_direction == 1 else RegimeState.BEAR
        multiplier = position_scale * cfg.scale_confirmed

    elif regime_confirmed:
        state      = RegimeState.BULL if bias_direction == 1 else RegimeState.BEAR
        multiplier = position_scale * cfg.scale_confirmed

    else:
        # cci_z is meaningful but regime not fully confirmed
        state      = RegimeState.NEUTRAL
        multiplier = cfg.scale_neutral

    # ── Apply Concentration Penalty ───────────────────────────────────────────
    if is_high_conc:
        multiplier *= cfg.conc_penalty

    multiplier = round(min(max(multiplier, 0.0), 2.0), 3)

    log.info(
        "regime.classified",
        state=state.value,
        direction=bias_direction,
        multiplier=multiplier,
        cci_z=round(cci_z, 3),
        signal_strength=round(signal_strength, 3),
    )

    return RegimeOutput(
        state=state,
        position_multiplier=multiplier,
        direction=bias_direction,
        signal_context={**context, "reason": "classified"}
    )