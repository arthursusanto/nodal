"""Single-shipment allocation pipeline (§7) — roadmap stage 2 acceptance."""

import json
import os
from pathlib import Path

import pytest

from nodal.allocate import ObjectiveConfig, allocate, commit
from nodal.allocate.config import ObjectiveWeights
from nodal.allocate.engine import AllocateError
from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import ShipmentStatus
from nodal.events import EventStore, load_state
from nodal.events import catalog as ev
from tests.helpers import at, draft, make_shipment

GOLDENS = Path(__file__).parent / "goldens"
CORE = ObjectiveConfig(packs=["core"])


def test_record_partitions_all_facilities(world_store: EventStore) -> None:
    state = load_state(world_store)
    record = allocate(state, "SHP-1", CORE)
    assert record.considered == ["FAC-A", "FAC-B", "FAC-C"]
    scored_ids = {c.facility_id for c in record.scored}
    rejected_ids = {r.facility_id for r in record.rejected}
    assert scored_ids | rejected_ids == set(record.considered)
    assert scored_ids & rejected_ids == set()
    assert record.chosen is not None
    assert record.chosen.facility_id == "FAC-A"
    assert record.chosen.zone_id == "ZON-A1"
    # FAC-C lacks equipment records for equip:forklift.
    fac_c = next(r for r in record.rejected if r.facility_id == "FAC-C")
    assert any(v.constraint_id == "EQUIPMENT_MISSING" for v in fac_c.facility_verdicts)
    assert record.decided_at == state.last_ts  # engine time, never wall clock


def test_decision_is_byte_reproducible(world_store: EventStore) -> None:
    state = load_state(world_store)
    first = allocate(state, "SHP-1", CORE)
    second = allocate(state, "SHP-1", CORE)
    assert first.model_dump_json() == second.model_dump_json()


def test_weight_flip_changes_winner(world_store: EventStore) -> None:
    """The documented weight-flip example (roadmap stage 2).

    Under the default weights SHP-1 goes to FAC-A (cheapest, fastest). Under a
    profile that cares only about preserving scarce capacity, FAC-A loses: it is
    one of only two forklift providers and SHP-1 would consume 15/90 of its free
    slots (norm 0.333 with the scarcity multiplier), while FAC-B's bulk zone has
    effectively unlimited slot headroom (norm 0.030). The record shows exactly
    that arithmetic.
    """
    state = load_state(world_store)
    default_choice = allocate(state, "SHP-1", CORE)
    assert default_choice.chosen is not None
    assert default_choice.chosen.facility_id == "FAC-A"

    preserve = CORE.model_copy(
        update={
            "weights": ObjectiveWeights(
                transport_cost=0.0,
                travel_time=0.0,
                lateness_risk=0.0,
                congestion=0.0,
                inv_balance=0.0,
                capacity_preservation=1.0,
                transfers=0.0,
                op_risk=0.0,
            )
        }
    )
    flipped = allocate(state, "SHP-1", preserve)
    assert flipped.chosen is not None
    assert flipped.chosen.facility_id == "FAC-B"
    contributions = {
        c.facility_id: c.components["capacity_preservation"].contribution for c in flipped.scored
    }
    assert contributions["FAC-B"] < contributions["FAC-A"]


def test_committed_reservation_blocks_future_capacity(world_store: EventStore) -> None:
    """Roadmap stage 2: a booked reservation makes a later conflicting shipment
    infeasible in the overlapping buckets."""
    state = load_state(world_store)
    record = allocate(state, "SHP-1", CORE)
    commit(world_store, record)

    world_store.append(
        [draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-BIG", slots=80)), at(0, 9))]
    )
    state = load_state(world_store)
    assert state.shipments["SHP-1"].status is ShipmentStatus.ALLOCATED
    big = allocate(state, "SHP-BIG", CORE)
    fac_a = next(r for r in big.rejected if r.facility_id == "FAC-A")
    zone_a1 = fac_a.zone_verdicts["ZON-A1"]
    capacity_verdict = next(v for v in zone_a1 if v.constraint_id == "CAPACITY")
    # 100 slots - 10 (LOT-1) - 15 (SHP-1's reservation) = 75 < 80.
    assert capacity_verdict.data["headroom"] == 75


def test_no_feasible_destination(world_store: EventStore) -> None:
    world_store.append(
        [
            draft(
                ev.ShipmentRegistered(shipment=make_shipment("SHP-VAULT", slots=1)),
                at(0, 9),
            )
        ]
    )
    state = load_state(world_store)
    shipment = state.shipments["SHP-VAULT"]
    shipment.requirements = shipment.requirements.model_copy(
        update={"required_tags": ["security:vault"]}
    )
    record = allocate(state, "SHP-VAULT", CORE)
    assert record.chosen is None
    assert record.scored == []
    assert {r.facility_id for r in record.rejected} == set(record.considered)
    for rejection in record.rejected:
        assert any(v.constraint_id == "REQUIRED_TAGS" for v in rejection.facility_verdicts)


def test_multiple_failing_constraints_all_reported(world_store: EventStore) -> None:
    world_store.append(
        [draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-MULTI", slots=1)), at(0, 9))]
    )
    state = load_state(world_store)
    shipment = state.shipments["SHP-MULTI"]
    shipment.requirements = shipment.requirements.model_copy(
        update={"required_tags": ["security:vault"], "temp_c": (-5, 4)}
    )
    record = allocate(state, "SHP-MULTI", CORE)
    fac_b = next(r for r in record.rejected if r.facility_id == "FAC-B")
    assert any(v.constraint_id == "REQUIRED_TAGS" for v in fac_b.facility_verdicts)
    # Zone-level verdicts are reported alongside the facility-level failure.
    assert any(v.constraint_id == "TEMP_RANGE" for v in fac_b.zone_verdicts.get("ZON-B1", []))


def test_commit_requires_feasible_choice(world_store: EventStore) -> None:
    world_store.append(
        [draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-VAULT", slots=1)), at(0, 9))]
    )
    state = load_state(world_store)
    shipment = state.shipments["SHP-VAULT"]
    shipment.requirements = shipment.requirements.model_copy(
        update={"required_tags": ["security:vault"]}
    )
    record = allocate(state, "SHP-VAULT", CORE)
    with pytest.raises(AllocateError, match="no destination"):
        commit(world_store, record)


def test_allocate_rejects_non_planned(world_store: EventStore) -> None:
    state = load_state(world_store)
    record = allocate(state, "SHP-1", CORE)
    commit(world_store, record)
    state = load_state(world_store)
    with pytest.raises(AllocateError, match="not planned"):
        allocate(state, "SHP-1", CORE)


def _normalized(record_json: str) -> str:
    data = json.loads(record_json)
    data["engine_version"] = "<v>"
    data["tzdata_version"] = "<v>"
    return json.dumps(data, indent=2)


def test_golden_decision_record(world_store: EventStore) -> None:
    """Byte-stable golden for the full SHP-1 record (environment versions
    normalized). Regenerate deliberately with NODAL_REGEN_GOLDENS=1."""
    state = load_state(world_store)
    record = allocate(state, "SHP-1", CORE)
    normalized = _normalized(record.model_dump_json())
    golden_path = GOLDENS / "shp1_decision.json"
    if os.environ.get("NODAL_REGEN_GOLDENS"):
        golden_path.parent.mkdir(exist_ok=True)
        golden_path.write_text(normalized + "\n", encoding="utf-8", newline="\n")
    assert golden_path.exists(), "golden missing: run with NODAL_REGEN_GOLDENS=1"
    assert normalized + "\n" == golden_path.read_text(encoding="utf-8")


def test_operating_window_wait_is_priced_not_rejected(world_store: EventStore) -> None:
    """Stage 2 acceptance: arriving outside operating hours becomes waiting time in
    the price — travel_time includes the wait and eta is the effective arrival —
    unless waiting breaks the deadline, which rejects with DEADLINE_UNREACHABLE."""
    from datetime import UTC, datetime

    world_store.append(
        [draft(ev.EquipmentCountSet(facility_id="FAC-C", tag="equip:forklift", count=2), at(0, 9))]
    )
    # Ready Tuesday 23:30Z = 18:30 America/Chicago — FAC-C just closed (8-18 local).
    ready = datetime(2026, 9, 1, 23, 30, tzinfo=UTC)
    shipment = make_shipment("SHP-WAIT", slots=5, ready=ready)
    shipment.origin_lat, shipment.origin_lon = 35.2, -90.0  # right next to FAC-C
    world_store.append([draft(ev.ShipmentRegistered(shipment=shipment), ready)])
    state = load_state(world_store)
    record = allocate(state, "SHP-WAIT", CORE)
    fac_c = next(c for c in record.scored if c.facility_id == "FAC-C")
    assert fac_c.route.wait_minutes > 600  # closed overnight: 18:xx -> 08:00 local
    assert fac_c.eta == datetime(2026, 9, 2, 13, 0, tzinfo=UTC)  # 08:00 CDT
    travel_time = fac_c.components["travel_time"]
    assert travel_time.raw == fac_c.route.minutes + fac_c.route.wait_minutes

    # Same shipment shape, but the deadline falls inside the wait: rejected.
    tight = make_shipment("SHP-TIGHT", slots=5, ready=ready)
    tight.origin_lat, tight.origin_lon = 35.2, -90.0
    tight.requirements = tight.requirements.model_copy(
        update={"deadline": datetime(2026, 9, 2, 2, 0, tzinfo=UTC)}
    )
    world_store.append([draft(ev.ShipmentRegistered(shipment=tight), ready)])
    state = load_state(world_store)
    record = allocate(state, "SHP-TIGHT", CORE)
    fac_c_rejected = next(r for r in record.rejected if r.facility_id == "FAC-C")
    assert any(v.constraint_id == "DEADLINE_UNREACHABLE" for v in fac_c_rejected.facility_verdicts)


def test_closed_facility_and_offline_zone_wired_into_pipeline(
    world_store: EventStore,
) -> None:
    """Deleting FacilityClosed/ZoneOffline from the engine must fail this test:
    the disruption codes appear in the record produced by allocate itself."""
    from nodal.domain.entities import Disruption, DisruptionKind

    world_store.append(
        [
            draft(
                ev.DisruptionStarted(
                    disruption=Disruption(
                        id="DIS-CLOSE",
                        kind=DisruptionKind.FACILITY_CLOSED,
                        target_id="FAC-B",
                        from_ts=at(0, 9),
                        until_ts=at(20),
                    )
                ),
                at(0, 9),
            ),
            draft(
                ev.DisruptionStarted(
                    disruption=Disruption(
                        id="DIS-ZONE",
                        kind=DisruptionKind.ZONE_OFFLINE,
                        target_id="ZON-A1",
                        from_ts=at(0, 9),
                        until_ts=at(20),
                    )
                ),
                at(0, 9),
            ),
        ]
    )
    state = load_state(world_store)
    record = allocate(state, "SHP-1", CORE)
    fac_b = next(r for r in record.rejected if r.facility_id == "FAC-B")
    assert any(v.constraint_id == "FACILITY_CLOSED" for v in fac_b.facility_verdicts)
    # FAC-A stays feasible via ZON-A2, and the offline sibling zone's verdict is
    # visible on the scored candidate (§7.5 completeness).
    fac_a = next(c for c in record.scored if c.facility_id == "FAC-A")
    assert fac_a.zone_id == "ZON-A2"
    assert any(v.constraint_id == "ZONE_OFFLINE" for v in fac_a.zone_verdicts["ZON-A1"])


def test_fully_blocked_lanes_reject_with_lane_blocked(world_store: EventStore) -> None:
    from nodal.domain.entities import Disruption, DisruptionKind

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
                            until_ts=at(6),
                        )
                    ),
                    at(0, 9),
                )
            ]
        )
    world_store.append(
        [draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-FA", slots=5)), at(0, 10))]
    )
    state = load_state(world_store)
    shipment = state.shipments["SHP-FA"]
    shipment.origin_facility_id = "FAC-A"
    shipment.origin_lat = shipment.origin_lon = None
    record = allocate(state, "SHP-FA", CORE)
    fac_b = next(r for r in record.rejected if r.facility_id == "FAC-B")
    blocked = next(v for v in fac_b.facility_verdicts if v.constraint_id == "LANE_BLOCKED")
    assert "LANE-AB" in blocked.data["blocked"]  # type: ignore[operator]


def test_zone_choice_follows_objective_not_alphabet(world_store: EventStore) -> None:
    """A tiny alphabetically-first zone must lose to a roomier one when the
    objective (capacity_preservation) says so."""
    from nodal.domain.entities import StorageZone

    world_store.append(
        [
            draft(
                ev.ZoneRegistered(
                    zone=StorageZone(
                        id="ZON-A0",
                        facility_id="FAC-A",
                        kind="rack",
                        capacity=CapacityVector(slots=16),
                    )
                ),
                at(0, 9),
            )
        ]
    )
    state = load_state(world_store)
    record = allocate(state, "SHP-1", CORE)
    fac_a = next(c for c in record.scored if c.facility_id == "FAC-A")
    # 15 slots into ZON-A0 would consume 15/16 of its headroom; ZON-A1 offers 90.
    assert fac_a.zone_id == "ZON-A1"


def test_top_k_detail_trims_tail_but_keeps_chosen(world_store: EventStore) -> None:
    """§7.5: beyond top-K, candidates keep totals and route sums but drop
    per-component detail and legs; the chosen candidate always stays detailed."""
    from nodal.allocate.engine import build_record, survey_candidates

    config = CORE.model_copy(update={"top_k_detail": 1})
    state = load_state(world_store)
    record = allocate(state, "SHP-1", config)
    assert record.scored[0].components  # rank 0 fully detailed
    tail = record.scored[1]
    assert tail.components == {}
    assert tail.route.legs == []
    assert tail.total > 0  # the sum survives

    # A policy choosing a facility ranked beyond K keeps its full detail (commit
    # needs the route legs).
    shipment = state.shipments["SHP-1"]
    assert state.last_ts is not None
    surveys = survey_candidates(state, shipment, config, state.last_ts)
    forced = build_record(
        state, shipment, config, state.last_ts, surveys, choice="FAC-B", policy="test"
    )
    chosen_candidate = next(c for c in forced.scored if c.facility_id == "FAC-B")
    assert chosen_candidate.components
    assert chosen_candidate.route.legs


def test_every_emittable_code_has_a_template() -> None:
    from nodal.rules.core import CORE_CONSTRAINTS
    from nodal.rules.messages import CORE_TEMPLATES

    emittable = {c.id for c in CORE_CONSTRAINTS} | {
        "EQUIPMENT_MISSING",
        "NO_ROUTE",
        "LANE_BLOCKED",
        "NO_OPERATING_WINDOW",
        "NO_ELIGIBLE_ZONE",
        # Per-stop verdicts the journey router emits (§7.9).
        "ENTRY_CLOSED",
        "TRANSIT_CLOSED",
        "EXIT_CLOSED",
        "STOP_EQUIPMENT_DOWN",
        "STOP_NO_STAGING",
        "STOP_NO_OPERATING_WINDOW",
    }
    missing = emittable - set(CORE_TEMPLATES)
    assert missing == set()


def test_reproducible_across_fresh_state_loads(world_store: EventStore) -> None:
    first = allocate(load_state(world_store), "SHP-1", CORE)
    second = allocate(load_state(world_store), "SHP-1", CORE)
    assert first.model_dump_json() == second.model_dump_json()


def test_commit_refuses_stale_decision(world_store: EventStore) -> None:
    state = load_state(world_store)
    record = allocate(state, "SHP-1", CORE)
    # The log advances after the decision was taken:
    world_store.append(
        [draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-LATE", slots=1)), at(0, 9))]
    )
    with pytest.raises(AllocateError, match="stale decision"):
        commit(world_store, record)


def test_commit_roundtrips_record_through_event(world_store: EventStore) -> None:
    from nodal.allocate.records import DecisionRecord

    state = load_state(world_store)
    record = allocate(state, "SHP-1", CORE)
    envelopes = commit(world_store, record)
    decided = next(e for e in envelopes if e.type == "AllocationDecided")
    payload = decided.payload
    assert isinstance(payload, ev.AllocationDecided)
    restored = DecisionRecord.from_event_record(payload.record)
    assert restored == record

    state = load_state(world_store)
    shipment = state.shipments["SHP-1"]
    assert shipment.status is ShipmentStatus.ALLOCATED
    assert shipment.assigned is not None
    assert shipment.assigned.facility_id == "FAC-A"
    reservations = state.reservations_on_zone("ZON-A1")
    assert [r.id for r in reservations] == ["RES-SHP-1-1"]
    assert reservations[0].size == CapacityVector(slots=15, volume_l=0, weight_g=6_000_000)
