# London Open

Python tooling for **MetaTrader 5**-backed analysis around the **London session**: London Open breakout context, London Close mean-reversion context, Asian range, CBDR (central bank dealers range), COT positioning, and economic calendar filters.

## Layout

- **`strategies/`** — `london_open_plan`, `london_close_plan`, CBDR wiring, shared market context.
- **`bias_detector/src/`** — `ingestion` (COT, calendar) and `signals` (regime); added to `sys.path` at runtime via `strategies/_bias_path.py`.
- **Root modules** — `main.py`, `config.py`, `session.py`, `asian_range.py`, etc.

## Requirements

Python 3.12+, dependencies in `pyproject.toml`. **MetaTrader5** is Windows-oriented; install and run MT5-backed paths on a supported platform.

## Run

```bash
python main.py
```

Configure sessions and windows in `config.py` (`session_clock`).
