import pytest

from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import ShipmentStatus
from nodal.events import EventStore, FoldError, load_state
from nodal.events import catalog as ev
from tests.helpers import at, draft, make_assignment, make_lot, make_reservation, make_shipment

SIZE = CapacityVector(slots=15)


def _allocate_batch(shipment_id: str) -> list[ev.EventPayload]:
    assignment = make_assignment(
        "FAC-B", "ZON-B1", eta=at(1, 12), departure=at(6), reservation_id="RES-9"
    )
    reservation = make_reservation(
        "RES-9", "ZON-B1", shipment_id, size=SIZE, from_ts=at(1, 12), until_ts=at(6)
    )
    return [
        ev.AllocationDecided(shipment_id=shipment_id, assignment=assignment, record={}),
        ev.ReservationPlaced(reservation=reservation),
    ]


def test_full_lifecycle_releases_reservation(world_store: EventStore) -> None:
    world_store.append(
        [draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-X", slots=15)), at(0, 12))]
    )
    world_store.append([draft(p, at(0, 13)) for p in _allocate_batch("SHP-X")])
    state = load_state(world_store)
    assert state.shipments["SHP-X"].status is ShipmentStatus.ALLOCATED
    assert [r.id for r in state.reservations_on_zone("ZON-B1")] == ["RES-9"]

    world_store.append([draft(ev.ShipmentDeparted(shipment_id="SHP-X"), at(0, 14))])
    world_store.append(
        [
            draft(ev.ShipmentArrived(shipment_id="SHP-X"), at(1, 12)),
            draft(
                ev.LotReceived(lot=make_lot("LOT-X", "ZON-B1", size=SIZE, shipment_id="SHP-X")),
                at(1, 12),
            ),
        ]
    )
    state = load_state(world_store)
    # Arrived = terminal: shipment and reservation are compacted out of the folded
    # state (§4); the lot — the physical inventory — remains.
    assert "SHP-X" not in state.shipments
    assert "RES-9" not in state.reservations
    assert state.reservations_on_zone("ZON-B1") == []
    assert "LOT-X" in {lot.id for lot in state.lots_in_zone("ZON-B1")}


def test_cancel_releases_reservation(world_store: EventStore) -> None:
    world_store.append([draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-X")), at(0, 12))])
    world_store.append([draft(p, at(0, 13)) for p in _allocate_batch("SHP-X")])
    world_store.append([draft(ev.ShipmentCancelled(shipment_id="SHP-X"), at(0, 15))])
    state = load_state(world_store)
    # Cancelled = terminal: compacted, and the zone shows no active reservation.
    assert "SHP-X" not in state.shipments
    assert "RES-9" not in state.reservations
    assert state.reservations_on_zone("ZON-B1") == []


def test_supersede_returns_to_planned_and_releases(world_store: EventStore) -> None:
    world_store.append([draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-X")), at(0, 12))])
    world_store.append([draft(p, at(0, 13)) for p in _allocate_batch("SHP-X")])
    world_store.append(
        [
            draft(
                ev.AllocationSuperseded(shipment_id="SHP-X", old_decision_seq=1, reason="test"),
                at(0, 16),
            )
        ]
    )
    state = load_state(world_store)
    shipment = state.shipments["SHP-X"]
    assert shipment.status is ShipmentStatus.PLANNED
    assert shipment.assigned is None
    assert state.reservations["RES-9"].released_at is not None


def test_depart_requires_allocation(world_store: EventStore) -> None:
    world_store.append([draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-X")), at(0, 12))])
    world_store.append([draft(ev.ShipmentDeparted(shipment_id="SHP-X"), at(0, 13))])
    with pytest.raises(FoldError, match="cannot depart"):
        load_state(world_store)


def test_duplicate_lot_rejected(world_store: EventStore) -> None:
    lot = make_lot("LOT-DUP", "ZON-A1", size=CapacityVector(slots=1))
    world_store.append([draft(ev.LotReceived(lot=lot), at(0, 12))])
    world_store.append([draft(ev.LotReceived(lot=lot), at(0, 13))])
    with pytest.raises(FoldError, match="duplicate lot"):
        load_state(world_store)


def test_unknown_zone_rejected(world_store: EventStore) -> None:
    lot = make_lot("LOT-Z", "ZON-NOPE", size=CapacityVector(slots=1))
    world_store.append([draft(ev.LotReceived(lot=lot), at(0, 12))])
    with pytest.raises(FoldError, match="unknown zone"):
        load_state(world_store)


def test_fold_error_carries_sequence_context(world_store: EventStore) -> None:
    world_store.append([draft(ev.ShipmentDeparted(shipment_id="SHP-NOPE"), at(0, 12))])
    with pytest.raises(FoldError, match=r"\[seq \d+ ShipmentDeparted\]"):
        load_state(world_store)


def test_double_decide_requires_supersede(world_store: EventStore) -> None:
    """Re-deciding an allocated shipment without AllocationSuperseded would leak
    the prior reservation (double-booking capacity) — the fold refuses it."""
    world_store.append([draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-X")), at(0, 12))])
    world_store.append([draft(p, at(0, 13)) for p in _allocate_batch("SHP-X")])
    second = make_assignment(
        "FAC-A", "ZON-A1", eta=at(1, 12), departure=at(6), reservation_id="RES-10"
    )
    world_store.append(
        [
            draft(
                ev.AllocationDecided(shipment_id="SHP-X", assignment=second, record={}),
                at(0, 14),
            )
        ]
    )
    with pytest.raises(FoldError, match="cannot allocate"):
        load_state(world_store)


def test_terminal_shipment_id_cannot_be_reused(world_store: EventStore) -> None:
    """Compaction removes the shipment object but must not re-open its id (§4):
    re-registering a compacted id would let reservation ids collide."""
    world_store.append([draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-X")), at(0, 12))])
    world_store.append([draft(ev.ShipmentCancelled(shipment_id="SHP-X"), at(0, 13))])
    world_store.append([draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-X")), at(0, 14))])
    with pytest.raises(FoldError, match="duplicate shipment"):
        load_state(world_store)


def test_duplicate_entity_guards(world_store: EventStore) -> None:
    """Every registration event refuses an id that already exists."""
    from nodal.domain.entities import Disruption, DisruptionKind, Facility, Lane, StorageZone

    state = load_state(world_store)
    cases: list[tuple[ev.EventPayload, str]] = [
        (
            ev.FacilityRegistered(facility=Facility(id="FAC-A", name="dup", lat=0.0, lon=0.0)),
            "duplicate facility",
        ),
        (
            ev.ZoneRegistered(
                zone=StorageZone(id="ZON-A1", facility_id="FAC-A", kind="rack", capacity=SIZE)
            ),
            "duplicate zone",
        ),
        (
            ev.LaneRegistered(
                lane=Lane(
                    id="LANE-AB",
                    from_facility_id="FAC-A",
                    to_facility_id="FAC-B",
                    distance_km=1.0,
                    minutes=1,
                )
            ),
            "duplicate lane",
        ),
        (
            ev.ShipmentRegistered(shipment=make_shipment("SHP-1")),
            "duplicate shipment",
        ),
        (
            ev.DisruptionStarted(
                disruption=Disruption(
                    id="DIS-1",
                    kind=DisruptionKind.CAPACITY_REDUCED,
                    target_id="ZON-B1",
                    from_ts=at(1),
                    until_ts=at(2),
                )
            ),
            "duplicate disruption",
        ),
    ]
    from nodal.events.envelope import Envelope
    from nodal.events.fold import apply_event

    for payload, match in cases:
        entity_type, entity_id = payload.entity_ref()
        envelope = Envelope(
            seq=state.last_seq + 1,
            id=f"EVT-{state.last_seq + 1}",
            ts=at(0, 12),
            type=payload.EVENT_TYPE,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
            actor="test",
        )
        with pytest.raises(FoldError, match=match):
            apply_event(state, envelope)


def test_state_at_mid_arrival_batch_never_double_counts(world_store: EventStore) -> None:
    """A `state_at` cut between an arrival batch's events (different timestamps in
    one atomic append) must not see lot + reservation together: LotReceived
    consumes the shipment's reservation on that zone structurally (§5)."""
    world_store.append(
        [draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-X", slots=15)), at(0, 12))]
    )
    world_store.append([draft(p, at(0, 13)) for p in _allocate_batch("SHP-X")])
    world_store.append([draft(ev.ShipmentDeparted(shipment_id="SHP-X"), at(0, 14))])
    # One atomic batch, receipt first, arrival two minutes later.
    world_store.append(
        [
            draft(
                ev.LotReceived(lot=make_lot("LOT-X", "ZON-B1", size=SIZE, shipment_id="SHP-X")),
                at(1, 12),
            ),
            draft(ev.ShipmentArrived(shipment_id="SHP-X"), at(1, 12, 2)),
        ]
    )
    from datetime import date

    sliced = load_state(world_store, at=at(1, 12, 1))
    assert sliced.reservations["RES-9"].released_at == at(1, 12)
    assert sliced.occupancy("ZON-B1", date(2026, 9, 3)).slots == 15 + 20  # lot + LOT-3, no res
