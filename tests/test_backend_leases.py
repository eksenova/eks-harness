from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from conftest import AuthEnv, make_app
from fastapi.testclient import TestClient

from eks_harness.config import Config, ConfigError
from eks_harness.daemon.leases import manager
from eks_harness.durations import NEVER, parse_duration
from eks_harness.pools.backend_info import (
    REDACTED,
    backend_identity,
    redact_info,
    reveal_info,
)
from eks_harness.pools.backends import (
    BackendManager,
    ProcessRegistry,
    alive,
    repo_cache_root,
)
from eks_harness.pools.base import BackendLeaseGone, PoolHost

PROJECT = "acme/web-app"

SCRIPT = textwrap.dedent("""\
    #!{python}
    import json, os, sys, time
    from pathlib import Path
    command = sys.argv[1]
    context = json.loads(Path(sys.argv[sys.argv.index("--context") + 1]).read_text())
    state = Path(context["stateDir"])
    knobs = json.loads(Path({knobs!r}).read_text())
    def note(text):
        with open(state / "calls.log", "a") as handle:
            handle.write(text + "\\n")
    if command == "describe":
        print(json.dumps({{"description": "slow api", "ports": ["api"], "idleTimeout": knobs.get("idle", 30),
                          "implements": ["describe", "fingerprint", "prepare", "spec", "info", "stop", "cleanup"]}}))
    elif command == "fingerprint":
        print(json.dumps({{"fingerprint": knobs.get("fingerprint", "v1")}}))
    elif command == "prepare":
        note("prepare")
        time.sleep(knobs.get("prepare", 0))
        if knobs.get("fail"):
            print("prepare broke on purpose", file=sys.stderr)
            sys.exit(4)
    elif command == "spec":
        port = context["ports"]["api"]
        code = ("import http.server, sys; port = int(sys.argv[1]); "
                "print(f'listening on http://127.0.0.1:{{port}}', flush=True); "
                "http.server.HTTPServer(('127.0.0.1', port), http.server.SimpleHTTPRequestHandler).serve_forever()")
        print(json.dumps({{
            "workdir": str(state),
            "processes": [{{"name": "slowapi", "command": [sys.executable, "-c", code, str(port)], "port": "api",
                           "ready": {{"pattern": "listening on (?P<url>\\\\S+)", "timeout": 30}}}}],
            "exports": {{"SLOW_API_URL": "{{captured.url}}"}},
            "info": {{"urls": {{"api": "{{captured.url}}"}}, "env": {{"SLOW_TOKEN": "tok-{{ports.api}}-secret"}},
                     "secretEnv": ["SLOW_TOKEN"]}},
        }}))
    elif command == "info":
        print(json.dumps({{"version": "9.9.9", "credentials": {{"admin": {{"username": "root",
                                                                          "password": "pw-very-secret"}}}},
                          "extra": {{"tenant": "acme", "apiKey": {{"$secret": "key-very-secret"}}}}}}))
    elif command == "stop":
        note("stop")
    elif command == "cleanup":
        note("cleanup")
""")

MANAGED_PLUGIN = textwrap.dedent("""\
    import os, signal, subprocess, sys, time, urllib.request
    from pathlib import Path

    from eks_harness.plugins.sdk import BackendInfo, Secret


    class Managed:
        def __init__(self, context=None):
            self.context = context

        def describe(self):
            return {"description": "self-managed http server", "ports": ["http"], "idleTimeout": "1s"}

        def note(self, context, text):
            with open(Path(context["stateDir"]) / "calls.log", "a") as handle:
                handle.write(text + "\\n")

        def start(self, context):
            state = Path(context["stateDir"])
            port = context["ports"]["http"]
            process = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                                       cwd=state, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            (state / "server.pid").write_text(str(process.pid))
            self.note(context, "start")
            return BackendInfo(url=f"http://127.0.0.1:{port}", version="1.2.3",
                               credentials={"admin": {"password": Secret("managed-pass-123")}},
                               env={"MANAGED_TOKEN": Secret("managed-token-456")}, secret_env=("MANAGED_TOKEN",),
                               extra={"tenant": "acme"})

        def health(self, context):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{context['ports']['http']}/", timeout=2)
                return True
            except Exception:
                return False

        def stop(self, context):
            pid_file = Path(context["stateDir"]) / "server.pid"
            if pid_file.exists():
                try:
                    os.killpg(int(pid_file.read_text()), signal.SIGTERM)
                except OSError:
                    pass
                pid_file.unlink()
            self.note(context, "stop")

        def cleanup(self, context):
            self.note(context, "cleanup")
""")


def wait_for(predicate, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("timed out")


@pytest.fixture
def knobs(tmp_path: Path) -> Path:
    file = tmp_path / "knobs.json"
    file.write_text("{}", encoding="utf-8")
    return file


def set_knobs(file: Path, **values) -> None:
    file.write_text(json.dumps(values), encoding="utf-8")


def write_script(folder: Path, knobs: Path, name: str = "slowapi") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / name
    script.write_text(SCRIPT.format(python=sys.executable, knobs=str(knobs)), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return script


def write_managed_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "managed-repo"
    plugin = tree / ".harness" / "plugins" / "managed"
    (plugin / "managed_backend").mkdir(parents=True)
    (plugin / "harness-plugin.toml").write_text(textwrap.dedent("""\
        [plugin]
        id = "acme.managed"
        name = "Managed"
        version = "1.0.0"
        api = "1"

        [[contributes.backend]]
        id = "managed"
        entry = "managed_backend.backend:Managed"
        idle_timeout = "5m"
    """), encoding="utf-8")
    (plugin / "managed_backend" / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "managed_backend" / "backend.py").write_text(MANAGED_PLUGIN, encoding="utf-8")
    (tree / ".harness" / "project.toml").write_text('[project]\nid = "acme/managed"\n\n[plugins]\n'
                                                   'trust = ["acme.managed"]\n', encoding="utf-8")
    return tree.resolve()


@pytest.fixture
def real(paths, config: Config):
    config.set_many({"backend.portRangeStart": 46000, "backend.portRangeEnd": 46999})
    host = PoolHost(config, paths)
    backends = BackendManager(host)
    yield host, backends
    for record in list(host.backends.values()):
        backends.stop(record["id"], final=True, reason="test cleanup")
    registry = ProcessRegistry(repo_cache_root(paths))
    for entry in registry.all():
        registry.stop(entry)


def calls(backends: BackendManager, record: dict) -> list[str]:
    path = backends.state_dir(record) / "calls.log"
    return path.read_text().split() if path.exists() else []


def test_durations_and_identity() -> None:
    assert parse_duration("15m") == 900 and parse_duration("1h30m") == 5400 and parse_duration(0) == 0
    assert parse_duration("never") == NEVER and parse_duration("2 min") == 120
    for bad in ("soon", "-1", "", "5x", True):
        with pytest.raises(ValueError):
            parse_duration(bad)
    assert backend_identity("/tmp/a") == f"tree:{Path('/tmp/a').resolve()}"
    assert backend_identity("/tmp/a", key="shared") == "shared"
    with_params = backend_identity("/tmp/a", params={"tenant": "x"})
    assert with_params.startswith("tree:") and "#" in with_params
    assert with_params == backend_identity("/tmp/a", params={"tenant": "x"})
    assert with_params != backend_identity("/tmp/a", params={"tenant": "y"})


def test_idle_timeout_setting_is_validated(config: Config) -> None:
    config.set("backend.idleTimeout", "never")
    assert config["backend.idleTimeout"] == "never"
    with pytest.raises(ConfigError):
        config.set("backend.idleTimeout", "whenever")


def test_info_redaction_keeps_shape_and_hides_secrets() -> None:
    info = {"url": "http://x", "credentials": {"admin": {"username": "root", "password": "pw"}},
            "env": {"TOKEN": "t-1", "PLAIN": "p"}, "secretEnv": ["TOKEN"],
            "extra": {"apiKey": {"$secret": "k-1"}, "tenant": "acme"}}
    redacted = redact_info(info)
    assert redacted["credentials"] == {"admin": {"username": REDACTED, "password": REDACTED}}
    assert redacted["env"] == {"TOKEN": REDACTED, "PLAIN": "p"}
    assert redacted["extra"] == {"apiKey": REDACTED, "tenant": "acme"} and redacted["redacted"] is True
    full = reveal_info(info)
    assert full["extra"]["apiKey"] == "k-1" and full["credentials"]["admin"]["password"] == "pw"
    assert full["redacted"] is False


def test_concurrent_acquires_share_one_start(paths, real, knobs: Path) -> None:
    host, backends = real
    set_knobs(knobs, prepare=1.0)
    write_script(paths.backends_dir, knobs)
    results: list[dict] = []
    errors: list[Exception] = []

    def take(index: int) -> None:
        try:
            results.append(backends.acquire({"definition": "slowapi", "key": "shared", "holder": f"agent-{index}"}))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=take, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    backend_id = "slowapi@shared"
    assert {r["backend"]["id"] for r in results} == {backend_id}
    assert backends.wait_ready(backend_id, 60) == "running", host.backends[backend_id].get("error")
    record = host.backends[backend_id]
    assert len(record["leases"]) == 6 and record["generation"] == 1
    assert calls(backends, record).count("prepare") == 1
    assert {lease["holder"] for lease in record["leases"].values()} == {f"agent-{i}" for i in range(6)}


def test_ref_counting_ttl_expiry_and_grace_window_reuse(paths, real, knobs: Path) -> None:
    host, backends = real
    set_knobs(knobs, idle=2)
    write_script(paths.backends_dir, knobs)
    first = backends.acquire({"definition": "slowapi", "key": "k", "holder": "a", "ttl": 0.5})
    second = backends.acquire({"definition": "slowapi", "key": "k", "holder": "b", "ttl": 60})
    backend_id = first["backend"]["id"]
    assert backends.wait_ready(backend_id, 60) == "running"
    entry = backends.registry.load_role("slowapi", "k")
    time.sleep(0.7)
    backends.tick()
    record = host.backends[backend_id]
    assert list(record["leases"]) == [second["lease"]["id"]]
    with pytest.raises(BackendLeaseGone):
        backends.renew(first["lease"]["id"])
    renewed = backends.renew(second["lease"]["id"], "5m")
    assert renewed["lease"]["ttl"] == 300 and renewed["lease"]["expiresIn"] > 290
    backends.release(second["lease"]["id"])
    backends.tick()
    assert host.backends[backend_id]["idleSince"] and host.backends[backend_id]["status"] == "running"
    time.sleep(1.0)
    again = backends.acquire({"definition": "slowapi", "key": "k", "holder": "c"})
    assert again["status"] == "granted" and again["backend"]["generation"] == 1
    assert "idleSince" not in host.backends[backend_id]
    time.sleep(1.5)
    backends.tick()
    assert host.backends[backend_id]["status"] == "running"
    assert backends.registry.load_role("slowapi", "k").pid == entry.pid
    assert calls(backends, host.backends[backend_id]).count("prepare") == 1


def test_idle_timeout_stops_and_runs_the_cleanup_hook(paths, real, knobs: Path) -> None:
    host, backends = real
    set_knobs(knobs, idle=1)
    write_script(paths.backends_dir, knobs)
    response = backends.acquire({"definition": "slowapi", "key": "idle", "holder": "a"})
    backend_id = response["backend"]["id"]
    assert backends.wait_ready(backend_id, 60) == "running"
    record = dict(host.backends[backend_id])
    entry = backends.registry.load_role("slowapi", "idle")
    info_file = backends.info_file(record)
    assert info_file.exists() and stat.S_IMODE(info_file.stat().st_mode) == 0o600
    backends.release(response["lease"]["id"])
    backends.tick()
    assert backend_id in host.backends
    time.sleep(1.2)
    backends.tick()
    assert backend_id not in host.backends
    wait_for(lambda: not alive(entry.pid, entry.started), 20)
    assert calls(backends, record) == ["prepare", "stop", "cleanup"]
    assert not info_file.exists()


def test_idle_timeout_zero_and_never(paths, real, knobs: Path, tmp_path: Path) -> None:
    host, backends = real
    set_knobs(knobs, idle=0)
    write_script(paths.backends_dir, knobs)
    response = backends.acquire({"definition": "slowapi", "key": "zero", "holder": "a"})
    assert backends.wait_ready(response["backend"]["id"], 60) == "running"
    backends.release(response["lease"]["id"])
    wait_for(lambda: response["backend"]["id"] not in host.backends, 20)
    tree = tmp_path / "never-tree"
    (tree / ".harness").mkdir(parents=True)
    (tree / ".harness" / "project.toml").write_text('[backends.slowapi]\nidle_timeout = "never"\n', encoding="utf-8")
    kept = backends.acquire({"definition": "slowapi", "tree": str(tree), "holder": "a"})
    backend_id = kept["backend"]["id"]
    assert backend_id == f"slowapi@tree:{tree.resolve()}"
    assert backends.wait_ready(backend_id, 60) == "running"
    assert host.backends[backend_id]["idleTimeout"] == "never"
    assert host.backends[backend_id]["idleTimeoutSource"] == "project"
    backends.release(kept["lease"]["id"])
    time.sleep(0.3)
    backends.tick()
    assert host.backends[backend_id]["status"] == "running" and backends.public(host.backends[backend_id])[
        "stopsAt"] is None


def test_fingerprint_change_replaces_the_instance(paths, real, knobs: Path) -> None:
    host, backends = real
    write_script(paths.backends_dir, knobs)
    first = backends.acquire({"definition": "slowapi", "key": "fp", "holder": "a"})
    backend_id = first["backend"]["id"]
    assert backends.wait_ready(backend_id, 60) == "running"
    old = backends.registry.load_role("slowapi", "fp")
    set_knobs(knobs, fingerprint="v2")
    host.backends[backend_id]["verifiedAt"] = 0
    backends.acquire({"definition": "slowapi", "key": "fp", "holder": "b"})
    assert host.backends[backend_id]["status"] in ("stopping", "stopped", "preparing", "starting")
    wait_for(lambda: host.backends[backend_id].get("generation") == 2
             and host.backends[backend_id]["status"] == "running", 60)
    assert len(host.backends[backend_id]["leases"]) == 2
    wait_for(lambda: not alive(old.pid, old.started), 20)
    assert backends.registry.load_role("slowapi", "fp").pid != old.pid


def test_daemon_restart_readopts_held_backends_and_cleans_up_orphans(paths, config: Config, real,
                                                                     knobs: Path) -> None:
    host, backends = real
    write_script(paths.backends_dir, knobs)
    held = backends.acquire({"definition": "slowapi", "key": "held", "holder": "a", "ttl": 30})
    loose = backends.acquire({"definition": "slowapi", "key": "loose", "holder": "b"})
    assert backends.wait_ready(held["backend"]["id"], 60) == "running"
    assert backends.wait_ready(loose["backend"]["id"], 60) == "running"
    backends.release(loose["lease"]["id"])
    held_entry = backends.registry.load_role("slowapi", "held")
    loose_record = dict(host.backends[loose["backend"]["id"]])
    loose_entry = backends.registry.load_role("slowapi", "loose")
    os.killpg(loose_entry.pid, signal.SIGKILL)
    orphan = backends.registry.start("stray", [sys.executable, "-c", "import time; time.sleep(60)"],
                                     instance="gone", meta={"backend": "slowapi@gone", "definition": "slowapi"})
    host.save()
    new_host = PoolHost(config, paths)
    new_host.load()
    new_backends = BackendManager(new_host)
    new_backends.reconcile()
    lease = new_host.backends[held["backend"]["id"]]["leases"][held["lease"]["id"]]
    assert lease["expires"] >= time.time() + 100
    wait_for(lambda: loose["backend"]["id"] not in new_host.backends, 30)
    assert calls(new_backends, loose_record)[-1] == "cleanup"
    wait_for(lambda: not alive(orphan.pid, orphan.started), 20)
    time.sleep(0.5)
    record = new_host.backends[held["backend"]["id"]]
    assert record["status"] == "running" and record["generation"] == 1
    assert new_backends.registry.load_role("slowapi", "held").pid == held_entry.pid
    for item in list(new_host.backends.values()):
        new_backends.stop(item["id"], final=True, reason="test cleanup")


def test_self_managed_plugin_lifecycle_and_info(config: Config, paths, real, tmp_path: Path) -> None:
    host, backends = real
    tree = write_managed_tree(tmp_path)
    response = backends.acquire({"definition": "managed", "tree": str(tree), "holder": "agent", "ttl": 60})
    backend_id = response["backend"]["id"]
    assert backends.wait_ready(backend_id, 90) == "running", host.backends[backend_id].get("error")
    record = host.backends[backend_id]
    assert record["processes"] == [] and record["idleTimeout"] == 1.0
    public = backends.public(record)
    port = record["ports"]["http"]
    assert public["info"]["url"] == f"http://127.0.0.1:{port}" and public["info"]["version"] == "1.2.3"
    assert public["info"]["credentials"] == {"admin": {"password": REDACTED}}
    assert public["info"]["env"]["MANAGED_TOKEN"] == REDACTED and public["info"]["extra"] == {"tenant": "acme"}
    full = backends.info(backend_id, secrets=True)
    assert full["credentials"]["admin"]["password"] == "managed-pass-123"
    assert full["env"]["MANAGED_TOKEN"] == "managed-token-456" and full["worktree"] == str(tree)
    for text in (paths.pools_state_file.read_text(), backends.log_path(record).read_text()):
        assert "managed-pass-123" not in text and "managed-token-456" not in text
    new_host = PoolHost(config, paths)
    new_host.load()
    new_backends = BackendManager(new_host)
    new_backends.readopt(backend_id)
    assert new_host.backends[backend_id]["status"] == "running"
    state = backends.state_dir(record)
    os.killpg(int((state / "server.pid").read_text()), signal.SIGTERM)
    backends.release(response["lease"]["id"])
    new_host.load()
    new_backends.reconcile()
    wait_for(lambda: backend_id not in new_host.backends, 60)
    assert calls(backends, record) == ["start", "stop", "cleanup"]


def acquire_driver(client: TestClient, instance: str, **extra) -> dict:
    response = client.post("/api/leases/acquire", params={"waitSeconds": 10},
                           json={"kind": "browser", "project": PROJECT, "session": "main", "instance": instance,
                                 **extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_backend_lease_api_redacts_secrets_and_links_drivers(client: TestClient, ctx, paths) -> None:
    taken = client.post("/api/backend-leases", json={"definition": "api", "key": "shared", "holder": "agent-1",
                                                     "ttl": "10m"})
    assert taken.status_code == 200, taken.text
    data = taken.json()
    lease_id = data["lease"]["id"]
    assert data["status"] == "granted" and data["lease"]["ttl"] == 600
    assert data["info"]["credentials"]["admin"]["password"] == REDACTED
    secret = "fake-api-secret"
    revealed = client.get(f"/api/backend-leases/{lease_id}/info", params={"secrets": "true"}).json()
    assert revealed["credentials"]["admin"]["password"] == secret and revealed["redacted"] is False
    listed = client.get("/api/backends").json()["items"][0]
    assert secret not in json.dumps(listed) and listed["leases"][0]["holder"] == "agent-1"
    driver = acquire_driver(client, "driver-1", backend=lease_id)
    sid = driver["sid"]
    assert driver["status"] == "granted" and driver["backend"]["id"] == "api@shared"
    linked = client.get(f"/api/leases/{sid}/backend", params={"secrets": "true"}).json()
    assert linked["lease"]["driverSid"] == sid and linked["info"]["credentials"]["admin"]["password"] == secret
    assert client.get(f"/api/leases/{sid}/backend").json()["info"]["credentials"]["admin"]["password"] == REDACTED
    record = ctx.pools.backends.get("api@shared")
    assert len(record["leases"]) == 2
    beat = client.post(f"/api/leases/{sid}/heartbeat", json={}).json()
    assert beat["backend"]["generation"] == 1 and beat["backend"]["status"] == "running"
    restarted = client.post("/api/backend-leases", json={"definition": "api", "key": "shared", "holder": "agent-2",
                                                         "restart": True}).json()
    assert restarted["backend"]["generation"] == 2
    beat = client.post(f"/api/leases/{sid}/heartbeat", json={}).json()
    assert beat["backend"]["generation"] == 2
    events = client.get("/api/events", params={"limit": 200}).json()
    replaced = [e for e in events["items"] if e["type"] == "backend.replaced"]
    assert replaced and replaced[-1]["detail"]["linked"] == [sid]
    assert secret not in json.dumps(events)
    assert secret not in paths.pools_state_file.read_text()
    client.post(f"/api/leases/{sid}/release", json={})
    assert all(not lease.get("driverSid") for lease in ctx.pools.backends.get("api@shared")["leases"].values())
    released = client.post(f"/api/backend-leases/{lease_id}/release", json={}).json()
    assert released["status"] == "released" and len(released["backend"]["leases"]) == 1
    assert client.post(f"/api/backend-leases/{lease_id}/renew", json={}).status_code == 404


def test_driver_link_by_definition_and_unlink(client: TestClient, ctx, tmp_path: Path) -> None:
    tree = tmp_path / "api-tree"
    tree.mkdir()
    driver = acquire_driver(client, "driver-2", backend="api", backend_tree=str(tree))
    backend_id = f"api@tree:{tree.resolve()}"
    assert driver["backend"]["id"] == backend_id
    assert client.get(f"/api/leases/{driver['sid']}").json()["backendId"] == backend_id
    again = acquire_driver(client, "driver-3", backend=backend_id)
    record = ctx.pools.backends.get(backend_id)
    assert sorted(lease["driverSid"] for lease in record["leases"].values()) == sorted([driver["sid"], again["sid"]])
    assert record["generation"] == 1
    unlinked = client.delete(f"/api/leases/{driver['sid']}/backend")
    assert unlinked.status_code == 200 and len(unlinked.json()["released"]) == 1
    assert client.get(f"/api/leases/{driver['sid']}/backend").status_code == 404
    bad = client.post("/api/leases/acquire", params={"waitSeconds": 5},
                      json={"kind": "browser", "project": PROJECT, "session": "main", "instance": "driver-4",
                            "backend": "bl_0000000000000000"})
    assert bad.status_code == 400 and bad.json()["error"] == "backend_error"
    assert client.get("/api/leases", params={"instance": "driver-4"}).json()["items"] == []
    linked = client.post(f"/api/leases/{again['sid']}/backend", json={"backend": "api", "key": "other"})
    assert linked.status_code == 200 and linked.json()["backend"]["id"] == "api@other"
    assert all(lease.get("driverSid") != again["sid"]
               for lease in ctx.pools.backends.get(backend_id)["leases"].values())


def test_stale_driver_link_is_dropped_by_the_tick(client: TestClient, ctx) -> None:
    driver = acquire_driver(client, "driver-5", backend="api")
    backend_id = driver["backend"]["id"]
    from eks_harness.db.repos import leases as leases_repo

    lease = leases_repo.get_by_sid(ctx.db.conn(), driver["sid"])
    with ctx.db.transaction() as conn:
        leases_repo.end(conn, lease.id, "released", "gone without the manager")
    ctx.pools.backends.tick()
    assert ctx.pools.backends.get(backend_id)["leases"] == {}


def test_linked_driver_waits_for_the_backend(paths, config: Config, real, knobs: Path) -> None:
    set_knobs(knobs, prepare=2.0)
    write_script(paths.backends_dir, knobs)
    app = make_app(config)
    ctx = app.state.ctx
    ctx.pools.backends = BackendManager(ctx.pools.host)
    with TestClient(app) as client:
        first = client.post("/api/leases/acquire", params={"waitSeconds": 0.5},
                            json={"kind": "browser", "project": PROJECT, "session": "main", "instance": "w1",
                                  "backend": "slowapi", "backend_key": "wait"}).json()
        assert first["status"] == "preparing" and first["backend"]["status"] in ("preparing", "starting")
        done = client.post("/api/leases/resume", params={"waitSeconds": 60}, json={"sid": first["sid"]}).json()
        assert done["status"] == "granted" and done["backend"]["status"] == "running"
        set_knobs(knobs, fail=True)
        failed = client.post("/api/leases/acquire", params={"waitSeconds": 60},
                             json={"kind": "browser", "project": PROJECT, "session": "main", "instance": "w2",
                                   "backend": "slowapi", "backend_key": "broken"}).json()
        assert failed["status"] == "failed" and "slowapi@broken failed" in failed["error"]
        for item in list(ctx.pools.host.backends.values()):
            ctx.pools.backends.stop(item["id"], final=True, reason="test cleanup")


def test_secrets_need_the_holder(auth_env: AuthEnv) -> None:
    client = auth_env.client
    member, key = auth_env.make_user("mia")
    _, other_key = auth_env.make_user("otto")
    auth_env.grant(member, PROJECT, "editor")
    refused = client.post("/api/backend-leases", headers=auth_env.headers_for(key),
                          json={"definition": "api", "key": "auth"})
    assert refused.status_code == 403
    taken = client.post("/api/backend-leases", headers=auth_env.headers_for(key),
                        json={"definition": "api", "key": "auth", "project": PROJECT, "session": "main"})
    assert taken.status_code == 200, taken.text
    lease_id = taken.json()["lease"]["id"]
    mine = client.get(f"/api/backend-leases/{lease_id}/info", params={"secrets": "true"},
                      headers=auth_env.headers_for(key))
    assert mine.status_code == 200 and mine.json()["credentials"]["admin"]["password"] == "fake-api-secret"
    theirs = client.get(f"/api/backend-leases/{lease_id}/info", params={"secrets": "true"},
                        headers=auth_env.headers_for(other_key))
    assert theirs.status_code == 403
    listed = client.get("/api/backends", headers=auth_env.headers_for(other_key)).json()["items"]
    assert listed[0]["redacted"] is True and listed[0]["leases"][0]["id"] == ""
    assert client.get("/api/backend-leases", headers=auth_env.headers_for(other_key)).json()["items"] == []


def test_instance_end_releases_its_backend_leases(client: TestClient, ctx) -> None:
    taken = client.post("/api/backend-leases", json={"definition": "api", "key": "end", "instance": "session:gone",
                                                     "holder": "gone"}).json()
    kept = client.post("/api/backend-leases", json={"definition": "api", "key": "end", "instance": "session:stay",
                                                    "holder": "stay"}).json()
    manager(ctx).instance_ended("session:gone")
    leases = ctx.pools.backends.get("api@end")["leases"]
    assert list(leases) == [kept["lease"]["id"]] and taken["lease"]["id"] not in leases


def test_stop_ends_every_lease(client: TestClient, ctx) -> None:
    client.post("/api/backend-leases", json={"definition": "api", "key": "stop", "holder": "x"})
    stopped = client.post("/api/backends/api@stop/stop", json={"cleanup": True})
    assert stopped.status_code == 200 and stopped.json()["leases"] == []
    assert ctx.pools.backends.get("api@stop") is None


def test_cli_backend_lease_commands(capsys, monkeypatch, config: Config, tmp_path: Path) -> None:
    from conftest import harness_client

    from eks_harness.cli import backend_cmds, lease_cmds, main

    app = make_app(config)
    tree = tmp_path / "cli-tree"
    tree.mkdir()
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    monkeypatch.setenv("EKS_HARNESS_INSTANCE", "session:cli")
    with TestClient(app) as test_client:
        def factory(args):
            return harness_client(test_client, config, None)

        monkeypatch.setattr(backend_cmds, "client_from_args", factory)
        monkeypatch.setattr(lease_cmds, "client_from_args", factory)
        assert main(["backend", "acquire", "--definition", "api", "--key", "cli", "--ttl", "5m", "--shell",
                     "--secrets"]) == 0
        lines = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
        lease_id = lines["EKS_BACKEND_LEASE"]
        assert lines["EKS_BACKEND_ID"] == "api@cli" and lines["EKS_BACKEND_URL"].startswith("http://127.0.0.1:")
        assert "fake-api-secret" in lines["EKS_BACKEND_CREDENTIALS"]
        assert main(["backend", "acquire", "--definition", "api", "--key", "cli", "--quiet"]) == 0
        out = capsys.readouterr()
        assert out.out.splitlines()[0].startswith("bl_") and "http://127.0.0.1:" in out.out
        assert "2 lease(s)" in out.err
        assert main(["backend", "info", lease_id]) == 0
        shown = capsys.readouterr().out
        assert "fake-api-secret" not in shown and "<redacted>" in shown
        assert main(["backend", "info", lease_id, "--secrets", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["credentials"]["admin"]["password"] == "fake-api-secret"
        assert main(["backend", "leases", "--json"]) == 0
        assert len(json.loads(capsys.readouterr().out)["items"]) == 2
        assert main(["lease", "acquire", "--kind", "browser", "--project", PROJECT, "--session", "main",
                     "--backend", lease_id, "--tree", str(tree), "--shell"]) == 0
        exports = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
        assert exports["EKS_BACKEND_ID"] == "api@cli" and exports["EKS_BACKEND_LEASE"].startswith("bl_")
        assert main(["backend", "release", lease_id]) == 0
        assert "keeps running" in capsys.readouterr().err
        assert main(["backend", "list"]) == 0
        listed = capsys.readouterr().out
        assert "api@cli" in listed and "api@cli: http://127.0.0.1:" in listed
        monkeypatch.setattr(lease_cmds, "client_from_args", factory)
        assert main(["lease", "status"]) == 0
        assert "Backends" in capsys.readouterr().out


def test_records_from_older_daemons_are_upgraded(config: Config, paths) -> None:
    from eks_harness.pools.fake import FakeBackendManager

    host = PoolHost(config, paths)
    stamp = time.time()
    host.backends["api@session:old"] = {
        "id": "api@session:old", "definition": "api", "instance": "session:old", "status": "running",
        "definitionPath": "fake://backends/api", "holds": {"ensure": stamp + 300, "gone": stamp - 10},
        "idleGraceSeconds": 60, "ports": {"api": 5400}}
    host.backends["api@session:empty"] = {
        "id": "api@session:empty", "definition": "api", "instance": "session:empty", "status": "running",
        "definitionPath": "fake://backends/api", "emptySince": stamp - 120, "idleGraceSeconds": 60}
    host.save()
    reloaded = PoolHost(config, paths)
    reloaded.load()
    backends = FakeBackendManager(reloaded)
    backends.reconcile()
    old = reloaded.backends["api@session:old"]
    assert "holds" not in old and [lease["holder"] for lease in old["leases"].values()] == ["ensure"]
    assert old["generation"] == 1
    backends.tick()
    assert "api@session:empty" not in reloaded.backends
    assert reloaded.backends["api@session:old"]["status"] == "running"
    assert reloaded.backends["api@session:old"]["idleTimeout"] == 60.0
