#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 || ( $# -eq 2 && "$2" != "--require-all" ) ]]; then
  echo "usage: $0 <native-artifact-directory> [--require-all]" >&2
  exit 2
fi

SOURCE_ROOT="$(CDPATH= cd -- "$1" && pwd)"
PACKAGE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
DESTINATION_ROOT="$PACKAGE_DIR/src/main/resources/native"
REQUIRE_ALL=false
if [[ ${2:-} == "--require-all" ]]; then
  REQUIRE_ALL=true
fi

staged=0
missing=0
while IFS='|' read -r platform source_name destination_name; do
  destination="$DESTINATION_ROOT/$platform/$destination_name"
  rm -f "$destination"

  source_file=""
  for candidate in \
    "$SOURCE_ROOT/$platform/$source_name" \
    "$SOURCE_ROOT/maven-native-$platform/$source_name"; do
    if [[ -f "$candidate" ]]; then
      source_file="$candidate"
      break
    fi
  done

  if [[ -z "$source_file" ]]; then
    if [[ "$REQUIRE_ALL" == true ]]; then
      echo "missing native for $platform ($source_name) under $SOURCE_ROOT" >&2
      missing=$((missing + 1))
    fi
    continue
  fi

  mkdir -p "$(dirname -- "$destination")"
  install -m 0644 "$source_file" "$destination"
  echo "staged $platform/$destination_name"
  staged=$((staged + 1))
done <<'PLATFORMS'
osx-aarch64|librustwright_capi.dylib|librustwright_capi.dylib
osx-x86_64|librustwright_capi.dylib|librustwright_capi.dylib
linux-x86_64|librustwright_capi.so|librustwright_capi.so
linux-aarch64|librustwright_capi.so|librustwright_capi.so
windows-x86_64|rustwright_capi.dll|librustwright_capi.dll
PLATFORMS

if [[ "$missing" -ne 0 ]]; then
  exit 1
fi
if [[ "$staged" -eq 0 ]]; then
  echo "no recognized native libraries found under $SOURCE_ROOT" >&2
  exit 1
fi

echo "staged $staged native library/libraries into $DESTINATION_ROOT"
