from __future__ import annotations

import difflib
import platform as host_platform
import posixpath
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from eks.validation import validate as validate_plan
from eks_harness._version import __version__
from eks_harness.engine.build import (
    BuildRequest,
    BuiltPlan,
    PlanBuilder,
    _in_walk_order,
    _tree_entry,
    issues_of,
)
from eks_harness.engine.cache import harness_digest
from eks_harness.engine.drivers import DriverRegistry
from eks_harness.engine.errors import AdmissionError, ResourceError
from eks_harness.engine.executors import (
    NO_MATCH,
    PLUGIN_EXECUTOR_PREFIX,
    ExecutorChoice,
    ExecutorRegistry,
    pattern_matches,
)
from eks_harness.engine.model import (
    MAIN_SESSION,
    EnvironmentOut,
    FakeProfile,
    IssueOut,
    PluginStamp,
    RunClock,
    RunInfo,
    RunRequest,
    sessions_of,
)
from eks_harness.engine.registries import TreeRegistry
from eks_harness.engine.resources import (
    CassetteHooks,
    CassetteNeed,
    ResourcePlan,
    ResourcePlanner,
    SeedHooks,
    lease_kinds,
)
from eks_harness.ir.hashing import node_needs
from eks_harness.ir.ops.media import Markers
from eks_harness.ir.plan import Plan, SessionSpec
from eks_harness.ir.registry import Registry
from eks_harness.ops.fakes import FakeExecutors
from eks_harness.plugins.project import ProjectConfigError, load_project_config
from eks_harness.pools.backend_info import git_facts

DEFAULT_SESSION = "main"
ENGINE_ISSUE_CODES: Mapping[str, str] = {
    "plan.source": "the request names no plan, spec or source, or more than one",
    "plan.json": "the plan JSON does not parse as an eks/plan@1 plan",
    "executor.missing": "no executor for the op on this hub and no fake covers it",
    "executor.location": "a plugin op's executor runs at a location this milestone cannot reach",
    "driver.missing": "no driver for the platform of a lease the plan uses",
    "lease.kind": "the hub has no devices of the lease's kind",
    "tempo.external": "a scene plan with an external tempo needs a run-time clock",
    "seed.hook": "no seed engine can restore the seed into the backend",
    "cassette.missing": "a replay cassette does not exist yet",
    "fakes.pattern": "a fake profile pattern names no op family or op",
    "target.project": "the run has no project",
    "plan.tree": "the plan needs a tree on the hub",
}


@dataclass(frozen=True)
class Target:
    project: str
    session: str


@dataclass(frozen=True)
class AdmittedRun:
    info: RunInfo
    plan: Plan
    registry: TreeRegistry
    executors: Mapping[str, ExecutorChoice]
    resources: ResourcePlan
    sessions: Mapping[str, SessionSpec]
    request: RunRequest
    environment: EnvironmentOut
    issues: tuple[IssueOut, ...] = ()
    fakes: FakeExecutors | None = None
    simulated: bool = False


_MISSING_EXECUTOR = ("no executor for {op} on this hub; driver executors arrive in M2, seed executors in M3, media "
                     "executors in M4 and M5; run with fakes to simulate it")
_DEVICE_KINDS = frozenset({"ios", "android"})
_BACKEND_KIND = "backend"
_MEDIA_SOURCE_OP = "media.source"
_MARKERS_OP = "media.markers"
_FILE_SCHEME = "file:"
_REPLAY = "replay"
_SCENE_KIND = "scene"


def _error(code: str, message: str, *, node: str | None = None) -> IssueOut:
    return IssueOut(severity="error", code=code, message=message, node=node)


def default_target(tree: Path | None) -> tuple[str | None, str | None]:
    if tree is None:
        return None, None
    try:
        project_id = load_project_config(tree).project_id
    except (ProjectConfigError, OSError, ValueError):
        project_id = None
    project = project_id if isinstance(project_id, str) and project_id else None
    branch = git_facts(str(tree)).get("branch") or None
    return project, branch


def _marker_streams(plan: Plan, registry: Registry) -> set[str]:
    streams: set[str] = set()
    if not registry.has_op(_MARKERS_OP):
        return streams
    model = registry.op(_MARKERS_OP).args
    for node, _ancestors in plan.walk():
        if node.op != _MARKERS_OP:
            continue
        try:
            parsed = model.model_validate(node.args)
        except ValidationError:
            continue
        if isinstance(parsed, Markers):
            for source in parsed.sources:
                streams.update(source.stream_names())
    return streams


def _external(plan: Plan) -> bool:
    tempo = plan.clock.tempo
    return plan.kind == _SCENE_KIND and tempo is not None and tempo.external


def clock_issues(plan: Plan, registry: Registry, clock: RunClock | None) -> list[IssueOut]:
    if not _external(plan):
        return [] if clock is None else [_error("tempo.external", "the plan has its own clock")]
    has_bpm = clock is not None and clock.tempo is not None and clock.tempo.bpm is not None
    cues = set(plan.clock.cues) | (set(clock.cues) if clock is not None else set())
    streams = _marker_streams(plan, registry) | (set(clock.markers) if clock is not None else set())
    issues: list[IssueOut] = []
    for node, _ancestors in plan.walk():
        if not registry.has_op(node.op):
            continue
        needs = node_needs(node, registry)
        if needs is None:
            continue
        what: list[str] = []
        if needs.tempo and not has_bpm:
            what.append("downbeat times" if needs.downbeat else "beat and bar times")
        what.extend(f"cue {cue!r}" for cue in sorted(needs.cues - cues))
        what.extend(f"marker stream {stream!r}" for stream in sorted(needs.markers - streams))
        if what:
            issues.append(_error(
                "tempo.external",
                f"scene {plan.name} uses {', '.join(what)} but its tempo is external; pass a run clock with bpm, "
                "cues and markers, or embed the scene in a score", node=node.id))
    return issues


def fake_issues(profile: FakeProfile, registry: Registry) -> list[IssueOut]:
    ops = registry.ops()
    names = sorted({op.name for op in ops} | {op.family for op in ops})
    issues: list[IssueOut] = []
    for field, patterns in (("families", profile.families), ("force", profile.force)):
        for pattern in patterns:
            if any(pattern_matches(pattern, op) != NO_MATCH for op in ops):
                continue
            close = difflib.get_close_matches(pattern.removesuffix(".*"), names, n=3, cutoff=0.6)
            hint = f"; did you mean {' or '.join(repr(name) for name in close)}?" if close else ""
            issues.append(_error("fakes.pattern",
                                 f"fake profile {field} pattern {pattern!r} names no op family or op of this tree's "
                                 f"registry{hint}"))
    return issues


def _relative_media(plan: Plan) -> str | None:
    for node, _ancestors in plan.walk():
        if node.op != _MEDIA_SOURCE_OP:
            continue
        uri = node.args.get("uri")
        if not isinstance(uri, str) or not uri.startswith(_FILE_SCHEME):
            continue
        path = uri.removeprefix(_FILE_SCHEME)
        if not posixpath.isabs(path) and not (len(path) > 2 and path[1] == ":" and path[2] in "/\\"):
            return path
    return None


def _tree_needs(plan: Plan) -> list[str]:
    reasons: list[str] = []
    if plan.requires:
        reasons.append(f"it requires plugins {', '.join(sorted(item.plugin for item in plan.requires))}")
    if any(resource.kind == _BACKEND_KIND for resource in plan.resources):
        reasons.append("it uses a backend")
    media = _relative_media(plan)
    if media is not None:
        reasons.append(f"it reads the tree file {media}")
    return reasons


class Admission:
    def __init__(self, *, builder: PlanBuilder, executors: ExecutorRegistry, drivers: DriverRegistry,
                 planner: ResourcePlanner, seeds: SeedHooks, cassettes: CassetteHooks,
                 devices: Callable[[str], tuple[bool, str]] | None = None,
                 builtin_drivers: Callable[[str], str | None] | None = None) -> None:
        self.builder = builder
        self.builtin_drivers = builtin_drivers
        self.executors = executors
        self.drivers = drivers
        self.planner = planner
        self.seeds = seeds
        self.cassettes = cassettes
        self.devices = devices

    def target(self, request: RunRequest, tree: Path | None) -> Target:
        project, branch = (None, None) if request.project and request.session else default_target(tree)
        chosen = request.project or project
        if not chosen:
            issue = _error("target.project",
                           "the run has no project: the request names none and "
                           + ("the tree's .harness/project.toml has no [project] id" if tree is not None else
                              "it has no tree")
                           + "; pass project=")
            raise AdmissionError(issue.message, [issue])
        return Target(project=chosen, session=request.session or branch or DEFAULT_SESSION)

    def admit(self, request: RunRequest, *, run_id: str, created_at: float, actor: str | None = None,
              user_id: int | None = None) -> AdmittedRun:
        given = [name for name, value in (("plan", request.plan), ("spec", request.spec), ("source", request.source))
                 if value is not None]
        if len(given) != 1:
            named = f"names {' and '.join(given)}" if given else "names none of them"
            issue = _error("plan.source", f"a run takes exactly one of plan, spec and source; the request {named}")
            raise AdmissionError(issue.message, [issue])
        issues: list[IssueOut] = []
        built: BuiltPlan | None = None
        if request.plan is not None:
            plan = request.plan
            tree = self._request_tree(request, issues)
        else:
            built = self.builder.build(BuildRequest(
                spec=request.spec, source=request.source, entry=request.entry, folder=request.folder,
                tree=Path(request.tree) if request.tree else None, cwd=Path(request.cwd) if request.cwd else None,
                params=dict(request.params), platform=request.platform, plugins=request.options.plugins))
            plan, tree = built.plan, built.tree
        if tree is None:
            reasons = _tree_needs(plan)
            if reasons:
                issues.append(_error("plan.tree", f"the plan needs a tree on the hub ({'; '.join(reasons)}); pass "
                                                  "tree= with the work tree it was built from"))
        entry = (built.registry if built is not None
                 else _tree_entry(self.builder.registries, tree, request.options.plugins, tree))
        registry = entry.registry
        issues.extend(issues_of(validate_plan(plan, registry=registry, tree=tree)))
        issues.extend(self.builder.registries.requirement_issues(plan, entry))
        issues.extend(clock_issues(plan, registry, request.clock))
        fake_source: FakeExecutors | None = None
        if request.fakes is not None:
            issues.extend(fake_issues(request.fakes, registry))
            fake_source = FakeExecutors(request.fakes, registry)
        executors, executor_issues = self.choose_executors(plan, registry, request.fakes, fake_source)
        issues.extend(executor_issues)
        sessions = sessions_of(plan)
        resources = self.planner.plan(plan, registry=registry, tree=str(tree) if tree is not None else None,
                                      sessions=sessions, backend=request.backend, backends=request.backends)
        issues.extend(self.resource_issues(plan, registry, resources, request, request.fakes))
        target: Target | None = None
        try:
            target = self.target(request, tree)
        except AdmissionError as refused:
            issues.extend(refused.issues)
        ordered = _in_walk_order(plan, issues)
        errors = [issue for issue in ordered if issue.severity == "error"]
        if errors or target is None:
            raise AdmissionError(errors[0].message if errors else "the run has no target", ordered)
        info = RunInfo(id=run_id, project=target.project, session=target.session,
                       tree=str(tree) if tree is not None else None,
                       spec=built.spec if built is not None else None, plan_hash=plan.hash, plan_kind=plan.kind,
                       plan_name=plan.name, options=request.options, instance=request.instance, actor=actor,
                       user_id=user_id, fakes=request.fakes, clock=request.clock, created_at=created_at)
        return AdmittedRun(info=info, plan=plan, registry=entry, executors=executors, resources=resources,
                           sessions=sessions, request=request, environment=self.environment(entry, executors),
                           issues=tuple(ordered), fakes=fake_source,
                           simulated=self._simulated(executors, resources, request))

    def _request_tree(self, request: RunRequest, issues: list[IssueOut]) -> Path | None:
        if not request.tree:
            return None
        tree = Path(request.tree).expanduser()
        if not tree.is_dir():
            issues.append(_error("plan.tree", f"tree {request.tree} is not a directory on the hub; pass the work tree "
                                              "the plan was built from"))
            return None
        return tree.resolve()

    def choose_executors(self, plan: Plan, registry: Registry, fakes: FakeProfile | None,
                         fake_source: FakeExecutors | None) -> tuple[dict[str, ExecutorChoice], list[IssueOut]]:
        chosen: dict[str, ExecutorChoice] = {}
        issues: list[IssueOut] = []
        seen: set[str] = set()
        for node, _ancestors in plan.walk():
            if node.op in seen or not registry.has_op(node.op):
                continue
            seen.add(node.op)
            op = registry.op(node.op)
            try:
                choice = self.executors.choose(op, fakes=fakes, fake_source=fake_source)
            except ValueError as problem:
                if op.executor is not None and op.location != "hub":
                    issues.append(self._location_issue(node.op, op.executor, op.location, node.id))
                else:
                    issues.append(_error("executor.missing", str(problem), node=node.id))
                continue
            if choice is None:
                issues.append(_error("executor.missing", _MISSING_EXECUTOR.format(op=node.op), node=node.id))
                continue
            if (not choice.fake and not choice.native and op.executor is not None and op.location != "hub"
                    and choice.executor.name.startswith(PLUGIN_EXECUTOR_PREFIX)):
                issues.append(self._location_issue(node.op, op.executor, op.location, node.id))
                continue
            chosen[node.op] = choice
        fake_drivers = fakes is not None and fakes.drivers
        for resource in plan.resources:
            if not resource.kind.startswith("lease."):
                continue
            platform = resource.kind.split(".", 1)[1]
            if self.drivers.provider(platform) is not None or fake_drivers:
                continue
            problem = (self.builtin_drivers(platform) if self.builtin_drivers is not None
                       else "no built-in driver is configured")
            if problem is not None:
                issues.append(_error("driver.missing",
                                     f"lease {resource.id} needs a {platform} driver and this hub has none ({problem}); "
                                     "run with fakes to simulate it"))
        return chosen, issues

    @staticmethod
    def _location_issue(op: str, executor: str, location: str, node: str) -> IssueOut:
        return _error("executor.location",
                      f"{op} names the Python executor {executor} but runs at location {location}; this hub runs "
                      "plugin executors only at location hub (worker ops without executor= go through the driver "
                      "port); run with fakes forcing it to simulate it", node=node)

    def resource_issues(self, plan: Plan, registry: Registry, resources: ResourcePlan, request: RunRequest,
                        fakes: FakeProfile | None) -> list[IssueOut]:
        issues: list[IssueOut] = []
        if self.devices is not None:
            for kind in lease_kinds(plan):
                if kind not in _DEVICE_KINDS:
                    continue
                available, why = self.devices(kind)
                if not available:
                    issues.append(_error("lease.kind", f"the plan uses a {kind} lease but this hub has no {kind} "
                                                       f"devices: {why}"))
        if request.options.seed == "restore" and not (fakes is not None and fakes.seeds):
            issues.extend(self._seed_issues(registry, resources, request))
        issues.extend(self._cassette_issues(plan, resources, request, fakes))
        return issues

    def _seed_issues(self, registry: Registry, resources: ResourcePlan, request: RunRequest) -> list[IssueOut]:
        issues: list[IssueOut] = []
        for need in resources.backends:
            if not need.seed or need.source not in ("plan", "request"):
                continue
            try:
                definition = self.planner.choose_backend(need, registry=registry, tree=request.tree,
                                                         session=None).definition
            except ResourceError:
                continue
            if self.seeds.hook(definition) is None:
                issues.append(_error("seed.hook",
                                     f"no seed engine can restore the seed {need.seed} into the backend {definition}; "
                                     "run with seed=skip to use the backend as it is, or with fakes to simulate the "
                                     "restore"))
        return issues

    def _cassette_issues(self, plan: Plan, resources: ResourcePlan, request: RunRequest,
                         fakes: FakeProfile | None) -> list[IssueOut]:
        if fakes is not None and fakes.cassettes:
            return []
        issues: list[IssueOut] = []
        hook = self.cassettes.hook()
        for need in resources.cassettes:
            if need.mode != _REPLAY:
                continue
            if hook is None:
                issues.append(self._cassette_issue(need, "no cassette store is registered on this hub"))
                continue
            if self._session_has_backend(plan, need):
                continue
            probe = RunInfo(id="admission", project=request.project or "", session=request.session or "",
                            plan_hash=plan.hash, plan_kind=plan.kind, plan_name=plan.name, options=request.options,
                            created_at=0.0)
            if not self.planner.cassette_available(probe, plan, need, None, hook):
                issues.append(self._cassette_issue(need, "it was never recorded for this plan"))
        return issues

    @staticmethod
    def _cassette_issue(need: CassetteNeed, why: str) -> IssueOut:
        return _error("cassette.missing", f"the replay cassette {need.name} of node {need.node_id} does not exist: "
                                          f"{why}; run it once with mode record or auto", node=need.node_id)

    @staticmethod
    def _session_has_backend(plan: Plan, need: CassetteNeed) -> bool:
        backends = [resource for resource in plan.resources if resource.kind == _BACKEND_KIND]
        key = need.session_key or MAIN_SESSION
        if any((resource.session or MAIN_SESSION) == key for resource in backends):
            return True
        return len(backends) == 1

    def _simulated(self, executors: Mapping[str, ExecutorChoice], resources: ResourcePlan,
                   request: RunRequest) -> bool:
        if any(choice.fake for choice in executors.values()):
            return True
        fakes = request.fakes
        if fakes is None:
            return False
        if fakes.drivers and any(self.drivers.provider(need.platform) is None for need in resources.leases):
            return True
        if fakes.seeds and request.options.seed == "restore" and any(need.seed for need in resources.backends):
            return True
        return fakes.cassettes and bool(resources.cassettes)

    def environment(self, entry: TreeRegistry, executors: Mapping[str, ExecutorChoice]) -> EnvironmentOut:
        return EnvironmentOut(
            harness=__version__, source=harness_digest(), python=host_platform.python_version(),
            machine=f"{host_platform.system().lower()}-{host_platform.machine().lower()}",
            executors={op: executors[op].executor.identity for op in sorted(executors)},
            plugins={plugin_id: PluginStamp(version=state.version, digest=state.digest)
                     for plugin_id, state in sorted(entry.plugins.items())},
            project_digest=entry.project_digest,
            fakes=sorted(op for op, choice in executors.items() if choice.fake))


__all__ = [
    "DEFAULT_SESSION",
    "ENGINE_ISSUE_CODES",
    "Admission",
    "AdmittedRun",
    "Target",
    "clock_issues",
    "default_target",
    "fake_issues",
]
