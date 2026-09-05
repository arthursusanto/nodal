"""Multi-dimensional capacity vectors (§3, §5).

A `CapacityVector` bounds or measures any subset of the dimensions
`{slots, volume_l, weight_g}`. Semantics by role:

- As a *capacity* (a zone's bound): `None` in a dimension means unbounded.
- As a *demand* or *occupancy* (a shipment's or lot's footprint): `None` means zero —
  the goods simply have no footprint in that dimension.

`headroom = capacity - occupancy` therefore keeps `None` (unbounded) wherever the
capacity was unbounded, and a fit check only tests dimensions the demand actually uses.
"""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict

Dimension = Literal["slots", "volume_l", "weight_g"]
DIMENSIONS: tuple[Dimension, ...] = ("slots", "volume_l", "weight_g")


class CapacityVector(BaseModel):
    model_config = ConfigDict(frozen=True)

    slots: int | None = None
    volume_l: int | None = None
    weight_g: int | None = None

    def get(self, dim: Dimension) -> int | None:
        value: int | None = getattr(self, dim)
        return value

    def demand(self, dim: Dimension) -> int:
        """This vector read as a demand/occupancy: None counts as 0."""
        value = self.get(dim)
        return 0 if value is None else value

    def is_zero(self) -> bool:
        return all(self.demand(d) == 0 for d in DIMENSIONS)

    def plus(self, other: "CapacityVector") -> "CapacityVector":
        """Demand-wise sum (None = 0; result has explicit ints everywhere)."""
        return CapacityVector(
            slots=self.demand("slots") + other.demand("slots"),
            volume_l=self.demand("volume_l") + other.demand("volume_l"),
            weight_g=self.demand("weight_g") + other.demand("weight_g"),
        )

    def times(self, factor: int) -> "CapacityVector":
        """This vector read as a demand, taken `factor` times over."""
        return CapacityVector(
            slots=self.demand("slots") * factor,
            volume_l=self.demand("volume_l") * factor,
            weight_g=self.demand("weight_g") * factor,
        )

    def minus_demand(self, other: "CapacityVector") -> "CapacityVector":
        """This vector read as a capacity, minus a demand. Unbounded stays unbounded."""
        return CapacityVector(
            slots=None if self.slots is None else self.slots - other.demand("slots"),
            volume_l=None if self.volume_l is None else self.volume_l - other.demand("volume_l"),
            weight_g=None if self.weight_g is None else self.weight_g - other.demand("weight_g"),
        )

    def scaled(self, factor: float) -> "CapacityVector":
        """This vector read as a capacity, scaled (disruption capacity loss).

        Floors — losing a fractional unit is the conservative direction — but with
        an epsilon so IEEE artifacts (0.7 * 0.5 = 0.34999...) don't eat a whole
        unit that is mathematically there.
        """

        def scale(value: int | None) -> int | None:
            return None if value is None else math.floor(value * factor + 1e-9)

        return CapacityVector(
            slots=scale(self.slots), volume_l=scale(self.volume_l), weight_g=scale(self.weight_g)
        )

    def fits_within(self, headroom: "CapacityVector") -> bool:
        """This vector read as a demand: does it fit in the given headroom?"""
        for dim in DIMENSIONS:
            need = self.demand(dim)
            if need == 0:
                continue
            available = headroom.get(dim)
            if available is not None and available < need:
                return False
        return True


ZERO = CapacityVector(slots=0, volume_l=0, weight_g=0)
UNBOUNDED = CapacityVector()
