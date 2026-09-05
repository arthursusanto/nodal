"""A->B delivery routing (§7.9): hold + itinerary, entry/exit choice, parity.

The load-bearing story is the one a nearest-facility policy gets wrong: the
customer's goods must be held somewhere for `hold_days` and then delivered, so a
full or infeasible nearest facility does not strand the shipment — a farther one
takes the hold and the itinerary still reaches the customer.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from nodal.allocate import ObjectiveConfig, allocate, commit
from nodal.allocate.batch import solve_batch
from nodal.allocate.records import DecisionRecord
from nodal.allocate.reopt import reoptimize
from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import (
    Destination,
    Disruption,
    DisruptionKind,
    RequirementSet,
    Shipment,
)
from nodal.domain.units import OBJECTIVE_SCALE
from nodal.events import EventStore, load_state
from nodal.events import catalog as ev
from nodal.events.envelope import EventDraft
from nodal.events.state import buckets_between
from nodal.network.travel import lane_blocked_at, lane_cost_cents
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


ONE_HUB = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-HUB
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-HUB, kind: rack, capacity: { slots: 100 } }]
shipments:
  - id: SHP-DEL
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -100.2
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer North", lat: 40.3, lon: -99.9 }
    hold_days: 3
    lines: [{ sku: X, group: general, quantity: 10, size: { slots: 10 } }]
"""


def test_delivery_books_a_hold_and_an_itinerary(tmp_path: Path) -> None:
    """End to end: the decision books a hold at a facility AND records the whole
    movement through to the customer."""
    with _world(tmp_path, ONE_HUB) as store:
        state = load_state(store)
        record = allocate(state, "SHP-DEL", CORE)
        assert record.chosen is not None
        chosen = record.chosen
        assert (chosen.facility_id, chosen.zone_id) == ("FAC-HUB", "ZON-HUB")

        itinerary = chosen.itinerary
        assert itinerary is not None
        assert itinerary.destination == "Customer North"
        # Hold exactly the customer's hold_days, starting on arrival.
        assert itinerary.hold.from_ts == chosen.eta
        assert itinerary.hold.until_ts == chosen.departure
        assert chosen.departure - chosen.eta == timedelta(days=3)
        # Two road legs around the hold: nothing else can reach a lane-less hub.
        assert [leg.kind for leg in itinerary.legs] == ["road", "road"]
        first, last = itinerary.legs
        assert (first.from_label, first.to_label) == ("Origin Gate", "FAC-HUB")
        assert (last.from_label, last.to_label) == ("FAC-HUB", "Customer North")
        # Legs are scheduled, and the outbound one rolls when the hold ends.
        assert first.depart == max(state.shipments["SHP-DEL"].ready_at, state.last_ts)
        assert last.depart == chosen.departure
        assert last.arrive == itinerary.delivered_at
        assert itinerary.cost_cents == first.cost_cents + last.cost_cents
        # The inbound half alone is what books the hold (and what a truck drives).
        assert len(chosen.route.legs) == 1


def test_hold_consumes_zone_capacity_over_its_window(tmp_path: Path) -> None:
    """The hold is real occupancy, not a note on a record: the reservation covers
    every bucket of the hold window and none after it."""
    with _world(tmp_path, ONE_HUB) as store:
        state = load_state(store)
        record = allocate(state, "SHP-DEL", CORE)
        assert record.chosen is not None
        commit(store, record)
        after = load_state(store)
        for day in buckets_between(record.chosen.eta, record.chosen.departure):
            assert after.occupancy("ZON-HUB", day).slots == 10
        beyond = record.chosen.departure.astimezone(UTC).date() + timedelta(days=1)
        assert after.occupancy("ZON-HUB", beyond).slots == 0


def test_zero_hold_days_delivers_straight_through(tmp_path: Path) -> None:
    """hold_days=0 is a cross-dock: a degenerate hold window, still a full
    itinerary, still checked against the arrival bucket's capacity."""
    with _world(tmp_path, ONE_HUB.replace("hold_days: 3", "hold_days: 0")) as store:
        state = load_state(store)
        record = allocate(state, "SHP-DEL", CORE)
        assert record.chosen is not None
        itinerary = record.chosen.itinerary
        assert itinerary is not None
        assert itinerary.hold.from_ts == itinerary.hold.until_ts == record.chosen.eta
        assert len(itinerary.legs) == 2
        assert itinerary.delivered_at == record.chosen.eta + timedelta(
            minutes=itinerary.legs[1].minutes
        )
        # It commits and folds like any other decision, and the degenerate hold
        # still occupies the bucket it falls in — the same convention
        # `buckets_between` uses, so the feasibility check and the booking agree.
        commit(store, record)
        after = load_state(store)
        held = after.active_reservations_of("SHP-DEL")
        assert len(held) == 1 and held[0].from_ts == held[0].until_ts
        assert after.occupancy("ZON-HUB", record.chosen.eta.astimezone(UTC).date()).slots == 10


NEAREST_FULL = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-A
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-A, kind: rack, capacity: { slots: 10 } }]
  - id: FAC-B
    lat: 40.0
    lon: -101.0
    zones: [{ id: ZON-B, kind: rack, capacity: { slots: 100 } }]
lots:
  - { id: LOT-FULL, zone: ZON-A, group: general, quantity: 10, size: { slots: 10 } }
shipments:
  - id: SHP-DEL
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -99.98 }
    hold_days: 2
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_non_nearest_facility_takes_the_hold_when_the_nearest_is_full(tmp_path: Path) -> None:
    """FAC-A is nearest to BOTH the origin and the customer, and its only zone is
    full. The delivery holds at FAC-B and still gets delivered — with FAC-A's
    rejection named."""
    with _world(tmp_path, NEAREST_FULL) as store:
        state = load_state(store)
        record = allocate(state, "SHP-DEL", CORE)
        assert record.chosen is not None
        assert record.chosen.facility_id == "FAC-B"
        rejected = {r.facility_id: r for r in record.rejected}
        assert set(rejected) == {"FAC-A"}
        assert [v.constraint_id for v in rejected["FAC-A"].facility_verdicts] == [
            "NO_ELIGIBLE_ZONE"
        ]
        assert [v.constraint_id for v in rejected["FAC-A"].zone_verdicts["ZON-A"]] == ["CAPACITY"]
        itinerary = record.chosen.itinerary
        assert itinerary is not None
        assert itinerary.hold.facility_id == "FAC-B"
        assert itinerary.legs[-1].to_label == "Customer"


ENTRY_VIA_LANE = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-GATE
    lat: 40.0
    lon: -100.0
    zones:
      - { id: ZON-GATE, kind: rack, capacity: { slots: 5 } }
      - { id: ZON-GATE-XD, kind: cross-dock, capacity: { slots: 40 } }
  - id: FAC-STORE
    lat: 40.0
    lon: -104.0
    zones: [{ id: ZON-STORE, kind: rack, capacity: { slots: 100 } }]
lanes:
  - { id: LANE-G2S, from: FAC-GATE, to: FAC-STORE, km: 350, minutes: 300, cost_fixed: 100 }
  - { id: LANE-S2G, from: FAC-STORE, to: FAC-GATE, km: 350, minutes: 300, cost_fixed: 100 }
lots:
  - { id: LOT-FULL, zone: ZON-GATE, group: general, quantity: 5, size: { slots: 5 } }
shipments:
  - id: SHP-DEL
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -99.98
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -99.96 }
    hold_days: 2
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_entry_and_exit_facilities_differ_from_the_holding_facility(tmp_path: Path) -> None:
    """Origin and customer both sit next to FAC-GATE, whose only rack zone is
    full. The hold goes to FAC-STORE, but the goods enter and leave the network
    through FAC-GATE because the lane is far cheaper than driving the whole way —
    and both of those passes book FAC-GATE's cross-dock zone for their handling.

    The cross-dock zone is why FAC-GATE can still be a stop at all: `zone_kinds`
    says where the goods must be STORED and so keeps FAC-GATE out of the hold
    race, while a handling dwell is not storage and is free to use it (§7.9)."""
    with _world(tmp_path, ENTRY_VIA_LANE) as store:
        state = load_state(store)
        record = allocate(state, "SHP-DEL", CORE)
        assert record.chosen is not None
        assert record.chosen.facility_id == "FAC-STORE"
        itinerary = record.chosen.itinerary
        assert itinerary is not None
        assert [
            (leg.kind, leg.from_label, leg.to_label, leg.lane_id) for leg in itinerary.legs
        ] == [
            ("road", "Origin Gate", "FAC-GATE", None),
            ("road", "FAC-GATE", "FAC-STORE", "LANE-G2S"),
            ("road", "FAC-STORE", "FAC-GATE", "LANE-S2G"),
            ("road", "FAC-GATE", "Customer", None),
        ]
        # The inbound half — first mile plus the lane — is what books the hold.
        assert [leg.lane_id for leg in record.chosen.route.legs] == [None, "LANE-G2S"]
        # Outbound legs are scheduled after the hold, inbound before it.
        assert itinerary.legs[1].arrive <= itinerary.hold.from_ts
        assert itinerary.legs[2].depart == itinerary.hold.until_ts
        # Three stops: enter through FAC-GATE, hold at FAC-STORE, leave through
        # FAC-GATE again — each with a real window, in travel order.
        assert [(s.facility_id, s.role.value, s.zone_id) for s in itinerary.stops] == [
            ("FAC-GATE", "entry", "ZON-GATE-XD"),
            ("FAC-STORE", "hold", "ZON-STORE"),
            ("FAC-GATE", "exit", "ZON-GATE-XD"),
        ]
        entry, hold, exit_stop = itinerary.stops
        assert entry.arrive == itinerary.legs[0].arrive
        assert entry.depart > entry.arrive  # a window, not a point
        assert entry.depart <= itinerary.legs[1].arrive  # carved out of the leg
        assert (hold.arrive, hold.depart) == (itinerary.hold.from_ts, itinerary.hold.until_ts)
        assert exit_stop.arrive == itinerary.legs[2].arrive
        assert exit_stop.depart <= itinerary.legs[3].arrive
        # Journey minutes are unchanged by the stops: the dwell is carved out of
        # the handling the legs already priced, never added on top.
        assert itinerary.delivered_at == itinerary.legs[-1].arrive


OUTBOUND_DECIDES = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-DEPOT
    lat: 40.5
    lon: -100.0
    zones: [{ id: ZON-DEPOT, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-PORT
    lat: 40.1
    lon: -105.8
    zones: [{ id: ZON-PORT, kind: rack, capacity: { slots: 100 } }]
shipments:
  - id: SHP-DEL
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -100.0
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -106.0 }
    hold_days: 2
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_the_outbound_half_is_priced_into_the_choice(tmp_path: Path) -> None:
    """FAC-DEPOT is far nearer the origin; FAC-PORT is far nearer the customer and
    cheaper over the WHOLE journey. Pricing only the inbound half — the ordinary
    allocation objective — would pick FAC-DEPOT, so this pins the outbound terms."""
    with _world(tmp_path, OUTBOUND_DECIDES) as store:
        state = load_state(store)
        record = allocate(state, "SHP-DEL", CORE)
        assert record.chosen is not None
        assert record.chosen.facility_id == "FAC-PORT"
        # The inbound leg alone is the expensive one: only the total explains it.
        depot = next(c for c in record.scored if c.facility_id == "FAC-DEPOT")
        port = next(c for c in record.scored if c.facility_id == "FAC-PORT")
        assert port.route.cost_cents > depot.route.cost_cents
        assert port.components["transport_cost"].raw < depot.components["transport_cost"].raw
        assert port.components["travel_time"].raw < depot.components["travel_time"].raw
        # The priced transport is exactly the itinerary's legs, both halves.
        itinerary = record.chosen.itinerary
        assert itinerary is not None
        assert port.components["transport_cost"].raw == itinerary.cost_cents
        assert itinerary.cost_cents == sum(leg.cost_cents for leg in itinerary.legs)


# FAC-EXIT only has a cross-dock zone and the shipment must be STORED in a rack,
# so FAC-EXIT can stage the goods but never hold them: the only decision left is
# HOW the goods leave FAC-HOLD. The lane is ~6x cheaper than driving the 500 km, and the
# block window opens on 09-03 — after the plan is made, before the hold ends.
BLOCKED_OUTBOUND = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-HOLD
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-HOLD, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-EXIT
    lat: 40.0
    lon: -106.0
    zones: [{ id: ZON-EXIT, kind: cross-dock, capacity: { slots: 40 } }]
lanes:
  - { id: LANE-H2E, from: FAC-HOLD, to: FAC-EXIT, km: 500, minutes: 420, cost_fixed: 200 }
shipments:
  - id: SHP-DEL
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -106.02 }
    hold_days: 3
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
disruptions:
  - id: DIS-BLOCK
    kind: lane_blocked
    target: LANE-H2E
    from: 2026-09-03T00:00:00+00:00
    until: 2026-09-10T00:00:00+00:00
"""


def test_outbound_lane_blocks_are_evaluated_when_the_hold_ends(tmp_path: Path) -> None:
    """The outbound leg rolls when the hold ends, so that is the instant its lanes
    are checked against — not the planning instant, which is days earlier and can
    sit outside a block window the goods would drive straight into."""
    with _world(tmp_path, BLOCKED_OUTBOUND, "caught") as store:
        state = load_state(store)
        assert state.last_ts is not None
        # The block is already in the log and is NOT active when the plan is made:
        # reading it at the planning instant is exactly what missed it.
        assert not lane_blocked_at(state, "LANE-H2E", state.last_ts)
        record = allocate(state, "SHP-DEL", CORE)
    assert record.chosen is not None and record.chosen.itinerary is not None
    caught = record.chosen.itinerary
    hold_end = caught.hold.until_ts  # when the outbound leg would actually roll
    assert datetime(2026, 9, 3, tzinfo=UTC) <= hold_end < datetime(2026, 9, 10, tzinfo=UTC)
    assert caught.lane_ids == []  # driven the whole way instead
    assert caught.exit_facility_id == "FAC-HOLD"

    # Same world, same hold, a block window that opens after the goods have gone:
    # the lane must still be usable, so this is a departure-time test and not a
    # "any block anywhere excludes the lane" test.
    later = BLOCKED_OUTBOUND.replace("from: 2026-09-03", "from: 2026-09-05")
    with _world(tmp_path, later, "missed") as store:
        record = allocate(load_state(store), "SHP-DEL", CORE)
    assert record.chosen is not None and record.chosen.itinerary is not None
    assert record.chosen.itinerary.lane_ids == ["LANE-H2E"]
    assert record.chosen.itinerary.exit_facility_id == "FAC-EXIT"


def test_deadline_unreachable_through_every_facility_is_honest(tmp_path: Path) -> None:
    """A deadline the hold itself cannot meet leaves the shipment unassigned with
    a per-facility reason — never a booking that silently misses it."""
    world = ONE_HUB.replace(
        "hold_days: 3",
        "hold_days: 3\n    deadline: 2026-09-02T00:00:00+00:00",
    )
    with _world(tmp_path, world) as store:
        state = load_state(store)
        record = allocate(state, "SHP-DEL", CORE)
        assert record.chosen is None
        assert record.scored == []
        assert [
            (r.facility_id, v.constraint_id) for r in record.rejected for v in r.facility_verdicts
        ] == [("FAC-HUB", "DELIVERY_DEADLINE_UNREACHABLE")]
        data = record.rejected[0].facility_verdicts[0].data
        assert data["destination"] == "Customer North"
        assert data["delivered_at"] > data["deadline"]  # type: ignore[operator]


def test_a_deadline_the_delivery_meets_is_not_rejected(tmp_path: Path) -> None:
    """The mirror image: the deadline binds at the CUSTOMER, so a deadline after
    the delivery eta must not reject — an arrival-time check would pass here too,
    which is why the failing case above is the discriminating one."""
    world = ONE_HUB.replace(
        "hold_days: 3",
        "hold_days: 3\n    deadline: 2026-09-10T00:00:00+00:00",
    )
    with _world(tmp_path, world) as store:
        state = load_state(store)
        record = allocate(state, "SHP-DEL", CORE)
        assert record.chosen is not None
        itinerary = record.chosen.itinerary
        assert itinerary is not None
        deadline = datetime(2026, 9, 10, tzinfo=UTC)
        assert itinerary.delivered_at < deadline
        # Slack is measured to the CUSTOMER, not to the holding facility: the hold
        # plus the outbound legs are days of it, so the two readings differ.
        slack = record.scored[0].components["lateness_risk"].raw
        assert slack == (deadline - itinerary.delivered_at).total_seconds() / 60
        assert slack != (deadline - record.chosen.eta).total_seconds() / 60


# Two identically-priced two-hop paths in each direction: FAC-IN -> FAC-OUT via
# FAC-M1 (the A/C lanes) or via FAC-M2 (the B/D lanes). Only FAC-OUT has a rack
# zone so only it can hold (the rest stage in cross-dock zones), and
# both the origin and the customer sit next to FAC-IN, so every itinerary must
# pick one of the tied paths in each direction.
_TIE_LANES = [
    "  - { id: LANE-A1, from: FAC-IN, to: FAC-M1, km: 90, minutes: 90, cost_fixed: 10 }",
    "  - { id: LANE-A2, from: FAC-M1, to: FAC-OUT, km: 90, minutes: 90, cost_fixed: 10 }",
    "  - { id: LANE-B1, from: FAC-IN, to: FAC-M2, km: 90, minutes: 90, cost_fixed: 10 }",
    "  - { id: LANE-B2, from: FAC-M2, to: FAC-OUT, km: 90, minutes: 90, cost_fixed: 10 }",
    "  - { id: LANE-C1, from: FAC-OUT, to: FAC-M1, km: 90, minutes: 90, cost_fixed: 10 }",
    "  - { id: LANE-C2, from: FAC-M1, to: FAC-IN, km: 90, minutes: 90, cost_fixed: 10 }",
    "  - { id: LANE-D1, from: FAC-OUT, to: FAC-M2, km: 90, minutes: 90, cost_fixed: 10 }",
    "  - { id: LANE-D2, from: FAC-M2, to: FAC-IN, km: 90, minutes: 90, cost_fixed: 10 }",
]


def _tie_break_world(lanes: list[str]) -> str:
    return (
        """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-IN
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-IN, kind: cross-dock, capacity: { slots: 40 } }]
  - id: FAC-M1
    lat: 40.0
    lon: -101.0
    zones: [{ id: ZON-M1, kind: cross-dock, capacity: { slots: 40 } }]
  - id: FAC-M2
    lat: 40.0
    lon: -101.0
    zones: [{ id: ZON-M2, kind: cross-dock, capacity: { slots: 40 } }]
  - id: FAC-OUT
    lat: 40.0
    lon: -102.0
    zones: [{ id: ZON-OUT, kind: rack, capacity: { slots: 100 } }]
lanes:
"""
        + "\n".join(lanes)
        + """
shipments:
  - id: SHP-DEL
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -99.98
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -99.96 }
    hold_days: 2
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""
    )


def _lane_path(record: DecisionRecord) -> list[str]:
    assert record.chosen is not None and record.chosen.itinerary is not None
    return [leg.lane_id for leg in record.chosen.itinerary.legs if leg.lane_id is not None]


def test_equal_cost_lane_paths_resolve_deterministically(tmp_path: Path) -> None:
    """Tied paths must break on lane ids — the same way every run, and whatever
    order the lanes were ingested in. Both directions are tied here, so this
    covers the outbound (reverse) relaxation as well as the inbound one."""
    with _world(tmp_path, _tie_break_world(_TIE_LANES), "forward") as store:
        state = load_state(store)
        first = allocate(state, "SHP-DEL", CORE)
        again = allocate(state, "SHP-DEL", CORE)
    with _world(tmp_path, _tie_break_world(list(reversed(_TIE_LANES))), "reversed") as store:
        reversed_record = allocate(load_state(store), "SHP-DEL", CORE)
    assert first.chosen is not None and first.chosen.facility_id == "FAC-OUT"
    assert _lane_path(first) == ["LANE-A1", "LANE-A2", "LANE-C1", "LANE-C2"]
    assert _lane_path(again) == _lane_path(first)
    assert _lane_path(reversed_record) == _lane_path(first)


# A direct lane and a FASTER two-hop path priced exactly the same, in each
# direction. Only FAC-HOLD has a rack zone, so the hold is forced and the lane
# paths are the only thing in question; the gate sits next to FAC-IN and the
# customer next to FAC-EXIT, so the entry and exit are forced too.
_HOPS_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-IN
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-IN, kind: cross-dock, capacity: { slots: 40 } }]
  - id: FAC-MA
    lat: 40.0
    lon: -100.5
    zones: [{ id: ZON-MA, kind: cross-dock, capacity: { slots: 40 } }]
  - id: FAC-HOLD
    lat: 40.0
    lon: -101.0
    zones: [{ id: ZON-HOLD, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-MB
    lat: 40.0
    lon: -101.5
    zones: [{ id: ZON-MB, kind: cross-dock, capacity: { slots: 40 } }]
  - id: FAC-EXIT
    lat: 40.0
    lon: -102.0
    zones: [{ id: ZON-EXIT, kind: cross-dock, capacity: { slots: 40 } }]
lanes:
  - { id: LANE-IH, from: FAC-IN, to: FAC-HOLD, km: 220, minutes: 1000, cost_fixed: 20 }
  - { id: LANE-IA, from: FAC-IN, to: FAC-MA, km: 110, minutes: 200, cost_fixed: 10 }
  - { id: LANE-AH, from: FAC-MA, to: FAC-HOLD, km: 110, minutes: 200, cost_fixed: 10 }
  - { id: LANE-HE, from: FAC-HOLD, to: FAC-EXIT, km: 220, minutes: 1000, cost_fixed: 20 }
  - { id: LANE-HB, from: FAC-HOLD, to: FAC-MB, km: 110, minutes: 200, cost_fixed: 10 }
  - { id: LANE-BE, from: FAC-MB, to: FAC-EXIT, km: 110, minutes: 200, cost_fixed: 10 }
shipments:
  - id: SHP-DEL
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -99.98
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -102.02 }
    hold_days: 2
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
  - id: SHP-PLAIN
    origin: FAC-IN
    ready: 2026-09-01T06:00:00+00:00
    requirements: { zone_kinds: [rack], dwell_days: 2 }
    lines: [{ sku: Y, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_a_cost_tied_direct_leg_beats_a_faster_two_hop_path(tmp_path: Path) -> None:
    """Fewer lanes outranks faster in the tie-break (§7.9). An extra hop is never
    free elsewhere in the engine — `transfers` is an objective component that
    prices it, and every hop books staging space for the dwell it takes — so on an
    exact cost tie the router must not hand the objective a multi-hop path it then
    charges for. Both delivery halves and an ordinary allocation's route are the
    same relaxation, so all three are pinned here."""
    with _world(tmp_path, _HOPS_WORLD) as store:
        state = load_state(store)
        size = state.shipments["SHP-DEL"].size
        # The tie is real, and the path being turned down really is the faster one.
        assert lane_cost_cents(state.lanes["LANE-IH"], size) == sum(
            lane_cost_cents(state.lanes[lane_id], size) for lane_id in ("LANE-IA", "LANE-AH")
        )
        assert state.lanes["LANE-IH"].minutes > (
            state.lanes["LANE-IA"].minutes + state.lanes["LANE-AH"].minutes
        )
        delivery = allocate(state, "SHP-DEL", CORE)
        ordinary = allocate(state, "SHP-PLAIN", CORE)

    assert delivery.chosen is not None and delivery.chosen.facility_id == "FAC-HOLD"
    itinerary = delivery.chosen.itinerary
    assert itinerary is not None
    assert _lane_path(delivery) == ["LANE-IH", "LANE-HE"]  # in and out, both direct
    assert itinerary.legs[0].to_label == "FAC-IN"  # first mile ends at the entry
    assert itinerary.exit_facility_id == "FAC-EXIT"
    # Nothing dwells at the hops the faster path would have crossed, so nothing
    # books staging space there either.
    assert [stop.facility_id for stop in delivery.chosen.stops] == [
        "FAC-IN",
        "FAC-HOLD",
        "FAC-EXIT",
    ]

    assert ordinary.chosen is not None and ordinary.chosen.facility_id == "FAC-HOLD"
    assert [leg.lane_id for leg in ordinary.chosen.route.legs] == ["LANE-IH"]
    assert [stop.facility_id for stop in ordinary.chosen.stops] == ["FAC-HOLD"]


MIXED_BATCH = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-A
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-A, kind: rack, capacity: { slots: 200 } }]
  - id: FAC-B
    lat: 40.0
    lon: -101.0
    zones: [{ id: ZON-B, kind: rack, capacity: { slots: 200 } }]
  - id: FAC-C
    lat: 40.5
    lon: -100.5
    zones: [{ id: ZON-C, kind: rack, capacity: { slots: 200 } }]
lanes:
  - { id: LANE-AB, from: FAC-A, to: FAC-B, km: 90, minutes: 90, cost_fixed: 40 }
  - { id: LANE-BA, from: FAC-B, to: FAC-A, km: 90, minutes: 90, cost_fixed: 40 }
  - { id: LANE-AC, from: FAC-A, to: FAC-C, km: 70, minutes: 80, cost_fixed: 35 }
  - { id: LANE-CA, from: FAC-C, to: FAC-A, km: 70, minutes: 80, cost_fixed: 35 }
shipments:
  - id: SHP-ALLOC
    origin_label: "Gate One"
    origin_lat: 40.1
    origin_lon: -100.1
    ready: 2026-09-01T06:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 8, size: { slots: 8 } }]
    requirements: { dwell_days: 2 }
  - id: SHP-DEL1
    origin_label: "Gate Two"
    origin_lat: 40.0
    origin_lon: -100.05
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer West", lat: 40.0, lon: -101.05 }
    hold_days: 2
    lines: [{ sku: Y, group: general, quantity: 6, size: { slots: 6 } }]
  - id: SHP-DEL2
    origin_label: "Gate Three"
    origin_lat: 40.6
    origin_lon: -100.4
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer North", lat: 40.7, lon: -100.6 }
    hold_days: 4
    lines: [{ sku: Z, group: general, quantity: 9, size: { slots: 9 } }]
"""

MIXED_IDS = ["SHP-ALLOC", "SHP-DEL1", "SHP-DEL2"]


def test_batch_equals_scorer_with_deliveries_in_the_mix(tmp_path: Path) -> None:
    """The load-bearing equivalence (§7.4, §14) must survive delivery shipments:
    with capacity to spare, the batch argmin is each shipment's own argmin, and
    its objective is exactly the sum of their scorer totals."""
    with _world(tmp_path, MIXED_BATCH) as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        singles = {sid: allocate(state, sid, SEPARABLE_ONLY, now) for sid in MIXED_IDS}
        batch = solve_batch(state, MIXED_IDS, SEPARABLE_ONLY, now, batch_id="B1")
        for sid, single in singles.items():
            assert single.chosen is not None, sid
            assert batch.assignments[sid] == (single.chosen.facility_id, single.chosen.zone_id)
        expected = sum(single.scored[0].total for single in singles.values())
        assert batch.meta.objective_scaled / OBJECTIVE_SCALE == pytest.approx(expected, abs=2e-4)
        # A mixed batch books the deliveries as deliveries.
        assert batch.records["SHP-DEL1"].chosen is not None
        assert batch.records["SHP-DEL1"].chosen.itinerary is not None
        assert batch.records["SHP-ALLOC"].chosen is not None
        assert batch.records["SHP-ALLOC"].chosen.itinerary is None


def test_batch_argmin_matches_the_scorer_under_the_default_profile(tmp_path: Path) -> None:
    """Same equivalence with the facility-level convex terms switched on, where
    only the argmin (not a plain sum) is well defined."""
    with _world(tmp_path, MIXED_BATCH) as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        batch = solve_batch(state, MIXED_IDS, CORE, now, batch_id="B1")
        for sid in MIXED_IDS:
            single = allocate(state, sid, CORE, now)
            assert single.chosen is not None
            assert batch.assignments[sid] == (single.chosen.facility_id, single.chosen.zone_id)


def test_delivery_survives_reoptimization(tmp_path: Path) -> None:
    """§7.6 on a delivery: closing the holding facility re-solves the shipment
    through the same machinery and re-routes the whole itinerary."""
    from nodal.allocate.reopt import reoptimize
    from nodal.domain.entities import Disruption, DisruptionKind
    from nodal.events import catalog as ev
    from nodal.events.envelope import EventDraft

    with _world(tmp_path, MIXED_BATCH) as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        record = allocate(state, "SHP-DEL1", CORE, now)
        assert record.chosen is not None
        held_at = record.chosen.facility_id
        commit(store, record)
        state = load_state(store)
        store.append(
            [
                EventDraft(
                    ts=now,
                    payload=ev.DisruptionStarted(
                        disruption=Disruption(
                            id="DIS-CLOSE",
                            kind=DisruptionKind.FACILITY_CLOSED,
                            target_id=held_at,
                            from_ts=now,
                            until_ts=now + timedelta(days=30),
                        )
                    ),
                )
            ]
        )
        state = load_state(store)
        result = reoptimize(store, state, "DIS-CLOSE", CORE, now)
        assert result.tier >= 1
        assert result.affected == ["SHP-DEL1"]
        moved = result.result
        assert moved is not None
        new_record = moved.records["SHP-DEL1"]
        assert new_record.chosen is not None
        assert new_record.chosen.facility_id != held_at
        assert new_record.chosen.itinerary is not None
        assert new_record.chosen.itinerary.hold.facility_id == new_record.chosen.facility_id


# FAC-HOLD is nearest the origin and wins while its outbound lane is open;
# FAC-ALT is farther from the origin but has its own lane to the exit, so it wins
# once FAC-HOLD's only way out is ~900 km of road — by enough to clear the churn
# penalty. FAC-EXIT is cross-dock only against a rack-only requirement: it is
# only ever the way out, never a hold.
REOPT_DELIVERY = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-HOLD
    lat: 40.0
    lon: -100.0
    zones: [{ id: ZON-HOLD, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-ALT
    lat: 40.0
    lon: -100.3
    zones: [{ id: ZON-ALT, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-EXIT
    lat: 40.0
    lon: -108.0
    zones: [{ id: ZON-EXIT, kind: cross-dock, capacity: { slots: 40 } }]
lanes:
  - { id: LANE-H2E, from: FAC-HOLD, to: FAC-EXIT, km: 683, minutes: 570, cost_fixed: 200 }
  - { id: LANE-A2E, from: FAC-ALT, to: FAC-EXIT, km: 700, minutes: 590, cost_fixed: 200 }
shipments:
  - id: SHP-DEL
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -108.02 }
    hold_days: 3
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def _book_delivery(store: EventStore) -> datetime:
    """Allocate and commit SHP-DEL at FAC-HOLD; return the decision instant."""
    state = load_state(store)
    now = state.last_ts
    assert now is not None
    record = allocate(state, "SHP-DEL", CORE, now)
    assert record.chosen is not None and record.chosen.facility_id == "FAC-HOLD"
    commit(store, record)
    booked = load_state(store).shipments["SHP-DEL"].assigned
    assert booked is not None
    # The outbound half is FOLDED STATE, not merely a detail of the record: the
    # inbound half of this delivery is road-only, so `route` alone knows nothing
    # about the lane the goods will actually travel.
    assert booked.route == []
    assert booked.outbound_route == ["LANE-H2E"]
    assert booked.exit_facility_id == "FAC-EXIT"
    return now


def test_reopt_sees_a_block_on_the_outbound_half(tmp_path: Path) -> None:
    """A block on the lane a booked delivery leaves the hold by must re-optimize
    it: the shipment moves to the facility that still has a way out, its new hold
    books, and the old one is released."""
    with _world(tmp_path, REOPT_DELIVERY) as store:
        now = _book_delivery(store)
        store.append(
            [
                EventDraft(
                    ts=now,
                    payload=ev.DisruptionStarted(
                        disruption=Disruption(
                            id="DIS-BLOCK",
                            kind=DisruptionKind.LANE_BLOCKED,
                            target_id="LANE-H2E",
                            from_ts=now,
                            until_ts=now + timedelta(days=30),
                        )
                    ),
                )
            ]
        )
        result = reoptimize(store, load_state(store), "DIS-BLOCK", CORE, now)
        assert result.tier == 1
        assert result.affected == ["SHP-DEL"]
        assert result.changed == ["SHP-DEL"]
        assert result.released == []

        replayed = load_state(store)
        moved = replayed.shipments["SHP-DEL"].assigned
        assert moved is not None
        assert moved.facility_id == "FAC-ALT"
        assert moved.outbound_route == ["LANE-A2E"]
        assert moved.exit_facility_id == "FAC-EXIT"
        # The new hold books and the old one is gone — live reservations are the
        # new hold plus the staging the goods take at the exit facility, and
        # nothing at the abandoned FAC-HOLD.
        assert [r.zone_id for r in replayed.active_reservations_of("SHP-DEL")] == [
            "ZON-ALT",
            "ZON-EXIT",
        ]
        # And the re-solved record explains the whole new journey.
        assert result.result is not None
        record = result.result.records["SHP-DEL"]
        assert record.chosen is not None and record.chosen.itinerary is not None
        assert record.chosen.itinerary.hold.facility_id == "FAC-ALT"
        assert record.chosen.itinerary.lane_ids == ["LANE-A2E"]


def test_reopt_reroutes_around_a_closed_exit_facility(tmp_path: Path) -> None:
    """Closing the exit facility must change the PLAN, not just the affected set.

    This is the hole the stop model closes. The exit facility is not the hold, so
    no stay overlaps it, and the old model both planned last miles out of a shut
    facility and — because the hold's (facility, zone) was unchanged — computed
    the re-solve and threw it away. Now the exit dwell is a window the closure
    overlaps, so the router refuses that path and the re-solve is committed even
    though the shipment never moves.
    """
    with _world(tmp_path, REOPT_DELIVERY) as store:
        now = _book_delivery(store)
        store.append(
            [
                EventDraft(
                    ts=now,
                    payload=ev.DisruptionStarted(
                        disruption=Disruption(
                            id="DIS-CLOSE",
                            kind=DisruptionKind.FACILITY_CLOSED,
                            target_id="FAC-EXIT",
                            from_ts=now,
                            until_ts=now + timedelta(days=30),
                        )
                    ),
                )
            ]
        )
        result = reoptimize(store, load_state(store), "DIS-CLOSE", CORE, now)
        assert result.tier == 1
        assert result.affected == ["SHP-DEL"]
        assert result.changed == ["SHP-DEL"]  # the route is stale even if the hold is not
        assert result.envelopes  # ...and the superseding decision really landed
        assert result.trapped == []  # nothing was physically at FAC-EXIT yet

        replayed = load_state(store)
        moved = replayed.shipments["SHP-DEL"].assigned
        assert moved is not None
        assert moved.facility_id == "FAC-HOLD"  # the hold itself never had to move
        assert moved.outbound_route == []  # the lane into the closed exit is gone
        assert moved.exit_facility_id == "FAC-HOLD"  # driven the whole way instead
        # Nothing is booked at the closed facility any more.
        assert [r.zone_id for r in replayed.active_reservations_of("SHP-DEL")] == ["ZON-HOLD"]


def test_a_closed_exit_facility_is_rejected_at_planning_time(tmp_path: Path) -> None:
    """The same closure, seen by the PLANNER: a candidate whose only way out
    passes through a shut facility is never scored with that path. FAC-EXIT is
    the sole lane destination here, so every candidate falls back to road."""
    world = (
        REOPT_DELIVERY
        + """
disruptions:
  - id: DIS-CLOSE
    kind: facility_closed
    target: FAC-EXIT
    from: 2026-09-01T00:00:00+00:00
    until: 2026-10-01T00:00:00+00:00
"""
    )
    with _world(tmp_path, world, "closed-exit") as store:
        record = allocate(load_state(store), "SHP-DEL", CORE)
    assert record.chosen is not None and record.chosen.itinerary is not None
    itinerary = record.chosen.itinerary
    assert itinerary.lane_ids == []
    assert itinerary.exit_facility_id == record.chosen.facility_id
    assert all(stop.facility_id != "FAC-EXIT" for stop in itinerary.stops)


def test_a_closed_hold_facility_still_rejects_the_whole_candidate(tmp_path: Path) -> None:
    """A closure that opens exactly when the hold ends leaves the stay window
    untouched but catches the truck being loaded — so the candidate is rejected,
    named by the ordinary stay reason at the facility that is actually shut."""
    # Undisrupted, the hold runs 09-01 07:05 -> 09-04 07:05 and the last-mile
    # truck loads for 45 minutes after that. This closure opens INSIDE the
    # loading window and strictly after the reservation ends.
    world = (
        ONE_HUB
        + """
disruptions:
  - id: DIS-SHUT
    kind: facility_closed
    target: FAC-HUB
    from: 2026-09-04T07:20:00+00:00
    until: 2026-10-01T00:00:00+00:00
"""
    )
    with _world(tmp_path, world, "shut-on-departure") as store:
        state = load_state(store)
        record = allocate(state, "SHP-DEL", CORE)
    assert record.chosen is None
    assert [
        (r.facility_id, v.constraint_id) for r in record.rejected for v in r.facility_verdicts
    ] == [("FAC-HUB", "FACILITY_CLOSED")]
    # The discriminating part: the reservation window itself is entirely clear of
    # the closure, so a stay-only check would have booked this and then watched
    # the delivery leave a shut facility.
    clean = _world(tmp_path, ONE_HUB, "shut-baseline")
    with clean as store:
        baseline = allocate(load_state(store), "SHP-DEL", CORE)
    assert baseline.chosen is not None
    assert baseline.chosen.departure < datetime(2026, 9, 4, 7, 20, tzinfo=UTC)


def test_hold_days_is_bounded_at_the_domain(tmp_path: Path) -> None:
    """A hold materializes one capacity bucket per day at EVERY candidate
    facility, so an absurd hold is a resource exhaustion, not a plan. The bound
    belongs to the entity, where every ingestion path — world file, replayed
    event, API command — has to pass through it."""
    with pytest.raises(ValidationError):
        Shipment(
            id="SHP-DEL",
            requirements=RequirementSet(size=CapacityVector(slots=1)),
            ready_at=datetime(2026, 9, 1, tzinfo=UTC),
            destination=Destination(label="Customer", lat=40.0, lon=-99.9),
            hold_days=366,
        )
    world = tmp_path / "huge.yaml"
    world.write_text(ONE_HUB.replace("hold_days: 3", "hold_days: 100000"), encoding="utf-8")
    with EventStore(tmp_path / "huge.sqlite3") as store, pytest.raises(ValidationError):
        load_world(world, store)


def _strip_new_fields(db: Path) -> None:
    """Rewrite the log as a pre-extension producer would have written it: no
    `destination`, `hold_days`, or lane `path` keys anywhere in the payloads."""
    connection = sqlite3.connect(str(db))
    try:
        rows = connection.execute("SELECT seq, payload FROM events").fetchall()
        stripped = 0
        for seq, payload_text in rows:
            payload = json.loads(payload_text)
            shipment = payload.get("shipment")
            if isinstance(shipment, dict):
                for key in ("destination", "hold_days"):
                    stripped += shipment.pop(key, None) is not None
            lane = payload.get("lane")
            if isinstance(lane, dict):
                stripped += lane.pop("path", "absent") != "absent"
            connection.execute(
                "UPDATE events SET payload = ? WHERE seq = ?",
                (json.dumps(payload), seq),
            )
        assert stripped > 0, "nothing stripped: the back-compat test proves nothing"
        connection.commit()
    finally:
        connection.close()


def test_pre_extension_log_folds_and_solves_identically(tmp_path: Path) -> None:
    """Old events carry none of the new keys. They must fold to the same state and
    produce the same decision as a log written after the extension."""
    fixtures = Path(__file__).parent / "fixtures" / "world_small.yaml"
    with EventStore(tmp_path / "current.sqlite3") as store:
        load_world(fixtures, store)
        current_state = load_state(store)
        current = allocate(current_state, "SHP-1", CORE)

    old_db = tmp_path / "old.sqlite3"
    with EventStore(old_db) as store:
        load_world(fixtures, store)
    _strip_new_fields(old_db)
    with EventStore(old_db) as store:
        old_state = load_state(store)
        old = allocate(old_state, "SHP-1", CORE)

    assert old_state.model_dump_json() == current_state.model_dump_json()
    assert old.model_dump_json() == current.model_dump_json()
    assert old_state.shipments["SHP-1"].destination is None
    assert old_state.shipments["SHP-1"].hold_days == 0


def test_pre_extension_assignments_still_fold(tmp_path: Path) -> None:
    """`AllocationDecided` events written before the assignment recorded the
    outbound half, the stops and the per-leg schedule carry none of those keys.
    They must fold to a valid assignment — that detail simply unknown — rather
    than failing validation."""
    with _world(tmp_path, REOPT_DELIVERY, "old") as store:
        _book_delivery(store)
    db = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(str(db))
    try:
        stripped = 0
        for seq, payload_text in connection.execute("SELECT seq, payload FROM events").fetchall():
            payload = json.loads(payload_text)
            assignment = payload.get("assignment")
            if not isinstance(assignment, dict):
                continue
            for key in ("outbound_route", "exit_facility_id", "stops", "legs"):
                stripped += assignment.pop(key, "absent") != "absent"
            connection.execute(
                "UPDATE events SET payload = ? WHERE seq = ?", (json.dumps(payload), seq)
            )
        assert stripped == 4, "nothing stripped: the back-compat test proves nothing"
        connection.commit()
    finally:
        connection.close()
    with EventStore(db) as store:
        folded = load_state(store).shipments["SHP-DEL"].assigned
    assert folded is not None
    assert folded.facility_id == "FAC-HOLD"
    assert folded.outbound_route == []
    assert folded.exit_facility_id is None
    assert folded.stops == []
    assert folded.legs == []


def test_a_pre_extension_assignment_still_matches_its_exit_closure(tmp_path: Path) -> None:
    """The affected set reads the folded stops. An old assignment has none, so it
    must fall back to what it does carry — the hold window and the one instant
    the exit was ever planned against — instead of quietly matching nothing."""
    from nodal.allocate.reopt import affected_shipments
    from nodal.domain.entities import Assignment

    with _world(tmp_path, REOPT_DELIVERY, "legacy") as store:
        now = _book_delivery(store)
        state = load_state(store)
    booked = state.shipments["SHP-DEL"].assigned
    assert booked is not None and booked.stops
    old = Assignment(**{**booked.model_dump(), "stops": [], "legs": []})
    state.shipments["SHP-DEL"] = state.shipments["SHP-DEL"].model_copy(update={"assigned": old})
    closure = Disruption(
        id="DIS-CLOSE",
        kind=DisruptionKind.FACILITY_CLOSED,
        target_id="FAC-EXIT",
        from_ts=now,
        until_ts=now + timedelta(days=30),
    )
    state.disruptions["DIS-CLOSE"] = closure
    state._index_disruption(closure)
    assert affected_shipments(state, closure, now) == ["SHP-DEL"]
