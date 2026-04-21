"""Time utilities shared across scripts.

All Ad Grants operations are JST-based: keyword pauses, BQ snapshot dates,
and search-term windows must agree on the same calendar boundary or the
data shown to operators will diverge from what's stored.
"""

from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


def jst_today() -> str:
    """Return today's date in JST as YYYY-MM-DD."""
    return datetime.now(tz=JST).strftime("%Y-%m-%d")


def jst_now_iso() -> str:
    """Return current JST timestamp in ISO 8601 format."""
    return datetime.now(tz=JST).isoformat()


def build_date_range(days: int, end_offset: int = 0) -> tuple[str, str]:
    """Build an inclusive (start, end) date range in JST.

    Args:
        days: Number of days in the window.
        end_offset: How many days back from today the window ends.
            0 = ends today (used by monitor_ad_performance.py).
            1 = ends yesterday (used by analyze_search_terms.py to avoid
                pulling partial-day data from Google Ads).

    Returns:
        (start_date, end_date) as YYYY-MM-DD strings.
    """
    end = datetime.now(tz=JST).date() - timedelta(days=end_offset)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()
