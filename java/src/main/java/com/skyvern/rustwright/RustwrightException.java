package com.skyvern.rustwright;

/** Thrown when the Rustwright C ABI or a binding invariant reports an error. */
public final class RustwrightException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public RustwrightException(String message) {
        super(message);
    }

    public RustwrightException(String message, Throwable cause) {
        super(message, cause);
    }
}
