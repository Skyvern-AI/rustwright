package com.skyvern.rustwright;

import java.nio.file.Path;
import java.util.Map;
import java.util.Objects;

/** Entry point for Chromium discovery and launch through one native library. */
public final class Chromium {
    private final NativeBindings bindings;

    /** Uses the native bundled for this platform, then the from-source fallback. */
    public Chromium() {
        bindings = new NativeBindings(NativeLibraryResolver.resolve(null));
    }

    /** Uses only the exact native library path supplied by the caller. */
    public Chromium(Path libraryPath) {
        bindings = new NativeBindings(NativeLibraryResolver.resolve(
                Objects.requireNonNull(libraryPath, "libraryPath")));
    }

    public Path libraryPath() {
        return bindings.libraryPath();
    }

    public String executablePath() {
        return bindings.chromiumExecutablePath();
    }

    public Browser launch() {
        return launch(Map.of());
    }

    public Browser launch(Map<String, ?> options) {
        Map<String, Object> normalized = Options.launch(
                options == null ? Map.of() : options);
        return new Browser(bindings, bindings.chromiumLaunch(Json.stringify(normalized)));
    }
}
