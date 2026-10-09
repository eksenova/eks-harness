from __future__ import annotations

from eks_harness.video import (
    Animated,
    Brightness,
    ImageFile,
    Seconds,
    Segment,
)
from eks_harness.video.render.cache import compute_segment_key


def _segment(brightness: float = 0.4) -> Segment:
    return Segment(
        id="hero",
        start=Seconds(t=0.0),
        media=ImageFile(path="poster.png"),
        in_=Seconds(t=0.0),
        out=Seconds(t=4.0),
        effects=[Brightness(amount=Animated[float](root=brightness))],
    )


def test_same_inputs_same_key() -> None:
    seg = _segment()
    key1 = compute_segment_key(seg, media_digest="abc", plugin_versions={"a": "1.0"})
    key2 = compute_segment_key(seg, media_digest="abc", plugin_versions={"a": "1.0"})
    assert key1 == key2


def test_changing_segment_changes_key() -> None:
    base = _segment(0.4)
    other = _segment(0.7)
    assert compute_segment_key(base, media_digest="x") != compute_segment_key(other, media_digest="x")


def test_changing_media_digest_changes_key() -> None:
    seg = _segment()
    assert compute_segment_key(seg, media_digest="a") != compute_segment_key(seg, media_digest="b")


def test_changing_plugin_version_changes_key() -> None:
    seg = _segment()
    a = compute_segment_key(seg, media_digest="x", plugin_versions={"glitch": "1.0"})
    b = compute_segment_key(seg, media_digest="x", plugin_versions={"glitch": "1.1"})
    assert a != b


def test_changing_render_settings_changes_key() -> None:
    seg = _segment()
    a = compute_segment_key(seg, media_digest="x", render_settings={"fps": 30})
    b = compute_segment_key(seg, media_digest="x", render_settings={"fps": 60})
    assert a != b
