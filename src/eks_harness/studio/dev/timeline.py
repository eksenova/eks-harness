"""Build the flattened timeline JSON consumed by the dev-server UI.

The UI's timeline widget walks tracks → segments → effects, draws marker
rugs underneath, and toggles whole layers via the ``used.*`` flags. Sending
the raw Project IR would force every layer to do its own pattern-matching
over the discriminated unions, so we flatten everything here:

* every segment's symbolic ``TimeRef`` is resolved to seconds (or ``None``
  when the resolution depends on markers that haven't been extracted);
* every effect is summarised as ``{kind, intensity_source}`` so the row
  renderer doesn't need the full effect schema;
* marker streams are loaded once from ``cache/markers/*.json`` and only the
  arrays the UI actually plots are forwarded (no megabyte-sized madmom
  activation buffers);
* the ``used.*`` flags are derived by inspecting what the project actually
  references, so the UI can hide unused layers without an extra round-trip.

The builder loads the project source through the sandbox runner so untrusted
project code never executes inside the dev-server event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from ..sandbox.runner import SandboxResult, run_project_py
from .time_resolve import resolve as resolve_timeref

__all__ = ["TimelineLoadError", "build_timeline"]

_LOG = logging.getLogger(__name__)

# Only the marker streams the UI actually draws. Trimming the response keeps
# the wire payload small (the BeatNet kick array is small; madmom activation
# blobs would be megabytes).
_BEAT_STREAMS_OF_INTEREST = ("kick", "snare", "beat", "downbeat")
_SCENE_STREAMS_OF_INTEREST = ("scenes", "cuts", "shots")


class TimelineLoadError(RuntimeError):
    """Raised when the sandbox refuses to load the project source.

    Carries the sandbox stderr summary so the WS handler can surface a
    structured ``project_load_error`` envelope to the client.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


async def build_timeline_async(project_dir: Path) -> dict[str, Any]:
    """Async wrapper around :func:`build_timeline` for use inside handlers.

    The sandbox launch itself is already async; reading marker files and the
    rest of the IR walk is plain I/O so we hand it to the default executor
    to keep the event loop responsive.
    """

    project_json = await _load_project_via_sandbox(project_dir)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _build_from_project_json, project_dir, project_json
    )


def build_timeline(project_dir: Path) -> dict[str, Any]:
    """Synchronous variant - used by tests and ad-hoc callers.

    Runs the sandbox via a private event loop and then performs the flat
    walk on the current thread. Production callers should prefer
    :func:`build_timeline_async`.
    """

    project_json = asyncio.run(_load_project_via_sandbox(project_dir))
    return _build_from_project_json(project_dir, project_json)


async def _load_project_via_sandbox(project_dir: Path) -> dict[str, Any]:
    project_py = project_dir / "project.py"
    if not project_py.exists():
        raise TimelineLoadError(f"no project.py at {project_dir}")

    source = project_py.read_text(encoding="utf-8")
    result: SandboxResult = await run_project_py(source, cwd=project_dir)
    if not result.ok or result.project_json is None:
        raise TimelineLoadError(
            result.error or "sandbox failed to load project",
            detail=result.stderr[-2000:] if result.stderr else None,
        )

    try:
        return json.loads(result.project_json)
    except json.JSONDecodeError as exc:
        raise TimelineLoadError(f"sandbox project_json was not valid JSON: {exc}") from exc


def _build_from_project_json(
    project_dir: Path, project_json: dict[str, Any]
) -> dict[str, Any]:
    fps = float(project_json.get("fps") or 30.0)
    resolution = tuple(project_json.get("resolution") or (1080, 1920))
    duration = float(project_json.get("duration") or 0.0)

    marker_sources = list(project_json.get("markers") or [])
    markers = _load_markers(project_dir, marker_sources)

    track_models = list(project_json.get("tracks") or [])
    audio_models = list(project_json.get("audio_tracks") or [])

    referenced_beat_streams: set[str] = set()
    used_captions = False
    transitions: list[dict[str, Any]] = []
    captions_payload: dict[str, Any] | None = None
    captions_sources: set[str] = set()

    tracks: list[dict[str, Any]] = []
    for track in track_models:
        flat_segments: list[dict[str, Any]] = []
        segments = list(track.get("segments") or [])
        for seg in segments:
            start_s, end_s = _segment_window(
                seg, project_json=project_json, markers=markers, duration=duration
            )
            effects_summary, seg_beat_refs, seg_caption_sources = _summarise_effects(
                seg.get("effects") or []
            )
            referenced_beat_streams.update(seg_beat_refs)
            if seg_caption_sources:
                used_captions = True
                captions_sources.update(seg_caption_sources)

            flat_segments.append(
                {
                    "id": seg.get("id"),
                    "start": start_s,
                    "end": end_s,
                    "media": _media_basename(seg.get("media")),
                    "effects": effects_summary,
                    "transition_in": _summarise_transition(seg.get("transition_in")),
                    "transition_out": _summarise_transition(seg.get("transition_out")),
                }
            )

        # Track-level transitions sit between specific segment IDs; the IR
        # encodes them in ``transitions`` adjacent to ``segments``. Surface
        # the non-Cut ones so the UI can draw a glyph at the boundary.
        for transition in track.get("transitions") or []:
            summary = _summarise_inter_segment_transition(
                transition, segments=segments, project_json=project_json, markers=markers
            )
            if summary is not None:
                transitions.append(summary)

        tracks.append({"name": track.get("name"), "z": int(track.get("z") or 0), "segments": flat_segments})

    audio_tracks: list[dict[str, Any]] = []
    for track in audio_models:
        flat_segments = []
        for seg in track.get("segments") or []:
            start_s, end_s = _segment_window(
                seg, project_json=project_json, markers=markers, duration=duration
            )
            flat_segments.append(
                {
                    "id": seg.get("id"),
                    "start": start_s,
                    "end": end_s,
                    "media": _media_basename(seg.get("media")),
                }
            )
        audio_tracks.append({"name": track.get("name"), "segments": flat_segments})

    # Captions panel: pull word arrays for any referenced STT source.
    if used_captions:
        captions_payload = _build_captions_payload(markers, captions_sources)

    used = {
        "beats": bool(referenced_beat_streams),
        "captions": used_captions,
        "scenes": any(
            (entry or {}).get("kind") == "scene_markers" for entry in marker_sources
        ),
        "transitions": bool(transitions),
    }

    return {
        "fps": fps,
        "resolution": [int(resolution[0]), int(resolution[1])],
        "duration": duration,
        "tracks": tracks,
        "audio_tracks": audio_tracks,
        "markers": markers,
        "captions": captions_payload,
        "transitions": transitions,
        "used": used,
    }


def _segment_window(
    seg: dict[str, Any],
    *,
    project_json: dict[str, Any],
    markers: dict[str, Any],
    duration: float,
) -> tuple[float | None, float | None]:
    """Compute resolved (start, end) seconds for a segment.

    ``start`` is the segment's anchor on the project timeline. The ``out``
    field on the media handle gives the in-clip end; we pair that with
    ``in`` to derive a clip-duration which is then added to ``start``. When
    a TimeRef can't be resolved (marker not yet extracted) the field is
    returned as ``None`` and the UI surfaces a placeholder.
    """

    start_s = resolve_timeref(
        seg.get("start"), project_json=project_json, markers=markers
    )
    in_s = resolve_timeref(seg.get("in"), project_json=project_json, markers=markers)
    out_s = resolve_timeref(seg.get("out"), project_json=project_json, markers=markers)

    if start_s is None:
        return None, None
    if in_s is not None and out_s is not None:
        return start_s, start_s + max(0.0, out_s - in_s)
    if out_s is not None and (in_s is None or in_s == 0):
        return start_s, start_s + out_s
    return start_s, None


def _summarise_effects(
    effects: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """Flatten a segment's effect list into ``{kind, intensity_source}`` rows.

    Returns the summary plus a set of beat streams referenced for the
    ``used.beats`` flag and a set of STT sources referenced by ``Captions``
    effects for the ``used.captions`` flag.
    """

    summary: list[dict[str, Any]] = []
    beat_streams: set[str] = set()
    caption_sources: set[str] = set()

    for effect in effects:
        kind = effect.get("kind") or ""
        class_name = _kind_to_class_name(kind)
        entry: dict[str, Any] = {"kind": class_name}

        if kind == "captions":
            source = effect.get("source")
            if isinstance(source, str):
                entry["source"] = source
                caption_sources.add(source)
        else:
            source_label, referenced = _intensity_source(effect)
            if source_label is not None:
                entry["intensity_source"] = source_label
            beat_streams.update(referenced)

        summary.append(entry)

    return summary, beat_streams, caption_sources


def _intensity_source(effect: dict[str, Any]) -> tuple[str | None, set[str]]:
    """Return a human-readable intensity source label and any beat streams used.

    The dev UI only needs to know *what* is driving the effect (so it can
    color the row and draw the beat ticks alongside). It doesn't need the
    full Animated value tree, so we collapse it to one of:

    * ``"beat:<stream>"`` for a BeatPulse triggered by a BeatRef;
    * ``"word:<text or #idx>"`` for word-triggered effects;
    * ``"static"`` for plain numeric intensities;
    * ``None`` when the effect has no intensity at all.
    """

    intensity = effect.get("intensity")
    streams: set[str] = set()

    if intensity is None:
        return None, streams

    if isinstance(intensity, (int, float)):
        return "static", streams

    if isinstance(intensity, dict):
        kind = intensity.get("kind")
        # Animated wrappers are unwrapped on serialise to their root, which
        # may already be a BeatPulse / Curve / scalar.
        if kind == "beat_pulse":
            trigger = intensity.get("trigger") or {}
            if trigger.get("kind") == "beat":
                stream = trigger.get("stream")
                if isinstance(stream, str):
                    streams.add(stream)
                    return f"beat:{stream}", streams
            return "beat", streams
        if kind == "beat":
            stream = intensity.get("stream")
            if isinstance(stream, str):
                streams.add(stream)
                return f"beat:{stream}", streams
        if kind == "word":
            text = intensity.get("text") or intensity.get("index")
            return f"word:{text}", streams
        return "animated", streams

    return None, streams


def _summarise_transition(transition: Any) -> dict[str, Any] | None:
    """Reduce a transition IR dict to ``{kind, ...}`` or ``None`` for Cut.

    Cut is the default and uninteresting (no visual artifact between segments)
    so we drop it; everything else round-trips as a dict so the UI can
    introspect the parameters without owning the full schema.
    """

    if not isinstance(transition, dict):
        return None
    kind = transition.get("kind")
    if not kind or kind == "cut":
        return None
    return {k: v for k, v in transition.items() if k != "compile_targets"}


def _summarise_inter_segment_transition(
    transition: dict[str, Any],
    *,
    segments: list[dict[str, Any]],
    project_json: dict[str, Any],
    markers: dict[str, Any],
) -> dict[str, Any] | None:
    """Build a ``{at, kind, duration?, between}`` entry for a track-level transition.

    Track-level transitions reference adjacent segments by id (``from``/``to``
    or ``a``/``b``); the renderer overlays them at the boundary. We surface
    them so the UI can draw a glyph at ``at`` between the two segment ids.
    """

    kind = transition.get("kind")
    if not kind or kind == "cut":
        return None

    a_id = transition.get("from") or transition.get("a")
    b_id = transition.get("to") or transition.get("b")
    if not a_id or not b_id:
        return None

    # Anchor the transition at segment B's start (where the new clip arrives).
    at: float | None = None
    for seg in segments:
        if seg.get("id") == b_id:
            at = resolve_timeref(
                seg.get("start"), project_json=project_json, markers=markers
            )
            break

    entry: dict[str, Any] = {
        "at": at,
        "kind": kind,
        "between": [a_id, b_id],
    }
    duration = transition.get("duration")
    resolved_duration = resolve_timeref(
        duration, project_json=project_json, markers=markers
    )
    if resolved_duration is not None:
        entry["duration"] = resolved_duration
    return entry


def _build_captions_payload(
    markers: dict[str, Any], sources: set[str]
) -> dict[str, Any] | None:
    """Collect word arrays for every STT source referenced by a Captions effect."""

    payload: dict[str, Any] = {}
    for source in sources:
        entry = markers.get(source)
        if not entry:
            payload[source] = {"words": [], "sentences": []}
            continue
        streams = entry.get("streams") or {}
        payload[source] = {
            "words": streams.get("words") or [],
            "sentences": streams.get("sentences") or [],
        }
    return payload or None


def _media_basename(media: Any) -> str | None:
    if not isinstance(media, dict):
        return None
    path = media.get("path")
    if isinstance(path, str) and path:
        return Path(path).name
    return None


def _kind_to_class_name(kind: str) -> str:
    """Convert a snake_case IR discriminator back to the pascal-case class name.

    Effect IR classes use ``Literal["snake_case"]`` discriminators (``shake``
    → :class:`Shake`); the timeline UI displays the class name to match what
    a user wrote in ``project.py``. Unknown kinds fall back to the raw
    string so plugins still render readably.
    """

    if not kind:
        return ""
    return "".join(part.capitalize() for part in kind.split("_"))


# ---------------------------------------------------------------------------
# Marker cache loading
# ---------------------------------------------------------------------------


def _load_markers(
    project_dir: Path, marker_sources: list[dict[str, Any]]
) -> dict[str, Any]:
    """Walk the declared marker sources and pair each with its on-disk cache.

    Sources without a matching cache file get ``streams: {}`` so the UI can
    still show the declaration ("you said you wanted beats, here's the
    placeholder") even before the first render has populated the cache.
    """

    markers_dir = project_dir / "cache" / "markers"
    by_kind: dict[str, list[Path]] = {"beat": [], "stt": [], "scene": []}
    if markers_dir.exists():
        for path in markers_dir.glob("*.json"):
            name = path.name
            if name.startswith("beat-"):
                by_kind["beat"].append(path)
            elif name.startswith("stt-"):
                by_kind["stt"].append(path)
            elif name.startswith("scene-"):
                by_kind["scene"].append(path)

    out: dict[str, Any] = {}
    for source in marker_sources:
        kind = source.get("kind")
        name = source.get("name")
        if not isinstance(name, str):
            continue
        if kind == "beat_tracker":
            streams = _pick_beat_streams(by_kind["beat"], source)
            out[name] = {"kind": "BeatTracker", "streams": streams}
        elif kind == "stt_markers":
            streams = _pick_stt_streams(by_kind["stt"])
            out[name] = {"kind": "STTMarkers", "streams": streams}
        elif kind == "scene_markers":
            streams = _pick_scene_streams(by_kind["scene"])
            out[name] = {"kind": "SceneMarkers", "streams": streams}
    return out


def _pick_beat_streams(
    candidates: list[Path], source: dict[str, Any]
) -> dict[str, list[float]]:
    """Pick the matching beat cache file for a declared BeatTracker source.

    The cache filename pattern is ``beat-<digest>-<bpm>-<backend>.json``.
    We don't know the digest from inside the dev server (it depends on the
    audio source mtime), so we prefer the most recently-modified file when
    multiple exist - that matches "the last render's output".
    """

    if not candidates:
        return {}

    backend = source.get("backend") or "auto"
    # Filter by backend tag in the filename when one matches; otherwise fall
    # through to mtime-newest.
    filtered = [p for p in candidates if f"-{backend}." in p.name or f"-{backend}-" in p.name]
    pool = filtered or candidates
    chosen = max(pool, key=lambda p: p.stat().st_mtime)

    try:
        data = json.loads(chosen.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _LOG.warning("failed to read beat marker cache %s", chosen)
        return {}

    streams: dict[str, list[float]] = {}
    if isinstance(data, dict):
        for stream in _BEAT_STREAMS_OF_INTEREST:
            value = data.get(stream)
            if isinstance(value, list):
                streams[stream] = [float(t) for t in value]
    return streams


def _pick_stt_streams(candidates: list[Path]) -> dict[str, Any]:
    if not candidates:
        return {}
    chosen = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(chosen.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _LOG.warning("failed to read stt marker cache %s", chosen)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    if isinstance(data.get("words"), list):
        out["words"] = data["words"]
    if isinstance(data.get("sentences"), list):
        out["sentences"] = data["sentences"]
    return out


def _pick_scene_streams(candidates: list[Path]) -> dict[str, list[float]]:
    if not candidates:
        return {}
    chosen = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(chosen.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _LOG.warning("failed to read scene marker cache %s", chosen)
        return {}
    streams: dict[str, list[float]] = {}
    if isinstance(data, dict):
        for stream in _SCENE_STREAMS_OF_INTEREST:
            value = data.get(stream)
            if isinstance(value, list):
                streams[stream] = [float(t) for t in value]
    if not streams and isinstance(data, list):
        streams["scenes"] = [float(t) for t in data]
    return streams
