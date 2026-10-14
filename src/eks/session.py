from __future__ import annotations

import difflib
import inspect
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Concatenate,
    NoReturn,
    cast,
)

from pydantic import BaseModel

from eks import capture
from eks.api import Api, Backend, act_as
from eks.checks import CheckLike
from eks.debug import Debug
from eks.fake import Fake
from eks.inject import Inject, evaluate
from eks.mobile import Mobile
from eks.net import Net
from eks.overlay import LiveOverlay
from eks.ref import Json, JsonLike, Ref, encode
from eks.tracing import SessionCore, TraceContext, TraceError
from eks.ui import TargetLike, Text, Ui, coerce_target_arg
from eks.web import Web
from eks_harness.ir.ops.capture import DumpKind
from eks_harness.ir.ops.check import (
    DEFAULT_EXPLORE_ACTIONS,
    DEFAULT_EXPLORE_FAILURES,
    ExploreAction,
    ExploreFailure,
)
from eks_harness.ir.plan import SessionSpec
from eks_harness.ir.presets import Pace
from eks_harness.ir.registry import EntrySpec, MacroSpec, RegistryError
from eks_harness.ir.target import TargetKind
from eks_harness.ir.time import Time, ensure_time
from eks_harness.ir.types import Artifact

MACRO_OP = "ctl.macro"


class Namespace:
    __slots__ = ("_name", "_session")

    def __init__(self, session: Session, name: str) -> None:
        self._session = session
        self._name = name

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
        session = self._session
        registry = session.ctx.registry
        qualified = f"{self._name}.{name}"
        macros = {spec.short: spec for spec in registry.macros(self._name)}
        if name in macros:
            spec = macros[name]
            return partial(invoke_macro, session, spec)
        if registry.has_op(qualified):
            return partial(session.op, qualified)
        raise AttributeError(self._unknown(name, macros))

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__:
            object.__setattr__(self, name, value)
            return
        raise AttributeError(f"s.{self._name} is a namespace of macros and ops; it has no settable attribute {name!r}")

    def __repr__(self) -> str:
        return f"Namespace({self._name!r})"

    if not TYPE_CHECKING:
        def __call__(self, *args: Any, **kwargs: Any) -> NoReturn:
            del args, kwargs
            session = self._session
            registry = session.ctx.registry
            members = {name for name in (*dir(type(session)), *vars(session)) if not name.startswith("_")}
            spaces = {spec.namespace for spec in registry.macros()} | set(registry.families())
            message = f"s.{self._name} is not a session method, macro or op, so it cannot be called"
            close = difflib.get_close_matches(self._name, sorted(members | spaces), n=3, cutoff=0.6)
            if close:
                raise AttributeError(message + f"; did you mean {' or '.join(f's.{c}' for c in close)}?")
            raise AttributeError(message + f"; session methods: {', '.join(sorted(members))}")

    def _unknown(self, name: str, macros: Mapping[str, MacroSpec]) -> str:
        registry = self._session.ctx.registry
        known = sorted({*macros, *(spec.name.split(".", 1)[1] for spec in registry.ops(self._name))})
        message = f"s.{self._name} has no macro or op {name!r}"
        if known:
            close = difflib.get_close_matches(name, known, n=3, cutoff=0.6)
            if close:
                return message + f"; did you mean {' or '.join(f's.{self._name}.{c}' for c in close)}?"
            return message + f"; known names: {', '.join(known)}"
        spaces = sorted({spec.namespace for spec in registry.macros()} | set(registry.families()))
        close = difflib.get_close_matches(self._name, spaces, n=3, cutoff=0.6)
        message = f"s.{self._name} is not a macro namespace or op family"
        if close:
            return message + f"; did you mean {' or '.join(f's.{c}' for c in close)}?"
        return message + f"; register a macro with @eks.macro(namespace={self._name!r}) or an op family first"


def coerce_target_args(op: str, model: type[BaseModel], args: Mapping[str, Any],
                       kinds: Mapping[str, TargetKind]) -> dict[str, Any]:
    fields = dict(model.model_fields)
    fields.update({field.alias: field for field in model.model_fields.values() if field.alias})
    return {
        key: value if key not in fields
        else coerce_target_arg(value, fields[key].annotation, f"s.op({op!r}) {key}", kinds)
        for key, value in args.items()
    }


def lazy_macro_error(spec: MacroSpec) -> TraceError:
    return TraceError(
        f"macro {spec.name} is a generator or async function; macros run at plan time and must be plain functions"
    )


def is_lazy_function(fn: Callable[..., Any]) -> bool:
    return inspect.isgeneratorfunction(fn) or inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn)


def invoke_macro(session: Session, spec: MacroSpec, /, *args: Any, **kwargs: Any) -> Any:
    if is_lazy_function(spec.fn):
        raise lazy_macro_error(spec)
    if spec.platforms is not None and session.platform not in spec.platforms:
        raise TraceError(
            f"macro {spec.name} runs on {', '.join(sorted(spec.platforms))}, not on {session.platform}; "
            "call it from a flow on one of those platforms"
        )
    try:
        bound = inspect.signature(spec.fn).bind(session, *args, **kwargs)
    except TypeError as error:
        raise TypeError(f"macro {spec.name}: {error}") from error
    bound.apply_defaults()
    names = list(bound.arguments)[1:]
    params = {name: encode(bound.arguments[name]) for name in names}
    session.ctx.require(spec.provenance)
    with session.record_container(MACRO_OP, {"name": spec.name, "version": spec.version, "params": params}):
        result = spec.fn(*bound.args, **bound.kwargs)
        if inspect.isgenerator(result) or inspect.iscoroutine(result):
            result.close()
            raise lazy_macro_error(spec)
        if inspect.isasyncgen(result):
            raise lazy_macro_error(spec)
        return result


class Session(SessionCore):
    def __init__(self, ctx: TraceContext, spec: SessionSpec) -> None:
        super().__init__(ctx, spec)
        self.ui = Ui(self)
        self.web = Web(self)
        self.mobile = Mobile(self)
        self.api = Api(self)
        self.backend = Backend(self)
        self.net = Net(self)
        self.inject = Inject(self)
        self.fake = Fake(self)
        self.overlay = LiveOverlay(self)
        self.debug = Debug(self)

    def checkpoint(
        self, label: Text, *, viewport: str | None = None, checks: Sequence[CheckLike] | None = None,
        shot: bool = True, full: bool = False, soft: bool = False,
    ) -> Ref[Json]:
        return capture.checkpoint(self, label, viewport=viewport, checks=checks, shot=shot, full=full, soft=soft)

    def explore(
        self, budget: Time, *, steps: int | None = None, depth: int = 3, seed: int | None = None,
        allow: Sequence[TargetLike] = (), avoid: Sequence[TargetLike] = (),
        actions: Sequence[ExploreAction] = DEFAULT_EXPLORE_ACTIONS,
        fail_on: Sequence[ExploreFailure] = DEFAULT_EXPLORE_FAILURES, checks: Sequence[CheckLike] = (),
        label: str | None = None, soft: bool = False,
    ) -> Ref[Json]:
        return capture.explore(self, budget, steps=steps, depth=depth, seed=seed, allow=allow, avoid=avoid,
                               actions=actions, fail_on=fail_on, checks=checks, label=label, soft=soft)

    def shot(
        self, name: Text, *, element: TargetLike | None = None, focus: TargetLike | None = None, full: bool = False,
        viewport: str | None = None, hide: Sequence[TargetLike] = (), at: Time | None = None,
    ) -> Ref[Artifact]:
        return capture.shot(self, name, element=element, focus=focus, full=full, viewport=viewport, hide=hide, at=at)

    def dump(self, kind: DumpKind, *, name: Text | None = None, viewport: str | None = None) -> Ref[Artifact]:
        return capture.dump(self, kind, name=name, viewport=viewport)

    def har(self, *, name: Text | None = None, whole: bool = False) -> Ref[Artifact]:
        return capture.har(self, name=name, whole=whole)

    @contextmanager
    def record(
        self, name: Text, *, pace: str | Pace | None = None, viewport: str | None = None, pointer: bool = True,
        from_here: bool = True, focus: TargetLike | None = None, trim: bool | None = None, hide: Sequence[TargetLike] = (),
    ) -> Iterator[Ref[Artifact]]:
        with capture.record(self, name, pace=pace, viewport=viewport, pointer=pointer, from_here=from_here,
                            focus=focus, trim=trim, hide=hide) as video:
            yield video

    def hold(self, duration: Time, *, at: Time | None = None, reason: str | None = None) -> Ref[None]:
        return capture.hold(self, duration, at=at, reason=reason)

    def as_(self, persona: str, *, at: Time | None = None) -> Ref[None]:
        return act_as(self, persona, at=at)

    def eval(self, source: Text, arg: JsonLike = None) -> Ref[Json]:
        return evaluate(self, source, arg)

    def op(self, name: str, args: Mapping[str, Any] | None = None, /, **kwargs: Any) -> Ref[Json]:
        if args is not None and not isinstance(args, Mapping):
            raise TypeError(
                f"s.op({name!r}, args) takes the arguments as a mapping such as {{'target': '#save'}}, "
                f"got {type(args).__name__} {args!r}; or pass them as keywords"
            )
        merged = dict(args or {})
        both = sorted(set(merged) & set(kwargs))
        if both:
            raise TypeError(f"s.op({name!r}) got {', '.join(both)} both in the mapping and as keywords; pass each once")
        merged.update(kwargs)
        registry = self.ctx.registry
        spec = registry.op(name)
        return self.record_op(name, coerce_target_args(name, spec.args, merged, registry.target_kinds()))

    def call(self, args: BaseModel, *, at: Time | None = None) -> Ref[Json]:
        if not isinstance(args, BaseModel):
            raise TypeError(
                f"s.call takes an instance of an op argument model, got {type(args).__name__} {args!r}; "
                "call ops by name with s.op('family.name', ...)"
            )
        model = type(args)
        matches = [spec.name for spec in self.ctx.registry.ops() if spec.args is model]
        if not matches:
            raise RegistryError(
                f"no op takes {model.__module__}.{model.__qualname__} as its arguments; "
                "register the model with @eks.op(family) or call the op by name with s.op(...)"
            )
        if len(matches) > 1:
            raise RegistryError(
                f"{model.__qualname__} is the argument model of several ops ({', '.join(matches)}); "
                "call the one you mean with s.op(name, ...)"
            )
        return self.record_op(matches[0], args, at=None if at is None else ensure_time(at, f"s.call({matches[0]}) at"))

    def macro[**P, R](self, fn: Callable[Concatenate[Session, P], R], /) -> Callable[P, R]:
        spec = self.ctx.registry.macro_for(fn)
        return cast("Callable[P, R]", partial(invoke_macro, self, spec))

    def __getattr__(self, name: str) -> Namespace:
        if name.startswith("_"):
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
        return Namespace(self, name)


def open_flow_session(ctx: TraceContext, entry: EntrySpec) -> Session:
    spec = ctx.session
    if spec is None:
        raise TraceError(f"flow {entry.name} has no session")
    session = Session(ctx, spec)
    if spec.persona:
        session.record_op("session.as", {"persona": spec.persona})
    return session


__all__ = ["MACRO_OP", "Namespace", "Session", "invoke_macro", "open_flow_session"]
