"""
Session gates in **London** timezone (``config.session_clock.tz_name``).

London Open breakout uses only the **open impulse** window (default 07:00–08:00 local).
London Close uses **pre-close** + **close** windows (defaults 15:30–16:00 and 16:00–17:00 local).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config


def _clock() -> dict:
    return config.config.get("session_clock", {})


def session_tz() -> ZoneInfo:
    return ZoneInfo(str(_clock().get("tz_name", "Europe/London")))


def now_local() -> datetime:
    return datetime.now(session_tz())


def to_local(dt: datetime | None) -> datetime:
    """Interpret naive ``dt`` as UTC; convert any aware ``dt`` to session local."""
    if dt is None:
        return now_local()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(session_tz())


def _to_minutes(hour: int, minute: int) -> int:
    return hour * 60 + minute


def _in_closed_interval_local(now_local: datetime, window_key: str) -> bool:
    w = _clock().get(window_key)
    if not w:
        return False
    cur = _to_minutes(now_local.hour, now_local.minute)
    start = _to_minutes(int(w["start_hour"]), int(w["start_minute"]))
    end = _to_minutes(int(w["end_hour"]), int(w["end_minute"]))
    return start <= cur <= end


def allow_london_open_impulse(dt: datetime | None = None) -> bool:
    """True during the London **open impulse** window (breakout hunt), local time."""
    return _in_closed_interval_local(to_local(dt), "london_open_impulse")


def allow_london_close_pre(dt: datetime | None = None) -> bool:
    """True 15–30 minutes before the scheduled London close (local)."""
    return _in_closed_interval_local(to_local(dt), "london_close_pre")


def allow_london_close_window(dt: datetime | None = None) -> bool:
    """True during the main London close mean-reversion window (local)."""
    return _in_closed_interval_local(to_local(dt), "london_close")


def past_lc_flat_cutoff(dt: datetime | None = None) -> bool:
    """True after configured local time — do not hold London Close legs past this point."""
    now_l = to_local(dt)
    fc = _clock().get("lc_flat_by_local", {"hour": 21, "minute": 0})
    cutoff = _to_minutes(int(fc["hour"]), int(fc["minute"]))
    return _to_minutes(now_l.hour, now_l.minute) >= cutoff


# --- Legacy names (used by older strategy imports) ---


def allow_london_open(dt: datetime | None = None) -> bool:
    """Alias: same as ``allow_london_open_impulse`` (London Open **breakout** window)."""
    return allow_london_open_impulse(dt)


def allow_london_close(dt: datetime | None = None) -> bool:
    """True during **pre-close OR close** window (London Close MR lifecycle)."""
    return allow_london_close_pre(dt) or allow_london_close_window(dt)


def allow_trade(hour: int, minute: int) -> bool:
    """Legacy: treat hour/minute as **UTC** wall clock (deprecated)."""
    dt = datetime.now(timezone.utc).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return allow_london_open_impulse(dt)


def is_session_active(hour: int, session_name: str) -> bool:
    session = config.config["session"][session_name]
    return session["start"] <= hour < session["end"]
