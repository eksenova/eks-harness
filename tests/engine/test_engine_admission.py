from __future__ import annotations

import platform
import re
import shutil
import subprocess
import textwrap
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
from pydantic import JsonValue

import eks
from eks.workspace import plan_spec
from eks_harness._version import __version__
from eks_harness.config import Config
from eks_harness.daemon.context import AppContext
from eks_harness.engine.admission import (
    DEFAULT_SESSION,
    ENGINE_ISSUE_CODES,
    Admission,
    AdmittedRun,
    Target,
    clock_issues,
    default_target,
    fake_issues,
)
from eks_harness.engine.build import PlanBuilder
from eks_harness.engine.cache import harness_digest
from eks_harness.engine.drivers import DriverPort, DriverRegistry
from eks_harness.engine.errors import AdmissionError, BuildError
from eks_harness.engine.executors import (
    BodyOutcome,
    ContainerEntry,
    ExecContext,
    ExecResult,
    ExecutorRegistry,
    ExecutorSpec,
)
from eks_harness.engine.model import (
    BackendRef,
    FakeProfile,
    IssueOut,
    LeaseHandle,
    RunClock,
    RunInfo,
    RunOptions,
    RunRequest,
    SeedRestoreResult,
    sessions_of,
)
from eks_harness.engine.registries import HubRegistries
from eks_harness.engine.resources import (
    CassetteHooks,
    CassetteRequest,
    ResourcePlanner,
    SeedHooks,
    SeedRestoreRequest,
)
from eks_harness.ir.plan import Plan, Requirement, SessionSpec
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.ir.time import TempoMap
from eks_harness.ops.fakes import FakeExecutors
from eks_harness.plugins.host import PluginHost

FIXTURES = Path(__file__).resolve().parent.parent / "eks" / "cli_fixtures"
RUN_ID = "01JTESTRUN0000000000000000"
CREATED = 1_760_000_000.0
SCREEN = {"screen": "artifact:SHOT1"}
CLOCK = RunClock(tempo=TempoMap(bpm=120))

PLAIN_FLOW = """
    import eks


    @eks.flow(app="web-app")
    def plain(s: eks.Session):
        s.ui.nav("/a")
"""

CASSETTE_FLOW = """
    import eks


    @eks.flow(app="web-app")
    def taped(s: eks.Session):
        s.api.get("/api/ping")
        with s.net.cassette("banka", mode="replay"):
            s.ui.nav("/a")
"""

CUED_SCENE = """
    import eks
    from eks import t


    @eks.scene(kind="blender", engine="cycles", samples=32, seed=7)
    def cued(sc: eks.BlenderScene):
        p = sc.load("assets/phone.blend", obj="Phone")
        sc.on(t("cue:drop"), p.pulse(scale=1.04))
        sc.on(t("marker:hits#1"), p.pulse(scale=1.02))
"""


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


def ok(call: ExecContext) -> ExecResult:
    return ExecResult()


class Body:
    def enter(self, call: ExecContext, /) -> ContainerEntry:
        return ContainerEntry()

    def exit(self, call: ExecContext, body: BodyOutcome, /) -> ExecResult:
        return ExecResult()


class Provider:
    def __init__(self, *platforms: str) -> None:
        self._platforms = frozenset(platforms)

    @property
    def name(self) -> str:
        return "test"

    @property
    def platforms(self) -> frozenset[str]:
        return self._platforms

    def open(self, lease: LeaseHandle, session: SessionSpec | None, run: RunInfo) -> DriverPort:
        raise RuntimeError("admission never opens driver ports")


class Restorer:
    def restore(self, request: SeedRestoreRequest, info: Mapping[str, JsonValue], *,
                registry: object = None) -> SeedRestoreResult:
        return SeedRestoreResult(resource_id=request.resource_id, seed=request.seed, key=request.key, restored=True)


class Tapes:
    def __init__(self, available: bool) -> None:
        self.answer = available
        self.requests: list[CassetteRequest] = []

    def available(self, request: CassetteRequest) -> bool:
        self.requests.append(request)
        return self.answer

    def opened(self, request: CassetteRequest) -> Mapping[str, JsonValue]:
        return {}

    def closed(self, request: CassetteRequest, recorded: Mapping[str, JsonValue]) -> str | None:
        return None


def acme_executor(call: ExecContext) -> object:
    return {"ok": True}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    shutil.copytree(FIXTURES, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root.resolve()


@pytest.fixture
def host(config: Config, tmp_path: Path) -> PluginHost:
    builtin = tmp_path / "no-builtins"
    builtin.mkdir(exist_ok=True)
    return PluginHost(config.paths, config, builtin_root=builtin, include_packages=False)


@pytest.fixture
def builder(host: PluginHost) -> PlanBuilder:
    return PlanBuilder(HubRegistries(host))


@pytest.fixture
def executors() -> ExecutorRegistry:
    chosen = ExecutorRegistry()
    chosen.register(ExecutorSpec("test.hub", "1", ops=("api", "backend", "expect"), run=ok))
    chosen.register(ExecutorSpec("eks.worker", "1", ops=("*",), run=ok, locations=frozenset({"worker"})))
    chosen.register(ExecutorSpec("eks.worker.container", "1", ops=("*",), container=Body(),
                                 locations=frozenset({"worker"})))
    return chosen


@pytest.fixture
def seeds() -> SeedHooks:
    return SeedHooks()


@pytest.fixture
def cassettes() -> CassetteHooks:
    return CassetteHooks()


@pytest.fixture
def drivers() -> DriverRegistry:
    return DriverRegistry()


@pytest.fixture
def devices() -> dict[str, tuple[bool, str]]:
    return {"ios": (True, "xcrun"), "android": (True, "android sdk")}


@pytest.fixture
def admission(ctx: AppContext, builder: PlanBuilder, executors: ExecutorRegistry, drivers: DriverRegistry,
              seeds: SeedHooks, cassettes: CassetteHooks, devices: dict[str, tuple[bool, str]]) -> Admission:
    def available(kind: str) -> tuple[bool, str]:
        return devices[kind]

    return Admission(builder=builder, executors=executors, drivers=drivers,
                     planner=ResourcePlanner(ctx, seeds=seeds, cassettes=cassettes), seeds=seeds,
                     cassettes=cassettes, devices=available)


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def admit(admission: Admission, **fields: object) -> AdmittedRun:
    return admission.admit(RunRequest.model_validate(fields), run_id=RUN_ID, created_at=CREATED, actor="ada",
                           user_id=7)


def refused(admission: Admission, **fields: object) -> AdmissionError:
    with pytest.raises(AdmissionError) as caught:
        admit(admission, **fields)
    return caught.value


def codes(issues: tuple[IssueOut, ...] | list[IssueOut]) -> list[str]:
    return [issue.code for issue in issues]


def spec_plan(tree: Path, spec: str, **params: str) -> Plan:
    return plan_spec(spec, params=params, cwd=tree)


def test_exactly_one_plan_source(admission: Admission, tree: Path) -> None:
    error = refused(admission)
    assert error.code == "run_refused"
    assert codes(error.issues) == ["plan.source"]
    assert "names none of them" in error.message
    plan = spec_plan(tree, ".harness/flows/subeler.py")
    both = refused(admission, plan=plan, spec=".harness/flows/subeler.py", tree=str(tree))
    assert codes(both.issues) == ["plan.source"]
    assert "plan and spec" in both.message
    assert set(ENGINE_ISSUE_CODES) >= {"plan.source", "plan.tree", "tempo.external", "target.project"}


def test_plan_json_is_taken_as_given_and_specs_are_built(admission: Admission, tree: Path) -> None:
    plan = spec_plan(tree, ".harness/flows/subeler.py")
    run = admit(admission, plan=plan, tree=str(tree), fakes=FakeProfile())
    assert run.plan is plan
    assert run.info.spec is None
    assert run.info.tree == str(tree)
    built = admit(admission, spec=".harness/flows/subeler.py", cwd=str(tree), params={"rows": "2"},
                  fakes=FakeProfile())
    assert built.plan.name == "subeler"
    assert built.plan.params["rows"] == 2
    assert built.info.spec == ".harness/flows/subeler.py"
    assert built.info.tree == str(tree)
    assert built.registry.tree == tree
    inline = admit(admission, source=textwrap.dedent(PLAIN_FLOW), tree=str(tree), fakes=FakeProfile())
    assert inline.plan.name == "plain"
    assert inline.info.spec is None
    with pytest.raises(BuildError):
        admit(admission, spec=".harness/flows/raises.py", cwd=str(tree), fakes=FakeProfile())


def test_plan_without_a_tree_is_refused_when_it_needs_one(admission: Admission, tree: Path) -> None:
    @eks.flow(app="web-app", platform="web")
    def calls(s: eks.Session) -> None:
        s.api.get("/api/branches")

    plan = eks.plan(calls)
    error = refused(admission, plan=plan, fakes=FakeProfile())
    tree_issue = next(issue for issue in error.issues if issue.code == "plan.tree")
    assert "it uses a backend" in tree_issue.message
    missing = refused(admission, plan=plan, tree=str(tree / "nowhere"), fakes=FakeProfile())
    assert "is not a directory" in next(issue for issue in missing.issues if issue.code == "plan.tree").message
    scene = spec_plan(tree, ".harness/scenes/phone.py", **SCREEN)
    media = refused(admission, plan=scene, clock=CLOCK, fakes=FakeProfile())
    assert "it reads the tree file" in next(issue for issue in media.issues if issue.code == "plan.tree").message
    needing = scene.model_copy(update={"requires": [Requirement(plugin="acme", version="1.0.0")]})
    plugin = refused(admission, plan=needing, clock=CLOCK, fakes=FakeProfile())
    assert "requires plugins acme" in next(issue for issue in plugin.issues if issue.code == "plan.tree").message


def test_validation_errors_refuse_with_every_issue_and_warnings_stay(admission: Admission, tree: Path) -> None:
    broken = spec_plan(tree, ".harness/flows/broken.py")
    error = refused(admission, plan=broken, tree=str(tree), fakes=FakeProfile())
    errors = [issue for issue in error.issues if issue.severity == "error"]
    assert errors
    assert error.message == errors[0].message
    assert all(isinstance(issue, IssueOut) for issue in error.issues)
    plan_level = [index for index, issue in enumerate(error.issues) if issue.node is None]
    assert plan_level == list(range(len(plan_level)))
    write(tree / ".harness/flows/plain.py", PLAIN_FLOW)
    run = admit(admission, spec=".harness/flows/plain.py", cwd=str(tree), fakes=FakeProfile())
    assert codes(run.issues) == ["clock.now"]
    assert run.issues[0].severity == "warning"


def test_external_tempo_needs_a_run_clock(admission: Admission, tree: Path) -> None:
    error = refused(admission, spec=".harness/scenes/phone.py", cwd=str(tree), params=SCREEN, fakes=FakeProfile())
    tempo = [issue for issue in error.issues if issue.code == "tempo.external"]
    assert tempo
    assert all(issue.node is not None for issue in tempo)
    assert tempo[0].message.startswith("scene phone uses ")
    assert tempo[0].message.endswith("but its tempo is external; pass a run clock with bpm, cues and markers, or "
                                     "embed the scene in a score")
    no_bpm = refused(admission, spec=".harness/scenes/phone.py", cwd=str(tree), params=SCREEN,
                     clock=RunClock(tempo=TempoMap(external=True)), fakes=FakeProfile())
    assert "tempo.external" in codes(no_bpm.issues)
    run = admit(admission, spec=".harness/scenes/phone.py", cwd=str(tree), params=SCREEN, clock=CLOCK,
                fakes=FakeProfile())
    assert run.info.clock == CLOCK
    assert run.plan.clock.tempo is not None and run.plan.clock.tempo.external
    own = refused(admission, spec=".harness/flows/subeler.py", cwd=str(tree), clock=CLOCK, fakes=FakeProfile())
    assert [issue.message for issue in own.issues if issue.code == "tempo.external"] == [
        "the plan has its own clock"]


def test_external_tempo_needs_the_cues_and_markers_it_uses(admission: Admission, tree: Path) -> None:
    write(tree / ".harness/scenes/cued.py", CUED_SCENE)
    error = refused(admission, spec=".harness/scenes/cued.py", cwd=str(tree), clock=CLOCK, fakes=FakeProfile())
    (issue,) = [issue for issue in error.issues if issue.code == "tempo.external"]
    assert "cue 'drop'" in issue.message
    assert "marker stream 'hits'" in issue.message
    full = RunClock.model_validate({"tempo": {"bpm": 120}, "cues": {"drop": "2s"},
                                    "markers": {"hits": ["1s", "2s"]}})
    run = admit(admission, spec=".harness/scenes/cued.py", cwd=str(tree), clock=full, fakes=FakeProfile())
    assert run.info.clock == full
    plan = run.plan
    assert clock_issues(plan, run.registry.registry, full) == []
    assert codes(clock_issues(plan, run.registry.registry, None)) == ["tempo.external"]


def test_fake_patterns_must_name_ops(admission: Admission, tree: Path, registry: Registry) -> None:
    error = refused(admission, spec=".harness/flows/subeler.py", cwd=str(tree),
                    fakes=FakeProfile(families=["uii"], force=["api.gett"]))
    patterns = [issue for issue in error.issues if issue.code == "fakes.pattern"]
    assert len(patterns) == 2
    assert "'uii'" in patterns[0].message and "did you mean 'ui'" in patterns[0].message
    assert "force pattern 'api.gett'" in patterns[1].message
    assert fake_issues(FakeProfile(families=["ui", "media.*", "api.get", "*"]), registry) == []


def test_missing_executors_name_the_milestone(admission: Admission, tree: Path) -> None:
    error = refused(admission, spec=".harness/scenes/phone.py", cwd=str(tree), params=SCREEN, clock=CLOCK)
    missing = {issue.message for issue in error.issues if issue.code == "executor.missing"}
    assert ("no executor for blender.render on this hub; driver executors arrive in M2, seed executors in M3, "
            "media executors in M4 and M5; run with fakes to simulate it") in missing
    assert all(issue.node is not None for issue in error.issues if issue.code == "executor.missing")


def test_plugin_executor_location_and_resolution(admission: Admission, registry: Registry) -> None:
    @eks.op("acme", out="json", location="worker", executor=f"{__name__}:acme_executor", resources=("lease",))
    class Ping(eks.Args):
        target: str = "self"

    @eks.op("acme", out="json", executor=f"{__name__}:acme_executor")
    class Local(eks.Args):
        value: int = 1

    @eks.op("acme", out="json", executor=f"{__name__}:nowhere")
    class Lost(eks.Args):
        value: int = 1

    @eks.flow(app="web-app", platform="web")
    def pinger(s: eks.Session) -> None:
        s.op("acme.ping")
        s.op("acme.local")
        s.op("acme.lost")

    plan = eks.plan(pinger)
    chosen, issues = admission.choose_executors(plan, registry, None, None)
    by_code = {issue.code: issue for issue in issues}
    assert set(by_code) == {"executor.location", "executor.missing", "driver.missing"}
    assert "acme.ping names the Python executor" in by_code["executor.location"].message
    assert "location worker" in by_code["executor.location"].message
    assert "nowhere" in by_code["executor.missing"].message
    assert chosen["acme.local"].executor.name == "plugin:project:acme.local"
    assert "acme.ping" not in chosen
    profile = FakeProfile(force=["acme.ping", "acme.lost"])
    forced, forced_issues = admission.choose_executors(plan, registry, profile, FakeExecutors(profile, registry))
    assert codes(forced_issues) == []
    assert forced["acme.ping"].fake and forced["acme.lost"].fake


def test_drivers_are_needed_for_every_lease(admission: Admission, tree: Path, drivers: DriverRegistry) -> None:
    write(tree / ".harness/flows/plain.py", PLAIN_FLOW)
    error = refused(admission, spec=".harness/flows/plain.py", cwd=str(tree))
    (driver,) = [issue for issue in error.issues if issue.code == "driver.missing"]
    assert "web driver" in driver.message
    simulated = admit(admission, spec=".harness/flows/plain.py", cwd=str(tree), fakes=FakeProfile())
    assert simulated.simulated
    assert simulated.environment.fakes == []
    drivers.register(Provider("web"))
    real = admit(admission, spec=".harness/flows/plain.py", cwd=str(tree))
    assert not real.simulated
    assert real.fakes is None
    assert real.info.fakes is None


def test_device_kinds_are_checked(admission: Admission, tree: Path, devices: dict[str, tuple[bool, str]]) -> None:
    devices["ios"] = (False, "xcrun not found (iOS simulators need macOS with Xcode)")
    error = refused(admission, spec="mobile-app/.harness/flows/tapper.py", cwd=str(tree), platform="ios",
                    fakes=FakeProfile())
    (kind,) = [issue for issue in error.issues if issue.code == "lease.kind"]
    assert "ios" in kind.message and "xcrun not found" in kind.message
    run = admit(admission, spec="mobile-app/.harness/flows/tapper.py", cwd=str(tree), platform="android",
                fakes=FakeProfile())
    assert [need.platform for need in run.resources.leases] == ["android"]


def test_seeded_backends_need_a_seed_hook(admission: Admission, tree: Path, seeds: SeedHooks,
                                         drivers: DriverRegistry) -> None:
    drivers.register(Provider("web"))
    no_fake_seeds = FakeProfile(seeds=False)
    error = refused(admission, spec=".harness/flows/subeler.py", cwd=str(tree), fakes=no_fake_seeds)
    (seed,) = [issue for issue in error.issues if issue.code == "seed.hook"]
    assert "seed standard into the backend eksenapi" in seed.message
    request = refused(admission, spec=".harness/flows/subeler.py", cwd=str(tree), fakes=no_fake_seeds,
                      backend=BackendRef(ref="eksenapi"))
    assert "seed.hook" in codes(request.issues)
    skipped = admit(admission, spec=".harness/flows/subeler.py", cwd=str(tree), fakes=no_fake_seeds,
                    options=RunOptions(seed="skip"))
    assert codes(skipped.issues) == []
    assert not skipped.simulated
    faked = admit(admission, spec=".harness/flows/subeler.py", cwd=str(tree), fakes=FakeProfile())
    assert faked.simulated
    seeds.register("eksenapi", Restorer())
    hooked = admit(admission, spec=".harness/flows/subeler.py", cwd=str(tree), fakes=no_fake_seeds)
    assert [need.seed for need in hooked.resources.backends] == ["standard"]


def test_replay_cassettes_must_exist_when_the_hook_can_tell(admission: Admission, tree: Path,
                                                            cassettes: CassetteHooks, registry: Registry) -> None:
    write(tree / ".harness/flows/taped.py", CASSETTE_FLOW)
    no_fake_tapes = FakeProfile(cassettes=False)
    error = refused(admission, spec=".harness/flows/taped.py", cwd=str(tree), fakes=no_fake_tapes)
    (missing,) = [issue for issue in error.issues if issue.code == "cassette.missing"]
    assert "banka" in missing.message and "no cassette store" in missing.message
    assert missing.node is not None
    faked = admit(admission, spec=".harness/flows/taped.py", cwd=str(tree), fakes=FakeProfile())
    assert faked.simulated
    tapes = Tapes(available=False)
    cassettes.register(tapes)
    with_backend = admit(admission, spec=".harness/flows/taped.py", cwd=str(tree), fakes=no_fake_tapes)
    assert tapes.requests == []
    plan = with_backend.plan
    unbacked = plan.model_copy(update={"resources": [item for item in plan.resources if item.kind != "backend"]})
    request = RunRequest(plan=unbacked, tree=str(tree), fakes=no_fake_tapes)
    needs = admission.planner.plan(unbacked, registry=with_backend.registry.registry, tree=str(tree),
                                   sessions=sessions_of(unbacked))
    issues = admission.resource_issues(unbacked, with_backend.registry.registry, needs, request, no_fake_tapes)
    assert codes(issues) == ["cassette.missing"]
    assert "never recorded" in issues[0].message
    (asked,) = tapes.requests
    assert asked.backend is None and asked.mode == "replay" and asked.name == "banka"
    tapes.answer = True
    assert admission.resource_issues(unbacked, with_backend.registry.registry, needs, request, no_fake_tapes) == []


def git(tree: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(tree), *args], check=True, capture_output=True)


def test_targets_from_request_tree_and_branch(admission: Admission, tree: Path, tmp_path: Path) -> None:
    assert default_target(None) == (None, None)
    assert default_target(tree)[0] == "acme/cli"
    request = RunRequest(spec="x.py")
    assert admission.target(request, tree).project == "acme/cli"
    assert admission.target(request.model_copy(update={"project": "own/p", "session": "s1"}), tree) == Target(
        project="own/p", session="s1")
    git(tree, "init", "-q", "-b", "feature/hedef")
    git(tree, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q", "--allow-empty", "-m", "x")
    assert default_target(tree) == ("acme/cli", "feature/hedef")
    assert admission.target(request, tree) == Target(project="acme/cli", session="feature/hedef")
    bare = tmp_path / "bare"
    bare.mkdir()
    assert admission.target(request.model_copy(update={"project": "own/p"}), bare).session == DEFAULT_SESSION
    with pytest.raises(AdmissionError) as caught:
        admission.target(request, bare)
    assert codes(caught.value.issues) == ["target.project"]
    write(bare / ".harness/project.toml", "[project\nid = 'broken'\n")
    assert default_target(bare) == (None, None)
    with pytest.raises(AdmissionError):
        admission.target(request, None)


def test_admit_refuses_without_a_project(admission: Admission, tmp_path: Path) -> None:
    @eks.flow(app="web-app", platform="web")
    def visit(s: eks.Session) -> None:
        s.ui.nav("/a")

    plan = eks.plan(visit)
    bare = tmp_path / "bare"
    bare.mkdir()
    write(bare / "web-app/.harness/app.toml", '[app]\nname = "web-app"\nplatforms = ["web"]\n')
    error = refused(admission, plan=plan, tree=str(bare), fakes=FakeProfile())
    assert "target.project" in codes(error.issues)
    run = admit(admission, plan=plan, tree=str(bare), project="own/p", fakes=FakeProfile())
    assert run.info.project == "own/p"
    assert run.info.session == DEFAULT_SESSION


def test_admitted_run_carries_everything_the_run_needs(admission: Admission, tree: Path) -> None:
    profile = FakeProfile(families=["*"])
    run = admit(admission, spec=".harness/flows/subeler.py", cwd=str(tree), fakes=profile, session="review",
                instance="laptop", options=RunOptions(debug=True, breakpoints=["op:ui.nav"]))
    info = run.info
    assert info.id == RUN_ID
    assert info.project == "acme/cli"
    assert info.session == "review"
    assert info.created_at == CREATED
    assert info.actor == "ada" and info.user_id == 7
    assert info.instance == "laptop"
    assert info.options.debug and info.options.breakpoints == ["op:ui.nav"]
    assert info.fakes == profile
    assert info.clock is None
    assert info.plan_hash == run.plan.hash
    assert info.plan_kind == "flow" and info.plan_name == "subeler"
    plan_ops = {node.op for node, _ancestors in run.plan.walk()}
    assert set(run.executors) == plan_ops
    assert run.executors["api.get"].executor.name == "test.hub"
    assert run.executors["ui.nav"].executor.name == "eks.worker"
    assert run.executors["check.checkpoint"].executor.name == "eks.worker"
    assert [need.platform for need in run.resources.leases] == ["web"]
    (backend,) = run.resources.backends
    assert backend.seed == "standard" and backend.source == "plan" and backend.seed_hash is not None
    assert dict(run.sessions) == sessions_of(run.plan)
    assert set(run.sessions) == {"main"}
    assert run.request.session == "review"
    assert isinstance(run.fakes, FakeExecutors)
    assert run.simulated
    environment = run.environment
    assert environment.harness == __version__
    assert environment.source == harness_digest()
    assert environment.python == platform.python_version()
    assert environment.machine == f"{platform.system().lower()}-{platform.machine().lower()}"
    assert re.match(r"^[a-z0-9_]+-[a-z0-9_]+$", environment.machine)
    assert environment.executors["api.get"] == "test.hub@1"
    assert environment.executors["ui.nav"] == "eks.worker@1"
    assert environment.plugins["fixtures"].version == "1.0.0"
    assert environment.plugins["fixtures"].digest == run.registry.plugin("fixtures")
    assert environment.project_digest == run.registry.project_digest
    assert environment.project_digest is not None
    assert environment.fakes == []
    assert environment.backends == {}


def test_faked_ops_are_listed_in_the_environment(admission: Admission, tree: Path) -> None:
    run = admit(admission, spec=".harness/scenes/phone.py", cwd=str(tree), params=SCREEN, clock=CLOCK,
                fakes=FakeProfile())
    faked = sorted(op for op, choice in run.executors.items() if choice.fake)
    assert faked
    assert "blender.render" in faked
    assert run.environment.fakes == faked
    assert all(run.environment.executors[op] == "eks.fake@1" for op in faked)
    assert run.simulated
    assert run.resources.slots == ["blender"]


def callable_devices(answers: dict[str, tuple[bool, str]]) -> Callable[[str], tuple[bool, str]]:
    return answers.__getitem__


def test_admission_without_a_device_probe_skips_device_checks(ctx: AppContext, builder: PlanBuilder,
                                                               executors: ExecutorRegistry, tree: Path) -> None:
    seeds, cassettes = SeedHooks(), CassetteHooks()
    plain = Admission(builder=builder, executors=executors, drivers=DriverRegistry(),
                      planner=ResourcePlanner(ctx, seeds=seeds, cassettes=cassettes), seeds=seeds,
                      cassettes=cassettes)
    run = plain.admit(RunRequest(spec="mobile-app/.harness/flows/tapper.py", cwd=str(tree), platform="ios",
                                 fakes=FakeProfile()), run_id=RUN_ID, created_at=CREATED)
    assert [need.platform for need in run.resources.leases] == ["ios"]
    strict = Admission(builder=builder, executors=executors, drivers=DriverRegistry(),
                       planner=ResourcePlanner(ctx, seeds=seeds, cassettes=cassettes), seeds=seeds,
                       cassettes=cassettes, devices=callable_devices({"ios": (False, "no simulators")}))
    with pytest.raises(AdmissionError, match="no simulators"):
        strict.admit(RunRequest(spec="mobile-app/.harness/flows/tapper.py", cwd=str(tree), platform="ios",
                                fakes=FakeProfile()), run_id=RUN_ID, created_at=CREATED)


INLINE_OP_FLOW = """
    import eks


    def run_local(call):
        return {"ok": True}


    @eks.op("acme", name="local", pure=True, executor=run_local)
    class Local(eks.Args):
        value: int = 1


    @eks.flow(app="web-app")
    def local(s: eks.Session):
        s.ui.nav("/a")
        s.op("acme.local", value=2)
"""


def test_inline_sources_are_admitted_with_what_they_register(admission: Admission, builder: PlanBuilder,
                                                             tree: Path) -> None:
    run = admit(admission, source=textwrap.dedent(INLINE_OP_FLOW), tree=str(tree), fakes=FakeProfile())
    assert run.registry.registry.has_op("acme.local")
    local = run.executors["acme.local"]
    assert not local.fake
    assert local.executor.name.endswith(":acme.local")
    assert local.executor.run is not None
    published = builder.registries.for_tree(tree)
    assert not published.registry.has_op("acme.local")
    assert run.environment.project_digest == run.registry.project_digest
    assert run.environment.project_digest != published.project_digest
    assert "op.unknown" not in codes(run.issues)


def test_a_plan_json_in_a_broken_tree_is_a_build_error(admission: Admission, tree: Path) -> None:
    plan = spec_plan(tree, ".harness/flows/subeler.py")
    seed = tree / ".harness/seeds/standard.py"
    text = seed.read_text(encoding="utf-8").rstrip("\n") + "\n\nboom = 1 / 0\n"
    seed.write_text(text, encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        admit(admission, plan=plan, tree=str(tree), fakes=FakeProfile())
    assert caught.value.message == "ZeroDivisionError: division by zero"
    assert caught.value.where == f".harness/seeds/standard.py:{len(text.splitlines())}: boom = 1 / 0"
    with pytest.raises(BuildError) as built:
        admit(admission, spec=".harness/flows/subeler.py", cwd=str(tree), fakes=FakeProfile())
    assert built.value.where == caught.value.where
