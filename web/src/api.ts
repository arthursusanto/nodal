/* Typed client for the Nodal API (§11). One fetch helper, thin types mirroring
   the server's read models — the UI renders what the engine knows, adds nothing. */

export type Dim = "slots" | "volume_l" | "weight_g";
export type DimMap = Record<Dim, number | null>;

export interface ZoneSummary {
  id: string;
  kind: string;
  lots: number;
  occupancy: Record<Dim, number>;
  capacity: DimMap;
}

export interface FacilitySummary {
  id: string;
  name: string;
  lat: number;
  lon: number;
  open: boolean;
  utilization: number;
  zones: ZoneSummary[];
}

/** A drawable geometry point, in GeoJSON order: [lon, lat]. */
export type LonLat = [number, number];

export interface LaneSummary {
  id: string;
  from: string;
  to: string;
  mode: string;
  km: number;
  minutes: number;
  /** Real geometry when the lane declares it (road lanes); null = draw the great circle. */
  path: LonLat[] | null;
}

/** Why a delivery touches a facility (§7.9). */
export type StopRole = "entry" | "transit" | "hold" | "exit";

/** One scheduled dwell at a facility (§7.9): the window the goods occupy it
    for, and the staging zone that window books. Every facility a delivery
    touches is one of these — the windows closures are matched against. */
export interface Stop {
  facility_id: string;
  role: StopRole;
  arrive: string;
  depart: string;
  /** The staging/holding zone; null until one is chosen. */
  zone_id: string | null;
}

export interface Destination {
  facility_id: string;
  zone_id: string;
  eta: string;
  departure: string;
  /** Inbound: origin → the holding facility. */
  route: string[];
  /** Outbound (deliveries only): holding facility → exit facility; [] otherwise. */
  outbound_route: string[];
  /** Where the last mile to the customer departs from; null for non-deliveries. */
  exit_facility_id: string | null;
  /** Every facility the booked journey touches, in travel order; [] for an
      ordinary allocation (and for bookings made before stops existed). */
  stops: Stop[];
}

/** The customer point of an A→B delivery — outside the network (§7.9). */
export interface DestinationPoint {
  label: string;
  lat: number;
  lon: number;
}

export interface ShipmentLine {
  sku: string;
  group: string;
  quantity: number;
  uom: string;
}

export interface ShipmentSummary {
  id: string;
  status: string;
  is_transfer: boolean;
  /** A closure shut around these goods where they stand (§7.9): the booking
      survives, nothing re-routes it, and only an operator can clear it. */
  trapped: boolean;
  origin_facility_id: string | null;
  origin_label: string | null;
  origin_lat: number | null;
  origin_lon: number | null;
  ready_at: string;
  deadline: string | null;
  size: Record<Dim, number>;
  lines: ShipmentLine[];
  requirements: {
    temp_c: [number, number] | null;
    compat_class: string | null;
    dwell_days: number | null;
  };
  /** Set = this is a delivery: the goods leave the network again for a customer. */
  destination_point: DestinationPoint | null;
  /** Days the customer requires the goods held before the last mile. */
  hold_days: number;
  destination: Destination | null;
}

export interface DisruptionSummary {
  id: string;
  kind: string;
  target_id: string;
  /** The facility the disruption lands on (null for lane disruptions). */
  facility_id: string | null;
  detail: string | null;
  from_ts: string;
  until_ts: string;
  magnitude: number;
  status: "active" | "upcoming";
}

/** One line per shipment in the plan drafted at the head — the signal that a
    review is open, not the review itself: the records the map and the decision
    panel draw from come from `/api/plan`. */
export interface PendingPlanRow {
  facility_id: string | null;
  zone_id: string | null;
  eta: string | null;
  unassigned_reasons: string[];
}

/** A batch plan proposed into the log and not yet committed or discarded
    (§7.5). It outlives the browser: a reload — or another operator — finds the
    same review open. */
export interface PendingPlan {
  batch_id: string;
  /** Head the solve READ; a commit hands it back as `expected_head`. */
  based_on_seq: number;
  /** Head the draft itself sits at. */
  head: number;
  assigned: number;
  unassigned: number;
  decisions: Record<string, PendingPlanRow>;
}

export interface MapState {
  empty: boolean;
  now?: string;
  seq?: number;
  facilities: FacilitySummary[];
  lanes: LaneSummary[];
  shipments: ShipmentSummary[];
  disruptions: DisruptionSummary[];
  /** A plan awaiting review at this point in the log, if there is one. */
  pending_plan: PendingPlan | null;
}

export interface ComponentScore {
  raw: number;
  normalized: number;
  weight: number;
  contribution: number;
  baseline: number | null;
}

export interface RouteSummary {
  legs: {
    from_label: string;
    to_facility_id: string;
    lane_id: string | null;
    minutes: number;
    km: number;
    cost_cents: number;
  }[];
  minutes: number;
  wait_minutes: number;
  km: number;
  cost_cents: number;
  transfers: number;
}

export interface Reject {
  constraint_id: string;
  data: Record<string, unknown>;
}

/** One scheduled movement of a delivery: a road mile or a lane leg (§7.9). */
export interface ItineraryLeg {
  /** "road" for the first and last mile, else the lane's mode. */
  kind: string;
  from_label: string;
  to_label: string;
  lane_id: string | null;
  km: number;
  minutes: number;
  cost_cents: number;
  depart: string;
  arrive: string;
}

/** Where and when the goods sit between the inbound and outbound legs. */
export interface Hold {
  facility_id: string;
  zone_id: string;
  from_ts: string;
  until_ts: string;
}

export interface Itinerary {
  legs: ItineraryLeg[];
  hold: Hold;
  /** Every facility the journey touches, in travel order, hold included. */
  stops: Stop[];
  /** The customer's label. */
  destination: string;
  delivered_at: string;
  cost_cents: number;
}

export interface ScoredCandidate {
  facility_id: string;
  zone_id: string;
  route: RouteSummary;
  eta: string;
  departure: string;
  components: Record<string, ComponentScore>;
  total: number;
  zone_verdicts: Record<string, Reject[]>;
}

export interface RejectedFacility {
  facility_id: string;
  facility_verdicts: Reject[];
  zone_verdicts: Record<string, Reject[]>;
}

export interface DecisionRecord {
  shipment_id: string;
  decided_at: string;
  mode: string;
  policy: string;
  considered: string[];
  rejected: RejectedFacility[];
  scored: ScoredCandidate[];
  chosen: {
    facility_id: string;
    zone_id: string;
    route: RouteSummary;
    eta: string;
    departure: string;
    /** Deliveries only: the whole movement through to the customer. */
    itinerary?: Itinerary | null;
    /** Every facility the chosen plan touches (§7.9) — ordinary routes too;
        absent on records written before stops existed. */
    stops?: Stop[];
  } | null;
  solver: { status: string; gap: number | null; batch_id: string } | null;
  reopt_tier: number | null;
  reopt_trigger: string | null;
}

export interface TimelineBucket {
  day: string;
  occupancy: Record<Dim, number>;
  capacity: DimMap;
}

export interface FacilityDetail {
  facility_id: string;
  zones: {
    id: string;
    kind: string;
    buckets: TimelineBucket[];
    reservations: {
      id: string;
      holder: string;
      /** What the booking IS: the customer's hold, or a pass-through dwell. */
      role: StopRole;
      from_ts: string;
      until_ts: string;
      size: Record<Dim, number>;
    }[];
    lots: {
      id: string;
      sku: string;
      group: string;
      quantity: number;
      compat_class: string | null;
      planned_departure: string | null;
    }[];
  }[];
}

export interface ForecastData {
  facilities: {
    facility_id: string;
    groups: {
      group: string;
      rate: number;
      target: number;
      current: number;
      series: { day: string; projected: number }[];
    }[];
  }[];
}

/** What a scheduled window IS: a dwell at a facility, or the last mile that
    ends a delivery at its customer. */
export type MovementRole = StopRole | "last_mile";

/** One window the booked goods occupy (§7.9) — every dwell on the way and the
    last mile that ends a delivery. A shipment contributes one row per window,
    so `id`, never `shipment_id`, is what identifies a row. */
export interface Movement {
  id: string;
  shipment_id: string;
  status: string;
  is_transfer: boolean;
  role: MovementRole;
  facility_id: string;
  /** The staging or holding zone; null on a last mile, which books none. */
  zone_id: string | null;
  ready_at: string;
  /** The window itself: when the goods reach this facility and when they leave.
      Null when the engine recorded no time for it. */
  arrive: string | null;
  depart: string | null;
  /** The same two moments under the names this read model has always used. */
  eta: string | null;
  departure: string | null;
  /** The customer a delivery ends at; null for anything else. */
  customer_label: string | null;
}

export interface ScheduleData {
  movements: Movement[];
}

export interface EventRow {
  seq: number;
  ts: string;
  type: string;
  entity_type: string;
  entity_id: string;
  actor: string;
  cause: string | null;
}

export interface WhatIfResult {
  at: string;
  hypotheticals: string[];
  policy: string;
  baseline: { allocated: number; unassigned: number; cost_cents: number };
  fork: { allocated: number; unassigned: number; cost_cents: number };
  changes: {
    shipment_id: string;
    before: string[] | null;
    after: string[] | null;
    cost_delta_cents: number;
  }[];
  reopts: {
    trigger: string;
    tier: number;
    affected: string[];
    moved: string[];
    trapped: TrappedCargo[];
  }[];
  rendered: string;
}

/** One shipment a closure stranded: it sits at `facility_id` with its booking
    intact, and only an operator can clear it (§7.9). */
export interface TrappedCargo {
  shipment_id: string;
  facility_id: string;
  disruption_id: string;
}

export interface ReoptSummary {
  tier: number;
  affected: string[];
  moved: string[];
  released: string[];
  /** Cargo the closure stranded — never re-planned, never released. */
  trapped: TrappedCargo[];
}

/** A batch solve (§7.4): one decision record per planned shipment. A dry run
    returns the same shape as a commit — it is the plan the operator reviews. */
export interface BatchPlan {
  /** The plan's identity in the log; a discard names it, a commit reuses it. */
  batch_id: string;
  /** Log head the plan was solved against; committing hands it back as `expected_head`. */
  head: number;
  meta: { status?: string; shipments?: number; gap?: number | null };
  assignments: Record<string, [string, string] | null>;
  records: Record<string, DecisionRecord>;
}

/* Bearer-token auth: the token lives in module state + sessionStorage; a 401
   raises AuthError so the app can fall back to the connect screen. */

const TOKEN_KEY = "nodal-token";
let authToken: string | null = sessionStorage.getItem(TOKEN_KEY);
let authFailureHandler: (() => void) | null = null;

export class AuthError extends Error {}

/** A non-2xx API answer, with the status so callers can tell a 409 (stale
    plan) from a genuine failure. */
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

export function setAuthToken(token: string | null): void {
  authToken = token;
  if (token === null) sessionStorage.removeItem(TOKEN_KEY);
  else sessionStorage.setItem(TOKEN_KEY, token);
}

export function hasAuthToken(): boolean {
  return authToken !== null;
}

/** Fires on every 401, before AuthError propagates — so the app drops to the
    connect screen even when the failing fetch was caught by a local panel. */
export function onAuthFailure(handler: (() => void) | null): void {
  authFailureHandler = handler;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (authToken !== null) headers.set("Authorization", `Bearer ${authToken}`);
  const response = await fetch(path, { ...init, headers });
  if (response.status === 401) {
    authFailureHandler?.();
    throw new AuthError("invalid token");
  }
  // The API always answers JSON. Anything else — the dev server's own HTML
  // when its /api proxy is not running, a proxy's plain-text error — means
  // the request never reached Nodal, and must say so instead of surfacing
  // as a JSON parse failure.
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    throw new ApiError(
      `the API did not answer ${path} (${response.status} ${contentType || "no content type"}) — ` +
        "is the Nodal server running behind this address?",
      response.status,
    );
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) {
        // FastAPI validation errors: an array of {loc, msg, ...} objects.
        detail = body.detail
          .map((item) => {
            const err = item as { loc?: unknown[]; msg?: string };
            const where = Array.isArray(err.loc) ? err.loc.slice(1).join(".") : "";
            return where ? `${where}: ${err.msg ?? "invalid"}` : (err.msg ?? "invalid");
          })
          .join("; ");
      }
    } catch {
      /* keep statusText */
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

const atSuffix = (at?: string | null) => (at ? `&at=${encodeURIComponent(at)}` : "");

export const api = {
  map: (at?: string) =>
    request<MapState>(at ? `/api/map?at=${encodeURIComponent(at)}` : "/api/map"),
  facility: (id: string, days = 14, at?: string | null) =>
    request<FacilityDetail>(
      `/api/facilities/${encodeURIComponent(id)}?days=${days}${atSuffix(at)}`,
    ),
  forecast: (days = 14, at?: string | null) =>
    request<ForecastData>(`/api/forecast?days=${days}${atSuffix(at)}`),
  schedules: (at?: string | null) =>
    request<ScheduleData>(
      at ? `/api/schedules?at=${encodeURIComponent(at)}` : "/api/schedules",
    ),
  decision: (shipmentId: string) =>
    request<{ seq: number; ts: string; record: DecisionRecord }>(
      `/api/decisions/${encodeURIComponent(shipmentId)}`,
    ),
  events: (afterSeq: number, limit = 100) =>
    request<{ events: EventRow[]; head: number }>(
      `/api/events?after_seq=${afterSeq}&limit=${limit}`,
    ),
  allocate: (shipmentId: string, commit: boolean) =>
    post<{ record: DecisionRecord; committed_events: number }>("/api/commands/allocate", {
      shipment_id: shipmentId,
      commit,
    }),
  /** A dry run DRAFTS the plan into the log (§7.5), so it answers with the head
      AFTER the draft — adopt it, or the next poll reads our own write as
      another operator's move and throws the review away. */
  optimize: (commit: boolean, expectedHead: number | null = null, batchId: string | null = null) =>
    post<{
      batch: BatchPlan | null;
      batch_id: string | null;
      head: number;
      committed_events?: number;
    }>("/api/commands/optimize", {
      commit,
      rebalance: false,
      expected_head: expectedHead,
      // Which plan is being booked, not just which log head it was solved
      // against: the server refuses (409) to commit any plan but the one the
      // operator reviewed.
      batch_id: batchId,
    }),
  /** The plan awaiting review, in exactly the shape a dry run returns. */
  plan: (at: string | null = null) =>
    request<{ batch: BatchPlan | null }>(`/api/plan${at ? `?at=${encodeURIComponent(at)}` : ""}`),
  /** Say NO to the drafted plan. Nothing is unbooked — a draft books nothing —
      but the review that ended in NO is history too. */
  discardPlan: (batchId: string, reason = "") =>
    post<{ batch_id: string; appended: number; head: number }>("/api/commands/plan/discard", {
      batch_id: batchId,
      reason,
    }),
  whatif: (events: string[], at: string | null = null, policy = "nodal-batch") =>
    post<WhatIfResult>("/api/commands/whatif", { events, policy, at }),
  disrupt: (kind: string, targetId: string, days: number, magnitude = 1.0) =>
    post<{ disruption_id: string; reopt: ReoptSummary | null; reopt_error?: string }>(
      "/api/commands/disrupt",
      { kind, target_id: targetId, days, magnitude },
    ),
  /** End a disruption now; with `until`, end it now and reopen it until then. */
  endDisruption: (disruptionId: string, until: string | null = null) =>
    post<{
      ended: string;
      disruption_id: string | null;
      reopt: ReoptSummary | null;
      reopt_error?: string;
    }>("/api/commands/disruptions/end", { disruption_id: disruptionId, until }),
  registerShipment: (body: NewShipment) =>
    post<{ shipment_id: string; delivery: boolean; appended: number }>(
      "/api/commands/shipments",
      body,
    ),
  /** Free-text business search; `enabled` false = no provider key configured. */
  placesSearch: (query: string) =>
    request<{ enabled: boolean; places: PlaceResult[] }>(
      `/api/places/search?q=${encodeURIComponent(query)}`,
    ),
  /** Driving distance, time and geometry; always answers, `estimated` when guessed. */
  roadRoute: (fromLat: number, fromLon: number, toLat: number, toLon: number) =>
    request<RoadRoute>(
      `/api/routes/road?from_lat=${fromLat}&from_lon=${fromLon}&to_lat=${toLat}&to_lon=${toLon}`,
    ),
  cancelShipment: (shipmentId: string, reason = "") =>
    post<{ shipment_id: string; appended: number }>("/api/commands/shipments/cancel", {
      shipment_id: shipmentId,
      reason,
    }),
  /** Move a shipment's readiness; a delay of a booked one re-optimizes (§7.6). */
  setReady: (shipmentId: string, newReady: string, reason = "") =>
    post<{
      shipment_id: string;
      new_ready: string;
      reopt: ReoptSummary | null;
      reopt_error?: string;
    }>("/api/commands/shipments/ready", { shipment_id: shipmentId, new_ready: newReady, reason }),
  /** Order §7.7 rebalancing transfers and book them; the planned queue is untouched. */
  rebalance: () =>
    post<{ transfers_registered: number; committed_events?: number }>(
      "/api/commands/rebalance",
      {},
    ),
  registerFacility: (body: NewFacility) =>
    post<{ facility_id: string; zone_ids: string[] }>("/api/commands/facilities", body),
  registerLane: (body: NewLane) =>
    post<{ lane_ids: string[]; km: number; minutes: number }>("/api/commands/lanes", body),
  addZone: (body: NewZoneInput & { facility_id: string }) =>
    post<{ zone_id: string }>("/api/commands/zones", body),
  /** Permanently replace a zone's base capacity (a disruption expires; this doesn't). */
  setZoneCapacity: (zoneId: string, slots: number) =>
    post<{ zone_id: string }>("/api/commands/zones/capacity", { zone_id: zoneId, slots }),
};

/** One hit from the place search — a real business, geocoded. */
export interface PlaceResult {
  name: string;
  address: string;
  lat: number;
  lon: number;
}

export interface RoadRoute {
  km: number;
  minutes: number;
  path: LonLat[];
  /** True = a great-circle guess, not routed geometry: draw it as such. */
  estimated: boolean;
}

export interface NewZoneInput {
  kind: string;
  slots?: number | null;
  weight_kg?: number | null;
  volume_m3?: number | null;
  temp_c?: [number, number] | null;
}

export interface NewFacility {
  name?: string | null;
  lat: number;
  lon: number;
  cold_certified?: boolean;
  hazmat_certified?: boolean;
  zones: NewZoneInput[];
}

export interface NewLane {
  from_facility_id: string;
  to_facility_id: string;
  mode: string;
  km?: number | null;
  minutes?: number | null;
  cost_fixed?: number | null;
  both_directions?: boolean;
}

/** Input for registering a shipment from the UI. */
export interface NewShipment {
  origin_facility_id?: string | null;
  origin_label?: string | null;
  origin_lat?: number | null;
  origin_lon?: number | null;
  sku?: string;
  group?: string;
  quantity?: number;
  uom?: string;
  slots: number;
  weight_kg?: number | null;
  temp_c?: [number, number] | null;
  compat_class?: string | null;
  ready?: string | null;
  deadline?: string | null;
  dwell_days?: number | null;
  /** A→B delivery (§7.9): all three destination fields, or none of them. */
  destination_label?: string | null;
  destination_lat?: number | null;
  destination_lon?: number | null;
  /** Days the customer requires the goods held; 0..365. */
  hold_days?: number;
}
