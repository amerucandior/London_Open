"""
London Close (LC) strategy — thin alias over :func:`london_close_plan`.

Import as ``from strategies.lc_strategy import lc_plan`` or use ``london_close_plan`` directly.
"""
from __future__ import annotations

from .london_close import london_close_plan

lc_plan = london_close_plan

__all__ = ["lc_plan"]
