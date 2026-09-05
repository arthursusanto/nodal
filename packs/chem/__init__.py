"""Chemical-storage pack (§8, roadmap stage 6).

Adds: a pairwise segregation matrix over chemical compatibility classes
(enforced zone-locally by the core survey and by the batch solver's class
indicators — §7.4), a hazmat facility-certification requirement, an open-yard
storage prohibition, and the `chem.*` attribute namespace. Class names are
deliberately generic (this is an industry-neutral engine): real deployments
rename them in their own pack.
"""

from dataclasses import dataclass

from pydantic import JsonValue

from nodal.domain.entities import Facility, Shipment, StorageZone
from nodal.rules.core import shipment_classes
from nodal.rules.framework import AllocationContext, ConstraintScope, Pack, Reject

CERT_TAG = "cert:hazmat"

# The classes this pack segregates. A shipment line opts in via `compat_class`.
CHEM_CLASSES = frozenset({"flammable", "oxidizer", "corrosive-acid", "corrosive-base"})

# IMDG-style pairwise segregation: these pairs must never share a zone.
SEGREGATION_MATRIX = frozenset(
    {
        frozenset({"flammable", "oxidizer"}),
        frozenset({"corrosive-acid", "corrosive-base"}),
        frozenset({"oxidizer", "corrosive-acid"}),
    }
)


def _chem_classes(shipment: Shipment) -> set[str]:
    # The core's class extraction (requirement-level AND line-level classes):
    # reading only the lines would let a requirement-only class bypass the
    # certification and yard constraints while the core still segregates it.
    return set(shipment_classes(shipment)) & CHEM_CLASSES


@dataclass(frozen=True)
class ChemCertConstraint:
    """Chemical classes require a facility certified for hazmat handling."""

    id: str = "CHEM_CERT"
    scope: ConstraintScope = ConstraintScope.FACILITY

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        classes = _chem_classes(shipment)
        if not classes:
            return None
        assert ctx.eta is not None
        if not facility.certified_for(CERT_TAG, ctx.eta):
            return Reject(
                constraint_id=self.id,
                data={"tag": CERT_TAG, "classes": sorted(classes)},
            )
        return None


@dataclass(frozen=True)
class ChemOpenYardConstraint:
    """Chemical classes must not sit in open yard zones."""

    id: str = "CHEM_OPEN_YARD"
    scope: ConstraintScope = ConstraintScope.ZONE

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        classes = _chem_classes(shipment)
        if not classes:
            return None
        assert zone is not None
        if zone.kind == "yard":
            return Reject(
                constraint_id=self.id,
                data={"zone_kind": zone.kind, "classes": sorted(classes)},
            )
        return None


_UN_CLASSES = {str(n) for n in range(1, 10)}


def _validate_chem_attributes(bag: dict[str, JsonValue]) -> None:
    for key, value in bag.items():
        if key == "chem.un_class":
            if value not in _UN_CLASSES:
                raise ValueError(f"{key} must be a UN class '1'..'9', got {value!r}")
        elif key == "chem.packing_group":
            if value not in ("I", "II", "III"):
                raise ValueError(f"{key} must be 'I', 'II', or 'III', got {value!r}")
        else:
            raise ValueError(f"unknown chem attribute {key!r}")


PACK = Pack(
    name="chem",
    constraints=(ChemCertConstraint(), ChemOpenYardConstraint()),
    incompatible_pairs=SEGREGATION_MATRIX,
    templates={
        "CHEM_CERT": "chemical classes {classes} require {tag}",
        "CHEM_OPEN_YARD": "chemical classes {classes} cannot be stored in an open {zone_kind}",
    },
    attribute_validators={"chem": _validate_chem_attributes},
)
