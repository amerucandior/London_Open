import MetaTrader5 as mt5
import config
from math import inf

from datetime import datetime, timezone
from utils.mt5_utils import compute_asian_range

asian_high = float(-inf)
asian_low = float(inf)
asian_mid = 0

asian_start = config.config["session"]["asian"]["start"]
asian_end = config.config["session"]["asian"]["end"]

today = datetime.now(timezone.utc)
asian_start_dt = today.replace(hour=asian_start, minute=0, second=0, microsecond=0)
asian_end_dt = today.replace(hour=asian_end, minute=0, second=0, microsecond=0)

rates = compute_asian_range(symbol="GBPUSD", start_dt=asian_start_dt, end_dt=asian_end_dt)

asian_high = max(rates['high'] for rate in rates)
asian_low = min(rates['low'] for rate in rates)
asian_mid = (asian_high + asian_low) / 2