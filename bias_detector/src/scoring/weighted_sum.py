# src/scoring/weighted_sum.py
import yaml
import numpy as np
from pathlib import Path
from functools import lru_cache
from typing import Dict

@lru_cache(maxsize=1)
def _load_cfg():
    return yaml.safe_load(
        (Path(__file__).parent.parent / "config" / "scoring.yaml").read_text()
    )

def score_pair(pair: str, horizon: str, features: Dict[str, float]) -> float:
    """
    Pure function. Returns score ∈ [-1, +1].
    Missing features treated as 0 — never raises on sparse input.
    """
    cfg = _load_cfg()
    coeffs = cfg["weighted_sum"][pair.lower()][horizon]
    total_weight = sum(coeffs.values())
    score = sum(
        (coeffs.get(k, 0.0) / total_weight) * features.get(k, 0.0)
        for k in coeffs
    )
    return float(np.clip(score, -1.0, 1.0))