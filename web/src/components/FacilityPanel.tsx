/* Facility popover: 2D zone/occupancy view with timelines + reservations,
   the disruption injector, and the controls for the disruptions already
   landing on this facility (end now / move the end). Dismissal goes through
   an explicit catch layer — never a window-level click-away listener. */

import { useEffect, useState } from "react";

import { api } from "../api";
import type { DisruptionSummary, FacilityDetail, FacilitySummary, NewZoneInput } from "../api";
import { fmtDay, fmtRelative, fmtTime } from "../fmt";

interface Props {
  facilityId: string;
  summary: FacilitySummary | null;
  /** Disruptions landing on this facility (facility- or zone-targeted). */
  disruptions: DisruptionSummary[];
  at: string | null;
  live: boolean;
  liveNow: string | null;
  /** A command is in flight: the disruption controls wait for it. */
  busy: boolean;
  onClose: () => void;
  onDisrupt: (kind: string, targetId: string, days: number, magnitude: number) => void;
  onEndDisruption: (disruptionId: string, until: string | null) => void;
  onAddZone: (body: NewZoneInput) => void;
  onSetZoneCapacity: (zoneId: string, slots: number) => void;
}

/** ISO → value for <input type="datetime-local"> (UTC wall clock). */
function toLocalInput(iso: string): string {
  return iso.slice(0, 16);
}

export function FacilityPanel({
  facilityId,
  summary,
  disruptions,
  at,
  live,
  liveNow,
  busy,
  onClose,
  onDisrupt,
  onEndDisruption,
  onAddZone,
  onSetZoneCapacity,
}: Props) {
  const [detail, setDetail] = useState<FacilityDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(3);
  // Held as the percentage the operator reads on the button, not the fraction
  // the command takes — the field and the label must not disagree.
  const [cutPercent, setCutPercent] = useState(50);
  const [newEnds, setNewEnds] = useState<Record<string, string>>({});
  // Per-zone resize drafts, and the new-zone form.
  const [resizes, setResizes] = useState<Record<string, string>>({});
  const [zoneKind, setZoneKind] = useState("rack");
  const [zoneSlots, setZoneSlots] = useState("60");
  const [zoneTempLo, setZoneTempLo] = useState("-25");
  const [zoneTempHi, setZoneTempHi] = useState("5");
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    // While a command is in flight the log is about to move: refetch when it
    // lands (busy → false), so a new zone or capacity shows immediately.
    if (busy) return;
    let cancelled = false;
    api
      .facility(facilityId, 14, at)
      .then((body) => {
        if (!cancelled) setDetail(body);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [facilityId, at, busy]);

  // A live closure shuts the facility whatever its standing `open` flag says.
  const closure = disruptions.find(
    (d) => d.status === "active" && d.kind === "facility_closed" && d.target_id === facilityId,
  );

  const submitZone = () => {
    const slots = Number(zoneSlots);
    if (!Number.isFinite(slots) || slots <= 0) {
      setProblem("a zone needs a positive slot capacity");
      return;
    }
    const cold = zoneKind === "cold";
    const lo = Number(zoneTempLo);
    const hi = Number(zoneTempHi);
    if (cold && (!Number.isFinite(lo) || !Number.isFinite(hi) || lo > hi)) {
      setProblem("a cold zone needs an ordered temperature band (low ≤ high)");
      return;
    }
    setProblem(null);
    onAddZone({
      kind: zoneKind,
      slots: Math.round(slots),
      temp_c: cold ? [Math.round(lo), Math.round(hi)] : null,
    });
  };

  return (
    <>
      {/* The catch layer is itself the dismiss control: it intercepts the
          click, so nothing underneath can also activate. */}
      <button className="catch-layer" aria-label="Dismiss" onClick={onClose} />
      <div className="popover">
        {/* Title and state STACK: the state line grows with what the world is
            doing (a closure names its disruption and when it lifts), and a
            status nobody can read to the end is worse than none. */}
        <div className="panel-header">
          <div className="panel-title">
            <span className="row-title" style={{ display: "block" }}>
              {facilityId}
              {summary ? ` · ${summary.name}` : ""}
            </span>
            {summary && (
              <span className="micro">
                {closure ? `CLOSED · ${closure.id} until ${fmtTime(closure.until_ts)}` : summary.open ? "OPEN" : "CLOSED"}{" "}
                · UTIL {(summary.utilization * 100).toFixed(0)}% ·{" "}
                {summary.zones.length} {summary.zones.length === 1 ? "ZONE" : "ZONES"}
              </span>
            )}
          </div>
          <button className="close" onClick={onClose}>
            CLOSE ✕
          </button>
        </div>
        <div className="panel-body">
          {error && <div className="reject-line reject-code">{error}</div>}
          {!detail && !error && <div className="reject-line row-sub mono">loading…</div>}

          {disruptions.length > 0 && (
            <div className="disruption-list">
              <div className="panel-header">
                <span className="micro">DISRUPTIONS ON THIS FACILITY</span>
                <span className="micro">{disruptions.length}</span>
              </div>
              {disruptions.map((disruption) => (
                <div key={disruption.id} className="reject-line">
                  <div className="row-title reject-title">
                    <span>
                      {disruption.kind.replace("_", " ").toUpperCase()} · {disruption.target_id}
                      {disruption.magnitude < 1
                        ? ` · CUT ${(disruption.magnitude * 100).toFixed(0)}%`
                        : ""}
                    </span>
                    <span className={`badge ${disruption.status === "active" ? "reject" : ""}`}>
                      {disruption.status.toUpperCase()}
                    </span>
                  </div>
                  <div className="row-line plain">
                    {disruption.id} · from {fmtTime(disruption.from_ts)} until{" "}
                    {fmtTime(disruption.until_ts)}{" "}
                    <span className="muted">{fmtRelative(disruption.until_ts, liveNow)}</span>
                    {disruption.detail ? ` · ${disruption.detail}` : ""}
                  </div>
                  {live && (
                    <div className="field-row" style={{ paddingLeft: 0 }}>
                      <button disabled={busy} onClick={() => onEndDisruption(disruption.id, null)}>
                        END NOW
                      </button>
                      <label className="micro" htmlFor={`end-${disruption.id}`}>
                        NEW END (UTC)
                      </label>
                      <input
                        id={`end-${disruption.id}`}
                        type="datetime-local"
                        value={newEnds[disruption.id] ?? toLocalInput(disruption.until_ts)}
                        onChange={(event) =>
                          setNewEnds((current) => ({
                            ...current,
                            [disruption.id]: event.target.value,
                          }))
                        }
                      />
                      <button
                        disabled={busy}
                        onClick={() => {
                          const value = newEnds[disruption.id] ?? toLocalInput(disruption.until_ts);
                          if (value) onEndDisruption(disruption.id, `${value}:00+00:00`);
                        }}
                        title="Ends this disruption now and reopens it until the new time"
                      >
                        SET END
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {detail?.zones.map((zone) => {
            const dim = "slots" as const;
            // The scale is TODAY's effective capacity, raised to the tallest
            // occupancy whenever the goods exceed it. Neither half is optional:
            // scaling by capacity alone flattened a cut facility's over-capacity
            // days into a 5px stripe (its capacity is back tenfold next week),
            // and scaling by occupancy alone drew a zone at 20% as a full bar.
            // Capacity then rides on top as a rule per day — clamped when it is
            // off the top — so "the bar stands above the line" is what being
            // over capacity looks like, whichever way the day went.
            const peak = Math.max(
              1,
              zone.buckets[0]?.capacity[dim] ?? 0,
              ...zone.buckets.map((b) => b.occupancy[dim]),
            );
            return (
              <div key={zone.id} className="zone-block">
                <div className="row-title reject-title">
                  <span>
                    {zone.id} <span className="micro">· {zone.kind}</span>
                  </span>
                  <span className="mono">
                    {zone.buckets[0]?.occupancy[dim] ?? 0}/{zone.buckets[0]?.capacity[dim] ?? "∞"}{" "}
                    slots today
                  </span>
                </div>
                <div className="timeline" title="daily occupancy against effective capacity, 14 days">
                  {zone.buckets.map((bucket) => {
                    const cap = bucket.capacity[dim];
                    const occ = bucket.occupancy[dim];
                    const over = cap !== null && occ > cap;
                    // Percentages, never pixels: the chart is sized in rem, so
                    // a pixel height would shrink away from its own box at
                    // every interface scale but 100%.
                    const pct = (value: number) => `${Math.min(100, (value / peak) * 100)}%`;
                    return (
                      <div
                        key={bucket.day}
                        className="bar-slot"
                        title={`${fmtDay(bucket.day)}: ${occ}/${cap ?? "∞"} slots`}
                      >
                        {/* Only a capacity that FITS the chart gets a rule: one
                            clamped to the top would sit on a bar that is
                            nowhere near it and read as "at capacity". A day
                            with room to spare is better left unmarked. */}
                        {cap !== null && cap <= peak && (
                          <span className="cap-mark" style={{ bottom: pct(cap) }} />
                        )}
                        <span className={over ? "bar over" : "bar"} style={{ height: pct(occ) }} />
                      </div>
                    );
                  })}
                </div>
                {zone.reservations.length > 0 && (
                  /* Why the space is taken, not just that it is: a HOLD is the
                     customer's storage, a pass-through dwell (§7.9) is a truck
                     being unloaded and reloaded. An operator looking at a busy
                     dock has to be able to tell them apart. */
                  <div className="reservations">
                    <div className="micro">RESERVATIONS</div>
                    {zone.reservations.map((r) => (
                      <div
                        key={r.id}
                        className={r.role === "hold" ? "reservation" : "reservation staging"}
                      >
                        <span className="reservation-role">{r.role}</span>
                        <span>
                          {r.holder} · {r.size.slots} slots · {fmtTime(r.from_ts)} →{" "}
                          {fmtTime(r.until_ts)}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                {zone.lots.length > 0 && (
                  <div className="reject-data">
                    lots:{" "}
                    {zone.lots
                      .map(
                        (lot) =>
                          `${lot.id} × ${lot.quantity}${lot.compat_class ? ` [${lot.compat_class}]` : ""}`,
                      )
                      .join(", ")}
                  </div>
                )}
                {live && (
                  <div className="field-row" style={{ paddingLeft: 0 }}>
                    <button disabled={busy} onClick={() => onDisrupt("zone_offline", zone.id, days, 1.0)}>
                      ZONE OFFLINE
                    </button>
                    <button
                      disabled={busy}
                      onClick={() => onDisrupt("capacity_reduced", zone.id, days, cutPercent / 100)}
                    >
                      CUT {cutPercent}%
                    </button>
                    <input
                      value={resizes[zone.id] ?? ""}
                      placeholder="slots"
                      style={{ width: "4.5rem" }}
                      onChange={(event) =>
                        setResizes((current) => ({ ...current, [zone.id]: event.target.value }))
                      }
                    />
                    <button
                      disabled={busy}
                      onClick={() => {
                        const slots = Number(resizes[zone.id]);
                        if (!Number.isFinite(slots) || slots <= 0 || (resizes[zone.id] ?? "").trim() === "") {
                          setProblem("resize needs a positive slot count");
                          return;
                        }
                        setProblem(null);
                        onSetZoneCapacity(zone.id, Math.round(slots));
                      }}
                      title="Set this zone's base slot capacity (disruption cuts still apply)"
                    >
                      SET CAPACITY
                    </button>
                  </div>
                )}
              </div>
            );
          })}

          {live && detail && (
            <div className="field-row" style={{ paddingLeft: 0 }}>
              <label className="micro">ADD ZONE</label>
              <select value={zoneKind} onChange={(event) => setZoneKind(event.target.value)}>
                {["rack", "cold", "bulk", "yard", "tank"].map((kind) => (
                  <option key={kind} value={kind}>
                    {kind}
                  </option>
                ))}
              </select>
              <input
                value={zoneSlots}
                style={{ width: "4.5rem" }}
                onChange={(event) => setZoneSlots(event.target.value)}
              />
              <span className="micro">SLOTS</span>
              {zoneKind === "cold" && (
                <>
                  <input
                    value={zoneTempLo}
                    style={{ width: "4rem" }}
                    onChange={(event) => setZoneTempLo(event.target.value)}
                  />
                  <span className="micro">…</span>
                  <input
                    value={zoneTempHi}
                    style={{ width: "4rem" }}
                    onChange={(event) => setZoneTempHi(event.target.value)}
                  />
                  <span className="micro">°C</span>
                </>
              )}
              <button disabled={busy} onClick={submitZone}>
                ADD
              </button>
            </div>
          )}
          {problem && <div className="reject-line reject-code">{problem}</div>}
        </div>
        {/* A facility that is already shut has nothing to close. The two things
            an operator can actually do to a standing closure — END NOW and SET
            END — are on its disruption above, so the footer says where they
            are instead of offering a second closure that changes nothing. */}
        {live && closure && (
          <div className="panel-footer">
            <span className="micro">
              ALREADY CLOSED BY {closure.id} · END NOW OR SET END ON ITS ROW ABOVE
            </span>
          </div>
        )}
        {live && !closure && (
          <div className="panel-footer" style={{ alignItems: "center" }}>
            <label className="micro" htmlFor="disrupt-days">
              DAYS
            </label>
            <input
              id="disrupt-days"
              type="number"
              min={1}
              max={30}
              value={days}
              style={{ width: "4rem" }}
              onChange={(event) => setDays(Math.max(1, Number(event.target.value) || 1))}
            />
            <label className="micro" htmlFor="cut-percent">
              CUT %
            </label>
            <input
              id="cut-percent"
              type="number"
              min={10}
              max={100}
              step={5}
              value={cutPercent}
              style={{ width: "4.5rem" }}
              onChange={(event) =>
                setCutPercent(Math.min(100, Math.max(10, Math.round(Number(event.target.value) || 50))))
              }
            />
            <button
              className="primary"
              disabled={busy}
              onClick={() => onDisrupt("facility_closed", facilityId, days, 1.0)}
            >
              CLOSE FACILITY FOR {days} {days === 1 ? "DAY" : "DAYS"}
            </button>
          </div>
        )}
      </div>
    </>
  );
}
