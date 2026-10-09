"""Tiny typed AST for ffmpeg ``-filter_complex`` graphs.

The builder is intentionally minimal: a :class:`FilterNode` is one filter
invocation with positional/keyword params and explicit input / output pad
labels. A :class:`FilterChain` is a comma-separated sequence; a
:class:`FilterGraph` is the semicolon-joined collection of chains that
ffmpeg consumes.

Helper constructors mirror the most common builtins (``scale``, ``crop``,
``eq``, ``overlay``, ``concat``, ``xfade``, ``setpts``, ``atempo``,
``volume``, ``loudnorm``, ``sidechaincompress``, ``colorchannelmixer``).
The serializer escapes single quotes inside string values per ffmpeg
expression rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "FilterChain",
    "FilterGraph",
    "FilterNode",
    "atempo",
    "build_audio_track_chain",
    "colorchannelmixer",
    "concat",
    "crop",
    "eq",
    "equalizer",
    "loudnorm",
    "overlay",
    "scale",
    "setpts",
    "sidechaincompress",
    "volume",
    "xfade",
]


def _escape(value: Any) -> str:
    text = str(value)
    if any(ch in text for ch in ":,'\\[]"):
        return "'" + text.replace("\\", "\\\\").replace("'", r"\'") + "'"
    return text


@dataclass
class FilterNode:
    """One filter invocation inside a chain."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    positional: list[Any] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    # Params whose values should be emitted verbatim (already escaped by the
    # caller). Needed for Windows file paths inside ``lut3d=file=...`` and
    # ``movie=filename=...`` where ``_escape``'s auto single-quote wrapping
    # interacts badly with ffmpeg's per-filter option parser. Caller is
    # responsible for escaping ``:`` as ``\:`` etc.
    raw_params: dict[str, str] = field(default_factory=dict)

    def serialize(self) -> str:
        parts: list[str] = []
        for label in self.inputs:
            parts.append(f"[{label}]")
        body = self.name
        args: list[str] = []
        if self.positional:
            args.extend(_escape(v) for v in self.positional)
        if self.params:
            args.extend(f"{key}={_escape(value)}" for key, value in self.params.items())
        if self.raw_params:
            args.extend(f"{key}={value}" for key, value in self.raw_params.items())
        if args:
            body = f"{body}={':'.join(args)}"
        parts.append(body)
        for label in self.outputs:
            parts.append(f"[{label}]")
        return "".join(parts)


@dataclass
class FilterChain:
    """Comma-separated sequence of filters sharing the same pad chain."""

    nodes: list[FilterNode] = field(default_factory=list)

    def add(self, node: FilterNode) -> FilterChain:
        self.nodes.append(node)
        return self

    def serialize(self) -> str:
        return ",".join(node.serialize() for node in self.nodes)


@dataclass
class FilterGraph:
    """Collection of chains joined by ';' for the ``-filter_complex`` argument."""

    chains: dict[str, FilterChain] = field(default_factory=dict)

    def add_chain(self, name: str, chain: FilterChain) -> FilterGraph:
        self.chains[name] = chain
        return self

    def serialize(self) -> str:
        return ";".join(chain.serialize() for chain in self.chains.values())


def scale(width: int | str, height: int | str, **params: Any) -> FilterNode:
    return FilterNode(name="scale", positional=[width, height], params=params)


def crop(width: int | str, height: int | str, x: int | str = 0, y: int | str = 0) -> FilterNode:
    return FilterNode(name="crop", positional=[width, height, x, y])


def eq(**params: Any) -> FilterNode:
    return FilterNode(name="eq", params=params)


def overlay(x: int | str = 0, y: int | str = 0, **params: Any) -> FilterNode:
    node = FilterNode(name="overlay", params={"x": x, "y": y, **params})
    return node


def setpts(expr: str) -> FilterNode:
    return FilterNode(name="setpts", positional=[expr])


def atempo(rate: float) -> FilterNode:
    return FilterNode(name="atempo", positional=[rate])


def volume(value: str | float) -> FilterNode:
    return FilterNode(name="volume", positional=[value])


def loudnorm(I: float = -14.0, LRA: float = 7.0, TP: float = -1.5) -> FilterNode:  # noqa: E741
    return FilterNode(name="loudnorm", params={"I": I, "LRA": LRA, "TP": TP})


def sidechaincompress(
    threshold: float = 0.05,
    ratio: float = 4.0,
    attack: float = 10.0,
    release: float = 200.0,
) -> FilterNode:
    return FilterNode(
        name="sidechaincompress",
        params={"threshold": threshold, "ratio": ratio, "attack": attack, "release": release},
    )


def colorchannelmixer(**params: float) -> FilterNode:
    return FilterNode(name="colorchannelmixer", params=dict(params))


def concat(n: int, v: int = 1, a: int = 0) -> FilterNode:
    return FilterNode(name="concat", params={"n": n, "v": v, "a": a})


def xfade(transition: str = "fade", duration: float = 0.5, offset: float = 0.0) -> FilterNode:
    return FilterNode(
        name="xfade",
        params={"transition": transition, "duration": duration, "offset": offset},
    )


def equalizer(frequency: float, width_type: str = "h", width: float = 200.0,
              gain: str | float = 0.0) -> FilterNode:
    return FilterNode(
        name="equalizer",
        params={"f": frequency, "t": width_type, "w": width, "g": gain},
    )


def build_audio_track_chain(
    track: Any,
    project: Any,
    markers: Any,
) -> FilterChain:
    """Lower an :class:`AudioTrack` into an ffmpeg audio filter chain.

    Composes (in order): three-band ``equalizer`` filters when ``track.eq`` is
    set, an animated ``volume`` filter for ``gain_db``, and an optional
    ``sidechaincompress`` keyed off ``track.sidechain``. Animated values are
    lowered through :func:`animated_to_ffmpeg_expr`; constants collapse to
    literal dB->linear conversions.
    """

    from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
    from eks_harness.video.ir.animated import Animated
    from eks_harness.video.ir.tracks import EQ as EQModel

    chain = FilterChain()

    eq_value = getattr(track, "eq", None)
    if eq_value is not None:
        eq_model: EQModel
        if isinstance(eq_value, Animated):
            root = eq_value.root
            eq_model = root if isinstance(root, EQModel) else EQModel()
        else:
            eq_model = eq_value
        chain.add(equalizer(frequency=100.0, width=200.0, gain=eq_model.low_db))
        chain.add(equalizer(frequency=1000.0, width=400.0, gain=eq_model.mid_db))
        chain.add(equalizer(frequency=8000.0, width=2000.0, gain=eq_model.high_db))

    gain_db = getattr(track, "gain_db", None)
    if gain_db is not None:
        gain_expr = animated_to_ffmpeg_expr(gain_db, project, markers)
        if gain_expr is None:
            gain_expr = "0.000000"
        # ffmpeg's volume filter wants a linear multiplier when eval=frame
        # (the trailing 'dB' suffix is only recognised on bare constants, not
        # inside expressions). Convert dB -> linear with 10^(dB/20).
        chain.add(
            FilterNode(
                name="volume",
                params={
                    "volume": f"pow(10,({gain_expr})/20)",
                    "eval": "frame",
                    "precision": "float",
                },
            )
        )

    sidechain = getattr(track, "sidechain", None)
    if sidechain is not None:
        chain.add(
            sidechaincompress(
                threshold=_db_to_linear(sidechain.threshold_db),
                ratio=sidechain.ratio,
                attack=sidechain.attack_ms,
                release=sidechain.release_ms,
            )
        )

    return chain


def _db_to_linear(db: float) -> float:
    return float(10.0 ** (db / 20.0))
