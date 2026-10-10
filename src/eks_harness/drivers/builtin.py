from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from eks_harness.drivers.client import WorkerError
from eks_harness.drivers.profile import (
    project_hide_selectors,
    LEASE_KINDS,
    WORKER_KINDS,
    load_profile,
    mobile_worker_config,
    node_modules_dir,
    pace_from_config,
    web_worker_config,
)
from eks_harness.drivers.registry import driver_extensions
from eks_harness.drivers.sessions import WorkerSession
from eks_harness.drivers.workers import WorkerManager
from eks_harness.plugins.host import PluginContext


class WorkerDriver:
    platform = "web"

    def __init__(self, context: PluginContext) -> None:
        self.context = context

    @property
    def lease_kind(self) -> str:
        return LEASE_KINDS[self.platform]

    def capabilities(self) -> dict[str, Any]:
        mobile = self.platform != "web"
        return {
            "platform": self.platform,
            "lease": self.lease_kind,
            "worker": WORKER_KINDS[self.platform],
            "act": ["press", "fill", "type", "scroll", "navigate", "back", "evaluate", "emit",
                    *(["patch", "unpatch", "inject", "arm", "dispatch", "toggle", "submit"] if mobile
                      else ["hover", "select", "check", "key", "goto", "reload"])],
            "observe": ["text", "exists", "tree", "logs", *(["route", "state"] if mobile else ["url", "visible"])],
            "capture": ["screenshot", "video.start", "video.stop", *([] if mobile else ["dom", "mhtml", "a11y",
                                                                                         "har.start", "har.stop"])],
            "events": True,
        }

    def open(self, target: Mapping[str, Any], *, app: Mapping[str, Any] | None = None) -> WorkerSession:
        sid = target.get("sid")
        if not sid:
            raise WorkerError(f"open needs the {self.lease_kind} lease sid in target['sid']")
        tree = Path(target["tree"]) if target.get("tree") else self.context.tree
        harness_dir = Path((app or {}).get("harness") or target.get("harness") or (tree / ".harness" if tree else "."))
        profile = load_profile(harness_dir)
        host = self.context.host
        extensions = driver_extensions(host, self.platform, tree)
        pace = pace_from_config(self.context.config)
        manager = WorkerManager(self.context.paths)
        if self.platform == "web":
            from eks_harness.cli.client import HarnessClient

            project = profile.project or self.context.project.project_id
            with HarnessClient(paths=self.context.paths, config=self.context.config) as client:
                hide = project_hide_selectors(client, project)
            config = web_worker_config(profile, sid=sid, extensions=extensions, pace=pace, hide=hide,
                                       playwright_dirs=[node_modules_dir(self.context.paths)])
            handle = manager.ensure("web", sid, config)
        else:
            sids = dict(target.get("sids") or {self.platform: sid})
            config = mobile_worker_config(profile, sids=sids, extensions=extensions, pace=pace)
            handle = manager.ensure("mobile", sid, config)
        return WorkerSession(handle.client(), platform=self.platform, sid=sid)


class WebDriver(WorkerDriver):
    platform = "web"


class IosDriver(WorkerDriver):
    platform = "ios"


class AndroidDriver(WorkerDriver):
    platform = "android"
