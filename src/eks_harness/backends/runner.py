from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from eks_harness.backends.eks_defs import load_eks_backend
from eks_harness.config import load as load_config
from eks_harness.paths import resolve_paths
from eks_harness.plugins import PluginHost

SEED_STEPS = ("reset", "seed_phase", "snapshot", "restore", "migrations", "drop_snapshot")
STEPS = ("describe", "fingerprint", "prepare", "spec", "start", "health", "info", "stop", "cleanup", "teardown",
         "seed", *SEED_STEPS)
REQUIRED = ("describe", "fingerprint", "spec", "start", "info")
NAMED_STEPS = ("snapshot", "restore", "drop_snapshot")


def _plain(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eks-harness-backend-runner")
    parser.add_argument("plugin")
    parser.add_argument("contribution")
    parser.add_argument("step", choices=STEPS)
    parser.add_argument("--context", required=True)
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--scenario")
    parser.add_argument("--name")
    parser.add_argument("--phase")
    args = parser.parse_args(argv)
    context = json.loads(Path(args.context).read_text(encoding="utf-8"))
    tree = Path(context["tree"]) if context.get("tree") else None
    if args.plugin == "eks":
        backend = load_eks_backend(context.get("tree"), args.contribution)
    else:
        paths = resolve_paths()
        host = PluginHost(paths, load_config(paths))
        backend = host.load(host.contribution("backend", args.contribution, tree), tree)
    method = getattr(backend, args.step, None)
    if method is None:
        if args.step in REQUIRED:
            print(f"{args.plugin}:{args.contribution} does not implement {args.step}", file=sys.stderr)
            return 2
        print(json.dumps({"healthy": True} if args.step == "health" else
                         {"missing": True} if args.step in SEED_STEPS else {}))
        return 0
    kwargs: dict[str, Any] = {}
    if args.step == "teardown":
        kwargs["final"] = args.final
    if args.step == "seed":
        kwargs["scenario"] = args.scenario
    if args.step in NAMED_STEPS:
        if not args.name:
            print(f"{args.step} needs --name", file=sys.stderr)
            return 2
        kwargs["name"] = args.name
    if args.step == "seed_phase":
        if not args.phase:
            print("seed_phase needs --phase", file=sys.stderr)
            return 2
        kwargs["phase"] = args.phase
    result = method(context, **kwargs) if args.step != "describe" else method()
    result = _plain(result)
    if args.step == "fingerprint" and isinstance(result, str):
        result = {"fingerprint": result}
    if args.step == "migrations" and isinstance(result, str):
        result = {"migrations": result}
    if args.step == "health" and not isinstance(result, dict):
        result = {"healthy": True if result is None else bool(result)}
    if args.step == "describe":
        result = {**(result or {}), "implements": [step for step in STEPS if callable(getattr(backend, step, None))]}
    print(json.dumps(result if result is not None else {}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
