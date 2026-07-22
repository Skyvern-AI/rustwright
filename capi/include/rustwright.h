#ifndef RUSTWRIGHT_H
#define RUSTWRIGHT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Opaque browser handle owned by the caller. */
typedef struct RwBrowser RwBrowser;

/** Opaque page handle owned by the caller. */
typedef struct RwPage RwPage;

/**
 * Return the current thread's last error as borrowed UTF-8.
 *
 * The pointer is NULL when no error is recorded. It remains valid until the
 * next Rustwright ABI call on this thread. Never free this pointer.
 */
const char *rw_last_error(void);

/** Free a UTF-8 string returned by a Rustwright function. NULL is accepted. */
void rw_string_free(char *s);

/**
 * Free a byte buffer returned by rw_page_screenshot.
 *
 * Pass the exact pointer and length returned by that call. NULL is accepted.
 */
void rw_bytes_free(uint8_t *buf, size_t len);

/**
 * Decode the core evaluate wire format into plain caller-owned JSON UTF-8.
 *
 * Array and object wrappers are removed, repeated non-cyclic references are
 * duplicated, and references that form cycles become
 * `{"__rustwright_cdp_cycle__": true}`. Leaf scalar tags are preserved for
 * binding-specific native-value mapping. On success, free `*out_json` with
 * rw_string_free. On failure, `*out_json` is NULL and rw_last_error describes
 * the error.
 */
int32_t rw_decode_wire(const char *wire_json, char **out_json);

/**
 * Discover Chromium and return its executable path.
 *
 * On success, `*out_path` is a caller-owned UTF-8 string, or NULL when no
 * executable is discoverable. Free non-NULL values with rw_string_free.
 */
int32_t rw_chromium_executable_path(char **out_path);

/**
 * Launch Chromium from a UTF-8 JSON object containing launch options.
 *
 * The JSON shape matches the Node LaunchOptions wire format (snake_case core
 * fields such as `headless`, `executable_path`, and `user_data_dir`). On
 * success, `*out_browser` must eventually be closed and freed.
 */
int32_t rw_chromium_launch(const char *options_json, RwBrowser **out_browser);

/** Create a fresh page. The returned handle must be freed with rw_page_free. */
int32_t rw_browser_new_page(RwBrowser *b, RwPage **out_page);

/** Close Chromium and its pages. The handle remains valid until freed. */
int32_t rw_browser_close(RwBrowser *b);

/**
 * Return the browser's WebSocket endpoint as caller-owned UTF-8.
 *
 * Returns NULL on an invalid handle, allocation failure, or panic. Inspect
 * rw_last_error for details. Free a non-NULL result with rw_string_free.
 */
char *rw_browser_ws_endpoint(RwBrowser *b);

/** Drop a browser handle. This does not replace rw_browser_close. NULL is accepted. */
void rw_browser_free(RwBrowser *b);

/**
 * Return the page target id as caller-owned UTF-8.
 *
 * Returns NULL on failure. Free a non-NULL result with rw_string_free.
 */
char *rw_page_target_id(RwPage *p);

/**
 * Navigate and return the response payload as caller-owned JSON UTF-8.
 *
 * `wait_until` and `referer` may be NULL. For every timeout argument in this
 * API, NAN means no explicit timeout; any other double is milliseconds.
 */
int32_t rw_page_goto(RwPage *p,
                     const char *url,
                     const char *wait_until,
                     double timeout_ms_or_nan,
                     const char *referer,
                     char **out_response_json);

/** Click the first element matching `selector`. */
int32_t rw_page_click(RwPage *p, const char *selector, double timeout_ms_or_nan);

/** Fill the first element matching `selector` with UTF-8 `value`. */
int32_t rw_page_fill(RwPage *p,
                     const char *selector,
                     const char *value,
                     double timeout_ms_or_nan);

/** Return the document title as caller-owned UTF-8. */
int32_t rw_page_title(RwPage *p, double timeout_ms_or_nan, char **out_title);

/**
 * Return textContent as caller-owned UTF-8.
 *
 * On success, `*out_text` is NULL when JavaScript returned null. Free a
 * non-NULL result with rw_string_free.
 */
int32_t rw_page_text_content(RwPage *p,
                             const char *selector,
                             double timeout_ms_or_nan,
                             char **out_text);

/**
 * Evaluate JavaScript and return the core's serialized JSON wire value.
 *
 * `arg_json` may be NULL or must contain one JSON value. The result is
 * caller-owned and must be freed with rw_string_free.
 */
int32_t rw_page_evaluate(RwPage *p,
                         const char *expression,
                         const char *arg_json,
                         double timeout_ms_or_nan,
                         char **out_json);

/**
 * Capture a screenshot and return caller-owned bytes.
 *
 * `options_json` may be NULL or a Node ScreenshotOptions-shaped object with
 * `path`, `fullPage`, `clip`, `timeout`, `type`, `quality`, and
 * `omitBackground`. Free the exact returned pointer/length pair with
 * rw_bytes_free. Empty output is represented by NULL and length zero.
 */
int32_t rw_page_screenshot(RwPage *p,
                           const char *options_json,
                           uint8_t **out_buf,
                           size_t *out_len);

/** Close the page. Any nonzero `run_before_unload` value is true. */
int32_t rw_page_close(RwPage *p,
                      double timeout_ms_or_nan,
                      int run_before_unload);

/** Drop a page handle. This does not replace rw_page_close. NULL is accepted. */
void rw_page_free(RwPage *p);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* RUSTWRIGHT_H */
