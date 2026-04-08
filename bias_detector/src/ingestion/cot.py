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

CFTC_BASE_URL  = "https://www.cftc.gov/files/dea/history/fut_fin_xls_{year}.zip"
OUTPUT_DIR     = Path(os.getenv("DATA_DIR", "data/raw/cot"))

# Contract names as they appear in the CFTC CSV (partial match, case-insensitive)
CONTRACT_MAP: dict[str, str] = {
    "EURUSD": "EURO FX",
    "GBPUSD": "BRITISH POUND",
}

# CFTC CSV column aliases
# Maps our clean name → possible raw CFTC column names
_COL_ALIASES: dict[str, list[str]] = {
    "report_date": ["As_of_Date_In_Form_YYMMDD", "As of Date in Form YYMMDD"],
    "contract_name": ["Market_and_Exchange_Names", "Market and Exchange Names"],
    "open_interest": ["Open_Interest_All", "Open Interest (All)"],
    
    # Disaggregated categories (the ones that matter)
    "dealer_long": ["Dealer_Positions_Long_All", "Dealer Positions Long (All)"],
    "dealer_short": ["Dealer_Positions_Short_All", "Dealer Positions Short (All)"],
    "dealer_spread": ["Dealer_Positions_Spread_All", "Dealer Positions Spread (All)"],
    
    "asset_mgr_long": ["Asset_Mgr_Positions_Long_All", "Asset Manager Positions Long (All)"],
    "asset_mgr_short": ["Asset_Mgr_Positions_Short_All", "Asset Manager Positions Short (All)"],
    "asset_mgr_spread": ["Asset_Mgr_Positions_Spread_All", "Asset Manager Positions Spread (All)"],
    
    "lev_money_long": ["Lev_Money_Positions_Long_All", "Leveraged Money Positions Long (All)"],
    "lev_money_short": ["Lev_Money_Positions_Short_All", "Leveraged Money Positions Short (All)"],
    "lev_money_spread": ["Lev_Money_Positions_Spread_All", "Leveraged Money Positions Spread (All)"],
    
    # Changes
    "change_dealer_long": ["Change_in_Dealer_Long_All", "Change in Dealer Long (All)"],
    "change_dealer_short": ["Change_in_Dealer_Short_All", "Change in Dealer Short (All)"],
    "change_asset_mgr_long": ["Change_in_Asset_Mgr_Long_All", "Change in Asset Manager Long (All)"],
    "change_asset_mgr_short": ["Change_in_Asset_Mgr_Short_All", "Change in Asset Manager Short (All)"],
    "change_lev_money_long": ["Change_in_Lev_Money_Long_All", "Change in Lev Money Long (All)"],
    "change_lev_money_short": ["Change_in_Lev_Money_Short_All", "Change in Lev Money Short (All)"],
    "change_oi": ["Change_in_Open_Interest_All", "Change in Open Interest (All)"],
    
    # Concentration (valuable) used for risk management when top traders have large positions
    "conc_gross_4_long": ["Conc_Gross_LE_4_TDR_Long_All", "Concentration Gross Long 4 TDR"],
    "conc_gross_4_short": ["Conc_Gross_LE_4_TDR_Short_All", "Concentration Gross Short 4 TDR"],
}


# Days before we flag COT data as stale
STALE_THRESHOLD_DAYS = 10

# Rolling window for z-score (weeks)
ZSCORE_WINDOW = 52


# ── Pydantic models ───────────────────────────────────────────────────────────

class COTRecord(BaseModel):
    """Single weekly COT record for one contract."""

    report_date:            str
    pair:                   str
    contract_name:          str
    open_interest:          int
    # dealers
    dealer_long:            int
    dealer_short:           int
    dealer_spread:          int
    # aum
    asset_mgr_long:         int
    asset_mgr_short:        int
    asset_mgr_spread:       int
    # hedge funds
    lev_money_long:         int
    lev_money_short:        int
    lev_money_spread:       int
    # changes
    change_dealer_long:     int
    change_dealer_short:    int
    change_asset_mgr_long:  int
    change_asset_mgr_short: int
    change_lev_money_long:  int
    change_lev_money_short: int
    change_oi:              int

    # Derived
    dealer_net: int = 0
    asset_mgr_net: int = 0
    lev_money_net: int = 0
    
    # The classic "Smart Money vs Dumb Money" spread
    smart_money_index: float = 0.0  # dealer_net / OI (z-scored later)
    speculative_pressure: float = 0.0  # asset_mgr_net / OI

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
    def compute_derived(self):
        self.dealer_net = self.dealer_long - self.dealer_short
        self.asset_mgr_net = self.asset_mgr_long - self.asset_mgr_short
        self.lev_money_net = self.lev_money_long - self.lev_money_short
        
        if self.open_interest > 0:
            self.smart_money_index = self.dealer_net / self.open_interest
            self.speculative_pressure = self.asset_mgr_net / self.open_interest
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
    url = CFTC_BASE_URL.format(year=year)
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


# ── Staleness check ───────────────────────────────────────────────────────────
def _check_staleness(
    df: pd.DataFrame, 
    threshold: int = 10) -> pd.DataFrame:
    """
    Flag rows where the report date is older than the most recent COT release.
    
    CFTC reports are as of Tuesday, released Friday ~15:30 ET.
    We measure staleness as days between the report's as-of date and the 
    most recent Tuesday (when fresh data should have been released).
    
    Parameters
    ----------
    df : pd.DataFrame
        Must contain 'report_date' column (string or datetime)
    threshold : int
        Days after which data is flagged as stale (default: 10)
        
    Returns
    -------
    pd.DataFrame
        Copy with added columns:
        - 'days_old': Days since last Tuesday
        - 'quality_flag': 0=fresh, 1=warning(>threshold), 2=stale(>threshold+7)
    """
    df = df.copy()
    
    # Calculate most recent Tuesday (CFTC reporting date)
    today = datetime.now(timezone.utc).date()
    days_since_tuesday = (today.weekday() - 1) % 7  # Tuesday = 1
    last_tuesday = today - timedelta(days=days_since_tuesday)
    
    # Calculate age of each record relative to reporting cycle
    report_dates = pd.to_datetime(df["report_date"]).dt.date
    
    # Handle timezone-aware vs naive comparisons
    df["days_old"] = (last_tuesday - report_dates).apply(
        lambda d: d.days if hasattr(d, 'days') else (last_tuesday - d).days
    )
    
    # Quality flag: 0=fresh, 1=warning, 2=stale
    df["quality_flag"] = df["days_old"].apply(
        lambda d: 0 if d <= threshold else (2 if d > (threshold + 7) else 1)
    )
    
    # Logging
    stale_count = (df["quality_flag"] > 0).sum()
    if stale_count > 0:
        max_days = int(df["days_old"].max())
        avg_days = float(df["days_old"].mean())
        log.warning(
            "cot.stale_data_detected",
            stale_records=int(stale_count),
            max_days_old=max_days,
            avg_days_old=round(avg_days, 1),
            threshold=threshold,
            reference_tuesday=str(last_tuesday)
        )
    
    return df



# ── Cache layer ───────────────────────────────────────────────────────────────

def _cache_path(pair: str, year: int, lookback_years: int) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / f"cot_{pair.upper()}_{year}_lb{lookback_years}.parquet"


def _save_parquet(df: pd.DataFrame, pair: str, year: int, lookback_years: int) -> Path:
    path = _cache_path(pair, year, lookback_years)
    df.to_parquet(path, index=False, compression="snappy")
    log.info("cot.cache.saved", path=str(path), rows=len(df))
    return path


def _load_cached(pair: str, year: int, lookback_years: int) -> Optional[pd.DataFrame]:
    path = _cache_path(pair, year, lookback_years)
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
    pair            : str
        "EURUSD" or "GBPUSD"
    lookback_years  : int
        How many years of history to load (min 1, max 5)
    use_cache       : bool
        Return cached Parquet if available and fresh (<6 days)

    Returns
    -------
    pd.DataFrame with columns including:
        report_date, pair, contract_name, open_interest,
        dealer_long, dealer_short, dealer_spread, dealer_net, dealer_net_pct,
        asset_mgr_long, asset_mgr_short, asset_mgr_net, asset_mgr_net_pct,
        lev_money_long, lev_money_short, lev_money_net, lev_money_net_pct,
        change_dealer_long, change_dealer_short, ... change_oi,
        cci, cci_z,                           # Composite index (z-scored)
        conc_risk, is_high_conc,              # Concentration metrics
        is_roll_week, is_roll_transition,     # Roll detection
        days_old, quality_flag                # Data freshness
    """
    pair = pair.upper()
    if pair not in CONTRACT_MAP:
        raise ValueError(f"Unsupported pair: {pair}. Use: {list(CONTRACT_MAP.keys())}")

    current_year = datetime.now().year
    years = list(range(current_year - lookback_years + 1, current_year + 1))

    # ── Cache check (includes lookback_years in key) ────────────────────────
    if use_cache:
        cached = _load_cached(pair, current_year, lookback_years)
        if cached is not None:
            # Verify schema compatibility - regenerate if columns mismatch
            required_new_cols = ["dealer_net", "cci_z", "is_roll_week"]
            if all(col in cached.columns for col in required_new_cols):
                return cached
            log.info("cot.cache.schema_mismatch", pair=pair, year=current_year)

    # ── Download from CFTC ────────────────────────────────────────────────────
    raw_combined = _load_years(years)

    # ── Normalise columns (now maps to Disaggregated schema) ──────────────────
    raw_combined = _normalise_columns(raw_combined)

    # ── Filter to target contract ─────────────────────────────────────────────
    filtered = _filter_contracts(raw_combined, pair)
    if filtered.empty:
        log.error("cot.no_data_for_pair", pair=pair)
        return pd.DataFrame()

    # ── Validate rows through Pydantic (Disaggregated fields) ───────────────────
    records: list[COTRecord] = []
    for _, row in filtered.iterrows():
        try:
            rec = COTRecord(
                report_date             = str(row.get("report_date", "")),
                pair                    = pair,
                contract_name           = str(row.get("contract_name", "")),
                open_interest           = row.get("open_interest", 0),
                # Disaggregated: Dealers (Commercial)
                dealer_long             = row.get("dealer_long", 0),
                dealer_short            = row.get("dealer_short", 0),
                dealer_spread           = row.get("dealer_spread", 0),
                # Disaggregated: Asset Managers (Institutional Specs)
                asset_mgr_long          = row.get("asset_mgr_long", 0),
                asset_mgr_short         = row.get("asset_mgr_short", 0),
                asset_mgr_spread        = row.get("asset_mgr_spread", 0),
                # Disaggregated: Leveraged Money (Small Specs)
                lev_money_long          = row.get("lev_money_long", 0),
                lev_money_short         = row.get("lev_money_short", 0),
                lev_money_spread        = row.get("lev_money_spread", 0),
                # Changes
                change_dealer_long      = row.get("change_dealer_long", 0),
                change_dealer_short     = row.get("change_dealer_short", 0),
                change_asset_mgr_long   = row.get("change_asset_mgr_long", 0),
                change_asset_mgr_short  = row.get("change_asset_mgr_short", 0),
                change_lev_money_long   = row.get("change_lev_money_long", 0),
                change_lev_money_short  = row.get("change_lev_money_short", 0),
                change_oi               = row.get("change_oi", 0),
            )
            records.append(rec)
        except Exception as exc:
            log.warning("cot.row.invalid", reason=str(exc), 
                       report_date=row.get("report_date"))

    if not records:
        log.error("cot.all_rows_invalid", pair=pair)
        return pd.DataFrame()

    log.info("cot.validated", pair=pair, valid_records=len(records))

    # ── Build DataFrame ───────────────────────────────────────────────────────
    # model_dump() includes derived fields: dealer_net, asset_mgr_net, lev_money_net, 
    # smart_money_index, speculative_pressure (calculated in Pydantic validator)
    df = pd.DataFrame([r.model_dump() for r in records])
    df = df.drop_duplicates(subset=["report_date"]).sort_values("report_date")

    # ── Enrichment Pipeline (order matters) ──────────────────────────────────
    
    # 1. Calculate % of OI (creates *_pct columns)
    df = _calc_percent_oi(df)
    
    # 2. Calculate CCI and z-score (main predictive feature)
    #    Requires: dealer_net, asset_mgr_net, open_interest
    df = _add_cci(df, window=ZSCORE_WINDOW)
    
    # 3. Calculate concentration risk (for position sizing adjustments)
    df = _calc_conc_risk(df, threshold=0.70)
    
    # 4. Detect contract roll weeks (mute signals during rolls)
    df = _detect_roll_weeks(df, threshold=0.25)
    
    # 5. Staleness check (data quality flag)
    df = _check_staleness(df, threshold=10)

    # ── Cache with proper key (includes lookback_years) ──────────────────────
    _save_parquet(df, pair, current_year, lookback_years)

    return df.reset_index(drop=True)



def get_latest_cot(pair: str = "EURUSD") -> Optional[pd.Series]:
    """
    Return the single most recent COT record as a Series.
    Used by the feature calculator to get F3 (cci_z) and bias inputs.
    """
    df = fetch_cot(pair=pair)
    if df.empty:
        return None
    latest = df.iloc[-1]
    log.info(
        "cot.latest",
        pair=pair,
        report_date=latest["report_date"],
        dealer_net=int(latest["dealer_net"]),
        asset_mgr_net=int(latest["asset_mgr_net"]),
        cci_z=round(float(latest["cci_z"]), 3),
        is_roll_week=bool(latest.get("is_roll_week", False)),
        is_high_conc=bool(latest.get("is_high_conc", False)),
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


def _calc_conc_risk(
    df: pd.DataFrame, 
    col: str = "conc_gross_4_long",
    threshold: float = 0.70) -> pd.DataFrame:
    """
    Calculate concentration risk from top-4 trader gross long positions.
    
    CFTC concentration data is typically expressed as a percentage (0-100).
    This normalizes it to a 0-1 fraction and flags high-concentration regimes
    where a few large players dominate (>70% by default).
    
    Parameters
    ----------
    df : pd.DataFrame
        Must contain the concentration column (default: 'conc_gross_4_long')
    col : str
        Column name for gross concentration of top 4 traders
    threshold : float
        Threshold above which concentration is flagged as high risk (0-1 scale)
        
    Returns
    -------
    pd.DataFrame
        Copy of input with added columns:
        - 'conc_risk': Normalized concentration [0, 1]
        - 'is_high_conc': Boolean flag if concentration > threshold
        
    Raises
    ------
    ValueError
        If the specified concentration column is missing
    """
    if col not in df.columns:
        raise ValueError(
            f"Concentration column '{col}' not found. "
            f"Available columns: {list(df.columns)}"
        )
    
    # Prevent mutation of original DataFrame
    df = df.copy()
    
    # Handle NaN and normalize percentage (e.g., 55.7 -> 0.557)
    # CFTC files report this as 0-100, so we divide by 100
    raw_values = df[col].fillna(0)
    
    # Auto-detect if already fractional (0-1) or percentage (0-100)
    if raw_values.max() > 1.0:
        df["conc_risk"] = (raw_values / 100).clip(0, 1)
    else:
        df["conc_risk"] = raw_values.clip(0, 1)
    
    df["is_high_conc"] = df["conc_risk"] > threshold
    
    # Monitoring: log when concentration spikes
    if df["is_high_conc"].any():
        log.warning(
            "cot.high_concentration",
            count=int(df["is_high_conc"].sum()),
            max_conc=round(float(df["conc_risk"].max()), 2),
            threshold=threshold
        )
    
    return df


def _calc_percent_oi(
    df: pd.DataFrame, 
    cols: list[str] | None = None) -> pd.DataFrame:
    """
    Calculate net positions as percentage of total Open Interest.
    
    Net position columns (e.g., 'dealer_net') are divided by Open Interest
    to produce a normalized fraction (e.g., 0.543 = 54.3% of OI).
    
    Parameters
    ----------
    df : pd.DataFrame
        Must contain 'open_interest' and the specified net position columns
    cols : list[str] | None, optional
        Net position columns to normalize. 
        Defaults to: ['dealer_net', 'asset_mgr_net', 'lev_money_net']
        
    Returns
    -------
    pd.DataFrame
        Copy of input with added columns '{col}_pct' containing the OI fraction
        
    Raises
    ------
    ValueError
        If required columns are missing from DataFrame
        
    Notes
    -----
    - Division by zero (0 OI) produces NaN rather than inf
    - Results are clipped to reasonable bounds (-1.5, +1.5) to catch data errors
    """
    if cols is None:
        cols = ["dealer_net", "asset_mgr_net", "lev_money_net"]
    
    required = ["open_interest"] + cols
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns for OI calc: {missing}")
    
    df = df.copy()
    
    # Protect against division by zero - replace 0 with NaN to propagate NaN in result
    oi_safe = df["open_interest"].replace(0, float("nan"))
    
    # Warn if we have zero OI rows (indicates bad data or roll week anomalies)
    if oi_safe.isna().any():
        zero_count = oi_safe.isna().sum()
        log.warning(
            "cot.zero_open_interest",
            count=int(zero_count),
            pct_of_data=round(100 * zero_count / len(df), 2)
        )
    
    for col in cols:
        new_col = f"{col}_pct"
        df[new_col] = (df[col] / oi_safe).round(6)
        
        # Sanity check: net positions shouldn't exceed OI by >50% (spreads can cause slight overlap)
        invalid_mask = df[new_col].abs() > 1.5
        if invalid_mask.any():
            log.error(
                "cot.invalid_oi_ratio",
                column=col,
                count=int(invalid_mask.sum()),
                max_ratio=float(df.loc[invalid_mask, new_col].abs().max()),
                dates=df.loc[invalid_mask, "report_date"].head(3).tolist()
            )
            # Cap extreme outliers to prevent downstream algorithm distortion
            df[new_col] = df[new_col].clip(-1.5, 1.5)
    
    return df

# ── Z-score enrichment && CCI calculation ────────────────────────────────────────────────────────

def _add_cci(df: pd.DataFrame, window: int = 52, min_periods: int = 12) -> pd.DataFrame:
    """
    Calculate COT Composite Index (CCI) and its rolling z-score.
    
    CCI captures the divergence between Commercial (Dealer) and Institutional 
    Speculative (Asset Manager) positioning:
        CCI = (Dealer_Net - Asset_Mgr_Net) / Open_Interest
    
    Interpretation:
        +CCI → Dealers net-long vs Asset Managers net-short (Smart Money bullish)
        -CCI → Dealers net-short vs Asset Managers net-long (Smart Money bearish)
    
    Parameters
    ----------
    df : pd.DataFrame
        Must contain columns: ['report_date', 'dealer_net', 'asset_mgr_net', 'open_interest']
    window : int, optional
        Rolling lookback window in weeks for z-score (default: 52)
    min_periods : int, optional
        Minimum observations required to calculate z-score (default: 12)
    
    Returns
    -------
    pd.DataFrame
        Input DataFrame with added columns:
        - 'cci'    : Raw composite index [-1, +1] range theoretically unbounded
        - 'cci_z'  : Z-scored CCI over the rolling window (clipped to ±4)
    """
    required = ["report_date", "dealer_net", "asset_mgr_net", "open_interest"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"CCI calculation missing columns: {missing}")
    
    df = df.sort_values("report_date").copy()
    
    # Calculate raw CCI (Smart Money vs Specs spread)
    # Protect against div-by-zero by replacing 0 OI with NaN temporarily
    oi_safe = df["open_interest"].replace(0, float("nan"))
    df["cci"] = (df["dealer_net"] - df["asset_mgr_net"]) / oi_safe
    
    # Rolling statistics for z-score
    rolling_mean = df["cci"].rolling(window=window, min_periods=min_periods).mean()
    rolling_std = df["cci"].rolling(window=window, min_periods=min_periods).std()
    
    # Avoid div-by-zero in std (flat periods where all values identical)
    rolling_std_safe = rolling_std.replace(0, float("nan"))
    
    # Calculate z-score: how many std-devs is current CCI vs its year-long average?
    df["cci_z"] = ((df["cci"] - rolling_mean) / rolling_std_safe).round(4)
    
    # Cap extremes to prevent outliers from dominating (optional but recommended)
    df["cci_z"] = df["cci_z"].clip(-4, 4)
    
    # Log latest value for monitoring
    latest = df.iloc[-1]
    log.debug(
        "cot.cci.calculated",
        report_date=latest["report_date"],
        cci=round(float(latest["cci"]), 4),
        cci_z=round(float(latest["cci_z"]), 4),
        dealer_net=int(latest["dealer_net"]),
        asset_mgr_net=int(latest["asset_mgr_net"]),
    )
    
    return df

def _detect_roll_weeks(df: pd.DataFrame, threshold: float = 0.25) -> pd.DataFrame:
    """
    Flag weeks where dealer spread positions exceed threshold % of OI.
    This indicates a contract roll - directional signals should be muted.
    """
    # Ensures data is sorted chronologically
    df = df.sort_values("report_date").copy()

    df["dealer_spread_pct"] = df["dealer_spread"] / df["open_interest"].replace(0, float("nan"))
    df["is_roll_week"] = df["dealer_spread_pct"] > threshold
    
    # Optional: flag the week before and after as "transition" periods
    df["is_roll_transition"] = (
        df["is_roll_week"] | 
        df["is_roll_week"].shift(1).fillna(False) | 
        df["is_roll_week"].shift(-1).fillna(False)
    )
    
    if df["is_roll_week"].any():
        log.warning("cot.roll_weeks_detected", 
                   count=int(df["is_roll_week"].sum()),
                   max_spread_pct=round(df["dealer_spread_pct"].max(), 2))
    
    return df


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