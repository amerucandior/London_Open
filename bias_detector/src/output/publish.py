import json
from datetime import datetime, timezone
from confluent_kafka import Producer   # Kafka client
from pathlib import Path

now = datetime.now(timezone.utc)

def emit(pair: str, horizon: str, bias: str, score: float, confidence: dict, drivers: list,
         scenario_notes: list| None = None):
    payload = {
        "timestamp": now.isoformat(),
        "pair": pair,
        "horizon": horizon,
        "bias": bias,
        "score": round(score, 3),
        "confidence_pct": confidence["pct"],
        "confidence_breakdown": confidence["breakdown"],
        "drivers": drivers,
        "scenario_notes": scenario_notes or []
    }
    # 1️⃣ write to file (daily folder)
    Path(f"output/{pair}/{now:%Y-%m-%d}.json").write_text(
        json.dumps(payload, ensure_ascii=False) + "\n"
    )
    # 2️⃣ push to Kafka (topic must be registered in schema registry)
    producer = Producer({"bootstrap.servers": "kafka:9092"})
    producer.produce("fx.bias.signals", json.dumps(payload).encode())
    producer.flush()
