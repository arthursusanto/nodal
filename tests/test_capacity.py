from nodal.domain.capacity import CapacityVector


def test_none_is_unbounded_capacity() -> None:
    unbounded = CapacityVector()
    demand = CapacityVector(slots=10_000, weight_g=10**9)
    assert demand.fits_within(unbounded)


def test_none_is_zero_demand() -> None:
    demand = CapacityVector(slots=5)  # no volume/weight footprint
    headroom = CapacityVector(slots=5, volume_l=0, weight_g=0)
    assert demand.fits_within(headroom)
    assert not CapacityVector(slots=6).fits_within(headroom)


def test_plus_and_minus() -> None:
    a = CapacityVector(slots=3, volume_l=100)
    b = CapacityVector(slots=2, weight_g=500)
    total = a.plus(b)
    assert (total.slots, total.volume_l, total.weight_g) == (5, 100, 500)
    capacity = CapacityVector(slots=10, weight_g=None)
    headroom = capacity.minus_demand(total)
    assert headroom.slots == 5
    assert headroom.weight_g is None  # unbounded stays unbounded


def test_scaled_floors() -> None:
    capacity = CapacityVector(slots=7, volume_l=None)
    half = capacity.scaled(0.5)
    assert half.slots == 3
    assert half.volume_l is None


def test_zero_demand_always_fits() -> None:
    assert CapacityVector().fits_within(CapacityVector(slots=0, volume_l=0, weight_g=0))
