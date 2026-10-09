"""Roots manager tests."""

from __future__ import annotations

from pathlib import Path

from eks_harness.studio.roots import MEDIA_LIBRARY_ROOT, PROJECT_WORKSPACE_ROOT, RootsManager


def test_is_under_root_accepts_nested_path(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    asset = media / "video" / "clip.mp4"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"")

    manager = RootsManager()
    manager.register(MEDIA_LIBRARY_ROOT, media)

    assert manager.is_under_root(asset, MEDIA_LIBRARY_ROOT) is True


def test_is_under_root_rejects_sibling_path(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    sibling = tmp_path / "other" / "leak.mp4"
    sibling.parent.mkdir(parents=True)
    sibling.write_bytes(b"")

    manager = RootsManager()
    manager.register(MEDIA_LIBRARY_ROOT, media)

    assert manager.is_under_root(sibling, MEDIA_LIBRARY_ROOT) is False


def test_is_under_root_returns_false_for_unknown_root(tmp_path: Path) -> None:
    manager = RootsManager()
    assert manager.is_under_root(tmp_path / "x", "nonexistent") is False


def test_project_workspace_falls_back_to_default_when_unregistered(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))

    manager = RootsManager()
    workspace = manager.project_workspace()
    assert workspace.exists()
    assert workspace.is_dir()


def test_project_workspace_uses_registered_path(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    manager = RootsManager()
    manager.register(PROJECT_WORKSPACE_ROOT, workspace)
    assert manager.project_workspace() == workspace.resolve()
