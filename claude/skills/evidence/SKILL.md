---
name: evidence
description: Work with the evidence store: projects, sessions, artifacts (screenshots, videos, DOM, HAR, logs, sites), links, downloads, tags, retention, search, share links and putting evidence into pull requests. Use when presenting captures to people or pull requests.
---

# Evidence and PR screenshots

Every capture is an artifact in a project (`owner/name`) and a session (usually the branch).

```bash
eks-harness artifacts list --project acme/web --session feature/x
eks-harness artifacts download <id>
eks-harness artifacts tag <id> evidence
eks-harness search "checkout error"
eks-harness share create <id>
eks-harness upload file.png --project acme/web --session feature/x --caption "..."
```

- Artifact links: `url` (page in the UI, needs login), `rawUrl` (the file), share links for people
  without a login. Present links, not just descriptions.
- Before you report a capture, download and Read it; for videos Read the contact sheet.
- Pull requests that change UI carry evidence: screenshots (and a video for animated changes) as share
  links in the body. A change with no visible effect says `NO-UI-EVIDENCE` and why. The plugin's PR guard
  checks this for the paths a repo lists in `[claude] ui_paths`.
