from __future__ import annotations

import hashlib
import io
import logging
import os
import secrets
import shutil
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from eks_harness.api.errors import ApiError, bad_request, too_large
from eks_harness.api.links import Links
from eks_harness.api.schemas import ArtifactOut, ts_to_datetime
from eks_harness.auth.core import Principal
from eks_harness.config import Config
from eks_harness.daemon import events as ev
from eks_harness.daemon.events import EventBus
from eks_harness.db import Database
from eks_harness.db.common import UNSET, now, placeholders
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import projects as projects_repo
from eks_harness.db.repos import seen as seen_repo
from eks_harness.db.repos import sessions as sessions_repo
from eks_harness.db.repos import tags as tags_repo
from eks_harness.db.repos.artifacts import Artifact
from eks_harness.ids import new_ulid
from eks_harness.paths import Paths
from eks_harness.store import access, layout, media, sites
from eks_harness.store.policy import retention_rule
from eks_harness.store.access import Target
from eks_harness.store.multipart import UploadedFile

log = logging.getLogger("eks_harness.store.artifacts")

MB = 1024 * 1024
COPY_CHUNK = 1024 * 1024
DEFAULT_FILENAMES = {
    "screenshot": "screenshot.png",
    "video": "video.mp4",
    "dom": "dom.html",
    "mhtml": "page.mhtml",
    "a11y": "accessibility.txt",
    "har": "network.har",
    "console": "console.log",
    "log": "device.log",
}
SITE_EXPANSION_FACTOR = 4

Source = Path | str | bytes | BinaryIO


@dataclass
class Staged:
    dir: Path
    filename: str
    size: int
    sha256: str
    mime: str
    kind: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    site: sites.SiteInfo | None = None
    thumbnail: bool = False
    extra_meta: dict[str, Any] = field(default_factory=dict)

    def discard(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def max_upload_bytes(config: Config) -> int:
    return int(config["storage.maxUploadMb"]) * MB


def _staging_dir(paths: Paths) -> Path:
    paths.tmp_dir.mkdir(parents=True, exist_ok=True)
    directory = paths.tmp_dir / f"stage-{os.getpid()}-{secrets.token_hex(8)}"
    directory.mkdir()
    return directory


def _limit_error(max_bytes: int) -> ApiError:
    return too_large(f"The file is larger than storage.maxUploadMb ({max_bytes // MB} MB).", limit_bytes=max_bytes)


def _copy_stream(source: BinaryIO, dest: Path, max_bytes: int) -> tuple[int, str, bytes]:
    digest = hashlib.sha256()
    size = 0
    head = b""
    with open(dest, "wb") as sink:
        while True:
            chunk = source.read(COPY_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise _limit_error(max_bytes)
            if len(head) < 64:
                head = (head + chunk)[:64]
            digest.update(chunk)
            sink.write(chunk)
    return size, digest.hexdigest(), head


def _hash_file(path: Path) -> tuple[int, str, bytes]:
    digest = hashlib.sha256()
    size = 0
    head = b""
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(COPY_CHUNK)
            if not chunk:
                break
            if len(head) < 64:
                head = (head + chunk)[:64]
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest(), head


def _source_name(source: Source) -> str | None:
    if isinstance(source, (str, Path)):
        return Path(source).name
    name = getattr(source, "name", None)
    return Path(name).name if isinstance(name, str) else None


def stage_source(paths: Paths, source: Source, filename: str, max_bytes: int,
                 move: bool = False) -> tuple[Path, Path, int, str, bytes]:
    directory = _staging_dir(paths)
    dest = directory / filename
    try:
        if isinstance(source, (str, Path)):
            path = Path(source)
            if not path.is_file():
                raise bad_request(f"{path} is not a file.", error="not_a_file")
            if path.stat().st_size > max_bytes:
                raise _limit_error(max_bytes)
            if move:
                shutil.move(str(path), dest)
                size, sha256, head = _hash_file(dest)
            else:
                with open(path, "rb") as handle:
                    size, sha256, head = _copy_stream(handle, dest, max_bytes)
        elif isinstance(source, (bytes, bytearray, memoryview)):
            size, sha256, head = _copy_stream(io.BytesIO(bytes(source)), dest, max_bytes)
        else:
            size, sha256, head = _copy_stream(source, dest, max_bytes)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory, dest, size, sha256, head


def prepare(directory: Path, file: Path, *, filename: str, size: int, sha256: str, head: bytes,
            declared_mime: str | None, kind: str | None, meta: dict | None) -> Staged:
    mime = media.resolve_mime(declared_mime, filename, head)
    try:
        chosen_kind = media.normalize_kind(kind) or media.infer_kind(mime, filename)
    except ValueError as problem:
        raise bad_request(str(problem), error="invalid_kind") from None
    if chosen_kind == "site":
        raise bad_request("Upload sites through POST /api/sites (eks-harness site upload).", error="use_site_upload")
    mime = media.mime_for_kind(chosen_kind, mime, filename)
    staged = Staged(dir=directory, filename=filename, size=size, sha256=sha256, mime=mime, kind=chosen_kind)
    probe = media.probe(file, mime)
    staged.width, staged.height, staged.duration_ms = probe.width, probe.height, probe.duration_ms
    staged.thumbnail = media.make_thumbnail(file, directory / layout.THUMB_NAME, mime)
    staged.extra_meta.update(media.content_counts(file, chosen_kind, mime, size))
    if chosen_kind == "mhtml":
        try:
            staged.site = sites.unpack_mhtml(file, directory / layout.SITE_DIR)
        except (sites.SiteError, ValueError, LookupError) as problem:
            log.warning("could not unpack MHTML %s: %s", filename, problem)
            staged.extra_meta["siteError"] = str(problem)
    elif chosen_kind == "dom":
        base_url = (meta or {}).get("url") if isinstance((meta or {}).get("url"), str) else None
        staged.site = sites.dom_site(file, directory / layout.SITE_DIR, base_url)
    return staged


def _principal(user: Principal | str | None) -> Principal | None:
    return user if isinstance(user, Principal) else None


def _browser(cfg: Config) -> str | None:
    value = cfg["browser.command"]
    return str(value) if value else None


def _actor(user: Principal | str | None) -> str | None:
    if isinstance(user, Principal):
        return user.username
    return user or None


def _clean_meta(meta: dict | None) -> dict:
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        raise bad_request("meta must be a JSON object.", error="invalid_meta")
    return dict(meta)


def commit(db: Database, paths: Paths, staged: Staged, *, project: str | None = None, session: str | None = None,
           sid: str | None = None, target: Target | None = None, caption: str = "",
           tags: Iterable[str] | None = None, meta: dict | None = None, source: str = "agent",
           user: Principal | str | None = None, events: EventBus | None = None,
           browser: str | None = None) -> Artifact:
    if source not in artifacts_repo.SOURCES:
        raise bad_request(f"source must be one of {', '.join(artifacts_repo.SOURCES)}.", error="invalid_source")
    try:
        clean_tags = artifacts_repo.normalize_tags(tags)
    except ValueError as problem:
        raise bad_request(str(problem), error="invalid_tag") from None
    final_meta = _clean_meta(meta)
    final_meta.update(staged.extra_meta)
    if staged.site is not None:
        final_meta["site"] = staged.site.meta()
        site_url = staged.site.extra.get("url")
        if site_url and "url" not in final_meta:
            final_meta["url"] = site_url
    final_dir: Path | None = None
    try:
        with db.transaction() as conn:
            resolved = target or access.resolve_target(conn, project=project, session=session, sid=sid,
                                                       principal=_principal(user), required="editor")
            if projects_repo.get(conn, resolved.project.id) is None or (
                    resolved.session_id is not None and sessions_repo.get(conn, resolved.session_id) is None):
                raise ApiError(409, "target_deleted", "The project or session was deleted during the upload.")
            lease_kind = resolved.lease.kind if resolved.lease else None
            all_tags = list(dict.fromkeys([*tags_repo.builtin_tags_for(lease_kind, browser), *clean_tags]))
            artifact_id = new_ulid()
            rel_path = layout.artifact_rel_path(resolved.project.id, resolved.session_slug, artifact_id,
                                                staged.filename)
            final_dir = layout.artifact_dir(paths, rel_path)
            layout.place_dir(staged.dir, final_dir)
            stamp = now()
            artifact = artifacts_repo.insert(
                conn, artifact_id=artifact_id, project_id=resolved.project.id, session_id=resolved.session_id,
                lease_sid=resolved.lease_sid, kind=staged.kind, filename=staged.filename, rel_path=rel_path,
                mime=staged.mime, size=staged.size, sha256=staged.sha256, width=staged.width,
                height=staged.height, duration_ms=staged.duration_ms, caption=(caption or "").strip(),
                source=source, created_by=_actor(user), created_at=stamp, meta=final_meta, tags=all_tags)
            if resolved.session_id is not None:
                sessions_repo.touch(conn, resolved.session_id, stamp, project_id=resolved.project.id)
            else:
                projects_repo.touch(conn, resolved.project.id, stamp)
    except BaseException:
        if final_dir is not None and final_dir.exists() and not staged.dir.exists():
            shutil.rmtree(final_dir, ignore_errors=True)
        staged.discard()
        raise
    if events is not None:
        publish_created(events, artifact, resolved, _actor(user))
    return artifact


def publish_created(events: EventBus, artifact: Artifact, target: Target, actor: str | None) -> None:
    try:
        if target.created_project:
            events.publish(ev.PROJECT_UPDATED, project_id=target.project.id, actor=actor,
                           detail={"project": target.project.id, "created": True, "implicit": True})
        events.publish(ev.ARTIFACT_CREATED, resource=target.lease.resource if target.lease else None,
                       lease_sid=artifact.lease_sid, session_id=artifact.session_id, project_id=artifact.project_id,
                       actor=actor, detail={"id": artifact.id, "kind": artifact.kind, "filename": artifact.filename,
                                            "size": artifact.size, "caption": artifact.caption,
                                            "sessionSlug": target.session_slug})
    except sqlite3.Error:
        log.exception("could not record the artifact.created event for %s", artifact.id)


def ingest_file(db: Database, cfg: Config, *, project: str | None = None, session: str | None = None,
                sid: str | None = None, path_or_stream: Source, kind: str | None = None, caption: str = "",
                tags: Iterable[str] | None = None, meta: dict | None = None, source: str = "agent",
                user: Principal | str | None = None, filename: str | None = None, mime: str | None = None,
                events: EventBus | None = None, move: bool = False, max_bytes: int | None = None,
                target: Target | None = None) -> Artifact:
    paths = cfg.paths
    limit = max_bytes if max_bytes is not None else max_upload_bytes(cfg)
    try:
        normalized_kind = media.normalize_kind(kind)
    except ValueError as problem:
        raise bad_request(str(problem), error="invalid_kind") from None
    fallback = DEFAULT_FILENAMES.get(normalized_kind or "", "file")
    name = layout.sanitize_filename(filename or _source_name(path_or_stream), fallback)
    if target is None:
        with db.transaction() as conn:
            target = access.resolve_target(conn, project=project, session=session, sid=sid,
                                           principal=_principal(user), required="editor")
    directory, file, size, sha256, head = stage_source(paths, path_or_stream, name, limit, move=move)
    try:
        staged = prepare(directory, file, filename=name, size=size, sha256=sha256, head=head, declared_mime=mime,
                         kind=normalized_kind, meta=meta)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return commit(db, paths, staged, target=target, caption=caption, tags=tags, meta=meta, source=source,
                  user=user, events=events, browser=_browser(cfg))


def ingest_upload(db: Database, cfg: Config, uploaded: UploadedFile, *, target: Target,
                  kind: str | None = None, caption: str = "", tags: Iterable[str] | None = None,
                  meta: dict | None = None, source: str = "cli", user: Principal | str | None = None,
                  filename: str | None = None, events: EventBus | None = None) -> Artifact:
    try:
        normalized_kind = media.normalize_kind(kind)
    except ValueError as problem:
        raise bad_request(str(problem), error="invalid_kind") from None
    fallback = DEFAULT_FILENAMES.get(normalized_kind or "", "file")
    name = layout.sanitize_filename(filename or uploaded.filename, fallback)
    directory = _staging_dir(cfg.paths)
    file = directory / name
    try:
        os.rename(uploaded.path, file)
        staged = prepare(directory, file, filename=name, size=uploaded.size, sha256=uploaded.sha256,
                         head=uploaded.head, declared_mime=uploaded.content_type, kind=normalized_kind, meta=meta)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return commit(db, cfg.paths, staged, target=target, caption=caption, tags=tags, meta=meta, source=source,
                  user=user, events=events, browser=_browser(cfg))


def ingest_site(db: Database, cfg: Config, *, target: Target | None = None, project: str | None = None,
                session: str | None = None, sid: str | None = None, zip_path: Path | None = None,
                files: Sequence[tuple[str, Path]] | None = None, entry: str = "index.html",
                name: str | None = None, caption: str = "", tags: Iterable[str] | None = None,
                meta: dict | None = None, source: str = "cli", user: Principal | str | None = None,
                events: EventBus | None = None) -> Artifact:
    if (zip_path is None) == (not files):
        raise bad_request("Send either one zip file or files[] with relative paths.", error="bad_site_upload")
    paths = cfg.paths
    limit = max_upload_bytes(cfg)
    if target is None:
        with db.transaction() as conn:
            target = access.resolve_target(conn, project=project, session=session, sid=sid,
                                           principal=_principal(user), required="editor")
    directory = _staging_dir(paths)
    site_root = directory / layout.SITE_DIR
    try:
        if zip_path is not None:
            filename = sites.site_name(name or Path(zip_path).name)
            info = sites.extract_zip(Path(zip_path), site_root, entry, limit * SITE_EXPANSION_FACTOR)
            stored = directory / filename
            shutil.move(str(zip_path), stored)
            size, sha256, _ = _hash_file(stored)
        else:
            top = next((p.replace("\\", "/").split("/", 1)[0] for p, _ in files if "/" in p.replace("\\", "/")), None)
            filename = sites.site_name(name or top or "site")
            info = sites.place_files(list(files), site_root, entry)
            stored = directory / filename
            size, sha256 = sites.zip_directory(site_root, stored)
    except sites.SiteError as problem:
        shutil.rmtree(directory, ignore_errors=True)
        raise bad_request(str(problem), error=problem.error) from None
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    staged = Staged(dir=directory, filename=filename, size=size, sha256=sha256, mime="application/zip", kind="site",
                    site=info)
    return commit(db, paths, staged, target=target, caption=caption, tags=tags, meta=meta, source=source, user=user,
                  events=events, browser=_browser(cfg))


def active_share_counts(conn: sqlite3.Connection, artifact_ids: Sequence[str]) -> dict[str, int]:
    ids = list(dict.fromkeys(artifact_ids))
    counts: dict[str, int] = {}
    stamp = now()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for row in conn.execute(
                f"SELECT artifact_id, COUNT(*) FROM shares WHERE artifact_id IN ({placeholders(len(chunk))}) "
                f"AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) GROUP BY artifact_id",
                [*chunk, stamp]):
            counts[row[0]] = row[1]
    return counts


def has_thumbnail(paths: Paths, artifact: Artifact) -> bool:
    try:
        return layout.thumb_path(paths, artifact.rel_path).is_file()
    except layout.UnsafePath:
        return False


def site_entry(artifact: Artifact) -> str | None:
    site = artifact.meta.get("site") if isinstance(artifact.meta, dict) else None
    if isinstance(site, dict) and isinstance(site.get("entry"), str):
        return site["entry"]
    return None


def public_meta(artifact: Artifact, compact: bool = False) -> dict:
    meta = dict(artifact.meta or {})
    site = meta.get("site")
    if compact and isinstance(site, dict) and "files" in site:
        meta["site"] = {k: v for k, v in site.items() if k != "files"}
    return meta


def to_out(conn: sqlite3.Connection, links: Links, paths: Paths, artifact: Artifact, *, user_id: int | None = None,
           seen: bool | None = None, share_count: int | None = None, with_neighbours: bool = False,
           compact: bool = False, default_retention_days: Any = UNSET) -> ArtifactOut:
    if seen is None:
        seen = bool(user_id is not None and seen_repo.is_seen(conn, user_id, artifact.id))
    if share_count is None:
        share_count = active_share_counts(conn, [artifact.id]).get(artifact.id, 0)
    previous_id = next_id = None
    if with_neighbours:
        previous_id, next_id = artifacts_repo.neighbours(conn, artifact)
    entry = site_entry(artifact)
    effective_days = retention_source = expires_at = None
    if default_retention_days is not UNSET:
        project = projects_repo.get(conn, artifact.project_id)
        effective_days, retention_source = retention_rule(
            artifact.retention_days, project.retention_days if project else None, default_retention_days,
            artifact.pinned)
        if effective_days:
            expires_at = ts_to_datetime(artifact.created_at + effective_days * 86400)
    return ArtifactOut(
        id=artifact.id,
        url=links.artifact(artifact.project_id, artifact.session_slug, artifact.id),
        raw_url=links.raw(artifact.id, artifact.filename),
        session_url=links.session(artifact.project_id, artifact.session_slug),
        shared_session_url=links.shared_session(artifact.session_slug) if artifact.session_slug else None,
        download_url=links.download(artifact.id, artifact.filename),
        kind=artifact.kind,
        size=artifact.size,
        caption=artifact.caption,
        project_id=artifact.project_id,
        session_id=artifact.session_id,
        session_slug=artifact.session_slug,
        session_name=artifact.session_name,
        lease_sid=artifact.lease_sid,
        mime=artifact.mime,
        filename=artifact.filename,
        sha256=artifact.sha256,
        width=artifact.width,
        height=artifact.height,
        duration_ms=artifact.duration_ms,
        pinned=artifact.pinned,
        source=artifact.source,
        created_by=artifact.created_by,
        created_at=ts_to_datetime(artifact.created_at),
        meta=public_meta(artifact, compact),
        tags=list(artifact.tags),
        retention_days=artifact.retention_days,
        effective_retention_days=effective_days,
        retention_source=retention_source,
        expires_at=expires_at,
        seen=seen,
        thumbnail_url=links.thumbnail(artifact.id) if has_thumbnail(paths, artifact) else None,
        site_url=links.site(artifact.id, entry) if entry else None,
        share_count=share_count,
        previous_id=previous_id,
        next_id=next_id,
    )


def to_out_many(conn: sqlite3.Connection, links: Links, paths: Paths, items: Sequence[Artifact],
                user_id: int | None = None) -> list[ArtifactOut]:
    ids = [a.id for a in items]
    seen_set = seen_repo.seen_ids(conn, user_id, ids) if user_id is not None else set()
    shares = active_share_counts(conn, ids)
    return [to_out(conn, links, paths, a, user_id=user_id, seen=a.id in seen_set, share_count=shares.get(a.id, 0),
                   compact=True) for a in items]


def update(db: Database, artifact: Artifact, *, caption: Any = UNSET, pinned: Any = UNSET,
           tags: Sequence[str] | None = None, add_tags: Sequence[str] | None = None,
           remove_tags: Sequence[str] | None = None, meta: dict | None = None,
           retention_days: Any = UNSET, events: EventBus | None = None, actor: str | None = None) -> Artifact:
    changed: list[str] = []
    try:
        with db.transaction() as conn:
            fields: dict[str, Any] = {}
            if caption is not UNSET and caption is not None and caption.strip() != artifact.caption:
                fields["caption"] = caption.strip()
                changed.append("caption")
            if pinned is not UNSET and pinned is not None and bool(pinned) != artifact.pinned:
                fields["pinned"] = bool(pinned)
                changed.append("pinned")
            if retention_days is not UNSET and retention_days != artifact.retention_days:
                fields["retention_days"] = retention_days
                changed.append("retention")
            if meta is not None:
                merged = dict(artifact.meta or {})
                for key, value in meta.items():
                    if value is None:
                        merged.pop(key, None)
                    else:
                        merged[key] = value
                if merged != artifact.meta:
                    fields["meta"] = merged
                    changed.append("meta")
            if fields:
                artifacts_repo.update(conn, artifact.id, **fields)
            if tags is not None:
                if sorted(artifacts_repo.normalize_tags(tags)) != sorted(artifact.tags):
                    artifacts_repo.set_tags(conn, artifact.id, tags)
                    changed.append("tags")
            if add_tags:
                if artifacts_repo.add_tags(conn, [artifact.id], add_tags):
                    changed.append("tags")
            if remove_tags:
                if artifacts_repo.remove_tags(conn, [artifact.id], remove_tags):
                    changed.append("tags")
            updated = artifacts_repo.get(conn, artifact.id)
    except ValueError as problem:
        raise bad_request(str(problem), error="invalid_tag") from None
    if changed and events is not None:
        events.publish(ev.ARTIFACT_UPDATED, lease_sid=updated.lease_sid, session_id=updated.session_id,
                       project_id=updated.project_id, actor=actor,
                       detail={"id": updated.id, "changed": sorted(set(changed)), "pinned": updated.pinned,
                               "caption": updated.caption, "tags": list(updated.tags),
                               "retentionDays": updated.retention_days})
    return updated


def bulk_retention(db: Database, items: Sequence[Artifact], retention_days: int | None,
                   events: EventBus | None = None, actor: str | None = None) -> int:
    changed = [a for a in items if a.retention_days != retention_days]
    with db.transaction() as conn:
        for artifact in changed:
            artifacts_repo.update(conn, artifact.id, retention_days=retention_days)
    if events is not None:
        for artifact in changed:
            events.publish(ev.ARTIFACT_UPDATED, lease_sid=artifact.lease_sid, session_id=artifact.session_id,
                           project_id=artifact.project_id, actor=actor,
                           detail={"id": artifact.id, "changed": ["retention"], "retentionDays": retention_days})
    return len(changed)


def bulk_tag(db: Database, items: Sequence[Artifact], add: Sequence[str], remove: Sequence[str],
             events: EventBus | None = None, actor: str | None = None) -> int:
    ids = [a.id for a in items]
    try:
        with db.transaction() as conn:
            changed = artifacts_repo.add_tags(conn, ids, add) if add else 0
            changed += artifacts_repo.remove_tags(conn, ids, remove) if remove else 0
            refreshed = artifacts_repo.get_many(conn, ids)
    except ValueError as problem:
        raise bad_request(str(problem), error="invalid_tag") from None
    if changed and events is not None:
        for artifact in refreshed:
            events.publish(ev.ARTIFACT_UPDATED, lease_sid=artifact.lease_sid, session_id=artifact.session_id,
                           project_id=artifact.project_id, actor=actor,
                           detail={"id": artifact.id, "changed": ["tags"], "tags": list(artifact.tags)})
    return changed
