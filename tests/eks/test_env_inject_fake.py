from typing import Any, Literal, cast

import pytest
from pydantic import ValidationError

import eks
from eks import t
from eks.ref import Ref, node_of, type_of
from eks.tracing import TraceError, current
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import Registry
from eks_harness.ir.types import MediaInfo


def lease_of(plan: Plan) -> str:
    return next(resource.id for resource in plan.resources if resource.kind.startswith("lease."))


def media_source(uri: str) -> eks.Media:
    return cast("Ref[MediaInfo]", current().record("media.source", {"uri": uri}))


def test_inject_style_and_script_options(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def styled(s: eks.Session) -> None:
        theme = s.api.get("/api/theme")
        s.inject.style("* { transition: none !important; }")
        s.inject.style(f"body {{ background: {theme.color}; }}", at=t("1s"))
        s.inject.script("window.scrollTo(0, 0)", when="now", at=t("2s"))

    plan = eks.plan(styled)
    theme, plain, dynamic, script = plan.nodes
    assert plain.op == dynamic.op == "inject.style"
    assert plain.args == {"css": "* { transition: none !important; }"}
    assert plain.uses == [lease_of(plan)]
    assert dynamic.args == {"css": {"$fmt": ["body { background: ", {"$ref": theme.id, "path": ["color"]}, "; }"]}}
    assert dynamic.pin == t("1s")
    assert theme.id in dynamic.after
    assert script.args == {"source": "window.scrollTo(0, 0)", "when": "now"}
    assert script.pin == t("2s")


def test_inject_rejects_non_text(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def bad_script(s: eks.Session) -> None:
        s.inject.script(42)  # pyright: ignore[reportArgumentType]

    @eks.flow(app="web-app", platform="web")
    def bad_when(s: eks.Session) -> None:
        s.inject.script("x()", when="later")  # pyright: ignore[reportArgumentType]

    @eks.flow(app="web-app", platform="web")
    def bad_eval(s: eks.Session) -> None:
        s.eval(None)  # pyright: ignore[reportArgumentType]

    with pytest.raises(TypeError, match="source text"):
        eks.plan(bad_script)
    with pytest.raises(ValidationError):
        eks.plan(bad_when)
    with pytest.raises(TypeError, match="source text"):
        eks.plan(bad_eval)


def test_eval_returns_a_json_ref_usable_later(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def evaluated(s: eks.Session) -> dict[str, Any]:
        user = s.api.get("/api/me")
        flags = s.eval("(arg) => window.__flags[arg.key]", {"key": "beta", "user": user.id})
        width = s.eval("window.innerWidth")
        s.api.post("/api/telemetry", json={"beta": flags.enabled, "width": width})
        return {"flags": flags}

    plan = eks.plan(evaluated)
    user, flags, width, post = plan.nodes
    assert flags.op == width.op == "inject.eval"
    assert flags.out == "json"
    assert flags.args == {
        "source": "(arg) => window.__flags[arg.key]",
        "arg": {"key": "beta", "user": {"$ref": user.id, "path": ["id"]}},
    }
    assert width.args == {"source": "window.innerWidth"}
    assert flags.uses == [lease_of(plan)]
    assert post.args["json"] == {
        "beta": {"$ref": flags.id, "path": ["enabled"]},
        "width": {"$ref": width.id, "path": []},
    }
    assert post.after == [width.id, flags.id]
    assert plan.outputs == {"flags": {"$ref": flags.id, "path": []}}


@pytest.mark.parametrize("platform", ["ios", "android"])
def test_init_scripts_are_web_only(eks_registry: Registry, platform: Literal["ios", "android"]) -> None:
    @eks.flow(app="mobile-app", platform=platform)
    def native(s: eks.Session) -> None:
        s.inject.script("x()", when="init")

    with pytest.raises(TraceError, match=f"exists only on web; this {platform} session can use when='now'"):
        eks.plan(native)


def test_now_scripts_run_on_native_sessions(eks_registry: Registry) -> None:
    @eks.flow(app="mobile-app", platform="ios")
    def native(s: eks.Session) -> None:
        s.inject.script("x()")

    (node,) = eks.plan(native).nodes
    assert node.args == {"source": "x()", "when": "now"}


def test_eval_ref_type(eks_registry: Registry) -> None:
    seen: list[Any] = []

    @eks.flow(app="mobile-app", platform="ios")
    def store(s: eks.Session) -> None:
        seen.append(s.eval("globalThis.store.getState()"))

    plan = eks.plan(store)
    (node,) = plan.nodes
    assert node_of(seen[0]) == node.id
    assert type_of(seen[0]) == "json"


def test_fake_arm_camera_with_media_on_ios(eks_registry: Registry) -> None:
    @eks.flow(app="mobile-app", platform="ios")
    def camera(s: eks.Session) -> None:
        photo = media_source("artifact:IMG1")
        s.fake.arm("camera", media=photo, mode="success")

    plan = eks.plan(camera)
    source, arm = plan.nodes
    assert source.op == "media.source"
    assert arm.op == "fake.arm"
    assert arm.args == {"slot": "camera", "mode": "success", "media": {"$ref": source.id, "path": []}}
    assert arm.uses == [lease_of(plan)]
    assert source.id in arm.after


def test_fake_fixture_data_modes_and_pins(eks_registry: Registry) -> None:
    @eks.flow(app="mobile-app", platform="android")
    def fakes(s: eks.Session) -> None:
        s.fake.arm("document", fixture="kimlik-on.jpg")
        s.fake.arm("nfc", mode="error", data={"code": "TAG_LOST"}, at=t("4s"))
        s.fake.arm("gallery", mode="cancel")
        s.fake.clear("nfc")
        s.fake.clear(at=t("9s"))

    plan = eks.plan(fakes)
    document, nfc, gallery, clear_one, clear_all = plan.nodes
    assert document.args == {"slot": "document", "mode": "success", "fixture": "kimlik-on.jpg"}
    assert nfc.args == {"slot": "nfc", "mode": "error", "data": {"code": "TAG_LOST"}}
    assert nfc.pin == t("4s")
    assert gallery.args == {"slot": "gallery", "mode": "cancel"}
    assert clear_one.op == clear_all.op == "fake.clear"
    assert clear_one.args == {"slot": "nfc"}
    assert clear_all.args == {}
    assert clear_all.pin == t("9s")
    assert nfc.after == [document.id]
    assert clear_all.after == [clear_one.id]


@pytest.mark.parametrize(
    ("arm", "error", "match"),
    [
        (lambda s: s.fake.arm("camera", media="photo.jpg"), TypeError, "fixture="),
        (lambda s: s.fake.arm("camera", media=42), TypeError, r"eks\.media\.source"),
        (lambda s: s.fake.arm("camera", media=s.api.get("/x").count.len()), TypeError, "media ref"),
        (lambda s: s.fake.arm("camera", media=media_source("artifact:A"), fixture="a.jpg"), TypeError, "not both"),
        (lambda s: s.fake.arm("camera", fixture=3), TypeError, "file name"),
        (lambda s: s.fake.arm("Camera"), ValidationError, "slot"),
        (lambda s: s.fake.arm("camera", mode="maybe"), ValidationError, "mode"),
        (lambda s: s.fake.clear("Bad Slot"), ValidationError, "slot"),
        (lambda s: s.fake.arm("camera", at=4), TypeError, "bare numbers"),
    ],
)
def test_bad_fake_calls(eks_registry: Registry, arm: Any, error: type[Exception], match: str) -> None:
    @eks.flow(app="mobile-app", platform="ios")
    def bad(s: eks.Session) -> None:
        arm(s)

    with pytest.raises(error, match=match):
        eks.plan(bad)


def test_fake_accepts_an_encodable_media_handle(eks_registry: Registry) -> None:
    class Handle:
        def __init__(self, ref: Ref[Any]) -> None:
            self.ref = ref

        def __eks_encode__(self) -> Any:
            return {"$ref": node_of(self.ref), "path": []}

    @eks.flow(app="mobile-app", platform="ios")
    def handled(s: eks.Session) -> None:
        s.fake.arm("camera", media=Handle(media_source("artifact:IMG2")))  # pyright: ignore[reportArgumentType]

    source, arm = eks.plan(handled).nodes
    assert arm.args["media"] == {"$ref": source.id, "path": []}
