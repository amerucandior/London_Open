"""
Strategy entrypoints — **two different edges**:

- **London Open** = ``london_open_plan`` … Asian range **breakout** (momentum / liquidity).
- **London Close** = ``london_close_plan`` / ``lc_plan`` … **Mean reversion** into session end.

COT + calendar are shared **context**; they do not turn an open breakout into a close MR trade.
"""

from .lc_strategy import lc_plan
from .london_close import london_close_plan
from .london_open import london_open_plan
from .overextension import Overextension

__all__ = [
    "london_open_plan",
    "london_close_plan",
    "lc_plan",
    "Overextension",
]
