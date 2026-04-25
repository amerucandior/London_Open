"""
Session parameters.

``session_clock`` uses *London* wall time (``Europe/London`` = GMT in winter, BST in summer).
When people say “07:00–08:00 GMT” for the open impulse, that is modelled as 07:00–08:00 **local**
in this timezone (align with your broker if needed).
"""

config = {
    # Broad reference (legacy helpers). Prefer ``session_clock`` for strategy logic.
    "session": {
        "asian": {"start": 0, "end": 7},
        "london": {"start": 7, "end": 16},
    },
    # All intervals below are **hour/minute in session_clock.tz_name** (same civil day).
    "session_clock": {
        "tz_name": "Europe/London",
        # Asian consolidation (range for London open breakout).
        "asian": {
            "start_hour": 0,
            "start_minute": 0,
            "end_hour": 7,
            "end_minute": 0,
        },
        # London Open **breakout** impulse window (not the whole London day).
        "london_open_impulse": {
            "start_hour": 7,
            "start_minute": 0,
            "end_hour": 8,
            "end_minute": 0,
        },
        # London Close: watch counter-trend 15–30 min before cash close hour.
        "london_close_pre": {
            "start_hour": 15,
            "start_minute": 30,
            "end_hour": 16,
            "end_minute": 0,
        },
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
    "trade_limits": {},
    "spread_limits": {},
}
