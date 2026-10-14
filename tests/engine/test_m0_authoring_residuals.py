from __future__ import annotations

import json
import textwrap
from collections.abc import Callable, Iterator
from typing import Any, Literal

import pytest
from pydantic import Field, ValidationError

import eks
from eks import by, t
from eks.tracing import TraceError, capture_function
from eks_harness.engine.validator import Report, validate
from eks_harness.ir.catalog.effects import EffectBase
from eks_harness.ir.contributions import contributions, node_args
from eks_harness.ir.hashing import (
    canonical_dump,
    hash_plan,
    machine_dependent,
    resource_id,
    time_needs,
)
from eks_harness.ir.ops.ctl import (
    ALLOWED_DYNAMIC_GLOBALS,
    sandbox_leaks,
    scan_sandbox_source,
)
from eks_harness.ir.ops.edit import RenderSettings
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import (
    Registry,
    RegistryError,
    contributing,
    default_registry,
)

NOW = "2026-01-15T09:00+03:00"
SOURCE_ID = "n0000000000"


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def one(plan: Plan, op: str) -> Node:
    (node,) = nodes_of(plan, op)
    return node


def codes(report: Report) -> list[tuple[str, str | None]]:
    return [(issue.code, issue.field) for issue in report.issues if issue.code != "clock.now"]


def rehashed(data: dict[str, Any]) -> Plan:
    return hash_plan(Plan.model_validate(data))


# Set and frozenset fields are walked for contributions and times, and dumped in a canonical order


def glitter_plan(registry: Registry, plugin_version: str, kind_version: str) -> Plan:
    state = registry.snapshot()
    try:
        with contributing("sparkle-fx", plugin_version):
            @eks.effect(description="Sparkles", version=kind_version)
            class Sparkle(EffectBase, frozen=True):
                kind: Literal["sparkle"] = "sparkle"
                density: float = 1.0

        @eks.op("acme", name="glitter", out="json", pure=True, description="Apply effects", examples=[{"fx": []}])
        class Glitter(eks.Args):
            fx: frozenset[Sparkle] = Field(description="Effects")

        @eks.flow(app="web-app")
        def flow(s: eks.Session) -> None:
            s.op("acme.glitter", fx=[{"kind": "sparkle", "density": 2.0}])

        plan = eks.plan(flow, platform="web")
        node = one(plan, "acme.glitter")
        model = node_args(node, registry)
        assert model is not None
        assert isinstance(dict(model)["fx"], frozenset)
        assert [(item.kind, item.version) for item in contributions(model, registry, op=node.op)] == [
            ("sparkle", kind_version)]
        return plan
    finally:
        registry.restore(state)


def test_plugin_kinds_inside_a_frozenset_field_reach_the_hash_and_requires(registry: Registry) -> None:
    first = glitter_plan(registry, "1.0", "1")
    second = glitter_plan(registry, "2.0", "2")
    assert [(item.plugin, item.version) for item in first.requires] == [("sparkle-fx", "1.0")]
    assert [(item.plugin, item.version) for item in second.requires] == [("sparkle-fx", "2.0")]
    assert one(first, "acme.glitter").hash != one(second, "acme.glitter").hash
    assert first.hash != second.hash


def test_time_needs_read_times_inside_sets() -> None:
    assert time_needs(frozenset({t("12f")})).fps
    assert time_needs({t("cue:intro")}).cues == frozenset({"intro"})


def test_set_fields_dump_in_canonical_order() -> None:
    @eks.op("acme", name="tagged", out="none", description="Tag the page", examples=[{"tags": []}])
    class Tagged(eks.Args):
        tags: frozenset[str] = Field(description="Tags")

    words = ["delta", "alpha", "charlie", "bravo", "echo", "golf", "foxtrot"]
    dumps = {json.dumps(canonical_dump(Tagged(tags=frozenset(words[index:] + words[:index]))))
             for index in range(len(words))}
    assert dumps == {json.dumps({"tags": sorted(words)})}

    @eks.flow(app="web-app")
    def flow(s: eks.Session) -> None:
        s.op("acme.tagged", tags=list(reversed(words)))

    assert one(eks.plan(flow, platform="web"), "acme.tagged").args["tags"] == sorted(words)


# A pinning macro reached through a local alias inside @eks.dynamic in a take is a pin.loop


def register_pinned_tap() -> None:
    @eks.macro(namespace="probe")
    def pinned_tap(s: eks.Session) -> None:
        s.ui.tap("#row", at=t("2s"))

    @eks.macro(namespace="probe")
    def aliased_tap(s: eks.Session) -> None:
        tap = s.probe.pinned_tap
        tap()

    @eks.macro(namespace="probe")
    def plain_tap(s: eks.Session) -> None:
        s.ui.tap("#row")


def score_with(body: Callable[[Any, Any], Any]) -> Plan:
    @eks.score(fps=30)
    def looped(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app", now=NOW)
        with take.flow() as s:
            count = s.record_op("ui.count", {"target": by.test_id("row")})
            body(s, count)

    return eks.plan(looped)


def loops(plan: Plan) -> list[str]:
    return [issue.message for issue in validate(plan).issues if issue.code == "pin.loop"]


def test_macro_alias_in_a_dynamic_function_is_a_pin_loop() -> None:
    register_pinned_tap()

    def body(s: Any, count: Any) -> None:
        @eks.dynamic
        def per_row(n: Any = count) -> Any:
            tap = s.probe.pinned_tap
            return [tap() for i in range(n)]

    [message] = loops(score_with(body))
    assert "calls macro 'probe.pinned_tap'" in message


def test_namespace_alias_in_a_dynamic_function_is_a_pin_loop() -> None:
    register_pinned_tap()

    def body(s: Any, count: Any) -> None:
        @eks.dynamic
        def per_row(n: Any = count) -> Any:
            p = s.probe
            return [p.pinned_tap() for i in range(n)]

    [message] = loops(score_with(body))
    assert "calls macro 'probe.pinned_tap'" in message


def test_walrus_alias_in_a_dynamic_function_is_a_pin_loop() -> None:
    register_pinned_tap()

    def body(s: Any, count: Any) -> None:
        @eks.dynamic
        def per_row(n: Any = count) -> Any:
            rows = [(p := s.probe).pinned_tap() for i in range(n)]
            return [rows, p is not None]

    [message] = loops(score_with(body))
    assert "calls macro 'probe.pinned_tap'" in message


def test_alias_inside_a_called_macro_body_is_a_pin_loop() -> None:
    register_pinned_tap()

    def body(s: Any, count: Any) -> None:
        @eks.dynamic
        def per_row(n: Any = count) -> Any:
            return [s.probe.aliased_tap() for i in range(n)]

    [message] = loops(score_with(body))
    assert "calls macro 'probe.aliased_tap'" in message and "pins a step with at=" in message


def test_aliases_of_unpinned_macros_stay_clean() -> None:
    register_pinned_tap()

    def body(s: Any, count: Any) -> None:
        @eks.dynamic
        def per_row(n: Any = count) -> Any:
            tap = s.probe.plain_tap
            ui = s.ui
            return [(tap(), ui.tap("#row")) for i in range(n)]

    assert loops(score_with(body)) == []


# Sandbox data inference: user dunder results and comparisons are opaque, and the pydantic parse API is blocked


@pytest.mark.parametrize("source", [
    '''
    def leak(n=1):
        class A:
            def __abs__(self):
                return lambda: n
        return str(abs(A()))
    ''',
    '''
    def leak(n=1):
        class A:
            def __lt__(self, other):
                return lambda: n
        return str(A() < 1)
    ''',
    '''
    def leak(n=1):
        class A:
            def __round__(self):
                return lambda: n
        return f"{round(A())}"
    ''',
])
def test_dunder_results_of_sandbox_classes_are_not_data(source: str) -> None:
    assert sandbox_leaks(textwrap.dedent(source))


def test_scalar_results_and_comparisons_of_data_stay_data() -> None:
    source = textwrap.dedent('''
    def fine(n=1):
        return str(n < 3) + str(abs(n)) + str(round(2.5)) + str([n] == [1]) + str(len("x") in [1]) + f"{divmod(n, 2)}"
    ''')
    assert sandbox_leaks(source) == ()


def test_the_pydantic_parse_api_of_eks_members_is_blocked() -> None:
    source = textwrap.dedent('''
    def readfile(path="/tmp/target.json"):
        return [eks.Target.parse_file(path), eks.Span.parse_raw(path), eks.Target.from_orm(path)]
    ''')
    assert [use.name for use in scan_sandbox_source(source).escapes] == ["parse_file", "parse_raw", "from_orm"]

    def readfile(path: str = "/tmp/target.json") -> Any:
        return eks.Target.parse_file(path).model_dump()

    with pytest.raises(TraceError, match="parse_file"):
        capture_function(readfile, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)


# An embedded scene on an open clock needs its own duration


def gfx_scene(name: str, duration: str | None = None, bpm: int | None = None,
              body: Callable[[Any], None] | None = None) -> Any:
    options: dict[str, Any] = {"name": name, "kind": "gfx", "size": (1080, 1920), "seed": 1}
    if duration is not None:
        options["duration"] = t(duration)
    if bpm is not None:
        options["bpm"] = bpm

    @eks.scene(**options)
    def scene(sc: eks.GfxScene) -> None:
        title = sc.text("EksenGOLD", id="title")
        if body is not None:
            body(sc)
        else:
            sc.on(t("end-1s"), title.assign("y", 300.0))

    return scene


def open_score(scene: Any, clock: dict[str, Any], end: str) -> Plan:
    @eks.score(fps=30, **clock)
    def ad(sc: eks.Score) -> None:
        sc.edit.track("graphics").clip(sc.gfx(scene), src=t.span(t("0s"), t(end)))

    return eks.plan(ad)


def test_a_scene_without_duration_embedded_on_an_open_clock_is_reported() -> None:
    scene = gfx_scene("endy")
    short, long = open_score(scene, {}, "5s"), open_score(scene, {}, "10s")
    assert one(short, "gfx.render").hash == one(long, "gfx.render").hash
    assert codes(validate(short)) == [("scene.length", "scene.duration")]
    assert codes(validate(open_score(gfx_scene("timed", "8s"), {}, "5s"))) == []
    assert codes(validate(open_score(scene, {"duration": t("15s")}, "5s"))) == []
    assert codes(validate(eks.plan(scene))) == []


# Times assigned through gfx set actions join the render hash and the time checks


def assign_start(sc: Any) -> None:
    title = sc.text("X", id="label")
    sc.on(t("0s"), title.assign("start", t("beat:4")))


def assign_src(sc: Any) -> None:
    clip = sc.video("artifact:VID123", id="clip")
    sc.on(t("0s"), clip.assign("src", t.span(t("beat:1"), t("beat:3"))))


def assign_cue(sc: Any) -> None:
    title = sc.text("X", id="label")
    sc.on(t("0s"), title.assign("start", t("cue:hit")))


@pytest.mark.parametrize("body", [assign_start, assign_src])
def test_times_assigned_by_set_actions_follow_the_tempo(body: Callable[[Any], None]) -> None:
    fast = eks.plan(gfx_scene("fast", "8s", bpm=120, body=body))
    slow = eks.plan(gfx_scene("slow", "8s", bpm=60, body=body))
    assert one(fast, "gfx.render").hash != one(slow, "gfx.render").hash


def test_cues_assigned_by_set_actions_join_the_hash_and_the_checks() -> None:
    scene = gfx_scene("cued", "8s", body=assign_cue)

    def build(at: str) -> Plan:
        @eks.score(name=f"cue_{at[0]}", fps=30, bpm=120, duration=t("8s"))
        def cued(sc: eks.Score) -> None:
            sc.cue("hit", t(at))
            sc.edit.track("graphics").clip(sc.gfx(scene))

        return eks.plan(cued)

    assert one(build("2s"), "gfx.render").hash != one(build("3s"), "gfx.render").hash

    @eks.edit(fps=30, size=(1080, 1920))
    def uncued(e: eks.Edit) -> None:
        e.track("g").clip(e.gfx(scene))

    assert codes(validate(eks.plan(uncued))) == [("time.unresolvable", "scene.triggers.0.action.value")]


# The versions of the session's checks are pinned by the plan hash


def register_contrast(version: str) -> None:
    with contributing("acme.checks", "1.0"):
        @eks.check("contrast_ratio", platforms=["web"], default=True, version=version,
                   description="Fails when text contrast is below the minimum")
        class ContrastRatio(eks.Args):
            minimum: float = 4.5


def test_a_default_check_version_bump_changes_the_plan_hash(registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web", now=NOW, timezone="Europe/Istanbul", locale="tr-TR")
    def checked(s: eks.Session) -> None:
        s.checkpoint("defaults")

    clean = registry.snapshot()
    register_contrast("1")
    first = eks.plan(checked)
    registry.restore(clean)
    register_contrast("2")
    second = eks.plan(checked)
    assert first.session is not None and "contrast_ratio" in [use.kind for use in first.session.defaults]
    assert first.hash != second.hash
    assert [issue.code for issue in validate(first).issues] == ["plan.hash"]


# Leases and backends that belong to no session, or carry params, are rejected in plans with sessions


def web_plan(*, api: bool = False) -> tuple[Plan, dict[str, Any]]:
    @eks.flow(app="web-app", platform="web", now=NOW, timezone="Europe/Istanbul", locale="tr-TR")
    def webflow(s: eks.Session) -> None:
        s.ui.nav("/tenant/settings")
        if api:
            s.api.get("/api/settings")

    plan = eks.plan(webflow)
    return plan, json.loads(plan.to_json())


def add_resource(data: dict[str, Any], kind: str, **identity: Any) -> str:
    rid = resource_id(kind, identity)
    data["resources"].append({"id": rid, "kind": kind, **identity})
    return rid


@pytest.mark.parametrize(("kind", "identity"), [
    ("lease.android", {"app": "mobile-app"}),
    ("lease.web", {"app": "web-app", "name": "second"}),
    ("backend", {"app": "web-app", "definition": "eksenapi", "seed": "other"}),
])
def test_a_session_node_cannot_use_a_session_less_lease_or_backend(kind: str, identity: dict[str, Any]) -> None:
    plan, data = web_plan()
    assert codes(validate(plan)) == []
    foreign = add_resource(data, kind, **identity)
    nav = next(node for node in data["nodes"] if node["op"] == "ui.nav")
    nav["uses"] = [*nav["uses"], foreign]
    found = sorted({code for code, _field in codes(validate(rehashed(data)))})
    assert "session.uses" in found and "session.resources" in found


def test_lease_and_backend_params_are_rejected() -> None:
    plan, data = web_plan(api=True)
    assert sorted(resource["kind"] for resource in data["resources"]) == ["backend", "lease.web"]
    renamed: dict[str, str] = {}
    for resource in data["resources"]:
        extra = {"sid": "abc123"} if resource["kind"].startswith("lease.") else {"url": "https://api.example.test"}
        resource["params"] = extra
        identity = {key: resource.get(key) for key in ("app", "definition", "seed", "session", "name")}
        new_id = resource_id(resource["kind"], {**extra, **identity})
        renamed[resource["id"]] = new_id
        resource["id"] = new_id
    for node in data["nodes"]:
        node["uses"] = [renamed.get(rid, rid) for rid in node["uses"]]
    found = codes(validate(rehashed(data)))
    assert [code for code, _field in found] == ["resource.params", "resource.params"]
    assert plan.resources


# Encoders are typed; hardware encoders are keyed per machine and software encoders run with fixed threads


def edit_plan(name: str, **settings: Any) -> Plan:
    @eks.edit(name=name, fps=30, size=(1080, 1920), duration=t("3s"))
    def still(e: eks.Edit) -> None:
        e.track("main").clip(e.media("artifact:SHOT1@sha256:" + "a" * 64))
        e.render_settings(**settings)

    return eks.plan(still)


def test_encoders_are_typed_and_classified() -> None:
    for name in ("auto", ""):
        with pytest.raises(ValidationError):
            RenderSettings.model_validate({"encoder": name})
    software = one(edit_plan("soft", encoder="libx264"), "edit.render")
    timeline: Any = software.args["timeline"]
    assert timeline["render"]["threads"] == 8
    assert not machine_dependent(software)
    for encoder in ("nvenc", "videotoolbox", "qsv", "amf"):
        assert machine_dependent(one(edit_plan(f"hard_{encoder}", encoder=encoder), "edit.render"))
    assert machine_dependent(one(edit_plan("unset"), "edit.render"))


# An op cannot be both pure (cached) and idempotent (polled)


def test_pure_and_idempotent_ops_are_rejected() -> None:
    with pytest.raises(RegistryError, match="pure and idempotent"):
        @eks.op("probe", name="peek", out="json", pure=True, idempotent=True, description="Peek",
                examples=[{"key": "k"}])
        class Peek(eks.Args):
            key: str = Field(description="Key")


# Two bases of one seed cannot create the same resource or persona


def register_seeds() -> None:
    @eks.resource("tenant", description="Tenant")
    class Tenant(eks.Resource):
        name: str

    @eks.resource("persona", parent="tenant", persona=True, description="User")
    class Persona(eks.Resource):
        role: str

    @eks.seed("first", backend="eksenapi", now=NOW, seed=2026)
    def first(d: Any) -> None:
        d.tenant("harman", name="H").persona("admin", role="admin")

    @eks.seed("second", backend="eksenapi", now=NOW, seed=2026)
    def second(d: Any) -> None:
        d.tenant("harman", name="H2").persona("admin", role="admin")

    @eks.seed("third", backend="eksenapi", now=NOW, seed=2026)
    def third(d: Any) -> None:
        d.tenant("moda", name="M")

    @eks.seed("shared", backend="eksenapi", now=NOW, seed=2026)
    def shared(d: Any) -> None:
        d.extend(base="third")

    @eks.seed("also", backend="eksenapi", now=NOW, seed=2026)
    def also(d: Any) -> None:
        d.extend(base="third")


def test_two_bases_creating_one_resource_raise_at_plan_time() -> None:
    register_seeds()

    @eks.seed("both", backend="eksenapi", now=NOW, seed=2026)
    def both(d: Any) -> None:
        d.extend(base="first")
        d.extend(base="second")

    with pytest.raises(TraceError, match="both create resource 'tenant:harman'"):
        eks.plan(both)

    @eks.seed("diamond", backend="eksenapi", now=NOW, seed=2026)
    def diamond(d: Any) -> None:
        d.extend(base="shared")
        d.extend(base="also")

    assert codes(eks.validate(eks.plan(diamond))) == []


def test_the_validator_reports_two_bases_creating_one_resource() -> None:
    register_seeds()

    @eks.seed("mixed", backend="eksenapi", now=NOW, seed=2026)
    def mixed(d: Any) -> None:
        d.extend(base="first")
        d.extend(base="third")

    plan = eks.plan(mixed)
    assert codes(eks.validate(plan)) == []
    data = json.loads(plan.to_json())
    extend = next(node for node in data["nodes"] if node["op"] == "data.extend" and node["args"]["base"] == "third")
    @eks.seed("probe_second", backend="eksenapi", now=NOW, seed=2026)
    def probe_second(d: Any) -> None:
        d.extend(base="second")

    extend["args"]["base"] = "second"
    extend["args"]["hash"] = one(eks.plan(probe_second), "data.extend").args["hash"]
    report = eks.validate(rehashed(data))
    assert {(issue.code, issue.node, issue.field) for issue in report.issues} == {
        ("seed.resource", extend["id"], "base")}
    messages = [issue.message for issue in report.issues]
    assert [message.split(" is created")[0] for message in messages] == [
        "resource 'tenant:harman'", "resource 'tenant:harman/persona:admin'", "persona 'admin'"]


# Scene graph rules of the builders are checked on edited plans


def blender_plan() -> Plan:
    @eks.scene(name="cube", kind="blender", engine="cycles", samples=8, fps=30, duration=t("2s"), device="GPU",
               bpm=120)
    def cube(sc: eks.BlenderScene) -> None:
        sc.world(hdri="artifact:HDRI1@sha256:" + "b" * 64)
        box = sc.mesh("cube", id="box")
        camera = sc.camera(lens=50, id="cam")
        box.animate("location", {t("0s"): (0.0, 0.0, 0.0), t("1s"): (0.0, 1.0, 0.0)})
        sc.on(t("1s"), box.pulse(amount=1.1))
        sc.on(t("1s"), camera.assign("lens", 35.0))

    return eks.plan(cube)


def gfx_plan() -> Plan:
    @eks.scene(name="card", kind="gfx", size=(1080, 1920), duration=t("2s"), seed=1)
    def card(sc: eks.GfxScene) -> None:
        title = sc.text("EksenGOLD", id="title")
        sc.on(t("0s"), title.pulse(amount=1.1))
        sc.on(t("0s"), title.assign("opacity", 0.5))
        sc.on(t("0s"), title.tween("y", {t("0s"): 0.0, t("1s"): 100.0}))

    return eks.plan(card)


def edited(plan: Plan, op: str, change: Callable[[dict[str, Any]], None]) -> list[tuple[str, str | None]]:
    data = json.loads(plan.to_json())
    found = next(node for node in data["nodes"] if node["op"] == op)
    change(found["args"]["scene"])
    sources = [node["id"] for node in data["nodes"] if node["op"] == "media.source"]
    text = json.dumps(data).replace(SOURCE_ID, sources[0] if sources else SOURCE_ID)
    return codes(validate(rehashed(json.loads(text))))


def animation(scene: dict[str, Any]) -> dict[str, Any]:
    return scene["animations"][0]


def action(scene: dict[str, Any], index: int) -> dict[str, Any]:
    return scene["triggers"][index]["action"]


def node(scene: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(item for item in scene["nodes"] if item["id"] == node_id)


BLENDER_EDITS: list[tuple[str, Callable[[dict[str, Any]], None], tuple[str, str]]] = [
    ("animate a fixed field", lambda scene: animation(scene).update(prop="kind"),
     ("scene.prop", "scene.animations.0.prop")),
    ("animate a field that cannot be animated", lambda scene: animation(scene).update(prop="size"),
     ("scene.prop", "scene.animations.0.prop")),
    ("keyframe of the wrong type", lambda scene: animation(scene)["keys"][0].update(v="not-a-vector"),
     ("scene.value", "scene.animations.0.keys.0.v")),
    ("keyframe of the wrong arity", lambda scene: animation(scene)["keys"][0].update(v=[1.0, 2.0]),
     ("scene.value", "scene.animations.0.keys.0.v")),
    ("keyframe holding a ref", lambda scene: animation(scene)["keys"][0].update(v={"$ref": SOURCE_ID, "path": []}),
     ("scene.value", "scene.animations.0.keys.0.v")),
    ("two animations of one property", lambda scene: scene["animations"].append(dict(animation(scene))),
     ("scene.duplicate", "scene.animations.1.prop")),
    ("pulse on a boolean", lambda scene: action(scene, 0).update(prop="visible"),
     ("scene.prop", "scene.triggers.0.action.prop")),
    ("set a fixed field", lambda scene: action(scene, 1).update(prop="kind", value="mesh"),
     ("scene.prop", "scene.triggers.1.action.prop")),
    ("set a value of the wrong type", lambda scene: action(scene, 1).update(value="wide"),
     ("scene.value", "scene.triggers.1.action.value")),
    ("camera naming a mesh", lambda scene: scene.update(camera="box"), ("scene.camera", "scene.camera")),
    ("camera tracking itself", lambda scene: node(scene, "cam").update(look_at="cam"),
     ("scene.camera", "scene.nodes.1.look_at")),
]


@pytest.mark.parametrize(("change", "expected"), [(change, expected) for _name, change, expected in BLENDER_EDITS],
                         ids=[name for name, _change, _expected in BLENDER_EDITS])
def test_edited_blender_scenes_are_checked(change: Callable[[dict[str, Any]], None], expected: tuple[str, str]) -> None:
    plan = blender_plan()
    assert codes(validate(plan)) == []
    assert edited(plan, "blender.render", change) == [expected]


GFX_EDITS: list[tuple[str, Callable[[dict[str, Any]], None], tuple[str, str]]] = [
    ("pulse on text", lambda scene: action(scene, 0).update(prop="text"), ("scene.prop", "scene.triggers.0.action.prop")),
    ("set a fixed field", lambda scene: action(scene, 1).update(prop="id", value="other"),
     ("scene.prop", "scene.triggers.1.action.prop")),
    ("set a value of the wrong type", lambda scene: action(scene, 1).update(value="loud"),
     ("scene.value", "scene.triggers.1.action.value")),
    ("animate the font", lambda scene: action(scene, 2).update(prop="font"),
     ("scene.prop", "scene.triggers.2.action.prop")),
    ("keyframe of the wrong type", lambda scene: action(scene, 2)["keys"][0].update(v="high"),
     ("scene.value", "scene.triggers.2.action.keys")),
]


@pytest.mark.parametrize(("change", "expected"), [(change, expected) for _name, change, expected in GFX_EDITS],
                         ids=[name for name, _change, _expected in GFX_EDITS])
def test_edited_gfx_scenes_are_checked(change: Callable[[dict[str, Any]], None], expected: tuple[str, str]) -> None:
    plan = gfx_plan()
    assert codes(validate(plan)) == []
    assert edited(plan, "gfx.render", change) == [expected]
