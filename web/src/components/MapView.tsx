/* Fullscreen real-world map: MapLibre GL over OpenStreetMap raster tiles,
   restyled dark to the Foundry palette. Facilities and shipment origins are
   DOM markers (the design's node grammar, CSS-styled); lanes, booked routes,
   plan routes and decision arcs are great-circle GeoJSON lines; zones rise as
   data-true 3D extrusions (height = capacity, core = occupancy) when the view
   is pitched. No animations beyond MapLibre's user-driven pan/zoom (jumpTo
   only, no easing). */

import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import mapWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import type {
  BatchPlan,
  DecisionRecord,
  FacilitySummary,
  LaneSummary,
  MapState,
  RoadRoute,
} from "../api";
import { panelGeometry } from "../layout";

// MapLibre resolves its worker from import.meta.url with a computed name that
// neither Vite's dev pre-bundler nor Rollup can follow (404 → no GeoJSON ever
// renders). Hand it a worker Vite bundles itself, dependencies included.
maplibregl.setWorkerUrl(mapWorkerUrl);

type LonLat = [number, number];

/* Great-circle interpolation with antimeridian unwrapping: MapLibre renders
   continuous lines when longitudes run past ±180, so we unwrap rather than
   split. */
function greatCircle(from: LonLat, to: LonLat, steps = 64): LonLat[] {
  const toRad = (d: number) => (d * Math.PI) / 180;
  const toDeg = (r: number) => (r * 180) / Math.PI;
  const [lon1, lat1] = [toRad(from[0]), toRad(from[1])];
  const lon2 = toRad(to[0]);
  const lat2 = toRad(to[1]);
  const d =
    2 *
    Math.asin(
      Math.sqrt(
        Math.sin((lat2 - lat1) / 2) ** 2 +
          Math.cos(lat1) * Math.cos(lat2) * Math.sin((lon2 - lon1) / 2) ** 2,
      ),
    );
  if (d === 0 || !Number.isFinite(d)) return [from, to];
  const points: LonLat[] = [];
  for (let i = 0; i <= steps; i++) {
    const f = i / steps;
    const a = Math.sin((1 - f) * d) / Math.sin(d);
    const b = Math.sin(f * d) / Math.sin(d);
    const x = a * Math.cos(lat1) * Math.cos(lon1) + b * Math.cos(lat2) * Math.cos(lon2);
    const y = a * Math.cos(lat1) * Math.sin(lon1) + b * Math.cos(lat2) * Math.sin(lon2);
    const z = a * Math.sin(lat1) + b * Math.sin(lat2);
    const lat = Math.atan2(z, Math.sqrt(x * x + y * y));
    let lon = Math.atan2(y, x);
    // Unwrap so the line never jumps across the antimeridian.
    if (points.length > 0) {
      const prev = toRad(points[points.length - 1]![0]);
      while (lon - prev > Math.PI) lon -= 2 * Math.PI;
      while (prev - lon > Math.PI) lon += 2 * Math.PI;
    }
    points.push([toDeg(lon), toDeg(lat)]);
  }
  return points;
}

/* Declared geometry (road lanes, fetched road legs) arrives as plain (lon, lat)
   points, which may cross the antimeridian: unwrap the longitudes the way
   greatCircle does so MapLibre draws one continuous line rather than a streak
   across the world. */
function unwrap(points: readonly LonLat[]): LonLat[] {
  const out: LonLat[] = [];
  for (const [lon, lat] of points) {
    let x = lon;
    const previous = out[out.length - 1];
    if (previous) {
      while (x - previous[0] > 180) x -= 360;
      while (previous[0] - x > 180) x += 360;
    }
    out.push([x, lat]);
  }
  return out;
}

/** Two coordinates the same to within a metre or so. */
function samePoint(a: LonLat, b: LonLat): boolean {
  return Math.abs(a[0] - b[0]) < 1e-5 && Math.abs(a[1] - b[1]) < 1e-5;
}

/* Extrusion height in meters from a slot count: sqrt-scaled so a 380-slot
   mega-hub reads as a tall block, not a 4 km spike; monotone, so bigger is
   always taller (the data stays true, the skyline stays plausible). */
function slotsToMeters(slots: number): number {
  return Math.round(40 * Math.sqrt(Math.max(slots, 0)));
}

/* A small square polygon (~side meters) around a point — the zone extrusion
   footprint. Zone geometry is not modeled (§11); placement around the
   facility is presentational, heights are real capacity/occupancy. */
function squareAround(center: LonLat, side: number, index: number, count: number): LonLat[] {
  const metersPerDegLat = 111_320;
  const metersPerDegLon = Math.max(1, Math.cos((center[1] * Math.PI) / 180) * 111_320);
  const ring = 900; // meters from the facility point
  const angle = (2 * Math.PI * index) / Math.max(count, 1) + Math.PI / 6;
  const cx = center[0] + (Math.cos(angle) * ring) / metersPerDegLon;
  const cy = center[1] + (Math.sin(angle) * ring) / metersPerDegLat;
  const dx = side / 2 / metersPerDegLon;
  const dy = side / 2 / metersPerDegLat;
  return [
    [cx - dx, cy - dy],
    [cx + dx, cy - dy],
    [cx + dx, cy + dy],
    [cx - dx, cy + dy],
    [cx - dx, cy - dy],
  ];
}

/* OSM's public tile service is fine for running the demo locally and is NOT a
   hosted default: it is a community service, not a CDN for third-party
   deployments. Anything deployed for real points `tiles` at its own provider
   (and its own key). */
const DARK_OSM_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      maxzoom: 19,
      attribution: "© OpenStreetMap contributors",
    },
  },
  layers: [
    { id: "bg", type: "background", paint: { "background-color": "#0f0c08" } },
    {
      id: "osm",
      type: "raster",
      source: "osm",
      paint: {
        // Foundry-dark treatment of the real map: desaturated, dimmed, warm.
        "raster-saturation": -1,
        "raster-brightness-min": 0,
        "raster-brightness-max": 0.32,
        "raster-contrast": 0.15,
        "raster-opacity": 0.9,
      },
    },
  ],
};

const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

function lineFeature(
  points: LonLat[],
  properties: Record<string, unknown>,
): GeoJSON.Feature {
  return {
    type: "Feature",
    properties,
    geometry: { type: "LineString", coordinates: points },
  };
}

/* Line grammar, in draw order (later = on top):
   lanes     — the network itself: faint hairlines, mode-dashed
   booked    — committed routes: sodium orange
   planned   — an uncommitted batch plan: dashed amber
   alt       — the top alternatives of the selected decision: dashed soft
   road      — first/last mile of the selected delivery: thin, cooler amber;
               dashed when the leg is a great-circle estimate, not real geometry
   selected  — the selected shipment's route: bright, with a halo underneath */
const LINE_LAYERS: maplibregl.LayerSpecification[] = [
  {
    id: "lanes-road",
    type: "line",
    source: "lanes",
    filter: ["==", ["get", "mode"], "road"],
    paint: { "line-color": "#8a7a60", "line-width": 1, "line-opacity": 0.35 },
  },
  {
    id: "lanes-sea",
    type: "line",
    source: "lanes",
    filter: ["==", ["get", "mode"], "sea"],
    paint: {
      "line-color": "#7f93a3",
      "line-width": 1,
      "line-opacity": 0.35,
      "line-dasharray": [4, 4],
    },
  },
  {
    id: "lanes-air",
    type: "line",
    source: "lanes",
    filter: ["==", ["get", "mode"], "air"],
    paint: {
      "line-color": "#a08a62",
      "line-width": 1,
      "line-opacity": 0.3,
      "line-dasharray": [1, 4],
    },
  },
  {
    id: "arcs-booked",
    type: "line",
    source: "arcs",
    filter: ["==", ["get", "grade"], "booked"],
    paint: { "line-color": "#ff7a1a", "line-width": 1.8, "line-opacity": 0.75 },
  },
  {
    id: "arcs-planned",
    type: "line",
    source: "arcs",
    filter: ["==", ["get", "grade"], "planned"],
    paint: {
      "line-color": "#ffb000",
      "line-width": 2,
      "line-opacity": 0.9,
      "line-dasharray": [3, 2],
    },
  },
  {
    id: "arcs-alt",
    type: "line",
    source: "arcs",
    filter: ["==", ["get", "grade"], "alt"],
    paint: {
      "line-color": "#ffc46b",
      "line-width": 1.4,
      "line-opacity": 0.55,
      "line-dasharray": [5, 4],
    },
  },
  {
    id: "arcs-road",
    type: "line",
    source: "arcs",
    filter: ["all", ["==", ["get", "grade"], "road"], ["!=", ["get", "estimated"], true]],
    paint: { "line-color": "#e0a35c", "line-width": 1.4, "line-opacity": 0.95 },
  },
  {
    id: "arcs-road-estimated",
    type: "line",
    source: "arcs",
    filter: ["all", ["==", ["get", "grade"], "road"], ["==", ["get", "estimated"], true]],
    paint: {
      "line-color": "#e0a35c",
      "line-width": 1.4,
      "line-opacity": 0.8,
      "line-dasharray": [2, 3],
    },
  },
  {
    id: "arcs-selected-halo",
    type: "line",
    source: "arcs",
    filter: ["==", ["get", "grade"], "selected"],
    paint: { "line-color": "#ff7a1a", "line-width": 9, "line-opacity": 0.22 },
  },
  {
    id: "arcs-selected",
    type: "line",
    source: "arcs",
    filter: ["==", ["get", "grade"], "selected"],
    paint: { "line-color": "#ffd28a", "line-width": 2.8 },
  },
];

export interface OverlayInsets {
  left: boolean;
  right: boolean;
  /** A bar along the bottom edge (the replay scrubber). */
  bottom?: boolean;
}

interface Props {
  world: MapState | null;
  plan: BatchPlan | null;
  selectedShipment: string | null;
  record: DecisionRecord | null;
  /** Which side panels this view reserves space for — the fit keeps hubs clear of them. */
  overlays: OverlayInsets | null;
  /** The decision panel is actually open right now: the control stack steps left of it. */
  rightPanelOpen: boolean;
  /** The interface scale; a change re-fits the map to the resized panels. */
  uiScale: number;
  /** Armed coordinate pick (network panel): the next map click reports back. */
  picking: boolean;
  onSelectShipment: (shipmentId: string | null) => void;
  onOpenFacility: (facilityId: string) => void;
  onPickCoords: (coords: { lat: number; lon: number }) => void;
}

/** A renderable position: an unhinged coordinate in the log (bad import,
    old data) must degrade to "not drawn", never crash the whole map. */
function onEarth(lon: number | null, lat: number | null): boolean {
  return (
    lon !== null &&
    lat !== null &&
    Number.isFinite(lon) &&
    Number.isFinite(lat) &&
    Math.abs(lat) <= 90 &&
    Math.abs(lon) <= 180
  );
}

/* A bounds over points that may straddle the antimeridian. LngLatBounds keeps
   longitudes exactly as handed to it, so extending it with 153° and then
   −179.6° frames 333° the long way round the planet instead of the 27° between
   the two points (a doubled world at zoom 1). Choose the longitude window a
   human would instead: the single interval covering every point that omits the
   widest empty gap between neighbouring longitudes, with each point shifted
   onto it. Order-independent, and identical to plain min/max whenever the
   widest gap is the one across the date line — that is, for every network that
   doesn't straddle it. */
function boundsOver(points: readonly LonLat[]): maplibregl.LngLatBounds {
  const bounds = new maplibregl.LngLatBounds();
  if (points.length === 0) return bounds;
  const sorted = points.map(([lon]) => lon).sort((a, b) => a - b);
  let west = sorted[0]!;
  // The gap that crosses ±180 holds the tie, so a non-wrapping set is untouched.
  let widest = sorted[0]! + 360 - sorted[sorted.length - 1]!;
  for (let i = 1; i < sorted.length; i++) {
    const gap = sorted[i]! - sorted[i - 1]!;
    if (gap > widest) {
      widest = gap;
      west = sorted[i]!;
    }
  }
  for (const [lon, lat] of points) bounds.extend([lon < west ? lon + 360 : lon, lat]);
  return bounds;
}

/** Everything the network occupies: facilities, coordinate origins, and the
    customer points deliveries end at — every dot the map draws is inside it. */
function networkBounds(world: MapState): maplibregl.LngLatBounds {
  const points: LonLat[] = [];
  for (const facility of world.facilities) {
    if (onEarth(facility.lon, facility.lat)) points.push([facility.lon, facility.lat]);
  }
  for (const shipment of world.shipments) {
    if (onEarth(shipment.origin_lon, shipment.origin_lat)) {
      points.push([shipment.origin_lon!, shipment.origin_lat!]);
    }
    const customer = shipment.destination_point;
    if (customer && onEarth(customer.lon, customer.lat)) {
      points.push([customer.lon, customer.lat]);
    }
  }
  return boundsOver(points);
}

// The fit paddings come from the same geometry the stylesheet is given
// (layout.ts), so the map never fits under a panel of a different width.
function fitPadding(insets: OverlayInsets | null, scale: number): maplibregl.PaddingOptions {
  const { unit, left, right, gutter } = panelGeometry(scale, window.innerWidth);
  // The extra rem on the sides keeps a node's label (wider than its square)
  // clear of the panels too.
  return {
    top: 3 * unit,
    bottom: (insets?.bottom ? 6 : 5) * unit,
    left: (insets?.left ? left + 2 * gutter : 3.5 * unit) + 1.5 * unit,
    right: (insets?.right ? right + 2 * gutter : 6 * unit) + 1.5 * unit,
  };
}

export function MapView({
  world,
  plan,
  selectedShipment,
  record,
  overlays,
  rightPanelOpen,
  uiScale,
  picking,
  onSelectShipment,
  onOpenFacility,
  onPickCoords,
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markersRef = useRef<maplibregl.Marker[]>([]);
  const fittedRef = useRef(false);
  // The camera before a 3D dive (and where the dive landed), so 2D can return
  // the operator to their view — but only if they haven't moved on since.
  const preDiveRef = useRef<{
    center: maplibregl.LngLat;
    zoom: number;
    landed: { center: LonLat; zoom: number };
  } | null>(null);
  const [ready, setReady] = useState(false);
  const [pitched, setPitched] = useState(false);
  // Mirrors whether focus bounds exist, for render-time labels (FIT).
  const [focused, setFocused] = useState(false);
  // The operator moved the camera themselves since our last fit: a window
  // resize must not throw their view away. Every camera move that is not
  // ours (see `asProgram`) counts — drag, wheel, keyboard, nav buttons,
  // double-click — without enumerating gestures.
  const userMovedRef = useRef(false);
  const progRef = useRef(false);
  const resizingRef = useRef(false);
  /* Wherever the viewport is wider than one world copy — the network fit lands
     at zoom ~1.4 on a 1920×1080 screen, some 516° of longitude — MapLibre draws
     the world more than once, and a DOM marker can only live on ONE copy. Which
     copy is decided by MapLibre's smartWrap() from the marker's LAST SCREEN
     POSITION, not from its longitude, so a marker never jumps under the
     operator's hand mid-drag. That is right for a drag and wrong for a jump of
     ours: after a pan west, FIT restores the same centre and zoom numerically
     while the Pacific hubs stay on the western copy, a few dozen pixels in,
     behind the queue panel — the trapped hub among them.
     So every camera move WE make re-homes each marker onto the copy nearest the
     new centre, which is inside the fitted window by construction and is where
     any ordinary pan already leaves it: a no-op except when it fixes this.
     setLngLat() cannot do it alone — it clears the marker's `_pos` but not the
     `_flatPos` smartWrap reads (maplibre-gl 6.5.0, src/ui/marker.ts), so the
     wrap drags the marker straight back onto the stale copy — hence clearing
     that anchor first. The operator's own pans never come through here, so
     smart wrapping still owns the drag. */
  const rehomeMarkers = () => {
    const map = mapRef.current;
    if (!map) return;
    const centre = map.getCenter().lng;
    for (const marker of markersRef.current) {
      const { lng, lat } = marker.getLngLat();
      (marker as unknown as { _flatPos: null })._flatPos = null;
      marker.setLngLat([lng + 360 * Math.round((centre - lng) / 360), lat]);
    }
  };
  const asProgram = (move: () => void) => {
    progRef.current = true;
    try {
      move();
      rehomeMarkers();
    } finally {
      progRef.current = false;
    }
  };
  // Focus mode: selecting a shipment shows only its nodes and fits them; the
  // camera from before the first focus comes back when the selection closes.
  const focusRef = useRef<{
    fittedFor: string | null;
    before: { center: maplibregl.LngLat; zoom: number; pitch: number; bearing: number } | null;
    bounds: maplibregl.LngLatBounds | null;
  }>({ fittedFor: null, before: null, bounds: null });

  // Road legs (the first and last mile of a delivery) are fetched only for the
  // SELECTED shipment, and kept for the session: re-selecting redraws from the
  // cache instead of asking the provider again. A key present with no route is
  // in flight (or failed) and draws the dashed straight fallback until the
  // answer lands and bumps `roadVersion`, which re-runs the draw.
  const roadRef = useRef(new Map<string, RoadRoute | null>());
  const [roadVersion, setRoadVersion] = useState(0);
  const roadLeg = useCallback((from: LonLat, to: LonLat): RoadRoute | null => {
    const key = `${from[0].toFixed(4)},${from[1].toFixed(4)}>${to[0].toFixed(4)},${to[1].toFixed(4)}`;
    const cache = roadRef.current;
    if (cache.has(key)) return cache.get(key) ?? null;
    cache.set(key, null);
    api
      .roadRoute(from[1], from[0], to[1], to[0])
      .then((route) => {
        cache.set(key, route);
        setRoadVersion((version) => version + 1);
      })
      .catch(() => {
        // Keep the straight fallback: the endpoint itself already degrades to
        // an estimate, so a failure here is the network, not the geometry.
      });
    return null;
  }, []);

  const handlers = useRef({ onSelectShipment, onOpenFacility, onPickCoords });
  handlers.current = { onSelectShipment, onOpenFacility, onPickCoords };
  const pickingRef = useRef(picking);
  pickingRef.current = picking;
  const overlaysRef = useRef(overlays);
  overlaysRef.current = overlays;
  const scaleRef = useRef(uiScale);
  scaleRef.current = uiScale;
  const padding = () => fitPadding(overlaysRef.current, scaleRef.current);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: DARK_OSM_STYLE,
      center: [10, 25],
      zoom: 1.6,
      attributionControl: false,
    });
    // Attribution bottom-left, force-expanded (OSM requires it visible);
    // navigation bottom-right, under the 2D/3D toggle.
    map.addControl(new maplibregl.AttributionControl({ compact: false }), "bottom-left");
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");
    // Any camera move we didn't make ourselves marks the camera as the
    // operator's (see the resize re-fit). Moves caused by the canvas
    // resizing don't count either.
    map.on("moveend", () => {
      if (!progRef.current && !resizingRef.current) userMovedRef.current = true;
    });
    // An armed coordinate pick (network panel) takes the next map click.
    // Markers lose pointer events while armed, so every click lands here.
    map.on("click", (event) => {
      if (!pickingRef.current) return;
      handlers.current.onPickCoords({ lat: event.lngLat.lat, lon: event.lngLat.lng });
    });
    map.on("load", () => {
      map.addSource("lanes", { type: "geojson", data: EMPTY });
      map.addSource("arcs", { type: "geojson", data: EMPTY });
      map.addSource("zones", { type: "geojson", data: EMPTY });
      for (const layer of LINE_LAYERS) map.addLayer(layer);
      // Data-true 3D (§11, a deliberate design call): shells are effective capacity,
      // cores are today's occupancy. Visible when the camera pitches.
      map.addLayer({
        id: "zone-capacity",
        type: "fill-extrusion",
        source: "zones",
        minzoom: 4,
        filter: ["==", ["get", "kind"], "shell"],
        paint: {
          "fill-extrusion-color": "#6a5438",
          "fill-extrusion-opacity": 0.5,
          "fill-extrusion-height": ["get", "height_m"],
        },
      });
      map.addLayer({
        id: "zone-occupancy",
        type: "fill-extrusion",
        source: "zones",
        minzoom: 4,
        filter: ["==", ["get", "kind"], "core"],
        paint: {
          "fill-extrusion-color": [
            "case",
            [">", ["get", "utilization"], 0.85],
            "#ff6b3d",
            [">", ["get", "utilization"], 0.6],
            "#ffc46b",
            "#ff7a1a",
          ],
          "fill-extrusion-opacity": 0.9,
          "fill-extrusion-height": ["get", "height_m"],
        },
      });
      setReady(true);
    });
    mapRef.current = map;
    if (import.meta.env.DEV) {
      // Dev-only handle for browser-driven checks (never in production builds).
      (window as unknown as { __nodalMap?: maplibregl.Map }).__nodalMap = map;
    }
    return () => {
      map.remove();
      mapRef.current = null;
      setReady(false);
    };
  }, []);

  const facilityById = useMemo(() => {
    const index = new Map<string, FacilitySummary>();
    for (const facility of world?.facilities ?? []) index.set(facility.id, facility);
    return index;
  }, [world]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    if (!world || world.empty) {
      // A replay instant before the world existed (or a genuinely empty log):
      // the map must clear, never keep showing the live network (§11 replay
      // consistency).
      for (const source of ["lanes", "arcs", "zones"] as const) {
        (map.getSource(source) as maplibregl.GeoJSONSource).setData(EMPTY);
      }
      for (const marker of markersRef.current) marker.remove();
      markersRef.current = [];
      return;
    }

    const originOf = (shipmentId: string): LonLat | null => {
      const shipment = world.shipments.find((s) => s.id === shipmentId);
      if (!shipment) return null;
      if (shipment.origin_facility_id) {
        const facility = facilityById.get(shipment.origin_facility_id);
        return facility ? [facility.lon, facility.lat] : null;
      }
      if (onEarth(shipment.origin_lon, shipment.origin_lat)) {
        return [shipment.origin_lon!, shipment.origin_lat!];
      }
      return null;
    };

    // -- lanes ---------------------------------------------------------------
    // A lane that declares geometry (road lanes routed by the maps provider)
    // is drawn along it; anything else is the great circle between its ends.
    const laneGeometry = (lane: LaneSummary): LonLat[] | null => {
      if (lane.path && lane.path.length >= 2) return unwrap(lane.path);
      const from = facilityById.get(lane.from);
      const to = facilityById.get(lane.to);
      if (!from || !to) return null;
      return greatCircle([from.lon, from.lat], [to.lon, to.lat]);
    };
    const laneFeatures: GeoJSON.Feature[] = [];
    for (const lane of world.lanes) {
      const geometry = laneGeometry(lane);
      if (!geometry) continue;
      laneFeatures.push(lineFeature(geometry, { mode: lane.mode }));
    }
    (map.getSource("lanes") as maplibregl.GeoJSONSource).setData({
      type: "FeatureCollection",
      features: laneFeatures,
    });

    // -- routes: booked, planned, and the selected decision ------------------
    const laneById = new Map(world.lanes.map((lane) => [lane.id, lane]));
    /* Join consecutive runs of points into one line, keeping longitude
       continuous across every join (each run unwraps only from its own start)
       and dropping a joint point that both runs share. */
    const stitch = (runs: LonLat[][]): LonLat[] | null => {
      const path: LonLat[] = [];
      for (const run of runs) {
        if (run.length === 0) continue;
        let shift = 0;
        const previous = path[path.length - 1];
        if (previous) {
          while (run[0]![0] + shift - previous[0] > 180) shift -= 360;
          while (previous[0] - (run[0]![0] + shift) > 180) shift += 360;
        }
        const moved =
          shift === 0 ? run : run.map(([lon, lat]) => [lon + shift, lat] as LonLat);
        path.push(...(previous && samePoint(previous, moved[0]!) ? moved.slice(1) : moved));
      }
      return path.length >= 2 ? path : null;
    };
    // A route follows its lanes' own geometry — real road waypoints where the
    // lane has them — never an as-the-crow-flies shortcut; gaps between the
    // origin, the lanes and the destination close with great circles. Origin ==
    // destination with no lanes means the shipment stores where it sits, so
    // nothing is drawn.
    const tracePath = (
      origin: LonLat,
      laneIds: readonly string[],
      dest: LonLat,
    ): LonLat[] | null => {
      const runs: LonLat[][] = [];
      let cursor = origin;
      for (const laneId of laneIds) {
        const lane = laneById.get(laneId);
        const geometry = lane ? laneGeometry(lane) : null;
        if (!geometry) continue;
        const start = geometry[0]!;
        if (!samePoint(cursor, start)) runs.push(greatCircle(cursor, start));
        runs.push(geometry);
        cursor = geometry[geometry.length - 1]!;
      }
      if (!samePoint(cursor, dest)) runs.push(greatCircle(cursor, dest));
      return stitch(runs);
    };
    const recordLaneIds = (rec: DecisionRecord): string[] =>
      rec.chosen
        ? rec.chosen.route.legs
            .map((leg) => leg.lane_id)
            .filter((id): id is string => id !== null)
        : [];
    const laneEnds = (laneIds: readonly string[]): string[] =>
      laneIds.flatMap((id) => {
        const lane = laneById.get(id);
        return lane ? [lane.from, lane.to] : [];
      });

    // -- focus: a selected shipment shows ONLY what concerns it --------------
    // Its origin, its booked/planned/chosen destination, the hubs its route
    // passes through, and the alternatives the decision weighed. Everything
    // else leaves the map until the selection closes. A planned shipment with
    // nothing decided yet has nothing to focus on: the whole network stays,
    // with its origin highlighted, until SOLVE gives it a destination.
    const selected = selectedShipment
      ? (world.shipments.find((s) => s.id === selectedShipment) ?? null)
      : null;
    const selectedPlanRecord = selected ? plan?.records[selected.id] : undefined;
    const focusDestination =
      selected?.destination?.facility_id ??
      record?.chosen?.facility_id ??
      selectedPlanRecord?.chosen?.facility_id ??
      null;
    let focus: Set<string> | null = null;
    if (selected && focusDestination) {
      focus = new Set<string>();
      if (selected.origin_facility_id) focus.add(selected.origin_facility_id);
      if (selected.destination) {
        focus.add(selected.destination.facility_id);
        for (const id of laneEnds(selected.destination.route)) focus.add(id);
        // A delivery continues past the hold: the outbound half and the
        // facility its last mile departs from belong to the story too.
        for (const id of laneEnds(selected.destination.outbound_route)) focus.add(id);
        if (selected.destination.exit_facility_id) {
          focus.add(selected.destination.exit_facility_id);
        }
        // Every facility the journey actually dwells at, from the booking's own
        // stops — the authority on where the goods stand, rather than a second
        // derivation from the lanes.
        for (const stop of selected.destination.stops) focus.add(stop.facility_id);
      }
      if (selectedPlanRecord?.chosen) {
        focus.add(selectedPlanRecord.chosen.facility_id);
        for (const id of laneEnds(recordLaneIds(selectedPlanRecord))) focus.add(id);
      }
      if (record?.chosen) {
        focus.add(record.chosen.facility_id);
        for (const id of laneEnds(recordLaneIds(record))) focus.add(id);
      }
      if (record) {
        for (const candidate of record.scored.slice(0, 4)) focus.add(candidate.facility_id);
      }
    }
    const inFocus = (facilityId: string) => focus === null || focus.has(facilityId);
    for (const layer of ["lanes-road", "lanes-sea", "lanes-air"]) {
      map.setPaintProperty(layer, "line-opacity", focus ? 0.08 : layer === "lanes-air" ? 0.3 : 0.35);
    }

    const arcFeatures: GeoJSON.Feature[] = [];
    const planDestinations = new Set<string>();
    // Shipments whose first mile is drawn as a road leg, and the facility that
    // leg ends at: every other trace of the same journey starts there.
    const roadEntry = new Map<string, LonLat>();
    /* Lane traces the SELECTED shipment has already had drawn. A committed
       shipment's decision record IS its booking, and a reviewed shipment's
       record IS its plan: tracing the same lanes a second time stacks two
       byte-identical lines, and the halo (line-opacity 0.22) composites to
       ~0.39 where they overlap — so one half of a journey reads heavier than
       the other. The record is traced below only where it says something the
       booking or the plan does not: a route that actually differs. */
    const drawnTraces = new Set<string>();
    const traceKey = (start: LonLat, laneIds: readonly string[], end: LonLat) =>
      JSON.stringify([start, laneIds, end]);

    // A road leg: the provider's geometry once it lands, a dashed straight
    // line meanwhile (and for legs the provider could only estimate).
    const addRoadLeg = (shipmentId: string, from: LonLat, to: LonLat) => {
      if (samePoint(from, to)) return;
      const route = roadLeg(from, to);
      const points = route && route.path.length >= 2 ? unwrap(route.path) : greatCircle(from, to);
      arcFeatures.push(
        lineFeature(points, {
          grade: "road",
          estimated: route === null || route.estimated,
          shipment: shipmentId,
        }),
      );
    };
    /* Where a route ENTERS the lane network. A delivery from a coordinate
       origin is driven to that facility on a road mile of its own, so every
       trace of the same journey — the booking's and the recorded decision's
       alike — has to start there. Starting at the origin instead draws the
       mile a second time, straight, under the road line already drawing it. */
    const entryOf = (laneIds: readonly string[], fallback: LonLat): LonLat => {
      const first = laneIds.length > 0 ? laneById.get(laneIds[0]!) : undefined;
      const facility = first ? facilityById.get(first.from) : undefined;
      return facility ? [facility.lon, facility.lat] : fallback;
    };

    for (const shipment of world.shipments) {
      const booking = shipment.destination;
      if (!booking) continue;
      if (focus && shipment.id !== selectedShipment) continue;
      const origin = originOf(shipment.id);
      const holding = facilityById.get(booking.facility_id);
      if (!origin || !holding) continue;
      const isSelected = shipment.id === selectedShipment;
      const grade = isSelected ? "selected" : "booked";
      const holdingAt: LonLat = [holding.lon, holding.lat];
      const customer = shipment.destination_point;
      const isDelivery = customer !== null && onEarth(customer.lon, customer.lat);

      // Where the goods ENTER the lane network, and where they leave it. A
      // delivery is driven to and from those points on road miles of its own,
      // so each mile belongs to exactly one line: the lane trace runs between
      // the entry and the exit, and never repeats a mile as a straight
      // shortcut underneath the road that is already drawing it.
      const entryAt = entryOf(booking.route, holdingAt);
      const exit = booking.exit_facility_id
        ? (facilityById.get(booking.exit_facility_id) ?? holding)
        : holding;
      const exitAt: LonLat = [exit.lon, exit.lat];
      const customerAt: LonLat | null = isDelivery ? [customer!.lon, customer!.lat] : null;
      // A delivery that starts AT a facility enters there: no mile to drive,
      // nothing to draw. An ordinary shipment from a coordinate keeps its one
      // synthetic direct leg into the network, exactly as before.
      const firstMile = isDelivery && !samePoint(origin, entryAt);
      const lastMile = customerAt !== null && !samePoint(exitAt, customerAt);

      const inboundFrom = firstMile ? entryAt : origin;
      const inbound = tracePath(inboundFrom, booking.route, holdingAt);
      if (inbound) {
        arcFeatures.push(lineFeature(inbound, { grade, shipment: shipment.id }));
        if (isSelected) drawnTraces.add(traceKey(inboundFrom, booking.route, holdingAt));
      }
      // The recorded decision is traced again below when this shipment is
      // selected: it has to start where the road mile ends, exactly as here.
      if (firstMile) roadEntry.set(shipment.id, entryAt);

      // Deliveries carry on past the hold: out to the exit facility, then the
      // last mile to the customer.
      if (!isDelivery || !customerAt) continue;
      const outbound = tracePath(holdingAt, booking.outbound_route, exitAt);
      if (outbound) arcFeatures.push(lineFeature(outbound, { grade, shipment: shipment.id }));
      if (isSelected) {
        // The provider's real geometry once it lands, the dashed estimate
        // until then — a cost only the SELECTED shipment pays.
        if (firstMile) addRoadLeg(shipment.id, origin, entryAt);
        if (lastMile) addRoadLeg(shipment.id, exitAt, customerAt);
      } else {
        // Unselected: no road fetch, so both ends get the SAME thin dashed
        // connector. Drawing one mile solid and the other dashed made the two
        // halves of one journey read as different kinds of thing.
        if (firstMile) {
          arcFeatures.push(
            lineFeature(greatCircle(origin, entryAt), {
              grade: "road",
              estimated: true,
              shipment: shipment.id,
            }),
          );
        }
        if (lastMile) {
          arcFeatures.push(
            lineFeature(greatCircle(exitAt, customerAt), {
              grade: "road",
              estimated: true,
              shipment: shipment.id,
            }),
          );
        }
      }
    }

    if (plan) {
      for (const [shipmentId, planRecord] of Object.entries(plan.records)) {
        if (!planRecord.chosen) continue;
        if (focus && shipmentId !== selectedShipment) continue;
        const origin = originOf(shipmentId);
        const destination = facilityById.get(planRecord.chosen.facility_id);
        if (!origin || !destination) continue;
        planDestinations.add(destination.id);
        const lanes = recordLaneIds(planRecord);
        const destinationAt: LonLat = [destination.lon, destination.lat];
        const path = tracePath(origin, lanes, destinationAt);
        if (!path) continue;
        arcFeatures.push(
          lineFeature(path, {
            grade: shipmentId === selectedShipment ? "selected" : "planned",
            shipment: shipmentId,
          }),
        );
        if (shipmentId === selectedShipment) {
          drawnTraces.add(traceKey(origin, lanes, destinationAt));
        }
      }
    }

    // The selected decision: alternatives as dashed beams (top three), the
    // chosen route traced and highlighted. Rejections are marker states and
    // panel rows — never a fan of arcs across the globe.
    const rejectedFacilities = new Set<string>();
    const candidateFacilities = new Set<string>();
    let recordChoice: string | null = null;
    if (record && selectedShipment) {
      for (const rejection of record.rejected) rejectedFacilities.add(rejection.facility_id);
      for (const candidate of record.scored) candidateFacilities.add(candidate.facility_id);
      recordChoice = record.chosen?.facility_id ?? null;
      const origin = originOf(selectedShipment);
      if (origin) {
        const alternatives = record.scored
          .filter((c) => c.facility_id !== recordChoice)
          .slice(0, 3);
        for (const candidate of alternatives) {
          const facility = facilityById.get(candidate.facility_id);
          if (!facility) continue;
          arcFeatures.push(
            lineFeature(greatCircle(origin, [facility.lon, facility.lat]), {
              grade: "alt",
              shipment: selectedShipment,
            }),
          );
        }
        const chosen = record.chosen;
        const facility = recordChoice ? facilityById.get(recordChoice) : undefined;
        if (chosen && facility) {
          // The booking above already drew this journey's first mile as a road
          // leg; tracing the record from the origin would lay a straight second
          // line along the same mile. Start where that leg ends.
          const start = roadEntry.get(selectedShipment) ?? origin;
          const lanes = recordLaneIds(record);
          const chosenAt: LonLat = [facility.lon, facility.lat];
          // Only where the record differs from what the booking or the plan
          // already drew: for a committed shipment the record IS the booking,
          // and one segment must be one line.
          if (!drawnTraces.has(traceKey(start, lanes, chosenAt))) {
            const path = tracePath(start, lanes, chosenAt);
            if (path) {
              arcFeatures.push(lineFeature(path, { grade: "selected", shipment: selectedShipment }));
            }
          }
        }
      }
    }
    (map.getSource("arcs") as maplibregl.GeoJSONSource).setData({
      type: "FeatureCollection",
      features: arcFeatures,
    });
    if (import.meta.env.DEV) {
      // Dev-only handle, beside __nodalMap: the arcs exactly as drawn, so a
      // browser-driven check can ask what each line IS and where it starts —
      // rather than inferring one line from two overlapping pixels.
      (window as unknown as { __nodalArcs?: GeoJSON.Feature[] }).__nodalArcs = arcFeatures;
    }

    // -- zone extrusions (data-true heights; sqrt-scaled) -------------------
    // Shell footprint 500 m, occupancy core 320 m inside it: distinct walls,
    // no z-fighting. An overfull core rising past its shell is deliberate —
    // that IS what overcommitment looks like.
    const zoneFeatures: GeoJSON.Feature[] = [];
    for (const facility of world.facilities) {
      facility.zones.forEach((zone, index) => {
        const capacity = zone.capacity.slots;
        const occupancy = zone.occupancy.slots;
        if (capacity === null && occupancy === 0) return;
        const shell = capacity !== null ? capacity : occupancy;
        const utilization =
          capacity !== null && capacity > 0 ? occupancy / capacity : occupancy > 0 ? 1 : 0;
        const shellRing = squareAround(
          [facility.lon, facility.lat], 500, index, facility.zones.length,
        );
        const coreRing = squareAround(
          [facility.lon, facility.lat], 320, index, facility.zones.length,
        );
        zoneFeatures.push({
          type: "Feature",
          properties: {
            kind: "shell",
            height_m: Math.max(50, slotsToMeters(shell)),
            utilization,
          },
          geometry: { type: "Polygon", coordinates: [shellRing] },
        });
        if (occupancy > 0) {
          zoneFeatures.push({
            type: "Feature",
            properties: {
              kind: "core",
              height_m: Math.max(30, slotsToMeters(occupancy)),
              utilization,
            },
            geometry: { type: "Polygon", coordinates: [coreRing] },
          });
        }
      });
    }
    (map.getSource("zones") as maplibregl.GeoJSONSource).setData({
      type: "FeatureCollection",
      features: zoneFeatures,
    });

    // -- DOM markers (the design's node grammar) ----------------------------
    for (const marker of markersRef.current) marker.remove();
    markersRef.current = [];

    // A disruption may target the facility itself OR one of its zones.
    const zoneOwner = new Map<string, string>();
    for (const facility of world.facilities) {
      for (const zone of facility.zones) zoneOwner.set(zone.id, facility.id);
    }
    const disruptedFacilities = new Set(
      world.disruptions
        .filter((d) => d.status === "active")
        .map((d) => zoneOwner.get(d.target_id) ?? d.target_id),
    );
    const selectedOriginFacility = selected?.origin_facility_id ?? null;
    const selectedDestination = selected?.destination?.facility_id ?? null;
    // Where the SELECTED booked journey dwells on the way (§7.9). The hold is
    // already the `chosen` hub; these are the facilities the goods pass
    // through, marked only while their shipment is selected.
    const selectedStops = new Set(
      (selected?.destination?.stops ?? [])
        .filter((stop) => stop.role !== "hold")
        .map((stop) => stop.facility_id),
    );
    // Cargo a closure stranded stands at its own origin facility: the hub says
    // so, so an operator reads it off the map without opening the queue.
    const trappedFacilities = new Set(
      world.shipments
        .filter((s) => s.trapped && s.origin_facility_id !== null)
        .map((s) => s.origin_facility_id!),
    );

    // Coordinate origins: every planned one is a diamond the operator can
    // pick; a booked one shows only while selected (its arc already tells
    // the story); in focus only the selected one remains. A factory is often
    // within a few pixels of its hub at continental zoom, so stacking order
    // alone can't arbitrate clicks.
    const origins = world.shipments.filter(
      (s) =>
        onEarth(s.origin_lon, s.origin_lat) &&
        (focus ? s.id === selectedShipment : s.status === "planned"),
    );

    // Click arbitration over every marker on screen, measured against the
    // markers' own DOM rectangles (which sit on whichever world copy MapLibre
    // drew — map.project() would not): a click inside a hub's drawn square
    // opens that hub; anything else goes to the nearest marker of any kind.
    // Keyboard / programmatic activation (detail === 0) has no position and
    // activates the focused marker itself.
    interface Target {
      kind: "hub" | "origin" | "endpoint";
      id: string;
      shape: HTMLElement;
      /** A hub's text label: clicking it is clicking the hub. */
      label?: HTMLElement;
    }
    const targets: Target[] = [];
    const activate = (target: Target) => {
      if (target.kind === "hub") handlers.current.onOpenFacility(target.id);
      else handlers.current.onSelectShipment(target.id);
    };
    // Endpoint dots are scenery next to the nodes an operator works with: at
    // the same distance a hub or a diamond always wins the click.
    const rank = (target: Target) => (target.kind === "endpoint" ? 1 : 0);
    const arbitrate = (event: MouseEvent, own: Target) => {
      if (event.detail === 0) {
        activate(own);
        return;
      }
      const { clientX: x, clientY: y } = event;
      const inside = (r: DOMRect) => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
      const centre = (r: DOMRect) => Math.hypot(r.left + r.width / 2 - x, r.top + r.height / 2 - y);
      let insideHub: { target: Target; d: number } | null = null;
      // Ties go to the marker the operator actually clicked.
      let nearest = { target: own, d: centre(own.shape.getBoundingClientRect()) };
      for (const target of targets) {
        const r = target.shape.getBoundingClientRect();
        const d = centre(r);
        const drawn = inside(r) || (target.label !== undefined && inside(target.label.getBoundingClientRect()));
        if (target.kind === "hub" && drawn && (!insideHub || d < insideHub.d)) {
          insideHub = { target, d };
        }
        if (d < nearest.d || (d === nearest.d && rank(target) < rank(nearest.target))) {
          nearest = { target, d };
        }
      }
      activate(insideHub?.target ?? nearest.target);
    };

    // -- endpoint dots: the businesses at the ends of the network ------------
    // Every booked shipment's coordinate origin and every delivery's customer
    // point, dimmer and smaller than any hub. Several shipments often share a
    // business, so dots dedupe by rounded coordinate and name every shipment
    // in their title; a click selects the first of them by id.
    // A shipment that already gets an origin DIAMOND (the selected one, in
    // focus) must not also get a dot underneath it at the same coordinate.
    const diamondIds = new Set(origins.map((s) => s.id));
    const endpoints = new Map<
      string,
      { at: LonLat; customer: boolean; label: string; ids: string[] }
    >();
    for (const shipment of world.shipments) {
      if (focus && shipment.id !== selectedShipment) continue;
      const customerPoint = shipment.destination_point;
      const ends: { at: LonLat; customer: boolean; label: string }[] = [];
      if (
        shipment.destination &&
        !diamondIds.has(shipment.id) &&
        onEarth(shipment.origin_lon, shipment.origin_lat)
      ) {
        ends.push({
          at: [shipment.origin_lon!, shipment.origin_lat!],
          customer: false,
          label: shipment.origin_label ?? "origin",
        });
      }
      if (customerPoint && onEarth(customerPoint.lon, customerPoint.lat)) {
        ends.push({
          at: [customerPoint.lon, customerPoint.lat],
          customer: true,
          label: customerPoint.label,
        });
      }
      for (const end of ends) {
        const key = `${end.customer ? "b" : "a"}:${end.at[0].toFixed(3)},${end.at[1].toFixed(3)}`;
        const existing = endpoints.get(key);
        if (existing) existing.ids.push(shipment.id);
        else endpoints.set(key, { ...end, ids: [shipment.id] });
      }
    }
    for (const endpoint of endpoints.values()) {
      const ids = [...endpoint.ids].sort();
      const classes = ["map-node", "endpoint-node"];
      if (endpoint.customer) classes.push("customer-node");
      if (selectedShipment !== null && ids.includes(selectedShipment)) classes.push("selected");
      const element = document.createElement("button");
      element.className = classes.join(" ");
      element.title = `${endpoint.label} — ${ids.join(", ")}`;
      const dot = document.createElement("span");
      dot.className = "endpoint-dot";
      element.appendChild(dot);
      const target: Target = { kind: "endpoint", id: ids[0]!, shape: dot };
      targets.push(target);
      element.addEventListener("click", (event) => {
        event.stopPropagation();
        arbitrate(event, target);
      });
      markersRef.current.push(
        new maplibregl.Marker({ element, anchor: "center" }).setLngLat(endpoint.at).addTo(map),
      );
    }

    const addOrigin = (shipment: (typeof origins)[number]) => {
      const isSelected = shipment.id === selectedShipment;
      const element = document.createElement("button");
      element.className = isSelected ? "map-node origin-node selected" : "map-node origin-node";
      element.title = `${shipment.id} — ${shipment.origin_label ?? "origin"}`;
      const diamond = document.createElement("span");
      diamond.className = "origin-diamond";
      element.appendChild(diamond);
      const target: Target = { kind: "origin", id: shipment.id, shape: diamond };
      targets.push(target);
      element.addEventListener("click", (event) => {
        event.stopPropagation();
        arbitrate(event, target);
      });
      markersRef.current.push(
        new maplibregl.Marker({ element, anchor: "center" })
          .setLngLat([shipment.origin_lon!, shipment.origin_lat!])
          .addTo(map),
      );
    };
    for (const shipment of origins) {
      if (shipment.id !== selectedShipment) addOrigin(shipment);
    }

    for (const facility of world.facilities) {
      if (!inFocus(facility.id)) continue;
      const disrupted = disruptedFacilities.has(facility.id);
      const classes = ["map-node"];
      if (facility.utilization >= 0.85) classes.push("util-hot");
      else if (facility.utilization >= 0.6) classes.push("util-warm");
      else if (facility.utilization > 0) classes.push("util-some");
      if (recordChoice === facility.id || selectedDestination === facility.id) {
        classes.push("chosen");
      } else if (candidateFacilities.has(facility.id)) classes.push("candidate");
      else if (rejectedFacilities.has(facility.id)) classes.push("rejected");
      if (planDestinations.has(facility.id)) classes.push("plan-dest");
      if (selectedStops.has(facility.id)) classes.push("transit-stop");
      if (selectedOriginFacility === facility.id) classes.push("origin");
      if (disrupted || !facility.open) classes.push("closed");
      const trapped = trappedFacilities.has(facility.id);
      if (trapped) classes.push("trapped");

      const element = document.createElement("button");
      element.className = classes.join(" ");
      element.title =
        `${facility.name} — utilization ${(facility.utilization * 100).toFixed(0)}%` +
        (selectedStops.has(facility.id) ? " — STOP ON THE SELECTED JOURNEY" : "") +
        (disrupted ? " — DISRUPTED" : "") +
        (!facility.open ? " — CLOSED" : "") +
        (trapped ? " — TRAPPED CARGO: MANUAL CLEARING REQUIRED" : "");
      const box = document.createElement("span");
      box.className = "node-box";
      const label = document.createElement("span");
      label.className = "node-label";
      label.textContent = facility.id;
      element.append(box, label);
      const target: Target = { kind: "hub", id: facility.id, shape: box, label };
      targets.push(target);
      element.addEventListener("click", (event) => {
        event.stopPropagation();
        arbitrate(event, target);
      });
      markersRef.current.push(
        new maplibregl.Marker({ element, anchor: "center" })
          .setLngLat([facility.lon, facility.lat])
          .addTo(map),
      );
    }

    // The selected origin sits above everything: its highlight must show.
    const selectedOrigin = origins.find((s) => s.id === selectedShipment);
    if (selectedOrigin) addOrigin(selectedOrigin);

    if (!fittedRef.current) {
      const bounds = networkBounds(world);
      if (!bounds.isEmpty()) {
        // Keep the network clear of the floating panels: pad the fit by
        // whatever overlays this view shows.
        asProgram(() => map.fitBounds(bounds, { padding: padding(), duration: 0, maxZoom: 8 }));
        fittedRef.current = true;
      }
    }

    // -- focus camera --------------------------------------------------------
    // First focus remembers the operator's view; each selection fits its
    // origin and its destination (plus the route between) — and fits again
    // when a SOLVE gives a planned shipment its destination. Closing the
    // selection restores the remembered view. Log refreshes with the same
    // selection leave the camera alone.
    const focusState = focusRef.current;
    const fitKey = selected && focus ? `${selected.id}|${focusDestination}` : null;
    if (focus && selected && fitKey && focusState.fittedFor !== fitKey) {
      if (focusState.before === null) {
        focusState.before = {
          center: map.getCenter(),
          zoom: map.getZoom(),
          pitch: map.getPitch(),
          bearing: map.getBearing(),
        };
      }
      const focusPoints: LonLat[] = [];
      if (selected.origin_facility_id) {
        const facility = facilityById.get(selected.origin_facility_id);
        if (facility) focusPoints.push([facility.lon, facility.lat]);
      }
      const origin = originOf(selected.id);
      if (origin) focusPoints.push(origin);
      const routeIds = selected.destination
        ? [
            ...laneEnds(selected.destination.route),
            ...laneEnds(selected.destination.outbound_route),
            ...(selected.destination.exit_facility_id
              ? [selected.destination.exit_facility_id]
              : []),
            // Every dwell the journey books, from the booking itself: a stop
            // the lanes somehow don't reach must still be inside the frame.
            ...selected.destination.stops.map((stop) => stop.facility_id),
          ]
        : record?.chosen
          ? laneEnds(recordLaneIds(record))
          : selectedPlanRecord?.chosen
            ? laneEnds(recordLaneIds(selectedPlanRecord))
            : [];
      for (const id of [...routeIds, ...(focusDestination ? [focusDestination] : [])]) {
        const facility = facilityById.get(id);
        if (facility) focusPoints.push([facility.lon, facility.lat]);
      }
      // A delivery's frame ends at the customer, not at the last facility.
      const customerPoint = selected.destination_point;
      if (customerPoint && onEarth(customerPoint.lon, customerPoint.lat)) {
        focusPoints.push([customerPoint.lon, customerPoint.lat]);
      }
      const bounds = boundsOver(focusPoints);
      if (!bounds.isEmpty()) {
        asProgram(() => {
          map.jumpTo({ pitch: 0, bearing: 0 });
          map.fitBounds(bounds, { padding: padding(), duration: 0, maxZoom: 7 });
        });
        setPitched(false);
        focusState.bounds = bounds;
        setFocused(true);
      }
      focusState.fittedFor = fitKey;
    } else if (!focus && focusState.fittedFor !== null) {
      const before = focusState.before;
      focusRef.current = { fittedFor: null, before: null, bounds: null };
      setFocused(false);
      if (before) {
        asProgram(() => map.jumpTo(before));
        setPitched(before.pitch > 0);
      }
    }
  }, [world, plan, record, selectedShipment, facilityById, ready, roadVersion, roadLeg]);

  // The panels changed width (interface scale) or the window changed size:
  // whatever was fitted — the network or the focused shipment — fits again.
  const worldRef = useRef(world);
  worldRef.current = world;
  const refit = useCallback((respectUserCamera: boolean) => {
    const map = mapRef.current;
    const current = worldRef.current;
    if (!map || !fittedRef.current) return;
    // The operator navigated somewhere (or is pitched in 3D): leave their
    // camera alone on a resize; a focus fit is ours to maintain.
    if (respectUserCamera && !focusRef.current.bounds && (userMovedRef.current || map.getPitch() > 0)) {
      return;
    }
    // May run before App's effect applies the new geometry to the DOM, which
    // is fine: the padding is computed from the scale, not measured.
    const pad = padding();
    const focusBounds = focusRef.current.bounds;
    if (focusBounds) {
      asProgram(() => map.fitBounds(focusBounds, { padding: pad, duration: 0, maxZoom: 7 }));
      return;
    }
    if (!current || current.empty) return;
    userMovedRef.current = false;
    asProgram(() =>
      map.fitBounds(networkBounds(current), { padding: pad, duration: 0, maxZoom: 8 }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refs only
  }, []);
  useEffect(() => {
    // A scale change resizes the panels deliberately: always re-fit.
    if (ready) refit(false);
  }, [uiScale, ready, refit]);
  useEffect(() => {
    // MapLibre resizes its canvas on the same event; re-fit after it has —
    // unless the operator had moved the camera, whose view then survives.
    let timer = 0;
    const onResize = () => {
      resizingRef.current = true; // canvas-resize moves are not the operator's
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        refit(true);
        resizingRef.current = false;
      }, 150);
    };
    window.addEventListener("resize", onResize);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("resize", onResize);
    };
  }, [refit]);

  // Zone blocks are a few hundred metres across: below this zoom they are
  // sub-pixel, so entering 3D dives to a hub first.
  const DIVE_BELOW_ZOOM = 11;

  const togglePitch = () => {
    const map = mapRef.current;
    if (!map) return;
    const next = !pitched;
    // jumpTo, never easeTo: no animation may carry state (reduced-motion rule).
    if (next && map.getZoom() < DIVE_BELOW_ZOOM && world && !world.empty) {
      // Dive to the hub nearest the operator's current view centre — their
      // regional intent survives — and remember where they were. Distance
      // wraps across the antimeridian and shrinks longitude by cos(lat).
      const center = map.getCenter();
      const cosLat = Math.cos((center.lat * Math.PI) / 180);
      const distance = (f: FacilitySummary) => {
        const dlon = (((f.lon - center.lng + 540) % 360) - 180) * cosLat;
        return (f.lat - center.lat) ** 2 + dlon ** 2;
      };
      const nearest = [...world.facilities].sort(
        (a, b) => distance(a) - distance(b) || a.id.localeCompare(b.id),
      )[0];
      if (nearest) {
        const landed = { center: [nearest.lon, nearest.lat] as LonLat, zoom: 13.6 };
        preDiveRef.current = { center, zoom: map.getZoom(), landed };
        asProgram(() =>
          map.jumpTo({ center: landed.center, zoom: landed.zoom, pitch: 60, bearing: -18 }),
        );
        setPitched(true);
        return;
      }
    }
    if (!next && preDiveRef.current) {
      // Leaving 3D after a dive: back to the view the operator had — unless
      // they navigated somewhere else while pitched, in which case flatten
      // where they are.
      const { center, zoom, landed } = preDiveRef.current;
      preDiveRef.current = null;
      // "Still there" is a screen question: the dive point must still sit
      // in the middle half of the viewport at roughly the dive zoom.
      const canvas = map.getCanvas().getBoundingClientRect();
      const p = map.project(landed.center);
      const stayed =
        Math.abs(map.getZoom() - landed.zoom) < 0.5 &&
        Math.abs(p.x - canvas.width / 2) < canvas.width / 4 &&
        Math.abs(p.y - canvas.height / 2) < canvas.height / 4;
      if (stayed) {
        asProgram(() => map.jumpTo({ center, zoom, pitch: 0, bearing: 0 }));
        setPitched(false);
        return;
      }
    }
    asProgram(() => map.jumpTo({ pitch: next ? 60 : 0, bearing: next ? -18 : 0 }));
    setPitched(next);
  };

  // FIT frames what is on the map: the focused shipment while one is
  // selected, the whole network otherwise.
  const fitNetwork = () => {
    const map = mapRef.current;
    if (!map || !world || world.empty) return;
    preDiveRef.current = null;
    setPitched(false);
    const focusBounds = focusRef.current.bounds;
    if (focusBounds) {
      asProgram(() => {
        map.jumpTo({ pitch: 0, bearing: 0 });
        map.fitBounds(focusBounds, { padding: padding(), duration: 0, maxZoom: 7 });
      });
      return;
    }
    focusRef.current.before = null; // FIT is an explicit new view; closing won't undo it
    userMovedRef.current = false;
    asProgram(() => {
      map.jumpTo({ pitch: 0, bearing: 0 });
      map.fitBounds(networkBounds(world), { padding: padding(), duration: 0, maxZoom: 8 });
    });
  };

  const rootClasses = ["map-root"];
  if (overlays?.left) rootClasses.push("left-inset");
  if (overlays?.bottom) rootClasses.push("bottom-inset");
  if (rightPanelOpen) rootClasses.push("right-inset");
  if (picking) rootClasses.push("picking");

  return (
    <div className={rootClasses.join(" ")}>
      <div ref={containerRef} className="map-canvas" />
      <div className="map-controls">
        <button
          className="chip"
          onClick={fitNetwork}
          title={focused ? "Fit the selected shipment's route" : "Fit the whole network"}
        >
          FIT
        </button>
        <button
          className={pitched ? "chip active-chip" : "chip"}
          onClick={togglePitch}
          title={pitched ? "Back to 2D" : "Pitch the camera: zone blocks rise with capacity"}
        >
          {pitched ? "2D" : "3D"}
        </button>
      </div>
    </div>
  );
}
