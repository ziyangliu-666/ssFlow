"""Tests for quantitative publication effects (Phase 5)."""

import pytest

from ssflow.publication_effects import (
    AggregateEffect,
    DecayingEffect,
    EffectTracker,
    PublicationEffect,
    aggregate_round_effects,
    apply_effects_to_participation,
    apply_effects_to_risk_budget,
    apply_effects_to_sentiment,
    compute_publication_effect,
    list_known_publication_types,
)


class TestComputePublicationEffect:
    """Test effect lookup for each source type."""

    def test_exchange_inquiry_bearish(self):
        eff = compute_publication_effect("exchange_inquiry")
        assert eff.source_type == "exchange_inquiry"
        assert eff.sentiment_shift < 0  # bearish
        assert eff.participation_modifier < 1.0  # dampens
        assert eff.risk_budget_shift < 0
        assert "retail" in eff.affected_personas
        assert "kol" in eff.affected_personas
        assert eff.decay_rounds == 3

    def test_csrc_verbal_support_bullish(self):
        eff = compute_publication_effect("csrc_verbal_support")
        assert eff.sentiment_shift > 0  # bullish
        assert eff.participation_modifier > 1.0  # increases activity
        assert eff.risk_budget_shift > 0
        assert eff.decay_rounds == 2

    def test_national_team_buy_strong_bullish(self):
        eff = compute_publication_effect("national_team_buy")
        assert eff.sentiment_shift == pytest.approx(0.35)
        assert eff.participation_modifier == pytest.approx(1.5)
        assert eff.risk_budget_shift == pytest.approx(0.3)
        assert "institutional" in eff.affected_personas
        assert "retail" in eff.affected_personas
        assert eff.decay_rounds == 4

    def test_analyst_upgrade_mild_bullish(self):
        eff = compute_publication_effect("analyst_upgrade")
        assert eff.sentiment_shift == pytest.approx(0.10)
        assert "institutional" in eff.affected_personas
        assert "analyst" in eff.affected_personas
        assert eff.decay_rounds == 2

    def test_analyst_downgrade_mild_bearish(self):
        eff = compute_publication_effect("analyst_downgrade")
        assert eff.sentiment_shift == pytest.approx(-0.10)
        assert eff.risk_budget_shift < 0

    def test_kol_hype_post(self):
        eff = compute_publication_effect("kol_hype_post")
        assert eff.sentiment_shift == pytest.approx(0.05)
        assert eff.urgency_modifier > 1.0  # high urgency
        assert eff.participation_modifier > 1.0
        assert "retail" in eff.affected_personas
        assert eff.decay_rounds == 1  # short-lived

    def test_official_media_warning_strong_bearish(self):
        eff = compute_publication_effect("official_media_warning")
        assert eff.sentiment_shift < -0.2  # strongly bearish
        assert eff.participation_modifier < 1.0  # dampens strongly
        assert eff.urgency_modifier < 1.0  # reduces urgency
        assert eff.risk_budget_shift == pytest.approx(-0.30)
        assert eff.decay_rounds == 3

    def test_broker_margin_call(self):
        eff = compute_publication_effect("broker_margin_call")
        assert eff.sentiment_shift < 0
        assert eff.urgency_modifier == pytest.approx(2.0)  # very urgent
        assert eff.risk_budget_shift == pytest.approx(-0.40)
        assert "margin_retail" in eff.affected_personas
        assert eff.decay_rounds == 1

    def test_alias_resolution(self):
        """Aliases should resolve to canonical types."""
        eff1 = compute_publication_effect("exchange_notice")
        eff2 = compute_publication_effect("exchange_inquiry")
        assert eff1.source_type == eff2.source_type
        assert eff1.sentiment_shift == eff2.sentiment_shift

    def test_unknown_type_raises(self):
        with pytest.raises(KeyError, match="Unknown publication type"):
            compute_publication_effect("nonexistent_type")

    def test_authority_weight_scaling(self):
        """Higher authority weight should scale the effect magnitude."""
        eff_full = compute_publication_effect("national_team_buy", authority_weight=1.0)
        eff_half = compute_publication_effect("national_team_buy", authority_weight=0.5)
        assert abs(eff_full.sentiment_shift) > abs(eff_half.sentiment_shift)
        assert eff_half.sentiment_shift == pytest.approx(0.35 * 0.5)

    def test_authority_weight_clamped(self):
        """Authority weight outside [0, 1] should be clamped."""
        eff = compute_publication_effect("kol_hype_post", authority_weight=1.5)
        # Should behave as weight=1.0
        eff_normal = compute_publication_effect("kol_hype_post", authority_weight=1.0)
        assert eff.sentiment_shift == eff_normal.sentiment_shift


class TestAggregateRoundEffects:
    def test_empty(self):
        agg = aggregate_round_effects([])
        assert agg.is_neutral
        assert agg.source_count == 0

    def test_single_effect(self):
        eff = compute_publication_effect("national_team_buy")
        agg = aggregate_round_effects([eff])
        assert agg.sentiment_shift == pytest.approx(0.35)
        assert agg.participation_modifier == pytest.approx(1.5)
        assert agg.source_count == 1

    def test_multiple_bullish(self):
        """Multiple bullish publications should compound."""
        eff1 = compute_publication_effect("csrc_verbal_support")
        eff2 = compute_publication_effect("national_team_buy")
        agg = aggregate_round_effects([eff1, eff2])
        assert agg.sentiment_shift == pytest.approx(0.20 + 0.35)
        # Participation is multiplicative
        assert agg.participation_modifier == pytest.approx(1.2 * 1.5)
        assert agg.source_count == 2

    def test_mixed_effects(self):
        """Bullish + bearish should partially cancel."""
        eff_bull = compute_publication_effect("national_team_buy")
        eff_bear = compute_publication_effect("official_media_warning")
        agg = aggregate_round_effects([eff_bull, eff_bear])
        # Net sentiment: +0.35 + (-0.25) = +0.10
        assert agg.sentiment_shift == pytest.approx(0.10)

    def test_sentiment_clamped(self):
        """Many bearish publications should clamp sentiment to -1.0."""
        bear = compute_publication_effect("official_media_warning")
        many = [bear] * 10
        agg = aggregate_round_effects(many)
        assert agg.sentiment_shift == pytest.approx(-1.0)

    def test_risk_budget_clamped(self):
        """Risk budget shift should clamp to [-0.5, 0.5]."""
        eff = compute_publication_effect("broker_margin_call")
        # Each shifts -0.40
        agg = aggregate_round_effects([eff, eff])
        assert agg.risk_budget_shift == pytest.approx(-0.5)

    def test_affected_personas_union(self):
        eff1 = compute_publication_effect("exchange_inquiry")  # retail, kol
        eff2 = compute_publication_effect("national_team_buy")  # institutional, retail
        agg = aggregate_round_effects([eff1, eff2])
        assert "retail" in agg.affected_personas
        assert "kol" in agg.affected_personas
        assert "institutional" in agg.affected_personas


class TestApplyEffects:
    def test_exchange_inquiry_dampens_retail_participation(self):
        """Core scenario: exchange inquiry letter should reduce participation."""
        eff = compute_publication_effect("exchange_inquiry")
        agg = aggregate_round_effects([eff])
        base = 0.80
        result = apply_effects_to_participation(base, agg)
        assert result < base  # dampened
        assert result == pytest.approx(0.80 * 0.7)

    def test_national_team_boosts_institutional_participation(self):
        """Core scenario: national team announcement should boost participation."""
        eff = compute_publication_effect("national_team_buy")
        agg = aggregate_round_effects([eff])
        base = 0.50
        result = apply_effects_to_participation(base, agg)
        assert result > base  # boosted
        assert result == pytest.approx(0.50 * 1.5)

    def test_participation_clamped_at_one(self):
        eff = compute_publication_effect("national_team_buy")
        agg = aggregate_round_effects([eff])
        result = apply_effects_to_participation(0.90, agg)
        assert result <= 1.0

    def test_participation_clamped_at_zero(self):
        eff = compute_publication_effect("official_media_warning")
        agg = aggregate_round_effects([eff, eff, eff])
        result = apply_effects_to_participation(0.10, agg)
        assert result >= 0.0

    def test_risk_budget_shift(self):
        eff = compute_publication_effect("broker_margin_call")
        agg = aggregate_round_effects([eff])
        base = 0.50
        result = apply_effects_to_risk_budget(base, agg)
        assert result < base  # reduced risk tolerance
        assert result == pytest.approx(0.50 + (-0.40))

    def test_risk_budget_clamped_floor(self):
        eff = compute_publication_effect("broker_margin_call")
        agg = aggregate_round_effects([eff])
        result = apply_effects_to_risk_budget(0.10, agg)
        assert result == 0.0

    def test_sentiment_shift(self):
        eff = compute_publication_effect("national_team_buy")
        agg = aggregate_round_effects([eff])
        base = -0.20
        result = apply_effects_to_sentiment(base, agg)
        assert result > base
        assert result == pytest.approx(-0.20 + 0.35)

    def test_neutral_effects_no_change(self):
        agg = aggregate_round_effects([])
        assert apply_effects_to_participation(0.5, agg) == pytest.approx(0.5)
        assert apply_effects_to_risk_budget(0.5, agg) == pytest.approx(0.5)
        assert apply_effects_to_sentiment(0.0, agg) == pytest.approx(0.0)


class TestDecay:
    def test_single_round_effect_expires(self):
        """KOL hype post has decay_rounds=1, so it expires after round 0."""
        eff = compute_publication_effect("kol_hype_post")
        de = DecayingEffect(effect=eff, origin_round=0)
        # Round 0: full effect
        r0 = de.effect_at_round(0)
        assert r0 is not None
        assert r0.sentiment_shift == pytest.approx(0.05)
        # Round 1: expired
        r1 = de.effect_at_round(1)
        assert r1 is None

    def test_multi_round_decay(self):
        """National team buy has decay_rounds=4, should decay over time."""
        eff = compute_publication_effect("national_team_buy")
        de = DecayingEffect(effect=eff, origin_round=0)
        # Round 0: full
        r0 = de.effect_at_round(0)
        assert r0.sentiment_shift == pytest.approx(0.35)
        # Round 1: decayed (default factor 0.6)
        r1 = de.effect_at_round(1)
        assert r1 is not None
        assert r1.sentiment_shift == pytest.approx(0.35 * 0.6)
        # Round 2: decayed further
        r2 = de.effect_at_round(2)
        assert r2 is not None
        assert r2.sentiment_shift == pytest.approx(0.35 * 0.6 ** 2)
        # Round 3: still within decay window
        r3 = de.effect_at_round(3)
        assert r3 is not None
        # Round 4: expired (decay_rounds=4, so rounds 0,1,2,3 are valid)
        r4 = de.effect_at_round(4)
        assert r4 is None

    def test_participation_modifier_decays_toward_one(self):
        """Participation modifier should decay toward 1.0 (no effect)."""
        eff = compute_publication_effect("national_team_buy")
        de = DecayingEffect(effect=eff, origin_round=0)
        r0 = de.effect_at_round(0)
        r1 = de.effect_at_round(1)
        # participation_modifier is 1.5 at round 0
        # At round 1: 1.0 + (1.5 - 1.0) * 0.6 = 1.3
        assert r0.participation_modifier == pytest.approx(1.5)
        assert r1.participation_modifier == pytest.approx(1.3)

    def test_tracker_lifecycle(self):
        """EffectTracker should track, decay, and prune effects."""
        tracker = EffectTracker()
        eff = compute_publication_effect("kol_hype_post")  # decay_rounds=1
        tracker.add(eff, round_idx=0)
        assert tracker.active_count == 1

        # Round 0: effect active
        round0 = tracker.effects_at_round(0)
        assert len(round0) == 1
        assert round0[0].sentiment_shift == pytest.approx(0.05)

        # Round 1: expired, pruned
        round1 = tracker.effects_at_round(1)
        assert len(round1) == 0
        assert tracker.active_count == 0

    def test_tracker_multiple_effects(self):
        """Multiple effects at different rounds should coexist."""
        tracker = EffectTracker()
        tracker.add(compute_publication_effect("national_team_buy"), round_idx=0)
        tracker.add(compute_publication_effect("kol_hype_post"), round_idx=1)

        r0 = tracker.effects_at_round(0)
        assert len(r0) == 1  # only national_team active

        r1 = tracker.effects_at_round(1)
        assert len(r1) == 2  # national_team (decayed) + kol (fresh)

        r2 = tracker.effects_at_round(2)
        assert len(r2) == 1  # only national_team (kol expired)

    def test_tracker_clear(self):
        tracker = EffectTracker()
        tracker.add(compute_publication_effect("national_team_buy"), round_idx=0)
        tracker.clear()
        assert tracker.active_count == 0
        assert tracker.effects_at_round(0) == []


class TestListKnownTypes:
    def test_all_types_present(self):
        types = list_known_publication_types()
        expected = {
            "exchange_inquiry", "csrc_verbal_support", "national_team_buy",
            "analyst_upgrade", "analyst_downgrade", "kol_hype_post",
            "official_media_warning", "broker_margin_call",
        }
        assert set(types) == expected

    def test_is_bullish_property(self):
        eff = compute_publication_effect("national_team_buy")
        assert eff.is_bullish
        assert not eff.is_bearish

    def test_is_bearish_property(self):
        eff = compute_publication_effect("exchange_inquiry")
        assert eff.is_bearish
        assert not eff.is_bullish
