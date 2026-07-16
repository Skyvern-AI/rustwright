# Rustwright C# alpha binding

This package is a synchronous .NET 8 P/Invoke binding for the Rustwright C ABI. It contains:

- `Rustwright/`: the public Chromium, Browser, and Page API plus all 19 native declarations.
- `Smoke/`: the reference inline-HTML smoke executable.
- `Runner/`: the manifest-v1 benchmark runner and strict manifest validation.

Run these commands from the repository root. When `--lib` is omitted, the binding looks for the platform-specific shared library in `target/release/` (`librustwright_capi.dylib` on macOS or `librustwright_capi.so` on Linux).

## Install (NuGet)

The package is not published yet. Once available from NuGet, install it with:

```sh
dotnet add package Rustwright
```

The package carries native libraries for macOS (Arm64 and x64), Linux (Arm64 and x64), and Windows x64. Package consumers do not need to configure a native library path explicitly.

## Build from source

Build the C ABI before running either source project:

```sh
cargo build -p rustwright-capi --release --locked
```

## Smoke

```sh
dotnet run --project csharp/Smoke -- --lib target/release/librustwright_capi.dylib
```

## Benchmark runner

```sh
dotnet run --project csharp/Runner -- --manifest bindings/cases/smoke.json --lib target/release/librustwright_capi.dylib --out /tmp/csharp-layout.json
```

The runner also accepts `--cases id1,id2`, preserves manifest order, and exits 0 only when all selected cases pass. `--manifest`, `--lib`, and `--out` are required.

## API sketch

```csharp
NativeLibraryLoader.Configure("target/release/librustwright_capi.dylib");
using var browser = Chromium.Launch(new LaunchOptions { Headless = true });
using var page = browser.NewPage();
page.Goto("data:text/html,hello");
Console.WriteLine(page.Title());
```

`LaunchOptions` properties serialize to the core snake_case wire names. `ScreenshotOptions` properties serialize to the Node-compatible `fullPage` and `omitBackground` names. An omitted native timeout is always sent as `double.NaN`.

The evaluate decoder recursively unwraps Rustwright array/object/reference tags. It maps JavaScript dates to `DateTimeOffset`, URLs to `Uri`, bigint values to `BigInteger`, regular expressions and errors to binding records, and non-finite numbers to their .NET `double` equivalents. JavaScript `undefined`, symbols, and functions have no direct JSON/.NET representation and fall back to `null`.

Opaque handles are owned by `SafeHandle` instances. Explicit `Close` performs browser/page lifecycle shutdown before the Rust allocation is freed; `Dispose` is idempotent. Rust-owned strings and screenshot buffers are copied into managed memory and released through the matching ABI deallocator.
