"""Snapshot equivalence and `state_at` (§4 acceptance)."""

from datetime import datetime

import pytest

from nodal.domain.capacity import CapacityVector
from nodal.events import EventStore, NetworkState, ensure_snapshots, fold, load_state
from nodal.events import catalog as ev
from tests.helpers import at, draft, make_lot


def _grow_world(store: EventStore) -> None:
    """Append a few dozen inventory events after the fixture world."""
    base_seq = 0
    for day in range(1, 5):
        drafts = []
        for i in range(6):
            base_seq += 1
            drafts.append(
                draft(
                    ev.LotReceived(
                        lot=make_lot(f"LOT-G{base_seq}", "ZON-A1", size=CapacityVector(slots=1))
                    ),
                    at(day, i),
                )
            )
        store.append(drafts)


def _reference_state(store: EventStore, upto: int) -> NetworkState:
    """Fold from scratch, ignoring snapshots entirely."""
    return fold(store.read(1, upto))


def test_fold_snapshot_tail_equivalence(world_store: EventStore) -> None:
    """§4 property: fold(snapshot + tail) == fold(all events), at every boundary."""
    from nodal.events import load_state_bytes

    _grow_world(world_store)
    written = ensure_snapshots(world_store, every=7)
    assert written == world_store.last_seq() // 7
    head = world_store.last_seq()
    reference = _reference_state(world_store, head)
    for boundary in world_store.snapshot_seqs():
        snapshot = world_store.latest_snapshot(max_upto_seq=boundary)
        assert snapshot is not None and snapshot[0] == boundary
        state = load_state_bytes(snapshot[1])
        fold(world_store.read(boundary + 1, head), into=state)
        assert state.model_dump_json() == reference.model_dump_json()
    # And the normal read path (latest snapshot + tail) agrees too.
    assert load_state(world_store).model_dump_json() == reference.model_dump_json()


@pytest.mark.parametrize(
    "probe",
    [
        at(0, 3),  # between master data and the first shipment
        at(0, 6),  # exactly on an event timestamp (inclusive)
        at(2, 12),  # mid-growth
        at(30),  # far after the head
    ],
)
def test_state_at_matches_scratch_fold(world_store: EventStore, probe: datetime) -> None:
    _grow_world(world_store)
    ensure_snapshots(world_store, every=10)
    upto = world_store.max_seq_at(probe)
    reference = _reference_state(world_store, upto)
    assert load_state(world_store, at=probe).model_dump_json() == reference.model_dump_json()


def test_state_at_before_first_event_is_empty(world_store: EventStore) -> None:
    state = load_state(world_store, at=at(-10))
    assert state.last_seq == 0
    assert state.facilities == {}


def test_state_grows_monotonically(world_store: EventStore) -> None:
    _grow_world(world_store)
    lots_seen = -1
    for probe in [at(-1), at(0), at(1, 3), at(2, 3), at(3, 3), at(10)]:
        state = load_state(world_store, at=probe)
        assert len(state.lots) >= lots_seen
        lots_seen = len(state.lots)


def test_snapshot_path_matches_scratch_fold_by_pydantic_equality(
    world_store: EventStore,
) -> None:
    """Private indexes must converge identically on both paths — pydantic `==`
    compares private attrs, so this catches empty-key divergence that JSON
    comparison structurally cannot see."""
    from nodal.domain.capacity import CapacityVector
    from nodal.domain.entities import Disruption, DisruptionKind
    from tests.helpers import make_lot

    # Handler-diverse activity BEFORE the snapshot boundaries: moves, releases,
    # cancellations, disruption ends — the operations that empty index buckets.
    world_store.append(
        [
            draft(
                ev.DisruptionStarted(
                    disruption=Disruption(
                        id="DIS-T",
                        kind=DisruptionKind.CAPACITY_REDUCED,
                        target_id="ZON-C1",
                        from_ts=at(0, 9),
                        until_ts=at(9),
                        magnitude=0.5,
                    )
                ),
                at(0, 9),
            ),
            draft(
                ev.LotReceived(lot=make_lot("LOT-M", "ZON-C1", size=CapacityVector(slots=2))),
                at(0, 10),
            ),
            draft(ev.LotMoved(lot_id="LOT-M", to_zone_id="ZON-B1"), at(0, 11)),
            draft(ev.LotMoved(lot_id="LOT-3", to_zone_id="ZON-B2"), at(0, 12)),
            draft(ev.DisruptionEnded(disruption_id="DIS-T"), at(0, 13)),
            draft(ev.ShipmentCancelled(shipment_id="SHP-1", reason="test"), at(0, 14)),
        ]
    )
    _grow_world(world_store)
    ensure_snapshots(world_store, every=5)
    head = world_store.last_seq()
    reference = _reference_state(world_store, head)
    via_snapshot = load_state(world_store)
    assert via_snapshot == reference  # full equality, private attrs included


def test_state_at_hand_checked_disruption_capacity(world_store: EventStore) -> None:
    """Roadmap stage 1: a hand-checked historical state through the snapshot path,
    including effective capacity under an active disruption."""
    from datetime import date

    _grow_world(world_store)
    ensure_snapshots(world_store, every=5)
    historical = load_state(world_store, at=at(2, 12))
    # DIS-1 halves ZON-B1 (60 slots) during [09-03, 09-05); LOT-3 (20 slots)
    # departs 09-04. Hand-checked: capacity 30, occupancy 20, headroom 10 on 09-03.
    assert historical.effective_capacity("ZON-B1", date(2026, 9, 3)).slots == 30
    assert historical.occupancy("ZON-B1", date(2026, 9, 3)).slots == 20
    assert historical.headroom("ZON-B1", date(2026, 9, 3)).slots == 10
    # at(2, 12) is 09-03T12Z — inside DIS-1's [09-03, 09-05) window.
    assert [d.id for d in historical.active_disruptions(at(2, 12))] == ["DIS-1"]
    assert historical.effective_capacity("ZON-B1", date(2026, 9, 5)).slots == 60
