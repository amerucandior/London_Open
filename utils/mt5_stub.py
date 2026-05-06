"""
Minimal MetaTrader5 fallback for non-Windows/dev environments.

This keeps imports working on platforms where the official MetaTrader5 wheel
is unavailable (e.g. Linux/Colab). Trading/data methods intentionally return
empty values so callers fail safely when they truly require a live terminal.
"""
from __future__ import annotations

from typing import Any


class _MT5Stub:
    # Common timeframe constants used across the project.
    TIMEFRAME_M5 = 5
    TIMEFRAME_H1 = 60

    # Common trade constants used by broker/risk code.
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009

    def initialize(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def shutdown(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def last_error(self) -> tuple[int, str]:
        return (
            1,
            "MetaTrader5 package unavailable on this platform; using stub.",
        )

    def copy_rates_range(self, *args: Any, **kwargs: Any):
        return None

    def copy_rates_from_pos(self, *args: Any, **kwargs: Any):
        return None

    def symbol_info_tick(self, *args: Any, **kwargs: Any):
        return None

    def symbol_info(self, *args: Any, **kwargs: Any):
        return None

    def positions_get(self, *args: Any, **kwargs: Any):
        return ()

    def order_send(self, *args: Any, **kwargs: Any):
        return None

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(
            f"MetaTrader5 stub has no attribute {name!r}. "
            "Add it to utils/mt5_stub.py if needed for tests."
        )


mt5 = _MT5Stub()

