# src/scoring/lightgbm_model.py
import pickle
import logging
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

class LGBMScorer:
    """
    Loads a versioned model from disk/S3.
    Validates AUC before allowing scoring.
    Falls back to linear model if validation fails — never crashes the pipeline.
    """

    def __init__(self, model_uri: str, min_auc: float = 0.55):
        self.model_uri = model_uri
        self.min_auc = min_auc
        self._model = None
        self._validated = False

    def load(self) -> bool:
        try:
            # In production: replace with boto3.resource('s3').download_fileobj
            with open(self.model_uri, "rb") as f:
                self._model = pickle.load(f)
            log.info(f"LightGBM model loaded from {self.model_uri}")
            return True
        except Exception as e:
            log.error(f"Model load failed: {e}")
            return False

    def validate(self, X_val: pd.DataFrame, y_val: pd.Series) -> bool:
        """
        Must pass AUC gate before being promoted to production.
        Binary classification: 1=Long, 0=Short (Neutral excluded from AUC calc).
        """
        if self._model is None:
            return False
        try:
            proba = self._model.predict_proba(X_val)[:, 1]
            auc = roc_auc_score(y_val, proba)
            log.info(f"Validation AUC: {auc:.4f} (min required: {self.min_auc})")
            self._validated = auc >= self.min_auc
            return self._validated
        except Exception as e:
            log.error(f"Validation failed: {e}")
            return False

    def score(self, features: pd.DataFrame) -> Optional[float]:
        """
        Returns blended P(Long) - P(Short) ∈ [-1, +1].
        Returns None if model not validated — caller falls back to linear.
        """
        if not self._validated or self._model is None:
            return None
        proba = self._model.predict_proba(features)[0]
        # proba: [P(Neutral), P(Short), P(Long)] — adjust index to your label encoding
        p_long, p_short = proba[2], proba[1]
        return float(np.clip(p_long - p_short, -1.0, 1.0))