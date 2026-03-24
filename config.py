"""Google Ads script common configuration"""
import os

CUSTOMER_ID = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")
CAMPAIGN_NAME = "IT適性診断 - 検索"
SURVEY_CAMPAIGN_NAME = "ISVD調査 - 検索"
ARTICLE_CAMPAIGN_NAME = "ISVD記事 - 検索"

# All monitored campaigns (used by monitor_ad_performance.py)
# NOTE: When adding a new campaign, always update this list and CAMPAIGN_PAUSE_TYPE simultaneously
CAMPAIGN_NAMES = [
    "IT適性診断 - 検索",
    "ISVD記事 - 検索",
    "ISVD調査 - 検索",
    "Example Campaign - Search",
]

# Per-campaign PAUSE strategy type (used by monitor_ad_performance.py)
# "cv": CV-focused (pause when clicks consumed without CV)
# "ctr": Access-focused (pause on CTR drop)
# None: Not subject to auto-PAUSE (only stop QS<=1)
# NOTE: Keys are the part of CAMPAIGN_NAMES before " - 検索" (prefix match)
CAMPAIGN_PAUSE_TYPE = {
    "IT適性診断": "cv",
    "ISVD記事": "ctr",
    "ISVD調査": "ctr",
    "Example Campaign": None,
}

# ---- URL management ----
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://example.org")
CONTENT_PATHS = {
    "guides": "/guides/",
    "columns": "/columns/",
}
