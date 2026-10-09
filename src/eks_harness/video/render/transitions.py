"""Assemble per-segment outputs into merged clips honoring transition IR.

The per-segment renderer produces one ``.mp4`` per :class:`Segment`. Hard
:class:`Cut` boundaries can be stitched together with ffmpeg's concat
demuxer (a fast stream-copy), but every other transition kind needs the
boundary frames to actually blend - both the plain xfade presets
(:class:`Crossfade`, :class:`Wipe`, :class:`Slide`, :class:`Push`,
:class:`ZoomTransition`, :class:`Iris`, :class:`LumaWipe`, ...) and the
custom-graph kinds (:class:`GlitchTransition`, :class:`RGBShiftWipe`,
:class:`WhipPan`, :class:`LightLeak`, :class:`FilmBurn`, :class:`BeatFlash`,
:class:`BeatGlitch`). This module folds neighbouring segment files into merged
files at every non-cut boundary by invoking ffmpeg's ``xfade`` filter,
then hands the resulting (possibly smaller) list of files back to the
orchestrator so the final concat-demuxer pass remains a fast copy.

Semantics
---------

For each adjacent pair ``(S_i, S_{i+1})`` on the same video track:

* If both ``S_i.transition_out`` and ``S_{i+1}.transition_in`` are
  :class:`Cut` (or a :class:`PluginTransition` without a registered plugin),
  the boundary is left untouched.
* Otherwise the xfade occupies the **last ``duration`` seconds of S_i**,
  with ``S_{i+1}`` fading in concurrently. Result duration is
  ``S_i_duration + S_{i+1}_duration - transition.duration`` - i.e. the
  project's total rendered duration shrinks by the sum of all xfade
  durations vs the sum of segment durations.
* When ``transition_out`` and ``transition_in`` disagree (e.g. ``S_i``
  declares :class:`Crossfade` and ``S_{i+1}`` declares :class:`Wipe`),
  the **leaving** clip's ``transition_out`` wins and a warning is
  emitted.
* ``transition.duration`` is clamped to
  ``min(left_duration, right_duration) - 1/fps`` so xfade can never
  exceed either input.

Push direction mapping
----------------------

ffmpeg's xfade filter has no preset that takes a direction parameter;
the closest analogues to a true "push" are the ``squeezeh`` (horizontal)
and ``squeezev`` (vertical) presets. Those compress the leaving clip
rather than sliding it off-screen, which is visually close but not
identical to a strict push. We map Push directions to:

* ``left`` / ``right`` -> ``squeezeh``
* ``up`` / ``down`` -> ``squeezev``

When a Push is encountered we emit a warning noting the fallback.

Audio
-----

This module touches video only. The project's audio stem is rendered
independently by :func:`eks_harness.video.render.audio.render_audio_stem` and is
muxed onto the final concatenated video by :func:`mux_segments` with the
``-shortest`` flag, which truncates the audio to the (now shorter)
post-xfade video length. That is the desired behaviour for the current
release; an ``acrossfade`` pass on the audio stem is left as future
work.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from eks_harness.video.ir.transitions import (
    BeatFlash,
    BeatGlitch,
    BlurThrough,
    Crossfade,
    Cut,
    DipToBlack,
    DipToWhite,
    Dissolve,
    FadeGrays,
    FilmBurn,
    GlitchTransition,
    Iris,
    LightLeak,
    LumaWipe,
    Pixelize,
    PluginTransition,
    Push,
    Radial,
    RGBShiftWipe,
    Slide,
    Transition,
    WhipPan,
    Wipe,
    ZoomTransition,
)

from .subprocess_runner import run_ffmpeg

if TYPE_CHECKING:
    from eks_harness.video.ir.tracks import Segment

__all__ = ["assemble_transitions", "has_xfade_transitions"]

_LOG = logging.getLogger(__name__)


def has_xfade_transitions(segments: Sequence["Segment"]) -> bool:
    """Return True iff any adjacent pair declares a non-cut transition."""

    return any(
        _resolve_transition(segments[i - 1], segments[i]) is not None
        for i in range(1, len(segments))
    )


def assemble_transitions(
    segment_outputs: Sequence[Path],
    segments: Sequence["Segment"],
    segment_durations: Sequence[float],
    *,
    workspace: Path,
    fps: float,
    binary: str = "ffmpeg",
    boundary_beats: Mapping[int, Sequence[float]] | None = None,
) -> list[Path]:
    """Fold per-segment mp4s at every non-cut boundary into merged mp4s.

    The returned list has one entry per remaining boundary group: a hard
    :class:`Cut` boundary keeps the two files separate, a non-cut
    transition merges them into a single file via ffmpeg ``xfade``.
    Audio is unaffected - the project's audio stem is rendered
    independently and will be truncated by :func:`mux_segments`'s
    ``-shortest`` to match the post-xfade video length.

    ``boundary_beats`` carries beat-reactive timing for :class:`BeatFlash` /
    :class:`BeatGlitch`: a mapping from the **right segment's index** ``i``
    (the same ``i`` this function iterates over, ``1 <= i < len(segments)``)
    to that boundary's beat times in **window-local seconds** (``0`` = start
    of the transition window). The orchestrator computes these from the
    resolved marker streams; non-beat transitions ignore the mapping.
    """

    if not segment_outputs:
        raise ValueError("assemble_transitions requires at least one segment output")
    if len(segment_outputs) != len(segments) or len(segments) != len(segment_durations):
        raise ValueError(
            "segment_outputs, segments, and segment_durations must be the same length"
        )

    merged_paths: list[Path] = [Path(segment_outputs[0])]
    merged_durations: list[float] = [float(segment_durations[0])]

    for i in range(1, len(segments)):
        transition = _resolve_transition(segments[i - 1], segments[i])
        if transition is None:
            merged_paths.append(Path(segment_outputs[i]))
            merged_durations.append(float(segment_durations[i]))
            continue

        left_path = merged_paths[-1]
        left_duration = merged_durations[-1]
        right_path = Path(segment_outputs[i])
        right_duration = float(segment_durations[i])

        clamped = _clamp_duration(transition.duration, left_duration, right_duration, fps)
        if clamped <= 0.0:
            _LOG.warning(
                "transition between %r and %r has non-positive clamped duration "
                "(requested %.3fs, left=%.3fs, right=%.3fs); falling back to hard cut",
                segments[i - 1].id,
                segments[i].id,
                transition.duration,
                left_duration,
                right_duration,
            )
            merged_paths.append(right_path)
            merged_durations.append(right_duration)
            continue

        offset = max(0.0, left_duration - clamped)
        beats = boundary_beats.get(i) if boundary_beats is not None else None
        filter_complex = _build_filter_complex(transition, clamped, offset, beats)
        merged = _run_xfade(
            left=left_path,
            right=right_path,
            filter_complex=filter_complex,
            workspace=workspace,
            binary=binary,
            index=i,
        )
        merged_paths[-1] = merged
        merged_durations[-1] = left_duration + right_duration - clamped

    return merged_paths


def _resolve_transition(left: "Segment", right: "Segment") -> Transition | None:
    """Pick the effective xfade transition between two adjacent segments.

    Returns ``None`` when neither side declares an xfade-style transition
    (i.e. both are :class:`Cut` or an unregistered :class:`PluginTransition`).
    """

    leaving = left.transition_out
    entering = right.transition_in
    leaving_xfade = _is_xfade(leaving)
    entering_xfade = _is_xfade(entering)

    if not leaving_xfade and not entering_xfade:
        return None
    if leaving_xfade and entering_xfade and not _same_transition(leaving, entering):
        _LOG.warning(
            "transition mismatch between %r.transition_out (%s) and "
            "%r.transition_in (%s); using the leaving clip's transition_out",
            left.id,
            type(leaving).__name__,
            right.id,
            type(entering).__name__,
        )
        return leaving
    return leaving if leaving_xfade else entering


def _is_xfade(transition: Transition) -> bool:
    if isinstance(transition, PluginTransition):
        from eks_harness.video.plugins.registry import get_transition

        if get_transition(transition.name) is None:
            _LOG.warning("no transition plugin named %r is registered; cutting instead", transition.name)
            return False
        return True
    return not isinstance(transition, Cut)


def _same_transition(a: Transition, b: Transition) -> bool:
    return type(a) is type(b) and a.model_dump() == b.model_dump()


def _clamp_duration(
    requested: float, left_duration: float, right_duration: float, fps: float
) -> float:
    frame = 1.0 / float(fps) if fps > 0 else 0.0
    upper = min(left_duration, right_duration) - frame
    if requested <= upper:
        return float(requested)
    _LOG.warning(
        "transition duration %.3fs exceeds adjacent segment lengths "
        "(left=%.3fs, right=%.3fs); clamping to %.3fs",
        requested,
        left_duration,
        right_duration,
        max(0.0, upper),
    )
    return max(0.0, upper)


def _ffmpeg_preset_for(transition: Transition) -> str:
    """Map an xfade-style IR transition to its ffmpeg ``xfade`` preset name."""

    if isinstance(transition, Crossfade):
        return "fade"
    if isinstance(transition, DipToBlack):
        return "fadeblack"
    if isinstance(transition, DipToWhite):
        return "fadewhite"
    if isinstance(transition, Wipe):
        return f"wipe{transition.direction}"
    if isinstance(transition, Slide):
        return f"slide{transition.direction}"
    if isinstance(transition, Push):
        axis = "squeezeh" if transition.direction in ("left", "right") else "squeezev"
        _LOG.warning(
            "Push(direction=%r) has no exact ffmpeg xfade preset; falling back to %s",
            transition.direction,
            axis,
        )
        return axis
    if isinstance(transition, ZoomTransition):
        return "zoomin"
    if isinstance(transition, Dissolve):
        return "dissolve"
    if isinstance(transition, BlurThrough):
        return "hblur"
    if isinstance(transition, Iris):
        return "circleopen" if transition.mode == "open" else "circleclose"
    if isinstance(transition, Radial):
        return "radial"
    if isinstance(transition, Pixelize):
        return "pixelize"
    if isinstance(transition, FadeGrays):
        return "fadegrays"
    if isinstance(transition, LumaWipe):
        return f"smooth{transition.direction}"
    raise ValueError(f"transition {type(transition).__name__!r} is not an xfade transition")


_PREP = (
    "[0:v]format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[a];"
    "[1:v]format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[b];"
)


def _build_filter_complex(
    transition: Transition,
    duration: float,
    offset: float,
    boundary_beats: Sequence[float] | None = None,
) -> str:
    """Build the ffmpeg ``filter_complex`` joining two clips at a boundary.

    Standard xfade transitions map to a single ``xfade=transition=<preset>``.
    A handful of transitions have no native xfade preset and are rendered as a
    base xfade followed by gated post-filters (chroma split, directional blur,
    brightness bloom, warm burn). The beat-reactive transitions
    (:class:`BeatFlash`, :class:`BeatGlitch`) additionally consume
    ``boundary_beats`` - beat times in **window-local seconds** (``0`` = start
    of the transition window, ``duration`` = end). When that list is empty (no
    beat landed in the window, or markers were unavailable) they fall back to a
    single pulse at mid-window so the transition is never a silent no-op.
    """

    end = offset + duration
    if isinstance(transition, PluginTransition):
        from eks_harness.video.plugins.registry import get_transition

        plugin = get_transition(transition.name)
        if plugin is None:
            raise ValueError(f"no transition plugin named {transition.name!r} is registered")
        return _PREP + plugin.filter_complex(dict(transition.params), duration, offset, list(boundary_beats or []))
    if isinstance(transition, GlitchTransition):
        shift = max(1, round(transition.intensity * 24))
        return (
            _PREP
            + f"[a][b]xfade=transition=pixelize:duration={duration:.6f}:offset={offset:.6f}[x];"
            + f"[x]rgbashift=rh={shift}:bh=-{shift}:"
            + f"enable='between(t,{offset:.6f},{end:.6f})'[v]"
        )
    if isinstance(transition, RGBShiftWipe):
        shift = max(1, round(transition.intensity * 20))
        return (
            _PREP
            + f"[a][b]xfade=transition=wipe{transition.direction}:"
            + f"duration={duration:.6f}:offset={offset:.6f}[x];"
            + f"[x]rgbashift=rh={shift}:bh=-{shift}:"
            + f"enable='between(t,{offset:.6f},{end:.6f})'[v]"
        )
    if isinstance(transition, WhipPan):
        extent = max(1, round(transition.intensity * 40))
        if transition.direction in ("left", "right"):
            blur = f"avgblur=sizeX={extent}:sizeY=1"
        else:
            blur = f"avgblur=sizeX=1:sizeY={extent}"
        return (
            _PREP
            + f"[a][b]xfade=transition=slide{transition.direction}:"
            + f"duration={duration:.6f}:offset={offset:.6f}[x];"
            + f"[x]{blur}:enable='between(t,{offset:.6f},{end:.6f})'[v]"
        )
    if isinstance(transition, LightLeak):
        mid = offset + duration / 2.0
        sig = max(0.05, duration / 4.0)
        expr = (
            f"{transition.intensity:.6f}*"
            f"exp(-((t-{mid:.6f})/{sig:.6f})*((t-{mid:.6f})/{sig:.6f}))"
        )
        return (
            _PREP
            + f"[a][b]xfade=transition=fade:duration={duration:.6f}:offset={offset:.6f}[x];"
            + f"[x]eq=brightness='{expr}':eval=frame[v]"
        )
    if isinstance(transition, FilmBurn):
        mid = offset + duration / 2.0
        sig = max(0.05, duration / 4.0)
        peak = 0.7 * transition.intensity
        shift = max(1, round(transition.intensity * 10))
        bloom = (
            f"{peak:.6f}*"
            f"exp(-((t-{mid:.6f})/{sig:.6f})*((t-{mid:.6f})/{sig:.6f}))"
        )
        return (
            _PREP
            + f"[a][b]xfade=transition=fade:duration={duration:.6f}:offset={offset:.6f}[x];"
            + f"[x]eq=brightness='{bloom}':eval=frame[y];"
            + "[y]colorbalance=rs=0.3:rm=0.2:bs=-0.25:"
            + f"enable='between(t,{offset:.6f},{end:.6f})'[z];"
            + f"[z]rgbashift=rh={shift}:bh=-{shift}:"
            + f"enable='between(t,{offset:.6f},{end:.6f})'[v]"
        )
    if isinstance(transition, BeatFlash):
        beats = _window_pulse_times(boundary_beats, duration)
        terms = "+".join(
            f"exp(-((t-{offset + b:.6f})/0.05)*((t-{offset + b:.6f})/0.05))"
            for b in beats
        )
        expr = f"{transition.intensity:.6f}*({terms})"
        return (
            _PREP
            + f"[a][b]xfade=transition=fade:duration={duration:.6f}:offset={offset:.6f}[x];"
            + f"[x]eq=brightness='{expr}':eval=frame[v]"
        )
    if isinstance(transition, BeatGlitch):
        beats = _window_pulse_times(boundary_beats, duration)
        shift = max(1, round(transition.intensity * 20))
        enable = "+".join(
            f"between(t,{offset + b - 0.04:.6f},{offset + b + 0.04:.6f})"
            for b in beats
        )
        return (
            _PREP
            + f"[a][b]xfade=transition=pixelize:duration={duration:.6f}:offset={offset:.6f}[x];"
            + f"[x]rgbashift=rh={shift}:bh=-{shift}:enable='{enable}'[v]"
        )
    preset = _ffmpeg_preset_for(transition)
    return (
        _PREP
        + f"[a][b]xfade=transition={preset}:duration={duration:.6f}:offset={offset:.6f}[v]"
    )


def _window_pulse_times(boundary_beats: Sequence[float] | None, duration: float) -> list[float]:
    """Beat times to pulse on, in window-local seconds.

    Keeps only beats inside ``[0, duration]``. When nothing qualifies (the
    boundary has no resolved beats in its window, or markers were never
    threaded through), returns a single pulse at mid-window so the
    beat-reactive transition still produces a visible effect.
    """

    if boundary_beats:
        kept = [float(b) for b in boundary_beats if 0.0 <= b <= duration]
        if kept:
            return kept
    return [duration / 2.0]


def _run_xfade(
    *,
    left: Path,
    right: Path,
    filter_complex: str,
    workspace: Path,
    binary: str,
    index: int,
) -> Path:
    """Render a single transition-joined mp4 from ``left`` and ``right``.

    The merged file is written under ``workspace/transitions/`` so the
    orchestrator can pass it straight to :func:`mux_segments` afterwards.
    Outputs use the same libx264 / yuv420p profile that the per-segment
    renders produce so the concat demuxer can stream-copy them.
    """

    out_dir = workspace / "transitions"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"xfade_{index:03d}.mp4"

    from . import hwaccel

    args = [
        *hwaccel.decode_args(),
        "-i", str(left),
        *hwaccel.decode_args(),
        "-i", str(right),
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-an",
        *hwaccel.encode_args(),
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    run_ffmpeg(args, binary=binary)
    return out_path
