from typing import Any

import pytest

import eks
from eks import media, t
from eks.effects import fx
from eks.ref import Ref, RefUsageError, node_of, path_of, type_of
from eks.tracing import TraceContext, TraceError, current, tracing
from eks_harness.ir.catalog.markers import BeatTracker, STTMarkers
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry, default_registry


def nodes(plan: Plan, op: str | None = None) -> list[Node]:
    return [node for node, _ in plan.walk() if op is None or node.op == op]


class Flag(eks.Args):
    pass


def flag() -> Ref[Any]:
    if not default_registry().has_op("demo.flag"):
        eks.op("demo", name="flag", out="bool", description="A flag known when the plan runs")(Flag)
    return current().record("demo.flag")


class Holder:
    # an Encodable, like a take handle: it encodes as the ref it carries
    def __init__(self, ref: Ref[Any]) -> None:
        self.ref = ref

    def __eks_encode__(self) -> Any:
        return self.ref


class Literal:
    def __eks_encode__(self) -> Any:
        return "not a ref"


def test_source_is_deduplicated_inside_one_trace() -> None:
    seen: dict[str, Any] = {}

    @eks.edit(fps=30, size=(1080, 1920))
    def dedup(e: eks.Edit) -> None:
        seen["a"] = media.source("clips/a.mp4")
        seen["b"] = media.source("file:clips/a.mp4")
        seen["c"] = e.media("clips/a.mp4")
        seen["d"] = media.source("clips\\a.mp4")
        seen["other"] = media.source("artifact:VID1")

    plan = eks.plan(dedup)
    sources = nodes(plan, "media.source")
    assert [node.args for node in sources] == [{"uri": "file:clips/a.mp4"}, {"uri": "artifact:VID1"}]
    assert seen["a"] is seen["b"] is seen["c"] is seen["d"]
    assert node_of(seen["a"]) == sources[0].id
    assert type_of(seen["a"]) == "media"
    assert sources[0].src is not None
    assert sources[0].src.startswith("tests/eks/test_media_sources.py:")


def test_a_different_kind_is_a_different_node() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def kinds(e: eks.Edit) -> None:
        plain = media.source("music/song.ogg")
        audio = media.source("music/song.ogg", kind="audio")
        again = media.source("music/song.ogg", kind="audio")
        assert node_of(plain) != node_of(audio)
        assert again is audio

    plan = eks.plan(kinds)
    assert [node.args for node in nodes(plan, "media.source")] == [
        {"uri": "file:music/song.ogg"}, {"uri": "file:music/song.ogg", "kind": "audio"}]


def test_source_ids_depend_on_the_uri_only(eks_registry: Registry) -> None:
    def ids(order: list[str], other: bool) -> dict[str, str]:
        ctx = TraceContext(kind="edit", name="same", registry=eks_registry)
        with tracing(ctx):
            if other:
                media.source("artifact:OTHER")
            refs = {uri: node_of(media.source(uri)) for uri in order}
        return {uri: refs[uri] for uri in sorted(refs)}

    assert ids(["a.mp4", "b.mp4"], other=False) == ids(["b.mp4", "a.mp4"], other=True)


def test_source_inside_a_body_is_recorded_again_outside(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="edit", name="scopes", registry=eks_registry)
    with tracing(ctx):
        cond = flag()
        with eks.when(cond):
            inner = media.source("a.mp4")
        outer = media.source("a.mp4")
        assert node_of(inner) != node_of(outer)
        assert media.source("a.mp4") is outer


def test_source_inside_a_closed_exporting_container_stays_visible(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="edit", name="exports", registry=eks_registry)
    with tracing(ctx):
        with eks.parallel():
            inner = media.source("a.mp4")
        # ctl.parallel exports its body, so the node recorded inside is reused
        assert media.source("a.mp4") is inner
        with ctx.container("ctl.macro", {"name": "fixture"}), eks.parallel():
            deep = media.source("b.mp4")
        # visible through two closed exporting containers
        assert media.source("b.mp4") is deep
        cond = flag()
        with eks.when(cond):
            with eks.parallel():
                hidden = media.source("c.mp4")
            assert media.source("c.mp4") is hidden
        # the parallel exports, but the when body around it does not
        after = media.source("c.mp4")
        assert node_of(after) != node_of(hidden)
        assert media.source("c.mp4") is after
        plan = ctx.finalize()
    uris = [node.args["uri"] for node in nodes(plan, "media.source")]
    assert uris.count("file:a.mp4") == 1
    assert uris.count("file:b.mp4") == 1
    assert uris.count("file:c.mp4") == 2


def test_source_recorded_outside_is_reused_inside_nested_bodies() -> None:
    seen: dict[str, Any] = {}

    @eks.edit(fps=30, size=(1080, 1920))
    def nested(e: eks.Edit) -> None:
        flagged = flag()
        outer = media.source("clips/a.mp4")
        with eks.when(flagged):
            assert media.source("clips/a.mp4") is outer
            with eks.when(flagged):
                assert media.source("file:clips/a.mp4") is outer
                seen["deep"] = media.source("clips/b.mp4")
            # b was recorded in a closed body, so it is not visible here any more
            seen["again"] = media.source("clips/b.mp4")
            # a later call in the same body reuses the body's own node
            assert media.source("clips/b.mp4") is seen["again"]
        with eks.each([1, 2]):
            assert media.source("clips/a.mp4") is outer
            seen["inside"] = media.source("clips/c.mp4")
            assert media.source("clips/c.mp4") is seen["inside"]
        seen["after"] = media.source("clips/c.mp4")

    plan = eks.plan(nested)
    uris = [node.args["uri"] for node in nodes(plan, "media.source")]
    assert uris.count("file:clips/a.mp4") == 1
    assert uris.count("file:clips/b.mp4") == 2
    assert node_of(seen["again"]) != node_of(seen["deep"])
    assert node_of(seen["after"]) != node_of(seen["inside"])
    # the literal each spliced one copy of c per item, plus the one recorded after it
    assert uris.count("file:clips/c.mp4") == 3


def test_source_rejects_bad_input(eks_registry: Registry) -> None:
    with pytest.raises(TraceError, match="only inside eks.plan"):
        media.source("a.mp4")
    ctx = TraceContext(kind="edit", name="bad", registry=eks_registry)
    with tracing(ctx):
        with pytest.raises(ValueError, match="invalid media reference"):
            media.source("")
        with pytest.raises(TypeError, match="convert paths with str"):
            media.source(42)  # type: ignore[arg-type]
        ref = media.source("a.mp4")
        with pytest.raises(RefUsageError, match="built from a ref"):
            media.source(f"clips/{ref}.mp4")


def test_as_media_cases(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="edit", name="as_media", registry=eks_registry)
    with tracing(ctx):
        clip = media.source("clips/a.mp4")
        assert media.as_media(clip) is clip
        found = media.markers(clip, STTMarkers(name="words"))
        assert media.as_media(found) is found  # type: ignore[arg-type]  # json refs are accepted
        by_text = media.as_media("clips/a.mp4")
        assert by_text is clip
        audio = media.as_media("music/song.ogg", kind="audio")
        assert ctx.node(node_of(audio)).args == {"uri": "file:music/song.ogg", "kind": "audio"}
        held = media.as_media(Holder(clip))
        assert isinstance(held, Ref)
        assert node_of(held) == node_of(clip)
        assert type_of(held) == "media"
        assert path_of(held) == ()
        nested = media.as_media(Holder(found.clips[0]))
        assert type_of(nested) == "json"
        assert path_of(nested) == ("clips", 0)
        with pytest.raises(RefUsageError, match="of type int, not media"):
            media.as_media(Ref("n0000000000", "int"))
        with pytest.raises(RefUsageError, match="of type str, not media"):
            media.as_media(clip.uri)  # type: ignore[arg-type]
        with pytest.raises(RefUsageError, match="of type str, not media"):
            media.as_media(Holder(clip.uri))
        with pytest.raises(RefUsageError, match="not a ref to media"):
            media.as_media(Literal())
        with pytest.raises(RefUsageError, match="expected media"):
            media.as_media(42)  # type: ignore[arg-type]
        with pytest.raises(TraceError, match="not recorded"):
            media.as_media(Holder(Ref("n0000000000", "media")))


def test_markers_node_args(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="edit", name="markers", registry=eks_registry)
    with tracing(ctx):
        song = media.source("music/song.ogg")
        found = media.markers(song, BeatTracker(name="song", streams=("beat",), bpm=120.0),
                              STTMarkers(name="words", language="tr"))
        assert type_of(found) == "json"
        node = ctx.node(node_of(found))
        assert node.op == "media.markers"
        assert node.args == {
            "media": {"$ref": node_of(song), "path": []},
            "sources": [
                {"kind": "beat_tracker", "name": "song", "streams": ["beat"], "bpm": 120.0, "backend": "auto"},
                {"kind": "stt_markers", "name": "words", "backend": "faster-whisper", "language": "tr"},
            ],
        }
        assert node.after == [node_of(song)]
        by_path = media.markers("music/song.ogg", BeatTracker(name="other"))
        assert ctx.node(node_of(by_path)).args["media"] == {"$ref": node_of(song), "path": []}
        with pytest.raises(TypeError, match="at least one marker source"):
            media.markers(song)
        with pytest.raises(TypeError, match="marker models"):
            media.markers(song, "beats")  # type: ignore[arg-type]


def test_media_parameters_of_an_edit_are_media_refs() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def parameterized(e: eks.Edit, clip: eks.Media) -> None:
        e.track("main").clip(clip)

    plan = eks.plan(parameterized, clip="clips/a.mp4")
    source = nodes(plan, "media.source")[0]
    render = nodes(plan, "edit.render")[0]
    timeline: Any = render.args["timeline"]
    assert timeline["tracks"][0]["clips"][0]["media"] == {"$ref": source.id, "path": []}


def test_text_effects_encode_refs_inside_a_trace(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="edit", name="texts", registry=eks_registry)
    with tracing(ctx):
        found = media.markers(media.source("v.mp4"), STTMarkers(name="words"))
        title = fx.text(f"Merhaba {found.name}", start=t("1s"))
        dumped = title.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert dumped["text"] == {"$fmt": ["Merhaba ", {"$ref": node_of(found), "path": ["name"]}]}
        lower = fx.lower_third(found.title)
        assert lower.model_dump(mode="json", by_alias=True)["title"] == {
            "$ref": node_of(found), "path": ["title"]}
