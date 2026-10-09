"""Timeline context for bpy scripts rendered by the video engine's ``BlenderScene`` (stdlib only).

Inside a ``BlenderScene`` script::

    import eks_harness.blender as vdb

    cube = bpy.data.objects["Cube"]
    for f in vdb.frames("beat"):            # scene frame of every beat in this segment
        cube.scale = (1.2, 1.2, 1.2)
        cube.keyframe_insert("scale", frame=f)
        cube.scale = (1.0, 1.0, 1.0)
        cube.keyframe_insert("scale", frame=f + 6)
    color = vdb.params.get("color", "gold")

Scene frame ``frame_zero`` is source time 0; the segment renders scene frames
``frame_start`` .. ``frame_end``. Marker times arrive on the project timeline
and are mapped through the segment's start, ``in`` and constant speed, so a
key placed on ``frame_at_project(t)`` lands exactly on project time ``t`` in
the final video.
"""

from __future__ import annotations

fps: float = 30.0
width: int = 1920
height: int = 1080
frame_zero: int = 1
frame_start: int = 1
frame_end: int = 1
frame_count: int = 1
source_start: float = 0.0
source_end: float = 0.0
project_start: float = 0.0
speed: float | None = 1.0
params: dict = {}
markers: dict = {"streams": {}, "named": {}, "words": []}
media: dict = {}
"""Nested media rendered for this segment, by name -> local file path (e.g. ``media["screen"]``)."""
segment_id: str = ""
score_inputs: list = []
emitted: list = []


def _load(config: dict) -> None:
    g = globals()
    for key in ("fps", "width", "height", "frame_zero", "frame_count", "source_start", "source_end",
                "project_start", "speed", "params", "markers", "segment_id"):
        g[key] = config[key]
    g["media"] = dict(config.get("media") or {})
    g["score_inputs"] = sorted((dict(item) for item in config.get("inputs") or []), key=lambda i: float(i["time"]))
    g["emitted"] = []
    g["frame_start"] = config["first_frame"]
    g["frame_end"] = config["first_frame"] + config["frame_count"] - 1


def project_to_source(t: float) -> float:
    """Source time (seconds on this scene's clock) shown at project time ``t``."""

    return source_start + (t - project_start) * (speed if speed is not None else 1.0)


def frame_at(source_t: float) -> float:
    """Scene frame (fractional) for a source time."""

    return frame_zero + source_t * fps


def frame_at_project(t: float) -> float:
    """Scene frame (fractional) that appears at project time ``t``."""

    return frame_at(project_to_source(t))


def _keep(frame: float, inside: bool) -> bool:
    return not inside or frame_start - 0.5 <= frame <= frame_end + 0.5


def times(stream: str) -> list[float]:
    """Project times of a marker stream (``beat``, ``downbeat``, ``onset``, ...)."""

    return list(markers["streams"].get(stream, []))


def frames(stream: str, *, inside: bool = True) -> list[int]:
    """Scene frames of a marker stream, rounded; by default only those this segment renders."""

    out = (round(frame_at_project(t)) for t in times(stream))
    return [f for f in out if _keep(f, inside)]


def named(name: str, *, inside: bool = True) -> list[int]:
    """Scene frames of a named marker."""

    out = (round(frame_at_project(t)) for t in markers["named"].get(name, []))
    return [f for f in out if _keep(f, inside)]


def words(*, inside: bool = True) -> list[dict]:
    """Transcribed words with their scene frames: ``{"text", "frame_start", "frame_end"}``."""

    result = []
    for w in markers["words"]:
        a, b = round(frame_at_project(w["t_start"])), round(frame_at_project(w["t_end"]))
        if _keep(a, inside) or _keep(b, inside):
            result.append({"text": w["text"], "frame_start": a, "frame_end": b})
    return result


def inputs(name: str | None = None) -> list[dict]:
    """Score inputs for this segment (``emit``/``set``/``key`` actions), each with its scene ``frame``."""

    found = []
    for item in score_inputs:
        if name is not None and item.get("name") != name and item.get("prop") != name:
            continue
        found.append({**item, "frame": round(frame_at(float(item["time"])))})
    return found


def input_frames(name: str) -> list[int]:
    """Scene frames at which the score sends ``name`` to this scene."""

    return [item["frame"] for item in inputs(name)]


def emit(name: str, frame: float | None = None, data: dict | None = None) -> None:
    """Emit a score event at a scene frame (default: the segment's first frame) for other tracks to react to."""

    at = float(frame_start if frame is None else frame)
    emitted.append({"name": name, "frame": round(at), "time": (at - frame_zero) / fps, "data": data or {}})
