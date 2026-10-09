# UI research: AI-default tells and what pro tools do

Input for `claude/skills/eks-harness-ui-design`. Collected 2026-10-09.

## AI-default tells and why they hurt a pro tool

Measured evidence: Adrian Krebs scored about 1,500 Show HN sites with deterministic DOM/CSS checks; 22% hit
4+ slop patterns, 32% hit 2-3 ([design slop study](https://www.adriankrebs.ch/blog/design-slop/)). Catalogs that
agree with it: [Impeccable slop catalog](https://impeccable.style/slop), [Fountain Institute, 7 signs of a
vibe-coded UI](https://www.thefountaininstitute.com/blog/signs-vibe-coded-ui), [Anthropic, improving frontend
design through skills](https://claude.com/blog/improving-frontend-design-through-skills), [Matthew Blode, you
don't have to make AI slop](https://matthewblode.com/blog/you-dont-have-to-make-ai-slop). Models sample the
high-probability center of web design; removing tells is necessary, not sufficient.

| Tell | Why it hurts here |
| --- | --- |
| Decorative and pulsing status dots | In a device lab state matters; decoration trains users to ignore real state |
| Blurred translucent sticky headers, glass | Low contrast over content, GPU cost next to live streams and timelines |
| Lucide icon soup | Generic glyphs cannot tell a Blender scene from an HTML scene from a device take |
| Large radii, pills everywhere | Eats corners of thumbnails and clips; every state looks like a button |
| Purple/indigo gradients, glows | Spends the color budget; falsifies the surround we judge renders against |
| Cards in cards, identical card grids | Flat hierarchy; 300 captures need rows and columns, not tiles |
| Colored side stripes | Everything looks like an alert |
| Heroes and stat tiles in a tool | The first screen must be the work |
| Helper paragraphs, disclaimers, empty-state essays | Permanent noise for experts |
| Emoji | Inconsistent rendering, unthemable |
| Low density, centered narrow column | Wastes the workstation screen |
| Gray on gray | Unreadable IDs and logs |
| Toasts for everything | Cover controls, announce the obvious |
| Default stacks (Inter, Geist, Space Grotesk, Instrument Serif, cream + terracotta) | Interchangeable products |

## Borrow from pro tools

- **NLE/DAW timelines** (Resolve, Premiere, Final Cut, Ableton, Reaper): fixed-width track header column whose
  controls grow with track height (Reaper TCP); track codes by type (E1 edit, B1 Blender, H1 HTML, D1 device,
  A1 audio); timecode ruler with a second bars.beats ruler (Ableton) and heavier downbeat ticks; one playhead,
  JKL shuttle, frame stepping; visible snapping with a snap line (beats matter most); zoom to fit and an
  overview strip; colored editorial markers separate from the lighter beat lane; flat clips, 2px radius,
  category color per track type (Final Cut roles); every action on a key, shown in tooltips and a palette;
  renders as a job queue with text progress.
- **Device farms** (BrowserStack, Sauce Labs, Genymotion, Android Studio, Xcode Simulator): device inventory as a
  table; running devices as tabs; a tool rail beside the live device (rotate, home, network, fakes); bezel-less
  frames with a text caption; lease state as words with time.
- **Observability** (Grafana, Honeycomb, Datadog, Sentry, Linear): one global time range every panel obeys,
  shared crosshair; state timeline bands for lease and device history; facet sidebar with counts and URL-held
  queries; short inline lists with a full drawer; dim chrome so content leads.
- **Warp**: each process run (backend boot, flow, render) is a block with a header (name, exit, duration) and a
  collapsible body.
- **Figma**: 240px properties panel, compact two-column numeric fields, scrubbable number labels.

Conventions: 13px UI text, 24-28px rows, 4px grid, hairlines instead of boxes, one sans plus one mono (mono for
ids, timecode, paths, logs), tabular figures, neutral chrome, color only for state and media category, text
labels first and a small custom glyph set for domain objects, functional motion of 160ms or less.

## Type and color

- Atkinson Hyperlegible Next + Atkinson Hyperlegible Mono (Google Fonts, 2025): distinctive, unambiguous 0/O
  and 1/l/I for sids and hashes. Timecode and numeric columns use tabular figures.
- One mark color with one meaning (needs the reader: unseen, selected, focus, error).
- State colors only as a second channel after the word, from Okabe-Ito: error `#D55E00`, warn `#E69F00`, ok
  `#009E73`, running `#0072B2`; plus shape or pattern (hatched queued, solid active, outline idle).
- Track categories: five muted Okabe-Ito hues (about 60% chroma in OKLCH).
- The media stage stays dark in both themes.

## Responsive

- Studio on phone: preview on top, timeline below, playhead fixed in the center with the timeline scrolling
  under it, collapsed track headers (category strip + code), pinch zoom; phone is for review, markers and
  comments, not trimming ([Resolve for iPad review](https://ymcinema.com/2022/12/07/davinci-resolve-for-ipad-a-review-and-insights/)).
  Tablet: single timeline, inspector as a slide-over.
- Lab on phone: device list rows, full-screen live view with the tool rail as a bottom sheet, swipe between
  leased devices; never a grid of live streams. Tablet: list left, one device right.
- Evidence on phone: contact-sheet strip and a full-screen viewer; share is a primary action. Logs as two-line
  rows, code blocks scroll inside themselves, never the page.
