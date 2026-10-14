from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure

FONTS_DIR = Path(__file__).resolve().parent.parent / "annotate" / "fonts"
SHIPPED: Mapping[str, Mapping[int, str]] = {
    "IBM Plex Sans": {400: "IBMPlexSans-Regular.ttf", 600: "IBMPlexSans-SemiBold.ttf"},
    "IBM Plex Mono": {400: "IBMPlexMono-Regular.ttf"},
}


def shipped_font(family: str, weight: int) -> Path:
    weights = SHIPPED.get(family)
    if weights is None:
        raise NodeFailure("font_unavailable", f"font family {family!r} is not shipped; shipped families are "
                          f"{', '.join(sorted(SHIPPED))}, or pass a font file")
    name = weights.get(weight)
    if name is None:
        raise NodeFailure("font_unavailable", f"{family} has no weight {weight}; it ships weights "
                          f"{', '.join(str(value) for value in sorted(weights))}, or pass a font file",
                          detail={"family": family, "weight": weight,
                                  "weights": list[JsonValue](sorted(weights))})
    return FONTS_DIR / name


def font_path(font: JsonValue, weight: int, media: Callable[[JsonValue], Path]) -> Path:
    if isinstance(font, str):
        return shipped_font(font, weight)
    if isinstance(font, Mapping):
        return media(font)
    raise NodeFailure("font_unavailable", f"{font!r} is neither a shipped family nor a font file")


__all__ = ["FONTS_DIR", "SHIPPED", "font_path", "shipped_font"]
