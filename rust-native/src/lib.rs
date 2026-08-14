//! Idiomatic, synchronous Rust API for Rustwright.
//!
//! This crate is intentionally a thin wrapper over `rustwright-core`. The core
//! owns Chromium, CDP, and its async runtime; callers do not need Tokio.

use serde::Serialize;
use serde_json::Value;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::mpsc::{self, RecvTimeoutError};
use std::thread;
use std::time::{Duration, Instant};

pub use rustwright_core::{
    ActionFailureError, ActionTimeoutError, ActionabilityError, CancelToken, CommandWritten,
    FailureKind, FailureMetadata, FailurePhase, FailureTargetKind, RwError as Error,
};

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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub headless: Option<bool>,
    pub executable_path: Option<String>,
    pub channel: Option<String>,
    pub args: Vec<String>,
    pub ignore_all_default_args: bool,
    pub ignore_default_args: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timeout: Option<f64>,
    pub user_data_dir: Option<String>,
    pub env: HashMap<String, String>,
    pub chromium_sandbox: bool,
    pub proxy: Option<ProxyOptions>,
}

impl Default for LaunchOptions {
    fn default() -> Self {
        Self {
            headless: None,
            executable_path: None,
            channel: None,
            args: Vec::new(),
            ignore_all_default_args: false,
            ignore_default_args: Vec::new(),
            timeout: None,
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
        self.headless = Some(headless);
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

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TargetLifecycleEvent {
    Upsert { target_id: String, url: String },
    Destroyed { target_id: String },
}

pub struct TargetLifecycleReceiver {
    inner: rustwright_core::RustwrightTargetLifecycleReceiver,
}

impl TargetLifecycleReceiver {
    fn map_event(event: rustwright_core::RustwrightTargetLifecycleEvent) -> TargetLifecycleEvent {
        match event {
            rustwright_core::RustwrightTargetLifecycleEvent::Upsert { target_id, url } => {
                TargetLifecycleEvent::Upsert { target_id, url }
            }
            rustwright_core::RustwrightTargetLifecycleEvent::Destroyed { target_id } => {
                TargetLifecycleEvent::Destroyed { target_id }
            }
        }
    }

    pub fn recv_timeout(&self, timeout: Duration) -> Option<TargetLifecycleEvent> {
        self.inner.recv_timeout(timeout).map(Self::map_event)
    }

    pub fn try_recv(
        &self,
    ) -> std::result::Result<Option<TargetLifecycleEvent>, std::sync::mpsc::TryRecvError> {
        self.inner
            .try_recv()
            .map(|event| event.map(Self::map_event))
    }
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

    pub fn target_lifecycle(&self) -> TargetLifecycleReceiver {
        TargetLifecycleReceiver {
            inner: self.inner.target_lifecycle(),
        }
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

/// A pending file chooser delivered by [`EventReceiver`].
#[derive(Clone, Debug)]
pub struct FileChooser {
    inner: rustwright_core::RustwrightFileChooser,
}

impl FileChooser {
    /// Supply workspace-confined local host paths to the file input.
    pub fn set_files(&self, paths: &[PathBuf]) -> Result<()> {
        self.inner.set_files(paths)
    }

    /// Cancel the chooser by supplying Chromium's supported empty file list.
    pub fn cancel(&self) -> Result<()> {
        self.inner.cancel()
    }

    /// Whether the intercepted input accepts more than one file.
    pub fn is_multiple(&self) -> bool {
        self.inner.is_multiple()
    }
}

/// Source location attached to a console record by Chromium.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConsoleLocation {
    pub url: String,
    pub line_number: u64,
    pub column_number: u64,
}

/// One captured console API call.
#[derive(Clone, Debug, PartialEq)]
pub struct ConsoleRecord {
    pub message_type: String,
    pub text: String,
    pub args: Vec<Value>,
    pub location: Option<ConsoleLocation>,
    /// First stack frame with a Chromium-attributed URL, falling back to the
    /// raw top frame when every frame is anonymous.
    pub attributed_location: Option<ConsoleLocation>,
    pub navigation_epoch: u64,
}

/// A bounded console-record read result.
#[derive(Clone, Debug, PartialEq)]
pub struct ConsoleRecords {
    pub records: Vec<ConsoleRecord>,
    pub navigation_epoch: u64,
    pub evicted: u64,
}

/// One captured request/response lifecycle.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NetworkRecord {
    pub index: u64,
    pub method: String,
    pub url: String,
    pub resource_type: String,
    pub response_status: Option<u16>,
    pub failure: Option<String>,
    pub request_headers: Vec<(String, String)>,
    pub request_body: Option<String>,
    pub response_headers: Vec<(String, String)>,
    pub navigation_epoch: u64,
    pub completed: bool,
}

/// A bounded network-record read result.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NetworkRecords {
    pub records: Vec<NetworkRecord>,
    pub navigation_epoch: u64,
    pub navigation_start_index: u64,
    pub evicted: u64,
}

/// Lazy bounded response-body result.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NetworkBody {
    Text {
        text: String,
        total_bytes: usize,
        truncated: bool,
    },
    Unavailable {
        reason: String,
    },
}

/// Metadata observed while waiting for a top-level navigation.
#[derive(Clone, Debug, PartialEq)]
pub struct NavigationObservation {
    pub response_json: Value,
    pub main_status: Option<u16>,
    pub same_document: bool,
}

/// A history-navigation result that distinguishes a missing entry from navigation.
#[derive(Clone, Debug, PartialEq)]
pub struct HistoryNavigationObservation {
    pub had_entry: bool,
    pub navigation: NavigationObservation,
}

/// Additive detail emitted for main-frame navigation events.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NavigationDetail {
    pub url: String,
    pub same_document: bool,
}

/// Typed page events emitted by [`Page::events`].
#[derive(Clone, Debug)]
pub enum PageEvent {
    Dialog {
        kind: DialogKind,
        message: String,
        dialog: Dialog,
    },
    FileChooser {
        multiple: bool,
        chooser: FileChooser,
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

/// Pull-based opt-in main-frame navigation-detail subscription.
///
/// Each receiver buffers at most 128 details. If producers outrun a receiver,
/// the oldest details are discarded and [`NavigationDetailReceiver::dropped_count`]
/// reports the cumulative loss. Dropping the receiver shuts down its subscription.
pub struct NavigationDetailReceiver {
    inner: rustwright_core::RustwrightNavigationDetailReceiver,
}

impl NavigationDetailReceiver {
    pub fn recv_timeout(&self, timeout: Duration) -> Option<NavigationDetail> {
        self.inner.recv_timeout(timeout).map(map_navigation_detail)
    }

    pub fn dropped_count(&self) -> u64 {
        self.inner.dropped_count()
    }

    #[doc(hidden)]
    pub fn recv_timeout_sequenced(&self, timeout: Duration) -> Option<(u64, NavigationDetail)> {
        self.inner
            .recv_timeout_sequenced(timeout)
            .map(|(sequence, detail)| (sequence, map_navigation_detail(detail)))
    }

    #[doc(hidden)]
    pub fn latest_sequence(&self) -> u64 {
        self.inner.latest_sequence()
    }

    pub fn capacity(&self) -> usize {
        self.inner.capacity()
    }
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

    /// Subscribe to additive main-frame navigation details.
    pub fn navigation_details(&self) -> NavigationDetailReceiver {
        NavigationDetailReceiver {
            inner: self.inner.navigation_details(),
        }
    }

    /// Read the page's bounded console-record ring.
    ///
    /// Pass `include_previous_navigations` to include all retained epochs and
    /// `clear` to remove the returned scope after reading it.
    pub fn console_records(
        &self,
        include_previous_navigations: bool,
        clear: bool,
    ) -> Result<ConsoleRecords> {
        let records = self
            .inner
            .console_records(include_previous_navigations, clear)?;
        Ok(ConsoleRecords {
            records: records
                .records
                .into_iter()
                .map(|record| ConsoleRecord {
                    message_type: record.message_type,
                    text: record.text,
                    args: record.args,
                    location: record.location.map(|location| ConsoleLocation {
                        url: location.url,
                        line_number: location.line_number,
                        column_number: location.column_number,
                    }),
                    attributed_location: record.attributed_location.map(|location| {
                        ConsoleLocation {
                            url: location.url,
                            line_number: location.line_number,
                            column_number: location.column_number,
                        }
                    }),
                    navigation_epoch: record.navigation_epoch,
                })
                .collect(),
            navigation_epoch: records.navigation_epoch,
            evicted: records.evicted,
        })
    }

    /// Arm console capture without reading or clearing retained records.
    ///
    /// Existing callers retain lazy first-read behavior unless they opt into
    /// this method before a navigation boundary.
    pub fn arm_console_capture(&self) -> Result<()> {
        self.inner.arm_console_capture()
    }

    /// Read the page's oldest-first, 1,024-record request/response lifecycle ring.
    ///
    /// The result reports evictions for the selected navigation scope. Pass
    /// `clear` to remove that scope after taking the snapshot.
    pub fn network_records(
        &self,
        include_previous_navigations: bool,
        clear: bool,
    ) -> NetworkRecords {
        let records = self
            .inner
            .network_records(include_previous_navigations, clear);
        NetworkRecords {
            records: records
                .records
                .into_iter()
                .map(|record| NetworkRecord {
                    index: record.index,
                    method: record.method,
                    url: record.url,
                    resource_type: record.resource_type,
                    response_status: record.response_status,
                    failure: record.failure,
                    request_headers: record.request_headers,
                    request_body: record.request_body,
                    response_headers: record.response_headers,
                    navigation_epoch: record.navigation_epoch,
                    completed: record.completed,
                })
                .collect(),
            navigation_epoch: records.navigation_epoch,
            navigation_start_index: records.navigation_start_index,
            evicted: records.evicted,
        }
    }

    /// Lazily fetch a retained text response body and cap returned bytes.
    ///
    /// The returned body is limited to the lesser of `max_bytes` and the
    /// core's 20 MiB per-read ceiling.
    pub fn network_response_body(&self, index: u64, max_bytes: usize) -> Result<NetworkBody> {
        Ok(match self.inner.network_response_body(index, max_bytes)? {
            rustwright_core::RustwrightNetworkBody::Text {
                text,
                total_bytes,
                truncated,
            } => NetworkBody::Text {
                text,
                total_bytes,
                truncated,
            },
            rustwright_core::RustwrightNetworkBody::Unavailable { reason } => {
                NetworkBody::Unavailable { reason }
            }
        })
    }

    /// Navigate to `url` and return the response metadata JSON value.
    pub fn goto(&self, url: &str, options: GotoOptions) -> Result<Value> {
        self.goto_with_cancel(url, options, None)
    }

    /// Navigate to `url` and return response and document-transition metadata.
    pub fn goto_observed(&self, url: &str, options: GotoOptions) -> Result<NavigationObservation> {
        self.goto_with_cancel_observed(url, options, None)
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

    /// Navigate with cancellation and return response and transition metadata.
    pub fn goto_with_cancel_observed(
        &self,
        url: &str,
        options: GotoOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<NavigationObservation> {
        let observation = self.inner.goto_with_cancel_observed(
            url,
            options.wait_until.as_deref(),
            options.timeout,
            options.referer.as_deref(),
            cancel,
        )?;
        map_navigation_observation(observation)
    }

    /// Emit the failure-only navigation dump for a caller-owned timeout.
    #[doc(hidden)]
    pub fn emit_navigation_timeout_diagnostic(&self, elapsed: Duration) {
        self.inner.emit_navigation_timeout_diagnostic(elapsed);
    }

    /// Navigate to the previous history entry, if one exists.
    pub fn go_back(&self, options: GotoOptions) -> Result<Value> {
        self.go_back_with_cancel(options, None)
    }

    pub fn go_back_observed(&self, options: GotoOptions) -> Result<HistoryNavigationObservation> {
        self.go_back_with_cancel_observed(options, None)
    }

    /// Navigate backward with an optional cancellation signal.
    pub fn go_back_with_cancel(
        &self,
        options: GotoOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<Value> {
        let json = self.inner.go_back_with_cancel(
            options.wait_until.as_deref(),
            duration_from_timeout_ms(options.timeout),
            cancel,
        )?;
        Ok(serde_json::from_str(&json)?)
    }

    pub fn go_back_with_cancel_observed(
        &self,
        options: GotoOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<HistoryNavigationObservation> {
        let timeout = duration_from_timeout_ms(options.timeout);
        let observation = self.inner.go_back_with_cancel_observed(
            options.wait_until.as_deref(),
            timeout,
            cancel,
        )?;
        map_history_navigation_observation(observation)
    }

    /// Navigate backward and report whether a history entry existed.
    pub fn go_back_with_cancel_status(
        &self,
        options: GotoOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<(bool, Value)> {
        let (had_entry, json) = self.inner.go_back_with_cancel_status(
            options.wait_until.as_deref(),
            duration_from_timeout_ms(options.timeout),
            cancel,
        )?;
        Ok((had_entry, serde_json::from_str(&json)?))
    }

    /// Navigate to the next history entry, if one exists.
    pub fn go_forward(&self, options: GotoOptions) -> Result<Value> {
        self.go_forward_with_cancel(options, None)
    }

    pub fn go_forward_observed(
        &self,
        options: GotoOptions,
    ) -> Result<HistoryNavigationObservation> {
        self.go_forward_with_cancel_observed(options, None)
    }

    /// Navigate forward with an optional cancellation signal.
    pub fn go_forward_with_cancel(
        &self,
        options: GotoOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<Value> {
        let json = self.inner.go_forward_with_cancel(
            options.wait_until.as_deref(),
            duration_from_timeout_ms(options.timeout),
            cancel,
        )?;
        Ok(serde_json::from_str(&json)?)
    }

    pub fn go_forward_with_cancel_observed(
        &self,
        options: GotoOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<HistoryNavigationObservation> {
        let timeout = duration_from_timeout_ms(options.timeout);
        let observation = self.inner.go_forward_with_cancel_observed(
            options.wait_until.as_deref(),
            timeout,
            cancel,
        )?;
        map_history_navigation_observation(observation)
    }

    /// Navigate forward and report whether a history entry existed.
    pub fn go_forward_with_cancel_status(
        &self,
        options: GotoOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<(bool, Value)> {
        let (had_entry, json) = self.inner.go_forward_with_cancel_status(
            options.wait_until.as_deref(),
            duration_from_timeout_ms(options.timeout),
            cancel,
        )?;
        Ok((had_entry, serde_json::from_str(&json)?))
    }

    /// Reload the page and wait for the requested navigation state.
    pub fn reload(&self, options: GotoOptions) -> Result<Value> {
        self.reload_with_cancel(options, None)
    }

    pub fn reload_observed(&self, options: GotoOptions) -> Result<NavigationObservation> {
        self.reload_with_cancel_observed(options, None)
    }

    /// Reload with an optional cancellation signal.
    pub fn reload_with_cancel(
        &self,
        options: GotoOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<Value> {
        let json = self.inner.reload_with_cancel(
            options.wait_until.as_deref(),
            duration_from_timeout_ms(options.timeout),
            cancel,
        )?;
        Ok(serde_json::from_str(&json)?)
    }

    pub fn reload_with_cancel_observed(
        &self,
        options: GotoOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<NavigationObservation> {
        let timeout = duration_from_timeout_ms(options.timeout);
        let observation = self.inner.reload_with_cancel_observed(
            options.wait_until.as_deref(),
            timeout,
            cancel,
        )?;
        map_navigation_observation(observation)
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

    /// Strictly resolve `selector`, auto-wait for actionability, and click it
    /// through Chromium's physical mouse input pipeline.
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

    /// Strictly resolve `selector`, auto-wait, and physically double-click it.
    pub fn dblclick(&self, selector: &str, options: ActionOptions) -> Result<()> {
        self.dblclick_with_cancel(selector, options, None)
    }

    /// Double-click with an optional cancellation signal.
    pub fn dblclick_with_cancel(
        &self,
        selector: &str,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .dblclick_with_cancel(selector, options.timeout, cancel)
    }

    /// Strictly resolve two selectors, auto-wait, and physically drag the first
    /// element to the second through Chromium's native mouse input pipeline.
    pub fn drag_and_drop(
        &self,
        start_selector: &str,
        end_selector: &str,
        options: ActionOptions,
    ) -> Result<()> {
        self.drag_and_drop_with_cancel(start_selector, end_selector, options, None)
    }

    /// Physically drag between two selectors with an optional cancellation signal.
    pub fn drag_and_drop_with_cancel(
        &self,
        start_selector: &str,
        end_selector: &str,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .drag_and_drop_with_cancel(start_selector, end_selector, options.timeout, cancel)
    }

    /// Click an element inside the first frame matching `frame_selector`.
    pub fn click_in_frame(
        &self,
        frame_selector: &str,
        selector: &str,
        options: ActionOptions,
    ) -> Result<()> {
        self.click_in_frame_with_cancel(frame_selector, selector, options, None)
    }

    /// Click inside a frame with an optional cancellation signal.
    pub fn click_in_frame_with_cancel(
        &self,
        frame_selector: &str,
        selector: &str,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        let locator = serde_json::json!({
            "kind": "frame",
            "frame_selector": frame_selector,
            "frame_index": 0,
            "frame_strict": true,
            "inner": {
                "kind": "css",
                "selector": selector,
            },
        });
        self.inner.click_locator_json_with_cancel(
            &locator.to_string(),
            0,
            options.timeout,
            true,
            cancel,
        )
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
        self.type_text_with_options_and_cancel(
            selector,
            text,
            delay,
            ActionOptions::default(),
            cancel,
        )
    }

    /// Type with an operation timeout.
    pub fn type_text_with_options(
        &self,
        selector: &str,
        text: &str,
        delay: Option<Duration>,
        options: ActionOptions,
    ) -> Result<()> {
        self.type_text_with_options_and_cancel(selector, text, delay, options, None)
    }

    /// Type with an operation timeout and optional cancellation signal.
    pub fn type_text_with_options_and_cancel(
        &self,
        selector: &str,
        text: &str,
        delay: Option<Duration>,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .type_text_with_timeout_and_cancel(selector, text, delay, options.timeout, cancel)
    }

    /// Press a native key, optionally after focusing an element.
    pub fn press_key(&self, selector: Option<&str>, key: &str) -> Result<()> {
        self.press_key_with_cancel(selector, key, None)
    }

    /// Press a native key with an optional cancellation signal.
    pub fn press_key_with_cancel(
        &self,
        selector: Option<&str>,
        key: &str,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.press_key_with_options_and_cancel(selector, key, ActionOptions::default(), cancel)
    }

    /// Press a native key with an operation timeout.
    pub fn press_key_with_options(
        &self,
        selector: Option<&str>,
        key: &str,
        options: ActionOptions,
    ) -> Result<()> {
        self.press_key_with_options_and_cancel(selector, key, options, None)
    }

    /// Press a native key with an operation timeout and optional cancellation signal.
    pub fn press_key_with_options_and_cancel(
        &self,
        selector: Option<&str>,
        key: &str,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .press_key_with_timeout_and_cancel(selector, key, options.timeout, cancel)
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
    ///
    /// Matches option values only. Use
    /// [`Self::select_options_by_value_or_label_with_cancel`] when a visible
    /// label should also match.
    pub fn select_options_with_cancel<S: AsRef<str>>(
        &self,
        selector: &str,
        values: &[S],
        cancel: Option<&CancelToken>,
    ) -> Result<Vec<String>> {
        self.select_options_with_options_and_cancel(
            selector,
            values,
            ActionOptions::default(),
            cancel,
        )
    }

    /// Select values with an operation timeout and optional cancellation signal.
    pub fn select_options_with_options_and_cancel<S: AsRef<str>>(
        &self,
        selector: &str,
        values: &[S],
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<Vec<String>> {
        let values = values
            .iter()
            .map(|value| value.as_ref().to_string())
            .collect::<Vec<_>>();
        self.inner
            .select_options_with_cancel(selector, &values, options.timeout, cancel)
    }

    /// Select exact values or visible labels in DOM order and return the resulting values.
    ///
    /// Unlike [`Self::select_options`], this treats value and label matches
    /// alike. For a single-select, the first matching option in DOM order wins.
    pub fn select_options_by_value_or_label<S: AsRef<str>>(
        &self,
        selector: &str,
        values: &[S],
    ) -> Result<Vec<String>> {
        self.select_options_by_value_or_label_with_cancel(selector, values, None)
    }

    /// Select exact values or visible labels in DOM order with cancellation.
    pub fn select_options_by_value_or_label_with_cancel<S: AsRef<str>>(
        &self,
        selector: &str,
        values: &[S],
        cancel: Option<&CancelToken>,
    ) -> Result<Vec<String>> {
        self.select_options_by_value_or_label_with_options_and_cancel(
            selector,
            values,
            ActionOptions::default(),
            cancel,
        )
    }

    /// Select exact values or visible labels with an operation timeout and cancellation.
    pub fn select_options_by_value_or_label_with_options_and_cancel<S: AsRef<str>>(
        &self,
        selector: &str,
        values: &[S],
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<Vec<String>> {
        let values = values
            .iter()
            .map(|value| value.as_ref().to_string())
            .collect::<Vec<_>>();
        self.inner.select_options_by_value_or_label_with_cancel(
            selector,
            &values,
            options.timeout,
            cancel,
        )
    }

    /// Move Chromium's native mouse to the element center.
    pub fn hover(&self, selector: &str) -> Result<()> {
        self.hover_with_cancel(selector, None)
    }

    /// Hover with an optional cancellation signal and a 30-second default timeout.
    pub fn hover_with_cancel(&self, selector: &str, cancel: Option<&CancelToken>) -> Result<()> {
        self.hover_with_options_and_cancel(selector, ActionOptions::default(), cancel)
    }

    /// Hover with an operation timeout.
    pub fn hover_with_options(&self, selector: &str, options: ActionOptions) -> Result<()> {
        self.hover_with_options_and_cancel(selector, options, None)
    }

    /// Hover with an operation timeout and optional cancellation signal.
    pub fn hover_with_options_and_cancel(
        &self,
        selector: &str,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .hover_with_timeout_and_cancel(selector, options.timeout, cancel)
    }

    /// Check a checkbox through Chromium's native mouse input.
    pub fn check(&self, selector: &str) -> Result<()> {
        self.check_with_cancel(selector, ActionOptions::default(), None)
    }

    /// Check with an operation timeout and optional cancellation signal.
    pub fn check_with_cancel(
        &self,
        selector: &str,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .check_with_cancel(selector, options.timeout, cancel)
    }

    /// Uncheck a checkbox through Chromium's native mouse input.
    pub fn uncheck(&self, selector: &str) -> Result<()> {
        self.uncheck_with_cancel(selector, ActionOptions::default(), None)
    }

    /// Uncheck with an operation timeout and optional cancellation signal.
    pub fn uncheck_with_cancel(
        &self,
        selector: &str,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .uncheck_with_cancel(selector, options.timeout, cancel)
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
        self.scroll_into_view_with_cancel(selector, ActionOptions::default(), None)
    }

    /// Scroll an element into view with an optional cancellation signal.
    pub fn scroll_into_view_with_cancel(
        &self,
        selector: &str,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .scroll_into_view_with_cancel(selector, options.timeout, cancel)
    }

    /// Scroll the page viewport and wait briefly for the visual position to settle.
    pub fn scroll_viewport(&self, delta_y: f64, options: ActionOptions) -> Result<()> {
        self.scroll_viewport_with_cancel(delta_y, options, None)
    }

    /// Scroll the page viewport with an optional cancellation signal.
    pub fn scroll_viewport_with_cancel(
        &self,
        delta_y: f64,
        options: ActionOptions,
        cancel: Option<&CancelToken>,
    ) -> Result<()> {
        self.inner
            .scroll_viewport_with_cancel(delta_y, options.timeout, cancel)
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
    ///
    /// JavaScript bigint values become decimal strings because
    /// [`serde_json::Value`] has no arbitrary-precision integer variant.
    /// Negative zero retains its sign. Non-finite `f64` values are mapped
    /// through their Rust constants, which become `Value::Null` because JSON
    /// cannot represent NaN or infinity.
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
        decode_evaluate_wire(&json)
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
        rustwright_core::RustwrightPageEvent::FileChooser { multiple, chooser } => {
            PageEvent::FileChooser {
                multiple,
                chooser: FileChooser { inner: chooser },
            }
        }
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

fn map_navigation_detail(detail: rustwright_core::RustwrightNavigationDetail) -> NavigationDetail {
    NavigationDetail {
        url: detail.url,
        same_document: detail.same_document,
    }
}

fn map_navigation_observation(
    observation: rustwright_core::NavigationObservation,
) -> Result<NavigationObservation> {
    Ok(NavigationObservation {
        response_json: serde_json::from_str(&observation.response_json)?,
        main_status: observation.main_status,
        same_document: observation.same_document,
    })
}

fn map_history_navigation_observation(
    observation: rustwright_core::HistoryNavigationObservation,
) -> Result<HistoryNavigationObservation> {
    Ok(HistoryNavigationObservation {
        had_entry: observation.had_entry,
        navigation: map_navigation_observation(observation.navigation)?,
    })
}

fn duration_from_timeout_ms(timeout_ms: Option<f64>) -> Duration {
    match timeout_ms {
        Some(ms) if ms <= 0.0 => Duration::from_secs(24 * 60 * 60),
        Some(ms) => Duration::from_millis(ms.max(1.0) as u64),
        None => Duration::from_secs(30),
    }
}

fn decode_evaluate_wire(wire_json: &str) -> Result<Value> {
    let decoded = rustwright_core::decode_wire_value(wire_json)?;
    let value = serde_json::from_str(&decoded)?;
    Ok(map_wire_leaves(value))
}

fn map_wire_leaves(value: Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(values.into_iter().map(map_wire_leaves).collect()),
        Value::Object(mut object) => {
            if object.contains_key("__rustwright_cdp_undefined__")
                || object.contains_key("__rustwright_cdp_symbol__")
                || object.contains_key("__rustwright_cdp_function__")
            {
                return Value::Null;
            }
            if let Some(value) = object.remove("__rustwright_cdp_unserializable_value__") {
                return map_unserializable_value(value);
            }
            if let Some(value) = object.remove("__rustwright_cdp_bigint__") {
                return map_bigint_value(value);
            }
            if let Some(value) = object.remove("__rustwright_cdp_date__") {
                return value;
            }
            if let Some(value) = object.remove("__rustwright_cdp_url__") {
                return value;
            }
            if let Some(value) = object.remove("__rustwright_cdp_regexp__") {
                return map_wire_leaves(value);
            }
            if let Some(value) = object.remove("__rustwright_cdp_error__") {
                return map_wire_leaves(value);
            }
            Value::Object(
                object
                    .into_iter()
                    .map(|(key, value)| (key, map_wire_leaves(value)))
                    .collect(),
            )
        }
        value => value,
    }
}

fn map_unserializable_value(value: Value) -> Value {
    let Value::String(value) = value else {
        return value;
    };
    match value.as_str() {
        "NaN" => Value::from(f64::NAN),
        "Infinity" => Value::from(f64::INFINITY),
        "-Infinity" => Value::from(f64::NEG_INFINITY),
        "-0" => Value::from(-0.0_f64),
        _ => value.strip_suffix('n').map_or_else(
            || Value::String(value.clone()),
            |digits| Value::String(digits.to_owned()),
        ),
    }
}

fn map_bigint_value(value: Value) -> Value {
    match value {
        Value::String(value) => {
            Value::String(value.strip_suffix('n').unwrap_or(value.as_str()).to_owned())
        }
        value => value,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn physical_drag_facade_signatures_match_action_options_and_cancellation_contract() {
        let _: fn(&Page, &str, &str, ActionOptions) -> Result<()> = Page::drag_and_drop;
        let _: fn(&Page, &str, &str, ActionOptions, Option<&CancelToken>) -> Result<()> =
            Page::drag_and_drop_with_cancel;
    }

    #[test]
    fn public_page_event_enum_remains_exhaustively_matchable() {
        fn event_name(event: PageEvent) -> &'static str {
            match event {
                PageEvent::Dialog { .. } => "dialog",
                PageEvent::FileChooser { .. } => "filechooser",
                PageEvent::Download { .. } => "download",
                PageEvent::PageCrashed => "crashed",
                PageEvent::Closed => "closed",
                PageEvent::Navigated { url: _ } => "navigated",
            }
        }

        assert_eq!(
            event_name(PageEvent::Navigated {
                url: "https://example.test/".to_owned(),
            }),
            "navigated"
        );
    }

    #[test]
    fn navigation_observation_and_detail_conversion_preserve_additive_fields() {
        let observation = map_navigation_observation(rustwright_core::NavigationObservation {
            response_json: r#"{"status":201}"#.to_owned(),
            main_status: Some(201),
            same_document: false,
        })
        .unwrap();
        assert_eq!(observation.response_json, json!({"status": 201}));
        assert_eq!(observation.main_status, Some(201));
        assert!(!observation.same_document);

        assert_eq!(
            map_navigation_detail(rustwright_core::RustwrightNavigationDetail {
                url: "https://example.test/#same".to_owned(),
                same_document: true,
            }),
            NavigationDetail {
                url: "https://example.test/#same".to_owned(),
                same_document: true,
            }
        );
    }

    #[test]
    fn deterministic_legacy_public_navigation_methods_preserve_exact_values() {
        #[derive(Clone, Copy)]
        enum Call {
            Goto,
            GotoWithCancel,
            Back,
            BackWithCancel,
            BackStatus,
            Forward,
            ForwardWithCancel,
            ForwardStatus,
            Reload,
            ReloadWithCancel,
        }

        enum CallResult {
            Value(Value),
            Status(bool, Value),
        }

        fn exact_response(request_id: &str, url: &str, status: u16) -> Value {
            json!({
                "request_id": request_id,
                "loader_id": format!("{request_id}-loader"),
                "frame_id": null,
                "resource_type": "Document",
                "url": url,
                "status": status,
                "status_text": null,
                "headers": {},
                "encoded_data_length": null,
                "protocol": null,
                "remote_ip_address": null,
                "remote_port": null,
                "security_details": null,
                "from_disk_cache": false,
                "from_service_worker": false
            })
        }

        fn run(call: Call) -> CallResult {
            let mut harness = rustwright_core::RustwrightNavigationHarness::new(16);
            let page = Page {
                inner: harness.page(),
            };
            let responder = thread::spawn(move || match call {
                Call::Goto | Call::GotoWithCancel => {
                    harness.reply_next(
                        "Page.navigate",
                        json!({ "frameId": "frame-main", "loaderId": "goto-loader" }),
                    );
                    harness.emit(json!({
                        "sessionId": "page-session",
                        "method": "Network.responseReceived",
                        "params": {
                            "requestId": "goto",
                            "loaderId": "goto-loader",
                            "type": "Document",
                            "response": {
                                "url": "https://example.test/goto",
                                "status": 200,
                                "headers": {}
                            }
                        }
                    }));
                }
                Call::Back | Call::BackWithCancel | Call::BackStatus => {
                    harness.reply_next(
                        "Page.getNavigationHistory",
                        json!({
                            "currentIndex": 1,
                            "entries": [
                                { "id": 1, "url": "https://example.test/back" },
                                { "id": 2, "url": "https://example.test/current" }
                            ]
                        }),
                    );
                    harness.reply_next("Page.navigateToHistoryEntry", json!({}));
                    harness.emit(json!({
                        "sessionId": "page-session",
                        "method": "Network.responseReceived",
                        "params": {
                            "requestId": "back",
                            "loaderId": "back-loader",
                            "type": "Document",
                            "response": {
                                "url": "https://example.test/back",
                                "status": 201,
                                "headers": {}
                            }
                        }
                    }));
                }
                Call::Forward | Call::ForwardWithCancel | Call::ForwardStatus => {
                    harness.reply_next(
                        "Page.getNavigationHistory",
                        json!({
                            "currentIndex": 0,
                            "entries": [
                                { "id": 1, "url": "https://example.test/current" },
                                { "id": 2, "url": "https://example.test/forward" }
                            ]
                        }),
                    );
                    harness.reply_next("Page.navigateToHistoryEntry", json!({}));
                    harness.emit(json!({
                        "sessionId": "page-session",
                        "method": "Network.responseReceived",
                        "params": {
                            "requestId": "forward",
                            "loaderId": "forward-loader",
                            "type": "Document",
                            "response": {
                                "url": "https://example.test/forward",
                                "status": 202,
                                "headers": {}
                            }
                        }
                    }));
                }
                Call::Reload | Call::ReloadWithCancel => {
                    harness.reply_next("Page.reload", json!({}));
                }
            });
            let options = GotoOptions::default().wait_until("commit").timeout(1_000.0);
            let result = match call {
                Call::Goto => {
                    CallResult::Value(page.goto("https://example.test/goto", options).unwrap())
                }
                Call::GotoWithCancel => CallResult::Value(
                    page.goto_with_cancel("https://example.test/goto", options, None)
                        .unwrap(),
                ),
                Call::Back => CallResult::Value(page.go_back(options).unwrap()),
                Call::BackWithCancel => {
                    CallResult::Value(page.go_back_with_cancel(options, None).unwrap())
                }
                Call::BackStatus => {
                    let (had_entry, value) =
                        page.go_back_with_cancel_status(options, None).unwrap();
                    CallResult::Status(had_entry, value)
                }
                Call::Forward => CallResult::Value(page.go_forward(options).unwrap()),
                Call::ForwardWithCancel => {
                    CallResult::Value(page.go_forward_with_cancel(options, None).unwrap())
                }
                Call::ForwardStatus => {
                    let (had_entry, value) =
                        page.go_forward_with_cancel_status(options, None).unwrap();
                    CallResult::Status(had_entry, value)
                }
                Call::Reload => CallResult::Value(page.reload(options).unwrap()),
                Call::ReloadWithCancel => {
                    CallResult::Value(page.reload_with_cancel(options, None).unwrap())
                }
            };
            responder.join().unwrap();
            result
        }

        let expected = [
            (
                [Call::Goto, Call::GotoWithCancel].as_slice(),
                exact_response("goto", "https://example.test/goto", 200),
            ),
            (
                [Call::Back, Call::BackWithCancel].as_slice(),
                exact_response("back", "https://example.test/back", 201),
            ),
            (
                [Call::Forward, Call::ForwardWithCancel].as_slice(),
                exact_response("forward", "https://example.test/forward", 202),
            ),
            (
                [Call::Reload, Call::ReloadWithCancel].as_slice(),
                Value::Null,
            ),
        ];
        for (calls, expected) in expected {
            for call in calls {
                let CallResult::Value(actual) = run(*call) else {
                    panic!("expected a value result")
                };
                assert_eq!(actual, expected);
            }
        }
        for (call, expected) in [
            (
                Call::BackStatus,
                exact_response("back", "https://example.test/back", 201),
            ),
            (
                Call::ForwardStatus,
                exact_response("forward", "https://example.test/forward", 202),
            ),
        ] {
            let CallResult::Status(had_entry, actual) = run(call) else {
                panic!("expected a status result")
            };
            assert!(had_entry);
            assert_eq!(actual, expected);
        }

        for forward in [false, true] {
            let harness = rustwright_core::RustwrightNavigationHarness::new(4);
            let page = Page {
                inner: harness.page(),
            };
            let cancel = CancelToken::new();
            cancel.cancel();
            let result = if forward {
                page.go_forward_with_cancel(GotoOptions::default(), Some(&cancel))
            } else {
                page.go_back_with_cancel(GotoOptions::default(), Some(&cancel))
            };
            assert!(matches!(result, Err(Error::Cancelled)));
        }
    }

    #[test]
    fn decode_evaluate_wire_resolves_references_instead_of_dropping_them() {
        let decoded = decode_evaluate_wire(
            r#"{
                "__rustwright_cdp_object__": 1,
                "entries": {
                    "first": {
                        "__rustwright_cdp_array__": 2,
                        "items": [1, {"ok": true}]
                    },
                    "again": {"__rustwright_cdp_ref__": 2}
                }
            }"#,
        )
        .unwrap();

        assert_eq!(
            decoded,
            json!({
                "first": [1, {"ok": true}],
                "again": [1, {"ok": true}],
            })
        );
    }

    #[test]
    fn decode_evaluate_wire_maps_leaf_values() {
        let decoded = decode_evaluate_wire(
            r#"[
                {"__rustwright_cdp_unserializable_value__": "NaN"},
                {"__rustwright_cdp_unserializable_value__": "Infinity"},
                {"__rustwright_cdp_unserializable_value__": "-Infinity"},
                {"__rustwright_cdp_unserializable_value__": "-0"},
                {"__rustwright_cdp_unserializable_value__": "12345678901234567890n"},
                {"__rustwright_cdp_bigint__": "98765432109876543210"},
                {"__rustwright_cdp_date__": "2026-07-21T12:34:56.789Z"},
                {"__rustwright_cdp_regexp__": {"p": "a+b", "f": "gi"}},
                {"__rustwright_cdp_url__": "https://example.com/path"},
                {"__rustwright_cdp_error__": {
                    "name": "TypeError", "message": "broken", "stack": "trace"
                }},
                {"__rustwright_cdp_undefined__": true},
                {"__rustwright_cdp_symbol__": true},
                {"__rustwright_cdp_function__": true}
            ]"#,
        )
        .unwrap();
        let values = decoded.as_array().unwrap();

        assert_eq!(values[0], Value::Null);
        assert_eq!(values[1], Value::Null);
        assert_eq!(values[2], Value::Null);
        let negative_zero = values[3].as_f64().unwrap();
        assert_eq!(negative_zero, 0.0);
        assert!(negative_zero.is_sign_negative());
        assert_eq!(values[4], "12345678901234567890");
        assert_eq!(values[5], "98765432109876543210");
        assert_eq!(values[6], "2026-07-21T12:34:56.789Z");
        assert_eq!(values[7], json!({"p": "a+b", "f": "gi"}));
        assert_eq!(values[8], "https://example.com/path");
        assert_eq!(
            values[9],
            json!({"name": "TypeError", "message": "broken", "stack": "trace"})
        );
        assert_eq!(&values[10..], &[Value::Null, Value::Null, Value::Null]);
    }

    #[test]
    fn decode_evaluate_wire_preserves_cycle_markers() {
        let decoded = decode_evaluate_wire(
            r#"{
                "__rustwright_cdp_object__": 1,
                "entries": {"self": {"__rustwright_cdp_ref__": 1}}
            }"#,
        )
        .unwrap();

        assert_eq!(decoded, json!({"self": {"__rustwright_cdp_cycle__": true}}));
    }
}
