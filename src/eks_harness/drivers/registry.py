from __future__ import annotations

from pathlib import Path
from typing import Any

from eks_harness.plugins import PluginError, PluginHost


def driver_extensions(host: PluginHost, platform: str, tree: Path | None = None) -> list[dict[str, Any]]:
    worker = "web" if platform == "web" else "mobile"
    found: list[dict[str, Any]] = []
    for contribution in host.contributions("driver_extension", tree):
        wanted = contribution.get("platforms") or [contribution.get("platform") or worker]
        if isinstance(wanted, str):
            wanted = [wanted]
        if platform not in wanted and worker not in wanted:
            continue
        record = host.get(contribution.plugin_id, tree)
        module = (record.manifest.root / contribution.path).resolve()
        if not module.is_file():
            raise PluginError(f"{contribution.key}: {module} does not exist")
        found.append({"id": f"{contribution.plugin_id}:{contribution.id}", "module": str(module),
                      "settings": host.settings(contribution.plugin_id, tree),
                      "required": contribution.get("required", True)})
    return found


def flow_helpers(host: PluginHost, platform: str, tree: Path | None = None) -> list[type]:
    mixins: list[type] = []
    for contribution in host.contributions("flow_helpers", tree):
        wanted = contribution.get("platforms") or [contribution.get("platform") or "any"]
        if isinstance(wanted, str):
            wanted = [wanted]
        if "any" not in wanted and platform not in wanted and not (platform != "web" and "mobile" in wanted):
            continue
        target = host.import_entry(contribution, tree)
        if not isinstance(target, type):
            raise PluginError(f"{contribution.key}: flow_helpers must name a class")
        mixins.append(target)
    return mixins


def driver_for(host: PluginHost, platform: str, tree: Path | None = None) -> Any:
    matches = [c for c in host.contributions("driver", tree) if c.id == platform or c.get("platform") == platform]
    if not matches:
        raise PluginError(f"no active plugin contributes a driver for {platform}")
    return host.load(matches[-1], tree)
