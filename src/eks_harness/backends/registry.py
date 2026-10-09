from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from eks_harness.plugins import PluginError, PluginHost

PLUGIN_SCHEME = "plugin:"


def plugin_definitions(host: PluginHost, tree: str | None) -> list[dict[str, Any]]:
    path = Path(tree) if tree else None
    found = []
    for contribution in host.contributions("backend", path):
        found.append({"name": contribution.id, "path": f"{PLUGIN_SCHEME}{contribution.plugin_id}:{contribution.id}",
                      "source": "plugin", "plugin": contribution.plugin_id, "tree": tree,
                      "description": str(contribution.get("description", ""))})
    return found


def is_plugin_path(path: str | Path) -> bool:
    return str(path).startswith(PLUGIN_SCHEME)


def runner_command(path: str | Path) -> list[str]:
    _, plugin_id, contribution_id = str(path).split(":", 2)
    return [sys.executable, "-m", "eks_harness.backends.runner", plugin_id, contribution_id]


def choose_backend(host: PluginHost, tree: Path, requested: str | None = None) -> dict[str, Any]:
    choices = []
    for contribution, policy in host.load_all("backend_policy", tree):
        try:
            choice = policy.choose(tree, requested)
        except Exception as error:
            choices.append({"policy": contribution.key, "error": str(error)})
            continue
        entry = {"policy": contribution.key, "backend": choice.backend, "reason": choice.reason,
                 "target": choice.target}
        choices.append(entry)
    decided = next((c for c in choices if "backend" in c), None)
    if decided is None:
        return {"backend": requested or "", "reason": "no backend policy for this tree" if not choices else
                "every backend policy failed", "candidates": choices}
    return {**decided, "candidates": choices}


def seeders_for(host: PluginHost, tree: Path | None, backend: str | None = None) -> list[tuple[Any, Any]]:
    out = []
    for contribution, seeder in host.load_all("seeder", tree):
        targets = contribution.get("backends") or []
        if backend and targets and backend not in targets:
            continue
        out.append((contribution, seeder))
    return out


def personas_for(host: PluginHost, tree: Path | None, target: str) -> list[dict[str, Any]]:
    found = []
    for contribution, provider in host.load_all("credentials", tree):
        try:
            for persona in provider.personas(target):
                item = {"id": persona.id, "label": persona.label, "username": persona.username,
                        "tenant": persona.tenant, "roles": list(persona.roles), "provider": contribution.key}
                found.append(item)
        except Exception as error:
            raise PluginError(f"{contribution.key}: {error}") from error
    return found


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
