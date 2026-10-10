from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eks_harness.paths import Paths, resolve_paths

ENV_PREFIX = "EKS_HARNESS_"


@dataclass(frozen=True)
class Setting:
    key: str
    default: Any
    type: str
    description: str
    restart_required: bool = False
    group: str = ""
    nullable: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.group:
            object.__setattr__(self, "group", self.key.split(".", 1)[0])


HOST_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|[0-9A-Fa-f:.]+")

GROUPS: dict[str, str] = {
    "server": "Server",
    "auth": "Authentication",
    "browser": "Browser",
    "capture": "Capture",
    "devices": "Devices",
    "lease": "Leases",
    "backend": "Backends",
    "daemon": "Daemon",
    "storage": "Storage",
    "retention": "Retention",
    "events": "Events",
    "live": "Live view",
    "service": "Service",
    "plugins": "Plugins",
    "video": "Video",
    "update": "Updates",
    "nodes": "Nodes",
    "render": "Rendering",
}

_SETTINGS: list[Setting] = [
    Setting("server.host", "127.0.0.1", "str", "IP address the daemon listens on (127.0.0.1, 0.0.0.0 or a LAN address)", True),
    Setting("server.port", 7171, "int", "TCP port the daemon listens on", True, minimum=1, maximum=65535),
    Setting("server.publicUrl", "", "str", "base URL used in every link the daemon returns (empty: http://<host>:<port>)"),
    Setting("server.shareUrl", "", "str", "base URL of share links (empty: the public URL); its host only serves share pages and shared files"),
    Setting("server.trustedProxies", [], "list[str]", "CIDRs of reverse proxies whose X-Forwarded-For is trusted (JSON list)"),
    Setting("server.cloudflare", False, "bool", "trust CF-Connecting-IP and X-Forwarded-Proto from trusted proxies (Cloudflare Tunnel)"),
    Setting("server.allowedHosts", [], "list[str]", "extra host names the daemon answers to, besides loopback, the listen address, this machine's LAN addresses and the public URL host; any other Host header is refused (DNS rebinding guard)"),
    Setting("auth.enabled", False, "bool", "require a login or an API key for the API, UI, raw files and sites"),
    Setting("auth.sessionHours", 168, "int", "lifetime of a web login session in hours", minimum=1),
    Setting("auth.cookieSecure", False, "bool", "mark the session cookie Secure (enable when every access goes through HTTPS)"),
    Setting("browser.command", "chrome", "str", "chrome | chrome-beta | chromium | edge | brave | an absolute path to a Chromium-based browser (CDP is required)"),
    Setting("browser.instances", 1, "int", "how many browser processes the daemon may run", True, minimum=1),
    Setting("browser.profilesPerInstance", 5, "int", "isolated profiles (browser contexts) per browser process", True, minimum=1),
    Setting("browser.idleSeconds", 600, "int", "quit a browser process after this long without profiles", minimum=0),
    Setting("browser.extraArgs", [], "list[str]", "extra command line arguments for the browser (JSON list)"),
    Setting("devices.ios", 3, "int", "iOS simulators in the pool", True, minimum=0),
    Setting("devices.android", 3, "int", "Android emulators in the pool", True, minimum=0),
    Setting("devices.maxRunning", 3, "int", "devices (iOS + Android) that may run at the same time", minimum=1),
    Setting("devices.idleSeconds", 600, "int", "shut a free device down after this long", minimum=0),
    Setting("devices.iosDeviceType", "iPhone 17 Pro", "str", "simulator device type for new pool simulators"),
    Setting("devices.namePrefix", "Harness", "str", "prefix of pool device names: iOS simulators are '<prefix> iOS N', Android AVDs '<prefix>_harness_N' (lower case; harness_N with the default prefix)"),
    Setting("devices.retireNamePrefixes", [], "list[str]", "simulators whose name starts with one of these (and are not pool devices) are deleted by the sweep (JSON list)"),
    Setting("devices.androidBaseAvd", "", "str", "AVD whose system image and device new pool AVDs copy (required for an Android pool)"),
    Setting("devices.androidPortBase", 5580, "int", "console port of the first pool emulator (then +2 each)", True, minimum=5554, maximum=65000),
    Setting("lease.idleSeconds", 600, "int", "release a lease after this long without a heartbeat", minimum=1),
    Setting("lease.agentStopGraceSeconds", 120, "int", "idle limit after a harness agent's turn ends (SubagentStop)", minimum=0),
    Setting("lease.queueTimeoutSeconds", 45, "int", "drop a queued request after this long without a poll", minimum=5),
    Setting("backend.idleGraceSeconds", 120, "int", "stop a backend this long after its last frontend is released", minimum=0),
    Setting("backend.portRangeStart", 5400, "int", "first port the daemon hands to backends", minimum=1024, maximum=65535),
    Setting("backend.portRangeEnd", 5999, "int", "last port the daemon hands to backends", minimum=1024, maximum=65535),
    Setting("backend.definitionDirs", [".harness/backends"], "list[str]", "tree-relative folders searched for backend definitions (JSON list); <config>/backends is always searched too"),
    Setting("service.retireLabels", [], "list[str]", "other service labels (launchd labels, systemd units, scheduled tasks) removed when the daemon service is installed (JSON list)"),
    Setting("plugins.paths", [], "list[str]", "folders holding plugins (a plugin folder or a folder of plugin folders; JSON list)"),
    Setting("plugins.git", [], "list[str]", "git plugins as url[#subpath][@ref] or github:org/repo[#subpath][@ref] (JSON list)"),
    Setting("plugins.enabled", [], "list[str]", "plugins enabled everywhere although they are off by default (JSON list of ids)"),
    Setting("plugins.disabled", [], "list[str]", "plugins disabled everywhere (JSON list of ids)"),
    Setting("plugins.settings", {}, "dict", "plugin settings by plugin id (JSON object of objects); project settings override them"),
    Setting("nodes.hostNode", True, "bool", "run a node on this machine too, so the hub machine's own GPUs and tools "
            "take jobs like any other node", True),
    Setting("nodes.hostNodeId", "", "str", "node id of this machine (empty: derived from the host name)", True),
    Setting("nodes.hubUrl", "", "str", "URL nodes dial to reach this hub (empty: the listen address); ws:// or wss:// is derived from it"),
    Setting("nodes.maxAttempts", 3, "int", "how often a job is retried after its node disconnects or fails it", minimum=1),
    Setting("nodes.offlineSeconds", 45, "int", "a node without a heartbeat for this long counts as offline", minimum=5),
    Setting("nodes.blobRetentionDays", 14, "int", "delete job blobs nobody used for this many days", minimum=1),
    Setting("daemon.idleExitSeconds", 3600, "int", "exit after this long with nothing running (0 = never; ignored when installed as a service)", minimum=0),
    Setting("daemon.tickSeconds", 5, "int", "housekeeping interval", True, minimum=1),
    Setting("daemon.sweepSeconds", 300, "int", "interval between sweeps for stray browsers, simulators and emulators", minimum=30),
    Setting("storage.maxUploadMb", 2048, "int", "largest accepted upload in MB", minimum=1),
    Setting("storage.quotaGb", None, "int", "show a storage warning in the UI above this many GB (empty: no quota)", nullable=True, minimum=1),
    Setting("update.repository", "https://github.com/eksenova/eks-harness.git", "str", "git repository updates install from (git's own credentials are used for private repositories)"),
    Setting("update.ref", "main", "str", "branch, tag or commit that updates follow"),
    Setting("update.auto", True, "bool", "check for new commits and install them automatically when nothing is running (service mode only); the daemon restarts gracefully afterwards"),
    Setting("update.checkMinutes", 60, "int", "how often the daemon checks for updates", minimum=5),
    Setting("update.replaceLocal", False, "bool", "let automatic updates replace an install built from a local checkout"),
    Setting("retention.defaultDays", None, "int", "delete unpinned artifacts older than this many days in projects without their own retention (empty: keep forever)", nullable=True, minimum=1),
    Setting("retention.checkMinutes", 60, "int", "how often retention and share expiry run", minimum=1),
    Setting("events.retentionDays", 90, "int", "keep activity events this many days", minimum=1),
    Setting("live.idleStopSeconds", 10, "int", "stop a live stream producer this long after its last viewer leaves", minimum=0),
    Setting("live.maxFps", 10, "int", "frame rate cap of live streams", minimum=1, maximum=60),
    Setting("live.jpegQuality", 70, "int", "JPEG quality of live stream frames", minimum=10, maximum=100),
    Setting("render.concurrency", 1, "int", "video renders that run at once on this machine; the rest wait first in, first out. Driver sessions, recording and recording encodes run under their device lease and never wait", minimum=1),
    Setting("capture.pace.moveMs", 300, "int", "visible pointer travel to the target before each action, in ms", minimum=0),
    Setting("capture.pace.dwellMs", 400, "int", "pause on the target before each action, in ms", minimum=0),
    Setting("capture.pace.typeMsPerChar", 40, "int", "delay between typed characters, in ms", minimum=0),
    Setting("capture.pace.holdMs", 700, "int", "hold after every interaction once settled, in ms", minimum=0),
    Setting("capture.pace.screenHoldMs", 1500, "int", "hold on every new screen or modal after it settles, in ms", minimum=0),
    Setting("capture.pace.mobilePressMs", 250, "int", "marker-visible delay before a mobile press lands, in ms", minimum=0),
    Setting("capture.trim.keepBeforeSec", 1.2, "float", "real-time seconds kept before every interaction", minimum=0),
    Setting("capture.trim.keepAfterSec", 2.0, "float", "real-time seconds kept after every interaction", minimum=0),
    Setting("capture.trim.maxSpeedup", 4, "float", "fastest idle speedup in trimmed videos", minimum=1),
    Setting("capture.trim.minScreenSec", 2.0, "float", "shortest any screen is shown in a trimmed video, in seconds", minimum=0),
    Setting("capture.annotate.matchPx", 2, "int", "re-measure match tolerance per edge, in CSS px", minimum=0),
    Setting("capture.annotate.minContrast", 4.5, "float", "minimum WCAG contrast for annotation text", minimum=1),
    Setting("capture.annotate.minContrastLarge", 3.0, "float", "minimum contrast for large annotation text", minimum=1),
    Setting("capture.annotate.minTextPx", 12, "int", "minimum annotation text size at the reference width, in px", minimum=1),
    Setting("capture.pointer", "presentation", "str", "video cursor: a neutral presentation cursor, or off",
            choices=("presentation", "off")),
    Setting("video.workspace", "", "str", "folder holding the studio's video projects (empty: <data dir>/video)"),
    Setting("video.mediaLibrary", "", "str", "folder offered to video projects as the media library (empty: none)"),
]

LABELS: dict[str, str] = {
    "server.host": "Listen address",
    "server.port": "Port",
    "server.publicUrl": "Public URL",
    "server.shareUrl": "Share link URL",
    "server.trustedProxies": "Trusted proxies",
    "server.cloudflare": "Trust Cloudflare headers (CF-Connecting-IP, X-Forwarded-Proto)",
    "server.allowedHosts": "Allowed host names",
    "auth.enabled": "Require sign-in",
    "auth.sessionHours": "Web session lifetime",
    "auth.cookieSecure": "Send the session cookie over HTTPS only",
    "browser.command": "Browser",
    "browser.instances": "Browser processes",
    "browser.profilesPerInstance": "Profiles per process",
    "browser.idleSeconds": "Quit an unused browser after",
    "browser.extraArgs": "Extra browser arguments",
    "devices.ios": "iOS simulators",
    "devices.android": "Android emulators",
    "devices.maxRunning": "Devices running at once",
    "devices.idleSeconds": "Shut down a free device after",
    "devices.iosDeviceType": "Simulator device type",
    "devices.namePrefix": "Pool device name prefix",
    "devices.retireNamePrefixes": "Retired simulator name prefixes",
    "devices.androidBaseAvd": "Base AVD",
    "devices.androidPortBase": "First emulator console port",
    "lease.idleSeconds": "Release a lease without heartbeat after",
    "lease.agentStopGraceSeconds": "Idle limit after an agent's turn ends",
    "lease.queueTimeoutSeconds": "Drop a queued request without a poll after",
    "backend.idleGraceSeconds": "Stop an unbound backend after",
    "backend.portRangeStart": "First backend port",
    "backend.portRangeEnd": "Last backend port",
    "backend.definitionDirs": "Backend definition folders",
    "service.retireLabels": "Retired service labels",
    "plugins.paths": "Plugin folders",
    "plugins.git": "Git plugins",
    "plugins.enabled": "Plugins enabled everywhere",
    "plugins.disabled": "Plugins disabled everywhere",
    "plugins.settings": "Plugin settings",
    "nodes.hostNode": "Run this machine as a node",
    "nodes.hostNodeId": "Node id of this machine",
    "nodes.hubUrl": "Hub URL for nodes",
    "nodes.maxAttempts": "Job attempts",
    "nodes.offlineSeconds": "Count a node offline after",
    "nodes.blobRetentionDays": "Keep unused job blobs for",
    "daemon.idleExitSeconds": "Exit when idle after",
    "daemon.tickSeconds": "Housekeeping interval",
    "daemon.sweepSeconds": "Sweep interval",
    "storage.maxUploadMb": "Largest upload",
    "storage.quotaGb": "Storage quota",
    "update.repository": "Update repository",
    "update.ref": "Update branch, tag or commit",
    "update.auto": "Install updates automatically",
    "update.checkMinutes": "Update check interval",
    "update.replaceLocal": "Let updates replace a local checkout install",
    "retention.defaultDays": "Default retention",
    "retention.checkMinutes": "Retention check interval",
    "events.retentionDays": "Keep activity events for",
    "live.idleStopSeconds": "Stop a live stream after the last viewer leaves",
    "live.maxFps": "Live stream frame rate",
    "live.jpegQuality": "Live stream JPEG quality",
    "render.concurrency": "Renders at once",
    "capture.pace.moveMs": "Pointer move time",
    "capture.pace.dwellMs": "Dwell before each action",
    "capture.pace.typeMsPerChar": "Typing speed",
    "capture.pace.holdMs": "Hold after each interaction",
    "capture.pace.screenHoldMs": "Hold on new screens",
    "capture.pace.mobilePressMs": "Mobile press marker time",
    "capture.trim.keepBeforeSec": "Keep before each interaction",
    "capture.trim.keepAfterSec": "Keep after each interaction",
    "capture.trim.maxSpeedup": "Fastest idle speedup",
    "capture.trim.minScreenSec": "Shortest screen time",
    "capture.annotate.matchPx": "Annotation re-measure tolerance",
    "capture.annotate.minContrast": "Annotation minimum contrast",
    "capture.annotate.minContrastLarge": "Annotation large-text contrast",
    "capture.annotate.minTextPx": "Annotation minimum text size",
    "capture.pointer": "Video pointer",
    "video.workspace": "Video project folder",
    "video.mediaLibrary": "Media library folder",
}

SETTINGS: dict[str, Setting] = {s.key: s for s in _SETTINGS}
DEFAULTS: dict[str, Any] = {s.key: s.default for s in _SETTINGS}
DESCRIPTIONS: dict[str, str] = {s.key: s.description for s in _SETTINGS}
RESTART_REQUIRED: frozenset[str] = frozenset(s.key for s in _SETTINGS if s.restart_required)


class ConfigError(ValueError):
    pass


def env_name(key: str) -> str:
    return ENV_PREFIX + key.replace(".", "_").upper()


def _parse_bool(key: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off", ""):
        return False
    raise ConfigError(f"{key} must be true or false")


def coerce(key: str, value: Any) -> Any:
    setting = SETTINGS.get(key)
    if setting is None:
        raise ConfigError(f"unknown key {key}; known: {', '.join(SETTINGS)}")
    if value is None or (isinstance(value, str) and value.strip().lower() in ("null", "none") and setting.type != "str"):
        if setting.nullable:
            return None
        raise ConfigError(f"{key} cannot be empty")
    if isinstance(value, str) and setting.nullable and value.strip() == "" and setting.type != "str":
        return None
    kind = setting.type
    if kind == "bool":
        if isinstance(value, str):
            value = _parse_bool(key, value)
        if not isinstance(value, bool):
            raise ConfigError(f"{key} must be true or false")
    elif kind == "int":
        if isinstance(value, str):
            try:
                value = int(value.strip())
            except ValueError as error:
                raise ConfigError(f"{key} must be an integer") from error
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{key} must be an integer")
    elif kind == "float":
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError as error:
                raise ConfigError(f"{key} must be a number") from error
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{key} must be a number")
        value = float(value)
    elif kind == "list[str]":
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                try:
                    value = json.loads(text)
                except json.JSONDecodeError as error:
                    raise ConfigError(f"{key} must be a JSON list") from error
            else:
                value = [part.strip() for part in text.split(",") if part.strip()]
        if not isinstance(value, list) or not all(isinstance(item, (str, int, float)) for item in value):
            raise ConfigError(f"{key} must be a list of strings")
        value = [str(item) for item in value]
    elif kind == "dict":
        if isinstance(value, str):
            try:
                value = json.loads(value.strip() or "{}")
            except json.JSONDecodeError as error:
                raise ConfigError(f"{key} must be a JSON object") from error
        if not isinstance(value, dict):
            raise ConfigError(f"{key} must be a JSON object")
    elif kind == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{key} must be a string")
        value = value.strip()
    if kind in ("int", "float"):
        if setting.minimum is not None and value < setting.minimum:
            raise ConfigError(f"{key} must be at least {setting.minimum:g}")
        if setting.maximum is not None and value > setting.maximum:
            raise ConfigError(f"{key} must be at most {setting.maximum:g}")
    if setting.choices and value not in setting.choices:
        raise ConfigError(f"{key} must be one of {', '.join(setting.choices)}")
    _validate_special(key, value)
    return value


def _validate_special(key: str, value: Any) -> None:
    if key == "server.host" and value:
        if value != "localhost":
            try:
                ipaddress.ip_address(value)
            except ValueError as error:
                raise ConfigError("server.host must be an IP address or localhost") from error
    elif key in ("server.publicUrl", "server.shareUrl") and value:
        if not value.startswith(("http://", "https://")):
            raise ConfigError(f"{key} must start with http:// or https://")
    elif key == "server.allowedHosts":
        for item in value:
            if not HOST_NAME.fullmatch(str(item)):
                raise ConfigError(f"server.allowedHosts: {item} is not a bare host name or IP address "
                                  f"(no scheme, port or path)")
    elif key == "server.trustedProxies":
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as error:
                raise ConfigError(f"server.trustedProxies: {item} is not a CIDR") from error


def parse_cli_value(key: str, raw: str) -> Any:
    setting = SETTINGS.get(key)
    if setting is None:
        raise ConfigError(f"unknown key {key}; known: {', '.join(SETTINGS)}")
    if setting.type == "str":
        return coerce(key, raw)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    return coerce(key, value)


def read_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


class Config:
    def __init__(self, paths: Paths, env: dict | None = None) -> None:
        self.paths = paths
        self._env = env
        self._lock = threading.RLock()
        self._values: dict[str, Any] = dict(DEFAULTS)
        self._sources: dict[str, str] = {key: "default" for key in DEFAULTS}
        self._errors: dict[str, str] = {}
        self.reload()

    @property
    def file(self) -> Path:
        return self.paths.config_file

    def reload(self) -> "Config":
        env = os.environ if self._env is None else self._env
        values = dict(DEFAULTS)
        sources = {key: "default" for key in DEFAULTS}
        errors: dict[str, str] = {}
        for key, value in read_file(self.file).items():
            if key not in SETTINGS:
                continue
            try:
                values[key] = coerce(key, value)
                sources[key] = "file"
            except ConfigError as error:
                errors[key] = str(error)
        for key in SETTINGS:
            raw = env.get(env_name(key))
            if raw in (None, ""):
                continue
            try:
                values[key] = parse_cli_value(key, raw)
                sources[key] = "env"
            except ConfigError as error:
                errors[key] = f"{env_name(key)}: {error}"
        with self._lock:
            self._values = values
            self._sources = sources
            self._errors = errors
        return self

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return self._values[key]

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._values.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in SETTINGS

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._values)

    def source(self, key: str) -> str:
        with self._lock:
            return self._sources.get(key, "default")

    def errors(self) -> dict[str, str]:
        with self._lock:
            return dict(self._errors)

    def file_values(self) -> dict[str, Any]:
        return {k: v for k, v in read_file(self.file).items() if k in SETTINGS}

    def set(self, key: str, value: Any) -> tuple[Any, Any]:
        new = coerce(key, value)
        with self._lock:
            data = read_file(self.file)
            old = self._values.get(key)
            data[key] = new
            write_file(self.file, data)
        self.reload()
        return old, new

    def set_many(self, values: dict[str, Any]) -> list[tuple[str, Any, Any]]:
        coerced = {key: coerce(key, value) for key, value in values.items()}
        changes = []
        with self._lock:
            data = read_file(self.file)
            for key, new in coerced.items():
                old = self._values.get(key)
                data[key] = new
                changes.append((key, old, new))
            write_file(self.file, data)
        self.reload()
        return changes

    def unset(self, key: str) -> tuple[Any, Any]:
        if key not in SETTINGS:
            raise ConfigError(f"unknown key {key}; known: {', '.join(SETTINGS)}")
        with self._lock:
            data = read_file(self.file)
            old = self._values.get(key)
            data.pop(key, None)
            write_file(self.file, data)
        self.reload()
        return old, self[key]

    def listen_host(self) -> str:
        return str(self["server.host"])

    def listen_port(self) -> int:
        return int(self["server.port"])

    def local_url(self) -> str:
        host = self.listen_host()
        if host in ("0.0.0.0", "", "localhost"):
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.listen_port()}"

    def public_url(self) -> str:
        configured = str(self["server.publicUrl"] or "").rstrip("/")
        return configured or self.local_url()

    def share_url(self) -> str:
        configured = str(self["server.shareUrl"] or "").rstrip("/")
        return configured or self.public_url()

    def share_only_host(self) -> str | None:
        from urllib.parse import urlsplit

        configured = str(self["server.shareUrl"] or "").strip()
        if not configured:
            return None
        share = urlsplit(configured).hostname
        public = urlsplit(self.public_url()).hostname
        return share.lower() if share and share != public else None

    def is_loopback_bind(self) -> bool:
        host = self.listen_host()
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False


def load(paths: Paths | None = None, env: dict | None = None) -> Config:
    return Config(paths or resolve_paths(env), env)


def set_value(paths: Paths, key: str, raw: str) -> tuple[Any, Any]:
    return Config(paths).set(key, parse_cli_value(key, raw))


def unset_value(paths: Paths, key: str) -> tuple[Any, Any]:
    return Config(paths).unset(key)


def describe(config: Config) -> list[dict]:
    rows = []
    values = config.as_dict()
    for setting in _SETTINGS:
        rows.append({
            "key": setting.key,
            "value": values[setting.key],
            "default": setting.default,
            "label": LABELS.get(setting.key, setting.key),
            "description": setting.description,
            "type": setting.type,
            "restartRequired": setting.restart_required,
            "group": setting.group,
            "groupTitle": GROUPS.get(setting.group, setting.group),
            "nullable": setting.nullable,
            "minimum": setting.minimum,
            "maximum": setting.maximum,
            "choices": list(setting.choices) if setting.choices else None,
            "source": config.source(setting.key),
            "envName": env_name(setting.key),
        })
    return rows
