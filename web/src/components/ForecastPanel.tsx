/* Forecast-inventory view (§7.7 read model): projected stock vs cover target
   per (facility, group), rendered as inline SVG series. */

import { useEffect, useState } from "react";

import { api } from "../api";
import type { ForecastData } from "../api";

function Spark({ series, target }: { series: { projected: number }[]; target: number }) {
  const width = 260;
  const height = 44;
  const peak = Math.max(target * 1.2, ...series.map((p) => p.projected), 1);
  const points = series
    .map((point, index) => {
      const x = (index / Math.max(series.length - 1, 1)) * width;
      const y = height - (point.projected / peak) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const targetY = height - (target / peak) * height;
  return (
    <svg width={width} height={height} role="img" aria-label="projected stock">
      <line x1={0} y1={targetY} x2={width} y2={targetY} stroke="#7dd87d" strokeDasharray="3 3" />
      <polyline points={points} fill="none" stroke="#ff7a1a" strokeWidth={1.5} />
    </svg>
  );
}

export function ForecastPanel({ at, onClose }: { at: string | null; onClose: () => void }) {
  const [data, setData] = useState<ForecastData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .forecast(14, at)
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

  return (
    <aside className="overlay center">
      <div className="panel-header">
        <span className="micro">FORECAST INVENTORY · 14 DAYS · TARGET = COVER × RATE</span>
        <button className="close" onClick={onClose}>
          CLOSE ✕
        </button>
      </div>
      <div className="panel-body">
        {error && <div className="reject-line reject-code">{error}</div>}
        {data?.facilities.length === 0 && (
          <div className="reject-line row-sub">No demand rates registered in this world.</div>
        )}
        {data?.facilities.map((facility) => (
          <div key={facility.facility_id} className="zone-block">
            <div className="row-title">{facility.facility_id}</div>
            {facility.groups.map((group) => (
              <div key={group.group} className="field-row" style={{ paddingLeft: 0 }}>
                <span className="row-line plain" style={{ minWidth: "8rem" }}>
                  {group.group} · {group.rate} per day
                </span>
                <Spark series={group.series} target={group.target} />
                <span className="row-line plain">
                  {group.current} now / {group.target} target
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </aside>
  );
}
