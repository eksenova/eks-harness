from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

import eks
import eks.media
from eks_harness.ir.entries import SceneOptions
from eks_harness.ir.hashing import derived_seed
from eks_harness.ir.ops.blender import BlenderRender
from eks_harness.ir.ops.gfx import GfxScene
from eks_harness.ir.ops.scene import SCENE_SEED_MAX, WebScene
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry, RegistryError

RENDER_OPS = ("gfx.render", "scene.render", "blender.render")


def define_scenes() -> None:
    @eks.scene(kind="gfx", size=(720, 1280))
    def titles(sc: eks.GfxScene) -> None:
        sc.text("EksenGOLD", id="title")

    @eks.scene(kind="gfx", size=(720, 1280))
    def badge(sc: eks.GfxScene) -> None:
        sc.text("Anlık kur", id="badge")

    @eks.scene(kind="web", entry="scenes/cards/index.html")
    def cards(sc: eks.WebScene) -> None:
        sc.prop("title", "Altın")

    @eks.scene(kind="blender", engine="cycles", samples=16)
    def phone(sc: eks.BlenderScene, screen: eks.Media) -> None:
        p = sc.load("assets/phone.blend", obj="Phone")
        p.material("Screen").texture("screen", screen)


def define_score(name: str, seed: int) -> Callable[..., Any]:
    @eks.score(name, fps=30, seed=seed)
    def ad(sc: eks.Score) -> dict[str, Any]:
        return {
            "titles": sc.gfx("titles"),
            "badge": sc.gfx("badge"),
            "cards": sc.scene("cards"),
            "phone": sc.scene("phone", screen=eks.media.source("artifact:SCREEN1")),
        }

    return ad


def renders(plan: Plan) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op in RENDER_OPS]


def seed_of(node: Node) -> int:
    scene: dict[str, Any] = node.args["scene"]  # type: ignore[assignment]
    if node.op == "blender.render":
        return scene["render"]["seed"]
    return scene["seed"]


def seeds(plan: Plan) -> list[tuple[str, int]]:
    return [(node.op, seed_of(node)) for node in renders(plan)]


def expected(root: int, key: str) -> int:
    return derived_seed(root, f"{key}/seed") % (SCENE_SEED_MAX + 1)


def test_score_root_seed_changes_every_embedded_scene_seed(eks_registry: Registry) -> None:
    define_scenes()
    five = eks.plan(define_score("ad5", 5))
    six = eks.plan(define_score("ad6", 6))
    keys = ["scene:titles#0", "scene:badge#0", "scene:cards#0", "scene:phone#0"]
    assert seeds(five) == [
        ("gfx.render", expected(5, keys[0])),
        ("gfx.render", expected(5, keys[1])),
        ("scene.render", expected(5, keys[2])),
        ("blender.render", expected(5, keys[3])),
    ]
    assert seeds(six) == [
        ("gfx.render", expected(6, keys[0])),
        ("gfx.render", expected(6, keys[1])),
        ("scene.render", expected(6, keys[2])),
        ("blender.render", expected(6, keys[3])),
    ]
    for (op, seed_five), (_op, seed_six) in zip(seeds(five), seeds(six), strict=True):
        assert seed_five != seed_six, op
        assert seed_five != 0
        assert seed_six != 0
    assert [node.hash for node in renders(five)] != [node.hash for node in renders(six)]


def test_scene_seeds_are_not_the_old_literal_default(eks_registry: Registry) -> None:
    define_scenes()
    plan = eks.plan(define_score("ad", 5))
    assert all(seed != 0 for _op, seed in seeds(plan))
    assert len({seed for _op, seed in seeds(plan)}) == 4


def test_two_embeddings_of_one_scene_get_different_seeds(eks_registry: Registry) -> None:
    define_scenes()

    @eks.score(fps=30, seed=5)
    def twice(sc: eks.Score) -> dict[str, Any]:
        return {
            "gfx": [sc.gfx("titles"), sc.gfx("titles")],
            "web": [sc.scene("cards"), sc.scene("cards")],
            "blender": [sc.scene("phone", screen=eks.media.source("artifact:SCREEN1")),
                        sc.scene("phone", screen=eks.media.source("artifact:SCREEN1"))],
        }

    plan = eks.plan(twice)
    by_op: dict[str, list[int]] = {}
    for op, seed in seeds(plan):
        by_op.setdefault(op, []).append(seed)
    assert by_op == {
        "gfx.render": [expected(5, "scene:titles#0"), expected(5, "scene:titles#1")],
        "scene.render": [expected(5, "scene:cards#0"), expected(5, "scene:cards#1")],
        "blender.render": [expected(5, "scene:phone#0"), expected(5, "scene:phone#1")],
    }
    for op, values in by_op.items():
        assert values[0] != values[1], op


def test_explicit_scene_seed_is_kept_in_every_embedding(eks_registry: Registry) -> None:
    @eks.scene(kind="gfx", seed=7)
    def titles(sc: eks.GfxScene) -> None:
        sc.text("EksenGOLD", id="title")

    @eks.scene(kind="web", entry="scenes/cards/index.html", seed=7)
    def cards(sc: eks.WebScene) -> None:
        sc.prop("title", "Altın")

    @eks.scene(kind="blender", engine="cycles", samples=16, seed=7)
    def phone(sc: eks.BlenderScene, screen: eks.Media) -> None:
        sc.load("assets/phone.blend", obj="Phone").material("Screen").texture("screen", screen)

    def score(name: str, seed: int) -> Callable[..., Any]:
        @eks.score(name, fps=30, seed=seed)
        def ad(sc: eks.Score) -> list[Any]:
            screen = eks.media.source("artifact:SCREEN1")
            return [sc.gfx("titles"), sc.gfx("titles"), sc.scene("cards"), sc.scene("cards"),
                    sc.scene("phone", screen=screen), sc.scene("phone", screen=screen)]

        return ad

    for plan in (eks.plan(score("ad5", 5)), eks.plan(score("ad6", 6))):
        assert [seed for _op, seed in seeds(plan)] == [7] * 6
    for scene in (titles, cards):
        assert [seed for _op, seed in seeds(eks.plan(scene))] == [7]
    assert [seed for _op, seed in seeds(eks.plan(phone, screen="artifact:SCREEN1"))] == [7]


def test_explicit_zero_seed_is_kept(eks_registry: Registry) -> None:
    @eks.scene(kind="gfx", seed=0)
    def titles(sc: eks.GfxScene) -> None:
        sc.text("EksenGOLD", id="title")

    @eks.score(fps=30, seed=5)
    def ad(sc: eks.Score) -> Any:
        return sc.gfx("titles")

    assert seeds(eks.plan(ad)) == [("gfx.render", 0)]
    assert seeds(eks.plan(titles)) == [("gfx.render", 0)]


def test_standalone_scene_plan_is_deterministic(eks_registry: Registry) -> None:
    define_scenes()
    first = {name: eks.plan(eks_registry.entry("scene", name)) for name in ("titles", "cards")}
    second = {name: eks.plan(eks_registry.entry("scene", name)) for name in ("titles", "cards")}
    for name in ("titles", "cards"):
        assert seeds(first[name]) == seeds(second[name])
        assert [node.hash for node in renders(first[name])] == [node.hash for node in renders(second[name])]
        assert first[name].hash == second[name].hash
    assert seeds(first["titles"]) == [("gfx.render", expected(0, "scene:titles"))]
    assert seeds(first["cards"]) == [("scene.render", expected(0, "scene:cards"))]


def test_standalone_blender_scene_seed_derives_from_its_name(eks_registry: Registry) -> None:
    @eks.scene(kind="blender", engine="cycles", samples=16)
    def turntable(sc: eks.BlenderScene) -> None:
        sc.load("assets/phone.blend", obj="Phone")

    plan = eks.plan(turntable)
    assert seeds(plan) == [("blender.render", expected(0, "scene:turntable"))]
    assert seeds(eks.plan(turntable)) == seeds(plan)


def test_explicit_seed_is_tracked_on_scene_options(eks_registry: Registry) -> None:
    @eks.scene(kind="gfx")
    def derived(sc: eks.GfxScene) -> None:
        return None

    @eks.scene(kind="gfx", seed=0)
    def pinned(sc: eks.GfxScene) -> None:
        return None

    derived_options = eks_registry.entry("scene", "derived").options
    pinned_options = eks_registry.entry("scene", "pinned").options
    assert isinstance(derived_options, SceneOptions)
    assert isinstance(pinned_options, SceneOptions)
    assert (derived_options.seed, derived_options.explicit_seed) == (0, None)
    assert (pinned_options.seed, pinned_options.explicit_seed) == (0, 0)


@pytest.mark.parametrize("seed", [-1, SCENE_SEED_MAX + 1])
def test_scene_seed_out_of_range_is_rejected(eks_registry: Registry, seed: int) -> None:
    with pytest.raises(RegistryError, match="seed"):
        @eks.scene(kind="gfx", seed=seed)
        def titles(sc: eks.GfxScene) -> None:
            return None


@pytest.mark.parametrize("seed", [-1, SCENE_SEED_MAX + 1])
def test_ir_scene_seeds_stay_in_the_cycles_range(seed: int) -> None:
    with pytest.raises(ValidationError):
        GfxScene.model_validate({"size": [10, 10], "seed": seed})
    with pytest.raises(ValidationError):
        WebScene.model_validate({"entry": {"$ref": "n0000000001", "path": []}, "size": [10, 10], "seed": seed})
    with pytest.raises(ValidationError):
        BlenderRender.model_validate(
            {"engine": "CYCLES", "samples": 1, "device": "GPU", "seed": seed, "size": [10, 10]})
    assert BlenderRender.model_validate(
        {"engine": "CYCLES", "samples": 1, "device": "GPU", "seed": SCENE_SEED_MAX, "size": [10, 10]}).seed == SCENE_SEED_MAX
