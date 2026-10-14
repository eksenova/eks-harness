from __future__ import annotations

import json
import posixpath
import shutil
from pathlib import Path
from typing import Any

import pytest

import eks
from eks import t
from eks.workspace import plan_spec
from eks_harness.engine.validator import validate
from eks_harness.ir.catalog.markers import BeatBackend
from eks_harness.ir.hashing import cache_inputs, hash_plan, machine_dependent
from eks_harness.ir.plan import Node, Plan, Requirement
from eks_harness.ir.registry import Registry, contributing

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = ROOT / "examples" / "dsl"
RENDER_OPS = ("blender.render", "gfx.render", "audio.mix", "edit.render")
WINDOW = 'window=t.span(t("0s"), t("15s"))'


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def one(plan: Plan, op: str) -> Node:
    (node,) = nodes_of(plan, op)
    return node


def copy_examples(tmp_path: Path) -> Path:
    root = tmp_path / "dsl"
    shutil.copytree(EXAMPLES_DIR, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def ad_with(root: Path, clock: str) -> Plan:
    score = root / ".harness" / "scores" / "ad.py"
    score.write_text((EXAMPLES_DIR / ".harness" / "scores" / "ad.py").read_text().replace(WINDOW, clock))
    return plan_spec(".harness/scores/ad.py", cwd=root, plugins=True)


# the clock bounds (window and duration) are part of every render that reads them


def test_moving_the_score_window_changes_every_render_and_take_hash(tmp_path: Path, eks_registry: Registry) -> None:
    root = copy_examples(tmp_path)
    first = ad_with(root, 'window=t.span(t("0s"), t("15s"))')
    second = ad_with(root, 'window=t.span(t("40s"), t("55s"))')
    assert eks.validate(first, tree=root).ok and eks.validate(second, tree=root).ok
    for op in (*RENDER_OPS, "score.take"):
        assert one(first, op).hash != one(second, op).hash, op
    assert [node.hash for node in nodes_of(first, "media.source")] == [
        node.hash for node in nodes_of(second, "media.source")]
    assert one(first, "media.markers").hash == one(second, "media.markers").hash


def test_score_duration_changes_scene_renders_without_their_own_duration(tmp_path: Path,
                                                                          eks_registry: Registry) -> None:
    root = copy_examples(tmp_path)
    short = ad_with(root, 'duration=t("15s")')
    long = ad_with(root, 'duration=t("40s")')
    for op in RENDER_OPS:
        assert one(short, op).hash != one(long, op).hash, op


def test_edit_window_changes_the_edit_render_hash(eks_registry: Registry) -> None:
    def build(start: str, end: str) -> Plan:
        @eks.edit(name="cut", fps=30, size=(1080, 1920), window=t.span(t(start), t(end)))
        def cut(e: eks.Edit) -> None:
            e.render_settings(encoder="libx264")
            e.track("main").clip(e.media("artifact:VID123@sha256:abababababababababababababababababababababababababababababababab"), src=t.span(t("0s"), t("20s")))

        return eks.plan(cut)

    early, late = build("0s", "5s"), build("10s", "15s")
    assert one(early, "edit.render").hash != one(late, "edit.render").hash
    assert cache_inputs(early, one(early, "edit.render")) == ()
    assert one(early, "media.source").hash == one(late, "media.source").hash


def test_a_musical_window_puts_the_tempo_into_the_render_hash(eks_registry: Registry) -> None:
    def build(bpm: int) -> Plan:
        @eks.score(name="bars", fps=30, bpm=bpm, window=t.span(t("bar:0"), t("bar:8")))
        def bars(sc: eks.Score) -> None:
            sc.edit.track("main").clip(sc.edit.media("artifact:VID123"))

        return eks.plan(bars)

    fast, slow = build(120), build(60)
    assert one(fast, "edit.render").hash != one(slow, "edit.render").hash
    assert one(fast, "media.source").hash == one(slow, "media.source").hash


# times inside JSON values (gfx and Blender assign, closures) are read like typed ones


def assigned_scene(name: str, bpm: int | None, keys: dict[str, float]) -> Any:
    options: dict[str, Any] = {"name": name, "kind": "gfx", "size": (1080, 1920), "duration": t("8s"), "seed": 1}
    if bpm is not None:
        options["bpm"] = bpm

    @eks.scene(**options)
    def scene(sc: eks.GfxScene) -> None:
        title = sc.text("EksenGOLD", id="title")
        sc.on(t("0s"), title.assign("y", eks.keys({t(key): value for key, value in keys.items()})))

    return scene


def test_assigned_beat_keys_move_the_gfx_render_hash_with_the_tempo(eks_registry: Registry) -> None:
    fast = eks.plan(assigned_scene("probe120", 120, {"beat:2": 0.0, "beat:6": 300.0}))
    slow = eks.plan(assigned_scene("probe60", 60, {"beat:2": 0.0, "beat:6": 300.0}))
    assert one(fast, "gfx.render").hash != one(slow, "gfx.render").hash


def test_assigned_cue_keys_move_the_embedded_render_hash_with_the_cue(eks_registry: Registry) -> None:
    scene = assigned_scene("cued", None, {"cue:hit": 0.0, "cue:hit+1b": 300.0})

    def build(at: str) -> Plan:
        @eks.score(name=f"cued_{at.replace('s', '')}", fps=30, bpm=120, duration=t("8s"))
        def cued(sc: eks.Score) -> None:
            sc.cue("hit", t(at))
            sc.edit.track("graphics").clip(sc.gfx(scene))

        return eks.plan(cued)

    early, late = build("2s"), build("3s")
    assert validate(early).ok
    assert one(early, "gfx.render").hash != one(late, "gfx.render").hash


def test_an_assigned_beat_time_without_a_tempo_is_unresolvable(eks_registry: Registry) -> None:
    scene = assigned_scene("untimed", None, {"beat:2": 0.0, "beat:6": 300.0})

    @eks.edit(fps=30, size=(1080, 1920))
    def with_assigned(e: eks.Edit) -> None:
        e.track("g").clip(e.gfx(scene))

    report = validate(eks.plan(with_assigned))
    assert [(issue.code, issue.field) for issue in report.issues] == [
        ("time.unresolvable", "scene.triggers.0.action.value.keys.0.t"),
        ("time.unresolvable", "scene.triggers.0.action.value.keys.1.t"),
    ]


def test_a_closure_time_without_a_tempo_is_unresolvable(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web", now="2026-01-15T09:00+03:00", timezone="Europe/Istanbul",
              locale="tr-TR")
    def waits(s: eks.Session) -> Any:
        pause = t("2b")

        @eks.dynamic
        def per_row(n=s.ui.count("#row")):  # noqa: B008
            return [s.ui.tap("#row", timeout=pause) for _ in range(n)]

        return per_row

    plan = eks.plan(waits)
    issues = [(issue.code, issue.field) for issue in validate(plan).issues if issue.code == "time.unresolvable"]
    assert issues == [("time.unresolvable", "fn.freevars.pause.value")]


# resource kind versions are part of the data.create hashes and so of the seed snapshot key


def test_bumping_a_resource_kind_version_changes_the_seed_plan_hash(eks_registry: Registry) -> None:
    def define(version: str) -> None:
        @eks.resource("shop", version=version)
        class Shop(eks.Resource):
            name: str

            def create(self, ctx: Any) -> None:
                return None

    @eks.seed("shops", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=1)
    def shops(d: Any) -> None:
        d.shop("harman", name="Harman")

    define("1")
    first = eks.plan(shops)
    define("2")
    second = eks.plan(shops)
    assert one(first, "data.create").hash != one(second, "data.create").hash
    assert first.hash != second.hash


# a beat grid chosen per machine joins the cache key


def beats_plan(backend: BeatBackend) -> Plan:
    @eks.scene(name=f"pulse_{backend}", kind="gfx", size=(1080, 1920), cpu=True)
    def pulse(sc: eks.GfxScene) -> None:
        title = sc.text("EksenGOLD", id="title")
        sc.on(t.beats("downbeat")[0:8], title.pulse(amount=1.06))

    @eks.edit(name=f"beats_{backend}", fps=30, size=(1080, 1920), duration=t("8s"))
    def beats(e: eks.Edit) -> None:
        e.render_settings(encoder="libx264")
        e.audio.track("artifact:SONG123@sha256:abababababababababababababababababababababababababababababababab", name="song").beats("beat", "downbeat", backend=backend)
        e.track("graphics").clip(e.gfx(pulse))

    return eks.plan(beats)


def test_an_auto_beat_backend_puts_the_markers_into_the_cache_inputs(eks_registry: Registry) -> None:
    plan = beats_plan("auto")
    markers = one(plan, "media.markers")
    assert machine_dependent(markers)
    for op in ("media.markers", "gfx.render", "edit.render"):
        assert cache_inputs(plan, one(plan, op)) == (markers.id,), op
    assert cache_inputs(plan, one(plan, "audio.mix")) == ()


def test_a_pinned_beat_backend_stays_cacheable_by_node_hash(eks_registry: Registry) -> None:
    plan = beats_plan("madmom")
    assert not machine_dependent(one(plan, "media.markers"))
    for op in ("media.markers", "gfx.render", "edit.render", "audio.mix"):
        assert cache_inputs(plan, one(plan, op)) == (), op


# the platform's default checks are resolved into the plan


def register_contrast(version: str) -> None:
    with contributing("acme.checks", version):
        @eks.check("contrast_ratio", platforms=["web"], default=True, version="1",
                   description="Fails when text contrast is below the minimum")
        class ContrastRatio(eks.Args):
            minimum: float = 4.5


def test_plugin_default_checks_are_pinned_by_the_plan(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web", now="2026-01-15T09:00+03:00", timezone="Europe/Istanbul",
              locale="tr-TR")
    def checked(s: eks.Session) -> None:
        s.ui.nav("/tenant/settings")
        s.checkpoint("settings")

    register_contrast("1.0")
    plan = eks.plan(checked)
    assert plan.session is not None
    assert "contrast_ratio" in [use.kind for use in plan.session.defaults]
    assert list(plan.requires) == [Requirement(plugin="acme.checks", version="1.0")]
    assert validate(plan).ok
    register_contrast("2.0")
    assert eks.plan(checked).hash != plan.hash
    eks_registry.drop_module(__name__, plugin="acme.checks")
    codes = [issue.code for issue in validate(plan).issues]
    assert "plugin.missing" in codes


def test_own_session_checks_replace_the_default_of_the_same_kind(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web", checks=[eks.checks.use("console_errors", noise=["favicon"])])
    def own(s: eks.Session) -> None:
        s.checkpoint("home")

    session = eks.plan(own).session
    assert session is not None
    assert [use.kind for use in session.checks] == ["console_errors"]
    assert "console_errors" not in [use.kind for use in session.defaults]
    assert "layout_spill" in [use.kind for use in session.defaults]


# a node drives only the lease and backend of the session that governs it


def two_takes() -> Plan:
    @eks.score(name="two_takes", fps=30, bpm=120, duration=t("12s"))
    def takes(sc: eks.Score) -> None:
        first = sc.take(platform="ios", app="mobile-app", seed="standard", as_="harman-admin",
                        timezone="Europe/Istanbul", locale="tr-TR", now="2026-01-15T09:00+03:00")
        second = sc.take(platform="android", app="mobile-app", seed="standard", as_="harman-admin",
                         timezone="Europe/Istanbul", locale="tr-TR", now="2026-01-15T09:00+03:00")
        with first.flow() as s:
            s.ui.tap("#new-invoice", at=t("1s"))
        with second.flow() as s:
            s.ui.tap("#total", at=t("2s"))
        sc.edit.track("a").clip(first)
        sc.edit.track("b").clip(second)

    return eks.plan(takes)


def rehashed(data: dict[str, Any]) -> Plan:
    return hash_plan(Plan.model_validate(data))


def session_uses(plan: Plan) -> list[str | None]:
    return [issue.node for issue in validate(plan).issues if issue.code == "session.uses"]


def test_a_node_in_one_take_driving_another_takes_lease_is_an_error(eks_registry: Registry) -> None:
    plan = two_takes()
    assert session_uses(plan) == []
    data = json.loads(plan.to_json())
    first, second = [node for node in data["nodes"] if node["op"] == "score.take"]
    tap = next(node for node in first["branches"]["body"] if node["op"] == "ui.tap")
    tap["uses"] = [second["uses"][0]]
    assert session_uses(rehashed(data)) == [tap["id"]]


def test_a_lease_op_outside_every_take_is_an_error(eks_registry: Registry) -> None:
    data = json.loads(two_takes().to_json())
    first = next(node for node in data["nodes"] if node["op"] == "score.take")
    stray = json.loads(json.dumps(next(node for node in first["branches"]["body"] if node["op"] == "ui.tap")))
    stray["id"] = "nstray00001"
    stray.pop("pin", None)
    stray["after"] = [first["id"]]
    data["nodes"].insert(data["nodes"].index(first) + 1, stray)
    assert session_uses(rehashed(data)) == ["nstray00001"]


def test_a_take_using_another_takes_resources_is_an_error(eks_registry: Registry) -> None:
    data = json.loads(two_takes().to_json())
    first, second = [node for node in data["nodes"] if node["op"] == "score.take"]
    first["uses"] = list(second["uses"])
    assert first["id"] in session_uses(rehashed(data))


# relative media paths resolve against the file whose code wrote them


def tree_path(plan: Plan, node: Node) -> str:
    uri = node.args["uri"]
    assert isinstance(uri, str)
    return posixpath.normpath(posixpath.join(plan.base, uri.removeprefix("file:")))


def test_an_embedded_scene_reads_its_files_next_to_its_own_source(tmp_path: Path, eks_registry: Registry) -> None:
    root = copy_examples(tmp_path)
    standalone = plan_spec(".harness/scenes/phone.py", cwd=root, plugins=True, params={"screen": "artifact:VID123"})
    embedded = plan_spec(".harness/scores/ad.py", cwd=root, plugins=True)

    def blend(plan: Plan) -> Node:
        return next(node for node in nodes_of(plan, "media.source") if node.args.get("kind") == "file")

    assert standalone.base == ".harness/scenes" and embedded.base == ".harness/scores"
    assert blend(standalone).args["uri"] == "file:assets/phone.blend"
    assert blend(embedded).args["uri"] == "file:../scenes/assets/phone.blend"
    assert tree_path(standalone, blend(standalone)) == tree_path(embedded, blend(embedded)) == (
        ".harness/scenes/assets/phone.blend")
    song = next(node for node in nodes_of(embedded, "media.source") if node.args.get("kind") is None)
    assert song.args["uri"] == "file:music/song.ogg"


def write_tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_one_file_named_from_two_directories_is_one_source(tmp_path: Path, eks_registry: Registry) -> None:
    root = tmp_path / "tree"
    write_tree(root, {
        ".harness/project.toml": '[project]\nid = "paths"\n',
        ".harness/scenes/card.py": (
            "import eks\n\n\n"
            "@eks.scene(kind='gfx', size=(1080, 1920))\n"
            "def card(sc: eks.GfxScene):\n"
            "    sc.image('assets/logo.png', id='logo')\n"
        ),
        ".harness/scenes/page.py": (
            "import eks\n\n\n"
            "@eks.scene(kind='web', entry='web/index.html', size=(1080, 1920))\n"
            "def page(sc: eks.WebScene, logo: eks.Media = 'assets/logo.png'):\n"
            "    sc.texture('logo', logo)\n"
        ),
        ".harness/scores/promo.py": (
            "import eks\n"
            "from eks import t\n\n\n"
            "@eks.score(fps=30, bpm=120, duration=t('8s'))\n"
            "def promo(sc: eks.Score):\n"
            "    sc.edit.track('logo').clip(sc.edit.media('../scenes/assets/logo.png'))\n"
            "    sc.edit.track('card').clip(sc.gfx('card'))\n"
            "    sc.edit.track('page').clip(sc.scene('page'))\n"
        ),
    })
    for name in ("card", "page"):
        plan_spec(f".harness/scenes/{name}.py", cwd=root, plugins=True)
    plan = plan_spec(".harness/scores/promo.py", cwd=root, plugins=True)
    sources = {str(node.args["uri"]): node for node in nodes_of(plan, "media.source")}
    assert sorted(sources) == ["file:../scenes/assets/logo.png", "file:../scenes/web/index.html"]
    assert {tree_path(plan, node) for node in sources.values()} == {
        ".harness/scenes/assets/logo.png", ".harness/scenes/web/index.html"}


@pytest.mark.parametrize("uri", ["/abs/clip.mov", "artifact:VID123", "https://example.com/a.mp4"])
def test_absolute_and_remote_media_are_not_rebased(uri: str, eks_registry: Registry) -> None:
    @eks.edit(name="remote", fps=30, size=(1080, 1920))
    def remote(e: eks.Edit) -> None:
        e.track("main").clip(e.media(uri))

    node = one(eks.plan(remote), "media.source")
    assert node.args["uri"] == (uri if not uri.startswith("/") else f"file:{uri}")
