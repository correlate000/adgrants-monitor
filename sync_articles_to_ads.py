#!/usr/bin/env python3
"""
sync_articles_to_ads.py — Auto-generate Ad Grants campaigns from content articles

Reads MDX article frontmatter from your content repository,
generates keyword candidates using Claude Haiku,
and adds/updates ad groups, keywords, and RSAs in the
"ISVD記事 - 検索" campaign (configurable via config.py).

Usage:
    python sync_articles_to_ads.py --article npo-ai-adoption-challenges
    python sync_articles_to_ads.py --all
    python sync_articles_to_ads.py --article npo-ai-adoption-challenges --dry-run
    python sync_articles_to_ads.py --article npo-ai-adoption-challenges --force

Requirements:
    pip install google-ads anthropic pyyaml python-dotenv

Environment variables:
    GOOGLE_ADS_CUSTOMER_ID  — Google Ads customer ID (set in .env or environment)
    ANTHROPIC_API_KEY       — Claude Haiku API key (.env or environment)
    CONTENT_REPO_PATH       — Path to your content repository root
                              (defaults to ~/content-repo)
    DISCORD_WEBHOOK_AD_ALERT — Discord Webhook URL (optional)
"""

import argparse
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import anthropic
import yaml

# Load .env if present
try:
    from dotenv import load_dotenv
    _env = Path(__file__).parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

try:
    from google.ads.googleads.client import GoogleAdsClient
    from google.ads.googleads.errors import GoogleAdsException
    from google.protobuf import field_mask_pb2
    GOOGLE_ADS_AVAILABLE = True
except ImportError:
    GOOGLE_ADS_AVAILABLE = False
    field_mask_pb2 = None  # type: ignore[assignment]

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

# ---- Path configuration ----
SCRIPT_DIR = Path(__file__).parent
# Override with CONTENT_REPO_PATH environment variable (e.g. in GitHub Actions)
CONTENT_REPO_DIR = Path(
    os.environ.get("CONTENT_REPO_PATH", str(Path.home() / "content-repo"))
)
CONTENT_DIR = CONTENT_REPO_DIR / "src/content/ja"
GOOGLE_ADS_YAML = SCRIPT_DIR / "google-ads.yaml"

# Shared with monitor_ad_performance.py (added 2026-08-02)
# Ledger of ad groups the monitor paused for low CTR. The sync must not revive
# anything listed here (see IMPORTANT in get_or_create_ad_group).
AG_PAUSE_LOG_PATH = SCRIPT_DIR / "ag_pause_log.json"
AG_PAUSE_EXCLUSION_PATH = SCRIPT_DIR / "ag_pause_exclusion.json"

# Ad Grants target categories (exclude news)
AD_CONTENT_CATEGORIES = ["columns", "guides"]
# labs uses a subdirectory structure and is handled separately
AD_LABS_ENABLED = True
FINAL_URL_MAP = {
    "columns": "https://isvd.or.jp/columns/",
    "guides": "https://isvd.or.jp/guides/",
    "labs": "https://isvd.or.jp/labs/",
}

# ---- Ad Grants settings ----
CUSTOMER_ID = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")
CAMPAIGN_NAME = "ISVD記事 - 検索"
DAILY_BUDGET_MICROS = 329_000_000  # $329/day
MAX_CPC_MICROS = 2_000_000          # $2.00 (Ad Grants maximum)

# ---- RSA constraints ----
HEADLINE_MAX_WIDTH = 30
DESCRIPTION_MAX_WIDTH = 90
HEADLINE_MAX_COUNT = 15
DESCRIPTION_MAX_COUNT = 4

# ---- Discord ----
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_AD_ALERT", "")

# ---- Claude Haiku ----
KEYWORD_EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
KEYWORD_MAX_TOKENS = 512


# ===========================================================================
# Utilities
# ===========================================================================

def _validate_resource_name(resource_name: str) -> bool:
    """Validate Google Ads API resource name format.
    Checks for the customers/NNNNN/campaigns/NNNNN pattern."""
    return bool(re.match(r'^customers/\d+/\w+/\d+$', resource_name))


def display_width(s: str) -> int:
    """String display width (full-width=2, half-width=1)"""
    return sum(
        2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        for c in s
    )


def truncate_to_width(s: str, max_width: int) -> str:
    """Truncate string to fit within the display width limit"""
    result = ""
    width = 0
    for c in s:
        w = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if width + w > max_width:
            break
        result += c
        width += w
    return result


def send_discord(title: str, description: str, severity: str, fields: dict) -> None:
    """Send notification via the ISVD /api/alert endpoint (CRON_SECRET auth -> email).
    Formerly a Discord webhook; switched to email on 2026-07-26. Name kept for caller compatibility."""
    secret = os.environ.get("CRON_SECRET", "")
    if not secret or not HTTPX_AVAILABLE:
        return
    body = "\n".join([description, ""] + [f"{k}: {v}" for k, v in fields.items()])
    try:
        resp = httpx.post(
            "https://isvd.or.jp/api/alert",
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            json={"feature": "ads-sync", "reason": f"[{severity}] {title}\n{body}"},
            timeout=10,
        )
        if resp.status_code >= 400:
            print(f"  [WARN] email notification failed: HTTP {resp.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"  [WARN] email notification failed: {e}", file=sys.stderr)


# ===========================================================================
# Article reading
# ===========================================================================

def _find_content_file(slug: str) -> Optional[tuple[Path, str]]:
    """Search all categories for the content file matching the given slug.

    labs slugs use the "lab-slug/note-slug" format (e.g. traffic-noise/essay-regulatory-gaps).
    columns/guides slugs are flat (e.g. unemployment-structure).

    Returns:
        (file_path, category) or None
    """
    # labs: search as labs when slug contains "/"
    if "/" in slug and AD_LABS_ENABLED:
        for ext in (".mdx", ".md"):
            candidate = CONTENT_DIR / "labs" / f"{slug}{ext}"
            if candidate.exists():
                return candidate, "labs"

    # columns / guides: flat search
    for category in AD_CONTENT_CATEGORIES:
        cat_dir = CONTENT_DIR / category
        if not cat_dir.exists():
            continue
        for ext in (".mdx", ".md"):
            candidate = cat_dir / f"{slug}{ext}"
            if candidate.exists():
                return candidate, category
    return None


def read_article_frontmatter(slug: str) -> Optional[dict]:
    """Read MDX article frontmatter (gray-matter compatible)"""
    found = _find_content_file(slug)
    if not found:
        print(f"  [ERROR] Article not found: {slug}", file=sys.stderr)
        return None

    target, category = found
    content = target.read_text(encoding="utf-8")

    # Parse frontmatter manually (no python-frontmatter dependency)
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1])
                body = parts[2].strip()
                # Normalize tags to a list of strings
                tags = fm.get("tags", [])
                if isinstance(tags, str):
                    tags = [tags]
                return {
                    "slug": slug,
                    "category": category,
                    "title": fm.get("title", slug),
                    "summary": fm.get("description", "") or fm.get("summary", ""),
                    "tags": tags,
                    "author": fm.get("author", "Editorial Team"),
                    "publishedAt": str(fm.get("publishedAt", "")),
                    "status": fm.get("status", ""),  # empty string = no explicit status
                    "body_preview": body[:500],  # First 500 chars for keyword extraction
                }
            except yaml.YAMLError as e:
                print(f"  [ERROR] Frontmatter parse failed: {e}", file=sys.stderr)
                return None

    print(f"  [WARN] No frontmatter found: {slug}", file=sys.stderr)
    return {"slug": slug, "category": category, "title": slug, "summary": "", "tags": [], "status": "", "body_preview": ""}


def _get_today_jst() -> str:
    """Return today's date in YYYY-MM-DD format (JST-based)"""
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime("%Y-%m-%d")


def _read_published_at(filepath: Path) -> str:
    """Read publishedAt from MDX file frontmatter (fast version: no yaml parse)"""
    try:
        with open(filepath, encoding="utf-8") as fh:
            in_frontmatter = False
            for line in fh:
                stripped = line.rstrip()
                if stripped == "---":
                    if not in_frontmatter:
                        in_frontmatter = True
                        continue
                    else:
                        break  # end of frontmatter
                if in_frontmatter and line.startswith("publishedAt:"):
                    return line.split(":", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _read_status(filepath: Path) -> str:
    """Read status from MDX file frontmatter (fast version: no yaml parse)"""
    try:
        with open(filepath, encoding="utf-8") as fh:
            in_frontmatter = False
            for line in fh:
                stripped = line.rstrip()
                if stripped == "---":
                    if not in_frontmatter:
                        in_frontmatter = True
                        continue
                    else:
                        break  # end of frontmatter
                if in_frontmatter and line.startswith("status:"):
                    return line.split(":", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""  # Default: empty string (consistent with read_article_frontmatter)


def list_all_articles() -> list[str]:
    """List all slugs across all categories (Ad Grants targets only).
    Skips articles whose publishedAt is in the future (JST-based).
    Warns if the same slug exists in multiple categories.
    labs slugs use the "lab-slug/note-slug" format."""
    slugs = []
    slug_category: dict[str, str] = {}
    today_jst = _get_today_jst()

    # columns / guides (flat)
    for category in AD_CONTENT_CATEGORIES:
        cat_dir = CONTENT_DIR / category
        if not cat_dir.exists():
            continue
        for f in cat_dir.iterdir():
            if f.suffix in (".mdx", ".md") and not f.stem.startswith("_"):
                # publishedAt filter: skip future-dated articles
                pub = _read_published_at(f)
                if pub and pub > today_jst:
                    continue
                stem = f.stem
                if stem in slug_category:
                    print(
                        f"  [WARN] Duplicate slug: '{stem}' exists in both {slug_category[stem]} and {category}. "
                        f"Only {slug_category[stem]} will be synced.",
                        file=sys.stderr,
                    )
                    continue
                slug_category[stem] = category
                slugs.append(stem)

    # labs (subdirectory structure)
    if AD_LABS_ENABLED:
        labs_dir = CONTENT_DIR / "labs"
        if labs_dir.exists():
            for lab_dir in sorted(labs_dir.iterdir()):
                if not lab_dir.is_dir() or lab_dir.name.startswith("_"):
                    continue
                # iterdir() walks direct children only (does not recurse into _drafts/)
                for f in sorted(lab_dir.iterdir()):
                    if not f.is_file() or f.suffix not in (".mdx", ".md"):
                        continue
                    if f.stem.startswith("_"):
                        continue
                    # publishedAt filter: skip future-dated articles
                    pub = _read_published_at(f)
                    if pub and pub > today_jst:
                        continue
                    lab_slug = f"{lab_dir.name}/{f.stem}"
                    slugs.append(lab_slug)

    return slugs


# ===========================================================================
# Keyword generation (Claude Haiku)
# ===========================================================================

def extract_keywords_with_claude(article: dict) -> list[str]:
    """
    Extract keyword candidates from an article using Claude Haiku.
    Works in GitHub Actions environments without MeCab.

    Returns:
        list[str]: Up to 8 keyword candidates (intended for phrase match)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  [WARN] ANTHROPIC_API_KEY not set. Skipping keyword generation.", file=sys.stderr)
        return []

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""Generate up to 8 keyword candidates suitable for Google Search Ads (Ad Grants) from the following blog article information.

## Article Information
Title: {article['title']}
Tags: {', '.join(article['tags'])}
Summary: {article['summary']}
Body (excerpt): {article['body_preview'][:300]}

## Requirements
- Specific phrases that users would realistically search for
- 1 keyword = 1 phrase (2-5 words)
- Display width within 30 full-width character equivalents (full-width=2, half-width=1)
- Target readers interested in NPOs, social entrepreneurship, government DX, and social problem solving
- One keyword per line, no bullet symbols (-, *, #)
- Japanese only
- No preamble or header lines. Output keywords only, one per line

Keyword candidates:"""

    try:
        message = client.messages.create(
            model=KEYWORD_EXTRACTION_MODEL,
            max_tokens=KEYWORD_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Parse each line as a keyword (skip blank lines, symbol lines, header lines)
        keywords = []
        for line in raw.split("\n"):
            kw = line.strip().lstrip("-* •·#").strip()
            if not kw:
                continue
            # Exclude lines with characters invalid for Google Ads API
            if re.search(r'[#!@%^&{}|\\<>]', kw):
                continue
            # Display width check
            if display_width(kw) > HEADLINE_MAX_WIDTH:
                kw = truncate_to_width(kw, HEADLINE_MAX_WIDTH)
            if kw:
                keywords.append(kw)
        return keywords[:8]
    except Exception as e:
        print(f"  [WARN] Claude API error: {e}", file=sys.stderr)
        return []


# ===========================================================================
# RSA text generation
# ===========================================================================

# Characters that get an ad disapproved under the Google Ads
# "Punctuation & symbols" policy (added 2026-08-03).
# Headline 1 was a verbatim copy of the article title, so an arrow in the title
# went straight into the ad and it was disapproved. Removing the symbol outright
# would turn "80→85万円" into "8085万円" and change the number, so replace with a
# space instead.
_AD_TEXT_BANNED = re.compile(
    r"[←-⇿⟰-⟿⤀-⥿]"  # arrows
    r"|[★☆●○◆◇■□▲△▼▽※＊*]"                        # decorative symbols / asterisks
    r"|[!！]"                                        # exclamation marks
    r"|[…‥]"                                        # ellipses
    r"|[｡-ﾟ]"                              # half-width katakana
    r"|[\U0001F000-\U0001FAFF☀-➿]"        # emoji / dingbats
)


def sanitize_ad_text(text: str) -> str:
    """Strip characters that are not allowed in ad text (added 2026-08-03).

    Arrows carry meaning, so they are translated rather than dropped:
      between digits  "80→85万円"      -> "80から85万円"  (a range / a shift)
      otherwise       "企画→設計→制作"  -> "企画、設計、制作" (a sequence)

    Replacing them with a plain space produces "80 85万円", which reads as
    nothing at all. Repeated punctuation and dashes are collapsed too;
    repetition is itself a disapproval reason.
    """
    if not text:
        return ""
    t = text
    t = re.sub(r"(\d)\s*[←-⇿⟰-⟿⤀-⥿]\s*(\d)", r"\1から\2", t)
    t = re.sub(r"\s*[←-⇿⟰-⟿⤀-⥿]\s*", "、", t)
    t = _AD_TEXT_BANNED.sub(" ", t)
    t = re.sub(r"、\s*、+", "、", t)
    t = re.sub(r"([。、，,.？?])\1+", r"\1", t)
    t = re.sub(r"([ー－—–\-]){2,}", r"\1", t)
    t = re.sub(r"[ 　]+", " ", t)
    t = re.sub(r"\s*、\s*", "、", t)
    return t.strip().strip("、")


# Where a headline may be cut (added 2026-08-03)
# Cutting purely on width chopped titles mid-word and left brackets open
# ("後期高齢者保険料上限「80から85"). Cut only at a meaning boundary.
_HEADLINE_CUT_AFTER = "：:、。，,！!？?）)」』】〉》"    # may cut right after these
_HEADLINE_CUT_BEFORE = "：:—–〜~（(「『【〈《"           # may cut right before these
# Below this width the fragment is not worth using (8 full-width chars).
# A headline of just "文献マップ" says nothing about the article; the ~8
# keyword-derived headlines cover it instead.
HEADLINE_MIN_WIDTH = 16

_BRACKET_PAIRS = (("「", "」"), ("『", "』"), ("（", "）"), ("(", ")"),
                  ("【", "】"), ("〈", "〉"), ("《", "》"), ("[", "]"))


def _brackets_balanced(s: str) -> bool:
    """Whether every opened bracket is closed. Unbalanced headlines look broken."""
    return all(s.count(o) == s.count(c) for o, c in _BRACKET_PAIRS)


# A headline ending on a particle is cut mid-sentence (added 2026-08-03)
#   "消滅可能性744自治体の共通点を"  <- ends on を, promises a continuation
#   "クレーム1本で2,100食が消えた日" <- ends on a noun, reads on its own
# Dropping every title without punctuation loses the second kind too, so the
# ending is inspected instead.
_TRAILING_PARTICLES = ("を", "が", "は", "に", "へ", "の", "と", "や", "で", "から", "まで", "より")

# Ending on an interrogative demands a clause that is no longer there.
#   "苦情空白という現象 — なぜ"  <- the clause after なぜ is gone
# The rule that allows cutting before an opening bracket makes this shape easy
# to produce, so it is screened separately.
_TRAILING_INTERROGATIVES = (
    "なぜ", "なぜか", "どう", "どうして", "いかに", "どこ", "いつ", "だれ", "誰",
    "何", "なに", "どの", "どんな", "いくら", "どれ", "この", "その", "あの",
)

# Ending on の is fine when the word is a nominaliser -- the phrase is complete.
#   "「無償化」されないもの"          <- complete (もの is a noun)
#   "介護人材危機の構造 — 2040年の"   <- incomplete (の is a case particle)
_TRAILING_NOMINALIZERS = ("もの", "こと", "ところ", "とき", "ほう", "わけ", "ため", "はず", "とは")

# Characters that must not end a headline. Missing ・ and ｜ let
# "愛知・静岡・" and "全国165｜" reach production.
_HEADLINE_TRAILING_BAD = "：:、。，,—–〜~・･｜|／/＆& 　"


def _ends_incomplete(h: str) -> bool:
    """Whether the text ends on a particle or interrogative and reads cut short."""
    if any(h.endswith(n) for n in _TRAILING_NOMINALIZERS):
        return False
    if any(h.endswith(q) for q in _TRAILING_INTERROGATIVES):
        return True
    return any(h.endswith(p) for p in _TRAILING_PARTICLES)


def _is_kanji(c: str) -> bool:
    return "一" <= c <= "鿿" or "㐀" <= c <= "䶿"


def _is_katakana(c: str) -> bool:
    return "゠" <= c <= "ヿ" or "ㇰ" <= c <= "ㇿ"


def _is_readable_ending(cand: str, rest: str) -> bool:
    """Whether a width-truncated candidate reads on its own.

    cand: the truncated text / rest: what follows it in the source.

    Japanese has no spaces between words, so word boundaries are approximated
    from script transitions and particles. It is not exact, but it catches the
    obvious mid-word cuts ("〜の共通点を", "参加型デザインの系(譜)").
    """
    if not cand or display_width(cand) < HEADLINE_MIN_WIDTH:
        return False
    if not _brackets_balanced(cand):
        return False
    if cand[-1] in _HEADLINE_TRAILING_BAD:
        return False
    if _ends_incomplete(cand):
        return False
    if not rest:
        return True
    last, nxt = cand[-1], rest[0]
    if last.isdigit() and (nxt.isdigit() or nxt in ",，."):
        return False
    if last.isdigit() and _is_kanji(nxt):  # a counter/unit follows the number
        return False
    if last.isascii() and last.isalpha() and nxt.isalpha():
        return False
    if _is_kanji(last) and _is_kanji(nxt):        # mid compound noun
        return False
    if _is_katakana(last) and _is_katakana(nxt):  # mid loanword
        return False
    return True


def headline_from_title(title: str, max_width: int = HEADLINE_MAX_WIDTH) -> str:
    """Build a headline from the article title without cutting mid-word.

    Returns the title as-is when it fits. Otherwise takes the longest prefix
    that ends at a meaning boundary and still fits. When no boundary works, or
    the fragment would be too short, returns "" and the caller drops the
    title-derived headline (keyword-derived headlines carry the ad).

    The Google Ads headline limit of 30 counts full-width characters as 2, so
    Japanese titles get roughly 15 characters -- most titles simply do not fit.
    """
    s = sanitize_ad_text(title)
    if not s:
        return ""
    if display_width(s) <= max_width:
        return s

    best = ""
    fallback = ""   # used when the title has no punctuation to cut at
    width = 0
    for i, c in enumerate(s):
        w = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if width + w > max_width:
            break
        width += w
        cand = s[:i + 1]
        if _is_readable_ending(cand, s[i + 1:]):
            fallback = cand
        # Titles write the dash as "現象 — なぜ", so skip whitespace when looking
        # ahead; otherwise the dash is never reached and never acts as a boundary.
        nxt = s[i + 1:].lstrip(" 　")
        is_cut = c in _HEADLINE_CUT_AFTER or (nxt and nxt[0] in _HEADLINE_CUT_BEFORE)
        if not is_cut:
            continue
        trimmed = cand.rstrip(_HEADLINE_TRAILING_BAD)
        if _brackets_balanced(trimmed) and not _ends_incomplete(trimmed):
            best = trimmed

    chosen = best if display_width(best) >= HEADLINE_MIN_WIDTH else fallback
    return chosen if display_width(chosen) >= HEADLINE_MIN_WIDTH else ""


def build_rsa_headlines(article: dict, keywords: list[str]) -> list[str]:
    """
    Build RSA headlines from article title, tags, and keywords.
    Truncates to HEADLINE_MAX_WIDTH (30 full-width char equivalents).
    Symbols are stripped by sanitize_ad_text() *before* truncation -- doing it
    the other way round leaves them in.
    """
    headlines = []

    # Headline 1: article title (intended for pin position 1)
    # Cut at a meaning boundary, not on raw width; drop it if nothing fits.
    title_headline = headline_from_title(article["title"])
    if title_headline:
        headlines.append(title_headline)

    # Headline 2+: keywords used directly
    for kw in keywords:
        if len(headlines) >= HEADLINE_MAX_COUNT:
            break
        h = truncate_to_width(sanitize_ad_text(kw), HEADLINE_MAX_WIDTH)
        if h and h not in headlines:
            headlines.append(h)

    # Fill with tag-based headlines (supports both English ID and Japanese label tags)
    tag_labels = {
        # English IDs (guides etc.)
        "labor": "雇用・労働問題",
        "economy": "経済データ解説",
        "education": "教育統計",
        "population": "人口・少子化",
        "health": "医療・健康",
        "welfare": "福祉・NPO支援",
        "digital": "デジタル・AI活用",
        "welfare-npo": "NPO支援・運営",
        "digital-ai": "AI導入事例",
        "npo": "NPO運営ガイド",
        "social-design": "社会構想デザイン",
        "technology": "テクノロジー活用",
        "ai-ethics": "AI倫理を考える",
        "sustainability": "サステナビリティ",
        "grants": "助成金活用",
        "evaluation": "評価・アウトカム",
        # Japanese labels (columns etc.)
        "労働": "雇用・労働問題",
        "経済格差": "格差問題を読む",
        "テクノロジー": "テクノロジー活用",
        "AI倫理": "AI倫理を考える",
        "サステナビリティ": "サステナビリティ",
        "企業責任": "企業の社会的責任",
        "法制度": "法制度と社会",
        "デジタル政策": "デジタル政策解説",
        "社会課題": "社会課題データ分析",
        # labs tags
        "traffic-noise": "交通騒音対策",
        "sensory-sensitivity": "感覚過敏と環境",
        "misophonia": "ミソフォニア対策",
        "environmental-justice": "環境正義を考える",
        "noise-regulation": "騒音規制の課題",
    }
    for tag in article.get("tags", []):
        if len(headlines) >= HEADLINE_MAX_COUNT:
            break
        label = tag_labels.get(tag, "")
        if label and label not in headlines:
            headlines.append(label)

    # Ensure at least 3 headlines
    fallbacks = ["社会課題 データで見る", "統計で解説", "ISVD 調査レポート"]
    for fb in fallbacks:
        if len(headlines) >= 3:
            break
        if fb not in headlines:
            headlines.append(fb)

    return headlines[:HEADLINE_MAX_COUNT]


# Landing page reachability gate (added 2026-08-03)
# Pushing an article fires this sync immediately, but the site deploy finishes
# 2-5 minutes later. Creating the ad in that window makes Google's crawler hit a
# 404 and the ad is disapproved for "Destination not working" -- 6 fresh articles
# were disapproved that way on 2026-08-03. Wait for the deploy before advertising.
LANDING_PAGE_MAX_WAIT_SEC = 360   # covers a typical 2-5 minute deploy
LANDING_PAGE_POLL_SEC = 20


def wait_for_landing_page(url: str, max_wait: int = LANDING_PAGE_MAX_WAIT_SEC) -> tuple[bool, int]:
    """Wait until the landing page answers 2xx. Returns (reachable, last status).

    Callers must skip ad creation when this returns False -- shipping an ad that
    points at a 404 gets it disapproved. Without httpx the check is skipped and
    True is returned, preserving the previous behaviour.
    """
    if not HTTPX_AVAILABLE:
        return True, 0
    import time
    deadline = time.monotonic() + max_wait
    last = 0
    attempt = 0
    while True:
        attempt += 1
        try:
            r = httpx.get(url, follow_redirects=True, timeout=20.0)
            last = r.status_code
            if 200 <= last < 300:
                if attempt > 1:
                    print(f"  [OK] Landing page is live (attempt {attempt}): {url}")
                return True, last
        except Exception as e:  # noqa: BLE001 -- a failed probe must not stop the sync
            last = 0
            print(f"  [WARN] Landing page probe failed (attempt {attempt}): {e}", file=sys.stderr)
        if time.monotonic() + LANDING_PAGE_POLL_SEC >= deadline:
            return False, last
        print(f"  ... Landing page not live yet (HTTP {last});"
              f" retrying in {LANDING_PAGE_POLL_SEC}s: {url}")
        time.sleep(LANDING_PAGE_POLL_SEC)


def build_rsa_descriptions(article: dict) -> list[str]:
    """Build RSA description texts (up to 4)"""
    descriptions = []

    # Description 1: generated from summary (symbols stripped before truncation)
    summary = article.get("summary", "")
    if summary:
        desc = truncate_to_width(sanitize_ad_text(summary), DESCRIPTION_MAX_WIDTH)
        if desc:
            descriptions.append(desc)

    # Description 2: fixed brand text
    descriptions.append(
        truncate_to_width("ISVDが提供する社会課題の統計データと解説記事。NPO・行政・社会起業家向け。", DESCRIPTION_MAX_WIDTH)
    )

    # Description 3: call to action
    descriptions.append(
        truncate_to_width("データに基づく社会構想を。ISVD統計メディアで根拠ある議論を始めよう。", DESCRIPTION_MAX_WIDTH)
    )

    return descriptions[:DESCRIPTION_MAX_COUNT]


# ===========================================================================
# Google Ads API operations
# ===========================================================================

def get_or_create_campaign_resource(client, ga_service) -> Optional[tuple[str, str]]:
    """Get the ISVD article campaign resource name and status (or create it if absent).

    Returns:
        (resource_name, status_name) or None
    """
    # CAMPAIGN_NAME is a module constant (not external input) but follows the
    # "always validate before GAQL embedding" rule from google-ads-patterns.md
    if not re.match(r'^[\w\s\-ぁ-んァ-ン一-龥：:・\(\)（）]+$', CAMPAIGN_NAME):
        raise ValueError(f"Invalid campaign name format: {CAMPAIGN_NAME!r}")
    query = f"""
        SELECT campaign.resource_name, campaign.name, campaign.status
        FROM campaign
        WHERE campaign.name = '{CAMPAIGN_NAME}'
          AND campaign.status != 'REMOVED'
    """
    response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
    rows = list(response)

    if rows:
        resource_name = rows[0].campaign.resource_name
        status_name = rows[0].campaign.status.name

        # Auto-enable campaign if it is PAUSED
        # (delivery is controlled at ad group level; campaign should stay ENABLED)
        if status_name == "PAUSED":
            # Check ad group counts before making changes
            ag_rows = list(ga_service.search(customer_id=CUSTOMER_ID, query=f"""
                SELECT ad_group.status
                FROM ad_group
                WHERE campaign.resource_name = '{resource_name}'
                  AND ad_group.status != 'REMOVED'
            """))
            enabled_count = sum(1 for r in ag_rows if r.ad_group.status.name == "ENABLED")
            paused_count = sum(1 for r in ag_rows if r.ad_group.status.name == "PAUSED")
            print(f"  [INFO] Ad groups: ENABLED={enabled_count} / PAUSED={paused_count}")
            send_discord(
                title=f"[WARN] Campaign \"{CAMPAIGN_NAME}\" changed to ENABLED",
                description="PAUSED campaign was automatically changed to ENABLED. Delivery is controlled at the ad group level.",
                severity="WARNING",
                fields={"ENABLED ad groups": str(enabled_count), "PAUSED ad groups": str(paused_count)},
            )
            print(f"  -> Changing campaign \"{CAMPAIGN_NAME}\" from PAUSED to ENABLED...")
            campaign_service = client.get_service("CampaignService")
            campaign_op = client.get_type("CampaignOperation")
            campaign_op.update.resource_name = resource_name
            campaign_op.update.status = client.enums.CampaignStatusEnum.ENABLED
            if field_mask_pb2 is None:
                raise RuntimeError("google.protobuf is not available. Check your google-ads package installation.")
            campaign_op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
            campaign_service.mutate_campaigns(
                customer_id=CUSTOMER_ID, operations=[campaign_op]
            )
            print(f"  [OK] Campaign changed to ENABLED")
            status_name = "ENABLED"

        return resource_name, status_name

    # --- Create new campaign ---
    print(f"  -> Creating new campaign \"{CAMPAIGN_NAME}\" (ENABLED)...")

    # Budget (reuse existing budget with the same name for idempotency)
    budget_name = f"{CAMPAIGN_NAME} 予算"
    # CAMPAIGN_NAME is already validated; also explicitly check derived string before GAQL embedding
    if "'" in budget_name:
        raise ValueError(f"Budget name contains single quote: {budget_name!r}")
    budget_service = client.get_service("CampaignBudgetService")
    existing_budget_rows = list(ga_service.search(
        customer_id=CUSTOMER_ID,
        query=f"""
            SELECT campaign_budget.resource_name
            FROM campaign_budget
            WHERE campaign_budget.name = '{budget_name}'
              AND campaign_budget.status != 'REMOVED'
        """
    ))
    if existing_budget_rows:
        budget_res = existing_budget_rows[0].campaign_budget.resource_name
        print(f"  [INFO] Reusing existing budget: {budget_res}")
    else:
        budget_op = client.get_type("CampaignBudgetOperation")
        b = budget_op.create
        b.name = budget_name
        b.amount_micros = DAILY_BUDGET_MICROS
        b.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget_res = budget_service.mutate_campaign_budgets(
            customer_id=CUSTOMER_ID, operations=[budget_op]
        ).results[0].resource_name

    # Campaign (if creation fails after budget creation, the budget will be orphaned,
    # but it will be recovered on the next run via reuse)
    campaign_service = client.get_service("CampaignService")
    campaign_op = client.get_type("CampaignOperation")
    c = campaign_op.create
    c.name = CAMPAIGN_NAME
    c.campaign_budget = budget_res
    c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    c.status = client.enums.CampaignStatusEnum.ENABLED  # Delivery controlled at ad group level
    c.manual_cpc.enhanced_cpc_enabled = False
    c.network_settings.target_google_search = True
    c.network_settings.target_search_network = False
    c.network_settings.target_content_network = False
    campaign_resource = campaign_service.mutate_campaigns(
        customer_id=CUSTOMER_ID, operations=[campaign_op]
    ).results[0].resource_name
    print(f"  [OK] Campaign created: {campaign_resource}")
    return campaign_resource, "ENABLED"


def _infer_article_status(article: dict) -> str:
    """Infer the effective status of an article.
    - Uses explicit status field if present
    - If status is not set and publishedAt is today or earlier, infers 'published'
    - Otherwise returns 'draft'"""
    explicit = article.get("status", "")
    if explicit:
        return explicit
    # For unset status: infer from publishedAt
    # Compare first 10 chars to handle ISO 8601 datetime (e.g. 2025-03-25T10:00:00+09:00)
    pub = article.get("publishedAt", "")
    if pub and str(pub)[:10] <= _get_today_jst():
        return "published"
    return "draft"


def _resolve_ad_group_status(client, article_status: str):
    """Determine Ad Group status from article status.
    published -> ENABLED (serving immediately), otherwise -> PAUSED"""
    if article_status == "published":
        return client.enums.AdGroupStatusEnum.ENABLED
    return client.enums.AdGroupStatusEnum.PAUSED


def load_monitor_paused_names() -> set[str]:
    """Names of ad groups the monitor deliberately paused (added 2026-08-02).

    ag_pause_log.json      -- auto/bulk pause ledger (the long-tail backlog)
    ag_pause_exclusion.json -- groups barred from automatic resume after a surge

    An unreadable ledger yields an empty set; the caller then cannot protect
    anything, so treat that as "do not resume".
    """
    names: set[str] = set()
    for path, key in ((AG_PAUSE_LOG_PATH, "entries"), (AG_PAUSE_EXCLUSION_PATH, "entries")):
        try:
            if not path.exists():
                continue
            import json as _json
            with open(path, encoding="utf-8") as f:
                for e in _json.load(f).get(key, []):
                    name = e.get("ad_group_name")
                    if name:
                        names.add(name)
        except Exception as e:  # noqa: BLE001 -- a broken ledger must not stop the sync
            print(f"  [WARN] Failed to read pause ledger {path.name}: {e}", file=sys.stderr)
    return names


def register_new_ad_group_in_longtail_pool(
    ad_group_id: int, group_name: str, campaign_name: str, resource_name: str
) -> None:
    """Register a freshly created ad group as long-tail inventory (added 2026-08-02).

    New groups are created PAUSED. The monitor eases them into delivery a few at a
    time, within the month's CTR headroom. Zero history (impressions_7d = 0) is the
    marker for "never tried yet".
    """
    import json as _json
    try:
        if AG_PAUSE_LOG_PATH.exists():
            with open(AG_PAUSE_LOG_PATH, encoding="utf-8") as f:
                entries = _json.load(f).get("entries", [])
        else:
            entries = []
        if any(e.get("resource_name") == resource_name for e in entries):
            return
        entries.append({
            "paused_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
            "source": "new_article_sync",
            "ad_group_id": ad_group_id,
            "ad_group_name": group_name,
            "campaign_name": campaign_name,
            "resource_name": resource_name,
            "impressions_7d": 0,
            "clicks_7d": 0,
            "ctr_7d": 0.0,
            "previous_status": "NEW",
        })
        with open(AG_PAUSE_LOG_PATH, "w", encoding="utf-8") as f:
            _json.dump({"entries": entries}, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001 -- a failed ledger write must not undo the sync
        print(f"  [WARN] Failed to register in long-tail pool: {e}", file=sys.stderr)


def get_or_create_ad_group(client, ga_service, campaign_resource: str, slug: str,
                           article_status: str = "draft",
                           monitor_paused: Optional[set[str]] = None,
                           campaign_name: str = "") -> str:
    """Get/create the ad group corresponding to the article slug.

    IMPORTANT (design change, 2026-08-02):
      1. New groups are always created PAUSED. The monitor's long-tail operation
         eases them into delivery within the month's CTR headroom. Enabling a large
         batch at once reproduces the 2026-06 collapse (weekly 8.81% -> 4.50%).
      2. **The sync never enables an ad group.** Only monitor_ad_performance.py
         decides when delivery starts. Previously a published article
         unconditionally forced PAUSED -> ENABLED, so the sync kept reviving
         whatever the monitor had stopped -- a tug of war found on 2026-08-02.

         The first attempt guarded this with the pause ledger, but
         ag_pause_log.json is not in version control and does not exist in a
         GitHub Actions checkout, so the guard was a no-op in CI: run
         30757373890 flipped 4 groups back to ENABLED. Dropping the transition
         removes the dependency on the ledger being present.

         The reverse direction (ENABLED -> PAUSED, when an article goes back to
         draft) is the safe way round and is still allowed.
    """
    if not _validate_resource_name(campaign_resource):
        raise ValueError(f"Invalid campaign resource name: {campaign_resource}")
    # Slug validation (GAQL injection prevention)
    if not re.match(r'^[a-z0-9-]+(/[a-z0-9-]+)?$', slug):
        raise ValueError(f"Invalid slug format (get_or_create_ad_group): {slug}")
    group_name = f"記事: {slug}"
    # Slug is already validated; also explicitly check derived string before GAQL embedding
    if "'" in group_name:
        raise ValueError(f"Ad group name contains single quote: {group_name!r}")
    desired_status_name = "ENABLED" if article_status == "published" else "PAUSED"

    query = f"""
        SELECT ad_group.id, ad_group.resource_name, ad_group.name, ad_group.status
        FROM ad_group
        WHERE campaign.resource_name = '{campaign_resource}'
          AND ad_group.name = '{group_name}'
          AND ad_group.status != 'REMOVED'
    """
    response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
    rows = list(response)
    if rows:
        resource_name = rows[0].ad_group.resource_name
        current_status = rows[0].ad_group.status.name

        # The sync never enables. Starting delivery is the monitor's call alone.
        if current_status == "PAUSED" and desired_status_name == "ENABLED":
            note = " (also listed in the monitor's pause ledger)" if group_name in (monitor_paused or set()) else ""
            print(f"  -> Ad group \"{group_name}\" is paused; keeping it PAUSED{note}."
                  " The monitor starts delivery once there is CTR headroom")
            return resource_name

        # Update if status has changed (only the safe ENABLED -> PAUSED reaches here)
        if current_status != desired_status_name:
            print(f"  -> Changing ad group \"{group_name}\" from {current_status} to {desired_status_name}...")
            ag_service = client.get_service("AdGroupService")
            op = client.get_type("AdGroupOperation")
            op.update.resource_name = resource_name
            op.update.status = _resolve_ad_group_status(client, article_status)
            if field_mask_pb2 is None:
                raise RuntimeError("google.protobuf is not available. Check your google-ads package installation.")
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
            ag_service.mutate_ad_groups(customer_id=CUSTOMER_ID, operations=[op])
            print(f"  [OK] Ad group changed to {desired_status_name}")

        return resource_name

    # Create new ad group -- always PAUSED, even for published articles.
    # Delivery is started by the monitor's long-tail operation.
    ag_service = client.get_service("AdGroupService")
    op = client.get_type("AdGroupOperation")
    ag = op.create
    ag.name = group_name
    ag.campaign = campaign_resource
    ag.status = client.enums.AdGroupStatusEnum.PAUSED
    ag.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    ag.cpc_bid_micros = MAX_CPC_MICROS
    print(f"  -> Creating ad group \"{group_name}\" (PAUSED;"
          " the long-tail operation decides when to start delivery)")
    result = ag_service.mutate_ad_groups(customer_id=CUSTOMER_ID, operations=[op])
    new_resource_name = result.results[0].resource_name

    # Register as long-tail inventory. Zero history marks it as never tried.
    if article_status == "published":
        try:
            new_id = int(new_resource_name.rsplit("/", 1)[-1])
        except ValueError:
            new_id = 0
        register_new_ad_group_in_longtail_pool(
            new_id, group_name, campaign_name, new_resource_name
        )
    return new_resource_name


def sync_keywords(client, ga_service, ad_group_resource: str, keywords: list[str]) -> tuple[int, list[str]]:
    """
    Add keywords one at a time with phrase match.
    Keywords that violate policy are skipped and logged.
    Only adds keywords that do not already exist (idempotent).

    Returns:
        (number_added, list_of_rejected_keywords)
    """
    if not _validate_resource_name(ad_group_resource):
        raise ValueError(f"Invalid ad_group resource name: {ad_group_resource}")
    # Fetch existing keywords
    query = f"""
        SELECT ad_group_criterion.keyword.text
        FROM ad_group_criterion
        WHERE ad_group.resource_name = '{ad_group_resource}'
          AND ad_group_criterion.type = 'KEYWORD'
          AND ad_group_criterion.status != 'REMOVED'
    """
    response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
    existing = {row.ad_group_criterion.keyword.text.lower() for row in response}

    new_keywords = [kw for kw in keywords if kw.lower() not in existing]
    if not new_keywords:
        return 0, []

    criterion_service = client.get_service("AdGroupCriterionService")
    added = 0
    rejected = []
    for kw in new_keywords:
        op = client.get_type("AdGroupCriterionOperation")
        c = op.create
        c.ad_group = ad_group_resource
        c.keyword.text = kw
        c.keyword.match_type = client.enums.KeywordMatchTypeEnum.PHRASE
        c.cpc_bid_micros = 2_000_000  # Ad Grants maximum $2.00
        try:
            criterion_service.mutate_ad_group_criteria(
                customer_id=CUSTOMER_ID, operations=[op]
            )
            added += 1
        except GoogleAdsException as kw_ex:
            # Log error type for easier troubleshooting
            err_detail = ""
            try:
                err_detail = f" [{kw_ex.failure.errors[0].error_code}] {kw_ex.failure.errors[0].message}"
            except Exception:
                pass
            print(f"    [WARN] KW rejected ({err_detail.strip() or 'GoogleAdsException'}): {kw}", file=sys.stderr)
            rejected.append(kw)
    return added, rejected


def upsert_rsa(client, ga_service, ad_group_resource: str,
               headlines: list[str], descriptions: list[str], final_url: str) -> str:
    """
    Create or update an RSA.
    If an RSA exists, the order is CREATE then REMOVE (REMOVE only after CREATE succeeds).
    This ensures the existing RSA is preserved if CREATE fails (e.g. due to policy violation).

    Returns:
        str: "created" | "updated"
    """
    if not _validate_resource_name(ad_group_resource):
        raise ValueError(f"Invalid ad_group resource name: {ad_group_resource}")
    # Check for existing RSA
    query = f"""
        SELECT ad_group_ad.resource_name, ad_group_ad.ad.id
        FROM ad_group_ad
        WHERE ad_group.resource_name = '{ad_group_resource}'
          AND ad_group_ad.status != 'REMOVED'
          AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
        ORDER BY ad_group_ad.ad.id DESC
    """
    response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
    existing_rows = list(response)

    ad_service = client.get_service("AdGroupAdService")

    # CREATE first (verify CREATE success before REMOVE)
    op = client.get_type("AdGroupAdOperation")
    aga = op.create
    aga.ad_group = ad_group_resource
    aga.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad = aga.ad
    ad.final_urls.append(final_url)

    pin_positions = [
        client.enums.ServedAssetFieldTypeEnum.HEADLINE_1,
        client.enums.ServedAssetFieldTypeEnum.HEADLINE_2,
        client.enums.ServedAssetFieldTypeEnum.HEADLINE_3,
    ]
    for i, hl in enumerate(headlines[:HEADLINE_MAX_COUNT]):
        asset = client.get_type("AdTextAsset")
        asset.text = hl
        if i < len(pin_positions):
            asset.pinned_field = pin_positions[i]
        ad.responsive_search_ad.headlines.append(asset)

    for desc in descriptions[:DESCRIPTION_MAX_COUNT]:
        asset = client.get_type("AdTextAsset")
        asset.text = desc
        ad.responsive_search_ad.descriptions.append(asset)

    # If CREATE fails, exception propagates and existing RSA is preserved
    ad_service.mutate_ad_group_ads(customer_id=CUSTOMER_ID, operations=[op])

    # REMOVE old RSA after successful CREATE
    # If REMOVE fails, the ad group still functions with the new RSA, so only WARNING (not CRITICAL)
    if existing_rows:
        try:
            remove_op = client.get_type("AdGroupAdOperation")
            remove_op.remove = existing_rows[0].ad_group_ad.resource_name
            ad_service.mutate_ad_group_ads(customer_id=CUSTOMER_ID, operations=[remove_op])
        except GoogleAdsException as remove_ex:
            print(
                f"  [WARN] Failed to REMOVE old RSA (new RSA is active): {remove_ex.failure.errors[0].message}",
                file=sys.stderr,
            )
        return "updated"

    return "created"


# ===========================================================================
# Main processing
# ===========================================================================

def sync_article(slug: str, dry_run: bool, force: bool,
                  ads_client=None, ga_service=None,
                  campaign_info: Optional[tuple[str, str]] = None,
                  monitor_paused: Optional[set[str]] = None) -> dict:
    """
    Sync one article.

    Returns:
        dict: {"slug": slug, "success": bool, "message": str, ...}
    """
    print(f"\n[{slug}]")

    # 0. Slug validation (GAQL injection prevention)
    # Allow "/" for labs slugs in "lab-slug/note-slug" format
    if not re.match(r'^[a-z0-9-]+(/[a-z0-9-]+)?$', slug):
        return {"slug": slug, "success": False, "message": f"Invalid slug format: {slug}"}

    # 1. Read frontmatter
    article = read_article_frontmatter(slug)
    if not article:
        return {"slug": slug, "success": False, "message": "Article not found"}

    inferred_status = _infer_article_status(article)
    print(f"  title:  {article['title']}")
    print(f"  status: {inferred_status}")
    print(f"  tags:   {', '.join(article['tags'])}")

    # 2. Generate keywords with Claude Haiku
    print("  -> Generating keywords (Claude Haiku)...", end="", flush=True)
    keywords = extract_keywords_with_claude(article)
    print(f" {len(keywords)} generated")
    for kw in keywords:
        print(f"    - {kw}")

    if not keywords and not dry_run:
        msg = "[WARN] No keywords generated. Skipping (ad group not created)."
        print(f"  {msg}")
        return {"slug": slug, "success": True, "message": msg, "kw_added": 0, "rsa": "skipped"}

    # 3. Generate RSA text
    headlines = build_rsa_headlines(article, keywords)
    descriptions = build_rsa_descriptions(article)
    category = article.get("category", "columns")
    final_url_base = FINAL_URL_MAP.get(category, FINAL_URL_MAP["columns"])
    final_url = f"{final_url_base}{slug}"

    if dry_run:
        article_status = _infer_article_status(article)
        ag_status = "ENABLED" if article_status == "published" else "PAUSED"
        print(f"\n  [DRY-RUN] No actual API calls")
        print(f"  Campaign: {CAMPAIGN_NAME} (ENABLED)")
        print(f"  Ad group: article: {slug} ({ag_status} <- status: {article_status})")
        print(f"  Landing page: {final_url}")
        print(f"  Keywords ({len(keywords)}): {', '.join(keywords[:3])}...")
        print(f"  RSA headlines ({len(headlines)}):")
        for i, h in enumerate(headlines[:5]):
            print(f"    [{i+1}] {h} (width:{display_width(h)})")
        print(f"  RSA descriptions ({len(descriptions)}):")
        for d in descriptions:
            print(f"    {d[:40]}...")
        return {
            "slug": slug,
            "success": True,
            "message": "dry-run: no API calls",
            "keywords_count": len(keywords),
            "headlines_count": len(headlines),
        }

    # 4. Check Google Ads API availability
    if not GOOGLE_ADS_AVAILABLE:
        print("  [ERROR] google-ads package not found: pip install google-ads", file=sys.stderr)
        return {"slug": slug, "success": False, "message": "google-ads not installed"}

    if not GOOGLE_ADS_YAML.exists():
        print(f"  [ERROR] google-ads.yaml not found: {GOOGLE_ADS_YAML}", file=sys.stderr)
        return {"slug": slug, "success": False, "message": "google-ads.yaml not found"}

    # 5. Sync via Google Ads API
    try:
        client = ads_client or GoogleAdsClient.load_from_storage(str(GOOGLE_ADS_YAML))
        svc = ga_service or client.get_service("GoogleAdsService")

        if campaign_info:
            campaign_resource, campaign_status = campaign_info
        else:
            result = get_or_create_campaign_resource(client, svc)
            if not result:
                return {"slug": slug, "success": False, "message": "Campaign fetch failed"}
            campaign_resource, campaign_status = result

        article_status = _infer_article_status(article)
        ad_group_resource = get_or_create_ad_group(
            client, svc, campaign_resource, slug, article_status,
            monitor_paused=monitor_paused, campaign_name=CAMPAIGN_NAME,
        )
        kw_added, kw_rejected = sync_keywords(client, svc, ad_group_resource, keywords)

        # Wait for the landing page to go live. Shipping an ad while the deploy is
        # still running gets it disapproved for "Destination not working".
        reachable, http_code = wait_for_landing_page(final_url)
        if not reachable:
            msg = (f"Landing page is not live, so no ad was created "
                   f"(HTTP {http_code}): {final_url}")
            print(f"  [WARN] {msg}", file=sys.stderr)
            send_discord(
                title=f"[WARN] Ad creation skipped: {slug}",
                description=msg,
                severity="warning",
                fields={"article": slug, "landing page": final_url, "status": str(http_code)},
            )
            return {"slug": slug, "success": False, "message": msg,
                    "kw_added": kw_added, "rsa": "skipped_unreachable"}

        # Create RSA (skip on policy violation and continue; CREATE-first design preserves existing RSA)
        rsa_action = "skipped"
        rsa_policy_error = None
        try:
            rsa_action = upsert_rsa(client, svc, ad_group_resource, headlines, descriptions, final_url)
        except GoogleAdsException as rsa_ex:
            # Determine policy violation via structured error codes (more type-safe than string matching)
            is_policy = any(
                e.error_code.HasField("policy_violation_error")
                or e.error_code.HasField("policy_finding_error")
                for e in rsa_ex.failure.errors
            )
            if is_policy:
                rsa_policy_error = rsa_ex.failure.errors[0].message
                rsa_action = "policy_violation"
                print(f"  [WARN] RSA policy violation, skipping (existing RSA preserved): {rsa_policy_error}", file=sys.stderr)
            else:
                raise  # Re-raise non-policy errors

        msg = f"[OK] KW+{kw_added} / RSA {rsa_action}"
        if kw_rejected:
            msg += f" / {len(kw_rejected)} KWs rejected (policy)"
        if rsa_policy_error:
            msg += " / RSA policy violation (existing RSA preserved)"
        print(f"  {msg}")

        ag_status_label = "ENABLED (serving)" if article_status == "published" else "PAUSED (waiting for publish)"
        discord_fields = {
            "Campaign": f"{CAMPAIGN_NAME} ({campaign_status})",
            "Ad Group": ag_status_label,
            "Landing Page": final_url,
            "New KWs": str(kw_added),
            "RSA": rsa_action,
        }
        if kw_rejected:
            discord_fields["Rejected KWs"] = ", ".join(kw_rejected[:3])
        if rsa_policy_error:
            discord_fields["RSA Warning"] = rsa_policy_error[:100]

        severity = "WARNING" if (kw_rejected or rsa_policy_error) else "SUCCESS"
        send_discord(
            title=f"[OK] Article -> Ad Grants sync complete: {slug}",
            description=f"Ad group for \"{article['title']}\" has been updated.",
            severity=severity,
            fields=discord_fields,
        )
        return {
            "slug": slug, "success": True, "message": msg,
            "kw_added": kw_added, "rsa": rsa_action,
            "kw_rejected": kw_rejected,
            "rsa_policy_error": rsa_policy_error,
        }

    except GoogleAdsException as ex:
        err = f"Google Ads API error: {ex.error.code().name}"
        print(f"  [ERROR] {err}", file=sys.stderr)
        send_discord(
            title=f"[ERROR] Article -> Ad Grants sync failed: {slug}",
            description=err,
            severity="CRITICAL",
            fields={"slug": slug, "Campaign": CAMPAIGN_NAME},
        )
        return {"slug": slug, "success": False, "message": err}


def main():
    parser = argparse.ArgumentParser(description="Auto-generate Ad Grants campaigns from content articles")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--article", type=str, help="Article slug (e.g. npo-ai-adoption-challenges)")
    group.add_argument("--all", action="store_true", help="Sync all articles")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no API calls")
    parser.add_argument("--force", action="store_true", help="Force-overwrite existing RSA")
    args = parser.parse_args()

    if args.article:
        slugs = [args.article]
    else:
        slugs = list_all_articles()
        if not slugs:
            print("No articles found.", file=sys.stderr)
            sys.exit(1)

    print(f"Target: {len(slugs)} articles")
    if args.dry_run:
        print("[DRY-RUN mode] No API calls will be made\n")

    # Initialize Google Ads client once (skip in dry-run mode)
    ads_client = None
    ads_ga_service = None
    campaign_info = None
    if not args.dry_run and GOOGLE_ADS_AVAILABLE and GOOGLE_ADS_YAML.exists():
        ads_client = GoogleAdsClient.load_from_storage(str(GOOGLE_ADS_YAML))
        ads_ga_service = ads_client.get_service("GoogleAdsService")
        campaign_info = get_or_create_campaign_resource(ads_client, ads_ga_service)

    # Read the monitor's pause ledger once, so the sync never revives what it stopped
    monitor_paused = load_monitor_paused_names()
    if monitor_paused:
        print(f"Ad groups paused by the monitor: {len(monitor_paused)} (the sync will not resume them)")

    results = []
    for slug in slugs:
        try:
            result = sync_article(slug, dry_run=args.dry_run, force=args.force,
                                  ads_client=ads_client, ga_service=ads_ga_service,
                                  campaign_info=campaign_info,
                                  monitor_paused=monitor_paused)
        except Exception as e:
            print(f"\n[{slug}] [ERROR] Unexpected error: {e}", file=sys.stderr)
            result = {"slug": slug, "success": False, "message": f"Unexpected error: {e}"}
        results.append(result)

    # Summary
    success = sum(1 for r in results if r["success"])
    fail = len(results) - success
    print(f"\n=== Complete ===")
    print(f"Success: {success} / Failed: {fail} / Total: {len(results)}")

    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
