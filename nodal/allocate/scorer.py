"""Objective components (§7.3).

One vocabulary, two evaluation contexts: these same functions serve single-shipment
scoring here and reappear as the CP-SAT batch objective's terms (stage 4). Components
carry raw value, normalized value, weight, and contribution — the explanation is the
actual arithmetic.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from nodal.allocate.config import ObjectiveConfig
from nodal.allocate.records import ComponentScore
from nodal.domain.capacity import DIMENSIONS, CapacityVector
from nodal.domain.entities import Facility, Shipment, StorageZone
from nodal.events.state import NetworkState, buckets_between
from nodal.network.journey import DeliveryPlan
from nodal.network.travel import Route
from nodal.rules.framework import Pack, PackStay

# Components whose contribution is a constant of the (shipment, zone) pair — the
# batch objective sums exactly these per pair; `congestion` and `inv_balance` are
# facility-level functions and get their own convex terms in the batch model (§7.4).
SEPARABLE_COMPONENTS = (
    "transport_cost",
    "travel_time",
    "lateness_risk",
    "capacity_preservation",
    "transfers",
    "op_risk",
)


@dataclass(frozen=True)
class ScoringPrecomputed:
    """Per-allocation precomputation shared across candidates (determinism + speed)."""

    # (facility, commodity_group) -> quantity already committed inbound.
    inbound: dict[tuple[str, str], int]
    # required tag -> number of facilities offering it (scarcity, §7.3).
    providers: dict[str, int]


class EvalCache:
    """Per-decision memo of time-phased queries (§15). One solve reads occupancy,
    effective capacity, and stock thousands of times against a single immutable
    state snapshot — memoizing them is what makes batch prep scale."""

    def __init__(self, state: NetworkState) -> None:
        self.state = state
        self._occupancy: dict[tuple[str, date], CapacityVector] = {}
        self._capacity: dict[tuple[str, date], CapacityVector] = {}
        self._stock: dict[tuple[str, str], int] = {}
        self._peak_baseline: dict[tuple[str, tuple[date, ...]], float] = {}

    def occupancy(self, zone_id: str, day: date) -> CapacityVector:
        key = (zone_id, day)
        if key not in self._occupancy:
            self._occupancy[key] = self.state.occupancy(zone_id, day)
        return self._occupancy[key]

    def effective_capacity(self, zone_id: str, day: date) -> CapacityVector:
        key = (zone_id, day)
        if key not in self._capacity:
            self._capacity[key] = self.state.effective_capacity(zone_id, day)
        return self._capacity[key]

    def headroom(self, zone_id: str, day: date) -> CapacityVector:
        return self.effective_capacity(zone_id, day).minus_demand(self.occupancy(zone_id, day))

    def stock(self, facility_id: str, group: str) -> int:
        key = (facility_id, group)
        if key not in self._stock:
            self._stock[key] = self.state.stock(facility_id, group)
        return self._stock[key]

    def facility_peak_baseline(self, facility_id: str, days: tuple[date, ...]) -> float:
        """Facility peak utilization over a bucket window, without any placement —
        the baseline the marginal congestion component subtracts (§7.3)."""
        key = (facility_id, days)
        if key not in self._peak_baseline:
            peak = 0.0
            for zone in self.state.zones_of(facility_id):
                for day in days:
                    capacity = self.effective_capacity(zone.id, day)
                    occupancy = self.occupancy(zone.id, day)
                    for dim in DIMENSIONS:
                        cap = capacity.get(dim)
                        if cap is None:
                            continue
                        used = occupancy.demand(dim)
                        ratio = (1.0 if used > 0 else 0.0) if cap <= 0 else used / cap
                        peak = max(peak, ratio)
            self._peak_baseline[key] = peak
        return self._peak_baseline[key]


def precompute_shared(
    state: NetworkState, shipments: dict[str, Shipment], now: datetime
) -> dict[str, ScoringPrecomputed]:
    """Batch-shared precompute: inbound is identical for every planned shipment
    (planned work is never counted), and provider counts depend only on the tag
    set — one state scan each instead of one per shipment."""
    inbound: dict[tuple[str, str], int] = {}
    for other in state.shipments.values():
        if other.assigned is None or other.status.value not in ("allocated", "in_transit"):
            continue
        for line in other.lines:
            key = (other.assigned.facility_id, line.commodity_group)
            inbound[key] = inbound.get(key, 0) + line.quantity
    providers_memo: dict[tuple[str, ...], dict[str, int]] = {}
    result: dict[str, ScoringPrecomputed] = {}
    for sid in sorted(shipments):
        tags = tuple(shipments[sid].requirements.required_tags)
        if tags not in providers_memo:
            providers: dict[str, int] = {}
            for tag in tags:
                count = 0
                for facility in state.facilities.values():
                    if tag.startswith("cert:"):
                        offered = facility.certified_for(tag, now)
                    elif tag.startswith("equip:"):
                        offered = facility.equipment.get(tag, 0) > 0
                    else:
                        offered = tag in facility.tags
                    if offered:
                        count += 1
                providers[tag] = count
            providers_memo[tags] = providers
        result[sid] = ScoringPrecomputed(inbound=inbound, providers=providers_memo[tags])
    return result


def precompute(state: NetworkState, shipment: Shipment, now: datetime) -> ScoringPrecomputed:
    inbound: dict[tuple[str, str], int] = {}
    for other in state.shipments.values():
        if other.id == shipment.id or other.assigned is None:
            continue
        if other.status.value not in ("allocated", "in_transit"):
            continue
        for line in other.lines:
            key = (other.assigned.facility_id, line.commodity_group)
            inbound[key] = inbound.get(key, 0) + line.quantity
    providers: dict[str, int] = {}
    for tag in shipment.requirements.required_tags:
        count = 0
        for facility in state.facilities.values():
            if tag.startswith("cert:"):
                offered = facility.certified_for(tag, now)
            elif tag.startswith("equip:"):
                offered = facility.equipment.get(tag, 0) > 0
            else:
                offered = tag in facility.tags
            if offered:
                count += 1
        providers[tag] = count
    return ScoringPrecomputed(inbound=inbound, providers=providers)


def facility_peak_util_after(
    state: NetworkState,
    facility: Facility,
    zone: StorageZone,
    size: CapacityVector,
    eta: datetime,
    departure: datetime,
    cache: EvalCache | None = None,
) -> float:
    """Peak utilization of the facility over the stay, with the placement applied."""
    if cache is None:
        cache = EvalCache(state)
    days = buckets_between(eta, departure)
    peak = 0.0
    for candidate_zone in state.zones_of(facility.id):
        for day in days:
            capacity = cache.effective_capacity(candidate_zone.id, day)
            occupancy = cache.occupancy(candidate_zone.id, day)
            if candidate_zone.id == zone.id:
                occupancy = occupancy.plus(size)
            for dim in DIMENSIONS:
                cap = capacity.get(dim)
                if cap is None:
                    continue
                used = occupancy.demand(dim)
                ratio = (1.0 if used > 0 else 0.0) if cap <= 0 else used / cap
                peak = max(peak, ratio)
    return peak


def compute_components(
    state: NetworkState,
    shipment: Shipment,
    facility: Facility,
    zone: StorageZone,
    route: Route,
    wait_minutes: int,
    eta: datetime,
    departure: datetime,
    config: ObjectiveConfig,
    pre: ScoringPrecomputed,
    cache: EvalCache | None = None,
    packs: Sequence[Pack] = (),
    delivery: DeliveryPlan | None = None,
) -> dict[str, ComponentScore]:
    """`delivery` (§7.9) adds the outbound half — lanes out plus the last-mile
    road leg — to the transport terms, and moves the lateness reference from
    arrival at the facility to arrival at the customer. It is a constant of the
    (shipment, zone) pair like everything else here, so the separable components
    stay separable and the batch model keeps pricing them exactly."""
    if cache is None:
        cache = EvalCache(state)
    size = shipment.size
    weights = config.weights
    components: dict[str, ComponentScore] = {}

    def add(name: str, raw: float, normalized: float, baseline: float | None = None) -> None:
        weight = weights.get(name)
        components[name] = ComponentScore(
            raw=raw,
            normalized=normalized,
            weight=weight,
            contribution=weight * normalized,
            baseline=baseline,
        )

    # transport_cost (inbound, plus the outbound half of a delivery)
    cost = float(route.cost_cents + (delivery.outbound_cost_cents if delivery else 0))
    add("transport_cost", cost, cost / config.cost_ref_cents)

    # travel_time (transit + destination operating-window wait)
    minutes = float(route.minutes + wait_minutes + (delivery.outbound_minutes if delivery else 0))
    add("travel_time", minutes, minutes / config.time_ref_minutes)

    # lateness_risk, against arrival where the goods are due
    deadline = shipment.requirements.deadline
    due_at = delivery.delivered_at if delivery else eta
    if deadline is None:
        add("lateness_risk", 0.0, 0.0)
    else:
        slack_minutes = (deadline - due_at).total_seconds() / 60
        risk = max(0.0, (config.buffer_ref_minutes - slack_minutes) / config.buffer_ref_minutes)
        add("lateness_risk", slack_minutes, risk)

    # congestion: MARGINAL system cost of this placement — the increase in the
    # facility's convex congestion penalty caused by it (§7.3 as amended: the
    # batch objective sums these same marginals, so its argmin and the scorer's
    # coincide for every config; an absolute-level definition would not compose).
    stay_days = tuple(buckets_between(eta, departure))
    peak_after = facility_peak_util_after(state, facility, zone, size, eta, departure, cache)
    peak_before = cache.facility_peak_baseline(facility.id, stay_days)
    congestion_marginal = config.congestion_penalty(peak_after) - config.congestion_penalty(
        peak_before
    )
    add("congestion", peak_after, congestion_marginal, baseline=peak_before)

    # inv_balance: MARGINAL change in |committed stock - target| / target, summed
    # over the shipment's targeted groups. Negative when the placement improves
    # balance — which is exactly how a rebalancing transfer earns its move (§7.7).
    balance_after = 0.0
    balance_before = 0.0
    for group in sorted({line.commodity_group for line in shipment.lines}):
        rate = state.demand_rate(facility.id, group)
        if rate <= 0:
            continue  # no target -> no balance signal for this group
        target = float(config.cover_days * rate)
        committed = cache.stock(facility.id, group) + pre.inbound.get((facility.id, group), 0)
        incoming = sum(line.quantity for line in shipment.lines if line.commodity_group == group)
        after = committed + incoming
        balance_after += abs(after - target) / max(target, 1.0)
        balance_before += abs(committed - target) / max(target, 1.0)
    add("inv_balance", balance_after, balance_after - balance_before, baseline=balance_before)

    # capacity_preservation: scarcity-weighted max consumed/headroom_before
    worst_ratio = 0.0
    for day in buckets_between(eta, departure):
        headroom = cache.headroom(zone.id, day)
        for dim in DIMENSIONS:
            need = size.demand(dim)
            if need == 0:
                continue
            available = headroom.get(dim)
            if available is not None and available > 0:
                worst_ratio = max(worst_ratio, need / available)
    scarce = any(
        0 < pre.providers.get(tag, 0) <= config.scarcity_k
        for tag in shipment.requirements.required_tags
    )
    multiplier = 1.0 + (config.scarcity_bonus if scarce else 0.0)
    add("capacity_preservation", worst_ratio, min(worst_ratio, 1.0) * multiplier)

    # transfers (every leg after the first hands the goods over once more)
    transfers = float(route.transfers + (delivery.outbound_leg_count if delivery else 0))
    add("transfers", transfers, transfers)

    # op_risk: facility risk factor + disruption adjacency during the stay
    adjacency = 0.0
    zone_ids = {z.id for z in state.zones_of(facility.id)}
    for disruption in state.disruptions.values():
        if (
            disruption.target_id == facility.id or disruption.target_id in zone_ids
        ) and disruption.active_during(eta, departure):
            adjacency = config.disruption_adjacency_risk
            break
    risk_raw = facility.risk_factor + adjacency
    add("op_risk", risk_raw, risk_raw)

    # Pack objective components (§8): separable by contract, so they join the
    # candidate's separable total and the batch model prices them exactly.
    # Weight: the profile's pack_weights[name], else the pack's default.
    if packs:
        stay = PackStay(route=route, eta=eta, departure=departure)
        for pack in packs:
            for component in pack.objective_components:
                raw, normalized = component.compute(shipment, facility, zone, stay)
                weight = config.pack_weights.get(component.name, component.default_weight)
                components[component.name] = ComponentScore(
                    raw=raw,
                    normalized=normalized,
                    weight=weight,
                    contribution=weight * normalized,
                )

    return components


def total_score(components: dict[str, ComponentScore]) -> float:
    return sum(score.contribution for score in components.values())
