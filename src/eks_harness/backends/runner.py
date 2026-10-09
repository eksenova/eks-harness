from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from eks_harness.config import load as load_config
from eks_harness.paths import resolve_paths
from eks_harness.plugins import PluginHost

STEPS = ("describe", "fingerprint", "prepare", "spec", "teardown", "seed", "snapshot", "restore", "health")


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
    args = parser.parse_args(argv)
    context = json.loads(Path(args.context).read_text(encoding="utf-8"))
    tree = Path(context["tree"]) if context.get("tree") else None
    paths = resolve_paths()
    host = PluginHost(paths, load_config(paths))
    backend = host.load(host.contribution("backend", args.contribution, tree), tree)
    method = getattr(backend, args.step, None)
    if method is None:
        if args.step in ("describe", "fingerprint", "spec"):
            print(f"{args.plugin}:{args.contribution} does not implement {args.step}", file=sys.stderr)
            return 2
        print(json.dumps({}))
        return 0
    kwargs: dict[str, Any] = {}
    if args.step == "teardown":
        kwargs["final"] = args.final
    if args.step == "seed":
        kwargs["scenario"] = args.scenario
    if args.step in ("snapshot", "restore"):
        kwargs["name"] = args.name
    result = method(context, **kwargs) if args.step != "describe" else method()
    result = _plain(result)
    if args.step == "fingerprint" and isinstance(result, str):
        result = {"fingerprint": result}
    print(json.dumps(result if result is not None else {}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
