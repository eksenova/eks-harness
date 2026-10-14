from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import eks
from eks_harness.drivers.runtime import AppContext, AppRuntime, AppRuntimeError, RuntimeWorker
from eks_harness.pools.devices import tool

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = "com.eksenova.reference"
ACTIVITY = f"{PACKAGE}/.MainActivity"
BUNDLE = "com.eksenova.reference"
APK = ROOT / "android-app" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
IOS_APP = ROOT / "ios-app" / "ios" / "build" / "Build" / "Products" / "Debug-iphonesimulator" / "Referans.app"
METRO_PORT = 8181
BRIDGE_WAIT = 180.0


def run(argv: Sequence[str], *, cwd: Path | None = None, timeout: float = 120, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
                          env={**os.environ, **(env or {})})
    if done.returncode != 0:
        raise AppRuntimeError(f"{' '.join(argv[:4])} failed ({done.returncode}): {(done.stdout + done.stderr)[-600:]}")
    return done.stdout


def java_home() -> str:
    found = run(["/usr/libexec/java_home", "-v", "17"]).strip()
    if not found:
        raise AppRuntimeError("the Android build needs JDK 17")
    return found


@eks.app("reference-web", description="Vite and React reference app served by its dev server")
class ReferenceWeb:
    def prepare(self, ctx: AppContext) -> AppRuntime:
        port = int((ctx.profile.get("web") or {}).get("dev_port") or 5391)
        url = f"http://127.0.0.1:{port}"
        ctx.spawn(["npx", "vite", "--host", "127.0.0.1", "--port", str(port), "--strictPort"], key=f"reference-web-{port}",
                  cwd=ROOT / "web-app", ready_url=f"{url}/", timeout=120)
        return AppRuntime(url=url)


@eks.app("reference-android", description="Native Android reference app driven on the native surface")
class ReferenceAndroid:
    def _adb(self, ctx: AppContext, *args: str, timeout: float = 120) -> str:
        adb = tool("adb")
        serial = ctx.device.get("serial")
        if not adb or not serial:
            raise AppRuntimeError("the Android reference app needs adb and the emulator serial of the lease")
        return run([adb, "-s", str(serial), *args], timeout=timeout)

    def prepare(self, ctx: AppContext) -> AppRuntime:
        if not APK.is_file():
            run([str(ROOT / "android-app" / "gradlew"), "--no-daemon", "-q", "assembleDebug"], cwd=ROOT / "android-app",
                timeout=1800, env={"JAVA_HOME": java_home()})
        self._adb(ctx, "install", "-r", str(APK), timeout=300)
        self._adb(ctx, "shell", "am", "force-stop", PACKAGE)
        self._adb(ctx, "shell", "pm", "revoke", PACKAGE, "android.permission.CAMERA")
        self._adb(ctx, "shell", "pm", "clear-permission-flags", PACKAGE, "android.permission.CAMERA", "user-set",
                  "user-fixed")
        return AppRuntime()

    def boot(self, ctx: AppContext, worker: RuntimeWorker) -> None:
        self._adb(ctx, "shell", "am", "start", "-W", "-n", ACTIVITY)


@eks.app("reference-ios", description="Expo React Native reference app with the harness bridge")
class ReferenceIos:
    def _simctl(self, *args: str, timeout: float = 120) -> str:
        return run(["xcrun", "simctl", *args], timeout=timeout)

    def prepare(self, ctx: AppContext) -> AppRuntime:
        udid = str(ctx.device.get("udid") or "")
        if not udid:
            raise AppRuntimeError("the iOS reference app needs the simulator udid of the lease")
        if not IOS_APP.is_dir():
            raise AppRuntimeError(f"build the iOS reference app first; {IOS_APP} is missing")
        subprocess.run(["xcrun", "simctl", "terminate", udid, BUNDLE], capture_output=True, check=False, timeout=60)
        if ctx.native is not None:
            ctx.native.dismiss_alerts()
        self._simctl("install", udid, str(IOS_APP), timeout=300)
        self._simctl("privacy", udid, "reset", "location", BUNDLE)
        self._simctl("spawn", udid, "defaults", "write", BUNDLE, "RCT_jsLocation", f"127.0.0.1:{METRO_PORT}")
        return AppRuntime()

    def boot(self, ctx: AppContext, worker: RuntimeWorker) -> None:
        udid = str(ctx.device.get("udid"))
        ctx.spawn(["npx", "expo", "start", "--port", str(METRO_PORT)], key=f"reference-ios-metro-{METRO_PORT}",
                  cwd=ROOT / "ios-app", env={"CI": "1", "EXPO_PUBLIC_EHX_HARNESS_URL": worker.url},
                  ready_url=f"http://127.0.0.1:{METRO_PORT}/status", timeout=180)
        self._simctl("launch", "--terminate-running-process", udid, BUNDLE)
        deadline = time.monotonic() + BRIDGE_WAIT
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                worker.call("mobile.idle", {"timeout": 5000}, timeout=30)
                return
            except Exception as error:
                last = error
                time.sleep(2)
        raise AppRuntimeError(f"the iOS reference app did not connect its harness bridge within {BRIDGE_WAIT:.0f} s: {last}")
