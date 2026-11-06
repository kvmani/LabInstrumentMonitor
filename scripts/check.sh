#!/usr/bin/env bash
# Local quality gate. CI is manual-only (workflow_dispatch), so run this before every push.
#   scripts/check.sh          lint, types, unit + integration tests, UI typecheck and build
#   scripts/check.sh --e2e    additionally the real-process and browser suites
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin
step() { printf '\n==> %s\n' "$*"; }

step "ruff (lint + format)"
"$PY/ruff" check src tests
"$PY/ruff" format --check src tests
step "mypy --strict"
"$PY/mypy"
step "pytest unit + integration (real PostgreSQL 16)"
"$PY/pytest" -q tests/unit tests/integration
step "web: typecheck + build"
(cd web && npm run --silent typecheck && npm run --silent build >/dev/null)
if [[ "${1:-}" == "--e2e" ]]; then
  step "pytest e2e (real processes + Chromium)"
  "$PY/pytest" -q tests/e2e
fi
step "all checks passed"
