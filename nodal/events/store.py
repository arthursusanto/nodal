"""Append-only SQLite event store with snapshots (§4).

One SQLite database per world. `seq` is the authoritative total order; appends
validate that `ts` is non-decreasing in `seq`. Timestamps are stored as integer
microseconds since the Unix epoch (UTC), so range queries are exact.
"""

import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

from nodal.domain.calendar import require_aware
from nodal.events.catalog import PAYLOAD_REGISTRY
from nodal.events.envelope import Envelope, EventDraft

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY,
    id          TEXT NOT NULL UNIQUE,
    ts_us       INTEGER NOT NULL,
    type        TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    cause       TEXT,
    actor       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts_us);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events (entity_type, entity_id);
CREATE TABLE IF NOT EXISTS snapshots (
    upto_seq INTEGER PRIMARY KEY,
    ts_us    INTEGER NOT NULL,
    state    BLOB NOT NULL
);
"""


class StoreError(Exception):
    pass


def _to_us(ts: datetime) -> int:
    return round(require_aware(ts).astimezone(UTC).timestamp() * 1_000_000)


def _from_us(us: int) -> datetime:
    return datetime.fromtimestamp(us / 1_000_000, tz=UTC)


class EventStore:
    def __init__(self, path: str | Path, cross_thread: bool = False) -> None:
        """`cross_thread=True` relaxes sqlite's same-thread check for callers
        that serialize access themselves (the API server's request threadpool,
        §11); engine and CLI use the safe default."""
        self.path = path  # ":memory:" is honored (tests); files are the normal case
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=not cross_thread)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- append -----------------------------------------------------------------

    def append(self, drafts: Sequence[EventDraft], actor: str = "cli") -> list[Envelope]:
        """Append a batch atomically, assigning sequence numbers and ids."""
        if not drafts:
            return []
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute("SELECT COALESCE(MAX(seq), 0), MAX(ts_us) FROM events").fetchone()
            last_seq: int = row[0]
            last_ts_us: int | None = row[1]
            envelopes: list[Envelope] = []
            for draft in drafts:
                ts_us = _to_us(draft.ts)
                if last_ts_us is not None and ts_us < last_ts_us:
                    raise StoreError(
                        f"non-monotonic timestamp: {draft.ts.isoformat()} is before the "
                        f"log head ({_from_us(last_ts_us).isoformat()})"
                    )
                last_ts_us = ts_us
                last_seq += 1
                entity_type, entity_id = draft.payload.entity_ref()
                env = Envelope(
                    seq=last_seq,
                    id=f"EVT-{last_seq}",
                    ts=_from_us(ts_us),
                    type=draft.payload.EVENT_TYPE,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    payload=draft.payload,
                    cause=draft.cause,
                    actor=actor,
                )
                cur.execute(
                    "INSERT INTO events (seq, id, ts_us, type, entity_type, entity_id,"
                    " payload, cause, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        env.seq,
                        env.id,
                        ts_us,
                        env.type,
                        env.entity_type,
                        env.entity_id,
                        draft.payload.model_dump_json(),
                        env.cause,
                        env.actor,
                    ),
                )
                envelopes.append(env)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return envelopes

    # -- read -------------------------------------------------------------------

    def read(self, from_seq: int = 1, to_seq: int | None = None) -> Iterator[Envelope]:
        query = (
            "SELECT seq, id, ts_us, type, entity_type, entity_id, payload, cause, actor"
            " FROM events WHERE seq >= ?"
        )
        params: list[int] = [from_seq]
        if to_seq is not None:
            query += " AND seq <= ?"
            params.append(to_seq)
        query += " ORDER BY seq"
        for row in self._conn.execute(query, params):
            payload_cls = PAYLOAD_REGISTRY.get(row[3])
            if payload_cls is None:
                raise StoreError(f"unknown event type in log: {row[3]!r} (seq {row[0]})")
            yield Envelope(
                seq=row[0],
                id=row[1],
                ts=_from_us(row[2]),
                type=row[3],
                entity_type=row[4],
                entity_id=row[5],
                payload=payload_cls.model_validate_json(row[6]),
                cause=row[7],
                actor=row[8],
            )

    def last_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        return int(row[0])

    def last_entity_event_seq(
        self, entity_type: str, entity_id: str, event_type: str
    ) -> int | None:
        """Seq of the entity's most recent event of one type (audit chains, §7.6)."""
        row = self._conn.execute(
            "SELECT MAX(seq) FROM events WHERE entity_type = ? AND entity_id = ? AND type = ?",
            (entity_type, entity_id, event_type),
        ).fetchone()
        return int(row[0]) if row[0] is not None else None

    def event_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0])

    def max_seq_at(self, at: datetime) -> int:
        """Highest seq whose ts <= at (0 if none). Valid because ts is monotonic in seq."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE ts_us <= ?", (_to_us(at),)
        ).fetchone()
        return int(row[0])

    # -- snapshots ---------------------------------------------------------------

    def write_snapshot(self, upto_seq: int, ts: datetime, state: bytes) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO snapshots (upto_seq, ts_us, state) VALUES (?, ?, ?)",
            (upto_seq, _to_us(ts), state),
        )
        self._conn.commit()

    def latest_snapshot(self, max_upto_seq: int | None = None) -> tuple[int, bytes] | None:
        """(upto_seq, state) of the newest snapshot with upto_seq <= max_upto_seq."""
        if max_upto_seq is None:
            row = self._conn.execute(
                "SELECT upto_seq, state FROM snapshots ORDER BY upto_seq DESC LIMIT 1"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT upto_seq, state FROM snapshots WHERE upto_seq <= ?"
                " ORDER BY upto_seq DESC LIMIT 1",
                (max_upto_seq,),
            ).fetchone()
        if row is None:
            return None
        return (int(row[0]), bytes(row[1]))

    def snapshot_seqs(self) -> list[int]:
        return [int(r[0]) for r in self._conn.execute("SELECT upto_seq FROM snapshots ORDER BY 1")]
