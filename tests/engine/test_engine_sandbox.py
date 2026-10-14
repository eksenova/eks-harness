from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import eks
from eks import by
from eks.session import Session
from eks.tracing import TraceContext
from eks_harness.config import Config
from eks_harness.engine import sandbox, sandbox_child
from eks_harness.engine.errors import SandboxFailure
from eks_harness.engine.registries import plugin_digest
from eks_harness.engine.sandbox import (
    CHILD_MODULE,
    OUTPUT_LIMIT_BYTES,
    PROTOCOL,
    STDERR_LIMIT_BYTES,
    SandboxLimits,
    SandboxRequest,
    SandboxResponse,
    SandboxRunner,
    SubPlan,
    child_env,
    subplan_name,
)
from eks_harness.engine.sandbox_child import (
    BLOCKED_EVENTS,
    SOURCE_PREFIX,
    audit_hook,
    bindings,
    compile_function,
    read_request,
)
from eks_harness.ir.hashing import digest
from eks_harness.ir.ops.ctl import SANDBOX_BUILTINS
from eks_harness.ir.plan import Binding, Node, Plan, SessionSpec
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.ir.target import Target
from eks_harness.ir.time import Time
from eks_harness.paths import DIR_ENVS, HOME_ENV, resolve_paths
from eks_harness.plugins.host import PluginHost

FIXTURES = Path(__file__).resolve().parents[1] / "eks" / "cli_fixtures"
LIMIT_KEYS = {"cpu", "memory", "file_size", "open_files", "core"}


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    yield chosen
    chosen.restore(state)


@pytest.fixture
def runner(tmp_path: Path) -> SandboxRunner:
    return SandboxRunner(limits=SandboxLimits(timeout_seconds=120), scratch=tmp_path / "scratch")


def rows_plan() -> tuple[Plan, Node]:
    @eks.flow(app="web-app", platform="web")
    def rows_flow(s: eks.Session) -> Any:
        rows = s.record_op("api.get", {"path": "/api/rows"})

        @eks.dynamic
        def per_row(items=rows.items, label="row"):
            taps = [s.ui.tap(by.test_id(label).nth(index)) for index in range(len(items))]
            return {"taps": taps, "count": len(items)}

        return {"rows": per_row}

    plan = eks.plan(rows_flow)
    return plan, dynamic_node(plan)


def dynamic_node(plan: Plan) -> Node:
    (node,) = [node for node, _ancestors in plan.walk() if node.op == "ctl.dynamic"]
    return node


def fn_of(node: Node) -> dict[str, Any]:
    return cast("dict[str, Any]", node.args["fn"])


def crafted(node: Node, source: str, *, freevars: dict[str, Any] | None = None) -> Node:
    text = textwrap.dedent(source).lstrip("\n")
    name = text.split("(", 1)[0].removeprefix("def ").strip()
    fn = {"name": name, "source": text, "hash": digest(text.encode("utf-8")), "params": ["n"],
          "freevars": freevars or {}, "globals": []}
    return node.model_copy(update={"args": {"fn": fn, "inputs": {"n": 1}}})


def run_crafted(runner: SandboxRunner, source: str, *, freevars: dict[str, Any] | None = None) -> SubPlan:
    plan, node = rows_plan()
    return runner.dynamic(plan, crafted(node, source, freevars=freevars), instance="", inputs={"n": 1}, tree=None,
                          plugins=False, digests={}, session=plan.session)


def failure_of(runner: SandboxRunner, source: str) -> SandboxFailure:
    with pytest.raises(SandboxFailure) as caught:
        run_crafted(runner, source)
    return caught.value


def no_guard(limits: SandboxLimits) -> dict[str, Any]:
    del limits
    return {}


def subplan_ops(plan: Plan) -> list[str]:
    return [node.op for node, _ancestors in plan.walk()]


def test_dynamic_returns_a_hashed_subplan_named_after_the_node(runner: SandboxRunner) -> None:
    plan, node = rows_plan()
    items: list[Any] = [{"id": 1}, {"id": 2}, {"id": 3}]
    first = runner.dynamic(plan, node, instance="", inputs={"items": items, "label": "row"}, tree=None,
                           plugins=False, digests={}, session=plan.session)
    sub = first.plan
    assert sub.name == f"{plan.name}~{node.id}" == subplan_name(plan, node)
    assert sub.kind == "flow"
    assert sub.session == plan.session
    assert sub.hash.startswith("sha256:")
    assert subplan_ops(sub) == ["ui.tap", "ui.tap", "ui.tap"]
    assert [tap.args["target"] for tap in sub.nodes] == [
        {"by": "test_id", "args": {"id": "row"}, "nth": index} for index in range(3)]
    assert all(tap.hash.startswith("sha256:") for tap in sub.nodes)
    assert sub.resources == plan.resources
    parent_ids = {resource.id for resource in plan.resources}
    assert all(set(tap.uses) <= parent_ids and tap.uses for tap in sub.nodes)
    assert all(tap.src is not None and tap.src.startswith(f"{SOURCE_PREFIX}{node.id}>:") for tap in sub.nodes)
    assert sub.outputs == {"taps": [{"$ref": tap.id, "path": []} for tap in sub.nodes], "count": 3}
    assert first.response.ok
    assert set(first.response.isolation) == {"limits", "network", "files"}

    again = runner.dynamic(plan, node, instance=f"{node.id}#4", inputs={"items": items, "label": "row"}, tree=None,
                           plugins=False, digests={}, session=plan.session)
    assert again.plan.to_json() == sub.to_json()
    assert [tap.id for tap in again.plan.nodes] == [tap.id for tap in sub.nodes]
    assert again.plan.hash == sub.hash

    other = runner.dynamic(plan, node, instance="", inputs={"items": items[:2], "label": "cell"}, tree=None,
                           plugins=False, digests={}, session=plan.session)
    assert other.plan.hash != sub.hash
    assert subplan_ops(other.plan) == ["ui.tap", "ui.tap"]
    assert other.plan.outputs == {"taps": [{"$ref": tap.id, "path": []} for tap in other.plan.nodes], "count": 2}


def test_guard_blocks_sockets_files_processes_and_removals(runner: SandboxRunner, tmp_path: Path) -> None:
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    written = tmp_path / "written.txt"
    kept = tmp_path / "kept.txt"
    kept.write_text("keep", encoding="utf-8")
    kept.chmod(0o600)
    linked = tmp_path / "linked.txt"
    sources = {
        "socket": f"""
            def reach(n=1):
                net = eks.tracing.builtins.__import__("socket")
                link = net.socket(net.AF_INET, net.SOCK_STREAM)
                link.connect(("127.0.0.1", {port}))
                return [n]
        """,
        "file": f"""
            def scribble(n=1):
                handle = eks.tracing.builtins.open({str(written)!r}, "w")
                handle.write("x")
                return [n]
        """,
        "process": """
            def spawn(n=1):
                eks.tracing.builtins.__import__("subprocess").run(["true"])
                return [n]
        """,
        "remove": f"""
            def erase(n=1):
                eks.tracing.os.remove({str(kept)!r})
                return [n]
        """,
        "swallowed": f"""
            def hide(n=1):
                try:
                    eks.tracing.os.remove({str(kept)!r})
                except:
                    pass
                return [n]
        """,
        "truncate": f"""
            def shrink(n=1):
                eks.tracing.os.truncate({str(kept)!r}, 0)
                return [n]
        """,
        "chmod": f"""
            def loosen(n=1):
                eks.tracing.os.chmod({str(kept)!r}, 0o777)
                return [n]
        """,
        "symlink": f"""
            def point(n=1):
                eks.tracing.os.symlink({str(kept)!r}, {str(linked)!r})
                return [n]
        """,
        "killpg": """
            def signal_group(n=1):
                eks.tracing.os.killpg(eks.tracing.os.getpgid(0), 0)
                return [n]
        """,
        "sqlite": f"""
            def database(n=1):
                eks.tracing.builtins.__import__("sqlite3").connect({str(written)!r})
                return [n]
        """,
    }
    try:
        failures = {name: failure_of(runner, source) for name, source in sources.items()}
        server.settimeout(0.2)
        with pytest.raises(TimeoutError):
            server.accept()
    finally:
        server.close()
    assert not written.exists()
    assert kept.read_text(encoding="utf-8") == "keep"
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert not linked.exists()
    assert not linked.is_symlink()
    events = {"socket": "socket.connect", "file": "open", "process": "subprocess.Popen", "remove": "os.remove",
              "swallowed": "os.remove", "truncate": "os.truncate", "chmod": "os.chmod", "symlink": "os.symlink",
              "killpg": "os.killpg", "sqlite": "sqlite3.connect"}
    for name, failure in failures.items():
        assert failure.code == "sandbox_violation", (name, failure.message)
        assert failure.detail["code"] == "violation"
        assert failure.detail["type"] == "PermissionError"
        assert events[name] in failure.message
        isolation = failure.detail["isolation"]
        assert isinstance(isolation, dict)
        assert isolation["network"] in ({"audit", "audit+netns"} if sys.platform == "linux" else {"audit"})
        assert isolation["files"] == "audit+fsize"
        limits = isolation["limits"]
        assert isinstance(limits, dict)
        assert set(limits) == LIMIT_KEYS


def test_endless_loop_hits_the_wall_clock_limit(tmp_path: Path) -> None:
    quick = SandboxRunner(limits=SandboxLimits(timeout_seconds=1), scratch=tmp_path / "scratch")
    started = time.monotonic()
    failure = failure_of(quick, """
        def spin(n=1):
            while True:
                n = n + 1
    """)
    assert time.monotonic() - started < 10
    assert failure.code == "sandbox_timeout"
    assert failure.detail["code"] == "limit"
    assert "1s" in failure.message
    assert list((tmp_path / "scratch").iterdir()) == []


def test_the_child_runs_in_its_own_session(runner: SandboxRunner) -> None:
    result = run_crafted(runner, """
        def where(n=1):
            return [eks.tracing.os.getsid(0), eks.tracing.os.getpgid(0), n]
    """)
    sid, pgid, _n = cast("list[int]", result.plan.outputs)
    assert sid != os.getsid(0)
    assert pgid != os.getpgid(0)
    assert sid == pgid


def wait_gone(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(sys.platform == "win32", reason="the fake interpreters are shell scripts")
@pytest.mark.parametrize("ending", ["timeout", "exit"])
def test_processes_left_by_the_child_are_killed_with_its_group(ending: str, tmp_path: Path) -> None:
    pidfile = tmp_path / "straggler.pid"
    last = "sleep 300" if ending == "timeout" else "echo 'not json'; exit 3"
    script = write_script(tmp_path / "straggler.sh", f"""
        sleep 300 &
        echo $! > {pidfile}
        cat > /dev/null
        {last}
    """)
    plan, node = rows_plan()
    # The limit leaves the shell time to start the straggler and write its pid even on a loaded machine.
    fake = SandboxRunner(python=str(script), limits=SandboxLimits(timeout_seconds=3), scratch=tmp_path / "s")
    started = time.monotonic()
    with pytest.raises(SandboxFailure) as caught:
        fake.dynamic(plan, node, instance="", inputs={"items": [], "label": "row"}, tree=None, plugins=False,
                     digests={}, session=plan.session)
    assert time.monotonic() - started < 8
    assert caught.value.detail["code"] == ("limit" if ending == "timeout" else "crash")
    straggler = int(pidfile.read_text(encoding="utf-8").strip())
    assert wait_gone(straggler, 5), f"process {straggler} outlived the sandbox"


def test_the_child_resolves_the_hub_paths_without_a_harness_home(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HOME_ENV, raising=False)
    for name in DIR_ENVS.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / "xdg" / name.lower()))
    hub = resolve_paths()
    assert str(hub.config_dir).startswith(str(tmp_path))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    probe = ("import json; from eks_harness.paths import resolve_paths; p = resolve_paths(); "
             "print(json.dumps([str(p.config_dir), str(p.data_dir), str(p.cache_dir), str(p.state_dir), "
             "str(p.log_dir), p.isolated]))")

    def child_paths(environ: dict[str, str]) -> list[Any]:
        done = subprocess.run([sys.executable, "-P", "-s", "-B", "-c", probe], capture_output=True, timeout=120,
                              check=True, cwd=scratch, env=child_env(environ, scratch))
        return cast("list[Any]", json.loads(done.stdout))

    expected = [str(hub.config_dir), str(hub.data_dir), str(hub.cache_dir), str(hub.state_dir), str(hub.log_dir),
                False]
    assert child_paths(sandbox._hub_environ()) == expected
    assert child_paths(dict(os.environ)) != expected


def isolation_of(runner: SandboxRunner) -> dict[str, Any]:
    result = run_crafted(runner, """
        def plain(n=1):
            return [n]
    """)
    return dict(result.response.isolation)


def test_apply_limits_reports_every_limit(runner: SandboxRunner) -> None:
    isolation = isolation_of(runner)
    limits = isolation["limits"]
    assert set(limits) == LIMIT_KEYS
    for key, entry in limits.items():
        assert entry["applied"] is True or entry["error"], key
        assert entry["resource"].startswith("RLIMIT_")
    assert limits["cpu"]["value"] == runner.limits.cpu_seconds
    assert limits["open_files"]["value"] == runner.limits.open_files
    assert limits["file_size"]["value"] == 0
    assert limits["core"]["value"] == 0
    assert limits["memory"]["value"] == runner.limits.memory_mb * 1024 * 1024
    assert isinstance(limits["memory"]["enforced"], bool)
    if limits["memory"]["enforced"]:
        assert limits["memory"]["resource"] == "RLIMIT_AS"


def test_allocation_beyond_the_address_space_limit(tmp_path: Path) -> None:
    small = SandboxRunner(limits=SandboxLimits(timeout_seconds=120, memory_mb=1024), scratch=tmp_path / "scratch")
    memory = isolation_of(small)["limits"]["memory"]
    if not memory["enforced"]:
        pytest.skip(f"the address space limit is not enforced here: {memory.get('error') or 'probe allocation passed'}")
    failure = failure_of(small, """
        def grab(n=1):
            return [len("x" * (8 * 1024 * 1024 * 1024 * n))]
    """)
    assert failure.code == "sandbox_timeout"
    assert failure.detail["code"] == "limit"
    assert failure.detail["type"] == "MemoryError"


def test_user_error_reports_the_line_and_function_frames(runner: SandboxRunner) -> None:
    @eks.flow(app="web-app", platform="web")
    def failing(s: eks.Session) -> None:
        @eks.dynamic
        def bad(n=1):
            total = n + 1
            return [int("x" * total)]

    plan = eks.plan(failing)
    node = dynamic_node(plan)
    with pytest.raises(SandboxFailure) as caught:
        runner.dynamic(plan, node, instance="", inputs={"n": 2}, tree=None, plugins=False, digests={},
                       session=plan.session)
    failure = caught.value
    assert failure.code == "sandbox_error"
    assert failure.detail["code"] == "user_error"
    assert failure.detail["type"] == "ValueError"
    assert failure.detail["line"] == 3
    assert "invalid literal" in failure.message
    assert failure.detail["traceback"] == ['line 3, in bad: return [int("x" * total)]']


def test_authoring_errors_are_trace_errors(runner: SandboxRunner) -> None:
    @eks.flow(app="web-app", platform="web")
    def authoring(s: eks.Session) -> None:
        @eks.dynamic
        def unknown(n=1):
            return [s.op("nope.missing") for _ in range(n)]

        @eks.dynamic
        def misuse(n=1):
            count = s.ui.count(by.test_id("row"))
            return [n, list(range(count))]

    plan = eks.plan(authoring)
    nodes = {fn_of(node)["name"]: node for node, _ancestors in plan.walk() if node.op == "ctl.dynamic"}
    found: dict[str, SandboxFailure] = {}
    for name, node in nodes.items():
        with pytest.raises(SandboxFailure) as caught:
            runner.dynamic(plan, node, instance="", inputs={"n": 1}, tree=None, plugins=False, digests={},
                           session=plan.session)
        found[name] = caught.value
    assert {name: failure.code for name, failure in found.items()} == {
        "unknown": "sandbox_error", "misuse": "sandbox_error"}
    assert found["unknown"].detail["code"] == "trace_error"
    assert found["unknown"].detail["type"] == "RegistryError"
    assert "nope.missing" in found["unknown"].message
    assert found["unknown"].detail["line"] == 2
    assert found["misuse"].detail["code"] == "trace_error"
    assert found["misuse"].detail["type"] == "RefUsageError"
    assert found["misuse"].detail["line"] == 3


def fixture_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "shop"
    shutil.copytree(FIXTURES, tree)
    return tree


def test_registry_changed_when_a_plugin_digest_differs(runner: SandboxRunner, tmp_path: Path, config: Config) -> None:
    tree = fixture_tree(tmp_path)
    builtin = tmp_path / "no-builtins"
    builtin.mkdir()
    host = PluginHost(config.paths, config, builtin_root=builtin, include_packages=False)
    (record,) = [record for record in host.active(tree) if record.id == "fixtures"]
    current = plugin_digest(record)
    plan, node = rows_plan()
    inputs: dict[str, Any] = {"items": [1], "label": "row"}

    ok = runner.dynamic(plan, node, instance="", inputs=inputs, tree=tree, plugins=True,
                        digests={"fixtures": current}, session=plan.session)
    assert subplan_ops(ok.plan) == ["ui.tap"]

    stale = "sha256:" + "0" * 64
    with pytest.raises(SandboxFailure) as caught:
        runner.dynamic(plan, node, instance="", inputs=inputs, tree=tree, plugins=True,
                       digests={"fixtures": stale}, session=plan.session)
    assert caught.value.code == "sandbox_error"
    assert caught.value.detail["code"] == "registry_changed"
    assert "fixtures" in caught.value.message
    assert current in caught.value.message

    with pytest.raises(SandboxFailure) as missing:
        runner.dynamic(plan, node, instance="", inputs=inputs, tree=tree, plugins=False,
                       digests={"fixtures": current}, session=plan.session)
    assert missing.value.detail["code"] == "registry_changed"


def child_command() -> list[str]:
    return [sys.executable, "-P", "-s", "-B", "-m", CHILD_MODULE]


@pytest.mark.parametrize("payload", [b"{not json", b"", b'{"protocol": 99, "plan": {}, "node": {}}'])
def test_main_reports_protocol_errors(payload: bytes, tmp_path: Path) -> None:
    done = subprocess.run(child_command(), input=payload, capture_output=True, timeout=120, check=False,
                          cwd=tmp_path, env=child_env(os.environ, tmp_path))
    assert done.returncode == 1, done.stderr.decode()
    response = SandboxResponse.model_validate_json(done.stdout)
    assert not response.ok
    assert response.error is not None
    assert response.error.code == "protocol"
    assert response.protocol == PROTOCOL


def test_read_request_round_trips_and_checks_the_protocol() -> None:
    plan, node = rows_plan()
    request = SandboxRequest(plan=plan.model_dump(mode="json", by_alias=True),
                             node=node.model_dump(mode="json", by_alias=True), inputs={"items": [], "label": "x"},
                             limits=SandboxLimits(timeout_seconds=5, memory_mb=128))
    parsed = read_request(request.model_dump_json().encode("utf-8"))
    assert parsed == request
    assert '"timeoutSeconds":5.0' in request.model_dump_json()
    with pytest.raises(ValueError, match="protocol 2"):
        read_request(request.model_copy(update={"protocol": 2}).model_dump_json().encode("utf-8"))
    with pytest.raises(ValueError, match="empty"):
        read_request(b"  \n")


def test_child_env_keeps_only_the_listed_variables(tmp_path: Path) -> None:
    environ = {
        "PATH": "/usr/bin", "HOME": "/home/someone", "LANG": "tr_TR.UTF-8", "LC_ALL": "C", "TMPDIR": "/tmp/x",
        "SYSTEMROOT": "C:\\Windows", "EKS_HARNESS_HOME": "/data/eks", "EKS_HARNESS_API_KEY": "secret",
        "PYTHONPATH": "/evil", "PYTHONHASHSEED": "random", "AWS_SECRET_ACCESS_KEY": "x", "VIRTUAL_ENV": "/venv",
        "LC_CTYPE": "UTF-8", "eks_harness_lower": "x",
    }
    env = child_env(environ, tmp_path)
    assert env == {
        "PATH": "/usr/bin", "HOME": str(tmp_path), "LANG": "tr_TR.UTF-8", "LC_ALL": "C", "TMPDIR": "/tmp/x",
        "SYSTEMROOT": "C:\\Windows", "EKS_HARNESS_HOME": "/data/eks", "EKS_HARNESS_API_KEY": "secret",
        "PYTHONHASHSEED": "0",
    }
    assert child_env({}, tmp_path) == {"HOME": str(tmp_path), "PYTHONHASHSEED": "0"}


def test_dynamic_inside_a_score_take_traces_on_the_take_session() -> None:
    @eks.score(fps=30, bpm=120)
    def ad(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.nav("/kasa")

            @eks.dynamic
            def taps(n=2):
                return [s.ui.tap(by.test_id("row").nth(index)) for index in range(n)]

    plan = eks.plan(ad)
    (take,) = [node for node, _ancestors in plan.walk() if node.op == "score.take"]
    session = SessionSpec.model_validate(take.args["session"])
    assert session.key == "take0"
    node = dynamic_node(plan)
    request = SandboxRequest(plan=plan.model_dump(mode="json", by_alias=True),
                             node=node.model_dump(mode="json", by_alias=True),
                             session=session.model_dump(mode="json", by_alias=True), inputs={"n": 2}, plugins=False)
    response = sandbox_child.run(request, guard=no_guard)
    assert response.ok, response.error
    assert response.isolation == {}
    assert response.plan is not None
    sub = Plan.model_validate(response.plan)
    assert sub.kind == "flow"
    assert sub.name == subplan_name(plan, node)
    assert sub.session == session
    assert sub.clock == plan.clock
    assert subplan_ops(sub) == ["ui.tap", "ui.tap"]
    assert all(tap.uses == [take.uses[0]] for tap in sub.nodes)
    assert all(tap.pin is None for tap in sub.nodes)
    report = eks.validate(sub)
    assert report.ok, report.text()

    flow_request = request.model_copy(update={"session": None})
    flow_response = sandbox_child.run(flow_request, guard=no_guard)
    assert flow_response.error is not None
    assert flow_response.error.code == "trace_error"
    assert "take0" in flow_response.error.message


def test_bindings_rebuild_every_kind() -> None:
    @eks.flow(app="web-app", platform="web")
    def bound(s: eks.Session) -> None:
        delay = eks.t("300ms")
        target = by.test_id("row").nth(2)
        names = ["a", ("b", 1)]
        factory = by
        ui = s.ui

        @eks.dynamic
        def closure(n=1):
            return [s, delay, target, names, factory, ui, n]

    plan = eks.plan(bound)
    node = dynamic_node(plan)
    freevars = {name: Binding.model_validate(value) for name, value in fn_of(node)["freevars"].items()}
    assert plan.session is not None
    ctx = TraceContext(kind="flow", name="bound~probe", registry=default_registry(), clock=plan.clock,
                       session=plan.session)
    session = Session(ctx, plan.session)
    other = Session(ctx, plan.session)
    cast("Any", session).borrowed = other.ui
    sessions: dict[str, Any] = {"main": session}
    scope = bindings(plan, freevars, default_registry(), sessions)
    assert scope["s"] is session
    assert scope["ui"] is session.ui
    assert scope["delay"] == eks.t("300ms")
    assert isinstance(scope["delay"], Time)
    assert isinstance(scope["target"], Target)
    assert scope["target"] == by.test_id("row").nth(2)
    assert scope["names"] == ["a", ["b", 1]]
    assert scope["factory"] is by
    assert set(scope["__builtins__"]) == set(SANDBOX_BUILTINS)
    assert scope["eks"] is eks
    assert scope["by"] is by
    assert bindings(plan, {"web": Binding(kind="namespace", value="main.web")}, default_registry(),
                    sessions)["web"] is session.web
    for value in ("main.ctx", "main.spec", "main.record_op", "main.nope", "main.borrowed", "main.__class__",
                  "main.resources"):
        with pytest.raises(eks.tracing.TraceError, match="namespace binding"):
            bindings(plan, {"x": Binding(kind="namespace", value=value)}, default_registry(), sessions)
    with pytest.raises(eks.tracing.TraceError, match="names session 'take3'"):
        bindings(plan, {"s": Binding(kind="session", value="take3")}, default_registry(), sessions)
    with pytest.raises(eks.tracing.TraceError, match="namespace binding"):
        bindings(plan, {"ui": Binding(kind="namespace", value="main._ctx")}, default_registry(), sessions)
    with pytest.raises(eks.tracing.TraceError, match="factory binding"):
        bindings(plan, {"f": Binding(kind="factory", value="open")}, default_registry(), sessions)


def test_compile_function_registers_lines_and_rejects_other_sources() -> None:
    import linecache

    source = "def fn(n=undefined_name):\n    return [n]\n"
    code = compile_function("fn", source, "n0000000001")
    assert code.co_filename == f"{SOURCE_PREFIX}n0000000001>"
    assert linecache.getline(code.co_filename, 2) == "    return [n]\n"
    with pytest.raises(eks.tracing.TraceError, match="exactly one plain def"):
        compile_function("fn", "def other(n=1):\n    return n\n", "n0000000001")
    with pytest.raises(eks.tracing.TraceError, match="exactly one plain def"):
        compile_function("fn", "def fn(n=1):\n    return n\nx = 1\n", "n0000000001")
    with pytest.raises(eks.tracing.TraceError, match="cannot parse"):
        compile_function("fn", "def fn(:\n", "n0000000001")


def test_audit_hook_blocks_writes_and_listed_events_only() -> None:
    audit_hook("open", ("/tmp/readable", "r", os.O_RDONLY))
    audit_hook("open", ("/tmp/readable", None, os.O_RDONLY | os.O_CLOEXEC))
    audit_hook("import", ("json", None, None, None, None))
    for args in (("/tmp/x", "w", 0), ("/tmp/x", "rb+", 0), ("/tmp/x", None, os.O_WRONLY | os.O_CREAT),
                 ("/tmp/x", "a", 0)):
        with pytest.raises(PermissionError, match="open"):
            audit_hook("open", args)
    for event in sorted(BLOCKED_EVENTS):
        with pytest.raises(PermissionError, match=event):
            audit_hook(event, ())


def write_script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="the fake interpreters are shell scripts")
def test_a_dying_or_garbage_child_is_a_crash_with_the_stderr_tail(tmp_path: Path) -> None:
    plan, node = rows_plan()
    inputs: dict[str, Any] = {"items": [], "label": "row"}
    scripts = {
        "garbage": write_script(tmp_path / "garbage.sh", """
            cat > /dev/null
            echo 'this is not json'
            echo 'stderr says garbage' >&2
            exit 3
        """),
        "dies": write_script(tmp_path / "dies.sh", """
            echo 'stderr says dying' >&2
            kill -9 $$
        """),
        "flood": write_script(tmp_path / "flood.sh", f"""
            echo 'stderr says flood' >&2
            head -c {OUTPUT_LIMIT_BYTES + 65536} /dev/zero
            sleep 30
        """),
        "invalid": write_script(tmp_path / "invalid.sh", """
            cat > /dev/null
            printf '%s' '{"protocol": 1, "ok": true, "plan": {"kind": "nope"}}'
        """),
    }
    found: dict[str, SandboxFailure] = {}
    for name, script in scripts.items():
        fake = SandboxRunner(python=str(script), limits=SandboxLimits(timeout_seconds=60), scratch=tmp_path / "s")
        with pytest.raises(SandboxFailure) as caught:
            fake.dynamic(plan, node, instance="", inputs=inputs, tree=None, plugins=False, digests={},
                         session=plan.session)
        found[name] = caught.value
    assert all(failure.code == "sandbox_error" for failure in found.values())
    assert found["garbage"].detail["code"] == "crash"
    assert "does not parse" in found["garbage"].message
    assert "code 3" in found["garbage"].message
    assert "stderr says garbage" in found["garbage"].message
    assert found["dies"].detail["code"] == "crash"
    assert "stderr says dying" in found["dies"].message
    assert "no response" in found["dies"].message
    assert found["flood"].detail["code"] == "crash"
    assert f"more than {OUTPUT_LIMIT_BYTES} bytes" in found["flood"].message
    assert "stderr says flood" in found["flood"].message
    assert found["invalid"].detail["code"] == "protocol"
    assert "does not validate" in found["invalid"].message


def test_a_missing_interpreter_is_a_crash(tmp_path: Path) -> None:
    plan, node = rows_plan()
    missing = SandboxRunner(python=str(tmp_path / "no-python"), scratch=tmp_path / "s")
    request = SandboxRequest(plan=plan.model_dump(mode="json", by_alias=True),
                             node=node.model_dump(mode="json", by_alias=True))
    response = missing.run(request)
    assert not response.ok
    assert response.error is not None
    assert response.error.code == "crash"
    assert "could not start" in response.error.message


def test_stderr_keeps_only_the_tail(tmp_path: Path) -> None:
    plan, node = rows_plan()
    script = write_script(tmp_path / "noisy.sh", """
        cat > /dev/null
        i=0
        while [ $i -lt 2000 ]; do echo "early line $i padding padding padding padding padding" >&2; i=$((i+1)); done
        echo 'the very last line' >&2
        exit 1
    """)
    noisy = SandboxRunner(python=str(script), scratch=tmp_path / "s")
    request = SandboxRequest(plan=plan.model_dump(mode="json", by_alias=True),
                             node=node.model_dump(mode="json", by_alias=True))
    response = noisy.run(request)
    assert response.error is not None
    assert response.error.code == "crash"
    assert response.error.message.rstrip().endswith("the very last line")
    assert "early line 0 " not in response.error.message
    assert len(response.error.message.encode("utf-8")) <= STDERR_LIMIT_BYTES + 200
