package com.skyvern.rustwright;

import java.lang.foreign.Arena;
import java.lang.foreign.FunctionDescriptor;
import java.lang.foreign.Linker;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.SymbolLookup;
import java.lang.foreign.ValueLayout;
import java.lang.invoke.MethodHandle;
import java.nio.file.Path;
import java.util.Objects;

/** Exact Java FFM declarations and ownership helpers for all 19 rw_* symbols. */
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

    String pageEvaluate(MemorySegment page, String expression, String argumentJson, double timeout) {
        try (Arena arena = Arena.ofConfined()) {
            MemorySegment out = pointerOut(arena);
            int status = invokeInt("rw_page_evaluate", rwPageEvaluate, page,
                    string(arena, expression), nullableString(arena, argumentJson), timeout, out);
            checkStatus(status, "rw_page_evaluate");
            MemorySegment value = requireOutPointer(out, "rw_page_evaluate");
            return takeNullableString(value);
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
