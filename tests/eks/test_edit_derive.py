from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

import eks
from eks import t
from eks.discovery import load_file
from eks.tracing import TraceError
from eks_harness.engine.validator import validate
from eks_harness.ir.ops.overlay import Caption, Title
from eks_harness.ir.plan import Node, Plan

DERIVED_EDIT = '''\
import eks
from eks import t
from eks_harness.ir.ops.overlay import Caption, Title


@eks.edit(fps=30, size=(1080, 1920), seed=3)
def promo(e: eks.Edit):
    e.audio.track("music/song.ogg").beats("beat", "downbeat")
    e.audio.master(loudness=-14.0, true_peak=-1.0)
    v = e.track("main")
    v.clip("clips/intro.mp4", start=t("0s"))
    v.clip("clips/demo.mp4", start=t("beat:8"))
    v.clip("clips/detail.mp4", start=t("beat:16")).transition_in(eks.tx.crossfade(t("8f")))
    v.clip("clips/outro.mp4", start=t("bar:6"))
    e.track("broll", z=4).clip("clips/hands.mp4", start=t("1s"))
    e.track("text", z=5).overlay(
        Caption(text="Kontör işlemleri", window=t.span(t("1s"), t("3s"))),
        Title(text="EksenGOLD", window=t.span(t("beat:0"), t("beat:5"))),
        Caption(text="Sonra", window=t.span(t("2s"), t("end"))),
        Caption(text="Hep"),
    )
    e.expect.cut(at=t("beat:8"), label="drop")
    e.expect.derive()
'''


def plan_source(path: Path, source: str) -> Plan:
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    entries = load_file(path)
    assert len(entries) == 1
    return eks.plan(entries[0].fn)


def checks(plan: Plan) -> list[Node]:
    return [node for node, _ in plan.walk() if node.op == "check.media"]


def render_of(plan: Plan) -> Node:
    return next(node for node, _ in plan.walk() if node.op == "edit.render")


@pytest.fixture
def derived(tmp_path: Path) -> Plan:
    return plan_source(tmp_path / "promo.py", DERIVED_EDIT)


def test_derived_checks_follow_the_hand_written_ones(derived: Plan) -> None:
    found = checks(derived)
    assert [node.label for node in found] == [
        "drop", "derived:cut:0", "derived:cut:1", "derived:text:0", "derived:text:1", "derived:beats:0",
        "derived:loudness:0"]
    render: dict[str, Any] = {"$ref": render_of(derived).id, "path": []}
    assert all(node.args["media"] == render for node in found)


def test_derived_cuts_are_the_hard_cuts_of_the_lowest_track(derived: Plan) -> None:
    cuts = [node.args for node in checks(derived) if str(node.label).startswith("derived:cut:")]
    assert cuts == [
        {"media": cuts[0]["media"], "kind": "cut", "params": {"threshold": 0.1}, "at": "beat:8",
         "label": "derived:cut:0", "soft": False},
        {"media": cuts[0]["media"], "kind": "cut", "params": {"threshold": 0.1}, "at": "bar:6",
         "label": "derived:cut:1", "soft": False},
    ]


def test_derived_text_checks_sit_in_the_middle_of_their_window(derived: Plan) -> None:
    texts: list[Any] = [node.args for node in checks(derived) if node.args["kind"] == "text"]
    assert [(args["params"]["text"], args["at"], args["soft"]) for args in texts] == [
        ("Kontör işlemleri", "2s", True),
        ("EksenGOLD", "beat:2.5", True),
    ]
    assert texts[0]["params"]["engine"] == "auto"


def test_derived_beats_cover_the_cut_times(derived: Plan) -> None:
    (beats,) = [node.args for node in checks(derived) if node.args["kind"] == "beats"]
    assert beats["params"] == {"stream": "beat", "min_ratio": 0.8}
    assert beats["at"] == {"start": "beat:8", "end": "bar:6"}
    assert beats["soft"] is False


def test_derived_loudness_comes_from_the_master(derived: Plan) -> None:
    (loudness,) = [node.args for node in checks(derived) if node.args["kind"] == "loudness"]
    assert loudness["params"] == {"lufs": -14.0, "tolerance": 1.0, "true_peak": -1.0}
    assert "at" not in loudness


def test_derived_plan_validates(derived: Plan) -> None:
    report = validate(derived)
    assert report.ok, report.text()


def test_derived_plan_is_deterministic(tmp_path: Path) -> None:
    first = plan_source(tmp_path / "promo.py", DERIVED_EDIT)
    second = plan_source(tmp_path / "promo.py", DERIVED_EDIT)
    assert [node.hash for node in checks(first)] == [node.hash for node in checks(second)]


def test_derive_switches_kinds_off() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def partial(e: eks.Edit) -> None:
        e.audio.track("music/song.ogg").beats("beat")
        e.audio.master(loudness=-16.0)
        main = e.track("main")
        main.clip("clips/a.mp4")
        main.clip("clips/b.mp4", start=t("beat:4"))
        e.track("text").overlay(Caption(text="Merhaba", window=t.span(t("0s"), t("1s"))))
        e.expect.derive(captions=False, loudness=False)

    labels = [node.label for node in checks(eks.plan(partial))]
    assert labels == ["derived:cut:0", "derived:beats:0"]


def test_derive_without_beat_anchors_or_master() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def plain(e: eks.Edit) -> None:
        e.audio.track("music/song.ogg")
        main = e.track("main")
        main.clip("clips/a.mp4")
        main.clip("clips/b.mp4", start=t("2s"))
        main.clip("clips/b.mp4", start=t("4s"))
        e.track("titles").overlay(Title(text="Son", window=t.span(t("f:30"), t("f:45"))))
        e.expect.derive()

    found = checks(eks.plan(plain))
    assert [node.label for node in found] == ["derived:cut:0", "derived:cut:1", "derived:text:0"]
    assert found[2].args["at"] == "f:37+0.5f"


def test_a_window_without_one_middle_time_is_checked_over_the_window() -> None:
    @eks.edit(fps=30, size=(1080, 1920), duration=t("6s"))
    def bounded(e: eks.Edit) -> None:
        e.track("main").clip("clips/a.mp4")
        e.track("text").overlay(Caption(text="Sonra", window=t.span(t("2s"), t("end"))))
        e.expect.derive()

    plan = eks.plan(bounded)
    (text,) = checks(plan)
    assert text.args["at"] == {"start": "2s", "end": "end"}
    assert validate(plan).ok


def test_continuous_split_is_not_a_cut() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def split(e: eks.Edit) -> None:
        clip = e.media("clips/long.mp4")
        main = e.track("main")
        main.clip(clip, start=t("0s"), src=t.span(t("0s"), t("2s")))
        main.clip(clip, start=t("2s"), src=t.span(t("2s"), t("4s")))
        main.clip(clip, start=t("4s"), src=t.span(t("8s"), t("9s")))
        e.expect.derive()

    found = checks(eks.plan(split))
    assert [(node.label, node.args["at"]) for node in found] == [("derived:cut:0", "4s")]


def test_derive_is_an_edit_builder_call() -> None:
    holder: dict[str, eks.Edit] = {}

    @eks.edit(fps=30, size=(1080, 1920))
    def late(e: eks.Edit) -> None:
        holder["e"] = e
        e.track("main").clip("clips/a.mp4")
        with pytest.raises(TypeError, match="expects a bool"):
            e.expect.derive(cuts="yes")  # type: ignore[arg-type]

    assert checks(eks.plan(late)) == []
    with pytest.raises(TraceError, match="already finalized"):
        holder["e"].expect.derive()
