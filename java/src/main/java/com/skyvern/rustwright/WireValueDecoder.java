package com.skyvern.rustwright;

import java.math.BigInteger;
import java.net.URI;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/** Decodes the tagged JSON values produced by the CDP evaluate serializer. */
final class WireValueDecoder {
    private final Map<Object, Object> references = new HashMap<>();

    static Object decodeJson(String json) {
        return new WireValueDecoder().decode(Json.parse(json));
    }

    private Object decode(Object value) {
        if (value instanceof List<?> list) {
            List<Object> decoded = new ArrayList<>(list.size());
            for (Object item : list) {
                decoded.add(decode(item));
            }
            return decoded;
        }
        if (!(value instanceof Map<?, ?> rawMap)) {
            return value;
        }

        Map<String, Object> map = stringMap(rawMap);
        if (map.containsKey("__rustwright_cdp_ref__")) {
            Object id = map.get("__rustwright_cdp_ref__");
            if (!references.containsKey(id)) {
                throw new RustwrightException("evaluate wire contains an unknown reference: " + id);
            }
            return references.get(id);
        }
        if (map.containsKey("__rustwright_cdp_array__")) {
            Object id = map.get("__rustwright_cdp_array__");
            Object itemsValue = map.get("items");
            if (!(itemsValue instanceof List<?> items)) {
                throw new RustwrightException("evaluate array wrapper has no items array");
            }
            List<Object> decoded = new ArrayList<>(items.size());
            register(id, decoded);
            for (Object item : items) {
                decoded.add(decode(item));
            }
            return decoded;
        }
        if (map.containsKey("__rustwright_cdp_object__")) {
            Object id = map.get("__rustwright_cdp_object__");
            Object entriesValue = map.get("entries");
            if (!(entriesValue instanceof Map<?, ?> entries)) {
                throw new RustwrightException("evaluate object wrapper has no entries object");
            }
            Map<String, Object> decoded = new LinkedHashMap<>();
            register(id, decoded);
            for (Map.Entry<?, ?> entry : entries.entrySet()) {
                if (!(entry.getKey() instanceof String key)) {
                    throw new RustwrightException("evaluate object wrapper has a non-string key");
                }
                decoded.put(key, decode(entry.getValue()));
            }
            return decoded;
        }
        if (map.containsKey("__rustwright_cdp_undefined__")
                || map.containsKey("__rustwright_cdp_symbol__")
                || map.containsKey("__rustwright_cdp_function__")) {
            return null;
        }
        if (map.containsKey("__rustwright_cdp_unserializable_value__")) {
            return decodeUnserializable(String.valueOf(map.get("__rustwright_cdp_unserializable_value__")));
        }
        if (map.containsKey("__rustwright_cdp_date__")) {
            String date = String.valueOf(map.get("__rustwright_cdp_date__"));
            try {
                return Instant.parse(date);
            } catch (DateTimeParseException ignored) {
                return date;
            }
        }
        if (map.containsKey("__rustwright_cdp_regexp__")) {
            return decodePattern(map.get("__rustwright_cdp_regexp__"));
        }
        if (map.containsKey("__rustwright_cdp_url__")) {
            String url = String.valueOf(map.get("__rustwright_cdp_url__"));
            try {
                return URI.create(url);
            } catch (IllegalArgumentException ignored) {
                return url;
            }
        }
        if (map.containsKey("__rustwright_cdp_error__")) {
            return decodeError(map.get("__rustwright_cdp_error__"));
        }

        Map<String, Object> decoded = new LinkedHashMap<>();
        for (Map.Entry<String, Object> entry : map.entrySet()) {
            decoded.put(entry.getKey(), decode(entry.getValue()));
        }
        return decoded;
    }

    private void register(Object id, Object value) {
        if (references.putIfAbsent(id, value) != null) {
            throw new RustwrightException("evaluate wire reuses reference id: " + id);
        }
    }

    private static Object decodeUnserializable(String value) {
        return switch (value) {
            case "NaN" -> Double.NaN;
            case "Infinity" -> Double.POSITIVE_INFINITY;
            case "-Infinity" -> Double.NEGATIVE_INFINITY;
            case "-0" -> -0.0d;
            default -> value.endsWith("n")
                    ? parseBigIntegerOrString(value.substring(0, value.length() - 1), value)
                    : value;
        };
    }

    private static Object parseBigIntegerOrString(String digits, String fallback) {
        try {
            return new BigInteger(digits);
        } catch (NumberFormatException ignored) {
            return fallback;
        }
    }

    private static Object decodePattern(Object value) {
        if (!(value instanceof Map<?, ?> raw)) {
            return value;
        }
        Map<String, Object> regexp = stringMap(raw);
        String source = String.valueOf(regexp.getOrDefault("p", ""));
        String flags = String.valueOf(regexp.getOrDefault("f", ""));
        int javaFlags = 0;
        if (flags.indexOf('i') >= 0) {
            javaFlags |= Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE;
        }
        if (flags.indexOf('m') >= 0) {
            javaFlags |= Pattern.MULTILINE;
        }
        if (flags.indexOf('s') >= 0) {
            javaFlags |= Pattern.DOTALL;
        }
        if (flags.indexOf('u') >= 0) {
            javaFlags |= Pattern.UNICODE_CASE;
        }
        try {
            return Pattern.compile(source, javaFlags);
        } catch (RuntimeException ignored) {
            return value;
        }
    }

    private static Object decodeError(Object value) {
        if (!(value instanceof Map<?, ?> raw)) {
            return new JavaScriptErrorValue("Error", String.valueOf(value), null);
        }
        Map<String, Object> error = stringMap(raw);
        return new JavaScriptErrorValue(
                nullableText(error.get("name"), "Error"),
                nullableText(error.get("message"), ""),
                nullableText(error.get("stack"), null));
    }

    private static String nullableText(Object value, String fallback) {
        return value == null ? fallback : String.valueOf(value);
    }

    private static Map<String, Object> stringMap(Map<?, ?> raw) {
        Map<String, Object> result = new LinkedHashMap<>();
        for (Map.Entry<?, ?> entry : raw.entrySet()) {
            if (!(entry.getKey() instanceof String key)) {
                throw new RustwrightException("evaluate wire object has a non-string key");
            }
            result.put(key, entry.getValue());
        }
        return result;
    }
}
