# Video review

Two outputs let an agent judge a recording or a render without opening frames one by one: a contact
sheet (one image) and machine checks against an expected timeline (a pass/fail table).

## Surfaces

| Surface | Contact sheet | Checks |
| --- | --- | --- |
| CLI | `eks-harness video sheet SOURCE` | `eks-harness video check SOURCE EXPECTATIONS` |
| HTTP | `POST /api/artifacts/{id}/sheet` | `POST /api/artifacts/{id}/checks` |
| MCP | `video_sheet` (returns the images too) | `video_check` |
| Python | `eks_harness.review.make_sheets` | `eks_harness.review.run_checks` |

`SOURCE` is a video artifact id or a local file. A local file is uploaded first when `--sid` or
`--project` (and `--session`) is given; `--local` works on the file only and stores nothing.
`GET /api/review/checks` lists the check kinds.

### HTTP

`POST /api/artifacts/{id}/sheet`

```json
{"markers": {"beats": [0.5], "downbeats": [2.0], "cues": [{"t": 3.1, "label": "stamp"}]},
 "frames": 40, "maxEdge": 2000, "replace": true}
```

Response: `video` (the video artifact), `info` (width, height, fps, duration, frames, hasAudio),
`sheets` (artifacts with `url`, `part`, `parts`, `range`, `tiles`: `[{frame, t, cue?}]`) and the parsed
`markers`. `replace` (default true) deletes the video's earlier sheets.

`POST /api/artifacts/{id}/checks`

```json
{"expectations": {"tolerance_frames": 2, "checks": []}, "tree": "/path/to/repo", "replace": true}
```

Response: `ok`, `report` (below), `text` (the plain table), `artifact` (the stored report) and `video`.
Templates are sent inline as `params.template_b64`; the CLI converts `params.template` paths for you.

Both need editor access to the video and run synchronously (seconds for a 30 s clip; OCR adds about a
second per probed frame).

### Python

```python
from eks_harness.review import make_sheets, parse_markers, run_checks, load_expectations

result = make_sheets("render.mp4", out_dir, markers=parse_markers(beatsheet), frames=None)
report = run_checks("render.mp4", load_expectations("expectations.json"))
report.ok, report.as_dict(), report.text()
```

Sheets and checks only read the video and run right away. Driver sessions, recording and recording
encodes run under their device lease and never wait for renders. Video renders wait in the machine's
render queue: one runs at a time (`render.concurrency`), the rest first in, first out
(`eks-harness queue`, which also lists the leases each render took).

## Contact sheet

- Tiles: `frames` evenly spaced frames including the first and the last (default two per second,
  between 12 and 96), plus one highlighted tile per cue or frame marker. A tile label reads
  `#index time-in-seconds frame-number [cue]`; times are the frame's presentation time, so frame
  numbers are exact for variable frame rate recordings too.
- Layout: the long edge stays at or under `maxEdge` (2000). Landscape tiles are 320 px wide (6 per
  row), square 240 px, portrait 170 px; when the tiles do not fit, the video is split into several
  sheets with contiguous time ranges.
- Audio strip: waveform (min/max envelope plus RMS) of the sheet's time range, a time axis, beat
  ticks (short, grey), downbeat ticks (tall, white), cue ticks (amber, labelled), other marker kinds
  (teal), and a small triangle under each tile's time.
- Stored as a `screenshot` artifact (`image/jpeg`) tagged `sheet`, meta
  `{role: "sheet", video, part, parts, range, tiles}`; the video's meta gets `sheets: [ids]`.

### Markers

```json
{
  "beats": [0.5, 1.0],
  "downbeats": [2.0],
  "cues": [{"t": 3.1, "label": "stamp"}],
  "markers": [{"t": 4.2, "kind": "word", "label": "kontor"}],
  "frames": [{"t": 5.0, "label": "brand"}]
}
```

`cues` draw a tick and a highlighted tile, `frames` only a tile, the rest only ticks. A plain list is
also accepted: bare numbers are ticks, items of kind `cue` or `frame` (or with `"frame": true`) add tiles.

## Checks

### Expectations

```json
{"tolerance_frames": 2, "fps": null, "checks": [{"t": 1.0, "kind": "cut", "params": {}, "tolerance_frames": 2, "label": ""}]}
```

A bare list of checks works too. `t` is seconds from the first frame. `fps` overrides the measured
frame rate for the frame deltas. Relative template paths resolve against the expectations file.

### Report

```json
{"ok": false, "video": {"path": "ad.mp4", "fps": 30.0, "frames": 240},
 "counts": {"pass": 2, "fail": 1, "skip": 0, "error": 0}, "elapsedSeconds": 1.9,
 "results": [{"index": 0, "kind": "cut", "label": "scene 2", "status": "pass", "expected": 2.0,
              "measured": 2.0, "deltaFrames": 0, "toleranceFrames": 2, "detail": "", "data": {"frame": 60}}],
 "detected": {"cuts": [2.0, 4.0], "onsets": [0.25]}}
```

`status` is `pass`, `fail`, `skip` (not applicable, for example audio checks on a silent video) or
`error` (bad parameters or an unknown kind; counts as a failure). The stored report artifact is a `log`
(`application/json`) tagged `checks` and `pass` or `fail`; the video's meta gets `checks` and
`checksOk`, and each sheet's meta gets `checks`.

### Kinds

| kind | needs | params (defaults) | measures |
| --- | --- | --- | --- |
| `cut` | `t` | `threshold` 0.1, `ratio` 3.0, `window` | nearest scene cut (frame difference peak over its neighbourhood) |
| `motion` | `t` | `mode` starts or stops, `threshold` 0.004, `min_frames` 3, `window` | first frame where motion starts or stops |
| `text` | `t` | `text`, `box`, `mode`, `until`, `window`, `engine` auto, vision or tesseract, `languages` ["tr-TR", "en-US"] | first frame where the text appears or disappears (OCR, accent and case insensitive) |
| `visual` | `t` | `color` + `color_tolerance` 48 + `min_fraction` 0.25, or `template` / `template_b64` + `threshold` 0.8 + `template_scale` 1; `box`, `mode`, `until`, `window` | first frame where the colour region or the template (normalized cross-correlation) appears or disappears |
| `black` | | `max_frames` 2, `pixel_threshold` 0.1, `picture_ratio` 0.98, `allow` [[a, b]] | black spans longer than `max_frames` |
| `frozen` | | `max_frames` (1 s of frames), `noise` 0.0008, `allow` | frozen spans longer than `max_frames` |
| `safe_area` | | `margins` 0.05 (number, [v, h] or [top, right, bottom, left]; fractions or pixels), `background` auto or a colour, `tolerance` 40, `max_fraction` 0.01, `sample_fps` 4, `window` | sampled frames with content in the margins |
| `loudness` | | `lufs`, `lufs_tolerance` 1, `true_peak_max` -1, `require_audio` | integrated loudness, true peak and range (EBU R128) |
| `onset` | `t` | `sensitivity` 1, `window`, `require_audio` | nearest audio onset (spectral flux, refined on the envelope) |
| `beats` | | `times` (or `beats`), `min_ratio` 0.9, `reach` 0.25, `require_audio` | for each beat, the strongest attack between it and halfway to its neighbours (at most `reach` s) and its frame delta; passes when `min_ratio` of them are within the tolerance and the whole grid is in phase with the attacks (the offset that collects the most attack energy is within the tolerance) |

Presence modes (`text`, `visual`): `appears` (default) and `disappears` search the window (by default
`t` +/- max(1.5 s, 3 tolerances)) coarsely and then bisect to the exact frame; `present` and `absent`
check the frame at `t`, or frames from `t` to `until`. Boxes are `[x, y, w, h]` in source pixels, or
fractions of the frame when every value is at most 1.

`onset` and the detected onsets list normalize the spectral flux by its 99.5th percentile, so a loud
opening does not hide the attacks after it. Dense music (string runs, hi-hats) has attacks between the
beats as loud as the beats themselves: there the grid phase is the reliable measure, and `min_ratio`
around 0.65 still separates a correct grid (about 0.7) from one half a beat off (about 0.25).

### Plugin checks

A plugin adds kinds with a `video_check` contribution whose entry is a class (instantiated with the
plugin context) or a function:

```toml
[[contributes.video_check]]
id = "subtitle-sync"
entry = "acme_checks:SubtitleSync"
```

```python
class SubtitleSync:
    kind = "subtitle_sync"

    def run(self, ctx, item):
        frames = ctx.gray()
        return ctx.result(item, measured=1.25)
```

`ctx` offers `info` (timing), `gray()` (analysis frames), `diffs()`, `frame(n, width)`, `audio(rate)`,
`loudness()`, `window(item)`, `resolve(path)`, `cached(key, make)` and `result(item, ...)`, which
fills the frame delta and pass or fail from the tolerance.
