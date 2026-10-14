from __future__ import annotations

import functools
import http.server
import json
import shutil
import subprocess
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eks_harness.config import Config
from eks_harness.drivers.cassettes import FileCassetteHook
from eks_harness.drivers.port import WorkerDriverPort, WorkerDriverProvider
from eks_harness.drivers.procs import ProcRegistry
from eks_harness.drivers.runtime import AppContext
from eks_harness.drivers.workers import WorkerHandle, WorkerManager, workers_root
from eks_harness.engine.drivers import CallSite
from eks_harness.engine.errors import DriverError, NodeFailure
from eks_harness.engine.model import LeaseHandle, RunInfo, RunResources
from eks_harness.engine.resources import CassetteRequest
from eks_harness.ir.plan import CheckUse, Node, Plan, Resource, SessionSpec
from eks_harness.ir.registry import default_registry
from eks_harness.media.overlays.boxmap import BoxMap, RecordingBoxes, image_size
from eks_harness.media.overlays.targets import resolve_on_boxmap
from eks_harness.paths import Paths

PROJECT = "acme/port"
SESSION = "port-tests"
HASH = "sha256:" + "c" * 64
WEB_DRIVER = workers_root() / "web-driver"
PLAYWRIGHT = WEB_DRIVER / "node_modules" / "playwright"

PAGE = """<!doctype html><html><head><title>Port</title></head><body><main>
<h1>Faturalar</h1>
<button data-testid="save">Kaydet</button>
<button>Sil</button><button>Sil</button>
<input placeholder="Ad" id="name">
<div id="out"></div>
<script>
document.querySelector('[data-testid=save]').addEventListener('click', () => {
  document.getElementById('out').textContent = 'kaydedildi ' + document.getElementById('name').value
})
console.error('page error line')
fetch('/api.json').then((r) => r.json()).then((j) => { document.getElementById('out').dataset.api = String(j.value) })
</script>
</main></body></html>
"""


def node(op: str, node_id: str = "n0000000001") -> Node:
    return Node(id=node_id, op=op, args={}, out=default_registry().op(op).out, hash=HASH, uses=["rlease01"])


def site(op: str) -> CallSite:
    return CallSite(node(op))


def lease(app: str = "web-app") -> LeaseHandle:
    return LeaseHandle(resource_id="rlease01", kind="lease.web", platform="web", session_key="main", app=app,
                       sid="sid001", project=PROJECT, session=SESSION)


def run_info(tree: Path | None = None) -> RunInfo:
    return RunInfo(id="01JPORTTESTRUN000000000000", project=PROJECT, session=SESSION,
                   tree=str(tree) if tree else None, plan_hash=HASH, plan_kind="flow", plan_name="port",
                   created_at=time.time())


class StubWorker:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.replies: dict[str, dict[str, Any]] = {}
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                return None

            def _send(self, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._send({"kind": "web", "version": "stub-7", "platforms": ["web"], "ops": [], "pid": 1})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.bodies.append({"path": self.path, **body})
                if self.path == "/snapshot":
                    out = Path(body["outDir"])
                    out.mkdir(parents=True, exist_ok=True)
                    (out / "screen.png").write_bytes(b"png")
                    self._send({"files": [{"path": str(out / "screen.png"), "role": "evidence", "kind": "screenshot"}]})
                    return
                self._send(stub.replies.get(body["op"], {"ok": True, "value": None, "files": [], "detail": {}}))

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def stub() -> Iterator[StubWorker]:
    worker = StubWorker()
    yield worker
    worker.close()


def stub_port(stub: StubWorker, paths: Paths, tmp_path: Path, session: SessionSpec | None) -> WorkerDriverPort:
    state = tmp_path / "state"
    handle = WorkerHandle(kind="web", key="sid001", pid=0, port=stub.port, state_dir=state, log_file=state / "log",
                          code="code-1", config_hash="cfg", started_at=time.time())
    context = AppContext(app="web-app", platform="web", tree=None, root=None, profile={}, lease=lease(),
                         session=session, run=run_info(), backend=None, state_dir=state,
                         procs=ProcRegistry.for_paths(paths))
    return WorkerDriverPort(platform="web", handle=handle, manager=WorkerManager(paths), lease=lease(),
                            session=session, registry=default_registry(), artifact_file=None, runtime=None,
                            context=context, scratch=tmp_path / "scratch")


def test_the_port_sends_the_wire_form_and_maps_replies(stub: StubWorker, paths: Paths, tmp_path: Path) -> None:
    session = SessionSpec(app="web-app", platform="web", viewport="desktop", timeout="4s", noise=("hotjar",),
                          defaults=(CheckUse(kind="console_errors"),), checks=(CheckUse(kind="layout_spill"),))
    port = stub_port(stub, paths, tmp_path, session)
    shot = tmp_path / "home.png"
    shot.write_bytes(b"png")
    stub.replies["capture.shot"] = {"ok": True, "value": None, "detail": {"n": 1},
                                    "files": [{"path": str(shot), "role": "value", "kind": "screenshot", "name": "home"}]}
    reply = port.call("capture.shot", {"name": "home"}, site=site("capture.shot"))
    assert [(f.path, f.role, f.kind, f.name) for f in reply.files] == [(shot, "value", "screenshot", "home")]
    assert reply.detail == {"n": 1}
    port.call("ui.tap", {"target": {"by": "text", "args": {"text": "Kaydet"}}, "timeout": "1500ms"}, site=site("ui.tap"))
    sent = stub.bodies[-1]
    assert sent["path"] == "/op" and sent["op"] == "ui.tap" and sent["phase"] == "run"
    assert sent["args"]["timeout"] == 1500 and sent["args"]["target"]["by"] == "text"
    assert sent["session"] == {"key": "main", "platform": "web", "app": "web-app", "persona": None,
                               "viewport": "desktop", "noise": ["hotjar"], "timeoutMs": 4000}
    assert sent["site"]["node"] == "n0000000001" and Path(sent["outDir"]).parent == tmp_path / "scratch" / "calls"
    port.call("check.checkpoint", {"label": "home"}, site=site("check.checkpoint"))
    assert [use["kind"] for use in stub.bodies[-1]["args"]["checks"]] == ["console_errors", "layout_spill"]
    assert port.version() == "stub-7"
    files = port.snapshot(["screen"], tmp_path / "snap")
    assert [f.role for f in files] == ["evidence"]


def test_worker_errors_become_driver_errors(stub: StubWorker, paths: Paths, tmp_path: Path) -> None:
    port = stub_port(stub, paths, tmp_path, None)
    stub.replies["ui.tap"] = {"ok": False, "error": {"code": "target_ambiguous", "message": "2 match",
                                                    "detail": {"count": 2}, "retryable": False}}
    with pytest.raises(DriverError) as caught:
        port.call("ui.tap", {"target": {"by": "text", "args": {"text": "Sil"}}}, site=site("ui.tap"))
    assert (caught.value.code, str(caught.value.detail.get("count"))) == ("target_ambiguous", "2")
    stub.replies["session.as"] = {"ok": False, "error": {"code": "op_unsupported", "message": "no session.as"}}
    with pytest.raises(DriverError) as login:
        port.call("session.as", {"persona": "muhasebeci"}, site=site("session.as"))
    assert login.value.code == "login_unsupported"
    with pytest.raises(DriverError) as invalid:
        port.call("ui.tap", {"target": {"by": "text", "args": {"text": "x"}}, "timeout": "beat:2"},
                  site=site("ui.tap"))
    assert invalid.value.code == "args_invalid"


ROOTS: dict[str, Path] = {}


def site_server_root(url: str) -> Path:
    return ROOTS[url]


@pytest.fixture
def site_server(tmp_path: Path) -> Iterator[str]:
    root = tmp_path / "site"
    root.mkdir()
    (root / "page.html").write_text(PAGE, encoding="utf-8")
    (root / "api.json").write_text('{"value": 1}', encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *_: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    ROOTS[url] = root
    yield url
    server.shutdown()
    ROOTS.pop(url, None)


@pytest.mark.skipif(shutil.which("node") is None or not PLAYWRIGHT.is_dir(),
                    reason="node or the web driver's Playwright is not installed")
def test_the_real_web_worker_runs_ops_on_a_local_page(site_server: str, paths: Paths, config: Config,
                                                       tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    harness = tree / "web-app" / ".harness"
    harness.mkdir(parents=True)
    (harness / "app.toml").write_text(textwrap.dedent(f"""
        [app]
        name = "web-app"
        platform = "web"

        [web]
        url = "{site_server}"
        browser = "launch"
    """), encoding="utf-8")
    session = SessionSpec(app="web-app", platform="web", defaults=(CheckUse(kind="console_errors"),))
    plan = Plan(kind="flow", name="port", session=session, nodes=[node("net.cassette")], hash=HASH,
                resources=[Resource(id="rlease01", kind="lease.web", app="web-app", session="main")])
    cassettes = FileCassetteHook(tmp_path / "cassettes")
    provider = WorkerDriverProvider(paths=paths, config=config, registry=default_registry(),
                                    resources=RunResources(), scratch=tmp_path / "scratch", plan=plan,
                                    cassettes=cassettes)
    try:
        port = provider.open(lease(), session, run_info(tree))
    except DriverError as error:
        if "Executable doesn't exist" in str(error) or "playwright install" in str(error):
            pytest.skip("Playwright's Chromium is not downloaded")
        raise
    try:
        def call(op: str, args: dict[str, Any]) -> Any:
            return port.call(op, args, site=site(op), timeout=60)

        call("ui.open_url", {"url": "/page.html"})
        call("ui.fill", {"target": {"by": "placeholder", "args": {"text": "Ad"}}, "value": "Ayşe"})
        call("ui.tap", {"target": {"by": "test_id", "args": {"id": "save"}}})
        assert call("ui.text", {"target": {"by": "css", "args": {"selector": "#out"}}}).value == "kaydedildi Ayşe"
        with pytest.raises(DriverError) as ambiguous:
            call("ui.tap", {"target": {"by": "text", "args": {"text": "Sil"}}, "timeout": "500ms"})
        assert ambiguous.value.code == "target_ambiguous"
        with pytest.raises(DriverError) as missing:
            call("ui.tap", {"target": {"by": "text", "args": {"text": "Yok"}}, "timeout": "300ms"})
        assert missing.value.code == "target_not_found"
        with pytest.raises(DriverError) as failed:
            call("expect.target", {"target": {"by": "css", "args": {"selector": "#out"}},
                                   "assertion": {"kind": "text", "equals": "başka"}, "timeout": "300ms"})
        assert failed.value.code == "assertion"
        checkpoint = call("check.checkpoint", {"label": "after-save"})
        assert checkpoint.value["ok"] is False
        assert [(c["kind"], c["ok"]) for c in checkpoint.value["checks"]] == [("console_errors", False)]
        assert [f.role for f in checkpoint.files] == ["shot", "evidence"] and checkpoint.files[0].path.is_file()
        assert checkpoint.files[1].path.name == checkpoint.files[0].path.stem + ".boxes.json"
        shot = call("capture.shot", {"name": "page"})
        files = {f.role: f for f in shot.files}
        assert sorted((f.role, f.kind) for f in shot.files) == [("evidence", "file"), ("value", "screenshot")]
        assert files["value"].path.read_bytes()[:4] == b"\x89PNG"
        assert files["evidence"].path.name == "page.boxes.json"
        boxes = BoxMap.from_json(json.loads(files["evidence"].path.read_text(encoding="utf-8")))
        assert boxes.size == image_size(files["value"].path)
        save = resolve_on_boxmap({"by": "test_id", "args": {"id": "save"}}, boxes)
        assert save == resolve_on_boxmap({"by": "text", "args": {"text": "Kaydet"}}, boxes)
        assert resolve_on_boxmap({"by": "role", "args": {"role": "heading", "name": "Faturalar"}}, boxes)[2] > 0
        assert resolve_on_boxmap({"by": "placeholder", "args": {"text": "Ad"}}, boxes)[3] > 0
        with pytest.raises(NodeFailure) as twice:
            resolve_on_boxmap({"by": "text", "args": {"text": "Sil"}}, boxes)
        assert twice.value.code == "target_ambiguous"
        for trim in (False, True):
            record = {"name": f"tour-{trim}", "trim": trim, "pointer": False}
            port.call("capture.record", record, site=site("capture.record"), phase="enter", timeout=60)
            call("ui.tap", {"target": {"by": "test_id", "args": {"id": "save"}}})
            call("capture.hold", {"duration": "1500ms"})
            call("ui.tap", {"target": {"by": "test_id", "args": {"id": "save"}}})
            call("capture.hold", {"duration": "300ms"})
            video = port.call("capture.record", record, site=site("capture.record"), phase="exit", timeout=120)
            by_name = {f.path.name: f for f in video.files}
            movie = next(f for f in video.files if f.role == "value")
            sidecar = by_name[f"tour-{trim}.boxes.json"]
            assert sidecar.path.name == movie.path.stem + ".boxes.json"
            recording = RecordingBoxes.from_json(json.loads(sidecar.path.read_text(encoding="utf-8")))
            assert len(recording.steps) == 3 and [step.action for step in recording.steps] == [None, "ui.tap", "ui.tap"]
            assert recording.steps[0].boxes.size == image_size_of_video(movie.path)
            probed = float(ffprobe_duration(movie.path))
            assert all(0 <= step.t <= probed + 0.05 for step in recording.steps)
            last = recording.at(recording.steps[-1].t)
            assert last is not None and resolve_on_boxmap({"by": "test_id", "args": {"id": "save"}}, last)[2] > 0
        api = {"target": {"by": "css", "args": {"selector": "#out"}}, "name": "data-api"}
        cassette = {"name": "api", "mode": "auto"}
        entered = port.call("net.cassette", cassette, site=site("net.cassette"), phase="enter", timeout=60)
        assert entered.detail["cassette"]["mode"] == "record"
        call("ui.reload", {})
        call("ui.wait_for", {"target": {"by": "css", "args": {"selector": "#out[data-api]"}}, "state": "attached"})
        assert call("ui.attr", api).value == "1"
        closed = port.call("net.cassette", cassette, site=site("net.cassette"), phase="exit", timeout=60)
        assert closed.detail["cassette"]["mode"] == "record" and closed.detail["cassette"]["entries"] >= 1
        (Path(site_server_root(site_server)) / "api.json").write_text('{"value": 2}', encoding="utf-8")
        replay = port.call("net.cassette", cassette, site=site("net.cassette"), phase="enter", timeout=60)
        assert replay.detail["cassette"]["mode"] == "replay"
        call("ui.reload", {})
        call("ui.wait_for", {"target": {"by": "css", "args": {"selector": "#out[data-api]"}}, "state": "attached"})
        assert call("ui.attr", api).value == "1"
        port.call("net.cassette", cassette, site=site("net.cassette"), phase="exit", timeout=60)
        call("net.route", {"pattern": "/api.json", "action": {"kind": "patch", "path": "value", "value": 9}})
        call("ui.reload", {})
        call("ui.wait_for", {"target": {"by": "css", "args": {"selector": "#out[data-api]"}}, "state": "attached"})
        assert call("ui.attr", api).value == "9"
        assert port.version()
    finally:
        port.close()


def ffprobe_duration(path: Path) -> float:
    from eks_harness.media.files import ffprobe

    return float(ffprobe(path)["format"]["duration"])


def image_size_of_video(path: Path) -> tuple[int, int]:
    from eks_harness.media.files import ffprobe

    stream = next(item for item in ffprobe(path)["streams"] if item.get("codec_type") == "video")
    return int(stream["width"]), int(stream["height"])


def test_file_cassettes_resolve_auto_and_keep_metadata(tmp_path: Path) -> None:
    hook = FileCassetteHook(tmp_path)
    request = CassetteRequest(run=run_info(), node_id="n0000000001", name="api", mode="auto", key=HASH, backend=None)
    replay = request.model_copy(update={"mode": "replay"})
    assert hook.available(request) and not hook.available(replay)
    opened = hook.opened(request)
    assert opened["mode"] == "record" and Path(str(opened["dir"])).is_dir()
    (Path(str(opened["dir"])) / "cassette.json").write_text('{"entries": []}', encoding="utf-8")
    assert hook.available(replay) and hook.opened(request)["mode"] == "replay"
    assert hook.closed(request, {"entries": 0}) is None
    meta = json.loads((Path(str(opened["dir"])) / "meta.json").read_text(encoding="utf-8"))
    assert (meta["name"], meta["recorded"]) == ("api", {"entries": 0})


class FakeDevices:
    def __init__(self) -> None:
        self.shots: list[Path] = []
        self.recording: Path | None = None
        self.stopped: dict[str, Any] = {}

    def device(self, resource: str) -> dict[str, Any]:
        return {"kind": "ios", "udid": "UDID-1", "name": resource}

    def screenshot(self, resource: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x89PNG")
        self.shots.append(dest)
        return dest

    def record_start(self, resource: str, dest: Path, sid: str) -> None:
        self.recording = dest

    def record_stop(self, resource: str, out: Path, *, trim: bool, events: list[float]) -> Path:
        self.stopped = {"trim": trim, "events": events}
        out.write_bytes(b"mp4")
        return out


class NativeCalls:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any, timeout: float) -> tuple[int, str]:
        self.calls.append(list(argv))
        return 0, "[]" if "describe-all" in argv else ""


def mobile_port(stub: StubWorker, paths: Paths, tmp_path: Path, devices: FakeDevices, runner: NativeCalls) -> WorkerDriverPort:
    from eks_harness.drivers.native import NativeSurface

    state = tmp_path / "state"
    ios = LeaseHandle(resource_id="rlease01", kind="lease.ios", platform="ios", session_key="main", app="mobile-app",
                      sid="sid002", resource="ios:0", project=PROJECT, session=SESSION)
    session = SessionSpec(app="mobile-app", platform="ios")
    handle = WorkerHandle(kind="mobile", key="sid002", pid=0, port=stub.port, state_dir=state, log_file=state / "log",
                          code="code-1", config_hash="cfg", started_at=time.time())
    context = AppContext(app="mobile-app", platform="ios", tree=None, root=None, profile={}, lease=ios,
                         session=session, run=run_info(), backend=None, state_dir=state,
                         procs=ProcRegistry.for_paths(paths))
    return WorkerDriverPort(platform="ios", handle=handle, manager=WorkerManager(paths), lease=ios, session=session,
                            registry=default_registry(), artifact_file=None, runtime=None, context=context,
                            scratch=tmp_path / "scratch", devices=devices,
                            native=NativeSurface(kind="ios", udid="UDID-1", runner=runner))


def test_mobile_captures_use_the_device_and_native_targets_skip_the_worker(
        stub: StubWorker, paths: Paths, tmp_path: Path) -> None:
    devices, runner = FakeDevices(), NativeCalls()
    port = mobile_port(stub, paths, tmp_path, devices, runner)
    stub.replies["_capture.prepare"] = {"ok": True, "value": {"keys": ["hide-1"]}, "files": [], "detail": {}}
    shot = port.call("capture.shot", {"name": "home", "hide": [{"by": "test_id", "args": {"id": "balance"}}]},
                     site=site("capture.shot"))
    assert [(f.role, f.kind, f.name) for f in shot.files] == [("value", "screenshot", "home")]
    assert [body["op"] for body in stub.bodies] == ["_capture.prepare", "_capture.restore"]
    assert stub.bodies[0]["args"]["hide"][0]["by"] == "test_id"
    stub.replies["check.checkpoint"] = {"ok": True, "value": {"label": "home", "ok": True, "checks": [], "shots": []},
                                        "files": [], "detail": {}}
    checkpoint = port.call("check.checkpoint", {"label": "home"}, site=site("check.checkpoint"))
    assert [f.role for f in checkpoint.files] == ["shot"] and len(devices.shots) == 2
    port.call("ui.deeplink", {"url": "eksengold://invoice/new"}, site=site("ui.deeplink"))
    assert ["xcrun", "simctl", "openurl", "UDID-1", "eksengold://invoice/new"] in runner.calls
    before = len(stub.bodies)
    with pytest.raises(DriverError) as native_missing:
        port.call("ui.tap", {"target": {"by": "text", "args": {"text": "Allow"}, "surface": "native"}, "timeout": "200ms"},
                  site=site("ui.tap"))
    assert native_missing.value.code in ("native_unavailable", "target_not_found")
    assert len(stub.bodies) == before


def test_mobile_recordings_bracket_the_device_recorder(stub: StubWorker, paths: Paths, tmp_path: Path) -> None:
    devices = FakeDevices()
    port = mobile_port(stub, paths, tmp_path, devices, NativeCalls())
    started = time.time() * 1000
    stub.replies["_recording"] = {"ok": True, "value": {"startedAt": started}, "files": [], "detail": {}}
    port.call("capture.record", {"name": "tour"}, site=site("capture.record"), phase="enter")
    assert devices.recording is not None and stub.bodies[-1]["args"]["state"] == "start"
    port.call("capture.hold", {"duration": "50ms"}, site=site("capture.hold"))
    stub.replies["_recording"] = {"ok": True, "value": {"startedAt": started, "annotations": [],
                                                        "steps": [{"t": 0.5, "action": "press"}]}, "files": [], "detail": {}}
    closed = port.call("capture.record", {"name": "tour"}, site=site("capture.record"), phase="exit")
    assert [(f.role, f.kind) for f in closed.files] == [("value", "video"), ("evidence", "file")]
    assert devices.stopped["trim"] is True and len(devices.stopped["events"]) == 1
    with pytest.raises(DriverError):
        port.call("capture.record", {"name": "tour"}, site=site("capture.record"), phase="exit")


BRIDGE_BOXES = {"window": {"width": 100, "height": 50}, "pixelRatio": 2,
                "nodes": [{"testId": "save", "kind": "button", "role": "button", "text": "Kaydet",
                           "box": [10, 5, 30, 10], "parent": None}]}


class PixelDevices(FakeDevices):
    def screenshot(self, resource: str, dest: Path) -> Path:
        from PIL import Image

        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (200, 100), (255, 255, 255)).save(dest)
        self.shots.append(dest)
        return dest

    def record_stop(self, resource: str, out: Path, *, trim: bool, events: list[float]) -> Path:
        self.stopped = {"trim": trim, "events": events}
        ffmpeg = shutil.which("ffmpeg")
        assert ffmpeg is not None
        subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=200x100:r=10:d=2",
                        "-pix_fmt", "yuv420p", str(out)], check=True)
        return out


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_mobile_shots_and_recordings_save_box_maps_from_the_bridge(stub: StubWorker, paths: Paths,
                                                                   tmp_path: Path) -> None:
    devices = PixelDevices()
    port = mobile_port(stub, paths, tmp_path, devices, NativeCalls())
    stub.replies["_capture.prepare"] = {"ok": True, "value": {"keys": [], "boxes": BRIDGE_BOXES}, "files": [],
                                        "detail": {}}
    shot = port.call("capture.shot", {"name": "home"}, site=site("capture.shot"))
    assert [(f.role, f.name) for f in shot.files] == [("value", "home"), ("evidence", "home.boxes.json")]
    assert stub.bodies[0]["args"]["boxes"] is True
    boxes = BoxMap.from_json(json.loads(shot.files[1].path.read_text(encoding="utf-8")))
    assert boxes.source == "app" and boxes.size == (200, 100)
    assert resolve_on_boxmap({"by": "test_id", "args": {"id": "save"}}, boxes) == (20, 10, 60, 20)
    stub.replies["check.checkpoint"] = {"ok": True, "value": {"label": "home", "ok": True, "checks": [], "shots": []},
                                        "files": [], "detail": {}}
    stub.replies["_capture.boxes"] = {"ok": True, "value": BRIDGE_BOXES, "files": [], "detail": {}}
    checkpoint = port.call("check.checkpoint", {"label": "home"}, site=site("check.checkpoint"))
    assert [f.role for f in checkpoint.files] == ["shot", "evidence"]
    started = time.time() * 1000
    stub.replies["_recording"] = {"ok": True, "value": {"startedAt": started}, "files": [], "detail": {}}
    port.call("capture.record", {"name": "tour", "trim": False}, site=site("capture.record"), phase="enter")
    stub.replies["_recording"] = {"ok": True, "value": {
        "startedAt": started, "annotations": [], "steps": [{"t": 0.5, "action": "press"}],
        "boxes": [{"t": 0.0, "action": None, "boxes": BRIDGE_BOXES},
                  {"t": 0.5, "action": "ui.tap", "boxes": {**BRIDGE_BOXES, "nodes": []}}]}, "files": [], "detail": {}}
    closed = port.call("capture.record", {"name": "tour", "trim": False}, site=site("capture.record"), phase="exit")
    assert [(f.role, f.kind) for f in closed.files] == [("value", "video"), ("evidence", "file"), ("evidence", "file")]
    recording = RecordingBoxes.from_json(json.loads(closed.files[2].path.read_text(encoding="utf-8")))
    assert closed.files[2].path.name == "tour.boxes.json"
    assert len(recording.steps) == 2 and recording.steps[0].boxes.size == (200, 100)
    first = recording.at(0.1)
    assert first is not None and first.nodes[0].box == (20, 10, 60, 20)
