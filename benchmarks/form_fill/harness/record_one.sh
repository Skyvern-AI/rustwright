#!/usr/bin/env bash
set -euo pipefail

if (( $# != 2 )); then
  echo "Usage: $0 <playwright|rustwright> <output-label>" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BENCH_RECORD=1 "$script_dir/run_one.sh" "$1" "$2"
