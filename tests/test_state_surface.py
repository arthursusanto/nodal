"""Stage 1 acceptance: the public state API exports no mutators.

The only sanctioned mutation path is `fold.apply_event` (which uses the
underscore-private index maintainers, in-package). This test pins the public
method surface so a mutator can't sneak in unreviewed.
"""

from nodal.events.state import NetworkState

QUERY_SURFACE = {
    "is_known_shipment_id",
    "zones_of",
    "lanes_from",
    "lots_in_zone",
    "reservations_on_zone",
    "reservations_of",
    "active_reservations_of",
    "disruptions_on",
    "departure_closure",
    "effective_capacity",
    "occupancy",
    "headroom",
    "fits",
    "zone_utilization",
    "facility_peak_utilization",
    "stock",
    "demand_rate",
    "active_disruptions",
    "facility_open",
    "facility_next_open",
}

MUTATOR_PREFIXES = ("index", "unindex", "reindex", "rebuild", "set_", "add_", "remove_", "clear")


def test_public_surface_is_queries_only() -> None:
    public_methods = {
        name
        for name, value in vars(NetworkState).items()
        if callable(value) and not name.startswith("_") and name != "model_post_init"
    }
    assert public_methods == QUERY_SURFACE


def test_no_public_mutator_naming() -> None:
    offenders = [
        name
        for name in vars(NetworkState)
        if not name.startswith("_") and name.startswith(MUTATOR_PREFIXES)
    ]
    assert offenders == []
