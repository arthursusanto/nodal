"""Event-sourced core: the append-only log is the only source of truth (§4)."""

from nodal.events.api import dump_state, ensure_snapshots, load_state, load_state_bytes
from nodal.events.envelope import Envelope, EventDraft, EventPayload
from nodal.events.fold import FoldError, apply_event, fold
from nodal.events.state import NetworkState
from nodal.events.store import EventStore, StoreError

__all__ = [
    "Envelope",
    "EventDraft",
    "EventPayload",
    "EventStore",
    "FoldError",
    "NetworkState",
    "StoreError",
    "apply_event",
    "dump_state",
    "ensure_snapshots",
    "fold",
    "load_state",
    "load_state_bytes",
]
