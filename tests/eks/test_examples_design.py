from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

import eks
from eks.tracing import TraceError
from eks.workspace import plan_spec
from eks_harness.engine.validator import Report
from eks_harness.ir.plan import Node, Plan, Requirement
from eks_harness.ir.registry import Registry
from eks_harness.ir.time import TempoMap, t

FIXTURE = Path(__file__).parent / "examples" / "tree"
MOBILE_DEFAULTS = [{"kind": "error_toasts", "params": {}}, {"kind": "page_errors", "params": {"noise": []}}]

# every example of design.md, as (entry file under the tree root, --set params)
EXAMPLES: dict[str, tuple[str, dict[str, str]]] = {
    "subeler": (".harness/flows/subeler.py", {}),
    "control": (".harness/flows/control.py", {}),
    "network": (".harness/flows/network.py", {}),
    "debugging": (".harness/flows/debugging.py", {}),
    "standard": (".harness/seeds/standard.py", {}),
    "demo": (".harness/edits/demo.py", {}),
    "phone": (".harness/scenes/phone.py", {"screen": "artifact:SCREEN1"}),
    "titles": (".harness/scenes/titles.py", {}),
    "ad": (".harness/scores/ad.py", {}),
}

# the op sequence of every example in plan walk order, with the nesting depth
OP_SEQUENCES: dict[str, list[tuple[str, int]]] = {
    "subeler": [
        ("session.as", 0), ("api.get", 0), ("ui.nav", 0), ("expect.target", 0), ("check.checkpoint", 0),
        ("check.checkpoint", 0), ("ui.nav", 0), ("expect.target", 0)],
    "control": [
        ("ui.exists", 0), ("ctl.when", 0), ("ui.tap", 1), ("ui.tap", 1), ("api.get", 0), ("ctl.each", 0),
        ("ui.nav", 1), ("check.checkpoint", 1), ("ctl.parallel", 0), ("ui.nav", 1), ("api.get", 1),
        ("ui.count", 0), ("ctl.dynamic", 0)],
    "network": [
        ("net.route", 0), ("net.route", 0), ("net.route", 0), ("net.shape", 0), ("net.cassette", 0),
        ("ui.nav", 1), ("inject.script", 0)],
    "debugging": [("debug.break", 0)],
    "standard": [("data.create", 0)] * 9,
    "demo": [
        ("media.source", 0), ("media.source", 0), ("media.markers", 0), ("audio.mix", 0), ("edit.render", 0),
        ("check.media", 0)],
    "phone": [("media.source", 0), ("media.source", 0), ("blender.render", 0)],
    "titles": [("gfx.render", 0)],
    "ad": [
        ("media.source", 0), ("media.markers", 0), ("score.take", 0), ("session.as", 1), ("ui.tap", 1),
        ("ui.fill", 1), ("overlay.show", 1), ("media.source", 0), ("blender.render", 0), ("gfx.render", 0),
        ("audio.mix", 0),
        ("edit.render", 0)],
}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "design"
    shutil.copytree(FIXTURE, root)
    return root


def plan_example(tree: Path, name: str) -> Plan:
    spec, params = EXAMPLES[name]
    return plan_spec(spec, params=params, cwd=tree, plugins=True)


def validated(plan: Plan, tree: Path) -> Report:
    report = eks.validate(plan, tree=tree)
    assert report.ok, report.text()
    assert report.issues == (), report.text()
    return report


def walk_ops(plan: Plan) -> list[tuple[str, int]]:
    return [(node.op, len(ancestors)) for node, ancestors in plan.walk()]


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def ref_to(node: Node, *path: Any) -> dict[str, Any]:
    return {"$ref": node.id, "path": list(path)}


def resources_of(plan: Plan) -> list[tuple[str, str | None, str | None, str | None, str | None]]:
    return [(resource.kind, resource.app, resource.definition, resource.seed, resource.session)
            for resource in plan.resources]


def test_the_fixture_tree_matches_the_contract_table() -> None:
    files = sorted(path.relative_to(FIXTURE).as_posix() for path in FIXTURE.rglob("*") if path.is_file()
                   and "__pycache__" not in path.parts)
    assert files == [
        ".harness/edits/demo.py",
        ".harness/flows/control.py",
        ".harness/flows/debugging.py",
        ".harness/flows/network.py",
        ".harness/flows/subeler.py",
        ".harness/plugins/examples/example_resources.py",
        ".harness/plugins/examples/harness-plugin.toml",
        ".harness/project.toml",
        ".harness/scenes/phone.py",
        ".harness/scenes/titles.py",
        ".harness/scores/ad.py",
        ".harness/seeds/standard.py",
        "mobile-app/.harness/app.toml",
        "web-app/.harness/app.toml",
    ]


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_every_example_plans_and_validates(name: str, tree: Path, eks_registry: Registry) -> None:
    plan = plan_example(tree, name)
    validated(plan, tree)
    assert plan.name == name
    assert walk_ops(plan) == OP_SEQUENCES[name]


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_every_example_is_deterministic(name: str, tree: Path, eks_registry: Registry) -> None:
    first = plan_example(tree, name)
    second = plan_example(tree, name)
    assert first.to_json() == second.to_json()
    assert first.hash == second.hash
    assert first.hash is not None


def test_subeler(tree: Path, eks_registry: Registry) -> None:
    plan = plan_example(tree, "subeler")
    assert plan.kind == "flow"
    assert plan.tags == ["subeler"]
    assert plan.base == ".harness/flows"
    assert plan.session is not None
    assert (plan.session.app, plan.session.platform, plan.session.seed, plan.session.persona) == (
        "web-app", "web", "standard", "harman-admin")
    assert set(resources_of(plan)) == {
        ("lease.web", "web-app", None, None, "main"),
        ("backend", "web-app", "eksenapi", "standard", "main"),
    }
    lease = next(resource.id for resource in plan.resources if resource.kind == "lease.web")
    backend = next(resource.id for resource in plan.resources if resource.kind == "backend")

    as_, get, nav, visible, liste, phone, nav_kadikoy, count = plan.nodes
    assert as_.args == {"persona": "harman-admin"}
    assert get.args == {"path": "/api/tenant-branches", "query": {"MaxResultCount": 50}}
    assert get.uses == [backend]
    assert get.src == ".harness/flows/subeler.py:6"
    assert nav.args == {"route": "/tenant/settings/branches"}
    assert nav.uses == [lease]
    assert visible.args["target"] == {"by": "text", "args": {"text": "Kadıköy", "exact": True}}
    assert visible.args["assertion"] == {"kind": "visible"}
    assert liste.args["label"] == "liste"
    assert "viewport" not in liste.args
    assert phone.args["label"] == "liste-telefon"
    assert phone.args["viewport"] == "phone"
    assert nav_kadikoy.args == {"route": {"$fmt": [
        "/tenant/invoice/list?branchId=",
        ref_to(get, "items", {"find": {"name": "Kadıköy"}}, "id"),
    ]}}
    assert get.id in nav_kadikoy.after
    assert count.args["target"] == {"by": "test_id", "args": {"id": "invoice-row"}}
    assert count.args["assertion"] == {"kind": "count", "at_least": 1}
    assert plan.outputs == {"branches": ref_to(get)}


def test_control(tree: Path, eks_registry: Registry) -> None:
    plan = plan_example(tree, "control")
    when = nodes_of(plan, "ctl.when")[0]
    assert when.branches is not None
    assert set(when.branches) == {"then", "else"}
    each = nodes_of(plan, "ctl.each")[0]
    (invoices, _parallel_get) = nodes_of(plan, "api.get")
    assert each.args["over"] == ref_to(invoices, "items", {"slice": [None, 3, None]})
    assert each.branches is not None
    nav, checkpoint = each.branches["body"]
    assert nav.args["route"] == {"$fmt": ["/tenant/invoice/", {"$item": each.id, "path": ["id"]}]}
    assert checkpoint.args["label"] == {"$fmt": ["fatura-", {"$item": each.id, "path": ["number"]}]}
    dynamic = nodes_of(plan, "ctl.dynamic")[0]
    count = nodes_of(plan, "ui.count")[0]
    assert dynamic.args["inputs"] == {"n": ref_to(count)}
    assert dynamic.args["fn"]["name"] == "per_row"  # type: ignore[index]
    assert dynamic.args["fn"]["freevars"] == {"s": {"kind": "session", "value": "main"}}  # type: ignore[index]


def test_network(tree: Path, eks_registry: Registry) -> None:
    plan = plan_example(tree, "network")
    delay, respond, patch = nodes_of(plan, "net.route")
    assert delay.args["action"] == {"kind": "delay", "by": "0.8s"}
    assert respond.args["action"] == {"kind": "respond", "status": 500, "json": {"error": "x"}}
    assert patch.args["action"] == {"kind": "patch", "path": "items[0].rate", "value": "2450.10"}
    assert nodes_of(plan, "net.shape")[0].args == {"profile": "3g"}
    cassette = nodes_of(plan, "net.cassette")[0]
    assert cassette.args == {"name": "banka-arama", "mode": "replay"}
    assert nodes_of(plan, "inject.script")[0].args == {"source": "window.__flags = {beta: true}", "when": "init"}


def test_debugging(tree: Path, eks_registry: Registry) -> None:
    plan = plan_example(tree, "debugging")
    assert plan.nodes[0].args == {"label": "before-submit"}


def test_standard_seed(tree: Path, eks_registry: Registry) -> None:
    plan = plan_example(tree, "standard")
    assert plan.kind == "seed"
    assert plan.personas == ["host", "harman-admin"]
    assert plan.clock.now == "2026-01-15T09:00:00+03:00"
    assert plan.clock.seed == 2026
    assert resources_of(plan) == [("backend", None, "eksenapi", "standard", None)]
    assert plan.requires == [Requirement(plugin="examples", version="1.0.0")]
    creates = {str(node.args["path"]): node for node in plan.nodes}
    assert list(creates) == [
        "host_persona:host",
        "federation:anadolu-bayi",
        "tenant:harman",
        "tenant:harman/persona:harman-admin",
        "accountant:yildiz",
        "accountant:yildiz/serves#0",
        "tenant:harman/bank_account:kuveyt",
        "tenant:harman/bank_account:kuveyt/transactions#0",
        "tenant:harman/invoices#0",
    ]
    harman = creates["tenant:harman"]
    assert harman.args["fields"] == {"name": "Harman Kuyumculuk",
                                     "reseller": ref_to(creates["federation:anadolu-bayi"])}
    serves = creates["accountant:yildiz/serves#0"]
    assert serves.args["parent"] == ref_to(creates["accountant:yildiz"])
    assert serves.args["fields"] == {"tenant": ref_to(harman)}
    assert creates["tenant:harman/bank_account:kuveyt"].args["fields"] == {"currency": "TRY", "opening": 250000}
    assert creates["tenant:harman/invoices#0"].args["fields"] == {"states": "*", "per_state": 3}


def test_demo_edit(tree: Path, eks_registry: Registry) -> None:
    plan = plan_example(tree, "demo")
    clip_source, song, markers, mix, render, cut = plan.nodes
    assert clip_source.args == {"uri": "artifact:VID123"}
    assert song.args == {"uri": "file:music/song.ogg"}
    assert plan.clock.tempo == TempoMap(source=markers.id)
    assert mix.args["mix"]["tracks"][0]["gain"] == -6.0  # type: ignore[index]
    timeline = render.args["timeline"]
    assert timeline["fps"] == 30  # type: ignore[index]
    assert timeline["size"] == [1080, 1920]  # type: ignore[index]
    assert timeline["audio"] == ref_to(mix)  # type: ignore[index]
    main, overlays = timeline["tracks"]  # type: ignore[index,misc]
    (clip,) = main["clips"]  # type: ignore[index]
    assert clip["media"] == ref_to(clip_source)  # type: ignore[index]
    assert clip["src"] == {"start": "2s", "end": "14s"}  # type: ignore[index]
    assert [effect["kind"] for effect in clip["effects"]] == ["zoom"]  # type: ignore[index]
    assert clip["transition_out"]["kind"] == "crossfade"  # type: ignore[index]
    assert overlays["overlays"][0]["kind"] == "caption"  # type: ignore[index]
    assert cut.args["kind"] == "cut"
    assert cut.args["at"] == "beat:8"
    assert cut.args["media"] == ref_to(render)


def test_phone_scene(tree: Path, eks_registry: Registry) -> None:
    plan = plan_example(tree, "phone")
    assert plan.params == {"screen": "artifact:SCREEN1"}
    screen, blend, render = plan.nodes
    assert screen.args == {"uri": "artifact:SCREEN1"}
    assert blend.args == {"uri": "file:assets/phone.blend", "kind": "file"}
    assert render.args["scene"]["nodes"][0]["path"] == ref_to(blend)  # type: ignore[index]
    scene = render.args["scene"]
    assert scene["render"]["engine"] == "CYCLES"  # type: ignore[index]
    assert scene["render"]["samples"] == 32  # type: ignore[index]
    assert scene["render"]["device"] == "GPU"  # type: ignore[index]
    assert scene["render"]["seed"] == 7  # type: ignore[index]
    assert scene["textures"] == [  # type: ignore[index]
        {"node": "phone", "material": "Screen", "slot": "screen", "media": ref_to(screen)}]
    assert scene["camera"] == "camera"  # type: ignore[index]
    assert [hook["fn"]["name"] for hook in scene["hooks"]] == ["rig"]  # type: ignore[index]
    assert plan.clock.tempo == TempoMap(external=True)


def test_ad_score(tree: Path, eks_registry: Registry) -> None:
    plan = plan_example(tree, "ad")
    assert plan.kind == "score"
    assert plan.clock.fps == 30
    assert plan.clock.window == t.span(t("0s"), t("15s"))
    song, markers, take, blend, scene, gfx, mix, render = plan.nodes
    assert blend.args == {"uri": "file:../scenes/assets/phone.blend", "kind": "file"}
    assert song.args == {"uri": "file:music/song.ogg"}
    assert markers.args["media"] == ref_to(song)
    assert plan.clock.tempo == TempoMap(source=markers.id)

    # the take: its session and three pinned body nodes after session.as
    assert take.args["session"] == {"key": "take0", "app": "mobile-app", "platform": "ios", "seed": "standard",
                                    "persona": "harman-admin", "backend": "eksenapi", "noise": [],
                                    "root_seed": 2026, "now": "2026-01-15T09:00:00+03:00",
                                    "timezone": "Europe/Istanbul", "locale": "tr-TR", "network_ignore": [],
                                    "viewports": {}, "checks": [], "defaults": MOBILE_DEFAULTS}
    assert take.branches is not None
    body = take.branches["body"]
    assert [(node.op, node.pin) for node in body] == [
        ("session.as", None), ("ui.tap", t("bar:1")), ("ui.fill", t("beat:6")), ("overlay.show", t("bar:2"))]
    assert body[0].args == {"persona": "harman-admin"}
    assert body[2].args["value"] == "5"
    assert body[3].args["overlay"]["kind"] == "callout"  # type: ignore[index]
    assert body[3].args["overlay"]["text"] == "Anlık kur"  # type: ignore[index]
    assert set(resources_of(plan)) == {
        ("lease.ios", "mobile-app", None, None, "take0"),
        ("backend", "mobile-app", "eksenapi", "standard", "take0"),
    }
    assert take.uses == [resource.id for kind in ("lease.ios", "backend")
                         for resource in plan.resources if resource.kind == kind]

    # the phone scene shows the take on its screen
    assert scene.args["scene"]["textures"] == [  # type: ignore[index]
        {"node": "phone", "material": "Screen", "slot": "screen", "media": ref_to(take)}]
    assert take.id in scene.after

    # one mix with the song and the -14 LUFS master, one render with both tracks
    assert mix.args["mix"]["master"]["loudness"] == -14.0  # type: ignore[index]
    tracks = mix.args["mix"]["tracks"]  # type: ignore[index]
    assert [(track["name"], track["media"]) for track in tracks] == [("song", ref_to(song))]  # type: ignore[index]
    timeline = render.args["timeline"]
    assert timeline["audio"] == ref_to(mix)  # type: ignore[index]
    assert [track["name"] for track in timeline["tracks"]] == ["scene", "graphics"]  # type: ignore[index]
    scene_track, graphics_track = timeline["tracks"]  # type: ignore[index,misc]
    assert [clip["media"] for clip in scene_track["clips"]] == [ref_to(scene)]  # type: ignore[index]
    assert [clip["media"] for clip in graphics_track["clips"]] == [ref_to(gfx)]  # type: ignore[index]
    assert gfx.args["scene"]["size"] == [1080, 1920]  # type: ignore[index]


def test_subeler_ids_survive_blank_lines_and_comments(tree: Path, eks_registry: Registry) -> None:
    before = plan_example(tree, "subeler")
    path = tree / EXAMPLES["subeler"][0]
    lines = path.read_text(encoding="utf-8").splitlines()
    edited: list[str] = []
    for line in lines:
        edited.append(line)
        if line.strip().startswith(("s.", "branches", "kadikoy", "expect")):
            edited.extend(["", "    # a comment between steps", ""])
    path.write_text("# leading comment\n\n" + "\n".join(edited) + "\n", encoding="utf-8")
    after = plan_example(tree, "subeler")
    assert [node.src for node in after.nodes] != [node.src for node in before.nodes]
    assert [node.id for node, _ancestors in after.walk()] == [node.id for node, _ancestors in before.walk()]
    assert [node.hash for node, _ancestors in after.walk()] == [node.hash for node, _ancestors in before.walk()]
    assert after.hash == before.hash
    assert after.outputs == before.outputs
    validated(after, tree)


@pytest.mark.parametrize(
    ("platform", "app", "message"),
    [
        ("web", "mobile-app", r"take0: app 'mobile-app' runs on \['ios', 'android'\]; platform 'web' is not one of them"),
        ("ios", "web-app", r"take0: app 'web-app' runs on \['web'\]; platform 'ios' is not one of them"),
    ],
)
def test_a_take_must_run_on_a_platform_of_its_app(
    tree: Path, eks_registry: Registry, platform: str, app: str, message: str
) -> None:
    # a take is a flow session: the app profile of the tree decides where it can run
    score = tree / ".harness" / "scores" / "mismatch.py"
    score.write_text(
        "import eks\n\n\n"
        "@eks.score(fps=30, bpm=120)\n"
        "def mismatch(sc: eks.Score):\n"
        f"    take = sc.take(platform={platform!r}, app={app!r})\n"
        "    with take.flow() as s:\n"
        "        s.ui.tap(\"#menu\")\n",
        encoding="utf-8",
    )
    with pytest.raises(TraceError, match=message):
        plan_spec(".harness/scores/mismatch.py", cwd=tree, plugins=True)


def test_a_take_of_an_unprofiled_app_keeps_its_platform(tree: Path, eks_registry: Registry) -> None:
    # without an app profile there is nothing to check against, as for flows
    score = tree / ".harness" / "scores" / "unprofiled.py"
    score.write_text(
        "import eks\n\n\n"
        "@eks.score(fps=30, bpm=120)\n"
        "def unprofiled(sc: eks.Score):\n"
        "    sc.take(platform='ios', app='no-such-app')\n",
        encoding="utf-8",
    )
    plan = plan_spec(".harness/scores/unprofiled.py", cwd=tree, plugins=True)
    assert [node.args["session"]["platform"] for node in nodes_of(plan, "score.take")] == ["ios"]  # type: ignore[index]
