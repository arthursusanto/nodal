/* New-shipment form: registers a shipment into the log; it joins the queue as
   PLANNED. Modal popover with catch-layer dismissal.

   Two shapes of shipment come out of this one form (§7.9): leave DESTINATION
   blank and the optimizer picks a storage facility; name a customer point and
   it becomes an A->B delivery — first mile, hold, last mile. Both ends can be
   found by business name when a maps provider is configured. */

import { useState } from "react";

import { api } from "../api";
import type { FacilitySummary, NewShipment, PlaceResult } from "../api";

/** Business search for one end of the shipment. The provider's absence is
    reported as such (once learned, the parent hides both searches) rather
    than leaving a control that answers nothing. */
function PlaceSearch({
  what,
  busy,
  onOff,
  onPick,
}: {
  what: string;
  busy: boolean;
  onOff: () => void;
  onPick: (place: PlaceResult) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<PlaceResult[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);

  const run = async () => {
    const text = query.trim();
    if (text === "" || searching) return;
    setSearching(true);
    setNote(null);
    try {
      const body = await api.placesSearch(text);
      if (!body.enabled) {
        setResults([]);
        onOff();
        return;
      }
      setResults(body.places);
      setNote(body.places.length === 0 ? "nothing matched" : null);
    } catch (err) {
      setResults([]);
      setNote(err instanceof Error ? err.message : String(err));
    } finally {
      setSearching(false);
    }
  };

  return (
    <>
      <input
        placeholder={`find the ${what} by name`}
        value={query}
        style={{ flex: "1 1 12rem" }}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key !== "Enter") return;
          event.preventDefault();
          void run();
        }}
      />
      <button disabled={busy || searching || query.trim() === ""} onClick={() => void run()}>
        {searching ? "SEARCHING…" : "SEARCH"}
      </button>
      {note && <span className="micro">{note}</span>}
      {results.length > 0 && (
        <div className="place-results">
          {results.map((place) => (
            <button
              key={`${place.lat},${place.lon},${place.name}`}
              className="place-hit"
              onClick={() => {
                onPick(place);
                setResults([]);
                setNote(null);
              }}
            >
              <span>{place.name || place.address}</span>
              <span className="micro">{place.address}</span>
            </button>
          ))}
        </div>
      )}
    </>
  );
}

interface Props {
  facilities: FacilitySummary[];
  /** The live head; readiness defaults to it. */
  liveNow: string | null;
  busy: boolean;
  onCreate: (body: NewShipment) => void;
  onClose: () => void;
}

/** A picked coordinate, at metre precision — enough for a loading dock, and
    short enough to read in the field. */
const coord = (value: number) => String(Number(value.toFixed(5)));

const toInput = (iso: string | null) => (iso ? iso.slice(0, 16) : "");
const fromInput = (value: string) => (value ? `${value}:00+00:00` : null);

export function NewShipmentPanel({ facilities, liveNow, busy, onCreate, onClose }: Props) {
  const [originMode, setOriginMode] = useState<"facility" | "coords">("facility");
  const [facilityId, setFacilityId] = useState(facilities[0]?.id ?? "");
  const [label, setLabel] = useState("");
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [sku, setSku] = useState("CARGO-1");
  const [group, setGroup] = useState("general");
  const [quantity, setQuantity] = useState(10);
  const [uom, setUom] = useState("unit");
  const [slots, setSlots] = useState(10);
  const [weightKg, setWeightKg] = useState(4500);
  const [cold, setCold] = useState(false);
  const [tempLo, setTempLo] = useState(-5);
  const [tempHi, setTempHi] = useState(4);
  const [compatClass, setCompatClass] = useState("");
  const [ready, setReady] = useState(toInput(liveNow));
  const [deadline, setDeadline] = useState("");
  const [dwellDays, setDwellDays] = useState(5);
  const [destLabel, setDestLabel] = useState("");
  const [destLat, setDestLat] = useState("");
  const [destLon, setDestLon] = useState("");
  const [holdDays, setHoldDays] = useState(0);
  // A search answered "no provider configured": say so instead of offering a
  // control that can only ever come back empty.
  const [searchOff, setSearchOff] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const destFields = [destLabel.trim(), destLat.trim(), destLon.trim()];
  const destGiven = destFields.filter((value) => value !== "").length;
  const isDelivery = destGiven > 0;

  const submit = () => {
    if (originMode === "coords") {
      // Number("") is 0 — an empty field must never silently become 0°N 0°E.
      const latNum = lat.trim() === "" ? NaN : Number(lat);
      const lonNum = lon.trim() === "" ? NaN : Number(lon);
      if (!Number.isFinite(latNum) || latNum < -90 || latNum > 90) {
        setProblem("latitude must be a number between −90 and 90");
        return;
      }
      if (!Number.isFinite(lonNum) || lonNum < -180 || lonNum > 180) {
        setProblem("longitude must be a number between −180 and 180");
        return;
      }
    }
    if (slots <= 0 || quantity <= 0) {
      setProblem("quantity and slots must be positive");
      return;
    }
    if (cold && tempLo > tempHi) {
      setProblem("the cold band's low bound must not exceed its high bound");
      return;
    }
    // The server's rule, mirrored: a half-specified customer point could never
    // be routed to, so it is all three fields or none.
    if (destGiven > 0 && destGiven < 3) {
      setProblem(
        "a destination needs a label, a latitude and a longitude — " +
          "or leave all three blank for an ordinary allocation",
      );
      return;
    }
    const destLatNum = Number(destLat);
    const destLonNum = Number(destLon);
    if (isDelivery) {
      if (!Number.isFinite(destLatNum) || destLatNum < -90 || destLatNum > 90) {
        setProblem("the destination latitude must be a number between −90 and 90");
        return;
      }
      if (!Number.isFinite(destLonNum) || destLonNum < -180 || destLonNum > 180) {
        setProblem("the destination longitude must be a number between −180 and 180");
        return;
      }
      if (!Number.isInteger(holdDays) || holdDays < 0 || holdDays > 365) {
        setProblem("the hold must be a whole number of days between 0 and 365");
        return;
      }
    }
    setProblem(null);
    onCreate({
      origin_facility_id: originMode === "facility" ? facilityId : null,
      origin_label: originMode === "coords" ? label.trim() || "origin" : null,
      origin_lat: originMode === "coords" ? Number(lat) : null,
      origin_lon: originMode === "coords" ? Number(lon) : null,
      sku: sku.trim() || "SKU",
      group: group.trim() || "general",
      quantity: Math.round(quantity),
      uom: uom.trim() || "unit",
      slots: Math.round(slots),
      weight_kg: weightKg > 0 ? weightKg : null,
      temp_c: cold ? [tempLo, tempHi] : null,
      compat_class: compatClass.trim() || null,
      ready: fromInput(ready),
      deadline: fromInput(deadline),
      // With a customer point the hold replaces the dwell: only one of them
      // describes how long the goods stay.
      dwell_days: isDelivery ? null : dwellDays > 0 ? Math.round(dwellDays) : null,
      destination_label: isDelivery ? destLabel.trim() : null,
      destination_lat: isDelivery ? destLatNum : null,
      destination_lon: isDelivery ? destLonNum : null,
      hold_days: isDelivery ? Math.round(holdDays) : 0,
    });
  };

  return (
    <>
      <button className="catch-layer" aria-label="Dismiss" onClick={onClose} />
      <div className="popover">
        <div className="panel-header">
          <span className="row-title">NEW SHIPMENT</span>
          <button className="close" onClick={onClose}>
            CLOSE ✕
          </button>
        </div>
        <div className="panel-body">
          {/* Both ends read the same way: a search row, then the label and the
              coordinates it fills in. */}
          <div className="field-row">
            <label>FROM</label>
            <select
              aria-label="origin kind"
              value={originMode}
              onChange={(event) => setOriginMode(event.target.value as "facility" | "coords")}
            >
              <option value="facility">a facility</option>
              <option value="coords">a place</option>
            </select>
            {originMode === "facility" && (
              <select
                aria-label="origin facility"
                value={facilityId}
                style={{ flex: "1 1 10rem", minWidth: 0 }}
                onChange={(event) => setFacilityId(event.target.value)}
              >
                {facilities.map((facility) => (
                  <option key={facility.id} value={facility.id}>
                    {facility.id} · {facility.name}
                  </option>
                ))}
              </select>
            )}
          </div>
          {originMode === "coords" && (
            <>
              <div className="field-row">
                <label>SEARCH</label>
                {searchOff ? (
                  <span className="micro">
                    place search unavailable — no maps provider is configured; enter coordinates
                  </span>
                ) : (
                  <PlaceSearch
                    what="origin"
                    busy={busy}
                    onOff={() => setSearchOff(true)}
                    onPick={(place) => {
                      setLabel(place.name || place.address);
                      setLat(coord(place.lat));
                      setLon(coord(place.lon));
                    }}
                  />
                )}
              </div>
              <div className="field-row">
                <label />
                <input
                  aria-label="origin label"
                  placeholder="origin label"
                  value={label}
                  style={{ width: "10rem" }}
                  onChange={(event) => setLabel(event.target.value)}
                />
                <input
                  aria-label="origin latitude"
                  placeholder="lat"
                  value={lat}
                  style={{ width: "5.5rem" }}
                  onChange={(event) => setLat(event.target.value)}
                />
                <input
                  aria-label="origin longitude"
                  placeholder="lon"
                  value={lon}
                  style={{ width: "5.5rem" }}
                  onChange={(event) => setLon(event.target.value)}
                />
              </div>
            </>
          )}

          <div className="field-row">
            <label>TO</label>
            {searchOff ? (
              <span className="micro">
                place search unavailable — no maps provider is configured; enter coordinates
              </span>
            ) : (
              <PlaceSearch
                what="customer"
                busy={busy}
                onOff={() => setSearchOff(true)}
                onPick={(place) => {
                  setDestLabel(place.name || place.address);
                  setDestLat(coord(place.lat));
                  setDestLon(coord(place.lon));
                }}
              />
            )}
          </div>
          <div className="field-row">
            <label />
            <input
              aria-label="customer label"
              placeholder="customer label"
              value={destLabel}
              style={{ width: "10rem" }}
              onChange={(event) => setDestLabel(event.target.value)}
            />
            <input
              aria-label="customer latitude"
              placeholder="lat"
              value={destLat}
              style={{ width: "5.5rem" }}
              onChange={(event) => setDestLat(event.target.value)}
            />
            <input
              aria-label="customer longitude"
              placeholder="lon"
              value={destLon}
              style={{ width: "5.5rem" }}
              onChange={(event) => setDestLon(event.target.value)}
            />
            <span className="micro field-hint">
              {isDelivery
                ? "A→B DELIVERY: FIRST MILE, HOLD, LAST MILE"
                : "BLANK — THE OPTIMIZER CHOOSES A STORAGE FACILITY"}
            </span>
          </div>
          <div className="field-row">
            <label>CARGO</label>
            <input
              aria-label="quantity"
              title="quantity"
              value={quantity}
              type="number"
              min={1}
              style={{ width: "5rem" }}
              onChange={(event) => setQuantity(Math.max(1, Number(event.target.value) || 1))}
            />
            <input
              aria-label="unit of measure"
              title="unit of measure"
              value={uom}
              style={{ width: "5rem" }}
              onChange={(event) => setUom(event.target.value)}
            />
            <input
              aria-label="SKU"
              title="SKU"
              value={sku}
              style={{ flex: "1 1 8rem", minWidth: 0 }}
              onChange={(event) => setSku(event.target.value)}
            />
            <input
              aria-label="commodity group"
              value={group}
              style={{ width: "7rem" }}
              title="commodity group (demand forecasting)"
              onChange={(event) => setGroup(event.target.value)}
            />
          </div>
          <div className="field-row">
            <label>SIZE</label>
            <input
              aria-label="slots"
              value={slots}
              type="number"
              min={1}
              style={{ width: "5rem" }}
              onChange={(event) => setSlots(Math.max(1, Number(event.target.value) || 1))}
            />
            <span className="micro">SLOTS</span>
            <input
              aria-label="weight in kg"
              value={weightKg}
              type="number"
              min={0}
              style={{ width: "6rem" }}
              onChange={(event) => setWeightKg(Math.max(0, Number(event.target.value) || 0))}
            />
            <span className="micro">KG</span>
          </div>
          <div className="field-row">
            <label>NEEDS</label>
            <label className="check">
              <input type="checkbox" checked={cold} onChange={(event) => setCold(event.target.checked)} />
              cold
            </label>
            {cold && (
              <>
                <input
                  value={tempLo}
                  type="number"
                  style={{ width: "4.5rem" }}
                  onChange={(event) => setTempLo(Number(event.target.value) || 0)}
                />
                <span className="micro">…</span>
                <input
                  value={tempHi}
                  type="number"
                  style={{ width: "4.5rem" }}
                  onChange={(event) => setTempHi(Number(event.target.value) || 0)}
                />
                <span className="micro">°C</span>
              </>
            )}
            <input
              placeholder="hazard class (optional)"
              value={compatClass}
              style={{ width: "12rem" }}
              title="e.g. flammable, oxidizer, corrosive-acid"
              onChange={(event) => setCompatClass(event.target.value)}
            />
          </div>
          <div className="field-row">
            <label>READY (UTC)</label>
            <input
              type="datetime-local"
              value={ready}
              onChange={(event) => setReady(event.target.value)}
            />
            <label>{isDelivery ? "DELIVER BY (UTC)" : "DUE (UTC)"}</label>
            <input
              type="datetime-local"
              value={deadline}
              onChange={(event) => setDeadline(event.target.value)}
            />
          </div>
          {isDelivery ? (
            <div className="field-row">
              <label>HOLD</label>
              <input
                aria-label="hold days"
                value={holdDays}
                type="number"
                min={0}
                max={365}
                style={{ width: "5rem" }}
                onChange={(event) =>
                  setHoldDays(Math.min(365, Math.max(0, Number(event.target.value) || 0)))
                }
              />
              <span className="micro">DAYS HELD FOR THE CUSTOMER BEFORE THE LAST MILE</span>
            </div>
          ) : (
            <div className="field-row">
              <label>DWELL</label>
              <input
                aria-label="dwell days"
                value={dwellDays}
                type="number"
                min={0}
                style={{ width: "5rem" }}
                onChange={(event) => setDwellDays(Math.max(0, Number(event.target.value) || 0))}
              />
              <span className="micro">DAYS OF STORAGE AT THE DESTINATION</span>
            </div>
          )}
          {problem && <div className="reject-line reject-code">{problem}</div>}
        </div>
        <div className="panel-footer">
          <button onClick={onClose} disabled={busy}>
            CANCEL
          </button>
          <button className="primary" onClick={submit} disabled={busy}>
            REGISTER SHIPMENT
          </button>
        </div>
      </div>
    </>
  );
}
