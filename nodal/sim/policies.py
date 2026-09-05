"""Allocation policies (§9): the optimizer and the naive baselines it must beat.

All policies share the engine's feasibility survey, so hard constraints are never
violated by anyone; baselines are naive only in *which feasible facility they
prefer* (zone choice and scoring stay the engine's, so records are comparable).
`infeasible_preferred` counts how far a policy's own myopic ranking walked past
infeasible candidates — a per-policy diagnostic, not a head-to-head KPI.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from nodal.allocate.config import ObjectiveConfig
from nodal.allocate.engine import CandidateSurvey, allocate, build_record, survey_candidates
from nodal.allocate.records import DecisionRecord
from nodal.events.state import NetworkState
from nodal.network.travel import haversine_km

if TYPE_CHECKING:
    from nodal.allocate.batch import BatchResult


@dataclass(frozen=True)
class PolicyResult:
    record: DecisionRecord
    # Candidates this policy's own ranking preferred above its choice that failed
    # feasibility. Structurally 0 for nodal-single (its ranking is the objective
    # over feasible candidates) — a diagnostic, not a head-to-head KPI.
    infeasible_preferred: int


class AllocationPolicy(Protocol):
    name: str

    def decide(
        self,
        state: NetworkState,
        shipment_id: str,
        config: ObjectiveConfig,
        now: datetime | None = None,
    ) -> PolicyResult: ...


class NodalSingle:
    """The engine's scored allocation (§7.3)."""

    name = "nodal-single"

    def decide(
        self,
        state: NetworkState,
        shipment_id: str,
        config: ObjectiveConfig,
        now: datetime | None = None,
    ) -> PolicyResult:
        return PolicyResult(
            record=allocate(state, shipment_id, config, now), infeasible_preferred=0
        )


class _RankingPolicy:
    """Walk a myopic facility ranking until feasibility admits one."""

    name = "ranking"

    def rank_key(
        self, state: NetworkState, shipment_id: str, survey: CandidateSurvey
    ) -> tuple[float, str]:
        raise NotImplementedError

    def decide(
        self,
        state: NetworkState,
        shipment_id: str,
        config: ObjectiveConfig,
        now: datetime | None = None,
    ) -> PolicyResult:
        shipment = state.shipments[shipment_id]
        if now is None:
            now = state.last_ts
        assert now is not None
        surveys = survey_candidates(state, shipment, config, now)
        ordered = sorted(surveys, key=lambda s: self.rank_key(state, shipment_id, s))
        attempts = 0
        choice: str | None = None
        for survey in ordered:
            if survey.feasible:
                choice = survey.facility_id
                break
            attempts += 1
        record = build_record(
            state, shipment, config, now, surveys, choice=choice, policy=self.name
        )
        return PolicyResult(
            record=record, infeasible_preferred=attempts if choice else len(surveys)
        )


class NearestFeasible(_RankingPolicy):
    name = "nearest-feasible"

    def rank_key(
        self, state: NetworkState, shipment_id: str, survey: CandidateSurvey
    ) -> tuple[float, str]:
        shipment = state.shipments[shipment_id]
        if shipment.origin_facility_id is not None:
            origin = state.facilities[shipment.origin_facility_id]
            lat, lon = origin.lat, origin.lon
        elif shipment.origin_lat is not None and shipment.origin_lon is not None:
            lat, lon = shipment.origin_lat, shipment.origin_lon
        else:
            return (float("inf"), survey.facility_id)
        facility = state.facilities[survey.facility_id]
        return (haversine_km(lat, lon, facility.lat, facility.lon), survey.facility_id)


class FirstAvailable(_RankingPolicy):
    name = "first-available"

    def rank_key(
        self, state: NetworkState, shipment_id: str, survey: CandidateSurvey
    ) -> tuple[float, str]:
        return (0.0, survey.facility_id)


class Greedy(_RankingPolicy):
    """Cheapest-now: myopic direct transport cost, nothing else."""

    name = "greedy"

    def rank_key(
        self, state: NetworkState, shipment_id: str, survey: CandidateSurvey
    ) -> tuple[float, str]:
        cost = float(survey.route.cost_cents) if survey.route is not None else float("inf")
        return (cost, survey.facility_id)


class BatchPolicy(Protocol):
    """Policies that decide whole pending batches at batch ticks (§7.4, §9)."""

    name: str

    def decide_batch(
        self,
        state: NetworkState,
        shipment_ids: list[str],
        config: ObjectiveConfig,
        now: datetime,
        batch_id: str,
    ) -> "BatchResult": ...


class NodalBatch:
    """CP-SAT batch optimization with the flow fast path (§7.4)."""

    name = "nodal-batch"

    def decide_batch(
        self,
        state: NetworkState,
        shipment_ids: list[str],
        config: ObjectiveConfig,
        now: datetime,
        batch_id: str,
    ) -> "BatchResult":
        from nodal.allocate.batch import solve_batch

        return solve_batch(state, shipment_ids, config, now, batch_id)


class FlowRelax:
    """Conservative transportation heuristic — a batch baseline, not a proof."""

    name = "flow-relax"

    def decide_batch(
        self,
        state: NetworkState,
        shipment_ids: list[str],
        config: ObjectiveConfig,
        now: datetime,
        batch_id: str,
    ) -> "BatchResult":
        from nodal.allocate.batch import solve_flow_relax

        return solve_flow_relax(state, shipment_ids, config, now, batch_id)


POLICIES: dict[str, type[NodalSingle] | type[_RankingPolicy]] = {
    "nodal-single": NodalSingle,
    "nearest-feasible": NearestFeasible,
    "first-available": FirstAvailable,
    "greedy": Greedy,
}

BATCH_POLICIES: dict[str, type[NodalBatch] | type[FlowRelax]] = {
    "nodal-batch": NodalBatch,
    "flow-relax": FlowRelax,
}


def make_policy(name: str) -> "AllocationPolicy | BatchPolicy":
    if name in POLICIES:
        return POLICIES[name]()
    if name in BATCH_POLICIES:
        return BATCH_POLICIES[name]()
    raise KeyError(f"unknown policy {name!r} (known: {sorted([*POLICIES, *BATCH_POLICIES])})")
