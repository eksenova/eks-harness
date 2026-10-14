from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel

from eks_harness.ir.plan import Plan, SessionSpec
from eks_harness.ir.registry import Location
from eks_harness.ir.time import TempoMap, Time

RunStatus = Literal["queued", "preparing", "running", "paused", "passed", "failed", "aborted", "error"]
NodeStatus = Literal["pending", "ready", "running", "paused", "passed", "soft_failed", "failed", "skipped", "cancelled"]
CacheMode = Literal["use", "refresh", "readonly", "off"]
CacheOutcome = Literal["hit", "miss", "stored", "bypass", "off"]
ResourceState = Literal["pending", "acquiring", "held", "released", "failed"]
ResourceKind = Literal["lease.web", "lease.ios", "lease.android", "backend", "seed", "cassette", "slot"]
ExpansionKind = Literal["each", "dynamic", "branch"]
ControlAction = Literal["continue", "step", "retry", "skip", "abort", "edit", "do", "pause", "break_add", "break_remove",
                        "break_list", "status"]
PauseReason = Literal["breakpoint", "source", "step", "error", "pause"]
DebuggerState = Literal["off", "running", "paused", "stepping", "aborting"]
BackendSource = Literal["request", "plan", "profile", "policy", "legacy_policy", "project_default"]
SeedMode = Literal["restore", "fresh", "rebuild", "skip"]
EntryFolder = Literal["flows", "seeds", "edits", "scenes", "scores"]
Platform = Literal["web", "ios", "android"]
CheckSeverity = Literal["error", "warning"]
DivergenceField = Literal["status", "output", "error", "artifacts"]

TERMINAL_STATUSES: frozenset[str] = frozenset({"passed", "failed", "aborted", "error"})
ACTIVE_STATUSES: frozenset[str] = frozenset({"queued", "preparing", "running", "paused"})
SETTLED_STATUSES: frozenset[str] = frozenset({"passed", "soft_failed", "skipped"})
FINAL_NODE_STATUSES: frozenset[str] = frozenset({"passed", "soft_failed", "skipped", "failed", "cancelled"})
ROOT_INSTANCE = ""
MAIN_SESSION = "main"
TAKE_OP = "score.take"


class EngineModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, validate_by_alias=True,
                              serialize_by_alias=True, extra="forbid")


class BackendRef(EngineModel):
    ref: str = Field(min_length=1, description="Backend lease id (bl_...), backend id (definition@identity) or a backend definition name")
    tree: str | None = Field(default=None, description="Tree the backend runs from when it is not the run's tree")
    key: str | None = Field(default=None, description="Identity of the backend instance instead of the tree")
    params: dict[str, str] = Field(default_factory=dict, description="Parameters of the backend definition")


class RunClock(EngineModel):
    tempo: TempoMap | None = Field(default=None, description="Tempo map for a scene plan whose clock tempo is external")
    cues: dict[str, Time] = Field(default_factory=dict, description="Cue times supplied at run time")
    markers: dict[str, list[Time]] = Field(default_factory=dict, description="Marker streams supplied at run time, by stream name")


class FakeAnswer(EngineModel):
    value: JsonValue = Field(default=None, description="Output the fake returns; it must match the node's output type")
    fail: str | None = Field(default=None, description="When set, the fake fails the node with this message")
    code: str = Field(default="fake_failure", description="Error code of a scripted failure")
    soft: bool = Field(default=False, description="Make a scripted failure soft")
    retryable: bool = Field(default=False, description="Mark a scripted failure retryable")
    times: int | None = Field(default=None, ge=1, description="Apply the answer to this many attempts only, then answer with the default")
    delay_ms: int = Field(default=0, ge=0, le=600_000, description="Delay before the fake answers")
    resolution: dict[str, JsonValue] | None = Field(default=None, description="Machine resolution record the fake resolver reports")


class FakeProfile(EngineModel):
    families: list[str] = Field(default_factory=lambda: ["*"], description="Op families or op names whose missing executors are faked; * fakes every missing one")
    force: list[str] = Field(default_factory=list, description="Op families or op names faked even when a real executor exists")
    drivers: bool = Field(default=True, description="Give every lease a fake driver port")
    seeds: bool = Field(default=True, description="Restore seed snapshots with the fake seed hook")
    cassettes: bool = Field(default=True, description="Open cassettes with the fake cassette hook")
    answers: dict[str, FakeAnswer] = Field(default_factory=dict, description="Scripted answers by node id or op name; a node id wins over an op name")


class RunOptions(EngineModel):
    debug: bool = Field(default=False, description="Let breakpoints, stepping and pause on error pause the run")
    breakpoints: list[str] = Field(default_factory=list, description="Breakpoint specs such as n12, op:ui.tap, label:before-submit or when:attempt > 0")
    break_on_error: bool = Field(default=False, description="Pause at a failing node instead of failing the run")
    cache: CacheMode = Field(default="use", description="Cache mode for pure nodes: use, refresh, readonly or off")
    parallelism: int | None = Field(default=None, ge=1, le=64, description="Nodes that run at once; the hub's engine.parallelism when unset")
    retries: int | None = Field(default=None, ge=0, le=10, description="Automatic retries of idempotent nodes; the hub's engine.retries when unset")
    node_timeout: float | None = Field(default=None, gt=0, le=86400, description="Seconds a node without its own timeout may run")
    pause_timeout: float | None = Field(default=None, gt=0, le=86400, description="Seconds the run may stay paused before it is aborted; the hub's engine.pauseTimeoutSeconds when unset")
    seed: SeedMode = Field(default="restore", description="restore loads a seed snapshot unless the backend already holds it, fresh always restores, rebuild builds the seed again, skip runs against the backend as it is")
    plugins: bool = Field(default=True, description="Load the tree's plugins")


class RunRequest(EngineModel):
    plan: Plan | None = Field(default=None, description="Plan JSON to run")
    spec: str | None = Field(default=None, description="Entry file to plan on the hub, path/to/file.py or path/to/file.py::name")
    source: str | None = Field(default=None, description="Python source of an entry file to plan on the hub")
    entry: str | None = Field(default=None, description="Entry name inside source when it defines several")
    folder: EntryFolder = Field(default="flows", description="Folder of the tree the inline source is planned from")
    tree: str | None = Field(default=None, description="Absolute path of the work tree on the hub")
    cwd: str | None = Field(default=None, description="Directory a relative spec is resolved against")
    params: dict[str, str] = Field(default_factory=dict, description="Entry parameters as text, parsed by their annotations")
    platform: Platform | None = Field(default=None, description="Platform of a flow")
    project: str | None = Field(default=None, description="Project (owner/name) the run belongs to; the tree's project when unset")
    session: str | None = Field(default=None, description="Session the run belongs to; the tree's git branch when unset")
    instance: str | None = Field(default=None, description="Harness instance that started the run")
    options: RunOptions = Field(default_factory=RunOptions, description="Run options")
    backend: BackendRef | None = Field(default=None, description="Backend for every backend resource of the plan")
    backends: dict[str, BackendRef] = Field(default_factory=dict, description="Backend per session key (main, take0, ...); wins over backend")
    clock: RunClock | None = Field(default=None, description="Run-time clock for a scene plan with an external tempo")
    fakes: FakeProfile | None = Field(default=None, description="Simulate ops without real executors with fake executors")
    replay_of: str | None = Field(default=None, description="Run this run replays")
    repeat_of: str | None = Field(default=None, description="First run of a --repeat series")


class RunInfo(EngineModel):
    id: str = Field(description="Run id (ULID)")
    project: str = Field(description="Project id owner/name")
    session: str = Field(description="Session name")
    session_id: int | None = Field(default=None, description="Session row id")
    tree: str | None = Field(default=None, description="Work tree on the hub")
    spec: str | None = Field(default=None, description="Entry the plan was built from")
    plan_hash: str = Field(description="Plan hash")
    plan_kind: str = Field(description="Plan kind")
    plan_name: str = Field(description="Plan name")
    options: RunOptions = Field(default_factory=RunOptions, description="Run options")
    instance: str | None = Field(default=None, description="Harness instance that started the run")
    actor: str | None = Field(default=None, description="User name that started the run")
    user_id: int | None = Field(default=None, description="User id that started the run")
    fakes: FakeProfile | None = Field(default=None, description="Fake profile of a simulated run")
    clock: RunClock | None = Field(default=None, description="Run-time clock")
    created_at: float = Field(description="Creation time, epoch seconds")


class PlanRef(EngineModel):
    kind: str = Field(description="Plan kind")
    name: str = Field(description="Plan name")
    hash: str = Field(description="Plan hash")


class RunLinks(EngineModel):
    run: str = Field(description="Run status endpoint")
    report: str = Field(description="Run report endpoint")
    journal: str = Field(description="Journal endpoint")
    events: str = Field(description="Server-sent event stream of the run")
    plan: str = Field(description="Plan JSON of the run")
    session: str | None = Field(default=None, description="Web UI page of the run's session")


class CacheCounts(EngineModel):
    hits: int = Field(default=0, description="Pure nodes served from the cache")
    misses: int = Field(default=0, description="Pure nodes looked up and executed")
    stored: int = Field(default=0, description="Results written to the cache")
    bypassed: int = Field(default=0, description="Pure nodes that could not be keyed")


class NodeError(EngineModel):
    code: str = Field(description="Error code, see engine.errors.ERROR_CODES")
    message: str = Field(description="Human readable message")
    detail: dict[str, JsonValue] = Field(default_factory=dict, description="Structured detail")
    retryable: bool = Field(default=False, description="The executor marked the failure as transient")
    soft: bool = Field(default=False, description="The failure is recorded without failing the run")


class ArtifactRef(EngineModel):
    id: str = Field(description="Artifact id")
    kind: str = Field(description="Artifact kind")
    name: str | None = Field(default=None, description="File name")
    url: str | None = Field(default=None, description="Web UI page")
    raw_url: str | None = Field(default=None, description="The file itself")
    node: str | None = Field(default=None, description="Node that produced it")
    instance: str = Field(default=ROOT_INSTANCE, description="Node instance that produced it")


class PauseInfo(EngineModel):
    reason: PauseReason = Field(description="Why the run paused")
    node: str | None = Field(default=None, description="Node the run paused at")
    instance: str = Field(default=ROOT_INSTANCE, description="Instance of that node")
    attempt: int = Field(default=0, description="Attempt of that node")
    op: str | None = Field(default=None, description="Op of the node")
    label: str | None = Field(default=None, description="Node or breakpoint label")
    src: str | None = Field(default=None, description="Source position file:line")
    args: JsonValue = Field(default=None, description="Resolved arguments of the node")
    error: NodeError | None = Field(default=None, description="Failure that paused the run")
    breakpoint: str | None = Field(default=None, description="Id of the breakpoint that hit")
    values: dict[str, JsonValue] = Field(default_factory=dict, description="Values computed so far, by node id (instance suffixed with @)")
    snapshots: list[ArtifactRef] = Field(default_factory=list, description="Screen and DOM snapshots taken at the pause")
    at: float = Field(description="Pause time, epoch seconds")


class Breakpoint(EngineModel):
    id: str = Field(description="Breakpoint id")
    text: str = Field(description="Spec the breakpoint was created from")
    node: str | None = Field(default=None, description="Node id it matches")
    op: str | None = Field(default=None, description="Op name or glob it matches")
    label: str | None = Field(default=None, description="Node or debug.break label it matches")
    condition: str | None = Field(default=None, description="Condition expression that must hold")
    enabled: bool = Field(default=True, description="Whether it is active")
    hits: int = Field(default=0, description="How often it paused the run")


class RunSummary(EngineModel):
    id: str = Field(description="Run id")
    status: RunStatus = Field(description="Run status")
    reason: str | None = Field(default=None, description="Why the run ended, failed or paused")
    plan: PlanRef = Field(description="The plan")
    project: str | None = Field(default=None, description="Project id")
    session: str | None = Field(default=None, description="Session name")
    tree: str | None = Field(default=None, description="Work tree on the hub")
    spec: str | None = Field(default=None, description="Entry the plan was built from")
    actor: str | None = Field(default=None, description="Who started the run")
    instance: str | None = Field(default=None, description="Harness instance that started the run")
    created_at: float = Field(description="Creation time")
    started_at: float | None = Field(default=None, description="Start of execution")
    finished_at: float | None = Field(default=None, description="End of the run")
    counts: dict[str, int] = Field(default_factory=dict, description="Node instances by status")
    cache: CacheCounts = Field(default_factory=CacheCounts, description="Cache use")
    pause: PauseInfo | None = Field(default=None, description="Pause details while paused")
    simulated: bool = Field(default=False, description="Fake executors answered some nodes")
    replay_of: str | None = Field(default=None, description="Run this run replays")
    repeat_of: str | None = Field(default=None, description="First run of a repeat series")
    links: RunLinks = Field(description="Endpoints of the run")


class NodeRecord(EngineModel):
    run_id: str = Field(description="Run id")
    node_id: str = Field(description="Node id")
    instance: str = Field(default=ROOT_INSTANCE, description="Instance key: each iterations and dynamic sub-plans")
    parent_id: str | None = Field(default=None, description="Enclosing container or dynamic node")
    parent_instance: str | None = Field(default=None, description="Instance of the parent")
    seq: int = Field(description="Order in which the scheduler first saw this instance")
    op: str = Field(description="Op name")
    node_hash: str = Field(description="Node hash")
    label: str | None = Field(default=None, description="Node label")
    src: str | None = Field(default=None, description="Source position")
    status: NodeStatus = Field(description="Node status")
    attempt: int = Field(default=0, description="Attempts so far")
    args: JsonValue = Field(default=None, description="Resolved arguments of the last attempt")
    output: JsonValue = Field(default=None, description="Output value, only once type checked")
    has_output: bool = Field(default=False, description="Whether output holds a value (null is a value)")
    error: NodeError | None = Field(default=None, description="Failure of the last attempt")
    cache: CacheOutcome | None = Field(default=None, description="Cache outcome of a pure node")
    cache_key: str | None = Field(default=None, description="Cache key of a pure node")
    resolution: dict[str, JsonValue] | None = Field(default=None, description="Machine resolution, content digests and backend identity")
    executor: str | None = Field(default=None, description="Executor name@version")
    location: Location | None = Field(default=None, description="Where the executor runs")
    simulated: bool = Field(default=False, description="A fake executor answered")
    edited: bool = Field(default=False, description="Arguments were edited in the debugger")
    artifacts: list[str] = Field(default_factory=list, description="Artifact ids produced")
    queued_at: float | None = Field(default=None, description="Time the instance became ready")
    started_at: float | None = Field(default=None, description="Start of the last attempt")
    finished_at: float | None = Field(default=None, description="End of the last attempt")
    duration_ms: int | None = Field(default=None, description="Duration of the last attempt")


class NodeEventIn(EngineModel):
    type: str = Field(description="Event type such as node.started")
    node_id: str | None = Field(default=None, description="Node id, none for run events")
    instance: str = Field(default=ROOT_INSTANCE, description="Node instance")
    attempt: int = Field(default=0, description="Attempt")
    detail: dict[str, JsonValue] = Field(default_factory=dict, description="Event detail")


class NodeEventRecord(EngineModel):
    id: int = Field(description="Journal event id, also the server-sent event id")
    run_id: str = Field(description="Run id")
    type: str = Field(description="Event type")
    node_id: str | None = Field(default=None, description="Node id")
    instance: str = Field(default=ROOT_INSTANCE, description="Node instance")
    attempt: int = Field(default=0, description="Attempt")
    ts: float = Field(description="Time, epoch seconds")
    detail: dict[str, JsonValue] = Field(default_factory=dict, description="Event detail")


class ExpansionRecord(EngineModel):
    run_id: str = Field(description="Run id")
    node_id: str = Field(description="ctl.each, ctl.dynamic or ctl.when node")
    instance: str = Field(default=ROOT_INSTANCE, description="Instance of that node")
    kind: ExpansionKind = Field(description="each items, dynamic sub-plan or chosen branch")
    detail: dict[str, JsonValue] = Field(description="Items, sub-plan or branch")
    created_at: float = Field(description="Time")


class ResourceRecord(EngineModel):
    run_id: str = Field(description="Run id")
    resource_id: str = Field(description="Plan resource id, or a synthetic id for slots, seeds and cassettes")
    kind: ResourceKind = Field(description="Resource kind")
    session_key: str | None = Field(default=None, description="Session the resource belongs to")
    state: ResourceState = Field(description="State")
    handle: str | None = Field(default=None, description="Lease sid, backend lease id, slot ticket or cassette key")
    detail: dict[str, JsonValue] = Field(default_factory=dict, description="Resolved identity, redacted")
    error: str | None = Field(default=None, description="Why acquisition failed")
    acquired_at: float | None = Field(default=None, description="Acquisition time")
    released_at: float | None = Field(default=None, description="Release time")


class LeaseHandle(EngineModel):
    resource_id: str = Field(description="Plan resource id")
    kind: ResourceKind = Field(description="lease.web, lease.ios or lease.android")
    platform: Platform = Field(description="Platform")
    session_key: str = Field(description="Session key")
    app: str | None = Field(default=None, description="App profile name")
    sid: str = Field(description="Driver lease sid")
    resource: str | None = Field(default=None, description="Pool resource such as browser:0:1 or ios:2")
    project: str = Field(description="Project of the lease")
    session: str = Field(description="Session of the lease")


class BackendIdentity(EngineModel):
    definition: str = Field(description="Backend definition that serves the resource")
    backend_id: str = Field(description="Backend instance id")
    lease_id: str = Field(description="Backend lease id the run holds")
    source: BackendSource = Field(description="What chose the definition")
    policy: str | None = Field(default=None, description="Backend policy that chose it")
    policy_plugin: str | None = Field(default=None, description="Plugin of that policy")
    policy_version: str | None = Field(default=None, description="Version of that plugin")
    plugin: str | None = Field(default=None, description="Plugin that provides the definition")
    plugin_version: str | None = Field(default=None, description="Version of that plugin")
    fingerprint: str = Field(default="", description="Backend fingerprint reported by the definition")
    commit: str = Field(default="", description="Commit the backend runs")
    branch: str = Field(default="", description="Branch the backend runs")
    version: str = Field(default="", description="Version the backend reports")
    generation: int = Field(default=0, description="Restart generation of the instance")


class BackendHandle(EngineModel):
    resource_id: str = Field(description="Plan resource id")
    session_key: str | None = Field(default=None, description="Session key")
    seed: str | None = Field(default=None, description="Seed scenario of the resource")
    url: str = Field(description="Base URL of the backend")
    identity: BackendIdentity = Field(description="Resolved identity")
    info: dict[str, JsonValue] = Field(default_factory=dict, description="Redacted backend info")
    secrets: dict[str, JsonValue] = Field(default_factory=dict, exclude=True, repr=False, description="Full backend info, never serialized")


class SlotHandle(EngineModel):
    ticket: str = Field(description="Render queue ticket id")
    location: Location = Field(description="Executor location the slot serves")
    label: str = Field(description="Label shown in the render queue")
    acquired_at: float = Field(description="Time the slot started")


class SeedRestoreResult(EngineModel):
    resource_id: str = Field(description="Backend resource id")
    seed: str = Field(description="Seed scenario")
    key: str = Field(description="Snapshot key")
    restored: bool = Field(description="A snapshot was restored")
    snapshot: str | None = Field(default=None, description="Snapshot reference")
    detail: dict[str, JsonValue] = Field(default_factory=dict, description="Hook detail")
    personas: dict[str, JsonValue] = Field(default_factory=dict, exclude=True, repr=False, description="Personas the seed created, with their credentials; never serialized")


class RunResources(EngineModel):
    leases: dict[str, LeaseHandle] = Field(default_factory=dict, description="Driver leases by resource id")
    backends: dict[str, BackendHandle] = Field(default_factory=dict, description="Backends by resource id")
    seeds: list[SeedRestoreResult] = Field(default_factory=list, description="Seed restores")

    def lease(self, resource_id: str) -> LeaseHandle | None:
        return self.leases.get(resource_id)

    def backend(self, resource_id: str) -> BackendHandle | None:
        return self.backends.get(resource_id)


class CheckOutcome(EngineModel):
    kind: str = Field(description="Check kind")
    ok: bool = Field(description="Whether the check passed")
    severity: CheckSeverity = Field(default="error", description="error fails the checkpoint, warning does not")
    message: str = Field(default="", description="What was found")
    detail: dict[str, JsonValue] = Field(default_factory=dict, description="Measurements")


class CheckpointOutcome(EngineModel):
    label: str = Field(description="Checkpoint label")
    viewport: str | None = Field(default=None, description="Viewport")
    ok: bool = Field(description="No error-severity check failed")
    checks: list[CheckOutcome] = Field(default_factory=list, description="Check results")
    shots: list[str] = Field(default_factory=list, description="Artifact ids of evidence shots")


class FailureOut(EngineModel):
    node: str | None = Field(default=None, description="Failing node, none for run-level failures")
    instance: str = Field(default=ROOT_INSTANCE, description="Instance")
    op: str | None = Field(default=None, description="Op")
    label: str | None = Field(default=None, description="Node label")
    src: str | None = Field(default=None, description="Source position file:line")
    code: str = Field(description="Error code")
    message: str = Field(description="Message")
    soft: bool = Field(default=False, description="Recorded without failing the run")
    checks: list[CheckOutcome] = Field(default_factory=list, description="Failed checks of a checkpoint")
    shots: list[ArtifactRef] = Field(default_factory=list, description="Failure shots and checkpoint evidence")
    detail: dict[str, JsonValue] = Field(default_factory=dict, description="Structured detail")


class CheckpointOut(EngineModel):
    node: str = Field(description="check.checkpoint node")
    instance: str = Field(default=ROOT_INSTANCE, description="Instance")
    label: str = Field(description="Checkpoint label")
    viewport: str | None = Field(default=None, description="Viewport")
    ok: bool = Field(description="Whether it passed")
    checks: list[CheckOutcome] = Field(default_factory=list, description="Check results")
    shots: list[ArtifactRef] = Field(default_factory=list, description="Evidence shots")


class NodeTiming(EngineModel):
    node: str = Field(description="Node id")
    instance: str = Field(default=ROOT_INSTANCE, description="Instance")
    op: str = Field(description="Op")
    ms: int = Field(description="Duration of the last attempt")
    cache: CacheOutcome | None = Field(default=None, description="Cache outcome")


class Timings(EngineModel):
    total_ms: int = Field(default=0, description="Creation to end")
    queued_ms: int = Field(default=0, description="Waiting for a run slot")
    preparing_ms: int = Field(default=0, description="Acquiring resources")
    running_ms: int = Field(default=0, description="Executing nodes, pauses included")
    paused_ms: int = Field(default=0, description="Time spent paused")
    nodes: list[NodeTiming] = Field(default_factory=list, description="Node durations, slowest first")


class IssueOut(EngineModel):
    severity: CheckSeverity = Field(description="error or warning")
    code: str = Field(description="Issue code")
    message: str = Field(description="Message")
    node: str | None = Field(default=None, description="Node id")
    field: str | None = Field(default=None, description="Dotted field path inside the node args")
    src: str | None = Field(default=None, description="Source position")


class PluginStamp(EngineModel):
    version: str = Field(description="Plugin version")
    digest: str | None = Field(default=None, description="Content digest of the plugin code")


class EnvironmentOut(EngineModel):
    harness: str = Field(default="", description="Harness version")
    source: str = Field(default="", description="Harness source digest")
    python: str = Field(default="", description="Python version")
    machine: str = Field(default="", description="os-arch of the hub")
    executors: dict[str, str] = Field(default_factory=dict, description="Executor name@version by op")
    plugins: dict[str, PluginStamp] = Field(default_factory=dict, description="Plugins loaded for the run")
    project_digest: str | None = Field(default=None, description="Digest of the tree's project code")
    backends: dict[str, BackendIdentity] = Field(default_factory=dict, description="Resolved backends by resource id")
    fakes: list[str] = Field(default_factory=list, description="Ops answered by fake executors")


class JournalRef(EngineModel):
    run_id: str = Field(description="Journal id, the run id")
    nodes: int = Field(default=0, description="Node instances journaled")
    events: int = Field(default=0, description="Events journaled")


class RunReport(EngineModel):
    run: RunSummary = Field(description="Run summary")
    failures: list[FailureOut] = Field(default_factory=list, description="Hard and soft failures, in journal order")
    checkpoints: list[CheckpointOut] = Field(default_factory=list, description="Checkpoints in journal order")
    artifacts: list[ArtifactRef] = Field(default_factory=list, description="Every artifact the run produced")
    values: JsonValue = Field(default=None, description="Resolved plan outputs")
    timings: Timings = Field(default_factory=Timings, description="Timings")
    cache: CacheCounts = Field(default_factory=CacheCounts, description="Cache use")
    resources: list[ResourceRecord] = Field(default_factory=list, description="Resources the run held")
    environment: EnvironmentOut = Field(default_factory=EnvironmentOut, description="What the run executed with")
    issues: list[IssueOut] = Field(default_factory=list, description="Admission warnings")
    journal: JournalRef = Field(description="Journal reference")


class RunQuery(EngineModel):
    project: str | None = Field(default=None, description="Only runs of this project")
    session: str | None = Field(default=None, description="Only runs of this session name")
    status: list[RunStatus] = Field(default_factory=list, description="Only runs in these states")
    plan_hash: str | None = Field(default=None, description="Only runs of this plan hash")
    name: str | None = Field(default=None, description="Only runs of plans with this name")
    limit: int = Field(default=50, ge=1, le=500, description="Page size")
    cursor: str | None = Field(default=None, description="Cursor of the next page")


class RunPage(EngineModel):
    items: list[RunSummary] = Field(description="Runs, newest first")
    next_cursor: str | None = Field(default=None, description="Cursor of the next page")


class JournalPage(EngineModel):
    run: RunSummary = Field(description="Run summary")
    nodes: list[NodeRecord] = Field(default_factory=list, description="Node instances in seq order")
    expansions: list[ExpansionRecord] = Field(default_factory=list, description="Journaled sub-plans and branches")
    resources: list[ResourceRecord] = Field(default_factory=list, description="Resources")
    events: list[NodeEventRecord] = Field(default_factory=list, description="Events after the requested id")
    next_after: int | None = Field(default=None, description="Pass as after to continue the events")


class Divergence(EngineModel):
    node: str = Field(description="Node id")
    instance: str = Field(default=ROOT_INSTANCE, description="Instance")
    op: str = Field(description="Op")
    field: DivergenceField = Field(description="What differs")
    a: JsonValue = Field(default=None, description="Value in the first run")
    b: JsonValue = Field(default=None, description="Value in the second run")


class RunComparison(EngineModel):
    a: str = Field(description="First run")
    b: str = Field(description="Second run")
    same: bool = Field(description="No node diverges")
    diverging: list[Divergence] = Field(default_factory=list, description="Diverging node instances")


class ControlRequest(EngineModel):
    action: ControlAction = Field(description="Debugger action")
    node: str | None = Field(default=None, description="Node the action targets; the paused node when unset")
    args: dict[str, JsonValue] | None = Field(default=None, description="New arguments for edit, op arguments for do")
    op: str | None = Field(default=None, description="Op for do")
    value: JsonValue = Field(default=None, description="Output value a skipped node takes")
    breakpoint: str | None = Field(default=None, description="Breakpoint spec for break_add")
    id: str | None = Field(default=None, description="Breakpoint id for break_remove")


class ControlResponse(EngineModel):
    ok: bool = Field(description="Whether the action was applied")
    status: RunStatus = Field(description="Run status after the action")
    debugger: DebuggerState = Field(description="Debugger state after the action")
    pause: PauseInfo | None = Field(default=None, description="Pause details")
    breakpoints: list[Breakpoint] = Field(default_factory=list, description="Breakpoints")
    result: JsonValue = Field(default=None, description="Value of a do action")
    message: str = Field(default="", description="What happened")


class DoRequest(EngineModel):
    args: dict[str, JsonValue] = Field(default_factory=dict, description="Op arguments; target fields take shorthand strings")
    platform: Platform | None = Field(default=None, description="Platform for ops that use a lease")
    app: str | None = Field(default=None, description="App profile for ops that use a lease")
    persona: str | None = Field(default=None, description="Persona to act as first")
    seed: str | None = Field(default=None, description="Seed scenario of the backend")
    backend: BackendRef | None = Field(default=None, description="Backend for ops that use one")
    project: str | None = Field(default=None, description="Project the run belongs to")
    session: str | None = Field(default=None, description="Session the run belongs to")
    tree: str | None = Field(default=None, description="Work tree on the hub")
    instance: str | None = Field(default=None, description="Harness instance")
    fakes: FakeProfile | None = Field(default=None, description="Fake profile")
    wait: float = Field(default=120.0, ge=0, le=3600, description="Seconds to wait for the result")


class DoResult(EngineModel):
    run: RunSummary = Field(description="The one-node run")
    value: JsonValue = Field(default=None, description="Output of the op")
    artifacts: list[ArtifactRef] = Field(default_factory=list, description="Artifacts it produced")
    failure: FailureOut | None = Field(default=None, description="Failure when it failed")


def sessions_of(plan: Plan) -> dict[str, SessionSpec]:
    found: dict[str, SessionSpec] = {}
    if plan.session is not None:
        found[plan.session.key] = plan.session
    for node, _ancestors in plan.walk():
        if node.op != TAKE_OP:
            continue
        raw = node.args.get("session")
        if isinstance(raw, dict):
            spec = SessionSpec.model_validate(raw)
            found.setdefault(spec.key, spec)
    return found


__all__ = [
    "ACTIVE_STATUSES",
    "FINAL_NODE_STATUSES",
    "MAIN_SESSION",
    "ROOT_INSTANCE",
    "SETTLED_STATUSES",
    "TERMINAL_STATUSES",
    "ArtifactRef",
    "BackendHandle",
    "BackendIdentity",
    "BackendRef",
    "BackendSource",
    "Breakpoint",
    "CacheCounts",
    "CacheMode",
    "CacheOutcome",
    "CheckOutcome",
    "CheckSeverity",
    "CheckpointOut",
    "CheckpointOutcome",
    "ControlAction",
    "ControlRequest",
    "ControlResponse",
    "DebuggerState",
    "Divergence",
    "DivergenceField",
    "DoRequest",
    "DoResult",
    "EngineModel",
    "EntryFolder",
    "EnvironmentOut",
    "ExpansionKind",
    "ExpansionRecord",
    "FailureOut",
    "FakeAnswer",
    "FakeProfile",
    "IssueOut",
    "JournalPage",
    "JournalRef",
    "LeaseHandle",
    "NodeError",
    "NodeEventIn",
    "NodeEventRecord",
    "NodeRecord",
    "NodeStatus",
    "NodeTiming",
    "PauseInfo",
    "PauseReason",
    "PlanRef",
    "Platform",
    "PluginStamp",
    "ResourceKind",
    "ResourceRecord",
    "ResourceState",
    "RunClock",
    "RunComparison",
    "RunInfo",
    "RunLinks",
    "RunOptions",
    "RunPage",
    "RunQuery",
    "RunReport",
    "RunRequest",
    "RunResources",
    "RunStatus",
    "RunSummary",
    "SeedMode",
    "SeedRestoreResult",
    "SlotHandle",
    "Timings",
    "sessions_of",
]
