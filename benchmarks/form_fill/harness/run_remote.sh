#!/usr/bin/env bash
set -euo pipefail

if (( $# != 2 )); then
  echo "Usage: $0 <playwright|rustwright> <output-label>" >&2
  exit 2
fi

skyvern_session="${SKYVERN_SESSION:-0}"
case "$skyvern_session" in
  0|1) ;;
  *) echo "SKYVERN_SESSION must be 0 or 1" >&2; exit 2 ;;
esac
if [[ -z "${CDP_URL:-}" && "$skyvern_session" != "1" ]]; then
  echo "CDP_URL is required unless SKYVERN_SESSION=1 provisions a session" >&2
  exit 2
fi
if [[ -z "${CDP_URL:-}" && -z "${SKYVERN_CLOUD_API_KEY:-}" ]]; then
  echo "SKYVERN_CLOUD_API_KEY is required when SKYVERN_SESSION=1" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BENCH_SKIP_UPLOADS="${BENCH_SKIP_UPLOADS:-1}" \
  "$script_dir/run_one.sh" "$1" "$2"
