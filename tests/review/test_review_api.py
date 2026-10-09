from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from eks_harness.cli import main, review_cmds
from eks_harness.cli.client import HarnessClient

sys.path.insert(0, str(Path(__file__).parent))
import synth  # noqa: E402

pytestmark = pytest.mark.skipif(not synth.have_ffmpeg(), reason="ffmpeg/ffprobe not on PATH")


class AppBridge(httpx.BaseTransport):
    def __init__(self, test_client) -> None:
        self.test_client = test_client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.test_client.request(request.method, str(request.url),
                                            headers=list(request.headers.multi_items()), content=request.read(),
                                            follow_redirects=False)
        return httpx.Response(response.status_code, headers=list(response.headers.multi_items()),
                              content=response.content, request=request)


@pytest.fixture(scope="module")
def clip_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    return synth.build(tmp_path_factory.mktemp("api") / "synth.mp4").read_bytes()


@pytest.fixture
def video_id(client, clip_bytes) -> str:
    response = client.post("/api/artifacts", data={"project": "acme/ads", "session": "render-1", "kind": "video"},
                           files={"file": ("ad.mp4", clip_bytes, "video/mp4")})
    assert response.status_code == 201, response.text
    return response.json()["id"]


EXPECTATIONS = {"tolerance_frames": 2, "checks": [
    {"t": 2.0, "kind": "cut", "label": "scene 2"},
    {"t": synth.STAMP_AT, "kind": "visual", "label": "stamp",
     "params": {"color": "#d32f2f", "box": synth.STAMP_BOX}},
    {"kind": "black", "params": {"max_frames": 2}},
]}


def test_sheet_is_stored_next_to_the_video(client, video_id):
    body = {"markers": {"beats": synth.BEATS, "cues": [{"t": synth.STAMP_AT, "label": "stamp"}]}, "frames": 16}
    response = client.post(f"/api/artifacts/{video_id}/sheet", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["info"]["frames"] == 240
    [sheet] = data["sheets"]
    assert "/p/acme/ads/s/render-1/a/" in sheet["url"] and "/raw/" not in sheet["url"]
    assert sheet["kind"] == "screenshot" and sheet["mime"] == "image/jpeg"
    assert any(t.get("cue") == "stamp" for t in sheet["tiles"])
    stored = client.get(f"/api/artifacts/{sheet['id']}").json()
    assert stored["meta"]["role"] == "sheet" and stored["meta"]["video"] == video_id
    assert "sheet" in stored["tags"]
    assert client.get(f"/api/artifacts/{video_id}").json()["meta"]["sheets"] == [sheet["id"]]
    again = client.post(f"/api/artifacts/{video_id}/sheet", json={"frames": 12}).json()
    assert client.get(f"/api/artifacts/{sheet['id']}").status_code == 404
    kept = client.post(f"/api/artifacts/{video_id}/sheet", json={"frames": 12, "replace": False}).json()
    assert client.get(f"/api/artifacts/{again['sheets'][0]['id']}").status_code == 200
    assert client.get(f"/api/artifacts/{video_id}").json()["meta"]["sheets"] == [kept["sheets"][0]["id"]]


def test_checks_store_a_report_and_link_it(client, video_id):
    sheet = client.post(f"/api/artifacts/{video_id}/sheet", json={"frames": 12}).json()["sheets"][0]
    response = client.post(f"/api/artifacts/{video_id}/checks", json={"expectations": EXPECTATIONS})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"] is False
    statuses = [r["status"] for r in data["report"]["results"]]
    assert statuses == ["pass", "pass", "fail"]
    assert data["text"].startswith("FAIL ad.mp4")
    report = data["artifact"]
    assert report["kind"] == "log" and report["filename"] == "ad-checks.json"
    assert report["meta"]["ok"] is False and report["meta"]["video"] == video_id
    assert "fail" in report["tags"]
    video = client.get(f"/api/artifacts/{video_id}").json()
    assert video["meta"]["checks"] == report["id"] and video["meta"]["checksOk"] is False
    assert client.get(f"/api/artifacts/{sheet['id']}").json()["meta"]["checks"] == report["id"]
    raw = client.get(report["rawUrl"].split("testserver", 1)[-1]).json()
    assert raw["videoArtifact"] == video_id and raw["results"][0]["deltaFrames"] == 0


def test_review_rejects_other_kinds(client, video_id):
    other = client.post("/api/artifacts", data={"project": "acme/ads", "kind": "log"},
                        files={"file": ("a.txt", b"hello", "text/plain")}).json()
    response = client.post(f"/api/artifacts/{other['id']}/sheet", json={})
    assert response.status_code == 400 and response.json()["error"] == "not_video"
    bad = client.post(f"/api/artifacts/{video_id}/checks", json={"expectations": {"nope": 1}})
    assert bad.status_code == 400 and bad.json()["error"] == "bad_expectations"
    assert "cut" in client.get("/api/review/checks").json()["items"]


@pytest.fixture
def cli(client, ctx, monkeypatch, capsys):
    def factory(args):
        return HarnessClient(base_url="http://testserver", paths=ctx.paths, config=ctx.config,
                             transport=AppBridge(client))

    monkeypatch.setattr(review_cmds, "client_from_args", factory)

    def run(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def test_cli_sheet_and_check_by_artifact(cli, video_id, tmp_path):
    markers = tmp_path / "markers.json"
    markers.write_text(json.dumps({"cues": [{"t": 2.0, "label": "cut"}]}))
    code, out, err = cli("video", "sheet", video_id, "--markers", str(markers), "--frames", "12",
                         "--out", str(tmp_path / "sheets"))
    assert code == 0, err
    assert "page: http://" in out and "/a/" in out
    local = next(line.split("local: ", 1)[1] for line in out.splitlines() if "local: " in line)
    assert Path(local).is_file()
    expectations = tmp_path / "exp.json"
    expectations.write_text(json.dumps(EXPECTATIONS))
    code, out, err = cli("video", "check", video_id, str(expectations))
    assert code == 1
    assert out.startswith("FAIL ad.mp4") and "report: http://" in out
    passing = tmp_path / "pass.json"
    passing.write_text(json.dumps({"checks": EXPECTATIONS["checks"][:2]}))
    code, out, err = cli("video", "check", video_id, str(passing), "--json")
    assert code == 0 and json.loads(out)["ok"] is True


def test_cli_local_file(cli, tmp_path, clip_bytes):
    video = tmp_path / "local.mp4"
    video.write_bytes(clip_bytes)
    expectations = tmp_path / "exp.json"
    expectations.write_text(json.dumps({"checks": EXPECTATIONS["checks"][:2]}))
    code, out, err = cli("video", "check", str(video), str(expectations), "--local")
    assert code == 0 and out.startswith("PASS local.mp4")
    code, out, err = cli("video", "sheet", str(video), "--local", "--out", str(tmp_path / "s"), "--at", "1.5:mark")
    assert code == 0 and (tmp_path / "s" / "local-sheet.jpg").is_file()
    code, out, err = cli("video", "check", str(video), str(expectations), "--project", "acme/ads",
                         "--session", "local", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["ok"] is True and "/p/acme/ads/s/local/a/" in data["artifact"]["url"]
