"""
Main runtime loop.

On every closed M5 bar for each watched pair:
  1. Update session VWAP in the state store.
  2. Enforce flat-by-time (close all LC positions after lc_flat_by_local).
  3. Run the London Open breakout signal engine.
  4. Run the London Close mean-reversion signal engine.

Each engine follows the same contract:
  - Pull the plan (pure function, no side effects).
  - Evaluate whether a trade trigger is present on this closed bar.
  - Run the risk gate.
  - Route to broker if all clear.
  - Record a one-shot flag so the same edge is only taken once per day.
"""
from __future__ import annotations

import logging
from datetime import date

try:
    import MetaTrader5 as mt5
except Exception:  # pragma: no cover - fallback for Linux/Colab dev
    from utils.mt5_stub import mt5

import session
from runtime.bar_feed import Bar, BarFeed
from runtime.broker import close_all_for_pair, place_bracket_order
from runtime.risk import RiskGate
from runtime.state import StateStore
from strategies.london_close import london_close_plan
from strategies.london_open import london_open_plan

log = logging.getLogger(__name__)

# M5 is the confirmation timeframe for both strategies.
TIMEFRAME = mt5.TIMEFRAME_M5

# ----------------------------------------------------------------- VWAP helper

def _update_vwap(bar: Bar, store: StateStore) -> None:
    """Accumulate price×volume and volume for a running session VWAP."""
    typical = (bar.high + bar.low + bar.close) / 3.0
    store.update_vwap(date.today(), bar.symbol, typical * bar.volume, bar.volume)


# --------------------------------------------------------- London Open engine

def _handle_open_breakout(bar: Bar, store: StateStore, risk: RiskGate) -> None:
    today = date.today()
    flag = "open_breakout_taken"

    if store.has_flag(today, bar.symbol, flag):
        return

    plan = london_open_plan(bar.symbol)

    if not plan.get("trade"):
        log.debug(
            "open_breakout: skipped %s — reason=%s armed=%s",
            bar.symbol,
            plan.get("reason"),
            plan.get("setup_armed"),
        )
        return

    asian = plan.get("asian_range", {})
    if "high" not in asian or "low" not in asian:
        log.warning("open_breakout: asian range missing for %s", bar.symbol)
        return

    c = bar.close
    if c > asian["high"]:
        side = "LONG"
        sl = asian["low"]
    elif c < asian["low"]:
        side = "SHORT"
        sl = asian["high"]
    else:
        return  # bar closed inside range — no breakout yet

    # Only trade in COT bias direction when a directional bias is present.
    # `bias` is an integer (+1 long / -1 short / 0 flat); `side` is "LONG"/"SHORT".
    # Comparing int to str was always not-equal — silently rejecting every
    # confirmed-regime trade. Map side to its directional integer first.
    bias = plan.get("direction_bias") or 0
    side_dir = 1 if side == "LONG" else -1
    if bias != 0 and bias != side_dir:
        log.info(
            "open_breakout: skipping %s %s — COT bias is %s",
            bar.symbol, side, bias,
        )
        return

    lots = round(float(plan.get("lot_size", 1.0)), 2)

    verdict = risk.check(bar.symbol, lots)
    if not verdict.allowed:
        log.info("open_breakout: risk gate blocked %s — %s", bar.symbol, verdict.reason)
        return

    # TP at 1.5× range extension.
    width = asian["high"] - asian["low"]
    tp = asian["high"] + 1.5 * width if side == "LONG" else asian["low"] - 1.5 * width

    ticket = place_bracket_order(
        pair=bar.symbol,
        side=side,
        lots=lots,
        sl=sl,
        tp=tp,
        strategy="london_open_breakout",
        store=store,
    )
    if ticket:
        store.set_flag(today, bar.symbol, flag)
        log.info("open_breakout: placed %s %s ticket=%d", bar.symbol, side, ticket)


# -------------------------------------------------------- London Close engine

def _handle_close_mr(bar: Bar, store: StateStore, risk: RiskGate) -> None:
    today = date.today()
    flag = "close_mr_taken"

    if store.has_flag(today, bar.symbol, flag):
        return

    plan = london_close_plan(bar.symbol)

    if not plan.get("engage_mean_reversion_leg"):
        log.debug(
            "close_mr: skipped %s — playbook=%s",
            bar.symbol,
            plan.get("playbook"),
        )
        return

    # Fade direction from COT mean reversion hint.
    mr_hint = plan.get("mean_reversion_hint")
    if mr_hint == "fade_crowded_long":
        side = "SHORT"
    elif mr_hint == "fade_crowded_short":
        side = "LONG"
    else:
        return

    # VWAP is the mean-reversion target; stop is one bar's range beyond the close.
    vwap = store.get_vwap(today, bar.symbol)
    if vwap is None:
        log.warning("close_mr: no VWAP for %s, skipping", bar.symbol)
        return

    bar_range = bar.high - bar.low
    if bar_range <= 0:
        return

    if side == "SHORT":
        if bar.close <= vwap:
            return  # price at or below mean already
        sl = bar.high + bar_range
        tp = vwap
    else:
        if bar.close >= vwap:
            return
        sl = bar.low - bar_range
        tp = vwap

    lots = 1.0

    verdict = risk.check(bar.symbol, lots)
    if not verdict.allowed:
        log.info("close_mr: risk gate blocked %s — %s", bar.symbol, verdict.reason)
        return

    ticket = place_bracket_order(
        pair=bar.symbol,
        side=side,
        lots=lots,
        sl=sl,
        tp=tp,
        strategy="london_close_mr",
        store=store,
    )
    if ticket:
        store.set_flag(today, bar.symbol, flag)
        log.info("close_mr: placed %s %s ticket=%d", bar.symbol, side, ticket)


# ------------------------------------------------------ flat-by-time enforcer

def _enforce_flat(pair: str, store: StateStore) -> None:
    if session.past_lc_flat_cutoff():
        closed = close_all_for_pair(pair, store)
        if closed:
            log.info("flat_enforcement: closed %d LC positions for %s", closed, pair)


# ---------------------------------------------------------------- bar handler

def make_on_bar(store: StateStore, risk: RiskGate):
    """Return the BarFeed handler bound to the given store and risk gate."""
    def on_bar(bar: Bar) -> None:
        _update_vwap(bar, store)
        _enforce_flat(bar.symbol, store)
        _handle_open_breakout(bar, store, risk)
        _handle_close_mr(bar, store, risk)

    return on_bar


# --------------------------------------------------------------------- main()

def main(
    watchlist: list[tuple[str, int]] | None = None,
    db_path: str = "runtime_state.sqlite",
    poll_seconds: float = 2.0,
    log_level: int = logging.INFO,
) -> None:
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if watchlist is None:
        watchlist = [("EURUSD", TIMEFRAME), ("GBPUSD", TIMEFRAME)]

    store = StateStore(db_path=db_path)
    risk = RiskGate(store)
    feed = BarFeed(poll_seconds=poll_seconds)
    feed.subscribe(make_on_bar(store, risk))

    log.info("runtime starting — watchlist=%s", [s for s, _ in watchlist])
    try:
        feed.run(watchlist)
    except KeyboardInterrupt:
        log.info("runtime stopped by user")
    finally:
        feed.stop()
