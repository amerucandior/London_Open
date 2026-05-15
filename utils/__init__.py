"""MT5 and shared helpers for root scripts and strategies."""
from __future__ import annotations

try:
    import MetaTrader5 as _real_mt5  # noqa: F401
    HAS_MT5: bool = True
except Exception:
    HAS_MT5 = False

__all__ = ["HAS_MT5"]
