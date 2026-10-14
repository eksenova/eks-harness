from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYRIGHT = "pyright@1.1.390"

NEGATIVE_CALLS: dict[str, dict[str, str]] = {
    "tests/eks/test_edit_audio.py": {
        'voice.set(name="x")': "reportCallIssue",
        "voice.set(volume=1.0)": "reportCallIssue",
        'e.audio.track("music/song.ogg").set(ducks=[{"under": "drums"}])': "reportArgumentType",
    },
    "tests/eks/test_edit_timeline.py": {
        'second.set(id="x/1")': "reportCallIssue",
        "second.set(volume=1.0)": "reportCallIssue",
        'second.set(start="1s")': "reportArgumentType",
        'second.set(src=(t("0s"), t("1s")))': "reportArgumentType",
        'e.render_settings(quality="best")': "reportArgumentType",
        "e.render_settings(bogus=1)": "reportCallIssue",
    },
    "tests/eks/test_media_catalog.py": {
        'fx.captions("words", kind="captions")': "reportCallIssue",
    },
}


def _pyright(*args: str, timeout: int) -> subprocess.CompletedProcess[str]:
    npx = shutil.which("npx")
    if npx is None:
        pytest.skip("npx is not installed; the tests type check needs pyright from npm")
    return subprocess.run(
        [npx, "--yes", PYRIGHT, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )


@pytest.fixture(scope="module")
def pyright_available() -> None:
    try:
        probe = _pyright("--version", timeout=300)
    except subprocess.TimeoutExpired:
        pytest.skip(f"{PYRIGHT} could not be fetched in time")
    if probe.returncode != 0:
        pytest.skip(f"{PYRIGHT} is unavailable: {probe.stderr.strip() or probe.stdout.strip()}")


def _errors(report: dict[str, Any]) -> list[str]:
    return [
        f"{Path(item['file']).relative_to(ROOT)}:{item['range']['start']['line'] + 1}: {item['message']}"
        for item in report["generalDiagnostics"]
        if item["severity"] == "error"
    ]


def test_negative_calls_carry_the_pyright_rule_they_trip() -> None:
    for path, calls in NEGATIVE_CALLS.items():
        lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
        for call, rule in calls.items():
            matching = [line for line in lines if line.strip().startswith(call)]
            assert len(matching) == 1, f"{path}: expected one line starting with {call!r}, found {len(matching)}"
            assert matching[0].rstrip().endswith(f"# pyright: ignore[{rule}]"), f"{path}: {matching[0].strip()}"


def test_tests_directory_is_pyright_clean(pyright_available: None) -> None:
    result = _pyright("--outputjson", "--pythonpath", sys.executable, "tests/eks", timeout=600)
    report = json.loads(result.stdout)
    assert _errors(report) == []
    tests_dir = ROOT / "tests/eks"
    visible = [
        path
        for path in tests_dir.rglob("*.py")
        if not any(part.startswith(".") for part in path.relative_to(tests_dir).parts)
    ]
    assert report["summary"]["filesAnalyzed"] == len(visible)


def test_tests_directory_is_pyright_clean_under_the_default_config(pyright_available: None) -> None:
    result = _pyright("--outputjson", "tests/eks", timeout=600)
    report = json.loads(result.stdout)
    assert _errors(report) == []


def test_negative_call_ignores_are_all_needed(pyright_available: None, tmp_path: Path) -> None:
    config = tmp_path / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "include": [os.path.relpath(ROOT / path, tmp_path) for path in NEGATIVE_CALLS],
                "reportUnnecessaryTypeIgnoreComment": "error",
            }
        ),
        encoding="utf-8",
    )
    result = _pyright("-p", str(config), "--outputjson", "--pythonpath", sys.executable, timeout=600)
    report = json.loads(result.stdout)
    assert _errors(report) == []
    assert report["summary"]["filesAnalyzed"] == len(NEGATIVE_CALLS)
