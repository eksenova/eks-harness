from __future__ import annotations

import json
from pathlib import Path

import pytest

from eks_harness.capture.steplog import StepBox, StepEntry, event_times, read_jsonl, write_jsonl


def sample() -> StepEntry:
    return StepEntry(t=1.25, action="click", target={"selector": "#save"},
                     page="desktop", ok=True, ms=320,
                     boxes=[StepBox(x=10.0, y=20.0, w=120.0, h=40.0, scale=1.0)])


def test_entry_round_trip(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "steps.jsonl",
                       [sample(), StepEntry(t=0.5, action="goto", target={"path": "/"},
                                            page="desktop", ok=True)])
    rows = read_jsonl(path)
    assert [entry.t for entry in rows] == [0.5, 1.25]
    assert rows[1].target == {"selector": "#save"}
    assert rows[1].boxes[0].w == 120.0


def test_entry_schema_matches_contract(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "steps.jsonl", [sample()])
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row["t"] == 1.25
    assert row["action"] == "click"
    assert row["target"] == {"selector": "#save"}
    assert row["ok"] is True
    assert row["ms"] == 320
    assert row["boxes"][0]["scale"] == 1.0


def test_error_is_capped_at_300_chars() -> None:
    entry = StepEntry(t=0.0, action="click", ok=False, error="x" * 500)
    assert entry.error is not None and len(entry.error) == 300


def test_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "steps.jsonl"
    path.write_text('{"t": 2.0, "action": "fill"}\n\n{"t": 1.0, "action": "click"}\n',
                    encoding="utf-8")
    assert [entry.t for entry in read_jsonl(path)] == [1.0, 2.0]


def test_bad_rows_fail_loudly(tmp_path: Path) -> None:
    path = tmp_path / "steps.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        read_jsonl(path)
    path.write_text('{"t": -1, "action": "click"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        read_jsonl(path)
    path.write_text('{"t": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        read_jsonl(path)


def test_rejects_empty_action_and_bad_target() -> None:
    with pytest.raises(ValueError):
        StepEntry(t=1.0, action="  ")
    with pytest.raises(ValueError):
        StepEntry(t=1.0, action="click", target=["#x"])  # type: ignore[arg-type]


def test_event_times_keep_recording_order() -> None:
    entries = [StepEntry(t=3.0, action="b"), StepEntry(t=1.0, action="a")]
    assert event_times(entries) == [3.0, 1.0]
