#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      DRY_RUN=1
      ;;
    -h|--help)
      cat <<'USAGE'
Usage: bash scripts/acceptance.sh [--dry-run]

Run the local 1.0 acceptance gates: lint, full tests, dogfood smoke, replay
baseline/screenshot suites, whitespace checks, and runtime/secret hygiene scans.
USAGE
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

print_command() {
  printf '+'
  for part in "$@"; do
    printf ' %q' "$part"
  done
  printf '\n'
}

run() {
  print_command "$@"
  if (( DRY_RUN == 0 )); then
    "$@"
  fi
}

run_bash() {
  local label="$1"
  local script="$2"
  echo "+ bash -c ${label@Q}"
  if (( DRY_RUN == 0 )); then
    bash -c "$script"
  fi
}

ARTIFACT_DIR="test-artifacts/acceptance"
DOGFOOD_RENDERER_COMMAND="uv run python examples/renderers/gibson_dogfood_renderer.py"

run uv run ruff check .
run uv run pytest
run uv run harn-gibson dogfood --harn-bin true --no-browser --no-codex-auth-import --no-hold-on-error
run uv run harn-gibson replay-dir examples/replays \
  --output-result "$ARTIFACT_DIR/replay-suite.json" \
  --baseline-dir examples/baselines/replays \
  --screenshot-dir "$ARTIFACT_DIR/replay-screenshots"
run uv run harn-gibson replay-dir examples/dogfood-replays \
  --renderer-command "$DOGFOOD_RENDERER_COMMAND" \
  --renderer-timeout-ms 10000 \
  --baseline-dir examples/baselines/dogfood-replays \
  --screenshot-dir "$ARTIFACT_DIR/dogfood-screenshots" \
  --output-result "$ARTIFACT_DIR/dogfood-suite.json"
run git diff --check

run_bash "secret pattern scan" '
set -euo pipefail
pattern="(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|PRIVATE) KEY|xox[baprs]-[A-Za-z0-9-]{20,})"
if rg --hidden -n "$pattern" . --glob '"'"'!uv.lock'"'"' --glob '"'"'!test-artifacts/**'"'"' --glob '"'"'!.git/**'"'"' --glob '"'"'!.venv/**'"'"' --glob '"'"'!.pytest_cache/**'"'"' --glob '"'"'!.ruff_cache/**'"'"'; then
  echo "secret-like values found" >&2
  exit 1
fi
'

run_bash "runtime config scan" '
set -euo pipefail
matches=$(
  find . \( -path ./.git -o -path ./test-artifacts -o -path ./.venv \) -prune -o \
    \( -name auth.json -o -name credentials -o -name credential -o -name '"'"'.env'"'"' -o -name '"'"'.env.*'"'"' -o -name '"'"'*.pem'"'"' -o -name '"'"'*.key'"'"' \) \
    -print
)
if [[ -n "$matches" ]]; then
  printf "%s\n" "$matches" >&2
  exit 1
fi
'
