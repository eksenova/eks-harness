from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from eks_harness.paths import DIR_ENVS, HOME_ENV, Paths

LABEL = "dev.eks-harness.daemon"
UNIT = "eks-harness.service"
TASK = "EksHarness"
EXECUTABLE = "eks-harness"
FILE_LIMIT = 8192
PASSED_ENV = ("ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_AVD_HOME", "JAVA_HOME", "EKS_HARNESS_CACHE",
              HOME_ENV, *DIR_ENVS.values())


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Role:
    name: str
    label: str
    unit: str
    task: str
    args: tuple[str, ...]
    description: str

    def log_file(self, paths: Paths) -> Path:
        return paths.daemon_log if self.name == "daemon" else paths.log_dir / f"{self.name}.log"

    def record(self, paths: Paths) -> Path:
        return paths.service_record if self.name == "daemon" else paths.config_dir / f"service-{self.name}.json"


DAEMON = Role("daemon", LABEL, UNIT, TASK, ("daemon", "serve", "--managed"),
              "eks-harness hub (pools, backends, nodes, artifact store, studio and web UI)")
NODE = Role("node", "dev.eks-harness.node", "eks-harness-node.service", "EksHarnessNode", ("node", "serve"),
            "eks-harness node agent (runs hub jobs on this machine)")


@dataclass(frozen=True)
class ServiceSpec:
    command: list[str]
    env: dict[str, str]
    log_file: Path
    working_dir: Path


def run(args: list[str], check: bool = False) -> tuple[int, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True)
    except OSError as error:
        if check:
            raise ServiceError(f"{' '.join(args)}: {error}") from error
        return 127, str(error)
    output = (result.stdout + result.stderr).strip()
    if check and result.returncode != 0:
        raise ServiceError(f"{' '.join(args)}: {output}")
    return result.returncode, output


def installed(paths: Paths, role: Role = DAEMON) -> dict | None:
    try:
        data = json.loads(role.record(paths).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_executable(path: Path) -> bool:
    return path.is_file() and (os.access(path, os.X_OK) or path.suffix.lower() in (".exe", ".cmd", ".bat"))


def executable_command(argv0: str | None = None, which=shutil.which) -> list[str]:
    found = which(EXECUTABLE)
    if found:
        return [str(Path(found).absolute())]
    candidate = Path(argv0 or sys.argv[0])
    if candidate.name.lower().startswith(EXECUTABLE):
        resolved = candidate if candidate.is_absolute() else Path.cwd() / candidate
        if is_executable(resolved):
            return [str(resolved.absolute())]
    sibling = Path(sys.executable).parent / (EXECUTABLE + (".exe" if platform.system() == "Windows" else ""))
    if is_executable(sibling):
        return [str(sibling)]
    return [sys.executable, "-m", "eks_harness.cli"]


def source_checkout_src() -> Path | None:
    try:
        import eks_harness
    except ImportError:
        return None
    pkg = Path(getattr(eks_harness, "__file__", "") or "").resolve()
    if pkg.parent.name == "eks_harness" and pkg.parent.parent.name == "src":
        return pkg.parent.parent
    return None


def service_path(environ: dict[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    home = Path.home()
    parts = environ.get("PATH", "").split(os.pathsep)
    extra = [str(home / ".local" / "bin"), str(home / ".dotnet"), "/opt/homebrew/bin", "/usr/local/bin",
             str(home / "Library" / "Android" / "sdk" / "platform-tools"),
             str(home / "Library" / "Android" / "sdk" / "emulator")]
    seen, ordered = set(), []
    for part in parts + extra:
        if part and part not in seen:
            seen.add(part)
            ordered.append(part)
    return os.pathsep.join(ordered)


def current_user(environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    user = env.get("USER") or env.get("LOGNAME")
    if user:
        return user
    try:
        import pwd
    except ImportError:
        return None
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        return None


def build_spec(paths: Paths, command: list[str] | None = None, environ: dict[str, str] | None = None,
               role: Role = DAEMON) -> ServiceSpec:
    environ = dict(os.environ if environ is None else environ)
    env = {"PATH": service_path(environ), "HOME": str(Path.home()), "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
           "PYTHONUNBUFFERED": "1"}
    user = current_user(environ)
    if user:
        env["USER"] = user
    for key in PASSED_ENV:
        if environ.get(key):
            env[key] = environ[key]
    resolved = command or executable_command()
    full = [*resolved, *role.args]
    if command is None and resolved[:1] == [sys.executable] and resolved[1:] == ["-m", "eks_harness.cli"]:
        src = source_checkout_src()
        if src is not None:
            prior = [entry for entry in (environ.get("PYTHONPATH") or "").split(os.pathsep)
                     if entry and Path(entry).is_absolute()]
            env["PYTHONPATH"] = os.pathsep.join([str(src), *prior])
    return ServiceSpec(command=full, env=env, log_file=role.log_file(paths), working_dir=paths.state_dir)


def launchd_plist(spec: ServiceSpec, label: str = LABEL) -> str:
    env_xml = "".join(f"\n      <key>{escape(k)}</key><string>{escape(v)}</string>" for k, v in spec.env.items())
    args_xml = "".join(f"\n      <string>{escape(a)}</string>" for a in spec.command)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>{escape(label)}</string>
    <key>ProgramArguments</key>
    <array>{args_xml}
    </array>
    <key>EnvironmentVariables</key>
    <dict>{env_xml}
    </dict>
    <key>WorkingDirectory</key><string>{escape(str(spec.working_dir))}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>ProcessType</key><string>Interactive</string>
    <key>SoftResourceLimits</key><dict><key>NumberOfFiles</key><integer>{FILE_LIMIT}</integer></dict>
    <key>HardResourceLimits</key><dict><key>NumberOfFiles</key><integer>{FILE_LIMIT}</integer></dict>
    <key>StandardOutPath</key><string>{escape(str(spec.log_file))}</string>
    <key>StandardErrorPath</key><string>{escape(str(spec.log_file))}</string>
  </dict>
</plist>
"""


def systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd_unit(spec: ServiceSpec, role: Role = DAEMON) -> str:
    env_lines = "".join(f"Environment={systemd_quote(f'{k}={v}')}\n" for k, v in spec.env.items())
    command = " ".join(systemd_quote(a) for a in spec.command)
    return f"""[Unit]
Description={role.description}
After=network.target

[Service]
Type=simple
ExecStart={command}
WorkingDirectory={spec.working_dir}
{env_lines}LimitNOFILE={FILE_LIMIT}
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=append:{spec.log_file}
StandardError=append:{spec.log_file}

[Install]
WantedBy=default.target
"""


def windows_quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def windows_task_command(spec: ServiceSpec) -> str:
    inner = " ".join(windows_quote(a) for a in spec.command)
    log = windows_quote(str(spec.log_file))
    return f'cmd.exe /c "{inner} >> {log} 2>&1"'


def launchd_plist_path(label: str = LABEL) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def systemd_unit_path(unit: str = UNIT) -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / unit


def retire_services(labels: list[str]) -> list[str]:
    """Remove other services (launchd labels, systemd units, scheduled tasks) this install replaces."""

    removed: list[str] = []
    system = platform.system()
    for label in labels:
        if system == "Darwin":
            plist = launchd_plist_path(label)
            if plist.exists():
                run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
                plist.unlink(missing_ok=True)
                removed.append(str(plist))
        elif system == "Linux":
            unit = systemd_unit_path(label if label.endswith(".service") else f"{label}.service")
            if unit.exists():
                run(["systemctl", "--user", "disable", "--now", unit.name])
                unit.unlink(missing_ok=True)
                run(["systemctl", "--user", "daemon-reload"])
                removed.append(str(unit))
        elif system == "Windows":
            code, _ = run(["schtasks", "/Query", "/TN", label])
            if code == 0:
                run(["schtasks", "/End", "/TN", label])
                run(["schtasks", "/Delete", "/TN", label, "/F"])
                removed.append(f"scheduled task {label}")
    return removed


def install(paths: Paths, command: list[str] | None = None, retire: list[str] | None = None,
            role: Role = DAEMON) -> dict:
    spec = build_spec(paths, command, role=role)
    spec.log_file.parent.mkdir(parents=True, exist_ok=True)
    spec.working_dir.mkdir(parents=True, exist_ok=True)
    retired = retire_services([label for label in (retire or []) if label != role.label])
    system = platform.system()
    if system == "Darwin":
        plist = launchd_plist_path(role.label)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(launchd_plist(spec, role.label), encoding="utf-8")
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", f"{domain}/{role.label}"])
        run(["launchctl", "bootstrap", domain, str(plist)], check=True)
        where = str(plist)
    elif system == "Linux":
        unit = systemd_unit_path(role.unit)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit(spec, role), encoding="utf-8")
        run(["systemctl", "--user", "daemon-reload"], check=True)
        run(["systemctl", "--user", "enable", "--now", role.unit], check=True)
        where = str(unit)
    elif system == "Windows":
        run(["schtasks", "/Create", "/TN", role.task, "/SC", "ONLOGON", "/RL", "LIMITED", "/F", "/TR",
             windows_task_command(spec)], check=True)
        run(["schtasks", "/Run", "/TN", role.task])
        where = f"scheduled task {role.task}"
    else:
        raise ServiceError(f"unsupported platform: {system}")
    info = {"platform": system, "where": where, "command": spec.command, "log": str(spec.log_file),
            "retired": retired}
    role.record(paths).parent.mkdir(parents=True, exist_ok=True)
    role.record(paths).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


def uninstall(paths: Paths, role: Role = DAEMON) -> dict | None:
    info = installed(paths, role)
    system = platform.system()
    if system == "Darwin":
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{role.label}"])
        launchd_plist_path(role.label).unlink(missing_ok=True)
    elif system == "Linux":
        run(["systemctl", "--user", "disable", "--now", role.unit])
        systemd_unit_path(role.unit).unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"])
    elif system == "Windows":
        run(["schtasks", "/End", "/TN", role.task])
        run(["schtasks", "/Delete", "/TN", role.task, "/F"])
    role.record(paths).unlink(missing_ok=True)
    return info


def start(paths: Paths, role: Role = DAEMON) -> bool:
    if not installed(paths, role):
        return False
    system = platform.system()
    if system == "Darwin":
        domain = f"gui/{os.getuid()}"
        code, _ = run(["launchctl", "kickstart", f"{domain}/{role.label}"])
        if code != 0:
            run(["launchctl", "bootstrap", domain, str(launchd_plist_path(role.label))], check=True)
    elif system == "Linux":
        run(["systemctl", "--user", "start", role.unit], check=True)
    elif system == "Windows":
        run(["schtasks", "/Run", "/TN", role.task], check=True)
    else:
        return False
    return True


def linger_hint() -> str | None:
    if platform.system() != "Linux":
        return None
    user = os.environ.get("USER") or ""
    code, out = run(["loginctl", "show-user", user, "--property=Linger"])
    if code == 0 and out.strip().endswith("=yes"):
        return None
    return f"run 'loginctl enable-linger {user}' so the service keeps running without a login session"
