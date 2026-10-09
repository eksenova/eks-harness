from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from eks_harness.api.schemas import StorageUsage
from eks_harness.config import Config
from eks_harness.daemon.events import EventBus
from eks_harness.db import Database
from eks_harness.db.common import now
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import projects as projects_repo
from eks_harness.paths import Paths
from eks_harness.store import deletion, layout, policy
from eks_harness.store import shares as store_shares

log = logging.getLogger("eks_harness.store.retention")

GB = 1024 ** 3
USAGE_CACHE_SECONDS = 60.0
TMP_MAX_AGE_SECONDS = 6 * 3600
TMP_PREFIXES = ("upload-", "stage-", "trash-", "zip-", "capture-")
RETENTION_BATCH = 500
SHARE_PRUNE_DAYS = 30.0


@dataclass
class RetentionReport:
    deleted: int = 0
    bytes: int = 0
    projects: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def default_retention_days(config: Config) -> int | None:
    default = config["retention.defaultDays"]
    return int(default) if default else None


def retention_days_for(project: projects_repo.Project, config: Config) -> int | None:
    return policy.project_days(project.retention_days, default_retention_days(config))


def run_retention(db: Database, config: Config, paths: Paths | None = None, *, events: EventBus | None = None,
                  at: float | None = None) -> RetentionReport:
    paths = paths or config.paths
    stamp = at or now()
    report = RetentionReport()
    for project in projects_repo.list_projects(db.conn()):
        days = retention_days_for(project, config)
        cutoff = stamp - days * 86400 if days else None
        guard = artifacts_repo.expired_clause(cutoff, stamp)
        while True:
            candidates = artifacts_repo.retention_candidates(db.conn(), project.id, cutoff, stamp,
                                                             limit=RETENTION_BATCH)
            if not candidates:
                break
            try:
                plan = deletion.delete_artifacts(db, paths, [a.id for a in candidates], events=events,
                                                 actor="retention", still_expired=guard)
            except Exception as problem:
                log.exception("retention failed in %s", project.id)
                report.errors[project.id] = f"{type(problem).__name__}: {problem}"
                break
            report.deleted += len(plan.artifact_ids)
            report.bytes += plan.bytes
            report.projects[project.id] = report.projects.get(project.id, 0) + len(plan.artifact_ids)
            if len(candidates) < RETENTION_BATCH or not plan.artifact_ids:
                break
    if report.deleted:
        log.info("retention deleted %d artifacts (%d bytes)", report.deleted, report.bytes)
    invalidate_usage()
    return report


_usage_lock = threading.Lock()
_usage_cache: dict[str, tuple[float, int]] = {}


def invalidate_usage() -> None:
    with _usage_lock:
        _usage_cache.clear()


def disk_usage(paths: Paths, max_age: float = USAGE_CACHE_SECONDS) -> int:
    key = str(paths.store_dir)
    with _usage_lock:
        cached = _usage_cache.get(key)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
    total = layout.dir_size(paths.store_dir) if paths.store_dir.exists() else 0
    with _usage_lock:
        _usage_cache[key] = (time.monotonic(), total)
    return total


def storage_usage(db: Database, config: Config, paths: Paths | None = None, *, fresh: bool = False) -> StorageUsage:
    paths = paths or config.paths
    conn = db.conn()
    artifact_bytes = artifacts_repo.total_size(conn)
    artifact_count = artifacts_repo.count(conn, artifacts_repo.ArtifactQuery())
    used = disk_usage(paths, 0 if fresh else USAGE_CACHE_SECONDS)
    quota_gb = config["storage.quotaGb"]
    quota = int(quota_gb) * GB if quota_gb else None
    try:
        free = shutil.disk_usage(paths.store_dir if paths.store_dir.exists() else paths.data_dir).free
    except OSError:
        free = None
    return StorageUsage(used_bytes=used, artifact_bytes=artifact_bytes, artifact_count=artifact_count,
                        quota_bytes=quota, over_quota=bool(quota is not None and used > quota),
                        free_disk_bytes=free)


def cleanup_tmp(paths: Paths, max_age_seconds: float = TMP_MAX_AGE_SECONDS,
                keep: Iterable[Path] = ()) -> int:
    root = paths.tmp_dir
    if not root.is_dir():
        return 0
    removed = 0
    cutoff = time.time() - max_age_seconds
    kept = {Path(p).resolve() for p in keep}
    for entry in root.iterdir():
        if not entry.name.startswith(TMP_PREFIXES):
            continue
        if any(entry.resolve() == k or entry.resolve() in k.parents for k in kept):
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        removed += 1
    return removed


def prune_shares(db: Database, older_than_days: float = SHARE_PRUNE_DAYS) -> int:
    return store_shares.prune(db, older_than_days)


def housekeeping(db: Database, config: Config, paths: Paths | None = None, *,
                 events: EventBus | None = None, keep_tmp: Iterable[Path] = ()) -> dict:
    paths = paths or config.paths
    report = run_retention(db, config, paths, events=events)
    return {
        "retention": {"deleted": report.deleted, "bytes": report.bytes, "projects": report.projects,
                      "errors": report.errors},
        "tmpRemoved": cleanup_tmp(paths, keep=keep_tmp),
        "sharesPruned": prune_shares(db),
    }
