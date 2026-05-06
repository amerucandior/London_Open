"""
Pre-trade risk gate — every order must pass ALL checks before routing to MT5.

Checks (in order):
1. Kill switch flag file  — create a file named ``KILL`` in the project root to halt all orders.
2. Daily loss limit       — configurable via ``config.trade_limits.daily_loss_limit``.
3. Max open positions     — per-pair and global caps from ``config.trade_limits``.
4. Spread ceiling         — reject if live spread > configured max pips.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

try:
    import MetaTrader5 as mt5
except Exception:  # pragma: no cover - fallback for Linux/Colab dev
    from utils.mt5_stub import mt5

import config
from runtime.state import StateStore
from utils.mt5_utils import MT5Connection

KILL_FILE = Path("KILL")


@dataclass
class RiskVerdict:
    allowed: bool
    reason: str


class RiskGate:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    # ------------------------------------------------------------------ public
    def check(self, pair: str, lots: float) -> RiskVerdict:
        """Run all gates in sequence. Returns on first failure."""
        verdict = self._kill_switch()
        if not verdict.allowed:
            return verdict

        verdict = self._daily_loss(date.today())
        if not verdict.allowed:
            return verdict

        verdict = self._max_positions(pair)
        if not verdict.allowed:
            return verdict

        verdict = self._spread(pair)
        if not verdict.allowed:
            return verdict

        return RiskVerdict(allowed=True, reason="all_clear")

    # ----------------------------------------------------------------- checks
    def _kill_switch(self) -> RiskVerdict:
        if KILL_FILE.exists():
            return RiskVerdict(False, "kill_switch_file_present")
        return RiskVerdict(True, "ok")

    def _daily_loss(self, today: date) -> RiskVerdict:
        limits = config.config.get("trade_limits", {})
        cap = float(limits.get("daily_loss_limit", 0.0))
        if cap <= 0.0:
            return RiskVerdict(True, "no_loss_limit_configured")
        pnl = self._store.daily_pnl(today)
        if pnl <= -abs(cap):
            return RiskVerdict(False, f"daily_loss_limit_hit pnl={pnl:.2f} cap={cap:.2f}")
        return RiskVerdict(True, "ok")

    def _max_positions(self, pair: str) -> RiskVerdict:
        limits = config.config.get("trade_limits", {})
        max_pair = int(limits.get("max_positions_per_pair", 1))
        max_global = int(limits.get("max_positions_global", 4))

        open_pair = self._store.open_positions(pair)
        if len(open_pair) >= max_pair:
            return RiskVerdict(
                False,
                f"max_positions_per_pair={max_pair} already reached for {pair}",
            )

        open_all = self._store.open_positions()
        if len(open_all) >= max_global:
            return RiskVerdict(
                False,
                f"max_positions_global={max_global} already reached",
            )

        return RiskVerdict(True, "ok")

    def _spread(self, pair: str) -> RiskVerdict:
        limits = config.config.get("spread_limits", {})
        # per-pair override first, then default
        max_spread_pips = float(
            limits.get(pair.upper(), limits.get("default_max_pips", 0.0))
        )
        if max_spread_pips <= 0.0:
            return RiskVerdict(True, "no_spread_limit_configured")

        try:
            with MT5Connection() as conn:
                tick = conn.symbol_info_tick(pair)
                info = conn.symbol_info(pair)
        except Exception as exc:
            return RiskVerdict(False, f"spread_check_failed: {exc}")

        if tick is None or info is None:
            return RiskVerdict(False, "spread_check_no_tick_data")

        digits = info.digits
        pip_size = 10 ** -(digits - 1) if digits >= 2 else 0.0001
        spread_pips = (tick.ask - tick.bid) / pip_size if pip_size else 0.0

        if spread_pips > max_spread_pips:
            return RiskVerdict(
                False,
                f"spread_too_wide {spread_pips:.1f} pips > limit {max_spread_pips:.1f}",
            )
        return RiskVerdict(True, "ok")