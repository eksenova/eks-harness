INSTRUCTIONS = """\
Video tools of eks-harness.

Tools:
  - render_project       - render a project (preview/final); returns a job reference at once and
                           reports progress; status at eks-harness://video/render_jobs/<id>.
  - cancel_job           - cancel a render job.
  - validate_project     - load project.py/project.json and report validation errors.
  - inspect_project      - summarize timeline, effect compile-target split,
                           marker sources and random_seed.
  - extract_markers      - run every declared MarkerSource and return resolved
                           streams / named markers / words / warnings.
  - list_effects         - enumerate every registered effect IR model with
                           kind, compile_targets, doc and fields.
  - add_segment          - append a Segment to a named track in project.json.
  - add_effect           - append an effect to a segment in project.json.
  - set_property         - set one field in project.json via dotted/indexed path.

Resources (eks-harness://video/...): templates, projects, effects, easings, audio_lib,
video_lib, image_lib, plugins, encoding_presets, render_jobs, preview_frames.

Two well-known roots are honoured: media_library (optional) and
project_workspace (default: the video.workspace setting).

Optional extras (lazy-imported; fail with a clear message if missing):
  - eks-harness[sam3]    - SAM3Track (Meta SAM 3 image model). Needs manual
                           `pip install --no-deps git+.../facebookresearch/sam3` plus
                           gated HF weights (request access, then `hf auth login`).
  - eks-harness[matting] - BiRefNetRemove (rembg BiRefNet-portrait ONNX, per-frame)
                           and MatAnyoneRemove (frame-by-frame
                           `matanyone.InferenceCore.step`). MatAnyone needs manual
                           `pip install --no-deps git+.../pq-yang/MatAnyone` plus a
                           torchvision matching the installed torch.
  - eks-harness[stems]   - SeparatedStem via Demucs (htdemucs) for vocals/drums/bass/other.
  - eks-harness[denoise] - DeepFilterDenoise (deepfilternet) on AudioSegment.denoise.
  - eks-harness[html]    - HTMLOverlay via Playwright (also run `playwright install chromium`).
  - eks.blender plugin   - BlenderScene: a .blend file / bpy script rendered headless, frame-locked to
                           the timeline (markers reach the script as scene frames via
                           `import eks_harness.blender`); enable the plugin per project and install
                           Blender 4.2+ (on PATH, EKS_HARNESS_BLENDER or the plugin's executable setting).
  - eks-harness[captions-onnx] - ONNX Whisper for the `winml-whisper` captioner (GPU/NPU-capable STT).
  - eks-harness[winml]   - Windows ML execution providers (AMD MIGraphX GPU / VitisAI NPU) for ONNX
                           matting and the captioner; Windows-only, needs a recent Adrenalin driver.

Rendering is hardware-accelerated where available (ffmpeg AMF/NVENC/QSV encode,
`-hwaccel` decode, Chromium GPU for HTML overlays); set `EKS_HARNESS_HWACCEL=0` to
force software.

Heavy ML effects (SAM3, MatAnyone) load model weights on first use. On AMD
ROCm set `TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1` in the daemon environment.
"""
