from __future__ import annotations

from fastapi.testclient import TestClient

from eks_harness.daemon.app import create_app


def test_share_host_serves_only_shares(config) -> None:
    config.set("server.publicUrl", "https://hub.example.test")
    config.set("server.shareUrl", "https://share.example.test")
    config.set("server.allowedHosts", ["testserver"])
    app = create_app(config, config.paths, fake_pools=True)
    with TestClient(app) as client:
        assert app.state.ctx.links.share("tok") == "https://share.example.test/s/tok"
        assert app.state.ctx.links.raw("01J0000000000000000000000A", "a.png").startswith("https://hub.example.test/")
        share = {"Host": "share.example.test"}
        assert client.get("/api/health", headers=share).status_code == 200
        assert client.get("/api/projects", headers=share).status_code == 404
        assert client.get("/", headers=share).status_code == 404
        assert client.get("/s/unknown-token", headers=share).status_code in (200, 404, 410)
        assert client.get("/api/projects", headers={"Host": "hub.example.test"}).status_code == 200


def test_share_host_redirects_old_app_links(config) -> None:
    config.set("server.publicUrl", "https://hub.example.test")
    config.set("server.shareUrl", "https://share.example.test")
    app = create_app(config, config.paths, fake_pools=True)
    with TestClient(app) as client:
        moved = client.get("/p/acme/web/s/main?x=1", headers={"Host": "share.example.test"}, follow_redirects=False)
        assert moved.status_code == 308 and moved.headers["location"] == "https://hub.example.test/p/acme/web/s/main?x=1"
        raw = client.get("/raw/01J0000000000000000000000A/a.png", headers={"Host": "share.example.test"},
                         follow_redirects=False)
        assert raw.headers["location"].startswith("https://hub.example.test/raw/")
        assert client.post("/p/x", headers={"Host": "share.example.test"}).status_code == 404
