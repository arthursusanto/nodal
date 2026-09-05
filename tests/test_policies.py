"""Allocation policies (§9): baselines rank myopically but obey hard constraints."""

import pytest

from nodal.allocate import ObjectiveConfig
from nodal.events import EventStore, load_state
from nodal.events import catalog as ev
from nodal.sim.policies import make_policy
from tests.helpers import at, draft, make_shipment

CORE = ObjectiveConfig(packs=["core"])


def test_unknown_policy_raises() -> None:
    with pytest.raises(KeyError, match="unknown policy"):
        make_policy("clairvoyant")


def test_first_available_picks_lowest_id(world_store: EventStore) -> None:
    state = load_state(world_store)
    result = make_policy("first-available").decide(state, "SHP-1", CORE)
    assert result.record.chosen is not None
    assert result.record.chosen.facility_id == "FAC-A"  # lowest feasible id
    assert result.record.policy == "first-available"
    assert result.infeasible_preferred == 0


def test_nearest_feasible_ranks_by_distance(world_store: EventStore) -> None:
    # SHP-1's gate (41.95, -87.65) is nearest FAC-A; FAC-A is feasible.
    state = load_state(world_store)
    result = make_policy("nearest-feasible").decide(state, "SHP-1", CORE)
    assert result.record.chosen is not None
    assert result.record.chosen.facility_id == "FAC-A"


def test_greedy_picks_cheapest_route(world_store: EventStore) -> None:
    state = load_state(world_store)
    result = make_policy("greedy").decide(state, "SHP-1", CORE)
    assert result.record.chosen is not None
    scored = {c.facility_id: c.route.cost_cents for c in result.record.scored}
    assert scored[result.record.chosen.facility_id] == min(scored.values())


def test_infeasible_preferred_are_counted(world_store: EventStore) -> None:
    """A big shipment that fits only FAC-B's bulk zone (95 slots exceeds every
    slot-bounded zone): nearest-feasible prefers FAC-A (closer), fails there, and
    the attempt is counted."""
    world_store.append(
        [draft(ev.ShipmentRegistered(shipment=make_shipment("SHP-BULK", slots=95)), at(0, 9))]
    )
    state = load_state(world_store)
    result = make_policy("nearest-feasible").decide(state, "SHP-BULK", CORE)
    assert result.record.chosen is not None
    assert result.record.chosen.facility_id == "FAC-B"  # only ZON-B2 (no slot bound) fits
    assert result.infeasible_preferred >= 1  # FAC-A was preferred and infeasible


def test_policy_choice_overrides_argmin_but_scoring_is_shared(
    world_store: EventStore,
) -> None:
    """Baseline records score all feasible candidates identically to the engine;
    only the chosen facility differs from the argmin."""
    state = load_state(world_store)
    engine_record = make_policy("nodal-single").decide(state, "SHP-1", CORE).record
    greedy_record = make_policy("greedy").decide(state, "SHP-1", CORE).record
    engine_scores = {(c.facility_id, c.zone_id, c.total) for c in engine_record.scored}
    greedy_scores = {(c.facility_id, c.zone_id, c.total) for c in greedy_record.scored}
    assert engine_scores == greedy_scores
    assert engine_record.considered == greedy_record.considered
