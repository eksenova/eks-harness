from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCOPES = ("src", "workers", "sdk", "web/src", "claude", "docs", "examples", "tests")
EXCLUDED = (Path("docs/dsl"), Path("docs/local"), Path("scripts/check_removed.py"),
            Path("tests/test_removed_apis.py"),
            Path("src/eks_harness/_workers"), Path("src/eks_harness/web_dist"), Path("docs/migration.md"),
            Path("claude/skills/eks-migration"))
SKIPPED_DIRS = frozenset({"node_modules", "__pycache__", ".git", ".venv", "dist", "build", ".pytest_cache",
                          ".ruff_cache", ".mypy_cache"})
TEXT_SUFFIXES = frozenset({".py", ".pyi", ".mjs", ".cjs", ".js", ".ts", ".tsx", ".jsx", ".md", ".toml", ".json",
                           ".tmpl", ".css", ".html", ".txt", ".yaml", ".yml", ".sh", ".cfg", ".ini"})
MAX_BYTES = 2_000_000

REMOVED_TYPES = ("platform", "device_pool", "app_profile", "login", "fake", "capture", "annotator", "mcp_resources",
                 "skill", "hook", "agent", "template", "driver", "flow_helpers")
TYPES = "|".join(REMOVED_TYPES)

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("module eks_harness.flows", re.compile(r"\beks_harness[./]flows\b")),
    ("module eks_harness.drivers.sessions", re.compile(r"\beks_harness[./]drivers[./]sessions\b|\bdrivers/sessions\.py\b")),
    ("module eks_harness.drivers.builtin", re.compile(r"\beks_harness[./]drivers[./]builtin\b|\bdrivers/builtin\.py\b")),
    ("module eks_harness.capture.pace", re.compile(r"\beks_harness[./]capture[./]pace\b|\bcapture/pace\.py\b")),
    ("module eks_harness.capture.steplog", re.compile(r"\bsteplog\b")),
    ("module eks_harness.cli.flow_cmds", re.compile(r"\bflow_cmds\b")),
    ("class WorkerSession", re.compile(r"\bWorkerSession\b")),
    ("flow app classes", re.compile(r"\b(?:WebApp|MobileApp)\b")),
    ("registry helpers", re.compile(r"\b(?:driver_for|flow_helpers)\b")),
    ("prepare_app of the flow runner", re.compile(r"\b(?:prepare_app|run_flow|FlowRequest)\b")),
    ("CLI flow run|list", re.compile(r"(?:eks-harness|ehx)\s+flow\s+(?:run|list)\b|`flow (?:run|list)\b")),
    ("MCP tools", re.compile(r"\b(?:driver_act|driver_observe|driver_capture|flow_run|seed_backend|list_personas|"
                             r"ensure_backend)\b")),
    ("driver routes", re.compile(r"/api/drivers/[^\s\"'`]*/(?:act|observe|capture|events)\b")),
    ("worker /exec and legacy run routes", re.compile(r"[\"'`]/(?:exec|run|run-wait|run-status|continue|abort)[\"'`]")),
    ("worker legacy route tables", re.compile(r"\b(?:RUN_ROUTES|EXTENSION_ROUTES)\b|^const ROUTES\b")),
    ("script runner", re.compile(r"script-runner|\bcreateRunner\b")),
    ("duplicated pace defaults", re.compile(r"\b(?:PACE_FAST|PACE_ENV)\b")),
    ("web UI tool rail", re.compile(r"\b(?:ToolRail|driverAct|driverObserve|driverCapture)\b")),
    ("removed contribution types", re.compile(rf"\bcontributes\.(?:{TYPES})\b|contributions\(\s*[\"'](?:{TYPES})[\"']"
                                              rf"|of_type\(\s*[\"'](?:{TYPES})[\"']")),
)

KEPT = "kept until M5: device and score takes run on it"
GONE = "asserts the tool is gone"
RUN_BLOCK = (r"script-runner\.mjs|\bcreateRunner\(|^const RUN_ROUTES = \{|RUN_ROUTES\[|\.\.\.Object\.keys\(RUN_ROUTES\)"
             r"|^[\"']/(?:run|continue|run-wait|abort|run-status|exec)[\"']:")
ALLOWED: tuple[tuple[str, re.Pattern[str] | None, str], ...] = (
    ("src/eks_harness/flows/", None, KEPT),
    ("src/eks_harness/drivers/sessions.py", None, KEPT),
    ("src/eks_harness/drivers/client.py", re.compile(r'^status, result = self\.post\("/exec", body, timeout\)$'), KEPT),
    ("src/eks_harness/drivers/registry.py", re.compile(r"\bflow_helpers\b"), KEPT),
    ("src/eks_harness/drivers/workers.py", re.compile(r'"kit/src/script-runner\.mjs"'), KEPT),
    ("src/eks_harness/plugins/manifest.py", re.compile(r'^"flow_helpers": '), KEPT),
    ("src/eks_harness/score/takes.py", None, KEPT),
    ("src/eks_harness/video/plugins/builtin/media/device_take/__init__.py", None, KEPT),
    ("src/eks_harness/api/routes_scores.py", re.compile(r"\bWorkerSession\b"), KEPT),
    ("workers/kit/src/script-runner.mjs", None, KEPT),
    ("workers/kit/package.json", re.compile(r"script-runner"), KEPT),
    ("workers/web-driver/src/server.mjs", re.compile(RUN_BLOCK), KEPT),
    ("workers/mobile-driver/src/server.mjs", re.compile(RUN_BLOCK), KEPT),
    ("tests/flows/", None, KEPT),
    ("tests/drivers/test_driver_workers.py", re.compile(r"\bWorkerSession\b"), KEPT),
    ("tests/drivers/test_driver_profiles.py", re.compile(r"\bflow_helpers\b"), KEPT),
    ("tests/fake_worker.py", re.compile(r'^if self\.path != "/exec":$'), KEPT),
    ("tests/test_renderq.py", re.compile(r"^from eks_harness\.flows import runner$"), KEPT),
    ("workers/kit/test/kit.test.mjs", re.compile(r"script-runner\.mjs|\bcreateRunner\("), KEPT),
    ("workers/web-driver/test/server.test.mjs", re.compile(r"worker\.post\('/exec', "), KEPT),
    ("workers/mobile-driver/test/server.test.mjs", re.compile(r"worker\.post\('/exec', "), KEPT),
    ("tests/test_mcp.py", re.compile(r'^assert not \{"driver_act", "driver_observe", "driver_capture", "flow_run", '
                                     r'"seed_backend", "list_personas",$|^"ensure_backend"\} & set\(tools\)$'), GONE),
    ("tests/test_mcp_system.py", re.compile(r'^removed = \{"driver_act", "driver_observe", "driver_capture", '
                                           r'"flow_run", "seed_backend", "list_personas",$|^"ensure_backend"\}$'), GONE),
    ("tests/test_mcp_http.py", re.compile(r'^assert "driver_act" not in names and "flow_run" not in names$'), GONE),
)


def allowed(relative: str, line: str) -> str | None:
    for path, pattern, reason in ALLOWED:
        inside = relative.startswith(path) if path.endswith("/") else relative == path
        if inside and (pattern is None or pattern.search(line)):
            return reason
    return None


def excluded(relative: Path) -> bool:
    return any(relative == item or item in relative.parents for item in EXCLUDED)


def files(root: Path) -> list[Path]:
    found: list[Path] = []
    for scope in SCOPES:
        base = root / scope
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(root)
            if any(part in SKIPPED_DIRS for part in relative.parts) or excluded(relative):
                continue
            if path.is_file() and path.suffix in TEXT_SUFFIXES and path.stat().st_size <= MAX_BYTES:
                found.append(path)
    return found


def scan(root: Path = ROOT) -> list[tuple[str, int, str, str]]:
    hits: list[tuple[str, int, str, str]] = []
    for path in files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        relative = path.relative_to(root).as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in PATTERNS:
                if pattern.search(line):
                    hits.append((relative, number, label, line.strip()))
    return hits


def split(hits: list[tuple[str, int, str, str]]) -> tuple[list[tuple[str, int, str, str]], dict[str, int]]:
    failing: list[tuple[str, int, str, str]] = []
    kept: dict[str, int] = {}
    for hit in hits:
        reason = allowed(hit[0], hit[3])
        if reason is None:
            failing.append(hit)
        else:
            kept[reason] = kept.get(reason, 0) + 1
    return failing, kept


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else ROOT
    failing, kept = split(scan(root))
    for relative, number, label, line in failing:
        print(f"{relative}:{number}: {label}: {line[:160]}")
    for reason, count in sorted(kept.items()):
        print(f"{count} allowlisted reference(s): {reason}")
    if failing:
        print(f"{len(failing)} reference(s) to removed APIs", file=sys.stderr)
        return 1
    print("no references to removed APIs outside the allowlist")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
