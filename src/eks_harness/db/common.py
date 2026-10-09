from __future__ import annotations

import json
import time
from typing import Any, Final


class _Unset:
    _instance: "_Unset | None" = None

    def __new__(cls) -> "_Unset":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Final = _Unset()


def now() -> float:
    return time.time()


def dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text in (None, ""):
        return {} if default is None else default
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {} if default is None else default


def placeholders(count: int) -> str:
    return ",".join("?" * count)


def assignments(fields: dict[str, Any]) -> tuple[str, list[Any]]:
    keys = [k for k, v in fields.items() if v is not UNSET]
    return ", ".join(f"{k} = ?" for k in keys), [fields[k] for k in keys]
