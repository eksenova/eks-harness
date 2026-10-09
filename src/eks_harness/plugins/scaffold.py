from __future__ import annotations

import re
from pathlib import Path

from eks_harness.plugins.manifest import PLUGIN_ID, load_manifest

TEMPLATES_ROOT = Path(__file__).resolve().parent / "templates"


def template_kinds(root: Path = TEMPLATES_ROOT) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def _slug(plugin_id: str) -> str:
    return plugin_id.rsplit(".", 1)[-1]


def _module(plugin_id: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", plugin_id.replace(".", "_"))


def scaffold(kind: str, target: Path, plugin_id: str, *, name: str = "", description: str = "",
             root: Path = TEMPLATES_ROOT, extra_roots: tuple[Path, ...] = ()) -> list[Path]:
    if not PLUGIN_ID.fullmatch(plugin_id):
        raise ValueError(f"'{plugin_id}' is not a plugin id (lower case, dot separated, for example acme.backend)")
    source = next((r / kind for r in (*extra_roots, root) if (r / kind).is_dir()), None)
    if source is None:
        raise ValueError(f"no template '{kind}'; known: {', '.join(template_kinds(root))}")
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"{target} is not empty")
    values = {"id": plugin_id, "slug": _slug(plugin_id), "module": _module(plugin_id),
              "name": name or _slug(plugin_id).replace("-", " ").title(),
              "description": description or f"{kind} plugin {plugin_id}"}
    written: list[Path] = []
    for file in sorted(source.rglob("*.tmpl")):
        relative = file.relative_to(source).as_posix()[: -len(".tmpl")]
        for key, value in values.items():
            relative = relative.replace("{{" + key + "}}", value)
        text = file.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        out = target / relative
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        written.append(out)
    load_manifest(target)
    return written
