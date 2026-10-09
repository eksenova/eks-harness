from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eks_harness.score import model
from eks_harness.score.timeref import parse_time_ref

MEDIA_SUFFIXES = (".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mkv")


@dataclass(frozen=True)
class At:
    text: str

    def __post_init__(self) -> None:
        parse_time_ref(self.text)

    def __add__(self, seconds: float | str) -> At:
        return At(self.text + _offset(seconds, "+"))

    def __sub__(self, seconds: float | str) -> At:
        return At(self.text + _offset(seconds, "-"))

    def __str__(self) -> str:
        return self.text


def _offset(value: float | str, sign: str) -> str:
    if isinstance(value, str):
        text = value.strip()
        return text if text[:1] in "+-" else sign + text
    return f"{sign}{abs(float(value)):g}s" if value >= 0 else f"{'-' if sign == '+' else '+'}{abs(float(value)):g}s"


@dataclass(frozen=True)
class EventRef:
    source: str
    name: str
    where: tuple[tuple[str, Any], ...] = ()
    offset: float = 0.0
    once: bool = False

    def matching(self, **where: Any) -> EventRef:
        return EventRef(self.source, self.name, tuple(sorted({**dict(self.where), **where}.items())), self.offset,
                        self.once)

    def first(self) -> EventRef:
        return EventRef(self.source, self.name, self.where, self.offset, True)

    def __add__(self, seconds: float) -> EventRef:
        return EventRef(self.source, self.name, self.where, self.offset + float(seconds), self.once)


class Grid:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def __getitem__(self, index: int | float | slice) -> At | list[At]:
        if isinstance(index, slice):
            if index.stop is None:
                raise ValueError("beat slices need an end, for example beats()[0:32:4]")
            start = index.start or 0
            step = index.step or 1
            return [At(f"{self.kind}:{i}") for i in range(start, index.stop, step)]
        return At(f"{self.kind}:{index:g}" if isinstance(index, float) else f"{self.kind}:{index}")

    def every(self, step: int = 1, start: int = 0, stop: int = 1024) -> list[At]:
        return self[start:stop:step]

    def event(self) -> EventRef:
        return EventRef("score", self.kind)


def beats(kind: str = "beat") -> Grid:
    if kind not in ("beat", "downbeat", "bar"):
        raise ValueError("beats() takes 'beat', 'downbeat' or 'bar'")
    return Grid("downbeat" if kind == "bar" else kind)


def cue(name: str) -> At:
    return At(f"cue:{name}")


def at(value: float | str) -> At:
    return At(value if isinstance(value, str) else f"{float(value):g}s")


def frame(number: int) -> At:
    return At(f"f:{int(number)}")


class TrackHandle:
    def __init__(self, score: Score, track: Any) -> None:
        self._score = score
        self.track = track

    @property
    def id(self) -> str:
        return self.track.id

    def act(self, verb: str, **args: Any) -> model.Action:
        return model.Action(target=self.id, verb=verb, args={k: v for k, v in args.items() if v is not None})

    def event(self, name: str, **where: Any) -> EventRef:
        return EventRef(self.id, name, tuple(sorted(where.items())))

    def emit(self, name: str, **data: Any) -> model.Action:
        return self.act("emit", name=name, data=data or None)

    def set(self, prop: str, value: Any, *, ease: float | None = None) -> model.Action:
        return self.act("set", prop=prop, value=value, ease=ease)

    def span(self, start: At | str | float | None = None, end: At | str | float | None = None) -> TrackHandle:
        if start is not None:
            self.track.start = str(start if not isinstance(start, int | float) else at(start))
        if end is not None:
            self.track.end = str(end if not isinstance(end, int | float) else at(end))
        return self


class DeviceHandle(TrackHandle):
    def press(self, target: str, **options: Any) -> model.Action:
        return self.act("press", target=target, **options)

    def fill(self, target: str, text: str, **options: Any) -> model.Action:
        return self.act("fill", target=target, text=text, **options)

    def scroll(self, target: str | None = None, *, dy: float = 0, dx: float = 0) -> model.Action:
        return self.act("scroll", target=target, dx=dx, dy=dy)

    def swipe(self, direction: str, **options: Any) -> model.Action:
        return self.act("swipe", direction=direction, **options)

    def navigate(self, route: str, **params: Any) -> model.Action:
        return self.act("navigate", route=route, params=params or None)

    def open_url(self, url: str) -> model.Action:
        return self.act("open", url=url)

    def evaluate(self, script: str) -> model.Action:
        return self.act("evaluate", script=script)

    def dispatch(self, action: dict[str, Any]) -> model.Action:
        return self.act("dispatch", action=action)

    def patch(self, target: str, *, props: dict[str, Any] | None = None, style: dict[str, Any] | None = None,
              text: str | None = None) -> model.Action:
        return self.act("patch", target=target, props=props, style=style, text=text)

    def overlay(self, kind: str, **options: Any) -> model.Action:
        return self.act("overlay", kind=kind, **options)

    def arm(self, fake: str, **payload: Any) -> model.Action:
        return self.act("arm", fake=fake, payload=payload or None)

    def mark(self, name: str) -> model.Action:
        return self.act("mark", name=name)


class SceneHandle(TrackHandle):
    def key(self, prop: str, value: Any) -> model.Action:
        return self.act("key", prop=prop, value=value)


class EditHandle(TrackHandle):
    def layer(self, source: TrackHandle | str, *, start: At | str | None = None, end: At | str | None = None,
              opacity: float = 1.0, blend: str = "normal", effects: Sequence[dict[str, Any]] = (),
              transition: dict[str, Any] | None = None) -> EditHandle:
        if isinstance(source, str):
            source = self._score._auto_track(source)
        ref = source.id if isinstance(source, TrackHandle) else source
        self.track.layers.append(model.EditLayer(source=ref, start=str(start) if start else None,
                                                 end=str(end) if end else None, opacity=opacity, blend=blend,
                                                 effects=list(effects), transition=transition))
        return self

    def marker(self, name: str) -> model.Action:
        return self.act("marker", name=name)


class Score:
    def __init__(self, *, fps: float = 30.0, song: str | None = None, window: tuple[float, float] | None = None,
                 duration: float | None = None, bpm: float | None = None, name: str = "score",
                 size: tuple[int, int] = (1080, 1920), markers: dict[str, float] | None = None) -> None:
        tempo = model.TempoMap(bpm=bpm)
        self.ir = model.Score(name=name, clock=model.Clock(fps=fps, duration=duration, window=window, tempo=tempo,
                                                           markers=dict(markers or {})))
        self.size = size
        self._counts: dict[str, int] = {}
        self._handles: dict[str, TrackHandle] = {}
        self.music: TrackHandle | None = None
        if song:
            self.music = self.audio(song, window=window, id="music")
        self._edit: EditHandle | None = None

    def _id(self, kind: str, explicit: str | None) -> str:
        if explicit:
            return explicit
        self._counts[kind] = self._counts.get(kind, 0) + 1
        return kind if self._counts[kind] == 1 else f"{kind}{self._counts[kind]}"

    def _add(self, track: Any, handle_cls: type[TrackHandle]) -> Any:
        if any(t.id == track.id for t in self.ir.tracks):
            raise ValueError(f"track id '{track.id}' is taken")
        self.ir.tracks.append(track)
        handle = handle_cls(self, track)
        self._handles[track.id] = handle
        return handle

    def _auto_track(self, source: str) -> TrackHandle | str:
        suffix = Path(source).suffix.lower()
        if suffix in (".html", ".htm"):
            return self.web(source)
        if suffix in (".py", ".blend"):
            return self.blender(source)
        if suffix in MEDIA_SUFFIXES:
            return f"file:{source}"
        raise ValueError(f"cannot tell what kind of layer '{source}' is")

    def audio(self, path: str, *, window: tuple[float, float] | None = None, gain_db: float = 0.0,
              id: str | None = None) -> TrackHandle:
        return self._add(model.AudioTrack(id=self._id("audio", id), path=path, window=window, gain_db=gain_db),
                         TrackHandle)

    def device(self, platform: str, *, flow: str | None = None, app: str | None = None, persona: str | None = None,
               target: str | None = None, id: str | None = None, lead_in: float = 1.0, warp: bool = True,
               **options: Any) -> DeviceHandle:
        track = model.DeviceTrack(id=self._id(platform, id), platform=platform, flow=flow, app=app, persona=persona,
                                  target=target, lead_in=lead_in, warp=warp, options=options)
        return self._add(track, DeviceHandle)

    def web(self, entry: str, *, size: tuple[int, int] | None = None, props: dict[str, Any] | None = None,
            textures: dict[str, TrackHandle | str] | None = None, id: str | None = None,
            transparent: bool = True) -> SceneHandle:
        track = model.WebTrack(id=self._id("web", id), entry=entry, size=size or self.size, props=dict(props or {}),
                               textures=_refs(textures), transparent=transparent)
        return self._add(track, SceneHandle)

    def blender(self, source: str, *, textures: dict[str, TrackHandle | str] | None = None,
                engine: str = "BLENDER_EEVEE_NEXT", samples: int | None = None,
                size: tuple[int, int] | None = None, props: dict[str, Any] | None = None, id: str | None = None,
                transparent: bool = True) -> SceneHandle:
        is_blend = source.lower().endswith(".blend")
        track = model.BlenderTrack(id=self._id("blender", id), script=None if is_blend else source,
                                   blend=source if is_blend else None, textures=_refs(textures), engine=engine,
                                   samples=samples, size=size or self.size, props=dict(props or {}),
                                   transparent=transparent)
        return self._add(track, SceneHandle)

    def track(self, kind: str, *, id: str | None = None, **fields: Any) -> TrackHandle:
        return self._add(model.PluginTrack(id=self._id(kind, id), kind=kind, **fields), TrackHandle)

    @property
    def edit(self) -> EditHandle:
        if self._edit is None:
            self._edit = self._add(model.EditTrack(id="edit", size=self.size,
                                                   audio=[self.music.id] if self.music else []), EditHandle)
        return self._edit

    def cue(self, name: str, seconds: float) -> At:
        self.ir.clock.markers[name] = float(seconds)
        return cue(name)

    def on(self, when: At | EventRef | str | float | Iterable[At], *actions: model.Action,
           id: str | None = None) -> Score:
        if not actions:
            raise ValueError("on() needs at least one action")
        if isinstance(when, list | tuple):
            for index, item in enumerate(when):
                self.on(item, *actions, id=f"{id}-{index + 1}" if id else None)
            return self
        if isinstance(when, EventRef):
            trigger = model.Trigger(event=when.name, source=when.source, where=dict(when.where), offset=when.offset,
                                    once=when.once)
        else:
            trigger = model.Trigger(at=str(when if not isinstance(when, int | float) else at(when)))
        self.ir.rules.append(model.Rule(id=id, when=trigger, do=list(actions)))
        return self

    def bind(self, name: str, source: TrackHandle | str) -> Score:
        self.ir.bindings[name] = source.id if isinstance(source, TrackHandle) else source
        return self

    def build(self) -> model.Score:
        return model.Score.model_validate(self.ir.model_dump())

    def save(self, path: str | Path) -> Path:
        return self.build().save(Path(path))


def _refs(textures: dict[str, TrackHandle | str] | None) -> dict[str, str]:
    return {name: (src.id if isinstance(src, TrackHandle) else src) for name, src in (textures or {}).items()}
