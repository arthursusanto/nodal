"""Single-shipment allocation pipeline (§7): enumerate -> filter -> score -> explain -> commit.

`survey_candidates` produces the complete feasibility picture (every facility, every
zone, every verdict); `build_record` turns a survey plus a facility choice into a
full decision record. `allocate` is simply build_record with the objective's argmin
as the choice — baseline policies (§9) reuse the identical machinery with their own
choice, so hard constraints bind everyone and every record explains itself.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import metadata as importlib_metadata

import nodal
from nodal.allocate.config import ObjectiveConfig
from nodal.allocate.records import (
    BucketImpact,
    CapacityImpact,
    Chosen,
    ComponentScore,
    DecisionRecord,
    Hold,
    Itinerary,
    ItineraryLeg,
    LegSummary,
    RejectedFacility,
    RouteSummary,
    ScoredCandidate,
    StagingBooking,
)
from nodal.allocate.scorer import (
    EvalCache,
    ScoringPrecomputed,
    compute_components,
    precompute,
    total_score,
)
from nodal.allocate.staging import StagingPlanner, StagingResult
from nodal.domain.entities import (
    Assignment,
    LegSchedule,
    Reservation,
    Shipment,
    ShipmentStatus,
    Stop,
    StopRole,
)
from nodal.events import EventStore
from nodal.events import catalog as ev
from nodal.events.envelope import Envelope, EventDraft
from nodal.events.state import NetworkState, buckets_between
from nodal.network.journey import DeliveryFailure, DeliveryPlan, JourneyRouter
from nodal.network.travel import Leg, MatrixTravelModel, Route, RouteFailure
from nodal.rules.core import CORE_CONSTRAINTS, RequiredTagsConstraint
from nodal.rules.framework import (
    AllocationContext,
    Constraint,
    ConstraintScope,
    Pack,
    Reject,
    load_packs,
)


class AllocateError(Exception):
    pass


@dataclass(frozen=True)
class CandidateSurvey:
    """One facility's complete feasibility picture for one shipment."""

    facility_id: str
    route: Route | None  # None when routing failed entirely
    eta: datetime | None  # effective arrival (includes operating-window wait)
    departure: datetime | None
    wait_minutes: int
    facility_verdicts: list[Reject]
    zone_verdicts: dict[str, list[Reject]]
    passing_zones: list[str]
    # Every facility this candidate's journey occupies, in travel order, with the
    # window it sits there (§7.9): the pass-through stops carry the staging zone
    # the router secured, and the last one is the stay itself, whose zone the
    # survey below chooses. Ordinary allocations have these too — an intermediate
    # hop is a dwell whoever is paying for it.
    stops: tuple[Stop, ...] = ()
    # A->B delivery (§7.9): the routing beyond the hold at this facility. None for
    # ordinary shipments, and for candidates whose inbound routing already failed.
    delivery: DeliveryPlan | None = None

    @property
    def feasible(self) -> bool:
        return (
            self.route is not None
            and self.eta is not None
            and not self.facility_verdicts
            and bool(self.passing_zones)
        )

    @property
    def staging_stops(self) -> tuple[Stop, ...]:
        """The dwells this candidate books space for; the stay books its own."""
        return tuple(stop for stop in self.stops if stop.books_staging)


def _route_summary(route: Route, wait_minutes: int) -> RouteSummary:
    return RouteSummary(
        legs=[
            LegSummary(
                from_label=leg.from_label,
                to_facility_id=leg.to_facility_id,
                lane_id=leg.lane_id,
                minutes=leg.minutes,
                km=leg.km,
                cost_cents=leg.cost_cents,
            )
            for leg in route.legs
        ],
        minutes=route.minutes,
        wait_minutes=wait_minutes,
        km=round(route.km, 1),
        cost_cents=route.cost_cents,
        transfers=route.transfers,
    )


def _scheduled(
    state: NetworkState, legs: Sequence[Leg], departs: Sequence[datetime]
) -> list[ItineraryLeg]:
    """Legs on the clock the router put them on — which is not `start + sum of
    minutes` once a dwell has waited for a facility's operating window (§6)."""
    return [
        ItineraryLeg(
            kind=state.lanes[leg.lane_id].mode if leg.lane_id is not None else "road",
            from_label=leg.from_label,
            to_label=leg.to_facility_id,
            lane_id=leg.lane_id,
            km=leg.km,
            minutes=leg.minutes,
            cost_cents=leg.cost_cents,
            depart=depart,
            arrive=depart + timedelta(minutes=leg.minutes),
        )
        for leg, depart in zip(legs, departs, strict=True)
    ]


def _itinerary(
    state: NetworkState,
    delivery: DeliveryPlan,
    stops: Sequence[Stop],
    facility_id: str,
    zone_id: str,
    hold_start: datetime,
    hold_end: datetime,
) -> Itinerary:
    """The whole A->B movement as scheduled legs (§7.9). Inbound legs run from
    the shipment's departure; the hold absorbs any wait for the facility's
    operating window; outbound legs roll the moment the hold ends."""
    legs = _scheduled(state, delivery.inbound.legs, delivery.inbound.departs)
    legs.extend(_scheduled(state, delivery.outbound_lanes, delivery.outbound_departs))
    last = delivery.last_mile
    legs.append(
        ItineraryLeg(
            kind="road",
            from_label=last.from_label,
            to_label=last.to_label,
            lane_id=None,
            km=last.km,
            minutes=last.minutes,
            cost_cents=last.cost_cents,
            depart=delivery.last_mile_depart,
            arrive=delivery.delivered_at,
        )
    )
    return Itinerary(
        legs=legs,
        hold=Hold(facility_id=facility_id, zone_id=zone_id, from_ts=hold_start, until_ts=hold_end),
        destination=last.to_label,
        delivered_at=delivery.delivered_at,
        cost_cents=delivery.inbound.cost_cents + delivery.outbound_cost_cents,
        stops=list(stops),
    )


def _staging_bookings(stops: Sequence[Stop], base_reservation_id: str) -> list[StagingBooking]:
    """One reservation per pass-through stop, ids derived from the hold's so they
    stay unique across a shipment's re-allocations (§5)."""
    bookings: list[StagingBooking] = []
    for index, stop in enumerate(s for s in stops if s.books_staging):
        assert stop.zone_id is not None, "a staging stop always carries its zone"
        bookings.append(
            StagingBooking(
                reservation_id=f"{base_reservation_id}-S{index + 1}",
                facility_id=stop.facility_id,
                zone_id=stop.zone_id,
                from_ts=stop.arrive,
                until_ts=stop.depart,
                role=stop.role.value,
            )
        )
    return bookings


def chosen_of(
    state: NetworkState,
    shipment: Shipment,
    survey: CandidateSurvey,
    zone_id: str,
    reservation_id: str,
) -> Chosen:
    """THE chosen candidate every commit path records (§7.5).

    One function so single allocate, batch, re-optimization and what-if cannot
    disagree about what a decision books: the stops it occupies, the schedule its
    legs run on, the staging reservations those stops imply, and — for a delivery
    — the itinerary through to the customer.
    """
    assert survey.route is not None and survey.eta is not None and survey.departure is not None
    stops = [
        stop.model_copy(update={"zone_id": zone_id}) if stop.role is StopRole.HOLD else stop
        for stop in survey.stops
    ]
    itinerary = (
        _itinerary(
            state,
            survey.delivery,
            stops,
            survey.facility_id,
            zone_id,
            survey.eta,
            survey.departure,
        )
        if survey.delivery is not None
        else None
    )
    legs = (
        _scheduled(state, survey.route.legs, survey.route.departs)
        if itinerary is None
        else itinerary.legs
    )
    return Chosen(
        facility_id=survey.facility_id,
        zone_id=zone_id,
        route=_route_summary(survey.route, survey.wait_minutes),
        eta=survey.eta,
        departure=survey.departure,
        size=shipment.size,
        reservation_ids=[reservation_id],
        itinerary=itinerary,
        staging=_staging_bookings(stops, reservation_id),
        stops=stops,
        legs=[
            LegSchedule(lane_id=leg.lane_id, depart=leg.depart, arrive=leg.arrive)
            for leg in legs
            if leg.lane_id is not None
        ],
    )


def reservations_of(shipment_id: str, chosen: Chosen) -> list[Reservation]:
    """Every reservation a decision books: the hold, then each staging dwell
    (§7.9). One function so no commit path can place some and drop the rest."""
    reservations = [
        Reservation(
            id=reservation_id,
            zone_id=chosen.zone_id,
            size=chosen.size,
            from_ts=chosen.eta,
            until_ts=chosen.departure,
            holder=shipment_id,
        )
        for reservation_id in chosen.reservation_ids
    ]
    reservations.extend(
        Reservation(
            id=booking.reservation_id,
            zone_id=booking.zone_id,
            size=chosen.size,
            from_ts=booking.from_ts,
            until_ts=booking.until_ts,
            holder=shipment_id,
        )
        for booking in chosen.staging
    )
    return reservations


_CLOSURE_REJECT_ID = {
    StopRole.ENTRY: "ENTRY_CLOSED",
    StopRole.TRANSIT: "TRANSIT_CLOSED",
    StopRole.EXIT: "EXIT_CLOSED",
    StopRole.HOLD: "FACILITY_CLOSED",
}


def _staging_reject(facility_id: str, role: StopRole, result: StagingResult) -> Reject:
    """Why the goods may not dwell at this facility, named per role so a closed
    exit reads differently from a closed hold (§7.9)."""
    if result.closed_by is not None:
        return Reject(
            constraint_id=_CLOSURE_REJECT_ID[role],
            data={"facility": facility_id, "disruption": result.closed_by, "role": role.value},
        )
    if result.equipment_down:
        return Reject(
            constraint_id="STOP_EQUIPMENT_DOWN",
            data={
                "facility": facility_id,
                "role": role.value,
                "tags": sorted({tag for tag, _ in result.equipment_down}),
                "disruptions": sorted({d for _, d in result.equipment_down}),
            },
        )
    if result.no_window:
        return Reject(
            constraint_id="STOP_NO_OPERATING_WINDOW",
            data={"facility": facility_id, "role": role.value},
        )
    return Reject(
        constraint_id="STOP_NO_STAGING",
        data={
            "facility": facility_id,
            "role": role.value,
            "zones": sorted(result.zone_verdicts),
        },
    )


def _refused_stop_verdicts(planner: StagingPlanner, stops: Sequence[Stop]) -> list[Reject]:
    """Name why each refused stop refused the goods, from the verdict the planner
    actually reached when it refused them."""
    return [_staging_reject(stop.facility_id, stop.role, planner.explain(stop)) for stop in stops]


def assignment_of(chosen: Chosen) -> Assignment:
    """The assignment an `AllocationDecided` carries for a chosen candidate.

    Every commit path (single, batch, re-optimization, what-if) goes through this
    one function, so the delivery fields can never be recorded by some of them and
    dropped by the rest. `route` is the INBOUND half — the transport that books the
    hold (§7.9) — and the outbound half is stashed alongside it so re-optimization
    can see the whole journey in folded state.
    """
    inbound = [leg.lane_id for leg in chosen.route.legs if leg.lane_id is not None]
    itinerary = chosen.itinerary
    # The itinerary runs inbound half then outbound half, so its lanes past the
    # inbound ones are exactly the lanes travelled after the hold.
    outbound = itinerary.lane_ids[len(inbound) :] if itinerary is not None else []
    return Assignment(
        facility_id=chosen.facility_id,
        zone_ids=[chosen.zone_id],
        route=inbound,
        eta=chosen.eta,
        expected_departure=chosen.departure,
        reservation_ids=[
            *chosen.reservation_ids,
            *(booking.reservation_id for booking in chosen.staging),
        ],
        outbound_route=outbound,
        exit_facility_id=itinerary.exit_facility_id if itinerary is not None else None,
        stops=list(chosen.stops),
        legs=list(chosen.legs),
    )


def _constraints(packs: list[Pack]) -> list[Constraint]:
    result: list[Constraint] = list(CORE_CONSTRAINTS)
    for pack in packs:
        result.extend(pack.constraints)
    return result


def survey_candidates(
    state: NetworkState,
    shipment: Shipment,
    config: ObjectiveConfig,
    now: datetime,
    packs: list[Pack] | None = None,
    cache: EvalCache | None = None,
) -> list[CandidateSurvey]:
    """Every facility, unconditionally (§7.1). Routing failures become verdicts,
    never silent skips; stay-free constraints still run for unroutable candidates."""
    travel = MatrixTravelModel(config.travel)
    if packs is None:
        packs = load_packs(config.packs)
    constraints = _constraints(packs)
    facility_constraints = [c for c in constraints if c.scope is ConstraintScope.FACILITY]
    zone_constraints = [c for c in constraints if c.scope is ConstraintScope.ZONE]
    base_ctx = AllocationContext(
        state=state, config=config, travel=travel, now=now, packs=packs, cache=cache
    )
    depart_at = max(shipment.ready_at, now)
    # No automatic routing OUT of a closed facility (§7.9). A departure from the
    # origin is not a stop, so the stop machinery never saw it and the solver
    # happily booked goods out of a shut yard. It is a property of the shipment,
    # not of any candidate: every facility is rejected, by name, and the reason
    # carries the closure's end so an operator knows when it becomes routable.
    # Deferring the departure past the closure is deliberately NOT done — a human
    # decides when trapped goods move, not the optimizer.
    closure = state.departure_closure(shipment.origin_facility_id, depart_at)
    if closure is not None:
        origin_closed = Reject(
            constraint_id="ORIGIN_CLOSED",
            data={
                "facility": shipment.origin_facility_id,
                "disruption": closure.id,
                "until": (closure.ended_at or closure.until_ts).isoformat(),
                "depart": depart_at.isoformat(),
            },
        )
        return [
            CandidateSurvey(
                facility_id=facility_id,
                route=None,
                eta=None,
                departure=None,
                wait_minutes=0,
                facility_verdicts=[origin_closed],
                zone_verdicts={},
                passing_zones=[],
            )
            for facility_id in sorted(state.facilities)
        ]
    declared_dwell = shipment.dwell_days
    dwell_days = declared_dwell if declared_dwell is not None else config.default_dwell_days
    # ONE router for every shipment (§7.9): an ordinary allocation is routed
    # origin -> hold, a delivery origin -> hold -> customer, and both go through
    # the same check-and-re-relax machinery, so an intermediate facility is a
    # checked, booked stop whichever kind of decision is passing through it. One
    # staging planner answers every stop that routing has to check on the way.
    staging = StagingPlanner(state, shipment, config, travel, now, packs, cache)
    router = JourneyRouter(state, shipment, travel, config.road, depart_at, staging.stage)
    delivering = shipment.destination is not None

    surveys: list[CandidateSurvey] = []
    for facility_id in sorted(state.facilities):
        facility = state.facilities[facility_id]
        routed = router.inbound(facility_id)
        if isinstance(routed, RouteFailure):
            if routed.reason == "stops_refused":
                # Every remaining way in dwells somewhere the goods may not sit.
                verdicts = _refused_stop_verdicts(staging, routed.refused_stops)
            elif routed.reason == "blocked":
                verdicts = [
                    Reject(constraint_id="LANE_BLOCKED", data={"blocked": [*routed.blocked_lanes]})
                ]
            else:
                verdicts = [
                    Reject(
                        constraint_id="NO_ROUTE",
                        data={"origin": shipment.origin_facility_id or shipment.origin_label},
                    )
                ]
            tags = RequiredTagsConstraint().check(shipment, facility, None, base_ctx)
            if tags is not None:
                verdicts.append(tags)
            surveys.append(
                CandidateSurvey(
                    facility_id=facility_id,
                    route=None,
                    eta=None,
                    departure=None,
                    wait_minutes=0,
                    facility_verdicts=verdicts,
                    zone_verdicts={},
                    passing_zones=[],
                )
            )
            continue
        physical_arrival = depart_at + timedelta(
            minutes=routed.minutes + routed.transit_wait_minutes
        )
        effective = state.facility_next_open(facility_id, physical_arrival)
        if effective is None:
            verdicts = [Reject(constraint_id="NO_OPERATING_WINDOW", data={"facility": facility_id})]
            tags = RequiredTagsConstraint().check(shipment, facility, None, base_ctx)
            if tags is not None:
                verdicts.append(tags)
            surveys.append(
                CandidateSurvey(
                    facility_id=facility_id,
                    route=routed,
                    eta=None,
                    departure=None,
                    wait_minutes=0,
                    facility_verdicts=verdicts,
                    zone_verdicts={},
                    passing_zones=[],
                )
            )
            continue
        # Waiting for a transit facility's window is time on the journey exactly
        # as waiting at the destination is: both are priced by `travel_time` (§7.3).
        wait_minutes = routed.transit_wait_minutes + round(
            (effective - physical_arrival).total_seconds() / 60
        )
        departure = effective + timedelta(days=dwell_days)
        delivery: DeliveryPlan | None = None
        # The stay is the last stop of an ordinary journey; a delivery's stops run
        # on past it, and `complete` returns the whole ordered list.
        stops: tuple[Stop, ...] = (
            *routed.stops,
            Stop(facility_id=facility_id, role=StopRole.HOLD, arrive=effective, depart=departure),
        )
        if delivering:
            planned = router.complete(facility_id, routed, effective, departure)
            if isinstance(planned, DeliveryFailure):
                # Every remaining way to the customer dwells somewhere the goods
                # may not sit: this holding facility cannot deliver at all (§7.9).
                verdicts = _refused_stop_verdicts(staging, planned.stops)
                surveys.append(
                    CandidateSurvey(
                        facility_id=facility_id,
                        route=routed,
                        eta=None,
                        departure=None,
                        wait_minutes=wait_minutes,
                        facility_verdicts=verdicts
                        or [Reject(constraint_id="NO_ROUTE", data={"origin": facility_id})],
                        zone_verdicts={},
                        passing_zones=[],
                    )
                )
                continue
            delivery = planned
            stops = planned.stops
        ctx = base_ctx.with_stay(effective, departure, routed, delivery)

        facility_verdicts = [
            v
            for constraint in facility_constraints
            if (v := constraint.check(shipment, facility, None, ctx)) is not None
        ]
        zone_verdicts: dict[str, list[Reject]] = {}
        passing_zones: list[str] = []
        for zone in state.zones_of(facility_id):
            failing = [
                v
                for constraint in zone_constraints
                if (v := constraint.check(shipment, facility, zone, ctx)) is not None
            ]
            if failing:
                zone_verdicts[zone.id] = failing
            else:
                passing_zones.append(zone.id)
        surveys.append(
            CandidateSurvey(
                facility_id=facility_id,
                route=routed,
                eta=effective,
                departure=departure,
                wait_minutes=wait_minutes,
                facility_verdicts=facility_verdicts,
                zone_verdicts=zone_verdicts,
                passing_zones=passing_zones,
                stops=stops,
                delivery=delivery,
            )
        )
    return surveys


def _score_facility(
    state: NetworkState,
    shipment: Shipment,
    survey: CandidateSurvey,
    config: ObjectiveConfig,
    pre: ScoringPrecomputed,
    cache: EvalCache | None = None,
    packs: Sequence[Pack] = (),
) -> ScoredCandidate:
    """Score every eligible zone by the full objective; the facility's candidate is
    its best zone (§7.2 zone-granularity — no proxy metrics, no alphabet)."""
    assert survey.route is not None and survey.eta is not None and survey.departure is not None
    facility = state.facilities[survey.facility_id]
    best: tuple[float, str, dict[str, ComponentScore]] | None = None
    for zone_id in survey.passing_zones:
        components = compute_components(
            state,
            shipment,
            facility,
            state.zones[zone_id],
            survey.route,
            survey.wait_minutes,
            survey.eta,
            survey.departure,
            config,
            pre,
            cache,
            packs=packs,
            delivery=survey.delivery,
        )
        total = round(total_score(components), 9)
        if best is None or (total, zone_id) < (best[0], best[1]):
            best = (total, zone_id, components)
    assert best is not None
    return ScoredCandidate(
        facility_id=survey.facility_id,
        zone_id=best[1],
        route=_route_summary(survey.route, survey.wait_minutes),
        eta=survey.eta,
        departure=survey.departure,
        components=best[2],
        total=best[0],
        zone_verdicts=survey.zone_verdicts,
    )


def build_record(
    state: NetworkState,
    shipment: Shipment,
    config: ObjectiveConfig,
    now: datetime,
    surveys: list[CandidateSurvey],
    choice: str | None = None,
    policy: str = "nodal-single",
    cache: EvalCache | None = None,
    packs: list[Pack] | None = None,
) -> DecisionRecord:
    """Assemble the §7.5 record. `choice=None` takes the objective's argmin; a
    policy may name any *feasible* facility instead — zone selection and scoring
    stay identical, so records are comparable across policies."""
    if packs is None:
        packs = load_packs(config.packs)
    pre = precompute(state, shipment, now)
    if cache is None:
        cache = EvalCache(state)
    rejected: list[RejectedFacility] = []
    scored: list[ScoredCandidate] = []
    for survey in surveys:
        if survey.feasible:
            scored.append(_score_facility(state, shipment, survey, config, pre, cache, packs=packs))
        else:
            facility_verdicts = list(survey.facility_verdicts)
            if not facility_verdicts:
                facility_verdicts = [
                    Reject(constraint_id="NO_ELIGIBLE_ZONE", data={"facility": survey.facility_id})
                ]
            rejected.append(
                RejectedFacility(
                    facility_id=survey.facility_id,
                    facility_verdicts=facility_verdicts,
                    zone_verdicts=survey.zone_verdicts,
                )
            )
    scored.sort(key=lambda c: (c.total, c.facility_id, c.zone_id))

    chosen_candidate: ScoredCandidate | None = None
    if choice is not None:
        chosen_candidate = next((c for c in scored if c.facility_id == choice), None)
        if chosen_candidate is None:
            raise AllocateError(f"policy chose {choice}, which is not feasible")
    elif scored:
        chosen_candidate = scored[0]

    # §7.5: beyond the configured top-K, non-chosen candidates keep their totals
    # and route sums but drop per-component detail and leg lists — records stay
    # bounded. The chosen candidate always keeps full detail.
    if len(scored) > config.top_k_detail:
        scored = [
            candidate
            if rank < config.top_k_detail or candidate is chosen_candidate
            else candidate.model_copy(
                update={
                    "components": {},
                    "route": candidate.route.model_copy(update={"legs": []}),
                }
            )
            for rank, candidate in enumerate(scored)
        ]

    chosen: Chosen | None = None
    capacity_impact: CapacityImpact | None = None
    if chosen_candidate is not None:
        reservation_id = f"RES-{shipment.id}-{shipment.allocation_seq + 1}"
        chosen_survey = next(s for s in surveys if s.facility_id == chosen_candidate.facility_id)
        chosen = chosen_of(state, shipment, chosen_survey, chosen_candidate.zone_id, reservation_id)
        buckets = []
        for day in buckets_between(chosen_candidate.eta, chosen_candidate.departure):
            occupancy = state.occupancy(chosen_candidate.zone_id, day)
            buckets.append(
                BucketImpact(
                    day=day,
                    capacity=state.effective_capacity(chosen_candidate.zone_id, day),
                    occupancy_before=occupancy,
                    occupancy_after=occupancy.plus(shipment.size),
                )
            )
        capacity_impact = CapacityImpact(zone_id=chosen_candidate.zone_id, buckets=buckets)

    return DecisionRecord(
        shipment_id=shipment.id,
        decided_at=now,
        mode="single",
        policy=policy,
        based_on_seq=state.last_seq,
        config_snapshot=config.model_dump(mode="json"),
        engine_version=nodal.__version__,
        tzdata_version=importlib_metadata.version("tzdata"),
        considered=sorted(state.facilities),
        rejected=rejected,
        scored=scored,
        chosen=chosen,
        capacity_impact=capacity_impact,
    )


def allocate(
    state: NetworkState,
    shipment_id: str,
    config: ObjectiveConfig,
    now: datetime | None = None,
) -> DecisionRecord:
    """Pure decision: reads state, returns the full record. Committing is separate."""
    shipment = state.shipments.get(shipment_id)
    if shipment is None:
        raise AllocateError(f"unknown shipment {shipment_id}")
    if shipment.status is not ShipmentStatus.PLANNED:
        raise AllocateError(f"shipment {shipment_id} is {shipment.status.value}, not planned")
    if now is None:
        now = state.last_ts
    if now is None:
        raise AllocateError("state is empty; no decision time available")
    packs = load_packs(config.packs)
    cache = EvalCache(state)
    surveys = survey_candidates(state, shipment, config, now, packs=packs, cache=cache)
    return build_record(
        state, shipment, config, now, surveys, choice=None, cache=cache, packs=packs
    )


def superseding_discard(
    pending: "ev.PlanDrafted | None", ts: datetime, by: str
) -> list[EventDraft]:
    """The `PlanDiscarded` a command must append ALONGSIDE the events that stale
    a drafted plan (§7.5).

    The fold already stops treating a draft as pending the moment anything lands
    on top of it, so this changes no read model. What it changes is the audit:
    without it the log reads "the optimizer proposed a plan, and then nothing",
    with no record of the proposal ever having been resolved. Prepending it to
    the SAME atomic append means the draft is never un-terminated, not even for
    one event. Terminating events — the plan's own commit, another draft, an
    explicit discard — say it themselves and pass `None` here.
    """
    if pending is None:
        return []
    return [
        EventDraft(
            ts=ts,
            payload=ev.PlanDiscarded(batch_id=pending.batch_id, reason=f"superseded by {by}"),
        )
    ]


def commit(
    store: EventStore,
    record: DecisionRecord,
    actor: str = "cli",
    pending_plan: "ev.PlanDrafted | None" = None,
) -> list[Envelope]:
    """Append the decision and its reservations atomically. Refuses stale records:
    the store head must still be the state the decision read (§7.5 audit safety —
    a concurrent commit would otherwise mint colliding reservation ids).

    `pending_plan` is the caller's `state.pending_plan`: booking one shipment by
    hand stales any drafted batch, and the discard that says so rides along in
    this same append (§7.5)."""
    if record.chosen is None:
        raise AllocateError(f"decision for {record.shipment_id} chose no destination")
    head = store.last_seq()
    if head != record.based_on_seq:
        raise AllocateError(
            f"stale decision: based on seq {record.based_on_seq} but the log head is "
            f"{head}; re-run allocate against current state"
        )
    chosen = record.chosen
    assignment = assignment_of(chosen)
    drafts = superseding_discard(pending_plan, record.decided_at, "AllocationDecided")
    drafts.append(
        EventDraft(
            ts=record.decided_at,
            payload=ev.AllocationDecided(
                shipment_id=record.shipment_id,
                assignment=assignment,
                record=record.as_event_record(),
            ),
        )
    )
    for reservation in reservations_of(record.shipment_id, chosen):
        drafts.append(
            EventDraft(
                ts=record.decided_at,
                payload=ev.ReservationPlaced(reservation=reservation),
                cause=f"decision:{record.shipment_id}",
            )
        )
    return store.append(drafts, actor=actor)


__all__ = [
    "AllocateError",
    "CandidateSurvey",
    "allocate",
    "assignment_of",
    "build_record",
    "commit",
    "reservations_of",
    "superseding_discard",
    "survey_candidates",
]
