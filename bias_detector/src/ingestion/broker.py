# src/ingestion/broker.py
from pydantic import BaseModel, field_validator
import pandas as pd
from .base import fetch_with_retry

class OHLCVBar(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    @field_validator("high")
    @classmethod
    def high_gte_low(cls, v, info):
        if "low" in info.data and v < info.data["low"]:
            raise ValueError("high < low — corrupt bar")
        return v

def fetch_ohlcv(pair: str, period: str = "2y") -> pd.DataFrame:
    def _pull():
        import yfinance as yf
        raw = yf.download(pair, period=period, interval="1d", progress=False)
        raw.columns = [c.lower() for c in raw.columns]
        # Validate every row through Pydantic — drops corrupt bars
        valid_rows = []
        for ts, row in raw.iterrows():
            try:
                bar = OHLCVBar(
                    timestamp=str(ts),
                    open=row["open"], high=row["high"],
                    low=row["low"],   close=row["close"],
                    volume=row.get("volume", 0)
                )
                valid_rows.append(bar.model_dump())
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Dropped bar {ts}: {e}")
        return pd.DataFrame(valid_rows).set_index("timestamp")

    return fetch_with_retry(_pull, source=f"broker:{pair}")