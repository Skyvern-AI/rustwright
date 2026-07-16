# Rustwright Go alpha binding

This package loads the exact Rustwright C ABI shared library at runtime with
[`purego`](https://github.com/ebitengine/purego), so it does not require cgo or
loader-path configuration. It binds all 19 `rw_*` functions from
`rustwright.h` and exposes the Phase 0 Chromium, browser, and page API.

## Commands

From the repository root on macOS:

```sh
go -C go build -o /tmp/rustwright-go-smoke ./cmd/smoke
/tmp/rustwright-go-smoke --lib target/release/librustwright_capi.dylib
go -C go build -o /tmp/rustwright-go-runner ./cmd/runner
/tmp/rustwright-go-runner --manifest bindings/cases/smoke.json --lib target/release/librustwright_capi.dylib --out /tmp/go-results.json
go -C go test ./...
```

On Linux, use the `.so` artifact. `smoke` defaults to
`target/release/librustwright_capi.dylib` (or `.so`) when run from the
repository root and also honors `RUSTWRIGHT_LIB`; an explicit `--lib` overrides
the default. The runner requires `--manifest`, `--lib`, and `--out`; optional
`--cases id1,id2` preserves manifest order.

## API sketch

```go
chromium, err := rustwright.Open("target/release/librustwright_capi.dylib")
browser, err := chromium.Launch(rustwright.LaunchOptions{})
defer browser.Close()

page, err := browser.NewPage()
defer page.Close(nil)
_, err = page.Goto("data:text/html,hello", nil)
title, err := page.Title(nil)
```

`LaunchOptions` fields are converted from Go camel case to the core's
snake_case JSON. `ScreenshotOptions` uses the Node wire names (`fullPage`,
`omitBackground`, and so on). A nil timeout emits IEEE-754 NaN at the C
boundary, meaning unspecified.

Every failed status is followed immediately, on the same locked OS thread, by
copying `rw_last_error()`. Returned strings and screenshot buffers are copied
before their Rust allocators are invoked, and `Close` closes then frees each
opaque handle once.

Evaluation recursively unwraps the core array/object/reference wire tags.
Dates become `time.Time`, URLs become `*url.URL`, regexps become
`RegExpValue`, errors become `JavaScriptError`, and non-finite numbers become
their Go `math` values. JavaScript undefined, symbols, and functions have no
JSON-compatible Go value and intentionally fall back to `nil`. Passing nil as
the evaluate argument means no argument; use `json.RawMessage("null")` to pass
an explicit JavaScript null.
