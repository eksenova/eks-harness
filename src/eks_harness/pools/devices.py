from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import struct
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from eks_harness.pools.backends import (
    WINDOWS,
    ProcessRegistry,
    alive,
    descendant_pids,
    detached_kwargs,
    kill_group,
    pid_started,
    process_table,
    repo_cache_root,
)
from eks_harness.pools.base import DevicePool as DevicePoolBase
from eks_harness.pools.base import (AppTargets, LiveSource, PoolError, PoolHost, device_key, device_name, iso_now,
                                    parse_device_key)

MOBILE_DRIVER_MARKER = "eks-harness-mobile-driver"
IOS_READY_TEXT = "Recording started"
RECORD_STOP_TIMEOUT = 20.0
ANY_STATUS = object()
SIMULATOR_APP_ID = "com.apple.iphonesimulator"
DEVICE_HUB_APP_ID = "com.apple.dt.Devices"
JDK_FORMULAS = ("openjdk@17", "openjdk")
HOMEBREW_PREFIXES = (Path("/opt/homebrew"), Path("/usr/local"))
MACOS_JAVA_HOME = Path("/usr/libexec/java_home")


def sh(args: list[str], timeout: float = 60, env: dict | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        return result.returncode, (result.stdout + result.stderr).strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)


def android_sdk() -> Path | None:
    for candidate in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                      str(Path.home() / "Library" / "Android" / "sdk"), str(Path.home() / "Android" / "Sdk"),
                      str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk")
                      if os.environ.get("LOCALAPPDATA") else None):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return None


def tool(name: str) -> str | None:
    sdk = android_sdk()
    if sdk is not None:
        places = {
            "adb": sdk / "platform-tools" / "adb",
            "emulator": sdk / "emulator" / "emulator",
            "avdmanager": sdk / "cmdline-tools" / "latest" / "bin" / "avdmanager",
        }
        path = places.get(name)
        if path and path.exists():
            return str(path)
        if path and path.with_suffix(".exe").exists():
            return str(path.with_suffix(".exe"))
        if path and path.with_suffix(".bat").exists():
            return str(path.with_suffix(".bat"))
    return shutil.which(name)


def jdk_home(candidate: str | Path | None) -> Path | None:
    if not candidate:
        return None
    root = Path(candidate)
    for home in (root / "libexec" / "openjdk.jdk" / "Contents" / "Home", root / "Contents" / "Home", root):
        if (home / "bin" / "java").is_file() or (home / "bin" / "java.exe").is_file():
            return home
    return None


def homebrew() -> str | None:
    found = shutil.which("brew")
    if found:
        return found
    for prefix in HOMEBREW_PREFIXES:
        if (prefix / "bin" / "brew").is_file():
            return str(prefix / "bin" / "brew")
    return None


def resolve_java_home() -> tuple[Path, str] | None:
    found = jdk_home(os.environ.get("JAVA_HOME"))
    if found:
        return found, "JAVA_HOME"
    if platform.system() == "Darwin" and MACOS_JAVA_HOME.exists():
        code, out = sh([str(MACOS_JAVA_HOME)], timeout=15)
        found = jdk_home(out.splitlines()[-1].strip()) if code == 0 and out else None
        if found:
            return found, str(MACOS_JAVA_HOME)
    brew = homebrew()
    for formula in JDK_FORMULAS:
        if brew:
            code, out = sh([brew, "--prefix", formula], timeout=30)
            found = jdk_home(out.splitlines()[-1].strip()) if code == 0 and out else None
            if found:
                return found, f"brew --prefix {formula}"
        for prefix in HOMEBREW_PREFIXES:
            found = jdk_home(prefix / "opt" / formula)
            if found:
                return found, f"Homebrew {formula}"
    return None


def java_env() -> tuple[dict, str | None]:
    env = dict(os.environ)
    resolved = resolve_java_home()
    if resolved is None:
        return env, None
    home, source = resolved
    env["JAVA_HOME"] = str(home)
    env["PATH"] = os.pathsep.join([str(home / "bin"), env.get("PATH", "")]).rstrip(os.pathsep)
    return env, source


@dataclass(frozen=True)
class SimulatorUI:
    name: str
    path: Path
    bundle_id: str

    def open_command(self, udid: str) -> list[str]:
        if self.bundle_id == DEVICE_HUB_APP_ID:
            return ["open", "-g", "-a", str(self.path), f"devices://device/open?id={udid}"]
        return ["open", "-g", "-a", str(self.path), "--args", "-CurrentDeviceUDID", udid]

    def quit_command(self) -> list[str]:
        return ["osascript", "-e", f'quit app id "{self.bundle_id}"']

    def running(self) -> bool:
        marker = f"{self.path}/Contents/MacOS/"
        return any(marker in command for _, command in process_table().values())


def developer_dir() -> Path | None:
    code, out = sh(["xcode-select", "-p"], timeout=15)
    if code != 0 or not out:
        return None
    return Path(out.splitlines()[-1].strip())


def spotlight_apps(bundle_id: str) -> list[Path]:
    code, out = sh(["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"], timeout=15)
    return [Path(line.strip()) for line in out.splitlines() if line.strip()] if code == 0 else []


def simulator_ui_candidates():
    developer = developer_dir()
    if developer is not None:
        yield "Simulator", developer / "Applications" / "Simulator.app", SIMULATOR_APP_ID
        yield "DeviceHub", developer.parent / "Applications" / "DeviceHub.app", DEVICE_HUB_APP_ID
    for path in spotlight_apps(SIMULATOR_APP_ID):
        yield "Simulator", path, SIMULATOR_APP_ID
    for path in spotlight_apps(DEVICE_HUB_APP_ID):
        yield "DeviceHub", path, DEVICE_HUB_APP_ID


def simulator_ui() -> SimulatorUI | None:
    for name, path, bundle_id in simulator_ui_candidates():
        if (path / "Contents" / "Info.plist").is_file():
            return SimulatorUI(name, path, bundle_id)
    return None


def avd_home() -> Path:
    override = os.environ.get("ANDROID_AVD_HOME")
    return Path(override) if override else Path.home() / ".android" / "avd"


def png_size(path: Path) -> tuple[int | None, int | None]:
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:
        return None, None
    if len(head) >= 24 and head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        width, height = struct.unpack(">II", head[16:24])
        return width, height
    return None, None


def probe_video(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    code, out = sh([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                    "stream=width,height:format=duration", "-of", "json", str(path)], timeout=60)
    if code != 0:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    stream = (data.get("streams") or [{}])[0]
    info: dict = {}
    if stream.get("width"):
        info["width"] = int(stream["width"])
        info["height"] = int(stream["height"])
    duration = (data.get("format") or {}).get("duration")
    if duration not in (None, "N/A"):
        info["durationMs"] = int(float(duration) * 1000)
    return info


class DevicePool(DevicePoolBase):
    def __init__(self, host: PoolHost) -> None:
        super().__init__(host)
        self.repo_root = repo_cache_root(host.paths)
        self.registry = ProcessRegistry(self.repo_root)

    @property
    def devices(self) -> dict[str, dict]:
        return self.host.devices

    def define(self) -> None:
        devices = self.host.devices
        wanted = set()
        with self.host.lock:
            for index in range(1, int(self.config["devices.ios"]) + 1):
                key = device_key("ios", index)
                wanted.add(key)
                devices.setdefault(key, {"kind": "ios", "index": index, "name": device_name(self.config, "ios", index)})
            base = int(self.config["devices.androidPortBase"])
            for index in range(1, int(self.config["devices.android"]) + 1):
                key = device_key("android", index)
                wanted.add(key)
                port = base + 2 * (index - 1)
                record = devices.setdefault(key, {"kind": "android", "index": index,
                                                  "name": device_name(self.config, "android", index)})
                record.update({"port": port, "serial": f"emulator-{port}"})
            for key in [k for k in devices if k not in wanted]:
                record = devices[key]
                if record.get("status") in ("on", "booting"):
                    record["retired"] = True
                else:
                    devices.pop(key)
            for record in devices.values():
                if record.get("status") in ("booting", "stopping"):
                    record["status"] = "unknown"
                record.pop("busy", None)
            self.host.save()

    def available(self, kind: str) -> tuple[bool, str]:
        if kind == "ios":
            if platform.system() != "Darwin" or not shutil.which("xcrun"):
                return False, "xcrun not found (iOS simulators need macOS with Xcode)"
            return True, "xcrun"
        if kind == "android":
            if not tool("emulator") or not tool("adb"):
                return False, "Android emulator/adb not found (set ANDROID_HOME)"
            return True, "android sdk"
        return False, f"unknown device kind {kind}"

    def device(self, key: str) -> dict:
        record = self.host.devices.get(key)
        if record is None:
            parse_device_key(key)
            raise PoolError(f"no device {key} in the pool")
        return record

    def log_path(self, key: str) -> Path:
        return self.host.home / f"device-{key.replace(':', '-')}.log"

    def set_status(self, key: str, status: str, expect: object = ANY_STATUS, **extra) -> None:
        with self.host.lock:
            device = self.host.devices.get(key)
            if device is None:
                return
            if expect is not ANY_STATUS and (device.get("status") != expect or device.get("busy")):
                return
            changed = device.get("status") != status
            device["status"] = status
            if changed:
                device["statusSince"] = time.time()
            device.update(extra)
            self.host.save()
        if changed:
            self.host.emit("device.status", resource=key, detail={"status": status, "name": device.get("name")})

    def allocate(self, kind: str, taken: set[str]) -> str | None:
        devices = self.host.devices
        free = [k for k, d in devices.items() if d["kind"] == kind and k not in taken and not d.get("retired")
                and not d.get("busy")]
        if not free:
            return None
        running = [k for k, d in devices.items() if d.get("status") in ("on", "booting")]
        warm = [k for k in free if devices[k].get("status") == "on"]
        if warm:
            return sorted(warm, key=lambda k: devices[k].get("last_used", 0), reverse=True)[0]
        if len(running) >= int(self.config["devices.maxRunning"]):
            idle = [k for k in running if k not in taken and devices[k].get("status") == "on"
                    and not devices[k].get("busy")]
            if not idle:
                return None
            victim = sorted(idle, key=lambda k: devices[k].get("last_used", 0))[0]
            self.host.log(f"{devices[victim]['name']} is idle; shutting it down to make room for {kind}")
            devices[victim]["status"] = "stopping"
            self.host.spawn(self.shutdown, victim)
        return sorted(free, key=lambda k: devices[k]["index"])[0]

    def ios_list(self) -> dict:
        if not shutil.which("xcrun"):
            return {}
        code, out = sh(["xcrun", "simctl", "list", "devices", "-j"], timeout=30)
        if code != 0:
            return {}
        try:
            return json.loads(out).get("devices", {})
        except json.JSONDecodeError:
            return {}

    def ios_udid(self, name: str) -> str | None:
        for group in self.ios_list().values():
            for device in group:
                if device.get("name") == name and device.get("isAvailable", True):
                    return device["udid"]
        return None

    def ios_state(self, udid: str) -> str:
        for group in self.ios_list().values():
            for device in group:
                if device.get("udid") == udid:
                    return device.get("state", "")
        return ""

    def newest_ios_runtime(self) -> str:
        code, out = sh(["xcrun", "simctl", "list", "runtimes", "available", "-j"])
        runtimes = [r for r in json.loads(out).get("runtimes", []) if r.get("platform") == "iOS"] if code == 0 else []
        if not runtimes:
            raise PoolError("no iOS simulator runtime is installed")
        return sorted(runtimes, key=lambda r: [int(x) for x in r["version"].split(".")])[-1]["identifier"]

    def ensure_ios(self, device: dict) -> str:
        runtime = self.newest_ios_runtime()
        for group_runtime, group in self.ios_list().items():
            for existing in group:
                if existing.get("name") != device["name"]:
                    continue
                available = existing.get("isAvailable", True)
                if available and (group_runtime == runtime or existing.get("state") == "Booted"):
                    device["udid"] = existing["udid"]
                    return existing["udid"]
                sh(["xcrun", "simctl", "delete", existing["udid"]], timeout=120)
                reason = "unavailable" if not available else f"on {group_runtime.rsplit('.', 1)[-1]}"
                self.host.log(f"pool simulator {device['name']} ({existing['udid']}) was {reason}; "
                              f"recreating it on {runtime.rsplit('.', 1)[-1]}")
        code, out = sh(["xcrun", "simctl", "list", "devicetypes", "-j"])
        types = json.loads(out).get("devicetypes", []) if code == 0 else []
        wanted = str(self.config["devices.iosDeviceType"])
        device_type = next((t["identifier"] for t in types if t.get("name") == wanted), None)
        if not device_type:
            raise PoolError(f"unknown simulator device type: {wanted}")
        code, out = sh(["xcrun", "simctl", "create", device["name"], device_type, runtime], timeout=120)
        if code != 0:
            raise PoolError(f"could not create the simulator: {out[-300:]}")
        device["udid"] = out.strip().splitlines()[-1]
        self.host.log(f"pool simulator created: {device['name']} ({device['udid']})")
        return device["udid"]

    def ensure_avd(self, device: dict) -> None:
        emulator = tool("emulator")
        if not emulator:
            raise PoolError("Android emulator not found (set ANDROID_HOME)")
        code, out = sh([emulator, "-list-avds"])
        if device["name"] in out.split():
            return
        avdmanager = tool("avdmanager")
        if not avdmanager:
            raise PoolError("avdmanager not found (Android cmdline-tools)")
        base_name = str(self.config["devices.androidBaseAvd"])
        base = avd_home() / f"{base_name}.avd" / "config.ini"
        config: dict[str, str] = {}
        if base.exists():
            for line in base.read_text().splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    config[key.strip()] = value.strip()
        sysdir = config.get("image.sysdir.1", "system-images/android-36/google_apis/arm64-v8a/").strip("/")
        package = sysdir.replace("/", ";")
        device_name = config.get("hw.device.name", "pixel_7")
        env = self.java_env()
        result = subprocess.run([avdmanager, "create", "avd", "-n", device["name"], "-k", package, "-d", device_name],
                                input="no\n", capture_output=True, text=True, timeout=180, env=env)
        if result.returncode != 0:
            raise PoolError(f"could not create the AVD: {(result.stdout + result.stderr)[-400:]}")
        ini = avd_home() / f"{device['name']}.avd" / "config.ini"
        if ini.exists():
            text = ini.read_text()
            for key, value in {"hw.ramSize": config.get("hw.ramSize", "2G"), "hw.keyboard": "yes",
                               "disk.dataPartition.size": config.get("disk.dataPartition.size", "6442450944")}.items():
                if f"{key}=" not in text.replace(" ", ""):
                    text += f"\n{key}={value}"
            ini.write_text(text + "\n")
        self.host.log(f"pool AVD created: {device['name']} ({package}, {device_name})")

    def java_env(self) -> dict:
        env, source = java_env()
        if source is None:
            self.host.log("warning: no Java runtime found for the Android SDK tools (JAVA_HOME, /usr/libexec/java_home, "
                          "Homebrew openjdk@17 or openjdk); install one with: brew install openjdk@17")
        elif source.startswith(("brew", "Homebrew")):
            self.host.log(f"JAVA_HOME={env['JAVA_HOME']} (from {source}; /usr/libexec/java_home does not see it)")
        return env

    def show_simulator(self, device: dict, udid: str) -> None:
        ui = simulator_ui()
        if ui is None:
            self.host.log(f"warning: {device['name']}: neither Simulator.app nor DeviceHub.app was found "
                          f"(xcode-select -p, Spotlight); the simulator runs without a window")
            return
        code, out = sh(ui.open_command(udid), timeout=30)
        if code != 0:
            self.host.log(f"warning: {device['name']}: could not open {ui.name} ({ui.path}) for {udid}; the simulator "
                          f"runs without a window: {out[-300:]}")

    def quit_simulator_ui(self) -> None:
        ui = simulator_ui()
        if ui is None or not ui.running():
            return
        code, out = sh(ui.quit_command(), timeout=15)
        if code != 0:
            self.host.log(f"warning: could not quit {ui.name} ({ui.path}): {out[-300:]}")

    def android_running(self, device: dict) -> bool:
        adb = tool("adb")
        if not adb or not device.get("serial"):
            return False
        code, out = sh([adb, "-s", device["serial"], "shell", "getprop", "sys.boot_completed"], timeout=10)
        return code == 0 and out.strip() == "1"

    def boot(self, key: str) -> None:
        device = self.device(key)
        self.set_status(key, "booting")
        try:
            if device["kind"] == "ios":
                udid = self.ensure_ios(device)
                if self.ios_state(udid) != "Booted":
                    sh(["xcrun", "simctl", "boot", udid], timeout=180)
                sh(["xcrun", "simctl", "bootstatus", udid, "-b"], timeout=300)
                if platform.system() == "Darwin":
                    self.show_simulator(device, udid)
            else:
                self.ensure_avd(device)
                if not self.android_running(device):
                    emulator, adb = tool("emulator"), tool("adb")
                    if not emulator or not adb:
                        raise PoolError("Android emulator/adb not found (set ANDROID_HOME)")
                    log_file = self.log_path(key)
                    log_file.parent.mkdir(parents=True, exist_ok=True)
                    running = self.running_emulator(device)
                    process: subprocess.Popen | None = None
                    if running is not None:
                        pid, started = running
                        self.host.log(f"{device['name']}: its emulator (pid {pid}) is still booting; waiting for it "
                                      f"instead of starting another")
                    else:
                        with open(log_file, "ab", buffering=0) as handle:
                            handle.write(f"\n==== {iso_now()} boot {device['name']}\n".encode())
                            process = subprocess.Popen(
                                [emulator, "-avd", device["name"], "-port", str(device["port"]), "-no-snapshot-save",
                                 "-no-boot-anim", "-crash-report-mode", "never", "-no-metrics", "-skip-adb-auth"],
                                stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, close_fds=True,
                                **detached_kwargs())
                        pid, started = process.pid, pid_started(process.pid)
                    with self.host.lock:
                        device["pid"] = pid
                        device["started"] = started
                        self.host.save()
                    sh([adb, "-s", device["serial"], "wait-for-device"], timeout=300)
                    deadline = time.time() + 300
                    while time.time() < deadline and not self.android_running(device):
                        exited = process.poll() is not None if process is not None else not alive(pid, started)
                        if exited:
                            raise PoolError(f"the emulator exited; log: {log_file}")
                        time.sleep(2)
                    if not self.android_running(device):
                        raise PoolError("the emulator did not boot within 5 minutes")
        except Exception:
            self.set_status(key, "off" if not self.running_now(device) else "on")
            raise
        self.set_status(key, "on", last_used=time.time())

    def running_emulator(self, device: dict) -> tuple[int, str] | None:
        pid, started = device.get("pid"), device.get("started")
        if pid and alive(pid, started):
            return int(pid), str(started or pid_started(int(pid)))
        if WINDOWS:
            return None
        wanted_avd, wanted_port = f"-avd {device['name']}", f"-port {device.get('port')}"
        for candidate, (_, command) in sorted(process_table().items()):
            if (f"{wanted_avd} " in f"{command} " and f"{wanted_port} " in f"{command} "
                    and ("emulator" in command or "qemu-system" in command)):
                return candidate, pid_started(candidate)
        return None

    def running_now(self, device: dict) -> bool:
        if device["kind"] == "ios":
            return bool(device.get("udid")) and self.ios_state(device["udid"]) == "Booted"
        return self.android_running(device)

    def fingerprints_dir(self) -> Path:
        return self.repo_root / "state" / "devices"

    def clean_app(self, key: str, apps: AppTargets) -> None:
        device = self.device(key)
        if device["kind"] == "ios" and device.get("udid"):
            udid = device["udid"]
            if self.ios_state(udid) == "Booted":
                for bundle in apps.ios:
                    sh(["xcrun", "simctl", "terminate", udid, bundle], timeout=30)
                    sh(["xcrun", "simctl", "uninstall", udid, bundle], timeout=60)
                sh(["xcrun", "simctl", "keychain", udid, "reset"], timeout=30)
            (self.fingerprints_dir() / f"{udid}.ios-fingerprint").unlink(missing_ok=True)
        elif device["kind"] == "android":
            adb = tool("adb")
            if adb and self.android_running(device):
                for package in apps.android:
                    sh([adb, "-s", device["serial"], "uninstall", package], timeout=60)
                sh([adb, "-s", device["serial"], "reverse", "--remove-all"], timeout=15)
            folder = self.fingerprints_dir()
            if folder.is_dir():
                for marker in folder.glob(f"{device['serial']}-*.android-fingerprint"):
                    marker.unlink(missing_ok=True)

    def shutdown(self, key: str) -> None:
        device = self.host.devices.get(key)
        if not device:
            return
        if device.get("recording"):
            try:
                self.record_stop(key)
            except Exception as error:
                self.host.log(f"{device.get('name', key)}: recording could not be finished before shutdown: {error}")
            self.forget_recording(device)
        self.set_status(key, "stopping")
        if device["kind"] == "ios" and device.get("udid"):
            sh(["xcrun", "simctl", "shutdown", device["udid"]], timeout=120)
        elif device["kind"] == "android":
            adb = tool("adb")
            if adb and device.get("serial"):
                sh([adb, "-s", device["serial"], "emu", "kill"], timeout=30)
            if device.get("pid"):
                kill_group(device["pid"], device.get("started"), grace=15)
        with self.host.lock:
            device.pop("pid", None)
            device.pop("started", None)
            if device.get("retired"):
                self.host.devices.pop(key, None)
            self.host.save()
        self.set_status(key, "off")
        self.host.log(f"device shut down: {device['name']}")
        if device["kind"] == "ios" and platform.system() == "Darwin":
            code, out = sh(["xcrun", "simctl", "list", "devices", "booted"], timeout=30)
            if code == 0 and "(Booted)" not in out:
                self.quit_simulator_ui()

    def refresh(self) -> None:
        with self.host.lock:
            snapshot = {key: dict(device) for key, device in self.host.devices.items()
                        if device.get("status") not in ("booting", "stopping") and not device.get("busy")}
        listing = self.ios_list() if any(d["kind"] == "ios" for d in snapshot.values()) else {}
        ios_by_name: dict[str, dict] = {}
        ios_by_udid: dict[str, dict] = {}
        for group in listing.values():
            for entry in group:
                ios_by_udid[entry.get("udid", "")] = entry
                if entry.get("isAvailable", True):
                    ios_by_name.setdefault(entry.get("name", ""), entry)
        observed: dict[str, tuple[str, str | None]] = {}
        for key, device in snapshot.items():
            if device["kind"] == "ios":
                found = ios_by_udid.get(device.get("udid") or "") or ios_by_name.get(device["name"])
                udid = found.get("udid") if found else None
                observed[key] = ("on" if found and found.get("state") == "Booted" else "off", udid)
            else:
                observed[key] = ("on" if self.android_running(device) else "off", None)
        for key, (status, udid) in observed.items():
            extra = {"udid": udid} if udid else {}
            self.set_status(key, status, expect=snapshot[key].get("status"), **extra)

    def reset(self, key: str) -> None:
        device = self.device(key)
        self.shutdown(key)
        if device["kind"] == "ios":
            udid = device.get("udid") or self.ios_udid(device["name"])
            if udid:
                code, out = sh(["xcrun", "simctl", "erase", udid], timeout=180)
                if code != 0:
                    raise PoolError(f"could not erase {device['name']}: {out[-300:]}")
                (self.fingerprints_dir() / f"{udid}.ios-fingerprint").unlink(missing_ok=True)
        else:
            folder = avd_home() / f"{device['name']}.avd"
            if folder.is_dir():
                for pattern in ("userdata-qemu.img*", "cache.img*", "*.lock"):
                    for item in folder.glob(pattern):
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink(missing_ok=True)
                shutil.rmtree(folder / "snapshots", ignore_errors=True)
            marker_dir = self.fingerprints_dir()
            if marker_dir.is_dir() and device.get("serial"):
                for marker in marker_dir.glob(f"{device['serial']}-*.android-fingerprint"):
                    marker.unlink(missing_ok=True)
        self.host.log(f"device erased: {device['name']}")

    def delete(self, key: str) -> None:
        device = self.device(key)
        self.shutdown(key)
        if device["kind"] == "ios":
            udid = device.get("udid") or self.ios_udid(device["name"])
            if udid:
                code, out = sh(["xcrun", "simctl", "delete", udid], timeout=180)
                if code != 0:
                    raise PoolError(f"could not delete {device['name']}: {out[-300:]}")
                (self.fingerprints_dir() / f"{udid}.ios-fingerprint").unlink(missing_ok=True)
        else:
            avdmanager = tool("avdmanager")
            deleted = False
            if avdmanager:
                code, _ = sh([avdmanager, "delete", "avd", "-n", device["name"]], timeout=120, env=self.java_env())
                deleted = code == 0
            if not deleted:
                shutil.rmtree(avd_home() / f"{device['name']}.avd", ignore_errors=True)
                (avd_home() / f"{device['name']}.ini").unlink(missing_ok=True)
        with self.host.lock:
            device.pop("udid", None)
            self.host.save()
        self.host.log(f"device deleted: {device['name']}; the pool recreates it on demand")

    def unaccounted_mobile_harness(self) -> list[int]:
        if WINDOWS:
            return [0]
        table = process_table()
        pool: set[int] = set()
        for entry in self.registry.all():
            if entry.is_alive() and (entry.meta or {}).get("pool"):
                pool.add(entry.pid)
                if entry.child_pid:
                    pool.add(entry.child_pid)
                pool.update(descendant_pids(entry.pid, table))
        pool.update(d.get("pid") for d in self.host.devices.values() if d.get("pid"))
        base_avd = str(self.config["devices.androidBaseAvd"] or "")
        stray_patterns = [str(p) for p in self.stray_patterns() if p]
        found = []
        for pid, (_, command) in table.items():
            mobile = (MOBILE_DRIVER_MARKER in command
                      or any(pattern and pattern in command for pattern in stray_patterns)
                      or (base_avd and "qemu-system" in command and base_avd in command))
            if mobile and pid not in pool:
                found.append(pid)
        return found

    def sweep(self) -> dict:
        report: dict[str, list] = {"legacySimulators": [], "strayEmulators": []}
        pool_names = {d["name"] for d in self.host.devices.values()}
        legacy_live = bool(self.unaccounted_mobile_harness())
        if shutil.which("xcrun"):
            for group in self.ios_list().values():
                for device in group:
                    name = device.get("name", "")
                    retire = [str(p) for p in (self.config["devices.retireNamePrefixes"] or []) if p]
                    if not any(name.startswith(p) for p in retire) or name in pool_names:
                        continue
                    if device.get("state") == "Booted" and legacy_live:
                        continue
                    sh(["xcrun", "simctl", "shutdown", device["udid"]], timeout=120)
                    sh(["xcrun", "simctl", "delete", device["udid"]], timeout=120)
                    report["legacySimulators"].append(name)
                    self.host.log(f"legacy simulator deleted: {name} ({device['udid']})")
        adb = tool("adb")
        if adb:
            pool_serials = {d.get("serial") for d in self.host.devices.values() if d["kind"] == "android"}
            legacy_emulator_live = legacy_live or any(
                e.is_alive() and e.role == "android-emulator" for e in self.registry.all())
            code, out = sh([adb, "devices"], timeout=15)
            for line in out.splitlines():
                serial = line.split("\t")[0]
                if serial.startswith("emulator-") and serial not in pool_serials and not legacy_emulator_live:
                    code, name = sh([adb, "-s", serial, "emu", "avd", "name"], timeout=10)
                    if str(self.config["devices.androidBaseAvd"]) in name:
                        sh([adb, "-s", serial, "emu", "kill"], timeout=30)
                        report["strayEmulators"].append(serial)
                        self.host.log(f"legacy emulator stopped: {serial}")
        return report

    def require_on(self, key: str) -> dict:
        device = self.device(key)
        if device.get("status") != "on":
            raise PoolError(f"{device['name']} is not running")
        return device

    def screenshot(self, key: str, dest: Path) -> dict:
        device = self.require_on(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if device["kind"] == "ios":
            code, out = sh(["xcrun", "simctl", "io", device["udid"], "screenshot", "--type=png", str(dest)], timeout=60)
            if code != 0 or not dest.exists():
                raise PoolError(f"simctl screenshot failed: {out[-300:]}")
        else:
            adb = tool("adb")
            if not adb:
                raise PoolError("adb not found (set ANDROID_HOME)")
            with open(dest, "wb") as handle:
                result = subprocess.run([adb, "-s", device["serial"], "exec-out", "screencap", "-p"],
                                        stdout=handle, stderr=subprocess.PIPE, timeout=60)
            if result.returncode != 0 or dest.stat().st_size == 0:
                raise PoolError(f"adb screencap failed: {result.stderr.decode('utf-8', 'replace')[-300:]}")
        width, height = png_size(dest)
        return {"path": str(dest), "mime": "image/png", "width": width, "height": height, "device": device["name"]}

    def recorder_log(self, key: str) -> Path:
        return self.host.home / f"recorder-{key.replace(':', '-')}.log"

    def record_start(self, key: str, dest: Path) -> None:
        device = self.require_on(key)
        if self.is_recording(key):
            raise PoolError(f"{device['name']} is already recording; stop that video first")
        dest.parent.mkdir(parents=True, exist_ok=True)
        log_file = self.recorder_log(key)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_bytes(b"")
        remote = None
        if device["kind"] == "ios":
            command = ["xcrun", "simctl", "io", device["udid"], "recordVideo", "--codec", "h264", "--force", str(dest)]
            ready_text, timeout = IOS_READY_TEXT, 15.0
        else:
            adb = tool("adb")
            if not adb:
                raise PoolError("adb not found (set ANDROID_HOME)")
            remote = f"/sdcard/eks-rec-{device['index']}.mp4"
            command = [adb, "-s", device["serial"], "shell", "screenrecord", remote]
            ready_text, timeout = None, 3.0
        spawned = time.time()
        with open(log_file, "ab", buffering=0) as handle:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                                       close_fds=True, **({} if WINDOWS else {"start_new_session": True}))
        deadline = time.time() + timeout
        ready = False
        while time.time() < deadline:
            text = log_file.read_text(errors="replace")
            if ready_text and ready_text in text:
                ready = True
                break
            if process.poll() is not None:
                break
            if not ready_text and time.time() > deadline - timeout + 1.5:
                ready = True
                break
            time.sleep(0.1)
        if process.poll() is not None or not ready:
            text = log_file.read_text(errors="replace")
            if process.poll() is None:
                try:
                    os.kill(process.pid, signal.SIGINT)
                except OSError:
                    pass
            if "already in progress" in text or "Resource busy" in text:
                raise PoolError(f"{device['name']} has an orphaned recording (Host recording is already in "
                                f"progress); shut the device down or reset it")
            raise PoolError(f"the recording did not start:\n{text[-800:]}")
        with self.host.lock:
            device["recording"] = {"pid": process.pid, "started": pid_started(process.pid), "dest": str(dest),
                                   "startedAt": time.time() if ready_text else spawned, "log": str(log_file),
                                   "remote": remote}
            self.host.save()
        self.host.log(f"{device['name']}: recording to {dest}")

    def stop_recorder(self, device: dict, recording: dict) -> bool:
        pid, started = recording.get("pid"), recording.get("started")
        if device["kind"] == "android":
            adb = tool("adb")
            if adb and device.get("serial"):
                sh([adb, "-s", device["serial"], "shell", "pkill", "-INT", "screenrecord"], timeout=15)
        elif alive(pid, started):
            try:
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        deadline = time.time() + RECORD_STOP_TIMEOUT
        while time.time() < deadline and alive(pid, started):
            time.sleep(0.2)
        return not alive(pid, started)

    def forget_recording(self, device: dict, recording: dict | None = None) -> None:
        with self.host.lock:
            if recording is None or device.get("recording") is recording:
                device.pop("recording", None)
                device.pop("recordingSid", None)
            self.host.save()

    def record_stop(self, key: str) -> dict:
        device = self.device(key)
        recording = device.get("recording")
        if not recording:
            raise PoolError(f"{device['name']} is not recording")
        if not self.stop_recorder(device, recording):
            raise PoolError(f"the recorder of {device['name']} did not finish in {RECORD_STOP_TIMEOUT:.0f}s; it was "
                            f"not killed, because SIGKILL leaves an orphaned capture on the device")
        try:
            return self.collect_recording(device, recording)
        finally:
            self.forget_recording(device, recording)

    def collect_recording(self, device: dict, recording: dict) -> dict:
        dest = Path(recording["dest"])
        if device["kind"] == "android" and recording.get("remote"):
            adb = tool("adb")
            if not adb:
                raise PoolError("adb not found (set ANDROID_HOME)")
            time.sleep(1.0)
            code, out = 1, ""
            for attempt in range(2):
                code, out = sh([adb, "-s", device["serial"], "pull", recording["remote"], str(dest)], timeout=180)
                if code == 0:
                    break
                time.sleep(2.0)
            sh([adb, "-s", device["serial"], "shell", "rm", "-f", recording["remote"]], timeout=15)
            if code != 0:
                raise PoolError(f"could not pull the recording of {device['name']}; it was discarded: {out[-300:]}")
        if not dest.exists() or dest.stat().st_size == 0:
            log_text = Path(recording.get("log", "")).read_text(errors="replace")[-600:] if recording.get("log") else ""
            raise PoolError(f"the recording of {device['name']} produced no file: {log_text}")
        info = {"path": str(dest), "mime": "video/mp4", "startedAt": recording.get("startedAt"),
                "wallSeconds": round(time.time() - float(recording.get("startedAt") or time.time()), 2),
                "device": device["name"]}
        info.update(probe_video(dest))
        return info

    def record_reset(self, key: str) -> dict:
        device = self.device(key)
        report: dict = {"device": device["name"], "stoppedRecorder": False, "orphan": False, "restarted": False}
        recording = device.get("recording")
        if recording:
            if not self.stop_recorder(device, recording):
                raise PoolError(f"the recorder of {device['name']} did not stop on SIGINT in "
                                f"{RECORD_STOP_TIMEOUT:.0f}s; shut the device down instead")
            report["stoppedRecorder"] = True
            if recording.get("dest"):
                Path(recording["dest"]).unlink(missing_ok=True)
        self.forget_recording(device)
        if device.get("status") != "on":
            return report
        if device["kind"] == "android":
            adb = tool("adb")
            if adb and device.get("serial"):
                sh([adb, "-s", device["serial"], "shell", "pkill", "-INT", "screenrecord"], timeout=15)
                sh([adb, "-s", device["serial"], "shell", "rm", "-f", f"/sdcard/eks-rec-{device['index']}.mp4"],
                   timeout=15)
            return report
        udid = device.get("udid")
        if not udid:
            return report
        marker = f"simctl io {udid} recordVideo"
        stray = [pid for pid, (_, command) in process_table().items() if marker in command]
        for pid in stray:
            try:
                os.kill(pid, signal.SIGINT)
            except OSError:
                pass
        deadline = time.time() + RECORD_STOP_TIMEOUT
        while time.time() < deadline and any(alive(pid) for pid in stray):
            time.sleep(0.25)
        report["stoppedRecorder"] = report["stoppedRecorder"] or bool(stray)
        if self.recorder_orphaned(key, device):
            report["orphan"] = True
            self.host.log(f"{device['name']}: an orphaned capture holds the recorder; restarting the simulator")
            self.shutdown(key)
            self.boot(key)
            report["restarted"] = True
        return report

    def recorder_orphaned(self, key: str, device: dict) -> bool:
        probe = self.host.home / f"recorder-probe-{key.replace(':', '-')}.mp4"
        log_file = self.host.home / f"recorder-probe-{key.replace(':', '-')}.log"
        probe.unlink(missing_ok=True)
        with open(log_file, "wb") as handle:
            process = subprocess.Popen(["xcrun", "simctl", "io", device["udid"], "recordVideo", "--codec", "h264",
                                        "--force", str(probe)], stdin=subprocess.DEVNULL, stdout=handle,
                                       stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
        deadline = time.time() + 15
        text = ""
        while time.time() < deadline:
            text = log_file.read_text(errors="replace")
            if IOS_READY_TEXT in text or process.poll() is not None:
                break
            time.sleep(0.1)
        if process.poll() is None:
            os.kill(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=RECORD_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.host.log(f"{device['name']}: the recorder probe did not stop on SIGINT (pid {process.pid})")
        probe.unlink(missing_ok=True)
        text = log_file.read_text(errors="replace")
        log_file.unlink(missing_ok=True)
        return "already in progress" in text or "Resource busy" in text

    def is_recording(self, key: str) -> bool:
        device = self.host.devices.get(key) or {}
        recording = device.get("recording")
        if not recording:
            return False
        if alive(recording.get("pid"), recording.get("started")):
            return True
        return device.get("kind") == "android" and bool(recording.get("remote"))

    def live_source(self, key: str) -> LiveSource:
        from eks_harness.live.sources import pool_source

        device = self.require_on(key)
        if device["kind"] == "ios" and self.is_recording(key):
            raise PoolError(f"{device['name']} is recording a video; simctl allows one recording at a time")
        return pool_source(self.host, key)

    def device_log(self, key: str, since: float | None, dest: Path) -> dict:
        device = self.require_on(key)
        since = since if since is not None else time.time() - 300
        dest.parent.mkdir(parents=True, exist_ok=True)
        if device["kind"] == "ios":
            start = datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M:%S")
            command = ["xcrun", "simctl", "spawn", device["udid"], "log", "show", "--style", "compact",
                       "--start", start]
        else:
            adb = tool("adb")
            if not adb:
                raise PoolError("adb not found (set ANDROID_HOME)")
            start = datetime.fromtimestamp(since).strftime("%m-%d %H:%M:%S.000")
            command = [adb, "-s", device["serial"], "logcat", "-d", "-v", "threadtime", "-T", start]
        with open(dest, "wb") as handle:
            try:
                result = subprocess.run(command, stdout=handle, stderr=subprocess.PIPE, timeout=180)
            except subprocess.TimeoutExpired as error:
                raise PoolError(f"the device log of {device['name']} took longer than 180s") from error
        if result.returncode != 0:
            raise PoolError(f"could not read the device log: {result.stderr.decode('utf-8', 'replace')[-300:]}")
        lines = sum(1 for _ in open(dest, "rb"))
        return {"path": str(dest), "mime": "text/plain", "lines": lines, "since": since, "device": device["name"]}
