from __future__ import annotations

import os
import time
from pathlib import Path

from eks_harness.score.live import LiveRegistry


class FakeDevice:
    sid = "abc123"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def act(self, verb: str, **args) -> dict:
        self.calls.append((verb, args))
        return {"ok": True}


def build(tmp_path: Path) -> Path:
    (tmp_path / "card.html").write_text("<html><head></head><body>card</body></html>", encoding="utf-8")
    (tmp_path / "score.py").write_text(
        "from eks_harness.score import Score, beats\n"
        "score = Score(fps=30, bpm=240, duration=2.0, name='live')\n"
        "app = score.device('ios', id='app')\n"
        "card = score.web('card.html', id='card')\n"
        "score.on(beats()[1], app.press('#go'), id='go')\n"
        "score.on(app.event('done'), card.emit('flip'), id='chain')\n", encoding="utf-8")
    return tmp_path / "score.py"


def test_live_session_fires_device_actions_and_chains_events(tmp_path: Path) -> None:
    from eks_harness.score.loader import load_any

    path = build(tmp_path)
    device = FakeDevice()
    registry = LiveRegistry()
    session = registry.open(load_any(path), tmp_path, devices={"app": device})
    seen: list[dict] = []
    session.listeners.append(seen.append)
    session.play(0.0)
    deadline = time.time() + 3
    while not device.calls and time.time() < deadline:
        time.sleep(0.02)
    assert device.calls == [("press", {"target": "#go"})]
    session.observe("app", "done", {"route": "/ok"})
    deadline = time.time() + 3
    while not any(m.get("type") == "input" for m in seen) and time.time() < deadline:
        time.sleep(0.02)
    inputs = [m for m in seen if m.get("type") == "input"]
    assert inputs and inputs[0]["target"] == "card" and inputs[0]["name"] == "flip" and inputs[0]["rule"] == "chain"
    session.pause()
    paused_at = session.now()
    time.sleep(0.15)
    assert session.now() == paused_at
    session.seek(0.0)
    assert session.now() == 0.0
    assert registry.list()[0]["state"] in ("paused", "playing")
    assert registry.close(session.id)


def test_live_api_and_scene_files(client, tmp_path: Path) -> None:
    path = build(tmp_path)
    opened = client.post("/api/scores/live", json={"path": str(path), "analyze": False})
    assert opened.status_code == 200, opened.text
    session_id = opened.json()["id"]
    scene = client.get(f"/api/scores/live/{session_id}/scene/card/card.html")
    assert scene.status_code == 200 and "__EHX_SCENE_CONFIG__" in scene.text and "scene-runtime.js" in scene.text
    assert client.get(f"/api/scores/live/{session_id}/scene/card/../score.py").status_code == 404
    with client.websocket_connect(f"/api/scores/live/{session_id}/ws?role=ui") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["plan"]["fps"] == 30
        ws.send_json({"type": "emit", "track": "app", "name": "done"})
        message = ws.receive_json()
        while message.get("type") != "event":
            message = ws.receive_json()
        assert message["name"] == "done"
    assert client.get(f"/api/scores/live/{session_id}/audio").status_code == 404
    assert client.post(f"/api/scores/live/{session_id}/stop").json()["state"] == "stopped"
    assert client.delete(f"/api/scores/live/{session_id}").json() == {"closed": session_id}
    listing = client.get("/api/scores", params={"tree": str(tmp_path)})
    assert listing.status_code == 200


def test_live_session_renders_blender_previews(tmp_path: Path, monkeypatch) -> None:
    import pytest

    from eks_harness.score.loader import load_any
    from eks_harness.video.plugins.builtin.media.blender import runner

    try:
        runner.find_blender()
    except RuntimeError:
        pytest.skip("needs Blender")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                       or str(Path.home() / "Library" / "Caches" / "ms-playwright"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "phone.py").write_text(
        "import bpy\nfor o in list(bpy.data.objects):\n    bpy.data.objects.remove(o)\n"
        "cam = bpy.data.objects.new('Cam', bpy.data.cameras.new('Cam'))\n"
        "bpy.context.scene.collection.objects.link(cam)\ncam.location = (0, 0, 3)\n"
        "bpy.context.scene.camera = cam\n", encoding="utf-8")
    (tmp_path / "score.py").write_text(
        "from eks_harness.score import Score\n"
        "score = Score(fps=12, bpm=120, duration=0.5, size=(256, 256), name='prev')\n"
        "app = score.device('ios', id='app')\n"
        "card = score.web('card.html', id='card', size=(256, 256))\n"
        "score.blender('phone.py', id='phone', engine='BLENDER_WORKBENCH', textures={'screen': app, 'card': card})\n",
        encoding="utf-8")
    (tmp_path / "card.html").write_text("<html><body style='background:red'></body></html>", encoding="utf-8")
    registry = LiveRegistry()
    session = registry.open(load_any(tmp_path / "score.py"), tmp_path, previews=tmp_path / "previews")
    deadline = time.time() + 120
    while session.previews.get("phone", {}).get("state") in (None, "rendering") and time.time() < deadline:
        time.sleep(0.2)
    preview = session.previews["phone"]
    assert preview["state"] == "ready", preview
    assert Path(preview["path"]).suffix == ".webm" and Path(preview["path"]).exists()
    assert preview["withoutTextures"] == ["screen"]
    assert session.snapshot()["previews"]["phone"]["url"].endswith("/phone")
    registry.close(session.id)
