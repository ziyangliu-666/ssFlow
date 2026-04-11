"""Tests for the unified world model (Phase 1)."""

import pytest

from ssflow.world import Flow, SimAgent, SimGraph, WorldState


class TestSimAgent:
    def test_state_access(self):
        a = SimAgent(id="a", kind="trader", display_name="A",
                     state={"cash": 100.0, "pos": 0.5})
        assert a.get("cash") == 100.0
        assert a.get("missing") == 0.0
        a.set("cash", 50.0)
        assert a.get("cash") == 50.0

    def test_snapshot(self):
        a = SimAgent(id="a", kind="company", display_name="A",
                     state={"revenue": 120e9})
        snap = a.snapshot()
        assert snap == {"revenue": 120e9}
        # Snapshot is a copy, not a reference
        snap["revenue"] = 0
        assert a.get("revenue") == 120e9

    def test_record_round(self):
        a = SimAgent(id="a", kind="trader", display_name="A",
                     state={"pos": 0.5})
        a.record_round()
        a.set("pos", 0.3)
        a.record_round()
        assert len(a.history) == 2
        assert a.history[0] == {"pos": 0.5}
        assert a.history[1] == {"pos": 0.3}

    def test_public_snapshot_all(self):
        a = SimAgent(id="a", kind="company", display_name="A",
                     state={"rev": 100, "secret": 42})
        # No public_state_keys → all state is public
        assert a.public_snapshot() == {"rev": 100, "secret": 42}

    def test_public_snapshot_filtered(self):
        a = SimAgent(id="a", kind="company", display_name="A",
                     state={"rev": 100, "secret": 42},
                     public_state_keys={"rev"})
        assert a.public_snapshot() == {"rev": 100}


class TestFlow:
    def _pair(self):
        src = SimAgent(id="s", kind="supplier", display_name="S",
                       state={"inv": 10.0})
        tgt = SimAgent(id="t", kind="company", display_name="T",
                       state={"inv": 5.0})
        return src, tgt

    def test_basic_transfer(self):
        src, tgt = self._pair()
        f = Flow(id="f1", source_id="s", target_id="t",
                 resource_type="parts", rate_per_round=3.0,
                 source_var="inv", target_var="inv")
        amount = f.execute(src, tgt)
        assert amount == 3.0
        assert src.get("inv") == 7.0
        assert tgt.get("inv") == 8.0

    def test_capped_by_source(self):
        src, tgt = self._pair()
        f = Flow(id="f1", source_id="s", target_id="t",
                 resource_type="parts", rate_per_round=15.0,
                 source_var="inv", target_var="inv")
        amount = f.execute(src, tgt)
        assert amount == 10.0  # capped at source's 10
        assert src.get("inv") == 0.0
        assert tgt.get("inv") == 15.0

    def test_display_only(self):
        src, tgt = self._pair()
        f = Flow(id="f1", source_id="s", target_id="t",
                 resource_type="trust", rate_per_round=1.0)
        amount = f.execute(src, tgt)
        assert amount == 1.0
        assert src.get("inv") == 10.0  # unchanged


class TestSimGraph:
    def _make_graph(self):
        g = SimGraph(topic="test")
        g.add_agent(SimAgent(id="co", kind="company", display_name="Co",
                             state={"stock_price": 100.0, "inv": 5.0}))
        g.add_agent(SimAgent(id="sup", kind="supplier", display_name="Sup",
                             state={"inv": 10.0}))
        g.add_agent(SimAgent(id="t1", kind="trader", display_name="Trader",
                             state={"avg_position_pct": 0.4},
                             persona_id="retail_short"))
        return g

    def test_add_agent(self):
        g = self._make_graph()
        assert len(g.agents) == 3
        assert "co" in g.agents

    def test_trader_agents(self):
        g = self._make_graph()
        traders = g.trader_agents()
        assert len(traders) == 1
        assert traders[0].id == "t1"

    def test_agent_by_persona(self):
        g = self._make_graph()
        a = g.agent_by_persona("retail_short")
        assert a is not None
        assert a.id == "t1"
        assert g.agent_by_persona("nonexistent") is None

    def test_add_flow(self):
        g = self._make_graph()
        g.add_flow(Flow(id="f1", source_id="sup", target_id="co",
                        resource_type="parts", rate_per_round=2.0,
                        source_var="inv", target_var="inv"))
        assert len(g.flows) == 1

    def test_add_flow_rejects_missing(self):
        g = self._make_graph()
        with pytest.raises(ValueError, match="source.*not in graph"):
            g.add_flow(Flow(id="f1", source_id="missing", target_id="co",
                            resource_type="x", rate_per_round=1.0))

    def test_execute_flows(self):
        g = self._make_graph()
        g.add_flow(Flow(id="f1", source_id="sup", target_id="co",
                        resource_type="parts", rate_per_round=2.0,
                        source_var="inv", target_var="inv"))
        results = g.execute_flows()
        assert len(results) == 1
        assert results[0] == ("f1", 2.0)
        assert g.agents["sup"].get("inv") == 8.0
        assert g.agents["co"].get("inv") == 7.0

    def test_recompute_world_state(self):
        g = self._make_graph()
        g.recompute_world_state({"BYD": 101.5}, round_idx=3, time_context="T+1 AM")
        assert g.world.prices == {"BYD": 101.5}
        assert g.world.round_idx == 3
        assert g.world.time_context == "T+1 AM"
        assert "co" in g.world.agent_public_states

    def test_update_price_derived_state(self):
        g = self._make_graph()
        g.agents["co"].state["price_change_pct"] = 0.0
        g.agents["co"].state["margin_utilization"] = 0.65
        g.update_price_derived_state(new_price=90.0, initial_price=100.0)
        assert g.agents["co"].get("stock_price") == 90.0
        assert g.agents["co"].get("price_change_pct") == pytest.approx(-10.0)
        # margin should rise: 0.65 / 0.90 ≈ 0.722
        assert g.agents["co"].get("margin_utilization") == pytest.approx(0.722, abs=0.01)

    def test_serialization_roundtrip(self):
        g = self._make_graph()
        g.add_flow(Flow(id="f1", source_id="sup", target_id="co",
                        resource_type="parts", rate_per_round=2.0, label="supply"))
        data = g.to_serializable()
        g2 = SimGraph.from_serializable(data)
        assert len(g2.agents) == 3
        assert len(g2.flows) == 1
        assert g2.agents["co"].get("stock_price") == 100.0
        assert g2.agents["t1"].persona_id == "retail_short"
        assert g2.flows[0].label == "supply"


class TestSimGraphBuilder:
    def test_build_from_entity_graph(self):
        from ssflow.sandbox_generator import build_from_template
        from ssflow.event import Event
        from ssflow.persona import load_personas
        from ssflow.sim_graph_builder import build_sim_graph
        from pathlib import Path

        # Build entity graph from template
        eg = build_from_template("test event", market="ashare", current_price=100.0)

        # Create a minimal event
        event = Event(
            ticker="TEST", event_text="test", event_type="other",
            event_date="2026-01-01",
        )

        # Load personas
        personas = load_personas(Path("personas/ashare.yaml"))

        sg = build_sim_graph(personas, eg, event)

        # Should have all entity graph agents + uncovered trader personas
        assert len(sg.agents) >= len(eg.entities)
        # Should have flows from entity graph
        assert len(sg.flows) == len(eg.flows)
        # Trader agents should exist
        traders = sg.trader_agents()
        assert len(traders) > 0
        # Entity-linked personas should be covered
        for eid, entity in eg.entities.items():
            assert eid in sg.agents
            if entity.persona_id:
                assert sg.agents[eid].persona_id == entity.persona_id
