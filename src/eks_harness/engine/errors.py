from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import JsonValue

from eks_harness.engine.model import IssueOut, NodeError

ERROR_CODES: Mapping[str, str] = {
    "args_invalid": "the resolved arguments do not validate against the op's argument model",
    "ref_unresolved": "a $ref, $item or $fmt could not be resolved at run time (missing value, bad path)",
    "output_type": "the executor produced a value that does not match the node's out type",
    "executor_missing": "no executor for the op on this hub",
    "executor_error": "the executor raised an unexpected exception",
    "timeout": "the node did not finish before its deadline",
    "cancelled": "the run was aborted while the node ran",
    "each_not_list": "ctl.each over a value that is not a list",
    "http_status": "an api.* response status is not one of the expected ones",
    "http_error": "an api.* request failed at the transport level",
    "persona_auth": "no authentication for the persona on this backend",
    "assertion": "an expectation did not hold",
    "check_failed": "a checkpoint check with error severity failed",
    "content_mismatch": "a pinned artifact:ID@sha256 input no longer has that content",
    "content_unavailable": "a media input could not be read to hash its content",
    "resolution_unavailable": "a pinned backend, model, device or engine is not available on this machine",
    "resolution_incomplete": "a machine-dependent node's resolver did not report every required field",
    "resolution_mismatch": "the executor used another machine resolution than the one it predicted",
    "subplan_invalid": "the sub-plan a dynamic function returned does not validate",
    "sandbox_error": "the dynamic function raised",
    "sandbox_timeout": "the dynamic function exceeded its time or memory limit",
    "sandbox_violation": "the dynamic function tried to reach the network, files or processes",
    "driver_missing": "no driver port for the lease's platform",
    "driver_error": "the driver failed the call",
    "resource_error": "a lease, backend, seed restore, cassette or slot could not be provided",
    "seed_error": "a seed resource could not be created, or a seed could not be restored or snapshotted",
    "fake_failure": "a fake executor was scripted to fail",
    "skipped": "the node was skipped in the debugger",
}


class EngineError(RuntimeError):
    code = "engine_error"

    def __init__(self, message: str, *, code: str | None = None,
                 detail: Mapping[str, JsonValue] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.detail: dict[str, JsonValue] = dict(detail or {})


class RunNotFound(EngineError):
    code = "run_not_found"


class AdmissionError(EngineError):
    code = "run_refused"

    def __init__(self, message: str, issues: Sequence[IssueOut], *, code: str | None = None) -> None:
        super().__init__(message, code=code)
        self.issues: tuple[IssueOut, ...] = tuple(issues)


class BuildError(EngineError):
    code = "plan_build"

    def __init__(self, message: str, *, where: str | None = None, code: str | None = None) -> None:
        super().__init__(message, code=code, detail={"where": where} if where else None)
        self.where = where


class ResourceError(EngineError):
    code = "resource_error"


class ControlError(EngineError):
    code = "control_invalid"


class NodeFailure(Exception):
    def __init__(self, code: str, message: str, *, detail: Mapping[str, JsonValue] | None = None,
                 retryable: bool = False, soft: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail: dict[str, JsonValue] = dict(detail or {})
        self.retryable = retryable
        self.soft = soft

    def error(self) -> NodeError:
        return NodeError(code=self.code, message=self.message, detail=self.detail, retryable=self.retryable,
                         soft=self.soft)


class OutputTypeError(NodeFailure):
    def __init__(self, message: str, *, detail: Mapping[str, JsonValue] | None = None) -> None:
        super().__init__("output_type", message, detail=detail)


class RefResolutionError(NodeFailure):
    def __init__(self, message: str, *, detail: Mapping[str, JsonValue] | None = None) -> None:
        super().__init__("ref_unresolved", message, detail=detail)


class ContentError(NodeFailure):
    pass


class ResolutionError(NodeFailure):
    pass


class SandboxFailure(NodeFailure):
    pass


class PersonaAuthError(NodeFailure):
    def __init__(self, message: str, *, detail: Mapping[str, JsonValue] | None = None) -> None:
        super().__init__("persona_auth", message, detail=detail)


class DriverError(NodeFailure):
    pass


__all__ = [
    "ERROR_CODES",
    "AdmissionError",
    "BuildError",
    "ContentError",
    "ControlError",
    "DriverError",
    "EngineError",
    "NodeFailure",
    "OutputTypeError",
    "PersonaAuthError",
    "RefResolutionError",
    "ResolutionError",
    "ResourceError",
    "RunNotFound",
    "SandboxFailure",
]
