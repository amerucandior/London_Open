# ──────────────────────────────────────────────────────────────────────────────
# emit.py
# ──────────────────────────────────────────────────────────────────────────────
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from confluent_kafka import Producer

# from kafka_producer import get_producer

# ----------------------------------------------------------------------
# Helper – make sure the output directory exists only once per process.
# ----------------------------------------------------------------------
_OUTPUT_ROOT = Path("output")
_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


def _write_json_file(pair: str, payload: Dict[str, Any]) -> None:
    """
    Write a JSON line to a daily file under ``output/<pair>/YYYY-MM-DD.json``.
    The file is opened in *append* mode so we never truncate previous data.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_dir = _OUTPUT_ROOT / pair
    target_dir.mkdir(parents=True, exist_ok=True)          # cheap if already exists
    target_file = target_dir / f"{today}.json"
    # Append a newline‑terminated JSON record (NDJSON style)
    with target_file.open("a", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False)
        fp.write("\n")


def emit(
    pair: str,
    horizon: str,
    bias: str,
    score: float,
    confidence: Dict[str, Any],
    drivers: List[Any],
    scenario_notes: Optional[List[Any]] = None,
    *,
    producer: Optional[Producer] = None,          # allow DI / testing
) -> None:
    """
    Build the payload, write it to disk, and push it to Kafka.

    Parameters
    ----------
    pair, horizon, bias, score, confidence, drivers, scenario_notes
        Business-level fields that compose the payload.
    producer
        Optional explicit ``Producer`` (useful for unit tests).  If omitted,
        the module-level singleton from ``kafka_producer.py`` is used.
    """
    # ------------------------------------------------------------------
    # 1️⃣  Build a *fresh* timestamp per call – the previous code used a
    #     module‑level ``now`` that never changed.
    # ------------------------------------------------------------------
    now = datetime.now(timezone.utc).isoformat()

    payload = {
        "timestamp": now,
        "pair": pair,
        "horizon": horizon,
        "bias": bias,
        "score": round(score, 3),
        "confidence_pct": confidence["pct"],
        "confidence_breakdown": confidence["breakdown"],
        "drivers": drivers,
        "scenario_notes": scenario_notes or [],
    }

    # ------------------------------------------------------------------
    # 2️⃣  Write to a daily NDJSON file.
    # ------------------------------------------------------------------
    _write_json_file(pair, payload)

    # ------------------------------------------------------------------
    # 3️⃣  Send to Kafka – *no* per‑call flush.
    # ------------------------------------------------------------------
    prod = producer #or get_producer()
    prod.produce(
        topic="fx.bias.signals",
        value=json.dumps(payload).encode("utf-8"),
        # Optional: you can add a key if you want ordering per pair
        # key=pair.encode("utf-8"),
    )
    # If you really need to guarantee delivery for a *single* high‑value
    # message you could call ``prod.flush(timeout=5)`` **only** in that
    # special branch – not on every normal emit.
# ─────────────────────────────────────────────────────────────────────────────