#!/usr/bin/env bash
set -euo pipefail

WORKFLOW="${RUSTWRIGHT_TESTBOX_WORKFLOW:-benchmark-testbox.yml}"
WORKFLOW="${WORKFLOW#.github/workflows/}"
IDLE_TIMEOUT="${RUSTWRIGHT_TESTBOX_IDLE_TIMEOUT:-60}"
WAIT_TIMEOUT="${RUSTWRIGHT_TESTBOX_WAIT_TIMEOUT:-30m}"
REF="${RUSTWRIGHT_TESTBOX_REF:-main}"
JOB="${RUSTWRIGHT_TESTBOX_JOB:-}"
RUN_COMMAND="${RUSTWRIGHT_TESTBOX_RUN_COMMAND:-}"
SSH_PRIVATE_KEY="${RUSTWRIGHT_TESTBOX_SSH_PRIVATE_KEY:-}"
DOWNLOAD_RESULTS="${RUSTWRIGHT_TESTBOX_DOWNLOAD_RESULTS:-0}"
REPO=""

usage() {
  cat >&2 <<'ERROR'
Usage:
  tools/run_benchmark_testbox.sh
  tools/run_benchmark_testbox.sh -- "<command to run inside the warmed testbox>"

Environment:
  RUSTWRIGHT_TESTBOX_WORKFLOW       Workflow file, default benchmark-testbox.yml.
  RUSTWRIGHT_TESTBOX_REF            Git ref to warm, default main.
  RUSTWRIGHT_TESTBOX_JOB            Optional workflow job name.
  RUSTWRIGHT_TESTBOX_IDLE_TIMEOUT   Warmed testbox idle timeout in minutes.
  RUSTWRIGHT_TESTBOX_WAIT_TIMEOUT   Readiness wait timeout, default 30m.
  RUSTWRIGHT_TESTBOX_RUN_COMMAND    Command to run after warmup, alternative to --.
  RUSTWRIGHT_TESTBOX_DOWNLOAD_RESULTS=1 downloads .benchmark-data/results and reports.
ERROR
}

if [ "${1:-}" = "--" ]; then
  shift
  if [ "$#" -eq 0 ]; then
    usage
    exit 2
  fi
  RUN_COMMAND="$*"
elif [ "$#" -gt 0 ]; then
  usage
  exit 2
fi

print_testbox_diagnostics() {
  cat >&2 <<ERROR

Blacksmith/GitHub diagnostics:
  origin: $(git remote get-url origin 2>/dev/null || printf '<unavailable>')
  repo: ${REPO:-<unknown>}
  ref: ${REF}
  workflow: .github/workflows/${WORKFLOW}
  job: ${JOB:-<unset>}
  BLACKSMITH_ORG: ${BLACKSMITH_ORG:-<unset>}
  warmup_args: blacksmith ${args[*]:-<not-built>}
ERROR

  if command -v blacksmith >/dev/null 2>&1; then
    {
      printf '  blacksmith_version: '
      blacksmith --version
    } >&2 || true
  fi

  if command -v gh >/dev/null 2>&1 && [ -n "$REPO" ]; then
    {
      echo "  gh_workflows:"
      gh workflow list --repo "$REPO" --all | sed 's/^/    /'
      echo "  gh_workflow_contents:"
      if gh api "repos/${REPO}/contents/.github/workflows/${WORKFLOW}?ref=${REF}" --jq '.path + " sha=" + .sha' >/tmp/rustwright-testbox-workflow-content.$$ 2>/tmp/rustwright-testbox-workflow-error.$$; then
        sed 's/^/    /' /tmp/rustwright-testbox-workflow-content.$$
      else
        sed 's/^/    /' /tmp/rustwright-testbox-workflow-error.$$
      fi
      rm -f /tmp/rustwright-testbox-workflow-content.$$ /tmp/rustwright-testbox-workflow-error.$$
      echo "  gh_blacksmith_app:"
      gh api "orgs/${REPO%%/*}/installations" \
        --jq '.installations[] | select(.app_slug == "blacksmith-sh") | "app_slug=\(.app_slug) id=\(.id) repository_selection=\(.repository_selection) contents=\(.permissions.contents // "unset") workflows=\(.permissions.workflows // "unset") updated_at=\(.updated_at)"' \
        | sed 's/^/    /'
    } >&2 || true
  fi
}

print_warmup_failure_classification() {
  local warmup_output="$1"
  if printf '%s\n' "$warmup_output" | grep -q "Could not fetch .github/workflows/"; then
    cat >&2 <<ERROR

Failure classification: blacksmith_repo_visibility_blocked
Blacksmith could not read the workflow from GitHub even though this helper
already verified that the local checkout contains it and, when gh is available,
that GitHub's contents API can read it at the requested ref. This usually means
the Blacksmith GitHub App or organization authorization cannot see this repo.

Suggested checks:
  1. Confirm the Blacksmith GitHub App is installed for ${REPO:-<repo>} with repository access.
  2. Run: blacksmith auth status
  3. Run: blacksmith testbox init
  4. If GitHub sees the workflow but Blacksmith still returns 404, escalate as a Blacksmith repo visibility issue.
ERROR
  fi
}

extract_testbox_id() {
  printf '%s\n' "$1" | grep -Eo 'tbx_[[:alnum:]_]+' | head -n 1 || true
}

blacksmith_testbox_run() {
  local testbox_id="$1"
  local command="$2"
  local run_args=(testbox run --id "$testbox_id")
  if [ -n "$SSH_PRIVATE_KEY" ]; then
    run_args+=(--ssh-private-key "$SSH_PRIVATE_KEY")
  fi
  run_args+=("$command")
  blacksmith "${run_args[@]}"
}

blacksmith_testbox_download() {
  local testbox_id="$1"
  local remote_path="$2"
  local local_path="$3"
  local download_args=(testbox download --id "$testbox_id")
  if [ -n "$SSH_PRIVATE_KEY" ]; then
    download_args+=(--ssh-private-key "$SSH_PRIVATE_KEY")
  fi
  download_args+=("$remote_path" "$local_path")
  blacksmith "${download_args[@]}"
}

if ! command -v blacksmith >/dev/null 2>&1; then
  cat >&2 <<'ERROR'
blacksmith CLI is not installed. Install it with:
  curl -fsSL https://get.blacksmith.sh | sh
ERROR
  exit 127
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  cat >&2 <<'ERROR'
Cannot run Blacksmith Testbox from this checkout because git remote "origin"
is not configured. Testbox warmup dispatches a GitHub Actions workflow, so the
repo must be backed by a GitHub remote containing the workflow file.
ERROR
  exit 2
fi

if ! git remote get-url origin | grep -Eq 'github.com[:/]'; then
  cat >&2 <<ERROR
Cannot run Blacksmith Testbox from this checkout because origin is not a GitHub
remote: $(git remote get-url origin)
ERROR
  exit 2
fi

if [ ! -f ".github/workflows/$WORKFLOW" ]; then
  cat >&2 <<ERROR
Missing Testbox workflow: .github/workflows/${WORKFLOW}
ERROR
  exit 2
fi

if command -v gh >/dev/null 2>&1; then
  REPO="$(git remote get-url origin | sed -E 's#^git@github.com:##; s#^https://github.com/##; s#\.git$##')"
  if ! gh api "repos/${REPO}/contents/.github/workflows/${WORKFLOW}?ref=${REF}" >/dev/null 2>&1; then
    cat >&2 <<ERROR
GitHub cannot read .github/workflows/${WORKFLOW} at ref ${REF} for ${REPO}.
Push the workflow to that ref or set RUSTWRIGHT_TESTBOX_REF to a ref that
contains the workflow before warming a Blacksmith Testbox.
ERROR
    print_testbox_diagnostics
    exit 2
  fi
fi

args=(testbox warmup "$WORKFLOW" --ref "$REF" --idle-timeout "$IDLE_TIMEOUT")
if [ -n "$JOB" ]; then
  args+=(--job "$JOB")
fi

if ! output="$(blacksmith "${args[@]}" 2>&1)"; then
  printf '%s\n' "$output" >&2
  print_warmup_failure_classification "$output"
  cat >&2 <<ERROR

Blacksmith Testbox warmup failed even though the local checkout and, when
available, GitHub's contents API can see the workflow. If the error says
"no workflows with jobs found" or "Could not fetch .github/workflows/...",
check Blacksmith repo authorization/org selection and run:
  blacksmith testbox init
You can also set RUSTWRIGHT_TESTBOX_JOB=benchmark if workflow job discovery is
ambiguous.
ERROR
  print_testbox_diagnostics
  exit 1
fi

printf '%s\n' "$output"

if [ -n "$RUN_COMMAND" ]; then
  testbox_id="$(extract_testbox_id "$output")"
  if [ -z "$testbox_id" ]; then
    cat >&2 <<ERROR
Blacksmith warmup succeeded but no testbox id matching tbx_* was found in the
output, so the benchmark command was not run.
ERROR
    print_testbox_diagnostics
    exit 1
  fi

  echo "Waiting for Testbox ${testbox_id} to become ready..."
  blacksmith testbox status --id "$testbox_id" --wait --wait-timeout "$WAIT_TIMEOUT"

  echo "Running benchmark command in Testbox ${testbox_id}..."
  blacksmith_testbox_run "$testbox_id" "$RUN_COMMAND"

  case "$DOWNLOAD_RESULTS" in
    1|true|TRUE|yes|YES)
      mkdir -p .benchmark-data
      blacksmith_testbox_download "$testbox_id" .benchmark-data/results .benchmark-data/results
      blacksmith_testbox_download "$testbox_id" .benchmark-data/reports .benchmark-data/reports
      ;;
  esac
fi
