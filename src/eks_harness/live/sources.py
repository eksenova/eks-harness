from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from eks_harness.db import Database
from eks_harness.db.repos import leases as leases_repo
from eks_harness.live.android import AndroidLiveSource
from eks_harness.live.chrome import ChromeScreencastSource
from eks_harness.live.common import (
    LiveError,
    LiveUnavailable,
    StreamSettings,
    adb_binary,
    ffmpeg_binary,
    xcrun_binary,
)
from eks_harness.live.ios import IosLiveSource
from eks_harness.pools.base import (
    DEVICE_KINDS,
    LiveSource,
    PoolHost,
    Pools,
    browser_resource,
    device_key,
    parse_browser_resource,
)

CONTEXT_META_KEYS = ("browserContextIds", "browserContextId")
KIND_LABELS = {"ios": "iOS", "android": "Android"}


@dataclass(frozen=True)
class LiveTarget:
    resource: str
    kind: str
    label: str
    index: int
    profile: int | None = None

    @property
    def is_browser(self) -> bool:
        return self.kind == "browser"


def device_target(pools: Pools, kind: str, index: int) -> LiveTarget:
    if kind not in DEVICE_KINDS:
        raise LiveUnavailable(404, "not_found", f"Unknown device kind {kind}; use ios or android.")
    key = device_key(kind, index)
    device = pools.host.devices.get(key)
    if device is None:
        raise LiveUnavailable(404, "device_not_found", f"No device {key} in the pool.")
    return LiveTarget(key, kind, f"{KIND_LABELS[kind]} {index}", index)


def profile_target(pools: Pools, profile_id: str) -> LiveTarget:
    text = profile_id.strip()
    if not text.startswith("browser:"):
        text = f"browser:{text}"
    try:
        index, profile = parse_browser_resource(text)
    except ValueError:
        raise LiveUnavailable(404, "profile_not_found",
                              f"No profile {profile_id}; profile ids look like browser:1:2.") from None
    if index not in pools.browsers.indices() or not 1 <= profile <= int(pools.host.config["browser.profilesPerInstance"]):
        raise LiveUnavailable(404, "profile_not_found", f"No profile {profile_id} in the pool.")
    return LiveTarget(browser_resource(index, profile), "browser", f"Chrome {index} profile {profile}", index,
                      profile)


def _meta_contexts(meta: dict) -> tuple[str, ...]:
    for key in CONTEXT_META_KEYS:
        value = meta.get(key)
        if isinstance(value, str) and value:
            return (value,)
        if isinstance(value, (list, tuple)):
            found = tuple(str(v) for v in value if v)
            if found:
                return found
    return ()


def profile_contexts(db: Database, target: LiveTarget) -> tuple[str, ...] | None:
    conn = db.conn()
    holder = leases_repo.holder_of(conn, target.resource)
    if holder is None:
        raise LiveUnavailable(409, "profile_not_in_use",
                              f"{target.label} is not in use, so it has no page to show.")
    reported = _meta_contexts(holder.meta or {})
    if reported:
        return reported
    prefix = f"browser:{target.index}:"
    others = [lease for lease in leases_repo.holding(conn, "browser")
              if lease.resource and lease.resource.startswith(prefix) and lease.resource != target.resource]
    if others:
        raise LiveUnavailable(
            409, "profile_contexts_unknown",
            f"Chrome {target.index} holds {len(others) + 1} profiles and the harness of {target.label} has not "
            f"reported its browser contexts (lease meta browserContextIds), so its window cannot be told apart.")
    return None


def check_ready(pools: Pools, target: LiveTarget) -> None:
    if target.is_browser:
        if not pools.browsers.alive(target.index):
            raise LiveUnavailable(409, "browser_not_running", f"Chrome {target.index} is not running.")
        if not pools.fake and not pools.browsers.cdp_url(target.index):
            raise LiveUnavailable(409, "browser_not_running",
                                  f"Chrome {target.index} has no DevTools endpoint.")
        return
    device = pools.host.devices.get(target.resource) or {}
    if device.get("status") != "on":
        state = device.get("status") or "off"
        raise LiveUnavailable(409, "device_not_running",
                              f"{target.label} is not running (it is {state}); start it first.")
    if pools.fake:
        return
    if not ffmpeg_binary():
        raise LiveUnavailable(503, "ffmpeg_missing",
                              "ffmpeg is not installed or not on PATH; the live view of devices needs it.")
    if target.kind == "ios":
        if not xcrun_binary():
            raise LiveUnavailable(503, "xcrun_missing", "xcrun was not found; the iOS live view needs Xcode.")
        if not device.get("udid"):
            raise LiveUnavailable(409, "device_not_ready", f"{target.label} has no simulator yet.")
        if pools.devices.is_recording(target.resource):
            raise LiveUnavailable(
                409, "device_recording",
                f"{target.label} is recording a video capture; a simulator records one stream at a time, "
                f"so the live view is available again once the capture stops.")
    else:
        if not adb_binary():
            raise LiveUnavailable(503, "adb_missing", "adb was not found (set ANDROID_HOME).")
        if not device.get("serial"):
            raise LiveUnavailable(409, "device_not_ready", f"{target.label} has no emulator serial yet.")


def pool_source(host: PoolHost, resource: str, *, cdp_url: str | None = None,
                context_ids: Iterable[str] | None = None) -> LiveSource:
    settings = StreamSettings.from_config(host.config)
    if resource.startswith("browser:"):
        index, profile = parse_browser_resource(resource)
        url = cdp_url or (host.browsers.get(str(index)) or {}).get("cdp")
        if not url:
            raise LiveError(f"Chrome {index} is not running")
        return ChromeScreencastSource(url, settings, name=f"Chrome {index} profile {profile}",
                                      context_ids=context_ids)
    kind, _, _ = resource.partition(":")
    device = host.devices.get(resource)
    if device is None:
        raise LiveError(f"no device {resource} in the pool")
    label = device.get("name") or resource
    if kind == "ios":
        udid = device.get("udid")
        if not udid:
            raise LiveError(f"{label} has no simulator yet")
        return IosLiveSource(udid, settings, host.paths.tmp_dir / "live", name=label)
    if kind == "android":
        serial = device.get("serial")
        if not serial:
            raise LiveError(f"{label} has no emulator serial yet")
        return AndroidLiveSource(serial, settings, name=label)
    raise LiveError(f"no live view for {resource}")


def prepare(pools: Pools, db: Database, target: LiveTarget) -> Callable[[], LiveSource]:
    check_ready(pools, target)
    if pools.fake:
        pool = pools.browsers if target.is_browser else pools.devices
        return lambda: pool.live_source(target.resource)
    if target.is_browser:
        contexts = profile_contexts(db, target)
        cdp_url = pools.browsers.cdp_url(target.index)
        return lambda: pool_source(pools.host, target.resource, cdp_url=cdp_url, context_ids=contexts)
    return lambda: pool_source(pools.host, target.resource)
