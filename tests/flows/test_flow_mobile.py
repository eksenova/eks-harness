from __future__ import annotations

from pathlib import Path

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
