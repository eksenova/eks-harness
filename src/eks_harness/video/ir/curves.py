"""Continuous-time curve generators for animated properties.

A ``Curve`` describes a function ``f(t) -> float`` evaluated at every project
frame. Concrete renderers (in ``eks_harness.video.compile.animated_resolve``) sample
the curve to produce a per-frame array.

``Lambda`` accepts a small subset of Python expressions through
``asteval``'s sandbox; the only injected name is ``t`` (seconds).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .easing import Easing
from .time import BeatRef

if TYPE_CHECKING:
    from .animated import Keyframe

__all__ = [
    "LFO",
    "BeatPulse",
    "Bezier",
    "Bounce",
    "BoxEnv",
    "Curve",
    "EasingEnv",
    "Envelope",
    "ExpDecay",
    "ExpDecayEnv",
    "Lambda",
    "LinearEnv",
    "Noise",
    "RandomChoice",
    "Sine",
    "Spring",
    "Step",
]


class _CurveBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpDecayEnv(_CurveBase):
    """Exponential decay envelope: ``exp(-t / tau)``."""

    kind: Literal["exp_decay_env"] = "exp_decay_env"
    tau: float


class LinearEnv(_CurveBase):
    """Triangular envelope with explicit rise / fall durations (seconds)."""

    kind: Literal["linear_env"] = "linear_env"
    rise: float
    fall: float


class BoxEnv(_CurveBase):
    """Rectangular envelope of ``width`` seconds at amplitude 1.0."""

    kind: Literal["box_env"] = "box_env"
    width: float


class EasingEnv(_CurveBase):
    """Duration-bounded envelope shaped by any :class:`Easing` variant.

    Lets the user spell things like "250ms glitch on every beat with
    easeOutElastic-shaped strength" via::

        BeatPulse(
            trigger=BeatRef(stream="kick", offset_ms=50),
            envelope=EasingEnv(duration=0.25, easing=ElasticOut()),
            intensity_ramp=0.8,
        )

    The envelope is zero outside ``[trigger, trigger + duration]``. Inside
    that window, the easing's ``evaluate(t01)`` is sampled at
    ``t01 = (frame - trigger) / duration_in_frames``. ``direction``
    controls whether the eased curve runs forward (``"in"``: 0→1, like a
    fade-up) or is inverted (``"out"``: 1→0, like a fade-down / one-shot
    decay). Default is ``"out"`` because the most common use is a strong
    initial pop that decays, matching ``ExpDecayEnv`` semantics.
    """

    kind: Literal["easing_env"] = "easing_env"
    duration: float = Field(gt=0)
    easing: Easing
    direction: Literal["in", "out"] = "out"


Envelope = Annotated[
    ExpDecayEnv | LinearEnv | BoxEnv | EasingEnv,
    Field(discriminator="kind"),
]


class BeatPulse(_CurveBase):
    """Pulse curve triggered on every beat in the referenced stream.

    ``intensity_ramp`` is either a constant amplitude or a list of keyframes
    spanning the project; the amplitude at each trigger is sampled from the
    ramp and multiplied into the envelope.
    """

    kind: Literal["beat_pulse"] = "beat_pulse"
    trigger: BeatRef
    envelope: Envelope
    intensity_ramp: float | list[Keyframe[float]]
    baseline: float = 0.0


class ExpDecay(_CurveBase):
    """Single-shot exponential decay anchored at t=0."""

    kind: Literal["exp_decay"] = "exp_decay"
    tau: float
    baseline: float = 0.0


class Sine(_CurveBase):
    kind: Literal["sine"] = "sine"
    freq_hz: float
    phase: float = 0.0
    amp: float = 1.0
    offset: float = 0.0


class LFO(_CurveBase):
    """Low-frequency oscillator with a selectable waveform shape."""

    kind: Literal["lfo"] = "lfo"
    shape: Literal["tri", "saw", "square"]
    freq_hz: float
    amp: float = 1.0
    offset: float = 0.0
    phase: float = 0.0


class Lambda(_CurveBase):
    """Sandboxed expression. Only ``t`` (seconds) is exposed."""

    kind: Literal["lambda"] = "lambda"
    expr: str

    @field_validator("expr")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Lambda.expr must be non-empty")
        return value


class Spring(_CurveBase):
    """Physics-based damped harmonic oscillator settling toward ``target``.

    Integrated per-frame with ``dt = 1 / fps`` starting from ``initial``
    position with zero velocity. ``stiffness`` controls oscillation
    frequency; ``damping`` controls how quickly oscillations die out.
    """

    kind: Literal["spring"] = "spring"
    target: float
    stiffness: float = Field(default=100.0, gt=0)
    damping: float = Field(default=10.0, gt=0)
    initial: float = 0.0


class Noise(_CurveBase):
    """Deterministic uniform random noise in ``[min_value, max_value]``.

    With ``hold == 0`` a fresh sample is drawn every frame. With ``hold > 0``
    each sample is held for ``hold`` seconds before redrawing (sample-and-hold).
    The seed is salted with the project-level ``random_seed`` (if set) so
    several curves in the same project can be jittered together.
    """

    kind: Literal["noise"] = "noise"
    seed: int
    min_value: float = 0.0
    max_value: float = 1.0
    hold: float = Field(default=0.0, ge=0)


class RandomChoice(_CurveBase):
    """Discrete random pick from ``values``, holding each pick for ``hold`` seconds.

    Salted with the project-level ``random_seed`` like :class:`Noise`.
    """

    kind: Literal["random_choice"] = "random_choice"
    seed: int
    values: list[float] = Field(min_length=1)
    hold: float = Field(default=0.5, gt=0)


class Step(_CurveBase):
    """Deterministic round-robin through ``values``, advancing every ``hold`` seconds."""

    kind: Literal["step"] = "step"
    values: list[float] = Field(min_length=1)
    hold: float = Field(gt=0)


class Bezier(_CurveBase):
    """Cubic Bezier curve in project-time.

    Goes from ``p0`` at ``t=0`` to ``p3`` at ``t=duration`` via control points
    ``p1`` and ``p2``. Beyond ``duration`` the value is clamped to ``p3``.

    Distinct from :class:`eks_harness.video.ir.easing.CubicBezier` which is a
    normalized easing (0..1 -> 0..1); this is a value-producing curve in
    seconds.
    """

    kind: Literal["bezier"] = "bezier"
    p0: float
    p1: float
    p2: float
    p3: float
    duration: float = Field(gt=0)


class Bounce(_CurveBase):
    """Damped absolute-value sine: ``amplitude * exp(-decay*t) * |sin(2*pi*t/period)|``.

    Useful for "settling" animations such as a UI element bouncing into place.
    """

    kind: Literal["bounce"] = "bounce"
    amplitude: float
    period: float = Field(gt=0)
    decay: float = 1.0


Curve = Annotated[
    BeatPulse
    | ExpDecay
    | Sine
    | LFO
    | Lambda
    | Spring
    | Noise
    | RandomChoice
    | Step
    | Bezier
    | Bounce,
    Field(discriminator="kind"),
]
