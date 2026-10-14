from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from pydantic import ValidationError

from eks_harness.ir.plan import CheckUse
from eks_harness.ir.presets import Viewport

DEFAULTS_TABLE = "defaults"
CHECKS_TABLE = "checks"
PLATFORM_TABLES: Mapping[str, str] = {"web": "web", "ios": "mobile", "android": "mobile"}
SCALAR_DEFAULTS = ("timeout", "pace", "viewport")
PLATFORM_SCALARS = ("timezone", "locale")
ALTERNATION = "|"


class ProfileDefaultsError(ValueError):
    pass


def _table(tables: Mapping[str, Any], name: str, app: str) -> Mapping[str, Any]:
    value = tables.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProfileDefaultsError(f"app {app!r}: [{name}] in app.toml must be a table, got {type(value).__name__}")
    return cast("Mapping[str, Any]", value)


def _text(value: object, where: str, app: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileDefaultsError(f"app {app!r}: {where} in app.toml must be a non-empty string, got {value!r}")
    return value


def _words(value: object, where: str, app: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(ALTERNATION) if part.strip())
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        items = cast("Sequence[object]", value)
        return tuple(_text(item, f"{where}[{index}]", app) for index, item in enumerate(items))
    raise ProfileDefaultsError(
        f"app {app!r}: {where} in app.toml must be a list of strings or one string with '|' between items, "
        f"got {value!r}")


def _check(value: object, where: str, app: str) -> CheckUse:
    if isinstance(value, str):
        return CheckUse(kind=_text(value, where, app))
    if isinstance(value, Mapping):
        try:
            return CheckUse.model_validate(value)
        except ValidationError as error:
            raise ProfileDefaultsError(f"app {app!r}: {where} in app.toml is not a check use: {error}") from error
    raise ProfileDefaultsError(
        f"app {app!r}: {where} in app.toml must be a check kind or a table {{kind = ..., params = {{...}}}}, "
        f"got {value!r}")


def _checks(value: object, where: str, app: str) -> tuple[CheckUse, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ProfileDefaultsError(f"app {app!r}: {where} in app.toml must be a list of checks, got {value!r}")
    items = cast("Sequence[object]", value)
    return tuple(_check(item, f"{where}[{index}]", app) for index, item in enumerate(items))


def _viewports(value: object, where: str, app: str) -> dict[str, Viewport]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProfileDefaultsError(f"app {app!r}: {where} in app.toml must be a table of viewports, got {value!r}")
    found: dict[str, Viewport] = {}
    for name, size in cast("Mapping[str, object]", value).items():
        try:
            found[name] = Viewport.model_validate(size)
        except ValidationError as error:
            raise ProfileDefaultsError(f"app {app!r}: {where}.{name} in app.toml is not a viewport: {error}") from error
    return found


def session_defaults(tables: Mapping[str, Any], platform: str, app: str) -> dict[str, Any]:
    defaults = _table(tables, DEFAULTS_TABLE, app)
    checks = _table(tables, CHECKS_TABLE, app)
    section_name = PLATFORM_TABLES.get(platform)
    section = _table(tables, section_name, app) if section_name is not None else {}
    found: dict[str, Any] = {}
    for name in SCALAR_DEFAULTS:
        if defaults.get(name) is not None:
            found[name] = _text(defaults[name], f"[{DEFAULTS_TABLE}] {name}", app)
    for name in PLATFORM_SCALARS:
        if section.get(name) is not None:
            found[name] = _text(section[name], f"[{section_name}] {name}", app)
    found["noise"] = tuple(dict.fromkeys((
        *_words(checks.get("noise"), f"[{CHECKS_TABLE}] noise", app),
        *_words(defaults.get("noise"), f"[{DEFAULTS_TABLE}] noise", app),
    )))
    found["network_ignore"] = _words(section.get("network_ignore"), f"[{section_name}] network_ignore", app)
    found["viewports"] = _viewports(section.get("viewports"), f"[{section_name}.viewports]", app)
    found["checks"] = _checks(defaults.get("checks"), f"[{DEFAULTS_TABLE}] checks", app)
    return found


__all__ = ["ProfileDefaultsError", "session_defaults"]
