from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from eks_harness.config import Config
from eks_harness.drivers.profile import (
    ProfileError,
    load_profile,
    mobile_worker_config,
    pace_from_config,
    web_worker_config,
)
from eks_harness.drivers.registry import driver_extensions, driver_for, flow_helpers
from eks_harness.plugins import PluginHost


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_profiles_expand_env_and_build_worker_configs(tmp_path: Path, config: Config) -> None:
    write(tmp_path / "web-app/.harness/app.toml", """
        [app]
        project = "acme/web-app"
        platform = "web"
        [web]
        url = "http://localhost:${WEB_PORT:-3000}"
        api_url = "${API_URL}"
        locale = "de-DE"
        hide = [".devtools"]
        viewports = { desktop = { width = 1280, height = 800 } }
        [checks]
        noise = "analytics"
    """)
    profile = load_profile(tmp_path / "web-app/.harness", env={"API_URL": "http://api"})
    assert profile.project == "acme/web-app" and profile.platforms == ("web",)
    assert profile.web["url"] == "http://localhost:3000"
    pace = pace_from_config(config)
    assert pace["moveMs"] == 300
    built = web_worker_config(profile, sid="s1", extensions=[], pace=pace, hide=["#x"], playwright_dirs=[tmp_path])
    assert built["baseUrl"] == "http://localhost:3000" and built["apiBaseUrl"] == "http://api"
    assert built["hideSelectors"] == ["#x", ".devtools"] and built["locale"] == "de-DE"
    assert built["viewports"]["desktop"]["width"] == 1280
    assert built["encoder"][1:] == ["-m", "eks_harness.capture.encode"]
    assert built["playwrightFrom"] == [str(tmp_path)]

    write(tmp_path / "mobile-app/.harness/app.toml", """
        [mobile]
        metro_port = 8081
        fixtures = ["fixtures"]
        fake_slots = ["nfc", "boarding-pass"]
    """)
    mobile = load_profile(tmp_path / "mobile-app/.harness")
    assert mobile.platforms == ("ios", "android")
    built = mobile_worker_config(mobile, sids={"ios": "s2"}, extensions=[], pace={})
    assert built["metroPort"] == 8081 and built["fakeSlots"] == ["nfc", "boarding-pass"]
    assert built["fixtureDirs"] == [str((tmp_path / "mobile-app/fixtures").resolve())]

    write(tmp_path / "bad/.harness/app.toml", '[app]\nplatform = "tv"\n')
    with pytest.raises(ProfileError, match="unknown platform"):
        load_profile(tmp_path / "bad/.harness")
    write(tmp_path / "nourl/.harness/app.toml", '[app]\nplatform = "web"\n')
    with pytest.raises(ProfileError, match="url"):
        web_worker_config(load_profile(tmp_path / "nourl/.harness"), sid="s", extensions=[], pace={})


def test_extensions_helpers_and_builtin_drivers_come_from_plugins(tmp_path: Path, config: Config) -> None:
    tree = tmp_path / "repo"
    plugin = tree / ".harness/plugins/acme"
    write(plugin / "harness-plugin.toml", """
        [plugin]
        id = "acme.app"

        [[contributes.driver_extension]]
        path = "web/auth.mjs"
        platform = "web"

        [[contributes.driver_extension]]
        id = "fakes"
        path = "mobile/fakes.mjs"
        platforms = ["ios", "android"]

        [[contributes.flow_helpers]]
        id = "web-helpers"
        entry = "acme_helpers:WebHelpers"
        platform = "web"

        [config]
        tenant = { type = "str", default = "demo" }
    """)
    write(plugin / "web/auth.mjs", "export default () => ({})\n")
    write(plugin / "mobile/fakes.mjs", "export default () => ({})\n")
    write(plugin / "acme_helpers.py", "class WebHelpers:\n    def menu(self, label):\n        return label\n")
    write(tree / ".harness/project.toml", '[plugins]\ntrust = ["acme"]\n')
    host = PluginHost(config.paths, config)
    web = driver_extensions(host, "web", tree)
    assert [e["id"] for e in web] == ["acme.app:auth.mjs"] and web[0]["settings"] == {"tenant": "demo"}
    assert web[0]["module"].endswith("web/auth.mjs")
    assert [e["id"] for e in driver_extensions(host, "android", tree)] == ["acme.app:fakes"]
    helpers = flow_helpers(host, "web", tree)
    assert [h.__name__ for h in helpers] == ["WebHelpers"] and flow_helpers(host, "ios", tree) == []
    driver = driver_for(host, "ios", tree)
    assert driver.platform == "ios" and "patch" in driver.capabilities()["act"]
    assert driver_for(host, "web").capabilities()["lease"] == "browser"


def test_profile_extends_another_app(tmp_path: Path) -> None:
    write(tmp_path / "apps/mobile/.harness/app.toml", """
        [app]
        project = "acme/mobile"
        platforms = ["ios", "android"]
        [mobile]
        bundle_id = "com.acme.mobile"
        metro = { port = 8081, reset = false }
    """)
    write(tmp_path / "apps/mobile/.harness/app.py", "from eks_harness.flows import MobileApp\n\n\nclass App(MobileApp):\n    pass\n")
    write(tmp_path / "ads/.harness/app.toml", """
        [app]
        extends = "../apps/mobile"
        project = "acme/ads"
        [mobile]
        metro = { reset = true }
    """)
    profile = load_profile(tmp_path / "ads/.harness")
    assert profile.root == (tmp_path / "apps/mobile").resolve()
    assert profile.project == "acme/ads"
    assert profile.platforms == ("ios", "android")
    assert profile.mobile == {"bundle_id": "com.acme.mobile", "metro": {"port": 8081, "reset": True}}
    assert profile.app_file == (tmp_path / "apps/mobile/.harness/app.py").resolve()
    assert "extends" not in profile.raw["app"]

    write(tmp_path / "ads/.harness/app.py", "from eks_harness.flows import MobileApp\n\n\nclass App(MobileApp):\n    pass\n")
    assert load_profile(tmp_path / "ads/.harness").app_file == tmp_path / "ads/.harness/app.py"


def test_profile_extends_rejects_loops_and_missing_targets(tmp_path: Path) -> None:
    write(tmp_path / "a/.harness/app.toml", '[app]\nextends = "../b"\n')
    write(tmp_path / "b/.harness/app.toml", '[app]\nextends = "../a"\n')
    with pytest.raises(ProfileError, match="loops"):
        load_profile(tmp_path / "a/.harness")
    write(tmp_path / "c/.harness/app.toml", '[app]\nextends = "../nowhere"\n')
    with pytest.raises(ProfileError, match="no .harness directory"):
        load_profile(tmp_path / "c/.harness")
