"""FastAPI app (§11): read models + commands over one world database.

Thin translation of the engine API. State is folded once and cached against the
log head (a MAX(seq) query per request); every command appends through the same
engine code paths the CLI uses, then the cache refreshes.
"""

import functools
import secrets
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field

from nodal.allocate import ObjectiveConfig
from nodal.allocate.engine import AllocateError, allocate, commit, superseding_discard
from nodal.allocate.reopt import ReoptResult, TrappedCargo, reoptimize
from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import (
    Certification,
    Destination,
    Disruption,
    DisruptionKind,
    Facility,
    Lane,
    LotSpec,
    RequirementSet,
    Shipment,
    ShipmentStatus,
    StorageZone,
)
from nodal.domain.units import currency, kg, m3
from nodal.events import EventStore, load_state
from nodal.events import catalog as ev
from nodal.events.envelope import Envelope, EventDraft
from nodal.events.state import NetworkState
from nodal.network.travel import haversine_km
from nodal.whatif import WhatIfError, run_whatif
from server import maps, readmodels


def _aware(at: datetime | None) -> datetime | None:
    if at is None or at.tzinfo is not None:
        return at
    return at.replace(tzinfo=UTC)


def _trapped_rows(trapped: list[TrappedCargo]) -> list[dict[str, Any]]:
    """Cargo the closure stranded (§7.9): it keeps its booking, nothing
    re-routes it, and an operator has to clear it by hand — so every command
    that can produce it says so."""
    return [
        {
            "shipment_id": entry.shipment_id,
            "facility_id": entry.facility_id,
            "disruption_id": entry.disruption_id,
        }
        for entry in trapped
    ]


def _reopt_body(result: ReoptResult) -> dict[str, Any]:
    """The one §7.6 outcome shape every command reports."""
    return {
        "tier": result.tier,
        "affected": result.affected,
        "moved": result.changed,
        "released": result.released,
        "trapped": _trapped_rows(result.trapped),
    }


class _World:
    """One open world: store + a folded state cached against the log head.

    Requests run on a threadpool, so every handler serializes through `lock`
    (single-user desktop scale; correctness beats concurrency here — and the
    engine's head-checked commits would refuse interleaved writes anyway).
    """

    def __init__(self, db_path: str | Path, config: ObjectiveConfig) -> None:
        self.store = EventStore(db_path, cross_thread=True)
        self.config = config
        self.lock = threading.Lock()
        self._state: NetworkState | None = None
        self._state_seq = -1

    def state(self) -> NetworkState:
        head = self.store.last_seq()
        if self._state is None or self._state_seq != head:
            self._state = load_state(self.store)
            self._state_seq = head
        return self._state

    def close(self) -> None:
        self.store.close()


class AllocateCommand(BaseModel):
    shipment_id: str
    commit: bool = False


class OptimizeCommand(BaseModel):
    commit: bool = False
    rebalance: bool = False
    # A reviewed plan may only be committed onto the log it was solved against:
    # when set, the command is refused (409) if the head has moved since. With a
    # plan pending this is the seq that plan READ (`based_on_seq`) — the draft's
    # own event is not a move — and nothing else; with none pending it is the
    # current head.
    expected_head: int | None = None
    # Which plan the caller believes it is committing. `expected_head` alone
    # cannot tell two successive drafts apart (the second is based on the seq
    # that sat at the head while the first was pending), so a reviewer that
    # names the batch is refused (409) the moment it is not the pending one.
    batch_id: str | None = None


class DiscardPlanCommand(BaseModel):
    batch_id: str
    reason: str = ""


class WhatIfCommand(BaseModel):
    events: list[str] = Field(min_length=1)
    policy: str = "nodal-batch"
    at: datetime | None = None


class DisruptCommand(BaseModel):
    kind: DisruptionKind
    target_id: str
    days: float = Field(default=3.0, gt=0)
    magnitude: float = Field(default=1.0, ge=0, le=1)
    detail: str | None = None


class EndDisruptionCommand(BaseModel):
    disruption_id: str
    # None ends the disruption now; a timestamp ends it now AND opens a fresh
    # window from now until then (the log is append-only: an end time is never
    # edited in place, it is superseded by a new disruption).
    until: datetime | None = None


# Caller-supplied ids land in the append-only log, read models, and composed
# lane ids, so they are constrained; omitted (None) means "mint one for me" —
# an empty string is a 422, never a silent mint.
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class NewZone(BaseModel):
    # minted "<facility>-Z<n>" when omitted
    zone_id: str | None = Field(default=None, max_length=64, pattern=_ID_PATTERN)
    kind: str = "rack"
    slots: int | None = Field(default=None, gt=0)
    weight_kg: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    volume_m3: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    temp_c: tuple[int, int] | None = None
    allowed_classes: list[str] = Field(default_factory=list)


class RegisterFacilityCommand(BaseModel):
    # minted from the log head when omitted
    facility_id: str | None = Field(default=None, max_length=64, pattern=_ID_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    tags: list[str] = Field(default_factory=lambda: ["equip:forklift", "cross-dock"])
    cold_certified: bool = False
    hazmat_certified: bool = False
    cert_years: int = Field(default=5, gt=0, le=50)
    risk: float = Field(default=0.05, ge=0, le=1)
    zones: list[NewZone] = Field(default_factory=list)


class AddZoneCommand(NewZone):
    facility_id: str


class SetZoneCapacityCommand(BaseModel):
    zone_id: str
    slots: int | None = Field(default=None, gt=0)
    weight_kg: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    volume_m3: float | None = Field(default=None, gt=0, allow_inf_nan=False)


# Mode defaults mirror the global demo: speed, fixed handling, base cost.
_LANE_SPEED_KMH = {"road": 68.0, "sea": 38.0, "air": 850.0}
_LANE_HANDLING_MIN = {"road": 45, "sea": 2880, "air": 240}
_LANE_COST_FIXED = {"road": 420, "sea": 2600, "air": 5400}


class RegisterLaneCommand(BaseModel):
    from_facility_id: str
    to_facility_id: str
    mode: str = "road"
    km: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    minutes: int | None = Field(default=None, gt=0)
    cost_fixed: float | None = Field(default=None, ge=0, allow_inf_nan=False)  # currency units
    cost_per_kg_cents: float = Field(default=0.03, ge=0, allow_inf_nan=False)
    both_directions: bool = True


class RegisterShipmentCommand(BaseModel):
    # minted from the log head when omitted
    shipment_id: str | None = Field(default=None, max_length=64, pattern=_ID_PATTERN)
    origin_facility_id: str | None = None
    origin_label: str | None = Field(default=None, min_length=1, max_length=120)
    origin_lat: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    origin_lon: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    sku: str = "SKU"
    group: str = "general"
    quantity: int = Field(default=1, gt=0)
    uom: str = "unit"
    slots: int = Field(gt=0)
    weight_kg: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    temp_c: tuple[int, int] | None = None
    compat_class: str | None = None
    ready: datetime | None = None  # default: now
    deadline: datetime | None = None
    dwell_days: int | None = Field(default=None, gt=0)
    # A->B delivery (§7.9): a customer point outside the network, plus the hold
    # the customer requires. All three destination fields or none; with a
    # destination set, `hold_days` replaces `dwell_days` and a deadline (which
    # stays optional) means delivered AT THE DESTINATION by.
    destination_label: str | None = Field(default=None, min_length=1, max_length=120)
    destination_lat: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    destination_lon: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    hold_days: int = Field(default=0, ge=0, le=365)
    required_tags: list[str] = Field(default_factory=list)
    zone_kinds: list[str] | None = None  # default: cold+rack when temp-bound, else rack


class CancelShipmentCommand(BaseModel):
    shipment_id: str
    reason: str = ""


class SetReadyCommand(BaseModel):
    shipment_id: str
    new_ready: datetime
    reason: str = ""


def create_app(
    db_path: str | Path,
    config: ObjectiveConfig | None = None,
    token: str | None = None,
) -> FastAPI:
    """`token` gates every /api route behind `Authorization: Bearer <token>`
    (the docs pages stay open — they carry no data). None disables auth
    (tests, trusted localhost use)."""
    world = _World(db_path, config or ObjectiveConfig())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        world.close()

    app = FastAPI(
        title="nodal",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect",
        lifespan=lifespan,
        # Advertise the bearer scheme so /api/docs grows an Authorize button;
        # enforcement lives in the middleware below, not in this dependency.
        dependencies=[Depends(HTTPBearer(auto_error=False))] if token is not None else None,
    )

    if token is not None:
        expected = f"Bearer {token}".encode()

        # Registered BEFORE CORSMiddleware so CORS ends up outermost: browser
        # preflights (OPTIONS carries no Authorization) must succeed, and 401s
        # must still carry CORS headers.
        @app.middleware("http")
        async def _require_token(request: Any, call_next: Any) -> Any:
            # Default-deny: every route needs the token except the explicit
            # open set (the docs pages, which carry no data) and preflights.
            open_paths = ("/api/docs", "/api/openapi.json", "/api/docs/oauth2-redirect")
            if request.method != "OPTIONS" and request.url.path not in open_paths:
                supplied: str = request.headers.get("authorization", "")
                # Header values are latin-1 text; compare as bytes so a
                # non-ASCII probe gets a clean 401, never a 500.
                if not secrets.compare_digest(supplied.encode("latin-1"), expected):
                    return JSONResponse(status_code=401, content={"detail": "invalid token"})
            return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.world = world

    def locked(fn: Any) -> Any:
        """Serialize every handler through the world lock (see _World)."""

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with world.lock:
                return fn(*args, **kwargs)

        return wrapper

    def _append(drafts: list[EventDraft]) -> list[Envelope]:
        """THE append every command writes through (§7.5).

        A command that lands on top of a drafted plan stales it — the fold has
        always said so — but the log would then read "the optimizer proposed a
        plan, and then nothing". So the discard that terminates the draft rides
        along in the same atomic append, naming what superseded it. Read models
        are unaffected: the plan had already stopped being pending.

        A plan event says it itself (a discard IS the termination; the next
        draft supersedes visibly), so those pass through untouched. The other
        self-terminating append — the plan's own `BatchSolved` — is written by
        `commit_batch` under `commit_reviewed`, which never comes through here.
        """
        if not drafts:
            return []
        pending = world.state().pending_plan
        if any(isinstance(d.payload, ev.PlanDrafted | ev.PlanDiscarded) for d in drafts):
            pending = None
        return world.store.append(
            [
                *superseding_discard(pending, drafts[0].ts, drafts[0].payload.EVENT_TYPE),
                *drafts,
            ],
            actor="api",
        )

    # -- read models -----------------------------------------------------------

    @app.get("/api/map")
    @locked
    def api_map(at: datetime | None = None) -> dict[str, Any]:
        return readmodels.map_state(_state_at(at), world.store)

    def _state_at(at: datetime | None) -> NetworkState:
        # Replay consistency: every read model accepts the same `at` the map
        # uses, so no view shows live data under a REPLAY header.
        at = _aware(at)
        if at is not None:
            return load_state(world.store, at=at)
        return world.state()

    @app.get("/api/facilities/{facility_id}")
    @locked
    def api_facility(
        facility_id: str,
        days: int = Query(default=14, ge=1, le=366),
        at: datetime | None = None,
    ) -> dict[str, Any]:
        state = _state_at(at)
        if facility_id not in state.facilities:
            raise HTTPException(status_code=404, detail=f"unknown facility {facility_id}")
        return readmodels.occupancy_timeline(state, facility_id, days=days)

    @app.get("/api/forecast")
    @locked
    def api_forecast(
        days: int = Query(default=14, ge=1, le=366), at: datetime | None = None
    ) -> dict[str, Any]:
        return readmodels.forecast_inventory(_state_at(at), world.config, days=days)

    @app.get("/api/schedules")
    @locked
    def api_schedules(at: datetime | None = None) -> dict[str, Any]:
        return readmodels.schedules(_state_at(at), world.store)

    @app.get("/api/plan")
    @locked
    def api_plan(at: datetime | None = None) -> dict[str, Any]:
        """The drafted, uncommitted plan at the head (§7.5) — the same `batch`
        body a dry run returns, or null. One round trip re-enters plan review
        after a reload; under `at` it is the plan that was pending back then."""
        return {"batch": readmodels.pending_plan(_state_at(at))}

    @app.get("/api/decisions/{shipment_id}")
    @locked
    def api_decision(shipment_id: str) -> dict[str, Any]:
        record = readmodels.decision_record(world.store, shipment_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no decision for {shipment_id}")
        return record

    @app.get("/api/events")
    @locked
    def api_events(after_seq: int = 0, limit: int = 100) -> dict[str, Any]:
        return readmodels.events_feed(world.store, after_seq=after_seq, limit=limit)

    # -- geography (see server/maps.py) ----------------------------------------
    # Not `locked`: these read the outside world, never the log, so they must
    # not queue behind a solve.

    @app.get("/api/places/search")
    def api_place_search(
        q: str = Query(min_length=1, max_length=200),
    ) -> dict[str, Any]:
        """Free-text place search. `enabled` is false, with no results, when no
        provider key is configured — the UI says so instead of showing empty."""
        return {"enabled": maps.maps_enabled(), "places": maps.search_places(q)}

    @app.get("/api/routes/road")
    def api_road_route(
        from_lat: float = Query(ge=-90, le=90, allow_inf_nan=False),
        from_lon: float = Query(ge=-180, le=180, allow_inf_nan=False),
        to_lat: float = Query(ge=-90, le=90, allow_inf_nan=False),
        to_lon: float = Query(ge=-180, le=180, allow_inf_nan=False),
    ) -> dict[str, Any]:
        """Driving distance, time and road geometry between two points. Falls
        back to a great-circle estimate, flagged `estimated`, with no key."""
        return maps.road_route(from_lat, from_lon, to_lat, to_lon)

    # -- commands --------------------------------------------------------------

    @app.post("/api/commands/allocate")
    @locked
    def api_allocate(command: AllocateCommand) -> dict[str, Any]:
        state = world.state()
        try:
            record = allocate(state, command.shipment_id, world.config)
            committed = 0
            if command.commit:
                if record.chosen is None:
                    raise HTTPException(status_code=409, detail="no feasible destination")
                # Booking one shipment by hand stales any drafted batch; the
                # discard that terminates it rides in the same append (§7.5).
                committed = len(
                    commit(world.store, record, actor="api", pending_plan=state.pending_plan)
                )
            return {"record": record.model_dump(mode="json"), "committed_events": committed}
        except AllocateError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.post("/api/commands/optimize")
    @locked
    def api_optimize(command: OptimizeCommand) -> dict[str, Any]:
        from nodal.allocate.batch import (
            PlanMismatch,
            commit_reviewed,
            draft_batch,
            solve_batch,
        )
        from nodal.allocate.rebalance import generate_rebalancing_transfers

        state = world.state()
        now = state.last_ts
        if now is None:
            raise HTTPException(status_code=409, detail="empty world")
        if command.rebalance and not command.commit:
            raise HTTPException(
                status_code=422,
                detail="rebalance requires commit (preview via `nodal optimize --rebalance`)",
            )
        head = world.store.last_seq()
        pending = state.pending_plan
        # What the caller must be holding to be allowed to act. With a plan
        # pending that is the seq the PLAN read and nothing else: the draft's own
        # event is not a move, but the CURRENT head is not an acceptable answer
        # either — after a second draft it names the first draft's world, and
        # accepting it would commit the wrong plan. With none pending, the head.
        reviewed_head = head if pending is None else pending.based_on_seq
        if command.expected_head is not None and command.expected_head != reviewed_head:
            raise HTTPException(
                status_code=409,
                detail=f"the log moved since the plan was made (head {head}, "
                f"plan saw {command.expected_head}); review a fresh plan",
            )
        # `expected_head` cannot tell two successive drafts apart — the second is
        # based on the seq that sat at the head while the first was pending — so
        # a caller that names its batch gets an exact answer.
        if command.batch_id is not None and (
            pending is None or command.batch_id != pending.batch_id
        ):
            raise HTTPException(
                status_code=409,
                detail=f"plan {command.batch_id} is not the pending plan"
                + (f" ({pending.batch_id})" if pending is not None else "; nothing is pending"),
            )
        # Rebalancing registers transfers, which would supersede the pending
        # plan and then book a batch nobody reviewed under a different id — so
        # with a plan pending it is refused; discard (or commit) the plan first.
        if command.rebalance and pending is not None:
            raise HTTPException(
                status_code=409,
                detail=f"plan {pending.batch_id} is pending; discard or commit it before "
                "rebalancing (the transfers would supersede it)",
            )
        transfers_registered = 0
        try:
            if command.rebalance:
                prefix = f"TRF-{world.store.last_seq()}"
                transfers = generate_rebalancing_transfers(
                    state, world.config, now, id_prefix=prefix
                )
                if transfers:
                    _append(
                        [
                            EventDraft(ts=now, payload=ev.TransferOrdered(shipment=t))
                            for t in transfers
                        ]
                    )
                    state = world.state()
                    transfers_registered = len(transfers)
            planned = sorted(
                sid for sid, s in state.shipments.items() if s.status.value == "planned"
            )
            if not planned:
                return {
                    "batch": None,
                    "batch_id": None,
                    "head": world.store.last_seq(),
                    "transfers_registered": transfers_registered,
                }
            committed = 0
            if command.commit:
                # The engine owns the review guard, so the CLI cannot book past
                # a draft the API would refuse: a pending plan keeps ITS batch id
                # (the audit chain reads PlanDrafted -> BatchSolved on one batch)
                # and a re-solve that differs is refused outright.
                result, envelopes = commit_reviewed(
                    world.store,
                    state,
                    planned,
                    world.config,
                    now,
                    batch_id=f"BATCH-API-{world.store.last_seq()}",
                    actor="api",
                )
                committed = len(envelopes)
            else:
                # A proposal is a fact about the world, not a browser artifact:
                # it goes in the log, where a reload (and replay) can find it.
                # A fresh id per invocation: BatchSolved's entity identity is the
                # batch id, and the audit trail must never alias two solves.
                result = solve_batch(
                    state,
                    planned,
                    world.config,
                    now,
                    batch_id=f"BATCH-API-{world.store.last_seq()}",
                )
                draft_batch(world.store, result, actor="api")
            return {
                "batch": {
                    "batch_id": result.batch_id,
                    # The head this solve was made against: a dry run hands it
                    # back as `expected_head` when it commits.
                    "head": head,
                    "meta": result.meta.model_dump(mode="json"),
                    "assignments": {
                        sid: list(pair) if pair else None
                        for sid, pair in result.assignments.items()
                    },
                    # The full per-shipment records: a dry run is a reviewable
                    # plan only if the UI can show each decision's arithmetic.
                    "records": {
                        sid: record.model_dump(mode="json")
                        for sid, record in result.records.items()
                    },
                },
                "batch_id": result.batch_id,
                # The head AFTER this command: a drafted plan moved it by one.
                "head": world.store.last_seq(),
                "transfers_registered": transfers_registered,
                "committed_events": committed,
            }
        except PlanMismatch as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        except AllocateError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.post("/api/commands/plan/discard")
    @locked
    def api_discard_plan(command: DiscardPlanCommand) -> dict[str, Any]:
        """Throw away the drafted plan at the head (§7.5). Nothing is unbooked —
        a draft books nothing — but the review that ended in NO is history too,
        and appending the discard is what un-pends the plan."""
        state = world.state()
        now = state.last_ts
        pending = state.pending_plan
        if now is None or pending is None or pending.batch_id != command.batch_id:
            raise HTTPException(
                status_code=409,
                detail=f"plan {command.batch_id} is not the pending plan; nothing to discard",
            )
        envelopes = _append(
            [
                EventDraft(
                    ts=now,
                    payload=ev.PlanDiscarded(batch_id=pending.batch_id, reason=command.reason),
                )
            ]
        )
        return {
            "batch_id": pending.batch_id,
            "appended": len(envelopes),
            "head": world.store.last_seq(),
        }

    @app.post("/api/commands/whatif")
    @locked
    def api_whatif(command: WhatIfCommand) -> dict[str, Any]:
        try:
            report = run_whatif(
                world.store, world.config, command.policy, command.events, at=_aware(command.at)
            )
        except WhatIfError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return {
            "at": report.at.isoformat(),
            "hypotheticals": report.hypotheticals,
            "policy": report.policy,
            "baseline": {
                "allocated": report.baseline.allocated,
                "unassigned": report.baseline.unassigned,
                "cost_cents": report.baseline.total_cost_cents,
                "assignments": {
                    sid: list(pair) if pair else None
                    for sid, pair in report.baseline.assignments.items()
                },
            },
            "fork": {
                "allocated": report.fork.allocated,
                "unassigned": report.fork.unassigned,
                "cost_cents": report.fork.total_cost_cents,
                "assignments": {
                    sid: list(pair) if pair else None
                    for sid, pair in report.fork.assignments.items()
                },
            },
            "changes": [
                {
                    "shipment_id": change.shipment_id,
                    "before": list(change.before) if change.before else None,
                    "after": list(change.after) if change.after else None,
                    "cost_delta_cents": change.cost_delta_cents,
                }
                for change in report.changes
            ],
            "reopts": [
                {
                    "trigger": r.trigger,
                    "tier": r.tier,
                    "affected": r.affected,
                    "moved": r.changed,
                    # What the hypothetical closure would strand: nothing
                    # re-routes it, so a preview has to name it too.
                    "trapped": _trapped_rows(r.trapped),
                }
                for r in report.reopts
            ],
            "rendered": report.render(),
        }

    _DISRUPT_TARGETS = {
        DisruptionKind.FACILITY_CLOSED: "facilities",
        DisruptionKind.EQUIPMENT_DOWN: "facilities",
        DisruptionKind.ZONE_OFFLINE: "zones",
        DisruptionKind.CAPACITY_REDUCED: "zones",
        DisruptionKind.LANE_BLOCKED: "lanes",
        DisruptionKind.SHIPMENT_DELAYED: "shipments",
    }

    @app.post("/api/commands/disrupt")
    @locked
    def api_disrupt(command: DisruptCommand) -> dict[str, Any]:
        state = world.state()
        now = state.last_ts
        if now is None:
            raise HTTPException(status_code=409, detail="empty world")
        # Validate BEFORE writing: an append-only log cannot take a disruption
        # against an entity that does not exist.
        registry = getattr(state, _DISRUPT_TARGETS[command.kind])
        if command.target_id not in registry:
            raise HTTPException(
                status_code=404,
                detail=f"unknown {_DISRUPT_TARGETS[command.kind][:-1]} {command.target_id!r}",
            )
        disruption_id = f"DIS-API-{world.store.last_seq()}"
        disruption = Disruption(
            id=disruption_id,
            kind=command.kind,
            target_id=command.target_id,
            detail=command.detail,
            from_ts=now,
            until_ts=now + timedelta(days=command.days),
            magnitude=command.magnitude,
        )
        envelopes = _append(
            [EventDraft(ts=now, payload=ev.DisruptionStarted(disruption=disruption))]
        )
        state = world.state()  # refolds past the new head
        try:
            result = reoptimize(
                world.store, state, disruption_id, world.config, now, batch_prefix="API-REOPT"
            )
        except AllocateError as err:
            # The disruption is real and already in the log; say so
            # instead of a 500 that hides the write. The operator can retry the
            # re-solve (optimize) once conditions change.
            return {
                "disruption_id": disruption_id,
                "appended": len(envelopes),
                "reopt": None,
                "reopt_error": str(err),
            }
        return {
            "disruption_id": disruption_id,
            "appended": len(envelopes) + len(result.envelopes),
            "reopt": _reopt_body(result),
        }

    @app.post("/api/commands/disruptions/end")
    @locked
    def api_end_disruption(command: EndDisruptionCommand) -> dict[str, Any]:
        """End a disruption now, optionally re-opening it until a new end time.

        Ending restores capacity; bookings that were moved away stay where they
        are (nothing forces them back). A new window is a new disruption, so
        its re-optimization runs exactly like a fresh one.
        """
        state = world.state()
        now = state.last_ts
        if now is None:
            raise HTTPException(status_code=409, detail="empty world")
        current = state.disruptions.get(command.disruption_id)
        if current is None:
            raise HTTPException(
                status_code=404, detail=f"unknown disruption {command.disruption_id!r}"
            )
        if current.ended_at is not None or current.until_ts <= now:
            raise HTTPException(
                status_code=409, detail=f"disruption {command.disruption_id} is already over"
            )
        until = command.until
        if until is not None:
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            if until <= now:
                raise HTTPException(
                    status_code=422,
                    detail="the new end must be after now; use no `until` to end now",
                )
        drafts = [EventDraft(ts=now, payload=ev.DisruptionEnded(disruption_id=current.id))]
        replacement: Disruption | None = None
        if until is not None:
            replacement = Disruption(
                id=f"{current.id}-R{world.store.last_seq()}",
                kind=current.kind,
                target_id=current.target_id,
                detail=current.detail,
                from_ts=now,
                until_ts=until,
                magnitude=current.magnitude,
            )
            drafts.append(EventDraft(ts=now, payload=ev.DisruptionStarted(disruption=replacement)))
        envelopes = _append(drafts)
        body: dict[str, Any] = {
            "ended": current.id,
            "disruption_id": replacement.id if replacement else None,
            "appended": len(envelopes),
            "reopt": None,
        }
        if replacement is not None:
            state = world.state()
            try:
                result = reoptimize(
                    world.store, state, replacement.id, world.config, now, batch_prefix="API-REOPT"
                )
            except AllocateError as err:
                body["reopt_error"] = str(err)
                return body
            body["appended"] += len(result.envelopes)
            body["reopt"] = _reopt_body(result)
        return body

    @app.post("/api/commands/rebalance")
    @locked
    def api_rebalance() -> dict[str, Any]:
        """Order §7.7 rebalancing transfers and book THEM — the planned queue
        is untouched (the optimize command's `rebalance` flag, which folds the
        transfers into a full batch commit, stays for CLI parity)."""
        from nodal.allocate.batch import commit_batch, solve_batch
        from nodal.allocate.rebalance import generate_rebalancing_transfers

        state = world.state()
        now = state.last_ts
        if now is None:
            raise HTTPException(status_code=409, detail="empty world")
        prefix = f"TRF-{world.store.last_seq()}"
        try:
            transfers = generate_rebalancing_transfers(state, world.config, now, id_prefix=prefix)
            if not transfers:
                return {"transfers_registered": 0, "batch": None, "committed_events": 0}
            drafts = [EventDraft(ts=now, payload=ev.TransferOrdered(shipment=t)) for t in transfers]
            _append(drafts)
            state = world.state()
            result = solve_batch(
                state,
                sorted(t.id for t in transfers),
                world.config,
                now,
                batch_id=f"BATCH-API-{world.store.last_seq()}",
            )
            committed = len(commit_batch(world.store, result, actor="api"))
            return {
                "transfers_registered": len(transfers),
                "batch": {
                    "meta": result.meta.model_dump(mode="json"),
                    "assignments": {
                        sid: list(pair) if pair else None
                        for sid, pair in result.assignments.items()
                    },
                },
                "committed_events": committed,
            }
        except AllocateError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    def _build_zone(spec: NewZone, facility_id: str, zone_id: str) -> StorageZone:
        if spec.temp_c is not None and spec.temp_c[0] > spec.temp_c[1]:
            raise HTTPException(
                status_code=422,
                detail=f"temperature band {list(spec.temp_c)} is inverted (low > high)",
            )
        return StorageZone(
            id=zone_id,
            facility_id=facility_id,
            kind=spec.kind,
            capacity=CapacityVector(
                slots=spec.slots,
                weight_g=kg(spec.weight_kg) if spec.weight_kg is not None else None,
                volume_l=m3(spec.volume_m3) if spec.volume_m3 is not None else None,
            ),
            temp_c=spec.temp_c,
            allowed_classes=list(spec.allowed_classes),
        )

    @app.post("/api/commands/facilities")
    @locked
    def api_register_facility(command: RegisterFacilityCommand) -> dict[str, Any]:
        """Register a facility (and its zones) into the network. There is no
        removal — a facility that should stop taking work gets closed with a
        disruption; the log never forgets it existed."""
        state = world.state()
        now = state.last_ts
        if now is None:
            raise HTTPException(status_code=409, detail="empty world")
        facility_id = command.facility_id or f"FAC-API-{world.store.last_seq()}"
        if facility_id in state.facilities:
            raise HTTPException(status_code=409, detail=f"facility {facility_id} already exists")
        certifications = []
        until = now + timedelta(days=365 * command.cert_years)
        if command.cold_certified:
            certifications.append(
                Certification(tag="cert:coldchain", valid_from=now, valid_until=until)
            )
        if command.hazmat_certified:
            certifications.append(
                Certification(tag="cert:hazmat", valid_from=now, valid_until=until)
            )
        facility = Facility(
            id=facility_id,
            name=command.name or facility_id,
            lat=command.lat,
            lon=command.lon,
            tags=list(command.tags),
            certifications=certifications,
            risk_factor=command.risk,
        )
        zone_ids: list[str] = []
        zones: list[StorageZone] = []
        for index, spec in enumerate(command.zones, start=1):
            zone_id = spec.zone_id or f"{facility_id}-Z{index}"
            if zone_id in state.zones or zone_id in zone_ids:
                raise HTTPException(status_code=409, detail=f"zone {zone_id} already exists")
            zones.append(_build_zone(spec, facility_id, zone_id))
            zone_ids.append(zone_id)
        drafts = [EventDraft(ts=now, payload=ev.FacilityRegistered(facility=facility))]
        drafts.extend(EventDraft(ts=now, payload=ev.ZoneRegistered(zone=z)) for z in zones)
        envelopes = _append(drafts)
        return {"facility_id": facility_id, "zone_ids": zone_ids, "appended": len(envelopes)}

    @app.post("/api/commands/zones")
    @locked
    def api_add_zone(command: AddZoneCommand) -> dict[str, Any]:
        """Add a storage zone to an existing facility."""
        state = world.state()
        now = state.last_ts
        if now is None:
            raise HTTPException(status_code=409, detail="empty world")
        if command.facility_id not in state.facilities:
            raise HTTPException(status_code=404, detail=f"unknown facility {command.facility_id!r}")
        zone_id = command.zone_id or f"{command.facility_id}-Z{world.store.last_seq()}"
        if zone_id in state.zones:
            raise HTTPException(status_code=409, detail=f"zone {zone_id} already exists")
        zone = _build_zone(command, command.facility_id, zone_id)
        envelopes = _append([EventDraft(ts=now, payload=ev.ZoneRegistered(zone=zone))])
        return {"zone_id": zone_id, "appended": len(envelopes)}

    @app.post("/api/commands/zones/capacity")
    @locked
    def api_set_zone_capacity(command: SetZoneCapacityCommand) -> dict[str, Any]:
        """Permanently set a zone's base capacity (CapacityAdjusted) —
        distinct from a capacity_reduced disruption, which expires. Omitted
        dimensions keep their current base value: in a CapacityVector, None
        means unbounded, so a slots-only resize must never silently uncap a
        zone's weight or volume. The deliberate flip side: this API cannot
        set a dimension back to unbounded at all — only world files (and the
        fold, whose semantics are unchanged) write None."""
        state = world.state()
        now = state.last_ts
        if now is None:
            raise HTTPException(status_code=409, detail="empty world")
        if command.zone_id not in state.zones:
            raise HTTPException(status_code=404, detail=f"unknown zone {command.zone_id!r}")
        if command.slots is None and command.weight_kg is None and command.volume_m3 is None:
            raise HTTPException(
                status_code=422,
                detail="at least one capacity dimension is required "
                "(omitted dimensions keep their current value)",
            )
        current = state.zones[command.zone_id].capacity
        capacity = CapacityVector(
            slots=command.slots if command.slots is not None else current.slots,
            weight_g=kg(command.weight_kg) if command.weight_kg is not None else current.weight_g,
            volume_l=m3(command.volume_m3) if command.volume_m3 is not None else current.volume_l,
        )
        envelopes = _append(
            [
                EventDraft(
                    ts=now,
                    payload=ev.CapacityAdjusted(zone_id=command.zone_id, capacity=capacity),
                )
            ]
        )
        return {
            "zone_id": command.zone_id,
            "capacity": {d: capacity.get(d) for d in ("slots", "weight_g", "volume_l")},
            "appended": len(envelopes),
        }

    @app.post("/api/commands/lanes")
    @locked
    def api_register_lane(command: RegisterLaneCommand) -> dict[str, Any]:
        """Register a lane (both directions by default). Distance and time
        default to great-circle km and the mode's speed + handling."""
        state = world.state()
        now = state.last_ts
        if now is None:
            raise HTTPException(status_code=409, detail="empty world")
        if command.mode not in _LANE_SPEED_KMH:
            raise HTTPException(
                status_code=422,
                detail=f"unknown mode {command.mode!r}; use one of {sorted(_LANE_SPEED_KMH)}",
            )
        if command.from_facility_id == command.to_facility_id:
            raise HTTPException(status_code=422, detail="a lane needs two distinct facilities")
        for facility_id in (command.from_facility_id, command.to_facility_id):
            if facility_id not in state.facilities:
                raise HTTPException(status_code=404, detail=f"unknown facility {facility_id!r}")
        origin = state.facilities[command.from_facility_id]
        target = state.facilities[command.to_facility_id]
        km_value = command.km or round(
            haversine_km(origin.lat, origin.lon, target.lat, target.lon), 1
        )
        minutes = command.minutes or (
            round(km_value / _LANE_SPEED_KMH[command.mode] * 60) + _LANE_HANDLING_MIN[command.mode]
        )
        cost_fixed = (
            command.cost_fixed if command.cost_fixed is not None else _LANE_COST_FIXED[command.mode]
        )
        pairs = [(command.from_facility_id, command.to_facility_id)]
        if command.both_directions:
            pairs.append((command.to_facility_id, command.from_facility_id))
        # A direction that already exists is skipped, so a one-way lane can be
        # completed into a pair; only a fully-duplicate request is refused.
        drafts: list[EventDraft] = []
        lane_ids: list[str] = []
        existing: list[str] = []
        for src, dst in pairs:
            lane_id = f"L-{command.mode.upper()}-{src}-{dst}"
            if lane_id in state.lanes:
                existing.append(lane_id)
                continue
            lane_ids.append(lane_id)
            drafts.append(
                EventDraft(
                    ts=now,
                    payload=ev.LaneRegistered(
                        lane=Lane(
                            id=lane_id,
                            from_facility_id=src,
                            to_facility_id=dst,
                            mode=command.mode,
                            distance_km=km_value,
                            minutes=minutes,
                            cost_fixed_cents=currency(cost_fixed),
                            cost_per_kg_cents=command.cost_per_kg_cents,
                        )
                    ),
                )
            )
        if not drafts:
            raise HTTPException(
                status_code=409,
                detail=f"already registered: {', '.join(existing)}",
            )
        envelopes = _append(drafts)
        return {
            "lane_ids": lane_ids,
            "km": km_value,
            "minutes": minutes,
            "appended": len(envelopes),
        }

    @app.post("/api/commands/shipments")
    @locked
    def api_register_shipment(command: RegisterShipmentCommand) -> dict[str, Any]:
        """Register a new shipment into the log; it lands in the queue as
        PLANNED, ready for SOLVE or the next batch."""
        state = world.state()
        now = state.last_ts
        if now is None:
            raise HTTPException(status_code=409, detail="empty world")
        if command.origin_facility_id is not None:
            if command.origin_facility_id not in state.facilities:
                raise HTTPException(
                    status_code=404, detail=f"unknown facility {command.origin_facility_id!r}"
                )
        elif command.origin_lat is None or command.origin_lon is None:
            raise HTTPException(
                status_code=422,
                detail="an origin is required: origin_facility_id, or origin_lat + origin_lon",
            )
        # A destination is all three fields or none: a half-specified customer
        # point in the append-only log could never be routed to (§7.9).
        label, dest_lat, dest_lon = (
            command.destination_label,
            command.destination_lat,
            command.destination_lon,
        )
        supplied = [field for field in (label, dest_lat, dest_lon) if field is not None]
        if supplied and len(supplied) < 3:
            raise HTTPException(
                status_code=422,
                detail="a destination needs all three of destination_label, destination_lat "
                "and destination_lon (or none of them, for an ordinary allocation)",
            )
        destination = (
            Destination(label=label, lat=dest_lat, lon=dest_lon)
            if label is not None and dest_lat is not None and dest_lon is not None
            else None
        )
        # For a delivery the hold is the dwell; a dwell_days the engine would
        # silently ignore must be refused, not recorded.
        if destination is not None and command.dwell_days is not None:
            raise HTTPException(
                status_code=422,
                detail="a delivery's storage time is hold_days; dwell_days does not apply "
                "when a destination is set",
            )
        shipment_id = command.shipment_id or f"SHP-API-{world.store.last_seq()}"
        if shipment_id in state.shipments:
            raise HTTPException(status_code=409, detail=f"shipment {shipment_id} already exists")
        ready = _aware(command.ready) or now
        deadline = _aware(command.deadline)
        if deadline is not None and deadline <= max(ready, now):
            raise HTTPException(status_code=422, detail="the deadline must be after readiness")
        if command.temp_c is not None and command.temp_c[0] > command.temp_c[1]:
            raise HTTPException(
                status_code=422,
                detail=f"temperature band {list(command.temp_c)} is inverted (low > high)",
            )
        size = CapacityVector(
            slots=command.slots,
            weight_g=kg(command.weight_kg) if command.weight_kg is not None else None,
        )
        shipment = Shipment(
            id=shipment_id,
            origin_facility_id=command.origin_facility_id,
            origin_label=command.origin_label,
            origin_lat=command.origin_lat,
            origin_lon=command.origin_lon,
            lines=[
                LotSpec(
                    sku=command.sku,
                    commodity_group=command.group,
                    quantity=command.quantity,
                    uom=command.uom,
                    size=size,
                    compat_class=command.compat_class,
                )
            ],
            requirements=RequirementSet(
                size=size,
                required_tags=list(command.required_tags),
                zone_kinds=(
                    command.zone_kinds
                    if command.zone_kinds is not None
                    else ["cold", "rack"]
                    if command.temp_c
                    else ["rack"]
                ),
                temp_c=command.temp_c,
                compat_class=command.compat_class,
                deadline=deadline,
                dwell_days=command.dwell_days,
            ),
            ready_at=ready,
            destination=destination,
            hold_days=command.hold_days,
        )
        envelopes = _append([EventDraft(ts=now, payload=ev.ShipmentRegistered(shipment=shipment))])
        return {
            "shipment_id": shipment_id,
            # Whether this registered an A->B delivery (hold + itinerary) or an
            # ordinary allocation — the caller asked implicitly, via the fields.
            "delivery": destination is not None,
            "appended": len(envelopes),
        }

    @app.post("/api/commands/shipments/cancel")
    @locked
    def api_cancel_shipment(command: CancelShipmentCommand) -> dict[str, Any]:
        """Cancel a shipment. A booked one releases its reservations; the
        cancellation is an event like any other — nothing is deleted."""
        state = world.state()
        now = state.last_ts
        if now is None:
            raise HTTPException(status_code=409, detail="empty world")
        shipment = state.shipments.get(command.shipment_id)
        if shipment is None:
            raise HTTPException(status_code=404, detail=f"unknown shipment {command.shipment_id!r}")
        if shipment.status not in (
            ShipmentStatus.PLANNED,
            ShipmentStatus.ALLOCATED,
            ShipmentStatus.IN_TRANSIT,
        ):
            raise HTTPException(
                status_code=409,
                detail=f"cannot cancel a shipment that is {shipment.status.value}",
            )
        envelopes = _append(
            [
                EventDraft(
                    ts=now,
                    payload=ev.ShipmentCancelled(shipment_id=shipment.id, reason=command.reason),
                )
            ]
        )
        return {"shipment_id": shipment.id, "appended": len(envelopes)}

    @app.post("/api/commands/shipments/ready")
    @locked
    def api_set_ready(command: SetReadyCommand) -> dict[str, Any]:
        """Move a shipment's readiness (delay or advance). Exactly the §7.6
        path what-if uses: the readiness change is real, and a DELAY of a
        booked shipment is re-optimized under a SHIPMENT_DELAYED disruption."""
        state = world.state()
        now = state.last_ts
        if now is None:
            raise HTTPException(status_code=409, detail="empty world")
        shipment = state.shipments.get(command.shipment_id)
        if shipment is None:
            raise HTTPException(status_code=404, detail=f"unknown shipment {command.shipment_id!r}")
        if shipment.status not in (ShipmentStatus.PLANNED, ShipmentStatus.ALLOCATED):
            raise HTTPException(
                status_code=409,
                detail=f"cannot change readiness of a shipment that is {shipment.status.value}",
            )
        new_ready = command.new_ready
        if new_ready.tzinfo is None:
            new_ready = new_ready.replace(tzinfo=UTC)
        # A re-optimizable delay pushes readiness into the future: a change in
        # the past never mints a disruption whose window would be inverted.
        is_delay = new_ready > shipment.ready_at and new_ready > now
        drafts: list[EventDraft] = [
            EventDraft(
                ts=now,
                payload=ev.ShipmentReadyChanged(
                    shipment_id=shipment.id, new_ready=new_ready, reason=command.reason
                ),
            )
        ]
        disruption: Disruption | None = None
        if is_delay and shipment.status is ShipmentStatus.ALLOCATED:
            disruption = Disruption(
                id=f"DIS-API-{world.store.last_seq()}",
                kind=DisruptionKind.SHIPMENT_DELAYED,
                target_id=shipment.id,
                detail=command.reason or None,
                from_ts=now,
                until_ts=new_ready,
                magnitude=0.0,
            )
            drafts.append(EventDraft(ts=now, payload=ev.DisruptionStarted(disruption=disruption)))
        envelopes = _append(drafts)
        body: dict[str, Any] = {
            "shipment_id": shipment.id,
            "new_ready": new_ready.isoformat(),
            "appended": len(envelopes),
            "reopt": None,
        }
        if disruption is not None:
            state = world.state()
            try:
                result = reoptimize(
                    world.store, state, disruption.id, world.config, now, batch_prefix="API-REOPT"
                )
            except AllocateError as err:
                body["reopt_error"] = str(err)
                return body
            body["appended"] += len(result.envelopes)
            body["reopt"] = _reopt_body(result)
        return body

    return app


def main() -> None:
    import argparse
    import os

    import uvicorn

    parser = argparse.ArgumentParser(description="Nodal API server (§11)")
    parser.add_argument("--db", required=True, help="World database (event log)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--profile", default=None, help="Objective profile YAML (weights, packs, budgets)"
    )
    parser.add_argument(
        "--packs",
        default=None,
        help="Comma-separated rule packs (overrides the profile's list), e.g. core,coldchain,chem",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="API bearer token (default: $NODAL_API_TOKEN, else generated and printed)",
    )
    parser.add_argument(
        "--no-auth", action="store_true", help="Disable authentication (trusted localhost only)"
    )
    args = parser.parse_args()
    config = ObjectiveConfig.from_yaml(args.profile) if args.profile else ObjectiveConfig()
    if args.packs:
        config = config.model_copy(update={"packs": [p.strip() for p in args.packs.split(",")]})
    from nodal.rules.framework import PackError, load_packs

    try:
        load_packs(config.packs)  # fail at startup, not on the first solve
    except PackError as err:
        parser.error(str(err))
    print(f"profile: {config.name}  packs: {', '.join(config.packs)}")
    token: str | None
    if args.no_auth:
        token = None
        print("auth DISABLED (--no-auth)")
    else:
        token = args.token or os.environ.get("NODAL_API_TOKEN") or secrets.token_urlsafe(24)
        print(f"API token: {token}")
        print("paste it into the UI's connect screen (or send Authorization: Bearer <token>)")
    uvicorn.run(create_app(args.db, config, token=token), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
