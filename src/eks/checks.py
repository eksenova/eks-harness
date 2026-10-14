from __future__ import annotations

import types
import typing
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, cast, get_args, get_origin

from pydantic import BaseModel, JsonValue

from eks.ref import Ref, encode
from eks.tracing import active
from eks_harness.ir.ops.check import CheckUse
from eks_harness.ir.registry import CheckSpec, Registry, RegistryError, default_registry
from eks_harness.ir.target import Target, TargetKind, ensure_target

CheckLike = str | CheckUse | BaseModel
CHECKPOINT_FAMILIES = frozenset({"checkpoint", "visual", "perf"})


def _registry(registry: Registry | None = None) -> Registry:
    if registry is not None:
        return registry
    ctx = active()
    return ctx.registry if ctx is not None else default_registry()


def _members(annotation: object) -> list[object]:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _members(get_args(annotation)[0])
    if origin is typing.Union or origin is types.UnionType:
        return [member for arg in get_args(annotation) for member in _members(arg)]
    return [annotation]


def _holds_targets(annotation: object) -> bool:
    return any(
        get_origin(member) in (list, tuple, Sequence) and any(Target in _members(arg) for arg in get_args(member))
        for member in _members(annotation)
    )


def _target(value: object, what: str, kinds: Mapping[str, TargetKind]) -> object:
    if value is None or isinstance(value, Mapping | Ref):
        return value
    return ensure_target(value, what, kinds)


def _coerce(value: object, annotation: object, what: str, kinds: Mapping[str, TargetKind]) -> object:
    if Target in _members(annotation):
        return _target(value, what, kinds)
    if isinstance(value, list | tuple) and _holds_targets(annotation):
        items = cast("Sequence[object]", value)
        return [_target(item, f"{what}[{index}]", kinds) for index, item in enumerate(items)]
    return value


def _targets(spec: CheckSpec, params: Mapping[str, Any], registry: Registry) -> dict[str, Any]:
    fields = {name: field.annotation for name, field in spec.args.model_fields.items()}
    fields.update({field.alias: field.annotation for field in spec.args.model_fields.values() if field.alias})
    kinds = registry.target_kinds()
    return {
        key: _coerce(value, fields[key], f"check {spec.kind} {key}", kinds) if key in fields else value
        for key, value in params.items()
    }


def _params(spec: CheckSpec, params: Mapping[str, Any], registry: Registry) -> dict[str, JsonValue]:
    model = spec.args.model_validate(encode(_targets(spec, params, registry)))
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _use(kind: object, params: Mapping[str, Any], registry: Registry) -> CheckUse:
    if not isinstance(kind, str) or not kind:
        raise TypeError(f"a check kind is a non-empty string such as 'console_errors', got {kind!r}")
    spec = registry.check(kind)
    return CheckUse(kind=spec.kind, params=_params(spec, params, registry))


def _spec_for_model(model: BaseModel, registry: Registry) -> CheckSpec:
    matches = [spec for spec in registry.checks() if spec.args is type(model)]
    if not matches:
        raise RegistryError(
            f"{type(model).__name__} is not the params model of a registered check; pass a check kind name, "
            "eks.checks.use(kind, **params) or an instance of a model registered with @eks.check"
        )
    if len(matches) > 1:
        kinds = ", ".join(spec.kind for spec in matches)
        raise RegistryError(
            f"{type(model).__name__} is the params model of several checks ({kinds}); "
            "pick one with eks.checks.use(kind, **params)"
        )
    return matches[0]


def use(kind: str, **params: Any) -> CheckUse:
    return _use(kind, params, _registry())


def to_use(value: CheckLike, registry: Registry | None = None) -> CheckUse:
    reg = _registry(registry)
    match value:
        case str():
            return _use(value, {}, reg)
        case CheckUse():
            return _use(value.kind, value.params, reg)
        case BaseModel():
            spec = _spec_for_model(value, reg)
            dumped = value.model_dump(mode="json", by_alias=True, exclude_none=True)
            return CheckUse(kind=spec.kind, params=_params(spec, dumped, reg))
        case _:
            raise TypeError(
                f"a check is a kind name, a CheckUse or a registered check params model, got "
                f"{type(value).__name__} {value!r}; write 'console_errors' or eks.checks.use('visual', threshold=0.01)"
            )


def default_checks(platform: str, checks: Sequence[CheckUse] = (), registry: Registry | None = None) -> tuple[CheckUse, ...]:
    reg = _registry(registry)
    named = {use.kind for use in checks}
    return tuple(
        _use(spec.kind, {}, reg) for spec in reg.checks()
        if spec.default and spec.family in CHECKPOINT_FAMILIES and spec.kind not in named
        and (spec.platforms is None or platform in spec.platforms)
    )


__all__ = ["CHECKPOINT_FAMILIES", "CheckLike", "default_checks", "to_use", "use"]
