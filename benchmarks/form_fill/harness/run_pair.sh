#!/usr/bin/env bash
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
overall=0

echo "[1/2] reference Playwright"
"$script_dir/run_one.sh" playwright "${PLAYWRIGHT_LABEL:-playwright}" || overall=$?

echo "[2/2] Rustwright"
"$script_dir/run_one.sh" rustwright "${RUSTWRIGHT_LABEL:-rustwright}" || {
  code=$?
  if (( overall == 0 )); then
    overall=$code
  fi
}

exit "$overall"
