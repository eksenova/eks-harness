"""YtDlpProvider URI parsing tests (no network)."""

from __future__ import annotations

from eks_harness.video.plugins.builtin.media_providers.yt_dlp import YtDlpProvider


def test_handles_yt_dlp_scheme() -> None:
    assert YtDlpProvider().can_handle("yt-dlp://https://youtu.be/abc123")


def test_handles_youtube_https() -> None:
    provider = YtDlpProvider()
    assert provider.can_handle("https://www.youtube.com/watch?v=abc")
    assert provider.can_handle("https://youtu.be/abc")


def test_does_not_handle_random_https() -> None:
    assert not YtDlpProvider().can_handle("https://example.com/video.mp4")


def test_does_not_handle_filesystem_paths() -> None:
    assert not YtDlpProvider().can_handle("/tmp/foo.mp4")
