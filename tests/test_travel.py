from nodal.allocate.config import TravelConfig
from nodal.domain.entities import Disruption, DisruptionKind
from nodal.events import EventStore, NetworkState, load_state
from nodal.events import catalog as ev
from nodal.network.travel import MatrixTravelModel, Route, RouteFailure, haversine_km
from tests.helpers import at, draft, make_shipment


def model(metric: str = "cost") -> MatrixTravelModel:
    return MatrixTravelModel(TravelConfig(metric=metric))  # type: ignore[arg-type]


def test_haversine_sanity() -> None:
    # Chicago -> Cincinnati is roughly 400 km great-circle.
    km = haversine_km(41.88, -87.63, 39.10, -84.51)
    assert 380 < km < 420


def test_lane_route_preferred_over_synthetic(world_state: NetworkState) -> None:
    shipment = make_shipment("SHP-T")
    shipment.origin_facility_id = "FAC-A"
    route = model().route(world_state, shipment, "FAC-B", at(0, 9))
    assert isinstance(route, Route)
    assert route.lane_ids == ["LANE-AB"]
    assert route.transfers == 0
    assert route.minutes == 420


def test_same_facility_is_zero_leg(world_state: NetworkState) -> None:
    shipment = make_shipment("SHP-T")
    shipment.origin_facility_id = "FAC-A"
    route = model().route(world_state, shipment, "FAC-A", at(0, 9))
    assert isinstance(route, Route)
    assert route.legs == ()
    assert route.cost_cents == 0 and route.minutes == 0 and route.transfers == 0


def test_external_origin_synthesizes_direct_leg(world_state: NetworkState) -> None:
    shipment = make_shipment("SHP-T")
    shipment.origin_lat, shipment.origin_lon = 41.95, -87.65
    route = model().route(world_state, shipment, "FAC-B", at(0, 9))
    assert isinstance(route, Route)
    assert len(route.legs) == 1
    assert route.legs[0].lane_id is None
    assert route.legs[0].km > 0


def test_multi_leg_when_no_direct_lane(world_state: NetworkState) -> None:
    # FAC-B -> FAC-C has no direct lane; the router finds the two-leg B->A->C path.
    shipment = make_shipment("SHP-T")
    shipment.origin_facility_id = "FAC-B"
    route = model().route(world_state, shipment, "FAC-C", at(0, 9))
    assert isinstance(route, Route)
    assert route.lane_ids == ["LANE-BA", "LANE-AC"]
    assert route.transfers == 1
    # A -> B exists directly and is cheapest as a single leg:
    shipment.origin_facility_id = "FAC-A"
    route_ab = model().route(world_state, shipment, "FAC-B", at(0, 9))
    assert isinstance(route_ab, Route) and route_ab.lane_ids == ["LANE-AB"]


def test_hop_bound_boundary(world_state: NetworkState) -> None:
    """B->C needs exactly two legs (B->A->C) = one transfer. max_transfers=1 is
    the exact boundary and must still find it; max_transfers=0 forbids it and
    falls back to a synthetic direct leg."""
    shipment = make_shipment("SHP-T")
    shipment.origin_facility_id = "FAC-B"
    boundary = MatrixTravelModel(TravelConfig(max_transfers=1)).route(
        world_state, shipment, "FAC-C", at(0, 9)
    )
    direct_only = MatrixTravelModel(TravelConfig(max_transfers=0)).route(
        world_state, shipment, "FAC-C", at(0, 9)
    )
    assert isinstance(boundary, Route) and boundary.lane_ids == ["LANE-BA", "LANE-AC"]
    assert isinstance(direct_only, Route) and direct_only.lane_ids == []  # synthetic


def test_fully_blocked_network_is_not_synthesized(world_store: EventStore) -> None:
    """Every lane path FAC-A -> FAC-B blocked (direct AB and the AC+CB detour):
    the router must report LANE_BLOCKED, not invent a road (§7.2)."""
    for lane_id in ("LANE-AB", "LANE-AC"):
        world_store.append(
            [
                draft(
                    ev.DisruptionStarted(
                        disruption=Disruption(
                            id=f"DIS-{lane_id}",
                            kind=DisruptionKind.LANE_BLOCKED,
                            target_id=lane_id,
                            from_ts=at(0, 8),
                            until_ts=at(3),
                        )
                    ),
                    at(0, 9),
                )
            ]
        )
    state = load_state(world_store)
    shipment = make_shipment("SHP-T")
    shipment.origin_facility_id = "FAC-A"
    outcome = model().route(state, shipment, "FAC-B", at(0, 10))
    assert isinstance(outcome, RouteFailure)
    assert outcome.reason == "blocked"
    assert "LANE-AB" in outcome.blocked_lanes


def test_blocked_lane_is_avoided(world_store: EventStore) -> None:
    world_store.append(
        [
            draft(
                ev.DisruptionStarted(
                    disruption=Disruption(
                        id="DIS-LANE",
                        kind=DisruptionKind.LANE_BLOCKED,
                        target_id="LANE-AB",
                        from_ts=at(0, 8),
                        until_ts=at(3),
                    )
                ),
                at(0, 9),
            )
        ]
    )
    state = load_state(world_store)
    shipment = make_shipment("SHP-T")
    shipment.origin_facility_id = "FAC-A"
    route = model().route(state, shipment, "FAC-B", at(0, 10))
    assert isinstance(route, Route)
    # Direct lane blocked: either a lane detour (A->C->B) or synthetic; the
    # cost metric picks the cheaper. A->C->B costs 520+495 fixed vs synthetic.
    assert route.lane_ids != ["LANE-AB"]
    assert route.lane_ids == ["LANE-AC", "LANE-CB"]


def test_time_metric_changes_choice(world_state: NetworkState) -> None:
    shipment = make_shipment("SHP-T")
    shipment.origin_facility_id = "FAC-A"
    by_cost = model("cost").route(world_state, shipment, "FAC-B", at(0, 9))
    by_time = model("time").route(world_state, shipment, "FAC-B", at(0, 9))
    assert isinstance(by_cost, Route) and isinstance(by_time, Route)
    assert by_time.minutes <= by_cost.minutes


def test_route_is_deterministic(world_state: NetworkState) -> None:
    shipment = make_shipment("SHP-T")
    shipment.origin_facility_id = "FAC-A"
    routes = [model().route(world_state, shipment, "FAC-B", at(0, 9)) for _ in range(3)]
    assert all(r == routes[0] for r in routes)
