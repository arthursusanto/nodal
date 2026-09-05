"""Pack registry, conformance suite, and the thin coldchain pack (§8, stage 2)."""

import pytest

from nodal.allocate import ObjectiveConfig, allocate
from nodal.events import EventStore, load_state
from nodal.rules.framework import ConstraintScope, PackError, load_packs, validate_attributes
from nodal.rules.messages import render_reject


def test_load_packs_resolves_and_orders() -> None:
    packs = load_packs(["core", "coldchain", "chem"])
    assert [p.name for p in packs] == ["core", "coldchain", "chem"]


def test_unknown_pack_raises() -> None:
    with pytest.raises(PackError, match="nonexistent"):
        load_packs(["nonexistent"])


@pytest.mark.parametrize("pack_name", ["core", "coldchain", "chem"])
def test_pack_conformance(pack_name: str, world_store: EventStore) -> None:
    """Shared conformance suite (§8, finalized stage 6): unique ids, valid
    scopes, pure checks, rejections render, well-formed segregation pairs, and
    well-formed pure objective components."""
    (pack,) = load_packs([pack_name])
    ids = [c.id for c in pack.constraints]
    assert len(ids) == len(set(ids))
    for constraint in pack.constraints:
        assert constraint.id
        assert isinstance(constraint.scope, ConstraintScope)
    # Purity: run every constraint twice on a live candidate and compare.
    from tests.test_constraints import ctx_for, shipment_with

    state = load_state(world_store)
    shipment = shipment_with(temp_c=(-5, 4))
    ctx = ctx_for(state, packs=[pack])
    for constraint in pack.constraints:
        zone = state.zones["ZON-A2"] if constraint.scope is ConstraintScope.ZONE else None
        first = constraint.check(shipment, state.facilities["FAC-A"], zone, ctx)
        second = constraint.check(shipment, state.facilities["FAC-A"], zone, ctx)
        assert first == second
        if first is not None:
            assert render_reject(first, [pack])
    # Every pack template renders safely even with missing data.
    for constraint_id in pack.templates:
        from nodal.rules.framework import Reject

        assert render_reject(Reject(constraint_id=constraint_id, data={}), [pack])
    # Segregation pairs: exactly two distinct non-empty class names each.
    for pair in pack.incompatible_pairs:
        assert len(pair) == 2
        assert all(isinstance(cls, str) and cls for cls in pair)
    # Objective components: namespaced, uniquely named, non-negative default
    # weight, pure, and numeric.
    from nodal.rules.framework import PackStay

    names = [component.name for component in pack.objective_components]
    assert len(names) == len(set(names))
    assert ctx.eta is not None and ctx.departure is not None
    stay = PackStay(route=None, eta=ctx.eta, departure=ctx.departure)
    for component in pack.objective_components:
        assert component.name.startswith(f"{pack.name}.")
        assert component.default_weight >= 0
        zone = state.zones["ZON-A2"]
        first_value = component.compute(shipment, state.facilities["FAC-A"], zone, stay)
        assert first_value == component.compute(shipment, state.facilities["FAC-A"], zone, stay)
        raw, normalized = first_value
        assert isinstance(raw, float) and isinstance(normalized, float)


def test_coldchain_requires_cert_and_disabling_removes_it(world_store: EventStore) -> None:
    """Stage 2 acceptance: the pack's constraint bites when active and disappears
    cleanly when the pack is disabled."""
    state = load_state(world_store)
    with_pack = allocate(state, "SHP-2", ObjectiveConfig(packs=["core", "coldchain"]))
    assert with_pack.chosen is None  # nobody holds cert:coldchain
    fac_a = next(r for r in with_pack.rejected if r.facility_id == "FAC-A")
    assert any(v.constraint_id == "COLDCHAIN_CERT" for v in fac_a.facility_verdicts)

    without = allocate(state, "SHP-2", ObjectiveConfig(packs=["core"]))
    assert without.chosen is not None
    assert without.chosen.facility_id == "FAC-A"
    assert not any(
        v.constraint_id == "COLDCHAIN_CERT" for r in without.rejected for v in r.facility_verdicts
    )


def test_coldchain_attribute_validators() -> None:
    (pack,) = load_packs(["coldchain"])
    validate_attributes({"coldchain.max_excursion_minutes": 120}, [pack])
    validate_attributes({"coldchain.band": "frozen"}, [pack])
    with pytest.raises(ValueError, match="band"):
        validate_attributes({"coldchain.band": "lukewarm"}, [pack])
    with pytest.raises(ValueError, match="max_excursion"):
        validate_attributes({"coldchain.max_excursion_minutes": -5}, [pack])
    with pytest.raises(ValueError, match="unknown coldchain"):
        validate_attributes({"coldchain.rocket": True}, [pack])


def test_core_pack_is_restriction_free() -> None:
    (core,) = load_packs(["core"])
    assert core.constraints == ()
    assert core.incompatible_pairs == frozenset()


def test_mixed_pack_scenario_allocates_and_segregates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Stage-6 acceptance: cold-chain and chem shipments in one network under
    both packs allocate correctly, the log replays, and no zone ever ends up
    holding incompatible classes."""
    from pathlib import Path

    from nodal.rules.framework import incompatible
    from nodal.sim.runner import run_scenario
    from nodal.sim.scenarios import load_scenario

    spec = load_scenario(Path("scenarios") / "mixed-pack.yaml").model_copy(
        update={"horizon_days": 6}
    )
    db = Path(tmp_path) / "mixed.sqlite3"
    stats = run_scenario(spec, "nodal-batch", db)
    allocated = [d for d in stats.decisions if d.allocated]
    assert allocated, "mixed-pack scenario allocated nothing"
    with EventStore(db) as store:
        state = load_state(store)  # full replay
    packs = load_packs(["core", "coldchain", "chem"])
    for zone_id in state.zones:
        classes = sorted(
            {
                lot.compat_class
                for lot in state.lots_in_zone(zone_id)
                if lot.compat_class is not None
            }
        )
        for i, class_a in enumerate(classes):
            for class_b in classes[i + 1 :]:
                assert not incompatible(class_a, class_b, packs), (
                    f"{zone_id} holds {class_a} with {class_b}"
                )


CHEM_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    certifications:
      - { tag: "cert:hazmat", valid_until: 2027-01-01T00:00:00+00:00 }
    zones:
      - { id: Z1, kind: rack, capacity: { slots: 40 } }
      - { id: Z2, kind: rack, capacity: { slots: 40 } }
      - { id: ZY, kind: yard, capacity: { slots: 40 } }
  - id: F2
    lat: 40.0
    lon: -100.1
    zones:
      - { id: Z3, kind: rack, capacity: { slots: 40 } }
lots:
  - { id: BASE-1, zone: Z1, group: general, quantity: 5, size: { slots: 5 },
      compat_class: corrosive-base }
shipments:
  - id: ACID-1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    lines:
      - { sku: HCL, group: general, quantity: 5, size: { slots: 5 },
          compat_class: corrosive-acid }
    requirements: { compat_class: corrosive-acid }
"""


def test_chem_pack_filters_and_names_the_matrix_entry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Stage-6 acceptance: incompatible classes never share a zone (and the
    rejection names the matrix entry), hazmat needs the cert, yards refuse
    chemicals — and a clean certified zone still wins."""
    from pathlib import Path

    from nodal.worlds import load_world

    world = Path(tmp_path) / "chem.yaml"
    world.write_text(CHEM_WORLD, encoding="utf-8")
    with EventStore(Path(tmp_path) / "chem.sqlite3") as store:
        load_world(world, store)
        state = load_state(store)
        record = allocate(state, "ACID-1", ObjectiveConfig(packs=["core", "chem"]))
        assert record.chosen is not None
        assert (record.chosen.facility_id, record.chosen.zone_id) == ("F1", "Z2")
        # The uncertified facility is rejected by the cert constraint.
        f2 = next(r for r in record.rejected if r.facility_id == "F2")
        assert any(v.constraint_id == "CHEM_CERT" for v in f2.facility_verdicts)
        # F1 is feasible overall, so its bad zones show up as zone verdicts on
        # the chosen record's survey: the base-holding zone names the matrix
        # entry that fired; the yard refuses chemicals outright.
        chosen_candidate = next(
            c for c in record.scored if (c.facility_id, c.zone_id) == ("F1", "Z2")
        )
        z1_verdicts = chosen_candidate.zone_verdicts["Z1"]
        segregation = next(v for v in z1_verdicts if v.constraint_id == "SEGREGATION")
        assert segregation.data["compat_class"] == "corrosive-acid"
        assert segregation.data["conflicts"] == ["corrosive-base"]
        zy_verdicts = chosen_candidate.zone_verdicts["ZY"]
        assert any(v.constraint_id == "CHEM_OPEN_YARD" for v in zy_verdicts)


# NEAR is 2 km away but congested (a lot fills half its 12-slot cold zone);
# FAR is 26 km away, huge and empty. The excursion weight decides which pain
# wins: priced out, FAR's roomy zone is cheaper; priced heavily, the 24-minute
# unrefrigerated ride to FAR costs more than NEAR's congestion.
EXCURSION_WORLD = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: NEAR
    lat: 40.0
    lon: -100.02
    certifications:
      - { tag: "cert:coldchain", valid_until: 2027-01-01T00:00:00+00:00 }
    zones:
      - { id: ZN, kind: cold, capacity: { slots: 12 }, temp_c: [-25, 5] }
  - id: FAR
    lat: 40.0
    lon: -100.4
    certifications:
      - { tag: "cert:coldchain", valid_until: 2027-01-01T00:00:00+00:00 }
    zones:
      - { id: ZF, kind: cold, capacity: { slots: 400 }, temp_c: [-25, 5] }
lots:
  - { id: HELD, zone: ZN, group: perishable, quantity: 6, size: { slots: 6 },
      compat_class: food }
shipments:
  - id: COLD-1
    origin_lat: 40.0
    origin_lon: -100.0
    ready: 2026-09-01T06:00:00+00:00
    lines:
      - { sku: VAX, group: perishable, quantity: 5, size: { slots: 5 },
          attributes: { "coldchain.max_excursion_minutes": 60 } }
    requirements: { temp_c: [-5, 4] }
"""


def test_excursion_objective_scores_and_steers(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Stage-6 acceptance for the coldchain objective: the excursion component
    appears in the record with the pack's arithmetic, and the pack_weights
    override genuinely flips the choice in BOTH directions."""
    from pathlib import Path

    from nodal.worlds import load_world

    world = Path(tmp_path) / "excursion.yaml"
    world.write_text(EXCURSION_WORLD, encoding="utf-8")
    with EventStore(Path(tmp_path) / "excursion.sqlite3") as store:
        load_world(world, store)
        state = load_state(store)
        config = ObjectiveConfig(packs=["core", "coldchain"])
        record = allocate(state, "COLD-1", config)
        assert record.chosen is not None
        chosen = next(
            c
            for c in record.scored
            if (c.facility_id, c.zone_id) == (record.chosen.facility_id, record.chosen.zone_id)
        )
        excursion = chosen.components["coldchain.excursion"]
        assert excursion.raw == chosen.route.minutes  # transport exposure
        assert excursion.normalized == pytest.approx(excursion.raw / 60)  # explicit budget
        assert excursion.contribution == pytest.approx(0.15 * excursion.normalized)

        # The flip, both directions: excursion priced out -> the roomy FAR zone
        # wins on congestion; priced heavily -> the short ride to NEAR wins.
        flat = config.model_copy(update={"pack_weights": {"coldchain.excursion": 0.0}})
        heavy = config.model_copy(update={"pack_weights": {"coldchain.excursion": 5.0}})
        chosen_flat = allocate(state, "COLD-1", flat).chosen
        chosen_heavy = allocate(state, "COLD-1", heavy).chosen
        assert chosen_flat is not None and chosen_heavy is not None
        assert chosen_flat.facility_id == "FAR"
        assert chosen_heavy.facility_id == "NEAR"


def test_single_batch_prices_pack_components_identically(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The stage's central invariant: with a pack objective component active,
    a single-shipment batch solve selects the scorer's argmin AND reports the
    scorer's total as its objective — deleting the pack term from either side
    breaks this."""
    from pathlib import Path

    from nodal.allocate.batch import solve_batch
    from nodal.domain.units import OBJECTIVE_SCALE
    from nodal.worlds import load_world

    world = Path(tmp_path) / "excursion.yaml"
    world.write_text(EXCURSION_WORLD, encoding="utf-8")
    with EventStore(Path(tmp_path) / "excursion.sqlite3") as store:
        load_world(world, store)
        state = load_state(store)
        assert state.last_ts is not None
        config = ObjectiveConfig(packs=["core", "coldchain"])
        single = allocate(state, "COLD-1", config)
        assert single.chosen is not None
        batch = solve_batch(state, ["COLD-1"], config, state.last_ts, batch_id="B1")
        assert batch.assignments["COLD-1"] == (
            single.chosen.facility_id,
            single.chosen.zone_id,
        )
        assert batch.meta.objective_scaled / OBJECTIVE_SCALE == pytest.approx(
            single.scored[0].total, abs=2e-4
        )


def test_chem_pack_reads_requirement_level_classes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A class declared only at the requirement level is segregated by the core
    and MUST also trigger the chem pack's certification constraint — reading
    only the lines would let it slip into an uncertified facility."""
    from pathlib import Path

    from nodal.worlds import load_world

    world_text = CHEM_WORLD.replace(
        """    lines:
      - { sku: HCL, group: general, quantity: 5, size: { slots: 5 },
          compat_class: corrosive-acid }
    requirements: { compat_class: corrosive-acid }""",
        """    lines:
      - { sku: HCL, group: general, quantity: 5, size: { slots: 5 } }
    requirements: { compat_class: corrosive-acid }""",
    )
    assert world_text != CHEM_WORLD  # the replacement actually applied
    world = Path(tmp_path) / "chem-req.yaml"
    world.write_text(world_text, encoding="utf-8")
    with EventStore(Path(tmp_path) / "chem-req.sqlite3") as store:
        load_world(world, store)
        state = load_state(store)
        record = allocate(state, "ACID-1", ObjectiveConfig(packs=["core", "chem"]))
        f2 = next(r for r in record.rejected if r.facility_id == "F2")
        assert any(v.constraint_id == "CHEM_CERT" for v in f2.facility_verdicts)
        assert record.chosen is not None
        assert record.chosen.facility_id == "F1"  # the certified one


def test_world_load_validates_attribute_bags_under_packs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """§8 ingestion enforcement: a bad attribute bag fails the load atomically
    when packs are given, and loads untouched when validation is off."""
    from pathlib import Path

    from nodal.worlds import WorldError, load_world

    world_text = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: cold, capacity: { slots: 20 }, temp_c: [-25, 5] }]
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    lines:
      - { sku: VAX, group: perishable, quantity: 5, size: { slots: 5 },
          attributes: { "coldchain.band": "lukewarm" } }
    requirements: { temp_c: [-5, 4] }
"""
    world = Path(tmp_path) / "bad.yaml"
    world.write_text(world_text, encoding="utf-8")
    packs = load_packs(["core", "coldchain"])
    with EventStore(Path(tmp_path) / "checked.sqlite3") as store:
        with pytest.raises(WorldError, match="lukewarm"):
            load_world(world, store, packs=packs)
        assert store.last_seq() == 0  # nothing landed
    with EventStore(Path(tmp_path) / "unchecked.sqlite3") as store:
        assert load_world(world, store) > 0  # validation off by default


def test_world_load_validates_requirements_attributes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The shipment-level requirements bag is a validation site too — a bad
    attribute there must fail the load, not slip through to solve time."""
    from pathlib import Path

    from nodal.worlds import WorldError, load_world

    world_text = """
start: 2026-09-01T00:00:00+00:00
facilities:
  - id: F1
    lat: 40.0
    lon: -100.0
    zones: [{ id: Z1, kind: cold, capacity: { slots: 20 }, temp_c: [-25, 5] }]
shipments:
  - id: S1
    origin_lat: 40.0
    origin_lon: -100.02
    ready: 2026-09-01T06:00:00+00:00
    lines:
      - { sku: VAX, group: perishable, quantity: 5, size: { slots: 5 } }
    requirements:
      temp_c: [-5, 4]
      attributes: { "coldchain.band": "lukewarm" }
"""
    world = Path(tmp_path) / "bad-req.yaml"
    world.write_text(world_text, encoding="utf-8")
    packs = load_packs(["core", "coldchain"])
    with EventStore(Path(tmp_path) / "req-checked.sqlite3") as store:
        with pytest.raises(WorldError, match="S1 requirements"):
            load_world(world, store, packs=packs)
        assert store.last_seq() == 0  # nothing landed


def test_chem_attribute_validators() -> None:
    (pack,) = load_packs(["chem"])
    validate_attributes({"chem.un_class": "3"}, [pack])
    validate_attributes({"chem.packing_group": "II"}, [pack])
    with pytest.raises(ValueError, match="un_class"):
        validate_attributes({"chem.un_class": "10"}, [pack])
    with pytest.raises(ValueError, match="packing_group"):
        validate_attributes({"chem.packing_group": "IV"}, [pack])
    with pytest.raises(ValueError, match="unknown chem"):
        validate_attributes({"chem.explosive": True}, [pack])
