"""EMA stretch filter (e.g. avoid chasing into London)."""
from __future__ import annotations

import pandas as pd


class Overextension:
    """
    Price distance from EMA — flag when stretch in pips exceeds threshold.
    Pip factor 10_000 suits 5-decimal majors (e.g. EURUSD); override for JPY pairs.
    """

    def __init__(self, ema_period: int = 50, threshold_pips: float = 20.0):
        self.ema_period = ema_period
        self.threshold_pips = threshold_pips

    def calculate_ema(self, prices: pd.Series) -> pd.Series:
        return prices.ewm(span=self.ema_period, adjust=False).mean()

    def calculate_stretch_pips(
        self, current_price: float, ema_value: float, pip_factor: float = 10000.0
    ) -> float:
        return abs(current_price - ema_value) * pip_factor

    def is_overextended(self, stretch_pips: float) -> bool:
        return stretch_pips >= self.threshold_pips

    def calculate_factor(
        self,
        df: pd.DataFrame,
        price_col: str = "close",
        timestamp_col: str = "timestamp",
    ) -> pd.Series:
        if price_col not in df.columns:
            raise ValueError(f"Price column '{price_col}' not in dataframe")

        ema_series = self.calculate_ema(df[price_col])
        stretch_pips = (df[price_col] - ema_series).abs() * 10000.0
        overextended = stretch_pips.ge(self.threshold_pips)

        if timestamp_col in df.columns:
            overextended = overextended.set_axis(df[timestamp_col])

        return overextended

    def calculate_single_point(self, current_price: float, ema_value: float) -> dict:
        stretch_pips = self.calculate_stretch_pips(current_price, ema_value)
        is_extended = self.is_overextended(stretch_pips)

        return {
            "stretch_pips": stretch_pips,
            "is_overextended": is_extended,
            "threshold": self.threshold_pips,
            "ema_period": self.ema_period,
        }
