from pathlib import Path

import pytest
from typer.testing import CliRunner

from nodal.cli import app
from nodal.events import EventStore, NetworkState
from nodal.worlds import WorldError, load_world
from tests.conftest import FIXTURES

runner = CliRunner()


def test_loader_builds_expected_world(world_state: NetworkState) -> None:
    assert set(world_state.facilities) == {"FAC-A", "FAC-B", "FAC-C"}
    assert len(world_state.zones) == 5
    assert len(world_state.lanes) == 4
    assert set(world_state.lots) == {"LOT-1", "LOT-2", "LOT-3"}
    assert set(world_state.shipments) == {"SHP-1", "SHP-2"}
    assert set(world_state.disruptions) == {"DIS-1"}
    # Units converted at ingestion: 200000 kg -> grams.
    assert world_state.zones["ZON-A1"].capacity.weight_g == 200_000_000
    # Requirements defaulted from lines when size omitted.
    assert world_state.shipments["SHP-1"].requirements.size.slots == 15
    assert world_state.shipments["SHP-1"].requirements.deadline is not None
    # Certification default valid_from = world start.
    assert world_state.facilities["FAC-C"].certified_for(
        "cert:organic", world_state.facilities["FAC-C"].certifications[0].valid_from
    )


def test_loader_rejects_unknown_dimension(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "start: 2026-09-01T00:00:00+00:00\n"
        "facilities:\n"
        "  - {id: F, lat: 0, lon: 0, zones: [{id: Z, capacity: {pallets: 3}}]}\n",
        encoding="utf-8",
    )
    with EventStore(tmp_path / "w.sqlite3") as store, pytest.raises(WorldError, match="pallets"):
        load_world(bad, store)


def test_cli_world_load_and_state(tmp_path: Path) -> None:
    db = tmp_path / "world.sqlite3"
    loaded = runner.invoke(
        app, ["world", "load", str(FIXTURES / "world_small.yaml"), "--db", str(db)]
    )
    assert loaded.exit_code == 0, loaded.output
    assert "loaded" in loaded.output

    shown = runner.invoke(app, ["state", "--db", str(db)])
    assert shown.exit_code == 0, shown.output
    assert "FAC-A" in shown.output
    assert "shipments: planned=2" in shown.output

    historical = runner.invoke(app, ["state", "--db", str(db), "--at", "2026-09-01T00:30:00+00:00"])
    assert historical.exit_code == 0, historical.output
    assert "planned" not in historical.output  # shipments registered later


def test_cli_allocate_json_and_commit(tmp_path: Path) -> None:
    from nodal.allocate.records import DecisionRecord

    db = tmp_path / "world.sqlite3"
    runner.invoke(app, ["world", "load", str(FIXTURES / "world_small.yaml"), "--db", str(db)])

    shown = runner.invoke(app, ["allocate", "SHP-1", "--db", str(db), "--json"])
    assert shown.exit_code == 0, shown.output
    record = DecisionRecord.model_validate_json(shown.output)
    assert record.chosen is not None

    committed = runner.invoke(app, ["allocate", "SHP-1", "--db", str(db), "--commit"])
    assert committed.exit_code == 0, committed.output
    assert "committed 2 events" in committed.output

    after = runner.invoke(app, ["state", "--db", str(db)])
    assert "allocated=1" in after.output
