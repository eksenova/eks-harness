from __future__ import annotations

import json

import pytest

from eks_harness.annotate import assets as annotate_assets
from eks_harness.annotate import versions as annotate_versions
from eks_harness.annotate.versions import AnnotationMeta, Recipe
from eks_harness.db import migrations
from eks_harness.db.repos import artifacts as artifacts_repo
from conftest import ensure_project


def test_migration_discovers_section11() -> None:
    found = {migration.version: migration.name for migration in migrations.discover()}
    assert found[1] == "schema"
    assert found[2] == "section11"


def test_migration_applies_on_fresh_db(db) -> None:
    assert migrations.current_version(db.conn()) >= 2
    tables = {row[0] for row in db.conn().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"artifact_versions", "project_assets"} <= tables


def _artifact_id(db, project_id: str = "acme/web") -> str:
    ensure_project(db, project_id)
    with db.transaction() as conn:
        artifact = artifacts_repo.insert(
            conn, artifact_id="01Jshire", project_id=project_id, kind="screenshot",
            filename="shot.png", rel_path="acme/web/_project/01Jshire/shot.png",
            mime="image/png", size=10, sha256="abc", width=800, height=600)
    return artifact.id


def test_version_lifecycle(db) -> None:
    artifact_id = _artifact_id(db)
    with db.transaction() as conn:
        first = annotate_versions.record_version(
            conn, artifact_id=artifact_id, file_rel_path="acme/web/_project/01Jshire/shot.png",
            kind="initial", spec={"version": 1, "items": []}, style_name="kb",
            style_snapshot={"name": "kb"}, boxes={}, report={"ok": True}, created_by="tester")
        second = annotate_versions.record_version(
            conn, artifact_id=artifact_id, file_rel_path="acme/web/_project/01Jshire/versions/2/shot.png",
            kind="rerender", spec={"version": 1, "items": []}, style_name="review",
            style_snapshot={"name": "review"}, boxes={}, report={"ok": True}, created_by="tester")
    assert (first.version_no, second.version_no) == (1, 2)
    with db.transaction() as conn:
        listed = annotate_versions.list_versions(conn, artifact_id)
        assert [version.kind for version in listed] == ["initial", "rerender"]
        fetched = annotate_versions.get_version(conn, artifact_id, 1)
        assert fetched is not None and fetched.style_name == "kb"
        restored = annotate_versions.record_version(
            conn, artifact_id=artifact_id, file_rel_path="acme/web/_project/01Jshire/shot.png",
            kind="restore", spec={"version": 1, "items": []}, style_name="kb",
            style_snapshot={"name": "kb"}, boxes={}, report={"ok": True})
    assert restored.version_no == 3
    assert restored.kind == "restore"
    with pytest.raises(ValueError):
        with db.transaction() as conn:
            annotate_versions.record_version(
                conn, artifact_id=artifact_id, file_rel_path="x", kind="bogus",
                spec={}, style_name="", style_snapshot={}, boxes={}, report={})


def test_version_cascade_on_artifact_delete(db) -> None:
    artifact_id = _artifact_id(db)
    with db.transaction() as conn:
        annotate_versions.record_version(
            conn, artifact_id=artifact_id, file_rel_path="p", kind="initial",
            spec={}, style_name="kb", style_snapshot={}, boxes={}, report={})
        artifacts_repo.delete(conn, artifact_id)
        assert annotate_versions.list_versions(conn, artifact_id) == []


def test_project_assets_crud(db) -> None:
    ensure_project(db, "acme/web")
    with db.transaction() as conn:
        created = annotate_assets.register_asset(
            conn, project_id="acme/web", name="logo", filename="logo.png",
            rel_path="assets/acme/web/logo/logo.png", mime="image/png", size=42, sha256="ff")
        assert created.name == "logo"
        assert [asset.name for asset in annotate_assets.list_assets(conn, "acme/web")] == ["logo"]
        assert annotate_assets.get_asset(conn, "acme/web", "logo") is not None
        removed = annotate_assets.delete_asset(conn, "acme/web", "logo")
        assert removed is not None and removed.sha256 == "ff"
        assert annotate_assets.list_assets(conn, "acme/web") == []
    assert annotate_assets.asset_rel_path("acme/web", "logo", "logo.png") == "assets/acme/web/logo/logo.png"


def test_meta_and_recipe_roundtrip() -> None:
    recipe = Recipe(kind="script", ref="login", viewport="desktop", project="acme/web",
                    session="main", sid="abc123")
    meta = AnnotationMeta(clean_id="clean-1", spec={"version": 1}, normalized_spec={"version": 1},
                          style_name="kb", style_snapshot={"name": "kb"}, boxes={"s": {"x": 1}},
                          recipe=recipe.to_dict(), anchor_fallback=False,
                          report={"ok": True}, crops=["crops/s.png"])
    raw = meta.to_dict()
    assert json.loads(json.dumps(raw))["recipe"]["sid"] == "abc123"
    assert AnnotationMeta.from_dict(raw).to_dict() == raw
    assert Recipe.from_dict(recipe.to_dict()).to_dict() == recipe.to_dict()
    superseded = annotate_versions.mark_superseded({"annotation": meta.to_dict()}, "new-1", "2026-09-26T00:00:00Z")
    assert superseded["annotation"]["superseded"]["by"] == "new-1"
    assert annotate_versions.overlay_filename("shot.png") == "shot-annotated.png"
    assert annotate_versions.version_rel_path("a/b/c", 2, "shot.png") == "a/b/c/versions/2/shot.png"
