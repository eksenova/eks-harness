---
name: blender-scenes
description: Write Blender scene scripts for the video engine and scores: BlenderScene media, the eks_harness.blender timing API (frames of beats and markers, score inputs, emitting events), textures from other tracks, engines and devices, the frame cache and node rendering. Use when creating or debugging Blender content synced to a timeline.
---

# Blender scenes

Blender is optional: enable the `eks.blender` plugin per project (`[plugins] enable = ["eks.blender"]`).
It needs Blender 4.2 or newer (`EKS_HARNESS_BLENDER` or on PATH) on the hub or a node.

```python
import bpy
import eks_harness.blender as ehb

cube = bpy.data.objects["Cube"]
for f in ehb.frames("beat"):
    cube.scale = (1.2, 1.2, 1.2)
    cube.keyframe_insert("scale", frame=f)
    cube.scale = (1.0, 1.0, 1.0)
    cube.keyframe_insert("scale", frame=f + 6)
for f in ehb.input_frames("impact"):
    ehb.emit("landed", f + 3, {"from": f})
screen = ehb.media.get("screen")
```

API: `fps`, `frame_start`, `frame_end`, `params`, `media`, `times(stream)`, `frames(stream)`,
`named(name)`, `words()`, `frame_at(source_t)`, `frame_at_project(t)`, `inputs(name)`,
`input_frames(name)`, `emit(name, frame, data)` (set `emits=True` on the scene so the engine collects
emitted events).

- Renders are cached per frame by a scene fingerprint; unchanged frames are not rendered again.
- With nodes online, frames spread over GPU slots (`eks-harness:nodes`).
- Keep scripts deterministic: no wall-clock time, seed randomness from `ehb.params`.
