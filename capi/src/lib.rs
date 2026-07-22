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
