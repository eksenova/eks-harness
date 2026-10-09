from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

from eks_harness.score.dsl import Score
from eks_harness.score.model import Score as ScoreIR


class ScoreLoadError(ValueError):
    pass


def load_score_module(path: Path) -> Any:
    namespace = runpy.run_path(str(path), run_name="__score__")
    candidate = namespace.get("score")
    if candidate is None and callable(namespace.get("build")):
        candidate = namespace["build"]()
    if isinstance(candidate, Score | ScoreIR):
        return candidate
    raise ScoreLoadError(f"{path} must define `score` (an eks_harness.score.Score) or a build() function returning one")


def load_any(path: Path) -> ScoreIR:
    if path.suffix == ".py":
        loaded = load_score_module(path)
        return loaded.build() if isinstance(loaded, Score) else loaded
    return ScoreIR.load(path)
