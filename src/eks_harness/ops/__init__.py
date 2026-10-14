from __future__ import annotations

from typing import TYPE_CHECKING

from eks_harness.engine.auth import PersonaAuthRegistry
from eks_harness.engine.executors import ExecutorRegistry

if TYPE_CHECKING:
    from eks_harness.data.engine import SeedEngine

API_EXECUTOR = "eks.api"
SESSION_EXECUTOR = "eks.session"
EXPECT_EXECUTOR = "eks.expect"
WORKER_EXECUTOR = "eks.worker"
BUILTIN_EXECUTOR_VERSION = "1"
API_TIMEOUT_SECONDS = 30.0


def register_builtin_executors(registry: ExecutorRegistry, auth: PersonaAuthRegistry, *,
                               api_timeout: float = API_TIMEOUT_SECONDS,
                               seeds: SeedEngine | None = None) -> ExecutorRegistry:
    from eks_harness.ops.api import ApiExecutor, BackendInfoExecutor
    from eks_harness.ops.expect import ExpectValueExecutor
    from eks_harness.ops.session import SessionAsExecutor
    from eks_harness.ops.worker import WorkerExecutor

    worker = WorkerExecutor()
    for spec in (
        ApiExecutor(auth, timeout=api_timeout).spec(),
        BackendInfoExecutor().spec(),
        SessionAsExecutor(auth).spec(),
        ExpectValueExecutor().spec(),
        worker.spec(),
        worker.container_spec(),
    ):
        registry.register(spec)
    register_media_executors(registry)
    if seeds is not None:
        from eks_harness.ops.data import DataExecutor

        registry.register(DataExecutor(seeds).spec())
    return registry


def register_media_executors(registry: ExecutorRegistry) -> ExecutorRegistry:
    from eks_harness.ops.audio import AudioMixExecutor
    from eks_harness.ops.check import MediaCheckExecutor
    from eks_harness.ops.edit import EditRenderExecutor
    from eks_harness.ops.gfx import GfxExecutor
    from eks_harness.ops.media import media_executor_specs
    from eks_harness.ops.overlay import AnnotateExecutor

    for spec in (*media_executor_specs(), AudioMixExecutor().spec(), GfxExecutor().spec(), EditRenderExecutor().spec(),
                 MediaCheckExecutor().spec(), AnnotateExecutor().spec()):
        registry.register(spec)
    return registry


__all__ = [
    "API_EXECUTOR",
    "API_TIMEOUT_SECONDS",
    "BUILTIN_EXECUTOR_VERSION",
    "EXPECT_EXECUTOR",
    "SESSION_EXECUTOR",
    "WORKER_EXECUTOR",
    "register_builtin_executors",
    "register_media_executors",
]
