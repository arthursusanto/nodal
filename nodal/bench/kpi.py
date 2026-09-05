"""KPI computation (§10).

Outcome KPIs are deterministic functions of the run (byte-stable for a given
scenario/seed/policy); performance KPIs are wall-clock and live in a separate
section that byte-identity checks exclude.
"""

import math
from datetime import datetime
from statistics import fmean, pvariance

from pydantic import BaseModel, ConfigDict, Field, JsonValue


def p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile: never below the true percentile."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[index]


class DecisionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    shipment_id: str
    allocated: bool
    cost_cents: int = 0
    km: float = 0.0
    transfers: int = 0
    # Candidates the policy's own preference ranking placed above its final choice
    # that failed feasibility. Structurally 0 for nodal-single, whose ranking is
    # defined over feasible candidates only — NOT comparable as a head-to-head KPI.
    infeasible_preferred: int = 0


class ArrivalOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    shipment_id: str
    arrived_at: datetime
    deadline: datetime | None
    # The destination facility had an active closure disruption at arrival — a
    # service failure only re-optimizing policies can avoid (§7.6).
    arrived_closed: bool = False


class RunStats(BaseModel):
    """Everything the runner accumulates during a simulation."""

    scenario: str
    policy: str
    seed: int
    horizon_days: int
    profile: str = "default"  # objective profile name (§7.8)
    decisions: list[DecisionOutcome] = Field(default_factory=list)
    rebalancing: list[DecisionOutcome] = Field(default_factory=list)  # transfers (§7.7)
    arrivals: list[ArrivalOutcome] = Field(default_factory=list)
    batch_gaps: list[float] = Field(default_factory=list)  # -1.0 = heuristic, no proof
    utilization_samples: dict[str, list[float]] = Field(default_factory=dict)  # dim -> ratios
    imbalance_samples: dict[str, list[float]] = Field(default_factory=dict)  # group -> ratios
    band_hits: int = 0
    band_samples: int = 0
    # Re-optimization (§7.6): one entry per disruption that triggered a re-solve.
    reopt_tiers: list[int] = Field(default_factory=list)
    reopt_moved: int = 0  # assignments changed to a new destination
    reopt_released: int = 0  # assignments released back to planned
    # performance (wall clock; excluded from byte-identity):
    decision_wall_ms: list[float] = Field(default_factory=list)
    reopt_wall_ms: list[float] = Field(default_factory=list)


class KPIReport(BaseModel):
    scenario: str
    policy: str
    seed: int
    profile: str
    outcome: dict[str, JsonValue]
    performance: dict[str, JsonValue]

    def outcome_json(self) -> str:
        """The deterministic section only — the byte-identity surface."""
        return self.model_copy(update={"performance": {}}).model_dump_json(indent=2)


def compute_kpis(stats: RunStats) -> KPIReport:
    allocated = [d for d in stats.decisions if d.allocated]
    with_deadline = [a for a in stats.arrivals if a.deadline is not None]
    lateness_minutes = [
        max(0.0, (a.arrived_at - a.deadline).total_seconds() / 60)
        for a in with_deadline
        if a.deadline is not None
    ]
    on_time = sum(1 for late in lateness_minutes if late == 0.0)

    outcome: dict[str, JsonValue] = {
        "shipments": len(stats.decisions),
        "allocated": len(allocated),
        "unallocated": len(stats.decisions) - len(allocated),
        "total_cost_cents": sum(d.cost_cents for d in allocated),
        "total_km": round(sum(d.km for d in allocated), 1),
        "transfers": sum(d.transfers for d in allocated),
        "infeasible_preferred": sum(d.infeasible_preferred for d in stats.decisions),
        "arrivals": len(stats.arrivals),
        "on_time_rate": round(on_time / len(with_deadline), 4) if with_deadline else 1.0,
        "lateness_mean_min": round(fmean(lateness_minutes), 1) if lateness_minutes else 0.0,
        "lateness_p95_min": round(p95(lateness_minutes), 1),
        "util_band_residency": (
            round(stats.band_hits / stats.band_samples, 4) if stats.band_samples else 0.0
        ),
    }
    moved = [d for d in stats.rebalancing if d.allocated]
    if stats.rebalancing:
        outcome["rebalancing_moves"] = len(moved)
        outcome["rebalancing_cancelled"] = len(stats.rebalancing) - len(moved)
        outcome["rebalancing_cost_cents"] = sum(d.cost_cents for d in moved)
    proven_gaps = [g for g in stats.batch_gaps if g >= 0]
    if stats.batch_gaps:
        outcome["batch_solves"] = len(stats.batch_gaps)
        outcome["batch_gap_max"] = round(max(proven_gaps), 6) if proven_gaps else None
        outcome["batch_gap_mean"] = round(fmean(proven_gaps), 6) if proven_gaps else None
    if stats.reopt_tiers:
        outcome["reopt_solves"] = len(stats.reopt_tiers)
        outcome["reopt_tier2"] = sum(1 for t in stats.reopt_tiers if t >= 2)
        outcome["reopt_moved"] = stats.reopt_moved
        outcome["reopt_released"] = stats.reopt_released
    outcome["closure_violations"] = sum(1 for a in stats.arrivals if a.arrived_closed)
    all_imbalance = [x for samples in stats.imbalance_samples.values() for x in samples]
    outcome["imbalance_mean"] = round(fmean(all_imbalance), 4) if all_imbalance else 0.0
    for group, samples in sorted(stats.imbalance_samples.items()):
        if samples:
            outcome[f"imbalance_{group}_mean"] = round(fmean(samples), 4)
    for dim, samples in sorted(stats.utilization_samples.items()):
        if samples:
            outcome[f"util_{dim}_mean"] = round(fmean(samples), 4)
            outcome[f"util_{dim}_var"] = round(pvariance(samples), 5)
            outcome[f"util_{dim}_p95"] = round(p95(samples), 4)

    performance: dict[str, JsonValue] = {}
    if stats.decision_wall_ms:
        performance["decision_ms_mean"] = round(fmean(stats.decision_wall_ms), 2)
        performance["decision_ms_p95"] = round(p95(stats.decision_wall_ms), 2)
    if stats.reopt_wall_ms:
        performance["reopt_ms_mean"] = round(fmean(stats.reopt_wall_ms), 2)
        performance["reopt_ms_max"] = round(max(stats.reopt_wall_ms), 2)

    return KPIReport(
        scenario=stats.scenario,
        policy=stats.policy,
        seed=stats.seed,
        profile=stats.profile,
        outcome=outcome,
        performance=performance,
    )
