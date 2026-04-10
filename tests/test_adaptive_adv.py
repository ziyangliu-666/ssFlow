"""Tests for AdaptiveADV (Fix 3: Dynamic ADV)."""

import pytest

from ssflow.market_dynamics import AdaptiveADV, compute_price_impact


class TestAdaptiveADV:
    def test_initial_equals_baseline(self):
        adv = AdaptiveADV(baseline=8e9)
        assert adv.effective == 8e9

    def test_update_increases_on_high_volume(self):
        adv = AdaptiveADV(baseline=8e9)
        adv.update(16e9)  # 2x baseline
        assert adv.effective > 8e9

    def test_update_decreases_on_low_volume(self):
        adv = AdaptiveADV(baseline=8e9)
        adv.update(2e9)  # 0.25x baseline
        assert adv.effective < 8e9

    def test_floor_clamp(self):
        adv = AdaptiveADV(baseline=8e9, floor_pct=0.20)
        # Drive volume to zero repeatedly
        for _ in range(50):
            adv.update(0.0)
        assert adv.effective >= 8e9 * 0.20
        assert adv.effective == pytest.approx(8e9 * 0.20, rel=0.01)

    def test_ceiling_clamp(self):
        adv = AdaptiveADV(baseline=8e9, ceiling_pct=3.0)
        # Drive volume way up
        for _ in range(50):
            adv.update(100e9)
        assert adv.effective <= 8e9 * 3.0
        assert adv.effective == pytest.approx(8e9 * 3.0, rel=0.01)

    def test_ema_convergence(self):
        """After many rounds of constant volume, effective should converge."""
        adv = AdaptiveADV(baseline=8e9, alpha=0.3)
        target = 5e9
        for _ in range(30):
            adv.update(target)
        assert adv.effective == pytest.approx(target, rel=0.01)

    def test_history_tracked(self):
        adv = AdaptiveADV(baseline=8e9)
        assert len(adv.history) == 0
        adv.update(4e9)
        adv.update(12e9)
        adv.update(8e9)
        assert len(adv.history) == 3

    def test_history_is_copy(self):
        adv = AdaptiveADV(baseline=8e9)
        adv.update(4e9)
        h = adv.history
        h.append(999)
        assert len(adv.history) == 1  # internal not mutated

    def test_low_volume_increases_impact(self):
        """When ADV drops, same net flow produces larger price impact."""
        baseline = 8e9
        flow = 1e8

        # Static ADV
        delta_static = compute_price_impact(flow, baseline)

        # After volume drops to floor
        adv = AdaptiveADV(baseline=baseline, floor_pct=0.20)
        for _ in range(50):
            adv.update(0.0)

        delta_dynamic = compute_price_impact(flow, adv.effective)
        # Lower ADV → higher impact
        assert abs(delta_dynamic) > abs(delta_static)

    def test_high_volume_decreases_impact(self):
        """When ADV rises, same net flow produces smaller price impact."""
        baseline = 8e9
        flow = 1e8

        delta_static = compute_price_impact(flow, baseline)

        adv = AdaptiveADV(baseline=baseline, ceiling_pct=3.0)
        for _ in range(50):
            adv.update(50e9)

        delta_dynamic = compute_price_impact(flow, adv.effective)
        assert abs(delta_dynamic) < abs(delta_static)
