#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
suite_dir="$(cd "$script_dir/.." && pwd -P)"
out_dir="${BENCH_OUT_DIR:-$suite_dir/out}"
image="${FORM_FILL_RECORD_IMAGE:-rustwright-form-fill-record:latest}"

docker run --rm \
  --volume "$out_dir:/output" \
  --entrypoint python \
  "$image" \
  /workspace/benchmarks/form_fill/render_artifacts.py \
  --playwright-run "/output/${PLAYWRIGHT_LABEL:-playwright-record}" \
  --rustwright-run "/output/${RUSTWRIGHT_LABEL:-rustwright-record}" \
  --output-dir /output/rendered
