"""Journey routing: every facility a shipment touches, with the window it sits
there (§6, §7.9).

One router answers both shapes of decision. An ordinary allocation is routed
origin -> holding facility; a shipment carrying a `destination` is routed
origin -> holding facility -> customer. The holding facility and zone are still
chosen by the ordinary survey, hard constraints, objective, and batch model
(§7.1-§7.4); this module supplies the route halves that decision prices and the
schedule of dwells they imply.

Every intermediate facility is a **stop**: it has an explicit dwell window carved
out of the handling the journey already pays for, and that window is what
closures, equipment outages, operating calendars and staging capacity are matched
against. There is no second, stop-blind path through the engine — an ordinary
multi-hop allocation and a delivery's inbound half are the same code, so nothing
can be routed through a facility that may not take the goods.

Both halves are constants of (shipment, holding facility) — the entry and the
exit are each the argmin of one relaxation over the whole lane graph — which is
what keeps the batch model's per-pair coefficients exact and makes the batch
argmin coincide with the scorer's (§7.4, §14).

Offline and deterministic (§2): first/last-mile road legs are haversine distance x
a circuity factor at a fixed speed, never an external routing service, and lane
paths are resolved by lexicographic comparison, never by iteration order.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import Lane, Shipment, Stop, StopRole
from nodal.events.state import NetworkState
from nodal.network.travel import (
    Leg,
    MatrixTravelModel,
    Route,
    RouteFailure,
    haversine_km,
    lane_blocked_at,
    lane_cost_cents,
)

# Secure a staging zone for a whole candidate's stops, in travel order: returns
# the stops with their zones filled in, and the first stop (if any) whose
# facility cannot take the goods. The WHOLE list goes in one call because the
# candidate's own earlier dwells occupy the zones its later dwells ask for
# (§7.9) — asking stop by stop would let one journey book a dock twice.
# Injected by the allocation layer, which owns requirement matching and capacity
# (§1: `network` never depends on `allocate` or `rules`).
StagingChecker = Callable[[Sequence[Stop]], tuple[list[Stop], Stop | None]]


class RoadLegConfig(BaseModel):
    """First/last-mile road pricing (§7.9). Lives at the network layer like
    `TravelConfig`; objective profiles embed it. Same fixed + per-distance +
    per-kg structure a lane carries, with a handling allowance at the customer
    end that a lane's declared minutes already include."""

    model_config = ConfigDict(frozen=True)

    circuity: float = 1.3  # great-circle -> road distance
    speed_kmh: float = 68.0
    # Handling included in the leg's declared minutes, spent at the facility the
    # leg DEPARTS from — for the last mile that is the exit facility, whose stop
    # window it therefore defines (§7.9). Never added on top of anything.
    handling_minutes: int = 45
    cost_fixed_cents: int = 15_000
    cost_per_km_cents: int = 120
    cost_per_kg_cents: float = 0.0  # per canonical kg (1000 g)


@dataclass(frozen=True)
class RoadLeg:
    """A priced road leg between two points, at least one of which is not a
    facility (so it is not a lane and never enters the lane graph)."""

    from_label: str
    to_label: str
    km: float
    minutes: int
    cost_cents: int


def road_leg(
    config: RoadLegConfig,
    size: CapacityVector,
    from_label: str,
    from_lat: float,
    from_lon: float,
    to_label: str,
    to_lat: float,
    to_lon: float,
) -> RoadLeg:
    km = haversine_km(from_lat, from_lon, to_lat, to_lon) * config.circuity
    minutes = max(1, round(km / config.speed_kmh * 60) + config.handling_minutes)
    cost = round(
        config.cost_fixed_cents
        + config.cost_per_km_cents * km
        + config.cost_per_kg_cents * (size.demand("weight_g") / 1000)
    )
    return RoadLeg(
        from_label=from_label, to_label=to_label, km=round(km, 1), minutes=minutes, cost_cents=cost
    )


# (weight, cost_cents, hops, minutes, seed_facility_id, lane_ids, km) for one lane
# path with its road leg already priced in. `weight` is what the path minimizes —
# cost or minutes, per the metric this half runs under — and the rest of the tuple
# IS the tie-break rule (§2): cheapest, then fewest lanes, then fastest, then the
# smallest facility id, then the smallest lane-id path. Two equal-weight paths
# therefore always resolve the same way, whatever order the graph is walked in.
#
# Fewest lanes outranks fastest because an extra leg is never free elsewhere in the
# engine: `transfers` is an objective component that prices it, and every hop books
# staging space for the dwell it takes (§7.9). Preferring a faster multi-hop over a
# cost-tied direct leg would hand the objective a path it then charges for.
_Path = tuple[int, int, int, int, str, tuple[str, ...], float]

Metric = Literal["cost", "time"]


class _Exclusions(NamedTuple):
    """What one candidate's retries have ruled out of its own relaxation: lanes
    a block closes when they would roll, facilities that cannot dwell the goods.
    Hashable, so it keys the memo and candidates seeing the same network share
    one pass."""

    lanes: frozenset[str] = frozenset()
    facilities: frozenset[str] = frozenset()

    def without_lane(self, lane_id: str) -> "_Exclusions":
        return self._replace(lanes=self.lanes | {lane_id})

    def without_facility(self, facility_id: str) -> "_Exclusions":
        return self._replace(facilities=self.facilities | {facility_id})


NOTHING_EXCLUDED = _Exclusions()


def _waited(start: datetime, end: datetime, legs: Sequence[Leg]) -> int:
    """Minutes of a walked half that were NOT travel: the operating-window waits
    its dwells sat out (§6). Journey minutes stay exactly what the lanes declare,
    so anything the clock gained beyond them is waiting."""
    elapsed = round((end - start).total_seconds() / 60)
    return max(0, elapsed - sum(leg.minutes for leg in legs))


def _relax(
    adjacency: dict[str, list[Lane]],
    seeds: dict[str, _Path],
    size: CapacityVector,
    max_legs: int,
    forward: bool,
    metric: Metric = "cost",
    excluded: _Exclusions = NOTHING_EXCLUDED,
) -> dict[str, _Path]:
    """Hop-bounded multi-source relaxation over the lane graph. `forward` walks
    lanes from their origin (inbound: seeds are entry facilities); otherwise it
    walks them backwards (outbound: seeds are exit facilities) and prepends each
    lane so the recovered path still reads in travel order.

    `excluded` names lanes and facilities this candidate has already ruled out
    (§7.9). Extending FROM a node is exactly what makes it a pass-through stop, so
    an excluded facility is never extended from — it can still be the node a
    caller asks about, because the journey's own endpoint does not dwell for
    handling. Excluding a node as a *seed* is the caller's business: an exit seed
    dwells, an entry seed does not.
    """
    best = dict(seeds)
    frontier = {n: p for n, p in seeds.items() if n not in excluded.facilities}
    for _ in range(max_legs):
        next_frontier: dict[str, _Path] = {}
        for node, (weight, cost, hops, minutes, seed, lanes, km) in sorted(frontier.items()):
            for lane in adjacency.get(node, ()):
                if lane.id in excluded.lanes:
                    continue
                step_cost = lane_cost_cents(lane, size)
                target = lane.to_facility_id if forward else lane.from_facility_id
                candidate: _Path = (
                    weight + (step_cost if metric == "cost" else lane.minutes),
                    cost + step_cost,
                    hops + 1,
                    minutes + lane.minutes,
                    seed,
                    (*lanes, lane.id) if forward else (lane.id, *lanes),
                    km + lane.distance_km,
                )
                if target not in best or candidate < best[target]:
                    best[target] = candidate
                    next_frontier[target] = candidate
        frontier = {n: p for n, p in next_frontier.items() if n not in excluded.facilities}
        if not frontier:
            break
    return best


@dataclass(frozen=True)
class DeliveryPlan:
    """The routing beyond the hold at one candidate holding facility (§7.9).

    `inbound` is the full Route the survey already priced; the outbound half is
    kept separate because it happens AFTER the hold: it is priced into the
    objective but is not the transport that books the reservation.

    `stops` is every facility the whole journey touches, in order, with the window
    it dwells there: the pass-through stops carry the staging zone the router
    already secured, the hold stop carries the hold window and gets its zone from
    the ordinary survey.
    """

    entry_facility_id: str
    inbound: Route  # origin -> holding facility
    exit_facility_id: str
    outbound_lanes: tuple[Leg, ...]  # holding -> exit facility; empty when equal
    outbound_departs: tuple[datetime, ...]  # when each outbound lane rolls
    last_mile: RoadLeg  # exit facility -> destination
    last_mile_depart: datetime
    delivered_at: datetime
    stops: tuple[Stop, ...]
    # Waiting for an exit or transit facility's operating window on the way out
    # (§6): real elapsed time between the hold ending and the customer's goods
    # arriving, so the objective prices it like any other minute.
    outbound_wait_minutes: int = 0

    @property
    def outbound_cost_cents(self) -> int:
        return sum(leg.cost_cents for leg in self.outbound_lanes) + self.last_mile.cost_cents

    @property
    def outbound_minutes(self) -> int:
        return (
            sum(leg.minutes for leg in self.outbound_lanes)
            + self.last_mile.minutes
            + self.outbound_wait_minutes
        )

    @property
    def outbound_leg_count(self) -> int:
        """Legs after the hold; each one is a further transfer of the goods."""
        return len(self.outbound_lanes) + 1

    @property
    def staging_stops(self) -> tuple[Stop, ...]:
        return tuple(stop for stop in self.stops if stop.books_staging)


@dataclass(frozen=True)
class DeliveryFailure:
    """No itinerary exists out of this holding facility: every remaining way to
    the customer dwells at a facility that cannot take the goods (§7.9). The
    refused stops are carried whole, window included, so the decision record can
    say exactly which facility refused and when."""

    stops: tuple[Stop, ...]


class JourneyRouter:
    """Routing for ONE shipment across every candidate holding facility.

    Two relaxations answer them all: one forward from the origin's entry options,
    one backward from the destination's exit options (deliveries only).

    Both are then CHECKED against the schedule they imply and re-run without
    whatever failed (§7.9). A journey is a sequence of legs and dwells, each with
    its own instant: the blocked-at-departure rule of §7.2 binds every lane at the
    moment that lane rolls, not at one probe instant for the whole trip; every
    facility must be able to take the goods for the dwell the journey books there;
    and a dwell that lands outside the facility's operating window WAITS for the
    next one (§6), which extends every leg and arrival after it. Whatever fails
    joins an exclusion set and the relaxation runs again, so the router takes the
    next-cheapest path AROUND a closed hop, a shut exit, or a block that only
    opens days into the trip. The exclusion sets key the memo, so candidates that
    see the same network still share one pass and an undisrupted network costs
    exactly one.

    The exclusion is global for the retry rather than window-specific: a lane or
    facility that failed where this path met it is not re-tried at a different
    time on a costlier path. That is conservative — it can pass over a path a
    finer search would admit, and can never admit one that moves or dwells when
    it may not.

    A delivery's lane paths are cheapest-cost regardless of `TravelConfig.metric`
    (§7.9): the entry and exit choices need one scalar to minimize, and the
    objective prices the resulting time separately. An ordinary allocation's route
    minimizes the configured metric, which is what §6 promises it.
    """

    def __init__(
        self,
        state: NetworkState,
        shipment: Shipment,
        travel: MatrixTravelModel,
        road: RoadLegConfig,
        depart_at: datetime,
        staging: StagingChecker,
    ) -> None:
        self.state = state
        self.shipment = shipment
        self.travel = travel
        self.road = road
        self.depart_at = depart_at
        self.staging = staging
        destination = shipment.destination
        size = shipment.size
        self._size = size
        self._external = shipment.origin_facility_id is None
        # A route carries at most `max_transfers + 1` legs everywhere in the
        # engine. A delivery half spends one of them on its road leg, so its lane
        # relaxation gets the rest (and none at all at max_transfers=0); a
        # shipment already inside the network spends none, so its inbound lane
        # path keeps the full budget the shared lane router always gave it.
        transfers = travel.config.max_transfers
        self._inbound_max_legs = transfers if self._external else transfers + 1
        self._outbound_max_legs = transfers
        self._inbound_metric: Metric = "cost" if destination is not None else travel.config.metric
        self._lane_ids = sorted(state.lanes)
        self._lanes_from: dict[str, list[Lane]] = {}
        self._lanes_to: dict[str, list[Lane]] = {}
        for lane_id in self._lane_ids:
            lane = state.lanes[lane_id]
            self._lanes_from.setdefault(lane.from_facility_id, []).append(lane)
            self._lanes_to.setdefault(lane.to_facility_id, []).append(lane)

        # Inbound. A shipment already inside the network enters at its own
        # facility. A delivery from an external origin may enter anywhere: seed
        # every facility with its first-mile road leg and let the relaxation pick
        # the cheapest entry. An ORDINARY shipment from an external gate is one
        # direct road leg (§6) — it touches no facility on the way, so it has no
        # entry to choose and nothing to stop at.
        self._inbound_seeds: dict[str, _Path] = {}
        self._first_mile: dict[str, RoadLeg] = {}
        if not self._external:
            origin = str(shipment.origin_facility_id)
            self._inbound_seeds[origin] = (0, 0, 0, 0, origin, (), 0.0)
        elif (
            destination is not None
            and shipment.origin_lat is not None
            and shipment.origin_lon is not None
        ):
            label = shipment.origin_label or "origin"
            for facility_id in sorted(state.facilities):
                facility = state.facilities[facility_id]
                leg = road_leg(
                    road,
                    size,
                    label,
                    shipment.origin_lat,
                    shipment.origin_lon,
                    facility_id,
                    facility.lat,
                    facility.lon,
                )
                self._first_mile[facility_id] = leg
                self._inbound_seeds[facility_id] = (
                    leg.cost_cents,
                    leg.cost_cents,
                    0,
                    leg.minutes,
                    facility_id,
                    (),
                    leg.km,
                )
        self._inbound_memo: dict[_Exclusions, dict[str, _Path]] = {}

        # Outbound: seed every facility with its last-mile road leg to the
        # customer and relax backwards, so each facility learns its cheapest way
        # out. Every facility is its own exit option, so this never fails. The
        # relaxation itself waits for `complete`, which knows when the hold ends.
        self._last_mile: dict[str, RoadLeg] = {}
        self._exit_seeds: dict[str, _Path] = {}
        if destination is not None:
            for facility_id in sorted(state.facilities):
                facility = state.facilities[facility_id]
                leg = road_leg(
                    road,
                    size,
                    facility_id,
                    facility.lat,
                    facility.lon,
                    destination.label,
                    destination.lat,
                    destination.lon,
                )
                self._last_mile[facility_id] = leg
                self._exit_seeds[facility_id] = (
                    leg.cost_cents,
                    leg.cost_cents,
                    0,
                    leg.minutes,
                    facility_id,
                    (),
                    leg.km,
                )
        self._outbound_memo: dict[_Exclusions, dict[str, _Path]] = {}

    def _inbound_paths(self, excluded: "_Exclusions") -> dict[str, _Path]:
        """Cheapest way in to every facility, avoiding what has been excluded."""
        cached = self._inbound_memo.get(excluded)
        if cached is None:
            cached = _relax(
                self._lanes_from,
                self._inbound_seeds,
                self._size,
                self._inbound_max_legs,
                forward=True,
                metric=self._inbound_metric,
                excluded=excluded,
            )
            self._inbound_memo[excluded] = cached
        return cached

    def _outbound_paths(self, excluded: "_Exclusions") -> dict[str, _Path]:
        """Cheapest way out of every facility, avoiding what has been excluded."""
        cached = self._outbound_memo.get(excluded)
        if cached is None:
            # An excluded facility cannot be the EXIT either — the goods dwell
            # there for the last mile's handling — so it loses its seed.
            seeds = {f: p for f, p in self._exit_seeds.items() if f not in excluded.facilities}
            cached = _relax(
                self._lanes_to,
                seeds,
                self._size,
                self._outbound_max_legs,
                forward=False,
                excluded=excluded,
            )
            self._outbound_memo[excluded] = cached
        return cached

    def _timed(self, route: Route) -> Route:
        """A route the shared lane router answered carries no schedule of its own.
        It is always the one synthesized leg §6 falls back to, so it touches no
        facility on the way and its legs simply run from the departure instant."""
        departs: list[datetime] = []
        clock = self.depart_at
        for leg in route.legs:
            departs.append(clock)
            clock += timedelta(minutes=leg.minutes)
        return route.model_copy(update={"departs": tuple(departs)})

    def _blocked_leg(self, legs: Sequence[Leg], departs: Sequence[datetime]) -> str | None:
        """The first lane a block closes at the instant that lane actually rolls."""
        for leg, depart in zip(legs, departs, strict=True):
            if leg.lane_id is not None and lane_blocked_at(self.state, leg.lane_id, depart):
                return leg.lane_id
        return None

    def _handling(self, leg: Leg) -> int:
        """Minutes of the leg that are handling at the facility it departs from."""
        mode = "road" if leg.lane_id is None else self.state.lanes[leg.lane_id].mode
        allowance = (
            self.road.handling_minutes
            if leg.lane_id is None
            else self.travel.config.handling_for(mode)
        )
        return max(0, min(allowance, leg.minutes))

    def _dwell(self, facility_id: str, role: StopRole, arrive: datetime, minutes: int) -> Stop:
        """One dwell window, shifted to the facility's next opening (§6): a
        calendar-closed facility makes the goods WAIT, exactly as the hold does,
        rather than being cross-docked through a shut building."""
        opened = self.state.facility_next_open(facility_id, arrive)
        assert opened is not None, "caller checks for a facility that never opens"
        return Stop(
            facility_id=facility_id,
            role=role,
            arrive=opened,
            depart=opened + timedelta(minutes=minutes),
        )

    def _walk(
        self, legs: Sequence[Leg], start: datetime, roles: Sequence[StopRole]
    ) -> tuple[list[Stop], list[datetime], datetime, Stop | None]:
        """Run the legs on the clock: when each one rolls, where the goods dwell
        in between, and when they finally arrive.

        The goods arrive on one leg and leave on the next, dwelling for the
        handling minutes that next leg already prices — the window is carved out
        of the leg, never added to it, so journey minutes stay exactly what the
        lanes declare. Waiting for an operating window is the one thing that DOES
        extend the schedule, and it pushes every later departure with it.

        `roles[i]` is the role of the stop after leg i. The fourth result is the
        stop whose facility has no operating window at all within the horizon:
        it cannot dwell the goods, so the caller routes around it.
        """
        stops: list[Stop] = []
        departs: list[datetime] = []
        clock = start
        for index, leg in enumerate(legs):
            departs.append(clock)
            arrive = clock + timedelta(minutes=leg.minutes)
            if index == len(legs) - 1:
                return stops, departs, arrive, None
            facility_id = leg.to_facility_id
            if self.state.facility_next_open(facility_id, arrive) is None:
                unreachable = Stop(
                    facility_id=facility_id, role=roles[index], arrive=arrive, depart=arrive
                )
                return stops, departs, arrive, unreachable
            stop = self._dwell(facility_id, roles[index], arrive, self._handling(legs[index + 1]))
            stops.append(stop)
            clock = stop.arrive
        return stops, departs, start, None

    def _inbound_roles(self, legs: Sequence[Leg]) -> list[StopRole]:
        """The first facility an externally-originating journey reaches is its
        entry; everything else on the way in is a transit hop. A shipment already
        inside the network does not dwell at its own origin — the goods are
        already there — so its origin is not a stop at all."""
        return [
            StopRole.ENTRY if self._external and index == 0 else StopRole.TRANSIT
            for index in range(len(legs))
        ]

    def _lane_legs(self, start: str, lane_ids: tuple[str, ...]) -> list[Leg]:
        legs: list[Leg] = []
        node = start
        for lane_id in lane_ids:
            lane = self.state.lanes[lane_id]
            legs.append(
                Leg(
                    from_label=node,
                    to_facility_id=lane.to_facility_id,
                    minutes=lane.minutes,
                    km=lane.distance_km,
                    cost_cents=lane_cost_cents(lane, self._size),
                    lane_id=lane.id,
                )
            )
            node = lane.to_facility_id
        return legs

    def _inbound_legs(self, found: _Path) -> list[Leg]:
        entry = found[4]
        lane_legs = self._lane_legs(entry, found[5])
        if not self._external:
            return lane_legs
        first = self._first_mile[entry]
        return [
            Leg(
                from_label=first.from_label,
                to_facility_id=entry,
                minutes=first.minutes,
                km=first.km,
                cost_cents=first.cost_cents,
            ),
            *lane_legs,
        ]

    def inbound(self, holding_facility_id: str) -> Route | RouteFailure:
        """Origin -> holding facility, with every facility on the way checked and
        booked for the dwell it takes (§7.9). Stops that cannot take the goods,
        and lanes blocked when they would roll, are routed around."""
        origin = self.shipment.origin_facility_id
        if origin == holding_facility_id:
            return Route(legs=())  # store where it already sits: zero transport
        if not self._inbound_seeds:
            # An ordinary shipment from an external gate (or one with no
            # coordinates at all): the shared lane router answers it with the one
            # direct road leg §6 synthesizes, which touches nothing on the way.
            return self._direct(holding_facility_id)
        excluded = NOTHING_EXCLUDED
        refused: list[Stop] = []
        blocked_lanes: list[str] = []
        while True:
            found = self._inbound_paths(excluded).get(holding_facility_id)
            if found is None:
                if self._inbound_paths(NOTHING_EXCLUDED).get(holding_facility_id) is None:
                    # The lane graph never had a path here: a blocked network is
                    # not an absent one (§7.2), so only now may a leg be
                    # synthesized, and the shared router owns that.
                    return self._direct(holding_facility_id)
                if refused:
                    return RouteFailure(
                        reason="stops_refused",
                        blocked_lanes=sorted(set(blocked_lanes)),
                        refused_stops=tuple(refused),
                    )
                return RouteFailure(reason="blocked", blocked_lanes=sorted(set(blocked_lanes)))
            legs = self._inbound_legs(found)
            stops, departs, arrive, unreachable = self._walk(
                legs, self.depart_at, self._inbound_roles(legs)
            )
            if unreachable is not None:
                refused.append(unreachable)
                excluded = excluded.without_facility(unreachable.facility_id)
                continue
            blocked = self._blocked_leg(legs, departs)
            if blocked is not None:
                blocked_lanes.append(blocked)
                excluded = excluded.without_lane(blocked)
                continue
            staged, refusal = self.staging(stops)
            if refusal is None:
                return Route(
                    legs=tuple(legs),
                    departs=tuple(departs),
                    stops=tuple(staged),
                    transit_wait_minutes=_waited(self.depart_at, arrive, legs),
                )
            refused.append(refusal)
            excluded = excluded.without_facility(refusal.facility_id)

    def complete(
        self,
        holding_facility_id: str,
        inbound: Route,
        hold_start: datetime,
        hold_end: datetime,
    ) -> DeliveryPlan | DeliveryFailure:
        """Attach the outbound half to a surveyed hold: lanes to the cheapest exit
        facility, then the last-mile road leg. The first truck rolls when the hold
        ends, and every later leg and dwell is scheduled from there — each read
        against the disruptions live at ITS own instant, not at the hold's end."""
        assert self.shipment.destination is not None, "only a delivery has an outbound half"
        blocked_stops: list[Stop] = []
        excluded = NOTHING_EXCLUDED
        while True:
            found = self._outbound_paths(excluded).get(holding_facility_id)
            if found is None:
                return DeliveryFailure(stops=tuple(blocked_stops))
            exit_facility_id = found[4]
            outbound_lanes = tuple(self._lane_legs(holding_facility_id, found[5]))
            stops, departs, arrive, unreachable = self._walk(
                outbound_lanes, hold_end, [StopRole.TRANSIT] * len(outbound_lanes)
            )
            if unreachable is not None:
                blocked_stops.append(unreachable)
                excluded = excluded.without_facility(unreachable.facility_id)
                continue
            blocked = self._blocked_leg(outbound_lanes, departs)
            if blocked is not None:
                excluded = excluded.without_lane(blocked)
                continue
            last_mile = self._last_mile[exit_facility_id]
            loading = max(0, min(self.road.handling_minutes, last_mile.minutes))
            if exit_facility_id == holding_facility_id:
                # The goods never leave the hold before the last-mile truck loads,
                # so that loading window is part of the hold's own stop rather
                # than a second booking on top of the reservation already there.
                hold_depart = hold_end + timedelta(minutes=loading)
                last_mile_depart = hold_end
            else:
                hold_depart = hold_end
                if self.state.facility_next_open(exit_facility_id, arrive) is None:
                    blocked_stops.append(
                        Stop(
                            facility_id=exit_facility_id,
                            role=StopRole.EXIT,
                            arrive=arrive,
                            depart=arrive,
                        )
                    )
                    excluded = excluded.without_facility(exit_facility_id)
                    continue
                exit_stop = self._dwell(exit_facility_id, StopRole.EXIT, arrive, loading)
                stops.append(exit_stop)
                last_mile_depart = exit_stop.arrive
            # The whole journey is staged in ONE call: the inbound dwells this
            # candidate already secured occupy the zones its outbound dwells ask
            # for, and a journey that passes the same dock twice must pay for it
            # twice — which is exactly what the batch model charges it (§7.4).
            inbound_count = len(inbound.stops)
            staged, refusal = self.staging([*inbound.stops, *stops])
            if refusal is None:
                hold_stop = Stop(
                    facility_id=holding_facility_id,
                    role=StopRole.HOLD,
                    arrive=hold_start,
                    depart=hold_depart,
                )
                return DeliveryPlan(
                    entry_facility_id=self._entry_of(inbound, holding_facility_id),
                    inbound=inbound,
                    exit_facility_id=exit_facility_id,
                    outbound_lanes=outbound_lanes,
                    outbound_departs=tuple(departs),
                    last_mile=last_mile,
                    last_mile_depart=last_mile_depart,
                    delivered_at=last_mile_depart + timedelta(minutes=last_mile.minutes),
                    stops=(
                        *staged[:inbound_count],
                        hold_stop,
                        *staged[inbound_count:],
                    ),
                    outbound_wait_minutes=_waited(hold_end, last_mile_depart, outbound_lanes),
                )
            if len(staged) < inbound_count:
                # The inbound half is already fixed by the time the hold is
                # surveyed; nothing here can re-route it, so this candidate is out.
                return DeliveryFailure(stops=(refusal,))
            blocked_stops.append(refusal)
            excluded = excluded.without_facility(refusal.facility_id)

    def _direct(self, holding_facility_id: str) -> Route | RouteFailure:
        answer = self.travel.route(self.state, self.shipment, holding_facility_id, self.depart_at)
        return self._timed(answer) if isinstance(answer, Route) else answer

    def _entry_of(self, inbound: Route, holding_facility_id: str) -> str:
        if self.shipment.origin_facility_id is not None:
            return self.shipment.origin_facility_id  # already inside the network
        if inbound.legs:
            return inbound.legs[0].to_facility_id  # end of the first-mile road leg
        return holding_facility_id
