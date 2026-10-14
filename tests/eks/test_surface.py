from __future__ import annotations

import inspect
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import eks
from eks.session import Session
from eks.tracing import TraceContext, tracing
from eks_harness.ir.plan import Plan, SessionSpec
from eks_harness.ir.registry import Registry
from eks_harness.ir.surface import (
    EXEMPT,
    OVERLAY_SURFACE,
    SURFACE,
    SurfaceEntry,
    call_text,
    op_text,
)

FAMILIES = ("ui", "web", "mobile", "api", "net", "inject", "capture", "overlay", "fake", "expect", "check",
            "session", "debug", "backend")
CUSTOM = frozenset({"expect.target", "expect.value", "overlay.show", "net.route"})


def resolve(path: str, session: Session) -> object:
    head, *rest = path.split(".")
    roots: dict[str, object] = {"s": session, "d": eks.Data, "e": eks.Edit, "eks": eks, "expect": eks.expect}
    target = roots[head]
    for part in rest[:-1]:
        target = getattr(target, part)
    return inspect.getattr_static(target, rest[-1]) if rest else target


def with_session(registry: Registry, check: Callable[[Session], None]) -> None:
    ctx = TraceContext(kind="flow", name="surface", registry=registry)
    with tracing(ctx):
        check(Session(ctx, SessionSpec.model_validate({"app": "web-app", "platform": "web"})))


def method_params(path: str, session: Session) -> dict[str, inspect.Parameter]:
    found = resolve(path, session)
    function = found.fget if isinstance(found, property) else found
    assert callable(function), path
    return dict(list(inspect.signature(function).parameters.items())[1:])


def test_every_path_resolves_on_the_builders(eks_registry: Registry) -> None:
    def check(session: Session) -> None:
        for op, entry in {**SURFACE, **{f"overlay:{k}": v for k, v in OVERLAY_SURFACE.items()}}.items():
            try:
                resolve(entry.path, session)
            except AttributeError as error:
                pytest.fail(f"{op} -> {entry.path}: {error}")

    with_session(eks_registry, check)


def test_every_session_family_op_has_an_entry_or_an_exemption(eks_registry: Registry) -> None:
    missing = [spec.name for family in FAMILIES for spec in eks_registry.ops(family)
               if spec.name not in SURFACE and spec.name not in EXEMPT]
    assert missing == []
    assert all(reason for reason in EXEMPT.values())
    assert not set(EXEMPT) & set(SURFACE)


def check_signature(op: str, entry: SurfaceEntry, fields: list[str], params: dict[str, inspect.Parameter]) -> None:
    names = [name for name, param in params.items() if param.kind is not inspect.Parameter.VAR_KEYWORD]
    positional = [name.lstrip("*") for name in entry.positional]
    for index, name in enumerate(entry.positional):
        if name.startswith("*"):
            assert params[name[1:]].kind is inspect.Parameter.VAR_POSITIONAL, (op, name)
        else:
            assert names[index] == entry.keyword_renames.get(name, name), (op, name, names)
    for field in fields:
        if field in entry.opaque or field in positional:
            continue
        if entry.keywords is not None and field not in entry.keywords:
            continue
        assert entry.keyword_renames.get(field, field) in names, (op, field, names)


def test_session_entries_match_the_builder_signatures(eks_registry: Registry) -> None:
    def check(session: Session) -> None:
        for op, entry in SURFACE.items():
            if op.split(".")[0] not in FAMILIES or op in CUSTOM or not entry.path.startswith("s."):
                continue
            spec = eks_registry.op(op)
            fields = [info.alias or name for name, info in spec.args.model_fields.items()]
            check_signature(op, entry, fields, method_params(entry.path, session))
        for kind, entry in OVERLAY_SURFACE.items():
            model = eks_registry.catalog("overlay", kind).model
            fields = [info.alias or name for name, info in model.model_fields.items() if name not in ("kind",
                                                                                                    "window")]
            params = method_params(entry.path, session)
            check_signature(kind, entry, fields, params)
            assert entry.keywords is not None
            assert entry.keywords <= set(params), kind
            assert set(fields) & set(params) <= set(entry.keywords) | set(entry.positional), kind

    with_session(eks_registry, check)


ROUND_TRIP = """
import eks
from eks import by, expect, t


@eks.flow(app="web-app", platform="web")
def source(s: eks.Session):
    s.ui.nav("/tenant/branches", {"page": 2}, timeout=t("10s"))
    s.ui.tap("#save", timeout=t("5s"), force=True)
    s.ui.fill(by.label("Ad").nth(1), "Ayşe", submit=True)
    s.ui.type(by.placeholder("Ara").within("#search"), "altın", pace="demo")
    s.ui.key("Enter", "#search")
    s.ui.select("#city", ["İzmir", "Aydın"])
    s.ui.check("#terms", False)
    s.ui.choose("#branch", "Moda")
    s.ui.scroll(to="bottom")
    s.ui.scroll("#list", by=(0, 400))
    s.ui.swipe("left", target=by.role("list"), distance=0.5)
    s.ui.drag("#card", by.css(".column:last-child"))
    s.ui.long_press(by.text("Kaydet", exact=False), hold=t("1500ms"))
    s.ui.tap(by.text("İzin ver").native)
    s.ui.text("#total")
    s.ui.attr("#link", "href")
    s.ui.tree(max_lines=40)
    s.ui.state("cart.items")
    s.ui.wait_for("#toast", state="hidden", timeout=t("3s"))
    s.ui.wait_url("**/done")
    s.ui.settle(strict=False)
    s.web.wheel("#list", dy=200)
    s.web.viewport("mobile")
    s.web.logs("network", limit=5)
    s.api.get("/api/x", query={"a": 1}, headers={"X-Tenant": "harman"}, as_="admin", expect_status=[200, 204])
    s.api.post("/api/branches", json={"name": "Moda", "tags": [1, None, True]}, expect_status=201)
    s.as_("muhasebeci")
    s.eval("document.title", {"a": 1})
    s.inject.script("window.x = 1")
    s.inject.style("body { color: red }")
    s.shot("one", element="#card", full=True, hide=["#a", by.css(".b").within("#c")])
    s.dump("dom", name="page")
    s.har(whole=True)
    s.checkpoint("list", full=True, soft=True)
    s.net.route("GET", "**/api/rates", times=2).respond(status=500, json={"e": 1}, headers={"X-A": "b"})
    s.net.route("POST", "**/x").delay(t("2s"))
    s.net.route("GET", "**/y").abort("timedout")
    s.net.route("PUT", "**/z").patch(lambda_path="items[0].rate", value=3)
    s.net.unroute("**/y")
    s.net.shape("3g")
    s.fake.clear("camera")
    s.overlay.caption("hello", hold=t("2s"), position="top")
    s.overlay.callout("#x", "Here", placement="right", keep=True, key="tip")
    s.overlay.arrow("#a", "#b", "Buraya")
    s.overlay.spotlight("#x", "a", dim=0.5, placement=eks.overlay.Placement(mode="force", side="top", dx=4))
    s.overlay.remove("tip")
    s.overlay.clear()
    expect(by.text("Hi")).visible()
    expect.soft("#n", timeout=t("2s")).text("Hello", message="m")
    expect(by.role("button", "Kaydet")).count(at_least=1)
    expect("#terms").checked(False)
    expect.eventually("#link").attr("href", contains="/branches")
    s.debug.pause_on_error(False)
    s.debug.break_("before-submit")
"""


def run_source(source: str, folder: Path, name: str) -> Plan:
    path = folder / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    namespace = runpy.run_path(str(path), run_name=f"surface_{name}")
    return eks.plan(namespace[name], platform="web")


def nodes_of(plan: Plan) -> list[tuple[str, dict[str, Any]]]:
    return [(node.op, dict(node.args)) for node, _ancestors in plan.walk()]


def test_call_text_round_trips_through_the_builders(eks_registry: Registry, tmp_path: Path) -> None:
    original = nodes_of(run_source(ROUND_TRIP, tmp_path, "source"))
    lines = []
    for op, args in original:
        text = call_text(op, args, registry=eks_registry)
        assert text is not None, (op, args)
        lines.append(f"    {text}")
    rebuilt = "import eks\nfrom eks import by, expect, t\n\n\n@eks.flow(app=\"web-app\", platform=\"web\")\n" \
              "def rebuilt(s: eks.Session):\n" + "\n".join(lines) + "\n"
    assert nodes_of(run_source(rebuilt, tmp_path, "rebuilt")) == original


def test_ops_without_a_literal_call_fall_back_to_s_op(eks_registry: Registry, tmp_path: Path) -> None:
    both = {"target": {"by": "test_id", "args": {"id": "x"}}, "assertion": {"kind": "visible"}, "soft": True,
            "eventually": True}
    assert call_text("expect.target", both, registry=eks_registry) is None
    assert call_text("debug.break", {"label": "x", "when": {"$ref": "n1"}}, registry=eks_registry) is None
    assert call_text("capture.record", {"name": "x"}, registry=eks_registry) is None
    assert call_text("no.such", {}, registry=eks_registry) is None
    assert call_text("ui.tap", {"target": "#x", "unknown": 1}, registry=eks_registry) is None
    assert call_text("backend.info", {}, registry=eks_registry) == "s.backend.info"
    assert call_text("mobile.invoke", {"target": {"by": "test_id", "args": {"id": "x"}}, "prop": "onPress",
                                       "args": [1, "a"]}, registry=eks_registry) == \
        's.mobile.invoke("#x", "onPress", 1, "a")'
    fallback = op_text("expect.target", both)
    assert fallback.startswith('s.op("expect.target", {"target": {"by": "test_id"')
    plan = run_source("import eks\n\n\n@eks.flow(app=\"web-app\", platform=\"web\")\ndef f(s: eks.Session):\n"
                      f"    {fallback}\n", tmp_path, "f")
    assert nodes_of(plan) == [("expect.target", {**both, "assertion": {"kind": "visible"}})]
