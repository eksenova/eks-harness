from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResourceSpec:
    uri: str
    name: str
    title: str
    description: str
    mime_type: str
    read: Callable[..., Any]
    needs_roots: bool = True
    needs_registry: bool = False
