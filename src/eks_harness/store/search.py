from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from eks_harness.api.links import Links
from eks_harness.api.schemas import SearchHitOut, SearchResponse
from eks_harness.db import Database
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos.artifacts import Artifact
from eks_harness.db.repos.grants import AccessScope
from eks_harness.paths import Paths
from eks_harness.store import artifacts as store_artifacts

MAX_RESULTS = 500


def fts_query(text: str) -> str:
    tokens = re.findall(r"[\w]+", text or "", re.UNICODE)
    return " AND ".join(f'"{t}"*' for t in tokens)


@dataclass(frozen=True)
class SearchHit:
    artifact: Artifact
    snippet: str
    rank: float


def search(conn: sqlite3.Connection, text: str, *, scope_sql: tuple[str, list] | None = None,
           project_id: str | None = None, limit: int = 50) -> list[SearchHit]:
    match = fts_query(text)
    if not match:
        return []
    clauses, params = ["artifacts_fts MATCH ?"], [match]
    if project_id is not None:
        clauses.append("a.project_id = ?")
        params.append(project_id)
    if scope_sql is not None:
        clauses.append(scope_sql[0])
        params.extend(scope_sql[1])
    rows = conn.execute(
        "SELECT f.artifact_id AS aid, bm25(artifacts_fts) AS rank, "
        "snippet(artifacts_fts, -1, '[', ']', '...', 12) AS snip "
        "FROM artifacts_fts f JOIN artifacts a ON a.id = f.artifact_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT ?", [*params, max(1, min(limit, MAX_RESULTS))]).fetchall()
    found = {a.id: a for a in artifacts_repo.get_many(conn, [r["aid"] for r in rows])}
    return [SearchHit(found[r["aid"]], r["snip"] or "", float(r["rank"])) for r in rows if r["aid"] in found]


def search_artifacts(db: Database, links: Links, paths: Paths, user_id: int, scope: AccessScope, text: str, *,
                     project_id: str | None = None, limit: int = 50) -> SearchResponse:
    conn = db.conn()
    hits = search(conn, text, scope_sql=None if scope.everything else scope.sql("a.project_id", "a.session_id"),
                  project_id=project_id, limit=limit)
    outs = store_artifacts.to_out_many(conn, links, paths, [h.artifact for h in hits], user_id)
    return SearchResponse(query=text, items=[SearchHitOut(artifact=o, snippet=h.snippet, rank=h.rank)
                                             for o, h in zip(outs, hits)])
