#!/usr/bin/env python3
"""
Google Ads campaign performance monitoring script
Monitoring and auto-response tool to maintain Ad Grants CTR standard (account-wide 5%+)

Usage:
  python monitor_ad_performance.py                    # Report for past 7 days (report only)
  python monitor_ad_performance.py --days 14          # Past 14 days
  python monitor_ad_performance.py --auto-pause       # Auto-pause low-CTR keywords
  python monitor_ad_performance.py --auto-pause --dry-run  # Dry run (no actual pause)
  python monitor_ad_performance.py --json-only        # JSON output only
  python monitor_ad_performance.py --undo             # Undo all latest PAUSEs
  python monitor_ad_performance.py --undo-date 2026-02-18  # Undo PAUSEs on specified date
"""

import argparse
import json
import logging
import os
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import analyze_search_terms as search_terms_mod

import httpx
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.api_core.exceptions import BadRequest
from google.cloud import bigquery
from google.protobuf import field_mask_pb2

from config import CAMPAIGN_NAME, CAMPAIGN_NAMES, CAMPAIGN_PAUSE_TYPE, CUSTOMER_ID
from ads_common.email import send_alert_email
from ads_common.gaql import validate_gaql_value

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Ad Grants CTR thresholds
CTR_CRITICAL_THRESHOLD = 0.05   # 5% or below -> CRITICAL (risk of suspension)
CTR_WARNING_THRESHOLD = 0.07    # 7% or below -> WARNING (approaching danger zone)
CTR_AD_GROUP_MIN = 0.05         # Per ad group: alert if < 5%
QS_MIN = 3                      # Ad Grants minimum quality score

# Ad-group level auto-pause (added 2026-07-28)
# Background: a low-CTR ad group used to raise a WARNING only, and was never paused --
# auto-pause covered keywords alone. Worse, e-mail escalation only fires on CRITICAL,
# so those WARNINGs reached nobody. One ad group sat at 2.15-4.37% CTR for 11 days
# before degrading to 1.47% and putting the whole account at risk. Close the loop here.
AG_PAUSE_MIN_IMP = 30           # Minimum impressions to judge (keep this floor small)
AG_PAUSE_MAX_PER_RUN = 10       # Blast-radius cap for unattended runs; rest is deferred
AG_MIN_ENABLED = 20             # Never pause below this many enabled ad groups
AG_ALERT_CRITICAL_MIN_IMP = 100  # 5% breach at this volume is CRITICAL (i.e. e-mailed)

# Never pause an ad group that is converting (added 2026-08-04)
# Background: an ad group was paused for a 2.78% CTR over a 7-day window (144
# impressions, 4 clicks). That same ad group produced 13 of the account's 23
# conversions that month -- 57% of everything the account achieved -- at a
# click-to-conversion rate of 34.2%, by far the best in the account. We paused the
# only thing that was working, in order to protect a click-through rate.
#
# It did not even protect the rate. Over 30 days that ad group ran at 6.22% CTR
# against an account average of 5.34%; including it makes the account CTR 0.005
# points *higher*. The 7-day window happened to catch a bad slice.
#
# It carries 0.57% of impressions, so protecting it costs nothing measurable, while
# pausing it costs 57% of results. The trade is asymmetric, so err on protecting.
#
# Note we do NOT raise AG_PAUSE_MIN_IMP. A larger eligibility floor lets borderline
# offenders slip through every run. This is not a floor -- it is protection by value,
# applied on a separate axis.
AG_PAUSE_CV_PROTECT = 1.0       # Conversions in-window at or above this: never auto-pause

# Impression surge detection (added 2026-07-28)
# Compare against the previous run's report JSON to catch an ad group whose
# impressions explode while CTR collapses, within the same day.
AG_SURGE_RATIO = 2.0            # Impressions grew at least this many times
AG_SURGE_MIN_IMP = 200          # ...and reached at least this absolute volume

# Long-tail operation (added 2026-07-28)
# "If the monthly 5% has room to spare, keep running the low-CTR long-tail articles."
# Treat the amount by which month-to-date beats the target as a click budget, and
# spend it on low-CTR delivery. No budget -> strict pausing, as before.
#
#   surplus clicks = MTD clicks - target CTR * MTD impressions
#   cost of keeping an ad group = days left * daily impressions * (target CTR - its CTR)
#
# Cheapest (i.e. highest CTR) ad groups are kept first, so a month with more room
# automatically runs a wider long tail, and a tight month tightens itself.
LONGTAIL_TARGET_CTR = 0.053      # Month-end goal (5% requirement + 0.3pt safety margin)
LONGTAIL_BUDGET_USE_RATIO = 0.6  # Share of surplus spent on long tail (rest is reserve)
LONGTAIL_MIN_MTD_IMP = 5000      # Below this MTD volume the budget is not measurable
LONGTAIL_RESUME_MARGIN = 1.5     # Resume only with 1.5x the cost in budget (anti-flapping)
LONGTAIL_RESUME_MAX_PER_RUN = 10  # Resume cap per run

# Trial slots for brand-new articles (added 2026-08-02)
# The article sync creates every new ad group PAUSED and registers it in
# ag_pause_log.json with source="new_article_sync" / impressions_7d=0.
# Those never-delivered groups are eased into delivery a few at a time.
#
# Why not enable them all at once: in 2026-06 a jump from 793 to 5,782 broad-match
# keywords dropped the weekly CTR from 8.81% to 4.50%. New keywords are phrase match
# now, which is safer, but 171 articles x ~10 terms in one go is the same shape.
#
# Groups with no history cannot have their cost estimated from measurements, so a
# flat click cost is charged per trial. A tight month stops trials automatically;
# a roomy month runs through the backlog faster.
LONGTAIL_EXPLORE_MAX_PER_RUN = 5   # Trial cap per run (2 runs/day = up to 10/day)
LONGTAIL_EXPLORE_COST_CLICKS = 3.0  # Assumed cost of running one new group to month end

# Per-campaign PAUSE criteria (type definitions in config.CAMPAIGN_PAUSE_TYPE)
CV_PAUSE_MIN_CLICKS = 20        # cv type: pause if click >= 20 and CV < 0.5
CTR_KW_PAUSE_100IMP = 0.03      # ctr type: auto-pause if 100+ imp and CTR < 3%
CTR_KW_PAUSE_50IMP = 0.02       # ctr type: auto-pause if 50+ imp and CTR < 2%
# Strict line used when there is no click budget (added 2026-07-28).
# The thresholds above never touch the 3-5% band, so keywords sitting there kept
# serving even inside ad groups that were above 5% overall -- 33% of live impressions.
KW_STRICT_PAUSE_CTR = 0.05      # The requirement itself
KW_STRICT_PAUSE_MIN_IMP = 30    # Keep the eligibility floor small
CTR_EARLY_WARNING_THRESHOLD = 0.06  # 6% -> early warning

# Safety guards
MAX_PAUSE_PER_RUN = 5           # Standard quota: max pauses per run (excess carries over)
MAX_PAUSE_PER_DAY = 5           # Standard quota: max cumulative pauses per day
# Strict mode quota (no click budget = on pace to miss the monthly 5%), added 2026-07-28.
# At 5 per day the backlog cannot be cleared before month end. A real run on
# 2026-07-28 19:00 processed only 2 of 35 targets and deferred 33.
MAX_PAUSE_PER_RUN_STRICT = 20
MAX_PAUSE_PER_DAY_STRICT = 20
MIN_ENABLED_KEYWORDS = 5        # Minimum enabled keywords remaining after PAUSE
# High-confidence pauses (enough impressions but near-zero CTR = shown but never
# clicked) carry minimal misjudgment risk, so they are exempt from both count caps.
# Without this, a backlog larger than the cap used to skip ALL pauses and the
# auto-pause pipeline silently stalled.
HIGH_CONFIDENCE_CTR_THRESHOLD = 0.001  # CTR < 0.1%
HIGH_CONFIDENCE_MIN_IMP = 50           # imp >= 50

# Auto-pause exclusion list (managed via JSON)
# Do not edit directly - update pause_exclusion_list.json instead
# Graduation criteria: QS >= QS_MIN and imp >= EXCLUSION_GRADUATION_MIN_IMP
EXCLUSION_GRADUATION_MIN_IMP = 10  # Minimum impressions to graduate from exclusion list

# Report output directory
SCRIPT_DIR = Path(__file__).parent
REPORTS_DIR = SCRIPT_DIR / "reports"
YAML_PATH = SCRIPT_DIR / "google-ads.yaml"
PAUSE_LOG_PATH = SCRIPT_DIR / "pause_log.json"
PAUSE_EXCLUSION_FILE = SCRIPT_DIR / "pause_exclusion_list.json"

# BigQuery configuration
SA_KEY_PATH = Path(os.path.expanduser("~/.config/gcloud/local-scripts-sa-key.json"))
BQ_PROJECT_ID = os.environ.get("BQ_PROJECT_ID", "")
BQ_DATASET_ID = os.environ.get("BQ_DATASET_ID", "")
BQ_BATCH_SIZE = 1000

# Load .env (Discord notifications are skipped if not configured)
load_dotenv(SCRIPT_DIR / ".env")
DISCORD_WEBHOOK_AD_ALERT = os.environ.get("DISCORD_WEBHOOK_AD_ALERT", "")
DISCORD_WEBHOOK_DAILY_REPORT = os.environ.get("DISCORD_WEBHOOK_DAILY_REPORT", "")

# Discord per-message limits (confirmed against the official docs on 2026-08-04)
# https://docs.discord.com/developers/resources/webhook - "array of up to 10 embed objects"
# https://docs.discord.com/developers/resources/message#embed-object-embed-limits - 6,000 chars total
# Exceeding either returns 400 Bad Request and **nothing is delivered**.
DISCORD_MAX_EMBEDS = 10
DISCORD_MAX_EMBED_CHARS = 6000

# How many examples of the same warning category to list in the email.
# The rest is reported as "+N more" - never dropped silently.
EMAIL_WARNING_SAMPLES = 3


# ===== GAQL Queries =====

CAMPAIGN_QUERY = """
    SELECT
        campaign.id,
        campaign.name,
        campaign.status,
        campaign_budget.amount_micros,
        metrics.impressions,
        metrics.clicks,
        metrics.ctr,
        metrics.average_cpc,
        metrics.cost_micros,
        metrics.conversions,
        metrics.all_conversions
    FROM campaign
    WHERE {campaign_filter}
      AND segments.date BETWEEN '{start_date}' AND '{end_date}'
"""

AD_GROUP_QUERY = """
    SELECT
        ad_group.id,
        ad_group.name,
        ad_group.resource_name,
        ad_group.status,
        campaign.name,
        metrics.impressions,
        metrics.clicks,
        metrics.ctr,
        metrics.average_cpc,
        metrics.cost_micros,
        metrics.conversions
    FROM ad_group
    WHERE {campaign_filter}
      AND segments.date BETWEEN '{start_date}' AND '{end_date}'
      AND ad_group.status != 'REMOVED'
"""

KEYWORD_QUERY = """
    SELECT
        ad_group_criterion.criterion_id,
        ad_group_criterion.resource_name,
        ad_group_criterion.keyword.text,
        ad_group_criterion.keyword.match_type,
        ad_group_criterion.status,
        ad_group_criterion.quality_info.quality_score,
        ad_group.id,
        ad_group.name,
        campaign.name,
        metrics.impressions,
        metrics.clicks,
        metrics.ctr,
        metrics.average_cpc,
        metrics.cost_micros,
        metrics.conversions
    FROM keyword_view
    WHERE {campaign_filter}
      AND ad_group_criterion.status != 'REMOVED'
      AND segments.date BETWEEN '{start_date}' AND '{end_date}'
"""

AD_QUERY = """
    SELECT
        ad_group_ad.ad.id,
        ad_group_ad.ad.responsive_search_ad.headlines,
        ad_group_ad.status,
        ad_group.name,
        campaign.name,
        metrics.impressions,
        metrics.clicks,
        metrics.ctr
    FROM ad_group_ad
    WHERE {campaign_filter}
      AND ad_group_ad.status != 'REMOVED'
      AND segments.date BETWEEN '{start_date}' AND '{end_date}'
"""

# For BQ storage: all campaign daily data (with segments.date)
# NOTE: When using .format(), always pass through _validate_gaql_value()
BQ_DAILY_CAMPAIGN_QUERY = """
    SELECT
        segments.date,
        campaign.id,
        campaign.name,
        campaign.status,
        campaign_budget.amount_micros,
        metrics.impressions,
        metrics.clicks,
        metrics.ctr,
        metrics.cost_micros,
        metrics.conversions
    FROM campaign
    WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
      AND campaign.status != 'REMOVED'
    ORDER BY segments.date DESC
"""

# NOTE: When using .format(), always pass through _validate_gaql_value()
BQ_DAILY_KEYWORD_QUERY = """
    SELECT
        segments.date,
        campaign.id,
        campaign.name,
        ad_group.id,
        ad_group.name,
        ad_group_criterion.criterion_id,
        ad_group_criterion.keyword.text,
        ad_group_criterion.keyword.match_type,
        ad_group_criterion.status,
        ad_group_criterion.quality_info.quality_score,
        metrics.impressions,
        metrics.clicks,
        metrics.ctr,
        metrics.average_cpc,
        metrics.cost_micros,
        metrics.conversions
    FROM keyword_view
    WHERE segments.date = '{date}'
      AND ad_group_criterion.status != 'REMOVED'
"""


# ===== Utilities =====

def _validate_gaql_value(value: str, field_name: str) -> str:
    """Validate a value to be embedded in a GAQL query (delegates to ads_common.gaql)."""
    return validate_gaql_value(value, field_name)


def micros_to_dollars(micros: int) -> float:
    return micros / 1_000_000


def load_pause_log() -> list[dict]:
    """Load pause_log.json. If file doesn't exist, attempt to restore from BQ."""
    if PAUSE_LOG_PATH.exists():
        try:
            with open(PAUSE_LOG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load pause_log.json: %s", e)
    # BQ fallback
    try:
        bq_client = bigquery.Client.from_service_account_json(str(SA_KEY_PATH))
        query = f"""
            SELECT id, target_name AS keyword_text, target_id AS keyword_id,
                   reason, previous_status, created_at
            FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.ad_actions_log`
            WHERE action_type = 'PAUSE'
            ORDER BY created_at DESC
            LIMIT 100
        """
        rows = list(bq_client.query(query).result())
        log = []
        for row in rows:
            d = dict(row)
            for k, v in d.items():
                if hasattr(v, 'isoformat'):
                    d[k] = v.isoformat()
            log.append(d)
        if log:
            logger.info("Restored %d PAUSE log entries from BQ", len(log))
        return log
    except Exception as e:
        logger.warning("BQ fallback also failed: %s", e)
        return []


def save_pause_log(log: list[dict]) -> None:
    """Save pause_log.json."""
    try:
        with open(PAUSE_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("Failed to save pause_log.json: %s", e)


# ===== Auto-pause exclusion list (JSON managed) =====

def load_pause_exclusion_list() -> dict[str, str]:
    """Load pause_exclusion_list.json and return {keyword: reason} dict."""
    if not PAUSE_EXCLUSION_FILE.exists():
        return {}
    try:
        with open(PAUSE_EXCLUSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {e["keyword"]: e["reason"] for e in data.get("entries", [])}
    except (json.JSONDecodeError, KeyError, OSError) as e:
        logger.warning("Failed to load pause_exclusion_list.json: %s", e)
        return {}


def save_pause_exclusion_list(entries: list[dict]) -> None:
    """Save pause_exclusion_list.json."""
    try:
        with open(PAUSE_EXCLUSION_FILE, "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("Failed to save pause_exclusion_list.json: %s", e)


def check_exclusion_graduation(
    keywords: list[dict],
    discord: bool = False,
) -> list[str]:
    """
    Check graduation of keywords registered in PAUSE_EXCLUSION_LIST.

    Graduation criteria: QS >= QS_MIN and imp >= EXCLUSION_GRADUATION_MIN_IMP
    If criteria met, automatically remove from JSON and notify Discord.

    Returns:
        graduated: list of graduated keyword texts
    """
    if not PAUSE_EXCLUSION_FILE.exists():
        return []

    try:
        with open(PAUSE_EXCLUSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("entries", [])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load exclusion list: %s", e)
        return []

    graduated: list[str] = []
    remaining: list[dict] = []

    for entry in entries:
        kw_text = entry["keyword"]
        kw_data = next(
            (k for k in keywords if k["keyword_text"] == kw_text and k["status"] == "ENABLED"),
            None,
        )

        if kw_data is None:
            remaining.append(entry)
            continue

        qs = kw_data.get("quality_score")
        imp = kw_data.get("impressions", 0)

        # Graduation: QS threshold cleared + not subject to current PAUSE criteria
        qs_ok = qs is not None and qs >= QS_MIN and imp >= EXCLUSION_GRADUATION_MIN_IMP
        would_be_paused = is_pause_target(kw_data)[0] if qs_ok else False

        if qs_ok and not would_be_paused:
            logger.info(
                "[Graduated] '%s' recovered to QS=%s / imp=%s, also clears PAUSE criteria. Removing from exclusion list.",
                kw_text, qs, imp,
            )
            graduated.append(kw_text)

            if discord and DISCORD_WEBHOOK_AD_ALERT:
                embed = build_ad_alert_embed(
                    title="Keyword Exclusion List Graduation",
                    description=f"'{kw_text}' QS has recovered above threshold ({QS_MIN}).",
                    severity="SUCCESS",
                    fields={
                        "QS": str(qs),
                        "Impressions (7d)": str(imp),
                        "Registration reason": entry.get("reason", "-"),
                        "Registration date": entry.get("added_at", "-"),
                    },
                )
                send_discord_notification(DISCORD_WEBHOOK_AD_ALERT, content="", embeds=[embed])
        else:
            remaining.append(entry)

    if graduated:
        save_pause_exclusion_list(remaining)
        # Update global dict (reflected in identify_pause_targets for this run)
        # WARNING: Thread-unsafe - this script assumes single-process (launchd single launch).
        # If parallelized in the future, remove PAUSE_EXCLUSION_LIST from global
        # and pass it as an argument from main().
        PAUSE_EXCLUSION_LIST.clear()
        PAUSE_EXCLUSION_LIST.update({e["keyword"]: e["reason"] for e in remaining})

    return graduated


# ===== Global exclusion list (loaded from JSON at startup) =====
PAUSE_EXCLUSION_LIST: dict[str, str] = load_pause_exclusion_list()


def format_ctr(ctr: float) -> str:
    """Format CTR as % display with icon based on threshold"""
    pct = ctr * 100
    if ctr >= CTR_WARNING_THRESHOLD:
        icon = "OK"
    elif ctr >= CTR_CRITICAL_THRESHOLD:
        icon = "WARNING"
    else:
        icon = "CRITICAL"
    return f"{pct:.2f}% [{icon}]"


def format_ctr_raw(ctr: float) -> str:
    return f"{ctr * 100:.2f}%"


def get_campaign_pause_type(campaign_name: str) -> str | None:
    """Return PAUSE strategy type from campaign name.

    Prefix match against CAMPAIGN_PAUSE_TYPE keys.
    Returns None if no match (not subject to auto-PAUSE).
    """
    for prefix, pause_type in CAMPAIGN_PAUSE_TYPE.items():
        if campaign_name.startswith(prefix):
            return pause_type
    return None


def is_pause_target(kw: dict) -> tuple[bool, str | None]:
    """Determine if keyword is a PAUSE target based on per-campaign criteria.

    Returns:
        (is_target, reason): True + reason string if PAUSE target
    """
    imp = kw["impressions"]
    clicks = kw.get("clicks", 0)
    ctr = kw["ctr"]
    qs = kw.get("quality_score")
    cv = kw.get("conversions", 0)
    campaign = kw.get("campaign_name", "")
    pause_type = get_campaign_pause_type(campaign)

    # Common: QS=1 stops immediately (cannot participate in auction)
    if qs is not None and qs <= 1:
        return True, f"QS {qs} (<=1) / {imp}imp — immediate stop (unrecoverable)"

    if pause_type == "cv":
        # CV-focused — stop KWs consuming clicks without CV
        if clicks >= CV_PAUSE_MIN_CLICKS and cv < 0.5:
            return True, f"{clicks}click / CV {cv:.1f} — clicks consumed without CV"

    elif pause_type == "ctr":
        # Access-focused — stop low CTR KWs
        if imp >= 100 and ctr < CTR_KW_PAUSE_100IMP:
            return True, f"{imp}imp with CTR {format_ctr_raw(ctr)} (< 3%)"
        if imp >= 50 and ctr < CTR_KW_PAUSE_50IMP:
            return True, f"{imp}imp with CTR {format_ctr_raw(ctr)} (< 2%)"

    # pause_type is None -> no auto-pause except QS<=1
    return False, None


JST = timezone(timedelta(hours=9))


def build_date_range(days: int) -> tuple[str, str]:
    end = datetime.now(tz=JST)
    start = end - timedelta(days=days - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def build_campaign_filter(campaign_names: list[str] | None = None) -> str:
    """Generate GAQL campaign filter condition.

    Single: campaign.name = 'X'
    Multiple: campaign.name IN ('X', 'Y')
    """
    names = campaign_names or CAMPAIGN_NAMES
    validated = [_validate_gaql_value(n, "campaign_name") for n in names]
    if len(validated) == 1:
        return f"campaign.name = '{validated[0]}'"
    quoted = ", ".join(f"'{n}'" for n in validated)
    return f"campaign.name IN ({quoted})"


def ensure_reports_dir():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ===== Data Fetching =====

def fetch_campaign_metrics(client, start_date: str, end_date: str) -> dict | None:
    """Fetch metrics for all monitored campaigns.

    Returns:
        Account-wide aggregated dict with campaigns key containing individual campaign list.
    """
    ga_service = client.get_service("GoogleAdsService")
    query = CAMPAIGN_QUERY.format(
        campaign_filter=build_campaign_filter(),
        start_date=_validate_gaql_value(start_date, "date"),
        end_date=_validate_gaql_value(end_date, "date"),
    )
    response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
    rows = list(response)

    if not rows:
        return None

    # Aggregate by campaign
    by_campaign: dict[int, dict] = {}
    for row in rows:
        m = row.metrics
        cid = row.campaign.id
        if cid not in by_campaign:
            by_campaign[cid] = {
                "campaign_id": cid,
                "campaign_name": row.campaign.name,
                "campaign_status": row.campaign.status.name,
                "daily_budget_dollars": micros_to_dollars(row.campaign_budget.amount_micros),
                "impressions": 0,
                "clicks": 0,
                "cost_micros": 0,
                "conversions": 0.0,
            }
        c = by_campaign[cid]
        c["impressions"] += m.impressions
        c["clicks"] += m.clicks
        c["cost_micros"] += m.cost_micros
        c["conversions"] += m.conversions

    campaigns_list = []
    for c in by_campaign.values():
        imp, clk = c["impressions"], c["clicks"]
        c["ctr"] = clk / imp if imp > 0 else 0.0
        c["avg_cpc_dollars"] = micros_to_dollars(c["cost_micros"]) / clk if clk > 0 else 0.0
        c["cost_dollars"] = micros_to_dollars(c.pop("cost_micros"))
        campaigns_list.append(c)

    # Account-wide aggregation
    total_impressions = sum(c["impressions"] for c in campaigns_list)
    total_clicks = sum(c["clicks"] for c in campaigns_list)
    total_cost = sum(c["cost_dollars"] for c in campaigns_list)
    total_conversions = sum(c["conversions"] for c in campaigns_list)
    ctr = total_clicks / total_impressions if total_impressions > 0 else 0.0
    avg_cpc = total_cost / total_clicks if total_clicks > 0 else 0.0

    return {
        "campaign_id": campaigns_list[0]["campaign_id"],
        "campaign_name": "All Campaigns",
        "campaign_status": campaigns_list[0]["campaign_status"],
        "daily_budget_dollars": sum(c["daily_budget_dollars"] for c in campaigns_list),
        "impressions": total_impressions,
        "clicks": total_clicks,
        "ctr": ctr,
        "avg_cpc_dollars": avg_cpc,
        "cost_dollars": total_cost,
        "conversions": total_conversions,
        "campaigns": campaigns_list,
    }


def fetch_ad_group_metrics(client, start_date: str, end_date: str) -> list[dict]:
    """Fetch per-ad-group metrics"""
    ga_service = client.get_service("GoogleAdsService")
    query = AD_GROUP_QUERY.format(
        campaign_filter=build_campaign_filter(),
        start_date=_validate_gaql_value(start_date, "date"),
        end_date=_validate_gaql_value(end_date, "date"),
    )
    response = ga_service.search(customer_id=CUSTOMER_ID, query=query)

    # Aggregate by ad group ID (in case of daily segments)
    groups: dict[int, dict] = {}
    for row in response:
        ag = row.ad_group
        m = row.metrics
        gid = ag.id

        if gid not in groups:
            groups[gid] = {
                "ad_group_id": gid,
                "ad_group_name": ag.name,
                "resource_name": ag.resource_name,
                "campaign_name": row.campaign.name,
                "status": ag.status.name,
                "impressions": 0,
                "clicks": 0,
                "cost_micros": 0,
                "conversions": 0.0,
            }

        groups[gid]["impressions"] += m.impressions
        groups[gid]["clicks"] += m.clicks
        groups[gid]["cost_micros"] += m.cost_micros
        groups[gid]["conversions"] += m.conversions

    result = []
    for g in groups.values():
        imp = g["impressions"]
        clk = g["clicks"]
        g["ctr"] = clk / imp if imp > 0 else 0.0
        g["avg_cpc_dollars"] = (
            micros_to_dollars(g["cost_micros"]) / clk if clk > 0 else 0.0
        )
        g["cost_dollars"] = micros_to_dollars(g["cost_micros"])
        del g["cost_micros"]
        result.append(g)

    result.sort(key=lambda x: x["ctr"], reverse=True)
    return result


def fetch_keyword_metrics(client, start_date: str, end_date: str) -> list[dict]:
    """Fetch per-keyword metrics"""
    ga_service = client.get_service("GoogleAdsService")
    query = KEYWORD_QUERY.format(
        campaign_filter=build_campaign_filter(),
        start_date=_validate_gaql_value(start_date, "date"),
        end_date=_validate_gaql_value(end_date, "date"),
    )
    response = ga_service.search(customer_id=CUSTOMER_ID, query=query)

    keywords: dict[str, dict] = {}
    for row in response:
        crit = row.ad_group_criterion
        m = row.metrics
        key = crit.resource_name

        qs_value = crit.quality_info.quality_score
        # quality_score of 0 means not yet measured (treat as None)
        quality_score = qs_value if qs_value > 0 else None

        if key not in keywords:
            keywords[key] = {
                "resource_name": crit.resource_name,
                "criterion_id": crit.criterion_id,
                "keyword_text": crit.keyword.text,
                "match_type": crit.keyword.match_type.name,
                "status": crit.status.name,
                "quality_score": quality_score,
                "ad_group_id": row.ad_group.id,
                "ad_group_name": row.ad_group.name,
                "campaign_name": row.campaign.name,
                "impressions": 0,
                "clicks": 0,
                "cost_micros": 0,
                "conversions": 0.0,
            }

        keywords[key]["impressions"] += m.impressions
        keywords[key]["clicks"] += m.clicks
        keywords[key]["cost_micros"] += m.cost_micros
        keywords[key]["conversions"] += m.conversions

    result = []
    for kw in keywords.values():
        imp = kw["impressions"]
        clk = kw["clicks"]
        kw["ctr"] = clk / imp if imp > 0 else 0.0
        kw["avg_cpc_dollars"] = (
            micros_to_dollars(kw["cost_micros"]) / clk if clk > 0 else 0.0
        )
        kw["cost_dollars"] = micros_to_dollars(kw["cost_micros"])
        del kw["cost_micros"]
        result.append(kw)

    result.sort(key=lambda x: x["impressions"], reverse=True)
    return result


def fetch_ad_metrics(client, start_date: str, end_date: str) -> list[dict]:
    """Fetch per-ad metrics"""
    ga_service = client.get_service("GoogleAdsService")
    query = AD_QUERY.format(
        campaign_filter=build_campaign_filter(),
        start_date=_validate_gaql_value(start_date, "date"),
        end_date=_validate_gaql_value(end_date, "date"),
    )
    response = ga_service.search(customer_id=CUSTOMER_ID, query=query)

    ads: dict[int, dict] = {}
    for row in response:
        ad_id = row.ad_group_ad.ad.id
        m = row.metrics

        if ad_id not in ads:
            # Get up to 3 headlines
            headlines = row.ad_group_ad.ad.responsive_search_ad.headlines
            headline_texts = [h.text for h in headlines[:3]] if headlines else []

            ads[ad_id] = {
                "ad_id": ad_id,
                "ad_group_name": row.ad_group.name,
                "status": row.ad_group_ad.status.name,
                "headlines": headline_texts,
                "impressions": 0,
                "clicks": 0,
            }

        ads[ad_id]["impressions"] += m.impressions
        ads[ad_id]["clicks"] += m.clicks

    result = []
    for ad in ads.values():
        imp = ad["impressions"]
        clk = ad["clicks"]
        ad["ctr"] = clk / imp if imp > 0 else 0.0
        result.append(ad)

    result.sort(key=lambda x: x["impressions"], reverse=True)
    return result


# ===== Alert Generation =====

def generate_alerts(
    campaign: dict | None,
    ad_groups: list[dict],
    keywords: list[dict],
) -> list[dict]:
    """Generate alert list"""
    alerts = []

    if campaign is None:
        alerts.append({
            "level": "INFO",
            "category": "campaign",
            "message": "No campaign data yet (possibly a new campaign)",
        })
        return alerts

    # Account-wide CTR check
    account_ctr = campaign["ctr"]
    if account_ctr < CTR_CRITICAL_THRESHOLD:
        alerts.append({
            "level": "CRITICAL",
            "category": "account_ctr",
            "message": (
                f"Account-wide CTR {format_ctr_raw(account_ctr)} is below 5%."
                " Ad Grants account will be suspended if this continues for 2 consecutive months."
            ),
        })
    elif account_ctr < CTR_WARNING_THRESHOLD:
        alerts.append({
            "level": "WARNING",
            "category": "account_ctr",
            "message": (
                f"Account-wide CTR {format_ctr_raw(account_ctr)} is approaching danger zone (< 7%)."
                " Consider pausing low-CTR keywords or improving ad copy."
            ),
        })
    elif account_ctr < CTR_EARLY_WARNING_THRESHOLD:
        alerts.append({
            "level": "WARNING",
            "category": "account_ctr_early_warning",
            "message": (
                f"Account-wide CTR {format_ctr_raw(account_ctr)} is in early warning zone (< 6%)."
                " Consider pausing low-CTR keywords or improving ad copy."
            ),
        })

    # Ad group CTR check
    # 2026-07-28: a 5% breach at meaningful volume is escalated to CRITICAL.
    # As WARNING it stayed out of e-mail escalation and reached nobody for 11 days.
    for ag in ad_groups:
        if ag["impressions"] < 10:
            continue  # Skip if insufficient impressions
        if ag["status"] != "ENABLED":
            continue  # Already paused; ignore the residue in the window
        if ag["ctr"] < CTR_AD_GROUP_MIN:
            material = ag["impressions"] >= AG_ALERT_CRITICAL_MIN_IMP
            alerts.append({
                "level": "CRITICAL" if material else "WARNING",
                "category": "ad_group_ctr",
                "message": (
                    f"Ad group '{ag['ad_group_name']}' CTR is"
                    f" {format_ctr_raw(ag['ctr'])} (< 5%, {ag['impressions']:,} impressions)"
                ),
                "ad_group_name": ag["ad_group_name"],
            })

    # Keyword QS check (exclude already PAUSEd)
    for kw in keywords:
        if kw.get("status") == "PAUSED":
            continue
        if kw["quality_score"] is not None and kw["quality_score"] < QS_MIN:
            alerts.append({
                "level": "WARNING",
                "category": "quality_score",
                "message": (
                    f"Keyword '{kw['keyword_text']}' quality score is"
                    f" {kw['quality_score']} (< 3). Ad Grants requires QS >= 3."
                    + ("" if kw['quality_score'] <= 1 else " (Auto-pause is only for QS<=1. Consider improving.)")
                ),
                "keyword_text": kw["keyword_text"],
                "quality_score": kw["quality_score"],
            })

    return alerts


# ===== Auto-Pause =====

def identify_pause_targets(
    keywords: list[dict],
    headroom: dict | None = None,
    ad_groups: list[dict] | None = None,
) -> list[dict]:
    """Identify keywords subject to auto-pause.

    Per-campaign pause criteria delegated to is_pause_target().
    Strategy types centrally managed in config.CAMPAIGN_PAUSE_TYPE.

    Added 2026-07-28: with no click budget left, raise the pause line to the
    requirement itself (5%). The legacy thresholds (3% at 100 imp / 2% at 50 imp)
    let the 3-5% band through entirely, so keywords there kept serving even inside
    ad groups that were above 5% overall -- 33% of live impressions in one account.
    While there is budget the legacy thresholds apply and the long tail keeps running.
    """
    strict = bool(headroom) and headroom.get("measurable") and headroom["budget_clicks"] <= 0

    # Keywords inside a paused ad group never serve, so pausing them moves CTR by
    # exactly zero -- yet they still consume the daily pause quota. In a real run on
    # 2026-07-28, 24 of 35 targets were of this kind and crowded out the 11 that mattered.
    paused_ag = {
        ag["ad_group_name"] for ag in (ad_groups or [])
        if ag.get("status") != "ENABLED"
    }

    targets = []
    for kw in keywords:
        if kw["status"] == "PAUSED":
            continue
        if kw.get("ad_group_name") in paused_ag:
            continue

        kw_text = kw.get("keyword_text", "")
        if kw_text in PAUSE_EXCLUSION_LIST:
            exclusion_reason = PAUSE_EXCLUSION_LIST[kw_text]
            logger.info(f"[Excluded] '{kw_text}' is registered in auto-pause exclusion list ({exclusion_reason})")
            continue

        is_target, reason = is_pause_target(kw)
        if not is_target and strict:
            imp = kw.get("impressions", 0)
            ctr = kw.get("ctr", 0.0)
            if imp >= KW_STRICT_PAUSE_MIN_IMP and ctr < KW_STRICT_PAUSE_CTR:
                is_target = True
                reason = (
                    f"strict mode (no budget): {imp} impressions at CTR {ctr*100:.2f}% "
                    f"(< {KW_STRICT_PAUSE_CTR:.0%})"
                )
        if is_target and reason:
            targets.append({**kw, "pause_reason": reason})

    return targets


def pause_keywords(
    client,
    pause_targets: list[dict],
    all_keywords: list[dict],
    dry_run: bool,
    discord: bool = False,
    strict: bool = False,
) -> list[dict]:
    """Pause target keywords. If dry_run=True, no actual pause is performed.

    Safety guards:
    - Skip if daily cumulative PAUSE count reaches MAX_PAUSE_PER_DAY
    - Skip if PAUSE targets exceed MAX_PAUSE_PER_RUN (prompt manual review)
    - Skip if enabled keywords would drop below MIN_ENABLED_KEYWORDS after PAUSE

    strict=True (no click budget = on pace to miss the monthly 5%) raises the
    quotas, because 5 per day cannot clear the backlog before month end.
    """
    paused_log = []

    if not pause_targets:
        return paused_log

    if dry_run:
        for kw in pause_targets:
            paused_log.append({
                "keyword_text": kw["keyword_text"],
                "ad_group_name": kw["ad_group_name"],
                "reason": kw["pause_reason"],
                "dry_run": True,
            })
        return paused_log

    # ===== Safety guard: count check =====
    today_str = datetime.now(tz=JST).strftime("%Y-%m-%d")
    existing_log = load_pause_log()
    today_paused_count = sum(1 for e in existing_log if e.get("date") == today_str)

    # Get today's PAUSE count from BQ too, use larger value (safer)
    if SA_KEY_PATH.exists():
        try:
            bq_client = bigquery.Client.from_service_account_json(str(SA_KEY_PATH))
            bq_count_query = f"""
                SELECT COUNT(*) as cnt
                FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.ad_actions_log`
                WHERE date = @today AND action_type = 'PAUSE'
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("today", "DATE", today_str),
                ]
            )
            bq_today_count = list(bq_client.query(bq_count_query, job_config=job_config).result())[0].cnt
            today_paused_count = max(today_paused_count, bq_today_count)
        except Exception as e:
            logger.debug("BQ PAUSE count retrieval failed (falling back to pause_log.json): %s", e)

    # ===== Selection: high-confidence exempt from caps; standard quota takes top-N =====
    # The old design skipped ALL pauses when targets exceeded the per-run cap, so a
    # backlog stalled the pipeline indefinitely. Now: process what fits, defer the rest.
    high_confidence = [
        t for t in pause_targets
        if t["ctr"] < HIGH_CONFIDENCE_CTR_THRESHOLD
        and t["impressions"] >= HIGH_CONFIDENCE_MIN_IMP
    ]
    high_conf_names = {t["resource_name"] for t in high_confidence}
    standard = [t for t in pause_targets if t["resource_name"] not in high_conf_names]

    # Remaining standard quota for today (per-day cap applies to standard only)
    per_day = MAX_PAUSE_PER_DAY_STRICT if strict else MAX_PAUSE_PER_DAY
    per_run = MAX_PAUSE_PER_RUN_STRICT if strict else MAX_PAUSE_PER_RUN
    remaining_today = max(0, per_day - today_paused_count)
    standard_quota = min(per_run, remaining_today)
    standard_sorted = sorted(standard, key=lambda t: t["impressions"], reverse=True)
    selected = high_confidence + standard_sorted[:standard_quota]
    deferred = standard_sorted[standard_quota:]

    if deferred:
        msg = (
            f"Of {len(pause_targets)} PAUSE targets, processing {len(high_confidence)} "
            f"high-confidence (cap-exempt) + {min(standard_quota, len(standard_sorted))} "
            f"standard (by imp desc) today; {len(deferred)} carried over to next runs."
        )
        logger.warning(msg)
        if discord:
            embed = build_ad_alert_embed(
                title="[WARNING] PAUSE targets exceed cap — processing top-N, deferring rest",
                description=msg,
                severity="WARNING",
                fields={
                    "Processed today": str(len(selected)),
                    "High-confidence (cap-exempt)": str(len(high_confidence)),
                    "Deferred": str(len(deferred)),
                    "Rollback": "`python monitor_ad_performance.py --undo`",
                },
            )
            send_discord_notification(DISCORD_WEBHOOK_AD_ALERT, content="", embeds=[embed])

    if not selected:
        logger.warning(
            "No standard quota left today and no high-confidence targets; skipping PAUSE "
            f"(today: {today_paused_count}/{MAX_PAUSE_PER_DAY}, "
            f"{len(pause_targets)} targets carried over)."
        )
        return paused_log

    # Check enabled keyword count after PAUSE (per campaign).
    # Exclude only the violating campaigns' targets and continue (no full abort).
    enabled_by_campaign = Counter(
        kw["campaign_name"] for kw in all_keywords if kw["status"] == "ENABLED"
    )
    targets_by_campaign = Counter(
        t["campaign_name"] for t in selected
    )
    blocked_campaigns = []
    blocked_set: set[str] = set()
    for campaign, target_count in targets_by_campaign.items():
        remaining = enabled_by_campaign.get(campaign, 0) - target_count
        if remaining < MIN_ENABLED_KEYWORDS:
            blocked_campaigns.append(
                f"{campaign}: enabled={enabled_by_campaign.get(campaign, 0)} -> remaining={remaining}"
            )
            blocked_set.add(campaign)
    if blocked_campaigns:
        msg = (
            f"Excluding campaigns that would drop below {MIN_ENABLED_KEYWORDS} enabled KWs: "
            + "; ".join(blocked_campaigns)
        )
        logger.warning(msg)
        if discord:
            embed = build_ad_alert_embed(
                title="[WARNING] Insufficient enabled keywords — excluding affected campaigns",
                description=msg,
                severity="WARNING",
                fields={
                    "Excluded campaigns": "\n".join(blocked_campaigns),
                    "Minimum required/campaign": str(MIN_ENABLED_KEYWORDS),
                },
            )
            send_discord_notification(DISCORD_WEBHOOK_AD_ALERT, content="", embeds=[embed])
        selected = [t for t in selected if t["campaign_name"] not in blocked_set]
        if not selected:
            return paused_log

    # ===== Execute PAUSE =====
    criterion_service = client.get_service("AdGroupCriterionService")
    operations = []

    for kw in selected:
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.update
        criterion.resource_name = kw["resource_name"]
        criterion.status = client.enums.AdGroupCriterionStatusEnum.PAUSED
        operation.update_mask.CopyFrom(
            field_mask_pb2.FieldMask(paths=["status"])
        )
        operations.append(operation)

    try:
        criterion_service.mutate_ad_group_criteria(
            customer_id=CUSTOMER_ID,
            operations=operations,
        )
        now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_entries = []
        for kw in selected:
            entry = {
                "date": today_str,
                "keyword_text": kw["keyword_text"],
                "resource_name": kw["resource_name"],
                "previous_status": "ENABLED",
                "paused_at": now_iso,
                "reason": kw["pause_reason"],
            }
            new_entries.append(entry)
            paused_log.append({
                "keyword_text": kw["keyword_text"],
                "ad_group_name": kw["ad_group_name"],
                "reason": kw["pause_reason"],
                "dry_run": False,
            })

            # Discord: individual PAUSE notification
            if discord:
                embed = build_ad_alert_embed(
                    title=f"Keyword PAUSED",
                    description=f"'{kw['keyword_text']}' has been paused.",
                    severity="WARNING",
                    fields={
                        "Reason": kw["pause_reason"],
                        "Ad group": kw["ad_group_name"],
                        "Rollback": "`python monitor_ad_performance.py --undo`",
                    },
                )
                send_discord_notification(DISCORD_WEBHOOK_AD_ALERT, content="", embeds=[embed])

        # Append to pause_log.json
        updated_log = existing_log + new_entries
        save_pause_log(updated_log)

    except GoogleAdsException as ex:
        logger.error("Keyword pause API error: %s", ex)
        for error in ex.failure.errors:
            logger.error("  - %s", error.message)
        if discord and DISCORD_WEBHOOK_AD_ALERT:
            kw_names = ", ".join(t["keyword_text"] for t in selected[:5])
            embed = build_ad_alert_embed(
                title="[CRITICAL] PAUSE API failed — keywords NOT paused",
                description=f"mutate_ad_group_criteria failed. Target KWs: {kw_names}",
                severity="CRITICAL",
                fields={
                    "Error": str(ex.failure.errors[0].message) if ex.failure.errors else str(ex),
                    "Target count": str(len(pause_targets)),
                    "Manual action": "Please manually pause in Google Ads console",
                },
            )
            send_discord_notification(DISCORD_WEBHOOK_AD_ALERT, content="", embeds=[embed])

    return paused_log


# ===== Rollback =====

def undo_pauses(client, undo_date: str | None, dry_run: bool) -> None:
    """Undo PAUSEs recorded in pause_log.json.

    Args:
        undo_date: Undo PAUSEs on specified date (YYYY-MM-DD). If None, undo all.
        dry_run: If True, preview only without actually reverting.
    """
    log = load_pause_log()

    if not log:
        print("[INFO] No entries in pause_log.json.")
        return

    if undo_date:
        targets = [e for e in log if e.get("date") == undo_date]
        if not targets:
            print(f"[INFO] No PAUSE entries found for {undo_date}.")
            return
        remaining = [e for e in log if e.get("date") != undo_date]
    else:
        targets = log
        remaining = []

    print(f"[INFO] Undoing {len(targets)} PAUSEs{'(dry run)' if dry_run else ''}:")
    for entry in targets:
        print(
            f"  - '{entry['keyword_text']}'"
            f"  paused_at={entry.get('paused_at', '-')}"
            f"  date={entry.get('date', '-')}"
        )

    if dry_run:
        print("[DRY-RUN] No actual undo performed.")
        return

    criterion_service = client.get_service("AdGroupCriterionService")
    operations = []

    for entry in targets:
        resource_name = entry.get("resource_name")
        if not resource_name:
            logger.warning(
                "resource_name unknown for '%s'. Skipping.",
                entry.get('keyword_text', '?'),
            )
            continue
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.update
        criterion.resource_name = resource_name
        criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        operation.update_mask.CopyFrom(
            field_mask_pb2.FieldMask(paths=["status"])
        )
        operations.append(operation)

    if not operations:
        logger.warning("No undoable entries found.")
        return

    try:
        criterion_service.mutate_ad_group_criteria(
            customer_id=CUSTOMER_ID,
            operations=operations,
        )
        print(f"[INFO] Restored {len(operations)} keywords to ENABLED.")
        save_pause_log(remaining)
        print(f"[INFO] pause_log.json updated (remaining: {len(remaining)}).")
    except GoogleAdsException as ex:
        logger.error("Rollback API error: %s", ex)
        for error in ex.failure.errors:
            logger.error("  - %s", error.message)


# ===== Keyword Recommendations =====

def generate_keyword_suggestions(keywords: list[dict]) -> dict:
    """Generate keyword recommendations based on performance"""
    # Top performers: 10+ clicks, sorted by CTR descending
    top_performers = sorted(
        [kw for kw in keywords if kw["clicks"] >= 10],
        key=lambda x: x["ctr"],
        reverse=True,
    )[:5]

    # Bottom performers: 50+ imp, sorted by CTR ascending (currently enabled)
    bottom_performers = sorted(
        [kw for kw in keywords if kw["impressions"] >= 50 and kw["status"] == "ENABLED"],
        key=lambda x: x["ctr"],
    )[:5]

    # Pause candidates: match per-campaign is_pause_target criteria and still enabled
    pause_candidates = [
        kw for kw in keywords
        if kw["status"] == "ENABLED"
        and kw.get("keyword_text", "") not in PAUSE_EXCLUSION_LIST
        and is_pause_target(kw)[0]
    ]

    # Derive theme suggestions from top keywords (using ad_group_name)
    top_themes = list({kw["ad_group_name"] for kw in top_performers})
    new_theme_suggestions = []
    for theme in top_themes:
        new_theme_suggestions.append(
            f"Consider adding long-tail / related terms for '{theme}'"
        )

    return {
        "top_performers": top_performers,
        "bottom_performers": bottom_performers,
        "pause_candidates": pause_candidates,
        "new_keyword_themes": new_theme_suggestions,
    }


# ===== BigQuery Storage =====

def save_to_bigquery(
    client,
    start_date: str,
    end_date: str,
    alerts: list[dict],
    paused_log: list[dict],
    failure_reasons: list[str] | None = None,
) -> bool:
    """Save performance data to BigQuery.

    Saves daily-granularity data for all campaigns.
    Re-fetches from Google Ads API with segments.date for per-day per-campaign storage.
    Ensures idempotency via DELETE before INSERT.

    BQ storage strategy:
    - ad_campaign_daily: daily x per-campaign data (partitioned by segments.date)
    - ad_keyword_performance: keyword snapshot at end_date (all campaigns)
    - ad_actions_log: action log for PAUSE etc. (generated from paused_log)

    Returns:
        bool: True if all tables saved successfully, False if any failed.
    """
    if not SA_KEY_PATH.exists():
        logger.warning("SA key file not found: %s. Skipping BQ save.", SA_KEY_PATH)
        if failure_reasons is not None:
            failure_reasons.append("the service account key file is missing")
        return False

    try:
        bq_client = bigquery.Client.from_service_account_json(str(SA_KEY_PATH))
    except Exception as e:
        logger.warning("Failed to initialize BigQuery client: %s", e)
        if failure_reasons is not None:
            failure_reasons.append(f"could not connect: {classify_bq_failure(e)}")
        return False

    account_id = CUSTOMER_ID
    now_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bq_save_ok = True
    ga_service = client.get_service("GoogleAdsService")

    # ---------- ad_campaign_daily (all campaigns x daily) ----------
    try:
        table_camp = f"`{BQ_PROJECT_ID}.{BQ_DATASET_ID}.ad_campaign_daily`"

        # Idempotent DELETE (by period x account)
        delete_camp_sql = f"""
            DELETE FROM {table_camp}
            WHERE date BETWEEN @start_date AND @end_date
              AND account_id = @account_id
        """
        job_config_camp = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
                bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
                bigquery.ScalarQueryParameter("account_id", "STRING", account_id),
            ]
        )
        camp_delete_ok = True
        try:
            bq_client.query(delete_camp_sql, job_config=job_config_camp).result()
        except BadRequest as e:
            if "streaming buffer" in str(e):
                logger.info("ad_campaign_daily: Skipping DELETE+INSERT due to streaming buffer")
                camp_delete_ok = False
            else:
                raise

        if camp_delete_ok:
            # Fetch daily x all campaigns data from Google Ads API
            # NOTE: Always pass through _validate_gaql_value() when using .format()
            daily_query = BQ_DAILY_CAMPAIGN_QUERY.format(
                start_date=_validate_gaql_value(start_date, "date"),
                end_date=_validate_gaql_value(end_date, "date"),
            )
            response = ga_service.search(customer_id=CUSTOMER_ID, query=daily_query)

            alert_count = len([a for a in alerts if a.get("level") in ("CRITICAL", "WARNING")])

            rows_camp = []
            for row in response:
                rows_camp.append({
                    "date": row.segments.date,
                    "account_id": account_id,
                    "campaign_id": str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "impressions": row.metrics.impressions,
                    "clicks": row.metrics.clicks,
                    "ctr": row.metrics.ctr,
                    "cost": row.metrics.cost_micros / 1_000_000,
                    "conversions": row.metrics.conversions,
                    "daily_budget": row.campaign_budget.amount_micros / 1_000_000,
                    "campaign_status": row.campaign.status.name,
                    "active_keywords": None,
                    "paused_keywords": None,
                    "alert_count": alert_count,
                    "synced_at": now_ts,
                })

            if rows_camp:
                bq_table_camp = bq_client.get_table(
                    f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.ad_campaign_daily"
                )
                for i in range(0, len(rows_camp), BQ_BATCH_SIZE):
                    batch = rows_camp[i:i + BQ_BATCH_SIZE]
                    errors = bq_client.insert_rows_json(bq_table_camp, batch)
                    if errors:
                        logger.warning(
                            "ad_campaign_daily INSERT error (batch %d): %s",
                            i // BQ_BATCH_SIZE + 1,
                            errors,
                        )
                        bq_save_ok = False
                print(f"[BQ] ad_campaign_daily: saved {len(rows_camp)} rows")
            else:
                print("[BQ] ad_campaign_daily: no data (skipped)")

    except GoogleAdsException as ex:
        logger.warning("ad_campaign_daily Google Ads API error: %s", ex)
        if failure_reasons is not None:
            failure_reasons.append(f"ad_campaign_daily: Google Ads API error ({str(ex)[:80]})")
        bq_save_ok = False
    except Exception as e:
        logger.warning("Failed to save ad_campaign_daily: %s", e)
        if failure_reasons is not None:
            failure_reasons.append(f"ad_campaign_daily: {classify_bq_failure(e)}")
        bq_save_ok = False

    # ---------- ad_keyword_performance (all campaigns, previous day snapshot) ----------
    # Google Ads API has incomplete data for current day, use previous day (end_date - 1)
    kw_snapshot_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    # If --days 1, ensure snapshot_date doesn't go before start_date
    if kw_snapshot_date < start_date:
        kw_snapshot_date = start_date
    try:
        table_kw = f"`{BQ_PROJECT_ID}.{BQ_DATASET_ID}.ad_keyword_performance`"

        # Idempotent DELETE (snapshot_date only)
        delete_kw_sql = f"""
            DELETE FROM {table_kw}
            WHERE date = @date
              AND account_id = @account_id
        """
        job_config_kw = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("date", "DATE", kw_snapshot_date),
                bigquery.ScalarQueryParameter("account_id", "STRING", account_id),
            ]
        )
        kw_delete_ok = True
        try:
            bq_client.query(delete_kw_sql, job_config=job_config_kw).result()
        except BadRequest as e:
            if "streaming buffer" in str(e):
                logger.info("ad_keyword_performance: Skipping DELETE+INSERT due to streaming buffer")
                kw_delete_ok = False
            else:
                raise

        if not kw_delete_ok:
            print("[BQ] ad_keyword_performance: streaming buffer active (next run will refresh)")
        else:
            # NOTE: Always pass through _validate_gaql_value() when using .format()
            kw_query = BQ_DAILY_KEYWORD_QUERY.format(
                date=_validate_gaql_value(kw_snapshot_date, "date"),
            )
            response = ga_service.search(customer_id=CUSTOMER_ID, query=kw_query)

            rows_kw = []
            for row in response:
                qs = row.ad_group_criterion.quality_info.quality_score
                rows_kw.append({
                    "date": kw_snapshot_date,
                    "account_id": account_id,
                    "campaign_id": str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "ad_group_id": str(row.ad_group.id),
                    "ad_group_name": row.ad_group.name,
                    "keyword_id": str(row.ad_group_criterion.criterion_id),
                    "keyword_text": row.ad_group_criterion.keyword.text,
                    "match_type": row.ad_group_criterion.keyword.match_type.name,
                    "impressions": row.metrics.impressions,
                    "clicks": row.metrics.clicks,
                    "ctr": row.metrics.ctr,
                    "cost_micros": row.metrics.cost_micros,
                    "cost": row.metrics.cost_micros / 1_000_000,
                    "conversions": row.metrics.conversions,
                    "quality_score": qs if qs and qs > 0 else None,
                    "avg_cpc_micros": int(row.metrics.average_cpc),
                    "keyword_status": row.ad_group_criterion.status.name,
                    "synced_at": now_ts,
                })

            if rows_kw:
                bq_table_kw = bq_client.get_table(
                    f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.ad_keyword_performance"
                )
                for i in range(0, len(rows_kw), BQ_BATCH_SIZE):
                    batch = rows_kw[i:i + BQ_BATCH_SIZE]
                    errors = bq_client.insert_rows_json(bq_table_kw, batch)
                    if errors:
                        logger.warning(
                            "ad_keyword_performance INSERT error (batch %d): %s",
                            i // BQ_BATCH_SIZE + 1,
                            errors,
                        )
                        bq_save_ok = False
                print(f"[BQ] ad_keyword_performance: saved {len(rows_kw)} rows")
            else:
                print("[BQ] ad_keyword_performance: no data (skipped)")

    except GoogleAdsException as ex:
        logger.warning("ad_keyword_performance Google Ads API error: %s", ex)
        if failure_reasons is not None:
            failure_reasons.append(f"ad_keyword_performance: Google Ads API error ({str(ex)[:80]})")
        bq_save_ok = False
    except Exception as e:
        logger.warning("Failed to save ad_keyword_performance: %s", e)
        if failure_reasons is not None:
            failure_reasons.append(f"ad_keyword_performance: {classify_bq_failure(e)}")
        bq_save_ok = False

    # ---------- ad_actions_log ----------
    # INSERT only if dry_run=False and actual PAUSEs occurred
    actual_pauses = [p for p in paused_log if not p.get("dry_run", True)]
    if actual_pauses:
        try:
            rows_action = []
            today_str = datetime.now(tz=JST).strftime("%Y-%m-%d")
            for p in actual_pauses:
                # 2026-07-28: ad-group level pauses land in the same table
                target_type = p.get("target_type", "KEYWORD")
                if target_type == "AD_GROUP":
                    target_id = str(p.get("ad_group_id", ""))
                    target_name = f"{p.get('ad_group_name', '')} @ {p.get('campaign_name', '')}"
                else:
                    target_id = str(p.get("criterion_id", ""))
                    target_name = p.get("keyword_text", "")
                rows_action.append({
                    "id": str(uuid.uuid4()),
                    "date": today_str,
                    "account_id": account_id,
                    "action_type": "PAUSE",
                    "target_type": target_type,
                    "target_id": target_id,
                    "target_name": target_name,
                    "reason": p.get("reason", ""),
                    "dry_run": False,
                    "previous_status": "ENABLED",
                    "created_at": now_ts,
                })

            bq_table_action = bq_client.get_table(
                f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.ad_actions_log"
            )
            errors = bq_client.insert_rows_json(bq_table_action, rows_action)
            if errors:
                logger.warning("ad_actions_log INSERT error: %s", errors)
                if failure_reasons is not None:
                    failure_reasons.append(f"ad_actions_log: write rejected ({str(errors)[:80]})")
                bq_save_ok = False
            else:
                print(f"[BQ] ad_actions_log: saved {len(rows_action)} rows")

        except Exception as e:
            logger.warning("Failed to save ad_actions_log: %s", e)
            bq_save_ok = False

    return bq_save_ok


# ===== Discord Notifications =====

def _embed_char_count(embed: dict) -> int:
    """Count only the characters Discord counts toward the 6,000 limit.

    Matches the official list (title / description / field name / field value /
    footer text / author name). color and timestamp do not count.
    """
    total = len(embed.get("title") or "") + len(embed.get("description") or "")
    for field in embed.get("fields") or []:
        total += len(field.get("name") or "") + len(field.get("value") or "")
    total += len((embed.get("footer") or {}).get("text") or "")
    total += len((embed.get("author") or {}).get("name") or "")
    return total


def fit_embeds_to_discord_limits(embeds: list[dict]) -> tuple[list[dict], int]:
    """Return only the embeds that fit in one Discord message.

    Two official limits apply
    (https://docs.discord.com/developers/resources/webhook and
    message#embed-object-embed-limits):

      - at most **10 embeds** per message
      - at most **6,000 characters** across all embeds

    Exceeding either returns 400 Bad Request and **nothing is delivered**.
    From 2026-07-05 to 2026-08-04 every ad alert was lost this way: the script
    packed 14-70 alerts into a single message.

    Returns:
        (embeds to send, number dropped)
    """
    kept: list[dict] = []
    used_chars = 0
    for embed in embeds:
        if len(kept) >= DISCORD_MAX_EMBEDS:
            break
        size = _embed_char_count(embed)
        if used_chars + size > DISCORD_MAX_EMBED_CHARS:
            break
        kept.append(embed)
        used_chars += size
    return kept, len(embeds) - len(kept)


def send_discord_notification(webhook_url: str, content: str, embeds: list[dict]) -> bool:
    """Send message to Discord Webhook.

    Trims to the per-message limits and **states how many were dropped** in the
    message body, so truncation is never silent.

    Returns:
        bool: True when delivered. False when unset or the send failed.
    """
    if not webhook_url:
        return False
    kept, dropped = fit_embeds_to_discord_limits(embeds)
    if dropped > 0:
        notice = (f"Showing {len(kept)} of {len(embeds)} alerts (Discord per-message limit). "
                  "The full list is in the report and the escalation email.")
        content = f"{content}\n{notice}".strip() if content else notice
        logger.info("Discord: sent %d of %d embeds (%d dropped by limit)", len(kept), len(embeds), dropped)
    payload = {"content": content, "embeds": kept}
    try:
        response = httpx.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Failed to send Discord notification: %s", e)
        return False


# Human-readable heading per category, used in the CRITICAL email subject
CRITICAL_SUBJECT_LABELS: dict[str, str] = {
    "account_ctr": "account CTR below the bar",
    "account_ctr_early_warning": "account CTR close to the bar",
    "daily_ctr_streak": "daily CTR below the bar repeatedly",
    "monthly_pace": "monthly CTR pacing below the bar",
    "campaign": "campaign anomaly",
    "ad_group_ctr": "ad group CTR is low",
    "ad_group_impression_surge": "ad group impressions surged",
    "ad_group_pause_deferred": "ad group auto-pause deferred",
    "quality_score": "quality score below the bar",
    "rsa_generic_template": "ad copy still on the template",
    "rsa_broken_headline": "broken ad headline",
    "rsa_duplicate": "duplicate ad copy",
    "bq_save_outage": "BigQuery save failing repeatedly",
    "alert_channel_down": "Discord alerts not delivered",
}


def build_critical_email_subject(critical_alerts: list[dict]) -> str:
    """Build the CRITICAL email subject from what is actually firing.

    Until 2026-08-04 the subject was always `CRITICAL: account CTR {value} (5% required)`.
    When the real cause was a BigQuery outage the subject still showed CTR, so on a day
    with a healthy 7.30% CTR the inbox got **"CRITICAL: account CTR 7.30%"** - a subject
    that contradicts itself and points the reader at the wrong problem.
    """
    if not critical_alerts:
        return "[AdGrants] CRITICAL"
    labels: list[str] = []
    for alert in critical_alerts:
        label = CRITICAL_SUBJECT_LABELS.get(alert.get("category", ""))
        if label and label not in labels:
            labels.append(label)
    if not labels:
        labels = ["see email body"]
    head = " / ".join(labels[:2])
    rest = len(labels) - 2
    if rest > 0:
        head += f" +{rest} more"
    return f"[AdGrants] CRITICAL: {head}"


def classify_bq_failure(error: Exception) -> str:
    """Turn a BigQuery save failure into a one-line cause the reader can act on.

    Hard-coding "typical cause: disabled service account" points the reader at the
    wrong thing whenever the real cause is something else (a scan quota, for example).
    """
    text = str(error)
    if "Custom quota exceeded" in text or "quota" in text.lower():
        return ("the daily scan quota is exhausted "
                "(raise the quota, change how the table is written, or accept the gap)")
    if "403" in text and ("permission" in text.lower() or "denied" in text.lower()):
        return "the service account lacks permission"
    if "401" in text or "invalid_grant" in text or "credential" in text.lower():
        return "the service account credentials are invalid (disabled or missing key)"
    if "streaming buffer" in text:
        return "the previous write has not settled yet (resolves on its own)"
    if "Not found" in text or "404" in text:
        return "the destination table was not found"
    return f"unexpected error: {text[:120]}"


def _first_sentence(message: str) -> str:
    """Take the one line used as a sample in the email.

    Warnings of the same kind share a trailing explanation ("... Ad Grants requires
    QS 3 or above. (auto-pause only at QS<=1)"), so listing three of them repeats the
    same sentence three times. The fact lives in the first sentence; the full text
    stays in the report.
    """
    head = message.splitlines()[0]
    for terminator in ("。", ". "):
        idx = head.find(terminator)
        if idx != -1 and idx < len(head) - len(terminator):
            return head[:idx + len(terminator)].rstrip()
    return head


def build_alert_email_body(
    campaign: dict | None,
    alerts: list[dict],
    executed_pauses: int,
    start_date: str,
    end_date: str,
    report_path: str = "",
) -> str:
    """Build the email body. This is the channel that actually gets read, so the
    email alone has to explain the situation.

    CRITICAL items are listed in full. WARNING items repeat by the dozen, so they are
    grouped by category with a count and a few examples - and **the number omitted is
    always stated**, never silently dropped.
    """
    lines = [f"Ad monitoring for {start_date} to {end_date}.", ""]

    criticals = [a for a in alerts if a["level"] == "CRITICAL"]
    if criticals:
        lines.append("== What is happening ==")
        for a in criticals:
            for i, part in enumerate(a["message"].split("\n")):
                lines.append(("  " if i == 0 else "    ") + part)
        lines.append("")

    lines.append("== Numbers ==")
    if campaign:
        lines.append(f"  CTR          {campaign['ctr'] * 100:.2f}% (Ad Grants requires 5%+)")
        lines.append(f"  Impressions  {campaign['impressions']:,}")
        lines.append(f"  Clicks       {campaign['clicks']:,}")
        lines.append(f"  Conversions  {campaign.get('conversions', 0):.0f}")
        lines.append(f"  Cost         ${campaign.get('cost_dollars', 0):,.2f}")
    else:
        lines.append("  could not be retrieved")
    lines.append(f"  Auto-paused  {executed_pauses}")
    lines.append("")

    warnings = [a for a in alerts if a["level"] == "WARNING"]
    if warnings:
        grouped: dict[str, list[dict]] = {}
        for a in warnings:
            grouped.setdefault(a.get("category", "other"), []).append(a)
        lines.append(f"== Worth a look ({len(warnings)}) ==")
        for category, items in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
            label = CRITICAL_SUBJECT_LABELS.get(category, category)
            lines.append(f"  {label}  {len(items)}")
            for a in items[:EMAIL_WARNING_SAMPLES]:
                lines.append(f"    - {_first_sentence(a['message'])}")
            hidden = len(items) - EMAIL_WARNING_SAMPLES
            if hidden > 0:
                lines.append(f"    - +{hidden} more (full list in the report)")
        lines.append("")

    if report_path:
        lines.append("== Full report ==")
        lines.append(f"  {report_path}")
        lines.append("")

    lines.append("Where to act: /adgrants-optimize (Claude Code) or the Google Ads UI")
    lines.append("Sent by monitor_ad_performance.py.")
    return "\n".join(lines)


def build_critical_email_fingerprint(critical_alerts: list[dict], today_mark: str) -> str:
    """Fingerprint so a *different* problem later the same day still gets sent.

    Suppressing by date alone means a problem that appears in the evening never
    reaches the inbox if something already fired that morning. Since 2026-07-28 the
    job runs twice a day, so that gap is reachable every single day.
    """
    categories = sorted({a.get("category", "") for a in critical_alerts})
    return today_mark + "|" + ",".join(categories)


def build_ad_alert_embed(
    title: str,
    description: str,
    severity: str,
    fields: dict[str, str],
) -> dict:
    """Build Discord Embed for ad alerts"""
    color_map = {
        "CRITICAL": 15158332,   # 0xE74C3C
        "WARNING": 15105826,    # 0xE67E22
        "INFO": 3447003,        # 0x3498DB
        "SUCCESS": 3066993,     # 0x2ECC71
    }
    color = color_map.get(severity, color_map["INFO"])

    # Generate current time in JST then convert to UTC for ISO8601
    now_jst = datetime.now(tz=JST)
    now_utc = now_jst.astimezone(timezone.utc)
    timestamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "title": title,
        "description": description,
        "color": color,
        "fields": [{"name": k, "value": v, "inline": True} for k, v in fields.items()],
        "footer": {"text": "Google Ads Monitor | Ad Grants"},
        "timestamp": timestamp,
    }


# ===== Report Output =====

def print_report(
    start_date: str,
    end_date: str,
    campaign: dict | None,
    ad_groups: list[dict],
    keywords: list[dict],
    ads: list[dict],
    alerts: list[dict],
    paused_log: list[dict],
    suggestions: dict,
):
    """Output formatted report to stdout"""
    W = 64

    def sep(char="="):
        print(char * W)

    def section(title):
        print(f"\n{title}")
        sep("-")

    sep()
    print("Google Ads Performance Report")
    print(f"Period: {start_date} to {end_date}")
    campaign_names_str = ", ".join(CAMPAIGN_NAMES)
    print(f"Campaigns: {campaign_names_str}")
    print(f"Customer ID: {CUSTOMER_ID}")
    sep()

    # --- Account-wide ---
    section("Account-wide (all campaigns combined)")
    if campaign is None:
        print("  No data (new campaign, or no impressions yet)")
    else:
        print(f"  Impressions: {campaign['impressions']:,}")
        print(f"  Clicks      : {campaign['clicks']:,}")
        print(f"  CTR         : {format_ctr(campaign['ctr'])}")
        print(f"  Avg CPC     : ${campaign['avg_cpc_dollars']:.2f}")
        print(f"  Cost        : ${campaign['cost_dollars']:.2f}")
        print(f"  Conversions : {campaign['conversions']:.1f}")
        print(f"  Daily budget: ${campaign['daily_budget_dollars']:.0f}")

        # Per-campaign breakdown
        if campaign.get("campaigns"):
            print()
            print("  Per-campaign breakdown:")
            for c in campaign["campaigns"]:
                ctr_flag = "[WARNING]" if c["ctr"] < CTR_CRITICAL_THRESHOLD else ""
                print(
                    f"    [{c['campaign_name']}] "
                    f"{c['impressions']:,}imp / {c['clicks']:,}click / "
                    f"CTR {c['ctr']*100:.2f}% {ctr_flag}"
                )

    # --- Per ad group ---
    section("Per Ad Group")
    if not ad_groups:
        print("  No data")
    else:
        col_w = [20, 6, 6, 8, 10]
        header = (
            f"  {'Ad Group':<{col_w[0]}}"
            f"{'Imp':>{col_w[1]}}"
            f"{'Click':>{col_w[2]}}"
            f"{'CTR':>{col_w[3]}}"
            f"{'Cost':>{col_w[4]}}"
        )
        print(header)
        print("  " + "-" * (sum(col_w) + 4))
        for ag in ad_groups:
            name = ag["ad_group_name"][:col_w[0]]
            ctr_flag = "[WARNING]" if ag["ctr"] < CTR_AD_GROUP_MIN and ag["impressions"] >= 10 else "  "
            print(
                f"  {name:<{col_w[0]}}"
                f"{ag['impressions']:>{col_w[1]},}"
                f"{ag['clicks']:>{col_w[2]},}"
                f"{ag['ctr']*100:>{col_w[3]-1}.1f}%"
                f"  ${ag['cost_dollars']:>7.2f} {ctr_flag}"
            )

    # --- Per keyword ---
    section("Per Keyword (top 20)")
    if not keywords:
        print("  No data")
    else:
        display_kws = keywords[:20]
        print(
            f"  {'Keyword':<28}"
            f"{'Match':<8}"
            f"{'Imp':>5}"
            f"{'Clk':>5}"
            f"{'CTR':>7}"
            f"{'QS':>3}"
            f"  Status"
        )
        print("  " + "-" * 70)
        for kw in display_kws:
            kw_text = kw["keyword_text"][:27]
            match = kw["match_type"][:6]
            qs = str(kw["quality_score"]) if kw["quality_score"] is not None else " -"
            status_flag = ""
            if kw["status"] == "PAUSED":
                status_flag = "[PAUSED]"
            elif is_pause_target(kw)[0]:
                status_flag = "[PAUSE_CANDIDATE]"
            print(
                f"  {kw_text:<28}"
                f"{match:<8}"
                f"{kw['impressions']:>5,}"
                f"{kw['clicks']:>5,}"
                f"{kw['ctr']*100:>6.1f}%"
                f"{qs:>3}"
                f"  {status_flag}"
            )

    # --- Per ad ---
    section("Per Ad")
    if not ads:
        print("  No data")
    else:
        for ad in ads:
            headline_str = " | ".join(ad["headlines"][:2]) if ad["headlines"] else "(no headlines)"
            print(
                f"  [{ad['ad_group_name']}] "
                f"'{headline_str[:40]}'"
                f"  {ad['impressions']:,}imp / {format_ctr_raw(ad['ctr'])}"
            )

    # --- Alerts ---
    section("Alerts")
    if not alerts:
        print("  No alerts [OK]")
    else:
        level_icons = {"CRITICAL": "[CRITICAL]", "WARNING": "[WARNING]", "INFO": "[INFO]"}
        for alert in alerts:
            icon = level_icons.get(alert["level"], "  ")
            print(f"  [{alert['level']}] {icon} {alert['message']}")

    # --- Auto actions ---
    section("Auto Actions (keyword pauses)")
    if not paused_log:
        print("  No pauses [OK]")
    else:
        # 2026-07-28: ad-group pauses are mixed in, so render by target type
        for p in paused_log:
            dry_label = "[DRY-RUN] " if p.get("dry_run") else ""
            reason = p.get("reason", "")
            if p.get("target_type") == "AD_GROUP":
                print(
                    f"  [PAUSED] {dry_label}[ad group] '{p.get('ad_group_name', '')}'"
                    f" ({p.get('campaign_name', '')}) -- {reason}"
                )
            else:
                print(
                    f"  [PAUSED] {dry_label}[keyword] '{p.get('keyword_text', '')}'"
                    f" ({p.get('ad_group_name', '')}) -- {reason}"
                )

    # --- Recommended actions ---
    section("Recommended Actions")

    print("\n  [Top 5 Keywords (10+ clicks, sorted by CTR)]")
    if suggestions["top_performers"]:
        for kw in suggestions["top_performers"]:
            print(
                f"    [OK] '{kw['keyword_text']}'"
                f"  CTR {format_ctr_raw(kw['ctr'])}"
                f"  {kw['clicks']} clicks"
            )
    else:
        print("    Insufficient data (no keywords with 10+ clicks)")

    print("\n  [Bottom 5 Keywords (50+ imp, sorted by CTR)]")
    if suggestions["bottom_performers"]:
        for kw in suggestions["bottom_performers"]:
            print(
                f"    [LOW] '{kw['keyword_text']}'"
                f"  CTR {format_ctr_raw(kw['ctr'])}"
                f"  {kw['impressions']}imp"
            )
    else:
        print("    None")

    print("\n  [Pause candidates (per-campaign criteria)]")
    if suggestions["pause_candidates"]:
        for kw in suggestions["pause_candidates"]:
            print(
                f"    [CANDIDATE] '{kw['keyword_text']}'"
                f"  CTR {format_ctr_raw(kw['ctr'])}"
                f"  {kw['impressions']}imp"
                f"  -> can auto-pause with --auto-pause"
            )
    else:
        print("    No pause candidates [OK]")

    print("\n  [New theme candidates]")
    if suggestions["new_keyword_themes"]:
        for theme in suggestions["new_keyword_themes"]:
            print(f"    [IDEA] {theme}")
    else:
        print("    Insufficient data for top keywords")

    sep()
    print(f"Report generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    sep()


def build_json_report(
    start_date: str,
    end_date: str,
    campaign: dict | None,
    ad_groups: list[dict],
    keywords: list[dict],
    ads: list[dict],
    alerts: list[dict],
    paused_log: list[dict],
    suggestions: dict,
) -> dict:
    """Build and return JSON report"""
    return {
        "generated_at": datetime.now().isoformat(),
        "period": {"start": start_date, "end": end_date},
        "customer_id": CUSTOMER_ID,
        "campaign_names": CAMPAIGN_NAMES,
        "campaign": campaign,
        "ad_groups": ad_groups,
        "keywords": keywords,
        "ads": ads,
        "alerts": alerts,
        "actions_taken": paused_log,
        "suggestions": {
            "top_performers": suggestions["top_performers"],
            "bottom_performers": suggestions["bottom_performers"],
            "pause_candidates": suggestions["pause_candidates"],
            "new_keyword_themes": suggestions["new_keyword_themes"],
        },
        "thresholds": {
            "account_ctr_critical": CTR_CRITICAL_THRESHOLD,
            "account_ctr_warning": CTR_WARNING_THRESHOLD,
            "ad_group_ctr_min": CTR_AD_GROUP_MIN,
            "kw_pause_at_100imp": CTR_KW_PAUSE_100IMP,
            "kw_pause_at_50imp": CTR_KW_PAUSE_50IMP,
            "quality_score_min": QS_MIN,
            # Ad-group auto-pause and long-tail operation (added 2026-07-28)
            "ad_group_pause_min_imp": AG_PAUSE_MIN_IMP,
            "ad_group_pause_max_per_run": AG_PAUSE_MAX_PER_RUN,
            "ad_group_min_enabled": AG_MIN_ENABLED,
            "longtail_target_ctr": LONGTAIL_TARGET_CTR,
            "longtail_budget_use_ratio": LONGTAIL_BUDGET_USE_RATIO,
            "longtail_min_mtd_imp": LONGTAIL_MIN_MTD_IMP,
        },
    }


# ===== Ad-group level auto-pause and long-tail operation (added 2026-07-28) =====
#
# Auto-pause used to cover keywords only (target_type="KEYWORD"), so a low-CTR ad group
# kept serving impressions no matter how often it was detected. This closes the gap:
# detect -> pause, and, when the month has room to spare, keep (or resume) the long tail.

AG_PAUSE_EXCLUSION_PATH = SCRIPT_DIR / "ag_pause_exclusion.json"
AG_PAUSE_LOG_PATH = SCRIPT_DIR / "ag_pause_log.json"


def load_ag_pause_exclusion() -> dict[str, str]:
    """Ad-group level pause exclusion list ({ad_group_name: reason}). Empty if absent."""
    if not AG_PAUSE_EXCLUSION_PATH.exists():
        return {}
    try:
        with open(AG_PAUSE_EXCLUSION_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {e["ad_group_name"]: e.get("reason", "") for e in data.get("entries", [])}
    except Exception as e:  # noqa: BLE001 -- a broken exclusion list must not stop monitoring
        logger.warning("Failed to load ad group exclusion list: %s", e)
        return {}


def days_left_in_month(now: datetime | None = None) -> float:
    """Days remaining in the calendar month (fractional); the judged unit is the month."""
    now = now or datetime.now(tz=JST)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1,
                                 hour=0, minute=0, second=0, microsecond=0)
    else:
        next_month = now.replace(month=now.month + 1, day=1,
                                 hour=0, minute=0, second=0, microsecond=0)
    return max(0.0, (next_month - now).total_seconds() / 86400)


def compute_ctr_headroom(mtd: dict | None, now: datetime | None = None) -> dict:
    """Derive the click budget available for low-CTR delivery from month-to-date results.

    surplus clicks = MTD clicks - target CTR * MTD impressions.
    A positive surplus is savings built up above the target; part of it funds the long tail.
    """
    imp = (mtd or {}).get("impressions", 0)
    clicks = (mtd or {}).get("clicks", 0)
    surplus = clicks - LONGTAIL_TARGET_CTR * imp
    measurable = imp >= LONGTAIL_MIN_MTD_IMP
    return {
        "mtd_impressions": imp,
        "mtd_clicks": clicks,
        "mtd_ctr": (clicks / imp) if imp else 0.0,
        "surplus_clicks": surplus,
        # No surplus, or not enough volume to judge -> zero budget -> strict pausing
        "budget_clicks": max(0.0, surplus * LONGTAIL_BUDGET_USE_RATIO) if measurable else 0.0,
        "measurable": measurable,
        "days_left": days_left_in_month(now),
        "target_ctr": LONGTAIL_TARGET_CTR,
    }


def estimate_longtail_cost(ag: dict, headroom: dict, window_days: int) -> float:
    """Clicks this ad group would cost (versus target) if kept running until month end."""
    if window_days <= 0:
        return 0.0
    daily_imp = ag["impressions"] / window_days
    gap = max(0.0, headroom["target_ctr"] - ag["ctr"])
    return daily_imp * headroom["days_left"] * gap


def identify_ad_group_pause_targets(
    ad_groups: list[dict],
    headroom: dict | None = None,
    window_days: int = 7,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split ad groups into pause / defer / keep-as-long-tail.

    Criteria: ENABLED, impressions >= AG_PAUSE_MIN_IMP, CTR < CTR_AD_GROUP_MIN.
    Candidates affordable within the click budget are kept. The eligibility floor is
    deliberately small: a large floor lets borderline offenders slip through every time.

    Ad groups with at least AG_PAUSE_CV_PROTECT conversions in-window are excluded.
    Pausing what converts in order to protect a click-through rate inverts ends and
    means (see the constant for the incident this prevents).
    """
    exclusion = load_ag_pause_exclusion()
    enabled = [ag for ag in ad_groups if ag.get("status") == "ENABLED"]

    low_ctr = [
        ag for ag in enabled
        if ag["impressions"] >= AG_PAUSE_MIN_IMP
        and ag["ctr"] < CTR_AD_GROUP_MIN
        and ag["ad_group_name"] not in exclusion
    ]

    # Keep what converts. Always say what was spared -- never trim silently.
    candidates = []
    for ag in low_ctr:
        if ag.get("conversions", 0.0) >= AG_PAUSE_CV_PROTECT:
            logger.warning(
                "Sparing %s (%s): %d impr / %d clicks / %.2f%% CTR / %.1f conversions. "
                "Tighten its keywords instead of pausing it",
                ag["ad_group_name"], ag.get("campaign_name", "-"),
                ag["impressions"], ag["clicks"], ag["ctr"] * 100,
                ag.get("conversions", 0.0),
            )
            continue
        candidates.append(ag)

    # Spend the budget cheapest-first (i.e. highest CTR first)
    kept: list[dict] = []
    to_pause: list[dict] = []
    if headroom and headroom["budget_clicks"] > 0:
        remaining = headroom["budget_clicks"]
        for ag in sorted(candidates, key=lambda a: -a["ctr"]):
            cost = estimate_longtail_cost(ag, headroom, window_days)
            if cost <= remaining:
                remaining -= cost
                kept.append(dict(ag, longtail_cost=round(cost, 1)))
            else:
                to_pause.append(ag)
    else:
        to_pause = list(candidates)

    # Handle the biggest bleeders first
    to_pause.sort(key=lambda a: -a["impressions"])

    # Never strand the account: keep at least AG_MIN_ENABLED ad groups running
    allowed_by_floor = max(0, len(enabled) - AG_MIN_ENABLED)
    limit = min(AG_PAUSE_MAX_PER_RUN, allowed_by_floor)

    return to_pause[:limit], to_pause[limit:], kept


def pause_ad_groups(client, targets: list[dict], dry_run: bool = False) -> list[dict]:
    """Pause ad groups. The undo record is written before anything is mutated."""
    if not targets:
        return []

    if dry_run:
        for ag in targets:
            logger.info(
                "[DRY-RUN] ad group pause target: %s (impressions %s / CTR %s)",
                ag["ad_group_name"], f"{ag['impressions']:,}", format_ctr_raw(ag["ctr"]),
            )
        return [dict(ag, dry_run=True) for ag in targets]

    now_iso = datetime.now(tz=JST).isoformat()

    # 1) Persist the undo record BEFORE mutating
    history = []
    if AG_PAUSE_LOG_PATH.exists():
        try:
            with open(AG_PAUSE_LOG_PATH, encoding="utf-8") as f:
                history = json.load(f).get("entries", [])
        except Exception as e:  # noqa: BLE001 -- a broken history must not block this pause
            logger.warning("Failed to read ad group pause log (recreating): %s", e)
    history.extend([
        {
            "paused_at": now_iso,
            "ad_group_id": ag["ad_group_id"],
            "ad_group_name": ag["ad_group_name"],
            "campaign_name": ag["campaign_name"],
            "resource_name": ag["resource_name"],
            "impressions_7d": ag["impressions"],
            "clicks_7d": ag["clicks"],
            "ctr_7d": round(ag["ctr"], 5),
            "previous_status": "ENABLED",
        }
        for ag in targets
    ])
    with open(AG_PAUSE_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump({"entries": history}, f, ensure_ascii=False, indent=2)

    # 2) Mutate
    ag_service = client.get_service("AdGroupService")
    ops = []
    for ag in targets:
        op = client.get_type("AdGroupOperation")
        op.update.resource_name = ag["resource_name"]
        op.update.status = client.enums.AdGroupStatusEnum.PAUSED
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        ops.append(op)

    try:
        resp = ag_service.mutate_ad_groups(customer_id=CUSTOMER_ID, operations=ops)
        logger.info("Paused ad groups: %d", len(resp.results))
    except GoogleAdsException as e:
        names = ", ".join(ag["ad_group_name"] for ag in targets[:5])
        logger.error("Failed to pause ad groups: %s", e)
        send_alert_email(
            subject="[AdGrants] CRITICAL: ad group pause failed (nothing was paused)",
            body=(
                f"mutate_ad_groups failed for {len(targets)} target(s), first 5: {names}\n"
                f"Error: {e}\n"
                "Low-CTR ad groups are still serving. Pause them in the UI."
            ),
        )
        return []

    # 3) Verify from live data that they really are paused
    rn_list = ", ".join(f"'{ag['resource_name']}'" for ag in targets)
    verify_query = (
        "SELECT ad_group.resource_name, ad_group.status "
        f"FROM ad_group WHERE ad_group.resource_name IN ({rn_list})"
    )
    try:
        svc = client.get_service("GoogleAdsService")
        statuses = {
            r.ad_group.resource_name: r.ad_group.status.name
            for r in svc.search(customer_id=CUSTOMER_ID, query=verify_query)
        }
        not_paused = [
            ag["ad_group_name"] for ag in targets
            if statuses.get(ag["resource_name"]) != "PAUSED"
        ]
        if not_paused:
            logger.error("verify: %d ad group(s) not paused: %s", len(not_paused), not_paused[:5])
            send_alert_email(
                subject="[AdGrants] CRITICAL: ad group pause verification failed",
                body=(
                    f"{len(not_paused)} ad group(s) are not PAUSED after the mutate:\n"
                    + "\n".join(f"- {n}" for n in not_paused[:10])
                ),
            )
        else:
            logger.info("verify: all %d ad group(s) confirmed paused", len(targets))
    except Exception as e:  # noqa: BLE001 -- verification failure must not lose the record
        logger.warning("Failed to verify ad group pause: %s", e)

    return [dict(ag, dry_run=False) for ag in targets]


def fetch_ad_group_status_by_resource(client, resource_names: list[str]) -> dict[str, str]:
    """Look up the current status of specific ad groups (added 2026-08-02).

    The regular ad group query is segmented by date, so groups with no delivery at
    all are never returned. Putting brand-new (PAUSED, zero-history) groups into the
    trial pool therefore needs a separate, date-free lookup.
    """
    if not resource_names:
        return {}
    out: dict[str, str] = {}
    svc = client.get_service("GoogleAdsService")
    # Keep the IN clause from growing without bound
    for i in range(0, len(resource_names), 200):
        chunk = resource_names[i:i + 200]
        # These come from a JSON ledger and go straight into GAQL -- check the shape
        for rn in chunk:
            if not re.fullmatch(r"customers/\d+/adGroups/\d+", rn):
                raise ValueError(f"Malformed resource name: {rn!r}")
        rn_list = ", ".join(f"'{rn}'" for rn in chunk)
        query = (
            "SELECT ad_group.resource_name, ad_group.name, ad_group.status "
            f"FROM ad_group WHERE ad_group.resource_name IN ({rn_list})"
        )
        try:
            for row in svc.search(customer_id=CUSTOMER_ID, query=query):
                out[row.ad_group.resource_name] = row.ad_group.status.name
        except Exception as e:  # noqa: BLE001 -- a failed lookup must not stop monitoring
            logger.warning("Failed to look up ad group status: %s", e)
    return out


ACCOUNT_HISTORY_START = "2026-01-01"  # How far back to look for any delivery


def adopt_never_delivered_paused_ad_groups(client, today_str: str) -> list[dict]:
    """Take never-delivered paused ad groups into the long-tail inventory.

    Added 2026-08-02. Ad groups created by the CI-side article sync cannot be
    registered in ag_pause_log.json -- the runner is ephemeral and the ledger
    only exists on the operator's machine. Left alone they stay paused forever
    and never enter any long-tail decision.

    So treat Google Ads as the source of truth and pick up anything that is
      - a "記事: " ad group in a monitored campaign, currently PAUSED, and
      - has zero impressions since {ACCOUNT_HISTORY_START} (never delivered)

    Requiring "never delivered" is what keeps manually paused ad groups out of
    the inventory: anything a human paused necessarily has delivery history.
    Names on the auto-resume exclusion list are skipped too.
    """
    svc = client.get_service("GoogleAdsService")
    campaign_filter = build_campaign_filter()

    # 1) Paused article ad groups (no date segment, so zero-history rows appear)
    paused: dict[str, dict] = {}
    query_all = (
        "SELECT ad_group.id, ad_group.name, ad_group.resource_name, campaign.name "
        f"FROM ad_group WHERE {campaign_filter} AND ad_group.status = 'PAUSED'"
    )
    for row in svc.search(customer_id=CUSTOMER_ID, query=query_all):
        if not row.ad_group.name.startswith("記事: "):
            continue
        paused[row.ad_group.resource_name] = {
            "ad_group_id": row.ad_group.id,
            "ad_group_name": row.ad_group.name,
            "campaign_name": row.campaign.name,
            "resource_name": row.ad_group.resource_name,
        }
    if not paused:
        return []

    # 2) Anything with at least one impression since the start date
    delivered: set[str] = set()
    query_hist = (
        "SELECT ad_group.resource_name, metrics.impressions FROM ad_group "
        f"WHERE {campaign_filter} AND segments.date BETWEEN "
        f"'{_validate_gaql_value(ACCOUNT_HISTORY_START, 'date')}' AND "
        f"'{_validate_gaql_value(today_str, 'date')}'"
    )
    for row in svc.search(customer_id=CUSTOMER_ID, query=query_hist):
        if row.metrics.impressions > 0:
            delivered.add(row.ad_group.resource_name)

    exclusion = load_ag_pause_exclusion()
    try:
        if AG_PAUSE_LOG_PATH.exists():
            with open(AG_PAUSE_LOG_PATH, encoding="utf-8") as f:
                entries = json.load(f).get("entries", [])
        else:
            entries = []
    except Exception as e:  # noqa: BLE001 -- a broken ledger must not stop monitoring
        logger.warning("Failed to read ad group pause log: %s", e)
        return []

    known = {e.get("resource_name") for e in entries}
    now_iso = datetime.now(tz=JST).isoformat()
    adopted: list[dict] = []
    for rn, ag in paused.items():
        if rn in known or rn in delivered:
            continue
        if ag["ad_group_name"] in exclusion:
            continue
        entries.append({
            "paused_at": now_iso,
            "source": "discovered_never_delivered",
            "ad_group_id": ag["ad_group_id"],
            "ad_group_name": ag["ad_group_name"],
            "campaign_name": ag["campaign_name"],
            "resource_name": rn,
            "impressions_7d": 0,
            "clicks_7d": 0,
            "ctr_7d": 0.0,
            "previous_status": "NEW",
        })
        adopted.append(ag)

    if adopted:
        try:
            with open(AG_PAUSE_LOG_PATH, "w", encoding="utf-8") as f:
                json.dump({"entries": entries}, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001 -- a failed write must not stop monitoring
            logger.warning("Failed to update ad group pause log: %s", e)
            return []
    return adopted


def identify_ad_group_resume_targets(
    ad_groups: list[dict],
    headroom: dict,
    window_days: int,
    already_spent: float,
    client=None,
) -> list[dict]:
    """Pick auto-paused ad groups to resume while the click budget allows it.

    Only ad groups recorded in ag_pause_log.json are eligible -- manual pauses and
    the exclusion list are never touched. Resuming requires LONGTAIL_RESUME_MARGIN
    times the cost in budget so groups do not flap between paused and enabled.
    """
    if not headroom.get("measurable") or headroom["budget_clicks"] <= already_spent:
        return []
    if not AG_PAUSE_LOG_PATH.exists():
        return []
    try:
        with open(AG_PAUSE_LOG_PATH, encoding="utf-8") as f:
            entries = json.load(f).get("entries", [])
    except Exception as e:  # noqa: BLE001 -- a broken record must not stop monitoring
        logger.warning("Failed to read ad group pause log: %s", e)
        return []

    exclusion = load_ag_pause_exclusion()
    # resource_name was added to the query on 2026-07-28; tolerate older shapes
    current = {ag["resource_name"]: ag for ag in ad_groups if ag.get("resource_name")}

    # Never-delivered groups do not appear in the date-segmented query, so pull
    # them from the ledger and look up their status separately.
    unseen = [
        e for e in entries
        if e.get("resource_name") and e.get("ad_group_name")
        and e["ad_group_name"] not in exclusion
        and e["resource_name"] not in current
        and not e.get("impressions_7d", 0)
    ]
    unseen_status: dict[str, str] = {}
    if unseen and client is not None:
        unseen_status = fetch_ad_group_status_by_resource(
            client, [e["resource_name"] for e in unseen]
        )

    candidates: list[dict] = []
    explore: list[dict] = []  # Zero-history newcomers (trial slots, added 2026-08-02)
    for e in entries:
        rn = e.get("resource_name")
        name = e.get("ad_group_name")
        if not rn or not name or name in exclusion:
            continue
        ag = current.get(rn)
        if ag is None:
            # Absent from the metrics query = never delivered. Freshly synced
            # articles land here.
            if unseen_status.get(rn) != "PAUSED":
                continue  # Already running, removed, or the lookup failed
            explore.append({
                "ad_group_name": name,
                "resource_name": rn,
                "campaign_name": e.get("campaign_name", ""),
                "impressions": 0,
                "clicks": 0,
                "ctr": 0.0,
                "status": "PAUSED",
                "registered_at": e.get("paused_at", ""),
                "explore": True,
                "longtail_cost": LONGTAIL_EXPLORE_COST_CLICKS,
            })
            continue
        if ag.get("status") != "PAUSED":
            continue  # Already running, or outside the current scope
        # Long-paused groups have no impressions left in the trailing window,
        # so fall back to the performance recorded at pause time
        if ag["impressions"] > 0:
            basis = ag
        elif e.get("impressions_7d", 0) > 0:
            basis = {
                **ag,
                "impressions": e["impressions_7d"],
                "clicks": e.get("clicks_7d", 0),
                "ctr": e.get("ctr_7d", 0.0),
            }
        else:
            # No data at all = never delivered. Handle as a trial slot instead.
            explore.append(dict(ag, registered_at=e.get("paused_at", ""),
                                explore=True, longtail_cost=LONGTAIL_EXPLORE_COST_CLICKS))
            continue
        candidates.append(basis)

    remaining = headroom["budget_clicks"] - already_spent
    resume: list[dict] = []
    # Proven groups first, cheapest (highest CTR) first within them
    for ag in sorted(candidates, key=lambda a: -a["ctr"]):
        cost = estimate_longtail_cost(ag, headroom, window_days)
        if cost <= 0:
            continue
        if cost * LONGTAIL_RESUME_MARGIN <= remaining:
            remaining -= cost
            resume.append(dict(ag, longtail_cost=round(cost, 1)))
        if len(resume) >= LONGTAIL_RESUME_MAX_PER_RUN:
            return resume

    # Spend whatever is left on trials, oldest registration first so every
    # article eventually gets its turn.
    explored = 0
    for ag in sorted(explore, key=lambda a: a.get("registered_at", "")):
        if explored >= LONGTAIL_EXPLORE_MAX_PER_RUN:
            break
        if LONGTAIL_EXPLORE_COST_CLICKS * LONGTAIL_RESUME_MARGIN > remaining:
            break
        remaining -= LONGTAIL_EXPLORE_COST_CLICKS
        resume.append(ag)
        explored += 1
    return resume


def resume_ad_groups(client, targets: list[dict], dry_run: bool = False) -> list[dict]:
    """Resume ad groups (PAUSED -> ENABLED) within the click budget."""
    if not targets:
        return []

    if dry_run:
        for ag in targets:
            logger.info(
                "[DRY-RUN] ad group resume target: %s (impressions %s / CTR %s / cost %.1f clicks)",
                ag["ad_group_name"], f"{ag['impressions']:,}",
                format_ctr_raw(ag["ctr"]), ag.get("longtail_cost", 0),
            )
        return [dict(ag, dry_run=True) for ag in targets]

    ag_service = client.get_service("AdGroupService")
    ops = []
    for ag in targets:
        op = client.get_type("AdGroupOperation")
        op.update.resource_name = ag["resource_name"]
        op.update.status = client.enums.AdGroupStatusEnum.ENABLED
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        ops.append(op)

    try:
        resp = ag_service.mutate_ad_groups(customer_id=CUSTOMER_ID, operations=ops)
        logger.info("Resumed ad groups: %d", len(resp.results))
    except GoogleAdsException as e:
        names = ", ".join(ag["ad_group_name"] for ag in targets[:5])
        logger.error("Failed to resume ad groups: %s", e)
        send_alert_email(
            subject="[AdGrants] ad group resume failed",
            body=(
                f"mutate_ad_groups (resume) failed for {len(targets)} target(s), first 5: {names}\n"
                f"Error: {e}\n"
                "They stay paused, so CTR is unaffected, but the long tail is not running."
            ),
        )
        return []

    # Drop resumed entries from the pause log so they are not picked up again
    try:
        with open(AG_PAUSE_LOG_PATH, encoding="utf-8") as f:
            entries = json.load(f).get("entries", [])
        resumed_rn = {ag["resource_name"] for ag in targets}
        kept_entries = [e for e in entries if e.get("resource_name") not in resumed_rn]
        with open(AG_PAUSE_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump({"entries": kept_entries}, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001 -- bookkeeping failure does not undo the resume
        logger.warning("Failed to update ad group pause log: %s", e)

    return [dict(ag, dry_run=False) for ag in targets]


def load_previous_ad_group_snapshot() -> dict[str, dict]:
    """Read per-ad-group figures from the most recent report JSON ({name: {imp, clicks}}).

    Reports are saved at the end of main(), so at startup this is the previous run.
    With two runs a day, the evening run compares against the same morning.
    """
    if not REPORTS_DIR.exists():
        return {}
    files = sorted(REPORTS_DIR.glob("ads-performance-*.json"))
    if not files:
        return {}
    try:
        with open(files[-1], encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001 -- a broken report must not stop monitoring
        logger.warning("Failed to read previous report: %s", e)
        return {}
    snapshot: dict[str, dict] = {}
    for ag in data.get("ad_groups", []):
        name = ag.get("ad_group_name")
        if not name:
            continue
        e = snapshot.setdefault(name, {"impressions": 0, "clicks": 0})
        e["impressions"] += ag.get("impressions", 0)
        e["clicks"] += ag.get("clicks", 0)
    return snapshot


def detect_impression_surge(ad_groups: list[dict]) -> list[dict]:
    """Detect ad groups whose impressions exploded while CTR collapsed."""
    prev = load_previous_ad_group_snapshot()
    if not prev:
        return []

    current: dict[str, dict] = {}
    for ag in ad_groups:
        if ag.get("status") != "ENABLED":
            continue
        e = current.setdefault(ag["ad_group_name"], {"impressions": 0, "clicks": 0})
        e["impressions"] += ag["impressions"]
        e["clicks"] += ag["clicks"]

    surges = []
    for name, cur in current.items():
        before = prev.get(name, {}).get("impressions", 0)
        now_imp = cur["impressions"]
        if now_imp < AG_SURGE_MIN_IMP or before <= 0:
            continue
        ratio = now_imp / before
        ctr = cur["clicks"] / now_imp if now_imp else 0.0
        if ratio >= AG_SURGE_RATIO and ctr < CTR_AD_GROUP_MIN:
            surges.append({
                "level": "CRITICAL",
                "category": "ad_group_impression_surge",
                "message": (
                    f"Ad group '{name}' impressions jumped {before:,} -> {now_imp:,}"
                    f" ({ratio:.1f}x) with CTR at {format_ctr_raw(ctr)}."
                    " Possible impression surge incident"
                ),
                "ad_group_name": name,
            })
    return surges


def save_reports(date_str: str, report_data: dict, human_text: str):
    """Save JSON report and text report to files"""
    ensure_reports_dir()

    json_path = REPORTS_DIR / f"ads-performance-{date_str}.json"
    txt_path = REPORTS_DIR / f"ads-performance-{date_str}.txt"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2, default=str)

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(human_text)

    print(f"\n[Saved] JSON: {json_path}")
    print(f"[Saved] TXT : {txt_path}")


def build_weekly_summary_embed(
    campaign: dict | None,
    keywords: list[dict],
    alerts: list[dict],
    paused_log: list[dict],
    suggestions: dict,
    start_date: str,
    end_date: str,
) -> dict:
    """Build weekly summary Discord embed."""
    if campaign is None:
        return build_ad_alert_embed(
            title="Weekly Ad Performance Summary",
            description=f"Period: {start_date} to {end_date}\nNo data",
            severity="INFO",
            fields={},
        )

    ctr_pct = f"{campaign['ctr'] * 100:.2f}%"
    ctr_status = "OK" if campaign['ctr'] >= CTR_WARNING_THRESHOLD else "WARNING" if campaign['ctr'] >= CTR_CRITICAL_THRESHOLD else "CRITICAL"

    top_kws = suggestions.get("top_performers", [])
    top_kw_text = "\n".join(
        f"• {kw['keyword_text']} ({kw['ctr']*100:.1f}%)"
        for kw in top_kws[:3]
    ) or "Insufficient data"

    bottom_kws = suggestions.get("bottom_performers", [])
    bottom_kw_text = "\n".join(
        f"• {kw['keyword_text']} ({kw['ctr']*100:.1f}%)"
        for kw in bottom_kws[:3]
    ) or "None"

    critical_count = sum(1 for a in alerts if a["level"] == "CRITICAL")
    warning_count = sum(1 for a in alerts if a["level"] == "WARNING")
    pause_count = len(paused_log)

    severity = "SUCCESS"
    if critical_count > 0:
        severity = "CRITICAL"
    elif warning_count > 0:
        severity = "WARNING"

    return build_ad_alert_embed(
        title="Weekly Ad Performance Summary",
        description=f"Campaigns: {', '.join(CAMPAIGN_NAMES)}\nPeriod: {start_date} to {end_date}",
        severity=severity,
        fields={
            f"CTR [{ctr_status}]": ctr_pct,
            "Clicks": f"{campaign['clicks']:,}",
            "Impressions": f"{campaign['impressions']:,}",
            "Cost": f"${campaign['cost_dollars']:.2f}",
            "CV": f"{campaign['conversions']:.1f}",
            "PAUSE count": str(pause_count),
            "TOP KW": top_kw_text,
            "Low CTR KW": bottom_kw_text,
            "Alerts": f"CRITICAL:{critical_count} / WARN:{warning_count}",
        },
    )


def check_tier_promotion(
    ad_groups: list[dict],
    keywords: list[dict],
    discord: bool = False,
) -> dict | None:
    """Check Tier promotion conditions and return suggestion if conditions met.

    Promotion criteria:
    - All ad groups in Tier N maintain CTR >= 5% for 14 days
    - All keywords in Tier N (with measured QS) have QS >= 3

    Note: Currently checks CTR/QS at snapshot time as BQ historical data reference
    is needed for 14-day check (planned for Phase 2 expansion).
    """
    TIER_MIN_CTR = 0.05
    TIER_MIN_QS = 3

    # Identify ad groups in article campaign
    # Target ad groups whose name starts with "AG-"
    tier_groups = [ag for ag in ad_groups if ag.get("ad_group_name", "").startswith("AG-")]
    if not tier_groups:
        return None

    # Tier 1: AG-01 ~ AG-05
    tier1_groups = [ag for ag in tier_groups if ag["ad_group_name"][:5] in ("AG-01", "AG-02", "AG-03", "AG-04", "AG-05")]
    if not tier1_groups:
        return None

    # Tier 2 already exists?
    tier2_groups = [ag for ag in tier_groups if ag["ad_group_name"][:5] in ("AG-06", "AG-07", "AG-08", "AG-09", "AG-10")]
    if tier2_groups:
        return None  # Tier 2 already deployed

    # Check all Tier 1 groups meet criteria
    all_meet = True
    details = []
    for ag in tier1_groups:
        ctr_ok = ag["ctr"] >= TIER_MIN_CTR or ag["impressions"] < 10  # Skip if insufficient imp
        detail = f"{ag['ad_group_name']}: CTR={ag['ctr']*100:.1f}% imp={ag['impressions']}"
        if not ctr_ok:
            all_meet = False
            detail += " [FAIL]"
        else:
            detail += " [OK]"
        details.append(detail)

    # Check QS for Tier 1 keywords
    tier1_kw_names = {ag["ad_group_name"] for ag in tier1_groups}
    tier1_kws = [kw for kw in keywords if kw.get("ad_group_name") in tier1_kw_names]
    qs_issues = []
    for kw in tier1_kws:
        if kw["quality_score"] is not None and kw["quality_score"] < TIER_MIN_QS:
            all_meet = False
            qs_issues.append(f"{kw['keyword_text']}: QS={kw['quality_score']}")

    result = {
        "ready": all_meet,
        "tier1_details": details,
        "qs_issues": qs_issues,
    }

    if all_meet and discord and DISCORD_WEBHOOK_AD_ALERT:
        embed = build_ad_alert_embed(
            title="Tier 2 Promotion Ready",
            description=(
                "All Tier 1 (AG-01 to AG-05) ad groups have met CTR and QS criteria.\n"
                "Consider deploying Tier 2 (AG-06 to AG-10)."
            ),
            severity="SUCCESS",
            fields={
                "Tier 1 status": "\n".join(details),
                "QS issues": "None" if not qs_issues else "\n".join(qs_issues),
            },
        )
        send_discord_notification(DISCORD_WEBHOOK_AD_ALERT, content="", embeds=[embed])

    return result


# ===== Entry Point =====

def parse_args():
    parser = argparse.ArgumentParser(
        description="Google Ads campaign performance monitoring & low-CTR keyword auto-pause tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        metavar="N",
        help="Number of days to aggregate (default: 7)",
    )
    parser.add_argument(
        "--auto-pause",
        action="store_true",
        default=True,
        help="Auto-pause low-CTR/low-QS keywords (default: ON)",
    )
    parser.add_argument(
        "--no-auto-pause",
        dest="auto_pause",
        action="store_false",
        help="Disable auto-pause and output report only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Combined with --auto-pause: preview only without actual pause",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Output JSON only to stdout without human-readable report",
    )
    parser.add_argument(
        "--discord",
        action="store_true",
        help="Send alerts and daily summary to Discord Webhook (default: off)",
    )
    parser.add_argument(
        "--undo",
        action="store_true",
        help="Undo all PAUSEs recorded in pause_log.json (restore to ENABLED)",
    )
    parser.add_argument(
        "--undo-date",
        metavar="YYYY-MM-DD",
        help="Undo PAUSEs on specified date (e.g., --undo-date 2026-02-18)",
    )
    parser.add_argument(
        "--save-bq",
        action="store_true",
        help="Save performance data to BigQuery",
    )
    parser.add_argument(
        "--analyze-search-terms",
        action="store_true",
        help=(
            "Analyze search query report and suggest negative keyword candidates."
            " Auto-runs every Monday (also saves to BQ when combined with --save-bq)"
        ),
    )
    parser.add_argument(
        "--weekly-summary",
        action="store_true",
        help="Send weekly summary to Discord (auto-runs every Monday)",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Disable email escalation on CRITICAL (default: one email per day)",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send one test alert email and exit",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # --test-email: verify the escalation path and exit
    if args.test_email:
        ok = send_alert_email(
            subject="[AdGrants] Test email (escalation path check)",
            body=(
                "Test send from monitor_ad_performance.py --test-email.\n"
                "If this arrived, CRITICAL email escalation is working."
            ),
        )
        print("Test email: " + ("OK" if ok else "FAILED (check .env settings)"))
        return

    # Set date range
    start_date, end_date = build_date_range(args.days)
    date_str = datetime.today().strftime("%Y-%m-%d")

    if not args.json_only:
        print(f"[INFO] Period: {start_date} to {end_date} ({args.days} days)")
        print(f"[INFO] google-ads.yaml: {YAML_PATH}")

    # Initialize Google Ads client
    try:
        client = GoogleAdsClient.load_from_storage(str(YAML_PATH))
    except Exception as e:
        logger.error("Failed to initialize Google Ads client: %s", e)
        logger.error("  google-ads.yaml path: %s", YAML_PATH)
        sys.exit(1)

    # --undo / --undo-date: PAUSE rollback
    if args.undo or args.undo_date:
        undo_pauses(client, args.undo_date, dry_run=args.dry_run)
        return

    # Fetch data
    if not args.json_only:
        print("[INFO] Fetching data...")

    try:
        campaign = fetch_campaign_metrics(client, start_date, end_date)
        ad_groups = fetch_ad_group_metrics(client, start_date, end_date)
        keywords = fetch_keyword_metrics(client, start_date, end_date)
        ads = fetch_ad_metrics(client, start_date, end_date)
    except GoogleAdsException as ex:
        logger.error("Google Ads API error: %s", ex)
        for error in ex.failure.errors:
            logger.error("  - %s", error.message)
        sys.exit(1)
    except Exception as e:
        logger.error("Data fetch error: %s", e)
        sys.exit(1)

    # Check exclusion list graduation (run after keywords fetched)
    graduated = check_exclusion_graduation(keywords, discord=args.discord)
    if graduated and not args.json_only:
        print(f"[INFO] Exclusion list graduated: {', '.join(graduated)}")

    # Generate alerts
    alerts = generate_alerts(campaign, ad_groups, keywords)

    # ----------------------------------------------------------------
    # Month-to-date results (moved ahead of auto-pause on 2026-07-28)
    # The ad-group pause/resume decision needs the click budget, so fetch it first
    # and reuse the same result in the monthly pace watch below (Ads API only = free).
    # ----------------------------------------------------------------
    mtd: dict | None = None
    try:
        _today_jst = datetime.now(tz=JST)
        mtd = fetch_campaign_metrics(
            client,
            _today_jst.replace(day=1).strftime("%Y-%m-%d"),
            _today_jst.strftime("%Y-%m-%d"),
        )
    except Exception as e:  # noqa: BLE001 -- on failure treat budget as zero (strict mode)
        logger.warning("Failed to fetch month-to-date metrics: %s", e)

    headroom = compute_ctr_headroom(mtd)
    if not args.json_only:
        if headroom["measurable"]:
            mode = "long-tail enabled" if headroom["budget_clicks"] > 0 else "strict (no budget)"
            print(
                f"[INFO] Click headroom: MTD CTR {headroom['mtd_ctr']*100:.2f}%"
                f" (target {LONGTAIL_TARGET_CTR*100:.1f}%) / surplus {headroom['surplus_clicks']:+.0f} clicks"
                f" -> budget for low-CTR delivery {headroom['budget_clicks']:.0f} clicks"
                f" / {headroom['days_left']:.1f} days left / mode: {mode}"
            )
        else:
            print(
                f"[INFO] Click headroom: MTD impressions {headroom['mtd_impressions']:,}"
                f" is below the {LONGTAIL_MIN_MTD_IMP:,} needed to judge -> strict mode"
            )

    # Auto-pause processing
    paused_log: list[dict] = []
    if args.auto_pause:
        kw_strict = headroom.get("measurable") and headroom["budget_clicks"] <= 0
        pause_targets = identify_pause_targets(
            keywords, headroom=headroom, ad_groups=ad_groups,
        )
        if not args.json_only and pause_targets:
            action_label = "[DRY-RUN] " if args.dry_run else ""
            print(
                f"[INFO] {action_label}{len(pause_targets)} keywords are pause targets"
            )
        paused_log = pause_keywords(
            client,
            pause_targets,
            all_keywords=keywords,
            dry_run=args.dry_run,
            discord=args.discord,
            strict=kw_strict,
        )

        # ---- Ad-group level auto-pause (added 2026-07-28) ----
        # Pausing keywords alone leaves the low-CTR ad group itself serving.
        ag_targets, ag_deferred, ag_longtail = identify_ad_group_pause_targets(
            ad_groups, headroom=headroom, window_days=args.days,
        )
        if ag_longtail and not args.json_only:
            spent = sum(ag.get("longtail_cost", 0) for ag in ag_longtail)
            print(
                f"[INFO] Keeping {len(ag_longtail)} low-CTR ad group(s) as long tail"
                f" ({spent:.0f} of {headroom['budget_clicks']:.0f} budget clicks used)"
            )
            for ag in sorted(ag_longtail, key=lambda a: -a["impressions"])[:10]:
                print(
                    f"  + {ag['ad_group_name'][:50]}"
                    f" imp={ag['impressions']:,} CTR={format_ctr_raw(ag['ctr'])}"
                    f" cost={ag.get('longtail_cost', 0):.1f}clicks"
                )
        if ag_deferred:
            msg = (
                f"{len(ag_deferred)} ad group pause candidate(s) deferred to the next run"
                f" (first: {ag_deferred[0]['ad_group_name']})"
            )
            logger.info(msg)
            alerts.append({
                "level": "WARNING",
                "category": "ad_group_pause_deferred",
                "message": msg,
            })
        # Long-tail groups we deliberately keep must not raise a CTR alert
        if ag_longtail:
            longtail_names = {ag["ad_group_name"] for ag in ag_longtail}
            for a in alerts:
                if a.get("category") == "ad_group_ctr" and a.get("ad_group_name") in longtail_names:
                    a["level"] = "INFO"
                    a["message"] += " (intentionally running within budget)"
        if ag_targets and not args.json_only:
            action_label = "[DRY-RUN] " if args.dry_run else ""
            reason_label = (
                "over budget" if headroom["budget_clicks"] > 0
                else f"imp>={AG_PAUSE_MIN_IMP} & CTR<{CTR_AD_GROUP_MIN:.0%}"
            )
            print(
                f"[INFO] {action_label}{len(ag_targets)} ad group(s) are pause targets ({reason_label})"
            )
            for ag in ag_targets:
                print(
                    f"  - {ag['ad_group_name'][:54]}"
                    f" imp={ag['impressions']:,} CTR={format_ctr_raw(ag['ctr'])}"
                )

        ag_paused = pause_ad_groups(client, ag_targets, dry_run=args.dry_run)
        for ag in ag_paused:
            paused_log.append({
                "target_type": "AD_GROUP",
                "ad_group_id": ag["ad_group_id"],
                "ad_group_name": ag["ad_group_name"],
                "campaign_name": ag["campaign_name"],
                "reason": (
                    f"ad_group_auto_pause: 7d imp>={AG_PAUSE_MIN_IMP} & "
                    f"CTR<{CTR_AD_GROUP_MIN:.0%} (actual imp={ag['impressions']} / "
                    f"CTR={ag['ctr']*100:.2f}%)"
                ),
                "dry_run": ag.get("dry_run", False),
            })

        # ---- Take never-delivered ad groups into inventory (added 2026-08-02) ----
        # Groups created by the CI-side sync cannot register themselves, so pick
        # them up from Google Ads instead.
        try:
            adopted = adopt_never_delivered_paused_ad_groups(
                client, datetime.now(tz=JST).strftime("%Y-%m-%d")
            )
            if adopted and not args.json_only:
                print(f"[INFO] Took {len(adopted)} never-delivered ad group(s) into the long-tail inventory")
                for ag in adopted[:10]:
                    print(f"  + {ag['ad_group_name'][:60]}")
                if len(adopted) > 10:
                    print(f"  ... and {len(adopted) - 10} more")
        except Exception as e:  # noqa: BLE001 -- inventory pickup must not stop monitoring
            logger.warning("Failed to take never-delivered ad groups into inventory: %s", e)

        # ---- Long-tail resume when there is budget to spare (added 2026-07-28) ----
        longtail_spent = sum(ag.get("longtail_cost", 0) for ag in ag_longtail)
        ag_resume = identify_ad_group_resume_targets(
            ad_groups, headroom, args.days, longtail_spent, client=client,
        )
        if ag_resume and not args.json_only:
            action_label = "[DRY-RUN] " if args.dry_run else ""
            n_new = sum(1 for a in ag_resume if a.get("explore"))
            n_back = len(ag_resume) - n_new
            print(
                f"[INFO] {action_label}Budget allows delivering {len(ag_resume)} ad group(s)"
                f" (resumed {n_back} / new trials {n_new},"
                f" cost {sum(a.get('longtail_cost', 0) for a in ag_resume):.0f} clicks)"
            )
            for ag in ag_resume:
                mark = "+" if ag.get("explore") else "^"
                print(
                    f"  {mark} {ag['ad_group_name'][:50]}"
                    f" imp={ag['impressions']:,} CTR={format_ctr_raw(ag['ctr'])}"
                )
        resume_ad_groups(client, ag_resume, dry_run=args.dry_run)

    # Impression surge detection (added 2026-07-28, diff against the previous run)
    surge_alerts = detect_impression_surge(ad_groups)
    if surge_alerts:
        alerts.extend(surge_alerts)
        if not args.json_only:
            for a in surge_alerts:
                print(f"[CRITICAL] {a['message']}")

    # ----------------------------------------------------------------
    # Monthly pace watch: Ad Grants deactivation is judged on MONTHLY CTR
    # (below 5% for two consecutive months). Watch the judged unit itself
    # daily, in addition to the 7-day rolling CTR. Ads API only = free.
    # ----------------------------------------------------------------
    try:
        today_jst = datetime.now(tz=JST)
        month_start_str = today_jst.replace(day=1).strftime("%Y-%m-%d")
        today_str_jst = today_jst.strftime("%Y-%m-%d")
        # 2026-07-28: reuse what the auto-pause decision already fetched
        if mtd is None:
            mtd = fetch_campaign_metrics(client, month_start_str, today_str_jst)
        if mtd and mtd.get("impressions", 0) >= 1000 and today_jst.day >= 3:
            mtd_ctr = mtd["ctr"]
            if not args.json_only:
                print(f"[INFO] Monthly pace: {today_jst.strftime('%Y-%m')} month-to-date CTR "
                      f"{mtd_ctr * 100:.2f}% (imp {mtd['impressions']:,} / 5% required)")
            if mtd_ctr < CTR_CRITICAL_THRESHOLD:
                alerts.append({
                    "level": "CRITICAL",
                    "category": "monthly_pace",
                    "message": (
                        f"Month-to-date CTR ({month_start_str} to {today_str_jst}) is "
                        f"{mtd_ctr * 100:.2f}%, below the 5% monthly requirement. "
                        "Two consecutive months below 5% deactivates an Ad Grants account."
                    ),
                })
    except Exception as e:  # noqa: BLE001 — pace watch must not stop the daily run
        logger.warning("Monthly pace fetch failed: %s", e)

    # Generate recommendations
    suggestions = generate_keyword_suggestions(keywords)

    # Build JSON report
    report_data = build_json_report(
        start_date, end_date,
        campaign, ad_groups, keywords, ads,
        alerts, paused_log, suggestions,
    )

    if args.json_only:
        print(json.dumps(report_data, ensure_ascii=False, indent=2, default=str))
        return

    # Capture text report to stdout then save to file
    import io
    buffer = io.StringIO()
    original_stdout = sys.stdout
    sys.stdout = buffer

    print_report(
        start_date, end_date,
        campaign, ad_groups, keywords, ads,
        alerts, paused_log, suggestions,
    )

    sys.stdout = original_stdout
    human_text = buffer.getvalue()
    print(human_text, end="")

    # Save to file
    save_reports(date_str, report_data, human_text)

    # BigQuery save
    bq_save_ok = True
    bq_failure_reasons: list[str] = []
    if args.save_bq:
        if not args.json_only:
            print("[INFO] Saving data to BigQuery...")
        bq_save_ok = save_to_bigquery(
            client,
            start_date, end_date,
            alerts, paused_log,
            failure_reasons=bq_failure_reasons,
        )

    # ----------------------------------------------------------------
    # Escalate consecutive BQ save failures to CRITICAL.
    # A silent 2-week save outage (disabled service account) once went
    # unnoticed; 3 consecutive failures now ride Discord + email.
    # ----------------------------------------------------------------
    if args.save_bq:
        streak_file = REPORTS_DIR / ".bq_save_fail_streak"
        try:
            streak = int(streak_file.read_text(encoding="utf-8").strip()) if streak_file.exists() else 0
        except (ValueError, OSError):
            streak = 0
        streak = 0 if bq_save_ok else streak + 1
        try:
            streak_file.write_text(str(streak), encoding="utf-8")
        except OSError as e:
            logger.warning("Failed to write BQ failure counter: %s", e)
        if streak >= 3:
            # "N runs", not "N days": since 2026-07-28 the job runs twice a day
            # (07:00 and 19:00), so the counter and the calendar do not match.
            reason_text = (
                " / ".join(bq_failure_reasons)
                if bq_failure_reasons
                else "check the warning lines in the log"
            )
            alerts.append({
                "level": "CRITICAL",
                "category": "bq_save_outage",
                "message": (
                    f"BigQuery data save has failed {streak} runs in a row.\n"
                    f"Cause: {reason_text}\n"
                    "Analytics data is accumulating gaps (ad serving unaffected)."
                ),
            })

    # Discord notification
    if args.discord:
        # Send to ad alert channel if CRITICAL/WARNING alerts exist.
        # CRITICAL first, so anything dropped by the per-message limit is the milder one.
        critical_warnings = [a for a in alerts if a["level"] == "CRITICAL"]
        critical_warnings += [a for a in alerts if a["level"] == "WARNING"]
        if critical_warnings and DISCORD_WEBHOOK_AD_ALERT:
            alert_embeds = []
            for alert in critical_warnings:
                embed = build_ad_alert_embed(
                    title=f"[{alert['level']}] Ad Performance Alert",
                    description=alert["message"],
                    severity=alert["level"],
                    fields={
                        "Category": alert.get("category", "-"),
                        "Period": f"{start_date} to {end_date}",
                    },
                )
                alert_embeds.append(embed)
            alert_sent = send_discord_notification(
                DISCORD_WEBHOOK_AD_ALERT,
                content="",
                embeds=alert_embeds,
            )
            if not alert_sent:
                # Make a dead alert channel an alert in itself. A logger warning
                # reaches nobody, which is how alerts went missing for 30 days
                # between 2026-07-05 and 2026-08-04.
                alerts.append({
                    "level": "CRITICAL",
                    "category": "alert_channel_down",
                    "message": (
                        "Could not deliver to the Discord ad alert webhook. "
                        f"Today's {len(critical_warnings)} alerts did not reach Discord. "
                        "Check DISCORD_WEBHOOK_AD_ALERT and the response code in the log."
                    ),
                })

        # Send daily summary to daily report channel (skip if --json-only)
        if DISCORD_WEBHOOK_DAILY_REPORT:
            if campaign is not None:
                ctr_pct = f"{campaign['ctr'] * 100:.2f}%"
                clicks_str = f"{campaign['clicks']:,}"
                impressions_str = f"{campaign['impressions']:,}"
                cost_str = f"${campaign['cost_dollars']:.2f}"
            else:
                ctr_pct = "-"
                clicks_str = "-"
                impressions_str = "-"
                cost_str = "-"

            pause_count = len(paused_log)
            alert_severity = "SUCCESS"
            if any(a["level"] == "CRITICAL" for a in alerts):
                alert_severity = "CRITICAL"
            elif any(a["level"] == "WARNING" for a in alerts):
                alert_severity = "WARNING"

            summary_description = f"Campaigns: {', '.join(CAMPAIGN_NAMES)}\nPeriod: {start_date} to {end_date}"
            if not bq_save_ok:
                summary_description += "\n\n[WARNING] BQ data save failed"
            summary_embed = build_ad_alert_embed(
                title="Daily Ad Performance Summary",
                description=summary_description,
                severity=alert_severity if bq_save_ok else "WARNING",
                fields={
                    "CTR": ctr_pct,
                    "Clicks": clicks_str,
                    "Impressions": impressions_str,
                    "Cost": cost_str,
                    "PAUSE count": str(pause_count),
                },
            )
            send_discord_notification(
                DISCORD_WEBHOOK_DAILY_REPORT,
                content="",
                embeds=[summary_embed],
            )

    # ----------------------------------------------------------------
    # Email notification (the channel that actually gets read; primary since 2026-08-04)
    # Discord delivered nothing for the 30 days from 2026-07-05 to 2026-08-04.
    # Deduplicate on "date + set of firing CRITICAL categories", not on date alone:
    # date alone hides a different problem appearing later the same day.
    # ----------------------------------------------------------------
    critical_alerts = [a for a in alerts if a["level"] == "CRITICAL"]
    if critical_alerts and not args.no_email and not args.json_only:
        email_sentinel = REPORTS_DIR / ".last_critical_email"
        today_mark = datetime.now(tz=JST).strftime("%Y-%m-%d")
        fingerprint = build_critical_email_fingerprint(critical_alerts, today_mark)
        already_sent = (
            email_sentinel.exists()
            and email_sentinel.read_text(encoding="utf-8").strip() == fingerprint
        )
        if already_sent:
            logger.info("Same content already emailed; skipping")
        else:
            executed_pauses = len([p for p in paused_log if not p.get("dry_run")])
            body = build_alert_email_body(
                campaign=campaign,
                alerts=alerts,
                executed_pauses=executed_pauses,
                start_date=start_date,
                end_date=end_date,
                report_path=str(REPORTS_DIR / f"ads-performance-{end_date}.txt"),
            )
            if send_alert_email(
                subject=build_critical_email_subject(critical_alerts),
                body=body,
            ):
                email_sentinel.write_text(fingerprint, encoding="utf-8")

    # ----------------------------------------------------------------
    # Weekly summary (auto-send every Monday)
    # ----------------------------------------------------------------
    is_monday = datetime.today().weekday() == 0
    should_weekly = getattr(args, "weekly_summary", False) or is_monday

    if should_weekly and args.discord and DISCORD_WEBHOOK_DAILY_REPORT:
        if not args.json_only:
            print("\n[INFO] Sending weekly summary to Discord...")
        weekly_embed = build_weekly_summary_embed(
            campaign, keywords, alerts, paused_log, suggestions,
            start_date, end_date,
        )
        send_discord_notification(
            DISCORD_WEBHOOK_DAILY_REPORT,
            content="",
            embeds=[weekly_embed],
        )

    # Send the weekly summary by email as well.
    # In a week with no CRITICAL, warnings never reach the inbox otherwise, so this
    # is the only regular email.
    if should_weekly and not args.no_email and not args.json_only:
        weekly_sentinel = REPORTS_DIR / ".last_weekly_email"
        week_mark = datetime.now(tz=JST).strftime("%G-W%V")
        weekly_done = (
            weekly_sentinel.exists()
            and weekly_sentinel.read_text(encoding="utf-8").strip() == week_mark
        )
        if weekly_done:
            logger.info("Weekly summary already emailed this week; skipping")
        else:
            print("[INFO] Sending weekly summary by email...")
            weekly_body = build_alert_email_body(
                campaign=campaign,
                alerts=alerts,
                executed_pauses=len([p for p in paused_log if not p.get("dry_run")]),
                start_date=start_date,
                end_date=end_date,
                report_path=str(REPORTS_DIR / f"ads-performance-{end_date}.txt"),
            )
            n_critical = len([a for a in alerts if a["level"] == "CRITICAL"])
            state = f"{n_critical} critical" if n_critical else "nothing critical"
            if send_alert_email(
                subject=f"[AdGrants] Weekly summary ({start_date} to {end_date}, {state})",
                body=weekly_body,
            ):
                weekly_sentinel.write_text(week_mark, encoding="utf-8")

    # ----------------------------------------------------------------
    # Search term analysis (--analyze-search-terms or auto every Monday)
    # ----------------------------------------------------------------
    should_analyze = getattr(args, "analyze_search_terms", False) or is_monday

    if should_analyze and not args.json_only:
        if is_monday and not getattr(args, "analyze_search_terms", False):
            print("\n[INFO] Monday: running search term analysis automatically...")
        else:
            print("\n[INFO] Running search term analysis...")
        try:
            search_terms_mod.run(
                days=args.days,
                save_bq=args.save_bq,
                execute=False,     # Do not add exclusions for manual-specified runs
                discord=args.discord,
                auto_execute=True,  # Auto-exclude high-confidence candidates (score>=7)
            )
        except Exception as e:
            logger.error("Error in search term analysis: %s", e)

    # ----------------------------------------------------------------
    # Tier promotion check (auto daily)
    # ----------------------------------------------------------------
    if not args.json_only:
        tier_result = check_tier_promotion(ad_groups, keywords, discord=args.discord)
        if tier_result:
            if tier_result["ready"]:
                print("\n[INFO] Tier 2 promotion conditions met!")
            else:
                print("\n[INFO] Tier 2 promotion: not yet")
                for detail in tier_result["tier1_details"]:
                    print(f"  {detail}")
                if tier_result["qs_issues"]:
                    print("  QS issues:")
                    for qs in tier_result["qs_issues"]:
                        print(f"    {qs}")


if __name__ == "__main__":
    main()
