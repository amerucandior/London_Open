"""Tests for session.py — session time gates."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from session import (
    allow_london_close,
    allow_london_close_pre,
    allow_london_close_window,
    allow_london_open,
    allow_london_open_impulse,
    is_session_active,
    past_lc_flat_cutoff,
    session_tz,
    to_local,
)


# ------------------------------------------------------------------ helpers

LONDON = ZoneInfo("Europe/London")


def _utc(hour: int, minute: int = 0) -> datetime:
    """Return a UTC datetime on an arbitrary winter weekday (no DST offset)."""
    return datetime(2026, 1, 12, hour, minute, tzinfo=timezone.utc)


# ------------------------------------------------------------------ tests


class TestSessionTz:
    def test_returns_london(self):
        assert str(session_tz()) == "Europe/London"


class TestToLocal:
    def test_naive_treated_as_utc(self):
        naive = datetime(2026, 1, 12, 7, 30)
        result = to_local(naive)
        assert result.tzinfo is not None
        # In winter London == UTC, so hour stays 7
        assert result.hour == 7

    def test_aware_converted(self):
        eastern = ZoneInfo("America/New_York")
        dt = datetime(2026, 1, 12, 2, 0, tzinfo=eastern)  # 02:00 ET = 07:00 UTC
        result = to_local(dt)
        assert result.hour == 7

    def test_none_returns_now(self):
        result = to_local(None)
        assert result.tzinfo is not None


class TestLondonOpenImpulse:
    """London open impulse window: 07:00–10:00 London local (ICT killzone)."""

    def test_inside(self):
        assert allow_london_open_impulse(_utc(7, 0)) is True
        assert allow_london_open_impulse(_utc(8, 30)) is True
        assert allow_london_open_impulse(_utc(9, 59)) is True

    def test_outside(self):
        assert allow_london_open_impulse(_utc(6, 59)) is False
        assert allow_london_open_impulse(_utc(10, 1)) is False

    def test_alias(self):
        dt = _utc(7, 15)
        assert allow_london_open(dt) == allow_london_open_impulse(dt)


class TestLondonClose:
    """Pre-close 15:30–16:00, close 16:00–17:00 London local."""

    def test_pre_close_inside(self):
        assert allow_london_close_pre(_utc(15, 30)) is True
        assert allow_london_close_pre(_utc(15, 45)) is True

    def test_pre_close_outside(self):
        assert allow_london_close_pre(_utc(15, 29)) is False
        assert allow_london_close_pre(_utc(16, 1)) is False

    def test_close_window_inside(self):
        assert allow_london_close_window(_utc(16, 0)) is True
        assert allow_london_close_window(_utc(16, 30)) is True

    def test_close_window_outside(self):
        assert allow_london_close_window(_utc(15, 59)) is False
        assert allow_london_close_window(_utc(17, 1)) is False

    def test_combined_alias(self):
        # allow_london_close = pre OR window
        assert allow_london_close(_utc(15, 35)) is True
        assert allow_london_close(_utc(16, 30)) is True
        assert allow_london_close(_utc(14, 0)) is False


class TestFlatCutoff:
    def test_before(self):
        assert past_lc_flat_cutoff(_utc(20, 59)) is False

    def test_at_cutoff(self):
        assert past_lc_flat_cutoff(_utc(21, 0)) is True

    def test_after(self):
        assert past_lc_flat_cutoff(_utc(22, 0)) is True


class TestIsSessionActive:
    def test_asian(self):
        assert is_session_active(3, "asian") is True
        assert is_session_active(7, "asian") is False  # end is exclusive

    def test_london(self):
        assert is_session_active(7, "london") is True
        assert is_session_active(15, "london") is True
        assert is_session_active(16, "london") is False
