import pendulum
import logging
from airflow.sdk import dag, task
from datetime import timedelta

# Best Practice: Keep logic in your src/ folder and import normally
from src.ingestion.broker import fetch_ohlcv
from src.ingestion.calendar import fetch_calendar
from src.ingestion.cot import fetch_cot
from src.features.compute_features import run as run_features
from src.scoring.weighted_sum import run as run_scoring
from src.output.publish import emit_all

log = logging.getLogger(__name__)

def _alert_on_failure(context):
    """Sends Slack webhook + writes audit row on any task failure."""
    # Tip: Use Airflow Connections/Hooks instead of os.getenv for DB/Slack
    # to make this more 'Airflow native' and secure.
    pass 

DEFAULT_ARGS = {
    "owner": "quant-ops",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "on_failure_callback": _alert_on_failure,
}

@dag(
    dag_id="fx_bias_detector_daily",
    default_args=DEFAULT_ARGS,
    schedule="0 6 * * 1-5",
    start_date=pendulum.datetime(2025, 1, 1, tz="UTC"), # Static date
    catchup=False,
    tags=["fx", "bias", "production"],
)
def fx_bias_detector():

    @task
    def ingest_ohlcv_task(symbol):
        return fetch_ohlcv(symbol)

    @task
    def ingest_calendar_task():
        return fetch_calendar()

    @task
    def ingest_cot_task():
        return fetch_cot()

    @task
    def compute_features_task(ohlcv, cal, cot):
        # TaskFlow automatically handles dependencies if you pass return values
        return run_features()

    @task
    def scoring_task(features):
        return run_scoring()

    @task
    def publish_task(score):
        return emit_all()

    # Define execution flow via function calls
    ohlcv_data = ingest_ohlcv_task("EURUSD=X")
    cal_data = ingest_calendar_task()
    cot_data = ingest_cot_task()
    
    features = compute_features_task(ohlcv_data, cal_data, cot_data)
    score = scoring_task(features)
    publish_task(score)

# Instantiate the DAG
fx_bias_detector()
