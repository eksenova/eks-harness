from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from typing import Any, Protocol, cast

from pydantic import JsonValue, ValidationError

from eks_harness.engine.errors import RunNotFound
from eks_harness.engine.journal import EVENT_PAGE, Journal, RunRow
from eks_harness.engine.model import (
    ArtifactRef,
    CheckOutcome,
    CheckpointOut,
    CheckpointOutcome,
    Divergence,
    DivergenceField,
    FailureOut,
    IssueOut,
    JournalPage,
    JournalRef,
    NodeEventRecord,
    NodeRecord,
    NodeTiming,
    PauseInfo,
    PlanRef,
    RunComparison,
    RunLinks,
    RunReport,
    RunSummary,
    Timings,
)
from eks_harness.ir.hashing import canonical_json, digest
from eks_harness.ir.types import Artifact

API_PREFIX = "/api/runs"
SLOWEST_NODES = 20

_CHECKPOINT_OP = "check.checkpoint"
_FAILURE_SHOT = "node.failure_shot"
_WARNING = "run.warning"
_RUN_STATUS_EVENTS = frozenset({"run.queued", "run.preparing", "run.running", "run.paused", "run.passed",
                                "run.failed", "run.aborted", "run.error"})
_ISSUE_FIELDS = ("severity", "code", "message", "node", "field", "src")
_ARTIFACT_PREFIX = "artifact:"


class ArtifactLookup(Protocol):
    def get(self, artifact_id: str) -> Artifact | None: ...


class LinkBuilder(Protocol):
    def absolute(self, path: str) -> str: ...

    def session(self, project_id: str, slug: str | None) -> str: ...


def run_links(run_id: str, links: LinkBuilder, *, project: str | None = None,
              session_slug: str | None = None) -> RunLinks:
    base = f"{API_PREFIX}/{run_id}"
    return RunLinks(
        run=links.absolute(base),
        report=links.absolute(f"{base}/report"),
        journal=links.absolute(f"{base}/journal"),
        events=links.absolute(f"{base}/events"),
        plan=links.absolute(f"{base}/plan"),
        session=links.session(project, session_slug) if project else None,
    )


def summary(row: RunRow, links: LinkBuilder, *, pause: PauseInfo | None = None,
            session_slug: str | None = None) -> RunSummary:
    info = row.info
    project = info.project or None
    return RunSummary(
        id=info.id,
        status=row.status,
        reason=row.reason,
        plan=PlanRef(kind=info.plan_kind, name=info.plan_name, hash=info.plan_hash),
        project=project,
        session=info.session or None,
        tree=info.tree,
        spec=info.spec,
        actor=info.actor,
        instance=info.instance,
        created_at=info.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        counts=dict(row.counts),
        cache=row.cache,
        pause=pause,
        simulated=row.simulated,
        replay_of=row.replay_of,
        repeat_of=row.repeat_of,
        links=run_links(info.id, links, project=project, session_slug=session_slug),
    )


def artifact_ref(artifact: Artifact, *, node: str | None = None, instance: str = "") -> ArtifactRef:
    return ArtifactRef(id=artifact.id, kind=artifact.kind, name=artifact.name, url=artifact.url,
                       raw_url=artifact.raw_url, node=node, instance=instance)


class _Refs:
    def __init__(self, artifacts: ArtifactLookup) -> None:
        self.artifacts = artifacts
        self._found: dict[str, Artifact | None] = {}

    def lookup(self, artifact_id: str) -> Artifact | None:
        if artifact_id not in self._found:
            self._found[artifact_id] = self.artifacts.get(artifact_id)
        return self._found[artifact_id]

    def refs(self, ids: Iterable[str], node: str | None, instance: str) -> list[ArtifactRef]:
        found: list[ArtifactRef] = []
        seen: set[str] = set()
        for artifact_id in ids:
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            artifact = self.lookup(artifact_id)
            if artifact is not None:
                found.append(artifact_ref(artifact, node=node, instance=instance))
        return found


def _all_events(journal: Journal, run_id: str) -> list[NodeEventRecord]:
    found: list[NodeEventRecord] = []
    after: int | None = None
    while True:
        page = journal.events(run_id, after=after, limit=EVENT_PAGE)
        found.extend(page)
        if len(page) < EVENT_PAGE:
            return found
        after = page[-1].id


def _checkpoint(node: NodeRecord) -> CheckpointOutcome | None:
    raw: Any = None
    if node.has_output and isinstance(node.output, dict):
        raw = node.output
    elif node.error is not None:
        raw = node.error.detail.get("checkpoint")
    if not isinstance(raw, dict):
        return None
    try:
        return CheckpointOutcome.model_validate(raw)
    except ValidationError:
        return None


def _failure_shots(events: Sequence[NodeEventRecord]) -> dict[tuple[str, str], list[str]]:
    shots: dict[tuple[str, str], list[str]] = {}
    for event in events:
        if event.type != _FAILURE_SHOT or event.node_id is None:
            continue
        ids = event.detail.get("artifacts")
        if isinstance(ids, list):
            shots.setdefault((event.node_id, event.instance), []).extend(
                item for item in ids if isinstance(item, str))
    return shots


def _failures(row: RunRow, nodes: Sequence[NodeRecord], events: Sequence[NodeEventRecord],
              refs: _Refs) -> list[FailureOut]:
    shots = _failure_shots(events)
    failures: list[FailureOut] = []
    for node in nodes:
        if node.status not in ("failed", "soft_failed"):
            continue
        checkpoint = _checkpoint(node) if node.op == _CHECKPOINT_OP else None
        checks: list[CheckOutcome] = [] if checkpoint is None else [check for check in checkpoint.checks
                                                                    if not check.ok]
        shot_ids = [*shots.get((node.node_id, node.instance), []), *([] if checkpoint is None else checkpoint.shots)]
        error = node.error
        failures.append(FailureOut(
            node=node.node_id,
            instance=node.instance,
            op=node.op,
            label=node.label,
            src=node.src,
            code=error.code if error is not None else node.status,
            message=error.message if error is not None else f"{node.op} {node.node_id} failed without an error record",
            soft=node.status == "soft_failed",
            checks=checks,
            shots=refs.refs(shot_ids, node.node_id, node.instance),
            detail=dict(error.detail) if error is not None else {},
        ))
    if row.status in ("error", "aborted") and not any(node.status == "failed" for node in nodes):
        failures.append(FailureOut(
            node=None,
            code="run_error" if row.status == "error" else "run_aborted",
            message=row.reason or f"the run ended {row.status}",
        ))
    return failures


def _checkpoints(nodes: Sequence[NodeRecord], refs: _Refs) -> list[CheckpointOut]:
    found: list[CheckpointOut] = []
    for node in nodes:
        if node.op != _CHECKPOINT_OP:
            continue
        outcome = _checkpoint(node)
        if outcome is None:
            continue
        found.append(CheckpointOut(node=node.node_id, instance=node.instance, label=outcome.label,
                                   viewport=outcome.viewport, ok=outcome.ok, checks=outcome.checks,
                                   shots=refs.refs(outcome.shots, node.node_id, node.instance)))
    return found


def _artifacts(nodes: Sequence[NodeRecord], refs: _Refs) -> list[ArtifactRef]:
    found: list[ArtifactRef] = []
    seen: set[str] = set()
    for node in nodes:
        fresh = [artifact_id for artifact_id in node.artifacts if artifact_id not in seen]
        seen.update(fresh)
        found.extend(refs.refs(fresh, node.node_id, node.instance))
    return found


def _issue(detail: dict[str, JsonValue]) -> IssueOut:
    known = {key: detail[key] for key in _ISSUE_FIELDS if key in detail}
    known.setdefault("severity", "warning")
    try:
        return IssueOut.model_validate(known)
    except ValidationError:
        return IssueOut(severity="warning", code=str(detail.get("code") or "warning"),
                        message=str(detail.get("message") or json.dumps(detail, ensure_ascii=False, default=str)))


def _ms(seconds: float) -> int:
    return max(round(seconds * 1000), 0)


def _timings(row: RunRow, nodes: Sequence[NodeRecord], events: Sequence[NodeEventRecord]) -> Timings:
    created = row.info.created_at
    end = row.finished_at if row.finished_at is not None else time.time()
    statuses = [event for event in events if event.node_id is None and event.type in _RUN_STATUS_EVENTS]
    preparing = next((event.ts for event in statuses if event.type == "run.preparing"), None)
    running = next((event.ts for event in statuses if event.type == "run.running"), None)
    total = _ms(end - created)
    queued = total if preparing is None else _ms(preparing - created)
    if preparing is None:
        prepared = 0
    elif running is None:
        prepared = _ms(end - preparing)
    else:
        prepared = _ms(running - preparing)
    paused = 0
    for index, event in enumerate(statuses):
        if event.type != "run.paused":
            continue
        following = statuses[index + 1].ts if index + 1 < len(statuses) else end
        paused += _ms(following - event.ts)
    timed = sorted((node for node in nodes if node.duration_ms is not None),
                   key=lambda node: (-(node.duration_ms or 0), node.seq))
    return Timings(
        total_ms=total,
        queued_ms=min(queued, total),
        preparing_ms=prepared,
        running_ms=max(total - queued - prepared, 0),
        paused_ms=paused,
        nodes=[NodeTiming(node=node.node_id, instance=node.instance, op=node.op, ms=node.duration_ms or 0,
                          cache=node.cache)
               for node in timed[:SLOWEST_NODES]],
    )


def _load_row(journal: Journal, run_id: str) -> RunRow:
    row = journal.run(run_id)
    if row is None:
        raise RunNotFound(f"run {run_id} not found", detail={"run": run_id})
    return row


def build_report(journal: Journal, run_id: str, links: LinkBuilder, artifacts: ArtifactLookup, *,
                 pause: PauseInfo | None = None, session_slug: str | None = None) -> RunReport:
    row = _load_row(journal, run_id)
    nodes = journal.nodes(run_id)
    events = _all_events(journal, run_id)
    refs = _Refs(artifacts)
    return RunReport(
        run=summary(row, links, pause=pause, session_slug=session_slug),
        failures=_failures(row, nodes, events, refs),
        checkpoints=_checkpoints(nodes, refs),
        artifacts=_artifacts(nodes, refs),
        values=row.outputs if row.has_outputs else None,
        timings=_timings(row, nodes, events),
        cache=row.cache,
        resources=journal.resources(run_id),
        environment=row.environment,
        issues=[_issue(event.detail) for event in events if event.type == _WARNING and event.node_id is None],
        journal=JournalRef(run_id=run_id, nodes=len(nodes), events=len(events)),
    )


def journal_page(journal: Journal, run_id: str, links: LinkBuilder, *, events: bool = False,
                 after: int | None = None, limit: int = 500, pause: PauseInfo | None = None,
                 session_slug: str | None = None) -> JournalPage:
    row = _load_row(journal, run_id)
    found: list[NodeEventRecord] = []
    next_after: int | None = None
    if events:
        page = journal.events(run_id, after=after, limit=limit + 1)
        found = page[:limit]
        if len(page) > limit and found:
            next_after = found[-1].id
    return JournalPage(
        run=summary(row, links, pause=pause, session_slug=session_slug),
        nodes=journal.nodes(run_id),
        expansions=journal.expansions(run_id),
        resources=journal.resources(run_id),
        events=found,
        next_after=next_after,
    )


def _as_artifact(value: dict[str, Any]) -> Artifact | None:
    if "id" not in value or "kind" not in value:
        return None
    try:
        return Artifact.model_validate(value)
    except ValidationError:
        return None


def _normalize(value: Any, own: dict[str, int]) -> Any:
    if isinstance(value, dict):
        mapping = cast("dict[str, Any]", value)
        artifact = _as_artifact(mapping)
        if artifact is not None and artifact.sha256 is not None:
            return artifact.sha256
        return {key: _normalize(item, own) for key, item in mapping.items()}
    if isinstance(value, list):
        return [_normalize(item, own) for item in cast("list[Any]", value)]
    if isinstance(value, str):
        bare = value.removeprefix(_ARTIFACT_PREFIX)
        if bare in own:
            return f"artifact#{own[bare]}"
    return value


def _output_digest(node: NodeRecord) -> str:
    if not node.has_output:
        return "absent"
    own: dict[str, int] = {}
    for index, artifact_id in enumerate(node.artifacts):
        own.setdefault(artifact_id, index)
    normalized = _normalize(node.output, own)
    try:
        return digest(canonical_json(normalized))
    except (TypeError, ValueError):
        return digest(json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))


def _diverge(node: NodeRecord | None, other: NodeRecord | None) -> list[Divergence]:
    present = node if node is not None else other
    if present is None:
        return []

    def make(field: DivergenceField, a: JsonValue, b: JsonValue) -> Divergence:
        return Divergence(node=present.node_id, instance=present.instance, op=present.op, field=field, a=a, b=b)

    if node is None or other is None:
        return [make("status", None if node is None else node.status, None if other is None else other.status)]
    found: list[Divergence] = []
    if node.status != other.status:
        found.append(make("status", node.status, other.status))
    if _output_digest(node) != _output_digest(other):
        found.append(make("output", node.output if node.has_output else None,
                          other.output if other.has_output else None))
    code_a = None if node.error is None else node.error.code
    code_b = None if other.error is None else other.error.code
    if code_a != code_b:
        found.append(make("error", code_a, code_b))
    if len(node.artifacts) != len(other.artifacts):
        found.append(make("artifacts", len(node.artifacts), len(other.artifacts)))
    return found


def compare_runs(a: str, a_nodes: Sequence[NodeRecord], b: str, b_nodes: Sequence[NodeRecord]) -> RunComparison:
    left = {(node.node_id, node.instance): node for node in sorted(a_nodes, key=lambda node: node.seq)}
    right = {(node.node_id, node.instance): node for node in sorted(b_nodes, key=lambda node: node.seq)}
    diverging: list[Divergence] = []
    for key, node in left.items():
        diverging.extend(_diverge(node, right.get(key)))
    for key, node in right.items():
        if key not in left:
            diverging.extend(_diverge(None, node))
    return RunComparison(a=a, b=b, same=not diverging, diverging=diverging)


__all__ = [
    "API_PREFIX",
    "SLOWEST_NODES",
    "ArtifactLookup",
    "LinkBuilder",
    "artifact_ref",
    "build_report",
    "compare_runs",
    "journal_page",
    "run_links",
    "summary",
]
