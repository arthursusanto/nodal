// Browser-driven smoke for the Nodal UI: logs in, measures the map (marker
// placement vs projection, rendered lanes/arcs, endpoint dots), and walks
// every operator flow — the review the log opens with (a wrong-batch commit
// refused, then COMMIT PLAN), focus on a shipment, an A→B delivery's itinerary
// and road legs, single solve + commit, registering a delivery through the form
// (with the place search answered by a canned intercept), OPTIMIZE ALL plan →
// discard → commit, facility disruption + ending it, 3D, every panel toggle,
// what-if, replay, the UI scale — screenshotting each step. It also walks the
// closure story the world ships with (§7.9): cargo a closure trapped (queue
// badge, decision banner, the hub on the map), the dwells a journey books on
// the way and the staging reservations they hold, a closure of its own that
// traps cargo live, and the cancel that clears it.
// Exits 1 if any check fails. Needs the API (with planned shipments) and the
// dev server up; see smoke/README.md.
//
//   NODAL_API_TOKEN=<token> npm run smoke [-- outDir width height dpr baseUrl]

import { chromium } from "playwright";

import { VET_KINDS, VET_SAYS, vetFindings } from "./vet.mjs";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const [, , outArg, widthArg, heightArg, dprArg, urlArg] = process.argv;
const SMOKE_DIR = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.resolve(SMOKE_DIR, outArg || "out");
const WIDTH = Number(widthArg || 1920);
const HEIGHT = Number(heightArg || 1080);
const DPR = Number(dprArg || 1);
const URL_ = urlArg || process.env.NODAL_UI_URL || "http://localhost:5173/";
const TOKEN = process.env.NODAL_API_TOKEN ?? "";

fs.mkdirSync(OUT, { recursive: true });
const report = { console: [], pageErrors: [], failedRequests: [], steps: [], problems: [] };

function note(step, data) {
  report.steps.push({ step, ...data });
  console.log(`## ${step}`, JSON.stringify(data));
}
function problem(text) {
  report.problems.push(text);
  console.log(`!! PROBLEM: ${text}`);
}
async function shot(page, name) {
  await page.screenshot({ path: path.join(OUT, `${name}.png`) });
}

const ARC_LAYERS = [
  "arcs-booked",
  "arcs-planned",
  "arcs-alt",
  "arcs-selected",
  "arcs-road",
  "arcs-road-estimated",
];

// Facility markers only. Endpoint dots (the businesses at the ends of the
// network) and origin diamonds are .map-node too, and neither carries a
// .node-box or a .node-label — every hub query must exclude both.
const HUB = ".map-node:not(.origin-node):not(.endpoint-node)";

async function mapDiagnostics(page) {
  return page.evaluate(
    ({ ARC_LAYERS, HUB }) => {
      const map = window.__nodalMap;
      if (!map) return { error: "no __nodalMap (dev build only)" };
      const rendered = (layers) =>
        map.queryRenderedFeatures({ layers: layers.filter((l) => map.getLayer(l)) }).length;
      const arcs = {};
      for (const l of ARC_LAYERS) arcs[l] = rendered([l]);
      return {
        zoom: Number(map.getZoom().toFixed(2)),
        pitch: Math.round(map.getPitch()),
        rendered: {
          lanes: rendered(["lanes-road", "lanes-sea", "lanes-air"]),
          arcs,
          // Both road layers are the same grade: real geometry when the
          // provider answered, the dashed estimate until it does.
          road: arcs["arcs-road"] + arcs["arcs-road-estimated"],
          zones: rendered(["zone-capacity", "zone-occupancy"]),
        },
        markerCount: document.querySelectorAll(".maplibregl-marker").length,
        hubCount: document.querySelectorAll(HUB).length,
        dotCount: document.querySelectorAll(".map-node.endpoint-node").length,
        customerDots: document.querySelectorAll(".map-node.customer-node").length,
      };
    },
    { ARC_LAYERS, HUB },
  );
}

async function markerDeviation(page, facilities) {
  return page.evaluate((facilities) => {
    const map = window.__nodalMap;
    const crect = map.getContainer().getBoundingClientRect();
    const worldPx = 512 * Math.pow(2, map.getZoom());
    const out = [];
    for (const f of facilities) {
      const el = [...document.querySelectorAll(".maplibregl-marker")].find((m) => {
        const label = m.querySelector(".node-label");
        return label && label.textContent.trim() === f.id;
      });
      if (!el) continue; // hidden by focus, or not in this world
      const br = el.querySelector(".node-box").getBoundingClientRect();
      const p = map.project([f.lon, f.lat]);
      // Markers sit on the world copy nearest the viewport centre; project()
      // may answer for another copy — compare modulo one world width.
      let dx = br.left + br.width / 2 - crect.left - p.x;
      dx = ((dx % worldPx) + worldPx) % worldPx;
      if (dx > worldPx / 2) dx -= worldPx;
      out.push({
        id: f.id,
        dx: Math.round(dx),
        dy: Math.round(br.top + br.height / 2 - crect.top - p.y),
        position: getComputedStyle(el).position,
      });
    }
    return {
      measured: out.length,
      maxDev: Math.max(0, ...out.map((o) => Math.max(Math.abs(o.dx), Math.abs(o.dy)))),
      nonAbsolute: out.filter((o) => o.position !== "absolute").length,
    };
  }, facilities);
}

/* Hubs a fit failed to frame: hidden behind the queue panel, or outside the
   map canvas altogether. A fit's whole job is to put the network in the clear
   window between the panels, so either is a defect — and the fit spans more
   than 360° of longitude on a 1920-wide screen, so MapLibre draws the world
   more than once and a marker can end up on the copy behind the panel rather
   than the one the fit framed. */
async function hubsOutOfSight(page) {
  return page.evaluate((hubSelector) => {
    const queue = document.querySelector(".overlay.left")?.getBoundingClientRect();
    const canvas = document.querySelector(".map-canvas").getBoundingClientRect();
    const underQueue = [];
    const offCanvas = [];
    for (const m of document.querySelectorAll(hubSelector)) {
      const r = m.querySelector(".node-box").getBoundingClientRect();
      const id = m.querySelector(".node-label").textContent;
      if (queue && r.left < queue.right && r.right > queue.left && r.top < queue.bottom && r.bottom > queue.top) {
        underQueue.push(id);
      }
      if (r.left < canvas.left || r.right > canvas.right || r.top < canvas.top || r.bottom > canvas.bottom) {
        offCanvas.push(id);
      }
    }
    return { underQueue, offCanvas };
  }, HUB);
}

async function waitForMapIdle(page, timeout = 30000) {
  await page.waitForFunction(() => window.__nodalMap && window.__nodalMap.loaded(), null, {
    timeout,
  });
  await page.evaluate(
    () =>
      new Promise((resolve) => {
        window.__nodalMap.once("idle", resolve);
        setTimeout(resolve, 6000);
      }),
  );
}

async function waitNotBusy(page) {
  await page.waitForFunction(
    () => {
      const b = document.querySelector(".overlay.left .field-row button.primary");
      return !b || !b.disabled;
    },
    null,
    { timeout: 180000 },
  );
}

/* The itinerary rows on screen, checked against the record the API served.
   The engine leaves lane_id null on exactly the two road miles at the ends of
   a journey, so THAT — never the leg's index — decides which row reads FIRST
   MILE or LAST MILE; a lane leg is named by its mode, road lanes included.
   Only call this for a shipment whose decision is in the log. */
async function itineraryLabels(page, shipmentId) {
  const body = await page.evaluate(async (id) => {
    const token = sessionStorage.getItem("nodal-token");
    const r = await fetch(`/api/decisions/${encodeURIComponent(id)}`, {
      headers: token ? { Authorization: "Bearer " + token } : {},
    });
    return r.ok ? r.json() : null;
  }, shipmentId);
  const legs = body?.record?.chosen?.itinerary?.legs;
  if (!legs) return { error: "the record carries no itinerary" };
  const expected = legs.map((leg, i) =>
    leg.lane_id !== null
      ? leg.kind.toUpperCase()
      : i === legs.length - 1
        ? "LAST MILE"
        : "FIRST MILE",
  );
  const shown = (
    await page.$$eval(".overlay.right .itinerary .leg-kind", (all) =>
      all.map((k) => k.textContent.trim()),
    )
  ).filter((k) => k !== "HOLD" && k !== "DELIVER");
  const laneLegsMislabelled = legs
    .map((leg, i) => ({ leg, i }))
    .filter(({ leg, i }) => leg.lane_id !== null && /^(FIRST|LAST) MILE$/.test(shown[i] ?? ""))
    .map(({ leg, i }) => `${shown[i]} on lane ${leg.lane_id} (${leg.from_label} → ${leg.to_label})`);
  return { expected, shown, laneLegsMislabelled };
}

/* Table cells whose content is wider than the cell that holds it: that is a
   column painting over the one beside it, which is what a dense table does
   when the panel is too narrow for it and nothing lets it scroll. */
async function overflowingCells(page, selector) {
  return page.$$eval(selector, (cells) =>
    cells
      .filter((td) => td.scrollWidth > td.clientWidth + 1)
      .map((td) => `${td.textContent.trim().slice(0, 40)} (${td.scrollWidth}>${td.clientWidth})`),
  );
}

async function vetPanel(page, selector, label, options = {}) {
  const findings = await vetFindings(page, selector, options);

  if (findings.missing) {
    problem(`vet ${label}: ${selector} is not on screen to vet`);
    return findings;
  }
  const summary = { elements: findings.counted };
  for (const kind of VET_KINDS) if (findings[kind].length) summary[kind] = findings[kind];
  note(`vet · ${label}`, summary);
  for (const kind of VET_KINDS) {
    const hits = [...new Set(findings[kind])];
    if (!hits.length) continue;
    problem(
      `vet ${label}: ${hits.length} ${VET_SAYS[kind]} — ${hits.slice(0, 4).join(" | ")}` +
        (hits.length > 4 ? ` | +${hits.length - 4} more` : ""),
    );
  }
  return findings;
}


/* One line per road mile, in every state.

   A delivery's first and last mile are ROAD legs. The lane trace therefore has
   to begin at the facility the goods ENTER the network at and end at the one
   they leave it from — if it starts at the customer's own origin instead, that
   mile is drawn twice: once as the fetched road polyline and once as a straight
   shortcut underneath it, two paths between the same two points.

   Measured two ways, because either alone can pass while the screen is wrong:
     structure — no lane-grade arc (booked/selected/planned) may begin at the
                 shipment's origin or end at its customer when a road mile
                 covers that segment, and each covered mile is drawn by exactly
                 one road feature;
     pixels    — midway along each mile the map must render a road layer and no
                 lane layer, which is what "two lines" actually looks like. */
async function roadMileArcs(page, shipmentId) {
  return page.evaluate(async (id) => {
    const map = window.__nodalMap;
    const arcs = window.__nodalArcs ?? [];
    const token = sessionStorage.getItem("nodal-token");
    const body = await (
      await fetch("/api/map", { headers: token ? { Authorization: "Bearer " + token } : {} })
    ).json();
    const shipment = body.shipments.find((s) => s.id === id);
    if (!shipment?.destination || !shipment.destination_point) return { error: `${id} is not a booked delivery` };
    const facility = new Map(body.facilities.map((f) => [f.id, f]));
    const lane = new Map(body.lanes.map((l) => [l.id, l]));
    const booking = shipment.destination;
    const at = (f) => [f.lon, f.lat];
    const origin = shipment.origin_facility_id
      ? at(facility.get(shipment.origin_facility_id))
      : [shipment.origin_lon, shipment.origin_lat];
    const firstLane = booking.route.length ? lane.get(booking.route[0]) : null;
    const entry = at(firstLane ? facility.get(firstLane.from) : facility.get(booking.facility_id));
    const exit = at(
      booking.exit_facility_id ? facility.get(booking.exit_facility_id) : facility.get(booking.facility_id),
    );
    const customer = [shipment.destination_point.lon, shipment.destination_point.lat];
    // Longitudes may be unwrapped onto another world copy: compare modulo 360.
    const same = (a, b) => {
      let dLon = (a[0] - b[0]) % 360;
      if (dLon > 180) dLon -= 360;
      if (dLon < -180) dLon += 360;
      return Math.abs(dLon) < 0.02 && Math.abs(a[1] - b[1]) < 0.02;
    };
    // This shipment's own arcs only: every feature carries the id it belongs
    // to, so a neighbour's route sharing a loading dock cannot read as a
    // duplicate of this one's.
    const mine = arcs.filter((f) => f.properties.shipment === id);
    const LANE_GRADES = ["booked", "selected", "planned"];
    const laneArcs = mine.filter((f) => LANE_GRADES.includes(f.properties.grade));
    const roadArcs = mine.filter((f) => f.properties.grade === "road");
    const ends = (f) => {
      const c = f.geometry.coordinates;
      return [c[0], c[c.length - 1]];
    };
    const miles = [];
    for (const [what, from, to] of [
      ["first mile", origin, entry],
      ["last mile", exit, customer],
    ]) {
      if (same(from, to)) continue; // nothing to drive: the origin IS the entry
      const drawnBy = roadArcs.filter((f) => {
        const [a, b] = ends(f);
        return (same(a, from) && same(b, to)) || (same(a, to) && same(b, from));
      });
      const doubled = laneArcs.filter((f) => {
        const [a, b] = ends(f);
        return same(a, from) || same(b, from) || same(a, to) || same(b, to);
      });
      // Midway along the STRAIGHT line between the two ends. A duplicate is
      // straight by construction, so it always passes through here — while
      // real road geometry usually does not, which is why what is asserted at
      // this point is the ABSENCE of a lane-grade line, never the presence of
      // the road one (the road mile's own feature is counted structurally).
      const mid = [(from[0] + to[0]) / 2, (from[1] + to[1]) / 2];
      const point = map.project(mid);
      const layers = ["arcs-road", "arcs-road-estimated", "arcs-selected", "arcs-booked", "arcs-planned"].filter(
        (l) => map.getLayer(l),
      );
      const canvas = map.getContainer().getBoundingClientRect();
      const a = map.project(from);
      const b = map.project(to);
      // A mile only a few pixels long has no "midway": its own ends — where the
      // lane trace legitimately begins — are inside the query box.
      const mileLengthPx = Math.round(Math.hypot(a.x - b.x, a.y - b.y));
      const onScreen =
        mileLengthPx > 40 &&
        point.x > 6 &&
        point.y > 6 &&
        point.x < canvas.width - 6 &&
        point.y < canvas.height - 6;
      const box = [
        [point.x - 4, point.y - 4],
        [point.x + 4, point.y + 4],
      ];
      const rendered = onScreen
        ? [...new Set(map.queryRenderedFeatures(box, { layers }).map((f) => f.layer.id))]
        : null;
      miles.push({
        what,
        onScreen,
        mileLengthPx,
        roadFeatures: drawnBy.length,
        // A lane arc ENDING at the entry (or starting at the exit) is the trace
        // doing its job; one touching the ORIGIN or the CUSTOMER is the mile
        // drawn a second time.
        laneArcsAtMileEnds: doubled
          .filter((f) => {
            const [a, b] = ends(f);
            const outer = what === "first mile" ? from : to;
            return same(a, outer) || same(b, outer);
          })
          .map((f) => f.properties.grade),
        renderedMidway: rendered,
      });
    }
    return { id, miles, arcs: arcs.length };
  }, shipmentId);
}

/* One segment of a journey, one line. A committed shipment's decision record
   IS its booking, and a reviewed one's draft IS its plan, so tracing the record
   on top of either lays a second byte-identical line along the same segment:
   the selected halo (line-opacity 0.22) composites to ~0.39 where they overlap,
   and one half of a journey reads heavier than the other for no reason the
   operator can see. Asked twice — structurally, of the arcs exactly as drawn,
   and of what the map renders at each segment's own midpoint. */
async function stackedTraces(page, shipmentId, grade = "selected") {
  return page.evaluate(
    ({ id, grade }) => {
      const map = window.__nodalMap;
      const arcs = (window.__nodalArcs ?? []).filter(
        (f) => f.properties.shipment === id && f.properties.grade === grade,
      );
      const layers = (grade === "selected" ? ["arcs-selected", "arcs-selected-halo"] : ["arcs-planned"])
        .filter((l) => map.getLayer(l));
      const canvas = map.getCanvas();
      const seen = new Map();
      for (const f of arcs) {
        const key = JSON.stringify(f.geometry.coordinates);
        seen.set(key, (seen.get(key) ?? 0) + 1);
      }
      // Halfway ALONG the line, not its middle vertex: for a two-point trace
      // the middle vertex is an end, which the next leg of the same journey
      // legitimately starts from — a midpoint has to be a point only this line
      // passes through.
      const midpoint = (c) => {
        const upto = [];
        let total = 0;
        for (let i = 1; i < c.length; i += 1) {
          total += Math.hypot(c[i][0] - c[i - 1][0], c[i][1] - c[i - 1][1]);
          upto.push(total);
        }
        if (total === 0) return c[0];
        for (let i = 0; i < upto.length; i += 1) {
          if (upto[i] < total / 2) continue;
          const before = i === 0 ? 0 : upto[i - 1];
          const t = upto[i] === before ? 0 : (total / 2 - before) / (upto[i] - before);
          return [
            c[i][0] + (c[i + 1][0] - c[i][0]) * t,
            c[i][1] + (c[i + 1][1] - c[i][1]) * t,
          ];
        }
        return c[c.length - 1];
      };
      const segments = arcs.map((f, index) => {
        const c = f.geometry.coordinates;
        const mid = midpoint(c);
        const p = map.project(mid);
        const box = [
          [p.x - 3, p.y - 3],
          [p.x + 3, p.y + 3],
        ];
        const rendered = {};
        for (const layer of layers) {
          rendered[layer] = map
            .queryRenderedFeatures(box, { layers: [layer] })
            .filter((r) => r.properties.shipment === id).length;
        }
        return {
          index,
          points: c.length,
          onScreen:
            p.x > 6 && p.y > 6 && p.x < canvas.clientWidth - 6 && p.y < canvas.clientHeight - 6,
          rendered,
        };
      });
      return {
        features: arcs.length,
        identical: [...seen.values()].filter((n) => n > 1).length,
        segments,
      };
    },
    { id: shipmentId, grade },
  );
}

/* The map's OWN controls — the FIT / 3D stack and MapLibre's navigation under
   it — are controls like any other, and the same rule holds for them: a click
   at the control's centre must reach the control. They live outside every
   panel, so `vetPanel`'s occlusion rule cannot reach them; this asks the same
   question directly, of each control's own centre, and names what it found so
   a failure says where to go and look. */
async function mapControlsCovered(page) {
  return page.evaluate(() => {
    const out = [];
    const label = (el) =>
      (el.textContent || el.getAttribute("title") || el.className).trim().slice(0, 24);
    for (const selector of [".map-controls .chip", ".maplibregl-ctrl-bottom-right button"]) {
      for (const el of document.querySelectorAll(selector)) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        // Unclickable for reasons of its own comes first: a control with
        // pointer-events:none is invisible to the hit test, so something
        // BEHIND it answers — its own container, usually, which a
        // `hit.contains(el)` test would have read as "the control answered".
        // A disabled control fails the same rule: the click goes nowhere.
        if (getComputedStyle(el).pointerEvents === "none") {
          out.push({ control: label(el), at: [Math.round(r.left), Math.round(r.top)], why: "unclickable (pointer-events: none)" });
          continue;
        }
        if (el.disabled) {
          out.push({ control: label(el), at: [Math.round(r.left), Math.round(r.top)], why: "disabled" });
          continue;
        }
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        // The control itself, or a child of it (its label, its icon), IS the
        // control answering. An ancestor answering is not: that click lands on
        // the container, not on the button.
        if (hit && (hit === el || el.contains(hit))) continue;
        const owner = hit ? hit.closest(".overlay, .popover, .toast, .stepper, .app-header") : null;
        const on = owner ?? hit;
        out.push({
          control: label(el),
          at: [Math.round(r.left), Math.round(r.top)],
          why: on ? `behind ${on.className || on.tagName}` : "at a point nothing answers",
        });
      }
    }
    return out;
  });
}

/** Panel geometry in CSS px, from the root's variables (px or rem). */
async function geometry(page) {
  return page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    const unit = parseFloat(root.fontSize);
    const pxOf = (name) => {
      const v = root.getPropertyValue(name).trim();
      return v.endsWith("px") ? parseFloat(v) : parseFloat(v) * unit;
    };
    return { unit, left: pxOf("--panel-left"), right: pxOf("--panel-right"), gutter: pxOf("--gutter") };
  });
}

const browser = await chromium.launch({
  headless: true,
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const context = await browser.newContext({
  viewport: { width: WIDTH, height: HEIGHT },
  deviceScaleFactor: DPR,
});
const page = await context.newPage();
page.on("console", (msg) => {
  if (["error", "warning"].includes(msg.type())) {
    report.console.push({ type: msg.type(), text: msg.text().slice(0, 400) });
  }
});
page.on("pageerror", (err) => report.pageErrors.push(String(err).slice(0, 400)));
// The harness deliberately provokes two failures (the wrong-token 401, the
// synthetic disrupt 500). Each is allowed ONLY while its window is armed, so
// the same status on the same path outside the window still fails the run.
// The harness provokes three failures itself: the wrong-token 401, the
// synthetic disrupt 500, and the 422 an unparseable what-if earns. Each is
// allowed only while its window is armed, so the same status on the same path
// outside that window still fails the run.
const armed = { login401: true, disrupt500: false, whatif422: false, commit409: false };
page.on("response", (res) => {
  if (res.status() >= 400 && !res.url().includes("tile.openstreetmap.org")) {
    let requestPath = res.url();
    try {
      requestPath = new URL(res.url()).pathname;
    } catch {
      // A URL in some other shape: the path stays the whole string.
    }
    const provoked =
      (armed.login401 && requestPath === "/api/map" && res.status() === 401) ||
      (armed.disrupt500 && requestPath === "/api/commands/disrupt" && res.status() === 500) ||
      (armed.whatif422 && requestPath === "/api/commands/whatif" && res.status() === 422) ||
      (armed.commit409 && requestPath === "/api/commands/optimize" && res.status() === 409);
    report.failedRequests.push({ url: res.url(), status: res.status(), provoked });
  }
});

const hub = (id) =>
  page
    .locator(HUB)
    .filter({ has: page.locator(".node-label", { hasText: new RegExp(`^${id}$`) }) })
    .first();
const popoverId = async () =>
  await page.$eval(".popover .row-title", (t) => t.textContent.trim().split(/\s|·/)[0]).catch(() => null);
const closePopover = async () => {
  if (await page.$(".popover")) await page.click(".popover .panel-header .close");
  await page.waitForTimeout(150);
};
const closeDecision = async () => {
  if (await page.$(".overlay.right")) await page.click(".overlay.right .panel-header .close");
  await page.waitForTimeout(300);
};
const selectedRowId = async () =>
  await page
    .$eval(".overlay.left .row.selected .row-title > span:first-child", (t) => t.textContent.trim())
    .catch(() => null);
const plannedRowCount = async () =>
  page.$$eval(".overlay.left .row .badge.planned", (badges) => badges.length);
/* A planned shipment registered through the + NEW form, from a COORDINATE
   origin. Cargo standing inside a hub can legitimately be solved to the hub it
   already stands in, and then there is no route to draw — a coordinate origin
   always travels, so every flow that asks what the map DREW has something to
   look at. Leaves the queue as it found it (nothing selected). */
const registerPlannedShipment = async (label, lat, lon) => {
  await page.click(".overlay.left .queue-actions button:has-text('+ NEW')");
  await page.waitForSelector(".popover", { timeout: 10000 });
  await page.selectOption(".popover select[aria-label='origin kind']", "coords");
  await page.fill(".popover input[aria-label='origin label']", label);
  await page.fill(".popover input[aria-label='origin latitude']", String(lat));
  await page.fill(".popover input[aria-label='origin longitude']", String(lon));
  await page.click(".popover .panel-footer button:has-text('REGISTER SHIPMENT')");
  await waitNotBusy(page);
  await page.waitForTimeout(2500);
  const id = await selectedRowId();
  await closeDecision();
  return id;
};

try {
  await page.goto(URL_, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("input[type=password]", { timeout: 30000 });
  await shot(page, "00-login");
  await vetPanel(page, ".login-card", "connect · nothing entered");
  if (TOKEN) {
    // A wrong token must be refused at the door.
    await page.fill("input[type=password]", "wrong-token");
    await page.click("button:has-text('CONNECT')");
    await page.waitForTimeout(1200);
    const loginErr = await page.$eval(".login-card .reject-code", (e) => e.textContent).catch(() => null);
    note("wrong-token", { error: loginErr });
    if (!loginErr) problem("wrong token did not surface an error on the connect screen");
    await vetPanel(page, ".login-card", "connect · wrong token refused");
  }
  await page.fill("input[type=password]", TOKEN);
  await page.click("button:has-text('CONNECT')");
  await page.waitForSelector(".overlay.left .row", { timeout: 30000 });
  armed.login401 = false; // logged in: any later 401 is a real defect
  await waitForMapIdle(page);
  await page.waitForTimeout(1500);
  // The dev server must actually proxy /api: a lost proxy answers the SPA's
  // own index.html with a 200 — which the app must refuse, and which this
  // run must name instead of dying on a JSON parse.
  const apiContentType = await page.evaluate(async () => {
    const token = sessionStorage.getItem("nodal-token");
    const r = await fetch("/api/map", { headers: token ? { Authorization: "Bearer " + token } : {} });
    return `${r.status} ${r.headers.get("content-type") || ""}`;
  });
  note("api-proxy", { apiContentType });
  if (!/application\/json/.test(apiContentType)) {
    throw new Error(`the dev server is not proxying /api to Nodal (got ${apiContentType}) — restart it`);
  }
  const world = await page.evaluate(async () => {
    const token = sessionStorage.getItem("nodal-token");
    const r = await fetch("/api/map", { headers: token ? { Authorization: "Bearer " + token } : {} });
    return r.json();
  });
  const geo = await geometry(page);
  note("loaded", { facilities: world.facilities.length, shipments: world.shipments.length, geo });
  // The flows commit plans and register network objects, so a world that has
  // already been driven gives null-click fatals and false problems. Fail fast
  // with the remedy instead.
  if (!world.shipments.some((s) => s.status === "planned") || world.facilities.some((f) => f.id.startsWith("FAC-API-"))) {
    throw new Error(
      "stale world: this DB was already driven (no planned shipments, or smoke-made facilities present) — rebuild it with scripts/make_global_demo.py and restart the API before running the smoke",
    );
  }
  if (await page.$(".overlay.right")) problem("decision panel shown with no selection");
  await vetPanel(page, ".app-header", "header · 100%");
  await vetPanel(page, ".stepper", "stepper · nothing selected");

  // -- the review the log opens with (§7.5) ---------------------------------
  // A dry run DRAFTS its proposal into the log, so a plan outlives the browser
  // that made it: the demo world ends with one pending, and the app has to
  // open IN review — the same review, with the same records, that whoever ran
  // OPTIMIZE ALL was looking at. A reload must land back in it.
  const pending = world.pending_plan;
  // What the pending plan could not place. Committing it books everything
  // else, so these are exactly the shipments still planned afterwards — and
  // exactly the ones a later single SOLVE must not pick.
  let unplacedByPlan = [];
  note("pending-plan-at-load", {
    pending: pending && {
      batch_id: pending.batch_id,
      based_on_seq: pending.based_on_seq,
      head: pending.head,
      assigned: pending.assigned,
      unassigned: pending.unassigned,
      rows: Object.keys(pending.decisions).length,
    },
  });
  if (!pending) {
    problem("the world serves no pending plan: the plan-review flow is untested");
  } else {
    const planState = async (label) => {
      const state = await page.evaluate(() => ({
        commitLabel:
          document.querySelector(".overlay.left .plan-actions button.primary")?.textContent.trim() ?? null,
        hasDiscard: [...document.querySelectorAll(".overlay.left .plan-actions button")].some(
          (b) => b.textContent.trim() === "DISCARD",
        ),
        planTags: document.querySelectorAll(".plan-tag").length,
        chip: document.querySelector(".header-right .plan-chip")?.textContent.replace(/\s+/g, " ").trim() ?? null,
      }));
      const arcs = (await mapDiagnostics(page)).rendered.arcs["arcs-planned"];
      note(`plan-review · ${label}`, { ...state, plannedArcs: arcs });
      return { ...state, plannedArcs: arcs };
    };
    await page.waitForSelector(".overlay.left .plan-actions", { timeout: 20000 }).catch(() => null);
    const review = await planState("at load");
    const expectLabel = `COMMIT PLAN (${pending.assigned}/${pending.assigned + pending.unassigned})`;
    if (review.commitLabel !== expectLabel) {
      problem(`the app did not open in plan review: the action reads "${review.commitLabel}", expected "${expectLabel}"`);
    }
    if (!review.hasDiscard) problem("plan review offers no DISCARD");
    if (review.planTags < pending.assigned) {
      problem(`${review.planTags} rows show where the pending plan sends them, for ${pending.assigned} assigned`);
    }
    if (!review.plannedArcs) problem("the pending plan draws no planned arcs on the map");
    if (!review.chip) problem("the header does not chip the uncommitted plan");
    // A shipment the draft could not place says WHY on its own row — the cause
    // the record gives, not just that there was one.
    const unplaceable = Object.entries(pending.decisions).filter(([, d]) => d.facility_id === null);
    unplacedByPlan = unplaceable.map(([sid]) => sid);
    note("plan-review-unassigned", { unplaceable: unplaceable.map(([sid, d]) => `${sid}: ${d.unassigned_reasons.join("/")}`) });
    if (!unplaceable.length) problem("the pending plan places everything: the unassignable row is untested");
    for (const [sid, decision] of unplaceable) {
      const row = page.locator(".overlay.left .row").filter({ hasText: sid }).first();
      await row.scrollIntoViewIfNeeded();
      const text = ((await row.textContent()) ?? "").replace(/\s+/g, " ");
      const named = decision.unassigned_reasons.filter((reason) => text.includes(reason));
      note("plan-review-row", { sid, reasons: decision.unassigned_reasons, named, text: text.slice(0, 160) });
      if (!/NO FEASIBLE/.test(text)) problem(`${sid} is unassignable in the pending plan but its row does not say so: ${text.slice(0, 140)}`);
      if (decision.unassigned_reasons.length && !named.length) {
        problem(`${sid}'s row does not name why the plan could not place it (${decision.unassigned_reasons.join(", ")}): ${text.slice(0, 140)}`);
      }
    }
    // Selecting a row in review shows the DRAFT decision, not a stale booking.
    const firstAssigned = Object.entries(pending.decisions).find(([, d]) => d.facility_id !== null);
    if (firstAssigned) {
      const row = page.locator(".overlay.left .row").filter({ hasText: firstAssigned[0] }).first();
      await row.scrollIntoViewIfNeeded();
      await row.click();
      await page.waitForTimeout(1200);
      const head = await page.$eval(".overlay.right .panel-header", (h) => h.textContent).catch(() => "NO PANEL");
      note("plan-review-decision", { id: firstAssigned[0], head: head.slice(0, 80) });
      if (!head.includes("BATCH PLAN")) {
        problem(`selecting ${firstAssigned[0]} in the pending review did not show its batch decision: ${head.slice(0, 80)}`);
      }
      await vetPanel(page, ".overlay.right", `decision · pending-plan draft record (${firstAssigned[0]})`);
      await closeDecision();
    }
    await vetPanel(page, ".overlay.left", "queue · plan review the log opened with");
    await shot(page, "28-plan-review-at-load");

    // A reload is not an escape from a review that lives in the log.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector(".overlay.left .row", { timeout: 30000 });
    await waitForMapIdle(page);
    await page.waitForTimeout(2000);
    const afterReload = await planState("after a reload");
    if (afterReload.commitLabel !== expectLabel) {
      problem(`a reload left the review: the action reads "${afterReload.commitLabel}", expected "${expectLabel}"`);
    }
    await shot(page, "28b-plan-review-after-reload");

    // A commit names the plan it means, not just the head it was solved
    // against: a COMMIT PLAN carrying the wrong batch id must be refused, and
    // the refusal must leave the review exactly where it was — nothing booked,
    // the plan still pending on the server, the operator told why.
    armed.commit409 = true; // this ONE refusal is the point of the check
    await page.route("**/api/commands/optimize", async (route) => {
      const body = JSON.parse(route.request().postData() ?? "{}");
      if (!body.commit) return route.continue();
      await route.continue({ postData: JSON.stringify({ ...body, batch_id: "BATCH-NOT-PENDING" }) });
    });
    await page.click(".overlay.left .plan-actions button.primary");
    await waitNotBusy(page);
    await page.waitForTimeout(3000);
    const refusal = await page.evaluate(async () => {
      const token = sessionStorage.getItem("nodal-token");
      const map = await (await fetch("/api/map", { headers: token ? { Authorization: "Bearer " + token } : {} })).json();
      return {
        toast: document.querySelector(".toast")?.textContent.replace(/\s+/g, " ").trim() ?? null,
        pending: map.pending_plan ? map.pending_plan.batch_id : null,
      };
    });
    note("commit-wrong-batch-id", refusal);
    if (!refusal.toast || !/not the pending plan/i.test(refusal.toast)) {
      problem(`a commit naming the wrong plan was not refused in words the operator can read: ${refusal.toast}`);
    }
    if (refusal.pending !== pending.batch_id) {
      problem(`a refused commit changed what is pending: ${refusal.pending}, expected ${pending.batch_id}`);
    }
    await page.unroute("**/api/commands/optimize");
    armed.commit409 = false; // any later 409 on this path is a real defect
    // The refusal booked nothing, so the review it refused is still the head's:
    // the app must be holding it again by now.
    await page.waitForFunction(
      (label) =>
        document.querySelector(".overlay.left .plan-actions button.primary")?.textContent.trim() === label,
      expectLabel,
      { timeout: 20000 },
    ).catch(() => null);
    const afterRefusal = await planState("after a refused commit");
    if (afterRefusal.commitLabel !== expectLabel) {
      problem(`a refused commit lost the review: the action reads "${afterRefusal.commitLabel}", expected "${expectLabel}"`);
    }
    await shot(page, "28c-plan-review-after-refused-commit");

    // COMMIT PLAN: the headline of the whole product. What the operator
    // reviewed is what gets booked — every assignable shipment at once — and
    // the plan stops being pending the moment it is.
    const planTotal = pending.assigned + pending.unassigned;
    await page.click(".overlay.left .plan-actions button.primary");
    await waitNotBusy(page);
    await page.waitForTimeout(1500);
    // Read the confirmation FIRST: notices fade on a 7s timer, and the map
    // settling below can take longer than that on its own.
    const commitToast = await page
      .$eval(".toast", (t) => t.textContent.replace(/\s+/g, " ").trim())
      .catch(() => null);
    await page.waitForTimeout(1500);
    await waitForMapIdle(page, 15000);
    const committed = await page.evaluate(async () => {
      const token = sessionStorage.getItem("nodal-token");
      const map = await (await fetch("/api/map", { headers: token ? { Authorization: "Bearer " + token } : {} })).json();
      return {
        pending: map.pending_plan,
        stillPlanned: map.shipments.filter((s) => s.status === "planned").map((s) => s.id),
        booked: map.shipments.filter((s) => s.destination).length,
        rows: document.querySelectorAll(".overlay.left .row .badge.planned").length,
        planTags: document.querySelectorAll(".plan-tag").length,
        queueActionsBack: !!document.querySelector(".overlay.left .queue-actions"),
        queueAction:
          document.querySelector(".overlay.left .queue-actions button.primary")?.textContent.trim() ?? null,
      };
    });
    const committedArcs = (await mapDiagnostics(page)).rendered.arcs;
    note("plan-committed-at-load", { commitToast, ...committed, arcs: committedArcs });
    if (!commitToast || !commitToast.includes(`plan committed: ${pending.assigned} of ${planTotal}`)) {
      problem(`COMMIT PLAN did not confirm what it booked: ${commitToast}`);
    }
    if (committed.pending) problem("the committed plan is still pending on the server");
    if (committed.stillPlanned.length !== pending.unassigned) {
      problem(
        `${committed.stillPlanned.length} shipments are still planned after COMMIT PLAN ` +
          `(${committed.stillPlanned.join(",")}), expected the ${pending.unassigned} the plan could not place`,
      );
    }
    if (committed.rows !== committed.stillPlanned.length) {
      problem(`the queue shows ${committed.rows} planned rows for ${committed.stillPlanned.length} planned shipments`);
    }
    if (committed.planTags) problem(`queue rows still tag a plan after COMMIT PLAN (${committed.planTags})`);
    if (!committed.queueActionsBack) problem("the queue kept its plan actions after COMMIT PLAN");
    // The proposal was dashed; what it booked is drawn solid.
    if (committedArcs["arcs-planned"]) problem("planned arcs still drawn after COMMIT PLAN");
    if (!committedArcs["arcs-booked"]) problem("COMMIT PLAN booked routes but the map draws none");
    await shot(page, "28d-plan-committed-at-load");
    await vetPanel(page, ".overlay.left", "queue · the review committed");
    if (await page.$(".toast")) await vetPanel(page, ".toast", "toast · plan committed");
  }

  // -- the closure story this world ships with (§7.9) -----------------------
  // A hub is SHUT when an active facility_closed disruption names it: the
  // summary's `open` flag is the standing state, not the disruption, so every
  // "is this facility usable" question here goes through the disruption log.
  const closedFacilities = new Set(
    world.disruptions
      .filter((d) => d.status === "active" && d.kind === "facility_closed")
      .map((d) => d.target_id),
  );
  const trappedCargo = world.shipments.filter((s) => s.trapped);
  const bookedStops = world.shipments.flatMap((s) =>
    (s.destination?.stops ?? []).map((stop) => ({ shipment: s.id, ...stop })),
  );
  const allocatedAt = (facilityId) =>
    world.shipments.filter((s) => s.status === "allocated" && s.origin_facility_id === facilityId);
  // The hub the harness closes itself, later. Picked BY ID, never as "the first
  // marker in the DOM": that pick can land on the hub the world already shut,
  // and the flow would then issue a closure against a closed facility. Prefer
  // one with cargo standing on it, so the trap path is the one exercised.
  const openFacilities = world.facilities.filter((f) => f.open && !closedFacilities.has(f.id));
  const disruptTarget =
    openFacilities.find((f) => allocatedAt(f.id).length > 0) ?? openFacilities[0] ?? null;
  note("closure-story", {
    closed: [...closedFacilities],
    trapped: trappedCargo.map((s) => `${s.id}@${s.origin_facility_id}`),
    dwells: bookedStops.length,
    transitDwells: bookedStops.filter((s) => s.role === "transit").length,
    disruptTarget: disruptTarget?.id ?? null,
    disruptWouldTrap: disruptTarget ? allocatedAt(disruptTarget.id).map((s) => s.id) : [],
  });
  if (!closedFacilities.size) problem("no facility is shut in this world: the closure story is untested");
  if (!trappedCargo.length) problem("no cargo is trapped in this world: the closure story is untested");
  if (!disruptTarget) throw new Error("no open facility to close: the disruption flow cannot run");
  if (closedFacilities.has(disruptTarget.id)) {
    problem(`the disruption flow would close ${disruptTarget.id}, which is already shut`);
  }

  // Rows must state origin and destination explicitly, in readable times.
  const firstRow = await page.$eval(".overlay.left .row", (r) => r.textContent);
  note("first-row", { text: firstRow.slice(0, 160) });
  if (!/FROM/.test(firstRow) || !/TO/.test(firstRow)) problem("queue row lacks FROM/TO");
  if (/\d{4}-\d{2}-\d{2}T/.test(firstRow)) problem("queue row still shows raw ISO timestamps");

  // Trapped cargo, before anything is selected: the queue row carries the
  // alarm badge and says where the goods stand, and the hub they stand at says
  // so too — an operator reads it off either surface.
  for (const stuck of trappedCargo) {
    const row = page.locator(".overlay.left .row").filter({ hasText: stuck.id }).first();
    await row.scrollIntoViewIfNeeded();
    const rowText = (await row.textContent()) ?? "";
    const badges = await row.locator(".badge.trapped").count();
    const lines = row.locator(".trapped-line");
    const line = (await lines.count()) ? ((await lines.first().textContent()) ?? "") : "";
    const hubTrapped = await page.evaluate(
      ([id, hubSelector]) => {
        const el = [...document.querySelectorAll(hubSelector)].find(
          (m) => m.querySelector(".node-label")?.textContent.trim() === id,
        );
        if (!el) return { marked: false, onScreen: false };
        const canvas = document.querySelector(".map-canvas").getBoundingClientRect();
        const r = el.querySelector(".node-box").getBoundingClientRect();
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        return {
          marked: el.classList.contains("trapped"),
          onScreen: cx > canvas.left && cx < canvas.right && cy > canvas.top && cy < canvas.bottom,
        };
      },
      [stuck.origin_facility_id, HUB],
    );
    note("trapped-at-load", { id: stuck.id, at: stuck.origin_facility_id, badges, line: line.trim(), hubTrapped });
    if (!badges) problem(`${stuck.id} is trapped but its queue row carries no TRAPPED badge`);
    if (!line.includes(stuck.origin_facility_id)) {
      problem(`${stuck.id}'s trapped line does not name ${stuck.origin_facility_id}: ${line.trim()}`);
    }
    if (!/manual clearing required/i.test(line)) {
      problem(`${stuck.id}'s trapped line does not say the clearing is manual: ${line.trim()}`);
    }
    if (!hubTrapped.marked) problem(`the hub ${stuck.origin_facility_id} does not show it holds trapped cargo`);
    if (!hubTrapped.onScreen) {
      problem(`the hub holding trapped cargo (${stuck.origin_facility_id}) is off screen at the initial fit`);
    }
    if (/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(rowText)) problem(`${stuck.id}'s row shows raw ISO timestamps`);
  }
  let diag = await mapDiagnostics(page);
  note("map-initial", diag);
  if (!diag.rendered.arcs["arcs-booked"]) problem("no booked arcs rendered at initial view");
  if (!diag.rendered.lanes) problem("no lanes rendered at initial view");
  // Nothing is selected here, so the road miles are unfetched: every booked
  // delivery must still connect its customer dot to the network with a dashed
  // great-circle connector, or the dot floats on its own.
  if (world.shipments.some((s) => s.destination_point && s.destination)) {
    if (!diag.rendered.arcs["arcs-road-estimated"]) {
      problem("booked deliveries draw no last-mile connector with nothing selected — customer dots float unattached");
    }
    if (diag.rendered.arcs["arcs-road"]) {
      problem("road geometry was fetched with nothing selected (the per-selection cost control is gone)");
    }
  }
  if (diag.hubCount !== world.facilities.length) problem(`expected every hub on the map, got ${diag.hubCount}`);
  let dev = await markerDeviation(page, world.facilities);
  note("marker-deviation-initial", dev);
  if (dev.maxDev > 1 || dev.nonAbsolute) problem(`marker placement: ${JSON.stringify(dev)}`);
  const framed = await hubsOutOfSight(page);
  note("hubs-at-initial-fit", framed);
  if (framed.underQueue.length) {
    problem(`facility markers under the queue panel at initial fit: ${framed.underQueue.join(",")}`);
  }
  if (framed.offCanvas.length) {
    problem(`facility markers outside the map canvas at initial fit: ${framed.offCanvas.join(",")}`);
  }
  // No hub may sit under the reserved decision-panel strip or the attribution.
  const hitTest = await page.evaluate(({ right, gutter, HUB }) => {
    const underRight = [];
    const rightEdge = window.innerWidth - (right + gutter);
    for (const m of document.querySelectorAll(HUB)) {
      const r = m.querySelector(".node-box").getBoundingClientRect();
      if (r.left + r.width / 2 > rightEdge) underRight.push(m.querySelector(".node-label").textContent);
    }
    const attrib = document.querySelector(".maplibregl-ctrl-bottom-left")?.getBoundingClientRect();
    const queue = document.querySelector(".overlay.left")?.getBoundingClientRect();
    const attribOverQueue =
      attrib && queue && attrib.left < queue.right && attrib.right > queue.left && attrib.top < queue.bottom;
    return { underRight, attribOverQueue: !!attribOverQueue };
  }, { ...geo, HUB });
  note("hit-test", hitTest);
  if (hitTest.underRight.length) problem(`hubs in the decision-panel strip at fit: ${hitTest.underRight.join(",")}`);
  if (hitTest.attribOverQueue) problem("attribution overlaps the queue panel");
  // Endpoint dots: the businesses at the ends of the network. One marker per
  // business — several shipments sharing a site must share its dot, so no two
  // dots may land on the same point.
  const dots = await page.evaluate(() => {
    const out = [];
    for (const el of document.querySelectorAll(".map-node.endpoint-node")) {
      const r = el.querySelector(".endpoint-dot").getBoundingClientRect();
      out.push({
        title: el.title,
        customer: el.classList.contains("customer-node"),
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
      });
    }
    return out;
  });
  const coincident = [];
  for (let i = 0; i < dots.length; i++) {
    for (let j = i + 1; j < dots.length; j++) {
      const d = Math.hypot(dots[i].x - dots[j].x, dots[i].y - dots[j].y);
      if (d < 1) coincident.push(`${dots[i].title} ~ ${dots[j].title} (${d.toFixed(2)}px)`);
    }
  }
  const customerPoints = new Set(
    world.shipments.filter((s) => s.destination_point).map((s) => s.destination_point.label),
  );
  note("endpoint-dots", {
    dots: dots.length,
    customers: dots.filter((d) => d.customer).length,
    customerPointsInWorld: customerPoints.size,
    coincident,
  });
  if (!dots.length) problem("no endpoint dots on the map (booked origins and delivery customers)");
  if (!dots.some((d) => d.customer)) problem("no customer dots on the map, though the world has deliveries");
  if (coincident.length) problem(`endpoint dots not deduped: ${coincident.join("; ")}`);
  // Every customer point must be inside the initial fit — the frame is the
  // whole network, dots included.
  const dotsOffFrame = await page.evaluate(() => {
    const canvas = document.querySelector(".map-canvas").getBoundingClientRect();
    const out = [];
    for (const el of document.querySelectorAll(".map-node.customer-node")) {
      const r = el.querySelector(".endpoint-dot").getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      if (cx < canvas.left || cx > canvas.right || cy < canvas.top || cy > canvas.bottom) {
        out.push(el.title);
      }
    }
    return out;
  });
  note("customer-dots-in-frame", { off: dotsOffFrame });
  if (dotsOffFrame.length) problem(`customer points outside the initial fit: ${dotsOffFrame.join("; ")}`);
  // A dot standing next to a hub must never take that hub's click.
  const closest = await page.evaluate((HUB) => {
    const dotRects = [...document.querySelectorAll(".map-node.endpoint-node .endpoint-dot")].map(
      (d) => d.getBoundingClientRect(),
    );
    let best = null;
    for (const m of document.querySelectorAll(HUB)) {
      const r = m.querySelector(".node-box").getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      for (const d of dotRects) {
        const dist = Math.hypot(d.left + d.width / 2 - cx, d.top + d.height / 2 - cy);
        if (!best || dist < best.dist) best = { id: m.querySelector(".node-label").textContent, dist };
      }
    }
    return best;
  }, HUB);
  if (closest) {
    const box = await hub(closest.id).locator(".node-box").boundingBox();
    if (box) {
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      await page.waitForTimeout(400);
      const opened = await popoverId();
      note("hub-nearest-a-dot", { hub: closest.id, dotPx: Math.round(closest.dist), opened });
      if (opened !== closest.id) {
        problem(`a dot ${Math.round(closest.dist)}px from ${closest.id} stole its click (opened ${opened})`);
      }
      await closePopover();
      await closeDecision();
    }
  }
  // Every hub must open ITSELF when its square is really clicked — neighbours
  // overlapping at continental zoom included (the arbitration decides by
  // distance, so DOM stacking is not the test; the popover is).
  const hubFailures = [];
  for (const f of world.facilities) {
    const b = await hub(f.id).locator(".node-box").boundingBox();
    if (!b) continue;
    await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
    await page.waitForTimeout(350);
    const got = await popoverId();
    if (got !== f.id) hubFailures.push(`${f.id} -> ${got}`);
    await closePopover();
    await closeDecision();
  }
  note("hub-clicks", { failures: hubFailures });
  if (hubFailures.length) problem(`hub squares not clickable (click lands elsewhere): ${hubFailures.join("; ")}`);
  // Every planned origin diamond must select ITS shipment when clicked at its
  // visual centre — unless that centre lies inside a hub's drawn square, where
  // the square (the larger visible affordance) wins by design.
  const diamondFailures = [];
  for (const diamond of await page.$$(".map-node.origin-node")) {
    const id = ((await diamond.getAttribute("title")) || "").split(" ")[0];
    const b = await diamond.boundingBox();
    if (!b) continue;
    const insideHubSquare = await page.evaluate(
      ([x, y, hubSelector]) =>
        [...document.querySelectorAll(`${hubSelector} .node-box`)].some((box) => {
          const r = box.getBoundingClientRect();
          return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
        }),
      [b.x + b.width / 2, b.y + b.height / 2, HUB],
    );
    if (insideHubSquare) continue;
    await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
    await page.waitForTimeout(500);
    const selectedId = await selectedRowId();
    const popover = !!(await page.$(".popover"));
    if (selectedId !== id || popover) diamondFailures.push(`${id}: selected=${selectedId} popover=${popover}`);
    await closePopover();
    await closeDecision();
  }
  note("diamond-clicks", { failures: diamondFailures });
  if (diamondFailures.length) problem(`origin diamonds not clickable: ${diamondFailures.join("; ")}`);

  // Hubs drawn on a WRAPPED world copy (after an ordinary pan) must still open themselves.
  const wrapFailures = [];
  await page.evaluate(() => window.__nodalMap.jumpTo({ center: [-110, 20], zoom: 2.2 }));
  await page.waitForTimeout(800);
  for (const id of ["SYD", "NRT", "SHA", "ICN", "LAX", "SCL"]) {
    const b = await hub(id).locator(".node-box").boundingBox();
    // Skip hubs off-screen on this copy or under the queue panel.
    if (!b || b.x < geo.left + 2 * geo.gutter || b.x > WIDTH - 60) continue;
    await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
    await page.waitForTimeout(400);
    const got = await popoverId();
    if (got !== id) wrapFailures.push(`${id} -> ${got}`);
    await closePopover();
    await closeDecision();
  }
  note("wrapped-copy-clicks", { failures: wrapFailures });
  if (wrapFailures.length) problem(`hub clicks on a wrapped world copy misfire: ${wrapFailures.join("; ")}`);
  await page.click(".map-controls .chip:has-text('FIT')");
  await page.waitForTimeout(600);
  // FIT after a pan must frame the network exactly as the first fit did. The
  // camera comes back to the same centre and zoom on its own; the markers do
  // not, because at this zoom the world is drawn more than once and MapLibre
  // keeps each marker on the copy nearest where it was last drawn. Unless the
  // fit re-homes them, the Pacific hubs stay on the copy the pan left them on —
  // a few dozen pixels in, behind the queue panel, or off the canvas — and the
  // hub holding the trapped cargo goes with them.
  const afterRefit = await hubsOutOfSight(page);
  // Several shipments can be stranded at the same hub: name it once.
  const trappedHubsHidden = [...new Set(trappedCargo.map((s) => s.origin_facility_id))].filter(
    (id) => afterRefit.underQueue.includes(id) || afterRefit.offCanvas.includes(id),
  );
  note("hubs-after-a-refit", { ...afterRefit, trappedHubsHidden });
  if (afterRefit.underQueue.length) {
    problem(`facility markers under the queue panel after a pan + FIT: ${afterRefit.underQueue.join(",")}`);
  }
  if (afterRefit.offCanvas.length) {
    problem(`facility markers outside the map canvas after a pan + FIT: ${afterRefit.offCanvas.join(",")}`);
  }
  if (trappedHubsHidden.length) {
    problem(`the hub holding trapped cargo is out of sight after a pan + FIT: ${trappedHubsHidden.join(",")}`);
  }
  // Keyboard activation goes to the focused marker itself.
  await hub("LHR").focus();
  await page.keyboard.press("Enter");
  await page.waitForTimeout(400);
  const kbd = await popoverId();
  note("keyboard-activation", { popover: kbd });
  if (kbd !== "LHR") problem(`Enter on the focused LHR hub opened ${kbd}`);
  await closePopover();
  await shot(page, "01-loaded");
  await vetPanel(page, ".overlay.left", "queue · loaded, nothing selected");

  // Focus mode: selecting a shipment leaves only ITS nodes on the map and
  // fits them; closing the panel brings everything (and the camera) back.
  const zoomBefore = diag.zoom;
  const bookedRow = await page.$(".overlay.left .row:has(.badge.allocated)");
  await bookedRow.click();
  await page.waitForSelector(".overlay.right .winner", { timeout: 20000 }).catch(() => null);
  await waitForMapIdle(page, 15000);
  await page.waitForTimeout(500);
  const header = await page.$eval(".overlay.right .panel-header", (h) => h.textContent).catch(() => "NO PANEL");
  const routeBlock = await page.$eval(".overlay.right .route-block", (r) => r.textContent).catch(() => "");
  diag = await mapDiagnostics(page);
  note("booked-selection", { header, hubCount: diag.hubCount, zoom: diag.zoom, selectedArcs: diag.rendered.arcs["arcs-selected"], booked: diag.rendered.arcs["arcs-booked"] });
  if (!header.includes("COMMITTED")) problem(`booked selection did not load its committed record: ${header}`);
  if (!diag.rendered.arcs["arcs-selected"]) problem("booked selection shows no selected arc");
  if (diag.hubCount >= world.facilities.length) problem("focus mode still shows every hub");
  if (diag.rendered.arcs["arcs-booked"]) problem("focus mode still shows other shipments' booked arcs");
  if (!/FROM/.test(routeBlock) || !/TO/.test(routeBlock)) problem("decision panel lacks FROM/TO");
  // A stored shipment states its ARRIVE; a delivery states the whole journey
  // in its itinerary instead (where the arrival is the HOLD, not the end).
  const whenShown = /ARRIVE/.test(routeBlock) || (await page.$(".overlay.right .itinerary")) !== null;
  if (!whenShown) problem("decision panel states neither an arrival nor an itinerary");
  const closeBox = await (await page.$(".overlay.right .panel-header .close")).boundingBox();
  if (closeBox.height < 32 || closeBox.width < 60) problem(`decision close button too small: ${JSON.stringify(closeBox)}`);
  await shot(page, "02-focus");
  await vetPanel(page, ".overlay.right", "decision · allocated, focus mode");
  await vetPanel(page, ".stepper", "stepper · committed record");
  await closeDecision();
  await page.waitForTimeout(600);
  diag = await mapDiagnostics(page);
  note("after-close", { hubCount: diag.hubCount, zoom: diag.zoom });
  if (diag.hubCount !== world.facilities.length) problem("closing the selection did not restore every hub");
  if (Math.abs(diag.zoom - zoomBefore) > 0.01) problem(`closing the selection did not restore the camera (${zoomBefore} -> ${diag.zoom})`);
  // No raw ISO timestamp anywhere in the decision panel (rejections included).
  await bookedRow.click();
  await page.waitForSelector(".overlay.right .winner", { timeout: 20000 }).catch(() => null);
  const panelText = await page.$eval(".overlay.right", (p) => p.textContent);
  const isoLeaks = (panelText.match(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/g) || []).length;
  note("decision-panel-iso", { isoLeaks });
  if (isoLeaks) problem(`decision panel shows ${isoLeaks} raw ISO timestamps`);
  // 3D, then a selection: the map flattens AND the button follows.
  await page.click(".map-controls .chip:has-text('3D')");
  await page.waitForTimeout(1200);
  await closeDecision();
  await (await page.$(".overlay.left .row:has(.badge.allocated)")).click();
  await page.waitForTimeout(800);
  const chipAfterSelect = await page.$eval(".map-controls .chip:nth-child(2)", (c) => c.textContent.trim());
  const pitchAfterSelect = await page.evaluate(() => Math.round(window.__nodalMap.getPitch()));
  note("3d-then-select", { chipAfterSelect, pitchAfterSelect });
  if (pitchAfterSelect === 0 && chipAfterSelect !== "3D") problem("3D/2D button desynced from the camera after selecting");
  // Hiding the queue with a selection open ends the focus too.
  await page.click(".tile-btn:has-text('QUEUE')");
  await page.waitForTimeout(600);
  diag = await mapDiagnostics(page);
  note("queue-hidden-with-selection", { hubCount: diag.hubCount });
  if (diag.hubCount !== world.facilities.length) problem("hiding the queue left the map focused with no way out");
  await page.click(".tile-btn:has-text('QUEUE')");
  await page.waitForTimeout(600);
  // Selecting a planned shipment before SOLVE must not blank the network.
  await (await page.$(".overlay.left .row:has(.badge.planned)")).click();
  await page.waitForTimeout(800);
  diag = await mapDiagnostics(page);
  note("planned-before-solve", { hubCount: diag.hubCount, zoom: diag.zoom });
  if (diag.hubCount !== world.facilities.length) problem("selecting an unsolved planned shipment hid the network");
  await vetPanel(page, ".overlay.right", "decision · planned, nothing solved yet");
  await vetPanel(page, ".stepper", "stepper · planned, nothing solved yet");
  await closeDecision();

  // The trapped shipment's panel: the alarm stands ABOVE everything the
  // decision says (none of it can be executed), and the one clearing path the
  // system offers — CANCEL SHIPMENT — is right there under it.
  if (trappedCargo.length) {
    const stuck = trappedCargo[0];
    const row = page.locator(".overlay.left .row").filter({ hasText: stuck.id }).first();
    await row.scrollIntoViewIfNeeded();
    await row.click();
    await page.waitForSelector(".overlay.right .trapped-banner", { timeout: 20000 }).catch(() => null);
    const banner = await page.$(".overlay.right .trapped-banner");
    const bannerText = banner ? ((await banner.textContent()) ?? "") : "";
    const placement = await page.evaluate(() => {
      const b = document.querySelector(".overlay.right .trapped-banner");
      const r = document.querySelector(".overlay.right .route-block");
      if (!b || !r) return null;
      return {
        above: b.getBoundingClientRect().bottom <= r.getBoundingClientRect().top + 1,
        beforeInDom: !!(b.compareDocumentPosition(r) & Node.DOCUMENT_POSITION_FOLLOWING),
      };
    });
    const cancel = page
      .locator(".overlay.right .shipment-actions button")
      .filter({ hasText: "CANCEL SHIPMENT" });
    const cancelReady = (await cancel.count()) > 0 && (await cancel.first().isEnabled());
    note("trapped-banner", { id: stuck.id, placement, cancelReady, text: bannerText.slice(0, 140) });
    if (!banner) problem(`${stuck.id} is trapped but its decision panel shows no banner`);
    else {
      if (!bannerText.includes(stuck.origin_facility_id)) {
        problem(`the trapped banner does not name ${stuck.origin_facility_id}: ${bannerText.slice(0, 140)}`);
      }
      if (!/manual clearing required/i.test(bannerText)) {
        problem(`the trapped banner does not say the clearing is manual: ${bannerText.slice(0, 140)}`);
      }
      if (/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(bannerText)) problem("the trapped banner shows raw ISO timestamps");
      if (!placement?.above || !placement?.beforeInDom) {
        problem(`the trapped banner is not above the route block: ${JSON.stringify(placement)}`);
      }
    }
    if (!cancelReady) problem(`${stuck.id} is trapped but its panel offers no usable CANCEL SHIPMENT`);
    await shot(page, "23-trapped-banner");
    await vetPanel(page, ".overlay.right", `decision · trapped ${stuck.id}`);
    await vetPanel(page, ".overlay.left", `queue · trapped ${stuck.id} selected`);
    await closeDecision();
    await page.waitForTimeout(400);
  }

  // -- an A→B delivery: itinerary, road legs, the customer point ------------
  // Prefer one that leaves the network again from a DIFFERENT facility than it
  // was held at: then the outbound half is a route of its own on the map.
  const deliveries = world.shipments.filter(
    (s) => s.destination_point && s.destination && !s.origin_facility_id,
  );
  const delivery =
    deliveries.find((s) => s.destination.outbound_route.length > 0) ?? deliveries[0];
  if (!delivery) {
    problem("the world has no booked delivery from a coordinate origin to check");
  } else {
    const row = page.locator(".overlay.left .row").filter({ hasText: delivery.id }).first();
    await row.scrollIntoViewIfNeeded();
    const rowText = (await row.textContent()) ?? "";
    note("delivery-row", { id: delivery.id, text: rowText.slice(0, 200) });
    // The row's TO is the CUSTOMER, with the holding facility as via.
    if (!rowText.includes(delivery.destination_point.label)) {
      problem(`the delivery row does not name its customer: ${rowText.slice(0, 160)}`);
    }
    if (!rowText.includes(`via ${delivery.destination.facility_id}`)) {
      problem(`the delivery row does not name its holding facility as "via": ${rowText.slice(0, 160)}`);
    }
    await row.click();
    await page.waitForSelector(".overlay.right .itinerary", { timeout: 20000 });
    await waitForMapIdle(page, 20000);
    // The road miles are fetched per selection: give the provider its answer
    // and the redraw that follows it.
    await page.waitForTimeout(2500);
    const kinds = await page.$$eval(".overlay.right .itinerary .leg-kind", (all) =>
      all.map((k) => k.textContent.trim()),
    );
    const itinText = await page.$eval(".overlay.right .itinerary", (e) => e.textContent);
    const routeBlockText = await page.$eval(".overlay.right .route-block", (e) => e.textContent);
    note("delivery-itinerary", { id: delivery.id, kinds });
    for (const need of ["FIRST MILE", "HOLD", "LAST MILE", "DELIVER"]) {
      if (!kinds.includes(need)) problem(`${delivery.id}'s itinerary has no ${need} row (${kinds.join(", ")})`);
    }
    // Every row named against the record the engine wrote: a lane leg must
    // never read as a mile just because of where it falls in the list.
    const labels = await itineraryLabels(page, delivery.id);
    note("delivery-itinerary-labels", { id: delivery.id, ...labels });
    if (labels.error) problem(`${delivery.id}: ${labels.error}`);
    else {
      if (labels.laneLegsMislabelled.length) {
        problem(`${delivery.id} labels lane legs as miles: ${labels.laneLegsMislabelled.join("; ")}`);
      }
      if (labels.expected.join("|") !== labels.shown.join("|")) {
        problem(
          `${delivery.id}'s itinerary rows read ${labels.shown.join("/")}, ` +
            `the record says ${labels.expected.join("/")}`,
        );
      }
    }
    if (/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(itinText)) problem("the itinerary shows raw ISO timestamps");
    if (!routeBlockText.includes(delivery.destination_point.label)) {
      problem(`the decision panel's TO is not the customer: ${routeBlockText.slice(0, 160)}`);
    }
    if (!routeBlockText.includes(`via ${delivery.destination.facility_id}`)) {
      problem(`the decision panel does not name the holding facility as "via"`);
    }
    diag = await mapDiagnostics(page);
    note("delivery-map", {
      road: diag.rendered.road,
      roadReal: diag.rendered.arcs["arcs-road"],
      roadEstimated: diag.rendered.arcs["arcs-road-estimated"],
      selectedArcs: diag.rendered.arcs["arcs-selected"],
      dots: diag.dotCount,
      hubCount: diag.hubCount,
    });
    if (!diag.rendered.road) problem("the selected delivery draws no road legs (first/last mile)");
    if (delivery.destination.outbound_route.length > 0 && diag.rendered.arcs["arcs-selected"] < 2) {
      problem(
        `the selected delivery draws ${diag.rendered.arcs["arcs-selected"]} route arcs; ` +
          "both the inbound and the outbound half are expected",
      );
    }
    // The customer's dot: present, emphasized, inside the focus fit — and the
    // origin carries ONE marker, the diamond, with no dot doubled under it.
    const markers = await page.evaluate(() => {
      const unit = parseFloat(getComputedStyle(document.documentElement).fontSize);
      const centre = (el) => {
        const r = el.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width };
      };
      const canvas = document.querySelector(".map-canvas").getBoundingClientRect();
      const panels = [...document.querySelectorAll(".overlay.left, .overlay.right")].map((p) =>
        p.getBoundingClientRect(),
      );
      const diamonds = [...document.querySelectorAll(".map-node.origin-node .origin-diamond")].map(centre);
      const allDots = [...document.querySelectorAll(".map-node.endpoint-node .endpoint-dot")].map(centre);
      const doubled = diamonds.filter((d) =>
        allDots.some((p) => Math.hypot(p.x - d.x, p.y - d.y) < 2),
      ).length;
      const el = document.querySelector(".map-node.customer-node.selected .endpoint-dot");
      if (!el) return { unit, diamonds: diamonds.length, dots: allDots.length, doubled, customer: null };
      const c = centre(el);
      return {
        unit,
        diamonds: diamonds.length,
        dots: allDots.length,
        doubled,
        customer: {
          size: Math.round(c.w * 10) / 10,
          onScreen:
            c.x > canvas.left && c.x < canvas.right && c.y > canvas.top && c.y < canvas.bottom,
          underPanel: panels.some((p) => c.x > p.left && c.x < p.right && c.y > p.top && c.y < p.bottom),
        },
      };
    });
    note("delivery-markers", markers);
    if (markers.doubled) problem(`${markers.doubled} origin diamond(s) have a redundant endpoint dot under them`);
    if (!markers.customer) problem("the selected delivery's customer point has no emphasized dot");
    else {
      if (!markers.customer.onScreen) problem("the focus fit left the customer point off screen");
      if (markers.customer.underPanel) problem("the focus fit parked the customer point under a panel");
      if (markers.customer.size < 0.35 * markers.unit) {
        problem(`the selected customer dot is not emphasized (${markers.customer.size}px at ${markers.unit}px root)`);
      }
    }
    await shot(page, "20-delivery-focus");
    await vetPanel(page, ".overlay.right", `decision · delivery ${delivery.id} (itinerary)`);
    await closeDecision();
    await page.waitForTimeout(600);
  }

  // -- one line per road mile, in every state -------------------------------
  // A delivery's first and last mile are ROAD legs. Drawing the lane trace from
  // the customer's own origin as well laid a straight second line along the
  // same mile — two paths between the same two points, one of them a lie about
  // how the goods travel. Checked unselected (dashed connectors, one per mile)
  // and selected (the fetched geometry, still one per mile).
  const bookedDeliveries = world.shipments
    .filter((s) => s.destination_point && s.destination)
    .map((s) => s.id);
  /* `focused` — the shipment is SELECTED, so the map is drawing its arcs and
     nothing else. Only then can the pixel rule speak: with every booking on
     screen, a neighbour's route crossing near a hub lands in the query box and
     is indistinguishable from a duplicate. The structural rules below carry
     the unselected case instead, and they are exact — every arc feature names
     the shipment it belongs to. */
  const checkMiles = (label, result, focused = false) => {
    if (result.error) {
      problem(`road miles ${label}: ${result.error}`);
      return;
    }
    note(`road-miles · ${label}`, result);
    for (const mile of result.miles) {
      if (mile.roadFeatures !== 1) {
        problem(`${label} ${mile.what}: ${mile.roadFeatures} road lines draw it, expected exactly one`);
      }
      if (mile.laneArcsAtMileEnds.length) {
        problem(
          `${label} ${mile.what}: a ${mile.laneArcsAtMileEnds.join("/")} lane arc reaches the mile's outer end — ` +
            "the mile is drawn twice, once along the road and once straight",
        );
      }
      if (!focused) continue;
      const laneish = (mile.renderedMidway ?? []).filter((l) => !l.startsWith("arcs-road"));
      if (laneish.length) {
        problem(`${label} ${mile.what}: ${laneish.join(",")} renders midway along the mile — two lines for one mile`);
      }
    }
  };
  /* Each of the selected shipment's own lines, at its own midpoint: exactly one
     feature on the highlight layer and one on its halo. Two means the record
     was traced over the booking (or over the plan) — the same segment, twice. */
  const checkStacking = (label, result, layers = ["arcs-selected", "arcs-selected-halo"]) => {
    note(`trace-stacking · ${label}`, result);
    if (result.features === 0) {
      problem(`${label}: nothing is drawn for it at all`);
      return;
    }
    if (result.identical) {
      problem(`${label}: ${result.identical} of its lines are drawn twice over — one segment, two identical features`);
    }
    for (const segment of result.segments) {
      if (!segment.onScreen) continue;
      for (const layer of layers) {
        if (segment.rendered[layer] !== 1) {
          problem(
            `${label}: ${layer} renders ${segment.rendered[layer]} features at segment ${segment.index}'s midpoint, expected exactly one`,
          );
        }
      }
    }
  };
  if (!bookedDeliveries.length) problem("no booked delivery in this world: the road miles are untested");
  for (const id of bookedDeliveries) checkMiles(`${id} unselected`, await roadMileArcs(page, id));
  await shot(page, "35-road-miles-unselected");
  // Selected, with the provider's answer in: the two named in the report if
  // this world has them, otherwise whatever it does have.
  const mileSubjects = ["DL-0003", "DL-0005"].filter((id) => bookedDeliveries.includes(id));
  for (const id of mileSubjects.length ? mileSubjects : bookedDeliveries.slice(0, 2)) {
    const row = page.locator(".overlay.left .row").filter({ hasText: id }).first();
    await row.scrollIntoViewIfNeeded();
    await row.click();
    await page.waitForSelector(".overlay.right .itinerary", { timeout: 20000 });
    await waitForMapIdle(page, 20000);
    await page.waitForTimeout(3000); // the road fetch, and the redraw after it
    checkMiles(`${id} selected`, await roadMileArcs(page, id), true);
    checkStacking(`${id} selected`, await stackedTraces(page, id));
    await shot(page, `35-road-miles-${id}`);
    await closeDecision();
    await page.waitForTimeout(500);
  }
  await page.click(".map-controls .chip:has-text('FIT')");
  await page.waitForTimeout(600);

  // -- dwells on the way (§7.9): every booked journey that stops somewhere ---
  // A stop is a row of its own in the itinerary (never a leg), a ring on the
  // map while its shipment is selected, and it must be inside the focus fit —
  // a dwell the operator cannot see is a dwell they cannot reason about.
  const STOP_LABEL = { entry: "ENTRY", transit: "CROSS-DOCK", exit: "EXIT" };
  const withDwells = world.shipments.filter((s) =>
    (s.destination?.stops ?? []).some((stop) => stop.role !== "hold"),
  );
  // A shut facility may appear in no booking at all: the closure rejects every
  // stop, so a re-routed journey exits elsewhere, nothing is left staged behind
  // a closed door, and no leg of any booked route runs into one. Every booking
  // is read, ordinary allocations included — one stop-aware router plans them
  // all, so a shut facility can turn up in any of them.
  // The one exception the engine makes is cargo the closure TRAPPED: it keeps
  // the booking it already had where it stands, so its stay AT THAT facility is
  // the trap itself. Listed, never asserted away — and only there.
  const trappedAt = new Map(trappedCargo.map((s) => [s.id, s.origin_facility_id]));
  const stopsAtAShutFacility = bookedStops.filter((s) => closedFacilities.has(s.facility_id));
  const closedInStops = stopsAtAShutFacility
    .filter((s) => trappedAt.get(s.shipment) !== s.facility_id)
    .map((s) => `${s.shipment}@${s.facility_id} (${s.role})`);
  const trappedInStops = stopsAtAShutFacility
    .filter((s) => trappedAt.get(s.shipment) === s.facility_id)
    .map((s) => `${s.shipment}@${s.facility_id}`);
  const closedExits = world.shipments
    .filter((s) => s.destination?.exit_facility_id && closedFacilities.has(s.destination.exit_facility_id))
    .map((s) => s.id);
  // Every lane a booking travels, resolved against the world's own lane list:
  // the facility a leg ENDS at is a facility the goods are routed into, and a
  // shut one may never be that. (A leg that STARTS at one is what trapped cargo
  // keeps, so only the far end is asserted.)
  const laneById = new Map(world.lanes.map((lane) => [lane.id, lane]));
  const bookedLanes = world.shipments.flatMap((s) =>
    [...(s.destination?.route ?? []), ...(s.destination?.outbound_route ?? [])].map((laneId) => ({
      shipment: s.id,
      laneId,
      lane: laneById.get(laneId) ?? null,
    })),
  );
  const unknownLanes = bookedLanes.filter((l) => !l.lane).map((l) => `${l.shipment}:${l.laneId}`);
  const closedLaneEnds = bookedLanes
    .filter((l) => l.lane && closedFacilities.has(l.lane.to))
    .map((l) => `${l.shipment}:${l.laneId}→${l.lane.to}`);
  const ordinaryWithDwells = withDwells.filter((s) => !s.destination_point);
  note("closed-facility-not-booked", {
    closed: [...closedFacilities],
    closedInStops,
    trappedInStops,
    closedExits,
    closedLaneEnds,
    unknownLanes,
    stopsRead: bookedStops.length,
    lanesRead: bookedLanes.length,
    dwellJourneys: withDwells.map((s) => s.id),
    ordinaryDwellJourneys: ordinaryWithDwells.map((s) => s.id),
    exits: withDwells.map((s) => `${s.id}:${s.destination.exit_facility_id ?? "—"}`),
  });
  if (closedInStops.length) problem(`booked journeys still dwell at a shut facility: ${closedInStops.join(", ")}`);
  if (closedExits.length) problem(`booked deliveries still exit through a shut facility: ${closedExits.join(", ")}`);
  if (closedLaneEnds.length) problem(`booked routes still run into a shut facility: ${closedLaneEnds.join(", ")}`);
  if (unknownLanes.length) problem(`booked routes cite lanes this world does not have: ${unknownLanes.join(", ")}`);
  if (!withDwells.length) problem("no booked journey records a dwell: transit stops are untested");
  if (!ordinaryWithDwells.length) {
    problem("no ORDINARY shipment records a dwell: the stop checks read deliveries only");
  }
  const longestJourney = withDwells.reduce(
    (best, s) => (s.destination.stops.length > (best?.destination.stops.length ?? 0) ? s : best),
    null,
  );
  for (const journey of withDwells) {
    const dwells = journey.destination.stops.filter((stop) => stop.role !== "hold");
    const row = page.locator(".overlay.left .row").filter({ hasText: journey.id }).first();
    await row.scrollIntoViewIfNeeded();
    await row.click();
    await page.waitForSelector(".overlay.right .itinerary", { timeout: 20000 });
    await waitForMapIdle(page, 20000);
    await page.waitForTimeout(1200);
    const stopKinds = await page.$$eval(".overlay.right .itinerary .leg.stop-leg .stop-kind", (all) =>
      all.map((k) => k.textContent.trim()),
    );
    // .leg-kind is still exactly the leg/hold/deliver contract the label check
    // relies on: a dwell must never show up there.
    const legKinds = await page.$$eval(".overlay.right .itinerary .leg-kind", (all) =>
      all.map((k) => k.textContent.trim()),
    );
    const expectedStops = dwells.map((stop) => STOP_LABEL[stop.role]);
    const stopNodes = await page.evaluate(
      ([ids, hubSelector]) => {
        const canvas = document.querySelector(".map-canvas").getBoundingClientRect();
        const panels = [...document.querySelectorAll(".overlay.left, .overlay.right")].map((p) =>
          p.getBoundingClientRect(),
        );
        return ids.map((id) => {
          const el = [...document.querySelectorAll(hubSelector)].find(
            (m) => m.querySelector(".node-label")?.textContent.trim() === id,
          );
          if (!el) return { id, present: false };
          const r = el.querySelector(".node-box").getBoundingClientRect();
          const cx = r.left + r.width / 2;
          const cy = r.top + r.height / 2;
          return {
            id,
            present: true,
            ring: el.classList.contains("transit-stop"),
            onScreen: cx > canvas.left && cx < canvas.right && cy > canvas.top && cy < canvas.bottom,
            underPanel: panels.some((p) => cx > p.left && cx < p.right && cy > p.top && cy < p.bottom),
          };
        });
      },
      [dwells.map((stop) => stop.facility_id), HUB],
    );
    const itinText = await page.$eval(".overlay.right .itinerary", (e) => e.textContent);
    // Ordinary allocations are routed by the same stop-aware router as
    // deliveries, so they stage through hubs too — and their dwells have to
    // read on screen exactly as a delivery's do, without a customer journey
    // around them.
    const kind = journey.destination_point ? "delivery" : "ordinary";
    note("journey-dwells", { id: journey.id, kind, stopKinds, legKinds, stopNodes });
    if (stopKinds.join("|") !== expectedStops.join("|")) {
      problem(
        `${journey.id}'s itinerary shows dwells ${stopKinds.join("/") || "(none)"}, ` +
          `the booking records ${expectedStops.join("/")}`,
      );
    }
    if (legKinds.some((k) => Object.values(STOP_LABEL).includes(k))) {
      problem(`${journey.id} renders a dwell as a leg row: ${legKinds.join(", ")}`);
    }
    if (/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(itinText)) problem(`${journey.id}'s itinerary shows raw ISO timestamps`);
    for (const node of stopNodes) {
      if (!node.present) problem(`${journey.id} dwells at ${node.id}, which the focus fit dropped from the map`);
      else if (!node.ring) problem(`${journey.id}'s dwell at ${node.id} is not marked on the map`);
      else if (!node.onScreen) problem(`${journey.id}'s dwell at ${node.id} is off screen after the focus fit`);
      else if (node.underPanel) problem(`${journey.id}'s dwell at ${node.id} sits under a panel after the focus fit`);
    }
    if (journey === longestJourney) await shot(page, "24-long-itinerary");
    if (journey === ordinaryWithDwells[0]) await shot(page, "24b-ordinary-dwells");
    if (journey === longestJourney || journey === ordinaryWithDwells[0]) {
      await vetPanel(page, ".overlay.right", `decision · ${journey.id} (${kind}, ${dwells.length} dwells)`);
    }
    await closeDecision();
    await page.waitForTimeout(400);
  }

  // A facility that stages a truck through says so on its zone: a staging
  // reservation, named for the role, against the shipment that booked it — the
  // operator must be able to tell it from a customer's hold.
  const transitDwell = bookedStops.find((s) => s.role === "transit");
  if (!transitDwell) problem("no booked journey stages through a facility: staging reservations are untested");
  else {
    const site = world.facilities.find((f) => f.id === transitDwell.facility_id);
    await page.evaluate(
      ([lon, lat]) => window.__nodalMap.jumpTo({ center: [lon, lat], zoom: 4 }),
      [site.lon, site.lat],
    );
    await page.waitForTimeout(800);
    const box = await hub(site.id).locator(".node-box").boundingBox();
    if (!box) problem(`the staging facility ${site.id} has no clickable hub`);
    else {
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      await page.waitForSelector(".popover .zone-block", { timeout: 15000 });
      await page.waitForTimeout(600);
      const reservations = await page.evaluate(() => {
        const read = (selector) =>
          [...document.querySelectorAll(selector)].map((r) => {
            const role = r.querySelector(".reservation-role");
            return {
              role: role?.textContent.trim() ?? "",
              uppercase: role ? getComputedStyle(role).textTransform === "uppercase" : false,
              text: r.textContent.trim(),
            };
          });
        return { staging: read(".popover .reservation.staging"), holds: read(".popover .reservation:not(.staging)") };
      });
      const staged = reservations.staging.find((r) => r.text.includes(transitDwell.shipment));
      note("staging-reservation", {
        facility: site.id,
        shipment: transitDwell.shipment,
        staging: reservations.staging.length,
        holds: reservations.holds.length,
        row: staged ?? null,
      });
      await shot(page, "25-staging-facility");
      await vetPanel(page, ".popover", `facility popover · ${site.id} (cross-dock staging)`, { modal: true });
      if (!staged) {
        problem(
          `${site.id} stages ${transitDwell.shipment} through, but its zones show no staging reservation for it ` +
            `(${reservations.staging.length} staging rows)`,
        );
      } else if (!/^transit$/i.test(staged.role) || !(staged.uppercase || staged.role === staged.role.toUpperCase())) {
        problem(`the staging reservation is labelled "${staged.role}", expected TRANSIT`);
      }
      await closePopover();
      await closeDecision();
    }
  }
  await page.click(".map-controls .chip:has-text('FIT')");
  await page.waitForTimeout(600);

  // The review the log opened with was COMMITTED above, so what is still
  // planned is what that plan could NOT place — cargo standing in a facility
  // the world has shut, which no solve can route out of. Everything from here
  // on needs a queue that can actually be worked: one shipment that can be
  // solved on its own, and a second planned row so OPTIMIZE ALL is offered at
  // all. Register one from a COORDINATE origin — cargo standing inside a hub
  // can legitimately be solved to the hub it already stands in, and then there
  // is no route to draw and the arc rule below has nothing to say. (The form's
  // own validation is walked in full further down, on a second one.)
  const solvableRowId = async () =>
    page.evaluate(
      (skip) =>
        [...document.querySelectorAll(".overlay.left .row")]
          .filter((r) => r.querySelector(".badge.planned") && !r.querySelector(".badge.trapped"))
          .map((r) => r.querySelector(".row-title > span:first-child")?.textContent.trim() ?? "")
          .filter((id) => id && !skip.includes(id))[0] ?? null,
      unplacedByPlan,
    );
  let solveId = await solvableRowId();
  if (!solveId) {
    solveId = await registerPlannedShipment("Smoke Solve Origin", 48.85, 2.35);
    note("registered-for-single-solve", { id: solveId, planned: await plannedRowCount() });
    if (!solveId) problem("registering a shipment for the single-solve flow did not select it");
  }
  if (!solveId) throw new Error("no solvable planned shipment: reset the demo database first");

  await scaleChecks(world, trappedCargo);

  // Single SOLVE + COMMIT.
  await page.locator(".overlay.left .row").filter({ hasText: solveId }).first().click();
  await page.click(".overlay.right .panel-footer button:has-text('SOLVE')");
  await page.waitForSelector(".overlay.right .winner, .overlay.right .reject-code", { timeout: 90000 });
  await waitNotBusy(page);
  await waitForMapIdle(page, 15000);
  await page.waitForTimeout(500);
  diag = await mapDiagnostics(page);
  note("after-solve", { arcs: diag.rendered.arcs, hubCount: diag.hubCount });
  if (!diag.rendered.arcs["arcs-selected"]) problem("single solve rendered no selected arc");
  await shot(page, "03-solved");
  await vetPanel(page, ".overlay.right", "decision · planned, draft solve");
  await vetPanel(page, ".stepper", "stepper · draft solve");
  await page.click(".overlay.right .panel-footer button:has-text('COMMIT')");
  await waitNotBusy(page);
  await page.waitForTimeout(2500);
  const rowBadge = await page.$eval(".overlay.left .row.selected .badge", (b) => b.textContent).catch(() => null);
  note("after-commit", { rowBadge });
  if (rowBadge !== "ALLOCATED") problem(`after COMMIT the selected row badge is ${rowBadge}`);
  await shot(page, "04-committed");
  await vetPanel(page, ".overlay.right", "decision · just committed");
  if (await page.$(".toast")) await vetPanel(page, ".toast", "toast · commit confirmed");
  await closeDecision();

  // Shipment lifecycle: register via the form, move readiness, cancel.
  await page.click(".overlay.left .queue-actions button:has-text('+ NEW')");
  await page.waitForSelector(".popover", { timeout: 10000 });
  await shot(page, "16-new-shipment-form");
  await vetPanel(page, ".popover", "+ NEW · allocation (defaults)", { modal: true });
  // Half a customer point can never be routed to: the form has to refuse it,
  // say why, and keep what was typed.
  await page.fill(".popover input[aria-label='customer label']", "Half A Customer");
  await page.click(".popover .panel-footer button:has-text('REGISTER SHIPMENT')");
  await page.waitForTimeout(600);
  const halfProblem = await page.$eval(".popover .reject-code", (e) => e.textContent.trim()).catch(() => null);
  const keptLabel = await page.inputValue(".popover input[aria-label='customer label']");
  note("new-shipment-validation", { halfProblem: halfProblem?.slice(0, 120) ?? null, keptLabel });
  await vetPanel(page, ".popover", "+ NEW · validation error (half-specified destination)", { modal: true });
  await shot(page, "38-new-shipment-validation");
  if (!halfProblem) problem("a half-specified destination was accepted, or refused silently");
  else if (!/latitude/i.test(halfProblem)) {
    problem(`the form's refusal does not say what is missing: ${halfProblem.slice(0, 120)}`);
  }
  if (keptLabel !== "Half A Customer") problem("the refused form threw away what was typed into it");
  if (await page.$(".popover")) await page.fill(".popover input[aria-label='customer label']", "");
  await page.fill(".popover input[type='datetime-local'] >> nth=1", "2026-09-06T12:00"); // DUE
  await page.click(".popover .panel-footer button:has-text('REGISTER SHIPMENT')");
  await page.waitForTimeout(2500);
  const newId = await selectedRowId();
  const newBadge = await page.$eval(".overlay.left .row.selected .badge", (b) => b.textContent).catch(() => null);
  note("register-shipment", { newId, newBadge, popoverGone: !(await page.$(".popover")) });
  if (!newId || !newId.startsWith("SHP-API-")) problem(`the new shipment was not selected (${newId})`);
  if (newBadge !== "PLANNED") problem(`the new shipment is ${newBadge}, expected PLANNED`);
  // Move its readiness.
  await page.fill("#ready-draft", "2026-09-02T08:00");
  await page.click(".shipment-actions button:has-text('SET')");
  await waitNotBusy(page);
  await page.waitForTimeout(1500);
  const readyToast = await page.$eval(".toast", (t) => t.textContent).catch(() => "");
  note("set-ready", { readyToast });
  if (!readyToast.includes("now ready")) problem(`SET READY gave no confirmation (${readyToast})`);
  // Cancel it (confirm-once).
  await page.click(".shipment-actions button:has-text('CANCEL SHIPMENT')");
  await page.click(".shipment-actions button:has-text('CONFIRM CANCEL?')");
  await waitNotBusy(page);
  await page.waitForTimeout(2000);
  const stillListed = await page
    .locator(".overlay.left .row")
    .filter({ hasText: newId })
    .count();
  note("cancel-shipment", { stillListed, panelGone: !(await page.$(".overlay.right")) });
  if (stillListed) problem("the cancelled shipment is still in the queue");

  // Register an A→B DELIVERY from the same form. The place search is answered
  // with a canned hit (the harness must never depend on a live maps provider),
  // picked for real; the coordinates it books with are then typed explicitly,
  // so the shipment lands in a known place every run.
  await page.route("**/api/places/search*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        enabled: true,
        places: [
          { name: "Smoke Search Hit", address: "1 Test Way, Ottawa", lat: 45.4215, lon: -75.6972 },
        ],
      }),
    }),
  );
  await page.click(".overlay.left .queue-actions button:has-text('+ NEW')");
  await page.waitForSelector(".popover", { timeout: 10000 });
  const customerSearch = ".popover input[placeholder='find the customer by name']";
  if (!(await page.$(customerSearch))) problem("the new-shipment form offers no customer place search");
  await page.fill(customerSearch, "smoke depot");
  await page.locator(".popover .field-row", { has: page.locator(`input[placeholder='find the customer by name']`) })
    .locator("button")
    .filter({ hasText: /^SEARCH$/ })
    .click();
  await page.waitForSelector(".popover .place-hit", { timeout: 15000 });
  await vetPanel(page, ".popover", "+ NEW · place search results", { modal: true });
  // A search that matched nothing, and a provider that is not configured at
  // all: both have to SAY which, rather than leaving an empty control that
  // looks like it is still thinking.
  for (const [label, body, expect] of [
    ["nothing matched", { enabled: true, places: [] }, /nothing matched/i],
    ["no maps provider", { enabled: false, places: [] }, /no maps provider is configured/i],
  ]) {
    await page.unroute("**/api/places/search*");
    await page.route("**/api/places/search*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) }),
    );
    await page.fill(customerSearch, `smoke ${label}`).catch(() => null);
    const searchRow = page
      .locator(".popover .field-row", { has: page.locator(`input[placeholder='find the customer by name']`) })
      .locator("button")
      .filter({ hasText: /^SEARCH$/ });
    if (await searchRow.count()) {
      await searchRow.click();
      await page.waitForTimeout(1200);
    }
    const said = await page.$eval(".popover .panel-body", (p) => p.textContent.replace(/\s+/g, " "));
    const hits = await page.$$eval(".popover .place-hit", (all) => all.length);
    note(`place-search-${label.replace(/\s+/g, "-")}`, { hits, says: expect.test(said) });
    await vetPanel(page, ".popover", `+ NEW · place search: ${label}`, { modal: true });
    await shot(page, `32-search-${label.replace(/\s+/g, "-")}`);
    if (hits) problem(`the ${label} search still lists ${hits} hits`);
    if (!expect.test(said)) problem(`the ${label} search does not say so in the form`);
  }
  // Back to the canned hit, and pick it for real.
  await page.unroute("**/api/places/search*");
  await page.route("**/api/places/search*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        enabled: true,
        places: [{ name: "Smoke Search Hit", address: "1 Test Way, Ottawa", lat: 45.4215, lon: -75.6972 }],
      }),
    }),
  );
  await page.click(".popover .panel-footer button:has-text('CANCEL')");
  await page.waitForTimeout(300);
  await page.click(".overlay.left .queue-actions button:has-text('+ NEW')");
  await page.waitForSelector(".popover", { timeout: 10000 });
  await page.fill(customerSearch, "smoke depot");
  await page
    .locator(".popover .field-row", { has: page.locator(`input[placeholder='find the customer by name']`) })
    .locator("button")
    .filter({ hasText: /^SEARCH$/ })
    .click();
  await page.waitForSelector(".popover .place-hit", { timeout: 15000 });
  await page.click(".popover .place-hit");
  await page.waitForTimeout(300);
  const picked = {
    label: await page.inputValue(".popover input[aria-label='customer label']"),
    lat: await page.inputValue(".popover input[aria-label='customer latitude']"),
    lon: await page.inputValue(".popover input[aria-label='customer longitude']"),
  };
  note("place-search-pick", picked);
  if (picked.label !== "Smoke Search Hit" || Math.abs(Number(picked.lat) - 45.4215) > 1e-4) {
    problem(`picking a search hit did not fill the customer fields: ${JSON.stringify(picked)}`);
  }
  const hintText = await page.$eval(".popover .field-hint", (e) => e.textContent.trim());
  if (!/DELIVERY/.test(hintText)) problem(`a filled destination does not read as a delivery (${hintText})`);
  // Deterministic coordinates, typed over the picked ones.
  await page.fill(".popover input[aria-label='customer label']", "Smoke Customer Site");
  await page.fill(".popover input[aria-label='customer latitude']", "45.42");
  await page.fill(".popover input[aria-label='customer longitude']", "-75.7");
  await page.fill(".popover input[aria-label='hold days']", "3");
  await page.fill(".popover input[type='datetime-local'] >> nth=1", "2026-09-25T12:00"); // DELIVER BY
  await shot(page, "21-new-delivery-form");
  await vetPanel(page, ".popover", "+ NEW · delivery, search hit picked", { modal: true });
  await page.click(".popover .panel-footer button:has-text('REGISTER SHIPMENT')");
  await waitNotBusy(page);
  await page.waitForTimeout(2500);
  const deliveryId = await selectedRowId();
  const deliveryRowText = await page
    .$eval(".overlay.left .row.selected", (r) => r.textContent)
    .catch(() => "");
  const newCustomerDot = await page.evaluate(
    (id) =>
      [...document.querySelectorAll(".map-node.customer-node")].some((el) => el.title.includes(id)),
    deliveryId ?? "",
  );
  note("register-delivery", { deliveryId, row: deliveryRowText.slice(0, 200), newCustomerDot });
  if (!deliveryId || !deliveryId.startsWith("SHP-API-")) problem(`the new delivery was not selected (${deliveryId})`);
  if (!deliveryRowText.includes("Smoke Customer Site")) {
    problem(`the new delivery's row does not read TO its customer: ${deliveryRowText.slice(0, 160)}`);
  }
  if (!/not yet routed/.test(deliveryRowText)) {
    problem(`an unrouted delivery row does not say so: ${deliveryRowText.slice(0, 160)}`);
  }
  if (!newCustomerDot) problem("the new delivery's customer point has no dot on the map");
  const deliveryPanel = await page.$eval(".overlay.right .route-block", (r) => r.textContent).catch(() => "");
  if (!deliveryPanel.includes("hold 3d")) problem(`the decision panel does not state the hold: ${deliveryPanel.slice(0, 160)}`);
  await page.click(".shipment-actions button:has-text('CANCEL SHIPMENT')");
  await page.click(".shipment-actions button:has-text('CONFIRM CANCEL?')");
  await waitNotBusy(page);
  await page.waitForTimeout(2000);
  const deliveryStillListed = await page
    .locator(".overlay.left .row")
    .filter({ hasText: deliveryId ?? "SHP-API-" })
    .count();
  note("cancel-delivery", { deliveryStillListed });
  if (deliveryStillListed) problem("the cancelled delivery is still in the queue");
  await page.unroute("**/api/places/search*");

  // A delivery across the date line: a Pacific hub as its origin, a customer
  // just WEST of ±180. The two are ~27° apart, but longitudes taken literally
  // make that 333° the long way round — a doubled world at zoom ~1 with the
  // region crushed under a panel. The fit must frame the short way.
  // The origin must be OPEN: nothing routes out of a shut facility, so a closed
  // Pacific hub (SYD in the closure story) cannot carry this flow — that case
  // gets its own check below.
  const pacific = ["BNE", "SYD", "MEL"].find(
    (id) => world.facilities.some((f) => f.id === id) && !closedFacilities.has(id),
  );
  if (!pacific) {
    problem("the world has no open Pacific hub (BNE/SYD/MEL): the antimeridian fit is untested");
  } else {
    await page.click(".overlay.left .queue-actions button:has-text('+ NEW')");
    await page.waitForSelector(".popover", { timeout: 10000 });
    await page.selectOption(".popover select[aria-label='origin facility']", pacific);
    await page.fill(".popover input[aria-label='customer label']", "Smoke Dateline Depot");
    await page.fill(".popover input[aria-label='customer latitude']", "-16.5");
    await page.fill(".popover input[aria-label='customer longitude']", "-179.6");
    await page.fill(".popover input[aria-label='hold days']", "2");
    await page.fill(".popover input[type='datetime-local'] >> nth=1", "2026-11-30T12:00");
    await page.click(".popover .panel-footer button:has-text('REGISTER SHIPMENT')");
    await waitNotBusy(page);
    await page.waitForTimeout(2500);
    const datelineId = await selectedRowId();
    if (!datelineId || !datelineId.startsWith("SHP-API-")) {
      problem(`the dateline delivery was not registered and selected (${datelineId})`);
    }
    await page.click(".overlay.right .panel-footer button:has-text('SOLVE')");
    await page.waitForSelector(".overlay.right .winner, .overlay.right .reject-code", { timeout: 90000 });
    await waitNotBusy(page);
    await waitForMapIdle(page, 20000);
    await page.waitForTimeout(1500);
    // `.winner` also wraps the NO FEASIBLE DESTINATION summary; only a chosen
    // candidate renders the head with its score.
    const datelineSolved = !!(await page.$(".overlay.right .winner .winner-head"));
    const datelineKinds = await page.$$eval(".overlay.right .itinerary .leg-kind", (all) =>
      all.map((k) => k.textContent.trim()),
    ).catch(() => []);
    // Longitudes the viewport spans: 360° occupy 512·2^zoom CSS px.
    const fit = await page.evaluate(() => {
      const map = window.__nodalMap;
      const canvas = map.getContainer().getBoundingClientRect();
      const dot = document.querySelector(".map-node.customer-node.selected .endpoint-dot");
      const r = dot && dot.getBoundingClientRect();
      const x = r && r.left + r.width / 2;
      const y = r && r.top + r.height / 2;
      return {
        zoom: Number(map.getZoom().toFixed(2)),
        lng: Number(map.getCenter().lng.toFixed(2)),
        visibleLonSpan: Math.round((360 * canvas.width) / (512 * Math.pow(2, map.getZoom()))),
        customerDot: !r
          ? null
          : { onScreen: x > canvas.left && x < canvas.right && y > canvas.top && y < canvas.bottom },
      };
    });
    note("dateline-delivery", { id: datelineId, origin: pacific, solved: datelineSolved, kinds: datelineKinds, fit });
    await shot(page, "22-dateline-fit");
    if (!datelineSolved) {
      problem(`the dateline delivery from ${pacific} did not solve, so its focus fit is untested`);
    } else {
      if (fit.visibleLonSpan >= 360) {
        problem(`the dateline focus fit spans ${fit.visibleLonSpan}° of longitude — the world is doubled`);
      }
      if (!(fit.zoom > 2)) problem(`the dateline focus fit zoomed out to ${fit.zoom}`);
      if (!fit.customerDot) problem("the dateline delivery's customer point has no selected dot");
      else if (!fit.customerDot.onScreen) problem("the dateline focus fit left the customer point off screen");
      // Its origin is a FACILITY: whatever leaves first is a lane, or — when it
      // is held where it starts — the whole journey is the one last mile. A
      // first mile is the one thing this delivery cannot have.
      if (datelineKinds[0] === "FIRST MILE") {
        problem(`a facility-origin delivery labels its first leg FIRST MILE (${datelineKinds.join(", ")})`);
      }
      if (datelineKinds.filter((k) => k === "LAST MILE").length !== 1) {
        problem(`the dateline delivery has ${datelineKinds.filter((k) => k === "LAST MILE").length} LAST MILE rows (${datelineKinds.join(", ")})`);
      }
    }
    await page.click(".shipment-actions button:has-text('CANCEL SHIPMENT')");
    await page.click(".shipment-actions button:has-text('CONFIRM CANCEL?')");
    await waitNotBusy(page);
    await page.waitForTimeout(2000);
    const datelineLeft = await page
      .locator(".overlay.left .row")
      .filter({ hasText: datelineId ?? "SHP-API-" })
      .count();
    note("cancel-dateline", { datelineLeft });
    if (datelineLeft) problem("the cancelled dateline delivery is still in the queue");
    await closeDecision();
  }

  // The SHUT facility's own popover: it must say CLOSED (whatever the standing
  // open flag says), and its occupancy bars — trapped cargo over a zeroed
  // capacity — must stay inside their chart instead of painting the panel.
  const shutHubId = [...closedFacilities][0];
  if (shutHubId) {
    const shutFacility = world.facilities.find((f) => f.id === shutHubId);
    await page.evaluate(
      ([lon, lat]) => window.__nodalMap.jumpTo({ center: [lon, lat], zoom: 4 }),
      [shutFacility.lon, shutFacility.lat],
    );
    await page.waitForTimeout(500);
    const shutBox = await hub(shutHubId).locator(".node-box").boundingBox();
    if (!shutBox) problem(`the shut facility ${shutHubId} has no clickable hub`);
    else {
      await page.mouse.click(shutBox.x + shutBox.width / 2, shutBox.y + shutBox.height / 2);
      await page.waitForSelector(".popover .zone-block", { timeout: 15000 });
      await page.waitForTimeout(800);
      const shutPopover = await page.evaluate(() => {
        const pop = document.querySelector(".popover");
        const header = pop.querySelector(".panel-header").textContent;
        const pr = pop.getBoundingClientRect();
        const escaped = [];
        for (const chart of pop.querySelectorAll(".timeline")) {
          const cr = chart.getBoundingClientRect();
          for (const bar of chart.querySelectorAll(".bar")) {
            const br = bar.getBoundingClientRect();
            if (br.top < cr.top - 1 || br.bottom > cr.bottom + 1) escaped.push(Math.round(br.height));
          }
        }
        // Nothing inside the popover may paint outside it.
        const outside = [...pop.querySelectorAll("*")].filter((el) => {
          const r = el.getBoundingClientRect();
          return r.width > 0 && (r.left < pr.left - 1 || r.right > pr.right + 1 || r.top < pr.top - 1 || r.bottom > pr.bottom + 1);
        }).length;
        return { header: header.slice(0, 120), escapedBars: escaped, outside };
      });
      note("shut-facility-popover", shutPopover);
      await shot(page, "27-shut-facility-popover");
      await vetPanel(page, ".popover", `facility popover · ${shutHubId} (CLOSED, trapped cargo, zeroed zones)`, { modal: true });
      if (!/CLOSED/.test(shutPopover.header) || /\bOPEN\b/.test(shutPopover.header)) {
        problem(`the shut facility's popover header does not say CLOSED (${shutPopover.header})`);
      }
      if (shutPopover.escapedBars.length) problem(`timeline bars paint outside their chart: heights ${shutPopover.escapedBars.join(",")}`);
      if (shutPopover.outside) problem(`${shutPopover.outside} element(s) paint outside the facility popover`);
      await closePopover();
    }
    await page.click(".map-controls .chip:has-text('FIT')");
    await page.waitForTimeout(500);
  }

  // -- a facility whose capacity was CUT, and a zone driven to zero ---------
  // The cut is the whole story of such a facility, and it is a story the chart
  // has to tell: occupancy standing above the day's effective capacity. The
  // trap is scale — capacity jumps back tenfold the day the cut lifts, and a
  // chart drawn against THAT flattens the days it is over into a stripe.
  const cutDisruption = world.disruptions.find(
    (d) => d.status === "active" && d.kind === "capacity_reduced" && d.facility_id,
  );
  if (!cutDisruption) {
    problem("no facility runs under a capacity cut in this world: that popover state is untested");
  } else {
    const cutFacility = world.facilities.find((f) => f.id === cutDisruption.facility_id);
    await page.evaluate(
      ([lon, lat]) => window.__nodalMap.jumpTo({ center: [lon, lat], zoom: 4 }),
      [cutFacility.lon, cutFacility.lat],
    );
    await page.waitForTimeout(600);
    const cutBox = await hub(cutFacility.id).locator(".node-box").boundingBox();
    if (!cutBox) problem(`the cut facility ${cutFacility.id} has no clickable hub`);
    else {
      await page.mouse.click(cutBox.x + cutBox.width / 2, cutBox.y + cutBox.height / 2);
      await page.waitForSelector(".popover .zone-block", { timeout: 15000 });
      await page.waitForTimeout(800);
      // Read the chart as a chart: how tall the bars are in the box they have,
      // which days are marked over, and where the capacity rule sits.
      // Read the chart the way an operator does: each day's drawn bar against
      // each day's drawn capacity rule, and both against the numbers the day's
      // own tooltip states.
      const charts = await page.evaluate(() =>
        [...document.querySelectorAll(".popover .zone-block")].map((zone) => {
          const chart = zone.querySelector(".timeline");
          const box = chart ? chart.getBoundingClientRect() : null;
          const days = [...zone.querySelectorAll(".bar-slot")].map((slot) => {
            const bar = slot.querySelector(".bar")?.getBoundingClientRect();
            const mark = slot.querySelector(".cap-mark")?.getBoundingClientRect();
            const numbers = /:\s*(\d+)\/(\d+|∞)/.exec(slot.title ?? "");
            return {
              occ: numbers ? Number(numbers[1]) : null,
              cap: numbers && numbers[2] !== "∞" ? Number(numbers[2]) : null,
              barTop: bar ? Math.round(bar.top) : null,
              barHeight: bar ? Math.round(bar.height) : 0,
              markTop: mark ? Math.round(mark.top) : null,
            };
          });
          return {
            zone: zone.querySelector(".row-title span")?.textContent.replace(/\s+/g, " ").trim() ?? "",
            head: zone.querySelector(".row-title .mono")?.textContent.replace(/\s+/g, " ").trim() ?? "",
            chartHeight: box ? Math.round(box.height) : 0,
            tallestBar: Math.round(Math.max(0, ...days.map((d) => d.barHeight))),
            overDays: zone.querySelectorAll(".bar.over").length,
            capMarks: zone.querySelectorAll(".cap-mark").length,
            buckets: days.length,
            days,
          };
        }),
      );
      note("cut-capacity-popover", { facility: cutFacility.id, disruption: cutDisruption.id, charts });
      // Two things the chart claims, both checked per DAY against that day's
      // own numbers:
      //   the rule means what it says — a bar drawn above the capacity rule is
      //     a day over capacity, and only such a day;
      //   the height means something — a zone nowhere near full must not be
      //     drawn as full (scaling by occupancy alone drew a zone at 40% of
      //     its capacity as a full-height bar).
      for (const chart of charts) {
        let fullest = 0;
        for (const day of chart.days) {
          if (day.occ === null || day.cap === null || day.barTop === null || day.markTop === null) continue;
          fullest = Math.max(fullest, day.cap > 0 ? day.occ / day.cap : day.occ > 0 ? Infinity : 0);
          const drawnOver = day.barTop < day.markTop - 1;
          const reallyOver = day.occ > day.cap;
          if (drawnOver !== reallyOver) {
            problem(
              `${chart.zone}: a day at ${day.occ}/${day.cap} is drawn ${drawnOver ? "above" : "below"} ` +
                `its capacity rule but is ${reallyOver ? "over" : "within"} capacity`,
            );
            break;
          }
        }
        const drawn = chart.tallestBar / Math.max(1, chart.chartHeight);
        if (Number.isFinite(fullest) && fullest < 0.9 && chart.buckets && drawn > fullest + 0.35) {
          problem(
            `${chart.zone} peaks at ${Math.round(fullest * 100)}% of its capacity but draws bars ` +
              `${Math.round(drawn * 100)}% of the chart — the fill is not readable`,
          );
        }
      }
      await vetPanel(page, ".popover", `facility popover · ${cutFacility.id} (capacity cut ${Math.round(cutDisruption.magnitude * 100)}%)`, { modal: true });
      await shot(page, "29-cut-capacity-popover");
      const overZone = charts.find((c) => c.overDays > 0);
      if (!overZone) {
        problem(`${cutFacility.id} runs under a ${cutDisruption.id} cut, but no zone chart marks a day over capacity`);
      } else {
        // The occupancy that breaches the cut must be READABLE, not a stripe
        // scaled against the capacity the zone gets back next week.
        if (overZone.tallestBar < overZone.chartHeight * 0.4) {
          problem(
            `${overZone.zone}: its tallest bar is ${overZone.tallestBar}px in a ${overZone.chartHeight}px chart — ` +
              "the cut days are flattened out of sight",
          );
        }
        if (!overZone.capMarks) {
          problem(`${overZone.zone} is over capacity but its chart draws no capacity rule to be over`);
        }
      }
      for (const chart of charts) {
        if (chart.buckets && chart.tallestBar > chart.chartHeight + 1) {
          problem(`${chart.zone}: a ${chart.tallestBar}px bar in a ${chart.chartHeight}px chart`);
        }
      }
      // Now drive one of its zones to ZERO capacity while cargo stands in it —
      // an OPEN facility in the state a closure puts a shut one in.
      const zoneWithCargo = await page.evaluate(() => {
        const zones = [...document.querySelectorAll(".popover .zone-block")];
        const busy = zones.find((z) => Number(z.querySelector(".row-title .mono")?.textContent.trim().split("/")[0]) > 0);
        return busy ? (busy.querySelector(".row-title span")?.textContent.trim().split(" ")[0] ?? null) : null;
      });
      note("zone-to-zero", { facility: cutFacility.id, zone: zoneWithCargo });
      if (!zoneWithCargo) {
        problem(`${cutFacility.id} has no occupied zone to take offline: the zero-capacity zone is untested`);
      } else {
        const zoneRow = page.locator(".popover .zone-block").filter({ hasText: zoneWithCargo }).first();
        await zoneRow.locator("button:has-text('ZONE OFFLINE')").click();
        await waitNotBusy(page);
        await page.waitForTimeout(2500);
        // The command closes the popover; its disruption row reopens it.
        const offlineRow = page.locator(".disruption-row").filter({ hasText: zoneWithCargo }).first();
        if (!(await offlineRow.count())) problem(`taking ${zoneWithCargo} offline left no disruption row`);
        else {
          await offlineRow.click();
          await page.waitForSelector(".popover .zone-block", { timeout: 15000 });
          await page.waitForTimeout(800);
          const zeroed = await page.evaluate((zoneId) => {
            const zone = [...document.querySelectorAll(".popover .zone-block")].find((z) =>
              z.textContent.includes(zoneId),
            );
            if (!zone) return null;
            const chart = zone.querySelector(".timeline").getBoundingClientRect();
            const bars = [...zone.querySelectorAll(".bar")].map((b) => b.getBoundingClientRect());
            return {
              head: zone.querySelector(".row-title .mono")?.textContent.replace(/\s+/g, " ").trim() ?? "",
              overDays: zone.querySelectorAll(".bar.over").length,
              tallest: Math.round(Math.max(0, ...bars.map((b) => b.height))),
              chartHeight: Math.round(chart.height),
              escaped: bars.filter((b) => b.top < chart.top - 1 || b.bottom > chart.bottom + 1).length,
            };
          }, zoneWithCargo);
          note("zero-capacity-zone", { zone: zoneWithCargo, ...zeroed });
          await vetPanel(page, ".popover", `facility popover · ${cutFacility.id} (${zoneWithCargo} offline: capacity 0 with cargo in it)`, { modal: true });
          await shot(page, "30-zero-capacity-zone");
          if (!zeroed) problem(`${zoneWithCargo} vanished from the popover after going offline`);
          else {
            if (!/\/0 slots today$/.test(zeroed.head)) {
              problem(`${zoneWithCargo} is offline but its header reads "${zeroed.head}", expected a zeroed capacity`);
            }
            if (!zeroed.overDays) problem(`${zoneWithCargo} holds cargo at zero capacity but no day is marked over`);
            if (zeroed.escaped) problem(`${zeroed.escaped} bars paint outside the chart at zero capacity`);
            if (zeroed.tallest > zeroed.chartHeight + 1) {
              problem(`a ${zeroed.tallest}px bar in a ${zeroed.chartHeight}px chart at zero capacity`);
            }
          }
          // Put it back: later flows read this facility's real capacity.
          await page.locator(".popover button", { hasText: "END NOW" }).first().click();
          await waitNotBusy(page);
          await page.waitForTimeout(2000);
          await closePopover();
        }
      }
    }
    await page.click(".map-controls .chip:has-text('FIT')");
    await page.waitForTimeout(500);
  }

  // Nothing routes OUT of a shut facility: a shipment registered at the closed
  // hub must solve to NO FEASIBLE DESTINATION naming the closure, badge as
  // trapped, and stay planned — CANCEL is the operator's clearing path.
  const shutOrigin = [...closedFacilities][0];
  if (shutOrigin) {
    await page.click(".overlay.left .queue-actions button:has-text('+ NEW')");
    await page.waitForSelector(".popover", { timeout: 10000 });
    await page.selectOption(".popover select[aria-label='origin facility']", shutOrigin);
    await page.click(".popover .panel-footer button:has-text('REGISTER SHIPMENT')");
    await waitNotBusy(page);
    await page.waitForTimeout(2500);
    const shutId = await selectedRowId();
    await page.click(".overlay.right .panel-footer button:has-text('SOLVE')");
    await page.waitForSelector(".overlay.right .winner, .overlay.right .reject-code", { timeout: 90000 });
    await waitNotBusy(page);
    await page.waitForTimeout(1000);
    const shutChosen = !!(await page.$(".overlay.right .winner .winner-head"));
    // The summary block itself must name the cause — not just the 38 lines below it.
    const shutText = await page.$eval(".overlay.right .winner", (p) => p.textContent).catch(() => "");
    const shutBadge = await page.$eval(".overlay.left .row.selected .badge", (b) => b.textContent).catch(() => null);
    const shutTrapped = !!(await page.$(".overlay.left .row.selected .badge.trapped"));
    const shutHead = await page.evaluate(async () => {
      const token = sessionStorage.getItem("nodal-token");
      const r = await fetch("/api/map", { headers: token ? { Authorization: "Bearer " + token } : {} });
      return r.json();
    });
    const shutRow = shutHead.shipments.find((s) => s.id === shutId);
    note("closed-origin-refused", {
      shutOrigin, shutId, shutChosen, shutBadge, shutTrapped,
      status: shutRow?.status, trappedFlag: shutRow?.trapped,
      namesClosure: /NO FEASIBLE DESTINATION/.test(shutText) && /ORIGIN_CLOSED/.test(shutText) && shutText.includes(shutOrigin),
    });
    if (shutChosen) problem(`a shipment at shut ${shutOrigin} was given a destination`);
    if (!/NO FEASIBLE DESTINATION/.test(shutText) || !/ORIGIN_CLOSED/.test(shutText) || !shutText.includes(shutOrigin)) {
      problem(`the closed-origin refusal does not name its cause in the summary (${shutText.slice(0, 160)})`);
    }
    if (shutBadge !== "PLANNED" || shutRow?.status !== "planned") problem(`the closed-origin shipment is ${shutBadge}/${shutRow?.status}, expected PLANNED`);
    if (!shutTrapped || !shutRow?.trapped) problem("the closed-origin shipment is not badged/flagged trapped");
    await vetPanel(page, ".overlay.right", "decision · unassignable (NO FEASIBLE DESTINATION, uniform cause)");
    await shot(page, "36-unassignable-decision");
    // This cargo is trapped because the facility it stands in was ALREADY
    // shut: it never had a booking, and the banner must not tell an operator
    // it is keeping one.
    const shutBanner = await page
      .$eval(".overlay.right .trapped-banner", (b) => b.textContent.replace(/\s+/g, " ").trim())
      .catch(() => null);
    note("trapped-banner-unbooked", { shutId, banner: shutBanner?.slice(0, 200) ?? null, booked: !!shutRow?.destination });
    if (!shutBanner) problem(`${shutId} is trapped at a shut origin but shows no banner`);
    else if (!shutRow?.destination && /keeps (the |its )?booking/i.test(shutBanner)) {
      problem(`${shutId} has no booking, but its trapped banner says it keeps one: ${shutBanner.slice(0, 160)}`);
    }
    // And the popover of a facility that is already shut must not offer to
    // close it again — the closure's own END NOW / SET END are the controls.
    await page.evaluate(
      ([lon, lat]) => window.__nodalMap.jumpTo({ center: [lon, lat], zoom: 4 }),
      [world.facilities.find((f) => f.id === shutOrigin).lon, world.facilities.find((f) => f.id === shutOrigin).lat],
    );
    await page.waitForTimeout(600);
    const shutAgain = await hub(shutOrigin).locator(".node-box").boundingBox();
    if (shutAgain) {
      await page.mouse.click(shutAgain.x + shutAgain.width / 2, shutAgain.y + shutAgain.height / 2);
      await page.waitForSelector(".popover .zone-block", { timeout: 15000 });
      await page.waitForTimeout(600);
      const footer = await page.evaluate(() => {
        const bar = document.querySelector(".popover .panel-footer");
        return bar
          ? { text: bar.textContent.replace(/\s+/g, " ").trim(), buttons: bar.querySelectorAll("button").length }
          : null;
      });
      note("shut-facility-footer", { facility: shutOrigin, footer });
      if (footer && /CLOSE FACILITY FOR/i.test(footer.text)) {
        problem(`${shutOrigin} is already closed, yet its popover offers to close it again`);
      }
      if (footer && !/END NOW|SET END/i.test(footer.text)) {
        problem(`${shutOrigin}'s popover does not point at the controls that DO apply to its closure`);
      }
      await closePopover();
    }
    // Put the camera back on the whole network: the steps below register a
    // facility at a point picked off the screen, and later ones count the arcs
    // the map RENDERS — both of which read whatever view is left here.
    await page.click(".map-controls .chip:has-text('FIT')");
    await page.waitForTimeout(600);
    await page.click(".shipment-actions button:has-text('CANCEL SHIPMENT')");
    await page.click(".shipment-actions button:has-text('CONFIRM CANCEL?')");
    await waitNotBusy(page);
    await page.waitForTimeout(1500);
    await closeDecision();
  }

  // Rebalance (confirm-once); any outcome message is fine.
  await page.click(".overlay.left .queue-actions button:has-text('REBALANCE')");
  await page.click(".overlay.left .queue-actions button:has-text('CONFIRM?')");
  await waitNotBusy(page);
  await page.waitForTimeout(1500);
  const rebalanceToast = await page.$eval(".toast", (t) => t.textContent).catch(() => "");
  note("rebalance", { rebalanceToast });
  if (!/transfer|balanced/.test(rebalanceToast)) problem(`REBALANCE gave no outcome (${rebalanceToast})`);

  // Network operations: register a facility (coords picked off the map),
  // connect it with a lane, then grow it — add a zone, resize a zone.
  await page.click(".tile-btn:has-text('NETWORK')");
  await page.waitForSelector(".network-panel", { timeout: 10000 });
  await shot(page, "17-network-panel");
  await vetPanel(page, ".overlay.center", "NETWORK · fresh form");
  await page.fill(".network-panel input[placeholder='e.g. Calgary Foothills']", "Smoke Yard");
  // Arm the pick, then Escape must cancel it (panel comes back untouched).
  await page.click(".network-panel button:has-text('PICK ON MAP')");
  await page.waitForTimeout(300);
  if (!(await page.$(".pick-hint"))) problem("armed pick shows no hint tile");
  if (await page.locator(".network-panel").isVisible()) problem("network panel still visible while picking");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  if (!(await page.locator(".network-panel").isVisible())) problem("Escape did not cancel the pick");
  // Arm again and really pick: the click's coordinates must land in the form.
  await page.click(".network-panel button:has-text('PICK ON MAP')");
  await page.waitForTimeout(300);
  const pickX = Math.round(WIDTH * 0.55);
  const pickY = Math.round(HEIGHT * 0.62);
  const expected = await page.evaluate(([x, y]) => {
    const r = window.__nodalMap.getContainer().getBoundingClientRect();
    const p = window.__nodalMap.unproject([x - r.left, y - r.top]);
    return { lat: p.lat, lon: p.lng };
  }, [pickX, pickY]);
  await page.mouse.click(pickX, pickY);
  await page.waitForTimeout(400);
  const latField = Number(await page.inputValue(".network-panel input[placeholder='lat']"));
  const lonField = Number(await page.inputValue(".network-panel input[placeholder='lon']"));
  note("pick-on-map", { expected, latField, lonField });
  if (!(Math.abs(latField - expected.lat) < 0.05) || !(Math.abs(lonField - expected.lon) < 0.05)) {
    problem(`picked coordinates missed the click: expected ${JSON.stringify(expected)}, form has ${latField},${lonField}`);
  }
  const nameKept = await page.inputValue(".network-panel input[placeholder='e.g. Calgary Foothills']");
  if (nameKept !== "Smoke Yard") problem("the facility form lost its state across the map pick");
  // What the form says when it refuses: the rule it is enforcing, in the panel,
  // with the fields it is talking about still filled in.
  const latField0 = await page.inputValue(".network-panel input[placeholder='lat']");
  await page.fill(".network-panel input[placeholder='lat']", "not-a-latitude");
  await page.click(".network-panel button:has-text('REGISTER FACILITY')");
  await page.waitForTimeout(600);
  const laneSelects = page.locator(".network-panel .field-row", { hasText: "ROUTE" }).locator("select");
  const facilityProblem = await page
    .$eval(".network-panel .reject-code", (e) => e.textContent.trim())
    .catch(() => null);
  note("network-validation", { facilityProblem });
  await vetPanel(page, ".overlay.center", "NETWORK · facility validation error");
  await shot(page, "37-network-validation");
  if (!facilityProblem) problem("REGISTER FACILITY with a nonsense latitude said nothing");
  else if (!/latitude/i.test(facilityProblem)) {
    problem(`the facility form's refusal does not name the field: ${facilityProblem}`);
  }
  // A lane needs two DISTINCT facilities; the form must say that too.
  await page.fill(".network-panel input[placeholder='lat']", latField0);
  const sameFacility = await laneSelects.nth(0).inputValue();
  await laneSelects.nth(1).selectOption(sameFacility);
  await page.click(".network-panel button:has-text('REGISTER LANE')");
  await page.waitForTimeout(600);
  const laneProblem = await page
    .$eval(".network-panel .reject-code", (e) => e.textContent.trim())
    .catch(() => null);
  note("network-lane-validation", { sameFacility, laneProblem });
  if (!laneProblem || !/distinct/i.test(laneProblem)) {
    problem(`a lane from ${sameFacility} to itself was not refused with a reason: ${laneProblem}`);
  }
  await laneSelects.nth(1).selectOption(world.facilities.find((f) => f.id !== sameFacility).id);
  await page.click(".network-panel button:has-text('REGISTER FACILITY')");
  await waitNotBusy(page);
  await page.waitForTimeout(2000);
  const facilityToast = await page.$eval(".toast", (t) => t.textContent).catch(() => "");
  const worldAfterFacility = await page.evaluate(async () => {
    const token = sessionStorage.getItem("nodal-token");
    const r = await fetch("/api/map", { headers: token ? { Authorization: "Bearer " + token } : {} });
    return r.json();
  });
  const newFacility = worldAfterFacility.facilities.find((f) => f.name === "Smoke Yard");
  diag = await mapDiagnostics(page);
  note("register-facility", { facilityToast, newFacility: newFacility?.id, hubCount: diag.hubCount });
  if (!/registered with/.test(facilityToast)) problem(`REGISTER FACILITY gave no confirmation (${facilityToast})`);
  if (!newFacility) problem("the registered facility is not in the map state");
  if (diag.hubCount !== world.facilities.length + 1) problem(`the new facility's hub is not on the map (${diag.hubCount} hubs)`);
  if (newFacility && (Math.abs(newFacility.lat - expected.lat) > 0.05 || Math.abs(newFacility.lon - expected.lon) > 0.05)) {
    problem(`the facility landed away from the pick: ${newFacility.lat},${newFacility.lon}`);
  }
  // A lane both ways to an existing hub, at the mode's computed distance/time.
  const routeSelects = page.locator(".network-panel .field-row", { hasText: "ROUTE" }).locator("select");
  await routeSelects.nth(0).selectOption(newFacility.id);
  await routeSelects.nth(1).selectOption(world.facilities[0].id);
  await page.click(".network-panel button:has-text('REGISTER LANE')");
  await waitNotBusy(page);
  await page.waitForTimeout(2000);
  const laneToast = await page.$eval(".toast", (t) => t.textContent).catch(() => "");
  note("register-lane", { laneToast });
  if (!/km/.test(laneToast) || !/registered/.test(laneToast)) problem(`REGISTER LANE gave no distance/time confirmation (${laneToast})`);
  await shot(page, "18-network-registered");
  await page.click(".network-panel .panel-header .close");
  await page.waitForTimeout(300);
  if (await page.$(".network-panel")) problem("the network panel did not close");
  // Reopening must start a fresh session: no stale coordinates from the
  // abandoned pick may survive into a later registration.
  await page.click(".tile-btn:has-text('NETWORK')");
  await page.waitForSelector(".network-panel", { timeout: 10000 });
  const staleLat = await page.inputValue(".network-panel input[placeholder='lat']");
  note("network-reopen-fresh", { staleLat });
  if (staleLat !== "") problem(`reopened network form kept stale picked coordinates (${staleLat})`);
  await page.click(".network-panel .panel-header .close");
  await page.waitForTimeout(300);
  // Grow the new facility from its popover: add a zone, then resize one.
  const newHubBox = await hub(newFacility.id).locator(".node-box").boundingBox();
  if (!newHubBox) {
    problem("the new facility's hub is not clickable on screen");
  } else {
    await page.mouse.click(newHubBox.x + newHubBox.width / 2, newHubBox.y + newHubBox.height / 2);
    await page.waitForSelector(".popover .zone-block", { timeout: 15000 });
    const zonesBefore = await page.$$eval(".popover .zone-block", (z) => z.length);
    const addRow = page.locator(".popover .field-row", { hasText: "ADD ZONE" });
    await addRow.locator("input").first().fill("30");
    await addRow.locator("button:has-text('ADD')").click();
    await waitNotBusy(page);
    await page.waitForTimeout(2000);
    const addToast = await page.$eval(".toast", (t) => t.textContent).catch(() => "");
    const zonesAfter = await page.$$eval(".popover .zone-block", (z) => z.length);
    note("add-zone", { zonesBefore, zonesAfter, addToast });
    if (!/added to/.test(addToast)) problem(`ADD ZONE gave no confirmation (${addToast})`);
    if (zonesAfter !== zonesBefore + 1) problem(`ADD ZONE did not appear in the popover (${zonesBefore} -> ${zonesAfter})`);
    await page.fill(".popover .zone-block input[placeholder='slots']", "42");
    await page.click(".popover .zone-block button:has-text('SET CAPACITY')");
    await waitNotBusy(page);
    await page.waitForTimeout(2000);
    const capToast = await page.$eval(".toast", (t) => t.textContent).catch(() => "");
    const firstZoneText = await page.$eval(".popover .zone-block", (z) => z.textContent);
    note("set-capacity", { capToast, firstZoneText: firstZoneText.slice(0, 120) });
    if (!/base capacity is now 42/.test(capToast)) problem(`SET CAPACITY gave no confirmation (${capToast})`);
    if (!/\/42 slots today/.test(firstZoneText)) problem(`the resized zone does not show /42 slots (${firstZoneText.slice(0, 120)})`);
    await shot(page, "19-new-facility-zones");
    await vetPanel(page, ".popover", `facility popover · ${newFacility.id} (just registered, zone added)`, { modal: true });
    await closePopover();
  }

  // The flows above have worked the queue all the way down — everything the
  // opening review booked, plus everything they registered and then cancelled.
  // What is left is cargo nothing can route, and OPTIMIZE ALL is only offered
  // with more than one planned shipment. Give the batch flow a real queue: two
  // coordinate-origin shipments that travel, so the plan has something to place
  // and the map has arcs to draw.
  // Inland, and several hundred km from the nearest hub in each case: an
  // origin sitting on top of the facility the plan picks draws an arc a couple
  // of pixels long, and "did the plan draw anything" then answers no for a
  // reason that has nothing to do with the plan.
  const seeds = [
    ["Smoke Batch Origin A", 39.0, -98.5], // US plains, ~700 km from DEN
    ["Smoke Batch Origin B", -31.4, -64.2], // Córdoba, ~650 km from BUE
  ];
  const seeded = [];
  while ((await plannedRowCount()) < 3 && seeds.length > 0) {
    const [label, lat, lon] = seeds.shift();
    seeded.push(await registerPlannedShipment(label, lat, lon));
  }
  note("seeded-for-batch-plan", { seeded, planned: await plannedRowCount() });

  // OPTIMIZE ALL with a BOOKED row selected: its committed record must survive.
  const bookedRow2 = await page.$(".overlay.left .row:has(.badge.allocated)");
  await bookedRow2.click();
  await page.waitForSelector(".overlay.right .winner", { timeout: 20000 });
  await page.click(".overlay.left .field-row button.primary");
  await waitNotBusy(page);
  await page.waitForTimeout(1500);
  const keptHeader = await page.$eval(".overlay.right .panel-header", (h) => h.textContent);
  const keptWinner = !!(await page.$(".overlay.right .winner"));
  note("booked-selection-survives-plan", { keptHeader, keptWinner });
  if (!keptWinner || !keptHeader.includes("COMMITTED")) problem("OPTIMIZE ALL wiped the selected booked shipment's committed record");
  // The toast must not cover the centre panels' headers.
  await page.click(".tile-btn:has-text('WHAT-IF')");
  await page.waitForTimeout(600);
  const toastOverPanel = await page.evaluate(() => {
    const header = document.querySelector(".overlay.center .panel-header");
    if (!header) return "no panel";
    const r = header.getBoundingClientRect();
    const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return header.contains(el) ? "clear" : el?.className || "?";
  });
  note("toast-vs-center-panel", { toastOverPanel });
  if (toastOverPanel !== "clear") problem(`toast covers the centre panel header (${toastOverPanel})`);
  await page.click(".tile-btn:has-text('WHAT-IF')"); // toggle off
  await page.waitForTimeout(300);
  if (await page.$(".overlay.center")) problem("WHAT-IF toggle did not close its panel");
  await page.click(".overlay.left .field-row button:has-text('DISCARD')");
  await page.waitForTimeout(500);
  await closeDecision();

  // OPTIMIZE ALL → plan → select → DISCARD → plan → COMMIT PLAN.
  await page.click(".overlay.left .field-row button.primary");
  await waitNotBusy(page);
  await page.waitForTimeout(1500);
  const planLabel = await page.$eval(".overlay.left .field-row button.primary", (b) => b.textContent);
  const planTags = await page.$$eval(".plan-tag", (t) => t.length);
  diag = await mapDiagnostics(page);
  note("plan-preview", { planLabel, planTags, plannedArcs: diag.rendered.arcs["arcs-planned"] });
  if (!planLabel.includes("COMMIT PLAN")) problem(`after OPTIMIZE ALL the button reads ${planLabel}`);
  if (!(await page.$(".overlay.left .field-row button:has-text('DISCARD')"))) problem("no DISCARD button with a plan up");
  if (!diag.rendered.arcs["arcs-planned"]) problem("plan preview rendered no planned arcs");
  if (planTags === 0) problem("queue rows show no plan destinations");
  await shot(page, "05-plan-preview");
  await vetPanel(page, ".overlay.left", "queue · batch plan up, PLAN tags");
  // A row the plan PLACED: its draft has a winner to show and a route to draw,
  // which a row the plan could not place has neither of.
  const placedId = await page.evaluate(
    () =>
      [...document.querySelectorAll(".overlay.left .row")]
        .filter((r) => r.querySelector(".plan-tag") && !r.querySelector(".plan-tag.unassigned"))
        .map((r) => r.querySelector(".row-title > span:first-child")?.textContent.trim())
        .filter(Boolean)[0] ?? null,
  );
  if (!placedId) problem("the plan placed nothing: the draft-record flow is untested");
  else {
    await page.locator(".overlay.left .row").filter({ hasText: placedId }).first().click();
    await waitForMapIdle(page, 15000);
    await page.waitForTimeout(1200);
    const panelHeader = await page.$eval(".overlay.right .panel-header", (h) => h.textContent).catch(() => "NO PANEL");
    if (!panelHeader.includes("BATCH PLAN") || !(await page.$(".overlay.right .winner"))) {
      problem("selecting a shipment during the plan did not show its batch decision");
    }
    // Selected inside the review, the draft's route is highlighted like any
    // other selection — and, like any other, drawn once. (The highlight moves
    // it off arcs-planned onto arcs-selected while it is the selection.)
    checkStacking(`${placedId} selected in plan review`, await stackedTraces(page, placedId));
  }
  await shot(page, "06-select-during-plan");
  await vetPanel(page, ".overlay.right", "decision · batch-plan draft record");
  await closeDecision();
  await page.click(".overlay.left .field-row button:has-text('DISCARD')");
  await page.waitForTimeout(800);
  const afterDiscard = await page.$eval(".overlay.left .field-row button.primary", (b) => b.textContent);
  diag = await mapDiagnostics(page);
  if (!afterDiscard.includes("OPTIMIZE ALL")) problem(`after DISCARD the button reads ${afterDiscard}`);
  if (diag.rendered.arcs["arcs-planned"]) problem("planned arcs still rendered after DISCARD");
  await page.click(".overlay.left .field-row button.primary");
  await waitNotBusy(page);
  await page.waitForTimeout(800);
  const planLabel2 = await page.$eval(".overlay.left .field-row button.primary", (b) => b.textContent);
  // "COMMIT PLAN (9/10)": what the plan can book, out of what it covers. A
  // shipment the solver could not place stays PLANNED — committing books the
  // assignable ones and leaves exactly the rest.
  const planCounts = /\((\d+)\/(\d+)\)/.exec(planLabel2);
  const expectedBooked = Number(planCounts?.[1] ?? -1);
  const expectedLeft = Number(planCounts?.[2] ?? -1) - expectedBooked;
  await page.click(".overlay.left .field-row button.primary");
  await waitNotBusy(page);
  await page.waitForTimeout(3000);
  const plannedLeft = await page.$$eval(".overlay.left .row:has(.badge.planned)", (r) => r.length);
  const commitToast = await page.$eval(".toast", (t) => t.textContent).catch(() => "");
  note("after-commit-plan", { plannedLeft, commitToast, expectedBooked, expectedLeft });
  if (plannedLeft !== expectedLeft) {
    problem(
      `${plannedLeft} planned shipments remain after COMMIT PLAN, expected ${expectedLeft} ` +
        `(the plan booked ${expectedBooked} of ${planCounts?.[2]})`,
    );
  }
  if (!commitToast.includes(`${expectedBooked} of`)) problem(`commit toast reports the wrong count: ${commitToast}`);
  await shot(page, "07-after-commit-plan");
  // What the plan booked out of a hub the world has SHUT: such a shipment is
  // trapped the instant it is booked. Recorded, not asserted — the choice is
  // the engine's, and the UI reports it faithfully either way (the badge shows
  // up on these rows too) — but the run must never hide it.
  const bookedInAShutHub = await page.evaluate(async (closed) => {
    const token = sessionStorage.getItem("nodal-token");
    const r = await fetch("/api/map", { headers: token ? { Authorization: "Bearer " + token } : {} });
    const body = await r.json();
    return body.shipments
      .filter((s) => s.status === "allocated" && closed.includes(s.origin_facility_id))
      .map((s) => `${s.id}@${s.origin_facility_id}${s.trapped ? " (trapped)" : ""}`);
  }, [...closedFacilities]);
  const newlyBookedInAShutHub = bookedInAShutHub.filter(
    (entry) => !trappedCargo.some((s) => entry.startsWith(`${s.id}@`)),
  );
  note("booked-out-of-a-shut-facility", { bookedInAShutHub, newlyBookedInAShutHub });

  // Facility popover + CLOSE FACILITY on the hub picked by id at load: open,
  // and never the one the world already shut. Then the disruption row opens it
  // and END NOW clears it — the cargo the closure trapped included.
  const closedId = disruptTarget.id;
  await page.evaluate(
    ([lon, lat]) => window.__nodalMap.jumpTo({ center: [lon, lat], zoom: 4 }),
    [disruptTarget.lon, disruptTarget.lat],
  );
  await page.waitForTimeout(900);
  const disruptBox = await hub(closedId).locator(".node-box").boundingBox();
  if (!disruptBox) throw new Error(`the hub to close (${closedId}) is not on screen`);
  await page.mouse.click(disruptBox.x + disruptBox.width / 2, disruptBox.y + disruptBox.height / 2);
  await page.waitForSelector(".popover", { timeout: 15000 });
  await page.waitForTimeout(1200);
  const openedForDisrupt = await popoverId();
  if (openedForDisrupt !== closedId) problem(`the disruption flow opened ${openedForDisrupt}, expected ${closedId}`);
  const facilityState = await page.$eval(".popover .panel-header .micro", (e) => e.textContent.trim());
  if (/^CLOSED/.test(facilityState)) problem(`the disruption flow is about to close ${closedId}, which is already closed`);
  // Whose goods are standing there RIGHT NOW (the queue has moved since load):
  // exactly those are what this closure traps.
  const standing = await page.evaluate(async (id) => {
    const token = sessionStorage.getItem("nodal-token");
    const r = await fetch("/api/map", { headers: token ? { Authorization: "Bearer " + token } : {} });
    const body = await r.json();
    return body.shipments
      .filter((s) => s.status === "allocated" && !s.trapped && s.origin_facility_id === id)
      .map((s) => s.id);
  }, closedId);
  await shot(page, "08-facility");
  await vetPanel(page, ".popover", `facility popover · ${closedId} (open, about to be closed)`, { modal: true });
  await page.click(".popover .panel-footer button.primary");
  await waitNotBusy(page);
  await page.waitForTimeout(2500);
  const disruptionsBefore = await page.$$eval(".disruption-row", (r) => r.length);
  const disruptToast = await page.$eval(".toast", (t) => t.textContent).catch(() => "");
  note("after-disrupt", { closedId, facilityState, standing, disruptionsBefore, toast: disruptToast });
  if (await page.$(".popover")) problem("popover survived the disruption command");
  if (standing.length === 0) {
    // Nothing was standing at this hub, so the no-trap notice is what gets
    // asserted: the clause must not appear when nothing was stranded.
    note("disrupt-no-trap", { closedId, why: "no allocated cargo stands at this facility" });
    if (/trapped at/.test(disruptToast)) problem(`closing ${closedId} stranded nothing, yet the notice claims a trap: ${disruptToast}`);
  } else {
    if (!disruptToast.includes(`trapped at ${closedId}`)) {
      problem(
        `closing ${closedId} stranded ${standing.join(", ")}, but the notice does not say so: ${disruptToast}`,
      );
    }
    if (!/manual clearing required/.test(disruptToast)) {
      problem(`the trap notice does not say the clearing is manual: ${disruptToast}`);
    }
    await vetPanel(page, ".toast", "toast · a closure's trapped clause");
    // And the queue badges it there and then — no reload, no re-selection.
    const stuckRow = page.locator(".overlay.left .row").filter({ hasText: standing[0] }).first();
    await stuckRow.scrollIntoViewIfNeeded();
    const badgedNow = await stuckRow.locator(".badge.trapped").count();
    const lineNow = await stuckRow.locator(".trapped-line").count();
    note("newly-trapped-row", { id: standing[0], badgedNow, lineNow });
    if (!badgedNow) problem(`${standing[0]} was trapped by the closure but its row shows no TRAPPED badge`);
    if (!lineNow) problem(`${standing[0]} was trapped by the closure but its row does not say where`);
    await shot(page, "08b-trapped-by-closure");
  }
  const disruptionRow = page.locator(".disruption-row").filter({ hasText: closedId }).first();
  if (!(await disruptionRow.count())) problem(`no disruption row for the closed facility ${closedId}`);
  else {
    await disruptionRow.click();
    await page.waitForSelector(".popover", { timeout: 15000 });
    const opened = await popoverId();
    const endButton = page.locator(".popover button", { hasText: "END NOW" }).first();
    note("disruption-row-opens-facility", { opened, hasEnd: (await endButton.count()) > 0 });
    if (opened !== closedId) problem(`disruption row opened ${opened}, expected ${closedId}`);
    await endButton.click();
    await waitNotBusy(page);
    await page.waitForTimeout(2500);
    const disruptionsAfter = await page.$$eval(".disruption-row", (r) => r.length);
    note("after-end-disruption", { disruptionsAfter, toast: await page.$eval(".toast", (t) => t.textContent).catch(() => null) });
    if (disruptionsAfter !== disruptionsBefore - 1) problem(`END NOW left ${disruptionsAfter} disruptions (had ${disruptionsBefore})`);
    await closePopover();
    // Reopening the facility releases what it trapped: the badge goes from the
    // row, and the read model stops calling the cargo trapped.
    if (standing.length) {
      await page.waitForTimeout(1500);
      const stuckRow = page.locator(".overlay.left .row").filter({ hasText: standing[0] }).first();
      await stuckRow.scrollIntoViewIfNeeded();
      const stillBadged = await stuckRow.locator(".badge.trapped").count();
      const stillTrapped = await page.evaluate(async (ids) => {
        const token = sessionStorage.getItem("nodal-token");
        const r = await fetch("/api/map", { headers: token ? { Authorization: "Bearer " + token } : {} });
        const body = await r.json();
        return body.shipments.filter((s) => ids.includes(s.id) && s.trapped).map((s) => s.id);
      }, standing);
      note("trap-cleared", { closedId, standing, stillBadged, stillTrapped });
      if (stillBadged) problem(`${standing[0]} still badges TRAPPED after ${closedId} reopened`);
      if (stillTrapped.length) problem(`${stillTrapped.join(", ")} is still trapped after ${closedId} reopened`);
    }
  }

  // 3D from a regional view: dive to the NEAREST hub; 2D restores the view.
  await page.evaluate(() => window.__nodalMap.jumpTo({ center: [5, 50], zoom: 5 }));
  await page.waitForTimeout(600);
  await page.click(".map-controls .chip:has-text('3D')");
  await page.waitForTimeout(2500);
  await waitForMapIdle(page, 20000);
  diag = await mapDiagnostics(page);
  const diveCenter = await page.evaluate(() => window.__nodalMap.getCenter());
  note("3d", { zoom: diag.zoom, pitch: diag.pitch, zones: diag.rendered.zones, center: diveCenter });
  if (!diag.rendered.zones) problem("3D view renders no zone extrusions");
  if (Math.abs(diveCenter.lng - 5) > 15 || Math.abs(diveCenter.lat - 50) > 15) {
    problem(`3D dive left the region the operator was looking at: ${JSON.stringify(diveCenter)}`);
  }
  await shot(page, "09-3d");
  await page.click(".map-controls .chip:has-text('2D')");
  await page.waitForTimeout(600);
  const back = await page.evaluate(() => ({ zoom: window.__nodalMap.getZoom(), pitch: window.__nodalMap.getPitch() }));
  if (Math.abs(back.zoom - 5) > 0.01 || back.pitch !== 0) problem(`2D did not restore the pre-dive view: ${JSON.stringify(back)}`);
  await page.click(".map-controls .chip:has-text('FIT')");

  // Panel toggles: each opens its centre panel and closes on a second click.
  for (const tab of ["FORECAST", "SCHEDULES", "WHAT-IF"]) {
    await page.click(`.tile-btn:has-text('${tab}')`);
    await page.waitForTimeout(1500);
    await shot(page, `10-panel-${tab.toLowerCase().replace("-", "")}`);
    if (!(await page.$(".overlay.center"))) problem(`${tab} toggle opened no panel`);
    await vetPanel(page, ".overlay.center", `${tab} · freshly opened`);
    const coveredByTab = await mapControlsCovered(page);
    if (coveredByTab.length) {
      note(`map-controls-under-${tab}`, { covered: coveredByTab });
      for (const hidden of coveredByTab) {
        problem(`the map control "${hidden.control}" is ${hidden.why} with ${tab} open`);
      }
    }
    const text = await page.$eval(".overlay.center", (p) => p.textContent);
    if (/\d{4}-\d{2}-\d{2}T/.test(text)) problem(`${tab} panel still shows raw ISO timestamps`);
    await page.click(`.tile-btn:has-text('${tab}')`);
    await page.waitForTimeout(300);
    if (await page.$(".overlay.center")) problem(`${tab} toggle did not close its panel`);
  }

  // SCHEDULES is one row per WINDOW the goods occupy (§7.9), not one per
  // shipment: every dwell on the way is a booking, and a delivery's last mile
  // is the movement that ends it. The panel must render every row the API
  // serves, say what each row IS in the same words the decision panel uses,
  // and — now that shipment ids repeat down the table — key its rows by
  // something unique, which React complains about loudly when it is not.
  const consoleBeforeSchedules = report.console.length;
  await page.click(".tile-btn:has-text('SCHEDULES')");
  await page.waitForSelector(".overlay.center table.data tbody tr", { timeout: 20000 });
  await page.waitForTimeout(800);
  const schedule = await page.evaluate(async () => {
    const token = sessionStorage.getItem("nodal-token");
    const r = await fetch("/api/schedules", { headers: token ? { Authorization: "Bearer " + token } : {} });
    return r.json();
  });
  const scheduleRows = await page.$$eval(".overlay.center table.data tbody tr", (rows) =>
    rows.map((tr) => ({
      shipment: tr.querySelector("td")?.textContent.trim() ?? "",
      role: tr.querySelector(".movement-role")?.textContent.trim() ?? "",
      text: tr.textContent.trim(),
    })),
  );
  const scheduleHead = await page.$eval(".overlay.center .panel-header", (h) =>
    h.textContent.replace(/\s+/g, " ").trim(),
  );
  const scheduleText = await page.$eval(".overlay.center", (p) => p.textContent);
  const movements = schedule.movements ?? [];
  const scheduleShipments = new Set(movements.map((m) => m.shipment_id)).size;
  const countRole = (rows, role) => rows.filter((r) => r.role === role).length;
  const transitWindows = movements.filter((m) => m.role === "transit").length;
  const lastMileWindows = movements.filter((m) => m.role === "last_mile").length;
  // A duplicate React key is exactly the defect one-row-per-shipment keying
  // would cause here, and it only ever shows up in the console.
  const keyWarnings = report.console
    .slice(consoleBeforeSchedules)
    .filter((c) => /key/i.test(c.text))
    .map((c) => `${c.type}: ${c.text.slice(0, 160)}`);
  const unlabelled = scheduleRows.filter((r) => !r.role).map((r) => r.shipment);
  const squashed = await overflowingCells(page, ".overlay.center table.data td");
  note("schedules-windows", {
    movements: movements.length,
    shipments: scheduleShipments,
    renderedRows: scheduleRows.length,
    roles: [...new Set(scheduleRows.map((r) => r.role))],
    transitWindows,
    crossDockRows: countRole(scheduleRows, "CROSS-DOCK"),
    lastMileWindows,
    lastMileRows: countRole(scheduleRows, "LAST MILE"),
    header: scheduleHead.slice(0, 120),
    squashed,
    keyWarnings,
  });
  await shot(page, "10b-schedules-windows");
  await vetPanel(page, ".overlay.center", "SCHEDULES · every booked window");
  if (movements.length <= scheduleShipments) {
    problem(
      `SCHEDULES served ${movements.length} rows for ${scheduleShipments} shipments: no booking reports more than one window`,
    );
  }
  if (scheduleRows.length !== movements.length) {
    problem(`SCHEDULES renders ${scheduleRows.length} rows for the ${movements.length} the API served`);
  }
  if (unlabelled.length) problem(`SCHEDULES rows without a role: ${unlabelled.join(", ")}`);
  if (!transitWindows) problem("no transit window in the schedule: the multi-window rows are untested");
  else if (countRole(scheduleRows, "CROSS-DOCK") !== transitWindows) {
    problem(
      `SCHEDULES shows ${countRole(scheduleRows, "CROSS-DOCK")} CROSS-DOCK rows for ${transitWindows} transit windows`,
    );
  }
  if (countRole(scheduleRows, "LAST MILE") !== lastMileWindows) {
    problem(
      `SCHEDULES shows ${countRole(scheduleRows, "LAST MILE")} LAST MILE rows for ${lastMileWindows} last miles`,
    );
  }
  // A last mile ends at the customer: the row has to name them, not just the
  // hub the van left from.
  const lastMile = movements.find((m) => m.role === "last_mile" && m.customer_label);
  if (lastMile) {
    const row = scheduleRows.find((r) => r.role === "LAST MILE" && r.shipment.startsWith(lastMile.shipment_id));
    if (!row) problem(`${lastMile.shipment_id} has a last mile in the schedule but no LAST MILE row on screen`);
    else if (!row.text.includes(lastMile.customer_label)) {
      problem(`${lastMile.shipment_id}'s LAST MILE row does not name its customer: ${row.text.slice(0, 120)}`);
    }
  }
  // The count in the header has to read as what it counts.
  if (!scheduleHead.includes(`${movements.length} MOVEMENTS`) || !scheduleHead.includes(`${scheduleShipments} SHIPMENTS`)) {
    problem(
      `the SCHEDULES header does not state ${movements.length} movements over ${scheduleShipments} shipments: ${scheduleHead.slice(0, 120)}`,
    );
  }
  if (/\d{4}-\d{2}-\d{2}T/.test(scheduleText)) problem("SCHEDULES still shows raw ISO timestamps");
  if (keyWarnings.length) problem(`SCHEDULES logs a React key warning: ${keyWarnings.join(" | ")}`);
  if (squashed.length) {
    problem(`${squashed.length} SCHEDULES cells are squeezed past their column: ${squashed.slice(0, 3).join(" | ")}`);
  }
  await page.click(".tile-btn:has-text('SCHEDULES')");
  await page.waitForTimeout(300);

  // What-if runs with the example, plus a hypothetical closure over cargo that
  // is standing still: trapped cargo never re-routes, so the fork's totals are
  // the only place it can be seen — the chip has to be there.
  await page.click(".tile-btn:has-text('WHAT-IF')");
  await page.click(".overlay.center button:has-text('EXAMPLE')");
  await page.click(".overlay.center button:has-text('ADD')");
  if (standing.length) {
    await page.fill(".overlay.center input[placeholder*='cut ZON-2']", `close ${closedId} 7d`);
    await page.click(".overlay.center button:has-text('ADD')");
  }
  // Whose goods stand at the hub the fork shuts, at the moment it is forked:
  // exactly the cargo that hypothesis strands, so exactly what the fork owes a
  // report on. Read fresh — the live closure has been ended and its traps
  // released since `standing` was taken.
  const standingAtFork = standing.length
    ? await page.evaluate(async (id) => {
        const token = sessionStorage.getItem("nodal-token");
        const r = await fetch("/api/map", { headers: token ? { Authorization: "Bearer " + token } : {} });
        const body = await r.json();
        return body.shipments
          .filter((s) => s.status === "allocated" && !s.trapped && s.origin_facility_id === id)
          .map((s) => s.id);
      }, closedId)
    : [];
  const forkResponse = page.waitForResponse(
    (r) => r.url().includes("/api/commands/whatif"),
    { timeout: 120000 },
  );
  await page.click(".overlay.center button:has-text('RUN FORK')");
  const forkBody = await (await forkResponse).json().catch(() => null);
  await page.waitForSelector(".overlay.center table.data", { timeout: 120000 });
  await shot(page, "11-whatif");
  await vetPanel(page, ".overlay.center", "WHAT-IF · fork result");
  const whatifText = await page.$eval(".overlay.center", (p) => p.textContent);
  if (/\d{4}-\d{2}-\d{2}T/.test(whatifText)) problem("what-if result still shows raw ISO timestamps");
  // The chip is the ONLY place a fork can report cargo it would strand (nothing
  // re-routes it, so it never appears as a change). What the panel shows is
  // checked against the fork the API actually answered with.
  const forkTrapped = (forkBody?.reopts ?? []).flatMap((r) => r.trapped ?? []);
  const chip = await page
    .$eval(".overlay.center .chip.alarm", (c) => ({ text: c.textContent.trim(), title: c.title }))
    .catch(() => null);
  const unreported = standingAtFork.filter((id) => !forkTrapped.some((t) => t.shipment_id === id));
  note("whatif-trapped-chip", {
    hypothesis: standing.length ? `close ${closedId} 7d` : "(example only)",
    liveClosureTrapped: standing,
    standingAtFork,
    forkTrapped: forkTrapped.map((t) => `${t.shipment_id}@${t.facility_id}`),
    unreported,
    chip,
  });
  if (forkTrapped.length) {
    if (!chip) problem(`the fork strands ${forkTrapped.length} shipment(s) but the panel shows no trapped chip`);
    else {
      if (!chip.text.includes(`${forkTrapped.length} TRAPPED`)) {
        problem(`the what-if trapped chip reads "${chip.text}" for ${forkTrapped.length} stranded`);
      }
      if (!/trapped at .+ — manual clearing required/.test(chip.title)) {
        problem(`the what-if trapped chip does not say where: ${chip.title}`);
      }
    }
  } else if (chip) {
    problem(`the fork stranded nothing, but the panel shows a trapped chip: ${chip.text}`);
  }
  // A fork that strands cargo and reports none is the worst answer the preview
  // can give: the operator reads a clean plan for a network that is about to
  // strand goods. The same closure, run for real, trapped exactly these (see
  // "after-disrupt"), so the fork owes an answer for every one of them.
  if (unreported.length) {
    problem(
      `the fork closes ${closedId} over ${unreported.join(", ")}, which stand there, but reports them stranded nowhere`,
    );
  }
  // A hypothesis the engine cannot parse: the panel has to SAY so, in the
  // panel, and keep the result it had rather than blanking with no explanation.
  await page.fill(".overlay.center input[placeholder*='cut ZON-2']", "not-a-command 9z");
  await page.click(".overlay.center button:has-text('ADD')");
  armed.whatif422 = true;
  const badFork = page.waitForResponse((r) => r.url().includes("/api/commands/whatif"), { timeout: 60000 });
  await page.click(".overlay.center button:has-text('RUN FORK')");
  const badStatus = (await badFork).status();
  await page.waitForSelector(".overlay.center .reject-code", { timeout: 30000 }).catch(() => null);
  await page.waitForTimeout(600);
  const forkError = await page.$eval(".overlay.center .reject-code", (e) => e.textContent.trim()).catch(() => null);
  const forkButton = await page
    .$eval(".overlay.center button.primary", (b) => ({ text: b.textContent.trim(), disabled: b.disabled }))
    .catch(() => null);
  armed.whatif422 = false;
  note("whatif-error", { badStatus, forkError: forkError?.slice(0, 160) ?? null, forkButton });
  await vetPanel(page, ".overlay.center", "WHAT-IF · unparseable hypothesis");
  await shot(page, "31-whatif-error");
  if (!forkError) problem("an unparseable what-if said nothing in the panel");
  else if (!/not-a-command/.test(forkError)) {
    problem(`the what-if error does not quote what it could not parse: ${forkError.slice(0, 160)}`);
  }
  if (forkButton?.disabled) problem("the what-if panel is stuck FORKING after an error");
  await page.click(".tile-btn:has-text('WHAT-IF')");
  // QUEUE toggle hides the side panels; the map keeps every hub.
  await page.click(".tile-btn:has-text('QUEUE')");
  await page.waitForTimeout(400);
  if (await page.$(".overlay.left")) problem("QUEUE toggle did not hide the queue");
  await shot(page, "12-map-only");
  await page.click(".tile-btn:has-text('QUEUE')");
  await page.waitForTimeout(400);
  if (!(await page.$(".overlay.left"))) problem("QUEUE toggle did not bring the queue back");

  // Replay: the bar, nothing on top of LIVE, notches show real history, the
  // header chip returns to live, closing the toggle returns to live.
  await page.click(".tile-btn:has-text('REPLAY')");
  await page.waitForTimeout(500);
  const liveCovered = await page.evaluate(() => {
    const b = [...document.querySelectorAll(".bottom-bar button")].find((x) => x.textContent.trim() === "LIVE");
    const r = b.getBoundingClientRect();
    const el = document.elementFromPoint(r.right - 3, r.top + r.height / 2);
    return b.contains(el) ? "clear" : el?.className || "?";
  });
  if (liveCovered !== "clear") problem(`something covers the LIVE button: ${liveCovered}`);
  // The bar must not cover the side panels' bottoms (the disruption board).
  const barOverPanels = await page.evaluate(() => {
    const bar = document.querySelector(".bottom-bar").getBoundingClientRect();
    return [...document.querySelectorAll(".overlay.left, .overlay.right")]
      .map((p) => p.getBoundingClientRect())
      .some((r) => r.bottom > bar.top + 1);
  });
  if (barOverPanels) problem("the replay bar overlaps the side panels");
  const slider = await page.$(".bottom-bar input[type=range]");
  const sliderBefore = (await slider.boundingBox()).width;
  await slider.focus();
  await page.keyboard.press("ArrowLeft");
  await page.waitForTimeout(2500);
  const sliderAfter = (await slider.boundingBox()).width;
  note("replay-slider-stability", { sliderBefore, sliderAfter });
  if (Math.abs(sliderAfter - sliderBefore) > 2) problem(`the replay slider resized on scrub (${sliderBefore} -> ${sliderAfter})`);
  diag = await mapDiagnostics(page);
  if (diag.markerCount === 0) problem("one replay notch back from live blanks the map");
  await vetPanel(page, ".bottom-bar", "replay bar · one notch back from live");
  await vetPanel(page, ".overlay.left", "queue · one notch back from live");
  // Trapped cargo READ in history. The alarm still stands — the cargo really
  // was stuck at that moment — but the sentence under it must not send the
  // operator to a CANCEL SHIPMENT button that a replay does not have.
  // This can legitimately find nothing to look at: a world whose closures are
  // stamped at the log's LAST timestamp has no replayed stamp at which anything
  // is trapped (the demo is such a world today — every command takes the last
  // event's ts). It says so rather than passing quietly, and starts asserting
  // the moment a world separates its closure from its head in time.
  if (trappedCargo.length) {
    const stuck = trappedCargo[0];
    const row = page.locator(".overlay.left .row").filter({ hasText: stuck.id }).first();
    if (await row.count()) {
      await row.scrollIntoViewIfNeeded();
      await row.click();
      await page.waitForTimeout(1500);
      const replayTrapped = await page.evaluate(() => ({
        banner:
          document.querySelector(".overlay.right .trapped-banner")?.textContent.replace(/\s+/g, " ").trim() ??
          null,
        cancels: [...document.querySelectorAll(".overlay.right button")].filter((b) =>
          /CANCEL SHIPMENT/.test(b.textContent),
        ).length,
      }));
      note("trapped-banner-in-replay", { id: stuck.id, ...replayTrapped, banner: replayTrapped.banner?.slice(0, 180) ?? null });
      if (!replayTrapped.banner) {
        note("trapped-banner-in-replay", { skipped: `${stuck.id} is not trapped at this replayed stamp` });
      } else {
        if (replayTrapped.cancels) {
          problem(`the decision panel offers ${replayTrapped.cancels} CANCEL SHIPMENT buttons inside a replay`);
        } else if (/CANCEL SHIPMENT below/i.test(replayTrapped.banner)) {
          problem(
            `the trapped banner sends the operator to CANCEL SHIPMENT inside a replay, where there is no ` +
              `such button: ${replayTrapped.banner.slice(0, 160)}`,
          );
        }
        await vetPanel(page, ".overlay.right", `decision · trapped ${stuck.id} (in replay, read-only)`);
        await shot(page, "13b-trapped-in-replay");
      }
      await closeDecision();
    }
  }
  await page.keyboard.press("Home");
  await page.waitForTimeout(2500);
  diag = await mapDiagnostics(page);
  note("replay-start", { markerCount: diag.markerCount });
  await shot(page, "13-replay-start");
  await vetPanel(page, ".bottom-bar", "replay bar · at the log start");
  await vetPanel(page, ".overlay.left", "queue · replayed to the log start");
  // An empty queue is a state, not a blank panel.
  const emptyQueue = await page.evaluate(() => {
    const panel = document.querySelector(".overlay.left");
    if (!panel) return null;
    return {
      rows: panel.querySelectorAll(".panel-body .row").length,
      body: panel.querySelector(".panel-body")?.textContent.replace(/\s+/g, " ").trim() ?? "",
    };
  });
  note("empty-queue", emptyQueue);
  if (emptyQueue && emptyQueue.rows === 0 && emptyQueue.body === "") {
    problem("the queue is empty at this point in the log and says nothing at all");
  }
  // Under a REPLAY header, a what-if fork must run AT the replayed time, and
  // the network form's PICK ON MAP must be as disabled as its submits.
  const replayStamp = await page.$eval(".header-right .replay-chip", (c) => c.textContent);
  const replayTime = (replayStamp.match(/[A-Z][a-z]{2} \d+ · \d{2}:\d{2}/) || [""])[0];
  await page.click(".tile-btn:has-text('WHAT-IF')");
  await page.click(".overlay.center button:has-text('EXAMPLE')");
  await page.click(".overlay.center button:has-text('ADD')");
  await page.click(".overlay.center button:has-text('RUN FORK')");
  await page.waitForSelector(".overlay.center table.data", { timeout: 120000 });
  const forkStamp = await page.$eval(".overlay.center .rendered", (p) => p.textContent.slice(0, 200));
  note("whatif-in-replay", { replayTime, forkStamp: forkStamp.slice(0, 80) });
  if (!replayTime || !forkStamp.includes(replayTime)) {
    problem(`what-if under REPLAY ${replayTime} forked at a different time: ${forkStamp.slice(0, 80)}`);
  }
  await page.click(".tile-btn:has-text('WHAT-IF')");
  await page.waitForTimeout(300);
  // A facility popover under a REPLAY header is a READING of that facility as
  // it was. Nothing may be disrupted, resized or added from inside history —
  // the same rule the decision panel's lifecycle row already follows.
  // Whatever hub the replayed world actually HAS on screen: at the log's start
  // most of the network does not exist yet, so a hub picked from the live map
  // would simply never resolve.
  const replayHub = await page.evaluate((hubSelector) => {
    const canvas = document.querySelector(".map-canvas").getBoundingClientRect();
    for (const marker of document.querySelectorAll(hubSelector)) {
      const r = marker.querySelector(".node-box")?.getBoundingClientRect();
      if (!r) continue;
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      if (cx > canvas.left + 80 && cx < canvas.right - 80 && cy > canvas.top + 80 && cy < canvas.bottom - 80) {
        return { id: marker.querySelector(".node-label")?.textContent.trim() ?? "?", x: cx, y: cy };
      }
    }
    return null;
  }, HUB);
  if (!replayHub) {
    note("replay-popover", { skipped: "no hub is on screen at this replay stamp" });
  } else {
    await page.mouse.click(replayHub.x, replayHub.y);
    await page.waitForSelector(".popover", { timeout: 15000 });
    await page.waitForTimeout(800);
    const replayControls = await page.evaluate(() => {
      const pop = document.querySelector(".popover");
      return {
        buttons: [...pop.querySelectorAll("button")].map((b) => b.textContent.trim()),
        inputs: pop.querySelectorAll("input, select").length,
        footer: !!pop.querySelector(".panel-footer"),
      };
    });
    const live = replayControls.buttons.filter((b) => b !== "CLOSE ✕");
    note("replay-popover", { hub: replayHub.id, ...replayControls });
    await vetPanel(page, ".popover", `facility popover · ${replayHub.id} (in replay, read-only)`, { modal: true });
    await shot(page, "33-replay-facility-popover");
    if (live.length) problem(`the facility popover offers ${live.join(", ")} inside a replay`);
    if (replayControls.footer) problem("the facility popover keeps its CLOSE FACILITY footer inside a replay");
    if (replayControls.inputs) problem(`the facility popover keeps ${replayControls.inputs} editable fields inside a replay`);
    await closePopover();
  }
  // A plan pending at the replayed stamp is history: readable, never actionable.
  const replayPlan = await page.evaluate(() => ({
    planActions: document.querySelectorAll(".overlay.left .plan-actions button").length,
    planNote: document.querySelector(".overlay.left .plan-actions .micro")?.textContent.replace(/\s+/g, " ").trim() ?? null,
  }));
  note("replay-plan-readonly", replayPlan);
  if (replayPlan.planActions) problem(`plan review offers ${replayPlan.planActions} actions inside a replay`);
  await page.click(".tile-btn:has-text('NETWORK')");
  await page.waitForSelector(".network-panel", { timeout: 10000 });
  await vetPanel(page, ".overlay.center", "NETWORK · in replay (submits disabled)");
  const pickDisabled = await page.$eval(".network-panel button:has-text('PICK ON MAP')", (b) => b.disabled);
  note("pick-disabled-in-replay", { pickDisabled });
  if (!pickDisabled) problem("PICK ON MAP is enabled during replay while every submit is disabled");
  await page.click(".tile-btn:has-text('NETWORK')");
  await page.waitForTimeout(300);
  await page.click(".header-right .replay-chip");
  await page.waitForTimeout(2000);
  diag = await mapDiagnostics(page);
  if (diag.markerCount === 0) problem("returning to LIVE via the header chip left the map empty");
  if (await page.$(".header-right .replay-chip")) problem("still in replay after the header chip");
  await page.click(".tile-btn:has-text('REPLAY')"); // close the bar
  await page.waitForTimeout(300);
  if (await page.$(".bottom-bar")) problem("REPLAY toggle did not close the bar");

  // Manual clearing — the only path the system offers for trapped cargo:
  // cancelling releases what it holds, the row leaves the queue and the hub
  // stops calling itself trapped. This DRIVES the world, so it runs last.
  if (trappedCargo.length) {
    const stuck = trappedCargo[0];
    const row = page.locator(".overlay.left .row").filter({ hasText: stuck.id }).first();
    await row.scrollIntoViewIfNeeded();
    await row.click();
    await page.waitForSelector(".overlay.right .trapped-banner", { timeout: 20000 });
    await page.click(".shipment-actions button:has-text('CANCEL SHIPMENT')");
    await page.click(".shipment-actions button:has-text('CONFIRM CANCEL?')");
    await waitNotBusy(page);
    await page.waitForTimeout(2500);
    const clearToast = await page.$eval(".toast", (t) => t.textContent).catch(() => "");
    const stillListed = await page.locator(".overlay.left .row").filter({ hasText: stuck.id }).count();
    const hubStillTrapped = await page.evaluate(
      ([id, hubSelector]) =>
        [...document.querySelectorAll(hubSelector)].some(
          (m) =>
            m.querySelector(".node-label")?.textContent.trim() === id &&
            m.classList.contains("trapped"),
        ),
      [stuck.origin_facility_id, HUB],
    );
    // What is stuck there NOW, not at load: the run books shipments of its own,
    // and one whose origin is the shut hub is trapped the moment it is booked.
    // The hub must agree with the read model either way.
    const trappedThereNow = await page.evaluate(async (id) => {
      const token = sessionStorage.getItem("nodal-token");
      const r = await fetch("/api/map", { headers: token ? { Authorization: "Bearer " + token } : {} });
      const body = await r.json();
      return body.shipments.filter((s) => s.trapped && s.origin_facility_id === id).map((s) => s.id);
    }, stuck.origin_facility_id);
    note("clear-trapped-by-cancelling", {
      id: stuck.id,
      clearToast,
      stillListed,
      hubStillTrapped,
      trappedThereNow,
    });
    if (stillListed) problem(`the cancelled trapped shipment ${stuck.id} is still in the queue`);
    if (!/reservations are released/.test(clearToast)) {
      problem(`clearing trapped cargo does not report the release: ${clearToast}`);
    }
    if (!trappedThereNow.length && hubStillTrapped) {
      problem(`${stuck.origin_facility_id} still shows trapped cargo after the last shipment stuck there was cleared`);
    }
    if (trappedThereNow.length && !hubStillTrapped) {
      problem(
        `${trappedThereNow.join(", ")} is trapped at ${stuck.origin_facility_id}, but the hub no longer says so`,
      );
    }
    await shot(page, "26-after-clearing");
    await vetPanel(page, ".overlay.left", "queue · after the trapped cargo was cleared");
  }

} catch (err) {
  report.fatal = String(err.stack || err);
  await shot(page, "99-fatal").catch(() => {});
}

// UI scale: A+ grows the root font size and the panels; the map re-fits so
// no hub hides under a panel; markers stay put; nothing paints over the
// panels or steals their clicks. Needs planned shipments (OPTIMIZE ALL).
async function scaleChecks(world, trappedCargo) {
  const unitBefore = (await geometry(page)).unit;
  for (let i = 0; i < 3; i++) {
    await page.click(".scale-control button[aria-label='Larger']");
    await page.waitForTimeout(500);
  }
  await page.waitForTimeout(800);
  const geoAfter = await geometry(page);
  const dev = await markerDeviation(page, world.facilities);
  const hiddenAtScale = await page.evaluate((HUB) => {
    const panels = [...document.querySelectorAll(".overlay.left, .overlay.right")].map((p) => p.getBoundingClientRect());
    const out = [];
    for (const m of document.querySelectorAll(HUB)) {
      const r = m.querySelector(".node-box").getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      if (panels.some((p) => cx > p.left && cx < p.right && cy > p.top && cy < p.bottom)) {
        out.push(m.querySelector(".node-label").textContent);
      }
    }
    return out;
  }, HUB);
  note("scale-up", { unitBefore, unitAfter: geoAfter.unit, leftPanel: geoAfter.left, maxDev: dev.maxDev, hiddenAtScale });
  if (geoAfter.unit <= unitBefore) problem("A+ did not enlarge the interface");
  if (dev.maxDev > 1) problem(`marker placement drifted after scaling: ${dev.maxDev}px`);
  if (hiddenAtScale.length) problem(`hubs under a panel after scaling to 150%: ${hiddenAtScale.join(",")}`);
  await shot(page, "14-scaled-up");
  await vetPanel(page, ".app-header", "header · 150% (A+ extreme)");
  await vetPanel(page, ".overlay.left", "queue · 150%");
  // The alarm badge sits beside the status badge on the title row: at 150% the
  // pair must not crowd the shipment id off it, and neither the badges nor the
  // trapped line may run past the panel.
  if (trappedCargo.length) {
    const stuckRow = page.locator(".overlay.left .row").filter({ hasText: trappedCargo[0].id }).first();
    await stuckRow.scrollIntoViewIfNeeded();
    const fit = await stuckRow.evaluate((el) => {
      const title = el.querySelector(".row-title");
      const id = title.querySelector("span:first-child").getBoundingClientRect();
      const badges = title.querySelector(".row-badges").getBoundingClientRect();
      const line = el.querySelector(".trapped-line").getBoundingClientRect();
      const row = el.getBoundingClientRect();
      return {
        gap: Math.round(badges.left - id.right),
        badgesHeight: Math.round(badges.height),
        titleHeight: Math.round(title.getBoundingClientRect().height),
        overflow: Math.round(Math.max(badges.right - row.right, line.right - row.right)),
      };
    });
    note("trapped-row-at-150", { id: trappedCargo[0].id, ...fit });
    if (fit.gap < 4) problem(`the TRAPPED badge crowds the shipment id at 150% (${fit.gap}px apart)`);
    if (fit.overflow > 0) problem(`the trapped row runs ${fit.overflow}px past the queue panel at 150%`);
    await shot(page, "14b-scaled-trapped-row");
    await vetPanel(page, ".overlay.left", "queue · 150% with a TRAPPED row");
  }
  // At 150%: OPTIMIZE ALL's notice must not cover DISCARD; controls must not
  // paint over an open centre panel.
  await page.click(".overlay.left .field-row button.primary");
  await waitNotBusy(page);
  await page.waitForTimeout(800);
  const discardClear = await page.evaluate(() => {
    const b = [...document.querySelectorAll(".overlay.left .field-row button")].find((x) => x.textContent.trim() === "DISCARD");
    if (!b) return "no button";
    const r = b.getBoundingClientRect();
    const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return b.contains(el) ? "clear" : el?.className || "?";
  });
  note("discard-at-150", { discardClear });
  if (discardClear !== "clear") problem(`DISCARD is covered at 150% (${discardClear})`);
  await page.click(".overlay.left .field-row button:has-text('DISCARD')");
  await page.waitForTimeout(400);
  await (await page.$(".overlay.left .row:has(.badge.allocated)")).click();
  await page.waitForTimeout(600);
  await page.click(".tile-btn:has-text('SCHEDULES')");
  await page.waitForTimeout(800);
  const controlsVsPanel = await page.evaluate(() => {
    const panel = document.querySelector(".overlay.center")?.getBoundingClientRect();
    const chips = [...document.querySelectorAll(".map-controls .chip")];
    if (!panel) return "no panel";
    for (const chip of chips) {
      const r = chip.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const inside = cx > panel.left && cx < panel.right && cy > panel.top && cy < panel.bottom;
      if (inside && chip.contains(document.elementFromPoint(cx, cy))) return `chip ${chip.textContent.trim()} paints over the panel`;
    }
    return "clear";
  });
  note("controls-vs-center-150", { controlsVsPanel });
  if (controlsVsPanel !== "clear") problem(controlsVsPanel);
  // The tightest the schedule table is ever asked to be: 150%, with both side
  // panels open beside it. It may scroll sideways there, but no column may be
  // squeezed into the one beside it.
  const squashedAt150 = await overflowingCells(page, ".overlay.center table.data td");
  const scheduleFit = await page.evaluate(() => {
    const body = document.querySelector(".overlay.center .panel-body");
    return body ? { width: Math.round(body.clientWidth), table: body.scrollWidth, scrolls: body.scrollWidth > body.clientWidth } : null;
  });
  note("schedules-at-150", { squashedAt150, scheduleFit });
  const coveredBeside = await mapControlsCovered(page);
  note("map-controls-at-150-side-panels", { covered: coveredBeside });
  for (const hidden of coveredBeside) {
    problem(`the map control "${hidden.control}" is ${hidden.why} at 150% with a centre panel open`);
  }
  if (squashedAt150.length) {
    problem(`${squashedAt150.length} SCHEDULES cells are squeezed past their column at 150%: ${squashedAt150.slice(0, 3).join(" | ")}`);
  }
  await shot(page, "15-scaled-schedules");
  await vetPanel(page, ".overlay.center", "SCHEDULES · 150% with both side panels");
  await vetPanel(page, ".overlay.right", "decision · 150% with both side panels");
  await vetPanel(page, ".stepper", "stepper · 150%");
  // The tightest the interface is ever asked to be: 150%, every panel open at
  // once, the replay bar taking the bottom edge off the side panels, and a
  // notice standing over the map. Nothing may be clipped, covered or squeezed.
  await page.click(".tile-btn:has-text('REPLAY')");
  await page.waitForTimeout(900);
  const everything = await page.evaluate(() => {
    const box = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { left: Math.round(r.left), right: Math.round(r.right), top: Math.round(r.top), bottom: Math.round(r.bottom) };
    };
    return {
      left: box(".overlay.left"),
      right: box(".overlay.right"),
      centre: box(".overlay.center"),
      bar: box(".bottom-bar"),
      viewport: { w: window.innerWidth, h: window.innerHeight },
    };
  });
  note("everything-open-at-150", everything);
  await vetPanel(page, ".bottom-bar", "replay bar · 150% with every panel open");
  await vetPanel(page, ".overlay.left", "queue · 150% with every panel open");
  await vetPanel(page, ".overlay.center", "SCHEDULES · 150% with every panel open");
  await vetPanel(page, ".overlay.right", "decision · 150% with every panel open");
  await vetPanel(page, ".app-header", "header · 150% with every panel open");
  await shot(page, "34-everything-open-150");
  await vetPanel(page, ".map-controls", "map controls · 150% with every panel open");
  const coveredControls = await mapControlsCovered(page);
  note("map-controls-at-150-everything-open", { covered: coveredControls });
  for (const hidden of coveredControls) {
    problem(
      `the map control "${hidden.control}" is ${hidden.why} at 150% with every panel open — ` +
        "a control nothing can click",
    );
  }
  // Every panel inside the viewport, and the side panels clear of the bar.
  for (const [what, r] of Object.entries(everything)) {
    if (!r || what === "viewport" || what === "bar") continue;
    if (r.left < 0 || r.top < 0 || r.right > everything.viewport.w || r.bottom > everything.viewport.h) {
      problem(`the ${what} panel runs outside the viewport at 150% with everything open: ${JSON.stringify(r)}`);
    }
    if (everything.bar && r.bottom > everything.bar.top + 1) {
      problem(`the ${what} panel runs under the replay bar at 150% with everything open`);
    }
  }
  await page.click(".tile-btn:has-text('REPLAY')");
  await page.waitForTimeout(600);
  await page.click(".tile-btn:has-text('SCHEDULES')");
  await closeDecision();
  // A standing error toast must never cover the popover's controls (the
  // popover is modal and wins), and Escape dismisses the error first.
  armed.disrupt500 = true;
  await page.route("**/api/commands/disrupt", (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "synthetic failure: ".padEnd(260, "x") }) }),
  );
  await hub("RTM").click({ force: true });
  await page.waitForSelector(".popover", { timeout: 15000 });
  await page.click(".popover .panel-footer button.primary");
  await page.waitForTimeout(1200);
  await page.unroute("**/api/commands/disrupt");
  armed.disrupt500 = false;
  const popoverClear = await page.evaluate(() => {
    const b = document.querySelector(".popover .panel-footer button.primary");
    if (!b) return "no popover";
    const r = b.getBoundingClientRect();
    const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return b.contains(el) ? "clear" : el?.className || "?";
  });
  note("error-toast-vs-popover-150", { popoverClear, toast: !!(await page.$(".toast.error")) });
  if (popoverClear !== "clear") problem(`an error toast covers the popover's controls at 150% (${popoverClear})`);
  // Decision panel, popover and a standing error all up at once, at 150% —
  // the busiest the screen ever gets, and a popover here is tall enough to
  // scroll (this is where the many-zone popover is really read).
  await vetPanel(page, ".popover", "facility popover · 150%, error toast standing (scrolls)", { modal: true });
  await vetPanel(page, ".toast", "toast · long error at 150%");
  const toastReachable = await page.evaluate(() => {
    const b = document.querySelector(".toast button");
    if (!b) return "no toast";
    const r = b.getBoundingClientRect();
    const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return b.contains(el) ? "clear" : el?.className || "?";
  });
  const popoverScroll = await page.evaluate(() => {
    const body = document.querySelector(".popover .panel-body");
    return body ? { height: Math.round(body.clientHeight), content: body.scrollHeight, scrolls: body.scrollHeight > body.clientHeight } : null;
  });
  note("toast-dismissable-under-modal", { toastReachable, popoverScroll });
  await shot(page, "39-popover-toast-decision-150");
  if (toastReachable !== "clear") {
    problem(`the notice's dismiss is unreachable while a popover stands over it (${toastReachable})`);
  }
  await page.keyboard.press("Escape");
  await page.waitForTimeout(200);
  if (await page.$(".toast.error")) problem("Escape did not dismiss the standing error");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(200);
  if (await page.$(".popover")) problem("Escape did not close the popover");
  // Escape closes the topmost thing.
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  if (await page.$(".overlay.center")) problem("Escape did not close the centre panel");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  if (await page.$(".overlay.right")) problem("Escape did not close the decision panel");
  for (let i = 0; i < 3; i++) {
    await page.click(".scale-control button[aria-label='Smaller']");
    await page.waitForTimeout(300);
  }
  // A window resize re-fits the map — but only when the camera is still at
  // our fit. FIT first (the camera becomes ours), then resize: hubs clear.
  await page.click(".map-controls .chip:has-text('FIT')");
  await page.waitForTimeout(400);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.waitForTimeout(1200);
  const hiddenAfterResize = await page.evaluate((HUB) => {
    const panels = [...document.querySelectorAll(".overlay.left, .overlay.right")].map((p) => p.getBoundingClientRect());
    const out = [];
    for (const m of document.querySelectorAll(HUB)) {
      const r = m.querySelector(".node-box").getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      if (panels.some((p) => cx > p.left && cx < p.right && cy > p.top && cy < p.bottom)) {
        out.push(m.querySelector(".node-label").textContent);
      }
    }
    return out;
  }, HUB);
  note("resize-refit", { hiddenAfterResize });
  if (hiddenAfterResize.length) problem(`hubs under a panel after a window resize: ${hiddenAfterResize.join(",")}`);
  await page.setViewportSize({ width: WIDTH, height: HEIGHT });
  await page.waitForTimeout(1200);
  // And the other half of the contract: a camera the operator moved SURVIVES
  // a resize.
  const box2 = await (await page.$(".map-canvas")).boundingBox();
  await page.mouse.move(box2.x + box2.width / 2, box2.y + box2.height / 2);
  await page.mouse.wheel(0, -400);
  await page.waitForTimeout(900);
  const cameraBefore = await page.evaluate(() => ({
    zoom: window.__nodalMap.getZoom(),
    lng: window.__nodalMap.getCenter().lng,
  }));
  await page.setViewportSize({ width: WIDTH - 200, height: HEIGHT - 100 });
  await page.waitForTimeout(1200);
  const cameraAfter = await page.evaluate(() => ({
    zoom: window.__nodalMap.getZoom(),
    lng: window.__nodalMap.getCenter().lng,
  }));
  note("resize-preserves-user-camera", { cameraBefore, cameraAfter });
  if (Math.abs(cameraAfter.zoom - cameraBefore.zoom) > 0.05) {
    problem(`a resize discarded the operator's camera (${cameraBefore.zoom} -> ${cameraAfter.zoom})`);
  }
  await page.setViewportSize({ width: WIDTH, height: HEIGHT });
  await page.waitForTimeout(800);
  await page.click(".map-controls .chip:has-text('FIT')");
  await page.waitForTimeout(600);
}

// The harness provokes a handful of failed requests itself — the wrong-token
// probe at the connect screen, the synthetic 500 the disruption intercept
// fulfils, the 422 the what-if fork is fed, and the 409 a commit naming the
// wrong plan earns. Every other 4xx/5xx is a real broken request and fails the
// run;
// matching by path AND status keeps a genuine 401 or 500 elsewhere visible.
for (const failure of report.failedRequests) {
  if (failure.provoked) continue; // tagged at record time, inside its armed window
  problem(`failed request: ${failure.status} ${failure.url}`);
}

fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
console.log("CONSOLE:", JSON.stringify(report.console));
console.log("PAGE ERRORS:", JSON.stringify(report.pageErrors));
console.log("FAILED REQUESTS:", JSON.stringify(report.failedRequests));
console.log("PROBLEMS:", JSON.stringify(report.problems));
if (report.fatal) console.log("FATAL:", report.fatal);
await browser.close();
process.exit(report.problems.length || report.fatal || report.pageErrors.length ? 1 : 0);
