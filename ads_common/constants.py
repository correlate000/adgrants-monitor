"""Google Ads shared constants"""

import os

# BigQuery
BQ_PROJECT_ID = os.environ.get("BQ_PROJECT_ID", "")
BQ_DATASET_ID = os.environ.get("BQ_DATASET_ID", "")
BQ_BATCH_SIZE = 1000

# CTR thresholds (Ad Grants requires >= 5% account-wide CTR)
CTR_CRITICAL_THRESHOLD = 0.05  # 5% -> CRITICAL
CTR_WARNING_THRESHOLD = 0.07   # 7% -> WARNING
CTR_EARLY_WARNING_THRESHOLD = 0.06  # 6% -> EARLY WARNING
CTR_AD_GROUP_MIN = 0.05
CTR_KW_PAUSE_100IMP = 0.03
CTR_KW_PAUSE_50IMP = 0.02
QS_MIN = 3

# Auto-PAUSE limits (safety guards)
MAX_PAUSE_PER_RUN = 3
MAX_PAUSE_PER_DAY = 5
MIN_ENABLED_KEYWORDS = 5
EXCLUSION_GRADUATION_MIN_IMP = 10

# Auto-exclude limits (analyze_search_terms)
MAX_AUTO_EXCLUDE_PER_RUN = 5  # Safety guard for automated exclusion

# Tier promotion criteria
TIER_PROMOTION_MIN_DAYS = 14   # Minimum 14 days of observation
TIER_PROMOTION_MIN_CTR = 0.05  # CTR >= 5%
TIER_PROMOTION_MIN_QS = 3      # Quality Score >= 3
