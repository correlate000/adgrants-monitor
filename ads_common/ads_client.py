"""Shared Google Ads client factory."""

from __future__ import annotations

from pathlib import Path

from google.ads.googleads.client import GoogleAdsClient

DEFAULT_YAML_PATH = Path(__file__).resolve().parent.parent / "google-ads.yaml"


def get_google_ads_client(yaml_path: Path | str | None = None) -> GoogleAdsClient:
    """Load a GoogleAdsClient from a YAML file.

    Defaults to <repo root>/google-ads.yaml so all three scripts agree on
    the same credential file regardless of their working directory.
    """
    path = Path(yaml_path) if yaml_path else DEFAULT_YAML_PATH
    return GoogleAdsClient.load_from_storage(str(path))
