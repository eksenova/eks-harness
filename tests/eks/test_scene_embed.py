from collections.abc import Callable
from typing import Any

import pytest

import eks
import eks.media
from eks import t
from eks.planning import PlanBuildError, trace
from eks.ref import Media, node_of
from eks.scenes import embed_scene, open_scene, scene_event
from eks.tracing import TraceContext, TraceError, current
from eks_harness.ir.entries import SceneKind
from eks_harness.ir.hashing import canonical_json, derive_id
from eks_harness.ir.ops.scene import EventRef
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import EntrySpec, Registry, default_registry


def define_phone() -> Callable[..., Any]:
    @eks.scene(kind="blender", engine="cycles", samples=32, seed=7, bpm=120)
    def phone(sc: eks.BlenderScene, screen: eks.Media):
        p = sc.load("assets/phone.blend", obj="Phone")
        p.material("Screen").texture("screen", screen)
        cam = sc.camera(lens=50).look_at(p)
        cam.animate("location", {t("bar:0"): (0, -4, 1), t("bar:4"): (0, -2, 1)}, ease="out_cubic")
        sc.on(t.beats("downbeat")[0:8], p.pulse(scale=1.04))

        @sc.bpy
        def rig(bpy, ctx):
            return None

    return phone


def define_logo() -> Callable[..., Any]:
    # a Blender scene that declares the event other scenes listen to
    @eks.scene(kind="blender", engine="cycles", samples=16, seed=2)
    def logo(sc: eks.BlenderScene, screen: eks.Media):
        p = sc.load("assets/logo.blend", obj="Logo")
        p.material("Face").texture("base", screen)
        sc.emits("logo.done")

    return logo


def event_ref(source: str, name: str, **where: Any) -> EventRef:
    return EventRef.model_validate({"source": {"$ref": source}, "name": name, "where": where})


def no_root(ctx: TraceContext, entry: EntrySpec) -> None:
    return None


def plan_edit(body: Callable[[TraceContext], Any], *, fps: int = 30, size: tuple[int, int] = (720, 1280)) -> Plan:
    # an @eks.edit plan with a custom root, so only the scene embedding records nodes
    @eks.edit(fps=fps, size=size, seed=5)
    def cut(e: Any) -> Any:
        return body(current())

    return trace(default_registry().entry_for(cut), root=no_root)


def renders(plan: Plan, op: str = "blender.render") -> list[Node]:
    return [node for node in plan.nodes if node.op == op]


def scene_of(node: Node) -> dict[str, Any]:
    return node.args["scene"]  # type: ignore[return-value]


def test_embedding_the_phone_scene_twice(eks_registry: Registry) -> None:
    phone = define_phone()

    def body(ctx: TraceContext) -> Any:
        first_screen = eks.media.source("artifact:SCREEN1")
        second_screen = eks.media.source("artifact:SCREEN2")
        first = embed_scene(ctx, phone, {"screen": first_screen})
        second = embed_scene(ctx, phone, {"screen": second_screen})
        return {"first": first, "second": second}

    plan = plan_edit(body)
    sources = [node for node in plan.nodes if node.op == "media.source" and node.args.get("kind") is None]
    assert [node.args["uri"] for node in sources] == ["artifact:SCREEN1", "artifact:SCREEN2"]
    # both embeddings load the same .blend file through one media.source node
    (blend,) = [node for node in plan.nodes if node.op == "media.source" and node.args.get("kind") == "file"]
    first, second = renders(plan)
    assert first.id == derive_id("n", "edit:cut/scene:phone#0#0")
    assert second.id == derive_id("n", "edit:cut/scene:phone#1#0")
    assert first.id != second.id
    assert scene_of(first)["textures"][0]["media"] == {"$ref": sources[0].id, "path": []}
    assert scene_of(second)["textures"][0]["media"] == {"$ref": sources[1].id, "path": []}
    assert sorted(first.after) == sorted([sources[0].id, blend.id])
    assert sorted(second.after) == sorted([sources[1].id, blend.id])
    assert plan.outputs == {"first": {"$ref": first.id, "path": []}, "second": {"$ref": second.id, "path": []}}
    render = scene_of(first)["render"]
    assert (render["engine"], render["samples"], render["device"], render["seed"]) == ("CYCLES", 32, "GPU", 7)
    assert render["size"] == [720, 1280]
    assert render["fps"] == 30
    assert scene_of(first)["hooks"][0]["fn"]["source"] == "def rig(bpy, ctx):\n    return None\n"


def test_embedding_keeps_the_embedding_clock(eks_registry: Registry) -> None:
    phone = define_phone()
    plan = plan_edit(lambda ctx: embed_scene(ctx, phone, {"screen": eks.media.source("artifact:SCREEN1")}))
    assert plan.kind == "edit"
    assert plan.clock.fps == 30
    assert plan.clock.seed == 5
    assert plan.clock.tempo is None


def test_embedding_by_registered_name_with_a_media_string(eks_registry: Registry) -> None:
    define_phone()
    plan = plan_edit(lambda ctx: embed_scene(ctx, "phone", {"screen": "artifact:SCREEN3"}))
    (source,) = [node for node in plan.nodes if node.op == "media.source" and node.args.get("kind") != "file"]
    assert source.args == {"uri": "artifact:SCREEN3"}
    (render,) = renders(plan)
    assert render.id == derive_id("n", "edit:cut/scene:phone#0#0")
    assert scene_of(render)["textures"][0]["media"] == {"$ref": source.id, "path": []}


def test_embedding_by_entry_spec(eks_registry: Registry) -> None:
    entry = eks_registry.entry_for(define_phone())
    plan = plan_edit(lambda ctx: embed_scene(ctx, entry, {"screen": eks.media.source("artifact:SCREEN1")}))
    assert len(renders(plan)) == 1


class Take:
    # stands in for a score take: an Encodable whose encoding is a media ref
    def __init__(self, ref: Media) -> None:
        self.ref = ref

    def __eks_encode__(self) -> Any:
        return self.ref


def test_embedding_with_an_encodable_input(eks_registry: Registry) -> None:
    phone = define_phone()

    def body(ctx: TraceContext) -> Any:
        take = Take(eks.media.source("artifact:TAKE1"))
        return embed_scene(ctx, phone, {"screen": take})

    plan = plan_edit(body)
    (source,) = [node for node in plan.nodes if node.op == "media.source" and node.args.get("kind") != "file"]
    (render,) = renders(plan)
    assert scene_of(render)["textures"][0]["media"] == {"$ref": source.id, "path": []}


def test_optional_media_parameters_are_converted(eks_registry: Registry) -> None:
    @eks.scene(kind="gfx")
    def badge(sc: eks.GfxScene, logo: eks.Media | None = None, title: str = "EksenGOLD") -> None:
        sc.text(title)
        if logo is not None:
            sc.image(logo)

    plan = plan_edit(lambda ctx: embed_scene(ctx, badge, {"logo": "artifact:LOGO1", "title": "artifact:TITLE"}))
    (render,) = renders(plan, "gfx.render")
    text, image = scene_of(render)["layers"]
    assert text["text"] == "artifact:TITLE"
    (source,) = [node for node in plan.nodes if node.op == "media.source"]
    assert image["media"] == {"$ref": source.id, "path": []}


def test_kind_mismatch_raises(eks_registry: Registry) -> None:
    phone = define_phone()

    def body(ctx: TraceContext) -> Any:
        return embed_scene(ctx, phone, {"screen": eks.media.source("artifact:SCREEN1")}, kind="gfx")

    with pytest.raises(TraceError, match="is a blender scene, but a gfx scene is expected"):
        plan_edit(body)


@pytest.mark.parametrize("kind", ["blender", "web", "gfx"])
def test_kind_check_accepts_the_matching_kind(eks_registry: Registry, kind: SceneKind) -> None:
    options: dict[str, Any] = {"entry": "scenes/x/index.html"} if kind == "web" else {}

    @eks.scene(kind=kind, **options)
    def any_scene(sc: Any) -> None:
        return None

    op = {"blender": "blender.render", "web": "scene.render", "gfx": "gfx.render"}[kind]
    plan = plan_edit(lambda ctx: embed_scene(ctx, any_scene, {}, kind=kind))
    (render,) = renders(plan, op)
    assert scene_of(render)["size" if kind != "blender" else "render"] is not None


def test_non_scene_entries_raise(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def visit(s: eks.Session) -> None:
        return None

    with pytest.raises(TraceError, match="flow 'visit' is not a scene"):
        plan_edit(lambda ctx: embed_scene(ctx, visit, {}))

    def plain(sc: Any) -> None:
        return None

    with pytest.raises(TraceError, match="not a registered"):
        plan_edit(lambda ctx: embed_scene(ctx, plain, {}))


def test_unknown_scene_name_raises(eks_registry: Registry) -> None:
    define_phone()
    with pytest.raises(TraceError, match="unknown scene 'phnoe'; did you mean 'phone'"):
        plan_edit(lambda ctx: embed_scene(ctx, "phnoe", {}))


def test_bad_scene_reference_raises(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="decorated with @eks.scene"):
        plan_edit(lambda ctx: embed_scene(ctx, 3, {}))  # type: ignore[arg-type]


def test_inputs_are_coerced(eks_registry: Registry) -> None:
    phone = define_phone()
    with pytest.raises(PlanBuildError, match="needs a value for 'screen'"):
        plan_edit(lambda ctx: embed_scene(ctx, phone, {}))
    with pytest.raises(PlanBuildError, match="has no parameter 'scren'"):
        plan_edit(lambda ctx: embed_scene(ctx, phone, {"scren": "artifact:S"}))
    with pytest.raises(PlanBuildError, match="expects media"):
        plan_edit(lambda ctx: embed_scene(ctx, phone, {"screen": 42}))


def test_scene_event_returns_an_event_ref_on_the_render(eks_registry: Registry) -> None:
    logo = define_logo()
    events: list[EventRef] = []

    def body(ctx: TraceContext) -> Any:
        render = embed_scene(ctx, logo, {"screen": eks.media.source("artifact:SCREEN1")})
        events.append(scene_event(render, "logo.done", side="left"))
        return render

    plan = plan_edit(body)
    (render,) = renders(plan)
    assert events == [event_ref(render.id, "logo.done", side="left")]
    assert events[0].model_dump(mode="json")["source"] == {"$ref": render.id, "path": []}


def define_overlay() -> Callable[..., Any]:
    @eks.scene(kind="gfx")
    def overlay(sc: eks.GfxScene, cue: eks.Json) -> None:
        title = sc.text("Anlık kur")
        sc.on(EventRef.model_validate(cue), title.pulse(amount=1.2))

    return overlay


def plan_logo_and_overlay(logo: Callable[..., Any], overlay: Callable[..., Any], screen: str) -> Plan:
    def body(ctx: TraceContext) -> Any:
        render = embed_scene(ctx, logo, {"screen": eks.media.source(screen)})
        event = scene_event(render, "logo.done")
        return embed_scene(ctx, overlay, {"cue": event.model_dump(mode="json")})

    return plan_edit(body)


def test_scene_event_drives_a_trigger_of_another_embedded_scene(eks_registry: Registry) -> None:
    plan = plan_logo_and_overlay(define_logo(), define_overlay(), "artifact:SCREEN1")
    (blender,) = renders(plan)
    (gfx,) = renders(plan, "gfx.render")
    assert scene_of(gfx)["triggers"][0]["at"] == {
        "source": {"$ref": blender.id, "path": []}, "name": "logo.done", "where": {}}
    # the listener depends on the scene whose events it reacts to
    assert gfx.after == [blender.id]


def test_event_source_is_part_of_the_listener_hash(eks_registry: Registry) -> None:
    logo = define_logo()
    overlay = define_overlay()
    first = plan_logo_and_overlay(logo, overlay, "artifact:SCREEN1")
    second = plan_logo_and_overlay(logo, overlay, "artifact:SCREEN2")
    (first_gfx,) = renders(first, "gfx.render")
    (second_gfx,) = renders(second, "gfx.render")
    assert first_gfx.id == second_gfx.id
    assert first_gfx.args == second_gfx.args
    assert first_gfx.hash != second_gfx.hash
    assert renders(first)[0].hash != renders(second)[0].hash


def test_scene_event_rejects_names(eks_registry: Registry) -> None:
    logo = define_logo()

    def body(ctx: TraceContext) -> Any:
        render = embed_scene(ctx, logo, {"screen": eks.media.source("artifact:SCREEN1")})
        scene_event(render, "not an event")

    with pytest.raises(ValueError, match="not an event name"):
        plan_edit(body)


def test_scene_event_rejects_undeclared_events(eks_registry: Registry) -> None:
    logo = define_logo()
    phone = define_phone()

    def typo(ctx: TraceContext) -> Any:
        render = embed_scene(ctx, logo, {"screen": eks.media.source("artifact:SCREEN1")})
        scene_event(render, "logo.dnoe")

    def silent(ctx: TraceContext) -> Any:
        render = embed_scene(ctx, phone, {"screen": eks.media.source("artifact:SCREEN1")})
        scene_event(render, "logo.done")

    with pytest.raises(TraceError, match="never emits 'logo.dnoe' \\(it declares 'logo.done'\\); declare it"):
        plan_edit(typo)
    with pytest.raises(TraceError, match="never emits 'logo.done' \\(it declares no events\\)"):
        plan_edit(silent)


def test_scene_event_accepts_events_of_blender_emit_actions(eks_registry: Registry) -> None:
    @eks.scene(kind="blender")
    def emitter(sc: eks.BlenderScene) -> None:
        sc.on(t("bar:2"), sc.empty(id="logo").emit("logo.shown"))

    seen: list[EventRef] = []

    def body(ctx: TraceContext) -> Any:
        seen.append(scene_event(embed_scene(ctx, emitter, {}), "logo.shown"))

    plan_edit(body)
    assert [event.name for event in seen] == ["logo.shown"]


def test_triggers_check_the_event_source(eks_registry: Registry) -> None:
    logo = define_logo()
    overlay = define_overlay()

    def unknown(ctx: TraceContext) -> Any:
        return embed_scene(ctx, overlay, {"cue": {"source": {"$ref": "nmadeup0001"}, "name": "logo.done"}})

    def not_a_render(ctx: TraceContext) -> Any:
        screen = eks.media.source("artifact:SCREEN1")
        return embed_scene(ctx, overlay, {"cue": {"source": {"$ref": node_of(screen)}, "name": "logo.done"}})

    def undeclared(ctx: TraceContext) -> Any:
        render = embed_scene(ctx, logo, {"screen": eks.media.source("artifact:SCREEN1")})
        return embed_scene(ctx, overlay, {"cue": {"source": {"$ref": node_of(render)}, "name": "flipp"}})

    with pytest.raises(TraceError, match="names node 'nmadeup0001', which is not recorded in this plan trace"):
        plan_edit(unknown)
    with pytest.raises(TraceError, match="which is media.source; events come from"):
        plan_edit(not_a_render)
    with pytest.raises(TraceError, match="gfx scene on\\(\\): the scene rendered by .* never emits 'flipp'"):
        plan_edit(undeclared)


def test_event_sources_are_whole_nodes() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="event source is the render or take node itself"):
        EventRef.model_validate({"source": {"$ref": "nabcdefghjk", "path": ["uri"]}, "name": "done"})
    with pytest.raises(ValidationError):
        EventRef.model_validate({"source": "nabcdefghjk", "name": "done"})


def test_scene_event_outside_a_trace() -> None:
    from eks.ref import Ref

    event = scene_event(Ref("nabcdefghjk", "media"), "done", nth=1)
    assert event == event_ref("nabcdefghjk", "done", nth=1)


def test_embedded_render_is_returned_and_typed(eks_registry: Registry) -> None:
    phone = define_phone()
    seen: list[str] = []

    def body(ctx: TraceContext) -> Any:
        render = embed_scene(ctx, phone, {"screen": eks.media.source("artifact:SCREEN1")})
        seen.append(node_of(render))
        assert ctx.node(node_of(render)).op == "blender.render"
        return None

    plan = plan_edit(body)
    assert seen == [renders(plan)[0].id]


def test_embedding_is_deterministic(eks_registry: Registry) -> None:
    phone = define_phone()

    def body(ctx: TraceContext) -> Any:
        return [embed_scene(ctx, phone, {"screen": eks.media.source(f"artifact:SCREEN{i}")}) for i in (1, 2)]

    first = plan_edit(body)
    second = plan_edit(body)
    assert canonical_json(first.model_dump(mode="json")) == canonical_json(second.model_dump(mode="json"))


def test_embedded_scene_size_follows_the_embedding(eks_registry: Registry) -> None:
    @eks.scene(kind="gfx", size=(400, 400), fps=24)
    def small(sc: eks.GfxScene) -> None:
        sc.text("x")

    plan = plan_edit(lambda ctx: embed_scene(ctx, small, {}), fps=60, size=(1080, 1080))
    (render,) = renders(plan, "gfx.render")
    assert scene_of(render)["size"] == [1080, 1080]
    assert scene_of(render)["fps"] == 60


def test_open_scene_registers_one_render(eks_registry: Registry) -> None:
    @eks.scene(kind="gfx")
    def lone(sc: eks.GfxScene) -> None:
        sc.text("x")

    plan = trace(eks_registry.entry_for(lone), root=open_scene)
    assert [node.op for node in plan.nodes] == ["gfx.render"]
    assert plan.outputs is None
