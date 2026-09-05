"""The §4 headline property, Hypothesis-driven: for randomized valid event
sequences exercising every lifecycle, fold(snapshot + tail) == fold(all events) —
by full pydantic equality (private indexes included) and by serialized JSON.
"""

from datetime import datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import Disruption, DisruptionKind
from nodal.events import EventStore, ensure_snapshots, fold, load_state
from nodal.events import catalog as ev
from nodal.events.envelope import EventDraft
from nodal.worlds import load_world
from tests.conftest import FIXTURES
from tests.helpers import at, make_assignment, make_lot, make_reservation, make_shipment

ZONES = ["ZON-A1", "ZON-A2", "ZON-B1", "ZON-B2", "ZON-C1"]
FACILITIES = {
    "ZON-A1": "FAC-A",
    "ZON-A2": "FAC-A",
    "ZON-B1": "FAC-B",
    "ZON-B2": "FAC-B",
    "ZON-C1": "FAC-C",
}

OPS = st.lists(
    st.sampled_from(
        [
            "receive",
            "receive",
            "move",
            "adjust",
            "ship_cycle",
            "cancel_planned",
            "supersede",
            "disrupt",
            "end_disrupt",
            "capacity",
        ]
    ),
    min_size=5,
    max_size=30,
)


class _Interpreter:
    """Turns abstract ops into valid event batches against the fixture world."""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.n = 0
        self.lots: list[str] = ["LOT-1", "LOT-2", "LOT-3"]
        self.open_disruptions: list[str] = ["DIS-1"]

    def _ts(self) -> datetime:
        self.n += 1
        return at(1) + timedelta(hours=self.n)

    def _append(self, payloads: list[ev.EventPayload], ts: datetime) -> None:
        self.store.append([EventDraft(ts=ts, payload=p) for p in payloads])

    def run(self, op: str, pick: int) -> None:
        ts = self._ts()
        zone = ZONES[pick % len(ZONES)]
        if op == "receive":
            lot_id = f"LOT-H{self.n}"
            self._append(
                [ev.LotReceived(lot=make_lot(lot_id, zone, size=CapacityVector(slots=1)))], ts
            )
            self.lots.append(lot_id)
        elif op == "move":
            self._append(
                [ev.LotMoved(lot_id=self.lots[pick % len(self.lots)], to_zone_id=zone)], ts
            )
        elif op == "adjust":
            self._append(
                [
                    ev.LotQuantityAdjusted(
                        lot_id=self.lots[pick % len(self.lots)], new_quantity=pick % 50
                    )
                ],
                ts,
            )
        elif op == "ship_cycle":
            sid = f"SHP-H{self.n}"
            rid = f"RES-{sid}-1"
            eta = ts + timedelta(hours=2)
            departure = ts + timedelta(days=3)
            self._append([ev.ShipmentRegistered(shipment=make_shipment(sid, slots=3))], ts)
            self._append(
                [
                    ev.AllocationDecided(
                        shipment_id=sid,
                        assignment=make_assignment(
                            FACILITIES[zone],
                            zone,
                            eta=eta,
                            departure=departure,
                            reservation_id=rid,
                        ),
                        record={},
                    ),
                    ev.ReservationPlaced(
                        reservation=make_reservation(
                            rid,
                            zone,
                            sid,
                            size=CapacityVector(slots=3),
                            from_ts=eta,
                            until_ts=departure,
                        )
                    ),
                ],
                ts + timedelta(minutes=1),
            )
            self._append([ev.ShipmentDeparted(shipment_id=sid)], ts + timedelta(minutes=2))
            lot_id = f"LOT-{sid}"
            self._append(
                [
                    ev.LotReceived(
                        lot=make_lot(lot_id, zone, size=CapacityVector(slots=3), shipment_id=sid)
                    ),
                    ev.ShipmentArrived(shipment_id=sid),
                ],
                eta,
            )
            self.lots.append(lot_id)
            self.n += 3  # keep timestamps ahead of the eta we just used
        elif op == "cancel_planned":
            sid = f"SHP-H{self.n}"
            self._append([ev.ShipmentRegistered(shipment=make_shipment(sid))], ts)
            self._append(
                [ev.ShipmentCancelled(shipment_id=sid, reason="hyp")], ts + timedelta(minutes=1)
            )
        elif op == "supersede":
            sid = f"SHP-H{self.n}"
            rid = f"RES-{sid}-1"
            eta = ts + timedelta(hours=2)
            self._append([ev.ShipmentRegistered(shipment=make_shipment(sid))], ts)
            self._append(
                [
                    ev.AllocationDecided(
                        shipment_id=sid,
                        assignment=make_assignment(
                            FACILITIES[zone],
                            zone,
                            eta=eta,
                            departure=ts + timedelta(days=2),
                            reservation_id=rid,
                        ),
                        record={},
                    ),
                    ev.ReservationPlaced(
                        reservation=make_reservation(
                            rid,
                            zone,
                            sid,
                            size=CapacityVector(slots=2),
                            from_ts=eta,
                            until_ts=ts + timedelta(days=2),
                        )
                    ),
                ],
                ts + timedelta(minutes=1),
            )
            self._append(
                [ev.AllocationSuperseded(shipment_id=sid, old_decision_seq=1, reason="hyp")],
                ts + timedelta(minutes=2),
            )
        elif op == "disrupt":
            did = f"DIS-H{self.n}"
            self._append(
                [
                    ev.DisruptionStarted(
                        disruption=Disruption(
                            id=did,
                            kind=DisruptionKind.CAPACITY_REDUCED,
                            target_id=zone,
                            from_ts=ts,
                            until_ts=ts + timedelta(days=2),
                            magnitude=0.5,
                        )
                    )
                ],
                ts,
            )
            self.open_disruptions.append(did)
        elif op == "end_disrupt":
            if self.open_disruptions:
                did = self.open_disruptions.pop(0)
                self._append([ev.DisruptionEnded(disruption_id=did)], ts)
        elif op == "capacity":
            self._append([ev.CapacityAdjusted(zone_id=zone, capacity=CapacityVector(slots=64))], ts)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    ops=OPS, picks=st.lists(st.integers(min_value=0, max_value=10_000), min_size=30, max_size=30)
)
def test_fold_snapshot_tail_equivalence_randomized(ops: list[str], picks: list[int]) -> None:
    store = EventStore(":memory:")
    try:
        load_world(FIXTURES / "world_small.yaml", store)
        interpreter = _Interpreter(store)
        for i, op in enumerate(ops):
            interpreter.run(op, picks[i % len(picks)])
        ensure_snapshots(store, every=4)
        head = store.last_seq()
        reference = fold(store.read(1, head))
        via_snapshot = load_state(store)
        assert via_snapshot == reference
        assert via_snapshot.model_dump_json() == reference.model_dump_json()
        # And a mid-log probe through the snapshot path:
        probe = at(1) + timedelta(hours=max(1, interpreter.n // 2))
        upto = store.max_seq_at(probe)
        assert load_state(store, at=probe) == fold(store.read(1, upto))
    finally:
        store.close()
