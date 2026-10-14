from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from eks_harness.ir.catalog.markers import MarkerSource
from eks_harness.ir.ops.audio import Stem
from eks_harness.ir.refs import RMedia, RStr
from eks_harness.ir.registry import COMPONENT_PATTERN, Args, op
from eks_harness.ir.types import ARTIFACT_URI, MediaKind

StemModel = Literal["htdemucs", "htdemucs_ft", "mdx_extra"]

MEDIA_URI_PATTERN = rf"^(?:{ARTIFACT_URI}|file:.+|plugin:[A-Za-z0-9_.-]+/.+|pkg:[A-Za-z_][A-Za-z0-9_]*/.+|https?://.+)$"


@op("media", name="source", out="media", pure=True, location="hub",
    description="An input media file or artifact, probed and fingerprinted",
    examples=[{"uri": "artifact:VID123"}, {"uri": "file:assets/phone.blend", "kind": "file"}])
class Source(Args):
    uri: str = Field(pattern=MEDIA_URI_PATTERN, description="Media URI: artifact:ID (the artifact's current version), artifact:ID@sha256:HEX (that exact content), file:path relative to the plan base, plugin:ID/path inside a plugin, pkg:MODULE/path inside an importable Python package, or an https URL")
    kind: MediaKind | None = Field(default=None, description="Media kind: video, audio or image (probed when unset), file for any other single file such as a .blend scene, a font or a LUT, or bundle for the directory around a web scene entry; every kind is fingerprinted by content")
    renderer: str | None = Field(default=None, pattern=COMPONENT_PATTERN.pattern, description="Media kind of the registered renderer (@eks.renderer) that turns this source into frames, such as svg; set from the file extension when a loaded plugin registers a renderer for it")


@op("media", name="markers", out="json", pure=True, location="hub",
    description="Extract marker streams (beats, words, scenes, onsets, momentum) from media",
    examples=[
        {
            "media": {"$ref": "n0000000001", "path": []},
            "sources": [{"kind": "beat_tracker", "name": "song", "streams": ["beat", "downbeat"]}],
        },
    ])
class Markers(Args):
    media: RMedia = Field(description="Media to analyse")
    sources: tuple[MarkerSource, ...] = Field(min_length=1, description="Marker analyses to run; each names its streams")


@op("media", name="tts", out="media", pure=True, location="node", device_dependent=True,
    description="Synthesize speech from text into an audio file, usable as a clip, an audio track or a word source",
    examples=[{"text": "Kontör işlemleri artık tek ekranda", "voice": "tr_female_1", "backend": "kokoro"}])
class Speech(Args):
    text: RStr = Field(description="Text to speak")
    voice: str = Field(default="default", min_length=1, description="Voice name of the speech backend")
    backend: str = Field(default="kokoro", pattern=COMPONENT_PATTERN.pattern, description="Speech synthesis backend")
    language: str | None = Field(default=None, description="Language tag such as tr-TR; the voice's own when unset")
    speed: float = Field(default=1.0, gt=0.0, le=4.0, description="Speaking rate as a multiple of the voice's normal rate")
    seed: int | None = Field(default=None, description="Sampling seed of the backend; derived from the node seed when unset")


@op("media", name="stems", out="media", pure=True, location="node", device_dependent=True,
    description="Separate the stems of an audio source (vocals, drums, bass, other) and mix the chosen ones into one "
                "audio file, usable as a clip or an audio track",
    examples=[{"media": {"$ref": "n0000000001", "path": []}, "stems": ["vocals"]}])
class Stems(Args):
    media: RMedia = Field(description="Audio or video to separate")
    stems: tuple[Stem, ...] = Field(min_length=1, description="Stems mixed into the output")
    model: StemModel = Field(default="htdemucs", description="Separation model")

    @field_validator("stems")
    @classmethod
    def _unique(cls, value: tuple[Stem, ...]) -> tuple[Stem, ...]:
        if len(set(value)) != len(value):
            raise ValueError(f"stems {list(value)} repeat")
        return value


__all__ = ["MEDIA_URI_PATTERN", "Markers", "Source", "Speech", "StemModel", "Stems"]
