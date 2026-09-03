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

/** Opaque immutable parsed wire graph owned by the caller. */
typedef struct RwWireGraph RwWireGraph;

/** Dense node ids are indices into the immutable graph. */
typedef size_t RwWireNodeId;

/*
 * These tags use a fixed-width integer type. C enum storage is implementation
 * defined and may not match the Rust ABI when passed through a pointer.
 */
typedef int32_t RwWireNodeKind;
#define RW_WIRE_NODE_NULL 0
#define RW_WIRE_NODE_BOOL 1
#define RW_WIRE_NODE_SIGNED 2
#define RW_WIRE_NODE_UNSIGNED 3
#define RW_WIRE_NODE_FLOAT 4
#define RW_WIRE_NODE_STRING 5
#define RW_WIRE_NODE_ARRAY 6
#define RW_WIRE_NODE_OBJECT 7
#define RW_WIRE_NODE_LEAF 8

typedef int32_t RwWireLeafKind;
#define RW_WIRE_LEAF_UNSERIALIZABLE 0
#define RW_WIRE_LEAF_BIGINT 1
#define RW_WIRE_LEAF_DATE 2
#define RW_WIRE_LEAF_REGEXP 3
#define RW_WIRE_LEAF_URL 4
#define RW_WIRE_LEAF_ERROR 5
#define RW_WIRE_LEAF_UNDEFINED 6
#define RW_WIRE_LEAF_SYMBOL 7
#define RW_WIRE_LEAF_FUNCTION 8

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
 * Parse the evaluate wire format into an immutable graph.
 *
 * The graph owns all nodes and strings. On success, free it with
 * rw_wire_graph_free. On failure, `*out_graph` is NULL.
 */
int32_t rw_wire_graph_parse(const char *wire_json, RwWireGraph **out_graph);

/** Release a graph and all borrowed views into it. NULL is accepted. */
void rw_wire_graph_free(RwWireGraph *graph);

/** Return the dense number of nodes in a graph. */
int32_t rw_wire_graph_node_count(const RwWireGraph *graph, size_t *out_count);

/** Return the graph root's dense node id. */
int32_t rw_wire_graph_root(const RwWireGraph *graph, RwWireNodeId *out_root);

/** Return a node's kind. */
int32_t rw_wire_graph_node_kind(const RwWireGraph *graph,
                                RwWireNodeId node,
                                RwWireNodeKind *out_kind);

/** Read a boolean node as 0 or 1. */
int32_t rw_wire_graph_get_bool(const RwWireGraph *graph,
                               RwWireNodeId node,
                               int32_t *out_value);

/** Read a signed integer node. */
int32_t rw_wire_graph_get_signed(const RwWireGraph *graph,
                                 RwWireNodeId node,
                                 int64_t *out_value);

/** Read an unsigned integer node. */
int32_t rw_wire_graph_get_unsigned(const RwWireGraph *graph,
                                   RwWireNodeId node,
                                   uint64_t *out_value);

/** Read a floating-point node. */
int32_t rw_wire_graph_get_float(const RwWireGraph *graph,
                                RwWireNodeId node,
                                double *out_value);

/**
 * Read a string node as a borrowed byte view.
 *
 * The view includes embedded NUL bytes and remains valid until graph free.
 * Empty strings return NULL and zero.
 */
int32_t rw_wire_graph_get_string(const RwWireGraph *graph,
                                 RwWireNodeId node,
                                 const uint8_t **out_data,
                                 size_t *out_len);

/** Return an array node's length. */
int32_t rw_wire_graph_array_length(const RwWireGraph *graph,
                                   RwWireNodeId node,
                                   size_t *out_len);

/** Return an array child node id in native order. */
int32_t rw_wire_graph_array_child(const RwWireGraph *graph,
                                  RwWireNodeId node,
                                  size_t index,
                                  RwWireNodeId *out_child);

/** Return an object node's entry count. */
int32_t rw_wire_graph_object_length(const RwWireGraph *graph,
                                    RwWireNodeId node,
                                    size_t *out_len);

/** Return an object key as a borrowed byte view in native order. */
int32_t rw_wire_graph_object_key(const RwWireGraph *graph,
                                 RwWireNodeId node,
                                 size_t index,
                                 const uint8_t **out_data,
                                 size_t *out_len);

/** Return an object child node id in native order. */
int32_t rw_wire_graph_object_child(const RwWireGraph *graph,
                                   RwWireNodeId node,
                                   size_t index,
                                   RwWireNodeId *out_child);

/** Return a leaf's canonical kind. */
int32_t rw_wire_graph_leaf_kind(const RwWireGraph *graph,
                                RwWireNodeId node,
                                RwWireLeafKind *out_kind);

/**
 * Return the positional field count for a leaf.
 *
 * Unserializable, bigint, date, and URL have one field; regexp has two;
 * error has three; undefined, symbol, and function have zero.
 */
int32_t rw_wire_graph_leaf_field_count(const RwWireGraph *graph,
                                       RwWireNodeId node,
                                       size_t *out_count);

/** Return one positional leaf field as a borrowed byte view. */
int32_t rw_wire_graph_leaf_field(const RwWireGraph *graph,
                                 RwWireNodeId node,
                                 size_t index,
                                 const uint8_t **out_data,
                                 size_t *out_len);

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
 * Set this page's general default timeout in milliseconds.
 *
 * NAN clears the slot. General calls resolve an omitted timeout from the page
 * slot, then the context slot, then the core's 30 second command default.
 */
int32_t rw_page_set_default_timeout(RwPage *p, double timeout_ms_or_nan);

/** Set or clear (with NAN) this page's navigation default timeout. */
int32_t rw_page_set_default_navigation_timeout(RwPage *p,
                                               double timeout_ms_or_nan);

/** Set or clear (with NAN) the inherited context general slot for this page. */
int32_t rw_page_set_context_default_timeout(RwPage *p,
                                            double timeout_ms_or_nan);

/**
 * Set or clear (with NAN) the inherited context navigation slot for this page.
 *
 * Navigation calls resolve an omitted timeout from page navigation, context
 * navigation, page general, context general, then the 30 second core default.
 */
int32_t rw_page_set_context_default_navigation_timeout(
    RwPage *p,
    double timeout_ms_or_nan);

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
 * `arg_json` may be NULL or must contain one JSON value. It is passed as the
 * sole argument when `expression` evaluates to a function and ignored
 * otherwise. The result is caller-owned and must be freed with rw_string_free.
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
