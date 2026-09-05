/* Network operations: register a facility (with zones) or a lane. Everything
   is an event — there is no removal; a facility that should stop taking work
   gets closed with a disruption. Coordinates can be picked off the map. */

import { useState } from "react";

import type { FacilitySummary, NewFacility, NewLane, NewZoneInput } from "../api";

interface Props {
  facilities: FacilitySummary[];
  busy: boolean;
  /** Armed pick: the next map click fills the coordinate fields. */
  picking: boolean;
  pickedCoords: { lat: number; lon: number } | null;
  onPickCoords: (arm: boolean) => void;
  onCreateFacility: (body: NewFacility) => void;
  onCreateLane: (body: NewLane) => void;
  onClose: () => void;
}

interface ZoneDraft {
  kind: string;
  slots: string;
  tempLo: string;
  tempHi: string;
}

const ZONE_KINDS = ["rack", "cold", "bulk", "yard", "tank"];

export function NetworkPanel({
  facilities,
  busy,
  picking,
  pickedCoords,
  onPickCoords,
  onCreateFacility,
  onCreateLane,
  onClose,
}: Props) {
  const [name, setName] = useState("");
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [coldCert, setColdCert] = useState(false);
  const [hazmatCert, setHazmatCert] = useState(false);
  const [zones, setZones] = useState<ZoneDraft[]>([
    { kind: "rack", slots: "120", tempLo: "-25", tempHi: "5" },
  ]);
  const [laneFrom, setLaneFrom] = useState(facilities[0]?.id ?? "");
  const [laneTo, setLaneTo] = useState(facilities[1]?.id ?? "");
  const [laneMode, setLaneMode] = useState("road");
  const [bothWays, setBothWays] = useState(true);
  const [problem, setProblem] = useState<string | null>(null);

  // A map pick landed since the last render: adopt it once.
  const [consumedPick, setConsumedPick] = useState<{ lat: number; lon: number } | null>(null);
  if (pickedCoords && pickedCoords !== consumedPick) {
    setConsumedPick(pickedCoords);
    setLat(pickedCoords.lat.toFixed(3));
    setLon(pickedCoords.lon.toFixed(3));
  }

  const submitFacility = () => {
    const latNum = lat.trim() === "" ? NaN : Number(lat);
    const lonNum = lon.trim() === "" ? NaN : Number(lon);
    if (!Number.isFinite(latNum) || latNum < -90 || latNum > 90) {
      setProblem("latitude must be a number between −90 and 90 (or PICK ON MAP)");
      return;
    }
    if (!Number.isFinite(lonNum) || lonNum < -180 || lonNum > 180) {
      setProblem("longitude must be a number between −180 and 180 (or PICK ON MAP)");
      return;
    }
    const zoneBodies: NewZoneInput[] = [];
    for (const zone of zones) {
      const slots = Number(zone.slots);
      if (!Number.isFinite(slots) || slots <= 0) {
        setProblem("every zone needs a positive slot capacity");
        return;
      }
      const cold = zone.kind === "cold";
      const lo = Number(zone.tempLo);
      const hi = Number(zone.tempHi);
      if (cold && (!Number.isFinite(lo) || !Number.isFinite(hi) || lo > hi)) {
        setProblem("a cold zone needs an ordered temperature band (low ≤ high)");
        return;
      }
      zoneBodies.push({
        kind: zone.kind,
        slots: Math.round(slots),
        temp_c: cold ? [Math.round(lo), Math.round(hi)] : null,
      });
    }
    setProblem(null);
    onCreateFacility({
      name: name.trim() || null,
      lat: latNum,
      lon: lonNum,
      cold_certified: coldCert || zoneBodies.some((z) => z.kind === "cold"),
      hazmat_certified: hazmatCert,
      zones: zoneBodies,
    });
  };

  const submitLane = () => {
    if (laneFrom === laneTo) {
      setProblem("a lane needs two distinct facilities");
      return;
    }
    setProblem(null);
    onCreateLane({
      from_facility_id: laneFrom,
      to_facility_id: laneTo,
      mode: laneMode,
      both_directions: bothWays,
    });
  };

  // While a pick is armed the panel hides (not unmounts — the form keeps its
  // state) so the whole map is clickable underneath.
  return (
    <aside className={picking ? "overlay center network-panel pick-hidden" : "overlay center network-panel"}>
      <div className="panel-header">
        <span className="micro">NETWORK · REGISTER FACILITIES AND LANES</span>
        <button className="close" onClick={onClose}>
          CLOSE ✕
        </button>
      </div>
      <div className="panel-body">
        <div className="panel-header">
          <span className="micro">NEW FACILITY</span>
          <span className="micro">no removal — closure is a disruption</span>
        </div>
        <div className="field-row">
          <label>NAME</label>
          <input
            placeholder="e.g. Calgary Foothills"
            value={name}
            style={{ flex: 1 }}
            onChange={(event) => setName(event.target.value)}
          />
        </div>
        <div className="field-row">
          <label>WHERE</label>
          <input
            placeholder="lat"
            value={lat}
            style={{ width: "6rem" }}
            onChange={(event) => setLat(event.target.value)}
          />
          <input
            placeholder="lon"
            value={lon}
            style={{ width: "6rem" }}
            onChange={(event) => setLon(event.target.value)}
          />
          <button
            className={picking ? "danger" : ""}
            disabled={busy}
            onClick={() => onPickCoords(!picking)}
            title="Then click the map where the facility sits"
          >
            {picking ? "CLICK THE MAP…" : "PICK ON MAP"}
          </button>
          <label className="check">
            <input
              type="checkbox"
              checked={coldCert}
              onChange={(event) => setColdCert(event.target.checked)}
            />
            cold certified
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={hazmatCert}
              onChange={(event) => setHazmatCert(event.target.checked)}
            />
            hazmat certified
          </label>
        </div>
        {zones.map((zone, index) => (
          <div className="field-row" key={index}>
            <label>{index === 0 ? "ZONES" : ""}</label>
            <select
              value={zone.kind}
              onChange={(event) =>
                setZones((current) =>
                  current.map((z, i) => (i === index ? { ...z, kind: event.target.value } : z)),
                )
              }
            >
              {ZONE_KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {kind}
                </option>
              ))}
            </select>
            <input
              value={zone.slots}
              style={{ width: "5rem" }}
              onChange={(event) =>
                setZones((current) =>
                  current.map((z, i) => (i === index ? { ...z, slots: event.target.value } : z)),
                )
              }
            />
            <span className="micro">SLOTS</span>
            {zone.kind === "cold" && (
              <>
                <input
                  value={zone.tempLo}
                  style={{ width: "4rem" }}
                  onChange={(event) =>
                    setZones((current) =>
                      current.map((z, i) =>
                        i === index ? { ...z, tempLo: event.target.value } : z,
                      ),
                    )
                  }
                />
                <span className="micro">…</span>
                <input
                  value={zone.tempHi}
                  style={{ width: "4rem" }}
                  onChange={(event) =>
                    setZones((current) =>
                      current.map((z, i) =>
                        i === index ? { ...z, tempHi: event.target.value } : z,
                      ),
                    )
                  }
                />
                <span className="micro">°C</span>
              </>
            )}
            {zones.length > 1 && (
              <button
                onClick={() => setZones((current) => current.filter((_, i) => i !== index))}
                title="Remove this zone row"
              >
                ✕
              </button>
            )}
          </div>
        ))}
        <div className="field-row">
          <label></label>
          <button
            onClick={() =>
              setZones((current) => [
                ...current,
                { kind: "rack", slots: "60", tempLo: "-25", tempHi: "5" },
              ])
            }
          >
            + ZONE
          </button>
          <button className="primary" disabled={busy} onClick={submitFacility}>
            REGISTER FACILITY
          </button>
        </div>

        <div className="panel-header">
          <span className="micro">NEW LANE</span>
          <span className="micro">distance and time default to the mode's speed</span>
        </div>
        <div className="field-row">
          <label>ROUTE</label>
          <select
            aria-label="lane origin"
            value={laneFrom}
            style={{ flex: "1 1 10rem", minWidth: 0 }}
            onChange={(event) => setLaneFrom(event.target.value)}
          >
            {facilities.map((facility) => (
              <option key={facility.id} value={facility.id}>
                {facility.id} · {facility.name}
              </option>
            ))}
          </select>
          <span className="micro">→</span>
          <select
            aria-label="lane destination"
            value={laneTo}
            style={{ flex: "1 1 10rem", minWidth: 0 }}
            onChange={(event) => setLaneTo(event.target.value)}
          >
            {facilities.map((facility) => (
              <option key={facility.id} value={facility.id}>
                {facility.id} · {facility.name}
              </option>
            ))}
          </select>
          <select
            aria-label="lane mode"
            value={laneMode}
            onChange={(event) => setLaneMode(event.target.value)}
          >
            <option value="road">road</option>
            <option value="sea">sea</option>
            <option value="air">air</option>
          </select>
        </div>
        {/* The lane's action row matches the facility's: label column empty,
            controls under the fields. */}
        <div className="field-row">
          <label />
          <label className="check">
            <input
              type="checkbox"
              checked={bothWays}
              onChange={(event) => setBothWays(event.target.checked)}
            />
            both directions
          </label>
          <button className="primary" disabled={busy} onClick={submitLane}>
            REGISTER LANE
          </button>
        </div>
        {problem && <div className="reject-line reject-code">{problem}</div>}
      </div>
    </aside>
  );
}
