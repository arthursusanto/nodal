/* Schedules view: one row per WINDOW the booked goods occupy (§7.9) — every
   dwell on the way and the last mile that ends a delivery — time-ordered, in
   readable times. A shipment fills as many rows as it has windows. */

import { useEffect, useState } from "react";

import { api } from "../api";
import type { MovementRole, ScheduleData } from "../api";
import { fmtTime } from "../fmt";
import { STOP_LABEL } from "./DecisionPanel";

/** What each row IS, named exactly as the decision panel names the same dwell.
    The last mile is the one window that is not a stay at a facility. */
const ROLE_LABEL: Record<MovementRole, string> = { ...STOP_LABEL, last_mile: "LAST MILE" };

export function SchedulesPanel({ at, onClose }: { at: string | null; onClose: () => void }) {
  const [data, setData] = useState<ScheduleData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .schedules(at)
      .then((body) => {
        if (!cancelled) setData(body);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [at]);

  const movements = data?.movements ?? [];
  const shipments = new Set(movements.map((movement) => movement.shipment_id)).size;

  return (
    <aside className="overlay center">
      <div className="panel-header">
        <span className="micro">SCHEDULES · BOOKED MOVEMENTS</span>
        {/* Rows are windows, not shipments: say both, or the count reads as a
            queue that suddenly grew. */}
        <span className="micro">
          {movements.length} MOVEMENTS · {shipments} SHIPMENTS
        </span>
        <button className="close" onClick={onClose}>
          CLOSE ✕
        </button>
      </div>
      {/* Dozens of one-line windows: the body scrolls sideways when the panel is
          too narrow for the columns, rather than squeezing them together. */}
      <div className="panel-body table-scroll">
        {error && <div className="reject-line reject-code">{error}</div>}
        <table className="data">
          <thead>
            <tr>
              <th>SHIPMENT</th>
              <th>STATUS</th>
              <th>WHERE</th>
              <th>READY</th>
              <th>ARRIVES</th>
              <th>DEPARTS</th>
            </tr>
          </thead>
          <tbody>
            {movements.map((movement) => (
              <tr key={movement.id}>
                <td>
                  {movement.shipment_id}
                  {movement.is_transfer ? " ⇄" : ""}
                </td>
                <td>{movement.status}</td>
                <td>
                  <span className="movement-where">
                    <span className="movement-role">{ROLE_LABEL[movement.role]}</span>
                    <span>
                      {movement.role === "last_mile" ? (
                        <>
                          {/* The last mile ends at the CUSTOMER; the facility is
                              only where it departs from. */}
                          {movement.customer_label ?? "customer"}{" "}
                          <span className="muted nowrap">from {movement.facility_id}</span>
                        </>
                      ) : (
                        <span className="nowrap">
                          {movement.facility_id}
                          {movement.zone_id ? ` / ${movement.zone_id}` : ""}
                        </span>
                      )}
                    </span>
                  </span>
                </td>
                <td className="nowrap">{fmtTime(movement.ready_at)}</td>
                <td className="nowrap">{fmtTime(movement.arrive)}</td>
                {/* A window with no length — the last mile — has no departure to
                    state; showing the arrival twice would read as a stay. */}
                <td className="nowrap">
                  {movement.depart && movement.depart !== movement.arrive
                    ? fmtTime(movement.depart)
                    : "—"}
                </td>
              </tr>
            ))}
            {data && movements.length === 0 && (
              <tr>
                <td colSpan={6}>nothing booked yet</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </aside>
  );
}
