#!/usr/bin/env bash
set -euo pipefail

PACKAGE_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
CLASSES="$PACKAGE_DIR/build/classes"

if [[ $# -lt 1 ]]; then
  echo "usage: ./run.sh build" >&2
  echo "       ./run.sh run <smoke|runner> [arguments...]" >&2
  exit 2
fi

build() {
  mkdir -p "$CLASSES"
  find "$PACKAGE_DIR/src/main/java" -name '*.java' -print0 \
    | xargs -0 javac --release 23 -encoding UTF-8 -d "$CLASSES"
}

if [[ $1 == build ]]; then
  [[ $# -eq 1 ]] || {
    echo "build takes no arguments" >&2
    exit 2
  }
  build
  exit 0
fi

if [[ $1 == run ]]; then
  shift
  [[ -d $CLASSES ]] || {
    echo "runner classes are missing; run ./run.sh build first" >&2
    exit 2
  }
else
  # Preserve the original one-command interface for development callers. The
  # release benchmark uses the explicit build/run phases above.
  build
fi

[[ $# -ge 1 ]] || {
  echo "usage: ./run.sh run <smoke|runner> [arguments...]" >&2
  exit 2
}

case "$1" in
  smoke) MAIN=com.skyvern.rustwright.Smoke ;;
  runner) MAIN=com.skyvern.rustwright.Runner ;;
  *)
    echo "unknown entrypoint: $1 (expected smoke or runner)" >&2
    exit 2
    ;;
esac
shift

exec java --enable-native-access=ALL-UNNAMED \
  -Drustwright.packageDir="$PACKAGE_DIR" \
  -cp "$CLASSES" "$MAIN" "$@"
