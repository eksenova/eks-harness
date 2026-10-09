from __future__ import annotations

import sqlite3
from datetime import datetime

from eks_harness.api.errors import bad_request, gone, not_found
from eks_harness.api.links import Links
from eks_harness.api.schemas import ShareOut, datetime_to_ts, ts_to_datetime
from eks_harness.daemon import events as ev
from eks_harness.daemon.events import EventBus
from eks_harness.db import Database
from eks_harness.db.common import now
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import shares as shares_repo
from eks_harness.db.repos.artifacts import Artifact
from eks_harness.db.repos.shares import Share
from eks_harness.ids import is_share_token, new_share_token
from eks_harness.store import artifacts as store_artifacts

UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
MAX_EXPIRY_SECONDS = 5 * 365 * 86400


def expiry_seconds(text: str) -> int:
    value = text.strip().lower()
    if len(value) < 2 or value[-1] not in UNIT_SECONDS or not value[:-1].isdigit() or int(value[:-1]) <= 0:
        raise bad_request("expires is 1h, 1d, 7d, 30d, another <n>m|h|d|w, or never.", error="invalid_expiry")
    seconds = int(value[:-1]) * UNIT_SECONDS[value[-1]]
    if seconds > MAX_EXPIRY_SECONDS:
        raise bad_request("A share link can live at most five years.", error="invalid_expiry")
    return seconds


def resolve_expiry(expires: str | None, expires_at: datetime | None, at: float | None = None) -> float | None:
    stamp = at or now()
    if expires_at is not None:
        value = datetime_to_ts(expires_at)
        if value <= stamp:
            raise bad_request("expiresAt must be in the future.", error="invalid_expiry")
        if value - stamp > MAX_EXPIRY_SECONDS:
            raise bad_request("A share link can live at most five years.", error="invalid_expiry")
        return value
    if not expires or expires.strip().lower() in ("never", "none"):
        return None
    return stamp + expiry_seconds(expires)


def direct_path(artifact: Artifact) -> str:
    return store_artifacts.site_entry(artifact) or artifact.filename


def direct_url(links: Links, share: Share, artifact: Artifact) -> str:
    return links.share_direct(share.token, direct_path(artifact))


def share_out(links: Links, share: Share, artifact: Artifact | None) -> ShareOut:
    return ShareOut(token=share.token, artifact_id=share.artifact_id, url=links.share(share.token),
                    raw_url=links.share_raw(share.token),
                    direct_url=direct_url(links, share, artifact) if artifact is not None else None,
                    created_by=share.created_by,
                    created_at=ts_to_datetime(share.created_at), expires_at=ts_to_datetime(share.expires_at),
                    revoked_at=ts_to_datetime(share.revoked_at), views=share.views,
                    last_viewed_at=ts_to_datetime(share.last_viewed_at), active=share.active())


def create(db: Database, artifact: Artifact, *, created_by: str | None, expires_at: float | None,
           events: EventBus | None = None) -> Share:
    with db.transaction() as conn:
        while True:
            token = new_share_token()
            if shares_repo.get(conn, token) is None:
                break
        share = shares_repo.create(conn, token, artifact.id, created_by, expires_at)
    if events is not None:
        events.publish(ev.SHARE_CREATED, session_id=artifact.session_id, project_id=artifact.project_id,
                       lease_sid=artifact.lease_sid, actor=created_by,
                       detail={"artifactId": artifact.id, "token": share.token[:6], "expiresAt": expires_at})
    return share


def revoke(db: Database, share: Share, artifact: Artifact | None, *, events: EventBus | None = None,
           actor: str | None = None) -> Share:
    with db.transaction() as conn:
        shares_repo.revoke(conn, share.token)
        updated = shares_repo.get(conn, share.token)
    if events is not None:
        events.publish(ev.SHARE_REVOKED, session_id=artifact.session_id if artifact else None,
                       project_id=artifact.project_id if artifact else None, actor=actor,
                       detail={"artifactId": share.artifact_id, "token": share.token[:6]})
    return updated


def resolve(conn: sqlite3.Connection, token: str) -> tuple[Share, Artifact]:
    share = shares_repo.get(conn, token) if is_share_token(token) else None
    if share is None:
        raise not_found("This share link does not exist.", error="share_not_found")
    if share.revoked_at is not None:
        raise gone("This share link was revoked.", error="share_revoked")
    if share.expires_at is not None and share.expires_at <= now():
        raise gone("This share link has expired.", error="share_expired")
    artifact = artifacts_repo.get(conn, share.artifact_id)
    if artifact is None:
        raise gone("The shared file was deleted.", error="share_target_deleted")
    return share, artifact


def record_view(db: Database, token: str) -> None:
    with db.transaction() as conn:
        shares_repo.record_view(conn, token)


def prune(db: Database, older_than_days: float = 30.0) -> int:
    with db.transaction() as conn:
        return shares_repo.delete_inactive_before(conn, now() - older_than_days * 86400)
