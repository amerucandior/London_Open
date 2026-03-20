import MetaTrader5 as mt5
import config
from math import inf

from datetime import datetime, timezone

# Initialize MT5
if not mt5.initialize():
    raise Exception("MT5 initialization failed")

asian_high = float(-inf)
asian_low = float(inf)
asian_mid = 0

asian_start = config.config["session"]["asian"]["start"]
asian_end = config.config["session"]["asian"]["end"]

today = datetime.now(timezone.utc)
asian_start_dt = today.replace(hour=asian_start, minute=0, second=0, microsecond=0)
asian_end_dt = today.replace(hour=asian_end, minute=0, second=0, microsecond=0)

symbol = "GBPUSD"
rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_H1, asian_start_dt, asian_end_dt)

asian_high = max(rate['high'] for rate in rates)
asian_low = min(rate['low'] for rate in rates)
asian_mid = (asian_high + asian_low) / 2