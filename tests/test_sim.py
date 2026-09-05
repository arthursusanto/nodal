"""Simulator, generator, and benchmark harness (§9, §10) — stage 3 acceptance."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from nodal.bench.harness import run_bench
from nodal.bench.kpi import RunStats, compute_kpis
from nodal.events import EventStore, load_state
from nodal.sim.generator import generate
from nodal.sim.runner import run_scenario
from nodal.sim.scenarios import ScenarioSpec, load_scenario

SCENARIOS = Path(__file__).parent.parent / "scenarios"
GOLDENS = Path(__file__).parent / "goldens"


def tiny_spec() -> ScenarioSpec:
    return load_scenario(SCENARIOS / "ci-tiny.yaml")


def test_generator_is_seed_deterministic() -> None:
    spec = tiny_spec()
    first = generate(spec)
    second = generate(spec)
    assert len(first.world_drafts) == len(second.world_drafts)
    assert [(ts, s.id, s.requirements.deadline) for ts, s in first.shipment_plan] == [
        (ts, s.id, s.requirements.deadline) for ts, s in second.shipment_plan
    ]
    different_seed = generate(spec.model_copy(update={"seed": 8}))
    assert [(ts, s.id) for ts, s in first.shipment_plan] != [
        (ts, s.id) for ts, s in different_seed.shipment_plan
    ]


def _log_fingerprint(db: Path) -> list[tuple[int, str, str, str]]:
    with EventStore(db) as store:
        return [
            (e.seq, e.ts.isoformat(), e.type, e.payload.model_dump_json()) for e in store.read()
        ]


def test_run_is_byte_deterministic(tmp_path: Path) -> None:
    """Stage 3 acceptance: same scenario/seed/policy => byte-identical event log
    and outcome KPIs, twice in a row."""
    spec = tiny_spec()
    stats_a = run_scenario(spec, "nodal-single", tmp_path / "a.sqlite3")
    stats_b = run_scenario(spec, "nodal-single", tmp_path / "b.sqlite3")
    assert _log_fingerprint(tmp_path / "a.sqlite3") == _log_fingerprint(tmp_path / "b.sqlite3")
    report_a = compute_kpis(stats_a)
    report_b = compute_kpis(stats_b)
    assert report_a.outcome_json() == report_b.outcome_json()
    # Performance sections exist but are excluded from the identity surface.
    assert "decision_ms_mean" in report_a.performance


def test_tiny_scenario_outcome_golden(tmp_path: Path) -> None:
    """CI regression guard: outcome KPIs asserted exactly against a golden."""
    stats = run_scenario(tiny_spec(), "nodal-single", tmp_path / "w.sqlite3")
    outcome = compute_kpis(stats).outcome_json()
    golden_path = GOLDENS / "ci_tiny_nodal_single.json"
    if os.environ.get("NODAL_REGEN_GOLDENS"):
        golden_path.parent.mkdir(exist_ok=True)
        golden_path.write_text(outcome + "\n", encoding="utf-8", newline="\n")
    assert golden_path.exists(), "golden missing: run with NODAL_REGEN_GOLDENS=1"
    assert outcome + "\n" == golden_path.read_text(encoding="utf-8")


def test_simulation_produces_activity(tmp_path: Path) -> None:
    stats = run_scenario(tiny_spec(), "nodal-single", tmp_path / "w.sqlite3")
    assert stats.decisions, "no shipments were generated"
    allocated = [d for d in stats.decisions if d.allocated]
    assert allocated, "nothing was allocated"
    assert stats.arrivals, "nothing arrived"
    assert stats.utilization_samples, "no utilization sampling happened"
    assert all(d.cost_cents > 0 or d.km == 0.0 for d in allocated)


@pytest.mark.bench
def test_contention_demo_optimizer_wins(tmp_path: Path) -> None:
    """The documented contention demonstration (roadmap stage 3), pinned **at its
    documented seed (42)**: nodal-single spends less than every naive baseline and
    leaves fewer shipments unserved.

    Against greedy and first-available that is the plain absolute bill: they serve
    no more shipments than nodal-single does and still cost more in total, so
    `total_cost_cents` is the strictest comparison available.

    Only nearest-feasible needs reading per shipment served. It buys a smaller
    total by refusing work — at this seed nodal-single serves several more
    shipments and therefore spends slightly more in total while costing less per
    shipment — so the sum there would reward a policy for what it walked away
    from. The claim against it is the composite the trade actually makes: cheaper
    per shipment served, no more of them left unserved, and no worse on time.

    This is a per-seed regression pin, not a mechanism proof — single-shipment
    scoring wins cost on most but not all seeds and can lose on service; the full
    answer to contention is stage-4 batch optimization. On-time rate is
    structurally 1.0 for every policy at this stage: deadline-infeasible
    candidates are hard-filtered and delay mechanics arrive in stage 5, so
    service failure shows up as `unallocated`.
    """
    spec = load_scenario(SCENARIOS / "generic-contention.yaml")
    assert spec.seed == 42  # the documented seed; claims below are per-seed
    results = {
        policy: compute_kpis(run_scenario(spec, policy, tmp_path / f"{policy}.sqlite3"))
        for policy in ("nodal-single", "greedy", "nearest-feasible", "first-available")
    }

    def per_served(outcome: dict[str, Any]) -> float:
        served = int(outcome["allocated"])  # type: ignore[arg-type]
        assert served > 0
        return float(outcome["total_cost_cents"]) / served  # type: ignore[arg-type]

    ours = results["nodal-single"].outcome
    for name in ("greedy", "first-available"):
        theirs = results[name].outcome
        assert ours["total_cost_cents"] < theirs["total_cost_cents"], name
        assert ours["unallocated"] <= theirs["unallocated"], name
        assert ours["on_time_rate"] >= theirs["on_time_rate"], name

    nearest = results["nearest-feasible"].outcome
    assert per_served(ours) < per_served(nearest)
    assert ours["unallocated"] <= nearest["unallocated"]
    assert ours["on_time_rate"] >= nearest["on_time_rate"]


def test_weekday_modulation_thins_arrivals() -> None:
    """factors [1,0,0,0,0,0,0]: every accepted arrival lands on a Monday."""
    spec = tiny_spec().model_copy(
        update={
            "demand": tiny_spec().demand.model_copy(
                update={"weekday_factors": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]}
            ),
            "horizon_days": 21,
        }
    )
    workload = generate(spec)
    assert workload.shipment_plan, "modulated demand generated nothing"
    assert all(ts.weekday() == 0 for ts, _ in workload.shipment_plan)


def test_consumption_is_fifo_and_conservative(tmp_path: Path) -> None:
    """Consumption drains the oldest lot first; footprints never grow and reach
    exactly zero with the quantity."""
    from datetime import UTC, datetime

    from nodal.domain.capacity import CapacityVector
    from nodal.events import EventStore, load_state
    from nodal.events.envelope import EventDraft
    from nodal.sim.runner import _Sim
    from nodal.worlds import load_world
    from tests.conftest import FIXTURES

    store = EventStore(tmp_path / "w.sqlite3")
    load_world(FIXTURES / "world_small.yaml", store)
    spec = tiny_spec()
    sim = _Sim.__new__(_Sim)
    sim.spec = spec
    sim.config = spec.objective
    sim.store = store
    sim.state = load_state(store)
    now = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)

    def append(payloads, ts, cause=None):  # type: ignore[no-untyped-def]
        envelopes = store.append([EventDraft(ts=ts, payload=p, cause=cause) for p in payloads])
        from nodal.events import fold

        fold(envelopes, into=sim.state)

    sim.append = append  # type: ignore[method-assign]
    before_occ = sim.state.occupancy("ZON-B1", now.date())
    # FAC-B consumes 12/day of "general"; LOT-3 (25 units, oldest) drains first.
    sim._consume(now)
    state = sim.state
    assert state.lots["LOT-3"].quantity == 13  # 25 - 12
    after_occ = state.occupancy("ZON-B1", now.date())
    assert after_occ.demand("slots") <= before_occ.demand("slots")
    # Drain to zero across further days: footprint hits exactly zero with quantity.
    for day in range(3, 6):
        sim._consume(datetime(2026, 9, day, 0, 0, tzinfo=UTC))
    assert state.lots["LOT-3"].quantity == 0
    assert state.lots["LOT-3"].size == CapacityVector(slots=0, volume_l=0, weight_g=0)
    store.close()


def test_batch_policy_runs_deterministically(tmp_path: Path) -> None:
    """nodal-batch in the sim: pending shipments solve at batch ticks, results are
    byte-deterministic, and every solve carries a proven gap."""
    spec = tiny_spec()
    stats_a = run_scenario(spec, "nodal-batch", tmp_path / "a.sqlite3")
    stats_b = run_scenario(spec, "nodal-batch", tmp_path / "b.sqlite3")
    assert _log_fingerprint(tmp_path / "a.sqlite3") == _log_fingerprint(tmp_path / "b.sqlite3")
    assert compute_kpis(stats_a).outcome_json() == compute_kpis(stats_b).outcome_json()
    outcome = compute_kpis(stats_a).outcome
    assert outcome["allocated"] > 0
    assert outcome["batch_solves"] > 0
    assert outcome["batch_gap_max"] == 0.0  # tiny instances prove optimality
    assert stats_a.arrivals


def test_flow_relax_policy_runs(tmp_path: Path) -> None:
    stats = run_scenario(tiny_spec(), "flow-relax", tmp_path / "w.sqlite3")
    outcome = compute_kpis(stats).outcome
    assert outcome["batch_solves"] > 0
    assert outcome["batch_gap_max"] is None  # heuristic: nothing proven
    assert outcome["allocated"] > 0


def test_rebalance_smoke_log_replays(tmp_path: Path) -> None:
    """Runner rebalance path (§7.7) on the tiny scenario: whatever transfers get
    proposed, claimed, cancelled, or moved, the log must replay from scratch."""
    spec = tiny_spec().model_copy(update={"rebalance": True})
    run_scenario(spec, "nodal-batch", tmp_path / "w.sqlite3")
    with EventStore(tmp_path / "w.sqlite3") as store:
        state = load_state(store)  # a corrupt transfer lifecycle raises FoldError
    assert state.last_seq > 0


def test_unassigned_transfer_is_cancelled_not_retried(tmp_path: Path) -> None:
    """§7.7 in the loop: a transfer the solver cannot place is cancelled at its
    own tick and recorded as a failed move — never left pending, where its lot
    claims and quantities would go stale."""
    from nodal.events import load_state as reload_state
    from nodal.sim.policies import make_policy
    from nodal.sim.runner import _Sim
    from nodal.worlds import load_world

    world = tmp_path / "w.yaml"
    world.write_text(
        """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: rack, capacity: { slots: 60 } }]
demand_rates:
  - { facility: F1, group: general, per_day: 1 }
lots:
  - { id: L1, zone: Z1, group: general, quantity: 20, size: { slots: 10 } }
  - { id: L2, zone: Z1, group: general, quantity: 20, size: { slots: 10 } }
""",
        encoding="utf-8",
    )
    # Stock 40 vs target 14 proposes a transfer; F1 is its origin (excluded) and
    # the only facility, so the solver can never place it.
    spec = tiny_spec().model_copy(update={"rebalance": True})
    with EventStore(tmp_path / "w.sqlite3") as store:
        load_world(world, store)
        sim = _Sim.__new__(_Sim)
        sim.spec = spec
        sim.config = spec.objective
        sim.store = store
        sim.state = load_state(store)
        sim.policy = make_policy("nodal-batch")
        sim.stats = RunStats(
            scenario="t", policy="nodal-batch", seed=1, horizon_days=1, profile="default"
        )
        sim.pending = []
        sim.batch_counter = 0
        sim._outcome_pos = {}
        now = sim.state.last_ts
        assert now is not None
        sim._solve_batch(now)
        assert sim.pending == []  # not retried
        assert not any(s.is_transfer for s in sim.state.shipments.values())  # cancelled
        assert [d.allocated for d in sim.stats.rebalancing] == [False]  # failure visible
        replayed = reload_state(store)  # and the cancel replays from scratch
        assert not any(s.is_transfer for s in replayed.shipments.values())


@pytest.mark.bench
def test_rebalance_scenario_end_to_end(tmp_path: Path) -> None:
    """Stage-4 acceptance for §7.7 in the loop: the rebalancing scenario actually
    exercises transfers (decisions recorded), completes, and leaves a log that
    replays end to end — the regression that once bricked worlds."""
    spec = load_scenario(SCENARIOS / "generic-rebalance.yaml")
    assert spec.rebalance
    stats = run_scenario(spec, "nodal-batch", tmp_path / "w.sqlite3")
    assert stats.rebalancing, "scenario produced no transfer decisions"
    report = compute_kpis(stats)
    assert report.outcome["shipments"] > 0
    with EventStore(tmp_path / "w.sqlite3") as store:
        state = load_state(store)
    assert state.last_seq > 0


def test_runner_reoptimizes_on_disruption(tmp_path: Path) -> None:
    """§7.6 in the loop, pinned at this seed: bookings exist when closures land
    (36h booking lead), re-optimization fires, and the whole run — supersedes
    included — is byte-deterministic and replayable."""
    from nodal.sim.scenarios import DemandSpec, DisruptionSpec, TopologySpec

    spec = ScenarioSpec(
        name="reopt-e2e",
        seed=3,
        horizon_days=10,
        topology=TopologySpec(
            facilities=6,
            zones_min=1,
            zones_max=2,
            slot_capacity_min=100,
            slot_capacity_max=200,
            lanes_nearest=2,
        ),
        demand=DemandSpec(shipments_per_day=5.0, booking_lead_hours=36.0),
        disruptions=DisruptionSpec(closures_per_30d=12.0, closure_days_min=1, closure_days_max=3),
    )
    assert generate(spec).disruption_plan, "seed must actually generate closures"
    stats_a = run_scenario(spec, "nodal-batch", tmp_path / "a.sqlite3")
    assert stats_a.reopt_tiers, "no re-optimization fired at the pinned seed"
    report = compute_kpis(stats_a)
    assert report.outcome["reopt_solves"] == len(stats_a.reopt_tiers)
    assert "closure_violations" in report.outcome
    with EventStore(tmp_path / "a.sqlite3") as store:
        state = load_state(store)  # supersede chains replay from scratch
    assert state.last_seq > 0
    stats_b = run_scenario(spec, "nodal-batch", tmp_path / "b.sqlite3")
    assert _log_fingerprint(tmp_path / "a.sqlite3") == _log_fingerprint(tmp_path / "b.sqlite3")
    assert compute_kpis(stats_a).outcome_json() == compute_kpis(stats_b).outcome_json()


def test_bench_rejects_duplicate_scenario_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate scenario names"):
        run_bench(
            [SCENARIOS / "ci-tiny.yaml", SCENARIOS / "ci-tiny.yaml"],
            ["nodal-single"],
            tmp_path,
        )


def test_bench_matrix_and_comparison(tmp_path: Path) -> None:
    policies = (
        "nodal-single",
        "nodal-batch",
        "flow-relax",
        "greedy",
        "nearest-feasible",
        "first-available",
    )
    reports = run_bench([SCENARIOS / "ci-tiny.yaml"], list(policies), tmp_path)
    assert len(reports) == len(policies)
    comparison = (tmp_path / "comparison.md").read_text(encoding="utf-8")
    for policy in policies:
        assert policy in comparison
        report_file = tmp_path / f"ci-tiny-{policy}.json"
        data = json.loads(report_file.read_text(encoding="utf-8"))
        assert data["outcome"]["shipments"] == reports[0].outcome["shipments"]
    assert all("infeasible_preferred" in r.outcome for r in reports)
