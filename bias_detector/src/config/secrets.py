# src/config/secrets.py
import os
import boto3
import json
from functools import lru_cache

@lru_cache(maxsize=1)
def get_secrets() -> dict:
    """
    Fetches all API keys from AWS Secrets Manager.
    Cached in-process — one network call per container lifecycle.
    """
    secret_name = os.getenv("SECRET_NAME", "fx-bias-detector/prod")
    region = os.getenv("AWS_REGION", "eu-west-1")

    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])

# Usage in any module:
# from src.config.secrets import get_secrets
# keys = get_secrets()
# oanda_key = keys["OANDA_API_KEY"]