"""What-if forks (§4) — stage 5 acceptance: all four framing categories, and
the baseline log stays byte-identical through every run."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from nodal.allocate import ObjectiveConfig
from nodal.allocate.engine import allocate, commit
from nodal.cli import app
from nodal.events import EventStore, load_state
from nodal.whatif import WhatIfError, run_whatif

CORE = ObjectiveConfig(packs=["core"])


def _fingerprint(store: EventStore) -> list[tuple[int, str, str, str]]:
    return [(e.seq, e.ts.isoformat(), e.type, e.payload.model_dump_json()) for e in store.read()]


def test_closure_whatif_diffs_and_leaves_baseline_untouched(world_store: EventStore) -> None:
    before = _fingerprint(world_store)
    report = run_whatif(world_store, CORE, "nodal-batch", ["close FAC-A 7d"])
    assert _fingerprint(world_store) == before  # byte-identical baseline
    # SHP-2 needs cold storage, which only FAC-A has: closing it strands SHP-2.
    baseline_pair = report.baseline.assignments["SHP-2"]
    assert baseline_pair is not None and baseline_pair[0] == "FAC-A"
    assert report.fork.assignments["SHP-2"] is None
    assert any(c.shipment_id == "SHP-2" and c.after is None for c in report.changes)
    assert "SHP-2" in report.render()


def test_closure_whatif_names_the_cargo_it_would_strand(world_store: EventStore) -> None:
    """A hypothesis whose entire reach is cargo it strands re-optimizes nothing —
    tier 0 — and the report used to drop tier-0 results, so the preview was
    silent about the one thing a closure cannot re-route. SHP-2's goods are
    inside FAC-A, so closing it must come back naming them."""
    report = run_whatif(world_store, CORE, "nodal-batch", ["close FAC-A 7d"])
    assert [r.tier for r in report.reopts] == [0]
    assert [
        (e.shipment_id, e.facility_id, e.disruption_id) for r in report.reopts for e in r.trapped
    ] == [("SHP-2", "FAC-A", "WHATIF-1")]


def test_capacity_cut_whatif(world_store: EventStore) -> None:
    before = _fingerprint(world_store)
    report = run_whatif(world_store, CORE, "nodal-batch", ["cut ZON-A2 0.9 7d"])
    assert _fingerprint(world_store) == before
    # The cold zone shrinks to 4 slots; SHP-2 (8 slots) no longer fits anywhere.
    assert report.fork.assignments["SHP-2"] is None
    assert any(c.shipment_id == "SHP-2" for c in report.changes)


def test_delay_whatif_magnitude_matters(world_store: EventStore) -> None:
    """A delay must be REAL: a small readiness slip re-examines the booking and
    keeps it; a slip past the deadline strands it. The two magnitudes must
    produce different forks."""
    state = load_state(world_store)
    assert state.last_ts is not None
    record = allocate(state, "SHP-2", CORE, state.last_ts)
    assert record.chosen is not None
    commit(world_store, record)
    before = _fingerprint(world_store)

    small = run_whatif(world_store, CORE, "nodal-batch", ["delay SHP-2 1h"])
    assert _fingerprint(world_store) == before
    assert [r.affected for r in small.reopts] == [["SHP-2"]]  # re-examined...
    assert not any(c.shipment_id == "SHP-2" for c in small.changes)  # ...and kept

    big = run_whatif(world_store, CORE, "nodal-batch", ["delay SHP-2 96h"])
    assert _fingerprint(world_store) == before
    # Readiness now sits past the 09-05 deadline: the booking cannot be kept.
    assert big.fork.assignments["SHP-2"] is None
    assert any(c.shipment_id == "SHP-2" and c.after is None for c in big.changes)


def test_delay_whatif_rejects_departed_shipments(world_store: EventStore) -> None:
    from nodal.events import catalog as ev
    from nodal.events.envelope import EventDraft

    state = load_state(world_store)
    assert state.last_ts is not None
    record = allocate(state, "SHP-2", CORE, state.last_ts)
    assert record.chosen is not None
    commit(world_store, record)
    world_store.append(
        [EventDraft(ts=state.last_ts, payload=ev.ShipmentDeparted(shipment_id="SHP-2"))]
    )
    with pytest.raises(WhatIfError, match="already departed"):
        run_whatif(world_store, CORE, "nodal-batch", ["delay SHP-2 24h"])


def test_demand_spike_whatif(world_store: EventStore) -> None:
    before = _fingerprint(world_store)
    report = run_whatif(world_store, CORE, "nodal-batch", ["spike 4 10"])
    assert _fingerprint(world_store) == before
    spiked = [c for c in report.changes if c.shipment_id.startswith("WHATIF-SPIKE-")]
    assert len(spiked) == 4  # absent in baseline, decided in the fork
    assert report.fork.allocated >= report.baseline.allocated


def test_whatif_rejects_nonsense(world_store: EventStore) -> None:
    with pytest.raises(WhatIfError, match="cannot parse"):
        run_whatif(world_store, CORE, "nodal-batch", ["explode everything"])
    with pytest.raises(WhatIfError, match="unknown facility"):
        run_whatif(world_store, CORE, "nodal-batch", ["close FAC-NOPE 7d"])


def test_whatif_cli(tmp_path: Path) -> None:
    from nodal.worlds import load_world
    from tests.conftest import FIXTURES

    db = tmp_path / "w.sqlite3"
    with EventStore(db) as store:
        load_world(FIXTURES / "world_small.yaml", store)
        before = _fingerprint(store)
    runner = CliRunner()
    result = runner.invoke(app, ["whatif", "--db", str(db), "--event", "close FAC-A 7d"])
    assert result.exit_code == 0, result.output
    assert "baseline log untouched" in result.output
    assert "changed decisions" in result.output
    with EventStore(db) as store:
        assert _fingerprint(store) == before
