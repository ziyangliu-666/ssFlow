"""Persona proposer — bridge between stored persona packs and the web UI.

Responsibilities
----------------
1. Load a base persona pack from disk and return a list of **partial**
   persona dicts suitable for display + inline editing in the browser.
   Only the fields the user is expected to edit (6 of them) are exposed;
   everything else lives in the base pack and is re-applied on submit.
2. Rebuild a full `Persona` object from an edited partial + the base
   pack (`persona_from_partial`).
3. Generate a blank "template" partial for the "add new persona"
   button, starting from the `DEFAULT_ACTION_TEMPLATES` in
   `persona_factory`.

The 6 editable fields (per user spec):
    - archetype              — short label ("散户(短线追涨)")
    - display_name           — one-line voice description
    - voice_prompt           — full multi-line system-prompt snippet
    - instance_count         — how many simulated agents in this class
    - capital_median_cny     — median capital per agent (log-normal)
    - prob_holding           — Bernoulli p for starting with a position

All other persona fields (biases, knowledge, risk, action_space, follows,
publishes, market_share, decision_mode, role, …) are frozen in the base
pack and transparently re-applied on the backend.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

from .event import Event
from .persona import (
    VALID_DECISION_MODES,
    MarketShare,
    Persona,
    PersonaSchemaError,
    SandboxConfig,
    load_personas,
)


log = logging.getLogger(__name__)


# ─────────────────────── Persona → partial ───────────────────────


def persona_to_partial(persona: Persona) -> dict[str, Any]:
    """Project a full `Persona` down to the 6-field editable partial."""
    sandbox = persona.sandbox
    instance_count: int | None = None
    capital_median_cny: float | None = None
    prob_holding: float | None = None

    if sandbox is not None:
        instance_count = int(sandbox.instance_count)
        cap = sandbox.capital_distribution or {}
        # Only lognormal carries a "median_cny"; fixed carries "value_cny".
        if "median_cny" in cap:
            try:
                capital_median_cny = float(cap["median_cny"])
            except (TypeError, ValueError):
                capital_median_cny = None
        elif "value_cny" in cap:
            try:
                capital_median_cny = float(cap["value_cny"])
            except (TypeError, ValueError):
                capital_median_cny = None
        init_pos = sandbox.initial_position_distribution or {}
        if "prob_holding" in init_pos:
            try:
                prob_holding = float(init_pos["prob_holding"])
            except (TypeError, ValueError):
                prob_holding = None

    return {
        "id": persona.id,
        "archetype": persona.archetype,
        "display_name": persona.display_name,
        "voice_prompt": persona.voice_prompt,
        "entity_role": persona.entity_role,
        "decision_mode": persona.decision_mode,
        # The 3 sandbox-only editable fields. None for non-trader personas.
        "instance_count": instance_count,
        "capital_median_cny": capital_median_cny,
        "prob_holding": prob_holding,
        # Sticky reference back to the base persona this partial was
        # projected from. The backend uses this on `/simulate-stream/init`
        # to re-hydrate the full persona on submit.
        "_base_template": persona.id,
    }


def propose_personas(
    event: Event | None = None,
    base_pack_path: str | Path = "personas/ashare.yaml",
) -> list[dict[str, Any]]:
    """Return a list of editable persona partials for the SetupView.

    v1 behaviour: load the base pack and return EVERY persona as a
    partial. No LLM-based selection. v2 may add a "pick the most
    relevant N personas for this event" step using a single LLM call.

    The `event` argument is accepted for forward compatibility but is
    currently unused.
    """
    path = Path(base_pack_path)
    personas = load_personas(path)
    log.info(
        "propose_personas: loaded %d base personas from %s (event=%s)",
        len(personas),
        path,
        getattr(event, "ticker", None),
    )
    return [persona_to_partial(p) for p in personas]


# ─────────────────────── Partial → Persona ───────────────────────


def _deep_merge_capital(
    base_cap: dict[str, Any],
    new_median: float | None,
) -> dict[str, Any]:
    """Return a new capital_distribution dict with the median overridden."""
    if new_median is None:
        return dict(base_cap)
    merged = dict(base_cap)
    # Preserve the base type but override the representative magnitude.
    # Lognormal uses median_cny; fixed uses value_cny.
    if merged.get("type") == "fixed":
        merged["value_cny"] = float(new_median)
    else:
        merged.setdefault("type", "lognormal")
        merged["median_cny"] = float(new_median)
    return merged


def _deep_merge_initial_position(
    base_init: dict[str, Any],
    new_prob: float | None,
) -> dict[str, Any]:
    """Return a new initial_position_distribution dict with prob_holding overridden."""
    if new_prob is None:
        return dict(base_init)
    merged = dict(base_init)
    merged.setdefault("type", "bernoulli")
    merged["prob_holding"] = float(new_prob)
    return merged


def persona_from_partial(
    partial: dict[str, Any],
    base_pack: list[Persona],
) -> Persona:
    """Rebuild a full `Persona` from an edited partial + the base pack.

    Lookup rules:
      1. If the partial carries ``_base_template`` and that id exists in
         the base pack, start from a deep copy of that base persona and
         overlay the 6 editable fields.
      2. Otherwise, if the partial's own ``id`` matches a base pack
         persona, use that.
      3. Otherwise, fall back to `_build_from_scratch` using the
         partial's ``decision_mode`` and `DEFAULT_ACTION_TEMPLATES` —
         this is the "user clicked Add new persona" path.

    Args:
        partial: dict shaped by the UI. See `persona_to_partial` for
            the expected keys.
        base_pack: the full list of personas loaded from the base pack
            (typically `personas/ashare.yaml`).

    Raises:
        PersonaSchemaError: if the partial cannot be reconciled into a
            valid `Persona` (missing required fields, unknown
            decision_mode, etc.)
    """
    base_by_id = {p.id: p for p in base_pack}
    base_id = partial.get("_base_template") or partial.get("id")
    base_persona: Persona | None = None
    if isinstance(base_id, str) and base_id in base_by_id:
        base_persona = base_by_id[base_id]

    if base_persona is not None:
        return _overlay_on_base(base_persona, partial)

    # Fallback: build from scratch using action templates
    return _build_from_scratch(partial)


def _overlay_on_base(base: Persona, partial: dict[str, Any]) -> Persona:
    """Deep-copy the base persona and overlay the 6 editable fields."""
    new = copy.deepcopy(base)

    # Identity + voice fields
    if partial.get("id"):
        new.id = str(partial["id"])
    if partial.get("archetype"):
        new.archetype = str(partial["archetype"])
    if partial.get("display_name"):
        new.display_name = str(partial["display_name"])
    if partial.get("voice_prompt"):
        new.voice_prompt = str(partial["voice_prompt"]).strip()

    # Sandbox-only fields (only apply if the base persona has a sandbox)
    if new.sandbox is not None:
        new_sb = new.sandbox
        if partial.get("instance_count") is not None:
            try:
                new_sb.instance_count = max(1, int(partial["instance_count"]))
            except (TypeError, ValueError) as exc:
                raise PersonaSchemaError(
                    f"persona '{new.id}': instance_count must be an integer, "
                    f"got {partial.get('instance_count')!r}"
                ) from exc
        if "capital_median_cny" in partial:
            new_sb.capital_distribution = _deep_merge_capital(
                new_sb.capital_distribution,
                _coerce_float_or_none(partial.get("capital_median_cny")),
            )
        if "prob_holding" in partial:
            new_sb.initial_position_distribution = _deep_merge_initial_position(
                new_sb.initial_position_distribution,
                _coerce_float_or_none(partial.get("prob_holding")),
            )

    return new


def _coerce_float_or_none(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _build_from_scratch(partial: dict[str, Any]) -> Persona:
    """Create a brand-new Persona from a user-supplied partial.

    Used when the user clicks "Add persona" and there is no base
    persona to inherit from. Requires: id, archetype, display_name,
    voice_prompt, decision_mode. Fills in sensible defaults for the
    rest.
    """
    # Imported lazily to avoid a module-load cycle with persona_factory
    # (which imports from this module in some code paths).
    from .persona_factory import DEFAULT_ACTION_TEMPLATES

    required = ("id", "archetype", "display_name", "voice_prompt", "decision_mode")
    missing = [k for k in required if not partial.get(k)]
    if missing:
        raise PersonaSchemaError(
            f"Cannot build persona from scratch: missing fields {missing}"
        )
    decision_mode = str(partial["decision_mode"])
    if decision_mode not in VALID_DECISION_MODES:
        raise PersonaSchemaError(
            f"Cannot build persona from scratch: decision_mode must be one of "
            f"{sorted(VALID_DECISION_MODES)}, got {decision_mode!r}"
        )

    entity_role = str(partial.get("entity_role", "trader"))

    sandbox: SandboxConfig | None = None
    if entity_role == "trader":
        sandbox = SandboxConfig(
            instance_count=max(1, int(partial.get("instance_count") or 100)),
            capital_distribution={
                "type": "lognormal",
                "median_cny": float(partial.get("capital_median_cny") or 100_000),
                "sigma": 0.8,
            },
            initial_position_distribution={
                "type": "bernoulli",
                "prob_holding": float(partial.get("prob_holding") or 0.5),
            },
            risk={"max_position_pct": 0.95},
            action_space=list(
                DEFAULT_ACTION_TEMPLATES.get(
                    decision_mode, DEFAULT_ACTION_TEMPLATES["discretionary"]
                )
            ),
        )

    return Persona(
        id=str(partial["id"]),
        archetype=str(partial["archetype"]),
        display_name=str(partial["display_name"]),
        voice_prompt=str(partial["voice_prompt"]).strip(),
        decision_mode=decision_mode,
        role=str(partial.get("role", "directional_speculator")),
        entity_role=entity_role,
        market_share=MarketShare(by_volume=0.01),
        sandbox=sandbox,
    )


# ─────────────────────── Blank template for "add new persona" ───────────────────────


def blank_partial(decision_mode: str = "discretionary") -> dict[str, Any]:
    """Return an empty editable partial for the "Add persona" button.

    The returned dict has all the keys the UI expects, with sensible
    defaults. The user must still fill in id / archetype / voice_prompt.
    """
    from .persona_factory import DEFAULT_ACTION_TEMPLATES

    if decision_mode not in VALID_DECISION_MODES:
        decision_mode = "discretionary"

    action_template = DEFAULT_ACTION_TEMPLATES.get(
        decision_mode, DEFAULT_ACTION_TEMPLATES["discretionary"]
    )

    return {
        "id": "",
        "archetype": "",
        "display_name": "",
        "voice_prompt": "",
        "entity_role": "trader",
        "decision_mode": decision_mode,
        "instance_count": 500,
        "capital_median_cny": 100_000,
        "prob_holding": 0.5,
        "_base_template": None,
        # Purely informational — the frontend shows these so the user
        # knows which action set will drive this class.
        "_default_actions": [a["name"] for a in action_template],
    }


__all__ = [
    "persona_to_partial",
    "propose_personas",
    "persona_from_partial",
    "blank_partial",
]
