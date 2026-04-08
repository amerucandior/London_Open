# src/features/compute_features.py
"""
Feature engineering for the FX Bias Detector.

All features are normalised to [-1, +1] or [0, 1] so the weighted-sum
scorer remains interpretable regardless of raw magnitude differences.

Feature catalogue:
  F1_ema_slope   — EMA(20) slope, normalised by ATR
  F2_rsi_div     — RSI(14) deviation from neutral (50), mapped to [-1, +1]
  F3_cot_z       — COT non-commercial net z-score (clipped ±4)
  F4_yield_delta — placeholder (set 0 until FRED integrated)
  F5_event_risk  — high-impact event density in next 48h, mapped to [-1, 0]
  F6_vix_regime  — VIX-based risk-off flag mapped to {-1, 0, +1}
  F7_sentiment   — retail contrarian signal from sentiment module
  F8_ppp_dev     — placeholder (monthly; 0 until OECD integrated)
  F9_atr_mom     — ATR-normalised 5-day momentum
  F10_carry      — placeholder (0 until yield data integrated)
"""
from __future__ import annotations

import logging
from typing import Dict, Any

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
EMA_PERIOD       = 20
RSI_PERIOD       = 14
ATR_PERIOD       = 14
MOM_PERIOD       = 5          # days for F9_atr_mom
VIX_HIGH         = 25.0       # above → risk-off (-1)
VIX_LOW          = 15.0       # below → risk-on (+1)
EVENT_RISK_DECAY = 0.5        # per-event score contribution (capped at -1)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(close: pd.Series, period: int = RSI_PERIOD) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty else 50.0


def _atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


# ── Feature calculators ────────────────────────────────────────────────────────

def _f1_ema_slope(ohlcv: pd.DataFrame) -> tuple[float, float]:
    """
    EMA(20) slope normalised by ATR.
    Returns (f1_value, atr) where f1 ∈ [-1, +1].
    """
    close = ohlcv["close"].astype(float)
    ema   = _ema(close, EMA_PERIOD)
    if len(ema) < 2:
        return 0.0, 1e-6
    raw_slope = float(ema.iloc[-1] - ema.iloc[-2])
    atr       = _atr(ohlcv) or 1e-6
    # Clip at ±3 ATR — anything beyond is extreme and still maps to ±1
    normalised = float(np.clip(raw_slope / atr, -3.0, 3.0) / 3.0)
    return normalised, atr


def _f2_rsi_div(ohlcv: pd.DataFrame) -> float:
    """
    RSI deviation from 50, mapped to [-1, +1].
    RSI=80 → +0.6; RSI=20 → -0.6; RSI=50 → 0.0
    """
    rsi = _rsi(ohlcv["close"].astype(float))
    return float(np.clip((rsi - 50.0) / 50.0, -1.0, 1.0))


def _f3_cot_z(cot: pd.DataFrame) -> tuple[float, bool]:
    """
    Latest COT z-score (F3). Returns (z_score, stale_flag).
    """
    if cot.empty or "cot_z_score" not in cot.columns:
        return 0.0, True
    latest = cot.iloc[-1]
    z      = float(latest.get("cot_z_score", 0.0))
    stale  = int(latest.get("quality_flag", 0)) == 2
    return float(np.clip(z / 4.0, -1.0, 1.0)), stale  # normalise ±4σ → ±1


def _f5_event_risk(cal: pd.DataFrame, pair: str) -> float:
    """
    Event risk score ∈ [-1, 0].
    Negative because high-impact events reduce directional confidence.
    Maps each high-impact event in next 48h to a haircut.
    """
    if cal.empty:
        return 0.0
    currency = pair[:3].upper()  # "EUR" from "EURUSD"
    high_impact = cal[
        (cal["currency"] == currency) &
        (cal["is_high_priority"])
    ]
    n_events = len(high_impact)
    # Each event contributes -EVENT_RISK_DECAY, capped at -1
    return float(max(-1.0, -n_events * EVENT_RISK_DECAY))


def _f6_vix_regime(ohlcv_vix: pd.DataFrame | None) -> float:
    """
    VIX regime flag: high → risk-off (-1), low → risk-on (+1), mid → 0.
    Returns 0 if VIX data unavailable.
    """
    if ohlcv_vix is None or ohlcv_vix.empty:
        return 0.0
    latest_vix = float(ohlcv_vix["close"].iloc[-1])
    if latest_vix >= VIX_HIGH:
        return -1.0
    if latest_vix <= VIX_LOW:
        return 1.0
    # Linear interpolation in (15, 25) → (1, -1)
    return float(1.0 - 2.0 * (latest_vix - VIX_LOW) / (VIX_HIGH - VIX_LOW))


def _f7_sentiment(sentiment: pd.DataFrame) -> float:
    """
    Retail contrarian signal from the sentiment module.
    Already ∈ [-1, +1] from sentiment.py.
    """
    if sentiment.empty or "sentiment_signal" not in sentiment.columns:
        return 0.0
    return float(sentiment["sentiment_signal"].iloc[0])


def _f9_atr_mom(ohlcv: pd.DataFrame, atr: float) -> float:
    """
    ATR-normalised 5-day momentum.
    (close[-1] - close[-MOM_PERIOD]) / (ATR * MOM_PERIOD)
    Clipped to [-1, +1].
    """
    close = ohlcv["close"].astype(float)
    if len(close) < MOM_PERIOD + 1 or atr < 1e-10:
        return 0.0
    mom = float(close.iloc[-1] - close.iloc[-MOM_PERIOD])
    return float(np.clip(mom / (atr * MOM_PERIOD), -1.0, 1.0))


# ── Public API ─────────────────────────────────────────────────────────────────

def run(
    pair:      str,
    ohlcv:     pd.DataFrame,
    cal:       pd.DataFrame,
    cot:       pd.DataFrame,
    sentiment: pd.DataFrame,
    ohlcv_vix: pd.DataFrame | None = None,
) -> Dict[str, Any]:
    """
    Compute the full feature vector for one pair.

    Returns
    -------
    {
        "values":       Dict[str, float],   # feature name → value ∈ [-1, +1]
        "vol_factor":   float,              # raw daily σ (for band() in decision.py)
        "drivers":      List[str],          # top contributing features (for output)
        "quality_flags": Dict[str, bool],   # data quality issues
    }
    """
    quality_flags: Dict[str, bool] = {}

    # ── F1, ATR ───────────────────────────────────────────────────────────────
    f1, atr = _f1_ema_slope(ohlcv)

    # ── F2 ───────────────────────────────────────────────────────────────────
    f2 = _f2_rsi_div(ohlcv)

    # ── F3 ───────────────────────────────────────────────────────────────────
    f3, cot_stale = _f3_cot_z(cot)
    if cot_stale:
        quality_flags["stale_cot"] = True

    # ── F5 ───────────────────────────────────────────────────────────────────
    f5 = _f5_event_risk(cal, pair)
    if cal.empty:
        quality_flags["missing_calendar"] = True

    # ── F6 ───────────────────────────────────────────────────────────────────
    f6 = _f6_vix_regime(ohlcv_vix)
    if ohlcv_vix is None:
        quality_flags["missing_vix"] = True

    # ── F7 ───────────────────────────────────────────────────────────────────
    f7 = _f7_sentiment(sentiment)
    if sentiment.empty:
        quality_flags["missing_sentiment"] = True

    # ── F9 ───────────────────────────────────────────────────────────────────
    f9 = _f9_atr_mom(ohlcv, atr)

    # ── Placeholders (require FRED / OECD integration) ────────────────────────
    f4  = 0.0   # yield_delta — add FRED integration
    f8  = 0.0   # ppp_dev     — add OECD integration
    f10 = 0.0   # carry       — add yield data

    if f4 == 0.0:
        quality_flags["missing_yield_data"] = True
    quality_flags["missing_features"] = any([
        quality_flags.get("missing_vix"),
        quality_flags.get("missing_sentiment"),
        quality_flags.get("missing_yield_data"),
    ])

    values = {
        "F1_ema_slope":  f1,
        "F2_rsi_div":    f2,
        "F3_cot_z":      f3,
        "F4_yield_delta": f4,
        "F5_event_risk": f5,
        "F6_vix_regime": f6,
        "F7_sentiment":  f7,
        "F8_ppp_dev":    f8,
        "F9_atr_mom":    f9,
        "F10_carry":     f10,
    }

    # ── Daily vol factor (absolute σ for decision.py) ─────────────────────────
    returns    = ohlcv["close"].astype(float).pct_change().dropna()
    vol_factor = float(returns.std()) if len(returns) > 1 else 0.006

    # ── Drivers (top 3 features by absolute contribution) ────────────────────
    drivers = sorted(values.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
    driver_strs = [f"{k}={v:+.3f}" for k, v in drivers]

    log.info(f"[{pair}] features computed | vol={vol_factor:.5f} | "
             f"drivers={driver_strs} | flags={quality_flags}")

    return {
        "values":        values,
        "vol_factor":    vol_factor,
        "drivers":       driver_strs,
        "quality_flags": quality_flags,
    }