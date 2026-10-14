from __future__ import annotations

import ast
import builtins
import inspect
import textwrap
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from types import CodeType, ModuleType
from typing import Any, Literal, cast, get_args

from pydantic import BaseModel, JsonValue, ValidationError
from pydantic_core import ErrorDetails

from eks_harness.ir import SCHEMA_ID
from eks_harness.ir.catalog.durations import NEGATIVE_ERROR, length_bound
from eks_harness.ir.catalog.markers import (
    CLOCK_STREAMS,
    BeatTracker,
    MarkerBase,
    STTMarkers,
)
from eks_harness.ir.contributions import JsonDecoder
from eks_harness.ir.hashing import (
    EACH_OP,
    binding_needs,
    canonical_items,
    clock_dependencies,
    hash_plan,
    node_hash,
    plan_hash,
    resource_hash,
    resource_id,
    scene_set_values,
    stored_clock_payload,
)
from eks_harness.ir.ops.audio import AudioTrack
from eks_harness.ir.ops.blender import (
    BLENDER_FIXED_FIELDS,
    BLENDER_REFERENCE_FIELDS,
    BlenderScene,
    CameraNode,
    Engine,
    NodeBase,
)
from eks_harness.ir.ops.blender import Pulse as BlenderPulse
from eks_harness.ir.ops.blender import SetProp as BlenderSetProp
from eks_harness.ir.ops.check import (
    CheckUse,
    MediaCheck,
    assertion_problem,
    literal_type,
)
from eks_harness.ir.ops.ctl import (
    ALLOWED_DYNAMIC_GLOBALS,
    BPY_MODULES,
    DATA_BINDING_KINDS,
    SANDBOX_BUILTINS,
    SANDBOX_EKS_ATTRIBUTES,
    SandboxScan,
    sandbox_leaks,
    scan_sandbox_source,
)
from eks_harness.ir.ops.data import keyless_position, resource_path, resource_segment
from eks_harness.ir.ops.edit import Clip, Timeline
from eks_harness.ir.ops.gfx import (
    GFX_FIXED_FIELDS,
    Animate,
    GfxScene,
    GroupLayer,
    LayerBase,
    SetProp,
    gfx_layer_index,
)
from eks_harness.ir.ops.gfx import Pulse as GfxPulse
from eks_harness.ir.ops.media import Markers
from eks_harness.ir.ops.overlay import OverlayBase
from eks_harness.ir.ops.scene import (
    SCENE_RENDER_OPS,
    EventRef,
    PropUse,
    WebScene,
    event_source_problem,
    find_cycle,
    keyframe_problem,
    prop_problem,
    repeated_ids,
    typed_value,
)
from eks_harness.ir.ops.score import Take
from eks_harness.ir.plan import (
    Binding,
    BindingKind,
    Node,
    Plan,
    PlanError,
    Resource,
    SessionSpec,
)
from eks_harness.ir.refs import (
    FilterStep,
    FindStep,
    FmtExpr,
    ItemExpr,
    RefError,
    RefExpr,
    is_expr_dict,
    parse_expr,
    ref_kind_of,
    walk_exprs,
)
from eks_harness.ir.registry import (
    CheckSpec,
    MacroSpec,
    OpSpec,
    Registry,
    RegistryError,
    default_registry,
    expected_out,
    out_problem,
    using_registry,
)
from eks_harness.ir.target import Target, TargetKind, check_target
from eks_harness.ir.time import (
    Span,
    TempoMap,
    Time,
    TimeContext,
    TimeError,
    TimeNeeds,
    TimeSet,
    field_needs,
    format_time,
)
from eks_harness.ir.types import RefTypeError, compatible, infer_path, parse_ref_type

Severity = Literal["error", "warning"]
PersonaLookup = Callable[[str], Sequence[str] | None]
AppLookup = Callable[[str], Sequence[str] | None]
BackendNames = Collection[str]


@dataclass(frozen=True, slots=True)
class SeedInfo:
    name: str
    backend: str
    now: str | None
    hash: str
    personas: tuple[str, ...] = ()
    bases: tuple[str, ...] = ()
    resources: tuple[tuple[str, str], ...] = ()


SeedLookup = Callable[[str], SeedInfo | None]

ISSUE_CODES: Mapping[str, str] = {
    "plan.schema": "the plan's schema id is not eks/plan@1",
    "plan.hash": "the stored plan or node hash differs from the recomputed one",
    "op.unknown": "the op is not registered",
    "op.version": "the node was built with another op version than the registry has",
    "op.out": "the node's out is not its op's output type (resource:KIND for data.create); an op producing untyped json "
              "may be narrowed",
    "op.platform": "the op does not run on the session platform",
    "op.resources": "the op needs a lease or backend the plan does not declare",
    "op.recording_only": "hold or another recording-only op outside a recording or take",
    "args.invalid": "the args do not validate against the op's argument model",
    "node.duplicate": "two nodes share an id",
    "node.after": "an 'after' edge points to an unknown, later, enclosing or invisible node, or a node does not wait "
                  "for a node it reads ($ref producer or clock provider) or for the node before it on its lane",
    "node.cycle": "the 'after' edges form a cycle",
    "node.branches": "a container has unknown branches, or a plain op has branches",
    "resource.unknown": "a node uses an undeclared resource id",
    "resource.kind": "a resource has an unknown kind",
    "ref.unknown": "a $ref points to no node",
    "ref.not_visible": "a $ref points to a node that is not visible here (later, or inside a when/each/dynamic body)",
    "ref.item_scope": "an $item is used outside its eks.each body",
    "ref.path": "a ref expression is malformed, or its path does not fit the producer's output type",
    "ref.type": "a ref's type is not compatible with the field it is used in",
    "target.kind": "the target kind is not registered",
    "target.surface": "the target kind or modifier is not supported on this platform or surface",
    "target.args": "the target args do not validate",
    "time.unresolvable": "a time needs clock information the plan does not have (fps, tempo, cue, marker, words, window)",
    "markers.duplicate": "a marker source or stream name is provided twice in the plan, or is a clock tempo stream name",
    "time.negative": "a duration resolves to a negative value",
    "pin.outside_take": "at= is only allowed inside a take",
    "pin.order": "pinned times do not increase along a path of program order",
    "pin.op": "the op cannot be pinned with at=",
    "pin.span": "a pin lies outside its take span, the score window or the clock duration, or before 0",
    "pin.loop": "at= inside a runtime eks.each body or an eks.dynamic function, directly, through pin= or a ** "
                "splat, or inside a macro or function the dynamic function calls",
    "take.span": "a take span reaches outside the score window or the clock duration, or starts before 0",
    "take.placement": "a score.take sits inside a container instead of at the top level of its score",
    "persona.seed": "the flow names a seed that is not registered",
    "persona.unknown": "the persona is not an output of the seed",
    "plugin.missing": "the plan requires a plugin that is not loaded",
    "plugin.version": "the plan requires another version of a plugin than the loaded one",
    "control.else": "an else branch without its when",
    "control.empty": "a control body is empty",
    "dynamic.binding": "an eks.dynamic function or @sc.bpy hook uses a closure, global, builtin, import, dunder name, "
                       "private, internal or introspection attribute, eks member or runtime format string that "
                       "cannot be rebuilt in or would leave the sandbox, or formats or hashes a value that may hold "
                       "an object address",
    "check.kind": "the check kind is not registered",
    "check.args": "the check params do not validate",
    "check.platform": "the check does not run on this platform",
    "overlay.renderer": "the overlay kind is not supported by this renderer or platform",
    "preset.unknown": "a viewport, pace, network or style preset is not registered",
    "record.nested": "a recording inside a recording",
    "blender.render": "the Blender engine, samples or device are not explicit after alias resolution",
    "scene.ref": "a scene graph references an unknown node or layer id",
    "scene.duplicate": "two Blender nodes or two gfx layers (group children included) share an id",
    "scene.cycle": "Blender parents, or gfx masks and group children, form a cycle",
    "scene.event": "an event trigger listens to a node that emits no events, or to an event its scene never declares",
    "scene.length": "an embedded scene sets no duration while the plan clock is open (no window and no duration)",
    "scene.prop": "a scene action or animation names a property that is fixed, unknown, not animatable or (for a "
                  "pulse) not an animatable number",
    "scene.value": "a scene set action or keyframe value does not fit the property's type, or a keyframe holds a ref",
    "scene.camera": "the scene camera is not a camera node, or a camera tracks or focuses on itself",
    "clock.now": "the frozen wall clock (now) of a flow, take or seed is not set",
    "seed.unknown": "data.extend names a seed that is not registered",
    "seed.backend": "a seed, flow or take uses another backend than the seed it builds on",
    "seed.now": "a seed extends a base seed with another frozen now",
    "seed.stale": "data.extend carries an older plan hash of its base seed",
    "seed.cycle": "seeds extend each other in a cycle",
    "seed.path": "a data.create or data.base path does not follow from its kind, key and parent, repeats another path "
                 "of the plan, or the node sits inside a container instead of at the seed root",
    "seed.persona": "a persona key is declared twice, or the plan's personas differ from the persona keys its "
                    "data.create nodes declare",
    "session.duplicate": "two sessions of the plan (the flow session and the take sessions) share a key",
    "session.resources": "a session has more than one lease or more than one backend resource, or a plan with "
                         "sessions declares a lease or backend that belongs to none",
    "resource.id": "a resource id is not the id derived from the resource's kind and identity",
    "seed.resource": "a seed declares again a resource path or persona key that the seeds it extends already create, "
                     "or looks up (data.base) a resource they do not create",
    "resource.app": "a lease or backend names an app without a .harness/app.toml, or a platform it does not list",
    "resource.kind_unknown": "data.create names a resource kind that is not registered",
    "resource.fields": "the fields or key of a data.create do not fit the resource kind's model",
    "resource.parent": "a data.create has no parent, an unexpected parent or a parent of another kind than the resource kind's",
    "resource.phase": "the phase of a data.create differs from the resource kind's phase",
    "session.mismatch": "a resource of a session names another platform, app, seed or backend than the session spec",
    "session.uses": "a node uses a lease or backend of another session than the one governing it, or one that "
                    "belongs to no session",
    "resource.params": "a lease or backend resource carries params; the engine acquires leases and backends itself",
    "macro.unknown": "ctl.macro names a macro that is not registered",
    "macro.version": "ctl.macro was traced with another macro version than the registry has",
    "macro.params": "the params of ctl.macro do not bind to the macro's signature",
    "macro.platform": "the macro does not run on the session platform",
    "time.reversed": "a span whose ends resolve without running ends before it starts",
    "resource.backend": "a backend resource names a backend definition that no loaded plugin or project registers",
    "renderer.unknown": "a media.source names a renderer that no loaded plugin or project registers",
    "cassette.nested": "a network cassette inside another cassette",
    "evidence.duplicate": "two checkpoints (per viewport), shots, recordings, HARs, dumps, cassettes or breakpoints of a "
                          "session share a name",
    "clock.zone": "a session with a frozen now has no timezone or locale, so dates render in the host's",
    "expect.eventually": "expect.value with eventually=true re-reads a value whose producer is not an idempotent op, or "
                         "an eks.each item, a formatted string or a literal",
}

RESOURCE_KINDS = ("backend", "lease.android", "lease.ios", "lease.web")
EXTEND_OP = "data.extend"
CREATE_OP = "data.create"
BASE_OP = "data.base"
MACRO_OP = "ctl.macro"
LEASE_PREFIX = "lease."
WHEN_OP = "ctl.when"
DYNAMIC_OP = "ctl.dynamic"
PARALLEL_OP = "ctl.parallel"
SINGLE_LANE = ""
FREE_LANE = "*"
RECORD_OP = "capture.record"
TAKE_OP = "score.take"
MACRO_METHOD = "macro"
PIN_KEYWORDS = ("at", "pin")
LIBRARY_PACKAGES = ("eks", "eks_harness")
CASSETTE_OP = "net.cassette"
EXPECT_VALUE_OP = "expect.value"
CHECKPOINT_OP = "check.checkpoint"
VISUAL_FAMILY = "visual"
EVIDENCE_NAMES: Mapping[str, tuple[str, str]] = {
    CHECKPOINT_OP: ("checkpoint", "label"),
    "capture.shot": ("shot", "name"),
    RECORD_OP: ("recording", "name"),
    "capture.har": ("HAR", "name"),
    "capture.dump": ("dump", "name"),
    CASSETTE_OP: ("cassette", "name"),
    "debug.break": ("breakpoint", "label"),
}
DEFAULT_EVIDENCE_NAMES: Mapping[str, str] = {"capture.har": "har", "capture.dump": "kind"}
LOOP_NODES = (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
DYNAMIC_EVIDENCE: Mapping[str, tuple[str, str, bool]] = {
    "checkpoint": ("checkpoint", "label", True),
    "shot": ("shot", "name", True),
    "record": ("recording", "name", True),
    "har": ("HAR", "name", False),
    "dump": ("dump", "name", False),
    "cassette": ("cassette", "name", True),
    "break_": ("breakpoint", "label", True),
}
AS_OP = "session.as"
PERSONA_FIELD = "persona"
MARKERS_OP = "media.markers"
SOURCE_OP = "media.source"
CHECK_MEDIA_OP = "check.media"
BLENDER_OP = "blender.render"
FMT_TYPES = frozenset({"str", "int", "float", "bool", "json"})
BEAT_STREAMS = frozenset({"beat", "downbeat", "kick", "snare"})
TEMPO_STREAM = "beat"
SCENE_KIND = "scene"
CHECKPOINT_FAMILIES = ("checkpoint", "visual", "perf")
MEDIA_FAMILIES = ("media",)
REQUIRED_BRANCHES: Mapping[str, str] = {WHEN_OP: "then"}
EMPTY_WARNED_BRANCHES = ("then", "body")
SURFACE_BY_OP: Mapping[str, str] = {
    "overlay.annotate": "screenshot",
    "edit.render": "video",
    CHECK_MEDIA_OP: "video",
    BLENDER_OP: "blender",
    "gfx.render": "gfx",
    "scene.render": "scene",
}
OVERLAY_RENDERER_BY_OP: Mapping[str, str] = {
    "overlay.show": "live",
    "overlay.annotate": "screenshot",
    "edit.render": "video",
}
PRESET_FIELDS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "capture.shot": (("viewport", "viewport"),),
    "capture.dump": (("viewport", "viewport"),),
    RECORD_OP: (("viewport", "viewport"), ("pace", "pace")),
    "check.checkpoint": (("viewport", "viewport"),),
    "web.viewport": (("name", "viewport"),),
    "ui.type": (("pace", "pace"),),
    "net.shape": (("profile", "network"),),
    "overlay.annotate": (("style", "style"),),
}
SESSION_PRESETS = (("viewport", "viewport"), ("pace", "pace"))
ENGINES: tuple[str, ...] = get_args(Engine)
DEVICES = ("CPU", "GPU")
BINDING_KINDS: tuple[str, ...] = get_args(BindingKind)
PLAN_LEVEL = -1
VARIADIC = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
BUILTIN_NAMES = frozenset(vars(builtins))

FieldPath = tuple[str | int, ...]
Expr = RefExpr | ItemExpr


@dataclass(frozen=True, slots=True)
class Issue:
    severity: Severity
    code: str
    message: str
    node: str | None = None
    field: str | None = None
    src: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "node": self.node,
            "field": self.field,
            "src": self.src,
        }

    def text(self) -> str:
        head = " ".join(part for part in (self.severity.upper(), self.code, self.node, self.src) if part)
        where = f"{self.field}: " if self.field else ""
        return f"{head}: {where}{self.message}"


@dataclass(frozen=True, slots=True)
class Report:
    plan_hash: str
    issues: tuple[Issue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "hash": self.plan_hash, "issues": [issue.as_dict() for issue in self.issues]}

    def text(self) -> str:
        return "\n".join(issue.text() for issue in self.issues)


@dataclass(frozen=True, slots=True)
class _Frame:
    container: Node
    branch: str


@dataclass(frozen=True, slots=True)
class _Visit:
    node: Node
    index: int
    frames: tuple[_Frame, ...]
    session: SessionSpec | None
    session_key: str
    take: Node | None


@dataclass(frozen=True, slots=True)
class _TimeScope:
    fps: bool
    window: bool
    local: bool


@dataclass(frozen=True, slots=True)
class _Pinned:
    ticks: int
    node: Node
    pin: Time


@dataclass(frozen=True, slots=True)
class _Bounds:
    low: int
    high: int | None
    what: str

    @property
    def ticks(self) -> str:
        return f"{self.low}..{'' if self.high is None else self.high} ticks"


@dataclass(frozen=True, slots=True)
class _Evidence:
    visit: _Visit
    what: str
    name: str
    viewport: str | None
    visual: bool


@dataclass(frozen=True, slots=True)
class _Found:
    position: int
    code: str
    order: int
    issue: Issue


def _dotted(path: FieldPath) -> str | None:
    return ".".join(str(step) for step in path) if path else None


def _shown_ids(ids: Sequence[str]) -> str:
    return ", ".join(repr(value) for value in ids) if ids else "none"


def _validation_summary(error: ValidationError, limit: int = 3) -> str:
    return _summary(error.errors(), error, limit)


def _summary(items: Sequence[ErrorDetails], error: ValidationError, limit: int = 3) -> str:
    del error
    parts: list[str] = []
    for item in items[:limit]:
        loc = ".".join(str(step) for step in item["loc"])
        parts.append(f"{loc}: {item['msg']}" if loc else str(item["msg"]))
    extra = len(items) - limit
    if extra > 0:
        parts.append(f"and {extra} more")
    return "; ".join(parts)


def _shown_input(value: object) -> str:
    if isinstance(value, Time):
        return repr(format_time(value))
    return repr(value)


def _shown_time(value: Time | Span | TimeSet) -> str:
    if isinstance(value, Time):
        return repr(format_time(value))
    if isinstance(value, Span):
        return f"span {format_time(value.start)!r}..{format_time(value.end)!r}"
    if value.stream is not None:
        return f"time set over stream {value.stream!r}"
    return f"time set {[format_time(item) for item in value.times]!r}"


def _pin_keyword(call: ast.Call) -> str | None:
    for keyword in call.keywords:
        if keyword.arg in PIN_KEYWORDS:
            return f"pins a step with {keyword.arg}="
        if keyword.arg is not None:
            continue
        if not isinstance(keyword.value, ast.Dict):
            return "passes keyword arguments through ** from a value that may carry at="
        for key in keyword.value.keys:
            if key is None or not isinstance(key, ast.Constant) or key.value in PIN_KEYWORDS:
                return "passes at= or pin= through a ** mapping"
    return None


type _CallPath = tuple[str, ...]
type _Callable = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


class _CallPaths:
    def __init__(self, tree: ast.AST, roots: Mapping[str, _CallPath]) -> None:
        self.roots = roots
        self.bindings: dict[str, list[ast.expr]] = {}
        self.attributes: dict[str, list[ast.expr]] = {}
        self.functions: dict[str, list[_Callable]] = {}
        self.params: set[str] = set()
        self.arguments: list[ast.expr] = []
        for found in ast.walk(tree):
            if isinstance(found, ast.Assign):
                for target in found.targets:
                    self.bind(target, found.value)
            elif isinstance(found, ast.AnnAssign | ast.AugAssign | ast.NamedExpr) and found.value is not None:
                self.bind(found.target, found.value)
            elif isinstance(found, ast.For | ast.AsyncFor | ast.comprehension):
                self.bind(found.target, found.iter)
            elif isinstance(found, ast.With | ast.AsyncWith):
                for item in found.items:
                    if item.optional_vars is not None:
                        self.bind(item.optional_vars, item.context_expr)
            elif isinstance(found, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                self.function(found)
            elif isinstance(found, ast.Call):
                self.arguments.extend(found.args)
                self.arguments.extend(keyword.value for keyword in found.keywords)

    def function(self, found: _Callable) -> None:
        if not isinstance(found, ast.Lambda):
            self.functions.setdefault(found.name, []).append(found)
        arguments = found.args
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg,
                         arguments.kwarg):
            if argument is not None and argument.arg not in self.roots:
                self.params.add(argument.arg)
        self.arguments.extend(arguments.defaults)
        self.arguments.extend(default for default in arguments.kw_defaults if default is not None)

    def bind(self, target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, ast.Attribute):
            self.attributes.setdefault(target.attr, []).append(value)
            return
        for found in ast.walk(target):
            if isinstance(found, ast.Name):
                self.bindings.setdefault(found.id, []).append(value)

    def paths(self, expr: ast.AST, seen: frozenset[str] = frozenset()) -> set[_CallPath]:
        if isinstance(expr, ast.Name):
            return self.name_paths(expr.id, seen)
        if isinstance(expr, ast.Attribute):
            found = {(*path, expr.attr) for path in self.paths(expr.value, seen)}
            key = "." + expr.attr
            if key not in seen:
                for value in self.attributes.get(expr.attr, ()):
                    found |= self.paths(value, seen | {key})
            return found
        if isinstance(expr, ast.Call):
            return self.call_paths(expr, seen)
        if isinstance(expr, ast.Lambda):
            return self.paths(expr.body, seen)
        if isinstance(expr, ast.ListComp | ast.SetComp | ast.GeneratorExp):
            return self.paths(expr.elt, seen)
        if isinstance(expr, ast.DictComp):
            return self.paths(expr.value, seen)
        found: set[_CallPath] = set()
        if isinstance(expr, ast.expr) and not isinstance(expr, ast.Compare | ast.Constant | ast.JoinedStr):
            for child in ast.iter_child_nodes(expr):
                found |= self.paths(child, seen)
        return found

    def name_paths(self, name: str, seen: frozenset[str]) -> set[_CallPath]:
        found: set[_CallPath] = set()
        root = self.roots.get(name)
        if root is not None:
            found.add(root)
        if name in seen:
            return found
        inner = seen | {name}
        for value in self.bindings.get(name, ()):
            found |= self.paths(value, inner)
        if name in self.params:
            for argument in self.arguments:
                found |= self.paths(argument, inner)
        return found

    def call_paths(self, call: ast.Call, seen: frozenset[str]) -> set[_CallPath]:
        found = self.paths(call.func, seen)
        for argument in (*call.args, *(keyword.value for keyword in call.keywords)):
            found |= self.paths(argument, seen)
        if isinstance(call.func, ast.Name) and call.func.id not in seen:
            inner = seen | {call.func.id}
            for function in self.functions.get(call.func.id, ()):
                for returned in ast.walk(function):
                    if isinstance(returned, ast.Return | ast.Yield | ast.YieldFrom) and returned.value is not None:
                        found |= self.paths(returned.value, inner)
        return found


def _root_params(tree: ast.AST) -> dict[str, _CallPath]:
    body = tree.body if isinstance(tree, ast.Module) else []
    function = next((item for item in body if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)), None)
    if function is None:
        return {}
    arguments = function.args
    return {argument.arg: () for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                                              arguments.vararg, arguments.kwarg) if argument is not None}


def _session_role(resource: Resource) -> str | None:
    if resource.kind.startswith(LEASE_PREFIX):
        return "lease"
    return "backend" if resource.kind == "backend" else None


def _ref_problem(value: JsonValue) -> str | None:
    if any(True for _expr in walk_exprs(value)):
        return "it holds a ref; keyframes and set values hold values known when the plan is built"
    return None


def _data_names(bindings: Mapping[str, Any]) -> list[str]:
    return [name for name, binding in bindings.items()
            if isinstance(binding, dict) and cast("dict[str, Any]", binding).get("kind") in DATA_BINDING_KINDS]


def _function_tree(fn: Callable[..., Any]) -> ast.Module | None:
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return None


def _library(value: object) -> bool:
    module = getattr(value, "__module__", None)
    return isinstance(module, str) and any(module == package or module.startswith(package + ".")
                                           for package in LIBRARY_PACKAGES)


def _called_function(func: ast.expr, scope: Mapping[str, Any]) -> Callable[..., Any] | None:
    found: object = None
    if isinstance(func, ast.Name):
        found = scope.get(func.id)
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        module = scope.get(func.value.id)
        found = getattr(module, func.attr, None) if isinstance(module, ModuleType) else None
    if inspect.isfunction(found) and not _library(found):
        return cast("Callable[..., Any]", found)
    return None


def _expr_parts(value: RefExpr | ItemExpr | FmtExpr) -> list[Expr]:
    if isinstance(value, FmtExpr):
        return [part for part in value.fmt if not isinstance(part, str)]
    return [value]


def _calls_in_loops(node: ast.AST, looped: bool) -> Iterator[tuple[ast.Call, bool]]:
    for child in ast.iter_child_nodes(node):
        inner = looped or isinstance(child, LOOP_NODES)
        if isinstance(child, ast.Call):
            yield child, looped
        yield from _calls_in_loops(child, inner)


def _dynamic_evidence_name(call: ast.Call) -> tuple[str, str] | None:
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in DYNAMIC_EVIDENCE:
        return None
    what, keyword_name, positional = DYNAMIC_EVIDENCE[func.attr]
    value = next((keyword.value for keyword in call.keywords if keyword.arg == keyword_name), None)
    if value is None and positional and call.args:
        value = call.args[0]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return what, value.value
    return None


def _strongly_connected(graph: Mapping[str, Sequence[str]]) -> list[list[str]]:
    order: list[str] = []
    seen: set[str] = set()
    for start in graph:
        if start in seen:
            continue
        seen.add(start)
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(graph[start]))]
        while stack:
            vertex, edges = stack[-1]
            following = next(edges, None)
            if following is None:
                stack.pop()
                order.append(vertex)
            elif following not in seen:
                seen.add(following)
                stack.append((following, iter(graph[following])))
    reverse: dict[str, list[str]] = {vertex: [] for vertex in graph}
    for vertex, edges in graph.items():
        for following in edges:
            reverse[following].append(vertex)
    components: list[list[str]] = []
    assigned: set[str] = set()
    for start in reversed(order):
        if start in assigned:
            continue
        assigned.add(start)
        component = [start]
        pending = [start]
        while pending:
            vertex = pending.pop()
            for previous in reverse[vertex]:
                if previous not in assigned:
                    assigned.add(previous)
                    component.append(previous)
                    pending.append(previous)
        components.append(component)
    return components


class _SeedLookupError(Exception):
    pass


def _seed_lookup(personas: PersonaLookup, seed: str) -> Sequence[str] | None:
    try:
        return personas(seed)
    except Exception as error:
        raise _SeedLookupError(f"{type(error).__name__}: {error}") from error


def _seed_info(seeds: SeedLookup, name: str) -> SeedInfo | None:
    try:
        return seeds(name)
    except Exception as error:
        raise _SeedLookupError(f"{type(error).__name__}: {error}") from error


def macro_binding_problems(fn: Callable[..., Any], params: Mapping[str, Any]) -> list[str]:
    try:
        parameters = list(inspect.signature(fn).parameters.values())[1:]
    except (TypeError, ValueError) as error:
        return [f"its signature cannot be read ({error})"]
    names = {parameter.name: parameter for parameter in parameters}
    problems = [f"parameter {name!r} is not in the signature ({_shown_ids(list(names))})"
                for name in params if name not in names]
    for parameter in parameters:
        if parameter.name not in params:
            if parameter.default is inspect.Parameter.empty and parameter.kind not in VARIADIC:
                problems.append(f"required parameter {parameter.name!r} is missing")
            continue
        value = params[parameter.name]
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL and not isinstance(value, list):
            problems.append(f"*{parameter.name} must be a list, got {_shown_input(value)}")
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD and not isinstance(value, dict):
            problems.append(f"**{parameter.name} must be an object, got {_shown_input(value)}")
    return problems


class _Run:
    def __init__(self, validator: Validator, plan: Plan) -> None:
        self.registry = validator.registry
        self.personas = validator.personas
        self.seeds = validator.seeds
        self.apps = validator.apps
        self.backends = validator.backends
        self.static_cache: list[TimeContext | None] = []
        self.pin_cache: list[TimeContext | None] = []
        self.seed_cache: dict[str, SeedInfo | None] = {}
        self.app_cache: dict[str, Sequence[str] | None] = {}
        self.versions = validator.versions
        self.plan = plan
        self.kinds: dict[str, TargetKind] = self.registry.target_kinds()
        self.found: list[_Found] = []
        self.visits: list[_Visit] = []
        self.by_id: dict[str, _Visit] = {}
        self.visit_of: dict[int, _Visit] = {}
        self.resources = {resource.id: resource for resource in plan.resources}
        self.decoder = JsonDecoder(self.registry)
        self.models: dict[int, BaseModel | None] = {}
        self.item_types: dict[str, str | None] = {}
        self.persona_cache: dict[str, Sequence[str] | None] = {}
        self.seed_errors: dict[str, str] = {}
        self.as_personas: dict[str, set[str]] = {}
        self.streams: set[str] = set()
        self.word_sources: set[str] = set()
        self.tempo_streams: set[str] = set()
        self.tempo_trackers: list[BeatTracker] = []
        self.current: _Visit | None = None
        self.surface: str | None = None
        self.platform: str | None = None
        self.repeated_takes: set[str] = set()

    def add(self, code: str, message: str, *, visit: _Visit | None = None, path: FieldPath = (),
            severity: Severity = "error") -> None:
        node = visit.node if visit is not None else None
        issue = Issue(severity=severity, code=code, message=message, node=node.id if node is not None else None,
                      field=_dotted(path), src=node.src if node is not None else None)
        position = visit.index if visit is not None else PLAN_LEVEL
        self.found.append(_Found(position, code, len(self.found), issue))

    def run(self) -> Report:
        self.collect()
        self.check_plan()
        for visit in self.visits:
            self.check_node(visit)
        self.current = None
        self.check_outputs()
        self.check_evidence()
        self.check_cycles()
        self.check_dependencies()
        self.check_seed_resources()
        self.check_seed_paths()
        self.check_pin_order()
        self.check_hashes()
        ordered = sorted(self.found, key=lambda found: (found.position, found.code, found.order))
        return Report(plan_hash=self.plan.hash, issues=tuple(found.issue for found in ordered))

    def spec(self, node: Node) -> OpSpec | None:
        return self.registry.op(node.op) if self.registry.has_op(node.op) else None

    def validated(self, node: Node) -> BaseModel | None:
        key = id(node)
        if key not in self.models:
            spec = self.spec(node)
            try:
                self.models[key] = spec.args.model_validate(node.args) if spec is not None else None
            except ValidationError:
                self.models[key] = None
        return self.models[key]

    def collect(self) -> None:
        self.walk(self.plan.nodes, (), self.plan.session, "", None)
        claimed: dict[str, str] = {}
        for visit in self.visits:
            node = visit.node
            if node.op == AS_OP:
                persona = node.args.get("persona")
                if isinstance(persona, str):
                    self.as_personas.setdefault(visit.session_key, set()).add(persona)
            if node.op == MARKERS_OP:
                model = self.validated(node)
                if isinstance(model, Markers):
                    for index, source in enumerate(model.sources):
                        self.claim_marker(source, visit, index, claimed)
                        self.streams.update(source.stream_names())
                        if isinstance(source, STTMarkers):
                            self.word_sources.add(source.name)
        tempo = self.plan.clock.tempo
        if tempo is not None and tempo.source is not None:
            visit = self.by_id.get(tempo.source)
            model = self.validated(visit.node) if visit is not None and visit.node.op == MARKERS_OP else None
            if isinstance(model, Markers):
                self.tempo_trackers = [source for source in model.sources
                                       if isinstance(source, BeatTracker) and TEMPO_STREAM in source.streams]
                if len(self.tempo_trackers) == 1:
                    self.tempo_streams = self.beat_streams(self.tempo_trackers[0])

    def claim_marker(self, source: MarkerBase, visit: _Visit, index: int, claimed: dict[str, str]) -> None:
        owner = f"{source.kind} {source.name!r} of node {visit.node.id}"
        for name in dict.fromkeys((source.name, *source.stream_names())):
            if name in CLOCK_STREAMS:
                self.add("markers.duplicate", f"{owner} provides the stream {name!r}, which is reserved for the clock "
                         f"tempo ({', '.join(sorted(CLOCK_STREAMS))}); name the analysis after its media",
                         visit=visit, path=("sources", index, "name"))
                continue
            earlier = claimed.get(name)
            if earlier is None:
                claimed[name] = owner
                continue
            self.add("markers.duplicate", f"{owner} provides {name!r}, which {earlier} already provides; marker "
                     "source and stream names are unique in a plan, so give each analysis its own name",
                     visit=visit, path=("sources", index, "name"))

    @staticmethod
    def beat_streams(source: MarkerBase) -> set[str]:
        prefix = source.name + "."
        return {name.removeprefix(prefix) for name in source.stream_names()
                if name.startswith(prefix) and name.removeprefix(prefix) in BEAT_STREAMS}

    def walk(self, nodes: Sequence[Node], frames: tuple[_Frame, ...], session: SessionSpec | None, key: str,
             take: Node | None) -> None:
        for node in nodes:
            visit = _Visit(node, len(self.visits), frames, session, key, take)
            self.visits.append(visit)
            self.visit_of[id(node)] = visit
            if node.id in self.by_id:
                self.add("node.duplicate", f"node id {node.id!r} ({node.op}) appears more than once in the plan",
                         visit=visit)
            else:
                self.by_id[node.id] = visit
            if node.branches is None:
                continue
            inner_session, inner_key, inner_take = session, key, take
            if node.op == TAKE_OP:
                inner_session, inner_key, inner_take = self.take_session(node), node.id, node
            for branch, children in node.branches.items():
                self.walk(children, (*frames, _Frame(node, branch)), inner_session, inner_key, inner_take)

    @staticmethod
    def take_session(node: Node) -> SessionSpec | None:
        try:
            return SessionSpec.model_validate(node.args.get("session"))
        except ValidationError:
            return None

    def check_plan(self) -> None:
        plan = self.plan
        if plan.schema_ != SCHEMA_ID:
            self.add("plan.schema", f"plan schema is {plan.schema_!r}; this validator reads {SCHEMA_ID!r}")
        seen: set[str] = set()
        for resource in plan.resources:
            if resource.id in seen:
                self.add("node.duplicate", f"resource id {resource.id!r} ({resource.kind}) is declared more than once")
            seen.add(resource.id)
            if resource.kind not in RESOURCE_KINDS:
                self.add("resource.kind", f"resource {resource.id!r} has kind {resource.kind!r}; "
                         f"resource kinds are {', '.join(RESOURCE_KINDS)}")
        for requirement in plan.requires:
            loaded = self.versions.get(requirement.plugin)
            if loaded is None:
                self.add("plugin.missing", f"the plan needs plugin {requirement.plugin!r} {requirement.version}, "
                         "which is not loaded; trust and enable it in the project or install it")
            elif loaded != requirement.version:
                self.add("plugin.version", f"the plan was built with plugin {requirement.plugin!r} "
                         f"{requirement.version} but {loaded} is loaded; plan it again or load that version")
        self.check_clock()
        self.check_now()
        self.check_apps()
        self.check_backends()
        self.check_session_resources()
        if plan.session is not None:
            if plan.session.timeout is not None:
                self.negative(plan.session.timeout, "session timeout", None, ())
            self.check_session(plan.session, "", None)

    def check_clock(self) -> None:
        clock = self.plan.clock
        scope = self.root_scope()
        if clock.duration is not None:
            self.negative(clock.duration, "clock duration", None, ())
            self.time(clock.duration, scope, None, (), "clock duration")
        if clock.window is not None:
            self.time(clock.window, scope, None, (), "clock window")
        for name, value in sorted(clock.cues.items()):
            if value.needs().cues:
                self.add("time.unresolvable", f"clock cue {name!r} is {format_time(value)!r}, which refers to "
                         "another cue; cues may not reference other cues, give each one a time on the clock")
                continue
            self.time(value, scope, None, (), f"clock cue {name!r}")
        tempo = clock.tempo
        if tempo is not None and tempo.external and self.plan.kind != SCENE_KIND:
            self.add("time.unresolvable", f"the clock tempo is external, which only a scene plan may declare (a scene "
                     f"resolves its musical times on the clock that embeds it); a {self.plan.kind} plan needs a bpm or "
                     "an analysed song", path=("clock", "tempo", "external"))
        if tempo is not None and tempo.source is not None:
            visit = self.by_id.get(tempo.source)
            if visit is None or visit.node.op != MARKERS_OP:
                self.add("time.unresolvable", f"the clock tempo source {tempo.source!r} is not a media.markers "
                         "node of this plan; analyse the song with .beats(...)")
            elif isinstance(self.validated(visit.node), Markers) and len(self.tempo_trackers) != 1:
                found = ", ".join(repr(tracker.name) for tracker in self.tempo_trackers)
                self.add("time.unresolvable", f"the clock tempo source {tempo.source!r} needs exactly one "
                         f"beat_tracker with the {TEMPO_STREAM!r} stream to give the beat grid, but it has "
                         f"{len(self.tempo_trackers)}{f' ({found})' if found else ''}; analyse the song with "
                         ".beats('beat', ...)", path=("clock", "tempo", "source"))

    def root_scope(self) -> _TimeScope:
        clock = self.plan.clock
        return _TimeScope(fps=clock.fps is not None, window=clock.window is not None or clock.duration is not None,
                          local=False)

    def check_session(self, session: SessionSpec, key: str, visit: _Visit | None) -> None:
        prefix: FieldPath = ("session",) if visit is not None else ()
        for name, category in SESSION_PRESETS:
            value = getattr(session, name)
            if isinstance(value, str):
                self.preset(category, value, visit, (*prefix, name) if prefix else (), session)
        outer = (self.current, self.platform, self.surface)
        self.current, self.platform, self.surface = visit, session.platform, session.platform
        for position, use in enumerate(session.checks):
            self.check_use(use.kind, use.params, CHECKPOINT_FAMILIES, (*prefix, "checks", position))
        for position, use in enumerate(session.defaults):
            self.check_use(use.kind, use.params, CHECKPOINT_FAMILIES, (*prefix, "defaults", position))
        self.current, self.platform, self.surface = outer
        if session.seed is not None and self.personas is not None and self.seed_personas(session.seed) is None:
            reason = self.seed_errors.get(session.seed)
            known = sorted(spec.name for spec in self.registry.entries("seed"))
            detail = f"planning it failed: {reason}" if reason else f"registered seeds: {_shown_ids(known)}"
            self.add("persona.seed", f"session {session.key!r} names seed {session.seed!r}, which gives no "
                     f"personas ({detail})", visit=visit, path=(*prefix, "seed") if prefix else ())
        if session.persona is not None and session.persona not in self.as_personas.get(key, set()):
            self.persona(session.persona, session, visit, (*prefix, "persona") if prefix else ())
        if visit is not None and session.now is None:
            self.add("clock.now", f"take session {session.key!r} has no frozen now; name a seed with now= or pass "
                     "now= to sc.take", visit=visit, path=(*prefix, "now"), severity="warning")
        frozen = session.now if visit is not None else session.now or self.plan.clock.now
        unset = [name for name in ("timezone", "locale") if getattr(session, name) is None]
        if frozen is not None and unset:
            self.add("clock.zone", f"session {session.key!r} freezes now at {frozen!r} but sets no "
                     f"{' or '.join(unset)}, so the device renders dates and numbers in the host's; set timezone and "
                     "locale in the app profile's [web] or [mobile] table, or on the flow or take", visit=visit,
                     path=(*prefix, unset[0]) if prefix else (), severity="warning")
        info = self.seed(session.seed) if session.seed is not None else None
        if info is not None and session.backend is not None and session.backend != info.backend:
            self.add("seed.backend", f"session {session.key!r} uses backend {session.backend!r} but seed "
                     f"{session.seed!r} is built on {info.backend!r}", visit=visit,
                     path=(*prefix, "backend") if prefix else ())

    def seed(self, name: str) -> SeedInfo | None:
        if self.seeds is None:
            return None
        if name not in self.seed_cache:
            try:
                self.seed_cache[name] = _seed_info(self.seeds, name)
            except _SeedLookupError as error:
                self.seed_errors[name] = str(error)
                self.seed_cache[name] = None
        return self.seed_cache[name]

    def check_now(self) -> None:
        plan = self.plan
        if plan.clock.now is not None:
            return
        if plan.kind == "seed":
            self.add("clock.now", f"seed {plan.name!r} has no frozen now; resources are created at the plan's now, so "
                     "pass now= such as '2026-01-15T09:00+03:00' to @eks.seed", severity="warning")
        elif plan.kind == "flow":
            self.add("clock.now", f"flow {plan.name!r} has no frozen now; name a seed that sets now= or pass now= "
                     "to @eks.flow so the app clock is frozen", severity="warning")

    def check_apps(self) -> None:
        if self.apps is None:
            return
        for resource in self.plan.resources:
            if resource.app is None:
                continue
            if resource.app not in self.app_cache:
                try:
                    self.app_cache[resource.app] = self.apps(resource.app)
                except LookupError as error:
                    self.add("resource.app", f"resource {resource.id!r} ({resource.kind}): {error}")
                    self.app_cache[resource.app] = ()
                    continue
            platforms = self.app_cache[resource.app]
            if platforms is None:
                self.add("resource.app", f"resource {resource.id!r} ({resource.kind}) names app {resource.app!r}, "
                         "which has no .harness/app.toml in the tree")
            elif (resource.kind.startswith(LEASE_PREFIX) and platforms
                  and resource.kind.removeprefix(LEASE_PREFIX) not in platforms):
                self.add("resource.app", f"resource {resource.id!r} leases {resource.kind.removeprefix(LEASE_PREFIX)} "
                         f"for app {resource.app!r}, which runs on {', '.join(platforms)}")

    def check_backends(self) -> None:
        if self.backends is None:
            return
        known = sorted(self.backends)
        for resource in self.plan.resources:
            if resource.kind != "backend" or resource.definition is None or resource.definition in self.backends:
                continue
            self.add("resource.backend", f"resource {resource.id!r} uses backend {resource.definition!r}, which no "
                     f"loaded plugin or project registers (@eks.backend or a [contributes] backend); known backends: "
                     f"{_shown_ids(known)}")

    def check_session_resources(self) -> None:
        sessions: dict[str, SessionSpec] = {}
        owners: dict[str, str] = {}
        if self.plan.session is not None:
            sessions[self.plan.session.key] = self.plan.session
            owners[self.plan.session.key] = "the plan session"
        for visit in self.visits:
            if visit.node.op == TAKE_OP:
                spec = self.take_session(visit.node)
                if spec is None:
                    continue
                if spec.key in sessions:
                    self.repeated_takes.add(visit.node.id)
                    self.add("session.duplicate", f"take session key {spec.key!r} is already the key of "
                             f"{owners[spec.key]}; every session of a plan needs its own key, because its lease and "
                             "backend are named by it", visit=visit, path=("session", "key"))
                    continue
                sessions[spec.key] = spec
                owners[spec.key] = f"take {visit.node.id!r}"
        self.check_resource_ids()
        self.check_session_counts()
        self.check_resource_params()
        for resource in self.plan.resources:
            if resource.session is None:
                if sessions and _session_role(resource) is not None:
                    self.add("session.resources", f"resource {resource.id!r} ({resource.kind}) belongs to no "
                             f"session, but the plan declares sessions {_shown_ids(sorted(sessions))}; every lease "
                             "and backend of a plan with sessions belongs to one of them, so a node cannot drive a "
                             "second device, browser or backend beside its session's")
                continue
            session = sessions.get(resource.session)
            if session is None:
                self.add("session.mismatch", f"resource {resource.id!r} ({resource.kind}) belongs to session "
                         f"{resource.session!r}, which the plan does not declare; sessions: "
                         f"{_shown_ids(sorted(sessions))}")
                continue
            for problem in self.session_problems(resource, session):
                self.add("session.mismatch", f"resource {resource.id!r} ({resource.kind}) of session "
                         f"{session.key!r} {problem}")

    def check_resource_ids(self) -> None:
        for resource in self.plan.resources:
            identity: dict[str, Any] = {**resource.params, "app": resource.app, "definition": resource.definition,
                                        "seed": resource.seed, "session": resource.session, "name": resource.name}
            try:
                expected = resource_id(resource.kind, identity)
            except ValueError as error:
                self.add("resource.id", f"resource {resource.id!r} ({resource.kind}) has no valid identity: {error}")
                continue
            if expected != resource.id:
                self.add("resource.id", f"resource {resource.id!r} ({resource.kind}) has the identity of resource "
                         f"{expected!r}; a resource id derives from its kind and identity, so plan it again instead "
                         "of renaming or copying resources")

    def check_resource_params(self) -> None:
        for resource in self.plan.resources:
            if _session_role(resource) is not None and resource.params:
                self.add("resource.params", f"resource {resource.id!r} ({resource.kind}) carries params "
                         f"{_shown_ids(sorted(resource.params))}; the engine acquires every lease and backend for "
                         "the run itself, so a plan cannot name a live lease, a backend lease or a backend URL")

    def check_session_counts(self) -> None:
        roles: dict[tuple[str, str], list[str]] = {}
        for resource in self.plan.resources:
            if resource.session is None:
                continue
            role = _session_role(resource)
            if role is not None:
                roles.setdefault((resource.session, role), []).append(resource.id)
        for (session, role), ids in sorted(roles.items()):
            if len(ids) > 1:
                self.add("session.resources", f"session {session!r} has {len(ids)} {role} resources "
                         f"({_shown_ids(ids)}); a session drives one lease and one backend, so its nodes share one "
                         "device or browser and one login state")

    def check_take_resources(self, visit: _Visit, own: SessionSpec) -> None:
        used: dict[str, None] = dict.fromkeys(visit.node.uses)
        for descendant in self.descendants(visit.node):
            used.update(dict.fromkeys(descendant.uses))
        for rid in used:
            resource = self.resources.get(rid)
            if resource is None or resource.session != own.key:
                continue
            for problem in self.session_problems(resource, own):
                self.add("session.mismatch", f"take {visit.node.id!r} declares session {own.key!r} on "
                         f"{own.platform} for {own.app!r}, but it uses resource {rid!r} ({resource.kind}), which "
                         f"{problem}", visit=visit, path=("session",))

    def check_session_uses(self, visit: _Visit) -> None:
        node = visit.node
        if node.op == TAKE_OP:
            own = self.take_session(node)
            if own is None:
                return
            if node.id in self.repeated_takes:
                self.check_take_resources(visit, own)
            governing: str | None = own.key
            where = f"its own take session {own.key!r}"
        elif visit.take is not None:
            if visit.session is None:
                return
            governing = visit.session.key
            where = f"take session {governing!r} (take {visit.take.id!r})"
        elif visit.session is not None:
            governing = visit.session.key
            where = f"the plan session {governing!r}"
        else:
            governing = None
            where = "no session: it is outside every take"
        for rid in node.uses:
            resource = self.resources.get(rid)
            if resource is not None and resource.session is None and governing is not None and _session_role(
                    resource) is not None:
                self.add("session.uses", f"node uses resource {rid!r} ({resource.kind}), which belongs to no "
                         f"session, but it runs under {where}; a node drives only the lease and backend of the "
                         "session that governs it", visit=visit)
                continue
            if resource is None or resource.session is None or resource.session == governing:
                continue
            self.add("session.uses", f"node uses resource {rid!r} ({resource.kind}) of session "
                     f"{resource.session!r}, but it runs under {where}; a node drives only the lease and backend "
                     "of the session that governs it", visit=visit)

    @staticmethod
    def session_problems(resource: Resource, session: SessionSpec) -> list[str]:
        problems: list[str] = []
        if resource.kind.startswith(LEASE_PREFIX) and resource.kind != LEASE_PREFIX + session.platform:
            problems.append(f"is a {resource.kind} lease but the session runs on {session.platform}")
        if resource.app != session.app:
            problems.append(f"names app {resource.app!r} but the session drives {session.app!r}")
        if resource.kind == "backend":
            if resource.seed != session.seed:
                problems.append(f"restores seed {resource.seed!r} but the session names {session.seed!r}")
            if resource.definition != session.backend:
                problems.append(f"is backend {resource.definition!r} but the session names {session.backend!r}")
        return problems

    def check_create(self, visit: _Visit) -> None:
        node = visit.node
        kind = node.args.get("kind")
        if not isinstance(kind, str):
            return
        try:
            spec = self.registry.resource(kind)
        except RegistryError as error:
            self.add("resource.kind_unknown", f"{error}; load the plugin that registers it", visit=visit,
                     path=("kind",))
            return
        fields = node.args.get("fields")
        try:
            model = spec.model.model_validate(fields if isinstance(fields, dict) else {})
        except ValidationError as error:
            self.add("resource.fields", f"fields of resource kind {kind!r} do not validate: "
                     f"{_validation_summary(error)}", visit=visit, path=("fields",))
        else:
            self.scan(model, None, ("fields",), self.root_scope())
        key = node.args.get("key")
        if spec.keyed and key is None:
            self.add("resource.fields", f"resource kind {kind!r} is keyed but the node has no key", visit=visit,
                     path=("key",))
        elif not spec.keyed and key is not None:
            self.add("resource.fields", f"resource kind {kind!r} is keyless but the node has key {key!r}",
                     visit=visit, path=("key",))
        self.check_create_parent(visit, spec.kind, spec.parent)
        phase = node.args.get("phase", "api")
        if phase != spec.phase:
            self.add("resource.phase", f"resource kind {kind!r} is created in phase {spec.phase!r}, not {phase!r}",
                     visit=visit, path=("phase",))

    def check_create_parent(self, visit: _Visit, kind: str, expected: str | None) -> None:
        parent = visit.node.args.get("parent")
        if parent is None:
            if expected is not None:
                self.add("resource.parent", f"resource kind {kind!r} is created under a {expected} resource but the "
                         "node has no parent", visit=visit, path=("parent",))
            return
        if expected is None:
            self.add("resource.parent", f"resource kind {kind!r} is created at the seed root but the node has a "
                     "parent", visit=visit, path=("parent",))
            return
        try:
            expr = parse_expr(parent) if is_expr_dict(parent) else None
        except RefError:
            expr = None
        if not isinstance(expr, RefExpr):
            return
        producer = self.by_id.get(expr.ref)
        if producer is None:
            return
        produced = f"resource:{expected}"
        if producer.node.op not in (CREATE_OP, BASE_OP) or expr.path or self.out_of(producer) != produced:
            self.add("resource.parent", f"resource kind {kind!r} is created under a {expected} resource, but its "
                     f"parent {expr.ref!r} is {producer.node.op} producing {self.out_of(producer)}", visit=visit,
                     path=("parent",))

    def check_macro(self, visit: _Visit) -> None:
        node = visit.node
        name = node.args.get("name")
        if not isinstance(name, str):
            return
        try:
            spec = self.registry.macro(name)
        except RegistryError as error:
            self.add("macro.unknown", f"{error}; load the plugin that registers it", visit=visit, path=("name",))
            return
        version = node.args.get("version", "1")
        if version != spec.version:
            self.add("macro.version", f"the node was traced with macro {name} version {version!r} but the registry "
                     f"has version {spec.version!r}; plan it again", visit=visit, path=("version",))
        params = node.args.get("params")
        for problem in macro_binding_problems(spec.fn, params if isinstance(params, dict) else {}):
            self.add("macro.params", f"macro {name}: {problem}", visit=visit, path=("params",))
        if spec.platforms is not None and self.platform is not None and self.platform not in spec.platforms:
            self.add("macro.platform", f"macro {name} runs on {', '.join(sorted(spec.platforms))}, not on "
                     f"{self.platform}", visit=visit, path=("name",))

    def check_renderer(self, visit: _Visit) -> None:
        renderer = visit.node.args.get("renderer")
        if not isinstance(renderer, str):
            return
        known = sorted(spec.media_kind for spec in self.registry.renderers())
        if renderer not in known:
            self.add("renderer.unknown", f"media.source names renderer {renderer!r}, which no loaded plugin or project "
                     f"registers (@eks.renderer); registered renderers: {_shown_ids(known)}", visit=visit,
                     path=("renderer",))

    def check_extend(self, visit: _Visit) -> None:
        if self.seeds is None:
            return
        node = visit.node
        base = node.args.get("base")
        if not isinstance(base, str):
            return
        info = self.seed(base)
        if info is None:
            reason = self.seed_errors.get(base)
            known = sorted(spec.name for spec in self.registry.entries("seed"))
            detail = f"planning it failed: {reason}" if reason else f"registered seeds: {_shown_ids(known)}"
            self.add("seed.unknown", f"data.extend names seed {base!r}, which is not registered ({detail})",
                     visit=visit, path=("base",))
            return
        if self.extends_back(base, set()):
            self.add("seed.cycle", f"seed {base!r} extends {self.plan.name!r} again, directly or through its bases",
                     visit=visit, path=("base",))
            return
        backends = [self.resources[rid].definition for rid in node.uses
                    if rid in self.resources and self.resources[rid].kind == "backend"]
        own = next((backend for backend in backends if backend is not None), None)
        if own is not None and own != info.backend:
            self.add("seed.backend", f"seed {self.plan.name!r} uses backend {own!r} but its base {base!r} is built on "
                     f"{info.backend!r}", visit=visit, path=("base",))
        if info.now != self.plan.clock.now:
            self.add("seed.now", f"base seed {base!r} freezes now at {info.now!r} but {self.plan.name!r} at "
                     f"{self.plan.clock.now!r}; a snapshot is only valid at its own now", visit=visit, path=("base",))
        if node.args.get("hash") != info.hash:
            self.add("seed.stale", f"data.extend carries plan hash {node.args.get('hash')!r} of {base!r}, which is now "
                     f"{info.hash!r}; plan the seed again", visit=visit, path=("hash",))

    def extended_bases(self) -> tuple[dict[str, SeedInfo], set[str]]:
        bases: dict[str, SeedInfo] = {}
        extended: set[str] = set()
        for visit in self.visits:
            base = visit.node.args.get("base")
            if visit.node.op == EXTEND_OP and isinstance(base, str):
                extended.add(base)
                info = self.seed(base)
                if info is not None:
                    bases[base] = info
        return bases, extended

    def check_seed_paths(self) -> None:
        bases, extended = self.extended_bases()
        open_counts = len(bases) < len(extended)
        counters: dict[tuple[str | None, str], int] = {}
        for info in bases.values():
            for path, _kind in info.resources:
                position = keyless_position(path)
                if position is not None:
                    parent, kind, index = position
                    counters[(parent, kind)] = max(counters.get((parent, kind), 0), index + 1)
        paths: dict[str, str] = {}
        personas: dict[str, str] = {}
        declared: list[str] = []
        complete = True
        for visit in self.visits:
            node = visit.node
            if node.op not in (CREATE_OP, BASE_OP):
                continue
            if visit.frames:
                container = visit.frames[-1].container
                self.add("seed.path", f"{node.op} sits inside {container.op} {container.id!r}; seed resources are "
                         "declared at the seed root because their path names them and their generated ids derive "
                         "from it, so every iteration or branch would create the same resource", visit=visit)
            path, kind, key = node.args.get("path"), node.args.get("kind"), node.args.get("key")
            if not isinstance(path, str) or not isinstance(kind, str):
                complete = False
                continue
            own_key = key if isinstance(key, str) else None
            if node.op == BASE_OP:
                self.check_base_path(visit, path, kind, own_key)
            else:
                expected = self.expected_path(visit, kind, own_key, counters, open_counts, path)
                if expected is not None and expected != path:
                    self.add("seed.path", f"data.create path {path!r} does not follow from its kind, key and parent; "
                             f"it is {expected!r}", visit=visit, path=("path",))
            if path in paths:
                self.add("seed.path", f"resource path {path!r} is already used by node {paths[path]!r}; generated ids "
                         "derive from the path, so two resources would get the same ids", visit=visit,
                         path=("path",))
            else:
                paths[path] = node.id
            if node.op != CREATE_OP:
                continue
            try:
                persona = self.registry.resource(kind).persona
            except RegistryError:
                complete = False
                continue
            if not persona or own_key is None:
                continue
            if own_key in personas:
                self.add("seed.persona", f"persona {own_key!r} is already declared by node {personas[own_key]!r}; "
                         "persona keys name users in flows and must be unique", visit=visit, path=("key",))
                continue
            personas[own_key] = node.id
            declared.append(own_key)
        if complete and list(self.plan.personas) != declared:
            self.add("seed.persona", f"the plan lists personas {_shown_ids(list(self.plan.personas))} but its persona "
                     f"resources declare {_shown_ids(declared)}; personas are the keys of the plan's persona "
                     "data.create nodes in order, so plan it again")

    def expected_path(self, visit: _Visit, kind: str, key: str | None, counters: dict[tuple[str | None, str], int],
                      open_counts: bool, path: str) -> str | None:
        parent = visit.node.args.get("parent")
        parent_path: str | None = None
        if parent is not None:
            try:
                expr = parse_expr(parent) if is_expr_dict(parent) else None
            except RefError:
                return None
            producer = self.by_id.get(expr.ref) if isinstance(expr, RefExpr) else None
            found = producer.node.args.get("path") if producer is not None else None
            if producer is None or producer.node.op not in (CREATE_OP, BASE_OP) or not isinstance(found, str):
                return None
            parent_path = found
        if key is not None:
            return resource_path(parent_path, resource_segment(kind, key, 0))
        counter = (parent_path, kind)
        if counter not in counters and open_counts:
            position = keyless_position(path)
            if position is not None and position[:2] == counter:
                counters[counter] = position[2]
        index = counters.get(counter, 0)
        counters[counter] = index + 1
        return resource_path(parent_path, resource_segment(kind, None, index))

    def check_base_path(self, visit: _Visit, path: str, kind: str, key: str | None) -> None:
        segment = path.rpartition("/")[2]
        if key is not None:
            fits = segment == resource_segment(kind, key, 0)
        else:
            position = keyless_position(path)
            fits = position is not None and position[1] == kind
        if not fits:
            shown = f"key {key!r}" if key is not None else "no key"
            self.add("seed.path", f"data.base path {path!r} does not end in a {kind} segment with {shown}",
                     visit=visit, path=("path",))

    def check_seed_resources(self) -> None:
        if self.seeds is None:
            return
        bases, extended = self.extended_bases()
        created = {path: (name, kind) for name, info in bases.items() for path, kind in info.resources}
        personas = {persona: name for name, info in bases.items() for persona in info.personas}
        self.check_base_overlap()
        for visit in self.visits:
            node = visit.node
            path, kind, key = node.args.get("path"), node.args.get("kind"), node.args.get("key")
            if not isinstance(path, str) or not isinstance(kind, str):
                continue
            if node.op == CREATE_OP:
                self.redeclared(visit, path, key, kind, created, personas)
            elif node.op == BASE_OP:
                self.looked_up(visit, path, kind, bases, extended)

    def check_base_overlap(self) -> None:
        paths: dict[str, tuple[str, str]] = {}
        personas: dict[str, tuple[str, str]] = {}
        for visit in self.visits:
            base = visit.node.args.get("base")
            if visit.node.op != EXTEND_OP or not isinstance(base, str):
                continue
            own_paths, own_personas = self.seed_origins(base, set())
            for what, found, known in (("resource", own_paths, paths), ("persona", own_personas, personas)):
                for item, origin in sorted(found.items()):
                    first = known.setdefault(item, (base, origin))
                    if first[1] != origin:
                        self.add("seed.resource", f"{what} {item!r} is created by base seed {first[0]!r} (in "
                                 f"{first[1]!r}) and by base seed {base!r} (in {origin!r}); the composed seed would "
                                 f"hold it twice, so extend one of them or move it into a seed both extend",
                                 visit=visit, path=("base",))

    def seed_origins(self, name: str, seen: set[str]) -> tuple[dict[str, str], dict[str, str]]:
        info = self.seed(name)
        if info is None or name in seen:
            return {}, {}
        seen.add(name)
        paths: dict[str, str] = {}
        personas: dict[str, str] = {}
        inherited_paths: set[str] = set()
        inherited_personas: set[str] = set()
        for base in info.bases:
            base_paths, base_personas = self.seed_origins(base, seen)
            for item, origin in base_paths.items():
                paths.setdefault(item, origin)
            for item, origin in base_personas.items():
                personas.setdefault(item, origin)
            base_info = self.seed(base)
            if base_info is not None:
                inherited_paths.update(path for path, _kind in base_info.resources)
                inherited_personas.update(base_info.personas)
        for path, _kind in info.resources:
            if path not in inherited_paths:
                paths.setdefault(path, name)
        for persona in info.personas:
            if persona not in inherited_personas:
                personas.setdefault(persona, name)
        return paths, personas

    def redeclared(self, visit: _Visit, path: str, key: object, kind: str,
                   created: Mapping[str, tuple[str, str]], personas: Mapping[str, str]) -> None:
        if path in created:
            self.add("seed.resource", f"resource {path!r} is already created by base seed {created[path][0]!r}; "
                     "this seed would create it a second time, so look it up with d.base(path) instead",
                     visit=visit, path=("path",))
            return
        try:
            persona = self.registry.resource(kind).persona
        except RegistryError:
            return
        if persona and isinstance(key, str) and key in personas:
            self.add("seed.resource", f"persona {key!r} is already an output of base seed {personas[key]!r}; persona "
                     "keys name users in flows and must be unique, so give this persona another key", visit=visit,
                     path=("key",))

    def looked_up(self, visit: _Visit, path: str, kind: str, bases: Mapping[str, SeedInfo],
                  extended: Collection[str]) -> None:
        base = visit.node.args.get("base")
        if not isinstance(base, str) or base not in extended:
            self.add("seed.resource", f"data.base names seed {base!r}, which this seed does not extend; extended: "
                     f"{_shown_ids(sorted(extended))}", visit=visit, path=("base",))
            return
        if base not in bases:
            return
        found = dict(bases[base].resources).get(path)
        if found is None:
            self.add("seed.resource", f"base seed {base!r} creates no resource {path!r}", visit=visit, path=("path",))
        elif found != kind:
            self.add("seed.resource", f"resource {path!r} of base seed {base!r} is a {found}, not a {kind}",
                     visit=visit, path=("kind",))

    def extends_back(self, base: str, seen: set[str]) -> bool:
        if base in seen:
            return False
        seen.add(base)
        info = self.seed(base)
        if info is None:
            return False
        return any(name == self.plan.name or self.extends_back(name, seen) for name in info.bases)

    def seed_personas(self, seed: str) -> Sequence[str] | None:
        if seed in self.persona_cache:
            return self.persona_cache[seed]
        result: Sequence[str] | None = None
        if self.personas is not None:
            try:
                result = _seed_lookup(self.personas, seed)
            except _SeedLookupError as error:
                self.seed_errors[seed] = str(error)
        self.persona_cache[seed] = result
        return result

    def persona(self, persona: str, session: SessionSpec | None, visit: _Visit | None, path: FieldPath) -> None:
        if session is not None and session.seed is not None:
            if self.personas is None:
                return
            known = self.seed_personas(session.seed)
            if known is not None and persona not in known:
                self.add("persona.unknown", f"persona {persona!r} is not an output of seed {session.seed!r}; "
                         f"its personas are {_shown_ids(sorted(known))}", visit=visit, path=path)
            return
        presets = [spec.name for spec in self.registry.presets("persona")]
        if persona not in presets:
            self.add("persona.unknown", f"persona {persona!r} has no seed to come from and is not a registered "
                     f"persona preset (presets: {_shown_ids(presets)}); name a seed in the flow or register "
                     "@eks.preset(..., category='persona')", visit=visit, path=path, severity="warning")

    def preset(self, category: str, name: str, visit: _Visit | None, path: FieldPath,
               session: SessionSpec | None = None) -> None:
        own = sorted(session.viewports) if category == "viewport" and session is not None else []
        if name in own:
            return
        names = [spec.name for spec in self.registry.presets(category)]
        if name not in names:
            app = f"; the app profile of {session.app!r} defines {_shown_ids(own)}" if own and session else ""
            self.add("preset.unknown", f"{category} preset {name!r} is not registered; known: {_shown_ids(names)}"
                     f"{app}", visit=visit, path=path)

    def check_node(self, visit: _Visit) -> None:
        node = visit.node
        self.current = visit
        self.platform = self.node_platform(visit)
        self.surface = self.node_surface(visit)
        spec = self.spec(node)
        if spec is None:
            known = [op.name for op in self.registry.ops(node.op.split(".")[0])]
            hint = f"ops of that family: {_shown_ids(known)}" if known else "load the plugin that defines it"
            self.add("op.unknown", f"op {node.op!r} is not registered; {hint}", visit=visit)
            return
        for resource in node.uses:
            if resource not in self.resources:
                self.add("resource.unknown", f"node uses resource {resource!r}, which the plan does not declare; "
                         f"declared: {_shown_ids(sorted(self.resources))}", visit=visit)
        self.check_session_uses(visit)
        self.check_after(visit)
        malformed: list[tuple[RefError, FieldPath]] = []
        for found, path in self.raw_exprs(node.args, ()):
            if isinstance(found, RefError):
                malformed.append((found, path))
            else:
                self.check_expr(found, visit, visit.index, visit.frames, path)
        if node.v != spec.version:
            self.add("op.version", f"the node was built with {node.op} version {node.v!r} but the registry has "
                     f"version {spec.version!r}; plan it again", visit=visit)
        problem = out_problem(spec, node.args, node.out)
        if problem is not None:
            hint = " (the node has no out field, which defaults to none)" if node.out == "none" else ""
            self.add("op.out", f"{problem}{hint}; set out to {expected_out(spec, node.args)}", visit=visit,
                     path=("out",))
        self.check_branches(visit, spec)
        self.check_placement(visit, spec)
        self.check_pin(visit, spec)
        for name, category in PRESET_FIELDS.get(node.op, ()):
            value = node.args.get(name)
            if isinstance(value, str):
                self.preset(category, value, visit, (name,), visit.session)
        if node.op == DYNAMIC_OP:
            self.check_dynamic(visit)
        elif node.op == BLENDER_OP:
            self.check_blender_render(visit)
        elif node.op == EXTEND_OP:
            self.check_extend(visit)
        elif node.op == CREATE_OP:
            self.check_create(visit)
        elif node.op == MACRO_OP:
            self.check_macro(visit)
        elif node.op == SOURCE_OP:
            self.check_renderer(visit)
        elif node.op == TAKE_OP:
            self.check_take_placement(visit)
            self.check_take_span(visit)
            session = self.take_session(node)
            if session is not None:
                self.check_session(session, node.id, visit)
        persona = node.args.get(PERSONA_FIELD)
        if isinstance(persona, str) and PERSONA_FIELD in spec.args.model_fields:
            self.persona(persona, visit.session, visit, (PERSONA_FIELD,))
        try:
            model = spec.args.model_validate(node.args)
        except ValidationError as error:
            rest = self.negative_errors(error, visit, ())
            if rest:
                self.add("args.invalid", f"{node.op} args do not validate: {_summary(rest, error)}", visit=visit)
            return
        self.report_malformed(malformed, visit)
        self.scan(model, None, (), self.root_scope())
        if node.op == EXPECT_VALUE_OP:
            self.check_expect_value(visit)

    def check_eventually(self, visit: _Visit) -> None:
        args = visit.node.args
        if args.get("eventually") is not True:
            return
        value = args.get("value")
        try:
            expr = parse_expr(value) if is_expr_dict(value) else None
        except RefError:
            return
        if isinstance(expr, ItemExpr):
            problem = ("its value is an eks.each item; the loop takes a snapshot of its list before the first iteration, "
                       "so the item never changes; poll the read that produced the list instead")
        elif not isinstance(expr, RefExpr):
            problem = "its value is not a $ref to a node, so there is nothing to read again; drop eventually"
        else:
            producer = self.by_id.get(expr.ref)
            if producer is None:
                return
            spec = self.spec(producer.node)
            if spec is None or spec.idempotent:
                return
            problem = (f"its value comes from {producer.node.op!r}, which is not idempotent, "
                       "so polling would run it again (a mutating request would repeat); assert on a read such as "
                       "api.get, ui.text or ui.count, or check the value once without eventually")
        self.add("expect.eventually", f"expect.value polls until the assertion holds, but {problem}", visit=visit,
                 path=("eventually",))

    def check_expect_value(self, visit: _Visit) -> None:
        self.check_eventually(visit)
        args = visit.node.args
        assertion = args.get("assertion")
        if not isinstance(assertion, dict):
            return
        kind = assertion.get("kind")
        produced = self.value_type(args.get("value"))
        if not isinstance(kind, str) or produced is None:
            return
        compared = self.value_type(assertion["value"]) if "value" in assertion else None
        problem = assertion_problem(kind, produced, compared)
        if problem is not None:
            self.add("ref.type", problem, visit=visit, path=("assertion",))

    def value_type(self, value: Any) -> str | None:
        if not is_expr_dict(value):
            return literal_type(value)
        try:
            expr = parse_expr(value)
        except RefError:
            return None
        if isinstance(expr, FmtExpr):
            return "str"
        return self.expr_type(expr)

    def lease_platform(self, visit: _Visit) -> str | None:
        for rid in visit.node.uses:
            resource = self.resources.get(rid)
            if resource is not None and resource.kind.startswith(LEASE_PREFIX):
                return resource.kind.removeprefix(LEASE_PREFIX)
        return None

    def node_platform(self, visit: _Visit) -> str | None:
        leased = self.lease_platform(visit)
        if leased is not None:
            return leased
        return visit.session.platform if visit.session is not None else None

    def node_surface(self, visit: _Visit) -> str | None:
        leased = self.lease_platform(visit)
        return leased if leased is not None else SURFACE_BY_OP.get(visit.node.op)

    def check_after(self, visit: _Visit) -> None:
        for after in visit.node.after:
            target = self.by_id.get(after)
            if target is None:
                self.add("node.after", f"'after' names node {after!r}, which is not in the plan", visit=visit)
            elif any(frame.container.id == after for frame in visit.frames):
                self.add("node.after", f"'after' names the enclosing container {after!r} ({target.node.op}), "
                         "which finishes only after this node", visit=visit)
            elif target.index >= visit.index:
                self.add("node.after", f"'after' names node {after!r} ({target.node.op}), which does not come "
                         "earlier in program order", visit=visit)
            elif not self.visible(target, visit.index, visit.frames):
                self.add("node.after", f"'after' names node {after!r} ({target.node.op}), which sits inside a when, "
                         "each or dynamic body (or another branch) this node is not in, so it may never run",
                         visit=visit)

    def check_dependencies(self) -> None:
        try:
            providers = clock_dependencies(self.plan, self.registry)
        except (PlanError, RegistryError):
            providers = {}
        reach: dict[str, set[str]] = {}
        for visit in self.visits:
            reach[visit.node.id] = self.reached(visit, reach)
        for visit in self.visits:
            node = visit.node
            needed: dict[str, str] = {}
            for found, _path in self.raw_exprs(node.args, ()):
                if isinstance(found, RefExpr):
                    needed.setdefault(found.ref, "reads its output")
            for provider in providers.get(node.id, ()):
                needed.setdefault(provider, "resolves its times against it (clock provider)")
            for producer_id, why in needed.items():
                producer = self.by_id.get(producer_id)
                if (producer is None or producer.index >= visit.index or producer_id in reach[node.id]
                        or not self.visible(producer, visit.index, visit.frames)):
                    continue
                self.add("node.after", f"node {producer_id!r} ({producer.node.op}) is not among this node's 'after' "
                         f"edges, nor reachable through them, although this node {why}; add it to 'after'",
                         visit=visit, path=("after",))
        self.check_lanes(self.plan.nodes, None, reach)

    def reached(self, visit: _Visit, reach: Mapping[str, set[str]]) -> set[str]:
        found: set[str] = set()
        if visit.frames:
            found |= reach.get(visit.frames[-1].container.id, set())
        for after in visit.node.after:
            target = self.by_id.get(after)
            if target is None or target.index >= visit.index or any(
                    frame.container.id == after for frame in visit.frames):
                continue
            found.add(after)
            found |= reach.get(after, set())
            for descendant in self.descendants(target.node):
                found.add(descendant.id)
                found |= reach.get(descendant.id, set())
        return found

    @staticmethod
    def descendants(node: Node) -> Iterator[Node]:
        for children in (node.branches or {}).values():
            for child in children:
                yield child
                yield from _Run.descendants(child)

    def check_lanes(self, nodes: Sequence[Node], container: Node | None, reach: Mapping[str, set[str]]) -> None:
        parallel = container is not None and container.op == PARALLEL_OP
        tails: dict[str, Node] = {}
        for node in nodes:
            spec = self.spec(node)
            if node.branches is not None:
                for children in node.branches.values():
                    self.check_lanes(children, node, reach)
            if spec is None or spec.ordering != "program":
                continue
            lanes = self.lanes(node, spec, parallel)
            for lane in lanes:
                tail = tails.get(lane)
                if tail is not None and tail.id not in reach.get(node.id, set()):
                    shown = "its scope" if lane == SINGLE_LANE else f"lane {lane!r}"
                    self.add("node.after", f"node {tail.id!r} ({tail.op}) runs before this node on {shown} in program "
                             "order, but this node does not wait for it; add it to 'after'",
                             visit=self.visit_of.get(id(node)), path=("after",))
                tails[lane] = node

    def lanes(self, node: Node, spec: OpSpec, parallel: bool) -> list[str]:
        if not parallel:
            return [SINGLE_LANE]
        if not spec.container:
            return list(dict.fromkeys(node.uses)) or [FREE_LANE]
        lanes = list(dict.fromkeys(node.uses))
        for descendant in self.descendants(node):
            child = self.spec(descendant)
            if child is None or child.ordering != "program" or (child.container and not descendant.uses):
                continue
            for lane in list(dict.fromkeys(descendant.uses)) or [FREE_LANE]:
                if lane not in lanes:
                    lanes.append(lane)
        return lanes or [FREE_LANE]

    def check_cycles(self) -> None:
        graph = {node_id: [after for after in visit.node.after if after in self.by_id]
                 for node_id, visit in self.by_id.items()}
        for component in _strongly_connected(graph):
            if len(component) == 1 and component[0] not in graph[component[0]]:
                continue
            members = sorted(component, key=lambda node_id: self.by_id[node_id].index)
            self.add("node.cycle", f"the 'after' edges of nodes {_shown_ids(members)} form a cycle",
                     visit=self.by_id[members[0]])

    def check_branches(self, visit: _Visit, spec: OpSpec) -> None:
        node = visit.node
        if not spec.container:
            if node.branches is not None:
                self.add("node.branches", f"{node.op} is not a container but the node has branches "
                         f"{_shown_ids(sorted(node.branches))}", visit=visit)
            return
        branches = node.branches or {}
        for name in branches:
            if name not in spec.branches:
                self.add("node.branches", f"{node.op} has no branch {name!r}; its branches are "
                         f"{_shown_ids(list(spec.branches))}", visit=visit)
        required = REQUIRED_BRANCHES.get(node.op)
        if required is None:
            required = "body" if "body" in spec.branches else next(iter(spec.branches), None)
        if required is not None and required not in branches:
            if node.op == WHEN_OP and "else" in branches:
                self.add("control.else", "the eks.when node has an 'else' branch but no 'then' branch; write the "
                         "then branch with `with eks.when(...)` before `with eks.otherwise()`", visit=visit)
            else:
                self.add("node.branches", f"the {node.op} container lacks its {required!r} branch", visit=visit)
        for name in EMPTY_WARNED_BRANCHES:
            if name in branches and not branches[name]:
                self.add("control.empty", f"the {name!r} branch of {node.op} is empty", visit=visit,
                         severity="warning")

    @staticmethod
    def enclosing(visit: _Visit, op: str) -> Node | None:
        for frame in reversed(visit.frames):
            if frame.container.op == op:
                return frame.container
        return None

    def check_placement(self, visit: _Visit, spec: OpSpec) -> None:
        node = visit.node
        if spec.platforms is not None and self.platform is not None and self.platform not in spec.platforms:
            self.add("op.platform", f"{node.op} runs on {', '.join(sorted(spec.platforms))}, not on "
                     f"{self.platform}", visit=visit)
        kinds = [self.resources[resource].kind for resource in node.uses if resource in self.resources]
        if "lease" in spec.resources and not any(kind.startswith(LEASE_PREFIX) for kind in kinds):
            self.add("op.resources", f"{node.op} needs a device or browser lease but the node uses none; call it "
                     "on a session (s.ui..., s.web..., or a take's flow)", visit=visit)
        if "backend" in spec.resources and "backend" not in kinds:
            self.add("op.resources", f"{node.op} needs a backend but the node uses none; call it on a session or "
                     "inside a seed", visit=visit)
        recording = self.enclosing(visit, RECORD_OP)
        if spec.recording_only and recording is None and visit.take is None:
            self.add("op.recording_only", f"{node.op} only works inside `with s.record(...)` or a take's flow",
                     visit=visit)
        if node.op == RECORD_OP and recording is not None:
            self.add("record.nested", f"capture.record inside the recording {recording.id!r}; end the outer "
                     "recording first", visit=visit)
        cassette = self.enclosing(visit, CASSETTE_OP)
        if node.op == CASSETTE_OP and cassette is not None:
            self.add("cassette.nested", f"net.cassette inside the cassette {cassette.id!r}; both would record or "
                     "replay the same requests, end the outer cassette first", visit=visit)

    def check_pin(self, visit: _Visit, spec: OpSpec) -> None:
        node = visit.node
        if node.pin is None:
            return
        if not spec.pinnable:
            self.add("pin.op", f"{node.op} cannot be pinned with at=", visit=visit)
        if visit.take is None:
            self.add("pin.outside_take", f"{node.op} is pinned at {format_time(node.pin)!r} outside a take; at= "
                     "only works inside `with take.flow() as s:`", visit=visit)
        else:
            self.check_pin_span(visit, node.pin, visit.take)
            loop = self.take_loop(visit)
            if loop is not None:
                self.add("pin.loop", f"{node.op} is pinned at {format_time(node.pin)!r} inside the runtime eks.each "
                         f"{loop.id!r}; every iteration would land on the same time. Pin a step outside the loop, "
                         "or unroll it with a Python for loop over a literal list", visit=visit)
        self.time(node.pin, self.root_scope(), visit, (), "pin")

    def check_take_span(self, visit: _Visit) -> None:
        model = self.validated(visit.node)
        if not isinstance(model, Take) or model.span is None:
            return
        context = self.pin_context()
        span = self.span_bounds(model.span, context, f"the take {_shown_time(model.span)}")
        clock = self.clock_bounds(context)
        if span is None:
            return
        if clock.low <= span.low and (clock.high is None or (span.high is not None and span.high <= clock.high)):
            return
        self.add("take.span", f"{span.what} ({span.ticks}) reaches outside {clock.what} ({clock.ticks}); the take "
                 "can only cover times the score clock has, so cut the span to it", visit=visit, path=("span",))

    def check_take_placement(self, visit: _Visit) -> None:
        if not visit.frames:
            return
        container = visit.frames[-1].container
        self.add("take.placement", f"score.take sits inside {container.op} {container.id!r}; a score is a static "
                 "arrangement, so a take belongs at the top level of the score function, outside eks.when, eks.each, "
                 "eks.parallel and every other container", visit=visit)

    def check_pin_span(self, visit: _Visit, pin: Time, take: Node) -> None:
        context = self.pin_context()
        ticks = self.static_ticks(pin, context)
        bounds = self.pin_bounds(take, context)
        if ticks is None:
            return
        if bounds.low <= ticks and (bounds.high is None or ticks <= bounds.high):
            return
        self.add("pin.span", f"{visit.node.op} is pinned at {format_time(pin)!r} ({ticks} ticks), outside "
                 f"{bounds.what} ({bounds.ticks}); the take can only warp footage onto times it covers, so move the "
                 "pin inside or widen the span", visit=visit)

    def pin_bounds(self, take: Node, context: TimeContext | None) -> _Bounds:
        model = self.validated(take)
        clock = self.clock_bounds(context)
        if not isinstance(model, Take) or model.span is None:
            return clock
        span = self.span_bounds(model.span, context, f"the take {_shown_time(model.span)}")
        if span is None:
            return clock
        low = max(span.low, clock.low)
        high = span.high if clock.high is None or span.high is None else min(span.high, clock.high)
        if (low, high) == (span.low, span.high):
            return span
        return _Bounds(low, high, f"{span.what} cut to {clock.what}")

    def clock_bounds(self, context: TimeContext | None) -> _Bounds:
        clock = self.plan.clock
        if clock.window is not None:
            window = self.span_bounds(clock.window, context, f"the score window {_shown_time(clock.window)}")
            return window or _Bounds(0, None, "the score clock, which starts at 0")
        if clock.duration is not None:
            end = self.static_ticks(clock.duration, context)
            if end is not None:
                return _Bounds(0, end, f"the clock duration {format_time(clock.duration)!r}")
        return _Bounds(0, None, "the score clock, which starts at 0")

    def span_bounds(self, span: Span, context: TimeContext | None, what: str) -> _Bounds | None:
        start = self.static_ticks(span.start, context)
        end = self.static_ticks(span.end, context)
        if start is None or end is None:
            return None
        return _Bounds(start, end, what)

    @staticmethod
    def take_loop(visit: _Visit) -> Node | None:
        return next((frame.container for frame in visit.frames if frame.container.op == EACH_OP), None)

    def check_pin_order(self) -> None:
        context = self.pin_context()
        for visit in self.visits:
            if visit.node.op == TAKE_OP and visit.node.branches is not None:
                for children in visit.node.branches.values():
                    self.order_pins(children, None, context)

    def order_pins(self, nodes: Sequence[Node], latest: _Pinned | None,
                   context: TimeContext | None) -> _Pinned | None:
        for node in nodes:
            if node.pin is not None:
                latest = self.order_pin(node, node.pin, latest, context)
            if node.branches is None or node.op == TAKE_OP:
                continue
            if node.op == WHEN_OP:
                start = latest
                ends = [self.order_pins(children, start, context) for children in node.branches.values()]
                latest = max((end for end in (start, *ends) if end is not None), key=lambda end: end.ticks,
                             default=None)
                continue
            for children in node.branches.values():
                latest = self.order_pins(children, latest, context)
        return latest

    def order_pin(self, node: Node, pin: Time, latest: _Pinned | None,
                  context: TimeContext | None) -> _Pinned | None:
        ticks = self.static_ticks(pin, context)
        if ticks is None:
            return latest
        if latest is not None and ticks <= latest.ticks:
            self.add("pin.order", f"pin {format_time(pin)!r} does not come after the pin of the earlier node "
                     f"{latest.node.id!r} ({format_time(latest.pin)!r}); pins of a take must increase along every "
                     "path of program order", visit=self.visit_of[id(node)])
            return latest
        return _Pinned(ticks, node, pin)

    def cached_static_context(self) -> TimeContext | None:
        if not self.static_cache:
            self.static_cache.append(self.static_context())
        return self.static_cache[0]

    def pin_context(self) -> TimeContext | None:
        if not self.pin_cache:
            self.pin_cache.append(self.clock_context())
        return self.pin_cache[0]

    def clock_context(self) -> TimeContext | None:
        base = self.cached_static_context()
        if base is None:
            return None
        bounds = self.clock_bounds(base)
        return base if bounds.high is None else replace(base, end=bounds.high)

    def static_context(self) -> TimeContext | None:
        clock = self.plan.clock
        for cues in (clock.cues, {}):
            try:
                return TimeContext.static(fps=clock.fps, tempo=clock.tempo, cues=cues)
            except TimeError:
                continue
        return None

    @staticmethod
    def static_ticks(pin: Time, context: TimeContext | None) -> int | None:
        if context is None:
            return pin.ticks if pin.is_concrete else None
        try:
            return context.resolve(pin)
        except TimeError:
            return None

    def check_dynamic(self, visit: _Visit) -> None:
        fn = visit.node.args.get("fn")
        if not isinstance(fn, dict):
            return
        freevars = fn.get("freevars")
        bindings = freevars if isinstance(freevars, dict) else {}
        for name, binding in bindings.items():
            kind = binding.get("kind") if isinstance(binding, dict) else None
            if kind not in BINDING_KINDS:
                self.add("dynamic.binding", f"closure variable {name!r} has binding kind {kind!r}; the sandbox "
                         f"rebuilds only {', '.join(BINDING_KINDS)}", visit=visit, path=("fn", "freevars", name))
        names = fn.get("globals")
        for position, name in enumerate(names if isinstance(names, list) else []):
            if name in ALLOWED_DYNAMIC_GLOBALS:
                continue
            binding = bindings.get(name) if isinstance(name, str) else None
            if isinstance(binding, dict) and binding.get("kind") == "literal":
                continue
            self.add("dynamic.binding", f"global {name!r} is neither one of {', '.join(ALLOWED_DYNAMIC_GLOBALS)} "
                     "nor a JSON literal; pass it as a parameter default", visit=visit,
                     path=("fn", "globals", position))
        source = fn.get("source")
        if not isinstance(source, str):
            return
        tree = self.parsed_source(source)
        scan = self.sandbox_scan(source)
        if tree is None or scan is None:
            return
        path: FieldPath = ("fn", "source")
        for use in scan.imports:
            self.add("dynamic.binding", f"line {use.line} imports {use.name!r}; an eks.dynamic function may import "
                     "nothing, so pass what it needs as a parameter default", visit=visit, path=path)
        self.check_sandbox_source(scan, set(bindings) | set(ALLOWED_DYNAMIC_GLOBALS), visit, path)
        self.check_leaks(source, _data_names(bindings), (), visit, path)
        if visit.take is None:
            return
        namespaces = {name: str(binding.get("value", "")).partition(".")[2] for name, binding in bindings.items()
                      if isinstance(binding, dict) and binding.get("kind") == "namespace"}
        roots: dict[str, _CallPath] = {name: () for name, binding in bindings.items()
                                       if isinstance(binding, dict) and binding.get("kind") == "session"}
        roots.update({name: tuple(namespace.split(".")) for name, namespace in namespaces.items() if namespace})
        for line, reason in self.dynamic_pins(tree, namespaces, _CallPaths(tree, roots)):
            self.add("pin.loop", f"line {line} {reason} inside the eks.dynamic function of a take; its steps run "
                     "once per runtime value, so pin steps outside the function", visit=visit, path=path)

    def dynamic_pins(self, tree: ast.AST, namespaces: Mapping[str, str],
                     paths: _CallPaths) -> Iterator[tuple[int, str]]:
        for found in ast.walk(tree):
            if not isinstance(found, ast.Call):
                continue
            reason = _pin_keyword(found)
            if reason is not None:
                yield found.lineno, reason
                continue
            for macro in self.call_macros(found, namespaces, {}, paths):
                detail = self.function_pin(macro.fn, set())
                if detail is not None:
                    yield found.lineno, f"calls macro {macro.name!r}, which {detail}"
                    break

    def call_macros(self, call: ast.Call, namespaces: Mapping[str, str], scope: Mapping[str, Any],
                    paths: _CallPaths) -> list[MacroSpec]:
        direct = self.called_macro(call, namespaces, scope)
        found: dict[str, MacroSpec] = {} if direct is None else {direct.name: direct}
        for path in sorted(paths.paths(call.func)):
            if len(path) < 2:
                continue
            try:
                macro = self.registry.macro(f"{path[-2]}.{path[-1]}")
            except RegistryError:
                continue
            found.setdefault(macro.name, macro)
        return list(found.values())

    def called_macro(self, call: ast.Call, namespaces: Mapping[str, str], scope: Mapping[str, Any]) -> MacroSpec | None:
        func = call.func
        if isinstance(func, ast.Call) and isinstance(func.func, ast.Attribute) and func.func.attr == MACRO_METHOD:
            first = func.args[0] if func.args else None
            target = scope.get(first.id) if isinstance(first, ast.Name) else None
            try:
                return self.registry.macro_for(target) if callable(target) else None
            except RegistryError:
                return None
        if not isinstance(func, ast.Attribute):
            return None
        owner = func.value
        namespace = owner.attr if isinstance(owner, ast.Attribute) else (
            namespaces.get(owner.id) if isinstance(owner, ast.Name) else None)
        if not namespace:
            return None
        try:
            return self.registry.macro(f"{namespace}.{func.attr}")
        except RegistryError:
            return None

    def function_pin(self, fn: Callable[..., Any], seen: set[int]) -> str | None:
        code = getattr(fn, "__code__", None)
        if id(code) in seen:
            return None
        seen.add(id(code))
        label = getattr(fn, "__qualname__", repr(fn))
        tree = _function_tree(fn)
        if tree is None or not isinstance(code, CodeType):
            return f"calls {label}, whose source cannot be read, so it may pin a step"
        scope: dict[str, Any] = dict(getattr(fn, "__globals__", {}))
        closure: tuple[Any, ...] = getattr(fn, "__closure__", None) or ()
        for var, cell in zip(code.co_freevars, closure, strict=True):
            try:
                scope[var] = cell.cell_contents
            except ValueError:
                continue
        paths = _CallPaths(tree, _root_params(tree))
        for found in ast.walk(tree):
            if not isinstance(found, ast.Call):
                continue
            reason = _pin_keyword(found)
            if reason is not None:
                return f"{reason} ({label}, line {code.co_firstlineno + found.lineno - 1} of {code.co_filename})"
            macros = self.call_macros(found, {}, scope, paths)
            targets = [macro.fn for macro in macros]
            if not macros:
                function = _called_function(found.func, scope)
                targets = [function] if function is not None else []
            for target in targets:
                detail = self.function_pin(target, seen)
                if detail is not None:
                    return detail
        return None

    def check_sandbox_source(self, scan: SandboxScan, provided: Collection[str], visit: _Visit,
                             path: FieldPath) -> None:
        for name in scan.denied_builtins(provided):
            self.add("dynamic.binding", f"the source uses the builtin {name!r}, which the sandbox does not provide "
                     f"because it is not deterministic or has side effects; the allowed builtins are "
                     f"{', '.join(SANDBOX_BUILTINS)}", visit=visit, path=path)
        for use in scan.escapes:
            self.add("dynamic.binding", f"line {use.line} reaches {use.name!r}, which can leave the sandbox; dunder "
                     "names, private (_name) and internal (ctx) attributes, the frame, generator, coroutine and "
                     "traceback attributes, the model parsing and construction API (parse_file, parse_raw, from_orm, "
                     f"model_rebuild and the like), eks members other than {', '.join(SANDBOX_EKS_ATTRIBUTES)} and .format "
                     "on anything but a string literal are not allowed", visit=visit, path=path)

    def check_leaks(self, source: str, data_names: Collection[str], modules: Collection[str], visit: _Visit,
                    path: FieldPath) -> None:
        try:
            leaks = sandbox_leaks(source, data_names=data_names, data_modules=modules, params_are_data=not modules)
        except (SyntaxError, ValueError):
            return
        for use in leaks:
            self.add("dynamic.binding", f"line {use.line} passes a value that may hold an object address to "
                     f"{use.name}; generators, iterators, functions, methods, classes and sandbox objects print "
                     "their memory address and hash by identity, so the result would change between runs. Format "
                     "or hash only data and materialize iterators with list(...) first", visit=visit, path=path)

    @staticmethod
    def sandbox_scan(source: str) -> SandboxScan | None:
        try:
            return scan_sandbox_source(source)
        except (SyntaxError, ValueError):
            return None

    @staticmethod
    def parsed_source(source: str) -> ast.Module | None:
        try:
            return ast.parse(source)
        except SyntaxError:
            return None

    def check_blender_render(self, visit: _Visit) -> None:
        scene = visit.node.args.get("scene")
        render = scene.get("render") if isinstance(scene, dict) else None
        settings = render if isinstance(render, dict) else {}
        path: FieldPath = ("scene", "render")
        engine = settings.get("engine")
        if engine not in ENGINES:
            self.add("blender.render", f"engine {engine!r} is not a resolved Blender engine; write one of "
                     f"{', '.join(ENGINES)} (aliases are resolved at plan time)", visit=visit,
                     path=(*path, "engine"))
        samples = settings.get("samples")
        if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
            self.add("blender.render", f"samples {samples!r} is not an explicit positive sample count",
                     visit=visit, path=(*path, "samples"))
        device = settings.get("device")
        if device not in DEVICES:
            self.add("blender.render", f"device {device!r} is not explicit; write one of {', '.join(DEVICES)}",
                     visit=visit, path=(*path, "device"))
        hooks = scene.get("hooks") if isinstance(scene, dict) else None
        for index, hook in enumerate(hooks if isinstance(hooks, list) else []):
            fn = hook.get("fn") if isinstance(hook, dict) else None
            if isinstance(fn, dict):
                self.check_bpy_hook(cast("dict[str, Any]", fn), visit, ("scene", "hooks", index, "fn"))

    def check_bpy_hook(self, fn: Mapping[str, Any], visit: _Visit, path: FieldPath) -> None:
        names = fn.get("globals")
        for position, name in enumerate(names if isinstance(names, list) else []):
            if isinstance(name, str) and name in BUILTIN_NAMES and name not in SANDBOX_BUILTINS:
                self.add("dynamic.binding", f"bpy hook global {name!r} is a builtin the sandbox does not "
                         f"provide; the allowed builtins are {', '.join(SANDBOX_BUILTINS)}", visit=visit,
                         path=(*path, "globals", position))
        source = fn.get("source")
        scan = self.sandbox_scan(source) if isinstance(source, str) else None
        if not isinstance(source, str) or scan is None:
            return
        for use in scan.imports:
            if use.name.split(".")[0] not in BPY_MODULES:
                self.add("dynamic.binding", f"line {use.line} of the bpy hook imports {use.name!r}; a bpy hook may "
                         f"import only {', '.join(BPY_MODULES)}", visit=visit, path=(*path, "source"))
        freevars = fn.get("freevars")
        bindings = cast("dict[str, Any]", freevars) if isinstance(freevars, dict) else {}
        self.check_sandbox_source(scan, set(bindings) | set(BPY_MODULES), visit, (*path, "source"))
        self.check_leaks(source, _data_names(bindings), BPY_MODULES, visit, (*path, "source"))

    def check_use(self, kind: str, params: Mapping[str, Any], families: Sequence[str], path: FieldPath) -> None:
        visit = self.current
        try:
            spec: CheckSpec = self.registry.check(kind)
        except RegistryError as error:
            self.add("check.kind", str(error), visit=visit, path=(*path, "kind"))
            return
        if spec.family not in families:
            self.add("check.kind", f"check {kind!r} is a {spec.family} check; use a {' or '.join(families)} "
                     "check here", visit=visit, path=(*path, "kind"))
            return
        try:
            model = spec.args.model_validate(dict(params))
        except ValidationError as error:
            rest = self.negative_errors(error, visit, (*path, "params"))
            if rest:
                self.add("check.args", f"params of check {kind!r} do not validate: {_summary(rest, error)}",
                         visit=visit, path=(*path, "params"))
            return
        if spec.platforms is not None and self.platform is not None and self.platform not in spec.platforms:
            self.add("check.platform", f"check {kind!r} runs on {', '.join(sorted(spec.platforms))}, not on "
                     f"{self.platform}", visit=visit, path=(*path, "kind"))
        self.scan(model, None, (*path, "params"), self.root_scope())

    def visible(self, producer: _Visit, index: int, frames: tuple[_Frame, ...]) -> bool:
        if producer.index >= index:
            return False
        if any(frame.container.id == producer.node.id for frame in frames):
            return False
        open_scopes = {(frame.container.id, frame.branch) for frame in frames}
        for frame in producer.frames:
            if (frame.container.id, frame.branch) in open_scopes:
                continue
            spec = self.spec(frame.container)
            if spec is None or not spec.exports_body:
                return False
        return True

    def report_malformed(self, malformed: Sequence[tuple[RefError, FieldPath]], visit: _Visit | None) -> None:
        for error, path in malformed:
            self.add("ref.path", f"malformed ref expression: {error}", visit=visit, path=path)

    def raw_exprs(self, value: Any, path: FieldPath) -> Iterator[tuple[Expr | RefError, FieldPath]]:
        if isinstance(value, dict):
            mapping = cast("dict[str, Any]", value)
            if is_expr_dict(mapping):
                try:
                    expr = parse_expr(mapping)
                except RefError as error:
                    yield error, path
                    return
                for part in _expr_parts(expr):
                    yield part, path
                    for step in part.path:
                        if isinstance(step, FindStep):
                            yield from self.raw_exprs(step.find, path)
                        elif isinstance(step, FilterStep):
                            yield from self.raw_exprs(step.filter_, path)
                return
            for key, item in mapping.items():
                yield from self.raw_exprs(item, (*path, key))
        elif isinstance(value, list | tuple):
            for position, item in enumerate(cast("Sequence[Any]", value)):
                yield from self.raw_exprs(item, (*path, position))

    def check_outputs(self) -> None:
        for found, path in self.raw_exprs(self.plan.outputs, ()):
            if isinstance(found, RefError):
                self.add("ref.path", f"malformed ref expression in the plan outputs: {found}", path=path)
            else:
                self.check_expr(found, None, len(self.visits), (), ())

    def check_expr(self, expr: Expr, visit: _Visit | None, index: int, frames: tuple[_Frame, ...],
                   path: FieldPath) -> None:
        where = "the plan outputs" if visit is None else "this node"
        if isinstance(expr, ItemExpr):
            if not any(frame.container.id == expr.item and frame.container.op == EACH_OP for frame in frames):
                self.add("ref.item_scope", f"$item {expr.item!r} is used outside the body of that eks.each "
                         f"({where} is not inside it)", visit=visit, path=path)
                return
            base = self.item_type(self.by_id[expr.item].node)
            if base is None:
                return
        else:
            producer = self.by_id.get(expr.ref)
            if producer is None:
                self.add("ref.unknown", f"$ref names node {expr.ref!r}, which is not in the plan", visit=visit,
                         path=path)
                return
            if not self.visible(producer, index, frames):
                self.add("ref.not_visible", f"$ref names node {expr.ref!r} ({producer.node.op}), which is not "
                         f"visible from {where}: it comes later, encloses it, or sits inside a when, each or "
                         "dynamic body", visit=visit, path=path)
                return
            base = self.out_of(producer)
        try:
            infer_path(base, expr.path)
        except RefTypeError as error:
            self.add("ref.path", str(error), visit=visit, path=path)

    def out_of(self, producer: _Visit) -> str:
        node = producer.node
        spec = self.spec(node)
        if spec is None or out_problem(spec, node.args, node.out) is None:
            return node.out
        expected = expected_out(spec, node.args)
        return expected if expected is not None else node.out

    def item_type(self, each: Node) -> str | None:
        if each.id in self.item_types:
            return self.item_types[each.id]
        self.item_types[each.id] = None
        over = each.args.get("over")
        result: str | None = None
        expr: RefExpr | ItemExpr | FmtExpr | None = None
        if is_expr_dict(over):
            try:
                expr = parse_expr(over)
            except RefError:
                expr = None
        produced = self.expr_type(expr) if isinstance(expr, RefExpr | ItemExpr) else None
        if produced is not None:
            try:
                parsed = parse_ref_type(produced)
            except RefTypeError:
                parsed = None
            if parsed is not None:
                result = str(parsed.item) if parsed.name == "list" and parsed.item is not None else "json"
        self.item_types[each.id] = result
        return result

    def expr_type(self, expr: Expr) -> str | None:
        if isinstance(expr, RefExpr):
            producer = self.by_id.get(expr.ref)
            base = self.out_of(producer) if producer is not None else None
        else:
            each = self.by_id.get(expr.item)
            base = self.item_type(each.node) if each is not None and each.node.op == EACH_OP else None
        if base is None:
            return None
        try:
            return infer_path(base, expr.path)
        except RefTypeError:
            return None

    def typed_ref(self, expr: Expr, expected: str | None, path: FieldPath, fmt: bool) -> None:
        produced = self.expr_type(expr)
        if produced is None:
            return
        name = expr.ref if isinstance(expr, RefExpr) else f"$item {expr.item}"
        if fmt:
            if produced not in FMT_TYPES:
                self.add("ref.type", f"ref to {name!r} produces {produced}, which cannot be interpolated into "
                         "text; refs inside an f-string must produce str, int, float, bool or json",
                         visit=self.current, path=path)
            return
        if expected is None:
            return
        try:
            fits = compatible(produced, expected)
        except RefTypeError:
            return
        if not fits:
            hint = "; use an f-string to turn it into text" if expected == "str" else ""
            self.add("ref.type", f"ref to {name!r} produces {produced} but the field takes {expected}{hint}",
                     visit=self.current, path=path)

    def scan(self, value: Any, kind: str | None, path: FieldPath, scope: _TimeScope) -> None:
        if isinstance(value, RefExpr | ItemExpr | FmtExpr):
            fmt = isinstance(value, FmtExpr)
            for part in _expr_parts(value):
                self.typed_ref(part, kind, path, fmt)
                for step in part.path:
                    if isinstance(step, FindStep):
                        self.scan(step.find, None, path, scope)
                    elif isinstance(step, FilterStep):
                        self.scan(step.filter_, None, path, scope)
        elif isinstance(value, Target):
            self.target(value, path, scope)
        elif isinstance(value, Time | Span | TimeSet):
            self.time(value, scope, self.current, path, None)
        elif isinstance(value, BaseModel):
            self.scan_model(value, path, scope)
        elif isinstance(value, dict):
            mapping = cast("dict[str, Any]", value)
            if is_expr_dict(mapping):
                try:
                    expr = parse_expr(mapping)
                except RefError:
                    return
                self.scan(expr, kind, path, scope)
                return
            for key, item in mapping.items():
                self.scan(item, kind, (*path, key), scope)
            self.json_times(mapping, path, scope)
        elif isinstance(value, list | tuple | set | frozenset):
            for position, item in enumerate(canonical_items(cast("Collection[Any]", value))):
                self.scan(item, kind, (*path, position), scope)

    def json_times(self, mapping: Mapping[str, Any], path: FieldPath, scope: _TimeScope) -> None:
        seen: set[FieldPath] = set()
        for model in self.decoder.models(mapping):
            self.model_times(model, path, scope, seen)

    def model_times(self, value: Any, path: FieldPath, scope: _TimeScope, seen: set[FieldPath]) -> None:
        if isinstance(value, Time | Span | TimeSet):
            if path not in seen:
                seen.add(path)
                self.time(value, scope, self.current, path, None)
        elif isinstance(value, BaseModel):
            inner = self.enter(value, scope)
            for name, info in type(value).model_fields.items():
                item = getattr(value, name)
                key = info.alias or name
                if isinstance(item, Time) and length_bound(info.annotation) is not None and (*path, key) not in seen:
                    self.negative(item, key, self.current, (*path, key))
                self.model_times(item, (*path, key), inner, seen)
        elif isinstance(value, list | tuple | set | frozenset):
            for position, item in enumerate(canonical_items(cast("Collection[Any]", value))):
                self.model_times(item, (*path, position), scope, seen)

    def binding_time(self, binding: Binding, path: FieldPath, scope: _TimeScope) -> None:
        if binding.kind != "time" or not isinstance(binding.value, str):
            return
        needs = binding_needs(binding)
        missing = self.unmet(needs, scope)
        if missing:
            self.add("time.unresolvable", f"closure time {binding.value!r} needs {'; '.join(missing)}",
                     visit=self.current, path=(*path, "value"))

    @staticmethod
    def enter(model: BaseModel, scope: _TimeScope) -> _TimeScope:
        if isinstance(model, Timeline):
            return replace(scope, fps=True, window=scope.window or model.duration is not None)
        if isinstance(model, GfxScene | WebScene):
            return _TimeScope(fps=scope.fps or model.fps is not None, window=True, local=True)
        if isinstance(model, BlenderScene):
            return _TimeScope(fps=scope.fps or model.render.fps is not None, window=True, local=True)
        if isinstance(model, Clip | AudioTrack | OverlayBase):
            return replace(scope, local=True)
        return scope

    def scan_model(self, model: BaseModel, path: FieldPath, scope: _TimeScope) -> None:
        inner = self.enter(model, scope)
        if isinstance(model, OverlayBase):
            self.overlay(model, path)
        elif isinstance(model, CheckUse):
            self.check_use(model.kind, model.params, CHECKPOINT_FAMILIES, path)
        elif isinstance(model, MediaCheck):
            self.check_use(model.kind, model.params, MEDIA_FAMILIES, path)
        elif isinstance(model, BlenderScene):
            self.blender_refs(model, path)
            self.blender_actions(model, path)
            self.scene_length(model.duration, path)
            self.set_value_times(model, path, inner)
        elif isinstance(model, GfxScene):
            self.gfx_refs(model, path)
            self.gfx_actions(model, path)
            self.scene_length(model.duration, path)
            self.set_value_times(model, path, inner)
        elif isinstance(model, WebScene):
            self.scene_length(model.duration, path)
        elif isinstance(model, EventRef):
            self.event_ref(model, path)
        elif isinstance(model, Binding):
            self.binding_time(model, path, scope)
        placed = isinstance(model, Clip | AudioTrack)
        owns_params = isinstance(model, CheckUse | MediaCheck)
        for name, info in type(model).model_fields.items():
            if owns_params and name == "params":
                continue
            value = getattr(model, name)
            key = info.alias or name
            if isinstance(value, Time) and length_bound(info.annotation) is not None:
                self.negative(value, key, self.current, (*path, key))
            self.named_clock(info.metadata, value, inner, (*path, key))
            child = scope if placed and name in ("start", "src") else inner
            self.scan(value, ref_kind_of(info), (*path, key), child)

    def named_clock(self, metadata: Sequence[Any], value: Any, scope: _TimeScope, path: FieldPath) -> None:
        needs = field_needs(metadata, value)
        if needs.empty:
            return
        missing = self.unmet(needs, scope)
        if missing:
            self.add("time.unresolvable", f"{value!r} names {'; '.join(missing)}", visit=self.current, path=path)

    def overlay(self, overlay: OverlayBase, path: FieldPath) -> None:
        visit = self.current
        if overlay.style is not None:
            self.preset("style", overlay.style, visit, (*path, "style"))
        renderer = OVERLAY_RENDERER_BY_OP.get(visit.node.op) if visit is not None else None
        if renderer is None:
            return
        try:
            spec = self.registry.catalog("overlay", overlay.kind)
        except RegistryError as error:
            self.add("overlay.renderer", str(error), visit=visit, path=(*path, "kind"))
            return
        if renderer not in spec.renderers:
            self.add("overlay.renderer", f"overlay {overlay.kind!r} has no {renderer} renderer; it renders "
                     f"{_shown_ids(sorted(spec.renderers))}", visit=visit, path=(*path, "kind"))
        elif (renderer == "live" and spec.platforms is not None and self.platform is not None
              and self.platform not in spec.platforms):
            self.add("overlay.renderer", f"overlay {overlay.kind!r} draws live on "
                     f"{', '.join(sorted(spec.platforms))}, not on {self.platform}", visit=visit,
                     path=(*path, "kind"))

    def set_value_times(self, model: GfxScene | BlenderScene, path: FieldPath, scope: _TimeScope) -> None:
        for position, typed in scene_set_values(model):
            raw = model.triggers[position].action
            value = raw.value if isinstance(raw, SetProp | BlenderSetProp) else None
            if isinstance(value, dict) and self.decoder.models(value):
                continue
            self.model_times(typed, (*path, "triggers", position, "action", "value"), scope, set())

    def scene_problem(self, code: str, problem: str | None, path: FieldPath) -> None:
        if problem is not None:
            self.add(code, problem, visit=self.current, path=path)

    def blender_actions(self, scene: BlenderScene, path: FieldPath) -> None:
        nodes: dict[str, NodeBase] = {}
        for node in scene.nodes:
            nodes.setdefault(node.id, node)
        for position, node in enumerate(scene.nodes):
            if isinstance(node, CameraNode):
                for field in ("look_at", "focus"):
                    if getattr(node, field) == node.id:
                        self.add("scene.camera", f"camera {node.id!r} names itself as its {field}; name another "
                                 "node of the scene", visit=self.current, path=(*path, "nodes", position, field))
        camera = nodes.get(scene.camera) if scene.camera is not None else None
        if camera is not None and not isinstance(camera, CameraNode):
            self.add("scene.camera", f"the scene camera {scene.camera!r} is a {type(camera).__name__}, not a camera; "
                     "name a node built with camera()", visit=self.current, path=(*path, "camera"))
        animated: set[tuple[str, str]] = set()
        for position, animation in enumerate(scene.animations):
            where = (*path, "animations", position)
            pair = (animation.node, animation.prop)
            if pair in animated:
                self.add("scene.duplicate", f"node {animation.node!r} animates {animation.prop!r} more than once; "
                         "one animation per node and property holds all its keyframes", visit=self.current,
                         path=(*where, "prop"))
            animated.add(pair)
            node = nodes.get(animation.node)
            if node is None:
                continue
            problem = prop_problem(type(node), animation.prop, BLENDER_FIXED_FIELDS, "animate", custom=True)
            self.scene_problem("scene.prop", problem, (*where, "prop"))
            if problem is not None:
                continue
            for index, key in enumerate(animation.keys):
                problem = (keyframe_problem(node, animation.prop, key.v) if animation.prop in type(node).model_fields
                           else _ref_problem(key.v))
                self.scene_problem("scene.value", problem, (*where, "keys", index, "v"))
        for position, trigger in enumerate(scene.triggers):
            node = nodes.get(trigger.node) if trigger.node is not None else None
            if node is not None:
                self.blender_action(node, trigger.action, nodes, (*path, "triggers", position, "action"))

    def blender_action(self, node: NodeBase, action: BaseModel, nodes: Mapping[str, NodeBase],
                       where: FieldPath) -> None:
        if isinstance(action, BlenderPulse):
            self.scene_problem("scene.prop", prop_problem(type(node), action.prop, BLENDER_FIXED_FIELDS, "pulse",
                                                          custom=True), (*where, "prop"))
        elif isinstance(action, BlenderSetProp):
            problem = prop_problem(type(node), action.prop, BLENDER_FIXED_FIELDS, "set", custom=True)
            self.scene_problem("scene.prop", problem, (*where, "prop"))
            if problem is not None:
                return
            _typed, problem = typed_value(node, action.prop, action.value)
            self.scene_problem("scene.value", problem, (*where, "value"))
            if problem is None and action.prop in BLENDER_REFERENCE_FIELDS and isinstance(action.value, str):
                if action.value == node.id:
                    self.add("scene.value", f"node {node.id!r} cannot name itself as its {action.prop}",
                             visit=self.current, path=(*where, "value"))
                elif action.value not in nodes:
                    self.scene_ref(action.prop, action.value, set(nodes), (*where, "value"))

    def gfx_actions(self, scene: GfxScene, path: FieldPath) -> None:
        layers = gfx_layer_index(scene.layers)
        for position, trigger in enumerate(scene.triggers):
            layer = layers.get(trigger.layer)
            if layer is None:
                continue
            action = trigger.action
            where = (*path, "triggers", position, "action")
            use: PropUse = "pulse" if isinstance(action, GfxPulse) else "animate" if isinstance(action, Animate) else "set"
            problem = prop_problem(type(layer), action.prop, GFX_FIXED_FIELDS, use, custom=False)
            self.scene_problem("scene.prop", problem, (*where, "prop"))
            if problem is not None or isinstance(action, GfxPulse):
                continue
            if isinstance(action, SetProp):
                _typed, problem = typed_value(layer, action.prop, action.value)
                self.scene_problem("scene.value", problem, (*where, "value"))
                continue
            refs = next((_ref_problem(key.v) for key in action.keys if _ref_problem(key.v) is not None), None)
            keys: JsonValue = {"keys": [key.model_dump(mode="json", by_alias=True, exclude_none=True)
                                        for key in action.keys]}
            _typed, problem = (None, refs) if refs is not None else typed_value(layer, action.prop, keys)
            self.scene_problem("scene.value", problem, (*where, "keys"))

    def scene_length(self, duration: Time | None, path: FieldPath) -> None:
        clock = self.plan.clock
        visit = self.current
        renders = [node for node in self.plan.nodes if node.op in SCENE_RENDER_OPS]
        own = self.plan.kind == "scene" and visit is not None and bool(renders) and renders[-1] is visit.node
        if duration is None and clock.window is None and clock.duration is None and not own:
            self.add("scene.length", "the embedded scene sets no duration and the plan clock is open (no window and "
                     "no duration), so its length would come from the clips that show it, which its render hash does "
                     "not cover; give the scene duration= or the plan a duration or window", visit=visit,
                     path=(*path, "duration"))

    def scene_ref(self, what: str, value: str | None, known: set[str], path: FieldPath) -> None:
        if value is not None and value not in known:
            self.add("scene.ref", f"{what} {value!r} names nothing in the scene; ids: {_shown_ids(sorted(known))}",
                     visit=self.current, path=path)

    def blender_refs(self, scene: BlenderScene, path: FieldPath) -> None:
        ids = {node.id for node in scene.nodes}
        order = [node.id for node in scene.nodes]
        for position in repeated_ids(order):
            self.add("scene.duplicate", f"Blender node id {order[position]!r} is used by more than one node; give "
                     "each node its own id", visit=self.current, path=(*path, "nodes", position, "id"))
        parents: dict[str, list[str]] = {}
        for node in scene.nodes:
            if node.parent is not None and node.parent in ids:
                parents.setdefault(node.id, []).append(node.parent)
        cycle = find_cycle(order, lambda node_id: parents.get(node_id, []))
        if cycle is not None:
            self.add("scene.cycle", f"the parents of Blender nodes form a cycle: {' -> '.join(cycle)}; parent each "
                     "node under one that does not descend from it", visit=self.current, path=(*path, "nodes"))
        for position, node in enumerate(scene.nodes):
            where = (*path, "nodes", position)
            self.scene_ref("parent", node.parent, ids, (*where, "parent"))
            if isinstance(node, CameraNode):
                self.scene_ref("look_at", node.look_at, ids, (*where, "look_at"))
                self.scene_ref("focus", node.focus, ids, (*where, "focus"))
        for position, texture in enumerate(scene.textures):
            self.scene_ref("texture node", texture.node, ids, (*path, "textures", position, "node"))
        for position, animation in enumerate(scene.animations):
            self.scene_ref("animation node", animation.node, ids, (*path, "animations", position, "node"))
        for position, trigger in enumerate(scene.triggers):
            self.scene_ref("trigger node", trigger.node, ids, (*path, "triggers", position, "node"))
        self.scene_ref("camera", scene.camera, ids, (*path, "camera"))

    def gfx_refs(self, scene: GfxScene, path: FieldPath) -> None:
        layers = list(self.gfx_layers(scene.layers, (*path, "layers")))
        ids = {layer.id for layer, _where in layers}
        order = [layer.id for layer, _where in layers]
        for position in repeated_ids(order):
            layer, where = layers[position]
            self.add("scene.duplicate", f"gfx layer id {layer.id!r} is used by more than one layer; give each layer "
                     "its own id", visit=self.current, path=(*where, "id"))
        following: dict[str, list[str]] = {}
        for layer, _where in layers:
            steps = following.setdefault(layer.id, [])
            if isinstance(layer, GroupLayer):
                steps.extend(child.id for child in layer.children)
            if layer.mask is not None and layer.mask in ids:
                steps.append(layer.mask)
        cycle = find_cycle(order, lambda layer_id: following.get(layer_id, []))
        if cycle is not None:
            self.add("scene.cycle", f"gfx masks and groups form a cycle: {' -> '.join(cycle)}; a layer cannot be "
                     "masked by a layer that is masked by it or by a group that contains it", visit=self.current,
                     path=(*path, "layers"))
        for layer, where in layers:
            self.scene_ref("mask", layer.mask, ids, (*where, "mask"))
        for position, trigger in enumerate(scene.triggers):
            self.scene_ref("trigger layer", trigger.layer, ids, (*path, "triggers", position, "layer"))

    def event_ref(self, event: EventRef, path: FieldPath) -> None:
        producer = self.by_id.get(event.source.ref)
        if producer is None:
            return
        problem = event_source_problem(event.name, event.source.ref, producer.node.op, producer.node.args)
        if problem is not None:
            self.add("scene.event", problem, visit=self.current, path=path)

    def gfx_layers(self, layers: Sequence[LayerBase], path: FieldPath) -> Iterator[tuple[LayerBase, FieldPath]]:
        for position, layer in enumerate(layers):
            where = (*path, position)
            yield layer, where
            if isinstance(layer, GroupLayer):
                yield from self.gfx_layers(layer.children, (*where, "children"))

    def target(self, target: Target, path: FieldPath, scope: _TimeScope) -> None:
        known = True
        for sub, where in self.subtargets(target, path):
            kind = self.kinds.get(sub.by)
            if kind is None:
                known = False
                self.add("target.kind", f"target kind {sub.by!r} is not registered; known kinds: "
                         f"{', '.join(sorted(self.kinds))} (plugin kinds are registered with @eks.target_kind)",
                         visit=self.current, path=(*where, "by"))
                continue
            try:
                args = kind.args.model_validate(dict(sub.args))
            except ValidationError as error:
                self.add("target.args", f"{sub.by} target args do not validate: {_validation_summary(error)}",
                         visit=self.current, path=(*where, "args"))
                continue
            self.scan(args, None, (*where, "args"), scope)
        if not known or self.surface is None:
            return
        for problem in check_target(target, self.surface, self.kinds):
            self.add("target.surface", problem, visit=self.current, path=path)

    def subtargets(self, target: Target, path: FieldPath) -> Iterator[tuple[Target, FieldPath]]:
        yield target, path
        if target.within_ is not None:
            yield from self.subtargets(target.within_, (*path, "within"))
        if target.near_ is not None:
            yield from self.subtargets(target.near_, (*path, "near"))

    def negative_errors(self, error: ValidationError, visit: _Visit | None, path: FieldPath) -> list[ErrorDetails]:
        rest: list[ErrorDetails] = []
        for item in error.errors():
            if item["type"] != NEGATIVE_ERROR:
                rest.append(item)
                continue
            where = (*path, *(step for step in item["loc"]))
            self.add("time.negative", f"{_dotted(tuple(item['loc'])) or 'value'} is "
                     f"{_shown_input(item['input'])}; lengths and durations cannot be negative",
                     visit=visit, path=where)
        return rest

    def negative(self, value: Time, what: str, visit: _Visit | None, path: FieldPath) -> None:
        if value.is_concrete and value.ticks < 0:
            self.add("time.negative", f"{what} is {format_time(value)!r}; durations cannot be negative",
                     visit=visit, path=path)

    def tempo(self) -> TempoMap | None:
        return self.plan.clock.tempo

    def external(self) -> bool:
        tempo = self.tempo()
        return tempo is not None and tempo.external and self.plan.kind == SCENE_KIND

    def has_tempo(self) -> bool:
        tempo = self.tempo()
        if tempo is None:
            return False
        if tempo.bpm is not None or self.external():
            return True
        return tempo.source is not None and len(self.tempo_trackers) == 1

    def analysed_downbeats(self) -> bool:
        tempo = self.tempo()
        return (tempo is None or tempo.bpm is not None or self.external() or not self.has_tempo()
                or "downbeat" in self.tempo_streams)

    def stream_known(self, stream: str) -> bool:
        return self.external() or stream in self.streams or (stream in BEAT_STREAMS and stream in self.tempo_streams)

    def unmet(self, needs: TimeNeeds, scope: _TimeScope) -> list[str]:
        clock = self.plan.clock
        external = self.external()
        missing: list[str] = []
        if needs.fps and not scope.fps:
            missing.append("an fps for frame times, which neither the plan clock nor the enclosing timeline or "
                           "scene sets")
        if needs.tempo and not self.has_tempo():
            missing.append("a tempo for beat, downbeat and bar times, which the clock does not have; set bpm= or "
                           "analyse the song with .beats(...)")
        if needs.downbeat and not self.analysed_downbeats():
            tracker = self.tempo_trackers[0].name
            missing.append(f"downbeats, which the tempo source's beat tracker {tracker!r} does not track; add "
                           "'downbeat' to its streams")
        if needs.window and not (scope.window or scope.local):
            missing.append("a clock window or duration for 'end'")
        missing.extend(f"cue {cue!r} (declared cues: {_shown_ids(sorted(clock.cues))})"
                       for cue in sorted(needs.cues) if cue not in clock.cues and not external)
        missing.extend(f"marker stream {stream!r} (media.markers streams: {_shown_ids(sorted(self.streams))})"
                       for stream in sorted(needs.markers) if not self.stream_known(stream))
        missing.extend(f"words of {source!r} (speech-to-text sources: {_shown_ids(sorted(self.word_sources))})"
                       for source in sorted(needs.words) if source not in self.word_sources and not external)
        if needs.default_words and not self.word_sources and not external:
            missing.append("words, but the plan has no speech-to-text markers")
        elif needs.default_words and len(self.word_sources) > 1 and not external:
            sources = sorted(self.word_sources)
            missing.append(f"words of one speech-to-text source, but the plan has {len(sources)} "
                           f"({_shown_ids(sources)}); name the source, as in word:{sources[0]}:'text'")
        return missing

    def time(self, value: Time | Span | TimeSet, scope: _TimeScope, visit: _Visit | None, path: FieldPath,
             what: str | None) -> None:
        missing = self.unmet(value.needs(), scope)
        if missing:
            label = f"{what} {_shown_time(value)}" if what else _shown_time(value)
            self.add("time.unresolvable", f"{label} needs {'; '.join(missing)}", visit=visit, path=path)
            return
        span = value.window if isinstance(value, TimeSet) else value
        if isinstance(span, Span):
            self.span_order(span, visit, (*path, "window") if span is not value else path, what)

    def span_order(self, span: Span, visit: _Visit | None, path: FieldPath, what: str | None) -> None:
        context = self.cached_static_context()
        if context is None:
            return
        try:
            start = context.resolve(span.start)
            end = context.resolve(span.end)
        except TimeError:
            return
        if end < start:
            label = f"{what} {_shown_time(span)}" if what else _shown_time(span)
            self.add("time.reversed", f"{label} ends before it starts ({format_time(span.end)!r} resolves to {end} "
                     f"ticks, {format_time(span.start)!r} to {start}); swap the times or fix the anchors",
                     visit=visit, path=path)

    def check_evidence(self) -> None:
        groups: dict[tuple[str, str, str, str | None], list[_Evidence]] = {}
        for visit in self.visits:
            found = self.evidence(visit)
            if found is not None:
                key = (visit.session_key, found.what, found.name, found.viewport)
                groups.setdefault(key, []).append(found)
                self.repeated_evidence(found)
            if visit.node.op == DYNAMIC_OP:
                self.dynamic_evidence(visit)
        for items in groups.values():
            for position, item in enumerate(items):
                earlier = next((other for other in items[:position]
                                if not self.exclusive(other.visit, item.visit)), None)
                if earlier is None:
                    continue
                severe = item.what == "cassette" or item.visual or earlier.visual
                where = f" in viewport {item.viewport!r}" if item.viewport is not None else ""
                consequence = ("its visual golden and screenshots are keyed by label and viewport" if severe
                               and item.what == "checkpoint" else "cassettes are artifacts named by it"
                               if item.what == "cassette" else "the report and artifacts cannot tell them apart")
                self.add("evidence.duplicate", f"{item.what} {item.name!r}{where} is also used by node "
                         f"{earlier.visit.node.id!r}; {consequence}, so give each one its own name", visit=item.visit,
                         path=(EVIDENCE_NAMES[item.visit.node.op][1],), severity="error" if severe else "warning")

    @staticmethod
    def runtime_loop(visit: _Visit) -> Node | None:
        return next((frame.container for frame in reversed(visit.frames) if frame.container.op == EACH_OP), None)

    def repeated_evidence(self, found: _Evidence) -> None:
        loop = self.runtime_loop(found.visit)
        if loop is None:
            return
        severe = found.what == "cassette" or found.visual
        self.add("evidence.duplicate", f"{found.what} {found.name!r} is a literal name inside eks.each {loop.id!r}, so "
                 f"every iteration writes and compares the same {found.what}; build the name from the loop item, such "
                 f"as f\"{found.name}-{{item.id}}\"", visit=found.visit,
                 path=(EVIDENCE_NAMES[found.visit.node.op][1],), severity="error" if severe else "warning")

    def dynamic_evidence(self, visit: _Visit) -> None:
        fn = visit.node.args.get("fn")
        source = fn.get("source") if isinstance(fn, dict) else None
        tree = self.parsed_source(source) if isinstance(source, str) else None
        if tree is None:
            return
        loop = self.runtime_loop(visit)
        for call, looped in _calls_in_loops(tree, False):
            found = _dynamic_evidence_name(call)
            if found is None or not (looped or loop is not None):
                continue
            what, name = found
            where = f"inside eks.each {loop.id!r}" if loop is not None else "in a loop"
            severe = what == "cassette" or (what == "checkpoint" and self.dynamic_visual(call, visit.session))
            self.add("evidence.duplicate", f"line {call.lineno} of the eks.dynamic function names {what} {name!r} "
                     f"literally and runs {where}, so every run writes and compares the same {what}; build the name "
                     "from the value that varies", visit=visit, path=("fn", "source"),
                     severity="error" if severe else "warning")

    def dynamic_visual(self, call: ast.Call, session: SessionSpec | None) -> bool:
        checks = next((keyword.value for keyword in call.keywords if keyword.arg == "checks"), None)
        if checks is None:
            return session is not None and any(self.family_of(use.kind) == VISUAL_FAMILY
                                               for use in (*session.defaults, *session.checks))
        return any(isinstance(item, ast.Constant) and isinstance(item.value, str)
                   and self.family_of(item.value) == VISUAL_FAMILY for item in ast.walk(checks))

    def family_of(self, kind: str) -> str | None:
        try:
            return self.registry.check(kind).family
        except RegistryError:
            return None

    def evidence(self, visit: _Visit) -> _Evidence | None:
        node = visit.node
        known = EVIDENCE_NAMES.get(node.op)
        if known is None:
            return None
        what, field = known
        name = node.args.get(field)
        if name is None:
            default = DEFAULT_EVIDENCE_NAMES.get(node.op)
            name = node.args.get(default) if default == "kind" else default
        if not isinstance(name, str):
            return None
        viewport: str | None = None
        visual = False
        if node.op == CHECKPOINT_OP:
            chosen = node.args.get("viewport")
            viewport = chosen if isinstance(chosen, str) else visit.session.viewport if visit.session else None
            visual = self.visual_checkpoint(node, visit.session)
        return _Evidence(visit, what, name, viewport, visual)

    def visual_checkpoint(self, node: Node, session: SessionSpec | None) -> bool:
        own = node.args.get("checks")
        if isinstance(own, list):
            kinds = [use.get("kind") for use in cast("list[Any]", own) if isinstance(use, dict)]
        else:
            kinds = [use.kind for use in (*session.defaults, *session.checks)] if session is not None else []
        for kind in kinds:
            if isinstance(kind, str):
                try:
                    if self.registry.check(kind).family == VISUAL_FAMILY:
                        return True
                except RegistryError:
                    continue
        return False

    @staticmethod
    def exclusive(first: _Visit, second: _Visit) -> bool:
        for one, other in zip(first.frames, second.frames, strict=False):
            if one.container.id != other.container.id:
                return False
            if one.branch != other.branch:
                return one.container.op == WHEN_OP
        return False

    def check_hashes(self) -> None:
        plan = self.plan
        if not plan.hash and not any(visit.node.hash for visit in self.visits):
            return
        versions = {visit.node.op: self.executor_version(visit.node.op) for visit in self.visits}
        fresh = plan.model_copy(deep=True)
        try:
            hash_plan(fresh, executor_versions=versions)
        except (PlanError, ValueError, TypeError):
            return
        fresh_hashes = [node.hash for node, _ancestors in fresh.walk()]
        stored = {visit.node.id: visit.node.hash for visit in self.visits}
        resources = {resource.id: resource_hash(resource, self.registry) for resource in plan.resources}
        for visit, recomputed in zip(self.visits, fresh_hashes, strict=True):
            if visit.node.hash == recomputed or not self.registry.has_op(visit.node.op):
                continue
            items = tuple(frame.container.id for frame in visit.frames if frame.container.op == EACH_OP)
            try:
                local = node_hash(visit.node, refs=stored, resources=resources, items=items,
                                  executor_version=versions[visit.node.op],
                                  clock=stored_clock_payload(plan, visit.node, self.registry))
            except PlanError:
                local = ""
            if local != visit.node.hash:
                self.add("plan.hash", f"the stored node hash {visit.node.hash or '(empty)'!r} does not match the "
                         "node's content; it was edited after hashing, plan it again", visit=visit)
        if fresh.hash == plan.hash:
            return
        try:
            consistent = plan_hash(plan, self.registry) == plan.hash
        except PlanError:
            consistent = False
        if not consistent:
            self.add("plan.hash", f"the stored plan hash {plan.hash or '(empty)'!r} does not match the plan; "
                     f"recomputed {fresh.hash!r}")

    def executor_version(self, op: str) -> str:
        return self.registry.op(op).executor_version if self.registry.has_op(op) else ""


class Validator:
    def __init__(
        self,
        registry: Registry | None = None,
        *,
        personas: PersonaLookup | None = None,
        seeds: SeedLookup | None = None,
        apps: AppLookup | None = None,
        versions: Mapping[str, str] | None = None,
        backends: BackendNames | None = None,
    ) -> None:
        self.registry = registry if registry is not None else default_registry()
        self.seeds = seeds
        self.apps = apps
        self.backends: frozenset[str] | None = frozenset(backends) if backends is not None else None
        self.personas = personas if personas is not None or seeds is None else _personas_of(seeds)
        self.versions: Mapping[str, str] = dict(versions) if versions is not None else self.registry.versions()

    def validate(self, plan: Plan) -> Report:
        with using_registry(self.registry):
            return _Run(self, plan).run()


def _personas_of(seeds: SeedLookup) -> PersonaLookup:
    def personas(name: str) -> Sequence[str] | None:
        info = seeds(name)
        return None if info is None else info.personas

    return personas


def validate(
    plan: Plan,
    *,
    registry: Registry | None = None,
    personas: PersonaLookup | None = None,
    seeds: SeedLookup | None = None,
    apps: AppLookup | None = None,
    versions: Mapping[str, str] | None = None,
    backends: BackendNames | None = None,
) -> Report:
    return Validator(registry, personas=personas, seeds=seeds, apps=apps, versions=versions,
                     backends=backends).validate(plan)


__all__ = [
    "ISSUE_CODES",
    "AppLookup",
    "BackendNames",
    "Issue",
    "PersonaLookup",
    "Report",
    "SeedInfo",
    "SeedLookup",
    "Severity",
    "Validator",
    "validate",
]
