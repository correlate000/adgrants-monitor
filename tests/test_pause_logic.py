"""Tests for monitor_ad_performance pause/strategy logic.

is_pause_target is the rule that decides which live keywords the script
will pause. The safety guarantees (QS<=1 always, per-strategy thresholds,
None=no-pause) must not silently regress.
"""

import importlib
import sys
from unittest import mock

import pytest


@pytest.fixture
def monitor_module():
    """Import monitor_ad_performance with a controlled CAMPAIGN_PAUSE_TYPE.

    The module validates the pause-type prefixes at import time, so we
    patch config.CAMPAIGN_PAUSE_TYPE before importing and reload if needed.
    """
    # Reset so the patch applies cleanly on first import.
    for mod in ("monitor_ad_performance",):
        sys.modules.pop(mod, None)

    pause_type_map = {
        "IT適性診断": "cv",
        "ISVD記事": "ctr",
        "ISVD調査": "ctr",
        "Example Campaign": None,
    }
    with mock.patch.dict("config.CAMPAIGN_PAUSE_TYPE", pause_type_map, clear=True):
        import monitor_ad_performance as mod
        importlib.reload(mod)
        yield mod


def _kw(campaign="IT適性診断 - 検索", imp=0, clicks=0, ctr=0.0, qs=5, conversions=0.0):
    return {
        "campaign_name": campaign,
        "impressions": imp,
        "clicks": clicks,
        "ctr": ctr,
        "quality_score": qs,
        "conversions": conversions,
    }


class TestGetCampaignPauseType:
    def test_cv_campaign(self, monitor_module):
        assert monitor_module.get_campaign_pause_type("IT適性診断 - 検索") == "cv"

    def test_ctr_campaign(self, monitor_module):
        assert monitor_module.get_campaign_pause_type("ISVD記事 - 検索") == "ctr"

    def test_no_match(self, monitor_module):
        assert monitor_module.get_campaign_pause_type("Unknown Campaign") is None

    def test_none_strategy(self, monitor_module):
        assert monitor_module.get_campaign_pause_type("Example Campaign - Search") is None


class TestIsPauseTarget:
    def test_qs_1_always_pauses(self, monitor_module):
        # Even with healthy metrics, QS=1 -> immediate stop.
        kw = _kw(qs=1, ctr=0.20, imp=1000, clicks=200, conversions=10)
        hit, reason = monitor_module.is_pause_target(kw)
        assert hit is True
        assert "QS 1" in reason

    def test_qs_0_also_pauses(self, monitor_module):
        kw = _kw(qs=0, ctr=0.20)
        hit, _ = monitor_module.is_pause_target(kw)
        assert hit is True

    def test_cv_strategy_pauses_when_many_clicks_no_cv(self, monitor_module):
        # Uses default campaign (IT適性診断 = cv). Threshold is 20 clicks.
        kw = _kw(campaign="IT適性診断 - 検索", clicks=20, conversions=0.0, qs=5)
        hit, _ = monitor_module.is_pause_target(kw)
        assert hit is True

    def test_cv_strategy_keeps_converting_keyword(self, monitor_module):
        kw = _kw(campaign="IT適性診断 - 検索", clicks=50, conversions=1.0, qs=5)
        hit, _ = monitor_module.is_pause_target(kw)
        assert hit is False

    def test_cv_strategy_under_click_threshold(self, monitor_module):
        kw = _kw(campaign="IT適性診断 - 検索", clicks=19, conversions=0, qs=5)
        hit, _ = monitor_module.is_pause_target(kw)
        assert hit is False

    def test_ctr_strategy_100imp_boundary(self, monitor_module):
        # imp>=100 and ctr<3%
        kw = _kw(campaign="ISVD記事 - 検索", imp=100, ctr=0.029, qs=5)
        hit, _ = monitor_module.is_pause_target(kw)
        assert hit is True

    def test_ctr_strategy_100imp_above_3pct(self, monitor_module):
        kw = _kw(campaign="ISVD記事 - 検索", imp=100, ctr=0.03, qs=5)
        hit, _ = monitor_module.is_pause_target(kw)
        assert hit is False

    def test_ctr_strategy_50imp_boundary(self, monitor_module):
        # imp>=50 and ctr<2% also fires.
        kw = _kw(campaign="ISVD記事 - 検索", imp=50, ctr=0.019, qs=5)
        hit, _ = monitor_module.is_pause_target(kw)
        assert hit is True

    def test_ctr_strategy_low_imp_safe(self, monitor_module):
        kw = _kw(campaign="ISVD記事 - 検索", imp=49, ctr=0.0, qs=5)
        hit, _ = monitor_module.is_pause_target(kw)
        assert hit is False

    def test_none_strategy_only_qs1_triggers(self, monitor_module):
        kw = _kw(campaign="Example Campaign - Search", imp=10_000, ctr=0.0, qs=5)
        hit, _ = monitor_module.is_pause_target(kw)
        assert hit is False

    def test_none_strategy_qs1_still_triggers(self, monitor_module):
        kw = _kw(campaign="Example Campaign - Search", qs=1)
        hit, _ = monitor_module.is_pause_target(kw)
        assert hit is True

    def test_unknown_campaign_defaults_safe(self, monitor_module):
        # Unknown campaign -> pause_type=None, so only QS<=1 would pause.
        kw = _kw(campaign="Untracked Campaign", imp=10_000, ctr=0.0, qs=5)
        hit, _ = monitor_module.is_pause_target(kw)
        assert hit is False


class TestAmbiguousPrefixAssertion:
    def test_ambiguous_prefixes_raise(self):
        """If a user adds a prefix that's a prefix of another key, fail fast."""
        sys.modules.pop("monitor_ad_performance", None)
        bad = {"IT": "ctr", "IT適性診断": "cv"}
        with mock.patch.dict("config.CAMPAIGN_PAUSE_TYPE", bad, clear=True):
            with pytest.raises(ValueError, match="ambiguous prefix"):
                import monitor_ad_performance  # noqa: F401

    def test_longest_prefix_wins(self):
        """When keys don't collide as prefixes, the longest matching prefix wins."""
        sys.modules.pop("monitor_ad_performance", None)
        # These don't overlap as prefixes, so import succeeds.
        ok_map = {"IT適性診断": "cv", "ISVD記事": "ctr"}
        with mock.patch.dict("config.CAMPAIGN_PAUSE_TYPE", ok_map, clear=True):
            import monitor_ad_performance as mod
            importlib.reload(mod)
            assert mod.get_campaign_pause_type("IT適性診断 - 検索") == "cv"
            assert mod.get_campaign_pause_type("ISVD記事 - 検索") == "ctr"
