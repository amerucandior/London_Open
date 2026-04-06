# src/scoring/weighted_sum.py
import yaml
import numpy as np
from pathlib import Path
from functools import lru_cache
from typing import Dict
from ..ingestion.broker import fetch_ohlcv

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

THR_PATH = Path(__file__).parent.parent / "config" / "thresholds.yaml"
THR = yaml.safe_load(THR_PATH.read_text())
# Example thresholds.yaml:
# base: 0.30
# vol_adjust_factor: 0.5   # multiply by (σ/σ_ref)

def band(vol_factor: float) -> float:
    """Volatility-adjusted neutral band."""
    base = THR["base"]
    return base * (vol_factor * THR["vol_adjust_factor"] + 1.0)

def bias_from_score(score: float, vol_factor: float) -> str:
    b = band(vol_factor)
    if score >= b:
        return "Long"
    if score <= -b:
        return "Short"
    return "Neutral"

def get_daily_bias(pair):
    # Fetch OHLCV data
    ticker = pair.upper() + '=X'
    df = fetch_ohlcv(ticker)
    
    # Compute features
    features = {}
    
    # F1_ema_slope: slope of 20-period EMA
    ema = df['close'].ewm(span=20).mean()
    if len(ema) >= 2:
        slope = (ema.iloc[-1] - ema.iloc[-2]) / ema.iloc[-2]
    else:
        slope = 0.0
    features['F1_ema_slope'] = slope
    
    # F2_rsi_div: RSI divergence (placeholder, set to 0)
    features['F2_rsi_div'] = 0.0
    
    # F5_event_risk: Event risk (placeholder, set to 0)
    features['F5_event_risk'] = 0.0
    
    # F6_vix_regime: VIX regime (placeholder, set to 0)
    features['F6_vix_regime'] = 0.0
    
    # F7_sentiment: Sentiment (placeholder, set to 0)
    features['F7_sentiment'] = 0.0
    
    # Compute volatility factor (simple std of returns)
    returns = df['close'].pct_change().dropna()
    if len(returns) > 0:
        vol_factor = returns.std()
    else:
        vol_factor = 1.0
    
    # Score the pair
    score = score_pair(pair, 'daily', features)
    
    # Get bias
    return bias_from_score(score, vol_factor)
    