"""Rendering decision records for humans (§7.5).

A pure function of the record: same record, same text. Wording lives here and in
rules/messages.py — never in the event log.
"""

from collections.abc import Sequence

from nodal.allocate.records import (
    DecisionRecord,
    Itinerary,
    ItineraryLeg,
    RouteSummary,
    ScoredCandidate,
)
from nodal.domain.capacity import DIMENSIONS
from nodal.rules.framework import Pack
from nodal.rules.messages import render_reject


def _route_line(route: RouteSummary) -> str:
    if not route.legs:
        return "already on site (no transport)"
    hops = " -> ".join([route.legs[0].from_label] + [leg.to_facility_id for leg in route.legs])
    wait = f" + wait {route.wait_minutes}m" if route.wait_minutes else ""
    return (
        f"{hops}  ({route.km:.0f} km, {route.minutes}m transit{wait}, "
        f"${route.cost_cents / 100:.2f}, transfers={route.transfers})"
    )


def _itinerary_block(itinerary: Itinerary) -> list[str]:
    """Legs in travel order with the hold in its place between the two halves."""
    hold = itinerary.hold
    lines = [
        f"  itinerary to {itinerary.destination}"
        f"  (delivered {itinerary.delivered_at.isoformat()},"
        f" ${itinerary.cost_cents / 100:.2f}):"
    ]

    def leg_line(leg: ItineraryLeg) -> str:
        return (
            f"    {leg.kind:<5} {leg.from_label} -> {leg.to_label}"
            f"  {leg.km:.0f} km, {leg.minutes}m"
            f"  {leg.depart.isoformat()} -> {leg.arrive.isoformat()}"
        )

    lines.extend(leg_line(leg) for leg in itinerary.legs if leg.depart < hold.until_ts)
    lines.append(
        f"    hold  {hold.facility_id}/{hold.zone_id}"
        f"  {hold.from_ts.isoformat()} -> {hold.until_ts.isoformat()}"
    )
    lines.extend(leg_line(leg) for leg in itinerary.legs if leg.depart >= hold.until_ts)
    # Where the goods occupy a facility, hold included — the windows closures and
    # staging capacity bind against (§7.9).
    for stop in itinerary.stops:
        lines.append(
            f"    stop  {stop.role.value:<7} {stop.facility_id}/{stop.zone_id or '?'}"
            f"  {stop.arrive.isoformat()} -> {stop.depart.isoformat()}"
        )
    return lines


def _components_block(candidate: ScoredCandidate, indent: str) -> list[str]:
    lines = []
    for name, score in candidate.components.items():
        base = f"  (from {score.baseline:.4f})" if score.baseline is not None else ""
        lines.append(
            f"{indent}{name:<22} raw={score.raw:>12.3f}  norm={score.normalized:>8.4f}"
            f"  w={score.weight:>5.2f}  -> {score.contribution:+.4f}{base}"
        )
    lines.append(f"{indent}{'total':<22} {'':>12}  {'':>13}  {'':>8}  = {candidate.total:.4f}")
    return lines


def render_record(record: DecisionRecord, packs: Sequence[Pack] = (), explain: bool = False) -> str:
    lines: list[str] = []
    lines.append(
        f"decision for {record.shipment_id} at {record.decided_at.isoformat()} "
        f"(mode={record.mode}, profile={record.config_snapshot.get('name', '?')}, "
        f"considered={len(record.considered)})"
    )
    chosen_candidate = None
    if record.chosen is not None:
        chosen = record.chosen
        chosen_candidate = next(
            c
            for c in record.scored
            if c.facility_id == chosen.facility_id and c.zone_id == chosen.zone_id
        )
        lines.append(
            f"chosen: {chosen.facility_id} / {chosen.zone_id}   score={chosen_candidate.total:.4f}"
            + (f"   (policy {record.policy})" if record.policy != "nodal-single" else "")
        )
        lines.append(f"  route: {_route_line(chosen.route)}")
        lines.append(
            f"  eta {chosen.eta.isoformat()}  ->  departure {chosen.departure.isoformat()}"
        )
        if chosen.itinerary is not None:
            lines.extend(_itinerary_block(chosen.itinerary))
        if explain:
            lines.extend(_components_block(chosen_candidate, "  "))
        if record.solver is not None:
            gap_text = f"{record.solver.gap:.4%}" if record.solver.gap is not None else "unproven"
            lines.append(
                f"  solver: {record.solver.status} (batch {record.solver.batch_id},"
                f" {record.solver.shipments} shipments, gap {gap_text})"
            )
        if record.batch_context is not None and record.batch_context.best_alternative:
            context = record.batch_context
            lines.append(
                f"  best alternative: {context.best_alternative}"
                f"  (delta {context.delta_vs_best_alternative:+.4f})"
            )
            if explain and context.binding_constraints:
                lines.append("  binding capacity: " + ", ".join(context.binding_constraints))
    elif record.solver is not None and record.scored:
        # Batch mode left a locally-feasible shipment unassigned: capacity
        # competition or budget, never "no feasible destination".
        gap_text = f"{record.solver.gap:.4%}" if record.solver.gap is not None else "unproven"
        lines.append(
            f"UNASSIGNED BY SOLVER ({record.solver.status}, batch {record.solver.batch_id},"
            f" gap {gap_text}): {len(record.scored)} feasible candidates competed"
        )
    else:
        lines.append("NO FEASIBLE DESTINATION — every facility was rejected:")
    if explain and len(record.scored) > 1:
        lines.append("alternatives:")
        best_total = chosen_candidate.total if chosen_candidate else record.scored[0].total
        for candidate in [c for c in record.scored if c is not chosen_candidate]:
            lines.append(
                f"  {candidate.facility_id} / {candidate.zone_id}"
                f"  score={candidate.total:.4f}  (delta {candidate.total - best_total:+.4f})"
            )
            lines.extend(_components_block(candidate, "      "))
    if record.rejected:
        lines.append("rejected:")
        for rejection in record.rejected:
            reasons = "; ".join(
                f"{v.constraint_id}: {render_reject(v, packs)}" for v in rejection.facility_verdicts
            )
            lines.append(f"  {rejection.facility_id}  {reasons}")
            if explain:
                for zone_id, verdicts in rejection.zone_verdicts.items():
                    zone_reasons = "; ".join(
                        f"{v.constraint_id}: {render_reject(v, packs)}" for v in verdicts
                    )
                    lines.append(f"      {zone_id}: {zone_reasons}")
    if explain and record.capacity_impact is not None:
        impact = record.capacity_impact
        lines.append(f"capacity impact at {impact.zone_id}:")
        for bucket in impact.buckets:
            parts = []
            for dim in DIMENSIONS:
                cap = bucket.capacity.get(dim)
                if cap is None:
                    continue
                before = bucket.occupancy_before.demand(dim)
                after = bucket.occupancy_after.demand(dim)
                if after != before or before:
                    parts.append(f"{dim} {before}->{after}/{cap}")
            if parts:
                lines.append(f"  {bucket.day.isoformat()}  " + "  ".join(parts))
    return "\n".join(lines)
