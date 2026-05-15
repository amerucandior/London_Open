"""
Session parameters.
"Session_clock" uses *London* wall time (``Europe/London`` = GMT in winter, BST in summer).
When people say “07:00–08:00 GMT” for the open impulse, that is modelled as 07:00–08:00 **local**
in this timezone (align with your broker if needed).
"""

config = {
    # Broad reference (legacy helpers). Prefer ``session_clock`` for strategy logic.
    "session": {
        "asian": {"start": 0, "end": 4},
        "london": {"start": 6, "end": 16},
    },
    # All intervals below are **hour/minute in session_clock.tz_name** (same civil day).
    # Anchored to the ICT London Killzone (02:00–05:00 NY = 07:00–10:00 Europe/London,
    # year-round via tz database — handles BST/GMT automatically).
    "session_clock": {
        "tz_name": "Europe/London",
        # Overnight consolidation range (Asia + Sydney + early Frankfurt).
        # Ends one hour before the killzone so the impulse breaks a fully-formed range.
        "asian": {
            "start_hour": 0,
            "start_minute": 0,
            "end_hour": 6,
            "end_minute": 0,
        },
        # London Open **breakout** impulse window — ICT killzone, not pre-cash-open.
        "london_open_impulse": {
            "start_hour": 7,
            "start_minute": 0,
            "end_hour": 10,
            "end_minute": 0,
        },
        # London Close: 30-min pre-close watch for counter-trend reversion setup.
        "london_close_pre": {
            "start_hour": 15,
            "start_minute": 30,
            "end_hour": 16,
            "end_minute": 0,
        },
        # London cash close hour (LSE closes 16:30; institutional rebalance flow runs
        # into 17:00). Mean-reversion entries are bounded to this hour.
        "london_close": {
            "start_hour": 16,
            "start_minute": 0,
            "end_hour": 17,
            "end_minute": 0,
        },
        # Flat / exit London Close style trades before this local time (NY / day risk).
        "lc_flat_by_local": {"hour": 21, "minute": 0},
    },
    "calendar": {
        "risk_hours_ahead": 8,
        "min_impact": "High",
        "block_if_high_impact_in_window": True,
    },
    # CBDR: NY 14:00–20:00 prior session → LoD/HoD 1σ / 2σ extensions (see centralbank_dealers_range).
    "cbdr": {
        "use_wicks": False,
        "ideal_min_pips": 20,
        "ideal_max_pips": 30,
        "max_allowed_pips": 40,
    },
    # Per-day drawdown cap in account currency (0 = disabled).
    # Max open positions: 1 per pair, 4 globally.
    "trade_limits": {
        "daily_loss_limit": 200.0,
        "max_positions_per_pair": 1,
        "max_positions_global": 4,
    },
    # Maximum allowable spread in pips before an order is rejected.
    "spread_limits": {
        "default_max_pips": 3.0,
        "EURUSD": 12.0,
        "GBPUSD": 15.5,
    },
}
