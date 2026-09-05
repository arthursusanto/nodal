"""What-if forks (§4): hypothetical events on an in-memory overlay.

A fork materializes `state_at(t)`, folds hypothetical events as synthetic
envelopes, re-optimizes existing allocations the hypothesis disrupts (§7.6),
then decides the open shipments under the chosen policy — on the fork AND on an
untouched baseline of the same instant. The report is the diff. Nothing a
what-if does can leak into history: no code path appends to the store, and
`run_whatif` verifies the log head is unchanged before returning.

Grammar (one hypothetical per --event, the four framing categories):
    close FAC-3 14d          facility closure
    cut ZON-2 0.5 7d         capacity cut (magnitude 0.5)
    delay SHP-9 24h          readiness slips 24h (planned/allocated bookings)
    spike 5 12               demand spike: 5 new shipments of 12 slots each
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from nodal.allocate.batch import solve_batch
from nodal.allocate.config import ObjectiveConfig
from nodal.allocate.engine import AllocateError, assignment_of, reservations_of
from nodal.allocate.records import Chosen
from nodal.allocate.reopt import ReoptResult, reoptimize
from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import (
    Disruption,
    DisruptionKind,
    LotSpec,
    RequirementSet,
    Shipment,
    ShipmentStatus,
)
from nodal.events import EventStore, fold, load_state
from nodal.events import catalog as ev
from nodal.events.envelope import Envelope, EventPayload
from nodal.events.state import NetworkState
from nodal.rules.framework import Pack, load_packs

_DURATION = re.compile(r"^(\d+)([dh])$")


class WhatIfError(Exception):
    pass


def _duration(text: str) -> timedelta:
    match = _DURATION.match(text)
    if match is None:
        raise WhatIfError(f"bad duration {text!r} (use e.g. 14d or 36h)")
    value = int(match.group(1))
    return timedelta(days=value) if match.group(2) == "d" else timedelta(hours=value)


def parse_hypothetical(
    text: str, state: NetworkState, now: datetime, index: int
) -> list[EventPayload]:
    """One grammar line -> the event payloads it stands for."""
    parts = text.split()
    if not parts:
        raise WhatIfError("empty hypothetical")
    verb = parts[0].lower()
    if verb == "close" and len(parts) == 3:
        target = parts[1]
        if target not in state.facilities:
            raise WhatIfError(f"unknown facility {target!r}")
        return [
            ev.DisruptionStarted(
                disruption=Disruption(
                    id=f"WHATIF-{index}",
                    kind=DisruptionKind.FACILITY_CLOSED,
                    target_id=target,
                    from_ts=now,
                    until_ts=now + _duration(parts[2]),
                    magnitude=1.0,
                )
            )
        ]
    if verb == "cut" and len(parts) == 4:
        target = parts[1]
        if target not in state.zones:
            raise WhatIfError(f"unknown zone {target!r}")
        magnitude = float(parts[2])
        if not 0.0 < magnitude <= 1.0:
            raise WhatIfError("cut magnitude must be in (0, 1]")
        return [
            ev.DisruptionStarted(
                disruption=Disruption(
                    id=f"WHATIF-{index}",
                    kind=DisruptionKind.CAPACITY_REDUCED,
                    target_id=target,
                    from_ts=now,
                    until_ts=now + _duration(parts[3]),
                    magnitude=magnitude,
                )
            )
        ]
    if verb == "delay" and len(parts) == 3:
        target = parts[1]
        shipment = state.shipments.get(target)
        if shipment is None:
            raise WhatIfError(f"unknown shipment {target!r}")
        if shipment.status not in (ShipmentStatus.PLANNED, ShipmentStatus.ALLOCATED):
            raise WhatIfError(
                f"{target} already departed; only planned/allocated bookings can be delayed"
            )
        # The planner reads ready_at, so the readiness slip is what makes the
        # delay REAL: the re-decision departs later, and a broken deadline or a
        # changed stay window shows up in the diff.
        new_ready = max(shipment.ready_at, now) + _duration(parts[2])
        delay_payloads: list[EventPayload] = [
            ev.ShipmentReadyChanged(shipment_id=target, new_ready=new_ready, reason="what-if")
        ]
        if shipment.status is ShipmentStatus.ALLOCATED:
            delay_payloads.append(
                ev.DisruptionStarted(
                    disruption=Disruption(
                        id=f"WHATIF-{index}",
                        kind=DisruptionKind.SHIPMENT_DELAYED,
                        target_id=target,
                        from_ts=now,
                        until_ts=new_ready,
                        magnitude=0.0,
                    )
                )
            )
        return delay_payloads
    if verb == "spike" and len(parts) == 3:
        count, slots = int(parts[1]), int(parts[2])
        if count <= 0 or slots <= 0:
            raise WhatIfError("spike needs positive count and size")
        facilities = list(state.facilities.values())
        if not facilities:
            raise WhatIfError("no facilities to spike demand against")
        lat = sum(f.lat for f in facilities) / len(facilities)
        lon = sum(f.lon for f in facilities) / len(facilities)
        payloads: list[EventPayload] = []
        for i in range(count):
            size = CapacityVector(slots=slots)
            payloads.append(
                ev.ShipmentRegistered(
                    shipment=Shipment(
                        id=f"WHATIF-SPIKE-{index}-{i + 1}",
                        origin_label="what-if spike",
                        origin_lat=lat,
                        origin_lon=lon,
                        lines=[
                            LotSpec(
                                sku="WHATIF",
                                commodity_group="general",
                                quantity=slots,
                                size=size,
                            )
                        ],
                        requirements=RequirementSet(size=size, required_tags=[]),
                        ready_at=now,
                    )
                )
            )
        return payloads
    raise WhatIfError(
        f"cannot parse {text!r} (grammar: 'close FAC d', 'cut ZON m d', 'delay SHP h', 'spike n s')"
    )


def _fold_synthetic(state: NetworkState, payloads: list[EventPayload], now: datetime) -> None:
    for i, payload in enumerate(payloads):
        entity_type, entity_id = payload.entity_ref()
        fold(
            [
                Envelope(
                    seq=state.last_seq + 1,
                    id=f"EVT-whatif-{state.last_seq + 1}-{i}",
                    ts=now,
                    type=payload.EVENT_TYPE,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    payload=payload,
                    actor="whatif",
                )
            ],
            into=state,
        )


def _apply_chosen(state: NetworkState, shipment_id: str, chosen: Chosen, now: datetime) -> None:
    """Fold a decision (+ reservations) into a fork so later decisions see it."""
    _fold_synthetic(
        state,
        [
            ev.AllocationDecided(
                shipment_id=shipment_id,
                assignment=assignment_of(chosen),
                record={},
            ),
            *(
                ev.ReservationPlaced(reservation=reservation)
                for reservation in reservations_of(shipment_id, chosen)
            ),
        ],
        now,
    )


@dataclass(frozen=True)
class Outcome:
    """One side's final decision surface: shipment -> (facility, zone) or None."""

    assignments: dict[str, tuple[str, str] | None]
    cost_cents: dict[str, int]

    @property
    def allocated(self) -> int:
        return sum(1 for pair in self.assignments.values() if pair is not None)

    @property
    def unassigned(self) -> int:
        return sum(1 for pair in self.assignments.values() if pair is None)

    @property
    def total_cost_cents(self) -> int:
        return sum(self.cost_cents.values())


@dataclass(frozen=True)
class WhatIfChange:
    shipment_id: str
    before: tuple[str, str] | None
    after: tuple[str, str] | None
    cost_delta_cents: int


@dataclass(frozen=True)
class WhatIfReport:
    at: datetime
    hypotheticals: list[str]
    policy: str
    baseline: Outcome
    fork: Outcome
    changes: list[WhatIfChange]
    reopts: list[ReoptResult]  # the fork's re-optimizations, one per disruption

    def render(self) -> str:
        lines = [
            f"what-if at {self.at.isoformat()}  (policy {self.policy})",
            *(f"  hypothesis: {text}" for text in self.hypotheticals),
            (
                f"baseline: {self.baseline.allocated} allocated, "
                f"{self.baseline.unassigned} unassigned, "
                f"${self.baseline.total_cost_cents / 100:.2f}"
            ),
            (
                f"fork:     {self.fork.allocated} allocated, "
                f"{self.fork.unassigned} unassigned, "
                f"${self.fork.total_cost_cents / 100:.2f}"
            ),
        ]
        for reopt in self.reopts:
            lines.append(
                f"re-optimization ({reopt.trigger}): tier {reopt.tier}, "
                f"{len(reopt.affected)} affected, {len(reopt.changed)} moved"
            )
        if not self.changes:
            lines.append("no decisions change under this hypothesis")
        else:
            lines.append(f"changed decisions ({len(self.changes)}):")
            for change in self.changes:
                before = "/".join(change.before) if change.before else "UNASSIGNED"
                after = "/".join(change.after) if change.after else "UNASSIGNED"
                lines.append(
                    f"  {change.shipment_id:<16} {before:>18} -> {after:<18} "
                    f"(cost {change.cost_delta_cents / 100:+.2f})"
                )
        lines.append("baseline log untouched (fork was in-memory only)")
        return "\n".join(lines)


def _decide_open(
    state: NetworkState,
    config: ObjectiveConfig,
    policy: str,
    now: datetime,
    packs: list[Pack],
) -> Outcome:
    """Decide every planned shipment on this (already-forked) state."""
    assignments: dict[str, tuple[str, str] | None] = {}
    cost_cents: dict[str, int] = {}
    # Existing allocations are part of the surface too (reopt may have moved them).
    for sid, shipment in sorted(state.shipments.items()):
        if shipment.status is ShipmentStatus.ALLOCATED and shipment.assigned is not None:
            assignments[sid] = (shipment.assigned.facility_id, shipment.assigned.zone_ids[0])
            cost_cents[sid] = 0  # transport cost of incumbents is not re-counted
    planned = sorted(
        sid for sid, s in state.shipments.items() if s.status is ShipmentStatus.PLANNED
    )
    if not planned:
        return Outcome(assignments, cost_cents)
    if policy == "nodal-batch":
        result = solve_batch(state, planned, config, now, batch_id="WHATIF", packs=packs)
        for sid in planned:
            assignments[sid] = result.assignments[sid]
            record = result.records[sid]
            cost_cents[sid] = record.chosen.route.cost_cents if record.chosen else 0
    else:
        from nodal.sim.policies import make_policy

        chooser = make_policy(policy)
        if not hasattr(chooser, "decide"):
            raise WhatIfError(f"policy {policy!r} cannot decide single shipments")
        for sid in planned:
            record = chooser.decide(state, sid, config, now).record
            if record.chosen is None:
                assignments[sid] = None
                cost_cents[sid] = 0
                continue
            assignments[sid] = (record.chosen.facility_id, record.chosen.zone_id)
            cost_cents[sid] = record.chosen.route.cost_cents
            _apply_chosen(state, sid, record.chosen, now)
    return Outcome(assignments, cost_cents)


def run_whatif(
    store: EventStore,
    config: ObjectiveConfig,
    policy: str,
    hypotheticals: list[str],
    at: datetime | None = None,
) -> WhatIfReport:
    """Fork, hypothesize, re-decide, diff. Never writes to the store."""
    if not hypotheticals:
        raise WhatIfError("nothing to test: pass at least one --event")
    packs = load_packs(config.packs)
    head = store.last_seq()
    baseline = load_state(store, at=at)
    fork = load_state(store, at=at)
    now = fork.last_ts
    if now is None:
        raise WhatIfError("empty world")

    payloads: list[EventPayload] = []
    for index, text in enumerate(hypotheticals, start=1):
        payloads.extend(parse_hypothetical(text, fork, now, index))
    _fold_synthetic(fork, payloads, now)

    # Existing allocations the hypothesis disrupts get the §7.6 treatment on the
    # fork. commit=False: the changes are folded synthetically, never appended.
    reopts: list[ReoptResult] = []
    fork_costs: dict[str, int] = {}
    baseline_costs: dict[str, int] = {}
    for payload in payloads:
        if not isinstance(payload, ev.DisruptionStarted):
            continue
        partial = reoptimize(
            store, fork, payload.disruption.id, config, now, packs=packs, commit=False
        )
        # Tier 0 means nothing was re-solved — but a closure whose whole reach is
        # cargo it strands returns exactly that, with a populated `trapped` list.
        # Dropping it would make the preview silent about the goods the
        # hypothesis would strand, which is the one thing it cannot re-route.
        if partial.tier == 0:
            if partial.trapped:
                reopts.append(partial)
            continue
        reopts.append(partial)
        assert partial.result is not None
        for sid in partial.changed:
            incumbent = fork.shipments[sid].assigned
            _fold_synthetic(
                fork,
                [ev.AllocationSuperseded(shipment_id=sid, old_decision_seq=0, reason="what-if")],
                now,
            )
            record = partial.result.records[sid]
            if record.chosen is not None:
                _apply_chosen(fork, sid, record.chosen, now)
                fork_costs[sid] = record.chosen.route.cost_cents
            # The incumbent's transport cost, if it still scored, anchors the delta.
            if incumbent is not None:
                for candidate in record.scored:
                    if (candidate.facility_id, candidate.zone_id) == (
                        incumbent.facility_id,
                        incumbent.zone_ids[0],
                    ):
                        baseline_costs[sid] = candidate.route.cost_cents
                        break

    try:
        baseline_outcome = _decide_open(baseline, config, policy, now, packs)
        fork_outcome = _decide_open(fork, config, policy, now, packs)
    except AllocateError as err:
        raise WhatIfError(str(err)) from err
    fork_outcome.cost_cents.update(fork_costs)
    baseline_outcome.cost_cents.update(baseline_costs)

    changes = []
    for sid in sorted(set(baseline_outcome.assignments) | set(fork_outcome.assignments)):
        before = baseline_outcome.assignments.get(sid)
        after = fork_outcome.assignments.get(sid)
        if before != after:
            changes.append(
                WhatIfChange(
                    shipment_id=sid,
                    before=before,
                    after=after,
                    cost_delta_cents=(
                        fork_outcome.cost_cents.get(sid, 0)
                        - baseline_outcome.cost_cents.get(sid, 0)
                    ),
                )
            )
    assert store.last_seq() == head, "what-if must never write to the baseline log"
    return WhatIfReport(
        at=now,
        hypotheticals=list(hypotheticals),
        policy=policy,
        baseline=baseline_outcome,
        fork=fork_outcome,
        changes=changes,
        reopts=reopts,
    )
