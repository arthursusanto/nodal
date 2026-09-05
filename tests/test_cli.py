from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

import nodal
from nodal.cli import app
from nodal.events import EventStore, load_state

runner = CliRunner()

# A two-facility world with one open shipment and a stock surplus at F1
# (stock 40 vs target 14): `optimize --rebalance` proposes exactly one
# whole-lot transfer per run (surplus 26 fits one 20-unit lot).
OPTIMIZE_WORLD = """
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


def _loaded_db(tmp_path: Path) -> Path:
    world = tmp_path / "world.yaml"
    world.write_text(OPTIMIZE_WORLD, encoding="utf-8")
    db = tmp_path / "world.sqlite3"
    result = runner.invoke(app, ["world", "load", str(world), "--db", str(db)])
    assert result.exit_code == 0, result.output
    return db


def _last_seq(db: Path) -> int:
    with EventStore(db) as store:
        return store.last_seq()


def test_help_runs() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "logistics network optimizer" in result.output


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == nodal.__version__


def test_optimize_rebalance_dry_run_leaves_log_untouched(tmp_path: Path) -> None:
    """The one dry run that must NOT draft: its transfers were never registered,
    so a plan over them would be a proposal the log cannot execute (§7.5)."""
    db = _loaded_db(tmp_path)
    before = _last_seq(db)
    result = runner.invoke(app, ["optimize", "--db", str(db), "--rebalance"])
    assert result.exit_code == 0, result.output
    assert "batch BATCH-CLI" in result.output
    assert "preview only" in result.output  # a transfer was proposed, not registered
    assert "plan not drafted" in result.output
    assert "S1: F" in result.output
    assert _last_seq(db) == before


def test_optimize_drafts_the_plan_and_plan_discard_throws_it_away(tmp_path: Path) -> None:
    """CLI parity with the UI's plan review (§7.5): a plain `optimize` proposes
    without booking, and the proposal is an event — so it is still there on the
    next command, until `plan discard` says no."""
    db = _loaded_db(tmp_path)
    before = _last_seq(db)
    result = runner.invoke(app, ["optimize", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "drafted plan BATCH-CLI" in result.output
    assert _last_seq(db) == before + 1  # exactly the draft; nothing is booked
    with EventStore(db) as store:
        state = load_state(store)
    assert state.shipments["S1"].status.value == "planned"
    assert state.pending_plan is not None
    assert state.pending_plan.assigned == 1

    discarded = runner.invoke(app, ["plan", "discard", "--db", str(db), "--reason", "not now"])
    assert discarded.exit_code == 0, discarded.output
    assert "discarded plan BATCH-CLI" in discarded.output
    with EventStore(db) as store:
        assert load_state(store).pending_plan is None


def test_plan_discard_without_a_draft_fails(tmp_path: Path) -> None:
    db = _loaded_db(tmp_path)
    before = _last_seq(db)
    result = runner.invoke(app, ["plan", "discard", "--db", str(db)])
    assert result.exit_code == 1
    assert "no plan is drafted" in result.output
    assert _last_seq(db) == before


def test_optimize_commit_assigns(tmp_path: Path) -> None:
    db = _loaded_db(tmp_path)
    result = runner.invoke(app, ["optimize", "--db", str(db), "--commit"])
    assert result.exit_code == 0, result.output
    assert "committed" in result.output
    with EventStore(db) as store:
        state = load_state(store)
    assert state.shipments["S1"].status.value == "allocated"
    again = runner.invoke(app, ["optimize", "--db", str(db)])
    assert again.exit_code == 0
    assert "nothing to optimize" in again.output


def test_optimize_rebalance_rerun_never_collides(tmp_path: Path) -> None:
    """Regression: repeated --rebalance --commit runs once minted colliding
    transfer ids, leaving a permanently unreplayable log."""
    db = _loaded_db(tmp_path)
    first = runner.invoke(app, ["optimize", "--db", str(db), "--rebalance", "--commit"])
    assert first.exit_code == 0, first.output
    assert "proposed 1 rebalancing transfers" in first.output
    second = runner.invoke(app, ["optimize", "--db", str(db), "--rebalance", "--commit"])
    assert second.exit_code == 0, second.output
    assert "proposed 1 rebalancing transfers" in second.output
    with EventStore(db) as store:
        state = load_state(store)  # full replay; a duplicate id raises FoldError
    transfers = [s for s in state.shipments.values() if s.is_transfer]
    assert len(transfers) == 2
    # The second run must claim the OTHER lot — L1 is held by the live transfer.
    claimed = sorted(lot for t in transfers for lot in t.transfer_lot_ids)
    assert claimed == ["L1", "L2"]


DELIVERY_WORLD = """
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
  - { id: L12, from: F1, to: F2, km: 45, minutes: 60, cost_fixed: 40,
      path: [[-100.0, 40.0], [-100.25, 40.1], [-100.5, 40.0]] }
shipments:
  - id: S-DEL
    origin_label: "Gate"
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    destination: { label: "Customer", lat: 40.2, lon: -100.3 }
    hold_days: 3
    lines: [{ sku: X, group: general, quantity: 5, size: { slots: 5 } }]
"""


def test_cli_drives_a_delivery_end_to_end(tmp_path: Path) -> None:
    """§7.9 without the API: a world file declares destination/hold_days and lane
    display geometry, and `allocate`/`optimize` book the hold and the itinerary."""
    world = tmp_path / "delivery.yaml"
    world.write_text(DELIVERY_WORLD, encoding="utf-8")
    db = tmp_path / "delivery.sqlite3"
    loaded = runner.invoke(app, ["world", "load", str(world), "--db", str(db)])
    assert loaded.exit_code == 0, loaded.output

    with EventStore(db) as store:
        state = load_state(store)
    assert state.shipments["S-DEL"].hold_days == 3
    assert state.shipments["S-DEL"].destination is not None
    assert state.lanes["L12"].path == [(-100.0, 40.0), (-100.25, 40.1), (-100.5, 40.0)]

    shown = runner.invoke(app, ["allocate", "S-DEL", "--db", str(db)])
    assert shown.exit_code == 0, shown.output
    assert "itinerary to Customer" in shown.output
    assert "hold  F" in shown.output

    committed = runner.invoke(app, ["allocate", "S-DEL", "--db", str(db), "--commit"])
    assert committed.exit_code == 0, committed.output
    with EventStore(db) as store:
        after = load_state(store)
    shipment = after.shipments["S-DEL"]
    assert shipment.status.value == "allocated"
    assert shipment.assigned is not None
    held = after.active_reservations_of("S-DEL")
    assert len(held) == 1
    assert held[0].until_ts - held[0].from_ts == timedelta(days=3)


def _events(db: Path) -> list[tuple[int, str, str]]:
    with EventStore(db) as store:
        return [(e.seq, e.type, e.entity_id) for e in store.read()]


def test_optimize_commit_books_the_drafted_plan_under_its_own_id(tmp_path: Path) -> None:
    """The CLI commits through the same engine guard the API does (§7.5): with a
    plan drafted, `--commit` books THAT plan — same batch id, so the audit chain
    reads PlanDrafted -> BatchSolved — instead of minting a fresh unreviewed
    solve and leaving the draft dangling."""
    db = _loaded_db(tmp_path)
    drafted = runner.invoke(app, ["optimize", "--db", str(db)])
    assert drafted.exit_code == 0, drafted.output
    with EventStore(db) as store:
        pending = load_state(store).pending_plan
    assert pending is not None
    batch_id = pending.batch_id

    committed = runner.invoke(app, ["optimize", "--db", str(db), "--commit"])
    assert committed.exit_code == 0, committed.output
    assert "committed" in committed.output
    with EventStore(db) as store:
        state = load_state(store)
    assert state.pending_plan is None, "committing consumes the draft"
    assert state.shipments["S1"].status.value == "allocated"
    assert state.shipments["S1"].assigned is not None
    # One batch id across proposal and booking; no second, unreviewed batch.
    chain = [(t, e) for _, t, e in _events(db) if t in ("PlanDrafted", "BatchSolved")]
    assert chain == [("PlanDrafted", batch_id), ("BatchSolved", batch_id)]


def test_optimize_commit_books_exactly_what_the_draft_proposed(tmp_path: Path) -> None:
    db = _loaded_db(tmp_path)
    assert runner.invoke(app, ["optimize", "--db", str(db)]).exit_code == 0
    with EventStore(db) as store:
        pending = load_state(store).pending_plan
    assert pending is not None
    proposed = pending.records["S1"]["chosen"]
    assert runner.invoke(app, ["optimize", "--db", str(db), "--commit"]).exit_code == 0
    with EventStore(db) as store:
        assigned = load_state(store).shipments["S1"].assigned
    assert assigned is not None
    assert (assigned.facility_id, assigned.zone_ids[0]) == (
        proposed["facility_id"],
        proposed["zone_id"],
    )


def test_allocate_commit_terminates_the_draft_it_stales(tmp_path: Path) -> None:
    """Booking one shipment by hand supersedes a drafted batch. The fold already
    stopped treating it as pending; the log has to say so too, in the same
    atomic append, or the audit reads "proposed, and then nothing" (§7.5)."""
    db = _loaded_db(tmp_path)
    assert runner.invoke(app, ["optimize", "--db", str(db)]).exit_code == 0
    with EventStore(db) as store:
        pending = load_state(store).pending_plan
    assert pending is not None

    result = runner.invoke(app, ["allocate", "S1", "--db", str(db), "--commit"])
    assert result.exit_code == 0, result.output
    rows = _events(db)
    discard = next(r for r in rows if r[1] == "PlanDiscarded")
    assert discard[2] == pending.batch_id
    following = next(r for r in rows if r[0] == discard[0] + 1)
    assert following[1] == "AllocationDecided"  # one append, adjacent seqs
    with EventStore(db) as store:
        payload = next(e.payload for e in store.read() if e.type == "PlanDiscarded")
        assert payload.reason == "superseded by AllocationDecided"
        assert load_state(store).pending_plan is None


def test_rebalance_commit_terminates_the_draft_it_stales(tmp_path: Path) -> None:
    db = _loaded_db(tmp_path)
    assert runner.invoke(app, ["optimize", "--db", str(db)]).exit_code == 0
    with EventStore(db) as store:
        pending = load_state(store).pending_plan
    assert pending is not None

    result = runner.invoke(app, ["optimize", "--db", str(db), "--rebalance", "--commit"])
    assert result.exit_code == 0, result.output
    rows = _events(db)
    discard = next(r for r in rows if r[1] == "PlanDiscarded")
    assert discard[2] == pending.batch_id
    assert next(r for r in rows if r[0] == discard[0] + 1)[1] == "TransferOrdered"
    # The transfers staled the draft, so the commit that follows is a NEW batch.
    solved = [e for _, t, e in rows if t == "BatchSolved"]
    assert solved and pending.batch_id not in solved
