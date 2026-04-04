"""
Defines the central bank dealers range which is used to project the day's high or low(LoD, HoD)
Optimal dealers range should be less than 40 pips ---- 20 pips to 30 pips
NB: Ranges larger than 30 pips are unfruitful for projections
Ideal time for the cbdr is 2pm to 8pm New York time (NY)
You must know the direction bias
Since in a bullish condition you will look forward for the LoD to be formed during London and vice versa

Optimal LoD or HoD occurs at around 2STDs 
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import pytz
from datetime import time

def cbdr_range(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str = 'M5',
    use_wicks: bool = False,
    ideal_min: float = 20,
    ideal_max: float = 30,
    max_allowed: float = 40,
) -> dict:
    """
    Compute cbdr for a single day

    Parameters
    -----------
    df: pd.DataFrame
        Must contain columns ['time', 'open', 'high' , 'low', 'close']
        time is a *UTC* datetime64[ns]
    """

    # Prepare Data
    if not np.issubdtype(df["time"].dtype, np.datetime64):
        df["time"] = pd.to_datetime(df["time"], utc=True)
    else:
        df["time"] = df ["time"].dt.tz_convert("UTC")

    df = df.sort_values("time").reset_index(drop=True)

    # ------- Convert to New York Time --------
    ny_tz = pytz.timezone("America/New_York")
    df["ny_time"] = df["time"].dt.tz_convert(ny_tz)

    # ----- Isolate the 14:00 - 20:00 NY window -----
    start_ny = time(14, 0) # 2pm
    end_ny = time(20, 0) # 8pm

    mask = (df["ny_time"].dt.time >= start_ny) & (df["ny_time"].dt.time < end_ny)

    window = df.loc[mask].copy()

    if window.empty:
        raise ValueError("No bars found inside 14-20 NY window for the day.")

    # ---- Determine high or low ** using bodies or wicks ----
    if use_wicks:
        # full candle extremes
        window["body_high"] = window["high"]
        window["body_low"] = window["low"]

    else:
        # Body only: max(open, close) and min(open, close)
        window["body_high"] = window["open", "close"].max(axis=1)
        window["body_low"] = window["open", "close"].min(axis=1)

    cbdr_high = window["body_high"].max()
    cbdr_low = window["body_low"].min()

    # Convert range to pips
    pip_size = mt5.symbol_info(symbol).point
    cbdr_range_pips = (cbdr_high - cbdr_low) / pip_size

    within_ideal_range = ideal_min <= cbdr_range_pips <= ideal_max
    within_max = cbdr_range_pips <= max_allowed

    # Standard deviation levels(1STD = cbdr itself)
    std1 = cbdr_range_pips
    std2 = 2 * std1
    std3 = 3 * std1
    std4 = 4 * std1

    # ----- Projected High -----
    proj_high = cbdr_high + 3 * std1 * pip_size
    proj_low = cbdr_low - 3 * std1 * pip_size

    # -------- BIAS logic ---------

    # To Do 
    bias = None
    # bias logic

    # assemble result
    result = {
        "date": window["ny_time"].dt.date.iloc[0].isoformat(),
        "cbdr_high": cbdr_high,
        "cbdr_low": cbdr_low,
        "cbdr_range_pips": round(cbdr_range_pips, 2),
        "within_ideal": within_ideal_range,
        "within_max": within_max,
        "std1_pips": round(std1, 2),
        "std2_pips": round(std2, 2),
        "std3_pips": round(std3, 2),
        "std4_pips": round(std4, 2),
        "proj_high": proj_high,
        "proj_low": proj_low,
        "bias": bias,
        "use_wicks": use_wicks,
        # Raw values for debugging
        "raw_body_high": cbdr_high,
        "raw_body_low": cbdr_low,
    }
    return result
