from __future__ import annotations

import gc
import hashlib
import re
import shutil
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eks.discovery import module_name
from eks.validation import validate as validate_plan
from eks_harness.config import Config
from eks_harness.engine import build as build_module
from eks_harness.engine.build import (
    INLINE_PREFIX,
    ONESHOT_NAME,
    BuildRequest,
    EntryFile,
    OneShotRequest,
    PlanBuilder,
    issues_of,
    tree_of,
)
from eks_harness.engine.errors import BuildError
from eks_harness.engine.model import IssueOut
from eks_harness.engine.registries import HubRegistries, TreeRegistry
from eks_harness.engine.validator import Issue, Report
from eks_harness.ir.plan import Plan, Requirement
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.plugins.host import PluginHost

FIXTURES = Path(__file__).resolve().parent.parent / "eks" / "cli_fixtures"
INLINE_NAME = re.compile(rf"^{INLINE_PREFIX}[0-9a-f]{{16}}\.py$")
NOW = "2026-01-15T09:00:00+03:00"

INLINE_FLOW = """
import eks


@eks.flow(app="web-app", seed="standard", as_="harman-admin")
def first(s: eks.Session):
    s.ui.nav("/first")


@eks.flow(app="web-app", seed="standard", as_="harman-admin")
def second(s: eks.Session, page: str = "/second"):
    s.ui.nav(page)
"""


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


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


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def ops(plan: Plan) -> list[str]:
    return [node.op for node, _ancestors in plan.walk()]


def inline_files(tree: Path) -> list[Path]:
    return sorted(tree.rglob(f"{INLINE_PREFIX}*"))


def test_build_spec_plans_the_flow_in_the_tree_registry(builder: PlanBuilder, tree: Path) -> None:
    built = builder.build(BuildRequest(spec=".harness/flows/subeler.py::subeler", cwd=tree,
                                       params={"branch": "Moda", "rows": "3"}))
    assert built.plan.kind == "flow"
    assert built.plan.name == "subeler"
    assert built.plan.params == {"branch": "Moda", "rows": 3}
    assert built.tree == tree
    assert built.spec == ".harness/flows/subeler.py::subeler"
    assert isinstance(built.registry, TreeRegistry)
    assert built.registry.tree == tree
    assert built.registry.registry is not default_registry()
    assert built.registry.registry.has_op("ui.nav")
    assert all(isinstance(issue, IssueOut) for issue in built.issues)
    assert not [issue for issue in built.issues if issue.severity == "error"]
    assert built.plan.session is not None
    assert built.plan.session.platform == "web"
    assert built.plan.session.now == NOW


def test_build_spec_resolves_relative_specs_against_cwd_and_tree(builder: PlanBuilder, tree: Path) -> None:
    by_tree = builder.build(BuildRequest(spec=".harness/flows/subeler.py", tree=tree))
    absolute = builder.build(BuildRequest(spec=str(tree / ".harness/flows/subeler.py")))
    assert by_tree.plan.hash == absolute.plan.hash
    assert absolute.tree == tree


def test_build_spec_takes_the_platform_of_a_matrix_app(builder: PlanBuilder, tree: Path) -> None:
    spec = "mobile-app/.harness/flows/tapper.py"
    android = builder.build(BuildRequest(spec=spec, cwd=tree, platform="android"))
    ios = builder.build(BuildRequest(spec=spec, cwd=tree, platform="ios"))
    assert android.plan.session is not None and android.plan.session.platform == "android"
    assert ios.plan.session is not None and ios.plan.session.platform == "ios"
    assert android.plan.hash != ios.plan.hash
    with pytest.raises(BuildError, match="platform") as caught:
        builder.build(BuildRequest(spec=spec, cwd=tree))
    assert "['ios', 'android']" in caught.value.message


def test_build_spec_entry_name_and_wrong_spec_combinations(builder: PlanBuilder, tree: Path) -> None:
    built = builder.build(BuildRequest(spec=".harness/flows/kasa.py", entry="iade", cwd=tree))
    assert built.plan.name == "iade"
    assert built.spec == ".harness/flows/kasa.py::iade"
    with pytest.raises(BuildError, match="names entry 'kasa'"):
        builder.build(BuildRequest(spec=".harness/flows/kasa.py::kasa", entry="iade", cwd=tree))
    with pytest.raises(BuildError, match="exactly one of spec and source"):
        builder.build(BuildRequest())
    with pytest.raises(BuildError, match="exactly one of spec and source"):
        builder.build(BuildRequest(spec="a.py", source="import eks"))
    with pytest.raises(BuildError, match="several entries") as caught:
        builder.build(BuildRequest(spec=".harness/flows/kasa.py", cwd=tree))
    assert caught.value.code == "plan_build"


def test_tree_of_finds_the_spec_tree_and_checks_an_explicit_one(tree: Path, tmp_path: Path) -> None:
    assert tree_of(None, tree, tree) is None
    assert tree_of(".harness/flows/kasa.py", None, tree) == tree
    assert tree_of(".harness/flows/kasa.py::kasa", tree, None) == tree
    assert tree_of(str(tree / "web-app/.harness/app.toml"), tree, None) == tree
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(BuildError, match="is not inside the tree"):
        tree_of(str(tree / ".harness/flows/kasa.py"), other, None)
    with pytest.raises(BuildError, match="names no file"):
        tree_of("::kasa", tree, tree)


def test_build_source_plans_an_inline_file_and_deletes_it(builder: PlanBuilder, tree: Path) -> None:
    built = builder.build(BuildRequest(source=INLINE_FLOW, entry="second", tree=tree,
                                       params={"page": "/elsewhere"}))
    assert built.plan.name == "second"
    assert built.spec is None
    assert built.tree == tree
    nav = next(node for node, _ancestors in built.plan.walk() if node.op == "ui.nav")
    assert nav.args["route"] == "/elsewhere"
    assert nav.src is not None
    file, _line = nav.src.rsplit(":", 1)
    folder, name = file.rsplit("/", 1)
    assert folder == ".harness/flows"
    assert INLINE_NAME.match(name)
    assert not (tree / file).exists()
    assert inline_files(tree) == []
    assert not [issue for issue in built.issues if issue.severity == "error"]


def test_build_source_in_a_missing_folder_leaves_no_folder_behind(builder: PlanBuilder, tree: Path) -> None:
    folder = tree / ".harness" / "scores"
    assert not folder.exists()
    source = """
import eks
from eks import t


@eks.score(bpm=120, fps=30, duration=t("4s"))
def tiny(sc: eks.Score):
    return None
"""
    built = builder.build(BuildRequest(source=source, folder="scores", tree=tree))
    assert built.plan.kind == "score"
    assert not folder.exists()
    assert inline_files(tree) == []


def test_build_source_deletes_the_file_when_planning_fails(builder: PlanBuilder, tree: Path) -> None:
    source = INLINE_FLOW + """

@eks.flow(app="web-app")
def failing(s: eks.Session):
    s.ui.nav("/x")
    1 / 0
"""
    with pytest.raises(BuildError) as caught:
        builder.build(BuildRequest(source=source, entry="failing", tree=tree))
    assert caught.value.message == "ZeroDivisionError: division by zero"
    assert caught.value.where is not None
    assert re.match(rf"^\.harness/flows/{INLINE_PREFIX}[0-9a-f]{{16}}\.py:\d+: 1 / 0$", caught.value.where)
    assert inline_files(tree) == []
    with pytest.raises(BuildError, match="several entries"):
        builder.build(BuildRequest(source=INLINE_FLOW, tree=tree))
    assert inline_files(tree) == []


def test_build_source_needs_a_tree(builder: PlanBuilder, tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="inside a tree"):
        builder.build(BuildRequest(source=INLINE_FLOW))
    with pytest.raises(BuildError, match="not a directory"):
        builder.build(BuildRequest(source=INLINE_FLOW, tree=tmp_path / "missing"))


def test_inline_builds_on_one_tree_serialize(builder: PlanBuilder, tree: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    overlaps: list[int] = []
    guard = threading.Lock()
    original = build_module.plan_spec

    def slow_plan_spec(spec: str, **kwargs: Any) -> Plan:
        nonlocal active
        with guard:
            active += 1
            overlaps.append(active)
            present = len(inline_files(tree))
        time.sleep(0.05)
        try:
            assert present == 1
            return original(spec, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(build_module, "plan_spec", slow_plan_spec)
    results: dict[str, Plan] = {}
    errors: list[BaseException] = []

    def run(entry: str) -> None:
        try:
            results[entry] = builder.build(BuildRequest(source=INLINE_FLOW.replace("/first", f"/{entry}"),
                                                        entry="first", tree=tree)).plan
        except (BuildError, AssertionError, OSError, RuntimeError, TypeError, ValueError) as problem:
            errors.append(problem)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("one", "two", "three")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert sorted(results) == ["one", "three", "two"]
    assert max(overlaps) == 1
    assert inline_files(tree) == []


def test_unknown_op_is_a_build_error_with_the_source_line(builder: PlanBuilder, tree: Path) -> None:
    write(tree / ".harness/flows/unknown.py", """
        import eks


        @eks.flow(app="web-app")
        def unknown(s: eks.Session):
            s.ui.nav("/a")
            s.op("nosuch.thing", value=1)
    """)
    with pytest.raises(BuildError) as caught:
        builder.build(BuildRequest(spec=".harness/flows/unknown.py", cwd=tree))
    assert "nosuch.thing" in caught.value.message
    assert caught.value.where == '.harness/flows/unknown.py:7: s.op("nosuch.thing", value=1)'
    assert caught.value.detail == {"where": caught.value.where}


def test_wrong_ref_use_is_a_build_error(builder: PlanBuilder, tree: Path) -> None:
    write(tree / ".harness/flows/refuse.py", """
        import eks


        @eks.flow(app="web-app", seed="standard")
        def refuse(s: eks.Session):
            found = s.api.get("/api/x")
            if found:
                s.ui.nav("/a")
    """)
    with pytest.raises(BuildError) as caught:
        builder.build(BuildRequest(spec=".harness/flows/refuse.py", cwd=tree))
    assert caught.value.where == ".harness/flows/refuse.py:7: if found:"
    assert "eks.when" in caught.value.message


def test_exception_in_the_flow_and_trace_errors_are_build_errors(builder: PlanBuilder, tree: Path) -> None:
    write(tree / ".harness/flows/boom.py", """
        import eks


        @eks.flow(app="web-app")
        def boom(s: eks.Session):
            s.ui.nav("/a")
            {}["missing"]
    """)
    with pytest.raises(BuildError) as caught:
        builder.build(BuildRequest(spec=".harness/flows/boom.py", cwd=tree))
    assert caught.value.message == "KeyError: 'missing'"
    assert caught.value.where == '.harness/flows/boom.py:7: {}["missing"]'
    with pytest.raises(BuildError) as nested:
        builder.build(BuildRequest(spec=".harness/flows/raises.py", cwd=tree))
    assert nested.value.where == ".harness/flows/raises.py:8: with eks.parallel():"
    with pytest.raises(BuildError, match="does not exist"):
        builder.build(BuildRequest(spec=".harness/flows/absent.py", cwd=tree))


def test_validate_returns_validator_and_requirement_issues(builder: PlanBuilder, tree: Path) -> None:
    broken = builder.build(BuildRequest(spec=".harness/flows/broken.py", cwd=tree))
    codes = {issue.code for issue in broken.issues if issue.severity == "error"}
    assert codes
    entry, again = builder.validate(broken.plan, tree=tree)
    assert entry.tree == tree
    assert [issue.code for issue in again] == [issue.code for issue in broken.issues]
    good = builder.build(BuildRequest(spec=".harness/flows/subeler.py", cwd=tree)).plan
    needing = good.model_copy(update={"requires": [Requirement(plugin="ghost", version="1.0.0")]})
    _entry, issues = builder.validate(needing, tree=tree)
    missing = [issue for issue in issues if issue.code == "plugin.missing"]
    assert len(missing) == 1
    assert "ghost" in missing[0].message
    assert missing[0].node is None
    plan_level = [index for index, issue in enumerate(issues) if issue.node is None]
    assert plan_level == list(range(len(plan_level)))


def test_issues_of_converts_a_report_in_order() -> None:
    report = Report(plan_hash="sha256:x", issues=(
        Issue(severity="error", code="plan.schema", message="bad schema"),
        Issue(severity="warning", code="ref.type", message="loose", node="n1", field="a.b", src="f.py:3"),
    ))
    assert issues_of(report) == [
        IssueOut(severity="error", code="plan.schema", message="bad schema"),
        IssueOut(severity="warning", code="ref.type", message="loose", node="n1", field="a.b", src="f.py:3"),
    ]
    with pytest.raises(TypeError):
        issues_of({"issues": []})


@pytest.fixture
def web_tree(tree: Path) -> Path:
    shutil.rmtree(tree / "mobile-app")
    return tree


def test_oneshot_api_get_with_persona_and_seed(builder: PlanBuilder, web_tree: Path) -> None:
    built = builder.oneshot(OneShotRequest(op="api.get", args={"path": "/api/branches"}, persona="harman-admin",
                                           seed="standard", tree=web_tree))
    plan = built.plan
    assert plan.kind == "flow"
    assert plan.name == ONESHOT_NAME
    assert built.spec is None
    assert built.tree == web_tree
    assert ops(plan) == ["session.as", "api.get"]
    assert plan.nodes[0].args == {"persona": "harman-admin"}
    session = plan.session
    assert session is not None
    assert session.key == "main"
    assert session.app == "web-app"
    assert session.platform == "web"
    assert session.persona == "harman-admin"
    assert session.seed == "standard"
    assert session.now == NOW
    assert session.root_seed == 2026
    assert session.backend == "eksenapi"
    assert session.timezone == "Europe/Istanbul"
    assert plan.clock.now == NOW
    assert plan.clock.seed == 2026
    assert plan.outputs == {"value": {"$ref": plan.nodes[1].id, "path": []}}
    assert plan.hash.startswith("sha256:")
    assert not [issue for issue in built.issues if issue.severity == "error"]


def test_oneshot_shorthand_targets_and_explicit_app(builder: PlanBuilder, tree: Path) -> None:
    built = builder.oneshot(OneShotRequest(op="ui.tap", args={"target": "text=Kaydet"}, app="web-app",
                                           tree=tree))
    (tap,) = built.plan.nodes
    assert tap.op == "ui.tap"
    assert tap.args["target"] == {"by": "text", "args": {"text": "Kaydet", "exact": True}}
    assert built.plan.session is not None and built.plan.session.persona is None
    android = builder.oneshot(OneShotRequest(op="ui.tap", args={"target": "id=save"}, app="mobile-app",
                                             platform="android", tree=tree))
    assert android.plan.session is not None and android.plan.session.platform == "android"


def test_oneshot_without_resources_records_the_op_directly(builder: PlanBuilder, tree: Path) -> None:
    built = builder.oneshot(OneShotRequest(op="media.source", args={"uri": "https://example.test/a.png"},
                                           tree=tree))
    (node,) = built.plan.nodes
    assert node.op == "media.source"
    assert built.plan.session is None
    assert built.plan.resources == []
    assert built.plan.outputs == {"value": {"$ref": node.id, "path": []}}
    with pytest.raises(BuildError, match="persona="):
        builder.oneshot(OneShotRequest(op="media.source", args={"uri": "https://example.test/a.png"},
                                       persona="harman-admin", tree=tree))


def test_oneshot_names_what_to_pass(builder: PlanBuilder, tree: Path) -> None:
    with pytest.raises(BuildError, match="pass app=") as several:
        builder.oneshot(OneShotRequest(op="ui.tap", args={"target": "text=Kaydet"}, tree=tree))
    assert "mobile-app" in several.value.message and "web-app" in several.value.message
    with pytest.raises(BuildError, match="pass platform="):
        builder.oneshot(OneShotRequest(op="ui.tap", args={"target": "text=Kaydet"}, app="mobile-app", tree=tree))
    with pytest.raises(BuildError, match="pass app="):
        builder.oneshot(OneShotRequest(op="ui.tap", args={"target": "text=Kaydet"}))
    chosen = builder.oneshot(OneShotRequest(op="ui.tap", args={"target": "text=Kaydet"}, platform="ios",
                                            tree=tree))
    assert chosen.plan.session is not None and chosen.plan.session.app == "mobile-app"
    with pytest.raises(BuildError, match="not one of them"):
        builder.oneshot(OneShotRequest(op="ui.tap", args={"target": "text=Kaydet"}, app="web-app",
                                       platform="ios", tree=tree))
    with pytest.raises(BuildError, match="nosuch.op"):
        builder.oneshot(OneShotRequest(op="nosuch.op", args={}, tree=tree))
    with pytest.raises(BuildError, match="container"):
        builder.oneshot(OneShotRequest(op="capture.record", args={"name": "x"}, app="web-app", tree=tree))


def by_path(files: list[EntryFile]) -> dict[str, EntryFile]:
    return {file.path: file for file in files}


def test_files_lists_entries_with_parameters_and_load_errors(builder: PlanBuilder, tree: Path) -> None:
    write(tree / ".harness/flows/bad.py", """
        import eks


        @eks.flow(app="web-app")
        def bad(s: eks.Session)
            s.ui.nav("/a")
    """)
    files = builder.files(tree)
    found = by_path(files)
    assert list(found) == [file.path for file in files]
    assert ".harness/flows/subeler.py" in found
    assert "mobile-app/.harness/flows/tapper.py" in found
    assert ".harness/seeds/standard.py" in found
    broken = found[".harness/flows/bad.py"]
    assert broken.kind == "flow"
    assert broken.entries == ()
    assert broken.error is not None and "SyntaxError" in broken.error
    (subeler,) = found[".harness/flows/subeler.py"].entries
    assert subeler.kind == "flow"
    assert subeler.name == "subeler"
    assert subeler.file == ".harness/flows/subeler.py"
    assert subeler.qualname == "subeler"
    assert subeler.line is not None
    assert subeler.params == ({"name": "branch", "annotation": "str", "default": "Kadıköy"},
                              {"name": "rows", "annotation": "int", "default": 1})
    assert subeler.tags == ("subeler",)
    assert subeler.app == "web-app"
    assert subeler.platforms == ("web",)
    (tapper,) = found["mobile-app/.harness/flows/tapper.py"].entries
    assert tapper.platforms == ("ios", "android")
    assert tapper.params == ()
    (seed,) = found[".harness/seeds/standard.py"].entries
    assert seed.kind == "seed"
    assert seed.app is None and seed.platforms == ()


def test_files_reports_parameters_without_json_defaults(builder: PlanBuilder, tree: Path) -> None:
    write(tree / ".harness/flows/typed.py", """
        from __future__ import annotations

        import eks


        class Marker:
            def __repr__(self) -> str:
                return "Marker()"


        @eks.flow(app="web-app", description="Typed parameters")
        def typed(s: eks.Session, when: list[int] | None = None, mark: object = Marker(), plain=3):
            s.ui.nav("/a")
    """)
    (typed,) = by_path(builder.files(tree))[".harness/flows/typed.py"].entries
    assert typed.description == "Typed parameters"
    assert typed.params == ({"name": "when", "annotation": "list[int] | None", "default": None},
                            {"name": "mark", "annotation": "object", "defaultText": "Marker()"},
                            {"name": "plain", "annotation": None, "default": 3})


def test_files_filters_by_kind_and_entries_flattens_loaded_files(builder: PlanBuilder, tree: Path) -> None:
    write(tree / ".harness/flows/bad.py", "import eks\n\nraise RuntimeError('module level')\n")
    seeds = builder.files(tree, kind="seed")
    assert [file.path for file in seeds] == [".harness/seeds/standard.py"]
    assert [file.path for file in builder.files(tree, kind="seeds")] == [".harness/seeds/standard.py"]
    broken = by_path(builder.files(tree, kind="flow"))[".harness/flows/bad.py"]
    assert broken.error is not None
    assert broken.error.startswith("RuntimeError: module level\n  at .harness/flows/bad.py:3:")
    entries = builder.entries(tree, kind="flow")
    names = sorted(entry.name for entry in entries)
    assert names == ["broken", "iade", "kasa", "raises", "subeler", "tapper"]
    with pytest.raises(BuildError, match="unknown entry kind"):
        builder.files(tree, kind="nonsense")
    with pytest.raises(BuildError, match="not a directory"):
        builder.files(tree / "missing")


LOCAL_OP_FLOW = """
import eks


@eks.op("acme", name="local", pure=True)
class Local(eks.Args):
    value: int = 1


@eks.flow(app="web-app")
def local(s: eks.Session):
    s.ui.nav("/a")
    s.op("acme.local", value=VALUE)
"""


def inline_module(tree: Path, source: str, folder: str = "flows") -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return module_name(tree / ".harness" / folder / f"{INLINE_PREFIX}{digest}.py")


def test_build_source_keeps_what_the_source_registers(builder: PlanBuilder, tree: Path) -> None:
    source = LOCAL_OP_FLOW.replace("VALUE", "2")
    built = builder.build(BuildRequest(source=source, tree=tree))
    assert not [issue for issue in built.issues if issue.severity == "error"]
    assert ops(built.plan) == ["ui.nav", "acme.local"]
    assert built.registry.registry.has_op("acme.local")
    report = validate_plan(built.plan, registry=built.registry.registry, tree=tree)
    assert not [issue for issue in report.issues if issue.severity == "error"]
    published = builder.registries.for_tree(tree)
    assert not published.registry.has_op("acme.local")
    assert built.registry.tree == published.tree
    assert built.registry.generation == published.generation
    assert built.registry.plugins == published.plugins
    assert built.registry.project_digest is not None
    assert built.registry.project_digest.startswith("sha256:")
    assert built.registry.project_digest != published.project_digest
    again = builder.build(BuildRequest(source=source, tree=tree))
    assert again.registry.project_digest == built.registry.project_digest
    assert again.registry.registry is not built.registry.registry
    other = builder.build(BuildRequest(source=LOCAL_OP_FLOW.replace("VALUE", "3"), tree=tree))
    assert other.registry.project_digest != built.registry.project_digest
    _entry, tree_issues = builder.validate(built.plan, tree=tree)
    assert "op.unknown" in [issue.code for issue in tree_issues]
    assert inline_files(tree) == []


def test_build_source_module_lives_as_long_as_its_registry(builder: PlanBuilder, tree: Path) -> None:
    source = LOCAL_OP_FLOW.replace("VALUE", "4")
    name = inline_module(tree, source)
    built = builder.build(BuildRequest(source=source, tree=tree))
    assert name in sys.modules
    del built
    gc.collect()
    assert name not in sys.modules
    failing = INLINE_FLOW + "\n\nraise RuntimeError('late')\n"
    with pytest.raises(BuildError):
        builder.build(BuildRequest(source=failing, entry="first", tree=tree))
    assert inline_module(tree, failing) not in sys.modules


def test_build_source_edits_a_flow_that_is_already_loaded(builder: PlanBuilder, tree: Path) -> None:
    original = builder.build(BuildRequest(spec=".harness/flows/kasa.py::kasa", cwd=tree))
    assert by_path(builder.files(tree))[".harness/flows/kasa.py"].error is None
    edited = (tree / ".harness/flows/kasa.py").read_text(encoding="utf-8").replace("/tenant/cash", "/tenant/kasa2")
    inline = builder.build(BuildRequest(source=edited, entry="kasa", tree=tree))
    routes = [node.args.get("route") for node, _ancestors in inline.plan.walk() if node.op == "ui.nav"]
    assert routes == ["/tenant/kasa2"]
    published = builder.registries.for_tree(tree)
    kasa = published.registry.entry("flow", "kasa")
    assert kasa.file is not None and Path(kasa.file).name == "kasa.py"
    again = builder.build(BuildRequest(spec=".harness/flows/kasa.py::kasa", cwd=tree))
    assert again.plan.hash == original.plan.hash
    seed = (tree / ".harness/seeds/standard.py").read_text(encoding="utf-8").replace("Harman Kuyumculuk",
                                                                                    "Harman Kuyum")
    seeded = builder.build(BuildRequest(source=seed, folder="seeds", tree=tree))
    assert seeded.plan.kind == "seed"
    assert seeded.plan.name == "standard"
    assert published.registry.entry("seed", "standard").file == seed_file(tree)
    assert inline_files(tree) == []


def seed_file(tree: Path) -> str:
    return str(tree / ".harness/seeds/standard.py")


def test_inline_build_hides_its_file_from_other_tree_loads(builder: PlanBuilder, tree: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    before = builder.registries.for_tree(tree)
    started = threading.Event()
    release = threading.Event()
    original = build_module.plan_spec

    def waiting_plan_spec(spec: str, **kwargs: Any) -> Plan:
        started.set()
        assert release.wait(10)
        return original(spec, **kwargs)

    monkeypatch.setattr(build_module, "plan_spec", waiting_plan_spec)
    seen: dict[str, TreeRegistry] = {}
    errors: list[BaseException] = []

    def inline() -> None:
        try:
            builder.build(BuildRequest(source=INLINE_FLOW, entry="first", tree=tree))
        except (BuildError, AssertionError, OSError, RuntimeError) as problem:
            errors.append(problem)

    def other() -> None:
        seen["entry"] = builder.registries.for_tree(tree)

    building = threading.Thread(target=inline)
    building.start()
    assert started.wait(10)
    assert len(inline_files(tree)) == 1
    loading = threading.Thread(target=other)
    loading.start()
    loading.join(0.3)
    assert loading.is_alive()
    release.set()
    building.join(30)
    loading.join(30)
    assert errors == []
    after = seen["entry"]
    assert after.project_digest == before.project_digest
    assert after.generation == before.generation
    assert after.registry is before.registry
    assert inline_files(tree) == []


def test_inline_build_deletes_files_a_stopped_hub_left_behind(builder: PlanBuilder, tree: Path) -> None:
    stale_flow = write(tree / f".harness/flows/{INLINE_PREFIX}0123456789abcdef.py", "raise SystemExit(1)\n")
    stale_seed = write(tree / f".harness/seeds/{INLINE_PREFIX}fedcba9876543210.py", "import eks\n")
    helper = write(tree / ".harness/flows/_helper.py", "VALUE = 1\n")
    other = write(tree / f".harness/flows/{INLINE_PREFIX}notes.py", "VALUE = 2\n")
    built = builder.build(BuildRequest(source=INLINE_FLOW, entry="first", tree=tree))
    assert built.plan.name == "first"
    assert not stale_flow.exists()
    assert not stale_seed.exists()
    assert helper.exists()
    assert other.exists()


QUITTING_FLOW = """
    import sys

    import eks


    @eks.flow(app="web-app")
    def quits(s: eks.Session):
        s.ui.nav("/a")
        sys.exit(3)
"""


def test_exits_in_entry_code_are_build_errors(builder: PlanBuilder, tree: Path) -> None:
    write(tree / ".harness/flows/quits.py", QUITTING_FLOW)
    with pytest.raises(BuildError) as caught:
        builder.build(BuildRequest(spec=".harness/flows/quits.py", cwd=tree))
    assert caught.value.message.startswith("SystemExit: 3 raised while loading or planning")
    assert caught.value.where == ".harness/flows/quits.py:9: sys.exit(3)"
    with pytest.raises(BuildError, match="SystemExit: 3 raised") as inline:
        builder.build(BuildRequest(source=textwrap.dedent(QUITTING_FLOW), tree=tree))
    assert inline.value.where is not None and inline.value.where.endswith(": sys.exit(3)")
    assert inline_files(tree) == []
    write(tree / ".harness/flows/interrupts.py", """
        import eks


        @eks.flow(app="web-app")
        def interrupts(s: eks.Session):
            raise KeyboardInterrupt
    """)
    with pytest.raises(BuildError, match="KeyboardInterrupt raised"):
        builder.build(BuildRequest(spec=".harness/flows/interrupts.py", cwd=tree))
    write(tree / ".harness/flows/leaves.py", "import sys\n\nsys.exit(3)\n")
    with pytest.raises(BuildError, match="SystemExit: 3 raised"):
        builder.build(BuildRequest(spec=".harness/flows/leaves.py", cwd=tree))
    found = by_path(builder.files(tree, kind="flow"))
    leaves = found[".harness/flows/leaves.py"]
    assert leaves.entries == ()
    assert leaves.error is not None and leaves.error.startswith("SystemExit: 3 raised while loading or planning")
    assert found[".harness/flows/quits.py"].error is None
    assert found[".harness/flows/subeler.py"].entries


BROKEN_SEED = ".harness/seeds/standard.py"


def break_file(tree: Path, relative: str) -> str:
    path = tree / relative
    text = path.read_text(encoding="utf-8").rstrip("\n") + "\n\nboom = 1 / 0\n"
    path.write_text(text, encoding="utf-8")
    return f"{relative}:{len(text.splitlines())}: boom = 1 / 0"


def test_files_puts_a_tree_load_error_on_the_failing_file(builder: PlanBuilder, tree: Path) -> None:
    where = break_file(tree, BROKEN_SEED)
    files = builder.files(tree)
    found = by_path(files)
    assert ".harness/flows/subeler.py" in found
    assert all(file.entries == () for file in files)
    seed = found[BROKEN_SEED]
    assert seed.kind == "seed"
    assert seed.error == f"ZeroDivisionError: division by zero\n  at {where}"
    others = [file for file in files if file.path != BROKEN_SEED]
    assert others
    for file in others:
        assert file.error is not None
        assert file.error.startswith("not loaded:")
        assert BROKEN_SEED in file.error
    assert builder.entries(tree) == []
    flows = builder.files(tree, kind="flow")
    assert flows and {file.kind for file in flows} == {"flow"}
    assert all(file.error is not None and BROKEN_SEED in file.error for file in flows)


def test_files_raises_a_tree_load_error_outside_every_entry_file(builder: PlanBuilder, tree: Path) -> None:
    plugin = ".harness/plugins/fixtures/fixture_backends.py"
    where = break_file(tree, plugin)
    with pytest.raises(BuildError) as caught:
        builder.files(tree)
    assert "plugin 'fixtures'" in caught.value.message
    assert caught.value.message.endswith("ZeroDivisionError: division by zero")
    assert caught.value.where == where


def test_a_broken_tree_is_a_build_error_everywhere_it_loads(builder: PlanBuilder, web_tree: Path) -> None:
    good = builder.build(BuildRequest(spec=".harness/flows/subeler.py", cwd=web_tree)).plan
    where = break_file(web_tree, BROKEN_SEED)
    with pytest.raises(BuildError) as validating:
        builder.validate(good, tree=web_tree)
    assert validating.value.message == "ZeroDivisionError: division by zero"
    assert validating.value.where == where
    with pytest.raises(BuildError) as doing:
        builder.oneshot(OneShotRequest(op="api.get", args={"path": "/api/branches"}, app="web-app",
                                       tree=web_tree))
    assert doing.value.message == "ZeroDivisionError: division by zero"
    assert doing.value.where == where
    with pytest.raises(BuildError) as inline:
        builder.build(BuildRequest(source=INLINE_FLOW, entry="first", tree=web_tree))
    assert inline.value.where == where
    assert inline_files(web_tree) == []
    with pytest.raises(BuildError) as spec:
        builder.build(BuildRequest(spec=".harness/flows/subeler.py", cwd=web_tree))
    assert spec.value.where == where


def test_oneshot_locates_errors_raised_by_tree_code(builder: PlanBuilder, web_tree: Path) -> None:
    plugin = web_tree / ".harness/plugins/fixtures/fixture_backends.py"
    plugin.write_text(plugin.read_text(encoding="utf-8") + textwrap.dedent("""


        import pydantic


        @eks.op("fixture", out="json", description="Look a page up")
        class Lookup(eks.Args):
            path: str = "/"

            @pydantic.field_validator("path")
            @classmethod
            def known(cls, value: str) -> str:
                raise LookupError(f"no page {value}")
    """), encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        builder.oneshot(OneShotRequest(op="fixture.lookup", args={"path": "/x"}, tree=web_tree))
    assert caught.value.message == "LookupError: no page /x"
    assert caught.value.where is not None
    assert caught.value.where.startswith(".harness/plugins/fixtures/fixture_backends.py:")
    assert caught.value.where.endswith(': raise LookupError(f"no page {value}")')
