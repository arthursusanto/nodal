/* Shipment queue (left overlay) + the active-disruption board. Every row
   states where the shipment is FROM and where it is going TO, what the cargo
   is in its own units, and when it is due — in readable times. With a batch
   plan up, planned rows show where the plan sends them and the header action
   becomes COMMIT PLAN / DISCARD. Disruption rows open their facility. */

import { useState } from "react";

import type {
  BatchPlan,
  DecisionRecord,
  DisruptionSummary,
  FacilitySummary,
  Reject,
  ShipmentSummary,
} from "../api";
import { fmtCargo, fmtRelative, fmtTemp, fmtTime, fmtWeight } from "../fmt";

interface Props {
  shipments: ShipmentSummary[];
  facilities: FacilitySummary[];
  selected: string | null;
  onSelect: (shipmentId: string | null) => void;
  disruptions: DisruptionSummary[];
  onOpenDisruption: (facilityId: string | null, disruptionId: string, kind: string) => void;
  busy: boolean;
  plan: BatchPlan | null;
  /** Live head (not replay): the plan under review can be acted on. */
  live: boolean;
  liveNow: string | null;
  onOptimizeAll: () => void;
  onCommitPlan: () => void;
  onDiscardPlan: () => void;
  onNewShipment: () => void;
  onRebalance: () => void;
}

const STATUS_ORDER: Record<string, number> = {
  planned: 0,
  allocated: 1,
  in_transit: 2,
  arrived: 3,
};

/** "BOM · Mumbai Nhava Sheva" — id plus name when the facility is known. */
export function facilityLabel(id: string, facilities: FacilitySummary[]): string {
  const facility = facilities.find((f) => f.id === id);
  return facility ? `${facility.id} · ${facility.name}` : id;
}

export function originLabel(shipment: ShipmentSummary, facilities: FacilitySummary[]): string {
  if (shipment.origin_facility_id) return facilityLabel(shipment.origin_facility_id, facilities);
  return shipment.origin_label ?? "unknown origin";
}

/** Where trapped cargo physically sits: the facility it was standing in when
    the closure landed, which is the shipment's own origin facility (§7.9). */
export function trappedAt(shipment: ShipmentSummary, facilities: FacilitySummary[]): string {
  return shipment.origin_facility_id
    ? facilityLabel(shipment.origin_facility_id, facilities)
    : (shipment.origin_label ?? "its origin");
}

/** The one verdict every facility returned, when they all returned the same
    one — a closed origin, say. That single cause is what a shipment nothing
    could place is really waiting on, so both surfaces that report an
    unplaceable shipment state it, and both derive it here. */
export function uniformReject(record: DecisionRecord | null | undefined): Reject | null {
  if (!record || record.rejected.length === 0) return null;
  const first = record.rejected[0]?.facility_verdicts[0];
  if (!first) return null;
  return record.rejected.every((r) => r.facility_verdicts[0]?.constraint_id === first.constraint_id)
    ? first
    : null;
}

/** "20 units GENERAL-21 · 9.0 t · 20 slots" — cargo in its units, then what it costs in capacity. */
export function fmtCargoLine(shipment: ShipmentSummary): string {
  const parts = [fmtCargo(shipment.lines), fmtWeight(shipment.size.weight_g)].filter(Boolean);
  parts.push(`${shipment.size.slots} slots`);
  return parts.join(" · ");
}

export function QueuePanel({
  shipments,
  facilities,
  selected,
  onSelect,
  disruptions,
  onOpenDisruption,
  busy,
  plan,
  live,
  liveNow,
  onOptimizeAll,
  onCommitPlan,
  onDiscardPlan,
  onNewShipment,
  onRebalance,
}: Props) {
  const [confirmRebalance, setConfirmRebalance] = useState(false);
  const rows = [...shipments].sort(
    (a, b) =>
      (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) || a.id.localeCompare(b.id),
  );
  const planned = rows.filter((s) => s.status === "planned").length;
  const planAssigned = plan
    ? Object.values(plan.assignments).filter((pair) => pair !== null).length
    : 0;
  const planTotal = plan ? Object.keys(plan.assignments).length : 0;
  const active = disruptions.filter((d) => d.status === "active");

  return (
    <aside className="overlay left">
      <div className="panel-header">
        <span className="micro">QUEUE</span>
        <span className="micro">
          {planned} PLANNED · {rows.length - planned} BOOKED
        </span>
      </div>
      {/* No "reviewed at this point in the log" state: a review is never open
          at a replayed stamp. A draft and the commit or discard that ends it
          carry the SAME log timestamp — every command is stamped with the last
          event's ts — so state_at() lands on both or on neither, and a draft
          still pending stands at the head, where the view is live by
          definition. The plan below is therefore always the live one, and the
          replay lock disables its actions like every other command. */}
      {plan ? (
        <div className="field-row plan-actions">
          <button className="primary" disabled={busy || planAssigned === 0} onClick={onCommitPlan}>
            COMMIT PLAN ({planAssigned}/{planTotal})
          </button>
          <button disabled={busy} onClick={onDiscardPlan} title="Drop the plan; nothing was written">
            DISCARD
          </button>
        </div>
      ) : (
        <div className="field-row queue-actions">
          {planned > 1 && (
            <button
              className="primary"
              style={{ flex: 1 }}
              disabled={busy}
              onClick={onOptimizeAll}
              title="Solve every planned shipment together; review the plan before committing"
            >
              OPTIMIZE ALL ({planned})
            </button>
          )}
          <button disabled={busy} onClick={onNewShipment} title="Register a new shipment">
            + NEW
          </button>
          <button
            className={confirmRebalance ? "danger" : ""}
            disabled={busy}
            title="Order rebalancing transfers between facilities and book them (§7.7)"
            onClick={() => {
              if (confirmRebalance) {
                setConfirmRebalance(false);
                onRebalance();
              } else {
                setConfirmRebalance(true);
              }
            }}
            onBlur={() => setConfirmRebalance(false)}
          >
            {confirmRebalance ? "CONFIRM?" : "REBALANCE"}
          </button>
        </div>
      )}
      <div className="panel-body">
        {/* An empty queue is a state, not a blank panel: replayed to before the
            first shipment, or a queue worked all the way down. */}
        {rows.length === 0 && (
          <div className="reject-line row-sub">
            {live
              ? "Nothing in the queue. + NEW registers a shipment."
              : "Nothing had been registered at this point in the log."}
          </div>
        )}
        {rows.map((shipment) => {
          const planPair = plan?.assignments[shipment.id];
          const inPlan =
            plan !== null && shipment.status === "planned" && shipment.id in plan.assignments;
          const dest = shipment.destination;
          // A delivery goes TO its customer; the facility it is held at is a
          // stop on the way, so it reads as secondary "via" information.
          const customer = shipment.destination_point;
          // A shipment the plan could not place: say what stopped it, not just
          // that something did. The reason is often the whole story (a closure
          // shut its origin), and it is one word long.
          const planReject = inPlan && !planPair ? uniformReject(plan?.records[shipment.id]) : null;
          const needs = [
            shipment.requirements.temp_c ? `COLD ${fmtTemp(shipment.requirements.temp_c)}` : "",
            shipment.requirements.compat_class ? `CLASS ${shipment.requirements.compat_class}` : "",
          ].filter(Boolean);
          return (
            <button
              key={shipment.id}
              className={shipment.id === selected ? "row selected" : "row"}
              onClick={() => onSelect(shipment.id === selected ? null : shipment.id)}
            >
              <span className="row-title">
                <span>
                  {shipment.id}
                  {shipment.is_transfer ? " ⇄" : ""}
                </span>
                <span className="row-badges">
                  <span className={`badge ${shipment.status}`}>{shipment.status.toUpperCase()}</span>
                  {shipment.trapped && <span className="badge trapped">TRAPPED</span>}
                </span>
              </span>
              {/* A closure shut around these goods: they are not going
                  anywhere until an operator clears them by hand (§7.9). */}
              {shipment.trapped && (
                <span className="row-line plain trapped-line">
                  trapped at {trappedAt(shipment, facilities)} — manual clearing required
                </span>
              )}
              <span className="row-line">
                <span className="row-key">FROM</span>
                <span className="row-val">{originLabel(shipment, facilities)}</span>
              </span>
              <span className="row-line">
                <span className="row-key">TO</span>
                <span className="row-val">
                  {customer ? (
                    <>
                      {customer.label}
                      {dest ? (
                        <span className="muted">
                          {" · via "}
                          <span className="nowrap">
                            {dest.facility_id} / {dest.zone_id}
                          </span>
                        </span>
                      ) : inPlan && planPair ? (
                        <span className="plan-tag">
                          {" "}
                          · PLAN → via {planPair[0]} / {planPair[1]}
                        </span>
                      ) : inPlan ? (
                        <span className="plan-tag unassigned">
                          {" · PLAN: NO FEASIBLE ROUTE"}
                          {planReject ? ` · ${planReject.constraint_id}` : ""}
                        </span>
                      ) : (
                        <span className="muted"> · not yet routed</span>
                      )}
                    </>
                  ) : dest ? (
                    <>
                      {facilityLabel(dest.facility_id, facilities)} / {dest.zone_id} · arrives{" "}
                      {fmtTime(dest.eta)}
                    </>
                  ) : inPlan ? (
                    planPair ? (
                      <span className="plan-tag">
                        PLAN → {facilityLabel(planPair[0], facilities)} / {planPair[1]}
                      </span>
                    ) : (
                      <span className="plan-tag unassigned">
                        PLAN: NO FEASIBLE DESTINATION
                        {planReject ? ` · ${planReject.constraint_id}` : ""}
                      </span>
                    )
                  ) : (
                    <span className="muted">not yet booked</span>
                  )}
                </span>
              </span>
              <span className="row-line">
                <span className="row-key">CARGO</span>
                <span className="row-val">{fmtCargoLine(shipment)}</span>
              </span>
              <span className="row-line">
                <span className="row-key">DUE</span>
                <span className="row-val">
                  {shipment.deadline ? (
                    <>
                      {fmtTime(shipment.deadline)}{" "}
                      <span className="muted">{fmtRelative(shipment.deadline, liveNow)}</span>
                    </>
                  ) : (
                    <span className="muted">open</span>
                  )}
                </span>
                {needs.length > 0 && <span className="row-needs">{needs.join(" · ")}</span>}
              </span>
            </button>
          );
        })}
      </div>
      {active.length > 0 && (
        <div className="disruption-board">
          <div className="panel-header">
            <span className="micro">ACTIVE DISRUPTIONS</span>
            <span className="micro">{active.length}</span>
          </div>
          {active.slice(0, 4).map((disruption) => (
            <button
              key={disruption.id}
              className="row disruption-row"
              onClick={() => onOpenDisruption(disruption.facility_id, disruption.id, disruption.kind)}
              title="Open the facility to end or reschedule this disruption"
            >
              <span className="reject-code">
                {disruption.kind.replace("_", " ").toUpperCase()} · {disruption.target_id}
                {disruption.magnitude < 1 ? ` · CUT ${(disruption.magnitude * 100).toFixed(0)}%` : ""}
              </span>
              <span className="row-line plain">
                UNTIL {fmtTime(disruption.until_ts)}{" "}
                <span className="muted">{fmtRelative(disruption.until_ts, liveNow)}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </aside>
  );
}
