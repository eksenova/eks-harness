from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eks_harness.data.context import (
    REDACTED,
    ResourceContext,
    SeedApi,
    SeedError,
    redact,
)
from eks_harness.data.ids import Ids, valid_iban, valid_tckn, valid_vkn
from eks_harness.data.store import Divergence, Manifest, SnapshotStore, snapshot_name
from eks_harness.plugins.sdk import BackendInfo
from eks_harness.pools.base import BackendError, PoolHost
from eks_harness.pools.fake import FakeBackendManager

NOW = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)


def test_ids_are_valid_and_repeat_for_the_same_seed() -> None:
    first, second = Ids(42, NOW), Ids(42, NOW)
    values = [first.tckn(), first.vkn(), first.iban(), first.ulid(), first.uuid(), first.phone(), first.email()]
    assert values == [second.tckn(), second.vkn(), second.iban(), second.ulid(), second.uuid(), second.phone(),
                      second.email()]
    tckn, vkn, iban, ulid, uuid, phone, email = values
    assert valid_tckn(tckn) and valid_vkn(vkn) and valid_iban(iban) and iban.startswith("TR")
    assert len(ulid) == 26 and len(uuid) == 36 and phone.startswith("+905") and email.endswith("@example.test")
    assert Ids(43, NOW).tckn() != Ids(42, NOW).tckn()
    assert valid_vkn("1234567891") is False
    with pytest.raises(ValueError):
        Ids(1, NOW).iban("12")


def test_known_numbers_validate() -> None:
    assert valid_tckn("10000000146")
    assert valid_iban("TR33 0006 1005 1978 6457 8413 26")
    assert not valid_iban("TR34 0006 1005 1978 6457 8413 26")


def test_redact_hides_secret_keys_deeply() -> None:
    assert redact({"token": "x", "nested": [{"password": "y", "name": "n"}]}) == {
        "token": REDACTED, "nested": [{"password": REDACTED, "name": "n"}]}


def manifest(seed: str, key: str, created: str) -> Manifest:
    return Manifest(key=key, seed=seed, seedHash="sha256:" + "a" * 64, definition="api", outputsDigest="sha256:x",
                    created=created)


def test_snapshot_store_keeps_manifests_private_and_finds_stale_ones(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    keys = [f"sha256:{digit * 64}" for digit in "123"]
    for index, key in enumerate(keys):
        path = store.put(manifest("s", key, f"2026-10-1{index}T00:00:00+00:00"))
        assert path.stat().st_mode & 0o777 == 0o600
    assert store.get("api", keys[0]) is not None
    assert store.find("2222") is not None and (found := store.find(keys[2])) is not None and found.key == keys[2]
    assert [m.key for m in store.stale("api", "s", 2, current=keys[2])] == [keys[0]]
    assert store.drop("api", keys[0]) and store.get("api", keys[0]) is None
    assert snapshot_name(keys[1]) == "seed-2222222222222222"
    with pytest.raises(ValueError):
        store.get("api", "nope")
    assert Divergence(paths=["a"]).paths == ["a"]


def test_resource_context_resolves_reference_fields_and_refuses_others() -> None:
    raw = {"payer": {"$ref": "n1", "path": []}, "name": "x"}
    ctx = ResourceContext(kind="payment", key=None, path="payment#0", phase="api", parent=None,
                          fields={"payer": {"token": "t"}, "name": "x"}, raw_fields=raw, api=None,
                          backend=BackendInfo(), now=NOW, seed=7, log=lambda message: None)
    assert ctx.resolve(raw["payer"]) == {"token": "t"}
    assert ctx.resolve("plain") == "plain"
    with pytest.raises(SeedError):
        ctx.resolve({"$ref": "n9", "path": []})
    with pytest.raises(SeedError, match="no url"):
        ctx.api  # noqa: B018
    with pytest.raises(SeedError):
        SeedApi("", auth=lambda persona: {})


@pytest.fixture
def fake_pool(paths, config, monkeypatch: pytest.MonkeyPatch):
    host = PoolHost(config, paths, fake=True)
    monkeypatch.setattr(host, "spawn", lambda fn, *args: fn(*args))
    manager = FakeBackendManager(host)
    metas = {
        "app": {"name": "app", "requires": [{"name": "db"}]},
        "db": {"name": "db", "identity": "host", "requires": [{"name": "disk"}], "idleTimeout": 5},
        "disk": {"name": "disk", "identity": "global"},
        "loop-a": {"name": "loop-a", "requires": ["loop-b"]},
        "loop-b": {"name": "loop-b", "requires": ["loop-a"]},
    }
    monkeypatch.setattr(manager, "definition_meta", lambda name, tree: metas.get(name, {}))
    return host, manager


def test_dependencies_chain_through_the_fake_pool(fake_pool, tmp_path: Path) -> None:
    _host, manager = fake_pool
    response = manager.acquire({"definition": "app", "tree": str(tmp_path), "holder": "t", "ttl": "5m"})
    app_id = response["backend"]["id"]
    record = manager.get(app_id)
    assert record["status"] == "running"
    db_id = record["dependencies"]["db"]["backend"]
    assert db_id.startswith("db@host:") and manager.get(db_id)["idleTimeout"] == 5
    assert manager.get(db_id)["dependencies"]["disk"]["backend"] == "disk@global"
    assert manager.dependents("disk@global") == [db_id]
    with pytest.raises(BackendError, match="dependency of"):
        manager.stop_checked(db_id, True, "t")
    manager.stop_checked("disk@global", True, "t", force=True)
    assert manager.get(app_id) is None and manager.get(db_id) is None and manager.get("disk@global") is None


def test_dependency_cycles_are_refused(fake_pool, tmp_path: Path) -> None:
    _host, manager = fake_pool
    with pytest.raises(BackendError, match="cycle: loop-a -> loop-b -> loop-a"):
        manager.acquire({"definition": "loop-a", "tree": str(tmp_path)})


def test_fake_seed_hooks_keep_snapshots_per_definition(fake_pool, tmp_path: Path) -> None:
    _host, manager = fake_pool
    backend_id = manager.acquire({"definition": "disk", "tree": str(tmp_path)})["backend"]["id"]
    assert manager.step(backend_id, "snapshot", name="s1")["digest"].startswith("sha256:")
    assert manager.step(backend_id, "restore", name="s1") == {}
    with pytest.raises(BackendError):
        manager.step(backend_id, "restore", name="s2")
    assert manager.step(backend_id, "migrations") == {"migrations": "fake"}
