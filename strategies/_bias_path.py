"""Ensure ``bias_detector/src`` is on ``sys.path`` for ``ingestion.*`` / ``signals.*``."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BIAS_SRC = _ROOT / "bias_detector" / "src"

if _BIAS_SRC.is_dir() and str(_BIAS_SRC) not in sys.path:
    sys.path.insert(0, str(_BIAS_SRC))
