from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from eks_harness.drivers import native
from eks_harness.drivers.native import (
    NativeSurface,
    parse_android,
    parse_ios,
    run_native,
)
from eks_harness.engine.errors import DriverError

ANDROID_DUMP = """UI hierchary dumped to: /dev/tty<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" enabled="true">
    <node index="0" text="Allow Eksen to take pictures?" resource-id="com.android.permissioncontroller:id/permission_message"
          class="android.widget.TextView" bounds="[100,900][980,1000]" enabled="true" />
    <node index="1" text="While using the app" resource-id="com.android.permissioncontroller:id/permission_allow_foreground_only_button"
          class="android.widget.Button" bounds="[100,1100][980,1200]" enabled="true" />
    <node index="2" text="Only this time" resource-id="" class="android.widget.Button" bounds="[100,1220][980,1320]" enabled="true" />
    <node index="3" text="Don't allow" resource-id="" class="android.widget.Button" bounds="[100,1340][980,1440]" enabled="true" />
    <node index="4" text="İzin ver" resource-id="" class="android.widget.Button" bounds="[100,1460][500,1560]" enabled="true" />
    <node index="5" text="İzin ver" resource-id="" class="android.widget.Button" bounds="[580,1460][980,1560]" enabled="false" />
  </node>
</hierarchy>"""

IOS_DESCRIBE = json.dumps([
    {"AXLabel": "Allow", "type": "Button", "AXUniqueId": "allow", "frame": {"x": 40, "y": 500, "width": 120, "height": 44}, "enabled": True},
    {"AXLabel": "Don’t Allow", "type": "Button", "frame": {"x": 200, "y": 500, "width": 140, "height": 44}, "enabled": True},
    {"AXLabel": "Bildirimler", "type": "Switch", "AXValue": "1", "frame": {"x": 40, "y": 600, "width": 300, "height": 44}, "enabled": True},
])


class Runner:
    def __init__(self, dump: str) -> None:
        self.dump = dump
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        self.calls.append(list(argv))
        if "uiautomator" in argv or "describe-all" in argv:
            return 0, self.dump
        if "dumpsys" in argv:
            return 0, "  runtime permissions:\n    android.permission.CAMERA: granted=false\n    android.permission.POST_NOTIFICATIONS: granted=true\n  other:\n"
        return 0, ""


@pytest.fixture
def android(monkeypatch: pytest.MonkeyPatch) -> tuple[NativeSurface, Runner]:
    monkeypatch.setattr(native, "tool", lambda name: "/sdk/adb")
    runner = Runner(ANDROID_DUMP)
    return NativeSurface(kind="android", serial="emulator-5554", runner=runner), runner


@pytest.fixture
def ios(monkeypatch: pytest.MonkeyPatch) -> tuple[NativeSurface, Runner]:
    monkeypatch.setattr(native.shutil, "which", lambda name: "/usr/local/bin/idb" if name == "idb" else None)
    runner = Runner(IOS_DESCRIBE)
    return NativeSurface(kind="ios", udid="UDID-1", runner=runner), runner


def text(value: str, **more: object) -> dict[str, object]:
    return {"by": "text", "args": {"text": value}, "surface": "native", **more}


def test_parsers_read_bounds_ids_and_state() -> None:
    nodes = parse_android(ANDROID_DUMP)
    allow = next(node for node in nodes if node.text == "While using the app")
    assert (allow.ident, allow.kind, allow.centre) == ("permission_allow_foreground_only_button", "Button", (540.0, 1150.0))
    assert [node.enabled for node in nodes if node.text == "İzin ver"] == [True, False]
    switch = next(node for node in parse_ios(IOS_DESCRIBE) if node.kind == "Switch")
    assert switch.checked is True and switch.label == "Bildirimler"


def test_taps_the_permission_dialog_strictly(android: tuple[NativeSurface, Runner]) -> None:
    surface, runner = android
    run_native(surface, "ui.tap", {"target": text("While using the app")}, 1000)
    assert runner.calls[-1] == ["/sdk/adb", "-s", "emulator-5554", "shell", "input", "tap", "540", "1150"]
    with pytest.raises(DriverError) as ambiguous:
        run_native(surface, "ui.tap", {"target": text("İzin ver")}, 1000)
    assert ambiguous.value.code == "target_ambiguous"
    run_native(surface, "ui.tap", {"target": text("İzin ver", nth=-1)}, 1000)
    assert runner.calls[-1][-2:] == ["780", "1510"]
    near = text("İzin ver", near={"by": "text", "args": {"text": "Only this time"}})
    run_native(surface, "ui.tap", {"target": near}, 1000)
    assert runner.calls[-1][-2:] == ["300", "1510"]
    with pytest.raises(DriverError) as missing:
        run_native(surface, "ui.tap", {"target": text("Reddet"), "timeout": 300}, 1000)
    assert missing.value.code == "target_not_found"


def test_assertions_and_reads(android: tuple[NativeSurface, Runner]) -> None:
    surface, _ = android
    assert run_native(surface, "ui.exists", {"target": text("Don't allow")}, 1000) is True
    assert run_native(surface, "ui.count", {"target": {"by": "role", "args": {"role": "button"}}}, 1000) == 5
    result = run_native(surface, "expect.target", {"target": text("İzin ver", nth=0), "assertion": {"kind": "enabled"}}, 1000)
    assert result == {"ok": True, "assertion": "enabled"}
    with pytest.raises(DriverError) as failed:
        run_native(surface, "expect.target", {"target": text("İzin ver", nth=1), "assertion": {"kind": "enabled"},
                                              "timeout": 300}, 1000)
    assert failed.value.code == "assertion"


def test_android_text_input_refuses_non_ascii(android: tuple[NativeSurface, Runner]) -> None:
    surface, runner = android
    with pytest.raises(DriverError) as refused:
        surface.type_text("Ayşe")
    assert refused.value.code == "native_unavailable"
    surface.type_text("a b")
    assert runner.calls[-1][-1] == "a%sb"


def test_deeplinks_permissions_and_swipes(android: tuple[NativeSurface, Runner], ios: tuple[NativeSurface, Runner]) -> None:
    surface, runner = android
    run_native(surface, "ui.deeplink", {"url": "eksengold://invoice/new"}, 1000)
    assert runner.calls[-1][3:] == ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "eksengold://invoice/new"]
    surface.set_permissions("grant", "com.eksenova.gold")
    granted = [call[-1] for call in runner.calls if "grant" in call]
    assert granted == ["android.permission.CAMERA", "android.permission.POST_NOTIFICATIONS"]
    ios_surface, ios_runner = ios
    ios_surface.set_permissions("deny", "com.eksenova.gold")
    assert ios_runner.calls[-1] == ["xcrun", "simctl", "privacy", "UDID-1", "revoke", "all", "com.eksenova.gold"]
    run_native(ios_surface, "ui.swipe", {"direction": "up"}, 1000)
    assert ios_runner.calls[-1][:2] == ["/usr/local/bin/idb", "ui"] and ios_runner.calls[-1][2] == "swipe"
    run_native(ios_surface, "ui.tap", {"target": {"by": "test_id", "args": {"id": "allow"}, "surface": "native"}}, 1000)
    assert ios_runner.calls[-1] == ["/usr/local/bin/idb", "ui", "tap", "100", "522", "--udid", "UDID-1"]


def test_ios_without_idb_is_native_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native.shutil, "which", lambda name: None)
    surface = NativeSurface(kind="ios", udid="UDID-1", runner=Runner(IOS_DESCRIBE))
    with pytest.raises(DriverError) as missing:
        run_native(surface, "ui.tap", {"target": text("Allow")}, 1000)
    assert missing.value.code == "native_unavailable" and "idb" in missing.value.message


def test_ios_deeplinks_confirm_the_open_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native.shutil, "which", lambda name: "/usr/local/bin/idb" if name == "idb" else None)
    prompt = json.dumps([
        {"AXLabel": "Open in “Referans”?", "type": "StaticText", "frame": {"x": 71, "y": 410, "width": 260, "height": 20}},
        {"AXLabel": "Cancel", "type": "Button", "frame": {"x": 57, "y": 450, "width": 140, "height": 48}},
        {"AXLabel": "Open", "type": "Button", "frame": {"x": 205, "y": 450, "width": 140, "height": 48}},
    ])
    runner = Runner(prompt)
    surface = NativeSurface(kind="ios", udid="UDID-1", runner=runner)

    def answer(argv: Sequence[str], timeout: float) -> tuple[int, str]:
        result = Runner.__call__(runner, argv, timeout)
        if "tap" in argv:
            runner.dump = json.dumps([])
        return result

    surface.runner = answer
    run_native(surface, "ui.deeplink", {"url": "eksref://invoice/9"}, 1000)
    taps = [call for call in runner.calls if "tap" in call]
    assert taps == [["/usr/local/bin/idb", "ui", "tap", "275", "474", "--udid", "UDID-1"]]


def test_a_stale_idb_companion_is_restarted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native.shutil, "which", lambda name: "/usr/local/bin/idb" if name == "idb" else None)
    calls: list[list[str]] = []
    failures = iter([(1, "The dtuhidd connection has been invalidated; events sent on it would be discarded")])

    def runner(argv: Sequence[str], timeout: float) -> tuple[int, str]:
        calls.append(list(argv))
        if "tap" in argv:
            return next(failures, (0, ""))
        return 0, ""

    NativeSurface(kind="ios", udid="UDID-1", runner=runner).tap(10, 20)
    assert [call[1] for call in calls] == ["ui", "kill", "ui"]
