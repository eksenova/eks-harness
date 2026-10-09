from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eks_harness.score.timeref import parse_time_ref

SCORE_VERSION = 1
BUILTIN_TRACK_KINDS = ("edit", "blender", "web", "device", "audio")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TempoMap(_Model):
    bpm: float | None = None
    offset: float = 0.0
    beats_per_bar: int = 4
    beats: list[float] | None = None
    downbeats: list[float] | None = None
    source: str | None = None

    @model_validator(mode="after")
    def _check(self) -> TempoMap:
        if self.bpm is not None and self.bpm <= 0:
            raise ValueError("bpm must be positive")
        if self.beats is not None and any(b2 < b1 for b1, b2 in zip(self.beats, self.beats[1:], strict=False)):
            raise ValueError("beats must be sorted")
        return self


class Clock(_Model):
    fps: float = 30.0
    duration: float | None = None
    window: tuple[float, float] | None = None
    tempo: TempoMap = Field(default_factory=TempoMap)
    markers: dict[str, float] = Field(default_factory=dict)

    @field_validator("fps")
    @classmethod
    def _fps(cls, value: float) -> float:
        if value <= 0 or value > 240:
            raise ValueError("fps must be in (0, 240]")
        return value


class Action(_Model):
    target: str
    verb: str
    args: dict[str, Any] = Field(default_factory=dict)


class Trigger(_Model):
    at: str | None = None
    event: str | None = None
    source: str | None = None
    where: dict[str, Any] = Field(default_factory=dict)
    offset: float = 0.0
    once: bool = False

    @model_validator(mode="after")
    def _one(self) -> Trigger:
        if (self.at is None) == (self.event is None):
            raise ValueError("a trigger has exactly one of 'at' (a time reference) or 'event' (an event name)")
        if self.at is not None:
            parse_time_ref(self.at)
        return self


class Rule(_Model):
    id: str | None = None
    when: Trigger
    do: list[Action]


class TrackBase(_Model):
    id: str
    enabled: bool = True
    start: str = "0s"
    end: str | None = None
    label: str | None = None
    emits: list[str] = Field(default_factory=list)

    @field_validator("start", "end")
    @classmethod
    def _time(cls, value: str | None) -> str | None:
        if value is not None:
            parse_time_ref(value)
        return value


class AudioTrack(TrackBase):
    kind: Literal["audio"] = "audio"
    path: str
    window: tuple[float, float] | None = None
    gain_db: float = 0.0
    analyze: bool = True


class DeviceTrack(TrackBase):
    kind: Literal["device"] = "device"
    platform: str
    flow: str | None = None
    app: str | None = None
    persona: str | None = None
    target: str | None = None
    size: tuple[int, int] | None = None
    lead_in: float = 1.0
    warp: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class WebTrack(TrackBase):
    kind: Literal["web"] = "web"
    entry: str
    size: tuple[int, int] = (1080, 1920)
    props: dict[str, Any] = Field(default_factory=dict)
    textures: dict[str, str] = Field(default_factory=dict)
    transparent: bool = True


class BlenderTrack(TrackBase):
    kind: Literal["blender"] = "blender"
    script: str | None = None
    blend: str | None = None
    textures: dict[str, str] = Field(default_factory=dict)
    engine: str = "BLENDER_EEVEE_NEXT"
    samples: int | None = None
    size: tuple[int, int] = (1080, 1920)
    props: dict[str, Any] = Field(default_factory=dict)
    transparent: bool = True

    @model_validator(mode="after")
    def _source(self) -> BlenderTrack:
        if not (self.script or self.blend):
            raise ValueError("a blender track needs a script or a .blend file")
        return self


class EditLayer(_Model):
    source: str
    start: str | None = None
    end: str | None = None
    opacity: float = 1.0
    blend: str = "normal"
    effects: list[dict[str, Any]] = Field(default_factory=list)
    transition: dict[str, Any] | None = None


class EditTrack(TrackBase):
    kind: Literal["edit"] = "edit"
    project: str | None = None
    size: tuple[int, int] = (1080, 1920)
    layers: list[EditLayer] = Field(default_factory=list)
    audio: list[str] = Field(default_factory=list)


class PluginTrack(TrackBase):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    kind: str

    @field_validator("kind")
    @classmethod
    def _plugin_kind(cls, value: str) -> str:
        if value in BUILTIN_TRACK_KINDS:
            raise ValueError(f"'{value}' is a built-in track kind")
        return value


Track = Annotated[AudioTrack | DeviceTrack | WebTrack | BlenderTrack | EditTrack, Field(discriminator="kind")]


class Score(_Model):
    version: int = SCORE_VERSION
    name: str = "score"
    clock: Clock = Field(default_factory=Clock)
    tracks: list[Track | PluginTrack] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    bindings: dict[str, str] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tracks", mode="before")
    @classmethod
    def _tracks(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        out = []
        for item in value:
            if isinstance(item, dict) and item.get("kind") not in BUILTIN_TRACK_KINDS:
                out.append(PluginTrack.model_validate(item))
            else:
                out.append(item)
        return out

    @model_validator(mode="after")
    def _links(self) -> Score:
        ids = [t.id for t in self.tracks]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate track ids: {', '.join(sorted(duplicates))}")
        known = set(ids) | {"score"}
        for rule in self.rules:
            if rule.when.source and rule.when.source not in known:
                raise ValueError(f"rule listens to unknown track '{rule.when.source}'")
            for action in rule.do:
                if action.target not in known:
                    raise ValueError(f"rule acts on unknown track '{action.target}'")
        for track in self.tracks:
            textures = getattr(track, "textures", {}) or {}
            for name, source in textures.items():
                if source not in known:
                    raise ValueError(f"{track.id}: texture '{name}' binds unknown track '{source}'")
            if isinstance(track, EditTrack):
                for layer in track.layers:
                    if layer.source not in known and not layer.source.startswith(("file:", "artifact:")):
                        raise ValueError(f"{track.id}: layer source '{layer.source}' is not a track")
        return self

    def track(self, track_id: str) -> Any:
        for track in self.tracks:
            if track.id == track_id:
                return track
        raise KeyError(track_id)

    def of_kind(self, kind: str) -> list[Any]:
        return [t for t in self.tracks if t.kind == kind and t.enabled]

    def dumps(self) -> str:
        return json.dumps(self.model_dump(mode="json", exclude_none=True), indent=2, ensure_ascii=False) + "\n"

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dumps(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> Score:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


def json_schema() -> dict[str, Any]:
    return Score.model_json_schema()
