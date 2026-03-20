# src/monitoring/metrics.py
from prometheus_client import Counter, Gauge, Histogram, start_http_server
import time

# Counters
BIAS_TOTAL = Counter(
    "fx_bias_total",
    "Count of bias signals emitted",
    ["pair", "horizon", "bias"]
)
ERRORS_TOTAL = Counter(
    "fx_bias_errors_total",
    "Count of pipeline errors",
    ["stage"]
)

# Gauges (current state)
CONFIDENCE_GAUGE = Gauge(
    "fx_bias_confidence",
    "Current confidence score",
    ["pair", "horizon"]
)
SCORE_GAUGE = Gauge(
    "fx_bias_score",
    "Current raw score",
    ["pair", "horizon"]
)
DATA_QUALITY_GAUGE = Gauge(
    "fx_bias_data_quality",
    "Data quality haircut factor (1.0 = perfect)",
    ["pair"]
)

# Latency
PIPELINE_LATENCY = Histogram(
    "fx_bias_pipeline_latency_seconds",
    "End-to-end pipeline latency",
    ["pair"],
    buckets=[1, 5, 10, 30, 60, 120]
)

def start_metrics_server(port: int = 8000):
    start_http_server(port)

def record_output(pair: str, horizon: str, bias: str, score: float, confidence: int):
    BIAS_TOTAL.labels(pair=pair, horizon=horizon, bias=bias).inc()
    CONFIDENCE_GAUGE.labels(pair=pair, horizon=horizon).set(confidence)
    SCORE_GAUGE.labels(pair=pair, horizon=horizon).set(score)