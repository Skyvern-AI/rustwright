#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/../../.." && pwd -P)"
base_image="${FORM_FILL_BASE_IMAGE:-rustwright-form-fill-base:latest}"
record_image="${FORM_FILL_RECORD_IMAGE:-rustwright-form-fill-record:latest}"

docker build --tag "$base_image" "$repo_root"
docker build \
  --build-arg "RUSTWRIGHT_BASE_IMAGE=$base_image" \
  --file "$script_dir/../Dockerfile.record" \
  --tag "$record_image" \
  "$repo_root"
