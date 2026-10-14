from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from eks.workspace import seed_info, workspace_registry
from eks_harness.config import Config
from eks_harness.data.engine import SeedEngine
from eks_harness.data.store import SnapshotStore
from eks_harness.engine.auth import PersonaAuthRegistry
from eks_harness.engine.errors import ResourceError
from eks_harness.engine.model import BackendIdentity, RunInfo
from eks_harness.engine.resources import SeedRestoreMode, SeedRestoreRequest
from eks_harness.pools.backends import BackendManager, ProcessRegistry, repo_cache_root
from eks_harness.pools.base import BackendError, PoolHost

TREE = Path(__file__).resolve().parents[2] / "examples" / "reference"
API = "reference-api"
INFRA = "reference-infra"
SEED = "reference-sample"


@pytest.fixture
def pool(paths, config: Config, monkeypatch: pytest.MonkeyPatch):
    config.set_many({"backend.portRangeStart": 47000, "backend.portRangeEnd": 47999,
                     "backend.readyTimeoutSeconds": 60})
    host = PoolHost(config, paths)
    monkeypatch.setattr(host, "spawn", lambda fn, *args: fn(*args))
    backends = BackendManager(host)
    yield host, backends
    for record in list(host.backends.values()):
        backends.stop(record["id"], final=True, reason="test cleanup")
    registry = ProcessRegistry(repo_cache_root(paths))
    for entry in registry.all():
        registry.stop(entry)


def acquire_api(backends: BackendManager, **extra: Any) -> dict[str, Any]:
    response = backends.acquire({"definition": API, "tree": str(TREE), "kind": "agent", "holder": "test",
                                 "ttl": "10m", **extra})
    backend_id = response["backend"]["id"]
    assert backends.wait_ready(backend_id, 60) == "running", backends.get(backend_id)
    return response


def test_an_eks_backend_holds_its_dependency_for_its_whole_life(pool) -> None:
    _host, backends = pool
    first = acquire_api(backends)
    api_id = first["backend"]["id"]
    infra_id = f"{INFRA}@global"
    infra = backends.get(infra_id)
    assert infra is not None and infra["status"] == "running"
    (lease,) = infra["leases"].values()
    assert lease["kind"] == "dependency" and lease["owner"] == f"backend:{api_id}"
    assert backends.public(backends.get(api_id))["dependencies"] == {INFRA: infra_id}
    assert backends.public(infra)["dependents"] == [api_id]
    assert backends.context(backends.get(api_id))["dependencies"][INFRA]["url"].startswith("http://127.0.0.1:")

    with pytest.raises(BackendError, match="is a dependency of"):
        backends.stop_checked(infra_id, True, "test")

    acquire_api(backends, restart=True)
    assert backends.get(api_id)["generation"] == 2
    assert list(backends.get(infra_id)["leases"]) == [lease["id"]]
    assert backends.get(infra_id)["generation"] == 1

    backends.stop_with_leases(api_id, True, "test")
    infra = backends.get(infra_id)
    assert infra is not None and infra["leases"] == {}
    assert backends.public(infra)["stopsAt"] is not None
    assert infra["idleTimeout"] == 20


def test_force_stop_takes_the_dependents_down_first(pool) -> None:
    _host, backends = pool
    api_id = acquire_api(backends)["backend"]["id"]
    backends.stop_checked(f"{INFRA}@global", True, "test", force=True)
    assert backends.get(api_id) is None
    assert backends.get(f"{INFRA}@global") is None


def test_reconcile_recreates_a_missing_dependency_lease(pool) -> None:
    _host, backends = pool
    api_id = acquire_api(backends)["backend"]["id"]
    infra = backends.get(f"{INFRA}@global")
    backends.drop_leases(infra, list(infra["leases"]), "lost")
    backends.reconcile_dependencies()
    (lease,) = backends.get(f"{INFRA}@global")["leases"].values()
    assert lease["owner"] == f"backend:{api_id}"
    assert backends.get(api_id)["dependencies"][INFRA]["lease"] == lease["id"]


def engine_for(backends: BackendManager, paths, keep: int = 3) -> SeedEngine:
    ctx = cast(Any, SimpleNamespace(pools=SimpleNamespace(backends=backends), paths=paths, config={}))
    return SeedEngine(ctx, PersonaAuthRegistry(), store=SnapshotStore(paths.state_dir / "seeds"), keep=keep)


def token(personas: dict[str, Any], key: str) -> str:
    return str(cast(dict[str, Any], personas[key])["token"])


def request_for(backends: BackendManager, api_id: str,
                mode: SeedRestoreMode) -> tuple[SeedRestoreRequest, dict[str, Any]]:
    record = backends.get(api_id)
    assert record is not None
    secrets = backends.info(api_id, secrets=True)
    identity = BackendIdentity(definition=API, backend_id=api_id, lease_id="bl_test", source="plan",
                               plugin="reference", plugin_version="1.0.0", fingerprint=str(record.get("fingerprint")),
                               generation=int(record["generation"]))
    run = RunInfo(id=f"run-{mode}-{time.monotonic_ns()}", project="examples/reference", session="main",
                  tree=str(TREE), plan_hash="sha256:" + "0" * 64, plan_kind="flow", plan_name="check",
                  created_at=time.time())
    info = seed_info(SEED, registry=workspace_registry(TREE))
    assert info is not None
    request = SeedRestoreRequest(run=run, resource_id="r1", session_key="main", seed=SEED, seed_hash=info.hash,
                                 backend=identity, url=str(secrets["url"]), now=None, key="sha256:" + "1" * 64,
                                 mode=mode)
    return request, secrets


def test_the_sample_seed_builds_snapshots_and_restores_deterministically(pool, paths) -> None:
    _host, backends = pool
    api_id = acquire_api(backends)["backend"]["id"]
    engine = engine_for(backends, paths)
    registry = workspace_registry(TREE)

    request, secrets = request_for(backends, api_id, "restore")
    built = engine.restore(request, secrets, registry=registry)
    assert built.detail["built"] is True and built.restored
    assert sorted(built.personas) == ["acme-admin", "acme-clerk"]
    manifests = {m.seed: m for m in engine.store.all(API)}
    assert set(manifests) == {"reference-base", SEED}
    first = manifests[SEED]
    assert first.resources == 10 and first.digest and first.deterministic is None
    assert [entry.path for entry in first.replay] == ["account:acme/note#0"]

    request, secrets = request_for(backends, api_id, "rebuild")
    rebuilt = engine.restore(request, secrets, registry=registry)
    assert rebuilt.detail["deterministic"] is True
    second = engine.store.get(API, built.key)
    assert second is not None
    assert (second.digest, second.outputs_digest) == (first.digest, first.outputs_digest)

    url = str(secrets["url"])
    clerk = token(built.personas, "acme-clerk")
    httpx.post(f"{url}/api/payments", headers={"Authorization": f"Bearer {clerk}"},
               json={"accountId": 1, "amount": 1, "iban": "x", "reference": "y", "paidAt": "z"}).raise_for_status()

    request, secrets = request_for(backends, api_id, "restore")
    reused = engine.restore(request, secrets, registry=registry)
    assert reused.detail.get("reused") is True and not reused.restored

    request, secrets = request_for(backends, api_id, "fresh")
    fresh = engine.restore(request, secrets, registry=registry)
    assert fresh.restored and fresh.detail["built"] is False and "replayDiverged" not in fresh.detail
    admin = token(built.personas, "acme-admin")
    payments = httpx.get(f"{url}/api/payments", headers={"Authorization": f"Bearer {admin}"}).json()["items"]
    assert [item["amount"] for item in payments] == [1250, 4800, 990, 310]
    assert httpx.get(f"{url}/api/notes").json()["items"] == [{"id": 1, "account": 1, "text": "Kasa sayımı cuma günü"}]
    me = httpx.get(f"{url}/api/me", headers={"Authorization": f"Bearer {clerk}"}).json()
    assert me["role"] == "clerk" and me["account"]["tax_number"] == cast(dict[str, Any], first.outputs["account:acme"])["tax_number"]


def test_a_replaced_instance_restores_again(pool, paths) -> None:
    _host, backends = pool
    api_id = acquire_api(backends)["backend"]["id"]
    engine = engine_for(backends, paths)
    registry = workspace_registry(TREE)
    request, secrets = request_for(backends, api_id, "restore")
    engine.restore(request, secrets, registry=registry)
    acquire_api(backends, restart=True)
    request, secrets = request_for(backends, api_id, "restore")
    again = engine.restore(request, secrets, registry=registry)
    assert again.restored and again.detail["built"] is False


def test_an_unknown_seed_is_a_resource_error(pool, paths) -> None:
    _host, backends = pool
    api_id = acquire_api(backends)["backend"]["id"]
    engine = engine_for(backends, paths)
    request, secrets = request_for(backends, api_id, "restore")
    with pytest.raises(ResourceError, match="not registered"):
        engine.restore(request.model_copy(update={"seed": "nope"}), secrets, registry=workspace_registry(TREE))
