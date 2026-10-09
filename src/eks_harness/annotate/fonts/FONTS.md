# Bundled annotation fonts

Render-usable TTF copies of the IBM Plex OFL families already vendored for
the web UI at `tools/harness/web/src/fonts/` (`IBMPlexSans-Regular.woff2`,
`IBMPlexSans-SemiBold.woff2`, `IBMPlexMono-Regular.woff2`, plus `OFL.txt`).

Why copies: woff2 is a web delivery format. The annotation renderer
(`eks_harness.annotate.render`) needs TTF/OTF files it can pass to resvg
(`font_files`) and measure with PIL/fontTools, identically on every machine.
System fonts vary per machine, so deterministic rendering requires these
bundled files. Never a system font name.

Source license: SIL Open Font License 1.1, see `tools/harness/web/src/fonts/OFL.txt`.
The TTF files are byte conversions of the same font data, so the same license
applies.

Conversion command (run from the repo root):

```bash
python3 -c "
from fontTools.ttLib import TTFont
for name in ['IBMPlexSans-Regular', 'IBMPlexSans-SemiBold', 'IBMPlexMono-Regular']:
    font = TTFont(f'tools/harness/web/src/fonts/{name}.woff2')
    font.flavor = None
    font.save(f'tools/harness/src/eks_harness/annotate/fonts/{name}.ttf')
"
```

Files:

- `IBMPlexSans-Regular.ttf` (body text, callouts, labels, captions)
- `IBMPlexSans-SemiBold.ttf` (badges, titles)
- `IBMPlexMono-Regular.ttf` (optional mono use)
