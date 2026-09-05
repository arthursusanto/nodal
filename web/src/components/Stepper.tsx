/* The solve-pipeline stepper (design handoff 2c — the one structural
   must-keep). State, not decoration: each cell mirrors live data. */

import type { ShipmentSummary } from "../api";
import type { Solve } from "../App";
import { fmtRelative, fmtTime } from "../fmt";

interface Props {
  shipment: ShipmentSummary | null;
  solve: Solve | null;
  liveNow: string | null;
}

interface Cell {
  number: string;
  label: string;
  detail: string;
  state: "pending" | "active" | "done" | "failed";
}

export function Stepper({ shipment, solve, liveNow }: Props) {
  const record = solve?.record ?? null;
  const committed = solve?.committed ?? false;
  const feasibleCount = record ? record.scored.length : 0;
  const consideredCount = record ? record.considered.length : 0;
  const chosenScore = record?.chosen
    ? record.scored
        .find(
          (c) =>
            c.facility_id === record.chosen!.facility_id && c.zone_id === record.chosen!.zone_id,
        )
        ?.total.toFixed(3)
    : null;

  const cells: Cell[] = [
    {
      number: "01",
      label: "SELECT",
      detail: shipment
        ? `${shipment.id} · from ${shipment.origin_label ?? shipment.origin_facility_id ?? "?"}` +
          (shipment.deadline ? ` · due ${fmtRelative(shipment.deadline, liveNow)}` : "")
        : "PICK A SHIPMENT",
      state: shipment ? "done" : "active",
    },
    {
      number: "02",
      label: "FEASIBILITY",
      detail: record ? `${consideredCount} facilities surveyed → ${feasibleCount} feasible` : "—",
      state: record ? "done" : shipment ? "active" : "pending",
    },
    {
      number: "03",
      label: "SOLVE",
      detail: record
        ? record.chosen
          ? // A delivery's moment is when the customer has the goods; anything
            // else arrives at the facility it is stored in.
            (record.chosen.itinerary
              ? `${record.chosen.facility_id} · delivers ${fmtTime(record.chosen.itinerary.delivered_at)}`
              : `${record.chosen.facility_id} · arrives ${fmtTime(record.chosen.eta)}`) +
            ` · score ${chosenScore ?? ""}`
          : "NO FEASIBLE DESTINATION"
        : "—",
      state: record ? (record.chosen ? "done" : "failed") : "pending",
    },
    {
      number: "04",
      label: "COMMIT",
      detail: committed
        ? "COMMITTED TO LOG"
        : record?.chosen
          ? solve?.fromPlan
            ? "IN BATCH PLAN · COMMIT PLAN TO BOOK"
            : "AWAITING OPERATOR"
          : "—",
      state: committed ? "done" : record?.chosen ? "active" : "pending",
    },
  ];

  return (
    <div className="stepper">
      {cells.map((cell) => (
        <div key={cell.number} className={`stage-cell ${cell.state}`}>
          <span className="stage-number">
            {cell.number}{" "}
            {cell.state === "done"
              ? "✓"
              : cell.state === "failed"
                ? "✗"
                : cell.state === "active"
                  ? "▸"
                  : "·"}
          </span>
          <span className="stage-label">{cell.label}</span>
          <span className="stage-detail">{cell.detail}</span>
        </div>
      ))}
    </div>
  );
}
