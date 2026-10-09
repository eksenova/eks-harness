from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

SKIP_WEB_ENV = "EKS_HARNESS_SKIP_WEB"


def load_version_module(root: Path):
    path = root / "src" / "eks_harness" / "_version.py"
    spec = importlib.util.spec_from_file_location("_eks_harness_version_for_build", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WebBuildError(RuntimeError):
    pass


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        package = root / "src" / "eks_harness"
        version_module = load_version_module(root)
        self.build_web(root, package)
        info = {
            "version": version_module.__version__,
            "sourceHash": version_module.compute_source_hash(root),
            "builtAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (package / version_module.BUILD_INFO_FILE).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")

    def build_web(self, root: Path, package: Path) -> None:
        web = root / "web"
        skip = os.environ.get(SKIP_WEB_ENV, "").strip().lower() in ("1", "true", "yes")
        if not (root / "pnpm-workspace.yaml").is_file() or not (web / "package.json").is_file():
            if skip:
                self.app.display_warning(
                    f"eks-harness: no pnpm workspace in {root}; {SKIP_WEB_ENV}=1 is set, so the web parts are not built")
                return
            raise WebBuildError(
                f"eks-harness: the web sources are missing ({root / 'pnpm-workspace.yaml'}). "
                f"Set {SKIP_WEB_ENV}=1 to build the package without the web UI, workers and scene runtime.")
        if skip:
            self.app.display_warning(f"eks-harness: {SKIP_WEB_ENV}=1 is set; the web parts are not built")
            return
        pnpm = shutil.which("pnpm")
        node = shutil.which("node")
        if not pnpm or not node:
            raise WebBuildError(
                "eks-harness: building the web UI, workers and scene runtime needs Node.js 22+ and pnpm on PATH "
                "(https://nodejs.org, then: corepack enable pnpm).")
        self.app.display_info("eks-harness: building the web UI, workers and scene runtime with pnpm")
        commands = (
            [pnpm, "install", "--frozen-lockfile"],
            [pnpm, "-r", "--filter", "./sdk/**", "--filter", "./workers/**", "run", "build"],
            [pnpm, "--filter", "./web", "run", "build"],
        )
        for command in commands:
            result = subprocess.run(command, cwd=root, stdout=sys.stderr, stderr=sys.stderr)
            if result.returncode != 0:
                raise WebBuildError(f"eks-harness: '{' '.join(command[1:])}' failed (exit {result.returncode})")
        dist = web / "dist"
        if not (dist / "index.html").is_file():
            raise WebBuildError(f"eks-harness: the web build produced no {dist / 'index.html'}")
        copy_tree(dist, package / "web_dist")
        assets = package / "assets"
        assets.mkdir(exist_ok=True)
        runtime = root / "sdk" / "scene" / "dist" / "scene-runtime.js"
        if not runtime.is_file():
            raise WebBuildError(f"eks-harness: the scene runtime was not built ({runtime})")
        shutil.copyfile(runtime, assets / "scene-runtime.js")
        workers = package / "_workers"
        for folder in sorted(p for p in (root / "workers").iterdir() if (p / "package.json").is_file()):
            target = workers / folder.name
            copy_tree(folder / "src", target / "src")
            shutil.copyfile(folder / "package.json", target / "package.json")


def copy_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
