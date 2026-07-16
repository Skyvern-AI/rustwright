package com.skyvern.rustwright;

import java.nio.file.Path;
import java.util.Map;
import java.util.Objects;

/** Entry point for Chromium discovery and launch through one explicit native library. */
public final class Chromium {
    private final NativeBindings bindings;

    public Chromium(Path libraryPath) {
        bindings = new NativeBindings(Objects.requireNonNull(libraryPath, "libraryPath"));
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
