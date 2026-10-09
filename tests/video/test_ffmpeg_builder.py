from __future__ import annotations

from eks_harness.video.render.ffmpeg_builder import (
    FilterChain,
    FilterGraph,
    FilterNode,
    crop,
    eq,
    overlay,
    scale,
    setpts,
    xfade,
)


def test_scale_eq_chain_serialization() -> None:
    chain = FilterChain().add(scale(1080, 1920)).add(eq(brightness=0.1))
    assert chain.serialize() == "scale=1080:1920,eq=brightness=0.1"


def test_node_with_input_output_pads() -> None:
    node = FilterNode(
        name="overlay",
        params={"x": 0, "y": 100},
        inputs=["base", "ovr"],
        outputs=["out"],
    )
    assert node.serialize() == "[base][ovr]overlay=x=0:y=100[out]"


def test_filter_graph_joins_chains_with_semicolons() -> None:
    g = FilterGraph()
    g.add_chain("a", FilterChain().add(scale(1080, 1920)))
    g.add_chain("b", FilterChain().add(eq(brightness=0.2)))
    assert g.serialize() == "scale=1080:1920;eq=brightness=0.2"


def test_crop_setpts_overlay_serialize() -> None:
    chain = FilterChain().add(crop(640, 360, 100, 50)).add(setpts("PTS-STARTPTS")).add(overlay(0, 0))
    assert chain.serialize() == "crop=640:360:100:50,setpts=PTS-STARTPTS,overlay=x=0:y=0"


def test_xfade_serialize() -> None:
    node = xfade(transition="fade", duration=0.5, offset=1.5)
    assert node.serialize() == "xfade=transition=fade:duration=0.5:offset=1.5"
