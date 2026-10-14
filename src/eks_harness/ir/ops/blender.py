from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from eks_harness.ir.catalog.animated import AColor, AFloat, AVec3
from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.catalog.easing import Easing
from eks_harness.ir.catalog.keys import Keyframe
from eks_harness.ir.ops.scene import EVENT_NAME, SCENE_SEED_MAX, Trigger, typed_value
from eks_harness.ir.plan import FunctionSource
from eks_harness.ir.refs import RMedia, RStr
from eks_harness.ir.registry import Args, op
from eks_harness.ir.time import Fps
from eks_harness.ir.types import Color

NODE_ID = r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$"
BLENDER_FIXED_FIELDS = frozenset({"id", "kind"})
BLENDER_REFERENCE_FIELDS = frozenset({"focus", "look_at", "parent"})
Engine = Literal["CYCLES", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"]
ENGINE_ALIASES: dict[str, Engine] = {
    "cycles": "CYCLES",
    "eevee": "BLENDER_EEVEE_NEXT",
    "eevee_next": "BLENDER_EEVEE_NEXT",
    "workbench": "BLENDER_WORKBENCH",
}
DEFAULT_SAMPLES: dict[Engine, int] = {"CYCLES": 64, "BLENDER_EEVEE_NEXT": 32, "BLENDER_WORKBENCH": 1}
CPU_ENGINE: Engine = "CYCLES"
DEFAULT_ENGINE = "eevee"
WORKER_PLATFORM = r"^[a-z0-9]+-[a-z0-9_]+$"


def lookup_engine(name: str | None) -> Engine | None:
    chosen = DEFAULT_ENGINE if name is None else name
    for engine in DEFAULT_SAMPLES:
        if chosen == engine:
            return engine
    return ENGINE_ALIASES.get(chosen.strip().lower())


def engine_device_problem(engine: str, device: str) -> str | None:
    if device != "CPU" or engine == CPU_ENGINE:
        return None
    return (f"Blender engine {engine} has no CPU device: EEVEE and Workbench always rasterize through the GPU driver "
            "(or software GL on a node without a GPU), so device='CPU' would not give a CPU path; use device='GPU' "
            "or engine='cycles' for a CPU render")


class _Blender(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NodeBase(_Blender):
    id: str = Field(pattern=NODE_ID, description="Node id, unique in the scene")
    name: str | None = Field(default=None, description="Blender object name; the id when unset")
    parent: str | None = Field(default=None, description="Id of the parent node")
    location: AVec3 = Field(default=(0.0, 0.0, 0.0), description="Location (x, y, z) in meters, animatable")
    rotation: AVec3 = Field(default=(0.0, 0.0, 0.0), description="Rotation (x, y, z) in degrees, animatable")
    scale: AVec3 = Field(default=(1.0, 1.0, 1.0), description="Scale (x, y, z), animatable")
    visible: bool = Field(default=True, description="Render the node")


class LoadNode(NodeBase):
    kind: Literal["load"] = Field(default="load", description="Object or collection loaded from a .blend file")
    path: RMedia = Field(description="The .blend file, a $ref to a media.source node of kind file")
    obj: str | None = Field(default=None, description="Object to load from the file")
    collection: str | None = Field(default=None, description="Collection to load from the file")
    link: bool = Field(default=False, description="Link instead of append")


class CameraNode(NodeBase):
    kind: Literal["camera"] = Field(default="camera", description="Camera")
    lens: AFloat = Field(default=50.0, description="Focal length in mm, animatable")
    look_at: str | None = Field(default=None, description="Id of the node the camera tracks")
    focus: str | None = Field(default=None, description="Id of the node in focus; no depth of field when unset")
    f_stop: float | None = Field(default=None, gt=0, description="Aperture for depth of field")
    clip_start: float = Field(default=0.1, gt=0, description="Near clip distance in meters")
    clip_end: float = Field(default=1000.0, gt=0, description="Far clip distance in meters")


class LightNode(NodeBase):
    kind: Literal["light"] = Field(default="light", description="Light")
    light: Literal["point", "sun", "spot", "area"] = Field(default="area", description="Light type")
    energy: AFloat = Field(default=1000.0, description="Power in watts, animatable")
    color: AColor = Field(default="#ffffff", description="Light color, animatable")
    size: AFloat = Field(default=1.0, description="Size of the light source in meters, animatable")


class MeshNode(NodeBase):
    kind: Literal["mesh"] = Field(default="mesh", description="Primitive mesh")
    primitive: Literal["cube", "plane", "sphere", "cylinder", "cone", "torus"] = Field(description="Mesh primitive")
    size: float = Field(default=1.0, gt=0, description="Primitive size in meters")
    material: str | None = Field(default=None, description="Material name")


class TextNode(NodeBase):
    kind: Literal["text"] = Field(default="text", description="3D text")
    body: RStr = Field(description="Text")
    font: RMedia | None = Field(default=None, description="Font file, a $ref to a media.source node of kind file; Blender's default when unset")
    size: float = Field(default=1.0, gt=0, description="Text size in meters")
    extrude: float = Field(default=0.0, ge=0, description="Extrusion depth in meters")


class EmptyNode(NodeBase):
    kind: Literal["empty"] = Field(default="empty", description="Empty used as a parent or target")


SceneNode = Annotated[
    LoadNode | CameraNode | LightNode | MeshNode | TextNode | EmptyNode, Field(discriminator="kind")
]


class TextureBinding(_Blender):
    node: str = Field(pattern=NODE_ID, description="Id of the node whose material receives the texture")
    material: str = Field(min_length=1, description="Material name")
    slot: str = Field(min_length=1, description="Image texture node of the material")
    media: RMedia = Field(description="Image or video bound to the slot")


class Animation(_Blender):
    node: str = Field(pattern=NODE_ID, description="Id of the animated node")
    prop: str = Field(min_length=1, description="Property path such as location or lens")
    keys: tuple[Keyframe[JsonValue], ...] = Field(min_length=1, description="Keyframes; times are scene-local unless anchored")
    ease: Easing | None = Field(default=None, description="Easing between keyframes; linear when unset")


class Pulse(_Blender):
    kind: Literal["pulse"] = Field(default="pulse", description="Scale a property up and back")
    prop: str = Field(default="scale", description="Property to pulse")
    amount: float = Field(description="Peak factor such as 1.04")
    duration: Duration = Field(description="Length of the pulse")
    ease: Easing | None = Field(default=None, description="Easing of the pulse")


class SetProp(_Blender):
    kind: Literal["set"] = Field(default="set", description="Set a property")
    prop: str = Field(min_length=1, description="Property path")
    value: JsonValue = Field(default=None, description="New value")


class Emit(_Blender):
    kind: Literal["emit"] = Field(default="emit", description="Emit an event into the score")
    event: str = Field(pattern=EVENT_NAME, description="Event name")
    data: JsonValue = Field(default=None, description="Event data")


BlenderAction = Annotated[Pulse | SetProp | Emit, Field(discriminator="kind")]


class BlenderTrigger(_Blender):
    at: Trigger = Field(description="Time, time set or event that fires the action")
    node: str | None = Field(default=None, pattern=NODE_ID, description="Id of the node the action applies to")
    action: BlenderAction = Field(description="Action to run")


class BpyHook(_Blender):
    fn: FunctionSource = Field(description="Captured source of the @scene.bpy function")
    phase: Literal["build", "frame"] = Field(default="build", description="Run once while building the scene, or on every frame")


class World(_Blender):
    hdri: RMedia | None = Field(default=None, description="HDRI environment image, a $ref to a media.source node")
    color: Color | None = Field(default=None, description="Background color when there is no HDRI")
    strength: float = Field(default=1.0, ge=0.0, description="Environment light strength")


class BlenderRender(_Blender):
    engine: Engine = Field(description="Render engine after alias resolution (CYCLES, BLENDER_EEVEE_NEXT, BLENDER_WORKBENCH)")
    samples: int = Field(ge=1, description="Render samples per pixel")
    device: Literal["CPU", "GPU"] = Field(description="Render device")
    seed: int = Field(
        ge=0, le=SCENE_SEED_MAX,
        description="cycles.seed of the render; derived from the embedding clock's root seed and the scene's embedding key unless the scene sets seed=")
    size: tuple[int, int] = Field(description="Frame size as (width, height) in pixels")
    fps: Fps | None = Field(default=None, description="Frame rate; the embedding clock's when unset")
    transparent: bool = Field(default=True, description="Render with an alpha channel")
    view_transform: str = Field(default="AgX", description="Color management view transform")
    denoise: bool = Field(default=True, description="Denoise the render")
    platform: str | None = Field(
        default=None, pattern=WORKER_PLATFORM,
        description="Worker platform the render is pinned to, as os-arch the way nodes report it (linux-x86_64, "
                    "darwin-arm64); every frame renders on workers of that platform and the render fails when none "
                    "is available. Unset, the farm still renders all frames on one platform, chosen per run")

    @model_validator(mode="after")
    def _check_device(self) -> BlenderRender:
        problem = engine_device_problem(self.engine, self.device)
        if problem is not None:
            raise ValueError(problem)
        return self


class BlenderScene(_Blender):
    nodes: tuple[SceneNode, ...] = Field(default=(), description="Scene graph nodes")
    textures: tuple[TextureBinding, ...] = Field(default=(), description="Media bound to material texture slots")
    animations: tuple[Animation, ...] = Field(default=(), description="Keyframe animations")
    triggers: tuple[BlenderTrigger, ...] = Field(default=(), description="Actions fired on the clock or by events")
    hooks: tuple[BpyHook, ...] = Field(default=(), description="bpy escape-hatch functions")
    camera: str | None = Field(default=None, description="Id of the active camera; the first camera when unset")
    world: World | None = Field(default=None, description="World lighting")
    render: BlenderRender = Field(description="Render settings")
    duration: Duration | None = Field(default=None, description="Length of the scene; when unset, the length of the embedding clock's bounds (its window, else its duration), which clips cut from; required on an open clock (no window and no duration)")
    events: tuple[str, ...] = Field(default=(), description="Events the scene emits back into the score")


@op("blender", name="render", out="media", pure=True, location="blender", bounded=True,
    description="Compile the scene graph to one bpy program and render it with the Blender bootstrap and farm",
    examples=[
        {
            "scene": {
                "nodes": [
                    {"id": "phone", "kind": "load", "path": {"$ref": "n0000000001", "path": []}, "obj": "Phone"},
                    {"id": "camera", "kind": "camera", "lens": 50.0, "look_at": "phone"},
                ],
                "camera": "camera",
                "render": {"engine": "CYCLES", "samples": 32, "device": "GPU", "seed": 7, "size": [1080, 1920]},
            },
        },
    ])
class RenderBlender(Args):
    scene: BlenderScene = Field(description="The Blender scene to render")


def blender_set_values(scene: BlenderScene) -> list[tuple[int, Any]]:
    index: dict[str, NodeBase] = {}
    for node in scene.nodes:
        index.setdefault(node.id, node)
    found: list[tuple[int, Any]] = []
    for position, trigger in enumerate(scene.triggers):
        action = trigger.action
        node = index.get(trigger.node) if trigger.node is not None else None
        if not isinstance(action, SetProp) or node is None or action.prop in BLENDER_FIXED_FIELDS:
            continue
        typed, problem = typed_value(node, action.prop, action.value)
        if problem is None and typed is not None:
            found.append((position, typed))
    return found


__all__ = [
    "BLENDER_FIXED_FIELDS",
    "BLENDER_REFERENCE_FIELDS",
    "CPU_ENGINE",
    "DEFAULT_ENGINE",
    "DEFAULT_SAMPLES",
    "ENGINE_ALIASES",
    "NODE_ID",
    "WORKER_PLATFORM",
    "Animation",
    "BlenderAction",
    "BlenderRender",
    "BlenderScene",
    "BlenderTrigger",
    "BpyHook",
    "CameraNode",
    "Emit",
    "EmptyNode",
    "Engine",
    "LightNode",
    "LoadNode",
    "MeshNode",
    "NodeBase",
    "Pulse",
    "RenderBlender",
    "SceneNode",
    "SetProp",
    "TextNode",
    "TextureBinding",
    "World",
    "blender_set_values",
    "engine_device_problem",
    "lookup_engine",
]
