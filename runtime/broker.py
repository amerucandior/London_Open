"""
MT5 execution adapter.

Responsibilities:
- Build market-order requests with SL + TP bracket.
- Close a position by ticket (market close at current bid/ask).
- Close ALL positions for a pair (flat-by-time enforcement).
- Sync closed MT5 positions back to the state store.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

try:
    import MetaTrader5 as mt5
except Exception:  # pragma: no cover - fallback for Linux/Colab dev
    from utils.mt5_stub import mt5

from runtime.state import StateStore, StoredPosition
from utils.mt5_utils import MT5Connection

log = logging.getLogger(__name__)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ public API

def place_bracket_order(
    *,
    pair: str,
    side: str,           # "LONG" or "SHORT"
    lots: float,
    sl: float,
    tp: Optional[float],
    strategy: str,
    store: StateStore,
    comment: str = "",
) -> Optional[int]:
    """
    Send a market order + SL/TP bracket to MT5.
    Returns the MT5 position ticket on success, ``None`` on failure.
    """
    with MT5Connection() as conn:
        info = conn.symbol_info(pair)
        tick = conn.symbol_info_tick(pair)

        if info is None or tick is None:
            log.error("place_bracket_order: no symbol/tick info for %s", pair)
            return None

        order_type = mt5.ORDER_TYPE_BUY if side == "LONG" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side == "LONG" else tick.bid

        request: dict = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pair,
            "volume":       lots,
            "type":         order_type,
            "price":        price,
            "sl":           sl,
            "deviation":    10,
            "magic":        202600,
            "comment":      comment or strategy,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if tp is not None:
            request["tp"] = tp

        result = conn.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        retcode = result.retcode if result else "no_result"
        comment_str = result.comment if result else ""
        log.error(
            "place_bracket_order: FAILED pair=%s side=%s retcode=%s comment=%s",
            pair, side, retcode, comment_str,
        )
        return None

    ticket = result.order
    log.info(
        "place_bracket_order: OK pair=%s side=%s lots=%.2f ticket=%d sl=%.5f tp=%s",
        pair, side, lots, ticket, sl, tp,
    )

    store.add_position(StoredPosition(
        ticket=ticket,
        pair=pair,
        strategy=strategy,
        side=side,
        lots=lots,
        entry=price,
        sl=sl,
        tp=tp,
        opened_utc=_now_utc_iso(),
    ))
    return ticket


def close_position(ticket: int, pair: str, store: StateStore) -> bool:
    """
    Close a single open position by ticket at current market price.
    Reconciles realised P&L back to the state store.
    Returns True on success.
    """
    with MT5Connection() as conn:
        positions = conn.positions_get(symbol=pair)
        if positions is None:
            log.error("close_position: positions_get returned None for %s", pair)
            return False

        pos = next((p for p in positions if p.ticket == ticket), None)
        if pos is None:
            log.warning("close_position: ticket %d not found in MT5 — already closed?", ticket)
            store.close_position(ticket, _now_utc_iso(), 0.0)
            return False

        close_type = (
            mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY
            else mt5.ORDER_TYPE_BUY
        )
        tick = conn.symbol_info_tick(pair)
        if tick is None:
            log.error("close_position: no tick for %s", pair)
            return False

        close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pair,
            "volume":       pos.volume,
            "type":         close_type,
            "position":     ticket,
            "price":        close_price,
            "deviation":    10,
            "magic":        202600,
            "comment":      "runtime_close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = conn.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        retcode = result.retcode if result else "no_result"
        log.error("close_position: FAILED ticket=%d retcode=%s", ticket, retcode)
        return False

    pnl = pos.profit
    log.info("close_position: OK ticket=%d pnl=%.2f", ticket, pnl)
    store.close_position(ticket, _now_utc_iso(), pnl)
    return True


def close_all_for_pair(pair: str, store: StateStore) -> int:
    """
    Close every open position for ``pair`` — used by flat-by-time enforcement.
    Returns number of positions closed.
    """
    open_pos = store.open_positions(pair)
    closed = 0
    for p in open_pos:
        if close_position(p.ticket, pair, store):
            closed += 1
    return closed