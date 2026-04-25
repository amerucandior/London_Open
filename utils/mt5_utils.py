# ----------------------------------------------------------------------
# MT5 helpers — connection only inside ``MT5Connection`` context.
# ----------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

import MetaTrader5 as mt5


class MT5Connection:
    """Initialize MT5 on enter and shut down on exit."""

    def __enter__(self) -> "MT5Connection":
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        return self

    def __exit__(self, *args: Any) -> None:
        mt5.shutdown()

    def copy_rates_range(
        self,
        symbol: str,
        timeframe: int,
        date_from: datetime,
        date_to: datetime,
    ):
        return mt5.copy_rates_range(symbol, timeframe, date_from, date_to)


def _calc_asian_stats(rates) -> tuple[float, float, float]:
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
    Hourly candles between ``start_dt`` and ``end_dt`` (UTC) → Asian high / low / mid.
    """
    if start_dt is None or end_dt is None:
        raise ValueError("Both start_dt and end_dt must be supplied")

    with MT5Connection() as conn:
        rates = conn.copy_rates_range(symbol, timeframe, start_dt, end_dt)

    if rates is None or len(rates) == 0:
        raise RuntimeError(
            f"No rate data returned for {symbol} between {start_dt} and {end_dt}"
        )

    high, low, mid = _calc_asian_stats(rates)
    return {"high": high, "low": low, "mid": mid}
