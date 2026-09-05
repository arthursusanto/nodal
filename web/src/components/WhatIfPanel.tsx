/* What-if panel (§4): compose hypotheticals, run the fork, diff against the
   untouched baseline. The grammar is the engine's own. */

import { useEffect, useState } from "react";

import { api } from "../api";
import type { FacilitySummary, WhatIfResult } from "../api";
import { fmtMoney, fmtTime, fmtTrapped } from "../fmt";

/** The engine's plain-text report carries ISO stamps; show them the UI's way. */
const readableStamps = (text: string) =>
  text.replace(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?/g, (m) =>
    `${fmtTime(m)} UTC`,
  );

interface Props {
  facilities: FacilitySummary[];
  /** Replay stamp: a fork under a REPLAY header runs at that time, not live. */
  at: string | null;
  onClose: () => void;
}

export function WhatIfPanel({ facilities, at, onClose }: Props) {
  const [events, setEvents] = useState<string[]>([]);
  const [draft, setDraft] = useState("");
  const [result, setResult] = useState<WhatIfResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Scrubbing to a different time invalidates a result computed at the old one.
  useEffect(() => {
    setResult(null);
  }, [at]);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.whatif(events, at));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setResult(null);
    } finally {
      setBusy(false);
    }
  };

  const addDraft = () => {
    const text = draft.trim();
    if (text) {
      setEvents((current) => [...current, text]);
      setDraft("");
      setResult(null);
    }
  };

  // Everything every hypothetical closure would strand, across the reopts the
  // fork ran.
  const trapped = result ? result.reopts.flatMap((r) => r.trapped) : [];

  return (
    <aside className="overlay center">
      <div className="panel-header">
        <span className="micro">WHAT-IF · A FORK NEVER TOUCHES THE LOG</span>
        <button className="close" onClick={onClose}>
          CLOSE ✕
        </button>
      </div>
      <div className="field-row">
        <input
          style={{ flex: 1 }}
          placeholder="close FAC-3 14d · cut ZON-2 0.5 7d · delay SHP-9 24h · spike 5 12"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") addDraft();
          }}
        />
        <button onClick={addDraft}>ADD</button>
        {facilities.length > 0 && (
          <button
            onClick={() => {
              // The busiest hub: closing it is the hypothesis that moves things.
              const busiest = [...facilities].sort(
                (a, b) => b.utilization - a.utilization || a.id.localeCompare(b.id),
              )[0]!;
              setDraft(`close ${busiest.id} 7d`);
            }}
          >
            EXAMPLE
          </button>
        )}
      </div>
      {events.length > 0 && (
        <div className="field-row" style={{ flexWrap: "wrap" }}>
          {events.map((text, index) => (
            <button
              key={`${text}-${index}`}
              className="chip"
              onClick={() => {
                setEvents((current) => current.filter((_, i) => i !== index));
                setResult(null);
              }}
              title="remove"
            >
              {text} ×
            </button>
          ))}
          <button className="primary" onClick={() => void run()} disabled={busy}>
            {busy ? "FORKING…" : "RUN FORK"}
          </button>
        </div>
      )}
      <div className="panel-body">
        {error && <div className="reject-line reject-code">{error}</div>}
        {result && (
          <>
            {/* Incumbent bookings count as allocated at $0: only decisions
                the fork makes (or re-makes) carry transport cost. */}
            <div className="field-row mono whatif-totals">
              <span>
                BASELINE {result.baseline.allocated} allocated · {result.baseline.unassigned}{" "}
                unassigned · new-route cost {fmtMoney(result.baseline.cost_cents)}
              </span>
              <span style={{ color: "var(--accent)" }}>
                FORK {result.fork.allocated} allocated · {result.fork.unassigned} unassigned ·
                new-route cost {fmtMoney(result.fork.cost_cents)}
              </span>
              {/* Cargo the hypothesis would strand: it never re-routes, so it
                  never appears as a change — the fork totals are the only
                  place it can be seen. */}
              {trapped.length > 0 && (
                <span className="chip alarm" title={fmtTrapped(trapped)}>
                  {trapped.length} TRAPPED
                </span>
              )}
            </div>
            <table className="data">
              <thead>
                <tr>
                  <th>SHIPMENT</th>
                  <th>BASELINE</th>
                  <th>FORK</th>
                  <th>Δ COST</th>
                </tr>
              </thead>
              <tbody>
                {result.changes.map((change) => (
                  <tr key={change.shipment_id}>
                    <td>{change.shipment_id}</td>
                    <td>{change.before ? change.before.join("/") : "—"}</td>
                    <td style={{ color: change.after ? undefined : "var(--alert-light)" }}>
                      {change.after ? change.after.join("/") : "UNASSIGNED"}
                    </td>
                    <td>{fmtMoney(change.cost_delta_cents)}</td>
                  </tr>
                ))}
                {result.changes.length === 0 && (
                  <tr>
                    <td colSpan={4}>no decisions change under this hypothesis</td>
                  </tr>
                )}
              </tbody>
            </table>
            <pre className="rendered">{readableStamps(result.rendered)}</pre>
          </>
        )}
      </div>
    </aside>
  );
}
