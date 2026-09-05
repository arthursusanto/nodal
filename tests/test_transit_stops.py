"""Transit stops (§7.9): every facility a delivery touches is occupied.

The defect these cover is one model, not one bug: a pass-through facility used to
be a dimensionless point. It charged handling minutes on the lane and then took
up no space, was never checked against its own state, and was invisible to
re-optimization — so a plan could route a delivery through a shut facility, book
a cross-dock it had no room in, and keep a stale route because the hold had not
moved. Stops give every touch a window; everything else follows from that.
"""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from nodal.allocate import ObjectiveConfig, allocate, commit
from nodal.allocate.batch import commit_batch, solve_batch
from nodal.allocate.rebalance import generate_rebalancing_transfers
from nodal.allocate.records import DecisionRecord
from nodal.allocate.reopt import (
    affected_shipments,
    all_trapped_cargo,
    reoptimize,
    trapped_cargo,
)
from nodal.domain.entities import Disruption, DisruptionKind
from nodal.domain.units import OBJECTIVE_SCALE
from nodal.events import EventStore, load_state
from nodal.events import catalog as ev
from nodal.events.envelope import EventDraft
from nodal.events.state import buckets_between
from nodal.rules.messages import render_reject
from nodal.worlds import load_world

CORE = ObjectiveConfig(packs=["core"])
# Facility-level terms off: the objective is then exactly the sum of the
# separable per-pair contributions, which is what makes batch == scorer testable
# as an equality rather than an approximation.
SEPARABLE_ONLY = CORE.model_copy(
    update={"weights": CORE.weights.model_copy(update={"congestion": 0.0, "inv_balance": 0.0})}
)


def _world(tmp_path: Path, text: str, name: str = "world") -> EventStore:
    path = tmp_path / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    store = EventStore(tmp_path / f"{name}.sqlite3")
    load_world(path, store)
    return store


def _disrupt(store: EventStore, at: datetime, disruption: Disruption) -> None:
    store.append([EventDraft(ts=at, payload=ev.DisruptionStarted(disruption=disruption))])


def _lanes(record: DecisionRecord) -> list[str]:
    assert record.chosen is not None and record.chosen.itinerary is not None
    return record.chosen.itinerary.lane_ids


# Only FAC-HOLD has a rack zone, so it always takes the hold. Getting out to the
# customer means two hops, through FAC-M1 (cheap) or FAC-M2 (dearer), and the
# lanes are long enough that the SECOND hop rolls more than a day after the
# first. Everything else is a cross-dock that can only ever stage.
TWO_WAYS_OUT = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-HOLD
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-HOLD, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-M1
    lat: 40.0
    lon: -103.0
    zones: [{ id: ZON-M1, kind: cross-dock, capacity: { slots: 20 } }]
  - id: FAC-M2
    lat: 40.0
    lon: -103.0
    zones: [{ id: ZON-M2, kind: cross-dock, capacity: { slots: 20 } }]
  - id: FAC-EXIT
    lat: 40.0
    lon: -106.0
    zones: [{ id: ZON-EXIT, kind: cross-dock, capacity: { slots: 20 } }]
lanes:
  - { id: LANE-H2M1, from: FAC-HOLD, to: FAC-M1, km: 260, minutes: 1500, cost_fixed: 100 }
  - { id: LANE-M12E, from: FAC-M1, to: FAC-EXIT, km: 260, minutes: 1500, cost_fixed: 100 }
  - { id: LANE-H2M2, from: FAC-HOLD, to: FAC-M2, km: 260, minutes: 1500, cost_fixed: 150 }
  - { id: LANE-M22E, from: FAC-M2, to: FAC-EXIT, km: 260, minutes: 1500, cost_fixed: 150 }
shipments:
  - id: SHP-DEL
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -99.98
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -106.02 }
    hold_days: 3
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_the_cheap_two_hop_route_is_the_baseline(tmp_path: Path) -> None:
    """Undisrupted, the delivery takes the cheap hops and stages at each one."""
    with _world(tmp_path, TWO_WAYS_OUT) as store:
        record = allocate(load_state(store), "SHP-DEL", CORE)
    assert _lanes(record) == ["LANE-H2M1", "LANE-M12E"]
    assert record.chosen is not None and record.chosen.itinerary is not None
    assert [(s.facility_id, s.role.value, s.zone_id) for s in record.chosen.itinerary.stops] == [
        ("FAC-HOLD", "hold", "ZON-HOLD"),
        ("FAC-M1", "transit", "ZON-M1"),
        ("FAC-EXIT", "exit", "ZON-EXIT"),
    ]
    # Each pass-through stop books staging; the hold books its own reservation.
    assert [(b.facility_id, b.zone_id) for b in record.chosen.staging] == [
        ("FAC-M1", "ZON-M1"),
        ("FAC-EXIT", "ZON-EXIT"),
    ]


def test_a_closed_intermediate_hop_is_detected_and_routed_around(tmp_path: Path) -> None:
    """FAC-M1 is only ever passed through — nothing stays there — so the old
    stay-only closure check never saw it and the plan cross-docked through a shut
    facility. Its dwell window is now real, so the router takes the dearer hop."""
    closed = (
        TWO_WAYS_OUT
        + """
disruptions:
  - id: DIS-M1
    kind: facility_closed
    target: FAC-M1
    from: 2026-09-01T00:00:00+00:00
    until: 2026-10-01T00:00:00+00:00
"""
    )
    with _world(tmp_path, closed, "closed-hop") as store:
        record = allocate(load_state(store), "SHP-DEL", CORE)
    assert _lanes(record) == ["LANE-H2M2", "LANE-M22E"]
    assert record.chosen is not None and record.chosen.itinerary is not None
    assert all(s.facility_id != "FAC-M1" for s in record.chosen.itinerary.stops)


def _later_leg_window(store: EventStore) -> tuple[datetime, datetime, datetime]:
    """(hold end, second outbound leg departure, its arrival) on the cheap route."""
    record = allocate(load_state(store), "SHP-DEL", CORE)
    assert record.chosen is not None and record.chosen.itinerary is not None
    second = record.chosen.itinerary.legs[-2]  # the M1 -> EXIT hop
    assert second.lane_id == "LANE-M12E"
    return record.chosen.departure, second.depart, second.arrive


def test_a_block_on_only_a_later_leg_is_seen_by_the_planner(tmp_path: Path) -> None:
    """The second hop rolls more than a day after the hold ends. A block covering
    only ITS window is invisible to a single probe at the hold's end — which is
    what the planner used to take — so this pins per-leg evaluation."""
    with _world(tmp_path, TWO_WAYS_OUT, "timing") as store:
        hold_end, depart, arrive = _later_leg_window(store)
    assert depart > hold_end + timedelta(days=1)  # the two instants really differ
    blocked = (
        TWO_WAYS_OUT
        + f"""
disruptions:
  - id: DIS-LATE
    kind: lane_blocked
    target: LANE-M12E
    from: {(depart - timedelta(hours=1)).isoformat()}
    until: {(arrive + timedelta(hours=1)).isoformat()}
"""
    )
    with _world(tmp_path, blocked, "late-block") as store:
        state = load_state(store)
        assert state.last_ts is not None
        # Not active at the hold's end, which is the instant the old model used.
        assert not state.disruptions["DIS-LATE"].active_at(hold_end)
        record = allocate(state, "SHP-DEL", CORE)
    assert _lanes(record) == ["LANE-H2M2", "LANE-M22E"]


def test_a_block_on_only_a_later_leg_is_seen_by_reoptimization(tmp_path: Path) -> None:
    """Same window, arriving after the booking: the folded per-leg schedule is
    what lets the affected set match the leg the block actually closes."""
    with _world(tmp_path, TWO_WAYS_OUT, "timing") as store:
        hold_end, depart, arrive = _later_leg_window(store)

    with _world(tmp_path, TWO_WAYS_OUT, "reopt-late") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        record = allocate(state, "SHP-DEL", CORE, now)
        commit(store, record)
        booked = load_state(store).shipments["SHP-DEL"].assigned
        assert booked is not None
        assert booked.outbound_route == ["LANE-H2M1", "LANE-M12E"]
        assert [leg.lane_id for leg in booked.legs] == ["LANE-H2M1", "LANE-M12E"]

        _disrupt(
            store,
            now,
            Disruption(
                id="DIS-LATE",
                kind=DisruptionKind.LANE_BLOCKED,
                target_id="LANE-M12E",
                from_ts=depart - timedelta(hours=1),
                until_ts=arrive + timedelta(hours=1),
            ),
        )
        state = load_state(store)
        assert not state.disruptions["DIS-LATE"].active_at(hold_end)
        result = reoptimize(store, state, "DIS-LATE", CORE, now)
        assert result.affected == ["SHP-DEL"]
        assert result.changed == ["SHP-DEL"]

        moved = load_state(store).shipments["SHP-DEL"].assigned
        assert moved is not None
        assert moved.facility_id == "FAC-HOLD"  # the hold never had to move
        assert moved.outbound_route == ["LANE-H2M2", "LANE-M22E"]


def test_a_stale_route_is_superseded_exactly_once(tmp_path: Path) -> None:
    """Route-level staleness has to append, or the re-solve is computed and
    thrown away — and it must not append AGAIN on an identical replan, which is
    what the churn penalty exists to prevent."""
    with _world(tmp_path, TWO_WAYS_OUT, "churn") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        commit(store, allocate(state, "SHP-DEL", CORE, now))
        _disrupt(
            store,
            now,
            Disruption(
                id="DIS-M1",
                kind=DisruptionKind.FACILITY_CLOSED,
                target_id="FAC-M1",
                from_ts=now,
                until_ts=now + timedelta(days=30),
            ),
        )
        first = reoptimize(store, load_state(store), "DIS-M1", CORE, now)
        # The hold is unchanged — only the route is — so this is exactly the case
        # the old (facility, zone) comparison discarded.
        assert first.changed == ["SHP-DEL"]
        assert load_state(store).shipments["SHP-DEL"].assigned is not None
        assert load_state(store).shipments["SHP-DEL"].assigned.facility_id == "FAC-HOLD"  # type: ignore[union-attr]
        head = store.last_seq()

        second = reoptimize(store, load_state(store), "DIS-M1", CORE, now)
        assert second.changed == []
        assert second.envelopes == []
        assert store.last_seq() == head  # nothing appended the second time


def test_transit_staging_consumes_capacity_and_is_released(tmp_path: Path) -> None:
    """A staging dwell is a booking like any other: it occupies the zone over the
    buckets it spans, and cancelling the shipment gives every one of them back."""
    with _world(tmp_path, TWO_WAYS_OUT) as store:
        state = load_state(store)
        record = allocate(state, "SHP-DEL", CORE)
        assert record.chosen is not None
        commit(store, record)
        after = load_state(store)
        booked = {b.zone_id: b for b in record.chosen.staging}
        assert set(booked) == {"ZON-M1", "ZON-EXIT"}
        for zone_id, booking in booked.items():
            for day in buckets_between(booking.from_ts, booking.until_ts):
                assert after.occupancy(zone_id, day).slots == 5, (zone_id, day)
        assert {r.zone_id for r in after.active_reservations_of("SHP-DEL")} == {
            "ZON-HOLD",
            "ZON-M1",
            "ZON-EXIT",
        }

        store.append(
            [
                EventDraft(
                    ts=record.chosen.eta,
                    payload=ev.ShipmentCancelled(shipment_id="SHP-DEL", reason="test"),
                )
            ]
        )
        cleared = load_state(store)
        for zone_id, booking in booked.items():
            for day in buckets_between(booking.from_ts, booking.until_ts):
                assert cleared.occupancy(zone_id, day).slots == 0, (zone_id, day)


def test_a_full_cross_dock_pushes_the_route_to_the_other_hop(tmp_path: Path) -> None:
    """Capacity binds a dwell exactly as it binds a stay: with FAC-M1's cross-dock
    already full for the day the goods would pass through, that path is infeasible
    and the router takes the dearer one."""
    full = TWO_WAYS_OUT.replace(
        "shipments:",
        """lots:
  - { id: LOT-M1, zone: ZON-M1, group: general, quantity: 20, size: { slots: 20 } }
shipments:""",
    )
    with _world(tmp_path, full, "full-crossdock") as store:
        record = allocate(load_state(store), "SHP-DEL", CORE)
    assert _lanes(record) == ["LANE-H2M2", "LANE-M22E"]


# The goods must be held between -20 and -18. FAC-COLD-A and FAC-COLD-B can do
# that; FAC-MID is ambient and sits on the only lane path between them.
COLD_THROUGH_AMBIENT = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-COLD-A
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-A, kind: cold, capacity: { slots: 50 }, temp_c: [-25, -15] }]
  - id: FAC-COLD-B
    lat: 40.0
    lon: -104.0
    zones: [{ id: ZON-B, kind: cold, capacity: { slots: 50 }, temp_c: [-25, -15] }]
  - id: FAC-MID
    lat: 40.0
    lon: -102.0
    zones: [{ id: ZON-MID, kind: cross-dock, capacity: { slots: 50 } }]
lanes:
  - { id: LANE-A2M, from: FAC-COLD-A, to: FAC-MID, km: 170, minutes: 160, cost_fixed: 50 }
  - { id: LANE-M2B, from: FAC-MID, to: FAC-COLD-B, km: 170, minutes: 160, cost_fixed: 50 }
shipments:
  - id: SHP-COLD
    origin: FAC-COLD-A
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -104.02 }
    hold_days: 2
    requirements: { temp_c: [-20, -18] }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_cold_cargo_cannot_cross_dock_through_an_ambient_facility(tmp_path: Path) -> None:
    """Temperature binds wherever the goods sit, including for an hour on a dock.
    FAC-COLD-B is only reachable through ambient FAC-MID, so it is rejected — by
    name, naming the stop and the zone that refused — and the hold stays at
    FAC-COLD-A even though that means driving the whole way to the customer."""
    with _world(tmp_path, COLD_THROUGH_AMBIENT) as store:
        record = allocate(load_state(store), "SHP-COLD", CORE)
    assert record.chosen is not None
    assert record.chosen.facility_id == "FAC-COLD-A"
    rejected = {r.facility_id: r for r in record.rejected}
    assert "FAC-COLD-B" in rejected
    verdict = next(
        v for v in rejected["FAC-COLD-B"].facility_verdicts if v.constraint_id == "STOP_NO_STAGING"
    )
    assert verdict.data["facility"] == "FAC-MID"
    assert verdict.data["role"] == "transit"
    assert verdict.data["zones"] == ["ZON-MID"]
    # And the reason renders as a sentence, like every other rejection (§7.5).
    assert "FAC-MID" in render_reject(verdict)


TRAPPED_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-YARD
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-YARD, kind: rack, capacity: { slots: 50 } }]
  - id: FAC-A
    lat: 40.0
    lon: -100.4
    zones: [{ id: ZON-A, kind: rack, capacity: { slots: 50 } }]
  - id: FAC-B
    lat: 40.0
    lon: -100.6
    zones: [{ id: ZON-B, kind: rack, capacity: { slots: 50 } }]
lanes:
  - { id: LANE-Y2A, from: FAC-YARD, to: FAC-A, km: 40, minutes: 60, cost_fixed: 10 }
  - { id: LANE-Y2B, from: FAC-YARD, to: FAC-B, km: 60, minutes: 80, cost_fixed: 10 }
lots:
  - { id: LOT-1, zone: ZON-YARD, group: general, quantity: 5, size: { slots: 5 } }
shipments:
  - id: SHP-OUT
    origin: FAC-YARD
    ready: 2026-09-01T06:00:00+00:00
    requirements: { dwell_days: 2 }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_cargo_inside_a_closing_facility_is_flagged_not_re_routed(tmp_path: Path) -> None:
    """SHP-OUT's goods are physically sitting in FAC-YARD when it shuts. No plan
    can move them, so re-optimization must leave the booking exactly where it is
    and hand the operator a `trapped` entry to clear by hand."""
    with _world(tmp_path, TRAPPED_WORLD) as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        record = allocate(state, "SHP-OUT", CORE, now)
        assert record.chosen is not None
        commit(store, record)
        before = load_state(store).shipments["SHP-OUT"].assigned
        assert before is not None

        _disrupt(
            store,
            now,
            Disruption(
                id="DIS-YARD",
                kind=DisruptionKind.FACILITY_CLOSED,
                target_id="FAC-YARD",
                from_ts=now,
                until_ts=now + timedelta(days=30),
            ),
        )
        head = store.last_seq()
        result = reoptimize(store, load_state(store), "DIS-YARD", CORE, now)
        assert result.trapped == [
            type(result.trapped[0])(
                shipment_id="SHP-OUT", facility_id="FAC-YARD", disruption_id="DIS-YARD"
            )
        ]
        assert result.changed == []
        assert result.envelopes == []
        assert store.last_seq() == head  # the booking is untouched
        after = load_state(store).shipments["SHP-OUT"].assigned
        assert after is not None
        assert (after.facility_id, after.zone_ids) == (before.facility_id, before.zone_ids)


def test_a_future_booking_at_the_same_facility_is_still_re_planned(tmp_path: Path) -> None:
    """The exemption is for cargo that is already there, not for the facility. A
    shipment merely BOOKED into the closing facility — nothing of it present yet —
    keeps the ordinary §7.6 treatment and moves."""
    world = TRAPPED_WORLD.replace(
        "shipments:",
        """shipments:
  - id: SHP-IN
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    requirements: { dwell_days: 2 }
    lines: [{ sku: Y, group: general, quantity: 5, size: { slots: 5 } }]""",
    )
    with _world(tmp_path, world, "future") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        record = allocate(state, "SHP-IN", CORE, now)
        assert record.chosen is not None and record.chosen.facility_id == "FAC-YARD"
        commit(store, record)
        _disrupt(
            store,
            now,
            Disruption(
                id="DIS-YARD",
                kind=DisruptionKind.FACILITY_CLOSED,
                target_id="FAC-YARD",
                from_ts=now,
                until_ts=now + timedelta(days=30),
            ),
        )
        result = reoptimize(store, load_state(store), "DIS-YARD", CORE, now)
        assert result.changed == ["SHP-IN"]
        # SHP-OUT is still sitting in the shut yard unbooked, so it is trapped —
        # but SHP-IN, which owns nothing there yet, is re-planned like any other.
        assert [entry.shipment_id for entry in result.trapped] == ["SHP-OUT"]
        moved = load_state(store).shipments["SHP-IN"].assigned
        assert moved is not None and moved.facility_id != "FAC-YARD"


YARD_SHUT = """
disruptions:
  - id: DIS-YARD
    kind: facility_closed
    target: FAC-YARD
    from: 2026-09-01T00:00:00+00:00
    until: 2026-09-08T00:00:00+00:00
"""


def test_a_closed_origin_makes_every_candidate_infeasible(tmp_path: Path) -> None:
    """A departure from the origin facility is not a stop, so the stop machinery
    never saw it and the solver routed goods straight out of a shut yard. Every
    candidate must be rejected by name instead, and the reason must carry the
    closure's end — the goods are waiting, not stranded forever."""
    with _world(tmp_path, TRAPPED_WORLD + YARD_SHUT, "origin-closed") as store:
        record = allocate(load_state(store), "SHP-OUT", CORE)
    assert record.chosen is None
    assert {r.facility_id for r in record.rejected} == set(record.considered)
    for rejection in record.rejected:
        assert [v.constraint_id for v in rejection.facility_verdicts] == ["ORIGIN_CLOSED"]
        data = rejection.facility_verdicts[0].data
        assert data["facility"] == "FAC-YARD"
        assert data["disruption"] == "DIS-YARD"
        assert data["until"] == "2026-09-08T00:00:00+00:00"
    sentence = render_reject(record.rejected[0].facility_verdicts[0])
    assert "FAC-YARD" in sentence and "2026-09-08" in sentence


def test_a_batch_never_books_a_shipment_out_of_a_closed_origin(tmp_path: Path) -> None:
    """The same rule through the solver, which is the path the UI's commit runs:
    the shipment stays planned with nothing booked, and its record says why."""
    with _world(tmp_path, TRAPPED_WORLD + YARD_SHUT, "origin-batch") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        result = solve_batch(state, ["SHP-OUT"], CORE, now, batch_id="B-SHUT")
        assert result.assignments["SHP-OUT"] is None
        assert result.records["SHP-OUT"].chosen is None
        commit_batch(store, result)
        after = load_state(store).shipments["SHP-OUT"]
    assert after.status.value == "planned"
    assert after.assigned is None


def test_a_planned_shipment_at_a_closed_origin_is_trapped(tmp_path: Path) -> None:
    """Nothing is booked for it and nothing can be, so the queue has to badge it:
    otherwise a solve that leaves it sitting there looks like a solver failure
    rather than a shut door."""
    with _world(tmp_path, TRAPPED_WORLD + YARD_SHUT, "origin-trapped") as store:
        state = load_state(store)
        assert state.last_ts is not None
        trapped = trapped_cargo(state, state.disruptions["DIS-YARD"], state.last_ts)
    assert [(e.shipment_id, e.facility_id, e.disruption_id) for e in trapped] == [
        ("SHP-OUT", "FAC-YARD", "DIS-YARD")
    ]


def test_a_reopt_re_solve_never_books_out_of_a_closed_origin(tmp_path: Path) -> None:
    """A closure exempts what is inside it from re-planning, and a DIFFERENT
    disruption must not be the back door around that. A lane block matches
    SHP-OUT, but its goods are standing in a shut yard: the re-solve leaves it
    strictly alone — no new booking, and no RELEASE of the one it has, which
    would be an automatic change to trapped cargo just the same. It comes back
    on `trapped` named against the closure holding it, not the trigger."""
    # The yard is full, so SHP-OUT's booking really travels a lane out of it —
    # which is what gives a lane block something to catch.
    packed = TRAPPED_WORLD.replace(
        "{ id: LOT-1, zone: ZON-YARD, group: general, quantity: 5, size: { slots: 5 } }",
        "{ id: LOT-1, zone: ZON-YARD, group: general, quantity: 50, size: { slots: 50 } }",
    )
    with _world(tmp_path, packed, "reopt-origin") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        commit(store, allocate(state, "SHP-OUT", CORE, now))
        booked = load_state(store).shipments["SHP-OUT"].assigned
        assert booked is not None and booked.route == ["LANE-Y2A"]

        for disruption in (
            Disruption(
                id="DIS-YARD",
                kind=DisruptionKind.FACILITY_CLOSED,
                target_id="FAC-YARD",
                from_ts=now,
                until_ts=now + timedelta(days=30),
            ),
            Disruption(
                id="DIS-LANE",
                kind=DisruptionKind.LANE_BLOCKED,
                target_id="LANE-Y2A",
                from_ts=now,
                until_ts=now + timedelta(days=30),
            ),
        ):
            _disrupt(store, now, disruption)

        state = load_state(store)
        head = store.last_seq()
        reservations = {r.id: r.model_dump_json() for r in state.active_reservations_of("SHP-OUT")}
        assert reservations

        # The block, not the closure, is the trigger: the lane really does match
        # SHP-OUT, and the exemption still has to hold.
        assert affected_shipments(state, state.disruptions["DIS-LANE"], now) == ["SHP-OUT"]
        result = reoptimize(store, state, "DIS-LANE", CORE, now)
        assert result.affected == []
        assert result.changed == []
        assert result.released == []
        assert [(e.shipment_id, e.disruption_id) for e in result.trapped] == [
            ("SHP-OUT", "DIS-YARD")
        ]

        after = load_state(store)
        assert store.last_seq() == head, "a re-solve must append nothing for trapped cargo"
        assert after.shipments["SHP-OUT"].assigned == booked
        assert {
            r.id: r.model_dump_json() for r in after.active_reservations_of("SHP-OUT")
        } == reservations


def test_a_closure_after_a_stale_eta_still_traps_the_departure(tmp_path: Path) -> None:
    """The window a booking implies and the instant it departs are the same thing
    only while the plan is fresh. Once a readiness slip pushes `ready_at` past
    the recorded eta, a closure can land entirely AFTER that eta — touching no
    stop and reaching no part of `[now, eta]` — and still shut the door the goods
    must leave by. The origin-departure predicate is what sees it."""
    packed = TRAPPED_WORLD.replace(
        "{ id: LOT-1, zone: ZON-YARD, group: general, quantity: 5, size: { slots: 5 } }",
        "{ id: LOT-1, zone: ZON-YARD, group: general, quantity: 50, size: { slots: 50 } }",
    )
    with _world(tmp_path, packed, "stale-eta") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        commit(store, allocate(state, "SHP-OUT", CORE, now))
        booked = load_state(store).shipments["SHP-OUT"].assigned
        assert booked is not None and booked.facility_id == "FAC-A"

        # Readiness slips five days — well past the eta the booking recorded.
        slipped = now + timedelta(days=5)
        store.append(
            [
                EventDraft(
                    ts=now,
                    payload=ev.ShipmentReadyChanged(
                        shipment_id="SHP-OUT", new_ready=slipped, reason="test"
                    ),
                )
            ]
        )
        closure = Disruption(
            id="DIS-LATE-YARD",
            kind=DisruptionKind.FACILITY_CLOSED,
            target_id="FAC-YARD",
            from_ts=now + timedelta(days=2),
            until_ts=now + timedelta(days=10),
        )
        _disrupt(store, now, closure)

        state = load_state(store)
        assert state.shipments["SHP-OUT"].ready_at > booked.eta  # the plan really is stale
        # The old window test misses this closure entirely, in both roles.
        assert not closure.overlaps(now, max(now, booked.eta))
        assert not any(stop.facility_id == "FAC-YARD" for stop in booked.stops)

        assert affected_shipments(state, closure, now) == ["SHP-OUT"]
        assert [e.shipment_id for e in trapped_cargo(state, closure, now)] == ["SHP-OUT"]
        head = store.last_seq()
        result = reoptimize(store, state, "DIS-LATE-YARD", CORE, now)
        assert result.affected == []
        assert [(e.shipment_id, e.disruption_id) for e in result.trapped] == [
            ("SHP-OUT", "DIS-LATE-YARD")
        ]
        assert store.last_seq() == head
        assert load_state(store).shipments["SHP-OUT"].assigned == booked


def test_a_delivery_from_a_closed_origin_names_the_origin_not_the_entry(tmp_path: Path) -> None:
    """A delivery that starts inside the network enters at its own facility, so
    something did always reject it — but never by the right name: the entry stop
    came back STOP_NO_STAGING (a closure zeroes the zone's effective capacity, so
    the dwell fails on room rather than on the closure) and other candidates came
    back NO_ELIGIBLE_ZONE. The goods cannot leave at all, and the record has to
    say so, decided before any routing is attempted."""
    shut = (
        COLD_THROUGH_AMBIENT
        + """
disruptions:
  - id: DIS-COLD-A
    kind: facility_closed
    target: FAC-COLD-A
    from: 2026-09-01T00:00:00+00:00
    until: 2026-09-08T00:00:00+00:00
"""
    )
    with _world(tmp_path, shut, "delivery-origin") as store:
        record = allocate(load_state(store), "SHP-COLD", CORE)
    assert record.chosen is None
    verdicts = {v.constraint_id for r in record.rejected for v in r.facility_verdicts}
    assert verdicts == {"ORIGIN_CLOSED"}


CLOSED_DONOR = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 200 } }]
  - id: F2
    lat: 40.0
    lon: -100.8
    zones: [{ id: Z2, kind: rack, capacity: { slots: 200 } }]
lanes:
  - { id: L12, from: F1, to: F2, km: 70, minutes: 70, cost_fixed: 90 }
demand_rates:
  - { facility: F1, group: general, per_day: 1 }
  - { facility: F2, group: general, per_day: 10 }
lots:
  - { id: LOT-A, zone: Z1, group: general, quantity: 60, size: { slots: 30 } }
  - { id: LOT-B, zone: Z1, group: general, quantity: 40, size: { slots: 20 } }
"""


def test_a_closed_donor_neither_proposes_nor_books_a_transfer(tmp_path: Path) -> None:
    """Rebalancing is automatic routing like any other (§7.7), so it obeys the
    same rule: F1 is over target and would donate, but nothing leaves a shut
    facility. It is not proposed — and a transfer fed in anyway is not booked."""
    from nodal.allocate.rebalance import generate_rebalancing_transfers

    with _world(tmp_path, CLOSED_DONOR, "donor") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        open_proposals = generate_rebalancing_transfers(state, CORE, now, id_prefix="TRF")
        assert [t.origin_facility_id for t in open_proposals] == ["F1"]

        _disrupt(
            store,
            now,
            Disruption(
                id="DIS-F1",
                kind=DisruptionKind.FACILITY_CLOSED,
                target_id="F1",
                from_ts=now,
                until_ts=now + timedelta(days=5),
            ),
        )
        shut = load_state(store)
        assert generate_rebalancing_transfers(shut, CORE, now, id_prefix="TRF") == []

        store.append(
            [EventDraft(ts=now, payload=ev.TransferOrdered(shipment=t)) for t in open_proposals]
        )
        forced = load_state(store)
        result = solve_batch(forced, ["TRF-1"], CORE, now, batch_id="B-DONOR")
    assert result.assignments["TRF-1"] is None
    assert result.records["TRF-1"].chosen is None


# Both deliveries can hold at their own near facility (cheap) or at the far one,
# and the near choice makes each cross-dock through FAC-MID. FAC-MID has room for
# exactly one of them, so the batch has to move a HOLD to resolve dock contention.
CONTENDED_TRANSIT = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-H1
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-H1, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-H2
    lat: 40.0
    lon: -100.2
    zones: [{ id: ZON-H2, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-MID
    lat: 40.0
    lon: -104.0
    zones: [{ id: ZON-MID, kind: cross-dock, capacity: { slots: 6 } }]
  - id: FAC-FAR
    lat: 40.0
    lon: -104.1
    zones: [{ id: ZON-FAR, kind: rack, capacity: { slots: 100 } }]
lanes:
  - { id: LANE-H12M, from: FAC-H1, to: FAC-MID, km: 340, minutes: 300, cost_fixed: 60 }
  - { id: LANE-H22M, from: FAC-H2, to: FAC-MID, km: 320, minutes: 290, cost_fixed: 60 }
shipments:
  - id: SHP-P
    origin_label: "Gate P"
    origin_lat: 40.0
    origin_lon: -99.98
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer P", lat: 40.0, lon: -104.02 }
    hold_days: 2
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
  - id: SHP-Q
    origin_label: "Gate Q"
    origin_lat: 40.0
    origin_lon: -100.22
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer Q", lat: 40.0, lon: -104.04 }
    hold_days: 2
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: Y, group: general, quantity: 5, size: { slots: 5 } }]
"""

CONTENDED_IDS = ["SHP-P", "SHP-Q"]


def _single_totals(
    store: EventStore, now: datetime, ids: list[str] | None = None
) -> dict[str, dict[str, float]]:
    """Each shipment's scorer total per holding facility, decided alone."""
    state = load_state(store)
    totals: dict[str, dict[str, float]] = {}
    for sid in ids or CONTENDED_IDS:
        record = allocate(state, sid, SEPARABLE_ONLY, now)
        totals[sid] = {c.facility_id: c.total for c in record.scored}
    return totals


def _mid_load(store: EventStore, now: datetime, sid: str, facility_id: str) -> dict[date, int]:
    """Slots that holding `sid` at `facility_id` books at ZON-MID, per day. Reads
    the survey's own stops, so an ordinary allocation is measured exactly like a
    delivery — which is the whole point of routing them through one router."""
    from nodal.allocate.engine import survey_candidates

    state = load_state(store)
    surveys = survey_candidates(state, state.shipments[sid], SEPARABLE_ONLY, now)
    survey = next(s for s in surveys if s.facility_id == facility_id)
    load: dict[date, int] = {}
    for stop in survey.staging_stops:
        if stop.zone_id != "ZON-MID":
            continue
        for day in buckets_between(stop.arrive, stop.depart):
            load[day] = load.get(day, 0) + state.shipments[sid].size.demand("slots")
    return load


def _stages_through_mid(store: EventStore, now: datetime, sid: str, facility_id: str) -> bool:
    """Does holding `sid` at `facility_id` book staging at FAC-MID?"""
    return bool(_mid_load(store, now, sid, facility_id))


def test_batch_resolves_transit_contention_and_matches_brute_force(tmp_path: Path) -> None:
    """Batch parity has to survive staging rows. Each candidate hold implies one
    deterministic path, so its transit bookings are per-pair constants and enter
    the model as more rows on capacity it already shares. The optimum must be the
    brute-force argmin over the pairs, and every per-candidate score must be
    bit-identical to what the single-shipment scorer produces."""
    with _world(tmp_path, CONTENDED_TRANSIT) as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        totals = _single_totals(store, now)
        options = {sid: sorted(totals[sid]) for sid in CONTENDED_IDS}
        assert all(len(options[sid]) >= 2 for sid in CONTENDED_IDS)

        # Brute force over every combination, ruling out the ones that overbook
        # the shared cross-dock. Both shipments are 5 slots against a 6-slot zone,
        # so at most one of them may pass through FAC-MID.
        best: tuple[float, tuple[str, str]] | None = None
        for first in options["SHP-P"]:
            for second in options["SHP-Q"]:
                through = sum(
                    _stages_through_mid(store, now, sid, facility_id)
                    for sid, facility_id in (("SHP-P", first), ("SHP-Q", second))
                )
                if through > 1:
                    continue
                cost = totals["SHP-P"][first] + totals["SHP-Q"][second]
                pair = (first, second)
                if best is None or (cost, pair) < best:
                    best = (cost, pair)
        assert best is not None
        assert (
            sum(
                _stages_through_mid(store, now, sid, facility_id)
                for sid, facility_id in zip(CONTENDED_IDS, best[1], strict=True)
            )
            == 1
        ), "the world does not actually contend for the transit zone"

        batch = solve_batch(state, CONTENDED_IDS, SEPARABLE_ONLY, now, batch_id="B-TRANSIT")
        chosen = tuple(batch.assignments[sid][0] for sid in CONTENDED_IDS)  # type: ignore[index]
        assert chosen == best[1]
        assert batch.meta.objective_scaled / OBJECTIVE_SCALE == pytest.approx(best[0], abs=2e-4)

        # batch == scorer, per candidate, bit for bit.
        for sid in CONTENDED_IDS:
            for candidate in batch.records[sid].scored:
                if candidate.components:  # top-K detail kept
                    assert candidate.total == totals[sid][candidate.facility_id]

        # And the winning plan really books the contended zone once, not twice.
        commit_batch(store, batch)
        after = load_state(store)
        for day in buckets_between(now, now + timedelta(days=4)):
            assert after.occupancy("ZON-MID", day).slots <= 6


def test_batch_and_single_agree_when_the_transit_zone_is_roomy(tmp_path: Path) -> None:
    """With the contention removed the batch argmin is each shipment's own argmin
    again — the staging rows must not perturb an uncontended solve."""
    roomy = CONTENDED_TRANSIT.replace("capacity: { slots: 6 }", "capacity: { slots: 100 }")
    with _world(tmp_path, roomy, "roomy") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        batch = solve_batch(state, CONTENDED_IDS, SEPARABLE_ONLY, now, batch_id="B-ROOMY")
        expected = 0.0
        for sid in CONTENDED_IDS:
            single = allocate(state, sid, SEPARABLE_ONLY, now)
            assert single.chosen is not None
            assert batch.assignments[sid] == (single.chosen.facility_id, single.chosen.zone_id)
            expected += single.scored[0].total
        assert batch.meta.objective_scaled / OBJECTIVE_SCALE == pytest.approx(expected, abs=2e-4)


def test_staging_events_are_plain_reservations(tmp_path: Path) -> None:
    """Nothing bespoke in the log: the staging bookings are the same event shape
    the hold uses, which is what keeps replay and what-if consistent."""
    with _world(tmp_path, TWO_WAYS_OUT, "events") as store:
        record = allocate(load_state(store), "SHP-DEL", CORE)
        assert record.chosen is not None
        envelopes = commit(store, record)
    placed = [e.payload for e in envelopes if isinstance(e.payload, ev.ReservationPlaced)]
    assert len(placed) == 3  # hold + two staging dwells
    assert {p.reservation.zone_id for p in placed} == {"ZON-HOLD", "ZON-M1", "ZON-EXIT"}
    assert all(p.reservation.holder == "SHP-DEL" for p in placed)


def test_the_pre_extension_record_shape_still_parses(tmp_path: Path) -> None:
    """A stored decision record written before stops existed must still validate:
    `stops` and `staging` are additive, so old records read back unchanged."""
    with _world(tmp_path, TWO_WAYS_OUT, "old-record") as store:
        record = allocate(load_state(store), "SHP-DEL", CORE)
    dumped = json.loads(record.model_dump_json())
    chosen = dumped["chosen"]
    chosen.pop("staging")
    chosen["itinerary"].pop("stops")
    revived = DecisionRecord.model_validate(dumped)
    assert revived.chosen is not None
    assert revived.chosen.staging == []
    assert revived.chosen.itinerary is not None
    assert revived.chosen.itinerary.stops == []


def test_sqlite_payloads_carry_the_stops(tmp_path: Path) -> None:
    """The stops travel in the event, not just in memory."""
    with _world(tmp_path, TWO_WAYS_OUT, "payload") as store:
        record = allocate(load_state(store), "SHP-DEL", CORE)
        commit(store, record)
    connection = sqlite3.connect(str(tmp_path / "payload.sqlite3"))
    try:
        rows = connection.execute("SELECT payload FROM events").fetchall()
    finally:
        connection.close()
    decided = [json.loads(text) for (text,) in rows if "assignment" in json.loads(text)]
    assert len(decided) == 1
    stops = decided[0]["assignment"]["stops"]
    assert [s["facility_id"] for s in stops] == ["FAC-HOLD", "FAC-M1", "FAC-EXIT"]
    assert all(datetime.fromisoformat(s["arrive"]).tzinfo is not None for s in stops)
    assert datetime.fromisoformat(stops[0]["arrive"]) < datetime(2027, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# One router for every journey (§6, §7.9): an ORDINARY allocation's intermediate
# hops are stops too. They used to be dimensionless points on a lane — no state
# check, no dwell, no booking, and `stops=[]` on the assignment, so a closure
# could not even be noticed afterwards. Everything below is that hole.
# ---------------------------------------------------------------------------

# FAC-A -> FAC-B -> FAC-C, and only FAC-C has a rack. Shut FAC-B and the only way
# to the rack passes through a closed building.
ORDINARY_THROUGH = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-A
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-A, kind: yard, capacity: { slots: 100 } }]
  - id: FAC-B
    lat: 40.0
    lon: -102.0
    zones: [{ id: ZON-B, kind: cross-dock, capacity: { slots: 100 } }]
  - id: FAC-C
    lat: 40.0
    lon: -104.0
    zones: [{ id: ZON-C, kind: rack, capacity: { slots: 100 } }]
lanes:
  - { id: LANE-AB, from: FAC-A, to: FAC-B, km: 170, minutes: 200, cost_fixed: 100 }
  - { id: LANE-BC, from: FAC-B, to: FAC-C, km: 170, minutes: 200, cost_fixed: 100 }
lots:
  - { id: LOT-1, zone: ZON-A, group: general, quantity: 60, size: { slots: 60 } }
shipments:
  - id: SHP-HOP
    origin: FAC-A
    ready: 2026-09-01T06:00:00+00:00
    requirements: { zone_kinds: [rack], dwell_days: 2 }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""

MIDDLE_SHUT = """
disruptions:
  - id: DIS-B
    kind: facility_closed
    target: FAC-B
    from: 2026-09-01T00:00:00+00:00
    until: 2026-09-30T00:00:00+00:00
"""


def test_an_ordinary_multi_hop_route_records_and_books_its_transit_stop(
    tmp_path: Path,
) -> None:
    """The undisrupted baseline: FAC-A -> FAC-B -> FAC-C is a real journey with a
    real dwell at FAC-B, and that dwell takes space out of ZON-B like any other."""
    with _world(tmp_path, ORDINARY_THROUGH, "ordinary-hop") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        record = allocate(state, "SHP-HOP", CORE, now)
        assert record.chosen is not None
        assert record.chosen.facility_id == "FAC-C"
        assert [(s.facility_id, s.role.value) for s in record.chosen.stops] == [
            ("FAC-B", "transit"),
            ("FAC-C", "hold"),
        ]
        transit = record.chosen.stops[0]
        assert transit.zone_id == "ZON-B"
        # The dwell is carved out of the leg that leaves FAC-B, never added on.
        assert transit.arrive == now + timedelta(minutes=200)
        assert transit.depart - transit.arrive == timedelta(minutes=45)
        # Both lanes are scheduled, each at the moment it actually rolls.
        assert [leg.lane_id for leg in record.chosen.legs] == ["LANE-AB", "LANE-BC"]
        assert record.chosen.legs[1].depart == transit.arrive
        assert [b.zone_id for b in record.chosen.staging] == ["ZON-B"]

        before = state.occupancy("ZON-B", transit.arrive.date()).demand("slots")
        commit(store, record)
        after = load_state(store)
        assert after.occupancy("ZON-B", transit.arrive.date()).demand("slots") == before + 5
        assigned = after.shipments["SHP-HOP"].assigned
        assert assigned is not None
        assert [s.facility_id for s in assigned.stops] == ["FAC-B", "FAC-C"]
        assert [leg.lane_id for leg in assigned.legs] == ["LANE-AB", "LANE-BC"]


def test_an_ordinary_allocation_is_never_routed_through_a_closed_facility(
    tmp_path: Path,
) -> None:
    """The defect: an ordinary shipment's intermediate hops were checked for lane
    blocks and nothing else, so FAC-C stayed the cheapest candidate and the plan
    drove the goods straight through a shut FAC-B. Single and batch both refuse."""
    with _world(tmp_path, ORDINARY_THROUGH + MIDDLE_SHUT, "ordinary-shut") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        record = allocate(state, "SHP-HOP", CORE, now)
        assert record.chosen is None, "no automatic routing THROUGH a closed facility"
        rejected = {
            r.facility_id: [v.constraint_id for v in r.facility_verdicts] for r in record.rejected
        }
        assert rejected["FAC-B"] == ["FACILITY_CLOSED"]
        assert rejected["FAC-C"] == ["TRANSIT_CLOSED"]
        verdict = next(
            v for r in record.rejected if r.facility_id == "FAC-C" for v in r.facility_verdicts
        )
        assert verdict.data["facility"] == "FAC-B"
        assert verdict.data["disruption"] == "DIS-B"
        assert "FAC-B" in render_reject(verdict)

        batch = solve_batch(state, ["SHP-HOP"], CORE, now, batch_id="B-THROUGH")
        assert batch.assignments["SHP-HOP"] is None


def test_a_rebalancing_transfer_is_not_routed_through_a_closed_hub(tmp_path: Path) -> None:
    """§7.7 transfers are ordinary shipments, so the same rule binds them: a move
    whose only path crosses a shut hub is refused, not quietly planned."""
    # FAC-A holds far more than its 14-day cover and FAC-C is starved, so §7.7
    # wants to move whole lots across — and the only lane path crosses FAC-B.
    world = ORDINARY_THROUGH.replace(
        "lots:\n  - { id: LOT-1, zone: ZON-A, group: general, quantity: 60, size: { slots: 60 } }",
        "demand_rates:\n"
        "  - { facility: FAC-A, group: general, per_day: 1 }\n"
        "  - { facility: FAC-C, group: general, per_day: 20 }\n"
        "lots:\n"
        + "".join(
            f"  - {{ id: LOT-{n}, zone: ZON-A, group: general, quantity: 5,"
            f" size: {{ slots: 5 }} }}\n"
            for n in range(1, 13)
        ).rstrip("\n"),
    )
    with _world(tmp_path, world + MIDDLE_SHUT, "transfer-shut") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        transfers = generate_rebalancing_transfers(state, CORE, now, id_prefix="TRF")
        assert transfers, "FAC-A is over target and should want to donate"
        overlay = state.model_copy(deep=True)
        for transfer in transfers:
            overlay.shipments[transfer.id] = transfer
        ids = sorted(t.id for t in transfers)
        batch = solve_batch(overlay, ids, CORE, now, batch_id="B-TRF")
        for sid in ids:
            assert batch.assignments[sid] is None
            codes = {
                r.facility_id: [v.constraint_id for v in r.facility_verdicts]
                for r in batch.records[sid].rejected
            }
            assert codes["FAC-C"] == ["TRANSIT_CLOSED"]


# A hub that opens for one hour a week. A dwell landing outside it must WAIT for
# the next opening (§6) rather than be cross-docked through a shut building.
CALENDAR_HUB = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-HOLD
    lat: 40.0
    lon: -100.0
    tz: UTC
    zones: [{ id: ZON-HOLD, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-M1
    lat: 40.0
    lon: -103.0
    tz: UTC
    calendar:
      week:
        "0": ["08:00-09:00"]
    zones: [{ id: ZON-M1, kind: cross-dock, capacity: { slots: 20 } }]
  - id: FAC-EXIT
    lat: 40.0
    lon: -106.0
    tz: UTC
    zones: [{ id: ZON-EXIT, kind: cross-dock, capacity: { slots: 20 } }]
lanes:
  - { id: LANE-H2M1, from: FAC-HOLD, to: FAC-M1, km: 260, minutes: 1500, cost_fixed: 100 }
  - { id: LANE-M12E, from: FAC-M1, to: FAC-EXIT, km: 260, minutes: 1500, cost_fixed: 100 }
shipments:
  - id: SHP-DEL
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -99.98
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -106.02 }
    hold_days: 3
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_a_transit_dwell_waits_for_the_operating_window(tmp_path: Path) -> None:
    """A calendar-closed facility is a WAIT, not a free pass: the dwell shifts to
    the next opening, and every leg and arrival after it moves with it. The map
    drew FAC-M1 as CLOSED while a plan quietly cross-docked through it."""
    with _world(tmp_path, CALENDAR_HUB, "calendar") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        record = allocate(state, "SHP-DEL", CORE, now)
        assert record.chosen is not None and record.chosen.itinerary is not None
        stops = {s.role.value: s for s in record.chosen.stops}
        transit = stops["transit"]
        assert transit.facility_id == "FAC-M1"
        assert state.facility_open("FAC-M1", transit.arrive)
        assert state.facility_open("FAC-M1", transit.depart - timedelta(microseconds=1))
        assert transit.arrive.weekday() == 0 and transit.arrive.hour == 8
        # The unshifted arrival was a Saturday, so the wait is days, not minutes.
        assert transit.arrive > stops["hold"].depart + timedelta(minutes=1500)
        # It is real elapsed time: the last leg rolls after it and the customer
        # gets the goods that much later.
        assert stops["exit"].arrive == transit.arrive + timedelta(minutes=1500)
        assert record.chosen.itinerary.delivered_at > stops["exit"].arrive


# One journey, two dwells at the SAME cross-dock: in through FAC-M1 and back out
# through FAC-M1. ZON-M1 holds exactly one of them.
DOUBLE_DWELL = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-M1
    lat: 40.0
    lon: -100.02
    zones: [{ id: ZON-M1, kind: cross-dock, capacity: { slots: 5 } }]
  - id: FAC-HOLD
    lat: 43.0
    lon: -100.0
    zones: [{ id: ZON-HOLD, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-EXIT
    lat: 40.0
    lon: -106.0
    zones: [{ id: ZON-EXIT, kind: cross-dock, capacity: { slots: 100 } }]
lanes:
  - { id: LANE-M2H, from: FAC-M1, to: FAC-HOLD, km: 340, minutes: 300, cost_fixed: 100 }
  - { id: LANE-H2M, from: FAC-HOLD, to: FAC-M1, km: 340, minutes: 300, cost_fixed: 100 }
  - { id: LANE-M2E, from: FAC-M1, to: FAC-EXIT, km: 520, minutes: 300, cost_fixed: 100 }
shipments:
  - id: SHP-DEL
    origin_label: "Gate"
    origin_lat: 40.0
    origin_lon: -100.0
    ready: 2026-09-01T00:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -106.02 }
    hold_days: 0
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_a_journey_dwelling_twice_at_one_zone_pays_for_it_twice(tmp_path: Path) -> None:
    """The planner answered each stop against a state that knew nothing of this
    candidate's own earlier dwells, so one journey booked ZON-M1 twice over — 10
    slots into a 5-slot zone — while the batch model, which sums both dwells onto
    one capacity row, refused the same candidate outright. They must agree."""
    with _world(tmp_path, DOUBLE_DWELL, "double") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        record = allocate(state, "SHP-DEL", CORE, now)
        assert record.chosen is not None
        at_m1 = [s for s in record.chosen.stops if s.facility_id == "FAC-M1"]
        assert len(at_m1) == 1, "the second dwell must be routed around, not double-booked"
        commit(store, record)
        after = load_state(store)
        for day in buckets_between(now, now + timedelta(days=2)):
            capacity = after.effective_capacity("ZON-M1", day).get("slots")
            assert capacity is not None
            assert after.occupancy("ZON-M1", day).demand("slots") <= capacity

    with _world(tmp_path, DOUBLE_DWELL, "double-batch") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        batch = solve_batch(state, ["SHP-DEL"], CORE, now, batch_id="B-DOUBLE")
        assert batch.assignments["SHP-DEL"] == (
            record.chosen.facility_id,
            record.chosen.zone_id,
        )
        chosen = batch.records["SHP-DEL"].chosen
        assert chosen is not None
        assert [(s.facility_id, s.role.value) for s in chosen.stops] == [
            (s.facility_id, s.role.value) for s in record.chosen.stops
        ]


# ---------------------------------------------------------------------------
# Trapped cargo is never re-solved AND never released (§7.9). Two ways that
# promise was broken: the tier-2 escalation re-derived stranded shipments from
# reservation holders and dropped their bookings, and the trapped predicate
# itself fired on cargo no closure could touch, freezing it for good.
# ---------------------------------------------------------------------------

# SHP-TRAP is stranded at a shut FAC-YARD and holds all of ZON-HOLD. SHP-MOVE
# arrives from a gate next to a shut FAC-OTHER, so the only room left in the
# network is the room SHP-TRAP is holding — which is exactly the chain tier 2
# follows.
TIER_TWO_TRAP = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-YARD
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-YARD, kind: rack, capacity: { slots: 50 } }]
  - id: FAC-HOLD
    lat: 40.0
    lon: -100.5
    zones: [{ id: ZON-HOLD, kind: rack, capacity: { slots: 10 } }]
  - id: FAC-OTHER
    lat: 40.0
    lon: -108.0
    zones: [{ id: ZON-OTHER, kind: rack, capacity: { slots: 50 } }]
lanes:
  - { id: LANE-Y2H, from: FAC-YARD, to: FAC-HOLD, km: 45, minutes: 60, cost_fixed: 50 }
lots:
  - { id: LOT-1, zone: ZON-YARD, group: general, quantity: 50, size: { slots: 50 } }
shipments:
  - id: SHP-TRAP
    origin: FAC-YARD
    ready: 2026-09-01T06:00:00+00:00
    requirements: { dwell_days: 6 }
    lines: [{ sku: X, group: general, quantity: 10, size: { slots: 10 } }]
  - id: SHP-MOVE
    origin_label: "Gate"
    origin_lat: 40.0
    origin_lon: -108.02
    ready: 2026-09-01T06:00:00+00:00
    requirements: { dwell_days: 6 }
    lines: [{ sku: Y, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_tier_two_escalation_never_drags_in_trapped_cargo(tmp_path: Path) -> None:
    """The tier-1 set drops stranded shipments, but tier 2 rebuilt its members
    from reservation holders — which still name them. Escalating onto SHP-TRAP
    found it infeasible everywhere (its origin is shut), left it unassigned, and
    released the reservation that IS its physical occupancy, without so much as
    naming it in `trapped`. The expansion must exclude it and report it."""
    with _world(tmp_path, TIER_TWO_TRAP, "tier2") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        commit(store, allocate(state, "SHP-TRAP", CORE, now))
        commit(store, allocate(load_state(store), "SHP-MOVE", CORE, now))
        booked = load_state(store).shipments["SHP-TRAP"].assigned
        assert booked is not None and booked.facility_id == "FAC-HOLD"

        for facility_id, disruption_id in (("FAC-YARD", "DIS-YARD"), ("FAC-OTHER", "DIS-OTHER")):
            _disrupt(
                store,
                now,
                Disruption(
                    id=disruption_id,
                    kind=DisruptionKind.FACILITY_CLOSED,
                    target_id=facility_id,
                    from_ts=now,
                    until_ts=now + timedelta(days=30),
                ),
            )
        state = load_state(store)
        held = sorted(r.id for r in state.active_reservations_of("SHP-TRAP"))
        assert held

        result = reoptimize(store, state, "DIS-OTHER", CORE, now)
        assert "SHP-TRAP" not in result.affected
        assert "SHP-TRAP" not in result.changed
        assert "SHP-TRAP" not in result.released
        # It is named, and named with the closure holding it — not the trigger.
        assert [(e.shipment_id, e.disruption_id) for e in result.trapped] == [
            ("SHP-TRAP", "DIS-YARD")
        ]
        after = load_state(store)
        trapped = after.shipments["SHP-TRAP"]
        assert trapped.status.value == "allocated"
        assert trapped.assigned is not None
        assert trapped.assigned.facility_id == booked.facility_id
        assert sorted(r.id for r in after.active_reservations_of("SHP-TRAP")) == held


# SHP-OUT sits in FAC-YARD and is booked into FAC-FAR. FAC-ALT is the dearer
# alternative it must fall back to when FAC-FAR shuts.
SLIPPED_READY = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-YARD
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-YARD, kind: rack, capacity: { slots: 5 } }]
  - id: FAC-FAR
    lat: 40.0
    lon: -112.0
    zones: [{ id: ZON-FAR, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-ALT
    lat: 40.0
    lon: -112.4
    zones: [{ id: ZON-ALT, kind: rack, capacity: { slots: 100 } }]
lanes:
  - { id: LANE-Y2F, from: FAC-YARD, to: FAC-FAR, km: 1000, minutes: 600, cost_fixed: 100 }
  - { id: LANE-Y2A, from: FAC-YARD, to: FAC-ALT, km: 1030, minutes: 620, cost_fixed: 400 }
lots:
  - { id: LOT-1, zone: ZON-YARD, group: general, quantity: 5, size: { slots: 5 } }
shipments:
  - id: SHP-OUT
    origin: FAC-YARD
    ready: 2026-09-01T06:00:00+00:00
    requirements: { dwell_days: 2 }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_a_closure_that_is_over_before_the_goods_roll_does_not_trap_them(
    tmp_path: Path,
) -> None:
    """Trapped means the door is shut when the goods are due to LEAVE. A closure
    that ends days before that touches nothing — but the old window ran from
    `now` to the plan's recorded ETA, so a readiness slip past that ETA made
    every short closure at the origin a permanent strand."""
    with _world(tmp_path, SLIPPED_READY, "slipped") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        commit(store, allocate(state, "SHP-OUT", CORE, now))
        store.append(
            [
                EventDraft(
                    ts=now,
                    payload=ev.ShipmentReadyChanged(
                        shipment_id="SHP-OUT", new_ready=now + timedelta(days=5), reason="slip"
                    ),
                )
            ]
        )
        _disrupt(
            store,
            now,
            Disruption(
                id="DIS-YARD",
                kind=DisruptionKind.FACILITY_CLOSED,
                target_id="FAC-YARD",
                from_ts=now,
                until_ts=now + timedelta(days=1),
            ),
        )
        state = load_state(store)
        assert trapped_cargo(state, state.disruptions["DIS-YARD"], now) == []
        assert all_trapped_cargo(state, now) == []


def test_a_stale_trapped_flag_cannot_freeze_a_booking_into_a_closed_facility(
    tmp_path: Path,
) -> None:
    """The false positive is not cosmetic: trapped cargo is never re-solved, so a
    spurious flag left SHP-OUT booked INTO a facility shut for thirty days. With
    the departure window right, the re-solve moves it to FAC-ALT."""
    with _world(tmp_path, SLIPPED_READY, "frozen") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        commit(store, allocate(state, "SHP-OUT", CORE, now))
        booked = load_state(store).shipments["SHP-OUT"].assigned
        assert booked is not None and booked.facility_id == "FAC-FAR"
        store.append(
            [
                EventDraft(
                    ts=now,
                    payload=ev.ShipmentReadyChanged(
                        shipment_id="SHP-OUT", new_ready=now + timedelta(days=5), reason="slip"
                    ),
                )
            ]
        )
        for facility_id, disruption_id, days in (
            ("FAC-YARD", "DIS-YARD", 1),
            ("FAC-FAR", "DIS-FAR", 30),
        ):
            _disrupt(
                store,
                now,
                Disruption(
                    id=disruption_id,
                    kind=DisruptionKind.FACILITY_CLOSED,
                    target_id=facility_id,
                    from_ts=now,
                    until_ts=now + timedelta(days=days),
                ),
            )
        result = reoptimize(store, load_state(store), "DIS-FAR", CORE, now)
        assert result.trapped == []
        assert result.changed == ["SHP-OUT"]
        moved = load_state(store).shipments["SHP-OUT"].assigned
        assert moved is not None and moved.facility_id == "FAC-ALT"


# A delivery and an ORDINARY multi-hop shipment contending one dock. SHP-D's
# cheapest holds cross-dock through FAC-MID on the way to a customer next door to
# it; holding at FAC-H2 instead leaves through FAC-ALT and touches the dock not
# at all. SHP-R needs the vault, so FAC-FAR is its only home and FAC-MID its only
# way there. ZON-MID holds 6 slots against SHP-D's 5 and SHP-R's 2, so the dock
# takes one of them and the batch has to move a HOLD to fit both.
MIXED_TRANSIT = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-ORD
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-ORD, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-H1
    lat: 40.0
    lon: -100.2
    zones: [{ id: ZON-H1, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-H2
    lat: 40.0
    lon: -100.25
    zones: [{ id: ZON-H2, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-ALT
    lat: 40.0
    lon: -103.9
    zones: [{ id: ZON-ALT, kind: cross-dock, capacity: { slots: 100 } }]
  - id: FAC-MID
    lat: 40.0
    lon: -104.0
    zones: [{ id: ZON-MID, kind: cross-dock, capacity: { slots: 6 } }]
  - id: FAC-FAR
    lat: 40.0
    lon: -104.1
    tags: ["security:vault"]
    zones: [{ id: ZON-FAR, kind: rack, capacity: { slots: 100 } }]
lanes:
  - { id: LANE-O2M, from: FAC-ORD, to: FAC-MID, km: 340, minutes: 300, cost_fixed: 60 }
  - { id: LANE-H12M, from: FAC-H1, to: FAC-MID, km: 320, minutes: 290, cost_fixed: 60 }
  - { id: LANE-H22A, from: FAC-H2, to: FAC-ALT, km: 330, minutes: 295, cost_fixed: 90 }
  - { id: LANE-M2F, from: FAC-MID, to: FAC-FAR, km: 10, minutes: 60, cost_fixed: 200 }
shipments:
  - id: SHP-D
    origin_label: "Gate D"
    origin_lat: 40.0
    origin_lon: -100.22
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer D", lat: 40.0, lon: -104.02 }
    hold_days: 0
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
  - id: SHP-R
    origin: FAC-ORD
    ready: 2026-09-01T06:00:00+00:00
    requirements: { required_tags: ["security:vault"], dwell_days: 2 }
    lines: [{ sku: Y, group: general, quantity: 2, size: { slots: 2 } }]
"""

MIXED_IDS = ["SHP-D", "SHP-R"]


def test_batch_parity_survives_an_ordinary_multi_hop_candidate(tmp_path: Path) -> None:
    """Ordinary candidates get staging rows exactly like deliveries, so the batch
    argmin must still be the brute-force argmin over the pairs and every
    per-candidate score must still be bit-identical to the single scorer's. Before
    the unification an ordinary route booked nothing, so the solver could hand the
    dock to a delivery AND drive SHP-R through it on the same day."""
    with _world(tmp_path, MIXED_TRANSIT, "mixed") as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        totals = _single_totals(store, now, MIXED_IDS)
        assert sorted(totals["SHP-R"]) == ["FAC-FAR"], "the vault is the only home"
        assert _mid_load(store, now, "SHP-R", "FAC-FAR"), "SHP-R must cross the dock"
        assert _mid_load(store, now, "SHP-D", "FAC-H1"), "the cheap hold crosses the dock"
        assert not _mid_load(store, now, "SHP-D", "FAC-H2"), "and the dearer one avoids it"
        assert totals["SHP-D"]["FAC-H1"] < totals["SHP-D"]["FAC-H2"]

        capacity = state.effective_capacity("ZON-MID", now.date()).get("slots")
        assert capacity == 6
        penalty = SEPARABLE_ONLY.solver.unassigned_penalty

        # Brute force over every combination, including leaving one unassigned,
        # ruling out the ones that overbook the shared cross-dock on any day.
        best: tuple[float, tuple[str | None, str | None]] | None = None
        contended = False
        for first in [*sorted(totals["SHP-D"]), None]:
            for second in [*sorted(totals["SHP-R"]), None]:
                load: dict[date, int] = {}
                for sid, facility_id in (("SHP-D", first), ("SHP-R", second)):
                    if facility_id is None:
                        continue
                    for day, slots in _mid_load(store, now, sid, facility_id).items():
                        load[day] = load.get(day, 0) + slots
                if any(slots > capacity for slots in load.values()):
                    contended = True
                    continue
                cost = penalty * sum(1 for f in (first, second) if f is None)
                cost += sum(
                    totals[sid][facility_id]
                    for sid, facility_id in (("SHP-D", first), ("SHP-R", second))
                    if facility_id is not None
                )
                pair = (first, second)
                if best is None or (cost, pair) < (best[0], best[1]):
                    best = (cost, pair)
        assert contended, "the world does not actually contend for the transit zone"
        assert best is not None

        batch = solve_batch(state, MIXED_IDS, SEPARABLE_ONLY, now, batch_id="B-MIXED")
        chosen = tuple(
            None if batch.assignments[sid] is None else batch.assignments[sid][0]  # type: ignore[index]
            for sid in MIXED_IDS
        )
        assert chosen == best[1]
        assert batch.meta.objective_scaled / OBJECTIVE_SCALE == pytest.approx(best[0], abs=2e-4)
        for sid in MIXED_IDS:
            for candidate in batch.records[sid].scored:
                if candidate.components:  # top-K detail kept
                    assert candidate.total == totals[sid][candidate.facility_id]

        # And the committed plan really books the dock within its capacity.
        commit_batch(store, batch)
        after = load_state(store)
        for day in buckets_between(now, now + timedelta(days=3)):
            assert after.occupancy("ZON-MID", day).demand("slots") <= 6
