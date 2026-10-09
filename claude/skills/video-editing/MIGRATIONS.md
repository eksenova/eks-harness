# eks-harness video schema migrations

`Project.schema_version` is currently `"0.1"`. The field is on the
`Project` model with a default value, so projects that omit it are
treated as `"0.1"` and the renderer does not require a version bump
for every release.

This file documents the forward-compatibility policy and shows the
shape future migrations will take. For the canonical history of every
change ever made to the IR, see the repository `CHANGELOG.md`.

## Forward-compatibility policy

The IR follows these rules so that older `project.json` files keep
loading against newer SDKs without modification:

1. **New fields are added with defaults.** Anything added to an
   existing model - `Project.random_seed`, `AudioSegment.ducking`,
   `Segment.fade` - is optional, has a sensible default, and does not
   change behaviour for projects that don't set it.
2. **New effect / marker / curve / transition kinds are non-breaking.**
   Each is a separate type joining the relevant discriminated union (or
   adding a new optional field on a parent model). Projects that don't
   reference the new kind are unaffected; projects that *do* require
   the SDK version that introduced them.
3. **Renames and removals are breaking.** They wait for a
   `schema_version` bump and a migration script (see below). Renames
   are kept on the deprecation list for at least one minor release with
   a pydantic validator that accepts both names.
4. **Field semantic changes are breaking.** Changing the meaning of an
   existing field (e.g. switching `lufs` from integrated to short-term)
   requires a `schema_version` bump.

The validator does not currently *enforce* `schema_version`; it is
informational until the first breaking change requires it.

## Added in this release

Surface area introduced in the current release. See `CHANGELOG.md` for
the version this lands in and `API_REFERENCE.md` for type-by-type
field documentation.

**Project root**

- `Project.random_seed: int = 0` - global salt for reproducible
  randomness.

**Visual effects (filters)** - `Blur`, `Sharpen`, `Vignette`,
`FilmGrain`, `LUT`, `Invert`, `Posterize`.

**Visual effects (overlays)** - `TextOverlay`, `LowerThird`,
`Watermark`, `StickerOverlay`.

**Audio effects** - `Ducking` (segment), `LoudnessNormalize` (track),
`AudioFade` (segment), `EQEffect` + `EQBand` (track), `Reverb`
(segment). Attached as typed optional fields on `AudioSegment` /
`AudioTrack`, not in a discriminated union.

**Curves** (valid in any `Animated[float]` slot) - `Spring`, `Noise`,
`RandomChoice`, `Step`, `Bezier`, `Bounce`.

**Markers** - `SilenceMarkers`, `OnsetMarkers`, `EnergyMarkers`,
`FaceMarkers`, `MotionMarkers`.

**Transitions** - `DipToBlack`, `DipToWhite`, `Wipe`, `Slide`, `Push`
(IR-only for now, same posture as `Crossfade`).

**Randomness primitives** - `RandomTrigger`, `EveryNth` (round-trip,
not yet wired into any effect field - park them under
`Project.metadata`).

**Encoders** - `libsvtav1` (software AV1) selectable via
`RenderSettings(encoder="libsvtav1")`. The auto-probe still prefers
`libx264`.

**CLI** - `eks-harness video doctor`, `eks-harness video init <template> <dest>`.

**MCP tools** - `validate_project`, `inspect_project`,
`extract_markers`, `list_effects`, `add_segment`, `add_effect`,
`set_property`.

All of the above are additive. Projects authored against the previous
release continue to load and validate unchanged.

## 0.2.x → next: `SAMTrack` / `BackgroundRemove` removal

The next release removes two effect IRs and replaces them with
three SOTA equivalents. The new types are not drop-in aliases - the
old `kind` discriminators (`sam_track`, `background_remove`) are gone
and the loader rejects them with a clear error.

**Removed**

- `SAMTrack` - was a `NotImplementedError` stub. Replaced by
  `SAM3Track` (Meta SAM 3, Nov 2025; open-vocabulary text prompts).
  Field names match 1:1, so the rewrite is a literal rename plus an
  optional `confidence_threshold`:

  ```diff
  - SAMTrack(prompt="person", output="alpha")
  + SAM3Track(prompt="person", output="matte")
  ```

  `output` widens from `Literal["mask", "alpha"]` to
  `Literal["mask", "matte", "highlight"]`. `"alpha"` had no working
  renderer in `SAMTrack`; the closest semantic in `SAM3Track` is
  `"matte"`. Pick `"mask"` for binary, `"matte"` for soft alpha,
  `"highlight"` to draw an outline without cutting the subject out.

- `BackgroundRemove` - was a birefnet / u2net stub. Splits into
  `MatAnyoneRemove` (CVPR 2026 video matting, temporally consistent,
  SOTA on hair / edge) and `BiRefNetRemove` (per-frame image matting,
  no temporal smoothing).

  ```diff
  - BackgroundRemove(model="birefnet")
  + BiRefNetRemove()
  ```

  Or, for video where temporal consistency matters:

  ```diff
  - BackgroundRemove(model="birefnet", temporal_smoothing=0.3)
  + MatAnyoneRemove()
  ```

  The `temporal_smoothing` knob is gone - `MatAnyoneRemove` learned a
  better temporal model than the post-hoc smoothing pass.

**New optional extras**

The replacements all sit behind lazy-imported optional extras so the
base install stays light:

| IR | Extra |
|---|---|
| `SAM3Track` | `eks-harness[sam3]` |
| `MatAnyoneRemove`, `BiRefNetRemove` | `eks-harness[matting]` |
| `SeparatedStem` | `eks-harness[stems]` |
| `DeepFilterDenoise` | `eks-harness[denoise]` |
| `HTMLOverlay` | `eks-harness[html]` + `playwright install chromium` |

Using one of these IRs without the extra raises
`RuntimeError("X requires pip install eks-harness[Y]")` at render time.
Validation still passes - the backend is lazy-imported.

**Migration script shape**

A `0.2_to_next.py` migration ships under `tooling/migrations/` that
performs the rewrite by walking every `Segment.effects` list:

```python
RENAMES = {
    "sam_track": ("sam3_track", {"alpha": "matte"}),  # output value remap
}

REPLACE_BACKGROUND_REMOVE = {
    # video-flagged segments → matanyone; everything else → birefnet
}
```

Run it once per project file; it is idempotent.

## How to handle a future schema bump

When a breaking change ships, the new SDK accepts the old
`schema_version` for one minor release behind a migration. The shape
of a migration script:

```python
"""Migrate project.json from schema 0.1 to 0.2.

Run once per project file. Idempotent: re-running on an already-
migrated project is a no-op.
"""

from __future__ import annotations

import json
from pathlib import Path

from eks_harness.video import Project

_FROM = "0.1"
_TO = "0.2"


def migrate(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))

    version = raw.get("schema_version", "0.1")
    if version == _TO:
        return
    if version != _FROM:
        raise ValueError(f"unexpected schema_version: {version!r}")

    # Apply the version-specific transform. Example: a hypothetical
    # rename of ``ColorGrade.lut_path`` to ``ColorGrade.lut``.
    for track in raw.get("tracks", []):
        for segment in track.get("segments", []):
            for effect in segment.get("effects", []):
                if effect.get("kind") == "color_grade" and "lut_path" in effect:
                    effect["lut"] = effect.pop("lut_path")

    raw["schema_version"] = _TO

    # Re-validate against the current SDK before writing - a migration
    # that produces an invalid project is a bug, not a silent corruption.
    Project.model_validate(raw)

    path.write_text(
        json.dumps(raw, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    import sys

    migrate(Path(sys.argv[1]))
```

The important invariants:

- Read the version off the file, not off the SDK; the file is the
  source of truth for what shape it has on disk.
- Make the transform idempotent so the script is safe to re-run.
- Always `Project.model_validate(...)` before writing - the
  round-trip catches transforms that produce invalid IR.
- Bump `schema_version` *after* the transform succeeds, not before.

The repository will ship one such script per breaking version bump
under `tooling/migrations/<from>_to_<to>.py` when the time comes.
