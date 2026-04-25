"""
src/ingestion/calendar.py
─────────────────────────────────────────────────────────
Economic calendar ingestion for the FX bias detector.

Data sources
────────────
  Primary   : ForexFactory JSON endpoint (free, no key required)
              https://nfs.faireconomy.media/ff_calendar_thisweek.json
              (This feed is “this week” only — forward risk past week boundary needs
              another source or rolling cache; see ``fetch_calendar`` docstring.)

  Secondary : FRED API (St. Louis Fed) — latest published series observations
              https://fred.stlouisfed.org/docs/api/fred/
              Required: FRED_API_KEY in .env
              FRED values are **context only**: they are not the same object as the
              calendar line’s release-time print and are **never** used for
              ``surprise_score`` / ``surprise_z`` (those use ForexFactory strings only).

Output     : pd.DataFrame + Parquet cache

Validated events: ECB, BoE, Fed, CPI, GDP, rate decisions (EUR, GBP, USD by default)
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Literal, Optional

import pandas as pd
import requests
import structlog
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator, model_validator
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

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

FF_JSON_THIS_WEEK: Final[str] = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

FRED_BASE_URL: Final[str] = "https://api.stlouisfed.org/fred"

# Currencies filtered from the FF feed
TARGET_CURRENCIES: Final[set[str]] = {"All", "EUR", "GBP", "USD"}

# High-priority event keywords (case-insensitive substring match)
HIGH_PRIORITY_KEYWORDS: Final[list[str]] = [
    "rate decision", "ecb", "boe", "bank of england", "european central bank",
    "fomc", "federal reserve", "fed", "cpi", "inflation", "gdp", 
    "nonfarm", "payroll", "unemployment", "pmi", "retail sales", 
    "trade balance", "pce", "nfp", "employment", "ppi",
]

IMPACT_MAP: Final[dict[str, int]] = {"Low": 1, "Medium": 2, "High": 3}

# FRED series that map to FF event titles (extend as needed)
FRED_SERIES: Final[dict[str, tuple[str, str]]] = {
    # US indicators
    "nonfarm":          ("PAYEMS",    "US Non-Farm Payrolls (k)"),
    "unemployment claims": ("ICSA",  "US Initial Jobless Claims"),
    "unemployment":     ("UNRATE",    "US Unemployment Rate (%)"),
    "cpi":              ("CPIAUCSL",  "US CPI All Urban (index)"),
    "core cpi":         ("CPILFESL",  "US Core CPI ex Food & Energy (index)"),
    "core ppi":         ("PPIACO",    "US Core PPI (index)"),
    "ppi":              ("PPIACO",    "US PPI All Commodities (index)"),
    "gdp":              ("GDP",       "US Real GDP (billions $)"),
    "fed funds":        ("FEDFUNDS",  "US Fed Funds Rate (%)"),
    "retail sales":     ("RSXFS",     "US Retail Sales ex Autos (millions $)"),
    "trade balance":    ("BOPGSTB",   "US Trade Balance (millions $)"),
    "industrial production": ("INDPRO", "US Industrial Production (index)"),
    "pce":              ("PCEPI",     "US PCE Price Index (index)"),
    "empire state":     ("GACDIFNS066MNFRBNY", "NY Fed Empire State Index"),
    "philly fed":       ("USPHCI",    "Philadelphia Fed Index"),
    "nahb":             ("HNOUTHANR", "NAHB Housing Market Index"),
    "existing home":    ("EXHOSLUSM495S", "Existing Home Sales"),
    "housing starts":   ("HOUST",     "Housing Starts"),
    "building permits": ("PERMIT",    "Building Permits"),
    "10-year":          ("DGS10",     "US 10-Year Treasury Yield (%)"),
    "2-year":           ("DGS2",      "US 2-Year Treasury Yield (%)"),
    "import prices":    ("IR",        "US Import Price Index"),
    
    # Euro Area indicators
    "ecb":              ("ECBDFR",     "ECB Deposit Facility Rate (%)"),
    "german":           ("DEUCPI01STM", "German CPI"),
    "french":           ("FRACPI03TQM", "French CPI"),
    "italian":          ("ITACPI03TQM", "Italian CPI"),
    "euro area":        ("CP0000EZ17M086NEST", "Euro Area CPI Harmonised (index)"),
    
    # UK indicators
    "boe":              ("IUDSOIA",    "UK SONIA Rate (%)"),
    "uk gdp":           ("NGDPMXGB",   "UK Real GDP"),
    "manufacturing production": ("MANMM101UKM189S", "UK Manufacturing Production"),
    
    # Exchange rates
    "eur/":             ("DEXUSEU",   "USD/EUR Exchange Rate"),
    "gbp/":             ("DEXUSGB",   "USD/GBP Exchange Rate"),
}

OUTPUT_DIR: Final[Path] = Path(os.getenv("DATA_DIR", "data/raw/calendar"))

# Rate limiting configuration
MIN_REQUEST_INTERVAL: Final[int] = 60  # Minimum seconds between FF requests
CACHE_DURATION_HOURS: Final[int] = 6   # Cache duration in hours

# Bumped when combined-calendar semantics change (e.g. FRED enrichment) so stale
# Parquet is not served forever.
CAL_COMBINED_CACHE_VERSION: Final[int] = 2
_CAL_COMBINED_V_COL: Final[str] = "_cal_combined_v"

# ``time`` field tokens → treat as whole-day bucket (UTC midnight on ``date``’s YYYY-MM-DD).
# Do **not** include empty string — missing ``time`` means “use clock from ``date``”.
_ALL_DAY_TOKENS: Final[frozenset[str]] = frozenset(
    {"all day", "allday", "tentative", "tbc", "tba"}
)


def parse_economic_string_to_float(value: Optional[str]) -> Optional[float]:
    """
    Parse ForexFactory-style numeric strings for surprise math only.

    Handles commas, trailing %, and K/M/B suffix multipliers (e.g. ``178K``, ``1.2M``).
    Returns ``None`` for placeholders and non-numeric prose.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    lower = raw.lower()
    if lower in {"n/a", "na", "-", "–", "—", "tba", "tbd", "...", "tbc", "tentative"}:
        return None

    pct = raw.endswith("%")
    s = raw[:-1] if pct else raw
    s = s.replace(",", "").replace(" ", "")

    mult = 1.0
    if len(s) >= 2 and s[-1].upper() in "KMB" and (s[-2].isdigit() or s[-2] == "."):
        suf = s[-1].upper()
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[suf]
        s = s[:-1]

    s = re.sub(r"[^\d.\-+]", "", s)
    if not s or s in {".", "+", "-", "+.", "-."}:
        return None
    try:
        v = float(s) * mult
        return v / 100.0 if pct else v
    except ValueError:
        return None


def _fred_series_match(title_lower: str) -> Optional[tuple[str, str]]:
    """
    Map event title → FRED ``(series_id, label)`` using **longest** keyword match.

    Longest substring wins, then lexicographic keyword tie-break for determinism.
    This avoids ``"unemployment"`` stealing events that belong to
    ``"unemployment claims"`` when dict iteration order was arbitrary.
    """
    best_rank: Optional[tuple[int, str]] = None  # (len(keyword), keyword) — max wins
    best_pair: Optional[tuple[str, str]] = None

    for keyword, pair in FRED_SERIES.items():
        if keyword not in title_lower:
            continue
        rank = (len(keyword), keyword)
        if best_rank is None or rank > best_rank:
            best_rank, best_pair = rank, pair
    return best_pair


def _parse_event_to_utc_timestamp(date: str, time: Optional[str]) -> pd.Timestamp:
    """
    Normalise FF ``date`` (+ optional ``time``) to a UTC pandas timestamp.

    - Full ISO in ``date`` → parsed in UTC (or with offset, then converted).
    - ``time`` in ``_ALL_DAY_TOKENS`` → ``YYYY-MM-DD`` from ``date`` at 00:00 UTC.
    - If ``date`` has no time and ``time`` is a clock like ``13:30``, combine as UTC
      (FF is ambiguous here — prefer full ISO in ``date`` when available).
    """
    d = (date or "").strip()
    tm_raw = (time or "").strip()
    tm = tm_raw.lower()

    if not d:
        return pd.NaT  # type: ignore[return-value]

    if tm in _ALL_DAY_TOKENS:
        return pd.to_datetime(d[:10] + "T00:00:00", utc=True, errors="coerce")

    ts = pd.to_datetime(d, utc=True, errors="coerce")
    if not pd.isna(ts):
        return ts

    if tm_raw:
        combined = f"{d[:10]}T{tm_raw}"
        return pd.to_datetime(combined, utc=True, errors="coerce")

    return pd.to_datetime(d[:10] + "T00:00:00", utc=True, errors="coerce")


def _validate_calendar_date_string(v: str) -> bool:
    """True if ``v`` yields a non-NaT event timestamp (possibly day-bucket)."""
    return not pd.isna(_parse_event_to_utc_timestamp(v, None))


def _combined_cache_usable(cached: pd.DataFrame) -> bool:
    """
    Reject old ``calendar_combined_*.parquet`` files that predate schema tagging,
    so FRED enrichment is not skipped forever after a one-time bad save.
    """
    if cached is None or cached.empty:
        return True
    if _CAL_COMBINED_V_COL not in cached.columns:
        log.info("calendar.cache.reject", reason="missing_schema_version")
        return False
    try:
        v = int(cached[_CAL_COMBINED_V_COL].iloc[0])
    except (TypeError, ValueError):
        log.info("calendar.cache.reject", reason="invalid_schema_version")
        return False
    if v < CAL_COMBINED_CACHE_VERSION:
        log.info(
            "calendar.cache.reject",
            reason="stale_schema_version",
            cached_version=v,
            required_version=CAL_COMBINED_CACHE_VERSION,
        )
        return False
    return True


def _tag_combined_cache_version(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out[_CAL_COMBINED_V_COL] = CAL_COMBINED_CACHE_VERSION
    return out


def _nonblank_economic_field(value: Optional[str]) -> bool:
    if value is None:
        return False
    t = str(value).strip()
    if not t:
        return False
    return t.lower() not in {
        "n/a", "na", "-", "–", "—", "tba", "tbd", "...", "tbc", "tentative",
    }


def _calendar_row_quality_flag(actual: Optional[str], forecast: Optional[str]) -> int:
    """
    Data-quality code for calendar rows (extends NaN checks with parseability).

    0 = both actual & forecast parse as numbers (post-release surprise usable).
    1 = pre-release / no actual (forecast may still be present).
    2 = missing or blank forecast.
    3 = forecast present but not parseable as a number.
    4 = actual present but not parseable as a number.
    """
    has_f = _nonblank_economic_field(forecast)
    has_a = _nonblank_economic_field(actual)
    if not has_f:
        return 2
    if not has_a:
        return 1
    if parse_economic_string_to_float(forecast) is None:
        return 3
    if parse_economic_string_to_float(actual) is None:
        return 4
    return 0


# ── Pydantic models ───────────────────────────────────────────────────────────

class CalendarEvent(BaseModel):
    """Single economic calendar event — validated on ingestion."""

    event_id:         Optional[str]   = None
    date:             str                              # ISO date string
    time:             Optional[str]   = None           # "13:30" or "All Day"
    currency:         str
    impact:           Literal["Low", "Medium", "High", "Holiday"]
    title:            str
    actual:           Optional[str]   = None
    forecast:         Optional[str]   = None
    previous:         Optional[str]   = None
    fred_series_id:   Optional[str]   = None           # matched FRED series (longest keyword)
    # Latest / prior FRED *series* observations — not tied to this event’s release instant;
    # never mixed into ``surprise_score`` (FF actual vs forecast only).
    fred_actual:      Optional[float] = None
    fred_previous:    Optional[float] = None
    surprise_z:       Optional[float] = None           # from FF surprise only (see surprise_score)
    is_high_priority: bool            = False
    source:           str             = "forexfactory"  # Track data source

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
        """Must yield a usable event timestamp when combined with FF ``date`` formats."""
        v = str(v).strip()
        if not v:
            raise ValueError("Cannot parse empty date")
        if not _validate_calendar_date_string(v):
            raise ValueError(f"Cannot parse date: {v!r}")
        return v

    @model_validator(mode="after")
    def flag_high_priority(self) -> "CalendarEvent":
        title_lower = self.title.lower()
        self.is_high_priority = any(kw in title_lower for kw in HIGH_PRIORITY_KEYWORDS)
        return self

    def surprise_score(self) -> Optional[float]:
        """
        Scalar surprise: **ForexFactory** ``actual`` minus ``forecast`` only.

        FRED fields are ignored — their latest observation is not the same object
        as the number released at this event’s timestamp.
        """
        a = parse_economic_string_to_float(self.actual)
        f = parse_economic_string_to_float(self.forecast)
        if a is None or f is None:
            return None
        return round(a - f, 4)

    def to_series(self) -> pd.Series:
        return pd.Series(self.model_dump())


# ── HTTP session & retry decorator ───────────────────────────────────────────

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
})

_RETRY = retry(
    retry=retry_if_exception_type((requests.RequestException, TimeoutError)),
    stop=stop_after_attempt(3),  # Reduced from 5 to avoid prolonged waits
    wait=wait_exponential(multiplier=2, min=2, max=30),  # Reduced max from 60
    before_sleep=before_sleep_log(
        logging.getLogger(__name__), logging.WARNING
    ),
    reraise=True,
)


# ── Source 1: ForexFactory JSON ───────────────────────────────────────────────

def _get_cache_file_path() -> Path:
    """Get the path for the ForexFactory cache file."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / "ff_calendar_cache.json"


def _is_cache_valid() -> bool:
    """Check if the cache file exists and is recent."""
    cache_file = _get_cache_file_path()
    if not cache_file.exists():
        return False
    
    # Check file age
    file_mtime = datetime.fromtimestamp(cache_file.stat().st_mtime, tz=timezone.utc)
    age = datetime.now(timezone.utc) - file_mtime
    
    return age.total_seconds() < (CACHE_DURATION_HOURS * 3600)


def _load_ff_cache() -> Optional[list[dict]]:
    """Load cached ForexFactory data if valid."""
    cache_file = _get_cache_file_path()
    
    if _is_cache_valid():
        try:
            import json
            with open(cache_file, 'r') as f:
                data = json.load(f)
                log.info("ff_cache.hit", age_hours=round((datetime.now(timezone.utc) - 
                     datetime.fromtimestamp(cache_file.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600, 1))
                return data
        except Exception as exc:
            log.warning("ff_cache.load_failed", error=str(exc))
    
    return None


def _save_ff_cache(data: list[dict]) -> None:
    """Save ForexFactory data to cache."""
    cache_file = _get_cache_file_path()
    try:
        import json
        with open(cache_file, 'w') as f:
            json.dump(data, f)
        log.info("ff_cache.saved", count=len(data))
    except Exception as exc:
        log.warning("ff_cache.save_failed", error=str(exc))


@_RETRY
def _fetch_ff_json(url: str, *, bust_json_cache: bool = False) -> list[dict]:
    """
    Fetch the ForexFactory weekly calendar JSON with rate limiting.
    
    Parameters
    ----------
    url : FF_JSON_THIS_WEEK
    bust_json_cache : If True, skip the on-disk FF JSON cache and re-download.

    Returns
    -------
    List of raw event dicts from the feed.
    """
    if not bust_json_cache:
        cached_data = _load_ff_cache()
        if cached_data is not None:
            return cached_data
    
    # Rate limiting - add delay between requests
    last_request_file = OUTPUT_DIR / ".ff_last_request"
    if last_request_file.exists():
        try:
            last_time = float(last_request_file.read_text().strip())
            elapsed = time.time() - last_time
            if elapsed < MIN_REQUEST_INTERVAL:
                sleep_time = MIN_REQUEST_INTERVAL - elapsed
                log.info("ff.rate_limiting", sleep_seconds=round(sleep_time, 1))
                time.sleep(sleep_time)
        except Exception:
            pass
    
    log.info("calendar.ff_json.fetch", url=url)
    
    try:
        resp = _SESSION.get(url, timeout=20)
        
        # Handle rate limiting
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", MIN_REQUEST_INTERVAL))
            log.warning("ff.rate_limited", retry_after=retry_after)
            raise requests.RequestException(f"Rate limited. Retry after {retry_after}s")
        
        resp.raise_for_status()
        data: list[dict] = resp.json()
        
        # Save successful request timestamp
        last_request_file.write_text(str(time.time()))
        
        # Cache the data
        _save_ff_cache(data)
        
        log.info("calendar.ff_json.ok", count=len(data))
        return data
        
    except requests.RequestException as exc:
        log.error("calendar.ff_json.failed", error=str(exc))
        raise


def _parse_ff_json(raw: list[dict]) -> list[CalendarEvent]:
    """
    Map ForexFactory JSON fields to CalendarEvent objects.
    """
    events: list[CalendarEvent] = []

    for raw_event in raw:
        currency = raw_event.get("country", "").upper()
        if currency not in TARGET_CURRENCIES:
            continue

        try:
            impact_raw = raw_event.get("impact", "Low").capitalize()
            if impact_raw not in (*IMPACT_MAP, "Holiday"):
                impact_raw = "Low"

            time_raw = raw_event.get("time")
            time_clean: Optional[str] = None
            if time_raw is not None:
                ts = str(time_raw).strip()
                time_clean = ts if ts else None

            ev = CalendarEvent(
                event_id = f"FF_{raw_event.get('date', '')}_{raw_event.get('title', '')}",
                date     = raw_event.get("date", ""),
                time     = time_clean,
                currency = currency,
                impact   = impact_raw,
                title    = raw_event.get("title", "Unknown"),
                forecast = raw_event.get("forecast") or None,
                previous = raw_event.get("previous") or None,
                actual   = raw_event.get("actual") or None,
                source   = "forexfactory",
            )
            events.append(ev)

        except Exception as exc:
            log.warning("calendar.ff_json.skip", raw=raw_event, reason=str(exc))

    log.info("calendar.ff_json.parsed", valid=len(events))
    return events


# ── Source 2: FRED API ────────────────────────────────────────────────────────

class FREDClient:
    """
    Thin wrapper around the FRED REST API.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or os.getenv("FRED_API_KEY")
        if not self._api_key:
            log.warning(
                "fred.no_api_key",
                hint="Set FRED_API_KEY in .env — get one free at "
                     "https://fred.stlouisfed.org/docs/api/api_key.html",
            )

    @property
    def available(self) -> bool:
        """True if an API key is configured."""
        return bool(self._api_key)

    @_RETRY
    def get_observations(
        self,
        series_id: str,
        limit: int = 36,
        sort_order: Literal["asc", "desc"] = "desc",
    ) -> pd.DataFrame:
        """
        Fetch time-series observations for a FRED series.
        """
        if not self.available:
            log.error("fred.get_observations.no_key", series_id=series_id)
            return pd.DataFrame()

        params: dict = {
            "series_id":  series_id,
            "api_key":    self._api_key,
            "file_type":  "json",
            "sort_order": sort_order,
            "limit":      limit,
        }

        try:
            log.info("fred.fetch", series_id=series_id, limit=limit)
            resp = _SESSION.get(
                f"{FRED_BASE_URL}/series/observations",
                params=params,
                timeout=20,
            )

            if resp.status_code == 429:
                log.warning("fred.rate_limited", series_id=series_id)
                raise requests.RequestException("FRED rate limit hit (429)")

            if resp.status_code == 400:
                log.error(
                    "fred.bad_series_id",
                    series_id=series_id,
                    response=resp.text[:200],
                )
                return pd.DataFrame()

            resp.raise_for_status()
            observations = resp.json().get("observations", [])

        except requests.RequestException as exc:
            log.error("fred.fetch_failed", series_id=series_id, error=str(exc))
            return pd.DataFrame()

        if not observations:
            log.warning("fred.empty_response", series_id=series_id)
            return pd.DataFrame()

        df = pd.DataFrame(observations)[["date", "value"]]
        df["value"]    = pd.to_numeric(df["value"], errors="coerce")
        df["date"]     = pd.to_datetime(df["date"], errors="coerce")
        df["series_id"] = series_id
        df = df.dropna(subset=["value"]).reset_index(drop=True)

        log.info("fred.ok", series_id=series_id, rows=len(df))
        return df

    def get_latest_values(
        self, 
        series_id: str, 
        n: int = 2
    ) -> tuple[Optional[float], Optional[float]]:
        """
        Return the latest and previous non-null observation for a series.
        """
        df = self.get_observations(series_id, limit=n, sort_order="desc")
        if df.empty:
            return None, None
        
        latest = float(df.iloc[0]["value"]) if len(df) > 0 else None
        previous = float(df.iloc[1]["value"]) if len(df) > 1 else None
        return latest, previous


# ── FRED enrichment pass ──────────────────────────────────────────────────────

def _enrich_with_fred(
    events: list[CalendarEvent],
    fred_client: FREDClient,
) -> list[CalendarEvent]:
    """
    Attach latest FRED series observations for **research context** only.

    Matching uses the **longest** title keyword in ``FRED_SERIES`` to reduce
    wrong-series attachment. Values are still not release-synchronised with the
    FF row — callers must not treat ``fred_actual`` as the event’s “actual”.
    """
    if not fred_client.available:
        log.info("fred.enrich.skipped", reason="no API key")
        return _enrich_surprise_z_simple(events)

    # Pre-fetch all series we might need
    fred_cache: dict[str, tuple[Optional[float], Optional[float]]] = {}

    for ev in events:
        title_lower = ev.title.lower()
        matched = _fred_series_match(title_lower)
        if matched is None:
            continue

        series_id, _label = matched
        ev.fred_series_id = series_id

        if series_id not in fred_cache:
            fred_cache[series_id] = fred_client.get_latest_values(series_id, n=2)

        latest, previous = fred_cache.get(series_id, (None, None))
        ev.fred_actual = latest
        ev.fred_previous = previous

    return _enrich_surprise_z_simple(events)


def _enrich_surprise_z_simple(
    events: list[CalendarEvent],
    history: Optional[pd.DataFrame] = None,
) -> list[CalendarEvent]:
    """Compute surprise_z for each event."""
    for ev in events:
        raw = ev.surprise_score()
        if raw is None:
            continue

        if history is not None and ev.title in history["title"].values:
            past = history.loc[history["title"] == ev.title, "surprise"].dropna()
            if len(past) >= 5:
                sigma = past.std()
                mu    = past.mean()
                ev.surprise_z = round((raw - mu) / sigma, 4) if sigma > 0 else 0.0
            else:
                ev.surprise_z = round(raw, 4)
        else:
            ev.surprise_z = round(raw, 4)

    return events


# ── DataFrame builder ─────────────────────────────────────────────────────────

def _to_dataframe(events: list[CalendarEvent]) -> pd.DataFrame:
    """Convert a list of CalendarEvent objects to a normalised DataFrame."""
    if not events:
        return pd.DataFrame()

    df = pd.DataFrame([e.model_dump() for e in events])
    df["datetime"] = [
        _parse_event_to_utc_timestamp(str(ev.date), ev.time) for ev in events
    ]
    df["impact_score"] = df["impact"].map(IMPACT_MAP).fillna(0).astype(int)

    df["quality_flag"] = [
        _calendar_row_quality_flag(ev.actual, ev.forecast) for ev in events
    ]

    return df.sort_values("datetime").reset_index(drop=True)


# ── Cache layer ───────────────────────────────────────────────────────────────

def _cache_path(label: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return OUTPUT_DIR / f"calendar_{label}_{date_str}.parquet"


def _save_parquet(df: pd.DataFrame, label: str) -> Path:
    """Persist DataFrame to a dated Parquet file and return its path."""
    path = _cache_path(label)
    df.to_parquet(path, index=False, compression="snappy")
    log.info("calendar.cache.saved", path=str(path), rows=len(df))
    return path


def _load_cached(label: str) -> Optional[pd.DataFrame]:
    """Return today's cached Parquet file if it exists, otherwise None."""
    path = _cache_path(label)
    if path.exists():
        df = pd.read_parquet(path)
        log.info("calendar.cache.hit", path=str(path))
        return df
    return None


# ── Filtering helper ──────────────────────────────────────────────────────────

def _filter(
    df: pd.DataFrame,
    currencies: set[str],
    min_impact: str,
) -> pd.DataFrame:
    """Apply currency and minimum impact score filters."""
    min_score = IMPACT_MAP.get(min_impact, 1)
    mask = df["currency"].isin(currencies) & (df["impact_score"] >= min_score)
    return df[mask].reset_index(drop=True)


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_calendar(
    use_cache:   bool = True,
    currencies:  Optional[set[str]] = None,
    min_impact:  Literal["Low", "Medium", "High"] = "Low",
    history_df:  Optional[pd.DataFrame] = None,
    fred_client: Optional[FREDClient] = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Main entry point — returns a cleaned, validated DataFrame of calendar events.

    **Feed coverage:** the primary JSON URL is ForexFactory’s *this week* list only.
    There is no second weekly endpoint in this module; retain a rolling Parquet
    history or plug a paid calendar if you need clean forward coverage across
    week boundaries (e.g. Sunday night vs Monday London).

    **Surprise / FRED:** ``surprise_z`` uses ``CalendarEvent.surprise_score()``,
    which is **ForexFactory actual minus forecast only**. FRED columns are
    informational (latest series observation), not the release-time print.

    Parameters
    ----------
    use_cache      : Return today's Parquet cache if available.
    currencies     : Currency filter (default: EUR, GBP, USD, All).
    min_impact     : Minimum impact level ("Low" includes everything).
    history_df     : Historical events DataFrame for z-score normalisation.
    fred_client    : Pre-initialised FREDClient (creates one if not supplied).
    force_refresh  : If True, skip the combined Parquet cache for today and skip
                     the FF JSON disk cache so events are re-parsed and FRED
                     enrichment runs again.

    Returns
    -------
    pd.DataFrame with calendar events, including:

    - ``datetime`` — UTC timestamp from ``date`` + optional ``time`` (``All Day``
      rows bucket to 00:00 UTC on the ``YYYY-MM-DD`` prefix of ``date``).
    - ``quality_flag`` — ``0`` both sides parse for surprise; ``1`` missing
      actual; ``2`` missing forecast; ``3`` / ``4`` unparsable forecast / actual.
    """
    if currencies is None:
        currencies = TARGET_CURRENCIES

    # Check processed cache first (must carry current schema version)
    if use_cache and not force_refresh:
        cached = _load_cached("combined")
        if cached is not None and _combined_cache_usable(cached):
            df_cached = cached.drop(columns=[_CAL_COMBINED_V_COL], errors="ignore")
            return _filter(df_cached, currencies, min_impact)
        if cached is not None and not _combined_cache_usable(cached):
            log.info("calendar.cache.skip_stale_combined")

    # Fetch FF JSON
    events: list[CalendarEvent] = []
    
    try:
        raw = _fetch_ff_json(FF_JSON_THIS_WEEK, bust_json_cache=force_refresh)
        parsed = _parse_ff_json(raw)
        events.extend(parsed)
    except Exception as exc:
        log.error("calendar.ff_json.failed", error=str(exc))
        
        # Try to load from processed cache as fallback
        if use_cache:
            cached = _load_cached("combined")
            if cached is not None:
                log.info("calendar.using_fallback_cache")
                cc = cached.drop(columns=[_CAL_COMBINED_V_COL], errors="ignore")
                return _filter(cc, currencies, min_impact)
        
        # Return empty DataFrame if all fails
        log.warning("calendar.empty_result")
        return pd.DataFrame()

    if not events:
        log.warning("calendar.empty_result")
        return pd.DataFrame()

    # Deduplicate
    seen:   set[tuple]          = set()
    unique: list[CalendarEvent] = []
    for ev in events:
        key = (ev.date[:10], ev.title.lower(), ev.currency)
        if key not in seen:
            seen.add(key)
            unique.append(ev)

    log.info("calendar.dedup", before=len(events), after=len(unique))

    # FRED enrichment
    client = fred_client or FREDClient()
    unique = _enrich_with_fred(unique, client)
    n_fred = sum(1 for ev in unique if ev.fred_series_id)
    log.info("calendar.fred.enriched_rows", count=n_fred, total=len(unique))

    # Surprise z-scores
    if history_df is not None:
        unique = _enrich_surprise_z_simple(unique, history_df)

    # Build DataFrame
    df = _to_dataframe(unique)
    if df.empty:
        log.warning("calendar.empty_result")
        return df

    df_versioned = _tag_combined_cache_version(df)
    _save_parquet(df_versioned, "combined")
    df_out = df_versioned.drop(columns=[_CAL_COMBINED_V_COL], errors="ignore")
    return _filter(df_out, currencies, min_impact)


def fetch_upcoming_events(
    hours_ahead:  int = 48,
    min_impact:   Literal["Low", "Medium", "High"] = "Medium",
    fred_client:  Optional[FREDClient] = None,
) -> pd.DataFrame:
    """
    Convenience wrapper — return events within the next N hours.
    """
    df = fetch_calendar(min_impact=min_impact, fred_client=fred_client)
    if df.empty:
        return df

    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)

    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    upcoming = df[(df["datetime"] >= now) & (df["datetime"] <= cutoff)]

    log.info("calendar.upcoming", hours_ahead=hours_ahead, count=len(upcoming))
    return upcoming.reset_index(drop=True)


def fetch_fred_series_bulk(
    series_ids: Optional[list[str]] = None,
    limit:      int = 24,
) -> dict[str, pd.DataFrame]:
    """
    Fetch multiple FRED series in one call and return a dict of DataFrames.
    """
    client = FREDClient()
    if not client.available:
        log.error("fetch_fred_series_bulk.no_key")
        return {}

    if series_ids is None:
        series_ids = list(set([sid for sid, _ in FRED_SERIES.values()]))

    results: dict[str, pd.DataFrame] = {}
    for sid in series_ids:
        df = client.get_observations(sid, limit=limit)
        if not df.empty:
            results[sid] = df

    log.info("fetch_fred_series_bulk.done", fetched=len(results), requested=len(series_ids))
    return results


def clear_cache() -> None:
    """
    Clear all cached data (FF JSON cache, combined calendar Parquet, rate limiter).
    Use this if you need fresh data immediately.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ff_cache = _get_cache_file_path()
    if ff_cache.exists():
        ff_cache.unlink()
        log.info("cache.cleared", file=str(ff_cache))

    for parquet_path in OUTPUT_DIR.glob("calendar_combined_*.parquet"):
        parquet_path.unlink()
        log.info("cache.cleared", file=str(parquet_path))
    
    # Also clear the rate limit file
    rate_limit_file = OUTPUT_DIR / ".ff_last_request"
    if rate_limit_file.exists():
        rate_limit_file.unlink()
        log.info("cache.cleared", file=str(rate_limit_file))


# ── CLI smoke-test ────────────────────────────────────────────────────────────

def _run_cli_test() -> None:
    """Run CLI smoke test."""
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 160)

    fred = FREDClient()

    print("\n" + "="*80)
    print("Economic Calendar Ingestion Test")
    print("="*80)

    print("\n── FRED API Status ──────────────────────────────────────────────")
    print(f"  API key configured: {fred.available}")
    if fred.available:
        latest, prev = fred.get_latest_values("PAYEMS", n=2)
        print(f"  Latest NFP:   {latest}")
        print(f"  Previous NFP: {prev}")

    print("\n── Full calendar (this week) ─────────────────────────────────────")
    print("  Note: Disk cache skipped once if schema is stale; use force_refresh=True")
    print("        or clear_cache() for a full FF + FRED rebuild.")

    cal = fetch_calendar(use_cache=True, fred_client=fred, force_refresh=False)
    
    if not cal.empty:
        print("\n📋 Recent Economic Events:")
        display_cols = [
            "datetime", "currency", "impact", "title",
            "forecast", "actual", "fred_actual", "surprise_z", 
            "is_high_priority", "source",
        ]
        avail_cols = [c for c in display_cols if c in cal.columns]
        print(cal[avail_cols].to_string(index=False))
        print(f"\n✅ Total events: {len(cal)}")
        
        # FRED enrichment stats
        fred_enriched = cal[cal["fred_series_id"].notna()]
        print(f"\n📈 FRED Enriched Events: {len(fred_enriched)}/{len(cal)}")
        
        if len(fred_enriched) > 0:
            print("\n📊 Sample Enriched Events:")
            print(fred_enriched[["title", "fred_series_id", "fred_actual"]].head(10).to_string(index=False))
    else:
        print("⚠️  No events found - you may be rate-limited")
        print("   Try again later or use: clear_cache() then fetch_calendar(force_refresh=True)")

    print("\n── Upcoming 48h (Medium+ impact) ────────────────────────────────")
    upcoming = fetch_upcoming_events(hours_ahead=48, min_impact="Medium", fred_client=fred)
    if not upcoming.empty:
        print(upcoming[["datetime", "currency", "impact", "title", "fred_actual"]].to_string(index=False))
    else:
        print("No upcoming events found.")

    print("\n── FRED Series Bulk Fetch Test ────────────────────────────────")
    fred_data = fetch_fred_series_bulk(series_ids=["PAYEMS", "UNRATE"], limit=5)
    for series_id, df in fred_data.items():
        print(f"\n{series_id} (latest {len(df)} observations):")
        print(df[["date", "value"]].to_string(index=False))


if __name__ == "__main__":
    _run_cli_test()
