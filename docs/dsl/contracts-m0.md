# eks M0 contracts

Status: normative for milestone M0 (authoring side only). Source of truth above this file:
`docs/dsl/design.md`. Choices the design left open are recorded in `docs/dsl/decisions-m0.md`.

The skeleton modules listed in section 1 are part of this contract: every class, field, default, signature
and op registration in them is normative. This document defines the semantics, grammars, algorithms and
rules the skeleton cannot express. Where a skeleton body raises `NotImplementedError`, the owning package
implements it exactly as specified here. If a contract is genuinely wrong, fix it in this file and in the
code in the same change and say so in the package result.

M0 is done when every example plan of design.md builds and validates (section 15).

## 0. Ground rules for every package

* Python 3.13, pydantic 2.14 (as locked). Use PEP 695 type parameters for new generics (`class Ref[T]:`);
  ruff asks for it (UP046/UP047).
* Lint: `uv run ruff check <paths>` must be clean. There is no ruff config in the repo, so ruff 0.16's
  default rule set applies (about 800 rules: isort `I001`, sorted `__all__` `RUF022`, `PYI041`, `RUF009`,
  `TRY004`, `B010`, `BLE001`, bandit `S*`, `SIM*`, `UP*`, `FURB*` and more). Tests are linted too
  (`S101` is not reported for tests).
* Types: `npx --yes pyright@1.1.390 --pythonpath .venv/bin/python <paths>` must report 0 errors.
  Pydantic models that are subclassed with a narrowed `kind: Literal[...]` use the class keyword form:
  the base is `class X(BaseModel, frozen=True, extra="forbid")` and every subclass repeats
  `frozen=True` (`class Y(X, frozen=True)`), otherwise pyright reports the override.
* No comments and no docstrings in non-test code. Human text that the catalog or schema needs goes into
  `description=` arguments. Comments are fine in tests.
* Never use the em dash character. Never commit, push or create branches.
* Exceptions: use the classes the skeleton declares (`TimeError`, `TargetError`, `RefError`,
  `RefUsageError`, `RefTypeError`, `TraceError`, `PlanBuildError`, `PlanError`, `RegistryError`,
  `DiscoveryError`). Messages name the offending value and say what to write instead. Pydantic validators
  raise `ValueError` (never `TypeError`, which pydantic does not convert).
* Import safety: `import eks` and `eks_harness.ir.registry.default_registry()` must keep working at every
  point in time, for every package. Code that runs at import (decorators, registrations, `Poly`, the
  pydantic schema hooks of `Time`, the `Session` constructor, `open_flow_session`, the CLI parsers) is
  already implemented in the skeleton; keep it working when you touch it.
* File ownership is disjoint (section 16). Do not edit files of other packages. If you need something from
  another package that is not implemented yet, code against its contract; never add a fallback.
* Tests live in `tests/eks/` (no `__init__.py`, no `conftest.py` there: a package named `eks` would
  shadow `src/eks`, and a second `conftest` module breaks `from conftest import ...` in the existing
  tests). The autouse fixture `eks_registry` in `tests/conftest.py` snapshots the registry before each test
  under `tests/eks` and restores it after, so tests may register ops, flows and resources freely; request
  it by name to get the `Registry`. Test file basenames must be unique across all of `tests/` (pytest's
  prepend import mode), so never reuse an existing name such as `test_score.py`.
  Run: `cd <worktree> && EKS_HARNESS_SKIP_WEB=1 uv run pytest -q -p no:cacheprovider tests/eks/...`.

## 1. Packages, layering and module map

`eks_harness.ir` is the dependency-free bottom layer: every data definition (Time, Target, Ref encodings,
plan models, op argument models, catalogs, presets, the registry store and the registration decorators).
It never imports `eks`. `eks` is the authoring SDK: Ref proxies, tracing, control flow, builders, planning
and discovery; it re-exports the public names of `eks_harness.ir`. `eks_harness.engine.validator` holds
the M0 validator. The CLI lives in `eks_harness.cli`.

| Module | Role | Owner |
| --- | --- | --- |
| `eks_harness/ir/__init__.py` | `SCHEMA_ID` only | P3 |
| `eks_harness/ir/time.py` | ticks, `Time`, `Anchor`, grammar, `Span`, `TimeSet`, tempo map, `TimeContext`, `t` | P1 |
| `eks_harness/ir/target.py` | `Target`, built-in target kinds, shorthand, capability tables, `by` | P2 |
| `eks_harness/ir/refs.py` | `$ref`, `$item`, `$fmt` encodings, path steps, `R*` field aliases, walkers | P3 |
| `eks_harness/ir/types.py` | RefType grammar and inference, `Artifact`, `MediaInfo`, `Color`, media URIs | P3 |
| `eks_harness/ir/plan.py` | `Plan`, `Node`, `Resource`, `Clock`, `SessionSpec`, `FunctionSource` | P3 |
| `eks_harness/ir/hashing.py` | canonical JSON, Merkle hashing, stable ids, derived seeds | P3 |
| `eks_harness/ir/registry.py` | spec types, `Registry`, decorators, `Poly`, provenance, builtins loader | P5 |
| `eks_harness/ir/entries.py` | `flow`/`seed`/`edit`/`scene`/`score` decorators and option models | P5 |
| `eks_harness/ir/presets.py` | preset models and built-in presets | P5 |
| `eks_harness/ir/ops/__init__.py` | imports every built-in family module | P5 |
| `eks_harness/ir/ops/ctl.py` | control op models | P4 |
| `eks_harness/ir/ops/{ui,web,mobile}.py` | driving op models | P6 |
| `eks_harness/ir/ops/{capture,overlay,check,debug}.py` | evidence op models, overlay model, check kinds | P7 |
| `eks_harness/ir/ops/{api,net,inject,fake,data}.py` | environment op models, `Resource` base | P8 |
| `eks_harness/ir/ops/{media,edit,audio}.py`, `eks_harness/ir/catalog/*` | media, timeline, mix, catalogs | P9 |
| `eks_harness/ir/ops/{gfx,blender,scene}.py` | scene graphs | P10 |
| `eks_harness/ir/ops/score.py` | take container | P12 |
| `eks_harness/ir/schema.py` | JSON Schema export | P13 |
| `eks_harness/engine/{__init__,validator}.py`, `eks/validation.py` | validator | P11 |
| `eks/time.py`, `eks/target.py` | re-exports | P1, P2 |
| `eks/ref.py` | `Ref` proxy, sentinels, `encode` | P3 |
| `eks/tracing.py`, `eks/control.py`, `eks/planning.py` | tracing, control flow, `plan()` | P4 |
| `eks/registry.py`, `eks/discovery.py`, `src/eks_harness/plugins/manifest.py` | re-exports, discovery, plugin modules | P5 |
| `eks/workspace.py` | `plan_spec` (the CLI path) and `seed_personas` | P11 |
| `eks/session.py`, `eks/ui.py`, `eks/web.py`, `eks/mobile.py` | session surface | P6 |
| `eks/assertions.py`, `eks/capture.py`, `eks/overlay.py`, `eks/checks.py`, `eks/debug.py` | evidence surface | P7 |
| `eks/api.py`, `eks/net.py`, `eks/inject.py`, `eks/fake.py`, `eks/data.py` | environment surface | P8 |
| `eks/media.py`, `eks/effects.py`, `eks/editing.py`, `eks/audio.py` | media surface | P9 |
| `eks/gfx.py`, `eks/blender.py`, `eks/webscene.py`, `eks/scenes.py` | scene surface | P10 |
| `eks/scoring.py` | score surface | P12 |
| `eks/__init__.py`, `eks/py.typed`, `eks_harness/cli/{plan,ops,schema}_cmds.py` | public API, CLI | P13 |

Import rules:

* `eks_harness.ir.*` never imports `eks`.
* Inside `eks`, import submodules by module path. No module of `eks` may share its name with an exported
  name of `eks` (hence `editing`, `scoring`, `assertions`, `effects`); `eks.overlay`, `eks.checks` and
  `eks.media` (`source`, `markers` and the marker models `BeatTracker`, `STTMarkers`, `SceneMarkers`, ...)
  are exported as modules on purpose.
* Built-ins are loaded by `eks_harness.ir.registry.ensure_builtins()`, which registers the built-in target
  kinds and imports `eks_harness.ir.catalog`, `eks_harness.ir.ops` and `eks_harness.ir.presets` (with no
  active registry, so built-ins always land in the process registry `REGISTRY`). Every lookup goes through
  `default_registry()`, which calls `ensure_builtins()` first and returns the active registry
  (`using_registry(registry)`, a context variable) or `REGISTRY` when none is active. The registration
  decorators write to `registry_for(provenance)`: `REGISTRY` for built-in (`eks`) provenance, else the active
  registry, else `REGISTRY` (section 9.3 for the per-tree registries).

Public API of `import eks` (fixed by `src/eks/__init__.py`): `Args`, `Artifact`, `BlenderScene`,
`CurveBase`, `Data`, `EasingBase`, `Edit`, `EffectBase`, `GfxScene`, `Json`, `JsonLike`, `MarkerBase`, `Media`, `NetProfile`,
`OverlayBase`, `OverlayStyle`, `Pace`, `Persona`, `PersonaPreset`, `Plan`, `PresetModel`, `Ref`, `Resource`,
`ResourceRef`, `Score`, `Session`, `Span`, `Target`, `Time`, `TimeSet`, `TransitionBase`, `Viewport`,
`WebScene`, `annotate`, `app`, `backend`, `backend_policy`, `by`, `check`, `checks`, `curve`, `curves`, `dynamic`,
`each`, `ease`, `easing`, `edit`, `effect`, `expect`, `flow`, `fx`, `keys`, `macro`, `marker`, `media`, `op`,
`otherwise`, `overlay`, `overlay_kind`, `parallel`, `plan`, `preset`, `refers_to`, `renderer`, `requires`, `resource`, `scene`,
`score`, `seed`, `t`, `target_kind`, `transition`, `tx`, `validate`, `when`. The catalog bases, the preset
models and the resource reference type are what extensions subclass or annotate (design 7 and 11), so
plugins need no import from the IR package. `refers_to(kind)` is the `RefKind("resource:<kind>")` metadata that
narrows a reference field to one resource kind: `Annotated[eks.ResourceRef, eks.refers_to("federation")]`
(a bare `eks.ResourceRef` accepts a handle of any kind). JsonLike (Json, Ref, Encodable, or mappings and sequences of
JsonLike) types every JSON-valued authoring parameter (expect values, dispatched actions, eval and
invoke arguments, route patches and bodies, scene props and event data, keyframe values, fake data).

## 2. Time (P1, `eks_harness/ir/time.py`)

### 2.1 Unit

* `TICKS_PER_SECOND = 705_600_000` (= 2^9 * 3^2 * 5^5 * 7^2), `TICKS_PER_MS = 705_600`. All resolved times
  are Python `int` ticks.
* A decimal quantity of seconds or milliseconds is accepted when it is a whole number of ticks (always the
  case with at most 5 decimals of seconds or 2 of milliseconds); otherwise `TimeError` ("finer than one
  tick").
* `ticks_per_frame(fps)` returns `TICKS_PER_SECOND / fps` and raises `TimeError` unless that is an
  integer. That holds for 24, 25, 30, 48, 50, 60, 120 and the NTSC rates 24000/1001, 30000/1001,
  60000/1001.
* `Fps` is a `Fraction`. JSON: an integer (`30`) or `"num/den"` (`"30000/1001"`); `parse_fps` accepts int,
  `Fraction`, those strings, and rejects floats, decimals, zero, negatives and fps for which
  `ticks_per_frame` fails (raise `ValueError` from the pydantic validator path, `TimeError` subclass is
  fine since it is a `ValueError`).

### 2.2 Value model

`Time` is a frozen, slotted dataclass `(anchor, bars, beats, frames, ticks)`:

| Field | Meaning |
| --- | --- |
| `anchor: Anchor or None` | a symbolic point, at most one per time |
| `bars: Fraction` | offset in bar space (unit `bar`) |
| `beats: Fraction` | offset in beat space (unit `b`) |
| `frames: Fraction` | offset in frames (unit `f`), needs fps |
| `ticks: int` | offset in ticks (units `s`, `ms`, `tk`) |

`bars`, `beats` and `frames` always have denominators whose only prime factors are 2 and 5 (they print as
terminating decimals). Constructors that would break this raise `TimeError`.

`Anchor(kind, index, name, text, source, nth)`:

| kind | grammar | fields used |
| --- | --- | --- |
| `start` | `start` | none |
| `end` | `end` | none |
| `frame` | `f:N` | `index` = N (integer, absolute frame) |
| `beat` | `beat:X` | `index` = X (decimal beat index, 0-based) |
| `downbeat` | `downbeat:X` | `index` = X (decimal downbeat index, 0-based) |
| `bar` | `bar:X` | `index` = X (decimal bar index, 0-based, time signature aware) |
| `cue` | `cue:NAME` | `name` |
| `marker` | `marker:STREAM[#N]` | `name` = stream, `nth` = N (0-based, default 0) |
| `word` | `word:[SOURCE:]'TEXT'[#N]` or `word:[SOURCE:]#I` | `text` + `nth`, or `index` = I; `source` |

`is_concrete` is true iff there is no anchor and `bars == beats == frames == 0`.

### 2.3 Grammar

```text
time      ::= ws? head ( ws? sign ws? quantity )* ws?
head      ::= anchor | sign? ws? quantity
anchor    ::= "start" | "end"
            | "f:" uint
            | ( "beat" | "downbeat" | "bar" ) ":" decimal
            | "cue:" name
            | "marker:" name ( "#" uint )?
            | "word:" ( name ":" )? ( quoted ( "#" uint )? | "#" uint )
quantity  ::= decimal unit
unit      ::= "ms" | "s" | "tk" | "f" | "bar" | "b"
decimal   ::= digit+ ( "." digit+ )?
uint      ::= digit+
name      ::= ident ( "-" ident )*
ident     ::= [A-Za-z_] [A-Za-z0-9_.]*
quoted    ::= "'" ( [^'\\] | "\\" ['\\] )* "'"
sign      ::= "+" | "-"
ws        ::= [ \t]+
```

* Units are matched longest first (`bar` before `b`, `ms` before `s`).
* A hyphen inside a name must be followed by a letter or underscore, so `cue:logo-intro` is one name and
  `cue:logo-2f` is `cue:logo` minus two frames.
* `f:120` is an absolute frame anchor; `120f` is a duration. `bar:4` is an anchor; `4bar` a duration.
* `tk` (ticks) exists so every value has a canonical spelling; authors rarely need it.
* `TIME_PATTERN` in the module is the JSON Schema pattern for this grammar.

Examples (input, then canonical form):

| input | canonical |
| --- | --- |
| `1.5s` | `1.5s` |
| `300ms` | `0.3s` |
| ` 2 s`  | rejected (no space between number and unit) |
| `f:120` | `f:120` |
| `beat:8 + 1b` | `beat:8+1b` |
| `cue:logo - 2f` | `cue:logo-2f` |
| `word:'EksenGOLD'` | `word:'EksenGOLD'` |
| `marker:intro#2` | `marker:intro#2` |
| `-0.5s+1b` | `1b-0.5s` |
| `end-1bar` | `end-1bar` |
| `0ms` | `0s` |

### 2.4 Canonical form (`format_time`)

1. If there is an anchor, write its text first (`marker:x` with `nth` 0 is written without `#0`; a word
   with `nth` 0 without `#0`; quotes and backslashes in word text are escaped).
2. Write the non-zero components in the fixed order bars (`bar`), beats (`b`), frames (`f`), ticks.
   Each component is written with an explicit sign, except the very first token of a time without an
   anchor, which only carries a sign when negative.
3. Ticks are written as seconds when `ticks % 7056 == 0` (at most 5 decimals), else as `<n>tk`.
4. Numbers are plain decimals: no exponent, no trailing zeros, no trailing dot, no leading `+`.
5. The zero time is `0s`.

`parse_time(format_time(x)) == x` for every valid `Time`, and `format_time(parse_time(s))` is idempotent.

### 2.5 Arithmetic and comparison

* `a + b`: at most one operand may carry an anchor (`TimeError` otherwise); components add.
* `a - b`: `b` must not carry an anchor (`TimeError`); components subtract.
* `-a` only for unanchored times. `a * n` and `n * a` only for unanchored times and `int` n
  (`TypeError` for other factor types).
* `==` and `hash` are structural (dataclass). `<`, `<=`, `>`, `>=` only between concrete times
  (`TimeError` otherwise) and compare ticks.

### 2.6 The `t` factory

| call | result |
| --- | --- |
| `t("...")` | `parse_time` |
| `t.start`, `t.end` | anchors `start`, `end` |
| `t.span(a, b)` | `Span(start=a, end=b)` |
| `t.set(*times)` | `TimeSet(times=...)` |
| `t.beats(stream="beat")` | `BeatGrid(stream)` |
| `t.secs(x)`, `t.ms(x)`, `t.ticks(n)` | durations; numbers are allowed here because the unit is explicit |
| `t.frames(x)` | duration in frames |
| `t.frame(n)`, `t.beat(x)`, `t.downbeat(x)`, `t.bar(x)` | anchors |
| `t.cue(name)`, `t.marker(stream, nth=0)` | anchors |
| `t.word(text=None, *, index=None, source=None, nth=0)` | word anchor (exactly one of `text`, `index`) |
| `t.parse(value)` | `coerce_time` |

Floats given to explicit-unit constructors are converted through their shortest `repr` (so `0.1` is
exactly one tenth). `BeatGrid[i]` returns the anchor `beat:i`, `downbeat:i` or `bar:i` for those stream
names and `marker:STREAM#i` for any other stream; `BeatGrid[a:b:c]` returns
`TimeSet(stream, start=a or 0, stop=b, step=c or 1)`; negative indices and steps below 1 raise
`TimeError`. `every(step, start=0, stop=None)`, `within(span)` and `all()` build the same sets.
`t.every(period, span)` builds a periodic set (`TimeSet(every=period, window=span)`).

### 2.7 `Span`, `TimeSet` and JSON

* `Span(start, end)` and `TimeSet(stream, start, stop, step, times, offset, window)` are frozen pydantic
  models; their JSON is the plain model dump with times as canonical strings.
* A `Span` whose end lies before its start without any clock raises `ValueError` in model validation
  (`span_reversed(start, end)`): both ends concrete, or both on the same anchor (equal anchors, or `f:`,
  `beat:`, `downbeat:`, `bar:` anchors compared by index, `marker:NAME#N` and `word:` anchors by
  occurrence) with every offset component of the end at most the start's and one strictly smaller. Spans
  that only order once resolved are checked by the validator (`time.reversed`) and by `resolve_span`.
* A `TimeSet` is either a stream slice (`stream` set, `times` empty) or an explicit list (`stream` None).
  Model validation enforces it: a name-grammar `stream` with no `times`, or `stream` None with `start`,
  `stop` and `step` at their defaults (strict ints); anything else raises `ValueError`. `offset` must be a
  duration: an anchored offset raises `ValueError` in model validation, `TimeError` in `TimeSet + time`.
  `TimeSet + time` adds to `offset`; `within(span)` sets `window`.
* Randomized and periodic sets (the old `random_trigger` timing): `every` (a positive anchor-free length)
  makes a periodic set from the window start to its end (no stream, no times, window required);
  `jittered(amount, seed=None)` sets `jitter` (a non-negative anchor-free length: every time moves by a
  seeded uniform offset within `[-amount, +amount]`); `random(n, seed=None)` sets `pick` (keep `n` of the
  times, chosen with the seed). `TimeContext.resolve_set(times, seed=None)` applies jitter, then pick, with
  `random.Random(times.seed or seed)`; a set with jitter or pick and no seed raises `TimeError`. Executors
  pass `node_seed(root, node, <field path>)`, so an unseeded random set is still deterministic per node.
  `seed` without jitter or pick is a `ValueError`.
* `Time` fields serialize to the canonical string in JSON mode and stay `Time` objects in python mode;
  JSON Schema `{"type": "string", "format": "eks-time", "pattern": TIME_PATTERN}`. Validation accepts a
  `Time` or a string; anything else (notably numbers) is rejected.
* `needs()` on `Time`, `Span`, `TimeSet` and `needs_of(values)` return `TimeNeeds`: `fps` (frames or
  `f:`), `tempo` (bar or beat offsets, beat/downbeat/bar anchors, beat or bar streams), `window` (`end`),
  `cues`, `markers` (stream names, including TimeSet streams other than beat/downbeat/bar), `words`
  (named sources), `default_words` (a word without source) and `downbeat` (a `downbeat:` anchor or a
  `downbeat` stream, which also need `tempo`). `TimeNeeds | TimeNeeds` merges; `empty` is
  true when nothing is needed. A `TimeSet` stream adds its needs to those of its `every`, `jitter`, times,
  offset and window (`stream_needs`).
* Fields that name a clock stream or a word source by a plain string are typed with the annotated aliases
  `StreamName` (marker `STREAM_REFERENCE`) and `WordSource` (marker `WORDS_REFERENCE`), both
  `Field(pattern=CLOCK_NAME_PATTERN)` with the schema extension `x-eks-clock` (`stream` or `words`).
  `field_needs(field_info.metadata, value)` turns such a value into needs: `stream_needs(name)` is
  `tempo` for `beat`, `downbeat` (with `downbeat`) and `bar` and `markers={name}` otherwise, `words_needs(source)` is
  `words={source}`. The typed fields are `Captions.source` (`WordSource`), `BeatFlash.stream`,
  `BeatGlitch.stream` and the `beats` media check's `stream` (`StreamName`). `time_needs` (7.3), the
  validator's `time.unresolvable` and the clock dependencies (5.4) read them like times.

### 2.8 Strictness at authoring time

`ensure_time(value, what)` returns `value` when it is a `Time` and raises `TypeError` otherwise, with a
message naming `what` and suggesting `t('1.5s')` (numbers: "bare numbers are not times"; strings: "pass
t('...') instead of a string"). Every builder parameter typed `Time` goes through it. `coerce_time(value)`
accepts a `Time` or a string and is used only for `--set` parameters and JSON input.

The shared authoring helpers live next to it, and builders import them from here instead of defining their
own: `optional_time(value, what)` (`None` or `ensure_time`) and `ensure_duration(value, what)` (`ensure_time`,
then `TimeError` for an anchored value) in `eks_harness/ir/time.py`; `ensure_positive_duration(value, what)`
(`ensure_duration`, then `TimeError` with `duration_problem`) and `optional_positive_duration(value, what)`
in `eks_harness/ir/catalog/durations.py`, next to the `Duration` type they mirror.

### 2.9 Resolution (`TimeContext`)

A `TimeContext` holds what resolution needs: `fps`, the beat grid `beats` (ticks, ascending), the
`downbeats` grid, `meter` and `meters` (meter changes), `cues`, `markers`, `words`, `default_words`,
`start` and `end` (ticks of the `start` and `end` anchors).

`TimeContext.static(fps=, tempo=, cues=, start=0, end=None, horizon=None)` builds a context without any
analysis: the beat grid comes from `tempo.bpm`, `tempo.offset` (must be concrete) and `tempo.changes`
(bpm from that beat index on), extended up to `horizon` (or `end`, or 600 s); a beat lasts
`60 * TICKS_PER_SECOND / bpm` ticks computed exactly with `Fraction(repr(bpm))` and accumulated exactly;
grid points are rounded half to even. The downbeat grid is the bar starts of the meter map. Cues are
resolved with the context being built (cues may not reference other cues). `tempo.external` or a
`tempo.source` without bpm leaves the beat grid empty.

Names: marker source names, their `stream_names()` and speech-to-text source names share one plan-wide
namespace that `markers` and `words` map by name, so each is provided by exactly one source of the plan and
none is a clock tempo stream (`beat`, `downbeat`, `bar`, `kick`, `snare`); the builders raise `TraceError`
(10.4) and the validator reports `markers.duplicate` (11). Scenes embedded more than once take their
analysis names as inputs.

`resolve(time)`:

1. Anchor position `p` (ticks): `start` -> `start`; `end` -> `end` (`TimeError` if unknown); `f:N` ->
   `N * tpf`; `beat:x` -> `beat_time(x)`; `downbeat:x` -> the downbeat grid at index x (linear
   interpolation for fractional x, extrapolation with the last interval past the end; when the context has
   no downbeat grid, same as `bar:x`, which a static tempo always has and the validator requires of an
   analysed one); `bar:x` -> `beat_time(bar_start_beat(x))`; `cue:n` -> `cues[n]`;
   `marker:s#n` -> `markers[s][n]`; `word` -> the n-th word of the source (a word without a source reads the
   plan's only speech-to-text source) whose text matches
   case-insensitively after Turkish-aware folding (`eks_harness.ids.TURKISH_FOLD`, `casefold`, NFKD, then
   dropping combining marks only, so non-Latin letters are kept), or the
   word at `index`. No anchor: `p = 0`. Unknown names, indices out of range and missing grids raise
   `TimeError` naming the name and the known ones.
2. Bars: when `bars != 0`, `p = beat_time(bar_start_beat(bar_index_at_beat(beat_index_at(p)) + bars))`.
3. Beats: when `beats != 0`, `p = beat_time(beat_index_at(p) + beats)`.
4. Frames: `p += round_half_even(frames * tpf)`.
5. Ticks: `p += ticks`.

Beat space: `beat_time(b)` interpolates linearly between grid points, extrapolates past the last point with
the last interval, and raises `TimeError` for `b < 0` or a grid with fewer than 2 points.
`beat_index_at(ticks)` is its inverse (before the first beat it extrapolates with the first interval).
Meter: a bar has `numerator` beats; `bpm` counts beats of the meter's denominator. `meters` change the
meter from a bar index on. `bar_start_beat(x)` sums the beats of the bars before `floor(x)` and adds
`frac(x) * beats_in_bar(floor(x))`; `bar_index_at_beat` is its inverse. All intermediate math uses
`Fraction`; only final tick values are rounded (half to even). `Meter`, `TempoChange`, `MeterChange` and
`TempoMap` numeric fields are strict: ints stay ints (no bools, floats or strings), `bpm` takes an int or a
float but never a bool or a string.

`resolve_set(set)`: a stream slice takes the indices `start, start+step, ...` below `stop` (or until the
grid or `end` runs out) of the beat grid (`beat`), the bar starts (`bar`), the downbeat grid (`downbeat`)
or `markers[stream]`; an explicit list resolves each time. Then `offset` is added and, if `window` is set,
only times in `[window.start, window.end)` are kept. The result is ascending. `resolve_span` returns
`(start, end)` and raises `TimeError` when `end < start`. `frame_of(ticks)` is
`round_half_even(ticks / tpf)`.

### 2.10 Clip-local time (used by P9 and P10)

Inside a clip of an edit (its effects, transitions and animated properties) and inside a gfx layer's or
Blender object's animations, unanchored times and the anchors `start` and `end` are local to the clip or
the scene (`start` = 0, `end` = the clip's or scene's duration). All other anchors (`beat`, `downbeat`,
`bar`, `cue`, `marker`, `word`, `f:N`) are absolute on the enclosing clock and the renderer subtracts the
clip's start. This fixes the old segment-local versus absolute ambiguity.

## 3. Targets (P2, `eks_harness/ir/target.py`)

### 3.1 Model and JSON

```json
{"by": "test_id", "args": {"id": "list"}, "within": {"by": "role", "args": {"role": "dialog", "exact": false}}, "nth": 2}
```

`Target(by, args, within_, near_, nth_)` with JSON keys `by`, `args`, `within`, `near`, `nth` (aliases);
dumps always use `by_alias=True, exclude_none=True`. `by` is a registered target kind; `args` validate
against that kind's args model and are stored as that model's JSON dump (`exclude_none=True`).
`nth` is 0-based; `-1` means last. Builder methods return new targets: `within(t)`, `near(t)`, `nth(i)`,
properties `first` (nth 0) and `last` (nth -1). `within`/`near` accept a `Target` or a shorthand string
(keyword `kinds=` as in 3.3).

Built-in kinds (also registered in the catalog as category `target` by `ensure_builtins`):

| kind | args model | shorthand prefix | surfaces |
| --- | --- | --- | --- |
| `test_id` | `ByTestId(id)` | `#id`, `testid=` | web, ios, android, screenshot, video, scene |
| `role` | `ByRole(role, name=None, exact=False)` | `role=ROLE[:NAME]` | same |
| `text` | `ByText(text, exact=True)` | bare string, `text=` | same |
| `label` | `ByLabel(text, exact=False)` | `label=` | same |
| `placeholder` | `ByPlaceholder(text, exact=False)` | `placeholder=` | same |
| `css` | `ByCss(selector)` | `css=` | web, screenshot, scene |
| `kind` | `ByKind(kind)` (React Native node kind) | `kind=` | ios, android, screenshot, video |
| `point` | `ByPoint(x, y)` | `point=X,Y` | every surface |
| `rect` | `ByRect(x, y, w, h)` | `rect=X,Y,W,H` | every surface |
| `object` | `ByObject(name)` | `object=` | blender |
| `layer` | `ByLayer(name)` | `layer=` | gfx |

Modifiers: `within` and `near` on web, ios, android, screenshot, video and scene; `nth` everywhere
(`MODIFIER_SURFACES`). Surfaces: the platforms `web`, `ios`, `android`, plus `screenshot` (anchors of
`eks.annotate`), `video` (overlay anchors in edits, resolved from capture metadata), `blender`, `scene`
(web scenes) and `gfx`.

Matching semantics for executors (M2, recorded here so plans mean one thing): strict. Zero matches waits
until the timeout; more than one match fails with the candidates listed unless `nth` is set. `text`,
`label`, `placeholder` and role `name` compare after whitespace normalization; `exact=False` means
case-insensitive containment.

### 3.2 Shorthand grammar (`parse_target`)

```text
target  ::= "#" idchars              -> test_id
          | prefix "=" rest          -> the prefix's kind
          | anything else            -> text, exact
idchars ::= ( [A-Za-z0-9_.:-] | SENTINEL )+   (the whole remaining string; SENTINEL is a ref's
            format sentinel U+E000 "r" [0-9]+ U+E001, so f"#row-{ref}" stays a test id)
prefix  ::= "testid" | "role" | "text" | "label" | "placeholder" | "css" | "kind" | "point" | "rect"
          | "object" | "layer" | a shorthand registered with @eks.target_kind
rest    ::= role: ROLE [ ":" NAME ]   (NAME may contain ':')
            point: X "," Y            rect: X "," Y "," W "," H   (floats)
            others: the raw text
```

A `#` string that contains other characters (for example a space) is text. Strings that look like CSS are
text unless written `css=...`; there is no CSS guessing. Parsing never raises for a bare string; prefixed
forms raise `TargetError` when the rest is malformed.

### 3.3 Functions

* `by.<kind>(...)` build targets (signatures in the skeleton). A value that is not a `str` is converted
  with `str(value)`, which turns a `Ref` into its format sentinel inside a trace; `eks.ref.encode` later
  turns sentinels into `$fmt`, so `by.text(ref)` and `by.text(f"#{ref}")` work. `by.custom(kind, **args)`
  builds plugin kinds. `by.parse(text, *, kinds=None)` is `parse_target`.
* `validate_args(target, kinds=None)` validates `args` through the kind's model and returns the target
  with normalized args; kinds not in the mapping pass through unchanged (the validator reports them).
  `kinds=None` means the default kinds: `BUILTIN_KINDS` merged with the provider set by
  `use_default_kinds(provider)`; importing `eks.target` installs `REGISTRY.target_kinds`, so every `eks` entry
  point (`within`/`near` strings, `by.parse`, `by.custom`, `describe`) knows `@eks.target_kind` shorthands.
  An explicit mapping is merged over `BUILTIN_KINDS` only. Shorthands must fullmatch
  `[A-Za-z][A-Za-z0-9_.-]*` and be unique across distinct kinds, else parsing raises `TargetError`.
* `ensure_target(value, what, kinds=None)`: a `Target` -> `validate_args`; a `str` -> `parse_target`;
  anything else -> `TypeError` naming `what`.
* `check_target(target, surface, kinds=None) -> list[str]`: human-readable problems (unknown kind, kind
  not supported on the surface, modifier not supported), recursing into `within` and `near`. Empty means
  fine.
* `describe(target, kinds=None)`: the shorthand (plugin shorthands included) when one exists, else `by=...`, followed by ` within <...>`,
  ` near <...>`, ` nth <i>`; used in errors and reports.

## 4. Refs

### 4.1 IR encodings (P3, `eks_harness/ir/refs.py`)

| encoding | JSON | meaning |
| --- | --- | --- |
| `RefExpr` | `{"$ref": "n7f3k2a9q01", "path": [...]}` | the output of a node, then a path |
| `ItemExpr` | `{"$item": "<each node id>", "path": [...]}` | the current element of an `eks.each` |
| `FmtExpr` | `{"$fmt": ["/x?id=", {"$ref": ...}, ""]}` | string interpolation; parts are strings, refs or items |

Path steps (`PathStep`): a string is a key, an integer an index (negative counts from the end),
`{"slice": [start, stop, step]}` (each may be null), `{"find": {field: value}}` (first element whose
fields equal the values), `{"filter": {field: value}}` (all such elements), `{"len": true}`. Values inside
`find` and `filter` may themselves be `$ref`/`$item` objects. `path` is always present in dumps (possibly
empty).

Field aliases for argument models, each `Annotated[..., RefKind(type)]`: `RStr` (str or `$fmt`/`$ref`/
`$item`), `RInt`, `RFloat`, `RBool`, `RJson` (any JSON; refs may appear nested as plain dicts), `RCond`
(a ref or item, bool or json), `RMedia` and `RArtifact` (a ref or item only). The validator reads
`RefKind` to know which ref types a field accepts.

Functions:

* `is_expr_dict(value)`: a dict whose key set is exactly `{"$ref"}`, `{"$ref", "path"}`, `{"$item"}`,
  `{"$item", "path"}` or `{"$fmt"}`.
* `parse_expr(value)`: such a dict as the model; `RefError` otherwise.
* `walk_exprs(value)`: every `RefExpr`/`ItemExpr` in a JSON value, a pydantic model or nested
  containers, depth first in document order, including those inside `$fmt` and path steps.
* `referenced_nodes(value)`, `referenced_items(value)`: their ids, in order, without duplicates.
* `map_exprs(value, fn)`: a copy of a JSON value where every ref or item dict (also inside `$fmt` parts and
  path steps) is replaced by `fn(expr)`; a `$fmt` whose parts become all literals collapses to one string.
* `apply_path(value, path)`: evaluates a path on a literal JSON value; `RefError` on a missing key, an
  index out of range, `find` without a match, or a step that does not fit the value (len works on lists,
  dicts and strings).
* `collapse_fmt(fmt)`: merges adjacent strings and drops empty ones; returns a plain `str` when no ref
  remains.
* `ref_kind_of(annotation)`: the `RefKind.type` found in an annotation (the last `RefKind` of one `Annotated` wins, so `Annotated[ResourceRef, refers_to("tenant")]` is `resource:tenant`; looking through `Annotated`,
  unions, `Optional`, `tuple[...]`, `list[...]` and `dict[str, ...]`), else `None`.

### 4.2 Ref types (P3, `eks_harness/ir/types.py`)

```text
type   ::= scalar | "list[" type "]" | "resource:" kind
scalar ::= "none" | "json" | "str" | "int" | "float" | "bool" | "artifact" | "media"
```

`Node.out` holds one of these strings. `infer_path(out, path)`:

| producer type | step | result |
| --- | --- | --- |
| `json` | any step | `json`; `len` gives `int` |
| `list[T]` | int | `T` |
| `list[T]` | slice, filter | `list[T]` |
| `list[T]` | find | `T` |
| `list[T]` or `str` | len | `int` |
| `artifact` | a field of `Artifact` | that field (`str`; `size` is `int`) |
| `media` | a field of `MediaInfo` | that field (`duration` is `str`, sizes `int`) |
| `resource:K` | any step | `json` |
| anything else | any step | `RefTypeError` |

`compatible(produced, expected)`: true when equal, when either side is `json` (checked at run time), for
`int` into `float`, and for `resource:K` into `json`. Everything else is false (notably a number into a
string field: use an f-string). `type_of_annotation(tp)` maps a Python type argument of `Ref[...]` to a
ref type: `str`, `int`, `float`, `bool`, `None`, `JsonValue`/`Any` (`json`), `Artifact`, `MediaInfo`
(`media`), `list[X]`.

`Color` is a string `#rrggbb` or `#rrggbbaa`, normalized to lower case (`normalize_color` raises
`ValueError` otherwise). `normalize_media_uri(text)`: `artifact:ID`, `file:PATH` and `http(s)://...` stay
as they are; any other string is a path and becomes `file:` + the path with forward slashes; empty strings
raise `ValueError`.

### 4.3 The `Ref` proxy (P3, `eks/ref.py`)

`Ref[T]` is what every builder returns. It is a path expression, never a value.

* `Ref(node, type_, path)`; accessors `node_of`, `type_of`, `path_of`, `is_item` (for `ItemRef`).
* `ref.name` appends a key step; names starting with `_` raise `AttributeError`. `ref["key"]` appends a
  key (use it for fields named like the methods below), `ref[i]` an index, `ref[a:b:c]` a slice.
  `ref.find(**eq)`, `ref.filter(**eq)` (values go through `encode`, so they may be refs), `ref.first`
  (index 0), `ref.last` (index -1), `ref.len()`.
* Each step computes the new type with `infer_path`; an impossible step raises `RefUsageError` at trace
  time (for example `s.ui.count("#x").foo`).
* `bool(ref)`, `iter(ref)`, `len(ref)` and `x in ref` raise `RefUsageError` with the fix: conditions go
  through `eks.when`, loops through `eks.each`, lengths through `ref.len()`.
* Assigning attributes raises `AttributeError`. `==` and `hash` are identity.
* `Ref[T]` is invariant in `T` (the class declares `__eks_resolved__: T`, the value the ref resolves to at
  run time), so pyright rejects a `Ref[int]` where `Ref[str]` or `Ref[Json]` is expected; `Ref[Any]` and
  `JsonLike` (which includes `Ref[Any]`) still accept every ref. Text parameters are typed
  `Text = str | Ref[str] | Ref[Json]` (`eks.ui`).
* `format(ref, spec)` and `str(ref)` return a sentinel `"\ue000r<n>\ue001"` from the active
  `SentinelTable` (the same `Ref` object always gets the same token). A non-empty format spec raises
  `RefUsageError` ("format specs are not supported on refs"); with no active table it raises
  `RefUsageError` ("format refs only inside a plan trace"). `repr(ref)` is a debug string such as
  `Ref(n7f3k2a9q01.items[0].id: json)` and never a sentinel.
* `ItemRef` is a `Ref` whose node is the id of the `eks.each` container; it encodes as `$item`.
* `sentinel_scope(table=None)` installs a table for a block (tracing installs one per trace).
* `format_text(text)`: no sentinel -> the text; otherwise `FmtExpr` parts (empty strings dropped);
  unknown tokens or stray `\ue000`/`\ue001` characters raise `RefUsageError`.
* `Encodable`: any object with `__eks_encode__()` (resource handles, take handles) encodes as the result.
* `encode(value)` returns plain JSON-compatible Python data:

| value | encoding |
| --- | --- |
| `Ref` / `ItemRef` | `{"$ref": ..., "path": [...]}` / `{"$item": ..., "path": [...]}` |
| `str` | the string, or the `$fmt` dict when it contains sentinels |
| `None`, `bool`, `int`, finite `float` | as is (`NaN`/`inf` raise `RefUsageError`) |
| `Time` | its canonical string |
| pydantic model (including `Target`, `Span`, `TimeSet`, catalog models) | `encode(model_dump(mode="json", by_alias=True, exclude_none=True))` |
| `Encodable` | `encode(value.__eks_encode__())` |
| mapping with `str` keys | dict with encoded values (other key types raise) |
| list, tuple | list with encoded items |
| anything else (Path, Fraction, sets, objects) | `RefUsageError` naming the type |

`contains_refs(value)` is true when `encode(value)` would contain any ref, item or `$fmt`.

`eks.Media` is `Ref[MediaInfo]`; `eks.Json` is pydantic's `JsonValue`; `eks.Persona` is a `NewType` of
`str` used to mark persona parameters.

## 5. Tracing (P4, `eks/tracing.py`)

### 5.1 Lifecycle

`planning.trace` creates a `TraceContext`, enters `tracing(ctx)` (sets the current context and installs
`ctx.sentinels` through `sentinel_scope`), builds the root builder with the kind's root factory, calls the
entry function `fn(builder, **params)`, stores its return value with `set_outputs`, and calls
`ctx.finalize()`. Context variables are restored on exit, also on errors. Tracing never does IO and never
runs anything; the only reads are of source text for call sites (`linecache`).

`current()` returns the active context or raises `TraceError("... only inside eks.plan(...)")`; `active()`
returns it or `None`. The module-level `record(...)` is `current().record(...)`.

### 5.2 `TraceContext.record(op, args, *, uses, pin, label, out, id_key, key_suffix)`

1. `spec = registry.op(op)`; a container op recorded with `record` raises `TraceError` (use `container`).
2. Arguments: a mapping or a model; both are normalized as
   `spec.args.model_validate(encode(<mapping or model_dump(mode="json", by_alias=True, exclude_none=True)>))`.
   Validation errors propagate as pydantic `ValidationError`.
3. JSON args: `model.model_dump(mode="json", by_alias=True, exclude_none=True)`. Rule for every argument
   model: an optional field defaults to `None`; a field with a non-`None` default does not accept `None`.
4. Every `$ref` in the args must name a node already recorded in this trace and every `$item` an enclosing
   `eks.each` container, else `TraceError`. A `$ref` to an enclosing container (its value exists only after
   its body ran, and its Merkle hash covers its children) also raises `TraceError`.
5. Node id (section 5.3), `after` edges (5.4), `src` (the innermost user frame as `path:line`, path
   relative to the tree root, or `None`).
6. `Node(id, op, v=spec.version, args, uses=list(uses or ()), after, out=out or spec.out, pin, label, src)`;
   `out` must pass `out_problem(spec, args, out)` (registry): it equals `expected_out(spec, args)` (the op's
   `out`, or `resource:<args.kind>` for `data.create` and `data.base`), or narrows an op whose `out` is
   `json` to another ref type (not `none`), else `TraceError`
   is appended to the current scope. The op's provenance is added to `requires` unless its plugin is `eks`
   or `project`.
7. Returns `Ref(node.id, node.out)`.
8. `label` and `id_key` must be plain text: one built from a `Ref` (holding sentinel text) raises
   `TraceError`; refs are resolved only inside args.

`ctx.require(provenance)` adds a plugin requirement explicitly (builders call it when they embed plugin
catalog kinds or resources).

### 5.3 Stable node ids

Ids must not change under unrelated edits (blank lines, comments, edits elsewhere) and must be identical
for identical source.

* Call chain: the user frames between the entry function's frame (inclusive) and the innermost frame that
  called into the library. A frame is a library frame when its module name is `eks`, starts with `eks.`,
  is `eks_harness` or starts with `eks_harness.`, or is `contextlib` (the frames of every `with eks.*`
  block; `eks.tracing.TRANSPARENT_MODULES`). Frames above the entry function are ignored; when the entry
  function's frame is not on the stack (nodes recorded before or after the call, contexts without
  `entry_code`), there are no user frames.
* Segment per user frame: `"{relpath}:{qualname}:{h}"`, where `h` is the first 12 hex digits of the
  SHA-256 of the whitespace-normalized source text of the call expression being executed in that frame
  (its `co_positions()` span at `f_lasti`, read with `linecache` after `linecache.checkcache`). The
  normalized text is the expression's token strings (Python `tokenize`, comments and line breaks dropped)
  joined by single spaces (`eks.tracing.normalize_call_text`); text that does not tokenize falls back to
  its whitespace runs collapsed to single spaces. When the source is not available, `h` is `L{line}C{col}`.
  `relpath` is the POSIX path of the file relative to the tree root, or to the entry file's directory when
  no tree is given (`..` segments allowed). With a tree, a file outside it is addressed by its owner, never
  by its position relative to the tree: `plugin:<id>/<path inside the plugin root>` when it lies under the
  root of a loaded plugin (`eks.tracing.register_plugin_root(id, root)`, called by `load_plugins` for every
  active manifest; roots are kept per root, so two trees that each load a plugin of the same id from their own
  root both keep their root; the deepest matching root wins), else `pkg:<top-level module>/<path inside its package
  directory>` (the directory holding a single-file module) for code of an importable package, found from
  the frame's `__name__` or, for an origin file, the loaded module whose `__file__` is that file; any other
  file outside the tree raises `TraceError`. So ids, `src` and `Plan.source` of plugin and package code are
  the same wherever the tree sits. The tree is resolved once (`Path(tree).resolve()`) and every
  file is resolved with `os.path.realpath` before relativizing, so a relative or symlinked spelling of the
  same tree gives the same ids, `src`, `Plan.source` and `Plan.base`.
* Key: `scope.key + "/" + ">".join(segments) + "/" + op`, or `scope.key + "/" + id_key` when the builder
  passes `id_key` (seed resources pass their resource path, finalizers pass `"final:" + name`). With no
  user frame, the chain is `lib`. A builder that records from a decorator (where the call expression is the
  decorator itself, so every use shares one text) passes `key_suffix`, which extends the op segment:
  `.../{op}:{key_suffix}` (`eks.dynamic` passes the function's `__qualname__`; `@sc.bpy` records no node,
  its hook is part of the `blender.render` args); `id_key` wins
  over `key_suffix`. `record`, `eks.tracing.record` and `SessionCore.record_op` take `key_suffix`.
* Occurrence: a per-trace counter of identical keys; the full key is `key + "#" + str(n)`.
* Id: `derive_id("n", full_key)` (section 7.4), 11 characters `n` + 10 Crockford base32 characters.
* Scope keys: the root scope key is `"{kind}:{name}"`; a container branch scope key is
  `"{container_id}/{branch}"`.
* Literal `eks.each` copies get the template node's id plus `"." + str(i)` (allowed by
  `NODE_ID_PATTERN`).

### 5.4 Ordering, lanes and `after`

* `OpSpec.ordering == "program"` (every effectful op): the node gets an edge to the scope's tail and becomes
  the new tail. In a parallel scope (`ctl.parallel` body) there is one tail per lane, one lane per resource:
  a node is on the lane of every resource in its `uses` (or on `*` without resources), gets an edge to the
  tail of each of those lanes (in `uses` order, without repeats) and becomes the tail of all of them. So
  `session.as` and `ctl.dynamic` (lease and backend) order both the UI and the API nodes around them.
* `"graph"` (pure ops and seed resources): no tail edge; the node only depends on what it references.
* Every node also gets edges to the nodes its args reference with `$ref`.
* `after` keeps insertion order (tail first, then refs in document order), without duplicates or self.
* Clock providers are dependencies too. `finalize` calls `eks_harness.ir.hashing.link_clock(plan, registry)`
  before hashing: `clock_dependencies(plan, registry)` maps every node to the providers its times resolve
  against (`clock_providers`: the clock tempo source when the node needs tempo, and the `media.markers`
  nodes that provide each needed stream or word source, or every speech-to-text node for words without a
  source; the needs are those of 7.3 including the cues the node uses and the clock bounds it reads,
  `clock_needs`, and a node whose args do not validate depends on every provider). A provider that comes
  later in program order than a dependent at the root level is hoisted, with the graph-ordered root nodes it
  reads (its `$ref` producers and its own providers), to just before the dependent's root node; a provider
  inside a container, a provider that reads a program-ordered node or the dependent itself raises
  `TraceError`. Then each provider id is appended to the dependent's `after` (after its tail and ref edges)
  when not already there. `d.base` (10.3) appends the `data.extend` it reads from the same way.
* A container joins the outer chain like a program node; the first node of each branch has no edge to the
  container (containment implies it). When a container closes, it stays the outer tail.
* A container in a parallel scope takes only the lanes its body uses, or `*` when it uses none: without
  `uses` it gets no tail edge and takes no lane when it opens (only its ref edges); with `uses` it joins the
  lane of each of them when it opens. Its lanes are those of every resource the leaf program nodes among its
  descendants use (a nested container counts only through its own `uses`):
  when it closes, it gets an edge to each such lane's tail as it was when the container opened, and becomes
  that lane's tail. Reopening its branch after another node was recorded on one of
  those lanes raises `TraceError`.

### 5.5 Containers and scopes

`container(op, args, *, branch="body", uses, pin, label, out, id_key, flags=(), parallel=False,
each=False)` records the container node like `record` (with `branches={}`), pushes a `Scope` for
`branch`, yields the container's `Ref`, and on exit pops the scope and stores its nodes in
`node.branches[branch]` (an empty branch is stored as an empty list). `branch(node_id, branch, flags=())`
reopens an existing container of the current scope and creates the named branch or appends to it (used
by `otherwise` and `take.flow`; a reopened branch continues its own program chain). Scope flags are
inherited by nested scopes; the flags in use are `record` (inside `capture.record`), `take` (inside a
`score.take` body), `template` (inside a literal `eks.each`) and `parallel` (inside a `ctl.parallel` body,
set by `container(parallel=True)`). `has_flag(name)` checks the current scope. Mismatched scope nesting
raises `TraceError`.

Literal `eks.each` support: `ctx.template()` opens a template scope (key `"{template_id}/body"`, the
template id derived like a `ctl.each` node id at the call site, `Scope.each` = template id, `parallel`
copied from the enclosing scope) and yields it; `ctx.lane_heads(scope)` returns the first program node per
lane; `ctx.splice(nodes, heads=, tails=)` appends copies to the current scope, prepending the current lane
tails of every lane a head starts to that head's `after` (in lane order, without repeats) and moving the
lane tails; `ctx.drop_nodes(ids)` forgets the template nodes, so
a ref leaked out of a literal `each` body raises `TraceError` when used.

Visibility (also validated): a node may reference an earlier node of its own scope, earlier nodes of any
enclosing scope, and nodes inside earlier containers whose op has `exports_body=True` (macro, parallel,
record, cassette, take), transitively. Nodes inside `when`, `each` and `dynamic` bodies are not visible
outside them.

### 5.6 Resources and sessions

* `resource(kind, **identity)`: id `resource_id(kind, identity)` (section 7.4); the identity keys `app`,
  `definition`, `seed`, `session`, `name` fill the `Resource` fields of the same name, other keys go to
  `params`. Declaring the same identity again returns the same id.
* `session_resources(spec)` declares and returns the session's resources: always the lease
  `resource("lease." + spec.platform, app=spec.app, session=spec.key)`, and the backend
  `session_backend(spec)` = `resource("backend", app=spec.app, definition=spec.backend, seed=spec.seed,
  session=spec.key)` only when the session names a seed, a backend or a persona (`needs_backend(spec)`;
  `None` identity values are left out). A flow that needs none of them holds no backend: `SessionCore` declares
  it on the first op whose spec uses the `backend` role (`api.*`, `session.as`, `backend.info`, `data.*`).
  `score.take` uses only the `lease` role; its container uses every resource its session holds. A function
  under `@eks.dynamic` cannot add a backend at run time: its sub-plan fails with `subplan.resource`.
* `SessionCore(ctx, spec)` keeps the context, the spec and those resources. `record_op(op, args, at=,
  label=, id_key=)` records with `uses` = the session's resources for the op's `OpSpec.resources` roles
  (in that order) and `pin=at`; `record_container(...)` does the same for containers. `activate()` pushes
  the session on the active-session stack for a block; `current_session()` returns the innermost active
  session or raises `TraceError` (used by `expect`); `active_session()` returns it or `None`.
  `tracing(ctx)` starts an empty session stack and restores the outer one on exit. `planning.trace`
  activates the root session of a flow for the whole call; the score activates each take's session inside
  `take.flow()`. `record_op(at=)` on an op without `pinnable=True` raises `TraceError`.
* `Bound(session)` is the base of every session namespace (`s.ui`, `s.api`, ...).

### 5.7 Clock, personas, outputs, finalizers

* `ctx.clock` starts as `planning.base_clock(entry)`; `update_clock(**fields)` replaces fields through
  `Clock.model_validate` and returns the new clock.
* `add_persona(key)` records a persona output of a seed plan (in order, without duplicates).
* `on_finalize(fn, order=0)` registers a callback run by `finalize` in `(order, registration)` order with
  the root scope current; builders use it to emit deferred nodes (edit render, audio mix, scene renders,
  media checks, route builders left without an action). `planning.trace` registers one more, the
  requirement finalizer, at `REQUIRES_FINALIZE_ORDER` (1 000 000, after every scene finalizer at
  `SCENE_FINALIZE_ORDER` 100): it walks `ctx.scope().nodes` through every branch and calls
  `ctx.require(Provenance(plugin, version))` for each `node_contributions(node, ctx.registry, scope=)`
  entry (with the `SessionScope` of the session governing the node, 7.3) and for each
  `contributions(ctx.session, ctx.registry)` entry (9.1), so `requires` is complete before `hash_plan`
  runs. The session's `defaults` (the platform default checks resolved at plan time, 10.2) and a take
  session's inside the `score.take` args are walked like any `CheckUse`, so a plugin's default check puts
  its plugin and version into `requires`.
* `set_outputs(value)` stores `encode(value)`; a `$ref` to a node not in this trace (a ref leaked out of a
  literal `each` body or from another trace) or any `$item` raises `TraceError`.
* `finalize()`: runs the finalizers, requires the scope stack to be back at the root (`TraceError`
  otherwise), builds the `Plan` (params as `encode`d JSON, resources sorted by id, nodes of the root scope,
  personas, requires sorted by plugin, `base`, `tags` from the entry options, `source`), fills every hash
  with `hash_plan` and returns it.
* `call_site()` returns the innermost user `CallSite` or `None`.

### 5.8 `capture_function(fn, allowed_globals=(), allowed_builtins=SANDBOX_BUILTINS)`

Builds the `FunctionSource` for `eks.dynamic` and `@sc.bpy`:

* `source`: `inspect.getsource(fn)` dedented, decorator lines before the `def` removed, line endings `\n`,
  one trailing newline. `hash`: `digest(source.encode("utf-8"))`. `params`: parameter names.
* `freevars`: one `Binding` per closure variable: a `SessionCore` -> `session` with the session key; JSON
  literals -> `literal`; `Time` -> `time` (canonical string); `Target` -> `target` (its JSON);
  the `by`, `t` and `expect` singletons -> `factory` with their name; a `Bound` namespace -> `namespace`
  with `"<session key>.<attribute>"`. Anything else raises `TraceError` naming the variable.
* `globals`: names the code (including nested code objects) loads from module globals or builtins, sorted.
  The names come from the bytecode (`LOAD_GLOBAL`, `STORE_GLOBAL`, `DELETE_GLOBAL`) and from
  `scan_sandbox_source(source)` (`eks_harness.ir.ops.ctl`), which reads the captured source with `symtable`
  so class bodies (`LOAD_NAME`) count: every global symbol of the function and of its nested function,
  class, lambda and comprehension scopes, minus the closure variables. Local variable annotations inside a
  function are not evaluated and are ignored; class body annotations are evaluated and count.
  A name that resolves to a builtin (it is not shadowed in `fn.__globals__`) is recorded when it is in
  `allowed_builtins` and otherwise raises `TraceError` naming it and listing the allowed set. Names read by
  the def-time expressions of the captured source (parameter defaults and annotations, evaluated when the
  sandbox executes the `def`) and names a class body assigns and reads (`LOAD_NAME` falls back to builtins
  before the assignment) are not recorded, but a builtin among them outside `allowed_builtins` raises the
  same `TraceError`.
  `SANDBOX_BUILTINS` (`eks_harness.ir.ops.ctl`) is the sorted tuple of deterministic, side-effect-free
  builtins: `abs all any bool dict divmod enumerate filter float frozenset int isinstance len list map max
  min pow range reversed round set sorted str sum tuple zip`; `__import__`, `open`, `eval`, `exec`,
  `compile`, `hash` (it depends on `PYTHONHASHSEED`), `id`, `input`, `globals`, `vars`, `getattr`,
  `setattr`, `breakpoint`, `print` and every other builtin are rejected. Any other name must be in
  `allowed_globals` or be a module-level JSON literal (then it becomes a `literal` freevar); otherwise
  `TraceError`. `ALLOWED_DYNAMIC_GLOBALS` is `("eks", "by", "t", "expect", *SANDBOX_BUILTINS)`.
* Escapes: an allowed builtin reaches the real `builtins` module through `len.__self__`, and every object
  reaches the type graph through `__class__`, so the source scan rejects with `TraceError` (naming the
  first one in source order and its line): a name starting with `__` that is not a builtin (`__builtins__`,
  `__loader__`, ...), an attribute starting with `__` (`__self__`, `__class__`, `__subclasses__`,
  `__globals__`, ...), an attribute in `INTROSPECTION_ATTRIBUTES` (`gi_frame`, `gi_code`, `gi_yieldfrom`,
  `cr_frame`, `cr_code`, `cr_await`, `cr_origin`, `ag_frame`, `ag_code`, `ag_await`, `f_back`,
  `f_builtins`, `f_code`, `f_globals`, `f_locals`, `f_trace`, `tb_frame`, `tb_next`), the same names as
  keyword attributes of a `match` class pattern, and the same names as attribute steps of a `str.format`
  field (`"{0.__self__}"`) in any string constant. `blocked_attribute` also rejects every private attribute
  (starting with `_`, so `s._ctx`) and the `INTERNAL_ATTRIBUTES` (`ctx`: the trace context reaches the
  workspace path, the registry and `record(..., pin=)`).
* The `eks` global: `eks` re-exports its submodules (`eks.tracing` holds `builtins` and `os`), so a `Name`
  `eks` may appear only as `eks.<a>` with `a` in `SANDBOX_EKS_ATTRIBUTES` (`Span Target Time TimeSet annotate
  by checks each expect otherwise overlay parallel t when`), and for the namespace modules in
  `SANDBOX_EKS_NAMESPACES` (`checks`: `to_use use`; `overlay`: `annotate arrow blur callout caption custom
  highlight icon image redact spotlight step title`) only as `eks.<a>.<b>` with `b` among them. Anything
  else (`eks.tracing`, `eks.overlay.builtins`, a bare `eks` that could be aliased) is an escape named by its
  dotted chain. The public attributes of these members, of `by`, `t`, `expect`, sessions and namespaces
  reach no module.
* Format strings: `.format` and `.format_map` are allowed only when called directly on a string literal,
  whose fields the escape scan reads; any other use (`field.format(gen)` with a field built at run time,
  `str.format(...)`, `"{}".format` passed as a value) is an escape named `format`.
* Object addresses: `sandbox_leaks(source, data_names=, data_modules=, params_are_data=)` (shared with the
  validator) rejects with `TraceError` any formatting or hashing of a value that may hold an object
  address: generators, `map`/`filter`/`zip`/`enumerate`/`reversed` objects, functions, lambdas, methods,
  classes, instances, modules and sandbox objects (`s`, namespaces, `eks`, `by`, `t`, `expect`) print
  their address in `str`/`repr` and hash by identity, so the result would change between runs. The sinks
  are the argument of `str(...)`, an f-string field, the right operand of `%`, the arguments of a literal
  `.format`/`.format_map`, the elements of a set display or set comprehension, the argument of
  `set`/`frozenset`, and `str` used as a value (allowed only as the function of `map`/`filter` or the
  `key=` of `sorted`/`min`/`max` over data). A sink accepts only data, inferred over the whole source:
  literals, f-strings, comparisons, the outer function's parameters when `params_are_data` (the resolved
  JSON inputs of a dynamic function; a bpy hook's `bpy` and `ctx` are host objects), `data_names` (the
  `literal`, `time` and `target` freevars, `DATA_BINDING_KINDS`), builtin names, attributes and calls
  rooted at a `data_modules` module (`math.pi`, `mathutils.Vector(...)`: numeric libraries, so their
  results count as data), results of `abs all any bool divmod float int isinstance len pow range round str`,
  `list`/`tuple`/`sorted`/`set`/`frozenset`/`dict`/`sum`/`min`/`max` over data elements, method calls on
  data whose arguments are data (or iterables of data), containers and comprehensions of data, and
  calls of nested functions and lambdas that return data. Names are data when every binding (assignment,
  loop and comprehension target, walrus, parameter at every call site, `map`/`filter`/`key=` element) is
  data; a method call or item or attribute store with a non-data argument taints every name that may alias
  the receiver (names linked by assignments, call arguments, defaults, returns, `match` subjects and class
  bodies). Outer defaults and every annotation are not observable and are skipped. `capture_function` takes
  `params_are_data` as "no importable modules in `allowed_globals`" and `data_modules` as those modules, so
  dynamic functions and bpy hooks get the same answer as the validator.
* Hash seed: `set` and `frozenset` stay allowed, and the iteration order of a set of strings depends on
  `PYTHONHASHSEED`, so the executor runs the sandbox interpreter with `PYTHONHASHSEED=0`; `hash` stays
  rejected so a function's result never exposes the seed directly.
* Imports: every `IMPORT_NAME` of the code and its nested code objects is checked (a relative import keeps
  its leading dots, so its root is empty). The root module must be in `allowed_globals` and not one of the
  sandbox-injected `ALLOWED_DYNAMIC_GLOBALS`; otherwise `TraceError`. So an `eks.dynamic` function imports
  nothing, and a `@sc.bpy` hook (`allowed_globals=BPY_GLOBALS`) may import `math` and `mathutils` (and their
  submodules) only. `BPY_MODULES` in `eks_harness.ir.ops.ctl` holds the same two names for the validator.

## 6. Control flow (P4, `eks/control.py`)

| construct | IR |
| --- | --- |
| `with eks.when(cond):` | container `ctl.when`, args `{"cond": <ref>}`, branch `then` |
| `with eks.otherwise():` | branch `else` of the `ctl.when` whose `with` block closed immediately before, in the same scope |
| `with eks.each(ref, limit=) as item:` | container `ctl.each`, args `{"over": <ref>, "limit": n}`, branch `body`; `item` is an `ItemRef`; `Each.over` is `RList` (`RefKind("list[json]")`), and a ref whose type is not a list or `json` raises `TypeError` at trace time (the validator reports `ref.type` for hand-written plans) |
| `with eks.each([..literal..]) as item:` | expanded at plan time: no container, one copy of the body per element |
| `with eks.parallel():` | container `ctl.parallel`, branch `body`, per-lane ordering |
| `@eks.dynamic` | node `ctl.dynamic`, args `{"fn": FunctionSource, "inputs": {...}}`, out `json` |
| macro call | container `ctl.macro`, args `{"name", "version", "params"}`, branch `body` |

* `when(cond)`: `cond` must be a `Ref` (`TypeError` for Python values; branch in Python instead).
* `otherwise()`: `TraceError` unless the last thing in the current scope is a `with eks.when(...)` block
  the user just closed there (`ctx.last_closed_container()`, cleared by every recorded or spliced node)
  and that `ctl.when` has no `else` branch; a `when` spliced in by a literal `each` copy does not qualify.
* `each(over)`: a `Ref` gives the runtime form; the `ItemRef` type is `T` for `list[T]`, else `json`.
  A list or tuple gives the literal form: the body is traced once in a `template` scope with an `ItemRef`
  on a template id; on exit, for each element (encoded to JSON; non-JSON elements raise `TypeError`) the
  template nodes are copied with `$item` of this loop replaced by `apply_path(element, path)`, `$fmt`
  collapsed, args re-validated, ids suffixed `.i`, intra-template `after` edges remapped, the first program
  node of copy `i` chained after the last of copy `i - 1` (copy 0 after the outer tail). `limit` truncates.
  Strings, mappings and other values raise `TypeError`; so do elements holding refs (loop over refs with a
  Python `for`). Refs to template nodes inside a copy are remapped to the copy's nodes.
* `parallel()`: nesting a parallel inside a parallel raises `TraceError`.
* `dynamic(fn)`: every parameter needs a default; defaults are `encode`d into `inputs` (refs allowed);
  `fn` is captured with `capture_function(fn, ALLOWED_DYNAMIC_GLOBALS)`; the node is program-ordered and
  uses the active session's resources when there is one; it passes `key_suffix=fn.__qualname__`, so
  adding, removing or reordering one dynamic function leaves the others' ids alone. The decorated name is
  bound to the returned `Ref`.

## 7. Plan IR (P3, `eks_harness/ir/plan.py`, `eks_harness/ir/hashing.py`)

### 7.1 Models

| model | fields (see skeleton for types) |
| --- | --- |
| `Plan` | `schema` ("eks/plan@1"), `kind`, `name`, `params`, `clock`, `session`, `resources`, `nodes`, `outputs`, `personas`, `requires`, `base`, `tags`, `source`, `hash` |
| `Node` | `id`, `op`, `v` (op version), `args`, `uses`, `after`, `out`, `pin`, `label`, `src`, `branches`, `hash` |
| `Resource` | `id`, `kind`, `app`, `definition`, `seed`, `session`, `name`, `params` |
| `Clock` | `now` (ISO 8601 with offset, seconds precision), `seed` (root random seed), `fps`, `window`, `duration` (`Duration`), `tempo`, `cues` |
| `SessionSpec` | `key`, `app`, `platform`, `seed` (scenario), `persona`, `backend`, `viewport`, `pace`, `timeout` (`Duration`), `noise`, `root_seed`, `now` (the device's frozen wall clock, normalized like `Clock.now`), `timezone` (IANA name), `locale` (BCP 47), `network_ignore` (URL substrings), `viewports` (name -> `Viewport`, from the app profile), `checks` (`CheckUse`s every checkpoint without its own `checks=` runs on top of the platform defaults), `defaults` (the platform default checks resolved at plan time, 10.2) |
| `CheckUse` | `kind`, `params` (re-exported by `eks_harness.ir.ops.check`) |
| `FunctionSource` | `name`, `source`, `hash`, `params`, `freevars`, `globals` |

* `branches` is `None` for plain ops and a dict branch name -> nodes for containers.
* `pin` is the take time of a node (`at=`).
* `base` is the directory of the entry's source file relative to the tree root (POSIX, `.` when at the
  root); relative media paths are relative to it. The builders store them that way whoever wrote them:
  `eks.media.source` (and `as_media`) resolves a relative `file:` path against the directory of the file
  whose code wrote it and stores it normalized and relative to `base` (`TraceContext.media_path(path,
  origin)`), so a scene file's `sc.load("assets/phone.blend")` embedded by `.harness/scores/ad.py` is
  stored as `file:../scenes/assets/phone.blend`, the same tree file the standalone scene plan stores as
  `file:assets/phone.blend`. `TraceContext.media_uri(path, origin)` returns the whole URI: a relative path
  written by plugin or package code outside the tree (5.3) becomes `plugin:<id>/<path>` or
  `pkg:<module>/<path>` resolved inside that owner (`TraceError` when it leaves it), and with a tree a
  `file:` path that resolves outside the tree raises `TraceError`; `media_path` is its path part. The
  writing file is `origin` when given (a web scene's `entry` and a scene
  parameter default use the scene's `EntrySpec.file`, a score's `song` and entry parameters the entry's
  file), else the innermost user frame of the call (section 5.3), else the entry file. Absolute paths,
  `artifact:` and `http(s):` URIs are kept as written, and media sources deduplicate on the stored
  form. `source` is `{file, qualname, line}` of the entry.
* `normalize_now(value)` parses with `datetime.fromisoformat`, requires an offset (`ValueError` for naive
  times) and returns `isoformat(timespec="seconds")`, so `2026-01-15T09:00+03:00` becomes
  `2026-01-15T09:00:00+03:00`.
* `Plan.walk()` yields `(node, ancestors)` depth first in program order (a container before its branches,
  branches in stored order); `node(id)` and `resource(id)` look up at any depth (`PlanError` if missing);
  `to_json(indent=2)` is `model_dump_json(by_alias=True, exclude_none=True, indent=indent)`;
  `from_json` validates.

The design's example IR maps one to one: `{"id": "n...", "op": "api.get", "args": {"path": "/api/..."},
"uses": ["r..."]}` and `{"op": "ui.nav", "args": {"route": {"$fmt": [...]}}, "after": [...],
"src": "flows/subeler.py:9"}`; node and resource ids are hashed instead of `n1`/`r1`.

### 7.2 Canonical JSON

`canonical_json(value) = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
allow_nan=False).encode("utf-8")`. Input must be JSON-native (dicts with string keys, lists, strings,
ints, finite floats, booleans, `None`); anything else raises `TypeError`. Models are dumped with
`mode="json", by_alias=True, exclude_none=True` first.

### 7.3 Merkle hashing

* `digest(data) = "sha256:" + hashlib.sha256(data).hexdigest()`.
* `resource_hash(r, registry=None) = digest(canonical(dump(r) without "id"))`, plus `"contributions":
  {"kinds": [...], "presets": []}` when the resource has registry contributions
  (`eks_harness.ir.contributions.resource_contributions(resource, registry)`): a `backend` resource whose
  `definition` names a registered backend contributes `["backend", name, "", plugin, plugin version]`, so
  the backend plugin's version is part of the resource hash, of every node that uses it and of the plan
  hash. `hash_plan`, `plan_hash` and the validator pass their registry.
* `node_hash(node, refs, resources, items, executor_version, clock=None)` hashes
  `{"op", "v", "x": executor_version, "args": args', "uses": [resource hashes in order],
  "after": sorted(hashes of the after nodes), "out", "pin": canonical pin or null,
  "branches": {name: [child hashes in order]} or null, "clock": clock}`
  where `args'` is `map_exprs(args, ...)` turning `{"$ref": id, "path": p}` into
  `{"$ref": refs[id], "path": p}` and `{"$item": each_id, "path": p}` into `{"$item": d, "path": p}`,
  `d` being the number of `eks.each` containers between the use and that container (0 = innermost);
  `items` is the stack of enclosing each ids. `id`, `src`, `label` and `hash` are excluded.
* Cacheability: a content-addressed cache keyed on the node hash may serve a pure node only when every
  transitive input is pure and immutable. `cache_inputs(plan, node, registry=None)` walks the node's data
  inputs (`$ref` and `$item` producers and its clock providers, `clock_providers` of 5.4, which follow the
  cues the node uses and the clock bounds it reads, the same needs as its clock payload) through pure
  producers and returns the ids, in program order, of the inputs whose resolved content must join the cache
  key: non-pure producers (a `score.take`'s footage, a `capture.shot`, a `ctl.each` item, `api.*`
  results), content-dependent pure nodes (`content_dependent(node)`: every `media.source` except an
  artifact pinned to its content, `artifact:ID@sha256:HEX` (`PINNED_ARTIFACT_URI`); the store re-renders,
  re-captures and restores artifacts under the same id, so a plain `artifact:ID` is read by content like a
  `file:`, `plugin:`, `pkg:` or `http(s):` URI, and the executor fails a pinned source whose artifact no
  longer has that sha256) and machine-dependent nodes (`machine_dependent(node, registry)`: a
  `media.markers` node with a source whose `pinned()` is false, which for the built-in kinds is a
  `BeatTracker` with `backend="auto"`, resolved per machine through BeatNet, madmom and librosa, and an
  `STTMarkers` without all of `model`, `device` and `compute_type` (speech to text is neural inference whose
  model, device and precision the backend otherwise picks per machine), a markers
  node whose args do not validate; an `edit.render` whose `timeline.render.encoder` is unset (the encoder is
  chosen per machine); a `gfx.render` without `cpu: true` (the GPU adapter and driver); a `blender.render`
  unless its `scene.render` is `CYCLES` on `CPU` with a `platform` pin (EEVEE and Workbench rasterize through
  the GPU driver, or software GL on a node without a GPU, whatever the device, and `BlenderRender` rejects
  them with `device="CPU"`; `GPU` adds the adapter and driver; an unpinned CPU Cycles render differs per
  worker platform, so the key adds the platform tag of the workers that rendered it); every `scene.render`
  (the browser build); every node of an op with
  `OpSpec.device_dependent` (built-in: `media.tts` and `media.stems`, whose neural inference gives other bytes
  on another CPU, CUDA or MPS device or model build; plugin ops set the flag on `@eks.op`); an `audio.mix`
  with a track that sets `stems` or `denoise` (separation and noise reduction models), read from the raw args;
  a `check.media` node whose kind is not a registered media check, whose kind is registered with
  `CheckSpec.device_dependent` (`@eks.check(device_dependent=True)`, media checks only, `RegistryError`
  otherwise), whose params do not validate, or whose validated params implement `pinned()`
  (`eks_harness.ir.registry.Pinnable`) and return false: the built-in `text` and `safe_area` checks with
  `engine="auto"`, which reads the frames with macOS Vision on macOS and Tesseract elsewhere).
  A machine-dependent node lists itself: the M1 key adds what the executing machine resolved for it (the
  beat grid, the chosen encoder, the GPU adapter and driver identity, the worker platform tag (`os-arch`, as
  nodes report it) of a Blender render, the browser build, the inference device, model weights and backend
  version, the OCR engine). A pinned analysis backend, model, device or OCR engine is part of the node hash,
  and the executor fails a node whose pinned choice is unavailable instead of substituting another one, so a
  pinned beat grid, transcript or OCR result is the same on every machine; a Blender render pinned to a
  platform renders every frame on workers of that platform and fails when there is none. Every `MarkerBase`
  kind is pinned unless it overrides `pinned()`. It returns `None`
  for a node that is not pure itself (never cached) and `()` when the node hash alone is a valid key;
  `cacheable(plan, node)` is `cache_inputs(...) == ()`. The M1 cache keys a pure node by its node hash plus
  the content hashes of `cache_inputs`. In the design's ad score the Blender scene and the edit depend on the
  take, the song and the scene file, and both render on the GPU, so neither is cacheable by node hash alone.
* `hash_plan(plan, executor_versions=None)` computes node hashes in program order, children before their
  container, fills `node.hash` everywhere and `plan.hash`, and returns the same plan. Executor versions
  default to the registry's `OpSpec.executor_version`. Each node's `clock` is `clock_payload(...)`: the
  part of `Plan.clock` its times resolve against. It always holds `seed` (the root seed, since per-node seeds
  derive from it) and `now` when set; then, from the `TimeNeeds` of the node's validated args and its pin
  (`time_needs`), `cues` (the needed cues' values, whose own needs are added), `fps`, `window` and
  `duration` (when the window is needed, the needs of the clock `window` and `duration` themselves are
  added too (`bounds_needs(clock)`), so a window written in bars carries the tempo and the cues it
  resolves against), `tempo` (the tempo map with `source` replaced by that node's hash), `markers` and `words`
  (stream or speech-to-text source name to the sorted hashes of the `media.markers` nodes providing it) and
  `all_words`. A node whose op is unknown or whose args do not validate depends on the whole clock.
  An op registered with `bounded=True` reads the clock bounds (its output length or cut follows the clock
  `window` and `duration`), so its node always needs the window: `edit.render` and `audio.mix` (the cut
  of the edit and of the song), `gfx.render`, `blender.render` and `scene.render` (a scene without its own
  `duration` renders the length of the embedding clock's bounds, its window, else its duration, and the
  clips that show it cut from that render; only on an open clock, with neither, is its length the
  embedding clip's) and `score.take` (its `span` defaults to the window).
  Moving the score window or changing the score duration therefore changes every render hash and the
  take hashes, while sources and marker analyses keep theirs. `time_needs(value, decoder)` also reads
  times held as plain JSON: every JSON object is decoded with `JsonDecoder(registry).models(mapping)` (a
  `Target` for a string `by`, `Keys[JsonValue]` for a `keys` list, `Driven` for a `curve`, each catalog
  kind of a string `kind` whose model validates it and a `CheckUse` for a check kind) and the decoded
  models' typed times count, so beat keyframes assigned with gfx or Blender `assign` (`SetProp.value`)
  follow the tempo and cues like `animate` keys; a `ctl.dynamic` closure `Binding` of kind `time` counts
  through `binding_needs(binding)` (its value parsed as a `Time`). Without a tempo, the validator reports
  those times as `time.unresolvable` (section 11). Fields typed `StreamName` or `WordSource` (2.7) count
  through `field_needs`, and the params of a `CheckUse` or `check.media` count through the check kind's
  params model (`JsonDecoder.check_params`). The clock payload expands the needs with `clock_needs(clock,
  needs)` until nothing changes: the clock bounds when the window is needed, and the needs of every needed
  cue. Clock providers (the tempo source and marker nodes) are hashed on demand when a node needs them
  before their program position, after their own `$ref` producers and `after` targets; a provider that
  needs its dependent raises `PlanError`. Traced plans never need this path, since `link_clock` (5.4) puts
  providers first.
  The context payload also carries the registry contributions of the node (`kinds_payload(node, registry)`,
  merged into the same dict): `kinds`, the sorted `[category, kind, version, plugin, plugin version]` of
  the node's own op when a plugin contributes it (`["op", op, OpSpec.version, plugin, plugin version]`;
  built-in and project ops are left out, their `v` and executor version already cover them), and of
  the resource kind of a `data.create` or `data.base` (`["resource", kind, ResourceSpec.version, plugin,
  plugin version]`, built-in and project kinds included, so bumping `@eks.resource(kind, version=)` changes the
  `data.create` hashes and with them the seed plan hash that keys the seed snapshot), and of
  of the renderer a `media.source` names (`["renderer", media kind, "", plugin, plugin version]`), and of
  every catalog kind (effect, transition, easing, curve, overlay, marker), target kind (`Target.by`, also
  inside `within`/`near` and inside target args) and check kind (`CheckUse` and `check.media`, with the
  check params validated and walked for targets) in the node's validated args, and `presets`, the sorted
  `[category, name, sha256 of the preset value's canonical JSON, plugin, plugin version]` of every
  registered preset the node names (the op fields of `PRESET_FIELDS`, an overlay's `style`, a take
  session's `viewport`, `pace` and `persona`, and the `persona` field of any op whose args model has one:
  `session.as` and the `api.*` family). Values also count where the args hold them as plain JSON (a
  `JsonValue` field such as gfx `SetProp.value` and `Animate.keys`, the blender equivalents, a `Binding`
  value, a `ctl.dynamic` input): every JSON object there is decoded, as a `Target` when it has a string
  `by`, and when it has a string `kind` as each catalog kind of that name whose model validates it and as
  a `CheckUse` when a check kind has that name; each decoded model is walked like a typed one, so a plugin
  curve assigned to a gfx property, an overlay with a `style` passed as a dynamic default and a target in
  a closure all count. A string in a `ctl.dynamic` input or in a `Binding` of kind `literal` that names a
  registered preset counts as that preset in every category that has the name (viewports the governing
  session defines excepted), because the plan cannot tell how the dynamic body uses it. Built-in (`eks`)
  contributions are listed with an empty plugin version, since a harness release is covered by executor
  versions. A viewport the governing session defines itself (`SessionSpec.viewports`: the plan session, or
  the take's for nodes in a take), a persona under a session with a `seed` (seed personas come first, as
  in the validator's `persona.unknown`) and an unknown name are not presets. A node with no contributions
  adds nothing, so its hash is unchanged; a node whose op is unknown or whose args do not validate adds
  `"kinds": null`. The walker is `eks_harness.ir.contributions` (`contributions(value, registry, op=,
  scope=)`, `node_contributions(node, registry, scope=)`, `SessionScope(viewports, seeded)` with
  `session_scope(session)` and `walk_scoped(nodes, scope)` following takes, `Contribution(category, kind,
  version, plugin, plugin_version, value)`), shared with the requirement finalizer (5.7).
  `stored_clock_payload(plan, node, registry)` computes the same payload, kinds and presets included, from
  the stored hashes (the validator's per-node `plan.hash` comparison uses it).
* `plan_hash(plan, registry=None) = digest(canonical({"schema", "kind", "params", "clock" (with
  `tempo.source` replaced by the source node's hash, so renames keep the hash), "session",
  "resources": sorted resource hashes, "nodes": [root node hashes in order], "outputs": outputs',
  "personas", "requires", "base", "presets"}))`, where `presets` (`session_presets(plan, registry)`) is the
  sorted `[category, name, value digest, plugin, plugin version]` of `plan.session.viewport`, `pace` and,
  for a session without a `seed`, `persona` when they name registered presets. `name`, `tags`, `source` and `hash` are excluded, so renaming a
  flow keeps its hash and so does moving code around without changing it.
* Consequences relied on by tests: the same source and params give byte-identical plan JSON and the same
  hash; blank lines, comments and unrelated edits change neither ids nor hashes; changing an argument
  changes the hash of that node and of every node after it in its lane.

### 7.4 Ids and seeds

* `b32(data, length)`: the first `5 * length` bits of `data` as lower-case Crockford base32 (`CROCKFORD`).
* `derive_id(prefix, key, length=10) = prefix + b32(sha256(key.encode()).digest(), length)`.
* `resource_id(kind, identity) = derive_id("r", canonical_json({"kind": kind, **identity}).decode(), 7)`
  (identity values that are `None` are dropped first).
* `derived_seed(root, path) = int.from_bytes(sha256(f"{root}:{path}".encode()).digest()[:8], "big") &
  (2**63 - 1)`; every node, effect and curve without an explicit seed gets
  `node_seed(clock.seed, node, field path) = derived_seed(clock.seed, node.hash + "/" + field path)` at run
  time. The node hash covers the root seed and the node's content but not its id, so renaming an entry or
  editing the call text keeps every derived seed, and a content-addressed cache keyed on the node hash
  serves a render only for the same seeds.

## 8. Registry and extensions (P5, `eks_harness/ir/registry.py`, `entries.py`, `presets.py`)

### 8.1 Specs and the store

A process-wide `REGISTRY` holds the built-ins and whatever is registered outside a tree; each tree plans
against its own registry (section 9.3). Spec dataclasses (skeleton): `OpSpec`, `MacroSpec`, `ResourceSpec`,
`CheckSpec`, `CatalogSpec` (categories `effect`, `transition`, `easing`, `curve`, `overlay`, `target`,
`marker`), `RendererSpec`, `BackendSpec`, `BackendPolicySpec`, `PresetSpec`, `EntrySpec`, each with a
`Provenance(plugin, version)`.

Provenance: models and functions defined in `eks` or `eks_harness` modules get `("eks", <harness
version>)`; inside `contributing(plugin_id, version)` (discovery wraps every plugin module import in it)
they get the plugin; otherwise `("project", "")`.

Conflict policy (P5 implements it in every `register_*`): registering a key again with the same object, or
with an object of the same module and qualname (a reload), replaces the entry; any other duplicate raises
`RegistryError` naming both definitions. Keys: op name, macro `namespace.name`, resource kind, check kind,
`(category, kind)`, renderer media kind, backend name, policy name, `(category, name)` presets,
`(kind, name)` entries. Presets carry `PresetSpec.source` (`module:qualname` of the preset function) for this
check; modules imported through a plugin `ImportScope` compare by plugin id and module tail, so a changed plugin
re-imported under a new scope generation replaces its entries. `Registry.drop_module(module, plugin=None)` removes
every spec of every table defined in that module (with `plugin`, scoped modules compare by plugin id and module tail,
so any scope generation matches); `load_file` calls it before it executes a file again and `load_plugins` before it
re-executes a plugin module, so renamed or removed definitions never conflict with or outlive their old version.
`Registry.rollback(state, module, plugin=None)` restores a snapshot for the specs of that module only and keeps what
other modules registered meanwhile; both loaders call it when execution raises. `Registry.drop_entries(module)` removes
the entries of one module. The plugin ids `eks` and `project` are reserved: `contributing()` raises `RegistryError`
for them and `parse_manifest` raises `ManifestError`.

Naming: op names are `family.name`, both segments `^[a-z][a-z0-9_]*$`; only `eks` provenance may register
ops in a built-in family (plugins and project code may not) (`ui`, `web`, `mobile`, `api`, `session`, `backend`, `net`, `inject`, `capture`,
`overlay`, `expect`, `check`, `fake`, `debug`, `data`, `media`, `edit`, `audio`, `gfx`, `blender`,
`scene`, `score`, `ctl`). Catalog kinds and check kinds are `^[a-z][a-z0-9_]*$`.

Lookups raise `RegistryError` with the closest known names (difflib) as suggestions. Listing methods
return sorted lists. `target_kinds()` builds the `TargetKind` mapping for `eks_harness.ir.target`.
`entry_for(fn)` reads `fn.__eks_entry__`. `versions()` maps plugins to versions. `snapshot()` and
`restore()` copy every table (tests). `adopt(contributions)` stores `(table, key, spec)` rows another registry
recorded for a plugin module (conflict policy as above). `rebase(base)` makes the registry's base layer the
current tables of `base`: every spec the registry holds that is not the previous base's spec under that key
(its own registrations) is kept over the new base, every other row follows `base`. `OpSpec.device_dependent`
(`@eks.op(device_dependent=True)`) marks an op whose output bytes depend on the executing device or model
build (neural inference); `CheckSpec.device_dependent` (`@eks.check(device_dependent=True)`, family `media`
only) does the same for a media check whose result depends on the machine; see 7.3.

### 8.2 Decorators

| decorator | decorates | registers |
| --- | --- | --- |
| `@eks.op(family, name=, out=, version=, description=, location=, executor=, executor_version=, pure=, idempotent=, device_dependent=, ordering=, platforms=, resources=, container=, branches=, exports_body=, pinnable=, recording_only=, bounded=, examples=)` | an `eks.Args` model | `OpSpec`; name defaults to the snake case class name; `executor` is stored as `"module:qualname"` and only resolved by the engine (M1); `examples` are JSON argument dicts that must validate against the model (`RegistryError` otherwise), stored in `OpSpec.examples` and emitted as JSON Schema `examples` by `op_schema`. Every built-in op declares at least one validated example. Every argument field of a built-in op, check, catalog kind and the models they reach carries `Field(description=...)`; `op_schema` and `eks-harness ops <name>` show it per argument. The built-in families are `api`, `audio`, `backend`, `blender`, `capture`, `check`, `ctl`, `data`, `debug`, `edit`, `expect`, `fake`, `gfx`, `inject`, `media`, `mobile`, `net`, `overlay`, `scene`, `score`, `session`, `store`, `ui`, `web` (`BUILTIN_FAMILIES`); `store` has no ops in M0 and is reserved so plugins cannot take it |
| `@eks.macro(name=, namespace=, platforms=, description=, version=)` | `def m(s: eks.Session, ...)` | `MacroSpec`; namespace defaults to the plugin id with dots and dashes as underscores, or `project` for project code; a namespace (and an op family) must not be a session member listed in `SESSION_MEMBERS` (`app`, `platform`, `spec`, `ctx`, `op`, `call`, `macro`, ...), which `s.<name>` would resolve first (`RegistryError`) |
| `@eks.resource(kind, parent=, phase=, keyed=, positional=, persona=, description=, version=)` | an `eks.Resource` model | `ResourceSpec`; a root kind named in `RESERVED_ROOT_KINDS` (`add`, `create`, `extend`, `now`, `seed`: members of `eks.Data`) or a child kind named in `RESERVED_CHILD_KINDS` (`expr`, `key`, `kind`, `path`, `ref`: members of `ResourceHandle`), both in `eks_harness.ir.ops.data`, raises `RegistryError` naming the member that would shadow it; a `create` method on the model (`def create(self, ctx)`, the design 7 creation hook the M3 seed engine calls) is recorded as `ResourceSpec.create` (`module:qualname`); a static or class method, or one needing more than the context, raises `RegistryError`; models without one keep `create=None` until M3 |
| `@eks.check(kind, family=, platforms=, default=, description=, version=, impl=, device_dependent=False)` | an `eks.Args` params model | `CheckSpec`; `impl` (a callable or a reference: `module:qualname` or a worker script path ending in `.js`, `.mjs` or `.ts`) is stored as `CheckSpec.impl` and resolved by the engine (M1/M2); `device_dependent=True` (media checks only, `RegistryError` otherwise) keys every `check.media` of the kind per machine, and a params model with a `pinned()` method keys the nodes whose `pinned()` is false (7.3) |
| `@eks.effect`, `@eks.transition`, `@eks.easing`, `@eks.curve`, `@eks.marker` | a catalog model with `kind: Literal[...]`, subclassing `EffectBase`, `TransitionBase`, `EasingBase`, `CurveBase` or `MarkerBase` (`RegistryError` otherwise); each is used bare (`@eks.marker`) or called (`@eks.marker(description=, version=)`); a marker kind is a source of `eks.media.markers(media, *sources)`, which adds its plugin to `requires` | `CatalogSpec` |
| `@eks.overlay_kind(live=, screenshot=, video=, platforms=)` | an overlay model (subclass of `OverlayBase`) | `CatalogSpec` with `renderers` |
| `@eks.target_kind(kind, surfaces=, shorthand=, resolvers=)` | a target args model | `CatalogSpec` category `target`; `resolvers` maps a declared surface to its resolver (a callable or a reference as for `impl`), stored as `CatalogSpec.resolvers`; a resolver for an undeclared surface or an empty reference raises `RegistryError` |
| `@eks.renderer(media_kind)` | a class or function | `RendererSpec`; a plan carries it as `media.source` `renderer` (10.4), which puts its plugin into `requires` and its version into the hashes |
| `@eks.backend(name)`, `@eks.backend_policy(name)` | classes implementing `eks_harness.plugins.sdk.Backend` (or `ManagedBackend`) / `BackendPolicy` | specs; the decorators are typed `Callable[[B], B]` with `B` bound to those protocol classes, so pyright rejects other objects, and registration raises `RegistryError` for a non-class or a class missing protocol members (listed) |
| `@eks.preset(name, category=)` | a zero-argument function returning a `PresetModel` | `PresetSpec` (the function is called once) |
| `@eks.flow`, `@eks.seed`, `@eks.edit`, `@eks.scene`, `@eks.score` | entry functions | `EntrySpec` (section 8.4) |

All decorators return what they decorate unchanged (macros too; macro calls are routed through the
session, section 10.1), so pyright keeps the original signatures. Plugin ops are called with
`s.call(Model(...))` (typed by the model constructor) or `s.op("family.name", **args)`; macros with
`s.macro(fn)(*args, **kwargs)` (typed: `Session.macro[**P, R](fn: Callable[Concatenate[Session, P], R]) ->
Callable[P, R]`, it looks the function up with `Registry.macro_for(fn)` and records the same `ctl.macro`
container, version and `requires` provenance) or the untyped shorthand `s.<namespace>.<name>(...)`; seed
resources with `d.add(Model(...), key=, parent=)` (typed by the model constructor) or the shorthand
`d.<kind>(...)`.

### 8.3 Polymorphic fields (`Poly`)

Extensible unions are declared as `Annotated[SerializeAsAny[Base], Poly(category)]`. Validation of a dict
dispatches on its `"kind"` to the registered model of that category (`RegistryError`, a `ValueError`, when
unknown); model instances pass through. Serialization dumps the concrete model. JSON Schema is a `oneOf`
of every registered model of the category at generation time. Used for effects, transitions, easings,
curves, overlays and marker sources.

### 8.4 Entry decorators (`entries.py`)

Implemented in the skeleton; P5 owns hardening. They validate options into `FlowOptions`,
`SeedOptions`, `EditOptions`, `SceneOptions`, `ScoreOptions` and register an `EntrySpec` with the
function, module, qualname, source file and first line, and set `fn.__eks_entry__ = (kind, name)`.
Names default to the function name and match `^[A-Za-z_][A-Za-z0-9_-]*$`.

| decorator | signature (keyword-only after `name`) |
| --- | --- |
| `flow` | `flow(name=None, *, app, platform=None, platforms=None, seed=None, as_=None, backend=None, viewport=None, pace=None, timeout=None, noise=(), checks=(), timezone=None, locale=None, tags=(), now=None, root_seed=None, description="")` |
| `seed` | `seed(name, *, backend, now=None, seed=0, base=None, tags=(), description="")` |
| `edit` | `edit(name=None, *, fps, size, seed=0, duration=None, window=None, background=None, tags=(), description="")` |
| `scene` | `scene(name=None, *, kind, engine=None, samples=None, device=None, seed=None, fps=None, size=None, duration=None, bpm=None, entry=None, transparent=True, cpu=False, platform=None, tags=(), description="")` (`cpu=True` only for `kind="gfx"`: `gfx.render` with `cpu=True`, the CPU reference renderer; `platform` only for `kind="blender"`: the `BlenderRender.platform` pin, `os-arch` such as `linux-x86_64`; `device="CPU"` with an engine that resolves to EEVEE or Workbench, the default `eevee` included, raises `RegistryError`) |
| `score` | `score(name=None, *, fps, song=None, window=None, size=(1080, 1920), seed=0, bpm=None, meter=None, duration=None, tags=(), description="")` |

Flow `checks=` takes check kind names, `eks.checks.use(...)` values or registered check params models (stored as
`CheckUse`s, normalized with `eks.checks.to_use` when the plan is traced). On flows `seed` is the scenario name and `root_seed` the random seed; on the other decorators `seed` is
the random seed. The first parameter of an entry function receives the root builder; a flow may not
declare a parameter named `platform`.

### 8.5 Presets

Categories and models (`presets.py`): `viewport` (`Viewport`), `pace` (`Pace`), `network`
(`NetProfile`), `style` (`OverlayStyle`), `persona` (`PersonaPreset`). Built-ins registered when the
module is imported: viewports `desktop` 1920x1080, `phone` 390x844 at 3x with touch, `tablet` 820x1180 at
2x with touch (already in the skeleton); pace `demo` (move 300ms, dwell 400ms, type 40ms per character,
hold 700ms, screen hold 1500ms, press 250ms, the values of today's demo pace; it is the only pace preset,
takes run without pacing); network `wifi` (2ms, unlimited), `4g` (60ms, 9000/9000 kbps), `3g` (563ms,
1440/675 kbps, Chrome's Fast 3G), `slow-3g` (2000ms, 400/400 kbps, Chrome's Slow 3G), `offline`; overlay styles `kb` and `review` with the values
of `eks_harness/annotate/spec.py` `BUILTIN_STYLES` mapped to the snake case fields.

Plans store presets by name, so their values are hashed (7.3): a node that names a registered preset
carries `digest(canonical_json(PresetSpec.value))` and the preset's provenance in its context payload, and
the plan hash carries the session's viewport and pace presets (and its persona preset when the session has
no seed) the same way. Changing a preset's values or its plugin's version changes the hashes of the nodes
that use it; a plugin preset also puts its plugin in `Plan.requires` (5.7). Persona presets count only
where no seed supplies personas; registering a preset under a name the plan already used while it was
unknown changes the hash too, so such a plan is planned again.

### 8.6 Plugins (P5)

`harness-plugin.toml` keeps `[plugin]`, `[requires]`, `[config]` and trust and isolation as they are, and
gains `modules = ["pkg.module", ...]` in `[plugin]` (`Manifest.modules: tuple[str, ...]`, validated as a
list of dotted module names; the old `[contributes]` tables keep parsing until M7). `eks.discovery.
load_plugins(tree)` imports, for every active (trusted, enabled) plugin with modules, each module through
the plugin's `ImportScope` (or plain import for built-in and package plugins) inside
`contributing(plugin.id, plugin.version)`, so everything it registers carries its provenance. Import errors
raise `DiscoveryError` naming the plugin and the module. A module is executed again when its scope generation
changed or the registry lost any of its contributions: first its earlier contributions are dropped
(`drop_module` with the plugin id), then every submodule that made them is reloaded before the module itself; a
failed import rolls the plugin's modules back. Loaded modules are recorded per registry: when another
registry (another tree) already imported the same module object, its recorded contributions are adopted
(`Registry.adopt`) instead of executing the module again, so the other tree keeps its specs.

## 9. Planning and discovery

### 9.1 `eks.plan` and `trace` (P4, `eks/planning.py`)

* `plan(entry_or_fn, /, **params)`: resolves the `EntrySpec` (`registry.entry_for`), takes the reserved
  keyword `platform` out of `params` for flows, coerces params (`coerce_params(..., text=False)`) and
  calls `trace` with `tree=entry_tree(entry)`, so `Plan.base`, node ids and the plan hash equal those of
  `plan_spec` for the same source and params (a seed's snapshot key does not depend on the entry point).
* `trace(entry, *, params=None, platform=None, tree=None, registry=None, root=None)`:
  1. registry defaults to `default_registry()`;
  2. flows: `app = flow_app(entry, tree)` (`find_app(tree, options.app)` when there is a tree) and
     `platform = choose_platform(entry, platform, app)`: `resolve_platform` with the app's platforms;
     `None` raises `PlanBuildError` asking for `platform=` (or `--platform`); then
     `check_persona_params`: every parameter annotated `eks.Persona` (also `eks.Persona | None` and
     `Annotated[eks.Persona, ...]`, `persona_params(entry)`) whose value (given or default) is not a persona of
     the session's seed (`eks.workspace.seed_personas`) raises `PlanBuildError` naming the parameter and the
     seed's personas; a flow without a seed, or a seed the registry does not know (the validator reports
     `persona.seed`), is not checked here;
  3. `ctx = TraceContext(kind, name, registry, entry, clock=base_clock(entry), session=session_spec(entry,
     platform) for flows, params, tree, entry_code=entry.fn.__code__)`;
  4. media parameters (annotated `eks.Media`) given as strings are recorded as `media.source` nodes
     (`normalize_media_uri`, `id_key="param:<name>"`) before the call and passed as `Ref`s; `Plan.params`
     keeps the strings as given;
  5. `builder = (root or root_factory(kind))(ctx, entry)`; flows run the call inside
     `builder.activate()`; `ctx.set_outputs(entry.fn(builder, **params))`; return `ctx.finalize()`.
     Before the entry runs, `trace` registers the requirement finalizer (5.7,
     `ctx.on_finalize(_require_contributions, order=REQUIRES_FINALIZE_ORDER)`). It uses only public
     `TraceContext` API and requires the plugin of every contribution reached by the plan: plugin ops
     (`s.op("acme.x", ...)`), target kinds used through shorthand strings (`"aria=Kaydet"`), `by.custom`,
     targets inside check params and targets bound into an `eks.dynamic` function (closure variables and
     parameter defaults), catalog kinds and overlays, media and checkpoint check kinds, the presets named
     by `s.checkpoint(viewport=)`, `s.shot(viewport=)`, `s.record(pace=, viewport=)`, `s.ui.type(pace=)`,
     `s.web.viewport(...)`, `s.net.shape(...)`, overlay `style=` (in `eks.annotate` and edit tracks), take
     sessions, and persona presets named by `@eks.flow(as_=)`, `s.as_(...)`, `as_=` of the `s.api` calls
     and take sessions when the governing session has no seed, and the flow session's viewport and pace
     presets and `SessionSpec.checks` (from `@eks.flow(checks=)` and the app profile's `[defaults]
     checks`). Built-in and project contributions add nothing (`ctx.require` skips them). Targets a
     dynamic function builds inside its body exist only at run time and are not part of the plan.
     The entry function receives the coerced values unchanged; `TraceContext(params=...)` (and so
     `Plan.params`) gets a copy with every `enum.Enum` member replaced by its `.value`, recursively.
* `ROOT_FACTORIES` maps kinds to `"module:function"`; `root_factory(kind)` imports it.
* `coerce_params(entry, values, text=False)`: reads the types of the given parameters only with
  `typing.get_type_hints(fn, include_extras=True)` (when that fails, for example on a TYPE_CHECKING-only
  name in the first parameter or the return annotation, each given parameter's annotation is evaluated on
  its own, and only a failure there raises `PlanBuildError`), skips the first parameter, fills defaults, raises
  `PlanBuildError` for missing required and unknown parameters (listing the known ones). With `text=True`
  every value is a string parsed by annotation: `str`; `int`; `float`; `bool` (`true/false/1/0/yes/no/
  on/off`); `Time` (`coerce_time`); `Target` (`parse_target`); `eks.Media` (kept as the string, see
  step 4); `Literal`/`Enum` (membership); `list[str]` (comma separated); anything else is parsed as JSON.
  Every parsed value except `Time`, `Target` and `eks.Media` is then validated with
  `pydantic.TypeAdapter(annotation)` on the full annotation, so `Annotated` constraints apply in both modes.
  With `text=False`, values are validated with a
  `TypeAdapter` except `Time` (must be a `Time`), `Target` (`Target` or shorthand string), `eks.Media`
  (a `Ref`, an `Encodable` or a string) and parameters annotated `Any` or unannotated (taken as they
  are).
* `resolve_platform(entry, requested, app_platforms=())`: candidates are the flow's platforms, else the
  app's; a requested platform must be a candidate when there are candidates (`PlanBuildError`); without a
  request the only candidate is used; otherwise `None`.
* `session_spec(entry, platform, registry=None, *, profile=None)` copies the flow options into
  `SessionSpec(key="main", ...)`, merged over the app profile's session defaults (`profile` is the merged
  `app.toml` tables, `AppInfo.tables`) by `merged_session`: the flow's `viewport`, `pace`, `timeout`,
  `timezone` and `locale` win when set; `noise` is the profile's then the flow's, without repeats; `checks`
  are the profile's checks whose kind the flow does not name, then the flow's, each normalized with
  `eks.checks.to_use` (unknown kinds raise `PlanBuildError`). `eks.profiles.session_defaults(tables,
  platform, app)` reads `[defaults] timeout, pace, viewport, noise, checks` (checks are kind names or
  `{kind, params}` tables), `[checks] noise` (a list, or one string split at `|`), and from the platform
  table (`[web]` for web, `[mobile]` for ios and android) `timezone`, `locale`, `network_ignore` (list or
  `|` string) and `viewports` (`[web.viewports.<name>]` tables, validated as `Viewport`); a wrong type raises
  `PlanBuildError` naming the key. Takes get the same defaults for their app (`sc.take(viewport=)` wins).
  `inherited(registry, seed, now=, root_seed=, backend=)` fills what the flow (or take) leaves unset from the
  registered seed it names: `now`, the root seed (`SeedOptions.seed`) and the backend definition; values the
  flow sets win. `SessionSpec.now`, `SessionSpec.root_seed` and `SessionSpec.backend` carry the result, and
  so does the backend resource's `definition`. `seed_plan(entry)` traces a seed in its own tree
  (`entry_tree`), so its plan hash does not depend on who plans it.
* `parse_sets(items)`: `--set` items `KEY=VALUE` (split at the first `=`, key stripped, value kept as is)
  into a dict; a missing `=`, an empty key or a repeated key raises `PlanBuildError`.
* `plan()` with a function that is not a registered entry, and `platform=` for a non-flow entry, raise
  `PlanBuildError`.
* `base_clock(entry, registry=None)`: flow `Clock(now, seed=root_seed or 0)` after `inherited`; seed `Clock(now=o.now,
  seed=o.seed)`; edit `Clock(seed, fps, duration, window)`; scene `Clock(seed, fps, duration,
  tempo=TempoMap(bpm=o.bpm) if o.bpm else TempoMap(external=True))`; score `Clock(seed, fps, window,
  duration, tempo=TempoMap(bpm=o.bpm, meter=Meter(*o.meter)) if o.bpm else None)`.

### 9.2 Discovery (P5, `eks/discovery.py`)

* `split_spec("path/file.py::name")` -> `(Path, "name")`; without `::` the name is `None`.
* `module_name(path)`: `eks_entry_<12 hex of sha256(resolved path)>_<stem with non-identifier
  characters as _>`.
* `load_file(path)`: imports the file under `module_name(path)` with
  `importlib.util.spec_from_file_location` (so `linecache` sees the source and call sites hash) into the
  active registry (`default_registry()`) and returns the entries registered by that module. Its cache is kept
  per registry, so loading one file into two registries executes it once for each. It executes the file again when it was never loaded, when its
  content changed, or when the registry no longer holds every entry it registered (tests restore the
  registry); otherwise it returns the cached entries. Before executing it drops every spec of the module
  (`Registry.drop_module`); when execution raises, the error propagates unchanged and the registry, the module
  and the cache are back to the previous version (`Registry.rollback`). Missing files raise `DiscoveryError`.
* `resolve_entry(spec, kind=None, cwd=None)`: loads the file; with a name, the entry of that name (and
  kind); without one, the only entry of the file, else `DiscoveryError` listing them.
* `find_tree(start)`: `eks_harness.plugins.project.find_tree`.
* `discover(tree)`: `EntryFile`s for `.harness/{flows,seeds,edits,scenes,scores}/*.py` at the tree root
  and `*/.harness/flows/*.py` below it (skipping `node_modules`, `.git`, `.venv`, `__pycache__` and every other
  hidden directory; files starting with `_` are helpers, not entries).
* `load_tree(tree, kinds=None)`: `load_file` for every discovered file of those kinds.
* `find_app(tree, name)`: the `.harness/app.toml` whose own `[app] name` (or app root directory name) equals
  `name`; a name inherited through `[app] extends` does not count, and `[app] name` matches win over directory
  names. Returns `AppInfo(name, root, platforms, tables)`: the app's own name (else its directory name), its own
  directory as `root` (also for an app that extends another), the platforms of the merged profile
  (`eks_harness.drivers.profile.load_profile`) and the merged profile tables (`tables`, not compared);
  `None` when there is none.
* `discover` and `find_app` do not descend into a directory with its own `.harness/project.toml`: a nested
  project is another tree.

### 9.3 Workspace (P11, `eks/workspace.py`)

* Every tree plans against its own registry. `eks.discovery.tree_registry(tree, plugins=True)` returns the
  registry cached for the resolved tree root and the `plugins` flag (`plugins=False` has its own registry, so
  a plugin loaded for one never shows in the other), rebased (`Registry.rebase`) on the process registry on
  every call: the built-ins and anything registered outside a tree, plus the tree's own plugins and entry
  files. Another tree's plugins, seeds, scenes and entries are never visible, so two copies of one project
  (worktrees) plan in one process with the same hashes, and plan validity, `requires`, preset digests and
  hashes do not depend on what the process planned before.
* `workspace_registry(tree, plugins=True)`: `tree_registry(tree, plugins=)` (the active registry when `tree`
  is `None`) with, inside `using_registry`, the tree's active plugins loaded (when `plugins`) and every seed
  and scene of the tree loaded (so personas and scene names resolve).
* `plan_spec(spec, params=None, platform=None, cwd=None, plugins=True)`: finds the tree, takes
  `workspace_registry(tree, plugins=)`, and inside `using_registry` resolves the entry, resolves a flow's
  platform from the flow and the app profile (`find_app`), coerces `params` with `text=True` and calls `trace`
  with that registry. Discovery and planning errors propagate unchanged.
* `trace` runs inside `using_registry(registry)`, and so do `Validator.validate` and the schema export, so
  polymorphic fields and target shorthand resolve against that registry. `eks.plan(entry)` and `seed_plan(entry)`
  without a registry plan an entry loaded from an entry file of a tree in that tree
  (`eks.planning.home_entry`: the file is loaded into `workspace_registry(tree)` and the entry of the same kind
  and name is traced there), so they give the plans of `plan_spec`; entries defined in imported modules stay in
  the active registry.
* `eks.validate(plan, registry=None, tree=None)` uses `registry`, else `workspace_registry(tree)` when a tree is
  given, else `default_registry()`; `hash_plan` without explicit executor versions reads the active registry,
  so hash a plan of a tree inside `using_registry(tree_registry(tree))`. The plan CLI validates with
  `workspace_registry(tree, plugins=not --no-plugins)`.
* `seed_personas(name)`: plans the registered seed `name` (cached by name and registry state) and returns
  its `personas` preceded by those of the seeds it extends (section 10.3), or `None` when no such seed is
  registered.
* `seed_info(name)`: the cached plan's `SeedInfo`. A cache entry stores every `(base, hash)` its
  `data.extend` nodes recorded and is used only while each base's current plan (checked recursively down the
  extend chain, with a guard for cycles) still has that hash, so reloading any seed further up the chain
  refreshes `SeedInfo.hash` and `seed.stale` stays exact in long-running processes.

## 10. Op family surfaces

### 10.0 Rules for every builder

* Signatures are fixed by the skeleton modules in `src/eks/`. A builder converts its inputs, records one
  op and returns the `Ref` (typed by the op's `out`; `Ref[None]` for ops without a value).
* Target-like parameters go through `ensure_target(value, what, registry.target_kinds())`; `Time`
  parameters through `ensure_time`; text parameters accept `str` (f-strings may carry refs) or a `Ref`.
* The mapping passed to `record_op`/`record` uses the argument model's field names and leaves out `None`
  values; `record` encodes and validates it.
* `at=` exists only on ops with `pinnable=True`; it becomes `Node.pin`. The validator rejects pins outside
  takes.
* Arg models are the skeleton classes in `eks_harness/ir/ops/*.py` (the op name, out type, platforms,
  resources and flags are in their `@op(...)` lines). The table below lists only what is not a one to one
  copy of parameter names.

### 10.1 Session and driving (P6)

| builder | op | notes |
| --- | --- | --- |
| `s.ui.nav(route, params)` | `ui.nav` | web: a path with query `params`; React Native: route name and params |
| `s.ui.back/reload/open_url/deeplink` | `ui.back`, ... | |
| `s.ui.tap/double_tap/long_press/hover/fill/type/clear/key/select/check/choose/swipe/drag` | `ui.<name>` | `select` value: a string or a sequence (tuple in args); `type(pace=)` names a pace preset |
| `s.ui.scroll(target, to=, by=)` | `ui.scroll` | `to` is an edge literal (`top`, `bottom`, `start`, `end`) or a target |
| `s.ui.text/value/attr/count/exists/visible/tree/url/route/state/layout` | `ui.<name>` | reads, `idempotent=True`; the target reads (`text`, `value`, `attr`, `count`, `exists`, `visible`, `layout`, `tree`) take `timeout=` to override the session default for that read |
| `s.ui.wait_for/wait_gone/wait_url/settle` | `ui.<name>` | |

Every `timeout=` and `hold=` of `s.ui.*` and the `timeout=`/`duration=` of `s.mobile.toggle/submit/idle/pointer` go through `optional_positive_duration` (an anchored, zero or negative `Time` raises `TimeError`), and the IR fields are `Duration`.
| `s.web.wheel/viewport/logs/clear_logs` | `web.<name>` | `viewport(name)` records `web.viewport` with `name` |
| `s.mobile.dispatch/patch/unpatch/mount/unmount/invoke/toggle/submit/idle/route_names/logout/close_dev_menu/pointer` | `mobile.<name>` | `invoke(target, prop, *args)` stores `args` |
| `s.mobile.state(path)` | `ui.state` | alias of `s.ui.state` |
| `s.op(name, **args)` | `name` | dynamic call of any op; a value of a field typed `Target` (alone, in a union or as list/tuple items) goes through `ensure_target(value, "s.op('<name>') <field>", registry.target_kinds())` like the builders, except a string equal to a `Literal` member of that field (the `ui.scroll` edges); mappings, refs and `None` pass as given; namespace op callables go through `s.op` |
| `s.call(model)` | the op whose args class is `type(model)` | typed call of plugin ops; `RegistryError` when no or several ops use that class |
| `s.<namespace>.<name>(...)` | macro or op | a registered macro `namespace.name` is invoked as below; else an op `namespace.name` is called with keyword args; else `AttributeError` with suggestions; names starting with `_` raise `AttributeError` |

Macro invocation: the macro's `platforms` must contain the session platform (`TraceError`); the call is
bound with `inspect.signature(fn).bind(session, *args, **kwargs)`, the bound arguments except the first are
`encode`d into `params`; the body runs inside `session.record_container("ctl.macro", {"name", "version",
"params"})` and the function's return value is returned unchanged (refs into a macro body are visible
afterwards because `ctl.macro` exports its body). Provenance of plugin macros goes to `ctx.require`.

`Session.__init__` and `open_flow_session` are implemented in the skeleton (the flow's persona becomes a
leading `session.as` node).

### 10.2 Evidence (P7)

| builder | op | notes |
| --- | --- | --- |
| `expect(target, timeout=).visible()/hidden()/enabled()/disabled()/checked(on)` | `expect.target` | assertion kinds `visible`, ...; session from `current_session()`; `timeout` overrides the session default for this op (design 4.6) and is a duration |
| `expect(target).text(equals, contains=, matches=)`, `.value(...)`, `.attr(name, ...)` | `expect.target` | at least one of equals/contains/matches, else `TypeError` |
| `expect(target).count(equals, at_least=, at_most=)` | `expect.target` | at least one bound |
| `expect(ref).eq/ne/contains/matches/gt/ge/lt/le/truthy/falsy/length` | `expect.value` | recorded through the active session when there is one, else `ctx.record`; `assertion_problem(kind, produced, compared)` (`eks_harness.ir.ops.check`) raises `TypeError` at trace time when the assertion cannot pass for the ref's type: `contains` and `length` need `str`, `list` or `json`, `matches` needs `str` or `json`, `gt`/`ge`/`lt`/`le` need `int`, `float` or `json` and a comparable value (number or json), `contains` on a `str` needs a `str` value; `eq`, `ne`, `truthy` and `falsy` take anything |
| `expect.soft(x, timeout=)`, `expect.eventually(x, timeout=)` | same ops | `soft=True`; `eventually=True` with the timeout; `expect(ref, timeout=)` and `expect.soft(ref, timeout=)` raise `TypeError` (a value is checked once, waiting is `expect.eventually`) |
| `s.explore(budget, steps=, depth=, seed=, allow=, avoid=, actions=, fail_on=, checks=, label=, soft=)` | `check.explore` | the exploration family of design 13: `budget` is a `Duration`; `allow`/`avoid` are lists of targets; `checks` are checkpoint-family checks run on every new screen; M6 adds the executor |
| `s.checkpoint(label, viewport=, checks=, shot=, full=, soft=)` | `check.checkpoint` | `checks=None` means the default set, which the plan pins: `SessionSpec.defaults` (resolved when the session spec is built by `eks.checks.default_checks(platform, checks, registry)`: every registered check with `default=True` of the `checkpoint`, `visual` or `perf` family (`CHECKPOINT_FAMILIES`) that runs on the session platform and whose kind the session's `checks` does not name, as `CheckUse`s in kind order with their params defaults filled in) followed by `SessionSpec.checks` (the app profile and flow or take options); the engine runs exactly these, never the registry's current defaults, so a plugin default check is in `requires` and the hashes and a plan without it loaded fails validation with `plugin.missing`; items may be a kind name, a `CheckUse` or a registered check params model instance (`checks.to_use`) |
| `s.shot(name, element=, focus=, full=, viewport=, hide=, at=)` | `capture.shot` | `hide` is a list of targets (shorthand allowed) hidden while capturing; a single target or string raises `TypeError` |
| `s.dump(kind, name=, viewport=)`, `s.har(name=, whole=)` | `capture.dump`, `capture.har` | |
| `with s.record(name, pace=, viewport=, pointer=, from_here=, focus=, trim=, hide=) as video:` | container `capture.record` | scope flag `record`; a record inside a record raises `TraceError`; `hide` as for `s.shot` |
| `s.hold(duration, at=, reason=)` | `capture.hold` | `TraceError` unless the scope has the `record` or `take` flag |
| `eks.overlay.title/caption/callout/spotlight/highlight/arrow/blur/redact/icon/image/step(...)` | none (values) | return overlay models; `at` as a `Span` is the window; `at` as a `Time` gives the window `[at, at + hold]`, or `[at, t.end]` without `hold` |
| `s.overlay.<kind>(...)`, `s.overlay.show(model, at=)` | `overlay.show` | live; `at` as a `Time` is the node pin; `at` as a `Span` pins at its start and sets the window |
| `s.overlay.remove(key)`, `s.overlay.clear(kind)` | `overlay.remove`, `overlay.clear` | |
| `eks.annotate(artifact, overlays, style=)` | `overlay.annotate` | pure, recorded with `ctx.record`, anchors are checked on the `screenshot` surface; the artifact must come from `capture.shot`, `check.checkpoint` or `overlay.annotate` (`TypeError`) |
| `s.debug.break_(label, when=)`, `s.debug.pause_on_error(on)` | `debug.break`, `debug.pause_on_error` | `when` is `Ref[bool] \| Ref[Json] \| None` like `eks.when`, so a `Ref[int]` (`s.ui.count(...)`, `.len()`) or `Ref[str]` is a type error and the validator's `ref.type` agrees |
| performance budgets | none | two check kinds of family `perf`: `perf` (platform web: `lcp`, `cls`, `inp`, `requests`, `bytes`) and `perf_rn` (platforms ios and android: `frame_drops`, `requests`, `bytes`); a web vital on a React Native flow is `check.platform`, a frame-drop budget in `perf` is `check.args` |
| `eks.checks.use(kind, **params)`, `eks.checks.to_use(value)` | none | build `CheckUse` validated against the check's params model; `Target` params go through `ensure_target` (10.0): `layout_spill` `ignore` is `tuple[Target, ...]` and `error_toasts` `selector` is `Target | None`, so shorthand strings work and `target.surface` checks them per platform |

### 10.3 Environment and data (P8)

| builder | op | notes |
| --- | --- | --- |
| `s.api.get/delete(path, query=, headers=, as_=, expect_status=)` | `api.get`, `api.delete` | `as_` -> `persona`; an int `expect_status` becomes a one-element tuple, every code must lie in 100..599 (`ValueError`; the model types items as `Annotated[int, Field(ge=100, le=599)]` and the tuple as non-empty); `query` is `Mapping[str, JsonLike]` |
| `s.api.post/put/patch(path, json=, ...)` | `api.post`, `api.put`, `api.patch` | `json` (`JsonLike`) -> the `json` field (alias of `body`) |
| `s.as_(persona, at=)` | `session.as` | (`eks.api.act_as`) |
| `s.backend.info` | `backend.info` | a property; every access records a node |
| `s.net.route(method, pattern, times=, at=)` then exactly one of `.delay(by)`, `.respond(status=, json=, body=, headers=)`, `.patch(lambda_path=, value=)`, `.abort(reason)`, `.passthrough()` | `net.route` | the terminal call records the node; a second terminal call raises `TraceError`; a route left without one raises `TraceError` at finalize naming its source line; `delay(by)` takes a duration (an anchored `Time` raises `TimeError`); `patch` refuses `value=None` (`TypeError`, and `PatchJson.value` rejects null) |
| `s.net.unroute(pattern)`, `s.net.shape(profile)` | `net.unroute`, `net.shape` | `profile` is a network preset name or a `NetProfile` |
| `with s.net.cassette(name, mode=):` | container `net.cassette` | a cassette inside a cassette raises `TraceError` (flag `cassette`) |
| `s.inject.script(source, when=)`, `s.inject.style(css)`, `s.eval(source, arg)` | `inject.script`, `inject.style`, `inject.eval` | `when='init'` exists only on web sessions; elsewhere `inject.script` raises `TraceError` |
| `s.fake.arm(slot, mode=, media=, fixture=, data=, at=)`, `s.fake.clear(slot)` | `fake.arm`, `fake.clear` | `media` is a `Media` ref (`eks.media.source(...)`); `fixture` names a file of the app's fixture dirs |

Seeds (`eks.Data`, `open_data`): the seed declares one backend resource
`ctx.resource("backend", definition=<options.backend>, seed=<seed name>)` and records every resource with
`ctx.record(..., uses=[backend])`.

* `d.<kind>(...)` creates a root resource (a kind whose `ResourceSpec.parent` is `None`);
  `handle.<kind>(...)` a child of a kind whose parent is `handle.kind`. Unknown or misplaced kinds raise
  `AttributeError` with the kinds that fit.
* Call convention: a keyed kind takes its key as the first positional argument (required); further
  positionals map to `ResourceSpec.positional` names in order; keywords are fields. Fields are validated
  with the resource model after `encode` (handles encode to refs of their node) and recorded as its full
  `model_dump(mode="json", by_alias=True)`: defaults included and an explicit `None` kept.
* Path: `"{kind}:{key}"` for keyed kinds, `"{kind}#{n}"` for keyless ones (n counts keyless siblings of
  that kind under the same parent from 0); children prefix the parent path and `/`. The tracer and the
  validator build paths with the same helpers of `eks_harness.ir.ops.data`: `resource_segment(kind, key,
  index)`, `resource_path(parent, segment)` and `keyless_position(path)` (`(parent, kind, n)` of a keyless
  path, else `None`).
* Node: `data.create` with `{kind, key, path, parent: <parent ref>, fields, phase}` and `id_key=path`, so
  seed node ids depend on resource paths only.
* `d.extend(base)` also learns the base chain from the planned base (`eks.data.BaseResource(base, origin,
  kind, key, path, persona)` for every `data.create` of the base and of its own bases, recursively): a
  resource path or persona key the chain creates cannot be declared again (`TraceError` naming the base
  and `d.base(path)`), and keyless children under a base resource continue its sibling count. `d.base(path)`
  returns a `ResourceHandle` bound to a base resource (cached per path; `TraceError` without an extend, for
  a path no base creates, with close matches, or outside the root scope): it records `data.base` with
  `{base, kind, key, path}`, `out=resource:<kind>`, `uses=[backend]` and `id_key="data.base:" + path`, and
  appends the `data.extend` node of that base to its `after`. Children (`handle.<kind>(...)`) and reference
  fields take it like any handle.
* Persona kinds (`persona=True`) add their key with `ctx.add_persona`; a persona key declared at two paths
  raises `TraceError`. Plugin kinds add their provenance with `ctx.require`.
* Resources are declared only in the seed's root scope; a declaration inside `eks.each`, `eks.when` or
  `eks.parallel` raises `TraceError` (copies would share a path), so loops are plain Python `for`.
* `d.extend(base)` records `data.extend` with `{base, hash}`, `hash` being the base seed's plan hash (planned in the base's own tree, `eks.planning.seed_plan`), so the extending seed's plan hash, its snapshot key, covers the base; an unknown base or an extend cycle leaves `hash` out and the validator reports `seed.unknown` or `seed.cycle`; `d.create(kind, key, parent=, **fields)` is the generic form and `d.add(model, key=, parent=)` the typed one (the registered kind is found by the model class, `TraceError` when none or several; reference fields take `handle.expr`, the handle's `RefExpr`, and are kind-checked like handles);
  `d.now` and `d.seed` read the clock.
* `d.extend` is called only in the seed's root scope (`TraceError` inside `eks.each`, `eks.when`,
  `eks.parallel` or `@eks.dynamic`) and before the seed's first resource (`TraceError` after one); extending
  the same base again returns the first ref. The engine runs every `data.extend` of a seed before any of its
  `data.create` nodes (no `after` edge encodes this).
* A seed's personas include those of the seeds it extends: `seed_personas` (section 9.3) prepends the
  personas of every `data.extend` base, resolved recursively with a guard against cycles, keeping order and
  dropping duplicates.

### 10.4 Media, edits and audio (P9)

* `media.source` kinds are `video`, `audio`, `image`, `file` (any other single file: a `.blend` scene, a
  font, a LUT) and `bundle` (a web scene entry: the directory around it). Every file an IR field reads is an
  `RMedia` ref to such a node, so the render's `after` edges and Merkle hash cover it: Blender `LoadNode.path`
  and `TextNode.font` (`file`), `World.hdri` (`image`), `WebScene.entry` (`bundle`), the `template` media
  check (`image`), the overlay `Image.image` (`image`; the builder is `image(anchor, media, ...)`), the
  effects `color_grade(lut=)` and `lut` (`file`) and `watermark` and `sticker` (`image`). The builders take a
  `MediaLike` and record the source in the active trace.
* Fonts (`eks_harness.ir.types.Font = Annotated[RefExpr | ItemExpr | ShippedFont, RefKind("media")]`): the
  gfx `TextLayer.font` and the `font` of `fx.text`, `fx.lower_third` and `fx.captions` hold a `$ref` to a
  `media.source` of kind `file` or the name of a family the renderers ship (`ShippedFont`: `IBM Plex Sans`,
  the default, and `IBM Plex Mono`, covered by executor versions). The builders take a `FontLike`
  through `eks.media.as_font(value, what)`: a shipped family stays a name, a string with a font extension
  (`FONT_EXTENSIONS`) or a media URI and a media `Ref` become a `file` source, and any other family name
  raises `ValueError`. Overlay styles (`StyleFonts`, a preset value that cannot hold refs) name only
  shipped families and font files (`ShippedFontFile`).
* `media.source` `uri` is `artifact:ID`, `artifact:ID@sha256:HEX` (pinned content, 7.3), `file:`,
  `plugin:ID/path`, `pkg:MODULE/path` (5.3) or `http(s):`. `renderer` names the `@eks.renderer` media kind
  that turns the source into frames: `eks.media.source(uri, kind=None, origin=None, renderer=None)` sets it
  from the file extension when a loaded renderer has that media kind, and an explicit `renderer=` must be
  registered (`TraceError`).
* `fx.captions(source)` names the `STTMarkers` source (`WordSource`, 2.7) whose words it shows; a
  transcript file is analysed with `eks.media.markers(media, STTMarkers(name=...))` first. `STTMarkers`
  takes `model`, `device` (`cpu` or `cuda` for faster-whisper and whisperx, `cpu` or `mps` for
  mlx-whisper; another device is a `ValueError`) and `compute_type`; only with all three is it pinned (7.3).
* `eks.media.markers(media, *sources)` (and `AudioTrackBuilder.beats`/`markers`, which call it) raises
  `TraceError` when a source's `name` or one of its `stream_names()` is already provided by another source
  of the same call or of an earlier analysis in the trace, or when a stream name is one of the clock tempo
  streams (`CLOCK_STREAMS`: `beat`, `downbeat`, `bar`, `kick`, `snare`; `MarkerBase.name` rejects them with
  a `ValueError`). Analyses recorded while a scene is embedded (`embed_scene`) share the plan-wide namespace,
  so a scene embedded more than once takes its analysis names as inputs and the error says so.
* `e.expect.text(text, at=, region=None, engine="auto", languages=("tr-TR", "en-US"), ...)` and
  `e.expect.safe_area(..., engine="auto")` set the OCR engine (`auto`, `vision`, `tesseract`) of the
  `text` and `safe_area` media checks; `TextCheck.languages` are the language tags the engine reads.
* `eks.media.tts(text, voice="default", backend="kokoro", language=None, speed=1.0, seed=None)` records
  `media.tts` (speech audio) and `eks.media.stems(media, *stems, model="htdemucs")` records `media.stems`
  (the chosen separated stems mixed into one audio file); both return `Media` usable as clips, audio tracks
  or word sources. `e.scene(scene, **inputs)` and `e.gfx(scene, **inputs)` embed scenes into an edit like
  `sc.scene`/`sc.gfx` (solid color clips and text cards are gfx scenes, HTML overlays web scenes).
* `eks.media.source(uri, kind=None, origin=None)` (and `as_media(value, kind=None, origin=None)`) records
  `media.source` once per normalized uri and kind within a trace, a relative `file:` path rebased on
  `Plan.base` from the file that wrote it (7.1): a later call returns the same `Ref` wherever that node is visible (its own scope or a scope
  nested in it); only a source first recorded inside a `when`/`each`/`dynamic` body that has closed since
  is recorded again where it is used next. `as_media(value)`: a `Ref` of type media or json as is, an
  `Encodable` through its encoding, a string through `source`. `markers(media, *sources)` records
  `media.markers`.
* `eks.fx`, `eks.tx`, `eks.curves`, `eks.ease`, `eks.keys` build catalog values without tracing.
  `ease(name)` accepts registered easing kinds and `LEGACY_NAMES`; `keys({time: value, ...}, ease=)`
  builds `Keys` in insertion order with `ease` on every keyframe after the first;
  `fx.zoom(start, end=None, ease=)` is a constant `start`, or keys from `t.start` to `t.end`
  (clip-local); `animated(curve)` wraps a curve in `Driven`. Lengths (transition and envelope durations,
  holds, periods, `tau`, attack, release, fades, marker minimum durations, timeline and mix durations) are
  the IR type `Duration`: positive and anchor-free (`t('8f')` and `t('1b')` are lengths, `t('0s')` and
  `t('beat:8')` are not); builders raise `TimeError`, the models `ValueError`. `Length` (same module) is the non-negative variant for lengths where zero means something (take `lead_in` and `tolerance`, media check `tolerance`, black and frozen `longest`, pace and network presets). The other timeouts and holds of every op (`ui.*`, `mobile.*`, `expect.*`, `capture.hold`, overlay `hold`, perf `lcp`/`inp`, `SessionSpec.timeout`, scene and gfx durations, `check.explore` budget) are `Duration`. Both types raise typed pydantic errors (`duration_anchor`, `time_negative`, `duration_zero`) that the validator maps (section 11); `LengthBound` metadata marks them and `length_bound(annotation)` finds it. `fx.custom`, `tx.custom`, `curves.custom`
  build plugin kinds and add their provenance to the active trace.
* Lambda curve grammar (`curves.expr(expr)`, IR `Lambda.expr`, `eks_harness.ir.catalog.curves`): a
  `field_validator` checks the text in four steps and stores its canonical form (the text the plan
  stores and hashes). Every failure raises `ValueError` starting `expr: `, which the models surface as a
  pydantic `ValidationError` on `expr`, so `curves.expr`, plan JSON and the validator all reject it
  before a renderer sees it. `check_lambda_expr(expr)` is the same check as a function returning the
  canonical text, and `LAMBDA_GRAMMAR_HINT` is the hint appended to grammar errors.
  1. Lexical: surrounding whitespace is stripped; the rest must be at most `LAMBDA_MAX_LENGTH` (1024)
     characters of printable ASCII (`U+0020` to `U+007E`) plus tab and newline. `#` (a comment) and `\`
     (a line continuation) are rejected by name, any other character as `character 'x' (U+XXXX) is not
     allowed`, so comments cannot carry text into the plan and NFKC-folded identifiers (fullwidth or
     mathematical letters) never reach the parser.
  2. Syntax: `ast.parse(expr, mode="eval")` accepts only these nodes: int constants with `|n| <=
     LAMBDA_MAX_INT` (2**53, the largest range of exact float integers) and finite float constants
     (`bool`, complex, strings, bytes, `None`, `...`, `1e400` are rejected); the names `t` (seconds) and
     `LAMBDA_CONSTANTS` (`pi`, `e`, `tau`, values in `LAMBDA_CONSTANT_VALUES`) in load context; unary
     `+ - not`; binary `+ - * / // % **`; `and`/`or`; comparisons `< <= > >= == !=` (chains allowed);
     `a if c else b`; and calls whose function is a bare name in `LAMBDA_FUNCTIONS` with positional,
     non-starred arguments in the arity of `LAMBDA_ARITY`: one argument for `sin cos tan asin acos atan
     sinh cosh tanh exp log2 log10 sqrt abs floor ceil`, two for `atan2 pow`, one or two for
     `log(x, base)`, three for `clamp(x, lo, hi)`, at least two for `min max` and at least one for
     `hypot`. Nodes nest at most `LAMBDA_MAX_DEPTH` (64) levels. Anything else (attributes, subscripts,
     collections, lambdas, comprehensions, f-strings, walrus, bitwise or identity or membership
     operators, keyword or attribute calls, a function name used without a call) is named with its node
     type or operator and source segment, for example `expr: Call to 'random' is not allowed; use t, e,
     pi, tau, ...`; every unknown name is listed once (`expr: unknown names 'x', 'y'; ...`); syntax
     errors are reported the same way.
  3. Constant parts: every subexpression without `t` (at any depth, in every branch, whether or not a
     conditional would reach it) is evaluated with the semantics below and must give a finite float,
     else `expr: overflow in '9 ** 9 ** 9'; every part without t must evaluate to a finite float` (also
     `division by zero` and `math domain error`). With integers bounded, floats only and the length and
     depth limits, evaluating one frame is a bounded number of double operations: `9**9**9**9`,
     `pow(10, pow(10, 10))` and `10**10**10 if t > 0 else 0` are plan errors, not renderer hangs.
  4. Canonical form: the tree is printed back with ints in decimal (`0x10` -> `16`, `1_000` -> `1000`),
     floats as Python `repr` (shortest round trip: `1e3` -> `1000.0`, `.5` -> `0.5`, `1e16` ->
     `1e+16`), names as written, one space around binary, boolean and comparison operators and after
     `not`, none after unary `+ -` or inside call parentheses, `, ` between arguments, and parentheses
     only where precedence needs them (`or` < `and` < `not` < comparisons < `+ -` < `* / // %` < unary
     `+ -` < `**`, `a if c else b` lowest; `+ - * / // %` left associative, `**` right associative with
     a unary right operand; nested `and`/`or` of the same operator and nested comparisons keep their
     parentheses). Parsing the canonical text gives the same tree and printing it again gives the same
     text, so `sin(t)*.5`, `(sin( t ) * 0.5)` and `sin(t) * 0.5` all store `sin(t) * 0.5`.

  Semantics (normative for every renderer): `compile_lambda(expr)` runs steps 1 to 3 and returns the
  reference evaluator `f(t: float) -> float`; renderers evaluate `Lambda.expr` through it (the asteval
  evaluator in `eks_harness.video.compile.animated_resolve` serves the legacy `eks_harness.video.ir`
  `Lambda`, not plan IR). Arithmetic is IEEE double: ints are converted to floats, `t` must be finite,
  `/` is true division, `//` and `%` follow Python float floor division and modulo (the result of `%`
  takes the sign of the divisor), `**` and `pow` are `math.pow`. Functions are the `math` functions of the
  same name with `abs` = `fabs`, `floor`/`ceil` returning floats, `log(x, base)` = `math.log(x, base)`,
  `clamp(x, lo, hi)` = `min(max(x, lo), hi)`; trigonometry is in radians. Truth is `!= 0`: comparisons
  give `1.0`/`0.0` (chains short circuit pairwise), `not x` gives `1.0` when `x == 0` else `0.0`,
  `a and b` gives `a` when `a == 0` else `b`, `a or b` gives `a` when `a != 0` else `b`, and
  `a if c else b` evaluates only the taken branch, so `log(t) if t > 0 else 0` is defined at `t = 0`.
  Every operation and function result must be finite: an overflow, a division by zero or a domain
  error raises `LambdaEvaluationError` (a `ValueError`) naming the canonical text, `t` and the failing
  part, for example `lambda 't ** 1e+300' at t=2.0: overflow in 't ** 1e+300'`.
* `eks.Edit` (`EditBuilder`, root of `@eks.edit`, created by `open_edit`, also used by scores):
  `media(uri)`; `audio` (an `AudioMix`); `track(name, z=, blend=, opacity=)` gets or creates a track
  (`z` defaults to the creation index); `track.clip(media, start=t.start, src=, ...)` returns a
  `ClipBuilder` with id `"{track}/{n}"`; `.effect(*effects)`, `.transition_in(tx)`, `.transition_out(tx)`,
  `.set(**fields)` chain (`fields: Unpack[ClipFields]`, a `TypedDict` of the `Clip` fields other than `id`;
  `AudioTrackBuilder.set(**fields: Unpack[AudioTrackFields])`, `edit.render_settings(**fields:
  Unpack[RenderSettingsFields])` and `fx.captions(source, **options: Unpack[CaptionOptions])` are typed the
  same way, so a misspelled keyword is a pyright error; unknown keys still raise `TypeError` at run time);
  `track.overlay(*overlays)` adds video overlays (anchors checked on the `video`
  surface); `cue(name, at)` adds a clock cue and returns `t.cue(name)`; `expect.<kind>(...)` queues a
  media check (built-in kinds have typed methods; any registered `family="media"` check, plugin ones
  included, is reachable as `expect.<kind>(at=, tolerance=, label=, soft=, **params)` or
  `expect.use(kind, ...)`). Edits and their audio mix are static: builder calls inside a
  `when`/`each`/`dynamic` body raise `TraceError`. `Timeline` and `Mix` validate their own invariants
  (unique track names, clip ids `"{track}/{n}"` unique, positive size, ducks under another track). A
  finalizer (order 100) records `audio.mix` (when there are audio tracks), then
  `edit.render` with the `Timeline` (`audio` = the mix ref) and `id_key="final:edit.render"`, then one
  `check.media` per queued expectation (media = the render). `output` returns the render ref after
  finalization.
* `AudioMix`: `track(media, name=, gain=, pan=, start=, src=, mute=)` (name defaults to the file stem or
  `track{n}`); `AudioTrackBuilder.beats(*streams, bpm=, backend=, name=)` records `media.markers` with a
  `BeatTracker` (default streams beat and downbeat, name defaults to the track name) and, when the clock
  has no tempo yet, sets `tempo=TempoMap(source=<markers node id>)`; it raises `TraceError` instead when the
  clock has no tempo yet and `beat` is not among the streams (a tempo source must give the beat grid; a
  kick or snare stream alone goes through `markers(BeatTracker(...))`); `markers(...)`, `duck`, `eq`,
  `compress`, `fade`, `reverb`, `denoise`, `stems`, `set` configure the track; `master(...)` sets the
  master bus; `finalize(duration=)` records `audio.mix` with `id_key="final:audio.mix"` or returns `None`
  when empty.

### 10.5 Scenes (P10)

* `open_scene(ctx, entry)` builds the builder of `SceneOptions.kind` and registers its finalizer.
* Blender (`eks.BlenderScene`): the engine alias (`options.engine`, default `eevee`) is resolved with
  `ENGINE_ALIASES` (unknown names raise `TraceError`); `samples` default `DEFAULT_SAMPLES[engine]`,
  `device` default `GPU`; all three are written explicitly into `BlenderRender`. `device="CPU"` is only
  for `CYCLES` (`engine_device_problem`: `TraceError` in the builder, `ValueError` in `BlenderRender`), and
  `platform` (`WORKER_PLATFORM`, `os-arch`) pins every frame of the render to workers of that platform. Node ids are the `id=`
  argument or derived (`load` -> slug of `obj` or of the file stem, `camera`, `camera_2`, `light`, ...);
  duplicates raise `TraceError`. `obj.material(name).texture(slot, media)` adds a `TextureBinding`;
  `cam.look_at(obj)` sets `look_at`; `obj.animate(prop, {time: value}, ease=)` adds an `Animation`
  (tuples become lists); `obj.pulse(scale=1.04)` is `Pulse(prop="scale", amount=1.04, duration=<half a
  beat when not given>)`; `assign` and `emit` build the other actions; `sc.on(trigger, *actions)` adds
  one `BlenderTrigger` per action; `@sc.bpy` adds a `BpyHook` (`capture_function(fn,
  allowed_globals=("math", "mathutils"))`) and returns the function; `world(...)`, `emits(...)`.
  Finalize records `blender.render` with `id_key="final:blender.render"`.
* Web scenes (`eks.WebScene`): `entry` comes from the scene options (`TraceError` when missing) and is
  recorded as a `media.source` of kind `bundle`; `prop`,
  `texture`, `on(trigger, emit=, data=)`, `emits`; finalize records `scene.render`.
* gfx (`eks.GfxScene`): layer ids `id=` or `{kind}{n}`; `group()` nests children; `mask`, `on`,
  `emits`; layer handles `set`, `animate`, `show`, and the trigger actions `pulse`, `assign`, `tween`;
  finalize records `gfx.render` with `cpu` from the scene options. `sc.text(font=)` takes a `FontLike`
  (10.4).
* Typed values: Blender `set(**props: JsonLike)`, `animate(prop, Mapping[Time, JsonLike])`,
  `assign(prop, JsonLike)`; gfx `set(**props: GfxValue)` and `assign(prop, GfxValue)` (`GfxValue` is
  `JsonLike | CurveBase | Keys | Driven`), `animate` and `tween` keys `Mapping[Time, JsonLike]`. Object and
  layer handles are `Encodable` (they encode as their id), so `parent=`/`mask=` handles type-check.
* `open_blender`, `open_web_scene` and `open_gfx` take `(ctx, entry, *, size=None, fps=None)` (overrides
  of the scene options, used by `embed_scene`); every builder's `finalize(*, id_key=None)` records
  its render once (under `id_key`, or `"final:<op>"` when it is `None`) and closes the builder (later calls
  raise `TraceError`).
* Scene seeds (`BlenderRender.seed` is `cycles.seed`, `WebScene.seed` seeds `Math.random` and
  `crypto.getRandomValues`, `GfxScene.seed` seeds random curves and effects) are concrete ints in
  `[0, SCENE_SEED_MAX]` (`SCENE_SEED_MAX = 2**31 - 1`, `eks_harness.ir.ops.scene`, the range of
  `cycles.seed`), fixed at build time. `scene(seed=...)` puts `seed` into `SceneOptions` only when given
  (`SceneOptions.seed` stays `int = 0` for `base_clock`, range-checked like the IR fields);
  `SceneOptions.explicit_seed` is `seed` when it was set (`seed=0` included) and `None` otherwise, and is
  what `open_blender`, `open_web_scene` and `open_gfx` pass to the builder (`seed: int | None`, with
  `name=entry.name`). At finalize an explicit seed is written as given; otherwise the builder writes
  `eks.webscene.scene_seed(ctx, None, key) = derived_seed(ctx.clock.seed, f"{key}/seed") % (SCENE_SEED_MAX
  + 1)`, where `key` (`scene_seed_key`) is the `finalize` `id_key` (`scene:{name}#{n}` for an embedding)
  or `scene:{name}` for a standalone scene plan. So the embedding clock's root seed (the score's or edit's
  `seed=`) changes every derived scene seed, two embeddings of one scene get different streams, an
  explicit `@eks.scene(seed=7)` keeps 7 in every embedding, and a standalone scene plan is deterministic.
  The node hash covers the seed, so the render cache never serves a render made with another seed.
* `embed_scene(ctx, scene, inputs, *, kind=None)`: used by scores; resolves the scene entry (a scene
  function, an `EntrySpec` or the name of a registered scene; anything that is not a scene raises
  `TraceError`), checks `kind` when given (`sc.gfx` passes `"gfx"`; a mismatch raises `TraceError`),
  coerces `inputs` (`text=False`) and turns media parameters given as strings or `Encodable`s into refs
  with `eks.media.as_media`, builds the builder for its kind on the score clock and size (the embedding
  entry's `size` and the clock's `fps` win over the scene's own), calls the scene function
  inside the current trace, finalizes it immediately with `id_key=f"scene:{name}#{n}"` (`n` counts
  embeddings of that scene in the trace from 0) and returns the render ref. `scene_event(media, name,
  **where: JsonLike)` returns an `EventRef` on that render node (a ref that is not a scene render raises; inside a
  trace a `name` the render does not declare in its `events` raises `TraceError`).
* `EventRef.source` is a `$ref` to the whole source node (`{"$ref": id, "path": []}`, `RefKind("media")`,
  a path raises), so a trigger on it adds an `after` edge and its Merkle hash covers the source scene.
  `TakeHandle.event(name, **where: JsonLike)` builds it with `encode(take ref)`. Every builder's `on()` checks an `EventRef`
  trigger at the call: the source must be recorded in the trace and be a scene render (`blender.render`,
  `gfx.render`, `scene.render`) or a take (`score.take`); for a scene render the event must be declared.
* `SetProp.value` (gfx and Blender) defaults to `None`, so assigning `None` is the action without `value`.
  `assign` validates a value against the target's model field (custom Blender properties pass through),
  `pulse` takes only animatable numeric fields (`Keys[float]`/`Keys[Vec3]`) and gfx `tween` only animated
  fields; failures raise `TypeError`. Blender `parent` cycles raise `TraceError`.

### 10.6 Scores and takes (P12)

* `open_score(ctx, entry)`: when `song` is set, `media.source` for it, an audio track named `song` with
  `.beats("beat", "downbeat")` (which sets the clock tempo source), and the window on the clock; an
  `EditBuilder` bound to the score (fps and size of the score) and its finalizer. `sc.audio` is
  `sc.edit.audio`, so the score has exactly one mix (song included) and one `edit.render`.
* `sc.take(platform=, app=, seed=, as_=, backend=, viewport=, span=, lead_in=, warp=, size=, root_seed=, now=, timezone=, locale=)`:
  `SessionSpec(key=f"take{n}", ...)` with `now`, `root_seed` and `backend` filled from the named seed by `inherited`
  (a take session without `now` gets the `clock.now` warning); `lead_in` and `tolerance` are `Length` (zero allowed), resources from `session_resources`, records the `score.take`
  container with an empty `body` and returns a `TakeHandle` (an `Encodable` whose encoding is the take's
  ref, usable wherever media is accepted).
* `with take.flow() as s:` (once per take, else `TraceError`) reopens the take's `body` with the `take`
  flag, creates a `Session` for the take's spec, activates it, records `session.as` when the take has a
  persona, and yields the session. Inside, `at=` pins are allowed, except inside a runtime `eks.each`
  body: `SessionCore.record_op` and `record_container` raise `TraceError` when `at` is given while
  `TraceContext.in_runtime_each()` is true (an open scope has `each` set and is not a literal-list
  template), because every iteration would land on the same time; pin a step outside the loop or unroll it
  with a Python for loop over a literal list. A literal-list `eks.each` expands into copies that keep the
  same pin, which `pin.order` reports. Inside an `eks.dynamic` function of a take, `pin.loop` reports any
  call with an `at=` or `pin=` keyword, any `**` keyword splat whose value is not a dict literal with only
  constant keys other than `"at"` and `"pin"` (it may carry a pin), and any call of a registered macro
  (`<x>.<namespace>.<name>(...)`, or `<ns>.<name>(...)` on a `namespace` freevar) whose body pins: the
  validator reads the macro's source and follows, transitively, the macros it calls (also through
  `s.macro(fn)`) and the non-library functions it calls by global or module attribute name, reporting a
  pin keyword or splat anywhere in them, or a body whose source cannot be read. Sessions reach the trace
  context only through `ctx`, which the sandbox rejects (section 5.8), and every public session pin is the
  keyword-only `at=`; the executor still rejects a pinned node in the sub-plan a dynamic function of a take
  returns at run time.
* Pins and spans live on the score clock: a pin must lie inside the take `span` cut to the clock bounds
  (`pin.span`), and the take `span` itself inside the clock bounds (`take.span`). The clock bounds are the
  clock `window`, else `[0, clock.duration]`, else (an open clock, or a window that does not resolve)
  `[0, )`: the score clock always starts at 0, so a pin or span start before 0 is always reported; `end`
  resolves to the end of those bounds for pins and take spans when they have one.
* A take belongs at the top level of its score: the builder raises `TraceError` for `sc.take` outside the
  score function's own scope, and the validator reports `take.placement` for a `score.take` node with any
  enclosing container (a hand-built take inside `ctl.each` would pin every iteration's take at the same
  time).
* `sc.scene(scene, **inputs)` and `sc.gfx(scene, **inputs)` call `embed_scene` with a scene function or
  a registered scene name (the latter requires a gfx scene); `sc.cue`, `sc.edit`, `sc.audio`, `sc.song`.
* The score `window` is the cut of the song and of the ad, and with `duration` the ad's length; both are
  part of the hash of every `bounded` op of the score (`edit.render`, `audio.mix`, the scene renders and
  the takes, 7.3), so the same score with another window or duration shares no render hash with it.
* Every node uses only the lease and backend of the session governing it: inside a take's body the take's
  session, the `score.take` node its own take session, elsewhere `Plan.session`; a lease or backend op of a
  score outside every take has no governing session. The validator reports any other use as
  `session.uses` (section 11).

## 11. Validator (P11, `eks_harness/engine/validator.py`, `eks/validation.py`)

`validate(plan, *, registry=None, personas=None, seeds=None, apps=None, versions=None, backends=None) -> Report` (and
`Validator(...).validate`). `registry` defaults to `default_registry()`, `versions` to `registry.versions()`,
`personas` maps a seed name to its persona keys or `None` for an unknown seed; `seeds` maps a seed name to a
`SeedInfo(name, backend, now, hash, personas, bases, resources)` or `None` (`resources`: the `(path, kind)`
of every `data.create` of the seed and of its bases) (and supplies `personas` when that is not
given); `apps` maps an app name to the platforms of its `.harness/app.toml` or `None` when the tree has no
such app (a `LookupError` becomes the issue message); `backends` is the collection of known backend
definition names (`None` skips the `resource.backend` rule). `eks.validate(plan, registry=None, tree=None)` passes
`eks.workspace.seed_info` and, when `tree` is given, an app lookup over that tree (`find_app`) and
`eks.validation.known_backends(registry, tree)`: the names of `registry.backends()` plus the ids of the
`[contributes] backend` entries of the tree's active plugins (`eks.discovery.legacy_backends`, while the old
tables exist); the plan CLI passes the entry file's tree. The validator never raises for a bad plan; every problem is an `Issue`
(`severity`, `code` from `ISSUE_CODES`, `message`, `node`, `field` as a dotted JSON path inside the args,
`src`). `Report.ok` is true when there is no error; `errors`, `warnings`, `as_dict()` (`{"ok", "hash",
"issues": [...]}`) and `text()` (one line per issue: `ERROR code node src: message`) render it. Issues come
in plan walk order (plan-level issues first), then by code.

Surfaces: a node that uses a `lease.<platform>` resource is checked against that platform;
`overlay.annotate` against `screenshot`; overlays and anchors inside `edit.render` against `video`;
`blender.render` against `blender`; `gfx.render` against `gfx`; `scene.render` against `scene`. Times inside
clips, layers and scene animations follow the clip-local rule (section 2.10).

Rules (all errors unless marked warning):

| code | condition |
| --- | --- |
| `plan.schema` | `plan.schema != "eks/plan@1"` |
| `plan.hash` | the plan carries hashes and `hash_plan` on a deep copy gives a different node or plan hash (a node whose op is not registered is not compared, since its executor version is unknown) |
| `node.duplicate` | a node id or resource id appears twice |
| `resource.kind` | a resource kind is not `lease.web`, `lease.ios`, `lease.android` or `backend` |
| `resource.unknown` | `uses` names an undeclared resource |
| `op.unknown` | the op is not registered (the node's other checks, including its `plan.hash` comparison, are skipped; `node.duplicate` and `node.cycle` still count it) |
| `op.version` | `node.v != OpSpec.version` |
| `op.out` | `out_problem(spec, args, node.out)` (5.2): `out` is neither the op's output type (`resource:<args.kind>` for `data.create` and `data.base`) nor a narrowing of a `json` op; an omitted `out` (`none`) says so. Ref typing (`ref.path`, `ref.type`, `resource.parent`, item types) then uses `expected_out` instead of the stored `out` |
| `args.invalid` | `OpSpec.args.model_validate(node.args)` fails (first 3 pydantic errors in the message) |
| `node.branches` | a container lacks a required branch (`then` for `ctl.when`, `body` for the others), has a branch not in `OpSpec.branches`, or a plain op has `branches` |
| `node.after` | an `after` id is unknown, not earlier in program order, names an enclosing container, or names a node that is not visible (section 5.5: inside a `when`, `each` or `dynamic` body, or another branch, the node is not in); a node does not reach, through `after` edges, the producer of a visible `$ref` in its args or one of its clock providers (`clock_dependencies`, 5.4), where reaching follows `after` targets transitively, a finished container covers its descendants and a node inside a container inherits what its innermost container reached; a program-ordered node does not reach the node before it on its lane (5.4: one lane per branch, one lane per resource in a `ctl.parallel` body where a node is on the lane of every resource in its `uses`, a container there taking its own and its body's lanes) (`field` = `after`) |
| `node.cycle` | the `after` graph has a cycle |
| `ref.unknown` | a `$ref` names no node |
| `ref.not_visible` | a `$ref` names a node that is not visible from the node (section 5.5), or `Plan.outputs` references a node not visible at the root |
| `ref.item_scope` | an `$item` names a container that is not an enclosing `ctl.each` |
| `ref.path` | `infer_path(producer.out, path)` fails, or a `$ref`/`$item`/`$fmt` object in the args or `Plan.outputs` does not parse (reported only when the args otherwise validate, so a typed field gives `args.invalid` alone) |
| `ref.type` | the inferred type is not `compatible` with the field's `RefKind` (refs inside `$fmt` must produce `str`, `int`, `float`, `bool` or `json`; `ctl.each` `over` must produce a list or `json`); for `expect.value`, `assertion_problem` reports an assertion that cannot pass for the value's type (`field` = `assertion`) |
| `target.kind` | a target kind (or a `within`/`near` kind) is not registered |
| `target.args` | target args do not validate against the kind's model |
| `target.surface` | `check_target` reports a problem for the node's surface |
| `time.unresolvable` | a time's `needs()`, or the `field_needs` of a `StreamName` or `WordSource` field (2.7, reported at that field), is not met (times held as plain JSON count: objects decoded by `JsonDecoder` as in 7.3, such as `Keys` assigned with `assign`, reported at their JSON path, and `ctl.dynamic` closure times at `fn.freevars.<name>.value`): fps (plan clock, or the timeline's or scene's own fps), tempo (a `bpm`, an `external` tempo on a scene plan, or a `source` naming a `media.markers` node with exactly one `BeatTracker` that tracks `beat`), downbeats (`downbeat:` anchors and `downbeat` streams on an analysed tempo need that tracker to track `downbeat`), cues (clock cues, or external tempo on a scene plan), markers and words (streams of `media.markers` nodes in the plan, or external tempo on a scene plan; unqualified `beat`, `downbeat`, `kick`, `snare` streams resolve against the tempo source; a word without a source needs exactly one `STTMarkers` source in the plan), window (`end` needs a window or duration outside clip-local contexts). Plan level: `clock.tempo.external` on a plan whose kind is not `scene` (`field` = `clock.tempo.external`; the other checks stay active, since external tempo is honoured only for scenes), a tempo `source` that is not a `media.markers` node, or one without exactly one `BeatTracker` tracking `beat` (`field` = `clock.tempo.source`) |
| `markers.duplicate` | a `media.markers` source whose `name` or one of whose `stream_names()` another source of the plan (of the same node or an earlier one) already provides, or that is a clock tempo stream name (`beat`, `downbeat`, `bar`, `kick`, `snare`) (`field` = `sources.<i>.name`) |
| `time.negative` | a field typed `Duration` or `Length` (section 10.4) holds a negative length: the type raises the pydantic error type `time_negative`, which the validator reports under this code with the field path (inside check params too) instead of `args.invalid`/`check.args`; other errors of the same model are still reported under those codes |
| `clock.now` (warning) | a flow or seed plan has no `clock.now`, or a take session has no `now` |
| `clock.zone` (warning) | a flow session (with `clock.now`) or a take session with `now` has no `timezone` or no `locale` (`field` = the first missing one under `session.` for takes); the app profile's `[web]`/`[mobile]` tables, `@eks.flow(timezone=, locale=)` and `sc.take(timezone=, locale=)` set them |
| `time.reversed` | a `Span` (also a `TimeSet` window) whose ends both resolve on the static clock context (concrete, or frame, beat, bar and cue times on a static tempo and the clock cues) ends before it starts: clip `src`, overlay windows, take `span`, the clock window, media check times |
| `resource.backend` | with a backend collection: a `backend` resource's `definition` is not a known backend |
| `cassette.nested` | `net.cassette` inside `net.cassette` |
| `evidence.duplicate` | within one session, two nodes that are not in different branches of the same `ctl.when` share a literal name: checkpoint `(label, viewport)` (the viewport defaults to the session's), shot, recording, HAR (`har` when unnamed), dump (its kind when unnamed), cassette or breakpoint label; also a literal name (not `$fmt`) on a node with a runtime `ctl.each` ancestor, which every iteration repeats, and a call in a `ctl.dynamic` `fn.source` that passes a string literal as the name of `checkpoint`, `shot`, `record`, `har`, `dump`, `cassette` or `break_` inside a loop or comprehension of the function or while the dynamic node sits in a runtime `ctl.each` (`field` = `fn.source`); an error for cassettes and for checkpoints with a `visual` check (their own `checks`, else the session's), a warning otherwise |
| `expect.eventually` | an `expect.value` with `eventually: true` whose `value` is not a `$ref`, is an `$item`, or comes from an op that is not `idempotent` (polling would run a mutating request again), mirroring `eks.assertions` (`field` = `eventually`) |
| `seed.path` | a `data.create` or `data.base` with an enclosing container (resources are declared at the seed root; no `field`); a `data.create` whose `path` is not `resource_path(<parent's path>, resource_segment(kind, key, n))`, the parent's path being the `path` of the `data.create` or `data.base` its `parent` references and `n` counting the keyless siblings of that kind under that parent in program order, after the highest index the base chain creates there (`SeedInfo.resources`; without a seed lookup, the first such sibling under an extended plan sets the count) (`field` = `path`); a `data.base` whose path does not end in the segment of its kind and key (`field` = `path`); a path that another `data.create` or `data.base` of the plan already uses (`field` = `path`) |
| `seed.persona` | a persona kind's key that another `data.create` of the plan already declares (`field` = `key`), or, when every `data.create` names a registered kind, `Plan.personas` differing from the persona keys of the plan's `data.create` nodes in program order (plan level) |
| `seed.resource` | with a seed lookup: a `data.create` path that a base of the seed (a `data.extend`) already creates (`field` = `path`), a persona kind's key that is already a base persona (`field` = `key`), or a `data.base` naming a seed the plan does not extend (`field` = `base`), a path that base does not create (`field` = `path`) or another kind than that base resource's (`field` = `kind`) |
| `renderer.unknown` | a `media.source` `renderer` is not a registered `@eks.renderer` media kind |
| `seed.unknown` | `data.extend` names a seed that the seed lookup does not know |
| `seed.backend` | a seed extends a base on another backend definition, or a flow or take session names a backend other than its seed's |
| `seed.now` | a seed extends a base whose frozen now differs from its own |
| `seed.stale` | the `hash` of `data.extend` is not the base seed's current plan hash |
| `seed.cycle` | the base of `data.extend` extends the plan's seed again, directly or through its bases |
| `resource.app` | with an app lookup: a lease or backend resource names an app the tree has no profile for, or a lease platform the profile does not list |
| `pin.op` | a node has `pin` but its op is not `pinnable` |
| `pin.outside_take` | a node has `pin` but no enclosing `score.take` |
| `pin.span` | inside a take, a pin that resolves on the pin context lies outside the take's bounds (inclusive): its `span` intersected with the clock bounds (the clock `window`, else `[0, clock.duration]`, else `[0, )` with no upper bound), else the clock bounds alone; a pin before 0 is always outside. Skipped when the pin does not resolve statically. The pin context is the static clock context with `end` set to the end of the clock bounds when they have one, so `t("end") + t("5s")` resolves |
| `take.span` | a take `span` that resolves on the pin context starts before or ends after the clock bounds, including a start before 0 on an open clock (`field` = `span`) |
| `take.placement` | a `score.take` node has an enclosing container (`ctl.each`, `ctl.when`, `ctl.parallel` or any other); the message names the innermost one |
| `pin.loop` | a pinned node has a `ctl.each` among its enclosing containers (between its take and itself, or around the take), or a `ctl.dynamic` inside a take has a call in its `fn.source` with an `at` or `pin` keyword, a `**` dict literal with an `"at"`/`"pin"` key or a non-constant key, a `**` splat of anything other than a dict literal, or a call of a registered macro whose body, or a macro or non-library function it calls by name, does any of these or cannot be read (`field` = `fn.source`) |
| `pin.order` | inside one take, pins that resolve on the pin context (concrete, a static tempo map, or `end` on a known clock end) do not strictly increase along each execution path (the `then` and `else` branches of a `ctl.when` are ordered separately from the pin before the when; a pin after it must exceed the latest pin of every branch) |
| `op.platform` | the session platform is not in `OpSpec.platforms` |
| `op.resources` | a role in `OpSpec.resources` has no resource of that kind in `uses` (for example `ui.tap` in an edit) |
| `op.recording_only` | a `recording_only` op without an enclosing `capture.record` or `score.take` |
| `record.nested` | `capture.record` inside `capture.record` |
| `persona.seed` | a session with a `seed` whose `personas(seed)` is `None` |
| `persona.unknown` | a session `persona`, a `session.as` persona or the `persona` of any op whose args model has that field (the `api.*` family) missing from its seed's personas; without a seed, a persona that is not a registered persona preset is a warning |
| `plugin.missing`, `plugin.version` | a `Plan.requires` plugin is not loaded, or has another version |
| `control.else` | a `ctl.when` has an `else` branch but no `then` branch |
| `control.empty` (warning) | an empty `then`, `body` branch |
| `dynamic.binding` | a `ctl.dynamic` function has a binding kind outside section 5.8, a global (builtins included) outside `ALLOWED_DYNAMIC_GLOBALS` that is not a literal in its `fn.globals`, or, read from `scan_sandbox_source(fn.source)` rather than trusting `fn.globals` (`field` = `fn.source`): an `import`/`from ... import`, a builtin outside `SANDBOX_BUILTINS` that no freevar or sandbox global shadows (body, class bodies, defaults and annotations), or an escape of section 5.8 (dunder name or attribute, private or internal attribute, introspection attribute, `match` class attribute, `str.format` field, `eks` outside `SANDBOX_EKS_ATTRIBUTES`/`SANDBOX_EKS_NAMESPACES`, `.format` not called on a string literal), or a `sandbox_leaks` sink of section 5.8 (a value that may hold an object address formatted or hashed; `data_names` are the `literal`, `time` and `target` freevars). A `@sc.bpy` hook of `blender.render` is checked the same way: a builtin outside `SANDBOX_BUILTINS` in its `fn.globals` (`field` = `scene.hooks.<i>.fn.globals.<j>`), and in its `fn.source` an import whose root is not in `BPY_MODULES`, a builtin outside `SANDBOX_BUILTINS`, an escape or a leak with `data_modules=BPY_MODULES` and host-object parameters (`field` = `scene.hooks.<i>.fn.source`) |
| `check.kind`, `check.args`, `check.platform` | checkpoint checks (of `check.checkpoint`, `check.explore`, `SessionSpec.checks` and `SessionSpec.defaults`, the latter two at plan level or under `session.checks` and `session.defaults` of a take) and `check.media` kinds must be registered (family `checkpoint`, `visual` or `perf` for checkpoints, `media` for `check.media`), their params must validate, and their platforms must include the session platform |
| `resource.kind_unknown` | a `data.create` kind is not a registered resource kind |
| `resource.fields` | `data.create` fields do not validate against the resource model (`field` = `fields`), or the key is missing for a keyed kind or present for a keyless one (`field` = `key`); fields that validate are scanned, so a reference field narrowed with `eks.refers_to(kind)` reports `ref.type` when it names a `data.create` of another kind |
| `resource.parent` | a `data.create` has no parent for a kind with `ResourceSpec.parent`, a parent for a root kind, or a parent `$ref` that is not a whole `data.create` or `data.base` producing `resource:<parent kind>` |
| `resource.phase` | a `data.create` phase differs from `ResourceSpec.phase` |
| `session.mismatch` | a resource whose `session` names no session of the plan (`Plan.session` or a take's), a lease of that session whose kind is not `lease.<platform>`, an `app` other than the session's, or a backend whose `seed` or `definition` differs from the session's; a take whose session key repeats another session's is checked against its own `SessionSpec` for every resource of that key its `score.take` node and body use (`field` = `session`) |
| `session.duplicate` | a take session key that `Plan.session` or an earlier take already uses (`field` = `session.key`) |
| `session.resources` | a session key with more than one lease resource or more than one backend resource (plan level) |
| `resource.id` | a resource id differs from `resource_id(kind, identity)`, the identity being its `params` with `app`, `definition`, `seed`, `session` and `name` (unset ones left out) (plan level) |
| `session.uses` | a node's `uses` names a resource whose `session` is not the key of the session governing the node: the enclosing take's session inside a take body, the take's own session for a `score.take` node, else `Plan.session`; a score node outside every take that uses a session's lease or backend has no governing session and is always reported (resources without a `session` are not checked) |
| `macro.unknown`, `macro.version`, `macro.params`, `macro.platform` | `ctl.macro` names an unregistered macro, carries another `version` than `MacroSpec.version`, has params that do not bind to the macro signature (unknown names, a missing required parameter, a non-list `*args` or non-object `**kwargs`), or runs on a platform outside `MacroSpec.platforms` |
| `overlay.renderer` | `overlay.show` needs the `live` renderer and the platform in the kind's platforms; `overlay.annotate` needs `screenshot`; edit track overlays need `video` |
| `preset.unknown` | viewport, pace, network and style names (session spec, `capture.*`, `check.checkpoint`, `web.viewport`, `ui.type`, `net.shape`, overlay `style`, `overlay.annotate`) are not registered presets of their category; a viewport name the node's `SessionSpec.viewports` defines (the app profile's) counts as known and wins over a preset of that name |
| `blender.render` | the render settings are not explicit (engine, samples, device) |
| `scene.ref` | a Blender `look_at`, `focus`, `parent`, texture, animation, trigger or camera id, or a gfx trigger, mask or layer id, names nothing in the scene |
| `scene.duplicate` | a Blender node id or a gfx layer id (group children included, depth first) repeats an earlier one (`field` = the later one's `id`) |
| `scene.cycle` | Blender `parent` links, or gfx `mask` links together with group-to-child links, form a cycle (`eks_harness.ir.ops.scene.find_cycle`, the same `dependency_path` the builders use; `field` = `scene.nodes` or `scene.layers`) |
| `scene.event` | an `EventRef` (`eks.scenes.scene_event`, scene trigger `at`) whose source node is not a `blender.render`, `gfx.render`, `scene.render` or `score.take`, or a render whose scene `events` lacks the name (`event_source_problem`, shared with the builders) |

## 12. CLI and JSON Schema (P13)

### 12.1 `eks-harness plan <file>[::name] [--set k=v]... [--platform P] [--no-plugins] [--json]`

Runs `eks.workspace.plan_spec` from the current directory (plugins unless `--no-plugins`), then
`eks.validate`. Text output: a header line (`<kind> <name>: <n> nodes, <m> resources, hash <first 16 hex>`),
a table of nodes in walk order (indented by depth) with columns id, op, args summary (120 characters),
uses, after, src, then the issues. `--json` prints `{"plan": <plan JSON>, "validation": <report
as_dict>}` with `print_json`. Exit 0 when valid, 1 (`EXIT_ERROR`) when the report has errors, 2
(`EXIT_USAGE`) for usage, discovery, `PlanBuildError`, `TraceError`, `RegistryError` and pydantic
validation errors raised while tracing (reported with the message and the user source line).

### 12.2 `eks-harness ops [name] [--family F] [--no-plugins] [--json]`

Loads plugins (unless `--no-plugins`) from the current tree. Without a name: a table of every op (name,
out, location, platforms, flags, plugin, description); `--json` prints `{"ops": [<op summary>...]}`. With a
name: the op summary plus its argument JSON Schema (`op_schema`); unknown names exit 6 (`EXIT_NOT_FOUND`)
with suggestions. Op summary keys: `name`, `family`, `version`, `out`, `description`, `location`,
`executor`, `pure`, `idempotent`, `deviceDependent`, `ordering`, `platforms`, `resources`, `container`,
`branches`, `exportsBody`, `pinnable`, `recordingOnly`, `bounded`, `plugin`, `pluginVersion`. The registry is
the current tree's (`tree_registry`, section 9.3) with its plugins loaded, or the process registry outside a
tree; `eks-harness schema` reads the same registry.

### 12.3 `eks-harness schema [--out DIR] [--op NAME] [--no-plugins] [--json]`

Prints `plan_schema()` as JSON, or `op_schema` of one op, or writes `DIR/plan.schema.json` and
`DIR/ops/<op>.json` (one per op) and prints the written paths.

### 12.4 Schema export (`eks_harness/ir/schema.py`)

* `op_schema(spec, *, registry=None)`: the op's argument model as the root schema, with `title` set to
  the op name, `description` to the op description, and `x-eks` holding the op summary. It is cut from
  the same generation as `plan_schema` (`by_alias=True`, `mode="validation"`,
  `schema_generator=schema_generator(registry)`, every op, catalog, target kind, check and resource model in
  one pass), so its `$defs` carry the same names and the same kind dispatch as the plan schema, holding
  only the defs the op reaches; references to the op's own model become `#`. `write_schemas` generates
  once and cuts every file from that generation.
* Kind dispatch: three registered model families are typed in the schema, both in `plan_schema` and in
  every `op_schema`, from the registry passed in. Each owner keeps its object schema (its properties and
  their descriptions), its discriminator gets an `enum` of the registered kinds, and a `oneOf` holds one
  variant per kind, `{"title": kind, "description": <kind description>, "properties": {<discriminator>:
  {"const": kind}, <payload>: {"$ref": "#/$defs/<model>"}}, "required": [<discriminator>, <payload> when
  the model has a required field]}`:

  | Owner | Discriminator | Payload | Kinds |
  | --- | --- | --- | --- |
  | `Target` | `by` | `args` | `registry.catalogs("target")` (built-in `ByTestId`, `ByRole`, ... and plugin kinds) |
  | `CheckUse` | `kind` | `params` | `registry.checks()` of the families `checkpoint`, `visual`, `perf` |
  | `check.media` args | `kind` | `params` | `registry.checks()` of the family `media` |
  | `data.create` args | `kind` | `fields` | `registry.resources()` |

  The families match the validator's `CHECKPOINT_FAMILIES` and `MEDIA_FAMILIES`. The kind models are
  `$defs` under the def naming rule above (`ByTestId`, `ConsoleErrors`, `<plugin id>.<Class>` for plugin
  kinds), so their field descriptions reach `eks-harness ops` and `eks-harness schema`, and a plan with an
  unknown kind, a misspelled or missing argument, parameter or field fails the schema as it fails the
  validator. With no kinds registered (no resource plugin loaded) the discriminator's `enum` is empty and
  no `oneOf` is written, so no `data.create` validates, as in the validator (`resource.kind_unknown`).
  The schema reflects the registry when it is generated: a seed's resource kinds are part of it only once
  the file that registers them has loaded.
* `plan_schema(registry)`: one JSON Schema (draft 2020-12, `$id` `"eks/plan@1"`) for `Plan`, where
  `nodes` items (and branch items) are `oneOf` per registered op of `{"op": {"const": name}, "v": ...,
  "args": {"$ref": "#/$defs/<op>"}, "out": ...}` merged with the common node fields, where `out` is
  `{"const": <op out>}` (with `"default": "none"` for ops that produce nothing), any string for ops that
  produce `json` (a narrowing, 5.2), and a string matching `RESOURCE_TYPE_PATTERN` for `data.create` and
  `data.base`; `out` is required for every op whose `out` is not `none`; every op's argument schema and
  every shared model lives in `$defs` (no duplicate definitions; names made unique and stable: built-in
  models keep their class name, project and plugin models are named `<plugin id>.<Class>`, so loading a
  plugin never renames a built-in def; see decisions-m0.md). `Poly`
  fields expand to the registered models. The schema validates every plan built in P13's tests
  (`jsonschema` 4.x is importable in the dev environment as a transitive dependency; use
  `jsonschema.Draft202012Validator`). Every property of every `$defs` model and of the plan itself carries a
  `description` (plan-level models included: `Plan`, `Node`, `Clock`, `Resource`, `Requirement`,
  `SourceInfo`, `SessionSpec`, `TempoMap`, `TempoChange`, `Meter`, `MeterChange`), and closed vocabularies
  are enums in the schema (`data.create` `phase` is the registry's `Phase` literal: `schema`, `api`,
  `finalize`, `mongo`).
* `write_schemas(out, registry=None)` writes the files and returns their paths sorted.

## 13. Packaging

* `pyproject.toml`: `requires-python = ">=3.13"`; wheel `packages = ["src/eks_harness", "src/eks"]`
  (done in the skeleton; `uv.lock` was relocked accordingly).
* `src/eks/py.typed` ships inline types; there are no separate stub files. Plugin ops are typed through
  their argument models (`s.call(Model(...))`), so no generated stubs are needed. P13 verifies the public
  API types with a pyright run over the design examples (section 15): `tests/eks/test_typing_pyright.py`
  runs pyright 1.1.390 (skipped without npx) over `tests/eks/typing_public_api.py`, every file under
  `examples/dsl` (included through a project file, since pyright skips `.harness/` by default) and
  `tests/eks/typing_negative.py`, whose lines must stay errors (each carries a pyright ignore that
  `reportUnnecessaryTypeIgnoreComment` turns into an error once a signature loosens).

## 14. Testing conventions

* One test file per concern, prefixed with the package's area: `test_time_*.py`, `test_target_*.py`,
  `test_ir_*.py`, `test_ref_*.py`, `test_tracing_*.py`, `test_control_*.py`, `test_planning_*.py`,
  `test_registry_*.py`, `test_discovery_*.py`, `test_flow_*.py`, `test_evidence_*.py`, `test_env_*.py`,
  `test_data_*.py`, `test_media_*.py`, `test_edit_*.py`, `test_scene_*.py`, `test_validator_*.py`,
  `test_score_*.py`, `test_examples_*.py`, `test_cli_*.py`, `test_schema_*.py`.
* Tests define entries and plugin ops inside test functions or module scope; the autouse registry fixture
  isolates them. Shared test helpers go into the package's own test modules (no shared helper module, no
  conftest in `tests/eks`).
* Determinism tests (every package that builds plans): plan the same entry twice and compare
  `plan.to_json()` byte for byte and `plan.hash`; write the source to a temp file, plan it through
  `eks.discovery.load_file`, then insert blank lines, comments and an unrelated statement and plan again:
  every node id of the unchanged statements and the hashes of nodes not after the change stay the same.
* Tests that need app profiles or `.harness` trees build them under `tmp_path`.

## 15. Design examples acceptance (P12)

M0 is accepted when every example of design.md plans and validates through the same path as the CLI
(`eks.workspace.plan_spec` then `eks.validate`), with no errors and no warnings. P12 owns the fixture tree
`tests/eks/examples/tree/` (copied to `tmp_path` by the tests) and `tests/eks/test_examples_design.py`:

| file in the tree | content |
| --- | --- |
| `.harness/project.toml` | a project id and trust for the `examples` repo plugin |
| `.harness/plugins/examples/harness-plugin.toml` + `example_resources.py` | `modules = ["example_resources"]`; the resource kinds the standard seed uses: `host_persona` (root, persona), `federation` (root), `tenant` (root, field `reseller` referencing a federation), `persona` (parent tenant, persona), `accountant` (root), `serves` (parent accountant, keyless, positional `tenant`, so `accountant.serves(harman)` works), `bank_account` (parent tenant), `transactions` (parent bank_account, keyless), `invoices` (parent tenant, keyless) |
| `web-app/.harness/app.toml` | `[app] name = "web-app"`, `platforms = ["web"]`, `[web] url = "http://localhost:3000"` |
| `mobile-app/.harness/app.toml` | `[app] name = "mobile-app"`, `platforms = ["ios", "android"]` |
| `.harness/flows/subeler.py` | design 4.1 verbatim |
| `.harness/flows/control.py` | design 4.3 wrapped in `@eks.flow(app="web-app", seed="standard") def control(s: eks.Session)` (the seed gives the flow its frozen now) |
| `.harness/flows/network.py` | design 8 wrapped in a flow naming the standard seed (the cassette body holds one `s.ui.nav`) |
| `.harness/flows/debugging.py` | design 9 `s.debug.break_("before-submit")` in a flow naming the standard seed |
| `.harness/seeds/standard.py` | design 7 verbatim |
| `.harness/edits/demo.py` | design 10.2, the chained clip calls wrapped in parentheses (the design's line continuation is not valid Python) |
| `.harness/scenes/phone.py` | design 10.4 verbatim (`rig` body `return None`) |
| `.harness/scenes/titles.py` | a gfx scene `titles` (two text layers, one pulse on downbeats), which design 10.5 uses but does not show |
| `.harness/scores/ad.py` | design 10.5 verbatim, preceded by `phone_scene = "phone"` and `titles = "titles"` (scene names; `plan_spec` loads the tree's scenes, so no imports between `.harness` files are needed) |

Per example the test asserts the op sequence (walk order), key args (for `subeler`: the second `ui.nav`
route is a `$fmt` whose ref path is `["items", {"find": {"name": "Kadıköy"}}, "id"]`; `outputs` is
`{"branches": {"$ref": ...}}`), resources, personas of the seed, the score's take body with three
pinned nodes, the Blender texture bound to the take, the gfx render in the `graphics` track, the master
loudness of -14 LUFS and the clock tempo source. Determinism: each example planned twice gives the same
JSON and hash; `subeler.py` with added blank lines and comments gives the same ids and hash.

The complete design workspace is `examples/dsl/` (every code example of design sections 4 to 12 as an entry
file, plus the `cookbook` plugin of section 11), exercised by `tests/eks/test_design_examples.py`: each
example builds through `plan_spec` and through `eks.plan`, validates with no issue (with the tree, so
`resource.app` runs), hashes the same twice, round-trips through JSON and the plan schema, freezes the
flow and seed clocks, and keeps every node hash and the plan hash when its entry is renamed.

## 16. Work packages

| id | wave | owns |
| --- | --- | --- |
| P1 time | 1 | `eks_harness/ir/time.py`, `eks/time.py` |
| P2 targets | 1 | `eks_harness/ir/target.py`, `eks/target.py` |
| P3 IR core and refs | 1 | `eks_harness/ir/__init__.py`, `refs.py`, `types.py`, `plan.py`, `hashing.py`, `eks/ref.py` |
| P4 tracing, control, planning | 2 | `eks/tracing.py`, `eks/control.py`, `eks/planning.py`, `eks_harness/ir/ops/ctl.py` |
| P5 registry, presets, discovery, plugins | 2 | `eks_harness/ir/registry.py`, `entries.py`, `presets.py`, `ops/__init__.py`, `eks/registry.py`, `eks/discovery.py`, `eks_harness/plugins/manifest.py` |
| P6 session and driving | 3 | `eks/session.py`, `eks/ui.py`, `eks/web.py`, `eks/mobile.py`, `eks_harness/ir/ops/{ui,web,mobile}.py` |
| P7 evidence | 3 | `eks/{assertions,capture,overlay,checks,debug}.py`, `eks_harness/ir/ops/{capture,overlay,check,debug}.py` |
| P8 environment and data | 3 | `eks/{api,net,inject,fake,data}.py`, `eks_harness/ir/ops/{api,net,inject,fake,data}.py` |
| P9 media, edits, audio, catalogs | 3 | `eks/{media,effects,editing,audio}.py`, `eks_harness/ir/ops/{media,edit,audio}.py`, `eks_harness/ir/catalog/*` |
| P10 scenes | 3 | `eks/{gfx,blender,webscene,scenes}.py`, `eks_harness/ir/ops/{gfx,blender,scene}.py` |
| P11 validator and workspace | 3 | `eks_harness/engine/{__init__,validator}.py`, `eks/validation.py`, `eks/workspace.py` |
| P12 scores and design acceptance | 4 | `eks/scoring.py`, `eks_harness/ir/ops/score.py`, `tests/eks/examples/` |
| P13 CLI, schema, public API | 4 | `eks_harness/cli/{plan,ops,schema}_cmds.py`, `eks_harness/ir/schema.py`, `eks/__init__.py`, `eks/py.typed` |

`src/eks_harness/cli/__init__.py`, `pyproject.toml`, `uv.lock` and `tests/conftest.py` (the `eks_registry`
fixture) were changed by the architect and are not owned by any package.
