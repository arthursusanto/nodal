"""API layer (§11) — thin-translation tests over a fixture world.

Run explicitly (`pytest server/tests`); the engine suite never needs this
package (CI proves the engine passes with server/ absent).
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nodal.allocate import ObjectiveConfig, allocate, commit
from nodal.allocate.reopt import reoptimize
from nodal.domain.entities import Disruption, DisruptionKind
from nodal.events import EventStore, load_state
from nodal.events import catalog as ev
from nodal.events.envelope import EventDraft
from nodal.worlds import load_world
from server import readmodels
from server.app import create_app

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db = tmp_path / "world.sqlite3"
    with EventStore(db) as store:
        load_world(FIXTURES / "world_small.yaml", store)
    app = create_app(db, ObjectiveConfig())
    with TestClient(app) as test_client:
        yield test_client


def test_map_state(client: TestClient) -> None:
    body = client.get("/api/map").json()
    assert body["empty"] is False
    assert {f["id"] for f in body["facilities"]} == {"FAC-A", "FAC-B", "FAC-C"}
    fac_a = next(f for f in body["facilities"] if f["id"] == "FAC-A")
    assert 0.0 <= fac_a["utilization"] <= 1.0
    assert {z["id"] for z in fac_a["zones"]} == {"ZON-A1", "ZON-A2"}
    dis_1 = next(d for d in body["disruptions"] if d["id"] == "DIS-1")
    assert dis_1["status"] == "upcoming"  # announced at load, active from 09-03
    assert {s["id"] for s in body["shipments"]} == {"SHP-1", "SHP-2"}


def test_map_state_at_history(client: TestClient) -> None:
    body = client.get("/api/map", params={"at": "2026-09-01T00:00:00+00:00"}).json()
    assert body["shipments"] == []  # both shipments register later that day


def test_facility_timeline_and_404(client: TestClient) -> None:
    body = client.get("/api/facilities/FAC-B", params={"days": 3}).json()
    zone_b1 = next(z for z in body["zones"] if z["id"] == "ZON-B1")
    assert len(zone_b1["buckets"]) == 3
    assert any(lot["id"] == "LOT-3" for lot in zone_b1["lots"])
    assert client.get("/api/facilities/FAC-NOPE").status_code == 404


def test_forecast_inventory(client: TestClient) -> None:
    body = client.get("/api/forecast", params={"days": 5}).json()
    fac_b = next(f for f in body["facilities"] if f["facility_id"] == "FAC-B")
    general = next(g for g in fac_b["groups"] if g["group"] == "general")
    assert general["rate"] == 12
    assert len(general["series"]) == 5
    # Stock drains by the demand rate when nothing arrives.
    assert general["series"][1]["projected"] < general["series"][0]["projected"]


def test_allocate_command_and_decision_readback(client: TestClient) -> None:
    assert client.get("/api/decisions/SHP-2").status_code == 404
    dry = client.post(
        "/api/commands/allocate", json={"shipment_id": "SHP-2", "commit": False}
    ).json()
    assert dry["committed_events"] == 0
    assert dry["record"]["chosen"]["facility_id"] == "FAC-A"  # only cold zone

    committed = client.post(
        "/api/commands/allocate", json={"shipment_id": "SHP-2", "commit": True}
    ).json()
    assert committed["committed_events"] > 0
    readback = client.get("/api/decisions/SHP-2").json()
    # The record in the log is exactly the record the command returned (§7.5).
    assert readback["record"]["chosen"] == committed["record"]["chosen"]
    assert readback["record"]["rejected"] == committed["record"]["rejected"]

    schedules = client.get("/api/schedules").json()
    assert any(m["shipment_id"] == "SHP-2" for m in schedules["movements"])


def test_optimize_command(client: TestClient) -> None:
    body = client.post("/api/commands/optimize", json={"commit": True}).json()
    assert body["batch"] is not None
    assert body["batch"]["meta"]["status"] in ("OPTIMAL", "FEASIBLE", "FLOW_OPTIMAL")
    again = client.post("/api/commands/optimize", json={"commit": True}).json()
    assert again["batch"] is None  # nothing planned remains


def test_optimize_dry_run_is_a_reviewable_plan(client: TestClient) -> None:
    """A dry run returns every shipment's decision record — the UI's plan
    preview — consistent with the assignments, books nothing, and appends the
    proposal itself as one `PlanDrafted` (§7.5)."""
    head = client.get("/api/events", params={"after_seq": 0, "limit": 1}).json()["head"]
    body = client.post("/api/commands/optimize", json={"commit": False}).json()
    batch = body["batch"]
    assert batch is not None
    assert body["committed_events"] == 0
    assert set(batch["records"]) == set(batch["assignments"])
    for shipment_id, pair in batch["assignments"].items():
        chosen = batch["records"][shipment_id]["chosen"]
        if pair is None:
            assert chosen is None
        else:
            assert [chosen["facility_id"], chosen["zone_id"]] == pair
    # Exactly one event: the draft. Nothing is booked, nothing is reserved.
    feed = client.get("/api/events", params={"after_seq": head, "limit": 100}).json()
    assert [e["type"] for e in feed["events"]] == ["PlanDrafted"]
    assert feed["events"][0]["entity_id"] == batch["batch_id"]
    assert feed["head"] == head + 1 == body["head"]
    assert batch["head"] == head  # the head the solve READ, for `expected_head`
    state = client.get("/api/map").json()
    assert all(
        s["status"] == "planned" for s in state["shipments"] if s["id"] in batch["assignments"]
    )


def test_commit_plan_refuses_a_moved_log(client: TestClient) -> None:
    """COMMIT PLAN books the plan the operator reviewed or nothing: with
    `expected_head` set, a log that moved in between is a 409, not a silent
    re-solve onto a different world."""
    plan = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    client.post(
        "/api/commands/disrupt", json={"kind": "zone_offline", "target_id": "ZON-A2", "days": 5}
    )
    stale = client.post(
        "/api/commands/optimize", json={"commit": True, "expected_head": plan["head"]}
    )
    assert stale.status_code == 409
    fresh = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    assert fresh["head"] > plan["head"]
    booked = client.post(
        "/api/commands/optimize", json={"commit": True, "expected_head": fresh["head"]}
    )
    assert booked.status_code == 200
    assert booked.json()["committed_events"] > 0


def test_a_drafted_plan_survives_a_reload(client: TestClient) -> None:
    """The plan review flow is state, not a browser tab (§7.5): after a dry run
    the map says a plan is pending and `/api/plan` hands back the whole thing,
    so a reload re-enters review instead of losing it."""
    assert client.get("/api/plan").json()["batch"] is None
    assert client.get("/api/map").json()["pending_plan"] is None

    batch = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    summary = client.get("/api/map").json()["pending_plan"]
    assert summary["batch_id"] == batch["batch_id"]
    assert summary["based_on_seq"] == batch["head"]
    assert summary["head"] == batch["head"] + 1  # the draft IS the head
    assert summary["assigned"] + summary["unassigned"] == len(batch["assignments"])
    for shipment_id, pair in batch["assignments"].items():
        row = summary["decisions"][shipment_id]
        assert [row["facility_id"], row["zone_id"]] == (list(pair) if pair else [None, None])
        assert (row["unassigned_reasons"] == []) is (pair is not None)

    # The full plan comes back verbatim — the records the map draws routes from.
    reloaded = client.get("/api/plan").json()["batch"]
    assert reloaded == batch
    # ... and only at a time when the draft was actually the head.
    at_start = client.get("/api/plan", params={"at": "2026-09-01T00:00:00+00:00"}).json()
    assert at_start["batch"] is None


def test_committing_the_plan_books_what_was_reviewed_and_consumes_the_draft(
    client: TestClient,
) -> None:
    """Determinism (§2) is what makes review meaningful: the commit re-solves on
    top of the draft — the only event since — and must land on the same
    assignments, under the plan's own batch id, leaving nothing pending."""
    batch = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    booked = client.post(
        "/api/commands/optimize", json={"commit": True, "expected_head": batch["head"]}
    )
    assert booked.status_code == 200, booked.text
    body = booked.json()
    assert body["committed_events"] > 0
    assert body["batch"]["assignments"] == batch["assignments"]
    # One batch id across the draft and the commit: the audit chain reads
    # PlanDrafted -> BatchSolved on the same solve.
    assert body["batch_id"] == batch["batch_id"]
    events = client.get("/api/events", params={"after_seq": 0, "limit": 1000}).json()["events"]
    chain = [e["type"] for e in events if e["entity_id"] == batch["batch_id"]]
    assert chain == ["PlanDrafted", "BatchSolved"]

    assert client.get("/api/plan").json()["batch"] is None
    assert client.get("/api/map").json()["pending_plan"] is None
    rows = {s["id"]: s for s in client.get("/api/map").json()["shipments"]}
    for shipment_id, pair in batch["assignments"].items():
        if pair is not None:
            assert rows[shipment_id]["status"] == "allocated"
            assert rows[shipment_id]["destination"]["facility_id"] == pair[0]


def test_discarding_a_plan_un_pends_it(client: TestClient) -> None:
    """DISCARD books nothing back — a draft booked nothing — but the review that
    ended in NO is history too, and the discard is what clears the plan."""
    batch = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    discarded = client.post(
        "/api/commands/plan/discard", json={"batch_id": batch["batch_id"], "reason": "not now"}
    )
    assert discarded.status_code == 200, discarded.text
    assert discarded.json()["appended"] == 1
    assert client.get("/api/plan").json()["batch"] is None
    assert client.get("/api/map").json()["pending_plan"] is None
    assert all(
        s["status"] == "planned"
        for s in client.get("/api/map").json()["shipments"]
        if s["id"] in batch["assignments"]
    )
    # Only the pending plan can be discarded; a second try has nothing to clear.
    again = client.post("/api/commands/plan/discard", json={"batch_id": batch["batch_id"]})
    assert again.status_code == 409
    assert client.post("/api/commands/plan/discard", json={"batch_id": "NOPE"}).status_code == 409


def test_a_staled_draft_stops_being_pending(client: TestClient) -> None:
    """A plan is reviewable only against the world it was solved against: one
    unrelated event later, the map stops offering it and the old `expected_head`
    is refused — the draft stays in the log as the proposal it was."""
    batch = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    client.post(
        "/api/commands/shipments/ready",
        json={"shipment_id": "SHP-1", "new_ready": "2026-09-04T00:00:00+00:00"},
    )
    assert client.get("/api/map").json()["pending_plan"] is None
    assert client.get("/api/plan").json()["batch"] is None
    stale = client.post(
        "/api/commands/optimize", json={"commit": True, "expected_head": batch["head"]}
    )
    assert stale.status_code == 409
    # The proposal itself is not erased; it is simply no longer pending — and the
    # command that staled it terminated it in the same append, so the audit never
    # reads "proposed, and then nothing" (§7.5).
    events = client.get("/api/events", params={"after_seq": 0, "limit": 1000}).json()["events"]
    chain = [e for e in events if e["entity_id"] == batch["batch_id"]]
    assert [e["type"] for e in chain] == ["PlanDrafted", "PlanDiscarded"]
    discard = next(e for e in events if e["seq"] == chain[1]["seq"])
    staler = next(e for e in events if e["seq"] == chain[1]["seq"] + 1)
    assert staler["type"] == "ShipmentReadyChanged"  # one atomic append
    assert discard["type"] == "PlanDiscarded"


def test_rebalance_cannot_ride_over_a_pending_plan(client: TestClient) -> None:
    """`optimize {rebalance, commit}` registers transfers, which would supersede
    the pending plan and then book a batch nobody reviewed under a different
    id — so with a plan pending it is refused outright, and nothing is written."""
    batch = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    head = client.get("/api/events", params={"after_seq": 0, "limit": 1}).json()["head"]
    refused = client.post(
        "/api/commands/optimize",
        json={
            "commit": True,
            "rebalance": True,
            "expected_head": batch["head"],
            "batch_id": batch["batch_id"],
        },
    )
    assert refused.status_code == 409
    assert "pending" in refused.json()["detail"]
    assert client.get("/api/events", params={"after_seq": 0, "limit": 1}).json()["head"] == head
    assert client.get("/api/map").json()["pending_plan"]["batch_id"] == batch["batch_id"]


def test_whatif_command_leaves_log_untouched(client: TestClient) -> None:
    before = client.get("/api/events", params={"after_seq": 0, "limit": 1000}).json()["head"]
    body = client.post(
        "/api/commands/whatif", json={"events": ["close FAC-A 7d"], "policy": "nodal-batch"}
    ).json()
    assert body["fork"]["assignments"]["SHP-2"] is None  # cold storage closed
    assert any(c["shipment_id"] == "SHP-2" for c in body["changes"])
    assert "baseline log untouched" in body["rendered"]
    after = client.get("/api/events", params={"after_seq": 0, "limit": 1000}).json()["head"]
    assert after == before


def test_disrupt_command_reoptimizes(client: TestClient) -> None:
    booked = client.post(
        "/api/commands/allocate", json={"shipment_id": "SHP-2", "commit": True}
    ).json()
    assert booked["record"]["chosen"]["facility_id"] == "FAC-A"
    body = client.post(
        "/api/commands/disrupt",
        json={"kind": "zone_offline", "target_id": "ZON-A2", "days": 5},
    ).json()
    assert body["reopt"]["affected"] == ["SHP-2"]
    # FAC-A's cold zone was the only feasible home: the booking is released.
    assert body["reopt"]["released"] == ["SHP-2"]
    events = client.get("/api/events", params={"after_seq": 0, "limit": 1000}).json()["events"]
    assert any(e["type"] == "AllocationSuperseded" for e in events)


def test_events_feed_pagination(client: TestClient) -> None:
    first = client.get("/api/events", params={"after_seq": 0, "limit": 5}).json()
    assert len(first["events"]) == 5
    rest = client.get("/api/events", params={"after_seq": first["events"][-1]["seq"]}).json()
    assert rest["events"][0]["seq"] == first["events"][-1]["seq"] + 1
    nothing = client.get("/api/events", params={"after_seq": 0, "limit": 0}).json()
    assert nothing["events"] == []


def test_disrupt_rejects_unknown_targets(client: TestClient) -> None:
    """An append-only log cannot take a disruption against a ghost entity."""
    head = client.get("/api/events", params={"after_seq": 0, "limit": 1}).json()["head"]
    response = client.post(
        "/api/commands/disrupt",
        json={"kind": "facility_closed", "target_id": "FAC-GHOST", "days": 2},
    )
    assert response.status_code == 404
    wrong_kind = client.post(
        "/api/commands/disrupt",
        json={"kind": "zone_offline", "target_id": "FAC-A", "days": 2},  # facility, not zone
    )
    assert wrong_kind.status_code == 404
    after = client.get("/api/events", params={"after_seq": 0, "limit": 1}).json()["head"]
    assert after == head  # nothing was written


def test_end_disruption_now_or_reschedule(client: TestClient) -> None:
    """An operator can end a disruption early, or move its end: the latter is
    an end plus a fresh window (append-only log), re-optimized like any new
    disruption, and the read model carries the owning facility."""
    started = client.post(
        "/api/commands/disrupt", json={"kind": "zone_offline", "target_id": "ZON-A2", "days": 5}
    ).json()
    listed = client.get("/api/map").json()["disruptions"]
    mine = next(d for d in listed if d["id"] == started["disruption_id"])
    assert mine["facility_id"] == "FAC-A"  # zone target -> its facility
    # Reschedule: ends now, reopens until the new time.
    moved = client.post(
        "/api/commands/disruptions/end",
        json={"disruption_id": mine["id"], "until": "2026-09-02T12:00:00+00:00"},
    ).json()
    assert moved["ended"] == mine["id"]
    assert moved["disruption_id"] and moved["reopt"] is not None
    listed = client.get("/api/map").json()["disruptions"]
    assert all(d["id"] != mine["id"] for d in listed)
    fresh = next(d for d in listed if d["id"] == moved["disruption_id"])
    assert fresh["until_ts"] == "2026-09-02T12:00:00+00:00"
    assert fresh["target_id"] == "ZON-A2" and fresh["status"] == "active"
    # End now: gone from the read model; ending again is a 409.
    ended = client.post("/api/commands/disruptions/end", json={"disruption_id": fresh["id"]})
    assert ended.status_code == 200 and ended.json()["disruption_id"] is None
    assert all(d["id"] != fresh["id"] for d in client.get("/api/map").json()["disruptions"])
    assert (
        client.post(
            "/api/commands/disruptions/end", json={"disruption_id": fresh["id"]}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/commands/disruptions/end", json={"disruption_id": "DIS-GHOST"}
        ).status_code
        == 404
    )
    # A new end in the past is refused.
    again = client.post(
        "/api/commands/disrupt", json={"kind": "zone_offline", "target_id": "ZON-A2", "days": 5}
    ).json()
    past = client.post(
        "/api/commands/disruptions/end",
        json={"disruption_id": again["disruption_id"], "until": "2020-01-01T00:00:00+00:00"},
    )
    assert past.status_code == 422


# Deliberately imbalanced: F1 hoards stock that F2's demand rate wants (the
# same world tests/test_cli.py uses to prove §7.7 proposes exactly one
# transfer), plus one planned shipment that must NOT be booked by REBALANCE.
REBALANCE_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 60 } }]
  - id: F2
    lat: 40.0
    lon: -100.5
    zones: [{ id: Z2, kind: rack, capacity: { slots: 60 } }]
lanes:
  - { id: L12, from: F1, to: F2, km: 45, minutes: 60, cost_fixed: 40 }
  - { id: L21, from: F2, to: F1, km: 45, minutes: 60, cost_fixed: 40 }
demand_rates:
  - { facility: F1, group: general, per_day: 1 }
  - { facility: F2, group: general, per_day: 10 }
lots:
  - { id: L1, zone: Z1, group: general, quantity: 20, size: { slots: 10 } }
  - { id: L2, zone: Z1, group: general, quantity: 20, size: { slots: 10 } }
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_rebalance_books_transfers_and_leaves_the_planned_queue_alone(tmp_path: Path) -> None:
    """The GUI's REBALANCE books only the transfers it orders — planned
    shipments stay planned (unlike `optimize` with the rebalance flag)."""
    world_file = tmp_path / "rebalance.yaml"
    world_file.write_text(REBALANCE_WORLD, encoding="utf-8")
    db = tmp_path / "rebalance.sqlite3"
    with EventStore(db) as store:
        load_world(world_file, store)
    with TestClient(create_app(db, ObjectiveConfig())) as client:
        body = client.post("/api/commands/rebalance", json={})
        assert body.status_code == 200
        result = body.json()
        assert result["transfers_registered"] == 1
        assert result["committed_events"] > 0
        shipments = client.get("/api/map").json()["shipments"]
        transfer = next(s for s in shipments if s["is_transfer"])
        assert transfer["status"] == "allocated"
        assert transfer["destination"]["facility_id"] == "F2"  # toward the demand
        # The ordinary planned shipment was NOT booked.
        s1 = next(s for s in shipments if s["id"] == "S1")
        assert s1["status"] == "planned"


def test_network_operations_facility_zone_lane_capacity(client: TestClient) -> None:
    """The GUI's network operations: register a facility with zones, add a
    zone, register a lane pair with computed distance/time, resize a zone —
    and the new node is immediately usable end to end."""
    created = client.post(
        "/api/commands/facilities",
        json={
            "name": "Calgary Foothills",
            "lat": 51.05,
            "lon": -114.07,
            "cold_certified": True,
            "zones": [
                {"kind": "rack", "slots": 80},
                {"kind": "cold", "slots": 24, "temp_c": [-25, 5]},
            ],
        },
    )
    assert created.status_code == 200
    fid = created.json()["facility_id"]
    zone_ids = created.json()["zone_ids"]
    assert len(zone_ids) == 2
    row = next(f for f in client.get("/api/map").json()["facilities"] if f["id"] == fid)
    assert row["name"] == "Calgary Foothills"
    assert {z["kind"] for z in row["zones"]} == {"rack", "cold"}

    # Guards: duplicate id, off-earth, inverted band, unknown facility for zones.
    assert (
        client.post(
            "/api/commands/facilities", json={"facility_id": fid, "lat": 0, "lon": 0}
        ).status_code
        == 409
    )
    assert client.post("/api/commands/facilities", json={"lat": 999, "lon": 0}).status_code == 422
    assert (
        client.post(
            "/api/commands/facilities",
            json={"lat": 0, "lon": 0, "zones": [{"kind": "cold", "temp_c": [5, -5]}]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/commands/zones", json={"facility_id": "FAC-NOPE", "kind": "rack", "slots": 5}
        ).status_code
        == 404
    )
    # Minted-id hygiene: empty means 422 (not a silent mint), and ids that
    # would poison read models, DOM labels, or composed lane ids are refused.
    for bad_id in ["", "BAD\nID", "-leading-dash", "x" * 10_000]:
        assert (
            client.post(
                "/api/commands/facilities", json={"facility_id": bad_id, "lat": 1, "lon": 1}
            ).status_code
            == 422
        )
    # The timeline read model refuses absurd horizons instead of grinding
    # under the world lock.
    assert client.get(f"/api/facilities/{fid}", params={"days": 20000}).status_code == 422
    assert client.get(f"/api/facilities/{fid}", params={"days": 0}).status_code == 422

    # A lane pair with computed km/minutes; duplicates refused atomically.
    lane = client.post(
        "/api/commands/lanes",
        json={"from_facility_id": fid, "to_facility_id": "FAC-A", "mode": "road"},
    )
    assert lane.status_code == 200
    assert len(lane.json()["lane_ids"]) == 2
    assert lane.json()["km"] > 0 and lane.json()["minutes"] > 45
    assert (
        client.post(
            "/api/commands/lanes",
            json={"from_facility_id": fid, "to_facility_id": "FAC-A", "mode": "road"},
        ).status_code
        == 409
    )
    # A one-way lane can be completed into a pair: the existing direction is
    # skipped, only the missing reverse is appended.
    one_way = client.post(
        "/api/commands/lanes",
        json={
            "from_facility_id": fid,
            "to_facility_id": "FAC-B",
            "mode": "air",
            "both_directions": False,
        },
    )
    assert one_way.status_code == 200 and len(one_way.json()["lane_ids"]) == 1
    completed = client.post(
        "/api/commands/lanes",
        json={"from_facility_id": fid, "to_facility_id": "FAC-B", "mode": "air"},
    )
    assert completed.status_code == 200
    assert completed.json()["lane_ids"] == [f"L-AIR-FAC-B-{fid}"]
    assert (
        client.post(
            "/api/commands/lanes",
            json={"from_facility_id": fid, "to_facility_id": "FAC-B", "mode": "air"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/commands/lanes",
            json={"from_facility_id": fid, "to_facility_id": fid},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/commands/lanes",
            json={"from_facility_id": fid, "to_facility_id": "FAC-A", "mode": "teleport"},
        ).status_code
        == 422
    )

    # Add a zone, then resize it; the read model shows the new base capacity —
    # and a slots-only resize leaves the other dimensions' caps untouched
    # (None means unbounded, so replacing them would silently uncap the zone).
    added = client.post(
        "/api/commands/zones", json={"facility_id": fid, "kind": "bulk", "volume_m3": 500}
    )
    assert added.status_code == 200
    zid = added.json()["zone_id"]
    resized = client.post("/api/commands/zones/capacity", json={"zone_id": zid, "slots": 42})
    assert resized.status_code == 200
    zones = next(f for f in client.get("/api/map").json()["facilities"] if f["id"] == fid)["zones"]
    bulk = next(z for z in zones if z["id"] == zid)
    assert bulk["capacity"]["slots"] == 42
    assert bulk["capacity"]["volume_l"] == 500_000  # the 500 m³ cap survives
    # And the reverse: a weight-only resize preserves slots and volume.
    assert (
        client.post(
            "/api/commands/zones/capacity", json={"zone_id": zid, "weight_kg": 10_000}
        ).status_code
        == 200
    )
    zones = next(f for f in client.get("/api/map").json()["facilities"] if f["id"] == fid)["zones"]
    bulk = next(z for z in zones if z["id"] == zid)
    assert bulk["capacity"] == {"slots": 42, "weight_g": 10_000_000, "volume_l": 500_000}
    assert client.post("/api/commands/zones/capacity", json={"zone_id": zid}).status_code == 422
    assert (
        client.post(
            "/api/commands/zones/capacity", json={"zone_id": "ZON-NOPE", "slots": 1}
        ).status_code
        == 404
    )

    # End to end: a cold shipment from the new facility books into its cold zone.
    shipped = client.post(
        "/api/commands/shipments",
        json={
            "origin_facility_id": fid,
            "slots": 4,
            "temp_c": [-5, 4],
            "required_tags": [],
        },
    )
    assert shipped.status_code == 200
    sid = shipped.json()["shipment_id"]
    solved = client.post("/api/commands/allocate", json={"shipment_id": sid, "commit": True})
    assert solved.status_code == 200
    chosen = solved.json()["record"]["chosen"]
    assert chosen is not None
    assert chosen["zone_id"] in zone_ids  # its own cold zone wins (zero transport)


def test_shipment_lifecycle_register_delay_cancel(client: TestClient) -> None:
    """The GUI's shipment operations: register a new shipment (it joins the
    queue as PLANNED and is solvable), move its readiness, and cancel it —
    a booked cancellation releases its reservations."""
    created = client.post(
        "/api/commands/shipments",
        json={
            "origin_facility_id": "FAC-B",
            "sku": "WIDGET",
            "group": "general",
            "quantity": 3,
            "slots": 3,
            "weight_kg": 120,
            "deadline": "2026-09-09T00:00:00+00:00",
        },
    )
    assert created.status_code == 200
    sid = created.json()["shipment_id"]
    row = next(s for s in client.get("/api/map").json()["shipments"] if s["id"] == sid)
    assert row["status"] == "planned"
    assert row["lines"] == [{"sku": "WIDGET", "group": "general", "quantity": 3, "uom": "unit"}]

    # Guards: unknown facility, missing origin, bad deadline, duplicate id.
    assert (
        client.post(
            "/api/commands/shipments", json={"origin_facility_id": "FAC-NOPE", "slots": 1}
        ).status_code
        == 404
    )
    assert client.post("/api/commands/shipments", json={"slots": 1}).status_code == 422
    # Off-earth coordinates and inverted cold bands are refused, never logged
    # (a bad coordinate in the append-only log would poison every render).
    assert (
        client.post(
            "/api/commands/shipments",
            json={"origin_lat": 999, "origin_lon": -4000, "origin_label": "bad", "slots": 1},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/commands/shipments",
            json={"origin_facility_id": "FAC-B", "slots": 1, "temp_c": [20, -30]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/commands/shipments",
            json={
                "origin_facility_id": "FAC-B",
                "slots": 1,
                "deadline": "2020-01-01T00:00:00+00:00",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/commands/shipments",
            json={"shipment_id": sid, "origin_facility_id": "FAC-B", "slots": 1},
        ).status_code
        == 409
    )

    # Readiness moves; a delay of a PLANNED shipment appends no disruption.
    moved = client.post(
        "/api/commands/shipments/ready",
        json={"shipment_id": sid, "new_ready": "2026-09-03T00:00:00+00:00"},
    ).json()
    assert moved["reopt"] is None
    row = next(s for s in client.get("/api/map").json()["shipments"] if s["id"] == sid)
    assert row["ready_at"] == "2026-09-03T00:00:00+00:00"

    # Book it, then a further delay re-optimizes under SHIPMENT_DELAYED.
    booked = client.post("/api/commands/allocate", json={"shipment_id": sid, "commit": True})
    assert booked.status_code == 200
    delayed = client.post(
        "/api/commands/shipments/ready",
        json={"shipment_id": sid, "new_ready": "2026-09-04T00:00:00+00:00", "reason": "supplier"},
    ).json()
    assert delayed["reopt"] is not None
    assert sid in delayed["reopt"]["affected"]

    # Cancel releases the booking's reservations.
    zones_before = client.get("/api/facilities/FAC-B").json()["zones"]
    held_before = sum(len(z["reservations"]) for z in zones_before)
    cancelled = client.post("/api/commands/shipments/cancel", json={"shipment_id": sid})
    assert cancelled.status_code == 200
    zones_after = client.get("/api/facilities/FAC-B").json()["zones"]
    assert sum(len(z["reservations"]) for z in zones_after) < held_before
    assert all(s["id"] != sid for s in client.get("/api/map").json()["shipments"])
    # Terminal shipments compact out of the fold: cancelling again is "unknown".
    assert (
        client.post("/api/commands/shipments/cancel", json={"shipment_id": sid}).status_code == 404
    )
    assert (
        client.post("/api/commands/shipments/cancel", json={"shipment_id": "SHP-GHOST"}).status_code
        == 404
    )


def test_optimize_batch_identities_are_unique(client: TestClient) -> None:
    """BatchSolved's entity identity is the batch id; two server solves must
    never alias in the audit log."""
    first = client.post("/api/commands/optimize", json={"commit": True}).json()
    assert first["batch"] is not None
    # Free a booking so a second solve has work: knock its zone offline.
    client.post(
        "/api/commands/disrupt", json={"kind": "zone_offline", "target_id": "ZON-A2", "days": 5}
    )
    second = client.post("/api/commands/optimize", json={"commit": True}).json()
    events = client.get("/api/events", params={"after_seq": 0, "limit": 1000}).json()["events"]
    batch_ids = [e["entity_id"] for e in events if e["type"] == "BatchSolved"]
    assert len(batch_ids) == len(set(batch_ids)), batch_ids
    assert second is not None


def test_token_auth_gates_every_api_route(tmp_path: Path) -> None:
    """With a token configured, unauthenticated and wrong-token requests get
    401 on reads AND commands; the right token passes."""
    db = tmp_path / "auth.sqlite3"
    with EventStore(db) as store:
        load_world(FIXTURES / "world_small.yaml", store)
    app = create_app(db, ObjectiveConfig(), token="s3cret")
    with TestClient(app) as client:
        assert client.get("/api/map").status_code == 401
        assert client.get("/api/map", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert (
            client.post(
                "/api/commands/allocate", json={"shipment_id": "SHP-2", "commit": False}
            ).status_code
            == 401
        )
        ok = client.get("/api/map", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert ok.json()["empty"] is False
        # A non-ASCII probe gets a clean 401, never a 500: the middleware
        # compares header bytes, and str compare_digest would raise on this.
        weird = client.get(
            "/api/map", headers={b"Authorization": "Bearer sëcret".encode("latin-1")}
        )
        assert weird.status_code == 401
        # Browser preflights carry no Authorization; CORS must answer them.
        preflight = client.options(
            "/api/map",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 200
        # The other half of the ordering rationale: a 401 still carries CORS
        # headers, so the browser lets the app read it and show the login.
        denied = client.get("/api/map", headers={"Origin": "http://localhost:5173"})
        assert denied.status_code == 401
        assert denied.headers.get("access-control-allow-origin") == "http://localhost:5173"
        # Default-deny: unknown paths answer 401 before routing can 404 ...
        assert client.get("/api/does-not-exist").status_code == 401
        # ... while the docs pages (no data) stay reachable.
        assert client.get("/api/docs").status_code == 200
        assert client.get("/api/openapi.json").status_code == 200


# A->B delivery world (§7.9). Origin and customer both sit next to FAC-GATE,
# whose storage zone is already full, so the hold must go to FAC-STORE while the
# goods still enter and leave the network through FAC-GATE: an itinerary with a
# lane leg on BOTH sides of the hold. FAC-GATE's cross-dock zone is what lets
# those pass-through stops book their staging dwell; it is never a hold, because
# an API-registered shipment asks for a `rack`. SHP-PLAIN only moves the world
# clock past the load, so a replay has an instant before the API's own writes.
DELIVERY_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-GATE
    lat: 40.0
    lon: -100.0
    zones:
      - { id: Z-GATE, kind: rack, capacity: { slots: 5 } }
      - { id: Z-GATE-DOCK, kind: cross-dock, capacity: { slots: 20 } }
  - id: FAC-STORE
    lat: 40.0
    lon: -104.0
    zones: [{ id: Z-STORE, kind: rack, capacity: { slots: 100 } }]
lanes:
  - { id: LANE-G2S, from: FAC-GATE, to: FAC-STORE, km: 350, minutes: 300, cost_fixed: 100 }
  - { id: LANE-S2G, from: FAC-STORE, to: FAC-GATE, km: 350, minutes: 300, cost_fixed: 100 }
lots:
  - { id: LOT-FULL, zone: Z-GATE, group: general, quantity: 5, size: { slots: 5 } }
shipments:
  - id: SHP-PLAIN
    origin: FAC-GATE
    ready: 2026-09-02T06:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 1, size: { slots: 1 } }]
"""

_INBOUND_THEN_OUTBOUND = [
    ("road", "Origin Gate", "FAC-GATE", None),
    ("road", "FAC-GATE", "FAC-STORE", "LANE-G2S"),
    ("road", "FAC-STORE", "FAC-GATE", "LANE-S2G"),
    ("road", "FAC-GATE", "Customer", None),
]


@pytest.fixture
def delivery_client(tmp_path: Path) -> Iterator[TestClient]:
    world_file = tmp_path / "delivery.yaml"
    world_file.write_text(DELIVERY_WORLD, encoding="utf-8")
    db = tmp_path / "delivery.sqlite3"
    with EventStore(db) as store:
        load_world(world_file, store)
    with TestClient(create_app(db, ObjectiveConfig())) as test_client:
        yield test_client


def _register_delivery(client: TestClient, **overrides: Any) -> str:
    """Register the DELIVERY_WORLD delivery through the API and return its id."""
    payload: dict[str, Any] = {
        "origin_label": "Origin Gate",
        "origin_lat": 40.0,
        "origin_lon": -99.98,
        "slots": 5,
        "destination_label": "Customer",
        "destination_lat": 40.0,
        "destination_lon": -99.96,
        "hold_days": 3,
    }
    payload.update(overrides)
    created = client.post("/api/commands/shipments", json=payload)
    assert created.status_code == 200, created.text
    assert created.json()["delivery"] is True
    return str(created.json()["shipment_id"])


def test_delivery_itinerary_map_and_capacity(delivery_client: TestClient) -> None:
    """Register an A->B delivery through the API (no deadline: an open delivery
    window), book it, and read the whole journey back — the decision carries the
    full itinerary, the map carries both route halves plus the customer point,
    and the hold really occupies the zone for its days."""
    client = delivery_client
    shipment_id = _register_delivery(client)
    row = next(s for s in client.get("/api/map").json()["shipments"] if s["id"] == shipment_id)
    assert row["destination_point"] == {"label": "Customer", "lat": 40.0, "lon": -99.96}
    assert row["hold_days"] == 3
    assert row["destination"] is None  # nothing booked yet

    booked = client.post(
        "/api/commands/allocate", json={"shipment_id": shipment_id, "commit": True}
    )
    assert booked.status_code == 200, booked.text
    chosen = booked.json()["record"]["chosen"]
    assert chosen["facility_id"] == "FAC-STORE"
    itinerary = chosen["itinerary"]
    assert [
        (leg["kind"], leg["from_label"], leg["to_label"], leg["lane_id"])
        for leg in itinerary["legs"]
    ] == _INBOUND_THEN_OUTBOUND
    hold = itinerary["hold"]
    assert (hold["facility_id"], hold["zone_id"]) == ("FAC-STORE", "Z-STORE")
    assert datetime.fromisoformat(hold["until_ts"]) - datetime.fromisoformat(
        hold["from_ts"]
    ) == timedelta(days=3)
    # The customer gets the goods when the last leg lands, after the hold.
    assert itinerary["destination"] == "Customer"
    assert itinerary["delivered_at"] == itinerary["legs"][-1]["arrive"]
    assert datetime.fromisoformat(itinerary["delivered_at"]) > datetime.fromisoformat(
        hold["until_ts"]
    )

    # The log's record is the record the command returned (§7.5) — itinerary and all.
    readback = client.get(f"/api/decisions/{shipment_id}").json()
    assert readback["record"]["chosen"] == chosen

    # The map draws the full journey: the assignment carries the inbound lanes,
    # the outbound half is recovered from the recorded itinerary.
    row = next(s for s in client.get("/api/map").json()["shipments"] if s["id"] == shipment_id)
    assert row["destination"]["facility_id"] == "FAC-STORE"
    assert row["destination"]["route"] == ["LANE-G2S"]
    assert row["destination"]["outbound_route"] == ["LANE-S2G"]
    assert row["destination"]["exit_facility_id"] == "FAC-GATE"
    # Lanes carry their display geometry (none declared here).
    assert all(lane["path"] is None for lane in client.get("/api/map").json()["lanes"])

    # Capacity is consumed at the holding facility for exactly the hold window.
    zones = client.get("/api/facilities/FAC-STORE", params={"days": 6}).json()["zones"]
    zone = next(z for z in zones if z["id"] == "Z-STORE")
    held = next(r for r in zone["reservations"] if r["holder"] == shipment_id)
    # Records dump timestamps as ...Z, read models as ...+00:00 — same instants.
    assert [datetime.fromisoformat(held[k]) for k in ("from_ts", "until_ts")] == [
        datetime.fromisoformat(hold[k]) for k in ("from_ts", "until_ts")
    ]
    assert held["size"]["slots"] == 5
    assert len([b for b in zone["buckets"] if b["occupancy"]["slots"] >= 5]) >= 3


def test_delivery_plan_review_and_cancellation(delivery_client: TestClient) -> None:
    """A dry-run plan is reviewable for deliveries too — each record keeps its
    whole itinerary — and cancelling a booked delivery releases its hold."""
    client = delivery_client
    shipment_id = _register_delivery(client)
    batch = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    itinerary = batch["records"][shipment_id]["chosen"]["itinerary"]
    assert itinerary["hold"]["facility_id"] == "FAC-STORE"
    assert [leg["lane_id"] for leg in itinerary["legs"]] == [None, "LANE-G2S", "LANE-S2G", None]
    assert itinerary["destination"] == "Customer"

    booked = client.post(
        "/api/commands/optimize", json={"commit": True, "expected_head": batch["head"]}
    )
    assert booked.status_code == 200

    def holds() -> list[dict[str, Any]]:
        zones = client.get("/api/facilities/FAC-STORE").json()["zones"]
        zone = next(z for z in zones if z["id"] == "Z-STORE")
        return [r for r in zone["reservations"] if r["holder"] == shipment_id]

    assert len(holds()) == 1
    # Readiness moves work on a delivery like any other shipment.
    delayed = client.post(
        "/api/commands/shipments/ready",
        json={"shipment_id": shipment_id, "new_ready": "2026-09-06T00:00:00+00:00"},
    )
    assert delayed.status_code == 200
    cancelled = client.post("/api/commands/shipments/cancel", json={"shipment_id": shipment_id})
    assert cancelled.status_code == 200
    assert holds() == []


def test_delivery_registration_guards(delivery_client: TestClient) -> None:
    """A half-specified customer point, a negative hold, and an off-earth
    destination are refused before anything reaches the append-only log."""
    client = delivery_client
    head = client.get("/api/events", params={"after_seq": 0, "limit": 1}).json()["head"]
    base: dict[str, Any] = {
        "origin_label": "Origin Gate",
        "origin_lat": 40.0,
        "origin_lon": -99.98,
        "slots": 1,
    }
    for partial in (
        {"destination_label": "Customer"},
        {"destination_lat": 40.0, "destination_lon": -99.96},
        {"destination_label": "Customer", "destination_lat": 40.0},
    ):
        assert client.post("/api/commands/shipments", json={**base, **partial}).status_code == 422
    assert client.post("/api/commands/shipments", json={**base, "hold_days": -1}).status_code == 422
    # A delivery's storage time is hold_days; a dwell_days the engine would
    # silently ignore is refused, not recorded.
    assert (
        client.post(
            "/api/commands/shipments",
            json={
                **base,
                "destination_label": "Customer",
                "destination_lat": 40.0,
                "destination_lon": -99.96,
                "dwell_days": 9,
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/commands/shipments",
            json={
                **base,
                "destination_label": "Customer",
                "destination_lat": 999,
                "destination_lon": -99.96,
            },
        ).status_code
        == 422
    )
    assert client.get("/api/events", params={"after_seq": 0, "limit": 1}).json()["head"] == head
    # An ordinary shipment still registers, and says it is not a delivery.
    plain = client.post("/api/commands/shipments", json=base)
    assert plain.status_code == 200 and plain.json()["delivery"] is False


def test_delivery_map_honors_replay_at(delivery_client: TestClient) -> None:
    """A replay before the delivery was registered shows none of it — while the
    network it was routed over is already there."""
    client = delivery_client
    shipment_id = _register_delivery(client)
    client.post("/api/commands/allocate", json={"shipment_id": shipment_id, "commit": True})
    early = client.get("/api/map", params={"at": "2026-09-01T12:00:00+00:00"}).json()
    assert {f["id"] for f in early["facilities"]} == {"FAC-GATE", "FAC-STORE"}
    assert early["shipments"] == []


# The same delivery, re-decided later: FAC-STORE closes on day 4 and the hold
# moves to FAC-ALT, whose way out is its own lane. FAC-ALT is far enough that it
# never wins while FAC-STORE is open.
REDECIDED_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-GATE
    lat: 40.0
    lon: -100.0
    zones:
      - { id: Z-GATE, kind: rack, capacity: { slots: 5 } }
      - { id: Z-GATE-DOCK, kind: cross-dock, capacity: { slots: 20 } }
  - id: FAC-STORE
    lat: 40.0
    lon: -104.0
    zones: [{ id: Z-STORE, kind: rack, capacity: { slots: 100 } }]
  - id: FAC-ALT
    lat: 44.0
    lon: -104.0
    zones: [{ id: Z-ALT, kind: rack, capacity: { slots: 100 } }]
lanes:
  - { id: LANE-G2S, from: FAC-GATE, to: FAC-STORE, km: 350, minutes: 300, cost_fixed: 100 }
  - { id: LANE-S2G, from: FAC-STORE, to: FAC-GATE, km: 350, minutes: 300, cost_fixed: 100 }
  - { id: LANE-G2A, from: FAC-GATE, to: FAC-ALT, km: 600, minutes: 500, cost_fixed: 400 }
  - { id: LANE-A2G, from: FAC-ALT, to: FAC-GATE, km: 600, minutes: 500, cost_fixed: 400 }
lots:
  - { id: LOT-FULL, zone: Z-GATE, group: general, quantity: 5, size: { slots: 5 } }
shipments:
  - id: SHP-DEL
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -99.98
    ready: 2026-09-02T06:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -99.96 }
    hold_days: 3
    # Storage means a rack: FAC-GATE's cross-dock zone stages the goods on the
    # way through, but is never where they are held.
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_map_replays_the_itinerary_that_was_current_then(tmp_path: Path) -> None:
    """The outbound half lives in the decision record, so a shipment that was
    re-decided later must not leak its NEW route into a replay of an older head:
    the map reads the decision that was current at the instant being replayed."""
    world_file = tmp_path / "redecided.yaml"
    world_file.write_text(REDECIDED_WORLD, encoding="utf-8")
    config = ObjectiveConfig()
    closed_at = datetime(2026, 9, 4, tzinfo=UTC)
    with EventStore(tmp_path / "redecided.sqlite3") as store:
        load_world(world_file, store)
        commit(store, allocate(load_state(store), "SHP-DEL", config))
        disruption = Disruption(
            id="DIS-CLOSE",
            kind=DisruptionKind.FACILITY_CLOSED,
            target_id="FAC-STORE",
            from_ts=closed_at,
            until_ts=closed_at + timedelta(days=10),
        )
        store.append(
            [EventDraft(ts=closed_at, payload=ev.DisruptionStarted(disruption=disruption))]
        )
        result = reoptimize(store, load_state(store), "DIS-CLOSE", config, closed_at)
        assert result.changed == ["SHP-DEL"]

        def row(state_at: datetime | None) -> dict[str, Any]:
            state = load_state(store, at=state_at) if state_at else load_state(store)
            shipments = readmodels.map_state(state, store)["shipments"]
            return next(s for s in shipments if s["id"] == "SHP-DEL")

        booked = row(datetime(2026, 9, 3, tzinfo=UTC))["destination"]
        assert booked["facility_id"] == "FAC-STORE"
        assert (booked["outbound_route"], booked["exit_facility_id"]) == (
            ["LANE-S2G"],
            "FAC-GATE",
        )
        moved = row(None)["destination"]
        assert moved["facility_id"] == "FAC-ALT"
        assert (moved["outbound_route"], moved["exit_facility_id"]) == (["LANE-A2G"], "FAC-GATE")


# A journey with a genuine TRANSIT stop (§7.9): the only storage zone is at
# FAC-STORE, so the goods enter at FAC-GATE, change trucks at FAC-MID, are held,
# and come back out the same way — five stops in four roles. Both intermediate
# facilities offer nothing but a cross-dock zone, which stages a dwell but never
# holds goods that ask for a `rack`. SHP-CLOCK only moves the world clock past
# the load, so a replay has an instant before the API's own writes.
TRANSIT_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-GATE
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z-GATE-DOCK, kind: cross-dock, capacity: { slots: 20 } }]
  - id: FAC-MID
    lat: 40.0
    lon: -102.0
    zones: [{ id: Z-MID-DOCK, kind: cross-dock, capacity: { slots: 20 } }]
  - id: FAC-STORE
    lat: 40.0
    lon: -104.0
    zones: [{ id: Z-STORE, kind: rack, capacity: { slots: 100 } }]
lanes:
  - { id: LANE-G2M, from: FAC-GATE, to: FAC-MID, km: 170, minutes: 180, cost_fixed: 60 }
  - { id: LANE-M2G, from: FAC-MID, to: FAC-GATE, km: 170, minutes: 180, cost_fixed: 60 }
  - { id: LANE-M2S, from: FAC-MID, to: FAC-STORE, km: 170, minutes: 180, cost_fixed: 60 }
  - { id: LANE-S2M, from: FAC-STORE, to: FAC-MID, km: 170, minutes: 180, cost_fixed: 60 }
shipments:
  - id: SHP-CLOCK
    origin: FAC-STORE
    ready: 2026-09-02T06:00:00+00:00
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 1, size: { slots: 1 } }]
"""

_TRANSIT_ITINERARY = [
    ("FAC-GATE", "entry", "Z-GATE-DOCK"),
    ("FAC-MID", "transit", "Z-MID-DOCK"),
    ("FAC-STORE", "hold", "Z-STORE"),
    ("FAC-MID", "transit", "Z-MID-DOCK"),
    ("FAC-GATE", "exit", "Z-GATE-DOCK"),
]


@pytest.fixture
def transit_client(tmp_path: Path) -> Iterator[TestClient]:
    world_file = tmp_path / "transit.yaml"
    world_file.write_text(TRANSIT_WORLD, encoding="utf-8")
    db = tmp_path / "transit.sqlite3"
    with EventStore(db) as store:
        load_world(world_file, store)
    with TestClient(create_app(db, ObjectiveConfig())) as test_client:
        yield test_client


def _register_transit_delivery(client: TestClient) -> str:
    created = client.post(
        "/api/commands/shipments",
        json={
            "shipment_id": "SHP-DEL",
            "origin_label": "Origin Gate",
            "origin_lat": 40.0,
            "origin_lon": -99.98,
            "slots": 4,
            "destination_label": "Customer",
            "destination_lat": 40.0,
            "destination_lon": -99.96,
            "hold_days": 2,
        },
    )
    assert created.status_code == 200, created.text
    return str(created.json()["shipment_id"])


def _roles(stops: list[dict[str, Any]]) -> list[tuple[str, str, str | None]]:
    return [(s["facility_id"], s["role"], s["zone_id"]) for s in stops]


def test_transit_stops_reach_every_read_model(transit_client: TestClient) -> None:
    """A delivery that changes trucks mid-journey (§7.9): the decision, the batch
    plan and the map all carry the same five stops, and the two pass-through
    dwells at FAC-MID are real reservations on its cross-dock zone — visible in
    the facility timeline, on exactly the days they occupy and no others."""
    client = transit_client
    shipment_id = _register_transit_delivery(client)

    # A dry-run plan is reviewable stop by stop, before anything is booked.
    plan = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    assert _roles(plan["records"][shipment_id]["chosen"]["itinerary"]["stops"]) == (
        _TRANSIT_ITINERARY
    )

    booked = client.post(
        "/api/commands/allocate", json={"shipment_id": shipment_id, "commit": True}
    )
    assert booked.status_code == 200, booked.text
    chosen = booked.json()["record"]["chosen"]
    stops = chosen["itinerary"]["stops"]
    assert _roles(stops) == _TRANSIT_ITINERARY
    # The log's record is the record the command returned — stops and all.
    assert client.get(f"/api/decisions/{shipment_id}").json()["record"]["chosen"] == chosen

    # The map carries the same itinerary off the folded assignment, so a journey
    # can be drawn through its transit facilities without fetching the decision.
    row = next(s for s in client.get("/api/map").json()["shipments"] if s["id"] == shipment_id)
    assert _roles(row["destination"]["stops"]) == _TRANSIT_ITINERARY
    assert [datetime.fromisoformat(s["arrive"]) for s in row["destination"]["stops"]] == [
        datetime.fromisoformat(s["arrive"]) for s in stops
    ]
    assert row["destination"]["route"] == ["LANE-G2M", "LANE-M2S"]
    assert row["destination"]["outbound_route"] == ["LANE-S2M", "LANE-M2G"]
    assert row["trapped"] is False

    # The staging dwells occupy FAC-MID for their windows — attributable to the
    # shipment holding them, and named as the pass-through stops they are.
    zone = next(
        z
        for z in client.get("/api/facilities/FAC-MID", params={"days": 6}).json()["zones"]
        if z["id"] == "Z-MID-DOCK"
    )
    dwells = [s for s in stops if s["facility_id"] == "FAC-MID"]
    assert [(r["holder"], r["role"]) for r in zone["reservations"]] == [
        (shipment_id, "transit"),
        (shipment_id, "transit"),
    ]
    assert [datetime.fromisoformat(r["from_ts"]) for r in zone["reservations"]] == [
        datetime.fromisoformat(s["arrive"]) for s in dwells
    ]
    assert [datetime.fromisoformat(r["until_ts"]) for r in zone["reservations"]] == [
        datetime.fromisoformat(s["depart"]) for s in dwells
    ]
    # Daily buckets (§5): the dock is busy on the two dwell days and empty on the
    # days the goods are sitting in the hold instead.
    busy = {datetime.fromisoformat(s["arrive"]).date().isoformat() for s in dwells}
    assert len(busy) == 2
    assert {b["day"]: b["occupancy"]["slots"] for b in zone["buckets"]} == {
        b["day"]: (4 if b["day"] in busy else 0) for b in zone["buckets"]
    }
    # The hold books the holding facility, and is named for what it is.
    held = next(
        r
        for z in client.get("/api/facilities/FAC-STORE").json()["zones"]
        for r in z["reservations"]
        if r["holder"] == shipment_id
    )
    assert held["role"] == "hold"


def test_transit_stops_honor_replay_at(transit_client: TestClient) -> None:
    """Replay consistency: at an instant before the delivery was booked the map
    shows no itinerary and the transit facility no staging."""
    client = transit_client
    shipment_id = _register_transit_delivery(client)
    client.post("/api/commands/allocate", json={"shipment_id": shipment_id, "commit": True})
    before = {"at": "2026-09-01T12:00:00+00:00"}
    assert client.get("/api/map", params=before).json()["shipments"] == []
    zones = client.get("/api/facilities/FAC-MID", params={"days": 6, **before}).json()["zones"]
    zone = next(z for z in zones if z["id"] == "Z-MID-DOCK")
    assert zone["reservations"] == []
    assert all(b["occupancy"]["slots"] == 0 for b in zone["buckets"])


def test_cancelling_a_delivery_releases_hold_and_staging(transit_client: TestClient) -> None:
    """Cancelling mid-hold gives back every booking the journey made — the hold
    AND each staging dwell (§7.9), which are ordinary reservations."""
    client = transit_client
    shipment_id = _register_transit_delivery(client)
    client.post("/api/commands/allocate", json={"shipment_id": shipment_id, "commit": True})

    def booked() -> list[tuple[str, str]]:
        return [
            (facility_id, r["role"])
            for facility_id in ("FAC-GATE", "FAC-MID", "FAC-STORE")
            for z in client.get(f"/api/facilities/{facility_id}").json()["zones"]
            for r in z["reservations"]
            if r["holder"] == shipment_id
        ]

    assert booked() == [
        ("FAC-GATE", "entry"),
        ("FAC-GATE", "exit"),
        ("FAC-MID", "transit"),
        ("FAC-MID", "transit"),
        ("FAC-STORE", "hold"),
    ]
    cancelled = client.post("/api/commands/shipments/cancel", json={"shipment_id": shipment_id})
    assert cancelled.status_code == 200, cancelled.text
    assert booked() == []


# Cargo already sitting at a facility when it shuts (§7.9). SHP-STUCK's goods are
# physically at FAC-GATE, so no re-solve can move them; SHP-DEL merely passes
# through FAC-GATE, so a closure there re-routes it via FAC-ALT — which is
# slightly farther and slightly dearer, so it never wins while FAC-GATE is open.
TRAPPED_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: FAC-GATE
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z-GATE-DOCK, kind: cross-dock, capacity: { slots: 20 } }]
  - id: FAC-ALT
    lat: 40.0
    lon: -100.05
    zones: [{ id: Z-ALT-DOCK, kind: cross-dock, capacity: { slots: 20 } }]
  - id: FAC-STORE
    lat: 40.0
    lon: -104.0
    zones: [{ id: Z-STORE, kind: rack, capacity: { slots: 100 } }]
lanes:
  - { id: LANE-G2S, from: FAC-GATE, to: FAC-STORE, km: 350, minutes: 300, cost_fixed: 100 }
  - { id: LANE-S2G, from: FAC-STORE, to: FAC-GATE, km: 350, minutes: 300, cost_fixed: 100 }
  - { id: LANE-A2S, from: FAC-ALT, to: FAC-STORE, km: 355, minutes: 320, cost_fixed: 150 }
  - { id: LANE-S2A, from: FAC-STORE, to: FAC-ALT, km: 355, minutes: 320, cost_fixed: 150 }
shipments:
  - id: SHP-STUCK
    origin: FAC-GATE
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 2, size: { slots: 2 } }]
  - id: SHP-DEL
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -99.98
    destination: { label: "Customer", lat: 40.0, lon: -99.96 }
    hold_days: 2
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: X, group: general, quantity: 4, size: { slots: 4 } }]
"""


@pytest.fixture
def trapped_client(tmp_path: Path) -> Iterator[TestClient]:
    world_file = tmp_path / "trapped.yaml"
    world_file.write_text(TRAPPED_WORLD, encoding="utf-8")
    db = tmp_path / "trapped.sqlite3"
    with EventStore(db) as store:
        load_world(world_file, store)
    with TestClient(create_app(db, ObjectiveConfig())) as test_client:
        yield test_client


def test_closure_traps_cargo_and_reroutes_the_journey(trapped_client: TestClient) -> None:
    """Closing a facility that holds physical cargo: the goods already there are
    reported as trapped and left exactly where they are (only an operator can
    clear them), while a journey that merely passes through is re-routed around
    it and gets a superseding decision."""
    client = trapped_client
    booked = client.post("/api/commands/optimize", json={"commit": True})
    assert booked.status_code == 200, booked.text
    row = next(s for s in client.get("/api/map").json()["shipments"] if s["id"] == "SHP-DEL")
    assert _roles(row["destination"]["stops"]) == [
        ("FAC-GATE", "entry", "Z-GATE-DOCK"),
        ("FAC-STORE", "hold", "Z-STORE"),
        ("FAC-GATE", "exit", "Z-GATE-DOCK"),
    ]

    closed = client.post(
        "/api/commands/disrupt",
        json={"kind": "facility_closed", "target_id": "FAC-GATE", "days": 4},
    )
    assert closed.status_code == 200, closed.text
    body = closed.json()
    disruption_id = body["disruption_id"]
    # The command says what it could not fix: the affected set the solver saw
    # excludes the trapped shipment, which keeps its booking untouched.
    assert body["reopt"]["trapped"] == [
        {
            "shipment_id": "SHP-STUCK",
            "facility_id": "FAC-GATE",
            "disruption_id": disruption_id,
        }
    ]
    assert body["reopt"]["affected"] == ["SHP-DEL"]
    assert body["reopt"]["moved"] == ["SHP-DEL"]
    assert body["reopt"]["released"] == []

    # And the map keeps saying so, so a queue can badge it long after the command.
    rows = {s["id"]: s for s in client.get("/api/map").json()["shipments"]}
    assert rows["SHP-STUCK"]["trapped"] is True
    assert rows["SHP-STUCK"]["status"] == "allocated"
    assert rows["SHP-DEL"]["trapped"] is False
    # The re-routed journey avoids the closed facility in every role.
    assert _roles(rows["SHP-DEL"]["destination"]["stops"]) == [
        ("FAC-ALT", "entry", "Z-ALT-DOCK"),
        ("FAC-STORE", "hold", "Z-STORE"),
        ("FAC-ALT", "exit", "Z-ALT-DOCK"),
    ]
    assert rows["SHP-DEL"]["destination"]["outbound_route"] == ["LANE-S2A"]
    assert rows["SHP-DEL"]["destination"]["exit_facility_id"] == "FAC-ALT"

    # The audit trail: the old decision is superseded, the new one names its cause.
    feed = client.get("/api/events", params={"limit": 500}).json()["events"]
    types = [(e["type"], e["entity_id"]) for e in feed]
    assert ("AllocationSuperseded", "SHP-DEL") in types
    assert ("AllocationSuperseded", "SHP-STUCK") not in types
    record = client.get("/api/decisions/SHP-DEL").json()["record"]
    assert record["reopt_trigger"] == disruption_id
    assert record["chosen"]["itinerary"]["hold"]["facility_id"] == "FAC-STORE"


def _origin_closed(record: dict[str, Any]) -> set[str]:
    return {
        verdict["constraint_id"]
        for rejection in record["rejected"]
        for verdict in rejection["facility_verdicts"]
    }


def test_a_closed_origin_is_never_solved_out_of(trapped_client: TestClient) -> None:
    """The departure half of the closure rule (§7.9). SHP-STUCK's goods are at
    FAC-GATE and it has no booking yet, so with FAC-GATE shut there is nowhere
    for it to go: the queue badges it trapped, a single allocate explains why,
    and neither a dry-run nor a committed plan books anything for it."""
    client = trapped_client
    closed = client.post(
        "/api/commands/disrupt",
        json={"kind": "facility_closed", "target_id": "FAC-GATE", "days": 4},
    )
    assert closed.status_code == 200, closed.text
    disruption_id = closed.json()["disruption_id"]
    # Nothing is booked yet, so the re-optimization moves nothing — but it must
    # still name the cargo the closure stranded.
    assert closed.json()["reopt"]["trapped"] == [
        {
            "shipment_id": "SHP-STUCK",
            "facility_id": "FAC-GATE",
            "disruption_id": disruption_id,
        }
    ]

    rows = {s["id"]: s for s in client.get("/api/map").json()["shipments"]}
    assert rows["SHP-STUCK"]["status"] == "planned"
    assert rows["SHP-STUCK"]["trapped"] is True

    single = client.post(
        "/api/commands/allocate", json={"shipment_id": "SHP-STUCK", "commit": False}
    )
    assert single.status_code == 200, single.text
    record = single.json()["record"]
    assert record["chosen"] is None
    assert _origin_closed(record) == {"ORIGIN_CLOSED"}
    verdict = record["rejected"][0]["facility_verdicts"][0]
    assert verdict["data"]["facility"] == "FAC-GATE"
    assert verdict["data"]["disruption"] == disruption_id

    dry_run = client.post("/api/commands/optimize", json={"commit": False})
    assert dry_run.status_code == 200, dry_run.text
    batch = dry_run.json()["batch"]
    assert batch["assignments"]["SHP-STUCK"] is None
    assert _origin_closed(batch["records"]["SHP-STUCK"]) == {"ORIGIN_CLOSED"}

    # COMMIT PLAN: the shipment the harness watches must come out the other side
    # still planned, with nothing booked out of the shut hub.
    committed = client.post("/api/commands/optimize", json={"commit": True})
    assert committed.status_code == 200, committed.text
    assert committed.json()["batch"]["assignments"]["SHP-STUCK"] is None
    after = {s["id"]: s for s in client.get("/api/map").json()["shipments"]}
    assert after["SHP-STUCK"]["status"] == "planned"
    assert after["SHP-STUCK"]["trapped"] is True
    booked_out = [
        s["id"]
        for s in after.values()
        if s["status"] == "allocated" and s["origin_facility_id"] == "FAC-GATE"
    ]
    assert booked_out == [], f"the plan booked {booked_out} out of a shut facility"


def test_whatif_reports_the_cargo_a_hypothetical_closure_strands(
    trapped_client: TestClient,
) -> None:
    """A preview whose whole reach is stranded cargo re-optimizes nothing, and
    tier-0 results used to be dropped from the report — so the one thing the
    closure cannot re-route was the one thing it did not mention. The fork must
    also refuse to book the stranded shipment the baseline books happily."""
    body = trapped_client.post(
        "/api/commands/whatif", json={"events": ["close FAC-GATE 7d"], "policy": "nodal-batch"}
    )
    assert body.status_code == 200, body.text
    report = body.json()
    assert [r["trapped"] for r in report["reopts"]] == [
        [{"shipment_id": "SHP-STUCK", "facility_id": "FAC-GATE", "disruption_id": "WHATIF-1"}]
    ]
    assert report["baseline"]["assignments"]["SHP-STUCK"] == ["FAC-STORE", "Z-STORE"]
    assert report["fork"]["assignments"]["SHP-STUCK"] is None


def test_map_replays_trapped_and_stops(tmp_path: Path) -> None:
    """Replay consistency for the closure view: at an instant before the closure
    nothing is trapped and the journey still runs through FAC-GATE; at the head
    both have changed."""
    world_file = tmp_path / "trapped.yaml"
    world_file.write_text(TRAPPED_WORLD, encoding="utf-8")
    config = ObjectiveConfig()
    closed_at = datetime(2026, 9, 2, tzinfo=UTC)
    with EventStore(tmp_path / "trapped.sqlite3") as store:
        load_world(world_file, store)
        for shipment_id in ("SHP-DEL", "SHP-STUCK"):
            commit(store, allocate(load_state(store), shipment_id, config))
        store.append(
            [
                EventDraft(
                    ts=closed_at,
                    payload=ev.DisruptionStarted(
                        disruption=Disruption(
                            id="DIS-CLOSE",
                            kind=DisruptionKind.FACILITY_CLOSED,
                            target_id="FAC-GATE",
                            from_ts=closed_at,
                            until_ts=closed_at + timedelta(days=4),
                        )
                    ),
                )
            ]
        )
        result = reoptimize(store, load_state(store), "DIS-CLOSE", config, closed_at)
        assert [entry.shipment_id for entry in result.trapped] == ["SHP-STUCK"]
        assert result.changed == ["SHP-DEL"]

        def rows(state_at: datetime | None) -> dict[str, dict[str, Any]]:
            state = load_state(store, at=state_at) if state_at else load_state(store)
            return {s["id"]: s for s in readmodels.map_state(state, store)["shipments"]}

        before = rows(datetime(2026, 9, 1, 12, tzinfo=UTC))
        assert before["SHP-STUCK"]["trapped"] is False
        assert [s["facility_id"] for s in before["SHP-DEL"]["destination"]["stops"]] == [
            "FAC-GATE",
            "FAC-STORE",
            "FAC-GATE",
        ]
        after = rows(None)
        assert after["SHP-STUCK"]["trapped"] is True
        assert [s["facility_id"] for s in after["SHP-DEL"]["destination"]["stops"]] == [
            "FAC-ALT",
            "FAC-STORE",
            "FAC-ALT",
        ]


def test_read_models_honor_replay_at(client: TestClient) -> None:
    """Replay consistency: schedules and facility timelines at a historical
    instant must not show live bookings."""
    client.post("/api/commands/allocate", json={"shipment_id": "SHP-2", "commit": True})
    live = client.get("/api/schedules").json()
    assert any(m["shipment_id"] == "SHP-2" for m in live["movements"])
    early = client.get("/api/schedules", params={"at": "2026-09-01T00:00:00+00:00"}).json()
    assert early["movements"] == []
    facility_early = client.get(
        "/api/facilities/FAC-A", params={"days": 2, "at": "2026-09-01T00:00:00+00:00"}
    ).json()
    zone = next(z for z in facility_early["zones"] if z["id"] == "ZON-A2")
    assert zone["reservations"] == []  # the booking does not exist yet


def test_schedules_show_every_window_the_goods_occupy(delivery_client: TestClient) -> None:
    """SCHEDULES is booked movements, and a journey's transit dwells are bookings:
    each stop is a row carrying its role and window, and the last mile is the row
    that ends the journey at the customer. The stay keeps the shape it had, so an
    ordinary shipment still reads exactly as one arrival and one stored-until."""
    client = delivery_client
    shipment_id = _register_delivery(client)
    client.post("/api/commands/allocate", json={"shipment_id": shipment_id, "commit": True})
    client.post("/api/commands/allocate", json={"shipment_id": "SHP-PLAIN", "commit": True})

    movements = client.get("/api/schedules").json()["movements"]
    assert len({m["id"] for m in movements}) == len(movements), "rows are addressable"

    plain = [m for m in movements if m["shipment_id"] == "SHP-PLAIN"]
    assert len(plain) == 1, "an ordinary stay is still one row"
    assert plain[0]["role"] == "hold"
    assert plain[0]["customer_label"] is None
    # The existing columns still mean what they meant: this window's arrival and
    # the instant the goods stop occupying it.
    assert plain[0]["eta"] == plain[0]["arrive"]
    assert plain[0]["departure"] == plain[0]["depart"]
    assert plain[0]["facility_id"] == "FAC-GATE"

    delivery = [m for m in movements if m["shipment_id"] == shipment_id]
    assert [(m["role"], m["facility_id"]) for m in delivery] == [
        ("entry", "FAC-GATE"),
        ("hold", "FAC-STORE"),
        ("exit", "FAC-GATE"),
        ("last_mile", "FAC-GATE"),
    ]
    assert all(m["customer_label"] == "Customer" for m in delivery)
    # Every dwell names the zone it books; the last mile books nothing.
    assert [m["zone_id"] for m in delivery] == ["Z-GATE-DOCK", "Z-STORE", "Z-GATE-DOCK", None]
    # The rows run in travel order and the customer is served last.
    arrivals = [datetime.fromisoformat(m["arrive"]) for m in delivery]
    assert arrivals == sorted(arrivals)
    itinerary = client.get(f"/api/decisions/{shipment_id}").json()["record"]["chosen"]["itinerary"]
    assert datetime.fromisoformat(delivery[-1]["arrive"]) == datetime.fromisoformat(
        itinerary["delivered_at"]
    )


# The same world with a delivery declared up front, so the log can be booked and
# then rewritten without going through the API.
LEGACY_DELIVERY_WORLD = (
    DELIVERY_WORLD
    + """  - id: SHP-OLD
    origin_label: "Origin Gate"
    origin_lat: 40.0
    origin_lon: -99.98
    ready: 2026-09-02T06:00:00+00:00
    destination: { label: "Customer", lat: 40.0, lon: -99.96 }
    hold_days: 3
    requirements: { zone_kinds: [rack] }
    lines: [{ sku: Y, group: general, quantity: 5, size: { slots: 5 } }]
"""
)


def _strip_the_journey(db: Path) -> None:
    """Rewrite the log the way a producer before the journey extension wrote it:
    an assignment with no stops or outbound half, and a decision record with no
    itinerary — so nothing in it says when the customer gets the goods."""
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
            chosen = (payload.get("record") or {}).get("chosen") or {}
            for key in ("itinerary", "staging", "stops", "legs"):
                stripped += chosen.pop(key, "absent") != "absent"
            connection.execute(
                "UPDATE events SET payload = ? WHERE seq = ?", (json.dumps(payload), seq)
            )
        assert stripped, "nothing stripped: the back-compat test proves nothing"
        connection.commit()
    finally:
        connection.close()


def test_schedules_drop_a_last_mile_nobody_ever_scheduled(tmp_path: Path) -> None:
    """A delivery decided before itineraries existed records no delivered-at
    instant, so there is no last mile to put on the board. The row must be left
    out rather than emitted with a null time — SCHEDULES is booked movements, and
    a movement with no time is not one. The stay stands in, which is exactly the
    single row this model produced before stops existed, and every row it returns
    is timed and in time order."""
    world = tmp_path / "legacy.yaml"
    world.write_text(LEGACY_DELIVERY_WORLD, encoding="utf-8")
    db = tmp_path / "legacy.sqlite3"
    with EventStore(db) as store:
        load_world(world, store)
        commit(store, allocate(load_state(store), "SHP-OLD", ObjectiveConfig()))
        commit(store, allocate(load_state(store), "SHP-PLAIN", ObjectiveConfig()))
        booked = readmodels.schedules(load_state(store), store)["movements"]
    # The modern record does show one, so the assertion below is about the strip.
    assert [r["role"] for r in booked if r["shipment_id"] == "SHP-OLD"][-1] == "last_mile"

    _strip_the_journey(db)
    with EventStore(db) as store:
        rows = readmodels.schedules(load_state(store), store)["movements"]
    assert [r["role"] for r in rows if r["shipment_id"] == "SHP-OLD"] == ["hold"]
    assert all(r["eta"] is not None for r in rows), rows
    assert [r["eta"] for r in rows] == sorted(r["eta"] for r in rows)


def _seq_of(client: TestClient, type_: str, entity_id: str) -> list[int]:
    events = client.get("/api/events", params={"after_seq": 0, "limit": 2000}).json()["events"]
    return [e["seq"] for e in events if e["type"] == type_ and e["entity_id"] == entity_id]


def _event_at(client: TestClient, seq: int) -> dict[str, Any]:
    events = client.get("/api/events", params={"after_seq": seq - 1, "limit": 1}).json()["events"]
    return dict(events[0])


def test_committing_a_pending_plan_refuses_the_current_head(client: TestClient) -> None:
    """`expected_head` names the world the PLAN read, and nothing else. The
    current head is not an acceptable answer: a caller that simply echoes it has
    reviewed nothing, and after a second draft that number names a world which is
    no longer the pending plan's."""
    body = client.post("/api/commands/optimize", json={"commit": False}).json()
    head_now = body["head"]  # the draft itself IS the head
    assert head_now == body["batch"]["head"] + 1
    refused = client.post(
        "/api/commands/optimize", json={"commit": True, "expected_head": head_now}
    )
    assert refused.status_code == 409, refused.text
    booked = client.post(
        "/api/commands/optimize", json={"commit": True, "expected_head": body["batch"]["head"]}
    )
    assert booked.status_code == 200, booked.text


def test_a_second_draft_cannot_be_committed_under_the_first_ones_name(
    client: TestClient,
) -> None:
    """Two successive drafts share a head number — the second is based on the seq
    the first draft occupies — so `expected_head` alone cannot tell them apart.
    Naming the batch can, and does."""
    first_body = client.post("/api/commands/optimize", json={"commit": False}).json()
    first, head_after_first = first_body["batch"], first_body["head"]
    second = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    assert second["batch_id"] != first["batch_id"]
    assert second["head"] == head_after_first  # one number, two meanings

    refused = client.post(
        "/api/commands/optimize",
        json={"commit": True, "expected_head": head_after_first, "batch_id": first["batch_id"]},
    )
    assert refused.status_code == 409, refused.text
    assert first["batch_id"] in refused.json()["detail"]
    assert client.get("/api/plan").json()["batch"]["batch_id"] == second["batch_id"]

    booked = client.post(
        "/api/commands/optimize",
        json={"commit": True, "expected_head": second["head"], "batch_id": second["batch_id"]},
    )
    assert booked.status_code == 200, booked.text
    assert booked.json()["batch_id"] == second["batch_id"]


def test_naming_a_batch_with_nothing_pending_is_refused(client: TestClient) -> None:
    refused = client.post("/api/commands/optimize", json={"commit": True, "batch_id": "BATCH-NOPE"})
    assert refused.status_code == 409, refused.text
    assert "nothing is pending" in refused.json()["detail"]


@pytest.mark.parametrize(
    ("path", "payload", "event_type"),
    [
        (
            "/api/commands/shipments",
            {"shipment_id": "SHP-NEW", "origin_facility_id": "FAC-A", "slots": 2},
            "ShipmentRegistered",
        ),
        (
            "/api/commands/disrupt",
            {"kind": "zone_offline", "target_id": "ZON-A2", "days": 5},
            "DisruptionStarted",
        ),
        (
            "/api/commands/shipments/ready",
            {"shipment_id": "SHP-1", "new_ready": "2026-09-04T00:00:00+00:00"},
            "ShipmentReadyChanged",
        ),
        (
            "/api/commands/facilities",
            {"facility_id": "FAC-NEW", "lat": 41.0, "lon": -87.0},
            "FacilityRegistered",
        ),
        (
            "/api/commands/shipments/cancel",
            {"shipment_id": "SHP-1"},
            "ShipmentCancelled",
        ),
    ],
)
def test_a_command_that_stales_a_draft_terminates_it_in_the_same_append(
    client: TestClient, path: str, payload: dict[str, Any], event_type: str
) -> None:
    """Every command that supersedes a drafted plan says so in the log, in the
    SAME atomic append (§7.5). The fold had always stopped treating the draft as
    pending; without this the audit read "proposed, and then nothing"."""
    batch = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    assert client.post(path, json=payload).status_code == 200

    discards = _seq_of(client, "PlanDiscarded", batch["batch_id"])
    assert len(discards) == 1
    assert _event_at(client, discards[0] + 1)["type"] == event_type  # adjacent seqs
    assert client.get("/api/plan").json()["batch"] is None


def test_committing_a_draft_never_discards_it(client: TestClient) -> None:
    """The one append that terminates a draft by BEING its outcome: no discard,
    just `BatchSolved` under the plan's own id."""
    batch = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    booked = client.post(
        "/api/commands/optimize",
        json={"commit": True, "expected_head": batch["head"], "batch_id": batch["batch_id"]},
    )
    assert booked.status_code == 200, booked.text
    events = client.get("/api/events", params={"after_seq": 0, "limit": 2000}).json()["events"]
    assert [e["type"] for e in events if e["entity_id"] == batch["batch_id"]] == [
        "PlanDrafted",
        "BatchSolved",
    ]


def test_discarding_a_draft_appends_exactly_one_discard(client: TestClient) -> None:
    """A discard IS the termination: the choke point must not double it."""
    batch = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    body = client.post(
        "/api/commands/plan/discard", json={"batch_id": batch["batch_id"], "reason": "no"}
    )
    assert body.status_code == 200, body.text
    assert body.json()["appended"] == 1
    assert len(_seq_of(client, "PlanDiscarded", batch["batch_id"])) == 1


def test_rebalance_terminates_the_draft_it_stales(tmp_path: Path) -> None:
    world_file = tmp_path / "rebalance.yaml"
    world_file.write_text(REBALANCE_WORLD, encoding="utf-8")
    db = tmp_path / "rebalance.sqlite3"
    with EventStore(db) as store:
        load_world(world_file, store)
    with TestClient(create_app(db, ObjectiveConfig())) as client:
        batch = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
        assert client.post("/api/commands/rebalance", json={}).status_code == 200
        discards = _seq_of(client, "PlanDiscarded", batch["batch_id"])
        assert len(discards) == 1
        assert _event_at(client, discards[0] + 1)["type"] == "TransferOrdered"


def test_allocate_commit_terminates_the_draft_it_stales(client: TestClient) -> None:
    """Booking one shipment by hand goes through the engine's own commit, not
    the command choke point — and still terminates the draft it supersedes."""
    batch = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    booked = client.post("/api/commands/allocate", json={"shipment_id": "SHP-2", "commit": True})
    assert booked.status_code == 200, booked.text
    discards = _seq_of(client, "PlanDiscarded", batch["batch_id"])
    assert len(discards) == 1
    assert _event_at(client, discards[0] + 1)["type"] == "AllocationDecided"
    assert client.get("/api/plan").json()["batch"] is None


def test_replay_collapses_a_draft_and_its_discard_at_one_instant(client: TestClient) -> None:
    """Every command stamps the world clock (`state.last_ts`), so a draft and the
    command that supersedes it share a timestamp. `at` resolves to the LAST event
    at that instant, so `/api/plan?at=` returns the plan as it ended up —
    discarded — never the moment in between (§11)."""
    batch = client.post("/api/commands/optimize", json={"commit": False}).json()["batch"]
    drafted_at = _event_at(client, batch["head"] + 1)["ts"]
    assert (
        client.get("/api/plan", params={"at": drafted_at}).json()["batch"]["batch_id"]
        == (batch["batch_id"])
    )
    discarded = client.post("/api/commands/plan/discard", json={"batch_id": batch["batch_id"]})
    assert discarded.status_code == 200, discarded.text
    discarded_at = _event_at(client, batch["head"] + 2)["ts"]
    assert discarded_at == drafted_at, "the world clock does not advance on a command"
    assert client.get("/api/plan", params={"at": drafted_at}).json()["batch"] is None
