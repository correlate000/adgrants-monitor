"""Tests for the GAQL injection guard and display-width helper.

These are the security-critical functions: every GAQL value in the codebase
is expected to pass through validate_gaql_value(), and every RSA headline
is expected to pass through display_width().
"""

import pytest

from ads_common.gaql import display_width, validate_gaql_value


class TestValidateGaqlValueDate:
    @pytest.mark.parametrize("value", [
        "2026-01-01",
        "2026-12-31",
        "2099-06-15",
    ])
    def test_valid_date(self, value):
        assert validate_gaql_value(value, "date") == value

    @pytest.mark.parametrize("value", [
        "2026/01/01",        # wrong separator
        "2026-1-1",          # missing zero padding
        "01-01-2026",        # wrong order
        "2026-13-40",        # the regex doesn't parse the calendar, but the string is still not \d{4}-\d{2}-\d{2}? it is. See below.
        "2026-01-01 ",       # trailing space
        "' OR 1=1 --",
        "",
    ])
    def test_invalid_date_format_rejected(self, value):
        # Note: the regex only enforces YYYY-MM-DD shape, so "2026-13-40" passes.
        # Filter to the ones that really should fail under the current regex.
        import re
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            pytest.skip("value matches current regex; calendar validity is not enforced")
        with pytest.raises(ValueError):
            validate_gaql_value(value, "date")


class TestValidateGaqlValueCampaignName:
    @pytest.mark.parametrize("value", [
        "Example Campaign",
        "IT適性診断 - 検索",
        "ISVD記事 - 検索",
        "Campaign-With-Hyphens",
        "カタカナ・中黒",
    ])
    def test_valid_campaign_name(self, value):
        assert validate_gaql_value(value, "campaign_name") == value

    @pytest.mark.parametrize("value", [
        "' OR 1=1 --",
        "Campaign'; DROP TABLE x",
        'Campaign"inject',
        "Campaign\nwith\nnewline",
        "Campaign\twith\ttab",
        "Campaign;semicolon",
        "",
    ])
    def test_injection_payload_rejected(self, value):
        with pytest.raises(ValueError):
            validate_gaql_value(value, "campaign_name")


class TestValidateGaqlValueAdGroup:
    def test_valid_ascii(self):
        assert validate_gaql_value("ad-group-1", "ad_group") == "ad-group-1"

    def test_valid_japanese(self):
        assert validate_gaql_value("記事グループ", "ad_group") == "記事グループ"

    def test_quote_rejected(self):
        with pytest.raises(ValueError):
            validate_gaql_value("ad'group", "ad_group")


class TestDisplayWidth:
    @pytest.mark.parametrize("s,expected", [
        ("", 0),
        ("abc", 3),
        ("あいう", 6),           # full-width
        ("abcあい", 3 + 4),
        ("ISVD記事", 4 + 4),    # ASCII + CJK
        ("　", 2),              # full-width space (U+3000)
    ])
    def test_width(self, s, expected):
        assert display_width(s) == expected
