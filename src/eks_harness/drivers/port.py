from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue

from eks_harness.drivers.client import WorkerError
from eks_harness.drivers.devices import DeviceCapture
from eks_harness.drivers.native import NATIVE_OPS, NativeSurface, run_native
from eks_harness.drivers.procs import Owner, ProcRegistry
from eks_harness.drivers.profile import (
    AppProfile,
    ProfileError,
    load_profile,
    mobile_worker_config,
    node_modules_dir,
    pace_from_config,
    web_worker_config,
)
from eks_harness.drivers.runtime import (
    AppContext,
    AppRuntime,
    AppRuntimeError,
    PersonaLogin,
    call_hook,
    has_hook,
    load_runtime,
)
from eks_harness.drivers.wire import (
    ArtifactFile,
    WireError,
    duration_ms,
    to_wire,
    wire_schemas,
)
from eks_harness.drivers.workers import WorkerHandle, WorkerManager
from eks_harness.engine.drivers import (
    CallPhase,
    CallSite,
    DriverFile,
    DriverReply,
    SnapshotKind,
)
from eks_harness.engine.errors import DriverError, NodeFailure
from eks_harness.engine.model import BackendHandle, LeaseHandle, RunInfo, RunResources
from eks_harness.engine.resources import (
    CASSETTE_OP,
    CassetteHook,
    CassetteRequest,
    cassette_key,
)
from eks_harness.ir.plan import Plan, SessionSpec
from eks_harness.ir.registry import Registry
from eks_harness.media.overlays.boxmap import (
    RECORDING_SCHEMA,
    BoxMap,
    BoxMapError,
    from_bridge,
    from_native,
    image_size,
    normalize_recording,
    sidecar_path,
    write,
)
from eks_harness.paths import Paths

log = logging.getLogger("eks_harness.drivers.port")

PROVIDER = "eks.workers"
PLATFORMS = frozenset({"web", "ios", "android"})
WORKER_KINDS = {"web": "web", "ios": "mobile", "android": "mobile"}
LOGIN_OP = "session.as"
MOBILE = frozenset({"ios", "android"})
NATIVE_ALWAYS = frozenset({"ui.swipe", "ui.drag", "ui.deeplink", "ui.open_url"})
TARGET_FIELDS = ("target", "source", "to", "trigger")
NATIVE_TARGETS = frozenset({"point", "rect"})
BRIDGELESS_OPS = frozenset({"capture.shot", "capture.record", "capture.hold", "session.as"})
CALL_SLACK = 15.0
DEFAULT_CALL_TIMEOUT = 600.0
FILE_ROLES = ("value", "shot", "evidence")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

ExtensionsFor = Callable[[str, Path | None], list[dict[str, Any]]]


def worker_driver_problem(platform: str) -> str | None:
    if platform not in WORKER_KINDS:
        return f"no built-in worker drives {platform}"
    from eks_harness.drivers.workers import SCRIPTS, node_binary, workers_root

    try:
        node_binary()
    except WorkerError as error:
        return str(error)
    script = workers_root() / SCRIPTS[WORKER_KINDS[platform]]
    if not script.is_file():
        return f"the {platform} worker is missing at {script}"
    return None


def _video_size(path: Path) -> tuple[tuple[int, int], float | None]:
    from eks_harness.media.files import ffprobe

    data: Mapping[str, Any] = ffprobe(path)
    streams = cast(list[Mapping[str, Any]], data.get("streams") or [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"{path.name} has no video stream")
    found_format = cast(Mapping[str, Any], data.get("format") or {})
    duration: object = found_format.get("duration")
    return (int(video["width"]), int(video["height"])), float(str(duration)) if duration else None


def _safe(text: str) -> str:
    return _SAFE.sub("-", text).strip("-") or "x"


class _Worker:
    def __init__(self, port: WorkerDriverPort) -> None:
        self._port = port

    @property
    def url(self) -> str:
        return self._port.handle.base_url

    def call(self, op: str, args: Mapping[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        reply = self._port.raw(op, dict(args or {}), phase="run", timeout=timeout)
        return reply.value


class WorkerDriverPort:
    def __init__(self, *, platform: str, handle: WorkerHandle, manager: WorkerManager, lease: LeaseHandle,
                 session: SessionSpec | None, registry: Registry, artifact_file: ArtifactFile | None,
                 runtime: object | None, context: AppContext, scratch: Path, plan: Plan | None = None,
                 cassettes: CassetteHook | None = None, native: NativeSurface | None = None,
                 devices: DeviceCapture | None = None, bridge: bool = True) -> None:
        self._platform = platform
        self.bridge = bridge
        self.plan = plan
        self.cassettes = cassettes
        self.native = native
        self.devices = devices
        self._recording: dict[str, Any] | None = None
        self.handle = handle
        self.manager = manager
        self.lease = lease
        self.session = session
        self.registry = registry
        self.artifact_file = artifact_file
        self.runtime = runtime
        self.context = context
        self.scratch = scratch
        self._calls = 0
        self._version: str | None = None

    @property
    def platform(self) -> str:
        return self._platform

    @property
    def simulated(self) -> bool:
        return False

    def worker(self) -> _Worker:
        return _Worker(self)

    def call(self, op: str, args: Mapping[str, JsonValue], *, site: CallSite, phase: CallPhase = "run",
             timeout: float | None = None) -> DriverReply:
        if op == LOGIN_OP and has_hook(self.runtime, "login"):
            return self._login(args)
        spec = self.registry.op(op)
        try:
            wire = to_wire(spec, args, session=self.session, registry=self.registry,
                           artifact_file=self.artifact_file)
        except WireError as error:
            raise DriverError("args_invalid", f"{op}: {error}") from error
        except DriverError:
            raise
        except NodeFailure as failure:
            raise DriverError(failure.code, failure.message, detail=failure.detail,
                              retryable=failure.retryable) from failure
        if op == CASSETTE_OP and phase != "run":
            return self._cassette(wire, phase=phase, timeout=timeout, site=site)
        if self._platform in MOBILE:
            routed = self._mobile(op, wire, phase=phase, timeout=timeout, site=site)
            if routed is not None:
                return routed
        reply = self.raw(op, wire, phase=phase, timeout=timeout, site=site)
        if op == "capture.record" and phase == "exit":
            return self._normalize_recording_boxes(reply)
        return reply

    def _normalize_recording_boxes(self, reply: DriverReply) -> DriverReply:
        video = next((file for file in reply.files if file.role == "value" and file.kind == "video"), None)
        sidecar = next((file for file in reply.files if file.path.name.endswith(".boxes.json")), None)
        if video is None or sidecar is None:
            return reply
        try:
            raw: object = json.loads(sidecar.path.read_text(encoding="utf-8"))
            document = cast(Mapping[str, object], raw) if isinstance(raw, Mapping) else None
            if document is None:
                raise ValueError("the recording box maps are not a JSON object")
            trimmed = bool(document.get("trimmed"))
            size, duration = _video_size(video.path)
            boxes = normalize_recording(document, video_size=size, video_duration=duration, trimmed=trimmed,
                                        video=video.path.name)
            write(sidecar.path, boxes)
        except (OSError, ValueError, NodeFailure) as error:
            log.warning("box maps of recording %s dropped: %s", video.path.name, error)
            return dataclasses.replace(reply, files=tuple(file for file in reply.files if file is not sidecar))
        return reply

    def _boxes_file(self, image: Path, boxes: BoxMap, name: str) -> DriverFile:
        path = write(sidecar_path(image), boxes)
        return DriverFile(path=path, role="evidence", kind="file", name=path.name, caption=f"box map of {name}")

    def _shot_boxes(self, image: Path, bridge_boxes: Any, name: str) -> DriverFile | None:
        try:
            if bridge_boxes is not None:
                return self._boxes_file(image, from_bridge(bridge_boxes, image_size(image)), name)
            if not self.bridge and self.native is not None:
                return self._boxes_file(image, from_native(self.native.nodes(), image_size(image)), name)
        except (BoxMapError, DriverError, OSError) as error:
            log.warning("box map of %s dropped: %s", name, error)
        return None

    def _wants_native(self, op: str, wire: Mapping[str, Any]) -> bool:
        if op in NATIVE_ALWAYS:
            return True
        if not self.bridge:
            if op in NATIVE_OPS:
                return True
            if op not in BRIDGELESS_OPS:
                raise DriverError("op_unsupported", f"{op} needs the harness bridge, and the {self.lease.app} app has "
                                                    "none ([mobile] bridge = false)", detail={"op": op})
        for name in TARGET_FIELDS:
            target = wire.get(name)
            if isinstance(target, dict) and (target.get("surface") == "native" or target.get("by") in NATIVE_TARGETS):
                return True
        return False

    def _timeout_ms(self) -> int | None:
        timeout = self.session.timeout if self.session is not None else None
        return duration_ms(timeout, "timeout") if timeout is not None else None

    def _require_devices(self, op: str) -> tuple[DeviceCapture, str]:
        if self.devices is None or not self.lease.resource:
            raise DriverError("driver_error", f"{op} needs the device of lease {self.lease.sid}, which this hub has not "
                                              "given the driver")
        return self.devices, self.lease.resource

    def _mobile(self, op: str, wire: Mapping[str, Any], *, phase: CallPhase, timeout: float | None,
                site: CallSite) -> DriverReply | None:
        if self._wants_native(op, wire):
            if self.native is None:
                raise DriverError("native_unavailable", f"{op}: lease {self.lease.sid} has no native surface")
            native_wire = {**wire, "noise": list(self.session.noise)} if op == "check.checkpoint" and self.session else wire
            value = run_native(self.native, op, native_wire, self._timeout_ms())
            if op == "check.checkpoint" and wire.get("shot") is not False:
                return self._with_checkpoint_shot(DriverReply(value=value), wire, site)
            return DriverReply(value=value)
        if op == "capture.shot":
            return self._mobile_shot(wire, timeout=timeout, site=site)
        if op == "capture.record":
            return self._mobile_record(wire, phase=phase, timeout=timeout, site=site)
        if op == "capture.hold":
            if self._recording is not None:
                time.sleep(int(wire.get("duration") or 0) / 1000)
            return DriverReply(value=None)
        if op == "check.checkpoint":
            reply = self.raw(op, wire, phase=phase, timeout=timeout, site=site)
            if wire.get("shot") is False:
                return reply
            return self._with_checkpoint_shot(reply, wire, site)
        return None

    def _with_checkpoint_shot(self, reply: DriverReply, wire: Mapping[str, Any], site: CallSite) -> DriverReply:
        devices, resource = self._require_devices("check.checkpoint")
        label = str(wire.get("label") or "checkpoint")
        bridge_boxes: Any = None
        if self.bridge:
            try:
                bridge_boxes = self.raw("_capture.boxes", {}, phase="run", timeout=30, site=site).value
            except DriverError as error:
                log.warning("box map of checkpoint %s skipped: %s", label, error.message)
        path = devices.screenshot(resource, self._call_dir(site, "shot") / f"{_safe(label)}.png")
        shot = DriverFile(path=path, role="shot", kind="screenshot", name=label, caption=f"checkpoint {label}")
        boxes = self._shot_boxes(path, bridge_boxes, label)
        extra = (shot,) if boxes is None else (shot, boxes)
        return dataclasses.replace(reply, files=(*reply.files, *extra))

    def _call_dir(self, site: CallSite | None, phase: str) -> Path:
        self._calls += 1
        label = f"{site.node.id}-{site.instance or 'root'}-{site.attempt}" if site is not None else f"call{self._calls}"
        folder = self.scratch / "calls" / f"{self._calls:05d}-{_safe(label)}-{phase}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _mobile_shot(self, wire: Mapping[str, Any], *, timeout: float | None, site: CallSite) -> DriverReply:
        devices, resource = self._require_devices("capture.shot")
        if wire.get("element"):
            raise DriverError("target_unsupported", "capture.shot element crops are web only; shoot the screen on mobile")
        keys: list[Any] = []
        bridge_boxes: Any = None
        if self.bridge:
            prepared = self.raw("_capture.prepare", {"hide": wire.get("hide") or [], "focus": wire.get("focus"),
                                                     "boxes": True}, phase="run", timeout=timeout, site=site)
            found_keys = prepared.value.get("keys") if isinstance(prepared.value, dict) else None
            keys = list(found_keys) if isinstance(found_keys, list) else []
            bridge_boxes = prepared.value.get("boxes") if isinstance(prepared.value, dict) else None
        elif wire.get("hide"):
            raise DriverError("target_unsupported", "hiding elements needs the harness bridge; this app has none")
        name = str(wire.get("name") or "shot")
        try:
            path = devices.screenshot(resource, self._call_dir(site, "shot") / f"{_safe(name)}.png")
        finally:
            if keys:
                self.raw("_capture.restore", {"keys": keys}, phase="run", timeout=timeout, site=site)
        shot = DriverFile(path=path, role="value", kind="screenshot", name=name)
        boxes = self._shot_boxes(path, bridge_boxes, name)
        return DriverReply(files=(shot,) if boxes is None else (shot, boxes))

    def _mobile_record(self, wire: Mapping[str, Any], *, phase: CallPhase, timeout: float | None,
                       site: CallSite) -> DriverReply:
        devices, resource = self._require_devices("capture.record")
        name = str(wire.get("name") or "recording")
        if phase == "enter":
            if self._recording is not None:
                raise DriverError("driver_error", f"capture.record {name}: {self._recording['name']} is still recording")
            if not wire.get("from_here", True) and self.bridge:
                self.raw("ui.reload", {}, phase="run", timeout=timeout, site=site)
            folder = self._call_dir(site, "record")
            if self.bridge:
                self.raw("_recording", {"state": "start", "name": name, "pace": wire.get("pace"),
                                        "pointer": wire.get("pointer", True)}, phase="run", timeout=timeout, site=site)
            try:
                devices.record_start(resource, folder / "raw" / f"{_safe(name)}.mp4", self.lease.sid)
            except DriverError:
                if self.bridge:
                    self.raw("_recording", {"state": "stop"}, phase="run", timeout=timeout, site=site)
                raise
            self._recording = {"name": name, "folder": folder, "started": time.time()}
            return DriverReply(value=None)
        state = self._recording
        self._recording = None
        if state is None:
            raise DriverError("driver_error", f"capture.record {name}: the recording is not running")
        stopped = (self.raw("_recording", {"state": "stop"}, phase="run", timeout=timeout, site=site) if self.bridge
                   else DriverReply(value={}))
        log_value: Mapping[str, Any] = stopped.value if isinstance(stopped.value, dict) else {}
        found_steps = log_value.get("steps")
        steps: list[Any] = list(found_steps) if isinstance(found_steps, list) else []
        started_at = log_value.get("startedAt")
        offset = (float(started_at) / 1000 if isinstance(started_at, (int, float)) else state["started"]) - state["started"]
        events = [max(0.0, float(step["t"]) + offset) for step in steps
                  if isinstance(step, dict) and isinstance(step.get("t"), (int, float))]
        trim = wire.get("trim")
        if trim is None:
            trim = not log_value.get("annotations")
        folder: Path = state["folder"]
        video = devices.record_stop(resource, folder / f"{_safe(name)}.mp4", trim=bool(trim), events=events)
        sidecar = folder / f"{_safe(name)}.interactions.json"
        sidecar.write_text(json.dumps({"video": video.name, "steps": steps, "annotations": log_value.get("annotations")
                                       or []}, indent=2), encoding="utf-8")
        files = [
            DriverFile(path=video, role="value", kind="video", name=name),
            DriverFile(path=sidecar, role="evidence", kind="file", name=sidecar.name,
                       caption="interactions of the recording"),
        ]
        found_boxes = log_value.get("boxes")
        if isinstance(found_boxes, list) and found_boxes:
            boxes = self._mobile_recording_boxes(video, cast(list[Any], found_boxes), offset, events, bool(trim))
            if boxes is not None:
                files.append(boxes)
        return DriverReply(files=tuple(files))

    def _mobile_recording_boxes(self, video: Path, found: list[Any], offset: float, events: list[float],
                                trim: bool) -> DriverFile | None:
        steps: list[dict[str, Any]] = []
        for item in found:
            if not isinstance(item, dict):
                continue
            step = cast(dict[str, Any], item)
            moment: object = step.get("t")
            if isinstance(moment, (int, float)):
                steps.append({**step, "t": float(moment) + offset})
        raw: dict[str, Any] = {"schema": RECORDING_SCHEMA, "units": "bridge", "events": events, "steps": steps}
        try:
            size, duration = _video_size(video)
            boxes = normalize_recording(raw, video_size=size, video_duration=duration,
                                        trimmed=trim and video.stem.endswith("-trimmed"), video=video.name)
            path = write(sidecar_path(video), boxes)
        except (OSError, ValueError, NodeFailure) as error:
            log.warning("box maps of recording %s dropped: %s", video.name, error)
            return None
        return DriverFile(path=path, role="evidence", kind="file", name=path.name,
                          caption="box maps of the recording steps")

    def _cassette(self, wire: Mapping[str, Any], *, phase: CallPhase, timeout: float | None,
                  site: CallSite) -> DriverReply:
        hook = self.cassettes
        if hook is None or self.plan is None:
            raise DriverError("resource_error", f"{CASSETTE_OP} needs a cassette store on the hub; none is registered")
        backend = self.context.backend.identity if self.context.backend is not None else None
        request = CassetteRequest(run=self.context.run, node_id=site.node.id, name=str(wire.get("name") or ""),
                                  mode=wire.get("mode") or "auto", key=cassette_key(self.plan, site.node, backend),
                                  backend=backend)
        if phase == "enter":
            opened = dict(hook.opened(request))
            reply = self.raw(CASSETTE_OP, wire, phase=phase, timeout=timeout, site=site,
                             resources={"cassette": opened})
            return dataclasses.replace(reply, detail={**reply.detail, "cassette": opened})
        recorded: dict[str, JsonValue] = {"node": site.node.id, "instance": site.instance, "attempt": site.attempt}
        try:
            reply = self.raw(CASSETTE_OP, wire, phase=phase, timeout=timeout, site=site)
            found = reply.detail.get("cassette")
            if isinstance(found, dict):
                recorded.update(found)
        except DriverError as error:
            recorded["error"] = error.message
            hook.closed(request, recorded)
            raise
        closed = hook.closed(request, recorded)
        return dataclasses.replace(reply, detail={**reply.detail, "cassette": {**recorded, "artifact": closed}})

    def raw(self, op: str, wire: Mapping[str, Any], *, phase: CallPhase, timeout: float | None,
            site: CallSite | None = None, resources: Mapping[str, Any] | None = None) -> DriverReply:
        self._calls += 1
        label = f"{site.node.id}-{site.instance or 'root'}-{site.attempt}" if site is not None else f"call{self._calls}"
        out_dir = self.scratch / "calls" / f"{self._calls:05d}-{_safe(label)}-{phase}"
        budget = timeout if timeout is not None else DEFAULT_CALL_TIMEOUT
        body: dict[str, Any] = {
            "op": op,
            "phase": phase,
            "args": dict(wire),
            "site": ({"node": site.node.id, "instance": site.instance, "attempt": site.attempt,
                      "src": site.node.src, "label": site.node.label} if site is not None else {}),
            "session": self._session_wire(),
            "resources": dict(resources or {}),
            "deadlineMs": int((time.time() + budget) * 1000),
            "outDir": str(out_dir),
        }
        try:
            status, reply = self.handle.client().post("/op", body, timeout=budget + CALL_SLACK)
        except (OSError, WorkerError) as error:
            raise DriverError("driver_error", f"{op}: the {self._platform} worker did not answer: {error}",
                              retryable=True) from error
        if status != 200 or not isinstance(reply, dict):
            raise DriverError("driver_error", f"{op}: the {self._platform} worker answered HTTP {status}: "
                                              f"{json.dumps(reply)[:400]}")
        if not reply.get("ok"):
            raise self._failure(op, reply.get("error") or {})
        return DriverReply(value=reply.get("value"), files=tuple(self._files(op, reply.get("files") or [])),
                           detail=dict(reply.get("detail") or {}))

    def _session_wire(self) -> dict[str, Any]:
        session = self.session
        timeout = session.timeout if session is not None else None
        return {"key": self.lease.session_key, "platform": self._platform, "app": self.lease.app,
                "persona": session.persona if session is not None else None,
                "viewport": session.viewport if session is not None else None,
                "noise": list(session.noise) if session is not None else [],
                "timeoutMs": duration_ms(timeout, "timeout") if timeout is not None else None}

    def _failure(self, op: str, error: Mapping[str, Any]) -> DriverError:
        code = str(error.get("code") or "driver_error")
        if op == LOGIN_OP and code == "op_unsupported":
            code = "login_unsupported"
        message = str(error.get("message") or f"{op} failed")
        detail = error.get("detail")
        return DriverError(code, message, detail=dict(detail) if isinstance(detail, dict) else {},
                           retryable=bool(error.get("retryable")))

    def _files(self, op: str, files: Sequence[Any]) -> list[DriverFile]:
        found: list[DriverFile] = []
        for item in files:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            role = item.get("role") or "evidence"
            if role not in FILE_ROLES:
                raise DriverError("driver_error", f"{op}: the worker returned a file with unknown role {role!r}")
            found.append(DriverFile(path=Path(str(item["path"])), role=role, kind=item.get("kind"),
                                    name=item.get("name"), caption=str(item.get("caption") or "")))
        return found

    def _login(self, args: Mapping[str, JsonValue]) -> DriverReply:
        persona = str(args.get("persona") or "")
        headers = args.get("headers")
        credentials = self.context.persona(persona)
        login = PersonaLogin(persona=persona,
                             headers={str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict)
                             else {}, credentials=credentials)
        try:
            call_hook(self.runtime, "login", self.context, self.worker(), login)
        except DriverError:
            raise
        except Exception as error:
            raise DriverError("driver_error", f"the {self.lease.app} login as {persona} failed: {error}") from error
        return DriverReply(value=None)

    def snapshot(self, kinds: Sequence[SnapshotKind], dest: Path) -> list[DriverFile]:
        files: list[DriverFile] = []
        if self._platform in MOBILE and "screen" in kinds and self.devices is not None and self.lease.resource:
            try:
                dest.mkdir(parents=True, exist_ok=True)
                path = self.devices.screenshot(self.lease.resource, dest / f"screen-{int(time.time() * 1000)}.png")
                files.append(DriverFile(path=path, role="evidence", kind="screenshot", name=path.name))
            except DriverError as error:
                log.warning("screen snapshot of lease %s failed: %s", self.lease.sid, error.message)
            kinds = [kind for kind in kinds if kind != "screen"]
            if not kinds:
                return files
        try:
            status, reply = self.handle.client().post("/snapshot", {"kinds": list(kinds), "outDir": str(dest),
                                                                    "platform": self._platform}, timeout=60)
        except (OSError, WorkerError) as error:
            log.warning("snapshot of lease %s failed: %s", self.lease.sid, error)
            return files
        if status != 200 or not isinstance(reply, dict):
            return files
        return [*files, *self._files("snapshot", reply.get("files") or [])]

    def version(self) -> str:
        if self._version is None:
            try:
                status, health = self.handle.client().get("/health", timeout=5)
            except (OSError, WorkerError):
                status, health = 0, {}
            reported = health.get("version") if status == 200 and isinstance(health, dict) else None
            self._version = str(reported or self.handle.code)
        return self._version

    def close(self) -> None:
        try:
            call_hook(self.runtime, "close", self.context)
        finally:
            self.manager.stop(self.handle.kind, self.handle.key)


class WorkerDriverProvider:
    def __init__(self, *, paths: Paths, config: Any, registry: Registry, resources: RunResources,
                 scratch: Path, artifact_file: ArtifactFile | None = None,
                 extensions: ExtensionsFor | None = None, hide: Sequence[str] = (),
                 platforms: frozenset[str] = PLATFORMS, owner: Owner | None = None, plan: Plan | None = None,
                 cassettes: CassetteHook | None = None, devices: DeviceCapture | None = None,
                 app_ids: Mapping[str, Sequence[str]] | None = None) -> None:
        self.plan = plan
        self.cassettes = cassettes
        self.devices = devices
        self.app_ids = dict(app_ids or {})
        self.paths = paths
        self.config = config
        self.registry = registry
        self.resources = resources
        self.scratch = scratch
        self.artifact_file = artifact_file
        self.extensions = extensions
        self.hide = list(hide)
        self._platforms = frozenset(platforms)
        self.owner = owner or Owner.current()

    @property
    def name(self) -> str:
        return PROVIDER

    @property
    def platforms(self) -> frozenset[str]:
        return self._platforms

    def open(self, lease: LeaseHandle, session: SessionSpec | None, run: RunInfo) -> WorkerDriverPort:
        platform = lease.platform
        tree = Path(run.tree) if run.tree else None
        profile = self._profile(lease, tree)
        runtime = self._runtime(lease)
        state = self.scratch / f"lease-{_safe(lease.sid)}"
        state.mkdir(parents=True, exist_ok=True)
        device = (dict(self.devices.device(lease.resource)) if platform in MOBILE and self.devices is not None
                  and lease.resource else {})
        context = AppContext(app=lease.app or profile.name, platform=platform, tree=tree, root=profile.root,
                             profile=profile.raw, lease=lease, session=session, run=run,
                             backend=self._backend(lease), state_dir=state,
                             procs=ProcRegistry.for_paths(self.paths), owner=self.owner, device=device,
                             native=self._native(lease) if platform in MOBILE else None)
        try:
            prepared = call_hook(runtime, "prepare", context)
        except AppRuntimeError as error:
            raise DriverError("driver_error", f"app {context.app}: {error}") from error
        except Exception as error:
            raise DriverError("driver_error", f"app {context.app} could not be prepared: {error}") from error
        app_runtime = prepared if isinstance(prepared, AppRuntime) else AppRuntime()
        worker_config = self._config(platform, lease, session, profile, app_runtime, context)
        manager = WorkerManager(self.paths, env=dict(app_runtime.env), owner=self.owner)
        try:
            handle = manager.ensure(WORKER_KINDS[platform], lease.sid, worker_config)
        except WorkerError as error:
            raise DriverError("driver_error", f"the {platform} worker for lease {lease.sid} did not start: "
                                              f"{error}") from error
        native = context.native
        if native is not None and session is not None and session.permissions in ("grant", "deny"):
            app_id = self._app_id(platform, profile)
            if app_id is None:
                manager.stop(handle.kind, handle.key)
                raise DriverError("driver_error", f"permissions={session.permissions} needs the app id: set [mobile] "
                                                  f"{'ios_bundle' if platform == 'ios' else 'android_package'} in the "
                                                  "app profile or the project's apps setting")
            native.set_permissions(session.permissions, app_id)
        port = WorkerDriverPort(platform=platform, handle=handle, manager=manager, lease=lease, session=session,
                                registry=self.registry, artifact_file=self.artifact_file, runtime=runtime,
                                context=context, scratch=state, plan=self.plan, cassettes=self.cassettes,
                                native=native, devices=self.devices,
                                bridge=profile.mobile.get("bridge", True) is not False)
        try:
            call_hook(runtime, "boot", context, port.worker())
        except DriverError:
            manager.stop(handle.kind, handle.key)
            raise
        except Exception as error:
            manager.stop(handle.kind, handle.key)
            raise DriverError("driver_error", f"app {context.app} could not boot: {error}") from error
        return port

    def _native(self, lease: LeaseHandle) -> NativeSurface | None:
        if self.devices is None or not lease.resource:
            return None
        device = self.devices.device(lease.resource)
        return NativeSurface(kind="ios" if lease.platform == "ios" else "android", udid=device.get("udid"),
                             serial=device.get("serial"))

    def _app_id(self, platform: str, profile: AppProfile) -> str | None:
        own = profile.mobile.get("ios_bundle" if platform == "ios" else "android_package")
        if own:
            return str(own)
        known = self.app_ids.get(platform) or ()
        return str(known[0]) if known else None

    def _profile(self, lease: LeaseHandle, tree: Path | None) -> AppProfile:
        if tree is None or not lease.app:
            raise DriverError("driver_error", f"lease {lease.sid} has no tree or app to load an app profile from")
        from eks.discovery import DiscoveryError, find_app

        try:
            info = find_app(tree, lease.app)
        except DiscoveryError as error:
            raise DriverError("driver_error", str(error)) from error
        if info is None:
            raise DriverError("driver_error", f"app {lease.app} has no .harness/app.toml in {tree}")
        try:
            return load_profile(info.root / ".harness")
        except ProfileError as error:
            raise DriverError("driver_error", str(error)) from error

    def _runtime(self, lease: LeaseHandle) -> object | None:
        try:
            return load_runtime(self.registry, lease.app)
        except AppRuntimeError as error:
            raise DriverError("driver_error", str(error)) from error

    def _backend(self, lease: LeaseHandle) -> BackendHandle | None:
        found = [handle for handle in self.resources.backends.values() if handle.session_key == lease.session_key]
        if not found and len(self.resources.backends) == 1:
            found = list(self.resources.backends.values())
        return found[0] if found else None

    def _config(self, platform: str, lease: LeaseHandle, session: SessionSpec | None, profile: AppProfile,
                runtime: AppRuntime, context: AppContext) -> dict[str, Any]:
        extensions = self.extensions(platform, context.tree) if self.extensions is not None else []
        pace = pace_from_config(self.config)
        if platform == "web":
            web = dict(profile.web)
            if runtime.url:
                web["url"] = runtime.url
            if runtime.api_url:
                web["api_url"] = runtime.api_url
            elif not web.get("api_url") and context.backend_url:
                web["api_url"] = context.backend_url
            try:
                config = web_worker_config(dataclasses.replace(profile, web=web), sid=lease.sid,
                                           extensions=extensions, pace=pace, hide=self.hide,
                                           playwright_dirs=[node_modules_dir(self.paths)])
            except ProfileError as error:
                raise DriverError("driver_error", str(error)) from error
        else:
            config = mobile_worker_config(profile, sids={platform: lease.sid}, extensions=extensions, pace=pace)
        if session is not None:
            if session.timezone:
                config["timezone"] = session.timezone
            if session.locale:
                config["locale"] = session.locale
            if session.now:
                config["now"] = session.now
            if session.permissions:
                config["permissions"] = session.permissions
            config["networkIgnore"] = [*(config.get("networkIgnore") or []), *session.network_ignore]
        for key, value in runtime.options.items():
            if key == "options" and isinstance(value, Mapping):
                config["options"] = {**(config.get("options") or {}), **dict(value)}
            else:
                config[key] = value
        schemas = wire_schemas(self.registry, platform)
        text = json.dumps(schemas, sort_keys=True)
        schema_file = context.state_dir / f"ops-{platform}.schema.json"
        schema_file.write_text(text, encoding="utf-8")
        config["opsSchemaFile"] = str(schema_file)
        config["opsSchemaHash"] = hashlib.sha256(text.encode()).hexdigest()[:16]
        config["platform"] = platform
        return config


__all__ = ["PLATFORMS", "PROVIDER", "WORKER_KINDS", "WorkerDriverPort", "WorkerDriverProvider", "worker_driver_problem"]
