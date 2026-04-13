"""Cognition pipeline: LLM intent -> constraint solver -> order generator.

Bridges the information layer (OASIS social feed) and the market engine
(LOB order book). The pipeline ensures:

1. The LLM expresses INTENT (what the agent wants to do and why)
2. Hard constraints CHECK the intent (can the agent actually do this?)
3. The order generator EXECUTES within constraints (how to express as orders)
"""

from .intent_resolver import IntentResolver, StructuredIntent
from .constraint_solver import ConstraintSolver, ValidatedIntent, RejectedIntent
from .order_generator import OrderGenerator, OrderSpec
from .information_filter import InformationFilter, FilteredContext
from .agent_memory import AgentMemory

__all__ = [
    "AgentMemory",
    "ConstraintSolver",
    "FilteredContext",
    "InformationFilter",
    "IntentResolver",
    "OrderGenerator",
    "OrderSpec",
    "RejectedIntent",
    "StructuredIntent",
    "ValidatedIntent",
]
