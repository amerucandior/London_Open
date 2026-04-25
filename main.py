"""
Print **London Open breakout** vs **London Close mean reversion** plans (same day, different edges).

Requires: pandas, requests, … (see pyproject) + MT5 for Asian range figures when available.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> None:
    pair = "EURUSD"
    try:
        from strategies.london_close import london_close_plan
        from strategies.london_open import london_open_plan
    except ImportError as exc:
        print("Import failed — install dependencies:", exc)
        sys.exit(1)

    print("=== London Open BREAKOUT (impulse / range break) ===")
    print(json.dumps(london_open_plan(pair), indent=2, default=str))
    print()
    print("=== London Close MEAN REVERSION (session unwind) ===")
    print(json.dumps(london_close_plan(pair), indent=2, default=str))


if __name__ == "__main__":
    main()
