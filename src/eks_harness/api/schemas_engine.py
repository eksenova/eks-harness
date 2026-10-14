from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from eks_harness.engine.model import (
    BackendRef,
    EngineModel,
    EntryFolder,
    FakeProfile,
    IssueOut,
    Platform,
    RunClock,
    RunOptions,
    RunSummary,
)


class PlanBuildIn(EngineModel):
    spec: str | None = Field(default=None, description="Entry file path/to/file.py or path/to/file.py::name")
    source: str | None = Field(default=None, description="Python source of an entry file")
    entry: str | None = Field(default=None, description="Entry name inside source when it defines several")
    folder: EntryFolder = Field(default="flows", description="Folder of the tree the inline source is planned from")
    tree: str | None = Field(default=None, description="Absolute work tree path on the hub")
    cwd: str | None = Field(default=None, description="Directory a relative spec is resolved against")
    params: dict[str, str] = Field(default_factory=dict, description="Entry parameters as text")
    platform: Platform | None = Field(default=None, description="Platform of a flow")
    plugins: bool = Field(default=True, description="Load the tree's plugins")


class PlanValidateIn(EngineModel):
    plan: dict[str, JsonValue] = Field(description="Plan JSON")
    tree: str | None = Field(default=None, description="Work tree whose registry validates the plan")
    plugins: bool = Field(default=True, description="Load the tree's plugins")


class PlanValidation(EngineModel):
    ok: bool = Field(description="No error issue")
    hash: str = Field(description="Plan hash")
    issues: list[IssueOut] = Field(default_factory=list, description="Validation issues")


class PlanOut(EngineModel):
    plan: dict[str, JsonValue] = Field(description="Plan JSON")
    validation: PlanValidation = Field(description="Validation report")
    tree: str | None = Field(default=None, description="Tree the plan was built in")


class RunCreate(EngineModel):
    plan: dict[str, JsonValue] | None = Field(default=None, description="Plan JSON to run")
    spec: str | None = Field(default=None, description="Entry file to plan on the hub")
    source: str | None = Field(default=None, description="Python source of an entry file to plan on the hub")
    entry: str | None = Field(default=None, description="Entry name inside source")
    folder: EntryFolder = Field(default="flows", description="Folder the inline source is planned from")
    tree: str | None = Field(default=None, description="Absolute work tree path on the hub")
    cwd: str | None = Field(default=None, description="Directory a relative spec is resolved against")
    params: dict[str, str] = Field(default_factory=dict, description="Entry parameters as text")
    platform: Platform | None = Field(default=None, description="Platform of a flow")
    project: str | None = Field(default=None, description="Project owner/name")
    session: str | None = Field(default=None, description="Session name")
    instance: str | None = Field(default=None, description="Harness instance starting the run")
    options: RunOptions = Field(default_factory=RunOptions, description="Run options")
    backend: BackendRef | None = Field(default=None, description="Backend for every backend resource")
    backends: dict[str, BackendRef] = Field(default_factory=dict, description="Backend per session key")
    clock: RunClock | None = Field(default=None, description="Run-time clock")
    fakes: FakeProfile | None = Field(default=None, description="Fake profile")
    repeat_of: str | None = Field(default=None, description="First run of a repeat series this run belongs to")
    wait: float = Field(default=0.0, ge=0, le=3600, description="Seconds to wait for the run to end or pause")


class RunRefused(EngineModel):
    error: Literal["run_refused"] = Field(default="run_refused", description="Error code")
    message: str = Field(description="Why the run was refused")
    issues: list[IssueOut] = Field(default_factory=list, description="Validation and admission issues")


class RunCancel(EngineModel):
    reason: str | None = Field(default=None, description="Why the run is cancelled")


class OpExecutorOut(EngineModel):
    name: str | None = Field(default=None, description="Executor name")
    version: str | None = Field(default=None, description="Executor version")
    location: str = Field(description="Where it runs")
    available: bool = Field(description="A real executor exists on this hub")
    native: bool = Field(default=False, description="The scheduler runs it itself")


class OpOut(EngineModel):
    summary: dict[str, JsonValue] = Field(description="Op summary, the keys of eks-harness ops --json")
    executor: OpExecutorOut = Field(description="Executor on this hub")
    op_schema: dict[str, JsonValue] | None = Field(default=None, alias="schema", description="Argument JSON Schema")


class OpList(EngineModel):
    items: list[OpOut] = Field(description="Ops")


class FlowEntryOut(EngineModel):
    kind: str = Field(description="flow, seed, edit, scene or score")
    name: str = Field(description="Entry name")
    file: str = Field(description="File relative to the tree")
    qualname: str = Field(description="Function")
    line: int | None = Field(default=None, description="First line")
    params: list[dict[str, JsonValue]] = Field(default_factory=list, description="Parameters with annotation and default")
    tags: list[str] = Field(default_factory=list, description="Tags")
    description: str = Field(default="", description="Description")
    app: str | None = Field(default=None, description="App of a flow")
    platforms: list[str] = Field(default_factory=list, description="Platforms of a flow")


class FlowFileOut(EngineModel):
    path: str = Field(description="File relative to the tree")
    kind: str | None = Field(default=None, description="Entry kind of its folder")
    entries: list[FlowEntryOut] = Field(default_factory=list, description="Entries it defines")
    error: str | None = Field(default=None, description="Why it could not be loaded")


class FlowList(EngineModel):
    tree: str = Field(description="Tree")
    files: list[FlowFileOut] = Field(description="Entry files")


class FlowSource(EngineModel):
    tree: str = Field(description="Tree")
    path: str = Field(description="File relative to the tree")
    source: str = Field(description="File content")
    sha256: str = Field(description="sha256 of the content")


class FlowSave(EngineModel):
    tree: str = Field(description="Absolute work tree path on the hub")
    path: str = Field(description="File relative to the tree, under a .harness entry folder")
    source: str = Field(description="New content")
    expect_sha256: str | None = Field(default=None, description="Refuse the write unless the file still has this sha256")
    validate_entries: bool = Field(default=True, alias="validate", description="Plan and validate every entry of the file")


class FlowSaved(EngineModel):
    path: str = Field(description="File relative to the tree")
    sha256: str = Field(description="sha256 of the new content")
    entries: list[FlowEntryOut] = Field(default_factory=list, description="Entries it defines")
    validations: dict[str, PlanValidation] = Field(default_factory=dict, description="Validation per entry kind:name")
    error: str | None = Field(default=None, description="Why loading or planning failed")


class RunCreated(EngineModel):
    run: RunSummary = Field(description="The run")
    issues: list[IssueOut] = Field(default_factory=list, description="Admission warnings")


__all__ = [
    "FlowEntryOut",
    "FlowFileOut",
    "FlowList",
    "FlowSave",
    "FlowSaved",
    "FlowSource",
    "OpExecutorOut",
    "OpList",
    "OpOut",
    "PlanBuildIn",
    "PlanOut",
    "PlanValidateIn",
    "PlanValidation",
    "RunCancel",
    "RunCreate",
    "RunCreated",
    "RunRefused",
]
