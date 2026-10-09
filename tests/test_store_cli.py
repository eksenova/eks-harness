from __future__ import annotations

import json
import zipfile

import httpx
import pytest

from eks_harness.cli import main
from eks_harness.cli import store_cmds
from eks_harness.cli.client import HarnessClient
from conftest import ensure_session
from test_store_support import add_lease, end_lease, png_bytes


class AppBridge(httpx.BaseTransport):
    def __init__(self, test_client) -> None:
        self.test_client = test_client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.test_client.request(request.method, str(request.url),
                                            headers=list(request.headers.multi_items()), content=request.read(),
                                            follow_redirects=False)
        return httpx.Response(response.status_code, headers=list(response.headers.multi_items()),
                              content=response.content, request=request)


@pytest.fixture
def cli(client, ctx, monkeypatch, capsys):
    def factory(args):
        return HarnessClient(base_url="http://testserver", paths=ctx.paths, config=ctx.config,
                             transport=AppBridge(client))

    monkeypatch.setattr(store_cmds, "client_from_args", factory)

    def run(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def run_json(cli, *argv: str):
    code, out, err = cli(*argv, "--json")
    assert code == 0, err
    return json.loads(out)


def test_upload_prints_links_and_json(cli, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(png_bytes())
    code, out, _ = cli("upload", "--project", "acme/web", "--session", "feature/x", "--caption", "Home",
                       "--tags", "a,b", str(shot))
    assert code == 0
    lines = out.strip().splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("http://127.0.0.1:7171/raw/") and lines[0].endswith("/shot.png")
    assert "/p/acme/web/s/feature-x/a/" in lines[1]
    assert lines[2] == "http://127.0.0.1:7171/p/acme/web/s/feature-x"
    item = run_json(cli, "upload", "--project", "acme/web", "--kind", "screenshot", "--meta",
                    '{"url": "http://localhost:3000"}', str(shot))
    assert item["kind"] == "screenshot" and item["meta"]["url"] == "http://localhost:3000"
    two = tmp_path / "two.txt"
    two.write_text("two")
    many = run_json(cli, "upload", "--project", "acme/web", str(shot), str(two))
    assert [a["filename"] for a in many] == ["shot.png", "two.txt"]
    code, _, err = cli("upload", str(shot))
    assert code == 2 and "--sid" in err
    code, _, err = cli("upload", "--project", "acme/web", str(tmp_path / "missing.png"))
    assert code == 2


def test_sid_upload_and_gone_exit_code(cli, ctx, tmp_path):
    session = ensure_session(ctx.db, "acme/mobile", "main")
    lease = add_lease(ctx.db, session.id)
    note = tmp_path / "device.log"
    note.write_text("log line")
    item = run_json(cli, "upload", "--sid", lease.sid, str(note))
    assert item["leaseSid"] == lease.sid and item["kind"] == "log"
    added = run_json(cli, "note", lease.sid, "Checked", "the", "flow")
    assert added["body"] == "Checked the flow"
    end_lease(ctx.db, lease)
    code, _, err = cli("upload", "--sid", lease.sid, str(note))
    assert code == 7
    assert "eks-harness lease acquire --project acme/mobile" in err


def test_artifact_commands(cli, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(png_bytes())
    first = run_json(cli, "upload", "--project", "acme/web", "--session", "s", str(shot))
    second = run_json(cli, "upload", "--project", "acme/web", "--session", "s", "--caption", "second one",
                      str(shot))
    listed = run_json(cli, "artifacts", "list", "--project", "acme/web")
    assert [a["id"] for a in listed] == [second["id"], first["id"]]
    code, out, _ = cli("artifacts", "list", "--project", "acme/web", "--session", "s")
    assert code == 0 and first["id"] in out
    shown = run_json(cli, "artifacts", "show", first["id"].lower())
    assert shown["id"] == first["id"] and shown["nextId"] is None
    code, out, _ = cli("artifacts", "show", first["id"])
    assert code == 0 and "Direct link" in out
    assert run_json(cli, "artifacts", "pin", first["id"])["pinned"] is True
    assert run_json(cli, "artifacts", "unpin", first["id"])["pinned"] is False
    tagged = run_json(cli, "artifacts", "tag", first["id"], second["id"], "--add", "evidence,pr")
    assert all(a["tags"] == ["evidence", "pr"] for a in tagged)
    assert run_json(cli, "artifacts", "caption", first["id"], "New", "caption")["caption"] == "New caption"
    assert run_json(cli, "artifacts", "seen", first["id"]) == {"updated": 1, "seen": True}
    unseen = run_json(cli, "artifacts", "list", "--unseen")
    assert [a["id"] for a in unseen] == [second["id"]]
    assert run_json(cli, "artifacts", "unseen", first["id"])["seen"] is False
    hits = run_json(cli, "search", "second")
    assert [h["artifact"]["id"] for h in hits["items"]] == [second["id"]]
    code, _, _ = cli("artifacts", "show", "01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert code == 6


def test_download_commands(cli, tmp_path):
    source = tmp_path / "in"
    source.mkdir()
    (source / "a.txt").write_text("alpha")
    (source / "b.txt").write_text("beta")
    a = run_json(cli, "upload", "--project", "acme/web", "--session", "feature/x", str(source / "a.txt"))
    b = run_json(cli, "upload", "--project", "acme/web", "--session", "feature/x", str(source / "b.txt"))
    out_dir = tmp_path / "out"
    downloaded = run_json(cli, "artifacts", "download", a["id"], b["id"], "-o", str(out_dir))
    assert sorted((out_dir / d["filename"]).read_text() for d in downloaded) == ["alpha", "beta"]
    single = tmp_path / "single.txt"
    run_json(cli, "artifacts", "download", a["id"], "-o", str(single))
    assert single.read_text() == "alpha"
    again = run_json(cli, "artifacts", "download", a["id"], "-o", str(out_dir))
    assert again[0]["path"].endswith("a (2).txt")
    session_dir = tmp_path / "session"
    by_session = run_json(cli, "artifacts", "download", "--session", "acme/web/feature/x", "-o", str(session_dir))
    assert len(by_session) == 2
    zipped = run_json(cli, "artifacts", "download", "--project", "acme/web", "--session", "feature/x", "--zip",
                      "-o", str(tmp_path))
    archive = zipfile.ZipFile(zipped[0]["path"])
    assert sorted(archive.namelist()) == ["feature-x/a.txt", "feature-x/b.txt"]
    code, _, _ = cli("artifacts", "download")
    assert code == 2


def test_delete_needs_confirmation_without_terminal(cli, tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("x")
    item = run_json(cli, "upload", "--project", "acme/web", str(path))
    code, _, err = cli("artifacts", "delete", item["id"])
    assert code == 2 and "--yes" in err
    summary = run_json(cli, "artifacts", "delete", item["id"], "--yes")
    assert summary["deleted"] is True and summary["artifacts"] == 1
    code, _, _ = cli("artifacts", "show", item["id"])
    assert code == 6


def test_site_upload_directory_and_zip(cli, tmp_path):
    site = tmp_path / "coverage"
    (site / "css").mkdir(parents=True)
    (site / "index.html").write_text("<p>cov</p>")
    (site / "css" / "a.css").write_text("a{}")
    (site / ".DS_Store").write_bytes(b"junk")
    code, out, _ = cli("site", "upload", "--project", "acme/web", "--session", "s", str(site))
    assert code == 0
    lines = out.strip().splitlines()
    assert lines[0].endswith("/index.html") and "/site/" in lines[0]
    item = run_json(cli, "site", "upload", "--project", "acme/web", str(site), "--caption", "Coverage")
    assert item["filename"] == "coverage.zip"
    assert sorted(item["meta"]["site"]["files"]) == ["css/a.css", "index.html"]
    archive = tmp_path / "report.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("main.html", "<p>main</p>")
    zipped = run_json(cli, "site", "upload", "--project", "acme/web", "--entry", "main.html", str(archive))
    assert zipped["siteUrl"].endswith("/main.html")
    code, _, err = cli("site", "upload", "--project", "acme/web", str(tmp_path / "nope"))
    assert code == 2


def test_share_commands(cli, tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("x")
    item = run_json(cli, "upload", "--project", "acme/web", str(path))
    code, out, _ = cli("share", "create", item["id"], "--expires", "7d")
    assert code == 0
    direct, url = out.strip().splitlines()
    assert url.startswith("http://127.0.0.1:7171/s/")
    assert direct == url.replace("/s/", "/d/") + "/a.txt"
    code, out, _ = cli("share", "create", item["id"], "--direct")
    assert code == 0 and out.strip().startswith("http://127.0.0.1:7171/d/") and len(out.strip().splitlines()) == 1
    shares = run_json(cli, "share", "list", item["id"])
    assert len(shares) == 2 and shares[1]["expiresAt"]
    assert shares[1]["directUrl"] == direct
    revoked = run_json(cli, "share", "revoke", shares[1]["token"])
    assert revoked["active"] is False


def test_project_and_session_commands(cli, ctx, tmp_path):
    created = run_json(cli, "projects", "create", "acme/docs", "--title", "Docs", "--retention-days", "30")
    assert created["retentionDays"] == 30
    edited = run_json(cli, "projects", "edit", "acme/docs", "--no-retention", "--description", "d")
    assert edited["retentionDays"] == 0 and edited["retentionSource"] == "forever" and edited["description"] == "d"
    inherited = run_json(cli, "projects", "edit", "acme/docs", "--inherit-retention")
    assert inherited["retentionDays"] is None and inherited["retentionSource"] == "global"
    code, out, _ = cli("projects", "show", "acme/docs")
    assert code == 0 and "keep forever (global default)" in out
    code, _, err = cli("projects", "edit", "acme/docs", "--retention-days", "0")
    assert code == 2 and "--keep-forever" in err
    code, _, _ = cli("projects", "edit", "acme/docs")
    assert code == 2
    path = tmp_path / "a.txt"
    path.write_text("x")
    run_json(cli, "upload", "--project", "acme/docs", "--session", "feature/Guide", str(path))
    projects = run_json(cli, "projects", "list")
    assert [p["id"] for p in projects] == ["acme/docs"]
    code, out, _ = cli("projects", "list")
    assert code == 0 and "acme/docs" in out
    assert run_json(cli, "projects", "show", "acme/docs")["artifactCount"] == 1
    sessions = run_json(cli, "sessions", "list", "acme/docs")
    assert [s["slug"] for s in sessions] == ["feature-guide"]
    assert run_json(cli, "sessions", "show", "acme/docs", "feature/Guide")["name"] == "feature/Guide"
    renamed = run_json(cli, "sessions", "rename", "acme/docs", "feature-guide", "Guide v2")
    assert renamed["slug"] == "guide-v2"
    timeline = run_json(cli, "timeline", "acme/docs", "guide-v2")
    assert [e["type"] for e in timeline["items"]] == ["artifact", "event"]
    assert timeline["items"][1]["event"]["type"] == "session.updated"
    code, out, _ = cli("timeline", "acme/docs", "guide-v2")
    assert code == 0 and "a.txt" in out
    deleted = run_json(cli, "sessions", "delete", "acme/docs", "guide-v2", "--yes")
    assert deleted["artifacts"] == 1 and deleted["sessions"] == 1
    gone = run_json(cli, "projects", "delete", "acme/docs", "--yes")
    assert gone["deleted"] is True
    code, _, _ = cli("projects", "show", "acme/docs")
    assert code == 6
    code, _, _ = cli("projects", "show", "not-a-project")
    assert code == 2


def test_sessions_across_projects(cli, tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("x")
    for project in ("acme/web", "acme/mobile"):
        run_json(cli, "upload", "--project", project, "--session", "feature/shared", str(path))
    listed = run_json(cli, "sessions", "list")
    assert [(s["slug"], s["projectIds"]) for s in listed] == [("feature-shared", ["acme/mobile", "acme/web"])]
    code, out, _ = cli("sessions", "list")
    assert code == 0 and "mobile, web" in out
    shown = run_json(cli, "sessions", "show", "feature/shared")
    assert shown["artifactCount"] == 2 and shown["projectId"] is None
    assert run_json(cli, "sessions", "show", "acme/web", "feature-shared")["artifactCount"] == 1
    assert len(run_json(cli, "timeline", "feature-shared", "--types", "artifact")["items"]) == 2
    assert run_json(cli, "sessions", "rename", "feature-shared", "Shared v2")["slug"] == "shared-v2"
    partial = run_json(cli, "sessions", "delete", "acme/web", "shared-v2", "--yes")
    assert partial["artifacts"] == 1 and partial["sessions"] == 0
    whole = run_json(cli, "sessions", "delete", "shared-v2", "--yes")
    assert whole["artifacts"] == 1 and whole["sessions"] == 1


def test_artifact_retention_and_tag_color_commands(cli, ctx, tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("x")
    first = run_json(cli, "upload", "--project", "acme/web", "--tags", "smoke", str(path))
    set_days = run_json(cli, "artifacts", "retention", first["id"], "--days", "9")
    assert set_days[0]["retentionDays"] == 9
    code, out, _ = cli("artifacts", "show", first["id"])
    assert code == 0 and "9 days (this artifact" in out
    cleared = run_json(cli, "artifacts", "retention", first["id"], "--inherit")
    assert cleared[0]["retentionDays"] is None
    code, _, _ = cli("artifacts", "retention", first["id"])
    assert code == 2
    colored = run_json(cli, "tags", "color", "smoke", "#123abc")
    assert colored["color"] == "#123abc"
    listed = {t["tag"]: t for t in run_json(cli, "tags", "list")}
    assert listed["smoke"]["color"] == "#123abc" and listed["web"]["builtin"]
    assert run_json(cli, "tags", "color", "smoke", "--reset")["color"] is None
    code, _, _ = cli("tags", "color", "smoke")
    assert code == 2
