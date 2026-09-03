//! Stable C ABI for the Rustwright core.
//!
//! The hand-written `capi/include/rustwright.h` header is the public contract.

use rustwright_core as rw;
use serde::Deserialize;
use serde_json::Value;
use std::cell::RefCell;
use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_double, c_int};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::ptr;

/// Opaque browser handle. Its layout is intentionally not part of the ABI.
pub struct RwBrowser {
    inner: rw::RustwrightBrowser,
}

/// Opaque page handle. Its layout is intentionally not part of the ABI.
pub struct RwPage {
    inner: rw::RustwrightPage,
}
/// Opaque immutable parsed wire graph. Its layout is intentionally not part of the ABI.
pub struct RwWireGraph {
    inner: rw::WireGraph,
}

/// Dense node ids are indices into an immutable wire graph.
pub type RwWireNodeId = usize;

#[repr(i32)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RwWireNodeKind {
    Null = 0,
    Bool = 1,
    Signed = 2,
    Unsigned = 3,
    Float = 4,
    String = 5,
    Array = 6,
    Object = 7,
    Leaf = 8,
}

impl From<RwWireNodeKind> for i32 {
    fn from(kind: RwWireNodeKind) -> Self {
        match kind {
            RwWireNodeKind::Null => 0,
            RwWireNodeKind::Bool => 1,
            RwWireNodeKind::Signed => 2,
            RwWireNodeKind::Unsigned => 3,
            RwWireNodeKind::Float => 4,
            RwWireNodeKind::String => 5,
            RwWireNodeKind::Array => 6,
            RwWireNodeKind::Object => 7,
            RwWireNodeKind::Leaf => 8,
        }
    }
}

impl Default for RwWireNodeKind {
    fn default() -> Self {
        Self::Null
    }
}

#[repr(i32)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RwWireLeafKind {
    Unserializable = 0,
    BigInt = 1,
    Date = 2,
    RegExp = 3,
    Url = 4,
    Error = 5,
    Undefined = 6,
    Symbol = 7,
    Function = 8,
}

impl From<RwWireLeafKind> for i32 {
    fn from(kind: RwWireLeafKind) -> Self {
        match kind {
            RwWireLeafKind::Unserializable => 0,
            RwWireLeafKind::BigInt => 1,
            RwWireLeafKind::Date => 2,
            RwWireLeafKind::RegExp => 3,
            RwWireLeafKind::Url => 4,
            RwWireLeafKind::Error => 5,
            RwWireLeafKind::Undefined => 6,
            RwWireLeafKind::Symbol => 7,
            RwWireLeafKind::Function => 8,
        }
    }
}

impl Default for RwWireLeafKind {
    fn default() -> Self {
        Self::Unserializable
    }
}

thread_local! {
    static LAST_ERROR: RefCell<Option<CString>> = const { RefCell::new(None) };
}

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ScreenshotOptions {
    path: Option<String>,
    full_page: Option<bool>,
    clip: Option<Value>,
    timeout: Option<f64>,
    #[serde(rename = "type")]
    image_type: Option<String>,
    quality: Option<u32>,
    omit_background: Option<bool>,
}

fn clear_error() {
    LAST_ERROR.with(|slot| *slot.borrow_mut() = None);
}

fn set_error(message: impl ToString) {
    let message = message.to_string().replace('\0', "\\0");
    let value = CString::new(message)
        .unwrap_or_else(|_| CString::new("Rustwright error contained an interior NUL").unwrap());
    LAST_ERROR.with(|slot| *slot.borrow_mut() = Some(value));
}

fn record_error(message: impl ToString) {
    let _ = catch_unwind(AssertUnwindSafe(|| set_error(message)));
}

fn panic_message(payload: Box<dyn std::any::Any + Send>) -> String {
    if let Some(message) = payload.downcast_ref::<&str>() {
        (*message).to_string()
    } else if let Some(message) = payload.downcast_ref::<String>() {
        message.clone()
    } else {
        "unknown panic payload".to_string()
    }
}

fn ffi_status(operation: impl FnOnce() -> Result<(), String>) -> c_int {
    match catch_unwind(AssertUnwindSafe(|| {
        clear_error();
        operation()
    })) {
        Ok(Ok(())) => 0,
        Ok(Err(error)) => {
            record_error(error);
            1
        }
        Err(payload) => {
            record_error(format!(
                "panic at Rustwright C ABI boundary: {}",
                panic_message(payload)
            ));
            2
        }
    }
}

fn ffi_pointer(operation: impl FnOnce() -> Result<*mut c_char, String>) -> *mut c_char {
    match catch_unwind(AssertUnwindSafe(|| {
        clear_error();
        operation()
    })) {
        Ok(Ok(value)) => value,
        Ok(Err(error)) => {
            record_error(error);
            ptr::null_mut()
        }
        Err(payload) => {
            record_error(format!(
                "panic at Rustwright C ABI boundary: {}",
                panic_message(payload)
            ));
            ptr::null_mut()
        }
    }
}

unsafe fn required_str<'a>(value: *const c_char, name: &str) -> Result<&'a str, String> {
    if value.is_null() {
        return Err(format!("{name} must not be NULL"));
    }
    // SAFETY: The public ABI requires a live NUL-terminated pointer. The
    // caller retains it for the duration of this synchronous call.
    unsafe { CStr::from_ptr(value) }
        .to_str()
        .map_err(|_| format!("{name} must be valid UTF-8"))
}

unsafe fn optional_str<'a>(value: *const c_char, name: &str) -> Result<Option<&'a str>, String> {
    if value.is_null() {
        Ok(None)
    } else {
        // SAFETY: Delegates to the same pointer contract as required_str.
        unsafe { required_str(value, name) }.map(Some)
    }
}

fn owned_string(value: String) -> Result<*mut c_char, String> {
    CString::new(value)
        .map(CString::into_raw)
        .map_err(|_| "Rustwright produced a string containing an interior NUL".to_string())
}

fn timeout(value: c_double) -> Option<f64> {
    (!value.is_nan()).then_some(value)
}

unsafe fn browser_ref<'a>(browser: *mut RwBrowser) -> Result<&'a RwBrowser, String> {
    // SAFETY: The caller owns a live handle created by rw_chromium_launch.
    unsafe { browser.as_ref() }.ok_or_else(|| "browser handle must not be NULL".to_string())
}

unsafe fn page_ref<'a>(page: *mut RwPage) -> Result<&'a RwPage, String> {
    // SAFETY: The caller owns a live handle created by rw_browser_new_page.
    unsafe { page.as_ref() }.ok_or_else(|| "page handle must not be NULL".to_string())
}

fn init_output<T: Copy>(output: *mut T, value: T, name: &str) -> Result<(), String> {
    if !output.is_null() {
        // SAFETY: A non-NULL output slot is caller-provided writable storage.
        unsafe { *output = value };
    } else {
        return Err(format!("{name} must not be NULL"));
    }
    Ok(())
}

fn init_bytes_outputs(out_data: *mut *const u8, out_len: *mut usize) -> Result<(), String> {
    if !out_data.is_null() {
        // SAFETY: A non-NULL output slot is caller-provided writable storage.
        unsafe { *out_data = ptr::null() };
    }
    if !out_len.is_null() {
        // SAFETY: A non-NULL output slot is caller-provided writable storage.
        unsafe { *out_len = 0 };
    }
    if out_data.is_null() {
        return Err("out_data must not be NULL".to_string());
    }
    if out_len.is_null() {
        return Err("out_len must not be NULL".to_string());
    }
    Ok(())
}

unsafe fn wire_graph_ref<'a>(graph: *const RwWireGraph) -> Result<&'a RwWireGraph, String> {
    // SAFETY: The caller owns a live handle created by rw_wire_graph_parse.
    unsafe { graph.as_ref() }.ok_or_else(|| "graph handle must not be NULL".to_string())
}

fn wire_node<'a>(
    graph: &'a RwWireGraph,
    node: RwWireNodeId,
) -> Result<&'a rw::WireNodeKind, String> {
    graph
        .inner
        .node(rw::WireNodeId::from_index(node))
        .ok_or_else(|| format!("wire graph node id {node} is out of range"))
}

fn wire_node_kind(kind: &rw::WireNodeKind) -> RwWireNodeKind {
    match kind {
        rw::WireNodeKind::Null => RwWireNodeKind::Null,
        rw::WireNodeKind::Bool(_) => RwWireNodeKind::Bool,
        rw::WireNodeKind::Number(rw::WireNumber::Signed(_)) => RwWireNodeKind::Signed,
        rw::WireNodeKind::Number(rw::WireNumber::Unsigned(_)) => RwWireNodeKind::Unsigned,
        rw::WireNodeKind::Number(rw::WireNumber::Float(_)) => RwWireNodeKind::Float,
        rw::WireNodeKind::String(_) => RwWireNodeKind::String,
        rw::WireNodeKind::Array(_) => RwWireNodeKind::Array,
        rw::WireNodeKind::Object(_) => RwWireNodeKind::Object,
        rw::WireNodeKind::Leaf(_) => RwWireNodeKind::Leaf,
    }
}

fn wire_leaf_kind(leaf: &rw::WireLeaf) -> RwWireLeafKind {
    match leaf {
        rw::WireLeaf::Unserializable(_) => RwWireLeafKind::Unserializable,
        rw::WireLeaf::BigInt(_) => RwWireLeafKind::BigInt,
        rw::WireLeaf::Date(_) => RwWireLeafKind::Date,
        rw::WireLeaf::RegExp { .. } => RwWireLeafKind::RegExp,
        rw::WireLeaf::Url(_) => RwWireLeafKind::Url,
        rw::WireLeaf::Error { .. } => RwWireLeafKind::Error,
        rw::WireLeaf::Undefined => RwWireLeafKind::Undefined,
        rw::WireLeaf::Symbol => RwWireLeafKind::Symbol,
        rw::WireLeaf::Function => RwWireLeafKind::Function,
    }
}

fn borrowed_bytes(value: &str) -> (*const u8, usize) {
    if value.is_empty() {
        (ptr::null(), 0)
    } else {
        (value.as_ptr(), value.len())
    }
}

/// Returns the current thread's borrowed last-error message, or NULL.
#[no_mangle]
pub extern "C" fn rw_last_error() -> *const c_char {
    match catch_unwind(AssertUnwindSafe(|| {
        LAST_ERROR.with(|slot| {
            slot.borrow()
                .as_ref()
                .map_or(ptr::null(), |message| message.as_ptr())
        })
    })) {
        Ok(value) => value,
        Err(payload) => {
            record_error(format!(
                "panic at Rustwright C ABI boundary: {}",
                panic_message(payload)
            ));
            catch_unwind(AssertUnwindSafe(|| {
                LAST_ERROR.with(|slot| {
                    slot.borrow()
                        .as_ref()
                        .map_or(ptr::null(), |message| message.as_ptr())
                })
            }))
            .unwrap_or(ptr::null())
        }
    }
}

/// Frees a string returned by this library.
#[no_mangle]
pub unsafe extern "C" fn rw_string_free(value: *mut c_char) {
    let result = catch_unwind(AssertUnwindSafe(|| {
        clear_error();
        if !value.is_null() {
            // SAFETY: `value` came from CString::into_raw in this library and
            // has not previously been freed.
            drop(unsafe { CString::from_raw(value) });
        }
    }));
    if let Err(payload) = result {
        record_error(format!(
            "panic at Rustwright C ABI boundary: {}",
            panic_message(payload)
        ));
    }
}

/// Frees a byte buffer returned by this library.
#[no_mangle]
pub unsafe extern "C" fn rw_bytes_free(buffer: *mut u8, len: usize) {
    let result = catch_unwind(AssertUnwindSafe(|| {
        clear_error();
        if !buffer.is_null() {
            let slice = ptr::slice_from_raw_parts_mut(buffer, len);
            // SAFETY: Screenshot buffers are exported as Box<[u8]> with this
            // exact pointer and length and have not previously been freed.
            drop(unsafe { Box::<[u8]>::from_raw(slice) });
        }
    }));
    if let Err(payload) = result {
        record_error(format!(
            "panic at Rustwright C ABI boundary: {}",
            panic_message(payload)
        ));
    }
}

/// Decodes the core evaluate wire format into caller-owned plain JSON.
#[no_mangle]
pub unsafe extern "C" fn rw_decode_wire(
    wire_json: *const c_char,
    out_json: *mut *mut c_char,
) -> c_int {
    ffi_status(|| {
        if out_json.is_null() {
            return Err("out_json must not be NULL".to_string());
        }
        // SAFETY: Validated above; initialize before any fallible work.
        unsafe { *out_json = ptr::null_mut() };
        let wire_json = unsafe { required_str(wire_json, "wire_json")? };
        let decoded = rw::decode_wire_value(wire_json).map_err(|error| error.to_string())?;
        // SAFETY: Validated above.
        unsafe { *out_json = owned_string(decoded)? };
        Ok(())
    })
}

/// Parse the evaluate wire format into an immutable graph owned by the caller.
#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_parse(
    wire_json: *const c_char,
    out_graph: *mut *mut RwWireGraph,
) -> c_int {
    ffi_status(|| {
        if !out_graph.is_null() {
            // SAFETY: A non-NULL output slot is caller-provided writable storage.
            unsafe { *out_graph = ptr::null_mut() };
        }
        if out_graph.is_null() {
            return Err("out_graph must not be NULL".to_string());
        }
        let wire_json = unsafe { required_str(wire_json, "wire_json")? };
        let graph = rw::parse_wire_graph(wire_json).map_err(|error| error.to_string())?;
        // SAFETY: `out_graph` was validated above.
        unsafe {
            *out_graph = Box::into_raw(Box::new(RwWireGraph { inner: graph }));
        }
        Ok(())
    })
}

/// Release an immutable wire graph. NULL is accepted.
#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_free(graph: *mut RwWireGraph) {
    let result = catch_unwind(AssertUnwindSafe(|| {
        clear_error();
        if !graph.is_null() {
            // SAFETY: The handle came from Box::into_raw in this library and
            // has not previously been freed.
            drop(unsafe { Box::from_raw(graph) });
        }
    }));
    if let Err(payload) = result {
        record_error(format!(
            "panic at Rustwright C ABI boundary: {}",
            panic_message(payload)
        ));
    }
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_node_count(
    graph: *const RwWireGraph,
    out_count: *mut usize,
) -> c_int {
    ffi_status(|| {
        init_output(out_count, 0, "out_count")?;
        let graph = unsafe { wire_graph_ref(graph)? };
        // SAFETY: `out_count` was validated above.
        unsafe { *out_count = graph.inner.nodes().len() };
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_root(
    graph: *const RwWireGraph,
    out_root: *mut RwWireNodeId,
) -> c_int {
    ffi_status(|| {
        init_output(out_root, 0, "out_root")?;
        let graph = unsafe { wire_graph_ref(graph)? };
        // SAFETY: `out_root` was validated above.
        unsafe { *out_root = graph.inner.root().index() };
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_node_kind(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    out_kind: *mut i32,
) -> c_int {
    ffi_status(|| {
        init_output(out_kind, i32::from(RwWireNodeKind::Null), "out_kind")?;
        let graph = unsafe { wire_graph_ref(graph)? };
        let kind = wire_node(graph, node)?;
        // SAFETY: `out_kind` was validated above.
        unsafe { *out_kind = i32::from(wire_node_kind(kind)) };
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_get_bool(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    out_value: *mut c_int,
) -> c_int {
    ffi_status(|| {
        init_output(out_value, 0, "out_value")?;
        let graph = unsafe { wire_graph_ref(graph)? };
        match wire_node(graph, node)? {
            rw::WireNodeKind::Bool(value) => {
                // SAFETY: `out_value` was validated above.
                unsafe { *out_value = i32::from(*value) };
                Ok(())
            }
            _ => Err(format!("wire node {node} is not a boolean")),
        }
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_get_signed(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    out_value: *mut i64,
) -> c_int {
    ffi_status(|| {
        init_output(out_value, 0, "out_value")?;
        let graph = unsafe { wire_graph_ref(graph)? };
        match wire_node(graph, node)? {
            rw::WireNodeKind::Number(rw::WireNumber::Signed(value)) => {
                // SAFETY: `out_value` was validated above.
                unsafe { *out_value = *value };
                Ok(())
            }
            _ => Err(format!("wire node {node} is not a signed number")),
        }
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_get_unsigned(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    out_value: *mut u64,
) -> c_int {
    ffi_status(|| {
        init_output(out_value, 0, "out_value")?;
        let graph = unsafe { wire_graph_ref(graph)? };
        match wire_node(graph, node)? {
            rw::WireNodeKind::Number(rw::WireNumber::Unsigned(value)) => {
                // SAFETY: `out_value` was validated above.
                unsafe { *out_value = *value };
                Ok(())
            }
            _ => Err(format!("wire node {node} is not an unsigned number")),
        }
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_get_float(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    out_value: *mut c_double,
) -> c_int {
    ffi_status(|| {
        init_output(out_value, 0.0, "out_value")?;
        let graph = unsafe { wire_graph_ref(graph)? };
        match wire_node(graph, node)? {
            rw::WireNodeKind::Number(rw::WireNumber::Float(value)) => {
                // SAFETY: `out_value` was validated above.
                unsafe { *out_value = *value };
                Ok(())
            }
            _ => Err(format!("wire node {node} is not a floating-point number")),
        }
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_get_string(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    out_data: *mut *const u8,
    out_len: *mut usize,
) -> c_int {
    ffi_status(|| {
        init_bytes_outputs(out_data, out_len)?;
        let graph = unsafe { wire_graph_ref(graph)? };
        let value = match wire_node(graph, node)? {
            rw::WireNodeKind::String(value) => value,
            _ => return Err(format!("wire node {node} is not a string")),
        };
        let (data, len) = borrowed_bytes(value);
        // SAFETY: Both output slots were validated above.
        unsafe {
            *out_data = data;
            *out_len = len;
        }
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_array_length(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    out_len: *mut usize,
) -> c_int {
    ffi_status(|| {
        init_output(out_len, 0, "out_len")?;
        let graph = unsafe { wire_graph_ref(graph)? };
        match wire_node(graph, node)? {
            rw::WireNodeKind::Array(children) => {
                // SAFETY: `out_len` was validated above.
                unsafe { *out_len = children.len() };
                Ok(())
            }
            _ => Err(format!("wire node {node} is not an array")),
        }
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_array_child(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    index: usize,
    out_child: *mut RwWireNodeId,
) -> c_int {
    ffi_status(|| {
        init_output(out_child, 0, "out_child")?;
        let graph = unsafe { wire_graph_ref(graph)? };
        let children = match wire_node(graph, node)? {
            rw::WireNodeKind::Array(children) => children,
            _ => return Err(format!("wire node {node} is not an array")),
        };
        let child = children
            .get(index)
            .ok_or_else(|| format!("wire array index {index} is out of range"))?;
        // SAFETY: `out_child` was validated above.
        unsafe { *out_child = child.index() };
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_object_length(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    out_len: *mut usize,
) -> c_int {
    ffi_status(|| {
        init_output(out_len, 0, "out_len")?;
        let graph = unsafe { wire_graph_ref(graph)? };
        match wire_node(graph, node)? {
            rw::WireNodeKind::Object(entries) => {
                // SAFETY: `out_len` was validated above.
                unsafe { *out_len = entries.len() };
                Ok(())
            }
            _ => Err(format!("wire node {node} is not an object")),
        }
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_object_key(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    index: usize,
    out_data: *mut *const u8,
    out_len: *mut usize,
) -> c_int {
    ffi_status(|| {
        init_bytes_outputs(out_data, out_len)?;
        let graph = unsafe { wire_graph_ref(graph)? };
        let entries = match wire_node(graph, node)? {
            rw::WireNodeKind::Object(entries) => entries,
            _ => return Err(format!("wire node {node} is not an object")),
        };
        let (key, _) = entries
            .get(index)
            .ok_or_else(|| format!("wire object index {index} is out of range"))?;
        let (data, len) = borrowed_bytes(key);
        // SAFETY: Both output slots were validated above.
        unsafe {
            *out_data = data;
            *out_len = len;
        }
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_object_child(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    index: usize,
    out_child: *mut RwWireNodeId,
) -> c_int {
    ffi_status(|| {
        init_output(out_child, 0, "out_child")?;
        let graph = unsafe { wire_graph_ref(graph)? };
        let entries = match wire_node(graph, node)? {
            rw::WireNodeKind::Object(entries) => entries,
            _ => return Err(format!("wire node {node} is not an object")),
        };
        let (_, child) = entries
            .get(index)
            .ok_or_else(|| format!("wire object index {index} is out of range"))?;
        // SAFETY: `out_child` was validated above.
        unsafe { *out_child = child.index() };
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_leaf_kind(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    out_kind: *mut i32,
) -> c_int {
    ffi_status(|| {
        init_output(
            out_kind,
            i32::from(RwWireLeafKind::Unserializable),
            "out_kind",
        )?;
        let graph = unsafe { wire_graph_ref(graph)? };
        let leaf = match wire_node(graph, node)? {
            rw::WireNodeKind::Leaf(leaf) => leaf,
            _ => return Err(format!("wire node {node} is not a leaf")),
        };
        // SAFETY: `out_kind` was validated above.
        unsafe { *out_kind = i32::from(wire_leaf_kind(leaf)) };
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_leaf_field_count(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    out_count: *mut usize,
) -> c_int {
    ffi_status(|| {
        init_output(out_count, 0, "out_count")?;
        let graph = unsafe { wire_graph_ref(graph)? };
        let leaf = match wire_node(graph, node)? {
            rw::WireNodeKind::Leaf(leaf) => leaf,
            _ => return Err(format!("wire node {node} is not a leaf")),
        };
        let count = match leaf {
            rw::WireLeaf::Unserializable(_)
            | rw::WireLeaf::BigInt(_)
            | rw::WireLeaf::Date(_)
            | rw::WireLeaf::Url(_) => 1,
            rw::WireLeaf::RegExp { .. } => 2,
            rw::WireLeaf::Error { .. } => 3,
            rw::WireLeaf::Undefined | rw::WireLeaf::Symbol | rw::WireLeaf::Function => 0,
        };
        // SAFETY: `out_count` was validated above.
        unsafe { *out_count = count };
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_wire_graph_leaf_field(
    graph: *const RwWireGraph,
    node: RwWireNodeId,
    index: usize,
    out_data: *mut *const u8,
    out_len: *mut usize,
) -> c_int {
    ffi_status(|| {
        init_bytes_outputs(out_data, out_len)?;
        let graph = unsafe { wire_graph_ref(graph)? };
        let leaf = match wire_node(graph, node)? {
            rw::WireNodeKind::Leaf(leaf) => leaf,
            _ => return Err(format!("wire node {node} is not a leaf")),
        };
        let value = match leaf {
            rw::WireLeaf::Unserializable(value)
            | rw::WireLeaf::BigInt(value)
            | rw::WireLeaf::Date(value)
            | rw::WireLeaf::Url(value) => (index == 0).then_some(value),
            rw::WireLeaf::RegExp { pattern, flags } => match index {
                0 => Some(pattern),
                1 => Some(flags),
                _ => None,
            },
            rw::WireLeaf::Error {
                name,
                message,
                stack,
            } => match index {
                0 => Some(name),
                1 => Some(message),
                2 => Some(stack),
                _ => None,
            },
            rw::WireLeaf::Undefined | rw::WireLeaf::Symbol | rw::WireLeaf::Function => None,
        }
        .ok_or_else(|| format!("wire leaf field index {index} is out of range"))?;
        let (data, len) = borrowed_bytes(value);
        // SAFETY: Both output slots were validated above.
        unsafe {
            *out_data = data;
            *out_len = len;
        }
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_chromium_executable_path(out_path: *mut *mut c_char) -> c_int {
    ffi_status(|| {
        if out_path.is_null() {
            return Err("out_path must not be NULL".to_string());
        }
        // SAFETY: Validated above; initialize before any fallible work.
        unsafe { *out_path = ptr::null_mut() };
        if let Some(path) = rw::rustwright_chromium_executable_path() {
            // SAFETY: Validated above.
            unsafe { *out_path = owned_string(path)? };
        }
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_chromium_launch(
    options_json: *const c_char,
    out_browser: *mut *mut RwBrowser,
) -> c_int {
    ffi_status(|| {
        if out_browser.is_null() {
            return Err("out_browser must not be NULL".to_string());
        }
        // SAFETY: Validated above; initialize before any fallible work.
        unsafe { *out_browser = ptr::null_mut() };
        let options = unsafe { required_str(options_json, "options_json")? };
        let browser = rw::rustwright_launch_chromium(options).map_err(|error| error.to_string())?;
        // SAFETY: Validated above.
        unsafe { *out_browser = Box::into_raw(Box::new(RwBrowser { inner: browser })) };
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_browser_new_page(
    browser: *mut RwBrowser,
    out_page: *mut *mut RwPage,
) -> c_int {
    ffi_status(|| {
        if out_page.is_null() {
            return Err("out_page must not be NULL".to_string());
        }
        // SAFETY: Validated above; initialize before any fallible work.
        unsafe { *out_page = ptr::null_mut() };
        let browser = unsafe { browser_ref(browser)? };
        let page = browser
            .inner
            .new_page()
            .map_err(|error| error.to_string())?;
        // SAFETY: Validated above.
        unsafe { *out_page = Box::into_raw(Box::new(RwPage { inner: page })) };
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_browser_close(browser: *mut RwBrowser) -> c_int {
    ffi_status(|| {
        let browser = unsafe { browser_ref(browser)? };
        browser.inner.close().map_err(|error| error.to_string())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_browser_ws_endpoint(browser: *mut RwBrowser) -> *mut c_char {
    ffi_pointer(|| {
        let browser = unsafe { browser_ref(browser)? };
        owned_string(browser.inner.ws_endpoint())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_browser_free(browser: *mut RwBrowser) {
    let result = catch_unwind(AssertUnwindSafe(|| {
        clear_error();
        if !browser.is_null() {
            // SAFETY: The handle came from Box::into_raw in this library and
            // has not previously been freed.
            drop(unsafe { Box::from_raw(browser) });
        }
    }));
    if let Err(payload) = result {
        record_error(format!(
            "panic at Rustwright C ABI boundary: {}",
            panic_message(payload)
        ));
    }
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_target_id(page: *mut RwPage) -> *mut c_char {
    ffi_pointer(|| {
        let page = unsafe { page_ref(page)? };
        owned_string(page.inner.target_id())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_set_default_timeout(
    page: *mut RwPage,
    timeout_ms_or_nan: c_double,
) -> c_int {
    ffi_status(|| {
        let page = unsafe { page_ref(page)? };
        page.inner.set_default_timeout(timeout(timeout_ms_or_nan));
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_set_default_navigation_timeout(
    page: *mut RwPage,
    timeout_ms_or_nan: c_double,
) -> c_int {
    ffi_status(|| {
        let page = unsafe { page_ref(page)? };
        page.inner
            .set_default_navigation_timeout(timeout(timeout_ms_or_nan));
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_set_context_default_timeout(
    page: *mut RwPage,
    timeout_ms_or_nan: c_double,
) -> c_int {
    ffi_status(|| {
        let page = unsafe { page_ref(page)? };
        page.inner
            .set_context_default_timeout(timeout(timeout_ms_or_nan));
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_set_context_default_navigation_timeout(
    page: *mut RwPage,
    timeout_ms_or_nan: c_double,
) -> c_int {
    ffi_status(|| {
        let page = unsafe { page_ref(page)? };
        page.inner
            .set_context_default_navigation_timeout(timeout(timeout_ms_or_nan));
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_goto(
    page: *mut RwPage,
    url: *const c_char,
    wait_until: *const c_char,
    timeout_ms_or_nan: c_double,
    referer: *const c_char,
    out_response_json: *mut *mut c_char,
) -> c_int {
    ffi_status(|| {
        if out_response_json.is_null() {
            return Err("out_response_json must not be NULL".to_string());
        }
        // SAFETY: Validated above; initialize before any fallible work.
        unsafe { *out_response_json = ptr::null_mut() };
        let page = unsafe { page_ref(page)? };
        let url = unsafe { required_str(url, "url")? };
        let wait_until = unsafe { optional_str(wait_until, "wait_until")? };
        let referer = unsafe { optional_str(referer, "referer")? };
        let response = page
            .inner
            .goto(url, wait_until, timeout(timeout_ms_or_nan), referer)
            .map_err(|error| error.to_string())?;
        // SAFETY: Validated above.
        unsafe { *out_response_json = owned_string(response)? };
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_click(
    page: *mut RwPage,
    selector: *const c_char,
    timeout_ms_or_nan: c_double,
) -> c_int {
    ffi_status(|| {
        let page = unsafe { page_ref(page)? };
        let selector = unsafe { required_str(selector, "selector")? };
        page.inner
            .click(selector, timeout(timeout_ms_or_nan))
            .map_err(|error| error.to_string())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_fill(
    page: *mut RwPage,
    selector: *const c_char,
    value: *const c_char,
    timeout_ms_or_nan: c_double,
) -> c_int {
    ffi_status(|| {
        let page = unsafe { page_ref(page)? };
        let selector = unsafe { required_str(selector, "selector")? };
        let value = unsafe { required_str(value, "value")? };
        page.inner
            .fill(selector, value, timeout(timeout_ms_or_nan))
            .map_err(|error| error.to_string())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_title(
    page: *mut RwPage,
    timeout_ms_or_nan: c_double,
    out_title: *mut *mut c_char,
) -> c_int {
    ffi_status(|| {
        if out_title.is_null() {
            return Err("out_title must not be NULL".to_string());
        }
        // SAFETY: Validated above; initialize before any fallible work.
        unsafe { *out_title = ptr::null_mut() };
        let page = unsafe { page_ref(page)? };
        let title = page
            .inner
            .title(timeout(timeout_ms_or_nan))
            .map_err(|error| error.to_string())?;
        // SAFETY: Validated above.
        unsafe { *out_title = owned_string(title)? };
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_text_content(
    page: *mut RwPage,
    selector: *const c_char,
    timeout_ms_or_nan: c_double,
    out_text: *mut *mut c_char,
) -> c_int {
    ffi_status(|| {
        if out_text.is_null() {
            return Err("out_text must not be NULL".to_string());
        }
        // SAFETY: Validated above; initialize before any fallible work.
        unsafe { *out_text = ptr::null_mut() };
        let page = unsafe { page_ref(page)? };
        let selector = unsafe { required_str(selector, "selector")? };
        if let Some(text) = page
            .inner
            .text_content(selector, timeout(timeout_ms_or_nan))
            .map_err(|error| error.to_string())?
        {
            // SAFETY: Validated above.
            unsafe { *out_text = owned_string(text)? };
        }
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_evaluate(
    page: *mut RwPage,
    expression: *const c_char,
    arg_json: *const c_char,
    timeout_ms_or_nan: c_double,
    out_json: *mut *mut c_char,
) -> c_int {
    ffi_status(|| {
        if out_json.is_null() {
            return Err("out_json must not be NULL".to_string());
        }
        // SAFETY: Validated above; initialize before any fallible work.
        unsafe { *out_json = ptr::null_mut() };
        let page = unsafe { page_ref(page)? };
        let expression = unsafe { required_str(expression, "expression")? };
        let arg_json = unsafe { optional_str(arg_json, "arg_json")? };
        let json = page
            .inner
            .evaluate(expression, arg_json, timeout(timeout_ms_or_nan))
            .map_err(|error| error.to_string())?;
        // SAFETY: Validated above.
        unsafe { *out_json = owned_string(json)? };
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_screenshot(
    page: *mut RwPage,
    options_json: *const c_char,
    out_buffer: *mut *mut u8,
    out_len: *mut usize,
) -> c_int {
    ffi_status(|| {
        if out_buffer.is_null() {
            return Err("out_buf must not be NULL".to_string());
        }
        if out_len.is_null() {
            return Err("out_len must not be NULL".to_string());
        }
        // SAFETY: Validated above; initialize before any fallible work.
        unsafe {
            *out_buffer = ptr::null_mut();
            *out_len = 0;
        }
        let page = unsafe { page_ref(page)? };
        let options_json = unsafe { optional_str(options_json, "options_json")? };
        let options = match options_json {
            Some(value) if !value.trim().is_empty() => {
                serde_json::from_str::<ScreenshotOptions>(value)
                    .map_err(|error| error.to_string())?
            }
            _ => ScreenshotOptions::default(),
        };
        let clip_json = options.clip.map(|clip| clip.to_string());
        let bytes = page
            .inner
            .screenshot(
                options.path.as_deref(),
                options.full_page,
                clip_json.as_deref(),
                options.timeout,
                options.image_type.as_deref(),
                options.quality,
                options.omit_background,
            )
            .map_err(|error| error.to_string())?;
        if !bytes.is_empty() {
            let mut bytes = bytes.into_boxed_slice();
            let len = bytes.len();
            let buffer = bytes.as_mut_ptr();
            std::mem::forget(bytes);
            // SAFETY: Both pointers were validated above.
            unsafe {
                *out_buffer = buffer;
                *out_len = len;
            }
        }
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_close(
    page: *mut RwPage,
    timeout_ms_or_nan: c_double,
    run_before_unload: c_int,
) -> c_int {
    ffi_status(|| {
        let page = unsafe { page_ref(page)? };
        page.inner
            .close(timeout(timeout_ms_or_nan), run_before_unload != 0)
            .map_err(|error| error.to_string())
    })
}

#[no_mangle]
pub unsafe extern "C" fn rw_page_free(page: *mut RwPage) {
    let result = catch_unwind(AssertUnwindSafe(|| {
        clear_error();
        if !page.is_null() {
            // SAFETY: The handle came from Box::into_raw in this library and
            // has not previously been freed.
            drop(unsafe { Box::from_raw(page) });
        }
    }));
    if let Err(payload) = result {
        record_error(format!(
            "panic at Rustwright C ABI boundary: {}",
            panic_message(payload)
        ));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::slice;

    unsafe fn parse_graph(wire: &str) -> *mut RwWireGraph {
        let wire = CString::new(wire).unwrap();
        let mut graph = ptr::null_mut();
        assert_eq!(unsafe { rw_wire_graph_parse(wire.as_ptr(), &mut graph) }, 0);
        assert!(!graph.is_null());
        graph
    }

    unsafe fn borrowed_bytes(data: *const u8, len: usize) -> Vec<u8> {
        if len == 0 {
            assert!(data.is_null());
            Vec::new()
        } else {
            assert!(!data.is_null());
            unsafe { slice::from_raw_parts(data, len) }.to_vec()
        }
    }

    unsafe fn object_entries(
        graph: *const RwWireGraph,
        node: RwWireNodeId,
    ) -> Vec<(Vec<u8>, RwWireNodeId)> {
        let mut len = 0;
        assert_eq!(
            unsafe { rw_wire_graph_object_length(graph, node, &mut len) },
            0
        );
        (0..len)
            .map(|index| {
                let mut data = ptr::null();
                let mut key_len = 0;
                let mut child = 0;
                assert_eq!(
                    unsafe {
                        rw_wire_graph_object_key(graph, node, index, &mut data, &mut key_len)
                    },
                    0
                );
                assert_eq!(
                    unsafe { rw_wire_graph_object_child(graph, node, index, &mut child) },
                    0
                );
                (unsafe { borrowed_bytes(data, key_len) }, child)
            })
            .collect()
    }

    unsafe fn leaf_field(graph: *const RwWireGraph, node: RwWireNodeId, index: usize) -> Vec<u8> {
        let mut data = ptr::null();
        let mut len = 0;
        assert_eq!(
            unsafe { rw_wire_graph_leaf_field(graph, node, index, &mut data, &mut len) },
            0
        );
        unsafe { borrowed_bytes(data, len) }
    }

    #[test]
    fn graph_accessors_preserve_types_order_identity_and_leaf_fields() {
        let graph = unsafe {
            parse_graph(
                r#"{
                    "__rustwright_cdp_object__": 1,
                    "entries": {
                        "signed": -7,
                        "unsigned": 18446744073709551615,
                        "nul\u0000key": "a\u0000b",
                        "values": {
                            "__rustwright_cdp_array__": 2,
                            "items": [
                                true,
                                1.25,
                                {"__rustwright_cdp_unserializable_value__": "NaN"},
                                {"__rustwright_cdp_unserializable_value__": "42n"},
                                {"__rustwright_cdp_date__": "2026-07-21T12:34:56.789Z"},
                                {"__rustwright_cdp_regexp__": {"p": "a+b", "f": "gi"}},
                                {"__rustwright_cdp_url__": "https://example.com/path"},
                                {"__rustwright_cdp_error__": {
                                    "name": "TypeError",
                                    "message": "broken",
                                    "stack": "TypeError: broken"
                                }},
                                {"__rustwright_cdp_undefined__": true},
                                {"__rustwright_cdp_symbol__": true},
                                {"__rustwright_cdp_function__": true},
                                {"__rustwright_cdp_ref__": 1}
                            ]
                        },
                        "forward": {"__rustwright_cdp_ref__": 3},
                        "later": {
                            "__rustwright_cdp_object__": 3,
                            "entries": {"ok": true}
                        },
                        "self": {"__rustwright_cdp_ref__": 1}
                    }
                }"#,
            )
        };

        let mut count = 0;
        let mut root = 0;
        let mut root_kind = i32::from(RwWireNodeKind::Null);
        assert_eq!(unsafe { rw_wire_graph_node_count(graph, &mut count) }, 0);
        assert!(count > 10);
        assert_eq!(unsafe { rw_wire_graph_root(graph, &mut root) }, 0);
        assert_eq!(
            unsafe { rw_wire_graph_node_kind(graph, root, &mut root_kind) },
            0
        );
        assert_eq!(root_kind, i32::from(RwWireNodeKind::Object));

        let entries = unsafe { object_entries(graph, root) };
        let keys = entries
            .iter()
            .map(|(key, _)| key.clone())
            .collect::<Vec<_>>();
        assert_eq!(
            keys,
            vec![
                b"signed".to_vec(),
                b"unsigned".to_vec(),
                b"nul\0key".to_vec(),
                b"values".to_vec(),
                b"forward".to_vec(),
                b"later".to_vec(),
                b"self".to_vec(),
            ]
        );

        let signed_node = entries[0].1;
        let unsigned_node = entries[1].1;
        let string_node = entries[2].1;
        let array_node = entries[3].1;
        let forward_node = entries[4].1;
        let later_node = entries[5].1;
        assert_eq!(entries[6].1, root);
        assert_eq!(forward_node, later_node);

        let mut signed = 0;
        let mut unsigned = 0;
        assert_eq!(
            unsafe { rw_wire_graph_get_signed(graph, signed_node, &mut signed) },
            0
        );
        assert_eq!(signed, -7);
        assert_eq!(
            unsafe { rw_wire_graph_get_unsigned(graph, unsigned_node, &mut unsigned) },
            0
        );
        assert_eq!(unsigned, u64::MAX);

        let mut string_data = ptr::null();
        let mut string_len = 0;
        assert_eq!(
            unsafe {
                rw_wire_graph_get_string(graph, string_node, &mut string_data, &mut string_len)
            },
            0
        );
        assert_eq!(
            unsafe { borrowed_bytes(string_data, string_len) },
            b"a\0b".to_vec()
        );

        let mut array_len = 0;
        assert_eq!(
            unsafe { rw_wire_graph_array_length(graph, array_node, &mut array_len) },
            0
        );
        assert_eq!(array_len, 12);
        let mut bool_node = 0;
        let mut float_node = 0;
        assert_eq!(
            unsafe { rw_wire_graph_array_child(graph, array_node, 0, &mut bool_node) },
            0
        );
        assert_eq!(
            unsafe { rw_wire_graph_array_child(graph, array_node, 1, &mut float_node) },
            0
        );
        let mut bool_value = 0;
        let mut float_value = 0.0;
        assert_eq!(
            unsafe { rw_wire_graph_get_bool(graph, bool_node, &mut bool_value) },
            0
        );
        assert_eq!(bool_value, 1);
        assert_eq!(
            unsafe { rw_wire_graph_get_float(graph, float_node, &mut float_value) },
            0
        );
        assert_eq!(float_value, 1.25);

        let field_counts = [1, 1, 1, 2, 1, 3, 0, 0, 0];
        for (offset, expected_kind) in [
            RwWireLeafKind::Unserializable,
            RwWireLeafKind::BigInt,
            RwWireLeafKind::Date,
            RwWireLeafKind::RegExp,
            RwWireLeafKind::Url,
            RwWireLeafKind::Error,
            RwWireLeafKind::Undefined,
            RwWireLeafKind::Symbol,
            RwWireLeafKind::Function,
        ]
        .into_iter()
        .enumerate()
        {
            let mut leaf_node = 0;
            let mut leaf_kind = i32::from(RwWireLeafKind::Unserializable);
            let mut field_count = usize::MAX;
            assert_eq!(
                unsafe { rw_wire_graph_array_child(graph, array_node, offset + 2, &mut leaf_node) },
                0
            );
            assert_eq!(
                unsafe { rw_wire_graph_leaf_kind(graph, leaf_node, &mut leaf_kind) },
                0
            );
            assert_eq!(leaf_kind, i32::from(expected_kind));
            assert_eq!(
                unsafe { rw_wire_graph_leaf_field_count(graph, leaf_node, &mut field_count,) },
                0
            );
            assert_eq!(field_count, field_counts[offset]);
        }

        let mut bigint_node = 0;
        assert_eq!(
            unsafe { rw_wire_graph_array_child(graph, array_node, 3, &mut bigint_node) },
            0
        );
        assert_eq!(unsafe { leaf_field(graph, bigint_node, 0) }, b"42");
        let mut regexp_node = 0;
        assert_eq!(
            unsafe { rw_wire_graph_array_child(graph, array_node, 5, &mut regexp_node) },
            0
        );
        assert_eq!(unsafe { leaf_field(graph, regexp_node, 0) }, b"a+b");
        assert_eq!(unsafe { leaf_field(graph, regexp_node, 1) }, b"gi");
        let mut error_node = 0;
        assert_eq!(
            unsafe { rw_wire_graph_array_child(graph, array_node, 7, &mut error_node) },
            0
        );
        assert_eq!(unsafe { leaf_field(graph, error_node, 0) }, b"TypeError");
        assert_eq!(unsafe { leaf_field(graph, error_node, 1) }, b"broken");
        assert_eq!(
            unsafe { leaf_field(graph, error_node, 2) },
            b"TypeError: broken"
        );
        let mut back_edge = 0;
        assert_eq!(
            unsafe { rw_wire_graph_array_child(graph, array_node, 11, &mut back_edge) },
            0
        );
        assert_eq!(back_edge, root);

        unsafe { rw_wire_graph_free(graph) };
    }

    #[test]
    fn graph_accessors_initialize_outputs_before_reporting_errors() {
        let malformed = CString::new(r#"{"__rustwright_cdp_ref__":99}"#).unwrap();
        let mut graph = 1usize as *mut RwWireGraph;
        assert_ne!(
            unsafe { rw_wire_graph_parse(malformed.as_ptr(), &mut graph) },
            0
        );
        assert!(graph.is_null());
        assert!(!rw_last_error().is_null());

        let graph = unsafe { parse_graph(r#"{"__rustwright_cdp_array__":1,"items":[true]}"#) };
        let mut root = 0;
        assert_eq!(unsafe { rw_wire_graph_root(graph, &mut root) }, 0);
        let mut bool_value = 99;
        assert_ne!(
            unsafe { rw_wire_graph_get_bool(graph, root, &mut bool_value) },
            0
        );
        assert_eq!(bool_value, 0);
        let mut data = 1usize as *const u8;
        let mut len = 99;
        assert_ne!(
            unsafe { rw_wire_graph_get_string(graph, root, &mut data, &mut len) },
            0
        );
        assert!(data.is_null());
        assert_eq!(len, 0);
        assert_ne!(
            unsafe { rw_wire_graph_node_count(ptr::null(), &mut len) },
            0
        );
        assert_eq!(len, 0);
        unsafe {
            rw_wire_graph_free(graph);
            rw_wire_graph_free(ptr::null_mut());
        }
    }

    #[test]
    fn wire_kind_types_have_fixed_width_abi() {
        assert_eq!(std::mem::size_of::<RwWireNodeKind>(), 4);
        assert_eq!(std::mem::size_of::<RwWireLeafKind>(), 4);
        assert_eq!(std::mem::size_of::<i32>(), 4);
    }

    #[test]
    fn decode_wire_round_trip_uses_c_string_ownership() {
        let wire = CString::new(
            r#"{"__rustwright_cdp_array__":1,"items":[{"value":true},{"__rustwright_cdp_ref__":1}]}"#,
        )
        .unwrap();
        let mut out = ptr::null_mut();

        let status = unsafe { rw_decode_wire(wire.as_ptr(), &mut out) };

        assert_eq!(status, 0);
        assert!(!out.is_null());
        let decoded = unsafe { CStr::from_ptr(out) }.to_str().unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(decoded).unwrap(),
            serde_json::json!([
                {"value": true},
                {"__rustwright_cdp_cycle__": true},
            ])
        );
        unsafe { rw_string_free(out) };
    }
}
