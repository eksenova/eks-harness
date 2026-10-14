from __future__ import annotations

from pathlib import Path

import pytest

import eks
from eks.discovery import tree_registry
from eks.planning import PlanBuildError
from eks.workspace import plan_spec
from eks_harness.engine.validator import Report
from eks_harness.ir.plan import CheckUse, Plan
from eks_harness.ir.presets import Viewport
from eks_harness.ir.registry import Registry
from eks_harness.ir.time import t

WEB_PROFILE = """
[app]
name = "web-app"
platform = "web"

[web]
url = "http://localhost:3000"
locale = "tr-TR"
timezone = "Europe/Istanbul"
network_ignore = "/hubs/|_next/webpack-hmr"

[web.viewports.desktop]
width = 1440
height = 900

[web.viewports.mobile]
width = 390
height = 844
mobile = true
touch = true

[checks]
noise = "google|sentry"

[defaults]
timeout = "20s"
pace = "demo"
viewport = "desktop"
checks = [{kind = "perf", params = {lcp = "2500ms"}}, "visual"]
"""

MOBILE_PROFILE = """
[app]
name = "mobile-app"
platforms = ["ios", "android"]

[mobile]
locale = "tr-TR"
timezone = "Europe/Istanbul"

[checks]
noise = ["analytics"]
"""


def write_tree(root: Path, flows: dict[str, str]) -> Path:
    (root / ".harness" / "flows").mkdir(parents=True)
    (root / ".harness" / "project.toml").write_text("[project]\n", encoding="utf-8")
    for app, profile in (("web-app", WEB_PROFILE), ("mobile-app", MOBILE_PROFILE)):
        (root / app / ".harness").mkdir(parents=True)
        (root / app / ".harness" / "app.toml").write_text(profile, encoding="utf-8")
    for name, body in flows.items():
        (root / ".harness" / "flows" / f"{name}.py").write_text(
            "import eks\nfrom eks import t\n\n\n" + body, encoding="utf-8")
    return root


def plan_of(root: Path, name: str, platform: str | None = None) -> Plan:
    return plan_spec(f".harness/flows/{name}.py", cwd=root, platform=platform, plugins=False)


def clean(report: Report) -> None:
    assert report.issues == (), report.text()


PLAIN = "@eks.flow(app='web-app', now='2026-01-15T09:00+03:00')\ndef plain(s: eks.Session):\n    s.checkpoint('home', viewport='mobile')\n"


def test_session_defaults_come_from_the_app_profile(tmp_path: Path, eks_registry: Registry) -> None:
    root = write_tree(tmp_path, {"plain": PLAIN})
    plan = plan_of(root, "plain")
    session = plan.session
    assert session is not None
    assert session.timeout == t("20s")
    assert (session.pace, session.viewport) == ("demo", "desktop")
    assert (session.timezone, session.locale) == ("Europe/Istanbul", "tr-TR")
    assert session.network_ignore == ("/hubs/", "_next/webpack-hmr")
    assert session.noise == ("google", "sentry")
    # the app's own viewports win over presets of the same name
    assert session.viewports == {"desktop": Viewport(width=1440, height=900),
                                 "mobile": Viewport(width=390, height=844, mobile=True, touch=True)}
    assert session.checks == (CheckUse(kind="perf", params={"lcp": "2.5s"}),
                              CheckUse(kind="visual", params={"threshold": 0.001, "masks": []}))
    # 'mobile' is not a registered preset but the profile defines it
    clean(eks.validate(plan, tree=root))


def test_eks_plan_folds_the_same_profile(tmp_path: Path, eks_registry: Registry) -> None:
    root = write_tree(tmp_path, {"plain": PLAIN})
    through_cli = plan_of(root, "plain")
    direct = eks.plan(tree_registry(root, plugins=False).entry("flow", "plain"))
    assert direct.to_json() == through_cli.to_json()


def test_flow_options_win_and_lists_merge(tmp_path: Path, eks_registry: Registry) -> None:
    root = write_tree(tmp_path, {"own": (
        "@eks.flow(app='web-app', timeout=t('5s'), viewport='mobile', noise=['favicon', 'google'], locale='en-US',\n"
        "          checks=[eks.checks.use('perf', lcp=t('1s')), 'console_errors'])\n"
        "def own(s: eks.Session):\n    s.checkpoint('home')\n")})
    session = plan_of(root, "own").session
    assert session is not None
    assert session.timeout == t("5s")
    assert session.viewport == "mobile"
    assert session.locale == "en-US"
    assert session.timezone == "Europe/Istanbul"
    assert session.noise == ("google", "sentry", "favicon")
    # a flow check replaces the profile check of the same kind; other profile checks stay
    assert [use.kind for use in session.checks] == ["visual", "perf", "console_errors"]
    assert session.checks[1].params == {"lcp": "1s"}


def test_the_profile_is_part_of_the_plan_hash(tmp_path: Path, eks_registry: Registry) -> None:
    root = write_tree(tmp_path, {"plain": PLAIN})
    first = plan_of(root, "plain")
    profile = root / "web-app" / ".harness" / "app.toml"
    profile.write_text(WEB_PROFILE.replace("width = 1440", "width = 1280"), encoding="utf-8")
    second = plan_of(root, "plain")
    assert second.hash != first.hash
    profile.write_text(WEB_PROFILE.replace('timezone = "Europe/Istanbul"', 'timezone = "UTC"'), encoding="utf-8")
    assert plan_of(root, "plain").hash not in (first.hash, second.hash)


def test_mobile_sessions_read_the_mobile_table(tmp_path: Path, eks_registry: Registry) -> None:
    root = write_tree(tmp_path, {"phone": (
        "@eks.flow(app='mobile-app')\ndef phone(s: eks.Session):\n    s.ui.tap('#save')\n")})
    session = plan_of(root, "phone", platform="ios").session
    assert session is not None
    assert (session.timezone, session.locale, session.noise) == ("Europe/Istanbul", "tr-TR", ("analytics",))
    assert session.viewports == {}


def test_unknown_viewports_still_fail(tmp_path: Path, eks_registry: Registry) -> None:
    root = write_tree(tmp_path, {"wide": (
        "@eks.flow(app='web-app')\ndef wide(s: eks.Session):\n    s.checkpoint('home', viewport='ultrawide')\n")})
    report = eks.validate(plan_of(root, "wide"), tree=root)
    [issue] = [issue for issue in report.issues if issue.code == "preset.unknown"]
    assert "desktop" in issue.message


def test_bad_profile_values_fail_planning(tmp_path: Path, eks_registry: Registry) -> None:
    root = write_tree(tmp_path, {"plain": PLAIN})
    profile = root / "web-app" / ".harness" / "app.toml"
    profile.write_text(WEB_PROFILE.replace('timezone = "Europe/Istanbul"', "timezone = 3"), encoding="utf-8")
    with pytest.raises(PlanBuildError, match=r"\[web\] timezone"):
        plan_of(root, "plain")
    profile.write_text(WEB_PROFILE.replace('"visual"]', '"no_such_check"]'), encoding="utf-8")
    with pytest.raises(PlanBuildError, match="no_such_check"):
        plan_of(root, "plain")


def test_session_checks_are_validated(tmp_path: Path, eks_registry: Registry) -> None:
    root = write_tree(tmp_path, {"plain": PLAIN})
    plan = plan_of(root, "plain")
    assert plan.session is not None
    broken = plan.session.model_copy(update={"checks": (CheckUse(kind="perf", params={"lcp": 5}),
                                                       CheckUse(kind="cut"))})
    report = eks.validate(plan.model_copy(update={"session": broken, "hash": ""}), tree=root)
    # the session is edited after hashing, so plan.hash is reported too
    assert {(issue.code, issue.field) for issue in report.errors if issue.code != "plan.hash"} == {
        ("check.args", "checks.0.params"), ("check.kind", "checks.1.kind")}
