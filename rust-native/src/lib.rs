//! Idiomatic, synchronous Rust API for Rustwright.
//!
//! This crate is intentionally a thin wrapper over `rustwright-core`. The core
//! owns Chromium, CDP, and its async runtime; callers do not need Tokio.

use serde::Serialize;
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::sync::mpsc::{self, RecvTimeoutError};
use std::thread;
use std::time::{Duration, Instant};

pub use rustwright_core::{CancelToken, RwError as Error};

/// Result type returned by the native API.
pub type Result<T> = std::result::Result<T, Error>;

/// Obtain the Chromium browser type.
pub fn chromium() -> Chromium {
    Chromium
}

/// Chromium launcher and executable discovery.
#[derive(Clone, Copy, Debug, Default)]
pub struct Chromium;

impl Chromium {
    /// Discover the Chromium executable that a launch would use.
    pub fn executable_path(&self) -> Option<String> {
        rustwright_core::rustwright_chromium_executable_path()
    }

    /// Launch Chromium with the supplied options.
    pub fn launch(&self, options: LaunchOptions) -> Result<Browser> {
        self.launch_with_cancel(options, None)
    }

    /// Launch Chromium with an optional cancellation signal.
    pub fn launch_with_cancel(
        &self,
        options: LaunchOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<Browser> {
        let options_json = serde_json::to_string(&options)?;
        let inner = rustwright_core::rustwright_launch_chromium_with_cancel(&options_json, cancel)?;
        Ok(Browser { inner })
    }

    /// Attach to an existing browser over its CDP endpoint.
    pub fn connect_over_cdp(&self, options: ConnectOptions) -> Result<Browser> {
        self.connect_over_cdp_with_cancel(options, None)
    }

    /// Attach to an existing browser with an optional cancellation signal.
    pub fn connect_over_cdp_with_cancel(
        &self,
        options: ConnectOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<Browser> {
        let inner = rustwright_core::rustwright_connect_over_cdp_with_cancel(
            &options.endpoint,
            &options.headers,
            options.timeout,
            cancel,
        )?;
        Ok(Browser { inner })
    }
}

/// Options for attaching to an existing browser over CDP.
#[derive(Clone, Debug)]
pub struct ConnectOptions {
    pub endpoint: String,
    pub headers: Vec<(String, String)>,
    pub timeout: Duration,
}

impl ConnectOptions {
    /// Create options with the default 60-second attach timeout.
    pub fn new(endpoint: impl Into<String>) -> Self {
        Self {
            endpoint: endpoint.into(),
            headers: Vec::new(),
            timeout: Duration::from_secs(60),
        }
    }

    /// Add an HTTP/WebSocket header used while resolving and attaching.
    pub fn header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push((name.into(), value.into()));
        self
    }

    /// Set the total attach timeout.
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }
}

/// Optional proxy configuration for Chromium.
#[derive(Clone, Debug, Serialize)]
pub struct ProxyOptions {
    pub server: String,
    pub bypass: Option<String>,
    pub username: Option<String>,
    pub password: Option<String>,
}

impl ProxyOptions {
    /// Create proxy options for a proxy server URL.
    pub fn new(server: impl Into<String>) -> Self {
        Self {
            server: server.into(),
            bypass: None,
            username: None,
            password: None,
        }
    }
}

/// Chromium process launch options.
#[derive(Clone, Debug, Serialize)]
pub struct LaunchOptions {
    pub headless: bool,
    pub executable_path: Option<String>,
    pub channel: Option<String>,
    pub args: Vec<String>,
    pub ignore_all_default_args: bool,
    pub ignore_default_args: Vec<String>,
    pub timeout: Option<f64>,
    pub user_data_dir: Option<String>,
    pub env: HashMap<String, String>,
    pub chromium_sandbox: bool,
    pub proxy: Option<ProxyOptions>,
}

impl Default for LaunchOptions {
    fn default() -> Self {
        Self {
            headless: true,
            executable_path: None,
            channel: None,
            args: Vec::new(),
            ignore_all_default_args: false,
            ignore_default_args: Vec::new(),
            timeout: Some(30_000.0),
            user_data_dir: None,
            env: HashMap::new(),
            chromium_sandbox: false,
            proxy: None,
        }
    }
}

impl LaunchOptions {
    /// Set whether Chromium launches headlessly.
    pub fn headless(mut self, headless: bool) -> Self {
        self.headless = headless;
        self
    }

    /// Override the Chromium executable path.
    pub fn executable_path(mut self, path: impl Into<String>) -> Self {
        self.executable_path = Some(path.into());
        self
    }

    /// Override the launch timeout in milliseconds; `None` uses the core default.
    pub fn timeout(mut self, timeout_ms: Option<f64>) -> Self {
        self.timeout = timeout_ms;
        self
    }

    /// Append one Chromium command-line argument.
    pub fn arg(mut self, arg: impl Into<String>) -> Self {
        self.args.push(arg.into());
        self
    }
}

/// A launched or remotely attached Chromium browser.
#[derive(Clone)]
pub struct Browser {
    inner: rustwright_core::RustwrightBrowser,
}

impl Browser {
    /// Open a fresh page in the browser's default context.
    pub fn new_page(&self) -> Result<Page> {
        self.new_page_with_cancel(None)
    }

    /// Open a fresh page with an optional cancellation signal.
    pub fn new_page_with_cancel(&self, cancel: Option<&CancelToken>) -> Result<Page> {
        self.inner
            .new_page_with_cancel(cancel)
            .map(|inner| Page { inner })
    }

    /// List and adopt the existing pages in the browser's default context.
    pub fn pages(&self) -> Result<Vec<Page>> {
        self.pages_with_cancel(Duration::from_secs(30), None)
    }

    /// List and adopt existing pages with a bounded timeout and optional cancellation signal.
    pub fn pages_with_cancel(
        &self,
        timeout: Duration,
        cancel: Option<&CancelToken>,
    ) -> Result<Vec<Page>> {
        const CANCEL_POLL_INTERVAL: Duration = Duration::from_millis(5);

        let Some(cancel) = cancel else {
            return self
                .inner
                .pages(timeout)
                .map(|pages| pages.into_iter().map(|inner| Page { inner }).collect());
        };
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        if timeout.is_zero() {
            return Err(Error::Timeout(0));
        }

        let timeout_ms = timeout.as_millis().min(u128::from(u64::MAX)) as u64;
        let deadline = Instant::now().checked_add(timeout);
        let inner = self.inner.clone();
        let (result_tx, result_rx) = mpsc::sync_channel(1);
        let worker = thread::Builder::new()
            .name("rustwright-pages".to_owned())
            .spawn(move || {
                let _ = result_tx.send(inner.pages(timeout));
            })?;
        drop(worker);

        loop {
            if cancel.is_cancelled() {
                return Err(Error::Cancelled);
            }
            let wait = deadline
                .map(|deadline| deadline.saturating_duration_since(Instant::now()))
                .unwrap_or(CANCEL_POLL_INTERVAL);
            if wait.is_zero() {
                return Err(Error::Timeout(timeout_ms));
            }
            match result_rx.recv_timeout(wait.min(CANCEL_POLL_INTERVAL)) {
                Ok(result) => {
                    if cancel.is_cancelled() {
                        return Err(Error::Cancelled);
                    }
                    return result
                        .map(|pages| pages.into_iter().map(|inner| Page { inner }).collect());
                }
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => {
                    return Err(Error::Message("page listing worker stopped".to_owned()));
                }
            }
        }
    }

    /// Close this browser handle: terminate an owned Chromium process, or detach
    /// from an attached browser while leaving the remote browser alive.
    pub fn close(&self) -> Result<()> {
        self.inner.close()
    }

    /// Whether the CDP connection is currently alive.
    pub fn is_connected(&self) -> bool {
        self.inner.is_connected()
    }

    /// Whether this handle owns the Chromium process it controls.
    pub fn is_owned(&self) -> bool {
        self.inner.is_owned()
    }

    /// Return Chromium's DevTools WebSocket endpoint.
    pub fn ws_endpoint(&self) -> String {
        self.inner.ws_endpoint()
    }
}

/// Options for navigation.
#[derive(Clone, Debug, Default)]
pub struct GotoOptions {
    pub wait_until: Option<String>,
    pub timeout: Option<f64>,
    pub referer: Option<String>,
}

impl GotoOptions {
    /// Wait for one of `load`, `domcontentloaded`, `networkidle`, or `commit`.
    pub fn wait_until(mut self, state: impl Into<String>) -> Self {
        self.wait_until = Some(state.into());
        self
    }

    /// Set the navigation timeout in milliseconds.
    pub fn timeout(mut self, timeout_ms: f64) -> Self {
        self.timeout = Some(timeout_ms);
        self
    }

    /// Set the HTTP Referer header for this navigation.
    pub fn referer(mut self, referer: impl Into<String>) -> Self {
        self.referer = Some(referer.into());
        self
    }
}

/// Timeout options shared by element actions and reads.
#[derive(Clone, Copy, Debug, Default)]
pub struct ActionOptions {
    pub timeout: Option<f64>,
}

impl ActionOptions {
    /// Set the operation timeout in milliseconds.
    pub fn timeout(timeout_ms: f64) -> Self {
        Self {
            timeout: Some(timeout_ms),
        }
    }
}

/// Screenshot options matching the alpha Node surface.
#[derive(Clone, Debug, Default)]
pub struct ScreenshotOptions {
    pub path: Option<String>,
    pub full_page: Option<bool>,
    pub clip: Option<Value>,
    pub timeout: Option<f64>,
    pub image_type: Option<String>,
    pub quality: Option<u32>,
    pub omit_background: Option<bool>,
}

impl ScreenshotOptions {
    /// Also write the screenshot to this path.
    pub fn path(mut self, path: impl Into<String>) -> Self {
        self.path = Some(path.into());
        self
    }

    /// Capture the entire scrollable page.
    pub fn full_page(mut self, full_page: bool) -> Self {
        self.full_page = Some(full_page);
        self
    }
}

/// Options for closing a page.
#[derive(Clone, Copy, Debug, Default)]
pub struct CloseOptions {
    pub timeout: Option<f64>,
    pub run_before_unload: bool,
}

/// The category of a JavaScript dialog.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DialogKind {
    Alert,
    Confirm,
    Prompt,
    BeforeUnload,
    Other(String),
}

/// A pending JavaScript dialog delivered by [`EventReceiver`].
#[derive(Clone, Debug)]
pub struct Dialog {
    inner: rustwright_core::RustwrightDialog,
}

impl Dialog {
    /// Accept the dialog, optionally supplying prompt text.
    pub fn accept(&self, prompt_text: Option<&str>) -> Result<()> {
        self.inner.accept(prompt_text)
    }

    /// Dismiss the dialog.
    pub fn dismiss(&self) -> Result<()> {
        self.inner.dismiss()
    }
}

/// Typed page events emitted by [`Page::events`].
#[derive(Clone, Debug)]
pub enum PageEvent {
    Dialog {
        kind: DialogKind,
        message: String,
        dialog: Dialog,
    },
    Download {
        guid: String,
        url: String,
        suggested_name: String,
    },
    PageCrashed,
    Closed,
    Navigated {
        url: String,
    },
}

/// Pull-based page event subscription.
///
/// Each receiver owns a 128-entry queue. New events evict the oldest entry when
/// full; [`EventReceiver::dropped_count`] reports queue evictions and upstream
/// transport lag. Events are ordered as observed from CDP. `Closed` and
/// `PageCrashed` are terminal after queued events have been drained.
pub struct EventReceiver {
    inner: rustwright_core::RustwrightPageEventReceiver,
}

impl EventReceiver {
    /// Wait up to `timeout` for the next event.
    pub fn recv_timeout(&self, timeout: Duration) -> Option<PageEvent> {
        self.inner.recv_timeout(timeout).map(map_page_event)
    }

    /// Return the number of events lost to bounded-queue eviction or upstream lag.
    pub fn dropped_count(&self) -> u64 {
        self.inner.dropped_count()
    }

    /// Return the maximum number of typed events buffered by this receiver.
    pub fn capacity(&self) -> usize {
        self.inner.capacity()
    }
}

/// A page controlled through the shared Rust CDP core.
#[derive(Clone)]
pub struct Page {
    inner: rustwright_core::RustwrightPage,
}

impl Page {
    /// Return the underlying Chromium target id.
    pub fn target_id(&self) -> String {
        self.inner.target_id()
    }

    /// Return the cached URL of the page's main frame.
    pub fn url(&self) -> String {
        self.inner.url()
    }

    /// Subscribe to this page's typed, bounded, drop-oldest event stream.
    pub fn events(&self) -> EventReceiver {
        EventReceiver {
            inner: self.inner.events(),
        }
    }

    /// Navigate to `url` and return the response metadata JSON value.
    pub fn goto(&self, url: &str, options: GotoOptions) -> Result<Value> {
        self.goto_with_cancel(url, options, None)
    }

    /// Navigate with an optional cancellation signal.
    pub fn goto_with_cancel(
        &self,
        url: &str,
        options: GotoOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<Value> {
        let json = self.inner.goto_with_cancel(
            url,
            options.wait_until.as_deref(),
            options.timeout,
            options.referer.as_deref(),
            cancel,
        )?;
        Ok(serde_json::from_str(&json)?)
    }

    /// Navigate to the previous history entry, if one exists.
    pub fn go_back(&self, options: GotoOptions) -> Result<Value> {
        self.go_back_with_cancel(options, None)
    }

    /// Navigate backward with an optional cancellation signal.
    pub fn go_back_with_cancel(
        &self,
        options: GotoOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<Value> {
        let timeout = duration_from_timeout_ms(options.timeout);
        let json =
            self.inner
                .go_back_with_cancel(options.wait_until.as_deref(), timeout, cancel)?;
        Ok(serde_json::from_str(&json)?)
    }

    /// Reload the page and wait for the requested navigation state.
    pub fn reload(&self, options: GotoOptions) -> Result<Value> {
        self.reload_with_cancel(options, None)
    }

    /// Reload with an optional cancellation signal.
    pub fn reload_with_cancel(
        &self,
        options: GotoOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<Value> {
        let timeout = duration_from_timeout_ms(options.timeout);
        let json = self
            .inner
            .reload_with_cancel(options.wait_until.as_deref(), timeout, cancel)?;
        Ok(serde_json::from_str(&json)?)
    }

    /// Wait until the page reaches a load lifecycle state.
    pub fn wait_for_load_state(&self, state: &str, timeout: Duration) -> Result<()> {
        self.wait_for_load_state_with_cancel(state, timeout, None)
    }

    /// Wait for a lifecycle state with an optional cancellation signal.
    pub fn wait_for_load_state_with_cancel(
        &self,
        state: &str,
        timeout: Duration,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .wait_for_load_state_with_cancel(state, timeout, cancel)
    }

    /// Click the first element matching `selector`.
    pub fn click(&self, selector: &str, options: ActionOptions) -> Result<()> {
        self.click_with_cancel(selector, options, None)
    }

    /// Click with an optional cancellation signal.
    pub fn click_with_cancel(
        &self,
        selector: &str,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .click_with_cancel(selector, options.timeout, cancel)
    }

    /// Fill the first element matching `selector`.
    pub fn fill(&self, selector: &str, value: &str, options: ActionOptions) -> Result<()> {
        self.fill_with_cancel(selector, value, options, None)
    }

    /// Fill with an optional cancellation signal.
    pub fn fill_with_cancel(
        &self,
        selector: &str,
        value: &str,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .fill_with_cancel(selector, value, options.timeout, cancel)
    }

    /// Focus the element and type through Chromium's native input domain.
    pub fn type_text(&self, selector: &str, text: &str, delay: Option<Duration>) -> Result<()> {
        self.type_text_with_cancel(selector, text, delay, None)
    }

    /// Type with an optional cancellation signal.
    pub fn type_text_with_cancel(
        &self,
        selector: &str,
        text: &str,
        delay: Option<Duration>,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .type_text_with_cancel(selector, text, delay, cancel)
    }

    /// Press a native key, optionally after focusing an element.
    pub fn press_key(&self, selector: Option<&str>, key: &str) -> Result<()> {
        self.inner.press_key(selector, key)
    }

    /// Select option values through the DOM and return the resulting values.
    ///
    /// This is intentionally a DOM-backed shortcut pending the P3 actionability
    /// phase; selection has no native pointer equivalent in the current engine.
    pub fn select_options<S: AsRef<str>>(
        &self,
        selector: &str,
        values: &[S],
    ) -> Result<Vec<String>> {
        self.select_options_with_cancel(selector, values, None)
    }

    /// Select values with an optional cancellation signal.
    pub fn select_options_with_cancel<S: AsRef<str>>(
        &self,
        selector: &str,
        values: &[S],
        cancel: Option<&CancelToken>,
    ) -> Result<Vec<String>> {
        let values = values
            .iter()
            .map(|value| value.as_ref().to_string())
            .collect::<Vec<_>>();
        self.inner
            .select_options_with_cancel(selector, &values, cancel)
    }

    /// Move Chromium's native mouse to the element center.
    pub fn hover(&self, selector: &str) -> Result<()> {
        self.hover_with_cancel(selector, None)
    }

    /// Hover with an optional cancellation signal.
    pub fn hover_with_cancel(&self, selector: &str, cancel: Option<&CancelToken>) -> Result<()> {
        self.inner.hover_with_cancel(selector, cancel)
    }

    /// Check a checkbox through Chromium's native mouse input.
    pub fn check(&self, selector: &str) -> Result<()> {
        self.inner.check(selector)
    }

    /// Uncheck a checkbox through Chromium's native mouse input.
    pub fn uncheck(&self, selector: &str) -> Result<()> {
        self.inner.uncheck(selector)
    }

    /// Return the DOM-backed rendered inner text of an element.
    pub fn inner_text(&self, selector: &str) -> Result<Option<String>> {
        self.inner.inner_text(selector)
    }

    /// Return a DOM-backed attribute value.
    pub fn get_attribute(&self, selector: &str, name: &str) -> Result<Option<String>> {
        self.inner.get_attribute(selector, name)
    }

    /// Return the locator engine's DOM-backed visibility state.
    pub fn is_visible(&self, selector: &str) -> Result<bool> {
        self.inner.is_visible(selector)
    }

    /// Return the locator engine's DOM-backed enabled state.
    pub fn is_enabled(&self, selector: &str) -> Result<bool> {
        self.inner.is_enabled(selector)
    }

    /// Return the DOM-backed checked state of a native or ARIA control.
    pub fn is_checked(&self, selector: &str) -> Result<bool> {
        self.inner.is_checked(selector)
    }

    /// Set the viewport through Chromium's emulation domain.
    pub fn set_viewport_size(&self, width: u32, height: u32) -> Result<()> {
        self.inner.set_viewport_size(width, height)
    }

    /// Scroll an element into view through the DOM.
    ///
    /// This is an explicit DOM-backed shortcut pending P3 actionability checks.
    pub fn scroll_into_view(&self, selector: &str) -> Result<()> {
        self.inner.scroll_into_view(selector)
    }

    /// Return the document title.
    pub fn title(&self, options: ActionOptions) -> Result<String> {
        self.inner.title(options.timeout)
    }

    /// Return an element's textContent, or `None` for JavaScript null.
    pub fn text_content(&self, selector: &str, options: ActionOptions) -> Result<Option<String>> {
        self.inner.text_content(selector, options.timeout)
    }

    /// Evaluate JavaScript and decode the core's JSON wire representation.
    pub fn evaluate(
        &self,
        expression: &str,
        arg: Option<&Value>,
        options: ActionOptions,
    ) -> Result<Value> {
        self.evaluate_with_cancel(expression, arg, options, None)
    }

    /// Evaluate JavaScript with an optional cancellation signal.
    pub fn evaluate_with_cancel(
        &self,
        expression: &str,
        arg: Option<&Value>,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<Value> {
        let arg_json = arg.map(serde_json::to_string).transpose()?;
        let json = self.inner.evaluate_with_cancel(
            expression,
            arg_json.as_deref(),
            options.timeout,
            cancel,
        )?;
        let wire: Value = serde_json::from_str(&json)?;
        Ok(decode_wire_value(wire))
    }

    /// Capture a screenshot and return its encoded bytes.
    pub fn screenshot(&self, options: ScreenshotOptions) -> Result<Vec<u8>> {
        self.screenshot_with_cancel(options, None)
    }

    /// Capture a screenshot with an optional cancellation signal.
    pub fn screenshot_with_cancel(
        &self,
        options: ScreenshotOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<Vec<u8>> {
        let clip_json = options.clip.map(|value| value.to_string());
        self.inner.screenshot_with_cancel(
            options.path.as_deref(),
            options.full_page,
            clip_json.as_deref(),
            options.timeout,
            options.image_type.as_deref(),
            options.quality,
            options.omit_background,
            cancel,
        )
    }

    /// Close this page.
    pub fn close(&self, options: CloseOptions) -> Result<()> {
        self.inner.close(options.timeout, options.run_before_unload)
    }
}

fn map_page_event(event: rustwright_core::RustwrightPageEvent) -> PageEvent {
    match event {
        rustwright_core::RustwrightPageEvent::Dialog {
            kind,
            message,
            dialog,
        } => PageEvent::Dialog {
            kind: match kind {
                rustwright_core::RustwrightDialogKind::Alert => DialogKind::Alert,
                rustwright_core::RustwrightDialogKind::Confirm => DialogKind::Confirm,
                rustwright_core::RustwrightDialogKind::Prompt => DialogKind::Prompt,
                rustwright_core::RustwrightDialogKind::BeforeUnload => DialogKind::BeforeUnload,
                rustwright_core::RustwrightDialogKind::Other(value) => DialogKind::Other(value),
            },
            message,
            dialog: Dialog { inner: dialog },
        },
        rustwright_core::RustwrightPageEvent::Download {
            guid,
            url,
            suggested_name,
        } => PageEvent::Download {
            guid,
            url,
            suggested_name,
        },
        rustwright_core::RustwrightPageEvent::PageCrashed => PageEvent::PageCrashed,
        rustwright_core::RustwrightPageEvent::Closed => PageEvent::Closed,
        rustwright_core::RustwrightPageEvent::Navigated { url } => PageEvent::Navigated { url },
    }
}

fn duration_from_timeout_ms(timeout_ms: Option<f64>) -> Duration {
    match timeout_ms {
        Some(ms) if ms <= 0.0 => Duration::from_secs(24 * 60 * 60),
        Some(ms) => Duration::from_millis(ms.max(1.0) as u64),
        None => Duration::from_secs(30),
    }
}

fn decode_wire_value(value: Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(values.into_iter().map(decode_wire_value).collect()),
        Value::Object(mut object) => {
            if object.contains_key("__rustwright_cdp_undefined__")
                || object.contains_key("__rustwright_cdp_symbol__")
                || object.contains_key("__rustwright_cdp_function__")
                || object.contains_key("__rustwright_cdp_ref__")
            {
                return Value::Null;
            }
            if let Some(value) = object.remove("__rustwright_cdp_date__") {
                return value;
            }
            if let Some(value) = object.remove("__rustwright_cdp_url__") {
                return value;
            }
            if object.contains_key("__rustwright_cdp_array__") {
                return object
                    .remove("items")
                    .map(decode_wire_value)
                    .unwrap_or(Value::Array(Vec::new()));
            }
            if object.contains_key("__rustwright_cdp_object__") {
                return object
                    .remove("entries")
                    .map(decode_wire_value)
                    .unwrap_or_else(|| Value::Object(Map::new()));
            }
            Value::Object(
                object
                    .into_iter()
                    .map(|(key, value)| (key, decode_wire_value(value)))
                    .collect(),
            )
        }
        value => value,
    }
}
