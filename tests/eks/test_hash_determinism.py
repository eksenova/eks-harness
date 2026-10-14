from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

import eks
from eks import t
from eks_harness.ir.hashing import derived_seed, hash_plan, node_seed
from eks_harness.ir.plan import Node, Plan, PlanError
from eks_harness.ir.registry import Registry


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def hashes_of(plan: Plan, op: str) -> list[str]:
    return [node.hash for node in nodes_of(plan, op)]


def edit_with_cue(name: str, cue: str, *, seed: int = 7) -> Callable[..., Any]:
    def body(e: eks.Edit) -> None:
        clip = e.media("artifact:VID123")
        logo = e.cue("logo", t(cue))
        e.track("main").clip(clip, start=logo, src=t.span(t("0s"), t("4s")))
        e.expect.cut(at=logo)

    body.__name__ = name
    body.__qualname__ = name
    return eks.edit(fps=30, size=(1080, 1920), seed=seed)(body)


def test_moving_a_cue_changes_the_render_and_check_hashes(eks_registry: Registry) -> None:
    # the clip start and the cut check resolve against the cue, so a content-addressed cache must see the move
    first = eks.plan(edit_with_cue("cue_at_two", "2s"))
    moved = eks.plan(edit_with_cue("cue_at_five", "5s"))
    assert first.hash != moved.hash
    assert hashes_of(first, "edit.render") != hashes_of(moved, "edit.render")
    assert hashes_of(first, "check.media") != hashes_of(moved, "check.media")
    # the media source does not depend on the cue
    assert hashes_of(first, "media.source") == hashes_of(moved, "media.source")


def test_the_root_seed_is_part_of_every_node_hash(eks_registry: Registry) -> None:
    one = eks.plan(edit_with_cue("seed_seven", "2s", seed=7))
    other = eks.plan(edit_with_cue("seed_eight", "2s", seed=8))
    assert hashes_of(one, "edit.render") != hashes_of(other, "edit.render")
    assert hashes_of(one, "media.source") != hashes_of(other, "media.source")


def test_renaming_an_edit_keeps_hashes_and_derived_seeds(eks_registry: Registry) -> None:
    demo = eks.plan(edit_with_cue("demo", "2s"))
    renamed = eks.plan(edit_with_cue("demo_renamed", "2s"))
    (render,) = nodes_of(demo, "edit.render")
    (other,) = nodes_of(renamed, "edit.render")
    assert render.id != other.id
    assert render.hash == other.hash
    assert demo.hash == renamed.hash
    # per-node seeds derive from the node hash, so they survive renames and change with the content
    assert node_seed(demo.clock.seed, render, "effects.0") == node_seed(renamed.clock.seed, other, "effects.0")
    assert node_seed(7, render) == derived_seed(7, render.hash)


def score_with_song(name: str, *, bpm: float | None = None) -> Callable[..., Any]:
    def body(sc: eks.Score) -> None:
        clip = sc.edit.media("artifact:VID123")
        sc.edit.track("main").clip(clip, start=t("bar:1"), src=t.span(t("0s"), t("4s")))

    body.__name__ = name
    body.__qualname__ = name
    if bpm is not None:
        return eks.score(fps=30, bpm=bpm, window=t.span(t("0s"), t("15s")))(body)
    return eks.score(fps=30, song="music/song.ogg", window=t.span(t("0s"), t("15s")))(body)


def test_renaming_a_score_with_a_song_keeps_the_plan_hash(eks_registry: Registry) -> None:
    ad = eks.plan(score_with_song("ad"))
    renamed = eks.plan(score_with_song("ad_renamed"))
    assert ad.clock.tempo is not None and renamed.clock.tempo is not None
    # the tempo source is a node id, which follows the name; the hash uses that node's Merkle hash instead
    assert ad.clock.tempo.source != renamed.clock.tempo.source
    assert ad.hash == renamed.hash


def test_changing_the_tempo_changes_bar_dependent_hashes(eks_registry: Registry) -> None:
    slow = eks.plan(score_with_song("slow", bpm=100))
    fast = eks.plan(score_with_song("fast", bpm=120))
    assert hashes_of(slow, "edit.render") != hashes_of(fast, "edit.render")
    assert hashes_of(slow, "media.source") == hashes_of(fast, "media.source")


def test_the_song_analysis_feeds_beat_dependent_nodes(eks_registry: Registry) -> None:
    plan = eks.plan(score_with_song("analysed"))
    (markers,) = nodes_of(plan, "media.markers")
    before = hashes_of(plan, "edit.render")
    markers.args = {**markers.args, "sources": [{"kind": "beat_tracker", "name": "song", "bpm": 90.0}]}
    hash_plan(plan)
    assert hashes_of(plan, "edit.render") != before


def test_an_unknown_tempo_source_cannot_be_hashed(eks_registry: Registry) -> None:
    plan = eks.plan(score_with_song("broken"))
    assert plan.clock.tempo is not None
    plan.clock.tempo = plan.clock.tempo.model_copy(update={"source": "n0000000000"})
    with pytest.raises(PlanError, match="n0000000000"):
        hash_plan(plan)


def test_node_seed_needs_a_hashed_node() -> None:
    with pytest.raises(PlanError, match="no hash"):
        node_seed(1, Node(id="n0000000001", op="ui.back"))
