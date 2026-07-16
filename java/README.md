# Rustwright Java alpha binding

This dependency-free Java 23 package uses the finalized `java.lang.foreign` API to load an explicit Rustwright C ABI dynamic library. It owns and serializes native browser/page handles, copies native errors immediately, and releases all transferred Rust strings, screenshot buffers, and opaque handles with their matching ABI free function.

## Requirements

- A 64-bit JDK 23 (`java` and `javac` on `PATH`)
- The shared library at `target/release/librustwright_capi.dylib` on macOS or `target/release/librustwright_capi.so` on Linux
- `--enable-native-access=ALL-UNNAMED` (already supplied by `run.sh`)

Run all commands from the repository root. On macOS, the exact smoke command is:

```sh
./java/run.sh smoke --lib target/release/librustwright_capi.dylib
```

The smoke command also accepts no arguments, defaulting to `target/release/librustwright_capi.dylib` on macOS and `target/release/librustwright_capi.so` on Linux, relative to the repository-root working directory.

The exact five-case runner command is:

```sh
./java/run.sh runner --manifest bindings/cases/smoke.json --lib target/release/librustwright_capi.dylib --out /tmp/java-results.json
```

The runner also accepts `--cases id1,id2`, preserves manifest order, writes the contract result JSON, and exits 0 only when every selected case passes. `--manifest`, `--lib`, and `--out` are required for the runner.

Run the dependency-free self-test from the repository root with:

```sh
mkdir -p java/build/test-classes
find java/src/main/java java/src/test/java -name '*.java' -print0 | xargs -0 javac --release 23 -encoding UTF-8 -d java/build/test-classes
java -cp java/build/test-classes com.skyvern.rustwright.ContractSelfTest
```

## API

The package is `com.skyvern.rustwright`. Construct `Chromium` with the exact native library path, then use owned `Browser` and `Page` values with try-with-resources:

```java
Chromium chromium = new Chromium(Path.of("target/release/librustwright_capi.dylib"));
try (Browser browser = chromium.launch(Map.of("headless", true));
     Page page = browser.newPage()) {
    page.goTo("data:text/html,<title>Hello</title>");
    System.out.println(page.title());
}
```

Java reserves the word `goto`, so the contract's `page.goto` operation is exposed as `page.goTo`. The complete alpha API is present: `Chromium.launch/executablePath`, `Browser.newPage/close/wsEndpoint`, and `Page.goTo/click/fill/title/textContent/evaluate/screenshot/close`. `Page.targetId` is also exposed because the underlying ABI includes it.

Launch maps accept camelCase Java/Node names and normalize `executablePath`, `userDataDir`, `ignoreAllDefaultArgs`, `ignoreDefaultArgs`, and `chromiumSandbox` to the C wire's snake_case fields. Screenshot maps use the Node wire names (`fullPage`, `omitBackground`, and so on), with snake_case aliases for those two camelCase fields. A null timeout becomes the ABI's `Double.NaN` no-timeout sentinel.

`evaluate` recursively unwraps Rustwright's array/object/reference tags. JSON primitives, lists, and maps become their natural Java counterparts; dates become `Instant`, URLs become `URI`, regular expressions become `Pattern`, errors become `JavaScriptErrorValue`, and non-finite values become Java `Double` values. JavaScript `undefined`, symbols, and functions intentionally fall back to Java `null`. Repeated and cyclic references retain identity, although manifest-v1 captures are guaranteed to be JSON-compatible and acyclic.
