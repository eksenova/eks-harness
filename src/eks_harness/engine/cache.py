from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from eks_harness._version import build_info, package_dir
from eks_harness.db import Database
from eks_harness.engine.errors import ContentError, NodeFailure
from eks_harness.engine.executors import ArtifactStore, BlobRef, ExecutorSpec
from eks_harness.engine.model import RunClock
from eks_harness.ir.catalog.markers import MarkerBase, STTMarkers
from eks_harness.ir.contributions import (
    CHECK_MEDIA_OP,
    NO_SESSION,
    SessionScope,
    node_args,
    node_contributions,
    session_scope,
    walk_scoped,
)
from eks_harness.ir.hashing import (
    AUDIO_MIX_OP,
    BLENDER_RENDER_OP,
    CPU_DEVICE,
    CPU_ENGINE,
    EACH_OP,
    EDIT_RENDER_OP,
    GFX_RENDER_OP,
    MARKERS_OP,
    SCENE_RENDER_OP,
    SOURCE_OP,
    cache_inputs,
    canonical_dump,
    canonical_json,
    content_dependent,
    digest,
    machine_dependent,
    pure_closure,
)
from eks_harness.ir.ops.edit import SOFTWARE_ENCODERS
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.refs import ItemExpr, RefError, RefExpr, walk_exprs
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    PROJECT_PLUGIN,
    Registry,
    RegistryError,
    using_registry,
)
from eks_harness.ir.types import ARTIFACT_ID, Artifact
from eks_harness.plugins.trust import SKIP_DIRS, SKIP_SUFFIXES, content_hash

CACHE_KEY_VERSION = "eks-cache@1"
CAS_FOLDER = "engine-cas"
HTTP_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
ContentSource = Literal["artifact", "file", "plugin", "pkg", "http"]

_HTTP_TIMEOUT_SECONDS = 60.0
_POLL_SECONDS = 0.05
_CHUNK = 1 << 20
_BUNDLE_KIND = "bundle"
_LOCAL_PREFIXES = ("file:", "plugin:", "pkg:")
_SHA_PREFIX = "sha256:"
_HEX = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT = re.compile(rf"^artifact:({ARTIFACT_ID})(?:@sha256:([0-9a-f]{{64}}))?$")
_ARTIFACT_REF = re.compile(rf"^artifact:({ARTIFACT_ID})(@sha256:[0-9a-f]{{64}})?$")
_CONTENT_PREFIXES = ("artifact:", "file:", "plugin:", "pkg:", "http://", "https://")
_ARTIFACT_KEYS = frozenset(Artifact.model_fields)
_DAY_SECONDS = 86400.0
_INFERENCE = ("inference.device", "inference.model", "inference.weights", "inference.backend_version")
_DEVICE = ("device", "model", "weights", "backend_version")
_GPU = ("gpu.adapter", "gpu.driver")
_STT_FIELDS = ("model", "device", "compute_type", "backend", "version")
_BEAT_FIELDS = ("backend", "version")


class PluginDigests(Protocol):
    def plugin(self, plugin_id: str) -> str | None: ...

    def project(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class MediaContent:
    uri: str
    sha256: str
    size: int
    source: ContentSource
    path: Path | None = None
    artifact: str | None = None


def _prefixed(sha: str) -> str:
    text = sha.strip().lower()
    return text if text.startswith(_SHA_PREFIX) else _SHA_PREFIX + text


def _plain(sha: str) -> str:
    return sha.strip().lower().removeprefix(_SHA_PREFIX)


def _sha256_file(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            hasher.update(chunk)
            size += len(chunk)
    return _SHA_PREFIX + hasher.hexdigest(), size


def _unavailable(uri: str, cause: str) -> ContentError:
    return ContentError("content_unavailable", f"{uri}: {cause}", detail={"uri": uri, "cause": cause})


def _store_sha(row: Artifact) -> str | None:
    plain = _plain(row.sha256 or "")
    return _SHA_PREFIX + plain if _HEX.fullmatch(plain) else None


def _tree_signature(root: Path) -> str:
    hasher = hashlib.sha256()
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
        for name in sorted(files):
            if name.endswith(SKIP_SUFFIXES) or name == ".DS_Store":
                continue
            path = Path(current) / name
            try:
                info = path.stat()
            except OSError:
                hasher.update(f"{path}\0-\0".encode())
                continue
            hasher.update(f"{path}\0{info.st_size}\0{info.st_mtime_ns}\0".encode())
    return hasher.hexdigest()


class ContentResolver:
    def __init__(self, artifacts: ArtifactStore, *, tree: Path | None, base: str,
                 plugin_roots: Mapping[str, Path], scratch: Path, http: httpx.Client | None = None,
                 cancel: threading.Event | None = None) -> None:
        self.artifacts = artifacts
        self.tree = tree
        self.base = base
        self.plugin_roots = dict(plugin_roots)
        self.scratch = scratch
        self.http = http
        self.cancel = cancel
        self._lock = threading.Lock()
        self._memo: dict[str, MediaContent] = {}
        self._files: dict[tuple[str, int, int], str] = {}
        self._bundles: dict[tuple[str, str], str] = {}
        self._uri_locks: dict[str, threading.Lock] = {}

    def _uri_lock(self, uri: str) -> threading.Lock:
        with self._lock:
            return self._uri_locks.setdefault(uri, threading.Lock())

    def media(self, uri: str) -> MediaContent:
        with self._uri_lock(uri):
            return self._resolve(uri)

    def _bundle_digest(self, uri: str) -> str:
        with self._uri_lock(uri):
            path, _source = self._local_path(uri)
            self._hash_local(uri, path)
            root = path.parent
            key = (str(root), _tree_signature(root))
            with self._lock:
                known = self._bundles.get(key)
            if known is not None:
                return known
            found = content_hash(root)
            with self._lock:
                self._bundles[key] = found
            return found

    def path_of(self, uri: str) -> Path | None:
        found = _ARTIFACT.fullmatch(uri)
        if found is not None:
            return self.artifacts.file(found.group(1))
        if uri.startswith(("http://", "https://")):
            with self._lock:
                known = self._memo.get(uri)
            return known.path if known is not None else None
        path, _source = self._local_path(uri)
        return path

    def _resolve(self, uri: str) -> MediaContent:
        found = _ARTIFACT.fullmatch(uri)
        if found is not None:
            return self._artifact(uri, found.group(1), found.group(2))
        if uri.startswith(("http://", "https://")):
            return self._download(uri)
        path, source = self._local_path(uri)
        sha, size = self._hash_local(uri, path)
        return MediaContent(uri=uri, sha256=sha, size=size, source=source, path=path)

    def _artifact(self, uri: str, artifact_id: str, pinned: str | None) -> MediaContent:
        row = self.artifacts.get(artifact_id)
        if row is None:
            raise _unavailable(uri, f"artifact {artifact_id} does not exist in the store")
        path = self.artifacts.file(artifact_id)
        stored = _store_sha(row)
        if stored is not None:
            sha = stored
            size = row.size if row.size is not None else self._size_of(uri, path)
        else:
            if path is None:
                raise _unavailable(uri, f"artifact {artifact_id} has no stored file to hash")
            sha, size = self._hash_local(uri, path)
        if pinned is not None and _plain(sha) != pinned:
            raise ContentError(
                "content_mismatch",
                f"artifact {artifact_id} now holds {sha}; the plan pins sha256:{pinned}. Restore that version or plan "
                "again with the new digest",
                detail={"uri": uri, "artifact": artifact_id, "current": sha, "pinned": _SHA_PREFIX + pinned})
        return MediaContent(uri=uri, sha256=sha, size=size, source="artifact", path=path, artifact=artifact_id)

    def _size_of(self, uri: str, path: Path | None) -> int:
        if path is None:
            return 0
        try:
            return path.stat().st_size
        except OSError as exc:
            raise _unavailable(uri, f"{path} cannot be read ({exc.strerror or exc})") from exc

    def _local_path(self, uri: str) -> tuple[Path, ContentSource]:
        if uri.startswith("file:"):
            return self._file_path(uri, uri.removeprefix("file:")), "file"
        if uri.startswith("plugin:"):
            plugin_id, _slash, inner = uri.removeprefix("plugin:").partition("/")
            root = self.plugin_roots.get(plugin_id)
            if root is None:
                raise _unavailable(uri, f"plugin {plugin_id!r} is not loaded on this hub")
            return self._inside(uri, root, inner), "plugin"
        if uri.startswith("pkg:"):
            module, _slash, inner = uri.removeprefix("pkg:").partition("/")
            return self._inside(uri, self._package_dir(uri, module), inner), "pkg"
        raise _unavailable(uri, "not a media URI the engine can read (artifact:, file:, plugin:, pkg:, http(s):)")

    def _file_path(self, uri: str, text: str) -> Path:
        if not text:
            raise _unavailable(uri, "the file: URI names no path")
        path = Path(text)
        if path.is_absolute():
            return path
        if self.tree is None:
            raise _unavailable(uri, "a relative file: path needs the plan's tree, and this run has none")
        return self.tree / self.base / path

    def _inside(self, uri: str, root: Path, inner: str) -> Path:
        if not inner:
            raise _unavailable(uri, "the URI names no path inside its owner")
        path = root / PurePosixPath(inner)
        if not path.resolve().is_relative_to(root.resolve()):
            raise _unavailable(uri, f"the path leaves {root}")
        return path

    def _package_dir(self, uri: str, module: str) -> Path:
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError) as exc:
            raise _unavailable(uri, f"package {module!r} cannot be found ({exc})") from exc
        if spec is None:
            raise _unavailable(uri, f"package {module!r} is not importable on this hub")
        locations = list(spec.submodule_search_locations or ())
        if locations:
            return Path(locations[0])
        if spec.origin is None:
            raise _unavailable(uri, f"package {module!r} has no location on disk")
        return Path(spec.origin).parent

    def _hash_local(self, uri: str, path: Path) -> tuple[str, int]:
        try:
            info = path.stat()
        except OSError as exc:
            raise _unavailable(uri, f"{path} cannot be read ({exc.strerror or exc})") from exc
        if not stat.S_ISREG(info.st_mode):
            raise _unavailable(uri, f"{path} is not a regular file")
        key = (str(path), info.st_size, info.st_mtime_ns)
        with self._lock:
            known = self._files.get(key)
        if known is not None:
            return known, info.st_size
        try:
            sha, size = _sha256_file(path)
        except OSError as exc:
            raise _unavailable(uri, f"{path} cannot be read ({exc.strerror or exc})") from exc
        with self._lock:
            self._files[key] = sha
        return sha, size

    def _cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    def _cancel_failure(self, uri: str) -> NodeFailure:
        return NodeFailure("cancelled", f"{uri}: the run was cancelled while its content was being downloaded",
                           detail={"uri": uri})

    def _download(self, uri: str) -> MediaContent:
        with self._lock:
            known = self._memo.get(uri)
        if known is not None:
            return known
        if self._cancelled():
            raise self._cancel_failure(uri)
        self.scratch.mkdir(parents=True, exist_ok=True)
        suffix = PurePosixPath(urlsplit(uri).path).suffix[:16]
        target = self.scratch / f"http-{uuid.uuid4().hex}{suffix}"
        budget = _HTTP_TIMEOUT_SECONDS
        stop = threading.Event()
        done = threading.Event()
        guard = threading.Lock()
        outcome: list[tuple[str, int] | NodeFailure] = []
        finished: list[bool] = []

        def work() -> None:
            client = self.http if self.http is not None else httpx.Client(timeout=budget)
            try:
                outcome.append(self._stream(uri, client, target, HTTP_LIMIT_BYTES, stop, budget))
            except NodeFailure as exc:
                outcome.append(exc)
            finally:
                if client is not self.http:
                    client.close()
                with guard:
                    finished.append(True)
                    if stop.is_set() or not outcome or isinstance(outcome[0], NodeFailure):
                        target.unlink(missing_ok=True)
                done.set()

        def abandon(failure: NodeFailure) -> NodeFailure:
            with guard:
                stop.set()
                if finished:
                    target.unlink(missing_ok=True)
            return failure

        deadline = time.monotonic() + budget
        threading.Thread(target=work, name="eks-content-download", daemon=True).start()
        while not done.wait(_POLL_SECONDS):
            if self._cancelled():
                raise abandon(self._cancel_failure(uri))
            if time.monotonic() >= deadline:
                raise abandon(_unavailable(uri, f"the download took longer than {budget:g} s"))
        if not outcome:
            raise _unavailable(uri, "the download thread ended without a result")
        result = outcome[0]
        if isinstance(result, NodeFailure):
            raise result
        sha, size = result
        found = MediaContent(uri=uri, sha256=sha, size=size, source="http", path=target)
        with self._lock:
            self._memo[uri] = found
        return found

    def _stream(self, uri: str, client: httpx.Client, target: Path, limit: int, stop: threading.Event,
                budget: float) -> tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0
        try:
            with client.stream("GET", uri, timeout=budget, follow_redirects=True) as response:
                if response.status_code >= 400:
                    raise _unavailable(uri, f"the server answered HTTP {response.status_code}")
                declared = response.headers.get("content-length")
                if declared is not None and declared.isdigit() and int(declared) > limit:
                    raise _unavailable(uri, f"the body of {declared} bytes is larger than the {limit} byte limit")
                with target.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        if stop.is_set():
                            raise _unavailable(uri, "the download was abandoned")
                        size += len(chunk)
                        if size > limit:
                            raise _unavailable(uri, f"the body is larger than the {limit} byte limit")
                        hasher.update(chunk)
                        handle.write(chunk)
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as exc:
            raise _unavailable(uri, f"the download failed ({type(exc).__name__}: {exc})") from exc
        except OSError as exc:
            raise _unavailable(uri, f"the download could not be written to {target} ({exc})") from exc
        return _SHA_PREFIX + hasher.hexdigest(), size


@dataclass(frozen=True, slots=True)
class CacheKeyRequest:
    plan: Plan
    node: Node
    registry: Registry
    executor: ExecutorSpec
    inputs: Mapping[str, str]
    resolution: Mapping[str, JsonValue] | None
    plugins: PluginDigests
    clock: RunClock | None = None


@dataclass(frozen=True, slots=True)
class CacheKey:
    key: str
    material: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Bypass:
    reason: str


@dataclass(frozen=True, slots=True)
class BlobRecord:
    sha256: str
    size: int
    name: str
    kind: str | None
    artifact: str | None


@dataclass(frozen=True, slots=True)
class CacheEntry:
    key: str
    node_hash: str
    op: str
    value: JsonValue
    blobs: tuple[BlobRecord, ...]
    resolution: Mapping[str, JsonValue] | None
    size: int
    run_id: str | None
    created_at: float
    last_used_at: float
    hits: int


@dataclass(frozen=True, slots=True)
class CacheHit:
    entry: CacheEntry
    value: JsonValue
    artifacts: tuple[str, ...]
    rematerialized: bool


@dataclass(frozen=True, slots=True)
class EvictionReport:
    removed: int
    freed: int
    blobs: int


@dataclass(frozen=True, slots=True)
class CacheStats:
    entries: int
    bytes: int
    blobs: int
    blob_bytes: int


Materializer = Callable[[BlobRecord, Path], Artifact]

_DIGESTS: dict[str, str] = {}
_DIGEST_LOCK = threading.Lock()


def _once(name: str, compute: Callable[[], str]) -> str:
    with _DIGEST_LOCK:
        known = _DIGESTS.get(name)
        if known is None:
            known = compute()
            _DIGESTS[name] = known
        return known


def _harness_source() -> str:
    source = build_info().get("sourceHash")
    if isinstance(source, str) and source:
        return _prefixed(source)
    return content_hash(package_dir())


def _fonts_dir() -> Path:
    spec = importlib.util.find_spec("eks_harness.annotate")
    if spec is None or spec.origin is None:
        raise RuntimeError("the eks_harness.annotate package is missing, so the shipped fonts cannot be hashed")
    return Path(spec.origin).parent / "fonts"


def harness_digest() -> str:
    return _once("harness", _harness_source)


def fonts_digest() -> str:
    return _once("fonts", lambda: content_hash(_fonts_dir()))


def _unique(fields: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(fields))


def _validated(node: Node, registry: Registry) -> Any:
    with using_registry(registry):
        return node_args(node, registry)


def _marker_fields(node: Node, registry: Registry) -> list[str]:
    model = _validated(node, registry)
    fields: list[str] = []
    for source in cast("tuple[Any, ...]", getattr(model, "sources", ())):
        if not isinstance(source, MarkerBase) or source.pinned():
            continue
        names = _STT_FIELDS if isinstance(source, STTMarkers) else _BEAT_FIELDS
        fields.extend(f"markers.{source.name}.{name}" for name in names)
    return fields


def _raw(args: Mapping[str, Any], *names: str) -> object:
    value: object = args
    for name in names:
        if not isinstance(value, dict):
            return None
        value = cast("dict[str, Any]", value).get(name)
    return value


def _render_effects_device(node: Node, registry: Registry) -> bool:
    tracks = _raw(node.args, "timeline", "tracks")
    for track in cast("list[object]", tracks) if isinstance(tracks, list) else []:
        clips = _raw(cast("dict[str, Any]", track), "clips") if isinstance(track, dict) else None
        for clip in cast("list[object]", clips) if isinstance(clips, list) else []:
            effects = _raw(cast("dict[str, Any]", clip), "effects") if isinstance(clip, dict) else None
            for effect in cast("list[object]", effects) if isinstance(effects, list) else []:
                kind = cast("dict[str, Any]", effect).get("kind") if isinstance(effect, dict) else None
                if not isinstance(kind, str):
                    continue
                try:
                    if registry.catalog("effect", kind).device_dependent:
                        return True
                except RegistryError:
                    continue
    return False


def _mix_inferred(node: Node) -> bool:
    tracks = _raw(node.args, "mix", "tracks")
    if not isinstance(tracks, list):
        return False
    for track in cast("list[object]", tracks):
        if isinstance(track, dict):
            entry = cast("dict[str, Any]", track)
            if entry.get("stems") is not None or entry.get("denoise") is not None:
                return True
    return False


def required_resolution(node: Node, registry: Registry) -> tuple[str, ...]:
    fields: list[str] = []
    spec = registry.op(node.op) if registry.has_op(node.op) else None
    if node.op == MARKERS_OP:
        fields.extend(_marker_fields(node, registry))
    elif node.op == CHECK_MEDIA_OP:
        with using_registry(registry):
            if machine_dependent(node, registry):
                fields.extend(("engine", "version"))
    elif node.op == EDIT_RENDER_OP:
        if _raw(node.args, "timeline", "render", "encoder") not in SOFTWARE_ENCODERS:
            fields.extend(("encoder", "encoder_version"))
        if _render_effects_device(node, registry):
            fields.extend(_INFERENCE)
    elif node.op == GFX_RENDER_OP:
        if _raw(node.args, "cpu") is not True:
            fields.extend(_GPU)
    elif node.op == BLENDER_RENDER_OP:
        engine = _raw(node.args, "scene", "render", "engine")
        device = _raw(node.args, "scene", "render", "device")
        platform = _raw(node.args, "scene", "render", "platform")
        if engine == CPU_ENGINE and device == CPU_DEVICE:
            if not (isinstance(platform, str) and platform):
                fields.append("platform")
        else:
            fields.extend(_GPU)
    elif node.op == SCENE_RENDER_OP:
        fields.append("browser")
    elif node.op == AUDIO_MIX_OP and _mix_inferred(node):
        fields.extend(_INFERENCE)
    if spec is not None and spec.device_dependent:
        fields.extend(_DEVICE)
    return _unique(fields)


def _field_value(record: Mapping[str, JsonValue], field: str) -> JsonValue:
    if field in record:
        return record[field]
    value: JsonValue = cast("JsonValue", dict(record))
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _empty(value: JsonValue) -> bool:
    return value is None or value == "" or value == [] or value == {}


def resolution_problems(node: Node, registry: Registry, record: Mapping[str, JsonValue] | None) -> list[str]:
    required = required_resolution(node, registry)
    if record is None:
        return list(required)
    return [field for field in required if _empty(_field_value(record, field))]


def _value_digest(value: JsonValue) -> str:
    return digest(canonical_json(value))


def _artifact_digest(artifact_id: str, artifacts: ArtifactStore) -> str:
    uri = f"artifact:{artifact_id}"
    row = artifacts.get(artifact_id)
    if row is None:
        raise _unavailable(uri, f"artifact {artifact_id} no longer exists in the store")
    stored = _store_sha(row)
    if stored is not None:
        return stored
    path = artifacts.file(artifact_id)
    if path is None:
        raise _unavailable(uri, f"artifact {artifact_id} has no stored file to hash")
    try:
        sha, _size = _sha256_file(path)
    except OSError as exc:
        raise _unavailable(uri, f"{path} cannot be read ({exc.strerror or exc})") from exc
    return sha


def content_digest(value: JsonValue, out: str, artifacts: ArtifactStore, *,
                   content: ContentResolver | None = None) -> str:
    if out.startswith("list[") and out.endswith("]") and isinstance(value, list):
        inner = out[5:-1]
        if inner in ("artifact", "media") or inner.startswith("list["):
            return _value_digest([content_digest(item, inner, artifacts, content=content) for item in value])
        return _value_digest(value)
    if out == "artifact" and isinstance(value, dict):
        artifact_id = value.get("id")
        if isinstance(artifact_id, str) and artifact_id:
            return _artifact_digest(artifact_id, artifacts)
        return _value_digest(value)
    if out == "media" and isinstance(value, dict):
        artifact_id = value.get("artifact")
        if isinstance(artifact_id, str) and artifact_id:
            return _artifact_digest(artifact_id, artifacts)
        uri = value.get("uri")
        if isinstance(uri, str) and uri.startswith(_CONTENT_PREFIXES):
            found = _ARTIFACT.fullmatch(uri)
            if content is not None:
                return content.media(uri).sha256
            if found is not None and found.group(2) is None:
                return _artifact_digest(found.group(1), artifacts)
            raise _unavailable(uri, "hashing this media input needs a content resolver")
        return _value_digest(value)
    return _value_digest(value)


def _input_ids(plan: Plan, node: Node, registry: Registry) -> tuple[str, ...] | None:
    found = cache_inputs(plan, node, registry)
    if found is None:
        return None
    own = content_dependent(node)
    order = {candidate.id: index for index, (candidate, _ancestors) in enumerate(plan.walk())}
    ids = dict.fromkeys(node_id for node_id in found if node_id != node.id or own)
    ids.update(dict.fromkeys(node_id for node_id in pure_closure(plan, node, registry) if node_id != node.id))
    return tuple(sorted(ids, key=lambda node_id: order.get(node_id, len(order))))


def _nodes(plan: Plan) -> dict[str, Node]:
    found: dict[str, Node] = {}
    for candidate, _ancestors in plan.walk():
        found.setdefault(candidate.id, candidate)
    return found


def _source_uri(node: Node) -> str:
    uri = node.args.get("uri")
    if not isinstance(uri, str) or not uri:
        raise _unavailable(repr(uri), f"media.source {node.id} has no URI to hash")
    return uri


def _source_digest(content: ContentResolver, producer: Node) -> str:
    uri = _source_uri(producer)
    if producer.args.get("kind") == _BUNDLE_KIND and uri.startswith(_LOCAL_PREFIXES):
        return content._bundle_digest(uri)
    return content.media(uri).sha256


def _each_reads(plan: Plan, node: Node, registry: Registry, nodes: Mapping[str, Node]) -> dict[str, set[str]]:
    reads: dict[str, set[str]] = {}
    for node_id in pure_closure(plan, node, registry):
        current = node if node_id == node.id else nodes.get(node_id)
        if current is None:
            continue
        try:
            exprs = list(walk_exprs(current.args))
        except RefError:
            continue
        for expr in exprs:
            if isinstance(expr, ItemExpr):
                reads.setdefault(expr.item, set()).add("item")
            elif isinstance(expr, RefExpr):
                reads.setdefault(expr.ref, set()).add("value")
    return reads


def _each_digest(node: Node, each_id: str, reads: set[str], value_of: Callable[[str], JsonValue],
                 item_of: Callable[[str], JsonValue] | None) -> str:
    if "item" in reads and item_of is None:
        raise ValueError(f"node {node.id} ({node.op}) reads the current item of {EACH_OP} {each_id}; pass item_of so "
                         "every iteration is keyed by its own item")
    item = _value_digest(item_of(each_id)) if "item" in reads and item_of is not None else None
    if "value" not in reads:
        if item is None:
            raise ValueError(f"node {node.id} ({node.op}) is keyed by {EACH_OP} {each_id} but reads neither its "
                             "value nor its item")
        return item
    value = _value_digest(value_of(each_id))
    return value if item is None else _value_digest({"item": item, "value": value})


def gather_inputs(plan: Plan, node: Node, registry: Registry, *, value_of: Callable[[str], JsonValue],
                  content: ContentResolver, artifacts: ArtifactStore,
                  override: Callable[[str], str | None] | None = None,
                  item_of: Callable[[str], JsonValue] | None = None) -> dict[str, str]:
    ids = _input_ids(plan, node, registry)
    if ids is None:
        return {}
    nodes = _nodes(plan)
    each_reads: dict[str, set[str]] | None = None
    found: dict[str, str] = {}
    for producer_id in ids:
        replaced = override(producer_id) if override is not None else None
        if replaced is not None:
            found[producer_id] = replaced
            continue
        producer = node if producer_id == node.id else nodes.get(producer_id)
        if producer is None:
            raise ValueError(f"node {node.id} ({node.op}) reads {producer_id!r}, which is not a node of the plan")
        if producer.op == SOURCE_OP:
            found[producer_id] = _source_digest(content, producer)
        elif producer.op == EACH_OP:
            if each_reads is None:
                with using_registry(registry):
                    each_reads = _each_reads(plan, node, registry, nodes)
            found[producer_id] = _each_digest(node, producer_id, each_reads.get(producer_id, set()), value_of,
                                              item_of)
        else:
            found[producer_id] = content_digest(value_of(producer_id), producer.out, artifacts, content=content)
    return found


def _scopes(plan: Plan) -> dict[str, SessionScope]:
    found: dict[str, SessionScope] = {}
    for candidate, scope in walk_scoped(plan.nodes, session_scope(plan.session)):
        found.setdefault(candidate.id, scope)
    return found


def _plugin_ids(plan: Plan, node: Node, registry: Registry) -> list[str]:
    nodes = _nodes(plan)
    scopes = _scopes(plan)
    plugins: list[str] = []
    with using_registry(registry):
        for node_id in pure_closure(plan, node, registry):
            current = node if node_id == node.id else nodes[node_id]
            if not registry.has_op(current.op):
                raise ValueError(f"node {current.id} uses op {current.op!r}, which this registry does not hold; its "
                                 "plugin code is unknown")
            plugins.append(registry.op(current.op).provenance.plugin)
            found = node_contributions(current, registry, scope=scopes.get(current.id, NO_SESSION))
            if found is None:
                raise ValueError(f"node {current.id} ({current.op}) has args that do not validate; its plugin "
                                 "contributions are unknown")
            plugins.extend(item.plugin for item in found)
    return plugins


def plugin_material(plan: Plan, node: Node, registry: Registry, digests: PluginDigests) -> dict[str, JsonValue]:
    material: dict[str, JsonValue] = {}
    for plugin in _plugin_ids(plan, node, registry):
        if plugin == BUILTIN_PLUGIN or plugin in material:
            continue
        material[plugin] = digests.project() if plugin == PROJECT_PLUGIN else digests.plugin(plugin)
    return dict(sorted(material.items()))


def clock_material(plan: Plan, clock: RunClock | None) -> JsonValue:
    tempo = plan.clock.tempo
    if clock is None or tempo is None or not tempo.external:
        return None
    dumped = canonical_dump(clock)
    return {"tempo": dumped.get("tempo"), "cues": dumped.get("cues", {}), "markers": dumped.get("markers", {})}


def _executor_material(executor: ExecutorSpec) -> dict[str, JsonValue]:
    return {
        "name": executor.name,
        "version": executor.version,
        "runtime": executor.runtime_version() if executor.runtime_version is not None else None,
        "harness": harness_digest() if executor.provenance.plugin == BUILTIN_PLUGIN else None,
        "fonts": fonts_digest() if executor.fonts else None,
    }


def _json_copy(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return cast("dict[str, JsonValue]", json.loads(canonical_json(dict(value))))


def cache_key(request: CacheKeyRequest) -> CacheKey | Bypass:
    plan, node, registry = request.plan, request.node, request.registry
    if not node.hash:
        raise ValueError(f"node {node.id} ({node.op}) has no hash; hash the plan before keying its nodes")
    ids = _input_ids(plan, node, registry)
    if ids is None:
        return Bypass(f"{node.op} is not pure")
    with using_registry(registry):
        machine = machine_dependent(node, registry)
    if machine and request.executor.resolver is None:
        return Bypass(f"{node.op} is machine-dependent and its executor has no resolver")
    try:
        plugins = plugin_material(plan, node, registry, request.plugins)
    except ValueError as exc:
        return Bypass(str(exc))
    runner = request.executor.provenance.plugin
    if runner != BUILTIN_PLUGIN and runner not in plugins:
        plugins[runner] = request.plugins.project() if runner == PROJECT_PLUGIN else request.plugins.plugin(runner)
        plugins = dict(sorted(plugins.items()))
    for plugin, value in plugins.items():
        if value is None:
            return Bypass("project code without a tree" if plugin == PROJECT_PLUGIN
                          else f"plugin {plugin} is not loaded on this hub")
    missing = [node_id for node_id in ids if node_id not in request.inputs]
    extra = sorted(set(request.inputs) - set(ids))
    if missing or extra:
        raise ValueError(f"node {node.id} ({node.op}) is keyed by the content of {list(ids)}; the request lacks "
                         f"{missing} and has extra {extra}")
    resolution: JsonValue = None
    if machine:
        if request.resolution is None:
            raise ValueError(f"node {node.id} ({node.op}) is machine-dependent; resolve it before keying it")
        problems = resolution_problems(node, registry, request.resolution)
        if problems:
            raise ValueError(f"node {node.id} ({node.op}) resolution lacks {', '.join(problems)}")
        resolution = _json_copy(request.resolution)
    material: dict[str, JsonValue] = {
        "v": CACHE_KEY_VERSION,
        "node": node.hash,
        "op": node.op,
        "executor": _executor_material(request.executor),
        "inputs": {node_id: _prefixed(request.inputs[node_id]) for node_id in ids},
        "resolution": resolution,
        "plugins": cast("JsonValue", plugins),
        "clock": clock_material(plan, request.clock),
    }
    return CacheKey(key=digest(canonical_json(material)), material=material)


def _artifact_dump(artifact: Artifact) -> JsonValue:
    return cast("JsonValue", artifact.model_dump(mode="json", by_alias=True, exclude_none=True))


def relink(value: JsonValue, artifacts: Mapping[str, Artifact]) -> JsonValue:
    if not artifacts:
        return value
    if isinstance(value, str):
        if value in artifacts:
            return artifacts[value].id
        found = _ARTIFACT_REF.fullmatch(value)
        if found is not None and found.group(1) in artifacts:
            return f"artifact:{artifacts[found.group(1)].id}{found.group(2) or ''}"
        return value
    if isinstance(value, list):
        return [relink(item, artifacts) for item in value]
    if isinstance(value, dict):
        old = value.get("id")
        if isinstance(old, str) and old in artifacts and "kind" in value and set(value) <= _ARTIFACT_KEYS:
            return _artifact_dump(artifacts[old])
        return {key: relink(item, artifacts) for key, item in value.items()}
    return value


def _blob_json(blob: BlobRecord) -> dict[str, JsonValue]:
    return {"sha256": blob.sha256, "size": blob.size, "name": blob.name, "kind": blob.kind,
            "artifact": blob.artifact}


def _blob_of(data: object) -> BlobRecord:
    item = cast("dict[str, Any]", data)
    return BlobRecord(sha256=str(item["sha256"]), size=int(item["size"]), name=str(item["name"]),
                      kind=item.get("kind"), artifact=item.get("artifact"))


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class CacheService:
    def __init__(self, root: Path, db: Database, *, max_bytes: int, max_age_days: float) -> None:
        self.root = root
        self.db = db
        self.max_bytes = max_bytes
        self.max_age_days = max_age_days
        self._lock = threading.Lock()

    def entry(self, key: str) -> CacheEntry | None:
        row = self.db.execute("SELECT * FROM cache_entries WHERE key = ?", (key,)).fetchone()
        return self._entry(row) if row is not None else None

    def _entry(self, row: Any) -> CacheEntry:
        resolution = json.loads(row["resolution"]) if row["resolution"] is not None else None
        blobs = tuple(_blob_of(item) for item in json.loads(row["blobs"] or "[]"))
        return CacheEntry(key=row["key"], node_hash=row["node_hash"], op=row["op"], value=json.loads(row["value"]),
                          blobs=blobs, resolution=resolution, size=int(row["size"]), run_id=row["run_id"],
                          created_at=float(row["created_at"]), last_used_at=float(row["last_used_at"]),
                          hits=int(row["hits"]))

    def lookup(self, key: CacheKey, *, artifacts: ArtifactStore, materialize: Materializer) -> CacheHit | None:
        entry = self.entry(key.key)
        if entry is None:
            return None
        kept = self._kept(entry, artifacts)
        if all(found is not None for found in kept):
            return self._hit(entry, [found for found in kept if found is not None], {}, rematerialized=False)
        with self._lock:
            entry = self.entry(key.key)
            if entry is None:
                return None
            kept = self._kept(entry, artifacts)
            needed = [blob for blob, found in zip(entry.blobs, kept, strict=True) if found is None]
            if any(not self.blob_path(blob.sha256).is_file() for blob in needed):
                self._delete(entry.key)
                return None
            mapping: dict[str, Artifact] = {}
            ids: list[str] = []
            for blob, found in zip(entry.blobs, kept, strict=True):
                if found is not None:
                    ids.append(found)
                    continue
                fresh = materialize(blob, self.blob_path(blob.sha256))
                if blob.artifact:
                    mapping[blob.artifact] = fresh
                ids.append(fresh.id)
            return self._hit(entry, ids, mapping, rematerialized=True)

    def _kept(self, entry: CacheEntry, artifacts: ArtifactStore) -> list[str | None]:
        kept: list[str | None] = []
        for blob in entry.blobs:
            current = artifacts.get(blob.artifact) if blob.artifact else None
            if current is None:
                kept.append(None)
                continue
            try:
                held = _artifact_digest(current.id, artifacts)
            except ContentError:
                kept.append(None)
                continue
            kept.append(current.id if _plain(held) == _plain(blob.sha256) else None)
        return kept

    def _hit(self, entry: CacheEntry, ids: list[str], mapping: Mapping[str, Artifact], *,
             rematerialized: bool) -> CacheHit:
        used = time.time()
        with self.db.transaction() as conn:
            conn.execute("UPDATE cache_entries SET hits = hits + 1, last_used_at = ? WHERE key = ?", (used, entry.key))
        updated = dataclasses.replace(entry, hits=entry.hits + 1, last_used_at=used)
        return CacheHit(entry=updated, value=relink(entry.value, mapping), artifacts=tuple(ids),
                        rematerialized=rematerialized)

    def store(self, key: CacheKey, *, node: Node, value: JsonValue, blobs: Sequence[BlobRef],
              resolution: Mapping[str, JsonValue] | None, run_id: str | None,
              artifacts: Sequence[str] = ()) -> CacheEntry:
        if len(artifacts) != len(blobs):
            raise ValueError(f"cache entry of node {node.id}: {len(blobs)} blobs need {len(blobs)} artifact ids, got "
                             f"{len(artifacts)}")
        value_json = _compact(value)
        resolution_json = _compact(dict(resolution)) if resolution is not None else None
        material_json = _compact(dict(key.material))
        with self._lock:
            records = [self._write_blob(blob, artifact) for blob, artifact in zip(blobs, artifacts, strict=True)]
            size = len(value_json.encode("utf-8")) + sum(record.size for record in records)
            created = time.time()
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO cache_entries (key, node_hash, op, value, blobs, material, resolution, "
                    "size, run_id, created_at, last_used_at, hits) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (key.key, node.hash or "", node.op, value_json, _compact([_blob_json(r) for r in records]),
                     material_json, resolution_json, size, run_id, created, created))
        return CacheEntry(key=key.key, node_hash=node.hash or "", op=node.op, value=json.loads(value_json),
                          blobs=tuple(records), resolution=json.loads(resolution_json) if resolution_json else None,
                          size=size, run_id=run_id, created_at=created, last_used_at=created, hits=0)

    def _write_blob(self, blob: BlobRef, artifact: str) -> BlobRecord:
        expected = _plain(blob.sha256)
        target = self.blob_path(expected)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{expected}.{uuid.uuid4().hex}.tmp")
        try:
            try:
                os.link(blob.path, temporary)
            except OSError:
                shutil.copyfile(blob.path, temporary)
            actual, size = _sha256_file(temporary)
            if _plain(actual) != expected:
                raise ValueError(f"blob {blob.name} ({blob.path}) holds {actual}, not {_prefixed(blob.sha256)}; it "
                                 "is not cached")
            if target.is_file():
                temporary.unlink()
            else:
                os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return BlobRecord(sha256=_SHA_PREFIX + expected, size=size, name=blob.name, kind=blob.kind,
                          artifact=artifact)

    def blob_path(self, sha256: str) -> Path:
        hexdigest = _plain(sha256)
        if not _HEX.fullmatch(hexdigest):
            raise ValueError(f"{sha256!r} is not a sha256 digest")
        return self.root / CAS_FOLDER / hexdigest[:2] / hexdigest

    def _delete(self, key: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))

    def _cas_files(self) -> Iterator[Path]:
        folder = self.root / CAS_FOLDER
        if not folder.is_dir():
            return
        for shard in sorted(folder.iterdir()):
            if shard.is_dir():
                yield from sorted(path for path in shard.iterdir() if path.is_file())

    def evict(self, *, now: float | None = None) -> EvictionReport:
        moment = time.time() if now is None else now
        cutoff = moment - self.max_age_days * _DAY_SECONDS
        removed = 0
        freed = 0
        with self._lock:
            with self.db.transaction() as conn:
                stale = conn.execute("SELECT key, value FROM cache_entries WHERE last_used_at < ?",
                                     (cutoff,)).fetchall()
                for row in stale:
                    conn.execute("DELETE FROM cache_entries WHERE key = ?", (row["key"],))
                    removed += 1
                    freed += len(str(row["value"]).encode("utf-8"))
                total = int(conn.execute("SELECT COALESCE(SUM(size), 0) FROM cache_entries").fetchone()[0])
                if total > self.max_bytes:
                    rows = conn.execute("SELECT key, value, size FROM cache_entries "
                                        "ORDER BY last_used_at ASC, created_at ASC, key ASC").fetchall()
                    for row in rows:
                        if total <= self.max_bytes:
                            break
                        conn.execute("DELETE FROM cache_entries WHERE key = ?", (row["key"],))
                        total -= int(row["size"])
                        removed += 1
                        freed += len(str(row["value"]).encode("utf-8"))
                referenced = self._referenced(conn)
            orphans = 0
            for path in list(self._cas_files()):
                if path.name in referenced:
                    continue
                try:
                    size = path.stat().st_size
                    path.unlink()
                except FileNotFoundError:
                    continue
                orphans += 1
                freed += size
        return EvictionReport(removed=removed, freed=freed, blobs=orphans)

    def _referenced(self, conn: Any) -> set[str]:
        referenced: set[str] = set()
        for row in conn.execute("SELECT blobs FROM cache_entries").fetchall():
            for item in json.loads(row["blobs"] or "[]"):
                referenced.add(_plain(str(cast("dict[str, Any]", item)["sha256"])))
        return referenced

    def stats(self) -> CacheStats:
        row = self.db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cache_entries").fetchone()
        blobs = 0
        blob_bytes = 0
        for path in self._cas_files():
            try:
                blob_bytes += path.stat().st_size
            except FileNotFoundError:
                continue
            blobs += 1
        return CacheStats(entries=int(row[0]), bytes=int(row[1]), blobs=blobs, blob_bytes=blob_bytes)


__all__ = [
    "CACHE_KEY_VERSION",
    "CAS_FOLDER",
    "HTTP_LIMIT_BYTES",
    "BlobRecord",
    "Bypass",
    "CacheEntry",
    "CacheHit",
    "CacheKey",
    "CacheKeyRequest",
    "CacheService",
    "CacheStats",
    "ContentResolver",
    "ContentSource",
    "EvictionReport",
    "Materializer",
    "MediaContent",
    "PluginDigests",
    "cache_key",
    "clock_material",
    "content_digest",
    "fonts_digest",
    "gather_inputs",
    "harness_digest",
    "plugin_material",
    "relink",
    "required_resolution",
    "resolution_problems",
]
