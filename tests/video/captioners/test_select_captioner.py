"""Captioner selection logic.

Pure logic against the registry -- no ML imports required. Each test wipes
and repopulates the captioner registry so the platform-conditional MLX
behaviour can be exercised on every host.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from eks_harness.video.plugins import registry as plugin_registry
from eks_harness.video.plugins.base import Captioner
from eks_harness.video.plugins.builtin.captioners import select_captioner


class _FakeCaptioner(Captioner):
    name = "_unset"

    def transcribe(self, audio_path: Any, opts: Any | None = None) -> list[Any]:
        return []


def _make_captioner(plugin_name: str) -> _FakeCaptioner:
    cls = type(f"_FakeCaptioner_{plugin_name}", (_FakeCaptioner,), {"name": plugin_name})
    return cls()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    captioners = plugin_registry._CAPTIONERS  # noqa: SLF001
    snapshot = dict(captioners)
    captioners.clear()
    try:
        yield
    finally:
        captioners.clear()
        captioners.update(snapshot)


def _register(name: str) -> _FakeCaptioner:
    plugin = _make_captioner(name)
    plugin_registry._CAPTIONERS[name] = plugin  # noqa: SLF001
    return plugin


def test_explicit_prefer_returns_named_backend() -> None:
    expected = _register("faster-whisper")

    selected = select_captioner(prefer="faster-whisper")

    assert selected is expected


def test_explicit_prefer_raises_when_missing() -> None:
    with pytest.raises(RuntimeError, match="not registered"):
        select_captioner(prefer="faster-whisper")


def test_apple_silicon_prefers_mlx(monkeypatch: pytest.MonkeyPatch) -> None:
    import platform as platform_module

    monkeypatch.setattr(platform_module, "system", lambda: "Darwin")
    monkeypatch.setattr(platform_module, "machine", lambda: "arm64")
    mlx = _register("mlx-whisper")
    _register("faster-whisper")

    selected = select_captioner()

    assert selected is mlx


def test_windows_skips_mlx(monkeypatch: pytest.MonkeyPatch) -> None:
    import platform as platform_module

    monkeypatch.setattr(platform_module, "system", lambda: "Windows")
    monkeypatch.setattr(platform_module, "machine", lambda: "AMD64")
    _register("mlx-whisper")
    fw = _register("faster-whisper")

    selected = select_captioner()

    assert selected is fw


def test_no_backends_raises() -> None:
    with pytest.raises(RuntimeError, match="no captioner backend available"):
        select_captioner()
