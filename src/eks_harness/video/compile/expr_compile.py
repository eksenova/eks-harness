"""Compile an ``Animated[float]`` to an ffmpeg expression.

The orchestrator prefers to fold animated parameters directly into ffmpeg
filter chains so the entire segment can run on the GPU encoder. Constants
collapse to a literal; keyframes lower to a chain of ``between(t,...)``
expressions; curves are not graph-compilable and trigger a fall-back to the
frame pipeline.

If the generated expression exceeds :data:`EXPR_BUDGET_BYTES` the function
returns ``None``; the caller then routes the segment to the frame pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eks_harness.video.ir.animated import Animated, Keyframe

from .time_resolve import resolve_time

if TYPE_CHECKING:
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.ir import Project

EXPR_BUDGET_BYTES = 8 * 1024
"""Maximum size of an emitted ffmpeg expression before we spill to the frame pipeline."""


def animated_to_ffmpeg_expr(
    animated: Animated[Any] | float,
    project: Project,
    markers: MarkerSet,
) -> str | None:
    """Lower ``animated`` to a single ffmpeg expression string.

    Returns ``None`` when the animated value uses a curve (those are not
    graph-compilable) or when the resulting expression would exceed the
    budget.
    """

    if not isinstance(animated, Animated):
        return _format_constant(animated)

    root = animated.root
    if isinstance(root, list):
        expr = _keyframes_to_expr(root, project, markers)
    else:
        if _is_curve(root):
            return None
        expr = _format_constant(root)

    if len(expr.encode("utf-8")) > EXPR_BUDGET_BYTES:
        return None
    return expr


def _is_curve(value: Any) -> bool:
    return getattr(value, "kind", None) in {"beat_pulse", "exp_decay", "sine", "lfo", "lambda"}


def _format_constant(value: Any) -> str:
    return f"{float(value):.6f}"


def _keyframes_to_expr(
    keyframes: list[Keyframe[Any]],
    project: Project,
    markers: MarkerSet,
) -> str:
    if not keyframes:
        raise ValueError("cannot lower an empty keyframe list")
    sorted_kfs = sorted(keyframes, key=lambda kf: resolve_time(kf.t, project, markers))
    times = [resolve_time(kf.t, project, markers) for kf in sorted_kfs]
    values = [float(kf.v) for kf in sorted_kfs]

    expr = _format_constant(values[-1])
    for i in range(len(sorted_kfs) - 2, -1, -1):
        t_start = times[i]
        t_end = times[i + 1]
        v_start = values[i]
        v_end = values[i + 1]
        interp = sorted_kfs[i].interp
        span = max(t_end - t_start, 1e-9)
        if interp == "hold":
            inner = _format_constant(v_start)
        else:
            inner = (
                f"({v_start:.6f}+({v_end - v_start:.6f})*"
                f"((t-{t_start:.6f})/{span:.6f}))"
            )
        expr = f"if(between(t,{t_start:.6f},{t_end:.6f}),{inner},{expr})"
    expr = f"if(lt(t,{times[0]:.6f}),{values[0]:.6f},{expr})"
    return expr


__all__ = ["EXPR_BUDGET_BYTES", "animated_to_ffmpeg_expr"]
