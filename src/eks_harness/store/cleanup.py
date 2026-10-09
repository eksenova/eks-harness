from __future__ import annotations

import fnmatch
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from eks_harness.api.errors import ApiError
from eks_harness.daemon.events import EventBus
from eks_harness.db import Database
from eks_harness.db.common import placeholders
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import leases as leases_repo
from eks_harness.db.repos import sessions as sessions_repo
from eks_harness.db.repos.grants import AccessScope
from eks_harness.paths import Paths
from eks_harness.store import deletion
from eks_harness.store import search as store_search

log = logging.getLogger("eks_harness.store.cleanup")

DAY = 86400.0
BATCH = 500
SAMPLE_MAX = 200
GROUP_MAX = 40
Seen = Literal["unseen", "seen", "never"]
Order = Literal["oldest", "newest", "largest"]
ORDERS = {"oldest": "a.created_at ASC, a.id ASC", "newest": "a.created_at DESC, a.id DESC",
          "largest": "a.size DESC, a.id ASC"}


class CleanupError(ValueError):
    pass


@dataclass(frozen=True)
class CleanupFilter:
    projects: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()
    session_pattern: str | None = None
    exclude_sessions: tuple[str, ...] = ()
    project_level: Literal["include", "exclude", "only"] = "include"
    session_idle_days: float | None = None
    older_than_days: float | None = None
    created_before: float | None = None
    created_after: float | None = None
    kinds: tuple[str, ...] = ()
    tags_any: tuple[str, ...] = ()
    tags_all: tuple[str, ...] = ()
    tags_none: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    min_size: int | None = None
    max_size: int | None = None
    q: str | None = None
    seen: Seen | None = None
    include_pinned: bool = False
    include_shared: bool = False
    include_live: bool = False

    def is_empty(self) -> bool:
        return not any((self.projects, self.sessions, self.session_pattern, self.exclude_sessions,
                        self.project_level != "include", self.session_idle_days, self.older_than_days,
                        self.created_before, self.created_after, self.kinds, self.tags_any, self.tags_all,
                        self.tags_none, self.sources, self.min_size, self.max_size, (self.q or "").strip(),
                        self.seen))


@dataclass
class Clause:
    sql: list[str] = field(default_factory=list)
    params: list = field(default_factory=list)

    def add(self, sql: str, *params) -> None:
        self.sql.append(sql)
        self.params.extend(params)

    def joined(self) -> tuple[str, list]:
        return (" AND ".join(f"({s})" for s in self.sql) or "1 = 1"), list(self.params)


def _tags(values: Sequence[str]) -> list[str]:
    try:
        return [artifacts_repo.normalize_tag(v) for v in values if v.strip()]
    except ValueError as problem:
        raise CleanupError(str(problem)) from None


def _session_ids(conn, slugs: Sequence[str]) -> list[int]:
    found = []
    for slug in slugs:
        session = sessions_repo.find(conn, slug)
        if session is None:
            raise CleanupError(f"No session {slug}.")
        found.append(session.id)
    return found


def _pattern_ids(conn, pattern: str) -> list[int]:
    lowered = pattern.strip().lower()
    return [row[0] for row in conn.execute("SELECT id, slug, name FROM sessions")
            if fnmatch.fnmatchcase(row[1].lower(), lowered) or fnmatch.fnmatchcase(row[2].lower(), lowered)]


def shared_clause(at: float) -> str:
    return ("EXISTS (SELECT 1 FROM shares s WHERE s.artifact_id = a.id AND s.revoked_at IS NULL "
            f"AND (s.expires_at IS NULL OR s.expires_at > {float(at)!r}))")


def live_clause() -> tuple[str, list]:
    states = list(leases_repo.LIVE_STATES)
    return (f"a.lease_sid IN (SELECT l.sid FROM leases l WHERE l.state IN ({placeholders(len(states))}))", states)


def match_clause(conn, scope: AccessScope, user_id: int | None, spec: CleanupFilter, at: float) -> Clause:
    out = Clause()
    if not scope.everything:
        sql, params = scope.sql("a.project_id", "a.session_id", "editor")
        out.add(sql, *params)
    if spec.projects:
        out.add(f"a.project_id IN ({placeholders(len(spec.projects))})", *spec.projects)
    if spec.sessions:
        ids = _session_ids(conn, spec.sessions)
        out.add(f"a.session_id IN ({placeholders(len(ids))})", *ids)
    if spec.session_pattern and spec.session_pattern.strip():
        ids = _pattern_ids(conn, spec.session_pattern)
        out.add(f"a.session_id IN ({placeholders(len(ids))})" if ids else "0 = 1", *ids)
    if spec.exclude_sessions:
        ids = _session_ids(conn, spec.exclude_sessions)
        out.add(f"a.session_id IS NULL OR a.session_id NOT IN ({placeholders(len(ids))})", *ids)
    if spec.project_level == "exclude":
        out.add("a.session_id IS NOT NULL")
    elif spec.project_level == "only":
        out.add("a.session_id IS NULL")
    if spec.session_idle_days is not None:
        out.add("a.session_id IN (SELECT x.id FROM sessions x WHERE x.last_active_at < ?)",
                at - spec.session_idle_days * DAY)
    if spec.older_than_days is not None:
        out.add("a.created_at < ?", at - spec.older_than_days * DAY)
    if spec.created_before is not None:
        out.add("a.created_at < ?", spec.created_before)
    if spec.created_after is not None:
        out.add("a.created_at >= ?", spec.created_after)
    if spec.kinds:
        kinds = [k.lower() for k in spec.kinds]
        out.add(f"a.kind IN ({placeholders(len(kinds))})", *kinds)
    tags_any = _tags(spec.tags_any)
    if tags_any:
        out.add(f"EXISTS (SELECT 1 FROM artifact_tags t WHERE t.artifact_id = a.id AND t.tag IN "
                f"({placeholders(len(tags_any))}))", *tags_any)
    for tag in _tags(spec.tags_all):
        out.add("EXISTS (SELECT 1 FROM artifact_tags t WHERE t.artifact_id = a.id AND t.tag = ?)", tag)
    tags_none = _tags(spec.tags_none)
    if tags_none:
        out.add(f"NOT EXISTS (SELECT 1 FROM artifact_tags t WHERE t.artifact_id = a.id AND t.tag IN "
                f"({placeholders(len(tags_none))}))", *tags_none)
    if spec.sources:
        bad = [s for s in spec.sources if s not in artifacts_repo.SOURCES]
        if bad:
            raise CleanupError(f"source must be one of {', '.join(artifacts_repo.SOURCES)}.")
        out.add(f"a.source IN ({placeholders(len(spec.sources))})", *spec.sources)
    if spec.min_size is not None:
        out.add("a.size >= ?", int(spec.min_size))
    if spec.max_size is not None:
        out.add("a.size <= ?", int(spec.max_size))
    match = store_search.fts_query(spec.q or "")
    if match:
        out.add("a.id IN (SELECT artifact_id FROM artifacts_fts WHERE artifacts_fts MATCH ?)", match)
    if spec.seen == "unseen":
        if user_id is None:
            raise CleanupError("unseen needs a signed-in user.")
        out.add("NOT EXISTS (SELECT 1 FROM seen x WHERE x.artifact_id = a.id AND x.user_id = ?)", user_id)
    elif spec.seen == "seen":
        if user_id is None:
            raise CleanupError("seen needs a signed-in user.")
        out.add("EXISTS (SELECT 1 FROM seen x WHERE x.artifact_id = a.id AND x.user_id = ?)", user_id)
    elif spec.seen == "never":
        out.add("NOT EXISTS (SELECT 1 FROM seen x WHERE x.artifact_id = a.id)")
    return out


def protect_clause(spec: CleanupFilter, at: float) -> Clause:
    out = Clause()
    if not spec.include_pinned:
        out.add("a.pinned = 0")
    if not spec.include_shared:
        out.add(f"NOT {shared_clause(at)}")
    if not spec.include_live:
        sql, params = live_clause()
        out.add(f"a.lease_sid IS NULL OR NOT {sql}", *params)
    return out


def full_clause(conn, scope: AccessScope, user_id: int | None, spec: CleanupFilter, at: float) -> tuple[str, list]:
    match = match_clause(conn, scope, user_id, spec, at)
    protect = protect_clause(spec, at)
    return Clause(match.sql + protect.sql, match.params + protect.params).joined()


def _group(conn, select: str, frm: str, where: str, params: list, order: str = "2 DESC") -> list[tuple]:
    return conn.execute(f"SELECT {select}, COUNT(*), COALESCE(SUM(a.size), 0) FROM {frm} WHERE {where} "
                        f"GROUP BY 1 ORDER BY {order} LIMIT {GROUP_MAX + 1}", params).fetchall()


def _empty_after(conn, where: str, params: list) -> list[int]:
    live = list(leases_repo.LIVE_STATES)
    rows = conn.execute(
        f"SELECT a.session_id FROM artifacts a WHERE {where} AND a.session_id IS NOT NULL GROUP BY a.session_id "
        f"HAVING COUNT(*) = (SELECT COUNT(*) FROM artifacts b WHERE b.session_id = a.session_id)", params).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return []
    keep = set()
    for chunk in [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]:
        marks = placeholders(len(chunk))
        keep.update(r[0] for r in conn.execute(f"SELECT DISTINCT session_id FROM notes WHERE session_id IN ({marks})",
                                               chunk))
        keep.update(r[0] for r in conn.execute(
            f"SELECT DISTINCT session_id FROM leases WHERE session_id IN ({marks}) "
            f"AND state IN ({placeholders(len(live))})", [*chunk, *live]))
    return [i for i in ids if i not in keep]


def _sessions_editable(conn, scope: AccessScope, session_ids: Sequence[int]) -> list[int]:
    if scope.everything:
        return list(session_ids)
    linked = sessions_repo.projects_by_session(conn, list(session_ids))
    return [sid for sid in session_ids if linked.get(sid) and all(scope.can(p, sid, "editor") for p in linked[sid])]


def preview(db: Database, scope: AccessScope, user_id: int | None, spec: CleanupFilter, *, sample: int = 50,
            order: Order = "oldest", at: float | None = None) -> dict:
    at = at or time.time()
    conn = db.conn()
    match = match_clause(conn, scope, user_id, spec, at)
    match_sql, match_params = match.joined()
    where, params = full_clause(conn, scope, user_id, spec, at)
    row = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(a.size), 0), MIN(a.created_at), MAX(a.created_at) "
                       f"FROM artifacts a WHERE {where}", params).fetchone()
    count, size, oldest, newest = row[0], row[1], row[2], row[3]
    live_sql, live_params = live_clause()
    skipped = {}
    for key, sql, extra, included in (
            ("pinned", "a.pinned = 1", [], spec.include_pinned),
            ("shared", shared_clause(at), [], spec.include_shared),
            ("live", live_sql, live_params, spec.include_live)):
        skipped[key] = 0 if included else conn.execute(
            f"SELECT COUNT(*) FROM artifacts a WHERE {match_sql} AND {sql}", [*match_params, *extra]).fetchone()[0]
    total = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(a.size), 0) FROM artifacts a WHERE {match_sql}",
                         match_params).fetchone()

    def rows(items: list[tuple], key: str) -> list[dict]:
        return [{key: r[0], "count": r[-2], "bytes": r[-1]} for r in items[:GROUP_MAX]]

    projects = _group(conn, "a.project_id", "artifacts a", where, params)
    kinds = _group(conn, "a.kind", "artifacts a", where, params)
    tags = _group(conn, "t.tag", "artifact_tags t JOIN artifacts a ON a.id = t.artifact_id", where, params)
    sessions = conn.execute(
        f"SELECT a.session_id, s.slug, s.name, s.last_active_at, COUNT(*), COALESCE(SUM(a.size), 0) "
        f"FROM artifacts a LEFT JOIN sessions s ON s.id = a.session_id WHERE {where} "
        f"GROUP BY a.session_id ORDER BY 6 DESC LIMIT {GROUP_MAX + 1}", params).fetchall()
    session_rows = [{"slug": r[1], "name": r[2] or "Project files", "lastActiveAt": r[3], "count": r[4], "bytes": r[5]}
                    for r in sessions[:GROUP_MAX]]
    sample_ids = [r[0] for r in conn.execute(f"SELECT a.id FROM artifacts a WHERE {where} ORDER BY {ORDERS[order]} "
                                             f"LIMIT ?", [*params, max(0, min(sample, SAMPLE_MAX))])]
    found = {a.id: a for a in artifacts_repo.get_many(conn, sample_ids)}
    empty = _sessions_editable(conn, scope, _empty_after(conn, where, params)) if count else []
    return {
        "count": count, "bytes": size, "oldest": oldest, "newest": newest,
        "matched": {"count": total[0], "bytes": total[1]}, "skipped": skipped,
        "projects": rows(projects, "project"), "kinds": rows(kinds, "kind"), "tags": rows(tags, "tag"),
        "sessions": session_rows,
        "more": {"projects": len(projects) > GROUP_MAX, "kinds": len(kinds) > GROUP_MAX,
                 "tags": len(tags) > GROUP_MAX, "sessions": len(sessions) > GROUP_MAX},
        "emptySessions": len(empty),
        "sample": [found[i] for i in sample_ids if i in found],
        "at": at,
    }


@dataclass
class CleanupResult:
    artifacts: int = 0
    bytes: int = 0
    shares: int = 0
    sessions: int = 0
    notes: int = 0
    batches: int = 0

    def as_dict(self) -> dict:
        return {"deleted": True, "artifacts": self.artifacts, "bytes": self.bytes, "shares": self.shares,
                "sessions": self.sessions, "notes": self.notes}


def apply(db: Database, paths: Paths, scope: AccessScope, user_id: int | None, spec: CleanupFilter, *,
          remove_empty_sessions: bool = False, events: EventBus | None = None, actor: str | None = None,
          at: float | None = None) -> CleanupResult:
    if spec.is_empty():
        raise CleanupError("Set at least one condition; a cleanup never matches everything by default.")
    at = at or time.time()
    result = CleanupResult()
    conn = db.conn()
    where, params = full_clause(conn, scope, user_id, spec, at)
    empty = (set(_sessions_editable(conn, scope, _empty_after(conn, where, params)))
             if remove_empty_sessions else set())
    last = ""
    while True:
        ids = [r[0] for r in conn.execute(f"SELECT a.id FROM artifacts a WHERE {where} AND a.id > ? "
                                          f"ORDER BY a.id LIMIT {BATCH}", [*params, last])]
        if not ids:
            break
        last = ids[-1]
        plan = deletion.delete_artifacts(db, paths, ids, events=events, actor=actor, still_expired=(where, params))
        result.artifacts += len(plan.artifact_ids)
        result.bytes += plan.bytes
        result.shares += plan.shares
        result.batches += 1
    for session_id in sorted(empty):
        session = sessions_repo.get(conn, session_id)
        if session is None:
            continue
        if conn.execute("SELECT 1 FROM artifacts WHERE session_id = ? LIMIT 1", (session_id,)).fetchone():
            continue
        try:
            plan = deletion.delete_session(db, paths, session, events=events, actor=actor)
        except ApiError as problem:
            log.info("cleanup kept session %s: %s", session.slug, problem)
            continue
        result.sessions += plan.sessions
        result.notes += plan.notes
    from eks_harness.store.retention import invalidate_usage

    invalidate_usage()
    return result
