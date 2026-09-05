"""The showcase demo world (`scripts/make_global_demo.py`).

Two things are worth a test here. First, the build is offline: it reads every
Google-derived fact from the committed snapshot file, so it runs with no maps
key and no network — proved below by unsetting the key and asserting that
`server.maps` is never even imported. Second, the world it produces has the
properties the browser demo makes claims about: routing that is demonstrably
not nearest-facility, multi-leg delivery itineraries that book staging capacity
at every facility they cross, all three transport modes, a live hub closure that
re-routed one delivery around itself and stranded the cargo standing in its
yard, and a queue whose plan is DRAFTED and not booked — so the UI opens in
plan review with proposed routes on the map (§7.5).
"""

import hashlib
import importlib.util
import json
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import ModuleType

import pytest

from nodal.allocate import ObjectiveConfig
from nodal.allocate.batch import drafted_assignments, solve_batch
from nodal.allocate.records import DecisionRecord
from nodal.allocate.reopt import affected_shipments, reoptimize, trapped_cargo
from nodal.domain.entities import Disruption, DisruptionKind, ShipmentStatus, StopRole
from nodal.events import EventStore, load_state
from nodal.events import catalog as ev
from nodal.events.envelope import EventDraft
from nodal.events.state import NetworkState
from nodal.rules.messages import render_reject
from nodal.whatif import run_whatif

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "make_global_demo.py"
SNAPSHOTS = REPO / "worlds" / "showcase_snapshots.json"

# Hub ids the browser harness clicks by name: they must survive any evolution
# of the demo world.
LEGACY_HUBS = {
    "LAX",
    "CHI",
    "MEM",
    "EWR",
    "HOU",
    "YYZ",
    "MEX",
    "GRU",
    "SCL",
    "BOG",
    "RTM",
    "HAM",
    "MAD",
    "MXP",
    "LHR",
    "WAW",
    "DXB",
    "JNB",
    "LOS",
    "SIN",
    "SHA",
    "SZX",
    "NRT",
    "BOM",
    "ICN",
    "SYD",
}


@pytest.fixture(scope="module")
def demo() -> ModuleType:
    """The build script, imported by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("make_global_demo", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_file_matches_the_world_it_feeds(demo: ModuleType) -> None:
    """Schema sanity on the committed snapshot: exactly the roads and places the
    script asks for, each with usable geometry and coordinates."""
    data = json.loads(SNAPSHOTS.read_text(encoding="utf-8"))
    assert data["version"] == demo.SNAPSHOT_VERSION

    assert set(data["roads"]) == {f"{a}>{b}" for a, b in demo.ROAD_PAIRS}
    for key, road in sorted(data["roads"].items()):
        assert road["km"] > 0, key
        assert road["minutes"] > 0, key
        assert isinstance(road["estimated"], bool), key
        path = road["path"]
        assert len(path) >= 2, key
        for lon, lat in path:
            assert -180 <= lon <= 180 and -90 <= lat <= 90, key

    assert set(data["places"]) == {key for key, _ in demo.PLACES}
    queries = dict(demo.PLACES)
    for key, place in sorted(data["places"].items()):
        assert place["query"] == queries[key]
        assert place["name"].strip(), key
        assert place["address"].strip(), key
        assert -90 <= place["lat"] <= 90 and -180 <= place["lon"] <= 180, key


def _build(demo: ModuleType, db: Path) -> str:
    """Build offline — no provider key in the environment, and the maps layer
    dropped from `sys.modules` so importing it would be visible. Returns the
    digest of the database, taken before anything can reopen the file."""
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("NODAL_GOOGLE_MAPS_KEY", raising=False)
        patch.delitem(sys.modules, "server.maps", raising=False)
        demo.build(db, demo.load_snapshots())
        assert "server.maps" not in sys.modules, "the offline build must not touch the maps layer"
    return hashlib.sha256(db.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def built(demo: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    db = tmp_path_factory.mktemp("showcase") / "demo.sqlite3"
    return db, _build(demo, db)


@pytest.fixture(scope="module")
def world(built: tuple[Path, str]) -> Iterator[tuple[EventStore, NetworkState]]:
    with EventStore(built[0]) as store:
        yield store, load_state(store)


def test_every_legacy_hub_survives(world: tuple[EventStore, NetworkState]) -> None:
    _, state = world
    assert set(state.facilities) >= LEGACY_HUBS
    assert len(state.facilities) >= 34, "the map should read as a dense global network"


def test_every_road_lane_carries_its_real_route(world: tuple[EventStore, NetworkState]) -> None:
    _, state = world
    road_lanes = [lane for lane in state.lanes.values() if lane.mode == "road"]
    assert road_lanes
    for lane in road_lanes:
        assert lane.path is not None and len(lane.path) >= 2, lane.id


def test_bookings_are_not_nearest_facility_hops(
    demo: ModuleType, world: tuple[EventStore, NetworkState]
) -> None:
    """The point of the whole scenario: for several shipments the nearest hub
    that COULD take the goods is passed over for a further one."""
    store, state = world
    moved = demo.non_nearest_bookings(state, demo.final_records(store))
    assert len(moved) >= 3
    allocations = [m for m in moved if state.shipments[m[0]].destination is None]
    assert len(allocations) >= 3
    for _, _, nearest_km, _, chosen_km in moved:
        assert chosen_km > nearest_km


def test_deliveries_route_through_intermediate_lanes(
    demo: ModuleType, world: tuple[EventStore, NetworkState]
) -> None:
    """A hold that is neither the entry nor the exit: the itinerary carries lane
    legs, not just a first and last mile. Every booked delivery must still find
    one — a pass-through with nowhere to stage makes a whole path infeasible
    (§7.9), so a thin world shows up here as a delivery that stopped routing."""
    store, _ = world
    records = demo.final_records(store)
    multi_leg = [
        record.chosen.itinerary
        for record in records.values()
        if record.chosen is not None
        and record.chosen.itinerary is not None
        and any(leg.lane_id is not None for leg in record.chosen.itinerary.legs)
    ]
    assert len(multi_leg) >= 2
    for itinerary in multi_leg:
        assert itinerary.legs[0].kind == "road"  # first mile
        assert itinerary.legs[-1].to_label == itinerary.destination  # last mile

    for row in demo.SHIPMENTS:
        if row.wave != 1 or row.dest is None:
            continue
        record = records.get(row.id)
        assert record is not None and record.chosen is not None, row.id
        assert record.chosen.itinerary is not None, row.id


def test_all_three_modes_are_booked(
    demo: ModuleType, world: tuple[EventStore, NetworkState]
) -> None:
    store, state = world
    modes = set()
    for record in demo.final_records(store).values():
        if record.chosen is None:
            continue
        legs = list(record.chosen.route.legs)
        if record.chosen.itinerary is not None:
            legs += list(record.chosen.itinerary.legs)
        modes |= {state.lanes[leg.lane_id].mode for leg in legs if leg.lane_id is not None}
    assert {"road", "sea", "air"} <= modes


def test_a_disruption_is_live_and_was_re_optimized(
    demo: ModuleType, world: tuple[EventStore, NetworkState]
) -> None:
    store, state = world
    assert [d for d in state.disruptions.values() if d.ended_at is None]
    reopted = [r for r in demo.final_records(store).values() if r.reopt_trigger is not None]
    assert reopted, "the re-optimization must be in the log, not just the disruption"


def _closure(demo: ModuleType, state: NetworkState) -> Disruption:
    closed = [
        disruption
        for disruption in state.disruptions.values()
        if disruption.kind is DisruptionKind.FACILITY_CLOSED
        and disruption.target_id == demo.CLOSED_FACILITY
        and disruption.ended_at is None
    ]
    assert len(closed) == 1, f"want exactly one live closure at {demo.CLOSED_FACILITY}"
    return closed[0]


def test_every_transit_stop_books_staging_capacity(
    world: tuple[EventStore, NetworkState],
) -> None:
    """A pass-through occupies the facility it crosses (§7.9): every transit
    stop names a zone AT that facility and holds a reservation over its dwell."""
    _, state = world
    staged = []
    for shipment_id in sorted(state.shipments):
        assignment = state.shipments[shipment_id].assigned
        if assignment is None:
            continue
        for stop in assignment.stops:
            if stop.role is not StopRole.TRANSIT:
                continue
            assert stop.zone_id is not None, (shipment_id, stop.facility_id)
            zone = state.zones[stop.zone_id]
            assert zone.facility_id == stop.facility_id
            assert [
                reservation
                for reservation in state.reservations_on_zone(stop.zone_id)
                if reservation.holder == shipment_id
                and reservation.from_ts <= stop.arrive
                and reservation.until_ts >= stop.depart
            ], f"{shipment_id} stages at {stop.facility_id} with no reservation"
            staged.append((shipment_id, zone.kind))
    assert len(staged) >= 4, f"want the demo to show staged pass-throughs, got {staged}"
    # The world gives every hub a place to stage, so no delivery has to consume
    # the storage racks the capacity story is about.
    assert all(kind == "cross-dock" for _, kind in staged), staged


def test_the_closure_re_routes_a_delivery_and_keeps_its_hold(
    demo: ModuleType, world: tuple[EventStore, NetworkState]
) -> None:
    """The plan-staleness path (§7.6/§7.9), read off the log: a delivery that
    used to run through the closed hub was superseded by a decision with the
    SAME hold and a different journey that avoids it."""
    store, state = world
    closed = _closure(demo, state)
    superseded = {
        envelope.payload.shipment_id
        for envelope in store.read()
        if isinstance(envelope.payload, ev.AllocationSuperseded)
    }
    rerouted = []
    for shipment_id, decisions in sorted(demo.decision_history(store).items()):
        if len(decisions) < 2 or state.shipments[shipment_id].destination is None:
            continue
        old, new = decisions[-2], decisions[-1]
        stops_before = {stop.facility_id for stop in old.stops}
        stops_after = {stop.facility_id for stop in new.stops}
        if closed.target_id not in stops_before or closed.target_id in stops_after:
            continue
        assert (old.facility_id, old.zone_ids) == (new.facility_id, new.zone_ids)
        assert demo.journey(old) != demo.journey(new)
        assert shipment_id in superseded
        rerouted.append(shipment_id)
    assert rerouted, f"want a delivery re-routed around {closed.target_id}"


def test_the_closure_traps_the_cargo_standing_in_the_yard(
    demo: ModuleType, world: tuple[EventStore, NetworkState]
) -> None:
    """Goods whose in-network origin IS the closed hub cannot be re-planned, so
    they come back on the re-optimization's trapped list — recomputed here
    against the world at head, which is what the UI badges. A booked one keeps
    the booking it has; an unbooked one cannot be given one."""
    _, state = world
    closed = _closure(demo, state)
    assert state.last_ts is not None
    trapped = trapped_cargo(state, closed, state.last_ts)
    assert trapped, f"want cargo trapped at {closed.target_id}"
    booked = []
    for entry in trapped:
        assert entry.facility_id == closed.target_id
        assert entry.disruption_id == closed.id
        shipment = state.shipments[entry.shipment_id]
        assert shipment.origin_facility_id == closed.target_id
        if shipment.status is ShipmentStatus.ALLOCATED:
            assert shipment.assigned is not None, "trapped cargo keeps the booking it has"
            booked.append(entry.shipment_id)
        else:
            assert shipment.status is ShipmentStatus.PLANNED
            assert shipment.assigned is None
    assert booked, f"want booked cargo standing inside {closed.target_id}"


def test_the_closure_leaves_its_own_queue_unsolvable(
    demo: ModuleType, world: tuple[EventStore, NetworkState]
) -> None:
    """The departure half of the closure rule (§7.9), which is the one the stop
    machinery could not see: a shipment still sitting in the queue at the shut
    hub must stay there. A dry-run solve at head — the same call the UI's
    OPTIMIZE makes — books nothing for it and names the origin closure."""
    _, state = world
    closed = _closure(demo, state)
    assert state.last_ts is not None
    stuck = sorted(
        sid
        for sid, shipment in state.shipments.items()
        if shipment.status is ShipmentStatus.PLANNED
        and shipment.origin_facility_id == closed.target_id
    )
    assert stuck, f"want a planned shipment sitting at {closed.target_id}"
    trapped = {entry.shipment_id for entry in trapped_cargo(state, closed, state.last_ts)}
    assert set(stuck) <= trapped, "planned cargo at a shut origin must badge as trapped"

    result = solve_batch(state, stuck, demo.CONFIG, state.last_ts, batch_id="T-DRYRUN")
    for shipment_id in stuck:
        assert result.assignments[shipment_id] is None
        record = result.records[shipment_id]
        assert record.chosen is None
        assert {v.constraint_id for r in record.rejected for v in r.facility_verdicts} == {
            "ORIGIN_CLOSED"
        }
        verdict = record.rejected[0].facility_verdicts[0]
        assert verdict.data["facility"] == closed.target_id
        assert verdict.data["until"] == closed.until_ts.isoformat()
        assert closed.target_id in render_reject(verdict)


def test_a_solvable_queue_is_left_for_the_ui(world: tuple[EventStore, NetworkState]) -> None:
    _, state = world
    planned = [s for s in state.shipments.values() if s.status.value == "planned"]
    assert len(planned) >= 8
    assert len([s for s in planned if s.destination is not None]) >= 2


def test_the_demo_opens_in_plan_review(
    demo: ModuleType, world: tuple[EventStore, NetworkState]
) -> None:
    """The world ships with routes that are PROPOSED, not booked (§7.5): the
    last event in the log is the plan for the queue, so the map opens with
    dashed routes for the whole queue — the closed hub's own shipment included,
    which the plan leaves unassigned and says why."""
    _, state = world
    plan = state.pending_plan
    assert plan is not None, "the demo must end with a drafted plan at the log head"
    assert plan.based_on_seq == state.last_seq - 1

    records = {sid: DecisionRecord.from_event_record(r) for sid, r in plan.records.items()}
    assert set(records) == {
        sid for sid, s in state.shipments.items() if s.status is ShipmentStatus.PLANNED
    }
    assert plan.assigned >= 9 and plan.unassigned == 1
    assert plan.assigned + plan.unassigned == len(records)

    unassigned = sorted(sid for sid, r in records.items() if r.chosen is None)
    assert unassigned == ["GS-0024"], unassigned
    closed = _closure(demo, state)
    stuck = records["GS-0024"]
    assert {v.constraint_id for r in stuck.rejected for v in r.facility_verdicts} == {
        "ORIGIN_CLOSED"
    }
    assert stuck.rejected[0].facility_verdicts[0].data["facility"] == closed.target_id

    # Nothing the plan proposes is booked: the queue is still planned, and no
    # proposal routes out of the shut hub.
    for shipment_id, record in records.items():
        assert state.shipments[shipment_id].assigned is None
        if record.chosen is not None:
            assert record.chosen.facility_id != closed.target_id

    deliveries = sorted(s for s in records if state.shipments[s].destination is not None)
    assert len(deliveries) >= 2, deliveries
    for shipment_id in deliveries:
        chosen = records[shipment_id].chosen
        assert chosen is not None and chosen.itinerary is not None, shipment_id


def test_the_build_is_byte_deterministic(
    demo: ModuleType, built: tuple[Path, str], tmp_path: Path
) -> None:
    """Same snapshots + same code -> the same database, byte for byte (§2)."""
    assert _build(demo, tmp_path / "demo.sqlite3") == built[1]


def _touches(state: NetworkState, shipment_id: str, facility_id: str) -> bool:
    """Does this shipment's booked plan sit at, or travel in or out of, the
    facility? Stops cover the dwells; lane ends cover the movements."""
    assignment = state.shipments[shipment_id].assigned
    if assignment is None:
        return False
    if any(stop.facility_id == facility_id for stop in assignment.stops):
        return True
    return any(
        facility_id in (state.lanes[lane_id].from_facility_id, state.lanes[lane_id].to_facility_id)
        for lane_id in [*assignment.route, *assignment.outbound_route]
    )


def test_nothing_is_booked_into_out_of_or_through_the_closed_hub(
    demo: ModuleType, world: tuple[EventStore, NetworkState]
) -> None:
    """The rule the whole closure story exists to demonstrate, asserted over the
    finished world rather than over one re-solve: after the closure, no booked
    plan — delivery or ordinary — stops at the shut hub or rolls a lane into or
    out of it. The single exception is cargo physically standing inside it, which
    keeps the booking it already had because no plan can move it (§7.9)."""
    _, state = world
    closed = _closure(demo, state)
    assert state.last_ts is not None
    trapped = {entry.shipment_id for entry in trapped_cargo(state, closed, state.last_ts)}
    offenders = sorted(
        shipment_id
        for shipment_id, shipment in state.shipments.items()
        if shipment.assigned is not None
        and shipment_id not in trapped
        and _touches(state, shipment_id, closed.target_id)
    )
    assert offenders == [], f"booked through the closed {closed.target_id}: {offenders}"
    # And the exception is real, not vacuous: something IS still booked there.
    assert [s for s in trapped if state.shipments[s].assigned is not None]


def test_closing_a_mid_route_hub_re_routes_the_ordinary_shipments_crossing_it(
    demo: ModuleType, built: tuple[Path, str], tmp_path: Path
) -> None:
    """A pin on a fixed defect, against the demo world: an ordinary allocation's
    intermediate hops were invisible — no stop, no state check — so shutting a hub
    it crossed left it routing straight through the closed building and out of the
    affected set entirely. Closing HOU must now pull every plan that crosses it
    into the re-solve and take it off HOU, with the supersedes committed."""
    db = tmp_path / "hou.sqlite3"
    db.write_bytes(built[0].read_bytes())
    with EventStore(db) as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        crossing = sorted(
            shipment_id
            for shipment_id, shipment in state.shipments.items()
            if shipment.assigned is not None and _touches(state, shipment_id, "HOU")
        )
        assert crossing, "the demo should route something through HOU"
        ordinary = [s for s in crossing if state.shipments[s].destination is None]
        assert ordinary, "and at least one of them an ORDINARY allocation"

        closure = Disruption(
            id="DIS-HOU",
            kind=DisruptionKind.FACILITY_CLOSED,
            target_id="HOU",
            from_ts=now,
            until_ts=now + timedelta(days=25),
        )
        store.append(
            [EventDraft(ts=now, payload=ev.DisruptionStarted(disruption=closure))], actor="test"
        )
        state = load_state(store)
        assert set(crossing) <= set(affected_shipments(state, closure, now))

        head = store.last_seq()
        result = reoptimize(store, state, "DIS-HOU", demo.CONFIG, now, batch_prefix="T-HOU")
        assert set(crossing) <= set(result.affected)
        assert set(crossing) <= set(result.changed)
        assert store.last_seq() > head, "the re-routes must be committed, not just computed"
        superseded = {
            envelope.payload.shipment_id
            for envelope in store.read(from_seq=head + 1)
            if isinstance(envelope.payload, ev.AllocationSuperseded)
        }
        assert set(crossing) <= superseded

        after = load_state(store)
        for shipment_id in crossing:
            assert not _touches(after, shipment_id, "HOU"), shipment_id


# -- the launch line the build prints ------------------------------------------
#
# The demo SOLVES under `scenarios/profiles/showcase.yaml`; the server must SERVE
# under it. Served under anything else the re-solve behind COMMIT PLAN lands
# somewhere the draft did not, and the plan the world ships with can never be
# booked at all — so the profile, the "ready:" line, and the READMEs are one fact
# and these tests hold them together.


def _profile_from_the_ready_line(demo: ModuleType) -> Path:
    """The profile the build TELLS the operator to serve under, read out of the
    line it prints — not out of the module's own constant, which would pass even
    if the launch line named something else."""
    source = SCRIPT.read_text(encoding="utf-8")
    named = {
        token.strip('"')
        for line in source.splitlines()
        if "--profile" in line
        for token in line.split()
        if token.strip('"').endswith(".yaml")
    }
    assert named == {"scenarios/profiles/showcase.yaml"}, named
    assert "--packs core,coldchain,chem" in source, "the packs flag must survive too"
    # The READMEs tell the same operator the same thing — pin them to it. The web
    # README only exists when web/ does (the engine-independence CI job removes it).
    readmes = [REPO / "README.md"]
    if (REPO / "web").is_dir():
        readmes.append(REPO / "web" / "smoke" / "README.md")
    for readme in readmes:
        text = readme.read_text(encoding="utf-8")
        assert "--profile scenarios/profiles/showcase.yaml" in text, readme
        assert "--packs core,coldchain,chem" in text, readme
    return REPO / named.pop()


def test_the_build_solves_under_the_profile_it_tells_you_to_serve(demo: ModuleType) -> None:
    served = ObjectiveConfig.from_yaml(_profile_from_the_ready_line(demo))
    assert served == demo.CONFIG
    assert served.name == "showcase"
    assert served != ObjectiveConfig(), "a profile that changed nothing would prove nothing"


def _pending(db: Path) -> tuple[str, int, dict[str, tuple[str, str] | None]]:
    """The batch id, the seq it read, and what it proposes — read and closed, so
    the API can open the same file."""
    with EventStore(db) as store:
        plan = load_state(store).pending_plan
        assert plan is not None
        return plan.batch_id, plan.based_on_seq, drafted_assignments(plan)


def test_the_shipped_plan_commits_under_the_showcase_profile(
    demo: ModuleType, built: tuple[Path, str], tmp_path: Path
) -> None:
    """The demo opens in plan review, so COMMIT PLAN is the first button anyone
    presses: served under the profile the build names, the re-solve reproduces the
    draft and every proposed shipment is booked exactly where the operator read
    it."""
    app_module = pytest.importorskip("server.app")
    from fastapi.testclient import TestClient

    db = tmp_path / "commit.sqlite3"
    db.write_bytes(built[0].read_bytes())
    batch_id, based_on, drafted = _pending(db)
    assert batch_id == "DEMO-PLAN"

    app = app_module.create_app(db, ObjectiveConfig.from_yaml(demo.PROFILE))
    with TestClient(app) as client:
        booked = client.post(
            "/api/commands/optimize",
            json={"commit": True, "expected_head": based_on, "batch_id": batch_id},
        )
        assert booked.status_code == 200, booked.text
        body = booked.json()
        assert body["batch_id"] == batch_id
        assert body["committed_events"] > 0

        assert client.get("/api/plan").json()["batch"] is None
        assert client.get("/api/map").json()["pending_plan"] is None
        rows = {s["id"]: s for s in client.get("/api/map").json()["shipments"]}
        for shipment_id, pair in drafted.items():
            if pair is None:
                assert rows[shipment_id]["status"] == "planned"
                continue
            assert rows[shipment_id]["status"] == "allocated", shipment_id
            assert rows[shipment_id]["destination"]["facility_id"] == pair[0], shipment_id
        # One batch id across the proposal and the booking.
        events = client.get("/api/events", params={"after_seq": 0, "limit": 2000}).json()["events"]
        assert [e["type"] for e in events if e["entity_id"] == batch_id] == [
            "PlanDrafted",
            "BatchSolved",
        ]


def test_the_default_profile_could_not_commit_the_shipped_plan(
    built: tuple[Path, str], tmp_path: Path
) -> None:
    """What makes the test above load-bearing: served under the engine defaults
    — which is what `python -m server` with no `--profile` gives you — the
    re-solve moves a delivery and the plan is refused, permanently."""
    app_module = pytest.importorskip("server.app")
    from fastapi.testclient import TestClient

    db = tmp_path / "default.sqlite3"
    db.write_bytes(built[0].read_bytes())
    batch_id, based_on, _ = _pending(db)

    app = app_module.create_app(db, ObjectiveConfig())
    with TestClient(app) as client:
        refused = client.post(
            "/api/commands/optimize",
            json={"commit": True, "expected_head": based_on, "batch_id": batch_id},
        )
        assert refused.status_code == 409, refused.text
        assert "no longer matches the plan you reviewed" in refused.json()["detail"]
        # And it is not a transient: the plan is still pending, still uncommittable.
        assert client.get("/api/plan").json()["batch"]["batch_id"] == batch_id


def test_the_what_if_baseline_agrees_with_the_plan_on_screen(
    demo: ModuleType, world: tuple[EventStore, NetworkState]
) -> None:
    """WHAT-IF's baseline column re-decides the open queue on the untouched
    world. Under the demo's own profile it must reproduce the draft the map is
    drawing — otherwise the screen contradicts itself on the delivery that made
    the profile necessary."""
    store, state = world
    plan = state.pending_plan
    assert plan is not None
    drafted = drafted_assignments(plan)
    report = run_whatif(store, demo.CONFIG, "nodal-batch", ["close BOG 3d"])
    assert report.baseline.assignments["DL-0007"] == drafted["DL-0007"]
    # The baseline column also carries the shipments already booked; on the queue
    # the plan covers it must agree with the plan, shipment for shipment.
    assert {sid: report.baseline.assignments[sid] for sid in drafted} == drafted
