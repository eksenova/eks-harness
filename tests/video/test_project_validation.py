from __future__ import annotations

import json

from eks_harness.video.ir.project import Project


def test_random_seed_defaults_to_zero() -> None:
    project = Project(fps=30, resolution=(640, 480), duration=1.0)
    assert project.random_seed == 0


def test_random_seed_round_trip() -> None:
    project = Project(fps=30, resolution=(640, 480), duration=1.0, random_seed=42)
    payload = project.model_dump(by_alias=True, mode="json")
    rebuilt = Project.model_validate(payload)
    assert rebuilt.random_seed == 42

    rebuilt_from_text = Project.model_validate(json.loads(json.dumps(payload)))
    assert rebuilt_from_text.random_seed == 42


def test_random_seed_accepts_negative_values() -> None:
    project = Project(fps=30, resolution=(640, 480), duration=1.0, random_seed=-12345)
    assert project.random_seed == -12345
    rebuilt = Project.model_validate(project.model_dump(mode="json"))
    assert rebuilt.random_seed == -12345


def test_random_seed_accepts_large_values() -> None:
    big = 2**62
    project = Project(fps=30, resolution=(640, 480), duration=1.0, random_seed=big)
    assert project.random_seed == big
