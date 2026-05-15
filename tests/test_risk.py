"""Tests for runtime/risk.py — pre-trade risk gates."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.risk import RiskGate, RiskVerdict
from runtime.state import StateStore, StoredPosition


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    return StateStore(db_path=str(tmp_path / "test.sqlite"))


@pytest.fixture()
def gate(store: StateStore) -> RiskGate:
    return RiskGate(store)


# ------------------------------------------------------------------ kill switch


class TestKillSwitch:
    def test_no_file_passes(self, gate: RiskGate):
        with patch("runtime.risk.KILL_FILE", Path("__nonexistent_kill__")):
            v = gate._kill_switch()
        assert v.allowed is True

    def test_file_present_blocks(self, gate: RiskGate, tmp_path: Path):
        kill = tmp_path / "KILL"
        kill.touch()
        with patch("runtime.risk.KILL_FILE", kill):
            v = gate._kill_switch()
        assert v.allowed is False
        assert "kill_switch" in v.reason


# ------------------------------------------------------------------ daily loss


class TestDailyLoss:
    def test_no_limit_passes(self, gate: RiskGate):
        with patch.dict("config.config", {"trade_limits": {"daily_loss_limit": 0}}):
            v = gate._daily_loss(date.today())
        assert v.allowed is True

    def test_within_limit_passes(self, gate: RiskGate, store: StateStore):
        # Simulate -100 P&L with 200 cap
        pos = StoredPosition(
            ticket=1, pair="EURUSD", strategy="lo", side="LONG",
            lots=0.1, entry=1.1, sl=None, tp=None, opened_utc="2026-01-12T07:00:00Z",
        )
        store.add_position(pos)
        store.close_position(1, "2026-01-12T08:00:00Z", pnl=-100.0)
        with patch.dict("config.config", {"trade_limits": {"daily_loss_limit": 200}}):
            v = gate._daily_loss(date.today())
        assert v.allowed is True

    def test_exceeds_limit_blocks(self, gate: RiskGate, store: StateStore):
        pos = StoredPosition(
            ticket=1, pair="EURUSD", strategy="lo", side="LONG",
            lots=0.1, entry=1.1, sl=None, tp=None, opened_utc="2026-01-12T07:00:00Z",
        )
        store.add_position(pos)
        store.close_position(1, "2026-01-12T08:00:00Z", pnl=-200.0)
        with patch.dict("config.config", {"trade_limits": {"daily_loss_limit": 200}}):
            v = gate._daily_loss(date.today())
        assert v.allowed is False


# ------------------------------------------------------------------ max positions


class TestMaxPositions:
    def _add_open(self, store: StateStore, ticket: int, pair: str):
        store.add_position(StoredPosition(
            ticket=ticket, pair=pair, strategy="lo", side="LONG",
            lots=0.1, entry=1.1, sl=None, tp=None, opened_utc="2026-01-12T07:00:00Z",
        ))

    def test_under_limit_passes(self, gate: RiskGate):
        with patch.dict("config.config", {"trade_limits": {
            "max_positions_per_pair": 1, "max_positions_global": 4,
        }}):
            v = gate._max_positions("EURUSD")
        assert v.allowed is True

    def test_per_pair_limit_blocks(self, gate: RiskGate, store: StateStore):
        self._add_open(store, 1, "EURUSD")
        with patch.dict("config.config", {"trade_limits": {
            "max_positions_per_pair": 1, "max_positions_global": 4,
        }}):
            v = gate._max_positions("EURUSD")
        assert v.allowed is False
        assert "per_pair" in v.reason

    def test_global_limit_blocks(self, gate: RiskGate, store: StateStore):
        for i in range(4):
            self._add_open(store, i + 1, f"PAIR{i}")
        with patch.dict("config.config", {"trade_limits": {
            "max_positions_per_pair": 2, "max_positions_global": 4,
        }}):
            v = gate._max_positions("NEWPAIR")
        assert v.allowed is False
        assert "global" in v.reason


# ------------------------------------------------------------------ RiskVerdict


class TestRiskVerdict:
    def test_dataclass(self):
        v = RiskVerdict(allowed=True, reason="ok")
        assert v.allowed is True
        assert v.reason == "ok"
