"""Read models (§11): pure derivations from NetworkState / the event log.

Every function returns JSON-ready plain data (datetimes as ISO strings). No
mutation, no business logic — the UI renders what the engine already knows.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from nodal.allocate.batch import drafted_assignments
from nodal.allocate.config import ObjectiveConfig
from nodal.allocate.reopt import all_trapped_cargo
from nodal.domain.capacity import DIMENSIONS
from nodal.domain.entities import Assignment, StopRole
from nodal.events import EventStore
from nodal.events.state import NetworkState

Json = dict[str, Any]


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def _stop_rows(assignment: Assignment) -> list[Json]:
    """The itinerary's stops as the map draws them (§7.9): every facility the
    goods touch, in travel order, with the window and staging zone it books.
    Empty for an ordinary allocation, which touches only its own destination."""
    return [
        {
            "facility_id": stop.facility_id,
            "role": stop.role.value,
            "arrive": _iso(stop.arrive),
            "depart": _iso(stop.depart),
            "zone_id": stop.zone_id,
        }
        for stop in assignment.stops
    ]


def _drafted_row(record: Any) -> Json:
    """One shipment's line in a drafted plan: where the plan would put it, or the
    constraints that left it unassigned. `rejected` is the complete picture (§7.5)
    and every entry carries at least one facility verdict, so the reasons are the
    distinct constraint ids across them."""
    chosen = record.get("chosen") if isinstance(record, dict) else None
    if isinstance(chosen, dict):
        return {
            "facility_id": str(chosen["facility_id"]),
            "zone_id": str(chosen["zone_id"]),
            "eta": str(chosen["eta"]),
            "unassigned_reasons": [],
        }
    rejected = record.get("rejected") if isinstance(record, dict) else None
    reasons = sorted(
        {
            str(verdict["constraint_id"])
            for entry in rejected or []
            for verdict in entry.get("facility_verdicts") or []
        }
    )
    return {
        "facility_id": None,
        "zone_id": None,
        "eta": None,
        "unassigned_reasons": reasons,
    }


def pending_plan(state: NetworkState) -> Json | None:
    """The plan drafted at the log head and not yet committed (§7.5), in exactly
    the shape a dry run returns — so a reload re-enters plan review with the
    full records the map and the decision panel draw from."""
    plan = state.pending_plan
    if plan is None:
        return None
    pairs = drafted_assignments(plan)
    return {
        "batch_id": plan.batch_id,
        # The head the solve READ: a commit hands it back as `expected_head`.
        "head": plan.based_on_seq,
        "meta": plan.meta,
        "assignments": {sid: list(pair) if pair else None for sid, pair in pairs.items()},
        "records": {sid: plan.records[sid] for sid in sorted(plan.records)},
    }


def pending_plan_summary(state: NetworkState) -> Json | None:
    """What the map needs to open IN plan review: the plan's identity, its head,
    its counts, and one line per shipment. The full records (routes, scores,
    rejections) come from `/api/plan` — this is the signal, not the plan."""
    plan = state.pending_plan
    if plan is None:
        return None
    return {
        "batch_id": plan.batch_id,
        "based_on_seq": plan.based_on_seq,
        # The draft IS the head while it is pending, so this is its own seq.
        "head": state.last_seq,
        "assigned": plan.assigned,
        "unassigned": plan.unassigned,
        "decisions": {sid: _drafted_row(plan.records[sid]) for sid in sorted(plan.records)},
    }


def _facility_utilization(state: NetworkState, facility_id: str, day: Any) -> float:
    """The engine's own peak-utilization convention (§11: thin translation —
    never a reimplementation that could drift from the scorer's)."""
    return round(state.facility_peak_utilization(facility_id, [day]), 4)


def _decision_seq(store: EventStore, state: NetworkState, shipment_id: str) -> int | None:
    """Seq of the AllocationDecided behind the shipment's assignment in `state`:
    its newest one at or before the head being read."""
    seq = store.last_entity_event_seq("shipment", shipment_id, "AllocationDecided")
    if seq is None or seq <= state.last_seq:
        return seq
    # Only a replay (`at`) can put the newest decision past the head being read;
    # then the one that was current back there needs a bounded scan.
    earlier = [
        envelope.seq
        for envelope in store.read(to_seq=state.last_seq)
        if envelope.type == "AllocationDecided" and envelope.entity_id == shipment_id
    ]
    return earlier[-1] if earlier else None


def _outbound_half(
    store: EventStore, state: NetworkState, shipment_id: str
) -> tuple[list[str], str | None]:
    """A booked delivery's lane ids after the hold, and the exit facility its
    last mile leaves from (§7.9). The assignment records only the inbound half —
    the transport that books the hold — so the outbound one is read back out of
    the decision record in the log."""
    seq = _decision_seq(store, state, shipment_id)
    if seq is None:
        return [], None
    record: Any = getattr(next(store.read(from_seq=seq, to_seq=seq)).payload, "record", None)
    if not isinstance(record, dict):
        return [], None
    itinerary = (record.get("chosen") or {}).get("itinerary")
    if not itinerary:
        return [], None
    # The hold splits the legs, exactly as the engine's own rendering does; the
    # final leg is always the last mile, so it departs the exit facility.
    until = datetime.fromisoformat(itinerary["hold"]["until_ts"])
    lanes = [
        leg["lane_id"]
        for leg in itinerary["legs"]
        if leg["lane_id"] is not None and datetime.fromisoformat(leg["depart"]) >= until
    ]
    return lanes, str(itinerary["legs"][-1]["from_label"])


def map_state(state: NetworkState, store: EventStore) -> Json:
    """Everything the global map draws: facilities (with utilization), lanes,
    shipments (with arcs where allocated), active disruptions."""
    now = state.last_ts
    if now is None:
        return {
            "empty": True,
            "facilities": [],
            "lanes": [],
            "shipments": [],
            "disruptions": [],
            "pending_plan": None,
        }
    day = now.astimezone(UTC).date()
    facilities = []
    for facility in state.facilities.values():
        zones = []
        for zone in state.zones_of(facility.id):
            occupancy = state.occupancy(zone.id, day)
            capacity = state.effective_capacity(zone.id, day)
            zones.append(
                {
                    "id": zone.id,
                    "kind": zone.kind,
                    "lots": len(state.lots_in_zone(zone.id)),
                    "occupancy": {d: occupancy.demand(d) for d in DIMENSIONS},
                    "capacity": {d: capacity.get(d) for d in DIMENSIONS},
                }
            )
        facilities.append(
            {
                "id": facility.id,
                "name": facility.name,
                "lat": facility.lat,
                "lon": facility.lon,
                "open": state.facility_open(facility.id, now),
                "utilization": _facility_utilization(state, facility.id, day),
                "zones": zones,
            }
        )
    lanes = [
        {
            "id": lane.id,
            "from": lane.from_facility_id,
            "to": lane.to_facility_id,
            "mode": lane.mode,
            "km": lane.distance_km,
            "minutes": lane.minutes,
            # Display geometry as (lon, lat) points, null when the lane declares
            # none — the map then draws the great circle itself.
            "path": [list(point) for point in lane.path] if lane.path else None,
        }
        for lane in state.lanes.values()
    ]
    # Cargo a closure physically stranded (§7.9), by re-optimization's OWN
    # predicate — the same shipments it refuses to re-route. Flagged on the read
    # model so the queue keeps badging them, not only in the one command
    # response whose disruption discovered them.
    trapped_ids = {entry.shipment_id for entry in all_trapped_cargo(state, now)}
    shipments = []
    for shipment in state.shipments.values():
        assigned = shipment.assigned
        outbound_route: list[str] = []
        exit_facility_id: str | None = None
        if assigned is not None and shipment.destination is not None:
            outbound_route, exit_facility_id = _outbound_half(store, state, shipment.id)
        shipments.append(
            {
                "id": shipment.id,
                "status": shipment.status.value,
                "is_transfer": shipment.is_transfer,
                # Stuck at a facility that shut around it: keeps its booking, and
                # only an operator can clear it (cancel or transfer).
                "trapped": shipment.id in trapped_ids,
                "origin_facility_id": shipment.origin_facility_id,
                "origin_label": shipment.origin_label,
                "origin_lat": shipment.origin_lat,
                "origin_lon": shipment.origin_lon,
                "ready_at": _iso(shipment.ready_at),
                "deadline": _iso(shipment.requirements.deadline),
                "size": {d: shipment.size.demand(d) for d in DIMENSIONS},
                # What is actually being moved, in its own units — slots are
                # the capacity currency, not the cargo.
                "lines": [
                    {
                        "sku": line.sku,
                        "group": line.commodity_group,
                        "quantity": line.quantity,
                        "uom": line.uom,
                    }
                    for line in shipment.lines
                ],
                "requirements": {
                    "temp_c": (
                        list(shipment.requirements.temp_c)
                        if shipment.requirements.temp_c is not None
                        else None
                    ),
                    "compat_class": shipment.requirements.compat_class,
                    # The EFFECTIVE dwell: for a delivery the hold is
                    # authoritative and a recorded dwell_days is ignored.
                    "dwell_days": shipment.dwell_days,
                },
                # The CUSTOMER point of an A->B delivery (§7.9) — outside the
                # network, and never the facility `destination` below.
                "destination_point": (
                    {
                        "label": shipment.destination.label,
                        "lat": shipment.destination.lat,
                        "lon": shipment.destination.lon,
                    }
                    if shipment.destination is not None
                    else None
                ),
                "hold_days": shipment.hold_days,
                "destination": (
                    {
                        "facility_id": assigned.facility_id,
                        "zone_id": assigned.zone_ids[0],
                        "eta": _iso(assigned.eta),
                        "departure": _iso(assigned.expected_departure),
                        # Inbound: origin -> holding facility, the transport that
                        # books the stay.
                        "route": list(assigned.route),
                        # Outbound (deliveries only): holding -> exit facility,
                        # whose last mile then reaches `destination_point`.
                        "outbound_route": outbound_route,
                        "exit_facility_id": exit_facility_id,
                        # Every facility the journey touches, with its window and
                        # staging zone — so the map can mark the transit stops of
                        # a selected journey without fetching the decision.
                        "stops": _stop_rows(assigned),
                    }
                    if assigned is not None
                    else None
                ),
            }
        )
    disruptions = [
        {
            "id": d.id,
            "kind": d.kind.value,
            "target_id": d.target_id,
            # The facility the disruption lands on, whether it targets the
            # facility itself, one of its zones, or a lane (None for lanes).
            "facility_id": (
                d.target_id
                if d.target_id in state.facilities
                else state.zones[d.target_id].facility_id
                if d.target_id in state.zones
                else None
            ),
            "detail": d.detail,
            "from_ts": _iso(d.from_ts),
            "until_ts": _iso(d.until_ts),
            "magnitude": d.magnitude,
            "status": "active" if d.active_at(now) else "upcoming",
        }
        for d in sorted(state.disruptions.values(), key=lambda d: d.id)
        if d.ended_at is None and d.until_ts > now
    ]
    return {
        "empty": False,
        "now": _iso(now),
        "seq": state.last_seq,
        "facilities": sorted(facilities, key=lambda f: str(f["id"])),
        "lanes": sorted(lanes, key=lambda x: str(x["id"])),
        "shipments": sorted(shipments, key=lambda s: str(s["id"])),
        "disruptions": disruptions,
        # A plan proposed and not booked (§7.5). Present here so a reload opens
        # in plan review instead of losing what the operator was looking at.
        "pending_plan": pending_plan_summary(state),
    }


def occupancy_timeline(state: NetworkState, facility_id: str, days: int = 14) -> Json:
    """Per-zone daily occupancy vs effective capacity from now forward, plus the
    reservations that shape it — the facility 2D view's data (§11)."""
    now = state.last_ts
    if now is None or facility_id not in state.facilities:
        return {"facility_id": facility_id, "zones": []}
    start = now.astimezone(UTC).date()
    # What each booking IS. A staging dwell (§7.9) is booked from a pass-through
    # stop, so it matches that stop exactly on holder, zone and window; every
    # other booking a holder made is the stay its assignment names. The role
    # lives on the stop, never on the reservation, so this is the only place it
    # can be recovered from.
    stop_roles = {
        (shipment.id, stop.zone_id, stop.arrive, stop.depart): stop.role.value
        for shipment in state.shipments.values()
        if shipment.assigned is not None
        for stop in shipment.assigned.stops
        if stop.books_staging
    }
    zones = []
    for zone in state.zones_of(facility_id):
        buckets = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            occupancy = state.occupancy(zone.id, day)
            capacity = state.effective_capacity(zone.id, day)
            buckets.append(
                {
                    "day": day.isoformat(),
                    "occupancy": {d: occupancy.demand(d) for d in DIMENSIONS},
                    "capacity": {d: capacity.get(d) for d in DIMENSIONS},
                }
            )
        reservations = [
            {
                "id": r.id,
                "holder": r.holder,
                "role": stop_roles.get((r.holder, r.zone_id, r.from_ts, r.until_ts), "hold"),
                "from_ts": _iso(r.from_ts),
                "until_ts": _iso(r.until_ts),
                "size": {d: r.size.demand(d) for d in DIMENSIONS},
            }
            for r in state.reservations_on_zone(zone.id)
        ]
        lots = [
            {
                "id": lot.id,
                "sku": lot.sku,
                "group": lot.commodity_group,
                "quantity": lot.quantity,
                "compat_class": lot.compat_class,
                "planned_departure": _iso(lot.planned_departure),
            }
            for lot in state.lots_in_zone(zone.id)
        ]
        zones.append(
            {
                "id": zone.id,
                "kind": zone.kind,
                "buckets": buckets,
                "reservations": reservations,
                "lots": lots,
            }
        )
    return {"facility_id": facility_id, "zones": zones}


def forecast_inventory(state: NetworkState, config: ObjectiveConfig, days: int = 14) -> Json:
    """§7.7 read model: projected stock per (facility, group) = current plus
    scheduled inbound minus demand-rate drain, day by day, with the cover target."""
    now = state.last_ts
    if now is None:
        return {"facilities": []}
    inbound: dict[tuple[str, str], list[tuple[datetime, int]]] = {}
    for shipment in state.shipments.values():
        if shipment.assigned is None or shipment.status.value not in ("allocated", "in_transit"):
            continue
        for line in shipment.lines:
            key = (shipment.assigned.facility_id, line.commodity_group)
            inbound.setdefault(key, []).append((shipment.assigned.eta, line.quantity))
    start = now.astimezone(UTC).date()
    facilities = []
    for facility_id in sorted(state.demand_rates):
        groups = []
        for group, rate in sorted(state.demand_rates[facility_id].items()):
            stock = state.stock(facility_id, group)
            target = config.cover_days * rate
            arrivals = sorted(inbound.get((facility_id, group), []))
            series = []
            level = float(stock)
            for offset in range(days):
                day = start + timedelta(days=offset)
                day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)
                while arrivals and arrivals[0][0] < day_end:
                    level += arrivals.pop(0)[1]
                if offset > 0:
                    level = max(0.0, level - rate)
                series.append({"day": day.isoformat(), "projected": round(level, 1)})
            groups.append(
                {"group": group, "rate": rate, "target": target, "current": stock, "series": series}
            )
        facilities.append({"facility_id": facility_id, "groups": groups})
    return {"facilities": facilities}


def _delivered_at(store: EventStore, state: NetworkState, shipment_id: str) -> str | None:
    """When the customer gets the goods. The assignment records the hold, not the
    customer, so the last mile's arrival comes back out of the decision record."""
    seq = _decision_seq(store, state, shipment_id)
    if seq is None:
        return None
    record: Any = getattr(next(store.read(from_seq=seq, to_seq=seq)).payload, "record", None)
    if not isinstance(record, dict):
        return None
    itinerary = (record.get("chosen") or {}).get("itinerary")
    if not itinerary:
        return None
    delivered: Any = itinerary.get("delivered_at")
    return str(delivered) if delivered is not None else None


def schedules(state: NetworkState, store: EventStore) -> Json:
    """Upcoming movements: booked departures and expected arrivals, time-ordered.

    One row per window the goods actually occupy (§7.9), not one per shipment: a
    journey's transit dwells are bookings like any other, and a delivery's last
    mile is the movement that ends it. The stay's row keeps the shape it always
    had — `eta`/`departure` are that window's own arrival and departure — and the
    stop fields (`id`, `role`, `arrive`, `depart`) are additive.
    """
    rows: list[Json] = []
    for shipment in state.shipments.values():
        assigned = shipment.assigned
        if assigned is None:
            continue
        base: Json = {
            "shipment_id": shipment.id,
            "status": shipment.status.value,
            "is_transfer": shipment.is_transfer,
            "ready_at": _iso(shipment.ready_at),
            # A delivery's movement ends at the customer, not the hold.
            "customer_label": (
                shipment.destination.label if shipment.destination is not None else None
            ),
        }
        stops = _stop_rows(assigned)
        if not stops:
            # Pre-extension assignments carry no stops; the stay stands in, which
            # is exactly the single row this model always produced.
            stops = [
                {
                    "facility_id": assigned.facility_id,
                    "role": StopRole.HOLD.value,
                    "arrive": _iso(assigned.eta),
                    "depart": _iso(assigned.expected_departure),
                    "zone_id": assigned.zone_ids[0],
                }
            ]
        for index, stop in enumerate(stops):
            rows.append(
                {
                    **base,
                    "id": f"{shipment.id}:{index}",
                    "role": stop["role"],
                    "facility_id": stop["facility_id"],
                    "zone_id": stop["zone_id"],
                    "arrive": stop["arrive"],
                    "depart": stop["depart"],
                    "eta": stop["arrive"],
                    "departure": stop["depart"],
                }
            )
        if shipment.destination is not None:
            delivered = _delivered_at(store, state, shipment.id)
            # A pre-extension record carries no itinerary, so there is no last mile
            # to show: an untimed row would claim a movement nobody scheduled.
            if delivered is not None:
                rows.append(
                    {
                        **base,
                        "id": f"{shipment.id}:last-mile",
                        "role": "last_mile",
                        "facility_id": assigned.exit_facility_id or assigned.facility_id,
                        "zone_id": None,
                        "arrive": delivered,
                        "depart": delivered,
                        "eta": delivered,
                        "departure": delivered,
                    }
                )
    # Untimed rows sort LAST: an unknown instant is not an early one.
    rows.sort(key=lambda r: (r["eta"] is None, r["eta"] or "", r["shipment_id"], r["id"]))
    return {"movements": rows}


def decision_record(store: EventStore, shipment_id: str) -> Json | None:
    """The latest §7.5 record for a shipment, verbatim from the log."""
    seq = store.last_entity_event_seq("shipment", shipment_id, "AllocationDecided")
    if seq is None:
        return None
    envelope = next(store.read(from_seq=seq, to_seq=seq))
    payload = envelope.payload
    record = getattr(payload, "record", None)
    return {
        "seq": seq,
        "ts": _iso(envelope.ts),
        "record": record,
    }


def events_feed(store: EventStore, after_seq: int = 0, limit: int = 100) -> Json:
    """The ticker: recent envelopes, oldest first, without payload bodies."""
    rows: list[Json] = []
    if limit > 0:
        for envelope in store.read(from_seq=after_seq + 1):
            rows.append(
                {
                    "seq": envelope.seq,
                    "ts": _iso(envelope.ts),
                    "type": envelope.type,
                    "entity_type": envelope.entity_type,
                    "entity_id": envelope.entity_id,
                    "actor": envelope.actor,
                    "cause": envelope.cause,
                }
            )
            if len(rows) >= limit:
                break
    return {"events": rows, "head": store.last_seq()}
