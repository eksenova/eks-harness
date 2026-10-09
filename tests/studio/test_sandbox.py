"""Sandbox runner tests.

Covers the four cardinal cases per plan §6.5:

1. Happy path - well-formed project.py executes and returns a Project pickle.
2. Syntax / runtime error - non-zero exit with traceback in ``stderr``.
3. Infinite loop - wall-clock timeout kills the child.
4. Network attempt - blocked socket monkey-patch denies the connect.
"""

from __future__ import annotations

import pytest

from eks_harness.studio.sandbox import run_project_py

pytestmark = pytest.mark.asyncio


_HAPPY_SOURCE = """
from eks_harness.video import Project, Track, Segment, ImageFile, Seconds

project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=4.0,
    tracks=[
        Track(
            name="main",
            segments=[
                Segment(
                    id="hero",
                    start=Seconds(t=0.0),
                    media=ImageFile(path="hero.png"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=4.0),
                ),
            ],
        ),
    ],
)
"""

_SYNTAX_ERROR_SOURCE = "def broken(:\n    pass\n"

_INFINITE_LOOP_SOURCE = "while True:\n    pass\n"

_NETWORK_SOURCE = """
import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.connect(('1.1.1.1', 80))
"""


async def test_sandbox_happy_path() -> None:
    result = await run_project_py(_HAPPY_SOURCE, timeout_s=30.0)
    assert result.ok is True, f"sandbox failed: {result.error}\n{result.traceback}"
    assert result.project_pickle is not None
    assert result.project_json is not None
    assert '"fps":30' in result.project_json or '"fps": 30' in result.project_json


async def test_sandbox_syntax_error() -> None:
    result = await run_project_py(_SYNTAX_ERROR_SOURCE, timeout_s=10.0)
    assert result.ok is False
    assert result.returncode != 0
    assert result.traceback is not None
    assert "SyntaxError" in (result.traceback or "") or "invalid syntax" in (result.traceback or "")


async def test_sandbox_infinite_loop_killed_by_timeout() -> None:
    result = await run_project_py(_INFINITE_LOOP_SOURCE, timeout_s=2.0)
    assert result.ok is False
    assert result.error is not None and "timed out" in result.error
    assert result.duration_s >= 1.5


async def test_sandbox_network_blocked_by_default() -> None:
    result = await run_project_py(_NETWORK_SOURCE, timeout_s=10.0)
    assert result.ok is False
    blob = (result.traceback or "") + (result.error or "")
    assert "network access is disabled" in blob or "PermissionError" in blob
