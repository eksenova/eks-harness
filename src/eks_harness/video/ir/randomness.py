"""Event-level randomness primitives.

These types describe streams of trigger times that can be referenced by
effect fields expecting a discrete-event source. They are standalone IR
nodes - neither :data:`eks_harness.video.ir.curves.Curve` nor
:data:`eks_harness.video.ir.transitions.Transition` members - and round-trip
through JSON like the rest of the IR.

Resolution is deterministic given the seed plus the owning
:class:`eks_harness.video.ir.project.Project`'s ``random_seed`` salt.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .time import BeatRef, MarkerRef, WordRef

__all__ = ["EveryNth", "RandomTrigger", "TriggerSource"]


class _RandomnessBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RandomTrigger(_RandomnessBase):
    """Pseudo-random trigger stream firing at an average rate.

    The renderer should derive its RNG from
    ``numpy.random.default_rng(seed ^ project.random_seed)`` so that
    runs are reproducible. With ``jitter=0`` events are perfectly
    periodic at ``rate_hz``; with ``jitter>0`` each inter-event
    interval is scaled by ``1 + jitter*uniform(-1, 1)``.
    """

    kind: Literal["random_trigger"] = "random_trigger"
    seed: int
    rate_hz: float = Field(default=1.0, gt=0)
    jitter: float = Field(default=0.0, ge=0.0, le=1.0)


class EveryNth(_RandomnessBase):
    """Pick every ``n``-th event from a discrete time-ref source.

    With ``jitter=0`` selection is deterministic: indices ``offset``,
    ``offset+n``, ``offset+2n`` ... With ``jitter>0`` each candidate
    index is independently skipped or kept with probability ``jitter``,
    seeded deterministically.
    """

    kind: Literal["every_nth"] = "every_nth"
    source: BeatRef | WordRef | MarkerRef
    n: int = Field(default=1, ge=1)
    offset: int = Field(default=0, ge=0)
    jitter: float = Field(default=0.0, ge=0.0, le=1.0)
    seed: int = 0


TriggerSource = Annotated[
    RandomTrigger | EveryNth,
    Field(discriminator="kind"),
]
"""Discriminated union of every concrete trigger-source kind.

Reserved for effect fields in subsequent waves; not currently wired
into any effect model.
"""
