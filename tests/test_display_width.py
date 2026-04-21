"""Tests for truncate_to_width — RSA headline/description width enforcement.

Google Ads rejects RSA text over 30 half-width chars (headline) or 90
(description). Full-width CJK counts as 2, so the truncation must never
leave a trailing fractional character that pushes over the limit.
"""

from sync_articles_to_ads import truncate_to_width


class TestTruncateToWidth:
    def test_ascii_exact(self):
        assert truncate_to_width("abcdef", 3) == "abc"

    def test_ascii_under_limit(self):
        assert truncate_to_width("abc", 10) == "abc"

    def test_empty_string(self):
        assert truncate_to_width("", 5) == ""

    def test_fullwidth_fits_exactly(self):
        # Each 全角 char = 2. 3 chars = width 6.
        assert truncate_to_width("あいう", 6) == "あいう"

    def test_fullwidth_rejects_partial_char(self):
        # Cannot fit a 2-width char at width 5 (would be 6). Stop at "あい" (width 4).
        assert truncate_to_width("あいう", 5) == "あい"

    def test_mixed_ascii_and_fullwidth(self):
        # "abあ" = 1+1+2 = 4. Limit 3 → "ab" (width 2, next would overflow).
        assert truncate_to_width("abあ", 3) == "ab"

    def test_mixed_fits_boundary(self):
        assert truncate_to_width("abあ", 4) == "abあ"

    def test_zero_limit(self):
        assert truncate_to_width("abc", 0) == ""

    def test_headline_max_width_30(self):
        # Realistic: 15 CJK chars = 30. Should fit.
        s = "あ" * 15
        assert truncate_to_width(s, 30) == s

    def test_headline_over_limit_truncates(self):
        s = "あ" * 20  # width 40
        out = truncate_to_width(s, 30)
        # Use the same width function it enforces.
        from ads_common.gaql import display_width
        assert display_width(out) <= 30
        assert display_width(out) == 30  # tight packing of full-width chars
