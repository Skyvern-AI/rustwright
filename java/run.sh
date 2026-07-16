#!/usr/bin/env bash
set -euo pipefail

PACKAGE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CLASSES="$PACKAGE_DIR/build/classes"
mkdir -p "$CLASSES"

find "$PACKAGE_DIR/src/main/java" -name '*.java' -print0 \
  | xargs -0 javac --release 23 -encoding UTF-8 -d "$CLASSES"

if [[ $# -lt 1 ]]; then
  echo "usage: ./run.sh <smoke|runner> [arguments...]" >&2
  exit 2
fi

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
