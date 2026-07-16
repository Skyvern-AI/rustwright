package com.skyvern.rustwright;

import java.lang.foreign.MemorySegment;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Set;

/** An owned RwBrowser handle. close() closes pages and frees every native handle once. */
public final class Browser implements AutoCloseable {
    private final NativeBindings bindings;
    private final Object lock = new Object();
    private final Set<Page> pages = Collections.newSetFromMap(new IdentityHashMap<>());
    private MemorySegment handle;
    private boolean freed;

    Browser(NativeBindings bindings, MemorySegment handle) {
        this.bindings = bindings;
        this.handle = handle;
    }

    public Page newPage() {
        synchronized (lock) {
            requireOpen();
            Page page = new Page(this, bindings, bindings.browserNewPage(handle), lock);
            pages.add(page);
            return page;
        }
    }

    public String wsEndpoint() {
        synchronized (lock) {
            requireOpen();
            return bindings.browserWsEndpoint(handle);
        }
    }

    @Override
    public void close() {
        synchronized (lock) {
            if (freed) {
                return;
            }

            RuntimeException failure = null;
            for (Page page : new ArrayList<>(pages)) {
                try {
                    page.close();
                } catch (RuntimeException error) {
                    failure = combine(failure, error);
                }
            }
            try {
                bindings.browserClose(handle);
            } catch (RuntimeException error) {
                failure = combine(failure, error);
            }
            try {
                bindings.browserFree(handle);
            } catch (RuntimeException error) {
                failure = combine(failure, error);
            } finally {
                handle = MemorySegment.NULL;
                freed = true;
            }
            if (failure != null) {
                throw failure;
            }
        }
    }

    void pageFreed(Page page) {
        // Called with the shared reentrant monitor held by Page.close().
        pages.remove(page);
    }

    private void requireOpen() {
        if (freed) {
            throw new IllegalStateException("browser is closed");
        }
    }

    private static RuntimeException combine(RuntimeException first, RuntimeException next) {
        if (first == null) {
            return next;
        }
        first.addSuppressed(next);
        return first;
    }
}
