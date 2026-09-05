from datetime import date

from nodal.domain.capacity import CapacityVector
from nodal.events import EventStore, NetworkState, load_state
from nodal.events import catalog as ev
from nodal.events.state import buckets_between
from tests.helpers import at, draft, make_assignment, make_lot, make_reservation, make_shipment

D = date


def test_buckets_between_covers_half_open_interval() -> None:
    assert buckets_between(at(0, 6), at(2, 0)) == [D(2026, 9, 1), D(2026, 9, 2)]
    # Exact-midnight end excludes that day; degenerate interval keeps its bucket.
    assert buckets_between(at(0), at(0)) == [D(2026, 9, 1)]


def test_occupancy_from_fixture_lots(world_state: NetworkState) -> None:
    occ = world_state.occupancy("ZON-A1", D(2026, 9, 1))
    assert occ.slots == 10
    assert world_state.headroom("ZON-A1", D(2026, 9, 1)).slots == 90


def test_planned_departure_frees_future_buckets(world_state: NetworkState) -> None:
    # LOT-3 (20 slots) departs ZON-B1 at 2026-09-04T00:00.
    assert world_state.occupancy("ZON-B1", D(2026, 9, 3)).slots == 20
    assert world_state.occupancy("ZON-B1", D(2026, 9, 4)).slots == 0


def test_disruption_scales_effective_capacity(world_state: NetworkState) -> None:
    # DIS-1 halves ZON-B1 (60 slots) during [09-03, 09-05).
    assert world_state.effective_capacity("ZON-B1", D(2026, 9, 2)).slots == 60
    assert world_state.effective_capacity("ZON-B1", D(2026, 9, 3)).slots == 30
    assert world_state.effective_capacity("ZON-B1", D(2026, 9, 5)).slots == 60


def test_fits_rejects_future_full_bucket(world_state: NetworkState) -> None:
    size = CapacityVector(slots=35)
    # Day 09-02: headroom 60-20=40 -> fits. Day 09-03: (60/2)-20=10 -> does not.
    assert world_state.fits("ZON-B1", size, at(1), at(2))
    assert not world_state.fits("ZON-B1", size, at(1), at(3))


def test_facility_closed_zeroes_capacity(world_store: EventStore) -> None:
    from nodal.domain.entities import Disruption, DisruptionKind

    world_store.append(
        [
            draft(
                ev.DisruptionStarted(
                    disruption=Disruption(
                        id="DIS-CLOSE",
                        kind=DisruptionKind.FACILITY_CLOSED,
                        target_id="FAC-C",
                        from_ts=at(1),
                        until_ts=at(2),
                    )
                ),
                at(0, 9),
            )
        ]
    )
    state = load_state(world_store)
    # Closure covers [09-02T00, 09-03T00): exactly the 09-02 bucket.
    assert state.effective_capacity("ZON-C1", D(2026, 9, 1)).slots == 80
    assert state.effective_capacity("ZON-C1", D(2026, 9, 2)).slots == 0
    assert state.effective_capacity("ZON-C1", D(2026, 9, 3)).slots == 80


def test_reservation_and_lot_never_double_count(world_store: EventStore) -> None:
    """The §5 invariant, walked through arrival, bucket by bucket."""
    size = CapacityVector(slots=15)
    world_store.append(
        [draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-X", slots=15)), at(0, 9))]
    )
    world_store.append(
        [
            draft(
                ev.AllocationDecided(
                    shipment_id="SHP-X",
                    assignment=make_assignment(
                        "FAC-B", "ZON-B1", eta=at(1, 12), departure=at(5), reservation_id="RES-X"
                    ),
                    record={},
                ),
                at(0, 10),
            ),
            draft(
                ev.ReservationPlaced(
                    reservation=make_reservation(
                        "RES-X", "ZON-B1", "SHP-X", size=size, from_ts=at(1, 12), until_ts=at(5)
                    )
                ),
                at(0, 10),
            ),
            draft(ev.ShipmentDeparted(shipment_id="SHP-X"), at(0, 11)),
        ]
    )
    state = load_state(world_store)
    # Pre-arrival: the reservation occupies the buckets it overlaps (on top of LOT-3's
    # 20 slots) — and only those: it starts 09-02T12, so 09-01 is untouched.
    assert state.occupancy("ZON-B1", D(2026, 9, 1)).slots == 20
    assert state.occupancy("ZON-B1", D(2026, 9, 2)).slots == 35
    assert state.occupancy("ZON-B1", D(2026, 9, 3)).slots == 35

    world_store.append(
        [
            draft(ev.ShipmentArrived(shipment_id="SHP-X"), at(1, 12)),
            draft(
                ev.LotReceived(
                    lot=make_lot(
                        "LOT-X",
                        "ZON-B1",
                        size=size,
                        shipment_id="SHP-X",
                        planned_departure=at(5),
                    )
                ),
                at(1, 12),
            ),
        ]
    )
    state = load_state(world_store)
    # Post-arrival: lot counts, reservation is consumed — never both. LOT-3's 20
    # slots leave at their planned departure on 09-04; LOT-X's 15 remain.
    for day in (D(2026, 9, 2), D(2026, 9, 3), D(2026, 9, 4)):
        expected = 15 if day >= D(2026, 9, 4) else 35
        assert state.occupancy("ZON-B1", day).slots == expected
    # Terminal shipment: compacted out of state along with its reservation (§4);
    # the log retains both.
    assert "RES-X" not in state.reservations
    assert "SHP-X" not in state.shipments


def test_stock_by_commodity_group(world_state: NetworkState) -> None:
    assert world_state.stock("FAC-A", "general") == 40
    assert world_state.stock("FAC-A", "perishable") == 12
    assert world_state.stock("FAC-B", "general") == 25
    assert world_state.demand_rate("FAC-B", "general") == 12


def test_zone_utilization_peaks_across_dimensions(world_state: NetworkState) -> None:
    # ZON-A1: 10/100 slots, 8_000_000/200_000_000 g -> peak is slots at 10%.
    assert world_state.zone_utilization("ZON-A1", D(2026, 9, 1)) == 0.1
    assert world_state.facility_peak_utilization("FAC-A", [D(2026, 9, 1)]) == 0.15


def test_past_bucket_queries_are_rejected(world_state: NetworkState) -> None:
    """Historical buckets are served by state_at, not by the live state (§5)."""
    import pytest

    with pytest.raises(ValueError, match="state_at"):
        world_state.occupancy("ZON-A1", D(2026, 8, 31))
    with pytest.raises(ValueError, match="state_at"):
        world_state.effective_capacity("ZON-A1", D(2026, 8, 30))


def test_capacity_reduction_stacking_is_order_independent(world_store: EventStore) -> None:
    """Multiple capacity_reduced disruptions stack multiplicatively with a single
    flooring — id naming/order must not change the physics."""
    from nodal.domain.entities import Disruption, DisruptionKind

    def reduction(disruption_id: str, magnitude: float) -> ev.DisruptionStarted:
        return ev.DisruptionStarted(
            disruption=Disruption(
                id=disruption_id,
                kind=DisruptionKind.CAPACITY_REDUCED,
                target_id="ZON-C1",
                from_ts=at(1),
                until_ts=at(3),
                magnitude=magnitude,
            )
        )

    world_store.append(
        [draft(reduction("DIS-A", 0.3), at(0, 9)), draft(reduction("DIS-B", 0.5), at(0, 9))]
    )
    state = load_state(world_store)
    # 80 * (1-0.3) * (1-0.5) = 28, floored once.
    assert state.effective_capacity("ZON-C1", D(2026, 9, 2)).slots == 28


def test_naive_datetimes_rejected_by_queries(world_state: NetworkState) -> None:
    from datetime import datetime

    import pytest

    from nodal.events.state import buckets_between

    naive = datetime(2026, 9, 2, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        world_state.active_disruptions(naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        buckets_between(naive, naive)


def test_calendar_queries_through_state(world_state: NetworkState) -> None:
    # FAC-C: Mon-Fri 08:00-18:00 America/Chicago; state time is 09-01T08:00 UTC = 03:00 local.
    assert world_state.last_ts is not None
    assert not world_state.facility_open("FAC-C", world_state.last_ts)
    reopens = world_state.facility_next_open("FAC-C", world_state.last_ts)
    assert reopens == at(0, 13)  # 08:00 CDT
    assert world_state.facility_open("FAC-A", world_state.last_ts)  # no calendar: 24/7
