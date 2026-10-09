"""AutoReframe tests (round-trip + clear error without ML deps)."""

from __future__ import annotations

import importlib

import pytest

from eks_harness.video.ir import EffectAdapter
from eks_harness.video.ir.effects import AutoReframe
from eks_harness.video.plugins.builtin.effects.auto_reframe import AutoReframePlugin
from eks_harness.video.render.context import RenderContext


def test_auto_reframe_round_trip() -> None:
    effect = AutoReframe(target_aspect=(9, 16), tracker="face", smoothing=0.9)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, AutoReframe)
    assert rebuilt.target_aspect == (9, 16)


def test_auto_reframe_face_raises_without_mediapipe(render_ctx: RenderContext) -> None:
    if importlib.util.find_spec("mediapipe") is not None:
        pytest.skip("mediapipe installed; skipping missing-dependency test")
    with pytest.raises(RuntimeError, match=r"mediapipe"):
        AutoReframePlugin().open(AutoReframe(tracker="face"), render_ctx)


def test_auto_reframe_salience_raises_without_cv2(render_ctx: RenderContext) -> None:
    if importlib.util.find_spec("cv2") is not None:
        pytest.skip("cv2 installed; skipping missing-dependency test")
    with pytest.raises(RuntimeError, match=r"opencv"):
        AutoReframePlugin().open(AutoReframe(tracker="salience"), render_ctx)
