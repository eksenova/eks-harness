from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue

from eks_harness.engine.auth import AuthRequest, PersonaAuthRegistry
from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import (
    ExecContext,
    ExecResult,
    ExecutorSpec,
    builtin_provenance,
)
from eks_harness.ir.ops.api import AsPersona
from eks_harness.ops import BUILTIN_EXECUTOR_VERSION, SESSION_EXECUTOR
from eks_harness.ops.worker import _port_call

AS_OP = "session.as"


def _uses_backend(call: ExecContext) -> bool:
    kinds = {resource.id: resource.kind for resource in call.plan.resources}
    return any(kinds.get(resource_id) == "backend" for resource_id in call.node.uses)


class SessionAsExecutor:
    def __init__(self, auth: PersonaAuthRegistry) -> None:
        self.auth = auth

    def run(self, call: ExecContext) -> ExecResult:
        args = call.args
        if not isinstance(args, AsPersona):
            raise NodeFailure("args_invalid", f"{call.op} needs a persona argument, got {type(args).__name__}")
        persona = args.persona
        if call.state is not None:
            call.state.persona = persona
        headers = self._headers(call, persona)
        call.event("persona", {"persona": persona, "api": headers is not None})
        sent: Mapping[str, JsonValue] = {"persona": persona, "headers": dict(headers or {})}
        reply = _port_call(call, AS_OP, sent, "run")
        return ExecResult(value=None, detail=dict(reply.detail))

    def spec(self) -> ExecutorSpec:
        return ExecutorSpec(SESSION_EXECUTOR, BUILTIN_EXECUTOR_VERSION, ops=(AS_OP,), run=self.run,
                            provenance=builtin_provenance(),
                            description="Switches the session persona for API calls and logs the app in as it")

    def _headers(self, call: ExecContext, persona: str) -> Mapping[str, str] | None:
        if not _uses_backend(call):
            return None
        handle = call.backend()
        request = AuthRequest(persona=persona, backend=handle, session=call.session, run=call.run,
                              registry=call.registry, tree=Path(call.run.tree) if call.run.tree else None)
        if not self.auth.can_authenticate(request):
            return None
        return self.auth.headers(request)


__all__ = ["AS_OP", "SessionAsExecutor"]
