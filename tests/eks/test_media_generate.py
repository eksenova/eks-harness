from __future__ import annotations

import pytest
from pydantic import ValidationError

import eks
from eks import t
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def test_speech_stems_and_scenes_feed_an_edit(eks_registry: Registry) -> None:
    # the old video IR's TTSGenerated, SeparatedStem, Solid and GeneratedCard sources, on the new model
    @eks.scene(kind="gfx", size=(1080, 1920), duration=t("2s"))
    def card(sc: eks.GfxScene, title: str = "EksenGOLD") -> None:
        sc.rect(1080.0, 1920.0, fill="#0b1628")
        sc.text(title, size=96.0)

    @eks.edit(fps=30, size=(1080, 1920), seed=3)
    def promo(e: eks.Edit) -> None:
        voice = eks.media.tts("Kontör işlemleri artık tek ekranda", voice="tr_female_1", language="tr-TR")
        vocals = eks.media.stems(e.media("music/song.ogg"), "vocals", "other", model="htdemucs_ft")
        main = e.track("main")
        main.clip(e.gfx(card, title="Kuyumcular için"), start=t("0s"))
        main.clip(e.gfx("card"), start=t("2s"))
        e.track("voice").clip(voice, start=t("0s"))
        e.audio.track(vocals, gain=-6)

    plan = eks.plan(promo)
    (speech,) = nodes_of(plan, "media.tts")
    assert speech.args == {"text": "Kontör işlemleri artık tek ekranda", "voice": "tr_female_1",
                           "backend": "kokoro", "language": "tr-TR", "speed": 1.0}
    assert speech.out == "media"
    (stems,) = nodes_of(plan, "media.stems")
    assert stems.args["stems"] == ["vocals", "other"]
    assert stems.args["model"] == "htdemucs_ft"
    assert len(nodes_of(plan, "gfx.render")) == 2
    (render,) = nodes_of(plan, "edit.render")
    assert speech.id in render.after
    assert stems.id in nodes_of(plan, "audio.mix")[0].after
    assert eks.validate(plan).issues == ()


def test_speech_and_stems_arguments_are_checked(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def bad_speed(e: eks.Edit) -> None:
        e.track("v").clip(eks.media.tts("Merhaba", speed=0.0))

    with pytest.raises(ValidationError):
        eks.plan(bad_speed)

    @eks.edit(fps=30, size=(1080, 1920))
    def no_stems(e: eks.Edit) -> None:
        eks.media.stems(e.media("music/song.ogg"))

    with pytest.raises(TypeError, match="at least one stem"):
        eks.plan(no_stems)

    @eks.edit(fps=30, size=(1080, 1920))
    def repeated(e: eks.Edit) -> None:
        eks.media.stems(e.media("music/song.ogg"), "vocals", "vocals")

    with pytest.raises(ValidationError, match="repeat"):
        eks.plan(repeated)
