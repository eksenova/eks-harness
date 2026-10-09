"""Audio stem renderer.

Lowers ``Project.audio_tracks`` into a single ``ffmpeg -filter_complex``
invocation that produces one AAC stem ready to mux alongside the rendered
video. The orchestrator calls :func:`render_audio_stem` after all video
segments are emitted and passes the resulting path into
:func:`eks_harness.video.render.mux.mux_segments` as ``audio_path``.

Graph shape per project::

    inputs : -i seg0.media -i seg1.media ...
    per-segment chain :
        [<idx>:a] atrim=...,asetpts=PTS-STARTPTS,
                  volume='<gain_expr>dB':eval=frame,
                  [afade=in/out=...,]
                  adelay=<start_ms>|<start_ms>
                  [,aecho=<reverb taps>] [seg_<track>_<i>]
        (optional) [seg_<track>_<i>][atrk_<src>_final]
            sidechaincompress=... [seg_<track>_<i>_duck]
    per-track mix :
        [seg_<track>_0][seg_<track>_1] amix=inputs=N:normalize=0
            -> per-track filter chain (gain/eq/parametric_eq/loudness/pan)
            -> [track_<name>_processed]
    sidechain (optional) :
        [track_<name>_processed][track_<source>_processed]
            sidechaincompress=... [track_<name>_final]
    final mix :
        [track_a_final][track_b_final] amix=inputs=N:duration=longest:normalize=0
            -> [aout]

Limitations (deliberate; documented for future patches):

- ``TTSGenerated`` segment media is rejected - synthesis pipeline is a
  separate concern.
- Animated ``AudioTrack.pan`` collapses to ``pan=0`` with a warning;
  per-frame channel-mix gains are non-trivial in ``ffmpeg``'s pan filter.
- Animated ``AudioTrack.eq`` falls back to the constant defaults already
  handled by :func:`build_audio_track_chain`.
- Sidechain (track) and ducking (segment) dependencies must be acyclic
  and reference an existing track by name.
"""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.compile.time_resolve import resolve_time
from eks_harness.video.ir.animated import Animated
from eks_harness.video.ir.audio_effects import (
    AudioFade,
    Ducking,
    EQEffect,
    Reverb,
)
from eks_harness.video.ir.media import AudioFile, SeparatedStem, TTSGenerated
from eks_harness.video.ir.tracks import AudioTrack

from .ffmpeg_builder import build_audio_track_chain, equalizer, loudnorm
from .subprocess_runner import run_ffmpeg

if TYPE_CHECKING:
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.ir import Project

__all__ = ["render_audio_stem"]

_LOG = logging.getLogger(__name__)


def render_audio_stem(
    project: Project,
    markers: MarkerSet,
    output_path: Path,
    *,
    ffmpeg_binary: str = "ffmpeg",
    cache_dir: Path | None = None,
) -> Path | None:
    """Render all audio tracks of ``project`` into a single AAC stem.

    Returns ``None`` when the project declares no audio tracks or none of
    them have segments - callers then mux video-only. Otherwise the file at
    ``output_path`` is overwritten and the path is returned.

    ``cache_dir`` is used to persist Demucs stem separations and
    DeepFilterNet denoise outputs across renders. Defaults to the parent
    directory of ``output_path``.
    """

    tracks = [t for t in project.audio_tracks if t.segments]
    if not tracks:
        return None

    for track in tracks:
        for segment in track.segments:
            if isinstance(segment.media, TTSGenerated):
                raise NotImplementedError(
                    f"audio segment {segment.id!r} uses TTSGenerated media; "
                    "the audio stem renderer does not synthesise TTS yet."
                )
            if not isinstance(segment.media, (AudioFile, SeparatedStem)):
                raise NotImplementedError(
                    f"audio segment {segment.id!r} uses {type(segment.media).__name__} "
                    "media; only AudioFile and SeparatedStem are supported."
                )

    workspace_cache = cache_dir if cache_dir is not None else output_path.parent

    ordered_tracks = _topo_order_for_sidechain(tracks)

    input_args: list[str] = []
    chains: list[str] = []
    track_final_labels: dict[str, str] = {}

    input_index = 0
    for track in ordered_tracks:
        segment_labels: list[str] = []
        for seg_i, segment in enumerate(track.segments):
            in_s = resolve_time(segment.in_, project, markers)
            out_s = resolve_time(segment.out, project, markers)
            start_s = resolve_time(segment.start, project, markers)
            if out_s <= in_s:
                raise ValueError(
                    f"audio segment {segment.id!r} has out ({out_s}) <= in ({in_s})"
                )

            media_path = _resolve_segment_media_path(
                segment.media, cache_dir=workspace_cache
            )
            if segment.denoise is not None and segment.denoise.enabled:
                from eks_harness.video.compile.stem_resolve import apply_denoise

                media_path = apply_denoise(
                    media_path, segment.denoise, cache_dir=workspace_cache
                )
            input_args.extend(["-i", str(media_path)])

            gain_expr = animated_to_ffmpeg_expr(segment.gain_db, project, markers)
            if gain_expr is None:
                _LOG.warning(
                    "segment %s has a curve-based gain_db that can't be lowered to "
                    "an ffmpeg expression; treating as 0dB.",
                    segment.id,
                )
                gain_expr = "0.000000"

            delay_ms = max(round(start_s * 1000), 0)
            seg_duration = out_s - in_s
            fade_clause = _render_fade_clause(segment.fade, seg_duration)
            reverb_clause = _render_reverb_clause(
                segment.reverb, segment.id, project, markers
            )

            seg_label = f"aseg_{_safe(track.name)}_{seg_i}"
            chain_parts = [
                f"atrim=start={in_s:.6f}:end={out_s:.6f}",
                "asetpts=PTS-STARTPTS",
                "aresample=async=1:first_pts=0",
                f"volume='pow(10,({gain_expr})/20)':eval=frame:precision=float",
            ]
            if fade_clause:
                chain_parts.append(fade_clause)
            chain_parts.append(f"adelay={delay_ms}|{delay_ms}:all=1")
            if reverb_clause:
                chain_parts.append(reverb_clause)
            chains.append(
                f"[{input_index}:a]" + ",".join(chain_parts) + f"[{seg_label}]"
            )

            if segment.ducking is not None:
                seg_label = _apply_segment_ducking(
                    segment.ducking,
                    segment_id=segment.id,
                    track_name=track.name,
                    seg_index=seg_i,
                    input_label=seg_label,
                    track_final_labels=track_final_labels,
                    chains=chains,
                )

            segment_labels.append(seg_label)
            input_index += 1

        track_raw_label = f"atrk_{_safe(track.name)}_raw"
        if len(segment_labels) == 1:
            # amix of one input is wasteful; alias label via anull.
            chains.append(f"[{segment_labels[0]}]anull[{track_raw_label}]")
        else:
            inputs_concat = "".join(f"[{label}]" for label in segment_labels)
            chains.append(
                f"{inputs_concat}amix=inputs={len(segment_labels)}:"
                f"duration=longest:normalize=0[{track_raw_label}]"
            )

        track_processed_label = f"atrk_{_safe(track.name)}_proc"
        chain_str = _serialize_track_chain(track, project, markers)
        if chain_str:
            chains.append(f"[{track_raw_label}]{chain_str}[{track_processed_label}]")
        else:
            chains.append(f"[{track_raw_label}]anull[{track_processed_label}]")

        pan_label = _apply_constant_pan(track, track_processed_label, chains)

        if track.sidechain is None:
            track_final_labels[track.name] = pan_label
        else:
            source_name = track.sidechain.source
            if source_name not in track_final_labels:
                raise ValueError(
                    f"audio track {track.name!r} sidechains to {source_name!r}, "
                    "which has not been rendered yet - topological order is broken."
                )
            ducked_label = f"atrk_{_safe(track.name)}_ducked"
            sc = track.sidechain
            chains.append(
                f"[{pan_label}][{track_final_labels[source_name]}]"
                f"sidechaincompress="
                f"threshold={_db_to_linear(sc.threshold_db):.6f}:"
                f"ratio={sc.ratio}:"
                f"attack={sc.attack_ms}:"
                f"release={sc.release_ms}"
                f"[{ducked_label}]"
            )
            track_final_labels[track.name] = ducked_label

    final_labels = list(track_final_labels.values())
    if len(final_labels) == 1:
        chains.append(f"[{final_labels[0]}]anull[aout]")
    else:
        inputs_mix = "".join(f"[{label}]" for label in final_labels)
        chains.append(
            f"{inputs_mix}amix=inputs={len(final_labels)}:"
            f"duration=longest:normalize=0[aout]"
        )

    filter_complex = ";".join(chains)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    args = [
        *input_args,
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-ac", "2",
        "-ar", "44100",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output_path),
    ]
    run_ffmpeg(args, binary=ffmpeg_binary)
    return output_path


def _resolve_segment_media_path(
    media: AudioFile | SeparatedStem, *, cache_dir: Path
) -> Path:
    """Resolve an audio segment's media to a concrete WAV/MP3 path.

    For :class:`AudioFile` this is just the declared path; for
    :class:`SeparatedStem` we delegate to the demucs-backed resolver in
    :mod:`eks_harness.video.compile.stem_resolve` (lazy-imported so the base
    base install doesn't pull demucs).
    """

    if isinstance(media, SeparatedStem):
        from eks_harness.video.compile.stem_resolve import resolve_stem_path

        return resolve_stem_path(media, cache_dir=cache_dir)
    return Path(media.path).resolve()


def _serialize_track_chain(
    track: AudioTrack, project: Project, markers: MarkerSet
) -> str:
    """Lower the track's EQ / gain / sidechain config and serialize.

    Sidechain is intentionally excluded here - :func:`render_audio_stem`
    wires the key signal explicitly with the source track's label so that
    sidechain depends on the source track's processed output, not on an
    unwired ``sidechaincompress`` filter node.

    Track-scoped audio effects (``parametric_eq``, ``loudness``) are
    appended after the base chain so they operate on the summed bus.
    """

    chain = build_audio_track_chain(track, project, markers)
    chain.nodes = [n for n in chain.nodes if n.name != "sidechaincompress"]

    parametric_eq = getattr(track, "parametric_eq", None)
    if parametric_eq is not None:
        for node in _parametric_eq_nodes(parametric_eq, project, markers, track.name):
            chain.add(node)

    loudness = getattr(track, "loudness", None)
    if loudness is not None:
        chain.add(
            loudnorm(I=loudness.target_lufs, LRA=loudness.lra, TP=loudness.true_peak_db)
        )

    return chain.serialize()


def _parametric_eq_nodes(
    parametric_eq: EQEffect,
    project: Project,
    markers: MarkerSet,
    track_name: str,
) -> list:
    """Lower a multi-band parametric EQ into one ``equalizer`` node per band."""

    nodes = []
    for band in parametric_eq.bands:
        gain_expr = animated_to_ffmpeg_expr(band.gain_db, project, markers)
        if gain_expr is None:
            _LOG.warning(
                "track %s parametric_eq band %.1fHz has a curve-based gain_db "
                "that can't be lowered to an ffmpeg expression; treating as 0dB.",
                track_name,
                band.frequency_hz,
            )
            gain_expr = "0.000000"
        # ffmpeg ``equalizer`` accepts numeric gain only; collapse animated to
        # constant when possible, otherwise fall back to 0dB. Q is mapped to
        # bandwidth via ``t=q``.
        gain_value = _try_float(gain_expr)
        nodes.append(
            equalizer(
                frequency=band.frequency_hz,
                width_type="q",
                width=band.q,
                gain=gain_value if gain_value is not None else 0.0,
            )
        )
    return nodes


def _try_float(expr: str) -> float | None:
    try:
        return float(expr)
    except (TypeError, ValueError):
        return None


def _render_fade_clause(fade: AudioFade | None, seg_duration: float) -> str | None:
    """Render an ``afade`` clause for the segment chain.

    Multiple fades are emitted as a comma-separated pair (``afade=in,...
    afade=out``). Returns ``None`` when neither fade is configured.
    """

    if fade is None:
        return None
    parts: list[str] = []
    if fade.fade_in > 0:
        parts.append(f"afade=t=in:st=0:d={fade.fade_in:.6f}")
    if fade.fade_out > 0:
        fade_out_start = max(seg_duration - fade.fade_out, 0.0)
        parts.append(
            f"afade=t=out:st={fade_out_start:.6f}:d={fade.fade_out:.6f}"
        )
    if not parts:
        return None
    return ",".join(parts)


def _render_reverb_clause(
    reverb: Reverb | None,
    segment_id: str,
    project: Project,
    markers: MarkerSet,
) -> str | None:
    """Render an ``aecho``-based reverb tap chain.

    ``room_size`` scales delay tap lengths, ``damping`` attenuates feedback,
    and ``wet`` (constant component) scales tap decay. Animated ``wet``
    collapses to its constant root when possible; non-constant ``wet`` falls
    back to the configured root value with a warning - ``aecho`` parameters
    aren't expression-evaluated by ffmpeg.
    """

    if reverb is None:
        return None
    wet = _extract_constant(reverb.wet)
    if wet is None:
        _LOG.warning(
            "audio segment %s reverb.wet is animated; using a constant fallback "
            "since ffmpeg's aecho does not accept expressions.",
            segment_id,
        )
        wet = 0.3
    wet = max(0.0, min(1.0, float(wet)))
    room_size = max(0.0, min(1.0, float(reverb.room_size)))
    damping = max(0.0, min(1.0, float(reverb.damping)))

    in_gain = 1.0 - 0.5 * wet
    out_gain = 0.4 + 0.6 * wet
    base_tap_ms = 40.0 + 200.0 * room_size
    delays = [base_tap_ms, base_tap_ms * 2.0, base_tap_ms * 3.0]
    feedback_attenuation = 1.0 - damping
    decays = [
        0.4 * feedback_attenuation,
        0.3 * feedback_attenuation,
        0.2 * feedback_attenuation,
    ]
    delays_str = "|".join(f"{d:.3f}" for d in delays)
    decays_str = "|".join(f"{d:.4f}" for d in decays)
    return (
        f"aecho={in_gain:.4f}:{out_gain:.4f}:{delays_str}:{decays_str}"
    )


def _apply_segment_ducking(
    ducking: Ducking,
    *,
    segment_id: str,
    track_name: str,
    seg_index: int,
    input_label: str,
    track_final_labels: dict[str, str],
    chains: list[str],
) -> str:
    """Wire a ``sidechaincompress`` keyed off another track's processed signal."""

    if ducking.source not in track_final_labels:
        raise ValueError(
            f"audio segment {segment_id!r} ducks to {ducking.source!r}, "
            "which has not been rendered yet - topological order is broken."
        )
    ducked_label = f"aseg_{_safe(track_name)}_{seg_index}_duck"
    chains.append(
        f"[{input_label}][{track_final_labels[ducking.source]}]"
        f"sidechaincompress="
        f"threshold={_db_to_linear(ducking.threshold_db):.6f}:"
        f"ratio={ducking.ratio}:"
        f"attack={ducking.attack_ms}:"
        f"release={ducking.release_ms}"
        f"[{ducked_label}]"
    )
    return ducked_label


def _apply_constant_pan(
    track: AudioTrack,
    input_label: str,
    chains: list[str],
) -> str:
    """Apply ``track.pan`` as a constant stereo pan; warn on animated pan."""

    pan_value = _extract_constant(track.pan)
    if pan_value is None:
        _LOG.warning(
            "audio track %s has an animated pan; using pan=0 (animated pan "
            "is not yet supported in the audio stem renderer).",
            track.name,
        )
        pan_value = 0.0
    if abs(pan_value) < 1e-6:
        return input_label

    # Constant-power stereo pan: pan in [-1, 1] maps to L/R gains.
    pan_value = max(-1.0, min(1.0, pan_value))
    left_gain = ((1.0 - pan_value) / 2.0) ** 0.5
    right_gain = ((1.0 + pan_value) / 2.0) ** 0.5
    panned_label = f"{input_label}_pan"
    chains.append(
        f"[{input_label}]"
        f"pan=stereo|c0={left_gain:.6f}*c0|c1={right_gain:.6f}*c1"
        f"[{panned_label}]"
    )
    return panned_label


def _extract_constant(animated: Animated[float] | float | None) -> float | None:
    if animated is None:
        return 0.0
    if isinstance(animated, Animated):
        root = animated.root
        if isinstance(root, (int, float)):
            return float(root)
        return None
    if isinstance(animated, (int, float)):
        return float(animated)
    return None


def _topo_order_for_sidechain(tracks: list[AudioTrack]) -> list[AudioTrack]:
    """Order tracks so each track's sidechain / ducking source appears first.

    Considers both track-level :attr:`AudioTrack.sidechain` and
    segment-level :attr:`AudioSegment.ducking` dependencies. A cycle in
    either kind is rejected.
    """

    by_name = {t.name: t for t in tracks}
    indegree: dict[str, int] = {t.name: 0 for t in tracks}
    edges: dict[str, list[str]] = {t.name: [] for t in tracks}

    def add_edge(source: str, downstream: str, origin: str) -> None:
        if source not in by_name:
            raise ValueError(
                f"{origin} references unknown audio track {source!r} "
                f"(available: {sorted(by_name)})."
            )
        if source == downstream:
            raise ValueError(f"{origin} references its own track {source!r}.")
        edges[source].append(downstream)
        indegree[downstream] += 1

    for track in tracks:
        if track.sidechain is not None:
            add_edge(
                track.sidechain.source,
                track.name,
                origin=f"audio track {track.name!r} sidechain",
            )
        for segment in track.segments:
            if segment.ducking is not None:
                add_edge(
                    segment.ducking.source,
                    track.name,
                    origin=f"audio segment {segment.id!r} ducking",
                )

    queue: deque[str] = deque(name for name, deg in indegree.items() if deg == 0)
    ordered: list[AudioTrack] = []
    while queue:
        name = queue.popleft()
        ordered.append(by_name[name])
        for downstream in edges[name]:
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                queue.append(downstream)

    if len(ordered) != len(tracks):
        cycle_members = [t.name for t in tracks if t.name not in {o.name for o in ordered}]
        raise ValueError(
            f"audio track sidechain config contains a cycle involving: {cycle_members}"
        )
    return ordered


def _db_to_linear(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def _safe(name: str) -> str:
    """Sanitize a track name for use inside a filter pad label."""

    return "".join(ch if ch.isalnum() else "_" for ch in name) or "track"
