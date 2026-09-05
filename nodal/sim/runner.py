"""The simulation loop (§9): a deterministic event producer with a virtual clock.

All randomness is consumed at generation time; the loop is a heap of scheduled
actions applied in (ts, seq) order. Wall-clock readings exist only for the
performance KPI section and never enter the event log (§2).

Two decision modes share the loop: per-shipment policies decide at registration;
batch policies accumulate pending shipments and solve at batch ticks (§7.4) —
unassigned shipments stay pending and retry at later ticks, so batch-mode
`unallocated` means "still unassigned when the horizon closed". Rebalancing
transfers are the exception: an unassigned transfer is cancelled at its tick
(recorded as a failed move), never retried (§7.7).
"""

import heapq
import math
import time
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import cast

from nodal.allocate.config import ObjectiveConfig
from nodal.allocate.engine import commit
from nodal.allocate.rebalance import generate_rebalancing_transfers
from nodal.allocate.records import DecisionRecord
from nodal.bench.kpi import ArrivalOutcome, DecisionOutcome, RunStats
from nodal.domain.capacity import DIMENSIONS, CapacityVector
from nodal.domain.entities import Disruption, DisruptionKind, InventoryLot, Shipment
from nodal.events import EventStore, fold, load_state
from nodal.events import catalog as ev
from nodal.events.envelope import EventDraft, EventPayload
from nodal.events.state import NetworkState
from nodal.sim.generator import GeneratedWorkload, generate
from nodal.sim.policies import AllocationPolicy, BatchPolicy, make_policy
from nodal.sim.scenarios import ScenarioSpec


class SimError(Exception):
    pass


def run_scenario(
    spec: ScenarioSpec,
    policy_name: str,
    db_path: str | Path,
    seed: int | None = None,
) -> RunStats:
    if spec.world_file is not None:
        raise SimError("explicit world_file scenarios are not simulated in v1 — generate instead")
    if seed is not None and seed != spec.seed:
        spec = spec.model_copy(update={"seed": seed})
    workload = generate(spec)
    store = EventStore(db_path)
    try:
        if store.last_seq() != 0:
            raise SimError(
                f"refusing to simulate into a non-empty event database ({db_path}); "
                "delete it or pass a fresh path"
            )
        return _Sim(spec, policy_name, store, workload).run()
    finally:
        store.close()


class _Sim:
    def __init__(
        self,
        spec: ScenarioSpec,
        policy_name: str,
        store: EventStore,
        workload: GeneratedWorkload,
    ) -> None:
        self.spec = spec
        self.store = store
        self.workload = workload
        self.policy: AllocationPolicy | BatchPolicy = make_policy(policy_name)
        self.batch_mode = hasattr(self.policy, "decide_batch")
        self.config: ObjectiveConfig = spec.objective
        self.stats = RunStats(
            scenario=spec.name,
            policy=self.policy.name,
            seed=spec.seed,
            horizon_days=spec.horizon_days,
            profile=spec.objective.name,
        )
        self.horizon_end = spec.start + timedelta(days=spec.horizon_days)
        self.queue: list[tuple[datetime, int, str, object]] = []
        self._tiebreak = 0
        self.state: NetworkState = NetworkState()
        self.pending: list[str] = []
        self.batch_counter = 0
        # KPI rows are FINAL outcomes, one per shipment: a booking released by
        # re-optimization and re-booked later must replace its row, not add one.
        self._outcome_pos: dict[str, int] = {}

    # -- plumbing ---------------------------------------------------------------

    def schedule(self, ts: datetime, kind: str, payload: object) -> None:
        self._tiebreak += 1
        heapq.heappush(self.queue, (ts, self._tiebreak, kind, payload))

    def append(self, payloads: list[EventPayload], ts: datetime, cause: str | None = None) -> None:
        envelopes = self.store.append(
            [EventDraft(ts=ts, payload=p, cause=cause) for p in payloads], actor="sim"
        )
        fold(envelopes, into=self.state)

    # -- main loop ---------------------------------------------------------------

    def run(self) -> RunStats:
        self.store.append(self.workload.world_drafts, actor="sim")
        self.state = load_state(self.store)
        for ts, shipment in self.workload.shipment_plan:
            self.schedule(ts, "register", shipment)
        for disruption in self.workload.disruption_plan:
            self.schedule(disruption.from_ts, "disrupt", disruption)
        day = self.spec.start
        while day < self.horizon_end:
            day = datetime.combine((day + timedelta(days=1)).date(), dtime.min, tzinfo=UTC)
            self.schedule(day, "daily", None)
        if self.batch_mode:
            tick = self.spec.start + timedelta(hours=self.spec.batch_interval_hours)
            while tick < self.horizon_end:
                self.schedule(tick, "solve", None)
                tick += timedelta(hours=self.spec.batch_interval_hours)

        while self.queue:
            now, _, kind, payload = heapq.heappop(self.queue)
            if now >= self.horizon_end and kind not in ("arrive", "depart"):
                continue  # booked work still moves; nothing else happens
            if kind == "register":
                assert isinstance(payload, Shipment)
                self._register(payload, now)
            elif kind == "depart":
                assert isinstance(payload, str)
                self._depart(payload, now)
            elif kind == "arrive":
                assert isinstance(payload, str)
                self._arrive(payload, now)
            elif kind == "disrupt":
                assert isinstance(payload, Disruption)
                self.append([ev.DisruptionStarted(disruption=payload)], now)
                if self.batch_mode:
                    self._reoptimize(payload, now)
            elif kind == "daily":
                self._daily_tick(now)
            elif kind == "solve":
                self._solve_batch(now)

        for sid in self.pending:
            leftover = self.state.shipments.get(sid)
            if leftover is not None and leftover.status.value == "planned":
                self._record_outcome(sid, None, 0, is_transfer=leftover.is_transfer)
        return self.stats

    # -- decisions ----------------------------------------------------------------

    def _register(self, shipment: Shipment, now: datetime) -> None:
        self.append([ev.ShipmentRegistered(shipment=shipment)], now)
        if self.batch_mode:
            self.pending.append(shipment.id)
            return
        policy = cast(AllocationPolicy, self.policy)
        started = time.perf_counter()
        result = policy.decide(self.state, shipment.id, self.config, now)
        self.stats.decision_wall_ms.append((time.perf_counter() - started) * 1000)
        record = result.record
        if record.chosen is None:
            self._record_outcome(shipment.id, None, result.infeasible_preferred)
            return
        envelopes = commit(self.store, record, actor="sim")
        fold(envelopes, into=self.state)
        self.schedule(max(shipment.ready_at, now), "depart", shipment.id)
        self._record_outcome(shipment.id, record, result.infeasible_preferred)

    def _solve_batch(self, now: datetime) -> None:
        policy = cast(BatchPolicy, self.policy)
        if self.spec.rebalance:
            self.batch_counter += 1
            transfers = generate_rebalancing_transfers(
                self.state, self.config, now, id_prefix=f"TRF-{self.batch_counter:03d}"
            )
            for transfer in transfers:
                self.append([ev.TransferOrdered(shipment=transfer)], now)
                self.pending.append(transfer.id)
        pending_planned = [
            sid
            for sid in self.pending
            if (s := self.state.shipments.get(sid)) is not None and s.status.value == "planned"
        ]
        if not pending_planned:
            return
        self.batch_counter += 1
        batch_id = f"BATCH-{self.batch_counter:04d}"
        started = time.perf_counter()
        result = policy.decide_batch(self.state, pending_planned, self.config, now, batch_id)
        self.stats.decision_wall_ms.append((time.perf_counter() - started) * 1000)
        self.stats.batch_gaps.append(result.meta.gap if result.meta.gap is not None else -1.0)

        from nodal.allocate.batch import commit_batch

        envelopes = commit_batch(self.store, result, actor="sim")
        fold(envelopes, into=self.state)
        assigned = {sid for sid, pair in result.assignments.items() if pair is not None}
        for sid in sorted(assigned):
            record = result.records[sid]
            self.schedule(max(self.state.shipments[sid].ready_at, now), "depart", sid)
            self._record_outcome(sid, record, 0)
        # Unassigned TRANSFERS are cancelled, not retried: a pending transfer's
        # lot claims and quantities go stale (consumption keeps draining them),
        # and the next cycle regenerates fresh proposals anyway. Each cancel is
        # recorded as a failed move so the §10 KPIs can show the failure rate.
        cancelled: set[str] = set()
        for sid in sorted(set(pending_planned) - assigned):
            shipment = self.state.shipments.get(sid)
            if shipment is not None and shipment.is_transfer:
                self._record_outcome(sid, None, 0, is_transfer=True)
                self.append([ev.ShipmentCancelled(shipment_id=sid, reason="rebalance-retry")], now)
                cancelled.add(sid)
        self.pending = [sid for sid in self.pending if sid not in assigned and sid not in cancelled]

    def _depart(self, shipment_id: str, now: datetime) -> None:
        """Dispatch at departure time, reading CURRENT state: a re-optimization
        between booking and departure redirects the truck; a released booking
        (superseded to planned) simply doesn't roll."""
        shipment = self.state.shipments.get(shipment_id)
        if shipment is None or shipment.status.value != "allocated" or shipment.assigned is None:
            return
        payloads: list[EventPayload] = []
        if shipment.is_transfer:
            for lot_id in shipment.transfer_lot_ids:
                payloads.append(ev.LotShipped(lot_id=lot_id, shipment_id=shipment_id))
        payloads.append(ev.ShipmentDeparted(shipment_id=shipment_id))
        self.append(payloads, now, cause=f"dispatch:{shipment_id}")
        self.schedule(shipment.assigned.eta, "arrive", shipment_id)

    def _reoptimize(self, disruption: Disruption, now: datetime) -> None:
        """§7.6 in the loop: re-solve what the disruption touched, atomically."""
        from nodal.allocate.reopt import reoptimize

        started = time.perf_counter()
        result = reoptimize(
            self.store, self.state, disruption.id, self.config, now, batch_prefix="SIM-REOPT"
        )
        if result.tier == 0:
            return
        self.stats.reopt_wall_ms.append((time.perf_counter() - started) * 1000)
        fold(result.envelopes, into=self.state)
        self.stats.reopt_tiers.append(result.tier)
        self.stats.reopt_moved += len(result.changed) - len(result.released)
        self.stats.reopt_released += len(result.released)
        # Released bookings rejoin the pending pool and retry at later ticks;
        # moved ones keep their original depart entry, which reads fresh state.
        for sid in result.released:
            if sid not in self.pending:
                self.pending.append(sid)

    def _record_outcome(
        self,
        shipment_id: str,
        record: DecisionRecord | None,
        infeasible_preferred: int,
        is_transfer: bool | None = None,
    ) -> None:
        if is_transfer is None:
            shipment = self.state.shipments.get(shipment_id)
            is_transfer = bool(shipment is not None and shipment.is_transfer)
        if record is not None and record.chosen is not None:
            outcome = DecisionOutcome(
                shipment_id=shipment_id,
                allocated=True,
                cost_cents=record.chosen.route.cost_cents,
                km=record.chosen.route.km,
                transfers=record.chosen.route.transfers,
                infeasible_preferred=infeasible_preferred,
            )
        else:
            outcome = DecisionOutcome(
                shipment_id=shipment_id,
                allocated=False,
                infeasible_preferred=infeasible_preferred,
            )
        target = self.stats.rebalancing if is_transfer else self.stats.decisions
        position = self._outcome_pos.get(shipment_id)
        if position is None:
            self._outcome_pos[shipment_id] = len(target)
            target.append(outcome)
        else:
            target[position] = outcome

    # -- physical events -----------------------------------------------------------

    def _arrive(self, shipment_id: str, now: datetime) -> None:
        shipment = self.state.shipments.get(shipment_id)
        if shipment is None or shipment.assigned is None:
            return  # cancelled or superseded before arrival
        deadline = shipment.requirements.deadline
        zone_id = shipment.assigned.zone_ids[0]
        departure = shipment.assigned.expected_departure
        payloads: list[EventPayload] = []
        if shipment.is_transfer:
            for lot_id in shipment.transfer_lot_ids:
                payloads.append(
                    ev.LotMoved(lot_id=lot_id, to_zone_id=zone_id, planned_departure=departure)
                )
        else:
            for i, line in enumerate(shipment.lines):
                payloads.append(
                    ev.LotReceived(
                        lot=InventoryLot(
                            id=f"LOT-{shipment_id}-{i}",
                            sku=line.sku,
                            commodity_group=line.commodity_group,
                            quantity=line.quantity,
                            uom=line.uom,
                            size=line.size,
                            compat_class=line.compat_class,
                            attributes=line.attributes,
                            zone_id=zone_id,
                            shipment_id=shipment_id,
                            planned_departure=departure,
                        )
                    )
                )
        destination = shipment.assigned.facility_id
        arrived_closed = any(
            d.kind is DisruptionKind.FACILITY_CLOSED and d.target_id == destination
            for d in self.state.active_disruptions(now)
        )
        payloads.append(ev.ShipmentArrived(shipment_id=shipment_id))
        self.append(payloads, now, cause=f"arrival:{shipment_id}")
        if not shipment.is_transfer:
            self.stats.arrivals.append(
                ArrivalOutcome(
                    shipment_id=shipment_id,
                    arrived_at=now,
                    deadline=deadline,
                    arrived_closed=arrived_closed,
                )
            )

    def _daily_tick(self, now: datetime) -> None:
        state = self.state
        day = now.astimezone(UTC).date()
        for facility_id in sorted(state.facilities):
            peak = 0.0
            for zone in state.zones_of(facility_id):
                capacity = state.effective_capacity(zone.id, day)
                occupancy = state.occupancy(zone.id, day)
                for dim in DIMENSIONS:
                    cap = capacity.get(dim)
                    if cap is None or cap <= 0:
                        continue
                    ratio = occupancy.demand(dim) / cap
                    self.stats.utilization_samples.setdefault(dim, []).append(round(ratio, 6))
                    peak = max(peak, ratio)
            self.stats.band_samples += 1
            band = self.config.util_band
            if band[0] <= peak <= band[1]:
                self.stats.band_hits += 1

        # Imbalance per commodity group (§10, §7.7): one sample per (facility, day).
        for facility_id in sorted(state.demand_rates):
            for group, rate in sorted(state.demand_rates[facility_id].items()):
                if rate <= 0:
                    continue
                target = float(self.config.cover_days * rate)
                deviation = abs(state.stock(facility_id, group) - target) / max(target, 1.0)
                self.stats.imbalance_samples.setdefault(group, []).append(round(deviation, 6))

        if self.spec.consumption:
            self._consume(now)

    def _consume(self, now: datetime) -> None:
        """Demand rates drain stock FIFO (oldest received first). Footprints are
        ceil-scaled with quantity — conservative, never under-counted."""
        state = self.state
        payloads: list[EventPayload] = []
        for facility_id in sorted(state.demand_rates):
            for group, rate in sorted(state.demand_rates[facility_id].items()):
                remaining = rate
                if remaining <= 0:
                    continue
                lots = [
                    lot
                    for zone in state.zones_of(facility_id)
                    for lot in state.lots_in_zone(zone.id)
                    if lot.commodity_group == group and lot.quantity > 0
                ]
                lots.sort(key=lambda lot: ((lot.received_at or now), lot.id))
                for lot in lots:
                    if remaining <= 0:
                        break
                    take = min(remaining, lot.quantity)
                    new_quantity = lot.quantity - take
                    if new_quantity == 0:
                        new_size = CapacityVector(slots=0, volume_l=0, weight_g=0)
                    else:
                        ratio = new_quantity / lot.quantity
                        new_size = CapacityVector(
                            slots=_ceil_scale(lot.size.slots, ratio),
                            volume_l=_ceil_scale(lot.size.volume_l, ratio),
                            weight_g=_ceil_scale(lot.size.weight_g, ratio),
                        )
                    payloads.append(
                        ev.LotQuantityAdjusted(
                            lot_id=lot.id,
                            new_quantity=new_quantity,
                            new_size=new_size,
                            reason="consumption",
                        )
                    )
                    remaining -= take
        if payloads:
            self.append(payloads, now, cause="consumption")


def _ceil_scale(value: int | None, ratio: float) -> int | None:
    if value is None:
        return None
    return math.ceil(value * ratio - 1e-9)
