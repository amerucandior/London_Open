# src/scoring/weighted_sum.py
import yaml
import numpy as np
from pathlib import Path
from functools import lru_cache
from typing import Dict

@lru_cache(maxsize=1)
def _load_cfg():
    """Load and cache the scoring configuration from a YAML file"""
    cfg_path = Path(__file__).parent.parent / "config" / "scoring.yaml"
    return yaml.safe_load(cfg_path.read_text())

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

THR = yaml.safe_load(Path(__file__).with_name("../config/thresholds.yaml").read_text())
# Example thresholds.yaml:
# base: 0.30
# vol_adjust_factor: 0.5   # multiply by (σ/σ_ref)

def band(vol_factor: float) -> float:
    """Volatility‑adjusted neutral band."""
    base = THR["base"]
    return base * (vol_factor * THR["vol_adjust_factor"] + 1.0)

def bias_from_score(score: float, vol_factor: float) -> str:
    b = band(vol_factor)
    if score >= b:
        return "Long"
    if score <= -b:
        return "Short"
    return "Neutral"