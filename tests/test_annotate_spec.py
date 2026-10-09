from __future__ import annotations

import pytest

from eks_harness.annotate.spec import (
    SpecError,
    builtin_style,
    loads_spec,
    normalize_asset_name,
    parse_spec,
    parse_style,
)


def test_json_spec_with_all_anchor_kinds() -> None:
    spec = loads_spec(
        '{"version": 1, "style": "review", "items": ['
        '{"type": "step", "anchor": {"kind": "selector", "selector": ".save"}}'
        ']}')
    assert spec.version == 1
    assert spec.style == "review"
    assert spec.items[0].anchor is not None and spec.items[0].anchor.kind == "selector"
    assert spec.items[0].payload["index"] == 1


def test_yaml_spec_parses_identically() -> None:
    text = "version: 1\nitems:\n  - type: label\n    anchor: {kind: text, text: Save}\n    text: Saved\n"
    spec = loads_spec(text)
    assert spec.items[0].type == "label"
    assert spec.items[0].anchor is not None and spec.items[0].anchor.text == "Save"
    assert spec.items[0].payload["text"] == "Saved"


def test_unknown_fields_rejected() -> None:
    with pytest.raises(SpecError):
        parse_spec({"version": 1, "style": "kb", "items": [], "extra": 1})
    with pytest.raises(SpecError):
        parse_spec({"version": 1, "items": [{"type": "step", "anchor": {"kind": "text", "text": "x"},
                                             "bogus": True}]})
    with pytest.raises(SpecError):
        parse_spec({"version": 1, "items": [{"type": "step",
                                             "anchor": {"kind": "text", "text": "x", "nope": 1}}]})


def test_version_and_bounds() -> None:
    with pytest.raises(SpecError):
        parse_spec({"version": 2, "items": [{"type": "step", "anchor": {"kind": "text", "text": "x"}}]})
    with pytest.raises(SpecError):
        parse_spec({"version": 1, "items": []})
    with pytest.raises(SpecError):
        parse_spec({"version": 1,
                    "items": [{"type": "step", "anchor": {"kind": "text", "text": "x"}}] * 101})


def test_coords_anchor_values() -> None:
    spec = parse_spec({"version": 1, "items": [
        {"type": "highlight", "anchor": {"kind": "coords", "x": 1, "y": 2, "width": 3, "height": 4}}]})
    anchor = spec.items[0].anchor
    assert anchor is not None and anchor.is_coords()
    with pytest.raises(SpecError):
        parse_spec({"version": 1, "items": [
            {"type": "highlight", "anchor": {"kind": "coords", "x": 1, "y": 2, "width": 0, "height": 4}}]})


def test_selector_rejected_on_mobile() -> None:
    with pytest.raises(SpecError):
        parse_spec({"version": 1, "items": [
            {"type": "step", "anchor": {"kind": "selector", "selector": ".x"}}]}, viewport="mobile")
    spec = parse_spec({"version": 1, "items": [
        {"type": "step", "anchor": {"kind": "testId", "testId": "save"}}]}, viewport="mobile")
    assert spec.items[0].anchor is not None and spec.items[0].anchor.kind == "testId"


def test_caption_and_title_media_rules() -> None:
    with pytest.raises(SpecError):
        parse_spec({"version": 1, "items": [
            {"type": "caption", "text": "hi", "start": 1.0, "end": 2.0}]})
    ok = parse_spec({"version": 1, "items": [
        {"type": "caption", "text": "hi", "start": 1.0, "end": 2.0}]}, for_video=True)
    assert ok.items[0].payload["start"] == 1.0
    with pytest.raises(SpecError):
        parse_spec({"version": 1, "items": [{"type": "title", "text": "hi"}]})
    video = parse_spec({"version": 1, "items": [{"type": "title", "text": "hi"}]}, for_video=True)
    assert video.items[0].payload["text"] == "hi"


def test_arrow_needs_both_anchors() -> None:
    with pytest.raises(SpecError):
        parse_spec({"version": 1, "items": [{"type": "arrow", "anchor": {"kind": "text", "text": "a"}}]})
    spec = parse_spec({"version": 1, "items": [
        {"type": "arrow", "anchor": {"kind": "text", "text": "a"},
         "to": {"kind": "text", "text": "b"}, "text": "flow"}]})
    assert spec.items[0].payload["to"]["text"] == "b"


def test_builtin_styles_validate() -> None:
    for name in ("kb", "review"):
        style = builtin_style(name)
        assert style.name == name
        assert style.ref_width == 720
        assert style.scaled(28, 1440) == 56.0
    with pytest.raises(SpecError):
        builtin_style("nope")


def test_style_schema_strict() -> None:
    style = builtin_style("kb")
    raw = style.to_dict()
    assert parse_style(raw).to_dict() == raw
    bad = dict(raw)
    bad["colors"] = dict(raw["colors"])
    bad["colors"]["badge"] = "red"
    with pytest.raises(SpecError):
        parse_style(bad)
    bad2 = dict(raw)
    bad2["unknown"] = 1
    with pytest.raises(SpecError):
        parse_style(bad2)


def test_asset_names_slugified() -> None:
    assert normalize_asset_name(None, "My Icon.PNG") == "my-icon"
    assert normalize_asset_name("  ", "a.png") == "a"
    assert len(normalize_asset_name("x" * 200, "f.png")) == 64
    with pytest.raises(SpecError):
        normalize_asset_name("!!!", "f.png")
