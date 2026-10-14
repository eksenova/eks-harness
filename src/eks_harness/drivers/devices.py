from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from eks_harness.engine.errors import DriverError
from eks_harness.pools.base import PoolError
from eks_harness.store.encode import EncodeError, encode_recording


class DeviceCapture(Protocol):
    def device(self, resource: str) -> Mapping[str, Any]: ...

    def screenshot(self, resource: str, dest: Path) -> Path: ...

    def record_start(self, resource: str, dest: Path, sid: str) -> None: ...

    def record_stop(self, resource: str, out: Path, *, trim: bool, events: list[float]) -> Path: ...


class PoolDeviceCapture:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def device(self, resource: str) -> Mapping[str, Any]:
        found = self.ctx.pools.host.devices.get(resource)
        if not isinstance(found, dict):
            raise DriverError("driver_error", f"the device {resource} is not in the device pool")
        return found

    def _live(self, resource: str) -> Any:
        if self.device(resource).get("kind") != "ios" or not self.ctx.has_service("live"):
            return None
        return self.ctx.service("live")

    def screenshot(self, resource: str, dest: Path) -> Path:
        try:
            info = self.ctx.pools.devices.screenshot(resource, dest)
        except PoolError as error:
            raise DriverError("driver_error", f"the screenshot of {resource} failed: {error}", retryable=True) from error
        return Path(info.get("path") or dest)

    def record_start(self, resource: str, dest: Path, sid: str) -> None:
        live = self._live(resource)
        if live is not None:
            live.suspend(resource, "plan recording")
        try:
            self.ctx.pools.devices.record_start(resource, dest)
        except PoolError as error:
            if live is not None:
                live.resume(resource)
            raise DriverError("driver_error", f"the recording of {resource} did not start: {error}") from error
        with self.ctx.pools.host.lock:
            device = self.ctx.pools.host.devices.get(resource)
            if device is not None:
                device["recordingSid"] = sid
                self.ctx.pools.host.save()

    def record_stop(self, resource: str, out: Path, *, trim: bool, events: list[float]) -> Path:
        live = self._live(resource)
        try:
            info = self.ctx.pools.devices.record_stop(resource)
        except PoolError as error:
            raise DriverError("driver_error", f"the recording of {resource} could not be stopped: {error}") from error
        finally:
            with self.ctx.pools.host.lock:
                (self.ctx.pools.host.devices.get(resource) or {}).pop("recordingSid", None)
                self.ctx.pools.host.save()
            if live is not None:
                live.resume(resource)
        raw = Path(info["path"])
        wall = info.get("wallSeconds")
        try:
            result = encode_recording(raw, out, pad_to=float(wall) if isinstance(wall, (int, float)) and wall > 0
                                      else None, trim=trim, events=events)
        except EncodeError as error:
            raise DriverError("driver_error", f"the recording of {resource} could not be encoded: {error}") from error
        finally:
            if raw.parent != out.parent:
                shutil.rmtree(raw.parent, ignore_errors=True)
            else:
                raw.unlink(missing_ok=True)
        chosen = result.trimmed or result.full
        if result.trimmed is not None and result.full != chosen:
            Path(result.full).unlink(missing_ok=True)
        return Path(chosen)


__all__ = ["DeviceCapture", "PoolDeviceCapture"]
