package com.skyvern.rustwright;

import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

final class Options {
    private static final Map<String, String> LAUNCH_NAMES = Map.ofEntries(
            Map.entry("executablePath", "executable_path"),
            Map.entry("userDataDir", "user_data_dir"),
            Map.entry("ignoreAllDefaultArgs", "ignore_all_default_args"),
            Map.entry("ignoreDefaultArgs", "ignore_default_args"),
            Map.entry("chromiumSandbox", "chromium_sandbox"));

    private static final Map<String, String> SCREENSHOT_NAMES = Map.of(
            "full_page", "fullPage",
            "omit_background", "omitBackground");

    private Options() {}

    static Map<String, Object> launch(Map<String, ?> options) {
        return normalize(options, LAUNCH_NAMES);
    }

    static Map<String, Object> screenshot(Map<String, ?> options) {
        return normalize(options, SCREENSHOT_NAMES);
    }

    private static Map<String, Object> normalize(Map<String, ?> options, Map<String, String> names) {
        Map<String, Object> normalized = new LinkedHashMap<>();
        for (Map.Entry<String, ?> entry : options.entrySet()) {
            String key = names.getOrDefault(entry.getKey(), entry.getKey());
            Object value = normalizeValue(entry.getValue());
            if (normalized.containsKey(key)) {
                throw new IllegalArgumentException("option is specified twice after normalization: " + key);
            }
            normalized.put(key, value);
        }
        return normalized;
    }

    private static Object normalizeValue(Object value) {
        if (value instanceof Path path) {
            return path.toString();
        }
        return value;
    }
}
