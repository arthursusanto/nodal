"""Cold-chain pack (§8; completed in roadmap stage 6).

Adds: a certification requirement for temperature-controlled shipments, the
`coldchain.*` attribute namespace, and the lane temperature-excursion budget
objective — every minute a temperature-controlled shipment spends in transit
draws down an excursion budget, so among feasible destinations the objective
prefers shorter unrefrigerated exposure. Separable by construction (a pure
per-route level), so the batch model prices it exactly (§7.4).
"""

from dataclasses import dataclass

from pydantic import JsonValue

from nodal.domain.entities import Facility, Shipment, StorageZone
from nodal.rules.framework import (
    AllocationContext,
    ConstraintScope,
    Pack,
    PackComponent,
    PackStay,
    Reject,
)

CERT_TAG = "cert:coldchain"
CHILL_THRESHOLD_C = 8  # a max holding temperature at or below this needs the cert
FROZEN_THRESHOLD_C = -10  # at or below: the frozen excursion budget applies
# Default excursion budgets (minutes) when no line carries an explicit
# `coldchain.max_excursion_minutes` attribute.
DEFAULT_BUDGET_MINUTES = {"frozen": 240, "chill": 480}


@dataclass(frozen=True)
class ColdchainCertConstraint:
    id: str = "COLDCHAIN_CERT"
    scope: ConstraintScope = ConstraintScope.FACILITY

    def check(
        self,
        shipment: Shipment,
        facility: Facility,
        zone: StorageZone | None,
        ctx: AllocationContext,
    ) -> Reject | None:
        required = shipment.requirements.temp_c
        if required is None or required[1] > CHILL_THRESHOLD_C:
            return None
        assert ctx.eta is not None
        if not facility.certified_for(CERT_TAG, ctx.eta):
            return Reject(
                constraint_id=self.id,
                data={"tag": CERT_TAG, "max_temp": required[1]},
            )
        return None


def _excursion_budget_minutes(shipment: Shipment) -> int:
    explicit = [
        value
        for line in shipment.lines
        if isinstance(value := line.attributes.get("coldchain.max_excursion_minutes"), int)
        and not isinstance(value, bool)
    ]
    if explicit:
        return max(1, min(explicit))  # the strictest line governs
    required = shipment.requirements.temp_c
    assert required is not None
    band = "frozen" if required[1] <= FROZEN_THRESHOLD_C else "chill"
    return DEFAULT_BUDGET_MINUTES[band]


def _excursion(
    shipment: Shipment,
    facility: Facility,
    zone: StorageZone,
    stay: PackStay,
) -> tuple[float, float]:
    """(raw = transit minutes, normalized = fraction of the excursion budget).

    Exposure counts transport time only — storage is temperature-controlled by
    the zone's own feasibility (§7.2). Can exceed 1.0: an over-budget route is
    still feasible, just expensive, and the overload is visible in the record.
    """
    if shipment.requirements.temp_c is None:
        return (0.0, 0.0)
    exposure = float(stay.route.minutes) if stay.route is not None else 0.0
    return (exposure, exposure / _excursion_budget_minutes(shipment))


def _validate_coldchain_attributes(bag: dict[str, JsonValue]) -> None:
    for key, value in bag.items():
        if key == "coldchain.max_excursion_minutes":
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{key} must be a non-negative integer, got {value!r}")
        elif key == "coldchain.band":
            if value not in ("frozen", "chill"):
                raise ValueError(f"{key} must be 'frozen' or 'chill', got {value!r}")
        else:
            raise ValueError(f"unknown coldchain attribute {key!r}")


PACK = Pack(
    name="coldchain",
    constraints=(ColdchainCertConstraint(),),
    templates={
        "COLDCHAIN_CERT": ("temperature-controlled shipment (max {max_temp}°C) requires {tag}")
    },
    attribute_validators={"coldchain": _validate_coldchain_attributes},
    objective_components=(
        PackComponent(
            name="coldchain.excursion",
            default_weight=0.15,
            compute=_excursion,
        ),
    ),
)
