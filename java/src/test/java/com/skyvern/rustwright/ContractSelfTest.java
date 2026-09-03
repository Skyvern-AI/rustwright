package com.skyvern.rustwright;

import java.math.BigInteger;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

public final class ContractSelfTest {
    private ContractSelfTest() {}

    public static void main(String[] arguments) {
        check(Runner.caseHtmlUrl("A z/~é").equals(
                "data:text/html;charset=utf-8,A%20z%2F~%C3%A9"), "canonical data URL");

        String nativeLibrary = System.getenv("RUSTWRIGHT_CAPI_LIB");
        if (nativeLibrary != null && !nativeLibrary.isBlank()) {
            verifyNativeGraph(Path.of(nativeLibrary));
        }

        expectInvalid("unknown version", Map.of("version", 2, "cases", validCases()));
        expectInvalid("duplicate ids", Map.of("version", 1, "cases", List.of(
                validCase("same", List.of(Map.of("op", "title", "capture", "a"))),
                validCase("same", List.of(Map.of("op", "title", "capture", "b"))))));
        expectInvalid("duplicate captures", Map.of("version", 1, "cases", List.of(
                validCase("captures", List.of(
                        Map.of("op", "title", "capture", "same"),
                        Map.of("op", "screenshot", "capture", "same"))))));
        expectInvalid("unknown op", Map.of("version", 1, "cases", List.of(
                validCase("operation", List.of(Map.of("op", "hover"))))));

        Map<String, Object> normalized = Options.launch(Map.of(
                "headless", true, "executablePath", "/browser", "userDataDir", "/profile"));
        check(normalized.containsKey("executable_path") && normalized.containsKey("user_data_dir"),
                "launch option normalization");
        System.out.println("ContractSelfTest: ok");
    }

    @SuppressWarnings("unchecked")
    private static void verifyNativeGraph(Path libraryPath) {
        NativeBindings bindings = new NativeBindings(libraryPath);
        Object decoded = WireValueDecoder.decode("""
                {
                  "__rustwright_cdp_object__": 1,
                  "entries": {
                    "self": {"__rustwright_cdp_ref__": 1},
                    "shared": {
                      "__rustwright_cdp_object__": 2,
                      "entries": {"value": "a\\u0000b"}
                    },
                    "again": {"__rustwright_cdp_ref__": 2},
                    "bigint": {"__rustwright_cdp_unserializable_value__": "123n"}
                  }
                }
                """, bindings);
        Map<String, Object> root = (Map<String, Object>) decoded;
        check(root.get("self") == root, "wire graph self-cycle");
        check(root.get("shared") == root.get("again"), "wire graph repeated identity");
        check(((Map<String, Object>) root.get("shared")).get("value").equals("a\0b"),
                "wire graph embedded NUL");
        check(root.get("bigint").equals(BigInteger.valueOf(123)), "wire graph bigint");
    }

    private static List<Map<String, Object>> validCases() {
        return List.of(validCase("valid", List.of(Map.of("op", "title", "capture", "title"))));
    }

    private static Map<String, Object> validCase(String id, List<Map<String, Object>> steps) {
        return Map.of("id", id, "steps", steps);
    }

    private static void expectInvalid(String label, Object manifest) {
        try {
            Manifest.validate(manifest);
            throw new AssertionError(label + " was accepted");
        } catch (IllegalArgumentException expected) {
            // Expected manifest/schema rejection.
        }
    }

    private static void check(boolean condition, String label) {
        if (!condition) {
            throw new AssertionError(label + " failed");
        }
    }
}
