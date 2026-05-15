"""Tests for strategies/overextension.py — EMA stretch filter."""
from __future__ import annotations

import pandas as pd
import pytest

from strategies.overextension import Overextension


class TestOverextension:
    def test_defaults(self):
        o = Overextension()
        assert o.ema_period == 50
        assert o.threshold_pips == 20.0

    def test_calculate_stretch_pips(self):
        o = Overextension()
        # 0.0015 * 10000 = 15 pips
        assert o.calculate_stretch_pips(1.1015, 1.1000) == pytest.approx(15.0)

    def test_is_overextended(self):
        o = Overextension(threshold_pips=10.0)
        assert o.is_overextended(10.0) is True
        assert o.is_overextended(9.99) is False

    def test_calculate_single_point(self):
        o = Overextension(ema_period=20, threshold_pips=10.0)
        result = o.calculate_single_point(current_price=1.1020, ema_value=1.1000)
        assert result["stretch_pips"] == pytest.approx(20.0)
        assert result["is_overextended"] is True
        assert result["threshold"] == 10.0
        assert result["ema_period"] == 20

    def test_calculate_ema(self):
        o = Overextension(ema_period=3)
        prices = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        ema = o.calculate_ema(prices)
        assert len(ema) == 5
        # EMA should be smoothed — last value < 5 but > simple mean
        assert ema.iloc[-1] < 5.0
        assert ema.iloc[-1] > 3.0

    def test_calculate_factor(self):
        o = Overextension(ema_period=3, threshold_pips=50.0)
        df = pd.DataFrame({
            "close": [1.1000, 1.1000, 1.1000, 1.1100, 1.1100],
            "timestamp": pd.date_range("2026-01-01", periods=5, freq="h"),
        })
        result = o.calculate_factor(df)
        assert len(result) == 5
        # First values close to EMA → not overextended; later ones diverge
        assert result.iloc[0] is False or result.iloc[0] == False  # noqa: E712

    def test_calculate_factor_missing_col_raises(self):
        o = Overextension()
        df = pd.DataFrame({"price": [1.0]})
        with pytest.raises(ValueError, match="Price column"):
            o.calculate_factor(df)
