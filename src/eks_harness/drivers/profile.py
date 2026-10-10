from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from eks_harness.drivers.workers import encoder_command

PLATFORMS = ("web", "ios", "android")
LEASE_KINDS = {"web": "browser", "ios": "ios", "android": "android"}
WORKER_KINDS = {"web": "web", "ios": "mobile", "android": "mobile"}
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ProfileError(ValueError):
    pass


def expand(value: Any, env: dict[str, str] | None = None) -> Any:
    source = os.environ if env is None else env
    if isinstance(value, str):
        return _VAR.sub(lambda m: source.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand(item, source) for item in value]
    if isinstance(value, dict):
        return {key: expand(item, source) for key, item in value.items()}
    return value


@dataclass
class AppProfile:
    root: Path
    name: str = "app"
    project: str | None = None
    session: str | None = None
    tags: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ("web",)
    web: dict[str, Any] = field(default_factory=dict)
    mobile: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    app_file: Path | None = None

    @property
    def default_platform(self) -> str:
        return self.platforms[0]

    def resolve(self, relative: str) -> Path:
        path = Path(relative).expanduser()
        return path if path.is_absolute() else (self.root / path).resolve()


def merge_tables(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        out[key] = merge_tables(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load_profile(harness_dir: Path, env: dict[str, str] | None = None, _seen: tuple[Path, ...] = ()) -> AppProfile:
    file = harness_dir / "app.toml"
    data: dict[str, Any] = {}
    if file.is_file():
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            raise ProfileError(f"{file}: {error}") from error
    data = expand(data, env)
    own_app = harness_dir / "app.py"
    app_file = own_app if own_app.is_file() else None
    root = harness_dir.parent
    extends = (data.get("app") or {}).get("extends")
    if extends:
        target = (harness_dir.parent / str(extends)).resolve()
        if target in _seen or len(_seen) > 8:
            raise ProfileError(f"{file}: [app] extends loops back to {target}")
        if not (target / ".harness").is_dir():
            raise ProfileError(f"{file}: [app] extends {extends!r} has no .harness directory")
        base = load_profile(target / ".harness", env, (*_seen, harness_dir.parent.resolve()))
        local_app = {k: v for k, v in (data.get("app") or {}).items() if k != "extends"}
        data = merge_tables(base.raw, {**data, "app": local_app})
        root = base.root
        app_file = app_file or base.app_file
    app = data.get("app") or {}
    platforms = app.get("platforms") or ([app["platform"]] if app.get("platform") else None)
    if not platforms:
        platforms = ["web"] if data.get("web") or not data.get("mobile") else ["ios", "android"]
    unknown = [p for p in platforms if p not in PLATFORMS]
    if unknown:
        raise ProfileError(f"{file}: unknown platform(s) {', '.join(unknown)} (use web, ios, android)")
    return AppProfile(root=root, name=str(app.get("name") or root.name),
                      project=app.get("project"), session=app.get("session") or None,
                      tags=tuple(str(t) for t in (app.get("tags") or [])), platforms=tuple(platforms),
                      web=dict(data.get("web") or {}), mobile=dict(data.get("mobile") or {}),
                      checks=dict(data.get("checks") or {}), raw=data, app_file=app_file)


def pace_from_config(config: Any) -> dict[str, int]:
    keys = ("moveMs", "dwellMs", "typeMsPerChar", "holdMs", "screenHoldMs", "mobilePressMs")
    out: dict[str, int] = {}
    for key in keys:
        try:
            out[key] = int(config[f"capture.pace.{key}"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


log = logging.getLogger("eks_harness.drivers.profile")


def project_hide_selectors(client: Any, project: str | None) -> list[str]:
    from eks_harness.cli.client import ApiClientError

    if client is None or not project or "/" not in project:
        return []
    owner, name = project.split("/", 1)
    try:
        data = client.request("GET", f"/api/projects/{quote(owner, safe='')}/{quote(name, safe='')}")
    except ApiClientError as error:
        log.warning("could not read the hidden selectors of %s: %s", project, error)
        return []
    settings = (data or {}).get("settings") or {}
    return [str(item) for item in settings.get("capture.hideSelectors") or []]


def node_modules_dir(paths: Any) -> Path:
    return paths.cache_dir / "node"


def web_worker_config(profile: AppProfile, *, sid: str, extensions: list[dict[str, Any]], pace: dict[str, int],
                      hide: list[str] | None = None, theme: dict[str, Any] | None = None,
                      playwright_dirs: list[Path] | None = None) -> dict[str, Any]:
    web = profile.web
    url = web.get("url")
    if not url:
        raise ProfileError(f"{profile.root}/.harness/app.toml needs [web] url (the address of the app under test)")
    config: dict[str, Any] = {
        "baseUrl": url,
        "sid": sid,
        "home": web.get("home", "/"),
        "locale": web.get("locale", "en-US"),
        "nav": web.get("nav", "location"),
        "extensions": extensions,
        "pace": pace,
        "encoder": encoder_command(),
        "hideSelectors": [*(hide or []), *(web.get("hide") or [])],
        "options": {"settleReact": bool(web.get("settle_react", False)), **(web.get("options") or {})},
    }
    optional = {
        "apiBaseUrl": web.get("api_url"),
        "timezone": web.get("timezone"),
        "networkIgnore": web.get("network_ignore"),
        "wsIgnore": web.get("ws_ignore"),
        "viewports": web.get("viewports"),
        "appOrigins": web.get("app_origins"),
        "theme": {**(theme or {}), **(web.get("theme") or {})} or None,
        "playwrightFrom": [*(str(profile.resolve(p)) for p in web.get("playwright_from") or []),
                           *(str(p) for p in playwright_dirs or [])] or None,
    }
    config.update({k: v for k, v in optional.items() if v})
    return config


def mobile_worker_config(profile: AppProfile, *, sids: dict[str, str], extensions: list[dict[str, Any]],
                         pace: dict[str, int]) -> dict[str, Any]:
    mobile = profile.mobile
    config: dict[str, Any] = {
        "sids": sids,
        "metroPort": int(mobile.get("metro_port") or 0),
        "fixtureDirs": [str(profile.resolve(p)) for p in mobile.get("fixtures") or []],
        "extensions": extensions,
        "pace": pace,
        "options": dict(mobile.get("options") or {}),
    }
    if mobile.get("fake_slots"):
        config["fakeSlots"] = list(mobile["fake_slots"])
    return config
