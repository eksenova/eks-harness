from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import eks
from eks.discovery import resolve_entry, tree_registry
from eks.planning import seed_plan
from eks.workspace import plan_spec, seed_personas
from eks_harness.engine.validator import Report
from eks_harness.ir.hashing import cache_inputs, cacheable, hash_plan
from eks_harness.ir.plan import Node, Plan, Requirement
from eks_harness.ir.registry import Registry, using_registry
from eks_harness.ir.schema import plan_schema

# examples/dsl is a runnable workspace: every code example of docs/dsl/design.md (sections 4 to 12) lives
# there as an entry file, and `eks-harness plan examples/dsl/<file>` builds it like these tests do.
ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = ROOT / "examples" / "dsl"


@dataclass(frozen=True)
class Example:
    file: str
    kind: str
    section: str
    params: dict[str, str] = field(default_factory=dict)
    platform: str | None = None


EXAMPLES: dict[str, Example] = {
    "subeler": Example(".harness/flows/subeler.py", "flow", "4.1 entry points"),
    "refs": Example(".harness/flows/refs.py", "flow", "4.2 refs"),
    "control": Example(".harness/flows/control.py", "flow", "4.3 control flow"),
    "timing": Example(".harness/edits/timing.py", "edit", "4.4 time"),
    "targets": Example(".harness/flows/targets.py", "flow", "4.5 targets (web)"),
    "targets_mobile": Example(".harness/flows/targets_mobile.py", "flow", "4.5 targets (React Native)",
                              platform="ios"),
    "surface_web": Example(".harness/flows/surface_web.py", "flow", "4.6 session surface (web)"),
    "surface_mobile": Example(".harness/flows/surface_mobile.py", "flow", "4.6 session surface (mobile)",
                              platform="ios"),
    "standard": Example(".harness/seeds/standard.py", "seed", "7 data and seeds"),
    "subeler_seed": Example(".harness/seeds/feature.py", "seed", "7 d.extend composition"),
    "network": Example(".harness/flows/network.py", "flow", "8 network and injection"),
    "debugging": Example(".harness/flows/debugging.py", "flow", "9 debugging"),
    "overlays": Example(".harness/flows/overlays.py", "flow", "10.1 overlays (live and screenshot)"),
    "annotated": Example(".harness/edits/annotated.py", "edit", "10.1 overlays (video)"),
    "demo": Example(".harness/edits/demo.py", "edit", "10.2 edits"),
    "mix": Example(".harness/edits/mix.py", "edit", "10.3 audio"),
    "lower_third": Example(".harness/scenes/lower_third.py", "scene", "10.3 gfx"),
    "titles": Example(".harness/scenes/titles.py", "scene", "10.3 gfx (used by 10.5)"),
    "phone": Example(".harness/scenes/phone.py", "scene", "10.4 scenes", {"screen": "artifact:SCREEN1"}),
    "ad": Example(".harness/scores/ad.py", "score", "10.5 scores and takes"),
    "extending": Example(".harness/flows/extending.py", "flow", "11 extensibility (flow)"),
    "extending_edit": Example(".harness/edits/extending.py", "edit", "11 extensibility (edit)"),
}
ENTRY_NAMES = {"subeler_seed": "subeler", "extending_edit": "extending"}

SUPPORT_FILES = [
    ".harness/plugins/cookbook/cookbook_extensions.py",
    ".harness/plugins/cookbook/cookbook_resources.py",
    ".harness/plugins/cookbook/harness-plugin.toml",
    ".harness/project.toml",
    "mobile-app/.harness/app.toml",
    "web-app/.harness/app.toml",
]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    # a copy, so plugin imports never write caches into the repository
    root = tmp_path / "dsl"
    shutil.copytree(EXAMPLES_DIR, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def build(tree: Path, name: str) -> Plan:
    example = EXAMPLES[name]
    return plan_spec(example.file, params=example.params, platform=example.platform, cwd=tree, plugins=True)


def clean(report: Report) -> Report:
    assert report.ok, report.text()
    assert report.issues == (), report.text()
    return report


def ops_of(plan: Plan) -> list[str]:
    return [node.op for node, _ancestors in plan.walk()]


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def args_of(node: Node) -> Any:
    return node.args


def json_of(plan: Plan) -> Any:
    return json.loads(plan.to_json())


def strings_in(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(strings_in(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(strings_in(item) for item in value)) if value else set()
    return set()


def targets_in(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        found = [value] if "by" in value and "args" in value else []
        return found + [target for item in value.values() for target in targets_in(item)]
    if isinstance(value, list):
        return [target for item in value for target in targets_in(item)]
    return []


def test_every_design_example_has_a_file_and_nothing_else_is_there() -> None:
    files = sorted(path.relative_to(EXAMPLES_DIR).as_posix() for path in EXAMPLES_DIR.rglob("*")
                   if path.is_file() and "__pycache__" not in path.parts)
    assert files == sorted([example.file for example in EXAMPLES.values()] + SUPPORT_FILES)


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_example_builds_and_validates(name: str, tree: Path, eks_registry: Registry) -> None:
    example = EXAMPLES[name]
    plan = build(tree, name)
    clean(eks.validate(plan, tree=tree))
    assert plan.kind == example.kind
    assert plan.name == ENTRY_NAMES.get(name, name)
    assert plan.hash is not None
    assert plan.nodes


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_eks_plan_builds_the_same_plan(name: str, tree: Path, eks_registry: Registry) -> None:
    # eks.plan is the in-process entry point: it finds the entry's tree like the CLI does, so Plan.base,
    # node ids, the app profile defaults and the plan hash (a seed's snapshot key) are the same
    example = EXAMPLES[name]
    through_cli = build(tree, name)
    entry = resolve_entry(example.file, cwd=tree)
    params: dict[str, Any] = dict(example.params)
    if example.platform is not None:
        params["platform"] = example.platform
    direct = eks.plan(entry, **params)
    clean(eks.validate(direct, tree=tree))
    assert direct.base == through_cli.base
    assert direct.hash == through_cli.hash
    assert direct.to_json() == through_cli.to_json()
    if example.kind == "seed":
        assert seed_plan(entry).hash == through_cli.hash


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_example_hash_is_stable(name: str, tree: Path, eks_registry: Registry) -> None:
    first = build(tree, name)
    second = build(tree, name)
    assert first.hash == second.hash
    assert first.to_json() == second.to_json()
    with using_registry(tree_registry(tree)):
        assert hash_plan(first).hash == first.hash


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_example_round_trips_through_json(name: str, tree: Path, eks_registry: Registry) -> None:
    plan = build(tree, name)
    text = plan.to_json()
    decoded = Plan.from_json(text)
    assert decoded == plan
    assert decoded.to_json() == text
    with using_registry(tree_registry(tree)):
        assert hash_plan(decoded).hash == plan.hash
    assert Plan.from_json(plan.to_json(indent=None)) == plan
    clean(eks.validate(decoded, tree=tree))
    jsonschema.Draft202012Validator(plan_schema(tree_registry(tree))).validate(json.loads(text))


def test_refs_build_path_expressions(tree: Path, eks_registry: Registry) -> None:
    plan = build(tree, "refs")
    (get,) = nodes_of(plan, "api.get")
    outputs = json.loads(json.dumps(plan.outputs))
    assert outputs["drafts"] == {"$ref": get.id, "path": ["items", {"filter": {"state": "Draft"}}]}
    assert outputs["kadikoy"] == {"$ref": get.id, "path": ["items", {"find": {"branch": "Kadıköy"}}, "id"]}
    assert outputs["first_three"] == {"$ref": get.id, "path": ["items", {"slice": [None, 3, None]}]}
    nav = nodes_of(plan, "ui.nav")[0]
    assert args_of(nav)["route"] == {"$fmt": ["/tenant/invoice/", {"$ref": get.id, "path": ["items", 0, "id"]}]}
    length = [node for node in nodes_of(plan, "expect.value") if args_of(node)["assertion"] == {"kind": "ge", "value": 0}]
    assert [args_of(node)["value"] for node in length] == [
        {"$ref": get.id, "path": ["items", {"filter": {"state": "Draft"}}, {"len": True}]}]


def test_time_example_uses_every_form_of_the_grammar(tree: Path, eks_registry: Registry) -> None:
    plan = build(tree, "timing")
    times = strings_in(json_of(plan)["nodes"])
    # canonical spellings: t("1.5s") + t("300ms") is 1.8s, t.start and t.end are start and end
    for written in ("start", "end", "0s", "15s", "1.5s", "1.8s", "f:120", "f:240", "beat:8+1b", "bar:4-1b",
                    "cue:logo", "cue:logo-2f", "word:'EksenGOLD'", "marker:intro#2", "end-2s"):
        assert written in times, written
    assert plan.clock.cues == {"logo": eks.t("bar:4")}
    (render,) = nodes_of(plan, "edit.render")
    flash = args_of(render)["timeline"]["tracks"][0]["clips"][2]["effects"][0]
    assert flash["kind"] == "flash"
    assert flash["at"] == {"stream": "downbeat", "start": 0, "stop": 8, "step": 1, "times": [], "offset": "0s"}
    streams = {source["name"] for node in nodes_of(plan, "media.markers")
               for source in args_of(node)["sources"]}
    assert streams == {"song", "vo", "intro"}
    (cut,) = nodes_of(plan, "check.media")
    assert args_of(cut)["at"] == "marker:intro#2"


def test_target_examples_cover_every_predicate(tree: Path, eks_registry: Registry) -> None:
    web = targets_in(json_of(build(tree, "targets"))["nodes"])
    mobile = targets_in(json_of(build(tree, "targets_mobile"))["nodes"])
    assert {target["by"] for target in web} == {
        "test_id", "role", "text", "label", "placeholder", "css", "point"}
    assert "kind" in {target["by"] for target in mobile}
    taps = [args_of(node)["target"] for node in nodes_of(build(tree, "targets"), "ui.tap")]
    # the shorthand strings of design 4.5 mean the same targets as the by. calls next to them
    assert taps[0] == taps[1] == {"by": "test_id", "args": {"id": "credit-add-submit"}}
    assert taps[2] == taps[3]
    assert taps[4] == taps[6] == {"by": "text", "args": {"text": "Kadıköy", "exact": True}}
    assert taps[5] == {"by": "text", "args": {"text": "Kadı", "exact": False}}
    scoped = next(target for target in web if target.get("within") is not None)
    assert scoped["nth"] == 2
    assert scoped["within"] == {"by": "role", "args": {"role": "dialog", "exact": False}}
    assert any(target.get("near") == {"by": "text", "args": {"text": "Şablon ile", "exact": True}} for target in web)


def test_web_flows_reject_react_native_predicates(tree: Path, eks_registry: Registry) -> None:
    flow = tree / ".harness" / "flows" / "kind_on_web.py"
    flow.write_text(
        "import eks\nfrom eks import by\n\n\n@eks.flow(app='web-app')\ndef kind_on_web(s: eks.Session):\n"
        "    s.ui.tap(by.kind('button'))\n", encoding="utf-8")
    plan = plan_spec(".harness/flows/kind_on_web.py", cwd=tree, plugins=True)
    report = eks.validate(plan)
    assert not report.ok
    assert any(issue.code.startswith("target.") for issue in report.issues), report.text()


SURFACE_OPS = {
    "navigation": {"ui.nav", "ui.back", "ui.reload", "ui.open_url", "ui.deeplink"},
    "input": {"ui.tap", "ui.double_tap", "ui.long_press", "ui.hover", "ui.fill", "ui.type", "ui.clear", "ui.key",
              "ui.select", "ui.check", "ui.choose", "ui.scroll", "ui.swipe", "ui.drag"},
    "reads": {"ui.text", "ui.value", "ui.attr", "ui.count", "ui.exists", "ui.visible", "ui.tree", "ui.url",
              "ui.route", "ui.state", "ui.layout"},
    "waits": {"ui.wait_for", "ui.wait_gone", "ui.wait_url", "ui.settle"},
    "assertions": {"expect.target", "expect.value"},
    "evidence": {"check.checkpoint", "capture.shot", "capture.dump", "capture.har"},
    "recording": {"capture.record", "capture.hold"},
    "overlays": {"overlay.show", "overlay.remove", "overlay.clear", "overlay.annotate"},
    "backend": {"api.get", "api.post", "api.put", "api.delete", "session.as", "backend.info"},
    "fakes": {"fake.arm", "fake.clear"},
    "network": {"net.route", "net.shape", "net.cassette"},
    "injection": {"inject.script", "inject.style", "mobile.patch", "mobile.unpatch", "mobile.mount", "inject.eval"},
    "store": {"mobile.dispatch"},
    "debug": {"debug.break", "debug.pause_on_error"},
}


def test_the_session_surface_table_is_covered(tree: Path, eks_registry: Registry) -> None:
    used: set[str] = set()
    for name in ("surface_web", "surface_mobile", "overlays", "network", "debugging"):
        used.update(ops_of(build(tree, name)))
    missing = {group: sorted(ops - used) for group, ops in SURFACE_OPS.items() if ops - used}
    assert missing == {}
    overlay_kinds = {args_of(node)["overlay"]["kind"] for node in nodes_of(build(tree, "overlays"), "overlay.show")}
    assert overlay_kinds == {"title", "caption", "callout", "spotlight", "highlight", "arrow", "blur", "redact",
                             "icon", "image", "step"}


def test_expectations_and_soft_options(tree: Path, eks_registry: Registry) -> None:
    plan = build(tree, "surface_web")
    kinds = {args_of(node)["assertion"]["kind"] for node in nodes_of(plan, "expect.target")}
    assert {"visible", "hidden", "text", "count", "value"} <= kinds
    values = nodes_of(plan, "expect.value")
    assert {args_of(node)["assertion"]["kind"] for node in values} >= {"eq", "contains", "matches"}
    assert any(args_of(node)["soft"] for node in values)
    assert any(args_of(node)["eventually"] for node in nodes_of(plan, "expect.target"))


def test_seed_composition_and_personas(tree: Path, eks_registry: Registry) -> None:
    standard = build(tree, "standard")
    assert standard.personas == ["host", "harman-admin"]
    feature = build(tree, "subeler_seed")
    (extend,) = nodes_of(feature, "data.extend")
    # the base seed's plan hash is part of the extending seed's args, so its snapshot key covers the base
    assert extend.args == {"base": "standard", "hash": standard.hash}
    assert feature.personas == ["moda-admin"]
    assert seed_personas("subeler", registry=tree_registry(tree)) == ["host", "harman-admin", "moda-admin"]
    assert seed_personas("subeler") is None
    assert feature.requires == [Requirement(plugin="cookbook", version="1.0.0")]


def test_network_and_debugging(tree: Path, eks_registry: Registry) -> None:
    network = build(tree, "network")
    actions = [args_of(node)["action"]["kind"] for node in nodes_of(network, "net.route")]
    assert actions == ["delay", "respond", "patch"]
    (cassette,) = nodes_of(network, "net.cassette")
    assert cassette.branches is not None
    assert [node.op for node in cassette.branches["body"]] == ["ui.nav"]
    debugging = build(tree, "debugging")
    assert [node.args for node in nodes_of(debugging, "debug.break")] == [{"label": "before-submit"}]
    assert nodes_of(debugging, "debug.pause_on_error")


def test_overlays_use_all_three_renderers(tree: Path, eks_registry: Registry) -> None:
    live = build(tree, "overlays")
    (record,) = nodes_of(live, "capture.record")
    assert record.branches is not None
    assert {node.op for node in record.branches["body"]} >= {"overlay.show", "ui.fill", "ui.tap"}
    (annotate,) = nodes_of(live, "overlay.annotate")
    assert args_of(annotate)["style"] == "review"
    assert [item["kind"] for item in args_of(annotate)["overlays"]] == [
        "callout", "highlight", "redact", "arrow", "step"]
    video = build(tree, "annotated")
    (render,) = nodes_of(video, "edit.render")
    overlays = args_of(render)["timeline"]["tracks"][1]["overlays"]
    assert [item["kind"] for item in overlays] == [
        "title", "callout", "spotlight", "highlight", "arrow", "redact", "blur", "step"]


def test_audio_mix_and_gfx_layers(tree: Path, eks_registry: Registry) -> None:
    mix = build(tree, "mix")
    (audio,) = nodes_of(mix, "audio.mix")
    graph = args_of(audio)["mix"]
    assert graph["master"]["loudness"] == -14.0
    assert graph["master"]["true_peak"] == -1.0
    song, voice = graph["tracks"]
    assert song["stems"] == ["drums", "bass", "other"]
    assert song["ducks"][0]["under"] == "voiceover"
    assert voice["compressor"]["ratio"] == 3.0
    assert [band["type"] for band in voice["eq"]["bands"]] == ["high_pass", "peak"]
    gfx = build(tree, "lower_third")
    (render,) = nodes_of(gfx, "gfx.render")
    kinds = {layer["kind"] for layer in args_of(render)["scene"]["layers"]}
    children = {child["kind"] for layer in args_of(render)["scene"]["layers"]
                for child in layer.get("children", [])}
    assert kinds | children >= {"group", "rect", "text", "path", "image", "video", "ellipse"}


def test_score_binds_take_scene_gfx_and_audio(tree: Path, eks_registry: Registry) -> None:
    plan = build(tree, "ad")
    assert ops_of(plan)[:3] == ["media.source", "media.markers", "score.take"]
    (take,) = nodes_of(plan, "score.take")
    assert take.branches is not None
    assert [node.pin for node in take.branches["body"] if node.pin is not None] == [
        eks.t("bar:1"), eks.t("beat:6"), eks.t("bar:2")]
    assert {"blender.render", "gfx.render", "audio.mix", "edit.render"} <= set(ops_of(plan))
    # the renders consume take footage and song and scene files, so their cache keys add resolved content
    for op in ("blender.render", "edit.render", "audio.mix", "gfx.render"):
        (node,) = nodes_of(plan, op)
        assert not cacheable(plan, node), op
    assert take.id in (cache_inputs(plan, nodes_of(plan, "edit.render")[0]) or ())


def test_extensions_register_and_are_used(tree: Path, eks_registry: Registry) -> None:
    flow = build(tree, "extending")
    edit = build(tree, "extending_edit")
    registry = tree_registry(tree)
    assert not eks_registry.has_op("cookbook.rate")
    # section 11: every decorator of the table contributed something from the cookbook plugin
    assert registry.op("cookbook.rate").provenance.plugin == "cookbook"
    assert registry.op("cookbook.rate").executor is not None
    assert registry.macro("cookbook.new_invoice").platforms == frozenset({"web"})
    assert registry.resource("tenant").provenance.plugin == "cookbook"
    assert registry.check("contrast").provenance.plugin == "cookbook"
    for category, kind in (("effect", "sparkle"), ("transition", "gold_wipe"), ("easing", "out_soft"),
                           ("curve", "heartbeat"), ("overlay", "badge"), ("target", "cookbook_chip")):
        assert registry.catalog(category, kind).provenance.plugin == "cookbook", (category, kind)
    assert set(registry.catalog("overlay", "badge").renderers) == {"live", "screenshot", "video"}
    assert registry.renderer("svg").provenance.plugin == "cookbook"
    assert registry.backend("staging-readonly").provenance.plugin == "cookbook"
    assert registry.backend("eksenapi").provenance.plugin == "cookbook"
    assert registry.backend_policy("local-when-api-changes").provenance.plugin == "cookbook"
    # implementation hooks: per-surface target resolvers and the check implementation are module:qualname refs
    assert set(registry.catalog("target", "cookbook_chip").resolvers) == {"web", "screenshot", "video"}
    assert (registry.check("contrast").impl or "").endswith(":low_contrast")
    assert registry.preset("viewport", "cookbook-tablet").provenance.plugin == "cookbook"
    assert registry.preset("network", "cookbook-edge").provenance.plugin == "cookbook"

    assert flow.requires == [Requirement(plugin="cookbook", version="1.0.0")]
    assert flow.session is not None
    assert flow.session.viewport == "cookbook-tablet"
    ops = ops_of(flow)
    assert {"cookbook.rate", "ctl.macro"} <= set(ops)
    (macro,) = nodes_of(flow, "ctl.macro")
    assert args_of(macro)["name"] == "cookbook.new_invoice"
    (checkpoint,) = nodes_of(flow, "check.checkpoint")
    assert args_of(checkpoint)["checks"] == [{"kind": "contrast", "params": {"minimum": 7.0}}]
    assert nodes_of(flow, "cookbook.rate")[0].args == {"currency": "XAU", "base": "TRY"}
    chip = {"by": "cookbook_chip", "args": {"label": "Bekleyen"}}
    assert args_of(nodes_of(flow, "ui.tap")[-1])["target"] == chip
    assert args_of(nodes_of(flow, "overlay.show")[0])["overlay"]["anchor"] == chip
    (render,) = nodes_of(edit, "edit.render")
    first, second = args_of(render)["timeline"]["tracks"][0]["clips"]
    assert [effect["kind"] for effect in first["effects"]] == ["sparkle", "zoom"]
    assert first["effects"][1]["scale"]["keys"][1]["ease"] == {"kind": "out_soft", "softness": 0.6}
    assert first["transition_out"] == {"kind": "gold_wipe", "duration": "12f"}
    assert first["opacity"]["curve"]["kind"] == "heartbeat"
    assert [effect["kind"] for effect in second["effects"]] == ["sparkle"]
    badges = args_of(render)["timeline"]["tracks"][1]["overlays"]
    assert badges == [{"kind": "badge", "anchor": {"by": "test_id", "args": {"id": "total"}}, "text": "Yeni",
                       "window": {"start": "1s", "end": "3s"}, "keep": False, "emphasis": "none"}]
    assert edit.requires == [Requirement(plugin="cookbook", version="1.0.0")]


def test_backends_of_every_example_are_registered(tree: Path, eks_registry: Registry) -> None:
    # design 5.2 resource availability: every backend definition names a backend the tree registers
    for name in EXAMPLES:
        plan = build(tree, name)
        definitions = {resource.definition for resource in plan.resources if resource.kind == "backend"}
        assert definitions <= {"eksenapi"}, name
        assert [issue for issue in eks.validate(plan, tree=tree).issues if issue.code == "resource.backend"] == []
    flow = build(tree, "subeler")
    flow.resources = [resource.model_copy(update={"definition": "elsewhere"}) if resource.kind == "backend"
                      else resource for resource in flow.resources]
    assert "resource.backend" in {issue.code for issue in eks.validate(flow, tree=tree).errors}


def test_flows_freeze_the_clock_of_their_seed(tree: Path, eks_registry: Registry) -> None:
    # design 5.1: the subeler flow's clock is the standard seed's frozen now and root seed, on its backend
    plan = build(tree, "subeler")
    clock = json_of(plan)["clock"]
    assert clock["now"] == "2026-01-15T09:00:00+03:00"
    assert clock["seed"] == 2026
    backend = next(resource for resource in json_of(plan)["resources"] if resource["kind"] == "backend")
    assert (backend["definition"], backend["seed"]) == ("eksenapi", "standard")
    for name, example in EXAMPLES.items():
        if example.kind in ("flow", "seed"):
            assert build(tree, name).clock.now is not None, name
        session = build(tree, name).session
        if session is not None:
            # the frozen clock comes with a fixed zone and locale from the app profile (no clock.zone warning)
            assert (session.timezone, session.locale) == ("Europe/Istanbul", "tr-TR"), name


def test_unknown_apps_and_platforms_are_reported(tree: Path, eks_registry: Registry) -> None:
    flow = tree / ".harness" / "flows" / "lost.py"
    flow.write_text(
        "import eks\n\n\n@eks.flow(app='no-such-app', platforms=['web'], seed='standard')\n"
        "def lost(s: eks.Session):\n    s.ui.nav('/tenant')\n", encoding="utf-8")
    report = eks.validate(plan_spec(".harness/flows/lost.py", cwd=tree, plugins=True), tree=tree)
    assert {issue.code for issue in report.errors} == {"resource.app"}
    flow.write_text(
        "import eks\n\n\n@eks.flow(app='web-app', platforms=['ios'], seed='standard')\n"
        "def lost(s: eks.Session):\n    s.ui.nav('/tenant')\n", encoding="utf-8")
    report = eks.validate(plan_spec(".harness/flows/lost.py", cwd=tree, plugins=True), tree=tree)
    assert [issue.code for issue in report.errors] == ["resource.app"]


@pytest.mark.parametrize(("name", "renamed"), [("demo", "demo_renamed"), ("ad", "ad_renamed"),
                                                 ("timing", "timing_renamed")])
def test_renaming_an_entry_keeps_every_hash(name: str, renamed: str, tree: Path, eks_registry: Registry) -> None:
    source = tree / EXAMPLES[name].file
    original = build(tree, name)
    text = source.read_text(encoding="utf-8").replace(f"def {name}(", f"def {renamed}(")
    copy = source.with_name(f"{renamed}.py")
    copy.write_text(text, encoding="utf-8")
    other = plan_spec(copy.relative_to(tree).as_posix(), params=EXAMPLES[name].params, cwd=tree, plugins=True)
    assert other.name == renamed
    assert [node.id for node, _ in other.walk()] != [node.id for node, _ in original.walk()]
    assert [(node.op, node.hash) for node, _ in other.walk()] == [(node.op, node.hash) for node, _ in original.walk()]
    assert other.hash == original.hash
