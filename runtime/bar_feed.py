"""
Polls MT5 for new **closed** bars. Emits one event per (symbol, timeframe) close.

Why closed bars? Both strategies require ``bar_close_outside_asian_range`` style
confirmation — acting on still-forming candles produces wick-based false positives.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Tuple

import pandas as pd

from utils.mt5_utils import MT5Connection


@dataclass(frozen=True)
class Bar:
    symbol: str
    timeframe: int
    time_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


BarHandler = Callable[[Bar], None]


def _last_closed_bar(symbol: str, timeframe: int) -> Bar | None:
    # pos=1 → most recent **closed** bar; pos=0 is the still-forming one.
    with MT5Connection() as conn:
        raw = conn.copy_rates_from_pos(symbol, timeframe, 1, 1)
    if raw is None or len(raw) == 0:
        return None
    df = pd.DataFrame(raw)
    r = df.iloc[0]
    return Bar(
        symbol=symbol,
        timeframe=timeframe,
        time_utc=datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
        open=float(r["open"]),
        high=float(r["high"]),
        low=float(r["low"]),
        close=float(r["close"]),
        volume=float(r.get("tick_volume", 0.0)),
    )


class BarFeed:
    """Polling bar feed; calls handlers only when a NEW closed bar appears."""

    def __init__(self, poll_seconds: float = 2.0) -> None:
        self.poll_seconds = poll_seconds
        self._handlers: List[BarHandler] = []
        self._seen: Dict[Tuple[str, int], datetime] = {}
        self._stop = False

    def subscribe(self, handler: BarHandler) -> None:
        self._handlers.append(handler)

    def stop(self) -> None:
        self._stop = True

    def run(self, watchlist: List[Tuple[str, int]]) -> None:
        """Block forever; for each (symbol, timeframe), fire handlers on new closed bars."""
        while not self._stop:
            for symbol, tf in watchlist:
                try:
                    bar = _last_closed_bar(symbol, tf)
                except Exception:
                    continue
                if bar is None:
                    continue
                last = self._seen.get((symbol, tf))
                if last is None or bar.time_utc > last:
                    self._seen[(symbol, tf)] = bar.time_utc
                    for h in self._handlers:
                        try:
                            h(bar)
                        except Exception as exc:  # never let a handler kill the loop
                            print(f"[bar_feed] handler error: {exc!r}")
            time.sleep(self.poll_seconds)