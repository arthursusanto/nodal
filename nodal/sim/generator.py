"""Seeded workload generation (§9).

One `random.Random(seed)` stream, consumed in a fixed order (topology, then demand,
then disruptions), so a scenario + seed fully determines the workload. Weekday
modulation uses thinning over a homogeneous Poisson process, which stays
deterministic under the seeded stream.
"""

import random
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import (
    Certification,
    Disruption,
    DisruptionKind,
    Facility,
    Lane,
    LotSpec,
    RequirementSet,
    Shipment,
    StorageZone,
)
from nodal.domain.units import kg
from nodal.events import catalog as ev
from nodal.events.envelope import EventDraft, EventPayload
from nodal.network.travel import haversine_km
from nodal.sim.scenarios import ScenarioSpec


class GeneratedWorkload(BaseModel):
    """Everything the runner schedules: world events at start, plus timed plans."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    world_drafts: list[EventDraft]
    shipment_plan: list[tuple[datetime, Shipment]] = Field(default_factory=list)
    disruption_plan: list[Disruption] = Field(default_factory=list)


def _uniform_int(rng: random.Random, lo: int, hi: int) -> int:
    return rng.randint(lo, max(lo, hi))


def generate(spec: ScenarioSpec) -> GeneratedWorkload:
    rng = random.Random(spec.seed)
    drafts: list[EventDraft] = []
    start = spec.start
    topology = spec.topology
    demand = spec.demand

    def emit(payload: EventPayload) -> None:
        drafts.append(EventDraft(ts=start, payload=payload))

    # -- topology ---------------------------------------------------------------
    facilities: list[Facility] = []
    cold_facilities: list[str] = []
    for f in range(topology.facilities):
        facility_id = f"FAC-{f:03d}"
        tags = ["equip:forklift"]
        if rng.random() < topology.crossdock_prob:
            tags.append("cross-dock")
        if rng.random() < topology.fenced_prob:
            tags.append("security:fenced")
        certifications = []
        if rng.random() < topology.coldchain_cert_prob:
            certifications.append(
                Certification(
                    tag="cert:coldchain",
                    valid_from=start,
                    valid_until=start + timedelta(days=730),
                )
            )
        # Guarded draw: at prob 0 no random number is consumed, so pre-existing
        # seed-pinned scenarios keep their published streams byte-identical.
        if topology.hazmat_cert_prob > 0 and rng.random() < topology.hazmat_cert_prob:
            certifications.append(
                Certification(
                    tag="cert:hazmat",
                    valid_from=start,
                    valid_until=start + timedelta(days=730),
                )
            )
        facility = Facility(
            id=facility_id,
            name=f"Facility {f:03d}",
            lat=rng.uniform(*topology.lat_range),
            lon=rng.uniform(*topology.lon_range),
            tags=tags,
            equipment={"equip:forklift": _uniform_int(rng, 1, topology.forklift_count_max)},
            certifications=certifications,
            risk_factor=round(rng.uniform(0.0, topology.risk_max), 3),
        )
        facilities.append(facility)
        emit(ev.FacilityRegistered(facility=facility))

        zone_count = _uniform_int(rng, topology.zones_min, topology.zones_max)
        has_cold = False
        for z in range(zone_count):
            capacity = CapacityVector(
                slots=_uniform_int(rng, topology.slot_capacity_min, topology.slot_capacity_max),
                weight_g=kg(_uniform_int(rng, 100_000, 400_000)),
            )
            kind = "rack"
            temp_c: tuple[int, int] | None = None
            if not has_cold and rng.random() < topology.cold_zone_prob:
                kind, temp_c, has_cold = "cold", (-25, 5), True
            elif rng.random() < topology.bulk_zone_prob:
                kind = "bulk"
            emit(
                ev.ZoneRegistered(
                    zone=StorageZone(
                        id=f"ZON-{f:03d}-{z}",
                        facility_id=facility_id,
                        kind=kind,
                        capacity=capacity,
                        temp_c=temp_c,
                    )
                )
            )
        if has_cold:
            cold_facilities.append(facility_id)

        for group in demand.commodity_groups:
            rate = _uniform_int(rng, topology.demand_rate_min, topology.demand_rate_max)
            if rate > 0:
                emit(ev.DemandRateSet(facility_id=facility_id, commodity_group=group, per_day=rate))

    # Lanes: each facility connects to its k nearest neighbours, both directions.
    lane_ids = set()
    for facility in facilities:
        by_distance = sorted(
            (other for other in facilities if other.id != facility.id),
            key=lambda other: (
                haversine_km(facility.lat, facility.lon, other.lat, other.lon),
                other.id,
            ),
        )
        for other in by_distance[: topology.lanes_nearest]:
            for a, b in ((facility, other), (other, facility)):
                lane_id = f"LANE-{a.id}-{b.id}"
                if lane_id in lane_ids:
                    continue
                lane_ids.add(lane_id)
                km_dist = haversine_km(a.lat, a.lon, b.lat, b.lon) * 1.25
                emit(
                    ev.LaneRegistered(
                        lane=Lane(
                            id=lane_id,
                            from_facility_id=a.id,
                            to_facility_id=b.id,
                            distance_km=round(km_dist, 1),
                            minutes=max(30, round(km_dist / topology.lane_speed_kmh * 60)),
                            cost_fixed_cents=topology.lane_cost_fixed_cents,
                            cost_per_kg_cents=topology.lane_cost_per_kg_cents,
                        )
                    )
                )

    # -- demand (thinned Poisson arrivals over the horizon) -----------------------
    shipment_plan: list[tuple[datetime, Shipment]] = []
    max_factor = max(demand.weekday_factors) or 1.0
    rate_per_hour = demand.shipments_per_day * max_factor / 24.0
    t = start
    horizon_end = start + timedelta(days=spec.horizon_days)
    n = 0
    while True:
        if rate_per_hour <= 0:
            break
        t = t + timedelta(hours=rng.expovariate(rate_per_hour))
        if t >= horizon_end:
            break
        factor = demand.weekday_factors[t.weekday()]
        if rng.random() >= (factor / max_factor):
            continue  # thinned out
        n += 1
        sid = f"SHP-{n:04d}"
        slots = _uniform_int(rng, demand.slots_min, demand.slots_max)
        weight = slots * _uniform_int(
            rng, demand.weight_per_slot_kg_min, demand.weight_per_slot_kg_max
        )
        group = demand.commodity_groups[rng.randrange(len(demand.commodity_groups))]
        line_temp: tuple[int, int] | None = None
        if rng.random() < demand.temp_controlled_prob:
            line_temp = (-25, -18) if rng.random() < demand.frozen_prob else (-5, 4)
        compat_class: str | None = None
        for cls in sorted(demand.compat_class_probs):
            probability = demand.compat_class_probs[cls]
            if probability > 0 and rng.random() < probability:
                compat_class = cls
                break  # at most one class per shipment (§8)
        deadline = None
        if rng.random() < demand.deadline_prob:
            deadline = t + timedelta(
                hours=_uniform_int(rng, demand.deadline_hours_min, demand.deadline_hours_max)
            )
        size = CapacityVector(slots=slots, volume_l=0, weight_g=kg(weight))
        origin_facility: str | None = None
        origin_lat = origin_lon = None
        if facilities and rng.random() < demand.origin_from_facility_prob:
            origin_facility = facilities[rng.randrange(len(facilities))].id
        else:
            origin_lat = rng.uniform(*topology.lat_range)
            origin_lon = rng.uniform(*topology.lon_range)
        shipment = Shipment(
            id=sid,
            origin_facility_id=origin_facility,
            origin_label=None if origin_facility else f"GATE-{n % 7}",
            origin_lat=origin_lat,
            origin_lon=origin_lon,
            lines=[
                LotSpec(
                    sku=f"SKU-{n % 200}",
                    commodity_group=group,
                    quantity=slots,
                    size=size,
                    compat_class=compat_class,
                )
            ],
            requirements=RequirementSet(
                size=size,
                required_tags=["equip:forklift"],
                temp_c=line_temp,
                deadline=deadline,
                dwell_days=_uniform_int(rng, demand.dwell_days_min, demand.dwell_days_max),
                compat_class=compat_class,
            ),
            ready_at=t,
        )
        # Booking lead (§9): the order is KNOWN before it is ready to move, so
        # decisions can book ahead — the window §7.6 re-optimization acts in.
        registered = max(spec.start, t - timedelta(hours=demand.booking_lead_hours))
        shipment_plan.append((registered, shipment))

    # -- disruptions ---------------------------------------------------------------
    disruption_plan: list[Disruption] = []
    spec_d = spec.disruptions
    n_d = 0
    for kind, per_30d in (
        (DisruptionKind.FACILITY_CLOSED, spec_d.closures_per_30d),
        (DisruptionKind.CAPACITY_REDUCED, spec_d.capacity_cuts_per_30d),
    ):
        if per_30d <= 0 or not facilities:
            continue
        rate_per_day = per_30d / 30.0
        t = start
        while True:
            t = t + timedelta(days=rng.expovariate(rate_per_day))
            if t >= horizon_end:
                break
            n_d += 1
            if kind is DisruptionKind.FACILITY_CLOSED:
                target = facilities[rng.randrange(len(facilities))].id
                days = _uniform_int(rng, spec_d.closure_days_min, spec_d.closure_days_max)
                magnitude = 1.0
            else:
                facility = facilities[rng.randrange(len(facilities))]
                target = f"ZON-{facility.id.removeprefix('FAC-')}-0"
                days = _uniform_int(rng, spec_d.cut_days_min, spec_d.cut_days_max)
                magnitude = round(
                    rng.uniform(spec_d.cut_magnitude_min, spec_d.cut_magnitude_max), 2
                )
            disruption_plan.append(
                Disruption(
                    id=f"DIS-{n_d:03d}",
                    kind=kind,
                    target_id=target,
                    from_ts=t,
                    until_ts=t + timedelta(days=days),
                    magnitude=magnitude,
                )
            )

    return GeneratedWorkload(
        world_drafts=drafts,
        shipment_plan=shipment_plan,
        disruption_plan=disruption_plan,
    )
