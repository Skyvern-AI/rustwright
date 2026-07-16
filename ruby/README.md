# Rustwright Ruby binding

This Phase 0 binding exposes the complete Rustwright alpha API through Ruby's
stdlib `Fiddle`; it has no native gem dependency and supports Ruby 2.6 or newer.
JSON is handled by the stdlib `json` package.

## Run it

From the repository root, run the browser smoke test:

```sh
ruby ruby/smoke.rb --lib target/release/librustwright_capi.dylib
```

Run the five-case benchmark subset and write its result document:

```sh
ruby ruby/runner.rb --manifest bindings/cases/smoke.json --lib target/release/librustwright_capi.dylib --out /tmp/ruby-results.json
```

On Linux, pass the corresponding `.so`. When run from the repository root,
direct API use defaults to `target/release/librustwright_capi.dylib` (or `.so`)
and can select another path with `RUSTWRIGHT_CAPI_LIB`; the smoke command uses
that default when `--lib` is omitted. The runner requires `--lib`, and an
explicit path always loads that exact library. The runner also supports `--cases
id1,id2`, preserves manifest order, exits 0 only when all selected cases pass,
and exits 1 for case failures (invocation/manifest errors use exit 2).

Run the dependency-free contract tests with:

```sh
ruby ruby/test/contract_test.rb
```

## API

```ruby
require_relative 'ruby/lib/rustwright'

browser = Rustwright.chromium.launch(headless: true)
begin
  page = browser.new_page
  begin
    page.goto('data:text/html,<title>Hello</title>')
    puts page.title
    bytes = page.screenshot(full_page: true, type: :png)
  ensure
    page.close
  end
ensure
  browser.close
end
```

`Rustwright.chromium.executable_path` discovers Chromium. Browser methods are
`new_page`, `close`, and `ws_endpoint`. Page methods are `goto`, `click`,
`fill`, `title`, `text_content`, `evaluate`, `screenshot`, and `close`.
Timeouts are milliseconds; omitting one sends IEEE-754 NaN to mean “no explicit
timeout.” Launch accepts snake_case, camelCase, string, or symbol option keys
and normalizes them to the core wire shape. Screenshot accepts Ruby snake_case
or Node camelCase keys and sends the Node screenshot shape.

Native handles are serialized per browser, explicitly closed, and then freed
exactly once. Returned strings and screenshot buffers are copied into Ruby
memory before their matching Rustwright free function is called. Call `close`
in `ensure` blocks as shown; `close` is idempotent.

## Evaluate values

`evaluate` JSON-encodes an explicitly supplied argument (including `nil`) and
recursively decodes the core CDP wire. Wrapped arrays/objects and repeated or
cyclic references become Ruby arrays/hashes. Non-finite numbers and BigInts
become `Float`/`Integer`, dates become `Time`, regexes become `Regexp`, URLs
become `URI`, and JavaScript errors become `Rustwright::JavaScriptError`.
JavaScript `undefined`, symbols, and functions have no lossless Ruby value and
fall back to `nil`.
