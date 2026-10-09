---
description: Render an eks-harness video project or score
argument-hint: "[args]"
---

Load eks-harness:video-editing and eks-harness:scores. If $ARGUMENTS is a score (score.py or score.json) run `eks-harness score plan` then `eks-harness score render`; otherwise `eks-harness video validate` then `eks-harness video render`. Then load eks-harness:video-review: run `eks-harness video sheet` on the render with the score's beats and cues as markers and Read the sheet, run `eks-harness video check` when expectations exist, and report the output path, the studio link, the sheet page link and the check verdict.
