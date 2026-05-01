"""
SQLite-backed state for the live runtime.

Tracks:
- ``daily_flags`` — one-shot flags (e.g. ``open_breakout_taken``) keyed by (date, pair).
- ``positions``   — strategy-tagged open positions (MT5 ticket + bracket).
- ``pnl_log``     — realised P&L per day, for the kill switch.
- ``vwap_state``  — rolling session VWAP accumulators (since session start).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_flags (
    day  TEXT NOT NULL,
    pair TEXT NOT NULL,
    flag TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (day, pair, flag)
);
CREATE TABLE IF NOT EXISTS positions (
    ticket      INTEGER PRIMARY KEY,
    pair        TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    side        TEXT NOT NULL,
    lots        REAL NOT NULL,
    entry       REAL NOT NULL,
    sl          REAL,
    tp          REAL,
    opened_utc  TEXT NOT NULL,
    closed_utc  TEXT,
    pnl         REAL
);
CREATE TABLE IF NOT EXISTS pnl_log (
    day      TEXT PRIMARY KEY,
    realised REAL NOT NULL DEFAULT 0.0
);
CREATE TABLE IF NOT EXISTS vwap_state (
    day  TEXT NOT NULL,
    pair TEXT NOT NULL,
    pv   REAL NOT NULL,
    vol  REAL NOT NULL,
    PRIMARY KEY (day, pair)
);
"""


@dataclass
class StoredPosition:
    ticket: int
    pair: str
    strategy: str
    side: str          # "LONG" / "SHORT"
    lots: float
    entry: float
    sl: Optional[float]
    tp: Optional[float]
    opened_utc: str


class StateStore:
    def __init__(self, db_path: str = "runtime_state.sqlite") -> None:
        self.path = Path(db_path)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ flags
    def set_flag(self, day: date, pair: str, flag: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO daily_flags (day, pair, flag, value) VALUES (?,?,?,1)",
                (day.isoformat(), pair.upper(), flag),
            )

    def has_flag(self, day: date, pair: str, flag: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM daily_flags WHERE day=? AND pair=? AND flag=?",
                (day.isoformat(), pair.upper(), flag),
            ).fetchone()
        return bool(row and row["value"])

    # -------------------------------------------------------------- positions
    def add_position(self, pos: StoredPosition) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO positions
                   (ticket, pair, strategy, side, lots, entry, sl, tp, opened_utc)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (pos.ticket, pos.pair.upper(), pos.strategy, pos.side,
                 pos.lots, pos.entry, pos.sl, pos.tp, pos.opened_utc),
            )

    def close_position(self, ticket: int, closed_utc: str, pnl: float) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE positions SET closed_utc=?, pnl=? WHERE ticket=?",
                (closed_utc, pnl, ticket),
            )
        self._add_pnl(date.today(), pnl)

    def open_positions(self, pair: Optional[str] = None) -> List[StoredPosition]:
        with self._conn() as c:
            if pair:
                rows = c.execute(
                    "SELECT * FROM positions WHERE closed_utc IS NULL AND pair=?",
                    (pair.upper(),),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM positions WHERE closed_utc IS NULL"
                ).fetchall()
        return [
            StoredPosition(
                ticket=r["ticket"], pair=r["pair"], strategy=r["strategy"],
                side=r["side"], lots=r["lots"], entry=r["entry"],
                sl=r["sl"], tp=r["tp"], opened_utc=r["opened_utc"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------- P&L
    def _add_pnl(self, day: date, amount: float) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO pnl_log (day, realised) VALUES (?,?)
                   ON CONFLICT(day) DO UPDATE SET realised = realised + excluded.realised""",
                (day.isoformat(), amount),
            )

    def daily_pnl(self, day: date) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT realised FROM pnl_log WHERE day=?", (day.isoformat(),)
            ).fetchone()
        return float(row["realised"]) if row else 0.0

    # ------------------------------------------------------------------ VWAP
    def update_vwap(self, day: date, pair: str, pv_delta: float, vol_delta: float) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO vwap_state (day, pair, pv, vol) VALUES (?,?,?,?)
                   ON CONFLICT(day, pair) DO UPDATE
                   SET pv  = pv  + excluded.pv,
                       vol = vol + excluded.vol""",
                (day.isoformat(), pair.upper(), pv_delta, vol_delta),
            )

    def get_vwap(self, day: date, pair: str) -> Optional[float]:
        with self._conn() as c:
            row = c.execute(
                "SELECT pv, vol FROM vwap_state WHERE day=? AND pair=?",
                (day.isoformat(), pair.upper()),
            ).fetchone()
        if not row or row["vol"] == 0:
            return None
        return row["pv"] / row["vol"]