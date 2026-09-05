"""Core hard constraints (§7.2).

Industry-neutral: capability tags, certifications, equipment, closures, deadline,
zone fit, temperature, compat classes, segregation, and time-phased capacity.
Vertical restrictions live in packs. Every constraint is evaluated for every
candidate — no short-circuiting — so rejection records are complete.
"""

from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue

from nodal.domain.capacity import DIMENSIONS
from nodal.domain.entities import (
    DisruptionKind,
    Facility,
    Shipment,
    StorageZone,
)
from nodal.events.state import buckets_between
from nodal.rules.framework import AllocationContext, Constraint, ConstraintScope, Reject


def _stay(ctx: AllocationContext) -> tuple[datetime, datetime]:
    assert ctx.eta is not None and ctx.departure is not None, "stay window not set"
    return ctx.eta, ctx.departure


def shipment_classes(shipment: Shipment) -> list[str]:
    """Every compat class the shipment carries: the requirement-level class plus
    any declared on individual lines (goods on a line are just as segregated)."""
    classes = set()
    if shipment.requirements.compat_class is not None:
        classes.add(shipment.requirements.compat_class)
    for line in shipment.lines:
        if line.compat_class is not None:
            classes.add(line.compat_class)
    return sorted(classes)


@dataclass(frozen=True)
class RequiredTagsConstraint:
    """Plain capability/security tags (everything outside cert:/equip: namespaces)."""

    id: str = "REQUIRED_TAGS"
    scope: ConstraintScope = ConstraintScope.FACILITY

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        wanted = [
            t for t in shipment.requirements.required_tags if not t.startswith(("cert:", "equip:"))
        ]
        missing = sorted(set(wanted) - set(facility.tags))
        if missing:
            return Reject(constraint_id=self.id, data={"missing": [*missing]})
        return None


@dataclass(frozen=True)
class CertificationConstraint:
    id: str = "CERT_INVALID"
    scope: ConstraintScope = ConstraintScope.FACILITY

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        eta, _ = _stay(ctx)
        invalid = [
            tag
            for tag in shipment.requirements.required_tags
            if tag.startswith("cert:") and not facility.certified_for(tag, eta)
        ]
        if invalid:
            return Reject(constraint_id=self.id, data={"tags": [*invalid], "eta": eta.isoformat()})
        return None


@dataclass(frozen=True)
class EquipmentConstraint:
    id: str = "EQUIPMENT_DOWN"
    scope: ConstraintScope = ConstraintScope.FACILITY

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        eta, departure = _stay(ctx)
        missing: list[str] = []
        down: list[tuple[str, str]] = []
        for tag in shipment.requirements.required_tags:
            if not tag.startswith("equip:"):
                continue
            if facility.equipment.get(tag, 0) <= 0:
                missing.append(tag)
                continue
            for disruption in ctx.state.disruptions_on(facility.id, eta, departure):
                if disruption.kind is DisruptionKind.EQUIPMENT_DOWN and disruption.detail == tag:
                    down.append((tag, disruption.id))
                    break
        if missing:
            return Reject(constraint_id="EQUIPMENT_MISSING", data={"tags": [*missing]})
        if down:
            return Reject(
                constraint_id=self.id,
                data={"tags": [t for t, _ in down], "disruptions": [d for _, d in down]},
            )
        return None


@dataclass(frozen=True)
class FacilityClosedConstraint:
    """A closure binds the whole window the goods are physically here — which for
    a delivery whose last mile leaves from the holding facility runs past the
    reservation, until that truck is loaded (§7.9). The pass-through stops are
    the delivery router's business; this is the one the survey owns."""

    id: str = "FACILITY_CLOSED"
    scope: ConstraintScope = ConstraintScope.FACILITY

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        eta, departure = _stay(ctx)
        if ctx.delivery is not None:
            for stop in ctx.delivery.stops:
                if stop.facility_id == facility.id:
                    departure = max(departure, stop.depart)
        for disruption in ctx.state.disruptions_on(facility.id, eta, departure):
            if disruption.kind is DisruptionKind.FACILITY_CLOSED:
                return Reject(constraint_id=self.id, data={"disruption": disruption.id})
        return None


@dataclass(frozen=True)
class DeadlineConstraint:
    """The deadline binds where the goods are DUE: at the facility for an
    ordinary allocation, at the customer for a delivery shipment (§7.9) — where
    missing it is a property of routing through this facility, not of arriving."""

    id: str = "DEADLINE_UNREACHABLE"
    scope: ConstraintScope = ConstraintScope.FACILITY

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        deadline = shipment.requirements.deadline
        if deadline is None:
            return None
        eta, _ = _stay(ctx)
        if ctx.delivery is not None:
            delivered_at = ctx.delivery.delivered_at
            if delivered_at > deadline:
                return Reject(
                    constraint_id="DELIVERY_DEADLINE_UNREACHABLE",
                    data={
                        "delivered_at": delivered_at.isoformat(),
                        "deadline": deadline.isoformat(),
                        "destination": ctx.delivery.last_mile.to_label,
                    },
                )
            return None
        if eta > deadline:
            return Reject(
                constraint_id=self.id,
                data={"eta": eta.isoformat(), "deadline": deadline.isoformat()},
            )
        return None


@dataclass(frozen=True)
class ZoneKindConstraint:
    id: str = "ZONE_KIND"
    scope: ConstraintScope = ConstraintScope.ZONE

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        assert zone is not None
        wanted = shipment.requirements.zone_kinds
        if wanted and zone.kind not in wanted:
            return Reject(constraint_id=self.id, data={"kind": zone.kind, "wanted": list(wanted)})
        return None


@dataclass(frozen=True)
class TempRangeConstraint:
    id: str = "TEMP_RANGE"
    scope: ConstraintScope = ConstraintScope.ZONE

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        assert zone is not None
        required = shipment.requirements.temp_c
        if required is None:
            return None
        if zone.temp_c is None or required[0] < zone.temp_c[0] or required[1] > zone.temp_c[1]:
            return Reject(
                constraint_id=self.id,
                data={
                    "required": list(required),
                    "zone_range": list(zone.temp_c) if zone.temp_c else "uncontrolled",
                },
            )
        return None


@dataclass(frozen=True)
class ClassAllowedConstraint:
    id: str = "CLASS_NOT_ALLOWED"
    scope: ConstraintScope = ConstraintScope.ZONE

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        assert zone is not None
        if not zone.allowed_classes:
            return None
        blocked = [c for c in shipment_classes(shipment) if c not in zone.allowed_classes]
        if blocked:
            return Reject(constraint_id=self.id, data={"compat_class": ", ".join(blocked)})
        return None


@dataclass(frozen=True)
class SegregationConstraint:
    """Compat-class segregation against current + reserved zone contents (§7.2, §8)."""

    id: str = "SEGREGATION"
    scope: ConstraintScope = ConstraintScope.ZONE

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        assert zone is not None
        own_classes = shipment_classes(shipment)
        if not own_classes:
            return None
        from nodal.rules.framework import incompatible

        eta, departure = _stay(ctx)
        present: set[str] = set()
        for lot in ctx.state.lots_in_zone(zone.id):
            if lot.compat_class is not None and (
                lot.planned_departure is None or lot.planned_departure > eta
            ):
                present.add(lot.compat_class)
        for reservation in ctx.state.reservations_on_zone(zone.id):
            if not reservation.active_during(eta, departure):
                continue
            holder = ctx.state.shipments.get(reservation.holder)
            if holder is not None:
                present.update(shipment_classes(holder))
        for cls in own_classes:
            conflicts = sorted(c for c in present if incompatible(cls, c, ctx.packs))
            if conflicts:
                return Reject(
                    constraint_id=self.id,
                    data={"compat_class": cls, "conflicts": [*conflicts]},
                )
        return None


@dataclass(frozen=True)
class ZoneOfflineConstraint:
    id: str = "ZONE_OFFLINE"
    scope: ConstraintScope = ConstraintScope.ZONE

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        assert zone is not None
        eta, departure = _stay(ctx)
        for disruption in ctx.state.disruptions_on(zone.id, eta, departure):
            if disruption.kind is DisruptionKind.ZONE_OFFLINE:
                return Reject(constraint_id=self.id, data={"disruption": disruption.id})
        return None


@dataclass(frozen=True)
class CapacityConstraint:
    """Dimensional fit in every bucket the stay overlaps (§5)."""

    id: str = "CAPACITY"
    scope: ConstraintScope = ConstraintScope.ZONE

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        assert zone is not None
        eta, departure = _stay(ctx)
        size = shipment.size
        for day in buckets_between(eta, departure):
            headroom = ctx.headroom_at(zone.id, day)
            for dim in DIMENSIONS:
                need = size.demand(dim)
                if need == 0:
                    continue
                available = headroom.get(dim)
                if available is not None and available < need:
                    data: dict[str, JsonValue] = {
                        "dimension": dim,
                        "need": need,
                        "headroom": available,
                        "day": day.isoformat(),
                    }
                    return Reject(constraint_id=self.id, data=data)
        return None


CORE_CONSTRAINTS: tuple[Constraint, ...] = (
    RequiredTagsConstraint(),
    CertificationConstraint(),
    EquipmentConstraint(),
    FacilityClosedConstraint(),
    DeadlineConstraint(),
    ZoneKindConstraint(),
    TempRangeConstraint(),
    ClassAllowedConstraint(),
    SegregationConstraint(),
    ZoneOfflineConstraint(),
    CapacityConstraint(),
)
