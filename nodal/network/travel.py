"""Travel primitives: lane costs, lane graph, synthesized legs (§6).

v1 `MatrixTravelModel`: lane distances/times as given; where the scenario omits
lanes, a synthetic direct leg is generated from great-circle distance x a per-mode
circuity factor. Multi-leg routes are hop-bounded shortest paths over the lane graph,
deterministic by construction (lexicographic tie-breaks). Lane-blocked disruptions
active at the probe instant exclude the lane.

`route` answers the raw question — is there a lane path, and if not what does a
direct leg cost — and knows nothing of what happens at the facilities in between.
`JourneyRouter` (`nodal.network.journey`) is what every decision actually routes
through: it schedules the legs, turns each intermediate facility into a checked and
booked stop, matches each lane block against the moment that lane rolls, and falls
back here for the one case with no facility in between — a leg that has to be
synthesized because the lane graph never reached the destination at all.
"""

import math
from datetime import datetime, timedelta
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import DisruptionKind, Lane, Shipment, Stop
from nodal.events.state import NetworkState


class TravelConfig(BaseModel):
    """Travel-model configuration. Lives at the network layer (§1: `allocate`
    depends on `network`, never the reverse); objective profiles embed it."""

    model_config = ConfigDict(frozen=True)

    metric: Literal["cost", "time"] = "cost"
    circuity: float = 1.3  # great-circle -> road factor for synthesized legs
    synth_speed_kmh: float = 70.0
    synth_cost_fixed_cents: int = 15_000
    synth_cost_per_km_cents: int = 120
    max_transfers: int = 2  # legs - 1 bound for multi-leg routes
    # Handling allowance (minutes) a lane's declared `minutes` ALREADY includes
    # for the loading/handover at the facility the leg departs from (§7.9). It is
    # never added to any total: it carves the dwell window of a pass-through stop
    # out of the leg the goods leave on, so journey minutes stay exactly what the
    # lanes declare while the facility still gets an occupied window.
    handling_minutes: int = Field(default=45, ge=0)
    handling_minutes_by_mode: dict[str, int] = Field(default_factory=dict)

    def handling_for(self, mode: str) -> int:
        return self.handling_minutes_by_mode.get(mode, self.handling_minutes)


EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def lane_cost_cents(lane: Lane, size: CapacityVector) -> int:
    weight_kg = size.demand("weight_g") / 1000
    volume_m3 = size.demand("volume_l") / 1000
    return round(
        lane.cost_fixed_cents
        + lane.cost_per_kg_cents * weight_kg
        + lane.cost_per_m3_cents * volume_m3
    )


def lane_blocked_at(state: NetworkState, lane_id: str, at: datetime) -> bool:
    """Blocked-at-departure approximation (documented, §7.2): a LANE_BLOCKED
    disruption live at the planning instant excludes the lane."""
    instant = at + timedelta(microseconds=1)
    return any(
        d.kind is DisruptionKind.LANE_BLOCKED for d in state.disruptions_on(lane_id, at, instant)
    )


class Leg(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_label: str  # facility id or origin label
    to_facility_id: str
    minutes: int
    km: float
    cost_cents: int
    lane_id: str | None = None  # None = synthesized direct leg


class Route(BaseModel):
    model_config = ConfigDict(frozen=True)

    legs: tuple[Leg, ...]
    # The journey on the clock it actually runs on (§7.9). `departs[i]` is when
    # leg i rolls; `stops` is the dwell window at every facility between two
    # legs, in travel order. Empty on a raw lane path nobody has scheduled yet
    # (`MatrixTravelModel.route`) — the router that owns the schedule fills them.
    departs: tuple[datetime, ...] = ()
    stops: tuple[Stop, ...] = ()
    # Minutes the goods spend waiting for an intermediate facility's operating
    # window (§6), on top of the legs' own minutes. The wait at the destination
    # is the survey's business, not the route's.
    transit_wait_minutes: int = 0

    @property
    def minutes(self) -> int:
        return sum(leg.minutes for leg in self.legs)

    @property
    def km(self) -> float:
        return sum(leg.km for leg in self.legs)

    @property
    def cost_cents(self) -> int:
        return sum(leg.cost_cents for leg in self.legs)

    @property
    def transfers(self) -> int:
        return max(0, len(self.legs) - 1)

    @property
    def lane_ids(self) -> list[str]:
        return [leg.lane_id for leg in self.legs if leg.lane_id is not None]


class RouteFailure(BaseModel):
    """Why no route was produced: `blocked` = unblocked lanes would reach the
    destination but active LANE_BLOCKED disruptions close every path; `no_route` =
    the origin has no coordinates to synthesize from; `stops_refused` = every
    remaining path dwells at a facility that may not take the goods (§7.9), and
    `refused_stops` carries those windows so the record can name them."""

    model_config = ConfigDict(frozen=True)

    reason: Literal["blocked", "no_route", "stops_refused"]
    blocked_lanes: list[str]
    refused_stops: tuple[Stop, ...] = ()


class TravelModel(Protocol):
    def route(
        self,
        state: NetworkState,
        shipment: Shipment,
        dest_facility_id: str,
        depart_at: datetime,
    ) -> Route | RouteFailure: ...


class MatrixTravelModel:
    def __init__(self, config: TravelConfig) -> None:
        self.config = config
        # Per-instance memos: the model is constructed per decision context, so a
        # cached relaxation can never outlive the state snapshot it read.
        self._paths_memo: dict[
            tuple[str, int | None, int | None, int | None, datetime, bool],
            dict[str, tuple[float, int, tuple[str, ...]]],
        ] = {}
        self._blocked_memo: dict[
            tuple[str, int | None, int | None, int | None, datetime, bool], set[str]
        ] = {}

    # -- synthetic legs ----------------------------------------------------------

    def _synthetic_leg(
        self,
        from_label: str,
        from_lat: float,
        from_lon: float,
        state: NetworkState,
        dest_facility_id: str,
    ) -> Leg:
        dest = state.facilities[dest_facility_id]
        km = haversine_km(from_lat, from_lon, dest.lat, dest.lon) * self.config.circuity
        minutes = max(1, round(km / self.config.synth_speed_kmh * 60))
        cost = round(self.config.synth_cost_fixed_cents + self.config.synth_cost_per_km_cents * km)
        return Leg(
            from_label=from_label,
            to_facility_id=dest_facility_id,
            minutes=minutes,
            km=round(km, 1),
            cost_cents=cost,
        )

    # -- lane-graph shortest path -------------------------------------------------

    def _all_paths(
        self,
        state: NetworkState,
        origin_facility_id: str,
        size: CapacityVector,
        depart_at: datetime,
        respect_blocks: bool,
        blocked_seen: set[str] | None = None,
    ) -> dict[str, tuple[float, int, tuple[str, ...]]]:
        """One hop-bounded relaxation from the origin reaches every destination —
        memoized per (origin, size, departure, blocks), so surveying N facilities
        costs one pass, not N. Deterministic via lexicographic
        (weight, hops, lane-id path) comparison."""
        key = (
            origin_facility_id,
            size.slots,
            size.volume_l,
            size.weight_g,
            depart_at,
            respect_blocks,
        )
        cached = self._paths_memo.get(key)
        if cached is not None:
            if blocked_seen is not None:
                blocked_seen.update(self._blocked_memo.get(key, set()))
            return cached
        max_legs = self.config.max_transfers + 1
        Best = tuple[float, int, tuple[str, ...]]  # (weight, hops, lane path)
        best: dict[str, Best] = {origin_facility_id: (0.0, 0, ())}
        frontier: dict[str, Best] = dict(best)
        blocked: set[str] = set()
        for _ in range(max_legs):
            next_frontier: dict[str, Best] = {}
            for node, (weight, hops, path) in sorted(frontier.items()):
                for lane in state.lanes_from(node):
                    if respect_blocks and lane_blocked_at(state, lane.id, depart_at):
                        blocked.add(lane.id)
                        continue
                    step = (
                        float(lane_cost_cents(lane, size))
                        if self.config.metric == "cost"
                        else float(lane.minutes)
                    )
                    candidate: Best = (weight + step, hops + 1, (*path, lane.id))
                    target = lane.to_facility_id
                    if target not in best or candidate < best[target]:
                        best[target] = candidate
                        next_frontier[target] = candidate
            frontier = next_frontier
            if not frontier:
                break
        self._paths_memo[key] = best
        self._blocked_memo[key] = blocked
        if blocked_seen is not None:
            blocked_seen.update(blocked)
        return best

    def _lane_path(
        self,
        state: NetworkState,
        origin_facility_id: str,
        dest_facility_id: str,
        size: CapacityVector,
        depart_at: datetime,
        respect_blocks: bool = True,
        blocked_seen: set[str] | None = None,
    ) -> Route | None:
        best = self._all_paths(
            state, origin_facility_id, size, depart_at, respect_blocks, blocked_seen
        )
        found = best.get(dest_facility_id)
        if found is None or dest_facility_id == origin_facility_id:
            return None
        legs = []
        node = origin_facility_id
        for lane_id in found[2]:
            lane = state.lanes[lane_id]
            legs.append(
                Leg(
                    from_label=node,
                    to_facility_id=lane.to_facility_id,
                    minutes=lane.minutes,
                    km=lane.distance_km,
                    cost_cents=lane_cost_cents(lane, size),
                    lane_id=lane.id,
                )
            )
            node = lane.to_facility_id
        return Route(legs=tuple(legs))

    # -- public API ---------------------------------------------------------------

    def route(
        self,
        state: NetworkState,
        shipment: Shipment,
        dest_facility_id: str,
        depart_at: datetime,
    ) -> "Route | RouteFailure":
        """A blocked network is not an absent one (§7.2): synthesis only fills in
        where the lane graph never had a path; if unblocked lanes would reach the
        destination but blocks close every one of them, that's LANE_BLOCKED."""
        size = shipment.size
        if shipment.origin_facility_id is not None:
            if shipment.origin_facility_id == dest_facility_id:
                return Route(legs=())  # store where it already sits: zero transport
            blocked_seen: set[str] = set()
            via_lanes = self._lane_path(
                state,
                shipment.origin_facility_id,
                dest_facility_id,
                size,
                depart_at,
                blocked_seen=blocked_seen,
            )
            if via_lanes is not None:
                return via_lanes
            unblocked = self._lane_path(
                state,
                shipment.origin_facility_id,
                dest_facility_id,
                size,
                depart_at,
                respect_blocks=False,
            )
            if unblocked is not None:
                return RouteFailure(reason="blocked", blocked_lanes=sorted(blocked_seen))
            origin = state.facilities[shipment.origin_facility_id]
            return Route(
                legs=(
                    self._synthetic_leg(origin.id, origin.lat, origin.lon, state, dest_facility_id),
                )
            )
        if shipment.origin_lat is None or shipment.origin_lon is None:
            return RouteFailure(reason="no_route", blocked_lanes=[])
        label = shipment.origin_label or "origin"
        return Route(
            legs=(
                self._synthetic_leg(
                    label, shipment.origin_lat, shipment.origin_lon, state, dest_facility_id
                ),
            )
        )
