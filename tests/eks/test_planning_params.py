import dataclasses
import enum
import importlib.util
import textwrap
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field

import eks
from eks import Target, Time, by, t
from eks.planning import PlanBuildError, coerce_params, parse_sets, trace
from eks.ref import Ref, node_of, type_of
from eks_harness.ir.hashing import derive_id
from eks_harness.ir.refs import RMedia
from eks_harness.ir.registry import EntrySpec, Registry

DEFAULT_DELAY = t("1s")
DEFAULT_TARGET = by.text("Kaydet")


class Window(BaseModel):
    width: int
    height: int = 800


class Mode(enum.Enum):
    FAST = "fast"
    SLOW = "slow"


def typed_entry(registry: Registry) -> EntrySpec:
    @eks.flow(app="web-app", platform="web")
    def typed(
        s: eks.Session,
        name: str,
        count: int = 1,
        ratio: float = 1.0,
        flag: bool = False,
        delay: Time = DEFAULT_DELAY,
        target: Target = DEFAULT_TARGET,
        speed: Literal["fast", "slow", 3] = "fast",
        mode: Mode = Mode.FAST,
        tags: list[str] | None = None,
        limits: dict[str, int] | None = None,
        window: Window | None = None,
        song: eks.Media | None = None,
        persona: eks.Persona | None = None,
        anything: Any = None,
        untyped=None,
    ) -> None:
        return None

    return registry.entry_for(typed)


def test_text_mode_parses_by_annotation(eks_registry: Registry) -> None:
    entry = typed_entry(eks_registry)
    values = coerce_params(entry, {
        "name": "Kadıköy", "count": "12", "ratio": "1.5", "flag": "yes", "delay": "beat:8 + 1b",
        "target": "#save", "speed": "slow", "mode": "SLOW", "tags": "a, b,c", "limits": '{"rows": 5}',
        "window": '{"width": 390}', "song": "music/song.ogg", "persona": "harman-admin",
        "anything": "kept", "untyped": "kept too",
    }, text=True)
    assert values["name"] == "Kadıköy"
    assert values["count"] == 12
    assert values["ratio"] == 1.5
    assert values["flag"] is True
    assert values["delay"] == t("beat:8") + t("1b")
    assert values["target"] == by.test_id("save")
    assert values["speed"] == "slow"
    assert values["mode"] is Mode.SLOW
    assert values["tags"] == ["a", "b", "c"]
    assert values["limits"] == {"rows": 5}
    assert values["window"] == Window(width=390)
    assert values["song"] == "music/song.ogg"
    assert values["persona"] == "harman-admin"
    assert values["anything"] == "kept"
    assert values["untyped"] == "kept too"


@pytest.mark.parametrize(("text", "expected"), [
    ("true", True), ("1", True), ("YES", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("Off", False),
])
def test_text_mode_booleans(eks_registry: Registry, text: str, expected: bool) -> None:
    values = coerce_params(typed_entry(eks_registry), {"name": "x", "flag": text}, text=True)
    assert values["flag"] is expected


def test_text_mode_literal_non_string_choice(eks_registry: Registry) -> None:
    values = coerce_params(typed_entry(eks_registry), {"name": "x", "speed": "3", "mode": "fast"}, text=True)
    assert values["speed"] == 3
    assert values["mode"] is Mode.FAST


def test_text_mode_empty_list(eks_registry: Registry) -> None:
    values = coerce_params(typed_entry(eks_registry), {"name": "x", "tags": ""}, text=True)
    assert values["tags"] == []


@pytest.mark.parametrize(("field", "text", "message"), [
    ("count", "1.5", "not an integer"),
    ("ratio", "fast", "not a number"),
    ("ratio", "nan", "not a finite number"),
    ("flag", "maybe", "not a boolean"),
    ("delay", "1.5", "bare numbers|unit|time"),
    ("speed", "medium", "not one of"),
    ("mode", "MEDIUM", "not one of"),
    ("limits", "{rows: 5}", "not JSON"),
    ("limits", '{"rows": "many"}', "invalid"),
    ("window", '{"height": 1}', "invalid"),
    ("target", "role=", "parameter 'target'"),
])
def test_text_mode_errors_name_the_parameter(eks_registry: Registry, field: str, text: str, message: str) -> None:
    with pytest.raises(PlanBuildError, match=message) as error:
        coerce_params(typed_entry(eks_registry), {"name": "x", field: text}, text=True)
    assert f"parameter {field!r}" in str(error.value)


def test_text_mode_rejects_non_strings(eks_registry: Registry) -> None:
    with pytest.raises(PlanBuildError, match="text mode takes strings"):
        coerce_params(typed_entry(eks_registry), {"name": "x", "count": 3}, text=True)


def test_object_mode_validates_values(eks_registry: Registry) -> None:
    entry = typed_entry(eks_registry)
    values = coerce_params(entry, {
        "name": "x", "count": 7, "delay": t("2s"), "target": "#save", "window": {"width": 1}, "mode": "slow",
        "limits": {"a": 1}, "anything": object, "song": "clip.mp4",
    })
    assert values["count"] == 7
    assert values["delay"] == t("2s")
    assert values["target"] == by.test_id("save")
    assert values["window"] == Window(width=1)
    assert values["mode"] is Mode.SLOW
    assert values["anything"] is object
    assert values["song"] == "clip.mp4"
    model = Window(width=5)
    assert coerce_params(entry, {"name": "x", "window": model})["window"] == model
    target = by.role("button", name="Kaydet")
    assert coerce_params(entry, {"name": "x", "target": target})["target"] is target


@pytest.mark.parametrize(("field", "value", "message"), [
    ("delay", "1s", "pass t"),
    ("delay", 1.5, "pass t"),
    ("target", 3, "Target or a target string"),
    ("count", "many", "invalid"),
    ("window", {"width": "wide"}, "invalid"),
    ("song", 3, "expects media"),
])
def test_object_mode_errors(eks_registry: Registry, field: str, value: Any, message: str) -> None:
    with pytest.raises(PlanBuildError, match=message):
        coerce_params(typed_entry(eks_registry), {"name": "x", field: value})


def test_defaults_are_filled(eks_registry: Registry) -> None:
    values = coerce_params(typed_entry(eks_registry), {"name": "x"})
    assert values["count"] == 1
    assert values["delay"] == t("1s")
    assert values["speed"] == "fast"
    assert values["tags"] is None
    assert list(values)[:3] == ["name", "count", "ratio"]


def test_missing_parameter_lists_known_names(eks_registry: Registry) -> None:
    with pytest.raises(PlanBuildError, match="needs a value for 'name'") as error:
        coerce_params(typed_entry(eks_registry), {})
    assert "'count'" in str(error.value)
    assert "'untyped'" in str(error.value)


def test_unknown_parameter_lists_known_names(eks_registry: Registry) -> None:
    with pytest.raises(PlanBuildError, match="has no parameter 'nmae'") as error:
        coerce_params(typed_entry(eks_registry), {"nmae": "x"}, text=True)
    assert "known parameters: 'name', 'count'" in str(error.value)


def test_unresolvable_annotations_raise(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def broken(s: eks.Session, value: "UndefinedThing" = None) -> None:  # type: ignore[name-defined]  # noqa: F821
        return None

    # only the coerced parameters need their types; a defaulted one is not evaluated
    assert coerce_params(eks_registry.entry_for(broken), {}) == {"value": None}
    with pytest.raises(PlanBuildError, match="cannot read the type of parameter 'value' of flow 'broken'"):
        coerce_params(eks_registry.entry_for(broken), {"value": "x"})


def test_type_checking_only_annotations_outside_the_parameters_do_not_matter(
    eks_registry: Registry, tmp_path: Path,
) -> None:
    path = tmp_path / "p4_type_checking_flow.py"
    path.write_text(textwrap.dedent('''
        from __future__ import annotations

        from typing import TYPE_CHECKING

        import eks

        if TYPE_CHECKING:
            from eks.ref import Json
            from eks.session import Session as Alias


        @eks.flow(app="web-app", platform="web")
        def tc(s: Alias, count: int = 1) -> Json:
            s.record_op("ui.nav", {"route": f"/items/{count}"})
            return None
    '''), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("p4_type_checking_flow", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert [node.args for node in eks.plan(module.tc).nodes] == [{"route": "/items/1"}]
    plan = eks.plan(module.tc, count=4)
    assert plan.params == {"count": 4}
    assert [node.args for node in plan.nodes] == [{"route": "/items/4"}]
    assert coerce_params(eks_registry.entry_for(module.tc), {"count": "7"}, text=True) == {"count": 7}


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


def test_enum_parameters_are_planned_as_their_values(eks_registry: Registry) -> None:
    received: list[Any] = []

    @eks.flow(app="web-app", platform="web")
    def colored(s: eks.Session, c: Color = Color.RED, many: list[Color] | None = None) -> None:
        received.append((c, many))

    # object mode: the flow receives the member, the plan stores its value
    plan = eks.plan(colored, c=Color.BLUE)
    assert received[-1] == (Color.BLUE, None)
    assert plan.params == {"c": "blue", "many": None}

    # text mode, as the CLI --set path does it
    entry = eks_registry.entry_for(colored)
    coerced = coerce_params(entry, {"c": "BLUE", "many": '["red", "blue"]'}, text=True)
    assert coerced == {"c": Color.BLUE, "many": [Color.RED, Color.BLUE]}
    plan = trace(entry, params=coerced)
    assert received[-1] == (Color.BLUE, [Color.RED, Color.BLUE])
    assert plan.params == {"c": "blue", "many": ["red", "blue"]}

    # the default alone
    assert eks.plan(colored).params == {"c": "red", "many": None}


def test_text_mode_applies_annotated_constraints(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def constrained(
        s: eks.Session,
        n: Annotated[int, Field(ge=1)] = 1,
        name: Annotated[str, Field(min_length=2)] = "ab",
        ratio: Annotated[float, Field(lt=1)] | None = None,
        tags: Annotated[list[str], Field(max_length=2)] | None = None,
    ) -> None:
        return None

    entry = eks_registry.entry_for(constrained)
    assert coerce_params(entry, {"n": "3", "name": "xyz", "ratio": "0.5", "tags": "a, b"}, text=True) == {
        "n": 3, "name": "xyz", "ratio": 0.5, "tags": ["a", "b"],
    }
    for field, text in (("n", "0"), ("name", "x"), ("ratio", "2"), ("tags", "a,b,c")):
        with pytest.raises(PlanBuildError, match=f"parameter '{field}'.*is invalid"):
            coerce_params(entry, {field: text}, text=True)
        # object mode rejects the same values
        with pytest.raises(PlanBuildError, match=f"parameter '{field}'.*is invalid"):
            coerce_params(entry, {field: {"n": 0, "name": "x", "ratio": 2.0, "tags": ["a", "b", "c"]}[field]})


def register_play_op() -> None:
    @eks.op("demo", name="play", out="json")
    class Play(eks.Args):
        clip: RMedia

    assert Play.__name__ == "Play"


def test_media_parameter_string_becomes_a_media_source_node() -> None:
    register_play_op()
    received: list[Any] = []

    @eks.flow(app="web-app", platform="web")
    def media(s: eks.Session, song: eks.Media, intro: eks.Media = "artifact:VID123") -> None:  # type: ignore[assignment]
        received.extend([song, intro])
        s.ctx.record("demo.play", {"clip": song})

    plan = eks.plan(media, song="music\\song.ogg")
    song_node, intro_node, play = plan.nodes
    assert song_node.op == "media.source"
    assert song_node.args == {"uri": "file:music/song.ogg"}
    assert song_node.id == derive_id("n", "flow:media/param:song#0")
    assert intro_node.args == {"uri": "artifact:VID123"}
    assert intro_node.id == derive_id("n", "flow:media/param:intro#0")
    assert song_node.after == []
    assert play.after == [song_node.id]
    assert play.args == {"clip": {"$ref": song_node.id, "path": []}}
    song, intro = received
    assert isinstance(song, Ref)
    assert node_of(song) == song_node.id
    assert type_of(song) == "media"
    assert node_of(intro) == intro_node.id
    # the plan keeps the parameter values as given
    assert plan.params == {"song": "music\\song.ogg", "intro": "artifact:VID123"}


def test_media_parameter_ref_is_passed_through(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def through(s: eks.Session, song: eks.Media) -> None:
        return None

    ref: Ref[Any] = Ref("n0000000000", "media")
    assert coerce_params(eks_registry.entry_for(through), {"song": ref})["song"] is ref


def test_empty_media_string_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def empty(s: eks.Session, song: eks.Media) -> None:
        return None

    with pytest.raises(PlanBuildError, match="parameter 'song'"):
        eks.plan(empty, song="  ")


def test_plan_coerces_params_in_object_mode() -> None:
    seen: dict[str, Any] = {}

    @eks.flow(app="web-app", platform="web")
    def coerced(s: eks.Session, count: int, at: Time = DEFAULT_DELAY, target: Target = DEFAULT_TARGET) -> None:
        seen.update(count=count, at=at, target=target)

    plan = eks.plan(coerced, count=3, target="#save")
    assert seen == {"count": 3, "at": t("1s"), "target": by.test_id("save")}
    assert plan.params == {"count": 3, "at": "1s", "target": {"by": "test_id", "args": {"id": "save"}}}
    with pytest.raises(PlanBuildError, match="has no parameter 'other'"):
        eks.plan(coerced, count=3, other=1)


def test_parse_sets() -> None:
    assert parse_sets(["a=1", "b=x=y", " c =", "d= spaced "]) == {"a": "1", "b": "x=y", "c": "", "d": " spaced "}
    with pytest.raises(PlanBuildError, match="KEY=VALUE"):
        parse_sets(["novalue"])
    with pytest.raises(PlanBuildError, match="KEY=VALUE"):
        parse_sets(["=1"])
    with pytest.raises(PlanBuildError, match="twice"):
        parse_sets(["a=1", "a=2"])


@dataclasses.dataclass
class Point:
    x: int
    y: int = 0


DEFAULT_POINT = Point(x=0)


def model_entry(registry: Registry) -> EntrySpec:
    # Bare models, Annotated models and dataclasses reject a TypeAdapter config in pydantic 2.14.
    @eks.flow(app="web-app", platform="web")
    def models(
        s: eks.Session,
        window: Window,
        framed: Annotated[Window, Field(description="the frame")],
        point: Point = DEFAULT_POINT,
    ) -> None:
        return None

    return registry.entry_for(models)


def test_bare_model_parameter_text_mode(eks_registry: Registry) -> None:
    entry = model_entry(eks_registry)
    values = coerce_params(entry, {
        "window": '{"width": 3}', "framed": '{"width": 4, "height": 5}', "point": '{"x": 2}',
    }, text=True)
    assert values["window"] == Window(width=3)
    assert values["framed"] == Window(width=4, height=5)
    assert values["point"] == Point(x=2)


def test_bare_model_parameter_object_mode(eks_registry: Registry) -> None:
    entry = model_entry(eks_registry)
    values = coerce_params(entry, {"window": {"width": 3}, "framed": Window(width=6), "point": {"x": 1, "y": 2}})
    assert values["window"] == Window(width=3)
    assert values["framed"] == Window(width=6)
    assert values["point"] == Point(x=1, y=2)
    with pytest.raises(PlanBuildError, match="invalid"):
        coerce_params(entry, {"window": {"width": "wide"}, "framed": {"width": 1}})
