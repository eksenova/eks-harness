from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal

from pydantic import BaseModel, Field, JsonValue, ValidationError

from eks_harness.engine.errors import SandboxFailure
from eks_harness.engine.model import EngineModel
from eks_harness.ir.plan import Node, Plan, SessionSpec
from eks_harness.paths import DIR_ENVS, resolve_paths

PROTOCOL = 1
CHILD_MODULE = "eks_harness.engine.sandbox_child"
ENV_PREFIXES = ("EKS_HARNESS_",)
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
STDERR_LIMIT_BYTES = 65_536
OUTPUT_LIMIT_BYTES = 16 * 1024 * 1024
MAX_SUBPLAN_NODES = 10_000
MAX_DEPTH = 4
FailureCode = Literal["trace_error", "user_error", "violation", "registry_changed", "limit", "protocol", "crash"]


class SandboxLimits(EngineModel):
    timeout_seconds: float = Field(default=30.0, gt=0, le=3600, description="Wall clock limit")
    cpu_seconds: int = Field(default=31, ge=1, le=3600, description="CPU time limit")
    memory_mb: int = Field(default=1024, ge=64, le=65536, description="Address space or data segment limit")
    open_files: int = Field(default=256, ge=16, le=4096, description="Open file descriptor limit")
    max_nodes: int = Field(default=MAX_SUBPLAN_NODES, ge=1, description="Largest sub-plan")


class SandboxRequest(EngineModel):
    protocol: int = Field(default=PROTOCOL, description="Protocol version")
    mode: Literal["dynamic"] = Field(default="dynamic", description="What to run")
    tree: str | None = Field(default=None, description="Work tree whose registry the child builds")
    plugins: bool = Field(default=True, description="Load the tree's plugins")
    digests: dict[str, str] = Field(default_factory=dict, description="Plugin digests the hub admitted the run with")
    plan: dict[str, JsonValue] = Field(description="Parent plan JSON")
    node: dict[str, JsonValue] = Field(description="The ctl.dynamic node JSON")
    session: dict[str, JsonValue] | None = Field(default=None, description="SessionSpec JSON governing the node: the enclosing take's, else the plan's")
    instance: str = Field(default="", description="Instance of the dynamic node")
    inputs: dict[str, JsonValue] = Field(default_factory=dict, description="Resolved inputs by parameter name")
    limits: SandboxLimits = Field(default_factory=SandboxLimits, description="Limits")


class SandboxError(EngineModel):
    code: FailureCode = Field(description="Failure class")
    type: str = Field(default="", description="Exception type")
    message: str = Field(description="Message")
    line: int | None = Field(default=None, description="Line in the function source")
    traceback: list[str] = Field(default_factory=list, description="Frames inside the function source")


class SandboxResponse(EngineModel):
    protocol: int = Field(default=PROTOCOL, description="Protocol version")
    ok: bool = Field(description="Whether the function returned a sub-plan")
    plan: dict[str, JsonValue] | None = Field(default=None, description="Sub-plan JSON, hashed")
    error: SandboxError | None = Field(default=None, description="Failure")
    isolation: dict[str, JsonValue] = Field(default_factory=dict, description="Protections that were applied")
    duration_ms: int = Field(default=0, description="Time inside the child")


@dataclass(frozen=True, slots=True)
class SubPlan:
    plan: Plan
    response: SandboxResponse


_FAILURES: Mapping[str, str] = {
    "trace_error": "sandbox_error",
    "user_error": "sandbox_error",
    "violation": "sandbox_violation",
    "limit": "sandbox_timeout",
    "registry_changed": "sandbox_error",
    "protocol": "sandbox_error",
    "crash": "sandbox_error",
}
_CHUNK = 65_536
_JOIN_SECONDS = 5.0
_HOME_ENV = "HOME"
_HASH_SEED_ENV = "PYTHONHASHSEED"
_FLAGS = ("-P", "-s", "-B", "-m")


def child_env(environ: Mapping[str, str], home: Path) -> dict[str, str]:
    env = {name: value for name, value in environ.items()
           if (name in ENV_KEEP and name != _HOME_ENV) or name.startswith(ENV_PREFIXES)}
    env[_HOME_ENV] = str(home)
    env[_HASH_SEED_ENV] = "0"
    return env


def _hub_environ() -> dict[str, str]:
    paths = resolve_paths()
    resolved = {"config": paths.config_dir, "data": paths.data_dir, "cache": paths.cache_dir,
                "state": paths.state_dir, "log": paths.log_dir}
    return {**os.environ, **{name: str(resolved[key]) for key, name in DIR_ENVS.items()}}


def subplan_name(plan: Plan, node: Node) -> str:
    return f"{plan.name}~{node.id}"


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True)


def _failure(code: Literal["limit", "protocol", "crash"], message: str, *, kind: str = "",
             duration_ms: int = 0) -> SandboxResponse:
    return SandboxResponse(ok=False, error=SandboxError(code=code, type=kind, message=message),
                           duration_ms=duration_ms)


def _collect_head(stream: IO[bytes], limit: int, sink: bytearray, overflow: threading.Event,
                  stop: Callable[[], None]) -> None:
    fd = stream.fileno()
    while True:
        try:
            chunk = os.read(fd, _CHUNK)
        except OSError:
            return
        if not chunk:
            return
        if overflow.is_set():
            continue
        if len(sink) + len(chunk) > limit:
            overflow.set()
            stop()
            continue
        sink.extend(chunk)


def _collect_tail(stream: IO[bytes], limit: int, sink: bytearray) -> None:
    fd = stream.fileno()
    while True:
        try:
            chunk = os.read(fd, _CHUNK)
        except OSError:
            return
        if not chunk:
            return
        sink.extend(chunk)
        if len(sink) > limit:
            del sink[:len(sink) - limit]


def _feed(stream: IO[bytes], payload: bytes) -> None:
    with suppress(OSError):
        stream.write(payload)
    with suppress(OSError):
        stream.close()


def _kill(process: subprocess.Popen[bytes]) -> None:
    with suppress(OSError):
        if sys.platform == "win32":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)


def _cpu_signal() -> int | None:
    if sys.platform == "win32":
        return None
    return int(signal.SIGXCPU)


def _with_stderr(message: str, stderr: bytes) -> str:
    tail = stderr.decode("utf-8", errors="replace").strip()
    return f"{message}; stderr:\n{tail}" if tail else f"{message}; stderr was empty"


@dataclass(slots=True)
class _Outcome:
    stdout: bytes
    stderr: bytes
    returncode: int | None
    timed_out: bool
    overflow: bool
    elapsed_ms: int


class SandboxRunner:
    def __init__(self, *, python: str | None = None, limits: SandboxLimits | None = None,
                 scratch: Path | None = None) -> None:
        self.python = python
        self.limits = limits or SandboxLimits()
        self.scratch = scratch

    def run(self, request: SandboxRequest) -> SandboxResponse:
        command = [self.python or sys.executable, *_FLAGS, CHILD_MODULE]
        payload = request.model_dump_json(by_alias=True).encode("utf-8")
        timeout = request.limits.timeout_seconds
        started = time.monotonic()
        with self._workdir() as home:
            try:
                outcome = self._execute(command, home, payload, timeout, started)
            except OSError as error:
                return _failure("crash", f"the sandbox interpreter {command[0]!r} could not start: {error}",
                                kind=type(error).__name__,
                                duration_ms=int((time.monotonic() - started) * 1000))
        return self._response(outcome, timeout)

    def dynamic(self, plan: Plan, node: Node, *, instance: str, inputs: Mapping[str, JsonValue], tree: Path | None,
                plugins: bool, digests: Mapping[str, str], session: SessionSpec | None = None) -> SubPlan:
        request = SandboxRequest(
            tree=str(tree) if tree is not None else None, plugins=plugins, digests=dict(digests), plan=_dump(plan),
            node=_dump(node), session=_dump(session) if session is not None else None, instance=instance,
            inputs=dict(inputs), limits=self.limits)
        response = self.run(request)
        if not response.ok or response.error is not None:
            error = response.error or SandboxError(
                code="protocol", type="", message="the sandbox reported a failure without an error")
            raise SandboxFailure(_FAILURES[error.code], error.message, detail=self._detail(error, response))
        if response.plan is None:
            error = SandboxError(code="protocol", message="the sandbox reported success without a sub-plan")
            raise SandboxFailure(_FAILURES[error.code], error.message, detail=self._detail(error, response))
        try:
            subplan = Plan.model_validate(response.plan)
        except ValidationError as invalid:
            error = SandboxError(code="protocol", type=type(invalid).__name__,
                                 message=f"the sandbox returned a sub-plan that does not validate: {invalid}")
            raise SandboxFailure(_FAILURES[error.code], error.message, detail=self._detail(error, response)) from invalid
        expected = subplan_name(plan, node)
        if subplan.name != expected:
            error = SandboxError(code="protocol",
                                 message=f"the sandbox returned the sub-plan {subplan.name!r}, expected {expected!r}")
            raise SandboxFailure(_FAILURES[error.code], error.message, detail=self._detail(error, response))
        return SubPlan(plan=subplan, response=response)

    @staticmethod
    def _detail(error: SandboxError, response: SandboxResponse) -> dict[str, JsonValue]:
        return {"code": error.code, "type": error.type, "message": error.message, "line": error.line,
                "traceback": list(error.traceback), "isolation": dict(response.isolation)}

    @contextmanager
    def _workdir(self) -> Iterator[Path]:
        if self.scratch is not None:
            self.scratch.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="eks-sandbox-", dir=self.scratch))
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _execute(command: list[str], home: Path, payload: bytes, timeout: float, started: float) -> _Outcome:
        stdout = bytearray()
        stderr = bytearray()
        overflow = threading.Event()
        with subprocess.Popen(command, cwd=home, env=child_env(_hub_environ(), home), stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as process:
            pipes = (process.stdin, process.stdout, process.stderr)
            if pipes[0] is None or pipes[1] is None or pipes[2] is None:
                _kill(process)
                raise OSError("the sandbox pipes could not be opened")
            threads = [
                threading.Thread(target=_feed, args=(pipes[0], payload), daemon=True),
                threading.Thread(target=_collect_head,
                                 args=(pipes[1], OUTPUT_LIMIT_BYTES, stdout, overflow, lambda: _kill(process)),
                                 daemon=True),
                threading.Thread(target=_collect_tail, args=(pipes[2], STDERR_LIMIT_BYTES, stderr), daemon=True),
            ]
            for thread in threads:
                thread.start()
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill(process)
                process.wait()
            _kill(process)
            for thread in threads:
                thread.join(_JOIN_SECONDS)
            returncode = process.returncode
        return _Outcome(stdout=bytes(stdout), stderr=bytes(stderr), returncode=returncode,
                        timed_out=timed_out and not overflow.is_set(), overflow=overflow.is_set(),
                        elapsed_ms=int((time.monotonic() - started) * 1000))

    @staticmethod
    def _response(outcome: _Outcome, timeout: float) -> SandboxResponse:
        if outcome.timed_out:
            return _failure("limit", f"the dynamic function did not finish within {timeout:g}s and was stopped",
                            kind="TimeoutError", duration_ms=outcome.elapsed_ms)
        if outcome.overflow:
            return _failure("crash", _with_stderr(
                f"the sandbox wrote more than {OUTPUT_LIMIT_BYTES} bytes to stdout and was stopped", outcome.stderr),
                duration_ms=outcome.elapsed_ms)
        try:
            response = SandboxResponse.model_validate_json(outcome.stdout)
        except ValidationError:
            cpu = _cpu_signal()
            if cpu is not None and outcome.returncode == -cpu:
                return _failure("limit", "the dynamic function used up its CPU time limit", kind="SIGXCPU",
                                duration_ms=outcome.elapsed_ms)
            summary = ("the sandbox wrote no response" if not outcome.stdout.strip()
                       else "the sandbox wrote a response that does not parse")
            return _failure("crash", _with_stderr(f"{summary} and exited with code {outcome.returncode}",
                                                  outcome.stderr), duration_ms=outcome.elapsed_ms)
        if response.protocol != PROTOCOL:
            return _failure("protocol", f"the sandbox answered with protocol {response.protocol}, expected {PROTOCOL}",
                            duration_ms=outcome.elapsed_ms)
        return response


__all__ = [
    "CHILD_MODULE",
    "ENV_KEEP",
    "ENV_PREFIXES",
    "MAX_DEPTH",
    "MAX_SUBPLAN_NODES",
    "OUTPUT_LIMIT_BYTES",
    "PROTOCOL",
    "STDERR_LIMIT_BYTES",
    "FailureCode",
    "SandboxError",
    "SandboxLimits",
    "SandboxRequest",
    "SandboxResponse",
    "SandboxRunner",
    "SubPlan",
    "child_env",
    "subplan_name",
]
