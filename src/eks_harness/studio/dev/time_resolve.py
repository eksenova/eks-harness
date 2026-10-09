"""TimeRef resolution for the dev-server timeline builder.

The timeline endpoint needs concrete seconds for every segment boundary so
the UI can lay them out on a time axis. Symbolic ``TimeRef`` values
(``BeatRef``, ``WordRef``, ``MarkerRef``) can only be resolved once the
relevant marker stream has been extracted; until then we return ``None`` so
callers can render a ``"?"`` placeholder rather than collapsing the segment
to zero.

The functions here operate on the JSON dump of the Project IR (as produced
by the sandbox runner). That way the timeline builder never touches the
real pydantic models and works even when the IR class hierarchy is patched
by a project-local plugin.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "parse_time_string",
    "resolve",
]


def parse_time_string(spec: str) -> dict[str, Any] | None:
    """Parse the compact ``TimeRef`` shorthand into a JSON-shaped dict.

    Mirrors :func:`eks_harness.video.ir.time.parse_time_string` but returns the JSON
    payload (``{"kind": ..., ...}``) rather than pydantic models, so the
    timeline builder can pipe the result straight back into :func:`resolve`.
    """

    if not spec:
        return None

    if spec.startswith("f:"):
        try:
            return {"kind": "frames", "n": int(spec[2:])}
        except ValueError:
            return None
    if spec.startswith("b:"):
        rest = spec[2:]
        bits = rest.split(":")
        if len(bits) == 1:
            return {"kind": "beat", "stream": bits[0], "every": 1}
        if len(bits) == 2:
            try:
                return {"kind": "beat", "stream": bits[0], "every": int(bits[1])}
            except ValueError:
                return None
        return None
    if spec.startswith("w:"):
        rest = spec[2:]
        if rest.startswith("#"):
            try:
                return {"kind": "word", "index": int(rest[1:])}
            except ValueError:
                return None
        return {"kind": "word", "text": rest}
    if spec.startswith("m:"):
        return {"kind": "marker", "name": spec[2:]}

    parts = spec.split(":")
    try:
        if len(parts) == 1:
            return {"kind": "seconds", "t": float(parts[0])}
        if len(parts) == 2:
            minutes, seconds = parts
            return {"kind": "seconds", "t": int(minutes) * 60 + float(seconds)}
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return {
                "kind": "seconds",
                "t": int(hours) * 3600 + int(minutes) * 60 + float(seconds),
            }
    except ValueError:
        return None
    return None


def _stream_times(markers: dict[str, Any], stream: str) -> list[float] | None:
    """Find a list of stream times across every marker source in ``markers``.

    ``markers`` mirrors the response shape from the timeline builder:
    ``{<source_name>: {"streams": {<stream_name>: [t, ...]}, ...}, ...}``.
    The first match wins - beat streams (``"kick"``, ``"snare"``, ...) are
    unique per project, so this is unambiguous in practice.
    """

    for entry in markers.values():
        streams = (entry or {}).get("streams") or {}
        times = streams.get(stream)
        if isinstance(times, list) and times:
            return [float(t) for t in times]
    return None


def _named_marker_times(
    markers: dict[str, Any], name: str
) -> list[float] | None:
    """Look up a marker stream by source ``name`` (e.g. an STT or scene source).

    Returns ``None`` if no source by that name has any stream data.
    """

    entry = markers.get(name)
    if not entry:
        return None
    streams = entry.get("streams") or {}
    # Take the first non-empty stream - the UI uses this for cue resolution.
    for value in streams.values():
        if isinstance(value, list) and value:
            return [float(t) for t in value]
    return None


def resolve(
    timeref_json: Any,
    *,
    project_json: dict[str, Any],
    markers: dict[str, Any],
) -> float | None:
    """Resolve a TimeRef JSON dict (or shorthand string) to seconds.

    Returns ``None`` for unknown kinds or for marker references whose stream
    has no data yet (because the marker extractor hasn't run). Callers
    surface ``None`` as a ``null`` start/end so the UI can display ``"?"``.
    """

    if timeref_json is None:
        return None

    if isinstance(timeref_json, (int, float)):
        return float(timeref_json)

    if isinstance(timeref_json, str):
        parsed = parse_time_string(timeref_json)
        if parsed is None:
            return None
        return resolve(parsed, project_json=project_json, markers=markers)

    if not isinstance(timeref_json, dict):
        return None

    kind = timeref_json.get("kind")
    fps = float(project_json.get("fps") or 30.0)

    if kind == "seconds":
        try:
            return float(timeref_json["t"])
        except (KeyError, TypeError, ValueError):
            return None

    if kind == "frames":
        try:
            return float(timeref_json["n"]) / fps
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None

    if kind == "beat":
        stream = timeref_json.get("stream")
        if not isinstance(stream, str):
            return None
        times = _stream_times(markers, stream)
        if times is None:
            return None
        every = int(timeref_json.get("every") or 1)
        index = timeref_json.get("index")
        offset_ms = float(timeref_json.get("offset_ms") or 0.0)
        selected = times[::every] if every > 1 else times
        if index is not None:
            try:
                idx = int(index)
            except (TypeError, ValueError):
                return None
            if 0 <= idx < len(selected):
                return selected[idx] + offset_ms / 1000.0
            return None
        if not selected:
            return None
        return selected[0] + offset_ms / 1000.0

    if kind == "word":
        source = timeref_json.get("source")
        text = timeref_json.get("text")
        index = timeref_json.get("index")
        # Try the named source first, else any STT source.
        candidates: list[dict[str, Any]] = []
        if isinstance(source, str) and source in markers:
            candidates.append(markers[source])
        else:
            for entry in markers.values():
                if (entry or {}).get("kind") == "stt_markers":
                    candidates.append(entry)
        for entry in candidates:
            streams = (entry or {}).get("streams") or {}
            words = streams.get("words")
            if not isinstance(words, list) or not words:
                continue
            if index is not None:
                try:
                    idx = int(index)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(words):
                    return _word_start(words[idx])
            if isinstance(text, str):
                for word in words:
                    if str(word.get("text", "")).strip().lower() == text.strip().lower():
                        return _word_start(word)
        return None

    if kind == "marker":
        name = timeref_json.get("name")
        if not isinstance(name, str):
            return None
        times = _named_marker_times(markers, name)
        if times is None:
            return None
        index = timeref_json.get("index")
        if index is None:
            return times[0] if times else None
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return None
        if 0 <= idx < len(times):
            return times[idx]
        return None

    return None


def _word_start(word: Any) -> float | None:
    if isinstance(word, dict):
        start = word.get("start") or word.get("t") or word.get("ts")
        try:
            return float(start) if start is not None else None
        except (TypeError, ValueError):
            return None
    if isinstance(word, (int, float)):
        return float(word)
    return None
