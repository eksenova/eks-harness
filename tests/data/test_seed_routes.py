from __future__ import annotations

from fastapi.testclient import TestClient

from eks_harness.data.store import Manifest, SnapshotStore

KEY = "sha256:" + "ab" * 32


def test_seed_snapshots_are_listed_shown_without_secrets_and_dropped(client: TestClient, paths) -> None:
    SnapshotStore(paths.state_dir / "seeds").put(Manifest(
        key=KEY, seed="sample", seedHash="sha256:" + "0" * 64, definition="api", outputsDigest="sha256:1",
        outputs={"user:a": {"id": 1, "token": "secret-token"}}, personas={"a": {"token": "secret-token", "role": "x"}},
        snapshot="seed-abababababababab", created="2026-10-14T00:00:00+00:00", deterministic=True))
    listed = client.get("/api/seeds").json()["items"]
    assert [(item["seed"], item["personas"], item["deterministic"]) for item in listed] == [("sample", ["a"], True)]
    shown = client.get("/api/seeds/abab").json()
    assert shown["outputs"]["user:a"] == {"id": 1, "token": "<redacted>"}
    assert shown["personaDetail"]["a"] == {"token": "<redacted>", "role": "x"}
    assert "secret-token" not in client.get("/api/seeds/abab").text
    assert client.get("/api/seeds/ffff").status_code == 404
    dropped = client.delete("/api/seeds/abab").json()
    assert dropped["dropped"] == KEY and dropped["snapshotDroppedBy"] is None and "stay" in dropped["note"]
    assert client.get("/api/seeds").json()["items"] == []
