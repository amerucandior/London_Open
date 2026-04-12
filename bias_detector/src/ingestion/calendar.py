"""
src/ingestion/calendar.py
─────────────────────────
Economic calendar ingestion for the FX bias detector.

Primary   : ForexFactory JSON endpoint  (free, no key required)
Secondary : ForexFactory HTML scraper   (BeautifulSoup4 fallback)
Output    : pd.DataFrame + Parquet cache

Validated events: ECB, BoE, CPI, GDP, rate decisions (EUR & GBP only by default)

Dependencies (all in requirements.txt):
    requests, tenacity, pydantic, pandas, beautifulsoup4,
    python-dotenv, structlog
"""

from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal, Optional

import pandas as pd
import requests
import structlog
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator, model_validator
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)
import logging

# ── Environment & logging ─────────────────────────────────────────────────────

load_dotenv()

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer(),
    ]
)
log = structlog.get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# ForexFactory publishes a clean JSON for the current & next week
FF_JSON_URL   = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXT_URL   = "https://nfs.faireconomy.media/ff_calendar_nextweek.json" # Does not work
FF_HTML_URL   = "https://www.forexfactory.com/calendar"

# Currencies we care about
TARGET_CURRENCIES: set[str] = {"EUR", "GBP", "USD"}

# High-priority event keywords (case-insensitive substring match)
HIGH_PRIORITY_KEYWORDS: list[str] = [
    "rate decision", "ecb", "boe", "bank of england", "european central bank",
    "cpi", "inflation", "gdp", "nonfarm", "payroll", "unemployment",
    "pmi", "retail sales", "trade balance",
]

IMPACT_MAP: dict[str, int] = {"Low": 1, "Medium": 2, "High": 3}

OUTPUT_DIR = Path(os.getenv("DATA_DIR", "data/raw/calendar"))

# ── Pydantic models ───────────────────────────────────────────────────────────

class CalendarEvent(BaseModel):
    """Single economic calendar event — validated on ingestion."""

    event_id:   Optional[str] = None
    date:       str                              # ISO date string
    time:       Optional[str] = None             # "13:30" or "All Day"
    currency:   str
    impact:     Literal["Low", "Medium", "High", "Holiday"]
    title:      str
    actual:     Optional[str] = None
    forecast:   Optional[str] = None
    previous:   Optional[str] = None
    surprise_z: Optional[float] = None          # populated post-fetch if data available
    is_high_priority: bool = False

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("title")
    @classmethod
    def non_empty_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Event title must not be empty")
        return v

    @field_validator("date")
    @classmethod
    def valid_date(cls, v: str) -> str:
        # Accept ISO format and common ForexFactory formats
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%m-%d-%Y"):
            try:
                datetime.strptime(v[:10], fmt[:8] if len(fmt) <= 8 else fmt)
                return v
            except ValueError:
                continue
        # Last resort: just check it's non-empty
        if not v:
            raise ValueError(f"Cannot parse date: {v!r}")
        return v

    @model_validator(mode="after")
    def flag_high_priority(self) -> "CalendarEvent":
        title_lower = self.title.lower()
        self.is_high_priority = any(kw in title_lower for kw in HIGH_PRIORITY_KEYWORDS)
        return self

    def surprise_score(self) -> Optional[float]:
        """
        Simple scalar surprise: (actual - forecast).
        Returns None if either value is missing or non-numeric.
        """
        try:
            a = float(re.sub(r"[%KMBk]", "", self.actual or ""))
            f = float(re.sub(r"[%KMBk]", "", self.forecast or ""))
            return round(a - f, 4)
        except (TypeError, ValueError):
            return None

    def to_series(self) -> pd.Series:
        return pd.Series(self.model_dump())


# ── Retry decorator ───────────────────────────────────────────────────────────

_RETRY = retry(
    retry=retry_if_exception_type((requests.RequestException, TimeoutError)),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    before_sleep=before_sleep_log(logging.getLogger(__name__), logging.WARNING),
    reraise=True,
)

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
})


# ── Source 1: ForexFactory JSON ───────────────────────────────────────────────

@_RETRY
def _fetch_ff_json(url: str) -> list[dict]:
    """Fetch ForexFactory JSON calendar. Returns raw list of event dicts."""
    log.info("calendar.ff_json.fetch", url=url)
    resp = _SESSION.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    log.info("calendar.ff_json.ok", count=len(data))
    return data


def _parse_ff_json(raw: list[dict]) -> list[CalendarEvent]:
    """
    Map ForexFactory JSON fields to CalendarEvent.

    FF JSON schema (as of 2024):
    {
      "id":       "...",
      "date":     "2024-10-14T00:00:00-04:00",
      "country":  "EUR",
      "category": "...",
      "name":     "German CPI m/m",
      "impact":   "High",
      "forecast": "0.2%",
      "previous": "0.1%"
    }
    """
    events: list[CalendarEvent] = []
    for raw_event in raw:
        currency = raw_event.get("country", "").upper()
        if currency not in TARGET_CURRENCIES:
            continue
        try:
            impact_raw = raw_event.get("impact", "Low").capitalize()
            # FF sometimes returns "Holiday" as impact
            if impact_raw not in IMPACT_MAP and impact_raw != "Holiday":
                impact_raw = "Low"

            ev = CalendarEvent(
                event_id  = str(raw_event.get("id", "")),
                date      = raw_event.get("date", ""),
                currency  = currency,
                impact    = impact_raw,                   # type: ignore[arg-type]
                title     = raw_event.get("name", "Unknown"),
                forecast  = raw_event.get("forecast"),
                previous  = raw_event.get("previous"),
                actual    = raw_event.get("actual"),
            )
            events.append(ev)
        except Exception as exc:
            log.warning("calendar.ff_json.skip", raw=raw_event, reason=str(exc))

    log.info("calendar.ff_json.parsed", valid=len(events))
    return events


# ── Source 2: ForexFactory HTML scraper (fallback) ────────────────────────────

@_RETRY
def _fetch_ff_html() -> str:
    """Fetch ForexFactory calendar HTML."""
    log.info("calendar.ff_html.fetch", url=FF_HTML_URL)
    resp = _SESSION.get(FF_HTML_URL, timeout=20)
    resp.raise_for_status()
    return resp.text


def _parse_ff_html(html: str) -> list[CalendarEvent]:
    """
    Parse ForexFactory calendar HTML table.
    Structure: <table class="calendar__table"> rows carry date, currency, impact, event name.

    NOTE: FF can change markup without notice — always prefer the JSON source.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_=re.compile(r"calendar", re.I))
    if not table:
        log.warning("calendar.ff_html.no_table")
        return []

    events: list[CalendarEvent] = []
    current_date = ""

    for row in table.find_all("tr", class_=re.compile(r"calendar__row", re.I)):
        # Date cell — persists across rows until the next date
        date_cell = row.find("td", class_=re.compile(r"calendar__date", re.I))
        if date_cell and date_cell.get_text(strip=True):
            raw_date = date_cell.get_text(strip=True)
            # Convert "Oct 14" → ISO with current year
            try:
                parsed = datetime.strptime(
                    f"{raw_date} {datetime.now().year}", "%b %d %Y"
                )
                current_date = parsed.strftime("%Y-%m-%d")
            except ValueError:
                pass  # keep previous date

        if not current_date:
            continue

        # Currency
        currency_cell = row.find("td", class_=re.compile(r"calendar__currency", re.I))
        if not currency_cell:
            continue
        currency = currency_cell.get_text(strip=True).upper()
        if currency not in TARGET_CURRENCIES:
            continue

        # Impact
        impact_cell = row.find("td", class_=re.compile(r"calendar__impact", re.I))
        impact = "Low"
        if impact_cell:
            span = impact_cell.find("span")
            if span:
                cls_str = " ".join(span.get("class", []))
                if "high" in cls_str.lower():
                    impact = "High"
                elif "medium" in cls_str.lower() or "med" in cls_str.lower():
                    impact = "Medium"

        # Title
        title_cell = row.find("td", class_=re.compile(r"calendar__event", re.I))
        if not title_cell:
            continue
        title = title_cell.get_text(strip=True)
        if not title:
            continue

        # Time
        time_cell = row.find("td", class_=re.compile(r"calendar__time", re.I))
        time_str  = time_cell.get_text(strip=True) if time_cell else None

        # Actual / Forecast / Previous
        def _cell_text(cls_fragment: str) -> Optional[str]:
            cell = row.find("td", class_=re.compile(cls_fragment, re.I))
            return cell.get_text(strip=True) or None if cell else None

        try:
            ev = CalendarEvent(
                date     = current_date,
                time     = time_str,
                currency = currency,
                impact   = impact,          # type: ignore[arg-type]
                title    = title,
                actual   = _cell_text("actual"),
                forecast = _cell_text("forecast"),
                previous = _cell_text("previous"),
            )
            events.append(ev)
        except Exception as exc:
            log.warning("calendar.ff_html.skip", title=title, reason=str(exc))

    log.info("calendar.ff_html.parsed", valid=len(events))
    return events


# ── Surprise z-score enrichment ───────────────────────────────────────────────

def _enrich_surprise_z(
    events: list[CalendarEvent],
    history: Optional[pd.DataFrame] = None,
) -> list[CalendarEvent]:
    """
    Compute rolling z-score of surprise = (actual - forecast) / σ_hist.

    If no history DataFrame is supplied, a simple scalar surprise is stored.
    history must have columns: [title, surprise] with sufficient rows.
    """
    for ev in events:
        raw_surprise = ev.surprise_score()
        if raw_surprise is None:
            continue

        if history is not None and ev.title in history["title"].values:
            past = history.loc[history["title"] == ev.title, "surprise"].dropna()
            if len(past) >= 5:
                σ = past.std()
                μ = past.mean()
                ev.surprise_z = round((raw_surprise - μ) / σ, 4) if σ > 0 else 0.0
            else:
                ev.surprise_z = round(raw_surprise, 4)
        else:
            ev.surprise_z = round(raw_surprise, 4)

    return events


# ── DataFrame builder ─────────────────────────────────────────────────────────

def _to_dataframe(events: list[CalendarEvent]) -> pd.DataFrame:
    if not events:
        return pd.DataFrame()

    df = pd.DataFrame([e.model_dump() for e in events])

    # Normalise date column to datetime
    df["datetime"] = pd.to_datetime(df["date"], errors="coerce", utc=True)

    # Numeric impact for downstream scoring
    df["impact_score"] = df["impact"].map(IMPACT_MAP).fillna(0).astype(int)

    # Quality flag: 0 = full data, 1 = missing actual, 2 = missing forecast
    df["quality_flag"] = 0
    df.loc[df["actual"].isna(), "quality_flag"]   = 1
    df.loc[df["forecast"].isna(), "quality_flag"] = 2

    return df.sort_values("datetime").reset_index(drop=True)


# ── Cache layer ───────────────────────────────────────────────────────────────

def _cache_path(label: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return OUTPUT_DIR / f"calendar_{label}_{date_str}.parquet"


def _save_parquet(df: pd.DataFrame, label: str) -> Path:
    path = _cache_path(label)
    df.to_parquet(path, index=False, compression="snappy")
    log.info("calendar.cache.saved", path=str(path), rows=len(df))
    return path


def _load_cached(label: str) -> Optional[pd.DataFrame]:
    """Return today's cached file if it exists, else None."""
    path = _cache_path(label)
    if path.exists():
        df = pd.read_parquet(path)
        log.info("calendar.cache.hit", path=str(path))
        return df
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_calendar(
    weeks: Literal["this", "both"] = "both",
    use_cache:  bool = True,
    currencies: Optional[set[str]] = None,
    min_impact: Literal["Low", "Medium", "High"] = "Low",
    history_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Main entry point — returns a cleaned, validated DataFrame of calendar events.

    Parameters
    ----------
    weeks       : "this" for current week only, "both" for this + next week.
    use_cache   : If True and today's cache exists, return it immediately.
    currencies  : Filter to these currencies (default: EUR, GBP, USD).
    min_impact  : Minimum impact level to include ("Low" includes all).
    history_df  : Historical events DataFrame for z-score calculation.

    Returns
    -------
    pd.DataFrame with columns:
        event_id, date, time, currency, impact, title,
        actual, forecast, previous, surprise_z, is_high_priority,
        datetime, impact_score, quality_flag
    """
    if currencies is None:
        currencies = TARGET_CURRENCIES

    # ── Cache check ───────────────────────────────────────────────────────────
    if use_cache:
        cached = _load_cached("combined")
        if cached is not None:
            return _filter(cached, currencies, min_impact)

    events: list[CalendarEvent] = []

    # ── Primary: JSON source ──────────────────────────────────────────────────
    urls = [FF_JSON_URL]
    if weeks == "both":
        urls.append(FF_NEXT_URL)

    json_ok = False
    for url in urls:
        try:
            raw = _fetch_ff_json(url)
            parsed = _parse_ff_json(raw)
            events.extend(parsed)
            json_ok = True
        except Exception as exc:
            log.error("calendar.ff_json.failed", url=url, error=str(exc))

    # ── Fallback: HTML scraper ────────────────────────────────────────────────
    if not json_ok or len(events) == 0:
        log.warning("calendar.fallback.html_scraper")
        try:
            html = _fetch_ff_html()
            events = _parse_ff_html(html)
        except Exception as exc:
            log.error("calendar.ff_html.failed", error=str(exc))
            # Return empty DataFrame rather than crashing the pipeline
            return pd.DataFrame()

    # ── Deduplicate by (date + title + currency) ──────────────────────────────
    seen: set[tuple] = set()
    unique: list[CalendarEvent] = []
    for ev in events:
        key = (ev.date[:10], ev.title.lower(), ev.currency)
        if key not in seen:
            seen.add(key)
            unique.append(ev)

    log.info("calendar.dedup", before=len(events), after=len(unique))

    # ── Surprise z-score enrichment ───────────────────────────────────────────
    unique = _enrich_surprise_z(unique, history_df)

    # ── Build DataFrame ───────────────────────────────────────────────────────
    df = _to_dataframe(unique)
    if df.empty:
        log.warning("calendar.empty_result")
        return df

    # ── Cache ─────────────────────────────────────────────────────────────────
    _save_parquet(df, "combined")

    return _filter(df, currencies, min_impact)


def _filter(
    df: pd.DataFrame,
    currencies: set[str],
    min_impact: str,
) -> pd.DataFrame:
    """Apply currency and impact filters."""
    min_score = IMPACT_MAP.get(min_impact, 1)
    mask = df["currency"].isin(currencies) & (df["impact_score"] >= min_score)
    return df[mask].reset_index(drop=True)


def fetch_upcoming_events(
    hours_ahead: int = 48,
    min_impact: Literal["Low", "Medium", "High"] = "Medium",
) -> pd.DataFrame:
    """
    Convenience wrapper: return events within the next N hours.
    Used by the Event Risk Score (F5) feature calculator.
    """
    df = fetch_calendar(min_impact=min_impact)
    if df.empty:
        return df

    now  = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)

    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    upcoming = df[(df["datetime"] >= now) & (df["datetime"] <= cutoff)]

    log.info(
        "calendar.upcoming",
        hours_ahead=hours_ahead,
        count=len(upcoming),
    )
    return upcoming.reset_index(drop=True)


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 140)

    print("\n── Full calendar (this + next week) ─────────────────────────────")
    cal = fetch_calendar(weeks="both", use_cache=False)
    print(cal[["datetime", "currency", "impact", "title", "forecast", "actual",
               "surprise_z", "is_high_priority"]].to_string(index=False))
    print(f"\nTotal events: {len(cal)}")

    print("\n── Upcoming 48h (Medium+ impact) ────────────────────────────────")
    upcoming = fetch_upcoming_events(hours_ahead=48, min_impact="Medium")
    if not upcoming.empty:
        print(upcoming[["datetime", "currency", "impact", "title"]].to_string(index=False))
    else:
        print("No upcoming events found (or all future-dated).")

    sys.exit(0)