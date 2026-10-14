from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import eks
from eks.discovery import tree_registry
from eks.workspace import plan_spec
from eks_harness.engine.validator import Report
from eks_harness.ir.hashing import cache_inputs, cacheable, hash_plan, machine_dependent
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import (
    Registry,
    RegistryError,
    default_registry,
    using_registry,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = ROOT / "examples" / "dsl"


def copy_examples(target: Path) -> Path:
    shutil.copytree(EXAMPLES_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def one(plan: Plan, op: str) -> Node:
    (node,) = nodes_of(plan, op)
    return node


def rehashed(data: dict[str, Any], tree: Path) -> Plan:
    edited = Plan.model_validate(data)
    with using_registry(tree_registry(tree)):
        return hash_plan(edited)


def codes(report: Report) -> list[tuple[str, str | None, str | None]]:
    return [(issue.code, issue.node, issue.field) for issue in report.issues]


def errors(report: Report) -> list[str]:
    return [issue.code for issue in report.issues if issue.severity == "error"]


# nodes that use several resources join every lane they use (contract 5.4)


LANES_FLOW = '''import eks


@eks.flow(app="web-app", seed="standard", as_="harman-admin")
def lanes(s: eks.Session):
    with eks.parallel():
        s.ui.nav("/tenant/invoice/list")
        s.as_("host")
        s.api.post("/api/tenant-branches", json={"name": "Moda"})
    with eks.parallel():
        s.ui.nav("/tenant/invoice/new")

        @eks.dynamic
        def login_as_host(name="host"):
            return [s.as_(name)]

        s.api.get("/api/tenant-branches")
'''


@pytest.fixture
def lanes_tree(tmp_path: Path) -> Path:
    tree = copy_examples(tmp_path / "dsl")
    (tree / ".harness" / "flows" / "lanes.py").write_text(LANES_FLOW, encoding="utf-8")
    return tree


def test_a_persona_switch_in_a_parallel_body_orders_the_later_api_call(lanes_tree: Path,
                                                                      eks_registry: Registry) -> None:
    plan = plan_spec(".harness/flows/lanes.py", cwd=lanes_tree, plugins=True)
    first, second = nodes_of(plan, "ctl.parallel")
    assert first.branches is not None and second.branches is not None
    nav, switch, post = first.branches["body"]
    assert (nav.op, switch.op, post.op) == ("ui.nav", "session.as", "api.post")
    assert len(switch.uses) == 2
    # the switch is on the lease and the backend lane, so the request waits for it on the backend lane
    assert switch.after == [nav.id]
    assert post.after == [switch.id]
    nav, dynamic, get = second.branches["body"]
    assert (nav.op, dynamic.op, get.op) == ("ui.nav", "ctl.dynamic", "api.get")
    assert dynamic.after == [nav.id]
    assert get.after == [dynamic.id]
    assert eks.validate(plan, tree=lanes_tree).issues == ()


def test_the_validator_wants_every_lane_of_a_multi_resource_node(lanes_tree: Path, eks_registry: Registry) -> None:
    plan = plan_spec(".harness/flows/lanes.py", cwd=lanes_tree, plugins=True)
    data = json.loads(plan.to_json())
    post = next(node for node in data["nodes"][1]["branches"]["body"] if node["op"] == "api.post")
    get = next(node for node in data["nodes"][2]["branches"]["body"] if node["op"] == "api.get")
    post["after"] = []
    get["after"] = []
    report = eks.validate(rehashed(data, lanes_tree), tree=lanes_tree)
    assert [(code, node, field) for code, node, field in codes(report)] == [
        ("node.after", post["id"], "after"), ("node.after", get["id"], "after")]


EACH_LANES_FLOW = '''import eks


@eks.flow(app="web-app", seed="standard", as_="harman-admin")
def eachlanes(s: eks.Session):
    with eks.parallel():
        s.ui.nav("/tenant/invoice/list")
        with eks.each(["host", "harman-admin"]) as who:
            s.api.get("/api/tenant-branches")
            s.as_("host")
            s.api.post("/api/tenant-branches", json={"name": who})
'''


def test_literal_each_copies_join_every_lane_of_their_heads(lanes_tree: Path, eks_registry: Registry) -> None:
    (lanes_tree / ".harness" / "flows" / "eachlanes.py").write_text(EACH_LANES_FLOW, encoding="utf-8")
    plan = plan_spec(".harness/flows/eachlanes.py", cwd=lanes_tree, plugins=True)
    (parallel,) = nodes_of(plan, "ctl.parallel")
    assert parallel.branches is not None
    nav, get0, switch0, post0, get1, switch1, post1 = parallel.branches["body"]
    assert [node.op for node in (get0, switch0, post0)] == ["api.get", "session.as", "api.post"]
    assert get0.after == []
    assert switch0.after == [nav.id, get0.id]
    assert post0.after == [switch0.id]
    assert get1.after == [post0.id]
    assert switch1.after == [switch0.id, get1.id]
    assert post1.after == [switch1.id]
    assert eks.validate(plan, tree=lanes_tree).issues == ()


# every tree plans against its own registry (contract 9.3)


def test_two_copies_of_one_project_plan_in_one_process(tmp_path: Path, eks_registry: Registry) -> None:
    first = copy_examples(tmp_path / "a" / "dsl")
    second = copy_examples(tmp_path / "b" / "dsl")
    plans = [plan_spec(".harness/flows/subeler.py", cwd=tree, plugins=True) for tree in (first, second)]
    assert plans[0].hash == plans[1].hash
    assert plans[0].to_json() == plans[1].to_json()
    for plan, tree in zip(plans, (first, second), strict=True):
        assert eks.validate(plan, tree=tree).issues == ()
    assert tree_registry(first) is not tree_registry(second)
    assert tree_registry(first).has_op("cookbook.rate")
    assert not eks_registry.has_op("cookbook.rate")
    assert default_registry() is eks_registry


LEAK_FLOW = '''import eks


@eks.flow(app="web-app", now="2026-01-15T09:00+03:00")
def leak(s: eks.Session):
    s.net.shape("cookbook-edge")
    s.op("cookbook.rate", currency="XAU")
'''


def plugin_less_tree(root: Path) -> Path:
    (root / ".harness" / "flows").mkdir(parents=True)
    (root / ".harness" / "project.toml").write_text('[project]\nid = "other/proj"\n', encoding="utf-8")
    (root / "web-app" / ".harness").mkdir(parents=True)
    (root / "web-app" / ".harness" / "app.toml").write_text(
        '[app]\nname = "web-app"\nplatforms = ["web"]\n\n[web]\nurl = "http://localhost:3000"\n'
        'timezone = "Europe/Istanbul"\nlocale = "tr-TR"\n', encoding="utf-8")
    (root / ".harness" / "flows" / "leak.py").write_text(LEAK_FLOW, encoding="utf-8")
    return root


def test_another_trees_plugins_never_leak_into_a_plan(tmp_path: Path, eks_registry: Registry) -> None:
    examples = copy_examples(tmp_path / "a" / "dsl")
    other = plugin_less_tree(tmp_path / "b")
    plan_spec(".harness/flows/extending.py", cwd=examples, plugins=True)
    with pytest.raises(RegistryError, match=r"unknown op 'cookbook\.rate'"):
        plan_spec(".harness/flows/leak.py", cwd=other, plugins=True)
    assert not tree_registry(other).has_op("cookbook.rate")
    assert not any(spec.name == "cookbook-edge" for spec in tree_registry(other).presets("network"))
    assert any(spec.name == "cookbook-edge" for spec in tree_registry(examples).presets("network"))


def test_a_seed_plans_in_its_own_tree_from_any_registry(tmp_path: Path, eks_registry: Registry) -> None:
    first = copy_examples(tmp_path / "a" / "dsl")
    second = copy_examples(tmp_path / "b" / "dsl")
    standard = plan_spec(".harness/seeds/standard.py", cwd=first, plugins=True)
    feature = plan_spec(".harness/seeds/feature.py", cwd=second, plugins=True)
    (extend,) = nodes_of(feature, "data.extend")
    assert extend.args["hash"] == standard.hash
    assert tree_registry(first).entry("seed", "standard").file != tree_registry(second).entry("seed", "standard").file


# device-dependent inference joins the cache key (contract 7.3)


VOICE_EDIT = '''import eks
from eks import t


@eks.edit(fps=30, size=(1080, 1920), seed=7, duration=t("10s"))
def voice(e: eks.Edit):
    vo = eks.media.tts("Kontör işlemleri artık tek ekranda", voice="tr_female_1")
    song = eks.media.source("artifact:SONG1@sha256:" + "a" * 64)
    vocals = eks.media.stems(song, "vocals")
    e.audio.track(vo, name="vo")
    e.audio.track(vocals, name="vocals").denoise()
    e.render_settings(encoder="libx264")
'''

PLAIN_MIX = '''import eks
from eks import t


@eks.edit(fps=30, size=(1080, 1920), seed=7, duration=t("10s"))
def plain(e: eks.Edit):
    song = eks.media.source("artifact:SONG1@sha256:" + "a" * 64)
    e.audio.track(song, name="song")
    e.render_settings(encoder="libx264")
'''


def test_speech_stems_and_denoised_mixes_are_machine_dependent(tmp_path: Path, eks_registry: Registry) -> None:
    tree = copy_examples(tmp_path / "dsl")
    (tree / ".harness" / "edits" / "voice.py").write_text(VOICE_EDIT, encoding="utf-8")
    (tree / ".harness" / "edits" / "plain.py").write_text(PLAIN_MIX, encoding="utf-8")
    plan = plan_spec(".harness/edits/voice.py", cwd=tree, plugins=True)
    assert eks.validate(plan, tree=tree).issues == ()
    tts, stems, mix, render = (one(plan, op) for op in ("media.tts", "media.stems", "audio.mix", "edit.render"))
    assert default_registry().op("media.tts").device_dependent
    assert default_registry().op("media.stems").device_dependent
    assert not default_registry().op("audio.mix").device_dependent
    for node in (tts, stems, mix):
        assert machine_dependent(node), node.op
        assert node.id in (cache_inputs(plan, node) or ()), node.op
        assert not cacheable(plan, node), node.op
    assert not machine_dependent(render)
    assert {tts.id, stems.id, mix.id} <= set(cache_inputs(plan, render) or ())
    plain = plan_spec(".harness/edits/plain.py", cwd=tree, plugins=True)
    assert not machine_dependent(one(plain, "audio.mix"))
    assert cacheable(plain, one(plain, "audio.mix"))
    assert cacheable(plain, one(plain, "edit.render"))


def test_a_plugin_op_can_declare_itself_device_dependent(eks_registry: Registry) -> None:
    @eks.op("acme", name="upscale", out="media", pure=True, location="node", device_dependent=True)
    class Upscale(eks.Args):
        factor: int = 2

    node = Node(id="n0000000001", op="acme.upscale", args={"factor": 2}, out="media")
    assert eks_registry.op("acme.upscale").device_dependent
    assert machine_dependent(node)


# seed paths, keys and personas are checked in the plan (contract 11, seed.path and seed.persona)


def test_seed_paths_keys_and_personas_are_checked(tmp_path: Path, eks_registry: Registry) -> None:
    tree = copy_examples(tmp_path / "dsl")
    plan = plan_spec(".harness/seeds/standard.py", cwd=tree, plugins=True)
    assert eks.validate(plan, tree=tree).issues == ()
    data = json.loads(plan.to_json())
    creates = [node for node in data["nodes"] if node["op"] == "data.create"]
    harman = next(node for node in creates if node["args"]["path"] == "tenant:harman")
    twin = json.loads(json.dumps(harman))
    twin["id"] = "n0dup000000"
    twin["args"]["key"] = "other"
    data["nodes"].append(twin)
    federation = next(node for node in creates if node["args"]["kind"] == "federation")
    federation["args"]["path"] = "tenant:harman/persona:harman-admin"
    persona = next(node for node in creates if node["args"]["kind"] == "persona")
    second = json.loads(json.dumps(persona))
    second["id"] = "n0dup000001"
    second["args"]["path"] = "tenant:harman/persona:harman-admin-2"
    data["nodes"].append(second)
    data["personas"] = [*data["personas"], "ghost-admin"]
    report = eks.validate(rehashed(data, tree), tree=tree)
    assert codes(report) == [
        ("seed.persona", None, None),
        ("seed.path", federation["id"], "path"),
        ("seed.path", persona["id"], "path"),
        ("seed.path", twin["id"], "path"),
        ("seed.path", twin["id"], "path"),
        ("seed.path", second["id"], "path"),
        ("seed.persona", second["id"], "key"),
    ]
    assert "it is 'federation:anadolu-bayi'" in report.issues[1].message
    assert "it is 'tenant:other'" in report.issues[3].message


def test_seed_resources_inside_a_container_are_reported(tmp_path: Path, eks_registry: Registry) -> None:
    tree = copy_examples(tmp_path / "dsl")
    plan = plan_spec(".harness/seeds/standard.py", cwd=tree, plugins=True)
    data = json.loads(plan.to_json())
    backend = data["resources"][0]["id"]
    harman = next(node for node in data["nodes"]
                  if node["op"] == "data.create" and node["args"]["path"] == "tenant:harman")
    reader = {"id": "n0read00000", "op": "api.get", "v": "1", "args": {"path": "/api/currencies"},
              "uses": [backend], "after": [], "out": "json"}
    loop = {"id": "n0loop00000", "op": "ctl.each", "v": "1",
            "args": {"over": {"$ref": "n0read00000", "path": ["items"]}},
            "after": ["n0read00000"], "out": "none",
            "branches": {"body": [{
                "id": "n0acct00000", "op": "data.create", "v": "1",
                "args": {"kind": "bank_account", "key": "loop", "path": "tenant:harman/bank_account:loop",
                         "parent": {"$ref": harman["id"], "path": []},
                         "fields": {"currency": "TRY", "opening": 0}, "phase": "api"},
                "uses": [backend], "after": [harman["id"]], "out": "resource:bank_account"}]}}
    data["nodes"].extend([reader, loop])
    report = eks.validate(rehashed(data, tree), tree=tree)
    assert codes(report) == [("seed.path", "n0acct00000", None)]
    assert "inside ctl.each 'n0loop00000'" in report.issues[0].message


def test_keyless_paths_count_their_siblings(tmp_path: Path, eks_registry: Registry) -> None:
    tree = copy_examples(tmp_path / "dsl")
    plan = plan_spec(".harness/seeds/standard.py", cwd=tree, plugins=True)
    data = json.loads(plan.to_json())
    invoice = next(node for node in data["nodes"] if node["args"].get("path") == "tenant:harman/invoices#0")
    invoice["args"]["path"] = "tenant:harman/invoices#3"
    report = eks.validate(rehashed(data, tree), tree=tree)
    assert codes(report) == [("seed.path", invoice["id"], "path")]
    assert "it is 'tenant:harman/invoices#0'" in report.issues[0].message


# a session has one key, one lease and one backend (contract 11, session.*)


TWO_TAKES = '''import eks
from eks import t


@eks.score(fps=30, song="music/song.ogg", window=t.span(t("0s"), t("15s")))
def two(sc: eks.Score):
    a = sc.take(platform="ios", app="mobile-app", seed="standard", as_="harman-admin",
                span=t.span(t("0s"), t("7s")))
    with a.flow() as s:
        s.ui.tap("#new-invoice", at=t("1s"))
    b = sc.take(platform="android", app="mobile-app", seed="standard", as_="harman-admin",
                span=t.span(t("7s"), t("15s")))
    with b.flow() as s:
        s.ui.tap("#new-invoice", at=t("8s"))
    sc.edit.track("a").clip(a)
    sc.edit.track("b").clip(b, start=t("7s"))
'''


def test_a_take_cannot_borrow_another_takes_session(tmp_path: Path, eks_registry: Registry) -> None:
    tree = copy_examples(tmp_path / "dsl")
    (tree / ".harness" / "scores" / "two.py").write_text(TWO_TAKES, encoding="utf-8")
    plan = plan_spec(".harness/scores/two.py", cwd=tree, plugins=True)
    assert eks.validate(plan, tree=tree).issues == ()
    data = json.loads(plan.to_json())
    first, second = (node for node in data["nodes"] if node["op"] == "score.take")
    lease = next(resource["id"] for resource in data["resources"] if resource["kind"] == "lease.ios")
    backend = next(resource["id"] for resource in data["resources"]
                   if resource["kind"] == "backend" and resource["session"] == first["args"]["session"]["key"])
    kinds = {resource["id"]: resource["kind"] for resource in data["resources"]}
    dropped = set(second["uses"])
    second["args"]["session"]["key"] = first["args"]["session"]["key"]
    second["uses"] = [lease, backend]
    for node in second["branches"]["body"]:
        node["uses"] = [lease if kinds[rid].startswith("lease.") else backend for rid in node["uses"]]
    data["resources"] = [resource for resource in data["resources"] if resource["id"] not in dropped]
    report = eks.validate(rehashed(data, tree), tree=tree)
    assert codes(report) == [
        ("session.duplicate", second["id"], "session.key"),
        ("session.mismatch", second["id"], "session"),
    ]
    assert "lease.ios lease but the session runs on android" in report.issues[1].message


def test_one_session_drives_one_lease_and_one_backend(tmp_path: Path, eks_registry: Registry) -> None:
    tree = copy_examples(tmp_path / "dsl")
    plan = plan_spec(".harness/flows/subeler.py", cwd=tree, plugins=True)
    data = json.loads(plan.to_json())
    lease = next(resource for resource in data["resources"] if resource["kind"] == "lease.web")
    backend = next(resource for resource in data["resources"] if resource["kind"] == "backend")
    data["resources"].append({**lease, "id": "r2ndls00"})
    data["resources"].append({**backend, "id": "r2ndbc00", "name": "second"})
    navs = [node for node in data["nodes"] if node["op"] == "ui.nav"]
    navs[1]["uses"] = ["r2ndls00"]
    (get,) = (node for node in data["nodes"] if node["op"] == "api.get")
    get["uses"] = ["r2ndbc00"]
    report = eks.validate(rehashed(data, tree), tree=tree)
    found = [(code, node) for code, node, _field in codes(report)]
    assert found == [
        ("resource.id", None), ("resource.id", None), ("session.resources", None), ("session.resources", None)]
    assert "'r2ndls00' (lease.web) has the identity of resource" in report.issues[0].message
    assert "'r2ndbc00' (backend) has the identity of resource" in report.issues[1].message
    assert "2 backend resources" in report.issues[2].message
    assert "2 lease resources" in report.issues[3].message


# scene graphs are checked in the plan, not only by the builders (contract 11, scene.*)


def test_scene_ids_cycles_and_events_are_checked(tmp_path: Path, eks_registry: Registry) -> None:
    tree = copy_examples(tmp_path / "dsl")
    plan = plan_spec(".harness/scores/ad.py", cwd=tree, plugins=True)
    assert eks.validate(plan, tree=tree).issues == ()
    data = json.loads(plan.to_json())
    blender = next(node for node in data["nodes"] if node["op"] == "blender.render")
    gfx = next(node for node in data["nodes"] if node["op"] == "gfx.render")
    source = next(node for node in data["nodes"] if node["op"] == "media.source")
    scene = blender["args"]["scene"]
    scene["nodes"].append({"id": "phone", "kind": "mesh", "primitive": "cube"})
    for node in scene["nodes"]:
        if node["id"] == "camera":
            node["parent"] = "phone"
        if node["id"] == "phone" and node["kind"] == "load":
            node["parent"] = "camera"
    layer = gfx["args"]["scene"]["layers"][0]["id"]
    pulse = {"kind": "pulse", "prop": "scale", "amount": 1.1, "duration": "0.25s"}
    gfx["args"]["scene"]["triggers"].append(
        {"at": {"source": {"$ref": blender["id"], "path": []}, "name": "never-declared"}, "layer": layer,
         "action": pulse})
    gfx["args"]["scene"]["triggers"].append(
        {"at": {"source": {"$ref": source["id"], "path": []}, "name": "reveal"}, "layer": layer, "action": pulse})
    layers = gfx["args"]["scene"]["layers"]
    layers.append(dict(layers[0], text="Duplicate"))
    layers[0]["mask"] = "subtitle"
    layers[1]["mask"] = "title"
    gfx["after"] = list(dict.fromkeys([*gfx["after"], blender["id"], source["id"]]))
    report = eks.validate(rehashed(data, tree), tree=tree)
    assert codes(report) == [
        ("scene.cycle", blender["id"], "scene.nodes"),
        ("scene.duplicate", blender["id"], "scene.nodes.2.id"),
        ("scene.cycle", gfx["id"], "scene.layers"),
        ("scene.duplicate", gfx["id"], "scene.layers.2.id"),
        ("scene.event", gfx["id"], "scene.triggers.1.at"),
        ("scene.event", gfx["id"], "scene.triggers.2.at"),
    ]
    assert "phone -> camera -> phone" in report.issues[0].message
    assert "title -> subtitle -> title" in report.issues[2].message
    assert "never emits 'never-declared'" in report.issues[4].message
    assert "which is media.source" in report.issues[5].message


def test_a_layer_masked_by_its_own_group_is_a_cycle(tmp_path: Path, eks_registry: Registry) -> None:
    tree = copy_examples(tmp_path / "dsl")
    plan = plan_spec(".harness/scores/ad.py", cwd=tree, plugins=True)
    data = json.loads(plan.to_json())
    gfx = next(node for node in data["nodes"] if node["op"] == "gfx.render")
    layers = gfx["args"]["scene"]["layers"]
    child = dict(layers[0], id="inner", mask="box")
    layers.append({"kind": "group", "id": "box", "children": [child]})
    report = eks.validate(rehashed(data, tree), tree=tree)
    assert [(code, field) for code, _node, field in codes(report)] == [("scene.cycle", "scene.layers")]
    assert "box -> inner -> box" in report.issues[0].message
