"""Shared pytest fixtures for the studio test suite."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eks_harness.studio.jobs import JobRegistry, set_registry
from eks_harness.studio.roots import MEDIA_LIBRARY_ROOT, PROJECT_WORKSPACE_ROOT, RootsManager


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    return root


@pytest.fixture
def roots(workspace_root: Path, media_root: Path) -> RootsManager:
    manager = RootsManager()
    manager.register(PROJECT_WORKSPACE_ROOT, workspace_root)
    manager.register(MEDIA_LIBRARY_ROOT, media_root)
    return manager


@pytest.fixture
def job_registry(tmp_path: Path) -> Iterator[JobRegistry]:
    registry = JobRegistry(persist_path=tmp_path / "jobs.json")
    set_registry(registry)
    yield registry
    set_registry(None)


class _FakeSession:
    """Minimal stand-in for ``mcp.server.session.ServerSession`` for tool tests."""

    async def send_resource_updated(self, **kwargs: Any) -> None:
        return None


class _FakeContext:
    """Minimal Context surface used by the tools' ``ctx`` parameter."""

    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.info_messages: list[str] = []
        self.progress_events: list[tuple[float, float | None, str | None]] = []

    async def info(self, message: str) -> None:
        self.info_messages.append(message)

    async def warning(self, message: str) -> None:
        self.info_messages.append(message)

    async def report_progress(
        self,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        self.progress_events.append((progress, total, message))


@pytest.fixture
def fake_session_factory() -> Any:
    def _factory() -> _FakeSession:
        return _FakeSession()

    return _factory


@pytest.fixture
def fake_ctx_factory() -> Any:
    def _factory(session: _FakeSession) -> _FakeContext:
        return _FakeContext(session)

    return _factory


@pytest.fixture(autouse=True)
def _reset_subprocesses() -> Iterator[None]:
    """Ensure subprocess tracking dict is empty between tests."""

    yield
    # tools.render_project keeps a tiny module-level dict; clear if it exists
    if "eks_harness.studio.toolimpl.render_project" in sys.modules:
        from eks_harness.studio.toolimpl import render_project as rp

        rp._PROCESSES.clear()  # type: ignore[attr-defined]
