---
name: eks-harness-ui-design
description: The binding UI/UX design system of the eks-harness web app (web/) and of plugin UI modules - the "lightbox, ledger and dope sheet" language, tokens, type, per-device layouts, information architecture, component and timeline rules, copy rules, banned AI-default patterns and the screenshot review loop. Load it before any change in web/, sdk/ui or a plugin's ui/ folder, before reviewing such a change, and whenever a page, panel, viewer, timeline or dialog is added or restyled.
---

# eks-harness UI design system

eks-harness is a workbench: a lab of live devices, browsers, backends and render nodes; an evidence
store of what agents captured; and a studio where video edits, Blender scenes, web scenes and device
takes run on one clock. The UI is where a developer watches, judges and hands on that work. Every
decision in `web/`, `sdk/ui` and plugin UI modules follows this document. When code and this document
disagree, change the code or change this document on purpose. Background research lives in
`docs/ui-research.md`.

## 1. Principles

1. **The work is the interface.** Captures, live screens, frames and timelines are the content. Chrome is
   paper, hairlines and type. Large tonal surfaces exist only where media is judged: the well and the stage.
2. **Ledger, not dashboard.** Aligned columns, tabular figures, hairline rules, read top to bottom. No stat
   tiles, no hero numbers, no charts that answer nothing.
3. **Words carry state.** `running`, `queued (2nd)`, `pending approval`, `offline 4 min`, `broken: no camera`.
   Color and shape reinforce a word; they never replace it.
4. **One mark color, one meaning.** Vermilion marks what needs the reader: unseen, selected, focused, failed.
   It is also the playhead, because the playhead is "where you are". Nothing else uses it.
5. **Time is drawn like a dope sheet.** Frames are columns, beats are rules, downbeats are heavier rules,
   cues are flags. Every timeline (studio, score, recording step logs, job history) uses the same grid.
6. **Identifiers are first-class.** sids, ULIDs, hashes, plugin ids, node ids, branch names, paths and
   timecode are mono, copyable in one click, never truncated without the full value in a title.
7. **Every device gets a layout designed for its job.** Desktop triages, edits and administers; tablet
   reviews and drives one device; phone reads, scrubs, comments and shares.
8. **Nothing is hidden behind beauty.** Fewer boxes, not fewer capabilities. Everything the API does for a
   role is reachable in two steps, and by keyboard.

## 2. Who and what

| Who | Job | Where |
| --- | --- | --- |
| Developer | See what is running and what my agent captured on my branch | Now, Evidence |
| Developer | Drive a simulator or browser by hand, arm fakes, watch logs | Lab |
| Developer | Hand evidence to a PR or a colleague | Artifact links, Share |
| Video author | Edit a cut against the beat, preview, render, publish | Studio |
| Video author | Sync Blender, web scenes and device takes on one score; play it live | Studio, Score |
| Operator | Nodes, GPUs, jobs, plugins, settings, users | Nodes, Plugins, Settings |
| Teammate | Open a share link on a phone | Share page |

## 3. Information architecture

Top-level sections, in this order everywhere (top bar, phone menu, `g` shortcuts):

| Key | Section | Route | Content |
| --- | --- | --- | --- |
| `g n` | Now | `/` | Running leases and takes, running jobs and renders, recent sessions, breakages |
| `g l` | Lab | `/lab` | Rack of devices, browser profiles, backends; `/lab/<kind>/<id>` live view with tool rail |
| `g e` | Evidence | `/evidence` | Sessions, projects, search; deep routes `/p/...`, `/sessions/...`, `/sid/...` |
| `g s` | Studio | `/studio` | Video projects and scores; `/studio/edit?project=` timeline; `/studio/score?path=` score |
| `g o` | Nodes | `/nodes` | Nodes, slots, GPUs; jobs as run blocks |
| `g p` | Plugins | `/plugins` | Plugins by state, approvals, settings, contributed pages |
| `g ,` | Settings | `/settings/<group>` | Hub settings, users, keys, grants |

Plugins add sections only through the `nav.section` slot, after Plugins and before Settings.
Deep links from older versions (`/p/<owner>/<name>/s/<slug>/a/<id>`, `/sessions/<slug>`, `/sid/<sid>`,
`/s/<token>`, `/studio?project=&render=`) keep working.

## 4. Visual language: lightbox, ledger and dope sheet

- Paper page, hairline rules, a 2px ink rule over each section head.
- Media sits in a neutral **well** so white screenshots never dissolve into the page; viewers, live screens
  and previews sit on the dark **stage**, which stays dark in both themes.
- Timelines are drawn on the **sheet**: a frame grid with beat and downbeat rules, track headers in a fixed
  column, clips as flat rectangles.
- Geometry is square: surfaces, wells, tables, dialogs 0 radius; controls and clips 2px.
- No shadows, blur, gradients, glow, transparency over content (except the dialog scrim), no pattern
  backgrounds.

### Tokens (`web/src/styles/tokens.css` is the source of truth; `@eks-harness/ui-sdk` re-exports them)

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--paper` | `#FFFFFF` | `#121314` | page and panels |
| `--canvas` | `#F5F5F2` | `#0C0D0E` | bars, side lists, table heads, track headers |
| `--well` | `#E8E8E4` | `#1D1E21` | thumbnails, media letterbox |
| `--stage` | `#151618` | `#0A0A0B` | viewers, live screens, previews (both themes) |
| `--sheet` | `#FAFAF7` | `#16171A` | timeline body |
| `--ink` | `#141518` | `#ECECE8` | text, primary buttons, strong rules |
| `--ink-2` | `#4A4D53` | `#B4B6BA` | secondary text |
| `--ink-3` | `#686C74` | `#8F9298` | meta, field captions (4.5:1 on paper) |
| `--rule` | `#DCDCD6` | `#2B2D31` | hairlines, frame grid |
| `--rule-beat` | `#C2C2BA` | `#3A3D42` | beat rules on the sheet |
| `--rule-bar` | `#8E8E86` | `#5C6068` | downbeat rules |
| `--mark` | `#D42E18` | `#FF6247` | unseen, selection, focus, errors, playhead |
| `--mark-wash` | `#FBEAE6` | `#3B1A14` | selected row, selected clip |

State colors (second channel only, always after a word; Okabe-Ito, color-blind safe):

| Token | Light | Dark | Word | Shape |
| --- | --- | --- | --- | --- |
| `--state-ok` | `#007A5A` | `#2BB894` | running, active, done, online | solid bar |
| `--state-busy` | `#0063A0` | `#4BA3E3` | starting, rendering, assigned | striped bar |
| `--state-wait` | `#A86E00` | `#E6A835` | queued, pending approval, idle | hatched bar |
| `--state-fail` | uses `--mark` | uses `--mark` | failed, broken, offline | outline + word in 700 |

Track categories (studio and score only; clip fill at 18% on the sheet, full strength in the 4px header
strip and the track code):

| Track | Code | Token | Hue |
| --- | --- | --- | --- |
| Edit / video | `E` | `--track-edit` | `#5E7FA8` |
| Blender | `B` | `--track-blender` | `#B5762E` |
| Web scene | `H` | `--track-web` | `#3E8C7A` |
| Device take | `D` | `--track-device` | `#8A6AA8` |
| Audio | `A` | `--track-audio` | `#7D8B3A` |
| Plugin tracks | `P` | `--track-plugin` | `#7A7A74` |

Spacing is a 4px scale: 4, 8, 12, 16, 24, 32, 48. Radius: `--r-control: 2px`. Rules: 1px `--rule`;
section heads 2px `--ink`. Focus: `outline: 2px solid var(--mark); outline-offset: 2px`. Motion: 120ms
ease-out on opacity and a 4px translate for sheets and drawers only; `prefers-reduced-motion` removes it.
The playhead moves with the clock and is never animated by CSS.

### Type

- Text: **Atkinson Hyperlegible Next** (variable, bundled). Mono: **Atkinson Hyperlegible Mono** for machine
  values only: ids, sids, hashes, paths, ports, URLs, code, logs, timecode, frame numbers. Never for labels or
  meta lines.
- Desktop scale: 11 mono ruler and track codes, 12 meta, 13 UI and table text, 14 body, 16 section titles,
  24 page titles. Phone: 13 meta, 15 body, 20 page titles. Line height 1.45 body, 1.2 titles.
- Weights: 400 body, 600 titles and labels, 700 only for a failed or broken state word.
- Sentence case. No uppercase labels, no letter-spacing tricks, no eyebrow text.
- `font-variant-numeric: tabular-nums slashed-zero` on every number, size, duration and timecode.
- Timecode is `HH:MM:SS:FF` in mono; bars.beats is `bar.beat` (1-based) in mono next to it.

### Glyphs

One 16px stroke set (1.5px stroke, square caps) in `components/Glyph.tsx`, for verbs only: play, pause,
stop, record, step back, step forward, previous, next, close, more, copy, download, external, check, chevron,
grid, list, zoom in, zoom out, snap, lock, eye. Domain objects (platforms, track kinds, capture kinds, node
kinds) are written as words or track codes, never as stock icons. No icon next to a heading. No emoji.

## 5. Layout per device

| Width | Name | Shell | Lists | Viewer / live / studio |
| --- | --- | --- | --- | --- |
| < 700 | phone | 48px top bar: wordmark, current section name, "Menu" text button opening a full-screen section sheet | stacked label/value rows, filters in a bottom sheet | stage 56vh; filmstrip; studio and score in review mode (fixed center playhead, collapsed track headers, scrub and comment only); live device full screen with tool rail as bottom sheet |
| 700-1099 | tablet | top bar with section links; search collapses to a link | 2-4 columns | stage full width, inspector below; one live device with the tool rail beside it; timeline single track list with slide-over inspector |
| 1100-1499 | desktop | top bar with sections, project switcher, search, account | ledger tables, 5-7 thumbnail columns | stage + 360px inspector; studio: preview top, timeline below, 280px inspector right |
| >= 1500 | wide | same, content max 1680px, left aligned, 32px gutter | up to 8 columns | inspector 400px; lab can tile up to four live screens |

No horizontal page scroll at any width; wide content (timelines, logs, code) scrolls inside its own box.
Touch targets 40px on phones, 28px minimum on desktop. Gutters 16 / 24 / 32.

## 6. Components

- **Top bar**: wordmark `eks-harness` in mono 14/600; section links (active = 2px ink rule flush with the
  bar's bottom hairline); project switcher (mono project id); search (`/` focuses); account menu. The event
  stream state appears only when it is down: "Live updates paused, reconnecting".
- **Page head**: crumbs in 13px ink-3, title (mono when it is an identifier), one meta line of facts as
  separate spans, actions right aligned, at most one primary.
- **Section**: 2px ink rule, 16/600 title, count in ink-3 tabular figures, optional quiet actions.
- **Ledger table**: canvas head 12/600 ink-2, 1px row rules, hover canvas, selected mark-wash, keyboard focus
  mark outline, numbers right aligned. Rows 28px desktop. Phones get label/value rows.
- **Rack row** (lab, nodes): name (mono for ids), kind word, state word with its state bar, holder (who has
  the lease, how long), resource facts, and a small live thumbnail in a well when streaming. Click opens the
  live view or the node.
- **Run block** (jobs, flows, backend boots, renders): one header line "name, state, duration, node", a
  progress bar for running blocks (striped `--state-busy`), a collapsible body with the log or the result, a
  copy link. Blocks stack newest first; `j`/`k` move between them.
- **State bar**: a 3px by 16px bar before the state word, solid / striped / hatched / outline per the state
  table. Never a dot, never pulsing.
- **Timeline (the sheet)**:
  - Fixed 168px track header column (desktop): track code in a 4px category strip, track name, compact
    toggles (mute, solo, lock, eye) as 16px glyph buttons that grow with track height.
  - Two rulers on top: timecode (major ticks each second, labels in mono 11) and bars.beats (downbeat rules
    heavy, beat rules light) taken from the score's beat grid.
  - Clips: flat 2px-radius rectangles, 18% category fill, 1px category border, name in 12px, waveform or
    filmstrip inside when the clip is tall enough.
  - Events: 8px flags on an event lane per track (emitted events point up, received inputs point down),
    linked by a 1px rule when a rule fired one from the other.
  - Playhead: 1px `--mark` line with a 9px head in the ruler.
  - Snapping to beats, cues, clip edges and frames; the snap target shows a 1px ink line while dragging.
  - Zoom with `Cmd +/-` and pinch; `Shift Z` fits; an overview strip under the sheet on desktop.
  - Keys: `space` play/pause, `J K L` shuttle, arrows frame step, shift arrows 1 second, `[ ]` previous or
    next beat, `I O` in and out, `M` marker.
- **Live screen**: device or browser frame on the stage, bezel-less, device name and lease state as caption.
  Tool rail (desktop right, tablet beside, phone bottom sheet): home, back, rotate, deep link, fakes (camera,
  NFC, documents, location), record, screenshot, logs. Each tool is a text button with a glyph only for
  verbs.
- **Buttons**: primary = ink fill, paper text; secondary = 1px ink border; quiet = underlined text. Height 28
  (desktop) / 40 (phone). Destructive buttons name the verb and object ("Delete 3 artifacts") and confirm.
- **Fields**: label above (13/600), 1px ink-3 border, 2px radius, help and error below. Numeric fields
  scrub when the label is dragged.
- **Tag token**: 2px radius, 1px border tinted by the tag color, 8px square swatch, label.
- **Dialog**: square, 1px ink border, scrim `rgb(20 21 24 / 0.45)`; full-width bottom sheet on phones.
- **Notice**: 2px left rule in ink (mark for errors) and a sentence.
- **Empty state**: one sentence and the exact command or action that fills it.
- **Loading**: "Loading sessions..." after 300ms; no shimmer, no spinners for less than a second.
- **Toasts**: only for async outcomes the user would miss (render finished, lease lost, node offline).
- **Command palette** (`Cmd K`): every navigation and action, including plugin `command` slot entries.

## 7. Plugin UI

Plugin modules render inside slots (`nav.section`, `route`, `project.tab`, `session.panel`,
`artifact.viewer`, `device.controls`, `backend.inspector`, `node.inspector`, `score.track`,
`score.inspector`, `settings.page`, `command`). They import React and components only from
`@eks-harness/ui-sdk`, use its tokens and components, and follow this document. A slot gets the host's
typed context (project, session, selection, theme, API client, event stream); it must not restyle the host.

## 8. Copy

Sentence case, plain verbs, no exclamation marks, no marketing words. Relative times ("3 min ago") with the
absolute time in the title; absolute dates in detail panels. Counts with units ("4 artifacts", "1.6 GB",
"12 frames"). Errors say what failed and what to do next. Helper text appears only where a field is
ambiguous, in one line. No em dashes.

## 9. Banned patterns (researched AI defaults; never ship)

- Inter, Roboto, Geist, Space Grotesk, IBM Plex, JetBrains Mono, Instrument Serif, system-ui as the brand
  face; cream backgrounds with terracotta accents; serif display headlines.
- Purple, indigo or violet accents, any gradient, gradient text, glow, aurora or neon backgrounds.
- Glassmorphism, `backdrop-filter`, translucent sticky headers, frosted panels.
- Radii above 4px, pills for every tag or state, soft shadows, hover lift, scale on hover, shimmer skeletons,
  count-up numbers, pulsing or colored status dots.
- Stat cards with icon badges, KPI rows, bento grids, three-card feature rows, cards inside cards, colored
  side-stripe cards, numbered 1-2-3 steps, hero sections, centered narrow content columns in tool pages.
- Middle-dot meta strings, arrows after links, monospace for labels, uppercase tracked eyebrows, emoji,
  sparkle icons, Lucide or Heroicons as decoration, icons beside headings, avatar initials as decoration.
- Explanatory paragraphs under headings, disclaimers, illustrated or multi-sentence empty states, toasts for
  synchronous actions, "Live" or "New" badges.

## 10. Review loop (required before a UI change is done)

1. `pnpm --filter ./web build` (type-checks and builds).
2. `uv run python web/tools/preview.py up` starts a throwaway hub from this checkout (own home under a temp
   dir, fake pools, seeded projects, sessions, artifacts of every kind, nodes, jobs, plugins, a studio
   project and a score) on its own port; never the user's hub or data.
3. `uv run python web/tools/preview.py shoot [--route /studio/score?path=...]` writes PNGs per route for
   phone (390x844), tablet (834x1112), desktop (1440x900) and wide (1920x1080) in light and dark, plus a spill
   report (elements wider than the viewport, horizontal page scroll, text overflow, touch targets under the
   minimum). With the Playwright MCP loaded, also take screenshots through it for interactive states
   (menus, drags, dialogs).
4. Open every shot and judge it against this checklist; fix and reshoot until every item holds:
   - one mark color with its one meaning; state words present wherever a state color is used;
   - no banned pattern; no radius above 4px; no shadow, gradient or blur;
   - 4px grid alignment; tabular, right-aligned numbers; mono only for machine values;
   - identifiers copyable and never cut without a title;
   - no horizontal page scroll on any viewport; touch targets met on phone;
   - dark mode readable, contrast 4.5:1 for text;
   - empty, loading and error states present and one line long;
   - the phone and tablet layouts are designed for their job, not squeezed desktop.
5. `uv run python web/tools/preview.py down`.

## 11. File map

| Path | Holds |
| --- | --- |
| `web/src/styles/tokens.css` | tokens above, light and dark |
| `web/src/styles/*.css` | base, components, shell, sheet (timeline), lab, evidence, viewers |
| `web/src/fonts/` | Atkinson Hyperlegible Next and Mono, OFL licenses |
| `web/src/components/` | primitives: Button, Field, Table, Dialog, Menu, Tag, Glyph, StateWord, RunBlock, Timecode |
| `web/src/shell/` | top bar, phone menu, command palette, shortcuts, announcer, plugin slot host |
| `web/src/sheet/` | the shared timeline: rulers, track headers, clips, event lanes, playhead, snapping |
| `web/src/pages/` | one file per route group: now, lab, evidence, studio, score, nodes, plugins, settings |
| `web/src/viewers/` | one viewer per artifact kind, stage-aware |
| `sdk/ui/` | `@eks-harness/ui-sdk`: slot types, hooks, tokens, components for plugin modules |
| `web/tools/preview.py` | the isolated preview hub and screenshot runner |
