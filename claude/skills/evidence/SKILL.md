---
name: evidence
description: "Work with the evidence store: projects, sessions, artifacts (screenshots, videos, DOM, accessibility and MHTML dumps, HAR, logs, sites, renders), the artifact links in eks run reports, downloads, tags, retention, search, share links, annotated screenshot versions and putting evidence into pull requests. Use when presenting captures to people or pull requests."
---

# Evidence and PR screenshots

Every capture of a run is an artifact in a project (`owner/name`) and a session (usually the branch). One
project per repository is the norm; tags tell apps and parts apart (`tags = [...]` in an app's
`.harness/app.toml`, and every run tags its captures `run:<id>`). Filter with `--tag`, not with separate
projects. An admin folds projects together with `eks-harness projects merge <a> <b> --into owner/name
--apply`.

## From a run report

`eks-harness run` prints `artifacts:` lines, `<kind> <name> <page url> <raw url>`, and the failure
screenshots under each failure; `--json` (and `eks_run`, `eks_do`, `eks_session_do`) gives `artifacts` with
`id`, `kind`, `name`, `url` and `rawUrl`. Before you report a capture, look at it:

```bash
eks-harness artifacts download <id> -o /tmp/evidence      # then Read the file
eks-harness video sheet <id>                              # recordings and renders: Read the local sheet
```

MCP: `get_artifact(artifact_id=<id>, image="full")` attaches the image; `video_sheet(artifact_id=<id>)`
attaches the sheet. The plugin's Stop hook blocks a report that skipped this.

## Store commands

```bash
eks-harness artifacts list --project acme/web --session feature/x --tag web-app
eks-harness artifacts tag <id> evidence
eks-harness search "checkout error"
eks-harness share create <id>                 # a link that opens without a login
eks-harness upload file.png --project acme/web --session feature/x --caption "..."
eks-harness timeline feature/x                # lease events, captures and notes in order
eks-harness annotate versions <id>            # versions of an annotated screenshot; rerender, recapture, restore
```

- Artifact links: `url` (page in the UI, needs login), `rawUrl` (the file), share links for people
  without a login. Give people the links along with what each capture shows.
- A before/after pair is captured "before" first: record the before take on the untouched code before
  any code change, then the after take with the same persona, page and data (skill `eks-flows`).
- Annotated screenshots made by `eks.annotate(...)` in a flow are new artifacts with their own versions;
  the store keeps every version and can re-render or re-capture them.
- Pull requests that change UI carry evidence: screenshots (and a recording for animated changes) as share
  links in the body. A change with no visible effect says `NO-UI-EVIDENCE` and why. The plugin's PR guard
  checks this for the paths a repo lists in `[claude] ui_paths`.
- Old data goes on the UI's Evidence > Clean up page (`/cleanup`): conditions on project, session, age,
  kind, tags, source, size, text and viewed state, with a live preview before a confirmed delete. Pinned
  artifacts, live share links and captures of live leases stay unless included. Delete only when the user
  asks for it.
