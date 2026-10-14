from __future__ import annotations

from collections.abc import Mapping

STROKE = "stroke"
FILL = "fill"

ICON_PATHS: Mapping[str, tuple[str, str]] = {
    "check": (STROKE, "M5 12.5 L10 17.5 L19 7"),
    "cross": (STROKE, "M6 6 L18 18 M18 6 L6 18"),
    "warning": (STROKE, "M12 4 L21 19.5 L3 19.5 Z M12 10 L12 14 M12 17 L12 17.2"),
    "info": (STROKE, "M12 11 L12 18 M12 6.5 L12 6.7"),
    "tap": (STROKE, "M10 20 L10 10 A2 2 0 0 1 14 10 L14 14 L18 15 L17 20 M7 7 A5 5 0 0 1 17 7"),
    "star": (FILL, "M12 3 L14.7 9 L21 9.3 L16 13.4 L17.8 20 L12 16.3 L6.2 20 L8 13.4 L3 9.3 L9.3 9 Z"),
    "plus": (STROKE, "M12 5 L12 19 M5 12 L19 12"),
    "minus": (STROKE, "M5 12 L19 12"),
    "lock": (STROKE, "M6 11 L18 11 L18 20 L6 20 Z M8.5 11 L8.5 8 A3.5 3.5 0 0 1 15.5 8 L15.5 11"),
    "bell": (STROKE, "M6 17 L6 11 A6 6 0 0 1 18 11 L18 17 L20 18 L4 18 Z M10 20.5 L14 20.5"),
    "arrow-up": (STROKE, "M12 19 L12 5 M6 11 L12 5 L18 11"),
    "arrow-down": (STROKE, "M12 5 L12 19 M6 13 L12 19 L18 13"),
    "arrow-left": (STROKE, "M19 12 L5 12 M11 6 L5 12 L11 18"),
    "arrow-right": (STROKE, "M5 12 L19 12 M13 6 L19 12 L13 18"),
    "search": (STROKE, "M10.5 4 A6.5 6.5 0 1 1 10.49 4 M15.5 15.5 L20 20"),
    "heart": (FILL, "M12 20 L4.5 12.5 A4.3 4.3 0 0 1 12 6.8 A4.3 4.3 0 0 1 19.5 12.5 Z"),
}


def icon_svg(name: str, *, x: float, y: float, size: float, color: str) -> str:
    try:
        mode, path = ICON_PATHS[name]
    except KeyError:
        raise ValueError(f"unknown overlay icon {name!r}; the set is {', '.join(ICON_PATHS)}") from None
    scale = size / 24.0
    paint = (f'fill="none" stroke="{color}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"'
             if mode == STROKE else f'fill="{color}"')
    return f'<path transform="translate({x:.2f} {y:.2f}) scale({scale:.4f})" d="{path}" {paint}/>'


__all__ = ["ICON_PATHS", "icon_svg"]
