"""Tests for the Entity State Sandbox data structures."""

import pytest

from ssflow.entity import (
    Entity,
    EntityGraph,
    EntityState,
    ResourceFlow,
    Threshold,
    ThresholdEffect,
    TriggerProbe,
    compile_condition,
)


# ─────────────────────── EntityState ───────────────────────


class TestEntityState:
    def test_get_set_roundtrip(self):
        s = EntityState(variables={"cash": 100.0})
        assert s.get("cash") == 100.0
        s.set("cash", 50.0)
        assert s.get("cash") == 50.0

    def test_get_default(self):
        s = EntityState()
        assert s.get("missing") == 0.0
        assert s.get("missing", 99.0) == 99.0

    def test_snapshot_is_copy(self):
        s = EntityState(variables={"a": 1.0, "b": 2.0})
        snap = s.snapshot()
        snap["a"] = 999.0
        assert s.get("a") == 1.0  # original not mutated


# ─────────────────────── Entity ───────────────────────


class TestEntity:
    def test_record_round(self):
        e = Entity(
            id="test", display_name="Test", entity_type="company",
            state=EntityState(variables={"x": 1.0}),
        )
        e.record_round()
        e.state.set("x", 2.0)
        e.record_round()
        assert len(e.history) == 2
        assert e.history[0] == {"x": 1.0}
        assert e.history[1] == {"x": 2.0}

    def test_history_snapshots_are_independent(self):
        e = Entity(
            id="test", display_name="Test", entity_type="company",
            state=EntityState(variables={"x": 1.0}),
        )
        e.record_round()
        e.state.set("x", 999.0)
        assert e.history[0] == {"x": 1.0}


# ─────────────────────── ResourceFlow ───────────────────────


class TestResourceFlow:
    def _make_entities(self):
        src = Entity(
            id="src", display_name="Src", entity_type="supplier",
            state=EntityState(variables={"inventory": 10.0}),
        )
        tgt = Entity(
            id="tgt", display_name="Tgt", entity_type="company",
            state=EntityState(variables={"inventory": 5.0}),
        )
        return src, tgt

    def test_basic_transfer(self):
        src, tgt = self._make_entities()
        flow = ResourceFlow(
            id="f1", source_id="src", target_id="tgt",
            resource_type="parts", rate_per_round=3.0,
            source_var="inventory", target_var="inventory",
        )
        amount = flow.execute(src, tgt)
        assert amount == 3.0
        assert src.state.get("inventory") == 7.0
        assert tgt.state.get("inventory") == 8.0

    def test_capped_by_available(self):
        src, tgt = self._make_entities()
        flow = ResourceFlow(
            id="f1", source_id="src", target_id="tgt",
            resource_type="parts", rate_per_round=15.0,
            source_var="inventory", target_var="inventory",
        )
        amount = flow.execute(src, tgt)
        assert amount == 10.0  # capped at source's available
        assert src.state.get("inventory") == 0.0
        assert tgt.state.get("inventory") == 15.0

    def test_display_only_flow(self):
        """Flows without source_var/target_var don't mutate state."""
        src, tgt = self._make_entities()
        flow = ResourceFlow(
            id="f1", source_id="src", target_id="tgt",
            resource_type="trust", rate_per_round=1.0,
        )
        amount = flow.execute(src, tgt)
        assert amount == 1.0
        assert src.state.get("inventory") == 10.0  # unchanged
        assert tgt.state.get("inventory") == 5.0   # unchanged


# ─────────────────────── Threshold ───────────────────────


class TestThreshold:
    def _make_entity(self, inv: float = 4.0):
        return Entity(
            id="co", display_name="Co", entity_type="company",
            state=EntityState(variables={"inventory_months": inv}),
        )

    def test_fires_when_condition_met(self):
        e = self._make_entity(inv=4.0)
        t = Threshold(
            id="t1", entity_id="co", description="inv > 3",
            condition=compile_condition("inventory_months > 3.0"),
            effect=ThresholdEffect(effect_type="inject_event"),
        )
        assert t.should_fire(e, current_round=0) is True

    def test_does_not_fire_when_condition_not_met(self):
        e = self._make_entity(inv=2.0)
        t = Threshold(
            id="t1", entity_id="co", description="inv > 3",
            condition=compile_condition("inventory_months > 3.0"),
            effect=ThresholdEffect(effect_type="inject_event"),
        )
        assert t.should_fire(e, current_round=0) is False

    def test_cooldown_prevents_refire(self):
        e = self._make_entity(inv=4.0)
        t = Threshold(
            id="t1", entity_id="co", description="inv > 3",
            condition=compile_condition("inventory_months > 3.0"),
            effect=ThresholdEffect(effect_type="inject_event"),
            cooldown_rounds=3,
        )
        assert t.should_fire(e, current_round=0) is True
        t.last_fired_round = 0
        assert t.should_fire(e, current_round=1) is False
        assert t.should_fire(e, current_round=2) is False
        assert t.should_fire(e, current_round=3) is True


# ─────────────────────── TriggerProbe ───────────────────────


class TestTriggerProbe:
    def test_catches_typo(self):
        e = Entity(
            id="co", display_name="Co", entity_type="company",
            state=EntityState(variables={"inventory_months": 2.5}),
        )
        bad = compile_condition("inventoyr_months > 3.0")
        with pytest.raises(ValueError, match="inventoyr_months"):
            TriggerProbe.validate(bad, e)

    def test_suggests_close_match(self):
        e = Entity(
            id="co", display_name="Co", entity_type="company",
            state=EntityState(variables={"inventory_months": 2.5}),
        )
        bad = compile_condition("inventory_month > 3.0")
        with pytest.raises(ValueError, match="Did you mean 'inventory_months'"):
            TriggerProbe.validate(bad, e)

    def test_valid_condition_passes(self):
        e = Entity(
            id="co", display_name="Co", entity_type="company",
            state=EntityState(variables={"inventory_months": 2.5}),
        )
        good = compile_condition("inventory_months > 3.0")
        TriggerProbe.validate(good, e)  # should not raise


# ─────────────────────── compile_condition ───────────────────────


class TestCompileCondition:
    def test_greater_than(self):
        c = compile_condition("x > 3.0")
        e = Entity(id="e", display_name="E", entity_type="custom",
                    state=EntityState(variables={"x": 4.0}))
        assert c(e) is True

    def test_less_than(self):
        c = compile_condition("x < 3.0")
        e = Entity(id="e", display_name="E", entity_type="custom",
                    state=EntityState(variables={"x": 2.0}))
        assert c(e) is True

    def test_greater_equal(self):
        c = compile_condition("x >= 3.0")
        e = Entity(id="e", display_name="E", entity_type="custom",
                    state=EntityState(variables={"x": 3.0}))
        assert c(e) is True

    def test_equal(self):
        c = compile_condition("x == 0.0")
        e = Entity(id="e", display_name="E", entity_type="custom",
                    state=EntityState(variables={"x": 0.0}))
        assert c(e) is True

    def test_rejects_invalid_format(self):
        with pytest.raises(ValueError, match="Cannot compile"):
            compile_condition("x AND y")

    def test_rejects_code_injection(self):
        with pytest.raises(ValueError, match="Cannot compile"):
            compile_condition("__import__('os').system('rm -rf /')")


# ─────────────────────── EntityGraph ───────────────────────


class TestEntityGraph:
    def _make_graph(self):
        g = EntityGraph(topic="test")
        g.add_entity(Entity(
            id="a", display_name="A", entity_type="company",
            state=EntityState(variables={"inv": 10.0}),
        ))
        g.add_entity(Entity(
            id="b", display_name="B", entity_type="supplier",
            state=EntityState(variables={"inv": 20.0}),
        ))
        return g

    def test_add_flow_validates_endpoints(self):
        g = self._make_graph()
        g.add_flow(ResourceFlow(
            id="f1", source_id="b", target_id="a",
            resource_type="parts", rate_per_round=1.0,
        ))
        assert len(g.flows) == 1

    def test_add_flow_rejects_unknown_source(self):
        g = self._make_graph()
        with pytest.raises(ValueError, match="source 'unknown'"):
            g.add_flow(ResourceFlow(
                id="f1", source_id="unknown", target_id="a",
                resource_type="parts", rate_per_round=1.0,
            ))

    def test_add_flow_rejects_unknown_target(self):
        g = self._make_graph()
        with pytest.raises(ValueError, match="target 'unknown'"):
            g.add_flow(ResourceFlow(
                id="f1", source_id="a", target_id="unknown",
                resource_type="parts", rate_per_round=1.0,
            ))

    def test_register_threshold_validates_entity(self):
        g = self._make_graph()
        with pytest.raises(ValueError, match="entity 'unknown'"):
            g.register_threshold(Threshold(
                id="t1", entity_id="unknown", description="test",
                condition=lambda e: True,
                effect=ThresholdEffect(effect_type="inject_event"),
            ))

    def test_register_threshold_runs_trigger_probe(self):
        g = self._make_graph()
        bad = compile_condition("nonexistent > 3.0")
        with pytest.raises(ValueError, match="nonexistent"):
            g.register_threshold(Threshold(
                id="t1", entity_id="a", description="test",
                condition=bad,
                effect=ThresholdEffect(effect_type="inject_event"),
            ))

    def test_add_entity_rejects_invalid_type(self):
        g = EntityGraph()
        with pytest.raises(ValueError, match="entity_type"):
            g.add_entity(Entity(
                id="x", display_name="X", entity_type="invalid_type",
            ))

    def test_serializable_roundtrip(self):
        g = self._make_graph()
        g.add_flow(ResourceFlow(
            id="f1", source_id="b", target_id="a",
            resource_type="parts", rate_per_round=2.0, label="supply",
        ))
        g.register_threshold(Threshold(
            id="t1", entity_id="a", description="inv > 5",
            condition=compile_condition("inv > 5.0"),
            effect=ThresholdEffect(effect_type="inject_event"),
        ))
        s = g.to_serializable()
        assert len(s["entities"]) == 2
        assert len(s["flows"]) == 1
        assert len(s["thresholds"]) == 1
        assert s["flows"][0]["resource_type"] == "parts"
        assert s["thresholds"][0]["description"] == "inv > 5"
