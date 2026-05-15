"""Tests for runtime/state.py — SQLite state store."""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest

from runtime.state import StateStore, StoredPosition


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    return StateStore(db_path=str(tmp_path / "test.sqlite"))


# ------------------------------------------------------------------ flags


class TestFlags:
    def test_set_and_has(self, store: StateStore):
        day = date(2026, 1, 12)
        assert store.has_flag(day, "EURUSD", "open_breakout_taken") is False
        store.set_flag(day, "EURUSD", "open_breakout_taken")
        assert store.has_flag(day, "EURUSD", "open_breakout_taken") is True

    def test_different_pair_independent(self, store: StateStore):
        day = date(2026, 1, 12)
        store.set_flag(day, "EURUSD", "flag_a")
        assert store.has_flag(day, "GBPUSD", "flag_a") is False

    def test_case_insensitive_pair(self, store: StateStore):
        day = date(2026, 1, 12)
        store.set_flag(day, "eurusd", "x")
        assert store.has_flag(day, "EURUSD", "x") is True


# ------------------------------------------------------------------ positions


class TestPositions:
    def _pos(self, ticket: int = 1001, pair: str = "EURUSD") -> StoredPosition:
        return StoredPosition(
            ticket=ticket, pair=pair, strategy="london_open", side="LONG",
            lots=0.1, entry=1.1000, sl=1.0950, tp=1.1100,
            opened_utc="2026-01-12T07:05:00Z",
        )

    def test_add_and_list(self, store: StateStore):
        store.add_position(self._pos())
        positions = store.open_positions()
        assert len(positions) == 1
        assert positions[0].ticket == 1001

    def test_filter_by_pair(self, store: StateStore):
        store.add_position(self._pos(1001, "EURUSD"))
        store.add_position(self._pos(1002, "GBPUSD"))
        assert len(store.open_positions("EURUSD")) == 1
        assert len(store.open_positions("GBPUSD")) == 1
        assert len(store.open_positions()) == 2

    def test_close_removes_from_open(self, store: StateStore):
        store.add_position(self._pos())
        store.close_position(1001, "2026-01-12T08:00:00Z", pnl=25.0)
        assert len(store.open_positions()) == 0


# ------------------------------------------------------------------ P&L


class TestPnl:
    def test_zero_default(self, store: StateStore):
        assert store.daily_pnl(date(2026, 1, 12)) == 0.0

    def test_accumulates(self, store: StateStore):
        pos = StoredPosition(
            ticket=1, pair="EURUSD", strategy="lo", side="LONG",
            lots=0.1, entry=1.1, sl=None, tp=None, opened_utc="2026-01-12T07:00:00Z",
        )
        store.add_position(pos)
        store.close_position(1, "2026-01-12T08:00:00Z", pnl=50.0)

        pos2 = StoredPosition(
            ticket=2, pair="EURUSD", strategy="lo", side="SHORT",
            lots=0.1, entry=1.1, sl=None, tp=None, opened_utc="2026-01-12T09:00:00Z",
        )
        store.add_position(pos2)
        store.close_position(2, "2026-01-12T10:00:00Z", pnl=-20.0)

        assert store.daily_pnl(date.today()) == pytest.approx(30.0)


# ------------------------------------------------------------------ VWAP


class TestVwap:
    def test_none_when_empty(self, store: StateStore):
        assert store.get_vwap(date(2026, 1, 12), "EURUSD") is None

    def test_accumulates(self, store: StateStore):
        day = date(2026, 1, 12)
        store.update_vwap(day, "EURUSD", pv_delta=110.0, vol_delta=100.0)
        store.update_vwap(day, "EURUSD", pv_delta=220.0, vol_delta=200.0)
        # (110+220) / (100+200) = 1.1
        assert store.get_vwap(day, "EURUSD") == pytest.approx(1.1)
