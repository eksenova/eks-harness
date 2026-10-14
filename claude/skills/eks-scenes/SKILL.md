---
name: eks-scenes
description: "Blender and web scenes in eks as they work today: @eks.scene(kind=\"blender\") with load, camera, light, mesh, text, world, materials and textures from other media, animations, beat triggers and the @sc.bpy escape hatch; @eks.scene(kind=\"web\") with props, textures and emitted events; scene seeds, engines and devices; embedding scenes in edits and scores. States plainly which parts render now and which wait for the scene graph executor. Use when writing or planning a 3D or HTML scene for a video."
---

# Scenes

Three scene kinds share `@eks.scene`: `gfx` (2D layers, skill `eks-harness:eks-gfx-audio`), `blender` and
`web`. A scene is media: run it alone or embed it in an edit or score with `e.scene(...)`, `sc.scene(...)`.

## What runs today

| Kind | `eks-harness plan` | `eks-harness run` |
| --- | --- | --- |
| `gfx` | yes | renders (`gfx.render`) |
| `blender` | yes, with engine, samples, device and seed resolved | not yet: `blender.render` has no executor; `--fake blender.render` simulates it |
| `web` | yes | not yet: `scene.render` has no executor; `--fake scene.render` simulates it |

The Blender scene graph executor, web scene rendering under the new engine, live preview and the render
farm changes (content-addressed sync bundles, local runs without packing) are milestone M5 work. Until they
land, render real Blender and HTML scenes through the earlier video engine: skills
`eks-harness:blender-scenes` and `eks-harness:web-scenes`, inside a video project (`eks-harness:video-editing`).

## Blender

```python
import eks
from eks import t


@eks.scene(kind="blender", engine="cycles", samples=32, seed=7)
def phone(sc: eks.BlenderScene, screen: eks.Media):
    p = sc.load("assets/phone.blend", obj="Phone")
    p.material("Screen").texture("screen", screen)
    cam = sc.camera(lens=50).look_at(p)
    cam.animate("location", {t("bar:0"): (0, -4, 1), t("bar:4"): (0, -2, 1)}, ease="out_cubic")
    sc.on(t.beats("downbeat")[0:8], p.pulse(scale=1.04))

    @sc.bpy
    def rig(bpy, ctx):
        return None
```

```bash
eks-harness plan .harness/scenes/phone.py --set screen=artifact:VID123
```

- Options: `engine=` (`eevee` default; aliases resolve at plan time, so `cycles` becomes the Cycles engine),
  `samples=`, `device="GPU"|"CPU"` (CPU only with Cycles), `seed=` (`cycles.seed`), `size=`, `fps=`,
  `duration=`, `bpm=`, `transparent=True`, `platform="darwin-arm64"` (pins every frame to one worker
  platform).
- Nodes: `load(path, obj=, collection=, link=)`, `camera(lens=, location=, rotation=)` with `.look_at(obj)`
  and `.focus(obj, f_stop=)`, `light("area"|"point"|"sun"|"spot", energy=, color=, size=)`,
  `mesh("cube"|"plane"|"sphere"|"cylinder"|"cone"|"torus", size=, material=)`, `text(body, font=, size=,
  extrude=)`, `empty()`, `world(hdri=, color=, strength=)`.
- Object handles: `.material(name).texture(slot, media)` (a texture can be a take, a gfx render or any
  media), `.set(**props)`, `.animate(prop, {time: value}, ease=)`, actions `.pulse(scale=)`, `.assign(prop,
  value)`, `.emit(event, data)`; `sc.on(trigger, *actions)`; `sc.emits("impact")` declares events.
- `@sc.bpy` and `@sc.bpy_frame` run Python against `bpy` (with `math` and `mathutils`); their source is
  hashed into the plan, so edits re-render.

## Web scenes

```python
@eks.scene(kind="web", entry="scenes/cards/index.html", size=(1080, 1920), fps=30, duration=t("8s"))
def cards(sc: eks.WebScene):
    sc.prop("accent", "#f5c542")
    sc.on(t.beats("downbeat")[0:4], emit="flip")
    sc.emits("flipped")
```

`entry` is an HTML file; its folder is the bundle. `prop(name, value)`, `texture(name, media)`,
`on(trigger, emit=, data=)` and `emits(*events)` declare the scene's inputs and events. The page uses the
`@eks-harness/scene` runtime (`ehx.on`, `ehx.onFrame`, `ehx.emit`, virtual clock); see skill
`eks-harness:web-scenes` for the page side.

## Determinism

A scene's seed is explicit (`seed=7` stays 7 everywhere) or derived from the embedding clock's root seed
and the embedding path, so two embeddings of one scene get different streams and a standalone plan is
stable. Engine, samples and device are explicit in the plan; GPU and EEVEE renders are keyed per machine,
CPU Cycles pinned to a platform is cacheable.

Generated reference: `docs/reference/ops.md` (families `blender`, `scene`).
