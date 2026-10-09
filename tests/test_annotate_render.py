from __future__ import annotations

import io

from PIL import Image

from eks_harness.annotate.measure import FakeMeasurement
from eks_harness.annotate.render import (
    badge_position,
    bundled_font_files,
    choose_side,
    render_annotated_image,
    wrap_text,
)
from eks_harness.annotate.service import annotate_clean_image, parse_spec_input, resolve_style
from eks_harness.annotate.spec import builtin_style, loads_spec


def _solid_png(width: int = 800, height: int = 600, color: tuple[int, int, int] = (0, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _provider(*, text: str = "Save changes") -> FakeMeasurement:
    anchor = f"text|text={text}"
    return FakeMeasurement({
        anchor: FakeMeasurement.box(100, 100, 120, 40, text=text, scale=1.0),
    })


def test_wrap_uses_real_metrics() -> None:
    fonts = bundled_font_files()
    lines = wrap_text("hello world from the harness", fonts[0], 15, 60)
    assert len(lines) > 1
    narrow = wrap_text("hello world", fonts[0], 15, 10000)
    assert narrow == ["hello world"]


def test_badge_prefers_top_left_and_stays_inside() -> None:
    rect, side = badge_position((100, 100, 120, 40), 31.0, 800, 600)
    assert side == "top"
    assert rect[0] >= 0 and rect[1] >= 0
    rect, _ = badge_position((5, 5, 120, 40), 31.0, 800, 600)
    assert rect[0] >= 0 and rect[1] >= 0


def test_callout_chooses_free_side() -> None:
    assert choose_side((10, 10, 60, 30), 800, 600) in ("bottom", "right")
    assert choose_side((700, 500, 60, 30), 800, 600) in ("top", "left")


def test_render_is_deterministic() -> None:
    clean = _solid_png()
    spec = loads_spec('{"version": 1, "items": ['
                      '{"type": "step", "id": "s", "anchor": {"kind": "text", "text": "Save changes"}}, '
                      '{"type": "highlight", "id": "h", "anchor": {"kind": "text", "text": "Save changes"}}, '
                      '{"type": "caption", "text": "hello"}]}')
    style = builtin_style("kb")
    first = render_annotated_image(clean, spec, {"s": _provider().resolve(spec.items[0].anchor),
                                                "h": _provider().resolve(spec.items[1].anchor)}, style)
    second = render_annotated_image(clean, spec, {"s": _provider().resolve(spec.items[0].anchor),
                                                 "h": _provider().resolve(spec.items[1].anchor)}, style)
    assert first.png == second.png
    assert first.svg.startswith("<svg")
    assert "overlay" not in first.svg
    assert set(first.marks) == {"s", "h", "item-3"}
    assert Image.open(io.BytesIO(first.png)).size == (800, 600)


def test_blur_redact_spotlight_change_pixels() -> None:
    noisy = io.BytesIO()
    Image.effect_noise((320, 200), 64).convert("RGB").save(noisy, format="PNG")
    clean = noisy.getvalue()
    style = builtin_style("kb")
    for item_type in ("blur", "redact", "spotlight"):
        spec = loads_spec('{"version": 1, "items": ['
                          f'{{"type": "{item_type}", "anchor": {{"kind": "coords", "x": 40, "y": 40, '
                          '"width": 120, "height": 60}}]}')
        result = render_annotated_image(clean, spec, {}, style)
        assert result.png != clean


def test_missing_asset_renders_placeholder() -> None:
    clean = _solid_png()
    spec = loads_spec('{"version": 1, "items": ['
                      '{"type": "icon", "anchor": {"kind": "coords", "x": 100, "y": 100, '
                      '"width": 40, "height": 40}, "name": "ghost"}]}')
    result = render_annotated_image(clean, spec, {}, builtin_style("kb"))
    assert "ghost" in result.svg


def test_service_end_to_end_report_ok() -> None:
    clean = _solid_png()
    spec, authored = parse_spec_input(
        '{"version": 1, "style": "kb", "items": ['
        '{"type": "step", "id": "s", "anchor": {"kind": "text", "text": "Save changes"}, '
        '"label": "save"}]}')
    style = resolve_style(spec, None)
    result = annotate_clean_image(clean, spec, style, _provider(), clean_id="clean-1",
                                  authored_spec=authored,
                                  recipe={"kind": "script", "ref": "login", "viewport": "desktop",
                                          "project": "acme/web"})
    assert result.report.ok, [rule.to_dict() for rule in result.report.rules]
    assert result.report.anchor_fallback is False
    assert set(result.crops) == {"s"}
    assert result.meta is not None
    assert result.meta.to_dict()["cleanId"] == "clean-1"
    assert result.meta.to_dict()["styleSnapshot"]["name"] == "kb"
    assert result.crops["s"].startswith(b"\x89PNG")
