from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from functools import cache
from types import UnionType
from typing import Annotated, Any, Literal, TypeAliasType, Union, get_args, get_origin

import typing_extensions
from pydantic import AfterValidator, BaseModel, ConfigDict, JsonValue, WithJsonSchema

from eks_harness.ir.refs import (
    FilterStep,
    FindStep,
    ItemExpr,
    LenStep,
    PathStep,
    RefExpr,
    RefKind,
    SliceStep,
)
from eks_harness.ir.time import Fps, Time

SCALAR_TYPES = ("none", "json", "str", "int", "float", "bool", "artifact", "media")
COLOR_PATTERN = r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$"


ShippedFont = Literal["IBM Plex Sans", "IBM Plex Mono"]
ShippedFontFile = Literal["IBMPlexSans-Regular.ttf", "IBMPlexSans-SemiBold.ttf", "IBMPlexMono-Regular.ttf"]
SHIPPED_FONTS: tuple[str, ...] = get_args(ShippedFont)
DEFAULT_FONT: ShippedFont = "IBM Plex Sans"
FONT_EXTENSIONS = (".ttf", ".otf", ".ttc", ".woff", ".woff2")
Font = Annotated[RefExpr | ItemExpr | ShippedFont, RefKind("media")]


class RefTypeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RefType:
    name: str
    item: RefType | None = None
    kind: str | None = None

    def __str__(self) -> str:
        return format_ref_type(self)


RESOURCE_KIND_PATTERN = r"^[a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)*$"
RESOURCE_TYPE_PATTERN = "^resource:" + RESOURCE_KIND_PATTERN[1:]
_KIND_PATTERN = re.compile(RESOURCE_KIND_PATTERN)
_COLOR = re.compile(COLOR_PATTERN)
_URI_PREFIXES = ("artifact:", "file:", "plugin:", "pkg:", "http://", "https://")
ARTIFACT_ID = r"[A-Za-z0-9_-]+"
CONTENT_PIN = r"@sha256:[0-9a-f]{64}"
PINNED_ARTIFACT_PATTERN = rf"^artifact:{ARTIFACT_ID}{CONTENT_PIN}$"
ARTIFACT_URI = rf"artifact:{ARTIFACT_ID}(?:{CONTENT_PIN})?"
_GRAMMAR_HINT = "write none, json, str, int, float, bool, artifact, media, list[T] or resource:KIND"


def parse_ref_type(text: str) -> RefType:
    if not isinstance(text, str) or not text:
        raise RefTypeError(f"invalid ref type {text!r}; {_GRAMMAR_HINT}")
    if text in SCALAR_TYPES:
        return RefType(text)
    if text.startswith("list[") and text.endswith("]"):
        return RefType("list", item=parse_ref_type(text[5:-1]))
    if text.startswith("resource:"):
        kind = text[len("resource:"):]
        if _KIND_PATTERN.fullmatch(kind):
            return RefType("resource", kind=kind)
        raise RefTypeError(f"invalid resource kind {kind!r} in ref type {text!r}; use a name such as resource:tenant")
    raise RefTypeError(f"invalid ref type {text!r}; {_GRAMMAR_HINT}")


def format_ref_type(value: RefType) -> str:
    if value.name == "list":
        if value.item is None:
            raise RefTypeError("a list ref type needs an item type")
        return f"list[{format_ref_type(value.item)}]"
    if value.name == "resource":
        if not value.kind or not _KIND_PATTERN.fullmatch(value.kind):
            raise RefTypeError(f"a resource ref type needs a kind, got {value.kind!r}")
        return f"resource:{value.kind}"
    if value.name in SCALAR_TYPES:
        return value.name
    raise RefTypeError(f"invalid ref type name {value.name!r}; {_GRAMMAR_HINT}")


def _step_text(step: PathStep) -> str:
    if isinstance(step, str):
        return f"key {step!r}"
    if isinstance(step, int):
        return f"index {step}"
    if isinstance(step, SliceStep):
        return "a slice"
    if isinstance(step, FindStep):
        return "find"
    if isinstance(step, FilterStep):
        return "filter"
    return "len"


@cache
def _model_field_types(model: type[BaseModel]) -> dict[str, str]:
    return {name: _field_type(field.annotation) for name, field in model.model_fields.items()}


def _field_type(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(members) == 1:
            return _field_type(members[0])
        return "json"
    if annotation is Time:
        return "str"
    if annotation is Fraction or (origin is Annotated and get_args(annotation)[0] is Fraction):
        return "json"
    if origin is Literal:
        return _literal_type(get_args(annotation))
    if isinstance(annotation, TypeAliasType | typing_extensions.TypeAliasType) and annotation is not JsonValue:
        return _field_type(annotation.__value__)
    return type_of_annotation(annotation)


def _literal_type(values: tuple[Any, ...]) -> str:
    if all(isinstance(value, bool) for value in values):
        return "bool"
    if all(isinstance(value, str) for value in values):
        return "str"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return "int"
    return "json"


def infer_path(out: str, path: Sequence[PathStep]) -> str:
    current = parse_ref_type(out)
    for step in path:
        current = _infer_step(current, step, out, path)
    return format_ref_type(current)


def _infer_step(current: RefType, step: PathStep, out: str, path: Sequence[PathStep]) -> RefType:
    where = f"{_step_text(step)} on a {format_ref_type(current)} value (ref type {out}, path {list(path)!r})"
    if current.name in ("json", "resource"):
        return RefType("int") if isinstance(step, LenStep) else RefType("json")
    if current.name == "list" and current.item is not None:
        if isinstance(step, LenStep):
            return RefType("int")
        if isinstance(step, SliceStep | FilterStep):
            return current
        if isinstance(step, FindStep) or (isinstance(step, int) and not isinstance(step, bool)):
            return current.item
        raise RefTypeError(f"{where}: lists take an index, a slice, find, filter or len, not a key")
    if current.name == "str":
        if isinstance(step, LenStep):
            return RefType("int")
        raise RefTypeError(f"{where}: a string only supports len()")
    if current.name in ("artifact", "media"):
        fields = _model_field_types(Artifact if current.name == "artifact" else MediaInfo)
        if isinstance(step, str) and step in fields:
            return parse_ref_type(fields[step])
        raise RefTypeError(f"{where}: use one of the fields {', '.join(fields)}")
    raise RefTypeError(f"{where}: a {format_ref_type(current)} value has no fields, items or length")


def compatible(produced: str, expected: str) -> bool:
    left = parse_ref_type(produced)
    right = parse_ref_type(expected)
    return _compatible(left, right)


def _compatible(produced: RefType, expected: RefType) -> bool:
    if produced == expected or "json" in (produced.name, expected.name):
        return True
    if produced.name == "int" and expected.name == "float":
        return True
    if produced.name == "list" and expected.name == "list" and produced.item and expected.item:
        return _compatible(produced.item, expected.item)
    return False


def type_of_annotation(annotation: Any) -> str:
    if annotation is None or annotation is type(None):
        return "none"
    if annotation is Any or annotation is JsonValue:
        return "json"
    simple = _SIMPLE_TYPES.get(annotation)
    if simple is not None:
        return simple
    if isinstance(annotation, TypeAliasType | typing_extensions.TypeAliasType):
        return type_of_annotation(annotation.__value__)
    origin = get_origin(annotation)
    if origin is Annotated:
        return type_of_annotation(get_args(annotation)[0])
    if origin in (list, tuple) or origin is Sequence:
        args = [arg for arg in get_args(annotation) if arg is not Ellipsis]
        if origin is tuple and len(set(args)) > 1:
            return "list[json]"
        return f"list[{type_of_annotation(args[0]) if args else 'json'}]"
    if origin is dict:
        return "json"
    raise RefTypeError(
        f"no ref type for {annotation!r}; use str, int, float, bool, None, Json, Artifact, MediaInfo or list[...]"
    )


def normalize_color(value: str) -> str:
    if not isinstance(value, str) or not _COLOR.fullmatch(value):
        raise ValueError(f"invalid color {value!r}; write '#rrggbb' or '#rrggbbaa' with hex digits")
    return value.lower()


def normalize_media_uri(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"invalid media reference {text!r}; give a path, file:PATH, artifact:ID or an http(s) URL")
    if text.startswith(_URI_PREFIXES):
        if text in _URI_PREFIXES:
            raise ValueError(f"media reference {text!r} has nothing after the prefix")
        return text
    return "file:" + text.replace("\\", "/")


Color = Annotated[
    str,
    AfterValidator(normalize_color),
    WithJsonSchema({"type": "string", "pattern": COLOR_PATTERN, "format": "eks-color"}),
]


class Artifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: str
    name: str | None = None
    url: str | None = None
    raw_url: str | None = None
    session_url: str | None = None
    mime: str | None = None
    size: int | None = None
    sha256: str | None = None


MediaKind = Literal["video", "audio", "image", "file", "bundle"]


class MediaInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    uri: str
    kind: MediaKind
    duration: Time | None = None
    width: int | None = None
    height: int | None = None
    fps: Fps | None = None
    artifact: str | None = None


_SIMPLE_TYPES: dict[Any, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    Artifact: "artifact",
    MediaInfo: "media",
}


__all__ = [
    "ARTIFACT_URI",
    "COLOR_PATTERN",
    "DEFAULT_FONT",
    "FONT_EXTENSIONS",
    "PINNED_ARTIFACT_PATTERN",
    "RESOURCE_KIND_PATTERN",
    "RESOURCE_TYPE_PATTERN",
    "SCALAR_TYPES",
    "SHIPPED_FONTS",
    "Artifact",
    "Color",
    "Font",
    "MediaInfo",
    "MediaKind",
    "RefType",
    "RefTypeError",
    "ShippedFont",
    "ShippedFontFile",
    "compatible",
    "format_ref_type",
    "infer_path",
    "normalize_color",
    "normalize_media_uri",
    "parse_ref_type",
    "type_of_annotation",
]
