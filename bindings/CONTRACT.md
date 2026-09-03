# Rustwright binding contract (Phase 0, manifest v1)

This document and `capi/include/rustwright.h` define the shared boundary for the
Go, Java, C#/.NET, Ruby, PHP, and native Rust alpha bindings. Language wrappers
may be idiomatic, but they must preserve the behavior, ownership, and runner
semantics below. The hand-written C header is the source of truth if generated
FFI declarations disagree with this document.

## Alpha binding surface

Every binding exposes these operations using the language's normal naming
conventions and native values:

- `chromium.launch(options)` and `chromium.executablePath()`
- `browser.newPage()`, `browser.close()`, and `browser.wsEndpoint()`
- `page.goto()`, `page.click()`, `page.fill()`, `page.title()`,
  `page.textContent()`, `page.evaluate()`, `page.screenshot()`, and
  `page.close()`

`goto` returns decoded response JSON or null. `textContent` returns a nullable
string. `evaluate` accepts an optional language value that the binding encodes
as one JSON value, and returns the core JSON wire value decoded to a native
value. `screenshot` returns encoded bytes. Timeout values are milliseconds.

The language-facing option names and behavior match the Node alpha surface.
The core launch parser accepts canonical snake_case fields and the current
camelCase aliases `executablePath`, `ignoreAllDefaultArgs`, `ignoreDefaultArgs`,
`userDataDir`, and `chromiumSandbox`. C callers should emit canonical snake_case
keys. The C launch JSON uses the normalized core wire shape: `headless`,
`executable_path`, `channel`, `args`, `ignore_all_default_args`,
`ignore_default_args`, `timeout`, `user_data_dir`, `env`, `chromium_sandbox`,
and `proxy`. Screenshot JSON uses the Node names `path`, `fullPage`, `clip`,
`timeout`, `type`, `quality`, and `omitBackground`.

## C ABI reference

Opaque handles have no public layout:

```c
typedef struct RwBrowser RwBrowser;
typedef struct RwPage RwPage;
typedef struct RwWireGraph RwWireGraph;
typedef size_t RwWireNodeId;
typedef int32_t RwWireNodeKind;
#define RW_WIRE_NODE_NULL 0
#define RW_WIRE_NODE_BOOL 1
#define RW_WIRE_NODE_SIGNED 2
#define RW_WIRE_NODE_UNSIGNED 3
#define RW_WIRE_NODE_FLOAT 4
#define RW_WIRE_NODE_STRING 5
#define RW_WIRE_NODE_ARRAY 6
#define RW_WIRE_NODE_OBJECT 7
#define RW_WIRE_NODE_LEAF 8
typedef int32_t RwWireLeafKind;
#define RW_WIRE_LEAF_UNSERIALIZABLE 0
#define RW_WIRE_LEAF_BIGINT 1
#define RW_WIRE_LEAF_DATE 2
#define RW_WIRE_LEAF_REGEXP 3
#define RW_WIRE_LEAF_URL 4
#define RW_WIRE_LEAF_ERROR 5
#define RW_WIRE_LEAF_UNDEFINED 6
#define RW_WIRE_LEAF_SYMBOL 7
#define RW_WIRE_LEAF_FUNCTION 8
```

The complete exported ABI is:

```c
/* lifecycle and errors */
const char *rw_last_error(void);
void rw_string_free(char *s);
void rw_bytes_free(uint8_t *buf, size_t len);

/* evaluate wire */
int32_t rw_decode_wire(const char *wire_json, char **out_json);
int32_t rw_wire_graph_parse(const char *wire_json,
                            RwWireGraph **out_graph);
void rw_wire_graph_free(RwWireGraph *graph);
int32_t rw_wire_graph_node_count(const RwWireGraph *graph,
                                 size_t *out_count);
int32_t rw_wire_graph_root(const RwWireGraph *graph,
                           RwWireNodeId *out_root);
int32_t rw_wire_graph_node_kind(const RwWireGraph *graph,
                                RwWireNodeId node,
                                RwWireNodeKind *out_kind);
int32_t rw_wire_graph_get_bool(const RwWireGraph *graph,
                               RwWireNodeId node,
                               int32_t *out_value);
int32_t rw_wire_graph_get_signed(const RwWireGraph *graph,
                                 RwWireNodeId node,
                                 int64_t *out_value);
int32_t rw_wire_graph_get_unsigned(const RwWireGraph *graph,
                                   RwWireNodeId node,
                                   uint64_t *out_value);
int32_t rw_wire_graph_get_float(const RwWireGraph *graph,
                                RwWireNodeId node,
                                double *out_value);
int32_t rw_wire_graph_get_string(const RwWireGraph *graph,
                                 RwWireNodeId node,
                                 const uint8_t **out_data,
                                 size_t *out_len);
int32_t rw_wire_graph_array_length(const RwWireGraph *graph,
                                   RwWireNodeId node,
                                   size_t *out_len);
int32_t rw_wire_graph_array_child(const RwWireGraph *graph,
                                  RwWireNodeId node,
                                  size_t index,
                                  RwWireNodeId *out_child);
int32_t rw_wire_graph_object_length(const RwWireGraph *graph,
                                    RwWireNodeId node,
                                    size_t *out_len);
int32_t rw_wire_graph_object_key(const RwWireGraph *graph,
                                 RwWireNodeId node,
                                 size_t index,
                                 const uint8_t **out_data,
                                 size_t *out_len);
int32_t rw_wire_graph_object_child(const RwWireGraph *graph,
                                   RwWireNodeId node,
                                   size_t index,
                                   RwWireNodeId *out_child);
int32_t rw_wire_graph_leaf_kind(const RwWireGraph *graph,
                                RwWireNodeId node,
                                RwWireLeafKind *out_kind);
int32_t rw_wire_graph_leaf_field_count(const RwWireGraph *graph,
                                       RwWireNodeId node,
                                       size_t *out_count);
int32_t rw_wire_graph_leaf_field(const RwWireGraph *graph,
                                 RwWireNodeId node,
                                 size_t index,
                                 const uint8_t **out_data,
                                 size_t *out_len);


/* Chromium */
int32_t rw_chromium_executable_path(char **out_path);
int32_t rw_chromium_launch(const char *options_json,
                           RwBrowser **out_browser);

/* browser */
int32_t rw_browser_new_page(RwBrowser *b, RwPage **out_page);
int32_t rw_browser_close(RwBrowser *b);
char *rw_browser_ws_endpoint(RwBrowser *b);
void rw_browser_free(RwBrowser *b);

/* page timeout defaults */
int32_t rw_page_set_default_timeout(RwPage *p, double timeout_ms_or_nan);
int32_t rw_page_set_default_navigation_timeout(RwPage *p,
                                               double timeout_ms_or_nan);
int32_t rw_page_set_context_default_timeout(RwPage *p,
                                            double timeout_ms_or_nan);
int32_t rw_page_set_context_default_navigation_timeout(
    RwPage *p,
    double timeout_ms_or_nan);

/* page */
char *rw_page_target_id(RwPage *p);
int32_t rw_page_goto(RwPage *p,
                     const char *url,
                     const char *wait_until,
                     double timeout_ms_or_nan,
                     const char *referer,
                     char **out_response_json);
int32_t rw_page_click(RwPage *p,
                      const char *selector,
                      double timeout_ms_or_nan);
int32_t rw_page_fill(RwPage *p,
                     const char *selector,
                     const char *value,
                     double timeout_ms_or_nan);
int32_t rw_page_title(RwPage *p,
                      double timeout_ms_or_nan,
                      char **out_title);
int32_t rw_page_text_content(RwPage *p,
                             const char *selector,
                             double timeout_ms_or_nan,
                             char **out_text);
int32_t rw_page_evaluate(RwPage *p,
                         const char *expression,
                         const char *arg_json,
                         double timeout_ms_or_nan,
                         char **out_json);
int32_t rw_page_screenshot(RwPage *p,
                           const char *options_json,
                           uint8_t **out_buf,
                           size_t *out_len);
int32_t rw_page_close(RwPage *p,
                      double timeout_ms_or_nan,
                      int run_before_unload);
void rw_page_free(RwPage *p);
```

### Status and error rules

All fallible `int32_t` functions return zero on success and nonzero on failure.
Bindings must treat every nonzero value as an error; numeric nonzero values are
not a stable error taxonomy. On failure, call `rw_last_error()` immediately on
the same OS thread and copy the borrowed UTF-8 message into language-owned
memory. It is NULL when no error is recorded and is valid only until the next
Rustwright ABI call on that thread. Never free it.

`rw_last_error()` is diagnostic prose, not a machine-readable taxonomy; bindings must not parse it to decide whether to retry.

`rw_browser_ws_endpoint` and `rw_page_target_id` return NULL on failure and set
the same last-error slot. All ABI entry points catch Rust panics; no unwind is
allowed to cross the C boundary.

### String, byte, and handle ownership

All input strings are borrowed, NUL-terminated UTF-8 and need remain live only
for the synchronous call. Parameters documented as nullable may be NULL; other
string and out-parameter pointers must not be NULL.

Every non-NULL `char *` returned through an out-parameter or direct return is
Rust-owned allocation transferred to the caller. Call `rw_string_free()`
exactly once. For `rw_chromium_executable_path`, NULL with status zero means no
browser was discovered. For `rw_page_text_content`, NULL with status zero means
JavaScript null.

`rw_page_screenshot` transfers one pointer/length pair. Pass the exact pair to
`rw_bytes_free()` exactly once. Empty bytes are represented as NULL plus zero.
Do not use a language allocator for Rust strings or bytes.

Successful launch and new-page calls transfer opaque boxed handles. Close each
page/browser for browser lifecycle semantics, then free each handle exactly
once. `close` does not free the handle, and `free` is not a substitute for
`close`. The free functions accept NULL.

### Null, timeout, threading, and JSON rules

Every C timeout uses one sentinel: IEEE-754 NaN means `None`/unspecified; every
other double is forwarded as `Some(milliseconds)`. `wait_until`, `referer`,
`arg_json`, and screenshot `options_json` are nullable where shown. A non-NULL
`arg_json` contains exactly one JSON value.

Calls are synchronous and require no ambient Tokio/event-loop runtime. A
binding may invoke them from a foreign worker thread. Do not concurrently call
through, close, or free the same handle, and never free a browser while a call
using it or one of its pages is active. Bindings should serialize operations on
each browser/page and must keep the native handle alive for the full call.
Different bindings must not cache `rw_last_error` across threads.

The evaluate wire is JSON. Primitives are ordinary JSON. The core can encode
arrays as `{ "__rustwright_cdp_array__": id, "items": [...] }` and objects as
`{ "__rustwright_cdp_object__": id, "entries": {...} }`; recursively unwrap
`items` and `entries`. It uses `__rustwright_cdp_ref__` for repeated/cyclic
references and tagged objects for undefined, non-finite numbers, dates,
regular expressions, URLs, errors, symbols, and functions.

The legacy core `decode_wire_value` and C ABI `rw_decode_wire` contract is
flattened plain JSON. Array and object wrappers are removed, repeated
non-cyclic references are duplicated, and references that form cycles become
`{"__rustwright_cdp_cycle__": true}`. Leaf scalar tags remain in the output for
binding-specific native mapping. This behavior is compatibility-stable.

Identity-preserving bindings use `rw_wire_graph_parse`. They allocate all
host containers by dense node id before they fill array and object edges.
This preserves repeated references, cycles, and object entry order. PyO3 and
napi use the same core graph directly. None of these adapters may substitute
the flattened `rw_decode_wire` compatibility path.

The core serializer is the single source of truth for this vocabulary; a
binding maps the core's tags to its closest native representation and must not
invent or assume tags the core does not emit. Manifest v1 expected/captured
values are JSON-compatible and never require cycles; a runner must at least
recursively decode array/object wrappers before capture or `assertEval`
comparison.

`RwWireGraph` is immutable and caller-owned. Borrowed string, object-key, and
leaf-field byte views remain valid only until `rw_wire_graph_free`. Bindings
must copy each view by its explicit length before graph release. The length can
include embedded NUL bytes. Empty views use NULL plus zero.

Node and leaf tag values are fixed in `capi/include/rustwright.h`, where both
public kind aliases are `int32_t` rather than implementation-defined C enums.
Leaf fields are positional: unserializable, bigint, date, and URL have one
field; regexp has pattern then flags; error has name, message, then stack;
undefined, symbol, and function have no fields.

### Thin-shim rule (single source of logic)

Any behavior expressible as a pure function of JSON-in/JSON-out — launch and
screenshot option normalization and defaulting, evaluate-wire parsing and
decoding, timeout-precedence resolution, data-URL construction, and structural
result comparison — is implemented once in `rustwright-core` and exposed
through the C ABI and native PyO3/napi adapters.

The legacy C ABI decoder intentionally remains a flattened JSON compatibility
path. Native graph adapters use core graph parsing to preserve host identity
and cycles. A binding limits itself to:

- marshalling native values to and from the documented JSON wire shapes,
- handle, memory, and thread ownership per this contract, and
- idiomatic naming and native-value coercion.

A binding must not introduce option defaults, timeout policy, retry or
polling loops, or its own copy of the evaluate decoder beyond leaf-scalar
mapping. New engine-semantic surface (contexts, default timeouts,
actionability waits, trusted input) lands in the core first and is exposed to
all bindings in the same change; a single-binding implementation of engine
semantics requires an explicit experimental gate and a core issue on file.

## Build and link

Build the shared foundation from the repository root:

```sh
cargo build -p rustwright-capi --release
```

The reviewed header is `capi/include/rustwright.h`. Unix artifacts are:

- macOS dynamic library: `target/release/librustwright_capi.dylib`
- Linux dynamic library: `target/release/librustwright_capi.so`
- static archive: `target/release/librustwright_capi.a`

The language runner receives the exact dynamic-library path via `--lib`; it
must load that path rather than relying on a repository-relative private path.
Runtime loader configuration (`rpath`, `LD_LIBRARY_PATH`, or equivalent) is a
binding/build concern.

## Binding directory and entrypoint convention

Each binding owns one top-level directory and keeps generated/build artifacts
untracked:

| Language | Directory | FFI mechanism |
| --- | --- | --- |
| Go | `go/` | `purego` (runtime `dlopen`, no cgo) |
| Java | `java/` | Java 22 FFM/Panama recommended; JNI accepted |
| C#/.NET | `csharp/` | P/Invoke |
| Ruby | `ruby/` | `fiddle` or `ffi` gem |
| PHP | `php/` | PHP FFI extension |
| Rust | `rust-native/` | in-process `rustwright-core` wrapper |

Every directory ships two documented executable entrypoints:

1. `smoke` mirrors `node/smoke.mjs`: launch headless Chromium, navigate to
   inline HTML containing a title, `#name`, `#go`, and `#message`; read the
   initial message; fill and click; read the changed message; evaluate the
   input value; screenshot; print a JSON record; and close page/browser.
2. `runner` implements the CLI and lifecycle below.

Package naming and build-system files may be idiomatic. Public wrapper methods
must expose the complete alpha surface, not only the operations used by smoke.

## Benchmark manifest v1

`bindings/cases/manifest.schema.json` is the machine-readable Draft 2020-12
schema. A manifest is:

```json
{ "version": 1, "cases": [ { "id": "stable-id", "html": "...", "steps": [] } ] }
```

Case ids are nonempty and must be unique within a manifest. `description` is
informational. `html` is consumed by `goto.useCaseHtml`. The optional case-level
`url` is source metadata in v1; navigation always comes from a `goto` step.
Steps execute in array order and capture names must be unique within a case.

Supported operations are exactly:

| Operation | Required behavior |
| --- | --- |
| `goto {url, waitUntil?}` | Navigate to the literal URL. |
| `goto {useCaseHtml:true, waitUntil?}` | Navigate to the canonical data URL built from case `html`. |
| `click {selector}` | Click the first matching element. |
| `fill {selector,value}` | Fill the first matching element. |
| `title {capture}` | Store the title string. |
| `textContent {selector,capture}` | Store string or JSON null. |
| `evaluate {expression,arg?,capture}` | JSON-encode optional arg, evaluate, decode wire JSON, and store the value. |
| `screenshot {capture}` | Capture default PNG bytes and store their byte length as a JSON number. |
| `assertTitle {equals}` / `{contains}` | Require exactly one string predicate. |
| `assertText {selector,equals}` / `{selector,contains}` | Require exactly one predicate; null fails. |
| `assertEval {expression,equals}` | Evaluate without an arg and use structural JSON equality. |

Canonical inline HTML URL construction is byte-for-byte defined as follows:

1. UTF-8 encode `case.html`.
2. Leave only ASCII `A-Z`, `a-z`, `0-9`, `-`, `.`, `_`, and `~` unchanged.
3. Encode every other byte as uppercase `%HH`.
4. Prefix the result with `data:text/html;charset=utf-8,`.

This is RFC 3986 unreserved-byte percent encoding. Do not use form encoding,
do not turn spaces into `+`, and do not use a platform encoder with a different
safe-character set.

## Runner CLI and results

Every runner is invoked with one common command shape:

```text
runner --manifest <path.json> --lib <path to librustwright_capi> \
       --out <results.json> [--cases id1,id2]
```

`--manifest`, `--lib`, and `--out` are required. The Rust runner accepts and
ignores `--lib` because it links the core in-process. `--cases` selects exact
ids; selection preserves manifest order. An unknown requested id or malformed
manifest is a CLI error.

Reference lifecycle:

- Launch one headless browser per runner invocation.
- Create one fresh page per selected case.
- Run cases and steps sequentially.
- Stop on the first failed step/assertion in a case, close that page, record the
  error, and continue with later cases.
- Close the browser after the last case.
- Measure wall-clock milliseconds per case, including page creation/close.
- Exit zero iff every selected case has `ok:true`; case failures exit 1 and
  invocation/manifest errors may exit 2.

Write this JSON shape to `--out` even when individual cases fail:

```json
{
  "lang": "go",
  "results": [
    {
      "id": "title-basic",
      "ok": true,
      "captures": { "title": "Rustwright Smoke Title" },
      "ms": 12.5
    },
    {
      "id": "another-case",
      "ok": false,
      "captures": {},
      "ms": 4.2,
      "error": "step 2: useful language-native error"
    }
  ]
}
```

`lang` is the lowercase stable name `go`, `java`, `csharp`, `ruby`, `php`, or
`rust`. Captured screenshot values are PNG byte lengths. Because all bindings
call the same core with the same fresh-page sequence, equivalent screenshot
captures must report identical byte lengths across languages.
