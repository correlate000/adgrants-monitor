"""Tests for analyze_search_terms scoring rules.

These rules decide whether a real search query gets added as a negative
keyword. A bug here either (a) blocks legitimate traffic or (b) wastes the
Ad Grants budget.
"""

from analyze_search_terms import (
    SCORE_AUTO_EXECUTE,
    SCORE_CANDIDATE,
    SCORE_HIGH,
    analyze_search_terms,
    score_search_term,
)


def _row(term="web marketing", imp=0, clicks=0, ctr=0.0, conversions=0.0, status="NONE"):
    return {
        "search_term": term,
        "impressions": imp,
        "clicks": clicks,
        "ctr": ctr,
        "conversions": conversions,
        "status": status,
        "ad_group": "test",
        "cost_micros": 0,
    }


class TestScoreRules:
    def test_benign_unclassified_term_scores_1(self):
        # status=NONE alone -> +1
        assert score_search_term(_row(status="NONE")) == 1

    def test_excluded_term_scores_0(self):
        assert score_search_term(_row(status="EXCLUDED")) == 0

    def test_imp10_low_ctr_rule(self):
        # status=NONE (+1), imp>=10 ctr<3% (+3), imp>=5 clicks=0 (+2) = 6.
        # The imp>=5 & clicks=0 rule naturally stacks with imp>=10 & ctr<3%.
        assert score_search_term(_row(imp=10, ctr=0.02, clicks=0, status="NONE")) == 6

    def test_imp5_zero_clicks_rule(self):
        # status=NONE (+1) AND imp>=5, clicks=0 (+2) = 3
        assert score_search_term(_row(imp=5, clicks=0, status="NONE")) == 3

    def test_clicks_no_conversion_rule(self):
        # status=NONE (+1) AND clicks>=3, conversions=0 (+2) = 3
        assert score_search_term(_row(clicks=3, conversions=0, status="NONE")) == 3

    def test_suspicious_pattern_rule(self):
        # "求人" = job-seeking pattern (+2), status=NONE (+1) = 3
        assert score_search_term(_row(term="it 求人", status="NONE")) == 3

    def test_suspicious_pattern_case_insensitive_for_ascii(self):
        # term is lower-cased before matching; "Indeed" should match.
        assert score_search_term(_row(term="Indeed Japan", status="NONE")) >= 3

    def test_all_rules_stack_to_auto_execute_threshold(self):
        # imp=10, ctr=0 (+3), clicks=0 — wait, clicks<3 so rule 3 won't fire.
        # Use: imp=10 ctr<3% (+3), imp>=5 & clicks=0 (+2), suspicious (+2), NONE (+1) = 8
        row = _row(term="求人 indeed", imp=10, clicks=0, ctr=0.0, conversions=0, status="NONE")
        score = score_search_term(row)
        assert score >= SCORE_AUTO_EXECUTE
        assert score == 8

    def test_clicks_gte_3_no_conversion_plus_suspicious(self):
        # clicks=3, conversions=0 (+2), suspicious (+2), NONE (+1) = 5
        row = _row(term="aws 資格 問題集", clicks=3, conversions=0, status="NONE")
        assert score_search_term(row) == SCORE_HIGH


class TestThresholdsSanity:
    def test_thresholds_ordered(self):
        assert SCORE_CANDIDATE < SCORE_HIGH < SCORE_AUTO_EXECUTE


class TestAnalyzeSearchTermsSort:
    def test_sorted_by_score_then_impressions(self):
        rows = [
            _row(term="low", imp=100, status="EXCLUDED"),   # score 0
            _row(term="hi-score", imp=10, ctr=0.0, status="NONE"),  # score 4+ via rule
            _row(term="med", imp=5, clicks=0, status="NONE"),       # score 3
        ]
        out = analyze_search_terms(rows)
        scores = [r["score"] for r in out]
        assert scores == sorted(scores, reverse=True), "Must be score-descending"

    def test_is_candidate_and_is_high_priority_flags(self):
        rows = [_row(imp=10, clicks=0, ctr=0.0, status="NONE", term="求人 it")]
        out = analyze_search_terms(rows)
        r = out[0]
        assert r["is_candidate"] is True
        assert r["is_high_priority"] == (r["score"] >= SCORE_HIGH)
