/* Replay scrubber: state_at(t) across the log's real span — first event to
   live now — so every slider position lands on history that exists. The
   map above re-renders from the historical fold; LIVE returns to the head. */

import { useMemo } from "react";

import { fmtTime } from "../fmt";

interface Props {
  liveNow: string | null;
  /** Timestamp of the log's first event; null until the head probe learns it. */
  logStart: string | null;
  replayAt: string | null;
  onScrub: (at: string | null) => void;
}

const STEPS = 200;
const MIN_SPAN_MS = 60 * 60 * 1000; // a one-event log still gets a usable bar

export function ReplayBar({ liveNow, logStart, replayAt, onScrub }: Props) {
  const end = useMemo(() => (liveNow ? new Date(liveNow).getTime() : null), [liveNow]);
  const start = useMemo(() => {
    if (end === null) return null;
    const first = logStart ? new Date(logStart).getTime() : end - MIN_SPAN_MS;
    return Math.min(first, end - MIN_SPAN_MS);
  }, [end, logStart]);
  if (end === null || start === null) return null;
  const current = replayAt ? new Date(replayAt).getTime() : end;
  const step = Math.round(((current - start) / (end - start)) * STEPS);
  const stamp = (ms: number) => fmtTime(new Date(ms).toISOString());

  return (
    <div className="overlay bottom-bar">
      <span className="micro">REPLAY</span>
      <span className="row-line plain">{stamp(start)}</span>
      <input
        type="range"
        min={0}
        max={STEPS}
        value={Math.max(0, Math.min(STEPS, step))}
        style={{ flex: 1 }}
        onChange={(event) => {
          const fraction = Number(event.target.value) / STEPS;
          const ts = new Date(start + fraction * (end - start));
          if (Number(event.target.value) >= STEPS) onScrub(null);
          else onScrub(ts.toISOString());
        }}
      />
      <span className="row-line plain replay-stamp">
        {replayAt ? `${fmtTime(replayAt)} UTC` : `${stamp(end)} UTC · LIVE`}
      </span>
      <button onClick={() => onScrub(null)} disabled={replayAt === null}>
        LIVE
      </button>
    </div>
  );
}
