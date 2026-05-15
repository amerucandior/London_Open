"""Shared fixtures for the test suite."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path so bare ``import config`` etc. work.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
