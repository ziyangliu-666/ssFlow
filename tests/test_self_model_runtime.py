"""End-to-end tests for ``ssflow.self_model.runtime.SelfModelEvaluator``.

Covers the full per-persona lifecycle:
  1. build_evaluators_for_personas from a persona list
  2. Initial state population
  3. update() with TradeResult writes back authoritative values
  4. step_utility() is idempotent within a round
  5. compute_utility breakdown matches weights
  6. select_peers respects role_match + top_k + sort_by
  7. render_prompt produces non-empty multi-section text
  8. state_public_snapshot filters internal-only atoms
  9. DEFAULT_SELF_MODEL works for personas without an explicit spec
"""

from __future__ import annotations

import pytest

from ssflow.event import Event
from ssflow.persona import Persona, SandboxConfig, MarketShare
from ssflow.self_model import (
    DEFAULT_SELF_MODEL_DICT,
    SelfModelEvaluator,
    build_evaluators_for_personas,
)
from ssflow.self_model.schema import RoundCtx, TradeResult, SelfModelSpec


def _make_trader_persona(pid: str = "test_trader", capital: float = 1_000_000.0) -> Persona:
    return Persona(
        id=pid,
        archetype="测试公募基金",
        display_name="测试 PM",
        voice_prompt="test",
        biases={},
        knowledge={},
        market_share=MarketShare(by_volume=0.01),
        decision_mode="discretionary",
        role="directional_speculator",
        sandbox=SandboxConfig(
            instance_count=10,
            capital_distribution={"type": "lognormal", "median_cny": capital},
            initial_position_distribution={
                "type": "bernoulli",
                "prob_holding": 0.5,
                "position_size_pct_when_holding": {
                    "type": "uniform", "min": 0.1, "max": 0.3,
                },
            },
            risk={"max_position_pct": 0.5},
            action_space=[
                {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
                {"name": "buy", "side": "buy", "pool": "cash", "fraction": 0.15},
            ],
        ),
    )


def _make_event(ticker: str = "300750", event_type: str = "policy") -> Event:
    return Event(
        ticker=ticker,
        event_text="Testing self_model integration.",
        event_type=event_type,
        event_date="2024-09-24",
    )


def _round_ctx(round_idx=0, round_hours=0, current_price=100.0, initial_price=100.0):
    return RoundCtx(
        round_idx=round_idx,
        round_hours=round_hours,
        n_rounds=6,
        current_price=current_price,
        initial_price=initial_price,
        cumulative_delta_pct=(current_price / initial_price - 1) if initial_price > 0 else 0,
        event_type="policy",
    )


def _trade(side="hold", qty=0.0, nav=1_100_000, cash=850_000, holdings=250_000, pnl=0.0):
    return TradeResult(
        persona_id="test_trader",
        side=side,
        quantity_pct=qty,
        nav=nav,
        cash=cash,
        holdings_value=holdings,
        unrealized_pnl_pct=pnl,
        avg_position_pct=holdings / nav if nav > 0 else 0,
        net_flow=0.0,
    )


# ────────────────────── Build + initialize ──────────────────────


class TestBuildEvaluators:
    def test_builds_one_per_trader(self):
        traders = [_make_trader_persona(f"pm_{i}") for i in range(3)]
        event = _make_event()
        evaluators = build_evaluators_for_personas(traders, event)
        assert len(evaluators) == 3
        assert all(isinstance(e, SelfModelEvaluator) for e in evaluators.values())

    def test_skips_personas_without_sandbox(self):
        trader = _make_trader_persona("has_sandbox")
        info = Persona(
            id="media",
            archetype="新闻",
            display_name="media",
            voice_prompt="test",
            market_share=MarketShare(by_volume=0.01),
            decision_mode="discretionary",
            role="news_wire",
            sandbox=None,
        )
        evaluators = build_evaluators_for_personas([trader, info], _make_event())
        assert "has_sandbox" in evaluators
        assert "media" not in evaluators

    def test_default_bundle_applied_when_no_spec(self):
        trader = _make_trader_persona()
        assert trader.self_model is None
        evaluators = build_evaluators_for_personas([trader], _make_event())
        ev = evaluators["test_trader"]
        assert "cash" in ev.state
        assert "nav" in ev.state
        assert "unrealized_pnl_pct" in ev.state
        # Default weights are in place
        assert "nav_growth" in ev.spec.utility_weights

    def test_custom_spec_honored(self):
        trader = _make_trader_persona()
        trader.self_model = {
            "state_atoms": ["cash", "nav"],
            "utility_weights": {"nav_growth": 2.0},
            "render_sections": ["financial_snapshot"],
        }
        evaluators = build_evaluators_for_personas([trader], _make_event())
        ev = evaluators["test_trader"]
        assert ev.spec.state_atoms == ["cash", "nav"]
        assert ev.spec.utility_weights == {"nav_growth": 2.0}

    def test_empty_spec_falls_back_to_default(self):
        trader = _make_trader_persona()
        trader.self_model = {
            "state_atoms": ["TOTALLY_FAKE"],
            "utility_weights": {"ALSO_FAKE": 1.0},
            "render_sections": ["FAKE"],
        }
        evaluators = build_evaluators_for_personas([trader], _make_event())
        ev = evaluators["test_trader"]
        # Should have fallen back to DEFAULT_SELF_MODEL since everything was stripped
        assert len(ev.spec.state_atoms) > 5
        assert "nav_growth" in ev.spec.utility_weights


# ────────────────────── Update lifecycle ──────────────────────


class TestUpdateLifecycle:
    def test_initial_nav_matches_sandbox(self):
        trader = _make_trader_persona(capital=2_000_000)
        ev = build_evaluators_for_personas([trader], _make_event())["test_trader"]
        # initial_nav = cash + holdings = capital + prob * size * capital
        # default: 2e6 + 0.5 * 0.2 * 2e6 = 2.2e6
        assert ev.state["initial_nav"] == pytest.approx(2.2e6, rel=0.01)

    def test_update_writes_back_trade_result(self):
        trader = _make_trader_persona()
        ev = build_evaluators_for_personas([trader], _make_event())["test_trader"]
        ctx = _round_ctx(round_idx=0)
        tr = _trade(side="buy", qty=0.15, nav=1.2e6, cash=8.5e5, holdings=3.5e5)
        ev.update(ctx, tr)
        assert ev.state["nav"] == pytest.approx(1.2e6, rel=0.01)
        assert ev.state["cash"] == pytest.approx(8.5e5, rel=0.01)
        assert ev.state["holdings_value"] == pytest.approx(3.5e5, rel=0.01)

    def test_pnl_percent_computed_from_initial(self):
        trader = _make_trader_persona()
        ev = build_evaluators_for_personas([trader], _make_event())["test_trader"]
        initial = ev.state["initial_nav"]
        tr = _trade(nav=initial * 1.10)
        ev.update(_round_ctx(round_idx=0), tr)
        assert ev.state["unrealized_pnl_pct"] == pytest.approx(10.0, abs=0.01)

    def test_drawdown_tracked_across_rounds(self):
        trader = _make_trader_persona()
        ev = build_evaluators_for_personas([trader], _make_event())["test_trader"]
        initial = ev.state["initial_nav"]
        # R0: buy and go up
        ev.update(_round_ctx(round_idx=0), _trade(nav=initial * 1.15))
        # R1: drop to -5%
        ev.update(_round_ctx(round_idx=1, round_hours=24), _trade(nav=initial * 0.95))
        # Drawdown = (peak - nav) / peak = (1.15 - 0.95) / 1.15 ≈ 17.4%
        assert ev.state["max_drawdown_pct"] == pytest.approx(17.4, abs=0.5)


class TestStepUtilityIdempotence:
    def test_second_call_same_round_returns_cached(self):
        trader = _make_trader_persona()
        ev = build_evaluators_for_personas([trader], _make_event())["test_trader"]
        ev.update(_round_ctx(), _trade(nav=1.1e6))
        t1, d1, _ = ev.step_utility()
        t2, d2, _ = ev.step_utility()
        assert t1 == t2
        assert d1 == d2  # no double-mutation

    def test_delta_correct_across_rounds(self):
        trader = _make_trader_persona()
        ev = build_evaluators_for_personas([trader], _make_event())["test_trader"]
        ev.update(_round_ctx(round_idx=0), _trade(nav=ev.state["initial_nav"] * 1.05))
        t0, d0, _ = ev.step_utility()
        ev.update(_round_ctx(round_idx=1, round_hours=24), _trade(nav=ev.state["initial_nav"] * 0.95))
        t1, d1, _ = ev.step_utility()
        assert t1 < t0
        assert d1 < 0
        # Second round delta should equal t1 - t0
        assert d1 == pytest.approx(t1 - t0, abs=0.01)


# ────────────────────── Peer selection ──────────────────────


class TestPeerSelection:
    def test_topk_limit_respected(self):
        traders = [_make_trader_persona(f"pm_{i}") for i in range(5)]
        evaluators = build_evaluators_for_personas(traders, _make_event())
        # Give each a unique unrealized_pnl_pct
        for i, pid in enumerate(evaluators):
            evaluators[pid].state["unrealized_pnl_pct"] = i * 2.0
        focal = evaluators["pm_0"]
        focal.spec.peer_watchlist_filter = {
            "role_match": ["pm_*"],
            "top_k": 3,
            "sort_by": "nav_growth_desc",
        }
        peers = focal.select_peers(evaluators)
        assert len(peers) == 3
        # Sorted descending by unrealized_pnl_pct — best (+8) first
        assert peers[0]["unrealized_pnl_pct"] >= peers[1]["unrealized_pnl_pct"]
        assert peers[1]["unrealized_pnl_pct"] >= peers[2]["unrealized_pnl_pct"]

    def test_self_excluded_from_watchlist(self):
        traders = [_make_trader_persona(f"pm_{i}") for i in range(3)]
        evaluators = build_evaluators_for_personas(traders, _make_event())
        focal = evaluators["pm_0"]
        focal.spec.peer_watchlist_filter = {
            "role_match": ["*"], "top_k": 10, "sort_by": "nav_growth_desc",
        }
        peers = focal.select_peers(evaluators)
        ids = [p["persona_id"] for p in peers]
        assert "pm_0" not in ids

    def test_role_match_filters_by_glob(self):
        retail = _make_trader_persona("retail_short_term")
        mf = _make_trader_persona("mutual_fund_active_pm")
        traders = [retail, mf]
        evaluators = build_evaluators_for_personas(traders, _make_event())
        focal = evaluators["mutual_fund_active_pm"]
        focal.spec.peer_watchlist_filter = {
            "role_match": ["mutual_fund_*"], "top_k": 5, "sort_by": "nav_growth_desc",
        }
        peers = focal.select_peers(evaluators)
        assert all("mutual_fund" in p["persona_id"] for p in peers)

    def test_empty_filter_returns_empty_list(self):
        trader = _make_trader_persona()
        ev = build_evaluators_for_personas([trader], _make_event())["test_trader"]
        ev.spec.peer_watchlist_filter = {}
        assert ev.select_peers({"test_trader": ev}) == []


# ────────────────────── Render prompt ──────────────────────


class TestRenderPrompt:
    def test_compact_format_contains_core_fields(self):
        """Render prompt is a single dense line with financial / action /
        emotional / utility blocks. Long markdown sections were
        deliberately replaced with this format to protect OASIS's
        twhin BERT embedding path from quadratic blow-up on long posts."""
        trader = _make_trader_persona()
        ev = build_evaluators_for_personas([trader], _make_event())["test_trader"]
        ev.update(_round_ctx(), _trade(nav=1.1e6))
        out = ev.render_prompt([])
        # Single header line
        assert out.startswith("# 你的处境:")
        # Financial / outcome / emotional / utility all present
        assert "持仓净值" in out
        assert "上轮" in out
        assert "信心" in out
        assert "U=" in out
        # Compact = fewer than 300 chars total
        assert len(out) < 300, f"render exceeded budget: {len(out)} chars"

    def test_utility_delta_shows_across_rounds(self):
        trader = _make_trader_persona()
        ev = build_evaluators_for_personas([trader], _make_event())["test_trader"]
        initial = ev.state["initial_nav"]
        # R0: +10%
        ev.update(_round_ctx(round_idx=0), _trade(nav=initial * 1.10))
        out_r0 = ev.render_prompt([])
        assert "Δ+" in out_r0  # positive delta shown
        # R1: drop to -5% — delta should be negative now
        ev.update(_round_ctx(round_idx=1, round_hours=24), _trade(nav=initial * 0.95))
        out_r1 = ev.render_prompt([])
        assert "Δ-" in out_r1  # negative delta shown

    def test_peer_block_appears_when_peers_available(self):
        traders = [_make_trader_persona(f"pm_{i}") for i in range(3)]
        evaluators = build_evaluators_for_personas(traders, _make_event())
        focal = evaluators["pm_0"]
        focal.spec = SelfModelSpec.from_dict({
            "state_atoms": list(focal.state.keys()),
            "utility_weights": {"nav_growth": 1.0},
            "render_sections": ["financial_snapshot", "peer_watchlist"],
            "peer_watchlist_filter": {"role_match": ["pm_*"], "top_k": 2},
        })
        peers = focal.select_peers(evaluators)
        out = focal.render_prompt(peers)
        assert "同业:" in out


# ────────────────────── Snapshots ──────────────────────


class TestSnapshots:
    def test_public_snapshot_excludes_internal_anchors(self):
        trader = _make_trader_persona()
        ev = build_evaluators_for_personas([trader], _make_event())["test_trader"]
        snap = ev.state_public_snapshot()
        # initial_nav / peak_nav are observable_by_self=False
        assert "initial_nav" not in snap
        assert "peak_nav" not in snap
        assert "nav" in snap
        assert "cash" in snap

    def test_state_labels_match_public_snapshot_keys(self):
        trader = _make_trader_persona()
        ev = build_evaluators_for_personas([trader], _make_event())["test_trader"]
        snap = ev.state_public_snapshot()
        labels = ev.state_labels()
        assert set(labels.keys()) == set(snap.keys())
