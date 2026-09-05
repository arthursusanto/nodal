"""Drafted batch plans (§7.5): a proposal is an event, not a browser artifact.

`PlanDrafted` puts the plan the optimizer proposed into the log, so it survives
a reload and a replay reproduces it. The fold keeps it PENDING only while it is
the log head — anything appended after it is a change the plan never saw — which
is the same head guard the commit path enforces.
"""

from datetime import timedelta

import pytest

from nodal.allocate import ObjectiveConfig
from nodal.allocate.batch import (
    BatchResult,
    PlanMismatch,
    commit_batch,
    commit_reviewed,
    draft_batch,
    drafted_assignments,
    solve_batch,
)
from nodal.allocate.engine import AllocateError
from nodal.domain.entities import ShipmentStatus
from nodal.events import EventStore, dump_state, load_state, load_state_bytes
from nodal.events import catalog as ev
from tests.helpers import at, draft, make_shipment

CONFIG = ObjectiveConfig()


def _plan(batch_id: str, based_on_seq: int) -> ev.PlanDrafted:
    """A minimal draft: the fold never interprets the records (§4)."""
    return ev.PlanDrafted(
        batch_id=batch_id,
        based_on_seq=based_on_seq,
        meta={"status": "OPTIMAL"},
        records={"SHP-1": {"chosen": {"facility_id": "FAC-A", "zone_id": "ZON-A1"}}},
        assigned=1,
        unassigned=0,
    )


def test_a_draft_is_pending_while_it_is_the_head(world_store: EventStore) -> None:
    head = world_store.last_seq()
    world_store.append([draft(_plan("B-1", head), at(0, 12))])
    state = load_state(world_store)
    assert state.pending_plan is not None
    assert state.pending_plan.batch_id == "B-1"
    assert state.pending_plan.based_on_seq == head
    assert state.last_seq == head + 1


@pytest.mark.parametrize(
    "later",
    [
        ev.ShipmentRegistered(shipment=make_shipment("SHP-LATE")),
        ev.PlanDiscarded(batch_id="B-1", reason="no"),
        ev.BatchSolved(batch_id="B-1", meta=None),
    ],
    ids=["a registration", "its own discard", "a commit"],
)
def test_anything_appended_after_a_draft_stales_it(
    world_store: EventStore, later: ev.EventPayload
) -> None:
    """Pending means head, whatever the later event is: the plan was solved
    against a world that has since moved, so it is no longer reviewable."""
    world_store.append([draft(_plan("B-1", world_store.last_seq()), at(0, 12))])
    world_store.append([draft(later, at(0, 13))])
    assert load_state(world_store).pending_plan is None


def test_a_second_draft_replaces_the_first(world_store: EventStore) -> None:
    world_store.append([draft(_plan("B-1", world_store.last_seq()), at(0, 12))])
    world_store.append([draft(_plan("B-2", world_store.last_seq()), at(0, 13))])
    pending = load_state(world_store).pending_plan
    assert pending is not None and pending.batch_id == "B-2"


def test_replay_sees_the_plan_that_was_pending_then(world_store: EventStore) -> None:
    """`state_at` is the whole point of keeping the proposal in the log: at a
    time when the draft was head, the plan is visible; after it, it is not."""
    world_store.append([draft(_plan("B-1", world_store.last_seq()), at(0, 12))])
    world_store.append([draft(ev.PlanDiscarded(batch_id="B-1"), at(0, 13))])
    during = load_state(world_store, at=at(0, 12))
    assert during.pending_plan is not None and during.pending_plan.batch_id == "B-1"
    assert load_state(world_store, at=at(0, 13)).pending_plan is None


def test_old_logs_fold_unchanged(world_store: EventStore) -> None:
    """A world that never drafted anything has no pending plan — the field is
    additive, so pre-existing logs and snapshots fold exactly as before."""
    assert load_state(world_store).pending_plan is None


def test_a_snapshot_round_trips_the_pending_plan(world_store: EventStore) -> None:
    world_store.append([draft(_plan("B-1", world_store.last_seq()), at(0, 12))])
    state = load_state(world_store)
    restored = load_state_bytes(dump_state(state))
    assert restored.pending_plan == state.pending_plan
    assert restored == state


def test_drafting_refuses_a_stale_result(world_store: EventStore) -> None:
    state = load_state(world_store)
    assert state.last_ts is not None
    result = solve_batch(state, ["SHP-1"], CONFIG, state.last_ts, batch_id="B-1")
    world_store.append([draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-9")), at(0, 12))])
    with pytest.raises(AllocateError, match="stale batch"):
        draft_batch(world_store, result, actor="test")


def test_committing_a_draft_books_exactly_the_plan_that_was_reviewed(
    world_store: EventStore,
) -> None:
    """Determinism (§2) is what makes review meaningful: re-solving on top of
    the draft — the only event since — reproduces it decision for decision, so
    the operator books what they read and the draft stops being pending."""
    state = load_state(world_store)
    assert state.last_ts is not None
    now = state.last_ts
    planned = sorted(sid for sid, s in state.shipments.items() if s.status.value == "planned")
    drafted = solve_batch(state, planned, CONFIG, now, batch_id="B-1")
    draft_batch(world_store, drafted, actor="test")

    pending = load_state(world_store).pending_plan
    assert pending is not None
    assert drafted_assignments(pending) == drafted.assignments

    # The commit re-solves the same world (the draft mutates nothing a solve
    # reads) and must land on the same assignment for every shipment.
    state = load_state(world_store)
    committed = solve_batch(state, planned, CONFIG, now, batch_id=pending.batch_id)
    assert committed.assignments == drafted.assignments
    commit_batch(world_store, committed, actor="test")

    state = load_state(world_store)
    assert state.pending_plan is None, "committing consumes the draft"
    for shipment_id, pair in drafted.assignments.items():
        shipment = state.shipments[shipment_id]
        if pair is None:
            assert shipment.status is ShipmentStatus.PLANNED
        else:
            assert shipment.status is ShipmentStatus.ALLOCATED
            assert shipment.assigned is not None
            assert (shipment.assigned.facility_id, shipment.assigned.zone_ids[0]) == pair


def test_committing_refuses_a_re_solve_that_changes_the_booking_not_the_zone(
    world_store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review guard compares the whole BOOKING, not the destination (§7.5).

    A re-solve that lands on the same (facility, zone) by a different journey —
    a different schedule, a different route, different staging — is a different
    plan from the one the operator read. Comparing destinations alone would wave
    it through, so this forces exactly that divergence and asserts the refusal.
    """
    from nodal.allocate import batch as batch_module

    state = load_state(world_store)
    assert state.last_ts is not None
    now = state.last_ts
    planned = sorted(sid for sid, s in state.shipments.items() if s.status.value == "planned")
    drafted = solve_batch(state, planned, CONFIG, now, batch_id="B-1")
    draft_batch(world_store, drafted, actor="test")
    state = load_state(world_store)
    pending = state.pending_plan
    assert pending is not None

    moved_id = next(sid for sid, r in drafted.records.items() if r.chosen is not None)
    real_solve = batch_module.solve_batch

    def tampered(*args: object, **kwargs: object) -> BatchResult:
        result = real_solve(*args, **kwargs)  # type: ignore[arg-type]
        record = result.records[moved_id]
        assert record.chosen is not None
        # Same hold, three hours later: the destination is untouched, everything
        # the commit would WRITE about the journey is not.
        result.records[moved_id] = record.model_copy(
            update={
                "chosen": record.chosen.model_copy(
                    update={"eta": record.chosen.eta + timedelta(hours=3)}
                )
            }
        )
        return result

    monkeypatch.setattr(batch_module, "solve_batch", tampered)
    head = world_store.last_seq()
    with pytest.raises(PlanMismatch) as raised:
        commit_reviewed(world_store, state, planned, CONFIG, now, batch_id="B-2", actor="test")
    assert raised.value.moved == [moved_id]
    assert world_store.last_seq() == head, "a refused commit books nothing"

    # Load-bearing: the destination-only comparison this replaced sees nothing.
    tampered_result = tampered(state, planned, CONFIG, now, batch_id="B-1")
    assert drafted_assignments(pending) == tampered_result.assignments


def test_commit_reviewed_keeps_the_drafts_batch_id_and_consumes_it(
    world_store: EventStore,
) -> None:
    state = load_state(world_store)
    assert state.last_ts is not None
    now = state.last_ts
    planned = sorted(sid for sid, s in state.shipments.items() if s.status.value == "planned")
    drafted = solve_batch(state, planned, CONFIG, now, batch_id="B-1")
    draft_batch(world_store, drafted, actor="test")
    state = load_state(world_store)

    result, envelopes = commit_reviewed(
        world_store, state, planned, CONFIG, now, batch_id="B-UNUSED", actor="test"
    )
    assert result.batch_id == "B-1", "the commit keeps the plan's own id"
    assert envelopes
    assert load_state(world_store).pending_plan is None
    types = [(e.type, e.entity_id) for e in world_store.read() if e.entity_type == "batch"]
    assert types == [("PlanDrafted", "B-1"), ("BatchSolved", "B-1")]


def test_commit_reviewed_mints_the_fallback_id_when_nothing_is_pending(
    world_store: EventStore,
) -> None:
    state = load_state(world_store)
    assert state.last_ts is not None
    planned = sorted(sid for sid, s in state.shipments.items() if s.status.value == "planned")
    result, envelopes = commit_reviewed(
        world_store, state, planned, CONFIG, state.last_ts, batch_id="B-FRESH", actor="test"
    )
    assert result.batch_id == "B-FRESH"
    assert envelopes


def test_same_instant_events_collapse_in_replay(world_store: EventStore) -> None:
    """`at` resolves to the LAST event at that timestamp, not the first (§11):
    `max_seq_at` is a MAX over seq. So a plan drafted and discarded at the same
    instant replays as discarded — the two never coexist at one `at`, and a
    replay can only land on a draft that outlived the instant it was made."""
    head = world_store.last_seq()
    world_store.append([draft(_plan("B-1", head), at(0, 12))])
    world_store.append([draft(ev.PlanDiscarded(batch_id="B-1", reason="no"), at(0, 12))])
    assert load_state(world_store, at=at(0, 12)).pending_plan is None
    assert load_state(world_store, at=at(0, 11)).pending_plan is None
    # Both events are in the log; only their order at the instant decides.
    assert [e.type for e in world_store.read(from_seq=head + 1)] == [
        "PlanDrafted",
        "PlanDiscarded",
    ]
