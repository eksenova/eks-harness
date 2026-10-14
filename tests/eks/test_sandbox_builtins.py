from __future__ import annotations

import builtins
import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

import eks
from eks import by
from eks.blender import BPY_GLOBALS, BlenderBuilder
from eks.control import ALLOWED_DYNAMIC_GLOBALS
from eks.tracing import TraceError, capture_function
from eks_harness.engine.validator import Issue, Report, validate
from eks_harness.ir.hashing import digest, hash_plan
from eks_harness.ir.ops.ctl import (
    SANDBOX_BUILTINS,
    SANDBOX_EKS_ATTRIBUTES,
    SANDBOX_EKS_NAMESPACES,
)
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry

DENIED = ("__import__", "open", "eval", "exec", "compile", "hash", "id", "input", "globals", "vars", "getattr",
          "setattr", "breakpoint", "print")


def issues_of(report: Report, code: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.code == code]


def by_op(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def dynamic_plan(body: Callable[[eks.Session], None]) -> Plan:
    @eks.flow(app="web-app", platform="web", now="2026-01-15T09:00+03:00", timezone="Europe/Istanbul",
              locale="tr-TR")
    def probe(s: eks.Session) -> None:
        body(s)

    return eks.plan(probe)


def fn_args(plan: Plan) -> dict[str, Any]:
    [node] = by_op(plan, "ctl.dynamic")
    return cast("dict[str, Any]", node.args["fn"])


def test_the_allowlist_is_sorted_builtin_and_part_of_the_dynamic_globals() -> None:
    assert list(SANDBOX_BUILTINS) == sorted(SANDBOX_BUILTINS)
    assert all(name in vars(builtins) for name in SANDBOX_BUILTINS)
    assert ALLOWED_DYNAMIC_GLOBALS == ("eks", "by", "t", "expect", *SANDBOX_BUILTINS)
    assert not set(DENIED) & set(SANDBOX_BUILTINS)


def test_dunder_import_raises(eks_registry: Registry) -> None:
    # the reproduction: __import__('random').random() planned with globals [] and validated
    def body(s: eks.Session) -> None:
        @eks.dynamic
        def roll(n=1):
            return __import__("random").random() * n

    with pytest.raises(TraceError, match="'__import__'") as caught:
        dynamic_plan(body)
    assert "'len'" in str(caught.value)


def test_time_through_an_import_statement_raises(eks_registry: Registry) -> None:
    def body(s: eks.Session) -> None:
        @eks.dynamic
        def stamp(n=1):
            import time

            return time.time() * n

    with pytest.raises(TraceError, match="imports 'time'") as caught:
        dynamic_plan(body)
    assert "import nothing" in str(caught.value)


def test_imports_in_nested_code_raise(eks_registry: Registry) -> None:
    def body(s: eks.Session) -> None:
        @eks.dynamic
        def nested(n=1):
            def helper():
                from os import path

                return path.sep

            return [helper() for _ in range(n)]

    with pytest.raises(TraceError, match="imports 'os'"):
        dynamic_plan(body)


def dynamic_open(n=1):
    return open("/etc/hosts").read() * n


def dynamic_hash(n=1):
    return hash("x") + n


def dynamic_eval(n=1):
    return eval("n + 1")


def dynamic_exec(n=1):
    exec("n = 2")  # noqa: S102
    return n


@pytest.mark.parametrize(("builtin", "fn"), [
    ("open", dynamic_open), ("hash", dynamic_hash), ("eval", dynamic_eval), ("exec", dynamic_exec),
])
def test_unsafe_builtins_raise(builtin: str, fn: Callable[..., Any], eks_registry: Registry) -> None:
    # the reproduction: open('/etc/hosts'), hash('x') (PYTHONHASHSEED dependent), eval and exec all validated
    def body(s: eks.Session) -> None:
        eks.dynamic(fn)

    with pytest.raises(TraceError, match=f"builtin '{builtin}'"):
        dynamic_plan(body)


def load_probe(path: Path, source: str) -> Callable[..., Any]:
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast("Callable[..., Any]", module.probe)


@pytest.mark.parametrize("builtin", DENIED)
def test_capture_function_rejects_every_denied_builtin(builtin: str, tmp_path: Path) -> None:
    probe = load_probe(tmp_path / "denied.py", f"def probe(n=1):\n    return {builtin}\n")
    with pytest.raises(TraceError, match=f"builtin '{builtin}'"):
        capture_function(probe, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)


def test_len_and_range_are_recorded(eks_registry: Registry) -> None:
    def body(s: eks.Session) -> None:
        count = s.record_op("ui.count", {"target": by.test_id("row")})

        @eks.dynamic
        def per_row(n: Any = count) -> Any:
            rows = list(range(n))
            return [s.ui.tap(by.test_id("row").nth(i)) for i in rows[: len(rows)]]

        assert per_row is not None

    plan = dynamic_plan(body)
    assert fn_args(plan)["globals"] == ["by", "len", "list", "range"]
    assert validate(plan).ok, validate(plan).text()


def test_a_module_global_shadowing_a_builtin_is_not_a_builtin(tmp_path: Path) -> None:
    probe = load_probe(tmp_path / "shadow.py", "open = 3\n\n\ndef probe(n=1):\n    return open + len([n])\n")
    captured = capture_function(probe, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)
    assert captured.globals == ("len",)
    assert captured.freevars["open"].kind == "literal"
    assert captured.freevars["open"].value == 3


def plan_scene(body: Callable[[BlenderBuilder], object]) -> Plan:
    @eks.scene(kind="blender", engine="cycles", samples=32)
    def custom(sc: eks.BlenderScene) -> None:
        body(sc)

    return eks.plan(custom)


def hooks_of(plan: Plan) -> list[dict[str, Any]]:
    [node] = by_op(plan, "blender.render")
    scene = cast("dict[str, Any]", node.args["scene"])
    return cast("list[dict[str, Any]]", scene["hooks"])


def test_bpy_hooks_reject_open(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        @sc.bpy
        def leak(bpy, ctx):
            return open("/etc/hosts")

    with pytest.raises(TraceError, match="builtin 'open'"):
        plan_scene(body)


def test_bpy_hooks_accept_import_math_and_record_builtins(eks_registry: Registry) -> None:
    assert BPY_GLOBALS == ("math", "mathutils")

    def body(sc: BlenderBuilder) -> None:
        @sc.bpy_frame
        def wobble(bpy, ctx):
            import math

            bpy.data.objects["Phone"].rotation_euler.z = abs(math.sin(ctx.frame)) * len(bpy.data.objects)

    plan = plan_scene(body)
    [hook] = hooks_of(plan)
    assert hook["fn"]["globals"] == ["abs", "len"]
    assert issues_of(validate(plan), "dynamic.binding") == []


def test_bpy_hooks_accept_mathutils_submodules(tmp_path: Path) -> None:
    probe = load_probe(tmp_path / "hook.py", (
        "def probe(bpy, ctx):\n"
        "    import mathutils.noise\n"
        "    from mathutils import Vector\n"
        "    return Vector((mathutils.noise.random(), 0))\n"
    ))
    captured = capture_function(probe, allowed_globals=BPY_GLOBALS)
    assert captured.globals == ()
    with pytest.raises(TraceError, match="imports 'mathutils'"):
        capture_function(probe, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)


def test_bpy_hooks_reject_other_imports(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        @sc.bpy
        def leak(bpy, ctx):
            import subprocess

            return subprocess.run

    with pytest.raises(TraceError, match="imports 'subprocess'") as caught:
        plan_scene(body)
    assert "'math'" in str(caught.value)


def test_validator_flags_hand_edited_dynamic_globals_and_imports(eks_registry: Registry) -> None:
    def body(s: eks.Session) -> None:
        @eks.dynamic
        def double(n=2):
            return [i * 2 for i in range(n)]

        assert double is not None

    plan = dynamic_plan(body)
    assert validate(plan).ok, validate(plan).text()
    fn = fn_args(plan)
    assert fn["globals"] == ["range"]

    fn["globals"] = ["open", "range"]
    [issue] = issues_of(validate(plan), "dynamic.binding")
    assert issue.field == "fn.globals.0"
    assert "'open'" in issue.message

    fn["globals"] = ["range"]
    fn["source"] = "def double(n=2):\n    import os\n    return [os.getpid() for i in range(n)]\n"
    [issue] = issues_of(validate(plan), "dynamic.binding")
    assert issue.field == "fn.source"
    assert "'os'" in issue.message

    fn["source"] = "def double(n=2):\n    from .. import secrets\n    return n\n"
    [issue] = issues_of(validate(plan), "dynamic.binding")
    assert "'..'" in issue.message


def test_validator_flags_hand_edited_bpy_hook_globals(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        @sc.bpy
        def rig(bpy, ctx):
            return len(bpy.data.objects)

    plan = plan_scene(body)
    assert issues_of(validate(plan), "dynamic.binding") == []
    [hook] = hooks_of(plan)
    assert hook["fn"]["globals"] == ["len"]
    hook["fn"]["globals"] = ["len", "open", "math"]
    [issue] = issues_of(validate(plan), "dynamic.binding")
    assert issue.field == "scene.hooks.0.fn.globals.1"
    assert "'open'" in issue.message


ESCAPES = {
    "class body": (
        (
            "def probe(n=1):\n"
            "    class Box:\n"
            "        value = __import__('random').random() * n\n"
            "        stamp = open('/etc/hosts').read()\n"
            "    return Box.value\n"
        ),
        "builtin '__import__'",
    ),
    "class local shadowing a builtin": (
        "def probe(n=1):\n    class Box:\n        open = open\n    return Box.open\n",
        "builtin 'open'",
    ),
    "len.__self__.__import__": ("def probe(n=1):\n    return len.__self__.__import__('random').random() * n\n",
                                "'__self__'"),
    "len.__self__.open": ("def probe(n=1):\n    return len.__self__.open('/etc/hosts').read()[:n]\n", "'__self__'"),
    "len.__self__.hash": ("def probe(n=1):\n    return len.__self__.hash('x') + n\n", "'__self__'"),
    "subclasses": ("def probe(n=1):\n    return [c for c in ().__class__.__base__.__subclasses__()][:n]\n",
                   "'__class__'"),
    "generator frame": ("def probe(n=1):\n    return (i for i in range(n)).gi_frame.f_builtins['open']\n",
                        "'gi_frame'"),
    "format field": ("def probe(n=1):\n    return '{0.__self__}'.format(len)\n", "'__self__'"),
    "match class attribute": (
        (
            "def probe(n=1):\n"
            "    match len:\n"
            "        case object(__self__=module):\n"
            "            return module\n"
            "    return n\n"
        ),
        "'__self__'",
    ),
    "annotation": ("def probe(n: __import__('os').getpid() = 1):\n    return n\n", "builtin '__import__'"),
    "default": ("def probe(n=open('/etc/hosts')):\n    return n\n", "builtin 'open'"),
    "module builtins": ("def probe(n=1):\n    return __builtins__['open']\n", "'__builtins__'"),
}


@pytest.mark.parametrize("case", sorted(ESCAPES))
def test_capture_function_rejects_sandbox_escapes(case: str, tmp_path: Path) -> None:
    # the reproductions: class bodies, len.__self__, __subclasses__ and annotations all planned and validated
    source, message = ESCAPES[case]
    probe = load_probe(tmp_path / "escape.py", source)
    with pytest.raises(TraceError, match=message):
        capture_function(probe, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)
    with pytest.raises(TraceError, match=message):
        capture_function(probe, allowed_globals=BPY_GLOBALS)


def test_class_bodies_still_carry_allowed_builtins_and_locals(tmp_path: Path) -> None:
    probe = load_probe(tmp_path / "boxed.py", (
        "def probe(n: int = 1):\n"
        "    class Box:\n"
        "        size = 2\n"
        "        total = size * len([n])\n"
        "    id = Box.total\n"
        "    return id\n"
    ))
    captured = capture_function(probe, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)
    assert captured.globals == ("len",)


def test_a_dynamic_class_body_raises_in_a_plan(eks_registry: Registry) -> None:
    def body(s: eks.Session) -> None:
        @eks.dynamic
        def roll(n=1):
            class Box:
                value = __import__("random").random() * n

            return Box.value

    with pytest.raises(TraceError, match="builtin '__import__'"):
        dynamic_plan(body)


def test_a_bpy_class_body_raises(eks_registry: Registry) -> None:
    # the reproduction: an @sc.bpy hook read /etc/hosts from a class body
    def body(sc: BlenderBuilder) -> None:
        @sc.bpy
        def leak(bpy, ctx):
            class Leak:
                hosts = open("/etc/hosts").read()  # noqa: SIM115

            return Leak.hosts

    with pytest.raises(TraceError, match="builtin 'open'"):
        plan_scene(body)


def test_a_bpy_hook_reaching_builtins_through_len_raises(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        @sc.bpy
        def leak(bpy, ctx):
            return len.__self__.open("/etc/hosts").read()  # pyright: ignore[reportFunctionMemberAccess]

    with pytest.raises(TraceError, match="'__self__'"):
        plan_scene(body)


def edited(plan: Plan, source: str, globals_: list[str]) -> Plan:
    data = plan.model_dump(mode="json")
    for node in data["nodes"]:
        if node["op"] == "ctl.dynamic":
            node["args"]["fn"]["source"] = source
            node["args"]["fn"]["hash"] = digest(source.encode("utf-8"))
            node["args"]["fn"]["globals"] = globals_
    return hash_plan(Plan.model_validate(data))


def clean_dynamic_plan() -> Plan:
    def body(s: eks.Session) -> None:
        @eks.dynamic
        def roll(n=1):
            return len([n])

        assert roll is not None

    return dynamic_plan(body)


def test_validator_reads_builtins_from_the_source_not_the_globals_list(eks_registry: Registry) -> None:
    # the reproduction: a rehashed plan whose source calls open and hash but declares globals [] validated
    plan = edited(clean_dynamic_plan(),
                  "def roll(n=1):\n    return open('/etc/hosts').read() + str(hash('x')) + str(__import__('os'))\n",
                  [])
    report = validate(plan)
    assert not report.ok
    assert {issue.code for issue in report.issues} == {"dynamic.binding"}
    found = [issue for issue in issues_of(report, "dynamic.binding") if "builtin" in issue.message]
    assert [issue.field for issue in found] == ["fn.source"] * 3
    assert ["'__import__'" in found[0].message, "'hash'" in found[1].message, "'open'" in found[2].message] == [
        True, True, True]


def test_validator_flags_escapes_in_a_hand_edited_source(eks_registry: Registry) -> None:
    plan = edited(clean_dynamic_plan(), "def roll(n=1):\n    return len.__self__.open('/etc/hosts')\n", ["len"])
    report = validate(plan)
    [issue] = issues_of(report, "dynamic.binding")
    assert issue.field == "fn.source"
    assert "'__self__'" in issue.message
    assert not report.ok


def test_validator_accepts_a_literal_global_shadowing_a_builtin(tmp_path: Path, eks_registry: Registry) -> None:
    probe = load_probe(tmp_path / "shadowed.py", "open = 3\n\n\ndef probe(n=1):\n    return open + len([n])\n")

    def body(s: eks.Session) -> None:
        eks.dynamic(probe)

    plan = dynamic_plan(body)
    assert validate(plan).ok, validate(plan).text()


def test_validator_flags_hand_edited_bpy_hook_sources(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        @sc.bpy
        def rig(bpy, ctx):
            import math

            return math.pi * len(bpy.data.objects)

    plan = plan_scene(body)
    assert issues_of(validate(plan), "dynamic.binding") == []
    [hook] = hooks_of(plan)
    hook["fn"]["source"] = "def rig(bpy, ctx):\n    import os\n    return open(os.sep)\n"
    found = issues_of(validate(plan), "dynamic.binding")
    assert [issue.field for issue in found] == ["scene.hooks.0.fn.source"] * 2
    assert "imports 'os'" in found[0].message
    assert "'open'" in found[1].message

    hook["fn"]["source"] = "def rig(bpy, ctx):\n    return (i for i in []).gi_frame.f_globals\n"
    found = issues_of(validate(plan), "dynamic.binding")
    assert {issue.message.split("'")[1] for issue in found} == {"gi_frame", "f_globals"}


def test_local_variable_annotations_are_not_read_but_class_annotations_are(tmp_path: Path) -> None:
    probe = load_probe(tmp_path / "annotated.py", (
        "from typing import Any\n\n\n"
        "def probe(n=1):\n"
        "    options: dict[str, Any] = {}\n"
        "    return len(options) + n\n"
    ))
    assert capture_function(probe, allowed_globals=ALLOWED_DYNAMIC_GLOBALS).globals == ("len",)
    boxed = load_probe(tmp_path / "boxed_annotation.py", (
        "from typing import Any\n\n\n"
        "def probe(n=1):\n"
        "    class Box:\n"
        "        value: Any = 1\n"
        "    return Box.value + n\n"
    ))
    with pytest.raises(TraceError, match="global 'Any'"):
        capture_function(boxed, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)


EKS_ESCAPES = {
    "builtins.open": ("def leak(n=1):\n    return eks.tracing.builtins.open('/etc/hosts').read() * n\n",
                      "'eks.tracing'"),
    "builtins.eval": ("def evaluate(n=1):\n    return eks.tracing.builtins.eval('n + 1', {'n': n})\n", "'eks.tracing'"),
    "os.urandom": ("def clock(n=1):\n    return eks.tracing.os.urandom(8).hex() * n\n", "'eks.tracing'"),
    "alias": ("def alias(n=1):\n    m = eks\n    return m.tracing.os.getpid() * n\n", "'eks'"),
    "namespace member": ("def member(n=1):\n    return eks.overlay.builtins.open('/etc/hosts')\n",
                         "'eks.overlay.builtins'"),
    "private": ("def private(n=1):\n    return s._ctx.tree\n", "'_ctx'"),
    "internal": ("def internal(n=1):\n    return s.ctx.tree.read_text()\n", "'ctx'"),
}


@pytest.mark.parametrize("case", sorted(EKS_ESCAPES))
def test_eks_attribute_chains_cannot_reach_modules(case: str, tmp_path: Path) -> None:
    # the reproduction: eks.tracing.builtins.open, eval and eks.tracing.os.urandom planned with globals ['eks']
    source, message = EKS_ESCAPES[case]
    probe = load_probe(tmp_path / "chain.py", "import eks\n\ns = None\n\n\n" + source.replace(
        source.split("(")[0].removeprefix("def "), "probe", 1))
    with pytest.raises(TraceError, match=message):
        capture_function(probe, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)


def test_the_reproduced_eks_chains_raise_in_a_plan(eks_registry: Registry) -> None:
    def body(s: eks.Session) -> None:
        @eks.dynamic
        def leak(n=1):
            return eks.tracing.builtins.open("/etc/hosts").read() * n  # pyright: ignore[reportAttributeAccessIssue]

    with pytest.raises(TraceError, match="reaches 'eks.tracing'"):
        dynamic_plan(body)


def test_the_validator_flags_eks_chains_in_a_hand_edited_source(eks_registry: Registry) -> None:
    plan = edited(clean_dynamic_plan(), EKS_ESCAPES["os.urandom"][0], ["eks"])
    [issue] = issues_of(validate(plan), "dynamic.binding")
    assert issue.field == "fn.source"
    assert "'eks.tracing'" in issue.message


def test_the_sandbox_eks_members_exist_and_are_not_modules() -> None:
    assert list(SANDBOX_EKS_ATTRIBUTES) == sorted(SANDBOX_EKS_ATTRIBUTES)
    for name in SANDBOX_EKS_ATTRIBUTES:
        value = getattr(eks, name)
        if name in SANDBOX_EKS_NAMESPACES:
            assert isinstance(value, ModuleType)
            members = SANDBOX_EKS_NAMESPACES[name]
            assert list(members) == sorted(members)
            for member in members:
                assert member in value.__all__
                assert not isinstance(getattr(value, member), ModuleType)
        else:
            assert not isinstance(value, ModuleType), name


def test_allowed_eks_members_plan_and_validate(eks_registry: Registry) -> None:
    def body(s: eks.Session) -> None:
        shot = s.shot("liste")

        @eks.dynamic
        def note(n=1, first=shot):
            eks.annotate(first, [eks.overlay.caption(f"satir {n}")])
            return [eks.when, eks.t, eks.Time]

        assert note is not None

    plan = dynamic_plan(body)
    assert issues_of(validate(plan), "dynamic.binding") == []


ADDRESS_LEAKS = {
    "str(map)": "def probe(n=1):\n    return str(map(int, []))[:n]\n",
    "str(zip)": "def probe(n=1):\n    return str(zip()) * n\n",
    "generator f-string": "def probe(n=1):\n    return f'{(x for x in range(n))}'\n",
    "alias": "def probe(n=1):\n    x = map(int, [])\n    return str(x)\n",
    "list display": "def probe(n=1):\n    return str([map(int, [])])\n",
    "mutation": "def probe(n=1):\n    x = []\n    x.append(map(int, []))\n    return str(x)\n",
    "aliased mutation": "def probe(n=1):\n    x = []\n    y = x\n    y.append(lambda: 0)\n    return str(x)\n",
    "nested return": "def probe(n=1):\n    def f():\n        return map(int, [])\n    return str(f())\n",
    "parameter mutation": (
        "def probe(n=1):\n    x = []\n    def add(items):\n        items.append(lambda: 0)\n    add(x)\n"
        "    return str(x)\n"
    ),
    "method mutation": (
        "def probe(n=1):\n    x = []\n    class Box:\n        items = x\n        def add(self):\n"
        "            self.items.append(lambda: 0)\n    Box().add()\n    return str(x)\n"
    ),
    "set of functions": "def probe(n=1):\n    return [f() for f in {lambda: 1, lambda: 2}][:n]\n",
    "set()": "def probe(n=1):\n    return len(set(map(lambda x: (lambda: x), range(n))))\n",
    "bound method": "def probe(rows=[]):\n    return str(rows.append)\n",
    "percent": "def probe(n=1):\n    return '%s' % ((x for x in range(n)),)\n",
    "map(str)": "def probe(n=1):\n    return ','.join(map(str, [map(int, [])]))\n",
    "str as a value": "def probe(n=1):\n    f = str\n    return f(map(int, []))\n",
    "format argument": "def probe(n=1):\n    return '{}'.format(map(int, []))\n",
    "session object": "def probe(n=1):\n    return f'{s}' * n\n",
    "classmethod call": "def probe(n=1):\n    return str(dict.fromkeys([map(int, [])]))\n",
    "slice store": "def probe(n=1):\n    x = [0]\n    x[0:1] = [map(int, [])]\n    return str(x)\n",
    "comprehension side effect": (
        "def probe(n=1):\n    x = []\n    [x.append(m) for m in [map(int, [])]]\n    return str(x)\n"
    ),
    "generator function": "def probe(n=1):\n    def g():\n        yield n\n    return str(g())\n",
    "length of an address": "def probe(n=1):\n    return len(str(map(int, []))) * n\n",
    "factory object": "def probe(n=1):\n    return str(by) * n\n",
}


@pytest.mark.parametrize("case", sorted(ADDRESS_LEAKS))
def test_values_with_object_addresses_cannot_be_formatted(case: str, tmp_path: Path) -> None:
    # the reproduction: str(map(int, [])) and f"{genexp}" returned a different heap address on every run
    probe = load_probe(tmp_path / "leak.py", "s = None\nby = None\n\n\n" + ADDRESS_LEAKS[case])
    with pytest.raises(TraceError, match="may hold an object address"):
        capture_function(probe, allowed_globals=(*ALLOWED_DYNAMIC_GLOBALS, "s"))


DATA_FORMATS = {
    "params and enumerate": "def probe(rows=[]):\n    return [f'#row-{r[\"id\"]}-{i}' for i, r in enumerate(rows)]\n",
    "materialized map": "def probe(n=1):\n    return str(list(map(int, ['1']))) * n\n",
    "join of str": "def probe(ids=[]):\n    return ', '.join(map(str, ids))\n",
    "lambda key": "def probe(rows=[]):\n    return sorted(rows, key=lambda r: str(r['x']))\n",
    "helper": "def probe(rows=[]):\n    def label(r):\n        return f\"row-{r['id']}\"\n    return [label(r) for r in rows]\n",
    "len": "def probe(n=1):\n    x = list(range(n))\n    k = len(x)\n    return f'{k}'\n",
    "format literal": "def probe(n=1):\n    return '{}-{}'.format(n, 2)\n",
    "set of strings": "def probe(rows=[]):\n    return sorted({str(r) for r in rows})\n",
}


@pytest.mark.parametrize("case", sorted(DATA_FORMATS))
def test_data_can_still_be_formatted(case: str, tmp_path: Path) -> None:
    probe = load_probe(tmp_path / "data.py", DATA_FORMATS[case])
    capture_function(probe, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)


def test_the_reproduced_address_leak_raises_in_a_plan(eks_registry: Registry) -> None:
    def body(s: eks.Session) -> None:
        @eks.dynamic
        def addr(n=1):
            return str(map(int, []))[:n]

    with pytest.raises(TraceError, match="may hold an object address to str"):
        dynamic_plan(body)


def test_the_validator_flags_address_leaks_in_a_hand_edited_source(eks_registry: Registry) -> None:
    plan = edited(clean_dynamic_plan(), "def roll(n=1):\n    return f'{(x for x in range(n))}'\n", ["range"])
    [issue] = issues_of(validate(plan), "dynamic.binding")
    assert issue.field == "fn.source"
    assert "object address" in issue.message
    assert "f-string field" in issue.message


RUNTIME_FORMAT = (
    "def probe(n=1):\n"
    "    gen = (x for x in range(n))\n"
    "    field = '{0.gi_fr' + 'ame.f_glo' + 'bals[_' + '_builtins__]}'\n"
    "    return field.format(gen)[:200]\n"
)


def test_a_format_string_built_at_run_time_is_rejected(tmp_path: Path) -> None:
    # the reproduction: a runtime-built str.format field reached gi_frame.f_globals['__builtins__']
    probe = load_probe(tmp_path / "runtime_format.py", RUNTIME_FORMAT)
    with pytest.raises(TraceError, match="reaches 'format'"):
        capture_function(probe, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)
    hook = load_probe(tmp_path / "runtime_hook.py", (
        "def probe(bpy, ctx):\n"
        "    gen = (x for x in range(1))\n"
        "    return ('{0.gi_fr' + 'ame.f_back}').format(gen)\n"
    ))
    with pytest.raises(TraceError, match="reaches 'format'"):
        capture_function(hook, allowed_globals=BPY_GLOBALS)
    unbound = load_probe(tmp_path / "unbound_format.py", "def probe(n=1):\n    return str.format('{0}', n)\n")
    with pytest.raises(TraceError, match="reaches 'format'"):
        capture_function(unbound, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)


def test_the_validator_flags_a_runtime_format_string(eks_registry: Registry) -> None:
    plan = edited(clean_dynamic_plan(), RUNTIME_FORMAT.replace("probe", "roll"), ["range"])
    found = issues_of(validate(plan), "dynamic.binding")
    assert any("reaches 'format'" in issue.message for issue in found)
    assert all(issue.field == "fn.source" for issue in found)


def test_bpy_hooks_format_only_data(eks_registry: Registry, tmp_path: Path) -> None:
    leaky = load_probe(tmp_path / "hook_leak.py", "def probe(bpy, ctx):\n    return f'{ctx.frame}'\n")
    with pytest.raises(TraceError, match="object address"):
        capture_function(leaky, allowed_globals=BPY_GLOBALS)
    fine = load_probe(tmp_path / "hook_fine.py", (
        "def probe(bpy, ctx):\n"
        "    import math\n"
        "    return f'{int(ctx.frame)} {math.pi:.2f} {math.sin(0.5)}'\n"
    ))
    capture_function(fine, allowed_globals=BPY_GLOBALS)

    def body(sc: BlenderBuilder) -> None:
        @sc.bpy
        def rig(bpy, ctx):
            return len(bpy.data.objects)

    plan = plan_scene(body)
    [hook] = hooks_of(plan)
    hook["fn"]["source"] = "def rig(bpy, ctx):\n    return str(bpy)\n"
    [issue] = issues_of(validate(plan), "dynamic.binding")
    assert issue.field == "scene.hooks.0.fn.source"
    assert "object address" in issue.message
