"""
Load prior NY-session M5 data and run CBDR + LoD/HoD projection for London Open.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

try:
    import MetaTrader5 as mt5
except Exception:  # pragma: no cover - fallback for Linux/Colab dev
    from utils.mt5_stub import mt5
import pandas as pd
from zoneinfo import ZoneInfo

import centralbank_dealers_range as cbdr_mod
import config
from utils import HAS_MT5
from utils.mt5_utils import MT5Connection


def _reference_ny_date_for_cbdr(anchor_utc: datetime) -> date:
    """
    NY calendar date of the **last completed** 14:00–20:00 CBDR window relative to ``anchor_utc``.
    """
    ny = ZoneInfo("America/New_York")
    t = anchor_utc.astimezone(ny)
    close_today = t.replace(hour=20, minute=0, second=0, microsecond=0)
    if t >= close_today:
        return t.date()
    return t.date() - timedelta(days=1)


def load_cbdr_m5_dataframe(symbol: str, ny_date: date) -> tuple[pd.DataFrame, float]:
    """M5 bars from NY ``ny_date`` 14:00 to 20:00 (America/New_York), ``time`` column UTC.

    Returns ``(df, pip_size)``. ``pip_size`` is fetched in the same MT5Connection
    context — otherwise ``mt5.symbol_info`` returns ``None`` after the terminal
    has been shut down by the context-manager exit.
    """
    ny = ZoneInfo("America/New_York")
    start_ny = datetime(ny_date.year, ny_date.month, ny_date.day, 14, 0, tzinfo=ny)
    end_ny = datetime(ny_date.year, ny_date.month, ny_date.day, 20, 0, tzinfo=ny)
    start_utc = start_ny.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_ny.astimezone(timezone.utc).replace(tzinfo=None)

    with MT5Connection() as conn:
        raw = conn.copy_rates_range(symbol, mt5.TIMEFRAME_M5, start_utc, end_utc)
        info = conn.symbol_info(symbol)
        if info is None:
            # Symbol may not yet be in Market Watch. Selecting forces it in,
            # which is enough for symbol_info to return a populated record.
            try:
                mt5.symbol_select(symbol, True)
            except Exception:
                pass
            info = conn.symbol_info(symbol)
        pip_size = float(info.point) if info is not None and getattr(info, "point", None) else 1e-5

    if raw is None or len(raw) == 0:
        raise RuntimeError(
            f"No M5 bars for {symbol} CBDR window {ny_date} 14:00–20:00 NY → UTC {start_utc}–{end_utc}"
        )

    df = pd.DataFrame(raw)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    return df[["time", "open", "high", "low", "close"]], pip_size


def cbdr_block_for_london_open(
    symbol: str,
    anchor_utc: datetime,
    *,
    direction_bias: int,
) -> dict[str, Any]:
    """
    CBDR stats + LoD/HoD projection for ``direction_bias`` (from COT regime).

    Returns ``{"ok": False, "error": ...}`` on failure so callers can still return a plan.
    """
    cfg = config.config.get("cbdr", {})
    if not HAS_MT5:
        return {
            "ok": False,
            "skipped": True,
            "reason": "no_mt5_dev_env",
            "reference_ny_date": None,
        }
    try:
        ny_d = _reference_ny_date_for_cbdr(anchor_utc)
        df, pip_size = load_cbdr_m5_dataframe(symbol, ny_d)
        raw = cbdr_mod.cbdr_range(
            df,
            symbol,
            use_wicks=bool(cfg.get("use_wicks", False)),
            ideal_min=float(cfg.get("ideal_min_pips", 20)),
            ideal_max=float(cfg.get("ideal_max_pips", 30)),
            max_allowed=float(cfg.get("max_allowed_pips", 40)),
            pip_size=pip_size,
        )
        proj = cbdr_mod.lod_hod_projection(raw, direction_bias)
        return {
            "ok": True,
            "reference_ny_date": ny_d.isoformat(),
            "cbdr": raw,
            "lod_hod": proj,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "reference_ny_date": None}
