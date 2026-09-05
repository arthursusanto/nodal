"""Hypothesis property tests: serialization round-trips and capacity algebra."""

from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import InventoryLot
from nodal.events import catalog as ev

dim = st.one_of(st.none(), st.integers(min_value=0, max_value=10**12))
capacity_vectors = st.builds(CapacityVector, slots=dim, volume_l=dim, weight_g=dim)
utc_datetimes = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2035, 1, 1),
    timezones=st.just(UTC),
)
names = st.text(min_size=1, max_size=40)


@given(a=capacity_vectors, b=capacity_vectors)
def test_plus_commutes(a: CapacityVector, b: CapacityVector) -> None:
    assert a.plus(b) == b.plus(a)


@given(demand=capacity_vectors, extra=capacity_vectors, capacity=capacity_vectors)
def test_fits_is_monotone_in_demand(
    demand: CapacityVector, extra: CapacityVector, capacity: CapacityVector
) -> None:
    """Removing demand never breaks a fit (role semantics: capacity None = unbounded)."""
    if demand.plus(extra).fits_within(capacity):
        assert demand.fits_within(capacity)


@given(capacity=capacity_vectors, occupied=capacity_vectors, demand=capacity_vectors)
def test_headroom_fit_implies_capacity_fit(
    capacity: CapacityVector, occupied: CapacityVector, demand: CapacityVector
) -> None:
    """Fitting after some capacity is consumed implies fitting the raw capacity."""
    if demand.fits_within(capacity.minus_demand(occupied)):
        assert demand.fits_within(capacity)


@given(vector=capacity_vectors)
def test_capacity_roundtrip(vector: CapacityVector) -> None:
    assert CapacityVector.model_validate_json(vector.model_dump_json()) == vector


@given(
    lot_id=names,
    sku=names,
    group=names,
    quantity=st.integers(min_value=0, max_value=10**9),
    size=capacity_vectors,
    received=utc_datetimes,
    departure=st.one_of(st.none(), utc_datetimes),
    attributes=st.dictionaries(
        names,
        st.one_of(st.integers(), st.text(max_size=20), st.booleans(), st.none()),
        max_size=4,
    ),
)
def test_lot_received_payload_roundtrip(
    lot_id: str,
    sku: str,
    group: str,
    quantity: int,
    size: CapacityVector,
    received: datetime,
    departure: datetime | None,
    attributes: dict[str, object],
) -> None:
    payload = ev.LotReceived(
        lot=InventoryLot(
            id=lot_id,
            sku=sku,
            commodity_group=group,
            quantity=quantity,
            size=size,
            zone_id="ZON-1",
            received_at=received,
            planned_departure=departure,
            attributes=attributes,  # type: ignore[arg-type]
        )
    )
    restored = ev.LotReceived.model_validate_json(payload.model_dump_json())
    assert restored == payload
