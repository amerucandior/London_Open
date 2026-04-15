import pandas as pd

def calculate_position_size(base_size: float, cot_row: pd.Series) -> float:
    """
    Adjust base position size based on COT momentum signals.
    """
    # Core signal: CCI direction
    direction = 1 if cot_row["cci_z"] > 0 else -1
    
    # Scale based on momentum confirmation
    scale = cot_row["position_scale"]  # 0.5 to 2.0
    
    # Additional sizing logic
    if cot_row["regime_confirmed"]:
        # Strong alignment - full size
        return base_size * scale * direction
    elif cot_row["conflict_opportunity"]:
        # Fade the asset manager (typically wrong at extremes)
        # If dealers buying, asset managers selling -> follow dealers
        if cot_row["dealer_flow_3w"] > 0:
            return base_size * 0.6 * 1   # Smaller long (contrarian to specs)
        else:
            return base_size * 0.6 * -1  # Smaller short
    else:
        # No clear signal - standard size
        return base_size * direction
