from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from pydantic import JsonValue

from eks_harness.db import Database
from eks_harness.engine import cache
from eks_harness.engine.cache import (
    CAS_FOLDER,
    BlobRecord,
    CacheHit,
    CacheKey,
    CacheService,
    CacheStats,
    ContentResolver,
    EvictionReport,
    relink,
)
from eks_harness.engine.errors import ContentError, NodeFailure
from eks_harness.engine.executors import BlobRef
from eks_harness.ir.plan import Node
from eks_harness.ir.types import Artifact

DAY = 86400.0


class FileStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.rows: dict[str, Artifact] = {}
        self.paths: dict[str, Path] = {}
        self.count = 0
        self.ingested: list[str] = []

    def add(self, data: bytes, *, kind: str = "image", name: str = "shot.png", record_sha: bool = True) -> Artifact:
        self.count += 1
        artifact_id = f"ART{self.count}"
        path = self.root / f"{artifact_id}-{name}"
        path.write_bytes(data)
        sha = "sha256:" + hashlib.sha256(data).hexdigest() if record_sha else None
        row = Artifact(id=artifact_id, kind=kind, name=name, url=f"http://hub/a/{artifact_id}", size=len(data),
                       sha256=sha)
        self.rows[artifact_id] = row
        self.paths[artifact_id] = path
        return row

    def rewrite(self, artifact_id: str, data: bytes) -> Artifact:
        path = self.paths[artifact_id]
        path.unlink()
        path.write_bytes(data)
        row = self.rows[artifact_id].model_copy(
            update={"size": len(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest()})
        self.rows[artifact_id] = row
        return row

    def delete(self, artifact_id: str) -> None:
        del self.rows[artifact_id]
        self.paths.pop(artifact_id).unlink()

    def ingest(self, path: Path, *, project: str, session: str, kind: str | None, name: str | None, caption: str,
               tags: Sequence[str], meta: Mapping[str, JsonValue], actor: str | None, move: bool) -> Artifact:
        data = path.read_bytes()
        if move:
            path.unlink()
        row = self.add(data, kind=kind or "file", name=name or path.name)
        self.ingested.append(row.id)
        return row

    def get(self, artifact_id: str) -> Artifact | None:
        return self.rows.get(artifact_id)

    def file(self, artifact_id: str) -> Path | None:
        path = self.paths.get(artifact_id)
        return path if path is not None and path.exists() else None

    def scratch(self) -> Path:
        folder = self.root / f"scratch-{self.count}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder


@pytest.fixture
def store(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path / "store")


@pytest.fixture
def service(tmp_path: Path, db: Database) -> CacheService:
    return CacheService(tmp_path / "cache", db, max_bytes=10_000_000, max_age_days=30)


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def node(op: str = "overlay.annotate", node_hash: str = "sha256:" + "1" * 64) -> Node:
    return Node(id="n0000000002", op=op, out="artifact", hash=node_hash)


def ckey(name: str) -> CacheKey:
    return CacheKey(key=sha(name.encode()), material={"v": "eks-cache@1", "name": name})


def blob_of(store: FileStore, artifact: Artifact) -> BlobRef:
    path = store.file(artifact.id)
    assert path is not None and artifact.sha256 is not None and artifact.name is not None
    return BlobRef(path=path, sha256=artifact.sha256, size=artifact.size or 0, name=artifact.name,
                   kind=artifact.kind)


def value_for(artifact: Artifact) -> JsonValue:
    return {
        "image": artifact.model_dump(mode="json", exclude_none=True),
        "plain": artifact.id,
        "uri": f"artifact:{artifact.id}",
        "media": {"uri": f"artifact:{artifact.id}", "kind": "image", "artifact": artifact.id},
        "other": "unrelated",
    }


def materializer(store: FileStore, calls: list[BlobRecord]) -> Any:
    def materialize(blob: BlobRecord, path: Path) -> Artifact:
        calls.append(blob)
        copy = store.root / f"materialized-{len(calls)}"
        shutil.copyfile(path, copy)
        return store.ingest(copy, project="p", session="s", kind=blob.kind, name=blob.name, caption="", tags=(),
                            meta={}, actor=None, move=True)

    return materialize


# store and lookup


def test_store_then_lookup_hits_with_a_hard_linked_blob(service: CacheService, store: FileStore,
                                                        tmp_path: Path) -> None:
    shot = store.add(b"annotated pixels")
    value = value_for(shot)
    entry = service.store(ckey("a"), node=node(), value=value, blobs=[blob_of(store, shot)],
                          resolution={"gpu": {"adapter": "m2"}}, run_id="run1", artifacts=[shot.id])
    assert entry.blobs == (BlobRecord(sha256=shot.sha256 or "", size=16, name="shot.png", kind="image",
                                      artifact=shot.id),)
    assert entry.size == len(cache._compact(value).encode()) + 16
    hexdigest = (shot.sha256 or "").removeprefix("sha256:")
    path = tmp_path / "cache" / CAS_FOLDER / hexdigest[:2] / hexdigest
    assert service.blob_path(shot.sha256 or "") == path
    assert service.blob_path(hexdigest) == path
    assert path.read_bytes() == b"annotated pixels"
    original = store.file(shot.id)
    assert original is not None and path.stat().st_ino == original.stat().st_ino
    calls: list[BlobRecord] = []
    hit = service.lookup(ckey("a"), artifacts=store, materialize=materializer(store, calls))
    assert hit is not None
    assert hit.value == value
    assert hit.artifacts == (shot.id,)
    assert not hit.rematerialized
    assert calls == []
    assert hit.entry.hits == 1
    assert hit.entry.resolution == {"gpu": {"adapter": "m2"}}
    assert hit.entry.run_id == "run1"
    assert service.lookup(ckey("missing"), artifacts=store, materialize=materializer(store, calls)) is None
    assert not list(path.parent.glob("*.tmp"))


def test_a_blob_is_copied_when_hard_links_fail(service: CacheService, store: FileStore,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    shot = store.add(b"copied pixels")

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", refuse)
    service.store(ckey("copy"), node=node(), value=value_for(shot), blobs=[blob_of(store, shot)], resolution=None,
                  run_id=None, artifacts=[shot.id])
    path = service.blob_path(shot.sha256 or "")
    original = store.file(shot.id)
    assert original is not None
    assert path.read_bytes() == b"copied pixels"
    assert path.stat().st_ino != original.stat().st_ino
    assert [item.name for item in path.parent.iterdir()] == [path.name]


def test_a_blob_whose_bytes_do_not_match_is_refused(service: CacheService, store: FileStore) -> None:
    shot = store.add(b"real pixels")
    lying = BlobRef(path=store.file(shot.id) or Path(), sha256=sha(b"other pixels"), size=11, name="shot.png")
    with pytest.raises(ValueError, match="is not cached"):
        service.store(ckey("bad"), node=node(), value=value_for(shot), blobs=[lying], resolution=None, run_id=None,
                      artifacts=[shot.id])
    assert service.entry(ckey("bad").key) is None
    assert service.stats() == CacheStats(entries=0, bytes=0, blobs=0, blob_bytes=0)


def test_store_needs_one_artifact_id_per_blob(service: CacheService, store: FileStore) -> None:
    shot = store.add(b"x")
    with pytest.raises(ValueError, match="artifact ids"):
        service.store(ckey("ids"), node=node(), value=None, blobs=[blob_of(store, shot)], resolution=None,
                      run_id=None)


def test_a_null_value_round_trips(service: CacheService, store: FileStore) -> None:
    service.store(ckey("none"), node=node("audio.mix"), value=None, blobs=[], resolution=None, run_id=None)
    hit = service.lookup(ckey("none"), artifacts=store, materialize=materializer(store, []))
    assert hit is not None and hit.value is None and hit.artifacts == ()


# rematerialization


def test_a_deleted_artifact_is_rematerialized_from_the_cas(service: CacheService, store: FileStore) -> None:
    shot = store.add(b"kept bytes")
    value = value_for(shot)
    service.store(ckey("re"), node=node(), value=value, blobs=[blob_of(store, shot)], resolution=None, run_id=None,
                  artifacts=[shot.id])
    store.delete(shot.id)
    calls: list[BlobRecord] = []
    hit = service.lookup(ckey("re"), artifacts=store, materialize=materializer(store, calls))
    assert hit is not None
    assert hit.rematerialized
    assert [blob.artifact for blob in calls] == [shot.id]
    (fresh_id,) = hit.artifacts
    fresh = store.get(fresh_id)
    assert fresh is not None and fresh.id != shot.id and fresh.sha256 == shot.sha256
    assert hit.value == {
        "image": fresh.model_dump(mode="json", by_alias=True, exclude_none=True),
        "plain": fresh.id,
        "uri": f"artifact:{fresh.id}",
        "media": {"uri": f"artifact:{fresh.id}", "kind": "image", "artifact": fresh.id},
        "other": "unrelated",
    }
    assert (store.file(fresh.id) or Path()).read_bytes() == b"kept bytes"


def test_an_artifact_changed_in_place_is_rematerialized(service: CacheService, store: FileStore) -> None:
    shot = store.add(b"version one")
    service.store(ckey("inplace"), node=node(), value=value_for(shot), blobs=[blob_of(store, shot)],
                  resolution=None, run_id=None, artifacts=[shot.id])
    copy = service.blob_path(shot.sha256 or "")
    store.rewrite(shot.id, b"version two")
    assert copy.read_bytes() == b"version one"
    hit = service.lookup(ckey("inplace"), artifacts=store, materialize=materializer(store, []))
    assert hit is not None and hit.rematerialized
    assert hit.artifacts != (shot.id,)
    assert (store.file(hit.artifacts[0]) or Path()).read_bytes() == b"version one"


def test_an_entry_without_artifact_or_cas_file_is_dropped(service: CacheService, store: FileStore) -> None:
    shot = store.add(b"lost bytes")
    service.store(ckey("lost"), node=node(), value=value_for(shot), blobs=[blob_of(store, shot)], resolution=None,
                  run_id=None, artifacts=[shot.id])
    store.delete(shot.id)
    service.blob_path(shot.sha256 or "").unlink()
    calls: list[BlobRecord] = []
    assert service.lookup(ckey("lost"), artifacts=store, materialize=materializer(store, calls)) is None
    assert calls == []
    assert service.entry(ckey("lost").key) is None


def test_a_missing_second_cas_file_drops_the_entry_before_materializing(service: CacheService,
                                                                        store: FileStore) -> None:
    first = store.add(b"first blob", name="a.png")
    second = store.add(b"second blob", name="b.png")
    service.store(ckey("pair"), node=node(), value=[value_for(first), value_for(second)],
                  blobs=[blob_of(store, first), blob_of(store, second)], resolution=None, run_id=None,
                  artifacts=[first.id, second.id])
    store.delete(first.id)
    store.delete(second.id)
    service.blob_path(second.sha256 or "").unlink()
    calls: list[BlobRecord] = []
    assert service.lookup(ckey("pair"), artifacts=store, materialize=materializer(store, calls)) is None
    assert calls == []
    assert store.ingested == []
    assert service.entry(ckey("pair").key) is None


def test_evict_waits_for_a_running_materialization(tmp_path: Path, db: Database, store: FileStore) -> None:
    service = CacheService(tmp_path / "cache", db, max_bytes=0, max_age_days=30)
    shot = store.add(b"slow bytes")
    service.store(ckey("slow"), node=node(), value=value_for(shot), blobs=[blob_of(store, shot)], resolution=None,
                  run_id=None, artifacts=[shot.id])
    store.delete(shot.id)
    entered = threading.Event()
    release = threading.Event()
    copy = materializer(store, [])
    evictions: list[EvictionReport] = []

    def materialize(blob: BlobRecord, path: Path) -> Artifact:
        entered.set()
        assert release.wait(5)
        assert path.is_file()
        return copy(blob, path)

    hits: list[CacheHit | None] = []

    def evict() -> None:
        evictions.append(service.evict())

    looker = threading.Thread(
        target=lambda: hits.append(service.lookup(ckey("slow"), artifacts=store, materialize=materialize)))
    looker.start()
    assert entered.wait(5)
    evicter = threading.Thread(target=evict)
    evicter.start()
    evicter.join(0.3)
    assert evicter.is_alive()
    release.set()
    looker.join(5)
    evicter.join(5)
    (hit,) = hits
    assert hit is not None and hit.rematerialized
    assert evictions == [EvictionReport(removed=1, freed=evictions[0].freed, blobs=1)]
    assert len(store.ingested) == 1


def test_a_legacy_row_without_a_digest_is_hashed_on_lookup(service: CacheService, store: FileStore) -> None:
    shot = store.add(b"legacy pixels")
    service.store(ckey("legacy"), node=node(), value=value_for(shot), blobs=[blob_of(store, shot)], resolution=None,
                  run_id=None, artifacts=[shot.id])
    store.rows[shot.id] = shot.model_copy(update={"sha256": "sha256:"})
    calls: list[BlobRecord] = []
    hit = service.lookup(ckey("legacy"), artifacts=store, materialize=materializer(store, calls))
    assert hit is not None and not hit.rematerialized and hit.artifacts == (shot.id,)
    assert calls == []
    (store.file(shot.id) or Path()).write_bytes(b"legacy pixels, edited")
    again = service.lookup(ckey("legacy"), artifacts=store, materialize=materializer(store, calls))
    assert again is not None and again.rematerialized and len(calls) == 1


def test_relink_rules() -> None:
    old = Artifact(id="OLD1", kind="image", name="a.png", url="http://hub/a/OLD1", sha256=sha(b"a"))
    new = Artifact(id="NEW1", kind="image", name="a.png", url="http://hub/a/NEW1", sha256=sha(b"a"))
    pinned = f"artifact:OLD1@{sha(b'a')}"
    value: JsonValue = {
        "dump": old.model_dump(mode="json"),
        "list": ["OLD1", "artifact:OLD1", pinned, "OLD2"],
        "media": {"uri": "artifact:OLD1", "kind": "video", "artifact": "OLD1", "width": 10},
        "notartifact": {"id": "OLD1", "extra": True},
    }
    assert relink(value, {"OLD1": new}) == {
        "dump": new.model_dump(mode="json", exclude_none=True),
        "list": ["NEW1", "artifact:NEW1", f"artifact:NEW1@{sha(b'a')}", "OLD2"],
        "media": {"uri": "artifact:NEW1", "kind": "video", "artifact": "NEW1", "width": 10},
        "notartifact": {"id": "NEW1", "extra": True},
    }
    assert relink(value, {}) == value


# replacement and hits


def test_store_replaces_an_older_row_and_lookups_count_hits(service: CacheService, store: FileStore) -> None:
    first = service.store(ckey("same"), node=node(), value={"n": 1}, blobs=[], resolution=None, run_id="r1")
    for _ in range(2):
        assert service.lookup(ckey("same"), artifacts=store, materialize=materializer(store, [])) is not None
    counted = service.entry(ckey("same").key)
    assert counted is not None and counted.hits == 2 and counted.last_used_at >= first.last_used_at
    second = service.store(ckey("same"), node=node(), value={"n": 2}, blobs=[], resolution=None, run_id="r2")
    stored = service.entry(ckey("same").key)
    assert stored is not None and stored == second
    assert stored.value == {"n": 2} and stored.run_id == "r2" and stored.hits == 0
    hit = service.lookup(ckey("same"), artifacts=store, materialize=materializer(store, []))
    assert hit is not None and hit.value == {"n": 2} and hit.entry.hits == 1
    after = service.entry(ckey("same").key)
    assert after is not None and after.hits == 1 and after.last_used_at >= second.last_used_at
    assert service.stats().entries == 1


# eviction


def set_used(db: Database, key: CacheKey, when: float) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE cache_entries SET last_used_at = ? WHERE key = ?", (when, key.key))


def test_evict_ages_out_then_trims_lru_then_drops_orphans(tmp_path: Path, db: Database, store: FileStore) -> None:
    service = CacheService(tmp_path / "cache", db, max_bytes=10_000_000, max_age_days=10)
    now = 1_000_000_000.0
    old, mid, new = store.add(b"o" * 100, name="o.png"), store.add(b"m" * 200, name="m.png"), store.add(
        b"n" * 300, name="n.png")
    for name, artifact in (("old", old), ("mid", mid), ("new", new)):
        service.store(ckey(name), node=node(), value={"a": artifact.id}, blobs=[blob_of(store, artifact)],
                      resolution=None, run_id=None, artifacts=[artifact.id])
    set_used(db, ckey("old"), now - 11 * DAY)
    set_used(db, ckey("mid"), now - 2 * DAY)
    set_used(db, ckey("new"), now - 1 * DAY)
    stray = service.blob_path("sha256:" + "e" * 64)
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"stray")
    before = service.stats()
    assert before.entries == 3 and before.blobs == 4 and before.blob_bytes == 605
    value_bytes = len(cache._compact({"a": old.id}).encode())
    assert before.bytes == 600 + 3 * value_bytes
    report = service.evict(now=now)
    assert report == EvictionReport(removed=1, freed=value_bytes + 100 + 5, blobs=2)
    assert service.entry(ckey("old").key) is None
    assert not stray.exists() and not service.blob_path(old.sha256 or "").exists()
    assert service.stats() == CacheStats(entries=2, bytes=500 + 2 * value_bytes, blobs=2, blob_bytes=500)
    service.max_bytes = 300 + value_bytes
    trimmed = service.evict(now=now)
    assert trimmed == EvictionReport(removed=1, freed=value_bytes + 200, blobs=1)
    assert service.entry(ckey("mid").key) is None and service.entry(ckey("new").key) is not None
    assert service.stats() == CacheStats(entries=1, bytes=300 + value_bytes, blobs=1, blob_bytes=300)
    assert service.evict(now=now) == EvictionReport(removed=0, freed=0, blobs=0)


def test_shared_blobs_stay_while_any_entry_needs_them(service: CacheService, store: FileStore, db: Database) -> None:
    shot = store.add(b"shared")
    for name in ("one", "two"):
        service.store(ckey(name), node=node(), value=None, blobs=[blob_of(store, shot)], resolution=None,
                      run_id=None, artifacts=[shot.id])
    set_used(db, ckey("one"), 0.0)
    report = service.evict(now=100 * DAY)
    assert report.removed == 1 and report.blobs == 0
    assert service.blob_path(shot.sha256 or "").exists()


# content resolution


@pytest.fixture
def resolver(tmp_path: Path, store: FileStore) -> ContentResolver:
    tree = tmp_path / "tree"
    (tree / "edits" / "assets").mkdir(parents=True)
    plugin = tmp_path / "plugins" / "acme"
    (plugin / "media").mkdir(parents=True)
    return ContentResolver(store, tree=tree, base="edits", plugin_roots={"acme.media": plugin},
                           scratch=tmp_path / "scratch")


def test_artifact_uris_use_the_stores_sha256(resolver: ContentResolver, store: FileStore) -> None:
    shot = store.add(b"stored")
    found = resolver.media(f"artifact:{shot.id}")
    assert (found.sha256, found.size, found.source, found.artifact) == (shot.sha256, 6, "artifact", shot.id)
    assert found.path == store.file(shot.id)
    assert resolver.path_of(f"artifact:{shot.id}") == store.file(shot.id)
    bare = store.add(b"hash me", record_sha=False)
    assert resolver.media(f"artifact:{bare.id}").sha256 == sha(b"hash me")
    with pytest.raises(ContentError) as missing:
        resolver.media("artifact:NOPE")
    assert missing.value.code == "content_unavailable"


def test_pinned_artifacts_are_verified(resolver: ContentResolver, store: FileStore) -> None:
    shot = store.add(b"pinned bytes")
    assert resolver.media(f"artifact:{shot.id}@{shot.sha256}").sha256 == shot.sha256
    wrong = "f" * 64
    with pytest.raises(ContentError) as mismatch:
        resolver.media(f"artifact:{shot.id}@sha256:{wrong}")
    assert mismatch.value.code == "content_mismatch"
    assert mismatch.value.message == (f"artifact {shot.id} now holds {shot.sha256}; the plan pins sha256:{wrong}. "
                                      "Restore that version or plan again with the new digest")


def test_file_uris_resolve_against_tree_and_base(resolver: ContentResolver, tmp_path: Path) -> None:
    relative = tmp_path / "tree" / "edits" / "assets" / "clip.mp4"
    relative.write_bytes(b"relative")
    found = resolver.media("file:assets/clip.mp4")
    assert (found.sha256, found.size, found.source, found.path) == (sha(b"relative"), 8, "file", relative)
    absolute = tmp_path / "elsewhere.bin"
    absolute.write_bytes(b"absolute")
    assert resolver.media(f"file:{absolute}").sha256 == sha(b"absolute")
    assert resolver.path_of(f"file:{absolute}") == absolute
    with pytest.raises(ContentError) as missing:
        resolver.media("file:assets/none.mp4")
    assert missing.value.code == "content_unavailable"
    folder = ContentResolver(resolver.artifacts, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "s")
    with pytest.raises(ContentError, match="needs the plan's tree"):
        folder.media("file:assets/clip.mp4")
    with pytest.raises(ContentError, match="not a regular file"):
        resolver.media(f"file:{tmp_path}")


def test_plugin_and_package_uris(resolver: ContentResolver, tmp_path: Path) -> None:
    logo = tmp_path / "plugins" / "acme" / "media" / "logo.png"
    logo.write_bytes(b"logo")
    found = resolver.media("plugin:acme.media/media/logo.png")
    assert (found.sha256, found.source, found.path) == (sha(b"logo"), "plugin", logo)
    with pytest.raises(ContentError, match="not loaded"):
        resolver.media("plugin:other/media/logo.png")
    with pytest.raises(ContentError, match="leaves"):
        resolver.media("plugin:acme.media/../../tree/x")
    import eks_harness.annotate

    fonts = Path(eks_harness.annotate.__file__).parent / "fonts" / "FONTS.md"
    packaged = resolver.media("pkg:eks_harness/annotate/fonts/FONTS.md")
    assert (packaged.sha256, packaged.source) == (sha(fonts.read_bytes()), "pkg")
    with pytest.raises(ContentError, match="not importable"):
        resolver.media("pkg:no_such_package_xyz/a.txt")
    with pytest.raises(ContentError):
        resolver.media("memory:thing")


def test_the_file_memo_follows_changes(resolver: ContentResolver, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "tree" / "edits" / "assets" / "loop.wav"
    path.write_bytes(b"first")
    calls: list[Path] = []
    real = cache._sha256_file

    def counting(target: Path) -> tuple[str, int]:
        calls.append(target)
        return real(target)

    monkeypatch.setattr(cache, "_sha256_file", counting)
    assert resolver.media("file:assets/loop.wav").sha256 == sha(b"first")
    assert resolver.media("file:assets/loop.wav").sha256 == sha(b"first")
    assert len(calls) == 1
    path.write_bytes(b"second version")
    assert resolver.media("file:assets/loop.wav").sha256 == sha(b"second version")
    assert len(calls) == 2


def test_eight_threads_hash_one_uri_once(resolver: ContentResolver, tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "tree" / "edits" / "assets" / "big.bin"
    path.write_bytes(os.urandom(1 << 20))
    calls: list[Path] = []
    real = cache._sha256_file
    gate = threading.Barrier(8)

    def counting(target: Path) -> tuple[str, int]:
        calls.append(target)
        return real(target)

    monkeypatch.setattr(cache, "_sha256_file", counting)
    results: list[str] = []

    def work() -> None:
        gate.wait()
        results.append(resolver.media("file:assets/big.bin").sha256)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8 and len(set(results)) == 1
    assert len(calls) == 1


class _Handler(BaseHTTPRequestHandler):
    bodies: ClassVar[dict[str, bytes]] = {}
    hits: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        self.hits.append(self.path)
        body = self.bodies.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        if not self.path.endswith("-chunked"):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.bodies = {"/clip.mp4": b"remote clip", "/large": b"x" * 4096, "/large-chunked": b"y" * 4096}
    _Handler.hits = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_uris_stream_into_scratch(resolver: ContentResolver, server: str, tmp_path: Path) -> None:
    found = resolver.media(f"{server}/clip.mp4")
    assert (found.sha256, found.size, found.source) == (sha(b"remote clip"), 11, "http")
    assert found.path is not None and found.path.parent == tmp_path / "scratch" and found.path.suffix == ".mp4"
    assert found.path.read_bytes() == b"remote clip"
    assert resolver.media(f"{server}/clip.mp4") == found
    assert resolver.path_of(f"{server}/clip.mp4") == found.path
    assert resolver.path_of(f"{server}/never") is None
    assert _Handler.hits == ["/clip.mp4"]
    with pytest.raises(ContentError, match="HTTP 404"):
        resolver.media(f"{server}/missing")


def test_http_uris_go_through_a_given_client(store: FileStore, server: str, tmp_path: Path) -> None:
    with httpx.Client() as client:
        content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "s", http=client)
        assert content.media(f"{server}/clip.mp4").sha256 == sha(b"remote clip")
        assert not client.is_closed


def test_http_downloads_respect_the_size_limit(resolver: ContentResolver, server: str, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "HTTP_LIMIT_BYTES", 1024)
    with pytest.raises(ContentError, match="larger than the 1024 byte limit") as declared:
        resolver.media(f"{server}/large")
    assert declared.value.code == "content_unavailable"
    with pytest.raises(ContentError, match="larger than the 1024 byte limit"):
        resolver.media(f"{server}/large-chunked")
    assert list((tmp_path / "scratch").iterdir()) == []
    unreachable = "http://127.0.0.1:9/x"
    with pytest.raises(ContentError, match="download failed"):
        resolver.media(unreachable)


class _Trickle(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "100000")
        self.end_headers()
        try:
            for _index in range(1000):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def trickle() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Trickle)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/slow.bin"
    finally:
        httpd.shutdown()
        httpd.server_close()


def wait_empty(folder: Path) -> list[Path]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        left = list(folder.iterdir()) if folder.is_dir() else []
        if not left:
            return left
        time.sleep(0.05)
    return list(folder.iterdir())


def test_a_trickling_download_stops_at_the_total_deadline(resolver: ContentResolver, trickle: str, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "_HTTP_TIMEOUT_SECONDS", 0.5)
    started = time.monotonic()
    with pytest.raises(ContentError, match="took longer than 0.5 s") as slow:
        resolver.media(trickle)
    assert slow.value.code == "content_unavailable"
    assert time.monotonic() - started < 3
    started = time.monotonic()
    with pytest.raises(ContentError, match="took longer than 0.5 s"):
        resolver.media(trickle)
    assert time.monotonic() - started < 3
    assert wait_empty(tmp_path / "scratch") == []


def test_cancel_aborts_a_download_and_releases_the_uri(store: FileStore, trickle: str, tmp_path: Path) -> None:
    cancel = threading.Event()
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch",
                              cancel=cancel)
    failures: list[NodeFailure] = []

    def resolve() -> None:
        try:
            content.media(trickle)
        except NodeFailure as exc:
            failures.append(exc)

    workers = [threading.Thread(target=resolve) for _index in range(2)]
    for worker in workers:
        worker.start()
    time.sleep(0.3)
    cancel.set()
    started = time.monotonic()
    for worker in workers:
        worker.join(3)
    assert time.monotonic() - started < 3
    assert [failure.code for failure in failures] == ["cancelled", "cancelled"]
    assert wait_empty(tmp_path / "scratch") == []
