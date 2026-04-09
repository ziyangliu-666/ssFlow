"""Tests for the Entity State Sandbox engine integration."""

from ssflow.entity import (
    Entity,
    EntityGraph,
    EntityState,
    ResourceFlow,
    Threshold,
    ThresholdEffect,
    compile_condition,
)
from ssflow.entity_engine import (
    collect_threshold_events,
    evaluate_thresholds,
    execute_resource_flows,
    record_entity_snapshots,
    render_situation,
    update_price_derived_state,
)
from ssflow.event_bus import ListSink


def _make_byd_graph() -> EntityGraph:
    """Build a minimal BYD scenario graph for testing.

    5 entities: BYD (company), CATL (supplier), retail (trader_class),
    institutional (trader_class), market_env (other).
    """
    g = EntityGraph(topic="BYD Q1 2026")

    g.add_entity(Entity(
        id="byd_corp", display_name="比亚迪", entity_type="company",
        state=EntityState(variables={
            "inventory_months": 2.5,
            "cash_bn": 50.0,
            "capacity_utilization": 0.78,
            "stock_price": 218.50,
            "price_change_pct": 0.0,
        }),
        state_labels={
            "inventory_months": "库存(月)",
            "cash_bn": "现金(亿元)",
            "capacity_utilization": "产能利用率",
            "stock_price": "股价",
            "price_change_pct": "涨跌幅(%)",
        },
        persona_id="company_ir",
    ))

    g.add_entity(Entity(
        id="catl_supply", display_name="宁德时代", entity_type="supplier",
        state=EntityState(variables={
            "inventory_months": 1.5,
            "byd_revenue_share": 0.30,
        }),
        state_labels={
            "inventory_months": "库存(月)",
            "byd_revenue_share": "比亚迪营收占比",
        },
    ))

    g.add_entity(Entity(
        id="retail_class", display_name="散户", entity_type="trader_class",
        state=EntityState(variables={
            "avg_position_pct": 0.45,
            "margin_utilization": 0.12,
        }),
        persona_id="retail_short_term_chaser",
    ))

    g.add_entity(Entity(
        id="institutional_class", display_name="机构", entity_type="trader_class",
        state=EntityState(variables={
            "avg_position_pct": 0.30,
            "max_position_pct": 0.50,
        }),
        persona_id="quant_fund_class",
    ))

    g.add_entity(Entity(
        id="market_env", display_name="市场环境", entity_type="other",
        state=EntityState(variables={
            "sector_avg_inventory": 2.0,
            "upstream_price_index": 100.0,
            "peer_stock_avg_change": 0.0,
        }),
    ))

    # Resource flows
    g.add_flow(ResourceFlow(
        id="catl_to_byd", source_id="catl_supply", target_id="byd_corp",
        resource_type="电池", rate_per_round=0.3,
        source_var="inventory_months", target_var="inventory_months",
        label="电池供应",
    ))

    # Thresholds
    g.register_threshold(Threshold(
        id="byd_inventory_warning",
        entity_id="byd_corp",
        description="库存积压超过 3 个月",
        condition=compile_condition("inventory_months > 3.0"),
        effect=ThresholdEffect(
            effect_type="inject_event",
            event_text="[公司公告] 比亚迪宣布终端优惠扩大,加速库存消化",
            event_author_id="byd_corp",
            event_content_type="company_announcement",
        ),
        cooldown_rounds=3,
    ))

    g.register_threshold(Threshold(
        id="byd_cash_warning",
        entity_id="byd_corp",
        description="现金储备低于 20 亿",
        condition=compile_condition("cash_bn < 20.0"),
        effect=ThresholdEffect(
            effect_type="mutate_state",
            state_mutations={"capacity_utilization": 0.60},
        ),
        cooldown_rounds=5,
    ))

    return g


# ─────────────────────── Resource Flow Execution ───────────────────────


class TestExecuteResourceFlows:
    def test_flows_transfer_resources(self):
        g = _make_byd_graph()
        sink = ListSink()
        results = execute_resource_flows(g, round_idx=0, event_sink=sink, simulation_id="test")

        assert len(results) == 1
        r = results[0]
        assert r.flow_id == "catl_to_byd"
        assert r.amount == 0.3
        assert not r.skipped

        # State mutations
        assert g.entities["catl_supply"].state.get("inventory_months") == 1.2
        assert g.entities["byd_corp"].state.get("inventory_months") == 2.8

        # Event emitted
        flow_events = [e for e in sink.events if e["type"] == "resource_flow_executed"]
        assert len(flow_events) == 1
        assert flow_events[0]["resource_type"] == "电池"

    def test_multiple_rounds_accumulate(self):
        g = _make_byd_graph()
        for i in range(3):
            execute_resource_flows(g, round_idx=i)

        # 3 rounds × 0.3 = 0.9 transferred from CATL (1.5 - 0.9 = 0.6)
        assert abs(g.entities["catl_supply"].state.get("inventory_months") - 0.6) < 0.01
        # BYD: 2.5 + 0.9 = 3.4
        assert abs(g.entities["byd_corp"].state.get("inventory_months") - 3.4) < 0.01


# ─────────────────────── Threshold Evaluation ───────────────────────


class TestEvaluateThresholds:
    def test_threshold_fires_when_condition_met(self):
        g = _make_byd_graph()
        g.entities["byd_corp"].state.set("inventory_months", 3.5)

        sink = ListSink()
        fires = evaluate_thresholds(g, round_idx=0, event_sink=sink, simulation_id="test")

        assert len(fires) == 1
        assert fires[0].threshold_id == "byd_inventory_warning"
        assert fires[0].effect.effect_type == "inject_event"

        # Event emitted
        t_events = [e for e in sink.events if e["type"] == "threshold_fired"]
        assert len(t_events) == 1

    def test_threshold_does_not_fire_when_condition_not_met(self):
        g = _make_byd_graph()
        # inventory_months = 2.5 (below 3.0), cash_bn = 50 (above 20)
        fires = evaluate_thresholds(g, round_idx=0)
        assert len(fires) == 0

    def test_mutate_state_effect(self):
        g = _make_byd_graph()
        g.entities["byd_corp"].state.set("cash_bn", 15.0)

        fires = evaluate_thresholds(g, round_idx=0)

        assert len(fires) == 1
        assert fires[0].threshold_id == "byd_cash_warning"
        # capacity_utilization should have been mutated to 0.60
        assert g.entities["byd_corp"].state.get("capacity_utilization") == 0.60

    def test_cooldown_prevents_refire(self):
        g = _make_byd_graph()
        g.entities["byd_corp"].state.set("inventory_months", 3.5)

        fires_r0 = evaluate_thresholds(g, round_idx=0)
        assert len(fires_r0) == 1

        fires_r1 = evaluate_thresholds(g, round_idx=1)
        assert len(fires_r1) == 0  # cooldown = 3

        fires_r3 = evaluate_thresholds(g, round_idx=3)
        assert len(fires_r3) == 1  # cooldown expired


# ─────────────────────── Threshold Event Collection ───────────────────────


class TestCollectThresholdEvents:
    def test_collects_inject_event_effects(self):
        g = _make_byd_graph()
        g.entities["byd_corp"].state.set("inventory_months", 3.5)

        fires = evaluate_thresholds(g, round_idx=0)
        events = collect_threshold_events(fires, round_idx=0)

        assert len(events) == 1
        assert "比亚迪" in events[0].text
        assert events[0].content_type == "company_announcement"

    def test_skips_non_event_effects(self):
        g = _make_byd_graph()
        g.entities["byd_corp"].state.set("cash_bn", 15.0)

        fires = evaluate_thresholds(g, round_idx=0)
        events = collect_threshold_events(fires, round_idx=0)

        assert len(events) == 0  # mutate_state, not inject_event


# ─────────────────────── State Recording ───────────────────────


class TestRecordEntitySnapshots:
    def test_records_all_entities(self):
        g = _make_byd_graph()
        sink = ListSink()
        record_entity_snapshots(g, round_idx=0, event_sink=sink, simulation_id="test")

        for entity in g.entities.values():
            assert len(entity.history) == 1

        state_events = [e for e in sink.events if e["type"] == "entity_state_updated"]
        assert len(state_events) == len(g.entities)


# ─────────────────────── Situation Rendering ───────────────────────


class TestRenderSituation:
    def test_includes_state_variables(self):
        g = _make_byd_graph()
        situation = render_situation(g.entities["byd_corp"], g, round_idx=0)

        assert "库存(月)" in situation
        assert "2.50" in situation
        assert "处境" in situation

    def test_includes_upstream_info(self):
        g = _make_byd_graph()
        situation = render_situation(g.entities["byd_corp"], g, round_idx=0)

        assert "宁德时代" in situation
        assert "电池" in situation

    def test_includes_recent_fires(self):
        g = _make_byd_graph()
        g.entities["byd_corp"].state.set("inventory_months", 3.5)
        evaluate_thresholds(g, round_idx=2)

        situation = render_situation(g.entities["byd_corp"], g, round_idx=3)
        assert "库存积压" in situation
        assert "[R2]" in situation


# ─────────────────────── Price-Derived State ───────────────────────


class TestUpdatePriceDerivedState:
    def test_updates_stock_price(self):
        g = _make_byd_graph()
        update_price_derived_state(g, new_price=200.0, initial_price=218.50)

        assert g.entities["byd_corp"].state.get("stock_price") == 200.0

    def test_updates_price_change_pct(self):
        g = _make_byd_graph()
        update_price_derived_state(g, new_price=200.0, initial_price=218.50)

        pct = g.entities["byd_corp"].state.get("price_change_pct")
        expected = (200.0 / 218.50 - 1.0) * 100
        assert abs(pct - expected) < 0.01


# ─────────────────────── Full BYD Scenario ───────────────────────


class TestBYDScenarioIntegration:
    def test_3_round_scenario(self):
        """Simulate 3 rounds of resource flows + thresholds on the BYD graph."""
        g = _make_byd_graph()
        sink = ListSink()

        for round_idx in range(5):
            # Execute resource flows
            execute_resource_flows(g, round_idx, event_sink=sink, simulation_id="demo")

            # Evaluate thresholds
            fires = evaluate_thresholds(g, round_idx, event_sink=sink, simulation_id="demo")

            # Record snapshots
            record_entity_snapshots(g, round_idx, event_sink=sink, simulation_id="demo")

        # After 5 rounds: CATL sent 5×0.3 = 1.5 to BYD
        # CATL: 1.5 - 1.5 = 0.0
        # BYD: 2.5 + 1.5 = 4.0
        assert abs(g.entities["catl_supply"].state.get("inventory_months") - 0.0) < 0.01
        assert abs(g.entities["byd_corp"].state.get("inventory_months") - 4.0) < 0.01

        # Inventory threshold should have fired when BYD hit 3.0+ (around round 2)
        threshold_events = [e for e in sink.events if e["type"] == "threshold_fired"]
        assert len(threshold_events) >= 1
        assert any("库存" in e["description"] for e in threshold_events)

        # All entities should have 5 history entries
        for entity in g.entities.values():
            assert len(entity.history) == 5
