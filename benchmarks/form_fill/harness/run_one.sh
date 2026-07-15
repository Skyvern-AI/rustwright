#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <playwright|rustwright> <output-label>" >&2
}

if (( $# != 2 )); then
  usage
  exit 2
fi

backend="$1"
label="$2"
case "$backend" in
  playwright|rustwright) ;;
  *) usage; exit 2 ;;
esac
if [[ ! "$label" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "output-label may contain only letters, digits, dots, underscores, and hyphens" >&2
  exit 2
fi
: "${BENCH_JOB_URL:?BENCH_JOB_URL must be set to an authorized target}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
suite_dir="$(cd "$script_dir/.." && pwd -P)"
out_dir="${BENCH_OUT_DIR:-$suite_dir/out}"
mkdir -p "$out_dir"

record="${BENCH_RECORD:-0}"
if [[ "$record" == "1" ]]; then
  image="${FORM_FILL_RECORD_IMAGE:-rustwright-form-fill-record:latest}"
else
  image="${FORM_FILL_BASE_IMAGE:-rustwright-form-fill-base:latest}"
fi

docker_args=(
  --rm
  --memory="${BENCH_MEMORY_LIMIT:-8g}"
  --memory-swap="${BENCH_MEMORY_LIMIT:-8g}"
  --cpus="${BENCH_CPUS:-4}"
  --shm-size="${BENCH_SHM_SIZE:-1g}"
  --volume "$out_dir:/output"
  --env BENCH_JOB_URL
  --env "BENCH_CHROMIUM_EXECUTABLE=${BENCH_CHROMIUM_EXECUTABLE:-/usr/local/bin/rustwright-chromium}"
  --entrypoint python
)

for variable in \
  CDP_URL \
  CDP_CONNECT_HEADERS \
  SKYVERN_SESSION \
  BENCH_PAUSE_SCALE \
  BENCH_SKIP_UPLOADS
do
  if [[ -n "${!variable:-}" ]]; then
    docker_args+=(--env "$variable")
  fi
done

if [[ "${SKYVERN_SESSION:-0}" == "1" && -z "${CDP_URL:-}" ]]; then
  for variable in SKYVERN_CLOUD_API_KEY SKYVERN_BASE_URL; do
    if [[ -n "${!variable:-}" ]]; then
      docker_args+=(--env "$variable")
    fi
  done
fi

if [[ -n "${BENCH_FIELD_CONFIG_HOST:-}" ]]; then
  config_dir="$(cd "$(dirname "$BENCH_FIELD_CONFIG_HOST")" && pwd -P)"
  config_name="$(basename "$BENCH_FIELD_CONFIG_HOST")"
  docker_args+=(
    --volume "$config_dir:/bench-config:ro"
    --env "BENCH_FIELD_CONFIG=/bench-config/$config_name"
  )
fi

if [[ "$backend" == "playwright" ]]; then
  docker_args+=(--env "PYTHONPATH=/opt/playwright-reference")
fi

measure_args=(
  /workspace/benchmarks/form_fill/harness/measure.py
  --backend "$backend"
  --output "/output/$label"
)
if [[ "$record" == "1" ]]; then
  measure_args+=(--record)
fi

docker run "${docker_args[@]}" "$image" "${measure_args[@]}"
