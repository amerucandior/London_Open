"""
src/ingestion/sentiment.py
──────────────────────────
FX sentiment ingestion for the FX bias detector.

Sources (in priority order):
  1. Myfxbook Community Outlook API   → long/short % for each pair
  2. OANDA Open Positions/Orders API  → institutional positioning proxy
  3. Synthetic fallback               → RSI-derived contrarian proxy from OHLCV

Note on contrarian logic:
    High retail long % → likely a fade signal (retail is wrong at extremes).
    Sentiment is INVERTED when |z-score| > EXTREME_THRESHOLD (default 1.5σ).

Output   : pd.DataFrame + Parquet cache

Dependencies (all in requirements.txt):
    requests, tenacity, pydantic, pandas, python-dotenv, structlog, yfinance
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import pandas as pd
import requests
import structlog
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator
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

OUTPUT_DIR        = Path(os.getenv("DATA_DIR", "data/raw/sentiment"))

# Myfxbook community outlook
MYFXBOOK_LOGIN    = "https://www.myfxbook.com/api/login.json"
MYFXBOOK_OUTLOOK  = "https://www.myfxbook.com/api/get-community-outlook.json"

# OANDA v20 API
OANDA_BASE_URL    = os.getenv("OANDA_API_URL", "https://api-fxtrade.oanda.com")
OANDA_API_KEY     = os.getenv("OANDA_API_KEY", "")

# RSI synthetic fallback
SYNTHETIC_RSI_PERIOD = 14
SYNTHETIC_LOOKBACK   = "6mo"

# Extreme positioning threshold: above this z-score, signal is inverted
EXTREME_THRESHOLD = 1.5

# Supported pairs and their Myfxbook / OANDA identifiers
PAIR_CONFIG: dict[str, dict] = {
    "EURUSD": {
        "myfxbook_symbol": "EUR/USD",
        "oanda_instrument": "EUR_USD",
        "yfinance_ticker": "EURUSD=X",
    },
    "GBPUSD": {
        "myfxbook_symbol": "GBP/USD",
        "oanda_instrument": "GBP_USD",
        "yfinance_ticker": "GBPUSD=X",
    },
}

# Sentiment window for z-score (days)
ZSCORE_WINDOW_DAYS = 90


# ── Pydantic models ───────────────────────────────────────────────────────────

class SentimentRecord(BaseModel):
    """Validated sentiment snapshot for a single pair."""

    timestamp:          str
    pair:               str
    source:             Literal["myfxbook", "oanda", "synthetic"]

    long_pct:           float = Field(ge=0.0, le=100.0)
    short_pct:          float = Field(ge=0.0, le=100.0)
    long_lots:          Optional[float] = None
    short_lots:         Optional[float] = None

    # Derived
    net_bias:           float = 0.0   # (long_pct - short_pct) / 100  → [-1, +1]
    is_extreme:         bool  = False  # True when |net_bias_z| > EXTREME_THRESHOLD
    sentiment_signal:   float = 0.0   # net_bias, inverted if extreme (contrarian)
    quality_flag:       int   = 0     # 0=good, 1=partial, 2=synthetic only

    @field_validator("long_pct", "short_pct", mode="before")
    @classmethod
    def coerce_float(cls, v) -> float:
        try:
            return float(str(v).replace("%", "").strip())
        except (ValueError, TypeError):
            return 0.0

    @model_validator(mode="after")
    def compute_derived_fields(self) -> "SentimentRecord":
        # Normalise percentages in case they were passed as fractions
        if self.long_pct <= 1.0 and self.short_pct <= 1.0:
            self.long_pct  *= 100
            self.short_pct *= 100
        # Net bias: (long% - short%) / 100 → [-1, +1]
        self.net_bias = round((self.long_pct - self.short_pct) / 100.0, 4)
        return self


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
    "User-Agent": "FXBiasDetector/1.0 (quant-research)",
    "Accept":     "application/json",
})


# ── Source 1: Myfxbook ────────────────────────────────────────────────────────

class _MyfxbookClient:
    """
    Thin stateful client handling Myfxbook session cookie.
    Credentials loaded from .env: MYFXBOOK_EMAIL, MYFXBOOK_PASSWORD
    """

    def __init__(self):
        self._session_token: Optional[str] = None

    @_RETRY
    def _login(self) -> str:
        email    = os.getenv("MYFXBOOK_EMAIL",    "")
        password = os.getenv("MYFXBOOK_PASSWORD", "")

        if not email or not password:
            raise EnvironmentError(
                "MYFXBOOK_EMAIL and MYFXBOOK_PASSWORD must be set in .env"
            )

        log.info("sentiment.myfxbook.login", email=email[:4] + "***")
        resp = _SESSION.get(
            MYFXBOOK_LOGIN,
            params={"email": email, "password": password},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("error"):
            raise ValueError(f"Myfxbook login failed: {data.get('message', 'unknown')}")

        token = data.get("session", "")
        if not token:
            raise ValueError("Myfxbook returned empty session token")

        log.info("sentiment.myfxbook.login.ok")
        return token

    @_RETRY
    def fetch_outlook(self) -> list[dict]:
        """Return raw Myfxbook community outlook list."""
        if not self._session_token:
            self._session_token = self._login()

        resp = _SESSION.get(
            MYFXBOOK_OUTLOOK,
            params={"session": self._session_token},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("error"):
            # Session may have expired — re-authenticate once
            log.warning("sentiment.myfxbook.session_expired. re-auth")
            self._session_token = self._login()
            resp = _SESSION.get(
                MYFXBOOK_OUTLOOK,
                params={"session": self._session_token},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

        symbols = data.get("symbols", {}).get("symbol", [])
        if isinstance(symbols, dict):
            symbols = [symbols]

        log.info("sentiment.myfxbook.ok", symbol_count=len(symbols))
        return symbols


_mb_client = _MyfxbookClient()


def _parse_myfxbook(symbols: list[dict], pair: str) -> Optional[SentimentRecord]:
    """
    Extract long/short % for the given pair from Myfxbook symbols list.

    Myfxbook outlook schema (abridged):
    {
      "name": "EUR/USD",
      "longPercentage": "64.17",
      "shortPercentage": "35.83",
      "longVolume": "12345.67",
      "shortVolume": "8765.43"
    }
    """
    target_symbol = PAIR_CONFIG[pair.upper()]["myfxbook_symbol"]

    for sym in symbols:
        name = sym.get("name", "")
        if name.upper().replace(" ", "") == target_symbol.upper().replace(" ", ""):
            try:
                rec = SentimentRecord(
                    timestamp  = datetime.now(timezone.utc).isoformat(),
                    pair       = pair.upper(),
                    source     = "myfxbook",
                    long_pct   = sym.get("longPercentage",  50.0),
                    short_pct  = sym.get("shortPercentage", 50.0),
                    long_lots  = float(sym.get("longVolume",  0) or 0),
                    short_lots = float(sym.get("shortVolume", 0) or 0),
                )
                log.info(
                    "sentiment.myfxbook.parsed",
                    pair=pair,
                    long_pct=rec.long_pct,
                    short_pct=rec.short_pct,
                )
                return rec
            except Exception as exc:
                log.warning("sentiment.myfxbook.parse_error", pair=pair, error=str(exc))
                return None

    log.warning("sentiment.myfxbook.symbol_not_found", pair=pair, target=target_symbol)
    return None


# ── Source 2: OANDA ───────────────────────────────────────────────────────────

@_RETRY
def _fetch_oanda_book(instrument: str, book_type: str = "openPositions") -> dict:
    """
    Fetch OANDA open positions or open orders for an instrument.
    book_type: "openPositions" or "openOrders"
    Requires OANDA_API_KEY in .env.
    """
    if not OANDA_API_KEY:
        raise EnvironmentError("OANDA_API_KEY not set in .env")

    url = f"{OANDA_BASE_URL}/v3/instruments/{instrument}/{book_type}"
    log.info("sentiment.oanda.fetch", url=url)

    resp = _SESSION.get(
        url,
        headers={"Authorization": f"Bearer {OANDA_API_KEY}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _parse_oanda_book(data: dict, pair: str) -> Optional[SentimentRecord]:
    """
    Parse OANDA order book JSON into a SentimentRecord.

    OANDA book schema:
    {
      "orderBook": {
        "instrument": "EUR_USD",
        "time": "...",
        "buckets": [
          {"price": "1.0850", "longCountPercent": "2.15", "shortCountPercent": "1.87"},
          ...
        ]
      }
    }
    Aggregate all buckets to get total long% and short%.
    """
    book_key = "orderBook" if "orderBook" in data else "positionBook"
    book     = data.get(book_key, {})
    buckets  = book.get("buckets", [])

    if not buckets:
        log.warning("sentiment.oanda.empty_buckets", pair=pair)
        return None

    total_long  = sum(float(b.get("longCountPercent",  0) or 0) for b in buckets)
    total_short = sum(float(b.get("shortCountPercent", 0) or 0) for b in buckets)

    # Normalise to 100%
    total = total_long + total_short
    if total <= 0:
        return None

    long_pct  = (total_long  / total) * 100
    short_pct = (total_short / total) * 100

    try:
        rec = SentimentRecord(
            timestamp = datetime.now(timezone.utc).isoformat(),
            pair      = pair.upper(),
            source    = "oanda",
            long_pct  = long_pct,
            short_pct = short_pct,
        )
        log.info(
            "sentiment.oanda.parsed",
            pair=pair,
            long_pct=round(long_pct, 2),
            short_pct=round(short_pct, 2),
        )
        return rec
    except Exception as exc:
        log.warning("sentiment.oanda.parse_error", pair=pair, error=str(exc))
        return None


# ── Source 3: Synthetic (RSI-based contrarian proxy) ─────────────────────────

def _fetch_synthetic_sentiment(pair: str) -> Optional[SentimentRecord]:
    """
    Derive a sentiment proxy from RSI when live feeds are unavailable.

    Logic:
    - RSI > 70 → assume 70% retail long (overbought euphoria)
    - RSI < 30 → assume 30% retail long (oversold capitulation)
    - 50-65 range → neutral
    - Result is a CONTRARIAN signal — we fade the retail position.
    """
    try:
        import yfinance as yf
        ticker = PAIR_CONFIG[pair.upper()]["yfinance_ticker"]
        log.info("sentiment.synthetic.fetch", pair=pair, ticker=ticker)

        df = yf.download(ticker, period=SYNTHETIC_LOOKBACK, interval="1d", progress=False)
        if df.empty:
            return None

        close = df["Close"].squeeze()
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(SYNTHETIC_RSI_PERIOD).mean()
        loss  = (-delta.clip(upper=0)).rolling(SYNTHETIC_RSI_PERIOD).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi   = (100 - (100 / (1 + rs))).iloc[-1]

        # Map RSI to implied retail long%
        # RSI=70+ → 72% long (contrarian short signal)
        # RSI=30- → 28% long (contrarian long signal)
        # RSI=50  → 50% (neutral)
        long_pct = float(max(5.0, min(95.0, (rsi - 50) * 0.44 + 50)))
        short_pct = 100.0 - long_pct

        rec = SentimentRecord(
            timestamp    = datetime.now(timezone.utc).isoformat(),
            pair         = pair.upper(),
            source       = "synthetic",
            long_pct     = long_pct,
            short_pct    = short_pct,
            quality_flag = 2,  # always flag synthetic as degraded
        )
        log.info(
            "sentiment.synthetic.ok",
            pair=pair,
            rsi=round(float(rsi), 2),
            implied_long_pct=round(long_pct, 2),
        )
        return rec

    except Exception as exc:
        log.error("sentiment.synthetic.failed", pair=pair, error=str(exc))
        return None


# ── Z-score & contrarian logic ────────────────────────────────────────────────

def _enrich_zscore_and_contrarian(
    current: SentimentRecord,
    history: Optional[pd.DataFrame] = None,
) -> SentimentRecord:
    """
    Compute z-score of net_bias over rolling history.
    Apply contrarian flip when |z| > EXTREME_THRESHOLD.

    At extremes, retail sentiment is a fade signal:
        crowd is too long → we go short
        crowd is too short → we go long
    """
    if history is None or history.empty or "net_bias" not in history.columns:
        # No history: use scalar signal without z-score
        current.sentiment_signal = current.net_bias
        return current

    hist_bias = history["net_bias"].dropna()
    if len(hist_bias) < 5:
        current.sentiment_signal = current.net_bias
        return current

    mu    = hist_bias.rolling(ZSCORE_WINDOW_DAYS, min_periods=5).mean().iloc[-1]
    sigma = hist_bias.rolling(ZSCORE_WINDOW_DAYS, min_periods=5).std().iloc[-1]

    if sigma > 0:
        z = (current.net_bias - mu) / sigma
    else:
        z = 0.0

    current.is_extreme = abs(z) > EXTREME_THRESHOLD

    # Contrarian flip at extremes
    if current.is_extreme:
        current.sentiment_signal = round(-current.net_bias, 4)
        log.info(
            "sentiment.contrarian_flip",
            pair=current.pair,
            net_bias=current.net_bias,
            z_score=round(z, 3),
        )
    else:
        current.sentiment_signal = round(current.net_bias, 4)

    return current


# ── Composite builder ─────────────────────────────────────────────────────────

def _build_composite(records: list[SentimentRecord]) -> SentimentRecord:
    """
    Blend multiple sentiment sources into one composite record.
    Weights: Myfxbook (0.5) > OANDA (0.3) > Synthetic (0.2)
    """
    source_weights = {"myfxbook": 0.5, "oanda": 0.3, "synthetic": 0.2}
    source_map     = {r.source: r for r in records}

    total_weight = sum(source_weights[s] for s in source_map)
    if total_weight == 0:
        raise ValueError("No valid sentiment sources")

    blended_long = sum(
        source_map[s].long_pct * source_weights[s]
        for s in source_map
    ) / total_weight

    blended_short = sum(
        source_map[s].short_pct * source_weights[s]
        for s in source_map
    ) / total_weight

    # Quality flag based on sources available
    if "myfxbook" in source_map and "oanda" in source_map:
        quality = 0
    elif "myfxbook" in source_map or "oanda" in source_map:
        quality = 1
    else:
        quality = 2

    pair   = records[0].pair
    source = "myfxbook" if len(source_map) == 1 else "myfxbook"

    rec = SentimentRecord(
        timestamp    = datetime.now(timezone.utc).isoformat(),
        pair         = pair,
        source       = source,   # label dominant source
        long_pct     = round(blended_long, 4),
        short_pct    = round(blended_short, 4),
        quality_flag = quality,
    )
    log.info(
        "sentiment.composite",
        pair=pair,
        sources=list(source_map.keys()),
        long_pct=round(blended_long, 2),
        quality_flag=quality,
    )
    return rec


# ── Cache layer ───────────────────────────────────────────────────────────────

def _cache_path(pair: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return OUTPUT_DIR / f"sentiment_{pair.upper()}_{date_str}.parquet"


def _save_parquet(df: pd.DataFrame, pair: str) -> Path:
    path = _cache_path(pair)
    df.to_parquet(path, index=False, compression="snappy")
    log.info("sentiment.cache.saved", path=str(path))
    return path


def _load_cached(pair: str) -> Optional[pd.DataFrame]:
    path = _cache_path(pair)
    if not path.exists():
        return None
    age_minutes = (
        datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    ).total_seconds() / 60
    # Sentiment refreshes intraday — cache valid for 4 hours
    if age_minutes > 240:
        log.info("sentiment.cache.expired", age_min=round(age_minutes, 1))
        return None
    df = pd.read_parquet(path)
    log.info("sentiment.cache.hit", path=str(path))
    return df


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_sentiment(
    pair: str = "EURUSD",
    use_cache: bool = True,
    history_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Main entry point — returns a single-row DataFrame with the current
    composite sentiment reading.

    Fetch order: Myfxbook → OANDA → Synthetic (fallback)
    Composite is built from however many sources succeed.

    Parameters
    ----------
    pair        : "EURUSD" or "GBPUSD"
    use_cache   : Return cached snapshot if < 4 hours old
    history_df  : Historical sentiment DataFrame for z-score / contrarian logic

    Returns
    -------
    pd.DataFrame with columns:
        timestamp, pair, source, long_pct, short_pct,
        long_lots, short_lots, net_bias, is_extreme,
        sentiment_signal, quality_flag
    """
    pair = pair.upper()
    if pair not in PAIR_CONFIG:
        raise ValueError(f"Unsupported pair: {pair}. Use: {list(PAIR_CONFIG.keys())}")

    # ── Cache check ───────────────────────────────────────────────────────────
    if use_cache:
        cached = _load_cached(pair)
        if cached is not None:
            return cached

    records: list[SentimentRecord] = []

    # ── Source 1: Myfxbook ────────────────────────────────────────────────────
    try:
        symbols = _mb_client.fetch_outlook()
        rec     = _parse_myfxbook(symbols, pair)
        if rec:
            records.append(rec)
    except EnvironmentError as env_err:
        log.warning("sentiment.myfxbook.skipped", reason=str(env_err))
    except Exception as exc:
        log.error("sentiment.myfxbook.error", pair=pair, error=str(exc))

    # ── Source 2: OANDA ───────────────────────────────────────────────────────
    try:
        instrument = PAIR_CONFIG[pair]["oanda_instrument"]
        raw_data   = _fetch_oanda_book(instrument, book_type="openPositions")
        rec        = _parse_oanda_book(raw_data, pair)
        if rec:
            records.append(rec)
    except EnvironmentError as env_err:
        log.warning("sentiment.oanda.skipped", reason=str(env_err))
    except Exception as exc:
        log.error("sentiment.oanda.error", pair=pair, error=str(exc))

    # ── Fallback: Synthetic ───────────────────────────────────────────────────
    if not records:
        log.warning("sentiment.all_live_sources_failed", pair=pair)
        synthetic = _fetch_synthetic_sentiment(pair)
        if synthetic:
            records.append(synthetic)

    if not records:
        log.error("sentiment.no_data", pair=pair)
        return pd.DataFrame()

    # ── Composite ─────────────────────────────────────────────────────────────
    composite = _build_composite(records) if len(records) > 1 else records[0]

    # ── Z-score / contrarian enrichment ──────────────────────────────────────
    composite = _enrich_zscore_and_contrarian(composite, history_df)

    # ── Serialise ─────────────────────────────────────────────────────────────
    df = pd.DataFrame([composite.model_dump()])
    _save_parquet(df, pair)

    return df


def fetch_sentiment_all_pairs(
    use_cache: bool = True,
    history: Optional[dict[str, pd.DataFrame]] = None,
) -> dict[str, pd.DataFrame]:
    """
    Fetch sentiment for all configured pairs.
    Returns dict keyed by pair name. Never crashes on per-pair failure.
    """
    results: dict[str, pd.DataFrame] = {}
    for pair in PAIR_CONFIG:
        try:
            hist = (history or {}).get(pair)
            results[pair] = fetch_sentiment(pair=pair, use_cache=use_cache, history_df=hist)
        except Exception as exc:
            log.error("sentiment.pair.failed", pair=pair, error=str(exc))
            results[pair] = pd.DataFrame()
    return results


def get_sentiment_signal(pair: str = "EURUSD") -> float:
    """
    Return the scalar sentiment signal ∈ [-1, +1] for the scoring engine.
    This is Feature F7_sentiment.

    Positive = net retail long (or contrarian short if extreme).
    Returns 0.0 on any failure.
    """
    try:
        df = fetch_sentiment(pair=pair)
        if df.empty:
            return 0.0
        signal = float(df["sentiment_signal"].iloc[0])
        log.info("sentiment.signal", pair=pair, signal=round(signal, 4))
        return signal
    except Exception as exc:
        log.error("sentiment.signal.failed", pair=pair, error=str(exc))
        return 0.0


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 140)
    pd.set_option("display.float_format", lambda x: f"{x:.4f}")

    for pair in ["EURUSD", "GBPUSD"]:
        print(f"\n{'─'*70}")
        print(f"  SENTIMENT — {pair}")
        print(f"{'─'*70}")

        df = fetch_sentiment(pair=pair, use_cache=False)

        if df.empty:
            print(f"  No sentiment data returned for {pair}")
            continue

        cols = [
            "timestamp", "pair", "source",
            "long_pct", "short_pct", "net_bias",
            "is_extreme", "sentiment_signal", "quality_flag",
        ]
        available = [c for c in cols if c in df.columns]
        print(df[available].to_string(index=False))

        signal = get_sentiment_signal(pair)
        print(f"\n  F7 sentiment_signal (→ scoring engine): {signal:.4f}")

    sys.exit(0)