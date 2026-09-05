"""World ingestion: declarative YAML -> events (§4, stage 1).

A world file describes the network as of a start instant; the loader turns it into
the corresponding event batch. Human-friendly units in YAML (kg, m3, currency units)
convert to canonical integers here, at the ingestion boundary (§2).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from nodal.domain.calendar import DayWindow, OperatingCalendar
from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import (
    Certification,
    Destination,
    Disruption,
    Facility,
    InventoryLot,
    Lane,
    LotSpec,
    RequirementSet,
    Shipment,
    StorageZone,
)
from nodal.domain.units import currency, kg, m3
from nodal.events import catalog as ev
from nodal.events.envelope import EventDraft, EventPayload
from nodal.events.store import EventStore


class WorldError(Exception):
    pass


def _ts(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise WorldError(f"{field}: expected ISO timestamp, got {value!r}")


def _capacity(raw: dict[str, Any] | None) -> CapacityVector:
    if not raw:
        return CapacityVector()
    known = {"slots", "volume_m3", "weight_kg", "volume_l", "weight_g"}
    unknown = set(raw) - known
    if unknown:
        raise WorldError(f"unknown capacity dimensions: {sorted(unknown)}")
    volume = raw.get("volume_l", m3(raw["volume_m3"]) if "volume_m3" in raw else None)
    weight = raw.get("weight_g", kg(raw["weight_kg"]) if "weight_kg" in raw else None)
    return CapacityVector(slots=raw.get("slots"), volume_l=volume, weight_g=weight)


def _calendar(raw: dict[str, Any] | None) -> OperatingCalendar | None:
    if raw is None:
        return None

    def windows(items: list[Any]) -> list[DayWindow]:
        result = []
        for item in items:
            start, end = str(item).split("-")
            sh, sm = [*start.split(":"), "0"][:2]
            eh, em = [*end.split(":"), "0"][:2]
            result.append(
                DayWindow(start_minute=int(sh) * 60 + int(sm), end_minute=int(eh) * 60 + int(em))
            )
        return result

    week = {int(day): windows(items) for day, items in (raw.get("week") or {}).items()}
    exceptions = {str(day): windows(items) for day, items in (raw.get("exceptions") or {}).items()}
    return OperatingCalendar(week=week, exceptions=exceptions)


def _requirements(raw: dict[str, Any], deadline_default: datetime | None) -> RequirementSet:
    temp = raw.get("temp_c")
    return RequirementSet(
        size=_capacity(raw.get("size")),
        required_tags=list(raw.get("required_tags") or []),
        zone_kinds=list(raw.get("zone_kinds") or []),
        temp_c=(int(temp[0]), int(temp[1])) if temp else None,
        compat_class=raw.get("compat_class"),
        deadline=_ts(raw["deadline"], "deadline") if raw.get("deadline") else deadline_default,
        dwell_days=raw.get("dwell_days"),
        attributes=dict(raw.get("attributes") or {}),
    )


def load_world(
    path: str | Path,
    store: EventStore,
    actor: str = "world",
    packs: list[Any] | None = None,
) -> int:
    """Append the world's events to `store`. Returns the number of events.

    `packs` (§8): when given, every attribute bag in the file — zone, lot,
    shipment line, and shipment requirements — is validated against the packs'
    attribute schemas before a single event is appended; a bad bag raises
    WorldError and nothing lands.
    """
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise WorldError("world file must be a mapping")
    start = _ts(raw.get("start", "2026-01-01T00:00:00+00:00"), "start")
    drafts: list[EventDraft] = []

    def emit(payload: EventPayload, ts: datetime | None = None) -> None:
        drafts.append(EventDraft(ts=ts or start, payload=payload))

    def check_attributes(bag: dict[str, Any], where: str) -> None:
        if not packs or not bag:
            return
        from nodal.rules.framework import validate_attributes

        try:
            validate_attributes(bag, packs)
        except ValueError as err:
            raise WorldError(f"{where}: {err}") from err

    for f in raw.get("facilities") or []:
        facility = Facility(
            id=f["id"],
            name=f.get("name", f["id"]),
            lat=float(f["lat"]),
            lon=float(f["lon"]),
            tz=f.get("tz", "UTC"),
            tags=list(f.get("tags") or []),
            equipment=dict(f.get("equipment") or {}),
            certifications=[
                Certification(
                    tag=c["tag"],
                    valid_from=_ts(c.get("valid_from", start), "valid_from"),
                    valid_until=_ts(c["valid_until"], "valid_until"),
                )
                for c in f.get("certifications") or []
            ],
            calendar=_calendar(f.get("calendar")),
            risk_factor=float(f.get("risk", 0.0)),
        )
        emit(ev.FacilityRegistered(facility=facility))
        for z in f.get("zones") or []:
            temp = z.get("temp_c")
            zone = StorageZone(
                id=z["id"],
                facility_id=facility.id,
                kind=z.get("kind", "rack"),
                capacity=_capacity(z.get("capacity")),
                temp_c=(int(temp[0]), int(temp[1])) if temp else None,
                allowed_classes=list(z.get("classes") or []),
                attributes=dict(z.get("attributes") or {}),
            )
            check_attributes(zone.attributes, f"zone {zone.id}")
            emit(ev.ZoneRegistered(zone=zone))

    for lane_raw in raw.get("lanes") or []:
        lane = Lane(
            id=lane_raw["id"],
            from_facility_id=lane_raw["from"],
            to_facility_id=lane_raw["to"],
            mode=lane_raw.get("mode", "road"),
            distance_km=float(lane_raw["km"]),
            minutes=int(lane_raw["minutes"]),
            cost_fixed_cents=currency(float(lane_raw.get("cost_fixed", 0))),
            cost_per_kg_cents=float(lane_raw.get("cost_per_kg_cents", 0.0)),
            cost_per_m3_cents=float(lane_raw.get("cost_per_m3_cents", 0.0)),
            path=(
                [(float(lon), float(lat)) for lon, lat in lane_raw["path"]]
                if lane_raw.get("path")
                else None
            ),
        )
        emit(ev.LaneRegistered(lane=lane))

    for d in raw.get("demand_rates") or []:
        emit(
            ev.DemandRateSet(
                facility_id=d["facility"],
                commodity_group=d["group"],
                per_day=int(d["per_day"]),
            )
        )

    for lot_raw in raw.get("lots") or []:
        departure = lot_raw.get("planned_departure")
        lot = InventoryLot(
            id=lot_raw["id"],
            sku=lot_raw.get("sku", "SKU"),
            commodity_group=lot_raw.get("group", "general"),
            quantity=int(lot_raw.get("quantity", 1)),
            uom=lot_raw.get("uom", "unit"),
            size=_capacity(lot_raw.get("size")),
            compat_class=lot_raw.get("compat_class"),
            attributes=dict(lot_raw.get("attributes") or {}),
            zone_id=lot_raw["zone"],
            received_at=start,
            planned_departure=_ts(departure, "planned_departure") if departure else None,
        )
        check_attributes(lot.attributes, f"lot {lot.id}")
        emit(ev.LotReceived(lot=lot))

    for s in raw.get("shipments") or []:
        ready = _ts(s.get("ready", start), "ready")
        deadline = _ts(s["deadline"], "deadline") if s.get("deadline") else None
        lines = [
            LotSpec(
                sku=line.get("sku", "SKU"),
                commodity_group=line.get("group", "general"),
                quantity=int(line.get("quantity", 1)),
                uom=line.get("uom", "unit"),
                size=_capacity(line.get("size")),
                compat_class=line.get("compat_class"),
                attributes=dict(line.get("attributes") or {}),
            )
            for line in s.get("lines") or []
        ]
        for line_spec in lines:
            check_attributes(line_spec.attributes, f"shipment {s['id']} line {line_spec.sku}")
        requirements = _requirements(dict(s.get("requirements") or {}), deadline)
        check_attributes(requirements.attributes, f"shipment {s['id']} requirements")
        if requirements.size.is_zero() and lines:
            total = CapacityVector(slots=0, volume_l=0, weight_g=0)
            for line in lines:
                total = total.plus(line.size)
            requirements = requirements.model_copy(update={"size": total})
        raw_destination = s.get("destination")
        if raw_destination is not None and not isinstance(raw_destination, dict):
            raise WorldError(f"shipment {s['id']}: destination must be a mapping")
        shipment = Shipment(
            id=s["id"],
            origin_facility_id=s.get("origin"),
            origin_label=s.get("origin_label"),
            origin_lat=s.get("origin_lat"),
            origin_lon=s.get("origin_lon"),
            lines=lines,
            requirements=requirements,
            ready_at=ready,
            destination=(
                Destination(
                    label=raw_destination.get("label", s["id"]),
                    lat=float(raw_destination["lat"]),
                    lon=float(raw_destination["lon"]),
                )
                if raw_destination is not None
                else None
            ),
            hold_days=int(s.get("hold_days", 0)),
        )
        emit(ev.ShipmentRegistered(shipment=shipment), ready if ready >= start else start)

    for d in raw.get("disruptions") or []:
        from_ts = _ts(d["from"], "from")
        disruption = Disruption(
            id=d["id"],
            kind=d["kind"],
            target_id=d["target"],
            detail=d.get("detail"),
            from_ts=from_ts,
            until_ts=_ts(d["until"], "until") if d.get("until") else from_ts + timedelta(days=1),
            magnitude=float(d.get("magnitude", 1.0)),
        )
        # Announced at world start; activity is governed by [from_ts, until_ts).
        emit(ev.DisruptionStarted(disruption=disruption))

    drafts.sort(key=lambda draft: draft.ts)
    store.append(drafts, actor=actor)
    return len(drafts)
