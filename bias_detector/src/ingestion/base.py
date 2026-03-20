# src/ingestion/base.py
import time
import logging
from typing import Callable, TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)

def fetch_with_retry(
    fn: Callable[[], T],
    source: str,
    max_attempts: int = 5,
    base_delay: float = 2.0,
) -> T:
    """
    Exponential back-off retry wrapper.
    Raises RuntimeError after max_attempts — caller decides how to handle.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn()
            log.info(f"[{source}] fetch OK (attempt {attempt})")
            return result
        except Exception as exc:
            wait = base_delay ** attempt
            log.warning(f"[{source}] attempt {attempt} failed: {exc}. Retry in {wait:.1f}s")
            if attempt == max_attempts:
                raise RuntimeError(f"[{source}] all {max_attempts} attempts exhausted") from exc
            time.sleep(wait)
    raise RuntimeError("Unreachable runtime error loop exited unexpectedly")