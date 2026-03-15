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

    def calculate_ema(self, prices: pd.series) -> pd.series:
        """calculate EMA with proper pandas method"""
        return prices.ewm(span=self.ema_period, adjust=False).mean()

    def calculate_stretch_pips(self, current_price: float, ema_value: float, pip_factor: float = 10000.0) -> float:
        """
        calculates stretch distance in pips 
        pip factor: 1000 for EURUSD
        """
        return abs(current_price - ema_value) * pip_factor

    def is_overextended(self, stretch_pips: float) -> bool:
        """checks if stretch is beyond threshold"""
        return stretch_pips >= self.threshold_pips

    def calculate_factor(self, 
                    df: pd.DataFrame, 
                    price_col: str = 'close',
                    timestamp_col: str = 'timestamp') -> pd.Series:
        """
        Calculate overextension factor for entire series
        Returns: Boolean series indicating overextension condition
        """
        #validate input
        if price_col not in df.columns:
            raise ValueError(f"Price column '{price_col}' not in dataframe")

        #calculate EMA
        ema_series = self.calculate_ema(df[price_col])

        #calculate stretch in pips 
        stretch_pips = self.calculate_stretch_pips(
            df[price_col].values,
            ema_series.values,
            pip_factor=1000.0
        )

        #create result series
        result = pd.Series(
            self.is_overextended(stretch_pips),
            index = df.index if timestamp_col not in df.columns else df[timestamp_col]
        )

        return result

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