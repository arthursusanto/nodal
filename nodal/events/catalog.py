"""The event catalog (§4).

Every state mutation in Nodal is one of these events. Decision events carry their
audit record as opaque JSON at this layer — the fold applies the assignment and never
interprets the record, which keeps `events` independent of `allocate` (§1).
"""

from datetime import datetime
from typing import ClassVar

from pydantic import Field, JsonValue

from nodal.domain.calendar import OperatingCalendar
from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import (
    Assignment,
    Certification,
    Disruption,
    Facility,
    InventoryLot,
    Lane,
    Reservation,
    Shipment,
    StorageZone,
)
from nodal.events.envelope import EventPayload

# --- master data ---------------------------------------------------------------


class FacilityRegistered(EventPayload):
    EVENT_TYPE: ClassVar[str] = "FacilityRegistered"
    facility: Facility

    def entity_ref(self) -> tuple[str, str]:
        return ("facility", self.facility.id)


class FacilityUpdated(EventPayload):
    """Partial update; only non-None fields apply."""

    EVENT_TYPE: ClassVar[str] = "FacilityUpdated"
    facility_id: str
    name: str | None = None
    risk_factor: float | None = None
    tags: list[str] | None = None

    def entity_ref(self) -> tuple[str, str]:
        return ("facility", self.facility_id)


class ZoneRegistered(EventPayload):
    EVENT_TYPE: ClassVar[str] = "ZoneRegistered"
    zone: StorageZone

    def entity_ref(self) -> tuple[str, str]:
        return ("zone", self.zone.id)


class ZoneUpdated(EventPayload):
    EVENT_TYPE: ClassVar[str] = "ZoneUpdated"
    zone_id: str
    kind: str | None = None
    temp_c: tuple[int, int] | None = None
    allowed_classes: list[str] | None = None

    def entity_ref(self) -> tuple[str, str]:
        return ("zone", self.zone_id)


class LaneRegistered(EventPayload):
    EVENT_TYPE: ClassVar[str] = "LaneRegistered"
    lane: Lane

    def entity_ref(self) -> tuple[str, str]:
        return ("lane", self.lane.id)


class LaneUpdated(EventPayload):
    EVENT_TYPE: ClassVar[str] = "LaneUpdated"
    lane_id: str
    distance_km: float | None = None
    minutes: int | None = None
    cost_fixed_cents: int | None = None
    cost_per_kg_cents: float | None = None
    cost_per_m3_cents: float | None = None

    def entity_ref(self) -> tuple[str, str]:
        return ("lane", self.lane_id)


class EquipmentCountSet(EventPayload):
    EVENT_TYPE: ClassVar[str] = "EquipmentCountSet"
    facility_id: str
    tag: str
    count: int

    def entity_ref(self) -> tuple[str, str]:
        return ("facility", self.facility_id)


class CertificationSet(EventPayload):
    """Replaces any existing certification with the same tag."""

    EVENT_TYPE: ClassVar[str] = "CertificationSet"
    facility_id: str
    certification: Certification

    def entity_ref(self) -> tuple[str, str]:
        return ("facility", self.facility_id)


class OperatingCalendarSet(EventPayload):
    EVENT_TYPE: ClassVar[str] = "OperatingCalendarSet"
    facility_id: str
    calendar: OperatingCalendar | None

    def entity_ref(self) -> tuple[str, str]:
        return ("facility", self.facility_id)


class DemandRateSet(EventPayload):
    EVENT_TYPE: ClassVar[str] = "DemandRateSet"
    facility_id: str
    commodity_group: str
    per_day: int

    def entity_ref(self) -> tuple[str, str]:
        return ("facility", self.facility_id)


# --- inventory -----------------------------------------------------------------


class LotReceived(EventPayload):
    """A lot materializes in a zone. `lot.zone_id` must be set."""

    EVENT_TYPE: ClassVar[str] = "LotReceived"
    lot: InventoryLot

    def entity_ref(self) -> tuple[str, str]:
        return ("lot", self.lot.id)


class LotQuantityAdjusted(EventPayload):
    EVENT_TYPE: ClassVar[str] = "LotQuantityAdjusted"
    lot_id: str
    new_quantity: int
    new_size: CapacityVector | None = None
    reason: str = ""

    def entity_ref(self) -> tuple[str, str]:
        return ("lot", self.lot_id)


class LotMoved(EventPayload):
    """Places a lot into a zone — including a lot arriving aboard a transfer
    shipment (its aboard-status clears). Optionally refreshes planned departure."""

    EVENT_TYPE: ClassVar[str] = "LotMoved"
    lot_id: str
    to_zone_id: str
    planned_departure: datetime | None = None

    def entity_ref(self) -> tuple[str, str]:
        return ("lot", self.lot_id)


class LotShipped(EventPayload):
    """Lot leaves its zone aboard a shipment (outbound or transfer pickup)."""

    EVENT_TYPE: ClassVar[str] = "LotShipped"
    lot_id: str
    shipment_id: str

    def entity_ref(self) -> tuple[str, str]:
        return ("lot", self.lot_id)


# --- shipments -----------------------------------------------------------------


class ShipmentRegistered(EventPayload):
    EVENT_TYPE: ClassVar[str] = "ShipmentRegistered"
    shipment: Shipment

    def entity_ref(self) -> tuple[str, str]:
        return ("shipment", self.shipment.id)


class TransferOrdered(EventPayload):
    """A rebalancing transfer is an ordinary shipment with `is_transfer=True` (§7.7)."""

    EVENT_TYPE: ClassVar[str] = "TransferOrdered"
    shipment: Shipment

    def entity_ref(self) -> tuple[str, str]:
        return ("shipment", self.shipment.id)


class ShipmentDeparted(EventPayload):
    EVENT_TYPE: ClassVar[str] = "ShipmentDeparted"
    shipment_id: str

    def entity_ref(self) -> tuple[str, str]:
        return ("shipment", self.shipment_id)


class ShipmentDelayed(EventPayload):
    """In-transit arrival slip: the shipment is now expected at `new_eta`."""

    EVENT_TYPE: ClassVar[str] = "ShipmentDelayed"
    shipment_id: str
    new_eta: datetime
    reason: str = ""

    def entity_ref(self) -> tuple[str, str]:
        return ("shipment", self.shipment_id)


class ShipmentReadyChanged(EventPayload):
    """Pre-departure readiness slip (or advance). The planner reads `ready_at`,
    so this is the event that makes a delay REAL for planned and allocated
    bookings (§7.6, what-if) — a departed shipment's readiness is history."""

    EVENT_TYPE: ClassVar[str] = "ShipmentReadyChanged"
    shipment_id: str
    new_ready: datetime
    reason: str = ""

    def entity_ref(self) -> tuple[str, str]:
        return ("shipment", self.shipment_id)


class ShipmentArrived(EventPayload):
    """Arrival at the assigned facility. The fold releases the shipment's
    reservations (its lots arrive as `LotReceived` events in the same batch, so
    goods are never counted twice — §5)."""

    EVENT_TYPE: ClassVar[str] = "ShipmentArrived"
    shipment_id: str

    def entity_ref(self) -> tuple[str, str]:
        return ("shipment", self.shipment_id)


class ShipmentCancelled(EventPayload):
    EVENT_TYPE: ClassVar[str] = "ShipmentCancelled"
    shipment_id: str
    reason: str = ""

    def entity_ref(self) -> tuple[str, str]:
        return ("shipment", self.shipment_id)


# --- capacity & disruption ------------------------------------------------------


class CapacityAdjusted(EventPayload):
    """Absolute replacement of a zone's base capacity vector."""

    EVENT_TYPE: ClassVar[str] = "CapacityAdjusted"
    zone_id: str
    capacity: CapacityVector

    def entity_ref(self) -> tuple[str, str]:
        return ("zone", self.zone_id)


class DisruptionStarted(EventPayload):
    EVENT_TYPE: ClassVar[str] = "DisruptionStarted"
    disruption: Disruption

    def entity_ref(self) -> tuple[str, str]:
        return ("disruption", self.disruption.id)


class DisruptionEnded(EventPayload):
    EVENT_TYPE: ClassVar[str] = "DisruptionEnded"
    disruption_id: str

    def entity_ref(self) -> tuple[str, str]:
        return ("disruption", self.disruption_id)


# --- decisions ------------------------------------------------------------------


class ReservationPlaced(EventPayload):
    EVENT_TYPE: ClassVar[str] = "ReservationPlaced"
    reservation: Reservation

    def entity_ref(self) -> tuple[str, str]:
        return ("reservation", self.reservation.id)


class ReservationReleased(EventPayload):
    """Manual/corrective release. Arrival, cancellation, and supersession release
    a shipment's reservations automatically in the fold."""

    EVENT_TYPE: ClassVar[str] = "ReservationReleased"
    reservation_id: str

    def entity_ref(self) -> tuple[str, str]:
        return ("reservation", self.reservation_id)


class AllocationDecided(EventPayload):
    """Applies the assignment; `record` is the full decision record (§7.5), opaque
    to the fold and owned by `nodal.allocate`."""

    EVENT_TYPE: ClassVar[str] = "AllocationDecided"
    shipment_id: str
    assignment: Assignment
    record: JsonValue = None

    def entity_ref(self) -> tuple[str, str]:
        return ("shipment", self.shipment_id)


class AllocationSuperseded(EventPayload):
    """Re-optimization audit link (§7.6). The fold clears the shipment's assignment
    and releases its reservations; a new `AllocationDecided` usually follows in the
    same batch."""

    EVENT_TYPE: ClassVar[str] = "AllocationSuperseded"
    shipment_id: str
    old_decision_seq: int
    reason: str = ""

    def entity_ref(self) -> tuple[str, str]:
        return ("shipment", self.shipment_id)


class BatchSolved(EventPayload):
    """Solver metadata for one batch solve; carries no state mutation."""

    EVENT_TYPE: ClassVar[str] = "BatchSolved"
    batch_id: str
    meta: JsonValue = None

    def entity_ref(self) -> tuple[str, str]:
        return ("batch", self.batch_id)


class PlanDrafted(EventPayload):
    """A batch plan the optimizer proposed and nobody has booked (§7.5).

    A proposal made at time T is an auditable fact, so it lives in the log like
    every other one: `records` is exactly what a commit would embed, per
    shipment, assigned and unassigned alike. The fold keeps it PENDING only
    while it is the log head — see `fold.apply_event` — because a plan is only
    reviewable against the world it was solved against."""

    EVENT_TYPE: ClassVar[str] = "PlanDrafted"
    batch_id: str
    based_on_seq: int  # the log head the solve read; a commit hands it back
    meta: JsonValue = None  # solver metadata, as BatchSolved carries it
    records: dict[str, JsonValue] = Field(default_factory=dict)  # shipment id -> record
    assigned: int = 0
    unassigned: int = 0

    def entity_ref(self) -> tuple[str, str]:
        return ("batch", self.batch_id)


class PlanDiscarded(EventPayload):
    """The operator threw a drafted plan away (§7.5). Nothing to undo — the
    draft booked nothing — but the review that ended in NO is history too."""

    EVENT_TYPE: ClassVar[str] = "PlanDiscarded"
    batch_id: str
    reason: str = ""

    def entity_ref(self) -> tuple[str, str]:
        return ("batch", self.batch_id)


# --- registry -------------------------------------------------------------------

ALL_PAYLOADS: tuple[type[EventPayload], ...] = (
    FacilityRegistered,
    FacilityUpdated,
    ZoneRegistered,
    ZoneUpdated,
    LaneRegistered,
    LaneUpdated,
    EquipmentCountSet,
    CertificationSet,
    OperatingCalendarSet,
    DemandRateSet,
    LotReceived,
    LotQuantityAdjusted,
    LotMoved,
    LotShipped,
    ShipmentRegistered,
    TransferOrdered,
    ShipmentDeparted,
    ShipmentDelayed,
    ShipmentReadyChanged,
    ShipmentArrived,
    ShipmentCancelled,
    CapacityAdjusted,
    DisruptionStarted,
    DisruptionEnded,
    ReservationPlaced,
    ReservationReleased,
    AllocationDecided,
    AllocationSuperseded,
    BatchSolved,
    PlanDrafted,
    PlanDiscarded,
)

PAYLOAD_REGISTRY: dict[str, type[EventPayload]] = {p.EVENT_TYPE: p for p in ALL_PAYLOADS}
