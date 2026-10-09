from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

from eks_harness.cli import driver_cmds, flow_cmds
from eks_harness.drivers.client import WorkerClient
from eks_harness.drivers.profile import load_profile
from eks_harness.flows import MobileApp

from fake_worker import FakeWorker


def test_mobile_app_fakes_patches_and_injections(tmp_path: Path) -> None:
    (tmp_path / "app/.harness").mkdir(parents=True)
    (tmp_path / "app/.harness/app.toml").write_text('[app]\nplatforms = ["ios", "android"]\n')
    profile = load_profile(tmp_path / "app/.harness")
    answers = {"arm": {"armed": "nfc"}, "patch": {"key": "p1", "mode": "overlay"}, "inject": "i1", "unpatch": True,
               "route": {"name": "Home"}, "logs": ["2026 [ios] [error] other", "2026 [android] [error] boom"], "emit": True}
    with FakeWorker(answers) as fake:
        app = MobileApp(profile, worker=WorkerClient(fake.url), sid="m1", platform="android")
        assert app.kind == "mobile-android"
        app.arm("nfc", mode="error", delayMs=200)
        assert app.patch("#total", text="1.250,00", style={"color": "#f00"}) == {"key": "p1", "mode": "overlay"}
        assert app.inject("Confetti", props={"count": 40}) == "i1"
        app.unpatch("p1")
        app.emit("beat", {"index": 4})
        checks = app.checks()
        assert checks["route"] == "Home" and checks["errors"] == ["[android] [error] boom"]
        assert fake.calls[0][1] == ["nfc", {"mode": "error", "delayMs": 200}]
        assert fake.calls[2][1] == [{"component": "Confetti", "props": {"count": 40}}]
        assert all(call[2]["platform"] == "android" and call[2]["sid"] == "m1" for call in fake.calls)


def test_cli_modules_register_and_list_flows(tmp_path: Path, capsys) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    flow_cmds.register(sub)
    driver_cmds.register(sub)
    args = parser.parse_args(["flow", "run", "x.py", "--platform", "ios", "--set", "a=1", "--json"])
    assert args.platform == "ios" and args.set == ["a=1"]
    assert flow_cmds.parse_sets(["a=1", "b=x=y"]) == {"a": "1", "b": "x=y"}
    flows = tmp_path / "web/.harness/flows"
    flows.mkdir(parents=True)
    (flows / "login-check.py").write_text(textwrap.dedent("def flow(app):\n    pass\n"))
    args = parser.parse_args(["flow", "list", "--tree", str(tmp_path), "--json"])
    assert args.func(args) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["flow"] for item in listed["items"]] == ["login-check.py"]
    args = parser.parse_args(["driver", "workers", "--json"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out) == {"items": []}
