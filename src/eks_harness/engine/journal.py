from __future__ import annotations

import base64
import binascii
import json
import math
import reprlib
import sqlite3
import threading
import time
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from pydantic import JsonValue

from eks_harness.db import Database
from eks_harness.db.common import placeholders
from eks_harness.engine.model import (
    TERMINAL_STATUSES,
    CacheCounts,
    EnvironmentOut,
    ExpansionKind,
    ExpansionRecord,
    FakeProfile,
    NodeError,
    NodeEventIn,
    NodeEventRecord,
    NodeRecord,
    PauseInfo,
    ResourceRecord,
    RunClock,
    RunInfo,
    RunOptions,
    RunQuery,
    RunStatus,
)
from eks_harness.ir.plan import Plan

EVENT_PAGE = 500
DETAIL_LIMIT_BYTES = 16_384

_ELLIPSIS = "..."
_SHRINK_STEPS: tuple[tuple[int, int], ...] = (
    (4096, 200),
    (1024, 100),
    (256, 50),
    (64, 20),
    (16, 8),
    (0, 2),
)
_PREVIEW = reprlib.Repr(maxlevel=4, maxdict=8, maxlist=8, maxtuple=8, maxset=8, maxstring=200, maxother=200)
_RUN_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()
_RUN_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class RunRow:
    info: RunInfo
    plan: Plan
    status: RunStatus
    reason: str | None
    request: dict[str, JsonValue]
    environment: EnvironmentOut
    outputs: JsonValue
    has_outputs: bool
    counts: dict[str, int]
    cache: CacheCounts
    simulated: bool
    replay_of: str | None
    repeat_of: str | None
    started_at: float | None
    finished_at: float | None


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _clean(text: str) -> str:
    return text if _encodable(text) else text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _clean_optional(text: str | None) -> str | None:
    return None if text is None else _clean(text)


def _text(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if _encodable(text) else json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)


def _run_lock(run_id: str) -> threading.RLock:
    with _RUN_LOCKS_GUARD:
        lock = _RUN_LOCKS.get(run_id)
        if lock is None:
            lock = threading.RLock()
            _RUN_LOCKS[run_id] = lock
        return lock


def _nullable(value: Any) -> str | None:
    return None if value is None else _text(value)


def _load(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _load_dict(text: str | None) -> dict[str, JsonValue]:
    loaded = _load(text)
    return cast("dict[str, JsonValue]", loaded) if isinstance(loaded, dict) else {}


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json", by_alias=True)


def _encode_cursor(created_at: float, run_id: str) -> str:
    raw = _text([created_at, run_id]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        loaded = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid run cursor {cursor!r}") from exc
    parts = _cursor_parts(loaded)
    if parts is None:
        raise ValueError(f"invalid run cursor {cursor!r}")
    return parts


def _cursor_parts(loaded: Any) -> tuple[float, str] | None:
    if not isinstance(loaded, list) or len(cast("list[Any]", loaded)) != 2:
        return None
    created, run_id = cast("list[Any]", loaded)
    if isinstance(created, bool) or not isinstance(created, int | float) or not isinstance(run_id, str):
        return None
    return float(created), run_id


class Journal:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create_run(self, info: RunInfo, plan: Plan, *, request: Mapping[str, JsonValue],
                   environment: EnvironmentOut, simulated: bool, replay_of: str | None = None,
                   repeat_of: str | None = None) -> RunRow:
        stored_request = {key: value for key, value in request.items() if key != "plan"}
        settings = {
            "options": _dump(info.options),
            "fakes": None if info.fakes is None else _dump(info.fakes),
            "clock": None if info.clock is None else _dump(info.clock),
        }
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO runs (id, plan_hash, plan_kind, plan_name, plan, spec, tree, project_id, session_id, "
                "session_name, user_id, actor, instance, status, reason, options, request, environment, outputs, "
                "counts, cache, simulated, replay_of, repeat_of, created_at, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', NULL, ?, ?, ?, NULL, '{}', ?, ?, ?, ?, ?, "
                "NULL, NULL)",
                (info.id, info.plan_hash, info.plan_kind, info.plan_name, _text(_dump(plan)), info.spec, info.tree,
                 info.project or None, info.session_id, info.session, info.user_id, info.actor, info.instance,
                 _text(settings), _text(stored_request), _text(_dump(environment)), _text(_dump(CacheCounts())),
                 1 if simulated else 0, replay_of, repeat_of, info.created_at))
        row = self.run(info.id)
        if row is None:
            raise RuntimeError(f"run {info.id} vanished right after it was journaled")
        return row

    def _row(self, row: sqlite3.Row) -> RunRow:
        settings = _load_dict(row["options"])
        fakes = settings.get("fakes")
        clock = settings.get("clock")
        info = RunInfo(
            id=row["id"],
            project=row["project_id"] or "",
            session=row["session_name"] or "",
            session_id=row["session_id"],
            tree=row["tree"],
            spec=row["spec"],
            plan_hash=row["plan_hash"],
            plan_kind=row["plan_kind"],
            plan_name=row["plan_name"],
            options=RunOptions.model_validate(settings.get("options") or {}),
            instance=row["instance"],
            actor=row["actor"],
            user_id=row["user_id"],
            fakes=None if fakes is None else FakeProfile.model_validate(fakes),
            clock=None if clock is None else RunClock.model_validate(clock),
            created_at=row["created_at"],
        )
        counts = _load_dict(row["counts"])
        return RunRow(
            info=info,
            plan=Plan.model_validate_json(row["plan"]),
            status=row["status"],
            reason=row["reason"],
            request=_load_dict(row["request"]),
            environment=EnvironmentOut.model_validate(_load_dict(row["environment"])),
            outputs=_load(row["outputs"]),
            has_outputs=row["outputs"] is not None,
            counts={str(key): int(cast("int", value)) for key, value in counts.items()},
            cache=CacheCounts.model_validate(_load_dict(row["cache"])),
            simulated=bool(row["simulated"]),
            replay_of=row["replay_of"],
            repeat_of=row["repeat_of"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def run(self, run_id: str) -> RunRow | None:
        found = self.db.conn().execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return None if found is None else self._row(found)

    def _state(self, run_id: str) -> tuple[RunStatus, float | None] | None:
        found = self.db.conn().execute("SELECT status, started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
        return None if found is None else (found["status"], found["started_at"])

    def runs(self, query: RunQuery, *, project_ids: Sequence[str] | None = None,
             session_ids: Sequence[int] | None = None) -> tuple[list[RunRow], str | None]:
        clauses: list[str] = []
        params: list[Any] = []
        if query.project is not None:
            clauses.append("project_id = ?")
            params.append(query.project)
        if query.session is not None:
            clauses.append("session_name = ?")
            params.append(query.session)
        if query.status:
            clauses.append(f"status IN ({placeholders(len(query.status))})")
            params.extend(query.status)
        if query.plan_hash is not None:
            clauses.append("plan_hash = ?")
            params.append(query.plan_hash)
        if query.name is not None:
            clauses.append("plan_name = ?")
            params.append(query.name)
        if project_ids is not None or session_ids is not None:
            visible: list[str] = []
            projects = list(project_ids or [])
            sessions = list(session_ids or [])
            if projects:
                visible.append(f"project_id IN ({placeholders(len(projects))})")
                params.extend(projects)
            if sessions:
                visible.append(f"session_id IN ({placeholders(len(sessions))})")
                params.extend(sessions)
            clauses.append("(" + " OR ".join(visible) + ")" if visible else "0 = 1")
        if query.cursor is not None:
            created, last_id = _decode_cursor(query.cursor)
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([created, created, last_id])
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = self.db.conn().execute(
            f"SELECT * FROM runs {where}ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, query.limit + 1)).fetchall()
        found = [self._row(row) for row in rows[:query.limit]]
        cursor = None
        if len(rows) > query.limit and found:
            last = found[-1]
            cursor = _encode_cursor(last.info.created_at, last.info.id)
        return found, cursor

    def set_status(self, run_id: str, status: RunStatus, *, reason: str | None = None,
                   started_at: float | None = None, finished_at: float | None = None) -> None:
        stamp = time.time()
        started = (started_at if started_at is not None else stamp) if status == "running" else None
        finished = (finished_at if finished_at is not None else stamp) if status in TERMINAL_STATUSES else None
        self._update(run_id, "status = ?, reason = ?, started_at = COALESCE(started_at, ?), "
                     "finished_at = COALESCE(?, finished_at)",
                     (status, _clean_optional(reason), started, finished))

    def _update(self, run_id: str, assignments: str, params: Sequence[Any]) -> None:
        with self.db.transaction() as conn:
            cursor = conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*params, run_id))
        if cursor.rowcount == 0:
            raise KeyError(f"no run {run_id!r} in the journal")

    def set_environment(self, run_id: str, environment: EnvironmentOut) -> None:
        self._update(run_id, "environment = ?", (_text(_dump(environment)),))

    def set_outputs(self, run_id: str, outputs: JsonValue) -> None:
        self._update(run_id, "outputs = ?", (_text(outputs),))

    def set_counts(self, run_id: str, counts: Mapping[str, int], cache: CacheCounts) -> None:
        self._update(run_id, "counts = ?, cache = ?", (_text(dict(counts)), _text(_dump(cache))))

    def upsert_node(self, record: NodeRecord) -> None:
        values = (
            record.run_id, record.node_id, record.instance, record.parent_id, record.parent_instance, record.seq,
            record.op, record.node_hash, record.label, record.src, record.status, record.attempt,
            _nullable(record.args), _text(record.output) if record.has_output else None,
            1 if record.has_output else 0,
            None if record.error is None else _text(_dump(record.error)),
            record.cache, record.cache_key, _nullable(record.resolution), record.executor, record.location,
            1 if record.simulated else 0, 1 if record.edited else 0, _text(list(record.artifacts)),
            record.queued_at, record.started_at, record.finished_at, record.duration_ms,
        )
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO run_nodes (run_id, node_id, instance, parent_id, parent_instance, seq, op, node_hash, "
                "label, src, status, attempt, args, output, has_output, error, cache, cache_key, resolution, "
                "executor, location, simulated, edited, artifacts, queued_at, started_at, finished_at, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (run_id, node_id, instance) DO UPDATE SET parent_id = excluded.parent_id, "
                "parent_instance = excluded.parent_instance, seq = excluded.seq, op = excluded.op, "
                "node_hash = excluded.node_hash, label = excluded.label, src = excluded.src, "
                "status = excluded.status, attempt = excluded.attempt, args = excluded.args, "
                "output = excluded.output, has_output = excluded.has_output, error = excluded.error, "
                "cache = excluded.cache, cache_key = excluded.cache_key, resolution = excluded.resolution, "
                "executor = excluded.executor, location = excluded.location, simulated = excluded.simulated, "
                "edited = excluded.edited, artifacts = excluded.artifacts, queued_at = excluded.queued_at, "
                "started_at = excluded.started_at, finished_at = excluded.finished_at, "
                "duration_ms = excluded.duration_ms",
                values)

    def _node(self, row: sqlite3.Row) -> NodeRecord:
        error = _load(row["error"])
        artifacts = _load(row["artifacts"])
        return NodeRecord(
            run_id=row["run_id"],
            node_id=row["node_id"],
            instance=row["instance"],
            parent_id=row["parent_id"],
            parent_instance=row["parent_instance"],
            seq=row["seq"],
            op=row["op"],
            node_hash=row["node_hash"],
            label=row["label"],
            src=row["src"],
            status=row["status"],
            attempt=row["attempt"],
            args=_load(row["args"]),
            output=_load(row["output"]) if row["has_output"] else None,
            has_output=bool(row["has_output"]),
            error=None if error is None else NodeError.model_validate(error),
            cache=row["cache"],
            cache_key=row["cache_key"],
            resolution=_load(row["resolution"]),
            executor=row["executor"],
            location=row["location"],
            simulated=bool(row["simulated"]),
            edited=bool(row["edited"]),
            artifacts=[str(item) for item in cast("list[Any]", artifacts)] if isinstance(artifacts, list) else [],
            queued_at=row["queued_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            duration_ms=row["duration_ms"],
        )

    def node(self, run_id: str, node_id: str, instance: str) -> NodeRecord | None:
        found = self.db.conn().execute(
            "SELECT * FROM run_nodes WHERE run_id = ? AND node_id = ? AND instance = ?",
            (run_id, node_id, instance)).fetchone()
        return None if found is None else self._node(found)

    def nodes(self, run_id: str) -> list[NodeRecord]:
        rows = self.db.conn().execute(
            "SELECT * FROM run_nodes WHERE run_id = ? ORDER BY seq, node_id, instance", (run_id,)).fetchall()
        return [self._node(row) for row in rows]

    def append(self, run_id: str, event: NodeEventIn) -> NodeEventRecord:
        detail = bounded_detail(event.detail)
        stamp = time.time()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO node_events (run_id, node_id, instance, attempt, ts, type, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, event.node_id, event.instance, event.attempt, stamp, event.type, _text(detail)))
        event_id = cursor.lastrowid
        if event_id is None:
            raise RuntimeError(f"the journal did not assign an id to the {event.type} event of run {run_id}")
        return NodeEventRecord(id=event_id, run_id=run_id, type=event.type, node_id=event.node_id,
                               instance=event.instance, attempt=event.attempt, ts=stamp, detail=detail)

    def events(self, run_id: str, *, after: int | None = None, node_id: str | None = None,
               limit: int = EVENT_PAGE) -> list[NodeEventRecord]:
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if after is not None:
            clauses.append("id > ?")
            params.append(after)
        if node_id is not None:
            clauses.append("node_id = ?")
            params.append(node_id)
        rows = self.db.conn().execute(
            f"SELECT * FROM node_events WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?",
            (*params, max(limit, 0))).fetchall()
        return [NodeEventRecord(id=row["id"], run_id=row["run_id"], type=row["type"], node_id=row["node_id"],
                                instance=row["instance"], attempt=row["attempt"], ts=row["ts"],
                                detail=_load_dict(row["detail"]))
                for row in rows]

    def event_count(self, run_id: str) -> int:
        found = self.db.conn().execute("SELECT COUNT(*) AS n FROM node_events WHERE run_id = ?",
                                       (run_id,)).fetchone()
        return int(found["n"])

    def record_expansion(self, run_id: str, node_id: str, instance: str, kind: ExpansionKind,
                         detail: Mapping[str, JsonValue]) -> ExpansionRecord:
        record = ExpansionRecord(run_id=run_id, node_id=node_id, instance=instance, kind=kind,
                                 detail=dict(detail), created_at=time.time())
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO run_expansions (run_id, node_id, instance, kind, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (run_id, node_id, instance) DO UPDATE SET "
                "kind = excluded.kind, detail = excluded.detail, created_at = excluded.created_at",
                (run_id, node_id, instance, kind, _text(record.detail), record.created_at))
        return record

    def expansions(self, run_id: str) -> list[ExpansionRecord]:
        rows = self.db.conn().execute(
            "SELECT * FROM run_expansions WHERE run_id = ? ORDER BY created_at, node_id, instance",
            (run_id,)).fetchall()
        return [ExpansionRecord(run_id=row["run_id"], node_id=row["node_id"], instance=row["instance"],
                                kind=row["kind"], detail=_load_dict(row["detail"]), created_at=row["created_at"])
                for row in rows]

    def set_resource(self, record: ResourceRecord) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO run_resources (run_id, resource_id, kind, session_key, state, handle, detail, error, "
                "acquired_at, released_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (run_id, resource_id) DO UPDATE SET kind = excluded.kind, "
                "session_key = excluded.session_key, state = excluded.state, handle = excluded.handle, "
                "detail = excluded.detail, error = excluded.error, acquired_at = excluded.acquired_at, "
                "released_at = excluded.released_at",
                (record.run_id, record.resource_id, record.kind, _clean_optional(record.session_key), record.state,
                 _clean_optional(record.handle), _text(record.detail), _clean_optional(record.error),
                 record.acquired_at, record.released_at))

    def resources(self, run_id: str) -> list[ResourceRecord]:
        rows = self.db.conn().execute(
            "SELECT * FROM run_resources WHERE run_id = ? "
            "ORDER BY acquired_at IS NULL, acquired_at, resource_id", (run_id,)).fetchall()
        return [ResourceRecord(run_id=row["run_id"], resource_id=row["resource_id"], kind=row["kind"],
                               session_key=row["session_key"], state=row["state"], handle=row["handle"],
                               detail=_load_dict(row["detail"]), error=row["error"],
                               acquired_at=row["acquired_at"], released_at=row["released_at"])
                for row in rows]

    def unfinished(self) -> list[str]:
        terminal = sorted(TERMINAL_STATUSES)
        rows = self.db.conn().execute(
            f"SELECT id FROM runs WHERE status NOT IN ({placeholders(len(terminal))}) ORDER BY created_at, id",
            terminal).fetchall()
        return [row["id"] for row in rows]

    def prune(self, before: float) -> int:
        terminal = sorted(TERMINAL_STATUSES)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                f"DELETE FROM runs WHERE status IN ({placeholders(len(terminal))}) "
                "AND finished_at IS NOT NULL AND finished_at < ?",
                (*terminal, before))
        return cursor.rowcount


class RunJournal:
    def __init__(self, journal: Journal, publish: RunPublisher, info: RunInfo) -> None:
        self.journal = journal
        self.publish = publish
        self.info = info
        self._lock = _run_lock(info.id)

    def event(self, type: str, *, node_id: str | None = None, instance: str = "", attempt: int = 0,
              detail: Mapping[str, JsonValue] | None = None) -> NodeEventRecord:
        entry = NodeEventIn(type=type, node_id=node_id, instance=instance, attempt=attempt,
                            detail=dict(detail or {}))
        with self._lock:
            record = self.journal.append(self.info.id, entry)
            self.publish.journaled(self.info, record)
        return record

    def node(self, record: NodeRecord) -> None:
        with self._lock:
            self.journal.upsert_node(record)

    def status(self, status: RunStatus, *, reason: str | None = None, pause: PauseInfo | None = None) -> None:
        stored_reason = _clean_optional(reason)
        detail: dict[str, JsonValue] = {"reason": stored_reason, "pause": None if pause is None else _dump(pause)}
        with self._lock:
            with self.journal.db.transaction():
                state = self.journal._state(self.info.id)
                if state is None:
                    raise KeyError(f"no run {self.info.id!r} in the journal")
                previous, started_at = state
                self.journal.set_status(self.info.id, status, reason=stored_reason)
                record = self.journal.append(self.info.id, NodeEventIn(type=f"run.{status}", detail=detail))
            self.publish.journaled(self.info, record)
            activity: str | None = None
            if status == "running" and started_at is None:
                activity = "run.started"
            elif status == "running" and previous == "paused":
                activity = "run.resumed"
            elif status == "paused" and previous != "paused":
                activity = "run.paused"
            elif status in TERMINAL_STATUSES and previous not in TERMINAL_STATUSES:
                activity = "run.finished"
            if activity is not None:
                self.publish.activity(self.info, activity, {"status": status, "reason": stored_reason})

    def expansion(self, node_id: str, instance: str, kind: ExpansionKind,
                  detail: Mapping[str, JsonValue]) -> ExpansionRecord:
        with self._lock:
            with self.journal.db.transaction():
                record = self.journal.record_expansion(self.info.id, node_id, instance, kind, detail)
                event = self.journal.append(self.info.id, NodeEventIn(
                    type="node.expanded", node_id=node_id, instance=instance,
                    detail={"kind": kind, "detail": record.detail}))
            self.publish.journaled(self.info, event)
        return record

    def resource(self, record: ResourceRecord) -> None:
        dumped = cast("dict[str, JsonValue]", _dump(record))
        dumped.pop("runId", None)
        with self._lock:
            with self.journal.db.transaction():
                self.journal.set_resource(record)
                event = self.journal.append(self.info.id, NodeEventIn(type=f"resource.{record.state}",
                                                                      detail=dumped))
            self.publish.journaled(self.info, event)

    def progress(self, node_id: str, instance: str, attempt: int, fraction: float, message: str = "") -> None:
        self.publish.progress(self.info, {"node": node_id, "instance": instance, "attempt": attempt,
                                          "fraction": fraction, "message": _clean(message)})


class RunPublisher(Protocol):
    def journaled(self, info: RunInfo, record: NodeEventRecord) -> None: ...

    def activity(self, info: RunInfo, type: str, detail: Mapping[str, JsonValue]) -> None: ...

    def progress(self, info: RunInfo, detail: Mapping[str, JsonValue]) -> None: ...


def _size(value: Any) -> int:
    return len(_text(value).encode("utf-8"))


def _jsonable(value: Any) -> Any:
    if isinstance(value, str):
        return _clean(value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {_clean(str(key)): _jsonable(item) for key, item in cast("Mapping[Any, Any]", value).items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in cast("Sequence[Any]", value)]
    return _clean(str(value))


def _shrink(value: Any, text_cap: int, items_cap: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= text_cap else value[:text_cap] + _ELLIPSIS
    if isinstance(value, list):
        return [_shrink(item, text_cap, items_cap) for item in cast("list[Any]", value)[:items_cap]]
    if isinstance(value, dict):
        pairs = list(cast("dict[str, Any]", value).items())[:items_cap]
        return {key: _shrink(item, text_cap, items_cap) for key, item in pairs}
    return value


def _preview(detail: Any, limit: int) -> dict[str, JsonValue]:
    try:
        text = _clean(_text(detail))
    except (RecursionError, ValueError, TypeError):
        text = _clean(_PREVIEW.repr(detail))
    room = max(limit - _size({"truncated": True, "preview": _ELLIPSIS}), 0)
    while text and _size(text) > room:
        text = text[: len(text) * 3 // 4]
    return {"truncated": True, "preview": text + _ELLIPSIS}


def bounded_detail(detail: Mapping[str, JsonValue], limit: int = DETAIL_LIMIT_BYTES) -> dict[str, JsonValue]:
    try:
        plain = cast("dict[str, Any]", _jsonable(detail))
        if _size(plain) <= limit:
            return plain
        for text_cap, items_cap in _SHRINK_STEPS:
            shrunk = cast("dict[str, Any]", _shrink(plain, text_cap, items_cap))
            shrunk["truncated"] = True
            if _size(shrunk) <= limit:
                return shrunk
        return _preview(plain, limit)
    except (RecursionError, ValueError, TypeError):
        return _preview(detail, limit)


__all__ = [
    "DETAIL_LIMIT_BYTES",
    "EVENT_PAGE",
    "Journal",
    "RunJournal",
    "RunPublisher",
    "RunRow",
    "bounded_detail",
]
