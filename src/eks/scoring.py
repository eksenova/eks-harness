from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from re import Pattern
from typing import Any, Literal, cast

from pydantic import BaseModel

from eks.audio import AudioMix
from eks.discovery import AppInfo, find_app
from eks.editing import FINALIZE_ORDER, EditBuilder
from eks.media import source
from eks.planning import app_defaults, inherited, merged_session
from eks.ref import JsonLike, Media, Ref, encode, node_of
from eks.scenes import embed_scene
from eks.session import Session
from eks.tracing import TraceContext, TraceError
from eks.webscene import TAKE_OP, check_event, ensure_event
from eks_harness.ir.catalog.durations import ensure_length
from eks_harness.ir.entries import ScoreOptions
from eks_harness.ir.ops.scene import EventRef
from eks_harness.ir.plan import SessionSpec
from eks_harness.ir.registry import (
    COMPONENT_PATTERN,
    ENTRY_NAME_PATTERN,
    PRESET_NAME_PATTERN,
    EntrySpec,
)
from eks_harness.ir.target import Platform
from eks_harness.ir.time import Meter, Span, Time

SONG_TRACK = "song"
SONG_STREAMS = ("beat", "downbeat")
TAKE_FLAG = "take"
SESSION_AS_OP = "session.as"


def _take_text(value: object, key: str, what: str, example: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{key} {what}= expects a name such as {example!r}, got {type(value).__name__} {value!r}")
    if not value.strip():
        raise ValueError(f"{key} {what}= must not be empty; pass a name such as {example!r}")
    return value


def _take_name(value: object, key: str, what: str, example: str, pattern: Pattern[str]) -> str:
    text = _take_text(value, key, what, example)
    if pattern.fullmatch(text) is None:
        raise ValueError(f"{key} {what}={text!r} must match {pattern.pattern}; pass a name such as {example!r}")
    return text


class TakeSession(Session):
    def __init__(self, ctx: TraceContext, spec: SessionSpec) -> None:
        super().__init__(ctx, spec)
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def close(self) -> None:
        self._open = False

    def record_op(
        self,
        op: str,
        args: BaseModel | Mapping[str, Any] | None = None,
        *,
        at: Time | None = None,
        label: str | None = None,
        id_key: str | None = None,
        key_suffix: str | None = None,
    ) -> Ref[Any]:
        self._require_open(op)
        return super().record_op(op, args, at=at, label=label, id_key=id_key, key_suffix=key_suffix)

    @contextmanager
    def record_container(
        self,
        op: str,
        args: BaseModel | Mapping[str, Any] | None = None,
        *,
        branch: str = "body",
        at: Time | None = None,
        label: str | None = None,
        flags: Sequence[str] = (),
    ) -> Iterator[Ref[Any]]:
        self._require_open(op)
        with super().record_container(op, args, branch=branch, at=at, label=label, flags=flags) as ref:
            yield ref

    def _require_open(self, op: str) -> None:
        if not self._open:
            key = self.spec.key
            raise TraceError(
                f"{op}: {key}'s session is used outside `with {key}.flow()`; drive each take only inside its "
                "own flow block, with the session that block yields"
            )


class TakeHandle:
    def __init__(self, score: ScoreBuilder, ref: Media, spec: SessionSpec) -> None:
        self._score = score
        self._ref = ref
        self._spec = spec
        self._opened = False

    @property
    def ref(self) -> Media:
        return self._ref

    @property
    def spec(self) -> SessionSpec:
        return self._spec

    def __eks_encode__(self) -> Any:
        return encode(self._ref)

    def __repr__(self) -> str:
        return f"TakeHandle({self._spec.key!r}, platform={self._spec.platform!r}, app={self._spec.app!r})"

    @contextmanager
    def flow(self) -> Iterator[Session]:
        if self._opened:
            raise TraceError(
                f"the flow of {self._spec.key} is already written; a take records one flow, so put every step "
                "inside a single `with take.flow() as s:` block or create another take with sc.take(...)"
            )
        self._opened = True
        ctx = self._score.ctx
        with ctx.branch(node_of(self._ref), "body", flags=(TAKE_FLAG,)):
            session = TakeSession(ctx, self._spec)
            try:
                with session.activate():
                    if self._spec.persona is not None:
                        session.record_op(SESSION_AS_OP, {"persona": self._spec.persona})
                    yield session
            finally:
                session.close()

    def event(self, name: str, **where: JsonLike) -> EventRef:
        what = f"{self._spec.key}.event()"
        event = EventRef.model_validate({
            "source": encode(self._ref), "name": ensure_event(name, what), "where": encode(where)})
        check_event(self._score.ctx, event, what)
        return event


class ScoreBuilder:
    def __init__(self, ctx: TraceContext, entry: EntrySpec) -> None:
        self._ctx = ctx
        self._entry = entry
        options = entry.options
        if not isinstance(options, ScoreOptions):
            raise TraceError(f"{entry.kind} {entry.name!r} is not a score; open_score builds @eks.score entries only")
        self._options = options
        self._scope_key = ctx.scope().key
        self._takes: list[TakeHandle] = []
        self._edit = EditBuilder(ctx, fps=options.fps, size=options.size, seed=options.seed, duration=options.duration)
        self._song: Media | None = None
        if options.song is not None:
            self._song = source(options.song, origin=entry.file or None)
            self._edit.audio.track(self._song, name=SONG_TRACK).beats(*SONG_STREAMS)
        if options.meter is not None and options.bpm is None:
            self._apply_song_meter(options.meter)

    @property
    def ctx(self) -> TraceContext:
        return self._ctx

    @property
    def options(self) -> ScoreOptions:
        return self._options

    @property
    def song(self) -> Media | None:
        return self._song

    @property
    def edit(self) -> EditBuilder:
        return self._edit

    @property
    def audio(self) -> AudioMix:
        return self._edit.audio

    @property
    def takes(self) -> tuple[TakeHandle, ...]:
        return tuple(self._takes)

    def take(
        self, *, platform: Platform, app: str, seed: str | None = None, as_: str | None = None,
        backend: str | None = None, viewport: str | None = None, span: Span | None = None,
        lead_in: Time | None = None, warp: bool = True, size: tuple[int, int] | None = None,
        root_seed: int | None = None, now: str | None = None, timezone: str | None = None,
        locale: str | None = None, permissions: Literal["ask", "grant", "deny"] | None = None,
    ) -> TakeHandle:
        self._require_scope("sc.take()")
        key = f"take{len(self._takes)}"
        checked_lead_in = self._lead_in(lead_in, f"{key} lead_in") if lead_in is not None else None
        scenario = None if seed is None else _take_name(seed, key, "seed", "standard", ENTRY_NAME_PATTERN)
        chosen_backend = None if backend is None else _take_name(backend, key, "backend", "eksenapi", COMPONENT_PATTERN)
        if now is not None and not isinstance(now, str):
            raise TypeError(f"{key}: now= takes an ISO 8601 time with an offset such as '2026-01-15T09:00+03:00', "
                            f"got {type(now).__name__} {now!r}")
        frozen, chosen_root, chosen_backend = inherited(self._ctx.registry, scenario, now=now, root_seed=root_seed,
                                                        backend=chosen_backend)
        app_name = _take_text(app, key, "app", "mobile-app")
        info = self._app(key, app_name, platform)
        own = {"viewport": None if viewport is None else _take_name(viewport, key, "viewport", "phone",
                                                                     PRESET_NAME_PATTERN),
               "timezone": None if timezone is None else _take_text(timezone, key, "timezone", "Europe/Istanbul"),
               "locale": None if locale is None else _take_text(locale, key, "locale", "tr-TR"),
               "permissions": permissions}
        fields = merged_session(app_defaults(info.tables if info is not None else None, platform, app_name), own,
                                self._ctx.registry, key, platform)
        spec = SessionSpec.model_validate({
            "key": key,
            "app": app_name,
            "platform": platform,
            "seed": scenario,
            "persona": None if as_ is None else _take_text(as_, key, "as_", "harman-admin"),
            "backend": chosen_backend,
            "root_seed": chosen_root,
            "now": frozen,
            **fields,
        })
        resources = self._ctx.session_resources(spec)
        args: dict[str, Any] = {"session": spec, "warp": warp}
        if span is not None:
            args["span"] = span
        if checked_lead_in is not None:
            args["lead_in"] = checked_lead_in
        if size is not None:
            args["size"] = size
        with self._ctx.container(TAKE_OP, args, uses=list(resources.values())) as ref:
            handle = TakeHandle(self, cast("Media", ref), spec)
        self._takes.append(handle)
        return handle

    def scene(self, scene: Callable[..., Any] | str, /, **inputs: Any) -> Media:
        self._require_scope("sc.scene()")
        return embed_scene(self._ctx, scene, inputs)

    def gfx(self, scene: Callable[..., Any] | str, /, **inputs: Any) -> Media:
        self._require_scope("sc.gfx()")
        return embed_scene(self._ctx, scene, inputs, kind="gfx")

    def cue(self, name: str, at: Time) -> Time:
        self._require_scope("sc.cue()")
        return self._edit.cue(name, at)

    def finalize(self) -> Media:
        return self._edit.finalize()

    def _apply_song_meter(self, meter: tuple[int, int]) -> None:
        tempo = self._ctx.clock.tempo
        if self._song is None or tempo is None:
            raise TraceError(
                f"score {self._entry.name!r}: meter={meter} needs a tempo; pass bpm= for a fixed tempo or song= "
                "for one tracked from the song's beats"
            )
        numerator, denominator = meter
        self._ctx.update_clock(
            tempo=tempo.model_copy(update={"meter": Meter(numerator=numerator, denominator=denominator)}))

    def _app(self, key: str, app: str, platform: str) -> AppInfo | None:
        tree = self._ctx.tree
        if tree is None:
            return None
        info = find_app(tree, app)
        if info is not None and info.platforms and platform not in info.platforms:
            raise TraceError(f"{key}: app {app!r} runs on {list(info.platforms)}; platform {platform!r} is not one of them")
        return info

    @staticmethod
    def _lead_in(value: object, what: str) -> Time:
        return ensure_length(value, what)

    def _require_scope(self, what: str) -> None:
        if self._ctx.scope().key != self._scope_key:
            raise TraceError(
                f"{what}: a score is a static arrangement; create takes, scenes, gfx and cues in the score "
                "function itself, outside take flows and eks.when, eks.each or eks.dynamic bodies"
            )


Score = ScoreBuilder


def open_score(ctx: TraceContext, entry: EntrySpec) -> ScoreBuilder:
    builder = ScoreBuilder(ctx, entry)

    def finish(_ctx: TraceContext) -> None:
        builder.finalize()

    ctx.on_finalize(finish, order=FINALIZE_ORDER)
    return builder


__all__ = ["Score", "ScoreBuilder", "TakeHandle", "TakeSession", "open_score"]
