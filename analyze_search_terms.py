#!/usr/bin/env python3
"""
Search query report analysis script
Fetches actual search terms from search_term_view and scores negative keyword candidates.

- Proposal only (no automatic addition unless --execute is specified)
- --auto-execute: automatically excludes high-confidence candidates with score>=7
  (intended for automated calls from the monitoring script)
- Designed for weekly Monday execution (can also be called via
  monitor_ad_performance.py --analyze-search-terms)

Usage:
  python analyze_search_terms.py                      # Last 7 days dry-run (display only)
  python analyze_search_terms.py --days 14            # Last 14 days
  python analyze_search_terms.py --save-bq            # Save to BigQuery
  python analyze_search_terms.py --execute            # Add high-priority candidates as negative KWs
  python analyze_search_terms.py --auto-execute       # Auto-exclude score>=7 (for monitoring)
  python analyze_search_terms.py --discord            # Send Discord notification
  python analyze_search_terms.py --days 7 --save-bq --discord  # Typical weekly run
"""

import argparse
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from config import CAMPAIGN_NAME, CUSTOMER_ID
from ads_common.constants import MAX_AUTO_EXCLUDE_PER_RUN
from ads_common.gaql import validate_gaql_value

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent
YAML_PATH = SCRIPT_DIR / "google-ads.yaml"
SA_KEY_PATH = Path(os.path.expanduser("~/.config/gcloud/local-scripts-sa-key.json"))

BQ_PROJECT_ID = os.environ.get("BQ_PROJECT_ID", "")
BQ_DATASET_ID = os.environ.get("BQ_DATASET_ID", "")
BQ_TABLE_ID = "ad_search_terms"
BQ_TABLE_FULL = f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.{BQ_TABLE_ID}"

load_dotenv(SCRIPT_DIR / ".env")
DISCORD_WEBHOOK_AD_ALERT = os.environ.get("DISCORD_WEBHOOK_AD_ALERT", "")

# Score thresholds
SCORE_CANDIDATE = 2    # >= this -> exclusion candidate
SCORE_HIGH = 5         # >= this -> high priority (Discord alert, --execute target)
                       # Note: During early operation (1-2 weeks after adding KWs), impressions may
                       # be too low to reach score>=5. Start with 3-4 and raise to 5 once data accumulates.
SCORE_AUTO_EXECUTE = 7  # >= this -> auto-exclude (with --auto-execute)
# MAX_AUTO_EXCLUDE_PER_RUN is imported from ads_common.constants

# ================================================================
# Suspicious patterns (EXACT match may misfire but context should be excluded)
# ================================================================
SUSPICIOUS_PATTERNS = [
    # Job-seeking intent
    "求人", "採用", "転職", "就職", "求人情報", "求人票",
    "リクルート", "doda", "indeed", "wantedly", "ハローワーク",
    # Certification/exam intent
    "資格", "検定", "基本情報", "応用情報", "itパスポート",
    "aws 資格", "ccna", "lpic",
    # School comparison intent
    "スクール 比較", "スクール 料金", "スクール 評判",
    "プログラミングスクール 比較",
    # Aptitude for non-IT jobs
    "看護師", "保育士", "介護", "営業 向き", "事務 向き", "教員",
    "医療事務", "薬剤師", "美容師", "公務員 向き", "経理 向き",
    # Learning content intent (searching for materials)
    "progate", "ドットインストール", "udemy", "atcoder",
    "プログラミング 本", "参考書", "教材",
    # Games / entertainment
    "ゲーム", "unity", "unreal",
    # Community / social
    "github", "qiita", "zenn", "stackoverflow",
]


# ================================================================
# GAQL queries
# ================================================================

# NOTE: Do NOT call .format() on this template directly.
# Always use build_search_term_query() to ensure validate_gaql_value() sanitization is applied.
_SEARCH_TERM_QUERY_TEMPLATE = """
    SELECT
        search_term_view.search_term,
        search_term_view.status,
        ad_group.name,
        metrics.impressions,
        metrics.clicks,
        metrics.ctr,
        metrics.cost_micros,
        metrics.conversions
    FROM search_term_view
    WHERE campaign.name = '{campaign_name}'
      AND segments.date BETWEEN '{start_date}' AND '{end_date}'
      AND metrics.impressions >= 1
    ORDER BY metrics.impressions DESC
"""


def build_search_term_query(campaign_name: str, start_date: str, end_date: str) -> str:
    """Build the GAQL query. Each value is sanitized via validate_gaql_value().

    This helper prevents direct .format() on the template and guarantees
    injection protection is always applied.
    """
    return _SEARCH_TERM_QUERY_TEMPLATE.format(
        campaign_name=validate_gaql_value(campaign_name, "campaign_name"),
        start_date=validate_gaql_value(start_date, "date"),
        end_date=validate_gaql_value(end_date, "date"),
    )


# NOTE: Do NOT call .format() on this template directly either.
_CAMPAIGN_ID_QUERY_TEMPLATE = """
    SELECT campaign.resource_name
    FROM campaign
    WHERE campaign.name = '{campaign_name}'
      AND campaign.status != 'REMOVED'
"""


def _build_campaign_id_query(campaign_name: str) -> str:
    """Build GAQL query to fetch a campaign ID.
    Sanitization via validate_gaql_value() is guaranteed.
    """
    return _CAMPAIGN_ID_QUERY_TEMPLATE.format(
        campaign_name=validate_gaql_value(campaign_name, "campaign_name"),
    )


# ================================================================
# Data retrieval
# ================================================================

def build_date_range(days: int) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return str(start), str(end)


def fetch_search_terms(client, start_date: str, end_date: str) -> list[dict]:
    ga_service = client.get_service("GoogleAdsService")
    query = build_search_term_query(CAMPAIGN_NAME, start_date, end_date)
    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        rows = []
        for row in response:
            rows.append({
                "search_term": row.search_term_view.search_term,
                "status": row.search_term_view.status.name,  # ADDED/EXCLUDED/NONE
                "ad_group": row.ad_group.name,
                "impressions": row.metrics.impressions,
                "clicks": row.metrics.clicks,
                "ctr": row.metrics.ctr,
                "cost_micros": row.metrics.cost_micros,
                "conversions": row.metrics.conversions,
            })
        logger.info("Search terms fetched: %d rows (%s to %s)", len(rows), start_date, end_date)
        return rows
    except GoogleAdsException as ex:
        logger.error("Google Ads API error: %s", ex.error.code().name)
        for error in ex.failure.errors:
            logger.error("  %s", error.message)
        sys.exit(1)


# ================================================================
# Scoring
# ================================================================

def score_search_term(row: dict) -> int:
    """Calculate exclusion candidate score (higher = higher exclusion priority)."""
    score = 0
    imp = row["impressions"]
    clicks = row["clicks"]
    ctr = row["ctr"]
    conversions = row["conversions"]
    term = row["search_term"].lower()

    # imp>=10 and CTR<3% -> +3 (has impressions but low CTR)
    if imp >= 10 and ctr < 0.03:
        score += 3

    # imp>=5 and CTR=0% -> +2 (zero clicks)
    if imp >= 5 and clicks == 0:
        score += 2

    # clicks>=3 and CV=0 -> +2 (clicked but no conversion)
    if clicks >= 3 and conversions == 0:
        score += 2

    # status=NONE -> +1 (unclassified; EXCLUDED is already excluded)
    if row["status"] == "NONE":
        score += 1

    # Matches suspicious pattern -> +2
    if any(pat in term for pat in SUSPICIOUS_PATTERNS):
        score += 2

    return score


def analyze_search_terms(rows: list[dict]) -> list[dict]:
    """Score all rows and return the candidate list."""
    scored = []
    for row in rows:
        score = score_search_term(row)
        row["score"] = score
        row["is_candidate"] = score >= SCORE_CANDIDATE
        row["is_high_priority"] = score >= SCORE_HIGH
        scored.append(row)
    # Sort by score descending, then impressions descending
    scored.sort(key=lambda x: (-x["score"], -x["impressions"]))
    return scored


# ================================================================
# Report display
# ================================================================

def print_report(rows: list[dict], start_date: str, end_date: str) -> None:
    candidates = [r for r in rows if r["is_candidate"]]
    high = [r for r in rows if r["is_high_priority"]]

    print("=" * 70)
    print(f"Search Query Analysis Report ({start_date} to {end_date})")
    print(f"  Campaign: {CAMPAIGN_NAME}")
    print(f"  Total queries: {len(rows)}")
    print(f"  Exclusion candidates (score>={SCORE_CANDIDATE}): {len(candidates)}")
    print(f"  High priority (score>={SCORE_HIGH}): {len(high)}")
    print("=" * 70)

    if not candidates:
        print("\nNo exclusion candidates. Account quality looks good.")
        return

    print(f"\n[HIGH PRIORITY exclusion candidates (score>={SCORE_HIGH})]: {len(high)} items")
    print(f"{'Search Term':<35} {'status':<10} {'imp':>6} {'CTR':>7} {'clicks':>7} {'CV':>5} {'score':>6}")
    print("-" * 80)
    for r in high:
        print(
            f"{r['search_term']:<35} {r['status']:<10} {r['impressions']:>6}"
            f" {r['ctr']*100:>6.1f}% {r['clicks']:>7} {r['conversions']:>5.1f} {r['score']:>6}"
        )

    print(f"\n[Exclusion candidates (score {SCORE_CANDIDATE} to {SCORE_HIGH-1})]: {len(candidates) - len(high)} items")
    print(f"{'Search Term':<35} {'status':<10} {'imp':>6} {'CTR':>7} {'clicks':>7} {'CV':>5} {'score':>6}")
    print("-" * 80)
    for r in candidates:
        if not r["is_high_priority"]:
            print(
                f"{r['search_term']:<35} {r['status']:<10} {r['impressions']:>6}"
                f" {r['ctr']*100:>6.1f}% {r['clicks']:>7} {r['conversions']:>5.1f} {r['score']:>6}"
            )

    print("\n* status=EXCLUDED is already excluded and not targeted by --execute")
    print(f"* Use --execute to add score>={SCORE_HIGH} + status=NONE candidates as negative KWs")


# ================================================================
# Keyword addition recommendations
# ================================================================

def suggest_new_keywords(rows: list[dict]) -> list[dict]:
    """Suggest keywords to add from search queries.

    Criteria for suggestions:
    - status=NONE (not yet added)
    - CTR >= 5% (high performance)
    - impressions >= 5 (minimum data volume)
    - clicks >= 2 (not accidental)
    - Does not match suspicious patterns
    """
    suggestions = []
    for row in rows:
        if row["status"] != "NONE":
            continue
        if row["ctr"] < 0.05:
            continue
        if row["impressions"] < 5:
            continue
        if row["clicks"] < 2:
            continue
        term = row["search_term"].lower()
        if any(pat in term for pat in SUSPICIOUS_PATTERNS):
            continue
        suggestions.append({
            "search_term": row["search_term"],
            "impressions": row["impressions"],
            "clicks": row["clicks"],
            "ctr": row["ctr"],
            "conversions": row["conversions"],
            "ad_group": row["ad_group"],
        })

    suggestions.sort(key=lambda x: (-x["ctr"], -x["impressions"]))
    return suggestions


def print_keyword_suggestions(suggestions: list[dict]) -> None:
    """Display recommended keywords to add."""
    if not suggestions:
        print("\n[Recommended keywords to add]: none")
        return

    print(f"\n[Recommended keywords to add (high-CTR search queries)]: {len(suggestions)} items")
    print(f"{'Search Term':<35} {'imp':>6} {'clicks':>7} {'CTR':>7} {'CV':>5} {'Ad Group'}")
    print("-" * 90)
    for s in suggestions[:10]:
        print(
            f"{s['search_term']:<35} {s['impressions']:>6}"
            f" {s['clicks']:>7} {s['ctr']*100:>6.1f}% {s['conversions']:>5.1f}"
            f"  {s['ad_group']}"
        )
    if len(suggestions) > 10:
        print(f"  ...and {len(suggestions) - 10} more")
    print("\n* These queries have high CTR but are not yet registered as keywords. Consider adding them.")


# ================================================================
# BigQuery storage
# ================================================================

BQ_SCHEMA = [
    bigquery.SchemaField("report_date", "DATE"),
    bigquery.SchemaField("search_term", "STRING"),
    bigquery.SchemaField("status", "STRING"),
    bigquery.SchemaField("ad_group", "STRING"),
    bigquery.SchemaField("impressions", "INT64"),
    bigquery.SchemaField("clicks", "INT64"),
    bigquery.SchemaField("ctr", "FLOAT64"),
    bigquery.SchemaField("cost_micros", "INT64"),
    bigquery.SchemaField("conversions", "FLOAT64"),
    bigquery.SchemaField("score", "INT64"),
    bigquery.SchemaField("is_candidate", "BOOL"),
    bigquery.SchemaField("is_high_priority", "BOOL"),
    bigquery.SchemaField("analysis_days", "INT64"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
]


BQ_AUDIT_TABLE_ID = "ad_negative_kw_audit"
BQ_AUDIT_TABLE_FULL = f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.{BQ_AUDIT_TABLE_ID}"
BQ_AUDIT_SCHEMA = [
    bigquery.SchemaField("executed_at", "TIMESTAMP"),
    bigquery.SchemaField("search_term", "STRING"),
    bigquery.SchemaField("score", "INT64"),
    bigquery.SchemaField("impressions", "INT64"),
    bigquery.SchemaField("added_count", "INT64"),
]


def _save_auto_execute_audit(auto_targets: list[dict], added_count: int) -> None:
    """Force-record --auto-execute exclusion operations to BigQuery (regardless of save_bq flag)."""
    if not SA_KEY_PATH.exists():
        logger.warning("SA key not found: %s. Skipping audit log BQ save.", SA_KEY_PATH)
        return

    try:
        bq_client = bigquery.Client.from_service_account_json(str(SA_KEY_PATH))

        # Auto-create table if it does not exist
        audit_table_ref = bq_client.dataset(BQ_DATASET_ID, project=BQ_PROJECT_ID).table(BQ_AUDIT_TABLE_ID)
        try:
            bq_client.get_table(audit_table_ref)
        except NotFound:
            audit_table = bigquery.Table(audit_table_ref, schema=BQ_AUDIT_SCHEMA)
            audit_table.time_partitioning = bigquery.TimePartitioning(field="executed_at")
            bq_client.create_table(audit_table)
            logger.info("Audit log table created: %s", BQ_AUDIT_TABLE_FULL)

        executed_at = datetime.now(timezone.utc).isoformat()
        audit_rows = [
            {
                "executed_at": executed_at,
                "search_term": t["search_term"],
                "score": t["score"],
                "impressions": t["impressions"],
                "added_count": added_count,
            }
            for t in auto_targets
        ]

        errors = bq_client.insert_rows_json(BQ_AUDIT_TABLE_FULL, audit_rows)
        if errors:
            logger.error("Audit log BQ save error: %s", errors)
        else:
            logger.info("Audit log BQ save complete: %d rows -> %s", len(audit_rows), BQ_AUDIT_TABLE_FULL)
    except Exception as e:
        logger.error("Unexpected error during audit log BQ save: %s", e)


def save_to_bigquery(rows: list[dict], start_date: str, end_date: str, days: int = 7) -> None:
    if not SA_KEY_PATH.exists():
        logger.warning("SA key not found: %s. Skipping BQ save.", SA_KEY_PATH)
        return

    bq_client = bigquery.Client.from_service_account_json(str(SA_KEY_PATH))

    # Create table if it does not exist
    table_ref = bq_client.dataset(BQ_DATASET_ID, project=BQ_PROJECT_ID).table(BQ_TABLE_ID)
    try:
        bq_client.get_table(table_ref)
    except NotFound:
        table = bigquery.Table(table_ref, schema=BQ_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(field="report_date")
        bq_client.create_table(table)
        logger.info("BQ table created: %s", BQ_TABLE_FULL)

    # report_date uses execution date (enables idempotent INSERT on the same date)
    # UTC-based for consistency with build_date_range()
    # analysis_days records the analysis window to preserve period info beyond report_date alone
    report_date = datetime.now(timezone.utc).date().isoformat()

    now = datetime.now(timezone.utc).isoformat()
    bq_rows = []
    for r in rows:
        bq_rows.append({
            "report_date": report_date,
            "search_term": r["search_term"],
            "status": r["status"],
            "ad_group": r["ad_group"],
            "impressions": r["impressions"],
            "clicks": r["clicks"],
            "ctr": r["ctr"],
            "cost_micros": int(r["cost_micros"]),
            "conversions": r["conversions"],
            "score": r["score"],
            "is_candidate": r["is_candidate"],
            "is_high_priority": r["is_high_priority"],
            "analysis_days": days,
            "created_at": now,
        })

    # Idempotent insert: MERGE upsert (avoids non-atomic DELETE -> INSERT)
    # Key: report_date + search_term + ad_group
    # INSERT into temp table -> MERGE into main table
    tmp_table_id = f"_tmp_search_terms_{report_date.replace('-', '')}_{uuid.uuid4().hex[:8]}"
    # Table name cannot be parameterized; whitelist validation guarantees safety of variable-derived names
    if not re.fullmatch(r"[A-Za-z0-9_]+", tmp_table_id):
        raise ValueError(f"Temporary table name contains invalid characters: {tmp_table_id}")
    tmp_table_full = f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.{tmp_table_id}"

    # Create temp table (delete if exists)
    bq_client.delete_table(tmp_table_full, not_found_ok=True)
    tmp_table = bigquery.Table(f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.{tmp_table_id}", schema=BQ_SCHEMA)
    bq_client.create_table(tmp_table)
    logger.info("Temp table created: %s", tmp_table_full)

    try:
        # Load into temp table via load_table_from_json (avoids streaming API)
        job_config_load = bigquery.LoadJobConfig(schema=BQ_SCHEMA)
        load_job = bq_client.load_table_from_json(bq_rows, tmp_table_full, job_config=job_config_load)
        load_job.result()  # Wait for completion
        logger.info("Loaded into temp table: %d rows", len(bq_rows))

        # MERGE: UPDATE on matching key, INSERT for new rows
        merge_query = f"""
            MERGE `{BQ_TABLE_FULL}` AS T
            USING `{tmp_table_full}` AS S
            ON T.report_date = S.report_date
               AND T.search_term = S.search_term
               AND T.ad_group = S.ad_group
            WHEN MATCHED THEN UPDATE SET
                T.status = S.status,
                T.impressions = S.impressions,
                T.clicks = S.clicks,
                T.ctr = S.ctr,
                T.cost_micros = S.cost_micros,
                T.conversions = S.conversions,
                T.score = S.score,
                T.is_candidate = S.is_candidate,
                T.is_high_priority = S.is_high_priority,
                T.analysis_days = S.analysis_days,
                T.created_at = S.created_at
            WHEN NOT MATCHED THEN INSERT (
                report_date, search_term, status, ad_group,
                impressions, clicks, ctr, cost_micros,
                conversions, score, is_candidate, is_high_priority, analysis_days, created_at
            ) VALUES (
                S.report_date, S.search_term, S.status, S.ad_group,
                S.impressions, S.clicks, S.ctr, S.cost_micros,
                S.conversions, S.score, S.is_candidate, S.is_high_priority, S.analysis_days, S.created_at
            )
        """
        merge_job = bq_client.query(merge_query)
        merge_job.result()
        # num_dml_affected_rows is INSERT + UPDATE combined (cannot be separated)
        logger.info(
            "MERGE complete: %d rows -> %s (affected=%s (INSERT+UPDATE combined))",
            len(bq_rows),
            BQ_TABLE_FULL,
            merge_job.num_dml_affected_rows,
        )

        # Delete stale rows for the same date (rows not present in this run's source)
        cleanup_query = f"""
            DELETE FROM `{BQ_TABLE_FULL}`
            WHERE report_date = @report_date
              AND STRUCT(search_term, ad_group) NOT IN (
                  SELECT STRUCT(search_term, ad_group) FROM `{tmp_table_full}`
              )
        """
        cleanup_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("report_date", "DATE", report_date),
            ]
        )
        try:
            cleanup_job = bq_client.query(cleanup_query, job_config=cleanup_config)
            cleanup_job.result()
            deleted = cleanup_job.num_dml_affected_rows or 0
        except Exception as e:
            logger.error("cleanup_query failed: %s", e)
            deleted = 0
        if deleted > 0:
            logger.info("Deleted stale rows: %d (not present in current source)", deleted)
    finally:
        # Delete temp table
        bq_client.delete_table(tmp_table_full, not_found_ok=True)
        logger.info("Temp table deleted: %s", tmp_table_full)


# ================================================================
# Discord notification
# ================================================================

def _send_discord_message(content: str) -> bool:
    """Common helper to send a message to the Discord webhook.

    Handles the 2000-character limit. Returns True on success, False on failure.
    All Discord notifications should go through this helper (DRY principle).
    """
    if not DISCORD_WEBHOOK_AD_ALERT:
        logger.warning("DISCORD_WEBHOOK_AD_ALERT is not set. Skipping notification.")
        return False
    # Discord single message limit is 2000 characters
    if len(content) > 1900:
        content = content[:1900] + "\n...(truncated)"
    try:
        resp = httpx.post(
            DISCORD_WEBHOOK_AD_ALERT,
            json={"content": content},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Discord notification sent")
        return True
    except Exception as e:
        logger.error("Discord notification failed: %s", e)
        return False


def send_discord_notification(
    rows: list[dict],
    start_date: str,
    end_date: str,
    suggestions: list[dict] | None = None,
) -> bool:
    """Send Discord notification. Returns True on success, False on failure/skip."""
    high = [r for r in rows if r["is_high_priority"] and r["status"] == "NONE"]
    candidates = [r for r in rows if r["is_candidate"]]

    if not candidates and not suggestions:
        logger.info("No exclusion candidates or suggestions. Skipping Discord notification.")
        return False

    lines = [f"**Search Query Analysis Report** ({start_date} to {end_date})\n"]
    lines.append(f"Total queries: {len(rows)} | Candidates: {len(candidates)} | High priority: {len(high)}\n")

    if high:
        lines.append("\n[ALERT] **High priority exclusion candidates** (manual negative KW addition recommended)")
        for r in high[:10]:
            lines.append(
                f"- `{r['search_term']}` imp={r['impressions']} CTR={r['ctr']*100:.1f}% score={r['score']}"
            )
        if len(high) > 10:
            lines.append(f"  ...and {len(high) - 10} more")

    if suggestions:
        lines.append(f"\n[TREND] **Recommended keywords to add** ({len(suggestions)} items)")
        for s in suggestions[:5]:
            lines.append(
                f"- `{s['search_term']}` CTR={s['ctr']*100:.1f}% clicks={s['clicks']} imp={s['impressions']}"
            )
        if len(suggestions) > 5:
            lines.append(f"  ...and {len(suggestions) - 5} more")

    lines.append("\n```")
    lines.append("python analyze_search_terms.py --execute  # Add high-priority as negative KWs")
    lines.append("```")

    return _send_discord_message("\n".join(lines))


# ================================================================
# Negative KW addition (--execute)
# ================================================================

def add_negative_keywords(client, rows: list[dict]) -> int:
    """
    Add score>=SCORE_HIGH + status=NONE candidates as negative keywords.
    Returns the number of keywords added.
    """
    targets = [r for r in rows if r["is_high_priority"] and r["status"] == "NONE"]
    if not targets:
        print("\nNo high-priority exclusion candidates (status=NONE). Nothing added.")
        return 0

    # Get campaign resource name
    ga_service = client.get_service("GoogleAdsService")
    query = _build_campaign_id_query(CAMPAIGN_NAME)
    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        campaign_resource = None
        for row in response:
            campaign_resource = row.campaign.resource_name
            break
        if not campaign_resource:
            logger.error("Campaign not found: %s", CAMPAIGN_NAME)
            return 0
    except GoogleAdsException as ex:
        logger.error("Campaign fetch failed: %s", ex.error.code().name)
        return 0

    criterion_service = client.get_service("CampaignCriterionService")
    operations = []
    for r in targets:
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = campaign_resource
        criterion.negative = True
        criterion.keyword.text = r["search_term"]
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum.EXACT
        operations.append(op)

    print(f"\nAdding the following {len(targets)} items as negative KWs (EXACT match):")
    for r in targets:
        print(f"  [{r['search_term']}] imp={r['impressions']} CTR={r['ctr']*100:.1f}% score={r['score']}")

    try:
        result = criterion_service.mutate_campaign_criteria(
            request={
                "customer_id": CUSTOMER_ID,
                "operations": operations,
                "partial_failure": True,
            }
        )
        # Collect indices of failed operations from partial_failure
        # NOTE: operations and targets are assumed to share the same index order
        failed_indices: set[int] = set()
        if result.partial_failure_error and result.partial_failure_error.code != 0:
            try:
                from google.ads.googleads import errors as googleads_errors
            except ImportError:
                logger.warning("Failed to import google-ads errors module. Skipping partial_failure detail analysis.")
                added = len(operations)  # Conservatively report all as success when details are unavailable
                logger.info("Negative KW add complete: %d (partial_failure detail analysis skipped)", added)
                return added
            for detail in result.partial_failure_error.details:
                failure = googleads_errors.GoogleAdsFailure()
                detail.Unpack(failure)
                for ads_error in failure.errors:
                    for field_ref in ads_error.location.field_path_elements:
                        if field_ref.field_name == "operations":
                            idx = field_ref.index
                            failed_indices.add(idx)
                            if idx < len(targets):
                                logger.warning("[SKIP] %s: %s", targets[idx]["search_term"], ads_error.message)
                            else:
                                logger.warning("[SKIP] index=%d (out of range): %s", idx, ads_error.message)
        added = len(operations) - len(failed_indices)
        logger.info("Negative KW add complete: %d (skipped: %d)", added, len(failed_indices))
        return added
    except GoogleAdsException as ex:
        logger.error("Negative KW add failed: %s", ex.error.code().name)
        for error in ex.failure.errors:
            logger.error("  %s", error.message)
        return 0


# ================================================================
# Main
# ================================================================

def run(days: int, save_bq: bool, execute: bool, discord: bool, auto_execute: bool = False) -> list[dict]:
    """Run analysis and return list of scored rows."""
    start_date, end_date = build_date_range(days)

    client = GoogleAdsClient.load_from_storage(str(YAML_PATH))
    rows = fetch_search_terms(client, start_date, end_date)

    if not rows:
        print("No search query data (no impressions in the measurement period).")
        return []

    scored_rows = analyze_search_terms(rows)
    print_report(scored_rows, start_date, end_date)

    # Keyword addition recommendations
    suggestions = suggest_new_keywords(scored_rows)
    print_keyword_suggestions(suggestions)

    if save_bq:
        save_to_bigquery(scored_rows, start_date, end_date, days=days)

    if discord:
        notified = send_discord_notification(scored_rows, start_date, end_date, suggestions=suggestions)
        if not notified:
            logger.error("Discord notification was not sent (not configured, no candidates, or send failure)")

    if execute:
        targets = [r for r in scored_rows if r["is_high_priority"] and r["status"] == "NONE"]
        if targets:
            print(f"\nAbout to add the following {len(targets)} items as negative KWs (EXACT match):")
            for r in targets:
                print(f"  [{r['search_term']}] imp={r['impressions']} CTR={r['ctr']*100:.1f}% score={r['score']}")
            try:
                answer = input("\nAdd negative KWs? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted. No negative KWs were added.")
                return scored_rows
            if answer != "y":
                print("Cancelled. No negative KWs were added.")
                return scored_rows
        added = add_negative_keywords(client, scored_rows)
        print(f"\n[OK] Negative KW add complete: {added} items")
    else:
        high = [r for r in scored_rows if r["is_high_priority"] and r["status"] == "NONE"]
        if high:
            print(f"\n[TIP] {len(high)} high-priority candidates found. To add as negative KWs:")
            print("   python analyze_search_terms.py --execute")

    if auto_execute and not execute:
        auto_targets = [
            r for r in scored_rows
            if r["score"] >= SCORE_AUTO_EXECUTE and r["status"] == "NONE"
        ]
        if auto_targets:
            if len(auto_targets) > MAX_AUTO_EXCLUDE_PER_RUN:
                logger.warning(
                    "Auto-exclude candidates %d exceed limit %d. Running top %d only; remaining require manual review",
                    len(auto_targets), MAX_AUTO_EXCLUDE_PER_RUN, MAX_AUTO_EXCLUDE_PER_RUN,
                )
                auto_targets = sorted(auto_targets, key=lambda r: r["score"], reverse=True)[:MAX_AUTO_EXCLUDE_PER_RUN]
            logger.info("Auto-exclude: automatically adding %d items with score>=%d", len(auto_targets), SCORE_AUTO_EXECUTE)
            # Set is_high_priority=True to pass the add_negative_keywords filter
            auto_rows = [dict(r, is_high_priority=True) for r in auto_targets]
            added = add_negative_keywords(client, auto_rows)
            print(f"\n[OK] Auto-exclude negative KW add complete: {added} (score>={SCORE_AUTO_EXECUTE})")
            if discord:
                lines = [f"**Auto-exclude negative KW add** ({added} items / score>={SCORE_AUTO_EXECUTE})"]
                for t in auto_targets[:5]:
                    lines.append(f"- `{t['search_term']}` score={t['score']} imp={t['impressions']}")
                if len(auto_targets) > 5:
                    lines.append(f"...and {len(auto_targets) - 5} more")
                if not _send_discord_message("\n".join(lines)):
                    logger.error("Discord notification after auto_execute failed")

            # Force-record --auto-execute operations to BQ (regardless of save_bq flag)
            _save_auto_execute_audit(auto_targets, added)

    return scored_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Google Ads search query analysis and negative KW candidate scoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage examples:
  python analyze_search_terms.py                        # Last 7 days, display only
  python analyze_search_terms.py --days 30              # Last 30 days
  python analyze_search_terms.py --save-bq              # Save to BigQuery
  python analyze_search_terms.py --discord              # Discord notification
  python analyze_search_terms.py --execute              # Add high-priority as negative KWs
  python analyze_search_terms.py --auto-execute         # Auto-exclude score>=7
  python analyze_search_terms.py --days 7 --save-bq --discord  # Recommended weekly run
  python analyze_search_terms.py --auto-execute --discord       # Automated monitoring run
        """,
    )
    parser.add_argument("--days", type=int, default=7, choices=range(1, 366),
                        metavar="N", help="Aggregation period in days (1-365, default 7)")
    parser.add_argument("--save-bq", action="store_true", help="Save to BigQuery")
    parser.add_argument("--execute", action="store_true", help="Add high-priority candidates as negative KWs (use with care)")
    parser.add_argument(
        "--auto-execute",
        action="store_true",
        help=(
            "Automatically add score>=7 high-confidence candidates as negative KWs. "
            "For automated calls from monitoring scripts. Use manual --execute for 5<=score<7."
        ),
    )
    parser.add_argument("--discord", action="store_true", help="Send Discord notification")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(
        days=args.days,
        save_bq=args.save_bq,
        execute=args.execute,
        discord=args.discord,
        auto_execute=args.auto_execute,
    )


if __name__ == "__main__":
    main()
