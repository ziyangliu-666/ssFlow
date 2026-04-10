"""Generalized action collector for non-trader agent tool calls.

When a non-trader agent's LLM calls a domain-specific tool (announce,
regulate, publish research), the tool closure captures the action into
this collector. After env.step(), the engine drains the collector and
dispatches each action.

This mirrors OrderCollector for trading — but for richer action types.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class PendingAction:
    """One action captured from a non-trader agent's LLM tool call."""

    agent_id: str          # SimAgent id (e.g., "company_ir_byd")
    persona_id: str        # Persona id for OASIS mapping
    action_type: str       # "announce" | "regulate" | "publish_research"
    payload: dict[str, Any] = field(default_factory=dict)
    round_idx: int = 0


class ActionCollector:
    """Thread-safe collector for non-trader agent actions.

    One instance per simulation. Each non-trader's domain tool closure
    captures a reference to the same collector. After env.step(), the
    engine calls drain() to process all collected actions.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[PendingAction] = []
        self._current_round: int = 0

    def set_round(self, round_idx: int) -> None:
        with self._lock:
            self._current_round = round_idx

    def add(
        self,
        agent_id: str,
        persona_id: str,
        action_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._pending.append(PendingAction(
                agent_id=agent_id,
                persona_id=persona_id,
                action_type=action_type,
                payload=dict(payload or {}),
                round_idx=self._current_round,
            ))

    def drain(self) -> list[PendingAction]:
        with self._lock:
            out = list(self._pending)
            self._pending.clear()
            return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)


__all__ = ["ActionCollector", "PendingAction"]
