package com.skyvern.rustwright;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.Locale;

/** Resolves an exact native path or extracts the native bundled for this JVM. */
final class NativeLibraryResolver {
    private NativeLibraryResolver() {}

    static Path resolve(Path explicitPath) {
        if (explicitPath != null) {
            // An explicit path is an exact pin. Never substitute a bundled or
            // repository-relative library when the caller supplied one.
            return explicitPath;
        }

        Platform platform = Platform.current();
        Path bundled = extractBundled(platform);
        if (bundled != null) {
            return bundled;
        }

        // Preserve the from-source fallback used before native JAR bundling.
        return Path.of("target", "release", platform.fallbackLibraryName());
    }

    private static Path extractBundled(Platform platform) {
        String resource = "/native/" + platform.resourceDirectory()
                + "/" + platform.resourceLibraryName();
        try (InputStream input = NativeLibraryResolver.class.getResourceAsStream(resource)) {
            if (input == null) {
                return null;
            }

            Path directory = Files.createTempDirectory("rustwright-" + platform.resourceDirectory() + "-");
            directory.toFile().deleteOnExit();
            Path library = directory.resolve(platform.resourceLibraryName());
            Files.copy(input, library, StandardCopyOption.REPLACE_EXISTING);
            library.toFile().deleteOnExit();
            return library;
        } catch (IOException error) {
            throw new RustwrightException("cannot extract bundled Rustwright library "
                    + resource + ": " + error.getMessage(), error);
        }
    }

    private record Platform(
            String resourceDirectory,
            String resourceLibraryName,
            String fallbackLibraryName) {
        private static Platform current() {
            String os = System.getProperty("os.name", "").toLowerCase(Locale.ROOT);
            String arch = System.getProperty("os.arch", "").toLowerCase(Locale.ROOT);
            String normalizedArch = switch (arch) {
                case "aarch64", "arm64" -> "aarch64";
                case "amd64", "x86_64" -> "x86_64";
                default -> throw unsupported(os, arch);
            };

            if (os.contains("mac") || os.contains("darwin")) {
                return new Platform("osx-" + normalizedArch,
                        "librustwright_capi.dylib", "librustwright_capi.dylib");
            }
            if (os.contains("linux")) {
                return new Platform("linux-" + normalizedArch,
                        "librustwright_capi.so", "librustwright_capi.so");
            }
            if (os.contains("win") && normalizedArch.equals("x86_64")) {
                return new Platform("windows-x86_64",
                        "librustwright_capi.dll", "rustwright_capi.dll");
            }
            throw unsupported(os, arch);
        }

        private static UnsupportedOperationException unsupported(String os, String arch) {
            return new UnsupportedOperationException(
                    "Rustwright has no bundled native library for os.name=" + os + ", os.arch=" + arch);
        }
    }
}
