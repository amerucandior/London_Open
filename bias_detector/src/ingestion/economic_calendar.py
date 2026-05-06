"""
Canonical import path for calendar ingestion.

This module keeps a stable, non-stdlib-conflicting name while re-exporting the
existing implementation from ``ingestion.calendar``.
"""
from __future__ import annotations

try:
    from .calendar import *  # noqa: F401,F403
    from .calendar import _run_cli_test
except ImportError:
    from ingestion.calendar import *  # type: ignore # noqa: F401,F403
    from ingestion.calendar import _run_cli_test  # type: ignore


if __name__ == "__main__":
    _run_cli_test()

