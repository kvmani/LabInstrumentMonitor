#!/usr/bin/env bash
# Local quality gate. CI is manual-only (workflow_dispatch), so run this before every push.
#   scripts/check.sh          lint, types, unit + integration tests, UI typecheck and build
#   scripts/check.sh --e2e    additionally the real-process and browser suites
#   scripts/check.sh --slow   additionally the 30-day fixture and latency evidence
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
for arg in "$@"; do
  case "$arg" in
    --e2e) step "pytest e2e (real processes + Chromium)"; "$PY/pytest" -q tests/e2e ;;
    --slow) step "pytest slow evidence (30-day fixture, latency)"
            LABMON_RUN_SLOW=1 "$PY/pytest" -q -s tests/perf ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
  esac
done
step "all checks passed"
