from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eks_harness.daemon.context import AppContext

log = logging.getLogger("eks_harness.hooks")

Hook = Callable[["AppContext"], Any]


@dataclass(frozen=True)
class RegisteredHook:
    fn: Hook
    order: int
    name: str


_startup: list[RegisteredHook] = []
_shutdown: list[RegisteredHook] = []


def _register(target: list[RegisteredHook], fn: Hook, order: int) -> Hook:
    name = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', repr(fn))}"
    target[:] = [h for h in target if h.name != name]
    target.append(RegisteredHook(fn, order, name))
    return fn


def on_startup(fn: Hook | None = None, *, order: int = 100):
    if fn is None:
        return lambda f: _register(_startup, f, order)
    return _register(_startup, fn, order)


def on_shutdown(fn: Hook | None = None, *, order: int = 100):
    if fn is None:
        return lambda f: _register(_shutdown, f, order)
    return _register(_shutdown, fn, order)


def startup_hooks() -> list[RegisteredHook]:
    return sorted(_startup, key=lambda h: (h.order, h.name))


def shutdown_hooks() -> list[RegisteredHook]:
    return sorted(_shutdown, key=lambda h: (h.order, h.name))


async def _call(hook: RegisteredHook, ctx: "AppContext") -> None:
    result = hook.fn(ctx)
    if inspect.isawaitable(result):
        await result


async def run_startup(ctx: "AppContext", extra: list[Hook] | None = None) -> None:
    hooks = startup_hooks() + [RegisteredHook(fn, 1000, repr(fn)) for fn in extra or []]
    for hook in hooks:
        log.debug("startup hook %s", hook.name)
        await _call(hook, ctx)


async def run_shutdown(ctx: "AppContext", extra: list[Hook] | None = None) -> None:
    hooks = [RegisteredHook(fn, -1000, repr(fn)) for fn in extra or []] + shutdown_hooks()
    for hook in hooks:
        try:
            await _call(hook, ctx)
        except Exception:
            log.exception("shutdown hook %s failed", hook.name)
