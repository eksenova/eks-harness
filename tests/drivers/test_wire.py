from __future__ import annotations

from pathlib import Path

import pytest

import eks
from eks_harness.drivers.wire import (
    WireError,
    to_wire,
    typescript,
    wire_schemas,
    worker_ops,
)
from eks_harness.ir.plan import SessionSpec
from eks_harness.ir.presets import Viewport
from eks_harness.ir.registry import Registry, default_registry

ROOT = Path(__file__).resolve().parents[2]
OPS_DTS = ROOT / "workers" / "kit" / "src" / "ops.d.ts"
TARGET = {"by": "test_id", "args": {"id": "save"}}


@pytest.fixture
def registry() -> Registry:
    assert eks is not None
    return default_registry()


def test_every_worker_op_has_a_wire_schema(registry: Registry) -> None:
    schemas = wire_schemas(registry)
    names = {spec.name for spec in worker_ops(registry)}
    assert set(schemas["ops"]) == names
    assert "Target" in schemas["$defs"]
    web = wire_schemas(registry, "web")["ops"]
    assert "web.wheel" in web and "mobile.dispatch" not in web
    ios = wire_schemas(registry, "ios")["ops"]
    assert "mobile.dispatch" in ios and "web.wheel" not in ios


def test_durations_become_milliseconds_and_targets_stay(registry: Registry) -> None:
    wire = to_wire(registry.op("ui.tap"), {"target": TARGET, "timeout": "1.5s"}, session=None, registry=registry)
    assert wire["timeout"] == 1500
    assert wire["target"] == {"by": "test_id", "args": {"id": "save"}}
    schema = wire_schemas(registry)
    tap = schema["$defs"][schema["ops"]["ui.tap"]["$ref"].rsplit("/", 1)[-1]]
    assert {"type": "integer", "description": "milliseconds"} in tap["properties"]["timeout"]["anyOf"]


def test_native_targets_keep_their_surface(registry: Registry) -> None:
    target = eks.by.text("İzin ver").native
    wire = to_wire(registry.op("ui.tap"), {"target": target.model_dump(mode="json", by_alias=True, exclude_none=True)},
                   session=None, registry=registry)
    assert wire["target"]["surface"] == "native"


def test_presets_resolve_to_their_values(registry: Registry) -> None:
    wire = to_wire(registry.op("capture.record"), {"name": "demo", "pace": "demo", "viewport": "phone"}, session=None,
                   registry=registry)
    assert wire["pace"]["move"] == 300 and wire["pace"]["type_per_char"] == 40
    assert wire["viewport"]["name"] == "phone" and wire["viewport"]["width"] == 390
    session = SessionSpec(app="web-app", platform="web",
                          viewports={"phone": Viewport(width=400, height=800, mobile=True, touch=True)})
    own = to_wire(registry.op("capture.shot"), {"name": "x", "viewport": "phone"}, session=session, registry=registry)
    assert own["viewport"] == {"name": "phone", "width": 400, "height": 800, "scale": 1.0, "mobile": True,
                               "touch": True}
    with pytest.raises(WireError, match="not registered"):
        to_wire(registry.op("capture.shot"), {"name": "x", "viewport": "nope"}, session=None, registry=registry)


def test_anchored_durations_cannot_reach_a_device(registry: Registry) -> None:
    with pytest.raises(WireError, match="plain duration"):
        to_wire(registry.op("ui.tap"), {"target": TARGET, "timeout": "1b"}, session=None, registry=registry)


def test_media_refs_become_files(registry: Registry, tmp_path: Path) -> None:
    file = tmp_path / "id.jpg"
    file.write_bytes(b"jpg")
    wire = to_wire(registry.op("fake.arm"), {"slot": "camera", "media": {"uri": "artifact:A1", "kind": "image",
                                                                         "artifact": "A1"}},
                   session=None, registry=registry, artifact_file=lambda artifact: file if artifact == "A1" else None)
    assert wire["media"] == {"path": str(file), "mime": "image/jpeg", "name": None}


def test_typescript_declarations_are_current(registry: Registry) -> None:
    assert OPS_DTS.read_text(encoding="utf-8") == typescript(registry), (
        "workers/kit/src/ops.d.ts is stale; regenerate it with "
        "python -c \"import eks; from eks_harness.ir.registry import default_registry; "
        "from eks_harness.drivers.wire import typescript; "
        "open('workers/kit/src/ops.d.ts','w').write(typescript(default_registry()))\"")


def test_the_kit_draws_every_overlay_icon() -> None:
    import re
    import typing

    from eks_harness.ir.ops.overlay import IconName

    source = (ROOT / "workers" / "kit" / "src" / "icons.mjs").read_text(encoding="utf-8")
    drawn = set(re.findall(r"^\s+'?([a-z-]+)'?:", source, flags=re.MULTILINE))
    assert drawn == set(typing.get_args(IconName))
