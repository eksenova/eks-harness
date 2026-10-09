from __future__ import annotations

from pathlib import Path

import pytest

from eks_harness.pools import devices


def make_jdk(home: Path) -> Path:
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("")
    return home


def make_app(path: Path) -> Path:
    (path / "Contents").mkdir(parents=True)
    (path / "Contents" / "Info.plist").write_text("")
    return path


class FakeShell:
    def __init__(self, responses: dict[str, tuple[int, str]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: float = 60, env: dict | None = None) -> tuple[int, str]:
        self.calls.append(list(args))
        for prefix, response in self.responses.items():
            if " ".join(args).startswith(prefix):
                return response
        return 1, f"unexpected command: {args}"


@pytest.fixture
def java_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(devices.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(devices, "MACOS_JAVA_HOME", tmp_path / "usr" / "libexec" / "java_home")
    monkeypatch.setattr(devices, "HOMEBREW_PREFIXES", (tmp_path / "opt" / "homebrew", tmp_path / "usr" / "local"))
    monkeypatch.setattr(devices.shutil, "which", lambda name: None)
    monkeypatch.setattr(devices, "sh", FakeShell({}))
    return tmp_path


def test_java_home_prefers_a_valid_java_home_variable(java_world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = make_jdk(java_world / "jdk-21")
    monkeypatch.setenv("JAVA_HOME", str(home))
    assert devices.resolve_java_home() == (home, "JAVA_HOME")


def test_java_home_skips_a_broken_java_home_variable(java_world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVA_HOME", str(java_world / "missing"))
    keg = make_jdk(java_world / "opt" / "homebrew" / "opt" / "openjdk@17" / "libexec" / "openjdk.jdk" / "Contents"
                   / "Home")
    assert devices.resolve_java_home() == (keg, "Homebrew openjdk@17")


def test_java_home_uses_the_macos_java_home_tool(java_world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = make_jdk(java_world / "Library" / "Java" / "JavaVirtualMachines" / "jdk.jdk" / "Contents" / "Home")
    tool = devices.MACOS_JAVA_HOME
    tool.parent.mkdir(parents=True)
    tool.write_text("")
    monkeypatch.setattr(devices, "sh", FakeShell({str(tool): (0, f"{home}\n")}))
    assert devices.resolve_java_home() == (home, str(tool))


def test_java_home_falls_back_to_the_brew_prefix_of_openjdk17(java_world: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    tool = devices.MACOS_JAVA_HOME
    tool.parent.mkdir(parents=True)
    tool.write_text("")
    prefix = java_world / "cellar" / "openjdk@17"
    home = make_jdk(prefix / "libexec" / "openjdk.jdk" / "Contents" / "Home")
    brew = java_world / "bin" / "brew"
    shell = FakeShell({str(tool): (1, "Unable to locate a Java Runtime."),
                       f"{brew} --prefix openjdk@17": (0, f"{prefix}\n")})
    monkeypatch.setattr(devices, "sh", shell)
    monkeypatch.setattr(devices.shutil, "which", lambda name: str(brew) if name == "brew" else None)
    assert devices.resolve_java_home() == (home, "brew --prefix openjdk@17")
    assert [str(tool)] in shell.calls


def test_java_home_prefers_the_jdk_home_inside_a_keg(java_world: Path) -> None:
    keg = java_world / "opt" / "homebrew" / "opt" / "openjdk@17"
    make_jdk(keg)
    home = make_jdk(keg / "libexec" / "openjdk.jdk" / "Contents" / "Home")
    assert devices.resolve_java_home() == (home, "Homebrew openjdk@17")


def test_java_home_finds_the_fixed_keg_paths_without_brew_on_path(java_world: Path) -> None:
    home = make_jdk(java_world / "usr" / "local" / "opt" / "openjdk@17" / "libexec" / "openjdk.jdk" / "Contents"
                    / "Home")
    assert devices.resolve_java_home() == (home, "Homebrew openjdk@17")


def test_java_home_prefers_openjdk17_over_plain_openjdk(java_world: Path) -> None:
    make_jdk(java_world / "opt" / "homebrew" / "opt" / "openjdk" / "libexec" / "openjdk.jdk" / "Contents" / "Home")
    keg17 = make_jdk(java_world / "usr" / "local" / "opt" / "openjdk@17" / "libexec" / "openjdk.jdk" / "Contents"
                     / "Home")
    assert devices.resolve_java_home() == (keg17, "Homebrew openjdk@17")


def test_java_home_uses_plain_openjdk_last(java_world: Path) -> None:
    home = make_jdk(java_world / "opt" / "homebrew" / "opt" / "openjdk" / "libexec" / "openjdk.jdk" / "Contents"
                    / "Home")
    assert devices.resolve_java_home() == (home, "Homebrew openjdk")


def test_java_home_skips_the_macos_tool_elsewhere(java_world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devices.platform, "system", lambda: "Linux")
    tool = devices.MACOS_JAVA_HOME
    tool.parent.mkdir(parents=True)
    tool.write_text("")
    shell = FakeShell({})
    monkeypatch.setattr(devices, "sh", shell)
    assert devices.resolve_java_home() is None
    assert shell.calls == []


def test_java_env_exports_the_resolved_home(java_world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    home = make_jdk(java_world / "opt" / "homebrew" / "opt" / "openjdk@17" / "libexec" / "openjdk.jdk" / "Contents"
                    / "Home")
    env, source = devices.java_env()
    assert env["JAVA_HOME"] == str(home)
    assert env["PATH"].split(devices.os.pathsep)[:2] == [str(home / "bin"), "/usr/bin"]
    assert source == "Homebrew openjdk@17"


def test_java_env_leaves_the_environment_alone_without_a_jdk(java_world: Path) -> None:
    env, source = devices.java_env()
    assert source is None and "JAVA_HOME" not in env


@pytest.fixture
def xcode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    developer = tmp_path / "Xcode.app" / "Contents" / "Developer"
    developer.mkdir(parents=True)
    monkeypatch.setattr(devices, "sh", FakeShell({"xcode-select -p": (0, f"{developer}\n"), "mdfind": (0, "")}))
    return developer


def test_simulator_ui_keeps_simulator_app_when_xcode_ships_it(xcode: Path) -> None:
    simulator = make_app(xcode / "Applications" / "Simulator.app")
    make_app(xcode.parent / "Applications" / "DeviceHub.app")
    ui = devices.simulator_ui()
    assert ui == devices.SimulatorUI("Simulator", simulator, devices.SIMULATOR_APP_ID)
    assert ui.open_command("UDID-1") == ["open", "-g", "-a", str(simulator), "--args", "-CurrentDeviceUDID", "UDID-1"]
    assert ui.quit_command() == ["osascript", "-e", 'quit app id "com.apple.iphonesimulator"']


def test_simulator_ui_uses_device_hub_on_xcode_27(xcode: Path) -> None:
    hub = make_app(xcode.parent / "Applications" / "DeviceHub.app")
    ui = devices.simulator_ui()
    assert ui == devices.SimulatorUI("DeviceHub", hub, devices.DEVICE_HUB_APP_ID)
    assert ui.open_command("UDID-1") == ["open", "-g", "-a", str(hub), "devices://device/open?id=UDID-1"]
    assert ui.quit_command() == ["osascript", "-e", 'quit app id "com.apple.dt.Devices"']


def test_simulator_ui_ignores_stale_launch_services_entries(tmp_path: Path, xcode: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    stale = tmp_path / "gone" / "Simulator.app"
    elsewhere = make_app(tmp_path / "Other" / "DeviceHub.app")
    monkeypatch.setattr(devices, "sh", FakeShell({
        "xcode-select -p": (0, f"{xcode}\n"),
        f"mdfind kMDItemCFBundleIdentifier == '{devices.SIMULATOR_APP_ID}'": (0, f"{stale}\n"),
        f"mdfind kMDItemCFBundleIdentifier == '{devices.DEVICE_HUB_APP_ID}'": (0, f"{elsewhere}\n"),
    }))
    assert devices.simulator_ui() == devices.SimulatorUI("DeviceHub", elsewhere, devices.DEVICE_HUB_APP_ID)


def test_simulator_ui_is_none_without_either_app(xcode: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devices, "sh", FakeShell({"xcode-select -p": (2, "error"), "mdfind": (0, "")}))
    assert devices.simulator_ui() is None


class RecordingHost:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def log(self, message: str) -> None:
        self.messages.append(message)


def bare_pool() -> devices.DevicePool:
    pool = devices.DevicePool.__new__(devices.DevicePool)
    pool.host = RecordingHost()
    return pool


def test_show_simulator_reports_a_failed_open(xcode: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub = make_app(xcode.parent / "Applications" / "DeviceHub.app")
    shell = FakeShell({"xcode-select -p": (0, f"{xcode}\n"), "mdfind": (0, ""),
                       "open": (1, "LSOpenURLsWithRole() failed")})
    monkeypatch.setattr(devices, "sh", shell)
    pool = bare_pool()
    pool.show_simulator({"name": "Harness iOS 1"}, "UDID-1")
    assert ["open", "-g", "-a", str(hub), "devices://device/open?id=UDID-1"] in shell.calls
    assert len(pool.host.messages) == 1
    assert pool.host.messages[0].startswith("warning: Harness iOS 1: could not open DeviceHub")
    assert "LSOpenURLsWithRole() failed" in pool.host.messages[0]


def test_show_simulator_is_quiet_when_the_window_opens(xcode: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_app(xcode / "Applications" / "Simulator.app")
    monkeypatch.setattr(devices, "sh", FakeShell({"xcode-select -p": (0, f"{xcode}\n"), "open": (0, "")}))
    pool = bare_pool()
    pool.show_simulator({"name": "Harness iOS 1"}, "UDID-1")
    assert pool.host.messages == []


def test_show_simulator_warns_without_a_simulator_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devices, "sh", FakeShell({"xcode-select -p": (2, ""), "mdfind": (0, "")}))
    pool = bare_pool()
    pool.show_simulator({"name": "Harness iOS 1"}, "UDID-1")
    assert pool.host.messages and "neither Simulator.app nor DeviceHub.app" in pool.host.messages[0]


def test_quit_simulator_ui_only_quits_a_running_app(xcode: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub = make_app(xcode.parent / "Applications" / "DeviceHub.app")
    shell = FakeShell({"xcode-select -p": (0, f"{xcode}\n"), "mdfind": (0, ""), "osascript": (0, "")})
    monkeypatch.setattr(devices, "sh", shell)
    monkeypatch.setattr(devices, "process_table", lambda: {})
    pool = bare_pool()
    pool.quit_simulator_ui()
    assert not any(call[0] == "osascript" for call in shell.calls)
    monkeypatch.setattr(devices, "process_table",
                        lambda: {42: (1, f"{hub}/Contents/MacOS/DeviceHub")})
    pool.quit_simulator_ui()
    assert ["osascript", "-e", 'quit app id "com.apple.dt.Devices"'] in shell.calls
    assert pool.host.messages == []


def test_pool_java_env_warns_when_no_jdk_is_found(java_world: Path) -> None:
    pool = bare_pool()
    env = pool.java_env()
    assert "JAVA_HOME" not in env
    assert pool.host.messages and pool.host.messages[0].startswith("warning: no Java runtime found")


IOS_26 = "com.apple.CoreSimulator.SimRuntime.iOS-26-5"
IOS_27 = "com.apple.CoreSimulator.SimRuntime.iOS-27-0"


def simctl_world(devices_by_runtime: dict) -> FakeShell:
    import json

    runtimes = {"runtimes": [{"platform": "iOS", "version": "26.5", "identifier": IOS_26},
                             {"platform": "iOS", "version": "27.0", "identifier": IOS_27}]}
    types = {"devicetypes": [{"name": "iPhone 17 Pro", "identifier": "iPhone-17-Pro"}]}
    return FakeShell({
        "xcrun simctl list devices -j": (0, json.dumps({"devices": devices_by_runtime})),
        "xcrun simctl list runtimes available -j": (0, json.dumps(runtimes)),
        "xcrun simctl list devicetypes -j": (0, json.dumps(types)),
        "xcrun simctl create": (0, "NEW-UDID\n"),
        "xcrun simctl delete": (0, ""),
    })


def ios_pool(monkeypatch: pytest.MonkeyPatch, shell: FakeShell) -> devices.DevicePool:
    monkeypatch.setattr(devices, "sh", shell)
    monkeypatch.setattr(devices.shutil, "which", lambda name: "/usr/bin/xcrun" if name == "xcrun" else None)
    pool = bare_pool()
    pool.host.config = {"devices.iosDeviceType": "iPhone 17 Pro"}
    return pool


def sim(udid: str, state: str = "Shutdown", available: bool = True) -> dict:
    return {"name": "Harness iOS 1", "udid": udid, "state": state, "isAvailable": available}


def test_ensure_ios_reuses_a_simulator_on_the_newest_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = simctl_world({IOS_27: [sim("UDID-27")]})
    pool = ios_pool(monkeypatch, shell)
    device = {"name": "Harness iOS 1"}
    assert pool.ensure_ios(device) == "UDID-27"
    assert device["udid"] == "UDID-27"
    assert not any(call[:3] in (["xcrun", "simctl", "create"], ["xcrun", "simctl", "delete"]) for call in shell.calls)


def test_ensure_ios_recreates_a_shut_down_simulator_on_an_older_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = simctl_world({IOS_26: [sim("UDID-26")]})
    pool = ios_pool(monkeypatch, shell)
    device = {"name": "Harness iOS 1"}
    assert pool.ensure_ios(device) == "NEW-UDID"
    assert ["xcrun", "simctl", "delete", "UDID-26"] in shell.calls
    assert ["xcrun", "simctl", "create", "Harness iOS 1", "iPhone-17-Pro", IOS_27] in shell.calls
    assert any("was on iOS-26-5; recreating it on iOS-27-0" in m for m in pool.host.messages)


def test_ensure_ios_keeps_a_booted_simulator_on_an_older_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = simctl_world({IOS_26: [sim("UDID-26", state="Booted")]})
    pool = ios_pool(monkeypatch, shell)
    assert pool.ensure_ios({"name": "Harness iOS 1"}) == "UDID-26"
    assert not any(call[:3] == ["xcrun", "simctl", "delete"] for call in shell.calls)


def test_ensure_ios_replaces_a_simulator_whose_runtime_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = simctl_world({IOS_27: [sim("UDID-GONE", available=False)]})
    pool = ios_pool(monkeypatch, shell)
    assert pool.ensure_ios({"name": "Harness iOS 1"}) == "NEW-UDID"
    assert ["xcrun", "simctl", "delete", "UDID-GONE"] in shell.calls
    assert any("was unavailable" in m for m in pool.host.messages)
