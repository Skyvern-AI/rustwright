#!/usr/bin/env bash
set -euo pipefail

if (( $# != 2 )); then
  echo "Usage: $0 <playwright|rustwright> <output-label>" >&2
  exit 2
fi
: "${CDP_URL:?CDP_URL must be set to a provider-neutral CDP endpoint}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BENCH_SKIP_UPLOADS="${BENCH_SKIP_UPLOADS:-1}" \
  "$script_dir/run_one.sh" "$1" "$2"
