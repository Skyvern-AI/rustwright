#!/usr/bin/env bash
set -euo pipefail

IMAGE="${RUSTWRIGHT_DOCKER_IMAGE:-rustwright-verify}"
HOST_WORKDIR="${RUSTWRIGHT_DOCKER_WORKDIR:-$PWD}"
# Hard cap for local verification builds and containers. Keep swap equal
# to memory so Docker cannot spill beyond the configured test budget.
TEST_DOCKER_MEMORY_LIMIT="${TEST_DOCKER_MEMORY_LIMIT:-8g}"
readonly TEST_DOCKER_MEMORY_SWAP_LIMIT="$TEST_DOCKER_MEMORY_LIMIT"
INSTALL_PUPPETEER="${INSTALL_PUPPETEER:-0}"
RUSTWRIGHT_DOCKER_BASE_IMAGE="${RUSTWRIGHT_DOCKER_BASE_IMAGE:-python:3.13-slim-bookworm}"
RUSTWRIGHT_DOCKER_LEGACY="${RUSTWRIGHT_DOCKER_LEGACY:-0}"
DOCKER_BIN="${DOCKER:-docker}"

usage() {
  cat >&2 <<'USAGE'
Usage:
  tools/docker_test.sh build [docker build args...]
  tools/docker_test.sh [pycompile|focused|parity|sampled|phase1|full|bench|bench-full|mind2web|mind2web-full|webvoyager|webvoyager-full|antibot|antibot-smoke] [mode args...]

Environment:
  RUSTWRIGHT_DOCKER_IMAGE          Image name. Defaults to rustwright-verify.
  RUSTWRIGHT_DOCKER_WORKDIR        Host worktree used for source/test file mounts. Defaults to $PWD.
  INSTALL_PUPPETEER                Build arg for optional puppeteer-core install. Defaults to 0.
  RUSTWRIGHT_DOCKER_BASE_IMAGE     Build base image. Defaults to python:3.13-slim-bookworm.
  RUSTWRIGHT_DOCKER_LEGACY         Set to 1 to build without BuildKit cache mounts.
  BENCHMARK_FULL_ITERATIONS        Iterations for bench-full. Defaults to 10.
  MIND2WEB_FULL_ITERATIONS         Iterations for mind2web-full. Defaults to 1.
  WEBVOYAGER_FULL_ITERATIONS       Iterations for webvoyager-full. Defaults to 1.
  BENCHMARK_CHROMIUM_EXECUTABLE    Optional in-container Chromium path used by all benchmarked Playwright-style implementations.
  RUSTWRIGHT_BENCH_REBUILD         Set to 1 for tools/docker_test.sh bench Rustwright/all release-wheel rebuilds.
  RUSTWRIGHT_DOCKER_REBUILD_TARGET_CACHE
                                    Set to 1 to mount a Docker volume at /workspace/target during Rustwright benchmark rebuilds.
  RUSTWRIGHT_DOCKER_REBUILD_CACHE_PREFIX
                                    Optional Docker volume prefix for the rebuild target cache.

This wrapper is the host-side Docker entrypoint for tests. It applies Docker
memory limits to verification image builds and every test container so local
runs cannot exceed 8GB. TEST_DOCKER_MEMORY_LIMIT may lower the cap for unstable
local Docker VMs, but values above 8g are rejected.
USAGE
}

validate_memory_limit() {
  case "$TEST_DOCKER_MEMORY_LIMIT" in
    1g|2g|3g|4g|5g|6g|7g|8g|1024m|2048m|3072m|4096m|5120m|6144m|7168m|8192m)
      ;;
    *)
      cat >&2 <<ERROR
TEST_DOCKER_MEMORY_LIMIT must be 8g or lower. Got: ${TEST_DOCKER_MEMORY_LIMIT}
ERROR
      exit 2
      ;;
  esac
}

reject_memory_override_args() {
  local arg
  for arg in "$@"; do
    case "$arg" in
      --memory|--memory=*|--memory-swap|--memory-swap=*|-m|-m=*)
        cat >&2 <<ERROR
Do not pass Docker memory override flags to this wrapper.
tools/docker_test.sh always uses --memory=${TEST_DOCKER_MEMORY_LIMIT} --memory-swap=${TEST_DOCKER_MEMORY_SWAP_LIMIT}.
ERROR
        exit 2
        ;;
    esac
  done
}

add_worktree_mount() {
  local relative_path="$1"
  if [ -e "${HOST_WORKDIR}/${relative_path}" ]; then
    run_args+=(--volume "${HOST_WORKDIR}/${relative_path}:/workspace/${relative_path}")
  fi
}

if [ "$#" -eq 0 ]; then
  mode="sampled"
else
  mode="$1"
  shift
fi

validate_memory_limit

case "$mode" in
  build)
    reject_memory_override_args "$@"
    if [ "$#" -eq 0 ]; then
      set -- .
    fi
    build_args=(
      build
      --memory "$TEST_DOCKER_MEMORY_LIMIT"
      --memory-swap "$TEST_DOCKER_MEMORY_SWAP_LIMIT"
      --build-arg "RUSTWRIGHT_DOCKER_BASE_IMAGE=${RUSTWRIGHT_DOCKER_BASE_IMAGE}"
      --build-arg "INSTALL_PUPPETEER=${INSTALL_PUPPETEER}"
      -t "$IMAGE"
    )
    if [ "$RUSTWRIGHT_DOCKER_LEGACY" = "1" ]; then
      legacy_tmpdir="$(mktemp -d)"
      trap 'rm -rf "$legacy_tmpdir"' EXIT
      perl -0pe 's/^# syntax=.*\n//; s/--mount=type=cache,target=[^\\\n]+\\\n[ \t]*//g; s/--mount=type=cache,target=\S+[ \t]*//g' \
        Dockerfile > "$legacy_tmpdir/Dockerfile"
      build_args+=(-f "$legacy_tmpdir/Dockerfile")
    fi
    exec "$DOCKER_BIN" "${build_args[@]}" "$@"
    ;;
  bench-full)
    reject_memory_override_args "$@"
    exec "${PYTHON:-python3}" "${HOST_WORKDIR}/tools/run_benchmark_matrix.py" \
      --iterations "${BENCHMARK_FULL_ITERATIONS:-10}" \
      "$@"
    ;;
  mind2web-full)
    reject_memory_override_args "$@"
    exec "${PYTHON:-python3}" "${HOST_WORKDIR}/tools/run_mind2web_matrix.py" \
      --iterations "${MIND2WEB_FULL_ITERATIONS:-1}" \
      "$@"
    ;;
  webvoyager-full)
    reject_memory_override_args "$@"
    exec "${PYTHON:-python3}" "${HOST_WORKDIR}/tools/run_webvoyager_matrix.py" \
      --iterations "${WEBVOYAGER_FULL_ITERATIONS:-1}" \
      "$@"
    ;;
  pycompile|focused|parity|sampled|phase1|full|bench|mind2web|webvoyager|antibot|antibot-smoke)
    run_args=(
      run
      --rm
      --memory "$TEST_DOCKER_MEMORY_LIMIT"
      --memory-swap "$TEST_DOCKER_MEMORY_SWAP_LIMIT"
      -e "RUSTWRIGHT_CONTAINER_ISOLATION=separate_container"
	    )
	    if [ "$mode" = "bench" ] && [ "${RUSTWRIGHT_BENCH_REBUILD:-0}" != "0" ] && [ "${RUSTWRIGHT_DOCKER_REBUILD_TARGET_CACHE:-0}" != "0" ]; then
	      cache_prefix="${RUSTWRIGHT_DOCKER_REBUILD_CACHE_PREFIX:-rustwright-bench-${IMAGE}}"
	      cache_prefix="$(printf '%s' "$cache_prefix" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_.-]/-/g')"
	      run_args+=(--volume "${cache_prefix}-target:/workspace/target")
	    fi
	    for worktree_path in \
      benchmarks \
      .github \
      tests \
      tools \
      docs \
      src \
      python/rustwright/__init__.py \
      python/rustwright/_devices.py \
      python/rustwright/async_api.py \
      python/rustwright/cli.py \
      python/rustwright/pytest_plugin.py \
      python/rustwright/sync_api.py \
      python/playwright \
      python/pytest_playwright \
      Cargo.lock \
      Cargo.toml \
      Dockerfile \
      pyproject.toml \
      README.md \
      BENCHMARK.md \
      CODE_ARCHITECTURE.md
    do
      add_worktree_mount "$worktree_path"
    done
    add_worktree_mount ".benchmark-data"
    for env_name in \
      BENCHMARK_ITERATIONS \
      TEST_DOCKER_MEMORY_LIMIT \
      BENCHMARK_CHROMIUM_EXECUTABLE \
      MIND2WEB_ITERATIONS \
      MIND2WEB_FIXTURE_TIMEOUT_MS \
      MIND2WEB_FIXTURE_WAIT_UNTIL \
      MIND2WEB_MAX_TASK_SECONDS \
      MIND2WEB_PROGRESS_EVERY \
      WEBVOYAGER_ITERATIONS \
      WEBVOYAGER_NAVIGATION_TIMEOUT \
      WEBVOYAGER_NETWORK_WARMUP_URL \
      WEBVOYAGER_NETWORK_WARMUP_TIMEOUT \
      WEBVOYAGER_BROWSER_LIFECYCLE \
      WEBVOYAGER_TASK_RETRIES \
      ANTIBOT_SMOKE_ITERATIONS \
      ANTIBOT_MATRIX_ITERATIONS \
      ANTIBOT_NETWORK_ITERATIONS \
      PLAYWRIGHT_REFERENCE_PATH \
      PUPPETEER_PACKAGE_PATH \
	      RUSTWRIGHT_BENCH_REBUILD \
	      RUSTWRIGHT_DOCKER_REBUILD_TARGET_CACHE \
	      RUSTWRIGHT_DOCKER_REBUILD_CACHE_PREFIX \
	      RUSTWRIGHT_CDP_TRANSPORT \
	      RUSTWRIGHT_INCLUDE_PUPPETEER_BENCHMARK \
      PYTHON
    do
      if [ -n "${!env_name:-}" ]; then
        run_args+=(-e "${env_name}=${!env_name}")
      fi
    done
    exec "$DOCKER_BIN" "${run_args[@]}" "$IMAGE" "$mode" "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
