"""State materialization: snapshots and `state_at` (§4)."""

from datetime import datetime

import zstandard

from nodal.events.fold import apply_event, fold
from nodal.events.state import SCHEMA_VERSION, NetworkState
from nodal.events.store import EventStore, StoreError

SNAPSHOT_EVERY_DEFAULT = 10_000


def dump_state(state: NetworkState) -> bytes:
    return zstandard.compress(state.model_dump_json().encode("utf-8"), 3)


def load_state_bytes(blob: bytes) -> NetworkState:
    state = NetworkState.model_validate_json(zstandard.decompress(blob))
    if state.schema_version != SCHEMA_VERSION:
        raise StoreError(
            f"snapshot schema {state.schema_version} != engine schema {SCHEMA_VERSION}"
        )
    return state


def load_state(store: EventStore, at: datetime | None = None) -> NetworkState:
    """State after the last event with ts <= `at` (or the whole log)."""
    upto = store.last_seq() if at is None else store.max_seq_at(at)
    snapshot = store.latest_snapshot(max_upto_seq=upto)
    if snapshot is None:
        state = NetworkState()
        from_seq = 1
    else:
        state = load_state_bytes(snapshot[1])
        from_seq = snapshot[0] + 1
    if upto >= from_seq:
        fold(store.read(from_seq, upto), into=state)
    return state


def ensure_snapshots(store: EventStore, every: int = SNAPSHOT_EVERY_DEFAULT) -> int:
    """Write any missing snapshots at multiples of `every`. Returns count written."""
    last = store.last_seq()
    snapshot = store.latest_snapshot()
    if snapshot is None:
        state = NetworkState()
        from_seq = 1
    else:
        state = load_state_bytes(snapshot[1])
        from_seq = snapshot[0] + 1
    written = 0
    for envelope in store.read(from_seq, last):
        apply_event(state, envelope)
        if envelope.seq % every == 0:
            store.write_snapshot(envelope.seq, envelope.ts, dump_state(state))
            written += 1
    return written
