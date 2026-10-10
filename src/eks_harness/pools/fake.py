from __future__ import annotations

import os
import re
import shutil
import struct
import threading
import time
import zlib
from collections.abc import Iterator
from pathlib import Path

from eks_harness.pools.base import (
    AppTargets,
    device_name,
    BackendError,
    BackendManager,
    BrowserPool,
    DevicePool,
    LiveSource,
    PoolError,
    PoolHost,
    browser_resource,
    device_key,
    iso_now,
    parse_browser_resource,
    parse_device_key,
)
from eks_harness.pools.fake_media import TINY_JPEG, TINY_MP4, TINY_MP4_DURATION_MS, TINY_MP4_SIZE

FAKE_ENV = "EKS_HARNESS_FAKE_POOLS"
FAKE_SCREENSHOT_SIZE = (4, 8)


def fake_enabled(env: dict | None = None) -> bool:
    value = (env if env is not None else os.environ).get(FAKE_ENV, "")
    return value.strip().lower() in ("1", "true", "yes", "on")


def png_bytes(width: int = 4, height: int = 8, rgb: tuple[int, int, int] = (242, 242, 242)) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def mp4_bytes() -> bytes:
    return TINY_MP4


def jpeg_bytes() -> bytes:
    return TINY_JPEG


class FakeLiveSource(LiveSource):
    def __init__(self, resource: str, fps: float = 5.0, limit: int | None = None) -> None:
        self.resource = resource
        self.interval = 1.0 / max(fps, 0.1)
        self.limit = limit
        self._stop = threading.Event()

    def frames(self) -> Iterator[bytes]:
        produced = 0
        while not self._stop.is_set():
            yield TINY_JPEG
            produced += 1
            if self.limit is not None and produced >= self.limit:
                return
            self._stop.wait(self.interval)

    def close(self) -> None:
        self._stop.set()


class FakeBrowserPool(BrowserPool):
    def __init__(self, host: PoolHost) -> None:
        super().__init__(host)
        self._next_pid = 900000

    def capacity(self) -> int:
        return int(self.config["browser.instances"]) * int(self.config["browser.profilesPerInstance"])

    def user_data_dir(self, index: int) -> Path:
        path = self.host.paths.browser_dir / str(index)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_path(self, index: int) -> Path:
        return self.host.home / f"browser-{index}.log"

    def _log(self, index: int, message: str) -> None:
        self.log_path(index).parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path(index), "a", encoding="utf-8") as handle:
            handle.write(f"[{iso_now()}] {message}\n")

    def alive(self, index: int) -> bool:
        return bool((self.host.browsers.get(str(index)) or {}).get("fakeRunning"))

    def allocate(self, taken: set[str]) -> str | None:
        per = int(self.config["browser.profilesPerInstance"])
        counts = {i: sum(1 for r in taken if r.startswith(f"browser:{i}:")) for i in self.indices()}
        running = [i for i in counts if self.alive(i) and counts[i] < per]
        order = sorted(running, key=lambda i: -counts[i]) + [i for i in counts if counts[i] < per and i not in running]
        for index in order:
            for profile in range(1, per + 1):
                resource = browser_resource(index, profile)
                if resource not in taken:
                    return resource
        return None

    def ensure(self, index: int) -> str:
        with self.host.lock:
            if self.alive(index):
                record = self.host.browsers[str(index)]
                record["last_used"] = time.time()
                return record["cdp"]
            self._next_pid += 1
            record = {
                "pid": self._next_pid, "started": iso_now(), "port": 0, "cdp": f"fake://browser/{index}",
                "binary": f"fake-{self.config['browser.command']}", "launched": time.time(), "last_used": time.time(),
                "fakeRunning": True,
            }
            self.host.browsers[str(index)] = record
            self.user_data_dir(index)
            self.host.save()
        self._log(index, f"fake browser {index} started")
        self.host.log(f"fake browser {index} started")
        self.host.emit("browser.status", resource=f"browser:{index}", detail={"status": "running"})
        return record["cdp"]

    def stop(self, index: int, reason: str) -> None:
        with self.host.lock:
            record = self.host.browsers.pop(str(index), None)
            self.host.save()
        if record:
            self._log(index, f"fake browser {index} stopped ({reason})")
            self.host.emit("browser.status", resource=f"browser:{index}", detail={"status": "stopped", "reason": reason})

    def processes(self) -> list[dict]:
        out = []
        for index in self.indices():
            record = dict(self.host.browsers.get(str(index)) or {})
            out.append({"index": index, "alive": self.alive(index), **record})
        return out

    def reset(self, resource: str) -> None:
        index = self._index(resource)
        self.stop(index, "reset")
        shutil.rmtree(self.user_data_dir(index), ignore_errors=True)
        self.user_data_dir(index)

    def delete(self, resource: str) -> None:
        index = self._index(resource)
        self.stop(index, "deleted")
        shutil.rmtree(self.user_data_dir(index), ignore_errors=True)

    def close_profile(self, resource: str, context_ids: list[str]) -> list[str]:
        index = self._index(resource)
        if not self.alive(index):
            return []
        closed = [str(c) for c in context_ids if c]
        self._log(index, f"fake profile {resource}: closed {len(closed)} browser context(s)")
        return closed

    def _index(self, resource: str) -> int:
        if resource.count(":") == 2:
            return parse_browser_resource(resource)[0]
        text = resource.split(":")[-1]
        if not text.isdigit():
            raise PoolError(f"unknown browser resource {resource}")
        return int(text)

    def sweep(self) -> dict:
        return {"headlessBrowsers": [], "strayBrowsers": [], "fake": True}

    def live_source(self, resource: str) -> LiveSource:
        index = self._index(resource)
        if not self.alive(index):
            raise PoolError(f"browser {index} is not running")
        return FakeLiveSource(resource, fps=float(self.config["live.maxFps"]))


class FakeDevicePool(DevicePool):
    def __init__(self, host: PoolHost) -> None:
        super().__init__(host)
        self._recordings: dict[str, dict] = {}

    def define(self) -> None:
        devices = self.host.devices
        wanted = set()
        with self.host.lock:
            for index in range(1, int(self.config["devices.ios"]) + 1):
                key = device_key("ios", index)
                wanted.add(key)
                record = devices.setdefault(key, {"kind": "ios", "index": index, "name": device_name(self.config, "ios", index)})
                record.setdefault("udid", f"FAKE-IOS-{index:04d}")
                record.setdefault("status", "off")
            base = int(self.config["devices.androidPortBase"])
            for index in range(1, int(self.config["devices.android"]) + 1):
                key = device_key("android", index)
                wanted.add(key)
                port = base + 2 * (index - 1)
                record = devices.setdefault(key, {"kind": "android", "index": index,
                                                  "name": device_name(self.config, "android", index)})
                record.update({"port": port, "serial": f"emulator-{port}"})
                record.setdefault("status", "off")
            for key in [k for k in devices if k not in wanted]:
                if devices[key].get("status") in ("on", "booting"):
                    devices[key]["retired"] = True
                else:
                    devices.pop(key)
            for record in devices.values():
                if record.get("status") in ("booting", "stopping"):
                    record["status"] = "off"
            self.host.save()

    def available(self, kind: str) -> tuple[bool, str]:
        if kind not in ("ios", "android"):
            return False, f"unknown device kind {kind}"
        return True, "fake device pool"

    def _device(self, key: str) -> dict:
        device = self.host.devices.get(key)
        if device is None:
            parse_device_key(key)
            raise PoolError(f"no device {key} in the pool")
        return device

    def log_path(self, key: str) -> Path:
        return self.host.home / f"device-{key.replace(':', '-')}.log"

    def _log(self, key: str, message: str) -> None:
        self.log_path(key).parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path(key), "a", encoding="utf-8") as handle:
            handle.write(f"[{iso_now()}] {message}\n")

    def _set_status(self, key: str, status: str, **extra) -> None:
        with self.host.lock:
            device = self._device(key)
            device["status"] = status
            device["statusSince"] = time.time()
            device.update(extra)
            self.host.save()
        self._log(key, f"status {status}")
        self.host.emit("device.status", resource=key, detail={"status": status})

    def allocate(self, kind: str, taken: set[str]) -> str | None:
        devices = self.host.devices
        free = [k for k, d in devices.items() if d["kind"] == kind and k not in taken and not d.get("retired")]
        if not free:
            return None
        warm = [k for k in free if devices[k].get("status") == "on"]
        if warm:
            return sorted(warm, key=lambda k: devices[k].get("last_used", 0), reverse=True)[0]
        running = [k for k, d in devices.items() if d.get("status") in ("on", "booting")]
        if len(running) >= int(self.config["devices.maxRunning"]):
            idle = [k for k in running if k not in taken and devices[k].get("status") == "on"]
            if not idle:
                return None
            victim = sorted(idle, key=lambda k: devices[k].get("last_used", 0))[0]
            self.shutdown(victim)
        return sorted(free, key=lambda k: devices[k]["index"])[0]

    def boot(self, key: str) -> None:
        self._set_status(key, "booting")
        self._set_status(key, "on", last_used=time.time())

    def shutdown(self, key: str) -> None:
        if key in self._recordings:
            self.record_stop(key)
        self._set_status(key, "off")
        with self.host.lock:
            device = self.host.devices.get(key)
            if device and device.get("retired"):
                self.host.devices.pop(key, None)
                self.host.save()

    def clean_app(self, key: str, apps: AppTargets) -> None:
        self._device(key)
        removed = apps.ios if key.startswith("ios") else apps.android
        self._log(key, f"removed {', '.join(removed) if removed else 'no apps'}")

    def refresh(self) -> None:
        return None

    def reset(self, key: str) -> None:
        self.shutdown(key)
        self._log(key, "erased")

    def delete(self, key: str) -> None:
        self.shutdown(key)
        with self.host.lock:
            device = self._device(key)
            device.pop("udid", None)
            if device["kind"] == "ios":
                device["udid"] = f"FAKE-IOS-{device['index']:04d}-{int(time.time())}"
            self.host.save()
        self._log(key, "deleted; recreated on demand")

    def sweep(self) -> dict:
        return {"legacySimulators": [], "strayEmulators": [], "fake": True}

    def _require_on(self, key: str) -> dict:
        device = self._device(key)
        if device.get("status") != "on":
            raise PoolError(f"{device['name']} is not running")
        return device

    def screenshot(self, key: str, dest: Path) -> dict:
        self._require_on(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(png_bytes(*FAKE_SCREENSHOT_SIZE))
        return {"path": str(dest), "mime": "image/png", "width": FAKE_SCREENSHOT_SIZE[0],
                "height": FAKE_SCREENSHOT_SIZE[1], "device": self._device(key)["name"]}

    def record_start(self, key: str, dest: Path) -> None:
        self._require_on(key)
        if key in self._recordings:
            raise PoolError(f"{self._device(key)['name']} is already recording")
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._recordings[key] = {"dest": dest, "started": time.time()}

    def record_stop(self, key: str) -> dict:
        recording = self._recordings.pop(key, None)
        if recording is None:
            raise PoolError(f"{self._device(key)['name']} is not recording")
        dest: Path = recording["dest"]
        dest.write_bytes(mp4_bytes())
        return {"path": str(dest), "mime": "video/mp4", "width": TINY_MP4_SIZE[0], "height": TINY_MP4_SIZE[1],
                "durationMs": TINY_MP4_DURATION_MS, "startedAt": recording["started"],
                "device": self._device(key)["name"]}

    def is_recording(self, key: str) -> bool:
        return key in self._recordings

    def record_reset(self, key: str) -> dict:
        device = self._device(key)
        recording = self._recordings.pop(key, None)
        if recording is not None:
            Path(recording["dest"]).unlink(missing_ok=True)
        with self.host.lock:
            device.pop("recordingSid", None)
            self.host.save()
        self._log(key, "recorder reset")
        return {"device": device["name"], "stoppedRecorder": recording is not None, "orphan": False,
                "restarted": False}

    def live_source(self, key: str) -> LiveSource:
        self._require_on(key)
        return FakeLiveSource(key, fps=float(self.config["live.maxFps"]))

    def device_log(self, key: str, since: float | None, dest: Path) -> dict:
        device = self._device(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{iso_now()} fake log line {n} from {device['name']}" for n in range(1, 4)]
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"path": str(dest), "mime": "text/plain", "lines": len(lines), "since": since}


class FakeBackendManager(BackendManager):
    def definitions(self, tree: str | None = None) -> list[dict]:
        found = []
        places = []
        if tree:
            for relative in self.host.config["backend.definitionDirs"]:
                places.append(("tree", Path(tree) / relative))
        places.append(("config", self.host.paths.backends_dir))
        for source, folder in places:
            if folder.is_dir():
                for entry in sorted(folder.iterdir()):
                    if entry.is_file() and not entry.name.startswith((".", "_")):
                        found.append({"name": entry.stem, "path": str(entry), "source": source, "tree": tree})
        names = {item["name"] for item in found}
        from eks_harness.backends.registry import plugin_definitions

        found.extend(item for item in plugin_definitions(self.host.plugin_host(), tree) if item["name"] not in names)
        return found

    def find_definition(self, name: str, tree: str | None) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name or ""):
            raise BackendError(f"invalid backend definition name: {name}")
        for item in self.definitions(tree):
            if item["name"] == name:
                return Path(item["path"])
        return Path(f"fake://backends/{name}")

    def log_path(self, record: dict) -> Path:
        return self.host.home / "backends" / f"{record['id'].replace('/', '_').replace('@', '_')}.log"

    def _log(self, record: dict, message: str) -> None:
        path = self.log_path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"[{iso_now()}] {message}\n")

    def process_alive(self, record: dict, name: str) -> bool:
        return record.get("status") == "running"

    def _allocate_ports(self, record: dict, names: list[str]) -> dict:
        ports = dict(record.get("ports") or {})
        reserved = {p for other in self.records.values() if other["id"] != record["id"]
                    for p in (other.get("ports") or {}).values()}
        start, end = int(self.host.config["backend.portRangeStart"]), int(self.host.config["backend.portRangeEnd"])
        mine = set(ports.values())
        for name in names:
            if name in ports:
                continue
            for candidate in range(start, end + 1):
                if candidate not in reserved and candidate not in mine:
                    ports[name] = candidate
                    mine.add(candidate)
                    break
            else:
                raise BackendError(f"no free port in {start}-{end} for {name}")
        return ports

    def ensure(self, body: dict) -> dict:
        definition, instance = body.get("definition"), body.get("instance")
        if not definition or not instance:
            raise BackendError("definition and instance are required")
        backend_id = body.get("id") or self.backend_id(definition, instance)
        with self.host.lock:
            record = self.records.get(backend_id)
            if record is None:
                record = {"id": backend_id, "definition": definition, "instance": instance, "status": "stopped",
                          "created": time.time()}
                self.records[backend_id] = record
            record["tree"] = body.get("tree") or record.get("tree")
            record["definitionPath"] = str(self.find_definition(definition, record["tree"]))
            hold = float(body.get("hold", 300))
            if hold > 0:
                record.setdefault("holds", {})[body.get("holdId") or "ensure"] = time.time() + hold
            record.pop("emptySince", None)
            restart = bool(body.get("restart"))
            if record["status"] != "running" or restart:
                record["ports"] = self._allocate_ports(record, ["api", "proxy"])
                record["processes"] = ["fake-api"]
                record["captured"] = {"url": f"http://127.0.0.1:{record['ports']['api']}"}
                record["exports"] = {"EKS_LOCAL_API_URL": f"http://127.0.0.1:{record['ports']['proxy']}"}
                record["logs"] = {"fake-api": str(self.log_path(record))}
                record["fingerprint"] = "fake"
                record["description"] = f"fake {definition} backend"
                record["status"] = "running"
                record["startedAt"] = time.time()
                record.pop("error", None)
                self._log(record, f"fake backend {backend_id} running")
            self.host.save()
        self.host.emit("backend.status", resource=f"backend:{backend_id}", detail={"status": record["status"]})
        return self.public(record)

    def adopt(self, backend_id: str) -> None:
        record = self.records.get(backend_id)
        if record is None:
            return
        if record.get("status") == "stopping":
            self.stop(backend_id, final=bool(record.get("stopFinal")), reason="re-adopted after a restart")
            return
        with self.host.lock:
            record["status"] = "running"
            record.setdefault("startedAt", time.time())
            self.host.save()
        self._log(record, f"fake backend {backend_id} re-adopted after a restart")
        self.host.emit("backend.status", resource=f"backend:{backend_id}",
                       detail={"status": "running", "adopted": True})

    def stop(self, backend_id: str, final: bool = False, reason: str = "") -> None:
        with self.host.lock:
            record = self.records.get(backend_id)
            if record is None:
                return
            record["status"] = "stopped"
            record["stoppedAt"] = time.time()
            record["holds"] = {}
            record.pop("emptySince", None)
            if final:
                self.records.pop(backend_id, None)
            self.host.save()
        self._log(record, f"fake backend {backend_id} stopped ({reason}){' final' if final else ''}")
        self.host.emit("backend.status", resource=f"backend:{backend_id}",
                       detail={"status": "stopped", "reason": reason, "final": final})
