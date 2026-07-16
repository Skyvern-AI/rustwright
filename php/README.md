# Rustwright PHP binding

This package is a plain-PHP FFI wrapper for the Rustwright C ABI. It requires
PHP 8.1 or newer with `ext-ffi`; Composer is optional because the entrypoints
include a small PSR-4 fallback autoloader.

## Smoke

From the repository root, run exactly:

```sh
php -d ffi.enable=1 php/smoke.php
```

The smoke launches headless Chromium, drives an inline form, evaluates
JavaScript, captures a PNG, and prints one JSON line.

## Benchmark runner

From the repository root, run exactly:

```sh
php -d ffi.enable=1 php/runner.php --manifest bindings/cases/smoke.json --lib target/release/librustwright_capi.dylib --out /tmp/php-layout.json
```

The runner also accepts `--cases id1,id2`. Selection preserves manifest order.
It writes the contract results object to `--out`, prints the same JSON, exits 0
only when every selected case passes, exits 1 for case failures, and exits 2
for invocation or manifest errors. `--manifest`, `--lib`, and `--out` are all
required.

When no library path is passed to `Chromium::launch()` or
`Chromium::executablePath()`, the binding loads
`target/release/librustwright_capi.dylib` on macOS or
`target/release/librustwright_capi.so` on Linux, relative to the current
working directory. Run from the repository root, or set
`RUSTWRIGHT_CAPI_LIB`/pass the explicit library path.

## API

The `Rustwright\Chromium`, `Rustwright\Browser`, and `Rustwright\Page` classes
provide the complete alpha surface:

- `Chromium::launch($options, $libraryPath)` and
  `Chromium::executablePath($libraryPath)`
- `Browser->newPage()`, `Browser->close()`, and `Browser->wsEndpoint()`
- `Page->goto()`, `click()`, `fill()`, `title()`, `textContent()`, `evaluate()`,
  `screenshot()`, and `close()`

Launch options accept camelCase Node names or the core snake_case aliases and
are normalized to the C JSON wire shape. Screenshot options accept the Node
names, with `full_page` and `omit_background` aliases. Timeouts are
milliseconds; omitted/null timeouts are sent as IEEE-754 `NAN`.

`evaluate()` recursively unwraps Rustwright CDP array/object wrappers. Dates
become `DateTimeImmutable`, URLs become strings, regular expressions become
objects with `pattern` and `flags`, and non-finite numbers become PHP floats.
PHP has no direct equivalents for JavaScript `undefined`, symbols, or
functions, so those tagged values intentionally decode to `null`. Manifest-v1
captures are JSON-compatible and do not require cyclic reference identity.

Rust-owned strings and screenshot buffers are copied before their matching
free call. Page and browser `close()` methods perform the native close and then
free each opaque handle exactly once; destructors provide best-effort cleanup.
