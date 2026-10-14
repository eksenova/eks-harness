from __future__ import annotations

from eks_harness.data.build import BASE_OP, CREATE_OP, EXTEND_OP
from eks_harness.data.context import SeedError, redact
from eks_harness.data.engine import SeedEngine
from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import ExecContext, ExecResult, ExecutorSpec

DATA_EXECUTOR = "eks.data"
DATA_EXECUTOR_VERSION = "1"


class DataExecutor:
    def __init__(self, engine: SeedEngine) -> None:
        self.engine = engine

    def run(self, call: ExecContext) -> ExecResult:
        handle = call.backend()
        try:
            build = self.engine.build_for(call.run.id, handle.resource_id)
            if call.op == EXTEND_OP:
                return ExecResult(value=None, detail={"extended": redact(build.extend(call.node))})
            if call.op == BASE_OP:
                value = build.base_resource(call.node)
            elif call.op == CREATE_OP:
                value = build.create(call.node, log=call.log)
            else:
                raise SeedError(f"{call.op} is not a data op")
        except SeedError as error:
            raise NodeFailure("seed_error", error.message, detail=error.detail) from error
        return ExecResult(value=redact(value))

    def spec(self) -> ExecutorSpec:
        return ExecutorSpec(DATA_EXECUTOR, DATA_EXECUTOR_VERSION, ops=(CREATE_OP, EXTEND_OP, BASE_OP), run=self.run,
                            locations=frozenset({"hub"}),
                            description="Creates seed resources and restores base seeds in seed runs")


__all__ = ["DATA_EXECUTOR", "DataExecutor"]
