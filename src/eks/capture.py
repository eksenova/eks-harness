from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from eks.checks import CHECKPOINT_FAMILIES, CheckLike, to_use
from eks.ref import Json, Ref
from eks.tracing import SessionCore, TraceError
from eks.ui import TargetLike, Text, maybe_target, to_target, to_text
from eks_harness.ir.catalog.durations import ensure_positive_duration
from eks_harness.ir.ops.capture import DumpKind
from eks_harness.ir.ops.check import (
    DEFAULT_EXPLORE_ACTIONS,
    DEFAULT_EXPLORE_FAILURES,
    CheckUse,
    ExploreAction,
    ExploreFailure,
)
from eks_harness.ir.presets import Pace
from eks_harness.ir.target import Target
from eks_harness.ir.time import Time, ensure_time
from eks_harness.ir.types import Artifact

RECORDING_FLAGS = ("record", "take")


def _hidden(session: SessionCore, value: Sequence[TargetLike], what: str) -> tuple[Target, ...]:
    if isinstance(value, str | Target):
        raise TypeError(f"{what} takes a list of targets, got the single target {value!r}; write [{value!r}]")
    return tuple(to_target(session, item, f"{what}[{index}]") for index, item in enumerate(value))


def _args(**values: Any) -> dict[str, Any]:
    return {name: value for name, value in values.items() if value is not None}


def _optional_text(value: Text | None, what: str) -> Text | None:
    return to_text(value, what) if value is not None else None


def checkpoint(
    session: SessionCore,
    label: Text,
    *,
    viewport: str | None = None,
    checks: Sequence[CheckLike] | None = None,
    shot: bool = True,
    full: bool = False,
    soft: bool = False,
) -> Ref[Json]:
    uses = _checkpoint_uses(session, checks, "checkpoint checks=") if checks is not None else None
    return session.record_op(
        "check.checkpoint",
        _args(label=to_text(label, "s.checkpoint label"), viewport=viewport, checks=uses, shot=shot, full=full, soft=soft),
    )


def _checkpoint_uses(session: SessionCore, checks: Sequence[CheckLike], what: str) -> tuple[CheckUse, ...]:
    if isinstance(checks, str):
        raise TypeError(f"{what} takes a list, got the string {checks!r}; write [{checks!r}]")
    registry = session.ctx.registry
    uses = tuple(to_use(item, registry) for item in checks)
    for use in uses:
        spec = registry.check(use.kind)
        if spec.family not in CHECKPOINT_FAMILIES:
            raise TraceError(
                f"check {use.kind!r} belongs to the {spec.family} family and cannot run in {what}; it takes checks "
                f"of the {', '.join(sorted(CHECKPOINT_FAMILIES))} families"
            )
        session.ctx.require(spec.provenance)
    return uses


def explore(
    session: SessionCore,
    budget: Time,
    *,
    steps: int | None = None,
    depth: int = 3,
    seed: int | None = None,
    allow: Sequence[TargetLike] = (),
    avoid: Sequence[TargetLike] = (),
    actions: Sequence[ExploreAction] = DEFAULT_EXPLORE_ACTIONS,
    fail_on: Sequence[ExploreFailure] = DEFAULT_EXPLORE_FAILURES,
    checks: Sequence[CheckLike] = (),
    label: str | None = None,
    soft: bool = False,
) -> Ref[Json]:
    for name, value in (("steps", steps), ("depth", depth), ("seed", seed)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise TypeError(f"s.explore {name}= takes an int, got {type(value).__name__} {value!r}")
    args = _args(
        budget=ensure_positive_duration(budget, "s.explore budget"),
        steps=steps,
        depth=depth,
        seed=seed,
        allow=_hidden(session, allow, "s.explore allow"),
        avoid=_hidden(session, avoid, "s.explore avoid"),
        actions=_names(actions, "s.explore actions"),
        fail_on=_names(fail_on, "s.explore fail_on"),
        checks=_checkpoint_uses(session, checks, "s.explore checks"),
        label=label,
        soft=soft,
    )
    return session.record_op("check.explore", args)


def _names(values: Sequence[str], what: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{what} takes a list of names, got the string {values!r}; write [{values!r}]")
    return tuple(values)


def shot(
    session: SessionCore,
    name: Text,
    *,
    element: TargetLike | None = None,
    focus: TargetLike | None = None,
    full: bool = False,
    viewport: str | None = None,
    hide: Sequence[TargetLike] = (),
    at: Time | None = None,
) -> Ref[Artifact]:
    args = _args(
        name=to_text(name, "s.shot name"),
        element=maybe_target(session, element, "shot element"),
        focus=maybe_target(session, focus, "shot focus"),
        full=full,
        viewport=viewport,
        hide=_hidden(session, hide, "shot hide"),
    )
    pin = ensure_time(at, "shot at") if at is not None else None
    return session.record_op("capture.shot", args, at=pin)


def dump(session: SessionCore, kind: DumpKind, *, name: Text | None = None, viewport: str | None = None) -> Ref[Artifact]:
    return session.record_op(
        "capture.dump", _args(kind=kind, name=_optional_text(name, "s.dump name="), viewport=viewport)
    )


def har(session: SessionCore, *, name: Text | None = None, whole: bool = False) -> Ref[Artifact]:
    return session.record_op("capture.har", _args(name=_optional_text(name, "s.har name="), whole=whole))


@contextmanager
def record(
    session: SessionCore,
    name: Text,
    *,
    pace: str | Pace | None = None,
    viewport: str | None = None,
    pointer: bool = True,
    from_here: bool = True,
    focus: TargetLike | None = None,
    trim: bool | None = None,
    hide: Sequence[TargetLike] = (),
) -> Iterator[Ref[Artifact]]:
    if session.ctx.has_flag("record"):
        raise TraceError(
            "s.record(...) cannot be nested inside another recording; end the outer `with s.record(...)` block first"
        )
    if pace is not None and not isinstance(pace, str | Pace):
        raise TypeError(f"record pace= takes a pace preset name such as 'demo' or a Pace, got {type(pace).__name__}")
    args = _args(
        name=to_text(name, "s.record name"),
        pace=pace,
        viewport=viewport,
        pointer=pointer,
        from_here=from_here,
        focus=maybe_target(session, focus, "record focus"),
        trim=trim,
        hide=_hidden(session, hide, "record hide"),
    )
    with session.record_container("capture.record", args, flags=("record",)) as video:
        yield video


def hold(session: SessionCore, duration: Time, *, at: Time | None = None, reason: str | None = None) -> Ref[None]:
    if not any(session.ctx.has_flag(flag) for flag in RECORDING_FLAGS):
        raise TraceError(
            "s.hold(...) works only while recording; call it inside `with s.record(...):` or inside a take's flow"
        )
    args = _args(duration=ensure_positive_duration(duration, "hold duration"), reason=reason)
    pin = ensure_time(at, "hold at") if at is not None else None
    return session.record_op("capture.hold", args, at=pin)


__all__ = ["checkpoint", "dump", "explore", "har", "hold", "record", "shot"]
