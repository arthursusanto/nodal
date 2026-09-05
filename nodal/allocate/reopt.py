"""Tiered re-optimization under disruption (§7.6).

Tier 1 re-solves only the affected set — allocated shipments whose assignment,
route, or timing the disruption touches — with everything else frozen and a
churn penalty on every change. If tier 1 leaves affected shipments unassigned,
or its objective degrades past the configured threshold, tier 2 expands the set
to the reservation holders on the capacity rows the tier-1 solve contends for
(hop-capped) so a closure can displace incumbents at the second-best facility
instead of dumping shipments into unassigned while feasible chains exist.

The solve runs on a deep-copied overlay whose affected reservations are folded
away; the real log gets ONE atomic append: per changed shipment an
`AllocationSuperseded` (old -> new audit link) followed by the new
`AllocationDecided` + `ReservationPlaced`. Shipments that keep their incumbent
produce no events at all. Only ALLOCATED (not yet departed) shipments are
re-plannable; in-transit and later shipments are physical facts.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from nodal.allocate.batch import (
    BatchResult,
    incumbent_objective_scaled,
    solve_batch,
)
from nodal.allocate.config import ObjectiveConfig
from nodal.allocate.engine import AllocateError, assignment_of, reservations_of
from nodal.allocate.records import DecisionRecord
from nodal.domain.capacity import DIMENSIONS
from nodal.domain.entities import (
    Assignment,
    Disruption,
    DisruptionKind,
    Shipment,
    ShipmentStatus,
    Stop,
    StopRole,
)
from nodal.domain.units import OBJECTIVE_SCALE
from nodal.events import EventStore, fold
from nodal.events import catalog as ev
from nodal.events.envelope import Envelope, EventDraft
from nodal.events.state import NetworkState
from nodal.rules.framework import Pack, load_packs


@dataclass(frozen=True)
class TrappedCargo:
    """Goods that were already at a facility when it shut (§7.9). Re-planning
    them would be a lie — nothing can move them out while the closure lasts — so
    they keep their booking and an operator clears them by hand (cancel/transfer)."""

    shipment_id: str
    facility_id: str
    disruption_id: str


@dataclass(frozen=True)
class ReoptResult:
    """What a re-optimization did. `tier=0`: nothing was affected."""

    tier: int
    trigger: str  # disruption id
    affected: list[str]  # shipments re-solved (final, post-escalation set)
    changed: list[str]  # subset whose assignment actually changed (events appended)
    released: list[str]  # subset left unassigned (superseded, back to planned)
    result: BatchResult | None  # records carry reopt_tier / reopt_trigger
    envelopes: list[Envelope]  # appended events; empty if nothing changed
    # Physically-present cargo a closure stranded: excluded from this re-solve
    # whatever triggered it, surfaced for manual clearing, and never silently
    # re-routed OR released. Each entry names the closure holding the cargo,
    # which is not necessarily `trigger`.
    trapped: list[TrappedCargo] = field(default_factory=list)


def _stops_of(assignment: Assignment) -> list[Stop]:
    """Every window the goods occupy a facility for. Pre-extension assignments
    carry no stops, so the hold stands in — exactly the old behaviour."""
    if assignment.stops:
        return list(assignment.stops)
    stops = [
        Stop(
            facility_id=assignment.facility_id,
            role=StopRole.HOLD,
            arrive=assignment.eta,
            depart=assignment.expected_departure,
            zone_id=assignment.zone_ids[0] if assignment.zone_ids else None,
        )
    ]
    if assignment.exit_facility_id not in (None, assignment.facility_id):
        # The one instant an old log could offer for the way out: the hold's end.
        stops.append(
            Stop(
                facility_id=str(assignment.exit_facility_id),
                role=StopRole.EXIT,
                arrive=assignment.expected_departure,
                depart=assignment.expected_departure,
            )
        )
    return stops


def _touched_stops(assignment: Assignment, disruption: Disruption) -> list[Stop]:
    """Stops at the disruption's target facility whose window it overlaps — the
    SAME predicate the planner rejects candidates with (§7.9), so what planning
    refuses to book is exactly what re-optimization notices."""
    return [
        stop
        for stop in _stops_of(assignment)
        if stop.facility_id == disruption.target_id
        and disruption.overlaps(stop.arrive, stop.depart)
    ]


def _lane_touched(assignment: Assignment, disruption: Disruption) -> bool:
    """A block matched against the leg it actually blocks. The folded schedule
    gives each lane its OWN departure; without it (pre-extension logs) fall back
    to the two probe instants the assignment can still offer."""
    lane_id = disruption.target_id
    if assignment.legs:
        return any(
            leg.lane_id == lane_id and disruption.active_at(leg.depart) for leg in assignment.legs
        )
    return lane_id in assignment.outbound_route and disruption.active_at(
        assignment.expected_departure
    )


_PlanSignature = tuple[
    str,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    str | None,
    tuple[tuple[str, str, str | None, date, date], ...],
]


def _plan_signature(assignment: Assignment) -> _PlanSignature:
    """What makes two plans the same plan (§7.6 staleness).

    Identity — hold, zones, lanes in and out, exit — compares exactly. Windows
    compare at the granularity the booking itself has: the UTC day buckets a stop
    occupies (§5). Re-optimizing later than the original decision legitimately
    shifts every clock reading by the gap between the two planning instants; that
    is not a changed plan, and treating it as one would re-commit the whole
    affected set on every disruption and defeat the churn penalty. A shift big
    enough to move the reservation onto different days IS a changed plan.
    """
    return (
        assignment.facility_id,
        tuple(assignment.zone_ids),
        tuple(assignment.route),
        tuple(assignment.outbound_route),
        assignment.exit_facility_id,
        tuple(
            (
                stop.facility_id,
                stop.role.value,
                stop.zone_id,
                stop.arrive.astimezone(UTC).date(),
                stop.depart.astimezone(UTC).date(),
            )
            for stop in _stops_of(assignment)
        ),
    )


def _origin_departure_blocked(shipment: Shipment, disruption: Disruption, now: datetime) -> bool:
    """Does this closure shut the shipment's own origin at the instant it would
    roll out of it? THE §7.9 origin-departure predicate, at the same instant the
    planner uses (`max(ready_at, now)`), so the affected set, the trapped set and
    the `ORIGIN_CLOSED` rejection all answer one question the same way."""
    origin = shipment.origin_facility_id
    return origin is not None and disruption.blocks_departure_from(
        origin, max(shipment.ready_at, now)
    )


def affected_shipments(
    state: NetworkState,
    disruption: Disruption,
    now: datetime,
) -> list[str]:
    """Tier-1 affected set: allocated shipments the disruption touches (§7.6)."""
    affected: set[str] = set()
    if disruption.kind is DisruptionKind.CAPACITY_REDUCED:
        affected.update(_overcommitted_holders(state, disruption, now))
    for shipment in state.shipments.values():
        if shipment.status is not ShipmentStatus.ALLOCATED or shipment.assigned is None:
            continue
        a = shipment.assigned
        kind = disruption.kind
        if kind is DisruptionKind.SHIPMENT_DELAYED:
            if disruption.target_id == shipment.id:
                affected.add(shipment.id)
        elif kind in (DisruptionKind.FACILITY_CLOSED, DisruptionKind.EQUIPMENT_DOWN):
            # Every stop counts, not just the hold: a delivery enters, transfers
            # and leaves through facilities it never stays at (§7.9), and each of
            # those windows is matched against the disruption's own. The one
            # touch that is NOT a stop is the departure out of the shipment's own
            # origin, so a closure that only blocks THAT instant — a plan whose
            # readiness has slipped past its own recorded eta — is matched by the
            # origin-departure predicate instead of going unnoticed.
            if _touched_stops(a, disruption) or _origin_departure_blocked(
                shipment, disruption, now
            ):
                affected.add(shipment.id)
        elif kind is DisruptionKind.ZONE_OFFLINE:
            if any(
                stop.zone_id == disruption.target_id
                and disruption.overlaps(stop.arrive, stop.depart)
                for stop in _stops_of(a)
            ):
                affected.add(shipment.id)
        elif kind is DisruptionKind.LANE_BLOCKED:
            # Blocked-at-departure approximation (§7.2): the shipment has not
            # departed, so a block active any time up to its eta may catch the
            # INBOUND half. Past that, each leg is matched against its own
            # departure rather than one probe at the hold's end (§7.9).
            on_inbound = disruption.target_id in a.route and disruption.overlaps(now, a.eta)
            if on_inbound or _lane_touched(a, disruption):
                affected.add(shipment.id)
    return sorted(affected)


def trapped_cargo(
    state: NetworkState,
    disruption: Disruption,
    now: datetime,
) -> list[TrappedCargo]:
    """Which cargo standing at a closing facility it physically strands.

    Cargo is trapped when it is already inside the facility the closure lands on
    AND the closure is shut at the moment those goods are due to LEAVE it:
    nothing a solver decides can move them then, so re-planning would produce a
    booking that cannot be executed. A shipment fed in from an external gate has
    no goods inside the network yet and is therefore never trapped, and every
    stop still ahead of a shipment is a future booking that IS re-planned around
    the closure. Two populations qualify, both keyed on an in-network origin:

    - ALLOCATED (§7.6 re-plannable — they have not departed): the dwell they are
      in is the one at their origin facility, which began before `now` by
      definition, so what decides is whether the door is shut when they roll.
    - PLANNED: nothing has been booked for them at all, and the same closure that
      makes every candidate ORIGIN_CLOSED (§7.9) is why. Flagging them is what
      tells an operator why a solve leaves them sitting there.

    Both are therefore the ONE origin-departure predicate at the ONE instant the
    planner would roll the goods out, `max(ready_at, now)`. Anything wider is a
    different question — "was this facility shut at some point while the goods
    happened to be booked" — and answering that one strands cargo a closure never
    touched: a shipment whose readiness slipped past a long-finished closure kept
    a trapped badge for good, and the badge then froze it, so a later closure of
    the facility it was booked INTO could not move it out.
    """
    if disruption.kind is not DisruptionKind.FACILITY_CLOSED:
        return []
    trapped: list[TrappedCargo] = []
    for sid in sorted(state.shipments):
        shipment = state.shipments[sid]
        if shipment.origin_facility_id != disruption.target_id:
            continue
        if not _origin_departure_blocked(shipment, disruption, now):
            continue
        if shipment.status is ShipmentStatus.ALLOCATED:
            if shipment.assigned is None:
                continue
        elif shipment.status is not ShipmentStatus.PLANNED:
            continue
        trapped.append(
            TrappedCargo(
                shipment_id=sid,
                facility_id=disruption.target_id,
                disruption_id=disruption.id,
            )
        )
    return trapped


def all_trapped_cargo(state: NetworkState, now: datetime) -> list[TrappedCargo]:
    """Every shipment any live closure has stranded, as of `now`.

    Being trapped is a property of the CARGO and the closure holding it, not of
    whichever disruption happens to be under consideration: a re-solve triggered
    by a lane block must leave stranded goods exactly as untouched as a re-solve
    triggered by the closure itself does. Releasing a trapped shipment's booking
    is an automatic change too — for cargo standing in the shut facility the
    booking IS its physical occupancy, and for cargo booked onward the
    reservation is held until a human cancels it or the facility reopens.

    One entry per shipment, attributed to the lowest-id closure that strands it,
    so overlapping closures still give a deterministic answer.
    """
    stranded: dict[str, TrappedCargo] = {}
    for disruption_id in sorted(state.disruptions):
        disruption = state.disruptions[disruption_id]
        # A pre-filter, never a second opinion: every instant `trapped_cargo`
        # tests is at or after `now`, so a disruption already over by `now` can
        # strand nothing and this skips work without changing the answer. It has
        # to stay implied by the predicate — a union that disagreed with the
        # per-closure call is exactly the divergence this function exists to end.
        if (disruption.ended_at or disruption.until_ts) <= now:
            continue
        for entry in trapped_cargo(state, disruption, now):
            stranded.setdefault(entry.shipment_id, entry)
    return [stranded[sid] for sid in sorted(stranded)]


def _overcommitted_holders(state: NetworkState, disruption: Disruption, now: datetime) -> set[str]:
    """Holders of reservations on buckets the reduced zone can no longer cover."""
    zone_id = disruption.target_id
    if zone_id not in state.zones:
        return set()
    holders: set[str] = set()
    day = max(disruption.from_ts.astimezone(UTC).date(), now.astimezone(UTC).date())
    last = disruption.until_ts.astimezone(UTC).date()
    while day <= last:
        effective = state.effective_capacity(zone_id, day)
        occupancy = state.occupancy(zone_id, day)
        over = any(
            (cap := effective.get(dim)) is not None and occupancy.demand(dim) > cap
            for dim in DIMENSIONS
        )
        if over:
            for reservation in state.reservations_on_zone(zone_id):
                if reservation.from_ts.date() <= day <= reservation.until_ts.date():
                    holder = state.shipments.get(reservation.holder)
                    if holder is not None and holder.status is ShipmentStatus.ALLOCATED:
                        holders.add(reservation.holder)
        day += timedelta(days=1)
    return holders


def _fold_supersedes(overlay: NetworkState, shipment_ids: list[str], now: datetime) -> None:
    """Release the affected incumbents on the overlay so the solve sees their
    capacity as free. Synthetic envelopes; the real log is untouched."""
    for i, sid in enumerate(shipment_ids):
        payload = ev.AllocationSuperseded(shipment_id=sid, old_decision_seq=0, reason="overlay")
        fold(
            [
                Envelope(
                    seq=overlay.last_seq + 1,
                    id=f"EVT-reopt-overlay-{i}",
                    ts=now,
                    type=payload.EVENT_TYPE,
                    entity_type="shipment",
                    entity_id=sid,
                    payload=payload,
                    actor="reopt",
                )
            ],
            into=overlay,
        )


def _capacity_connected(
    state: NetworkState, result: BatchResult, exclude: set[str], stranded: set[str]
) -> tuple[set[str], set[str]]:
    """Tier-2 expansion (§7.6): allocated holders of reservations on capacity
    the tier-1 solve contends for — its binding rows, and every zone that
    CAPACITY-rejected a shipment the solve left unassigned (a full zone never
    becomes a candidate, so it can never show up as a binding row).

    Returns the expansion and, separately, the stranded shipments it WOULD have
    pulled in. Trapped cargo is never re-solved (§7.9), and the expansion is the
    one path that could re-derive it: it reads reservation holders straight off
    the state, which still names the stranded shipment the tier-1 set removed.
    Escalating onto it would find it infeasible everywhere (its origin is shut),
    leave it unassigned, and release the reservation that IS its physical
    occupancy. Naming it instead is what tells the operator the chain the solve
    could not follow.
    """
    contended: set[tuple[str, date]] = set()
    contended_zones: set[str] = set()
    for sid, record in result.records.items():
        context = record.batch_context
        if context is not None:
            for row in context.binding_constraints:
                zone_id, _dim, day_text = row.split("/")
                contended.add((zone_id, date.fromisoformat(day_text)))
        if result.assignments.get(sid) is None:
            for rejection in record.rejected:
                for zone_id, verdicts in rejection.zone_verdicts.items():
                    if any(v.constraint_id == "CAPACITY" for v in verdicts):
                        contended_zones.add(zone_id)
    expansion: set[str] = set()
    held_back: set[str] = set()

    def add_holder(reservation_holder: str) -> None:
        if reservation_holder in exclude:
            return
        holder = state.shipments.get(reservation_holder)
        if holder is None or holder.status is not ShipmentStatus.ALLOCATED:
            return
        if reservation_holder in stranded:
            held_back.add(reservation_holder)
            return
        expansion.add(reservation_holder)

    for zone_id, day in sorted(contended):
        for reservation in state.reservations_on_zone(zone_id):
            if reservation.from_ts.date() <= day <= reservation.until_ts.date():
                add_holder(reservation.holder)
    for zone_id in sorted(contended_zones):
        for reservation in state.reservations_on_zone(zone_id):
            add_holder(reservation.holder)
    return expansion, held_back


def reoptimize(
    store: EventStore,
    state: NetworkState,
    disruption_id: str,
    config: ObjectiveConfig,
    now: datetime,
    batch_prefix: str = "REOPT",
    packs: list[Pack] | None = None,
    commit: bool = True,
) -> ReoptResult:
    """Run the §7.6 tiers for one disruption and (by default) commit atomically."""
    disruption = state.disruptions.get(disruption_id)
    if disruption is None:
        raise AllocateError(f"unknown disruption {disruption_id!r}")
    if packs is None:
        packs = load_packs(config.packs)

    # Goods already inside a facility when it shuts cannot be re-routed by a
    # solver — nothing can move them until it reopens (§7.9). They keep their
    # booking and come back as `trapped` for an operator to clear by hand. The
    # exclusion is keyed on the cargo's OWN closure, not on what triggered this
    # re-solve: a lane block that happens to match a stranded shipment must not
    # become the back door through which the system releases its booking.
    stranded = {entry.shipment_id: entry for entry in all_trapped_cargo(state, now)}
    matched = affected_shipments(state, disruption, now)
    affected = [sid for sid in matched if sid not in stranded]
    # What this re-solve had to leave alone: whatever the trigger itself stranded
    # (a closure names its own victims even when nothing else was affected) plus
    # anything it would otherwise have re-solved. Each entry names the closure
    # holding the cargo, which is not necessarily the triggering disruption.
    reported = {entry.shipment_id for entry in trapped_cargo(state, disruption, now)}
    reported.update(sid for sid in matched if sid in stranded)
    if not affected:
        return ReoptResult(
            0, disruption_id, [], [], [], None, [], [stranded[sid] for sid in sorted(reported)]
        )

    def incumbent_pair(sid: str) -> tuple[str, str] | None:
        shipment = state.shipments.get(sid)
        if shipment is None or shipment.assigned is None:
            return None
        return (shipment.assigned.facility_id, shipment.assigned.zone_ids[0])

    def incumbents_of(subset: list[str]) -> dict[str, tuple[str, str]]:
        # EVERY solved shipment's incumbent, tier-2 expansion members included:
        # they pay churn like everyone else, and — critically — a dropped one
        # must compare unequal to its incumbent so it gets superseded and its
        # reservation released (leaving it live would overbook the zone).
        return {sid: pair for sid in subset if (pair := incumbent_pair(sid)) is not None}

    result_degradation: dict[int, float] = {}

    def solve(subset: list[str], tier: int) -> BatchResult:
        overlay = state.model_copy(deep=True)
        _fold_supersedes(overlay, subset, now)
        subset_incumbents = incumbents_of(subset)
        result = solve_batch(
            overlay,
            subset,
            config,
            now,
            batch_id=f"{batch_prefix}-{disruption_id}-T{tier}",
            packs=packs,
            incumbents=subset_incumbents,
        )
        baseline = incumbent_objective_scaled(
            overlay, subset, config, now, subset_incumbents, packs
        )
        degradation = (result.meta.objective_scaled - baseline) / OBJECTIVE_SCALE
        stamped = {
            sid: record.model_copy(
                update={
                    "based_on_seq": state.last_seq,
                    "reopt_tier": tier,
                    "reopt_trigger": disruption_id,
                }
            )
            for sid, record in result.records.items()
        }
        result = BatchResult(
            batch_id=result.batch_id,
            records=stamped,
            assignments=result.assignments,
            meta=result.meta,
        )
        result_degradation[tier] = degradation
        return result

    current = list(affected)
    tier = 1
    result = solve(current, tier)
    hops = 0
    while hops < config.reopt_hops:
        unassigned = [sid for sid in current if result.assignments.get(sid) is None]
        degraded = result_degradation[tier] > config.reopt_degradation_threshold
        if not unassigned and not degraded:
            break
        expansion, held_back = _capacity_connected(
            state, result, exclude=set(current), stranded=set(stranded)
        )
        reported.update(held_back)
        if not expansion:
            break
        current = sorted(set(current) | expansion)
        tier = 2
        hops += 1
        result = solve(current, tier)

    final_incumbents = incumbents_of(current)

    def window_stale(sid: str) -> bool:
        # A shipment can keep its destination yet have an unusable booking: a
        # readiness slip past its recorded eta means it arrives after its
        # reservation window. Same pair or not, that booking must be re-made.
        shipment = state.shipments.get(sid)
        return (
            shipment is not None
            and shipment.assigned is not None
            and shipment.ready_at > shipment.assigned.eta
        )

    def plan_stale(sid: str) -> bool:
        """The hold can stay put while the JOURNEY changes — a different exit, a
        different lane out, a different facility staged through. Keeping the pair
        and discarding the re-solve would leave the stale route folded and drawn,
        so route-level difference supersedes too (§7.6)."""
        shipment = state.shipments.get(sid)
        record = result.records.get(sid)
        if shipment is None or shipment.assigned is None or record is None:
            return False
        if record.chosen is None:
            return False
        return _plan_signature(assignment_of(record.chosen)) != _plan_signature(shipment.assigned)

    changed = sorted(
        sid
        # Stranded cargo is never superseded, whatever the solve says about it:
        # releasing its booking is an automatic change too, and for goods standing
        # in a shut facility that booking IS their physical occupancy (§7.9).
        # `current` should never name one; this is the guard that makes it true
        # however the set was assembled.
        for sid in current
        if sid not in stranded
        and (
            result.assignments.get(sid) != final_incumbents.get(sid)
            or window_stale(sid)
            or plan_stale(sid)
        )
    )
    released = sorted(sid for sid in changed if result.assignments.get(sid) is None)
    envelopes: list[Envelope] = []
    if commit and changed:
        envelopes = commit_reopt(store, state, result, changed, disruption_id, tier, now)
    return ReoptResult(
        tier,
        disruption_id,
        current,
        changed,
        released,
        result,
        envelopes,
        [stranded[sid] for sid in sorted(reported)],
    )


def commit_reopt(
    store: EventStore,
    state: NetworkState,
    result: BatchResult,
    changed: list[str],
    disruption_id: str,
    tier: int,
    now: datetime,
) -> list[Envelope]:
    """One atomic append: supersede + (new decision + reservations) per changed
    shipment. Refuses to commit over a moved log head."""
    head = store.last_seq()
    if head != state.last_seq:
        raise AllocateError(f"stale re-optimization: state at seq {state.last_seq}, head {head}")
    cause = f"reopt:{disruption_id}:tier{tier}"
    drafts: list[EventDraft] = [
        EventDraft(
            ts=now,
            payload=ev.BatchSolved(
                batch_id=result.batch_id, meta=result.meta.model_dump(mode="json")
            ),
            cause=cause,
        )
    ]
    for sid in changed:
        old_seq = store.last_entity_event_seq("shipment", sid, "AllocationDecided")
        if old_seq is None:
            raise AllocateError(f"no prior AllocationDecided for {sid}; nothing to supersede")
        drafts.append(
            EventDraft(
                ts=now,
                payload=ev.AllocationSuperseded(
                    shipment_id=sid, old_decision_seq=old_seq, reason=cause
                ),
                cause=cause,
            )
        )
        record: DecisionRecord = result.records[sid]
        if record.chosen is None:
            continue  # released back to planned; no new decision
        chosen = record.chosen
        drafts.append(
            EventDraft(
                ts=now,
                payload=ev.AllocationDecided(
                    shipment_id=sid,
                    assignment=assignment_of(chosen),
                    record=record.as_event_record(),
                ),
                cause=cause,
            )
        )
        for reservation in reservations_of(sid, chosen):
            drafts.append(
                EventDraft(
                    ts=now,
                    payload=ev.ReservationPlaced(reservation=reservation),
                    cause=cause,
                )
            )
    return store.append(drafts, actor="reopt")
