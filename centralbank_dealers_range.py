"""
Central bank dealers range (CBDR) — NY 14:00–20:00 window, body or wicks.

Used for London Open **LoD / HoD** projections: extend the CBDR band by 1× or 2×
the band width (treated as one “sigma” width) above/below the CBDR high/low.

Bias (from COT / regime): bullish → focus **LoD** formation during London (levels below
the band); bearish → **HoD** (levels above). See ``lod_hod_projection``.
"""

from __future__ import annotations

from typing import Any, Optional

try:
    import MetaTrader5 as mt5
except Exception:  # pragma: no cover - fallback for Linux/Colab dev
    from utils.mt5_stub import mt5
import pandas as pd
from datetime import time
from zoneinfo import ZoneInfo


def cbdr_range(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str = "M5",
    use_wicks: bool = False,
    ideal_min: float = 20,
    ideal_max: float = 30,
    max_allowed: float = 40,
    pip_size: Optional[float] = None,
) -> dict[str, Any]:
    """
    CBDR for one day: NY 14:00–20:00 on the dates already contained in ``df``.

    ``df`` must have columns ``time, open, high, low, close`` with ``time`` timezone-aware
    (any tz); it is converted to UTC then to America/New_York for the window filter.
    """
    if not {"time", "open", "high", "low", "close"}.issubset(df.columns):
        raise ValueError("df must contain columns: time, open, high, low, close")

    df = df.copy()
    # Use pandas-aware dtype check: np.issubdtype rejects `datetime64[ns, tz]`
    # (a pandas DatetimeTZDtype) on numpy >= 2.0 with the obscure
    # "Cannot interpret 'datetime64[ns, UTC]' as a data type" error.
    if not pd.api.types.is_datetime64_any_dtype(df["time"]):
        df["time"] = pd.to_datetime(df["time"], utc=True)
    elif df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize("UTC")
    else:
        df["time"] = df["time"].dt.tz_convert("UTC")

    df = df.sort_values("time").reset_index(drop=True)

    ny_tz = ZoneInfo("America/New_York")
    df["ny_time"] = df["time"].dt.tz_convert(ny_tz)

    start_ny = time(14, 0)
    end_ny = time(20, 0)
    mask = (df["ny_time"].dt.time >= start_ny) & (df["ny_time"].dt.time < end_ny)
    window = df.loc[mask].copy()

    if window.empty:
        raise ValueError("No bars found inside 14:00–20:00 NY window for the day.")

    if use_wicks:
        window["body_high"] = window["high"]
        window["body_low"] = window["low"]
    else:
        window["body_high"] = window[["open", "close"]].max(axis=1)
        window["body_low"] = window[["open", "close"]].min(axis=1)

    cbdr_high = float(window["body_high"].max())
    cbdr_low = float(window["body_low"].min())
    range_price = cbdr_high - cbdr_low

    # Prefer the pip_size passed by the caller — it was fetched while the MT5
    # connection was still open. Falling back to symbol_info() after the
    # MT5Connection context has exited returns None and breaks the calculation.
    # NOTE: pip_size means "1 pip in price units" (0.0001 for EURUSD), NOT
    # ``info.point`` (0.00001 for 5-digit brokers).
    if pip_size is None:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"MT5 symbol_info is None for {symbol!r}")
        from utils.mt5_utils import pip_size_from_info
        pip_size = pip_size_from_info(info)
    pip_size = float(pip_size) or 0.0001
    cbdr_range_pips = range_price / pip_size

    within_ideal_range = ideal_min <= cbdr_range_pips <= ideal_max
    within_max = cbdr_range_pips <= max_allowed

    std1_pips = float(round(cbdr_range_pips, 2))
    std2_pips = float(round(2 * cbdr_range_pips, 2))

    # Price levels: 1σ / 2σ = 1× or 2× the **CBDR band width** from the band edges.
    hod_1sigma = cbdr_high + 1.0 * range_price
    hod_2sigma = cbdr_high + 2.0 * range_price
    lod_1sigma = cbdr_low - 1.0 * range_price
    lod_2sigma = cbdr_low - 2.0 * range_price

    return {
        "date": window["ny_time"].dt.date.iloc[0].isoformat(),
        "cbdr_high": cbdr_high,
        "cbdr_low": cbdr_low,
        "cbdr_range_price": range_price,
        "cbdr_range_pips": std1_pips,
        "within_ideal": within_ideal_range,
        "within_max": within_max,
        "std1_pips": std1_pips,
        "std2_pips": std2_pips,
        "hod_1sigma_price": hod_1sigma,
        "hod_2sigma_price": hod_2sigma,
        "lod_1sigma_price": lod_1sigma,
        "lod_2sigma_price": lod_2sigma,
        "use_wicks": use_wicks,
    }


def lod_hod_projection(
    cbdr: dict[str, Any],
    direction_bias: int,
) -> dict[str, Any]:
    """
    Map COT/regime ``direction_bias`` (+1 bull, -1 bear, 0 neutral) to LoD vs HoD thesis.

    - **Bullish (+1):** expect **LoD** work during London → primary levels ``lod_*sigma``.
    - **Bearish (-1):** expect **HoD** extension → primary levels ``hod_*sigma``.
    - **Neutral (0):** return both legs for discretion.
    """
    out: dict[str, Any] = {
        "lod_1sigma_price": cbdr.get("lod_1sigma_price"),
        "lod_2sigma_price": cbdr.get("lod_2sigma_price"),
        "hod_1sigma_price": cbdr.get("hod_1sigma_price"),
        "hod_2sigma_price": cbdr.get("hod_2sigma_price"),
        "cbdr_high": cbdr.get("cbdr_high"),
        "cbdr_low": cbdr.get("cbdr_low"),
        "cbdr_range_pips": cbdr.get("cbdr_range_pips"),
        "within_ideal": cbdr.get("within_ideal"),
        "within_max": cbdr.get("within_max"),
    }

    if direction_bias == 1:
        out["primary_thesis"] = "LoD"
        out["note"] = (
            "Bullish bias: session often prints the day's low from London — "
            "watch tests toward lod_1sigma / lod_2sigma (typical mean reversion into lows)."
        )
        out["primary_price_levels"] = {
            "lod_1sigma": cbdr.get("lod_1sigma_price"),
            "lod_2sigma": cbdr.get("lod_2sigma_price"),
        }
    elif direction_bias == -1:
        out["primary_thesis"] = "HoD"
        out["note"] = (
            "Bearish bias: session often extends the day's high — "
            "watch hod_1sigma / hod_2sigma for take-profit or exhaustion."
        )
        out["primary_price_levels"] = {
            "hod_1sigma": cbdr.get("hod_1sigma_price"),
            "hod_2sigma": cbdr.get("hod_2sigma_price"),
        }
    else:
        out["primary_thesis"] = "neutral"
        out["note"] = "No directional CBDR leg preferred — monitor both HoD and LoD extensions."
        out["primary_price_levels"] = {
            "hod_1sigma": cbdr.get("hod_1sigma_price"),
            "hod_2sigma": cbdr.get("hod_2sigma_price"),
            "lod_1sigma": cbdr.get("lod_1sigma_price"),
            "lod_2sigma": cbdr.get("lod_2sigma_price"),
        }

    return out
