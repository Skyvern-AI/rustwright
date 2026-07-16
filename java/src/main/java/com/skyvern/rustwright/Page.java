package com.skyvern.rustwright;

import java.lang.foreign.MemorySegment;
import java.util.Map;
import java.util.Objects;

/** An owned RwPage handle. Java spells the reserved JavaScript name goto as goTo. */
public final class Page implements AutoCloseable {
    private final Browser owner;
    private final NativeBindings bindings;
    private final Object lock;
    private MemorySegment handle;
    private boolean freed;

    Page(Browser owner, NativeBindings bindings, MemorySegment handle, Object lock) {
        this.owner = owner;
        this.bindings = bindings;
        this.handle = handle;
        this.lock = lock;
    }

    public String targetId() {
        synchronized (lock) {
            requireOpen();
            return bindings.pageTargetId(handle);
        }
    }

    public Object goTo(String url) {
        return goTo(url, null, null, null);
    }

    public Object goTo(String url, String waitUntil) {
        return goTo(url, waitUntil, null, null);
    }

    public Object goTo(String url, String waitUntil, Double timeoutMs, String referer) {
        Objects.requireNonNull(url, "url");
        synchronized (lock) {
            requireOpen();
            String response = bindings.pageGoto(handle, url, waitUntil, timeout(timeoutMs), referer);
            return response == null ? null : Json.parse(response);
        }
    }

    public void click(String selector) {
        click(selector, null);
    }

    public void click(String selector, Double timeoutMs) {
        Objects.requireNonNull(selector, "selector");
        synchronized (lock) {
            requireOpen();
            bindings.pageClick(handle, selector, timeout(timeoutMs));
        }
    }

    public void fill(String selector, String value) {
        fill(selector, value, null);
    }

    public void fill(String selector, String value, Double timeoutMs) {
        Objects.requireNonNull(selector, "selector");
        Objects.requireNonNull(value, "value");
        synchronized (lock) {
            requireOpen();
            bindings.pageFill(handle, selector, value, timeout(timeoutMs));
        }
    }

    public String title() {
        return title(null);
    }

    public String title(Double timeoutMs) {
        synchronized (lock) {
            requireOpen();
            return bindings.pageTitle(handle, timeout(timeoutMs));
        }
    }

    public String textContent(String selector) {
        return textContent(selector, null);
    }

    public String textContent(String selector, Double timeoutMs) {
        Objects.requireNonNull(selector, "selector");
        synchronized (lock) {
            requireOpen();
            return bindings.pageTextContent(handle, selector, timeout(timeoutMs));
        }
    }

    public Object evaluate(String expression) {
        return evaluateInternal(expression, null, null);
    }

    public Object evaluate(String expression, Object argument) {
        return evaluateInternal(expression, Json.stringify(argument), null);
    }

    public Object evaluate(String expression, Object argument, Double timeoutMs) {
        return evaluateInternal(expression, Json.stringify(argument), timeoutMs);
    }

    private Object evaluateInternal(String expression, String argumentJson, Double timeoutMs) {
        Objects.requireNonNull(expression, "expression");
        synchronized (lock) {
            requireOpen();
            String wireJson = bindings.pageEvaluate(handle, expression, argumentJson, timeout(timeoutMs));
            return WireValueDecoder.decodeJson(wireJson);
        }
    }

    public byte[] screenshot() {
        synchronized (lock) {
            requireOpen();
            return bindings.pageScreenshot(handle, null);
        }
    }

    public byte[] screenshot(Map<String, ?> options) {
        if (options == null) {
            return screenshot();
        }
        String optionsJson = Json.stringify(Options.screenshot(options));
        synchronized (lock) {
            requireOpen();
            return bindings.pageScreenshot(handle, optionsJson);
        }
    }

    @Override
    public void close() {
        close(null, false);
    }

    public void close(Double timeoutMs, boolean runBeforeUnload) {
        synchronized (lock) {
            if (freed) {
                return;
            }
            RuntimeException failure = null;
            try {
                bindings.pageClose(handle, timeout(timeoutMs), runBeforeUnload);
            } catch (RuntimeException error) {
                failure = error;
            }
            try {
                bindings.pageFree(handle);
            } catch (RuntimeException error) {
                if (failure == null) {
                    failure = error;
                } else {
                    failure.addSuppressed(error);
                }
            } finally {
                handle = MemorySegment.NULL;
                freed = true;
                owner.pageFreed(this);
            }
            if (failure != null) {
                throw failure;
            }
        }
    }

    private void requireOpen() {
        if (freed) {
            throw new IllegalStateException("page is closed");
        }
    }

    private static double timeout(Double timeoutMs) {
        return timeoutMs == null ? Double.NaN : timeoutMs;
    }
}
