# London Open

Python tooling for **MetaTrader 5**-backed analysis around the **London session**: London Open breakout context, London Close mean-reversion context, Asian range, CBDR (central bank dealers range), COT positioning, and economic calendar filters.

## Requirements

- **Python 3.12+**
- **MetaTrader 5** terminal (Windows) — the `MetaTrader5` Python package is Windows-only; a stub (`utils/mt5_stub.py`) is provided for Linux/Colab development.
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Setup

```bash
# Clone the repo
git clone git@github.com:amerucandior/London_Open.git && cd London_Open

# Install dependencies (pick one)
uv sync                  # uses uv.lock
pip install -e .         # editable install via pyproject.toml
pip install -r requirements.txt  # pinned full environment
```

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

## Environment Variables

| Variable | Description |
|---|---|
| `FRED_API_KEY` | API key for FRED (Federal Reserve Economic Data). Required by bias-detector ingestion modules. |

> Add any additional keys to `.env` as needed. The app loads them via **python-dotenv**.

## Usage

### One-shot analysis (no live connection required)

```bash
python main.py
```

Prints London Open breakout and London Close mean-reversion plans for EURUSD.

### Live trading loop

```bash
python run_live.py
python run_live.py --pairs EURUSD GBPUSD --poll 2.0 --db runtime_state.sqlite
```

| Flag | Default | Description |
|---|---|---|
| `--pairs` | `EURUSD GBPUSD` | Symbols to watch |
| `--poll` | `2.0` | Bar-feed polling interval (seconds) |
| `--db` | `runtime_state.sqlite` | SQLite state file path |

### Configuration

Session windows, calendar filters, CBDR parameters, trade/spread limits are all in **`config.py`** (`session_clock` uses `Europe/London` timezone).

## Project Structure

```
London_Open/
├── main.py                  # One-shot analysis entry point
├── run_live.py              # Live trading entry point
├── config.py                # Session & risk parameters
├── session.py               # Session time helpers
├── asian_range.py           # Asian session range calculation
├── centralbank_dealers_range.py  # CBDR logic
├── liquidity_sweep.py       # Liquidity sweep detection
│
├── strategies/              # Strategy modules
│   ├── london_open.py       #   London Open breakout plan
│   ├── london_close.py      #   London Close mean-reversion plan
│   ├── lc_strategy.py       #   LC strategy helpers
│   ├── cbdr_context.py      #   CBDR wiring
│   ├── market_context.py    #   Shared market context
│   ├── overextension.py     #   Overextension filter
│   └── _bias_path.py        #   Adds bias_detector/src to sys.path
│
├── bias_detector/src/       # Macro bias detection
│   ├── ingestion/           #   COT, calendar, economic calendar scrapers
│   └── signals/             #   Regime classification
│
├── runtime/                 # Live-loop infrastructure
│   ├── loop.py              #   Main polling loop
│   ├── bar_feed.py          #   MT5 bar feed
│   ├── broker.py            #   Order execution
│   ├── risk.py              #   Risk management
│   └── state.py             #   SQLite state persistence
│
├── utils/                   # Helpers
│   ├── mt5_utils.py         #   MT5 connection utilities
│   └── mt5_stub.py          #   MT5 stub for non-Windows dev
│
├── data/raw/                # Cached data (calendar, COT parquets)
├── pyproject.toml           # Package metadata & dependencies
├── requirements.txt         # Pinned full dependency list
├── uv.lock                  # uv lockfile
└── LICENSE
```

## Tests

Run the full test suite with [pytest](https://docs.pytest.org/):

```bash
uv run python -m pytest tests/ -v
```

| Test file | Covers |
|---|---|
| `tests/test_session.py` | Session time gates, timezone conversion, open/close windows |
| `tests/test_cbdr.py` | CBDR range calculation, sigma levels, LoD/HoD projection |
| `tests/test_overextension.py` | EMA stretch filter (`Overextension` class) |
| `tests/test_state.py` | SQLite state store (flags, positions, P&L, VWAP) |
| `tests/test_risk.py` | Risk gates (kill switch, daily loss, max positions) |

## License

**All rights reserved.** Copyright © 2026 Ian Mwirigi. See [LICENSE](LICENSE) for details.
