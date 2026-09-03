package com.skyvern.rustwright;

import java.lang.foreign.Arena;
import java.lang.foreign.FunctionDescriptor;
import java.lang.foreign.Linker;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.SymbolLookup;
import java.lang.foreign.ValueLayout;
import java.lang.invoke.MethodHandle;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Objects;

/** Exact Java FFM declarations and ownership helpers for Rustwright's native ABI. */
final class NativeBindings {
    private static final ValueLayout.OfLong SIZE_T = ValueLayout.JAVA_LONG;
    private static final long MAX_C_STRING_BYTES = Integer.MAX_VALUE;

    private final Path libraryPath;
    // Shared because calls are allowed from different Java threads. The wrapper locks each handle.
    @SuppressWarnings("FieldCanBeLocal")
    private final Arena libraryArena;

    private final MethodHandle rwLastError;
    private final MethodHandle rwStringFree;
    private final MethodHandle rwBytesFree;
    private final MethodHandle rwChromiumExecutablePath;
    private final MethodHandle rwChromiumLaunch;
    private final MethodHandle rwBrowserNewPage;
    private final MethodHandle rwBrowserClose;
    private final MethodHandle rwBrowserWsEndpoint;
    private final MethodHandle rwBrowserFree;
    private final MethodHandle rwPageTargetId;
    private final MethodHandle rwPageGoto;
    private final MethodHandle rwPageClick;
    private final MethodHandle rwPageFill;
    private final MethodHandle rwPageTitle;
    private final MethodHandle rwPageTextContent;
    private final MethodHandle rwPageEvaluate;
    private final MethodHandle rwPageScreenshot;
    private final MethodHandle rwPageClose;
    private final MethodHandle rwPageFree;
    private final MethodHandle rwWireGraphParse;
    private final MethodHandle rwWireGraphFree;
    private final MethodHandle rwWireGraphNodeCount;
    private final MethodHandle rwWireGraphRoot;
    private final MethodHandle rwWireGraphNodeKind;
    private final MethodHandle rwWireGraphGetBool;
    private final MethodHandle rwWireGraphGetSigned;
    private final MethodHandle rwWireGraphGetUnsigned;
    private final MethodHandle rwWireGraphGetFloat;
    private final MethodHandle rwWireGraphGetString;
    private final MethodHandle rwWireGraphArrayLength;
    private final MethodHandle rwWireGraphArrayChild;
    private final MethodHandle rwWireGraphObjectLength;
    private final MethodHandle rwWireGraphObjectKey;
    private final MethodHandle rwWireGraphObjectChild;
    private final MethodHandle rwWireGraphLeafKind;
    private final MethodHandle rwWireGraphLeafFieldCount;
    private final MethodHandle rwWireGraphLeafField;


    NativeBindings(Path path) {
        Objects.requireNonNull(path, "path");
        if (ValueLayout.ADDRESS.byteSize() != Long.BYTES) {
            throw new UnsupportedOperationException("Rustwright Java currently requires a 64-bit JVM");
        }
        try {
            libraryPath = path.toAbsolutePath().normalize().toRealPath();
        } catch (Exception error) {
            throw new RustwrightException("cannot resolve Rustwright library " + path + ": " + error.getMessage(), error);
        }

        libraryArena = Arena.ofShared();
        Linker linker = Linker.nativeLinker();
        SymbolLookup lookup = SymbolLookup.libraryLookup(libraryPath, libraryArena);

        rwLastError = bind(linker, lookup, "rw_last_error", FunctionDescriptor.of(ValueLayout.ADDRESS));
        rwStringFree = bind(linker, lookup, "rw_string_free",
                FunctionDescriptor.ofVoid(ValueLayout.ADDRESS));
        rwBytesFree = bind(linker, lookup, "rw_bytes_free",
                FunctionDescriptor.ofVoid(ValueLayout.ADDRESS, SIZE_T));
        rwChromiumExecutablePath = bind(linker, lookup, "rw_chromium_executable_path",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS));
        rwChromiumLaunch = bind(linker, lookup, "rw_chromium_launch",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.ADDRESS));
        rwBrowserNewPage = bind(linker, lookup, "rw_browser_new_page",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.ADDRESS));
        rwBrowserClose = bind(linker, lookup, "rw_browser_close",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS));
        rwBrowserWsEndpoint = bind(linker, lookup, "rw_browser_ws_endpoint",
                FunctionDescriptor.of(ValueLayout.ADDRESS, ValueLayout.ADDRESS));
        rwBrowserFree = bind(linker, lookup, "rw_browser_free",
                FunctionDescriptor.ofVoid(ValueLayout.ADDRESS));
        rwPageTargetId = bind(linker, lookup, "rw_page_target_id",
                FunctionDescriptor.of(ValueLayout.ADDRESS, ValueLayout.ADDRESS));
        rwPageGoto = bind(linker, lookup, "rw_page_goto",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.ADDRESS,
                        ValueLayout.ADDRESS, ValueLayout.JAVA_DOUBLE, ValueLayout.ADDRESS, ValueLayout.ADDRESS));
        rwPageClick = bind(linker, lookup, "rw_page_click",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.ADDRESS,
                        ValueLayout.JAVA_DOUBLE));
        rwPageFill = bind(linker, lookup, "rw_page_fill",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.ADDRESS,
                        ValueLayout.ADDRESS, ValueLayout.JAVA_DOUBLE));
        rwPageTitle = bind(linker, lookup, "rw_page_title",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.JAVA_DOUBLE,
                        ValueLayout.ADDRESS));
        rwPageTextContent = bind(linker, lookup, "rw_page_text_content",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.ADDRESS,
                        ValueLayout.JAVA_DOUBLE, ValueLayout.ADDRESS));
        rwPageEvaluate = bind(linker, lookup, "rw_page_evaluate",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.ADDRESS,
                        ValueLayout.ADDRESS, ValueLayout.JAVA_DOUBLE, ValueLayout.ADDRESS));
        rwPageScreenshot = bind(linker, lookup, "rw_page_screenshot",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.ADDRESS,
                        ValueLayout.ADDRESS, ValueLayout.ADDRESS));
        rwPageClose = bind(linker, lookup, "rw_page_close",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.JAVA_DOUBLE,
                        ValueLayout.JAVA_INT));
        rwPageFree = bind(linker, lookup, "rw_page_free",
                FunctionDescriptor.ofVoid(ValueLayout.ADDRESS));
        rwWireGraphParse = bind(linker, lookup, "rw_wire_graph_parse",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.ADDRESS));
        rwWireGraphFree = bind(linker, lookup, "rw_wire_graph_free",
                FunctionDescriptor.ofVoid(ValueLayout.ADDRESS));
        rwWireGraphNodeCount = bind(linker, lookup, "rw_wire_graph_node_count",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.ADDRESS));
        rwWireGraphRoot = bind(linker, lookup, "rw_wire_graph_root",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.ADDRESS));
        rwWireGraphNodeKind = bind(linker, lookup, "rw_wire_graph_node_kind",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T,
                        ValueLayout.ADDRESS));
        rwWireGraphGetBool = bind(linker, lookup, "rw_wire_graph_get_bool",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T,
                        ValueLayout.ADDRESS));
        rwWireGraphGetSigned = bind(linker, lookup, "rw_wire_graph_get_signed",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T,
                        ValueLayout.ADDRESS));
        rwWireGraphGetUnsigned = bind(linker, lookup, "rw_wire_graph_get_unsigned",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T,
                        ValueLayout.ADDRESS));
        rwWireGraphGetFloat = bind(linker, lookup, "rw_wire_graph_get_float",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T,
                        ValueLayout.ADDRESS));
        rwWireGraphGetString = bind(linker, lookup, "rw_wire_graph_get_string",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T,
                        ValueLayout.ADDRESS, ValueLayout.ADDRESS));
        rwWireGraphArrayLength = bind(linker, lookup, "rw_wire_graph_array_length",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T,
                        ValueLayout.ADDRESS));
        rwWireGraphArrayChild = bind(linker, lookup, "rw_wire_graph_array_child",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T, SIZE_T,
                        ValueLayout.ADDRESS));
        rwWireGraphObjectLength = bind(linker, lookup, "rw_wire_graph_object_length",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T,
                        ValueLayout.ADDRESS));
        rwWireGraphObjectKey = bind(linker, lookup, "rw_wire_graph_object_key",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T, SIZE_T,
                        ValueLayout.ADDRESS, ValueLayout.ADDRESS));
        rwWireGraphObjectChild = bind(linker, lookup, "rw_wire_graph_object_child",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T, SIZE_T,
                        ValueLayout.ADDRESS));
        rwWireGraphLeafKind = bind(linker, lookup, "rw_wire_graph_leaf_kind",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T,
                        ValueLayout.ADDRESS));
        rwWireGraphLeafFieldCount = bind(linker, lookup, "rw_wire_graph_leaf_field_count",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T,
                        ValueLayout.ADDRESS));
        rwWireGraphLeafField = bind(linker, lookup, "rw_wire_graph_leaf_field",
                FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.ADDRESS, SIZE_T, SIZE_T,
                        ValueLayout.ADDRESS, ValueLayout.ADDRESS));

    }

    Path libraryPath() {
        return libraryPath;
    }

    String chromiumExecutablePath() {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = pointerOut(arena);
            int status = invokeInt("rw_chromium_executable_path", rwChromiumExecutablePath, out);
            checkStatus(status, "rw_chromium_executable_path");
            MemorySegment path = out.get(ValueLayout.ADDRESS, 0);
            return takeNullableString(path);
        }
    }

    MemorySegment chromiumLaunch(String optionsJson) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment options = string(arena, optionsJson);
            MemorySegment out = pointerOut(arena);
            int status = invokeInt("rw_chromium_launch", rwChromiumLaunch, options, out);
            checkStatus(status, "rw_chromium_launch");
            return requireOutPointer(out, "rw_chromium_launch");
        }
    }

    MemorySegment browserNewPage(MemorySegment browser) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = pointerOut(arena);
            int status = invokeInt("rw_browser_new_page", rwBrowserNewPage, browser, out);
            checkStatus(status, "rw_browser_new_page");
            return requireOutPointer(out, "rw_browser_new_page");
        }
    }

    void browserClose(MemorySegment browser) {
        checkStatus(invokeInt("rw_browser_close", rwBrowserClose, browser), "rw_browser_close");
    }

    String browserWsEndpoint(MemorySegment browser) {
        MemorySegment value = invokeAddress("rw_browser_ws_endpoint", rwBrowserWsEndpoint, browser);
        if (isNull(value)) {
            throw nativeErrorNow("rw_browser_ws_endpoint returned NULL");
        }
        return takeNullableString(value);
    }

    void browserFree(MemorySegment browser) {
        invokeVoid("rw_browser_free", rwBrowserFree, browser);
    }

    String pageTargetId(MemorySegment page) {
        MemorySegment value = invokeAddress("rw_page_target_id", rwPageTargetId, page);
        if (isNull(value)) {
            throw nativeErrorNow("rw_page_target_id returned NULL");
        }
        return takeNullableString(value);
    }

    String pageGoto(MemorySegment page, String url, String waitUntil, double timeout, String referer) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment urlString = string(arena, url);
            MemorySegment waitString = nullableString(arena, waitUntil);
            MemorySegment refererString = nullableString(arena, referer);
            MemorySegment out = pointerOut(arena);
            int status = invokeInt("rw_page_goto", rwPageGoto, page, urlString, waitString,
                    timeout, refererString, out);
            checkStatus(status, "rw_page_goto");
            return takeNullableString(out.get(ValueLayout.ADDRESS, 0));
        }
    }

    void pageClick(MemorySegment page, String selector, double timeout) {
        try (Arena arena = Arena.ofConfined()) {
            int status = invokeInt("rw_page_click", rwPageClick, page, string(arena, selector), timeout);
            checkStatus(status, "rw_page_click");
        }
    }

    void pageFill(MemorySegment page, String selector, String value, double timeout) {
        try (Arena arena = Arena.ofConfined()) {
            int status = invokeInt("rw_page_fill", rwPageFill, page, string(arena, selector),
                    string(arena, value), timeout);
            checkStatus(status, "rw_page_fill");
        }
    }

    String pageTitle(MemorySegment page, double timeout) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = pointerOut(arena);
            int status = invokeInt("rw_page_title", rwPageTitle, page, timeout, out);
            checkStatus(status, "rw_page_title");
            MemorySegment value = requireOutPointer(out, "rw_page_title");
            return takeNullableString(value);
        }
    }

    String pageTextContent(MemorySegment page, String selector, double timeout) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = pointerOut(arena);
            int status = invokeInt("rw_page_text_content", rwPageTextContent, page,
                    string(arena, selector), timeout, out);
            checkStatus(status, "rw_page_text_content");
            return takeNullableString(out.get(ValueLayout.ADDRESS, 0));
        }
    }

    MemorySegment pageEvaluateGraph(
            MemorySegment page,
            String expression,
            String argumentJson,
            double timeout) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = pointerOut(arena);
            int status = invokeInt("rw_page_evaluate", rwPageEvaluate, page,
                    string(arena, expression), nullableString(arena, argumentJson), timeout, out);
            checkStatus(status, "rw_page_evaluate");
            MemorySegment wire = requireOutPointer(out, "rw_page_evaluate");
            try {
                return wireGraphParse(wire);
            } finally {
                invokeVoid("rw_string_free", rwStringFree, wire);
            }
        }
    }


    byte[] pageScreenshot(MemorySegment page, String optionsJson) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment outBuffer = pointerOut(arena);
            MemorySegment outLength = arena.allocate(SIZE_T);
            outLength.set(SIZE_T, 0, 0L);
            int status = invokeInt("rw_page_screenshot", rwPageScreenshot, page,
                    nullableString(arena, optionsJson), outBuffer, outLength);
            checkStatus(status, "rw_page_screenshot");

            MemorySegment buffer = outBuffer.get(ValueLayout.ADDRESS, 0);
            long length = outLength.get(SIZE_T, 0);
            try {
                if (length < 0 || length > Integer.MAX_VALUE) {
                    throw new RustwrightException("rw_page_screenshot returned invalid byte length: " + length);
                }
                if (isNull(buffer)) {
                    if (length != 0) {
                        throw new RustwrightException("rw_page_screenshot returned NULL with nonzero length " + length);
                    }
                    return new byte[0];
                }
                return buffer.reinterpret(length).toArray(ValueLayout.JAVA_BYTE);
            } finally {
                // The ABI transfers the exact pointer/length pair, including NULL/zero.
                bytesFree(buffer, length);
            }
        }
    }
    MemorySegment wireGraphParse(String wireJson) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = pointerOut(arena);
            int status = invokeInt("rw_wire_graph_parse", rwWireGraphParse,
                    string(arena, wireJson), out);
            checkStatus(status, "rw_wire_graph_parse");
            return requireOutPointer(out, "rw_wire_graph_parse");
        }
    }
    MemorySegment wireGraphParse(MemorySegment wireJson) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = pointerOut(arena);
            int status = invokeInt("rw_wire_graph_parse", rwWireGraphParse, wireJson, out);
            checkStatus(status, "rw_wire_graph_parse");
            return requireOutPointer(out, "rw_wire_graph_parse");
        }
    }


    void wireGraphFree(MemorySegment graph) {
        invokeVoid("rw_wire_graph_free", rwWireGraphFree, graph);
    }

    long wireGraphNodeCount(MemorySegment graph) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = sizeOut(arena);
            int status = invokeInt("rw_wire_graph_node_count", rwWireGraphNodeCount, graph, out);
            checkStatus(status, "rw_wire_graph_node_count");
            return out.get(SIZE_T, 0);
        }
    }

    long wireGraphRoot(MemorySegment graph) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = sizeOut(arena);
            int status = invokeInt("rw_wire_graph_root", rwWireGraphRoot, graph, out);
            checkStatus(status, "rw_wire_graph_root");
            return out.get(SIZE_T, 0);
        }
    }

    int wireGraphNodeKind(MemorySegment graph, long node) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = intOut(arena);
            int status = invokeInt("rw_wire_graph_node_kind", rwWireGraphNodeKind, graph, node, out);
            checkStatus(status, "rw_wire_graph_node_kind");
            return out.get(ValueLayout.JAVA_INT, 0);
        }
    }

    boolean wireGraphGetBool(MemorySegment graph, long node) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = intOut(arena);
            int status = invokeInt("rw_wire_graph_get_bool", rwWireGraphGetBool, graph, node, out);
            checkStatus(status, "rw_wire_graph_get_bool");
            return out.get(ValueLayout.JAVA_INT, 0) != 0;
        }
    }

    long wireGraphGetSigned(MemorySegment graph, long node) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = longOut(arena);
            int status = invokeInt("rw_wire_graph_get_signed", rwWireGraphGetSigned, graph, node, out);
            checkStatus(status, "rw_wire_graph_get_signed");
            return out.get(ValueLayout.JAVA_LONG, 0);
        }
    }

    long wireGraphGetUnsigned(MemorySegment graph, long node) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = longOut(arena);
            int status = invokeInt("rw_wire_graph_get_unsigned", rwWireGraphGetUnsigned, graph, node, out);
            checkStatus(status, "rw_wire_graph_get_unsigned");
            return out.get(ValueLayout.JAVA_LONG, 0);
        }
    }

    double wireGraphGetFloat(MemorySegment graph, long node) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = doubleOut(arena);
            int status = invokeInt("rw_wire_graph_get_float", rwWireGraphGetFloat, graph, node, out);
            checkStatus(status, "rw_wire_graph_get_float");
            return out.get(ValueLayout.JAVA_DOUBLE, 0);
        }
    }

    String wireGraphGetString(MemorySegment graph, long node) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment outData = pointerOut(arena);
            MemorySegment outLength = sizeOut(arena);
            int status = invokeInt("rw_wire_graph_get_string", rwWireGraphGetString, graph, node,
                    outData, outLength);
            checkStatus(status, "rw_wire_graph_get_string");
            return copyBorrowedUtf8(outData.get(ValueLayout.ADDRESS, 0), outLength.get(SIZE_T, 0));
        }
    }

    long wireGraphArrayLength(MemorySegment graph, long node) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = sizeOut(arena);
            int status = invokeInt("rw_wire_graph_array_length", rwWireGraphArrayLength, graph, node, out);
            checkStatus(status, "rw_wire_graph_array_length");
            return out.get(SIZE_T, 0);
        }
    }

    long wireGraphArrayChild(MemorySegment graph, long node, long index) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = sizeOut(arena);
            int status = invokeInt("rw_wire_graph_array_child", rwWireGraphArrayChild,
                    graph, node, index, out);
            checkStatus(status, "rw_wire_graph_array_child");
            return out.get(SIZE_T, 0);
        }
    }

    long wireGraphObjectLength(MemorySegment graph, long node) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = sizeOut(arena);
            int status = invokeInt("rw_wire_graph_object_length", rwWireGraphObjectLength, graph, node, out);
            checkStatus(status, "rw_wire_graph_object_length");
            return out.get(SIZE_T, 0);
        }
    }

    String wireGraphObjectKey(MemorySegment graph, long node, long index) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment outData = pointerOut(arena);
            MemorySegment outLength = sizeOut(arena);
            int status = invokeInt("rw_wire_graph_object_key", rwWireGraphObjectKey,
                    graph, node, index, outData, outLength);
            checkStatus(status, "rw_wire_graph_object_key");
            return copyBorrowedUtf8(outData.get(ValueLayout.ADDRESS, 0), outLength.get(SIZE_T, 0));
        }
    }

    long wireGraphObjectChild(MemorySegment graph, long node, long index) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = sizeOut(arena);
            int status = invokeInt("rw_wire_graph_object_child", rwWireGraphObjectChild,
                    graph, node, index, out);
            checkStatus(status, "rw_wire_graph_object_child");
            return out.get(SIZE_T, 0);
        }
    }

    int wireGraphLeafKind(MemorySegment graph, long node) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = intOut(arena);
            int status = invokeInt("rw_wire_graph_leaf_kind", rwWireGraphLeafKind, graph, node, out);
            checkStatus(status, "rw_wire_graph_leaf_kind");
            return out.get(ValueLayout.JAVA_INT, 0);
        }
    }

    long wireGraphLeafFieldCount(MemorySegment graph, long node) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = sizeOut(arena);
            int status = invokeInt("rw_wire_graph_leaf_field_count", rwWireGraphLeafFieldCount,
                    graph, node, out);
            checkStatus(status, "rw_wire_graph_leaf_field_count");
            return out.get(SIZE_T, 0);
        }
    }

    String wireGraphLeafField(MemorySegment graph, long node, long index) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment outData = pointerOut(arena);
            MemorySegment outLength = sizeOut(arena);
            int status = invokeInt("rw_wire_graph_leaf_field", rwWireGraphLeafField,
                    graph, node, index, outData, outLength);
            checkStatus(status, "rw_wire_graph_leaf_field");
            return copyBorrowedUtf8(outData.get(ValueLayout.ADDRESS, 0), outLength.get(SIZE_T, 0));
        }
    }


    void pageClose(MemorySegment page, double timeout, boolean runBeforeUnload) {
        int status = invokeInt("rw_page_close", rwPageClose, page, timeout, runBeforeUnload ? 1 : 0);
        checkStatus(status, "rw_page_close");
    }

    void pageFree(MemorySegment page) {
        invokeVoid("rw_page_free", rwPageFree, page);
    }

    private static MethodHandle bind(Linker linker, SymbolLookup lookup, String name,
            FunctionDescriptor descriptor) {
        MemorySegment symbol = lookup.find(name)
                .orElseThrow(() -> new RustwrightException("missing native symbol: " + name));
        return linker.downcallHandle(symbol, descriptor);
    }

    private static MemorySegment pointerOut(Arena arena) {
        MemorySegment out = arena.allocate(ValueLayout.ADDRESS);
        out.set(ValueLayout.ADDRESS, 0, MemorySegment.NULL);
        return out;
    }
    private static MemorySegment sizeOut(Arena arena) {
        MemorySegment out = arena.allocate(SIZE_T);
        out.set(SIZE_T, 0, 0L);
        return out;
    }

    private static MemorySegment intOut(Arena arena) {
        MemorySegment out = arena.allocate(ValueLayout.JAVA_INT);
        out.set(ValueLayout.JAVA_INT, 0, 0);
        return out;
    }

    private static MemorySegment longOut(Arena arena) {
        MemorySegment out = arena.allocate(ValueLayout.JAVA_LONG);
        out.set(ValueLayout.JAVA_LONG, 0, 0L);
        return out;
    }

    private static MemorySegment doubleOut(Arena arena) {
        MemorySegment out = arena.allocate(ValueLayout.JAVA_DOUBLE);
        out.set(ValueLayout.JAVA_DOUBLE, 0, 0.0d);
        return out;
    }

    private static String copyBorrowedUtf8(MemorySegment pointer, long length) {
        if (length < 0 || length > Integer.MAX_VALUE) {
            throw new RustwrightException("native returned invalid UTF-8 byte length: " + length);
        }
        if (length == 0) {
            return "";
        }
        if (isNull(pointer)) {
            throw new RustwrightException("native returned NULL UTF-8 data with nonzero length " + length);
        }
        byte[] bytes = pointer.reinterpret(length).toArray(ValueLayout.JAVA_BYTE);
        return new String(bytes, StandardCharsets.UTF_8);
    }


    private static MemorySegment nullableString(Arena arena, String value) {
        return value == null ? MemorySegment.NULL : string(arena, value);
    }

    private static MemorySegment string(Arena arena, String value) {
        if (value.indexOf('\0') >= 0) {
            throw new RustwrightException("strings passed to the C ABI cannot contain NUL");
        }
        return arena.allocateFrom(value);
    }

    private MemorySegment requireOutPointer(MemorySegment out, String operation) {
        MemorySegment pointer = out.get(ValueLayout.ADDRESS, 0);
        if (isNull(pointer)) {
            // The status was successful, so this is a binding/core invariant rather than rw_last_error.
            throw new RustwrightException(operation + " succeeded but returned NULL");
        }
        return pointer;
    }

    private String takeNullableString(MemorySegment pointer) {
        if (isNull(pointer)) {
            return null;
        }
        try {
            return pointer.reinterpret(MAX_C_STRING_BYTES).getString(0);
        } finally {
            invokeVoid("rw_string_free", rwStringFree, pointer);
        }
    }

    private void bytesFree(MemorySegment pointer, long length) {
        invokeVoid("rw_bytes_free", rwBytesFree, pointer, length);
    }

    private void checkStatus(int status, String operation) {
        if (status != 0) {
            // This must remain the very next ABI call; rw_last_error is thread-local and borrowed.
            throw nativeErrorNow(operation + " failed");
        }
    }

    private RustwrightException nativeErrorNow(String context) {
        MemorySegment errorPointer = invokeAddress("rw_last_error", rwLastError);
        String message = isNull(errorPointer)
                ? "native error (rw_last_error returned NULL)"
                : errorPointer.reinterpret(MAX_C_STRING_BYTES).getString(0);
        return new RustwrightException(context + ": " + message);
    }

    private int invokeInt(String operation, MethodHandle handle, Object... arguments) {
        try {
            return (int) handle.invokeWithArguments(arguments);
        } catch (Throwable error) {
            throw invocationFailure(operation, error);
        }
    }

    private MemorySegment invokeAddress(String operation, MethodHandle handle, Object... arguments) {
        try {
            return (MemorySegment) handle.invokeWithArguments(arguments);
        } catch (Throwable error) {
            throw invocationFailure(operation, error);
        }
    }

    private void invokeVoid(String operation, MethodHandle handle, Object... arguments) {
        try {
            handle.invokeWithArguments(arguments);
        } catch (Throwable error) {
            throw invocationFailure(operation, error);
        }
    }

    private static RustwrightException invocationFailure(String operation, Throwable error) {
        if (error instanceof RustwrightException rustwright) {
            return rustwright;
        }
        return new RustwrightException(operation + " native invocation failed: " + error.getMessage(), error);
    }

    private static boolean isNull(MemorySegment pointer) {
        return pointer == null || pointer.address() == 0;
    }
}
