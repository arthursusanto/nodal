"""Table-driven pass/reject cases for the core constraint set (§7.2)."""

from datetime import datetime

from nodal.allocate.config import ObjectiveConfig
from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import Disruption, DisruptionKind, Shipment
from nodal.events import EventStore, NetworkState, load_state
from nodal.events import catalog as ev
from nodal.network.travel import MatrixTravelModel
from nodal.rules.core import (
    CapacityConstraint,
    CertificationConstraint,
    ClassAllowedConstraint,
    DeadlineConstraint,
    EquipmentConstraint,
    FacilityClosedConstraint,
    RequiredTagsConstraint,
    SegregationConstraint,
    TempRangeConstraint,
    ZoneKindConstraint,
    ZoneOfflineConstraint,
)
from nodal.rules.framework import AllocationContext, Pack, Reject, load_packs
from nodal.rules.messages import render_reject
from tests.helpers import at, draft, make_lot, make_shipment

CONFIG = ObjectiveConfig(packs=["core"])


def ctx_for(
    state: NetworkState,
    eta: datetime | None = None,
    departure: datetime | None = None,
    packs: list[Pack] | None = None,
) -> AllocationContext:
    assert state.last_ts is not None
    base = AllocationContext(
        state=state,
        config=CONFIG,
        travel=MatrixTravelModel(CONFIG.travel),
        now=state.last_ts,
        packs=packs if packs is not None else load_packs(["core"]),
    )
    return base.with_stay(eta or at(1, 6), departure or at(5, 6), None)


def shipment_with(**kwargs: object) -> Shipment:
    shipment = make_shipment("SHP-T", slots=int(kwargs.pop("slots", 10)))
    requirements = shipment.requirements.model_copy(update=kwargs)  # type: ignore[arg-type]
    shipment.requirements = requirements
    return shipment


def test_required_tags(world_state: NetworkState) -> None:
    ctx = ctx_for(world_state)
    shipment = shipment_with(required_tags=["security:fenced"])
    assert (
        RequiredTagsConstraint().check(shipment, world_state.facilities["FAC-A"], None, ctx) is None
    )
    verdict = RequiredTagsConstraint().check(shipment, world_state.facilities["FAC-B"], None, ctx)
    assert verdict is not None and verdict.data == {"missing": ["security:fenced"]}


def test_certification_validity_at_eta(world_state: NetworkState) -> None:
    ctx = ctx_for(world_state)
    shipment = shipment_with(required_tags=["cert:organic"])
    assert (
        CertificationConstraint().check(shipment, world_state.facilities["FAC-C"], None, ctx)
        is None
    )
    verdict = CertificationConstraint().check(shipment, world_state.facilities["FAC-A"], None, ctx)
    assert verdict is not None and verdict.constraint_id == "CERT_INVALID"
    # Past the certificate's validity, FAC-C also rejects.
    late = ctx_for(world_state, eta=datetime(2027, 6, 1, tzinfo=at(0).tzinfo))
    assert (
        CertificationConstraint().check(shipment, world_state.facilities["FAC-C"], None, late)
        is not None
    )


def test_equipment_missing_and_down(world_store: EventStore) -> None:
    state = load_state(world_store)
    shipment = shipment_with(required_tags=["equip:forklift"])
    ctx = ctx_for(state)
    missing = EquipmentConstraint().check(shipment, state.facilities["FAC-C"], None, ctx)
    assert missing is not None and missing.constraint_id == "EQUIPMENT_MISSING"
    assert EquipmentConstraint().check(shipment, state.facilities["FAC-A"], None, ctx) is None

    world_store.append(
        [
            draft(
                ev.DisruptionStarted(
                    disruption=Disruption(
                        id="DIS-EQ",
                        kind=DisruptionKind.EQUIPMENT_DOWN,
                        target_id="FAC-A",
                        detail="equip:forklift",
                        from_ts=at(1),
                        until_ts=at(2),
                    )
                ),
                at(0, 9),
            )
        ]
    )
    disrupted = load_state(world_store)
    down = EquipmentConstraint().check(
        shipment, disrupted.facilities["FAC-A"], None, ctx_for(disrupted)
    )
    assert down is not None and down.constraint_id == "EQUIPMENT_DOWN"
    assert down.data == {"tags": ["equip:forklift"], "disruptions": ["DIS-EQ"]}
    # A stay entirely after the outage passes.
    after = ctx_for(disrupted, eta=at(3), departure=at(5))
    assert EquipmentConstraint().check(shipment, disrupted.facilities["FAC-A"], None, after) is None


def test_facility_closed_overlap(world_store: EventStore) -> None:
    world_store.append(
        [
            draft(
                ev.DisruptionStarted(
                    disruption=Disruption(
                        id="DIS-CL",
                        kind=DisruptionKind.FACILITY_CLOSED,
                        target_id="FAC-B",
                        from_ts=at(2),
                        until_ts=at(4),
                    )
                ),
                at(0, 9),
            )
        ]
    )
    state = load_state(world_store)
    shipment = shipment_with()
    verdict = FacilityClosedConstraint().check(
        shipment, state.facilities["FAC-B"], None, ctx_for(state)
    )
    assert verdict is not None and verdict.data == {"disruption": "DIS-CL"}
    clear = ctx_for(state, eta=at(4, 1), departure=at(6))
    assert (
        FacilityClosedConstraint().check(shipment, state.facilities["FAC-B"], None, clear) is None
    )


def test_deadline(world_state: NetworkState) -> None:
    shipment = shipment_with(deadline=at(1))
    late = ctx_for(world_state, eta=at(1, 6))
    verdict = DeadlineConstraint().check(shipment, world_state.facilities["FAC-A"], None, late)
    assert verdict is not None and verdict.constraint_id == "DEADLINE_UNREACHABLE"
    on_time = ctx_for(world_state, eta=at(0, 20))
    assert (
        DeadlineConstraint().check(shipment, world_state.facilities["FAC-A"], None, on_time) is None
    )


def test_zone_kind(world_state: NetworkState) -> None:
    ctx = ctx_for(world_state)
    shipment = shipment_with(zone_kinds=["tank"])
    facility = world_state.facilities["FAC-A"]
    verdict = ZoneKindConstraint().check(shipment, facility, world_state.zones["ZON-A1"], ctx)
    assert verdict is not None and verdict.data["kind"] == "rack"


def test_temp_range(world_state: NetworkState) -> None:
    ctx = ctx_for(world_state)
    facility = world_state.facilities["FAC-A"]
    cold_zone = world_state.zones["ZON-A2"]
    ambient = world_state.zones["ZON-A1"]
    ok = shipment_with(temp_c=(-5, 4))
    assert TempRangeConstraint().check(ok, facility, cold_zone, ctx) is None
    rejected = TempRangeConstraint().check(ok, facility, ambient, ctx)
    assert rejected is not None and rejected.data["zone_range"] == "uncontrolled"
    too_cold = shipment_with(temp_c=(-30, -28))
    assert TempRangeConstraint().check(too_cold, facility, cold_zone, ctx) is not None


def test_class_allowed(world_store: EventStore) -> None:
    world_store.append(
        [draft(ev.ZoneUpdated(zone_id="ZON-B1", allowed_classes=["food"]), at(0, 9))]
    )
    state = load_state(world_store)
    ctx = ctx_for(state)
    facility = state.facilities["FAC-B"]
    chem = shipment_with(compat_class="chem")
    verdict = ClassAllowedConstraint().check(chem, facility, state.zones["ZON-B1"], ctx)
    assert verdict is not None and verdict.data == {"compat_class": "chem"}
    food = shipment_with(compat_class="food")
    assert ClassAllowedConstraint().check(food, facility, state.zones["ZON-B1"], ctx) is None


def test_segregation_against_lots_and_reservations(world_store: EventStore) -> None:
    world_store.append(
        [
            draft(
                ev.LotReceived(lot=make_lot("LOT-ACID", "ZON-B1", size=CapacityVector(slots=1))),
                at(0, 9),
            )
        ]
    )
    state = load_state(world_store)
    state.lots["LOT-ACID"].compat_class = "acid"  # direct tweak for the test
    pack = Pack(name="testchem", incompatible_pairs=frozenset({frozenset({"acid", "base"})}))
    ctx = ctx_for(state, packs=[pack])
    facility = state.facilities["FAC-B"]
    base_shipment = shipment_with(compat_class="base")
    verdict = SegregationConstraint().check(base_shipment, facility, state.zones["ZON-B1"], ctx)
    assert verdict is not None and verdict.data == {
        "compat_class": "base",
        "conflicts": ["acid"],
    }
    neutral = shipment_with(compat_class="water")
    assert SegregationConstraint().check(neutral, facility, state.zones["ZON-B1"], ctx) is None

    # A *reserved* holder's class segregates too: ZON-B2 has no acid lot, only a
    # reservation whose holder carries acid.
    from tests.helpers import make_reservation, make_shipment

    holder = make_shipment("SHP-ACID", slots=2)
    holder.requirements = holder.requirements.model_copy(update={"compat_class": "acid"})
    world_store.append([draft(ev.ShipmentRegistered(shipment=holder), at(0, 10))])
    world_store.append(
        [
            draft(
                ev.ReservationPlaced(
                    reservation=make_reservation(
                        "RES-ACID",
                        "ZON-B2",
                        "SHP-ACID",
                        size=CapacityVector(slots=2),
                        from_ts=at(1),
                        until_ts=at(6),
                    )
                ),
                at(0, 10),
            )
        ]
    )
    reserved_state = load_state(world_store)
    reserved_ctx = ctx_for(reserved_state, packs=[pack])
    via_reservation = SegregationConstraint().check(
        base_shipment,
        reserved_state.facilities["FAC-B"],
        reserved_state.zones["ZON-B2"],
        reserved_ctx,
    )
    assert via_reservation is not None
    assert via_reservation.data == {"compat_class": "base", "conflicts": ["acid"]}


def test_zone_offline(world_store: EventStore) -> None:
    world_store.append(
        [
            draft(
                ev.DisruptionStarted(
                    disruption=Disruption(
                        id="DIS-ZO",
                        kind=DisruptionKind.ZONE_OFFLINE,
                        target_id="ZON-C1",
                        from_ts=at(1),
                        until_ts=at(3),
                    )
                ),
                at(0, 9),
            )
        ]
    )
    state = load_state(world_store)
    verdict = ZoneOfflineConstraint().check(
        shipment_with(), state.facilities["FAC-C"], state.zones["ZON-C1"], ctx_for(state)
    )
    assert verdict is not None and verdict.data == {"disruption": "DIS-ZO"}


def test_capacity_names_failing_bucket_and_dimension(world_state: NetworkState) -> None:
    ctx = ctx_for(world_state, eta=at(1, 6), departure=at(4))
    shipment = shipment_with(slots=35)
    verdict = CapacityConstraint().check(
        shipment, world_state.facilities["FAC-B"], world_state.zones["ZON-B1"], ctx
    )
    # Day 09-03: effective 30 (DIS-1) minus LOT-3's 20 = 10 headroom < 35.
    assert verdict is not None
    assert verdict.data["dimension"] == "slots"
    assert verdict.data["day"] == "2026-09-03"
    assert verdict.data["headroom"] == 10
    fits = shipment_with(slots=5)
    assert (
        CapacityConstraint().check(
            fits, world_state.facilities["FAC-B"], world_state.zones["ZON-B1"], ctx
        )
        is None
    )


def test_constraints_are_pure(world_state: NetworkState) -> None:
    ctx = ctx_for(world_state)
    shipment = shipment_with(required_tags=["security:fenced"], temp_c=(-5, 4))
    for constraint, zone in [
        (RequiredTagsConstraint(), None),
        (TempRangeConstraint(), world_state.zones["ZON-A1"]),
    ]:
        first = constraint.check(shipment, world_state.facilities["FAC-B"], zone, ctx)
        second = constraint.check(shipment, world_state.facilities["FAC-B"], zone, ctx)
        assert first == second


def test_every_core_rejection_renders(world_state: NetworkState) -> None:
    """Every reject produced above renders to a sentence without raising."""
    samples = [
        Reject(constraint_id="REQUIRED_TAGS", data={"missing": ["x"]}),
        Reject(
            constraint_id="CAPACITY",
            data={"dimension": "slots", "need": 5, "headroom": 1, "day": "2026-09-03"},
        ),
        Reject(constraint_id="UNKNOWN_CODE", data={"a": 1}),
        Reject(constraint_id="TEMP_RANGE", data={}),  # missing keys -> safe fallback
    ]
    for sample in samples:
        text = render_reject(sample)
        assert isinstance(text, str) and text
