from __future__ import annotations

import io
import json
import struct
import zlib

import pytest

from eks_harness.store import artifacts as store_artifacts

SPEC = {
    "version": 1,
    "items": [
        {"id": "first", "type": "step", "anchor": {"kind": "coords", "x": 5, "y": 5, "width": 20, "height": 10}},
        {"id": "note", "type": "callout", "anchor": {"kind": "text", "text": "Hi"}, "text": "note here"},
    ],
}

YAML_SPEC = "version: 1\nitems:\n- id: yaml-step\n  type: label\n  anchor: {kind: text, text: Hi}\n  text: yaml tag\n"


def png_bytes(width: int = 40, height: int = 30) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xc8\x1e\x1e" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def shot(ctx, config, project: str = "acme/web", session: str = "s"):
    return store_artifacts.ingest_file(
        ctx.db, config, project=project, session=session, path_or_stream=io.BytesIO(png_bytes()), kind="screenshot",
        filename="shot.png", mime="image/png", source="cli")


def local_path(url: str) -> str:
    return "/" + url.split("://", 1)[1].split("/", 1)[1]


def test_annotate_render_stores_version_and_crops(client, ctx, config):
    artifact = shot(ctx, config)
    response = client.post(f"/api/artifacts/{artifact.id}/annotate", json={"spec": SPEC})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 1 and body["kind"] == "initial"
    assert body["validation"]["ok"] is True
    assert body["validation"]["anchorFallback"] is True
    assert [c["itemId"] for c in body["crops"]] == ["first", "note"]
    assert len(body["validation"]["rules"]) == 9
    assert body["artifact"]["meta"]["annotation"]["cleanId"] == artifact.id
    assert body["artifact"]["meta"]["annotation"]["spec"] == SPEC

    versions = client.get(f"/api/artifacts/{artifact.id}/annotations").json()["versions"]
    assert [(v["version"], v["kind"]) for v in versions] == [(1, "initial")]

    for crop in body["crops"]:
        answer = client.get(local_path(crop["url"]))
        assert answer.status_code == 200
        assert answer.headers["content-type"] == "image/png"
        assert answer.content.startswith(b"\x89PNG")
    answer = client.get(f"/api/artifacts/{artifact.id}/versions/1/file")
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "image/png"


def test_annotate_yaml_string_spec_and_no_fallback(client, ctx, config):
    artifact = shot(ctx, config)
    response = client.post(f"/api/artifacts/{artifact.id}/annotate", json={"spec": YAML_SPEC, "style": "review"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 1
    assert body["validation"]["anchorFallback"] is False
    assert body["artifact"]["meta"]["annotation"]["styleName"] == "review"


def test_annotate_dry_run_stores_no_version(client, ctx, config):
    artifact = shot(ctx, config)
    response = client.post(f"/api/artifacts/{artifact.id}/annotate", json={"spec": SPEC, "dryRun": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 0 and body["kind"] == "dryRun"
    assert body["validation"]["ok"] is True
    assert len(body["crops"]) == 2
    assert client.get(f"/api/artifacts/{artifact.id}/annotations").json()["versions"] == []


@pytest.mark.parametrize("spec", [
    {"version": 2, "items": [{"type": "step", "anchor": {"kind": "text", "text": "x"}}]},
    {"version": 1, "items": []},
    {"version": 1, "items": [{"type": "nope"}]},
    {"version": 1, "items": [{"type": "step"}], "bogus": 1},
    {"version": 1, "items": [{"type": "title", "text": "cards are video only"}]},
    {"version": 1, "items": [{"type": "callout", "anchor": {"kind": "text", "text": "x"}, "text": "t",
                              "start": 1.0}]},
])
def test_annotate_invalid_spec_is_422(client, ctx, config, spec):
    artifact = shot(ctx, config)
    response = client.post(f"/api/artifacts/{artifact.id}/annotate", json={"spec": spec})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "annotation_invalid"
    assert body["validation"]["ok"] is False
    assert client.get(f"/api/artifacts/{artifact.id}/annotations").json()["versions"] == []


def test_annotate_rerender_recapture_restore(client, ctx, config):
    artifact = shot(ctx, config)
    assert client.post(f"/api/artifacts/{artifact.id}/annotate", json={"spec": SPEC}).status_code == 200

    rerender = client.post(f"/api/artifacts/{artifact.id}/annotate/rerender", json={"style": "review"})
    assert rerender.status_code == 200, rerender.text
    assert rerender.json()["version"] == 2 and rerender.json()["kind"] == "rerender"
    assert rerender.json()["artifact"]["meta"]["annotation"]["styleName"] == "review"

    recapture = client.post(f"/api/artifacts/{artifact.id}/annotate/recapture", json={})
    assert recapture.status_code == 200, recapture.text
    assert recapture.json()["version"] == 3 and recapture.json()["kind"] == "recapture"

    restore = client.post(f"/api/artifacts/{artifact.id}/annotate/restore", json={"version": 1})
    assert restore.status_code == 200, restore.text
    assert restore.json()["version"] == 4

    versions = client.get(f"/api/artifacts/{artifact.id}/annotations").json()["versions"]
    assert [(v["version"], v["kind"]) for v in versions] == [
        (1, "initial"), (2, "rerender"), (3, "recapture"), (4, "restore")]
    assert all(v["url"] for v in versions)

    missing = client.post(f"/api/artifacts/{artifact.id}/annotate/restore", json={"version": 99})
    assert missing.status_code == 404
    assert missing.json()["error"] == "version_not_found"


def test_rerender_without_annotation_is_rejected(client, ctx, config):
    artifact = shot(ctx, config)
    response = client.post(f"/api/artifacts/{artifact.id}/annotate/rerender", json={})
    assert response.status_code == 400
    response = client.post(f"/api/artifacts/{artifact.id}/annotate/recapture", json={})
    assert response.status_code == 400


def test_assets_add_list_get_remove(client, ctx, config):
    add = client.post("/api/projects/acme/web/assets", files={"file": ("icon.png", png_bytes(8, 8), "image/png")})
    assert add.status_code == 200, add.text
    assert add.json()["name"] == "icon"

    named = client.post("/api/projects/acme/web/assets", data={"name": "hero"},
                        files={"file": ("hero.png", png_bytes(8, 8), "image/png")})
    assert named.status_code == 200, named.text
    assert named.json()["name"] == "hero"

    listed = client.get("/api/projects/acme/web/assets").json()["items"]
    assert [a["name"] for a in listed] == ["hero", "icon"]
    answer = client.get(local_path(listed[0]["url"]))
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "image/png"

    bad = client.post("/api/projects/acme/web/assets", files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_asset"

    remove = client.delete("/api/projects/acme/web/assets/icon")
    assert remove.status_code == 200
    assert [a["name"] for a in client.get("/api/projects/acme/web/assets").json()["items"]] == ["hero"]
    assert client.delete("/api/projects/acme/web/assets/icon").status_code == 404


def test_capture_request_accepts_pacing_fields():
    from eks_harness.api.schemas import CaptureRequest

    body = CaptureRequest.model_validate({"caption": "x", "pace": "demo", "fromHere": True, "noPointer": True,
                                          "hide": [".ad"]})
    assert body.pace == "demo"
    assert body.from_here is True and body.no_pointer is True and body.hide == [".ad"]
    assert CaptureRequest().pace is None


def test_capture_cli_pacing_flags():
    from eks_harness.cli import build_parser
    from eks_harness.cli import lease_cmds

    args = build_parser().parse_args(["capture", "screenshot", "--sid", "abcdef", "--pace", "demo", "--from-here",
                                      "--no-pointer", "--hide", ".ad", "--hide", ".toast"])
    assert args.pace == "demo" and args.from_here and args.no_pointer
    assert args.hide == [".ad", ".toast"]
    body = lease_cmds.capture_body(args)
    assert body["pace"] == "demo" and body["fromHere"] is True and body["noPointer"] is True
    assert body["hide"] == [".ad", ".toast"]

    plain = build_parser().parse_args(["capture", "screenshot", "--sid", "abcdef"])
    body = lease_cmds.capture_body(plain)
    assert "pace" not in body and "fromHere" not in body and "hide" not in body


@pytest.fixture
def annotate_cli(client, ctx, monkeypatch, capsys):
    from conftest import harness_client
    from eks_harness.cli import annotate_cmds, main

    def factory(args):
        return harness_client(client, ctx.config)

    monkeypatch.setattr(annotate_cmds, "client_from_args", factory)

    def run(*argv: str):
        capsys.readouterr()
        code = main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def run_json(cli, *argv: str):
    code, out, err = cli(*argv, "--json")
    assert code == 0, err
    return json.loads(out)


def test_cli_annotate_versions_and_restore(annotate_cli, ctx, config, tmp_path):
    artifact = shot(ctx, config)
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(SPEC), encoding="utf-8")

    rendered = run_json(annotate_cli, "annotate", artifact.id, "--spec", str(spec_file))
    assert rendered["version"] == 1 and rendered["validation"]["ok"] is True

    yaml_file = tmp_path / "spec.yaml"
    yaml_file.write_text(YAML_SPEC, encoding="utf-8")
    dry = run_json(annotate_cli, "annotate", artifact.id, "--spec", str(yaml_file), "--dry-run")
    assert dry["version"] == 0

    versions = run_json(annotate_cli, "annotate", "versions", artifact.id)
    assert [v["version"] for v in versions] == [1]

    rerendered = run_json(annotate_cli, "annotate", "rerender", artifact.id, "--style", "review")
    assert rerendered["version"] == 2

    recaptured = run_json(annotate_cli, "annotate", "recapture", artifact.id)
    assert recaptured["version"] == 3

    restored = run_json(annotate_cli, "annotate", "restore", artifact.id, "1")
    assert restored["version"] == 4

    code, out, _ = annotate_cli("annotate", "versions", artifact.id)
    assert code == 0 and "restore" in out

    code, _, err = annotate_cli("annotate")
    assert code == 2
    code, _, err = annotate_cli("annotate", artifact.id)
    assert code == 2 and "--spec" in err


def test_cli_assets_add_list_remove(annotate_cli, tmp_path):
    icon = tmp_path / "icon.png"
    icon.write_bytes(png_bytes(8, 8))
    added = run_json(annotate_cli, "assets", "add", "acme/web", str(icon), "--name", "icon")
    assert added["name"] == "icon"
    listed = run_json(annotate_cli, "assets", "list", "acme/web")
    assert [a["name"] for a in listed] == ["icon"]
    removed = run_json(annotate_cli, "assets", "rm", "acme/web", "icon")
    assert removed["deleted"] is True
    assert run_json(annotate_cli, "assets", "list", "acme/web") == []


def test_mcp_annotate_tools(client, ctx, config):
    pytest.importorskip("mcp.server.mcpserver")
    from conftest import harness_client
    from eks_harness.mcp_server import HarnessTools, create_server

    tools = HarnessTools(lambda: harness_client(client, config))
    try:
        rendered = tools.annotate(shot(ctx, config).id, SPEC, None, False)
        assert rendered["version"] == 1
        assert [c["itemId"] for c in rendered["crops"]] == ["first", "note"]

        dry = tools.annotate(rendered["artifact"]["id"], SPEC, None, True)
        assert dry["version"] == 0

        rerendered = tools.annotate_rerender(rendered["artifact"]["id"], "review")
        assert rerendered["version"] == 2

        recaptured = tools.annotate_recapture(rendered["artifact"]["id"], None)
        assert recaptured["version"] == 3

        listed = tools.list_annotation_versions(rendered["artifact"]["id"])
        assert [v["version"] for v in listed["versions"]] == [1, 2, 3]

        restored = tools.restore_annotation_version(rendered["artifact"]["id"], 1)
        assert restored["version"] == 4
    finally:
        tools.close()

    for name in ("annotate", "annotate_rerender", "annotate_recapture", "list_annotation_versions",
                 "restore_annotation_version"):
        assert callable(getattr(HarnessTools, name))
    server, owned = create_server(lambda: harness_client(client, config))
    owned.close()
    assert server is not None


def test_annotate_tables_survive_migration_discovery(ctx):
    from eks_harness.db import migrations

    versions = [m.version for m in migrations.discover()]
    assert versions[0] == 1
    assert versions == sorted(versions)
