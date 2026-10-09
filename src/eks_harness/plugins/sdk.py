from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from eks_harness.plugins.host import PluginContext
from eks_harness.plugins.manifest import API_VERSION, Contribution


@dataclass(frozen=True)
class Health:
    url: str
    expect: int | None = None
    grace: float = 120
    timeout: float = 120


@dataclass(frozen=True)
class Ready:
    pattern: str
    timeout: float = 120


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    command: list[str]
    port: str | None = None
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    health: Health | None = None
    ready: Ready | None = None


@dataclass(frozen=True)
class BackendSpec:
    processes: list[ProcessSpec] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    exports: dict[str, str] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    workdir: str | None = None
    inherit_env: bool = True

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"processes": [], "env": self.env, "exports": self.exports, "files": self.files,
                               "inheritEnv": self.inherit_env}
        if self.workdir:
            out["workdir"] = self.workdir
        for process in self.processes:
            item: dict[str, Any] = {"name": process.name, "command": process.command, "env": process.env}
            for key in ("port", "cwd"):
                if getattr(process, key):
                    item[key] = getattr(process, key)
            if process.health:
                item["health"] = {k: v for k, v in vars(process.health).items() if v is not None}
            if process.ready:
                item["ready"] = vars(process.ready)
            out["processes"].append(item)
        return out


@dataclass(frozen=True)
class Persona:
    id: str
    label: str
    username: str = ""
    secret_ref: str = ""
    tenant: str = ""
    roles: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendChoice:
    backend: str
    reason: str
    target: str = ""


@runtime_checkable
class Backend(Protocol):
    def describe(self) -> dict[str, Any]: ...
    def fingerprint(self, context: Mapping[str, Any]) -> str: ...
    def prepare(self, context: Mapping[str, Any]) -> None: ...
    def spec(self, context: Mapping[str, Any]) -> BackendSpec | dict[str, Any]: ...
    def teardown(self, context: Mapping[str, Any], final: bool = False) -> None: ...


@runtime_checkable
class BackendPolicy(Protocol):
    def choose(self, tree: Path, requested: str | None = None) -> BackendChoice: ...


@runtime_checkable
class Seeder(Protocol):
    def scenarios(self) -> list[str]: ...
    def seed(self, context: Mapping[str, Any], scenario: str) -> dict[str, Any]: ...


@runtime_checkable
class CredentialsProvider(Protocol):
    def personas(self, target: str) -> list[Persona]: ...
    def secret(self, persona: Persona) -> str: ...


@runtime_checkable
class LoginProvider(Protocol):
    platforms: tuple[str, ...]

    def login(self, session: Any, persona: Persona, *, target: str, options: Mapping[str, Any]) -> None: ...


@runtime_checkable
class AppProfile(Protocol):
    platform: str

    def describe(self) -> dict[str, Any]: ...
    def build(self, tree: Path, *, variant: str = "dev") -> Path | None: ...
    def install(self, device: Any, artifact: Path | None) -> None: ...
    def launch(self, device: Any, *, url: str | None = None, args: Mapping[str, str] | None = None) -> None: ...


@runtime_checkable
class Fake(Protocol):
    kind: str

    def arm(self, session: Any, payload: Mapping[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class DriverSession(Protocol):
    def act(self, action: str, **params: Any) -> dict[str, Any]: ...
    def observe(self, query: str, **params: Any) -> dict[str, Any]: ...
    def capture(self, kind: str, **params: Any) -> dict[str, Any]: ...
    def events(self) -> AsyncIterator[dict[str, Any]]: ...
    def close(self) -> None: ...


@runtime_checkable
class Driver(Protocol):
    platform: str

    def capabilities(self) -> dict[str, Any]: ...
    def open(self, target: Mapping[str, Any], *, app: Mapping[str, Any] | None = None) -> DriverSession: ...


@runtime_checkable
class McpTools(Protocol):
    def register(self, server: Any, harness: Any) -> None: ...


@runtime_checkable
class CliCommands(Protocol):
    def register(self, subparsers: Any) -> None: ...


@runtime_checkable
class ApiRoutes(Protocol):
    def router(self, context: PluginContext) -> Any: ...


@runtime_checkable
class NodeCapability(Protocol):
    name: str

    def probe(self) -> dict[str, Any] | None: ...


@runtime_checkable
class ScoreTrack(Protocol):
    kind: str

    def prepare(self, track: Mapping[str, Any], score: Any) -> None: ...
    def render(self, track: Mapping[str, Any], score: Any, out_dir: Path) -> Any: ...
    def live(self, track: Mapping[str, Any], score: Any) -> Any: ...


@runtime_checkable
class SceneEngine(Protocol):
    kind: str

    def render(self, scene: Mapping[str, Any], *, fps: float, frames: range, events: list[dict[str, Any]],
               size: tuple[int, int], out_dir: Path) -> list[Path]: ...


Factory = Callable[[PluginContext], Any]

__all__ = [
    "API_VERSION",
    "ApiRoutes",
    "AppProfile",
    "Backend",
    "BackendChoice",
    "BackendPolicy",
    "BackendSpec",
    "CliCommands",
    "Contribution",
    "CredentialsProvider",
    "Driver",
    "DriverSession",
    "Factory",
    "Fake",
    "Health",
    "LoginProvider",
    "McpTools",
    "NodeCapability",
    "Persona",
    "PluginContext",
    "ProcessSpec",
    "Ready",
    "SceneEngine",
    "ScoreTrack",
    "Seeder",
]
