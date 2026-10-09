from __future__ import annotations

import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eks_harness.cli import main
from eks_harness.cli.client import HarnessClient
from eks_harness.config import Config
from eks_harness.store.encode import EncodeError, encode_recording, target_size, top_level_atoms

from conftest import TestClientTransport, make_app
from test_leases import granted
from test_store_support import png_bytes, upload

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg and ffprobe are needed")


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def make_clip(path: Path) -> Path:
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=d=1.5:s=320x240:r=30",
        "-f", "lavfi", "-i", "color=c=gray:d=6:s=320x240:r=30",
        "-f", "lavfi", "-i", "testsrc=d=1.5:s=320x240:r=30",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_target_size_keeps_even_dimensions_within_level_41() -> None:
    assert target_size(320, 240) == (320, 240)
    width, height = target_size(1179 * 2, 2556 * 2)
    assert width % 2 == 0 and height % 2 == 0
    assert ((width + 15) // 16) * ((height + 15) // 16) <= 8192


@needs_ffmpeg
def test_encode_recording_writes_a_verified_full_and_trimmed_clip(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "raw.mp4")
    result = encode_recording(source, tmp_path / "out.mp4", trim=True)
    assert result.full.is_file() and result.trimmed is not None and result.trimmed.is_file()
    full, trimmed = probe_duration(result.full), probe_duration(result.trimmed)
    assert abs(full - 9.0) < 0.3
    assert trimmed < full - 2
    for path in (result.full, result.trimmed):
        atoms = top_level_atoms(path)
        assert atoms.index("moov") < atoms.index("mdat")
    assert result.report[0].startswith("full:") and result.report[1].startswith("trimmed:")


@needs_ffmpeg
def test_encode_recording_pads_to_the_wall_clock_length(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "raw.mp4")
    result = encode_recording(source, tmp_path / "out.mp4", pad_to=11.0)
    assert result.trimmed is None
    assert abs(probe_duration(result.full) - 11.0) < 0.3


def test_encode_rejects_a_file_without_video(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    with pytest.raises(EncodeError):
        encode_recording(broken, tmp_path / "out.mp4")


@needs_ffmpeg
def test_device_video_stop_stores_trimmed_and_full_length_videos(client: TestClient) -> None:
    sid = granted(client, kind="ios", project="acme/mobile-app", session="main")["sid"]
    assert client.post(f"/api/captures/{sid}/video/start", json={}).status_code == 200
    stopped = client.post(f"/api/captures/{sid}/video/stop", json={"caption": "flow", "tags": ["smoke"]})
    assert stopped.status_code == 200, stopped.text
    video = stopped.json()
    assert video["kind"] == "video" and "trimmed" in video["tags"] and video["meta"]["trimmed"] is True
    assert video["full"]["kind"] == "video" and "full" in video["full"]["tags"]
    assert video["meta"]["fullId"] == video["full"]["id"]
    assert video["full"]["caption"] == "flow (full length)"
    assert video["encoding"] and video["encoding"][0].startswith("full:")
    raw = client.get("/" + video["rawUrl"].split("/", 3)[3])
    assert raw.status_code == 200 and raw.content[4:8] == b"ftyp"

    assert client.post(f"/api/captures/{sid}/video/start", json={}).status_code == 200
    single = client.post(f"/api/captures/{sid}/video/stop", json={"meta": {"trim": False}}).json()
    assert single.get("full") is None and single["meta"]["trimmed"] is False


def test_ios_video_capture_suspends_the_live_view(client: TestClient, ctx) -> None:
    sid = granted(client, kind="ios", project="acme/mobile-app", session="main")["sid"]
    hub = ctx.service("live")
    assert client.post(f"/api/captures/{sid}/video/start", json={}).status_code == 200
    assert hub.suspended("ios:1") == "video capture"
    client.post(f"/api/leases/{sid}/release", json={})
    assert hub.suspended("ios:1") is None


def test_screenshot_records_the_device_screen_size(client: TestClient) -> None:
    sid = granted(client, kind="android", project="acme/mobile-app", session="main")["sid"]
    shot = client.post(f"/api/captures/{sid}/screenshot", json={}).json()
    device = client.get("/api/devices/android/1").json()
    assert (device["screenWidth"], device["screenHeight"]) == (shot["width"], shot["height"])
    assert device["liveViewers"] == 0


def test_device_queue_entries_are_full_leases(paths) -> None:
    config = Config(paths)
    config.set_many({"devices.ios": 1, "devices.maxRunning": 1})
    with TestClient(make_app(config)) as client:
        holder = granted(client, kind="ios", project="acme/mobile-app", session="main", instance="agent:a")
        waiting = client.post("/api/leases/acquire", json={
            "kind": "ios", "project": "acme/mobile-app", "session": "second", "instance": "agent:b",
            "wait": False}, params={"waitSeconds": 0}).json()
        assert waiting["status"] == "queued", waiting
        detail = client.get(f"/api/devices/ios/{holder['lease']['device']['index']}").json()
        assert detail["queue"], detail
        entry = detail["queue"][0]
        assert entry["projectId"] == "acme/mobile-app" and entry["sessionName"] == "second"
        assert entry["queuedAt"] is not None


def test_profile_output_has_status_since_and_page_url(client: TestClient) -> None:
    lease = granted(client)
    sid, resource = lease["sid"], lease["resource"]
    assert client.post(f"/api/leases/{sid}/meta", json={"meta": {"pageUrl": "http://localhost:3850/panel",
                                                                  "browserContextIds": ["ctx-1"]}}).status_code == 200
    detail = client.get(f"/api/profiles/{resource}").json()
    assert detail["statusSince"] is not None
    assert detail["pageUrl"] == "http://localhost:3850/panel"
    assert "screenWidth" in detail and detail["liveViewers"] == 0


def test_browser_endpoint_restarts_a_dead_browser(client: TestClient, ctx) -> None:
    lease = granted(client)
    sid = lease["sid"]
    first = client.post(f"/api/leases/{sid}/browser", json={})
    assert first.status_code == 200, first.text
    assert first.json()["cdp"] and first.json()["restarted"] is False
    ctx.pools.browsers.stop(first.json()["browser"], "test")
    again = client.post(f"/api/leases/{sid}/browser", json={}).json()
    assert again["restarted"] is True and again["cdp"]
    client.post(f"/api/leases/{sid}/release", json={})
    gone = client.post(f"/api/leases/{sid}/browser", json={})
    assert gone.status_code == 410 and "reacquire" in gone.json()


def test_browser_endpoint_refuses_device_leases(client: TestClient) -> None:
    sid = granted(client, kind="android", session="main")["sid"]
    response = client.post(f"/api/leases/{sid}/browser", json={})
    assert response.status_code == 409 and response.json()["error"] == "not_a_browser_lease"


def test_sid_info_reports_the_lease_like_the_lease_route(client: TestClient) -> None:
    sid = granted(client, kind="ios", project="acme/mobile-app", session="main")["sid"]
    info = client.get(f"/api/sid/{sid}").json()
    lease = client.get(f"/api/leases/{sid}").json()
    assert info["valid"] is True
    assert info["lease"]["device"] == lease["device"] and info["lease"]["device"]["kind"] == "ios"
    assert info["lease"]["urls"]["session"].endswith("/p/acme/mobile-app/s/main")


def seed_artifacts(client: TestClient) -> list[dict]:
    names = [("b.png", 300, "screenshot"), ("a.log", 100, "log"), ("c.png", 200, "screenshot"),
             ("d.txt", 400, "file"), ("e.log", 50, "log")]
    made = []
    for name, size, kind in names:
        content = png_bytes(10, 10) if name.endswith(".png") else b"x" * size
        response = upload(client, content, name, project="acme/web", session="main", kind=kind,
                          tags="alpha" if name in ("b.png", "a.log") else "beta")
        assert response.status_code == 201, response.text
        made.append(response.json())
    return made


def page(client: TestClient, **params) -> dict:
    response = client.get("/api/artifacts", params={"project": "acme/web", "session": "main", **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_artifact_list_sorts_on_the_server_and_pages_both_ways(client: TestClient) -> None:
    seed_artifacts(client)
    names = [i["filename"] for i in page(client, sort="name", limit=10)["items"]]
    assert names == ["a.log", "b.png", "c.png", "d.txt", "e.log"]
    by_size = [i["size"] for i in page(client, sort="size", limit=10)["items"]]
    assert by_size == sorted(by_size, reverse=True)
    first = page(client, sort="name", limit=2)
    assert [i["filename"] for i in first["items"]] == ["a.log", "b.png"] and first["prevCursor"] is None
    second = page(client, sort="name", limit=2, cursor=first["nextCursor"])
    assert [i["filename"] for i in second["items"]] == ["c.png", "d.txt"]
    third = page(client, sort="name", limit=2, cursor=second["nextCursor"])
    assert [i["filename"] for i in third["items"]] == ["e.log"] and third["nextCursor"] is None
    back = page(client, sort="name", limit=2, cursor=third["prevCursor"])
    assert [i["filename"] for i in back["items"]] == ["c.png", "d.txt"]
    start = page(client, sort="name", limit=2, cursor=back["prevCursor"])
    assert [i["filename"] for i in start["items"]] == ["a.log", "b.png"] and start["prevCursor"] is None
    descending = [i["filename"] for i in page(client, sort="name", dir="desc", limit=10)["items"]]
    assert descending == ["e.log", "d.txt", "c.png", "b.png", "a.log"]
    created = page(client, limit=2)
    older = page(client, limit=2, cursor=created["nextCursor"])
    assert older["prevCursor"] and not set(i["id"] for i in older["items"]) & set(i["id"] for i in created["items"])
    wrong = client.get("/api/artifacts", params={"project": "acme/web", "sort": "size", "cursor": first["nextCursor"]})
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_cursor"


def test_artifact_list_facets_count_kinds_and_tags(client: TestClient) -> None:
    seed_artifacts(client)
    facets = page(client, facets="true")["facets"]
    assert facets["kind"] == {"file": 1, "log": 2, "screenshot": 2}
    assert facets["tag"] == {"alpha": 2, "beta": 3}
    narrowed = page(client, facets="true", kind="log")
    assert narrowed["total"] == 2
    assert narrowed["facets"]["kind"]["screenshot"] == 2
    assert narrowed["facets"]["tag"] == {"alpha": 1, "beta": 1}
    assert page(client)["facets"] is None


def test_project_reports_its_project_level_file_count(client: TestClient) -> None:
    upload(client, b"x", "notes.txt", "text/plain", project="acme/web")
    upload(client, b"y", "other.txt", "text/plain", project="acme/web", session="main")
    project = client.get("/api/projects/acme/web").json()
    assert project["projectFileCount"] == 1 and project["artifactCount"] == 2


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_config_cli_writes_the_file_when_the_daemon_is_down(capsys, paths, monkeypatch) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    monkeypatch.setenv("EKS_HARNESS_URL", f"http://127.0.0.1:{probe.getsockname()[1]}")
    try:
        code, out, _ = run_cli(capsys, "config", "set", "server.port", "7391", "devices.ios", "2", "--json")
        assert code == 0, out
        changed = {c["key"]: c for c in json.loads(out)["changed"]}
        assert changed["server.port"]["new"] == 7391 and changed["server.port"]["restartRequired"] is True
        assert json.loads(paths.config_file.read_text())["devices.ios"] == 2
        code, out, _ = run_cli(capsys, "config", "get", "server.port")
        assert code == 0 and out.strip() == "7391"
        code, out, _ = run_cli(capsys, "config", "unset", "devices.ios", "--json")
        assert code == 0 and json.loads(out)["via"] == "file"
    finally:
        probe.close()
    assert "devices.ios" not in json.loads(paths.config_file.read_text())
    code, _, err = run_cli(capsys, "config", "set", "server.host", "0.0.0.0")
    assert code == 8 and "authentication disabled" in err
    code, _, err = run_cli(capsys, "config", "set", "server.nope", "1")
    assert code == 2 and "Unknown setting" in err
    code, _, err = run_cli(capsys, "config", "set", "server.port")
    assert code == 2
    code, out, _ = run_cli(capsys, "config", "show", "--group", "server", "--json")
    rows = json.loads(out)["settings"]
    assert {r["group"] for r in rows} == {"server"} and json.loads(out)["via"] == "file"


def test_config_cli_goes_through_the_api_when_the_daemon_is_up(capsys, client: TestClient, ctx,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    original = HarnessClient.__init__
    transport = TestClientTransport(client)

    def init(self, base_url=None, api_key=None, **kwargs):
        kwargs["transport"] = transport
        original(self, "http://testserver", api_key, **kwargs)

    monkeypatch.setattr(HarnessClient, "__init__", init)
    code, out, err = run_cli(capsys, "config", "set", "lease.idleSeconds", "900", "--json")
    assert code == 0, err
    result = json.loads(out)
    assert result["via"] == "daemon" and result["changed"][0]["new"] == 900
    assert ctx.config["lease.idleSeconds"] == 900
    audit = client.get("/api/settings/audit", params={"key": "lease.idleSeconds"}).json()
    assert audit and audit[0]["new"] == 900
    code, _, err = run_cli(capsys, "config", "set", "server.port", "7392")
    assert code == 0 and "needs a restart" in err
    code, out, _ = run_cli(capsys, "config", "get", "server.port", "--json")
    assert json.loads(out)["value"] == 7392 and json.loads(out)["via"] == "daemon"
    code, _, err = run_cli(capsys, "config", "set", "server.host", "0.0.0.0")
    assert code == 8 and "force" in err


def test_config_cli_reports_restart_needed(capsys, client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                           config: Config) -> None:
    original = HarnessClient.__init__
    transport = TestClientTransport(client)

    def init(self, base_url=None, api_key=None, **kwargs):
        kwargs["transport"] = transport
        original(self, "http://testserver", api_key, **kwargs)

    monkeypatch.setattr(HarnessClient, "__init__", init)
    code, _, err = run_cli(capsys, "config", "set", "browser.instances", "2")
    assert code == 0 and "needs a restart" in err
    code, out, _ = run_cli(capsys, "config", "show", "--json")
    data = json.loads(out)
    assert data["restartPending"] is True and "browser.instances" in data["restartPendingKeys"]


def test_spa_serves_symlinked_assets_and_blocks_traversal(client: TestClient, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "package-cache"
    (store / "assets").mkdir(parents=True)
    (store / "assets" / "index-abc.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("secret")
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    (dist / "assets" / "index-abc.js").symlink_to(store / "assets" / "index-abc.js")
    monkeypatch.setenv("EKS_HARNESS_WEB_DIST", str(dist))
    asset = client.get("/assets/index-abc.js")
    assert asset.status_code == 200 and asset.text == "console.log(1)"
    assert "immutable" in asset.headers["cache-control"]
    assert client.get("/p/acme/web/s/main/a/01ABC").text == "<!doctype html><div id=root></div>"
    assert client.get("/assets/missing.js").status_code == 404
    traversal = client.get("/assets/..%2f..%2fsecret.txt")
    assert traversal.status_code == 404 and traversal.text != "secret"
