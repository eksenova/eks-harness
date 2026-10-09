"""``eks-harness://video/easings/{name}`` - small curve preview PNG.

Renders the easing function's [0, 1] -> [0, 1] curve into a 256x256 PNG using
PIL. The curve is sampled directly from the ``Easing`` evaluator when
available; otherwise a placeholder line is drawn.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Callable

from ._spec import ResourceSpec
from PIL import Image, ImageDraw

__all__ = ["BUILTIN_EASINGS", "RESOURCES", "read_easing_preview"]


BUILTIN_EASINGS: dict[str, Callable[[float], float]] = {
    "linear": lambda t: t,
    "ease_in": lambda t: t * t,
    "ease_out": lambda t: 1.0 - (1.0 - t) * (1.0 - t),
    "ease_in_out": lambda t: 3 * t * t - 2 * t * t * t,
    "ease_in_cubic": lambda t: t ** 3,
    "ease_out_cubic": lambda t: 1.0 - (1.0 - t) ** 3,
    "ease_in_out_cubic": lambda t: 4 * t ** 3 if t < 0.5 else 1 - ((-2 * t + 2) ** 3) / 2,
}


def read_easing_preview(name: str) -> bytes:
    if name not in BUILTIN_EASINGS:
        raise KeyError(f"unknown easing {name!r}; available: {', '.join(sorted(BUILTIN_EASINGS))}")
    return _render_curve(BUILTIN_EASINGS[name])


def _render_curve(fn: Callable[[float], float], *, size: int = 256, samples: int = 128) -> bytes:
    image = Image.new("RGB", (size, size), color=(245, 246, 250))
    draw = ImageDraw.Draw(image)

    margin = 16
    plot = size - margin * 2
    draw.rectangle((margin, margin, size - margin, size - margin), outline=(180, 184, 193))

    points: list[tuple[float, float]] = []
    for i in range(samples + 1):
        t = i / samples
        v = max(0.0, min(1.0, fn(t)))
        x = margin + t * plot
        y = (size - margin) - v * plot
        points.append((x, y))

    draw.line(points, fill=(36, 132, 245), width=2)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def encode_base64_preview(name: str) -> str:
    """Return the easing preview as a base64-encoded PNG. Used by tests."""

    return base64.b64encode(_render_curve(BUILTIN_EASINGS[name])).decode("ascii")


RESOURCES = [
    ResourceSpec(uri='eks-harness://video/easings/{name}', name='video_easing_preview', title='Easing preview',
                 description='256x256 PNG preview of the named easing curve.',
                 mime_type='image/png', read=read_easing_preview, needs_roots=False, needs_registry=False),
]
