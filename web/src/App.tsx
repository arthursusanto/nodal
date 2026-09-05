/* NODAL — fullscreen network map with operational overlays (§11).
   The map is the base layer, always; views swap the overlay panels around it.
   No CSS animations anywhere (OS reduced-motion parity by construction). */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, ApiError, AuthError, hasAuthToken, onAuthFailure, setAuthToken } from "./api";
import type {
  BatchPlan,
  DecisionRecord,
  MapState,
  NewFacility,
  NewLane,
  NewShipment,
  NewZoneInput,
  ReoptSummary,
} from "./api";
import { fmtDuration, fmtTime, fmtTrapped } from "./fmt";
import { applyPanelGeometry, panelGeometry } from "./layout";
import { DecisionPanel } from "./components/DecisionPanel";
import { FacilityPanel } from "./components/FacilityPanel";
import { ForecastPanel } from "./components/ForecastPanel";
import { Login } from "./components/Login";
import { MapView } from "./components/MapView";
import { NetworkPanel } from "./components/NetworkPanel";
import { NewShipmentPanel } from "./components/NewShipmentPanel";
import { QueuePanel } from "./components/QueuePanel";
import { ReplayBar } from "./components/ReplayBar";
import { SchedulesPanel } from "./components/SchedulesPanel";
import { Stepper } from "./components/Stepper";
import { WhatIfPanel } from "./components/WhatIfPanel";

/** The one centre panel that can be open at a time. */
export type Panel = "forecast" | "schedules" | "whatif" | "network";

const SCALE_KEY = "nodal-ui-scale";
const SCALES = [1, 1.15, 1.3, 1.5];

function loadScale(): number {
  try {
    const saved = Number(localStorage.getItem(SCALE_KEY));
    return SCALES.includes(saved) ? saved : 1;
  } catch {
    return 1;
  }
}

export interface Solve {
  record: DecisionRecord;
  committed: boolean;
  /** The record came from a batch plan (OPTIMIZE ALL dry run), not a single solve. */
  fromPlan: boolean;
}

const NOTICE_MS = 7000;

const assignedCount = (plan: BatchPlan): number =>
  Object.values(plan.assignments).filter((pair) => pair !== null).length;

/** What a command's re-optimization stranded, as a clause for its notice
    (§7.9) — nothing at all when it stranded nothing. Every command that can
    trap cargo says so: the queue badge is not the only place it is reported. */
const trappedClause = (reopt: ReoptSummary | null): string => {
  const text = reopt ? fmtTrapped(reopt.trapped) : "";
  return text ? ` · ${text}` : "";
};

export function App() {
  const [connected, setConnected] = useState(hasAuthToken);
  const [showQueue, setShowQueue] = useState(true);
  const [panel, setPanel] = useState<Panel | null>(null);
  const [replayOpen, setReplayOpen] = useState(false);
  const [uiScale, setUiScale] = useState(loadScale);
  const [world, setWorld] = useState<MapState | null>(null);
  const [liveNow, setLiveNow] = useState<string | null>(null);
  const [logStart, setLogStart] = useState<string | null>(null);
  const [replayAt, setReplayAt] = useState<string | null>(null);
  const [selectedShipment, setSelectedShipment] = useState<string | null>(null);
  const [solve, setSolve] = useState<Solve | null>(null);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [plan, setPlan] = useState<BatchPlan | null>(null);
  const [openFacility, setOpenFacility] = useState<string | null>(null);
  const [newShipmentOpen, setNewShipmentOpen] = useState(false);
  // Picking coordinates off the map for the network panel's facility form.
  const [pickingCoords, setPickingCoords] = useState(false);
  const [pickedCoords, setPickedCoords] = useState<{ lat: number; lon: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const eventsHead = useRef<number | null>(null);
  const mapToken = useRef(0);
  // Mirrors `plan` for code that runs between renders (the poll, command
  // continuations); commands that drop the plan clear BOTH synchronously.
  const planRef = useRef<BatchPlan | null>(null);
  planRef.current = plan;
  // Count of OUR log-moving commands in flight: while non-zero the poll
  // leaves plan bookkeeping to those commands instead of racing them.
  const commandsInFlight = useRef(0);

  const shipment = useMemo(
    () => world?.shipments.find((s) => s.id === selectedShipment) ?? null,
    [world, selectedShipment],
  );
  const shipmentStatus = shipment?.status;
  const shipmentStatusRef = useRef(shipmentStatus);
  shipmentStatusRef.current = shipmentStatus;

  // UI scale: every size in the stylesheet is rem, so the root font size IS
  // the scale; the panel widths follow it (capped on narrow viewports, see
  // layout.ts). Per-viewer convenience, remembered in localStorage.
  useEffect(() => {
    const apply = () => applyPanelGeometry(panelGeometry(uiScale, window.innerWidth));
    apply();
    window.addEventListener("resize", apply);
    try {
      localStorage.setItem(SCALE_KEY, String(uiScale));
    } catch {
      /* storage unavailable: the scale still applies for this page */
    }
    return () => window.removeEventListener("resize", apply);
  }, [uiScale]);
  const stepScale = (direction: 1 | -1) => {
    const index = SCALES.indexOf(uiScale);
    const next = SCALES[Math.min(SCALES.length - 1, Math.max(0, index + direction))];
    if (next !== undefined) setUiScale(next);
  };

  // Notices fade on a timer (plain JS, no CSS animation); errors stay until
  // dismissed.
  useEffect(() => {
    if (notice === null) return;
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [notice]);

  // Any 401 anywhere — commands, polls, panel-local fetches — drops back to
  // the connect screen via the api layer's hook, BEFORE the AuthError even
  // propagates to whichever catch is nearest.
  useEffect(() => {
    onAuthFailure(() => {
      setAuthToken(null);
      planRef.current = null;
      setPlan(null);
      setConnected(false);
    });
    return () => onAuthFailure(null);
  }, []);

  // Command/read failures surface as a toast; AuthErrors are swallowed here
  // because the onAuthFailure hook above has already handled them.
  const handleError = useCallback((err: unknown) => {
    if (err instanceof AuthError) return;
    setError(err instanceof Error ? err.message : String(err));
  }, []);

  // A fresh connect may be a different server or a rebuilt log: session-
  // scoped state must not leak through the connect screen (a stale plan
  // would turn the first OPTIMIZE ALL click into a commit).
  const handleConnected = useCallback(() => {
    planRef.current = null;
    setPlan(null);
    setDecisionError(null);
    setError(null);
    setNotice(null);
    setSolve(null);
    setSelectedShipment(null);
    setReplayAt(null);
    setLogStart(null);
    eventsHead.current = null;
    setConnected(true);
  }, []);

  // A single solve's record is transient; a booked shipment's record is the
  // log's. Plan changes may only wipe the former — wiping the latter left
  // the panel "loading" a record nothing would ever re-fetch.
  const clearTransientSolve = useCallback(() => {
    if (shipmentStatusRef.current === "planned") setSolve(null);
  }, []);

  // Drop the plan now — ref and state together — so nothing that runs before
  // the next render can still see it.
  const dropPlan = useCallback(() => {
    const had = planRef.current !== null;
    planRef.current = null;
    setPlan(null);
    if (had) clearTransientSolve();
    return had;
  }, [clearTransientSolve]);

  // After one of our commands moved the log, adopt the new head so the poll
  // never mistakes our own write for another operator's.
  const syncHead = useCallback(async () => {
    try {
      const probe = await api.events(0, 1);
      eventsHead.current = probe.head;
      setLogStart((current) => current ?? probe.events[0]?.ts ?? null);
    } catch {
      // The poll will catch up.
    }
  }, []);

  const refreshMap = useCallback(
    async (at: string | null) => {
      const token = ++mapToken.current;
      try {
        const state = await api.map(at ?? undefined);
        if (token !== mapToken.current) return; // a newer request superseded us
        setWorld(state);
        if (!at && state.now) setLiveNow(state.now);
        setError(null);
      } catch (err) {
        if (token === mapToken.current) handleError(err);
      }
    },
    [handleError],
  );

  useEffect(() => {
    if (!connected) return;
    void refreshMap(replayAt);
  }, [refreshMap, replayAt, connected]);

  // Poll the log head: a moved head refreshes the live map (committed
  // decisions, disruptions, other operators) and invalidates any dry-run
  // plan, which described a world that no longer exists. The first probe
  // also learns when the log starts — the replay range.
  useEffect(() => {
    if (!connected) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const probe = await api.events(0, 1);
        if (cancelled) return;
        if (eventsHead.current === null) {
          eventsHead.current = probe.head;
          setLogStart(probe.events[0]?.ts ?? null);
          return;
        }
        if (probe.head !== eventsHead.current) {
          eventsHead.current = probe.head;
          // A move while our own command is in flight is that command's
          // business (it discards or confirms the plan itself); anything
          // else is another operator, and the reviewed plan is stale.
          if (commandsInFlight.current === 0 && dropPlan()) {
            // Nothing of the operator's was thrown away: the review was
            // superseded by whatever moved the log (a newer draft, if one
            // exists, is adopted on this refresh).
            setNotice("plan superseded: the log moved underneath it");
          }
          if (replayAt === null) void refreshMap(null);
        }
      } catch {
        // Transient failures retry on the next tick; a 401 (server restart)
        // already dropped us to the connect screen via onAuthFailure.
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [refreshMap, replayAt, connected, dropPlan]);

  // A drafted plan is a fact about the world, not a browser artifact (§7.5):
  // it stands at the log head until it is committed or discarded. So whenever
  // the live map says a review is open and it is not the one we are already
  // holding, fetch the plan itself and enter review with it — on first load,
  // after a reload mid-review, and when another operator proposes one.
  // Adoption only: a plan that leaves the head moves it, and the poll below
  // drops the stale one. At the head and nowhere else: a draft and the commit
  // or discard that ends it are stamped with the SAME log timestamp (every
  // command takes the last event's ts), so no replayed stamp can fall between
  // them — a review is never open in history.
  const pendingBatchId = world?.pending_plan?.batch_id ?? null;
  // Which plan we are HOLDING, read from state rather than the ref: letting go
  // of one has to re-run this effect. A commit the server refuses drops the
  // review on screen while the plan is still pending, and the map's pending id
  // has not changed — so nothing else here would ever bring it back.
  const heldBatchId = plan?.batch_id ?? null;
  useEffect(() => {
    if (!connected || replayAt !== null || pendingBatchId === null) return;
    if (heldBatchId === pendingBatchId) return;
    let cancelled = false;
    api
      .plan()
      .then((body) => {
        if (cancelled || !body.batch || body.batch.batch_id !== pendingBatchId) return;
        planRef.current = body.batch;
        setPlan(body.batch);
        clearTransientSolve();
      })
      .catch(() => {
        // Transient: the next map refresh brings us back here.
      });
    return () => {
      cancelled = true;
    };
  }, [connected, pendingBatchId, heldBatchId, replayAt, clearTransientSolve]);

  const selectShipment = useCallback((shipmentId: string | null) => {
    setSelectedShipment(shipmentId);
    setSolve(null);
    setDecisionError(null);
  }, []);

  // A non-planned selection loads its RECORDED decision from the log — the
  // committed explanation, re-optimization records included (§7.5, §7.6).
  useEffect(() => {
    if (!selectedShipment || shipmentStatus === undefined || shipmentStatus === "planned") {
      return;
    }
    let cancelled = false;
    setDecisionError(null);
    api
      .decision(selectedShipment)
      .then((body) => {
        if (!cancelled) setSolve({ record: body.record, committed: true, fromPlan: false });
      })
      .catch((err: unknown) => {
        // e.g. a cancelled shipment with no decision recorded — say so
        // rather than sit on "loading" (401s disconnect via onAuthFailure
        // before this catch runs).
        if (cancelled || err instanceof AuthError) return;
        setSolve(null);
        setDecisionError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedShipment, shipmentStatus]);

  const runSolve = useCallback(async () => {
    if (!selectedShipment) return;
    setBusy(true);
    try {
      const result = await api.allocate(selectedShipment, false);
      setSolve({ record: result.record, committed: false, fromPlan: false });
      setError(null);
    } catch (err) {
      handleError(err);
    } finally {
      setBusy(false);
    }
  }, [selectedShipment, handleError]);

  const commitSolve = useCallback(async () => {
    if (!selectedShipment) return;
    setBusy(true);
    commandsInFlight.current += 1;
    try {
      const result = await api.allocate(selectedShipment, true);
      const dropped = dropPlan(); // the log moved: any plan is stale now
      setSolve({ record: result.record, committed: true, fromPlan: false });
      setNotice(
        `committed ${selectedShipment} -> ${result.record.chosen?.facility_id ?? "?"}` +
          (dropped ? " · plan discarded (log moved)" : ""),
      );
      await syncHead();
      await refreshMap(null);
    } catch (err) {
      handleError(err);
    } finally {
      commandsInFlight.current -= 1;
      setBusy(false);
    }
  }, [refreshMap, selectedShipment, handleError, dropPlan, syncHead]);

  // OPTIMIZE ALL is a dry run: the plan lands on the map and in the queue for
  // review; COMMIT PLAN books it; DISCARD drops it. Nothing is written until
  // the operator commits.
  const optimizeAll = useCallback(async () => {
    setBusy(true);
    commandsInFlight.current += 1;
    try {
      if (eventsHead.current === null) await syncHead(); // before the first poll lands
      const headBefore = eventsHead.current;
      const result = await api.optimize(false);
      if (!result.batch) {
        setNotice("nothing to optimize: no planned shipments");
        return;
      }
      // The dry run DRAFTS the proposal into the log (§7.5), so it moves the
      // head by one — our own write, which the poll must not read as another
      // operator's and throw the review away. Adopt the head the server wrote
      // the draft at BEFORE judging movement: the draft is on the log either
      // way, so telling the operator to "run OPTIMIZE ALL again" would be a
      // lie — the review below is that draft, and the map's pending plan would
      // hand it straight back.
      const observed = eventsHead.current;
      eventsHead.current = result.head;
      const moved = observed !== headBefore && observed !== result.head;
      const assigned = assignedCount(result.batch);
      const total = Object.keys(result.batch.assignments).length;
      planRef.current = result.batch;
      setPlan(result.batch);
      clearTransientSolve();
      setNotice(
        (assigned === 0
          ? `plan: nothing assignable — every planned shipment was rejected ` +
            `(${result.batch.meta.status ?? "?"}); select a row to see why`
          : `plan ready: ${assigned}/${total} assignable (${result.batch.meta.status ?? "?"}) — ` +
            "review it on the map, then COMMIT PLAN") +
          // Someone else wrote while the solve ran: the numbers were read
          // against an older world, so say so rather than pretend otherwise.
          (moved ? " · the log moved while planning — re-check before committing" : ""),
      );
    } catch (err) {
      handleError(err);
    } finally {
      commandsInFlight.current -= 1;
      setBusy(false);
    }
  }, [handleError, clearTransientSolve, syncHead]);

  const commitPlan = useCallback(async () => {
    const current = planRef.current;
    if (!current) return;
    setBusy(true);
    commandsInFlight.current += 1;
    try {
      // expected_head AND batch_id: the server refuses (409) to book anything
      // but the plan the operator reviewed — neither a plan drafted since, nor
      // one drafted against a different head.
      const result = await api.optimize(true, current.head, current.batch_id);
      dropPlan();
      setNotice(
        result.batch
          ? `plan committed: ${assignedCount(result.batch)} of ` +
              `${Object.keys(result.batch.assignments).length} shipments booked`
          : "nothing to commit: no planned shipments remain",
      );
      await syncHead();
      await refreshMap(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // The server refused this plan: it is not the one standing at the
        // head. Nothing was booked and nothing was discarded — drop what is on
        // screen and let the refresh below hand back whatever IS pending.
        dropPlan();
        setNotice(`plan not committed: ${err.message}`);
        await syncHead();
        await refreshMap(null);
      } else {
        // Any other failure leaves the reviewed plan in place.
        handleError(err);
      }
    } finally {
      commandsInFlight.current -= 1;
      setBusy(false);
    }
  }, [refreshMap, handleError, dropPlan, syncHead]);

  // Saying NO is history too (§7.5): the discard goes in the log, and being an
  // event at all is what un-pends the plan — for this browser and every other
  // one looking at the same head.
  const discardPlan = useCallback(async () => {
    const current = planRef.current;
    if (!current) return;
    setBusy(true);
    commandsInFlight.current += 1;
    try {
      const result = await api.discardPlan(current.batch_id);
      eventsHead.current = result.head;
      dropPlan();
      setNotice("plan discarded — nothing was booked");
      await refreshMap(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Someone got there first, or the log moved: it is off the head either
        // way, so there is nothing left to review.
        dropPlan();
        setNotice(`plan discarded: ${err.message}`);
        await syncHead();
        await refreshMap(null);
      } else {
        handleError(err);
      }
    } finally {
      commandsInFlight.current -= 1;
      setBusy(false);
    }
  }, [dropPlan, handleError, refreshMap, syncHead]);

  const disrupt = useCallback(
    async (kind: string, targetId: string, days: number, magnitude: number) => {
      setBusy(true);
      commandsInFlight.current += 1;
      try {
        const result = await api.disrupt(kind, targetId, days, magnitude);
        const reopt: ReoptSummary | null = result.reopt;
        const dropped = dropPlan(); // the log moved: any plan is stale now
        setNotice(
          (reopt === null
            ? `${result.disruption_id} is in the log, but re-optimization FAILED: ` +
              `${result.reopt_error ?? "unknown"} — retry via OPTIMIZE ALL`
            : reopt.tier === 0
              ? // Tier 0 with trapped cargo is not "nothing happened": the
                // closure stranded goods that nothing could re-plan.
                `${result.disruption_id}: ${
                  reopt.trapped.length > 0 ? "nothing could be re-planned" : "no bookings affected"
                }`
              : `${result.disruption_id}: tier ${reopt.tier} re-opt, ` +
                `${reopt.affected.length} affected, ${reopt.moved.length} changed` +
                (reopt.released.length > 0 ? `, ${reopt.released.length} released` : "")) +
            trappedClause(reopt) +
            (dropped ? " · plan discarded (log moved)" : ""),
        );
        setOpenFacility(null);
        await syncHead();
        await refreshMap(null);
      } catch (err) {
        handleError(err);
      } finally {
        commandsInFlight.current -= 1;
        setBusy(false);
      }
    },
    [refreshMap, handleError, dropPlan, syncHead],
  );

  // End a disruption now, or move its end (an end plus a fresh window).
  const endDisruption = useCallback(
    async (disruptionId: string, until: string | null) => {
      setBusy(true);
      commandsInFlight.current += 1;
      try {
        const result = await api.endDisruption(disruptionId, until);
        const dropped = dropPlan();
        const reopt = result.reopt;
        setNotice(
          (result.disruption_id
            ? `${disruptionId} now ends ${until ? fmtTime(until) : "?"} (as ${result.disruption_id})` +
              (reopt && reopt.tier > 0
                ? ` · tier ${reopt.tier} re-opt, ${reopt.moved.length} changed`
                : result.reopt_error
                  ? ` · re-optimization FAILED: ${result.reopt_error}`
                  : " · no bookings affected")
            : `${disruptionId} ended now`) +
            trappedClause(reopt) +
            (dropped ? " · plan discarded (log moved)" : ""),
        );
        await syncHead();
        await refreshMap(null);
      } catch (err) {
        handleError(err);
      } finally {
        commandsInFlight.current -= 1;
        setBusy(false);
      }
    },
    [refreshMap, handleError, dropPlan, syncHead],
  );

  // -- shipment lifecycle (GUI parity with the engine) ----------------------

  const createShipment = useCallback(
    async (body: NewShipment) => {
      setBusy(true);
      commandsInFlight.current += 1;
      try {
        const result = await api.registerShipment(body);
        const dropped = dropPlan();
        setNotice(
          `${result.shipment_id} registered — it joins the queue as PLANNED` +
            (dropped ? " · plan discarded (log moved)" : ""),
        );
        setNewShipmentOpen(false);
        await syncHead();
        await refreshMap(null);
        setSelectedShipment(result.shipment_id);
        setSolve(null);
        setDecisionError(null);
      } catch (err) {
        handleError(err);
      } finally {
        commandsInFlight.current -= 1;
        setBusy(false);
      }
    },
    [refreshMap, handleError, dropPlan, syncHead],
  );

  const cancelShipment = useCallback(
    async (shipmentId: string) => {
      setBusy(true);
      commandsInFlight.current += 1;
      try {
        await api.cancelShipment(shipmentId);
        const dropped = dropPlan();
        setNotice(
          `${shipmentId} cancelled — its reservations are released` +
            (dropped ? " · plan discarded (log moved)" : ""),
        );
        selectShipment(null);
        await syncHead();
        await refreshMap(null);
      } catch (err) {
        handleError(err);
      } finally {
        commandsInFlight.current -= 1;
        setBusy(false);
      }
    },
    [refreshMap, handleError, dropPlan, syncHead, selectShipment],
  );

  const setShipmentReady = useCallback(
    async (shipmentId: string, newReady: string) => {
      setBusy(true);
      commandsInFlight.current += 1;
      try {
        const result = await api.setReady(shipmentId, newReady);
        const dropped = dropPlan();
        const reopt = result.reopt;
        setNotice(
          `${shipmentId} now ready ${fmtTime(result.new_ready)}` +
            (reopt
              ? reopt.tier > 0
                ? ` · tier ${reopt.tier} re-opt, ${reopt.moved.length} changed` +
                  (reopt.released.length > 0 ? `, ${reopt.released.length} released` : "")
                : " · booking unaffected"
              : result.reopt_error
                ? ` · re-optimization FAILED: ${result.reopt_error}`
                : "") +
            trappedClause(reopt) +
            (dropped ? " · plan discarded (log moved)" : ""),
        );
        setSolve(null);
        await syncHead();
        await refreshMap(null);
      } catch (err) {
        handleError(err);
      } finally {
        commandsInFlight.current -= 1;
        setBusy(false);
      }
    },
    [refreshMap, handleError, dropPlan, syncHead],
  );

  // §7.7: order rebalancing transfers between facilities and book them.
  const rebalance = useCallback(async () => {
    setBusy(true);
    commandsInFlight.current += 1;
    try {
      const result = await api.rebalance();
      const dropped = dropPlan();
      setNotice(
        (result.transfers_registered > 0
          ? `${result.transfers_registered} rebalancing transfer${result.transfers_registered === 1 ? "" : "s"} ordered and booked`
          : "stock already balanced: no transfers worth making") +
          (dropped ? " · plan discarded (log moved)" : ""),
      );
      await syncHead();
      await refreshMap(null);
    } catch (err) {
      handleError(err);
    } finally {
      commandsInFlight.current -= 1;
      setBusy(false);
    }
  }, [refreshMap, handleError, dropPlan, syncHead]);

  // -- network operations ---------------------------------------------------

  const createFacility = useCallback(
    async (body: NewFacility) => {
      setBusy(true);
      commandsInFlight.current += 1;
      try {
        const result = await api.registerFacility(body);
        const dropped = dropPlan();
        setNotice(
          `${result.facility_id} registered with ${result.zone_ids.length} ` +
            `zone${result.zone_ids.length === 1 ? "" : "s"} — connect it with a lane` +
            (dropped ? " · plan discarded (log moved)" : ""),
        );
        await syncHead();
        await refreshMap(null);
      } catch (err) {
        handleError(err);
      } finally {
        commandsInFlight.current -= 1;
        setBusy(false);
      }
    },
    [refreshMap, handleError, dropPlan, syncHead],
  );

  const createLane = useCallback(
    async (body: NewLane) => {
      setBusy(true);
      commandsInFlight.current += 1;
      try {
        const result = await api.registerLane(body);
        const dropped = dropPlan();
        setNotice(
          `${result.lane_ids.join(" and ")} registered — ` +
            `${Math.round(result.km).toLocaleString("en-US")} km, ${fmtDuration(result.minutes)}` +
            (dropped ? " · plan discarded (log moved)" : ""),
        );
        await syncHead();
        await refreshMap(null);
      } catch (err) {
        handleError(err);
      } finally {
        commandsInFlight.current -= 1;
        setBusy(false);
      }
    },
    [refreshMap, handleError, dropPlan, syncHead],
  );

  const addZone = useCallback(
    async (facilityId: string, body: NewZoneInput) => {
      setBusy(true);
      commandsInFlight.current += 1;
      try {
        const result = await api.addZone({ ...body, facility_id: facilityId });
        const dropped = dropPlan();
        setNotice(
          `${result.zone_id} added to ${facilityId}` +
            (dropped ? " · plan discarded (log moved)" : ""),
        );
        await syncHead();
        await refreshMap(null);
      } catch (err) {
        handleError(err);
      } finally {
        commandsInFlight.current -= 1;
        setBusy(false);
      }
    },
    [refreshMap, handleError, dropPlan, syncHead],
  );

  const setZoneCapacity = useCallback(
    async (zoneId: string, slots: number) => {
      setBusy(true);
      commandsInFlight.current += 1;
      try {
        await api.setZoneCapacity(zoneId, slots);
        const dropped = dropPlan();
        setNotice(
          `${zoneId} base capacity is now ${slots} slots` +
            (dropped ? " · plan discarded (log moved)" : ""),
        );
        await syncHead();
        await refreshMap(null);
      } catch (err) {
        handleError(err);
      } finally {
        commandsInFlight.current -= 1;
        setBusy(false);
      }
    },
    [refreshMap, handleError, dropPlan, syncHead],
  );

  // Replay is a view of history; a plan is an artifact of the live head, so
  // scrubbing away from live drops it.
  const scrub = useCallback(
    (at: string | null) => {
      // A review is only ever open at the head, so looking away from the head
      // closes it. Nothing is discarded by looking away — the plan stands in
      // the log, and BACK TO LIVE adopts it again.
      if (dropPlan() && at !== null) {
        setNotice("left the plan at the live head — BACK TO LIVE returns to it");
      }
      setOpenFacility(null);
      setReplayAt(at);
    },
    [dropPlan],
  );

  const toggleReplay = useCallback(() => {
    setReplayOpen((open) => {
      if (open) scrub(null); // closing the scrubber returns to live
      return !open;
    });
  }, [scrub]);

  const togglePanel = useCallback((next: Panel) => {
    setPanel((current) => (current === next ? null : next));
    setOpenFacility(null);
    setPickingCoords(false);
    // A picked coordinate must not outlive its panel session: reopening the
    // network form later must not silently reuse an abandoned pick.
    setPickedCoords(null);
  }, []);

  // Hiding the queue hides the decision panel with it, so the selection (and
  // the map's focus on it) ends rather than lingering with no way out.
  const toggleQueue = useCallback(() => {
    setShowQueue((open) => {
      if (open) selectShipment(null);
      return !open;
    });
  }, [selectShipment]);

  // Escape closes the topmost thing: a standing error, a popover, the
  // centre panel, then the decision panel.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (pickingCoords) setPickingCoords(false);
      else if (error) setError(null);
      else if (newShipmentOpen) setNewShipmentOpen(false);
      else if (openFacility) setOpenFacility(null);
      else if (panel) {
        setPanel(null);
        setPickedCoords(null);
      } else if (selectedShipment) selectShipment(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pickingCoords, error, newShipmentOpen, openFacility, panel, selectedShipment, selectShipment]);

  // While a plan is up, a planned shipment's decision IS its plan record.
  const inPlan = plan !== null && shipment?.status === "planned" && shipment.id in plan.assignments;
  const activeSolve = useMemo<Solve | null>(() => {
    if (solve) return solve;
    if (inPlan && plan && selectedShipment) {
      const record = plan.records[selectedShipment];
      if (record) return { record, committed: false, fromPlan: true };
    }
    return null;
  }, [solve, plan, inPlan, selectedShipment]);

  const activeDisruptions = world?.disruptions.filter((d) => d.status === "active").length ?? 0;
  const planAssigned = plan ? assignedCount(plan) : 0;
  const planTotal = plan ? Object.keys(plan.assignments).length : 0;
  const interactive = !busy && replayAt === null;
  const queueOpen = showQueue && world !== null && !world.empty;
  const decisionOpen = queueOpen && shipment !== null;

  // A disruption row opens the facility it lands on; lane and shipment
  // disruptions land on no facility.
  const openDisruption = useCallback(
    (facilityId: string | null, disruptionId: string, kind: string) => {
      if (facilityId) setOpenFacility(facilityId);
      else {
        setNotice(
          `${disruptionId} targets a ${kind === "lane_blocked" ? "lane" : "shipment"}, ` +
            "not a facility — no facility view",
        );
      }
    },
    [],
  );

  if (!connected) {
    return <Login onConnected={handleConnected} />;
  }

  const toggles: { id: Panel; label: string }[] = [
    { id: "network", label: "NETWORK" },
    { id: "forecast", label: "FORECAST" },
    { id: "schedules", label: "SCHEDULES" },
    { id: "whatif", label: "WHAT-IF" },
  ];
  const stageClasses = ["stage"];
  if (queueOpen) stageClasses.push("queue-open");
  if (decisionOpen) stageClasses.push("decision-open");
  if (replayOpen) stageClasses.push("replay-open");

  return (
    <div className="app">
      <header className="app-header">
        <div className="logo-plate" aria-hidden="true" />
        <div className="wordmark">NODAL</div>
        {/* The views ride in the chrome, centred between the wordmark and the
            status cluster; they compact rather than wrapping the header. */}
        <nav className="header-views" aria-label="Views">
          <button
            className={showQueue ? "tile-btn active" : "tile-btn"}
            onClick={toggleQueue}
            title="Show or hide the shipment queue and decision panels"
          >
            QUEUE
          </button>
          {toggles.map((toggle) => (
            <button
              key={toggle.id}
              className={panel === toggle.id ? "tile-btn active" : "tile-btn"}
              onClick={() => togglePanel(toggle.id)}
            >
              {toggle.label}
            </button>
          ))}
          <button
            className={replayOpen ? "tile-btn active" : "tile-btn"}
            onClick={toggleReplay}
            title="Scrub the log's history; closing returns to live"
          >
            REPLAY
          </button>
        </nav>
        <div className="header-right mono">
          <span className="scale-control" title="Interface size">
            <button onClick={() => stepScale(-1)} disabled={uiScale === SCALES[0]} aria-label="Smaller">
              A−
            </button>
            <span>{Math.round(uiScale * 100)}%</span>
            <button
              onClick={() => stepScale(1)}
              disabled={uiScale === SCALES[SCALES.length - 1]}
              aria-label="Larger"
            >
              A+
            </button>
          </span>
          {plan && (
            <span className="chip plan-chip">
              PLAN {planAssigned}/{planTotal} · UNCOMMITTED
            </span>
          )}
          {replayAt ? (
            <button
              className="chip replay-chip"
              onClick={() => scrub(null)}
              title="Back to the live head"
            >
              REPLAY {fmtTime(replayAt)} · BACK TO LIVE
            </button>
          ) : (
            <span className="chip">{liveNow ? fmtTime(liveNow) : "—"} UTC · LIVE</span>
          )}
          <span className={activeDisruptions > 0 ? "chip alarm" : "chip"}>
            DISRUPTIONS {activeDisruptions}
          </span>
        </div>
      </header>

      {queueOpen && <Stepper shipment={shipment} solve={activeSolve} liveNow={liveNow} />}

      <div className={stageClasses.join(" ")}>
        <MapView
          world={world}
          plan={plan}
          selectedShipment={selectedShipment}
          record={activeSolve?.record ?? null}
          // Reserve both panel widths while the queue is up so the fit never
          // parks a hub under the decision panel when it opens.
          overlays={{ left: queueOpen, right: queueOpen, bottom: replayOpen }}
          rightPanelOpen={decisionOpen}
          uiScale={uiScale}
          picking={pickingCoords}
          onSelectShipment={selectShipment}
          onOpenFacility={setOpenFacility}
          onPickCoords={(coords) => {
            setPickedCoords(coords);
            setPickingCoords(false);
          }}
        />

        {queueOpen && world && (
          <>
            <QueuePanel
              shipments={world.shipments}
              facilities={world.facilities}
              selected={selectedShipment}
              onSelect={selectShipment}
              disruptions={world.disruptions}
              onOpenDisruption={openDisruption}
              busy={!interactive}
              plan={plan}
              live={replayAt === null}
              liveNow={liveNow}
              onOptimizeAll={() => void optimizeAll()}
              onCommitPlan={() => void commitPlan()}
              onDiscardPlan={() => void discardPlan()}
              onNewShipment={() => setNewShipmentOpen(true)}
              onRebalance={() => void rebalance()}
            />
            {shipment && (
              <DecisionPanel
                shipment={shipment}
                facilities={world.facilities}
                solve={activeSolve}
                loadError={decisionError}
                busy={!interactive}
                planMode={inPlan}
                planCount={planAssigned}
                liveNow={liveNow}
                live={replayAt === null}
                onSolve={() => void runSolve()}
                onCommit={() => void commitSolve()}
                onCommitPlan={() => void commitPlan()}
                onDiscardPlan={() => void discardPlan()}
                onCancelShipment={() => void cancelShipment(shipment.id)}
                onSetReady={(newReady) => void setShipmentReady(shipment.id, newReady)}
                onClose={() => selectShipment(null)}
              />
            )}
          </>
        )}

        {panel === "network" && world && !world.empty && (
          <NetworkPanel
            facilities={world.facilities}
            busy={!interactive}
            picking={pickingCoords}
            pickedCoords={pickedCoords}
            onPickCoords={setPickingCoords}
            onCreateFacility={(body) => void createFacility(body)}
            onCreateLane={(body) => void createLane(body)}
            onClose={() => togglePanel("network")}
          />
        )}
        {panel === "network" && pickingCoords && (
          <div className="pick-hint mono">click the map where the facility sits · Esc cancels</div>
        )}
        {panel === "forecast" && <ForecastPanel at={replayAt} onClose={() => setPanel(null)} />}
        {panel === "whatif" && (
          <WhatIfPanel
            facilities={world?.facilities ?? []}
            at={replayAt}
            onClose={() => setPanel(null)}
          />
        )}
        {panel === "schedules" && <SchedulesPanel at={replayAt} onClose={() => setPanel(null)} />}
        {replayOpen && (
          <ReplayBar liveNow={liveNow} logStart={logStart} replayAt={replayAt} onScrub={scrub} />
        )}

        {newShipmentOpen && world && !world.empty && (
          <NewShipmentPanel
            facilities={world.facilities}
            liveNow={liveNow}
            busy={busy}
            onCreate={(body) => void createShipment(body)}
            onClose={() => setNewShipmentOpen(false)}
          />
        )}

        {openFacility && world && (
          <FacilityPanel
            facilityId={openFacility}
            summary={world.facilities.find((f) => f.id === openFacility) ?? null}
            disruptions={world.disruptions.filter((d) => d.facility_id === openFacility)}
            at={replayAt}
            live={replayAt === null}
            liveNow={liveNow}
            busy={busy}
            onClose={() => setOpenFacility(null)}
            onDisrupt={disrupt}
            onEndDisruption={(id, until) => void endDisruption(id, until)}
            onAddZone={(body) => void addZone(openFacility, body)}
            onSetZoneCapacity={(zoneId, slots) => void setZoneCapacity(zoneId, slots)}
          />
        )}

        {(error || notice) && (
          <div className={error ? "toast error" : "toast"}>
            <span className="mono">{error ?? notice}</span>
            <button
              onClick={() => {
                setError(null);
                setNotice(null);
              }}
            >
              ×
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
