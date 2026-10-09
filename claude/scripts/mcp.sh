#!/bin/sh
set -eu
if command -v eks-harness >/dev/null 2>&1; then
  exec eks-harness mcp "$@"
fi
if [ -n "${EKS_HARNESS_SOURCE:-}" ] && [ -f "$EKS_HARNESS_SOURCE/pyproject.toml" ]; then
  exec uv run --quiet --project "$EKS_HARNESS_SOURCE" eks-harness mcp "$@"
fi
exec uvx --quiet --from "git+https://github.com/eksenova/eks-harness" eks-harness mcp "$@"
