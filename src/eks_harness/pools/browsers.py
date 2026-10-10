from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

from eks_harness.pools.backends import (
    WINDOWS,
    InstanceRegistry,
    ProcessRegistry,
    alive,
    detached_kwargs,
    kill_group,
    pid_started,
    process_table,
    repo_cache_root,
    run,
)
from eks_harness.pools.base import BrowserPool as BrowserPoolBase
from eks_harness.pools.base import LiveSource, PoolError, PoolHost, browser_resource, iso_now, parse_browser_resource

KNOWN_BROWSERS = {
    "Darwin": {
        "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "chrome-beta": ["/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"],
        "chromium": ["/Applications/Chromium.app/Contents/MacOS/Chromium"],
        "edge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        "brave": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
    },
    "Linux": {
        "chrome": ["google-chrome", "google-chrome-stable"],
        "chrome-beta": ["google-chrome-beta"],
        "chromium": ["chromium", "chromium-browser"],
        "edge": ["microsoft-edge", "microsoft-edge-stable"],
        "brave": ["brave-browser", "brave"],
    },
    "Windows": {
        "chrome": [r"Google\Chrome\Application\chrome.exe"],
        "chrome-beta": [r"Google\Chrome Beta\Application\chrome.exe"],
        "chromium": [r"Chromium\Application\chrome.exe"],
        "edge": [r"Microsoft\Edge\Application\msedge.exe"],
        "brave": [r"BraveSoftware\Brave-Browser\Application\brave.exe"],
    },
}
CONTROL_SERVER_ROLE = "web-ctl"
WEB_DRIVER_MARKER = "eks-harness-web-driver"
LOCK_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")
CODE_SIGN_CLONE_GRACE_SECONDS = 600
CODE_SIGN_CLONE_LAUNCH_WINDOW_SECONDS = 10
APP_EXECUTABLE = re.compile(r"/(?:Google Chrome(?: Beta| Dev| Canary| for Testing)?|Chromium|Microsoft Edge"
                            r"(?: Beta| Dev| Canary)?|Brave Browser)\.app(?:\.bundle)?/Contents/MacOS/")


def user_data_pattern(root: Path) -> re.Pattern[str]:
    return re.compile(re.escape(f"--user-data-dir={root}") + r"(?=\s|$)")


def process_cwd(pid: int) -> str:
    try:
        result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True,
                                timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return line[1:]
    return ""


def resolve_browser(command: str) -> str:
    if os.path.isabs(command):
        if not Path(command).exists():
            raise PoolError(f"browser.command points at a missing file: {command}")
        return command
    system = platform.system()
    candidates = KNOWN_BROWSERS.get(system, {}).get(command)
    if candidates is None:
        raise PoolError(f"unknown browser.command '{command}'; use one of "
                        f"{', '.join(KNOWN_BROWSERS.get(system, {}))} or an absolute path")
    for candidate in candidates:
        if system == "Windows":
            for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                         os.environ.get("LOCALAPPDATA")):
                if base and Path(base, candidate).exists():
                    return str(Path(base, candidate))
        elif system == "Linux":
            found = shutil.which(candidate)
            if found:
                return found
        elif Path(candidate).exists():
            return candidate
    raise PoolError(f"browser '{command}' is not installed on this machine")


def code_sign_clone_roots() -> list[Path]:
    if platform.system() != "Darwin":
        return []
    temp = run(["getconf", "DARWIN_USER_TEMP_DIR"])
    if not temp:
        return []
    return sorted(Path(temp).parent.joinpath("X").glob("*.code_sign_clone"))


def clone_birth(path: Path) -> float:
    info = path.stat()
    return getattr(info, "st_birthtime", info.st_mtime)


def browser_start_times(table: dict[int, tuple[int, str]]) -> tuple[list[float], set[str]]:
    starts: list[float] = []
    referenced: set[str] = set()
    for pid, (_, command) in table.items():
        if not APP_EXECUTABLE.search(command):
            continue
        for match in re.finditer(r"code_sign_clone\.[A-Za-z0-9]+", command):
            referenced.add(match.group(0))
        if "--type=" in command or "MacAppCodeSignClone" in command:
            continue
        lstart = " ".join(pid_started(pid).split())
        try:
            starts.append(time.mktime(time.strptime(lstart, "%a %b %d %H:%M:%S %Y")))
        except ValueError:
            continue
    return starts, referenced


def orphaned_code_sign_clones(roots: list[Path], starts: list[float], referenced: set[str], now: float,
                              grace: float = CODE_SIGN_CLONE_GRACE_SECONDS,
                              born: Callable[[Path], float] = clone_birth) -> list[Path]:
    orphans = []
    for root in roots:
        for clone in sorted(root.glob("code_sign_clone.*")):
            if clone.name in referenced:
                continue
            try:
                birth = born(clone)
            except OSError:
                continue
            if now - birth < grace:
                continue
            if any(start - 2 <= birth <= start + CODE_SIGN_CLONE_LAUNCH_WINDOW_SECONDS for start in starts):
                continue
            orphans.append(clone)
    return orphans


def remove_tree(path: Path) -> None:
    def unlock(function: Callable, target: str, _: BaseException) -> None:
        os.chmod(Path(target).parent, 0o700)
        os.chmod(target, 0o700)
        function(target)

    shutil.rmtree(path, onexc=unlock)


class BrowserPool(BrowserPoolBase):
    def __init__(self, host: PoolHost) -> None:
        super().__init__(host)
        self.repo_root = repo_cache_root(host.paths)
        self.registry = ProcessRegistry(self.repo_root)
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.contexts_for: Callable[[str], list[str]] = lambda resource: []

    def index_lock(self, index: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(index, threading.Lock())

    def user_data_dir(self, index: int) -> Path:
        path = self.host.paths.browser_dir / str(index)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_path(self, index: int) -> Path:
        return self.host.home / f"browser-{index}.log"

    def capacity(self) -> int:
        return int(self.config["browser.instances"]) * int(self.config["browser.profilesPerInstance"])

    def alive(self, index: int) -> bool:
        record = self.host.browsers.get(str(index)) or {}
        port = record.get("port")
        if not port or not alive(record.get("pid"), record.get("started")):
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
                return response.status == 200
        except Exception:
            return False

    def allocate(self, taken: set[str]) -> str | None:
        per = int(self.config["browser.profilesPerInstance"])
        counts = {}
        for index in self.indices():
            counts[index] = sum(1 for resource in taken if resource.startswith(f"browser:{index}:"))
        running = [i for i in counts if (self.host.browsers.get(str(i)) or {}).get("pid") and counts[i] < per]
        idle_slots = [i for i in counts if counts[i] < per]
        order = sorted(running, key=lambda i: -counts[i]) + [i for i in idle_slots if i not in running]
        for index in order:
            for profile in range(1, per + 1):
                resource = browser_resource(index, profile)
                if resource not in taken:
                    return resource
        return None

    def our_pids(self, index: int | None = None, table: dict[int, tuple[int, str]] | None = None) -> list[int]:
        if WINDOWS:
            return []
        indices = [index] if index else self.indices()
        patterns = [user_data_pattern(self.user_data_dir(i)) for i in indices]
        if index is None:
            patterns.append(user_data_pattern(self.repo_root / "chrome" / "user-data"))
        pids = []
        for pid, (_, command) in (table if table is not None else process_table()).items():
            if "--type=" in command:
                continue
            if any(pattern.search(command) for pattern in patterns):
                pids.append(pid)
        return pids

    def pids_by_index(self, table: dict[int, tuple[int, str]]) -> dict[int, list[int]]:
        return {index: self.our_pids(index, table) for index in self.indices()}

    def ensure(self, index: int) -> str:
        with self.index_lock(index):
            if self.alive(index):
                with self.host.lock:
                    record = self.host.browsers[str(index)]
                    record["last_used"] = time.time()
                return record["cdp"]
            binary = resolve_browser(str(self.config["browser.command"]))
            for pid in self.our_pids(index):
                self.host.log(f"stray harness browser in slot {index} stopped (pid {pid})")
                kill_group(pid, None, grace=5)
            root = self.user_data_dir(index)
            (root / "DevToolsActivePort").unlink(missing_ok=True)
            for lock in LOCK_FILES:
                (root / lock).unlink(missing_ok=True)
            args = [
                binary, f"--user-data-dir={root}", "--remote-debugging-port=0",
                "--no-first-run", "--no-default-browser-check", "--disable-default-apps",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                # MacAppCodeSignClone: Chrome clones its whole bundle into /var/folders/.../X at every
                # launch and only deletes it on a clean exit, so each pool restart leaked ~1.4 GB.
                "--disable-backgrounding-occluded-windows",
                "--disable-features=CalculateNativeWinOcclusion,MacAppCodeSignClone",
                "--disable-blink-features=AutomationControlled", "--password-store=basic",
                "--window-size=1280,800", *[str(a) for a in self.config["browser.extraArgs"]], "about:blank",
            ]
            log_file = self.log_path(index)
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "ab", buffering=0) as handle:
                handle.write(f"\n==== {iso_now()} start {' '.join(args)}\n".encode())
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                                           close_fds=True, **detached_kwargs())
            deadline = time.time() + 45
            port = None
            while time.time() < deadline:
                marker = root / "DevToolsActivePort"
                if marker.exists():
                    lines = marker.read_text().splitlines()
                    if lines and lines[0].isdigit():
                        port = int(lines[0])
                        break
                if process.poll() is not None:
                    break
                time.sleep(0.2)
            if not port:
                raise PoolError(f"the browser did not start or never wrote its DevTools port; log: {log_file}")
            with self.host.lock:
                self.host.browsers[str(index)] = {
                    "pid": process.pid, "started": pid_started(process.pid), "port": port,
                    "cdp": f"http://127.0.0.1:{port}", "binary": binary, "launched": time.time(),
                    "last_used": time.time(),
                }
                self.host.save()
        self.host.log(f"browser {index} started: {binary} pid {process.pid}, CDP http://127.0.0.1:{port} (windowed)")
        self.host.emit("browser.status", resource=f"browser:{index}",
                       detail={"status": "running", "pid": process.pid, "port": port})
        return f"http://127.0.0.1:{port}"

    def stop(self, index: int, reason: str) -> None:
        with self.host.lock:
            record = self.host.browsers.pop(str(index), None) or {}
            self.host.save()
        if record.get("pid") and alive(record["pid"], record.get("started")):
            self.host.log(f"browser {index} stopped ({reason})")
            kill_group(record["pid"], record.get("started"), grace=8)
        if record:
            self.host.emit("browser.status", resource=f"browser:{index}", detail={"status": "stopped", "reason": reason})

    def stop_control_server(self, instance: str) -> bool:
        entry = self.registry.load_role(CONTROL_SERVER_ROLE, instance)
        if entry and entry.is_alive():
            return self.registry.stop(entry)
        return False

    def processes(self) -> list[dict]:
        out = []
        for index in self.indices():
            record = dict(self.host.browsers.get(str(index)) or {})
            out.append({"index": index, "alive": self.alive(index) if record else False, **record})
        return out

    def index_of(self, resource: str) -> int:
        if resource.count(":") == 2:
            return parse_browser_resource(resource)[0]
        text = resource.split(":")[-1]
        if not text.isdigit():
            raise PoolError(f"unknown browser resource {resource}")
        return int(text)

    def reset(self, resource: str) -> None:
        index = self.index_of(resource)
        with self.index_lock(index):
            self.stop(index, "reset")
            for pid in self.our_pids(index):
                kill_group(pid, None, grace=5)
            shutil.rmtree(self.host.paths.browser_dir / str(index), ignore_errors=True)
            self.user_data_dir(index)
        self.host.log(f"browser {index}: user data erased")

    def delete(self, resource: str) -> None:
        index = self.index_of(resource)
        with self.index_lock(index):
            self.stop(index, "deleted")
            for pid in self.our_pids(index):
                kill_group(pid, None, grace=5)
            shutil.rmtree(self.host.paths.browser_dir / str(index), ignore_errors=True)
        self.host.log(f"browser {index}: user data deleted; recreated on demand")

    def close_profile(self, resource: str, context_ids: list[str]) -> list[str]:
        from eks_harness.live.chrome import dispose_contexts
        from eks_harness.live.common import LiveError

        index = self.index_of(resource)
        if not context_ids or not self.alive(index):
            return []
        cdp = self.cdp_url(index)
        if not cdp:
            return []
        try:
            closed = dispose_contexts(cdp, context_ids)
        except LiveError as error:
            raise PoolError(f"could not close the browser contexts of {resource}: {error}") from error
        self.host.log(f"{resource}: closed {len(closed)} browser context(s); the other profiles of browser {index} "
                      f"keep running")
        return closed

    def sweep(self) -> dict:
        report: dict[str, list] = {"headlessBrowsers": [], "strayBrowsers": []}
        if WINDOWS:
            return report
        table = process_table()
        registered = set()
        for entry in self.registry.all():
            if entry.is_alive():
                registered.add(entry.pid)
            if entry.child_pid:
                registered.add(entry.child_pid)
        harness_trees = self.harness_trees()
        for pid, (ppid, command) in table.items():
            if "chrome-headless-shell" not in command or "ms-playwright" not in command or "--type=" in command:
                continue
            parent = table.get(ppid, (0, ""))[1]
            if WEB_DRIVER_MARKER in parent and ppid not in registered:
                orphan = True
            elif ppid == 1:
                cwd = process_cwd(pid)
                orphan = bool(cwd) and any(cwd == tree or cwd.startswith(tree + "/") for tree in harness_trees)
            else:
                orphan = False
            if orphan:
                kill_group(pid, None, grace=3)
                report["headlessBrowsers"].append(pid)
                self.host.log(f"orphaned headless harness browser stopped: pid {pid}")
        for index, pids in self.pids_by_index(table).items():
            lock = self.index_lock(index)
            if not lock.acquire(blocking=False):
                continue
            try:
                with self.host.lock:
                    known = (self.host.browsers.get(str(index)) or {}).get("pid")
                for pid in pids:
                    if pid != known:
                        kill_group(pid, None, grace=5)
                        report["strayBrowsers"].append(pid)
                        self.host.log(f"stray harness browser {index} stopped: pid {pid}")
            finally:
                lock.release()
        legacy = user_data_pattern(self.repo_root / "chrome" / "user-data")
        for pid, (_, command) in table.items():
            if "--type=" not in command and legacy.search(command):
                kill_group(pid, None, grace=5)
                report["strayBrowsers"].append(pid)
                self.host.log(f"legacy harness browser stopped: pid {pid}")
        report["codeSignClones"] = self.sweep_code_sign_clones(table)
        return report

    def sweep_code_sign_clones(self, table: dict[int, tuple[int, str]]) -> list[str]:
        roots = code_sign_clone_roots()
        if not roots:
            return []
        starts, referenced = browser_start_times(table)
        removed = []
        for clone in orphaned_code_sign_clones(roots, starts, referenced, time.time()):
            try:
                remove_tree(clone)
            except OSError as error:
                self.host.log(f"orphaned browser code-sign clone not removed: {clone} ({error})")
                continue
            removed.append(str(clone))
            self.host.log(f"orphaned browser code-sign clone removed: {clone}")
        return removed

    def harness_trees(self) -> list[str]:
        trees: set[str] = set()
        for data in InstanceRegistry(self.repo_root).read().values():
            for key in ("tree", "previous_tree"):
                value = data.get(key)
                if value:
                    trees.add(str(Path(value)))
        for entry in self.registry.all():
            if entry.tree:
                trees.add(str(Path(entry.tree)))
        return sorted(trees)

    def live_source(self, resource: str) -> LiveSource:
        from eks_harness.live.sources import pool_source

        index = self.index_of(resource)
        if not self.alive(index):
            raise PoolError(f"browser {index} is not running")
        contexts = self.contexts_for(resource) if resource.count(":") == 2 else []
        return pool_source(self.host, resource, cdp_url=self.cdp_url(index), context_ids=contexts or None)
