"""Runtime layer that turns plan-generators into a live trading loop."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
import MetaTrader5 as mt5
from utils.mt5_utils import MT5Connection, compute_asian_range


def get_today_asian_range(pair: str, on: datetime) -> Dict[str, float]:
    """
    Return the Asian session high/low/mid for the same calendar day as ``on``.

    ``on`` is expected to be a tz-aware datetime in the session-local timezone
    (Europe/London). The Asian session runs 00:00–07:00 local time; we convert
    both endpoints to UTC before fetching from MT5.
    """
    # Build session-local midnight and 07:00 on the same date as ``on``.
    session_date = on.date()
    tz = on.tzinfo
    start_local = datetime(session_date.year, session_date.month, session_date.day,
                           0, 0, tzinfo=tz)
    end_local = datetime(session_date.year, session_date.month, session_date.day,
                         7, 0, tzinfo=tz)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    return compute_asian_range(pair, start_dt=start_utc, end_dt=end_utc)
