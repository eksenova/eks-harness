from __future__ import annotations

from collections.abc import Mapping

from eks_harness.ir.registry import default_registry
from eks_harness.ir.target import (
    By,
    Target,
    TargetError,
    TargetKind,
    by,
    parse_target,
    use_default_kinds,
)


def _active_target_kinds() -> Mapping[str, TargetKind]:
    return default_registry().target_kinds()


use_default_kinds(_active_target_kinds)

__all__ = ["By", "Target", "TargetError", "by", "parse_target"]
