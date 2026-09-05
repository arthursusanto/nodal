# Browser smoke

## Every panel gets vetted, in whatever state it is in

`vet.mjs` is the generic pass, and it exists because bespoke checks only ever
cover the states somebody thought to open. The 880px occupancy bars that buried
a CLOSED facility's popover were never going to be caught by a step, because no
step had ever opened that popover. So every panel and popover `drive.mjs` opens
also goes through `vetPanel(page, selector, label)`, which measures — never
eyeballs — that:

* nothing **paints outside** the panel (each element's box intersected with
  every clipping ancestor, so content merely scrolled out of view does not
  count, while a bar or row that escapes does);
* nothing that **clips** overflow is cutting its own children off (this is the
  880px-bar rule: they never left the panel, they buried it);
* no **text is clipped** — a leaf, a table cell, or an overflow-hidden box
  holding more text than it can show, with nothing scrollable to reach the
  rest. **Ellipsized header copy counts as clipped**;
* no **machine text** reaches the screen: a raw ISO stamp, `NaN`, `undefined`,
  `null`, `Infinity`, `[object Object]` — in copy or in a `title`;
* no two **interactive controls overlap**, and no enabled control is
  **covered** by something else (its own visible centre must belong to it);
* every control the stylesheet pads with an invisible `::after` — its promise
  of a 44px target — really measures 44 CSS px with that pad counted;
* every badge and chip reads at **3:1 or better** against whatever is actually
  behind it, semi-transparent panels composited down to the page.

`label` names the screen AND the state, because that pair is what a failure has
to send someone back to ("facility popover · SYD (CLOSED, trapped cargo, zeroed
zones)"). Pass `{ modal: true }` for a panel a catch layer deliberately covers:
occlusion is the one rule that cannot hold there, and only it is dropped.

Two measurement traps the rules had to be written around, both of which produce
a wall of false positives if you reach for the obvious API: `scrollWidth` counts
the absolutely-positioned `::after` hit pads, so clipped text is measured with a
`Range` over the element's own contents instead; and a queue row scrolled out of
the panel body still has a laid-out box over the disruption board below it, so
overlap compares PAINTED boxes, not laid-out ones.

## The rest of the run

`drive.mjs` drives the real UI in headless Chromium: logs in, checks that
every facility marker sits exactly where `map.project()` puts its coordinate
(before and after zoom/pan), that lanes and booked routes actually render,
and walks every operator flow — single solve + commit, an A→B delivery
(itinerary rows, road legs, the customer dot inside the focus fit),
registering a delivery through `+ NEW`, a delivery ACROSS THE DATE LINE (a
Pacific hub to a customer just west of ±180, whose fit must frame the 27°
between them and not the 333° the other way round), OPTIMIZE ALL plan →
discard → commit, a disruption, 3D, every tab, what-if, replay — with a
screenshot per step in `smoke/out/`. It exits 1 on any failed check, page
error, fatal step, or unexpected failed request (only the wrong-token 401 and
the synthetic 500 the harness provokes itself are allowed). The UI's type
check and bundle passing proves nothing about what renders; this does.

Itinerary rows are checked against the decision record the API served, not
against their order: the engine leaves `lane_id` null on exactly the two road
miles, so that is what decides which row reads FIRST MILE or LAST MILE.

The closure story (§7.9) is walked too, all of it against the world JSON
rather than against hard-coded ids: cargo a closure trapped (the queue badge
and line, the decision banner above the route block with CANCEL under it, the
hub on the map), every dwell a booked journey records (a row of its own — never
a leg — a ring on the map, inside the focus fit), the staging reservation a
cross-dock holds, and the fact that a shut facility appears in no booking's
stops, exits, or route lane ends. Ordinary allocations are read exactly like
deliveries throughout — one stop-aware router plans them all, so they stage
through hubs too, and the run fails if none of them does (the checks would be
covering deliveries only). The one stop at a shut facility the run tolerates is
the trap itself: cargo a closure shut around keeps the booking it already had,
where it stands.

SCHEDULES is checked as what it is: one row per WINDOW the goods occupy, so
rows outnumber shipments. The panel must render every row the API served, name
each one (ENTRY / CROSS-DOCK / HOLD / EXIT / LAST MILE), name the customer on a
last mile, count both in its header — and log no React key warning, which is
what one-row-per-shipment keying would produce now. Cell width is measured,
not eyeballed: a table too wide for its panel scrolls, it never squeezes one
column into the next (checked at 100% and again at 150% with both side panels
open, the tightest the table is ever asked to be). Then the harness shuts a hub of its own — chosen **by id**: open,
never one the world already shut, and preferably one with cargo standing on it
— asserts the trap is reported in the notice and badged on the row at once, and
that ending the disruption clears it. Cancelling the trapped shipment (the only
manual clearing path) runs last, because it drives the world.

A batch plan is DRAFTED into the log (§7.5), so the demo world ends with one
pending and the app must open **in plan review** — the run asserts the
`COMMIT PLAN (n/m)` action, the `PLAN →` tags, the dashed planned arcs, the
reason on the row nothing could place, and the batch decision on a selected
row; then reloads the page (a review that lives in the log survives it), tries
a commit naming the WRONG batch (refused 409, nothing booked, the review still
standing), and finally COMMITS it — the product's headline move. The commit is
asserted end to end: the confirmation toast's counts, the plan gone from the
server, exactly the unplaceable shipments left planned, no PLAN tags, and the
proposal's dashed arcs replaced by solid booked ones. A drafted-but-undriven
world is FRESH — the stale-world guard still keys on planned counts and
`FAC-API-*`. Committing at load empties the queue of everything solvable, so
the flows that follow register a shipment of their own before asking for a
single SOLVE or an OPTIMIZE ALL.

Road miles are checked as a drawing question: a delivery's first and last mile
are ROAD legs, so the lane trace has to begin at the facility the goods ENTER
the network at and end at the one they leave from. Otherwise the same mile gets
two lines — the fetched road polyline and a straight shortcut under it.
`roadMileArcs` checks both structurally (arc features carry the shipment they
belong to, and no lane-grade arc may reach a mile's outer end) and in pixels
(midway along the mile, no lane layer may render — the road line's own geometry
usually does not pass through that point, so it is the ABSENCE of the straight
duplicate that is asserted there).

Facility markers, origin diamonds and endpoint dots are all `.map-node`: hub
queries go through the `HUB` selector, which excludes the other two (only hubs
carry a `.node-box` and a `.node-label`). The place search is answered by a
`page.route` intercept, so no run depends on a live maps provider.

One-time setup (Chromium download):

```
cd web && npm install && npx playwright install chromium
```

Each run needs a fresh demo database (the flows commit bookings), the API,
and the dev server:

```
python scripts/make_global_demo.py
python -m server --db var/global-demo.sqlite3 --profile scenarios/profiles/showcase.yaml \
    --packs core,coldchain,chem --token <token>
cd web && npm run dev
NODAL_API_TOKEN=<token> npm run smoke            # PowerShell: $env:NODAL_API_TOKEN="<token>"; npm run smoke
```

The `--profile` flag is load-bearing, not decoration: the demo world is solved
under `scenarios/profiles/showcase.yaml`, and a server on any other objective
re-solves the pending plan differently, so COMMIT PLAN answers 409 and the plan
flow cannot be driven at all.

Optional positional arguments: `outDir width height dpr baseUrl` — e.g.
`npm run smoke -- out-4k 2560 1400 1.5` mirrors a 4K display at 150% scaling.
Against a `--no-auth` server leave the token unset. Map diagnostics read the
dev-only `window.__nodalMap` handle, so point it at the dev server, not a
production bundle.
