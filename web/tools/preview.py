from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WEB_DIST = REPO / "web" / "dist"
WORK = Path(os.environ.get("EHX_PREVIEW_DIR") or Path(os.environ.get("CLAUDE_JOB_DIR") or tempfile.gettempdir())
            / "tmp" / "eks-harness-ui-preview")
STATE = WORK / "state.json"
SHOTS = WORK / "shots"
TREE = WORK / "tree"

DEVICES = {
    "phone": {"width": 390, "height": 844, "scale": 2, "mobile": True},
    "tablet": {"width": 834, "height": 1112, "scale": 2, "mobile": True},
    "desktop": {"width": 1440, "height": 900, "scale": 1, "mobile": False},
    "wide": {"width": 1920, "height": 1080, "scale": 1, "mobile": False},
}

WEB_PROJECT = "acme/web-app"
MOBILE_PROJECT = "acme/mobile-app"
SESSION = "feature/checkout-redesign"
SESSION_SLUG = "feature-checkout-redesign"


class PreviewError(Exception):
    pass


def say(text: str) -> None:
    print(text, flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def load_state() -> dict:
    if not STATE.exists():
        raise PreviewError("no preview hub; run: preview.py up")
    return json.loads(STATE.read_text())


def request(base: str, method: str, path: str, body: dict | None = None, *, data: bytes | None = None,
            headers: dict | None = None, params: dict | None = None) -> dict | list | None:
    payload = data if data is not None else (json.dumps(body).encode() if body is not None else None)
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    req = urllib.request.Request(base + path + query, data=payload, method=method,
                                 headers={"User-Agent": "eks-harness-ui-preview", **(headers or {}),
                                          **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as problem:
        detail = problem.read().decode(errors="replace")[:400]
        raise PreviewError(f"{method} {path}: {problem.code} {detail}") from None


import urllib.parse  # noqa: E402


def multipart(fields: dict[str, str], files: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----ehxpreview{int(time.time() * 1000)}"
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
    for name, filename, content, mime in files:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n").encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def upload(base: str, fields: dict[str, str], filename: str, content: bytes, mime: str, path: str = "/api/artifacts") -> dict:
    data, ctype = multipart({**fields, "source": "agent"}, [("file", filename, content, mime)])
    return request(base, "POST", path, data=data, headers={"Content-Type": ctype})  # type: ignore[return-value]


def png(width: int, height: int, top: tuple[int, int, int], band: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = []
    for y in range(height):
        color = band if height * 0.08 < y < height * 0.14 or (height * 0.3 < y < height * 0.34) else top
        if width * 0.06 < (y * 7) % width < width * 0.5 and height * 0.4 < y < height * 0.9:
            color = tuple(max(0, c - 18) for c in top)
        rows.append(b"\x00" + bytes(color) * width)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b"")


def har() -> bytes:
    entries = []
    for i, (method, url, status, mime, size) in enumerate([
        ("GET", "https://app.example.test/", 200, "text/html", 18234),
        ("GET", "https://app.example.test/assets/main.js", 200, "application/javascript", 284120),
        ("GET", "https://api.example.test/v1/cart", 200, "application/json", 1840),
        ("POST", "https://api.example.test/v1/checkout/preview", 422, "application/json", 512),
        ("GET", "https://api.example.test/v1/addresses", 200, "application/json", 9241),
        ("GET", "https://app.example.test/images/hero.png", 304, "image/png", 0),
        ("GET", "https://api.example.test/v1/notifications", 0, "", 0),
    ]):
        entries.append({
            "startedDateTime": "2026-10-09T13:10:00.000Z", "time": 40 + i * 31,
            "request": {"method": method, "url": url, "headers": [{"name": "Accept", "value": "*/*"}], "queryString": []},
            "response": {"status": status, "statusText": "", "headers": [{"name": "Content-Type", "value": mime}],
                         "content": {"size": size, "mimeType": mime, "text": json.dumps({"ok": status < 400}) if "json" in mime else ""}},
            "timings": {"blocked": 1, "dns": 0, "connect": 0, "ssl": 0, "send": 1, "wait": 30 + i * 20, "receive": 8},
            "_resourceType": "document" if mime == "text/html" else "script" if "javascript" in mime else "xhr",
            **({"_error": "net::ERR_CONNECTION_RESET"} if status == 0 else {}),
        })
    return json.dumps({"log": {"version": "1.2", "creator": {"name": "eks-harness", "version": "0"}, "entries": entries}}).encode()


def log_text() -> bytes:
    lines = []
    for i in range(1, 420):
        level = "error" if i % 97 == 0 else "warn" if i % 41 == 0 else "info"
        lines.append(f"13:10:{i % 60:02d}.{i * 7 % 1000:03d} [{level}] checkout: rendered step={i // 20} items={i % 13}")
    return "\n".join(lines).encode()


def site_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("index.html", "<!doctype html><title>Checkout</title><body style='font-family:sans-serif;padding:40px'>"
                                       "<h1>Checkout</h1><p>Static export of the redesigned flow.</p></body>")
    return buffer.getvalue()


def make_video(target: Path, size: str = "640x400", seconds: int = 4) -> bytes | None:
    if not shutil.which("ffmpeg"):
        return None
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration={seconds}",
                    "-pix_fmt", "yuv420p", str(target)], check=True)
    return target.read_bytes()


SCENE_CARD = """<!doctype html><html><head><style>
html,body{margin:0;height:100%;background:#151618;color:#ececea;font-family:sans-serif}
#c{position:absolute;inset:20% 15%;background:#d42e18;display:flex;align-items:center;justify-content:center;font-size:64px}
</style></head><body><div id="c">Ready</div><script>
ehx.on("flip", () => { document.getElementById("c").textContent = "Flip"; ehx.emit("flipped"); });
</script></body></html>"""

SCORE = """from eks_harness.score import Score, beats, cue

score = Score(fps=30, bpm=112, duration=16.0, size=(1080, 1920), name="launch-reel", song="tone.wav", window=(0.0, 16.0))
score.cue("drop", 8.57)
app = score.device("ios", id="app", flow="flows/checkout.py")
cards = score.web("scenes/cards/index.html", id="cards")
phone = score.blender("scenes/phone.py", id="phone", textures={"screen": app, "cards": cards})
titles = score.web("scenes/titles/index.html", id="titles")
score.on(beats("downbeat")[0:8], cards.emit("flip"), id="flip-on-bars")
score.on(beats()[4], app.press("#start-checkout"), id="start-checkout")
score.on(app.event("order-placed"), phone.emit("impact"), id="order-to-phone")
score.on(cards.event("flipped"), titles.emit("pulse"), id="cards-to-titles")
score.on(cue("drop"), phone.emit("spin"), titles.set("headline", "Paid"), id="drop")
score.edit.layer(phone).layer(cards).layer(titles)
"""

VIDEO_PROJECT = """from eks_harness.video.ir import AudioSegment, AudioTrack, Project, Seconds, Segment, Solid, Track
from eks_harness.video.ir.markers import BeatTracker
from eks_harness.video.ir.media import AudioFile

LENGTH = 12.0
project = Project(
    fps=30, resolution=(1080, 1920), duration=LENGTH,
    tracks=[
        Track(name="main", z=0, segments=[
            Segment(id="intro", start=Seconds(t=0.0), out=Seconds(t=3.21), media=Solid(color=(20, 21, 24, 255))),
            Segment(id="hook", start=Seconds(t=3.21), out=Seconds(t=4.29), media=Solid(color=(212, 46, 24, 255))),
            Segment(id="proof", start=Seconds(t=7.5), out=Seconds(t=4.5), media=Solid(color=(236, 236, 232, 255))),
        ]),
        Track(name="titles", z=1, segments=[
            Segment(id="title-a", start=Seconds(t=0.54), out=Seconds(t=2.14), media=Solid(color=(255, 255, 255, 40))),
            Segment(id="title-b", start=Seconds(t=7.5), out=Seconds(t=3.2), media=Solid(color=(255, 255, 255, 40))),
        ]),
    ],
    audio_tracks=[AudioTrack(name="music", segments=[
        AudioSegment(id="song", start=Seconds(t=0.0), out=Seconds(t=LENGTH), media=AudioFile(path="media/click.wav")),
    ])],
    markers=[BeatTracker(name="beats", source="music", bpm=112, backend="librosa")],
)
"""

PLUGIN_UI = """import { createElement as h } from "react";
import { Section, StateLine, useHost } from "@eks-harness/ui-sdk";

function Tab() {
  const host = useHost();
  return h(Section, { title: "Release checklist", count: 3 },
    h("ul", { className: "plain-list" },
      h("li", null, h(StateLine, { state: "done" }), " Seed the staging persona set"),
      h("li", null, h(StateLine, { state: "running" }), " Capture checkout on iPhone and Pixel"),
      h("li", null, h(StateLine, { state: "queued" }), " Attach the evidence to the pull request")),
    h("p", { className: "ink-3" }, "Project ", h("span", { className: "mono" }, host.project)));
}

export default { "project.tab": Tab };
"""


def write_tree() -> None:
    if TREE.exists():
        shutil.rmtree(TREE)
    harness = TREE / ".harness"
    (harness / "plugins" / "release" / "ui").mkdir(parents=True)
    (harness / "project.toml").write_text(f'[project]\nid = "{WEB_PROJECT}"\n\n[plugins]\ntrust = ["release"]\n', encoding="utf-8")
    (harness / "plugins" / "release" / "harness-plugin.toml").write_text(
        '[plugin]\nid = "acme.release"\nname = "Release checklist"\nversion = "0.3.0"\n'
        'description = "Release checklist tab and staging personas"\n\n'
        '[[contributes.ui]]\nslot = "project.tab"\nmodule = "ui/tab.js"\ntitle = "Release"\n\n'
        '[config]\nchannel = { type = "str", default = "beta", description = "release channel" }\n', encoding="utf-8")
    (harness / "plugins" / "release" / "ui" / "tab.js").write_text(PLUGIN_UI, encoding="utf-8")
    scores = harness / "scores" / "launch"
    (scores / "scenes" / "cards").mkdir(parents=True)
    (scores / "scenes" / "titles").mkdir(parents=True)
    (scores / "scenes" / "cards" / "index.html").write_text(SCENE_CARD, encoding="utf-8")
    (scores / "scenes" / "titles" / "index.html").write_text(SCENE_CARD.replace("Ready", "Launch"), encoding="utf-8")
    (scores / "scenes" / "phone.py").write_text("import eks_harness.blender as ehb\n", encoding="utf-8")
    (scores / "score.py").write_text(SCORE, encoding="utf-8")
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=16",
                        str(scores / "tone.wav")], check=True)
    pending = harness / "plugins" / "lighthouse"
    pending.mkdir(parents=True)
    (pending / "harness-plugin.toml").write_text(
        '[plugin]\nid = "acme.lighthouse"\nname = "Lighthouse capture"\nversion = "0.1.0"\n'
        'description = "Lighthouse reports as artifacts"\n\n[[contributes.capture]]\nid = "lighthouse"\n'
        'entry = "lighthouse:Capture"\n', encoding="utf-8")


def write_video_workspace(root: Path) -> None:
    for name, body in (("launch-reel", VIDEO_PROJECT), ("pricing-explainer", VIDEO_PROJECT.replace("LENGTH = 12.0", "LENGTH = 18.0"))):
        folder = root / name
        (folder / "renders").mkdir(parents=True, exist_ok=True)
        (folder / "media").mkdir(exist_ok=True)
        (folder / "project.py").write_text(body, encoding="utf-8")
        if shutil.which("ffmpeg"):
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "aevalsrc='if(lt(mod(t,60/112),0.04),sin(2*PI*880*t),0)':d=20:s=44100",
                            str(folder / "media" / "click.wav")], check=True)


class FakeAgents:
    def __init__(self, base: str, specs: list[tuple[str, str, dict, list]]) -> None:
        self.base = base
        self.specs = specs


def run_agents(state: dict) -> None:
    from eks_harness.nodes.agent import NodeAgent, NodeSettings
    from eks_harness.nodes.jobs import BUILTIN
    from eks_harness.paths import resolve_paths

    paths = resolve_paths()
    agents = []
    for spec in state["nodes"]:
        settings = NodeSettings(hub=state["url"], token=spec["token"], name=spec["id"], slots=spec["slots"],
                                cache_dir=str(WORK / "node-cache" / spec["id"]))
        agent = NodeAgent(settings, paths, handlers={"blender.frames": BUILTIN["echo"], "web.scene": BUILTIN["echo"]})
        caps = spec["capabilities"]

        def refresh(agent=agent, caps=caps, slots=spec["slots"]) -> None:
            agent.capabilities = {**caps, "jobKinds": sorted(agent.handlers)}
            agent.slots = slots

        agent.refresh_capabilities = refresh
        agents.append(agent)
    threads = [threading.Thread(target=lambda a=a: asyncio.run(a.run()), daemon=True) for a in agents]
    for thread in threads:
        thread.start()
    while True:
        time.sleep(3600)


NODES = [
    {"id": "gpu-rack", "label": "GPU rack", "capabilities": {
        "os": "linux", "arch": "x86_64", "hostname": "gpu-rack", "cpus": 32, "memoryMb": 128000, "diskFreeGb": 812.4,
        "diskTotalGb": 1863.0, "gpus": [
            {"index": 0, "name": "RTX 5090", "vendor": "nvidia", "memoryMb": 32607, "backends": ["CUDA", "OPTIX"]},
            {"index": 1, "name": "RTX 3090", "vendor": "nvidia", "memoryMb": 24576, "backends": ["CUDA", "OPTIX"]},
            {"index": 2, "name": "RTX 3090", "vendor": "nvidia", "memoryMb": 24576, "backends": ["CUDA", "OPTIX"]}],
        "blender": {"path": "/opt/blender/blender", "version": "4.5.3"}, "tools": {"ffmpeg": "/usr/bin/ffmpeg"}},
     "slots": [{"id": "gpu0", "workers": 2, "backend": "OPTIX", "tags": ["gpu", "optix"], "env": {"CUDA_VISIBLE_DEVICES": "0"}},
               {"id": "gpu1", "workers": 1, "backend": "OPTIX", "tags": ["gpu", "optix"], "env": {"CUDA_VISIBLE_DEVICES": "1"}},
               {"id": "gpu2", "workers": 1, "backend": "OPTIX", "tags": ["gpu", "optix"], "env": {"CUDA_VISIBLE_DEVICES": "2"}},
               {"id": "cpu", "workers": 4, "tags": ["cpu"], "env": {}}]},
    {"id": "studio-pc", "label": "Studio PC (WSL)", "capabilities": {
        "os": "linux", "arch": "x86_64", "hostname": "studio-pc", "cpus": 24, "memoryMb": 65536, "wsl": True,
        "diskFreeGb": 402.0, "diskTotalGb": 953.0,
        "gpus": [{"index": 0, "name": "RTX 5090", "vendor": "nvidia", "memoryMb": 32607, "backends": ["CUDA", "OPTIX"]}],
        "blender": {"path": "/home/render/blender/blender", "version": "4.5.3"}},
     "slots": [{"id": "gpu0", "workers": 2, "backend": "CUDA", "tags": ["gpu", "cuda"], "env": {"CUDA_VISIBLE_DEVICES": "0"}},
               {"id": "cpu", "workers": 2, "tags": ["cpu"], "env": {}}]},
]


def seed(base: str) -> dict:
    created: dict = {}
    for project, title in ((WEB_PROJECT, "Web app"), (MOBILE_PROJECT, "Mobile app"), ("acme/marketing", "Marketing")):
        try:
            request(base, "POST", "/api/projects", {"id": project, "title": title})
        except PreviewError:
            pass
    request(base, "PATCH", f"/api/projects/{MOBILE_PROJECT}", {"retentionDays": 30})
    browser = request(base, "POST", "/api/leases/acquire", {"kind": "browser", "project": WEB_PROJECT, "session": SESSION, "instance": "agent:preview"})
    ios = request(base, "POST", "/api/leases/acquire", {"kind": "ios", "project": MOBILE_PROJECT, "session": SESSION, "instance": "agent:preview"})
    android = request(base, "POST", "/api/leases/acquire", {"kind": "android", "project": MOBILE_PROJECT, "session": SESSION, "instance": "agent:preview"})
    other = request(base, "POST", "/api/leases/acquire", {"kind": "browser", "project": WEB_PROJECT, "session": "bug/address-form-overflow", "instance": "agent:other"})
    web_sid, ios_sid, other_sid = browser["sid"], ios["sid"], other["sid"]  # type: ignore[index]
    created["sids"] = [web_sid, ios_sid, android["sid"], other_sid]  # type: ignore[index]
    palette = [((246, 244, 238), (20, 40, 80)), ((255, 255, 255), (212, 168, 42)), ((236, 240, 247), (11, 31, 58)), ((250, 250, 250), (60, 60, 60))]
    shots = [("checkout-cart.png", "Cart with three items", "smoke"), ("checkout-address.png", "Address step", "smoke"),
             ("checkout-payment.png", "Payment step", ""), ("checkout-preview-error.png", "Preview 422", "bug"),
             ("dashboard-wide.png", "", "")]
    ids = []
    for index, (name, caption, tags) in enumerate(shots):
        top, band = palette[index % len(palette)]
        ids.append(upload(base, {"sid": web_sid, "kind": "screenshot", "caption": caption, "tags": tags}, name,
                          png(1440, 900, top, band), "image/png")["id"])
    for index, name in enumerate(["cart-ios.png", "address-ios.png", "payment-ios.png"]):
        top, band = palette[(index + 1) % len(palette)]
        ids.append(upload(base, {"sid": ios_sid, "kind": "screenshot", "tags": "smoke" if index == 0 else ""}, name,
                          png(393, 852, top, band), "image/png")["id"])
    movie = make_video(WORK / "seed.mp4")
    if movie:
        created["video"] = upload(base, {"sid": ios_sid, "kind": "video", "caption": "Checkout flow"}, "checkout-flow.mp4", movie, "video/mp4")["id"]
    created["har"] = upload(base, {"sid": web_sid, "kind": "har"}, "network.har", har(), "application/json")["id"]
    created["log"] = upload(base, {"sid": web_sid, "kind": "console"}, "console.log", log_text(), "text/plain")["id"]
    created["json"] = upload(base, {"sid": web_sid, "kind": "file"}, "cart.json", json.dumps({"cart": {"id": "c-1042", "items": [{"sku": "A-1", "qty": 2}, {"sku": "B-7", "qty": 1}], "total": 129.5}}, indent=2).encode(), "application/json")["id"]
    created["dom"] = upload(base, {"sid": web_sid, "kind": "dom"}, "page.html", b"<!doctype html><body style='font-family:sans-serif;padding:32px'><h1>DOM snapshot</h1></body>", "text/html")["id"]
    data, ctype = multipart({"sid": web_sid, "caption": "Static checkout export", "source": "agent", "entry": "index.html"}, [("file", "site.zip", site_zip(), "application/zip")])
    created["site"] = request(base, "POST", "/api/sites", data=data, headers={"Content-Type": ctype})["id"]  # type: ignore[index]
    upload(base, {"sid": other_sid, "kind": "screenshot", "caption": "Address form overflow", "tags": "bug"}, "overflow.png", png(1280, 800, (255, 255, 255), (180, 40, 30)), "image/png")
    upload(base, {"project": "acme/marketing", "kind": "screenshot"}, "banner.png", png(1200, 700, (240, 240, 240), (30, 30, 30)), "image/png")
    created["images"] = ids
    request(base, "POST", "/api/artifacts/tags", {"ids": ids[:2], "add": ["review"], "remove": []})
    request(base, "PATCH", f"/api/artifacts/{ids[0]}", {"pinned": True})
    request(base, "PUT", "/api/tags/bug/color", {"color": "#d42e18"})
    share = request(base, "POST", f"/api/artifacts/{ids[0]}/shares", {"expires": "7d"})
    created["share"] = share["token"] if isinstance(share, dict) else None  # type: ignore[index]
    for body in ("Checkout flow passes on iOS; the 422 on preview is a backend validation bug.", "Repro: open /checkout with an empty address."):
        request(base, "POST", f"/api/sessions/{SESSION_SLUG}/notes", {"body": body})
    request(base, "POST", "/api/artifacts/seen", {"ids": ids[1:3], "seen": True})
    nodes = []
    for spec in NODES:
        made = request(base, "POST", "/api/nodes", {"id": spec["id"], "label": spec["label"]})
        nodes.append({**spec, "token": made["token"]})  # type: ignore[index]
    created["nodes"] = nodes
    request(base, "POST", "/api/nodes", {"id": "mac-mini", "label": "Mac mini (offline)"})
    request(base, "GET", "/api/plugins", params={"tree": str(TREE)})
    request(base, "POST", "/api/plugins/acme.release/trust", {"tree": str(TREE)})
    return created


def seed_jobs(base: str) -> None:
    for frames in ((1, 48), (49, 96)):
        request(base, "POST", "/api/jobs", {"kind": "blender.frames", "requirements": {"gpu": True},
                                            "payload": {"scene": "phone.blend", "frames": list(frames), "sleep": 900, "steps": 900}})
    request(base, "POST", "/api/jobs", {"kind": "web.scene", "payload": {"scene": "cards", "value": "rendered 240 frames"}})
    request(base, "POST", "/api/jobs", {"kind": "blender.frames", "payload": {"fail": "CUDA error: out of memory"}})
    request(base, "POST", "/api/jobs", {"kind": "blender.frames", "requirements": {"capabilities": ["metal"]},
                                        "payload": {"scene": "phone.blend", "frames": [97, 144]}})


def cmd_up(args: argparse.Namespace) -> int:
    if STATE.exists():
        state = json.loads(STATE.read_text())
        if alive(state["pid"]):
            say(f"preview hub already runs: {state['url']}")
            return 0
        STATE.unlink()
    if not (WEB_DIST / "index.html").exists():
        raise PreviewError("the web UI is not built: pnpm --filter ./web build")
    home = WORK / "home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    video_root = WORK / "video"
    if video_root.exists():
        shutil.rmtree(video_root)
    write_tree()
    write_video_workspace(video_root)
    port = args.port or free_port()
    env = {**os.environ, "EKS_HARNESS_HOME": str(home), "EKS_HARNESS_FAKE_POOLS": "1", "EKS_HARNESS_WEB_DIST": str(WEB_DIST),
           "EKS_HARNESS_LEASE_IDLESECONDS": "86400", "EKS_HARNESS_DAEMON_IDLEEXITSECONDS": "0", "NO_COLOR": "1",
           "EKS_HARNESS_VIDEO_WORKSPACE": str(video_root), "EKS_HARNESS_NODES_HUBURL": f"http://127.0.0.1:{port}"}
    for key in ("EKS_HARNESS_URL", "EKS_HARNESS_API_KEY"):
        env.pop(key, None)
    log = (WORK / "daemon.log").open("w")
    process = subprocess.Popen([sys.executable, "-m", "eks_harness.cli", "daemon", "serve", "--port", str(port), "--fake-pools"],
                               cwd=WORK, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            request(base, "GET", "/api/status")
            break
        except (PreviewError, OSError):
            if process.poll() is not None:
                raise PreviewError(f"the hub did not start; log: {WORK / 'daemon.log'}") from None
            time.sleep(0.5)
    else:
        raise PreviewError("the hub did not answer within 60 s")
    created = seed(base)
    state = {"pid": process.pid, "port": port, "url": base, "home": str(home), "tree": str(TREE), "seed": created,
             "nodes": created["nodes"]}
    STATE.write_text(json.dumps(state, indent=2))
    agents_log = (WORK / "agents.log").open("w")
    agents = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "agents"], env=env, stdout=agents_log,
                              stderr=subprocess.STDOUT, start_new_session=True)
    state["agents"] = agents.pid
    STATE.write_text(json.dumps(state, indent=2))
    for _ in range(40):
        items = request(base, "GET", "/api/nodes")["items"]  # type: ignore[index]
        if sum(1 for n in items if n["online"]) >= len(NODES):
            break
        time.sleep(0.5)
    seed_jobs(base)
    say(f"preview hub: {base}")
    say(f"home: {home}")
    say(f"tree: {TREE}")
    return 0


def cmd_agents(_: argparse.Namespace) -> int:
    run_agents(load_state())
    return 0


def routes(state: dict) -> list[str]:
    seed_data = state.get("seed") or {}
    images = seed_data.get("images") or []
    tree = urllib.parse.quote(state.get("tree") or "")
    score = urllib.parse.quote(str(Path(state.get("tree") or "") / ".harness" / "scores" / "launch" / "score.py"))
    out = ["/", "/lab", "/devices/ios/1", "/browsers/profiles/browser:1:1", "/backends", "/evidence", f"/sessions/{SESSION_SLUG}",
           "/projects", f"/p/{WEB_PROJECT}", f"/backends", "/search?q=checkout", "/studio", "/studio/edit?project=launch-reel",
           f"/studio/score?path={score}", "/nodes", "/nodes/gpu-rack", "/plugins", f"/plugins?tree={tree}",
           "/settings/server"]
    if images:
        out.append(f"/sessions/{SESSION_SLUG}/a/{images[0]}")
    for key in ("video", "har"):
        if seed_data.get(key):
            out.append(f"/sessions/{SESSION_SLUG}/a/{seed_data[key]}")
    if seed_data.get("share"):
        out.append(f"/s/{seed_data['share']}")
    return out


SPILL_SCRIPT = """
() => {
  const width = document.documentElement.clientWidth;
  const out = [];
  const small = [];
  const phone = width < 700;
  for (const el of document.querySelectorAll('body *')) {
    const style = getComputedStyle(el);
    if (style.position === 'fixed' || el.closest('.scroll-x, .ledger-wrap, .text-frame, .har-table, .filmstrip, .tabs, .json-tree, .frame-stage, pre, .sheet-scroll, .rack, .ledger')) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.right > width + 1 || r.left < -1) out.push(`${el.tagName.toLowerCase()}.${[...el.classList].join('.')} ${Math.round(r.left)}..${Math.round(r.right)}`);
    const hit = Math.max(r.height, parseFloat(getComputedStyle(el, '::after').height) || 0);
    if (phone && (el.tagName === 'BUTTON' || el.tagName === 'A') && hit < 32 && r.height > 0 && !el.closest('.sheet, .inline-links')) small.push(`${el.tagName.toLowerCase()} "${(el.textContent || '').trim().slice(0, 20)}" ${Math.round(r.height)}px`);
  }
  return { pageScroll: document.documentElement.scrollWidth > width + 1, spill: out.slice(0, 12), small: small.slice(0, 8) };
}
"""


def slug_for(route: str) -> str:
    cleaned = route.strip("/").split("?")[0].replace("/", "_") or "now"
    if "?" in route:
        tail = route.split("?", 1)[1].split("=")[0]
        cleaned += f"_{tail}"
    return cleaned[:80]


def cmd_shoot(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    state = load_state()
    if not alive(state["pid"]):
        raise PreviewError("the preview hub stopped; run: preview.py up")
    targets = args.route or routes(state)
    devices = args.device or list(DEVICES)
    themes = args.theme or ["light", "dark"]
    SHOTS.mkdir(parents=True, exist_ok=True)
    report = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for device in devices:
            spec = DEVICES[device]
            for theme in themes:
                context = browser.new_context(viewport={"width": spec["width"], "height": spec["height"]},
                                              device_scale_factor=spec["scale"], is_mobile=spec["mobile"],
                                              has_touch=spec["mobile"], color_scheme=theme)
                context.add_init_script(f"try {{ localStorage.setItem('ehx.currentProject', '{WEB_PROJECT}') }} catch (e) {{}}")
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                for route in targets:
                    errors.clear()
                    try:
                        page.goto(state["url"] + route, wait_until="load", timeout=30_000)
                        page.wait_for_load_state("networkidle", timeout=4_000)
                    except Exception as problem:
                        if "Timeout" not in type(problem).__name__ and "Timeout" not in str(problem):
                            errors.append(f"goto: {problem}")
                    page.wait_for_timeout(args.wait)
                    name = f"{slug_for(route)}--{device}-{theme}.png"
                    page.screenshot(path=str(SHOTS / name), full_page=args.full)
                    spill = page.evaluate(SPILL_SCRIPT)
                    report.append({"route": route, "device": device, "theme": theme, "file": str(SHOTS / name),
                                   "pageScroll": spill["pageScroll"], "spill": spill["spill"], "small": spill["small"],
                                   "errors": [e for e in errors if "favicon" not in e][:5]})
                if device in ("desktop", "phone") and not args.route:
                    score = [r for r in targets if r.startswith("/studio/score")]
                    if score:
                        try:
                            page.goto(state["url"] + score[0], wait_until="load", timeout=30_000)
                            page.wait_for_timeout(1500)
                            page.get_by_role("button", name="Play live").click()
                            page.wait_for_timeout(600)
                            name = f"score-bind--{device}-{theme}.png"
                            page.screenshot(path=str(SHOTS / name), full_page=args.full)
                            report.append({"route": "score bind", "device": device, "theme": theme, "file": str(SHOTS / name), "pageScroll": False, "spill": [], "small": [], "errors": []})
                            page.get_by_role("button", name="Start live mode").click()
                            page.wait_for_timeout(2500)
                            page.get_by_role("button", name="Play", exact=True).click()
                            page.wait_for_timeout(2500)
                            name = f"score-live--{device}-{theme}.png"
                            page.screenshot(path=str(SHOTS / name), full_page=args.full)
                            report.append({"route": "score live", "device": device, "theme": theme, "file": str(SHOTS / name), "pageScroll": False, "spill": [], "small": [], "errors": list(errors)})
                            page.get_by_role("button", name="Close live mode").click()
                        except Exception as problem:
                            report.append({"route": "score live", "device": device, "theme": theme, "file": "", "pageScroll": False, "spill": [], "small": [], "errors": [f"live flow: {problem}"[:300]]})
                context.close()
        browser.close()
    (WORK / "report.json").write_text(json.dumps(report, indent=2))
    problems = 0
    for row in report:
        bad = row["pageScroll"] or row["spill"] or row["errors"] or row["small"]
        if bad:
            problems += 1
            say(f"{row['route']} {row['device']}/{row['theme']}: scroll={row['pageScroll']} spill={row['spill'][:2]} "
                f"small={row['small'][:2]} errors={row['errors'][:2]}")
    say(f"{len(report)} shots in {SHOTS}, {problems} with findings; report {WORK / 'report.json'}")
    return 0


def cmd_down(_: argparse.Namespace) -> int:
    if not STATE.exists():
        say("no preview hub")
        return 0
    state = json.loads(STATE.read_text())
    for key in ("agents", "pid"):
        try:
            os.killpg(state[key], signal.SIGTERM)
        except (OSError, KeyError):
            pass
    STATE.unlink()
    say("preview hub stopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="isolated preview hub and screenshot runner for the eks-harness UI")
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="start an isolated hub from this checkout and seed it")
    up.add_argument("--port", type=int)
    up.set_defaults(func=cmd_up)
    agents = sub.add_parser("agents", help=argparse.SUPPRESS)
    agents.set_defaults(func=cmd_agents)
    shoot = sub.add_parser("shoot", help="screenshot routes at every device size and theme")
    shoot.add_argument("--route", action="append")
    shoot.add_argument("--device", action="append", choices=list(DEVICES))
    shoot.add_argument("--theme", action="append", choices=["light", "dark"])
    shoot.add_argument("--full", action="store_true")
    shoot.add_argument("--wait", type=int, default=700)
    shoot.set_defaults(func=cmd_shoot)
    down = sub.add_parser("down", help="stop the preview hub")
    down.set_defaults(func=cmd_down)
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        return args.func(args)
    except PreviewError as problem:
        say(f"error: {problem}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
