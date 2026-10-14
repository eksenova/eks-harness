from __future__ import annotations

import keyword
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast
from weakref import WeakKeyDictionary

from pydantic import JsonValue

from eks.ref import (
    SENTINEL_CLOSE,
    SENTINEL_OPEN,
    Encodable,
    ItemRef,
    Json,
    Media,
    Ref,
    RefUsageError,
    encode,
    node_of,
    type_of,
)
from eks.tracing import Scope, TraceContext, TraceError, current
from eks.ui import Text, to_text
from eks_harness.ir.catalog.markers import (
    CLOCK_STREAMS,
    BeatTracker,
    EnergyMarkers,
    FaceMarkers,
    MarkerBase,
    MomentumMarkers,
    MotionMarkers,
    OnsetMarkers,
    SceneMarkers,
    SilenceMarkers,
    STTMarkers,
)
from eks_harness.ir.ops.audio import Stem
from eks_harness.ir.ops.media import Speech, StemModel, Stems
from eks_harness.ir.refs import FmtExpr, ItemExpr, RefError, parse_expr
from eks_harness.ir.types import (
    FONT_EXTENSIONS,
    SHIPPED_FONTS,
    MediaKind,
    RefTypeError,
    infer_path,
    normalize_media_uri,
)

MediaLike = Media | str | Encodable
FontLike = MediaLike
URI_PREFIXES = ("artifact:", "file:", "http://", "https://")

SOURCE_OP = "media.source"
MARKERS_OP = "media.markers"
TTS_OP = "media.tts"
STEMS_OP = "media.stems"
MEDIA_TYPES = frozenset({"media", "json"})
FILE_PREFIX = "file:"

_SOURCES: WeakKeyDictionary[TraceContext, dict[tuple[str, str | None], list[tuple[tuple[Scope, ...], Media]]]] = (
    WeakKeyDictionary()
)
_ANALYSES: WeakKeyDictionary[TraceContext, dict[str, str]] = WeakKeyDictionary()
_EMBEDDINGS: WeakKeyDictionary[TraceContext, list[tuple[str, int]]] = WeakKeyDictionary()


def source(uri: str, *, kind: MediaKind | None = None, origin: str | None = None,
           renderer: str | None = None) -> Media:
    if not isinstance(uri, str):
        raise TypeError(
            "eks.media.source() takes the media as a string (a path, file:PATH, artifact:ID or an http(s) URL), "
            f"got {type(uri).__name__} {uri!r}; convert paths with str(path)"
        )
    if SENTINEL_OPEN in uri or SENTINEL_CLOSE in uri:
        raise RefUsageError(
            f"media uri {uri!r} is built from a ref; media sources are literal paths, artifact ids or URLs known "
            "when the plan is built, so pass the media Ref itself where media is accepted"
        )
    normalized = normalize_media_uri(uri)
    ctx = current()
    if normalized.startswith(FILE_PREFIX):
        normalized = ctx.media_uri(normalized.removeprefix(FILE_PREFIX), origin)
    chosen = _renderer(ctx, normalized, renderer)
    recorded = _SOURCES.setdefault(ctx, {}).setdefault((normalized, kind), [])
    chain = _open_chain(ctx)
    for where, known in recorded:
        if _visible_from(ctx, where, chain, node_of(known)):
            return known
    args: dict[str, Any] = {"uri": normalized}
    if kind is not None:
        args["kind"] = kind
    if chosen is not None:
        args["renderer"] = chosen
    id_key = f"media:{normalized}" if kind is None else f"media:{kind}:{normalized}"
    ref = cast("Media", ctx.record(SOURCE_OP, args, id_key=id_key))
    recorded.append((chain, ref))
    return ref


def _renderer(ctx: TraceContext, uri: str, renderer: str | None) -> str | None:
    known = {spec.media_kind for spec in ctx.registry.renderers()}
    if renderer is not None:
        if renderer not in known:
            raise TraceError(f"media {uri!r} names renderer {renderer!r}, which no loaded plugin registers; "
                             f"registered renderers: {', '.join(sorted(known)) or 'none'}")
        return renderer
    path = uri.split("?", 1)[0].rsplit("/", 1)[-1]
    extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return extension if extension in known else None


def _open_chain(ctx: TraceContext) -> tuple[Scope, ...]:
    return tuple(ctx._scopes)


def _visible_from(ctx: TraceContext, where: tuple[Scope, ...], chain: tuple[Scope, ...], node_id: str) -> bool:
    try:
        ctx.node(node_id)
    except TraceError:
        return False
    open_keys = {scope.key for scope in chain}
    for scope in reversed(where):
        if scope.key in open_keys:
            return True
        if scope.container is None:
            return False
        spec = ctx.registry.op(ctx.node(scope.container).op)
        if not spec.exports_body:
            return False
    return False


def _from_encoding(value: Encodable) -> Ref[Any]:
    encoded = encode(value)
    try:
        expr = parse_expr(encoded)
    except RefError as error:
        raise RefUsageError(
            f"{type(value).__name__} encodes as {encoded!r}, which is not a ref to media; "
            "pass a media Ref, a take or a media path"
        ) from error
    if isinstance(expr, FmtExpr):
        raise RefUsageError(
            f"{type(value).__name__} encodes as a formatted string {encoded!r}, not media; "
            "pass a media Ref, a take or a media path"
        )
    if isinstance(expr, ItemExpr):
        return ItemRef(expr.item, "json", expr.path)
    try:
        produced = infer_path(current().node(expr.ref).out, expr.path)
    except RefTypeError as error:
        raise RefUsageError(f"{type(value).__name__} encodes as a ref with an impossible path: {error}") from error
    return Ref(expr.ref, produced, expr.path)


def _media_ref(ref: Ref[Any], shown: object) -> Media:
    produced = type_of(ref)
    if produced not in MEDIA_TYPES:
        raise RefUsageError(
            f"{shown!r} is a Ref of type {produced}, not media; pass a ref returned by a media producing op "
            "(eks.media.source, an edit, scene or take render) or a media path"
        )
    return cast("Media", ref)


def as_media(value: MediaLike, *, kind: MediaKind | None = None, origin: str | None = None) -> Media:
    if isinstance(value, Ref):
        return _media_ref(cast("Ref[Any]", value), value)
    if isinstance(value, str):
        return source(value, kind=kind, origin=origin)
    if isinstance(value, Encodable):
        return _media_ref(_from_encoding(value), value)
    raise RefUsageError(
        "expected media: a media Ref, a take, or a path, file:PATH, artifact:ID or http(s) URL string; "
        f"got {type(value).__name__} {value!r}"
    )


def as_font(value: FontLike, what: str = "font", *, origin: str | None = None) -> JsonValue:
    if isinstance(value, str) and value in SHIPPED_FONTS:
        return value
    if isinstance(value, str):
        path = value.split("?", 1)[0].lower()
        if not (value.startswith(URI_PREFIXES) or path.endswith(FONT_EXTENSIONS)):
            raise ValueError(
                f"{what} {value!r} is neither a font family the renderer ships ({', '.join(SHIPPED_FONTS)}) nor a font "
                f"file ({', '.join(FONT_EXTENSIONS)}); pass the font file, such as 'fonts/{value.replace(' ', '')}.ttf', "
                "so its content is part of the plan"
            )
    return encode(as_media(value, kind="file", origin=origin))


def tts(
    text: Text, *, voice: str = "default", backend: str = "kokoro", language: str | None = None,
    speed: float = 1.0, seed: int | None = None,
) -> Media:
    args = Speech.model_validate(encode({
        "text": to_text(text, "eks.media.tts text"), "voice": voice, "backend": backend, "language": language,
        "speed": speed, "seed": seed}))
    return cast("Media", current().record(TTS_OP, args.model_dump(mode="json", by_alias=True, exclude_none=True)))


def stems(media: MediaLike, *names: Stem, model: StemModel = "htdemucs") -> Media:
    if not names:
        raise TypeError("eks.media.stems() needs at least one stem: 'vocals', 'drums', 'bass' or 'other'")
    args = Stems.model_validate({"media": encode(as_media(media)), "stems": names, "model": model})
    return cast("Media", current().record(STEMS_OP, args.model_dump(mode="json", by_alias=True)))


def markers(media: MediaLike, *sources: MarkerBase) -> Ref[Json]:
    if not sources:
        raise TypeError(
            "eks.media.markers() needs at least one marker source, for example "
            "BeatTracker(name='song', streams=('beat', 'downbeat'))"
        )
    ctx = current()
    for item in sources:
        if not isinstance(item, MarkerBase):
            raise TypeError(
                "marker sources are marker models such as BeatTracker(...) or STTMarkers(...), "
                f"got {type(item).__name__} {item!r}"
            )
        ctx.require(ctx.registry.catalog("marker", item.kind).provenance)
    claimed = _claim_names(ctx, sources)
    ref = as_media(media)
    recorded = ctx.record(MARKERS_OP, {"media": ref, "sources": list(sources)})
    _ANALYSES.setdefault(ctx, {}).update(claimed)
    return recorded


@contextmanager
def scene_analyses(ctx: TraceContext, scene: str, number: int) -> Iterator[None]:
    stack = _EMBEDDINGS.setdefault(ctx, [])
    stack.append((scene, number))
    try:
        yield
    finally:
        stack.pop()


def _claim_names(ctx: TraceContext, sources: tuple[MarkerBase, ...]) -> dict[str, str]:
    known = _ANALYSES.get(ctx, {})
    embedding = _EMBEDDINGS.get(ctx) or []
    scene = embedding[-1] if embedding else None
    where = f" in embedding #{scene[1] + 1} of scene {scene[0]!r}" if scene is not None else ""
    claimed: dict[str, str] = {}
    for item in sources:
        owner = f"{type(item).__name__} {item.name!r}{where}"
        for name in dict.fromkeys((item.name, *item.stream_names())):
            if name in CLOCK_STREAMS:
                raise TraceError(
                    f"{owner} produces the stream {name!r}, which is reserved for the clock tempo "
                    f"({', '.join(sorted(CLOCK_STREAMS))}); name the analysis after its media")
            earlier = claimed.get(name) or known.get(name)
            if earlier is None:
                claimed[name] = owner
                continue
            hint = (f"; marker names are plan-wide, so scene {scene[0]!r} must take the name of its analysis as an "
                    f"input (a parameter such as {_parameter(item.name)}: str) and each embedding must pass its own "
                    "name" if scene is not None else "; give each analysis its own name")
            raise TraceError(f"{owner} provides {name!r}, which {earlier} already provides{hint}")
    return claimed


def _parameter(name: str) -> str:
    return name if name.isidentifier() and not keyword.iskeyword(name) else f"{name}_name"


__all__ = [
    "FILE_PREFIX",
    "MARKERS_OP",
    "MEDIA_TYPES",
    "SOURCE_OP",
    "STEMS_OP",
    "TTS_OP",
    "BeatTracker",
    "EnergyMarkers",
    "FaceMarkers",
    "FontLike",
    "MarkerBase",
    "MediaLike",
    "MomentumMarkers",
    "MotionMarkers",
    "OnsetMarkers",
    "STTMarkers",
    "SceneMarkers",
    "SilenceMarkers",
    "as_font",
    "as_media",
    "markers",
    "scene_analyses",
    "source",
    "stems",
    "tts",
]
