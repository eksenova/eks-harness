from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eks_harness.score import Event, Score, ScoreIR, TimeRefError, at, beats, cue, parse_time_ref, plan
from eks_harness.score.timeref import TimeContext


def ctx(**kw) -> TimeContext:
    base = dict(fps=30.0, beats=[0.5 * i for i in range(64)], downbeats=[2.0 * i for i in range(16)],
                markers={"drop": 7.25}, duration=30.0)
    base.update(kw)
    return TimeContext(**base)


@pytest.mark.parametrize(("text", "seconds"), [
    ("12.5s", 12.5), ("300ms", 0.3), ("4", 4.0), ("f:45", 1.5), ("beat:3", 1.5), ("beat:2.5", 1.25),
    ("downbeat:2", 4.0), ("bar:3", 6.0), ("cue:drop", 7.25), ("end", 30.0), ("beat:4+0.25s", 2.25),
    ("cue:drop-3f", 7.15), ("beat:2+1b", 1.5), ("downbeat:1 + 250ms - 1f", 2.25 - 1 / 30),
])
def test_time_refs_resolve(text: str, seconds: float) -> None:
    assert ctx().resolve(text) == pytest.approx(seconds)


def test_time_ref_errors() -> None:
    for bad in ("soon", "beat:", "f:1.5", "cue:", "3s+x"):
        with pytest.raises(TimeRefError):
            parse_time_ref(bad)
    with pytest.raises(TimeRefError, match="unknown cue"):
        ctx().resolve("cue:nope")
    with pytest.raises(TimeRefError, match="no beat grid"):
        ctx(beats=[], downbeats=[]).resolve("beat:1")
    assert ctx().resolve("beat:70") == pytest.approx(35.0)


def build_demo() -> Score:
    s = Score(fps=30, song="music/summer.ogg", window=(147.89, 175.55))
    app = s.device("ios", flow="flows/kyc.py")
    card = s.web("scenes/card/index.html")
    phone = s.blender("scenes/phone.py", textures={"screen": app, "card": card})
    s.edit.layer(phone).layer("scenes/titles.py").layer("footage/intro.mp4")
    s.on(beats("downbeat")[3], app.press("#start-nfc"), id="start")
    s.on(app.event("result-shown"), card.emit("flip", side="back"), id="flip")
    s.on(cue("match") + 0.1, phone.emit("impact"), id="impact")
    s.on(beats()[0:8:2], card.set("pulse", 1.0, ease=0.1), id="pulse")
    s.cue("match", 12.0)
    return s


def test_dsl_builds_valid_ir_and_roundtrips(tmp_path: Path) -> None:
    score = build_demo().build()
    assert [t.id for t in score.tracks] == ["music", "ios", "web", "blender", "edit", "blender2"]
    assert score.track("blender").textures == {"screen": "ios", "card": "web"}
    edit = score.track("edit")
    assert [layer.source for layer in edit.layers] == ["blender", "blender2", "file:footage/intro.mp4"]
    assert edit.audio == ["music"]
    assert score.clock.duration is None and score.clock.window == (147.89, 175.55)
    path = score.save(tmp_path / "score.json")
    again = ScoreIR.load(path)
    assert again == score
    assert len([r for r in again.rules if r.id and r.id.startswith("pulse")]) == 4


def test_ir_validation_rejects_broken_links() -> None:
    with pytest.raises(ValidationError, match="unknown track"):
        ScoreIR.model_validate({"tracks": [{"kind": "web", "id": "w", "entry": "a.html"}],
                                "rules": [{"when": {"at": "1s"}, "do": [{"target": "nope", "verb": "emit"}]}]})
    with pytest.raises(ValidationError, match="exactly one"):
        ScoreIR.model_validate({"rules": [{"when": {"at": "1s", "event": "x"}, "do": []}]})
    with pytest.raises(ValidationError, match="duplicate"):
        ScoreIR.model_validate({"tracks": [{"kind": "web", "id": "w", "entry": "a"},
                                           {"kind": "web", "id": "w", "entry": "b"}]})
    with pytest.raises(ValidationError, match="script or"):
        ScoreIR.model_validate({"tracks": [{"kind": "blender", "id": "b"}]})
    custom = ScoreIR.model_validate({"tracks": [{"kind": "lottie", "id": "l", "file": "a.json"}]})
    assert custom.track("l").kind == "lottie" and custom.track("l").file == "a.json"


def test_plan_schedules_static_event_and_chained_rules() -> None:
    s = build_demo()
    analyzed = {"beats": [0.5 * i for i in range(56)], "downbeats": [2.0 * i for i in range(14)]}
    calls = []

    def analyzer(path, window):
        calls.append((path, window))
        return analyzed

    first = plan(s.build(), base=Path("/proj"), analyzer=analyzer)
    assert calls == [(Path("/proj/music/summer.ogg"), (147.89, 175.55))]
    assert first.context.duration == pytest.approx(27.66)
    by_rule = {a.rule: a for a in first.actions}
    assert by_rule["start"].time == pytest.approx(6.0) and by_rule["start"].frame == 180
    assert by_rule["impact"].frame == round(12.1 * 30)
    assert "flip" not in by_rule
    assert [a.time for a in first.actions if a.rule.startswith("pulse")] == [0.0, 1.0, 2.0, 3.0]
    observed = [Event(9.013, "ios", "result-shown", {"route": "/result"})]
    second = plan(s.build(), observed=observed, base=Path("/proj"), analyzer=analyzer)
    flip = next(a for a in second.actions if a.rule == "flip")
    assert flip.target == "web" and flip.frame == 270 and flip.cause.source == "ios"
    assert flip.action.args == {"name": "flip", "data": {"side": "back"}}
    inputs = second.inputs("web")
    assert any(i["verb"] == "emit" and i["localFrame"] == 270 for i in inputs)
    dumped = second.as_dict()
    assert dumped["fps"] == 30 and dumped["events"][0]["name"] == "beat"


def test_plan_clock_event_rules_and_bpm_grid() -> None:
    s = Score(fps=24, bpm=120, duration=4.0)
    web = s.web("a.html")
    s.on(beats("downbeat").event(), web.emit("bar"), id="bars")
    s.on(web.event("done").first() + 0.5, web.emit("again"), id="after")
    result = plan(s.build(), observed=[Event(1.0, "web", "done"), Event(2.0, "web", "done")])
    assert [a.time for a in result.actions if a.rule == "bars"] == [0.0, 2.0, 4.0]
    assert [a.time for a in result.actions if a.rule == "after"] == [1.5]
    late = Score(fps=30, duration=2.0)
    w = late.web("a.html")
    late.on(at(5), w.emit("x"))
    assert "outside the score" in plan(late.build()).warnings[0]
