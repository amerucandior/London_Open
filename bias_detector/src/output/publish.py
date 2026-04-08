# src/output/publish.py
"""
Output publisher for FX Bias signals.
Writes NDJSON to disk and produces to Kafka.
Kafka producer is injected or created lazily — never crashes on missing broker.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_OUTPUT_ROOT = Path("output")
_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

_producer = None  # lazy singleton


def _get_producer():
    global _producer
    if _producer is not None:
        return _producer
    try:
        from confluent_kafka import Producer
        conf = {"bootstrap.servers": os.environ["KAFKA_BOOTSTRAP_SERVERS"]}
        _producer = Producer(conf)
        atexit.register(_producer.flush)
        log.info("Kafka producer initialised")
    except (ImportError, KeyError) as e:
        log.warning(f"Kafka producer unavailable ({e}). File-only output mode.")
        _producer = None
    return _producer


def _write_json_file(pair: str, payload: Dict[str, Any]) -> None:
    today      = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_dir = _OUTPUT_ROOT / pair
    target_dir.mkdir(parents=True, exist_ok=True)
    with (target_dir / f"{today}.json").open("a", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False)
        fp.write("\n")


def emit(
    pair:           str,
    horizon:        str,
    bias:           str,
    score:          float,
    confidence:     Dict[str, Any],
    drivers:        List[Any],
    scenario_notes: Optional[List[Any]] = None,
    producer=None,
) -> None:
    payload = {
        "timestamp":            datetime.now(timezone.utc).isoformat(),
        "pair":                 pair,
        "horizon":              horizon,
        "bias":                 bias,
        "score":                round(score, 3),
        "confidence_pct":       confidence["pct"],
        "confidence_breakdown": confidence.get("breakdown", []),
        "drivers":              drivers,
        "scenario_notes":       scenario_notes or [],
    }
    _write_json_file(pair, payload)

    prod = producer or _get_producer()
    if prod is not None:
        prod.produce(
            topic="fx.bias.signals",
            key=pair.encode("utf-8"),
            value=json.dumps(payload).encode("utf-8"),
        )


def emit_all(scored: List[Dict[str, Any]]) -> None:
    """
    Publish all scored results. Called from the Airflow publish_task.
    `scored` is the list returned by scoring_task.
    """
    prod = _get_producer()
    for result in scored:
        emit(
            pair=result["pair"],
            horizon=result["horizon"],
            bias=result["bias"],
            score=result["score"],
            confidence=result["confidence"],
            drivers=result.get("drivers", []),
            producer=prod,
        )
    if prod is not None:
        prod.flush(timeout=10)
    log.info(f"emit_all: published {len(scored)} signals")