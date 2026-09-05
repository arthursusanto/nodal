"""Rebalancing transfer generation (§7.7).

A (facility, group) whose stock exceeds its target by the configured surplus ratio
donates: the oldest surplus lots become a transfer shipment (ordinary shipment,
`is_transfer=True`, soft deadline). Transfers enter the same batch solve as every
other shipment — a move happens exactly when its balance gain beats its transport
cost, and the decision record shows that arithmetic like any other allocation.
Deterministic: donors are visited in sorted order, lots oldest-first.
"""

from datetime import datetime

from nodal.allocate.config import ObjectiveConfig
from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import LotSpec, RequirementSet, Shipment
from nodal.events.state import NetworkState


def generate_rebalancing_transfers(
    state: NetworkState,
    config: ObjectiveConfig,
    now: datetime,
    id_prefix: str,
) -> list[Shipment]:
    """Propose up to `rebalance_max_per_cycle` transfers from over-target donors."""
    proposals: list[tuple[float, str, str]] = []  # (surplus_ratio desc via sort, facility, group)
    for facility_id in sorted(state.demand_rates):
        # Nothing leaves a closed facility (§7.9), so a transfer out of one is a
        # move that cannot happen: the engine rejects every candidate for it as
        # ORIGIN_CLOSED anyway, and proposing it would claim lots and burn a slot
        # in this cycle's cap that a donor which CAN ship could have used.
        if state.departure_closure(facility_id, now) is not None:
            continue
        for group, rate in sorted(state.demand_rates[facility_id].items()):
            if rate <= 0:
                continue
            target = float(config.cover_days * rate)
            stock = state.stock(facility_id, group)
            surplus_ratio = (stock - target) / max(target, 1.0)
            if surplus_ratio > config.rebalance_surplus_ratio:
                proposals.append((-surplus_ratio, facility_id, group))
    proposals.sort()

    # Lots already committed to a live transfer must not be claimed again — a
    # double claim would leave the second LotShipped unfoldable.
    claimed: set[str] = set()
    for shipment in state.shipments.values():
        if shipment.is_transfer:
            claimed.update(shipment.transfer_lot_ids)

    transfers: list[Shipment] = []
    for index, (_neg_ratio, facility_id, group) in enumerate(
        proposals[: config.rebalance_max_per_cycle]
    ):
        rate = state.demand_rate(facility_id, group)
        target = float(config.cover_days * rate)
        stock = state.stock(facility_id, group)
        surplus_units = int(stock - target)
        if surplus_units <= 0:
            continue
        lots = [
            lot
            for zone in state.zones_of(facility_id)
            for lot in state.lots_in_zone(zone.id)
            if lot.commodity_group == group and lot.quantity > 0 and lot.id not in claimed
        ]
        lots.sort(key=lambda lot: ((lot.received_at or now), lot.id))
        picked: list[str] = []
        lines: list[LotSpec] = []
        remaining = surplus_units
        total_size = CapacityVector(slots=0, volume_l=0, weight_g=0)
        for lot in lots:
            if remaining <= 0:
                break
            if lot.quantity > remaining:
                continue  # v1 moves whole lots only; skip lots larger than the surplus
            picked.append(lot.id)
            remaining -= lot.quantity
            total_size = total_size.plus(lot.size)
            lines.append(
                LotSpec(
                    sku=lot.sku,
                    commodity_group=lot.commodity_group,
                    quantity=lot.quantity,
                    uom=lot.uom,
                    size=lot.size,
                    compat_class=lot.compat_class,
                    attributes=lot.attributes,
                )
            )
        if not picked:
            continue
        transfers.append(
            Shipment(
                id=f"{id_prefix}-{index + 1}",
                origin_facility_id=facility_id,
                lines=lines,
                requirements=RequirementSet(size=total_size, required_tags=[]),
                ready_at=now,
                is_transfer=True,
                transfer_lot_ids=picked,
            )
        )
    return transfers
