from collections.abc import Callable
from typing import Any

import pytest

import eks
from eks import t
from eks.planning import trace
from eks.scenes import embed_scene, scene_event
from eks.tracing import TraceContext, TraceError, current
from eks.webscene import WebSceneBuilder, open_web_scene
from eks_harness.ir.hashing import canonical_json, derive_id, derived_seed
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry


def define_logo() -> Callable[..., Any]:
    # a gfx scene whose render emits the event the cards listen to
    @eks.scene(kind="gfx")
    def logo(sc: eks.GfxScene) -> None:
        sc.text("EksenGOLD")
        sc.emits("logo.done")

    return logo


def define_cards() -> Callable[..., Any]:
    logo = define_logo()

    @eks.scene(kind="web", entry="scenes/cards/index.html", size=(1080, 1350), fps=60, seed=11,
               duration=t("8s"), transparent=False)
    def cards(sc: eks.WebScene, cover: eks.Media, title: str = "Altın") -> None:
        logo_render = embed_scene(current(), logo, {})
        sc.prop("title", title)
        sc.prop("cards", [{"label": "Gram", "price": 2450.1}, {"label": "Çeyrek", "price": 4010}])
        sc.texture("cover", cover)
        sc.on(t.beats("downbeat")[0:4], emit="flip")
        sc.on(t("bar:4"), emit="reveal", data={"index": 2})
        sc.on(scene_event(logo_render, "logo.done"), emit="settle")
        sc.emits("cards.done", "card.flipped")

    return cards


def render_node(plan: Plan) -> Node:
    (node,) = [node for node in plan.nodes if node.op == "scene.render"]
    return node


def test_web_scene_render_args(eks_registry: Registry) -> None:
    plan = eks.plan(define_cards(), cover="artifact:COVER1")
    # the entry page is a media.source of kind bundle: the scene's directory is fingerprinted as a whole
    assert [node.op for node in plan.nodes] == ["media.source", "media.source", "gfx.render", "scene.render"]
    cover, entry, logo, render = plan.nodes
    assert entry.args == {"uri": "file:scenes/cards/index.html", "kind": "bundle"}
    assert render.id == derive_id("n", "scene:cards/final:scene.render#0")
    # the event trigger is a node ref: an edge to the logo render and part of the Merkle hash
    assert sorted(render.after) == sorted([cover.id, entry.id, logo.id])
    assert render.out == "media"
    assert render.args == {"scene": {
        "entry": {"$ref": entry.id, "path": []},
        "size": [1080, 1350],
        "fps": 60,
        "duration": "8s",
        "transparent": False,
        "seed": 11,
        "props": {
            "title": "Altın",
            "cards": [{"label": "Gram", "price": 2450.1}, {"label": "Çeyrek", "price": 4010}],
        },
        "textures": {"cover": {"$ref": cover.id, "path": []}},
        "triggers": [
            {"at": {"stream": "downbeat", "start": 0, "stop": 4, "step": 1, "times": [], "offset": "0s"},
             "emit": "flip"},
            {"at": "bar:4", "emit": "reveal", "data": {"index": 2}},
            {"at": {"source": {"$ref": logo.id, "path": []}, "name": "logo.done", "where": {}}, "emit": "settle"},
        ],
        "events": ["cards.done", "card.flipped"],
    }}


def test_web_scene_params_and_determinism(eks_registry: Registry) -> None:
    cards = define_cards()
    first = eks.plan(cards, cover="artifact:COVER1", title="Gümüş")
    second = eks.plan(cards, cover="artifact:COVER1", title="Gümüş")
    assert render_node(first).args["scene"]["props"]["title"] == "Gümüş"  # type: ignore[index]
    assert canonical_json(first.model_dump(mode="json")) == canonical_json(second.model_dump(mode="json"))
    assert first.clock.tempo is not None
    assert first.clock.tempo.external is True


def test_props_from_refs_are_encoded(eks_registry: Registry) -> None:
    @eks.scene(kind="web", entry="scenes/x/index.html")
    def x(sc: eks.WebScene, cover: eks.Media) -> None:
        sc.prop("cover_uri", f"{cover.uri}?v=1").prop("width", cover.width)

    plan = eks.plan(x, cover="artifact:COVER1")
    props = render_node(plan).args["scene"]["props"]  # type: ignore[index]
    source = plan.nodes[0].id
    assert props == {
        "cover_uri": {"$fmt": [{"$ref": source, "path": ["uri"]}, "?v=1"]},
        "width": {"$ref": source, "path": ["width"]},
    }


def test_texture_from_a_string_uses_media_source(eks_registry: Registry) -> None:
    @eks.scene(kind="web", entry="scenes/x/index.html")
    def x(sc: eks.WebScene) -> None:
        sc.texture("bg", "artifact:BG1").texture("fg", "artifact:BG1")

    plan = eks.plan(x)
    assert [node.op for node in plan.nodes] == ["media.source", "media.source", "scene.render"]
    textures = render_node(plan).args["scene"]["textures"]  # type: ignore[index]
    assert textures == {"bg": {"$ref": plan.nodes[1].id, "path": []}, "fg": {"$ref": plan.nodes[1].id, "path": []}}


def test_defaults(eks_registry: Registry) -> None:
    @eks.scene(kind="web", entry="scenes/x/index.html")
    def x(sc: eks.WebScene) -> None:
        return None

    plan = eks.plan(x)
    scene = render_node(plan).args["scene"]
    assert scene == {"entry": {"$ref": plan.nodes[0].id, "path": []}, "size": [1080, 1920], "transparent": True,
                     "seed": derived_seed(0, "scene:x/seed") % 2**31,
                     "props": {}, "textures": {}, "triggers": [], "events": []}


def test_missing_entry_raises(eks_registry: Registry) -> None:
    @eks.scene(kind="web")
    def entryless(sc: eks.WebScene) -> None:
        return None

    with pytest.raises(TraceError, match=r"has no entry page.*scenes/entryless/index.html"):
        eks.plan(entryless)


def test_blank_entry_raises(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="scene", name="x", registry=eks_registry)
    with pytest.raises(TraceError, match="needs its entry page"):
        WebSceneBuilder(ctx, entry="  ", size=(10, 10))


def test_duplicate_props_and_textures_raise(eks_registry: Registry) -> None:
    @eks.scene(kind="web", entry="scenes/x/index.html")
    def twice(sc: eks.WebScene) -> None:
        sc.prop("a", 1)
        sc.prop("a", 2)

    with pytest.raises(TraceError, match="prop 'a' is set twice"):
        eks.plan(twice)

    @eks.scene(kind="web", entry="scenes/y/index.html")
    def unnamed(sc: eks.WebScene) -> None:
        sc.texture("", "artifact:BG1")

    with pytest.raises(TypeError, match="non-empty name"):
        eks.plan(unnamed)


def test_trigger_and_event_errors(eks_registry: Registry) -> None:
    @eks.scene(kind="web", entry="scenes/x/index.html")
    def bad_trigger(sc: eks.WebScene) -> None:
        sc.on("bar:1", emit="flip")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="pass t\\('bar:1'\\)"):
        eks.plan(bad_trigger)

    @eks.scene(kind="web", entry="scenes/y/index.html")
    def bad_emit(sc: eks.WebScene) -> None:
        sc.on(t("1s"), emit="two words")

    with pytest.raises(ValueError, match="not an event name"):
        eks.plan(bad_emit)

    @eks.scene(kind="web", entry="scenes/z/index.html")
    def bad_number(sc: eks.WebScene) -> None:
        sc.on(1.5, emit="flip")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="expects a Time"):
        eks.plan(bad_number)


def test_builder_is_closed_after_render(eks_registry: Registry) -> None:
    kept: list[WebSceneBuilder] = []

    @eks.scene(kind="web", entry="scenes/x/index.html")
    def x(sc: eks.WebScene) -> None:
        kept.append(sc)

    eks.plan(x)
    with pytest.raises(TraceError, match="already rendered"):
        kept[0].prop("late", 1)


def test_wrong_kind_for_the_web_builder(eks_registry: Registry) -> None:
    @eks.scene(kind="gfx")
    def flat(sc: eks.GfxScene) -> None:
        return None

    with pytest.raises(TraceError, match="is a gfx scene, not a web scene"):
        trace(eks_registry.entry_for(flat), root=open_web_scene)

    @eks.edit(fps=30, size=(1080, 1920))
    def cut(e: eks.Edit) -> None:
        return None

    with pytest.raises(TraceError, match="is not a scene"):
        trace(eks_registry.entry_for(cut), root=open_web_scene)
