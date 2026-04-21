"""Shared BigQuery client factory.

Centralizes service-account key lookup and client construction so the
three scripts agree on where to find credentials.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from google.cloud import bigquery

logger = logging.getLogger(__name__)

SA_KEY_PATH = Path(
    os.environ.get(
        "BQ_SA_KEY_PATH",
        os.path.expanduser("~/.config/gcloud/local-scripts-sa-key.json"),
    )
)


def get_bq_client() -> Optional[bigquery.Client]:
    """Return a BigQuery client, or None if the service-account key is missing.

    Callers should treat None as "skip BigQuery writes" rather than an error,
    since BigQuery persistence is always best-effort in this codebase.
    """
    if not SA_KEY_PATH.exists():
        logger.warning("SA key file not found: %s. Skipping BigQuery operation.", SA_KEY_PATH)
        return None
    try:
        return bigquery.Client.from_service_account_json(str(SA_KEY_PATH))
    except Exception as e:  # google.auth raises a handful of exception types
        logger.warning("Failed to initialize BigQuery client: %s", e)
        return None
