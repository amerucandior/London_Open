"""
Smoke test for the MT5 order flow.

Exercises `place_bracket_order` + `close_position` end-to-end against the
live MT5 terminal — primarily to verify that the filling-mode selection
in `runtime/broker.get_filling_mode` is accepted by the broker.

Usage:
    python smoke_test_order.py [PAIR] [SIDE] [LOTS]

Defaults: GBPUSD SHORT 0.01

Run on a DEMO account. Asks for confirmation before sending the order.
"""
from __future__ import annotations

import logging
import sys
import time

from runtime.broker import close_position, place_bracket_order
from runtime.state import StoredPosition
from utils.mt5_utils import MT5Connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("smoke_test")


class _MemStore:
    """Minimal in-memory stand-in for StateStore — keeps prod state clean."""

    def __init__(self) -> None:
        self._positions: dict[int, StoredPosition] = {}

    def add_position(self, pos: StoredPosition) -> None:
        self._positions[pos.ticket] = pos
        log.info("store.add_position ticket=%d", pos.ticket)

    def close_position(self, ticket: int, closed_utc: str, pnl: float) -> None:
        self._positions.pop(ticket, None)
        log.info("store.close_position ticket=%d pnl=%.2f", ticket, pnl)

    def open_positions(self, pair: str) -> list[StoredPosition]:
        return [p for p in self._positions.values() if p.pair == pair]


def _compute_bracket(pair: str, side: str) -> tuple[float, float, float]:
    """Return (approx_entry, sl, tp) with a 100-pip bracket on each side."""
    with MT5Connection() as conn:
        info = conn.symbol_info(pair)
        tick = conn.symbol_info_tick(pair)
    if info is None or tick is None:
        raise RuntimeError(
            f"no symbol/tick info for {pair} — is it enabled in Market Watch?"
        )
    # 1000 points ≈ 100 pips on a 5-digit FX broker, 100 pips on 3-digit JPY.
    distance = info.point * 1000
    if side == "LONG":
        entry = tick.ask
        return entry, entry - distance, entry + distance
    entry = tick.bid
    return entry, entry + distance, entry - distance


def main() -> int:
    pair = sys.argv[1] if len(sys.argv) > 1 else "GBPUSD"
    side = sys.argv[2].upper() if len(sys.argv) > 2 else "SHORT"
    lots = float(sys.argv[3]) if len(sys.argv) > 3 else 0.01

    if side not in {"LONG", "SHORT"}:
        log.error("SIDE must be LONG or SHORT (got %r)", side)
        return 2

    entry, sl, tp = _compute_bracket(pair, side)
    log.info(
        "PLAN: pair=%s side=%s lots=%.2f entry≈%.5f sl=%.5f tp=%.5f",
        pair, side, lots, entry, sl, tp,
    )

    if input("Send live order? [y/N] ").strip().lower() != "y":
        log.info("aborted by user")
        return 0

    store = _MemStore()
    ticket = place_bracket_order(
        pair=pair,
        side=side,
        lots=lots,
        sl=sl,
        tp=tp,
        strategy="smoke_test",
        store=store,
        comment="smoke",
    )
    if ticket is None:
        log.error("OPEN FAILED — see broker error above")
        return 1
    log.info("OPEN OK ticket=%d — sleeping 5s before close", ticket)
    time.sleep(5)

    if close_position(ticket, pair, store):
        log.info("CLOSE OK — round trip succeeded ✅")
        return 0

    log.error(
        "CLOSE FAILED — ticket %d may still be open in MT5, close it manually!",
        ticket,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
