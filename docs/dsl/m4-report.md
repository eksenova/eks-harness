# M4 report: media

Contract: `contracts-m4.md`. Status: done, with the gaps listed under "Not done". The exit check passed on
2026-10-14: all 18 cookbook edits render byte-identical twice with the cache off on a temp hub, and a fast subset does
the same in the test suite.

## What landed

| Area | Where | Notes |
| --- | --- | --- |
| Run-time clock | `engine/clock.py`, `ExecContext.time()` | beat grid from a static tempo or the `media.markers` tempo source, markers, words, cues; only the providers a node depends on |
| Media files | `ExecContext.media()`, `media/files.py` | content resolver, ffprobe, bitexact ffmpeg flags, ingestion |
| Animated values | `media/animate.py` | IR semantics for keys, easings (new: quart, sine, expo families), every curve kind, clip-local times and time sets |
| Fonts (BL-003) | `media/fonts.py` | shipped families by weight, font files, `font_unavailable` otherwise |
| Media sources and analyses | `ops/media.py` | `media.source`, `media.markers` (legacy analysers, resolution records), `media.stems` |
| Audio mix | `media/audio/`, `ops/audio.py` | numpy mix graph: EQ, compressor, ducks, fades, seeded reverb, pan, BS.1770-4 loudness, 4x true-peak limiter |
| Motion graphics | `media/gfx/`, `ops/gfx.py` | Skia renderer in a child process; CPU reference and GPU (Skia GL on CGL, EGL on Linux); gradients, mask modes, 17 blend modes, SVG images, kerning |
| Edit render | `media/edit/`, `ops/edit.py` | new frame compositor: timing (speed, `speed_ramp`, `freeze`), sources, 32 effects, 22 transitions, Skia compositing, video overlays, blur, emphasis, clip audio, codec-aware true-peak limiting |
| Overlay renderers | `media/overlays/`, `ops/overlay.py` | box maps from web, app and native surfaces; strict target resolution on box maps; screenshot renderer (`overlay.annotate`); video overlay renderer |
| Media checks | `ops/check.py`, `review/` | every IR check kind through the review checks with explicit adapters, OCR resolution |
| Derived review | `eks/editing.py` | `e.expect.derive(cuts=, captions=, beats=, loudness=)` |
| Cookbook | `examples/cookbook/.harness/` | 18 edits, 2 gfx scenes, deterministic fixture generator |

## Decisions taken on the way

* The legacy orchestrator could not express clip transforms, and its time model was where the listed bugs lived, so
  `edit.render` is a new compositor (contract 7). Only the five machine-learning effects bridge to the legacy plugins.
  The legacy orchestrator remains for the legacy video API until M7.
* skia-python does not expose a Metal backend context; the GPU path is Skia GL on an offscreen CGL context.
* ffmpeg's AAC encoder overshoots true peak by up to 2.8 dB on sharp transients. The edit render measures the encoded
  audio and re-limits the bus until the encoded true peak stays at the mix's own peak.
* A node's clock only reads the tempo source when the node depends on it (the audio mix no longer waits for beat
  analysis it does not use).
* `overlay.annotate` takes its source as a JSON ref, so a checkpoint (its first shot) works as well as a shot.
* The cookbook fixtures needed a clean synthetic beat (librosa read the first version at 60 bpm).

Behaviour choices from the agents' parts (worth knowing): `fade` fades the clip to transparent; `datamosh` is
stateless (seeded per 30-frame group) so output does not depend on frame order; text effects use weight 400; pan
leaves uncovered pixels transparent; gfx positions are relative to the frame centre; a pulse jumps to its amount on
the trigger frame and eases back.

## Exit check

| Edits | Result |
| --- | --- |
| beat_flash_montage, before_after_split, lyric_video, product_showcase, reaction_overlay, slideshow_kenburns, speed_ramp_drop, storytelling_bg_music, podcast_clip, annotated_walkthrough, titles, mix_only | passed twice, equal SHA-256 |
| auto_reframe_vertical, captioned_tutorial, talking_head_meme, chapter_markers_youtube, sponsor_skipper, multicam_switching | passed twice, equal SHA-256, with faster-whisper, mediapipe and scenedetect installed into the worktree venv for the run |

Contact sheets of the annotated walkthrough, the podcast clip and the lyric video were inspected: titles, callouts,
highlight, arrow, redaction, step badges, captions, the gfx ring pulse and the colour split appear where planned.

## Tests

* New: `tests/media/` (compositor 26, effects 54, audio and media ops 28, gfx 35, overlays 12, checks 28, cookbook 6),
  `tests/eks/test_edit_derive.py` (12).
* Updated: engine tests that relied on media ops having no executor now force the fake explicitly or use real clips.
* JS: kit 17, web-driver 4, mobile-driver 11, react-native SDK 23; typechecks clean.

## Not done

* `media.tts`: the codebase has no speech synthesis integration, so the op fails with `resolution_unavailable` and
  says so. A kokoro integration (new code and a new extra) is needed.
* `media.stems`, and `stems`/`denoise` in the mix: wired to the legacy Demucs and DeepFilterNet code but not run here
  (packages not installed).
* `master.lra` is refused (no loudness-range processing).
* The analysis packages for the ML recipes (faster-whisper, mediapipe, scenedetect) are not in the lockfile; the
  `ml` extra lists most of them but also madmom and BeatNet, which do not install on Python 3.13 here.
* Overlay styles use fixed pixel sizes, so overlay text is small on 720-wide frames.
* The Linux EGL GPU path is written but has never run.
* gfx triggers on scene events fail with `time_unresolved`: no executor publishes event times yet (M5 web and Blender
  scenes).
* Media check regions other than `by.rect` need a box map, which renders do not have.
