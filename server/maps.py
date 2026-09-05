"""Google Maps data layer: geocoding, place search, road routing.

The provider key is server-side only (`NODAL_GOOGLE_MAPS_KEY`, or the gitignored
`server/.env.maps`) — it is never handed to a browser. Without a key, or when a
lookup fails, the module degrades gracefully rather than failing: geocoding and
search report nothing, and a road route becomes a great-circle estimate flagged
`estimated`. That flag is the contract — nothing here ever presents an estimate
as routed geometry, and no provider error escapes the module.

Billed answers cache in var/maps-cache.sqlite3, which is infrastructure, not
world truth: it holds no events and is safe to delete at any time.
"""

import json
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from nodal.network.travel import haversine_km

_ENV_FILE = Path(__file__).resolve().parent / ".env.maps"
_CACHE_PATH = Path(__file__).resolve().parents[1] / "var" / "maps-cache.sqlite3"

_TIMEOUT_S = 10.0
_ROUTE_TTL_S = 30 * 86_400  # roads change; names and coordinates do not
_MAX_PATH_POINTS = 120  # a drawn leg, not a survey — 120 points reads as a road
_ROAD_FACTOR = 1.3  # great-circle -> road, matching TravelConfig.circuity
_ROAD_KMH = 68.0  # matching the road lane speed the network uses


def _load_env_file() -> None:
    """Local convenience: fill the key from server/.env.maps (KEY=VALUE lines)
    when the process was started without it in the environment."""
    if os.environ.get("NODAL_GOOGLE_MAPS_KEY"):
        return
    try:
        text = _ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


_load_env_file()


def _key() -> str:
    return os.environ.get("NODAL_GOOGLE_MAPS_KEY", "")


def maps_enabled() -> bool:
    """Whether billed lookups are available (a provider key is configured)."""
    return _key() != ""


# -- cache ---------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache_conn: sqlite3.Connection | None = None


def _conn() -> sqlite3.Connection:
    """The cache connection, opened on first use. Callers hold `_cache_lock`:
    one connection shared across the server's threadpool is enough at this
    scale, and serializing it keeps sqlite3 happy."""
    global _cache_conn
    if _cache_conn is None:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _cache_conn = sqlite3.connect(_CACHE_PATH, check_same_thread=False)
        _cache_conn.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(key TEXT PRIMARY KEY, kind TEXT, payload TEXT, created_at REAL)"
        )
        _cache_conn.commit()
    return _cache_conn


def _cache_get(key: str, ttl_s: float | None) -> Any:
    """The cached payload, or None when absent, expired or unreadable. `ttl_s`
    None never expires. A cache miss is only ever a re-fetch, so every failure
    here is swallowed."""
    with _cache_lock:
        try:
            row = (
                _conn()
                .execute("SELECT payload, created_at FROM cache WHERE key = ?", (key,))
                .fetchone()
            )
        except sqlite3.Error:
            return None
    if row is None:
        return None
    payload, created_at = row
    if ttl_s is not None and time.time() - created_at > ttl_s:
        return None
    try:
        return json.loads(payload)
    except ValueError:
        return None


def _cache_put(key: str, kind: str, payload: Any) -> None:
    with _cache_lock:
        try:
            connection = _conn()
            connection.execute(
                "INSERT OR REPLACE INTO cache (key, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (key, kind, json.dumps(payload), time.time()),
            )
            connection.commit()
        except sqlite3.Error:
            pass


# -- provider calls ------------------------------------------------------------


def _get_json(url: str, params: dict[str, str]) -> Any:
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}")
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
        return json.loads(response.read())


def _post_json(url: str, body: dict[str, Any], field_mask: str) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": _key(),
            "X-Goog-FieldMask": field_mask,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
        return json.loads(response.read())


# -- geocoding -----------------------------------------------------------------


def geocode(text: str) -> dict[str, Any] | None:
    """A place name to coordinates; None when maps are off or nothing matched.

    Places do not move: hits cache forever. Misses are not cached — a name that
    resolves once the provider improves should not stay unresolvable.
    """
    clean = text.strip()
    if not clean:
        return None
    cache_key = f"geocode:{clean.lower()}"
    cached: dict[str, Any] | None = _cache_get(cache_key, None)
    if cached is not None:
        return cached
    if not maps_enabled():
        return None
    try:
        data = _get_json(
            "https://maps.googleapis.com/maps/api/geocode/json",
            {"address": clean, "key": _key()},
        )
        results = data.get("results") or []
        if not results:
            return None
        top = results[0]
        location = top["geometry"]["location"]
        country = next(
            (c for c in top.get("address_components", []) if "country" in c.get("types", [])),
            None,
        )
        hit = {
            "lat": float(location["lat"]),
            "lon": float(location["lng"]),
            "label": top.get("formatted_address") or clean,
            "country": country["long_name"] if country else "",
        }
    except Exception:  # any provider or shape failure is a clean miss
        return None
    _cache_put(cache_key, "geocode", hit)
    return hit


def search_places(text: str, max_results: int = 5) -> list[dict[str, Any]]:
    """Free-text place search. Empty when maps are off or nothing matched."""
    clean = text.strip()
    if not clean:
        return []
    cache_key = f"places:{max_results}:{clean.lower()}"
    cached: list[dict[str, Any]] | None = _cache_get(cache_key, None)
    if cached is not None:
        return cached
    if not maps_enabled():
        return []
    try:
        data = _post_json(
            "https://places.googleapis.com/v1/places:searchText",
            {"textQuery": clean, "maxResultCount": max_results},
            "places.displayName,places.location,places.formattedAddress",
        )
        places = [
            {
                "name": (place.get("displayName") or {}).get("text") or "",
                "address": place.get("formattedAddress") or "",
                "lat": float(place["location"]["latitude"]),
                "lon": float(place["location"]["longitude"]),
            }
            for place in (data.get("places") or [])
            if place.get("location")
        ]
    except Exception:
        return []
    # Same policy as geocode: an empty answer is not cached, so a query that
    # starts matching once the provider improves does not stay empty forever.
    if places:
        _cache_put(cache_key, "places", places)
    return places


# -- road routing --------------------------------------------------------------


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Google's encoded polyline (precision 5) to [(lat, lon), ...]."""
    points: list[tuple[float, float]] = []
    index = 0
    lat = 0
    lon = 0

    def read_delta() -> int:
        nonlocal index
        result = 0
        shift = 0
        while True:
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        return ~(result >> 1) if result & 1 else result >> 1

    while index < len(encoded):
        lat += read_delta()
        lon += read_delta()
        points.append((lat / 1e5, lon / 1e5))
    return points


def _downsample(path: list[list[float]], limit: int = _MAX_PATH_POINTS) -> list[list[float]]:
    """Thin a decoded path to at most `limit` evenly spaced points. The first
    and last indices map exactly onto the endpoints, so a thinned leg still
    starts and ends where the route does."""
    if len(path) <= limit:
        return path
    step = (len(path) - 1) / (limit - 1)
    return [path[round(i * step)] for i in range(limit)]


def _estimated_route(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float
) -> dict[str, Any]:
    km = haversine_km(from_lat, from_lon, to_lat, to_lon) * _ROAD_FACTOR
    return {
        "km": round(km, 1),
        "minutes": round(km / _ROAD_KMH * 60),
        "path": [[from_lon, from_lat], [to_lon, to_lat]],
        "estimated": True,
    }


def _fetch_road_route(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float
) -> dict[str, Any] | None:
    try:
        data = _post_json(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            {
                "origin": {"location": {"latLng": {"latitude": from_lat, "longitude": from_lon}}},
                "destination": {"location": {"latLng": {"latitude": to_lat, "longitude": to_lon}}},
                "travelMode": "DRIVE",
            },
            "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline",
        )
        routes = data.get("routes") or []
        if not routes:
            return None
        route = routes[0]
        encoded = (route.get("polyline") or {}).get("encodedPolyline") or ""
        # [lon, lat] to match the GeoJSON order the map layers already draw.
        path = [[lon, lat] for lat, lon in decode_polyline(encoded)]
        if len(path) < 2:
            return None
        seconds = float(str(route.get("duration") or "0s").rstrip("s"))
        return {
            "km": round(float(route.get("distanceMeters") or 0) / 1000, 1),
            "minutes": round(seconds / 60),
            "path": _downsample(path),
            "estimated": False,
        }
    except Exception:
        return None


def road_route(from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> dict[str, Any]:
    """Driving distance, time and road geometry between two points.

    Always answers. Without a key, or on any provider failure, the answer is a
    great-circle estimate on the straight segment with `estimated` set.
    """
    cache_key = f"route:{from_lat:.5f},{from_lon:.5f}:{to_lat:.5f},{to_lon:.5f}"
    # Read before the fallback: a route cached while a key was configured is
    # real data and beats a live estimate. Estimates are never written back.
    cached: dict[str, Any] | None = _cache_get(cache_key, _ROUTE_TTL_S)
    if cached is not None:
        return cached
    if maps_enabled():
        route = _fetch_road_route(from_lat, from_lon, to_lat, to_lon)
        if route is not None:
            _cache_put(cache_key, "route", route)
            return route
    return _estimated_route(from_lat, from_lon, to_lat, to_lon)
