"""The fold: NetworkState = fold(events) (§4).

Handlers are total over well-formed logs and raise `FoldError` (with sequence
context) on referential or transition violations — a producer bug, never a user
input path. Determinism, not purity: handlers mutate state in place.
"""

from collections.abc import Callable, Iterable
from datetime import datetime

from nodal.domain.entities import Shipment, ShipmentStatus
from nodal.events import catalog as ev
from nodal.events.envelope import Envelope, EventPayload
from nodal.events.state import NetworkState


class FoldError(Exception):
    def __init__(self, message: str, seq: int | None = None, event_type: str | None = None):
        prefix = f"[seq {seq} {event_type}] " if seq is not None else ""
        super().__init__(prefix + message)


_ALLOWED_TRANSITIONS: dict[str, tuple[ShipmentStatus, ...]] = {
    "depart": (ShipmentStatus.ALLOCATED,),
    "arrive": (ShipmentStatus.IN_TRANSIT,),
    "cancel": (ShipmentStatus.PLANNED, ShipmentStatus.ALLOCATED, ShipmentStatus.IN_TRANSIT),
    # Re-deciding an allocated shipment requires AllocationSuperseded first (§7.6):
    # allowing decide-on-allocated would leak the prior reservation.
    "decide": (ShipmentStatus.PLANNED,),
    "supersede": (ShipmentStatus.ALLOCATED,),
}


def _shipment(state: NetworkState, shipment_id: str) -> Shipment:
    shipment = state.shipments.get(shipment_id)
    if shipment is None:
        raise FoldError(f"unknown shipment {shipment_id}")
    return shipment


def _release_reservations(
    state: NetworkState, shipment_id: str, ts: datetime, zone_id: str | None = None
) -> None:
    for res in state.active_reservations_of(shipment_id):
        if zone_id is None or res.zone_id == zone_id:
            res.released_at = ts
            state._unindex_reservation(res)


def _compact_terminal_shipment(state: NetworkState, shipment_id: str) -> None:
    """Terminal shipments (arrived/cancelled) and their reservations leave the
    folded state (§4): the log keeps the full history — records, audit, replay —
    while the state stays bounded by *active* work, not by everything that ever
    happened. Every reservation is released by the time this runs. The id lands in
    the terminal tombstone set so duplicate-id guards survive compaction."""
    for res in state.reservations_of(shipment_id):
        state._unindex_reservation(res)
        state._unindex_reservation_holder(res)
        del state.reservations[res.id]
    del state.shipments[shipment_id]
    state._mark_terminal(shipment_id)


# --- handlers -------------------------------------------------------------------


def _facility_registered(state: NetworkState, p: ev.FacilityRegistered, ts: datetime) -> None:
    if p.facility.id in state.facilities:
        raise FoldError(f"duplicate facility {p.facility.id}")
    state.facilities[p.facility.id] = p.facility.model_copy(deep=True)


def _facility_updated(state: NetworkState, p: ev.FacilityUpdated, ts: datetime) -> None:
    facility = state.facilities.get(p.facility_id)
    if facility is None:
        raise FoldError(f"unknown facility {p.facility_id}")
    if p.name is not None:
        facility.name = p.name
    if p.risk_factor is not None:
        facility.risk_factor = p.risk_factor
    if p.tags is not None:
        facility.tags = sorted(set(p.tags))


def _zone_registered(state: NetworkState, p: ev.ZoneRegistered, ts: datetime) -> None:
    if p.zone.id in state.zones:
        raise FoldError(f"duplicate zone {p.zone.id}")
    if p.zone.facility_id not in state.facilities:
        raise FoldError(f"zone {p.zone.id} references unknown facility {p.zone.facility_id}")
    zone = p.zone.model_copy(deep=True)
    state.zones[zone.id] = zone
    state._index_zone(zone)


def _zone_updated(state: NetworkState, p: ev.ZoneUpdated, ts: datetime) -> None:
    zone = state.zones.get(p.zone_id)
    if zone is None:
        raise FoldError(f"unknown zone {p.zone_id}")
    if p.kind is not None:
        zone.kind = p.kind
    if p.temp_c is not None:
        zone.temp_c = p.temp_c
    if p.allowed_classes is not None:
        zone.allowed_classes = sorted(set(p.allowed_classes))


def _lane_registered(state: NetworkState, p: ev.LaneRegistered, ts: datetime) -> None:
    if p.lane.id in state.lanes:
        raise FoldError(f"duplicate lane {p.lane.id}")
    for facility_id in (p.lane.from_facility_id, p.lane.to_facility_id):
        if facility_id not in state.facilities:
            raise FoldError(f"lane {p.lane.id} references unknown facility {facility_id}")
    lane = p.lane.model_copy(deep=True)
    state.lanes[lane.id] = lane
    state._index_lane(lane)


def _lane_updated(state: NetworkState, p: ev.LaneUpdated, ts: datetime) -> None:
    lane = state.lanes.get(p.lane_id)
    if lane is None:
        raise FoldError(f"unknown lane {p.lane_id}")
    if p.distance_km is not None:
        lane.distance_km = p.distance_km
    if p.minutes is not None:
        lane.minutes = p.minutes
    if p.cost_fixed_cents is not None:
        lane.cost_fixed_cents = p.cost_fixed_cents
    if p.cost_per_kg_cents is not None:
        lane.cost_per_kg_cents = p.cost_per_kg_cents
    if p.cost_per_m3_cents is not None:
        lane.cost_per_m3_cents = p.cost_per_m3_cents


def _equipment_set(state: NetworkState, p: ev.EquipmentCountSet, ts: datetime) -> None:
    facility = state.facilities.get(p.facility_id)
    if facility is None:
        raise FoldError(f"unknown facility {p.facility_id}")
    facility.equipment[p.tag] = p.count


def _certification_set(state: NetworkState, p: ev.CertificationSet, ts: datetime) -> None:
    facility = state.facilities.get(p.facility_id)
    if facility is None:
        raise FoldError(f"unknown facility {p.facility_id}")
    facility.certifications = [
        c for c in facility.certifications if c.tag != p.certification.tag
    ] + [p.certification]


def _calendar_set(state: NetworkState, p: ev.OperatingCalendarSet, ts: datetime) -> None:
    facility = state.facilities.get(p.facility_id)
    if facility is None:
        raise FoldError(f"unknown facility {p.facility_id}")
    facility.calendar = p.calendar.model_copy(deep=True) if p.calendar is not None else None


def _demand_rate_set(state: NetworkState, p: ev.DemandRateSet, ts: datetime) -> None:
    if p.facility_id not in state.facilities:
        raise FoldError(f"unknown facility {p.facility_id}")
    state.demand_rates.setdefault(p.facility_id, {})[p.commodity_group] = p.per_day


def _lot_received(state: NetworkState, p: ev.LotReceived, ts: datetime) -> None:
    if p.lot.id in state.lots:
        raise FoldError(f"duplicate lot {p.lot.id}")
    if p.lot.zone_id is None:
        raise FoldError(f"LotReceived {p.lot.id} must name a zone")
    if p.lot.zone_id not in state.zones:
        raise FoldError(f"lot {p.lot.id} references unknown zone {p.lot.zone_id}")
    lot = p.lot.model_copy(deep=True)
    if lot.received_at is None:
        lot.received_at = ts
    state.lots[lot.id] = lot
    state._reindex_lot(lot.id, None, lot.zone_id)
    if lot.shipment_id is not None:
        # Goods landing consume the shipment's reservation on this zone (§5) — a
        # structural guarantee, so a `state_at` cut between the events of an
        # arrival batch can never double-count.
        _release_reservations(state, lot.shipment_id, ts, zone_id=lot.zone_id)


def _lot_adjusted(state: NetworkState, p: ev.LotQuantityAdjusted, ts: datetime) -> None:
    lot = state.lots.get(p.lot_id)
    if lot is None:
        raise FoldError(f"unknown lot {p.lot_id}")
    lot.quantity = p.new_quantity
    if p.new_size is not None:
        lot.size = p.new_size


def _lot_moved(state: NetworkState, p: ev.LotMoved, ts: datetime) -> None:
    lot = state.lots.get(p.lot_id)
    if lot is None:
        raise FoldError(f"unknown lot {p.lot_id}")
    if p.to_zone_id not in state.zones:
        raise FoldError(f"unknown zone {p.to_zone_id}")
    old_zone = lot.zone_id
    lot.zone_id = p.to_zone_id
    lot.shipment_id = None  # placement into a zone ends any aboard-status
    if p.planned_departure is not None:
        lot.planned_departure = p.planned_departure
    state._reindex_lot(lot.id, old_zone, p.to_zone_id)


def _lot_shipped(state: NetworkState, p: ev.LotShipped, ts: datetime) -> None:
    lot = state.lots.get(p.lot_id)
    if lot is None:
        raise FoldError(f"unknown lot {p.lot_id}")
    if lot.zone_id is None:
        raise FoldError(f"lot {p.lot_id} is not in a zone")
    if p.shipment_id not in state.shipments:
        raise FoldError(f"unknown shipment {p.shipment_id}")
    old_zone = lot.zone_id
    lot.zone_id = None
    lot.shipment_id = p.shipment_id
    state._reindex_lot(lot.id, old_zone, None)


def _register_shipment(state: NetworkState, shipment: Shipment) -> None:
    if state.is_known_shipment_id(shipment.id):
        raise FoldError(f"duplicate shipment {shipment.id}")
    if shipment.status is not ShipmentStatus.PLANNED or shipment.assigned is not None:
        raise FoldError(f"shipment {shipment.id} must register as planned/unassigned")
    if shipment.origin_facility_id is not None and (
        shipment.origin_facility_id not in state.facilities
    ):
        raise FoldError(
            f"shipment {shipment.id} references unknown facility {shipment.origin_facility_id}"
        )
    state.shipments[shipment.id] = shipment.model_copy(deep=True)


def _shipment_registered(state: NetworkState, p: ev.ShipmentRegistered, ts: datetime) -> None:
    _register_shipment(state, p.shipment)


def _transfer_ordered(state: NetworkState, p: ev.TransferOrdered, ts: datetime) -> None:
    if not p.shipment.is_transfer:
        raise FoldError(f"TransferOrdered {p.shipment.id} must set is_transfer")
    for lot_id in p.shipment.transfer_lot_ids:
        if lot_id not in state.lots:
            raise FoldError(f"transfer {p.shipment.id} references unknown lot {lot_id}")
    _register_shipment(state, p.shipment)


def _shipment_departed(state: NetworkState, p: ev.ShipmentDeparted, ts: datetime) -> None:
    shipment = _shipment(state, p.shipment_id)
    if shipment.status not in _ALLOWED_TRANSITIONS["depart"]:
        raise FoldError(f"cannot depart shipment in status {shipment.status}")
    shipment.status = ShipmentStatus.IN_TRANSIT
    if shipment.assigned is not None:
        shipment.eta = shipment.assigned.eta


def _shipment_delayed(state: NetworkState, p: ev.ShipmentDelayed, ts: datetime) -> None:
    shipment = _shipment(state, p.shipment_id)
    if shipment.status in (ShipmentStatus.ARRIVED, ShipmentStatus.CANCELLED):
        raise FoldError(f"cannot delay shipment in status {shipment.status}")
    shipment.eta = p.new_eta


def _shipment_ready_changed(state: NetworkState, p: ev.ShipmentReadyChanged, ts: datetime) -> None:
    shipment = _shipment(state, p.shipment_id)
    if shipment.status not in (ShipmentStatus.PLANNED, ShipmentStatus.ALLOCATED):
        raise FoldError(f"cannot change readiness of shipment in status {shipment.status}")
    shipment.ready_at = p.new_ready


def _shipment_arrived(state: NetworkState, p: ev.ShipmentArrived, ts: datetime) -> None:
    shipment = _shipment(state, p.shipment_id)
    if shipment.status not in _ALLOWED_TRANSITIONS["arrive"]:
        raise FoldError(f"cannot arrive shipment in status {shipment.status}")
    shipment.status = ShipmentStatus.ARRIVED
    shipment.eta = ts
    _release_reservations(state, shipment.id, ts)
    _compact_terminal_shipment(state, shipment.id)


def _shipment_cancelled(state: NetworkState, p: ev.ShipmentCancelled, ts: datetime) -> None:
    shipment = _shipment(state, p.shipment_id)
    if shipment.status not in _ALLOWED_TRANSITIONS["cancel"]:
        raise FoldError(f"cannot cancel shipment in status {shipment.status}")
    shipment.status = ShipmentStatus.CANCELLED
    _release_reservations(state, shipment.id, ts)
    _compact_terminal_shipment(state, shipment.id)


def _capacity_adjusted(state: NetworkState, p: ev.CapacityAdjusted, ts: datetime) -> None:
    zone = state.zones.get(p.zone_id)
    if zone is None:
        raise FoldError(f"unknown zone {p.zone_id}")
    zone.capacity = p.capacity


def _disruption_started(state: NetworkState, p: ev.DisruptionStarted, ts: datetime) -> None:
    if p.disruption.id in state.disruptions:
        raise FoldError(f"duplicate disruption {p.disruption.id}")
    disruption = p.disruption.model_copy(deep=True)
    state.disruptions[disruption.id] = disruption
    state._index_disruption(disruption)


def _disruption_ended(state: NetworkState, p: ev.DisruptionEnded, ts: datetime) -> None:
    disruption = state.disruptions.get(p.disruption_id)
    if disruption is None:
        raise FoldError(f"unknown disruption {p.disruption_id}")
    if disruption.ended_at is None:
        disruption.ended_at = ts
        state._unindex_disruption(disruption)


def _reservation_placed(state: NetworkState, p: ev.ReservationPlaced, ts: datetime) -> None:
    res = p.reservation
    if res.id in state.reservations:
        raise FoldError(f"duplicate reservation {res.id}")
    if res.zone_id not in state.zones:
        raise FoldError(f"reservation {res.id} references unknown zone {res.zone_id}")
    if res.holder not in state.shipments:
        raise FoldError(f"reservation {res.id} references unknown shipment {res.holder}")
    if res.released_at is not None:
        raise FoldError(f"reservation {res.id} must be placed unreleased")
    stored = res.model_copy(deep=True)
    state.reservations[stored.id] = stored
    state._index_reservation(stored)


def _reservation_released(state: NetworkState, p: ev.ReservationReleased, ts: datetime) -> None:
    res = state.reservations.get(p.reservation_id)
    if res is None:
        raise FoldError(f"unknown reservation {p.reservation_id}")
    if res.released_at is None:
        res.released_at = ts
        state._unindex_reservation(res)


def _allocation_decided(state: NetworkState, p: ev.AllocationDecided, ts: datetime) -> None:
    shipment = _shipment(state, p.shipment_id)
    if shipment.status not in _ALLOWED_TRANSITIONS["decide"]:
        raise FoldError(f"cannot allocate shipment in status {shipment.status}")
    if p.assignment.facility_id not in state.facilities:
        raise FoldError(f"assignment references unknown facility {p.assignment.facility_id}")
    for zone_id in p.assignment.zone_ids:
        if zone_id not in state.zones:
            raise FoldError(f"assignment references unknown zone {zone_id}")
    shipment.assigned = p.assignment.model_copy(deep=True)
    shipment.status = ShipmentStatus.ALLOCATED
    shipment.allocation_seq += 1
    shipment.eta = p.assignment.eta


def _allocation_superseded(state: NetworkState, p: ev.AllocationSuperseded, ts: datetime) -> None:
    shipment = _shipment(state, p.shipment_id)
    if shipment.status not in _ALLOWED_TRANSITIONS["supersede"]:
        raise FoldError(f"cannot supersede shipment in status {shipment.status}")
    shipment.assigned = None
    shipment.status = ShipmentStatus.PLANNED
    shipment.eta = None
    _release_reservations(state, shipment.id, ts)


def _batch_solved(state: NetworkState, p: ev.BatchSolved, ts: datetime) -> None:
    pass  # audit only


def _plan_drafted(state: NetworkState, p: ev.PlanDrafted, ts: datetime) -> None:
    """The proposal IS the state (§7.5): the drafted plan stands until the next
    event lands on the log, which `apply_event` treats as staling it."""
    state.pending_plan = p


def _plan_discarded(state: NetworkState, p: ev.PlanDiscarded, ts: datetime) -> None:
    pass  # audit only: being an event at all is what clears the pending plan


_Handler = Callable[[NetworkState, EventPayload, datetime], None]

_HANDLERS: dict[type[EventPayload], _Handler] = {}


def _register(payload_type: type[EventPayload], handler: Callable[..., None]) -> None:
    _HANDLERS[payload_type] = handler


_register(ev.FacilityRegistered, _facility_registered)
_register(ev.FacilityUpdated, _facility_updated)
_register(ev.ZoneRegistered, _zone_registered)
_register(ev.ZoneUpdated, _zone_updated)
_register(ev.LaneRegistered, _lane_registered)
_register(ev.LaneUpdated, _lane_updated)
_register(ev.EquipmentCountSet, _equipment_set)
_register(ev.CertificationSet, _certification_set)
_register(ev.OperatingCalendarSet, _calendar_set)
_register(ev.DemandRateSet, _demand_rate_set)
_register(ev.LotReceived, _lot_received)
_register(ev.LotQuantityAdjusted, _lot_adjusted)
_register(ev.LotMoved, _lot_moved)
_register(ev.LotShipped, _lot_shipped)
_register(ev.ShipmentRegistered, _shipment_registered)
_register(ev.TransferOrdered, _transfer_ordered)
_register(ev.ShipmentDeparted, _shipment_departed)
_register(ev.ShipmentDelayed, _shipment_delayed)
_register(ev.ShipmentReadyChanged, _shipment_ready_changed)
_register(ev.ShipmentArrived, _shipment_arrived)
_register(ev.ShipmentCancelled, _shipment_cancelled)
_register(ev.CapacityAdjusted, _capacity_adjusted)
_register(ev.DisruptionStarted, _disruption_started)
_register(ev.DisruptionEnded, _disruption_ended)
_register(ev.ReservationPlaced, _reservation_placed)
_register(ev.ReservationReleased, _reservation_released)
_register(ev.AllocationDecided, _allocation_decided)
_register(ev.AllocationSuperseded, _allocation_superseded)
_register(ev.BatchSolved, _batch_solved)
_register(ev.PlanDrafted, _plan_drafted)
_register(ev.PlanDiscarded, _plan_discarded)


def apply_event(state: NetworkState, envelope: Envelope) -> None:
    handler = _HANDLERS.get(type(envelope.payload))
    if handler is None:
        raise FoldError("no handler", envelope.seq, envelope.type)
    # A drafted plan is pending only while it IS the log head (§7.5): a commit, a
    # disruption, a registration, a discard — any later event is a change the plan
    # never saw, so the fold stales it exactly as the API's head guard does. The
    # PlanDrafted handler then installs its own.
    state.pending_plan = None
    try:
        handler(state, envelope.payload, envelope.ts)
    except FoldError as err:
        raise FoldError(str(err), envelope.seq, envelope.type) from None
    state.last_seq = envelope.seq
    state.last_ts = envelope.ts


def fold(envelopes: Iterable[Envelope], into: NetworkState | None = None) -> NetworkState:
    state = into if into is not None else NetworkState()
    for envelope in envelopes:
        apply_event(state, envelope)
    return state
