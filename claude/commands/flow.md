---
description: Run an eks flow and read its evidence
argument-hint: "<file.py[::name]> [--platform ios|android|web] [--set k=v]"
---

Load the eks-harness:eks-flows skill. Run `eks-harness plan $ARGUMENTS` first when the file is new or changed, then `eks-harness run $ARGUMENTS`. For every screenshot in the report, download it (`eks-harness artifacts download <id> -o <dir>`) and Read it; for every recording, run `eks-harness video sheet <id>` and Read the sheet (for timing questions use `eks-harness video check`, skill eks-harness:video-review). Then report the run status, failures with their source lines, the checkpoint table, what is on screen, and the artifact links.
