from __future__ import annotations


def project_days(project_days: int | None, default_days: int | None) -> int | None:
    if project_days is None:
        return default_days
    return int(project_days) or None


def project_source(project_days: int | None) -> str:
    if project_days is None:
        return "global"
    return "project" if project_days else "forever"


def retention_rule(artifact_days: int | None, project_retention: int | None, default_days: int | None,
                   pinned: bool) -> tuple[int | None, str]:
    if pinned:
        return None, "pinned"
    if artifact_days:
        return int(artifact_days), "artifact"
    if project_retention is not None:
        return (int(project_retention) or None), "project"
    return default_days, "global"
