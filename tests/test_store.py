from pathlib import Path

import pytest

from nodal.domain.capacity import CapacityVector
from nodal.events import EventStore, StoreError
from nodal.events import catalog as ev
from tests.helpers import at, draft, make_shipment


def test_append_assigns_sequential_ids(tmp_path: Path) -> None:
    with EventStore(tmp_path / "log.sqlite3") as store:
        envelopes = store.append(
            [
                draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-A")), at(0)),
                draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-B")), at(0, 1)),
            ]
        )
        assert [e.seq for e in envelopes] == [1, 2]
        assert [e.id for e in envelopes] == ["EVT-1", "EVT-2"]
        assert envelopes[0].entity_type == "shipment"
        assert envelopes[0].entity_id == "SHP-A"


def test_append_rejects_non_monotonic_ts(tmp_path: Path) -> None:
    with EventStore(tmp_path / "log.sqlite3") as store:
        store.append([draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-A")), at(1))])
        with pytest.raises(StoreError, match="non-monotonic"):
            store.append([draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-B")), at(0))])
        # The failed batch must not have been partially applied.
        assert store.last_seq() == 1


def test_read_roundtrips_payloads(tmp_path: Path) -> None:
    with EventStore(tmp_path / "log.sqlite3") as store:
        payload = ev.CapacityAdjusted(
            zone_id="ZON-X", capacity=CapacityVector(slots=5, volume_l=None, weight_g=123)
        )
        store.append([draft(payload, at(0))], actor="test")
        stored = list(store.read())
        assert len(stored) == 1
        assert stored[0].payload == payload
        assert stored[0].actor == "test"
        assert stored[0].ts == at(0)


def test_envelope_json_roundtrip(tmp_path: Path) -> None:
    """Envelope serialization keeps the concrete payload (§4): the obvious way to
    export an event log must not silently drop payload fields."""
    from nodal.events import Envelope

    with EventStore(tmp_path / "log.sqlite3") as store:
        payload = ev.CapacityAdjusted(zone_id="ZON-X", capacity=CapacityVector(slots=7))
        (envelope,) = store.append([draft(payload, at(0))])
    restored = Envelope.model_validate_json(envelope.model_dump_json())
    assert restored == envelope
    assert isinstance(restored.payload, ev.CapacityAdjusted)
    assert restored.payload.capacity.slots == 7


def test_naive_timestamp_rejected_on_queries(tmp_path: Path) -> None:
    from datetime import datetime

    with EventStore(tmp_path / "log.sqlite3") as store:
        store.append([draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-A")), at(0))])
        with pytest.raises(ValueError, match="timezone-aware"):
            store.max_seq_at(datetime(2026, 9, 1, 12, 0))


def test_max_seq_at(tmp_path: Path) -> None:
    with EventStore(tmp_path / "log.sqlite3") as store:
        store.append(
            [
                draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-A")), at(0)),
                draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-B")), at(2)),
            ]
        )
        assert store.max_seq_at(at(-1)) == 0
        assert store.max_seq_at(at(0)) == 1  # boundary is inclusive
        assert store.max_seq_at(at(1)) == 1
        assert store.max_seq_at(at(3)) == 2
