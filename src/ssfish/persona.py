"""Persona dataclass + YAML loader.

A persona is the unit of variability in the simulation. Each persona has:
    - a stable id (used for cross-round memory)
    - an archetype label (e.g., "散户大妈", "短线游资")
    - a model name (which yourapi model backs this persona)
    - a voice prompt (free-form description that becomes the system prompt)
    - biases (numeric weights — used in aggregation, also surfaced to LLM)
    - knowledge (holdings, info sources, what they ignore)

The schema is intentionally flat YAML so a future open-source persona pack
contributor can drop in a new yaml without learning the codebase.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


SCHEMA_VERSION = 1


class PersonaSchemaError(ValueError):
    """Raised when a persona YAML file is malformed."""


@dataclass
class Persona:
    id: str
    archetype: str
    display_name: str
    model: str
    voice_prompt: str
    biases: dict[str, float] = field(default_factory=dict)
    knowledge: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    schema_version: int = SCHEMA_VERSION

    def system_prompt(self) -> str:
        """Render the persona as a system prompt for an LLM call."""
        bias_lines = "\n".join(f"  - {k}: {v}" for k, v in self.biases.items()) or "  (none specified)"
        knowledge_lines = []
        for key in ("holdings", "information_sources", "ignores"):
            value = self.knowledge.get(key)
            if value:
                if isinstance(value, list):
                    knowledge_lines.append(f"  - {key}: {', '.join(map(str, value))}")
                else:
                    knowledge_lines.append(f"  - {key}: {value}")
        knowledge_block = "\n".join(knowledge_lines) or "  (none specified)"

        return (
            f"You are simulating a Chinese A-share market participant.\n"
            f"\n"
            f"# 身份 / Identity\n"
            f"  - id: {self.id}\n"
            f"  - 类型 (archetype): {self.archetype}\n"
            f"  - 画像 (profile): {self.display_name}\n"
            f"\n"
            f"# 行为偏差 / Behavioral biases\n"
            f"{bias_lines}\n"
            f"\n"
            f"# 知识与信息源 / Knowledge & sources\n"
            f"{knowledge_block}\n"
            f"\n"
            f"# 你的语气 / Your voice\n"
            f"{self.voice_prompt}\n"
            f"\n"
            f"# 重要规则 / Critical rules\n"
            f"  1. Stay strictly in character. Speak like {self.archetype}, not like a neutral analyst.\n"
            f"  2. NEVER output investment recommendations. Avoid these forbidden words in your\n"
            f"     comments (they will be regex-filtered): 建议, 推荐, 应该, 必须, 买入, 卖出,\n"
            f"     减仓, 加仓, 建仓, 清仓, 目标价, 评级, 止损位, 止盈位, BUY, SELL, target price.\n"
            f"  3. Use descriptive language instead. Say '我倾向 / 我看好 / 我担心 / 我会先观望'\n"
            f"     not '我建议你 / 应该买入'. Describe how YOU (this character) would react,\n"
            f"     not what the user should do.\n"
            f"  4. Be specific. Reference actual numbers, prices, or behaviors from the event.\n"
            f"  5. Disagree with other personas when your character would. Do not reach false consensus.\n"
        )


# ─────────────────────── YAML loader ───────────────────────


REQUIRED_FIELDS = {"id", "archetype", "display_name", "model", "voice_prompt"}


def _validate_persona_dict(data: dict[str, Any], idx: int, source: str) -> None:
    missing = REQUIRED_FIELDS - data.keys()
    if missing:
        raise PersonaSchemaError(
            f"Persona #{idx} in {source} missing required fields: {sorted(missing)}"
        )
    if not isinstance(data.get("biases", {}), dict):
        raise PersonaSchemaError(
            f"Persona #{idx} ({data.get('id')}) in {source}: biases must be a dict"
        )
    if not isinstance(data.get("knowledge", {}), dict):
        raise PersonaSchemaError(
            f"Persona #{idx} ({data.get('id')}) in {source}: knowledge must be a dict"
        )


def load_personas(path: str | Path) -> list[Persona]:
    """Load + validate persona YAML.

    Expected file structure:

        schema_version: 1
        personas:
          - id: retail_aunt_alpha
            archetype: 散户大妈
            display_name: ...
            model: gpt-4o-mini
            voice_prompt: |
              ...
            biases:
              loss_averse: 0.8
            knowledge:
              holdings: [银行股, 白酒]
              information_sources: [微信群, 央视财经]
              ignores: [财报附注]
            weight: 1.0
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Persona file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PersonaSchemaError(f"{path}: top-level must be a YAML mapping")

    file_schema = raw.get("schema_version", SCHEMA_VERSION)
    if file_schema != SCHEMA_VERSION:
        raise PersonaSchemaError(
            f"{path}: schema_version {file_schema} != expected {SCHEMA_VERSION}"
        )

    personas_raw = raw.get("personas")
    if not isinstance(personas_raw, list) or not personas_raw:
        raise PersonaSchemaError(f"{path}: 'personas' must be a non-empty list")

    personas: list[Persona] = []
    seen_ids: set[str] = set()
    for i, p in enumerate(personas_raw):
        if not isinstance(p, dict):
            raise PersonaSchemaError(f"Persona #{i} in {path} is not a mapping")
        _validate_persona_dict(p, i, str(path))
        if p["id"] in seen_ids:
            raise PersonaSchemaError(f"{path}: duplicate persona id '{p['id']}'")
        seen_ids.add(p["id"])
        personas.append(
            Persona(
                id=p["id"],
                archetype=p["archetype"],
                display_name=p["display_name"],
                model=p["model"],
                voice_prompt=p["voice_prompt"].strip(),
                biases=p.get("biases", {}),
                knowledge=p.get("knowledge", {}),
                weight=float(p.get("weight", 1.0)),
                schema_version=file_schema,
            )
        )

    return personas


def persona_set_hash(personas: list[Persona]) -> str:
    """Stable hash of a persona set, for reproducibility tracking in scorecard."""
    h = hashlib.sha256()
    for p in sorted(personas, key=lambda x: x.id):
        h.update(p.id.encode())
        h.update(p.model.encode())
        h.update(p.voice_prompt.encode())
    return h.hexdigest()[:16]


__all__ = [
    "Persona",
    "PersonaSchemaError",
    "SCHEMA_VERSION",
    "load_personas",
    "persona_set_hash",
]
