"""Tests for ads_common.time — the JST date-range unifier."""

from datetime import datetime, timezone
from unittest import mock

import pytest

from ads_common import time as t


class TestJstToday:
    def test_format(self):
        s = t.jst_today()
        # YYYY-MM-DD
        datetime.strptime(s, "%Y-%m-%d")

    def test_timezone_boundary(self):
        """At 23:30 UTC on Jan 1, JST is already Jan 2."""
        with mock.patch("ads_common.time.datetime") as mdt:
            mdt.now.return_value = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc).astimezone(t.JST)
            # Emulate .astimezone behavior: direct call via real datetime
            mdt.now.side_effect = lambda tz=None: datetime(2026, 1, 2, 8, 30, tzinfo=tz)
            assert t.jst_today() == "2026-01-02"


class TestBuildDateRange:
    def test_days_1_ends_today(self):
        with mock.patch("ads_common.time.datetime") as mdt:
            mdt.now.side_effect = lambda tz=None: datetime(2026, 3, 20, 12, 0, tzinfo=tz)
            start, end = t.build_date_range(1)
            assert start == "2026-03-20"
            assert end == "2026-03-20"

    def test_days_7_window(self):
        with mock.patch("ads_common.time.datetime") as mdt:
            mdt.now.side_effect = lambda tz=None: datetime(2026, 3, 20, 12, 0, tzinfo=tz)
            start, end = t.build_date_range(7)
            assert start == "2026-03-14"
            assert end == "2026-03-20"

    def test_end_offset_1_ends_yesterday(self):
        """analyze_search_terms uses end_offset=1 to skip partial-day data."""
        with mock.patch("ads_common.time.datetime") as mdt:
            mdt.now.side_effect = lambda tz=None: datetime(2026, 3, 20, 12, 0, tzinfo=tz)
            start, end = t.build_date_range(7, end_offset=1)
            assert end == "2026-03-19"
            assert start == "2026-03-13"

    @pytest.mark.parametrize("days", [1, 7, 14, 30, 90])
    def test_window_size_matches_days(self, days):
        with mock.patch("ads_common.time.datetime") as mdt:
            mdt.now.side_effect = lambda tz=None: datetime(2026, 3, 20, 12, 0, tzinfo=tz)
            start, end = t.build_date_range(days)
            s = datetime.strptime(start, "%Y-%m-%d")
            e = datetime.strptime(end, "%Y-%m-%d")
            assert (e - s).days == days - 1
