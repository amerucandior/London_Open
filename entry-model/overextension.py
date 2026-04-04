import pandas as pd

class Overextension:
    """
    Factor 1: Price Overextension Measurement
    Measures how far price has stretched from medium-term equilibrium (EMA50)
    """

    def __init__(self, ema_period: int = 50, threshold_pips: float = 20.0):
        self.ema_period = ema_period
        self.threshold_pips = threshold_pips
        self.ema_calculator = None

    def calculate_ema(self, prices: pd.Series) -> pd.Series:
        """calculate EMA with proper pandas method"""
        return prices.ewm(span=self.ema_period, adjust=False).mean()

    def calculate_stretch_pips(self, current_price: float, ema_value: float, pip_factor: float = 10000.0) -> float:
        """
        calculates stretch distance in pips 
        pip factor: 10000 for EURUSD
        """
        return abs(current_price - ema_value) * pip_factor

    def is_overextended(self, stretch_pips: float) -> bool:
        """checks if stretch is beyond threshold"""
        return stretch_pips >= self.threshold_pips

    def calculate_factor(
        self,
        df: pd.DataFrame,
        price_col: str = "close",
        timestamp_col: str = "timestamp",
    ) -> pd.Series:
        """
        Vectorised computation of the over-extension flag.
        Returns a Boolean Series whose index matches ``df`` (or the timestamp column
        if it exists).
        """
        # ---- validation -------------------------------------------------------
        if price_col not in df.columns:
            raise ValueError(f"Price column '{price_col}' not in dataframe")

        # ---- EMA ------
        ema_series = self.calculate_ema(df[price_col])

        # ---- stretch in pips ------------
        #   (price – EMA).abs() gives a Series; multiply by the pip factor
        stretch_pips = (df[price_col] - ema_series).abs() * 10000.0   # ← inline, no .values
        #   docs: Series.abs() returns element‑wise absolute values【13†L2-L9】

        # ---- over‑extension flag – element‑wise comparison --------------------
        overextended = stretch_pips.ge(self.threshold_pips)          # same as >=
        #   docs: Series.ge() is the element‑wise >= operator【14†L70-L74】

        # ---- optional timestamp index -----------------------------------------
        if timestamp_col in df.columns:
            # Preserve the original order but replace the index with the timestamp column
            overextended = overextended.set_axis(df[timestamp_col])
            # If you want the column to become the *real* index you could also do:
            # overextended = overextended.set_index(df[timestamp_col])
        # otherwise the index is already df.index

        return overextended


    def calculate_single_point(self, current_price: float, ema_value: float) -> dict:
        """calculate factor for a single point"""
        stretch_pips = self.calculate_stretch_pips(current_price, ema_value)
        is_extended = self.is_overextended(stretch_pips)

        return {
            'stretch_pips': stretch_pips,
            'is_overextended': is_extended,
            'threshold': self.threshold_pips,
            'ema_period': self.ema_period
        }