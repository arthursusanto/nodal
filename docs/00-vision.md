# Nodal — vision and framing

This document is the **source of truth** for what Nodal is. Architecture and roadmap decisions
are subordinate to it. If a proposed feature or refactor conflicts with this framing, the
feature is wrong, not the framing.

## Purpose

Nodal is an **industry-neutral industrial logistics network optimizer**. It is not primarily a
warehouse inventory application, a chemical-management system, an ERP replacement, or a 3D
visualization. Its core purpose is to optimize how shipments and inventory are allocated across
a distributed network of facilities.

The central problem:

> Given the current inventory, available capacity, facility capabilities, shipment
> requirements, the transportation network, deadlines, and operating constraints — determine
> where shipments should go and how inventory should be distributed.

Two decision shapes share that problem. An **allocation** shipment has an origin and no
destination: the optimizer chooses which facility should receive and store it. A **delivery**
shipment (A→B) additionally names a customer endpoint outside the network: the optimizer
routes it origin → entry facility → through the lane network → exit facility → customer,
choosing where in the network the cargo is held for its required hold time. The operator
leaves the destination blank for allocation; setting one makes it a delivery.

## The operational model

To make those decisions the system needs an accurate operational model of the network. It
must represent:

- Facilities and their geographic locations
- Storage zones within each facility
- Current and forecast capacity
- Inventory lots, containers, quantities, and reservations
- Inbound, outbound, and in-transit shipments
- Facility capabilities, equipment, certifications, and operating hours
- Transportation routes, costs, distances, and expected travel times
- Shipment requirements: size, weight, temperature, handling equipment, security,
  compatibility, and delivery deadline
- Customer endpoints and hold requirements for A→B deliveries (first/last-mile road legs,
  required in-network hold time)
- Temporary disruptions: facility closures, delayed shipments, unavailable equipment,
  lost capacity

## The decision pipeline

For each shipment (or batch of shipments):

1. **Feasibility first.** Hard constraints eliminate infeasible destinations. Every
   elimination is recorded with the specific constraint responsible.
2. **Optimization second.** The remaining assignments are optimized against configurable
   objectives: transportation cost, travel distance or time, delivery lateness risk, facility
   utilization, inventory balance, future capacity preservation, number of inter-facility
   transfers, and operational risk.
3. **Explanation always.** The result must show which facilities were considered, which were
   rejected and by exactly which constraint, why the selected destination scored best, and how
   the decision affects future capacity.

Inventory tracking and mapping are necessary parts of the optimizer — they supply the state the
engine needs and make its decisions inspectable. They are not separate headline features.

## Technical core

The engine, not the interface, is the substance of the project:

- Constraint-based optimization — min-cost flow where the structure permits, CP-SAT/MIP for
  the general case
- A geospatial data model and routing calculations
- An auditable, append-only event history for all inventory and shipment movements
- Historical state reconstruction (the network as it was at any past instant)
- Disruption simulation and re-optimization
- Synthetic workload generation
- Benchmarks against simple policies: nearest-facility, first-available, greedy allocation

## Industry neutrality

The core is industry-neutral. Industry-specific restrictions are modular rule packs layered on
a shared constraint vocabulary. Candidate verticals: third-party logistics, cold-chain
pharmaceuticals, food distribution, chemicals, batteries, bulk liquids, automotive parts,
disaster-response logistics. Shipping at least two non-trivial packs is part of the project's
definition of done — one pack proves nothing about neutrality.

## The interface

The UI is a client of the engine, built late, and includes:

- A global map of facilities, shipments, routes, disruptions, and capacity
- Current and forecast inventory at each location
- Inbound and outbound shipment schedules
- Recommended allocations and routes, with their explanations
- Facility-level layouts showing storage zones and occupancy
- Optional 3D facility views **only where spatial layout genuinely matters**
- What-if simulation: closures, delays, demand spikes, capacity changes
- Historical replay of the network at an earlier point in time

## Invariants (anti-collapse guarantees)

1. **Headless-meaningful.** Remove the entire graphical interface and the project remains
   technically meaningful: engine + CLI can model, allocate, explain, simulate, and benchmark.
2. **Event log is the only source of truth.** All state is derived by folding events; nothing
   mutates state directly. This is what makes audit, replay, and what-if forks real features
   rather than bolted-on views.
3. **Every decision is explainable and reproducible.** Same state + same configuration ⇒ same
   decision, with a machine-readable record of candidates, rejections, scores, and capacity
   impact.
4. **Deterministic under seed.** Simulations and benchmarks are reproducible end to end.
5. **Benchmarked against baselines.** The optimizer is measured against naive policies on
   generated workloads, with the same feasibility surface for every policy.

## Non-goals

- No ERP/WMS feature set: no purchasing, billing, labor management, barcode/label workflows.
- No multi-tenancy or SaaS packaging (a local bearer token guards the API; that is the extent
  of auth).
- No live integrations with external *operational* systems (carrier APIs, telematics, ERP
  connectors). One deliberate exception: a maps provider (geocoding, place lookup, road
  geometry) for composing shipments and rendering real road routes — presentation and input
  aids only, cached and snapshot onto world data, with graceful degradation when absent. The
  optimizer itself never depends on it: engine decisions stay offline-deterministic.
- No CRUD-first UI: forms exist only where event ingestion needs them.
- No 3D for its own sake.

The project must not collapse into a CRUD warehouse dashboard with a small optimizer attached.
