package com.skyvern.rustwright;

import java.math.BigInteger;
import java.net.URI;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.lang.foreign.MemorySegment;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/** Materializes the canonical native wire graph without exposing native views. */
final class WireValueDecoder {
    private static final int NODE_NULL = 0;
    private static final int NODE_BOOL = 1;
    private static final int NODE_SIGNED = 2;
    private static final int NODE_UNSIGNED = 3;
    private static final int NODE_FLOAT = 4;
    private static final int NODE_STRING = 5;
    private static final int NODE_ARRAY = 6;
    private static final int NODE_OBJECT = 7;
    private static final int NODE_LEAF = 8;

    private static final int LEAF_UNSERIALIZABLE = 0;
    private static final int LEAF_BIGINT = 1;
    private static final int LEAF_DATE = 2;
    private static final int LEAF_REGEXP = 3;
    private static final int LEAF_URL = 4;
    private static final int LEAF_ERROR = 5;
    private static final int LEAF_UNDEFINED = 6;
    private static final int LEAF_SYMBOL = 7;
    private static final int LEAF_FUNCTION = 8;

    private WireValueDecoder() {}

    static Object decode(String json, NativeBindings bindings) {
        return decodeGraph(bindings.wireGraphParse(json), bindings);
    }

    static Object decodeGraph(MemorySegment graph, NativeBindings bindings) {
        try {
            return new Materializer(bindings, graph).materialize();
        } finally {
            bindings.wireGraphFree(graph);
        }
    }


    private static final class Materializer {
        private final NativeBindings bindings;
        private final MemorySegment graph;
        private Object[] values;
        private int[] kinds;

        private Materializer(NativeBindings bindings, MemorySegment graph) {
            this.bindings = bindings;
            this.graph = graph;
        }

        private Object materialize() {
            long countValue = bindings.wireGraphNodeCount(graph);
            int count = checkedLength(countValue, "node count");
            long root = bindings.wireGraphRoot(graph);
            values = new Object[count];
            kinds = new int[count];

            // First pass: every node gets its final scalar or an empty identity-bearing container.
            for (int node = 0; node < count; node++) {
                long nodeId = node;
                int kind = bindings.wireGraphNodeKind(graph, nodeId);
                kinds[node] = kind;
                values[node] = switch (kind) {
                    case NODE_NULL -> null;
                    case NODE_BOOL -> bindings.wireGraphGetBool(graph, nodeId);
                    case NODE_SIGNED -> bindings.wireGraphGetSigned(graph, nodeId);
                    case NODE_UNSIGNED -> decodeUnsigned(bindings.wireGraphGetUnsigned(graph, nodeId));
                    case NODE_FLOAT -> bindings.wireGraphGetFloat(graph, nodeId);
                    case NODE_STRING -> bindings.wireGraphGetString(graph, nodeId);
                    case NODE_ARRAY -> emptyArray(bindings.wireGraphArrayLength(graph, nodeId));
                    case NODE_OBJECT -> new LinkedHashMap<String, Object>();
                    case NODE_LEAF -> decodeLeaf(nodeId);
                    default -> throw new RustwrightException("native wire graph returned unknown node kind " + kind);
                };
            }

            // Second pass: all child ids now point at allocated placeholders or immutable values.
            for (int node = 0; node < count; node++) {
                long nodeId = node;
                if (kinds[node] == NODE_ARRAY) {
                    @SuppressWarnings("unchecked")
                    List<Object> array = (List<Object>) values[node];
                    long length = bindings.wireGraphArrayLength(graph, nodeId);
                    for (long index = 0; index < length; index++) {
                        array.set(checkedIndex(index, array.size(), "array child"),
                                values[nodeIndex(bindings.wireGraphArrayChild(graph, nodeId, index))]);
                    }
                } else if (kinds[node] == NODE_OBJECT) {
                    @SuppressWarnings("unchecked")
                    Map<String, Object> object = (Map<String, Object>) values[node];
                    long length = bindings.wireGraphObjectLength(graph, nodeId);
                    for (long index = 0; index < length; index++) {
                        String key = bindings.wireGraphObjectKey(graph, nodeId, index);
                        object.put(key, values[nodeIndex(
                                bindings.wireGraphObjectChild(graph, nodeId, index))]);
                    }
                }
            }
            return values[nodeIndex(root)];
        }

        private Object decodeLeaf(long node) {
            int leafKind = bindings.wireGraphLeafKind(graph, node);
            long fieldCountValue = bindings.wireGraphLeafFieldCount(graph, node);
            int fieldCount = checkedLength(fieldCountValue, "leaf field count");
            List<String> fields = new ArrayList<>(fieldCount);
            for (long index = 0; index < fieldCount; index++) {
                fields.add(bindings.wireGraphLeafField(graph, node, index));
            }

            return switch (leafKind) {
                case LEAF_UNSERIALIZABLE -> requireFields(fields, 1, "unserializable",
                        () -> decodeUnserializable(fields.get(0)));
                case LEAF_BIGINT -> requireFields(fields, 1, "bigint",
                        () -> parseBigIntegerOrString(fields.get(0), fields.get(0)));
                case LEAF_DATE -> requireFields(fields, 1, "date",
                        () -> decodeDate(fields.get(0)));
                case LEAF_REGEXP -> requireFields(fields, 2, "regexp",
                        () -> decodePattern(fields.get(0), fields.get(1)));
                case LEAF_URL -> requireFields(fields, 1, "url",
                        () -> decodeUrl(fields.get(0)));
                case LEAF_ERROR -> requireFields(fields, 3, "error",
                        () -> new JavaScriptErrorValue(fields.get(0), fields.get(1), fields.get(2)));
                case LEAF_UNDEFINED, LEAF_SYMBOL, LEAF_FUNCTION -> {
                    if (!fields.isEmpty()) {
                        throw new RustwrightException("native wire graph leaf has unexpected fields");
                    }
                    yield null;
                }
                default -> throw new RustwrightException("native wire graph returned unknown leaf kind " + leafKind);
            };
        }

        private int nodeIndex(long node) {
            return checkedIndex(node, values.length, "node id");
        }

        private static List<Object> emptyArray(long lengthValue) {
            int length = checkedLength(lengthValue, "array length");
            List<Object> array = new ArrayList<>(length);
            for (int index = 0; index < length; index++) {
                array.add(null);
            }
            return array;
        }

        private static <T> T requireFields(
                List<String> fields,
                int expected,
                String leaf,
                java.util.function.Supplier<T> decoder) {
            if (fields.size() != expected) {
                throw new RustwrightException(
                        "native wire graph " + leaf + " leaf has " + fields.size()
                                + " fields; expected " + expected);
            }
            return decoder.get();
        }
    }

    private static Object decodeUnsigned(long value) {
        return value >= 0 ? value : new BigInteger(Long.toUnsignedString(value));
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

    private static Object decodeDate(String date) {
        try {
            return Instant.parse(date);
        } catch (DateTimeParseException ignored) {
            return date;
        }
    }

    private static Object decodePattern(String source, String flags) {
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
            Map<String, Object> fallback = new LinkedHashMap<>();
            fallback.put("p", source);
            fallback.put("f", flags);
            return fallback;
        }
    }

    private static Object decodeUrl(String url) {
        try {
            return URI.create(url);
        } catch (IllegalArgumentException ignored) {
            return url;
        }
    }

    private static int checkedLength(long value, String what) {
        if (value < 0 || value > Integer.MAX_VALUE) {
            throw new RustwrightException("native wire graph returned invalid " + what + ": " + value);
        }
        return (int) value;
    }

    private static int checkedIndex(long value, int size, String what) {
        if (value < 0 || value >= size) {
            throw new RustwrightException("native wire graph returned invalid " + what + ": " + value);
        }
        return (int) value;
    }
}
