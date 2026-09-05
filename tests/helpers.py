"""Shared event-construction helpers for tests."""

from datetime import UTC, datetime, timedelta

from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import (
    Assignment,
    InventoryLot,
    RequirementSet,
    Reservation,
    Shipment,
)
from nodal.events import catalog as ev
from nodal.events.envelope import EventDraft

BASE = datetime(2026, 9, 1, tzinfo=UTC)


def at(days: float = 0, hours: float = 0, minutes: float = 0) -> datetime:
    return BASE + timedelta(days=days, hours=hours, minutes=minutes)


def draft(payload: ev.EventPayload, ts: datetime) -> EventDraft:
    return EventDraft(ts=ts, payload=payload)


def make_shipment(shipment_id: str, *, slots: int = 10, ready: datetime | None = None) -> Shipment:
    return Shipment(
        id=shipment_id,
        origin_label="gate",
        origin_lat=41.95,
        origin_lon=-87.65,
        requirements=RequirementSet(size=CapacityVector(slots=slots), required_tags=[]),
        ready_at=ready or BASE,
    )


def make_assignment(
    facility_id: str,
    zone_id: str,
    *,
    eta: datetime,
    departure: datetime,
    reservation_id: str,
    route: list[str] | None = None,
) -> Assignment:
    return Assignment(
        facility_id=facility_id,
        zone_ids=[zone_id],
        route=route or [],
        eta=eta,
        expected_departure=departure,
        reservation_ids=[reservation_id],
    )


def make_reservation(
    reservation_id: str,
    zone_id: str,
    holder: str,
    *,
    size: CapacityVector,
    from_ts: datetime,
    until_ts: datetime,
) -> Reservation:
    return Reservation(
        id=reservation_id,
        zone_id=zone_id,
        size=size,
        from_ts=from_ts,
        until_ts=until_ts,
        holder=holder,
    )


def make_lot(
    lot_id: str,
    zone_id: str,
    *,
    size: CapacityVector,
    group: str = "general",
    quantity: int = 1,
    shipment_id: str | None = None,
    planned_departure: datetime | None = None,
) -> InventoryLot:
    return InventoryLot(
        id=lot_id,
        sku="SKU",
        commodity_group=group,
        quantity=quantity,
        size=size,
        zone_id=zone_id,
        shipment_id=shipment_id,
        planned_departure=planned_departure,
    )
