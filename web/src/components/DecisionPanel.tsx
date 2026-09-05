/* Decision explorer (right overlay) — the UI's reason to exist (§11): the
   §7.5 record rendered verbatim: where the shipment goes FROM and TO with real
   times, the winner's arithmetic, alternatives, rejections with the exact
   constraint data the engine recorded. */

import { Fragment, useEffect, useState } from "react";

import type {
  DecisionRecord,
  FacilitySummary,
  Itinerary,
  Reject,
  ScoredCandidate,
  ShipmentSummary,
  Stop,
  StopRole,
} from "../api";
import type { Solve } from "../App";
import { fmtDay, fmtDuration, fmtMoney, fmtRelative, fmtTemp, fmtTime } from "../fmt";
import { facilityLabel, fmtCargoLine, originLabel, trappedAt, uniformReject } from "./QueuePanel";

interface Props {
  shipment: ShipmentSummary | null;
  facilities: FacilitySummary[];
  solve: Solve | null;
  /** Why a booked shipment's recorded decision could not be loaded, if it couldn't. */
  loadError: string | null;
  busy: boolean;
  /** This shipment is covered by the batch plan currently up. */
  planMode: boolean;
  /** Assigned count of that plan (0 when there is none). */
  planCount: number;
  liveNow: string | null;
  /** Live head (not replay): lifecycle actions are available. */
  live: boolean;
  onSolve: () => void;
  onCommit: () => void;
  onCommitPlan: () => void;
  onDiscardPlan: () => void;
  onCancelShipment: () => void;
  onSetReady: (newReady: string) => void;
  onClose: () => void;
}

const ISO_STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

/** Constraint data as the engine recorded it, with timestamps made readable. */
function fmtValue(value: unknown): string {
  if (typeof value === "string") {
    if (ISO_STAMP.test(value)) return fmtTime(value);
    if (ISO_DAY.test(value)) return fmtDay(value);
    return value;
  }
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(3);
  if (Array.isArray(value)) return `[${value.map(fmtValue).join(", ")}]`;
  return JSON.stringify(value);
}

function rejectText(verdict: Reject): string {
  return Object.entries(verdict.data)
    .map(([key, value]) => `${key}=${fmtValue(value)}`)
    .join(" · ");
}

/** Minutes between two engine timestamps; null when either is unparseable.
    Records stamp "…Z" and read models "+00:00" — both are parsed, never
    compared as strings. */
function minutesBetween(from: string, to: string): number | null {
  const delta = (new Date(to).getTime() - new Date(from).getTime()) / 60000;
  return Number.isFinite(delta) ? delta : null;
}

/** What a dwell at a facility is called on the itinerary. The hold has a row of
    its own already, so it never reaches this table. Exported so every surface
    that shows the same dwell calls it the same thing. */
export const STOP_LABEL: Record<StopRole, string> = {
  entry: "ENTRY",
  transit: "CROSS-DOCK",
  hold: "HOLD",
  exit: "EXIT",
};

/** A dwell the goods spend at a facility on the way. The hold has its own row
    (and its own reservation), so only the pass-through stops render here. */
function StopRow({ stop, facilities }: { stop: Stop; facilities: FacilitySummary[] }) {
  const dwell = minutesBetween(stop.arrive, stop.depart);
  return (
    <div className="leg stop-leg">
      <span className="stop-kind">{STOP_LABEL[stop.role]}</span>
      <span className="leg-body">
        <span className="leg-main">
          {facilityLabel(stop.facility_id, facilities)}
          {stop.zone_id ? ` · ${stop.zone_id}` : ""}
        </span>
        {/* Same shape as every other row in the block: how long, then from
            when to when — a dwell and a hold must not read differently. */}
        <span className="leg-meta">
          {dwell !== null && dwell > 0 ? `${fmtDuration(dwell)} · ` : ""}
          {fmtTime(stop.arrive)} → {fmtTime(stop.depart)}
        </span>
      </span>
    </div>
  );
}

/** The dwells a booking with no itinerary of its own records (§7.9): one
    stop-aware router plans every shipment, so an ordinary allocation stages
    through a hub exactly as a delivery does — the same rows, without the
    customer legs around them. */
function StopsBlock({ stops, facilities }: { stops: Stop[]; facilities: FacilitySummary[] }) {
  const dwells = stops.filter((stop) => stop.role !== "hold");
  if (dwells.length === 0) return null;
  return (
    <>
      <div className="panel-header">
        <span className="micro">STOPS ON THE WAY</span>
      </div>
      <div className="itinerary">
        {dwells.map((stop, index) => (
          <StopRow key={`stop-${index}`} stop={stop} facilities={facilities} />
        ))}
      </div>
    </>
  );
}

/** The delivery journey in order (§7.9): first mile, lanes in, the dwells at
    every facility it touches, the hold, lanes out, last mile, and the moment
    the customer has the goods. */
function ItineraryBlock({
  itinerary,
  deadline,
  facilities,
}: {
  itinerary: Itinerary;
  deadline: string | null;
  facilities: FacilitySummary[];
}) {
  const { hold, legs } = itinerary;
  // The hold splits the legs exactly as the engine's own rendering does: the
  // outbound half is everything departing at or after the hold ends.
  const until = new Date(hold.until_ts).getTime();
  const outboundAt = legs.findIndex((leg) => new Date(leg.depart).getTime() >= until);
  const holdAt = outboundAt === -1 ? legs.length : outboundAt;
  const holdMinutes = minutesBetween(hold.from_ts, hold.until_ts);
  const lateBy = deadline ? minutesBetween(deadline, itinerary.delivered_at) : null;

  const legRow = (leg: Itinerary["legs"][number], index: number) => {
    // The engine leaves lane_id null on exactly the two road miles at the ends
    // of the journey, so that — not the leg's index — says which is which: a
    // delivery can begin at a facility (its first leg is a lane, however it is
    // driven) and can be a single last mile with no leg before it.
    const kind =
      leg.lane_id !== null
        ? leg.kind.toUpperCase()
        : index === legs.length - 1
          ? "LAST MILE"
          : "FIRST MILE";
    return (
      <div className="leg" key={`leg-${index}`}>
        <span className="leg-kind">{kind}</span>
        <span className="leg-body">
          <span className="leg-main">
            {leg.from_label} → {leg.to_label}
          </span>
          <span className="leg-meta">
            {Math.round(leg.km).toLocaleString("en-US")} km · {fmtDuration(leg.minutes)} ·{" "}
            {fmtTime(leg.depart)} → {fmtTime(leg.arrive)}
          </span>
        </span>
      </div>
    );
  };

  const holdRow = (
    <div className="leg hold-leg" key="hold">
      <span className="leg-kind">HOLD</span>
      <span className="leg-body">
        <span className="leg-main">
          {facilityLabel(hold.facility_id, facilities)} / {hold.zone_id}
        </span>
        <span className="leg-meta">
          {holdMinutes === null ? "—" : fmtDuration(holdMinutes)} · {fmtTime(hold.from_ts)} →{" "}
          {fmtTime(hold.until_ts)}
        </span>
      </span>
    </div>
  );

  // Legs and dwells share one timeline: a stop belongs above the leg the goods
  // leave it on, so every dwell whose arrival is not after that leg's departure
  // is drawn first. Stops the engine did not record (an older booking) simply
  // leave the block as it was.
  const dwells = (itinerary.stops ?? []).filter((stop) => stop.role !== "hold");
  let nextStop = 0;
  const dwellsUpTo = (depart: string | null) => {
    const rows = [];
    while (nextStop < dwells.length) {
      const stop = dwells[nextStop]!;
      if (depart !== null && new Date(stop.arrive).getTime() > new Date(depart).getTime()) break;
      rows.push(<StopRow key={`stop-${nextStop}`} stop={stop} facilities={facilities} />);
      nextStop += 1;
    }
    return rows;
  };
  const rows = legs.map((leg, index) => (
    <Fragment key={index}>
      {index === holdAt && holdRow}
      {dwellsUpTo(leg.depart)}
      {legRow(leg, index)}
    </Fragment>
  ));

  return (
    <>
      <div className="panel-header">
        <span className="micro">ITINERARY</span>
        {/* The whole journey, not the inbound leg the winner block prices. */}
        <span className="micro">{fmtMoney(itinerary.cost_cents)} END TO END</span>
      </div>
      <div className="itinerary">
        {rows}
        {holdAt === legs.length && holdRow}
        {dwellsUpTo(null)}
        <div className={lateBy !== null && lateBy > 0 ? "leg deliver late" : "leg deliver"}>
          <span className="leg-kind">DELIVER</span>
          <span className="leg-body">
            <span className="leg-main">{itinerary.destination}</span>
            <span className="leg-meta">
              {fmtTime(itinerary.delivered_at)}
              {deadline === null
                ? " · no deadline"
                : lateBy === null
                  ? ""
                  : lateBy > 0
                    ? ` · LATE by ${fmtDuration(lateBy)} (DELIVER BY ${fmtTime(deadline)})`
                    : ` · ${fmtDuration(-lateBy)} before DELIVER BY ${fmtTime(deadline)}`}
            </span>
          </span>
        </div>
      </div>
    </>
  );
}

function chosenCandidate(record: DecisionRecord): ScoredCandidate | null {
  if (!record.chosen) return null;
  return (
    record.scored.find(
      (c) =>
        c.facility_id === record.chosen!.facility_id && c.zone_id === record.chosen!.zone_id,
    ) ?? null
  );
}

export function DecisionPanel({
  shipment,
  facilities,
  solve,
  loadError,
  busy,
  planMode,
  planCount,
  liveNow,
  live,
  onSolve,
  onCommit,
  onCommitPlan,
  onDiscardPlan,
  onCancelShipment,
  onSetReady,
  onClose,
}: Props) {
  const [readyDraft, setReadyDraft] = useState("");
  const [confirmCancel, setConfirmCancel] = useState(false);
  // The draft follows the selected shipment; a confirm never survives a
  // selection change.
  useEffect(() => {
    setReadyDraft(shipment ? shipment.ready_at.slice(0, 16) : "");
    setConfirmCancel(false);
  }, [shipment?.id, shipment?.ready_at, shipment]);
  const record = solve?.record ?? null;
  const winner = record ? chosenCandidate(record) : null;
  const alternatives = record ? record.scored.filter((c) => c !== winner).slice(0, 6) : [];
  const worstTotal =
    record && record.scored.length > 0 ? Math.max(...record.scored.map((c) => c.total)) : 1;
  const state = solve?.committed ? "COMMITTED" : solve?.fromPlan ? "BATCH PLAN" : "DRAFT";
  const uniform = uniformReject(record);
  // Where a delivery is held on the way to its customer: the booking if it has
  // one, otherwise whatever the record on screen chose.
  const holdingAt = shipment?.destination ?? record?.chosen ?? null;
  const via = holdingAt ? `${holdingAt.facility_id} / ${holdingAt.zone_id}` : null;

  return (
    <aside className="overlay right">
      <div className="panel-header">
        <div className="panel-title">
          <span className="micro">
            {shipment ? shipment.id : "DECISION"}
            {record ? ` · ${state}` : ""}
          </span>
          {record?.solver && (
            <span className="micro">
              {record.solver.status}
              {record.solver.gap !== null && record.solver.gap > 0.0005
                ? ` GAP ${(record.solver.gap * 100).toFixed(2)}%`
                : ""}
              {record.reopt_tier ? ` · RE-OPT TIER ${record.reopt_tier}` : ""}
            </span>
          )}
        </div>
        <button className="close" onClick={onClose} title="Close and show the whole network again">
          CLOSE ✕
        </button>
      </div>
      <div className="panel-body">
        {/* Trapped cargo (§7.9) is the loudest thing this panel can say: the
            goods are standing at a facility that shut around them, the booking
            they keep cannot be executed, and no re-optimization will touch
            them. Cancelling is the only clearing path the system offers today,
            so the banner names it and CANCEL SHIPMENT stays below. */}
        {shipment?.trapped && (
          <div className="trapped-banner">
            <span className="trapped-headline">
              TRAPPED AT {trappedAt(shipment, facilities)} — MANUAL CLEARING REQUIRED
            </span>
            {/* Two ways to be trapped, and they are not the same sentence:
                cargo that HELD a booking when the closure landed keeps it,
                while cargo standing at a facility that was already shut never
                had one and cannot be given one. Saying "it keeps its booking"
                to the second is simply false. */}
            <span className="trapped-note">
              {shipment.destination
                ? "A closure shut around this cargo where it stands: it keeps the booking it already had, and re-optimization will not re-plan it."
                : "A closure has shut the facility this cargo stands in: nothing can be routed out of it while the closure lasts, so no destination can be chosen for it."}{" "}
              {/* The button the sentence points at is live-only. Under a
                  REPLAY header it is not there to press, so the banner must
                  not send the operator looking for it. */}
              {live
                ? "CANCEL SHIPMENT below releases it — today that is the only way to clear it."
                : "Cancelling it is the only way to clear it — and that can only be done live."}
            </span>
          </div>
        )}
        {shipment && (
          <div className="route-block">
            <div className="row-line">
              <span className="row-key">FROM</span>
              <span className="row-val">{originLabel(shipment, facilities)}</span>
            </div>
            <div className="row-line">
              <span className="row-key">TO</span>
              <span className="row-val">
                {/* A delivery goes to the CUSTOMER; the facility it is held at
                    is a stop on the way — secondary "via" information here,
                    and a row of its own in the itinerary below. */}
                {shipment.destination_point ? (
                  <>
                    {shipment.destination_point.label}
                    <span className="muted">
                      {via ? (
                        <>
                          {" · via "}
                          <span className="nowrap">{via}</span>
                        </>
                      ) : (
                        " · customer · not yet routed"
                      )}
                      {shipment.hold_days > 0 ? ` · hold ${shipment.hold_days}d` : ""}
                    </span>
                  </>
                ) : record?.chosen ? (
                  <>
                    {facilityLabel(record.chosen.facility_id, facilities)} / {record.chosen.zone_id}
                  </>
                ) : shipment.destination ? (
                  <>
                    {facilityLabel(shipment.destination.facility_id, facilities)} /{" "}
                    {shipment.destination.zone_id}
                  </>
                ) : (
                  <span className="muted">not yet decided</span>
                )}
              </span>
            </div>
            <div className="row-line">
              <span className="row-key">CARGO</span>
              <span className="row-val">
                {fmtCargoLine(shipment)}
                {shipment.requirements.temp_c
                  ? ` · COLD ${fmtTemp(shipment.requirements.temp_c)}`
                  : ""}
                {shipment.requirements.compat_class
                  ? ` · CLASS ${shipment.requirements.compat_class}`
                  : ""}
              </span>
            </div>
            <div className="row-line">
              <span className="row-key">READY</span>
              <span className="row-val">
                {fmtTime(shipment.ready_at)}
                <span className="muted"> {fmtRelative(shipment.ready_at, liveNow)}</span>
              </span>
            </div>
            <div className="row-line">
              <span className="row-key">{shipment.destination_point ? "DELIVER BY" : "DUE"}</span>
              <span className="row-val">
                {shipment.deadline ? (
                  <>
                    {fmtTime(shipment.deadline)}
                    <span className="muted"> {fmtRelative(shipment.deadline, liveNow)}</span>
                  </>
                ) : (
                  <span className="muted">open</span>
                )}
              </span>
            </div>
            {/* A delivery's arrival is at the hold, not at its destination:
                the itinerary below states it — with the hold and the last mile
                around it — so the row would only repeat it under a wrong name. */}
            {record?.chosen && !record.chosen.itinerary && (
              <div className="row-line">
                <span className="row-key">ARRIVE</span>
                <span className="row-val">
                  {fmtTime(record.chosen.eta)}
                  <span className="muted"> · stored until {fmtTime(record.chosen.departure)}</span>
                </span>
              </div>
            )}
            {live && (shipment.status === "planned" || shipment.status === "allocated") && (
              <div className="field-row shipment-actions">
                <label htmlFor="ready-draft">READY (UTC)</label>
                <input
                  id="ready-draft"
                  type="datetime-local"
                  value={readyDraft}
                  onChange={(event) => setReadyDraft(event.target.value)}
                />
                <button
                  disabled={busy || !readyDraft || readyDraft === shipment.ready_at.slice(0, 16)}
                  title="Move readiness; delaying a booked shipment re-optimizes it"
                  onClick={() => onSetReady(`${readyDraft}:00+00:00`)}
                >
                  SET
                </button>
                <button
                  className={confirmCancel ? "danger" : ""}
                  disabled={busy}
                  title="Cancel this shipment; a booked one releases its reservations"
                  onClick={() => {
                    if (confirmCancel) {
                      setConfirmCancel(false);
                      onCancelShipment();
                    } else {
                      setConfirmCancel(true);
                    }
                  }}
                  onBlur={() => setConfirmCancel(false)}
                >
                  {confirmCancel ? "CONFIRM CANCEL?" : "CANCEL SHIPMENT"}
                </button>
              </div>
            )}
          </div>
        )}

        {record?.chosen?.itinerary ? (
          <ItineraryBlock
            itinerary={record.chosen.itinerary}
            deadline={shipment?.deadline ?? null}
            facilities={facilities}
          />
        ) : (
          // No customer journey to draw, but the plan can still stage through
          // hubs on the way: those dwells are bookings the operator must be
          // able to see — from the record while it is a draft, from the
          // booking once committed.
          <StopsBlock
            stops={record?.chosen?.stops ?? shipment?.destination?.stops ?? []}
            facilities={facilities}
          />
        )}

        {shipment && !record && (
          <div className="reject-line row-sub">
            {shipment.status === "planned"
              ? "Run SOLVE to survey every facility for this shipment."
              : loadError
                ? `Already ${shipment.status}, but its recorded decision could not be loaded: ${loadError}`
                : `Already ${shipment.status}; loading its recorded decision…`}
          </div>
        )}

        {record && winner && record.chosen && (
          <div className="winner">
            <div className="winner-head">
              <span className="row-title">
                {record.chosen.facility_id} / {record.chosen.zone_id}
              </span>
              <span className="score" title="Weighted objective: lower is better">
                {winner.total.toFixed(3)}
              </span>
            </div>
            <div className="stat-grid">
              <div className="stat">
                <span className="micro">COST</span>
                <span className="mono">{fmtMoney(winner.route.cost_cents)}</span>
              </div>
              <div className="stat">
                <span className="micro">DISTANCE</span>
                <span className="mono">{Math.round(winner.route.km).toLocaleString("en-US")} km</span>
              </div>
              <div className="stat">
                <span className="micro">TRANSIT</span>
                <span className="mono">
                  {fmtDuration(winner.route.minutes)}
                  {winner.route.wait_minutes ? ` +${fmtDuration(winner.route.wait_minutes)} wait` : ""}
                </span>
              </div>
              <div className="stat">
                <span className="micro">LEGS</span>
                <span className="mono">{winner.route.legs.length}</span>
              </div>
            </div>
            <table className="component-table">
              <thead>
                <tr>
                  <th>component</th>
                  <th>norm</th>
                  <th>weight</th>
                  <th>adds</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(winner.components).map(([name, score]) => (
                  <tr key={name}>
                    <td>
                      {name}
                      {score.baseline !== null ? ` (from ${score.baseline.toFixed(3)})` : ""}
                    </td>
                    <td>{score.normalized.toFixed(4)}</td>
                    <td>{score.weight.toFixed(2)}</td>
                    <td className="contribution">
                      {score.contribution >= 0 ? "+" : ""}
                      {score.contribution.toFixed(4)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {record && !record.chosen && (
          <div className="winner">
            <span className="reject-code">NO FEASIBLE DESTINATION</span>
            {/* One cause shared by every facility (a closed origin, say) is
                stated once here, so nobody has to scan the list to find it —
                the same cause the queue row names. */}
            {uniform ? (
              <div className="reject-data">
                every facility: {uniform.constraint_id} · {rejectText(uniform)}
              </div>
            ) : (
              <div className="reject-data">every facility was rejected — see below</div>
            )}
          </div>
        )}

        {alternatives.length > 0 && (
          <>
            <div className="panel-header">
              <span className="micro">ALTERNATIVES</span>
            </div>
            {alternatives.map((candidate) => (
              <div key={`${candidate.facility_id}/${candidate.zone_id}`} className="reject-line">
                <div className="row-title mono alt-title">
                  <span>
                    {candidate.facility_id}/{candidate.zone_id}
                  </span>
                  <span>
                    {candidate.total.toFixed(3)} · {fmtMoney(candidate.route.cost_cents)} ·{" "}
                    {fmtDuration(candidate.route.minutes)}
                  </span>
                </div>
                <div
                  className="alt-bar"
                  style={{
                    width: `${Math.max(6, 100 - (candidate.total / Math.max(worstTotal, 1e-9)) * 94)}%`,
                  }}
                />
              </div>
            ))}
          </>
        )}

        {record && record.rejected.length > 0 && (
          <>
            <div className="panel-header">
              <span className="micro">REJECTED ({record.rejected.length})</span>
            </div>
            {record.rejected.map((rejection) => (
              <div key={rejection.facility_id} className="reject-line">
                <div className="row-title reject-title">
                  <span>{rejection.facility_id}</span>
                  <span className="badge reject">
                    {rejection.facility_verdicts[0]?.constraint_id ??
                      Object.values(rejection.zone_verdicts)[0]?.[0]?.constraint_id ??
                      "NO ZONE"}
                  </span>
                </div>
                {rejection.facility_verdicts.map((verdict, index) => (
                  <div key={index} className="reject-data">
                    {verdict.constraint_id}: {rejectText(verdict)}
                  </div>
                ))}
                {Object.entries(rejection.zone_verdicts).map(([zoneId, verdicts]) => (
                  <div key={zoneId} className="reject-data">
                    {zoneId}: {verdicts.map((v) => `${v.constraint_id} ${rejectText(v)}`).join("; ")}
                  </div>
                ))}
              </div>
            ))}
          </>
        )}
      </div>
      {/* Replay is a view of history: nothing is solved, committed, or
          discarded from inside one — the same rule the lifecycle row above
          and the facility popover already follow. */}
      {live && shipment?.status === "planned" && (
        <div className="panel-footer">
          {planMode ? (
            <>
              <button onClick={onDiscardPlan} disabled={busy}>
                DISCARD PLAN
              </button>
              <button className="primary" onClick={onCommitPlan} disabled={busy || planCount === 0}>
                COMMIT PLAN ({planCount})
              </button>
            </>
          ) : (
            <>
              <button onClick={onSolve} disabled={busy}>
                {record ? "RE-SOLVE" : "SOLVE"}
              </button>
              <button
                className="primary"
                onClick={onCommit}
                disabled={busy || !record?.chosen || solve?.committed === true}
              >
                {solve?.committed ? "COMMITTED ✓" : "COMMIT"}
              </button>
            </>
          )}
        </div>
      )}
    </aside>
  );
}
