"""Staging space at a pass-through stop (§7.9).

A journey that enters, transfers or exits through a facility occupies it for the
handling dwell the journey already prices. This module answers the one question
the router asks per candidate — *which zone at each of these stops can take these
goods for its window, if any* — and keeps the explanation of a "no" so the
decision record can name it.

The answer is a pure function of (shipment, facility, window, state) plus the
candidate's OWN earlier dwells: it never looks at other candidates or other
shipments in a batch, which is what keeps a candidate's staging bookings
constants of (shipment, holding facility) and the batch model's per-pair
coefficients exact (§7.4, §14). Counting its own earlier dwells is what makes it
agree with the batch model, which sums every dwell a candidate books onto the
zone-bucket row it shares (§7.4) — one journey passing the same dock twice pays
for it twice, in both paths.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from nodal.allocate.config import ObjectiveConfig
from nodal.allocate.scorer import EvalCache
from nodal.domain.entities import DisruptionKind, Shipment, Stop
from nodal.events.state import NetworkState, buckets_between
from nodal.network.travel import TravelModel
from nodal.rules.core import (
    CapacityConstraint,
    ClassAllowedConstraint,
    SegregationConstraint,
    TempRangeConstraint,
    ZoneOfflineConstraint,
)
from nodal.rules.framework import (
    AllocationContext,
    Constraint,
    ConstraintScope,
    Pack,
    Reject,
)

# Zone-level rules a staging dwell must satisfy. Deliberately NOT the full stay
# set: `zone_kinds` states where the customer's goods must be STORED, and a
# handling dwell is not storage — requiring it would make cross-docking through
# any facility without the exact zone kind impossible. Temperature, permitted
# classes and segregation are safety properties of the goods and bind wherever
# they sit, capacity is capacity, and an offline zone is offline.
_CORE_STAGING_CONSTRAINTS: tuple[Constraint, ...] = (
    TempRangeConstraint(),
    ClassAllowedConstraint(),
    SegregationConstraint(),
    ZoneOfflineConstraint(),
    CapacityConstraint(),
)

# A zone declared as a cross-dock is what a facility uses to say "stage here".
# It is a ZONE kind, not the `cross-dock` facility tag: that tag is a capability
# consumed by REQUIRED_TAGS and names no storage location, so it cannot be
# booked. Facilities without such a zone still stage — in their cheapest
# compatible zone by id — they just do not get to express a preference.
CROSSDOCK_KINDS = ("cross-dock", "crossdock")

# How much of a zone's headroom this candidate's own earlier dwells have already
# taken, per (zone, UTC day bucket). Every dwell books the same shipment, so a
# count is the whole story.
_Pending = dict[tuple[str, date], int]


@dataclass(frozen=True)
class StagingResult:
    """Whether a facility can take the goods for one dwell, and why not."""

    zone_id: str | None
    closed_by: str | None = None  # FACILITY_CLOSED disruption over the dwell
    equipment_down: tuple[tuple[str, str], ...] = ()  # (required tag, disruption)
    no_window: bool = False  # the operating calendar never opens within the horizon
    zone_verdicts: dict[str, list[Reject]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.zone_id is not None


class StagingPlanner:
    """Per-shipment staging answers, memoized on (facility, window, own load)."""

    def __init__(
        self,
        state: NetworkState,
        shipment: Shipment,
        config: ObjectiveConfig,
        travel: TravelModel,
        now: datetime,
        packs: list[Pack],
        cache: EvalCache | None = None,
    ) -> None:
        self.state = state
        self.shipment = shipment
        self._ctx = AllocationContext(
            state=state,
            config=config,
            travel=travel,
            now=now,
            packs=packs,
            cache=cache,
        )
        self._constraints = [
            *_CORE_STAGING_CONSTRAINTS,
            *(c for pack in packs for c in pack.constraints if c.scope is ConstraintScope.ZONE),
        ]
        self._memo: dict[
            tuple[str, datetime, datetime, tuple[tuple[str, date, int], ...]], StagingResult
        ] = {}
        self._refusals: dict[Stop, StagingResult] = {}

    def stage(self, stops: Sequence[Stop]) -> tuple[list[Stop], Stop | None]:
        """Secure a staging zone for one candidate's stops, in travel order.

        THE `StagingChecker` the router routes against. The whole list is answered
        in one call because each dwell occupies the zone the next one asks for:
        evaluating them independently is how a single journey came to book a dock
        it had already filled, while the batch model — which sums both dwells onto
        one capacity row — refused the same candidate outright.
        """
        pending: _Pending = {}
        staged: list[Stop] = []
        for stop in stops:
            result = self.at(stop.facility_id, stop.arrive, stop.depart, pending)
            if result.zone_id is None:
                self._refusals[stop] = result
                return staged, stop
            staged.append(stop.model_copy(update={"zone_id": result.zone_id}))
            for day in buckets_between(stop.arrive, stop.depart):
                key = (result.zone_id, day)
                pending[key] = pending.get(key, 0) + 1
        return staged, None

    def explain(self, stop: Stop) -> StagingResult:
        """Why this stop refused the goods — the verdict `stage` actually reached,
        so a refusal caused by the candidate's own earlier dwell reads as the
        capacity rejection it was, not as a zone that looks free from outside."""
        found = self._refusals.get(stop)
        if found is None:
            found = self.at(stop.facility_id, stop.arrive, stop.depart, {})
        return found

    def at(
        self,
        facility_id: str,
        arrive: datetime,
        depart: datetime,
        pending: _Pending | None = None,
    ) -> StagingResult:
        own = self._own_load(facility_id, pending)
        key = (facility_id, arrive, depart, own)
        found = self._memo.get(key)
        if found is None:
            found = self._evaluate(facility_id, arrive, depart, pending or {})
            self._memo[key] = found
        return found

    def _own_load(
        self, facility_id: str, pending: _Pending | None
    ) -> tuple[tuple[str, date, int], ...]:
        """The part of the candidate's own load that can bind here — nothing, in
        the overwhelmingly common case, so the memo keeps hitting as before."""
        if not pending:
            return ()
        here = {zone.id for zone in self.state.zones_of(facility_id)}
        return tuple(
            sorted(
                (zone_id, day, count)
                for (zone_id, day), count in pending.items()
                if zone_id in here and count
            )
        )

    def _evaluate(
        self, facility_id: str, arrive: datetime, depart: datetime, pending: _Pending
    ) -> StagingResult:
        state = self.state
        facility = state.facilities[facility_id]
        # A degenerate dwell is an instant, not an empty interval (§7.9).
        window_end = depart if depart > arrive else arrive + timedelta(microseconds=1)
        # The router shifts a dwell to the facility's next opening (§6); a
        # facility with no window at all within the horizon can never take the
        # goods, and says so rather than being silently cross-docked through.
        if state.facility_next_open(facility_id, arrive) is None:
            return StagingResult(zone_id=None, no_window=True)
        active = state.disruptions_on(facility_id, arrive, window_end)
        # A closure means no automatic routing in, out or THROUGH while it lasts
        # — the goods must not be planned to sit at a shut facility for a minute.
        for disruption in active:
            if disruption.kind is DisruptionKind.FACILITY_CLOSED:
                return StagingResult(zone_id=None, closed_by=disruption.id)
        required = set(self.shipment.requirements.required_tags)
        down = tuple(
            (str(disruption.detail), disruption.id)
            for disruption in active
            if disruption.kind is DisruptionKind.EQUIPMENT_DOWN and disruption.detail in required
        )
        if down:
            return StagingResult(zone_id=None, equipment_down=down)
        ctx = self._ctx.with_stay(arrive, depart, route=None)
        if pending:
            ctx = ctx.with_pending(
                {key: self.shipment.size.times(count) for key, count in pending.items() if count}
            )
        verdicts: dict[str, list[Reject]] = {}
        passing: list[str] = []
        for zone in state.zones_of(facility_id):
            failing = [
                v
                for constraint in self._constraints
                if (v := constraint.check(self.shipment, facility, zone, ctx)) is not None
            ]
            if failing:
                verdicts[zone.id] = failing
            else:
                passing.append(zone.id)
        if not passing:
            return StagingResult(zone_id=None, zone_verdicts=verdicts)
        # Deterministic (§2): a declared cross-dock zone first, then by id.
        best = min(passing, key=lambda z: (state.zones[z].kind not in CROSSDOCK_KINDS, z))
        return StagingResult(zone_id=best, zone_verdicts=verdicts)
