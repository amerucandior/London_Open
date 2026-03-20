"""
    Liquidity Sweep is whereby price moves through a thin liquidity zone(round numborderblockser, swing high, swing low, )
    and meets a large market order, rpoducing a sharp rapid move
    Market should sweep the liquidity and reverse sharply. Once the big order is filled.

    1. liquidity zones
    2. Fast price detection
    3. Volume spike detection
"""

import time
from collections import deque
import datetime as datetime

import pandas as pd
import numpy as np
import MetaTrader5 as mt5

def fetch_mt5(symbol: str, timeframe, n_bars: int) -> pd.DataFrame:
    """
    Pull the last `n_bars` from MT5 and return a tidy DataFrame.
    The DataFrame has a UTC datetime index and the columns:
        price  - the close price (used for all calculations)
        size   - tick_volume (number of trades in the bar)
        open, high, low - kept for optional later use
    """
    if not mt5.initialize():
        raise RuntimeError(f"mt5 initialization failed: {mt5.last_error()}")

    raw = mt5.copy_rates_from_pos(symbol, timeframe, 0, n_bars)
    if raw is None:
        raise RuntimeError(f"mt5.copy_rates_from_pos returned None: {mt5.last_error()}")

    df = pd.DataFrame(raw)

    # Rename columns to names canonical to the project
    df.rename(columns={"close": "price", "tick_volume": "size"}, inplace=True)

    # Convert UNIX based timestamp into proper datetime index
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)

    # Keep columns needed
    df = df[["price", "size", "open", "high", "low", "close"]]

    return df

# define Liquidity zones
def round_zones(df: pd.DataFrame, step: float = 0.0010) -> np.ndarray:
    """
    Returns an array of “round‑number” price levels.
    Example for GBPUSD: step = 0.0010  → 10 pips.
    """
    low = np.floor(df['price'].min() / step) * step
    high = np.ceil(df['price'].max() / step) * step + step
    return np.arange(low, high, step)   

def volume_clusters(
    df: pd.DataFrame,
    lookback: int = 200,
    col_factor: float = 3.0,
    ) -> list[tuple[float, float]]:
    """
    Detect high-volume “clusters”.
    For each rolling window of `lookback` bars we compute the total volume.
    If that total exceeds `col_factor * median_volume` we treat the window
    as a cluster and store its price range (low, high).
    """
    # Rolling sum of volume (use bar level data)
    vol_roll = df['size'].rolling(window=lookback, min_periods=1).sum()

    # Median volume for the whole series 
    median_vol = vol_roll.median()

    # Identify indexes where rolled volume is high
    high_vol_index = vol_roll[vol_roll > col_factor * median_vol].index

    # Group consecutive indexes to clusters
    clusters = []
    if not high_vol_index.empty:
        cur_start = cur_end = high_vol_index[0]

        for t in high_vol_index[1:]:
            # If next high_volume bar is within 1 second it belongs to the same cluster.
            # Tolerance can be increaseed for higher timeframes
            if (t - cur_end).total_seconds() <= 1:
                cur_end = t
            else:
                low = df.loc[cur_start:cur_end, 'price'].min()
                high = df.loc[cur_start:cur_end, 'price'].max()
                clusters.append((low, high))
                cur_start = cur_end = t

        # Final cluster
        low = df.loc[cur_start:cur_end, 'price'].min()
        high = df.loc[cur_start:cur_end, 'price'].max()
        clusters.append((low, high))
    return clusters


def liquidity_zones(
    df: pd.DataFrame, 
    step: float=0.0010, 
    tolerance: float=0.0002, 
    min_touches: int=3,
    ) -> pd.DataFrame:
    """ 
    Combine round-number zones and volume clusters.
    A “zone” is considered a *liquidity zone* if price has touched it at
    least `min_touches` times within `tolerance` and the average wick size
    (rejection strength) is non-zero.
    Returns a DataFrame sorted by “strength”.
    """

    # 1. round-number levels
    round_levels = round_zones(df, step=step)

    # 2. High volume clusters (as (low, high) tuples)
    vol_clusters = volume_clusters(df)

    # 3. build the final list
    zones = []

    # Round number zones
    for level in round_levels:
        # Bars whose highs or lows entered the tolerance band around the level
        touches_low = df[np.abs(df['low'] - level) <= tolerance]
        touches_high = df[np.abs(df['high'] - level) <= tolerance]
        all_touches = pd.concat([touches_low, touches_high]).drop_duplicates()

        if len(all_touches) < min_touches:
            continue

        # wick magnitude at zone - proxy for rejection stregth
        wick_below = (all_touches['close'] - all_touches['low']).clip(lower=0)
        wick_above = (all_touches['close'] - all_touches['high']).clip(lower=0)
        avg_wick = pd.concat([wick_above, wick_below]).mean()


        zones.append(
            {
            'zone': round(level, 5),
            'touches': len(all_touches),
            'avg_wick': round(avg_wick, 5),
            'first_touch': all_touches.index.min(),
            'last_touch': all_touches.index.max(),
            'strength': len(all_touches) * avg_wick,
            'low': level - tolerance,
            'high': level + tolerance
            }
        )

    # Volume clusters
    for low, high in vol_clusters:
        # Bars that lie inside cluster range
        inside = df[(df["price"] >= low) & (df["price"] <= high)]
        if len(inside) < min_touches:
            continue

        # For a cluster we use simple ranges as "strength"
        strength = (high - low) * len(inside)

        zones.append(
            {
                "zone": round((high + low) / 2, 5),
                "type": "cluster",
                "touches": len(inside),
                "avg_wick": np.nan,
                "first_touch": inside.index.min(),
                "last_touch": inside.index.max(),
                "strength": strength,
                "low": low,
                "high": high,
            }
        )
    return (
        pd.DataFrame(zones)
        .sort_values('strength', ascending=False)
        .reset_index(drop=True)
    )

# Fast Price Spike detection
def detect_price_spikes(
    df: pd.DataFrame,
    max_seconds: int=5,
    min_pips: float=15.0
    ) -> pd.DataFrame:
    """
    Sliding-window algorithm that finds any price movement of at least
    `min_pips` within `max_seconds`.  Returns a DataFrame with the
    start/end timestamps, the price change (in pips) and the duration.
    """
    # Compute forward price change over `max_seconds`
    spikes = []
    window = deque()

    for ts, row in df.iterrows():
        # Add current window to the window
        window.append((ts, row["price"]))

        # Discard rows that are older than 'max_seconds'
        while window and (ts - window[0][0]).total_seconds()> max_seconds:
            window.popleft

    # Compare newest price with the oldest price in the window 
    start_ts, start_price = window[0]
    price_change = (row["price"] - start_price) * 10_000 # pips 

    if abs(price_change) >= min_pips:
        spikes.append(
            {
            "start_ts": start_ts,
            "end_ts": ts,
            "start_price": start_price,
            "end_price": row["price"],
            "pips": round(price_change, 1),
            "duration_sec": (ts - start_ts).total_seconds()
            }
        )
        # Clear window to ensure move is not counted twice
        window.clear

    return pd.DataFrame(spikes)


# Attach volume information to each spike
def add_volume_to_spikes(
    df: pd.DataFrame,
    spikes: pd.DataFrame,
    vol_factor: int=3
    ) -> pd.DataFrame:
    """
    For every spike we compute the total executed volume (`size`) that took
    place between its start and end timestamps.
    If that total exceeds `vol_factor` × the **global median** volume,
    the spike is kept; otherwise it is dropped.
    """

    if spikes.empty:
        return spikes

    # cumulative volume series – O(1) interval queries
    df['col_vol'] = df['size'].cumsum()

    # Overall median volume
    median_vol = df['size'].rolling(window=200, min_periods=1).median() #rolling median volume
    overall_median = median_vol.median()

    # Compute volume per spike
    vol = []
    for _, sp in spikes.iterows():
        # locate rows between start_ts and end_ts (binary search via index)
        start_idx = df.index.get_loc(sp['start_ts'])
        end_idx = df.index.get_loc(sp['end_ts'])
        # Cumulative volume diff
        vol_spike = df['cum_vol'].iloc[end_idx] - (df['cum_vol'].iloc[start_idx-1] if start_idx>0 else 0)
        vol.append(vol_spike)

    spikes = spikes.copy()
    spikes['vol'] = vol
    spikes['vol_factor'] = spikes['vol'] / overall_median
    # keep spikes with noticeable volume surge
    spikes = spikes[spikes['vol_factor'] >= vol_factor].reset_index(drop=True)
    return spikes


# -------------  Final function that returns sweeps --------------------
def detect_liquidity_sweeps(
    df: pd.DataFrame,
    step: float = 0.0010,
    tolerance: float = 0.0002,
    min_touches: int=3,
    max_seconds: int=5,
    min_pips: float=15,
    vol_factor: int=3,
) -> pd.DataFrame:
    """
    High-evel wrapper:
      1️⃣ Build liquidity zones.
      2️⃣ Find price spikes that *start* inside any zone.
      3️⃣ Keep only those spikes that also show a volume surge.
    Returns a DataFrame with the final “sweep” signals.
    """
    zones = liquidity_zones(df, step, tolerance, min_touches)

    if zones.empty:
        print("No liquidty zones found - try adjusting parameters")
        return pd.DataFrame()

    # Fast price spikes
    raw_spikes = detect_price_spikes(df, max_seconds, min_pips)
    
    if raw_spikes.empty:
        print("⚠️ No fast price spikes detected")
        return pd.DataFrame()

    # Keep spikes that begin inside a zone
    def zone_of_ts(ts):
        """Return the zone (dict) that contains the timestamp `ts`."""
        price = df.at[ts, "price"]
        # Scan zones 
        for _, z in zones.iterrows():
            if z["low"] <= price <= z["high"]:
                return z
        return None

    # Attach zone information to each spike
    spikes_with_zone = []
    for _, sp in raw_spikes.iterrows():
        zone = zone_of_ts(sp["start_ts"])
        if zone is not None:
            sp_dict = sp.to_dict()
            sp_dict["zone_type"] = zone["type"]
            sp_dict["zone_center"] = zone["zone"]
            spikes_with_zone.append(sp_dict)

    if not spikes_with_zone:
        print("⚠️ No spikes originated from a defined liquidity zone")
        return pd.DataFrame()

    spike_df = pd.DataFrame(spikes_with_zone)

    # Volume confirmation
    sweep_df = add_volume_to_spikes(df, spike_df, vol_factor)

    return sweep_df


# --------- Live tracking skeleton
def live_loop(
    symbol: str = "GBPUSD",
    timeframe = mt5.TIMEFRAME_M5,
    n_bars = 500,
    sleep_seconds: int = 30,
):

    """
    Very small example of a “run-forever” program.
    Every `sleep_seconds` we download the most recent `n_bars`,
    run the sweep detector and (for demo) just print the results.
    In a production bot you would replace the `print` with order logic,
    a webhook, a database insert, etc.
    """

    try:
        while True:
            # Fetch fresh data
            df = fetch_mt5(symbol, timeframe, n_bars)

            # Run the detector
            sweeps = detect_liquidity_sweeps(df)

            # Display
            if not sweeps.empty:
                print("\n=== Liquidity sweeps detected===")
                print(sweeps[["start_ts", "end_ts", "pips", "zone_type", "zone_center", "vol"]])
            else:
                print(".", end="", flush=True)

            # Pause
            time.sleep(sleep_seconds)

    finally:
        mt5.shutdown()

# ----------------------------------------------------------------------
# 8️⃣  Main guard – lets you run the script directly
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # Example: run a quick back‑test on the last 2000 M5 bars
    df = fetch_mt5("EURUSD", mt5.TIMEFRAME_M5, n_bars=2000)
    sweeps = detect_liquidity_sweeps(df)
    if not sweeps.empty:
        print("\n=== Historical sweeps (last 2000 bars) ===")
        print(sweeps.head())
    else:
        print("\nNo sweeps found in the historical window.")

    # Uncomment the next line if you want the live infinite loop:
    # live_loop(symbol="EURUSD", timeframe=mt5.TIMEFRAME_M5, n_bars=500, sleep_seconds=30)


