"""Decision records (§7.5): the full, machine-readable explanation of a decision.

Records carry data only — no rendered prose, no wall-clock timings. Rendering is a
pure function of the record (render.py). Records are embedded in `AllocationDecided`
events, so the audit trail of *why* travels with the history of *what*.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import LegSchedule, Stop
from nodal.rules.framework import Reject


class LegSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_label: str
    to_facility_id: str
    lane_id: str | None
    minutes: int
    km: float
    cost_cents: int


class RouteSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    legs: list[LegSummary]
    minutes: int  # transit only
    # Operating-window waits on the journey (§6): the dwells that had to sit out a
    # shut transit facility, plus the wait at the destination itself.
    wait_minutes: int
    km: float
    cost_cents: int
    transfers: int


class ItineraryLeg(BaseModel):
    """One scheduled movement of an A->B delivery (§7.9)."""

    model_config = ConfigDict(frozen=True)

    kind: str  # "road" for first/last mile, else the lane's mode
    from_label: str
    to_label: str
    lane_id: str | None  # None = a road leg, which is never a lane
    km: float
    minutes: int
    cost_cents: int
    depart: datetime
    arrive: datetime


class Hold(BaseModel):
    """Where and when a delivery's goods sit between its inbound and outbound
    legs. The reservation books exactly this window."""

    model_config = ConfigDict(frozen=True)

    facility_id: str
    zone_id: str
    from_ts: datetime
    until_ts: datetime


class Itinerary(BaseModel):
    """The delivery decision made inspectable: every leg in order, every facility
    the goods dwell at with its window, the hold, and when the customer gets the
    goods (§7.9)."""

    model_config = ConfigDict(frozen=True)

    legs: list[ItineraryLeg]
    hold: Hold
    destination: str
    delivered_at: datetime
    cost_cents: int  # inbound + outbound transport
    # Every facility touched, in order, with the window it is occupied for and
    # the zone that window books. Derived exactly from the legs — no estimation —
    # and the thing closures, equipment outages and staging capacity bind against.
    stops: list[Stop] = Field(default_factory=list)

    @property
    def lane_ids(self) -> list[str]:
        """Every lane the goods travel, in order: the inbound half followed by the
        outbound half (road legs are never lanes)."""
        return [leg.lane_id for leg in self.legs if leg.lane_id is not None]

    @property
    def exit_facility_id(self) -> str:
        """The facility the last-mile road leg — always the final leg — starts
        from; equal to the holding facility when the goods never move on."""
        return self.legs[-1].from_label


class ComponentScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw: float
    normalized: float
    weight: float
    contribution: float
    # For marginal components (congestion, inv_balance): the pre-placement level
    # the marginal is taken against, so the arithmetic is reproducible from the
    # record alone (raw = level after, normalized = f(raw) - f(baseline)).
    baseline: float | None = None


class ScoredCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    facility_id: str
    zone_id: str  # the facility's best zone by the full objective
    route: RouteSummary
    eta: datetime
    departure: datetime
    components: dict[str, ComponentScore]
    total: float
    # Sibling zones that failed zone-level constraints — the rejection picture is
    # complete even for scored facilities (§7.2, §7.5).
    zone_verdicts: dict[str, list[Reject]] = Field(default_factory=dict)


class RejectedFacility(BaseModel):
    model_config = ConfigDict(frozen=True)

    facility_id: str
    facility_verdicts: list[Reject] = Field(default_factory=list)
    zone_verdicts: dict[str, list[Reject]] = Field(default_factory=dict)


class BucketImpact(BaseModel):
    model_config = ConfigDict(frozen=True)

    day: date
    capacity: CapacityVector
    occupancy_before: CapacityVector
    occupancy_after: CapacityVector


class CapacityImpact(BaseModel):
    model_config = ConfigDict(frozen=True)

    zone_id: str
    buckets: list[BucketImpact]


class StagingBooking(BaseModel):
    """One pass-through stop's reservation (§7.9): the same event a hold books,
    for the handling dwell, so audit, replay and what-if get it for free."""

    model_config = ConfigDict(frozen=True)

    reservation_id: str
    facility_id: str
    zone_id: str
    from_ts: datetime
    until_ts: datetime
    role: str


class Chosen(BaseModel):
    model_config = ConfigDict(frozen=True)

    facility_id: str
    zone_id: str
    route: RouteSummary
    eta: datetime
    departure: datetime
    size: CapacityVector  # what the reservation books
    reservation_ids: list[str]  # the hold's own reservation(s)
    # A->B delivery only (§7.9): `route` above is the inbound half — what books
    # the hold — and this is the whole movement through to the customer.
    itinerary: Itinerary | None = None
    # Staging reservations at the pass-through stops, empty for a journey that
    # goes straight to its stay without touching a facility on the way.
    staging: list[StagingBooking] = Field(default_factory=list)
    # Every facility the journey occupies, in travel order, and when each lane is
    # actually travelled (§7.9). Present for ordinary allocations too — an
    # intermediate hop is a dwell whoever is paying for it — so re-optimization
    # matches a closure against the window it really touches. Empty on records
    # written before the extension, which therefore fold exactly as they did.
    stops: list[Stop] = Field(default_factory=list)
    legs: list[LegSchedule] = Field(default_factory=list)


class SolverMeta(BaseModel):
    """Batch-solver metadata (§7.5). Deterministic budget, never wall clock."""

    model_config = ConfigDict(frozen=True)

    status: str
    objective_scaled: int
    bound_scaled: int
    gap: float | None  # proven optimality gap; None = heuristic, nothing proven
    det_time_budget: float
    workers: int
    seed: int
    batch_id: str
    shipments: int
    # §2: parallel search is nondeterministic — results are budget-dependent.
    reproducible: bool = True
    # §13: an incumbent was seeded from the validated flow-relax hint — up
    # front when `SolverConfig.warm_start` is set, otherwise via the one
    # fallback re-solve after a cold start exhausted its budget with no
    # incumbent. The reported gap is still CP-SAT's own proven bound.
    warm_started: bool = False


class BatchContext(BaseModel):
    """The batch explanation (§7.5): the shipment's best local alternative
    with everything else fixed, and the capacity rows binding at the optimum."""

    model_config = ConfigDict(frozen=True)

    delta_vs_best_alternative: float | None  # normalized objective units; None = no alternative
    best_alternative: str | None  # "FAC-X/ZON-Y"
    binding_constraints: list[str]  # "ZON-X/slots/2026-09-03"


class DecisionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    shipment_id: str
    decided_at: datetime  # engine/simulation time, never wall clock (§2)
    mode: Literal["single", "batch"] = "single"
    policy: str = "nodal-single"  # which policy chose (baselines share this record shape)
    based_on_seq: int  # state.last_seq the decision read; commit() refuses stale records
    config_snapshot: dict[str, JsonValue]
    engine_version: str
    tzdata_version: str
    considered: list[str]  # every facility in the network, always (§7.1)
    rejected: list[RejectedFacility]
    scored: list[ScoredCandidate]  # sorted best-first by the objective
    chosen: Chosen | None  # None: no feasible destination exists
    capacity_impact: CapacityImpact | None
    solver: SolverMeta | None = None  # batch mode only
    batch_context: BatchContext | None = None  # batch mode only
    # Re-optimization audit (§7.6): which escalation tier produced this record
    # and which disruption triggered it. None outside re-optimization.
    reopt_tier: int | None = None
    reopt_trigger: str | None = None

    def as_event_record(self) -> JsonValue:
        dumped: JsonValue = self.model_dump(mode="json")
        return dumped

    @classmethod
    def from_event_record(cls, record: JsonValue) -> "DecisionRecord":
        return cls.model_validate(record)
