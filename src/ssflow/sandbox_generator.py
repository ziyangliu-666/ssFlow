"""Sandbox generator — build an EntityGraph from a topic string.

Pipeline:
  1. Pick template by market
  2. LLM Call #1: identify real entities for each archetype slot
  3. Enrich with real market data (price, ADV)
  4. LLM Call #2: tune state values, flows, thresholds for this specific topic
  5. Compile thresholds from natural-language conditions to Python callables
  6. Assemble EntityGraph with TriggerProbe validation

Cost: 2 LLM calls at ~1100 tokens total ≈ $0.001.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .entity import (
    Entity,
    EntityGraph,
    EntityState,
    ResourceFlow,
    Threshold,
    ThresholdEffect,
    compile_condition,
)
from .llm_client import chat_json_sync
from .sandbox_templates import get_template

log = logging.getLogger(__name__)


# ─────────────────────── LLM Prompts ───────────────────────

_IDENTIFY_SYSTEM = """\
你是一个金融市场分析师。给定一个市场事件主题和一组实体原型 slot，你需要：
1. 为每个 slot 指定一个具体的真实世界实体（公司名/组织名/群体名）
2. 估计每个实体的初始状态数值（用你的训练数据中的公开信息）
3. 为"其他/市场环境"slot 估计几个行业聚合指标

输出严格 JSON 格式，不要包含其他文字。"""

_IDENTIFY_USER_TEMPLATE = """\
事件主题: {topic}
市场: {market}

需要填充的实体 slot:
{slots_description}

请为每个 slot 输出:
{{
  "entities": {{
    "<slot_name>": {{
      "real_name": "具体名称",
      "state_overrides": {{"var_name": value, ...}}
    }}
  }}
}}

只覆盖你有信心的数值，其他保持默认。"""

_TUNE_SYSTEM = """\
你是一个金融市场模拟配置师。给定事件主题、已识别的实体、模板中的默认流转和阈值，你需要：
1. 调整资源流转的 rate_per_round（根据实际规模）
2. 调整阈值条件的数值（根据行业实际情况）
3. 可以新增最多 2 个与此事件特别相关的阈值

输出严格 JSON 格式。"""

_TUNE_USER_TEMPLATE = """\
事件主题: {topic}

已识别实体:
{entities_json}

默认资源流转:
{flows_json}

默认阈值:
{thresholds_json}

请输出:
{{
  "flow_overrides": [
    {{"flow_index": 0, "rate_per_round": <new_rate>}}
  ],
  "threshold_overrides": [
    {{"threshold_index": 0, "condition": "<new_condition>", "description": "<new_desc>"}}
  ],
  "new_thresholds": [
    {{
      "entity_slot": "<slot>",
      "condition": "<var> <op> <val>",
      "description": "中文描述",
      "effect_type": "inject_event",
      "event_text": "具体事件文本"
    }}
  ]
}}

flow_overrides 和 threshold_overrides 可以为空数组。new_thresholds 最多 2 个。"""


# ─────────────────────── Generator ───────────────────────


def generate_sandbox(
    topic: str,
    market: str = "ashare",
    current_price: float = 0.0,
) -> EntityGraph:
    """Generate a complete EntityGraph from a topic string.

    Uses 2 LLM calls + template + optional market data.
    Returns a fully validated EntityGraph ready for run_simulation().
    """
    template = get_template(market)
    log.info("Sandbox generator: topic=%r, market=%s, template=%s",
             topic, market, template["id"])

    # ── Step 1: LLM identifies real entities ──
    slots_desc = _format_slots(template)
    _raw = chat_json_sync([
        {"role": "system", "content": _IDENTIFY_SYSTEM},
        {"role": "user", "content": _IDENTIFY_USER_TEMPLATE.format(
            topic=topic, market=market, slots_description=slots_desc,
        )},
    ])
    identify_response = _raw.parsed if hasattr(_raw, 'parsed') else _raw
    if not isinstance(identify_response, dict):
        identify_response = {}

    entity_map = identify_response.get("entities", {})
    log.info("Sandbox generator: identified %d entities", len(entity_map))

    # ── Step 2: Build entities from template + LLM overrides ──
    graph = EntityGraph(
        topic=topic,
        generated_at=datetime.now(timezone.utc).isoformat(),
        template_used=template["id"],
    )

    slot_to_entity_id: dict[str, str] = {}

    for archetype in template["entity_archetypes"]:
        slot = archetype["slot"]
        llm_data = entity_map.get(slot, {})
        real_name = llm_data.get("real_name", archetype["display_name_hint"])
        state_overrides = llm_data.get("state_overrides", {})

        entity_id = slot  # use slot as ID for simplicity
        slot_to_entity_id[slot] = entity_id

        # Merge default state with LLM overrides
        state_vars = dict(archetype.get("default_state", {}))
        for k, v in state_overrides.items():
            if k in state_vars:
                try:
                    state_vars[k] = float(v)
                except (TypeError, ValueError):
                    pass

        # Fill stock price from market data if available
        if "stock_price" in state_vars and current_price > 0:
            state_vars["stock_price"] = current_price

        entity = Entity(
            id=entity_id,
            display_name=real_name,
            entity_type=archetype["entity_type"],
            state=EntityState(variables=state_vars),
            state_labels=dict(archetype.get("default_labels", {})),
            persona_id=archetype.get("persona_link"),
        )
        graph.add_entity(entity)

    # ── Step 3: Build default flows ──
    for i, flow_def in enumerate(template.get("default_flows", [])):
        src_slot = flow_def["source_slot"]
        tgt_slot = flow_def["target_slot"]
        if src_slot in slot_to_entity_id and tgt_slot in slot_to_entity_id:
            graph.add_flow(ResourceFlow(
                id=f"flow_{i}",
                source_id=slot_to_entity_id[src_slot],
                target_id=slot_to_entity_id[tgt_slot],
                resource_type=flow_def.get("resource_type", "resource"),
                rate_per_round=float(flow_def.get("rate_per_round", 1.0)),
                source_var=flow_def.get("source_var", ""),
                target_var=flow_def.get("target_var", ""),
                label=flow_def.get("label", ""),
            ))

    # ── Step 4: Build default thresholds ──
    for i, t_def in enumerate(template.get("default_thresholds", [])):
        entity_slot = t_def["entity_slot"]
        if entity_slot not in slot_to_entity_id:
            continue
        entity_id = slot_to_entity_id[entity_slot]
        entity = graph.entities[entity_id]

        # Build event text from template
        event_text = t_def.get("event_text_template", "")
        if event_text:
            event_text = event_text.replace("{display_name}", entity.display_name)

        effect = ThresholdEffect(
            effect_type=t_def.get("effect_type", "inject_event"),
            event_text=event_text,
            event_author_id=entity_id,
        )
        if t_def.get("state_mutations"):
            effect = ThresholdEffect(
                effect_type="mutate_state",
                state_mutations=dict(t_def["state_mutations"]),
            )

        try:
            condition = compile_condition(t_def["condition"])
            graph.register_threshold(Threshold(
                id=f"threshold_{i}",
                entity_id=entity_id,
                description=t_def.get("description", t_def["condition"]),
                condition=condition,
                effect=effect,
                cooldown_rounds=3,
            ))
        except ValueError as exc:
            log.warning("Skipping threshold %d: %s", i, exc)

    # ── Step 5: LLM tunes flows + thresholds ──
    try:
        tune_response = _tune_config(topic, graph, template, slot_to_entity_id)
        _apply_tune_overrides(graph, tune_response, template, slot_to_entity_id)
    except Exception as exc:
        log.warning("Sandbox tuning LLM call failed, using defaults: %s", exc)

    log.info(
        "Sandbox generator done: %d entities, %d flows, %d thresholds",
        len(graph.entities), len(graph.flows), len(graph.thresholds),
    )
    return graph


def _format_slots(template: dict) -> str:
    lines = []
    for arch in template["entity_archetypes"]:
        state_keys = list(arch.get("default_state", {}).keys())
        lines.append(
            f"- {arch['slot']} ({arch['entity_type']}): "
            f"{arch['display_name_hint']}. "
            f"状态变量: {', '.join(state_keys)}"
        )
    return "\n".join(lines)


def _tune_config(
    topic: str,
    graph: EntityGraph,
    template: dict,
    slot_to_entity_id: dict[str, str],
) -> dict:
    entities_json = json.dumps(
        {eid: {"name": e.display_name, "state": e.state.snapshot()}
         for eid, e in graph.entities.items()},
        ensure_ascii=False, indent=2,
    )
    flows_json = json.dumps(
        [{"index": i, "source": f.source_id, "target": f.target_id,
          "type": f.resource_type, "rate": f.rate_per_round}
         for i, f in enumerate(graph.flows)],
        ensure_ascii=False, indent=2,
    )
    thresholds_json = json.dumps(
        [{"index": i, "entity": t.entity_id, "description": t.description}
         for i, t in enumerate(graph.thresholds)],
        ensure_ascii=False, indent=2,
    )

    _raw = chat_json_sync([
        {"role": "system", "content": _TUNE_SYSTEM},
        {"role": "user", "content": _TUNE_USER_TEMPLATE.format(
            topic=topic,
            entities_json=entities_json,
            flows_json=flows_json,
            thresholds_json=thresholds_json,
        )},
    ])
    result = _raw.parsed if hasattr(_raw, 'parsed') else _raw
    return result if isinstance(result, dict) else {}


def _apply_tune_overrides(
    graph: EntityGraph,
    tune: dict,
    template: dict,
    slot_to_entity_id: dict[str, str],
) -> None:
    """Apply LLM-suggested overrides to flows and thresholds."""
    # Flow rate overrides
    for override in tune.get("flow_overrides", []):
        idx = override.get("flow_index")
        if idx is not None and 0 <= idx < len(graph.flows):
            try:
                graph.flows[idx].rate_per_round = float(override["rate_per_round"])
            except (KeyError, TypeError, ValueError):
                pass

    # Threshold condition overrides
    for override in tune.get("threshold_overrides", []):
        idx = override.get("threshold_index")
        if idx is not None and 0 <= idx < len(graph.thresholds):
            new_cond_str = override.get("condition")
            new_desc = override.get("description")
            if new_cond_str:
                try:
                    new_cond = compile_condition(new_cond_str)
                    entity = graph.entities[graph.thresholds[idx].entity_id]
                    from .entity import TriggerProbe
                    TriggerProbe.validate(new_cond, entity)
                    graph.thresholds[idx].condition = new_cond
                    if new_desc:
                        graph.thresholds[idx].description = new_desc
                except (ValueError, KeyError) as exc:
                    log.warning("Skipping threshold override %d: %s", idx, exc)

    # New thresholds from LLM
    for new_t in tune.get("new_thresholds", [])[:2]:  # max 2
        entity_slot = new_t.get("entity_slot", "")
        if entity_slot not in slot_to_entity_id:
            continue
        entity_id = slot_to_entity_id[entity_slot]
        entity = graph.entities.get(entity_id)
        if entity is None:
            continue

        cond_str = new_t.get("condition", "")
        if not cond_str:
            continue

        try:
            condition = compile_condition(cond_str)
            effect = ThresholdEffect(
                effect_type=new_t.get("effect_type", "inject_event"),
                event_text=new_t.get("event_text", ""),
                event_author_id=entity_id,
            )
            new_id = f"llm_threshold_{len(graph.thresholds)}"
            graph.register_threshold(Threshold(
                id=new_id,
                entity_id=entity_id,
                description=new_t.get("description", cond_str),
                condition=condition,
                effect=effect,
                cooldown_rounds=3,
            ))
        except ValueError as exc:
            log.warning("Skipping LLM-suggested threshold: %s", exc)


# ─────────────────────── Standalone builder (no LLM) ───────────────────────


def build_from_template(
    topic: str,
    market: str = "ashare",
    current_price: float = 0.0,
    entity_overrides: dict[str, dict[str, Any]] | None = None,
) -> EntityGraph:
    """Build an EntityGraph from template defaults only (no LLM calls).

    Useful for testing, offline mode, and manual configuration.
    entity_overrides: {slot_name: {"display_name": ..., "state": {var: val}}}
    """
    template = get_template(market)
    entity_overrides = entity_overrides or {}

    graph = EntityGraph(
        topic=topic,
        generated_at=datetime.now(timezone.utc).isoformat(),
        template_used=template["id"],
    )

    slot_to_entity_id: dict[str, str] = {}

    for archetype in template["entity_archetypes"]:
        slot = archetype["slot"]
        entity_id = slot
        slot_to_entity_id[slot] = entity_id

        overrides = entity_overrides.get(slot, {})
        display_name = overrides.get("display_name", archetype["display_name_hint"])

        state_vars = dict(archetype.get("default_state", {}))
        for k, v in overrides.get("state", {}).items():
            state_vars[k] = float(v)

        if "stock_price" in state_vars and current_price > 0:
            state_vars["stock_price"] = current_price

        entity = Entity(
            id=entity_id,
            display_name=display_name,
            entity_type=archetype["entity_type"],
            state=EntityState(variables=state_vars),
            state_labels=dict(archetype.get("default_labels", {})),
            persona_id=archetype.get("persona_link"),
        )
        graph.add_entity(entity)

    for i, flow_def in enumerate(template.get("default_flows", [])):
        src_slot = flow_def["source_slot"]
        tgt_slot = flow_def["target_slot"]
        if src_slot in slot_to_entity_id and tgt_slot in slot_to_entity_id:
            graph.add_flow(ResourceFlow(
                id=f"flow_{i}",
                source_id=slot_to_entity_id[src_slot],
                target_id=slot_to_entity_id[tgt_slot],
                resource_type=flow_def.get("resource_type", "resource"),
                rate_per_round=float(flow_def.get("rate_per_round", 1.0)),
                source_var=flow_def.get("source_var", ""),
                target_var=flow_def.get("target_var", ""),
                label=flow_def.get("label", ""),
            ))

    for i, t_def in enumerate(template.get("default_thresholds", [])):
        entity_slot = t_def["entity_slot"]
        if entity_slot not in slot_to_entity_id:
            continue
        entity_id = slot_to_entity_id[entity_slot]
        entity = graph.entities[entity_id]

        event_text = t_def.get("event_text_template", "")
        if event_text:
            event_text = event_text.replace("{display_name}", entity.display_name)

        if t_def.get("state_mutations"):
            effect = ThresholdEffect(
                effect_type="mutate_state",
                state_mutations=dict(t_def["state_mutations"]),
            )
        else:
            effect = ThresholdEffect(
                effect_type=t_def.get("effect_type", "inject_event"),
                event_text=event_text,
                event_author_id=entity_id,
            )

        try:
            condition = compile_condition(t_def["condition"])
            graph.register_threshold(Threshold(
                id=f"threshold_{i}",
                entity_id=entity_id,
                description=t_def.get("description", t_def["condition"]),
                condition=condition,
                effect=effect,
                cooldown_rounds=3,
            ))
        except ValueError as exc:
            log.warning("Skipping threshold %d: %s", i, exc)

    return graph


__all__ = [
    "build_from_template",
    "generate_sandbox",
]
