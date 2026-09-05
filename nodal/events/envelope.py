"""Event envelope and payload base (§4).

Determinism rules that live here: event ids derive from the sequence number
(`EVT-<seq>`), never from RNG; timestamps are timezone-aware UTC; payloads carry no
wall-clock measurements.
"""

from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)


class EventPayload(BaseModel):
    """Base class for all event payloads.

    Subclasses set `EVENT_TYPE` and implement `entity_ref()` naming the primary
    entity the event concerns (used for log indexing, not for fold dispatch).
    """

    model_config = ConfigDict(frozen=True)

    EVENT_TYPE: ClassVar[str] = ""

    def entity_ref(self) -> tuple[str, str]:
        raise NotImplementedError


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class EventDraft(BaseModel):
    """What a producer hands to the store: the store assigns seq and id."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    ts: datetime
    payload: EventPayload
    cause: str | None = None

    _utc_ts = field_validator("ts")(_require_utc)


class Envelope(BaseModel):
    """Round-trips faithfully: the payload serializes as its concrete type and is
    resolved back through the catalog registry by the envelope's `type` field."""

    model_config = ConfigDict(frozen=True)

    seq: int
    id: str
    ts: datetime
    type: str
    entity_type: str
    entity_id: str
    payload: EventPayload
    cause: str | None = None
    actor: str = "cli"

    _utc_ts = field_validator("ts")(_require_utc)

    @model_validator(mode="before")
    @classmethod
    def _resolve_payload(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("payload"), dict):
            from nodal.events.catalog import PAYLOAD_REGISTRY

            payload_cls = PAYLOAD_REGISTRY.get(str(data.get("type")))
            if payload_cls is None:
                raise ValueError(f"unknown event type {data.get('type')!r}")
            data = {**data, "payload": payload_cls.model_validate(data["payload"])}
        return data

    @field_serializer("payload")
    def _serialize_payload(self, payload: EventPayload) -> JsonValue:
        dumped: JsonValue = payload.model_dump(mode="json")
        return dumped
