"""Domain entities (§3).

Neutrality mechanism: the core knows tags, typed attributes, compatibility classes, and
commodity groups — never what they mean. Determinism note: collections that would be sets
are sorted lists, so state serialization (snapshots, decision records) is byte-stable.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from nodal.domain.calendar import OperatingCalendar
from nodal.domain.capacity import CapacityVector

AttributeBag = dict[str, JsonValue]
TagList = list[str]


def _sorted_unique(values: list[str]) -> list[str]:
    return sorted(set(values))


class Certification(BaseModel):
    model_config = ConfigDict(frozen=True)

    tag: str
    valid_from: datetime
    valid_until: datetime


class Facility(BaseModel):
    id: str
    name: str
    lat: float
    lon: float
    tz: str = "UTC"
    tags: TagList = Field(default_factory=list)
    equipment: dict[str, int] = Field(default_factory=dict)  # equipment tag -> unit count
    certifications: list[Certification] = Field(default_factory=list)
    calendar: OperatingCalendar | None = None  # None = open 24/7
    risk_factor: float = Field(default=0.0, ge=0.0, le=1.0)

    _dedupe_tags = field_validator("tags")(_sorted_unique)

    def certified_for(self, tag: str, at: datetime) -> bool:
        return any(c.tag == tag and c.valid_from <= at < c.valid_until for c in self.certifications)


def _ordered_band(value: tuple[int, int] | None) -> tuple[int, int] | None:
    """An inverted temperature band would make the TEMP_RANGE comparison
    vacuously true everywhere — reject it at construction, every ingestion
    path included."""
    if value is not None and value[0] > value[1]:
        raise ValueError(f"temperature band {value} is inverted (low > high)")
    return value


class StorageZone(BaseModel):
    id: str
    facility_id: str
    kind: str  # "rack", "bulk", "tank", "yard", "cold", ... — opaque to the core
    capacity: CapacityVector
    temp_c: tuple[int, int] | None = None  # holdable range, inclusive
    allowed_classes: TagList = Field(default_factory=list)  # classes allowed; empty = any
    attributes: AttributeBag = Field(default_factory=dict)

    _dedupe_classes = field_validator("allowed_classes")(_sorted_unique)
    _ordered_temp = field_validator("temp_c")(_ordered_band)


class LotSpec(BaseModel):
    """Goods description carried by a shipment line, before lots exist."""

    model_config = ConfigDict(frozen=True)

    sku: str
    commodity_group: str
    quantity: int = Field(gt=0)
    uom: str = "unit"
    size: CapacityVector  # footprint of the whole line
    compat_class: str | None = None
    attributes: AttributeBag = Field(default_factory=dict)


class InventoryLot(BaseModel):
    id: str
    sku: str
    commodity_group: str
    quantity: int = Field(ge=0)
    uom: str = "unit"
    size: CapacityVector
    compat_class: str | None = None
    attributes: AttributeBag = Field(default_factory=dict)
    zone_id: str | None = None  # None while aboard a shipment
    shipment_id: str | None = None
    received_at: datetime | None = None
    planned_departure: datetime | None = None  # None = occupies to end of horizon (§5)


class Reservation(BaseModel):
    id: str
    zone_id: str
    size: CapacityVector
    from_ts: datetime
    until_ts: datetime
    holder: str  # shipment id
    released_at: datetime | None = None  # set on consumption/cancellation

    def active_during(self, start: datetime, end: datetime) -> bool:
        if self.released_at is not None:
            return False
        return self.from_ts < end and start < self.until_ts


class RequirementSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    size: CapacityVector
    required_tags: TagList = Field(default_factory=list)
    zone_kinds: TagList = Field(default_factory=list)  # acceptable zone kinds; empty = any
    temp_c: tuple[int, int] | None = None  # required holding range
    compat_class: str | None = None
    deadline: datetime | None = None
    dwell_days: int | None = None  # expected days stored at destination
    attributes: AttributeBag = Field(default_factory=dict)

    _dedupe_tags = field_validator("required_tags")(_sorted_unique)
    _dedupe_kinds = field_validator("zone_kinds")(_sorted_unique)
    _ordered_temp = field_validator("temp_c")(_ordered_band)


class ShipmentStatus(StrEnum):
    PLANNED = "planned"
    ALLOCATED = "allocated"
    IN_TRANSIT = "in_transit"
    ARRIVED = "arrived"
    CANCELLED = "cancelled"


class Destination(BaseModel):
    """A customer point OUTSIDE the network — the B of an A->B delivery (§7.9).
    Never a facility: nothing is stored there and no lane reaches it."""

    model_config = ConfigDict(frozen=True)

    label: str
    lat: float
    lon: float


class StopRole(StrEnum):
    """Why a delivery touches a facility (§7.9)."""

    ENTRY = "entry"  # first facility the goods reach
    TRANSIT = "transit"  # an intermediate hop, in or out
    HOLD = "hold"  # where the goods are stored for the customer's hold_days
    EXIT = "exit"  # the facility the last-mile leg departs from


class Stop(BaseModel):
    """One scheduled dwell at a facility (§7.9). Every facility a delivery
    touches is a stop with an explicit window: the hold dwells for `hold_days`,
    a pass-through stop dwells for the handling time the journey already prices
    into the leg it departs on. Windows are what closures, equipment outages and
    staging capacity are matched against."""

    model_config = ConfigDict(frozen=True)

    facility_id: str
    role: StopRole
    arrive: datetime
    depart: datetime
    zone_id: str | None = None  # staging/holding zone; None until one is chosen

    @property
    def books_staging(self) -> bool:
        """A pass-through stop books staging space of its own; the hold's own
        reservation already covers the hold stop."""
        return self.role is not StopRole.HOLD


class LegSchedule(BaseModel):
    """When one lane of a journey is actually travelled (§7.9). Folded into the
    assignment so re-optimization can match a lane block against the leg's OWN
    departure instead of one probe instant for the whole journey."""

    model_config = ConfigDict(frozen=True)

    lane_id: str
    depart: datetime
    arrive: datetime


class Assignment(BaseModel):
    model_config = ConfigDict(frozen=True)

    facility_id: str
    zone_ids: list[str]
    route: list[str]  # lane ids, origin -> destination
    eta: datetime  # effective arrival (includes operating-window wait, §6)
    expected_departure: datetime  # end of dwell; reservation until_ts
    reservation_ids: list[str]
    # A->B delivery (§7.9): the outbound half, which `route` above never covers —
    # the lanes the goods travel AFTER the hold and the facility they finally
    # leave the network from. Empty/None for an ordinary allocation, so events
    # written before the extension fold to exactly the same assignment.
    outbound_route: list[str] = Field(default_factory=list)  # lane ids, hold -> exit
    exit_facility_id: str | None = None
    # The itinerary's stops and per-lane departure times (§7.9). Empty for an
    # ordinary allocation and for pre-extension events, which therefore fold to
    # exactly the same assignment they always did.
    stops: list[Stop] = Field(default_factory=list)
    legs: list[LegSchedule] = Field(default_factory=list)


class Shipment(BaseModel):
    id: str
    origin_facility_id: str | None = None  # None = external gate
    origin_label: str | None = None
    origin_lat: float | None = None
    origin_lon: float | None = None
    lines: list[LotSpec] = Field(default_factory=list)
    requirements: RequirementSet
    ready_at: datetime
    status: ShipmentStatus = ShipmentStatus.PLANNED
    assigned: Assignment | None = None
    allocation_seq: int = 0  # decisions taken so far; derives unique reservation ids
    is_transfer: bool = False
    transfer_lot_ids: list[str] = Field(default_factory=list)
    eta: datetime | None = None  # current best estimate; ShipmentDelayed updates it
    # A->B delivery (§7.9). `destination` set = the goods are held in the network
    # and then delivered onward to a customer point; `hold_days` is the minimum
    # storage the customer requires, and replaces `requirements.dwell_days` for
    # such a shipment. Both absent/default = ordinary allocation, unchanged.
    destination: Destination | None = None
    # Bounded at the domain, not at one ingestion path: a hold materializes one
    # capacity bucket per day at every candidate facility, so an unbounded value
    # from a world file or a replayed event is a denial of service, not a plan.
    hold_days: int = Field(default=0, ge=0, le=365)

    @property
    def dwell_days(self) -> int | None:
        """Days the goods occupy the chosen zone, or None to take the profile
        default. A delivery's hold is authoritative — it is the customer's
        requirement, not an estimate."""
        if self.destination is not None:
            return self.hold_days
        return self.requirements.dwell_days

    @property
    def size(self) -> CapacityVector:
        """The shipment's capacity footprint. An explicitly declared
        `requirements.size` is authoritative; otherwise the line sum."""
        if not self.requirements.size.is_zero():
            return self.requirements.size
        total = CapacityVector(slots=0, volume_l=0, weight_g=0)
        for line in self.lines:
            total = total.plus(line.size)
        return total


class Lane(BaseModel):
    id: str
    from_facility_id: str
    to_facility_id: str
    mode: str = "road"
    distance_km: float
    minutes: int
    cost_fixed_cents: int = 0
    cost_per_kg_cents: float = 0.0  # per canonical kg (1000 g)
    cost_per_m3_cents: float = 0.0  # per canonical m3 (1000 l)
    # Display geometry only: the drawn shape of the lane as (lon, lat) points.
    # No engine semantics — distance, time, and cost stay the declared values.
    path: list[tuple[float, float]] | None = None


class DisruptionKind(StrEnum):
    FACILITY_CLOSED = "facility_closed"
    ZONE_OFFLINE = "zone_offline"
    EQUIPMENT_DOWN = "equipment_down"
    LANE_BLOCKED = "lane_blocked"
    CAPACITY_REDUCED = "capacity_reduced"
    SHIPMENT_DELAYED = "shipment_delayed"


class Disruption(BaseModel):
    id: str
    kind: DisruptionKind
    target_id: str  # facility/zone/lane/shipment id, per kind
    detail: str | None = None  # e.g. the equipment tag for EQUIPMENT_DOWN
    from_ts: datetime
    until_ts: datetime  # planned end; DisruptionEnded may end it earlier
    magnitude: float = Field(default=1.0, ge=0.0, le=1.0)  # capacity fraction lost
    ended_at: datetime | None = None

    def active_during(self, start: datetime, end: datetime) -> bool:
        actual_end = self.ended_at if self.ended_at is not None else self.until_ts
        return self.from_ts < end and start < actual_end

    def active_at(self, at: datetime) -> bool:
        actual_end = self.ended_at if self.ended_at is not None else self.until_ts
        return self.from_ts <= at < actual_end

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """THE window predicate (§7.6, §7.9): does this disruption touch the
        window [start, end)? A degenerate window (a cross-dock hold, a zero-length
        handling dwell) is an instant, not an empty interval — `active_during`
        would call it untouched, which is how a closure over a cross-dock stop
        went unseen. Planning and re-optimization must use this same function or
        they disagree about what a disruption hits."""
        if end <= start:
            return self.active_at(start)
        return self.active_during(start, end)

    def blocks_departure_from(self, facility_id: str, at: datetime) -> bool:
        """THE origin-departure predicate (§7.9): is this facility shut at the
        instant the goods would roll OUT of it? A departure is an instant, not a
        window, so it goes through `overlaps` degenerate — the same function
        every stop window is matched with, so the planner, re-optimization and
        the trapped-cargo read model can never disagree about what a closure
        stops. No automatic routing out of a closed facility, ever."""
        return (
            self.kind is DisruptionKind.FACILITY_CLOSED
            and self.target_id == facility_id
            and self.overlaps(at, at)
        )
