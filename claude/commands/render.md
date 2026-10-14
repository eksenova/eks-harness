---
description: Render an eks edit or scene, or an earlier video project or score
argument-hint: "<file.py[::name]>"
---

Load eks-harness:eks-edit and eks-harness:video-review. If $ARGUMENTS is an eks entry (`@eks.edit`, or `@eks.scene(kind="gfx")`), run `eks-harness plan $ARGUMENTS`, then `eks-harness run $ARGUMENTS`. If it is an earlier video project (`project.py` with `Project(...)`), load eks-harness:video-editing and run `eks-harness video validate` then `eks-harness video render`; for an earlier score (`score.py` built with `eks_harness.score`), load eks-harness:scores and run `eks-harness score plan` then `eks-harness score render`. Blender and web scenes and `@eks.score` entries plan with `eks-harness plan` but do not render under `eks-harness run` yet; say so. After the render, run `eks-harness video sheet <artifact>` with the beats and cues as markers and Read the sheet, run `eks-harness video check` when expectations exist outside the edit, and report the artifact page and direct links, the sheet page link and the check verdict.
