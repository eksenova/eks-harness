from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from eks_harness.api.errors import bad_request
from eks_harness.api.links import Links
from eks_harness.api.schemas import EventOut, TimelineEntry, ts_to_datetime
from eks_harness.daemon import events as ev
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import events as events_repo
from eks_harness.db.repos import leases as leases_repo
from eks_harness.db.repos import notes as notes_repo
from eks_harness.db.repos.events import EventRow
from eks_harness.db.repos.sessions import Session
from eks_harness.paths import Paths
from eks_harness.store import artifacts as store_artifacts
from eks_harness.store.notes import note_out

REDUNDANT_EVENT_TYPES = frozenset({ev.ARTIFACT_CREATED, ev.NOTE_CREATED})
TIMELINE_TYPES = ("event", "note", "artifact")


def event_out(row: EventRow) -> EventOut:
    return EventOut(id=row.id, ts=ts_to_datetime(row.ts), type=row.type, resource=row.resource,
                    lease_sid=row.lease_sid, session_id=row.session_id, project_id=row.project_id, actor=row.actor,
                    detail=row.detail)


def _session_events(conn: sqlite3.Connection, session: Session, since: float | None, until: float | None,
                    limit: int, project_id: str | None = None) -> list[EventRow]:
    rows = {r.id: r for r in events_repo.list_events(conn, session_id=session.id, since_ts=since, until_ts=until,
                                                     limit=limit)}
    for lease in leases_repo.for_session(conn, session.id):
        if project_id is not None and lease.project_id not in (None, project_id):
            continue
        for row in events_repo.list_events(conn, lease_sid=lease.sid, since_ts=since, until_ts=until, limit=limit):
            if row.session_id in (None, session.id):
                rows.setdefault(row.id, row)
    return [r for r in rows.values() if r.type not in REDUNDANT_EVENT_TYPES
            and (project_id is None or r.project_id in (None, project_id))]


def session_timeline(conn: sqlite3.Connection, links: Links, paths: Paths, session: Session, *,
                     user_id: int | None = None, since: float | None = None, until: float | None = None,
                     limit: int = 500, types: Iterable[str] | None = None, project_id: str | None = None,
                     artifact_scope: tuple[str, list] | None = None) -> list[TimelineEntry]:
    wanted = set(types or TIMELINE_TYPES)
    unknown = wanted - set(TIMELINE_TYPES)
    if unknown:
        raise bad_request(f"Unknown timeline types: {', '.join(sorted(unknown))}.", error="invalid_type")
    limit = max(1, min(int(limit), 5000))
    entries: list[tuple[float, int, TimelineEntry]] = []
    if "event" in wanted:
        for row in _session_events(conn, session, since, until, limit, project_id):
            entries.append((row.ts, 0, TimelineEntry(ts=ts_to_datetime(row.ts), type="event", event=event_out(row))))
    if "note" in wanted:
        for note in notes_repo.list_for_session(conn, session.id, since=since):
            if until is not None and note.created_at > until:
                continue
            entries.append((note.created_at, 1, TimelineEntry(ts=ts_to_datetime(note.created_at), type="note",
                                                              note=note_out(note))))
    if "artifact" in wanted:
        query = artifacts_repo.ArtifactQuery(session_id=session.id, project_id=project_id, created_after=since,
                                             scope_sql=artifact_scope, limit=limit)
        items, _ = artifacts_repo.query(conn, query)
        if until is not None:
            items = [a for a in items if a.created_at <= until]
        for out, artifact in zip(store_artifacts.to_out_many(conn, links, paths, items, user_id), items):
            entries.append((artifact.created_at, 2, TimelineEntry(ts=ts_to_datetime(artifact.created_at),
                                                                  type="artifact", artifact=out)))
    entries.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in entries[-limit:]]
