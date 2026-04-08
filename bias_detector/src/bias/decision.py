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

def get_daily_bias(pair: str, reference_vol: float = 0.0055) -> str:
    """
    Convenience function: fetch OHLCV, compute minimal feature set, return bias.
    Used for quick CLI checks — not the production path (DAG uses compute_features.run).

    reference_vol: long-run daily σ for the pair (EURUSD ≈ 0.0055, GBPUSD ≈ 0.0070)
    """
    import pandas as pd

    ticker = pair.upper() + "=X" if "=X" not in pair else pair
    df     = fetch_ohlcv(ticker)

    if df.empty:
        return "Neutral"

    close   = df["close"].astype(float)
    returns = close.pct_change().dropna()

    # F1 — EMA slope (normalised by ATR)
    ema = close.ewm(span=20, adjust=False).mean()
    hl  = df["high"].astype(float) - df["low"].astype(float)
    hc  = (df["high"].astype(float) - close.shift()).abs()
    lc  = (df["low"].astype(float)  - close.shift()).abs()
    atr = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(14).mean().iloc[-1]
    slope = float(ema.iloc[-1] - ema.iloc[-2]) / (atr or 1e-6)
    f1 = float(np.clip(slope / 3.0, -1.0, 1.0))

    # F2 — RSI divergence
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rsi   = float(100 - 100 / (1 + gain.iloc[-1] / (loss.iloc[-1] or 1e-6)))
    f2    = float(np.clip((rsi - 50.0) / 50.0, -1.0, 1.0))

    features = {
        "F1_ema_slope":  f1,
        "F2_rsi_div":    f2,
        # TO:DO
        "F5_event_risk": 0.0,  # not available in CLI mode
        "F6_vix_regime": 0.0,
        "F7_sentiment":  0.0,
    }

    score      = score_pair(pair, "daily", features)
    vol_factor = float(returns.std()) / reference_vol

    return bias_from_score(score, vol_factor)