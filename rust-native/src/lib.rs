//! Idiomatic, synchronous Rust API for Rustwright.
//!
//! This crate is intentionally a thin wrapper over `rustwright-core`. The core
//! owns Chromium, CDP, and its async runtime; callers do not need Tokio.

use serde::Serialize;
use serde_json::{Map, Value};
use std::collections::HashMap;

pub use rustwright_core::RwError as Error;

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
        let options_json = serde_json::to_string(&options)?;
        let inner = rustwright_core::rustwright_launch_chromium(&options_json)?;
        Ok(Browser { inner })
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

/// A launched Chromium browser.
#[derive(Clone)]
pub struct Browser {
    inner: rustwright_core::RustwrightBrowser,
}

impl Browser {
    /// Open a fresh page in the browser's default context.
    pub fn new_page(&self) -> Result<Page> {
        self.inner.new_page().map(|inner| Page { inner })
    }

    /// Close Chromium and all of its pages.
    pub fn close(&self) -> Result<()> {
        self.inner.close()
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

    /// Navigate to `url` and return the response metadata JSON value.
    pub fn goto(&self, url: &str, options: GotoOptions) -> Result<Value> {
        let json = self.inner.goto(
            url,
            options.wait_until.as_deref(),
            options.timeout,
            options.referer.as_deref(),
        )?;
        Ok(serde_json::from_str(&json)?)
    }

    /// Click the first element matching `selector`.
    pub fn click(&self, selector: &str, options: ActionOptions) -> Result<()> {
        self.inner.click(selector, options.timeout)
    }

    /// Fill the first element matching `selector`.
    pub fn fill(&self, selector: &str, value: &str, options: ActionOptions) -> Result<()> {
        self.inner.fill(selector, value, options.timeout)
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
        let arg_json = arg.map(serde_json::to_string).transpose()?;
        let json = self
            .inner
            .evaluate(expression, arg_json.as_deref(), options.timeout)?;
        let wire: Value = serde_json::from_str(&json)?;
        Ok(decode_wire_value(wire))
    }

    /// Capture a screenshot and return its encoded bytes.
    pub fn screenshot(&self, options: ScreenshotOptions) -> Result<Vec<u8>> {
        let clip_json = options.clip.map(|value| value.to_string());
        self.inner.screenshot(
            options.path.as_deref(),
            options.full_page,
            clip_json.as_deref(),
            options.timeout,
            options.image_type.as_deref(),
            options.quality,
            options.omit_background,
        )
    }

    /// Close this page.
    pub fn close(&self, options: CloseOptions) -> Result<()> {
        self.inner.close(options.timeout, options.run_before_unload)
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
