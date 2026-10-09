"""Background system-statistics sampler.

Runs as a single asyncio task. Every 2s it polls CPU, memory, and (where
available) GPU/temperature counters, then broadcasts a ``SystemSnapshot``
to the ``system`` topic of the dev-server WS hub. Failures inside any
probe are swallowed and the corresponding field is omitted so the loop
never dies.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from .dev.ws_hub import WSHub
from .dev.ws_protocol import SystemSnapshot, SystemSnapshotEvent
from .jobs import JobRegistry, get_registry

__all__ = ["SystemStatsSampler"]

_LOG = logging.getLogger(__name__)

_SAMPLE_INTERVAL_S = 2.0


class SystemStatsSampler:
    """Polls system metrics on a fixed cadence and pushes them through the hub."""

    def __init__(self, registry: JobRegistry | None = None) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._registry = registry or get_registry()
        # NVML state - initialised lazily on first GPU probe.
        self._pynvml: Any = None
        self._nvml_handle: Any = None
        self._nvml_ready = False
        # WMI state for CPU temp on Windows.
        self._wmi_ohm: Any = None
        self._wmi_tried = False

    def start(self, hub: WSHub) -> None:
        """Schedule the background sampler. Idempotent."""

        if self._task is not None:
            return
        # Prime psutil so the first cpu_percent() call returns a real value.
        try:
            import psutil

            psutil.cpu_percent(interval=None)
        except Exception:
            pass
        loop = asyncio.get_event_loop()
        self._stop_event = asyncio.Event()
        self._task = loop.create_task(self._run(hub), name="system-stats-sampler")

    async def stop(self) -> None:
        """Cancel the background task and wait for it to exit."""

        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            self._task = None
            self._stop_event = None

    async def _run(self, hub: WSHub) -> None:
        """Main sampling loop. Never raises out."""

        try:
            while True:
                snapshot = self._build_snapshot()
                event = SystemSnapshotEvent(payload=snapshot)
                try:
                    await hub.broadcast(
                        WSHub.TOPIC_SYSTEM,
                        event.model_dump(mode="json", exclude_none=True),
                    )
                except Exception:
                    _LOG.debug("stats: broadcast failed", exc_info=True)
                if self._stop_event is not None:
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=_SAMPLE_INTERVAL_S
                        )
                        return
                    except asyncio.TimeoutError:
                        continue
                else:  # pragma: no cover - defensive
                    await asyncio.sleep(_SAMPLE_INTERVAL_S)
        except asyncio.CancelledError:
            return

    def _build_snapshot(self) -> SystemSnapshot:
        """Probe every available counter; omit anything that errors."""

        cpu_pct = 0.0
        mem_pct = 0.0
        gpu_pct: float | None = None
        cpu_temp_c: float | None = None
        gpu_temp_c: float | None = None

        try:
            import psutil

            cpu_pct = float(psutil.cpu_percent(interval=None))
            mem_pct = float(psutil.virtual_memory().percent)
        except Exception:
            _LOG.debug("stats: psutil cpu/mem probe failed", exc_info=True)

        gpu_pct, gpu_temp_c = self._probe_gpu()
        cpu_temp_c = self._probe_cpu_temp()
        render_pct = self._probe_render_pct()

        return SystemSnapshot(
            ts=time.time(),
            cpu_pct=cpu_pct,
            mem_pct=mem_pct,
            gpu_pct=gpu_pct,
            cpu_temp_c=cpu_temp_c,
            gpu_temp_c=gpu_temp_c,
            render_pct=render_pct,
        )

    def _probe_gpu(self) -> tuple[float | None, float | None]:
        """Return (gpu_pct, gpu_temp_c) via pynvml, or (None, None) on failure."""

        if not self._nvml_ready:
            try:
                import pynvml  # lazy

                pynvml.nvmlInit()
                if pynvml.nvmlDeviceGetCount() > 0:
                    self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                    self._pynvml = pynvml
                    self._nvml_ready = True
                else:
                    self._nvml_ready = True  # mark as "tried, nothing here"
            except Exception:
                self._nvml_ready = True
                return None, None

        if self._pynvml is None or self._nvml_handle is None:
            return None, None

        gpu_pct: float | None = None
        gpu_temp_c: float | None = None
        try:
            util = self._pynvml.nvmlDeviceGetUtilizationRates(self._nvml_handle)
            gpu_pct = float(util.gpu)
        except Exception:
            gpu_pct = None
        try:
            temp = self._pynvml.nvmlDeviceGetTemperature(
                self._nvml_handle, self._pynvml.NVML_TEMPERATURE_GPU
            )
            gpu_temp_c = float(temp)
        except Exception:
            gpu_temp_c = None
        return gpu_pct, gpu_temp_c

    def _probe_cpu_temp(self) -> float | None:
        """Best-effort CPU temperature in Celsius. None if unavailable."""

        # Linux / macOS path via psutil.
        if not sys.platform.startswith("win"):
            try:
                import psutil

                fn = getattr(psutil, "sensors_temperatures", None)
                if fn is None:
                    return None
                temps = fn()
                if not temps:
                    return None
                # Prefer "coretemp" if present; otherwise take the first reading.
                preferred = temps.get("coretemp") or next(iter(temps.values()))
                if not preferred:
                    return None
                first = preferred[0]
                return float(getattr(first, "current", 0.0)) or None
            except Exception:
                return None

        # Windows path via WMI / OpenHardwareMonitor.
        if not self._wmi_tried:
            self._wmi_tried = True
            try:
                import wmi  # lazy

                try:
                    self._wmi_ohm = wmi.WMI(namespace="root\\OpenHardwareMonitor")
                except Exception:
                    self._wmi_ohm = None
            except Exception:
                self._wmi_ohm = None
        if self._wmi_ohm is None:
            return None
        try:
            sensors = self._wmi_ohm.Sensor()
            for sensor in sensors:
                if getattr(sensor, "SensorType", "") == "Temperature" and "CPU" in (
                    getattr(sensor, "Name", "") or ""
                ):
                    val = float(getattr(sensor, "Value", 0.0))
                    if val > 0:
                        return val
        except Exception:
            return None
        return None

    def _probe_render_pct(self) -> float | None:
        """Latest progress (0..1) of any in-flight render, else None."""

        try:
            running = [r for r in self._registry.all() if r.status == "running"]
            if not running:
                return None
            running.sort(key=lambda r: r.updated_at, reverse=True)
            return float(running[0].progress)
        except Exception:
            return None
