"""NetworkState: the folded projection of the event log at an instant (§3, §5).

Everything here is derived — the only way state changes is `fold.apply_event`; the
public surface is queries only (a test asserts this). The time-phased capacity API
follows §5: daily UTC buckets, occupancy = present lots (until planned departure,
conservatively to the horizon when unknown) + unconsumed reservations, capacity
adjusted by disruptions active in the bucket. Bucket queries are defined for the
present and future only — historical buckets are served by `state_at`, where the
relevant disruptions and lots are still live.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr

from nodal.domain.calendar import is_open as _cal_is_open
from nodal.domain.calendar import next_open as _cal_next_open
from nodal.domain.calendar import require_aware
from nodal.domain.capacity import DIMENSIONS, CapacityVector
from nodal.domain.entities import (
    Disruption,
    DisruptionKind,
    Facility,
    InventoryLot,
    Lane,
    Reservation,
    Shipment,
    StorageZone,
)
from nodal.events.catalog import PlanDrafted

SCHEMA_VERSION = 1

Index = dict[str, set[str]]


def bucket_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def buckets_between(start: datetime, end: datetime) -> list[date]:
    """UTC day buckets overlapped by [start, end). A degenerate interval still
    occupies the bucket it sits in."""
    start = require_aware(start).astimezone(UTC)
    end = require_aware(end).astimezone(UTC)
    if end <= start:
        end = start + timedelta(microseconds=1)
    last = (end - timedelta(microseconds=1)).date()
    days: list[date] = []
    day = start.date()
    while day <= last:
        days.append(day)
        day += timedelta(days=1)
    return days


def _index_add(index: Index, key: str, value: str) -> None:
    index.setdefault(key, set()).add(value)


def _index_discard(index: Index, key: str, value: str) -> None:
    """Remove and drop empty keys, so incremental maintenance converges to exactly
    what `_rebuild_indexes` builds (fold(snapshot+tail) == fold(all), §4)."""
    bucket = index.get(key)
    if bucket is None:
        return
    bucket.discard(value)
    if not bucket:
        del index[key]


class NetworkState(BaseModel):
    schema_version: int = SCHEMA_VERSION
    last_seq: int = 0
    last_ts: datetime | None = None

    facilities: dict[str, Facility] = Field(default_factory=dict)
    zones: dict[str, StorageZone] = Field(default_factory=dict)
    lanes: dict[str, Lane] = Field(default_factory=dict)
    lots: dict[str, InventoryLot] = Field(default_factory=dict)
    shipments: dict[str, Shipment] = Field(default_factory=dict)
    reservations: dict[str, Reservation] = Field(default_factory=dict)
    disruptions: dict[str, Disruption] = Field(default_factory=dict)
    demand_rates: dict[str, dict[str, int]] = Field(default_factory=dict)  # facility -> group
    # Ids of compacted terminal shipments (§4): keeps duplicate-id guards intact
    # after the objects leave the state. Append-ordered, so serialization is stable.
    terminal_shipment_ids: list[str] = Field(default_factory=list)
    # The batch plan an operator drafted and has not committed (§7.5), or None.
    # Pending only while its `PlanDrafted` is the log head: anything appended
    # after it is a change the plan never saw. `fold.apply_event` owns the rule.
    pending_plan: PlanDrafted | None = None

    _lots_by_zone: Index = PrivateAttr(default_factory=dict)
    _active_res_by_zone: Index = PrivateAttr(default_factory=dict)
    _res_by_holder: Index = PrivateAttr(default_factory=dict)
    _zones_by_facility: Index = PrivateAttr(default_factory=dict)
    _lanes_from: Index = PrivateAttr(default_factory=dict)
    _open_disruptions_by_target: Index = PrivateAttr(default_factory=dict)
    _terminal_shipments: set[str] = PrivateAttr(default_factory=set)

    def model_post_init(self, context: Any, /) -> None:
        self._rebuild_indexes()

    # -- index maintenance (package-private: only the fold may call these) --------

    def _rebuild_indexes(self) -> None:
        self._lots_by_zone = {}
        self._active_res_by_zone = {}
        self._res_by_holder = {}
        self._zones_by_facility = {}
        self._lanes_from = {}
        self._open_disruptions_by_target = {}
        for zone in self.zones.values():
            _index_add(self._zones_by_facility, zone.facility_id, zone.id)
        for lane in self.lanes.values():
            _index_add(self._lanes_from, lane.from_facility_id, lane.id)
        for lot in self.lots.values():
            if lot.zone_id is not None:
                _index_add(self._lots_by_zone, lot.zone_id, lot.id)
        for res in self.reservations.values():
            _index_add(self._res_by_holder, res.holder, res.id)
            if res.released_at is None:
                _index_add(self._active_res_by_zone, res.zone_id, res.id)
        for disruption in self.disruptions.values():
            if disruption.ended_at is None:
                _index_add(self._open_disruptions_by_target, disruption.target_id, disruption.id)
        self._terminal_shipments = set(self.terminal_shipment_ids)

    def _mark_terminal(self, shipment_id: str) -> None:
        self.terminal_shipment_ids.append(shipment_id)
        self._terminal_shipments.add(shipment_id)

    def is_known_shipment_id(self, shipment_id: str) -> bool:
        """True if the id names a live or compacted-terminal shipment."""
        return shipment_id in self.shipments or shipment_id in self._terminal_shipments

    def _index_zone(self, zone: StorageZone) -> None:
        _index_add(self._zones_by_facility, zone.facility_id, zone.id)

    def _index_lane(self, lane: Lane) -> None:
        _index_add(self._lanes_from, lane.from_facility_id, lane.id)

    def _reindex_lot(self, lot_id: str, old_zone: str | None, new_zone: str | None) -> None:
        if old_zone is not None:
            _index_discard(self._lots_by_zone, old_zone, lot_id)
        if new_zone is not None:
            _index_add(self._lots_by_zone, new_zone, lot_id)

    def _index_reservation(self, res: Reservation) -> None:
        _index_add(self._active_res_by_zone, res.zone_id, res.id)
        _index_add(self._res_by_holder, res.holder, res.id)

    def _unindex_reservation(self, res: Reservation) -> None:
        _index_discard(self._active_res_by_zone, res.zone_id, res.id)

    def _unindex_reservation_holder(self, res: Reservation) -> None:
        _index_discard(self._res_by_holder, res.holder, res.id)

    def _index_disruption(self, disruption: Disruption) -> None:
        _index_add(self._open_disruptions_by_target, disruption.target_id, disruption.id)

    def _unindex_disruption(self, disruption: Disruption) -> None:
        _index_discard(self._open_disruptions_by_target, disruption.target_id, disruption.id)

    # -- entity queries ----------------------------------------------------------

    def zones_of(self, facility_id: str) -> list[StorageZone]:
        return [self.zones[z] for z in sorted(self._zones_by_facility.get(facility_id, ()))]

    def lanes_from(self, facility_id: str) -> list[Lane]:
        return [self.lanes[i] for i in sorted(self._lanes_from.get(facility_id, ()))]

    def lots_in_zone(self, zone_id: str) -> list[InventoryLot]:
        return [self.lots[i] for i in sorted(self._lots_by_zone.get(zone_id, ()))]

    def reservations_on_zone(self, zone_id: str) -> list[Reservation]:
        """Unreleased reservations booked on the zone."""
        return [self.reservations[i] for i in sorted(self._active_res_by_zone.get(zone_id, ()))]

    def reservations_of(self, shipment_id: str) -> list[Reservation]:
        """All of a shipment's reservations, released ones included (audit trail)."""
        return [self.reservations[i] for i in sorted(self._res_by_holder.get(shipment_id, ()))]

    def active_reservations_of(self, shipment_id: str) -> list[Reservation]:
        return [r for r in self.reservations_of(shipment_id) if r.released_at is None]

    # -- time-phased capacity (§5) ----------------------------------------------

    def _require_scheduling_bucket(self, day: date) -> None:
        if self.last_ts is not None and day < self.last_ts.astimezone(UTC).date():
            raise ValueError(
                f"bucket {day.isoformat()} precedes state time "
                f"{self.last_ts.isoformat()}; reconstruct history via state_at"
            )

    def disruptions_on(self, target_id: str, start: datetime, end: datetime) -> list[Disruption]:
        """Open disruptions on the target active during [start, end)."""
        ids = self._open_disruptions_by_target.get(target_id, set())
        return sorted(
            (d for d in (self.disruptions[i] for i in ids) if d.active_during(start, end)),
            key=lambda d: d.id,
        )

    def departure_closure(self, facility_id: str | None, at: datetime) -> Disruption | None:
        """The open closure that keeps goods from leaving `facility_id` at the
        instant `at`, or None (§7.9). The single lookup every path asks — single
        allocate, batch, re-optimization, what-if, rebalancing — so none of them
        can plan a departure the others would refuse. `None` facility = an
        external gate, which no closure can hold."""
        if facility_id is None:
            return None
        ids = self._open_disruptions_by_target.get(facility_id, set())
        candidates = sorted((self.disruptions[i] for i in ids), key=lambda d: d.id)
        return next((d for d in candidates if d.blocks_departure_from(facility_id, at)), None)

    def effective_capacity(self, zone_id: str, day: date) -> CapacityVector:
        self._require_scheduling_bucket(day)
        zone = self.zones[zone_id]
        start, end = bucket_bounds(day)
        factor = 1.0
        for disruption in self.disruptions_on(zone.id, start, end) + self.disruptions_on(
            zone.facility_id, start, end
        ):
            if disruption.kind in (DisruptionKind.FACILITY_CLOSED, DisruptionKind.ZONE_OFFLINE):
                return CapacityVector(slots=0, volume_l=0, weight_g=0)
            if disruption.kind is DisruptionKind.CAPACITY_REDUCED:
                factor *= 1.0 - disruption.magnitude
        # Single flooring at the end: stacking is order-independent.
        return zone.capacity if factor == 1.0 else zone.capacity.scaled(factor)

    def occupancy(self, zone_id: str, day: date) -> CapacityVector:
        self._require_scheduling_bucket(day)
        start, end = bucket_bounds(day)
        total = CapacityVector(slots=0, volume_l=0, weight_g=0)
        for lot in self.lots_in_zone(zone_id):
            if lot.planned_departure is None or lot.planned_departure > start:
                total = total.plus(lot.size)
        for res in self.reservations_on_zone(zone_id):
            if res.active_during(start, end):
                total = total.plus(res.size)
        return total

    def headroom(self, zone_id: str, day: date) -> CapacityVector:
        return self.effective_capacity(zone_id, day).minus_demand(self.occupancy(zone_id, day))

    def fits(self, zone_id: str, size: CapacityVector, start: datetime, end: datetime) -> bool:
        days = buckets_between(start, end)
        return all(size.fits_within(self.headroom(zone_id, day)) for day in days)

    def zone_utilization(self, zone_id: str, day: date) -> float:
        capacity = self.effective_capacity(zone_id, day)
        occupancy = self.occupancy(zone_id, day)
        worst = 0.0
        for dim in DIMENSIONS:
            cap = capacity.get(dim)
            if cap is None:
                continue
            used = occupancy.demand(dim)
            ratio = (1.0 if used > 0 else 0.0) if cap <= 0 else used / cap
            worst = max(worst, ratio)
        return worst

    def facility_peak_utilization(self, facility_id: str, days: list[date]) -> float:
        peak = 0.0
        for zone in self.zones_of(facility_id):
            for day in days:
                peak = max(peak, self.zone_utilization(zone.id, day))
        return peak

    # -- inventory distribution (§7.7) -------------------------------------------

    def stock(self, facility_id: str, commodity_group: str) -> int:
        total = 0
        for zone in self.zones_of(facility_id):
            for lot in self.lots_in_zone(zone.id):
                if lot.commodity_group == commodity_group:
                    total += lot.quantity
        return total

    def demand_rate(self, facility_id: str, commodity_group: str) -> int:
        return self.demand_rates.get(facility_id, {}).get(commodity_group, 0)

    # -- disruptions & calendars ---------------------------------------------------

    def active_disruptions(self, at: datetime) -> list[Disruption]:
        require_aware(at)
        return sorted((d for d in self.disruptions.values() if d.active_at(at)), key=lambda d: d.id)

    def facility_open(self, facility_id: str, at: datetime) -> bool:
        facility = self.facilities[facility_id]
        return _cal_is_open(facility.calendar, facility.tz, at)

    def facility_next_open(self, facility_id: str, at: datetime) -> datetime | None:
        facility = self.facilities[facility_id]
        return _cal_next_open(facility.calendar, facility.tz, at)
