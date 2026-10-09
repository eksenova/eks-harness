from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eks_harness import service
from eks_harness.config import Config
from eks_harness.daemon.leases import manager
from eks_harness.pools.backends import BackendManager, ProcessRegistry, alive, pid_started, repo_cache_root
from eks_harness.pools.backends import supervisor_env
from eks_harness.pools.base import BackendError, PoolHost

from conftest import AuthEnv, make_app

PROJECT = "acme/mobile-app"

DEFINITION = textwrap.dedent("""\
    #!{python}
    import json, sys
    from pathlib import Path
    command = sys.argv[1]
    context = json.loads(Path(sys.argv[sys.argv.index("--context") + 1]).read_text())
    state = Path(context["stateDir"])
    if command == "describe":
        print(json.dumps({{"description": "fake api", "ports": ["api"], "idleGraceSeconds": 1}}))
    elif command == "fingerprint":
        print(json.dumps({{"fingerprint": "v1"}}))
    elif command == "prepare":
        print("prepared", file=sys.stderr)
    elif command == "spec":
        port = context["ports"]["api"]
        code = ("import http.server, sys; port = int(sys.argv[1]); "
                "print(f'listening on http://127.0.0.1:{{port}}', flush=True); "
                "http.server.HTTPServer(('127.0.0.1', port), http.server.SimpleHTTPRequestHandler).serve_forever()")
        print(json.dumps({{
            "workdir": str(state),
            "processes": [{{"name": "fakeapi", "command": [sys.executable, "-c", code, str(port)], "port": "api",
                           "ready": {{"pattern": "listening on (?P<url>\\\\S+)", "timeout": 30}}}}],
            "exports": {{"EKS_LOCAL_API_URL": "{{captured.url}}", "EKS_API_PORT": "{{ports.api}}"}},
        }}))
    elif command == "teardown":
        (state / "teardown.txt").write_text(" ".join(sys.argv[2:]))
""")


def acquire(client: TestClient, instance: str, kind: str, headers: dict | None = None, **extra) -> dict:
    response = client.post("/api/leases/acquire", params={"waitSeconds": 5}, headers=headers,
                           json={"kind": kind, "project": PROJECT, "session": "main", "instance": instance, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_devices_and_profiles_are_listed(client: TestClient) -> None:
    devices = client.get("/api/devices").json()
    keys = [d["key"] for d in devices["items"]]
    assert keys == ["android:1", "android:2", "android:3", "ios:1", "ios:2", "ios:3"]
    assert devices["maxRunning"] == 3 and devices["running"] == 0
    assert devices["items"][0]["statusText"] == "Shut down"
    lease = acquire(client, "agent", "ios")
    detail = client.get("/api/devices/ios/1").json()
    assert detail["status"] == "on" and detail["statusText"] == "In use"
    assert detail["lease"]["sid"] == lease["sid"]
    assert detail["session"]["slug"] == "main"
    assert detail["history"][0]["sid"] == lease["sid"]
    assert any(e["type"] == "device.status" for e in detail["activity"])
    assert detail["logResource"] == "ios:1"
    profiles = client.get("/api/profiles").json()["items"]
    assert len(profiles) == 5 and profiles[0]["status"] == "off"
    assert client.get("/api/devices/ios/9").status_code == 404
    assert client.get("/api/devices/tv/1").status_code == 404


def test_manual_start_is_visible_and_never_takes_an_agent_lease(client: TestClient) -> None:
    started = client.post("/api/devices/android/2/start", json={})
    assert started.status_code == 200, started.text
    lease = started.json()["lease"]
    assert lease["ownerKind"] == "manual" and lease["ownerInstance"] == "manual:local"
    assert client.post("/api/devices/android/2/start", json={}).json()["lease"]["sid"] == lease["sid"]
    listed = {item["sid"]: item for item in client.get("/api/leases").json()["items"]}
    assert lease["sid"] in listed
    agent = acquire(client, "agent", "android")
    assert agent["resource"] == "android:1"
    taken = client.post("/api/devices/android/1/start", json={})
    assert taken.status_code == 409 and taken.json()["error"] == "resource_busy"
    client.post(f"/api/leases/{lease['sid']}/release", json={})
    assert client.get(f"/api/leases/{lease['sid']}").json()["state"] == "released"


def test_shutdown_needs_confirmation_and_breaks_the_agent_lease(client: TestClient, ctx) -> None:
    agent = acquire(client, "agent", "ios")
    refused = client.post("/api/devices/ios/1/shutdown", json={})
    assert refused.status_code == 409
    body = refused.json()
    assert body["error"] == "confirmation_required" and body["confirm"] in ("Harness iOS 1", "ios:1")
    assert body["interrupts"][0]["sid"] == agent["sid"] and body["interrupts"][0]["session"] == "main"
    done = client.post("/api/devices/ios/1/shutdown", json={"confirm": "ios:1", "reason": "stuck"})
    assert done.status_code == 200, done.text
    assert done.json()["brokenLeases"] == [agent["sid"]]
    gone = client.post(f"/api/leases/{agent['sid']}/heartbeat", json={})
    assert gone.status_code == 410 and gone.json()["state"] == "broken"
    lease = client.get(f"/api/leases/{agent['sid']}").json()
    assert lease["reason"] == "shut down by local from the UI: stuck"
    assert ctx.pools.host.devices["ios:1"]["status"] == "off"
    events = client.get("/api/events", params={"resource": "ios:1", "type": "device.action"}).json()["items"]
    assert events[0]["detail"]["brokenLeases"] == [agent["sid"]]


def test_reset_and_delete_devices(client: TestClient, ctx) -> None:
    client.post("/api/devices/ios/2/start", json={})
    udid = ctx.pools.host.devices["ios:2"]["udid"]
    reset = client.post("/api/devices/ios/2/reset", json={"confirm": "Harness iOS 2"})
    assert reset.status_code == 200 and len(reset.json()["brokenLeases"]) == 1
    assert ctx.pools.host.devices["ios:2"]["status"] == "off"
    deleted = client.post("/api/devices/ios/2/delete", json={"confirm": "ios:2"})
    assert deleted.status_code == 200
    assert ctx.pools.host.devices["ios:2"]["udid"] != udid
    assert client.post("/api/devices/ios/2/explode", json={}).status_code == 404
    assert not manager(ctx).busy


def test_pool_actions_need_rights(auth_env: AuthEnv) -> None:
    client = auth_env.client
    admin = auth_env.admin_headers
    agent = acquire(client, "agent", "ios", headers=admin)
    member, key = auth_env.make_user("mo")
    headers = auth_env.headers_for(key)
    assert client.post("/api/devices/ios/1/reset", json={"confirm": "ios:1"}, headers=headers).status_code == 403
    assert client.post("/api/devices/ios/1/shutdown", json={"confirm": "ios:1"}, headers=headers).status_code == 403
    listed = client.get("/api/devices/ios/1", headers=headers).json()
    assert listed["lease"]["sid"] == "hidden" and listed["session"] is None
    auth_env.grant(member, PROJECT, "editor")
    shown = client.get("/api/devices/ios/1", headers=headers).json()
    assert shown["lease"]["sid"] == agent["sid"]
    done = client.post("/api/devices/ios/1/shutdown", json={"confirm": "ios:1"}, headers=headers)
    assert done.status_code == 200 and done.json()["brokenLeases"] == [agent["sid"]]
    started = client.post("/api/devices/android/1/start", json={}, headers=headers)
    assert started.status_code == 200
    assert started.json()["lease"]["ownerUsername"] == "mo"


def test_profile_and_browser_actions(client: TestClient, ctx) -> None:
    first = acquire(client, "a", "browser")
    second = acquire(client, "b", "browser")
    assert (first["resource"], second["resource"]) == ("browser:1:1", "browser:1:2")
    closed = client.post("/api/profiles/browser:1:1/shutdown", json={"confirm": "browser:1:1"})
    assert closed.status_code == 200 and closed.json()["brokenLeases"] == [first["sid"]]
    assert ctx.pools.browsers.alive(1)
    third = acquire(client, "c", "browser")
    manager(ctx).update_meta(second["sid"], {"browserContextIds": ["context-b"]})
    refused = client.post("/api/profiles/browser:1:2/reset", json={})
    assert refused.status_code == 409 and [i["sid"] for i in refused.json()["interrupts"]] == [second["sid"]]
    assert client.post("/api/profiles/browser:1:2/reset", json={"confirm": "browser:1"}).status_code == 409
    reset = client.post("/api/profiles/browser:1:2/reset", json={"confirm": "browser:1:2"})
    assert reset.status_code == 200 and reset.json()["brokenLeases"] == [second["sid"]]
    assert ctx.pools.browsers.alive(1)
    assert client.get(f"/api/sid/{third['sid']}").json()["valid"] is True
    assert "closed 1 browser context(s)" in ctx.pools.browsers.log_path(1).read_text()
    client.post(f"/api/leases/{third['sid']}/release", json={})
    assert client.post("/api/browsers/1/stop", json={"confirm": "browser:1"}).status_code == 200
    assert not ctx.pools.browsers.alive(1)
    started = client.post("/api/browsers/1/start", json={})
    assert started.status_code == 200 and ctx.pools.browsers.alive(1)
    listing = client.get("/api/browsers").json()
    assert listing["items"][0]["status"] == "running" and listing["capacity"] == 5
    manual = client.post("/api/profiles/browser:1:3/start", json={}).json()["lease"]
    detail = client.get("/api/profiles/browser:1:3").json()
    assert detail["lease"]["sid"] == manual["sid"] and detail["status"] == "in_use"
    stopped = client.post("/api/browsers/1/stop", json={"confirm": "browser:1"})
    assert stopped.status_code == 200 and stopped.json()["brokenLeases"] == [manual["sid"]]
    assert not ctx.pools.browsers.alive(1)
    assert client.get("/api/profiles/browser:9:9").status_code == 404


def test_sweep_is_disabled_with_fake_pools(client: TestClient) -> None:
    result = client.post("/api/daemon/sweep", json={})
    assert result.status_code == 200 and "fake pools" in result.json()["skipped"]
    report = client.post("/api/daemon/housekeeping", json={}).json()
    assert "sweep" not in report and "tick" in report


def test_logs_api(client: TestClient, ctx) -> None:
    acquire(client, "agent", "android")
    log = client.get("/api/logs", params={"resource": "android:1", "lines": 50})
    assert log.status_code == 200 and "status on" in log.json()["text"]
    grep = client.get("/api/logs", params={"resource": "android:1", "grep": "booting"}).json()
    assert grep["text"] and all("booting" in line for line in grep["text"].splitlines())
    tail = client.get("/api/logs", params={"resource": "android:1", "offset": grep["offset"]}).json()
    assert tail["text"] == ""
    assert client.get("/api/logs", params={"resource": "nothing"}).status_code == 400
    assert client.get("/api/logs", params={"resource": "backend:none"}).status_code == 404
    assert client.get("/api/logs", params={"resource": "daemon", "grep": "("}).status_code == 400
    resources = [item["resource"] for item in client.get("/api/logs/resources").json()["items"]]
    assert "daemon" in resources and "browser:1" in resources and "ios:3" in resources


def test_status_reports_everything(client: TestClient) -> None:
    lease = acquire(client, "agent", "ios")
    status = client.get("/api/status").json()
    assert status["fakePools"] is True and status["pid"] == os.getpid()
    assert [item["sid"] for item in status["leases"]] == [lease["sid"]]
    assert len(status["devices"]) == 6 and status["browsers"][0]["index"] == 1
    assert client.post("/api/daemon/restart", json={}).status_code == 503


def write_definition(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "fakeapi"
    script.write_text(DEFINITION.format(python=sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return script


def wait_for(predicate, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("timed out")


@pytest.fixture
def real_backends(paths, config: Config):
    config.set_many({"backend.portRangeStart": 47000, "backend.portRangeEnd": 47999})
    host = PoolHost(config, paths)
    manager_ = BackendManager(host)
    yield host, manager_
    for record in list(host.backends.values()):
        manager_.stop(record["id"], final=True, reason="test cleanup")
    for entry in ProcessRegistry(repo_cache_root(paths)).all():
        ProcessRegistry(repo_cache_root(paths)).stop(entry)


def test_backend_definition_protocol_binding_and_auto_stop(paths, real_backends) -> None:
    host, backends = real_backends
    write_definition(paths.backends_dir)
    assert [d["name"] for d in backends.definitions()] == ["fakeapi"]
    bindings: list[str] = ["lease browser:1:1"]
    host.binding_source = lambda backend_id: list(bindings)
    record = backends.ensure({"definition": "fakeapi", "instance": "tree:/tmp/work", "hold": 0})
    backend_id = record["id"]
    assert backend_id == "fakeapi@tree:/tmp/work"
    wait_for(lambda: host.backends[backend_id]["status"] in ("running", "failed"))
    current = backends.public(host.backends[backend_id])
    assert current["status"] == "running", current.get("error")
    port = current["ports"]["api"]
    assert current["captured"]["url"] == f"http://127.0.0.1:{port}"
    assert current["exports"] == {"EKS_LOCAL_API_URL": f"http://127.0.0.1:{port}", "EKS_API_PORT": str(port)}
    assert current["alive"] == {"fakeapi": True}
    assert current["bindings"] == ["lease browser:1:1"]
    entry = backends.registry.load_role("fakeapi", "tree:/tmp/work")
    assert entry is not None and entry.supervised and alive(entry.pid, entry.started)
    exports = (backends.state_dir(host.backends[backend_id]) / "exports.env").read_text()
    assert f'EKS_LOCAL_API_URL="http://127.0.0.1:{port}"' in exports
    assert backends.ensure({"definition": "fakeapi", "instance": "tree:/tmp/work", "hold": 0})["status"] == "running"
    backends.tick()
    assert host.backends[backend_id]["status"] == "running" and "emptySince" not in host.backends[backend_id]
    bindings.clear()
    backends.tick()
    assert "emptySince" in host.backends[backend_id]
    time.sleep(1.2)
    backends.tick()
    assert host.backends[backend_id]["status"] == "stopped"
    wait_for(lambda: not alive(entry.pid, entry.started), 20)
    teardown = (backends.state_dir(host.backends[backend_id]) / "teardown.txt").read_text()
    assert teardown.startswith("--context ") and "--final" not in teardown
    assert backends.registry.load_role("fakeapi", "tree:/tmp/work") is None


def test_backend_failure_is_recorded(paths, real_backends) -> None:
    host, backends = real_backends
    folder = paths.backends_dir
    folder.mkdir(parents=True, exist_ok=True)
    broken = folder / "broken"
    broken.write_text(f"#!{sys.executable}\nimport sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
    broken.chmod(0o755)
    backends.ensure({"definition": "broken", "instance": "i"})
    wait_for(lambda: host.backends["broken@i"]["status"] in ("running", "failed"))
    record = host.backends["broken@i"]
    assert record["status"] == "failed" and "describe failed (exit 3)" in record["error"] and "boom" in record["error"]
    with pytest.raises(BackendError, match="invalid backend definition name"):
        backends.ensure({"definition": "../etc", "instance": "i"})


def test_backend_auto_stop_follows_lease_bindings(paths, config: Config, real_backends) -> None:
    host, _ = real_backends
    write_definition(paths.backends_dir)
    app = make_app(config)
    ctx = app.state.ctx
    real = BackendManager(ctx.pools.host)
    ctx.pools.backends = real
    with TestClient(app) as client:
        ensured = client.post("/api/backends/ensure", json={"definition": "fakeapi", "instance": "inst", "hold": 0})
        assert ensured.status_code == 200, ensured.text
        backend_id = ensured.json()["id"]
        wait_for(lambda: client.get(f"/api/backends/{backend_id}").json()["status"] in ("running", "failed"))
        lease = acquire(client, "inst", "browser", backend=backend_id)
        assert client.get(f"/api/backends/{backend_id}").json()["bindings"] == ["lease browser:1:1"]
        real.tick()
        assert ctx.pools.host.backends[backend_id]["status"] == "running"
        logs = client.get("/api/logs", params={"resource": f"backend:{backend_id}:fakeapi"}).json()
        assert "listening on" in logs["text"]
        own = client.get(f"/api/backends/{backend_id}/logs", params={"process": "fakeapi", "grep": "listening"})
        assert own.status_code == 200
        assert any(line.startswith("listening on") for line in own.json()["text"].splitlines())
        prepare = client.get(f"/api/backends/{backend_id}/logs").json()
        assert "prepared" in prepare["text"]
        client.post(f"/api/leases/{lease['sid']}/release", json={})
        real.tick()
        time.sleep(1.2)
        manager(ctx).tick()
        assert ctx.pools.host.backends[backend_id]["status"] == "stopped"
        stopped = client.post(f"/api/backends/{backend_id}/stop", json={"final": True})
        assert stopped.status_code == 200
        assert client.get(f"/api/backends/{backend_id}").status_code == 404


def test_service_files_point_at_the_installed_executable(paths, tmp_path: Path) -> None:
    fake = tmp_path / "bin" / "eks-harness"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    command = service.executable_command(which=lambda name: str(fake) if name == "eks-harness" else None)
    assert command == [str(fake.absolute())]
    fallback = service.executable_command(argv0="/nowhere/python", which=lambda name: None)
    assert fallback[-2:] == ["-m", "eks_harness.cli"] or fallback[0].endswith("eks-harness")
    spec = service.build_spec(paths, command, environ={"PATH": "/usr/bin", "ANDROID_HOME": "/sdk",
                                                       "EKS_HARNESS_HOME": str(paths.config_dir.parent)})
    assert spec.command == [str(fake.absolute()), "daemon", "serve", "--managed"]
    assert spec.env["ANDROID_HOME"] == "/sdk" and spec.env["EKS_HARNESS_HOME"] == str(paths.config_dir.parent)
    plist = service.launchd_plist(spec)
    assert f"<string>{fake.absolute()}</string>" in plist and "<string>--managed</string>" in plist
    assert "<key>SuccessfulExit</key><false/>" in plist and str(paths.daemon_log) in plist
    unit = service.systemd_unit(spec)
    assert f'ExecStart="{fake.absolute()}" "daemon" "serve" "--managed"' in unit
    assert 'Environment="ANDROID_HOME=/sdk"' in unit and "Restart=on-failure" in unit
    task = service.windows_task_command(spec)
    assert task.startswith('cmd.exe /c "') and '"--managed"' in task


def test_service_spec_sets_pythonpath_for_source_checkout(paths, tmp_path: Path, monkeypatch) -> None:
    import eks_harness

    src = tmp_path / "src"
    (src / "eks_harness").mkdir(parents=True)
    monkeypatch.setattr(eks_harness, "__file__", str(src / "eks_harness" / "__init__.py"))
    monkeypatch.setattr(service, "executable_command",
                        lambda *args, **kwargs: [sys.executable, "-m", "eks_harness.cli"])
    spec = service.build_spec(paths, None, environ={"PATH": "/usr/bin"})
    assert spec.env["PYTHONPATH"] == str(src)
    spec = service.build_spec(paths, None, environ={"PATH": "/usr/bin", "PYTHONPATH": "/other"})
    assert spec.env["PYTHONPATH"] == os.pathsep.join([str(src), "/other"])
    spec = service.build_spec(paths, None, environ={"PATH": "/usr/bin",
                                                    "PYTHONPATH": os.pathsep.join(["/keep", "relative"])})
    assert spec.env["PYTHONPATH"] == os.pathsep.join([str(src), "/keep"])
    plist = service.launchd_plist(spec)
    assert "<key>PYTHONPATH</key>" in plist


def test_supervisor_env_carries_source_checkout(tmp_path: Path, monkeypatch) -> None:
    import eks_harness

    src = tmp_path / "src"
    (src / "eks_harness").mkdir(parents=True)
    monkeypatch.setattr(eks_harness, "__file__", str(src / "eks_harness" / "__init__.py"))
    assert supervisor_env({})["PYTHONPATH"] == str(src)
    assert supervisor_env({"PYTHONPATH": "/lib"})["PYTHONPATH"] == os.pathsep.join([str(src), "/lib"])
    monkeypatch.chdir(tmp_path)
    assert supervisor_env({"PYTHONPATH": "rel"})["PYTHONPATH"] == os.pathsep.join([str(src), str(tmp_path / "rel")])


def test_supervisor_env_untouched_when_installed(monkeypatch) -> None:
    import eks_harness

    monkeypatch.setattr(eks_harness, "__file__",
                        "/lib/python3.13/site-packages/eks_harness/__init__.py")
    assert supervisor_env({}) == {}
    assert supervisor_env({"PYTHONPATH": "/lib"}) == {"PYTHONPATH": "/lib"}


requires_pwd = pytest.mark.skipif(sys.platform == "win32", reason="pwd is POSIX-only")


@requires_pwd
def test_service_spec_carries_user(paths, monkeypatch) -> None:
    import pwd

    spec = service.build_spec(paths, None, environ={"PATH": "/usr/bin", "USER": "tester"})
    assert spec.env["USER"] == "tester"
    expected = pwd.getpwuid(os.getuid()).pw_name
    spec = service.build_spec(paths, None, environ={"PATH": "/usr/bin"})
    assert spec.env["USER"] == expected
    plist = service.launchd_plist(spec)
    assert f"<key>USER</key><string>{expected}</string>" in plist


def test_service_spec_omits_pythonpath_when_installed(paths, monkeypatch) -> None:
    import eks_harness

    monkeypatch.setattr(eks_harness, "__file__",
                        "/lib/python3.13/site-packages/eks_harness/__init__.py")
    monkeypatch.setattr(service, "executable_command",
                        lambda *args, **kwargs: [sys.executable, "-m", "eks_harness.cli"])
    spec = service.build_spec(paths, None, environ={"PATH": "/usr/bin"})
    assert "PYTHONPATH" not in spec.env


def reloaded(paths, config: Config) -> tuple[PoolHost, BackendManager]:
    host = PoolHost(config, paths)
    host.load()
    return host, BackendManager(host)


def test_restart_readopts_a_starting_backend_without_restarting_it(paths, config: Config, real_backends) -> None:
    host, backends = real_backends
    write_definition(paths.backends_dir)
    backend_id = backends.ensure({"definition": "fakeapi", "instance": "adopt", "hold": 0})["id"]
    wait_for(lambda: host.backends[backend_id]["status"] in ("running", "failed"))
    entry = backends.registry.load_role("fakeapi", "adopt")
    assert host.backends[backend_id]["status"] == "running" and entry is not None
    host.backends[backend_id]["status"] = "starting"
    host.save()
    new_host, new_backends = reloaded(paths, config)
    assert new_host.backends[backend_id]["adopt"] is True
    assert new_backends.reconcile() == [backend_id]
    wait_for(lambda: new_host.backends[backend_id]["status"] in ("running", "failed"))
    assert new_host.backends[backend_id]["status"] == "running", new_host.backends[backend_id].get("error")
    again = new_backends.registry.load_role("fakeapi", "adopt")
    assert again is not None and again.pid == entry.pid
    assert new_host.backends[backend_id]["exports"]["EKS_API_PORT"]
    new_backends.stop(backend_id, final=True, reason="test cleanup")


def test_restart_waits_for_an_orphaned_prepare_and_does_not_repeat_it(paths, config: Config, real_backends) -> None:
    host, backends = real_backends
    write_definition(paths.backends_dir)
    backend_id = backends.ensure({"definition": "fakeapi", "instance": "prep", "hold": 0})["id"]
    wait_for(lambda: host.backends[backend_id]["status"] in ("running", "failed"))
    backends.stop(backend_id, reason="test")
    log = backends.log_path(host.backends[backend_id])
    prepares = log.read_text().count("fakeapi prepare")
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    record = host.backends[backend_id]
    record.update({"status": "preparing", "step": {"command": "prepare", "pid": orphan.pid,
                                                   "started": pid_started(orphan.pid), "at": time.time(),
                                                   "timeout": 60}})
    host.save()
    new_host, new_backends = reloaded(paths, config)
    new_backends.reconcile()
    time.sleep(0.5)
    assert new_host.backends[backend_id]["status"] == "preparing"
    wait_for(lambda: new_host.backends[backend_id]["status"] in ("running", "failed"), 30)
    assert new_host.backends[backend_id]["status"] == "running", new_host.backends[backend_id].get("error")
    assert log.read_text().count("fakeapi prepare") == prepares
    assert "step" not in new_host.backends[backend_id]
    new_backends.stop(backend_id, final=True, reason="test cleanup")


def test_restart_finishes_a_stop_that_was_in_flight(paths, config: Config, real_backends) -> None:
    host, backends = real_backends
    write_definition(paths.backends_dir)
    backend_id = backends.ensure({"definition": "fakeapi", "instance": "stopping", "hold": 0})["id"]
    wait_for(lambda: host.backends[backend_id]["status"] in ("running", "failed"))
    entry = backends.registry.load_role("fakeapi", "stopping")
    record = host.backends[backend_id]
    record.update({"status": "stopping", "stopFinal": False, "stopReason": "no frontend bound"})
    host.save()
    new_host, new_backends = reloaded(paths, config)
    new_backends.reconcile()
    wait_for(lambda: new_host.backends[backend_id]["status"] == "stopped", 30)
    wait_for(lambda: not alive(entry.pid, entry.started), 20)
    assert (new_backends.state_dir(new_host.backends[backend_id]) / "teardown.txt").exists()
