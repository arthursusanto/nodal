"""Numeric pins for the CP-SAT batch model (§7.4) — the mutant killers.

These tests exist because a review demonstrated that coefficient mutations
(halved scaling, dropped intercepts, deleted rows) survived the functional suite.
Each test pins arithmetic, not just argmins.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nodal.allocate import ObjectiveConfig, allocate
from nodal.allocate.batch import solve_batch
from nodal.allocate.config import ObjectiveWeights, SolverConfig
from nodal.allocate.engine import survey_candidates
from nodal.domain.capacity import CapacityVector
from nodal.domain.units import OBJECTIVE_SCALE
from nodal.events import EventStore, load_state
from nodal.events import catalog as ev
from nodal.worlds import load_world
from tests.helpers import at, draft, make_lot, make_reservation, make_shipment

CORE = ObjectiveConfig(packs=["core"])
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _world(tmp_path: Path, text: str) -> EventStore:
    path = tmp_path / "world.yaml"
    path.write_text(text, encoding="utf-8")
    store = EventStore(tmp_path / "world.sqlite3")
    load_world(path, store)
    return store


CONGESTION_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 10 } }]
  - id: F2
    lat: 40.0
    lon: -100.05
    zones: [{ id: Z2, kind: rack, capacity: { slots: 1000 } }]
lots:
  - { id: LOT-BASE, zone: Z2, group: general, quantity: 850, size: { slots: 850 } }
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines:
      - { sku: X, group: general, quantity: 6, size: { slots: 6 } }
"""


def congestion_config() -> ObjectiveConfig:
    return CORE.model_copy(
        update={
            "weights": ObjectiveWeights(
                transport_cost=0.0,
                travel_time=0.0,
                lateness_risk=0.0,
                congestion=1.0,
                inv_balance=0.0,
                capacity_preservation=0.0,
                transfers=0.0,
                op_risk=0.0,
            )
        }
    )


def test_marginal_congestion_batch_matches_scorer(tmp_path: Path) -> None:
    """The review's counterexample: a big 85%-full zone absorbs a 6-slot shipment
    with a smaller SYSTEM penalty increase than a tiny empty zone it would push
    to 60%. Marginal semantics make batch and scorer agree — on F2."""
    with _world(tmp_path, CONGESTION_WORLD) as store:
        state = load_state(store)
        config = congestion_config()
        single = allocate(state, "S1", config)
        batch = solve_batch(state, ["S1"], config, state.last_ts, batch_id="B1")
        assert single.chosen is not None
        assert single.chosen.facility_id == "F2"  # marginal: 0.856 vs 0.85 beats 0->0.6
        assert batch.assignments["S1"] == ("F2", "Z2")


def test_congestion_epigraph_arithmetic(tmp_path: Path) -> None:
    """Pin the model's congestion numbers against the scorer's piecewise math:
    the batch objective must equal the chosen candidate's marginal contribution
    (separable terms are zeroed). Kills coefficient/intercept/segment mutants."""
    with _world(tmp_path, CONGESTION_WORLD) as store:
        state = load_state(store)
        config = congestion_config()
        single = allocate(state, "S1", config)
        assert single.chosen is not None
        expected = single.scored[0].total  # only congestion contributes
        batch = solve_batch(state, ["S1"], config, state.last_ts, batch_id="B1")
        assert batch.meta.gap == 0.0
        got = batch.meta.objective_scaled / OBJECTIVE_SCALE
        assert got == pytest.approx(expected, abs=2e-4)
        assert got > 0  # deleting the epigraph rows would drive this to ~0


def test_marginal_balance_batch_matches_scorer(world_store: EventStore) -> None:
    """The review's inv_balance counterexample at weight 5.0."""
    config = CORE.model_copy(
        update={"weights": CORE.weights.model_copy(update={"inv_balance": 5.0})}
    )
    state = load_state(world_store)
    single = allocate(state, "SHP-1", config)
    batch = solve_batch(state, ["SHP-1"], config, state.last_ts, batch_id="B1")
    assert single.chosen is not None
    assert batch.assignments["SHP-1"] == (single.chosen.facility_id, single.chosen.zone_id)
    # And the objective equals the scorer total (marginal-sum property at n=1).
    got = batch.meta.objective_scaled / OBJECTIVE_SCALE
    assert got == pytest.approx(single.scored[0].total, abs=2e-4)


BALANCE_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 500 } }]
  - id: F2
    lat: 40.0
    lon: -100.05
    zones: [{ id: Z2, kind: rack, capacity: { slots: 500 } }]
lanes:
  - { id: L12, from: F1, to: F2, km: 5, minutes: 10, cost_fixed: 1 }
  - { id: L21, from: F2, to: F1, km: 5, minutes: 10, cost_fixed: 1 }
demand_rates:
  - { facility: F2, group: general, per_day: 10 }
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines:
      - { sku: X, group: general, quantity: 50, size: { slots: 50 } }
"""


def test_balance_v_penalty_both_arms(tmp_path: Path) -> None:
    """A deficit facility rewards inbound stock (negative marginal); the V's
    lower arm exists. Deleting either V row or the whole term flips this."""
    with _world(tmp_path, BALANCE_WORLD) as store:
        state = load_state(store)
        config = CORE.model_copy(
            update={
                "weights": ObjectiveWeights(
                    transport_cost=0.0,
                    travel_time=0.0,
                    lateness_risk=0.0,
                    congestion=0.0,
                    inv_balance=1.0,
                    capacity_preservation=0.0,
                    transfers=0.0,
                    op_risk=0.0,
                )
            }
        )
        single = allocate(state, "S1", config)
        assert single.chosen is not None
        assert single.chosen.facility_id == "F2"  # target 140, stock 0: inbound helps
        f2_candidate = next(c for c in single.scored if c.facility_id == "F2")
        assert f2_candidate.components["inv_balance"].contribution < 0  # signed marginal
        batch = solve_batch(state, ["S1"], config, state.last_ts, batch_id="B1")
        assert batch.assignments["S1"] == ("F2", "Z2")
        # Negative marginal objective: the placement improves the network.
        assert batch.meta.objective_scaled < 0


DISRUPTED_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 100 } }]
  - id: F2
    lat: 40.0
    lon: -100.05
    zones: [{ id: Z2, kind: rack, capacity: { slots: 100 } }]
lots:
  - { id: LOT-1, zone: Z1, group: general, quantity: 15, size: { slots: 15 } }
  - { id: LOT-2, zone: Z2, group: general, quantity: 85, size: { slots: 85 } }
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines:
      - { sku: X, group: general, quantity: 5, size: { slots: 5 } }
disruptions:
  - { id: DIS-1, kind: capacity_reduced, target: Z1, from: 2026-09-01T00:00:00+00:00,
      until: 2026-09-30T00:00:00+00:00, magnitude: 0.8 }
"""


def test_congestion_uses_effective_capacity(tmp_path: Path) -> None:
    """Z1 is disrupted to 20 slots holding 15 (75% real utilization, 100% after
    the shipment); pricing it against base capacity would call it nearly empty.
    Batch and scorer must both see the disruption and pick F2."""
    with _world(tmp_path, DISRUPTED_WORLD) as store:
        state = load_state(store)
        config = congestion_config()
        single = allocate(state, "S1", config)
        assert single.chosen is not None
        assert single.chosen.facility_id == "F2"
        batch = solve_batch(state, ["S1"], config, state.last_ts, batch_id="B1")
        assert batch.assignments["S1"] == ("F2", "Z2")


def test_extreme_overload_degrades_gracefully(tmp_path: Path) -> None:
    """A sibling zone shrunk far below its contents (>1000% utilization) must not
    make the model INFEASIBLE (§13)."""
    with _world(tmp_path, CONGESTION_WORLD) as store:
        store.append(
            [
                draft(
                    ev.LotReceived(lot=make_lot("LOT-HUGE", "Z1", size=CapacityVector(slots=200))),
                    at(0, 9),
                )
            ]
        )
        state = load_state(store)
        result = solve_batch(state, ["S1"], congestion_config(), state.last_ts, batch_id="B1")
        assert result.meta.status in ("OPTIMAL", "FEASIBLE")
        assert result.assignments["S1"] == ("F2", "Z2")


def test_capacity_row_is_exact(tmp_path: Path) -> None:
    """Zone headroom fits exactly one of two identical shipments; a +1 mutant
    would admit both."""
    world = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 10 } }]
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 10, size: { slots: 10 } }]
  - id: S2
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 10, size: { slots: 10 } }]
"""
    with _world(tmp_path, world) as store:
        state = load_state(store)
        result = solve_batch(state, ["S1", "S2"], CORE, state.last_ts, batch_id="B1")
        assigned = [sid for sid, pair in result.assignments.items() if pair is not None]
        assert len(assigned) == 1


def test_batch_segregation_between_batch_shipments(tmp_path: Path) -> None:
    """Two incompatible-class shipments both fit one zone; the indicator rows
    must forbid co-location. Deleting them co-locates."""
    world = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones:
      - { id: Z1, kind: rack, capacity: { slots: 100 } }
      - { id: Z2, kind: rack, capacity: { slots: 100 } }
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines: [{ sku: A, group: general, quantity: 5, size: { slots: 5 }, compat_class: acid }]
    requirements: { compat_class: acid }
  - id: S2
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines: [{ sku: B, group: general, quantity: 5, size: { slots: 5 }, compat_class: base }]
    requirements: { compat_class: base }
"""
    from nodal.rules.framework import Pack

    chem_pack = Pack(name="testchem", incompatible_pairs=frozenset({frozenset({"acid", "base"})}))
    with _world(tmp_path, world) as store:
        state = load_state(store)
        result = solve_batch(
            state, ["S1", "S2"], CORE, state.last_ts, batch_id="B1", packs=[chem_pack]
        )
        z1 = result.assignments["S1"]
        z2 = result.assignments["S2"]
        assert z1 is not None and z2 is not None
        assert z1[1] != z2[1]  # never the same zone


def test_flow_gate_rejects_differing_windows(tmp_path: Path) -> None:
    """The review's counterexample class: two stays at one zone occupy
    different bucket windows (here via different dwells), so a single arc per
    zone cannot price both. The gate must route this to CP-SAT."""
    world = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 10 } }]
  - id: F2
    lat: 40.0
    lon: -101.5
    zones: [{ id: Z2, kind: rack, capacity: { slots: 100 } }]
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
    requirements: { dwell_days: 1 }
  - id: S2
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
    requirements: { dwell_days: 3 }
"""
    zeroed = CORE.model_copy(
        update={"weights": CORE.weights.model_copy(update={"congestion": 0.0, "inv_balance": 0.0})}
    )
    with _world(tmp_path, world) as store:
        state = load_state(store)
        result = solve_batch(state, ["S1", "S2"], zeroed, state.last_ts, batch_id="B1")
        assert result.meta.status == "OPTIMAL"  # CP-SAT, not FLOW_OPTIMAL
        assert result.assignments["S1"] == ("F1", "Z1")
        assert result.assignments["S2"] == ("F1", "Z1")  # both fit Z1 (5+5 slots)


def test_flow_prices_unbounded_zone_as_unlimited(tmp_path: Path) -> None:
    """A zone with an empty capacity vector is legal and constrains nothing.
    The fast path must treat it like CP-SAT does (no capacity row), not as a
    zero-capacity arc: here the unbounded zone is the cheap winner."""
    world = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.8
    zones: [{ id: Z1, kind: rack, capacity: { slots: 100 } }]
  - id: F2
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z2, kind: yard, capacity: {} }]
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""
    zeroed = CORE.model_copy(
        update={"weights": CORE.weights.model_copy(update={"congestion": 0.0, "inv_balance": 0.0})}
    )
    with _world(tmp_path, world) as store:
        state = load_state(store)
        result = solve_batch(state, ["S1"], zeroed, state.last_ts, batch_id="B1")
        assert result.meta.status == "FLOW_OPTIMAL"  # the gate admits this world
        assert result.assignments["S1"] == ("F2", "Z2")  # nearby unbounded zone wins


def test_flow_overflow_lands_in_unbounded_zone(tmp_path: Path) -> None:
    """When the bounded zone fits only one of two shipments, the loser must land
    in the unbounded zone — never 'unassigned' with a claimed optimality proof."""
    world = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 5 } }]
  - id: F2
    lat: 40.0
    lon: -100.8
    zones: [{ id: Z2, kind: yard, capacity: {} }]
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
  - id: S2
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""
    # Zero every capacity-shaped preference so transport alone ranks the zones:
    # the near bounded zone is strictly cheaper, and only its arc capacity can
    # push the second shipment to the far unbounded zone.
    zeroed = CORE.model_copy(
        update={
            "weights": CORE.weights.model_copy(
                update={"congestion": 0.0, "inv_balance": 0.0, "capacity_preservation": 0.0}
            )
        }
    )
    with _world(tmp_path, world) as store:
        state = load_state(store)
        result = solve_batch(state, ["S1", "S2"], zeroed, state.last_ts, batch_id="B1")
        assert result.meta.status == "FLOW_OPTIMAL"
        chosen = {result.assignments["S1"], result.assignments["S2"]}
        assert chosen == {("F1", "Z1"), ("F2", "Z2")}  # one each, nobody stranded


def test_flow_gate_rejects_time_phased_headroom(tmp_path: Path) -> None:
    """A reservation mid-stay makes headroom vary across buckets: gate -> CP-SAT."""
    with _world(
        tmp_path,
        """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 50 } }]
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 10, size: { slots: 10 } }]
""",
    ) as store:
        store.append(
            [draft(ev.ShipmentRegistered(shipment=make_shipment("HOLDER", slots=5)), at(0, 9))]
        )
        store.append(
            [
                draft(
                    ev.ReservationPlaced(
                        reservation=make_reservation(
                            "RES-H",
                            "Z1",
                            "HOLDER",
                            size=CapacityVector(slots=5),
                            from_ts=at(2),
                            until_ts=at(3),
                        )
                    ),
                    at(0, 10),
                )
            ]
        )
        state = load_state(store)
        zeroed = CORE.model_copy(
            update={
                "weights": CORE.weights.model_copy(update={"congestion": 0.0, "inv_balance": 0.0})
            }
        )
        result = solve_batch(state, ["S1"], zeroed, state.last_ts, batch_id="B1")
        assert result.meta.status == "OPTIMAL"  # not FLOW_OPTIMAL


def test_flow_relax_respects_every_dimension(tmp_path: Path) -> None:
    """The review's overbooking counterexample: slots ration under flow-relax but
    weight must too. Post-validation drops what hard capacity forbids."""
    world = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 100, weight_kg: 3000 } }]
shipments:
"""
    for i in range(1, 5):
        world += f"""
  - id: S{i}
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines: [{{ sku: X, group: general, quantity: 5, size: {{ slots: 5, weight_kg: 1000 }} }}]
"""
    from nodal.allocate.batch import solve_flow_relax

    with _world(tmp_path, world) as store:
        state = load_state(store)
        result = solve_flow_relax(
            state, ["S1", "S2", "S3", "S4"], CORE, state.last_ts, batch_id="B1"
        )
        assigned = [sid for sid, pair in result.assignments.items() if pair is not None]
        assert len(assigned) == 3  # 3 x 1000kg fits; the 4th would overbook weight


def test_upfront_warm_start_matches_cold_solve(world_store: EventStore) -> None:
    """warm_start=True seeds the hint before the first solve: same proven
    objective as the cold solve, flagged warm_started."""
    state = load_state(world_store)
    cold = solve_batch(state, ["SHP-1", "SHP-2"], CORE, state.last_ts, batch_id="COLD")
    warm_config = CORE.model_copy(update={"solver": SolverConfig(warm_start=True)})
    warm = solve_batch(state, ["SHP-1", "SHP-2"], warm_config, state.last_ts, batch_id="WARM")
    assert warm.meta.warm_started is True
    assert cold.meta.warm_started is False
    assert warm.meta.objective_scaled == cold.meta.objective_scaled
    assert warm.assignments == cold.assignments


def test_fallback_warm_start_rescues_a_failed_cold_solve(
    world_store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the cold solve exhausts its budget with no incumbent, the hinted
    re-solve must produce the real answer — not a fabricated OPTIMAL over a
    solver that never found a solution."""
    from ortools.sat.python import cp_model

    calls = {"n": 0}
    original = cp_model.CpSolver.solve

    def flaky(self: cp_model.CpSolver, model: cp_model.CpModel) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return cp_model.UNKNOWN  # simulate budget exhaustion, no incumbent
        return original(self, model)

    monkeypatch.setattr(cp_model.CpSolver, "solve", flaky)
    state = load_state(world_store)
    result = solve_batch(state, ["SHP-1", "SHP-2"], CORE, state.last_ts, batch_id="B1")
    assert calls["n"] == 2  # the fallback actually re-solved
    assert result.meta.warm_started is True
    assert result.meta.status in ("OPTIMAL", "FEASIBLE")
    assert result.meta.gap is not None
    assert any(pair is not None for pair in result.assignments.values())


def test_wall_budget_marks_result_nonreproducible(world_store: EventStore) -> None:
    """§2: a wall-clock budget is machine-dependent, so the record must say so."""
    config = CORE.model_copy(update={"solver": SolverConfig(max_wall_seconds=60.0)})
    state = load_state(world_store)
    result = solve_batch(state, ["SHP-1", "SHP-2"], config, state.last_ts, batch_id="B1")
    assert result.meta.reproducible is False
    reproducible = solve_batch(state, ["SHP-1", "SHP-2"], CORE, state.last_ts, batch_id="B2")
    assert reproducible.meta.reproducible is True


def test_no_incumbent_reports_nothing_proven(world_store: EventStore) -> None:
    """At a budget too small even to register the warm-start hint, the result is
    still the explicit all-unassigned NO_INCUMBENT — never a fabricated answer."""
    config = CORE.model_copy(update={"solver": SolverConfig(det_time_budget=1e-9)})
    state = load_state(world_store)
    result = solve_batch(state, ["SHP-1", "SHP-2"], config, state.last_ts, batch_id="B1")
    assert result.meta.status == "NO_INCUMBENT"
    assert result.meta.gap is None
    assert result.meta.warm_started is False
    assert all(pair is None for pair in result.assignments.values())


def test_flow_relax_respects_segregation_between_peers(tmp_path: Path) -> None:
    """The survey only checks a zone's EXISTING contents; two incompatible batch
    peers routed to the same zone must be caught by the heuristic's own
    validation (also the warm-start hint safety, §13)."""
    world = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones:
      - { id: Z1, kind: rack, capacity: { slots: 100 } }
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines: [{ sku: A, group: general, quantity: 5, size: { slots: 5 }, compat_class: acid }]
    requirements: { compat_class: acid }
  - id: S2
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines: [{ sku: B, group: general, quantity: 5, size: { slots: 5 }, compat_class: base }]
    requirements: { compat_class: base }
"""
    from nodal.allocate.batch import solve_flow_relax
    from nodal.rules.framework import Pack

    chem = Pack(name="testchem", incompatible_pairs=frozenset({frozenset({"acid", "base"})}))
    with _world(tmp_path, world) as store:
        state = load_state(store)
        result = solve_flow_relax(
            state, ["S1", "S2"], CORE, state.last_ts, batch_id="B1", packs=[chem]
        )
        pairs = [result.assignments["S1"], result.assignments["S2"]]
        assigned = [p for p in pairs if p is not None]
        assert len(assigned) == 1  # only one zone exists; the peer must be dropped
        assert result.assignments["S1"] == ("F1", "Z1")  # first in sorted order wins


def test_batch_records_apply_top_k(tmp_path: Path) -> None:
    world = "start: 2026-09-01T00:00:00+00:00\nfacilities:\n"
    for i in range(6):
        world += f"""
  - id: F{i}
    lat: 40.0
    lon: {-100.0 - i * 0.3}
    zones: [{{ id: Z{i}, kind: rack, capacity: {{ slots: 100 }} }}]
"""
    world += """
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T08:00:00+00:00
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""
    with _world(tmp_path, world) as store:
        state = load_state(store)
        config = CORE.model_copy(update={"top_k_detail": 2})
        result = solve_batch(state, ["S1"], config, state.last_ts, batch_id="B1")
        record = result.records["S1"]
        detailed = [c for c in record.scored if c.components]
        trimmed = [c for c in record.scored if not c.components]
        assert len(detailed) <= 3  # top-2 plus possibly the chosen
        assert trimmed and all(c.total for c in trimmed)


def test_rebalance_generation_guards(tmp_path: Path) -> None:
    """Threshold respected, claimed lots excluded, donors ordered by surplus."""
    from nodal.allocate.rebalance import generate_rebalancing_transfers

    world = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 500 } }]
  - id: F2
    lat: 40.0
    lon: -100.4
    zones: [{ id: Z2, kind: rack, capacity: { slots: 500 } }]
  - id: F3
    lat: 40.0
    lon: -100.8
    zones: [{ id: Z3, kind: rack, capacity: { slots: 500 } }]
demand_rates:
  - { facility: F1, group: general, per_day: 1 }
  - { facility: F2, group: general, per_day: 1 }
  - { facility: F3, group: general, per_day: 10 }
lots:
  - { id: LOT-A1, zone: Z1, group: general, quantity: 30, size: { slots: 15 } }
  - { id: LOT-A2, zone: Z1, group: general, quantity: 30, size: { slots: 15 } }
  - { id: LOT-B1, zone: Z2, group: general, quantity: 16, size: { slots: 8 } }
"""
    with _world(tmp_path, world) as store:
        state = load_state(store)
        now = state.last_ts
        assert now is not None
        config = CORE.model_copy(update={"rebalance_max_per_cycle": 1})
        transfers = generate_rebalancing_transfers(state, config, now, id_prefix="TRF")
        # F1 surplus ratio (60-14)/14 = 3.29 > F2's (16-14)/14 = 0.14 (< threshold).
        assert len(transfers) == 1
        assert transfers[0].origin_facility_id == "F1"
        # Claimed lots are excluded on the next cycle.
        store.append([draft(ev.TransferOrdered(shipment=transfers[0]), now)])
        state = load_state(store)
        again = generate_rebalancing_transfers(state, config, now, id_prefix="TRF2")
        claimed = set(transfers[0].transfer_lot_ids)
        assert all(not (set(t.transfer_lot_ids) & claimed) for t in again)


def test_survey_shared_cache_matches_fresh(world_store: EventStore) -> None:
    """A shared EvalCache must not change any verdict vs a fresh survey."""
    from nodal.allocate.scorer import EvalCache

    state = load_state(world_store)
    shipment = state.shipments["SHP-1"]
    fresh = survey_candidates(state, shipment, CORE, state.last_ts)
    cached = survey_candidates(state, shipment, CORE, state.last_ts, cache=EvalCache(state))
    assert [(s.facility_id, s.passing_zones, s.facility_verdicts) for s in fresh] == [
        (s.facility_id, s.passing_zones, s.facility_verdicts) for s in cached
    ]
