from __future__ import annotations

import pytest

from eks_harness.capture.trim import (
    TrimConfig,
    expected_duration,
    filter_graph,
    output_offset,
    plan,
)


def test_no_events_keeps_real_time() -> None:
    segments = plan(10.0, [])
    assert [(seg.start, seg.end, seg.speed) for seg in segments] == [(0.0, 10.0, 1.0)]
    assert expected_duration(segments) == 10.0


def test_interaction_window_is_real_time() -> None:
    segments = plan(16.0, [4.0, 11.0])
    keeps = [seg for seg in segments if seg.speed == 1.0]
    assert (keeps[0].start, keeps[0].end) == (2.8, 6.0)
    assert (keeps[1].start, keeps[1].end) == (9.8, 13.0)
    assert output_offset(segments, 4.5) == pytest.approx(3.7)


def test_idle_is_sped_up_at_most_4x() -> None:
    segments = plan(16.0, [4.0, 11.0])
    for seg in segments:
        assert 1.0 <= seg.speed <= 4.0
    assert "setpts=N/30" not in filter_graph(segments)
    wide = plan(30.0, [2.0, 28.0])
    assert max(seg.speed for seg in wide) == 4.0
    assert "setpts=PTS/4" in filter_graph(wide)


def test_short_gap_is_absorbed_so_no_screen_is_under_2s() -> None:
    segments = plan(14.0, [5.0, 10.0])
    keeps = [seg for seg in segments if seg.speed == 1.0]
    assert (keeps[0].start, keeps[0].end) == (3.8, 12.0)
    for seg in segments:
        assert seg.output_seconds >= 2.0
    assert expected_duration(segments) >= 2.0


def test_three_second_still_survives() -> None:
    assert expected_duration(plan(3.0, [])) == 3.0
    assert expected_duration(plan(3.0, [1.5])) >= 2.0


def test_out_of_range_events_are_ignored() -> None:
    assert plan(5.0, [-2.0, 99.0]) == plan(5.0, [])


def test_zero_duration_plans_nothing() -> None:
    assert plan(0.0, [0.0]) == []


def test_filter_graph_concats_keep_and_sped_segments() -> None:
    graph = filter_graph(plan(16.0, [4.0, 11.0]))
    assert "concat=n=5:v=1:a=0" in graph
    assert graph.endswith("[outv]")


def test_single_segment_filter_has_no_concat() -> None:
    graph = filter_graph(plan(10.0, []))
    assert "concat" not in graph
    assert graph.endswith("[outv]")


def test_every_threshold_is_configurable() -> None:
    config = TrimConfig(keep_before_sec=0.5, keep_after_sec=0.5,
                        max_speedup=2.0, min_screen_sec=1.0)
    segments = plan(10.0, [5.0], config)
    keeps = [seg for seg in segments if seg.speed == 1.0]
    assert (keeps[0].start, keeps[0].end) == (4.5, 5.5)
    assert max(seg.speed for seg in segments) <= 2.0


def test_config_rejects_bad_thresholds() -> None:
    with pytest.raises(ValueError):
        TrimConfig(max_speedup=0.5)
    with pytest.raises(ValueError):
        TrimConfig(keep_before_sec=-1.0)


def test_config_reads_env_overrides() -> None:
    config = TrimConfig.from_env({"EKS_TRIM_KEEP_BEFORE": "0.5", "EKS_TRIM_MAX_SPEEDUP": "2"})
    assert config.keep_before_sec == 0.5
    assert config.max_speedup == 2.0
    assert TrimConfig.from_env({}) == TrimConfig()
