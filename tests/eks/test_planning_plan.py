import importlib.util
import textwrap
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

import eks
from eks import t
from eks.checks import default_checks
from eks.planning import (
    ROOT_FACTORIES,
    PlanBuildError,
    base_clock,
    resolve_platform,
    root_factory,
    session_spec,
    trace,
)
from eks.session import open_flow_session
from eks.tracing import TraceContext
from eks_harness.ir.plan import Clock, SessionSpec
from eks_harness.ir.registry import EntrySpec, Registry
from eks_harness.ir.time import Meter, TempoMap


def entry_of(registry: Registry, fn: Any) -> EntrySpec:
    return registry.entry_for(fn)


def test_platform_from_the_flow(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def web_only(s: eks.Session) -> None:
        return None

    entry = entry_of(eks_registry, web_only)
    assert resolve_platform(entry, None) == "web"
    assert resolve_platform(entry, "web") == "web"
    assert eks.plan(web_only).session == SessionSpec(app="web-app", platform="web", defaults=default_checks("web"))


def test_matrix_flow_needs_a_platform(eks_registry: Registry) -> None:
    @eks.flow(app="mobile-app", platforms=["ios", "android"])
    def matrix(s: eks.Session) -> None:
        return None

    entry = entry_of(eks_registry, matrix)
    assert resolve_platform(entry, None) is None
    assert resolve_platform(entry, "android") == "android"
    with pytest.raises(PlanBuildError, match="pass platform="):
        eks.plan(matrix)
    plan = eks.plan(matrix, platform="ios")
    assert plan.session is not None
    assert plan.session.platform == "ios"
    assert plan.params == {}
    assert {resource.kind for resource in plan.resources} == {"lease.ios"}


def test_requested_platform_must_be_a_candidate(eks_registry: Registry) -> None:
    @eks.flow(app="mobile-app", platforms=["ios", "android"])
    def mobile(s: eks.Session) -> None:
        return None

    with pytest.raises(PlanBuildError, match="'web' is not one of them"):
        eks.plan(mobile, platform="web")
    with pytest.raises(PlanBuildError, match="unknown platform 'desktop'"):
        resolve_platform(entry_of(eks_registry, mobile), "desktop")


def test_app_platforms_are_the_fallback(eks_registry: Registry) -> None:
    @eks.flow(app="web-app")
    def from_app(s: eks.Session) -> None:
        return None

    entry = entry_of(eks_registry, from_app)
    assert resolve_platform(entry, None) is None
    assert resolve_platform(entry, None, ("web",)) == "web"
    assert resolve_platform(entry, None, ("ios", "android")) is None
    assert resolve_platform(entry, "android", ("ios", "android")) == "android"
    with pytest.raises(PlanBuildError, match="not one of them"):
        resolve_platform(entry, "web", ("ios", "android"))
    # without flow or app platforms any known platform may be requested
    assert resolve_platform(entry, "ios") == "ios"
    with pytest.raises(PlanBuildError, match="pass platform="):
        eks.plan(from_app)
    assert trace(entry, platform="web").session == SessionSpec(app="web-app", platform="web", defaults=default_checks("web"))


def test_platform_on_other_kinds_raises(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def clip(e: Any) -> None:
        return None

    entry = entry_of(eks_registry, clip)
    with pytest.raises(PlanBuildError, match="flows only"):
        trace(entry, platform="web", root=lambda ctx, spec: None)
    with pytest.raises(PlanBuildError, match="not a flow"):
        resolve_platform(entry, None)
    with pytest.raises(PlanBuildError, match="not a flow"):
        session_spec(entry, "web")


def test_session_spec_copies_flow_options(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web", seed="standard", as_="harman-admin", backend="eksenapi",
              viewport="phone", pace="demo", timeout=t("30s"), noise=("favicon",), root_seed=7)
    def options(s: eks.Session) -> None:
        return None

    spec = session_spec(entry_of(eks_registry, options), "web")
    assert spec == SessionSpec(
        key="main", app="web-app", platform="web", seed="standard", persona="harman-admin", backend="eksenapi",
        viewport="phone", pace="demo", timeout=t("30s"), noise=("favicon",), root_seed=7,
        defaults=default_checks("web"))


def test_base_clock_of_flows_and_seeds(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web", now="2026-01-15T09:00+03:00", root_seed=2026)
    def timed(s: eks.Session) -> None:
        return None

    @eks.flow(app="web-app", platform="web")
    def untimed(s: eks.Session) -> None:
        return None

    @eks.seed("p4-clock-seed", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=11)
    def seeded(d: Any) -> None:
        return None

    assert base_clock(entry_of(eks_registry, timed)) == Clock(now="2026-01-15T09:00:00+03:00", seed=2026)
    assert base_clock(entry_of(eks_registry, untimed)) == Clock(seed=0)
    assert base_clock(entry_of(eks_registry, seeded)) == Clock(now="2026-01-15T09:00:00+03:00", seed=11)
    plan = eks.plan(timed)
    assert plan.clock.now == "2026-01-15T09:00:00+03:00"
    assert plan.clock.seed == 2026


def test_base_clock_of_edits(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920), seed=7, duration=t("15s"), window=t.span(t("0s"), t("10s")))
    def edited(e: Any) -> None:
        return None

    @eks.edit(fps="30000/1001", size=(1920, 1080))
    def ntsc(e: Any) -> None:
        return None

    clock = base_clock(entry_of(eks_registry, edited))
    assert clock.seed == 7
    assert clock.fps == Fraction(30)
    assert clock.duration == t("15s")
    assert clock.window == t.span(t("0s"), t("10s"))
    assert clock.tempo is None
    assert base_clock(entry_of(eks_registry, ntsc)).fps == Fraction(30000, 1001)


def test_base_clock_of_scenes(eks_registry: Registry) -> None:
    @eks.scene(kind="blender", engine="cycles", samples=32, seed=7)
    def external(sc: Any) -> None:
        return None

    @eks.scene(kind="gfx", fps=60, bpm=120, duration=t("4s"))
    def tempo(sc: Any) -> None:
        return None

    first = base_clock(entry_of(eks_registry, external))
    assert first.seed == 7
    assert first.tempo == TempoMap(external=True)
    assert first.fps is None
    second = base_clock(entry_of(eks_registry, tempo))
    assert second.tempo == TempoMap(bpm=120.0)
    assert second.fps == Fraction(60)
    assert second.duration == t("4s")


def test_base_clock_of_scores(eks_registry: Registry) -> None:
    @eks.score(fps=30, bpm=128, meter=(3, 4), seed=5, window=t.span(t("0s"), t("15s")), duration=t("20s"))
    def metered(sc: Any) -> None:
        return None

    @eks.score(fps=30, bpm=90)
    def common(sc: Any) -> None:
        return None

    @eks.score(fps=30, song="music/song.ogg")
    def song(sc: Any) -> None:
        return None

    clock = base_clock(entry_of(eks_registry, metered))
    assert clock.tempo == TempoMap(bpm=128.0, meter=Meter(numerator=3, denominator=4))
    assert clock.seed == 5
    assert clock.window == t.span(t("0s"), t("15s"))
    assert clock.duration == t("20s")
    assert base_clock(entry_of(eks_registry, common)).tempo == TempoMap(bpm=90.0, meter=Meter())
    # a song's tempo comes from its analysis; the score builder sets it, not the base clock
    assert base_clock(entry_of(eks_registry, song)).tempo is None


def test_outputs_are_encoded() -> None:
    @eks.flow(app="web-app", platform="web")
    def outputs(s: eks.Session) -> dict[str, Any]:
        rows = s.record_op("api.get", {"path": "/api/rows"})
        return {"x": rows, "first": rows.items[0].id, "label": f"n={rows.total}", "when": t("1s"), "n": 3}

    plan = eks.plan(outputs)
    (rows,) = plan.nodes
    assert plan.outputs == {
        "x": {"$ref": rows.id, "path": []},
        "first": {"$ref": rows.id, "path": ["items", 0, "id"]},
        "label": {"$fmt": ["n=", {"$ref": rows.id, "path": ["total"]}]},
        "when": "1s",
        "n": 3,
    }


def test_no_return_value_means_no_outputs() -> None:
    @eks.flow(app="web-app", platform="web")
    def silent(s: eks.Session) -> None:
        return None

    plan = eks.plan(silent)
    assert plan.outputs is None
    assert '"outputs"' not in plan.to_json()


def test_tags_and_name_come_from_the_entry() -> None:
    @eks.flow("p4-tagged", app="web-app", platform="web", tags=["subeler", "smoke"])
    def tagged(s: eks.Session) -> None:
        return None

    plan = eks.plan(tagged)
    assert plan.name == "p4-tagged"
    assert plan.kind == "flow"
    assert plan.tags == ["subeler", "smoke"]


def load_flow(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("p4_plan_tree_flow", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_and_base_for_a_file_in_a_tree(tmp_path: Path, eks_registry: Registry) -> None:
    folder = tmp_path / "web-app" / ".harness" / "flows"
    folder.mkdir(parents=True)
    path = folder / "liste.py"
    path.write_text(textwrap.dedent("""\
        import eks


        @eks.flow(app="web-app", platform="web")
        def liste(s: eks.Session):
            s.record_op("ui.nav", {"route": "/tenant/settings/branches"})
    """), encoding="utf-8")
    module = load_flow(path)
    entry = eks_registry.entry_for(module.liste)
    plan = trace(entry, tree=tmp_path)
    assert plan.base == "web-app/.harness/flows"
    assert plan.source is not None
    assert plan.source.file == "web-app/.harness/flows/liste.py"
    assert plan.source.qualname == "liste"
    assert plan.source.line == 4
    (nav,) = plan.nodes
    assert nav.src == "web-app/.harness/flows/liste.py:6"
    # the same file planned without a tree is relative to its own folder
    alone = trace(entry)
    assert alone.base == "."
    assert alone.source is not None
    assert alone.source.file == "liste.py"
    assert alone.nodes[0].src == "liste.py:6"
    # ids use the path relative to the tree root; node hashes never cover ids, the plan hash covers base
    assert alone.nodes[0].id != nav.id
    assert alone.nodes[0].hash == nav.hash
    assert alone.hash != plan.hash


def test_file_at_the_tree_root_has_base_dot(tmp_path: Path, eks_registry: Registry) -> None:
    path = tmp_path / "rooted.py"
    path.write_text(
        'import eks\n\n\n@eks.flow(app="web-app", platform="web")\ndef liste(s: eks.Session):\n    return None\n',
        encoding="utf-8")
    plan = trace(eks_registry.entry_for(load_flow(path).liste), tree=tmp_path)
    assert plan.base == "."
    assert plan.source is not None
    assert plan.source.file == "rooted.py"


def test_root_override(eks_registry: Registry) -> None:
    calls: list[Any] = []

    class Builder:
        def __init__(self, ctx: TraceContext, entry: EntrySpec) -> None:
            self.ctx = ctx
            self.entry = entry

    def make(ctx: TraceContext, entry: EntrySpec) -> Builder:
        calls.append((ctx.plan_kind, ctx.name, entry.name))
        return Builder(ctx, entry)

    @eks.edit(fps=30, size=(1080, 1920))
    def custom(e: Builder, label: str = "x") -> dict[str, str]:
        e.ctx.record("media.source", {"uri": f"file:{label}.mp4"})
        return {"label": label}

    plan = trace(entry_of(eks_registry, custom), params={"label": "intro"}, root=make)
    assert calls == [("edit", "custom", "custom")]
    assert [node.args for node in plan.nodes] == [{"uri": "file:intro.mp4"}]
    assert plan.outputs == {"label": "intro"}
    assert plan.params == {"label": "intro"}
    assert plan.session is None


def test_root_override_for_a_flow_without_a_session_builder(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def plain(s: Any) -> str:
        return str(type(s).__name__)

    plan = trace(entry_of(eks_registry, plain), root=lambda ctx, entry: object())
    assert plan.outputs == "object"


def test_root_factories() -> None:
    assert set(ROOT_FACTORIES) == {"flow", "seed", "edit", "scene", "score"}
    assert root_factory("flow") is open_flow_session
    with pytest.raises(PlanBuildError, match="no root builder"):
        root_factory("movie")


def test_plan_rejects_functions_that_are_not_entries() -> None:
    def not_an_entry(s: eks.Session) -> None:
        return None

    with pytest.raises(PlanBuildError, match="not a registered"):
        eks.plan(not_an_entry)


def test_plan_takes_an_entry_spec(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def by_spec(s: eks.Session, n: int = 1) -> int:
        return n

    assert eks.plan(entry_of(eks_registry, by_spec), n=4).outputs == 4


def test_platform_keyword_is_reserved_for_flows(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platforms=["web"])
    def reserved(s: eks.Session) -> None:
        return None

    plan = eks.plan(reserved, platform="web")
    assert plan.params == {}
    with pytest.raises(PlanBuildError, match="platform name"):
        eks.plan(reserved, platform=3)
    assert entry_of(eks_registry, reserved).kind == "flow"


def test_errors_restore_the_trace_context() -> None:
    from eks.tracing import active

    @eks.flow(app="web-app", platform="web")
    def failing(s: eks.Session) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        eks.plan(failing)
    assert active() is None
