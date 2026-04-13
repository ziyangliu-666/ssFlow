"""Decomposed simulation orchestrator.

Replaces the monolithic oasis_engine.py with a modular pipeline:
  - clock.py: A-share session scheduler
  - orchestrator.py: main simulation loop wiring social → cognition → market
  - round_adapter.py: maps micro-steps back to rounds for backward compat
"""

from .clock import AShareClock, SessionEvent
from .orchestrator import SimulationResult, run_simulation_lob

__all__ = [
    "AShareClock",
    "SessionEvent",
    "SimulationResult",
    "run_simulation_lob",
]
