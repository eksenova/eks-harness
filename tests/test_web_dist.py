from __future__ import annotations

from eks_harness.daemon import app as daemon_app


def test_dangling_web_ui_symlink_is_reported_as_a_cleaned_uv_cache(tmp_path, monkeypatch) -> None:
    dist = tmp_path / "web_dist"
    dist.mkdir()
    (dist / "index.html").symlink_to(tmp_path / "cache" / "index.html")
    monkeypatch.delenv("EKS_HARNESS_WEB_DIST", raising=False)
    monkeypatch.setattr(daemon_app, "packaged_web_dist", lambda: dist)
    assert daemon_app.web_dist_dir() is None
    assert "uv cache that has since been cleaned" in daemon_app.web_dist_missing_message()


def test_missing_web_ui_asks_for_a_build(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EKS_HARNESS_WEB_DIST", raising=False)
    monkeypatch.setattr(daemon_app, "packaged_web_dist", lambda: tmp_path / "web_dist")
    assert daemon_app.web_dist_dir() is None
    assert "not built into this installation" in daemon_app.web_dist_missing_message()
