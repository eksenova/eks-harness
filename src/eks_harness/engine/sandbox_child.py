from __future__ import annotations

import ast
import builtins
import linecache
import os
import signal
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from pathlib import Path
from types import CodeType, FrameType, FunctionType
from typing import IO, Any, cast

from pydantic import JsonValue, ValidationError

import eks
from eks import by, expect, t
from eks.ref import RefUsageError
from eks.session import Session
from eks.tracing import RESOURCE_FIELDS, Bound, TraceContext, TraceError, tracing
from eks.workspace import workspace_registry
from eks_harness.config import load as load_config
from eks_harness.engine.registries import plugin_digest
from eks_harness.engine.sandbox import (
    PROTOCOL,
    SandboxError,
    SandboxLimits,
    SandboxRequest,
    SandboxResponse,
    subplan_name,
)
from eks_harness.ir.contributions import (
    contributions,
    node_contributions,
    resource_contributions,
    session_scope,
    walk_scoped,
)
from eks_harness.ir.ops.ctl import SANDBOX_BUILTINS, blocked_attribute
from eks_harness.ir.plan import (
    Binding,
    FunctionSource,
    Node,
    Plan,
    PlanKind,
    SessionSpec,
)
from eks_harness.ir.registry import Provenance, Registry, RegistryError, using_registry
from eks_harness.ir.target import Target, TargetError, validate_args
from eks_harness.ir.time import TimeError, parse_time
from eks_harness.paths import resolve_paths
from eks_harness.plugins.host import PluginHost

if sys.platform != "win32":
    import resource

BLOCKED_EVENTS: frozenset[str] = frozenset({
    "socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo", "socket.gethostbyname",
    "socket.gethostbyaddr", "socket.sethostname", "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
    "os.spawn", "os.fork", "os.forkpty", "os.kill", "os.killpg", "signal.pthread_kill", "os.remove", "os.rename",
    "os.rmdir", "os.mkdir", "os.unlink", "os.truncate", "os.chmod", "os.chown", "os.chflags", "os.symlink", "os.link",
    "os.utime", "os.setxattr", "os.removexattr", "os.putenv", "os.unsetenv", "os.chdir", "shutil.rmtree",
    "shutil.copyfile", "shutil.move", "shutil.chown", "sqlite3.connect", "sqlite3.connect/handle",
    "resource.setrlimit", "resource.prlimit", "ctypes.dlopen", "ctypes.cdata", "urllib.Request", "webbrowser.open",
})
SOURCE_PREFIX = "<eks-dynamic:"
_DYNAMIC_OP = "ctl.dynamic"
_MIB = 1024 * 1024
_REQUIRES_ORDER = 1_000_000
_WRITE_MODES = frozenset("wax+")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_FACTORIES: Mapping[str, Any] = {"by": by, "t": t, "expect": expect}
_LIMIT_KEYS = ("cpu", "memory", "file_size", "open_files", "core")
_AUTHORING_ERRORS: tuple[type[BaseException], ...] = (
    TraceError, RefUsageError, RegistryError, TargetError, TimeError, ValidationError,
)
_VIOLATIONS: list[str] = []


class _Violation(PermissionError):
    pass


class _RegistryChanged(RuntimeError):
    pass


class _CpuLimit(RuntimeError):
    pass


class _ProtocolError(ValueError):
    pass


class _Failed(Exception):
    def __init__(self, error: SandboxError) -> None:
        super().__init__(error.message)
        self.error = error


def read_request(data: bytes) -> SandboxRequest:
    if not data.strip():
        raise _ProtocolError("the sandbox received an empty request on stdin")
    request = SandboxRequest.model_validate_json(data)
    if request.protocol != PROTOCOL:
        raise _ProtocolError(f"the request speaks protocol {request.protocol}, this sandbox speaks {PROTOCOL}")
    return request


def build_registry(request: SandboxRequest) -> Registry:
    tree = Path(request.tree) if request.tree is not None else None
    registry = workspace_registry(tree, plugins=request.plugins, isolated=True)
    if request.digests:
        active = _active_digests(tree) if request.plugins else {}
        for plugin_id, expected in sorted(request.digests.items()):
            found = active.get(plugin_id)
            if found is None:
                raise _RegistryChanged(
                    f"plugin {plugin_id!r} was active with digest {expected} when the run was admitted, but it is "
                    "not active in the sandbox; start the run again so it plans against the current plugins")
            if found != expected:
                raise _RegistryChanged(
                    f"plugin {plugin_id!r} changed since the run was admitted: digest {found} instead of {expected}; "
                    "start the run again so it plans against the current plugin code")
    return registry


def _active_digests(tree: Path | None) -> dict[str, str]:
    paths = resolve_paths()
    host = PluginHost(paths, load_config(paths))
    digests: dict[str, str] = {}
    for record in host.active(tree):
        manifest = record.manifest
        if manifest is not None and manifest.modules:
            digests[manifest.id] = plugin_digest(record)
    return digests


def _set_limit(name: str, rid: int, value: int, extra: int = 0) -> str | None:
    try:
        _soft, hard = resource.getrlimit(rid)
        soft = value if hard == resource.RLIM_INFINITY else min(value, hard)
        ceiling = value + extra if hard == resource.RLIM_INFINITY else min(value + extra, hard)
        resource.setrlimit(rid, (soft, ceiling))
    except (ValueError, OSError) as error:
        return f"{name}: {error}"
    return None


def _limit_report(candidates: tuple[tuple[str, int], ...], value: int, extra: int = 0) -> dict[str, JsonValue]:
    errors: list[str] = []
    for name, rid in candidates:
        error = _set_limit(name, rid, value, extra)
        if error is None:
            return {"resource": name, "value": value, "applied": True}
        errors.append(error)
    return {"resource": candidates[0][0], "value": value, "applied": False, "error": "; ".join(errors)}


def _address_space_enforced(limit_bytes: int) -> bool:
    try:
        probe = bytearray(limit_bytes)
    except MemoryError:
        return True
    del probe
    return False


def _on_cpu_limit(signum: int, frame: FrameType | None) -> None:
    del signum, frame
    raise _CpuLimit("the dynamic function used up its CPU time limit")


def apply_limits(limits: SandboxLimits) -> dict[str, JsonValue]:
    if sys.platform == "win32":
        return {key: {"applied": False, "error": "resource limits need a POSIX system"} for key in _LIMIT_KEYS}
    memory_bytes = limits.memory_mb * _MIB
    memory = _limit_report((("RLIMIT_AS", resource.RLIMIT_AS), ("RLIMIT_DATA", resource.RLIMIT_DATA)), memory_bytes)
    memory["enforced"] = (memory["applied"] is True and memory["resource"] == "RLIMIT_AS"
                          and _address_space_enforced(memory_bytes))
    report: dict[str, JsonValue] = {
        "cpu": _limit_report((("RLIMIT_CPU", resource.RLIMIT_CPU),), limits.cpu_seconds, extra=1),
        "memory": memory,
        "file_size": _limit_report((("RLIMIT_FSIZE", resource.RLIMIT_FSIZE),), 0),
        "open_files": _limit_report((("RLIMIT_NOFILE", resource.RLIMIT_NOFILE),), limits.open_files),
        "core": _limit_report((("RLIMIT_CORE", resource.RLIMIT_CORE),), 0),
    }
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    signal.signal(signal.SIGXCPU, _on_cpu_limit)
    return report


def _unshare_network() -> bool:
    if sys.platform == "linux":
        try:
            os.unshare(os.CLONE_NEWNET)
        except OSError:
            return False
        return True
    return False


def install_guard(limits: SandboxLimits) -> dict[str, JsonValue]:
    report = apply_limits(limits)
    network = "audit+netns" if _unshare_network() else "audit"
    sys.addaudithook(audit_hook)
    file_size = report.get("file_size")
    fsize = isinstance(file_size, dict) and file_size.get("applied") is True
    return {"limits": report, "network": network, "files": "audit+fsize" if fsize else "audit"}


def _writes(args: tuple[Any, ...]) -> bool:
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else None
    if isinstance(mode, str) and any(letter in _WRITE_MODES for letter in mode):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if event in BLOCKED_EVENTS or (event == "open" and _writes(args)):
        target = f" {args[0]!r}" if args and isinstance(args[0], str | bytes | int) else ""
        message = f"the dynamic function tried {event}{target}, which the sandbox does not allow"
        _VIOLATIONS.append(message)
        raise _Violation(message)


def _function_def(module: ast.Module, name: str, filename: str) -> None:
    body = module.body
    if len(body) != 1 or not isinstance(body[0], ast.FunctionDef) or body[0].name != name:
        raise TraceError(f"the source of {filename} must be exactly one plain def named {name!r}")
    if body[0].decorator_list:
        raise TraceError(f"the dynamic function {name!r} must not carry decorators in its captured source")
    if body[0].type_params:
        raise TraceError(f"the dynamic function {name!r} must not declare type parameters")


def compile_function(name: str, source: str, node_id: str) -> CodeType:
    filename = f"{SOURCE_PREFIX}{node_id}>"
    try:
        module = ast.parse(source, filename=filename)
    except SyntaxError as error:
        raise TraceError(f"cannot parse the dynamic function {name!r}: {error}") from error
    _function_def(module, name, filename)
    code = compile(module, filename, "exec", dont_inherit=True)
    linecache.cache[filename] = (len(source), None, source.splitlines(keepends=True), filename)
    return code


def _session_of(plan: Plan, var: str, key: str, sessions: Mapping[str, Any]) -> Any:
    session = sessions.get(key)
    if session is None:
        known = ", ".join(sorted(sessions)) or "none"
        raise TraceError(
            f"closure variable {var!r} names session {key!r}, but the dynamic node of {plan.name!r} runs on "
            f"session {known}")
    return session


def _bound(plan: Plan, var: str, binding: Binding, registry: Registry, sessions: Mapping[str, Any]) -> Any:
    value = binding.value
    match binding.kind:
        case "session":
            if not isinstance(value, str):
                raise TraceError(f"closure variable {var!r}: a session binding holds a session key, got {value!r}")
            return _session_of(plan, var, value, sessions)
        case "namespace":
            key, dot, attribute = value.partition(".") if isinstance(value, str) else ("", "", "")
            if not dot or not attribute or blocked_attribute(attribute):
                raise TraceError(
                    f"closure variable {var!r}: a namespace binding is '<session key>.<attribute>', got {value!r}")
            session = _session_of(plan, var, key, sessions)
            found = getattr(session, attribute, None)
            if not isinstance(found, Bound) or found.session is not session:
                raise TraceError(
                    f"closure variable {var!r}: a namespace binding names a session namespace such as ui or api, "
                    f"got {value!r}")
            return found
        case "literal":
            return value
        case "time":
            if not isinstance(value, str):
                raise TraceError(f"closure variable {var!r}: a time binding holds a time string, got {value!r}")
            return parse_time(value)
        case "target":
            return validate_args(Target.model_validate(value), registry.target_kinds())
        case "factory":
            if not isinstance(value, str) or value not in _FACTORIES:
                raise TraceError(f"closure variable {var!r}: a factory binding names by, t or expect, got {value!r}")
            return _FACTORIES[value]


def bindings(plan: Plan, freevars: Mapping[str, Binding], registry: Registry,
             sessions: Mapping[str, Any]) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "__builtins__": {name: getattr(builtins, name) for name in SANDBOX_BUILTINS},
        "eks": eks,
        "by": by,
        "t": t,
        "expect": expect,
    }
    for var, binding in freevars.items():
        scope[var] = _bound(plan, var, binding, registry, sessions)
    return scope


def _def_code(module: CodeType, name: str) -> CodeType:
    for const in module.co_consts:
        if isinstance(const, CodeType) and const.co_name == name:
            return const
    raise TraceError(f"the compiled source defines no function {name!r}")


def _require_contributions(ctx: TraceContext) -> None:
    registry = ctx.registry
    session = ctx.session
    found = list(contributions(session, registry)) if session is not None else []
    for node, scope in walk_scoped(ctx.scope().nodes, session_scope(session)):
        found.extend(node_contributions(node, registry, scope=scope) or ())
    for declared in ctx.declared_resources():
        found.extend(resource_contributions(declared, registry))
    for item in found:
        ctx.require(Provenance(item.plugin, item.plugin_version))


def _redeclare(ctx: TraceContext, plan: Plan) -> None:
    for declared in plan.resources:
        identity: dict[str, Any] = dict(declared.params)
        for field in RESOURCE_FIELDS:
            value = getattr(declared, field)
            if value is not None:
                identity[field] = value
        rid = ctx.resource(declared.kind, **identity)
        if rid != declared.id:
            raise TraceError(
                f"resource {declared.id} of plan {plan.name!r} does not match its identity, which gives {rid}; "
                "the parent plan was edited by hand")


def trace_dynamic(request: SandboxRequest, registry: Registry) -> Plan:
    plan = Plan.model_validate(request.plan)
    node = Node.model_validate(request.node)
    if node.op != _DYNAMIC_OP:
        raise TraceError(f"node {node.id} is a {node.op} node, the sandbox runs only {_DYNAMIC_OP}")
    fn = FunctionSource.model_validate(node.args.get("fn"))
    missing = [name for name in fn.params if name not in request.inputs]
    unknown = [name for name in request.inputs if name not in fn.params]
    if missing or unknown:
        raise TraceError(
            f"the inputs of {fn.name} do not match its parameters {list(fn.params)}: missing {missing}, unknown "
            f"{unknown}")
    spec = SessionSpec.model_validate(request.session) if request.session is not None else plan.session
    kind: PlanKind = "flow" if request.session is not None else plan.kind
    code = compile_function(fn.name, fn.source, node.id)
    entry_code = _def_code(code, fn.name)
    ctx = TraceContext(kind=kind, name=subplan_name(plan, node), registry=registry, clock=plan.clock,
                       session=spec, tree=None, entry_code=entry_code)
    ctx.on_finalize(_require_contributions, order=_REQUIRES_ORDER)
    _redeclare(ctx, plan)
    sessions: dict[str, Session] = {spec.key: Session(ctx, spec)} if spec is not None else {}
    scope = bindings(plan, fn.freevars, registry, sessions)
    function = FunctionType(entry_code, scope, fn.name)
    bound = [scope[var] for var, binding in fn.freevars.items() if binding.kind == "session"]
    active = cast("Session", bound[0]) if bound else next(iter(sessions.values()), None)
    with ExitStack() as stack:
        stack.enter_context(using_registry(registry))
        stack.enter_context(tracing(ctx))
        if active is not None:
            stack.enter_context(active.activate())
        result = function(**request.inputs)
        ctx.set_outputs(result)
        return ctx.finalize()


def _source_frames(error: BaseException) -> list[traceback.FrameSummary]:
    return [frame for frame in traceback.extract_tb(error.__traceback__)
            if frame.filename.startswith(SOURCE_PREFIX)]


def _innermost_in_source(error: BaseException) -> bool:
    frames = traceback.extract_tb(error.__traceback__)
    return bool(frames) and frames[-1].filename.startswith(SOURCE_PREFIX)


def _classify(error: Exception) -> SandboxError:
    frames = _source_frames(error)
    lines = [f"line {frame.lineno}, in {frame.name}: {(frame.line or '').strip()}" for frame in frames]
    line = frames[-1].lineno if frames else None
    kind = type(error).__name__
    message = str(error) or kind
    if _VIOLATIONS or isinstance(error, _Violation):
        violation = _VIOLATIONS[0] if _VIOLATIONS else message
        return SandboxError(code="violation", type="PermissionError", message=violation, line=line, traceback=lines)
    if isinstance(error, _RegistryChanged):
        return SandboxError(code="registry_changed", type=kind, message=message)
    if isinstance(error, MemoryError | RecursionError | _CpuLimit):
        detail = "the dynamic function ran out of memory" if isinstance(error, MemoryError) else message
        return SandboxError(code="limit", type=kind, message=detail, line=line, traceback=lines)
    if isinstance(error, _AUTHORING_ERRORS):
        return SandboxError(code="trace_error", type=kind, message=message, line=line, traceback=lines)
    if _innermost_in_source(error):
        return SandboxError(code="user_error", type=kind, message=message, line=line, traceback=lines)
    return SandboxError(code="trace_error", type=kind, message=message, line=line, traceback=lines)


def _attempt(request: SandboxRequest, guard: Callable[[SandboxLimits], dict[str, JsonValue]],
             isolation: dict[str, JsonValue]) -> Plan:
    try:
        registry = build_registry(request)
        isolation.update(guard(request.limits))
        return trace_dynamic(request, registry)
    except Exception as error:
        raise _Failed(_classify(error)) from error


def run(request: SandboxRequest, *,
        guard: Callable[[SandboxLimits], dict[str, JsonValue]] = install_guard) -> SandboxResponse:
    started = time.monotonic()
    _VIOLATIONS.clear()
    isolation: dict[str, JsonValue] = {}
    try:
        plan = _attempt(request, guard, isolation)
    except _Failed as failed:
        return SandboxResponse(ok=False, error=failed.error, isolation=isolation,
                               duration_ms=int((time.monotonic() - started) * 1000))
    if _VIOLATIONS:
        return SandboxResponse(ok=False, error=SandboxError(code="violation", type="PermissionError",
                                                            message=_VIOLATIONS[0]),
                               isolation=isolation, duration_ms=int((time.monotonic() - started) * 1000))
    return SandboxResponse(ok=True, plan=plan.model_dump(mode="json", by_alias=True), isolation=isolation,
                           duration_ms=int((time.monotonic() - started) * 1000))


def _write(out: IO[bytes], response: SandboxResponse) -> None:
    out.write(response.model_dump_json(by_alias=True).encode("utf-8"))
    out.flush()


def main() -> int:
    out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    data = sys.stdin.buffer.read()
    try:
        request = read_request(data)
    except ValueError as error:
        _write(out, SandboxResponse(ok=False, error=SandboxError(
            code="protocol", type=type(error).__name__, message=f"the sandbox request does not parse: {error}")))
        return 1
    _write(out, run(request))
    return 0


__all__ = [
    "BLOCKED_EVENTS",
    "SOURCE_PREFIX",
    "apply_limits",
    "audit_hook",
    "bindings",
    "build_registry",
    "compile_function",
    "install_guard",
    "main",
    "read_request",
    "run",
    "trace_dynamic",
]


if __name__ == "__main__":
    raise SystemExit(main())
