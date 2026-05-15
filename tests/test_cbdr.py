"""Tests for centralbank_dealers_range.py — CBDR logic."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from centralbank_dealers_range import cbdr_range, lod_hod_projection


# ------------------------------------------------------------------ fixtures

NY = ZoneInfo("America/New_York")


def _make_bars(
    n: int = 24,
    open_: float = 1.1000,
    high_add: float = 0.0005,
    low_sub: float = 0.0005,
    close_: float = 1.1002,
) -> pd.DataFrame:
    """Create 5-min bars spanning 14:00–16:00 NY on a single day (stored as naive UTC)."""
    start_ny = datetime(2026, 1, 12, 14, 0, tzinfo=NY)
    start_utc = start_ny.astimezone(timezone.utc).replace(tzinfo=None)
    times = pd.date_range(start_utc, periods=n, freq="5min")
    return pd.DataFrame({
        "time": times,
        "open": [open_] * n,
        "high": [open_ + high_add] * n,
        "low": [open_ - low_sub] * n,
        "close": [close_] * n,
    })


def _fake_symbol_info(point: float = 0.00001, digits: int = 5):
    """Default = 5-digit EURUSD broker. 1 pip = point * 10 = 0.0001."""
    info = MagicMock()
    info.point = point
    info.digits = digits
    return info


# ------------------------------------------------------------------ cbdr_range


class TestCbdrRange:
    def test_basic_body_range(self):
        # 25 actual pips body range on a 5-digit EURUSD feed.
        df = _make_bars(close_=1.1025)
        with patch("centralbank_dealers_range.mt5") as mock_mt5:
            mock_mt5.symbol_info.return_value = _fake_symbol_info()
            result = cbdr_range(df, "EURUSD")

        # body high = max(open, close) = 1.1025, body low = min(open, close) = 1.1000
        assert result["cbdr_high"] == pytest.approx(1.1025, abs=1e-6)
        assert result["cbdr_low"] == pytest.approx(1.1000, abs=1e-6)
        assert result["cbdr_range_pips"] == pytest.approx(25.0, abs=0.1)  # 0.0025 / 0.0001 = 25 pips
        assert result["within_ideal"] is True  # 25 pips within 20-30

    def test_wick_mode(self):
        df = _make_bars(high_add=0.0010, low_sub=0.0010)
        with patch("centralbank_dealers_range.mt5") as mock_mt5:
            mock_mt5.symbol_info.return_value = _fake_symbol_info()
            result = cbdr_range(df, "EURUSD", use_wicks=True)

        # wicks: high=1.1010, low=1.0990 → range=0.0020 → 200 pips
        assert result["cbdr_high"] == pytest.approx(1.1010, abs=1e-6)
        assert result["cbdr_low"] == pytest.approx(1.0990, abs=1e-6)
        assert result["use_wicks"] is True

    def test_sigma_levels(self):
        df = _make_bars()
        with patch("centralbank_dealers_range.mt5") as mock_mt5:
            mock_mt5.symbol_info.return_value = _fake_symbol_info()
            result = cbdr_range(df, "EURUSD")

        rng = result["cbdr_range_price"]
        assert result["hod_1sigma_price"] == pytest.approx(result["cbdr_high"] + rng)
        assert result["lod_1sigma_price"] == pytest.approx(result["cbdr_low"] - rng)
        assert result["hod_2sigma_price"] == pytest.approx(result["cbdr_high"] + 2 * rng)
        assert result["lod_2sigma_price"] == pytest.approx(result["cbdr_low"] - 2 * rng)

    def test_missing_columns_raises(self):
        df = pd.DataFrame({"time": [1], "open": [1]})
        with pytest.raises(ValueError, match="must contain columns"):
            cbdr_range(df, "EURUSD")

    def test_empty_window_raises(self):
        # Bars outside 14:00-20:00 NY
        start_utc = datetime(2026, 1, 12, 10, 0, tzinfo=NY).astimezone(timezone.utc).replace(tzinfo=None)
        times = pd.date_range(start_utc, periods=5, freq="5min")
        df = pd.DataFrame({
            "time": times,
            "open": [1.1] * 5, "high": [1.1] * 5,
            "low": [1.1] * 5, "close": [1.1] * 5,
        })
        with patch("centralbank_dealers_range.mt5"):
            with pytest.raises(ValueError, match="No bars found"):
                cbdr_range(df, "EURUSD")

    def test_symbol_info_none_raises(self):
        df = _make_bars()
        with patch("centralbank_dealers_range.mt5") as mock_mt5:
            mock_mt5.symbol_info.return_value = None
            with pytest.raises(RuntimeError, match="symbol_info is None"):
                cbdr_range(df, "EURUSD")


# ------------------------------------------------------------------ lod_hod_projection


class TestLodHodProjection:
    CBDR = {
        "lod_1sigma_price": 1.0980,
        "lod_2sigma_price": 1.0960,
        "hod_1sigma_price": 1.1020,
        "hod_2sigma_price": 1.1040,
        "cbdr_high": 1.1002,
        "cbdr_low": 1.1000,
        "cbdr_range_pips": 20.0,
        "within_ideal": True,
        "within_max": True,
    }

    def test_bullish(self):
        result = lod_hod_projection(self.CBDR, direction_bias=1)
        assert result["primary_thesis"] == "LoD"
        assert "lod_1sigma" in result["primary_price_levels"]
        assert "hod_1sigma" not in result["primary_price_levels"]

    def test_bearish(self):
        result = lod_hod_projection(self.CBDR, direction_bias=-1)
        assert result["primary_thesis"] == "HoD"
        assert "hod_1sigma" in result["primary_price_levels"]
        assert "lod_1sigma" not in result["primary_price_levels"]

    def test_neutral(self):
        result = lod_hod_projection(self.CBDR, direction_bias=0)
        assert result["primary_thesis"] == "neutral"
        assert len(result["primary_price_levels"]) == 4

    def test_passthrough_fields(self):
        result = lod_hod_projection(self.CBDR, direction_bias=0)
        assert result["cbdr_high"] == self.CBDR["cbdr_high"]
        assert result["within_ideal"] is True
