from __future__ import annotations

import json
import subprocess
import time

import pytest

from eks_harness import selfupdate
from eks_harness.daemon import updater as updater_module


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "first")
    first = _git(work, "rev-parse", "HEAD")
    _git(work, "tag", "v1")
    _git(work, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "second")
    second = _git(work, "rev-parse", "HEAD")
    return {"url": work.as_uri(), "first": first, "second": second}


def _installed(commit, source="git", extras=("video",)):
    return selfupdate.Installed(version="0.1.0", commit=commit, dirty=False, source=source,
                                location="git+https://example/x.git", extras=list(extras), receipt="/r")


def test_remote_commit_resolves_branches_tags_and_shas(remote):
    assert selfupdate.remote_commit(remote["url"], "main") == remote["second"]
    assert selfupdate.remote_commit(remote["url"], "v1") == remote["first"]
    assert selfupdate.remote_commit(remote["url"], remote["first"]) == remote["first"]
    with pytest.raises(selfupdate.UpdateError, match="no branch or tag"):
        selfupdate.remote_commit(remote["url"], "nope")


def test_check_compares_the_installed_commit(remote, monkeypatch):
    monkeypatch.setattr(selfupdate, "installed", lambda: _installed(remote["first"]))
    status = selfupdate.check(remote["url"], "main", with_changes=False)
    assert status.available and status.latest == remote["second"]
    monkeypatch.setattr(selfupdate, "installed", lambda: _installed(remote["second"]))
    assert not selfupdate.check(remote["url"], "main").available
    monkeypatch.setattr(selfupdate, "installed", lambda: _installed(remote["second"], "directory"))
    local = selfupdate.check(remote["url"], "main")
    assert not local.available and "switches the source" in local.reason
    monkeypatch.setattr(selfupdate, "installed", lambda: _installed(remote["first"], "editable"))
    assert not selfupdate.check(remote["url"], "main").available


def test_requirement_pins_the_commit_and_keeps_extras():
    sha = "a" * 40
    assert selfupdate.requirement("https://github.com/o/r.git", sha, ["video", "html"]) == \
        f"eks-harness[video,html] @ git+https://github.com/o/r.git@{sha}"
    assert selfupdate.requirement("git+ssh://git@github.com/o/r.git", sha, []) == \
        f"eks-harness @ git+ssh://git@github.com/o/r.git@{sha}"


def test_receipt_and_direct_url_are_read(tmp_path, monkeypatch):
    (tmp_path / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "eks-harness", extras = ["video", "html"], git = "https://x" }]\n')
    dist = tmp_path / "lib" / "python3.12" / "site-packages" / "eks_harness-0.1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "direct_url.json").write_text(json.dumps({"url": "https://x", "vcs_info": {"vcs": "git", "commit_id": "c" * 40}}))
    monkeypatch.setattr(selfupdate, "tool_dir", lambda: tmp_path)
    assert selfupdate._requirement(selfupdate.receipt_path())["extras"] == ["video", "html"]
    assert selfupdate.installed_commit_on_disk() == "c" * 40


def test_install_fails_when_uv_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(selfupdate, "install_command", lambda *a: ["sh", "-c", "echo boom; exit 3"])
    with pytest.raises(selfupdate.UpdateError, match="exit 3.*boom"):
        selfupdate.install("https://x", "a" * 40, [])


def test_update_lock_is_exclusive(paths):
    with selfupdate.update_lock(paths), pytest.raises(selfupdate.UpdateError, match="another update"), \
            selfupdate.update_lock(paths):
        pass


class Fake:
    def __init__(self, ctx, monkeypatch, latest="b" * 40, current="a" * 40, source="git"):
        self.installs = []
        self.restarts = 0
        status = selfupdate.Status(_installed(current, source), "https://x", "main", latest, latest != current,
                                   "a -> b")
        monkeypatch.setattr(selfupdate, "check", lambda *a, **k: status)
        monkeypatch.setattr(selfupdate, "installed", lambda: _installed(current, source))
        monkeypatch.setattr(selfupdate, "install", lambda repo, commit, extras, output=None: self.installs.append(
            (repo, commit, tuple(extras))) or commit)
        ctx.managed = True
        ctx.restart_handler = self.restart
        monkeypatch.setattr(type(ctx.pools), "fake", property(lambda self: False), raising=False)

    def restart(self):
        self.restarts += 1


@pytest.fixture
def service(ctx, monkeypatch):
    monkeypatch.setattr(updater_module.renderq, "status", lambda: {"items": []})
    return updater_module.Updater(ctx)


def test_auto_update_installs_and_restarts_when_idle(ctx, service, monkeypatch):
    fake = Fake(ctx, monkeypatch)
    service.tick()
    assert fake.installs == [("https://x", "b" * 40, ("video",))] and fake.restarts == 1
    saved = selfupdate.read_state(ctx.paths)
    assert saved["lastUpdate"]["to"] == "b" * 40 and saved["lastUpdate"]["by"] == "daemon"
    assert service.state == "restarting"


def test_auto_update_waits_while_busy(ctx, service, monkeypatch):
    fake = Fake(ctx, monkeypatch)
    monkeypatch.setattr(service, "busy_reasons", lambda: ["1 live lease"])
    service.tick()
    assert not fake.installs and service.state == "waiting" and "1 live lease" in service.message


def test_auto_update_skips_local_checkouts_and_failed_commits(ctx, service, monkeypatch):
    fake = Fake(ctx, monkeypatch, source="directory")
    service.tick()
    assert not fake.installs and "local checkout" in service.message
    ctx.config.set("update.replaceLocal", True)
    selfupdate.write_state(ctx.paths, failed={"commit": "b" * 40, "error": "x", "at": time.time()})
    service.last_check = 0
    service.tick()
    assert not fake.installs
    service.request_apply()
    service.tick()
    assert len(fake.installs) == 1


def test_auto_update_is_off_when_disabled(ctx, service, monkeypatch):
    fake = Fake(ctx, monkeypatch)
    ctx.config.set("update.auto", False)
    service.tick()
    assert not fake.installs
    service.request_apply()
    service.tick()
    assert len(fake.installs) == 1


def test_failed_install_is_remembered(ctx, service, monkeypatch):
    fake = Fake(ctx, monkeypatch)

    def broken(*a, **k):
        raise selfupdate.UpdateError("uv tool install failed")

    monkeypatch.setattr(selfupdate, "install", broken)
    service.tick()
    assert service.state == "error" and fake.restarts == 0
    assert selfupdate.read_state(ctx.paths)["failed"]["commit"] == "b" * 40


def test_busy_reasons_count_leases_and_jobs(ctx, service, db):
    from conftest import ensure_session

    assert service.busy_reasons() == []
    ensure_session(db, "acme/x", "s")
    with db.transaction() as conn:
        conn.execute("INSERT INTO node_jobs (id, kind, state, created_at) VALUES ('j1', 'render', 'running', 0)")
    assert service.busy_reasons() == ["1 node job active"]


def test_update_api(client, ctx, monkeypatch):
    Fake(ctx, monkeypatch)
    data = client.get("/api/update").json()
    assert data["installed"]["commit"] == "a" * 40 and data["auto"] is True
    checked = client.post("/api/update/check").json()
    assert checked["status"]["available"] is True and checked["state"] == "available"


def test_cli_check_and_update(monkeypatch, capsys, paths):
    from eks_harness.cli import main, update_cmds

    current = {"commit": "a" * 40}
    installs = []

    def fake_check(repository, ref, with_changes=True):
        latest = "b" * 40
        return selfupdate.Status(_installed(current["commit"]), repository, ref, latest, current["commit"] != latest,
                                 "a -> b", [{"sha": latest, "title": "New thing"}])

    def fake_install(repository, commit, extras, output=None):
        installs.append((repository, commit, extras))
        current["commit"] = commit
        return commit

    monkeypatch.setattr(selfupdate, "check", fake_check)
    monkeypatch.setattr(selfupdate, "install", fake_install)
    monkeypatch.setattr(selfupdate, "receipt_path", lambda: paths.state_dir / "uv-receipt.toml")
    monkeypatch.setattr(update_cmds, "_restart_services", lambda paths, restart, as_json: {"daemon": "restarted"})
    capsys.readouterr()
    assert main(["update", "--check", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["available"] is True and data["changes"][0]["title"] == "New thing"
    assert main(["update", "--json", "--ref", "main"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["updated"] is True and data["restarted"] == {"daemon": "restarted"}
    assert installs == [("https://github.com/eksenova/eks-harness.git", "b" * 40, ["video"])]
    assert main(["update", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["updated"] is False
    assert selfupdate.read_state(paths)["lastUpdate"]["by"] == "cli"
