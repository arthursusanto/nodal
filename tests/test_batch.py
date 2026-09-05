"""Batch optimization (§7.4) — roadmap stage 4 acceptance."""

from pathlib import Path

import pytest

from nodal.allocate import ObjectiveConfig, allocate, commit
from nodal.allocate.batch import commit_batch, solve_batch
from nodal.allocate.engine import AllocateError
from nodal.events import EventStore, load_state
from nodal.events import catalog as ev
from nodal.worlds import load_world
from tests.conftest import FIXTURES
from tests.helpers import at, draft, make_shipment

CORE = ObjectiveConfig(packs=["core"])

MONOTONE = CORE.model_copy(
    update={
        "weights": CORE.weights.model_copy(update={"inv_balance": 0.0}),
    }
)


def contention_store(tmp_path: Path) -> EventStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = EventStore(tmp_path / "contention.sqlite3")
    load_world(FIXTURES / "world_contention.yaml", store)
    return store


def test_singleton_batch_matches_scorer_argmin(world_store: EventStore) -> None:
    """§7.4 consistency invariant, under a config with every component weighted."""
    state = load_state(world_store)
    for sid in ("SHP-1", "SHP-2"):
        single = allocate(state, sid, CORE)
        batch = solve_batch(state, [sid], CORE, state.last_ts, batch_id="B1")
        assert single.chosen is not None
        assert batch.assignments[sid] == (single.chosen.facility_id, single.chosen.zone_id)


def test_batch_beats_sequential_on_contention(tmp_path: Path) -> None:
    """Stage 4 acceptance: sequential single-shipment allocation strands S2; the
    batch solver finds the assignment that serves both — and both are explained."""
    with contention_store(tmp_path) as store:
        state = load_state(store)
        # Sequential: S1's own argmin is F1's cold zone (nearest), which is the
        # only zone S2 can use.
        s1_single = allocate(state, "S1", CORE)
        assert s1_single.chosen is not None
        assert s1_single.chosen.zone_id == "Z1"
        commit(store, s1_single)
        state = load_state(store)
        s2_single = allocate(state, "S2", CORE)
        assert s2_single.chosen is None  # stranded: Z1 headroom 2 < 10

        # Batch over the same starting state:
    with contention_store(tmp_path / "fresh") as store:
        state = load_state(store)
        result = solve_batch(state, ["S1", "S2"], CORE, state.last_ts, batch_id="B1")
        assert result.assignments["S1"] == ("F2", "Z2")
        assert result.assignments["S2"] == ("F1", "Z1")
        assert result.meta.status in ("OPTIMAL", "FLOW_OPTIMAL")
        # Both records carry the batch explanation.
        s1_record = result.records["S1"]
        assert s1_record.mode == "batch" and s1_record.policy == "nodal-batch"
        assert s1_record.batch_context is not None
        assert s1_record.batch_context.best_alternative == "F1/Z1"
        assert s1_record.batch_context.delta_vs_best_alternative is not None
        assert s1_record.solver is not None and s1_record.solver.gap == 0.0


def test_binding_constraints_reported(tmp_path: Path) -> None:
    from nodal.domain.capacity import CapacityVector

    with contention_store(tmp_path) as store:
        # Shrink Z1 to exactly S2's size so the capacity row binds at the optimum.
        store.append(
            [draft(ev.CapacityAdjusted(zone_id="Z1", capacity=CapacityVector(slots=10)), at(0, 8))]
        )
        state = load_state(store)
        result = solve_batch(state, ["S1", "S2"], CORE, state.last_ts, batch_id="B1")
        s2_record = result.records["S2"]
        assert s2_record.batch_context is not None
        assert any(b.startswith("Z1/slots/") for b in s2_record.batch_context.binding_constraints)


def test_commit_batch_applies_and_rejects_stale(tmp_path: Path) -> None:
    with contention_store(tmp_path) as store:
        state = load_state(store)
        result = solve_batch(state, ["S1", "S2"], CORE, state.last_ts, batch_id="B1")
        envelopes = commit_batch(store, result)
        assert envelopes[0].type == "BatchSolved"
        after = load_state(store)
        assert after.shipments["S1"].status.value == "allocated"
        assert after.shipments["S2"].status.value == "allocated"
        with pytest.raises(AllocateError, match="stale batch"):
            commit_batch(store, result)


def test_flow_fast_path_gate(tmp_path: Path) -> None:
    """The fast path engages only under the exact structural conditions —
    tested just inside and just outside each (§7.4)."""
    zeroed = CORE.model_copy(
        update={"weights": CORE.weights.model_copy(update={"congestion": 0.0, "inv_balance": 0.0})}
    )
    with contention_store(tmp_path) as store:
        # Just inside: uniform 10-slot shipments, slots-only capacity, no
        # facility-level terms. (S2's temp requirement only prunes candidates —
        # pruning does not break flow structure.)
        state = load_state(store)
        result = solve_batch(state, ["S1", "S2"], zeroed, state.last_ts, batch_id="B1")
        assert result.meta.status == "FLOW_OPTIMAL"
        assert result.assignments["S2"] == ("F1", "Z1")

        # Outside 1: facility-level terms active -> CP-SAT.
        cp_result = solve_batch(state, ["S1", "S2"], CORE, state.last_ts, batch_id="B2")
        assert cp_result.meta.status == "OPTIMAL"

        # Outside 2: non-uniform sizes -> CP-SAT.
        store.append(
            [draft(ev.ShipmentRegistered(shipment=make_shipment("S3", slots=4)), at(0, 8))]
        )
        state = load_state(store)
        mixed = solve_batch(state, ["S1", "S2", "S3"], zeroed, state.last_ts, batch_id="B3")
        assert mixed.meta.status == "OPTIMAL"


def test_metamorphic_properties(tmp_path: Path) -> None:
    """§12 scoped metamorphic suite on the batch model."""
    with contention_store(tmp_path) as store:
        state = load_state(store)
        now = state.last_ts
        base = solve_batch(state, ["S1", "S2"], MONOTONE, now, batch_id="B1")

        # Permutation invariance: input order changes nothing (always).
        permuted = solve_batch(state, ["S2", "S1"], MONOTONE, now, batch_id="B1")
        assert permuted.assignments == base.assignments
        assert permuted.meta.objective_scaled == base.meta.objective_scaled

        # Tightening a hard constraint never improves the optimum (always).
        store.append(
            [
                draft(
                    ev.CapacityAdjusted(
                        zone_id="Z2",
                        capacity=state.zones["Z2"].capacity.model_copy(update={"slots": 10}),
                    ),
                    at(0, 8),
                )
            ]
        )
        tightened_state = load_state(store)
        tightened = solve_batch(
            tightened_state, ["S1", "S2"], MONOTONE, tightened_state.last_ts, batch_id="B1"
        )
        assert tightened.meta.objective_scaled >= base.meta.objective_scaled

    with contention_store(tmp_path / "grow") as store:
        state = load_state(store)
        base = solve_batch(state, ["S1", "S2"], MONOTONE, state.last_ts, batch_id="B1")
        # Adding capacity never worsens the optimum (monotone config).
        store.append(
            [
                draft(
                    ev.CapacityAdjusted(
                        zone_id="Z1",
                        capacity=state.zones["Z1"].capacity.model_copy(update={"slots": 40}),
                    ),
                    at(0, 8),
                )
            ]
        )
        grown_state = load_state(store)
        grown = solve_batch(grown_state, ["S1", "S2"], MONOTONE, grown_state.last_ts, batch_id="B1")
        assert grown.meta.objective_scaled <= base.meta.objective_scaled

        # Removing a shipment never worsens the optimum (monotone config).
        fewer = solve_batch(grown_state, ["S1"], MONOTONE, grown_state.last_ts, batch_id="B1")
        assert fewer.meta.objective_scaled <= grown.meta.objective_scaled


def test_rebalancing_transfer_moves_toward_target(tmp_path: Path) -> None:
    """§7.7 acceptance: a surplus donor's transfer lands at the under-target
    facility, and the deviation-from-target sum strictly improves once it arrives."""
    from nodal.allocate.rebalance import generate_rebalancing_transfers
    from nodal.events.envelope import EventDraft

    world = tmp_path / "rebalance.yaml"
    world.write_text(
        """
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
  - { id: L21, from: F2, to: F1, km: 70, minutes: 70, cost_fixed: 90 }
demand_rates:
  - { facility: F1, group: general, per_day: 1 }
  - { facility: F2, group: general, per_day: 10 }
lots:
  - { id: LOT-A, zone: Z1, group: general, quantity: 60, size: { slots: 30 } }
  - { id: LOT-B, zone: Z1, group: general, quantity: 40, size: { slots: 20 } }
""",
        encoding="utf-8",
    )
    with EventStore(tmp_path / "w.sqlite3") as store:
        load_world(world, store)
        state = load_state(store)
        now = state.last_ts
        assert now is not None

        def deviation(current) -> float:  # type: ignore[no-untyped-def]
            total = 0.0
            for facility_id in ("F1", "F2"):
                rate = current.demand_rate(facility_id, "general")
                target = float(CORE.cover_days * rate)
                total += abs(current.stock(facility_id, "general") - target) / max(target, 1.0)
            return total

        before = deviation(state)
        transfers = generate_rebalancing_transfers(state, CORE, now, id_prefix="TRF")
        assert transfers, "surplus at F1 (stock 100 vs target 14) must trigger a proposal"
        store.append(
            [EventDraft(ts=now, payload=ev.TransferOrdered(shipment=t)) for t in transfers]
        )
        state = load_state(store)
        result = solve_batch(state, [t.id for t in transfers], CORE, now, batch_id="B1")
        transfer = transfers[0]
        assignment = result.assignments[transfer.id]
        assert assignment is not None and assignment[0] == "F2"
        commit_batch(store, result)
        # Walk the transfer through departure and arrival.
        record = result.records[transfer.id]
        assert record.chosen is not None
        store.append(
            [
                *(
                    EventDraft(
                        ts=now, payload=ev.LotShipped(lot_id=lot_id, shipment_id=transfer.id)
                    )
                    for lot_id in transfer.transfer_lot_ids
                ),
                EventDraft(ts=now, payload=ev.ShipmentDeparted(shipment_id=transfer.id)),
            ]
        )
        eta = record.chosen.eta
        store.append(
            [
                *(
                    EventDraft(
                        ts=eta,
                        payload=ev.LotMoved(
                            lot_id=lot_id,
                            to_zone_id=record.chosen.zone_id,
                            planned_departure=record.chosen.departure,
                        ),
                    )
                    for lot_id in transfer.transfer_lot_ids
                ),
                EventDraft(ts=eta, payload=ev.ShipmentArrived(shipment_id=transfer.id)),
            ]
        )
        after_state = load_state(store)
        after = deviation(after_state)
        assert after < before
        # The record carries the arithmetic (§7.7): the move's transport cost and
        # its balance component are both explicit in the chosen candidate.
        chosen_candidate = next(
            c for c in record.scored if c.facility_id == "F2" and c.zone_id == record.chosen.zone_id
        )
        assert chosen_candidate.components["transport_cost"].raw > 0
        assert "inv_balance" in chosen_candidate.components


def test_transfer_excludes_its_origin(world_store: EventStore) -> None:
    from nodal.domain.capacity import CapacityVector
    from nodal.domain.entities import LotSpec, RequirementSet, Shipment

    transfer = Shipment(
        id="TRF-1",
        origin_facility_id="FAC-A",
        lines=[
            LotSpec(sku="X", commodity_group="general", quantity=5, size=CapacityVector(slots=5))
        ],
        requirements=RequirementSet(size=CapacityVector(slots=5), required_tags=[]),
        ready_at=at(0, 9),
        is_transfer=True,
        transfer_lot_ids=["LOT-1"],
    )
    world_store.append([draft(ev.TransferOrdered(shipment=transfer), at(0, 9))])
    state = load_state(world_store)
    result = solve_batch(state, ["TRF-1"], CORE, state.last_ts, batch_id="B1")
    assignment = result.assignments["TRF-1"]
    assert assignment is not None and assignment[0] != "FAC-A"
