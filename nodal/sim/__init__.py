"""Simulation: virtual clock, workload generation, policies (§9).

The simulator is an event producer driving the same engine through the same event
log — never a parallel implementation. All randomness lives in seeded generation;
the run loop itself is deterministic.
"""

from nodal.sim.generator import GeneratedWorkload, generate
from nodal.sim.scenarios import ScenarioSpec, load_scenario

__all__ = ["GeneratedWorkload", "ScenarioSpec", "generate", "load_scenario"]
