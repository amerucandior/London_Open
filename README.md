# LondonOpen

A Python-based automated trading analysis tool for MetaTrader 5, specializing in forex trading strategies around the London market opening. This project implements various technical analysis components to identify trading opportunities during the volatile London session.

## Features

- **Session Management**: Tracks Asian and London trading sessions with UTC-based timing
- **Asian Range Calculation**: Computes high, low, and midpoint prices during the Asian session (00:00-06:00 UTC)
- **Liquidity Sweep Detection**: Identifies rapid price movements through thin liquidity zones with volume spikes
- **Overextension Entry Model**: Measures price deviation from EMA50 to detect overextended market conditions
- **Economic Calendar Integration**: Placeholder for economic event monitoring (future implementation)
- **MT5 Integration**: Direct connection to MetaTrader 5 for real-time data and trading

## Components

### Core Modules
- `main.py`: Main entry point with MT5 initialization and symbol validation
- `config.py`: Configuration settings for sessions, trade limits, and spreads
- `session.py`: Session timing and trade permission logic

### Analysis Modules
- `asian_range.py`: Calculates Asian session price ranges
- `liquidity_sweep.py`: Detects liquidity sweeps using price and volume analysis
- `economic_calendar.py`: Economic event calendar (currently placeholder)

### Entry Models
- `entry-model/overextension.py`: Overextension factor based on EMA50 deviation

## Installation

1. Ensure you have Python 3.12 or higher installed
2. Install MetaTrader 5 platform
3. Clone this repository
4. Install dependencies:
   ```bash
   uv pip install -r requirements.txt
   ```
   Or if using pyproject.toml:
   ```bash
   uv pip install -e .
   ```

## Dependencies

- MetaTrader5
- pandas
- numpy
- pytz

## Configuration

Edit `config.py` to customize:
- Session times (UTC)
- Trade limits
- Spread limits

## Usage

1. Ensure MetaTrader 5 is running
2. Run the main script:
   ```bash
   python main.py
   ```

## Trading Strategy Overview

The system focuses on the London open (07:00-16:00 UTC) and uses:
- Asian range as reference levels
- Liquidity sweeps for entry signals
- Overextension to avoid entering during extreme moves
- Session timing to restrict trading to optimal hours

## Disclaimer

This is a trading analysis tool for educational and research purposes. Use at your own risk. Always test strategies in a demo environment before live trading.

## License

[MIT License

Copyright (c) 2026 Mwirigi Ian

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.]