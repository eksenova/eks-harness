from __future__ import annotations

from collections.abc import Iterable

from eks_harness.db import Database
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import seen as seen_repo


def set_seen(db: Database, user_id: int, artifact_ids: Iterable[str], seen: bool = True) -> int:
    ids = list(dict.fromkeys(artifact_ids))
    with db.transaction() as conn:
        return seen_repo.mark(conn, user_id, ids) if seen else seen_repo.unmark(conn, user_id, ids)


def set_session_seen(db: Database, user_id: int, session_id: int, seen: bool = True,
                     project_id: str | None = None) -> int:
    with db.transaction() as conn:
        ids = artifacts_repo.ids_matching(conn, artifacts_repo.ArtifactQuery(session_id=session_id,
                                                                             project_id=project_id))
        return seen_repo.mark(conn, user_id, ids) if seen else seen_repo.unmark(conn, user_id, ids)
