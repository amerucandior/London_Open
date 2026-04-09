# strategy/london_open.py  (simplified integration point)

from src.ingestion.cot import get_latest_cot
from src.signals.regime import classify_regime, RegimeConfig

def get_trade_parameters(pair: str, base_lot_size: float = 1.0) -> dict:
    """
    Called once per session before London open.
    Returns whether to trade, in which direction, and at what size.
    """
    cot_row = get_latest_cot(pair)
    if cot_row is None:
        return {"trade": False, "reason": "no_cot_data"}

    regime = classify_regime(cot_row)

    # Hard kill conditions
    if not regime.is_active():
        return {"trade": False, "reason": regime.state.value}

    # Directional gate — only trade breakouts that match COT bias
    adjusted_size = round(base_lot_size * regime.position_multiplier, 2)

    return {
        "trade":           True,
        "direction_bias":  regime.direction,    # +1 long only, -1 short only, 0 both
        "lot_size":        adjusted_size,
        "regime_state":    regime.state.value,
        "signal_strength": cot_row.get("signal_strength", 0.0),
        "context":         regime.signal_context,

        # Tighten stops in squeeze/neutral regimes
        "stop_multiplier": 0.7 if regime.state.value in ("SQUEEZE_RISK", "NEUTRAL") else 1.0,
    }