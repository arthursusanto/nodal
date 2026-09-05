"""Tiered re-optimization (§7.6) — stage 5 acceptance."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nodal.allocate import ObjectiveConfig
from nodal.allocate.batch import commit_batch, solve_batch
from nodal.allocate.reopt import reoptimize
from nodal.domain.entities import Disruption, DisruptionKind
from nodal.events import EventStore, load_state
from nodal.events import catalog as ev
from nodal.events.envelope import EventDraft
from nodal.worlds import load_world

CORE = ObjectiveConfig(packs=["core"])


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 9, 1 + day, hour, 0, tzinfo=UTC)


@contextmanager
def _world(tmp_path: Path, text: str):
    world = tmp_path / "world.yaml"
    world.write_text(text, encoding="utf-8")
    store = EventStore(tmp_path / "world.sqlite3")
    try:
        load_world(world, store)
        yield store
    finally:
        store.close()


def _allocate(store: EventStore, shipment_ids: list[str], now: datetime) -> None:
    """Book the given planned shipments via a batch solve and commit."""
    state = load_state(store)
    result = solve_batch(state, shipment_ids, CORE, now, batch_id="SETUP")
    assert all(result.assignments[sid] is not None for sid in shipment_ids)
    commit_batch(store, result)


def _assert_capacity_sane(state) -> None:  # type: ignore[no-untyped-def]
    """No zone bucket may hold more occupancy (lots + live reservations) than
    its effective capacity — the invariant a missed reservation release breaks."""
    from nodal.domain.capacity import DIMENSIONS

    for zone_id in state.zones:
        days = set()
        for reservation in state.reservations_on_zone(zone_id):
            day = reservation.from_ts.date()
            while day <= reservation.until_ts.date():
                days.add(day)
                day = day + timedelta(days=1)
        for day in sorted(days):
            occupancy = state.occupancy(zone_id, day)
            effective = state.effective_capacity(zone_id, day)
            for dim in DIMENSIONS:
                cap = effective.get(dim)
                if cap is not None:
                    assert occupancy.demand(dim) <= cap, (
                        f"{zone_id}/{dim}/{day}: {occupancy.demand(dim)} > {cap}"
                    )


def _disrupt(store: EventStore, disruption: Disruption) -> None:
    store.append(
        [EventDraft(ts=disruption.from_ts, payload=ev.DisruptionStarted(disruption=disruption))]
    )


# Two facilities, both feasible; shipments book a day ahead so allocations
# exist with future stays when the disruption lands.
CLOSURE_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 50 } }]
  - id: F2
    lat: 40.0
    lon: -100.3
    zones: [{ id: Z2, kind: rack, capacity: { slots: 50 } }]
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 8, size: { slots: 8 } }]
    requirements: { dwell_days: 2 }
  - id: S2
    origin_lat: 40.0
    origin_lon: -100.28
    ready: 2026-09-01T06:00:00+00:00
    lines: [{ sku: Y, group: general, quantity: 8, size: { slots: 8 } }]
    requirements: { dwell_days: 2 }
"""


def test_closure_reopt_tier1_moves_only_the_affected(tmp_path: Path) -> None:
    """Tier 1: the shipment booked into the closing facility moves; the other
    keeps its assignment with no events; the audit chain links old -> new."""
    with _world(tmp_path, CLOSURE_WORLD) as store:
        now = at(0, 12)
        _allocate(store, ["S1", "S2"], now)
        state = load_state(store)
        assert state.shipments["S1"].assigned is not None
        assert state.shipments["S1"].assigned.facility_id == "F1"  # near its origin
        assert state.shipments["S2"].assigned is not None
        assert state.shipments["S2"].assigned.facility_id == "F2"
        old_decide_seq = store.last_entity_event_seq("shipment", "S1", "AllocationDecided")
        assert old_decide_seq is not None

        disrupt_at = at(0, 13)
        _disrupt(
            store,
            Disruption(
                id="DIS-CLOSE",
                kind=DisruptionKind.FACILITY_CLOSED,
                target_id="F1",
                from_ts=disrupt_at,
                until_ts=at(5),
                magnitude=1.0,
            ),
        )
        state = load_state(store)
        result = reoptimize(store, state, "DIS-CLOSE", CORE, disrupt_at)

        assert result.tier == 1
        assert result.affected == ["S1"]  # S2 is untouched by the closure
        assert result.changed == ["S1"]
        assert result.released == []
        assert result.result is not None
        assert result.result.assignments["S1"] == ("F2", "Z2")
        record = result.result.records["S1"]
        assert record.reopt_tier == 1
        assert record.reopt_trigger == "DIS-CLOSE"

        # The audit chain: supersede names the actual old decision seq.
        supersedes = [e for e in result.envelopes if isinstance(e.payload, ev.AllocationSuperseded)]
        assert [(e.payload.shipment_id, e.payload.old_decision_seq) for e in supersedes] == [
            ("S1", old_decide_seq)
        ]
        # And the full log replays: supersede-then-decide is a legal transition.
        replayed = load_state(store)
        assert replayed.shipments["S1"].assigned is not None
        assert replayed.shipments["S1"].assigned.facility_id == "F2"
        assert replayed.shipments["S2"].assigned is not None
        assert replayed.shipments["S2"].assigned.facility_id == "F2"
        # S2's reservation was never touched.
        assert store.last_entity_event_seq("shipment", "S2", "AllocationSuperseded") is None


# Cascade: F1 closes; its shipment S1 needs a forklift, which the roomy F3
# lacks; the only forklift alternative F2 is exactly filled by S2 (which has no
# equipment needs). Tier 1 dead-ends; tier 2 must move S2 to F3 to admit S1.
CASCADE_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    tags: ["equip:forklift"]
    equipment: { "equip:forklift": 2 }
    zones: [{ id: Z1, kind: rack, capacity: { slots: 10 } }]
  - id: F2
    lat: 40.0
    lon: -100.25
    tags: ["equip:forklift"]
    equipment: { "equip:forklift": 2 }
    zones: [{ id: Z2, kind: rack, capacity: { slots: 10 } }]
  - id: F3
    lat: 40.0
    lon: -103.0
    zones: [{ id: Z3, kind: rack, capacity: { slots: 40 } }]
shipments:
  - id: S2
    origin_lat: 40.0
    origin_lon: -100.24
    ready: 2026-09-01T06:00:00+00:00
    lines: [{ sku: Y, group: general, quantity: 10, size: { slots: 10 } }]
    requirements: { dwell_days: 2 }
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 10, size: { slots: 10 } }]
    requirements: { required_tags: ["equip:forklift"], dwell_days: 2 }
"""


def test_cascade_escalates_to_tier2_and_finds_the_chain(tmp_path: Path) -> None:
    with _world(tmp_path, CASCADE_WORLD) as store:
        now = at(0, 12)
        # Book S2 first (fills F2), then S1 (lands at F1).
        _allocate(store, ["S2"], now)
        _allocate(store, ["S1"], now)
        state = load_state(store)
        assert state.shipments["S2"].assigned is not None
        assert state.shipments["S2"].assigned.facility_id == "F2"
        assert state.shipments["S1"].assigned is not None
        assert state.shipments["S1"].assigned.facility_id == "F1"

        disrupt_at = at(0, 13)
        _disrupt(
            store,
            Disruption(
                id="DIS-CASCADE",
                kind=DisruptionKind.FACILITY_CLOSED,
                target_id="F1",
                from_ts=disrupt_at,
                until_ts=at(6),
                magnitude=1.0,
            ),
        )
        state = load_state(store)
        result = reoptimize(store, state, "DIS-CASCADE", CORE, disrupt_at)

        assert result.tier == 2
        assert set(result.affected) == {"S1", "S2"}
        assert result.result is not None
        assert result.result.assignments["S1"] == ("F2", "Z2")  # displaced into F2
        assert result.result.assignments["S2"] == ("F3", "Z3")  # incumbent moved aside
        assert result.released == []
        assert result.result.records["S2"].reopt_tier == 2
        # Both moves are audited and the log replays with sane capacity.
        replayed = load_state(store)
        assert replayed.shipments["S1"].assigned is not None
        assert replayed.shipments["S1"].assigned.facility_id == "F2"
        assert replayed.shipments["S2"].assigned is not None
        assert replayed.shipments["S2"].assigned.facility_id == "F3"
        _assert_capacity_sane(replayed)


# Expansion-release regression: F1 closes; its shipment S1 (forklift) can only
# go to F2, whose single zone is exactly filled by S2 — which sits far from home
# and, needing a forklift itself, has no third option. Tier 2 must displace S2
# into UNASSIGNED, and that displaced expansion member must be superseded so its
# reservation is actually released; missing it would overbook Z2 forever.
EXPANSION_RELEASE_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.2
    tags: ["equip:forklift"]
    equipment: { "equip:forklift": 2 }
    zones: [{ id: Z1, kind: rack, capacity: { slots: 10 } }]
  - id: F2
    lat: 40.0
    lon: -100.25
    tags: ["equip:forklift"]
    equipment: { "equip:forklift": 2 }
    zones: [{ id: Z2, kind: rack, capacity: { slots: 10 } }]
  - id: F3
    lat: 40.0
    lon: -100.5
    zones: [{ id: Z3, kind: rack, capacity: { slots: 40 } }]
shipments:
  - id: S2
    origin_lat: 40.0
    origin_lon: -108.0
    ready: 2026-09-01T06:00:00+00:00
    lines: [{ sku: Y, group: general, quantity: 10, size: { slots: 10 } }]
    requirements: { required_tags: ["equip:forklift"], dwell_days: 2 }
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.26
    ready: 2026-09-01T06:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 10, size: { slots: 10 } }]
    requirements: { required_tags: ["equip:forklift"], dwell_days: 2 }
"""


def test_tier2_releases_the_displaced_expansion_member(tmp_path: Path) -> None:
    with _world(tmp_path, EXPANSION_RELEASE_WORLD) as store:
        now = at(0, 12)
        _allocate(store, ["S2"], now)  # fills F2 (nearest forklift facility)
        _allocate(store, ["S1"], now)  # lands at F1
        state = load_state(store)
        assert state.shipments["S2"].assigned is not None
        assert state.shipments["S2"].assigned.facility_id == "F2"
        assert state.shipments["S1"].assigned is not None
        assert state.shipments["S1"].assigned.facility_id == "F1"

        disrupt_at = at(0, 13)
        _disrupt(
            store,
            Disruption(
                id="DIS-EXP",
                kind=DisruptionKind.FACILITY_CLOSED,
                target_id="F1",
                from_ts=disrupt_at,
                until_ts=at(6),
                magnitude=1.0,
            ),
        )
        state = load_state(store)
        result = reoptimize(store, state, "DIS-EXP", CORE, disrupt_at)

        assert result.tier == 2
        assert result.result is not None
        # S1 (2 km from F2) takes the slot; the far-from-home expansion member
        # is displaced to unassigned and MUST be released.
        assert result.result.assignments["S1"] == ("F2", "Z2")
        assert result.result.assignments["S2"] is None
        assert result.released == ["S2"]
        replayed = load_state(store)
        assert replayed.shipments["S1"].assigned is not None
        assert replayed.shipments["S1"].assigned.facility_id == "F2"
        assert replayed.shipments["S2"].status.value == "planned"  # superseded
        assert replayed.active_reservations_of("S2") == []  # reservation released
        _assert_capacity_sane(replayed)


# Churn: S1 books into F1 while the nearer F2 sits congested behind a decoy
# lot. The lot then drains, F2 becomes marginally cheaper, and a crane outage
# at F1 (equipment S1 never needed) flags the booking as affected. With the
# default churn penalty the booking stays put; at zero churn it chases the
# marginal saving — churn is exactly that difference.
CHURN_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    tags: ["equip:forklift", "equip:crane"]
    equipment: { "equip:forklift": 2, "equip:crane": 1 }
    zones: [{ id: Z1, kind: rack, capacity: { slots: 50 } }]
  - id: F2
    lat: 40.0
    lon: -100.03
    tags: ["equip:forklift"]
    equipment: { "equip:forklift": 2 }
    zones: [{ id: Z2, kind: rack, capacity: { slots: 50 } }]
lots:
  - { id: DECOY, zone: Z2, group: general, quantity: 40, size: { slots: 40 } }
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 8, size: { slots: 8 } }]
    requirements: { dwell_days: 2 }
"""


def test_churn_keeps_incumbents_against_marginal_gains(tmp_path: Path) -> None:
    from nodal.domain.capacity import CapacityVector

    with _world(tmp_path, CHURN_WORLD) as store:
        now = at(0, 12)
        _allocate(store, ["S1"], now)
        state = load_state(store)
        assert state.shipments["S1"].assigned is not None
        assert state.shipments["S1"].assigned.facility_id == "F1"  # F2 congested

        # The decoy drains: F2 is now empty and marginally cheaper than F1.
        store.append(
            [
                EventDraft(
                    ts=at(0, 13),
                    payload=ev.LotQuantityAdjusted(
                        lot_id="DECOY",
                        new_quantity=0,
                        new_size=CapacityVector(slots=0, volume_l=0, weight_g=0),
                        reason="drained",
                    ),
                )
            ]
        )
        disrupt_at = at(0, 14)
        _disrupt(
            store,
            Disruption(
                id="DIS-CRANE",
                kind=DisruptionKind.EQUIPMENT_DOWN,
                target_id="F1",
                detail="equip:crane",
                from_ts=disrupt_at,
                until_ts=at(6),
                magnitude=1.0,
            ),
        )
        state = load_state(store)
        result = reoptimize(store, state, "DIS-CRANE", CORE, disrupt_at)
        assert result.affected == ["S1"]  # touched, so re-examined
        assert result.changed == []  # ...but churn keeps the booking
        assert result.envelopes == []

        free = CORE.model_copy(update={"churn_penalty": 0.0})
        result_free = reoptimize(
            store, state, "DIS-CRANE", free, disrupt_at, batch_prefix="FREE", commit=False
        )
        assert result_free.result is not None
        assert result_free.result.assignments["S1"] == ("F2", "Z2")  # chased the saving
        assert result_free.changed == ["S1"]
