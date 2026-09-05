"""Batch optimization (§7.4): CP-SAT over feasible (shipment, zone) pairs.

One objective vocabulary, two evaluation contexts (§14): per-pair separable
contributions are the §7.3 functions; `congestion` and `inv_balance` become
per-facility / per-(facility, group) convex terms via epigraph encodings. CP-SAT's
own proven bound reports the optimality gap. Determinism (§2): canonical model
ordering (sorted ids), fixed seed, deterministic-time budget, one worker in
reproducible modes.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime
from importlib import metadata as importlib_metadata

from ortools.sat.python import cp_model

import nodal
from nodal.allocate.config import CurvePoint, ObjectiveConfig, piecewise
from nodal.allocate.engine import (
    AllocateError,
    CandidateSurvey,
    _route_summary,
    _score_facility,
    assignment_of,
    chosen_of,
    reservations_of,
    survey_candidates,
)
from nodal.allocate.records import (
    BatchContext,
    BucketImpact,
    CapacityImpact,
    ComponentScore,
    DecisionRecord,
    RejectedFacility,
    ScoredCandidate,
    SolverMeta,
)
from nodal.allocate.scorer import (
    SEPARABLE_COMPONENTS,
    EvalCache,
    ScoringPrecomputed,
    compute_components,
    precompute_shared,
    total_score,
)
from nodal.domain.capacity import DIMENSIONS, Dimension
from nodal.domain.entities import Assignment, Reservation, Shipment, ShipmentStatus
from nodal.domain.units import OBJECTIVE_SCALE
from nodal.events import EventStore
from nodal.events import catalog as ev
from nodal.events.envelope import Envelope, EventDraft
from nodal.events.state import NetworkState, buckets_between
from nodal.rules.core import shipment_classes
from nodal.rules.framework import Pack, Reject, incompatible, load_packs

Components = dict[str, ComponentScore]


@dataclass(frozen=True)
class _Candidate:
    shipment_id: str
    facility_id: str
    zone_id: str
    separable_scaled: int  # sum of separable weighted contributions x OBJECTIVE_SCALE
    buckets: tuple[date, ...]
    min_headroom: dict[Dimension, int]
    constant_headroom: bool
    # Staging the delivery's pass-through stops book if this candidate is picked
    # (§7.9): (zone, buckets) per stop. A constant of (shipment, holding
    # facility) — the path out of a candidate hold is deterministic — so these
    # are extra rows on capacity the batch already shares, not new coupling.
    staging: tuple[tuple[str, tuple[date, ...]], ...] = ()

    @property
    def occupied(self) -> tuple[tuple[str, tuple[date, ...]], ...]:
        """Every (zone, buckets) this candidate consumes: the hold, then staging."""
        return ((self.zone_id, self.buckets), *self.staging)


@dataclass
class BatchResult:
    batch_id: str
    records: dict[str, DecisionRecord]  # shipment id -> record (assigned + unassigned)
    assignments: dict[str, tuple[str, str] | None]  # shipment -> (facility, zone) | None
    meta: SolverMeta


_Prep = tuple[
    list[str],
    dict[str, Shipment],
    dict[str, list[CandidateSurvey]],
    dict[str, ScoringPrecomputed],
    list[_Candidate],
    dict[tuple[str, str], Components],
    EvalCache,
]


def _prepare(
    state: NetworkState,
    shipment_ids: list[str],
    config: ObjectiveConfig,
    now: datetime,
    packs: list[Pack] | None,
) -> _Prep:
    ordered_ids = sorted(set(shipment_ids))  # canonical ordering (§2)
    if packs is None:
        packs = load_packs(config.packs)
    shipments: dict[str, Shipment] = {}
    for sid in ordered_ids:
        shipment = state.shipments.get(sid)
        if shipment is None:
            raise AllocateError(f"unknown shipment {sid}")
        if shipment.status is not ShipmentStatus.PLANNED:
            raise AllocateError(f"shipment {sid} is {shipment.status.value}, not planned")
        shipments[sid] = shipment

    surveys: dict[str, list[CandidateSurvey]] = {}
    pres = precompute_shared(state, shipments, now)
    cache = EvalCache(state)
    candidates: list[_Candidate] = []
    components_cache: dict[tuple[str, str], Components] = {}

    for sid in ordered_ids:
        shipment = shipments[sid]
        surveys[sid] = survey_candidates(state, shipment, config, now, packs=packs, cache=cache)
        for survey in surveys[sid]:
            if not survey.feasible:
                continue
            if shipment.is_transfer and survey.facility_id == shipment.origin_facility_id:
                continue  # a transfer parked at its origin is a no-op, not a move
            assert survey.route is not None and survey.eta is not None
            assert survey.departure is not None
            staging = tuple(
                (str(stop.zone_id), tuple(buckets_between(stop.arrive, stop.depart)))
                for stop in survey.staging_stops
            )
            for zone_id in survey.passing_zones:
                components = compute_components(
                    state,
                    shipment,
                    state.facilities[survey.facility_id],
                    state.zones[zone_id],
                    survey.route,
                    survey.wait_minutes,
                    survey.eta,
                    survey.departure,
                    config,
                    pres[sid],
                    cache,
                    packs=packs,
                    delivery=survey.delivery,
                )
                components_cache[(sid, zone_id)] = components
                # Core separable components plus pack components (namespaced
                # "<pack>.<name>" — separable by the §8 contract).
                separable = sum(
                    score.contribution
                    for name, score in components.items()
                    if name in SEPARABLE_COMPONENTS or "." in name
                )
                stay_buckets = tuple(buckets_between(survey.eta, survey.departure))
                min_headroom: dict[Dimension, int] = {}
                constant = True
                for dim in DIMENSIONS:
                    values = [
                        h
                        for b in stay_buckets
                        if (h := cache.headroom(zone_id, b).get(dim)) is not None
                    ]
                    if values:
                        min_headroom[dim] = min(values)
                        constant = constant and min(values) == max(values)
                candidates.append(
                    _Candidate(
                        shipment_id=sid,
                        facility_id=survey.facility_id,
                        zone_id=zone_id,
                        separable_scaled=round(separable * OBJECTIVE_SCALE),
                        buckets=stay_buckets,
                        min_headroom=min_headroom,
                        constant_headroom=constant,
                        staging=staging,
                    )
                )
    return ordered_ids, shipments, surveys, pres, candidates, components_cache, cache


def _pwl_epigraph(
    model: cp_model.CpModel,
    u_var: cp_model.IntVar,
    points: list[CurvePoint],
    weight: float,
    name: str,
    u_max: int,
) -> cp_model.IntVar:
    """P >= weight * curve(u/1e4) * OBJECTIVE_SCALE, per convex segment (§7.4).

    Segment through (x_i, y_i) with slope m: P >= wS(y_i + m(u/1e4 - x_i)),
    i.e. 1e4*P >= round(wSm)*u + round(1e4*wS*(y_i - m*x_i)). The last segment
    extrapolates (piecewise() does the same on the scorer side). The domain is
    sized to the curve's value at u_max so the epigraph can never be infeasible.
    """
    scale = OBJECTIVE_SCALE
    p_upper = round(weight * scale * piecewise(points, u_max / 10_000)) + 10 * scale
    p_var = model.new_int_var(0, max(scale, p_upper), name)
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        slope = (b.y - a.y) / (b.x - a.x) if b.x != a.x else 0.0
        coeff_u = round(weight * scale * slope)
        intercept = round(10_000 * weight * scale * (a.y - slope * a.x))
        model.add(10_000 * p_var >= coeff_u * u_var + intercept)
    return p_var


def solve_batch(
    state: NetworkState,
    shipment_ids: list[str],
    config: ObjectiveConfig,
    now: datetime,
    batch_id: str,
    packs: list[Pack] | None = None,
    incumbents: dict[str, tuple[str, str]] | None = None,
) -> BatchResult:
    """Solve the assignment of all given planned shipments simultaneously.

    `incumbents` (§7.6 re-optimization): a shipment's previous (facility, zone).
    Every option that CHANGES it — a different pair, or unassigned — pays the
    configured churn penalty; keeping it costs nothing extra. Absent for
    ordinary batches, so nothing else changes.
    """
    if packs is None:
        packs = load_packs(config.packs)
    prep = _prepare(state, shipment_ids, config, now, packs)
    ordered_ids, shipments, surveys, pres, candidates, components_cache, cache = prep

    incumbents = incumbents or {}
    churn_scaled = round(config.churn_penalty * OBJECTIVE_SCALE) if incumbents else 0

    def churn_for(sid: str, pair: tuple[str, str] | None) -> int:
        if sid not in incumbents or incumbents[sid] == pair:
            return 0
        return churn_scaled

    def assignment_objective(assignments: dict[str, tuple[str, str] | None]) -> int:
        return _separable_objective(ordered_ids, candidates, assignments, config) + sum(
            churn_for(sid, assignments.get(sid)) for sid in ordered_ids
        )

    # -- min-cost-flow fast path (§7.4): exact only under the explicit gate -------
    from nodal.allocate.flow import FlowCandidate, check_flow_gate, solve_flow

    flow_candidates = [
        FlowCandidate(
            shipment_id=c.shipment_id,
            facility_id=c.facility_id,
            zone_id=c.zone_id,
            # Churn is a per-pair constant, so the flow arcs price it exactly.
            separable_scaled=(
                c.separable_scaled + churn_for(c.shipment_id, (c.facility_id, c.zone_id))
            ),
            min_headroom=c.min_headroom,
            constant_headroom=c.constant_headroom,
            buckets=c.buckets,
            stages=bool(c.staging),
        )
        for c in candidates
    ]
    extra_unassigned = {sid: churn_for(sid, None) for sid in ordered_ids if churn_for(sid, None)}
    gate = check_flow_gate(state, shipments, flow_candidates, config, packs)
    if gate.ok:
        flow_assignments = solve_flow(
            state,
            shipments,
            flow_candidates,
            config,
            conservative=False,
            extra_unassigned_scaled=extra_unassigned,
        )
        if flow_assignments is not None:
            objective_scaled = assignment_objective(flow_assignments)
            meta = SolverMeta(
                status="FLOW_OPTIMAL",
                objective_scaled=objective_scaled,
                bound_scaled=objective_scaled,
                gap=0.0,
                det_time_budget=config.solver.det_time_budget,
                workers=1,
                seed=config.solver.random_seed,
                batch_id=batch_id,
                shipments=len(ordered_ids),
            )
            return _assemble_result(
                state,
                shipments,
                surveys,
                pres,
                components_cache,
                candidates,
                flow_assignments,
                meta,
                batch_id,
                config,
                now,
                cache,
                incumbents=incumbents,
                packs=packs,
            )

    model = cp_model.CpModel()
    x: dict[tuple[str, str], cp_model.IntVar] = {}
    unassigned: dict[str, cp_model.IntVar] = {}
    by_shipment: dict[str, list[_Candidate]] = {}
    by_zone: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        x[(candidate.shipment_id, candidate.zone_id)] = model.new_bool_var(
            f"x_{candidate.shipment_id}_{candidate.zone_id}"
        )
        by_shipment.setdefault(candidate.shipment_id, []).append(candidate)
        by_zone.setdefault(candidate.zone_id, []).append(candidate)

    # Objective assembled as flat (vars, coeffs) for one C++-side weighted_sum;
    # python-side expression trees do not scale to 10^5-term models.
    obj_vars: list[cp_model.IntVar] = []
    obj_coeffs: list[int] = []
    unassigned_coeff = round(config.solver.unassigned_penalty * OBJECTIVE_SCALE)
    for sid in ordered_ids:
        unassigned[sid] = model.new_bool_var(f"un_{sid}")
        row = [x[(sid, c.zone_id)] for c in by_shipment.get(sid, [])]
        model.add_exactly_one([*row, unassigned[sid]])
        obj_vars.append(unassigned[sid])
        obj_coeffs.append(unassigned_coeff + churn_for(sid, None))
    for candidate in candidates:
        obj_vars.append(x[(candidate.shipment_id, candidate.zone_id)])
        obj_coeffs.append(
            candidate.separable_scaled
            + churn_for(candidate.shipment_id, (candidate.facility_id, candidate.zone_id))
        )

    # -- capacity per (zone, dimension, bucket) ----------------------------------
    # Hold stays and delivery staging dwells share these rows: both are real
    # occupancy of the same zone in the same bucket (§7.9).
    rows: dict[_LoadKey, tuple[list[cp_model.IntVar], list[int]]] = {}
    for candidate in candidates:
        var = x[(candidate.shipment_id, candidate.zone_id)]
        for load_key, need in _candidate_load_keys(shipments[candidate.shipment_id], candidate):
            entry = rows.setdefault(load_key, ([], []))
            entry[0].append(var)
            entry[1].append(need)
    for load_key in sorted(rows):
        zone_id, dim, bucket = load_key
        if state.zones[zone_id].capacity.get(dim) is None:
            continue
        headroom = cache.headroom(zone_id, bucket).get(dim)
        assert headroom is not None
        row_vars, row_sizes = rows[load_key]
        model.add(cp_model.LinearExpr.weighted_sum(row_vars, row_sizes) <= max(0, headroom))

    # -- segregation via class indicators (§7.4) ---------------------------------
    any_pairs = any(pack.incompatible_pairs for pack in packs)
    if any_pairs:
        for zone_id in sorted(by_zone):
            classes_here: dict[str, list[cp_model.IntVar]] = {}
            for candidate in by_zone[zone_id]:
                for cls in shipment_classes(shipments[candidate.shipment_id]):
                    classes_here.setdefault(cls, []).append(
                        x[(candidate.shipment_id, candidate.zone_id)]
                    )
            if len(classes_here) < 2:
                continue
            y: dict[str, cp_model.IntVar] = {}
            for cls in sorted(classes_here):
                y[cls] = model.new_bool_var(f"y_{zone_id}_{cls}")
                for var in classes_here[cls]:
                    model.add_implication(var, y[cls])
            class_list = sorted(y)
            for i, c1 in enumerate(class_list):
                for c2 in class_list[i + 1 :]:
                    if incompatible(c1, c2, packs):
                        model.add(y[c1] + y[c2] <= 1)

    # -- congestion: per-facility peak-utilization epigraph (§7.4) ----------------
    # The peak envelope spans every zone of the facility over the union of the
    # facility's candidate stay buckets — the same window the single-shipment
    # scorer peaks over. Each facility's baseline penalty is subtracted as a
    # constant, so the objective sums the same MARGINALS the scorer prices and
    # the n=1 argmin equivalence is exact (and gaps aren't diluted by baselines).
    objective_offset = 0
    w_congestion = config.weights.congestion
    facilities_touched = sorted({c.facility_id for c in candidates})
    if w_congestion > 0:
        for facility_id in facilities_touched:
            facility_buckets = sorted(
                {b for c in candidates if c.facility_id == facility_id for b in c.buckets}
            )
            baseline_peak = cache.facility_peak_baseline(facility_id, tuple(facility_buckets))
            # Domain must cover whatever baseline ratios already exist (a zone
            # shrunk below its contents can sit far above 100%), or the model
            # would be INFEASIBLE instead of degrading gracefully (§13). The
            # 10^9-bp cap (100 000x utilization) keeps every constraint term far
            # inside CP-SAT's int64 validation bounds; beyond it congestion
            # saturates instead of overflowing (rows that would exceed the
            # domain pin u at the cap below).
            u_max = max(100_000, min(10**9, math.ceil(10_000 * baseline_peak) + 50_000))
            u_var = model.new_int_var(0, u_max, f"u_{facility_id}")
            floor_bp = 0
            for zone in state.zones_of(facility_id):
                zone_cands = by_zone.get(zone.id, [])
                for bucket in facility_buckets:
                    effective = cache.effective_capacity(zone.id, bucket)
                    occupied = cache.occupancy(zone.id, bucket)
                    for dim in DIMENSIONS:
                        cap = effective.get(dim)
                        if cap is None:
                            continue
                        existing = occupied.demand(dim)
                        if cap <= 0:
                            # Scorer convention: occupied zero-capacity reads 100%.
                            if existing > 0:
                                floor_bp = max(floor_bp, 10_000)
                            continue
                        if 10_000 * existing > cap * u_max:
                            # Only reachable when the 10^9 cap clipped the domain:
                            # pin u at the cap (saturated congestion) instead of
                            # writing a row the domain cannot satisfy.
                            floor_bp = max(floor_bp, u_max)
                            continue
                        row_vars = [u_var]
                        row_coeffs = [cap]
                        for c in zone_cands:
                            if bucket not in c.buckets:
                                continue
                            size = shipments[c.shipment_id].size.demand(dim)
                            if size > 0:
                                row_vars.append(x[(c.shipment_id, c.zone_id)])
                                row_coeffs.append(-10_000 * size)
                        model.add(
                            cp_model.LinearExpr.weighted_sum(row_vars, row_coeffs)
                            >= 10_000 * existing
                        )
            if floor_bp > 0:
                model.add(u_var >= floor_bp)
            p_var = _pwl_epigraph(
                model, u_var, config.congestion_curve, w_congestion, f"p_{facility_id}", u_max
            )
            obj_vars.append(p_var)
            obj_coeffs.append(1)
            # Offset from the same (possibly clamped) domain the model prices in,
            # so a saturated facility contributes exactly zero marginal.
            objective_offset += round(
                w_congestion
                * OBJECTIVE_SCALE
                * piecewise(config.congestion_curve, min(baseline_peak, u_max / 10_000))
            )

    # -- inventory balance: per-(facility, group) V-penalty (§7.4, §7.7) ----------
    w_balance = config.weights.inv_balance
    if w_balance > 0:
        group_qty: dict[tuple[str, str], int] = {}
        for sid in ordered_ids:
            for line in shipments[sid].lines:
                key = (sid, line.commodity_group)
                group_qty[key] = group_qty.get(key, 0) + line.quantity
        shared_inbound = pres[ordered_ids[0]].inbound if ordered_ids else {}
        for facility_id in facilities_touched:
            for group in sorted(state.demand_rates.get(facility_id, {})):
                rate = state.demand_rate(facility_id, group)
                if rate <= 0:
                    continue
                target = config.cover_days * rate
                before = cache.stock(facility_id, group) + shared_inbound.get(
                    (facility_id, group), 0
                )
                row_vars = []
                row_coeffs = []
                for c in candidates:
                    if c.facility_id == facility_id and (c.shipment_id, group) in group_qty:
                        row_vars.append(x[(c.shipment_id, c.zone_id)])
                        row_coeffs.append(group_qty[(c.shipment_id, group)])
                if not row_vars:
                    continue
                max_dev = (
                    before
                    + sum(
                        group_qty[(sid, group)] for sid in ordered_ids if (sid, group) in group_qty
                    )
                    + target
                )
                dev = model.new_int_var(0, max_dev, f"dev_{facility_id}_{group}")
                # dev >= (before + load) - target  and  dev >= target - (before + load)
                model.add(
                    dev - cp_model.LinearExpr.weighted_sum(row_vars, row_coeffs) >= before - target
                )
                model.add(
                    dev + cp_model.LinearExpr.weighted_sum(row_vars, row_coeffs) >= target - before
                )
                coeff = round(w_balance * OBJECTIVE_SCALE / max(target, 1))
                if coeff > 0:
                    obj_vars.append(dev)
                    obj_coeffs.append(coeff)
                    # Subtract the baseline deviation: the objective prices the
                    # MARGINAL change, matching the scorer (§7.3 as amended).
                    objective_offset += coeff * abs(before - target)

    model.minimize(cp_model.LinearExpr.weighted_sum(obj_vars, obj_coeffs) - objective_offset)

    # Warm start (§13): never hint the TRIVIAL solution (all-unassigned anchors
    # LNS on a poor incumbent); the hint is
    # the validated flow-relax solution, a real feasible assignment CP-SAT starts
    # from and can only improve on. Deterministic (a pure function of the input),
    # and the reported gap stays CP-SAT's own proven bound. Applied up front when
    # configured (very large batches), otherwise only as a fallback re-solve
    # after a cold start exhausts its budget without any incumbent.
    def add_heuristic_hint() -> bool:
        hint = _validated_heuristic_assignments(
            state, ordered_ids, shipments, candidates, config, cache, packs
        )
        if not any(pair is not None for pair in hint.values()):
            return False
        for sid in ordered_ids:
            pair = hint.get(sid)
            model.add_hint(unassigned[sid], 0 if pair is not None else 1)
            for c in by_shipment.get(sid, []):
                model.add_hint(x[(sid, c.zone_id)], 1 if pair == (c.facility_id, c.zone_id) else 0)
        return True

    def make_solver() -> cp_model.CpSolver:
        cp_solver = cp_model.CpSolver()
        cp_solver.parameters.max_deterministic_time = config.solver.det_time_budget
        if config.solver.max_wall_seconds is not None:
            cp_solver.parameters.max_time_in_seconds = config.solver.max_wall_seconds
        cp_solver.parameters.num_workers = config.solver.workers
        cp_solver.parameters.random_seed = config.solver.random_seed
        return cp_solver

    hinted = config.solver.warm_start and add_heuristic_hint()
    solver = make_solver()
    status = solver.solve(model)
    status_name = solver.status_name(status)
    if status == cp_model.UNKNOWN and not hinted and add_heuristic_hint():
        hinted = True
        warm = make_solver()
        status = warm.solve(model)
        status_name = warm.status_name(status)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            solver = warm
    warm_started = hinted and status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    if status == cp_model.UNKNOWN:
        # Still nothing (or no feasible hint existed): the explicit
        # all-unassigned result, labeled as such, nothing proven.
        meta = SolverMeta(
            status="NO_INCUMBENT",
            objective_scaled=assignment_objective({}),
            bound_scaled=0,
            gap=None,
            det_time_budget=config.solver.det_time_budget,
            workers=config.solver.workers,
            seed=config.solver.random_seed,
            batch_id=batch_id,
            shipments=len(ordered_ids),
            reproducible=config.solver.workers == 1 and config.solver.max_wall_seconds is None,
        )
        return _assemble_result(
            state,
            shipments,
            surveys,
            pres,
            components_cache,
            candidates,
            {sid: None for sid in ordered_ids},
            meta,
            batch_id,
            config,
            now,
            cache,
            incumbents=incumbents,
            packs=packs,
        )
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise AllocateError(f"batch solve failed: {status_name}")

    objective_scaled = round(solver.objective_value)
    bound_scaled = round(solver.best_objective_bound)
    # Marginal objectives can be near zero or negative (a batch of pure balance
    # improvements); normalize the gap by magnitude so it stays meaningful.
    gap_denominator = max(abs(objective_scaled), abs(bound_scaled), 1)
    gap = (objective_scaled - bound_scaled) / gap_denominator
    meta = SolverMeta(
        status=status_name,
        objective_scaled=objective_scaled,
        bound_scaled=bound_scaled,
        gap=round(gap, 6),
        det_time_budget=config.solver.det_time_budget,
        workers=config.solver.workers,
        seed=config.solver.random_seed,
        batch_id=batch_id,
        shipments=len(ordered_ids),
        reproducible=config.solver.workers == 1 and config.solver.max_wall_seconds is None,
        warm_started=warm_started,
    )

    assignments: dict[str, tuple[str, str] | None] = {}
    for sid in ordered_ids:
        chosen_pair: tuple[str, str] | None = None
        for candidate in by_shipment.get(sid, []):
            if solver.value(x[(sid, candidate.zone_id)]) == 1:
                chosen_pair = (candidate.facility_id, candidate.zone_id)
                break
        assignments[sid] = chosen_pair

    return _assemble_result(
        state,
        shipments,
        surveys,
        pres,
        components_cache,
        candidates,
        assignments,
        meta,
        batch_id,
        config,
        now,
        cache,
        incumbents=incumbents,
        packs=packs,
    )


def _separable_objective(
    ordered_ids: list[str],
    candidates: list[_Candidate],
    assignments: dict[str, tuple[str, str] | None],
    config: ObjectiveConfig,
) -> int:
    assigned = sum(
        c.separable_scaled
        for c in candidates
        if assignments.get(c.shipment_id) == (c.facility_id, c.zone_id)
    )
    penalty = round(config.solver.unassigned_penalty * OBJECTIVE_SCALE)
    return assigned + penalty * sum(1 for sid in ordered_ids if assignments.get(sid) is None)


def _assemble_result(
    state: NetworkState,
    shipments: dict[str, Shipment],
    surveys: dict[str, list[CandidateSurvey]],
    pres: dict[str, ScoringPrecomputed],
    components_cache: dict[tuple[str, str], Components],
    candidates: list[_Candidate],
    assignments: dict[str, tuple[str, str] | None],
    meta: SolverMeta,
    batch_id: str,
    config: ObjectiveConfig,
    now: datetime,
    cache: EvalCache | None = None,
    incumbents: dict[str, tuple[str, str]] | None = None,
    packs: list[Pack] | None = None,
) -> BatchResult:
    if cache is None:
        cache = EvalCache(state)
    if packs is None:
        # One resolution rule everywhere (memoized): a caller that skipped packs
        # must not end up with alternatives scored pack-free against a chosen
        # candidate whose cached components included them.
        packs = load_packs(config.packs)
    view = _build_solution_view(shipments, candidates, assignments)
    binding = _binding_constraints(cache, view)
    own_candidates: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        own_candidates.setdefault(candidate.shipment_id, []).append(candidate)
    records = {
        sid: _batch_record(
            state,
            shipments[sid],
            config,
            now,
            surveys[sid],
            pres[sid],
            components_cache,
            view,
            own_candidates.get(sid, []),
            meta,
            binding,
            cache,
            incumbent=(incumbents or {}).get(sid),
            packs=packs,
        )
        for sid in sorted(shipments)
    }
    return BatchResult(batch_id=batch_id, records=records, assignments=assignments, meta=meta)


def solve_flow_relax(
    state: NetworkState,
    shipment_ids: list[str],
    config: ObjectiveConfig,
    now: datetime,
    batch_id: str,
    packs: list[Pack] | None = None,
) -> BatchResult:
    """The flow-relax baseline (§9): the transportation construction with
    conservative capacities, applied unconditionally. A labeled heuristic — its
    metadata proves nothing (gap=None), and its `objective_scaled` covers the
    separable terms only (facility-level congestion/inv_balance terms are
    outside the flow model, so the number is not comparable to nodal-batch's)."""
    if packs is None:
        packs = load_packs(config.packs)
    prep = _prepare(state, shipment_ids, config, now, packs)
    ordered_ids, shipments, surveys, pres, candidates, components_cache, cache = prep
    assignments = _validated_heuristic_assignments(
        state, ordered_ids, shipments, candidates, config, cache, packs
    )
    meta = SolverMeta(
        status="HEURISTIC",
        objective_scaled=_separable_objective(ordered_ids, candidates, assignments, config),
        bound_scaled=0,
        gap=None,
        det_time_budget=config.solver.det_time_budget,
        workers=1,
        seed=config.solver.random_seed,
        batch_id=batch_id,
        shipments=len(ordered_ids),
    )
    result = _assemble_result(
        state,
        shipments,
        surveys,
        pres,
        components_cache,
        candidates,
        assignments,
        meta,
        batch_id,
        config,
        now,
        cache,
        packs=packs,
    )
    for sid, record in result.records.items():
        result.records[sid] = record.model_copy(update={"policy": "flow-relax"})
    return result


_LoadKey = tuple[str, Dimension, date]


@dataclass
class _SolutionView:
    """Solution-wide aggregates built once per solve; move deltas adjust them
    incrementally instead of rebuilding — the §7.5 delta explanation at scale."""

    loads: dict[_LoadKey, int]  # batch-assigned zone loads, staging included
    # Hold stays only. `congestion` is the marginal cost of PLACING a shipment at
    # a facility over its stay (§7.3); pricing a passing dwell into it would put
    # a term in the batch objective the single-shipment scorer does not have, and
    # batch == scorer (§14) is worth more than pricing an hour of dock time.
    hold_loads: dict[_LoadKey, int]
    group_loads: dict[tuple[str, str], int]  # batch-assigned qty per (facility, group)
    facility_buckets: dict[str, list[date]]  # the model's peak window per facility
    chosen: dict[str, _Candidate]  # shipment -> its chosen candidate


def _candidate_load_keys(
    shipment: Shipment, candidate: _Candidate, staging: bool = True
) -> list[tuple[_LoadKey, int]]:
    """Capacity this candidate consumes, per (zone, dimension, bucket). With
    `staging=False` only the hold — the window the congestion objective prices.
    Totals are aggregated, so a delivery entering and leaving through the same
    zone on the same day counts both dwells on that one row."""
    size = shipment.size
    occupied = candidate.occupied if staging else ((candidate.zone_id, candidate.buckets),)
    totals: dict[_LoadKey, int] = {}
    for zone_id, buckets in occupied:
        for bucket in buckets:
            for dim in DIMENSIONS:
                need = size.demand(dim)
                if need:
                    key = (zone_id, dim, bucket)
                    totals[key] = totals.get(key, 0) + need
    return sorted(totals.items())


def _validated_heuristic_assignments(
    state: NetworkState,
    ordered_ids: list[str],
    shipments: dict[str, Shipment],
    candidates: list[_Candidate],
    config: ObjectiveConfig,
    cache: EvalCache,
    packs: list[Pack],
) -> dict[str, tuple[str, str] | None]:
    """The conservative flow proposal, validated against every hard constraint.

    The flow rations one dimension only, so each proposed assignment is checked
    (in sorted shipment order, deterministically) against ALL bounded dimensions
    and buckets, and against segregation among the batch's own shipments — the
    survey only covers what is already in the zone. Violators become None. Used
    as the flow-relax policy's answer and as the CP-SAT warm-start hint (§13),
    both of which must never book what hard constraints forbid.
    """
    from nodal.allocate.flow import FlowCandidate, solve_flow

    flow_candidates = [
        FlowCandidate(
            shipment_id=c.shipment_id,
            facility_id=c.facility_id,
            zone_id=c.zone_id,
            separable_scaled=c.separable_scaled,
            min_headroom=c.min_headroom,
            constant_headroom=c.constant_headroom,
            buckets=c.buckets,
            stages=bool(c.staging),
        )
        for c in candidates
    ]
    proposed = solve_flow(state, shipments, flow_candidates, config, conservative=True)
    if proposed is None:
        return {sid: None for sid in ordered_ids}
    cand_by_pair = {(c.shipment_id, c.zone_id): c for c in candidates}
    running: dict[_LoadKey, int] = {}
    zone_classes: dict[str, set[str]] = {}
    validated: dict[str, tuple[str, str] | None] = {}
    for sid in ordered_ids:
        pair = proposed.get(sid)
        if pair is None:
            validated[sid] = None
            continue
        candidate = cand_by_pair[(sid, pair[1])]
        keys = _candidate_load_keys(shipments[sid], candidate)
        fits = True
        for key, need in keys:
            zone_id, dim, bucket = key
            headroom = cache.headroom(zone_id, bucket).get(dim)
            if headroom is not None and running.get(key, 0) + need > headroom:
                fits = False
                break
        classes = shipment_classes(shipments[sid])
        if fits and classes:
            present = zone_classes.get(pair[1], set())
            if any(incompatible(c1, c2, packs) for c1 in classes for c2 in present):
                fits = False
        if fits:
            for key, need in keys:
                running[key] = running.get(key, 0) + need
            if classes:
                zone_classes.setdefault(pair[1], set()).update(classes)
            validated[sid] = pair
        else:
            validated[sid] = None
    return validated


def _build_solution_view(
    shipments: dict[str, Shipment],
    candidates: list[_Candidate],
    assignments: dict[str, tuple[str, str] | None],
) -> _SolutionView:
    chosen: dict[str, _Candidate] = {}
    facility_buckets: dict[str, set[date]] = {}
    for candidate in candidates:
        facility_buckets.setdefault(candidate.facility_id, set()).update(candidate.buckets)
        if assignments.get(candidate.shipment_id) == (candidate.facility_id, candidate.zone_id):
            chosen[candidate.shipment_id] = candidate
    loads: dict[_LoadKey, int] = {}
    hold_loads: dict[_LoadKey, int] = {}
    group_loads: dict[tuple[str, str], int] = {}
    for sid, candidate in chosen.items():
        for key, need in _candidate_load_keys(shipments[sid], candidate):
            loads[key] = loads.get(key, 0) + need
        for key, need in _candidate_load_keys(shipments[sid], candidate, staging=False):
            hold_loads[key] = hold_loads.get(key, 0) + need
        for line in shipments[sid].lines:
            gkey = (candidate.facility_id, line.commodity_group)
            group_loads[gkey] = group_loads.get(gkey, 0) + line.quantity
    return _SolutionView(
        loads=loads,
        hold_loads=hold_loads,
        group_loads=group_loads,
        facility_buckets={f: sorted(b) for f, b in facility_buckets.items()},
        chosen=chosen,
    )


def _peak_util(
    state: NetworkState,
    cache: EvalCache,
    view: _SolutionView,
    facility_id: str,
    override: dict[_LoadKey, int],
) -> float:
    """Facility peak over the model's window, with an incremental load override.
    Conventions match the scorer exactly: effective (disruption-adjusted)
    capacity per bucket; an occupied zero-capacity zone reads 100%."""
    peak = 0.0
    buckets = view.facility_buckets.get(facility_id, [])
    for zone in state.zones_of(facility_id):
        for bucket in buckets:
            effective = cache.effective_capacity(zone.id, bucket)
            occupied = cache.occupancy(zone.id, bucket)
            for dim in DIMENSIONS:
                cap = effective.get(dim)
                if cap is None:
                    continue
                key = (zone.id, dim, bucket)
                total = occupied.demand(dim) + view.hold_loads.get(key, 0) + override.get(key, 0)
                ratio = (1.0 if total > 0 else 0.0) if cap <= 0 else total / cap
                peak = max(peak, ratio)
    return peak


def _binding_constraints(
    cache: EvalCache,
    view: _SolutionView,
) -> list[str]:
    """Capacity rows with zero remaining slack at the optimum (§7.5). Headroom is
    already net of pre-existing occupancy and reservations, so a row is binding
    exactly when the batch load consumes all of it."""
    binding = []
    for (zone_id, dim, bucket), load in sorted(view.loads.items()):
        headroom = cache.headroom(zone_id, bucket).get(dim)
        if headroom is not None and load >= headroom:
            binding.append(f"{zone_id}/{dim}/{bucket.isoformat()}")
    return binding


def _rejected_from_surveys(surveys: list[CandidateSurvey]) -> list[RejectedFacility]:
    rejected: list[RejectedFacility] = []
    for survey in surveys:
        if survey.feasible:
            continue
        verdicts = list(survey.facility_verdicts)
        if not verdicts:
            verdicts = [
                Reject(constraint_id="NO_ELIGIBLE_ZONE", data={"facility": survey.facility_id})
            ]
        rejected.append(
            RejectedFacility(
                facility_id=survey.facility_id,
                facility_verdicts=verdicts,
                zone_verdicts=survey.zone_verdicts,
            )
        )
    return rejected


def _batch_record(
    state: NetworkState,
    shipment: Shipment,
    config: ObjectiveConfig,
    now: datetime,
    shipment_surveys: list[CandidateSurvey],
    pre: ScoringPrecomputed,
    components_cache: dict[tuple[str, str], Components],
    view: _SolutionView,
    own_candidates: list[_Candidate],
    meta: SolverMeta,
    binding: list[str],
    cache: EvalCache | None = None,
    incumbent: tuple[str, str] | None = None,
    packs: list[Pack] | None = None,
) -> DecisionRecord:
    from nodal.allocate.engine import build_record

    if cache is None:
        cache = EvalCache(state)
    chosen_candidate = view.chosen.get(shipment.id)
    assignment = (
        (chosen_candidate.facility_id, chosen_candidate.zone_id)
        if chosen_candidate is not None
        else None
    )
    # Binding rows relevant to THIS shipment: its own candidate zones, staging
    # included (§7.5) — a full cross-dock is as much a reason it went elsewhere
    # as a full rack, and tier-2 escalation reads this list to find the chain.
    own_zones = {c.zone_id for c in own_candidates} | {
        zone_id for c in own_candidates for zone_id, _ in c.staging
    }
    binding = [b for b in binding if b.split("/", 1)[0] in own_zones][:20]
    if assignment is None:
        record = build_record(
            state,
            shipment,
            config,
            now,
            shipment_surveys,
            choice=None,
            policy="nodal-batch",
            cache=cache,
            packs=packs,
        )
        # Solver left it unassigned even if locally feasible: keep the survey's
        # rejections, but strip any chosen the local argmin would have made.
        record = record.model_copy(
            update={
                "mode": "batch",
                "chosen": None,
                "capacity_impact": None,
                "solver": meta,
                "batch_context": BatchContext(
                    delta_vs_best_alternative=None,
                    best_alternative=None,
                    binding_constraints=binding,
                ),
            }
        )
        return record

    facility_id, zone_id = assignment
    survey = next(s for s in shipment_surveys if s.facility_id == facility_id)
    assert survey.route is not None and survey.eta is not None and survey.departure is not None

    scored: list[ScoredCandidate] = []
    for other in shipment_surveys:
        if not other.feasible:
            continue
        if other.facility_id == facility_id:
            components = components_cache[(shipment.id, zone_id)]
            scored.append(
                ScoredCandidate(
                    facility_id=facility_id,
                    zone_id=zone_id,
                    route=_route_summary(survey.route, survey.wait_minutes),
                    eta=survey.eta,
                    departure=survey.departure,
                    components=components,
                    total=round(total_score(components), 9),
                    zone_verdicts=survey.zone_verdicts,
                )
            )
        else:
            scored.append(
                _score_facility(state, shipment, other, config, pre, cache, packs=packs or ())
            )
    scored.sort(key=lambda c: (c.total, c.facility_id, c.zone_id))
    # §7.5 top-K truncation, chosen exempt — same rule as single mode.
    if len(scored) > config.top_k_detail:
        scored = [
            candidate
            if rank < config.top_k_detail
            or (candidate.facility_id, candidate.zone_id) == (facility_id, zone_id)
            else candidate.model_copy(
                update={
                    "components": {},
                    "route": candidate.route.model_copy(update={"legs": []}),
                }
            )
            for rank, candidate in enumerate(scored)
        ]

    delta, best_alt = _delta_vs_best_alternative(
        state, cache, config, view, shipment, own_candidates, pre, incumbent
    )

    reservation_id = f"RES-{shipment.id}-{shipment.allocation_seq + 1}"
    chosen = chosen_of(state, shipment, survey, zone_id, reservation_id)
    buckets = []
    for day in buckets_between(survey.eta, survey.departure):
        occupancy = state.occupancy(zone_id, day)
        buckets.append(
            BucketImpact(
                day=day,
                capacity=state.effective_capacity(zone_id, day),
                occupancy_before=occupancy,
                occupancy_after=occupancy.plus(shipment.size),
            )
        )

    rejected = _rejected_from_surveys(shipment_surveys)

    return DecisionRecord(
        shipment_id=shipment.id,
        decided_at=now,
        mode="batch",
        policy="nodal-batch",
        based_on_seq=state.last_seq,
        config_snapshot=config.model_dump(mode="json"),
        engine_version=nodal.__version__,
        tzdata_version=importlib_metadata.version("tzdata"),
        considered=sorted(state.facilities),
        rejected=rejected,
        scored=scored,
        chosen=chosen,
        capacity_impact=CapacityImpact(zone_id=zone_id, buckets=buckets),
        solver=meta,
        batch_context=BatchContext(
            delta_vs_best_alternative=delta,
            best_alternative=best_alt,
            binding_constraints=binding,
        ),
    )


def _delta_vs_best_alternative(
    state: NetworkState,
    cache: EvalCache,
    config: ObjectiveConfig,
    view: _SolutionView,
    shipment: Shipment,
    own_candidates: list[_Candidate],
    pre: ScoringPrecomputed,
    incumbent: tuple[str, str] | None = None,
) -> tuple[float | None, str | None]:
    """Objective delta (normalized units) of moving this shipment to its cheapest
    alternative with everything else fixed — the local 'why here' (§7.5).
    Separable terms are exact; the facility-level terms are evaluated on the
    solution loads via incremental overrides, so the delta reflects the batch
    context without rebuilding the solution per alternative. Under
    re-optimization the churn penalty joins the delta (§7.6) — an alternative
    that would abandon the incumbent pays it, returning to the incumbent
    recovers it.
    """
    current_candidate = view.chosen.get(shipment.id)
    if current_candidate is None or len(own_candidates) < 2:
        return None, None

    def churn(pair: tuple[str, str]) -> float:
        if incumbent is None or pair == incumbent:
            return 0.0
        return config.churn_penalty

    current_pair = (current_candidate.facility_id, current_candidate.zone_id)
    best: tuple[float, str] | None = None
    for alternative in sorted(own_candidates, key=lambda c: (c.facility_id, c.zone_id)):
        alt_pair = (alternative.facility_id, alternative.zone_id)
        if alt_pair == current_pair:
            continue
        separable_delta = (
            alternative.separable_scaled - current_candidate.separable_scaled
        ) / OBJECTIVE_SCALE
        facility_delta = _facility_terms_delta(
            state, cache, config, view, shipment, pre, current_candidate, alternative
        )
        delta = separable_delta + facility_delta + churn(alt_pair) - churn(current_pair)
        label = f"{alternative.facility_id}/{alternative.zone_id}"
        if best is None or (delta, label) < best:
            best = (delta, label)
    assert best is not None
    return round(best[0], 6), best[1]


def _facility_terms_delta(
    state: NetworkState,
    cache: EvalCache,
    config: ObjectiveConfig,
    view: _SolutionView,
    shipment: Shipment,
    pre: ScoringPrecomputed,
    current: _Candidate,
    alternative: _Candidate,
) -> float:
    """Exact change in the congestion + balance terms if the shipment moved —
    evaluated as incremental overrides on the solution view."""
    w_cong = config.weights.congestion
    w_bal = config.weights.inv_balance
    delta = 0.0
    affected = sorted({current.facility_id, alternative.facility_id})

    if w_cong > 0:
        override: dict[_LoadKey, int] = {}
        for key, need in _candidate_load_keys(shipment, current, staging=False):
            override[key] = override.get(key, 0) - need
        for key, need in _candidate_load_keys(shipment, alternative, staging=False):
            override[key] = override.get(key, 0) + need
        for facility_id in affected:
            before = piecewise(
                config.congestion_curve, _peak_util(state, cache, view, facility_id, {})
            )
            after = piecewise(
                config.congestion_curve, _peak_util(state, cache, view, facility_id, override)
            )
            delta += w_cong * (after - before)

    if w_bal > 0:
        groups = sorted({line.commodity_group for line in shipment.lines})
        qty = {
            g: sum(line.quantity for line in shipment.lines if line.commodity_group == g)
            for g in groups
        }
        for facility_id in affected:
            for group in groups:
                rate = state.demand_rate(facility_id, group)
                if rate <= 0:
                    continue
                target = float(config.cover_days * rate)
                # Batch-assigned + pre-batch committed, minus this shipment's own
                # contribution wherever it currently sits.
                own_here = qty[group] if current.facility_id == facility_id else 0
                base = (
                    cache.stock(facility_id, group)
                    + pre.inbound.get((facility_id, group), 0)
                    + view.group_loads.get((facility_id, group), 0)
                    - own_here
                )
                own_after = qty[group] if alternative.facility_id == facility_id else 0
                before_dev = abs(base + own_here - target) / max(target, 1.0)
                after_dev = abs(base + own_after - target) / max(target, 1.0)
                delta += w_bal * (after_dev - before_dev)
    return delta


def incumbent_objective_scaled(
    state: NetworkState,
    shipment_ids: list[str],
    config: ObjectiveConfig,
    now: datetime,
    incumbents: dict[str, tuple[str, str]],
    packs: list[Pack] | None = None,
) -> int:
    """Separable objective of keeping the still-feasible incumbent assignments —
    the §7.6 degradation baseline (churn-free by definition: keeping costs no
    churn). An incumbent no longer among the feasible candidates contributes
    ZERO to the baseline, so its entire re-placement cost (or unassigned
    penalty) registers as degradation — pricing it as unassigned here would
    inflate the baseline and drive degradation negative, making the tier-2
    trigger unreachable."""
    if packs is None:
        packs = load_packs(config.packs)
    prep = _prepare(state, shipment_ids, config, now, packs)
    ordered_ids, _shipments, _surveys, _pres, candidates, _components, _cache = prep
    available = {(c.shipment_id, c.facility_id, c.zone_id) for c in candidates}
    kept_ids: list[str] = []
    assignments: dict[str, tuple[str, str] | None] = {}
    for sid in ordered_ids:
        pair = incumbents.get(sid)
        if pair is not None and (sid, pair[0], pair[1]) in available:
            kept_ids.append(sid)
            assignments[sid] = pair
    return _separable_objective(kept_ids, candidates, assignments, config)


def drafted_assignments(plan: ev.PlanDrafted) -> dict[str, tuple[str, str] | None]:
    """The (facility, zone) a drafted plan proposes per shipment (§7.5).

    Read off `chosen` in each record — exactly the field `commit_batch` books
    from — so a draft and the commit that follows it are directly comparable.
    None is a shipment the solve left unassigned.
    """
    pairs: dict[str, tuple[str, str] | None] = {}
    for sid in sorted(plan.records):
        record = plan.records[sid]
        chosen = record.get("chosen") if isinstance(record, dict) else None
        pairs[sid] = (
            (str(chosen["facility_id"]), str(chosen["zone_id"]))
            if isinstance(chosen, dict)
            else None
        )
    return pairs


def draft_batch(store: EventStore, result: BatchResult, actor: str = "cli") -> list[Envelope]:
    """Append the plan as a proposal: one `PlanDrafted` carrying the same
    per-shipment records a commit would embed, and nothing else (§7.5).

    Nothing is booked and no capacity is reserved — but the proposal the
    optimizer made at this instant is a fact about the world, so it belongs in
    the log rather than in a browser tab. The fold keeps it pending only while
    it stays the head, so the operator who reloads mid-review sees the plan they
    were reviewing and nothing staler. Stale records are refused exactly as
    `commit_batch` refuses them."""
    head = store.last_seq()
    first_record = next(iter(result.records.values()), None)
    if first_record is None:
        return []
    if head != first_record.based_on_seq:
        raise AllocateError(
            f"stale batch: based on seq {first_record.based_on_seq} but the log head is {head}"
        )
    assigned = sum(1 for record in result.records.values() if record.chosen is not None)
    return store.append(
        [
            EventDraft(
                ts=first_record.decided_at,
                payload=ev.PlanDrafted(
                    batch_id=result.batch_id,
                    based_on_seq=first_record.based_on_seq,
                    meta=result.meta.model_dump(mode="json"),
                    records={
                        sid: result.records[sid].as_event_record() for sid in sorted(result.records)
                    },
                    assigned=assigned,
                    unassigned=len(result.records) - assigned,
                ),
            )
        ],
        actor=actor,
    )


def commit_batch(store: EventStore, result: BatchResult, actor: str = "cli") -> list[Envelope]:
    """Append the batch decision atomically: BatchSolved + per-shipment
    AllocationDecided + ReservationPlaced. Stale records are refused (§7.5)."""
    head = store.last_seq()
    drafts: list[EventDraft] = []
    first_record = next(iter(result.records.values()), None)
    if first_record is None:
        return []
    if head != first_record.based_on_seq:
        raise AllocateError(
            f"stale batch: based on seq {first_record.based_on_seq} but the log head is {head}"
        )
    ts = first_record.decided_at
    drafts.append(
        EventDraft(
            ts=ts,
            payload=ev.BatchSolved(
                batch_id=result.batch_id, meta=result.meta.model_dump(mode="json")
            ),
        )
    )
    for sid in sorted(result.records):
        record = result.records[sid]
        if record.chosen is None:
            continue
        chosen = record.chosen
        drafts.append(
            EventDraft(
                ts=ts,
                payload=ev.AllocationDecided(
                    shipment_id=sid,
                    assignment=assignment_of(chosen),
                    record=record.as_event_record(),
                ),
                cause=f"batch:{result.batch_id}",
            )
        )
        for reservation in reservations_of(sid, chosen):
            drafts.append(
                EventDraft(
                    ts=ts,
                    payload=ev.ReservationPlaced(reservation=reservation),
                    cause=f"batch:{result.batch_id}",
                )
            )
    return store.append(drafts, actor=actor)


class PlanMismatch(AllocateError):
    """The re-solve no longer books what the operator reviewed (§7.5)."""

    def __init__(self, moved: list[str]) -> None:
        self.moved = moved
        super().__init__(
            f"the re-solve no longer matches the plan you reviewed ({', '.join(moved)}); "
            "review a fresh plan"
        )


def _booking(
    shipment_id: str, record: DecisionRecord
) -> tuple[Assignment, list[Reservation]] | None:
    """Everything a commit would WRITE for one shipment, or None if it books
    nothing: the assignment (facility, zone, route, outbound, exit, stops, legs,
    eta, departure) and every reservation it places."""
    chosen = record.chosen
    if chosen is None:
        return None
    return assignment_of(chosen), reservations_of(shipment_id, chosen)


def plan_divergence(plan: ev.PlanDrafted, result: BatchResult) -> list[str]:
    """Shipments the re-solve would book differently from the reviewed plan.

    The comparison is the whole booking, not just the destination: a re-solve
    that lands on the same (facility, zone) by a different route, on a different
    schedule, or holding different staging capacity is a different plan from the
    one the operator read, and committing it silently would defeat the review.
    """
    drafted = {sid: DecisionRecord.from_event_record(rec) for sid, rec in plan.records.items()}
    moved = set(drafted) ^ set(result.records)  # a shipment only one of them decided
    moved |= {
        sid
        for sid in set(drafted) & set(result.records)
        if _booking(sid, drafted[sid]) != _booking(sid, result.records[sid])
    }
    return sorted(moved)


def commit_reviewed(
    store: EventStore,
    state: NetworkState,
    shipment_ids: list[str],
    config: ObjectiveConfig,
    now: datetime,
    *,
    batch_id: str,
    actor: str = "cli",
) -> tuple[BatchResult, list[Envelope]]:
    """Solve the queue and BOOK it, honouring any plan under review (§7.5).

    Every surface commits a batch through here, so none of them can book past a
    review. With a draft pending, the commit re-solves under the DRAFT's batch id
    — the audit chain reads `PlanDrafted` -> `BatchSolved` on one batch — and is
    refused outright if that re-solve diverges from what the operator read.
    Determinism (§2) makes the two agree on an unchanged world; a server
    configured for interactive speed (parallel workers, a wall-clock budget) is
    explicitly budget-dependent and is allowed to differ, and booking a plan
    nobody reviewed is the failure this whole flow exists to prevent.

    `batch_id` is the id to mint when nothing is pending.
    """
    pending = state.pending_plan
    result = solve_batch(
        state,
        shipment_ids,
        config,
        now,
        batch_id=pending.batch_id if pending is not None else batch_id,
    )
    if pending is not None:
        moved = plan_divergence(pending, result)
        if moved:
            raise PlanMismatch(moved)
    return result, commit_batch(store, result, actor=actor)
