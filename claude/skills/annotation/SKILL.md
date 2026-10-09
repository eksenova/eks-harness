---
name: annotation
description: Annotate screenshots and images with eks-harness annotate: spec format, callouts, highlights, arrows, blur, crops, re-render and recapture, versions. Use for marking up still captures; videos are annotated live during recording instead.
---

# Annotation

Still images: `eks-harness annotate <artifact-id> --spec spec.json` renders a version 1 spec (callouts,
highlights, arrows, labels, blur, crop) onto the capture and stores a new version.

```bash
eks-harness annotate <id> --spec spec.json
eks-harness annotate rerender <id>
eks-harness annotate recapture <id>
eks-harness annotate versions <id>
eks-harness annotate restore <id> --version 2
```

Videos: never annotate afterwards. Draw while recording (`app.title`, `app.caption`, `app.callout`,
`app.spotlight` inside `app.recording_of(...)`); the uploaded video is the deliverable.
