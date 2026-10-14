from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import pytest
from pydantic import BaseModel, JsonValue, ValidationError

from eks_harness.ir.refs import FilterStep, FindStep, LenStep, PathStep, SliceStep
from eks_harness.ir.types import (
    Artifact,
    Color,
    MediaInfo,
    RefType,
    RefTypeError,
    compatible,
    format_ref_type,
    infer_path,
    normalize_color,
    normalize_media_uri,
    parse_ref_type,
    type_of_annotation,
)

FIND = FindStep(find={"name": "Kadıköy"})
FILTER = FilterStep.model_validate({"filter": {"active": True}})
SLICE = SliceStep.model_validate({"slice": [0, 3, None]})
LEN = LenStep()

# parse / format


@pytest.mark.parametrize(
    "text",
    [
        "none",
        "json",
        "str",
        "int",
        "float",
        "bool",
        "artifact",
        "media",
        "list[str]",
        "list[list[artifact]]",
        "resource:tenant",
        "resource:lease.web",
        "list[resource:tenant]",
    ],
)
def test_parse_format_round_trip(text: str) -> None:
    parsed = parse_ref_type(text)
    assert format_ref_type(parsed) == text
    assert str(parsed) == text


def test_parse_ref_type_structure() -> None:
    assert parse_ref_type("list[list[artifact]]") == RefType("list", item=RefType("list", item=RefType("artifact")))
    assert parse_ref_type("resource:tenant") == RefType("resource", kind="tenant")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "string",
        "list[]",
        "list[str",
        "list(str)",
        "resource:",
        "resource:Tenant",
        "List[str]",
        " str",
        "dict",
        "resource:tenant\n",
        "list[resource:tenant\n]",
    ],
)
def test_parse_ref_type_errors(text: str) -> None:
    with pytest.raises(RefTypeError):
        parse_ref_type(text)


def test_format_ref_type_errors() -> None:
    with pytest.raises(RefTypeError):
        format_ref_type(RefType("list"))
    with pytest.raises(RefTypeError):
        format_ref_type(RefType("resource"))
    with pytest.raises(RefTypeError):
        format_ref_type(RefType("thing"))
    with pytest.raises(RefTypeError):
        format_ref_type(RefType("resource", kind="tenant\n"))


# infer_path


@pytest.mark.parametrize(
    ("out", "path", "expected"),
    [
        ("json", [], "json"),
        ("json", ["items"], "json"),
        ("json", [0], "json"),
        ("json", ["items", FIND, "id"], "json"),
        ("json", [SLICE], "json"),
        ("json", [FILTER], "json"),
        ("json", [LEN], "int"),
        ("json", ["items", LEN], "int"),
        ("list[str]", [0], "str"),
        ("list[str]", [-1], "str"),
        ("list[artifact]", [SLICE], "list[artifact]"),
        ("list[artifact]", [FILTER], "list[artifact]"),
        ("list[json]", [FIND], "json"),
        ("list[artifact]", [FIND, "url"], "str"),
        ("list[list[int]]", [0, 1], "int"),
        ("list[str]", [LEN], "int"),
        ("str", [LEN], "int"),
        ("artifact", ["id"], "str"),
        ("artifact", ["url"], "str"),
        ("artifact", ["size"], "int"),
        ("media", ["uri"], "str"),
        ("media", ["kind"], "str"),
        ("media", ["duration"], "str"),
        ("media", ["width"], "int"),
        ("media", ["height"], "int"),
        ("media", ["fps"], "json"),
        ("media", ["artifact"], "str"),
        ("media", ["duration", LEN], "int"),
        ("resource:tenant", ["id"], "json"),
        ("resource:tenant", [0, "x"], "json"),
        ("resource:tenant", [LEN], "int"),
    ],
)
def test_infer_path(out: str, path: Sequence[PathStep], expected: str) -> None:
    assert infer_path(out, path) == expected


@pytest.mark.parametrize(
    ("out", "path"),
    [
        ("none", ["x"]),
        ("int", ["foo"]),
        ("int", [LEN]),
        ("float", [0]),
        ("bool", [LEN]),
        ("str", [0]),
        ("str", ["x"]),
        ("list[str]", ["name"]),
        ("artifact", ["missing"]),
        ("artifact", [0]),
        ("artifact", [LEN]),
        ("media", ["path"]),
        ("artifact", ["size", "x"]),
        ("nonsense", []),
    ],
)
def test_infer_path_errors(out: str, path: Sequence[PathStep]) -> None:
    with pytest.raises(RefTypeError):
        infer_path(out, path)


def test_infer_path_error_names_type_and_step() -> None:
    with pytest.raises(RefTypeError, match=r"key 'foo' on a int value"):
        infer_path("int", ["foo"])


# compatible


@pytest.mark.parametrize(
    ("produced", "expected", "result"),
    [
        ("str", "str", True),
        ("int", "float", True),
        ("float", "int", False),
        ("int", "str", False),
        ("str", "int", False),
        ("json", "str", True),
        ("str", "json", True),
        ("json", "media", True),
        ("resource:tenant", "json", True),
        ("resource:tenant", "resource:tenant", True),
        ("resource:tenant", "resource:branch", False),
        ("resource:tenant", "str", False),
        ("artifact", "media", False),
        ("media", "artifact", False),
        ("bool", "int", False),
        ("list[int]", "list[float]", True),
        ("list[json]", "list[str]", True),
        ("list[str]", "str", False),
        ("none", "str", False),
    ],
)
def test_compatible(produced: str, expected: str, result: bool) -> None:
    assert compatible(produced, expected) is result


def test_compatible_rejects_invalid_types() -> None:
    with pytest.raises(RefTypeError):
        compatible("text", "str")


# type_of_annotation


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (str, "str"),
        (int, "int"),
        (float, "float"),
        (bool, "bool"),
        (None, "none"),
        (type(None), "none"),
        (JsonValue, "json"),
        (Any, "json"),
        (Artifact, "artifact"),
        (MediaInfo, "media"),
        (list[Artifact], "list[artifact]"),
        (list[list[str]], "list[list[str]]"),
        (tuple[int, ...], "list[int]"),
        (dict[str, int], "json"),
    ],
)
def test_type_of_annotation(annotation: Any, expected: str) -> None:
    assert type_of_annotation(annotation) == expected


@pytest.mark.parametrize("annotation", [bytes, set[int], object])
def test_type_of_annotation_errors(annotation: Any) -> None:
    with pytest.raises(RefTypeError):
        type_of_annotation(annotation)


# Color


def test_normalize_color() -> None:
    assert normalize_color("#AABBCC") == "#aabbcc"
    assert normalize_color("#aabbcc") == "#aabbcc"
    assert normalize_color("#AABBCC80") == "#aabbcc80"


@pytest.mark.parametrize(
    "value", ["#abc", "aabbcc", "#aabbc", "#gggggg", "#aabbccd", "", "#aabbccdd00", "#aabbcc\n", "#AABBCC80\n"]
)
def test_normalize_color_errors(value: str) -> None:
    with pytest.raises(ValueError, match="invalid color"):
        normalize_color(value)


class _Styled(BaseModel):
    color: Color
    kind: Literal["box"] = "box"


def test_color_field() -> None:
    assert _Styled(color="#FFAA00").color == "#ffaa00"
    with pytest.raises(ValidationError):
        _Styled(color="#fa0")
    with pytest.raises(ValidationError):
        _Styled(color="#AABBCC\n")
    schema = _Styled.model_json_schema()
    assert schema["properties"]["color"]["format"] == "eks-color"


# media URIs


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("artifact:01HABC", "artifact:01HABC"),
        ("file:music/song.ogg", "file:music/song.ogg"),
        ("https://cdn.example.com/a.mp4", "https://cdn.example.com/a.mp4"),
        ("http://localhost:8000/a.mp4", "http://localhost:8000/a.mp4"),
        ("music/song.ogg", "file:music/song.ogg"),
        ("a\\b.ogg", "file:a/b.ogg"),
        ("/abs/clip.mov", "file:/abs/clip.mov"),
        ("../shared/logo.png", "file:../shared/logo.png"),
    ],
)
def test_normalize_media_uri(text: str, expected: str) -> None:
    assert normalize_media_uri(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "artifact:", "file:"])
def test_normalize_media_uri_errors(text: str) -> None:
    with pytest.raises(ValueError, match="media reference"):
        normalize_media_uri(text)


def test_artifact_and_media_models() -> None:
    artifact = Artifact(id="a1", kind="screenshot", size=12)
    assert artifact.model_dump(exclude_none=True) == {"id": "a1", "kind": "screenshot", "size": 12}
    media = MediaInfo(uri="file:a.ogg", kind="audio", width=None)
    assert media.kind == "audio"
    with pytest.raises(ValidationError):
        MediaInfo(uri="file:a.ogg", kind="text")  # pyright: ignore[reportArgumentType]
