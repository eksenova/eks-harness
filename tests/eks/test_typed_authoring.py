from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

import jsonschema
import pytest
from pydantic import BaseModel, ValidationError

import eks
from eks import t
from eks.discovery import tree_registry
from eks.tracing import TraceError
from eks.workspace import plan_spec
from eks_harness.engine.validator import validate
from eks_harness.ir.hashing import cache_inputs, cacheable, content_dependent
from eks_harness.ir.ops.api import Get
from eks_harness.ir.ops.data import Create
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry, RegistryError, contributing
from eks_harness.ir.schema import plan_schema
from eks_harness.ir.time import TempoMap

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = ROOT / "examples" / "dsl"
NOW = "2026-01-15T09:00+03:00"


def by_op(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


# typed macro calls: s.macro(fn)(...)


def test_typed_macro_calls_keep_the_macro_container(eks_registry: Registry) -> None:
    @eks.macro(namespace="shop", platforms=["web"], version="2")
    def login(s: eks.Session, user: str, remember: bool = False) -> eks.Ref[None]:
        s.ui.fill("#user", user)
        return s.ui.tap("#login")

    @eks.flow(app="web-app", platform="web", now=NOW, timezone="UTC", locale="en-US")
    def typed(s: eks.Session) -> None:
        s.macro(login)("ayse", remember=True)
        s.shop.login("ayse", remember=True)

    plan = eks.plan(typed)
    first, second = by_op(plan, "ctl.macro")
    # both spellings record the same container, version and params; only the call site (node id) differs
    assert first.args == second.args == {"name": "shop.login", "version": "2",
                                          "params": {"user": "ayse", "remember": True}}
    assert first.branches is not None
    assert [node.op for node in first.branches["body"]] == ["ui.fill", "ui.tap"]
    assert validate(plan).issues == ()


def test_typed_macro_calls_reject_unregistered_functions_and_bad_arguments(eks_registry: Registry) -> None:
    def plain(s: eks.Session) -> None:
        s.ui.back()

    @eks.macro(namespace="shop")
    def login(s: eks.Session, user: str) -> None:
        s.ui.fill("#user", user)

    @eks.flow(app="web-app", platform="web", now=NOW)
    def unregistered(s: eks.Session) -> None:
        s.macro(plain)()

    with pytest.raises(RegistryError, match="'.*plain' is not a registered macro"):
        eks.plan(unregistered)

    @eks.flow(app="web-app", platform="web", now=NOW)
    def wrong(s: eks.Session) -> None:
        s.macro(login)(nick="x")  # pyright: ignore[reportCallIssue]

    with pytest.raises(TypeError, match="macro shop.login"):
        eks.plan(wrong)
    with pytest.raises(RegistryError, match="session"):
        eks.macro(namespace="macro")(login)


# typed seed resources: d.add(Model(...))


class Federation(eks.Resource):
    type: str


class Tenant(eks.Resource):
    name: str
    reseller: Annotated[eks.ResourceRef, eks.refers_to("federation")] | None = None


class TenantPersona(eks.Resource):
    role: str


def register_kinds() -> None:
    eks.resource("federation", description="A reseller federation")(Federation)
    eks.resource("tenant", description="A jeweller tenant")(Tenant)
    eks.resource("persona", parent="tenant", persona=True, description="A tenant user")(TenantPersona)


def content(plan: Plan) -> list[str]:
    paths = {node.id: node.args["path"] for node in plan.nodes}
    text = [json.dumps(node.args, sort_keys=True) for node in plan.nodes]
    for node_id, path in paths.items():
        text = [line.replace(node_id, str(path)) for line in text]
    return text


def test_add_records_the_same_nodes_as_the_shorthand(eks_registry: Registry) -> None:
    register_kinds()

    @eks.seed("typed", backend="eksenapi", now=NOW)
    def typed(d: eks.Data) -> None:
        bayi = d.add(Federation(type="Reseller"), key="bayi")
        harman = d.add(Tenant(name="Harman", reseller=bayi.expr), key="harman")
        d.add(TenantPersona(role="admin"), key="harman-admin", parent=harman)

    @eks.seed("short", backend="eksenapi", now=NOW)
    def short(d: eks.Data) -> None:
        bayi = d.federation("bayi", type="Reseller")
        harman = d.tenant("harman", name="Harman", reseller=bayi)
        harman.persona("harman-admin", role="admin")

    first, second = eks.plan(typed), eks.plan(short)
    # ids follow the call site (and the seed name is part of the backend resource), so compare the nodes with
    # every $ref replaced by the path of the resource it names
    assert content(first) == content(second)
    assert [node.args["path"] for node in first.nodes] == ["federation:bayi", "tenant:harman",
                                                           "tenant:harman/persona:harman-admin"]
    assert first.personas == ["harman-admin"]
    assert validate(first).issues == ()


def test_add_checks_kinds_placement_and_references(eks_registry: Registry) -> None:
    register_kinds()

    class Unregistered(eks.Resource):
        name: str

    cases: list[tuple[Any, type[Exception], str]] = [
        (lambda d: d.add(Unregistered(name="x"), key="x"), TraceError, "not a registered resource model"),
        (lambda d: d.add(TenantPersona(role="admin"), key="p"), TraceError, "under a tenant handle"),
        (lambda d: d.add(Tenant(name="x")), TypeError, "needs a key"),
        (lambda d: d.add("tenant", key="x"), TypeError, "registered resource model"),
        (lambda d: d.add(Tenant(name="x", reseller=d.add(Tenant(name="y"), key="y").expr), key="x"), TraceError,
         "refers to a federation resource"),
    ]
    for index, (call, error, message) in enumerate(cases):
        @eks.seed(f"bad{index}", backend="eksenapi", now=NOW)
        def bad(d: eks.Data, call: Any = call) -> None:
            call(d)

        with pytest.raises(error, match=message):
            eks.plan(bad)
    with pytest.raises(ValidationError):
        Tenant(nme="typo")  # pyright: ignore[reportCallIssue]


# implementation hooks of extension decorators


class Chip(BaseModel, frozen=True, extra="forbid"):
    label: str


def locate(tree: Mapping[str, Any], label: str) -> list[tuple[int, int, int, int]]:
    return [tuple(tree["bounds"])] if tree.get("name") == label else []


def test_target_kinds_store_resolvers_per_surface(eks_registry: Registry) -> None:
    with contributing("acme", "1.0.0"):
        eks.target_kind("acme_chip", surfaces=["web", "screenshot"], resolvers={"web": "acme/chip.js",
                                                                               "screenshot": locate})(Chip)
    spec = eks_registry.catalog("target", "acme_chip")
    assert spec.resolvers == {"web": "acme/chip.js", "screenshot": f"{__name__}:locate"}
    with pytest.raises(RegistryError, match=r"resolvers for \['ios'\] name surfaces the kind does not declare"):
        eks.target_kind("acme_other", surfaces=["web"], resolvers={"ios": "acme/other.js"})(Chip)
    with pytest.raises(RegistryError, match="non-empty reference"):
        eks.target_kind("acme_empty", surfaces=["web"], resolvers={"web": ""})(Chip)
    with pytest.raises(RegistryError, match="module:qualname"):
        eks.target_kind("acme_broken", surfaces=["web"], resolvers={"web": "acme:"})(Chip)


def low_contrast(args: BaseModel, texts: list[tuple[str, str, str]]) -> list[str]:
    return [label for label, foreground, background in texts if foreground == background]


def test_checks_store_their_implementation(eks_registry: Registry) -> None:
    @eks.check("acme_contrast", platforms=["web"], impl=low_contrast, description="Contrast")
    class Contrast(eks.Args):
        minimum: float = 4.5

    assert eks_registry.check("acme_contrast").impl == f"{__name__}:low_contrast"
    assert eks_registry.check("acme_contrast").args is Contrast

    @eks.check("acme_worker", impl="acme/checks/spill.js", description="Worker side check")
    class Worker(eks.Args):
        pass

    assert eks_registry.check("acme_worker").impl == "acme/checks/spill.js"
    assert eks_registry.check("layout_spill").impl is None
    with pytest.raises(RegistryError, match="implementation"):
        eks.check("acme_bad", impl="   ")(Worker)


def test_resource_kinds_record_their_create_hook(eks_registry: Registry) -> None:
    @eks.resource("acme_branch", description="A branch created through the API")
    class Branch(eks.Resource):
        name: str

        def create(self, ctx: Any) -> Mapping[str, Any]:
            return ctx.api.post("/api/branches", json={"name": self.name})

    assert eks_registry.resource("acme_branch").create == f"{__name__}:{Branch.create.__qualname__}"

    @eks.resource("acme_plain", description="No create hook yet")
    class Plain(eks.Resource):
        name: str

    assert eks_registry.resource("acme_plain").create is None

    with pytest.raises(RegistryError, match="must be a plain method"):
        @eks.resource("acme_static")
        class Static(eks.Resource):
            @staticmethod
            def create(ctx: Any) -> None:
                del ctx

    with pytest.raises(RegistryError, match=r"must take the instance and the creation context"):
        @eks.resource("acme_two")
        class Two(eks.Resource):
            def create(self, ctx: Any, extra: int) -> None:
                del ctx, extra


def test_cookbook_contributions_carry_their_implementations(tmp_path: Path, eks_registry: Registry) -> None:
    root = tmp_path / "dsl"
    shutil.copytree(EXAMPLES_DIR, root, ignore=shutil.ignore_patterns("__pycache__"))
    plan_spec(".harness/flows/extending.py", cwd=root, plugins=True)
    registry = tree_registry(root)
    chip = registry.catalog("target", "cookbook_chip")
    assert chip.resolvers["web"] == "cookbook/chip.js"
    assert chip.resolvers["screenshot"].endswith(":locate_chips")
    assert registry.check("contrast").impl is not None
    assert registry.check("contrast").impl.endswith(":low_contrast")  # type: ignore[union-attr]
    assert registry.backend("eksenapi").impl.endswith(":LocalApi")
    assert not eks_registry.has_op("cookbook.rate")


# cacheability of pure nodes


def test_pure_nodes_downstream_of_effectful_nodes_are_not_cacheable_by_node_hash(tmp_path: Path,
                                                                                  eks_registry: Registry) -> None:
    root = tmp_path / "dsl"
    shutil.copytree(EXAMPLES_DIR, root, ignore=shutil.ignore_patterns("__pycache__"))
    plan = plan_spec(".harness/scores/ad.py", cwd=root, plugins=True)
    (take,) = by_op(plan, "score.take")
    (song, blend) = by_op(plan, "media.source")
    (render,) = by_op(plan, "edit.render")
    (scene,) = by_op(plan, "blender.render")
    (mix,) = by_op(plan, "audio.mix")
    (markers,) = by_op(plan, "media.markers")
    (gfx,) = by_op(plan, "gfx.render")
    # the take records footage every run, so the scene and the edit depend on it; files may change on disk;
    # the song's beat grid comes from backend='auto', which each machine resolves on its own, and so do the
    # GPU renders of the Blender and gfx scenes and the edit's encoder, which the score leaves unset
    assert cache_inputs(plan, take) is None
    assert not cacheable(plan, render)
    assert cache_inputs(plan, render) == (song.id, markers.id, take.id, blend.id, scene.id, gfx.id, render.id)
    assert cache_inputs(plan, scene) == (song.id, markers.id, take.id, blend.id, scene.id)
    assert cache_inputs(plan, gfx) == (song.id, markers.id, gfx.id)
    assert cache_inputs(plan, mix) == (song.id,)
    assert content_dependent(song) and content_dependent(blend)


def test_pure_chains_over_pinned_artifacts_are_cacheable(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def still(e: eks.Edit) -> None:
        e.render_settings(encoder="libx264")
        e.track("main").clip(e.media("artifact:VID123@sha256:cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"), src=t.span(t("1s"), t("3s")))

    plan = eks.plan(still)
    (render,) = by_op(plan, "edit.render")
    (source,) = by_op(plan, "media.source")
    assert not content_dependent(source)
    assert cacheable(plan, render)
    assert cache_inputs(plan, render) == ()

    @eks.edit(fps=30, size=(1080, 1920))
    def current(e: eks.Edit) -> None:
        e.render_settings(encoder="libx264")
        e.track("main").clip(e.media("artifact:VID123"), src=t.span(t("1s"), t("3s")))

    plan = eks.plan(current)
    (render,) = by_op(plan, "edit.render")
    (source,) = by_op(plan, "media.source")
    # the store re-renders, re-captures and restores artifacts under the same id, so an unpinned
    # artifact is read by content like a file
    assert content_dependent(source)
    assert cache_inputs(plan, render) == (source.id,)

    @eks.flow(app="web-app", platform="web", now=NOW)
    def shot(s: eks.Session) -> None:
        picture = s.shot("liste")
        eks.annotate(picture, [eks.overlay.caption("Liste", at=t.span(t("0s"), t("1s")))])

    plan = eks.plan(shot)
    (annotate,) = by_op(plan, "overlay.annotate")
    (capture,) = by_op(plan, "capture.shot")
    assert cache_inputs(plan, annotate) == (capture.id,)


def test_tempo_sources_are_inputs_of_beat_timed_nodes(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def beats(e: eks.Edit) -> None:
        e.audio.track("music/song.ogg").beats("beat", "downbeat")
        e.track("main").clip(e.media("artifact:VID123"), start=t("beat:4"))

    plan = eks.plan(beats)
    (render,) = by_op(plan, "edit.render")
    (song,) = [node for node in by_op(plan, "media.source") if str(node.args["uri"]).startswith("file:")]
    assert isinstance(plan.clock.tempo, TempoMap)
    assert song.id in (cache_inputs(plan, render) or ())


# schema strictness and descriptions


def test_every_plan_schema_property_is_described() -> None:
    schema = plan_schema()
    missing = [f"{name}.{field}" for name, body in schema["$defs"].items()
               for field, value in (body.get("properties") or {}).items() if "description" not in value]
    missing += [f"<plan>.{field}" for field, value in schema["properties"].items() if "description" not in value]
    assert missing == []


def test_create_phase_and_expected_statuses_are_constrained() -> None:
    with pytest.raises(ValidationError, match="phase"):
        Create.model_validate({"kind": "tenant", "path": "tenant:x", "phase": "later"})
    for phase in ("schema", "api", "finalize", "mongo"):
        assert Create.model_validate({"kind": "tenant", "path": "tenant:x", "phase": phase}).phase == phase
    with pytest.raises(ValidationError):
        Get.model_validate({"path": "/x", "expect_status": [700]})
    with pytest.raises(ValidationError):
        Get.model_validate({"path": "/x", "expect_status": []})
    assert Get.model_validate({"path": "/x", "expect_status": [204, 599]}).expect_status == (204, 599)
    schema = plan_schema()
    create = schema["$defs"]["data.create"]["properties"]["phase"]
    assert create["enum"] == ["schema", "api", "finalize", "mongo"]
    statuses = schema["$defs"]["api.get"]["properties"]["expect_status"]
    bounds = json.dumps(statuses)
    assert '"minimum": 100' in bounds and '"maximum": 599' in bounds
    node = {"id": "n0000000001", "op": "api.get", "args": {"path": "/x", "expect_status": [700]}}
    plan = {"schema": "eks/plan@1", "kind": "flow", "name": "x", "nodes": [node]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(plan)


def test_status_codes_are_checked_when_building() -> None:
    @eks.flow(app="web-app", platform="web", now=NOW)
    def calls(s: eks.Session) -> None:
        s.api.get("/x", expect_status=700)

    with pytest.raises(ValueError, match="not an HTTP status code"):
        eks.plan(calls)


def test_literal_phase_is_the_registry_phase() -> None:
    phase: Literal["schema", "api", "finalize", "mongo"] = Create.model_fields["phase"].default
    assert phase == "api"
