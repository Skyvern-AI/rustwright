package com.skyvern.rustwright;

import java.lang.reflect.Array;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/** Small, dependency-free JSON codec used for the ABI wire format and manifests. */
final class Json {
    private Json() {}

    static Object parse(String json) {
        return new Parser(Objects.requireNonNull(json, "json")).parseDocument();
    }

    static String stringify(Object value) {
        StringBuilder output = new StringBuilder();
        write(value, output);
        return output.toString();
    }

    static boolean structuralEquals(Object left, Object right) {
        if (left == right) {
            return true;
        }
        if (left == null || right == null) {
            return false;
        }
        if (left instanceof Number a && right instanceof Number b) {
            if (isNonFinite(a) || isNonFinite(b)) {
                return Double.doubleToLongBits(a.doubleValue()) == Double.doubleToLongBits(b.doubleValue());
            }
            try {
                return new BigDecimal(a.toString()).compareTo(new BigDecimal(b.toString())) == 0;
            } catch (NumberFormatException ignored) {
                return a.doubleValue() == b.doubleValue();
            }
        }
        if (left instanceof List<?> a && right instanceof List<?> b) {
            if (a.size() != b.size()) {
                return false;
            }
            for (int i = 0; i < a.size(); i++) {
                if (!structuralEquals(a.get(i), b.get(i))) {
                    return false;
                }
            }
            return true;
        }
        if (left instanceof Map<?, ?> a && right instanceof Map<?, ?> b) {
            if (!a.keySet().equals(b.keySet())) {
                return false;
            }
            for (Object key : a.keySet()) {
                if (!structuralEquals(a.get(key), b.get(key))) {
                    return false;
                }
            }
            return true;
        }
        return left.equals(right);
    }

    private static boolean isNonFinite(Number number) {
        return (number instanceof Double || number instanceof Float)
                && !Double.isFinite(number.doubleValue());
    }

    private static void write(Object value, StringBuilder output) {
        if (value == null) {
            output.append("null");
        } else if (value instanceof String string) {
            writeString(string, output);
        } else if (value instanceof Boolean bool) {
            output.append(bool);
        } else if (value instanceof BigDecimal decimal) {
            output.append(decimal.toPlainString());
        } else if (value instanceof BigInteger integer) {
            output.append(integer);
        } else if (value instanceof Byte || value instanceof Short
                || value instanceof Integer || value instanceof Long) {
            output.append(value);
        } else if (value instanceof Number number) {
            if (!Double.isFinite(number.doubleValue())) {
                throw new IllegalArgumentException("JSON cannot encode non-finite number: " + number);
            }
            output.append(number);
        } else if (value instanceof Map<?, ?> map) {
            output.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!(entry.getKey() instanceof String key)) {
                    throw new IllegalArgumentException("JSON object keys must be strings");
                }
                if (!first) {
                    output.append(',');
                }
                first = false;
                writeString(key, output);
                output.append(':');
                write(entry.getValue(), output);
            }
            output.append('}');
        } else if (value instanceof Iterable<?> iterable) {
            output.append('[');
            boolean first = true;
            for (Object item : iterable) {
                if (!first) {
                    output.append(',');
                }
                first = false;
                write(item, output);
            }
            output.append(']');
        } else if (value.getClass().isArray()) {
            output.append('[');
            for (int i = 0; i < Array.getLength(value); i++) {
                if (i != 0) {
                    output.append(',');
                }
                write(Array.get(value, i), output);
            }
            output.append(']');
        } else {
            throw new IllegalArgumentException("unsupported JSON value type: " + value.getClass().getName());
        }
    }

    private static void writeString(String value, StringBuilder output) {
        output.append('"');
        for (int i = 0; i < value.length(); i++) {
            char character = value.charAt(i);
            switch (character) {
                case '"' -> output.append("\\\"");
                case '\\' -> output.append("\\\\");
                case '\b' -> output.append("\\b");
                case '\f' -> output.append("\\f");
                case '\n' -> output.append("\\n");
                case '\r' -> output.append("\\r");
                case '\t' -> output.append("\\t");
                default -> {
                    if (character < 0x20) {
                        output.append(String.format("\\u%04X", (int) character));
                    } else {
                        output.append(character);
                    }
                }
            }
        }
        output.append('"');
    }

    private static final class Parser {
        private final String input;
        private int position;

        private Parser(String input) {
            this.input = input;
        }

        private Object parseDocument() {
            skipWhitespace();
            Object value = parseValue();
            skipWhitespace();
            if (position != input.length()) {
                fail("unexpected trailing content");
            }
            return value;
        }

        private Object parseValue() {
            if (position >= input.length()) {
                return fail("expected a JSON value");
            }
            return switch (input.charAt(position)) {
                case 'n' -> literal("null", null);
                case 't' -> literal("true", Boolean.TRUE);
                case 'f' -> literal("false", Boolean.FALSE);
                case '"' -> parseString();
                case '[' -> parseArray();
                case '{' -> parseObject();
                default -> parseNumber();
            };
        }

        private Object literal(String literal, Object value) {
            if (!input.startsWith(literal, position)) {
                return fail("expected " + literal);
            }
            position += literal.length();
            return value;
        }

        private String parseString() {
            position++;
            StringBuilder value = new StringBuilder();
            while (position < input.length()) {
                char character = input.charAt(position++);
                if (character == '"') {
                    return value.toString();
                }
                if (character == '\\') {
                    if (position >= input.length()) {
                        return fail("unterminated escape sequence");
                    }
                    char escape = input.charAt(position++);
                    switch (escape) {
                        case '"', '\\', '/' -> value.append(escape);
                        case 'b' -> value.append('\b');
                        case 'f' -> value.append('\f');
                        case 'n' -> value.append('\n');
                        case 'r' -> value.append('\r');
                        case 't' -> value.append('\t');
                        case 'u' -> value.append(parseUnicodeEscape());
                        default -> fail("invalid escape sequence");
                    }
                } else {
                    if (character < 0x20) {
                        return fail("unescaped control character");
                    }
                    value.append(character);
                }
            }
            return fail("unterminated string");
        }

        private char parseUnicodeEscape() {
            if (position + 4 > input.length()) {
                return fail("incomplete unicode escape");
            }
            int value = 0;
            for (int i = 0; i < 4; i++) {
                int digit = Character.digit(input.charAt(position++), 16);
                if (digit < 0) {
                    return fail("invalid unicode escape");
                }
                value = value * 16 + digit;
            }
            return (char) value;
        }

        private List<Object> parseArray() {
            position++;
            skipWhitespace();
            List<Object> values = new ArrayList<>();
            if (consume(']')) {
                return values;
            }
            while (true) {
                skipWhitespace();
                values.add(parseValue());
                skipWhitespace();
                if (consume(']')) {
                    return values;
                }
                expect(',');
            }
        }

        private Map<String, Object> parseObject() {
            position++;
            skipWhitespace();
            Map<String, Object> values = new LinkedHashMap<>();
            if (consume('}')) {
                return values;
            }
            while (true) {
                skipWhitespace();
                if (position >= input.length() || input.charAt(position) != '"') {
                    return fail("expected an object key");
                }
                String key = parseString();
                skipWhitespace();
                expect(':');
                skipWhitespace();
                Object value = parseValue();
                if (values.containsKey(key)) {
                    return fail("duplicate object key: " + key);
                }
                values.put(key, value);
                skipWhitespace();
                if (consume('}')) {
                    return values;
                }
                expect(',');
            }
        }

        private Number parseNumber() {
            int start = position;
            consume('-');
            if (consume('0')) {
                if (position < input.length() && Character.isDigit(input.charAt(position))) {
                    return fail("leading zero in number");
                }
            } else {
                requireDigit();
                while (position < input.length() && Character.isDigit(input.charAt(position))) {
                    position++;
                }
            }
            boolean decimal = false;
            if (consume('.')) {
                decimal = true;
                requireDigit();
                while (position < input.length() && Character.isDigit(input.charAt(position))) {
                    position++;
                }
            }
            if (position < input.length() && (input.charAt(position) == 'e' || input.charAt(position) == 'E')) {
                decimal = true;
                position++;
                if (position < input.length() && (input.charAt(position) == '+' || input.charAt(position) == '-')) {
                    position++;
                }
                requireDigit();
                while (position < input.length() && Character.isDigit(input.charAt(position))) {
                    position++;
                }
            }
            if (start == position) {
                return fail("expected a JSON value");
            }
            String token = input.substring(start, position);
            try {
                if (decimal) {
                    return new BigDecimal(token);
                }
                try {
                    return Long.valueOf(token);
                } catch (NumberFormatException tooLargeForLong) {
                    return new BigInteger(token);
                }
            } catch (NumberFormatException invalid) {
                return fail("invalid number");
            }
        }

        private void requireDigit() {
            if (position >= input.length() || !Character.isDigit(input.charAt(position))) {
                fail("expected a digit");
            }
        }

        private void expect(char expected) {
            if (!consume(expected)) {
                fail("expected '" + expected + "'");
            }
        }

        private boolean consume(char expected) {
            if (position < input.length() && input.charAt(position) == expected) {
                position++;
                return true;
            }
            return false;
        }

        private void skipWhitespace() {
            while (position < input.length()) {
                char character = input.charAt(position);
                if (character == ' ' || character == '\n' || character == '\r' || character == '\t') {
                    position++;
                } else {
                    return;
                }
            }
        }

        private <T> T fail(String message) {
            throw new IllegalArgumentException(message + " at character " + position);
        }
    }
}
