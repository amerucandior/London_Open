"""
Entry point for live trading.

Usage:
    python run_live.py
    python run_live.py --pairs EURUSD GBPUSD --poll 2.0 --db runtime_state.sqlite
"""
from __future__ import annotations

import argparse
import logging
import sys

try:
    import MetaTrader5 as mt5
except Exception:  # pragma: no cover - fallback for Linux/Colab dev
    from utils.mt5_stub import mt5

from runtime.loop import TIMEFRAME, main


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="London Open/Close live trading runtime")
    p.add_argument(
        "--pairs",
        nargs="+",
        default=["EURUSD", "GBPUSD"],
        metavar="PAIR",
        help="Symbols to watch (default: EURUSD GBPUSD)",
    )
    p.add_argument(
        "--poll",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="Bar feed polling interval in seconds (default: 2.0)",
    )
    p.add_argument(
        "--db",
        default="runtime_state.sqlite",
        metavar="PATH",
        help="SQLite state file path (default: runtime_state.sqlite)",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG-level logging (per-bar skip reasons, etc.)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    watchlist = [(pair.upper(), TIMEFRAME) for pair in args.pairs]
    main(
        watchlist=watchlist,
        db_path=args.db,
        poll_seconds=args.poll,
        log_level=logging.DEBUG if args.debug else logging.INFO,
    )
