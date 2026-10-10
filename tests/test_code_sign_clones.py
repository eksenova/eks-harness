from __future__ import annotations

import time
from pathlib import Path

import pytest

from eks_harness.pools import browsers
from eks_harness.pools.browsers import (
    browser_start_times,
    orphaned_code_sign_clones,
    remove_tree,
)

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HELPER = ("/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Helpers/"
          "Google Chrome Helper.app/Contents/MacOS/Google Chrome Helper")


def clones(root: Path, *names: str) -> dict[Path, float]:
    root.mkdir(parents=True, exist_ok=True)
    made = {}
    for name in names:
        clone = root / f"code_sign_clone.{name}"
        (clone / "Google Chrome.app.bundle" / "Contents").mkdir(parents=True)
        made[clone] = 0.0
    return made


def test_only_clones_without_a_running_browser_are_orphaned(tmp_path: Path) -> None:
    root = tmp_path / "com.google.Chrome.code_sign_clone"
    births = clones(root, "live", "dead", "young", "named")
    now = 10_000.0
    births[root / "code_sign_clone.live"] = 5_000.0
    births[root / "code_sign_clone.dead"] = 4_000.0
    births[root / "code_sign_clone.young"] = now - 30
    births[root / "code_sign_clone.named"] = 3_000.0

    orphans = orphaned_code_sign_clones([root], starts=[4_999.0], referenced={"code_sign_clone.named"}, now=now,
                                        born=births.__getitem__)

    assert orphans == [root / "code_sign_clone.dead"]


def test_a_clone_made_well_after_a_launch_is_not_claimed_by_it(tmp_path: Path) -> None:
    root = tmp_path / "com.google.Chrome.code_sign_clone"
    births = clones(root, "later")
    births[root / "code_sign_clone.later"] = 5_030.0

    orphans = orphaned_code_sign_clones([root], starts=[5_000.0], referenced=set(), now=10_000.0,
                                        born=births.__getitem__)

    assert orphans == [root / "code_sign_clone.later"]


def test_start_times_come_from_browser_main_processes_only(monkeypatch: pytest.MonkeyPatch) -> None:
    lstarts = {10: "Sat Oct 10 16:28:23 2026", 11: "Sat Oct 10 16:28:24 2026", 12: "Sat Oct 10 09:00:00 2026",
               14: "Sat Oct 10 16:27:52 2026"}
    monkeypatch.setattr(browsers, "pid_started", lambda pid: lstarts[pid])
    table = {
        10: (1, f"{CHROME} --headless=new --dump-dom about:blank"),
        11: (10, f"{HELPER} --type=renderer"),
        12: (1, "/usr/bin/python3 worker.py"),
        14: (1, "/Applications/Visual Studio Code.app/Contents/MacOS/Electron"),
        15: (1, f"{CHROME} --user-data-dir=/pool/1 --disable-features=CalculateNativeWinOcclusion,MacAppCodeSignClone"),
        13: (1, ("/private/var/folders/x/X/com.google.Chrome.code_sign_clone/code_sign_clone.AbC123/"
                 "Google Chrome.app.bundle/Contents/MacOS/Google Chrome --type=gpu-process")),
    }

    starts, referenced = browser_start_times(table)

    assert starts == [time.mktime(time.strptime("Sat Oct 10 16:28:23 2026", "%a %b %d %H:%M:%S %Y"))]
    assert referenced == {"code_sign_clone.AbC123"}


def test_remove_tree_handles_read_only_bundles(tmp_path: Path) -> None:
    clone = tmp_path / "code_sign_clone.ro"
    binary = clone / "Google Chrome.app.bundle" / "Contents" / "MacOS" / "Google Chrome"
    binary.parent.mkdir(parents=True)
    binary.write_text("x")
    binary.chmod(0o444)
    binary.parent.chmod(0o555)

    remove_tree(clone)

    assert not clone.exists()
