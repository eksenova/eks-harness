from collections.abc import Callable
from typing import Any, Literal, cast

import pytest
from pydantic import ValidationError

import eks
from eks import by, t
from eks.ref import Ref, node_of, type_of
from eks.tracing import TraceContext, TraceError, tracing
from eks_harness.ir.ops.overlay import (
    Annotate,
    Arrow,
    Blur,
    Callout,
    Caption,
    Highlight,
    Icon,
    Image,
    OverlayBase,
    Placement,
    Redact,
    Show,
    Spotlight,
    Step,
    Title,
)
from eks_harness.ir.plan import Node, Plan, Requirement
from eks_harness.ir.refs import RefExpr
from eks_harness.ir.registry import RegistryError, contributing, default_registry
from eks_harness.ir.target import Target, TargetError
from eks_harness.ir.time import Span, TimeError
from eks_harness.ir.types import Artifact

TOTAL = {"by": "test_id", "args": {"id": "total"}}


def plan_twice(fn: Callable[..., Any]) -> Plan:
    first = eks.plan(fn, platform="web")
    second = eks.plan(fn, platform="web")
    assert first.to_json() == second.to_json()
    assert first.hash == second.hash
    return first


def overlay_args(node: Node) -> dict[str, Any]:
    return cast(dict[str, Any], node.args["overlay"])


def test_caption_with_a_span_window() -> None:
    caption = eks.overlay.caption("Kontör işlemleri", at=t.span(t("1s"), t("4s")))
    assert isinstance(caption, Caption)
    assert caption.kind == "caption"
    assert caption.text == "Kontör işlemleri"
    assert caption.window == Span(start=t("1s"), end=t("4s"))
    assert caption.hold is None
    assert caption.model_dump(mode="json", exclude_none=True) == {
        "kind": "caption",
        "window": {"start": "1s", "end": "4s"},
        "keep": False,
        "emphasis": "none",
        "text": "Kontör işlemleri",
        "position": "bottom",
    }


def test_time_with_hold_gives_a_closed_window() -> None:
    callout = eks.overlay.callout("#total", "Anlık kur", at=t("1s"), hold=t("2s"))
    assert isinstance(callout, Callout)
    assert callout.anchor == by.test_id("total")
    assert callout.window == Span(start=t("1s"), end=t("3s"))
    assert callout.hold == t("2s")
    # symbolic times add durations to their anchor
    beat = eks.overlay.highlight("#total", at=t("beat:4"), hold=t("2b"))
    assert beat.window == Span(start=t("beat:4"), end=t("beat:4") + t("2b"))


def test_time_without_hold_runs_to_the_end() -> None:
    caption = eks.overlay.caption("Son", at=t("2s"))
    assert caption.window == Span(start=t("2s"), end=t.end)
    assert caption.model_dump(mode="json")["window"] == {"start": "2s", "end": "end"}
    assert eks.overlay.caption("Her zaman").window is None


def test_every_value_builder() -> None:
    placement = Placement(mode="force", side="left", dx=4.0)
    title = eks.overlay.title("EksenGOLD", subtitle="Kuyumcular için", kicker="Yeni", style="kb", emphasis="freeze")
    caption = eks.overlay.caption("Adım", position="top", step=2, keep=True, key="cap", placement=placement)
    callout = eks.overlay.callout(by.text("Toplam"), "Anlık kur", placement=placement)
    spotlight = eks.overlay.spotlight("#total", "Burada", dim=0.6)
    highlight = eks.overlay.highlight("#total", shape="rounded")
    arrow = eks.overlay.arrow("#from", "#to", "Sürükle")
    blur = eks.overlay.blur("#iban", radius=8)
    redact = eks.overlay.redact("#tckn")
    icon = eks.overlay.icon("#total", "check")
    # an image is media: building it records a media.source node, so it needs an active trace
    with tracing(TraceContext(kind="edit", name="images", registry=default_registry())) as ctx:
        image = eks.overlay.image("#total", "assets/logo.png", width=120)
        (source,) = ctx.finalize().nodes
    assert source.args == {"uri": "file:assets/logo.png", "kind": "image"}
    assert image.image == RefExpr.model_validate({"$ref": source.id, "path": []})
    step = eks.overlay.step("#total", 3)
    built: list[OverlayBase] = [title, caption, callout, spotlight, highlight, arrow, blur, redact, icon, image, step]
    kinds = [Title, Caption, Callout, Spotlight, Highlight, Arrow, Blur, Redact, Icon, Image, Step]
    assert [type(model) for model in built] == kinds
    assert (title.subtitle, title.kicker, title.style, title.emphasis) == ("Kuyumcular için", "Yeni", "kb", "freeze")
    assert (caption.position, caption.step, caption.keep, caption.key) == ("top", 2, True, "cap")
    assert caption.placement == placement
    assert callout.anchor == by.text("Toplam")
    assert spotlight.dim == 0.6
    assert highlight.shape == "rounded"
    assert (arrow.anchor, arrow.to, arrow.text) == (by.test_id("from"), by.test_id("to"), "Sürükle")
    assert blur.radius == 8
    assert redact.anchor == by.test_id("tckn")
    assert icon.name == "check"
    assert image.width == 120
    assert step.index == 3
    # every model dispatches back to its own class through the overlay registry
    for model in built:
        show = Show.model_validate({"overlay": model.model_dump(mode="json", exclude_none=True)})
        assert type(show.overlay) is type(model)
        assert show.overlay == model


@pytest.mark.parametrize(
    ("call", "error", "message"),
    [
        (lambda: eks.overlay.caption("x", at=t.span(t("1s"), t("2s")), hold=t("1s")), TypeError, "drop hold"),
        (lambda: eks.overlay.caption("x", at="1s"), TypeError, "at= takes a Time"),  # type: ignore[arg-type]
        (lambda: eks.overlay.caption("x", at=t("1s"), hold="1s"), TypeError, "expects a Time"),  # type: ignore[arg-type]
        (lambda: eks.overlay.caption("x", at=t("1s"), hold=t("beat:2")), TimeError, "duration"),
        (lambda: eks.overlay.caption("x", hold=t("end")), TimeError, "duration"),
        (lambda: eks.overlay.callout(42, "x"), TypeError, "callout anchor"),  # type: ignore[arg-type]
        (lambda: eks.overlay.caption("x", step=0), ValidationError, "step"),
        (lambda: eks.overlay.spotlight("#x", dim=2.0), ValidationError, "dim"),
        (lambda: eks.overlay.icon("#x", ""), ValidationError, "name"),
        (lambda: eks.overlay.caption(5), TypeError, "eks.overlay.caption text must be a string or a Ref"),  # type: ignore[arg-type]
        (lambda: eks.overlay.title("x", subtitle=1), TypeError, "eks.overlay.title subtitle must be a string"),  # type: ignore[arg-type]
        (lambda: eks.overlay.title("x", kicker=[1]), TypeError, "eks.overlay.title kicker must be a string"),  # type: ignore[arg-type]
        (lambda: eks.overlay.callout("#x", 2.5), TypeError, "eks.overlay.callout text must be a string"),  # type: ignore[arg-type]
        (lambda: eks.overlay.arrow("#x", "#y", 3), TypeError, "eks.overlay.arrow text must be a string"),  # type: ignore[arg-type]
    ],
)
def test_value_builder_errors(call: Callable[[], object], error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        call()


def test_live_callout_from_the_score_example() -> None:
    seen: dict[str, Any] = {}

    @eks.flow(app="web-app")
    def live(s: eks.Session) -> None:
        seen["lease"] = s.resources["lease"]
        seen["ref"] = s.overlay.callout("#total", "Anlık kur", at=t("bar:2"))

    plan = plan_twice(live)
    (node,) = plan.nodes
    assert node.op == "overlay.show"
    assert node.pin == t("bar:2")
    assert node.uses == [seen["lease"]]
    assert node.args == {
        "overlay": {
            "kind": "callout",
            "keep": False,
            "emphasis": "none",
            "anchor": TOTAL,
            "text": "Anlık kur",
        }
    }
    show = Show.model_validate(node.args)
    assert isinstance(show.overlay, Callout)
    assert type_of(seen["ref"]) == "none"
    assert node_of(seen["ref"]) == node.id


def test_live_overlays_with_spans_hold_and_refs() -> None:
    @eks.flow(app="web-app")
    def live(s: eks.Session) -> None:
        total = s.record_op("ui.text", {"target": TOTAL})
        s.overlay.caption("Kontör", at=t.span(t("1s"), t("4s")))
        s.overlay.highlight("#total", hold=t("2s"), keep=True, key="vurgu")
        s.overlay.spotlight("#total", f"Toplam {total}")
        s.overlay.title("Başlık")
        s.overlay.arrow("#a", "#b", at=t("beat:3"))
        s.overlay.blur("#iban")
        s.overlay.redact("#tckn")
        s.overlay.icon("#ok", "check")
        s.overlay.image("#ok", "assets/logo.png")
        s.overlay.step("#ok", 1)

    plan = plan_twice(live)
    read, caption, highlight, spotlight, *rest = [node for node in plan.nodes if node.op != "media.source"]
    (logo,) = [node for node in plan.nodes if node.op == "media.source"]
    assert caption.pin == t("1s")
    assert overlay_args(caption)["window"] == {"start": "1s", "end": "4s"}
    assert highlight.pin is None
    assert overlay_args(highlight)["hold"] == "2s"
    assert overlay_args(highlight)["keep"] is True
    assert overlay_args(highlight)["key"] == "vurgu"
    assert "window" not in overlay_args(highlight)
    assert overlay_args(spotlight)["text"] == {"$fmt": ["Toplam ", {"$ref": read.id, "path": []}]}
    assert read.id in spotlight.after
    assert [overlay_args(node)["kind"] for node in rest] == [
        "title", "arrow", "blur", "redact", "icon", "image", "step"]
    assert overlay_args(rest[5])["image"] == {"$ref": logo.id, "path": []}
    assert logo.id in rest[5].after
    arrow = rest[1]
    # a Time at= on a live overlay is the pin only, not a window
    assert arrow.pin == t("beat:3")
    assert "window" not in overlay_args(arrow)


def test_show_remove_and_clear() -> None:
    @eks.flow(app="web-app")
    def manage(s: eks.Session) -> None:
        kept = eks.overlay.highlight("#total", keep=True, key="vurgu")
        s.overlay.show(kept)
        s.overlay.show(eks.overlay.caption("Ara"), at=t("2s"))
        s.overlay.show(eks.overlay.caption("Pencere"), at=t.span(t("3s"), t("5s")))
        s.overlay.remove("vurgu")
        s.overlay.remove("vurgu", at=t("6s"))
        s.overlay.clear("caption")
        s.overlay.clear(at=t("7s"))

    plan = plan_twice(manage)
    kept, timed, windowed, remove, remove_at, clear_kind, clear_all = plan.nodes
    assert kept.pin is None
    assert overlay_args(kept)["key"] == "vurgu"
    assert timed.pin == t("2s")
    assert "window" not in overlay_args(timed)
    assert windowed.pin == t("3s")
    assert overlay_args(windowed)["window"] == {"start": "3s", "end": "5s"}
    assert (remove.op, remove.args, remove.pin) == ("overlay.remove", {"key": "vurgu"}, None)
    assert remove_at.pin == t("6s")
    assert (clear_kind.op, clear_kind.args) == ("overlay.clear", {"kind": "caption"})
    assert clear_all.args == {}
    assert clear_all.pin == t("7s")


def test_live_overlay_errors() -> None:
    @eks.flow(app="web-app")
    def not_a_model(s: eks.Session) -> None:
        s.overlay.show("caption")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="overlay models"):
        eks.plan(not_a_model, platform="web")

    @eks.flow(app="web-app")
    def span_and_hold(s: eks.Session) -> None:
        s.overlay.caption("x", at=t.span(t("1s"), t("2s")), hold=t("1s"))

    with pytest.raises(TypeError, match="drop hold"):
        eks.plan(span_and_hold, platform="web")

    @eks.flow(app="web-app")
    def unknown_kind(s: eks.Session) -> None:
        s.overlay.clear("confetti")

    with pytest.raises(RegistryError, match="confetti"):
        eks.plan(unknown_kind, platform="web")

    @eks.flow(app="web-app")
    def empty_key(s: eks.Session) -> None:
        s.overlay.remove("")

    with pytest.raises(TypeError, match="key"):
        eks.plan(empty_key, platform="web")

    @eks.flow(app="web-app")
    def string_at(s: eks.Session) -> None:
        s.overlay.show(eks.overlay.caption("x"), at="1s")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="at= takes a Time"):
        eks.plan(string_at, platform="web")


def test_annotate_records_a_pure_node() -> None:
    seen: dict[str, Any] = {}

    @eks.flow(app="web-app")
    def annotated(s: eks.Session) -> None:
        shot_ref = s.shot("liste")
        seen["ref"] = eks.annotate(shot_ref, [eks.overlay.highlight("#x")], style="kb")
        eks.annotate(shot_ref, [eks.overlay.callout("#total", "Kur", placement=Placement(side="top")),
                                eks.overlay.redact(by.css(".iban"))])

    plan = plan_twice(annotated)
    shot, first, second = plan.nodes
    assert first.op == "overlay.annotate"
    assert first.args == {
        "artifact": {"$ref": shot.id, "path": []},
        "overlays": [{
            "kind": "highlight",
            "keep": False,
            "emphasis": "none",
            "anchor": {"by": "test_id", "args": {"id": "x"}},
            "shape": "box",
        }],
        "style": "kb",
    }
    # pure: no resources and no program-order edge, only the artifact it reads
    assert first.uses == []
    assert first.after == [shot.id]
    assert second.after == [shot.id]
    assert first.out == "artifact"
    assert type_of(seen["ref"]) == "artifact"
    assert "style" not in second.args
    model = Annotate.model_validate(second.args)
    assert [type(item) for item in model.overlays] == [Callout, Redact]


def test_annotate_errors() -> None:
    @eks.flow(app="web-app")
    def title_on_shot(s: eks.Session) -> None:
        eks.annotate(s.shot("x"), [eks.overlay.title("Başlık")])

    with pytest.raises(TraceError, match="no screenshot renderer"):
        eks.plan(title_on_shot, platform="web")

    @eks.flow(app="web-app")
    def blender_anchor(s: eks.Session) -> None:
        eks.annotate(s.shot("x"), [eks.overlay.highlight(by.object("Phone"))])

    with pytest.raises(TargetError, match="screenshot"):
        eks.plan(blender_anchor, platform="web")

    @eks.flow(app="web-app")
    def single(s: eks.Session) -> None:
        eks.annotate(s.shot("x"), eks.overlay.highlight("#x"))  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="list of overlays"):
        eks.plan(single, platform="web")

    @eks.flow(app="web-app")
    def empty(s: eks.Session) -> None:
        eks.annotate(s.shot("x"), [])

    with pytest.raises(ValidationError):
        eks.plan(empty, platform="web")

    @eks.flow(app="web-app")
    def not_a_ref(s: eks.Session) -> None:
        eks.annotate("artifact:ABC", [eks.overlay.highlight("#x")])  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="screenshot Ref"):
        eks.plan(not_a_ref, platform="web")

    with pytest.raises(TraceError, match="eks.plan"):
        eks.annotate(eks.Ref("n0000000000", "artifact"), [eks.overlay.highlight("#x")])


def test_plugin_overlay_kinds_follow_their_renderers() -> None:
    @eks.overlay_kind(video="acme.confetti", description="Confetti burst")
    class Confetti(OverlayBase, frozen=True):
        kind: Literal["confetti"] = "confetti"
        anchor: Target

    @eks.overlay_kind(live="acme.lasso", screenshot="acme.lasso", description="Lasso around targets")
    class Lasso(OverlayBase, frozen=True):
        kind: Literal["lasso"] = "lasso"
        targets: tuple[Target, ...]

    @eks.flow(app="web-app")
    def video_only(s: eks.Session) -> None:
        s.overlay.show(Confetti(anchor=by.test_id("total")))

    with pytest.raises(TraceError, match="no live renderer"):
        eks.plan(video_only, platform="web")

    @eks.flow(app="web-app")
    def lasso_ok(s: eks.Session) -> None:
        s.overlay.show(Lasso(targets=(by.test_id("a"), by.text("B"))))
        eks.annotate(s.shot("x"), [Lasso(targets=(by.test_id("a"),))])

    plan = plan_twice(lasso_ok)
    show, _, annotate = plan.nodes
    assert overlay_args(show)["kind"] == "lasso"
    assert isinstance(Show.model_validate(show.args).overlay, Lasso)
    overlays = cast(list[dict[str, Any]], annotate.args["overlays"])
    assert overlays[0]["targets"] == [{"by": "test_id", "args": {"id": "a"}}]

    @eks.flow(app="web-app")
    def lasso_bad(s: eks.Session) -> None:
        eks.annotate(s.shot("x"), [Lasso(targets=(by.test_id("a"), by.layer("logo")))])

    with pytest.raises(TargetError, match=r"targets\[1\]"):
        eks.plan(lasso_bad, platform="web")


def test_plugin_overlay_and_check_kinds_are_required_by_the_plan() -> None:
    with contributing("acme.tools", "1.2.0"):

        @eks.overlay_kind(live="acme.lasso", screenshot="acme.lasso", description="Lasso around targets")
        class Lasso(OverlayBase, frozen=True):
            kind: Literal["lasso"] = "lasso"
            targets: tuple[Target, ...]

        @eks.check("acme_contrast", platforms=["web"], description="Text contrast ratio")
        class AcmeContrast(eks.Args):
            ratio: float = 4.5

    acme = [Requirement(plugin="acme.tools", version="1.2.0")]

    @eks.flow(app="web-app")
    def shown(s: eks.Session) -> None:
        s.overlay.show(Lasso(targets=(by.test_id("a"),)))

    @eks.flow(app="web-app")
    def annotated(s: eks.Session) -> None:
        eks.annotate(s.shot("x"), [Lasso(targets=(by.test_id("a"),))])

    @eks.flow(app="web-app")
    def cleared(s: eks.Session) -> None:
        s.overlay.clear("lasso")

    @eks.flow(app="web-app")
    def checked(s: eks.Session) -> None:
        s.checkpoint("renk", checks=[AcmeContrast(ratio=7.0)])

    @eks.flow(app="web-app")
    def everything(s: eks.Session) -> None:
        s.overlay.show(Lasso(targets=(by.test_id("a"),)))
        eks.annotate(s.shot("x"), [Lasso(targets=(by.test_id("a"),))])
        s.overlay.clear("lasso")
        s.checkpoint("renk", checks=["acme_contrast", "console_errors"])

    for flow in (shown, annotated, cleared, checked, everything):
        assert plan_twice(flow).requires == acme

    # built-in overlay kinds and checks never add a requirement
    @eks.flow(app="web-app")
    def builtin(s: eks.Session) -> None:
        s.overlay.callout("#total", "Anlık kur")
        eks.annotate(s.shot("x"), [eks.overlay.highlight("#x")])
        s.overlay.clear("caption")
        s.checkpoint("liste", checks=["console_errors", eks.checks.use("visual", threshold=0.01)])

    assert plan_twice(builtin).requires == []


def recorded(s: eks.Session) -> Any:
    with s.record("demo") as video:
        s.shot("inside")
    return video


@pytest.mark.parametrize(
    ("producer", "op"),
    [
        (recorded, "capture.record"),
        (lambda s: s.har(), "capture.har"),
        (lambda s: s.dump("dom"), "capture.dump"),
    ],
)
def test_annotate_rejects_artifacts_that_are_not_screenshots(
    producer: Callable[[eks.Session], Any], op: str
) -> None:
    @eks.flow(app="web-app")
    def wrong(s: eks.Session) -> None:
        eks.annotate(producer(s), [eks.overlay.highlight("#x")])

    with pytest.raises(TypeError, match=rf"comes from '{op}'.*s\.shot\(\.\.\.\)"):
        eks.plan(wrong, platform="web")


def test_annotate_accepts_checkpoints_and_earlier_annotations() -> None:
    @eks.flow(app="web-app")
    def layered(s: eks.Session) -> None:
        # a checkpoint returns its results as JSON, which carry the evidence shot
        shot = cast(Ref[Artifact], s.checkpoint("liste"))
        once = eks.annotate(shot, [eks.overlay.highlight("#x")])
        eks.annotate(once, [eks.overlay.highlight("#y")])

    plan = plan_twice(layered)
    assert [node.op for node in plan.nodes] == ["check.checkpoint", "overlay.annotate", "overlay.annotate"]
