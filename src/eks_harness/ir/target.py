from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from eks_harness.ir.refs import FmtExpr, ItemExpr, RefExpr, RStr

Platform = Literal["web", "ios", "android"]
Surface = Literal["web", "ios", "android", "screenshot", "video", "blender", "scene", "gfx"]
Modifier = Literal["within", "near", "nth"]
NodeKind = Literal["screen", "button", "input", "switch", "text", "view"]

PLATFORMS: tuple[Platform, ...] = ("web", "ios", "android")
SURFACES: tuple[Surface, ...] = ("web", "ios", "android", "screenshot", "video", "blender", "scene", "gfx")
NATIVE_SURFACES: tuple[str, ...] = ("ios", "android")
KIND_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$"


class TargetError(ValueError):
    pass


class _TargetArgs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ByTestId(_TargetArgs):
    id: RStr = Field(description="Test id: data-testid or id on web, testID on React Native")


class ByRole(_TargetArgs):
    role: str = Field(description="Accessibility role such as button or dialog")
    name: RStr | None = Field(default=None, description="Accessible name")
    exact: bool = Field(default=False, strict=True, description="Match the name exactly instead of as a substring")


class ByText(_TargetArgs):
    text: RStr = Field(description="Visible text")
    exact: bool = Field(default=True, strict=True, description="Match the whole text instead of a substring")


class ByLabel(_TargetArgs):
    text: RStr = Field(description="Label text")
    exact: bool = Field(default=False, strict=True, description="Match the whole label instead of a substring")


class ByPlaceholder(_TargetArgs):
    text: RStr = Field(description="Placeholder text")
    exact: bool = Field(default=False, strict=True, description="Match the whole placeholder instead of a substring")


class ByCss(_TargetArgs):
    selector: RStr = Field(description="CSS selector (web only)")


class ByKind(_TargetArgs):
    kind: NodeKind = Field(description="React Native component kind such as button")


class ByPoint(_TargetArgs):
    x: float = Field(strict=True, description="x in viewport pixels")
    y: float = Field(strict=True, description="y in viewport pixels")


class ByRect(_TargetArgs):
    x: float = Field(strict=True, description="Left edge in viewport pixels")
    y: float = Field(strict=True, description="Top edge in viewport pixels")
    w: float = Field(gt=0, strict=True, description="Width in pixels")
    h: float = Field(gt=0, strict=True, description="Height in pixels")


class ByObject(_TargetArgs):
    name: str = Field(description="Blender object name")


class ByLayer(_TargetArgs):
    name: str = Field(description="Layer id of a gfx or web scene")


@dataclass(frozen=True, slots=True)
class TargetKind:
    name: str
    args: type[BaseModel]
    surfaces: frozenset[str]
    shorthand: str | None = None
    description: str = ""


_ELEMENT = frozenset({"web", "ios", "android", "screenshot", "video", "scene"})
_ALL = frozenset(SURFACES)

BUILTIN_KINDS: Mapping[str, TargetKind] = {
    "test_id": TargetKind("test_id", ByTestId, _ELEMENT, "testid", "element by test id (data-testid or id on web, testID on React Native)"),
    "role": TargetKind("role", ByRole, _ELEMENT, "role", "element by accessibility role and optional accessible name"),
    "text": TargetKind("text", ByText, _ELEMENT, "text", "element by visible text"),
    "label": TargetKind("label", ByLabel, _ELEMENT, "label", "form control by its label or accessibility label"),
    "placeholder": TargetKind("placeholder", ByPlaceholder, _ELEMENT, "placeholder", "input by placeholder text"),
    "css": TargetKind("css", ByCss, frozenset({"web", "screenshot", "scene"}), "css", "CSS selector (web only)"),
    "kind": TargetKind("kind", ByKind, frozenset({"ios", "android", "screenshot", "video"}), "kind", "React Native node kind"),
    "point": TargetKind("point", ByPoint, _ALL, "point", "a point in surface pixels"),
    "rect": TargetKind("rect", ByRect, _ALL, "rect", "a rectangle in surface pixels"),
    "object": TargetKind("object", ByObject, frozenset({"blender"}), "object", "a Blender object by name"),
    "layer": TargetKind("layer", ByLayer, frozenset({"gfx"}), "layer", "a gfx layer by name"),
}

MODIFIER_SURFACES: Mapping[str, frozenset[str]] = {
    "within": _ELEMENT,
    "near": _ELEMENT,
    "nth": _ALL,
}


class Target(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    by: str = Field(pattern=KIND_PATTERN, description="Target kind such as test_id, role, text or css")
    args: dict[str, JsonValue] = Field(default_factory=dict, description="Arguments of the target kind")
    within_: Target | None = Field(default=None, alias="within", description="Only match inside this target")
    near_: Target | None = Field(default=None, alias="near", description="Prefer the match closest to this target")
    nth_: int | None = Field(default=None, alias="nth", strict=True, description="Pick one of several matches, 0-based; -1 is the last")
    surface_: Literal["native"] | None = Field(default=None, alias="surface", description="native: match system dialogs and OS screens outside the app instead of the app's own tree")

    def within(self, scope: Target | str, *, kinds: Mapping[str, TargetKind] | None = None) -> Target:
        return self.model_copy(update={"within_": ensure_target(scope, "within()", kinds)})

    def near(self, anchor: Target | str, *, kinds: Mapping[str, TargetKind] | None = None) -> Target:
        return self.model_copy(update={"near_": ensure_target(anchor, "near()", kinds)})

    def nth(self, index: int) -> Target:
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError(f"nth() takes an int index (0-based, -1 for the last match), got {index!r}")
        return self.model_copy(update={"nth_": index})

    @property
    def native(self) -> Target:
        return self.model_copy(update={"surface_": "native"})

    @property
    def first(self) -> Target:
        return self.nth(0)

    @property
    def last(self) -> Target:
        return self.nth(-1)

    def __str__(self) -> str:
        return describe(self)


SHORTHAND_PATTERN = r"[A-Za-z][A-Za-z0-9_.-]*"

_SENTINEL_TOKEN = "\ue000r[0-9]+\ue001"
_ID_RE = re.compile(f"#((?:[A-Za-z0-9_.:-]|{_SENTINEL_TOKEN})+)")
_PREFIX_RE = re.compile(f"({SHORTHAND_PATTERN})=(.*)", re.DOTALL)
_SHORTHAND_RE = re.compile(SHORTHAND_PATTERN)
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_JSON_SCALARS = (str, int, float, bool)
_TEXT_MODELS = (FmtExpr, RefExpr, ItemExpr)


type KindsProvider = Callable[[], Mapping[str, TargetKind]]


@dataclass(slots=True)
class _DefaultKinds:
    provider: KindsProvider | None = None


_DEFAULT_KINDS = _DefaultKinds()


def use_default_kinds(provider: KindsProvider | None) -> None:
    _DEFAULT_KINDS.provider = provider


def _kinds(kinds: Mapping[str, TargetKind] | None) -> Mapping[str, TargetKind]:
    if kinds is None and _DEFAULT_KINDS.provider is not None:
        kinds = _DEFAULT_KINDS.provider()
    if not kinds:
        return BUILTIN_KINDS
    return {**BUILTIN_KINDS, **kinds}


def _shorthands(kinds: Mapping[str, TargetKind]) -> dict[str, TargetKind]:
    found: dict[str, TargetKind] = {}
    for kind in kinds.values():
        if kind.shorthand is None:
            continue
        if not _SHORTHAND_RE.fullmatch(kind.shorthand):
            raise TargetError(
                f"target kind {kind.name!r} declares shorthand {kind.shorthand!r}, which can never be written "
                "as SHORTHAND=VALUE; a shorthand starts with a letter and holds only letters, digits, '_', '.' and '-'"
            )
        other = found.get(kind.shorthand)
        if other is not None and other.name != kind.name:
            raise TargetError(
                f"target kinds {other.name!r} and {kind.name!r} both declare shorthand {kind.shorthand!r}; "
                "give one of them another shorthand"
            )
        found[kind.shorthand] = kind
    return found


def _special(kind: TargetKind) -> Literal["role", "point", "rect"] | None:
    if kind.name == "role" and kind.args is ByRole:
        return "role"
    if kind.name == "point" and kind.args is ByPoint:
        return "point"
    if kind.name == "rect" and kind.args is ByRect:
        return "rect"
    return None


def _errors(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(step) for step in error["loc"])
        parts.append(f"{loc}: {error['msg']}" if loc else error["msg"])
    return "; ".join(parts)


def _dump_args(kind: TargetKind, raw: Mapping[str, Any], shown: str) -> dict[str, JsonValue]:
    try:
        model = kind.args.model_validate(dict(raw))
    except ValidationError as exc:
        raise TargetError(
            f"invalid {kind.name} target {shown}: {_errors(exc)}; check the arguments of by.{kind.name}(...)"
        ) from exc
    dumped = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    if not _all_finite(dumped):
        raise TargetError(
            f"invalid {kind.name} target {shown}: arguments must be finite numbers; check the arguments of by.{kind.name}(...)"
        )
    return dumped


def _all_finite(value: JsonValue) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_all_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_all_finite(item) for item in value)
    return True


def _make(by: str, args: Mapping[str, Any]) -> Target:
    try:
        return Target(by=by, args=dict(args))
    except ValidationError as exc:
        raise TargetError(
            f"invalid target kind {by!r}: {_errors(exc)}; kinds are lower case names like 'test_id' or 'chip'"
        ) from exc


def _number(text: str, shown: str, form: str) -> float:
    value = text.strip()
    if not _NUMBER_RE.fullmatch(value):
        raise TargetError(f"invalid target {shown!r}: {value!r} is not a number; write {form}")
    number = float(value)
    if not math.isfinite(number):
        raise TargetError(f"invalid target {shown!r}: {value!r} is not a finite number; write {form}")
    return number


def _first_field(kind: TargetKind, shown: str) -> str:
    for name, info in kind.args.model_fields.items():
        return info.alias or name
    raise TargetError(
        f"target kind {kind.name!r} has no arguments, so {shown!r} cannot be parsed; write by.custom({kind.name!r})"
    )


def _parse_prefixed(kind: TargetKind, rest: str, text: str) -> Target:
    if not rest:
        raise TargetError(f"invalid target {text!r}: nothing after '{kind.shorthand}='; write {kind.shorthand}=VALUE")
    special = _special(kind)
    if special == "role":
        role, sep, name = rest.partition(":")
        if not role:
            raise TargetError(f"invalid target {text!r}: the role is empty; write role=ROLE or role=ROLE:NAME")
        if sep and not name:
            raise TargetError(f"invalid target {text!r}: the name after ':' is empty; write role={role}")
        raw: dict[str, Any] = {"role": role} if not sep else {"role": role, "name": name}
    elif special == "point":
        parts = rest.split(",")
        if len(parts) != 2:
            raise TargetError(f"invalid target {text!r}: a point needs two numbers; write point=X,Y")
        raw = {"x": _number(parts[0], text, "point=X,Y"), "y": _number(parts[1], text, "point=X,Y")}
    elif special == "rect":
        parts = rest.split(",")
        if len(parts) != 4:
            raise TargetError(f"invalid target {text!r}: a rect needs four numbers; write rect=X,Y,W,H")
        x, y, w, h = (_number(part, text, "rect=X,Y,W,H") for part in parts)
        raw = {"x": x, "y": y, "w": w, "h": h}
    else:
        raw = {_first_field(kind, text): rest}
    return _make(kind.name, _dump_args(kind, raw, repr(text)))


def parse_target(text: str, kinds: Mapping[str, TargetKind] | None = None) -> Target:
    if not isinstance(text, str):
        raise TypeError(f"parse_target() takes a string, got {type(text).__name__} {text!r}")
    known = _kinds(kinds)
    shorthands = _shorthands(known)
    if _ID_RE.fullmatch(text):
        return _make("test_id", _dump_args(known["test_id"], {"id": text[1:]}, repr(text)))
    match = _PREFIX_RE.fullmatch(text)
    if match:
        kind = shorthands.get(match.group(1))
        if kind is not None:
            return _parse_prefixed(kind, match.group(2), text)
    return _make("text", _dump_args(known["text"], {"text": text}, repr(text)))


def ensure_target(value: object, what: str, kinds: Mapping[str, TargetKind] | None = None) -> Target:
    if isinstance(value, Target):
        return validate_args(value, kinds)
    if isinstance(value, str):
        return parse_target(value, kinds)
    raise TypeError(
        f"{what} must be a Target or a target string, got {type(value).__name__} {value!r}; "
        "write a target like by.test_id('save'), by.text('Kaydet') or '#save'"
    )


def validate_args(target: Target, kinds: Mapping[str, TargetKind] | None = None) -> Target:
    known = _kinds(kinds)
    update: dict[str, Any] = {}
    kind = known.get(target.by)
    if kind is not None:
        args = _dump_args(kind, target.args, f"by={target.by} {_args_json(target.args)}")
        if _args_json(args) != _args_json(target.args):
            update["args"] = args
    if target.within_ is not None:
        scope = validate_args(target.within_, known)
        if scope is not target.within_:
            update["within_"] = scope
    if target.near_ is not None:
        anchor = validate_args(target.near_, known)
        if anchor is not target.near_:
            update["near_"] = anchor
    return target.model_copy(update=update) if update else target


def _check(target: Target, surface: str, known: Mapping[str, TargetKind], problems: list[str]) -> None:
    kind = known.get(target.by)
    shown = _describe(target, known)
    if kind is None:
        problems.append(
            f"target {shown}: unknown target kind {target.by!r}; known kinds: {', '.join(sorted(known))} "
            "(plugin kinds are registered with @eks.target_kind)"
        )
    elif surface not in kind.surfaces:
        problems.append(
            f"target {shown}: kind {target.by!r} is not supported on {surface}; "
            f"it works on {', '.join(sorted(kind.surfaces))}"
        )
    for modifier, value in (("within", target.within_), ("near", target.near_), ("nth", target.nth_)):
        if value is not None and surface not in MODIFIER_SURFACES[modifier]:
            problems.append(
                f"target {shown}: {modifier} is not supported on {surface}; "
                f"it works on {', '.join(sorted(MODIFIER_SURFACES[modifier]))}"
            )
    if target.surface_ == "native" and surface not in NATIVE_SURFACES:
        problems.append(
            f"target {shown}: the native surface (system dialogs outside the app) exists on "
            f"{', '.join(NATIVE_SURFACES)}, not on {surface}"
        )
    if target.within_ is not None:
        _check(target.within_, surface, known, problems)
    if target.near_ is not None:
        _check(target.near_, surface, known, problems)


def check_target(target: Target, surface: str, kinds: Mapping[str, TargetKind] | None = None) -> list[str]:
    if surface not in SURFACES:
        return [f"unknown target surface {surface!r}; surfaces are {', '.join(SURFACES)}"]
    problems: list[str] = []
    _check(target, surface, _kinds(kinds), problems)
    return problems


def _args_json(args: Mapping[str, JsonValue]) -> str:
    return json.dumps(args, ensure_ascii=False, sort_keys=True)


def _format_number(value: JsonValue) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return str(int(number)) if number.is_integer() else repr(number)


def _coords(args: Mapping[str, JsonValue], keys: tuple[str, ...]) -> str | None:
    coords = [_format_number(args.get(key)) for key in keys]
    return ",".join(coord for coord in coords if coord) if all(coords) else None


def _candidates(target: Target, kind: TargetKind) -> list[str]:
    prefix = kind.shorthand
    if prefix is None:
        return []
    args = target.args
    special = _special(kind)
    if special == "point" or special == "rect":
        coords = _coords(args, ("x", "y") if special == "point" else ("x", "y", "w", "h"))
        return [f"{prefix}={coords}"] if coords else []
    if special == "role":
        role, name = args.get("role"), args.get("name")
        if not isinstance(role, str) or ":" in role:
            return []
        if name is None:
            return [f"{prefix}={role}"]
        return [f"{prefix}={role}:{name}"] if isinstance(name, str) else []
    try:
        value = args.get(_first_field(kind, target.by))
    except TargetError:
        return []
    if not isinstance(value, str):
        return []
    found: list[str] = []
    if kind.name == "test_id" and kind.args is ByTestId:
        found.append(f"#{value}")
    if (
        kind.name == "text"
        and kind.args is ByText
        and value
        and not _PREFIX_RE.fullmatch(value)
        and not _ID_RE.fullmatch(value)
    ):
        found.append(value)
    found.append(f"{prefix}={value}")
    return found


def _base(target: Target, known: Mapping[str, TargetKind]) -> str:
    plain = target.model_copy(update={"within_": None, "near_": None, "nth_": None})
    kind = known.get(target.by)
    for candidate in [] if kind is None else _candidates(plain, kind):
        try:
            parsed = parse_target(candidate, known)
        except TargetError:
            continue
        if parsed == plain:
            return candidate
    return f"by={target.by} {_args_json(target.args)}"


def _nested(target: Target, known: Mapping[str, TargetKind]) -> str:
    text = _describe(target, known)
    has_modifiers = target.within_ is not None or target.near_ is not None or target.nth_ is not None
    return f"({text})" if has_modifiers else text


def _describe(target: Target, known: Mapping[str, TargetKind]) -> str:
    parts = [_base(target, known)]
    if target.within_ is not None:
        parts.append(f"within {_nested(target.within_, known)}")
    if target.near_ is not None:
        parts.append(f"near {_nested(target.near_, known)}")
    if target.nth_ is not None:
        parts.append(f"nth {target.nth_}")
    if target.surface_ is not None:
        parts.append(f"on the {target.surface_} surface")
    return " ".join(parts)


def describe(target: Target, kinds: Mapping[str, TargetKind] | None = None) -> str:
    return _describe(target, _kinds(kinds))


def _text_value(value: object) -> str | FmtExpr | RefExpr | ItemExpr | Mapping[str, Any]:
    if isinstance(value, str | FmtExpr | RefExpr | ItemExpr):
        return value
    if isinstance(value, Mapping):
        return cast("Mapping[str, Any]", value)
    return str(value)


def _json_value(value: object) -> JsonValue:
    if isinstance(value, float) and not math.isfinite(value):
        raise TargetError(f"invalid target argument {value!r}: target arguments must be finite numbers")
    if value is None or isinstance(value, _JSON_SCALARS):
        return cast("JsonValue", value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, Mapping):
        items = cast("Mapping[object, object]", value)
        return {str(key): _json_value(item) for key, item in items.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in cast("list[object] | tuple[object, ...]", value)]
    return str(value)


def _builtin(kind_name: str, raw: Mapping[str, Any]) -> Target:
    return _make(kind_name, _dump_args(BUILTIN_KINDS[kind_name], raw, f"by.{kind_name}(...)"))


class By:
    def test_id(self, id: str | Any) -> Target:
        return _builtin("test_id", {"id": _text_value(id)})

    def role(self, role: str, name: str | Any | None = None, *, exact: bool = False) -> Target:
        return _builtin("role", {"role": role, "name": None if name is None else _text_value(name), "exact": exact})

    def text(self, text: str | Any, *, exact: bool = True) -> Target:
        return _builtin("text", {"text": _text_value(text), "exact": exact})

    def label(self, text: str | Any, *, exact: bool = False) -> Target:
        return _builtin("label", {"text": _text_value(text), "exact": exact})

    def placeholder(self, text: str | Any, *, exact: bool = False) -> Target:
        return _builtin("placeholder", {"text": _text_value(text), "exact": exact})

    def css(self, selector: str | Any) -> Target:
        return _builtin("css", {"selector": _text_value(selector)})

    def kind(self, kind: NodeKind) -> Target:
        return _builtin("kind", {"kind": kind})

    def point(self, x: float, y: float) -> Target:
        return _builtin("point", {"x": x, "y": y})

    def rect(self, x: float, y: float, w: float, h: float) -> Target:
        return _builtin("rect", {"x": x, "y": y, "w": w, "h": h})

    def object(self, name: str) -> Target:
        return _builtin("object", {"name": name})

    def layer(self, name: str) -> Target:
        return _builtin("layer", {"name": name})

    def custom(self, kind: str, **args: Any) -> Target:
        return validate_args(_make(kind, {key: _json_value(value) for key, value in args.items()}))

    def parse(self, text: str, *, kinds: Mapping[str, TargetKind] | None = None) -> Target:
        return parse_target(text, kinds)


by = By()

__all__ = [
    "BUILTIN_KINDS",
    "KIND_PATTERN",
    "MODIFIER_SURFACES",
    "PLATFORMS",
    "SHORTHAND_PATTERN",
    "SURFACES",
    "By",
    "ByCss",
    "ByKind",
    "ByLabel",
    "ByLayer",
    "ByObject",
    "ByPlaceholder",
    "ByPoint",
    "ByRect",
    "ByRole",
    "ByTestId",
    "ByText",
    "KindsProvider",
    "Modifier",
    "NodeKind",
    "Platform",
    "Surface",
    "Target",
    "TargetError",
    "TargetKind",
    "by",
    "check_target",
    "describe",
    "ensure_target",
    "parse_target",
    "use_default_kinds",
    "validate_args",
]
