# Video engine troubleshooting

Recurring failure modes and how to fix them. Ordered roughly by how
often each one shows up in practice.

## "ffmpeg not found" on render

**Symptom** - `FileNotFoundError: [Errno 2] No such file or directory: 'ffmpeg'`
during the render step (validation passes; render fails).

**Cause** - The engine shells out to `ffmpeg` for the encoder leg. It must
be on `PATH` or supplied via `eks-harness video render --ffmpeg /full/path/to/ffmpeg`.

**Fix** -

- macOS: `brew install ffmpeg`
- Windows: `winget install Gyan.FFmpeg` (or grab a static build from
  gyan.dev / BtbN and put `ffmpeg.exe` on `PATH`).
- Linux: `sudo apt install ffmpeg` / `sudo dnf install ffmpeg`.

Verify with `ffmpeg -version` from the same shell that runs the harness.
On Windows, restart the shell after installing - `winget` does not
update the active session's `PATH`.

## `madmom` import error on Windows

**Symptom** - `ImportError: numpy.core.multiarray failed to import` when
the `BeatTracker` extractor loads, only on Windows + Python 3.12+.
BeatNet bundles madmom as a hard dependency, so the same error fires
when the default `backend="auto"` chain tries to load BeatNet first.

**Cause** - `madmom` was built against numpy 1.x; numpy 2.x changed the
multiarray ABI.

**Fix** - Pin numpy below 2.0 in the same environment:
`pip install "numpy<2"`. Re-create the venv if the conflict is sticky.
Linux/macOS users can usually skip the pin; Windows wheels for `madmom`
have not been re-released. As a workaround that needs no rebuild, pin
the IR to `BeatTracker(..., backend="librosa")` - librosa is pure-Python
and numpy-2-compatible, so the chain skips madmom entirely.

## "PyAV stdin pipe closed" mid-render

**Symptom** - Render starts producing frames, then dies with a broken
pipe / `BrokenPipeError` from inside the frame-pipeline encoder feed.

**Cause** - Almost always the *source* codec, not the engine. Some
in-the-wild mp4s (10-bit HEVC from phones, AV1 from yt-dlp) decode
intermittently and stall the rdframe loop.

**Fix** - Transcode the source to a clean intermediate first:

```bash
ffmpeg -i input.mov -c:v libx264 -crf 12 -preset slow -c:a copy intermediate.mp4
```

Point your `VideoFile(path=...)` at `intermediate.mp4` and re-render.
If you hit this repeatedly, set the project's `RenderSettings.quality_profile="preview"`
while iterating; the preview pipeline tolerates more codec quirks.

## "NVENC session limit reached"

**Symptom** - `Cannot load NVENC: Out of session limits` after the
second or third concurrent render on a consumer NVIDIA GPU.

**Cause** - Consumer-grade GeForce drivers cap NVENC to two simultaneous
encode sessions. Pro / data-center cards do not have this limit.

**Fix** -

- Serialise renders (queue them; do not fan out).
- Or fall back to `libx264` for everything except the final pass.
- Or apply the community NVENC patch on Linux (unsupported; do not ship
  on customer machines).

Set `RenderSettings(encoder="libx264")` to opt out of NVENC entirely.

## VideoToolbox ignores `crf`

**Symptom** - Setting `RenderSettings.crf=18` on macOS produces
identical-bitrate outputs to `crf=23`.

**Cause** - Apple's VideoToolbox encoder does not implement constant-
rate-factor mode. It honors target bitrate only.

**Fix** - When the encoder is VideoToolbox, use `bitrate_kbps` instead:

```python
RenderSettings(encoder="videotoolbox", bitrate_kbps=10_000)
```

If the user really needs CRF on macOS, switch to `libx264` (the
all-software encoder; available everywhere).

## Captions never appear in the output

**Symptom** - Render completes; captions are simply missing. No error.

**Cause** - `Captions.source` does not match any `MarkerSource.name`. The
mismatch is not a hard validation error today (the captioner just gets
no input and renders nothing).

**Fix** - The `name` on the `STTMarkers` source must equal the `source`
on the `Captions` effect, byte-for-byte:

```python
markers=[STTMarkers(name="speech", source="tracks[0].segments[0]")],
...
effects=[Captions(source="speech", ...)],   # must be "speech"
```

## Beat detection feels late by half a beat

**Symptom** - Flashes consistently land 200–300 ms after the perceived
kick.

**Cause** - `librosa`'s onset-strength tracker has a known group-delay
bias on percussive content. `madmom`'s RNN tracker is tighter but still
drifts on syncopated material. BeatNet (CRNN + DBN) is the current
open-source SOTA and locks the pulse closer to the audio attack.

**Fix** -

- For polished output: install the `[ml]` extras
  (`pip install eks-harness[ml]`). `BeatTracker.backend="auto"` then picks
  BeatNet automatically and falls back to madmom if BeatNet's import
  fails.
- To force BeatNet (and surface a warning if it's missing rather than
  silently falling back): set `backend="beatnet"` on the
  `BeatTracker`.
- For half-time / double-time errors on EDM and hip-hop: BeatNet's DBN
  is the most reliable of the three; pin to it explicitly.
- For quick fixes that don't need a re-render of markers: shift
  `BeatRef.offset_ms=-100` (negative = earlier) on the affected
  references.

## Frame-pipeline OOM on 4K footage

**Symptom** - Render killed by the OS / Python `MemoryError` only on 4K
sources, only when frame-pipeline effects (`Captions`, `MatAnyoneRemove`,
`BiRefNetRemove`, `SAM3Track`, `RGBSplit`) are in the chain.

**Cause** - The frame pipeline holds full-resolution `np.uint8` buffers
plus temp masks. At 3840×2160 this is ~25 MB per active buffer; with
ML masks and intermediate alpha layers, peak usage climbs fast.

**Fix** -

- Use `RenderSettings(quality_profile="preview")` while iterating; the
  preview profile downsamples internally before running frame-pipeline
  effects.
- For finals, render in segments via `eks-harness video render --range START:END`
  and concatenate with ffmpeg.
- If a `Crop` would tighten the working area before the heavy effect,
  put it earlier in the chain - the orchestrator runs effects in order.

## "Project ends at Xs which exceeds project duration"

**Symptom** - `ValueError: segment 'foo' ends at 17.0s which exceeds
project duration 16.0s` during validation.

**Cause** - Segment placement (`start + (out - in_)`) overflows
`Project.duration`. Validation is strict on purpose - silent truncation
would hide bugs.

**Fix** - Either bump `Project.duration` or shorten the offending
segment's `out`. The error names the segment id so it is easy to find.

## `Blur` / `Sharpen` makes the render unusably slow

**Symptom** - Adding `Blur(radius=...)` or `Sharpen(amount=...)` to a
segment increases render time several-fold even though the other
effects on the segment used to fold cleanly into the ffmpeg graph.

**Cause** - `Blur` and `Sharpen` are ffmpeg-foldable on their own, but
if a frame-pipeline effect (`Captions`, `RGBSplit`, `MatAnyoneRemove`)
sits *later* in the chain, the orchestrator splits the pipeline at
that boundary - and now the blur runs on full-resolution buffers in
the slower frame path rather than inside ffmpeg.

**Fix** -

- Reorder so all ffmpeg-foldable effects come *before* the first
  frame-pipeline effect. Run `inspect_project` via the MCP server to
  see exactly where the split happens.
- If the heavy effect's working area is smaller than the frame,
  insert a `Crop` earlier in the chain to shrink the buffers before
  the blur runs.
- For preview iterations, set `RenderSettings(quality_profile="preview")`
  so the frame pipeline downsamples internally.

## Pillow not found when using `LowerThird` / `StickerOverlay`

**Symptom** - `ImportError: No module named 'PIL'` the first time a
project that uses `LowerThird`, `StickerOverlay` or text-rendering
overlays actually renders. Validation passes (Pillow is lazy-imported).

**Cause** - The overlay rasterisers use Pillow for glyph rendering and
RGBA alpha-composition, and the environment was installed without it.

**Fix** - Install the video extra: `uv tool install 'eks-harness[video]'`
(or `pip install 'eks-harness[video]'`), which pulls Pillow.

## `ModuleNotFoundError` / `RuntimeError` from `SAM3Track`, `MatAnyoneRemove`, `BiRefNetRemove`, `SeparatedStem`, `DeepFilterDenoise`, `HTMLOverlay`

**Symptom** - Render fails with `RuntimeError: X requires pip install
eks-harness[Y]` (or, in older builds, a bare `ModuleNotFoundError`) the
first time the project actually renders. Validation passes - these
backends are all lazy-imported.

**Cause** - The new SOTA IRs are gated behind optional extras so the
base install stays light. The renderer probes the backend on first
use and raises only if the import fails.

**Fix** - Install the matching extra:

| IR | Install |
|---|---|
| `SAM3Track` | `git clone https://github.com/facebookresearch/sam3 && cd sam3 && pip install --no-deps .` **then** `pip install eks-harness[sam3]` (gated weights - see below) |
| `MatAnyoneRemove` | `pip install eks-harness[matting]` **and** `pip install --no-deps git+https://github.com/pq-yang/MatAnyone` |
| `BiRefNetRemove` | `pip install eks-harness[matting]` |
| `SeparatedStem` | `pip install eks-harness[stems]` |
| `DeepFilterDenoise` | `pip install eks-harness[denoise]` |
| `HTMLOverlay` | `pip install eks-harness[html]` **and** `playwright install chromium` |

`SAM3Track` and `MatAnyoneRemove` cannot be installed with a plain
`pip install eks-harness[X]` alone - neither SAM 3 nor MatAnyone has a
PyPI release, so each needs the manual git install shown above in
addition to the extra. `eks-harness[sam3]` / `eks-harness[matting]` only
pull the runtime deps (timm/iopath/ftfy/hydra/pycocotools/triton for
sam3; rembg/thinplate for matting).

`HTMLOverlay` specifically needs two install steps - the pip extra
pulls in Playwright but not its browser binary; `playwright install
chromium` downloads the headless Chromium build the renderer drives.
Without that second step the renderer raises a
`playwright._impl._api_types.Error: Executable doesn't exist`. Verify
with `python -c "from playwright.sync_api import sync_playwright;
sync_playwright().start().chromium.launch()"`; it should return
without printing anything.

`eks-harness video doctor` reports which of the extras are present, so run it
once after install if you are unsure.

## `SAM3Track` fails with `403 Forbidden` / `GatedRepoError`

**Symptom** - `SAM3Track` import succeeds but the first inference dies
downloading weights with `huggingface_hub.errors.GatedRepoError` or an
HTTP `403 Forbidden` on `facebook/sam3`.

**Cause** - The `facebook/sam3` weights are GATED. The download fails
until Meta has approved your access *and* the host is authenticated.

**Fix** -

- Visit https://huggingface.co/facebook/sam3 while logged in, click
  "Request access" and agree to the terms. Wait for Meta to approve
  (you get an email).
- Authenticate the host: `hf auth login` (paste a token from
  https://huggingface.co/settings/tokens), or export `HF_TOKEN`.
- Re-run. The first inference then downloads ~3.3 GB.

## `BFloat16 and Float` dtype error from SAM 3 in custom code

**Symptom** - `RuntimeError: expected scalar type BFloat16 but found
Float` (or the reverse) when calling SAM 3 directly.

**Cause** - SAM 3's weights load in `bfloat16` but the image transform
produces a `float32` tensor; the two only reconcile under autocast.
The `SAM3Track` plugin already wraps inference in
`torch.autocast(device_type="cuda", dtype=torch.bfloat16)`, so projects
using the IR never hit this. It only bites when calling
`Sam3Processor` from your own code.

**Fix** - Wrap the `set_image` / `set_text_prompt` calls in
`with torch.autocast(device_type="cuda", dtype=torch.bfloat16):`.

## `module 'mediapipe' has no attribute 'solutions'`

**Symptom** - `AttributeError: module 'mediapipe' has no attribute
'solutions'` when an `AutoReframe(tracker="face")` or `PunchIn`
(`track_speaker=True`) effect opens.

**Cause** - mediapipe 0.10.30+ removed the legacy
`mp.solutions.face_detection` API. Code (or an old engine build)
written against the legacy API breaks on the newer wheel.

**Fix** - The current `AutoReframe` / `PunchIn` plugins use the modern
`mediapipe.tasks.python.vision.FaceDetector` API (BlazeFace short-range
tflite, auto-downloaded to `~/.cache/mp_face_detector.tflite`), so
upgrading eks-harness resolves it. In custom code, migrate to the `tasks`
Vision API. `pip install eks-harness[ml]` pulls a compatible mediapipe.

## `torchvision` / `torch` ROCm version mismatch

**Symptom** - Loading `MatAnyoneRemove` (or any torch path) fails with
a torchvision/torch C-extension mismatch, e.g. `RuntimeError: Couldn't
load custom C++ ops` or an ABI/`undefined symbol` error, on AMD ROCm.

**Cause** - `torchvision` must be built against the exact `torch`
build. On Windows + AMD ROCm the pytorch.org wheels do not match the
ROCm `torch` you need.

**Fix** - Install both from AMD's repo, not pytorch.org. Use the
matching ROCm wheel, e.g.
`https://repo.radeon.com/rocm/windows/rocm-rel-<ver>/torchvision-<ver>+rocm<ver>-cp312-cp312-win_amd64.whl`,
and the corresponding `torch` wheel from the same `rocm-rel-<ver>`
directory. Also set
`TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1` to enable flash /
memory-efficient attention on ROCm.

## `SAM3Track` / `MatAnyoneRemove` first run is extremely slow

**Symptom** - The first render using `SAM3Track` or `MatAnyoneRemove`
appears to hang for minutes before any frame completes; later runs are
much faster.

**Cause** - These backends compile triton kernels on first inference;
the compile output is cached, so subsequent runs reuse it.

**Fix** - Wait it out the first time (it is compiling, not hung). The
cache persists across runs, so only the first inference pays the cost.

Note on ROCm-on-Windows specifically: the GPU process can leak on exit,
leaving a wedged Python process holding the device. If a later run
can't acquire the GPU, kill the stale process via Task Manager (or
reboot) before re-running.

## OpenCV cascade missing when using `FaceMarkers` / `MotionMarkers`

**Symptom** - `FaceMarkers` or `MotionMarkers` produce an empty stream
on every run, with a one-line warning on stderr about a missing
backend. No crash.

**Cause** - Both markers lazy-import `cv2`. When OpenCV is absent, the
marker source degrades to empty rather than raising - the convention
across all the new marker types. The Haar cascade XML that
`FaceMarkers` needs ships *inside* the `opencv-python` wheel, so a
correctly-installed cv2 always has it.

**Fix** - `pip install opencv-python`. Verify with `python -c "import
cv2; print(cv2.data.haarcascades)"` - it should print a path that
exists. If you specifically want the headless wheel for CI, use
`opencv-python-headless`; both ship the cascade.

## `LoudnessNormalize` on a short clip clips the peaks

**Symptom** - Output is louder than the configured `lufs` target,
hard-clipped on peaks, only on clips shorter than ~3 seconds.

**Cause** - ffmpeg's `loudnorm` (the implementation behind
`LoudnessNormalize`) does a two-pass measurement by default. Pass one
analyses the input; on a very short clip the integrated-loudness
estimate is noisy and the second pass over-corrects.

**Fix** -

- Set an explicit `true_peak_db` (e.g. `-1.5`) so the limiter still
  catches the peaks even when the loudness estimate is off.
- Or switch the normaliser to single-pass / measure-only mode if your
  delivery target accepts the looser tolerance.
- For very short stings (< 1 s) prefer a peak normaliser or fixed
  `gain_db` adjustment to loudnorm.

## Random renders aren't reproducible

**Symptom** - Two renders of the same project file produce different
visual output where a `Noise` / `RandomChoice` curve is in the chain.

**Cause** - One of the seeds is missing. The renderer combines
`Project.random_seed` with each node's local `seed`; if either is
left at its default-of-zero across renders the project still renders,
but the determinism guarantee only holds when *both* are set.

**Fix** -

- Set `Project.random_seed` to a non-zero integer.
- Audit every `Noise`, `RandomChoice` and `RandomTrigger` node in the
  IR and give each an explicit `seed`. The validator does not require
  this - it is a deliberate choice so quick experiments stay
  ergonomic - but reproducibility does.
- Pin marker backends too (`BeatTracker(backend="madmom")` etc.):
  random determinism does not help if the marker stream itself drifts
  between hosts.

## Captions are slow / I want GPU STT

**Symptom** - Final-render caption transcription is the slowest step,
pinning a CPU core for minutes on long talking-head clips.

**Cause** - The default `faster-whisper` backend is CPU-only on Windows
AMD (CTranslate2 has no AMD-GPU path there).

**Fix** - Use the GPU-accelerated `winml-whisper` backend:

```python
STTMarkers(name="speech", source="tracks[0].segments[0]",
           backend="winml-whisper")
```

It runs ONNX Whisper through ONNX Runtime with the WinML MIGraphX EP
(AMD GPU). Install `pip install eks-harness[captions-onnx]` plus
`eks-harness[winml]`. `backend="auto"` already selects it first on Windows.
If it silently runs on CPU, update the Adrenalin driver (see the next
entry). Note word timings are approximated (no DTW); for tight word
alignment keep `faster-whisper` / `whisperx`.

## MIGraphX EP fails with Error 1114

**Symptom** - On first ONNX inference (`winml-whisper`, rembg/BiRefNet
matting) a warning reports the MIGraphX execution provider DLL failed
to initialise with `Error 1114` (`ERROR_DLL_INIT_FAILED`); the run then
proceeds on CPU.

**Cause** - The Adrenalin GPU driver is too old for the MIGraphX EP.

**Fix** - Update to a recent Adrenalin driver (≥ 25.10). The MIGraphX EP
registers via Windows ML's `ExecutionProviderCatalog`; until the driver
is current it transparently falls back to CPU, so renders still
succeed, just slower. Re-run `eks-harness video doctor` to confirm the EP is
listed after updating.

## Renders aren't using the GPU encoder

**Symptom** - Renders are slower than expected and ffmpeg logs show
`libx264` even though the host has an AMD/NVIDIA/Intel GPU.

**Cause** - `EKS_HARNESS_HWACCEL=0` is set (forcing software libx264 + no
`-hwaccel`), or the HW encoder isn't present in the ffmpeg build.

**Fix** -

- Unset the kill switch: `EKS_HARNESS_HWACCEL` must not be `0` for the
  auto-probe to pick a HW encoder. Leave `RenderSettings.encoder` unset
  so `eks_harness.video.render.hwaccel` selects AMF/NVENC/QSV/VideoToolbox.
- Confirm the encoder exists in your ffmpeg: `ffmpeg -encoders | grep amf`
  (or `nvenc` / `qsv`). If it's missing, install a full ffmpeg build
  with the HW encoders compiled in.
- Keep `EKS_HARNESS_HWACCEL=0` only when you deliberately want reproducible,
  byte-identical cross-machine output.

## `CUDAExecutionProvider` not available warning

**Symptom** - ONNX Runtime logs `CUDAExecutionProvider is not in
available provider names` on an AMD host.

**Cause** - Expected on AMD - there is no CUDA. The ONNX path uses the
WinML MIGraphX EP (AMD GPU) and auto-falls-back to CPU, never CUDA or
DirectML (DirectML OOMs the big models on shared-VRAM APUs).

**Fix** - None needed; the warning is benign and the provider list
auto-falls-back. To get the GPU path, ensure `eks-harness[winml]` is
installed and the Adrenalin driver is current (see the Error 1114
entry). On AMD ROCm torch paths, install torch/torchvision from
`repo.radeon.com` (not pytorch.org) and set
`TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1` for flash attention.

## AV1 render is glacial

**Symptom** - `RenderSettings(encoder="libsvtav1")` produces a render
that runs ~10× longer than the libx264 baseline at the same
resolution.

**Cause** - Software AV1 encoding is fundamentally slower than
libx264; SVT-AV1's default preset is tuned for quality rather than
throughput. The auto-probe still picks libx264 for this reason -
`libsvtav1` is only selected when the user explicitly opts in.

**Fix** -

- Bump `RenderSettings.preset` to `10`–`12` (SVT-AV1 presets range 0
  slowest / highest quality to 13 fastest). `preset=10` is roughly
  comparable to libx264's `medium` in encode time and still produces
  smaller files at equivalent quality.
- For previews, stick with libx264; reserve libsvtav1 for the final
  delivery pass.
- If hardware AV1 is available (NVIDIA Ada / Intel ARC / Apple
  Silicon M3+), the appropriate hardware encoder will be much faster
  - `eks-harness video doctor` lists what the host actually has.
