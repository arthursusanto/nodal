# Nodal — architecture

Subordinate to [00-vision.md](00-vision.md). This document fixes the system shape, the domain
and event model, the allocation engine design, the stack, and the testing strategy.

## 1. System shape

Engine-first: `nodal` is a Python library with a CLI. Everything the project claims to do is
callable headless. The HTTP server and web UI (final stage) are thin clients of the same
engine API that the CLI uses.

```
            ┌──────────────────────────────────────────────┐
            │                   clients                    │
            │     CLI (from stage 0)     web UI (stage 7)  │
            └─────────────┬───────────────┬────────────────┘
                          │               │ HTTP (FastAPI, stage 7)
            ┌─────────────▼───────────────▼────────────────┐
            │                nodal engine                  │
            │                                              │
            │  allocate │ feasibility → scoring → batch    │
            │           │ explanations, reservations,      │
            │           │ rebalancing, re-optimization     │
            │  sim      │ virtual clock, workload          │
            │           │ generator, disruption injection, │
            │           │ policy interface                 │
            │  bench    │ KPIs, policy comparison reports  │
            │  network  │ lane graph, travel model, paths  │
            │  rules    │ constraint framework, rule packs │
            │  domain   │ entities, attributes, capacity   │
            │  events   │ store, fold, snapshots, state_at,│
            │           │ forks                            │
            └──────────────────────┬───────────────────────┘
                                   │
                     SQLite: append-only event log + snapshots
```

Repository layout:

```
nodal/
  domain/      entities and value objects (no I/O)
  events/      event types, store, fold, snapshots, forks
  network/     geospatial model, lane graph, travel models, shortest paths
  rules/       constraint framework, core constraints, pack registry
  allocate/    feasibility filter, scorer, batch optimizer, rebalancing,
               decision records
  sim/         virtual clock, generators, disruption injection, policies
  bench/       KPI computation, comparison harness, report rendering
  cli/         Typer CLI wiring the above
  worlds.py    YAML world ingestion (declarative world -> event batch)
packs/         industry rule packs (core, coldchain, chem), each a small package
scenarios/     YAML scenario/fixture definitions (worlds, profiles, seeds)
tests/
server/        FastAPI app (stage 7)
web/           Vite + React + TypeScript UI (stage 7)
docs/
```

Dependency direction is strictly downward: `domain` and `events` import nothing above them;
`allocate` may use `network`/`rules`/`domain`/`events`; `cli`/`server` may use everything.
Packs depend on the core; the core never imports a pack.

## 2. Stack

| Layer | Choice | Why |
| --- | --- | --- |
| Engine language | Python 3.12+ | Fastest path to a correct optimizer; the solver ecosystem decides this. |
| Optimization | Google OR-Tools | CP-SAT for the general batch problem, `SimpleMinCostFlow` for the restricted fast path. Free, world-class, one dependency. |
| Data/validation | Pydantic v2 | Typed events and entities, JSON serialization for the event log and decision records. |
| Timezones | stdlib `zoneinfo` + pinned `tzdata` package | Windows has no system tz database — without the `tzdata` dependency, `zoneinfo` raises on the primary dev platform. The pinned tzdata version is recorded in decision-record config snapshots, since tz rule changes would otherwise make historical operating-hours evaluation environment-dependent. |
| Persistence | SQLite (stdlib `sqlite3`) | Single-file, zero-ops, transactional appends; right size for the scale targets. No ORM. |
| CLI | Typer | Thin wiring only. |
| Tests | pytest + Hypothesis | Property tests are load-bearing here (fold equivalence, optimizer metamorphic tests). |
| Lint/type | ruff + mypy (strict on engine packages) | CI-enforced. |
| Server (stage 7) | FastAPI | Read models + command endpoints; nothing lives here that the CLI can't do. |
| Web (stage 7) | Vite + React + TS, MapLibre GL over OpenStreetMap | Map-first UI on real geography; 3D via MapLibre fill-extrusions (gated on a design decision, §11). |

**Rejected alternatives.** A single-language TypeScript stack was rejected because the JS
solver ecosystem (glpk.js, javascript-lp-solver) is far below OR-Tools and the optimizer is
the substance of the project. A Rust core was rejected as premature; Python meets the scale
targets in §13, and the event-sourced design leaves the door open to porting hot paths later.

**Determinism rules (engine-wide).** The engine never reads the wall clock or OS RNG. Time
comes from the event log / simulation clock; randomness comes from injected seeded generators.
Identifiers are derived from sequence numbers (`EVT-<seq>`, `DEC-<n>`), never from UUID/RNG.
No wall-clock measurement is ever written into an event — timings (solve wall time, decision
latency) go to run reports, not the log. CP-SAT runs with a fixed `random_seed`; benchmark and
test runs use `num_search_workers = 1` with a budget in **deterministic time**
(`max_deterministic_time`), so results are reproducible across runs and machines. Interactive
use may enable parallel workers and wall-clock limits; the decision record then flags the
result as budget-dependent. Model building uses canonical ordering (entities sorted by id), so
a permuted input yields an identical model. All timestamps are stored UTC; facility operating
hours are defined in the facility's IANA timezone and evaluated against UTC instants
(nonexistent local times resolve forward, ambiguous ones to the first occurrence).

**Units.** Canonical internal quantities are integers at fixed precision: `weight_g` (grams),
`volume_l` (liters), minutes, currency cents. Conversion happens at ingestion; the scaling
lives once in `domain.units`. A pallet is 1200 l, not 1.2 of anything. Latitude/longitude stay
float64 WGS84 degrees — they never enter a solver model. CP-SAT takes integer coefficients
only: objective contributions enter the model scaled ×10⁸ from their normalized values (§7.3)
— 10⁸ rather than 10⁴ so per-unit coefficients such as balance-deviation-per-inventory-unit
survive integer rounding — and decision records keep the exact pre-scaling values.

## 3. Domain model

Industry neutrality mechanism first: the core knows **tags**, **typed attributes**,
**compatibility classes**, and **commodity groups** — it does not know what "hazmat 5.1" or
"GDP-certified" mean.

- `Tag` — an opaque string in a namespace, e.g. `equip:reach-stacker`, `cert:gdp`,
  `security:bonded`. Facilities/zones *offer* tags; shipments *require* tags.
- `AttributeBag` — typed key→value map (`temp_c: range`, `weight_g: int`, ...). Packs publish
  attribute schemas; the core validates against whichever schemas are loaded.
- `CompatClass` — label carried by lots/shipments; a pack provides the pairwise predicate
  saying which classes may share a zone (segregation matrices, §8).
- `CommodityGroup` — opaque classification of inventory for distribution purposes (targets,
  demand rates, balance objective — §7.7). Scenario- or pack-defined.

Entities (Pydantic models; ids are short prefixed strings like `FAC-3`, `SHP-1042`):

- `Facility` — id, name, `lat`/`lon`, IANA timezone, operating calendar (weekly hours +
  exceptions), offered tags, equipment units (tag + count), certifications (tag + validity
  interval), risk factor (0–1, input to the risk objective), zones.
- `StorageZone` — id, facility, kind (`rack`, `bulk`, `tank`, `yard`, `cold`, ...), capacity
  vector, zone attributes (e.g. `temp_c` range it can hold), allowed compat classes.
- `Capacity` — vector over dimensions `{slots, volume_l, weight_g}` (a zone may bound any
  subset). Occupancy and reservations use the same vector type; a fit check is elementwise.
- `InventoryLot` — id, sku (opaque), commodity group, quantity + UoM, attribute bag, compat
  class, current zone, planned departure (set when an outbound allocation exists; else the
  scenario dwell model's estimate; else open — see §5), owning shipment (if in transit).
- `Reservation` — zone, capacity vector, interval `[from, until)`, holder (shipment id),
  created by an allocation decision. Reservations are how future capacity becomes real.
- `Shipment` — id, contents (lot lines), requirement set (capacity vector, required tags,
  attribute constraints such as `temp_c` range, compat class, deadline), origin (facility or
  external gate), ready time, status (`planned → allocated → in-transit → arrived → stored /
  cancelled`), assigned destination + route once allocated. Rebalancing transfers (§7.7) are
  ordinary shipments whose origin is a facility and whose deadline is soft.
- `DemandRate` — (facility, commodity group) → expected outbound/consumption per day.
  Scenario input (or generator output); v1 has **no forecasting engine** — rates are data.
- `Lane` — directed edge `(from, to, mode)` with distance km, expected travel minutes (plus a
  variance field, v2), cost model `fixed_cents + cents_per_kg + cents_per_m3`, capacity per
  departure (optional), schedule (continuous or departure calendar, v2).
- `Disruption` — kind (`facility_closed`, `zone_offline`, `equipment_down`, `lane_blocked`,
  `capacity_reduced`, `shipment_delayed`), target ref, interval, magnitude (e.g. fraction of
  capacity lost). Disruptions are events like everything else; the fold turns them into
  effective capacity/availability from stage 1 on.

`NetworkState` is the folded projection of all of the above at an instant: entity indexes plus
derived structures (occupancy timelines, active reservations, active disruptions, lane graph,
stock-vs-target positions).

## 4. Event model and persistence

The append-only event log is the **only** source of truth. `NetworkState = fold(events)`;
mutating an entity means appending an event. Master data is no exception (`FacilityRegistered`
is an event) — that is what makes `state_at(t)` exact rather than approximate.

Envelope (top-level fields, set by the event constructor):

```json
{ "seq": 18231, "id": "EVT-18231", "ts": "2026-09-01T14:03:00Z",
  "type": "ShipmentArrived", "entity_type": "shipment", "entity_id": "SHP-1042",
  "payload": { ... }, "cause": "EVT-18007", "actor": "sim|cli|api" }
```

Ordering: `seq` is the authoritative total order; appends validate that `ts` is non-decreasing
in `seq`. v1 is single-timeline (transaction time = valid time); retroactive corrections are
modeled as compensating events (`LotQuantityAdjusted`, `CapacityAdjusted`) at the time they
become known. Bitemporal history is explicitly out of scope.

Event catalog (initial):

- Master data: `FacilityRegistered`, `FacilityUpdated`, `ZoneRegistered`, `ZoneUpdated`,
  `LaneRegistered`, `LaneUpdated`, `EquipmentCountSet`, `CertificationSet`,
  `OperatingCalendarSet`, `DemandRateSet`
- Inventory: `LotReceived`, `LotQuantityAdjusted`, `LotMoved`, `LotShipped`
- Shipments: `ShipmentRegistered`, `ShipmentDeparted`, `ShipmentDelayed`, `ShipmentArrived`,
  `ShipmentCancelled`, `TransferOrdered`
- Capacity/disruption: `CapacityAdjusted`, `DisruptionStarted`, `DisruptionEnded`
- Decisions: `AllocationDecided` (embeds the decision record, §7.5), `ReservationPlaced`,
  `ReservationReleased`, `AllocationSuperseded` (re-optimization audit), `BatchSolved`,
  `PlanDrafted` / `PlanDiscarded` (a plan proposed but not booked, §7.5)

Storage: one SQLite database per world. `events(seq INTEGER PRIMARY KEY, id, ts, type,
entity_type, entity_id, payload JSON, cause, actor)` with indexes on `(ts)` and
`(entity_type, entity_id)`; `snapshots(upto_seq, ts, state BLOB)` — zstd-compressed
serialized state — written every N events (N ≈ 10 000, tunable by snapshot size).
`state_at(t)` = nearest snapshot ≤ t, then fold the tail. Property test:
`fold(all events) == fold(snapshot + tail)` at every snapshot boundary.

Fold implementation note: determinism, not purity, is the requirement. The fold mutates its
internal state objects for speed; the public API hands out state that callers must treat as
read-only (no mutating methods are exported), and appending events is the only mutation path.

**Compaction.** Terminal shipments (arrived, cancelled) and their reservations are removed
from the folded state — the log retains the full history (decision records, audit chain,
replay), while `NetworkState` stays bounded by *active* work rather than by everything that
ever happened. This is what keeps snapshots small and `state_at` fast on long-running worlds;
reservation ids stay unique across a shipment's re-allocations via its `allocation_seq`
counter rather than by counting stored reservations.

**Forks.** A what-if run opens the world read-only, materializes `state_at(t)` (default: now),
then appends hypothetical events to an in-memory overlay stream. Baseline history is never
touched. Persisting a fork = a new `.sqlite3` file containing the materialized base snapshot
at the fork point, the overlay events, and a parent pointer (world id + fork seq) in metadata.
Historical replay is just `state_at(t)` sampled along a time range.

## 5. Time, capacity, and reservations

Capacity is **time-phased**. The horizon (default 90 days) is discretized into daily buckets
(configurable). For each zone, dimension, and bucket:

```
occupancy[z, d, b] = Σ lots occupying z in b   +   Σ unconsumed reservations on z overlapping b
capacity [z, d, b] = base capacity, adjusted by CapacityAdjusted / disruptions active in b
headroom [z, d, b] = capacity − occupancy          (feasible fit ⇔ headroom ≥ demand, all d, b)
```

Multiple `capacity_reduced` disruptions stack multiplicatively with a single flooring
at the end, so the result is independent of disruption ordering. Bucket queries are
defined for the present and future; historical buckets are answered via `state_at`
(where the relevant disruptions and lots are still live), and the state API rejects
past-bucket queries rather than answering them wrongly.

A lot occupies its zone from now until its **planned departure**: the departure of its
outbound allocation when one exists, else the scenario dwell model's estimate, else the end of
horizon. The no-information default is deliberately conservative — it can only over-count
future occupancy, never admit an infeasible plan; the demand/rebalancing machinery (§7.7) and
simulated consumption are what actually drain facilities. "Forecast capacity" and "forecast
inventory" read models derive from exactly this timeline.

An allocation books a `Reservation` covering `[eta, expected_departure)`. **A reservation and
its lots are never counted together**: on `ShipmentArrived`/`LotReceived`, the corresponding
reservation is consumed (released) in the same atomic append batch, and cancellation or
supersession releases it likewise. Feasibility for a candidate is checked against every bucket
its reservation would overlap, so "full in three weeks when the shipment lands" correctly
rejects a facility that looks empty today.

Bucketing is a conservative approximation: a plan admitted under daily buckets is feasible in
the continuous model, but coarse buckets may reject plans a finer model would admit (a 4-hour
cross-dock dwell consumes a full day-bucket at the transfer facility — a known v1 bias against
multi-leg routes; the mitigation is a finer bucket size, which only relaxes the model toward
continuous time, never the reverse).

## 6. Network and routing

Facilities are nodes; `Lane`s are directed edges. Routing questions are answered by a
`TravelModel` behind a small interface:

- v1 `MatrixTravelModel`: lane distance/time as given; where a scenario omits lanes,
  great-circle distance × per-mode circuity factor generates them. Deterministic.
- later (optional): OSRM-backed model for real road geometry. Pluggable, never required.

Multi-leg routes are shortest paths over the lane graph (Dijkstra on cost or time, per the
active objective config). A route's transfer count = legs − 1 and feeds the transfer
objective; each intermediate facility must itself pass feasibility for the transiting shipment
(compatible staging zone + capacity in its buckets). **One router enforces this for every
journey** (§7.9): an ordinary allocation's inbound route and a delivery's two halves are the
same code, so every facility either kind of decision touches is a stop with an explicit dwell
window that is checked and booked. There is no second, stop-blind path through the engine —
an ordinary multi-hop allocation, a §7.7 rebalancing transfer and a delivery are all refused
a route through a facility that may not take the goods. A blocked
network is never mistaken for an absent one: synthetic legs fill in only where the lane graph
has no path at all, while lanes that exist but are all blocked reject with `LANE_BLOCKED`.

The one shape with no intermediate facility to check is an ordinary shipment fed in from an
external gate: it has no lane out of a point, so §6 synthesizes the single direct leg and
there is nothing on the way to stop at. (A *delivery* from an external gate does choose an
entry facility — that is the first-mile machinery of §7.9, and its entry is a stop like any
other.)

Arrival timing respects operating hours: a shipment arriving outside the destination's
operating window **waits** until the next open window; the effective arrival used everywhere
(deadline check, reservation start, lateness slack) is `physical arrival + wait`. Deadline
reachability: `earliest_effective_arrival(origin → f, ready_time) > deadline` ⇒ infeasible
(`DEADLINE_UNREACHABLE`). Being closed at the raw ETA is therefore not itself a rejection —
it becomes waiting time, which the time and lateness objectives price.

## 7. The allocation engine

Pipeline per decision: **enumerate → filter (hard) → score/solve (soft) → explain → commit**.

### 7.1 Candidate enumeration

Every facility in the network, unconditionally. There is no pre-filtering of any kind:
liveness during the ETA window, zone-kind fit, disruption closures — all of it is expressed as
constraints (§7.2) so that every exclusion produces a recorded verdict. "Which facilities were
considered" is always the complete facility list, and a facility closed by a disruption shows
up as rejected by `FACILITY_CLOSED`, not silently absent. This is affordable because candidate
count is bounded by facility count (~hundreds), and verdict evaluation is cheap relative to
solving. The one exclusion that is *not* a constraint is `ORIGIN_CLOSED` (§7.9): it is a fact
about the shipment, identical at every candidate, so it is settled once before routing runs —
and it still obeys the rule that matters here, enumerating the whole facility list and giving
every entry the same recorded verdict.

### 7.2 Hard-constraint filter

A constraint is a pure function:

```python
class Constraint(Protocol):
    id: str                      # "CAPACITY_VOLUME", "CERT_MISSING", "SEGREGATION", ...
    def check(self, shipment, candidate, ctx) -> Verdict  # Pass | Reject(code, data)
```

`ctx` carries `NetworkState`, the travel model, and config. Core constraint set: required-tag
coverage (capabilities, equipment, security), certification validity at effective arrival,
attribute fit (e.g. shipment temp range ⊆ zone temp range), dimensional capacity in all
overlapped buckets (§5), deadline reachability under waiting semantics (§6), zone-kind fit,
compatibility/segregation against current + reserved zone contents (§8), disruption overlays
(`FACILITY_CLOSED`, `ZONE_OFFLINE`, `EQUIPMENT_DOWN`, `LANE_BLOCKED`).

Every constraint is evaluated for every candidate — no short-circuiting — because the complete
rejection picture is a product requirement: "rejected by 3 constraints" is materially
different information from "rejected by 1". A rejection carries the constraint id and
machine-readable data (expected vs actual); human sentences are **rendered on demand** from
id + data via message templates, never stored in the log (this keeps events compact and makes
wording changes non-breaking). Candidates are zone-granular where zones differ in eligibility;
interchangeable zones within a facility are grouped (§7.4).

### 7.3 Objective components

Both decision modes optimize the **same eight component functions** with the same weights from
an `ObjectiveConfig` profile (YAML), snapshotted into every decision record. Components map
1:1 to the framing's objectives:

| Component | Framing objective | v1 definition |
| --- | --- | --- |
| `transport_cost` | transportation cost | lane-cost model over the chosen route, cents |
| `travel_time` | travel distance or time | route minutes incl. operating-window waits (or km, per config) |
| `lateness_risk` | delivery lateness risk | `max(0, (buffer_ref − slack) / buffer_ref)`, slack = deadline − effective arrival; hard-rejected already if slack < 0. v2: `P(late)` from lane time variance |
| `congestion` | facility utilization | the **marginal increase** in the destination's convex congestion penalty caused by this placement: `curve(peak after) − curve(peak before)`, peaks over the stay window against effective capacity. Marginal, not absolute — a large half-empty zone that absorbs the shipment cheaply beats a tiny zone it would saturate, and the batch objective (a sum of exactly these marginals) then shares the scorer's argmin |
| `inv_balance` | inventory balance | the **marginal change** in the destination's deviation-from-target penalty: Σ over the shipment's targeted groups of `(|after − target| − |before − target|) / target`. Negative when the placement improves balance — which is precisely how a rebalancing transfer earns its move (§7.7) |
| `capacity_preservation` | future capacity preservation | scarcity-weighted headroom consumption: per dimension, `consumed / headroom_before` ∈ [0, 1] (headroom before this decision — well-defined and bounded, since feasibility guarantees `headroom_before ≥ consumed`), weighted up when the facility is one of few offering a required tag |
| `transfers` | number of transfers | route legs − 1 |
| `op_risk` | operational risk | facility risk factor + active-disruption adjacency |

Five components (`transport_cost`, `travel_time`, `lateness_risk`, `transfers`, `op_risk`,
plus `capacity_preservation` by pricing it at pre-decision headroom) are separable per
(shipment, candidate) pair. `congestion` and `inv_balance` are deliberately **facility-level
functions** — they are evaluated on a facility's post-allocation state, which is what makes
batch optimization mean something (§7.4). In single-shipment mode each candidate changes only
its own facility's terms, so the same functions rank candidates directly.

Normalization uses **fixed reference scales from config** (e.g. `cost_ref` = P90 direct-lane
cost for the scenario), not per-candidate-set min-max — min-max would make a weight's meaning
depend on which competitors happened to be feasible, which destroys comparability across
decisions and makes explanations misleading. Component ranges are documented:
`capacity_preservation`, `lateness_risk`, and `op_risk` are bounded by construction;
`transport_cost`/`travel_time` ratios can exceed 1.0 (a route at 3× reference scores 3.0) and
are bounded in practice by deadline feasibility; `congestion` is a non-negative marginal on a
curve that extrapolates its last segment linearly (a facility at 200% overload must not price
added load like one at 100% — and the stage-4 epigraph extends that same segment, keeping the
two modes consistent); `inv_balance` is a **signed** marginal, linear in the deviation ratio
(convex, unbounded — a shipment far larger than any target genuinely deserves a dominating
penalty, and one that repairs a deficit earns a negative contribution), and commodity groups
without a declared demand rate contribute no balance signal, so balance only differentiates
facilities with targets. Nothing is clipped: the decision record carries raw
value, normalized value, weight, and weighted contribution per component per candidate, so
dominance by one component is visible rather than hidden. Ties break deterministically
(score, then facility id, then zone id).

### 7.4 Batch optimization (network path)

Assigning many shipments (plus rebalancing transfers, §7.7) interacts through shared capacity
— sequential greedy scoring is exactly what the benchmarks will show losing. The batch
problem is a generalized-assignment-family problem (indivisible shipments, multi-dimensional
time-phased capacity, segregation), NP-hard in general — solved with **CP-SAT**:

- Variables: `x[s, g] ∈ {0,1}` over feasible (shipment, **zone-group**) pairs, where a
  zone-group is a maximal set of zones in one facility that are interchangeable for `s` (same
  eligibility verdicts, shared capacity pool); a group is often a whole facility, sometimes a
  single zone. This is the granularity the constraints actually live at — reservations and
  segregation are zone-level (§3, §8) — while grouping keeps the variable count near
  facility-level in the common case. Plus `x[s, ⊥]` (unassigned) with a large configured
  penalty so overload is diagnosable rather than infeasible.
- Assignment: `Σ_g x[s,g] + x[s,⊥] = 1`.
- Capacity: for every zone-group, dimension, and overlapped bucket, Σ reserved demand ≤
  headroom.
- Segregation, class-indicator encoding: boolean `y[z, c]` = "compat class c present in zone
  z"; `x[s,g] ⇒ y[z, class(s)]` for z ∈ g, and `y[z,c₁] + y[z,c₂] ≤ 1` for each incompatible
  class pair in the active matrix. O(zones × classes²) constraints — not O(shipments²).
- Objective = Σ separable per-pair contributions (the §7.3 functions, normalized, weighted,
  scaled ×10⁸ to integers — `OBJECTIVE_SCALE`) + Σ per-facility `congestion` PWL terms + Σ
  per-(facility, commodity-group) `inv_balance` PWL terms − the constant baseline offset
  (each facility's and group's pre-batch penalty, which turns the levels into marginals —
  the invariant below) + churn penalty (§7.6; zero for a fresh batch, by definition). The
  facility-level PWL terms are the batch forms of the same §7.3 functions (convex PWL ⇒
  epigraph encoding, `p ≥ aᵢ·u + bᵢ` per segment, no extra binaries needed); utilization
  enters those rows in basis points (×10⁴), a separate scale from the objective's ×10⁸.
- Budget: deterministic-time limit in reproducible modes (§2); result records solver status,
  objective, and optimality gap.

**Consistency invariant (tested):** the batch objective subtracts each facility's baseline
penalty as a constant, so it sums the §7.3 *marginals* — on a single-shipment instance it
equals the scorer's total up to utilization quantization (u lives in 10⁻⁴ steps, so each
facility term can differ by ≤ weight × steepest-slope × 10⁻⁴; the tests bound this at
2×10⁻⁴), and CP-SAT selects the scorer's argmin for every objective config. (This is also
why the components are defined as marginals: an absolute-level definition cannot compose
into any facility-summed batch objective.) The subtraction also keeps reported optimality
gaps meaningful — baselines would otherwise dilute them. Two caveats, documented rather than
hidden: among *exactly tied* optima the flow fast path and CP-SAT may pick different
(equally optimal) assignments — each path is individually deterministic and the gate
decides the path from the input alone; and at n>1 the baseline is taken per facility over
the *batch's union* bucket window, so the marginal sum is exact at facility granularity —
per-shipment attribution (the `batch_context` delta) is computed against the solved
solution, not read off the objective.

**Min-cost-flow fast path.** A batch reduces to min-cost flow only when *all* of the
following structural conditions hold: exactly one bounded capacity dimension anywhere among
the candidates; shipments uniform in that dimension (and demanding nothing outside it);
headroom constant across every candidate's stay buckets, observed identically by all
candidates at a zone; all candidates at a zone occupying the *identical* bucket window
(differing windows are time-phased capacity in disguise — one arc per zone would misprice
them); no segregation classes in play; no facility-level PWL terms weighted. A candidate
zone with no capacity in the priced dimension constrains nothing and gets an unlimited arc,
mirroring CP-SAT's absent capacity rows. The engine checks these conditions explicitly and uses
`SimpleMinCostFlow` (exact, milliseconds at this scale) when they hold — never silently where
indivisibility, multi-dimensionality, time-phasing, or compatibility matter: that would be
wrong, not just approximate. **Optimality gaps come from CP-SAT's own proven bound**, which
every batch record reports; the transportation construction reappears outside the gate only
as the `flow-relax` *benchmark policy* — deliberately conservative capacities, explicitly
labeled a heuristic (its solver metadata proves nothing).

### 7.5 Decision records and explanation

Every allocation appends `AllocationDecided` embedding a `DecisionRecord` (one per shipment;
a batch additionally appends one `BatchSolved` event with solver metadata):

```
DecisionRecord
  shipment, decided_at, mode: single | batch
  config_snapshot: objective weights, reference scales, solver budget, engine version,
                   tzdata version
  considered: [facility ids]                        # always the complete list
  rejected:   [{facility, zone?, verdicts: [{constraint_id, data}]}]   # all failing constraints
  scored:     [{facility, zone_group, route, components: {raw, normalized, weight,
               contribution}×8, total}]
  chosen:     {facility, zone(s), route, reservation ids}
  capacity_impact: destination occupancy timeline before/after, per dimension
  solver: {status, objective, gap, deterministic_time, workers, seed}   # batch mode
  batch_context: {delta_vs_best_alternative_cents, binding_constraints: [...]}  # batch mode
```

Records contain machine-readable data only — no rendered prose, no wall-clock timings (§2);
rendering (CLI table, later UI panel) is a pure function of the record, and scored candidates
beyond a configured top-K keep component sums but drop per-component detail to bound record
size. Batch explanations are explicit about globality: the record states the shipment's own
best alternative and the cost delta if it were moved there with everything else fixed, and
lists the capacity constraints that were binding at the optimum — it does not pretend a local
ranking produced a globally-optimal assignment.

Records are events, so the audit trail of *why* travels with the history of *what*, and
replay reproduces both.

**Drafted plans.** A batch plan the optimizer proposed and nobody booked is a fact about the
world at the instant it was made, so it is an event too, not a client-side artifact: a dry-run
batch appends one `PlanDrafted {batch_id, based_on_seq, meta, records, assigned, unassigned}`
carrying exactly the per-shipment records a commit would embed, and nothing else — no
assignment, no reservation. `PlanDiscarded {batch_id, reason}` records a review that ended in
NO. The fold projects `state.pending_plan` = the newest `PlanDrafted` **iff it is the log
head**: any later event — a commit, a disruption, a registration, the discard itself — is a
change the plan never saw, which is precisely the staleness the commit's `expected_head` guard
already enforced client-side. So a reload re-enters plan review, `state_at(t)` reproduces the
plan that was pending back then, and committing re-solves the same world (determinism, §2)
under the draft's own batch id — with the re-solve compared against the draft, because a
server configured for interactive speed (parallel workers, wall-clock budget) is explicitly
budget-dependent and must refuse rather than book something nobody reviewed.

*One guard, every surface.* The review guard lives in the engine — `batch.commit_reviewed`,
which every batch commit goes through, API and CLI alike — so no surface can book past a
review another would refuse. With a plan pending it re-solves under the draft's batch id
(the audit chain reads `PlanDrafted` → `BatchSolved` on one batch) and raises `PlanMismatch`
on divergence; with none pending it mints the caller's fresh id. The comparison is the whole
**booking**, not the destination: the `Assignment` a commit would write (facility, zone,
inbound and outbound route, exit facility, stops, legs, eta, departure) and every
`Reservation` it would place. A re-solve that reaches the same zone by a different journey,
on a different schedule, or holding different staging capacity is a different plan from the
one that was read, and is refused.

*A draft is always terminated.* The fold stops treating a draft as pending the moment
anything lands on top of it, but the log would then read "proposed, and then nothing". So
every command that supersedes a draft prepends `PlanDiscarded {batch_id, reason: "superseded
by <EventType>"}` to the **same atomic append** — one choke point per surface
(`server.app._append`, the CLI's commit paths), with `engine.superseding_discard` shared
between them. The three appends that terminate a draft by themselves are exempt: its own
`BatchSolved`, an explicit `PlanDiscarded`, and the next `PlanDrafted`. Read models are
unchanged by this: the plan had already stopped being pending.

### 7.6 Re-optimization under disruption

On disruption events the engine re-solves in escalating tiers. Only ALLOCATED (booked, not
yet departed) shipments are re-plannable — in-transit and later stages are physical facts —
so the window re-optimization acts in is the booking lead between decision and departure
(§9's `booking_lead_hours` in generated scenarios).

- **Tier 1 — affected set.** A conservative superset per disruption kind: allocations at the
  closed/impaired facility or offline zone whose stay overlaps the disruption window;
  allocations routed over a blocked lane before their eta; the delayed shipment itself; and
  for capacity cuts, every reservation holder on a bucket the reduced zone can no longer
  cover. Re-solved with everything else frozen, on a deep-copied overlay whose affected
  reservations are released so the solve sees that capacity as free.
- **Tier 2 — capacity-connected closure.** Triggers when tier 1 leaves affected shipments
  unassigned, or when its objective exceeds the *still-feasible* incumbents' separable cost
  on the same overlay by more than the configured `reopt_degradation_threshold` — an
  incumbent that lost feasibility contributes zero to that baseline, so its entire
  re-placement cost (or unassigned penalty) registers as degradation. The set then
  expands to allocated holders of reservations on capacity the tier-1 solve contends for:
  its binding rows, **and** every zone that CAPACITY-rejected a still-unassigned shipment (a
  full zone never becomes a candidate, so it can never appear as a binding row). Expansion
  repeats at most `reopt_hops` times. This is what lets a closure cascade displace incumbents
  at the second-best facility instead of dumping the displaced shipments into `⊥` while
  feasible chains exist.

Both tiers carry a **churn penalty** (`churn_penalty`, normalized units) added to every
option that CHANGES an existing assignment — a different destination, or unassigned — while
keeping the incumbent costs nothing extra; one closed facility therefore doesn't reshuffle
the network for marginal gains, and the flow fast path prices churn exactly (a per-pair
constant on its arcs). The decision record states which tier ran (`reopt_tier`) and which
disruption triggered it (`reopt_trigger`); the Δ-vs-best-alternative includes the churn a
move would pay or recover. Shipments that keep their incumbent produce **no events**.
Changed ones get ONE atomic append: `AllocationSuperseded` (naming the actual old
`AllocationDecided` seq — the audit link) followed by the new decision and reservations.
A delayed shipment that keeps its destination is still re-booked when its readiness has
moved past its recorded eta — the stale reservation window would otherwise expire before
the shipment arrives. Likewise, a shipment that keeps its destination but whose **route**
changed is superseded rather than discarded, and a closure that has already trapped goods
inside a facility exempts them from re-planning and reports them for manual clearing — both
in §7.9. Re-optimization time is a first-class performance KPI (§10), and
arrivals into a facility closed at arrival time are counted as `closure_violations` — the
service failure only re-optimizing policies can avoid.

### 7.7 Inventory distribution and rebalancing

The framing's second verb — how inventory should be *distributed* — gets first-class
machinery, deliberately minimal:

- **Targets.** `target_stock(facility, group) = cover_days(config) × DemandRate(facility,
  group)`. Demand rates are scenario data (§3); v1 derives targets, never forecasts them.
- **Objective.** `inv_balance` (§7.3) penalizes deviation from target on both sides —
  starving a facility below its cover is as bad as hoarding above it. Network imbalance
  (mean |stock − target| / target, per group) is a KPI (§10).
- **Rebalancing moves.** When a (facility, group) surplus exceeds a configured threshold,
  the engine generates candidate transfer shipments from over-target donors (whole lots
  only in v1 — a donor whose smallest lot exceeds its surplus does not rebalance; lots
  already claimed by a live transfer are never re-claimed; capped count per cycle, soft
  deadlines). They enter the standard pipeline — same feasibility, same batch solve, same
  records — so a transfer happens exactly when its balance gain (a negative `inv_balance`
  marginal) beats its transport + churn cost, and the record shows that arithmetic like any
  other allocation. A transfer the solver leaves unassigned is cancelled and regenerated
  fresh next cycle rather than retried with stale lot claims.
- **Forecast inventory.** The read model projects stock per (facility, group): current +
  scheduled inbound − planned departures − demand-rate drain over the horizon. This is the
  "current and forecast inventory at each location" view the vision requires, distinct from
  forecast *capacity* (§5).

### 7.8 Objective-config profiles

`ObjectiveConfig` profiles (YAML, in `scenarios/`) carry weights, reference scales, PWL curve
points, cover days, churn cost, thresholds, solver budgets. Profiles are inputs, recorded in
every decision; changing priorities is configuration, not code.

A world and the objective it was solved under are one artifact, not two. The showcase demo
is the sharp case: `scripts/make_global_demo.py` builds its world under
`scenarios/profiles/showcase.yaml` and leaves an uncommitted plan on screen, so the server
must be launched with `--profile scenarios/profiles/showcase.yaml` or the commit's re-solve
diverges from the draft and the plan can never be booked (§7.5). The builder loads that file
rather than restating its values, so there is exactly one place for the two to agree.

### 7.9 Journey routing, stops, and delivery (A→B)

Some shipments do not end at a facility. A shipment may carry an optional `destination` —
a customer point **outside** the network (label, lat, lon) — plus `hold_days ≥ 0`, the
minimum time the customer requires the goods to be stored in the network before final
delivery. Without a destination the *decision* is the ordinary allocation of §7.1–§7.5 and
`hold_days` is ignored — but the *journey* is routed by the machinery below either way, which
is what makes "stops" a property of every plan rather than of one shipment kind. This is a
strict extension — old logs, whose events carry neither field, fold and solve identically.

**The decision.** For a delivery shipment the engine chooses one thing with three parts:

- a **holding facility + zone**, through the *same* enumeration, hard constraints, scoring,
  and batch model as any allocation — temperature, hazmat/segregation, certifications,
  equipment, closures, and time-phased capacity over the hold window all bind unchanged;
- an **inbound route**: a first-mile road leg from an external origin to an *entry* facility,
  then the cheapest lane path entry → holding facility (entry may equal holding). A shipment
  that is already inside the network enters at its own facility, routed by the ordinary
  lane router;
- an **outbound route**: the cheapest lane path holding → *exit* facility, then a last-mile
  road leg exit → destination (exit may equal holding).

**Road legs.** First and last mile are priced deterministically and offline (`RoadLegConfig`,
a profile field): `km = haversine × circuity`, `minutes = km / speed + handling`, and
`cost = fixed + per_km × km + per_kg × kg` — the same shape a lane carries, plus the
per-distance term a lane's declared cost already absorbs. No external routing service ever
enters the engine.

**Lane paths.** Both halves come from one hop-bounded relaxation each (bounded by
`travel.max_transfers`, like every other route): forward from every candidate entry seeded
with its first-mile cost, backward from every candidate exit seeded with its last-mile cost.
One pass therefore answers *every* candidate holding facility, and the entry/exit choice is
the argmin rather than a heuristic. Ties break lexicographically on
`(weight, cost, hops, minutes, facility id, lane ids)`, so two equal-weight paths always
resolve the same way. Fewer legs outranks faster deliberately: an extra hop is never free —
`transfers` is an objective component that prices it and every hop books staging space for its
dwell — so on an exact cost tie the router must not hand the objective a multi-hop path it then
charges for. **An ordinary allocation's inbound half is the same relaxation**, seeded
at its own facility rather than at road legs, and gets the full `max_transfers + 1` lane
budget a delivery half spends one of on its road leg. What differs is only the scalar being
minimized: an ordinary route minimizes the configured `travel.metric` (§6's promise), while
delivery lane paths minimize **cost** even under `travel.metric = "time"` — the entry and exit
choices need one scalar, and the objective prices the resulting time separately.

Each relaxation is then **checked against the schedule it implies and re-run without whatever
failed**. A journey is a sequence of legs and dwells, each with its own instant: the §7.2
blocked-at-departure rule binds every lane at the moment *that lane* rolls, every facility
must be able to take the goods for the dwell the journey books there (below), and a dwell
outside the facility's operating window waits for the next one. Whatever fails
joins an exclusion set and the relaxation runs again, so the router takes the next-cheapest
path **around** a closed hop, a shut exit, or a block that only opens days into the trip —
rather than planning through it and discovering the problem at execution. Both exclusion sets
key the memo, so candidates seeing the same network still share one pass and an undisrupted
network costs exactly one. *Approximation:* an exclusion is global for the retry, not
window-specific — a lane or facility that failed where this path met it is not re-tried at a
different time on a costlier path. That can pass over a path a finer search would admit; it
can never admit one that moves or dwells when it may not.

**Timing.** The hold starts at effective arrival (physical arrival + any operating-window
wait), ends at `hold start + hold_days`, and the outbound legs roll the moment it ends;
delivery eta = hold end + outbound minutes. For a delivery shipment the deadline means
*delivered at the destination by* — so `DELIVERY_DEADLINE_UNREACHABLE` rejects a facility
whose itinerary misses it, which is a property of routing **through** that facility, not of
arriving at it. `hold_days = 0` is a cross-dock: the hold window is degenerate,
and both the capacity check and the reservation it books occupy the single bucket it falls
in — the same convention `buckets_between` applies to any degenerate interval (§5).

**Stops.** Every facility a journey touches is a **stop** with an explicit window:
`{facility, role ∈ entry|transit|hold|exit, arrive, depart, zone}`, in travel order, derived
exactly from the legs — nothing estimated. **This is not a delivery feature.** An ordinary
allocation's intermediate hops are stops on the same terms, with the same checks and the same
bookings; its last stop is simply the stay itself, and its journey has no outbound half. An
ordinary shipment therefore records `stops` and per-lane `legs` on its assignment like any
delivery, which is what lets §7.6 notice a closure landing on a hub it merely passes through.
The hold's window is the hold. A pass-through stop
dwells for the **handling time the journey already prices**: a lane's declared `minutes`
include a per-mode handling allowance (`travel.handling_minutes`, overridable per mode) spent
at the facility the leg departs from, and the last mile's `road.handling_minutes` likewise.
The stop window is *carved out of* that allowance, never added to it — journey minutes,
costs and the delivery eta are exactly what they were before stops existed; only the
attribution changed. Where the exit facility *is* the holding facility, the loading window
extends the hold's own stop instead of becoming a second stop, so the goods are never booked
twice into the zone they are already sitting in. The shipment's own **origin is not a stop**
when it is already inside the network: the goods are there, they do not dwell for handling,
and the departure out of it is governed by `ORIGIN_CLOSED` below.

**A dwell outside the operating window waits.** A stop landing when its facility is shut for
the night, the weekend, or a holiday is treated exactly as an arrival at the hold is (§6): the
dwell shifts to `facility_next_open`, and every leg and arrival after it moves with it. It is
real elapsed time — the delivery eta and the `travel_time` objective both carry it — and it is
the only thing that makes a journey take longer than the sum of its legs. Rejecting instead
would be wrong (a closed calendar is not a closure), and *ignoring* it was worse: the map drew
the hub as CLOSED while the plan cross-docked through it. A facility whose calendar never opens
within the horizon cannot dwell the goods at all and is routed around, `STOP_NO_OPERATING_WINDOW`.

**Closures.** A facility closure (and any state that renders a facility shut) means **no
automatic routing into, out of, or through it while it lasts**. The router rejects any
candidate whose stop window *overlaps* a closure at that facility, in every role, and takes
the next-cheapest path instead. A degenerate window is an instant, not an empty interval, so
a cross-dock stop is matched like any other. Rejections are per facility and per role —
`ENTRY_CLOSED`, `TRANSIT_CLOSED`, `EXIT_CLOSED`, distinct from the stay's own
`FACILITY_CLOSED` — plus `STOP_EQUIPMENT_DOWN` and `STOP_NO_STAGING`. The stay check itself
now runs to the end of the *hold stop*, so a closure that opens exactly when the reservation
ends still catches the truck being loaded.

*Out of* includes the **departure from the shipment's own origin**, which is not a stop and
so was invisible to the machinery above: goods whose in-network origin facility is closed at
the instant they would roll (`Disruption.blocks_departure_from`, the `overlaps` predicate
applied to that one instant, looked up once as `NetworkState.departure_closure`) are
**infeasible at every candidate**, rejected `ORIGIN_CLOSED` naming the facility and the
closure's end. It is a property of the shipment rather than of any candidate, so it is
decided before routing is attempted and every facility carries the same verdict — which is
also what makes the reason the *origin* one rather than whatever a downstream stop happened
to refuse first. The choice is **reject, not defer**: the plan stays PLANNED with the reopen
date in the sentence, and a human decides when the goods move — an optimizer that silently
slid the departure past a closure would be automatic routing out of a shut facility with
extra steps. One predicate, every path: single allocate, batch dry-run and commit,
re-optimization re-solves, what-if forks, and §7.7 rebalancing, which additionally declines to
*propose* a transfer out of a closed donor so a doomed move cannot burn a cycle's transfer cap.
An *allocated* shipment at a closed origin is trapped rather than re-solved (below), so this
rejection is what stops a new booking being written, never what strips an existing one.

**Transit occupancy.** During each pass-through dwell the shipment **books staging space** at
that stop, using the same `ReservationPlaced` event the hold uses — so audit, replay, snapshots
and what-if get transit occupancy for free, and cancel/supersede release it with everything
else. Reservation ids derive from the hold's (`RES-<shipment>-<seq>-S<n>`), so they stay unique
across re-allocations. The zone is chosen by the existing requirement matching evaluated over
the dwell window: temperature, permitted classes, segregation, zone-offline and capacity all
bind — **cold cargo cannot cross-dock through an ambient-only facility**, and that is the
intended answer, explained by name. `zone_kinds` deliberately does *not* bind a staging dwell:
it states where the goods must be **stored**, and a handling dwell is not storage. Among the
zones that pass, one whose `kind` is `cross-dock` wins, then the smallest id — a zone kind is
the only thing in the model that names a bookable staging location (the `cross-dock` *facility*
tag is a capability consumed by `REQUIRED_TAGS`, not a place). Daily buckets mean an hours-long
dwell books the day(s) it falls in: conservative, consistent with §5, and a bias against
multi-leg routes that a finer bucket size would relax. A stop whose facility cannot
fit the dwell makes that path infeasible, and the router takes the next-cheapest; when no path
remains, the candidate is rejected with the reason above.

A candidate's stops are evaluated **as one sequence, against each other**. A journey that
crosses the same dock twice — in through a hub and back out through it — occupies it twice, so
the second dwell finds the zone already holding the first. Answering each stop independently
against unmodified state is how a single allocation once booked ten slots into a five-slot
zone while the batch model, which sums every dwell a candidate books onto the one shared
capacity row (§7.4), refused the same candidate outright. The per-stop and per-batch answers
must be the same answer, so the planner carries the candidate's own pending load per
(zone, bucket) while it walks the stops.

**Objective and batch equivalence.** Both route halves are constants of the
(shipment, holding facility) pair, so the §7.3 components absorb them without changing
shape: `transport_cost` and `travel_time` sum inbound + outbound, `transfers` counts every
leg, `lateness_risk` measures slack to the customer, and the storage terms (congestion,
capacity preservation) price the hold exactly as they price any stay. The batch model
(§7.4) therefore keeps its structure — delivery shipments enter as ordinary
per-(shipment, zone) coefficients under shared capacity rows — and batch ≡ scorer still
holds with deliveries in the mix.

Staging bookings preserve that. Each candidate hold implies one deterministic path, so its
transit bookings are constants of (shipment, candidate) too, and they enter the batch model as
**additional rows on capacity it already shares**: a candidate simply consumes several
(zone, bucket) rows instead of one. The batch therefore resolves dock contention by moving
**holds**, not by re-pathing a hold it has already chosen — a documented approximation, and the
reason the argmin stays comparable to the scorer's. Two deliberate exclusions keep the
equivalence exact: staging load is *not* priced into the facility-level `congestion` term (the
single-shipment scorer has no such term for a facility it merely passes through), and the
min-cost-flow fast path (§7.4) declines any batch containing staging bookings, because a
transportation arc rations one zone per assignment and a delivery consumes several.

**Explanation.** The decision record explains a delivery like any allocation (every facility
considered, every rejection named) and adds the chosen **itinerary** to `chosen`: ordered
legs `{kind, from, to, lane_id, km, minutes, cost, depart, arrive}`, the `hold`
`{facility, zone, from, until}`, the destination, the delivery eta, and the total transport
cost, plus the ordered **stops** with their windows and zones. `chosen.staging` lists the
reservations the pass-through stops book. `chosen.route` stays the *inbound* half — the
transport that books the hold and that the assignment records. `chosen.stops` and `chosen.legs`
carry the journey's windows and per-lane schedule for **every** shipment, and one function
(`chosen_of`) builds them, so single allocate, batch, re-optimization and what-if cannot
disagree about what a decision books. The assignment folds them, together with the delivery-only
outbound half — `outbound_route` (lane ids after the hold) and `exit_facility_id` — because the
affected set of §7.6 is a function of folded
state: `route` alone would leave the second half of the journey invisible to it, and lane ids
without departure times would leave every leg matched at one probe instant. All of these are
optional and empty on pre-extension `AllocationDecided` events, which therefore fold
unchanged; the `itinerary`, the outbound half and the customer are still delivery-only.

**Re-optimization** (§7.6) needs no special case: a disrupted delivery is re-solved through
the same batch machinery on the same affected-set tiers, and its whole itinerary is re-routed
with it. The affected set matches every disruption against **the leg or stop it actually
overlaps** — a closure or equipment outage against each stop's own window, a lane block against
that leg's own departure — using the same predicate the planner rejects candidates with, so
what planning refuses to book is exactly what re-optimization notices. Assignments written
before stops existed carry neither, and fall back to what they do have: the hold window, and
the hold's end as the one instant the exit was ever planned against.

**Trapped cargo.** Goods that are *physically present* when a closure lands are **stuck**:
no plan can move them while the facility is shut, so re-optimization must not re-route them.
They keep their booking and come back on `ReoptResult.trapped` as
`{shipment, facility, disruption}`, for an operator to clear with the existing commands
(cancel, transfer). Two populations qualify, both keyed on an **in-network origin** at the
closed facility — a shipment fed in from an external gate has nothing inside the network yet
and is never trapped:

- **ALLOCATED** (the only re-plannable status, §7.6 — they have not departed): the dwell they
  are actually in is the one at their origin facility, which began before `now` by definition,
  so what decides is whether the door is shut at the moment they are due to **leave** —
  `blocks_departure_from(origin, max(ready_at, now))`, the same instant `ORIGIN_CLOSED` tests.
  They keep the booking they have.
- **PLANNED**: nothing is booked for them and, by the `ORIGIN_CLOSED` rule above, nothing can
  be. Flagging them is what tells an operator why a solve leaves them sitting there rather
  than looking like a solver failure.

Both populations are therefore **the one origin-departure predicate at the one departure
instant**, so `trapped` is exactly the cargo the solve cannot move and the planner's rejection
is the same test. Anything wider answers a different question — "was this facility shut at some
point while the goods happened to be booked" — and strands cargo no closure ever touched: a
window running from `now` to the plan's recorded eta made every short closure at the origin
permanent once a readiness slip pushed `ready_at` past that eta, and because trapped cargo is
never re-solved, the badge then *froze* the shipment. A later closure of the facility it was
booked **into** could no longer move it, which is the opposite of what the exemption is for.

**Trapped is a property of the cargo, not of the trigger.** The exemption therefore holds in
*every* re-solve, not only one triggered by the closure itself: `reoptimize` excludes anything
`all_trapped_cargo(state, now)` names, so a lane block or a delay that happens to match a
stranded shipment cannot become the back door through which the system changes it. **Releasing
a booking is an automatic change too** — for cargo standing in the shut facility the booking
*is* its physical occupancy, and for cargo booked onward the reservation is held until a human
cancels it or the facility reopens — so a trapped shipment is neither re-routed nor released,
and each `trapped` entry names the closure holding the cargo rather than the disruption that
triggered the re-solve.

The exclusion binds **every tier**, which the tier-1 affected set alone does not achieve. The
tier-2 expansion (§7.6) does not filter the tier-1 set; it re-derives its members from the
reservation holders on the contended capacity rows, and a stranded shipment is exactly the
kind of thing holding a full zone. Escalating onto one finds it infeasible everywhere (its
origin is shut), leaves it unassigned, and releases the reservation that is its occupancy — the
back door reopened one tier down. So the stranded set is excluded from the exclusion set of
every expansion, the changed/superseded computation refuses to supersede a stranded shipment
however the solved set was assembled, and any stranded shipment an expansion *would* have
pulled in is reported in `trapped`: the chain the solve could not follow is named rather than
silently walked.

Every stop still ahead of a shipment is a *future* booking and IS re-planned around the
closure, which is what preserves the existing §7.6 behaviour. Because the read models call
the same union directly, the map's `trapped` flag and the queue's badge follow both
populations, and what-if reports its `reopts[].trapped` **whatever the tier** — a closure whose
entire reach is cargo it strands re-optimizes nothing, and a tier-0 result dropped from the
report would leave the preview silent about the one thing the hypothesis cannot re-route.

**Plan staleness.** A re-solve that keeps the hold but changes the *journey* must supersede,
or the new plan is computed and thrown away while the stale route stays folded and drawn. A
shipment is therefore superseded when its re-solved plan differs from the incumbent in the
holding facility or zone, the inbound or outbound lane list, the exit facility, or the ordered
stops — through the same §7.6 supersede/audit chain, and for ordinary shipments' inbound routes
too. Windows compare at the granularity the booking itself has, the UTC day buckets a stop
occupies (§5): re-optimizing later than the original decision legitimately shifts every clock
reading, and treating that as a changed plan would re-commit the whole affected set on every
disruption and defeat the churn penalty. A shift big enough to move the reservation onto
different days is a changed plan. An identical replan appends nothing.

*Not yet modeled:* the simulator (§9) executes a shipment as one departure and one arrival,
so it runs the inbound half and the hold, and the outbound legs exist only as the recorded
itinerary schedule. Multi-leg physical execution — outbound departure and customer delivery
as their own events — is deliberately left out rather than half-implemented.

`Lane` also carries an optional `path: [(lon, lat), ...]`, display geometry for the map with
no engine semantics: distance, time, and cost stay the declared values.

## 8. Industry rule packs

A pack is a small Python package registering, via entry points:

- attribute schemas (e.g. `chem.un_class`, `pharma.temp_band`) — validators exposed via
  `validate_attributes` and enforced at world ingestion (`load_world(..., packs=...)` /
  `nodal world load --packs core,coldchain`): a bad bag fails the load atomically before
  any event lands; loading without `--packs` skips validation, documented
- constraints (implementations of the §7.2 protocol)
- a compatibility matrix over its compat classes (e.g. IMDG-style segregation)
- objective adjustments (optional extra components, e.g. excursion risk) — implemented as
  `PackComponent`s: separable by contract (a pure per-candidate level, no cross-shipment
  coupling — enforced by what `PackStay` withholds: no state handle), namespaced
  `<pack>.<component>` (validated at load time, so an entry-point pack cannot collide with
  a core component or evade the batch model's separable classification), weighted by the
  profile's `pack_weights` or the pack default. Because they are separable, the batch
  model prices them exactly by adding them to each pair's separable cost (§7.4), and they
  appear in decision records like any core component.

Vertical *workload* profiles (§9) live in scenario configuration rather than in the packs
themselves — `coldchain_cert_prob`, `hazmat_cert_prob`, `compat_class_probs` and class
names are plain strings in scenario YAML, so synthetic workloads exercise a pack without
the generator importing it. (The original sketch put generator profiles inside packs;
config-side profiles keep the dependency arrow strictly core-ward.)

The core defines the interfaces and runs whatever is registered; it never imports a pack.
Planned packs: `packs/core` (generic 3PL vocabulary — the proof that the default path is
industry-free; built in stage 2 alongside the registry), `packs/coldchain` (temp bands, cert
requirement, lane temperature-excursion budget), `packs/chem` (segregation matrix, zone
certification classes). Pack conformance is tested by a shared suite every pack must pass
(constraints pure/deterministic, schemas valid, every rejection renders from id + data).

## 9. Simulation and workload generation

The simulator drives the same engine through the same event log — it is an event producer with
a virtual clock, not a parallel implementation:

- `VirtualClock` advancing over a scheduled event queue (arrivals, departures, consumption,
  disruptions, decision points). No wall-clock coupling.
- `WorkloadGenerator` (seeded): network topology (facility count, geography, capability/tag
  distributions, zone mixes), demand (Poisson arrival of shipments with configurable
  intensity, weekly/seasonal modulation, attribute distributions and demand rates per
  vertical profile), disruption schedule (closure/delay/capacity-loss processes).
- Scenario = YAML: topology params or explicit world, demand profile, disruption schedule,
  objective config, seed. `scenarios/` holds the standard suite; fixtures for tests are tiny
  hand-written worlds.
- Policy interface: `Allocator.decide(state, shipment | batch) -> Decision`. Implementations:
  `nearest-feasible`, `first-available` (first feasible by facility id), `greedy`
  (cheapest-now, myopic single-shipment cost), `nodal-single` (§7.3), `nodal-batch` (§7.4),
  and `flow-relax` (min-cost-flow relaxation where §7.4's conditions permit — both a policy
  and a bound).

Same scenario + same seed + same policy ⇒ byte-identical event log. That property is a test
(it is why events carry no timings and no random ids).

## 10. Benchmark harness

`nodal bench` runs a scenario suite × policy matrix and emits per-run JSON plus a rendered
comparison report. KPIs are split into two classes:

**Outcome KPIs** (deterministic; byte-stable for a given scenario/seed/policy):

- total transport cost; total distance
- on-time rate; mean and p95 lateness
- utilization mean / variance / p95 (per dimension), target-band residency
- network inventory imbalance (mean |stock − target| / target, overall and per commodity
  group)
- `infeasible_preferred`: candidates a policy's own preference ranking placed above its
  eventual choice that failed feasibility. A per-policy diagnostic of ranking naivety —
  structurally 0 for the engine, whose ranking is defined over feasible candidates only —
  and therefore **not** a head-to-head KPI (every policy shares the same feasibility
  filter; nobody actually books an infeasible assignment)
- unallocated shipments; inter-facility transfers; rebalancing moves and their cost
- proven optimality gap per batch solve (CP-SAT's own bound; heuristic policies report
  none rather than pretending)

**Performance KPIs** (wall-clock; reported with hardware note, excluded from byte-identity):

- decision latency; batch solve time; re-optimization time after disruption

Reports state seeds and configs, and include the losses: any scenario where a naive policy
matches or beats `nodal-batch` appears in the table like everything else. CI runs a
small-scale suite as regression guard — outcome KPIs asserted exactly against goldens,
performance KPIs against generous tolerances.

## 11. API and UI (stage 7)

FastAPI exposes read models (map state, occupancy timelines, forecast inventory, schedules,
decision records, replay states, bench reports) and commands (ingest events, allocate,
simulate, what-if) — a thin translation of the engine API, no business logic. `schedules`
returns one row per window the goods actually occupy (§7.9), not one per shipment: every stop
carries its `role`, `arrive`/`depart` and staging zone, a delivery ends with a `last_mile` row
at the customer, and each row has its own `id`. The stay's row keeps the shape it always had —
`eta`/`departure` are that window's own arrival and departure — so the added fields are
additive. The web UI is
a **fullscreen map with floating overlay panels** (queue, decision explorer, solve-pipeline
stepper) — the map is the base layer, never a tile in a page. Panels appear only when
they carry state (the decision explorer opens with a selection; the stepper belongs to
the allocate view); there is no decorative chrome such as an event ticker:

- Global map: MapLibre GL over OpenStreetMap raster tiles (attributed), restyled dark to
  the design palette — real geography, so multi-continent networks read as what they are.
  Facilities as nodes (utilization as fill, decision state as border), lanes as faint
  mode-styled hairlines (road solid / sea dashed / air dotted), disruption overlays, and
  the decision grammar drawn live from the record: the chosen route traced and lit, the
  top alternatives as dashed beams, rejections as node states and panel rows — never a
  fan of arcs. Booked arcs trace the committed lane route hop by hop (great-circle
  segments, antimeridian-safe); a booking that stores where it sits draws nothing.
  MapLibre's worker is bundled by Vite explicitly (`?worker&url` + `setWorkerUrl`):
  its own `import.meta.url` resolution 404s under both the dev server and Rollup, and
  without the worker no GeoJSON source ever renders.
- Batch plan review: OPTIMIZE ALL is a dry run whose per-shipment records come back
  with the assignments and the log head they were solved against; the map draws the
  plan as dashed routes, queue rows show their planned destination, selecting a
  shipment shows its batch decision, and the operator then COMMITs or DISCARDs the
  plan explicitly. The commit is `POST /api/commands/optimize {commit: true,
  expected_head?, batch_id?}`. With a plan pending, `expected_head` must be exactly the
  seq that plan READ (`based_on_seq`) — the draft's own event is not a move, but the
  current head is not an acceptable answer either, since after a second draft that number
  names the world the FIRST plan read; with none pending it is the current head. Anything
  else is a 409. `batch_id` is optional and exact: supplied, it must be the pending
  plan's, so a reviewer holding one of two successive drafts cannot commit the other
  (clients should send it — `expected_head` alone cannot separate them). A re-solve that
  diverges from the reviewed booking is a 409 too (§7.5). The UI discards a plan the
  moment its own poll sees the head move.
  The plan is **not** a client artifact: the dry run appends `PlanDrafted` (§7.5), so
  `GET /api/map` carries a `pending_plan` summary (batch id, the head it read, the head
  it sits at, assigned/unassigned counts, and one row per shipment — proposed facility,
  zone, eta, or the constraint ids that left it unassigned) and `GET /api/plan` returns
  the whole plan in the shape the dry run returns it. A reload therefore re-enters plan
  review instead of losing it, and `?at=` replays the plan that was pending back then.
  DISCARD is `POST /api/commands/plan/discard {batch_id, reason?}`, accepted only for
  the plan actually pending. The CLI has the same two halves: `nodal optimize` without
  `--commit` drafts, `nodal plan discard` throws the draft away. The single dry run that
  does not draft is `optimize --rebalance` without `--commit`, whose previewed transfers
  were never registered — a plan over shipments the log has never seen could not be
  executed. `nodal optimize --commit` commits through the same engine guard the API does
  (§7.5): with a plan pending it books THAT plan, under its id, or refuses.
- Replay and same-instant events: `?at=` resolves to the state after the **last** event
  whose ts ≤ `at` (`max_seq_at` is a MAX over seq, and ts is monotone in seq). Commands
  stamp the world clock (`state.last_ts`), which only the simulator advances, so a draft
  and the command that supersedes it normally share a timestamp — and `at` on that instant
  returns the outcome (discarded, committed), never the moment in between. A drafted plan
  is therefore visible to `/api/plan?at=` only while it outlives the instant it was made:
  today that means a draft still standing at the head. Whether an operator command should
  advance the world clock at all is an open design question, not a defect of the read
  model, and the clock semantics are deliberately left alone here.
- Focus: selecting a shipment leaves only its nodes on the map — origin, destination
  (booked, planned, or chosen), route waypoints, the top alternatives — fits them,
  and restores the previous view on close. A planned shipment with nothing decided
  keeps the whole network in view until SOLVE gives it a destination.
- Readability is a requirement, not a style: queue rows carry FROM / TO / CARGO (in the
  shipment's own units and weight; slots are the capacity currency, shown last) / DUE;
  every timestamp renders as "Sep 5 · 04:00" UTC with a relative "in 3d 4h", durations
  as "2h 16m"; all sizes are rem and the header's A−/A+ sets the root font size (the
  panels cede width on narrow viewports so the map strip can still hold the network —
  MapLibre will not zoom out past the world covering the viewport height).
- Disruptions are operable, not just visible: the board row opens the facility it
  lands on; there the operator can END NOW (`DisruptionEnded`) or move the end, which
  is an end-now plus a fresh window re-optimized like any new disruption (the log is
  append-only; an end time is superseded, never edited).
- Shipments are operable too, through the same events the engine already speaks:
  register (`ShipmentRegistered`, the + NEW form), move readiness
  (`ShipmentReadyChanged`; a DELAY of a booked shipment adds a `SHIPMENT_DELAYED`
  disruption and re-optimizes — the exact §7.6 path what-if exercises), and cancel
  (`ShipmentCancelled`, releasing reservations). REBALANCE orders §7.7 transfers and
  books only them (`POST /api/commands/rebalance`); the optimize command's
  `rebalance` flag keeps the CLI's fold-into-the-batch semantics. Deliberately NOT in
  the GUI: network topology (facilities, zones, lanes — world files own
  configuration) and depart/arrive transitions (the simulator's clock owns time).
- The view controls live ON the map — a floating tile at the top of the stage, not
  header chrome: the map is the base layer, the views are overlays it toggles.
- Facility view: 2D zone layout with occupancy timelines and reservations.
- Decision explorer: the §7.5 record rendered — considered / rejected-with-constraint /
  score breakdown / capacity impact. This view is the UI's reason to exist.
- What-if panel: compose hypothetical events, run a fork, diff KPIs and changed decisions
  against baseline.
- Replay scrubber: `state_at(t)` over a time range.

**3D.** The original gating criterion (3D only once zone geometry is a constraint input)
was overridden by a later design decision: the map carries MapLibre fill-extrusion
zone blocks, pitch/rotate camera, and a 2D/3D toggle. Extrusions are building-scale, so
toggling 3D from a world-level zoom first dives (a single `jumpTo`, no easing) to the
busiest hub — pitching an intercontinental view would show nothing. The extrusions stay
data-true — shell height is effective slot capacity (sqrt-scaled meters, monotone), core
height is today's occupancy, core color is utilization — while the footprints (small
squares ringed around the facility point) are presentational, because zone geometry is
still not modeled. When geometry becomes a constraint input, the footprints become real.

**Auth.** Every `/api` route sits behind a bearer token (`--token` / `$NODAL_API_TOKEN`,
generated and printed on startup otherwise; `--no-auth` opts out for trusted localhost).
The web UI gates on a connect screen and holds the token in sessionStorage. Single-operator
token auth by design — no user database until there are users.

UI conventions: all states reachable with OS reduced-motion enabled (no feature may depend on
a CSS animation firing — the map uses `jumpTo`, never eased camera transitions); dismissal
of popovers uses an explicit catch layer, never a window-level click-away listener.

## 12. Testing strategy

- **Event core:** Hypothesis property tests — fold(snapshot+tail) ≡ fold(all); event
  round-trip serialization; `state_at` monotonicity; ts-monotonic append validation.
- **Constraints:** table-driven pass/reject cases per constraint; every reject carries id +
  data and renders via template; purity (same inputs ⇒ same verdict).
- **Scorer:** golden decision records on fixture worlds (byte-stable JSON — possible because
  records carry no timings); documented weight-flip example (changing one weight changes the
  winner as designed).
- **Optimizer metamorphic suite**, each property scoped to where it genuinely holds:
  - *Always:* tightening a hard constraint never improves the optimum (feasible set shrinks,
    objective unchanged, `⊥` keeps the model feasible). Input permutation yields the
    identical assignment (canonical model ordering + fixed seed + one worker, §2).
  - *Under monotone configs* (`inv_balance` weight = 0 — the one deliberately non-monotone
    component, since both starving and hoarding deviate from target): adding capacity or
    lanes never worsens the optimum; removing a shipment never worsens it. The suite runs
    these under such configs and documents why the scoping is principled, not evasive.
  - *All configs:* single-shipment batch solve ≡ scorer argmin (§7.4's consistency
    invariant); fast-path structural gate engages on cases just inside and just outside each
    condition.
- **Simulator:** same seed ⇒ identical event log; KPI computations against hand-computed
  fixtures.
- **Benchmarks-as-tests:** small-scale suite in CI; outcome KPIs exact, performance KPIs
  tolerant (§10).

## 13. Scale targets

Design envelope (single desktop, no distributed systems): ~200 facilities, ~2 000 zones,
~100 000 lots, ~10 000 active shipments, batch solves of ~2 000 shipments × ≤50 feasible
facilities × 1–3 zone-groups each — roughly 2–5 × 10⁵ assignment binaries plus ~10⁴
segregation indicators. That is large-but-tractable CP-SAT territory under an incumbent+gap
regime: the falsifiable performance gate lives in the roadmap (stage 4), not in adjectives.
Horizon 90 daily buckets; event logs to ~10⁶ events. Snapshots at envelope scale are tens of
MB of JSON → single-digit MB zstd-compressed; `state_at` from the nearest snapshot targets
< 2 s at full envelope (sub-second at fixture scale), with delta snapshots and lazy per-entity
hydration as the named fallbacks if measurement disagrees. Exceeding a solver budget degrades
gracefully: best incumbent + reported gap; a cold start that exhausts its budget with no
incumbent at all is re-solved once seeded with the validated flow-relax solution as a hint
(a real feasible assignment — never the trivial all-unassigned one, which measurably anchors
LNS on a terrible start), and only if that too produces nothing does the engine return the
explicit all-unassigned `NO_INCUMBENT` result. Never a silent fallback. Very large batches
may hint up front (`SolverConfig.warm_start`), and interactive use may add a wall-clock
budget and parallel workers — both flagged `reproducible=False` on the record (§2).

## 14. Decision log

- **Python + OR-Tools over TS-only / Rust** — solver quality is the project; see §2.
- **Event sourcing over mutable rows + audit table** — audit, replay, and what-if are core
  features, not add-ons; deriving them from a mutable schema would be strictly more code.
- **SQLite over Postgres** — single-user desktop scale; zero ops; trivially copyable worlds.
- **One objective vocabulary, two evaluation contexts** — the same eight component functions
  serve single-shipment scoring and the batch model (facility-level terms become per-facility
  convex PWL sums). This is what makes the scorer-vs-CP-SAT cross-check exact instead of
  approximate, at the price of defining components with batch-separability in mind from the
  start.
- **Weighted-sum objectives over lexicographic/Pareto** — transparent arithmetic is what makes
  explanations exact; config profiles cover priority orderings in practice. A Pareto-front
  report can be added later without changing the model.
- **Daily buckets over continuous-time capacity** — a conservative approximation knob with a
  clean refinement path (§5), versus a large modeling complexity jump.
- **Deterministic-time solver budgets in reproducible modes** — wall-clock budgets make
  results machine-dependent even single-threaded; `max_deterministic_time` keeps benchmarks
  comparable across machines.

## 15. Risks and mitigations

- **CP-SAT scaling.** Mitigate: feasibility pruning and zone-grouping shrink the model;
  time-bucket coarsening; decomposition by capacity-connected components (the same structure
  §7.6 tier 2 uses); warm starts from incumbent assignments; flow relaxation as bound to
  report meaningful gaps.
- **Batch explanation fidelity.** Global optima lack local "reasons"; mitigated by
  Δ-vs-best-alternative and binding-constraint reporting (§7.5) rather than pretending.
- **Determinism erosion.** The rules in §2 are load-bearing and easy to violate accidentally
  (a stray UUID, a wall-clock read, an unpinned tzdata bump); the byte-identical-log property
  test is the tripwire.
- **Synthetic-workload realism.** Profiles per vertical with pack-supplied distributions;
  benchmarks report across a suite, not one flattering scenario.
- **Scope creep toward WMS/ERP.** The vision's non-goals are enforced in review; any feature
  that only makes sense with barcode guns or invoices is out.
- **UI absorbing the project.** UI is the final stage, after the engine has already proven
  its claims headless — including the neutrality packs and the benchmark suite; the
  demo exists before the first React component.
