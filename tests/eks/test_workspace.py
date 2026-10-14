from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import eks
from eks.discovery import DiscoveryError, tree_registry
from eks.planning import PlanBuildError
from eks.workspace import plan_spec, seed_personas
from eks_harness.ir.registry import Registry, RegistryError

FLOW = """
    import eks
    from eks import by, t


    @eks.flow(app="web-app", seed="standard", as_="harman-admin")
    def x(s: eks.Session, count: int = 1, label: str = "liste", wait: eks.Time = t("1s"), fast: bool = False):
        s.record_op("ui.nav", {"route": f"/rows/{label}"})
        s.record_op("ui.wait_for", {"target": by.test_id("row"), "timeout": wait})
        rows = s.record_op("ui.count", {"target": by.test_id("row")})
        return {"rows": rows}
"""

MOBILE_FLOW = """
    import eks
    from eks import by


    @eks.flow(app="mobile-app")
    def tapper(s: eks.Session):
        s.record_op("ui.tap", {"target": by.test_id("save")})
"""

SEED = """
    import eks


    @eks.resource("member", persona=True)
    class Member(eks.Resource):
        role: str = "admin"


    @eks.seed("standard", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026)
    def standard(d: eks.Data):
        d.member("harman-admin")
        d.member("host", role="host")
"""

SCENE = """
    import eks


    @eks.scene(kind="gfx", fps=30, size=(1080, 1920))
    def phone(sc: eks.GfxScene, screen: eks.Media):
        return None
"""

PLUGIN_MANIFEST = """
    [plugin]
    id = "acme"
    name = "Acme"
    version = "1.2.0"
    api = "1"
    modules = ["acme_ops"]
"""

PLUGIN_OPS = """
    import eks


    @eks.op("acme", out="json", resources=("lease",), description="Ping the acme widget")
    class Ping(eks.Args):
        target: str = "self"
"""

PLUGIN_FLOW = """
    import eks


    @eks.flow(app="web-app")
    def pinger(s: eks.Session):
        s.op("acme.ping")
"""


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def make_tree(root: Path, *, trust: str = "") -> Path:
    write(root / ".harness" / "project.toml", f'[project]\nid = "acme/shop"\n{trust}')
    zone = 'timezone = "Europe/Istanbul"\nlocale = "tr-TR"\n'
    write(root / "web-app" / ".harness" / "app.toml", f'[app]\nname = "web-app"\nplatforms = ["web"]\n\n[web]\n{zone}')
    write(root / "mobile-app" / ".harness" / "app.toml",
          f'[app]\nname = "mobile-app"\nplatforms = ["ios", "android"]\n\n[mobile]\n{zone}')
    write(root / ".harness" / "flows" / "x.py", FLOW)
    write(root / "mobile-app" / ".harness" / "flows" / "tapper.py", MOBILE_FLOW)
    write(root / ".harness" / "seeds" / "standard.py", SEED)
    write(root / ".harness" / "scenes" / "phone.py", SCENE)
    return root


def test_platform_comes_from_the_app_profile(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    plan = plan_spec("flows/x.py", cwd=tree / ".harness")
    assert plan.kind == "flow"
    assert plan.name == "x"
    assert plan.session is not None
    assert plan.session.platform == "web"
    assert plan.source is not None
    assert plan.source.file == ".harness/flows/x.py"
    assert [node.op for node in plan.nodes] == ["session.as", "ui.nav", "ui.wait_for", "ui.count"]
    report = eks.validate(plan, registry=tree_registry(tree))
    assert report.ok, report.text()
    assert report.issues == ()


def test_set_parameters_are_coerced_from_text(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    plan = plan_spec(str(tree / ".harness" / "flows" / "x.py::x"),
                     params={"count": "3", "label": "fatura", "wait": "2s", "fast": "yes"})
    assert plan.params == {"count": 3, "label": "fatura", "wait": "2s", "fast": True}
    nav = plan.nodes[1]
    assert nav.args["route"] == "/rows/fatura"
    assert plan.nodes[2].args["timeout"] == "2s"


def test_planning_twice_is_identical(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    first = plan_spec("flows/x.py", cwd=tree / ".harness", params={"count": "2"})
    second = plan_spec("flows/x.py", cwd=tree / ".harness", params={"count": "2"})
    assert first.to_json() == second.to_json()
    assert eks.validate(first).as_dict() == eks.validate(second).as_dict()


def test_app_with_several_platforms_needs_a_platform(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    with pytest.raises(PlanBuildError, match=r"\['ios', 'android'\]"):
        plan_spec("mobile-app/.harness/flows/tapper.py", cwd=tree)
    plan = plan_spec("mobile-app/.harness/flows/tapper.py", cwd=tree, platform="android")
    assert plan.session is not None
    assert plan.session.platform == "android"
    with pytest.raises(PlanBuildError, match="'web' is not one of them"):
        plan_spec("mobile-app/.harness/flows/tapper.py", cwd=tree, platform="web")


def test_scene_with_a_media_parameter(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    plan = plan_spec(".harness/scenes/phone.py", cwd=tree, params={"screen": "artifact:S1"})
    assert plan.kind == "scene"
    assert plan.params == {"screen": "artifact:S1"}
    source = plan.nodes[0]
    assert source.op == "media.source"
    assert source.args == {"uri": "artifact:S1"}
    assert [node.op for node in plan.nodes][-1] == "gfx.render"
    report = eks.validate(plan)
    assert report.ok, report.text()


def test_seed_personas(tmp_path: Path, eks_registry: Registry) -> None:
    tree = make_tree(tmp_path / "shop")
    assert seed_personas("standard") is None
    plan_spec("flows/x.py", cwd=tree / ".harness")
    registry = tree_registry(tree)
    assert seed_personas("standard", registry=registry) == ["harman-admin", "host"]
    assert seed_personas("standard") is None
    assert seed_personas("standard", registry=eks_registry) is None
    assert seed_personas("nothing", registry=registry) is None
    entry = registry.entry("seed", "standard")
    assert entry.file is not None


def test_seed_personas_follow_the_registry(eks_registry: Registry) -> None:
    @eks.resource("member", persona=True)
    class Member(eks.Resource):
        role: str = "admin"

    before = eks_registry.snapshot()

    @eks.seed("pilot", backend="eksenapi")
    def pilot(d: eks.Data) -> None:
        d.member("ayse")

    assert seed_personas("pilot") == ["ayse"]
    with_pilot = eks_registry.snapshot()
    eks_registry.restore(before)
    assert seed_personas("pilot") is None

    @eks.seed("pilot", backend="eksenapi")
    def pilot_again(d: eks.Data) -> None:
        d.member("mehmet")

    # a different seed under the same name is planned again instead of served from the cache
    assert seed_personas("pilot") == ["mehmet"]
    eks_registry.restore(with_pilot)
    assert seed_personas("pilot") == ["ayse"]
    assert eks_registry.entry("seed", "pilot").fn is pilot
    assert pilot_again is not pilot
    assert eks_registry.resource("member").model is Member


def test_flow_persona_is_checked_against_the_tree_seed(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    flow = write(tree / ".harness" / "flows" / "ghost.py", FLOW.replace("harman-admin", "ghost").replace(
        "def x(", "def ghost("))
    plan = plan_spec(str(flow))
    report = eks.validate(plan, registry=tree_registry(tree))
    assert [issue.code for issue in report.issues] == ["persona.unknown"]
    assert "'harman-admin', 'host'" in report.issues[0].message


def test_plan_spec_errors_propagate(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    with pytest.raises(DiscoveryError):
        plan_spec("flows/missing.py", cwd=tree / ".harness")
    with pytest.raises(DiscoveryError, match="no entry named 'y'"):
        plan_spec("flows/x.py::y", cwd=tree / ".harness")
    with pytest.raises(PlanBuildError, match="nope"):
        plan_spec("flows/x.py", cwd=tree / ".harness", params={"nope": "1"})
    with pytest.raises(PlanBuildError):
        plan_spec("flows/x.py", cwd=tree / ".harness", params={"count": "many"})
    with pytest.raises(PlanBuildError, match="flows only"):
        plan_spec(".harness/scenes/phone.py", cwd=tree, params={"screen": "artifact:S1"}, platform="web")
    broken = write(tree / ".harness" / "flows" / "broken.py", "import eks\nraise RuntimeError('boom')\n")
    with pytest.raises(RuntimeError, match="boom"):
        plan_spec(str(broken))


def test_plugins_are_loaded_unless_disabled(tmp_path: Path, eks_registry: Registry) -> None:
    tree = make_tree(tmp_path / "shop", trust='[plugins]\ntrust = ["acme"]\n')
    plugin = tree / ".harness" / "plugins" / "acme"
    write(plugin / "harness-plugin.toml", PLUGIN_MANIFEST)
    write(plugin / "acme_ops.py", PLUGIN_OPS)
    flow = write(tree / ".harness" / "flows" / "pinger.py", PLUGIN_FLOW)
    with pytest.raises(RegistryError, match=r"acme\.ping"):
        plan_spec(str(flow), plugins=False)
    plan = plan_spec(str(flow))
    assert [node.op for node in plan.nodes] == ["acme.ping"]
    assert [(item.plugin, item.version) for item in plan.requires] == [("acme", "1.2.0")]
    report = eks.validate(plan, tree=tree)
    assert report.ok, report.text()
    assert tree_registry(tree).versions()["acme"] == "1.2.0"
    assert "acme" not in tree_registry(tree, plugins=False).versions()
    assert "acme" not in eks_registry.versions()


def test_seed_personas_include_extended_seeds(eks_registry: Registry) -> None:
    @eks.resource("member", persona=True)
    class Member(eks.Resource):
        role: str = "admin"

    @eks.seed("basis", backend="eksenapi")
    def basis(d: eks.Data) -> None:
        d.member("host")
        d.member("ayse")

    @eks.seed("by-option", backend="eksenapi", base="basis")
    def by_option(d: eks.Data) -> None:
        d.member("mehmet")

    @eks.seed("by-call", backend="eksenapi")
    def by_call(d: eks.Data) -> None:
        d.extend("basis")
        d.member("zeynep")

    @eks.seed("chained", backend="eksenapi", base="by-call")
    def chained(d: eks.Data) -> None:
        d.extend("by-option")
        d.member("ali")

    @eks.seed("orphan", backend="eksenapi", base="nowhere")
    def orphan(d: eks.Data) -> None:
        d.member("veli")

    # base personas come first; a persona reached through two bases keeps its first position
    assert seed_personas("by-option") == ["host", "ayse", "mehmet"]
    assert seed_personas("by-call") == ["host", "ayse", "zeynep"]
    assert seed_personas("chained") == ["host", "ayse", "zeynep", "mehmet", "ali"]
    # an unregistered base adds nothing
    assert seed_personas("orphan") == ["veli"]
    assert [eks_registry.entry("seed", name).fn for name in ("basis", "by-option", "by-call", "chained", "orphan")] == [
        basis, by_option, by_call, chained, orphan]
    assert eks_registry.resource("member").model is Member


def test_seed_personas_survive_an_extend_cycle(eks_registry: Registry) -> None:
    @eks.resource("member", persona=True)
    class Member(eks.Resource):
        role: str = "admin"

    @eks.seed("ping", backend="eksenapi", base="pong")
    def ping(d: eks.Data) -> None:
        d.member("ayse")

    @eks.seed("pong", backend="eksenapi")
    def pong(d: eks.Data) -> None:
        d.extend("ping")
        d.member("mehmet")

    assert seed_personas("ping") == ["mehmet", "ayse"]
    assert seed_personas("pong") == ["ayse", "mehmet"]
    assert eks_registry.entry("seed", "ping").fn is ping
    assert eks_registry.entry("seed", "pong").fn is pong
    assert eks_registry.resource("member").model is Member


def test_seed_personas_follow_a_changed_base(eks_registry: Registry) -> None:
    @eks.resource("member", persona=True)
    class Member(eks.Resource):
        role: str = "admin"

    before = eks_registry.snapshot()

    @eks.seed("basis", backend="eksenapi")
    def basis(d: eks.Data) -> None:
        d.member("host")

    @eks.seed("child", backend="eksenapi", base="basis")
    def child(d: eks.Data) -> None:
        d.member("ayse")

    assert seed_personas("child") == ["host", "ayse"]
    child_entry = eks_registry.entry("seed", "child")
    eks_registry.restore(before)

    @eks.seed("basis", backend="eksenapi")
    def basis_again(d: eks.Data) -> None:
        d.member("operator")

    # the child seed itself is unchanged (same entry), only its base differs
    eks_registry.register_entry(child_entry)
    assert seed_personas("child") == ["operator", "ayse"]
    assert basis_again is not basis
    assert eks_registry.entry("seed", "child").fn is child
    assert eks_registry.resource("member").model is Member
