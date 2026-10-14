---
name: nodes
description: "Add and operate nodes: machines that dial out to the hub and run jobs (Blender frame ranges, previews, shell jobs, plugin jobs) in GPU and CPU slots; installing over SSH (including WSL), slots and CUDA or OptiX devices, blobs, the job queue and the render queue, troubleshooting. Use for render farms and remote capacity."
---

# Nodes and the render farm

```bash
eks-harness node add user@gpu-box --slots '[{"id":"gpu0","workers":2,"env":{"CUDA_VISIBLE_DEVICES":"0"},"tags":["gpu","optix"],"backend":"OPTIX"}]'
eks-harness node add user@win-box --wsl
eks-harness node list
eks-harness node show gpu-box
eks-harness node set gpu-box --slots '<json>'
eks-harness job submit probe --requirements '{"node":"gpu-box"}' --wait 60
eks-harness job list --state running
eks-harness queue                                   # renders on the hub, one at a time
```

- `node add` builds a wheel from this checkout, installs it with uv on the host, writes the node settings
  (hub URL and a token, mode 600) and installs a user service (`eks-harness node service install`). On Linux
  enable lingering so it survives logout.
- Nodes dial the hub over WebSocket; no inbound ports. Inputs and outputs move as content-addressed blobs.
- A job runs in a slot whose tags and backend satisfy its requirements (`gpu`, `capabilities`, `backend`,
  `os`, `node`). Jobs requeue when a node drops, up to `nodes.maxAttempts`.
- Blender frame jobs use the node's Blender and the slot's environment (GPU selection, Cycles device). They
  serve the earlier video engine's Blender scenes today (skill `eks-harness:blender-scenes`).
- In eks plans, render nodes (`edit.render`, `gfx.render`, `audio.mix`) take a render slot on the hub's render
  queue; the run journals the wait. `blender.render` will spread frames over nodes once its executor lands
  (skill `eks-harness:eks-scenes`); a Blender scene pinned to a `platform=` will then only use workers of that
  platform.
