# adgrants-monitor

[English](#english) | [日本語](#日本語)

---

## 日本語

### 課題

Google Ad Grantsはアカウント全体のCTRが5%を下回ると停止されます。NPOの限られたリソースで手動監視を継続することは困難であり、対応が遅れれば広告掲載が止まります。

### 3つのツール

**`monitor_ad_performance.py`** — CTRコンプライアンス監視

アカウント全体および各キャンペーンのCTRを5%閾値に照らしてチェックし、品質スコアとCTRが基準を下回るキーワードを自動停止します。安全制限（1回最大3件停止・1日最大5件・最低有効キーワード数保証）を備えており、`--undo` オプションで停止をロールバックできます。

**`analyze_search_terms.py`** — 検索クエリ分析

`search_term_view` データを取得し、各クエリを除外キーワード候補としてスコアリングします。インプレッション数・CTR・コンバージョン数などの条件でスコアを算出し、閾値を超えたクエリを除外キーワードとして自動追加できます。

**`sync_articles_to_ads.py`** — コンテンツ→広告パイプライン

MDX記事のフロントマターをClaude Haikuに渡してキーワード候補を生成し、Google Ads APIでアドグループとRSAを自動作成・更新します。

### 安全設計

`ads_common/constants.py` で一元管理された安全制限により、過剰な自動操作を防止します。`pause_log.json` を利用した `--undo` / `--undo-date` でいつでもロールバック可能です。

### セキュリティ

GAQLクエリに埋め込む値はすべて `ads_common/gaql.py` のバリデーターを経由します。直接の `.format()` によるクエリ文字列生成は意図的に禁止されています。

### 本番実績

ISVDのAd Grantsアカウントで運用中です。

### 著者

横田直也 / 一般社団法人社会構想デザイン機構（ISVD）

---

## English

Python tools for monitoring and optimizing [Google Ad Grants](https://www.google.com/grants/) campaigns.

Ad Grants requires a **5% account-wide CTR** at all times or the account is suspended. These scripts automate compliance monitoring, keyword hygiene, and content-to-ads synchronization.

---

## Tools

### 1. `monitor_ad_performance.py` — CTR Compliance Monitor

Checks account-level and campaign-level CTR against the Ad Grants 5% threshold. Automatically pauses low-quality keywords and sends alerts via Discord.

**Key features:**
- Account-wide CTR check (CRITICAL if < 5%, WARNING if < 7%)
- Per-keyword pause based on Quality Score and CTR thresholds
- Safety guards: `MAX_PAUSE_PER_RUN=3`, `MAX_PAUSE_PER_DAY=5`, `MIN_ENABLED_KEYWORDS=5`
- Rollback via `--undo` / `--undo-date` using `pause_log.json`
- Weekly summary with CTR trend and ad group health
- Stores daily snapshots to BigQuery for historical analysis
- Per-campaign pause strategy: `cv` (conversion-focused) or `ctr` (access-focused)

```
python monitor_ad_performance.py                # Check + auto-pause
python monitor_ad_performance.py --weekly       # Weekly summary report
python monitor_ad_performance.py --undo         # Roll back last pause batch
python monitor_ad_performance.py --undo-date 2026-03-20  # Roll back by date
```

### 2. `analyze_search_terms.py` — Search Query Analyzer

Pulls `search_term_view` data and scores each query as a negative keyword candidate. Protects against budget waste from irrelevant traffic.

**Scoring rules (higher = higher exclusion priority):**

| Condition | Points |
|-----------|--------|
| imp ≥ 10 and CTR < 3% | +3 |
| imp ≥ 5 and clicks = 0 | +2 |
| clicks ≥ 3 and conversions = 0 | +2 |
| status = NONE (unclassified) | +1 |
| Matches suspicious pattern (job-seeking, certifications, etc.) | +2 |

**Thresholds:** `SCORE_CANDIDATE=2` (show in report), `SCORE_HIGH=5` (Discord alert + `--execute`), `SCORE_AUTO_EXECUTE=7` (auto-exclude with `--auto-execute`).

```
python analyze_search_terms.py                      # 7-day dry-run
python analyze_search_terms.py --days 14 --save-bq  # Save to BigQuery
python analyze_search_terms.py --execute            # Add high-priority as negative KWs
python analyze_search_terms.py --auto-execute --discord  # Automated weekly run
```

### 3. `sync_articles_to_ads.py` — Content-to-Ads Pipeline

Reads MDX article frontmatter from your content repository, generates keyword candidates using **Claude Haiku** (Anthropic), and creates/updates ad groups and RSAs in the article campaign.

**Flow:**

```
MDX frontmatter (title, tags, summary, body)
    └─> Claude Haiku (keyword generation, up to 8 phrases)
        └─> Google Ads API
            ├─> get_or_create campaign
            ├─> get_or_create ad group  (ENABLED if published, PAUSED if draft)
            ├─> sync_keywords           (phrase match, idempotent)
            └─> upsert_rsa              (CREATE-first, then REMOVE old)
```

```
python sync_articles_to_ads.py --article my-article-slug --dry-run
python sync_articles_to_ads.py --article my-article-slug
python sync_articles_to_ads.py --all
```

---

## Architecture

```
adgrants-monitor/
├── config.py                  # Campaign names, pause strategies, env vars
├── monitor_ad_performance.py  # CTR compliance + auto-pause
├── analyze_search_terms.py    # Negative KW scoring and exclusion
├── sync_articles_to_ads.py    # MDX article -> Ad Grants pipeline
├── ads_common/
│   ├── gaql.py                # GAQL injection protection + display width
│   └── constants.py           # Shared thresholds and limits
├── google-ads.yaml            # Google Ads API credentials (gitignored)
└── .env                       # Environment variables (gitignored)
```

### Data flow

```
Google Ads API
    ├─> monitor_ad_performance.py  ─> pause_log.json
    │                              ─> BigQuery (daily snapshots)
    │                              ─> Discord webhook
    ├─> analyze_search_terms.py    ─> BigQuery (search term scores)
    │                              ─> BigQuery (audit log)
    │                              ─> Discord webhook
    └─> sync_articles_to_ads.py    ─> Discord webhook
            ^
            └─ MDX content repo + Claude Haiku API
```

---

## Security features

### GAQL injection protection

All values embedded in GAQL queries pass through `ads_common/gaql.py`:

```python
def validate_gaql_value(value: str, field_name: str) -> str:
    if field_name == "date":
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            raise ValueError(f"Invalid date format: {value}")
    elif field_name in ("campaign_name", "ad_group"):
        if not re.match(r"^[\w\s\-\u3000-\u9FFF\uFF00-\uFFEF・]+$", value):
            raise ValueError(f"Invalid {field_name}: {value}")
    return value
```

Query builders (`build_search_term_query`, `_build_campaign_id_query`) always call this validator — direct `.format()` on query templates is intentionally prevented.

### BigQuery idempotent writes

Search term data uses a **MERGE-via-temp-table** pattern to avoid the non-atomic `DELETE → INSERT` approach:

1. Create UUID-suffixed temp table
2. Load rows via `load_table_from_json`
3. `MERGE` into the main table (UPDATE on match, INSERT on new)
4. Delete temp table in `finally` block

Temp table names are validated against `[A-Za-z0-9_]+` before use.

### Auto-pause safety guards

Defined in `ads_common/constants.py`:

| Guard | Value | Purpose |
|-------|-------|---------|
| `MAX_PAUSE_PER_RUN` | 3 | Maximum pauses per execution |
| `MAX_PAUSE_PER_DAY` | 5 | Maximum pauses per calendar day |
| `MIN_ENABLED_KEYWORDS` | 5 | Never pause below this keyword count |
| `MAX_AUTO_EXCLUDE_PER_RUN` | 5 | Maximum auto-excludes per search term run |

---

## Setup

### Prerequisites

- Python 3.12+
- Google Ads API access (developer token + OAuth credentials)
- Google Cloud service account with BigQuery write permissions (for `--save-bq`)
- Anthropic API key (for `sync_articles_to_ads.py`)

### Installation

```bash
git clone https://github.com/your-org/adgrants-monitor.git
cd adgrants-monitor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env
# Edit .env with your credentials
```

Create `google-ads.yaml` following the [Google Ads Python client library documentation](https://github.com/googleads/google-ads-python).

Edit `config.py` to set your campaign names and pause strategies:

```python
CAMPAIGN_NAMES = [
    "My Campaign - Search",
]
CAMPAIGN_PAUSE_TYPE = {
    "My Campaign": "cv",  # or "ctr" or None
}
```

Place your GCP service account key at `~/.config/gcloud/local-scripts-sa-key.json` (or update `SA_KEY_PATH` in the scripts).

---

## Usage examples

```bash
# Daily CTR check (recommended as a cron job)
python monitor_ad_performance.py

# Weekly search term analysis with BigQuery storage and Discord alert
python analyze_search_terms.py --days 7 --save-bq --discord

# Sync a new article (dry-run first)
python sync_articles_to_ads.py --article my-article-slug --dry-run
python sync_articles_to_ads.py --article my-article-slug

# Roll back yesterday's keyword pauses
python monitor_ad_performance.py --undo
```

---

## Runbook

### CTR dropped below 5% (account-wide)

Ad Grants will suspend the account if the 5%-CTR requirement is breached repeatedly. If `monitor_ad_performance.py` reports `CRITICAL`:

1. **Identify the bleed.** Run `python monitor_ad_performance.py --weekly` to see which campaign pulled the average down.
2. **Pause low-CTR keywords.** Run `python monitor_ad_performance.py` (no flags) — this respects the `MAX_PAUSE_PER_RUN=3` / `MAX_PAUSE_PER_DAY=5` safety limits.
3. **If the account-wide CTR is still under 5% after pauses**, consider pausing entire under-performing ad groups manually in the Google Ads UI. Never lower `MIN_ENABLED_KEYWORDS=5`.
4. **Confirm with the daily BigQuery snapshot** (`ad_campaign_daily`) that the trend reverses the next day.

### `--undo` fails or `pause_log.json` is corrupted

`pause_log.json` is the source of truth for the last 100 pauses. If it's missing or corrupt, `load_pause_log()` falls back to `ad_actions_log` in BigQuery automatically.

- **If `pause_log.json` is missing:** no action needed — next run rebuilds it from BQ.
- **If `pause_log.json` is malformed JSON:** delete it and run `--undo-date YYYY-MM-DD` for the affected day. The BQ fallback will supply the pause list.
- **If both are unavailable:** re-enable the affected keywords manually via the Google Ads UI. `pause_exclusion_list.json` will prevent the next monitor run from re-pausing them.

### Tests are failing in CI but pass locally

- CI uses Python 3.12; confirm your local interpreter (`python --version`).
- If a test touches `monitor_ad_performance.py`, it uses `importlib.reload()` with a patched `config.CAMPAIGN_PAUSE_TYPE`. Make sure `config.py` is unchanged in your branch.

### Rotating the service-account key

The BigQuery service-account key path is controlled by `BQ_SA_KEY_PATH` (defaults to `~/.config/gcloud/local-scripts-sa-key.json`). To rotate:

1. Drop the new key at the same path, or set `BQ_SA_KEY_PATH=/new/path` in `.env`.
2. Run `python monitor_ad_performance.py --weekly` and confirm it prints `[BQ] ad_campaign_daily: saved N rows`.

### Tier 1 → Tier 2 promotion

`monitor_ad_performance.py` checks daily whether AG-01…AG-05 (the Tier 1 ad groups) have reached ≥5% CTR and ≥3 Quality Score for 14 consecutive days. When the criteria are met the script prints `[INFO] Tier 2 promotion conditions met!` (and sends a Discord notification with `--discord`). Promotion is reported only — no automatic reclassification happens.

### Verifying changes before merging

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest                     # must be green
ruff check .               # must be green
python monitor_ad_performance.py --dry-run  # optional end-to-end smoke
```

---

## License

MIT License — see [LICENSE](LICENSE).
