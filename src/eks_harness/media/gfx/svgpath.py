from __future__ import annotations

import importlib
import re
from typing import Any

skia: Any = importlib.import_module("skia")

_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_ARITY = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


class SvgPathError(ValueError):
    pass


class _Reader:
    def __init__(self, data: str) -> None:
        self.tokens = _TOKEN.findall(data)
        leftover = _TOKEN.sub("", data).replace(",", "").strip()
        if leftover:
            raise SvgPathError(f"SVG path data has characters that are not path commands or numbers: {leftover[:20]!r}")
        self.index = 0

    def done(self) -> bool:
        return self.index >= len(self.tokens)

    def command(self) -> str | None:
        token = self.tokens[self.index]
        if token.isalpha():
            self.index += 1
            return token
        return None

    def numbers(self, count: int) -> list[float]:
        found: list[float] = []
        for _ in range(count):
            if self.done() or self.tokens[self.index].isalpha():
                raise SvgPathError(f"SVG path data ends a command early near token {self.index}")
            found.append(float(self.tokens[self.index]))
            self.index += 1
        return found

    def flag(self) -> bool:
        return self.numbers(1)[0] != 0.0

    def more(self) -> bool:
        return not self.done() and not self.tokens[self.index].isalpha()


def parse_svg_path(data: str) -> Any:
    path = skia.Path()
    reader = _Reader(data)
    x = y = start_x = start_y = 0.0
    control: tuple[float, float] | None = None
    quad: tuple[float, float] | None = None
    current: str | None = None
    while not reader.done():
        command = reader.command()
        if command is None:
            if current is None:
                raise SvgPathError("SVG path data must start with a command such as M")
            command = "l" if current == "m" else "L" if current == "M" else current
        current = command
        upper = command.upper()
        relative = command.islower()
        dx, dy = (x, y) if relative else (0.0, 0.0)
        if upper == "Z":
            if reader.more():
                raise SvgPathError("SVG path data has numbers after Z")
            path.close()
            x, y = start_x, start_y
            control = quad = None
            continue
        if upper == "A":
            rx, ry, rotation = reader.numbers(3)
            large, sweep = reader.flag(), reader.flag()
            ex, ey = reader.numbers(2)
            ex, ey = ex + dx, ey + dy
            path.arcTo(abs(rx), abs(ry), rotation,
                       skia.Path.ArcSize.kLarge_ArcSize if large else skia.Path.ArcSize.kSmall_ArcSize,
                       skia.PathDirection.kCW if sweep else skia.PathDirection.kCCW, ex, ey)
            x, y = ex, ey
            control = quad = None
            continue
        values = reader.numbers(_ARITY[upper])
        if upper == "M":
            x, y = values[0] + dx, values[1] + dy
            path.moveTo(x, y)
            start_x, start_y = x, y
            control = quad = None
        elif upper == "L":
            x, y = values[0] + dx, values[1] + dy
            path.lineTo(x, y)
            control = quad = None
        elif upper == "H":
            x = values[0] + dx
            path.lineTo(x, y)
            control = quad = None
        elif upper == "V":
            y = values[0] + dy
            path.lineTo(x, y)
            control = quad = None
        elif upper == "C":
            x1, y1, x2, y2, ex, ey = values
            path.cubicTo(x1 + dx, y1 + dy, x2 + dx, y2 + dy, ex + dx, ey + dy)
            control, quad = (x2 + dx, y2 + dy), None
            x, y = ex + dx, ey + dy
        elif upper == "S":
            x2, y2, ex, ey = values
            x1, y1 = (2 * x - control[0], 2 * y - control[1]) if control is not None else (x, y)
            path.cubicTo(x1, y1, x2 + dx, y2 + dy, ex + dx, ey + dy)
            control, quad = (x2 + dx, y2 + dy), None
            x, y = ex + dx, ey + dy
        elif upper == "Q":
            x1, y1, ex, ey = values
            path.quadTo(x1 + dx, y1 + dy, ex + dx, ey + dy)
            quad, control = (x1 + dx, y1 + dy), None
            x, y = ex + dx, ey + dy
        elif upper == "T":
            ex, ey = values
            x1, y1 = (2 * x - quad[0], 2 * y - quad[1]) if quad is not None else (x, y)
            path.quadTo(x1, y1, ex + dx, ey + dy)
            quad, control = (x1, y1), None
            x, y = ex + dx, ey + dy
    return path


__all__ = ["SvgPathError", "parse_svg_path"]
