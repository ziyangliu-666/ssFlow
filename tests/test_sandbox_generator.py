"""Tests for the sandbox generator (template-only mode, no LLM)."""

from ssflow.sandbox_generator import build_from_template
from ssflow.sandbox_templates import ASHARE_EQUITY_TEMPLATE


class TestBuildFromTemplate:
    def test_default_template_produces_valid_graph(self):
        g = build_from_template("BYD Q1 test", market="ashare", current_price=218.50)

        assert len(g.entities) == 5
        assert g.topic == "BYD Q1 test"
        assert g.template_used == "ashare_equity"

        # All entity types valid
        for e in g.entities.values():
            assert e.entity_type in {"company", "supplier", "trader_class", "other"}

    def test_has_flows(self):
        g = build_from_template("test", market="ashare")
        assert len(g.flows) >= 1
        assert g.flows[0].resource_type == "核心零部件"

    def test_has_thresholds(self):
        g = build_from_template("test", market="ashare")
        assert len(g.thresholds) >= 3  # template has 4 default thresholds

    def test_stock_price_filled(self):
        g = build_from_template("test", market="ashare", current_price=300.0)
        company = g.entities["subject_company"]
        assert company.state.get("stock_price") == 300.0

    def test_entity_overrides(self):
        g = build_from_template(
            "BYD Q1",
            market="ashare",
            entity_overrides={
                "subject_company": {
                    "display_name": "比亚迪",
                    "state": {"inventory_months": 4.5, "cash_bn": 80.0},
                },
                "key_supplier": {
                    "display_name": "宁德时代",
                    "state": {"subject_revenue_share": 0.30},
                },
            },
        )

        assert g.entities["subject_company"].display_name == "比亚迪"
        assert g.entities["subject_company"].state.get("inventory_months") == 4.5
        assert g.entities["subject_company"].state.get("cash_bn") == 80.0
        assert g.entities["key_supplier"].display_name == "宁德时代"
        assert g.entities["key_supplier"].state.get("subject_revenue_share") == 0.30

    def test_persona_links(self):
        g = build_from_template("test", market="ashare")
        assert g.entities["subject_company"].persona_id == "company_ir"
        assert g.entities["retail_class"].persona_id == "retail_short_term_chaser"
        assert g.entities["key_supplier"].persona_id is None
        assert g.entities["market_environment"].persona_id is None

    def test_serializable(self):
        g = build_from_template("test", market="ashare")
        s = g.to_serializable()
        assert "entities" in s
        assert "flows" in s
        assert "thresholds" in s

    def test_threshold_fires_on_high_inventory(self):
        """End-to-end: create graph with high inventory, threshold should fire."""
        from ssflow.entity_engine import evaluate_thresholds

        g = build_from_template(
            "BYD Q1",
            market="ashare",
            entity_overrides={
                "subject_company": {
                    "display_name": "比亚迪",
                    "state": {"inventory_months": 5.0},
                },
            },
        )

        fires = evaluate_thresholds(g, round_idx=0)
        inv_fires = [f for f in fires if "库存" in f.description]
        assert len(inv_fires) == 1

    def test_threshold_fires_on_price_crash(self):
        """Price-derived threshold: 跌超8% triggers 监管问询."""
        from ssflow.entity_engine import evaluate_thresholds, update_price_derived_state

        g = build_from_template("test", market="ashare", current_price=100.0)
        update_price_derived_state(g, new_price=90.0, initial_price=100.0)

        fires = evaluate_thresholds(g, round_idx=0)
        reg_fires = [f for f in fires if "监管" in f.description]
        assert len(reg_fires) == 1

    def test_unknown_market_falls_back_to_ashare(self):
        g = build_from_template("test", market="crypto_defi")
        assert len(g.entities) == 5  # same as ashare

    def test_5_round_integration(self):
        """Run 5 rounds of flows + thresholds to verify the full pipeline."""
        from ssflow.entity_engine import (
            evaluate_thresholds,
            execute_resource_flows,
            record_entity_snapshots,
        )
        from ssflow.event_bus import ListSink

        g = build_from_template(
            "BYD Q1",
            market="ashare",
            current_price=218.50,
            entity_overrides={
                "subject_company": {
                    "display_name": "比亚迪",
                    "state": {"inventory_months": 2.5},
                },
                "key_supplier": {
                    "display_name": "宁德时代",
                },
            },
        )

        sink = ListSink()
        for r in range(5):
            execute_resource_flows(g, r, sink, "test")
            evaluate_thresholds(g, r, sink, "test")
            record_entity_snapshots(g, r, sink, "test")

        # Verify state evolved
        byd = g.entities["subject_company"]
        assert len(byd.history) == 5
        # inventory should have grown (CATL delivers 0.3/round)
        assert byd.state.get("inventory_months") > 2.5

        # At least one threshold should have fired
        fired = [e for e in sink.events if e["type"] == "threshold_fired"]
        # inventory > 3.0 fires around round 2
        assert len(fired) >= 1
