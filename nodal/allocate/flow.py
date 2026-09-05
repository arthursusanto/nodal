"""Min-cost-flow fast path and the flow-relax heuristic (§7.4).

The fast path is *exact* only under a narrow, explicitly checked structural gate;
it is never silently substituted where time-phasing, multi-dimensionality, or
segregation matter. Outside the gate, the same transportation construction with
deliberately conservative capacities serves as the `flow-relax` baseline policy —
a heuristic, labeled as such (its solver metadata carries no optimality proof).
"""

from dataclasses import dataclass
from datetime import date

from ortools.graph.python import min_cost_flow

from nodal.allocate.config import ObjectiveConfig
from nodal.domain.capacity import DIMENSIONS, Dimension
from nodal.domain.entities import Shipment
from nodal.domain.units import OBJECTIVE_SCALE
from nodal.events.state import NetworkState
from nodal.rules.core import shipment_classes
from nodal.rules.framework import Pack


@dataclass(frozen=True)
class FlowGate:
    ok: bool
    reason: str


@dataclass(frozen=True)
class FlowCandidate:
    """Mirror of the batch candidate the flow layer needs (kept import-light)."""

    shipment_id: str
    facility_id: str
    zone_id: str
    separable_scaled: int
    min_headroom: dict[Dimension, int]  # min over the stay's buckets, per bounded dim
    constant_headroom: bool  # headroom identical across the stay's buckets
    buckets: tuple[date, ...]  # the stay's bucket window
    stages: bool = False  # books staging capacity elsewhere too (a delivery, §7.9)


def check_flow_gate(
    state: NetworkState,
    shipments: dict[str, Shipment],
    candidates: list[FlowCandidate],
    config: ObjectiveConfig,
    packs: list[Pack],
) -> FlowGate:
    """Every condition §7.4 names, checked explicitly."""
    if config.weights.congestion > 0 or config.weights.inv_balance > 0:
        return FlowGate(False, "facility-level objective terms active")
    if any(pack.incompatible_pairs for pack in packs) and any(
        shipment_classes(s) for s in shipments.values()
    ):
        return FlowGate(False, "segregation classes present")
    if any(candidate.stages for candidate in candidates):
        # A transportation arc rations ONE zone per assignment; a delivery that
        # also books staging elsewhere consumes several (§7.9), so the flow model
        # is no longer exact and CP-SAT takes the whole batch.
        return FlowGate(False, "delivery staging bookings present")
    bound_dims: set[Dimension] = set()
    for candidate in candidates:
        zone = state.zones[candidate.zone_id]
        for dim in DIMENSIONS:
            if zone.capacity.get(dim) is not None:
                bound_dims.add(dim)
    if len(bound_dims) != 1:
        return FlowGate(False, f"{len(bound_dims)} bounded capacity dimensions")
    (dim,) = bound_dims
    sizes = {s.size.demand(dim) for s in shipments.values()}
    if len(sizes) != 1 or 0 in sizes:
        return FlowGate(False, "shipment sizes not uniform in the bounded dimension")
    if any(s.size.demand(d) > 0 for s in shipments.values() for d in DIMENSIONS if d != dim):
        return FlowGate(False, "shipments demand capacity outside the bounded dimension")
    if not all(c.constant_headroom for c in candidates):
        return FlowGate(False, "headroom varies across stay buckets (time-phasing active)")
    # Candidates at one zone must consume the SAME capacity: identical bucket
    # windows and identical observed headroom. Disjoint stays sharing a zone are
    # time-phased capacity in disguise — collapsing them to one arc double-books.
    per_zone_headroom: dict[str, set[int | None]] = {}
    per_zone_windows: dict[str, set[tuple[date, ...]]] = {}
    for candidate in candidates:
        per_zone_headroom.setdefault(candidate.zone_id, set()).add(
            candidate.min_headroom.get(dim)  # None = unbounded zone
        )
        per_zone_windows.setdefault(candidate.zone_id, set()).add(candidate.buckets)
    if any(len(values) != 1 for values in per_zone_headroom.values()):
        return FlowGate(False, "candidates at one zone observe different headroom")
    if any(len(windows) != 1 for windows in per_zone_windows.values()):
        return FlowGate(False, "candidates at one zone occupy different bucket windows")
    return FlowGate(True, "flow-exact structure")


def solve_flow(
    state: NetworkState,
    shipments: dict[str, Shipment],
    candidates: list[FlowCandidate],
    config: ObjectiveConfig,
    conservative: bool,
    extra_unassigned_scaled: dict[str, int] | None = None,
) -> dict[str, tuple[str, str] | None] | None:
    """Transportation solve. Exact under the gate; with `conservative=True` the
    zone capacities are floored to min-headroom units of the *largest* shipment —
    the flow-relax heuristic. `extra_unassigned_scaled` adds a per-shipment cost
    to the unassigned arc (churn under re-optimization, §7.6). Returns None if
    the flow solver fails."""
    ordered_sids = sorted(shipments)
    zones = sorted({c.zone_id for c in candidates})
    dim = _dominant_dimension(state, candidates)
    unit = 0
    if dim is not None:
        unit = max((s.size.demand(dim) for s in shipments.values()), default=0)

    flow = min_cost_flow.SimpleMinCostFlow()
    source = 0
    sink = 1
    shipment_node = {sid: 2 + i for i, sid in enumerate(ordered_sids)}
    zone_node = {zone: 2 + len(ordered_sids) + i for i, zone in enumerate(zones)}
    unassigned_cost = round(config.solver.unassigned_penalty * OBJECTIVE_SCALE)
    extra = extra_unassigned_scaled or {}

    for sid in ordered_sids:
        flow.add_arc_with_capacity_and_unit_cost(source, shipment_node[sid], 1, 0)
        flow.add_arc_with_capacity_and_unit_cost(
            shipment_node[sid], sink, 1, unassigned_cost + extra.get(sid, 0)
        )
    arc_meta: dict[int, tuple[str, str, str]] = {}
    for candidate in sorted(candidates, key=lambda c: (c.shipment_id, c.zone_id)):
        arc = flow.add_arc_with_capacity_and_unit_cost(
            shipment_node[candidate.shipment_id],
            zone_node[candidate.zone_id],
            1,
            candidate.separable_scaled,
        )
        arc_meta[arc] = (candidate.shipment_id, candidate.facility_id, candidate.zone_id)
    headroom_by_zone: dict[str, int] = {}
    if dim is not None:
        for candidate in candidates:
            current = headroom_by_zone.get(candidate.zone_id)
            value = candidate.min_headroom.get(dim, 0)
            headroom_by_zone[candidate.zone_id] = value if current is None else min(current, value)
    for zone in zones:
        # A zone unbounded in the priced dimension (or a zero-size load in it)
        # constrains nothing — CP-SAT has no capacity row there either, so the
        # exact-equivalence claim needs an unlimited arc, not a zero one.
        if dim is None or unit <= 0 or state.zones[zone].capacity.get(dim) is None:
            units = len(ordered_sids)
        else:
            units = max(0, headroom_by_zone.get(zone, 0) // unit)
        flow.add_arc_with_capacity_and_unit_cost(zone_node[zone], sink, units, 0)
    for sid in ordered_sids:
        flow.set_node_supply(shipment_node[sid], 0)
    flow.set_node_supply(source, len(ordered_sids))
    flow.set_node_supply(sink, -len(ordered_sids))

    if flow.solve() != flow.OPTIMAL:
        return None
    assignments: dict[str, tuple[str, str] | None] = {sid: None for sid in ordered_sids}
    for arc, (sid, facility_id, zone_id) in arc_meta.items():
        if flow.flow(arc) == 1:
            assignments[sid] = (facility_id, zone_id)
    return assignments


def _dominant_dimension(state: NetworkState, candidates: list[FlowCandidate]) -> Dimension | None:
    counts: dict[Dimension, int] = {}
    for candidate in candidates:
        zone = state.zones[candidate.zone_id]
        for dim in DIMENSIONS:
            if zone.capacity.get(dim) is not None:
                counts[dim] = counts.get(dim, 0) + 1
    if not counts:
        return None
    return max(sorted(counts), key=lambda d: counts[d])
