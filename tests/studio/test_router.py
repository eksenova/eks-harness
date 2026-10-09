"""The studio router mounted on the harness app: workspace roots, auth, file routes and the WebSocket."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from eks_harness.config import Config
from eks_harness.studio.roots import get_roots_manager
from eks_harness.studio.router import studio_router, workspace_root

from conftest import create_key, create_user, make_app


def _mounted(config: Config):
    app = make_app(config)
    app.include_router(studio_router(app.state.ctx), prefix="/api/studio")
    included = app.router.routes.pop()
    fallback = next(i for i, r in enumerate(app.router.routes) if getattr(r, "path", "") == "/{path:path}")
    app.router.routes.insert(fallback, included)
    return app


def test_workspace_comes_from_config(config: Config, tmp_path: Path) -> None:
    app = make_app(config)
    assert workspace_root(app.state.ctx) == (config.paths.data_dir / "video").resolve()
    config.set("video.workspace", str(tmp_path / "videos"))
    config.set("video.mediaLibrary", str(tmp_path / "media"))
    studio_router(app.state.ctx)
    roots = get_roots_manager()
    assert roots.project_workspace() == (tmp_path / "videos").resolve()
    assert roots.media_library() == (tmp_path / "media").resolve()


def test_files_and_websocket_without_auth(config: Config, tmp_path: Path) -> None:
    config.set("video.workspace", str(tmp_path / "videos"))
    project = tmp_path / "videos" / "promo"
    (project / "renders" / "j1").mkdir(parents=True)
    (project / "renders" / "j1" / "out.mp4").write_bytes(b"mp4")
    with TestClient(_mounted(config)) as client:
        response = client.get("/api/studio/files/projects/promo/renders/j1/out.mp4")
        assert response.status_code == 200 and response.content == b"mp4"
        assert client.get("/api/studio/files/projects/promo/../../etc/passwd").status_code == 404
        with client.websocket_connect("/api/studio/ws") as socket:
            hello = json.loads(socket.receive_text())
            assert hello["type"] == "hello"
            assert hello["payload"]["server"] == "eks-harness-studio"
            assert hello["payload"]["project_workspace"] == str((tmp_path / "videos").resolve())


def test_studio_needs_an_admin_when_auth_is_on(auth_config: Config, tmp_path: Path) -> None:
    auth_config.set("video.workspace", str(tmp_path / "videos"))
    (tmp_path / "videos" / "promo").mkdir(parents=True)
    (tmp_path / "videos" / "promo" / "project.py").write_text("project = None\n")
    app = _mounted(auth_config)
    db = app.state.ctx.db
    admin_key = create_key(db, create_user(db, "boss", "admin").id)
    member_key = create_key(db, create_user(db, "dev", "member").id)
    url = "/api/studio/files/projects/promo/project.py"
    with TestClient(app) as client:
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"Authorization": f"Bearer {member_key}"}).status_code == 403
        assert client.get(url, headers={"Authorization": f"Bearer {admin_key}"}).status_code == 200
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/studio/ws") as socket:
            socket.receive_text()
        with client.websocket_connect("/api/studio/ws", headers={"Authorization": f"Bearer {admin_key}"}) as socket:
            assert json.loads(socket.receive_text())["type"] == "hello"
