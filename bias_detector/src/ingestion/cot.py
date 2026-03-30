"""
src/ingestion/cot.py
─────────────────────
CFTC Commitment of Traders (COT) ingestion for the FX bias detector.

Source   : CFTC public FTP/HTTPS — Disaggregated Futures Only (financial)
URL      : https://www.cftc.gov/files/dea/history/fut_fin_xls_{YEAR}.zip
Format   : CSV inside ZIP, released every Friday ~15:30 ET (data as of prior Tuesday)
Output   : pd.DataFrame + Parquet cache

Relevant contracts:
    EURUSD → "EURO FX"    (CFTC code 099741)
    GBPUSD → "BRITISH POUND STG" (CFTC code 096742)

Features extracted:
    - Non-commercial net positions (large speculators)
    - Change in net positions (week-on-week)
    - Open interest
    - COT index (net as % of OI, z-scored over rolling 52w window)

Dependencies (all in requirements.txt):
    requests, tenacity, pydantic, pandas, python-dotenv, structlog
"""

from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import structlog
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
        structlog.dev.ConsoleRenderer(),
    ]
)
log = structlog.get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

CFTC_BASE_URL  = "https://www.cftc.gov/dea/futures/financial_lf.html"
OUTPUT_DIR     = Path(os.getenv("DATA_DIR", "data/raw/cot"))

# Contract names as they appear in the CFTC CSV (partial match, case-insensitive)
CONTRACT_MAP: dict[str, str] = {
    "EURUSD": "EURO FX",
    "GBPUSD": "BRITISH POUND",
}

# CFTC CSV column aliases (the file uses verbose headers)
# Maps our clean name → possible raw CFTC column names
_COL_ALIASES: dict[str, list[str]] = {
    "report_date":           ["As of Date in Form YYYY-MM-DD", "Report_Date_as_YYYY-MM-DD"],
    "contract_name":         ["Market and Exchange Names", "Market_and_Exchange_Names"],
    "open_interest":         ["Open Interest (All)", "Open_Interest_All"],
    "nc_long":               ["NonComm Positions Long (All)", "NonComm_Positions_Long_All"],
    "nc_short":              ["NonComm Positions Short (All)", "NonComm_Positions_Short_All"],
    "nc_spread":             ["NonComm Positions Spread (All)", "NonComm_Positions_Spread_All"],
    "comm_long":             ["Comm Positions Long (All)", "Comm_Positions_Long_All"],
    "comm_short":            ["Comm Positions Short (All)", "Comm_Positions_Short_All"],
    "change_nc_long":        ["Change in NonComm Long (All)", "Change_in_NonComm_Long_All"],
    "change_nc_short":       ["Change in NonComm Short (All)", "Change_in_NonComm_Short_All"],
    "change_oi":             ["Change in Open Interest (All)", "Change_in_Open_Interest_All"],
}

# Days before we flag COT data as stale
STALE_THRESHOLD_DAYS = 10

# Rolling window for z-score (weeks)
ZSCORE_WINDOW = 52


# ── Pydantic models ───────────────────────────────────────────────────────────

class COTRecord(BaseModel):
    """Single weekly COT record for one contract."""

    report_date:    str
    pair:           str
    contract_name:  str
    open_interest:  int
    nc_long:        int
    nc_short:       int
    nc_spread:      int
    comm_long:      int
    comm_short:     int
    change_nc_long: int
    change_nc_short: int
    change_oi:      int

    # Derived fields (populated post-construction)
    nc_net:        int   = 0
    nc_net_change: int   = 0
    cot_index:     float = 0.0   # net / OI, range [-1, +1]
    quality_flag:  int   = 0     # 0=good, 1=partial, 2=stale

    @field_validator("open_interest", "nc_long", "nc_short", mode="before")
    @classmethod
    def coerce_int(cls, v) -> int:
        try:
            return int(str(v).replace(",", "").strip())
        except (ValueError, TypeError):
            return 0

    @field_validator("report_date")
    @classmethod
    def valid_date(cls, v: str) -> str:
        pd.to_datetime(v, errors="raise")
        return v

    @model_validator(mode="after")
    def compute_derived(self) -> "COTRecord":
        self.nc_net        = self.nc_long - self.nc_short
        self.nc_net_change = self.change_nc_long - self.change_nc_short
        if self.open_interest > 0:
            self.cot_index = round(self.nc_net / self.open_interest, 6)
        return self


# ── Retry decorator ───────────────────────────────────────────────────────────

_RETRY = retry(
    retry=retry_if_exception_type((requests.RequestException, TimeoutError)),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=3, max=90),
    before_sleep=before_sleep_log(logging.getLogger(__name__), logging.WARNING),
    reraise=True,
)

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "FXBiasDetector/1.0 (research; contact: quant-ops@internal)",
})


# ── CFTC download ─────────────────────────────────────────────────────────────

@_RETRY
def _download_cftc_zip(year: int) -> bytes:
    """Download and return raw ZIP bytes for a given year."""
    url = f"{CFTC_BASE_URL}/fut_fin_xls_{year}.zip"
    log.info("cot.cftc.download", url=url, year=year)
    resp = _SESSION.get(url, timeout=60, stream=True)
    resp.raise_for_status()
    content = resp.content
    log.info("cot.cftc.downloaded", year=year, size_kb=len(content) // 1024)
    return content


def _extract_csv_from_zip(zip_bytes: bytes, year: int) -> str:
    """Extract the main CSV from a CFTC ZIP file."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        log.debug("cot.zip.contents", files=names)

        # CFTC ZIP typically contains one CSV
        csv_files = [n for n in names if n.lower().endswith(".csv")]
        if not csv_files:
            # Also try .txt (older releases)
            csv_files = [n for n in names if n.lower().endswith(".txt")]
        if not csv_files:
            raise ValueError(f"No CSV/TXT found in CFTC ZIP for {year}. Contents: {names}")

        target = csv_files[0]
        log.info("cot.zip.extract", file=target)
        return zf.read(target).decode("utf-8", errors="replace")


# ── Column normalisation ──────────────────────────────────────────────────────

def _resolve_column(df: pd.DataFrame, aliases: list[str]) -> Optional[str]:
    """Return the first alias that exists in df.columns, else None."""
    for alias in aliases:
        if alias in df.columns:
            return alias
        # Try case-insensitive
        for col in df.columns:
            if col.strip().lower() == alias.lower():
                return col
    return None


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Rename verbose CFTC headers to clean names.
    Drops rows where critical columns are missing.
    """
    rename_map: dict[str, str] = {}
    missing: list[str] = []

    for clean_name, aliases in _COL_ALIASES.items():
        raw_col = _resolve_column(df, aliases)
        if raw_col:
            rename_map[raw_col] = clean_name
        else:
            missing.append(clean_name)
            log.warning("cot.column.missing", field=clean_name)

    df = df.rename(columns=rename_map)

    # Add missing columns as zeros to keep schema consistent
    for col in missing:
        df[col] = 0

    return df


# ── Contract filter ───────────────────────────────────────────────────────────

def _filter_contracts(df: pd.DataFrame, pair: str) -> pd.DataFrame:
    """Return rows matching the target contract for the given pair."""
    keyword = CONTRACT_MAP.get(pair.upper(), "")
    if not keyword:
        raise ValueError(f"Unknown pair: {pair}. Supported: {list(CONTRACT_MAP.keys())}")

    mask = df["contract_name"].str.contains(keyword, case=False, na=False)
    filtered = df[mask].copy()
    log.info("cot.filter", pair=pair, keyword=keyword, rows=len(filtered))
    return filtered


# ── Z-score enrichment ────────────────────────────────────────────────────────

def _add_zscore(df: pd.DataFrame, window: int = ZSCORE_WINDOW) -> pd.DataFrame:
    """
    Add a z-scored COT net position column over a rolling window.
    z_score = (nc_net - rolling_mean) / rolling_std

    This is Feature F3 in the bias detector.
    """
    df = df.sort_values("report_date").copy()
    df["nc_net_rolling_mean"] = df["nc_net"].rolling(window, min_periods=4).mean()
    df["nc_net_rolling_std"]  = df["nc_net"].rolling(window, min_periods=4).std()
    df["cot_z_score"] = (
        (df["nc_net"] - df["nc_net_rolling_mean"])
        / df["nc_net_rolling_std"].replace(0, float("nan"))
    ).round(4)
    df["cot_z_score"] = df["cot_z_score"].clip(-4, 4)  # cap extremes
    return df


# ── Staleness check ───────────────────────────────────────────────────────────

def _check_staleness(df: pd.DataFrame) -> pd.DataFrame:
    """Flag rows where the report date is more than STALE_THRESHOLD_DAYS old."""
    now = datetime.now(timezone.utc).date()
    df["report_date_dt"] = pd.to_datetime(df["report_date"], errors="coerce")
    df["days_old"] = (now - df["report_date_dt"].dt.date).apply(
        lambda d: d.days if pd.notna(d) else 999
    )
    df["quality_flag"] = df["days_old"].apply(
        lambda d: 2 if d > STALE_THRESHOLD_DAYS else 0
    )
    if (df["quality_flag"] == 2).any():
        max_days = df["days_old"].max()
        log.warning("cot.stale_data", max_days_old=int(max_days))
    return df


# ── Cache layer ───────────────────────────────────────────────────────────────

def _cache_path(pair: str, year: int) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / f"cot_{pair.upper()}_{year}.parquet"


def _save_parquet(df: pd.DataFrame, pair: str, year: int) -> Path:
    path = _cache_path(pair, year)
    df.to_parquet(path, index=False, compression="snappy")
    log.info("cot.cache.saved", path=str(path), rows=len(df))
    return path


def _load_cached(pair: str, year: int) -> Optional[pd.DataFrame]:
    path = _cache_path(pair, year)
    if not path.exists():
        return None
    age_hours = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 3600
    # COT is weekly — cache is valid for 6 days
    if age_hours > 144:
        log.info("cot.cache.expired", path=str(path), age_h=round(age_hours, 1))
        return None
    df = pd.read_parquet(path)
    log.info("cot.cache.hit", path=str(path))
    return df


# ── Multi-year loader ─────────────────────────────────────────────────────────

def _load_years(years: list[int]) -> pd.DataFrame:
    """Download and concatenate COT data for the requested years."""
    frames: list[pd.DataFrame] = []

    for year in years:
        try:
            zip_bytes = _download_cftc_zip(year)
            csv_text  = _extract_csv_from_zip(zip_bytes, year)
            raw_df    = pd.read_csv(io.StringIO(csv_text), low_memory=False)
            frames.append(raw_df)
            log.info("cot.year.loaded", year=year, rows=len(raw_df))
        except Exception as exc:
            log.error("cot.year.failed", year=year, error=str(exc))
            # Non-fatal: continue with remaining years

    if not frames:
        raise RuntimeError("Could not load any COT data from CFTC")

    combined = pd.concat(frames, ignore_index=True)
    log.info("cot.combined", total_rows=len(combined))
    return combined


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_cot(
    pair: str = "EURUSD",
    lookback_years: int = 2,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Main entry point — returns a validated, enriched COT DataFrame.

    Parameters
    ----------
    pair            : "EURUSD" or "GBPUSD"
    lookback_years  : How many years of history to load (min 1, max 5)
    use_cache       : Return cached Parquet if available and fresh (<6 days)

    Returns
    -------
    pd.DataFrame with columns:
        report_date, pair, contract_name,
        open_interest, nc_long, nc_short, nc_spread,
        comm_long, comm_short,
        change_nc_long, change_nc_short, change_oi,
        nc_net, nc_net_change, cot_index,
        cot_z_score, days_old, quality_flag
    """
    pair = pair.upper()
    if pair not in CONTRACT_MAP:
        raise ValueError(f"Unsupported pair: {pair}. Use: {list(CONTRACT_MAP.keys())}")

    current_year = datetime.now().year
    years = list(range(current_year - lookback_years + 1, current_year + 1))

    # ── Cache check (current year only) ──────────────────────────────────────
    if use_cache:
        cached = _load_cached(pair, current_year)
        if cached is not None:
            return cached

    # ── Download from CFTC ────────────────────────────────────────────────────
    raw_combined = _load_years(years)

    # ── Normalise columns ─────────────────────────────────────────────────────
    raw_combined = _normalise_columns(raw_combined)

    # ── Filter to target contract ─────────────────────────────────────────────
    filtered = _filter_contracts(raw_combined, pair)
    if filtered.empty:
        log.error("cot.no_data_for_pair", pair=pair)
        return pd.DataFrame()

    # ── Validate rows through Pydantic ────────────────────────────────────────
    records: list[COTRecord] = []
    for _, row in filtered.iterrows():
        try:
            rec = COTRecord(
                report_date     = str(row.get("report_date", "")),
                pair            = pair,
                contract_name   = str(row.get("contract_name", "")),
                open_interest   = row.get("open_interest",   0),
                nc_long         = row.get("nc_long",         0),
                nc_short        = row.get("nc_short",        0),
                nc_spread       = row.get("nc_spread",       0),
                comm_long       = row.get("comm_long",       0),
                comm_short      = row.get("comm_short",      0),
                change_nc_long  = row.get("change_nc_long",  0),
                change_nc_short = row.get("change_nc_short", 0),
                change_oi       = row.get("change_oi",       0),
            )
            records.append(rec)
        except Exception as exc:
            log.warning("cot.row.invalid", reason=str(exc))

    if not records:
        log.error("cot.all_rows_invalid", pair=pair)
        return pd.DataFrame()

    log.info("cot.validated", pair=pair, valid_records=len(records))

    # ── Build DataFrame ───────────────────────────────────────────────────────
    df = pd.DataFrame([r.model_dump() for r in records])
    df = df.drop_duplicates(subset=["report_date"]).sort_values("report_date")

    # ── Z-score enrichment ────────────────────────────────────────────────────
    df = _add_zscore(df, window=ZSCORE_WINDOW)

    # ── Staleness flag ────────────────────────────────────────────────────────
    df = _check_staleness(df)

    # ── Cache current year ────────────────────────────────────────────────────
    _save_parquet(df, pair, current_year)

    return df.reset_index(drop=True)


def get_latest_cot(pair: str = "EURUSD") -> Optional[pd.Series]:
    """
    Return the single most recent COT record as a Series.
    Used by the feature calculator to get F3_cot_z.
    """
    df = fetch_cot(pair=pair)
    if df.empty:
        return None
    latest = df.iloc[-1]
    log.info(
        "cot.latest",
        pair=pair,
        report_date=latest["report_date"],
        nc_net=int(latest["nc_net"]),
        cot_z=round(float(latest["cot_z_score"]), 3),
        quality_flag=int(latest["quality_flag"]),
    )
    return latest


def fetch_cot_all_pairs(lookback_years: int = 2) -> dict[str, pd.DataFrame]:
    """
    Fetch COT for all supported pairs. Returns dict keyed by pair name.
    Continues on per-pair failure — never crashes the full pipeline.
    """
    results: dict[str, pd.DataFrame] = {}
    for pair in CONTRACT_MAP:
        try:
            results[pair] = fetch_cot(pair=pair, lookback_years=lookback_years)
        except Exception as exc:
            log.error("cot.pair.failed", pair=pair, error=str(exc))
            results[pair] = pd.DataFrame()
    return results


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 160)
    pd.set_option("display.float_format", lambda x: f"{x:.4f}")

    for pair in ["EURUSD", "GBPUSD"]:
        print(f"\n{'─'*70}")
        print(f"  COT — {pair}")
        print(f"{'─'*70}")

        df = fetch_cot(pair=pair, lookback_years=2, use_cache=False)

        if df.empty:
            print(f"  No data returned for {pair}")
            continue

        cols = [
            "report_date", "nc_long", "nc_short", "nc_net",
            "nc_net_change", "open_interest", "cot_index",
            "cot_z_score", "days_old", "quality_flag",
        ]
        available = [c for c in cols if c in df.columns]
        print(df[available].tail(10).to_string(index=False))

        latest = get_latest_cot(pair)
        if latest is not None:
            print(f"\n  Latest record  : {latest['report_date']}")
            print(f"  NC Net         : {int(latest['nc_net']):,}")
            print(f"  COT z-score    : {latest['cot_z_score']:.4f}")
            print(f"  Quality flag   : {int(latest['quality_flag'])}")

    sys.exit(0)