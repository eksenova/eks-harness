---
name: video-review
description: Review a video (recording or render) without opening frames one by one - build a contact sheet (one image with frame-labelled tiles, highlighted cue frames and the audio waveform with beat and cue ticks) and run machine checks against an expected timeline (cuts, motion, OCR text, logos and colour regions, black or frozen spans, safe area, loudness, onsets and beats). Use after every render or recording you need to judge.
---

# Video review

You cannot watch a video. Look at its contact sheet, and let machine checks measure timing.

## Contact sheet

```bash
eks-harness video sheet <artifact-id> [--markers markers.json] [--frames N] [--at 3.2:stamp]
eks-harness video sheet ./render.mp4 --project acme/ads --session render-12   # uploads the video first
eks-harness video sheet ./render.mp4 --local                                   # local only, stores nothing
```

- One image, long edge at most 2000 px: evenly spaced tiles (2 per second, 12 to 96), each labelled
  `#index time frame`, plus highlighted tiles at cues and `--at` times, and an audio strip with
  waveform, beat (short), downbeat (tall) and cue (labelled) ticks on the same time axis. Long videos
  split into several sheets, each covering a contiguous time range.
- Stored as a `screenshot` artifact tagged `sheet` next to the video. The command prints the page link
  and a local copy: Read the local copy. Give people the page link, not the raw link.
- Markers JSON: `{"beats": [s], "downbeats": [s], "cues": [{"t", "label"}], "markers": [{"t", "kind",
  "label"}], "frames": [{"t", "label"}]}`, or a list of markers. Cues and frames become highlighted tiles.
- Flow recordings get a sheet automatically; the flow report prints `contact sheet` (local) and
  `contact sheet page`.

## Checks

```bash
eks-harness video check <artifact-id> expectations.json [--json]
eks-harness video check ./render.mp4 expectations.json --local
eks-harness video check --list
```

Exit code 0 when every check passes, 1 when one fails. Read the table (or `--json`) instead of frames.
The report is stored as a `log` artifact tagged `checks` next to the video, and the video, its sheets
and the report link to each other on their artifact pages.

```json
{"tolerance_frames": 2, "checks": [
  {"t": 2.0, "kind": "cut", "label": "scene 2"},
  {"t": 4.0, "kind": "motion", "params": {"mode": "stops"}},
  {"t": 5.0, "kind": "text", "params": {"text": "Kontör", "box": [0, 0.6, 1, 0.4]}},
  {"t": 5.5, "kind": "visual", "params": {"color": "#d32f2f", "box": [900, 80, 220, 120]}},
  {"t": 6.0, "kind": "visual", "params": {"template": "logo.png", "mode": "present"}},
  {"kind": "black", "params": {"max_frames": 2, "allow": [[0, 0.5]]}},
  {"kind": "frozen", "params": {"max_frames": 45}},
  {"kind": "safe_area", "params": {"margins": 0.05}},
  {"kind": "loudness", "params": {"lufs": -14, "lufs_tolerance": 1, "true_peak_max": -1}},
  {"t": 0.25, "kind": "onset"},
  {"kind": "beats", "params": {"times": [0.25, 0.75, 1.25], "min_ratio": 0.9}}
]}
```

- Timed checks report expected, measured and the delta in frames; they pass within
  `tolerance_frames`. Presence checks (`text`, `visual`) take `mode`: `appears` (default),
  `disappears`, `present` or `absent` (at `t`, or from `t` to `until`), and an optional `window`.
- Boxes are `[x, y, w, h]` in source pixels, or fractions when every value is at most 1.
- OCR is local: macOS Vision when available, otherwise tesseract (`engine` picks one).
- Audio checks skip a video without audio unless `require_audio` is true.
- `beats` proves music sync: each beat's strongest attack (up to halfway to its neighbours) must be
  on the beat, and the whole grid must be in phase with the attacks (`phaseFrames` in the result). For
  dense music (string runs, hi-hats) use `min_ratio` about 0.65; the phase still catches a grid that
  is off by an eighth. Text, logo and stamp rows only prove the video follows the beat sheet, not
  that the beat sheet follows the music: always add a `beats` row.
- Repos and plugins add check kinds with a `video_check` contribution (a class or function with
  `kind` and `run(ctx, item)`).

Details and every parameter: `docs/video-review.md` in the eks-harness repo.
