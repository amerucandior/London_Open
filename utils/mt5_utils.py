# asian_range.py
# ----------------------------------------------------------------------
# Pure python code that *does not* touch MT5 at import time.
# ----------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from typing import Dict

from .mt5_utils import MT5Connection, mt5


def _calc_asian_stats(rates) -> tuple[float, float, float]:
    """Given a ``numpy.ndarray`` of MT5 rates, compute high/low/mid."""
    # The shape of ``rates`` is (n, 6) – columns: time, open, high, low, close, volume
    high = float(rates["high"].max())
    low = float(rates["low"].min())
    mid = (high + low) / 2.0
    return high, low, mid


def compute_asian_range(
    symbol: str = "GBPUSD",
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    timeframe: int = mt5.TIMEFRAME_H1,
) -> Dict[str, float]:
    """
    Fetch hourly candles for *symbol* between *start_dt* and *end_dt*,
    then return the Asian session high / low / mid.

    The function **does not** create any global side-effects.
    """
    if start_dt is None or end_dt is None:
        raise ValueError("Both start_dt and end_dt must be supplied")

    # ------------------------------------------------------------------
    # The *only* place where we talk to MT5 is inside the context.
    # ------------------------------------------------------------------
    with MT5Connection() as conn:
        rates = conn.copy_rates_range(symbol, timeframe, start_dt, end_dt)

    if rates is None or len(rates) == 0:
        raise RuntimeError(
            f"No rate data returned for {symbol} between {start_dt} and {end_dt}"
        )

    high, low, mid = _calc_asian_stats(rates)
    return {"high": high, "low": low, "mid": mid}
