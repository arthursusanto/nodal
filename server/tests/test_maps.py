"""Maps data layer — decoding, degradation and caching.

Nothing here touches Google: the two provider calls are stubbed, the key is
removed from the environment, and the cache is redirected into tmp_path.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nodal.allocate import ObjectiveConfig
from nodal.network.travel import haversine_km
from server import maps
from server.app import create_app

# Google's own documented example.
KNOWN_POLYLINE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

RESPONSES: dict[str, Any] = {
    GEOCODE_URL: {
        "results": [
            {
                "geometry": {"location": {"lat": 51.9225, "lng": 4.47917}},
                "formatted_address": "Rotterdam, Netherlands",
                "address_components": [
                    {
                        "long_name": "Netherlands",
                        "short_name": "NL",
                        "types": ["country", "political"],
                    }
                ],
            }
        ]
    },
    PLACES_URL: {
        "places": [
            {
                "displayName": {"text": "Port of Rotterdam"},
                "formattedAddress": "Wilhelminakade 909, Rotterdam",
                "location": {"latitude": 51.95, "longitude": 4.14},
            },
            {"displayName": {"text": "No coordinates"}},  # dropped: unplaceable
        ]
    },
    ROUTES_URL: {
        "routes": [
            {
                "distanceMeters": 42000,
                "duration": "3600s",
                "polyline": {"encodedPolyline": KNOWN_POLYLINE},
            }
        ]
    },
}


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test sees the real key or the real cache file."""
    monkeypatch.delenv("NODAL_GOOGLE_MAPS_KEY", raising=False)
    monkeypatch.setattr(maps, "_CACHE_PATH", tmp_path / "maps-cache.sqlite3")
    monkeypatch.setattr(maps, "_cache_conn", None)
    yield
    with maps._cache_lock:
        if maps._cache_conn is not None:
            maps._cache_conn.close()


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A configured key with both HTTP calls stubbed; the returned list records
    every provider call, so a test can prove the cache spared one."""
    calls: list[str] = []

    def get(url: str, params: dict[str, str]) -> Any:
        calls.append(url)
        return RESPONSES[url]

    def post(url: str, body: dict[str, Any], field_mask: str) -> Any:
        calls.append(url)
        return RESPONSES[url]

    monkeypatch.setenv("NODAL_GOOGLE_MAPS_KEY", "test-key")
    monkeypatch.setattr(maps, "_get_json", get)
    monkeypatch.setattr(maps, "_post_json", post)
    return calls


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    # The geography routes never read the log, so an empty world is enough.
    app = create_app(tmp_path / "world.sqlite3", ObjectiveConfig())
    with TestClient(app) as test_client:
        yield test_client


# -- polyline ------------------------------------------------------------------


def test_decode_polyline() -> None:
    assert maps.decode_polyline(KNOWN_POLYLINE) == pytest.approx(
        [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    )
    assert maps.decode_polyline("") == []


def test_downsample_keeps_the_endpoints() -> None:
    path = [[float(i), 0.0] for i in range(1000)]
    thin = maps._downsample(path)
    assert len(thin) == 120
    assert thin[0] == path[0]
    assert thin[-1] == path[-1]
    assert thin == sorted(thin)  # order preserved, no duplicates
    assert maps._downsample(path[:10]) == path[:10]  # short paths pass through


# -- degradation ---------------------------------------------------------------


def test_lookups_report_nothing_without_a_key() -> None:
    assert maps.maps_enabled() is False
    assert maps.geocode("Rotterdam") is None
    assert maps.search_places("Rotterdam") == []


def test_road_route_without_a_key_is_a_flagged_estimate() -> None:
    route = maps.road_route(51.95, 4.14, 52.37, 4.90)
    km = haversine_km(51.95, 4.14, 52.37, 4.90) * 1.3
    assert route["estimated"] is True
    assert route["km"] == pytest.approx(round(km, 1))
    assert route["minutes"] == round(km / 68 * 60)
    # The straight segment, in [lon, lat] as the map layers draw it.
    assert route["path"] == [[4.14, 51.95], [4.90, 52.37]]


def test_a_failing_provider_degrades_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("provider down")

    monkeypatch.setenv("NODAL_GOOGLE_MAPS_KEY", "test-key")
    monkeypatch.setattr(maps, "_get_json", boom)
    monkeypatch.setattr(maps, "_post_json", boom)
    assert maps.road_route(1.0, 2.0, 3.0, 4.0)["estimated"] is True
    assert maps.geocode("Rotterdam") is None
    assert maps.search_places("Rotterdam") == []


# -- provider answers ----------------------------------------------------------


def test_geocode_reads_the_provider_shape(provider: list[str]) -> None:
    hit = maps.geocode("Rotterdam")
    assert hit == {
        "lat": 51.9225,
        "lon": 4.47917,
        "label": "Rotterdam, Netherlands",
        "country": "Netherlands",
    }
    # Whitespace and case fold into the same cache key.
    assert maps.geocode("  ROTTERDAM ") == hit
    assert provider == [GEOCODE_URL]


def test_search_places_reads_the_provider_shape(provider: list[str]) -> None:
    hits = maps.search_places("Port of Rotterdam")
    assert hits == [
        {
            "name": "Port of Rotterdam",
            "address": "Wilhelminakade 909, Rotterdam",
            "lat": 51.95,
            "lon": 4.14,
        }
    ]
    maps.search_places("Port of Rotterdam")
    assert provider == [PLACES_URL]


def test_an_empty_search_is_not_cached(
    provider: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same policy as geocode misses: a query that starts matching once the
    provider improves must not stay pinned empty forever."""
    monkeypatch.setitem(RESPONSES, PLACES_URL, {"places": []})
    assert maps.search_places("nowhere-yet") == []
    monkeypatch.setitem(
        RESPONSES,
        PLACES_URL,
        {
            "places": [
                {
                    "displayName": {"text": "Nowhere Depot"},
                    "formattedAddress": "1 Nowhere Rd",
                    "location": {"latitude": 1.0, "longitude": 2.0},
                }
            ]
        },
    )
    assert maps.search_places("nowhere-yet") != []
    assert provider == [PLACES_URL, PLACES_URL]  # both calls really hit the provider


def test_road_route_uses_the_provider_and_caches_it(provider: list[str]) -> None:
    route = maps.road_route(38.5, -120.2, 43.252, -126.453)
    assert route["estimated"] is False
    assert route["km"] == 42.0
    assert route["minutes"] == 60
    assert route["path"][0] == pytest.approx([-120.2, 38.5])
    assert len(route["path"]) == 3
    assert maps.road_route(38.5, -120.2, 43.252, -126.453) == route
    assert provider == [ROUTES_URL]


def test_a_cached_route_beats_a_live_estimate(
    provider: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    route = maps.road_route(38.5, -120.2, 43.252, -126.453)
    monkeypatch.delenv("NODAL_GOOGLE_MAPS_KEY")
    assert maps.maps_enabled() is False
    assert maps.road_route(38.5, -120.2, 43.252, -126.453) == route
    assert provider == [ROUTES_URL]


def test_estimates_are_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    assert maps.road_route(38.5, -120.2, 43.252, -126.453)["estimated"] is True
    assert maps._cache_get("route:38.50000,-120.20000:43.25200,-126.45300", None) is None
    # A key arriving later still gets a real lookup, not the stale estimate.
    calls: list[str] = []

    def post(url: str, body: dict[str, Any], field_mask: str) -> Any:
        calls.append(url)
        return RESPONSES[url]

    monkeypatch.setenv("NODAL_GOOGLE_MAPS_KEY", "test-key")
    monkeypatch.setattr(maps, "_post_json", post)
    assert maps.road_route(38.5, -120.2, 43.252, -126.453)["estimated"] is False
    assert calls == [ROUTES_URL]


# -- endpoints -----------------------------------------------------------------


def test_place_search_endpoint_says_when_it_is_disabled(client: TestClient) -> None:
    body = client.get("/api/places/search", params={"q": "Rotterdam"}).json()
    assert body == {"enabled": False, "places": []}
    assert client.get("/api/places/search").status_code == 422


def test_place_search_endpoint_with_a_provider(client: TestClient, provider: list[str]) -> None:
    body = client.get("/api/places/search", params={"q": "Port of Rotterdam"}).json()
    assert body["enabled"] is True
    assert [p["name"] for p in body["places"]] == ["Port of Rotterdam"]


def test_road_route_endpoint(client: TestClient) -> None:
    params = {"from_lat": 51.95, "from_lon": 4.14, "to_lat": 52.37, "to_lon": 4.90}
    body = client.get("/api/routes/road", params=params).json()
    assert body["estimated"] is True
    assert body["path"] == [[4.14, 51.95], [4.90, 52.37]]
    assert body["km"] > 0

    assert client.get("/api/routes/road", params={**params, "from_lat": 999}).status_code == 422
    assert client.get("/api/routes/road", params={**params, "to_lon": -181}).status_code == 422
