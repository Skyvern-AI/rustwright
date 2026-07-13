#!/usr/bin/env bash
set -euo pipefail

cd "${RUSTWRIGHT_WORKDIR:-/workspace}"

PYTHON_BIN="${PYTHON:-python}"
REFERENCE_PATH="${PLAYWRIGHT_REFERENCE_PATH:-.audit-playwright}"
MODE="${1:-sampled}"
shift || true
readonly MAX_TEST_MEMORY_BYTES=8589934592

run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

inside_container() {
  if [ -f /.dockerenv ]; then
    return 0
  fi
  if [ -r /proc/1/cgroup ] && grep -qaE 'docker|containerd|kubepods|libpod' /proc/1/cgroup; then
    return 0
  fi
  return 1
}

read_memory_limit_bytes() {
  if [ -r /sys/fs/cgroup/memory.max ]; then
    cat /sys/fs/cgroup/memory.max
    return
  fi
  if [ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]; then
    cat /sys/fs/cgroup/memory/memory.limit_in_bytes
    return
  fi
  echo "unknown"
}

enforce_container_memory_limit() {
  if ! inside_container; then
    return
  fi

  local limit
  limit="$(read_memory_limit_bytes)"
  case "$limit" in
    ''|max|unknown)
      cat >&2 <<'ERROR'
Docker verification is running without a detected memory cap.
Use tools/docker_test.sh so test builds and containers run with --memory=8g --memory-swap=8g.
ERROR
      exit 1
      ;;
    *[!0-9]*)
      echo "Docker verification could not parse cgroup memory limit: $limit" >&2
      exit 1
      ;;
  esac

  if [ "$limit" -gt "$MAX_TEST_MEMORY_BYTES" ]; then
    cat >&2 <<ERROR
Docker verification memory cap is above 8GB: ${limit} bytes.
Use tools/docker_test.sh, or run Docker directly with --memory=8g --memory-swap=8g.
ERROR
    exit 1
  fi
}

check_browser_executables() {
  local root
  local failed=0
  for root in "${RUSTWRIGHT_BROWSERS_PATH:-/ms-rustwright}" "${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"; do
    if [ ! -d "$root" ]; then
      continue
    fi
    while IFS= read -r path; do
      if [ ! -x "$path" ]; then
        echo "Browser executable is not runnable: $path" >&2
        failed=1
      fi
    done < <(
      find "$root" -type f \( \
        -name chrome -o \
        -name chrome_crashpad_handler -o \
        -name chromium_headless_shell -o \
        -name headless_shell \
      \)
    )
  done
  if [ "$failed" -ne 0 ]; then
    cat >&2 <<'ERROR'
Docker benchmark image contains browser files without executable bits.
Rebuild the image through tools/docker_test.sh build so cached Chromium copies
are normalized before benchmark containers launch browsers.
ERROR
    exit 1
  fi
}

run_pytest() {
  run env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$PYTHON_BIN" -m pytest -p rustwright.pytest_plugin "$@"
}

pycompile() {
  run "$PYTHON_BIN" -m py_compile \
    python/rustwright/sync_api.py \
    python/rustwright/async_api.py \
    benchmarks/run_benchmarks.py \
    benchmarks/automation_cases.py \
    tests/test_rustwright_sync_api.py \
    tools/download_mind2web.py \
    tools/import_mind2web.py \
    tools/import_webvoyager.py \
    tools/run_mind2web_benchmark.py \
    tools/run_mind2web_matrix.py \
    tools/run_webvoyager_benchmark.py \
    tools/run_webvoyager_matrix.py \
    tools/run_parity_cases.py \
    tools/run_antibot_benchmarks.py \
    tools/run_benchmark_matrix.py \
    tools/render_benchmark_matrix.py \
    tools/check_cross_library_speed_goal.py \
    tools/check_launch_latency_claim.py \
    tools/run_remote_docker_test.py \
    tools/query_project_state.py \
    tools/render_project_tables.py \
    tools/check_phase1_gate.py \
    tools/check_native_extension.py \
    tools/api_surface_audit.py \
    tools/check_benchmark_artifacts.py \
    tools/check_testbox_visibility.py \
    tools/run_skyvern_replacement_smoke.py \
    tools/run_skyvern_cloud_overlay_tests.py \
    tools/run_skyvern_alias_command.py \
    tools/run_skyvern_prompt_overlay_smoke.py \
    tests/test_project_status_tools.py \
    tests/test_skyvern_replacement_smoke.py \
    tests/test_skyvern_cloud_overlay_tests.py \
    tests/test_skyvern_alias_command.py \
    tests/test_skyvern_prompt_overlay_smoke.py
  run "$PYTHON_BIN" tools/check_native_extension.py
}

sampled() {
  pycompile
  run_pytest -q tests/test_rustwright_sync_api.py \
    -k "viewport_screen_and_device_scale_option_validation or context_no_viewport_disables_default_viewport or browser_new_page_no_viewport_disables_implicit_context_viewport or record_video_size_option_validation_matches_playwright or route_fetch_relative_url_validation_matches_playwright or route_fetch_unsupported_protocol_errors_match_playwright or api_request_context_invalid_url_errors_match_playwright or api_request_context_dispose_reason_and_header_errors_match_playwright or context_request_skips_blank_context_cookie_sync_until_page_can_mutate_cookies or api_request_context_negative_max_redirects_default_matches_playwright or api_request_context_negative_timeout_errors_match_playwright or expect_to_have_count_timeout_message_matches_playwright or simple_role_count_and_attribute_fast_paths or simple_css_count_fast_path_covers_indexed_and_shadow_fallback or simple_css_text_read_fast_paths_cover_first_last_and_wait_fallback or simple_css_all_text_fast_paths_cover_indexed_and_shadow_fallback or simple_css_input_value_fast_paths_cover_first_last_and_wait_fallback or simple_css_attribute_fast_paths_cover_first_last_and_wait_fallback or simple_css_inner_html_fast_paths_cover_first_last_and_wait_fallback or simple_css_visibility_fast_paths_cover_first_last_and_missing or simple_css_enabled_fast_paths_cover_first_last_and_role_aware_disabled or download_save_as_fetches_when_cdp_path_is_not_local or page_download_waiters_ignore_other_pages or select_option_timeout_and_target_errors_match_playwright or tracing_unexpected_keyword_arguments_match_playwright or unknown_event_waiters_timeout_like_playwright or worker_unknown_event_expect_event_times_out_like_playwright or worker_close_listener_receives_worker_like_playwright or websocket_unknown_event_waiters_timeout_like_playwright or wait_for_event_console_does_not_replay_history or service_worker_automation_signals_are_suppressed or popup or dblclick_dispatch_and_editable_selection_match_playwright or native_check_uncheck_mouse_events_and_prevent_default_match_playwright or hover_dispatches_native_pointer_mouse_events_like_playwright or mouse_wheel_dispatches_single_trusted_event_like_playwright or mouse_click_count_dispatch_sequence_matches_playwright or mouse_dblclick_delay_reuses_click_count_sequence_like_playwright or drag_and_drop_dispatches_native_pointer_mouse_events_like_playwright"
  run_pytest -q tests/test_rustwright_sync_api.py \
    -k "async_wait_helpers_yield_event_loop or async_navigation_helpers_yield_event_loop or async_actions_yield_event_loop_while_waiting_for_targets or async_browser_type_session_setup_yields_event_loop or async_browser_context_creation_and_close_yield_event_loop or async_browser_context_new_page_and_close_yield_event_loop or async_browser_context_state_and_cdp_methods_yield_event_loop or async_skyvern_page_artifact_input_helpers_yield_event_loop or async_skyvern_artifact_cdp_frame_helpers_yield_event_loop or async_skyvern_route_response_helpers_yield_event_loop or async_skyvern_element_locator_handle_helpers_yield_event_loop or async_skyvern_lifecycle_tooling_helpers_yield_event_loop or async_assertion_helpers_yield_event_loop or async_clock_debugger_screencast_helpers_yield_event_loop or async_event_context_manager_entry_yields_event_loop or async_api_request_context_get_yields_event_loop"
  run_pytest -q \
    tests/test_skyvern_replacement_smoke.py \
    tests/test_skyvern_cloud_overlay_tests.py \
    tests/test_skyvern_alias_command.py \
    tests/test_skyvern_prompt_overlay_smoke.py \
    tests/test_project_status_tools.py
  run "$PYTHON_BIN" tools/run_parity_cases.py \
    --impl rustwright \
    --reference-path "$REFERENCE_PATH" \
    --case context_viewport_screen_device_option_validation_matches_playwright \
    --case context_no_viewport_and_viewport_none_disable_viewport_emulation \
    --case context_environment_and_emulate_media_validation_matches_playwright \
    --case context_base_url_resolves_page_and_frame_navigation \
    --case context_string_header_and_http_credentials_validation_matches_playwright \
    --case geolocation_option_validation_matches_playwright \
    --case context_record_har_artifact \
    --case context_record_har_option_validation_matches_playwright \
    --case persistent_context_skyvern_artifact_options_match_playwright \
    --case route_fetch_unsupported_protocol_errors_match_playwright \
    --case api_request_context_invalid_url_errors_match_playwright \
    --case api_request_context_dispose_reason_and_header_errors_match_playwright \
    --case api_request_context_negative_max_redirects_default_matches_playwright \
    --case api_request_context_negative_timeout_errors_match_playwright \
    --case expect_to_have_count_timeout_message_matches_playwright \
    --case wait_for_function \
    --case frame_wait_for_function_returns_js_handle \
    --case wait_for_request_and_response \
    --case locator_collection_text_helpers \
    --case locator_collection_input_value_helpers \
    --case locator_collection_attribute_helpers \
    --case locator_collection_inner_html_helpers \
    --case locator_collection_visibility_helpers \
    --case locator_collection_enabled_helpers \
    --case select_option_timeout_and_target_errors_match_playwright \
    --case tracing_unexpected_keyword_arguments_match_playwright \
    --case unknown_event_waiters_timeout_like_playwright \
    --case worker_unknown_event_expect_event_times_out_like_playwright \
    --case worker_close_listener_receives_worker_like_playwright \
    --case websocket_unknown_event_waiters_timeout_like_playwright \
    --case browser_type_launch_persistent_context_option_validation_matches_playwright \
    --case page_wait_for_event_console_does_not_replay_history \
    --case context_console_message_captures_immediate_popup_console \
    --case evaluate_window_open_returns_without_popup_waiter \
    --case context_expose_binding_child_frame_source_and_handle \
    --case cdp_runtime_target_and_navigation_events_match_playwright \
    --case dblclick_dispatch_and_editable_selection_match_playwright \
    --case native_check_uncheck_mouse_events_and_prevent_default_match_playwright \
    --case hover_dispatches_native_pointer_mouse_events_like_playwright \
    --case mouse_wheel_dispatches_single_trusted_event_like_playwright \
    --case mouse_click_count_dispatch_sequence_matches_playwright \
    --case mouse_dblclick_delay_reuses_click_count_sequence_like_playwright \
    --case drag_and_drop_dispatches_native_pointer_mouse_events_like_playwright \
    --json
  run "$PYTHON_BIN" tools/run_parity_cases.py \
    --impl playwright \
    --reference-path "$REFERENCE_PATH" \
    --case context_viewport_screen_device_option_validation_matches_playwright \
    --case context_string_header_and_http_credentials_validation_matches_playwright \
    --case geolocation_option_validation_matches_playwright \
    --case context_record_har_artifact \
    --case context_record_har_option_validation_matches_playwright \
    --case persistent_context_skyvern_artifact_options_match_playwright \
    --case route_fetch_unsupported_protocol_errors_match_playwright \
    --case api_request_context_invalid_url_errors_match_playwright \
    --case api_request_context_dispose_reason_and_header_errors_match_playwright \
    --case api_request_context_negative_max_redirects_default_matches_playwright \
    --case api_request_context_negative_timeout_errors_match_playwright \
    --case expect_to_have_count_timeout_message_matches_playwright \
    --case wait_for_function \
    --case frame_wait_for_function_returns_js_handle \
    --case wait_for_request_and_response \
    --case locator_collection_text_helpers \
    --case locator_collection_input_value_helpers \
    --case locator_collection_attribute_helpers \
    --case locator_collection_inner_html_helpers \
    --case locator_collection_visibility_helpers \
    --case locator_collection_enabled_helpers \
    --case select_option_timeout_and_target_errors_match_playwright \
    --case tracing_unexpected_keyword_arguments_match_playwright \
    --case unknown_event_waiters_timeout_like_playwright \
    --case worker_unknown_event_expect_event_times_out_like_playwright \
    --case worker_close_listener_receives_worker_like_playwright \
    --case websocket_unknown_event_waiters_timeout_like_playwright \
    --case browser_type_launch_persistent_context_option_validation_matches_playwright \
    --case page_wait_for_event_console_does_not_replay_history \
    --case context_console_message_captures_immediate_popup_console \
    --case evaluate_window_open_returns_without_popup_waiter \
    --case context_expose_binding_child_frame_source_and_handle \
    --case cdp_runtime_target_and_navigation_events_match_playwright \
    --case dblclick_dispatch_and_editable_selection_match_playwright \
    --case native_check_uncheck_mouse_events_and_prevent_default_match_playwright \
    --case hover_dispatches_native_pointer_mouse_events_like_playwright \
    --case mouse_wheel_dispatches_single_trusted_event_like_playwright \
    --case mouse_click_count_dispatch_sequence_matches_playwright \
    --case mouse_dblclick_delay_reuses_click_count_sequence_like_playwright \
    --case drag_and_drop_dispatches_native_pointer_mouse_events_like_playwright \
    --json
}

focused() {
  pycompile
  if [ "$#" -eq 0 ]; then
    echo "focused mode requires pytest arguments, for example: tests/test_rustwright_sync_api.py -k '<selector>'" >&2
    exit 2
  fi
  run_pytest -q "$@"
}

parity() {
  pycompile
  local has_reference_path=0
  local arg
  for arg in "$@"; do
    case "$arg" in
      --reference-path|--reference-path=*)
        has_reference_path=1
        ;;
    esac
  done
  if [ "$has_reference_path" -eq 1 ]; then
    run "$PYTHON_BIN" tools/run_parity_cases.py "$@"
  else
    run "$PYTHON_BIN" tools/run_parity_cases.py --reference-path "$REFERENCE_PATH" "$@"
  fi
}

full() {
  pycompile
  run_pytest -q
  run "$PYTHON_BIN" tools/run_parity_cases.py \
    --impl rustwright \
    --reference-path "$REFERENCE_PATH" \
    --json
  run "$PYTHON_BIN" tools/run_parity_cases.py \
    --impl playwright \
    --reference-path "$REFERENCE_PATH" \
    --json
}

phase1() {
  sampled
  run "$PYTHON_BIN" tools/check_phase1_gate.py --current-docker-run --require-docker --pretty
}

bench() {
  local impl_args=(--impl all)
  local arg
  local selected_impl="all"
  local next_is_impl=0
  for arg in "$@"; do
    if [ "$next_is_impl" -eq 1 ]; then
      selected_impl="$arg"
      next_is_impl=0
      continue
    fi
    case "$arg" in
      --impl)
        impl_args=()
        next_is_impl=1
        ;;
      --impl=*)
        impl_args=()
        selected_impl="${arg#--impl=}"
        ;;
    esac
  done
  if [ "${RUSTWRIGHT_BENCH_REBUILD:-0}" != "0" ] && { [ "$selected_impl" = "rustwright" ] || [ "$selected_impl" = "all" ]; }; then
    local wheel_dir
    wheel_dir="$(mktemp -d)"
    run "$PYTHON_BIN" -m maturin build --release --out "$wheel_dir"
    run "$PYTHON_BIN" -m pip install --force-reinstall --no-deps "$wheel_dir"/rustwright-*.whl
  fi
  check_browser_executables
  run "$PYTHON_BIN" benchmarks/run_benchmarks.py \
    "${impl_args[@]}" \
    --reference-path "$REFERENCE_PATH" \
    --iterations "${BENCHMARK_ITERATIONS:-20}" \
    "$@"
}

antibot() {
  run "$PYTHON_BIN" tools/run_antibot_benchmarks.py "$@"
}

mind2web() {
  run "$PYTHON_BIN" tools/run_mind2web_benchmark.py \
    --reference-path "$REFERENCE_PATH" \
    "$@"
}

webvoyager() {
  run "$PYTHON_BIN" tools/run_webvoyager_benchmark.py \
    --reference-path "$REFERENCE_PATH" \
    "$@"
}

antibot_smoke() {
  run "$PYTHON_BIN" tools/run_antibot_benchmarks.py \
    --suite smoke \
    --impl all \
    --iterations "${ANTIBOT_SMOKE_ITERATIONS:-3}" \
    --json \
    "$@"
  run "$PYTHON_BIN" tools/run_antibot_benchmarks.py \
    --suite network \
    --impl all \
    --iterations "${ANTIBOT_NETWORK_ITERATIONS:-1}" \
    --json
  run "$PYTHON_BIN" tools/run_antibot_benchmarks.py \
    --suite matrix \
    --impl all \
    --iterations "${ANTIBOT_MATRIX_ITERATIONS:-1}" \
    --json
}

case "$MODE" in
  pycompile)
    enforce_container_memory_limit
    pycompile "$@"
    ;;
  sampled)
    enforce_container_memory_limit
    sampled "$@"
    ;;
  phase1)
    enforce_container_memory_limit
    phase1 "$@"
    ;;
  focused)
    enforce_container_memory_limit
    focused "$@"
    ;;
  parity)
    enforce_container_memory_limit
    parity "$@"
    ;;
  full)
    enforce_container_memory_limit
    full "$@"
    ;;
  bench)
    enforce_container_memory_limit
    bench "$@"
    ;;
  mind2web)
    enforce_container_memory_limit
    mind2web "$@"
    ;;
  webvoyager)
    enforce_container_memory_limit
    webvoyager "$@"
    ;;
  antibot)
    enforce_container_memory_limit
    antibot "$@"
    ;;
  antibot-smoke)
    enforce_container_memory_limit
    antibot_smoke "$@"
    ;;
  *)
    cat >&2 <<'USAGE'
Usage inside the container:
  docker run --rm --memory=8g --memory-swap=8g rustwright-verify [pycompile|focused|parity|sampled|phase1|full|bench|mind2web|webvoyager|antibot|antibot-smoke]
  Uncapped Docker runs, or runs with a memory cap above 8GB, exit before verification starts.

Preferred host usage:
  tools/docker_test.sh [pycompile|focused|parity|sampled|phase1|full|bench|bench-full|mind2web|mind2web-full|webvoyager|webvoyager-full|antibot|antibot-smoke]

Modes:
  pycompile       Compile Python sources used by the verification loops.
  focused         Compile Python sources, then run the supplied pytest selector.
  parity          Compile Python sources, then run supplied shared parity cases.
  sampled         Focused tests plus a stratified option/event/anti-bot/async parity sample.
  phase1          Run sampled, then require tools/check_phase1_gate.py to pass in the current capped Docker container.
  full            Full pytest plus full Rustwright and Playwright parity.
  bench           Comparable benchmark run; set BENCHMARK_ITERATIONS to tune. Set RUSTWRIGHT_BENCH_REBUILD=1 to rebuild Rustwright release wheel for Rustwright/all runs.
  bench-full      Host wrapper mode only: one capped Docker container per benchmark implementation.
  mind2web        Imported Mind2Web quality benchmark run; set MIND2WEB_ITERATIONS to tune.
  mind2web-full   Host wrapper mode only: one capped Docker container per Mind2Web implementation.
  webvoyager      Imported WebVoyager reliability benchmark run; set WEBVOYAGER_ITERATIONS to tune.
  webvoyager-full Host wrapper mode only: one capped Docker container per WebVoyager implementation.
  antibot         Run tools/run_antibot_benchmarks.py with supplied arguments.
  antibot-smoke   Tier 0 smoke, local Tier 2 network, and fresh/warm matrix anti-bot checks.
USAGE
    exit 2
    ;;
esac
