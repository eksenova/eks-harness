from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import eks
from eks import t
from eks.workspace import seed_info
from eks_harness.engine.validator import Issue, Report
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry

NOW = "2026-01-15T09:00+03:00"
FROZEN = "2026-01-15T09:00:00+03:00"


def issues_of(report: Report, code: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.code == code]


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def register_standard(*, backend: str = "eksenapi", now: str | None = NOW, name: str = "standard") -> None:
    @eks.resource("member", persona=True)
    class Member(eks.Resource):
        role: str = "admin"

    options: dict[str, Any] = {"backend": backend, "seed": 2026}
    if now is not None:
        options["now"] = now

    def standard(d: eks.Data) -> None:
        d.member("host")

    standard.__name__ = name
    eks.seed(name, **options)(standard)


def test_a_flow_inherits_now_root_seed_and_backend_from_its_seed(eks_registry: Registry) -> None:
    register_standard()

    @eks.flow(app="web-app", seed="standard", platforms=["web"], timezone="Europe/Istanbul", locale="tr-TR")
    def subeler(s: eks.Session) -> None:
        s.ui.nav("/tenant")

    plan = eks.plan(subeler)
    assert plan.clock.now == FROZEN
    assert plan.clock.seed == 2026
    assert plan.session is not None
    assert (plan.session.backend, plan.session.now, plan.session.root_seed) == ("eksenapi", FROZEN, 2026)
    (backend,) = [resource for resource in plan.resources if resource.kind == "backend"]
    assert backend.definition == "eksenapi"
    assert eks.validate(plan).issues == ()


def test_own_values_win_over_the_seed(eks_registry: Registry) -> None:
    register_standard()

    @eks.flow(app="web-app", seed="standard", platforms=["web"], now="2026-02-01T10:00+03:00", root_seed=5)
    def own(s: eks.Session) -> None:
        s.ui.nav("/tenant")

    plan = eks.plan(own)
    assert (plan.clock.now, plan.clock.seed) == ("2026-02-01T10:00:00+03:00", 5)


def test_a_take_inherits_from_its_seed(eks_registry: Registry) -> None:
    register_standard()

    @eks.score(fps=30, bpm=120)
    def ad(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app", seed="standard", timezone="Europe/Istanbul", locale="tr-TR")
        with take.flow() as s:
            s.ui.nav("/tenant", at=t("bar:1"))

    plan = eks.plan(ad)
    (take,) = nodes_of(plan, "score.take")
    session: Any = take.args["session"]
    assert (session["backend"], session["now"], session["root_seed"]) == ("eksenapi", FROZEN, 2026)
    assert eks.validate(plan).issues == ()


def test_a_take_without_now_is_warned(eks_registry: Registry) -> None:
    @eks.score(fps=30, bpm=120)
    def bare(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.nav("/tenant", at=t("bar:1"))

    [issue] = issues_of(eks.validate(eks.plan(bare)), "clock.now")
    assert issue.severity == "warning"
    assert issue.field == "session.now"


def test_flows_and_seeds_without_now_are_warned(eks_registry: Registry) -> None:
    register_standard(now=None, name="timeless")

    @eks.flow(app="web-app", platforms=["web"])
    def loose(s: eks.Session) -> None:
        s.ui.nav("/tenant")

    [flow_issue] = issues_of(eks.validate(eks.plan(loose)), "clock.now")
    assert flow_issue.severity == "warning"
    [seed_issue] = issues_of(eks.validate(eks.plan(eks_registry.entry("seed", "timeless"))), "clock.now")
    assert seed_issue.severity == "warning"


def test_extending_an_unknown_seed(eks_registry: Registry) -> None:
    @eks.seed("orphan", backend="eksenapi", now=NOW)
    def orphan(d: eks.Data) -> None:
        d.extend("no-such-seed")

    plan = eks.plan(orphan)
    (extend,) = nodes_of(plan, "data.extend")
    assert "hash" not in extend.args
    [issue] = issues_of(eks.validate(plan), "seed.unknown")
    assert "'no-such-seed'" in issue.message


def test_extending_a_seed_on_another_backend_or_now(eks_registry: Registry) -> None:
    register_standard()

    @eks.seed("other", backend="otherapi", now="2026-03-01T10:00+03:00")
    def other(d: eks.Data) -> None:
        d.extend("standard")

    report = eks.validate(eks.plan(other))
    assert issues_of(report, "seed.backend")
    assert issues_of(report, "seed.now")


def test_a_flow_on_another_backend_than_its_seed(eks_registry: Registry) -> None:
    register_standard()

    @eks.flow(app="web-app", seed="standard", backend="otherapi", platforms=["web"])
    def mismatched(s: eks.Session) -> None:
        s.ui.nav("/tenant")

    [issue] = issues_of(eks.validate(eks.plan(mismatched)), "seed.backend")
    assert "'otherapi'" in issue.message


def test_the_extend_hash_follows_the_base_seed(eks_registry: Registry) -> None:
    @eks.resource("member", persona=True)
    class Member(eks.Resource):
        role: str = "admin"

    before = eks_registry.snapshot()

    @eks.seed("standard", backend="eksenapi", now=NOW, seed=2026)
    def standard(d: eks.Data) -> None:
        d.member("host")

    @eks.seed("feature", backend="eksenapi", now=NOW)
    def feature(d: eks.Data) -> None:
        d.extend("standard")
        d.member("moda")

    first = eks.plan(feature)
    (extend,) = nodes_of(first, "data.extend")
    base = seed_info("standard")
    assert base is not None
    assert extend.args == {"base": "standard", "hash": base.hash}
    assert eks.validate(first).issues == ()
    feature_entry = eks_registry.entry("seed", "feature")

    # the base changes: the extending seed's plan hash (its snapshot key) changes with it
    eks_registry.restore(before)

    @eks.seed("standard", backend="eksenapi", now=NOW, seed=2026)
    def standard_again(d: eks.Data) -> None:
        d.member("host")
        d.member("extra")

    eks_registry.register_entry(feature_entry)
    second = eks.plan(feature)
    assert second.hash != first.hash
    [issue] = issues_of(eks.validate(first), "seed.stale")
    assert issue.field == "hash"
    assert eks.validate(second).issues == ()
    assert standard is not standard_again
    assert eks_registry.resource("member").model is Member


def test_extend_cycles_are_reported(eks_registry: Registry) -> None:
    register_standard()

    @eks.seed("ping", backend="eksenapi", now=NOW, base="pong")
    def ping(d: eks.Data) -> None:
        d.member("ayse")

    @eks.seed("pong", backend="eksenapi", now=NOW)
    def pong(d: eks.Data) -> None:
        d.extend("ping")

    assert issues_of(eks.validate(eks.plan(ping)), "seed.cycle")


@pytest.fixture
def app_tree(tmp_path: Path) -> Path:
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "project.toml").write_text('[project]\nid = "apps"\n', encoding="utf-8")
    harness = tmp_path / "web-app" / ".harness"
    harness.mkdir(parents=True)
    (harness / "app.toml").write_text('[app]\nname = "web-app"\nplatforms = ["web"]\n', encoding="utf-8")
    return tmp_path


def test_an_unknown_app_is_reported(eks_registry: Registry, app_tree: Path) -> None:
    @eks.flow(app="no-such-app", platforms=["web"], now=NOW)
    def lost(s: eks.Session) -> None:
        s.ui.nav("/tenant")

    plan = eks.plan(lost)
    assert issues_of(eks.validate(plan), "resource.app") == []
    found = issues_of(eks.validate(plan, tree=app_tree), "resource.app")
    assert found and all("'no-such-app'" in issue.message for issue in found)


def test_a_platform_the_app_does_not_list(eks_registry: Registry, app_tree: Path) -> None:
    @eks.flow(app="web-app", platforms=["ios"], now=NOW)
    def wrong(s: eks.Session) -> None:
        s.ui.nav("/tenant")

    [issue] = issues_of(eks.validate(eks.plan(wrong), tree=app_tree), "resource.app")
    assert "ios" in issue.message
    assert "web" in issue.message

    @eks.flow(app="web-app", platforms=["web"], now=NOW)
    def right(s: eks.Session) -> None:
        s.ui.nav("/tenant")

    assert issues_of(eks.validate(eks.plan(right), tree=app_tree), "resource.app") == []


def test_seed_info_follows_a_reloaded_base_down_the_extend_chain(tmp_path: Path, eks_registry: Registry) -> None:
    # z <- a <- b: reloading z must refresh the cached plan of a (its data.extend hash names z's plan)
    from eks.discovery import load_file
    from eks.planning import seed_plan

    @eks.resource("member", persona=True)
    class Member(eks.Resource):
        role: str = "admin"

    seeds = tmp_path / ".harness" / "seeds"
    seeds.mkdir(parents=True)
    (tmp_path / ".harness" / "project.toml").write_text("[project]\n", encoding="utf-8")
    header = "import eks\n\n\n"
    z_file = seeds / "z.py"
    z_file.write_text(header + f"@eks.seed('z', backend='eksenapi', now={NOW!r})\ndef z(d: eks.Data) -> None:\n"
                      "    d.member('zeynep')\n", encoding="utf-8")
    (seeds / "a.py").write_text(header + f"@eks.seed('a', backend='eksenapi', now={NOW!r}, base='z')\n"
                                "def a(d: eks.Data) -> None:\n    d.member('ayse')\n", encoding="utf-8")
    (seeds / "b.py").write_text(header + f"@eks.seed('b', backend='eksenapi', now={NOW!r}, base='a')\n"
                                "def b(d: eks.Data) -> None:\n    d.member('burak')\n", encoding="utf-8")
    for name in ("z", "a", "b"):
        load_file(seeds / f"{name}.py")
    first = seed_info("a")
    assert first is not None
    assert issues_of(eks.validate(seed_plan(eks_registry.entry("seed", "b"))), "seed.stale") == []

    z_file.write_text(header + f"@eks.seed('z', backend='eksenapi', now={NOW!r})\ndef z(d: eks.Data) -> None:\n"
                      "    d.member('zeynep')\n    d.member('zafer')\n", encoding="utf-8")
    load_file(z_file)
    second = seed_info("a")
    assert second is not None
    assert second.hash != first.hash
    assert second.hash == seed_plan(eks_registry.entry("seed", "a")).hash
    b_plan = seed_plan(eks_registry.entry("seed", "b"))
    assert eks.validate(b_plan).issues == ()
    assert seed_info("b") is not None
    assert seed_info("b").hash == b_plan.hash  # type: ignore[union-attr]
    assert eks_registry.resource("member").model is Member
