from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYRIGHT = "pyright@1.1.390"
CHECKED = ["tests/eks/typing_public_api.py", "tests/eks/typing_negative.py", "examples/dsl"]


def _pyright(*args: str, timeout: int) -> subprocess.CompletedProcess[str]:
    npx = shutil.which("npx")
    if npx is None:
        pytest.skip("npx is not installed; the public API type check needs pyright from npm")
    return subprocess.run([npx, "--yes", PYRIGHT, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                          check=False)


@pytest.fixture(scope="module")
def pyright_available() -> None:
    try:
        probe = _pyright("--version", timeout=300)
    except subprocess.TimeoutExpired:
        pytest.skip(f"{PYRIGHT} could not be fetched in time")
    if probe.returncode != 0:
        pytest.skip(f"{PYRIGHT} is unavailable: {probe.stderr.strip() or probe.stdout.strip()}")


def test_public_api_types_check_cleanly(pyright_available: None, tmp_path: Path) -> None:
    # contract 13: the design examples and the typing examples type-check with pyright (basic); the negative
    # file holds lines that must stay errors, each with an ignore that pyright reports once it is unnecessary.
    # The examples live under .harness/, which pyright skips by default, so a project file includes them by
    # relative path and excludes only caches.
    config = tmp_path / "pyrightconfig.json"
    config.write_text(json.dumps({
        "include": [os.path.relpath(ROOT / path, tmp_path) for path in CHECKED],
        "exclude": ["**/__pycache__"],
        "typeCheckingMode": "basic",
    }), encoding="utf-8")
    result = _pyright("-p", str(config), "--outputjson", "--pythonpath", sys.executable, timeout=600)
    report = json.loads(result.stdout)
    problems = [f"{item['file']}:{item['range']['start']['line'] + 1}: {item['message']}"
                for item in report["generalDiagnostics"] if item["severity"] == "error"]
    assert problems == []
    expected = sum(1 for path in CHECKED for _ in ([ROOT / path] if path.endswith(".py")
                                                    else (ROOT / path).rglob("*.py")))
    assert report["summary"]["filesAnalyzed"] == expected
