"""Constraint framework (§7.2) and pack registry (§8).

A constraint is a pure function producing `None` (pass) or a `Reject` carrying the
constraint id and machine-readable data. Human sentences are rendered on demand from
id + data (messages.py) and never stored in the event log.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from importlib import import_module, metadata
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from nodal.domain.entities import Facility, Shipment, StorageZone
from nodal.events.state import NetworkState

if TYPE_CHECKING:
    from datetime import date

    from nodal.allocate.config import ObjectiveConfig
    from nodal.allocate.scorer import EvalCache
    from nodal.domain.capacity import CapacityVector
    from nodal.network.journey import DeliveryPlan
    from nodal.network.travel import Route, TravelModel


class Reject(BaseModel):
    model_config = ConfigDict(frozen=True)

    constraint_id: str
    data: dict[str, JsonValue] = {}


class ConstraintScope(StrEnum):
    FACILITY = "facility"  # checked once per facility (zone is None)
    ZONE = "zone"  # checked per candidate zone


@dataclass(frozen=True)
class AllocationContext:
    """Everything a constraint may consult. Constraints must not mutate any of it."""

    state: NetworkState
    config: "ObjectiveConfig"
    travel: "TravelModel"
    now: datetime
    packs: Sequence["Pack"]
    cache: "EvalCache | None" = None  # per-decision memo of time-phased queries (§15)
    # Per-candidate stay window, set by the allocator before zone checks run:
    eta: datetime | None = None
    departure: datetime | None = None
    route: "Route | None" = None
    # A->B delivery (§7.9): the routing beyond the hold. Set only for shipments
    # carrying a destination, so a constraint that ignores it behaves as before.
    delivery: "DeliveryPlan | None" = None
    # What THIS candidate's own earlier dwells already took out of a zone's
    # headroom, per (zone, UTC day bucket) (§7.9). A journey that passes the same
    # dock twice must find it half full the second time — the batch model sums
    # both dwells onto one capacity row, and this is how the per-stop path agrees.
    pending: "dict[tuple[str, date], CapacityVector]" = field(default_factory=dict)

    def headroom_at(self, zone_id: str, day: "date") -> "CapacityVector":
        headroom = (
            self.cache.headroom(zone_id, day)
            if self.cache is not None
            else self.state.headroom(zone_id, day)
        )
        own = self.pending.get((zone_id, day))
        return headroom if own is None else headroom.minus_demand(own)

    def with_stay(
        self,
        eta: datetime,
        departure: datetime,
        route: "Route | None",
        delivery: "DeliveryPlan | None" = None,
    ) -> "AllocationContext":
        return AllocationContext(
            state=self.state,
            config=self.config,
            travel=self.travel,
            now=self.now,
            packs=self.packs,
            cache=self.cache,
            eta=eta,
            departure=departure,
            route=route,
            delivery=delivery,
        )

    def with_pending(
        self, pending: "dict[tuple[str, date], CapacityVector]"
    ) -> "AllocationContext":
        return AllocationContext(
            state=self.state,
            config=self.config,
            travel=self.travel,
            now=self.now,
            packs=self.packs,
            cache=self.cache,
            eta=self.eta,
            departure=self.departure,
            route=self.route,
            delivery=self.delivery,
            pending=pending,
        )


class Constraint(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def scope(self) -> ConstraintScope: ...

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None: ...


@dataclass(frozen=True)
class PackStay:
    """The candidate stay a pack objective component prices (§8). Deliberately
    narrow: no NetworkState handle, so a component CANNOT read other shipments —
    separability is enforced by what the interface withholds."""

    route: "Route | None"  # None: already on site
    eta: datetime
    departure: datetime


@dataclass(frozen=True)
class PackComponent:
    """A pack-contributed objective component (§8).

    MUST be separable: a pure per-candidate level with no cross-shipment
    coupling, so the batch model prices it exactly like the core separable
    components (§7.4). `compute` returns (raw, normalized); the contribution is
    weight x normalized, with the weight taken from the profile's
    `pack_weights[name]` or falling back to `default_weight`. Names are
    namespaced `<pack>.<component>` so profiles and records stay unambiguous.
    """

    name: str
    default_weight: float
    compute: Callable[[Shipment, Facility, StorageZone, PackStay], tuple[float, float]]


@dataclass(frozen=True)
class Pack:
    """An industry rule pack (§8). The core never imports one; it loads them."""

    name: str
    constraints: tuple[Constraint, ...] = ()
    # Pairwise segregation over compat classes: frozenset pairs that must not share a zone.
    incompatible_pairs: frozenset[frozenset[str]] = frozenset()
    # Message templates for this pack's constraint ids (merged into the renderer).
    templates: dict[str, str] = field(default_factory=dict)
    # Attribute validators: namespace prefix -> callable raising ValueError on bad bags.
    attribute_validators: dict[str, Callable[[dict[str, JsonValue]], None]] = field(
        default_factory=dict
    )
    # Optional extra objective components (§8) — separable by contract.
    objective_components: tuple[PackComponent, ...] = ()


class PackError(Exception):
    pass


@lru_cache(maxsize=32)
def _load_packs_cached(names: tuple[str, ...]) -> tuple[Pack, ...]:
    entry_points = {ep.name: ep for ep in metadata.entry_points(group="nodal.packs")}
    loaded: list[Pack] = []
    component_names: set[str] = set()
    for name in names:
        if name in entry_points:
            pack = entry_points[name].load()
        else:
            try:
                pack = import_module(f"packs.{name}").PACK
            except (ImportError, AttributeError) as err:
                raise PackError(f"unknown pack {name!r}") from err
        if not isinstance(pack, Pack):
            raise PackError(f"pack {name!r} did not resolve to a Pack")
        # Objective components MUST be namespaced "<pack>.<component>": the
        # batch model classifies pack components by the dot (§7.4), and the
        # namespace makes collision with a core component name impossible
        # (core names carry no dot). Enforced here, not just in conformance
        # tests, so entry-point packs cannot silently corrupt the objective.
        for component in pack.objective_components:
            if not component.name.startswith(f"{pack.name}."):
                raise PackError(
                    f"pack {pack.name!r} objective component {component.name!r} "
                    f"must be namespaced {pack.name!r}.<component>"
                )
            if component.name in component_names:
                raise PackError(f"duplicate objective component {component.name!r}")
            component_names.add(component.name)
        loaded.append(pack)
    return tuple(loaded)


def load_packs(names: Sequence[str]) -> list[Pack]:
    """Resolve pack names in config order: entry points first, then `packs.<name>`.

    Memoized per name-tuple — the entry-point scan costs milliseconds and this
    is called on every decision; packs are frozen and safely shared."""
    return list(_load_packs_cached(tuple(names)))


def incompatible(class_a: str, class_b: str, packs: Sequence[Pack]) -> bool:
    """Two compat classes must not share a zone if any active pack says so."""
    if class_a == class_b:
        return False
    pair = frozenset((class_a, class_b))
    return any(pair in pack.incompatible_pairs for pack in packs)


def validate_attributes(bag: dict[str, JsonValue], packs: Sequence[Pack]) -> None:
    """Run pack attribute validators over a bag (keys are namespaced, `pack.attr`)."""
    for pack in packs:
        for prefix, validator in pack.attribute_validators.items():
            scoped = {k: v for k, v in bag.items() if k.startswith(prefix + ".")}
            if scoped:
                validator(scoped)
