"""
Asian session high / low / mid (MT5) for the **session_clock.asian** window in London local time.
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
from zoneinfo import ZoneInfo

from utils.mt5_utils import compute_asian_range


def get_today_asian_range(symbol: str, *, on: datetime | None = None) -> dict[str, float]:
    """
    Asian range for the **calendar day** of ``on`` in ``session_clock.tz_name`` (default: now).

    Bounds follow ``config.session_clock.asian`` (default 00:00–07:00 local), converted to UTC
    for ``copy_rates_range`` (assumes broker/server aligns with UTC; adjust if not).
    """
    clock = config.config.get("session_clock", {})
    tz = ZoneInfo(str(clock.get("tz_name", "Europe/London")))
    on = on or datetime.now(tz)
    if on.tzinfo is None:
        on = on.replace(tzinfo=tz)
    else:
        on = on.astimezone(tz)

    a = clock.get(
        "asian",
        {"start_hour": 0, "start_minute": 0, "end_hour": 7, "end_minute": 0},
    )
    start_local = on.replace(
        hour=int(a["start_hour"]),
        minute=int(a["start_minute"]),
        second=0,
        microsecond=0,
    )
    end_local = on.replace(
        hour=int(a["end_hour"]),
        minute=int(a["end_minute"]),
        second=0,
        microsecond=0,
    )
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    return compute_asian_range(symbol=symbol, start_dt=start_utc, end_dt=end_utc)
