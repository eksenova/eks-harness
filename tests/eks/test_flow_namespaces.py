# pyright: reportUnnecessaryTypeIgnoreComment=true
import textwrap
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import eks
from eks import by, t
from eks.discovery import load_file
from eks.ref import Ref, type_of
from eks.tracing import TraceError
from eks_harness.ir.hashing import resource_id
from eks_harness.ir.plan import Node, Plan, Requirement
from eks_harness.ir.registry import (
    PROJECT_NAMESPACE,
    SESSION_MEMBERS,
    Registry,
    RegistryError,
    contributing,
)

LEASE = resource_id("lease.web", {"app": "web-app", "session": "main"})
CREATE = {"by": "test_id", "args": {"id": "create"}}


def twice(fn: Any, **params: Any) -> Plan:
    first = eks.plan(fn, **params)
    second = eks.plan(fn, **params)
    assert first.to_json() == second.to_json()
    assert first.hash == second.hash
    return first


def body(node: Node) -> list[Node]:
    assert node.branches is not None
    return node.branches["body"]


class Ping(eks.Args):
    host: str
    count: int = 1


class DemoPress(eks.Args):
    target: eks.Target
    times: int = 1


def register_demo_ops() -> tuple[type[Ping], type[DemoPress]]:
    # registered per test: the eks_registry fixture restores the registry afterwards
    eks.op("demo", out="json", description="Ping a host")(Ping)
    eks.op("demo", name="press", resources=("lease",), pinnable=True, description="Press a demo button")(DemoPress)
    return Ping, DemoPress


# ---------------------------------------------------------------- s.op and s.call


def test_op_by_name_equals_the_builder() -> None:
    @eks.flow(app="web-app", platform="web")
    def both(s: eks.Session) -> None:
        s.ui.tap("#create")
        s.op("ui.tap", target=by.test_id("create"))
        s.op("ui.tap", target={"by": "test_id", "args": {"id": "create"}}, force=False, settle=True)

    builder, dynamic, raw = twice(both).nodes
    expected = (builder.op, builder.args, builder.out, builder.uses)
    assert (dynamic.op, dynamic.args, dynamic.out, dynamic.uses) == expected
    assert (raw.op, raw.args, raw.out, raw.uses) == expected
    assert builder.args == {"target": CREATE, "force": False, "settle": True}
    assert dynamic.after == [builder.id]
    assert len({builder.id, dynamic.id, raw.id}) == 3


def test_op_by_name_returns_a_ref_typed_by_out() -> None:
    seen: list[Ref[Any]] = []

    @eks.flow(app="web-app", platform="web")
    def reads(s: eks.Session) -> None:
        seen.append(s.op("ui.count", target=by.test_id("row")))
        seen.append(s.op("ui.url"))

    count, url = eks.plan(reads).nodes
    assert [type_of(ref) for ref in seen] == ["int", "str"]
    assert (count.out, url.out) == ("int", "str")


def test_op_by_name_errors() -> None:
    @eks.flow(app="web-app", platform="web")
    def unknown(s: eks.Session) -> None:
        s.op("ui.tapp", target=by.test_id("x"))

    with pytest.raises(RegistryError, match="did you mean 'ui.tap'"):
        eks.plan(unknown)

    @eks.flow(app="web-app", platform="web")
    def container(s: eks.Session) -> None:
        s.op("ctl.parallel")

    with pytest.raises(TraceError, match="container op"):
        eks.plan(container)


def test_call_records_the_op_of_the_model() -> None:
    ping, press = register_demo_ops()

    @eks.flow(app="web-app", platform="web")
    def typed(s: eks.Session) -> None:
        s.call(ping(host="api.eksenova.local"))
        s.call(press(target=by.test_id("create"), times=2), at=t("beat:4"))

    first, second = twice(typed).nodes
    assert (first.op, first.args, first.out, first.uses) == (
        "demo.ping", {"host": "api.eksenova.local", "count": 1}, "json", [])
    assert (second.op, second.args, second.uses) == ("demo.press", {"target": CREATE, "times": 2}, [LEASE])
    assert second.pin == t("beat:4")
    assert second.after == [first.id]


def test_call_errors() -> None:
    class Unregistered(eks.Args):
        host: str

    @eks.flow(app="web-app", platform="web")
    def unregistered(s: eks.Session) -> None:
        s.call(Unregistered(host="x"))

    with pytest.raises(RegistryError, match=r"no op takes .*Unregistered"):
        eks.plan(unregistered)

    class Shared(eks.Args):
        value: int = 0

    eks.op("demo", name="first")(Shared)
    eks.op("demo", name="second")(Shared)

    @eks.flow(app="web-app", platform="web")
    def ambiguous(s: eks.Session) -> None:
        s.call(Shared())

    with pytest.raises(RegistryError, match=r"several ops \(demo.first, demo.second\)"):
        eks.plan(ambiguous)

    @eks.flow(app="web-app", platform="web")
    def not_a_model(s: eks.Session) -> None:
        s.call({"host": "x"})  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="s.call takes an instance"):
        eks.plan(not_a_model)

    _ping, press = register_demo_ops()

    @eks.flow(app="web-app", platform="web")
    def numeric_pin(s: eks.Session) -> None:
        s.call(press(target=by.test_id("create")), at=3)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match=r"s.call\(demo.press\) at expects a Time"):
        eks.plan(numeric_pin)


# ---------------------------------------------------------------- plugin op namespaces


def test_plugin_ops_through_their_namespace() -> None:
    register_demo_ops()
    seen: list[Ref[Any]] = []

    @eks.flow(app="web-app", platform="web")
    def namespaced(s: eks.Session) -> None:
        seen.append(s.demo.ping(host="h1", count=3))
        s.demo.press(target=by.test_id("create"))
        s.op("demo.ping", host="h1", count=3)

    via_namespace, press, via_op = twice(namespaced).nodes
    assert (via_namespace.op, via_namespace.args) == ("demo.ping", {"host": "h1", "count": 3})
    assert (via_op.op, via_op.args) == (via_namespace.op, via_namespace.args)
    assert (press.op, press.args, press.uses) == ("demo.press", {"target": CREATE, "times": 1}, [LEASE])
    assert type_of(seen[0]) == "json"


def test_op_by_name_parses_target_shorthands_like_the_builder() -> None:
    register_demo_ops()

    @eks.flow(app="web-app", platform="web")
    def shorthands(s: eks.Session) -> None:
        s.ui.tap("#create")
        s.op("ui.tap", target="#create")
        s.demo.press(target="#create")
        s.ui.scroll(to="bottom")
        s.op("ui.scroll", to="bottom")
        s.ui.scroll(to="#row")
        s.op("ui.scroll", to="#row")
        s.ui.drag("#a", "Kaydet")
        s.op("ui.drag", source="#a", to="Kaydet")

    nodes = twice(shorthands).nodes
    tap, op_tap, press, edge, op_edge, row, op_row, drag, op_drag = nodes
    assert (op_tap.op, op_tap.args) == (tap.op, tap.args)
    assert (press.op, press.args) == ("demo.press", {"target": CREATE, "times": 1})
    assert edge.args == op_edge.args
    assert edge.args["to"] == "bottom"
    assert row.args == op_row.args
    assert row.args["to"] == {"by": "test_id", "args": {"id": "row"}}
    assert drag.args == op_drag.args
    assert op_drag.args["to"] == {"by": "text", "args": {"text": "Kaydet", "exact": True}}


def test_op_by_name_parses_plugin_shorthands_and_target_sequences() -> None:
    @eks.target_kind("acme_chip", surfaces=["web"], shorthand="chip")
    class AcmeChip(BaseModel, frozen=True, extra="forbid"):
        label: str

    class Masked(eks.Args):
        masks: tuple[eks.Target, ...] = ()
        region: eks.Target | None = None

    eks.op("demo", name="masked", description="Masked demo op")(Masked)

    @eks.flow(app="web-app", platform="web")
    def plugin(s: eks.Session) -> None:
        s.op("ui.tap", target="chip=Yeni")
        s.op("demo.masked", masks=["#a", by.text("b")], region="chip=Yeni")
        s.demo.masked(masks=("#c",))

    tap, masked, namespaced = twice(plugin).nodes
    chip = {"by": "acme_chip", "args": {"label": "Yeni"}}
    assert tap.args["target"] == chip
    assert masked.args == {
        "masks": [{"by": "test_id", "args": {"id": "a"}}, {"by": "text", "args": {"text": "b", "exact": True}}],
        "region": chip,
    }
    assert namespaced.args == {"masks": [{"by": "test_id", "args": {"id": "c"}}]}
    del AcmeChip


type Anchor = eks.Target | None
type Anchors = tuple[Anchor, ...]
type Boxed[T] = T | None
type Same[T] = T
type Many[T] = list[T]


def test_op_by_name_parses_shorthands_through_type_aliases() -> None:
    class Aliased(eks.Args):
        anchor: Anchor = None
        anchors: tuple[Anchor, ...] = ()
        listed: Anchors = ()
        boxed: Boxed[eks.Target] = None
        same: Same[eks.Target] | None = None
        many: Many[eks.Target] | None = None

    eks.op("demo", name="aliased", description="Aliased demo op")(Aliased)

    @eks.flow(app="web-app", platform="web")
    def aliased(s: eks.Session) -> None:
        s.op("demo.aliased", anchor="#a", anchors=("#b", "text=c"), listed=["#d"], boxed="#e",
             same="#f", many=["#g"])
        s.demo.aliased(anchor="#a")

    by_name, namespaced = twice(aliased).nodes
    assert by_name.args == {
        "anchor": {"by": "test_id", "args": {"id": "a"}},
        "anchors": [{"by": "test_id", "args": {"id": "b"}}, {"by": "text", "args": {"text": "c", "exact": True}}],
        "listed": [{"by": "test_id", "args": {"id": "d"}}],
        "boxed": {"by": "test_id", "args": {"id": "e"}},
        "same": {"by": "test_id", "args": {"id": "f"}},
        "many": [{"by": "test_id", "args": {"id": "g"}}],
    }
    assert namespaced.args == {"anchor": {"by": "test_id", "args": {"id": "a"}}, "anchors": [], "listed": []}


def test_op_by_name_rejects_non_target_values_with_the_field_name() -> None:
    @eks.flow(app="web-app", platform="web")
    def wrong(s: eks.Session) -> None:
        s.op("ui.tap", target=42)

    with pytest.raises(TypeError, match=r"s.op\('ui.tap'\) target must be a Target or a target string, got int 42"):
        eks.plan(wrong)


def test_namespace_op_arguments_are_validated() -> None:
    register_demo_ops()

    @eks.flow(app="web-app", platform="web")
    def missing(s: eks.Session) -> None:
        s.demo.ping(count=2)

    with pytest.raises(ValueError, match="host"):
        eks.plan(missing)

    @eks.flow(app="web-app", platform="web")
    def positional(s: eks.Session) -> None:
        s.demo.ping("h1")

    with pytest.raises(TypeError):
        eks.plan(positional)


def test_unknown_namespace_attributes_suggest_names() -> None:
    register_demo_ops()

    @eks.macro(namespace="demo")
    def warm_up(s: eks.Session) -> None:
        s.ui.settle()

    @eks.flow(app="web-app", platform="web")
    def typo(s: eks.Session) -> None:
        s.demo.pnig(host="x")

    with pytest.raises(AttributeError, match=r"s.demo has no macro or op 'pnig'; did you mean s.demo.ping"):
        eks.plan(typo)

    @eks.flow(app="web-app", platform="web")
    def unrelated(s: eks.Session) -> None:
        s.demo.zzz()

    with pytest.raises(AttributeError, match=r"known names: ping, press, warm_up"):
        eks.plan(unrelated)

    @eks.flow(app="web-app", platform="web")
    def missing_space(s: eks.Session) -> None:
        s.dmeo.ping(host="x")

    with pytest.raises(AttributeError, match=r"s.dmeo is not a macro namespace or op family; did you mean s.demo"):
        eks.plan(missing_space)

    @eks.flow(app="web-app", platform="web")
    def nothing_close(s: eks.Session) -> None:
        s.qqqqqq.anything()

    with pytest.raises(AttributeError, match=r"@eks.macro\(namespace='qqqqqq'\)"):
        eks.plan(nothing_close)


def test_private_names_raise_attribute_error() -> None:
    names: list[str] = []

    @eks.flow(app="web-app", platform="web")
    def private(s: eks.Session) -> None:
        with pytest.raises(AttributeError, match="'_x'"):
            s._x  # noqa: B018
        with pytest.raises(AttributeError, match="'_y'"):
            s.demo._y  # noqa: B018
        assert not hasattr(s, "__eks_encode__")
        names.append(repr(s.demo))
        with pytest.raises(AttributeError, match="no settable attribute"):
            s.demo.x = 1

    eks.plan(private)
    assert names == ["Namespace('demo')"]


# ---------------------------------------------------------------- macros


def test_macro_records_a_container_and_returns_its_value() -> None:
    @eks.macro(namespace="acme", description="Log in as a persona")
    def login(s: eks.Session, persona: str) -> Ref[str]:
        s.ui.fill("#username", persona)
        s.ui.tap("#login")
        return s.ui.text("#welcome")

    returned: list[Ref[Any]] = []

    @eks.flow(app="web-app", platform="web")
    def logs_in(s: eks.Session) -> Ref[str]:
        s.ui.nav("/login")
        welcome = s.acme.login("harman-admin")
        returned.append(welcome)
        s.ui.fill("#greeting", welcome)
        return welcome

    plan = twice(logs_in)
    nav, macro, fill = plan.nodes
    assert macro.op == "ctl.macro"
    assert macro.args == {"name": "acme.login", "version": "1", "params": {"persona": "harman-admin"}}
    assert macro.after == [nav.id]
    assert macro.uses == []
    username, tap, text = body(macro)
    assert [node.op for node in body(macro)] == ["ui.fill", "ui.tap", "ui.text"]
    assert username.args["value"] == "harman-admin"
    assert username.after == []
    assert tap.after == [username.id]
    assert type_of(returned[0]) == "str"
    assert fill.args["value"] == {"$ref": text.id, "path": []}
    assert fill.after == [macro.id, text.id]
    assert plan.outputs == {"$ref": text.id, "path": []}
    assert plan.requires == []


def test_macro_keyword_arguments_defaults_and_refs() -> None:
    @eks.macro(namespace="acme", version="3")
    def open_branch(s: eks.Session, name: str, *, tab: str = "summary", when: eks.Time | None = None) -> None:
        s.ui.nav(f"/tenant/branches/{name}/{tab}")

    @eks.flow(app="web-app", platform="web")
    def opens(s: eks.Session) -> None:
        current = s.ui.text("#branch-name")
        s.acme.open_branch(current)
        s.acme.open_branch(name="Kadıköy", tab="invoices", when=t("1s"))

    text, first, second = twice(opens).nodes
    assert first.args == {
        "name": "acme.open_branch", "version": "3",
        "params": {"name": {"$ref": text.id, "path": []}, "tab": "summary", "when": None},
    }
    assert first.after == [text.id]
    assert second.args["params"] == {"name": "Kadıköy", "tab": "invoices", "when": "1s"}
    (nav,) = body(second)
    assert nav.args == {"route": "/tenant/branches/Kadıköy/invoices"}
    (inner,) = body(first)
    assert inner.args["route"] == {"$fmt": ["/tenant/branches/", {"$ref": text.id, "path": []}, "/summary"]}


def test_macro_call_errors() -> None:
    @eks.macro(namespace="acme", platforms=["ios", "android"])
    def native_only(s: eks.Session) -> None:
        s.mobile.close_dev_menu()

    @eks.macro(namespace="acme")
    def needs_persona(s: eks.Session, persona: str) -> None:
        s.ui.tap(f"text={persona}")

    @eks.flow(app="web-app", platform="web")
    def wrong_platform(s: eks.Session) -> None:
        s.acme.native_only()

    with pytest.raises(TraceError, match="macro acme.native_only runs on android, ios, not on web"):
        eks.plan(wrong_platform)

    @eks.flow(app="mobile-app", platform="ios")
    def right_platform(s: eks.Session) -> None:
        s.acme.native_only()

    (macro,) = eks.plan(right_platform).nodes
    assert [node.op for node in body(macro)] == ["mobile.close_dev_menu"]

    @eks.flow(app="web-app", platform="web")
    def missing_argument(s: eks.Session) -> None:
        s.acme.needs_persona()

    with pytest.raises(TypeError, match="macro acme.needs_persona: missing a required argument: 'persona'"):
        eks.plan(missing_argument)

    @eks.flow(app="web-app", platform="web")
    def unencodable(s: eks.Session) -> None:
        s.acme.needs_persona(Path("x"))

    with pytest.raises(TypeError, match="PosixPath|Path"):
        eks.plan(unencodable)


def test_generator_and_async_macros_are_rejected_before_recording() -> None:
    @eks.macro(namespace="acme")
    def lazy(s: eks.Session):
        yield s.ui.tap("#a")

    @eks.macro(namespace="acme")
    async def awaited(s: eks.Session) -> None:
        s.ui.tap("#a")

    @eks.macro(namespace="acme")
    async def streamed(s: eks.Session):
        yield s.ui.tap("#a")

    for name in ("lazy", "awaited", "streamed"):

        @eks.flow(app="web-app", platform="web")
        def calls(s: eks.Session, name: str = name) -> None:
            getattr(s.acme, name)()

        with pytest.raises(TraceError, match=f"macro acme.{name} is a generator or async function"):
            eks.plan(calls)


def test_macro_returning_a_generator_is_rejected() -> None:
    import functools
    import warnings

    def produce(s: eks.Session):
        yield s.ui.tap("#a")

    async def run(s: eks.Session) -> None:
        s.ui.tap("#a")

    @eks.macro(namespace="acme")
    def hidden_generator(s: eks.Session) -> Any:
        return produce(s)

    @functools.wraps(run)
    def hidden_coroutine_impl(s: eks.Session) -> Any:
        return run(s)

    eks.macro(namespace="acme", name="hidden_coroutine")(hidden_coroutine_impl)

    @eks.flow(app="web-app", platform="web")
    def generator_flow(s: eks.Session) -> None:
        s.acme.hidden_generator()

    with pytest.raises(TraceError, match="macro acme.hidden_generator is a generator or async function"):
        eks.plan(generator_flow)

    @eks.flow(app="web-app", platform="web")
    def coroutine_flow(s: eks.Session) -> None:
        s.acme.hidden_coroutine()

    with warnings.catch_warnings():
        # the returned coroutine is closed, so no "never awaited" warning leaks
        warnings.simplefilter("error")
        with pytest.raises(TraceError, match="macro acme.hidden_coroutine is a generator or async function"):
            eks.plan(coroutine_flow)


def test_macros_nest_and_win_over_ops_of_the_same_name() -> None:
    register_demo_ops()

    @eks.macro(namespace="demo")
    def ping(s: eks.Session, host: str) -> Ref[Any]:
        return s.op("demo.ping", host=host, count=5)

    @eks.macro(namespace="acme")
    def checkup(s: eks.Session) -> Ref[Any]:
        return s.demo.ping("h2")

    @eks.flow(app="web-app", platform="web")
    def nested(s: eks.Session) -> None:
        result = s.acme.checkup()
        s.ui.fill("#out", f"{result.ok}")

    outer, fill = twice(nested).nodes
    (inner,) = body(outer)
    assert (outer.args["name"], inner.op, inner.args["name"]) == ("acme.checkup", "ctl.macro", "demo.ping")
    (op_node,) = body(inner)
    assert (op_node.op, op_node.args) == ("demo.ping", {"host": "h2", "count": 5})
    assert fill.args["value"] == {"$fmt": [{"$ref": op_node.id, "path": ["ok"]}]}


def test_plugin_macro_adds_its_requirement() -> None:
    with contributing("acme.tools", "1.2.0"):
        @eks.macro()
        def stamp(s: eks.Session, label: str) -> None:
            s.ui.fill("#stamp", label)

    @eks.flow(app="web-app", platform="web")
    def stamped(s: eks.Session) -> None:
        s.acme_tools.stamp("ok")
        s.acme_tools.stamp("again")

    plan = twice(stamped)
    assert plan.requires == [Requirement(plugin="acme.tools", version="1.2.0")]
    assert [node.args["name"] for node in plan.nodes] == ["acme_tools.stamp", "acme_tools.stamp"]


def test_project_macro_without_namespace_is_called_through_the_default_namespace(eks_registry: Registry) -> None:
    @eks.macro()
    def login(s: eks.Session, persona: str) -> Ref[str]:
        s.ui.tap(f"text={persona}")
        return s.ui.text("#welcome")

    assert eks_registry.macro(f"{PROJECT_NAMESPACE}.login").fn is login

    @eks.flow(app="web-app", platform="web")
    def logs_in(s: eks.Session) -> None:
        welcome = s.project.login("harman")
        s.ui.fill("#greeting", welcome)

    macro, fill = twice(logs_in).nodes
    assert macro.op == "ctl.macro"
    assert macro.args == {"name": "project.login", "version": "1", "params": {"persona": "harman"}}
    tap, text = body(macro)
    assert tap.args["target"] == {"by": "text", "args": {"text": "harman", "exact": True}}
    assert fill.args["value"] == {"$ref": text.id, "path": []}


def test_session_members_are_reserved_for_namespaces_and_families() -> None:
    @eks.flow(app="web-app", platform="web")
    def members(s: eks.Session) -> None:
        public = {name for name in (*dir(type(s)), *vars(s)) if not name.startswith("_")}
        # every public session attribute shadows s.<name>, so the registry must refuse it as a namespace
        assert public <= SESSION_MEMBERS, sorted(public - SESSION_MEMBERS)

    eks.plan(members)
    assert PROJECT_NAMESPACE not in SESSION_MEMBERS

    for space in ("app", "platform", "spec", "op", "call"):
        with pytest.raises(RegistryError, match=rf"{space!r} is a member of the session \(s.{space}\)"):
            @eks.macro(namespace=space)
            def clash(s: eks.Session) -> None:
                del s

    with contributing("platform", "1"), pytest.raises(RegistryError, match="for example 'platform_ext'"):
        @eks.macro()
        def boot(s: eks.Session) -> None:
            del s

    class CallArgs(eks.Args):
        value: int = 0

    with pytest.raises(RegistryError, match=r"'call' is a member of the session.*for example 'project'"):
        eks.op("call")(CallArgs)


def test_misspelled_session_method_suggests_the_real_one() -> None:
    @eks.flow(app="web-app", platform="web")
    def typo(s: eks.Session) -> None:
        # Namespace hides __call__ from type checkers, so pyright reports the typo here and keeps checking
        # the rest of the flow (the file enables reportUnnecessaryTypeIgnoreComment to enforce both)
        s.checkpiont("x")  # pyright: ignore[reportCallIssue]
        s.ui.tap(42)  # pyright: ignore[reportArgumentType]

    with pytest.raises(AttributeError, match=r"s.checkpiont is not a session method, macro or op.*s.checkpoint"):
        eks.plan(typo)

    @eks.flow(app="web-app", platform="web")
    def far_off(s: eks.Session) -> None:
        s.qqqqqq()  # pyright: ignore[reportCallIssue]

    with pytest.raises(AttributeError, match=r"s.qqqqqq is not a session method.*session methods: .*checkpoint"):
        eks.plan(far_off)


def test_macro_ids_follow_the_call_site() -> None:
    @eks.macro(namespace="acme")
    def tap_twice(s: eks.Session) -> None:
        s.ui.tap("#a")
        s.ui.tap("#a")

    @eks.flow(app="web-app", platform="web")
    def called(s: eks.Session) -> None:
        s.acme.tap_twice()
        s.acme.tap_twice()

    first, second = eks.plan(called).nodes
    ids = [first.id, second.id, *(node.id for node in body(first)), *(node.id for node in body(second))]
    assert len(set(ids)) == 6


# ---------------------------------------------------------------- stable ids from a file


FLOW_SOURCE = """\
import eks
from eks import by


@eks.macro(namespace="filed")
def login(s: eks.Session, persona: str):
    s.ui.fill("#username", persona)
    s.ui.tap("#login")
    return s.ui.text("#welcome")


@eks.flow(app="web-app", platform="web")
def filed(s: eks.Session):
    s.ui.nav("/login")
    welcome = s.filed.login("harman-admin")
    with eks.when(s.ui.exists("#empty-state")):
        s.ui.tap("#create")
    with eks.otherwise():
        s.ui.tap(by.test_id("row").first)
    s.ui.fill("#greeting", f"Merhaba {welcome}")
    s.demo_file.ping(host="h")
"""

EDITED_SOURCE = """\
import eks
from eks import by

# a comment that moves every line down


@eks.macro(namespace="filed")
def login(s: eks.Session, persona: str):

    s.ui.fill("#username", persona)   # fill it

    s.ui.tap("#login")
    return s.ui.text("#welcome")


@eks.flow(app="web-app", platform="web")
def filed(s: eks.Session):
    # navigate first
    s.ui.nav("/login")

    welcome = s.filed.login("harman-admin")
    with eks.when(s.ui.exists("#empty-state")):

        s.ui.tap("#create")
    with eks.otherwise():
        s.ui.tap(by.test_id("row").first)  # first row
    s.ui.fill("#greeting", f"Merhaba {welcome}")
    s.demo_file.ping(
        host="h"
    )
"""


def ids_and_src(plan: Plan) -> tuple[list[str], list[str | None]]:
    walked = [node for node, _ancestors in plan.walk()]
    return [node.id for node in walked], [node.src for node in walked]


def test_blank_lines_and_comments_keep_ids_and_hash(tmp_path: Path, eks_registry: Registry) -> None:
    @eks.op("demo_file", name="ping", out="json")
    class FilePing(eks.Args):
        host: str

    path = tmp_path / "filed.py"
    path.write_text(FLOW_SOURCE, encoding="utf-8")
    (entry,) = load_file(path)
    first = eks.plan(entry)
    assert first.to_json() == eks.plan(entry).to_json()

    path.write_text(textwrap.dedent(EDITED_SOURCE), encoding="utf-8")
    (edited,) = load_file(path)
    second = eks.plan(edited)

    first_ids, first_src = ids_and_src(first)
    second_ids, second_src = ids_and_src(second)
    assert first_ids == second_ids
    assert first_src != second_src
    assert first.hash == second.hash
    assert [node.hash for node, _ in first.walk()] == [node.hash for node, _ in second.walk()]
    assert [node.op for node in first.nodes] == ["ui.nav", "ctl.macro", "ui.exists", "ctl.when", "ui.fill",
                                                 "demo_file.ping"]
    assert eks_registry.macro("filed.login").fn.__module__ == entry.module
