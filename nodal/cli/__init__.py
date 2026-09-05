"""Nodal command-line interface."""

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

import nodal
from nodal.domain.capacity import DIMENSIONS
from nodal.events import EventStore, ensure_snapshots, load_state

if TYPE_CHECKING:
    from nodal.allocate import ObjectiveConfig

app = typer.Typer(
    name="nodal",
    help="Nodal: industry-neutral industrial logistics network optimizer.",
    no_args_is_help=True,
    add_completion=False,
)

world_app = typer.Typer(help="World databases (event logs).", no_args_is_help=True)
app.add_typer(world_app, name="world")

plan_app = typer.Typer(help="Drafted batch plans (§7.5).", no_args_is_help=True)
app.add_typer(plan_app, name="plan")


@app.callback()
def _root() -> None:
    """Nodal: industry-neutral industrial logistics network optimizer."""


@app.command()
def version() -> None:
    """Print the engine version."""
    typer.echo(nodal.__version__)


def _parse_at(at: str | None) -> datetime | None:
    if at is None:
        return None
    parsed = datetime.fromisoformat(at)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _load_profile(profile: Path | None) -> "ObjectiveConfig":
    """Build the objective config and validate its packs UP FRONT — a typo'd
    pack name fails clean here, never as a traceback mid-solve."""
    from nodal.allocate import ObjectiveConfig
    from nodal.rules.framework import PackError, load_packs

    config = ObjectiveConfig.from_yaml(profile) if profile else ObjectiveConfig()
    try:
        load_packs(config.packs)  # memoized: the solve pays nothing extra
    except PackError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(code=1) from None
    return config


@world_app.command("load")
def world_load(
    world_file: Annotated[Path, typer.Argument(help="World YAML file.")],
    db: Annotated[Path, typer.Option("--db", help="Event database to create/append.")],
    packs: Annotated[
        str | None,
        typer.Option(
            "--packs",
            help="Validate attribute bags against these packs (comma-separated, §8).",
        ),
    ] = None,
) -> None:
    """Ingest a YAML world file into an event database."""
    from nodal.rules.framework import PackError, load_packs
    from nodal.worlds import WorldError, load_world

    try:
        pack_list = load_packs([p.strip() for p in packs.split(",")]) if packs else None
    except PackError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(code=1) from None
    with EventStore(db) as store:
        try:
            count = load_world(world_file, store, packs=pack_list)
        except WorldError as err:
            typer.echo(str(err), err=True)
            raise typer.Exit(code=1) from None
        snapshots = ensure_snapshots(store)
    typer.echo(f"loaded {count} events into {db} ({snapshots} snapshots written)")


@app.command()
def state(
    db: Annotated[Path, typer.Option("--db", help="Event database.")],
    at: Annotated[
        str | None, typer.Option("--at", help="ISO timestamp for historical state.")
    ] = None,
) -> None:
    """Render the network state (optionally as of --at)."""
    with EventStore(db) as store:
        network = load_state(store, at=_parse_at(at))
    if network.last_ts is None:
        typer.echo("empty world (no events)")
        return
    day = network.last_ts.astimezone(UTC).date()
    typer.echo(f"state at {network.last_ts.isoformat()}  (events: {network.last_seq})")
    typer.echo(f"facilities: {len(network.facilities)}  lanes: {len(network.lanes)}")
    for facility in network.facilities.values():
        zones = network.zones_of(facility.id)
        open_now = "open" if network.facility_open(facility.id, network.last_ts) else "closed"
        typer.echo(f"  {facility.id:<10} {facility.name:<24} zones={len(zones)} {open_now}")
        for zone in zones:
            occupancy = network.occupancy(zone.id, day)
            capacity = network.effective_capacity(zone.id, day)
            parts = []
            for dim in DIMENSIONS:
                cap = capacity.get(dim)
                if cap is not None:
                    parts.append(f"{dim} {occupancy.demand(dim)}/{cap}")
            lots = len(network.lots_in_zone(zone.id))
            typer.echo(f"    {zone.id:<10} {zone.kind:<6} lots={lots:<4} " + "  ".join(parts))
    by_status: dict[str, int] = {}
    for shipment in network.shipments.values():
        by_status[shipment.status.value] = by_status.get(shipment.status.value, 0) + 1
    if by_status:
        summary = "  ".join(f"{k}={v}" for k, v in sorted(by_status.items()))
        typer.echo(f"shipments: {summary}")
    if network.last_ts is not None:
        active = network.active_disruptions(network.last_ts)
        if active:
            typer.echo("disruptions:")
            for d in active:
                typer.echo(
                    f"  {d.id:<10} {d.kind.value:<18} target={d.target_id}"
                    f" until={d.until_ts.isoformat()} magnitude={d.magnitude:.2f}"
                )


@app.command()
def allocate(
    shipment_id: Annotated[str, typer.Argument(help="Shipment to allocate.")],
    db: Annotated[Path, typer.Option("--db", help="Event database.")],
    profile: Annotated[
        Path | None, typer.Option("--profile", help="Objective profile YAML.")
    ] = None,
    explain: Annotated[bool, typer.Option("--explain", help="Full explanation.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the record as JSON.")] = False,
    do_commit: Annotated[
        bool, typer.Option("--commit", help="Append the decision to the log.")
    ] = False,
) -> None:
    """Decide a destination for one shipment, with the full explanation."""
    from nodal.allocate import allocate as decide
    from nodal.allocate import commit
    from nodal.allocate.render import render_record
    from nodal.rules.framework import load_packs

    config = _load_profile(profile)
    with EventStore(db) as store:
        network = load_state(store)
        record = decide(network, shipment_id, config)
        if as_json:
            typer.echo(record.model_dump_json(indent=2))
        else:
            typer.echo(render_record(record, load_packs(config.packs), explain=explain))
        if do_commit:
            if record.chosen is None:
                typer.echo("nothing to commit: no feasible destination", err=True)
                raise typer.Exit(code=1)
            # Booking one shipment by hand stales any drafted batch; the discard
            # that terminates it rides in the same append (§7.5).
            envelopes = commit(store, record, pending_plan=network.pending_plan)
            typer.echo(f"committed {len(envelopes)} events (through {envelopes[-1].id})")


@app.command()
def optimize(
    db: Annotated[Path, typer.Option("--db", help="Event database.")],
    profile: Annotated[
        Path | None, typer.Option("--profile", help="Objective profile YAML.")
    ] = None,
    rebalance: Annotated[
        bool, typer.Option("--rebalance", help="Propose rebalancing transfers (§7.7).")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit records as JSON.")] = False,
    do_commit: Annotated[
        bool, typer.Option("--commit", help="Append the batch decision to the log.")
    ] = False,
) -> None:
    """Batch-optimize every planned shipment simultaneously (§7.4).

    Without `--commit` the plan is DRAFTED: the proposal is appended to the log
    as `PlanDrafted` (§7.5), so it survives a reload and the operator can commit
    it (`optimize --commit`) or throw it away (`plan discard`). The one exception
    is `--rebalance` without `--commit`, which previews transfers that were never
    registered — drafting a plan over shipments the log has never seen would put
    a proposal in the log that nothing can execute.

    With a plan pending, `--commit` books THAT plan: the re-solve keeps its batch
    id and is refused outright if it no longer matches what was reviewed. This is
    the same engine guard the API commits through — the CLI cannot book past a
    review the browser would refuse.
    """
    from nodal.allocate.batch import PlanMismatch, commit_reviewed, draft_batch, solve_batch
    from nodal.allocate.engine import superseding_discard
    from nodal.allocate.rebalance import generate_rebalancing_transfers
    from nodal.events import catalog as ev
    from nodal.events.envelope import EventDraft
    from nodal.events.fold import fold

    config = _load_profile(profile)
    with EventStore(db) as store:
        network = load_state(store)
        now = network.last_ts
        if now is None:
            typer.echo("empty world", err=True)
            raise typer.Exit(code=1)
        if rebalance:
            # Unique per invocation, so repeated runs never mint colliding ids.
            prefix = f"TRF-{store.last_seq()}"
            transfers = generate_rebalancing_transfers(network, config, now, id_prefix=prefix)
            if transfers:
                drafts = [
                    EventDraft(ts=now, payload=ev.TransferOrdered(shipment=t)) for t in transfers
                ]
                if do_commit:
                    # Registering transfers stales any drafted plan; the discard
                    # that terminates it rides in the same append (§7.5).
                    envelopes = store.append(
                        [
                            *superseding_discard(network.pending_plan, now, "TransferOrdered"),
                            *drafts,
                        ],
                        actor="cli",
                    )
                    fold(envelopes, into=network)
                else:
                    # Dry run: preview the transfers on a copy; the log is untouched.
                    from nodal.events.envelope import Envelope

                    network = network.model_copy(deep=True)
                    for i, draft in enumerate(drafts):
                        entity_type, entity_id = draft.payload.entity_ref()
                        fold(
                            [
                                Envelope(
                                    seq=network.last_seq + 1,
                                    id=f"EVT-preview-{i}",
                                    ts=now,
                                    type=draft.payload.EVENT_TYPE,
                                    entity_type=entity_type,
                                    entity_id=entity_id,
                                    payload=draft.payload,
                                    actor="cli",
                                )
                            ],
                            into=network,
                        )
                typer.echo(
                    f"proposed {len(transfers)} rebalancing transfers"
                    + ("" if do_commit else " (preview only; --commit to register)")
                )
        planned = sorted(sid for sid, s in network.shipments.items() if s.status.value == "planned")
        if not planned:
            typer.echo("nothing to optimize: no planned shipments")
            return
        # Unique per invocation: a batch id is an entity identity in the log
        # (BatchSolved, PlanDrafted), and two solves must never alias. The one
        # exception is committing a plan under review, which keeps the DRAFT's
        # id so the audit chain reads PlanDrafted -> BatchSolved on one batch —
        # `commit_reviewed` owns that, and the refusal that goes with it.
        fresh_id = f"BATCH-CLI-{store.last_seq()}"
        if do_commit:
            try:
                result, booked = commit_reviewed(
                    store, network, planned, config, now, batch_id=fresh_id, actor="cli"
                )
            except PlanMismatch as err:
                typer.echo(str(err), err=True)
                raise typer.Exit(code=1) from None
            committed = len(booked)
        else:
            result = solve_batch(network, planned, config, now, batch_id=fresh_id)
            committed = 0
        meta = result.meta
        gap_text = f"{meta.gap:.4%}" if meta.gap is not None else "n/a"
        typer.echo(
            f"batch {meta.batch_id}: {meta.status}, {meta.shipments} shipments, gap {gap_text}"
        )
        for sid in sorted(result.records):
            record = result.records[sid]
            if record.chosen is None:
                typer.echo(f"  {sid}: UNASSIGNED")
            else:
                context = record.batch_context
                delta = (
                    f"  (best alt {context.best_alternative},"
                    f" delta {context.delta_vs_best_alternative:+.4f})"
                    if context is not None and context.delta_vs_best_alternative is not None
                    else ""
                )
                typer.echo(f"  {sid}: {record.chosen.facility_id}/{record.chosen.zone_id}{delta}")
        if as_json:
            for sid in sorted(result.records):
                typer.echo(result.records[sid].model_dump_json(indent=2))
        if do_commit:
            typer.echo(f"committed {committed} events")
        elif rebalance:
            typer.echo("plan not drafted: the previewed transfers are not in the log")
        else:
            draft_batch(store, result)
            typer.echo(
                f"drafted plan {result.meta.batch_id} at seq {store.last_seq()}"
                f" (optimize --commit to book it, plan discard to throw it away)"
            )


@plan_app.command("discard")
def plan_discard(
    db: Annotated[Path, typer.Option("--db", help="Event database.")],
    reason: Annotated[str, typer.Option("--reason", help="Why the plan was rejected.")] = "",
) -> None:
    """Throw away the drafted plan sitting at the log head (§7.5).

    Nothing is unbooked — a draft books nothing — but a review that ended in NO
    is history too, and appending the discard is what un-pends the plan.
    """
    from nodal.events import catalog as ev
    from nodal.events.envelope import EventDraft

    with EventStore(db) as store:
        network = load_state(store)
        pending = network.pending_plan
        if pending is None or network.last_ts is None:
            typer.echo("no plan is drafted at the log head", err=True)
            raise typer.Exit(code=1)
        store.append(
            [
                EventDraft(
                    ts=network.last_ts,
                    payload=ev.PlanDiscarded(batch_id=pending.batch_id, reason=reason),
                )
            ],
            actor="cli",
        )
    typer.echo(f"discarded plan {pending.batch_id}")


@app.command()
def whatif(
    db: Annotated[Path, typer.Option("--db", help="Event database (opened read-only).")],
    event: Annotated[
        list[str],
        typer.Option(
            "--event",
            help="Hypothetical, repeatable: 'close FAC-3 14d', 'cut ZON-2 0.5 7d',"
            " 'delay SHP-9 24h', 'spike 5 12'.",
        ),
    ],
    at: Annotated[
        str | None, typer.Option("--at", help="Fork point (ISO timestamp; default: now).")
    ] = None,
    policy: Annotated[str, typer.Option("--policy", help="Decision policy.")] = "nodal-batch",
    profile: Annotated[
        Path | None, typer.Option("--profile", help="Objective profile YAML.")
    ] = None,
) -> None:
    """Run a what-if fork (§4) and print the baseline-vs-fork diff."""
    from nodal.whatif import WhatIfError, run_whatif

    config = _load_profile(profile)
    with EventStore(db) as store:
        try:
            report = run_whatif(store, config, policy, event, at=_parse_at(at))
        except WhatIfError as err:
            typer.echo(str(err), err=True)
            raise typer.Exit(code=1) from None
    typer.echo(report.render())


@app.command()
def simulate(
    scenario: Annotated[Path, typer.Argument(help="Scenario YAML.")],
    policy: Annotated[str, typer.Option("--policy", help="Allocation policy.")] = "nodal-single",
    db: Annotated[Path | None, typer.Option("--db", help="Event database to write.")] = None,
    seed: Annotated[int | None, typer.Option("--seed", help="Seed override.")] = None,
) -> None:
    """Run one scenario under one policy; print the KPI report."""
    from nodal.bench.kpi import compute_kpis
    from nodal.sim.runner import SimError, run_scenario
    from nodal.sim.scenarios import load_scenario

    spec = load_scenario(scenario)
    if db is None:
        db = Path("var") / "sim" / f"{spec.name}-{policy}.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
    try:
        stats = run_scenario(spec, policy, db, seed=seed)
    except SimError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(compute_kpis(stats).model_dump_json(indent=2))


# The standard suite (§10): every checked-in scenario, generic and vertical.
SUITE_SCENARIOS = (
    "generic-small",
    "generic-contention",
    "generic-broad",
    "generic-rebalance",
    "disrupted-generic",
    "coldchain-mixed",
    "coldchain-tight",
    "chem-segregated",
    "mixed-pack",
)


@app.command()
def bench(
    scenarios: Annotated[list[Path] | None, typer.Argument(help="Scenario YAML files.")] = None,
    suite: Annotated[
        bool, typer.Option("--suite", help="Run the standard checked-in scenario suite.")
    ] = False,
    policies: Annotated[
        str,
        typer.Option(
            "--policies",
            help="Comma-separated policy names. Batch policies (nodal-batch, "
            "flow-relax) solve at every batch tick and run much longer.",
        ),
    ] = "nodal-single,nodal-batch,flow-relax,nearest-feasible,first-available,greedy",
    out: Annotated[Path, typer.Option("--out", help="Report directory.")] = Path("var/bench"),
    seed: Annotated[int | None, typer.Option("--seed", help="Seed override.")] = None,
) -> None:
    """Run the scenario x policy matrix; write reports and print the comparison."""
    from nodal.bench.harness import run_bench
    from nodal.bench.render import render_comparison

    if suite:
        scenarios = [Path("scenarios") / f"{name}.yaml" for name in SUITE_SCENARIOS]
    if not scenarios:
        typer.echo("pass scenario files or --suite", err=True)
        raise typer.Exit(code=1)
    missing = [str(path) for path in scenarios if not path.exists()]
    if missing:
        typer.echo(
            f"scenario files not found: {', '.join(missing)}"
            + (" (--suite must run from the repository root)" if suite else ""),
            err=True,
        )
        raise typer.Exit(code=1)
    reports = run_bench(scenarios, [p.strip() for p in policies.split(",")], out, seed=seed)
    typer.echo(render_comparison(reports))
    typer.echo(f"reports written to {out}")


def main() -> None:
    app()
