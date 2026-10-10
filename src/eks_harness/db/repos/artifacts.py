from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from eks_harness.db.common import UNSET, dumps, loads, now, placeholders

KNOWN_KINDS = ("screenshot", "video", "audio", "dom", "mhtml", "a11y", "har", "console", "log", "site", "file")
SOURCES = ("agent", "cli", "ui", "mcp")
TAG_PATTERN = re.compile(r"^[\w.:/+-]{1,64}$", re.UNICODE)

_COLUMNS = (
    "id", "project_id", "session_id", "lease_sid", "kind", "mime", "filename", "rel_path", "size", "sha256",
    "width", "height", "duration_ms", "caption", "pinned", "source", "created_by", "created_at", "meta",
    "retention_days",
)


@dataclass(frozen=True)
class Artifact:
    id: str
    project_id: str
    session_id: int | None
    lease_sid: str | None
    kind: str
    mime: str
    filename: str
    rel_path: str
    size: int
    sha256: str
    width: int | None
    height: int | None
    duration_ms: int | None
    caption: str
    pinned: bool
    source: str
    created_by: str | None
    created_at: float
    meta: dict[str, Any] = field(default_factory=dict)
    retention_days: int | None = None
    tags: tuple[str, ...] = ()
    session_slug: str | None = None
    session_name: str | None = None


def normalize_tag(tag: str) -> str:
    value = (tag or "").strip().lower()
    if not TAG_PATTERN.match(value):
        raise ValueError(f"invalid tag '{tag}': 1-64 letters, digits or . _ : / + -")
    return value


def normalize_tags(tags: Iterable[str] | None) -> list[str]:
    return list(dict.fromkeys(normalize_tag(t) for t in (tags or []) if t and t.strip()))


def _row(row: sqlite3.Row | None, tags: Iterable[str] = ()) -> Artifact | None:
    if row is None:
        return None
    keys = row.keys()
    values = {c: row[c] for c in _COLUMNS}
    values["pinned"] = bool(values["pinned"])
    values["meta"] = loads(values["meta"])
    return Artifact(**values, tags=tuple(sorted(tags)),
                    session_slug=row["session_slug"] if "session_slug" in keys else None,
                    session_name=row["session_name"] if "session_name" in keys else None)


_SELECT = (
    "SELECT a.*, s.slug AS session_slug, s.name AS session_name "
    "FROM artifacts a LEFT JOIN sessions s ON s.id = a.session_id"
)


def tags_for(conn: sqlite3.Connection, artifact_ids: Iterable[str]) -> dict[str, list[str]]:
    ids = list(dict.fromkeys(artifact_ids))
    found: dict[str, list[str]] = {i: [] for i in ids}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for row in conn.execute(
                f"SELECT artifact_id, tag FROM artifact_tags WHERE artifact_id IN ({placeholders(len(chunk))}) "
                f"ORDER BY tag", chunk):
            found[row[0]].append(row[1])
    return found


def _hydrate(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[Artifact]:
    tags = tags_for(conn, [r["id"] for r in rows])
    return [_row(r, tags.get(r["id"], [])) for r in rows]


def insert(conn: sqlite3.Connection, *, artifact_id: str, project_id: str, kind: str, filename: str,
           rel_path: str, session_id: int | None = None, lease_sid: str | None = None,
           mime: str = "application/octet-stream", size: int = 0, sha256: str = "", width: int | None = None,
           height: int | None = None, duration_ms: int | None = None, caption: str = "", pinned: bool = False,
           source: str = "cli", created_by: str | None = None, created_at: float | None = None,
           meta: dict | None = None, tags: Iterable[str] | None = None,
           retention_days: int | None = None) -> Artifact:
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    clean_tags = normalize_tags(tags)
    conn.execute(
        f"INSERT INTO artifacts ({', '.join(_COLUMNS)}) VALUES ({placeholders(len(_COLUMNS))})",
        (artifact_id, project_id, session_id, lease_sid, kind, mime, filename, rel_path, size, sha256, width,
         height, duration_ms, caption or "", int(pinned), source, created_by, created_at or now(), dumps(meta or {}),
         retention_days))
    if clean_tags:
        conn.executemany("INSERT OR IGNORE INTO artifact_tags (artifact_id, tag) VALUES (?, ?)",
                         [(artifact_id, t) for t in clean_tags])
    return get(conn, artifact_id)


def get(conn: sqlite3.Connection, artifact_id: str) -> Artifact | None:
    row = conn.execute(f"{_SELECT} WHERE a.id = ?", (artifact_id,)).fetchone()
    if row is None:
        return None
    return _hydrate(conn, [row])[0]


def get_many(conn: sqlite3.Connection, artifact_ids: Iterable[str]) -> list[Artifact]:
    ids = list(dict.fromkeys(artifact_ids))
    if not ids:
        return []
    rows = conn.execute(f"{_SELECT} WHERE a.id IN ({placeholders(len(ids))})", ids).fetchall()
    order = {i: n for n, i in enumerate(ids)}
    return sorted(_hydrate(conn, rows), key=lambda a: order[a.id])


def update(conn: sqlite3.Connection, artifact_id: str, *, caption=UNSET, pinned=UNSET, meta=UNSET,
           kind=UNSET, filename=UNSET, mime=UNSET, size=UNSET, sha256=UNSET, width=UNSET, height=UNSET,
           duration_ms=UNSET, rel_path=UNSET, retention_days=UNSET) -> Artifact | None:
    fields = {"caption": caption, "pinned": int(pinned) if pinned is not UNSET else UNSET,
              "meta": dumps(meta) if meta is not UNSET else UNSET, "kind": kind, "filename": filename, "mime": mime,
              "size": size, "sha256": sha256, "width": width, "height": height, "duration_ms": duration_ms,
              "rel_path": rel_path, "retention_days": retention_days}
    values = {k: v for k, v in fields.items() if v is not UNSET}
    if values:
        conn.execute(f"UPDATE artifacts SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                     (*values.values(), artifact_id))
    return get(conn, artifact_id)


def set_tags(conn: sqlite3.Connection, artifact_id: str, tags: Iterable[str]) -> list[str]:
    clean = normalize_tags(tags)
    current = set(tags_for(conn, [artifact_id]).get(artifact_id, []))
    remove = current - set(clean)
    if remove:
        conn.execute(f"DELETE FROM artifact_tags WHERE artifact_id = ? AND tag IN ({placeholders(len(remove))})",
                     [artifact_id, *remove])
    conn.executemany("INSERT OR IGNORE INTO artifact_tags (artifact_id, tag) VALUES (?, ?)",
                     [(artifact_id, t) for t in clean if t not in current])
    return sorted(clean)


def add_tags(conn: sqlite3.Connection, artifact_ids: Iterable[str], tags: Iterable[str]) -> int:
    clean = normalize_tags(tags)
    rows = [(a, t) for a in dict.fromkeys(artifact_ids) for t in clean]
    before = conn.total_changes
    conn.executemany("INSERT OR IGNORE INTO artifact_tags (artifact_id, tag) VALUES (?, ?)", rows)
    return conn.total_changes - before


def remove_tags(conn: sqlite3.Connection, artifact_ids: Iterable[str], tags: Iterable[str]) -> int:
    clean = normalize_tags(tags)
    ids = list(dict.fromkeys(artifact_ids))
    if not clean or not ids:
        return 0
    return conn.execute(
        f"DELETE FROM artifact_tags WHERE artifact_id IN ({placeholders(len(ids))}) "
        f"AND tag IN ({placeholders(len(clean))})", [*ids, *clean]).rowcount


def all_tags(conn: sqlite3.Connection, project_id: str | None = None, session_id: int | None = None) -> list[tuple[str, int]]:
    clauses, params = [], []
    if project_id is not None:
        clauses.append("a.project_id = ?")
        params.append(project_id)
    if session_id is not None:
        clauses.append("a.session_id = ?")
        params.append(session_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return [(r[0], r[1]) for r in conn.execute(
        f"SELECT t.tag, COUNT(*) FROM artifact_tags t JOIN artifacts a ON a.id = t.artifact_id{where} "
        f"GROUP BY t.tag ORDER BY t.tag", params)]


def delete(conn: sqlite3.Connection, artifact_id: str) -> bool:
    return conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,)).rowcount > 0


def delete_many(conn: sqlite3.Connection, artifact_ids: Iterable[str]) -> int:
    ids = list(dict.fromkeys(artifact_ids))
    deleted = 0
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        deleted += conn.execute(f"DELETE FROM artifacts WHERE id IN ({placeholders(len(chunk))})", chunk).rowcount
    return deleted


@dataclass(frozen=True)
class ArtifactQuery:
    project_id: str | None = None
    session_id: int | None = None
    project_level_only: bool = False
    kinds: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    fts_match: str | None = None
    unseen_for_user: int | None = None
    seen_for_user: int | None = None
    pinned: bool | None = None
    lease_sid: str | None = None
    created_before: float | None = None
    created_after: float | None = None
    scope_sql: tuple[str, list] | None = None
    cursor: str | None = None
    limit: int = 50
    ascending: bool = False
    sort: str = "created"


def _where(q: ArtifactQuery) -> tuple[str, list]:
    clauses, params = [], []
    if q.project_id is not None:
        clauses.append("a.project_id = ?")
        params.append(q.project_id)
    if q.session_id is not None:
        clauses.append("a.session_id = ?")
        params.append(q.session_id)
    if q.project_level_only:
        clauses.append("a.session_id IS NULL")
    if q.kinds:
        clauses.append(f"a.kind IN ({placeholders(len(q.kinds))})")
        params.extend(q.kinds)
    for tag in q.tags:
        clauses.append("EXISTS (SELECT 1 FROM artifact_tags t WHERE t.artifact_id = a.id AND t.tag = ?)")
        params.append(normalize_tag(tag))
    if q.fts_match:
        clauses.append("a.id IN (SELECT artifact_id FROM artifacts_fts WHERE artifacts_fts MATCH ?)")
        params.append(q.fts_match)
    if q.unseen_for_user is not None:
        clauses.append("NOT EXISTS (SELECT 1 FROM seen x WHERE x.artifact_id = a.id AND x.user_id = ?)")
        params.append(q.unseen_for_user)
    if q.seen_for_user is not None:
        clauses.append("EXISTS (SELECT 1 FROM seen x WHERE x.artifact_id = a.id AND x.user_id = ?)")
        params.append(q.seen_for_user)
    if q.pinned is not None:
        clauses.append("a.pinned = ?")
        params.append(int(q.pinned))
    if q.lease_sid is not None:
        clauses.append("a.lease_sid = ?")
        params.append(q.lease_sid)
    if q.created_before is not None:
        clauses.append("a.created_at < ?")
        params.append(q.created_before)
    if q.created_after is not None:
        clauses.append("a.created_at >= ?")
        params.append(q.created_after)
    if q.scope_sql is not None:
        clauses.append(q.scope_sql[0])
        params.extend(q.scope_sql[1])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


SORT_COLUMNS = {
    "created": None,
    "name": "a.filename COLLATE NOCASE",
    "size": "a.size",
    "kind": "a.kind",
}


class CursorError(ValueError):
    pass


def encode_cursor(direction: str, sort: str, artifact: Artifact) -> str:
    value: Any = None
    if sort == "name":
        value = artifact.filename
    elif sort == "size":
        value = artifact.size
    elif sort == "kind":
        value = artifact.kind
    raw = json.dumps({"d": direction, "s": sort, "v": value, "i": artifact.id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, sort: str) -> tuple[str, Any, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        if sort == "created" and re.fullmatch(r"[0-9A-Za-z]{10,40}", cursor):
            return "a", None, cursor
        raise CursorError("the cursor is not valid") from None
    if not isinstance(data, dict) or data.get("d") not in ("a", "b") or not isinstance(data.get("i"), str):
        raise CursorError("the cursor is not valid")
    if data.get("s", "created") != sort:
        raise CursorError("the cursor belongs to another sort order; start from the first page")
    return data["d"], data.get("v"), data["i"]


def _seek(column: str | None, forward_greater: bool, value: Any, artifact_id: str) -> tuple[str, list]:
    op = ">" if forward_greater else "<"
    if column is None:
        return f"a.id {op} ?", [artifact_id]
    return f"({column} {op} ? OR ({column} = ? AND a.id {op} ?))", [value, value, artifact_id]


def query_page(conn: sqlite3.Connection, q: ArtifactQuery) -> tuple[list[Artifact], str | None, str | None]:
    if q.sort not in SORT_COLUMNS:
        raise CursorError(f"sort must be one of {', '.join(SORT_COLUMNS)}")
    column = SORT_COLUMNS[q.sort]
    where, params = _where(q)
    direction = "a"
    if q.cursor:
        direction, value, artifact_id = decode_cursor(q.cursor, q.sort)
        forward_greater = q.ascending if direction == "a" else not q.ascending
        clause, extra = _seek(column, forward_greater, value, artifact_id)
        where += (" AND " if where else " WHERE ") + clause
        params.extend(extra)
    walk_ascending = q.ascending if direction == "a" else not q.ascending
    order_word = "ASC" if walk_ascending else "DESC"
    order = f"{column} {order_word}, a.id {order_word}" if column else f"a.id {order_word}"
    limit = max(1, min(int(q.limit), 1000))
    rows = conn.execute(f"{_SELECT}{where} ORDER BY {order} LIMIT ?", [*params, limit + 1]).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    if direction == "b":
        rows.reverse()
    items = _hydrate(conn, rows)
    if not items:
        return items, None, None
    if direction == "a":
        next_cursor = encode_cursor("a", q.sort, items[-1]) if more else None
        prev_cursor = encode_cursor("b", q.sort, items[0]) if q.cursor else None
    else:
        next_cursor = encode_cursor("a", q.sort, items[-1])
        prev_cursor = encode_cursor("b", q.sort, items[0]) if more else None
    return items, next_cursor, prev_cursor


def query(conn: sqlite3.Connection, q: ArtifactQuery) -> tuple[list[Artifact], str | None]:
    items, next_cursor, _ = query_page(conn, q)
    return items, next_cursor


def facets(conn: sqlite3.Connection, q: ArtifactQuery) -> dict[str, dict[str, int]]:
    without_kinds = replace(q, kinds=(), cursor=None)
    where, params = _where(without_kinds)
    kinds = {r[0]: r[1] for r in conn.execute(
        f"SELECT a.kind, COUNT(*) FROM artifacts a{where} GROUP BY a.kind ORDER BY a.kind", params)}
    without_tags = replace(q, tags=(), cursor=None)
    where, params = _where(without_tags)
    tags = {r[0]: r[1] for r in conn.execute(
        f"SELECT t.tag, COUNT(*) FROM artifact_tags t JOIN artifacts a ON a.id = t.artifact_id{where} "
        f"GROUP BY t.tag ORDER BY t.tag", params)}
    return {"kind": kinds, "tag": tags}


def count(conn: sqlite3.Connection, q: ArtifactQuery) -> int:
    where, params = _where(q)
    return conn.execute(f"SELECT COUNT(*) FROM artifacts a{where}", params).fetchone()[0]


def total_size(conn: sqlite3.Connection, q: ArtifactQuery | None = None) -> int:
    where, params = _where(q or ArtifactQuery())
    return conn.execute(f"SELECT COALESCE(SUM(a.size), 0) FROM artifacts a{where}", params).fetchone()[0]


def ids_matching(conn: sqlite3.Connection, q: ArtifactQuery) -> list[str]:
    where, params = _where(q)
    return [r[0] for r in conn.execute(f"SELECT a.id FROM artifacts a{where} ORDER BY a.id", params)]


def expired_clause(project_cutoff: float | None, at: float) -> tuple[str, list]:
    own = "(a.retention_days IS NOT NULL AND a.created_at < ? - a.retention_days * 86400.0)"
    if project_cutoff is None:
        return f"a.pinned = 0 AND {own}", [at]
    return (f"a.pinned = 0 AND ({own} OR (a.retention_days IS NULL AND a.created_at < ?))",
            [at, project_cutoff])


def retention_candidates(conn: sqlite3.Connection, project_id: str, project_cutoff: float | None, at: float,
                         limit: int = 1000) -> list[Artifact]:
    clause, params = expired_clause(project_cutoff, at)
    rows = conn.execute(
        f"{_SELECT} WHERE a.project_id = ? AND {clause} ORDER BY a.id LIMIT ?",
        (project_id, *params, limit)).fetchall()
    return _hydrate(conn, rows)


def neighbours(conn: sqlite3.Connection, artifact: Artifact) -> tuple[str | None, str | None]:
    if artifact.session_id is None:
        scope, params = "a.project_id = ? AND a.session_id IS NULL", [artifact.project_id]
    else:
        scope, params = "a.project_id = ? AND a.session_id = ?", [artifact.project_id, artifact.session_id]
    newer = conn.execute(f"SELECT a.id FROM artifacts a WHERE {scope} AND a.id > ? ORDER BY a.id ASC LIMIT 1",
                         [*params, artifact.id]).fetchone()
    older = conn.execute(f"SELECT a.id FROM artifacts a WHERE {scope} AND a.id < ? ORDER BY a.id DESC LIMIT 1",
                         [*params, artifact.id]).fetchone()
    return (newer[0] if newer else None), (older[0] if older else None)
