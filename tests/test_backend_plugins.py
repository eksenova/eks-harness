from __future__ import annotations

import json
import textwrap
import time
from pathlib import Path

import httpx

from eks_harness.backends.runner import main as runner_main
from eks_harness.plugins.scaffold import scaffold


def make_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "repo"
    plugins = tree / ".harness" / "plugins"
    scaffold("backend", plugins / "api", "acme.api")
    scaffold("seeder", plugins / "seed", "acme.seed")
    scaffold("credentials", plugins / "people", "acme.people")
    policy = plugins / "policy"
    policy.mkdir(parents=True)
    (policy / "harness-plugin.toml").write_text(textwrap.dedent("""
        [plugin]
        id = "acme.policy"
        [[contributes.backend_policy]]
        id = "branch"
        entry = "acme_policy:Policy"
    """), encoding="utf-8")
    (policy / "acme_policy.py").write_text(textwrap.dedent("""
        from eks_harness.plugins.sdk import BackendChoice

        class Policy:
            def choose(self, tree, requested=None):
                if requested:
                    return BackendChoice(requested, "asked for it")
                return BackendChoice("api", "the branch changes the API", target="local")
    """), encoding="utf-8")
    seed_file = plugins / "seed" / "harness-plugin.toml"
    seed_file.write_text(seed_file.read_text().replace('entry = "acme_seed.seed:Seeder"',
                                                       'entry = "acme_seed.seed:Seeder"\nbackends = ["api"]'))
    (tree / ".harness" / "project.toml").write_text(textwrap.dedent("""
        [project]
        id = "acme/web"
        [plugins]
        trust = ["api", "seed", "people", "policy"]
    """), encoding="utf-8")
    return tree


def test_runner_speaks_the_definition_protocol(tmp_path: Path, capsys) -> None:
    tree = make_tree(tmp_path)
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"tree": str(tree), "ports": {"http": 5999}}), encoding="utf-8")
    assert runner_main(["acme.api", "api", "describe", "--context", str(context)]) == 0
    assert json.loads(capsys.readouterr().out)["ports"] == ["http"]
    assert runner_main(["acme.api", "api", "spec", "--context", str(context)]) == 0
    spec = json.loads(capsys.readouterr().out)
    process = spec["processes"][0]
    assert process["port"] == "http" and process["health"]["url"] == "http://127.0.0.1:5999/"
    assert spec["exports"] == {"API_BASE_URL": "http://127.0.0.1:{ports.http}"}
    assert runner_main(["acme.api", "api", "fingerprint", "--context", str(context)]) == 0
    assert json.loads(capsys.readouterr().out) == {"fingerprint": ""}
    assert runner_main(["acme.api", "api", "teardown", "--context", str(context), "--final"]) == 0


def test_backend_api_with_plugins(client, ctx, tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    definitions = client.get("/api/backends/definitions", params={"tree": str(tree)}).json()["items"]
    plugin_def = next(d for d in definitions if d["name"] == "api")
    assert plugin_def["source"] == "plugin" and plugin_def["path"] == "plugin:acme.api:api"
    choice = client.post("/api/backends/choose", json={"tree": str(tree)}).json()
    assert choice["backend"] == "api" and choice["target"] == "local"
    assert client.post("/api/backends/choose", json={"tree": str(tree), "requested": "staging"}).json()["backend"] == "staging"
    people = client.get("/api/backends/personas", params={"tree": str(tree)}).json()["items"]
    assert people[0]["id"] == "admin" and "password" not in json.dumps(people)
    seeders = client.get("/api/backends/seeders", params={"tree": str(tree), "backend": "api"}).json()["items"]
    assert seeders[0]["scenarios"] == ["standard"] and seeders[0]["backends"] == ["api"]
    started = client.post("/api/backends/ensure", json={"definition": "api", "instance": "tree:x", "tree": str(tree)})
    assert started.status_code == 200, started.text
    backend_id = started.json()["id"]
    deadline = time.time() + 10
    while client.get(f"/api/backends/{backend_id}").json()["status"] != "running" and time.time() < deadline:
        time.sleep(0.1)
    seeded = client.post(f"/api/backends/{backend_id}/seed", json={"scenario": "standard"})
    assert seeded.status_code == 200, seeded.text
    assert seeded.json()["items"][0]["result"] == {"scenario": "standard"}


def test_real_manager_runs_a_plugin_backend(config, tmp_path: Path) -> None:
    from eks_harness.pools.backends import BackendManager
    from eks_harness.pools.base import PoolHost

    tree = make_tree(tmp_path)
    host = PoolHost(config, config.paths)
    manager = BackendManager(host)
    record = manager.ensure({"definition": "api", "instance": "tree:real", "tree": str(tree)})
    backend_id = record["id"]
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            record = manager.get(backend_id)
            if record["status"] in ("running", "failed"):
                break
            time.sleep(0.2)
        assert record["status"] == "running", record.get("error")
        port = record["ports"]["http"]
        assert record["exports"]["API_BASE_URL"] == f"http://127.0.0.1:{port}"
        assert httpx.get(f"http://127.0.0.1:{port}/", timeout=5).status_code == 200
    finally:
        manager.stop(backend_id, final=True, reason="test")
