# Nodal — staged implementation roadmap

Subordinate to [00-vision.md](00-vision.md); architecture references are to
[01-architecture.md](01-architecture.md). Stages are ordered so that **every stage ends with a
demonstrable, headless increment of the actual thesis** — model → explainable decisions →
measure → optimize → stress → prove neutrality and write it up → visualize. The UI is the
final stage, after the engine has already proven every headline claim headless.

Conventions: each stage lists Goal / Deliverables / Acceptance / Out of scope / Size
(S = small, M = medium, L = large, relative to one another). Global definition of done for
every stage: tests and CI green, docs updated where behavior changed, a CLI entry point
demonstrates the new capability, no engine dependency on `server/` or `web/`.

---

## Stage 0 — Skeleton (S)

**Goal.** A repo where every later stage lands with tests from day one.

**Deliverables.** `pyproject.toml` (package `nodal`, console script `nodal`), pinned dev env
(including `tzdata` — §2), ruff + strict mypy on engine packages, pytest wiring, GitHub
Actions (lint + type + test on push/PR), `nodal --help` and `nodal version`.

**Acceptance.** Fresh clone → documented setup → CI and local runs green; `nodal --help` works.

**Out of scope.** Any domain code.

## Stage 1 — Event core and world state (M)

**Goal.** The event-sourced network model: the part everything else stands on.

**Deliverables.** Domain entities (§3); event envelope + catalog (§4); SQLite append-only
store with seq/ts ordering rules; fold → `NetworkState`, **including folding disruptions into
effective capacity/availability** (§3); zstd-compressed snapshots; `state_at(t)`; time-phased
occupancy/headroom with lot planned-departure and reservation-consumption semantics (§5);
scenario/world loader (YAML fixtures); `nodal state [--at TS]` rendering facilities, zones,
occupancy, shipments in flight, active disruptions.

**Acceptance.**
- Property tests: fold(snapshot + tail) ≡ fold(all events); event serialization round-trips;
  appends reject non-monotonic `ts`.
- `state_at` reproduces a hand-checked historical state on a fixture world exactly, including
  effective capacity under an active disruption.
- Occupancy never double-counts a shipment across its reservation and its received lots
  (fixture walks arrival through `ShipmentArrived`/`LotReceived` and checks every bucket).
- No code path mutates state except event append: the public state API exports no mutators,
  and a test suite asserts the module surface.
- Envelope-scale check on a generated world (~10⁶ events, ~10⁵ lots — §13): fold from
  nearest snapshot < 2 s on the dev machine.

**Out of scope.** Constraints, allocation, travel.

## Stage 2 — Feasibility and explainable single-shipment allocation (L)

**Goal.** The first real decisions, with the full explanation contract.

**Deliverables.** Constraint framework + core constraint set incl. disruption overlays (§7.2);
lane graph, travel model, shortest paths, operating-window waiting semantics, deadline
reachability (§6); objective components + scorer with config profiles (§7.3, §7.8);
`DecisionRecord` + `AllocationDecided`/`ReservationPlaced` events with template-rendered
messages (§7.5); reservation booking against future buckets; **pack registry with
`packs/core`** (the generic vocabulary that proves the default path is industry-free) **and a
thin `packs/coldchain`** (temp-band attribute schema + constraint + cert tag) proving the
plug-point with a second, restrictive pack (§8); `nodal allocate SHP-X [--explain] [--json]`.

**Acceptance.**
- On fixture worlds, every facility in the network appears in the record as chosen, scored,
  or rejected — no silent pre-filtering; a facility closed by disruption shows `FACILITY_CLOSED`
  as its rejection; a candidate failing multiple constraints reports all of them.
- Same state + same config ⇒ byte-identical decision record (records carry no timings).
- A documented weight-flip example: changing one weight in the objective profile changes the
  chosen facility, and the record's arithmetic shows exactly why.
- Reservations booked by a decision make a later conflicting shipment infeasible in the
  overlapping buckets (future-capacity test).
- A shipment arriving outside operating hours is priced with waiting time, not rejected,
  unless waiting breaks its deadline.
- Core imports no pack; each pack passes the conformance suite; disabling `coldchain` removes
  its constraints cleanly.

**Out of scope.** Batch optimization, rebalancing, simulation, re-optimization.

## Stage 3 — Simulator, baselines, benchmark harness (M)

**Goal.** The measuring apparatus — naive policies and outcome KPIs — *before* the heavy
optimizer, so stage 4 has a bar to clear.

**Deliverables.** Virtual clock + scheduled event queue; seeded workload generator (topology,
Poisson demand with modulation, attribute distributions, demand rates, disruption schedule)
(§9) — disruptions fold into state (stage 1) and affect feasibility (stage 2), though nothing
re-optimizes yet; policy interface + `nearest-feasible`, `first-available`, `greedy`,
`nodal-single`; KPI module with the outcome/performance split (§10);
`nodal simulate --scenario S --policy P --seed N`; `nodal bench` producing JSON + rendered
comparison table; standard scenario suite (≥5 scenarios: generic 3PL + coldchain profiles,
including at least one capacity-contention scenario).

**Acceptance.**
- Same scenario/seed/policy ⇒ byte-identical event log and outcome-KPI report, twice in a
  row; performance KPIs reported separately with a hardware note and excluded from the
  byte-identity check.
- Bench report compares all policies on the full outcome-KPI set (§10), states seeds and
  configs, and renders losses as plainly as wins.
- On the documented contention scenario **at its documented seed**, `nodal-single` is
  strictly cheapest and serves at least as many shipments (unallocated count) as every naive
  baseline. This is a per-seed regression pin, published together with a multi-seed sweep
  that shows where single-shipment scoring wins and where it doesn't — the full answer to
  contention is stage-4 batch optimization. (On-time rate is structurally 1.0 for every
  policy until stage-5 delay mechanics land: deadline-infeasible candidates are
  hard-filtered, so service failure appears as `unallocated`, not lateness.)
  Aggregate suite results are published as they land; no gate requires winning everywhere,
  and no scenario may be dropped or retuned to manufacture a win (the suite is
  version-controlled; changes to it are reviewed like code).
- Small-scale bench suite runs in CI: outcome KPIs asserted exactly against goldens;
  performance KPIs are reported in their own section and excluded from gating entirely
  (wall-clock on shared CI runners is noise, not signal).

**Out of scope.** Batch/CP-SAT, rebalancing, re-optimization.

## Stage 4 — Batch optimization and rebalancing (L)

**Goal.** The network-level optimizer — the project's technical peak — and the inventory-
distribution machinery.

**Deliverables.** CP-SAT batch model at zone-group granularity (feasible-pair variables,
unassigned penalty, bucket-capacity constraints, class-indicator segregation encoding,
per-facility congestion and per-group inv_balance PWL terms, integer scaling) (§7.4);
demand-rate targets, imbalance KPI, and rebalancing transfer generation entering the same
solve (§7.7); min-cost-flow fast path behind an explicit structural gate; optimality gaps
from CP-SAT's proven bound; deterministic-time solver budgets with status/gap reporting;
batch decision records with Δ-vs-best-alternative and binding constraints (§7.5);
`nodal optimize`; `nodal-batch` and the `flow-relax` heuristic in the bench matrix.

**Acceptance.**
- Contention fixture where `nodal-batch` strictly beats `nodal-single` sequential allocation;
  both results explained.
- Rebalancing fixture: a skewed-stock world converges toward targets within the configured
  transfer cap, imbalance KPI improves by a stated margin, and each transfer's record shows
  balance gain vs transport + churn cost.
- Metamorphic suite passes as scoped in §12: constraint-tightening and permutation-invariance
  properties always; capacity/lane-addition and shipment-removal monotonicity under
  monotone configs; single-shipment batch ≡ scorer argmin under **all** configs.
- Performance gate: at 50-facility scale on two-day-cohort instances (~140 shipments — a
  heavy tick backlog, the §7.4/§9 operating regime), ≥ 90% of 10 seeded instances reach
  proven gap ≤ 5% within a 30 s wall budget in the interactive solver configuration
  (8 workers, wall-clock budget, flow-relax warm start — the §2-sanctioned non-reproducible
  mode, `reproducible=False` on every such record). The warm start is a validated feasible
  hint, never the trivial all-unassigned solution, which anchors LNS on a poor incumbent.
  Budget-exhausted runs still return best incumbent + gap, and a run with no incumbent even
  after the hinted re-solve returns the explicit `NO_INCUMBENT`.
- Fast path engages only when the structural conditions hold (tested on cases just inside and
  just outside each condition).

**Out of scope.** Disruption-triggered re-solving, UI.

## Stage 5 — Disruptions, re-optimization, what-if (M)

**Goal.** The network under stress — the capability the whole framing points at.

**Deliverables.** Tiered re-optimization: affected-set computation, capacity-connected
escalation, churn penalty, `AllocationSuperseded` audit chain (§7.6); what-if forks from
`state_at(t)` with overlay streams and parent pointers (§4); baseline-vs-fork diff report
(KPIs + changed decisions); `nodal whatif --at TS --event 'close FAC-3 14d' --policy P`;
re-optimization time as a performance KPI; disruption scenarios joining the standard suite.

**Acceptance.**
- Closure fixture: tier 1 re-solves only the affected set; a constructed cascade fixture
  (displaced shipments contending at a nearly-full second-best facility) triggers tier 2 and
  finds the feasible chain instead of unassigning; reassignment count respects the churn
  configuration; every superseded decision links old → new records; records state which tier
  ran.
- What-if fixtures for **all four framing categories** — closure, delay, demand spike,
  capacity change — produce correct diffs; what-if runs leave the baseline event log
  byte-identical.
- Bench reports re-optimization time and post-disruption outcome-KPI deltas per policy on the
  disruption scenarios (the report shows how each policy, naive and optimizing, absorbs the
  hit).

**Out of scope.** UI.

## Stage 6 — Vertical packs (M)

**Goal.** Prove industry neutrality with a second restrictive vertical. This is a headline
claim of the framing — it lands **before** the UI, so a runway-truncated project still ships
its substance.

**Deliverables.** `packs/coldchain` completed (lane temperature-excursion budget objective,
GDP-style cert constraints, generator profile); `packs/chem` (segregation matrix, zone
certification classes, generator profile); pack conformance suite finalized (§8); scenario
suite extended per pack, including a mixed-pack scenario, runnable as one matrix with
`nodal bench --suite`.

**Acceptance.**
- Both packs pass conformance; core still imports no pack; the mixed-pack scenario (chem +
  coldchain shipments in one network) allocates correctly.
- Segregation fixture: incompatible classes never share a zone — enforced by the solver's
  class indicators, not post-hoc — and rejection records name the matrix entry that fired.
- The suite's outcome KPIs regenerate byte-identically from checked-in scenarios + seeds.

**Out of scope.** UI, 3D.

## Stage 7 — API and web UI (L)

**Goal.** Make the engine's existing capabilities visible; add none in the UI layer.

**Deliverables.** FastAPI read models + command endpoints (§11); React/TS app: global map
(facilities by utilization, shipment arcs, disruptions, routes), facility 2D zone/occupancy
view with timelines, forecast-inventory view, decision explorer rendering the full record,
what-if panel, replay scrubber, schedules view.

**Acceptance.**
- Browser demo of the full loop: seed world → allocate with visible explanation → inject
  disruption → watch re-optimization → what-if compare → replay scrub to before the
  disruption.
- Engine test suite still passes with `server/` and `web/` absent; CI proves it.
- Every UI state reachable with OS reduced-motion enabled; popover dismissal uses catch
  layers.
- Decision explorer shows, for a rejected facility, the exact constraint data the engine
  recorded (spot-checked against `--json` output).

**Out of scope.** 3D (unless §11's gating criterion has been met by then), auth, multi-user.
