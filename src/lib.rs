use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::env;
use std::fs;
use std::future::Future;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::ops::Deref;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
#[cfg(unix)]
use std::os::unix::io::{FromRawFd, RawFd};
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
#[cfg(feature = "python")]
use std::sync::OnceLock;
use std::sync::{Arc, Condvar, Mutex, Weak};
use std::time::{Duration, Instant};

use base64::Engine;
use futures_util::{future::try_join_all, SinkExt, StreamExt};
#[cfg(feature = "python")]
use pyo3::exceptions::{PyRuntimeError, PyValueError};
#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::{PyAny, PyBytes, PyModule};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tempfile::{NamedTempFile, TempDir};
use thiserror::Error;
use tokio::sync::{broadcast, mpsc, oneshot, watch};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::uri::PathAndQuery;
use tokio_tungstenite::tungstenite::http::{HeaderName, HeaderValue, Uri};
use tokio_tungstenite::tungstenite::{Error as WsError, Message};
use tokio_tungstenite::{connect_async, MaybeTlsStream};

pub type RwResult<T> = Result<T, RwError>;
type CdpPendingMap = Arc<Mutex<HashMap<u64, oneshot::Sender<RwResult<Value>>>>>;

/// A thread-safe cancellation signal for synchronous facade operations.
///
/// Cancelling a token wakes the async CDP wait owned by a synchronous call, so
/// the owner thread can return without waiting for the operation timeout.
#[derive(Clone, Debug, Default)]
pub struct CancelToken {
    inner: Arc<CancelTokenInner>,
}

#[derive(Debug, Default)]
struct CancelTokenInner {
    cancelled: Arc<AtomicBool>,
    changed: tokio::sync::Notify,
}

impl CancelToken {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn cancel(&self) {
        if !self.inner.cancelled.swap(true, Ordering::SeqCst) {
            self.inner.changed.notify_waiters();
        }
    }

    pub fn is_cancelled(&self) -> bool {
        self.inner.cancelled.load(Ordering::SeqCst)
    }

    fn atomic_flag(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.inner.cancelled)
    }

    async fn cancelled(&self) {
        loop {
            if self.is_cancelled() {
                return;
            }
            let changed = self.inner.changed.notified();
            if self.is_cancelled() {
                return;
            }
            changed.await;
        }
    }
}

async fn cancelable<T, Fut>(token: Option<CancelToken>, future: Fut) -> RwResult<T>
where
    Fut: Future<Output = RwResult<T>>,
{
    let Some(token) = token else {
        return future.await;
    };
    tokio::select! {
        biased;
        () = token.cancelled() => Err(RwError::Cancelled),
        result = future => result,
    }
}

async fn cancelable_navigation<T, Fut>(
    client: Arc<CdpClient>,
    session_id: String,
    token: Option<CancelToken>,
    future: Fut,
) -> RwResult<T>
where
    Fut: Future<Output = RwResult<T>>,
{
    let Some(token) = token else {
        return future.await;
    };
    tokio::select! {
        biased;
        () = token.cancelled() => {
            let _ = tokio::time::timeout(
                Duration::from_millis(100),
                client.send(
                    "Page.stopLoading",
                    json!({}),
                    Some(&session_id),
                    Duration::from_millis(100),
                ),
            )
            .await;
            Err(RwError::Cancelled)
        }
        result = future => result,
    }
}

#[derive(Clone, Debug)]
struct CloseOutcome {
    error: Option<String>,
}

impl CloseOutcome {
    fn from_result(result: &RwResult<()>) -> Self {
        Self {
            error: result.as_ref().err().map(ToString::to_string),
        }
    }

    fn into_result(self) -> RwResult<()> {
        self.error
            .map_or(Ok(()), |error| Err(RwError::Message(error)))
    }
}

enum ClosePhase {
    Open,
    Closing(watch::Sender<Option<CloseOutcome>>),
    Closed(CloseOutcome),
}

struct CloseLifecycle {
    phase: Mutex<ClosePhase>,
}

enum CloseStart {
    Lead(watch::Sender<Option<CloseOutcome>>),
    Wait(watch::Receiver<Option<CloseOutcome>>),
    Done(CloseOutcome),
}

impl CloseLifecycle {
    fn new() -> Self {
        Self {
            phase: Mutex::new(ClosePhase::Open),
        }
    }

    fn start(&self) -> CloseStart {
        let mut phase = self.phase.lock().unwrap();
        match &*phase {
            ClosePhase::Open => {
                let (sender, _) = watch::channel(None);
                *phase = ClosePhase::Closing(sender.clone());
                CloseStart::Lead(sender)
            }
            ClosePhase::Closing(sender) => CloseStart::Wait(sender.subscribe()),
            ClosePhase::Closed(outcome) => CloseStart::Done(outcome.clone()),
        }
    }

    fn finish(
        &self,
        sender: watch::Sender<Option<CloseOutcome>>,
        result: &RwResult<()>,
        close_on_error: bool,
    ) {
        let outcome = CloseOutcome::from_result(result);
        {
            let mut phase = self.phase.lock().unwrap();
            if result.is_ok() || close_on_error {
                *phase = ClosePhase::Closed(outcome.clone());
            } else {
                *phase = ClosePhase::Open;
            }
        }
        sender.send_replace(Some(outcome));
    }

    fn is_closed(&self) -> bool {
        matches!(*self.phase.lock().unwrap(), ClosePhase::Closed(_))
    }

    fn is_closing_or_closed(&self) -> bool {
        !matches!(*self.phase.lock().unwrap(), ClosePhase::Open)
    }
}

async fn wait_for_close_outcome(
    mut receiver: watch::Receiver<Option<CloseOutcome>>,
) -> RwResult<()> {
    loop {
        if let Some(outcome) = receiver.borrow().clone() {
            return outcome.into_result();
        }
        receiver
            .changed()
            .await
            .map_err(|_| RwError::Message("close operation ended without a result".to_string()))?;
    }
}

async fn single_flight_close<F, Fut>(
    lifecycle: Arc<CloseLifecycle>,
    close_on_error: bool,
    cleanup: F,
) -> RwResult<()>
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = RwResult<()>>,
{
    match lifecycle.start() {
        CloseStart::Done(outcome) => outcome.into_result(),
        CloseStart::Wait(receiver) => wait_for_close_outcome(receiver).await,
        CloseStart::Lead(sender) => {
            let result = cleanup().await;
            lifecycle.finish(sender, &result, close_on_error);
            result
        }
    }
}

struct PendingCommandGuard {
    id: u64,
    pending: CdpPendingMap,
}

struct SpawnedTaskAbortGuard(tokio::task::AbortHandle);

impl PendingCommandGuard {
    fn new(id: u64, pending: &CdpPendingMap) -> Self {
        Self {
            id,
            pending: Arc::clone(pending),
        }
    }
}

impl Drop for PendingCommandGuard {
    fn drop(&mut self) {
        self.pending.lock().unwrap().remove(&self.id);
    }
}

impl Drop for SpawnedTaskAbortGuard {
    fn drop(&mut self) {
        self.0.abort();
    }
}

const CDP_EVENT_LOG_LIMIT: usize = 8192;
const FRAME_UTILITY_WORLD_NAME: &str = "__utility_world__";
// Closed set of structured errors carried across the Python FFI boundary. Each
// marker prefixes exactly one JSON payload schema; user-visible prose is rebuilt
// by the Python shim and never includes these private wire markers.
const ACTION_TIMEOUT_MARKER: &str = "__rustwright_action_timeout__:";
const TIMEOUT_MARKER: &str = "__rustwright_timeout__:";
const TARGET_CLOSED_MARKER: &str = "__rustwright_target_closed__:";
const PAGE_CRASHED_MARKER: &str = "__rustwright_page_crashed__:";
const DISCONNECTED_MARKER: &str = "__rustwright_disconnected__:";
const LOCATOR_TARGET_STATE_TEMPLATE: &str = r#"
if (el && __SCROLL__) el.scrollIntoView({ block: 'center', inline: 'center' });
const ownerDocument = el ? (el.ownerDocument || document) : document;
const ownerWindow = ownerDocument.defaultView || window;
const actionPosition = __ACTION_POSITION__;
const needsReceivesEvents = __RECEIVES_EVENTS__;
const deepElementFromPoint = (x, y) => {
  let hit = ownerDocument.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) {
    const nested = hit.shadowRoot.elementFromPoint(x, y);
    if (!nested || nested === hit) break;
    hit = nested;
  }
  return hit;
};
const targetContains = (node) => {
  let current = node;
  while (current) {
    if (current === el) return true;
    const root = current.getRootNode ? current.getRootNode() : null;
    current = current.parentElement || (root && root.host) || null;
  }
  return false;
};
const snapshot = () => {
  const attached = !!el;
  const rect = el ? el.getBoundingClientRect() : null;
  const point = needsReceivesEvents && rect ? {
    x: Math.min(Math.max(rect.left + (actionPosition ? Number(actionPosition.x || 0) : rect.width / 2), 0), Math.max(ownerWindow.innerWidth - 1, 0)),
    y: Math.min(Math.max(rect.top + (actionPosition ? Number(actionPosition.y || 0) : rect.height / 2), 0), Math.max(ownerWindow.innerHeight - 1, 0)),
  } : null;
  const hit = needsReceivesEvents && el && point ? deepElementFromPoint(point.x, point.y) : null;
  const tagName = el ? String(el.tagName || '').toUpperCase() : '';
  const inputType = tagName === 'INPUT' ? String(el.type || 'text').toLowerCase() : '';
  const nonFillableInputTypes = new Set(['button', 'checkbox', 'file', 'image', 'radio', 'reset', 'submit']);
  const fillableForFill = !!el && (
    (tagName === 'INPUT' && !nonFillableInputTypes.has(inputType)) ||
    tagName === 'TEXTAREA' ||
    el.isContentEditable
  );
  const checkedState = (() => {
    if (!el) return { valid: false, checked: false, indeterminate: false, native_input: false, native_radio: false };
    const checkedRoles = new Set(['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio', 'option', 'treeitem']);
    const role = typeof locatorRoleOf === 'function' ? locatorRoleOf(el) : '';
    const aria = String(el.getAttribute ? el.getAttribute('aria-checked') || '' : '').toLowerCase();
    if (tagName === 'INPUT' && (inputType === 'checkbox' || inputType === 'radio')) {
      const checked = !!el.checked;
      return {
        valid: true,
        checked,
        indeterminate: !!(el.indeterminate && !checked),
        native_input: true,
        native_radio: inputType === 'radio',
      };
    }
    if (!checkedRoles.has(role)) {
      return { valid: false, checked: false, indeterminate: false, native_input: false, native_radio: false };
    }
    if (aria === 'true') return { valid: true, checked: true, indeterminate: false, native_input: false, native_radio: false };
    if (aria === 'false') return { valid: true, checked: false, indeterminate: false, native_input: false, native_radio: false };
    if (aria === 'mixed') return { valid: true, checked: false, indeterminate: true, native_input: false, native_radio: false };
    return { valid: true, checked: false, indeterminate: false, native_input: false, native_radio: false };
  })();
  const visibleState = (() => {
    if (!attached || !el.isConnected) return false;
    if (tagName === 'OPTION') return visible(el);
    const computedStyle = ownerWindow.getComputedStyle(el);
    if (!computedStyle || computedStyle.visibility === 'hidden' || computedStyle.display === 'none') return false;
    return !!rect && rect.width > 0 && rect.height > 0;
  })();
  const disabled = attached && disabledState(el);
  const hasLayout = attached && el.getClientRects().length > 0;
  return {
    count: matches.length,
    frame_strict_violation: strictFrameViolation,
    attached,
    visible: visibleState,
    enabled: attached && !disabled,
    editable: attached && !disabled && !el.readOnly &&
      (el.isContentEditable || /^(INPUT|TEXTAREA)$/.test(el.tagName)),
    tag_name: tagName,
    input_type: inputType,
    is_select: tagName === 'SELECT',
    non_fillable_input: tagName === 'INPUT' && nonFillableInputTypes.has(inputType),
    fillable_for_fill: fillableForFill,
    editable_for_fill: fillableForFill && !disabled && !el.readOnly,
    has_layout: hasLayout,
    checked_valid: checkedState.valid,
    checked: checkedState.checked,
    indeterminate: checkedState.indeterminate,
    native_input: checkedState.native_input,
    native_radio: checkedState.native_radio,
    receives_events: needsReceivesEvents && attached && !!rect && rect.width > 0 && rect.height > 0 && targetContains(hit),
    rect: rect ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height } : null,
  };
};
const first = snapshot();
if (!__STABLE__ || !first.attached) return first;
const style = ownerWindow.getComputedStyle(el);
const zeroTime = value => String(value || '').split(',').every(part => {
  const text = part.trim();
  if (!text) return true;
  if (text.endsWith('ms')) return Number.parseFloat(text) === 0;
  if (text.endsWith('s')) return Number.parseFloat(text) === 0;
  return Number.parseFloat(text) === 0;
});
const hasNoCssMotion = style &&
  (!__STABLE_POSITION_REQUIRED__ || String(style.position || 'static') === 'static') &&
  (String(style.animationName || 'none') === 'none' || zeroTime(style.animationDuration)) &&
  zeroTime(style.animationDelay) &&
  zeroTime(style.transitionDuration) &&
  zeroTime(style.transitionDelay);
if (hasNoCssMotion) {
  first.stable = true;
  return first;
}
return new Promise(resolve => {
  const finish = () => {
    const second = snapshot();
    const left = first.rect;
    const right = second.rect;
    second.stable = !!left && !!right && ["x", "y", "width", "height"].every(
      key => Math.abs(Number(left[key] || 0) - Number(right[key] || 0)) <= 0.5
    );
    resolve(second);
  };
  ownerWindow.setTimeout(finish, 20);
});
"#;
const LOCATOR_FILL_TEMPLATE: &str = r#"
const info = {
  count: matches.length,
  frame_strict_violation: strictFrameViolation,
  attached: !!el,
};
const strict = __STRICT__;
if (strict && (strictFrameViolation || matches.length > 1)) {
  return { ok: false, type: 'strict', info };
}
if (!el) return { ok: false, type: 'pending', info };
const value = __VALUE__;
const forced = __FORCED__;
const nonFillableInputTypes = new Set(['button', 'checkbox', 'file', 'image', 'radio', 'reset', 'submit']);
const tagName = String(el.tagName || '').toUpperCase();
const inputType = tagName === 'INPUT' ? String(el.type || 'text').toLowerCase() : '';
info.visible = visible(el);
info.enabled = !disabledState(el);
info.tag_name = tagName;
info.input_type = inputType;
info.non_fillable_input = tagName === 'INPUT' && nonFillableInputTypes.has(inputType);
info.is_select = tagName === 'SELECT';
info.fillable_for_fill = tagName === 'INPUT' || tagName === 'TEXTAREA' || el.isContentEditable;
info.editable_for_fill = info.fillable_for_fill && !disabledState(el) && !el.readOnly;
if (tagName === 'INPUT' && nonFillableInputTypes.has(inputType)) {
  return { ok: false, type: 'input-type', inputType, info };
}
if (tagName === 'SELECT') {
  return { ok: false, type: forced ? 'force-non-fillable' : 'select', info };
}
const isFillable = tagName === 'INPUT' || tagName === 'TEXTAREA' || el.isContentEditable;
if (!isFillable) {
  return { ok: false, type: forced ? 'force-non-fillable' : 'non-fillable', info };
}
if (forced && (!visible(el) || disabledState(el) || el.readOnly)) return { ok: true, info };
if (!forced && (!visible(el) || disabledState(el) || el.readOnly)) {
  return { ok: false, type: 'pending', info };
}
if ('value' in el) {
  el.scrollIntoView({ block: 'center', inline: 'center' });
  if (typeof el.focus === 'function') el.focus({ preventScroll: true });
  el.value = value;
  if (value !== '' && el.value !== value) {
    return {
      ok: false,
      type: inputType === 'number' ? 'number-text' : 'malformed',
      value: el.value,
      info,
    };
  }
} else {
  el.scrollIntoView({ block: 'center', inline: 'center' });
  if (typeof el.focus === 'function') el.focus({ preventScroll: true });
  el.textContent = value;
}
el.dispatchEvent(new Event('input', { bubbles: true }));
el.dispatchEvent(new Event('change', { bubbles: true }));
return { ok: true, info };
"#;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum TargetClosedKind {
    Page,
    Context,
    Browser,
    Target,
}

#[derive(Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct ActionTimeoutWirePayload {
    state: String,
    action: String,
    last_info_json: String,
    last_info_key: Option<String>,
}

#[derive(Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct TimeoutWirePayload {
    ms: u64,
}

#[derive(Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct TargetClosedWirePayload {
    kind: TargetClosedKind,
}

#[derive(Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct EmptyWirePayload {}

#[derive(Debug, Eq, PartialEq)]
enum FfiWireError {
    ActionTimeout(ActionTimeoutWirePayload),
    Timeout(TimeoutWirePayload),
    TargetClosed(TargetClosedWirePayload),
    PageCrashed,
    Disconnected,
}

impl FfiWireError {
    fn marker(&self) -> &'static str {
        match self {
            Self::ActionTimeout(_) => ACTION_TIMEOUT_MARKER,
            Self::Timeout(_) => TIMEOUT_MARKER,
            Self::TargetClosed(_) => TARGET_CLOSED_MARKER,
            Self::PageCrashed => PAGE_CRASHED_MARKER,
            Self::Disconnected => DISCONNECTED_MARKER,
        }
    }

    fn wire_message(&self) -> String {
        let payload = match self {
            Self::ActionTimeout(payload) => serde_json::to_string(payload),
            Self::Timeout(payload) => serde_json::to_string(payload),
            Self::TargetClosed(payload) => serde_json::to_string(payload),
            Self::PageCrashed | Self::Disconnected => serde_json::to_string(&EmptyWirePayload {}),
        }
        .expect("FFI wire error payloads are always JSON-serializable");
        format!("{}{payload}", self.marker())
    }

    #[cfg(test)]
    fn parse(message: &str) -> Option<Self> {
        if let Some(payload) = message.strip_prefix(ACTION_TIMEOUT_MARKER) {
            return serde_json::from_str(payload).ok().map(Self::ActionTimeout);
        }
        if let Some(payload) = message.strip_prefix(TIMEOUT_MARKER) {
            return serde_json::from_str(payload).ok().map(Self::Timeout);
        }
        if let Some(payload) = message.strip_prefix(TARGET_CLOSED_MARKER) {
            return serde_json::from_str(payload).ok().map(Self::TargetClosed);
        }
        if let Some(payload) = message.strip_prefix(PAGE_CRASHED_MARKER) {
            serde_json::from_str::<EmptyWirePayload>(payload)
                .ok()
                .map(|_| Self::PageCrashed)
        } else if let Some(payload) = message.strip_prefix(DISCONNECTED_MARKER) {
            serde_json::from_str::<EmptyWirePayload>(payload)
                .ok()
                .map(|_| Self::Disconnected)
        } else {
            None
        }
    }
}

#[derive(Debug)]
pub struct ActionTimeoutError {
    state: &'static str,
    action: &'static str,
    count: u64,
    last_info_json: String,
    last_info_key: Option<&'static str>,
}

impl ActionTimeoutError {
    fn from_raw_json(
        state: &'static str,
        action: &'static str,
        raw_json: String,
        info: &Value,
        info_key: Option<&'static str>,
    ) -> Self {
        let count = info.get("count").and_then(Value::as_u64).unwrap_or(0);
        Self {
            state,
            action,
            count,
            last_info_json: raw_json,
            last_info_key: info_key,
        }
    }

    fn wire_message(&self) -> String {
        FfiWireError::ActionTimeout(ActionTimeoutWirePayload {
            state: self.state.to_string(),
            action: self.action.to_string(),
            last_info_json: self.last_info_json.clone(),
            last_info_key: self.last_info_key.map(ToString::to_string),
        })
        .wire_message()
    }
}

impl std::fmt::Display for ActionTimeoutError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.count == 0 {
            return write!(
                formatter,
                "timed out waiting for locator to be {} while trying to {}; no element matched",
                self.state, self.action
            );
        }
        write!(
            formatter,
            "timed out waiting for locator to be {} while trying to {}; last state was {}",
            self.state, self.action, self.last_info_json
        )
    }
}

impl std::error::Error for ActionTimeoutError {}
const MAX_FRAME_TREE_DEPTH: usize = 256;

#[derive(Debug, Error)]
pub enum RwError {
    #[error("{0}")]
    Message(String),
    #[error("CDP connection failed")]
    ConnectFailed,
    #[error("Protocol error ({method}): {message}")]
    Cdp { method: String, message: String },
    #[error("timed out after {0} ms")]
    Timeout(u64),
    #[error("operation cancelled")]
    Cancelled,
    #[error("target or browser is closed")]
    Closed,
    #[error("target or browser is closed")]
    Disconnected,
    #[error("Target page, context or browser has been closed")]
    TargetClosed(TargetClosedKind),
    #[error("Page crashed")]
    PageCrashed,
    #[error("invalid input: {0}")]
    InvalidInput(String),
    #[error(transparent)]
    ActionTimeout(#[from] ActionTimeoutError),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Reqwest(#[from] reqwest::Error),
    #[error(transparent)]
    WebSocket(#[from] tokio_tungstenite::tungstenite::Error),
}

#[cfg(feature = "python")]
fn py_err(error: RwError) -> PyErr {
    match error {
        RwError::Message(message) => PyRuntimeError::new_err(message),
        RwError::InvalidInput(message) => PyValueError::new_err(message),
        RwError::Timeout(ms) => {
            PyRuntimeError::new_err(FfiWireError::Timeout(TimeoutWirePayload { ms }).wire_message())
        }
        RwError::TargetClosed(kind) => PyRuntimeError::new_err(
            FfiWireError::TargetClosed(TargetClosedWirePayload { kind }).wire_message(),
        ),
        RwError::PageCrashed => PyRuntimeError::new_err(FfiWireError::PageCrashed.wire_message()),
        RwError::Disconnected => PyRuntimeError::new_err(FfiWireError::Disconnected.wire_message()),
        RwError::ActionTimeout(error) => PyRuntimeError::new_err(error.wire_message()),
        other => PyRuntimeError::new_err(other.to_string()),
    }
}

#[cfg(feature = "python")]
#[pyclass(name = "_RustFutureAbort")]
struct PyRustFutureAbort {
    cancellation: RustFutureCancellation,
}

#[cfg(feature = "python")]
enum RustFutureCancellation {
    Tokio(tokio::task::AbortHandle),
    Thread(Arc<AtomicBool>),
}

#[cfg(feature = "python")]
impl RustFutureCancellation {
    fn cancel(&self) {
        match self {
            Self::Tokio(abort_handle) => abort_handle.abort(),
            Self::Thread(cancelled) => cancelled.store(true, Ordering::SeqCst),
        }
    }
}

#[cfg(feature = "python")]
#[pyclass(name = "_RustFutureSettler")]
struct PyRustFutureSettler {
    future: Py<PyAny>,
    method: String,
    value: Py<PyAny>,
    on_delivered: Mutex<Option<Box<dyn FnOnce() + Send + 'static>>>,
}

#[cfg(feature = "python")]
static PYTHON_SETTLEMENTS_ENABLED: AtomicBool = AtomicBool::new(true);

#[cfg(feature = "python")]
fn active_python_settlements() -> &'static (Mutex<usize>, Condvar) {
    static ACTIVE: OnceLock<(Mutex<usize>, Condvar)> = OnceLock::new();
    ACTIVE.get_or_init(|| (Mutex::new(0), Condvar::new()))
}

#[cfg(feature = "python")]
struct PythonSettlementActivity;

#[cfg(feature = "python")]
impl PythonSettlementActivity {
    fn begin() -> Option<Self> {
        let (active, _) = active_python_settlements();
        let mut active = active.lock().unwrap();
        if !PYTHON_SETTLEMENTS_ENABLED.load(Ordering::SeqCst) {
            return None;
        }
        *active += 1;
        Some(Self)
    }
}

#[cfg(feature = "python")]
impl Drop for PythonSettlementActivity {
    fn drop(&mut self) {
        let (active, changed) = active_python_settlements();
        let mut active = active.lock().unwrap();
        *active = active.saturating_sub(1);
        changed.notify_all();
    }
}

#[cfg(feature = "python")]
#[pyclass(name = "_RustShutdownGate")]
struct PyRustShutdownGate;

#[cfg(feature = "python")]
#[pymethods]
impl PyRustShutdownGate {
    fn __call__(&self, py: Python<'_>) {
        let (active, _) = active_python_settlements();
        {
            let _active = active.lock().unwrap();
            PYTHON_SETTLEMENTS_ENABLED.store(false, Ordering::SeqCst);
        }
        py.detach(|| {
            let (active, changed) = active_python_settlements();
            let deadline = Instant::now() + Duration::from_secs(2);
            let mut count = active.lock().unwrap();
            while *count > 0 {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    break;
                }
                let (next, timeout) = changed.wait_timeout(count, remaining).unwrap();
                count = next;
                if timeout.timed_out() {
                    break;
                }
            }
        });
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyRustFutureAbort {
    fn __call__(&self, future: &Bound<'_, PyAny>) -> PyResult<()> {
        if future.call_method0("cancelled")?.extract::<bool>()? {
            self.cancellation.cancel();
        }
        Ok(())
    }
}

#[cfg(feature = "python")]
impl Drop for PyRustFutureAbort {
    fn drop(&mut self) {
        // asyncio schedules done callbacks. If Future.cancel() is immediately followed by
        // loop.close(), the loop discards that callback queue. Dropping the queued callback is
        // therefore the final cancellation signal and must abort the Rust work synchronously.
        self.cancellation.cancel();
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyRustFutureSettler {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let future = self.future.bind(py);
        if future.call_method0("done")?.extract::<bool>()? {
            return Ok(());
        }
        future.call_method1(self.method.as_str(), (self.value.bind(py),))?;
        if let Some(on_delivered) = self.on_delivered.lock().unwrap().take() {
            on_delivered();
        }
        Ok(())
    }
}

#[cfg(feature = "python")]
struct PythonFutureOutput {
    value: Py<PyAny>,
    on_delivered: Option<Box<dyn FnOnce() + Send + 'static>>,
}

#[cfg(feature = "python")]
fn settle_python_future_output<T, C>(
    event_loop: Py<PyAny>,
    future: Py<PyAny>,
    result: RwResult<T>,
    convert: C,
) where
    T: Send + 'static,
    C: for<'py> FnOnce(Python<'py>, T) -> PyResult<PythonFutureOutput> + Send + 'static,
{
    let Some(_activity) = PythonSettlementActivity::begin() else {
        // Python references cannot be decremented safely after orderly interpreter shutdown has
        // begun. Leaking these final process-lifetime references is preferable to reattaching.
        std::mem::forget(event_loop);
        std::mem::forget(future);
        return;
    };
    let mut event_loop = Some(event_loop);
    let mut future = Some(future);
    let _ = Python::try_attach(|py| {
        let event_loop = event_loop.take().unwrap();
        let future = future.take().unwrap();
        let cancelled = future
            .bind(py)
            .call_method0("cancelled")
            .and_then(|value| value.extract::<bool>())
            .unwrap_or(false);
        if cancelled {
            return;
        }
        let settled = match result {
            Ok(value) => convert(py, value).map(|output| ("set_result", output)),
            Err(error) => Err(py_err(error)),
        };
        let (method, output) = match settled {
            Ok(value) => value,
            Err(error) => (
                "set_exception",
                PythonFutureOutput {
                    value: error.into_value(py).into_any(),
                    on_delivered: None,
                },
            ),
        };
        let callback = match Py::new(
            py,
            PyRustFutureSettler {
                future,
                method: method.to_string(),
                value: output.value,
                on_delivered: Mutex::new(output.on_delivered),
            },
        ) {
            Ok(callback) => callback,
            Err(error) => {
                error.write_unraisable(py, Some(event_loop.bind(py)));
                return;
            }
        };
        let fallback_future = callback.borrow(py).future.clone_ref(py);
        if let Err(error) = event_loop
            .bind(py)
            .call_method1("call_soon_threadsafe", (callback,))
        {
            let fallback = fallback_future.bind(py);
            let already_settled = fallback
                .call_method0("done")
                .and_then(|value| value.extract::<bool>())
                .unwrap_or(false);
            if !already_settled {
                let _ = fallback.call_method0("cancel");
            }
            let was_settled = fallback
                .call_method0("done")
                .and_then(|value| value.extract::<bool>())
                .unwrap_or(false);
            if !was_settled {
                eprintln!(
                    "rustwright: native future could not be delivered to its owner event loop"
                );
                error.write_unraisable(py, Some(event_loop.bind(py)));
            }
        }
    });
    if let Some(event_loop) = event_loop.take() {
        std::mem::forget(event_loop);
    }
    if let Some(future) = future.take() {
        std::mem::forget(future);
    }
}

#[cfg(feature = "python")]
fn settle_python_future<T, C>(
    event_loop: Py<PyAny>,
    future: Py<PyAny>,
    result: RwResult<T>,
    convert: C,
) where
    T: Send + 'static,
    C: for<'py> FnOnce(Python<'py>, T) -> PyResult<Py<PyAny>> + Send + 'static,
{
    settle_python_future_output(event_loop, future, result, move |py, value| {
        convert(py, value).map(|value| PythonFutureOutput {
            value,
            on_delivered: None,
        })
    });
}

#[cfg(feature = "python")]
fn python_future_on<F, T, C>(
    py: Python<'_>,
    handle: tokio::runtime::Handle,
    rust_future: F,
    convert: C,
) -> PyResult<Py<PyAny>>
where
    F: Future<Output = RwResult<T>> + Send + 'static,
    T: Send + 'static,
    C: for<'py> FnOnce(Python<'py>, T) -> PyResult<Py<PyAny>> + Send + 'static,
{
    let asyncio = PyModule::import(py, "asyncio")?;
    let event_loop = asyncio.call_method0("get_running_loop")?.unbind();
    let future = event_loop.call_method0(py, "create_future")?;
    let returned = future.clone_ref(py);
    let task = handle.spawn(async move {
        let result = rust_future.await;
        settle_python_future(event_loop, future, result, convert);
    });
    let aborter = Py::new(
        py,
        PyRustFutureAbort {
            cancellation: RustFutureCancellation::Tokio(task.abort_handle()),
        },
    )?;
    if let Err(error) = returned
        .bind(py)
        .call_method1("add_done_callback", (aborter,))
    {
        task.abort();
        return Err(error);
    }
    Ok(returned)
}

#[cfg(feature = "python")]
fn python_future_on_with_delivery<F, T, C>(
    py: Python<'_>,
    handle: tokio::runtime::Handle,
    rust_future: F,
    convert: C,
) -> PyResult<Py<PyAny>>
where
    F: Future<Output = RwResult<T>> + Send + 'static,
    T: Send + 'static,
    C: for<'py> FnOnce(Python<'py>, T) -> PyResult<PythonFutureOutput> + Send + 'static,
{
    let asyncio = PyModule::import(py, "asyncio")?;
    let event_loop = asyncio.call_method0("get_running_loop")?.unbind();
    let future = event_loop.call_method0(py, "create_future")?;
    let returned = future.clone_ref(py);
    let task = handle.spawn(async move {
        let result = rust_future.await;
        settle_python_future_output(event_loop, future, result, convert);
    });
    let aborter = Py::new(
        py,
        PyRustFutureAbort {
            cancellation: RustFutureCancellation::Tokio(task.abort_handle()),
        },
    )?;
    if let Err(error) = returned
        .bind(py)
        .call_method1("add_done_callback", (aborter,))
    {
        task.abort();
        return Err(error);
    }
    Ok(returned)
}

#[cfg(feature = "python")]
fn python_future_on_thread<F, T, C>(py: Python<'_>, work: F, convert: C) -> PyResult<Py<PyAny>>
where
    F: FnOnce(Arc<AtomicBool>) -> RwResult<T> + Send + 'static,
    T: Send + 'static,
    C: for<'py> FnOnce(Python<'py>, T) -> PyResult<Py<PyAny>> + Send + 'static,
{
    let asyncio = PyModule::import(py, "asyncio")?;
    let event_loop = asyncio.call_method0("get_running_loop")?.unbind();
    let future = event_loop.call_method0(py, "create_future")?;
    let returned = future.clone_ref(py);
    let cancelled = Arc::new(AtomicBool::new(false));
    let thread_cancelled = Arc::clone(&cancelled);
    std::thread::Builder::new()
        .name("rustwright-native-launch".to_string())
        .spawn(move || settle_python_future(event_loop, future, work(thread_cancelled), convert))
        .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
    let aborter = Py::new(
        py,
        PyRustFutureAbort {
            cancellation: RustFutureCancellation::Thread(cancelled),
        },
    )?;
    returned
        .bind(py)
        .call_method1("add_done_callback", (aborter,))?;
    Ok(returned)
}

fn mouse_event_payload(
    event_type: &str,
    x: f64,
    y: f64,
    button: &str,
    buttons: i64,
    click_count: i64,
    modifiers: i64,
) -> Value {
    json!({
        "type": event_type,
        "x": x,
        "y": y,
        "button": button,
        "buttons": buttons,
        "clickCount": click_count,
        "modifiers": modifiers,
    })
}

fn mouse_event_payload_json(
    event_type: &str,
    x: f64,
    y: f64,
    button: &str,
    buttons: i64,
    click_count: i64,
    modifiers: i64,
) -> RwResult<String> {
    Ok(format!(
        "{{\"type\":{},\"x\":{},\"y\":{},\"button\":{},\"buttons\":{},\"clickCount\":{},\"modifiers\":{}}}",
        serde_json::to_string(event_type)?,
        serde_json::to_string(&x)?,
        serde_json::to_string(&y)?,
        serde_json::to_string(button)?,
        buttons,
        click_count,
        modifiers
    ))
}

#[allow(clippy::too_many_arguments)]
fn mouse_click_batch_json(
    start_x: f64,
    start_y: f64,
    target_x: f64,
    target_y: f64,
    step_count: u32,
    button: &str,
    button_mask: i64,
    click_count: i64,
    initial_buttons: i64,
    modifiers: i64,
) -> RwResult<Vec<String>> {
    let steps = step_count.max(1);
    let mut events = Vec::with_capacity(steps as usize + click_count.max(0) as usize * 2);
    for index in 1..=steps {
        let fraction = index as f64 / steps as f64;
        let x = start_x + (target_x - start_x) * fraction;
        let y = start_y + (target_y - start_y) * fraction;
        events.push(mouse_event_payload_json(
            "mouseMoved",
            x,
            y,
            "none",
            initial_buttons,
            0,
            modifiers,
        )?);
    }
    if click_count > 0 {
        for count in 1..=click_count {
            events.push(mouse_event_payload_json(
                "mousePressed",
                target_x,
                target_y,
                button,
                initial_buttons | button_mask,
                count,
                modifiers,
            )?);
            events.push(mouse_event_payload_json(
                "mouseReleased",
                target_x,
                target_y,
                button,
                initial_buttons & !button_mask,
                count,
                modifiers,
            )?);
        }
    }
    Ok(events)
}

fn chromium_permission_mapping(
    permission: &str,
    fallback: bool,
) -> Option<&'static [&'static str]> {
    match permission {
        "geolocation" => Some(&["geolocation"]),
        "midi" => Some(&["midi"]),
        "notifications" => Some(&["notifications"]),
        "camera" => Some(&["videoCapture"]),
        "microphone" => Some(&["audioCapture"]),
        "background-sync" => Some(&["backgroundSync"]),
        "ambient-light-sensor" => Some(&["sensors"]),
        "accelerometer" => Some(&["sensors"]),
        "gyroscope" => Some(&["sensors"]),
        "magnetometer" => Some(&["sensors"]),
        "clipboard-read" => Some(&["clipboardReadWrite"]),
        "clipboard-write" => Some(&["clipboardSanitizedWrite"]),
        "payment-handler" => Some(&["paymentHandler"]),
        "midi-sysex" => Some(&["midiSysex"]),
        "storage-access" => Some(&["storageAccess"]),
        "local-fonts" => Some(&["localFonts"]),
        "local-network-access" if fallback => Some(&["localNetworkAccess"]),
        "local-network-access" => Some(&["localNetworkAccess", "localNetwork", "loopbackNetwork"]),
        "screen-wake-lock" => Some(&["wakeLockScreen"]),
        _ => None,
    }
}

fn map_chromium_permissions(permissions: &Value, fallback: bool) -> RwResult<Value> {
    let items = permissions
        .as_array()
        .ok_or_else(|| RwError::Message("permissions must be an array".to_string()))?;
    let mut mapped = Vec::new();
    for item in items {
        let permission = item
            .as_str()
            .ok_or_else(|| RwError::Message("permissions must be strings".to_string()))?;
        let protocol_permissions = chromium_permission_mapping(permission, fallback)
            .ok_or_else(|| RwError::Message(format!("Unknown permission: {permission}")))?;
        for protocol_permission in protocol_permissions {
            mapped.push(Value::String((*protocol_permission).to_string()));
        }
    }
    Ok(Value::Array(mapped))
}

#[cfg(test)]
mod tests {
    use super::*;

    use tokio_tungstenite::tungstenite::client::IntoClientRequest;

    #[test]
    fn default_timeout_register_unset_preserves_command_timeout_default() {
        let register = DefaultTimeoutRegister::default();

        assert_eq!(register.resolve(None, false), None);
        assert_eq!(register.resolve(None, true), None);
        assert_eq!(
            BrowserInner::command_timeout(register.resolve(None, false)),
            Duration::from_millis(30_000)
        );
        assert_eq!(
            BrowserInner::command_timeout(register.resolve(None, true)),
            Duration::from_millis(30_000)
        );
    }

    #[test]
    fn default_timeout_register_general_precedence_lattice() {
        let mut register = DefaultTimeoutRegister {
            general: DefaultTimeoutSlots {
                page_default: Some(2_000.0),
                context_default: Some(3_000.0),
            },
            navigation: DefaultTimeoutSlots {
                page_default: Some(4_000.0),
                context_default: Some(5_000.0),
            },
        };

        assert_eq!(register.resolve(Some(1_000.0), false), Some(1_000.0));
        assert_eq!(register.resolve(None, false), Some(2_000.0));

        register.general.page_default = None;
        assert_eq!(register.resolve(None, false), Some(3_000.0));

        register.general.context_default = None;
        assert_eq!(register.resolve(None, false), None);
    }

    #[test]
    fn default_timeout_register_navigation_precedence_lattice() {
        let mut register = DefaultTimeoutRegister {
            general: DefaultTimeoutSlots {
                page_default: Some(4_000.0),
                context_default: Some(5_000.0),
            },
            navigation: DefaultTimeoutSlots {
                page_default: Some(2_000.0),
                context_default: Some(3_000.0),
            },
        };

        assert_eq!(register.resolve(Some(1_000.0), true), Some(1_000.0));
        assert_eq!(register.resolve(None, true), Some(2_000.0));

        register.navigation.page_default = None;
        assert_eq!(register.resolve(None, true), Some(3_000.0));

        register.navigation.context_default = None;
        assert_eq!(register.resolve(None, true), Some(4_000.0));

        register.general.page_default = None;
        assert_eq!(register.resolve(None, true), Some(5_000.0));

        register.general.context_default = None;
        assert_eq!(register.resolve(None, true), None);
    }

    #[tokio::test]
    async fn cancel_token_interrupts_an_async_wait() {
        let token = CancelToken::new();
        let signal = token.clone();
        let started = Instant::now();
        let cancellation = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(10)).await;
            signal.cancel();
        });
        let result = cancelable(Some(token), std::future::pending::<RwResult<()>>()).await;
        cancellation.await.unwrap();
        assert!(matches!(result, Err(RwError::Cancelled)));
        assert!(started.elapsed() < Duration::from_millis(250));
    }

    #[test]
    fn locator_wait_context_loss_classification_is_narrow() {
        for message in [
            "Inspected target navigated or closed",
            "Execution context was destroyed.",
            "Cannot find context with specified id",
            "Session with given id not found.",
            "CDP session is detached",
            "No frame for given id found",
        ] {
            assert!(is_locator_wait_context_loss(&RwError::Cdp {
                method: "Runtime.evaluate".to_string(),
                message: message.to_string(),
            }));
        }

        for message in [
            "Target closed",
            "Session closed",
            "CDP websocket is closed",
            "Invalid selector",
        ] {
            assert!(!is_locator_wait_context_loss(&RwError::Cdp {
                method: "Runtime.evaluate".to_string(),
                message: message.to_string(),
            }));
        }
        assert!(!is_locator_wait_context_loss(&RwError::Message(
            "Execution context was destroyed.".to_string()
        )));
        assert!(!is_locator_wait_context_loss(&RwError::Timeout(100)));
    }

    #[test]
    fn cached_main_frame_url_prefers_last_navigated_url() {
        let mut state = PageFrameState::new("session-main".to_string());

        // No main frame known yet -> caller must fall back to the live probe.
        assert_eq!(resolve_cached_main_frame_url(None, &state), None);

        // Frame attached but no committed url yet -> still fall back.
        state.record_frame(
            "MAIN".to_string(),
            None,
            None,
            None,
            "session-main".to_string(),
        );
        assert_eq!(resolve_cached_main_frame_url(Some("MAIN"), &state), None);

        // After Page.frameNavigated the cached url is served with no round-trip.
        state.record_frame(
            "MAIN".to_string(),
            None,
            None,
            Some("https://example.com/".to_string()),
            "session-main".to_string(),
        );
        assert_eq!(
            resolve_cached_main_frame_url(Some("MAIN"), &state),
            Some("https://example.com/".to_string()),
        );

        // A subsequent navigation updates the cached value (Playwright parity).
        state.record_frame(
            "MAIN".to_string(),
            None,
            None,
            Some("https://example.com/submit".to_string()),
            "session-main".to_string(),
        );
        assert_eq!(
            resolve_cached_main_frame_url(Some("MAIN"), &state),
            Some("https://example.com/submit".to_string()),
        );
    }

    #[test]
    fn nested_oopif_attachment_preserves_parent_session_root() {
        let mut state = PageFrameState::new("session-main".to_string());
        state.record_frame(
            "main".to_string(),
            None,
            None,
            None,
            "session-main".to_string(),
        );
        state.record_session_for_frame("outer", "session-outer");
        state.record_frame(
            "outer".to_string(),
            Some("main".to_string()),
            None,
            None,
            "session-outer".to_string(),
        );

        state.record_frame(
            "inner".to_string(),
            Some("outer".to_string()),
            None,
            None,
            "session-outer".to_string(),
        );
        assert_eq!(
            state
                .session_frames
                .get("session-outer")
                .map(String::as_str),
            Some("outer")
        );
        assert_eq!(
            state.frame_sessions.get("inner").map(String::as_str),
            Some("session-outer")
        );
        assert!(state.frames.contains_key("outer"));

        state.record_session_for_frame("inner", "session-inner");
        assert_eq!(
            state
                .session_frames
                .get("session-outer")
                .map(String::as_str),
            Some("outer")
        );
        assert_eq!(
            state
                .session_frames
                .get("session-inner")
                .map(String::as_str),
            Some("inner")
        );
        assert_eq!(state.frames["inner"].parent_id.as_deref(), Some("outer"));

        state.record_frame(
            "leaf".to_string(),
            Some("inner".to_string()),
            None,
            None,
            "session-inner".to_string(),
        );
        state.detach_session("session-inner");
        assert!(!state.session_frames.contains_key("session-inner"));
        assert_eq!(
            state.frame_sessions.get("inner").map(String::as_str),
            Some("session-outer")
        );
        assert_eq!(
            state.frame_sessions.get("leaf").map(String::as_str),
            Some("session-outer")
        );
        assert_eq!(state.frames["inner"].session_id.as_str(), "session-outer");
        assert_eq!(state.frames["leaf"].session_id.as_str(), "session-outer");
        assert_eq!(
            state
                .session_frames
                .get("session-outer")
                .map(String::as_str),
            Some("outer")
        );
    }

    fn request_target(endpoint: &str) -> String {
        let mut request = endpoint.into_client_request().unwrap();
        ensure_ws_request_path(&mut request);
        request
            .uri()
            .path_and_query()
            .map(|paq| paq.as_str().to_string())
            .unwrap_or_default()
    }

    #[test]
    fn empty_path_ws_endpoint_gets_origin_form_target() {
        // Anchor Browser's CDP URL shape: empty path, query-only.
        assert_eq!(
            request_target("wss://connect.example.io?apiKey=secret&sessionId=abc"),
            "/?apiKey=secret&sessionId=abc"
        );
        // No query, empty path -> bare "/".
        assert_eq!(request_target("wss://connect.example.io"), "/");
    }

    #[test]
    fn non_empty_path_ws_endpoint_is_unchanged() {
        assert_eq!(
            request_target("ws://127.0.0.1:9222/devtools/browser/abc?token=xyz"),
            "/devtools/browser/abc?token=xyz"
        );
        assert_eq!(
            request_target("ws://127.0.0.1:9222/devtools/browser/abc"),
            "/devtools/browser/abc"
        );
    }

    #[test]
    fn chromium_permissions_match_playwright_protocol_names() {
        let mapped = map_chromium_permissions(
            &json!([
                "geolocation",
                "camera",
                "microphone",
                "clipboard-read",
                "clipboard-write",
                "local-network-access",
                "screen-wake-lock"
            ]),
            false,
        )
        .unwrap();

        assert_eq!(
            mapped,
            json!([
                "geolocation",
                "videoCapture",
                "audioCapture",
                "clipboardReadWrite",
                "clipboardSanitizedWrite",
                "localNetworkAccess",
                "localNetwork",
                "loopbackNetwork",
                "wakeLockScreen"
            ])
        );
    }

    #[test]
    fn chromium_permissions_support_local_network_fallback() {
        let mapped = map_chromium_permissions(&json!(["local-network-access"]), true).unwrap();

        assert_eq!(mapped, json!(["localNetworkAccess"]));
    }

    #[test]
    fn chromium_permissions_reject_unknown_playwright_permission() {
        let error = map_chromium_permissions(&json!(["camera", "unknown-permission"]), false)
            .unwrap_err()
            .to_string();

        assert_eq!(error, "Unknown permission: unknown-permission");
    }

    #[test]
    fn chromium_launch_failure_message_includes_stderr_tail() {
        let stderr = NamedTempFile::new().unwrap();
        fs::write(
            stderr.path(),
            format!("{}launch failed: synthetic stderr", "x".repeat(5000)),
        )
        .unwrap();

        let message = chromium_launch_failure_message(None, None, stderr.path());

        assert!(message.contains("Failed to launch chromium"));
        assert!(
            message.contains("Chromium did not expose a CDP endpoint before the launch timeout.")
        );
        assert!(message.contains("launch failed: synthetic stderr"));
        assert!(!message.contains(&"x".repeat(5000)));
    }

    #[test]
    fn remote_debugging_port_args_support_fixed_and_dynamic_values() {
        assert_eq!(
            remote_debugging_port_from_args(&["--remote-debugging-port=9222".to_string()]).unwrap(),
            Some(9222)
        );
        assert_eq!(
            remote_debugging_port_from_args(&[
                "--first".to_string(),
                "--remote-debugging-port".to_string(),
                "0".to_string()
            ])
            .unwrap(),
            Some(0)
        );
        assert_eq!(
            remote_debugging_port_from_args(&["--other".to_string()]).unwrap(),
            None
        );
        assert!(
            remote_debugging_port_from_args(&["--remote-debugging-port=bad".to_string()]).is_err()
        );
    }

    #[test]
    fn macos_mach_port_launch_failure_is_retryable_in_headless_mode() {
        let options = LaunchOptions {
            headless: true,
            args: Vec::new(),
            ..Default::default()
        };
        let error = RwError::Message(
            "Chromium stderr:\n[FATAL:mach_port_rendezvous_mac.cc] MachPortRendezvousServer"
                .to_string(),
        );

        assert_eq!(
            should_retry_chromium_single_process(&options, &error),
            cfg!(target_os = "macos")
        );
    }

    #[test]
    fn macos_sandbox_parameter_launch_failure_is_retryable_in_headless_mode() {
        let options = LaunchOptions {
            headless: true,
            chromium_sandbox: true,
            args: Vec::new(),
            ..Default::default()
        };
        let error = RwError::Message(
            "Chromium stderr:\n[FATAL:content/browser/sandbox_parameters_mac.mm:67]".to_string(),
        );

        assert_eq!(
            should_retry_chromium_single_process(&options, &error),
            cfg!(target_os = "macos")
        );
    }

    #[test]
    fn explicit_single_process_arg_disables_macos_retry() {
        let options = LaunchOptions {
            headless: true,
            args: vec!["--single-process".to_string()],
            ..Default::default()
        };
        let error = RwError::Message("MachPortRendezvousServer".to_string());

        assert!(!should_retry_chromium_single_process(&options, &error));
    }

    #[test]
    fn simple_css_locator_fast_path_only_accepts_plain_css_specs() {
        assert!(simple_css_locator_spec(&json!({
            "kind": "css",
            "selector": "#email"
        })));
        assert!(simple_css_locator_spec(&json!({
            "kind": "nth",
            "base": { "kind": "css", "selector": "li" },
            "index": 2
        })));
        assert!(!simple_css_locator_spec(&json!({
            "kind": "css",
            "selector": "button",
            "has_text": "Save"
        })));
        assert!(!simple_css_locator_spec(&json!({
            "kind": "text",
            "text": { "text": "Save", "exact": false }
        })));
        assert!(!body_requires_full_locator_runtime(
            "return !!el && visible(el);"
        ));
        assert!(body_requires_full_locator_runtime(
            "return referencedText(el, 'aria-labelledby');"
        ));
    }

    #[test]
    fn native_actionability_success_requires_every_click_state() {
        let actionable = json!({
            "attached": true,
            "visible": true,
            "enabled": true,
            "receives_events": true,
            "stable": true,
        });
        assert!(actionability_state_succeeds(&actionable));
        for field in [
            "attached",
            "visible",
            "enabled",
            "receives_events",
            "stable",
        ] {
            let mut pending = actionable.clone();
            pending[field] = Value::Bool(false);
            assert!(!actionability_state_succeeds(&pending));
        }
    }

    #[test]
    fn native_action_timeout_formats_sync_messages_and_structured_payload() {
        let missing_json = r#"{"count":0,"attached":false}"#.to_string();
        let missing_info = serde_json::from_str::<Value>(&missing_json).unwrap();
        let missing = ActionTimeoutError::from_raw_json(
            "actionable",
            "click",
            missing_json,
            &missing_info,
            None,
        );
        assert_eq!(
            missing.to_string(),
            "timed out waiting for locator to be actionable while trying to click; no element matched"
        );
        assert!(missing.wire_message().starts_with(ACTION_TIMEOUT_MARKER));

        let pending_json = r#"{"count":1,"attached":true,"visible":false}"#.to_string();
        let pending_info = serde_json::from_str::<Value>(&pending_json).unwrap();
        let pending = ActionTimeoutError::from_raw_json(
            "editable",
            "fill",
            pending_json.clone(),
            &pending_info,
            None,
        );
        assert_eq!(
            pending.to_string(),
            format!(
                "timed out waiting for locator to be editable while trying to fill; last state was {pending_json}"
            )
        );
    }

    #[test]
    fn ffi_wire_error_markers_round_trip_with_closed_payload_schemas() {
        let cases = vec![
            (
                FfiWireError::ActionTimeout(ActionTimeoutWirePayload {
                    state: "actionable".to_string(),
                    action: "click".to_string(),
                    last_info_json: r#"{"count":0}"#.to_string(),
                    last_info_key: None,
                }),
                r#"__rustwright_action_timeout__:{"state":"actionable","action":"click","last_info_json":"{\"count\":0}","last_info_key":null}"#,
            ),
            (
                FfiWireError::Timeout(TimeoutWirePayload { ms: 250 }),
                r#"__rustwright_timeout__:{"ms":250}"#,
            ),
            (
                FfiWireError::TargetClosed(TargetClosedWirePayload {
                    kind: TargetClosedKind::Page,
                }),
                r#"__rustwright_target_closed__:{"kind":"page"}"#,
            ),
            (
                FfiWireError::TargetClosed(TargetClosedWirePayload {
                    kind: TargetClosedKind::Context,
                }),
                r#"__rustwright_target_closed__:{"kind":"context"}"#,
            ),
            (
                FfiWireError::TargetClosed(TargetClosedWirePayload {
                    kind: TargetClosedKind::Browser,
                }),
                r#"__rustwright_target_closed__:{"kind":"browser"}"#,
            ),
            (
                FfiWireError::TargetClosed(TargetClosedWirePayload {
                    kind: TargetClosedKind::Target,
                }),
                r#"__rustwright_target_closed__:{"kind":"target"}"#,
            ),
            (
                FfiWireError::PageCrashed,
                r#"__rustwright_page_crashed__:{}"#,
            ),
            (
                FfiWireError::Disconnected,
                r#"__rustwright_disconnected__:{}"#,
            ),
        ];
        let markers = [
            ACTION_TIMEOUT_MARKER,
            TIMEOUT_MARKER,
            TARGET_CLOSED_MARKER,
            PAGE_CRASHED_MARKER,
            DISCONNECTED_MARKER,
        ];
        assert_eq!(
            markers.into_iter().collect::<HashSet<_>>().len(),
            markers.len()
        );

        for (error, expected) in cases {
            let marker = error.marker();
            let message = error.wire_message();
            assert_eq!(message, expected);
            assert_eq!(FfiWireError::parse(&message), Some(error));
            assert!(message.starts_with(marker));
            assert_eq!(
                markers
                    .iter()
                    .map(|item| message.matches(*item).count())
                    .sum::<usize>(),
                1
            );
        }

        assert_eq!(
            FfiWireError::parse(r#"__rustwright_timeout__:{"ms":1,"extra":true}"#),
            None
        );
        assert_eq!(
            FfiWireError::parse(r#"__rustwright_target_closed__:{"kind":"tab"}"#),
            None
        );
        assert_eq!(
            FfiWireError::parse(r#"__rustwright_page_crashed__:{"extra":true}"#),
            None
        );
        assert_eq!(FfiWireError::parse("plain legacy error"), None);
    }

    #[test]
    fn native_fill_result_discriminators_match_sync_errors() {
        assert_eq!(
            classify_fill_attempt(&json!({ "ok": true })).unwrap(),
            FillAttempt::Success
        );
        assert_eq!(
            classify_fill_attempt(&json!({ "ok": false, "type": "pending" })).unwrap(),
            FillAttempt::Pending
        );
        assert_eq!(
            classify_fill_attempt(&json!({
                "ok": false,
                "type": "input-type",
                "inputType": "checkbox",
            }))
            .unwrap_err()
            .to_string(),
            "Locator.fill: Error: Input of type \"checkbox\" cannot be filled"
        );
        assert_eq!(
            classify_fill_attempt(&json!({ "ok": false, "type": "number-text" }))
                .unwrap_err()
                .to_string(),
            "Locator.fill: Error: Cannot type text into input[type=number]"
        );
        assert_eq!(
            classify_fill_attempt(&json!({ "ok": false, "type": "malformed" }))
                .unwrap_err()
                .to_string(),
            "Locator.fill: Error: Malformed value"
        );
        assert_eq!(
            classify_fill_attempt(&json!({ "ok": false, "type": "select" }))
                .unwrap_err()
                .to_string(),
            "Locator.fill: Error: Element is not an <input>, <textarea> or [contenteditable] element"
        );
    }

    #[test]
    fn native_action_results_decode_runtime_serializer_envelopes() {
        let decoded = decode_runtime_serialized_value(json!({
            "__rustwright_cdp_object__": 1,
            "entries": {
                "ok": false,
                "type": "pending",
                "info": {
                    "__rustwright_cdp_object__": 2,
                    "entries": { "count": 1, "attached": true },
                },
            },
        }));
        assert_eq!(decoded["type"], "pending");
        assert_eq!(decoded["info"]["count"], 1);
        assert_eq!(decoded["info"]["attached"], true);
    }

    #[test]
    fn default_native_mouse_batch_matches_sync_sequence() {
        let events = mouse_click_batch_json(5.0, 6.0, 25.0, 30.0, 1, "left", 1, 1, 2, 8)
            .unwrap()
            .into_iter()
            .map(|event| serde_json::from_str::<Value>(&event).unwrap())
            .collect::<Vec<_>>();
        assert_eq!(events.len(), 3);
        assert_eq!(
            events
                .iter()
                .map(|event| event.get("type").and_then(Value::as_str).unwrap())
                .collect::<Vec<_>>(),
            ["mouseMoved", "mousePressed", "mouseReleased"]
        );
        assert_eq!(events[0]["button"], "none");
        assert_eq!(events[0]["buttons"], 2);
        assert_eq!(events[0]["clickCount"], 0);
        assert_eq!(events[1]["button"], "left");
        assert_eq!(events[1]["buttons"], 3);
        assert_eq!(events[1]["clickCount"], 1);
        assert_eq!(events[2]["button"], "left");
        assert_eq!(events[2]["buttons"], 2);
        assert_eq!(events[2]["clickCount"], 1);
        assert!(events.iter().all(|event| event["x"] == 25.0));
        assert!(events.iter().all(|event| event["y"] == 30.0));
        assert!(events.iter().all(|event| event["modifiers"] == 8));
    }

    #[test]
    fn native_frame_owner_specs_and_offsets_accumulate_in_order() {
        let spec = json!({
            "kind": "frame",
            "frame_selector": "iframe.outer",
            "frame_index": 1,
            "inner": {
                "kind": "frame",
                "frame_selector": "iframe.inner",
                "frame_index": 2,
                "inner": { "kind": "css", "selector": "button" },
            },
        });
        let owners = frame_owner_specs_for_point_translation(&spec, None);
        assert_eq!(owners.len(), 2);
        assert_eq!(owners[0].1, 1);
        assert_eq!(owners[0].0["selector"], "iframe.outer");
        assert_eq!(owners[1].1, 2);
        assert_eq!(owners[1].0["kind"], "frame");

        let mut offset = (0.0, 0.0);
        assert!(accumulate_frame_offset(
            &mut offset,
            &json!({ "x": 10.5, "y": 20.25 })
        ));
        assert!(accumulate_frame_offset(
            &mut offset,
            &json!({ "x": 3.0, "y": 4.75 })
        ));
        assert_eq!(offset, (13.5, 25.0));
    }

    #[test]
    fn native_action_timeout_zero_is_path_specific() {
        assert_eq!(
            action_timeout_duration(Some(0.0), true),
            Duration::from_secs(24 * 60 * 60)
        );
        assert_eq!(action_timeout_duration(Some(0.0), false), Duration::ZERO);
        assert_eq!(
            action_poll_timeout(Some(0.0), true, Duration::from_secs(60)),
            Duration::from_secs(1)
        );
        assert_eq!(
            action_poll_timeout(Some(0.0), false, Duration::ZERO),
            Duration::from_millis(1)
        );
    }

    #[test]
    fn native_action_timeouts_sanitize_non_finite_and_huge_values() {
        let disabled = Duration::from_secs(24 * 60 * 60);
        for timeout_ms in [f64::NAN, f64::INFINITY, 1e300, -5.0, 0.0] {
            let click = std::panic::catch_unwind(|| {
                (
                    action_timeout_duration(Some(timeout_ms), true),
                    action_poll_timeout(Some(timeout_ms), true, disabled),
                )
            });
            let (click_window, click_poll) =
                click.unwrap_or_else(|_| panic!("click timeout {timeout_ms:?} panicked"));
            assert_eq!(click_window, disabled);
            assert!((Duration::from_millis(1)..=Duration::from_secs(1)).contains(&click_poll));

            let fill = std::panic::catch_unwind(|| {
                let window = action_timeout_duration(Some(timeout_ms), false);
                let poll = action_poll_timeout(Some(timeout_ms), false, window);
                (window, poll)
            });
            let (fill_window, fill_poll) =
                fill.unwrap_or_else(|_| panic!("fill timeout {timeout_ms:?} panicked"));
            let expected_fill = if timeout_ms.is_finite() && timeout_ms <= 0.0 {
                Duration::ZERO
            } else {
                disabled
            };
            assert_eq!(fill_window, expected_fill);
            if fill_window.is_zero() {
                assert_eq!(fill_poll, Duration::from_millis(1));
            } else {
                assert!((Duration::from_millis(1)..=Duration::from_secs(1)).contains(&fill_poll));
            }
        }
    }

    #[test]
    fn native_action_probe_uses_full_remaining_finite_budget() {
        assert_eq!(
            action_poll_timeout(Some(30_000.0), true, Duration::from_secs(17)),
            Duration::from_secs(17)
        );
        assert_eq!(
            action_poll_timeout(Some(30_000.0), false, Duration::from_millis(12_345)),
            Duration::from_millis(12_345)
        );
    }

    #[test]
    fn native_frame_strict_violation_uses_raw_selector_quoting() {
        let error = strict_violation_error(
            &json!({
                "frame_strict_violation": {
                    "count": 2,
                    "selector": "iframe[title=\"quoted\"]",
                },
            }),
            true,
            "click",
        )
        .unwrap();
        assert_eq!(
            error.to_string(),
            "strict mode violation: locator(\"iframe[title=\"quoted\"]\") resolved to 2 elements"
        );
    }

    #[test]
    fn device_screen_orientation_matches_playwright_chromium_metrics() {
        assert_eq!(
            device_screen_orientation(390, 844, true),
            json!({ "angle": 0, "type": "portraitPrimary" })
        );
        assert_eq!(
            device_screen_orientation(844, 390, true),
            json!({ "angle": 90, "type": "landscapePrimary" })
        );
        assert_eq!(
            device_screen_orientation(390, 844, false),
            json!({ "angle": 0, "type": "landscapePrimary" })
        );
    }

    #[test]
    fn plain_string_script_is_wrapped_in_indirect_eval() {
        // A plain statement script (not a function, no arg) must not be handed
        // to Runtime.evaluate verbatim: top-level `let`/`const` would leak into
        // the global lexical environment and break repeated evaluation. It is
        // instead wrapped in an indirect `eval` that scopes those declarations
        // to the call while preserving the script's completion value.
        let script = "let browserNameForWorkarounds = 'chromium';\nhelper();";
        let wrapped = make_evaluate_expression(script, None);
        assert_eq!(
            wrapped,
            r#"(0, eval)("let browserNameForWorkarounds = 'chromium';\nhelper();")"#
        );
    }

    #[test]
    fn plain_expression_is_wrapped_in_indirect_eval() {
        assert_eq!(
            make_evaluate_expression("1 + 2", None),
            r#"(0, eval)("1 + 2")"#
        );
        assert_eq!(
            make_evaluate_expression("document.title", None),
            r#"(0, eval)("document.title")"#
        );
    }

    #[test]
    fn declaration_helper_script_is_block_scoped_and_exports_functions() {
        let script = r#"
            // helper prelude
            const browserName = "chromium";
            function helper() { return browserName; }
            async function asyncHelper() { return browserName; }
        "#;

        let wrapped = make_evaluate_expression(script, None);

        assert!(wrapped.starts_with("{\n"));
        assert!(!wrapped.contains("(0, eval)"));
        assert!(wrapped.contains("globalThis.helper = helper;"));
        assert!(wrapped.contains("globalThis.asyncHelper = asyncHelper;"));
    }

    #[test]
    fn declaration_without_function_stays_in_indirect_eval() {
        assert_eq!(
            make_evaluate_expression("let localValue = 1; localValue", None),
            r#"(0, eval)("let localValue = 1; localValue")"#
        );
    }

    #[test]
    fn indirect_eval_wrapper_escapes_embedded_quotes_and_newlines() {
        // The source is embedded as a JS string literal, so any quotes,
        // backslashes, or newlines in the script must be escaped safely.
        let script = "const s = \"a\\tb\";\ns;";
        let wrapped = make_evaluate_expression(script, None);
        assert_eq!(wrapped, r#"(0, eval)("const s = \"a\\tb\";\ns;")"#);
    }

    #[test]
    fn function_and_arg_scripts_are_not_double_wrapped_in_eval() {
        // Functions and arg-bearing calls keep their IIFE wrapping, which
        // already scopes their internals; they should not be routed through
        // the indirect-eval branch.
        assert!(!make_evaluate_expression("() => 1", None).contains("(0, eval)"));
        assert!(!make_evaluate_expression("(x) => x + 1", Some("2")).contains("(0, eval)"));
    }

    #[test]
    fn blocking_wrapper_falls_back_when_python_is_unavailable() {
        assert!(
            Python::try_attach(|_| ()).is_none(),
            "cargo test should not initialize the Python interpreter"
        );
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .worker_threads(1)
            .build()
            .unwrap();

        let result = run_blocking_detached(&runtime, async { 42 });

        assert_eq!(result, 42);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn close_lifecycle_shares_one_in_flight_cleanup_and_result() {
        let lifecycle = Arc::new(CloseLifecycle::new());
        let cleanup_count = Arc::new(AtomicU64::new(0));
        let first_lifecycle = Arc::clone(&lifecycle);
        let first_count = Arc::clone(&cleanup_count);
        let first = tokio::spawn(async move {
            single_flight_close(first_lifecycle, false, move || async move {
                first_count.fetch_add(1, Ordering::SeqCst);
                tokio::time::sleep(Duration::from_millis(50)).await;
                Ok(())
            })
            .await
        });
        tokio::task::yield_now().await;
        let second_lifecycle = Arc::clone(&lifecycle);
        let second_count = Arc::clone(&cleanup_count);
        let second = tokio::spawn(async move {
            single_flight_close(second_lifecycle, false, move || async move {
                second_count.fetch_add(1, Ordering::SeqCst);
                Ok(())
            })
            .await
        });

        first.await.unwrap().unwrap();
        second.await.unwrap().unwrap();
        assert_eq!(cleanup_count.load(Ordering::SeqCst), 1);
        assert!(lifecycle.is_closed());
    }

    #[tokio::test]
    async fn drag_interception_timeout_still_disables_and_releases_mouse() {
        let (write_tx, mut write_rx) = mpsc::unbounded_channel();
        let pending = Arc::new(Mutex::new(HashMap::new()));
        let (events, _) = broadcast::channel(4);
        let event_log = Arc::new(Mutex::new(CdpEventLog::new()));
        let (alive_tx, _) = watch::channel(true);
        let client = CdpClient {
            write_tx,
            pending: Arc::clone(&pending),
            events: events.clone(),
            event_log: Arc::clone(&event_log),
            next_id: AtomicU64::new(1),
            sent_runtime_enable_count: AtomicU64::new(0),
            sent_target_close_count: AtomicU64::new(0),
            sent_context_dispose_count: AtomicU64::new(0),
            alive: Arc::new(AtomicBool::new(true)),
            alive_tx,
        };
        let mut drag_events = client.subscribe();
        let respond_pending = Arc::clone(&pending);
        let respond_events = events.clone();
        let respond_event_log = Arc::clone(&event_log);

        let (result, cleanup_commands) = tokio::join!(
            run_drag_with_cleanup(
                &client,
                "root-session",
                "pointer-session",
                30.0,
                40.0,
                0,
                async {
                    wait_for_drag_intercepted(
                        &mut drag_events,
                        "pointer-session",
                        OperationDeadline::new(Duration::from_millis(20)),
                    )
                    .await
                    .map(Some)
                },
                |_| async { Ok(()) },
            ),
            async {
                let mut commands = Vec::new();
                for _ in 0..2 {
                    let command = match write_rx.recv().await.unwrap() {
                        CdpOutgoing::Text(payload) => {
                            serde_json::from_str::<Value>(&payload).unwrap()
                        }
                        CdpOutgoing::Close => panic!("unexpected transport close"),
                    };
                    dispatch_cdp_payload(
                        json!({ "id": command["id"], "result": {} }),
                        Arc::clone(&respond_pending),
                        respond_events.clone(),
                        Arc::clone(&respond_event_log),
                    );
                    commands.push(command);
                }
                commands
            },
        );

        assert!(matches!(result, Err(RwError::Timeout(20))));
        assert_eq!(cleanup_commands[0]["method"], "Input.setInterceptDrags");
        assert_eq!(cleanup_commands[0]["params"]["enabled"], false);
        assert_eq!(cleanup_commands[0]["sessionId"], "root-session");
        assert_eq!(cleanup_commands[1]["method"], "Input.dispatchMouseEvent");
        assert_eq!(cleanup_commands[1]["params"]["type"], "mouseReleased");
        assert_eq!(cleanup_commands[1]["params"]["buttons"], 0);
        assert_eq!(cleanup_commands[1]["sessionId"], "pointer-session");
        assert!(write_rx.try_recv().is_err());
    }

    #[test]
    fn cancelled_create_target_round_trip_closes_orphan_without_closing_success() {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .worker_threads(2)
            .build()
            .unwrap();
        let handle = runtime.handle().clone();
        let (write_tx, mut write_rx) = mpsc::unbounded_channel();
        let pending = Arc::new(Mutex::new(HashMap::new()));
        let (events, _) = broadcast::channel(4);
        let (alive_tx, _) = watch::channel(true);
        let client = Arc::new(CdpClient {
            write_tx,
            pending: Arc::clone(&pending),
            events,
            event_log: Arc::new(Mutex::new(CdpEventLog::new())),
            next_id: AtomicU64::new(1),
            sent_runtime_enable_count: AtomicU64::new(0),
            sent_target_close_count: AtomicU64::new(0),
            sent_context_dispose_count: AtomicU64::new(0),
            alive: Arc::new(AtomicBool::new(true)),
            alive_tx,
        });
        let browser = Arc::new(BrowserInner {
            runtime: OwnedRuntime::new(runtime),
            client: Arc::clone(&client),
            process: Mutex::new(None),
            profile_dir: Mutex::new(None),
            owned: false,
            ws_endpoint: "ws://test.invalid".to_string(),
            stealth_user_agent_override: Mutex::new(None),
            single_process_fallback: false,
            lifecycle: Arc::new(CloseLifecycle::new()),
            attached_pages: AttachedPageRegistry::default(),
        });

        let test_browser = Arc::clone(&browser);
        handle.block_on(async move {
            let cancelled_browser = Arc::clone(&test_browser);
            let cancelled = tokio::spawn(async move {
                create_target_cancellation_safe(cancelled_browser, json!({ "url": "about:blank" }))
                    .await
            });
            let create = match write_rx.recv().await.unwrap() {
                CdpOutgoing::Text(payload) => serde_json::from_str::<Value>(&payload).unwrap(),
                CdpOutgoing::Close => panic!("unexpected transport close"),
            };
            assert_eq!(create["method"], "Target.createTarget");
            cancelled.abort();
            assert!(matches!(cancelled.await, Err(error) if error.is_cancelled()));
            dispatch_cdp_payload(
                json!({ "id": create["id"], "result": { "targetId": "orphan" } }),
                Arc::clone(&pending),
                client.events.clone(),
                Arc::clone(&client.event_log),
            );

            let close = tokio::time::timeout(Duration::from_secs(1), write_rx.recv())
                .await
                .expect("the orphan target should be closed")
                .unwrap();
            let close = match close {
                CdpOutgoing::Text(payload) => serde_json::from_str::<Value>(&payload).unwrap(),
                CdpOutgoing::Close => panic!("unexpected transport close"),
            };
            assert_eq!(close["method"], "Target.closeTarget");
            assert_eq!(close["params"]["targetId"], "orphan");
            dispatch_cdp_payload(
                json!({ "id": close["id"], "result": { "success": true } }),
                Arc::clone(&pending),
                client.events.clone(),
                Arc::clone(&client.event_log),
            );

            let successful_browser = Arc::clone(&test_browser);
            let successful = tokio::spawn(async move {
                create_target_cancellation_safe(successful_browser, json!({ "url": "about:blank" }))
                    .await
            });
            let create = match write_rx.recv().await.unwrap() {
                CdpOutgoing::Text(payload) => serde_json::from_str::<Value>(&payload).unwrap(),
                CdpOutgoing::Close => panic!("unexpected transport close"),
            };
            dispatch_cdp_payload(
                json!({ "id": create["id"], "result": { "targetId": "delivered" } }),
                Arc::clone(&pending),
                client.events.clone(),
                Arc::clone(&client.event_log),
            );
            successful.await.unwrap().unwrap().disarm();
            assert!(write_rx.try_recv().is_err());
        });
        drop(browser);
    }

    #[tokio::test]
    async fn unrelated_page_attachment_reservations_do_not_head_of_line_block() {
        let registry = AttachedPageRegistry::default();
        let slow_lock = match registry.reserve("slow-target") {
            AttachedPageReservation::Attach { attach_lock, .. } => attach_lock,
            AttachedPageReservation::Existing(_) => panic!("unexpected attached page"),
        };
        let _slow_attach = slow_lock.lock().await;

        let fast_lock = match registry.reserve("fast-target") {
            AttachedPageReservation::Attach { attach_lock, .. } => attach_lock,
            AttachedPageReservation::Existing(_) => panic!("unexpected attached page"),
        };
        let _fast_attach = tokio::time::timeout(Duration::from_millis(50), fast_lock.lock())
            .await
            .expect("an unrelated target must use a different attachment lock");
    }

    #[test]
    fn failed_attachment_reservations_do_not_accumulate_target_ids() {
        let registry = AttachedPageRegistry::default();
        for index in 0..100 {
            let target_id = format!("historical-target-{index}");
            let (generation, attach_lock) = match registry.reserve(&target_id) {
                AttachedPageReservation::Attach {
                    generation,
                    attach_lock,
                } => (generation, attach_lock),
                AttachedPageReservation::Existing(_) => panic!("unexpected attached page"),
            };
            registry.remove_reservation(&target_id, generation, &attach_lock);
        }
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn stale_page_removal_does_not_erase_a_new_attachment_reservation() {
        let registry = AttachedPageRegistry::default();
        let old_lock = Arc::new(tokio::sync::Mutex::new(()));
        registry.entries.lock().unwrap().insert(
            "same-target".to_string(),
            AttachedPageEntry {
                generation: 1,
                page: Weak::new(),
                attach_lock: Arc::downgrade(&old_lock),
                registered: false,
            },
        );

        let new_lock = Arc::new(tokio::sync::Mutex::new(()));
        registry.entries.lock().unwrap().insert(
            "same-target".to_string(),
            AttachedPageEntry {
                generation: 2,
                page: Weak::new(),
                attach_lock: Arc::downgrade(&new_lock),
                registered: false,
            },
        );

        let new_page_pointer = registry.entries.lock().unwrap()["same-target"]
            .page
            .as_ptr();
        registry.remove_page("same-target", 1, new_page_pointer);

        let reserved = match registry.reserve("same-target") {
            AttachedPageReservation::Attach { attach_lock, .. } => attach_lock,
            AttachedPageReservation::Existing(_) => panic!("unexpected attached page"),
        };
        assert!(Arc::ptr_eq(&reserved, &new_lock));
    }

    #[tokio::test]
    async fn queued_page_attachment_re_reserves_after_registered_page_drops() {
        let (write_tx, _write_rx) = mpsc::unbounded_channel();
        let (events, _) = broadcast::channel(4);
        let (alive_tx, _) = watch::channel(true);
        let browser = Arc::new(BrowserInner {
            runtime: OwnedRuntime(None),
            client: Arc::new(CdpClient {
                write_tx,
                pending: Arc::new(Mutex::new(HashMap::new())),
                events,
                event_log: Arc::new(Mutex::new(CdpEventLog::new())),
                next_id: AtomicU64::new(1),
                sent_runtime_enable_count: AtomicU64::new(0),
                sent_target_close_count: AtomicU64::new(0),
                sent_context_dispose_count: AtomicU64::new(0),
                alive: Arc::new(AtomicBool::new(true)),
                alive_tx,
            }),
            process: Mutex::new(None),
            profile_dir: Mutex::new(None),
            owned: false,
            ws_endpoint: "ws://test.invalid".to_string(),
            stealth_user_agent_override: Mutex::new(None),
            single_process_fallback: false,
            lifecycle: Arc::new(CloseLifecycle::new()),
            attached_pages: AttachedPageRegistry::default(),
        });
        let make_page = |generation| {
            Arc::new(PageInner {
                browser: Arc::clone(&browser),
                target_id: "same-target".to_string(),
                registry_generation: generation,
                session_id: format!("session-{generation}"),
                context_id: None,
                main_frame_id: Mutex::new(None),
                frame_state: Mutex::new(PageFrameState::new(format!("session-{generation}"))),
                network_requests: Arc::new(Mutex::new(NetworkRequestStore::new(0))),
                event_stream_start_cursor: 0,
                background_override_active: Arc::new(AtomicBool::new(false)),
                screenshot_lock: Arc::new(tokio::sync::Mutex::new(())),
                mouse_dispatch_lock: Arc::new(tokio::sync::Mutex::new(())),
                default_timeouts: Mutex::new(DefaultTimeoutRegister::default()),
                lifecycle: Arc::new(CloseLifecycle::new()),
                target_closed: AtomicBool::new(false),
                crashed: AtomicBool::new(false),
                close_target_on_drop: AtomicBool::new(false),
            })
        };
        let (generation, winner_lock) = match browser.attached_pages.reserve("same-target") {
            AttachedPageReservation::Attach {
                generation,
                attach_lock,
            } => (generation, attach_lock),
            AttachedPageReservation::Existing(_) => panic!("unexpected attached page"),
        };
        let (waiter_generation, waiter_lock) = match browser.attached_pages.reserve("same-target") {
            AttachedPageReservation::Attach {
                generation,
                attach_lock,
            } => (generation, attach_lock),
            AttachedPageReservation::Existing(_) => panic!("unexpected attached page"),
        };
        assert_eq!(waiter_generation, generation);
        assert!(Arc::ptr_eq(&waiter_lock, &winner_lock));

        let winner_guard = winner_lock.lock().await;
        let winner_page = make_page(generation);
        assert!(matches!(
            browser
                .attached_pages
                .register("same-target", &winner_page, generation, &winner_lock),
            AttachedPageRegistration::Registered
        ));

        let waiter_browser = Arc::clone(&browser);
        let waiter_lock_for_task = Arc::clone(&waiter_lock);
        let (acquired_tx, acquired_rx) = oneshot::channel();
        let proceed = Arc::new(tokio::sync::Notify::new());
        let waiter_proceed = Arc::clone(&proceed);
        let waiter = tokio::spawn(async move {
            let _waiter_guard = waiter_lock_for_task.lock().await;
            acquired_tx.send(()).unwrap();
            waiter_proceed.notified().await;
            waiter_browser.attached_pages.claim_after_lock(
                "same-target",
                waiter_generation,
                &waiter_lock_for_task,
            )
        });

        drop(winner_guard);
        acquired_rx.await.unwrap();
        drop(winner_page);
        proceed.notify_one();

        let replacement_generation = match waiter.await.unwrap() {
            AttachedPageClaim::Attach { generation } => generation,
            AttachedPageClaim::Existing(_) => panic!("dropped winner must not remain live"),
            AttachedPageClaim::Retry => panic!("waiter should claim the missing reservation"),
        };
        assert_ne!(replacement_generation, generation);
        let replacement_page = make_page(replacement_generation);
        assert!(matches!(
            browser.attached_pages.register(
                "same-target",
                &replacement_page,
                replacement_generation,
                &waiter_lock,
            ),
            AttachedPageRegistration::Registered
        ));
        match browser.attached_pages.reserve("same-target") {
            AttachedPageReservation::Existing(page) => {
                assert!(Arc::ptr_eq(&page, &replacement_page));
            }
            AttachedPageReservation::Attach { .. } => panic!("queued observer lost its page"),
        }
    }

    #[cfg(feature = "python")]
    #[test]
    fn page_event_lease_rolls_back_and_merges_network_state_transactionally() {
        fn store(url: &str, next_seq: u64) -> NetworkRequestStore {
            let mut store = NetworkRequestStore::new(next_seq);
            let snapshot = NetworkRequestSnapshot {
                seq: next_seq - 1,
                request: json!({ "url": url }),
                redirect_ancestry: Vec::new(),
            };
            store.requests = HashMap::from([(
                "request-1".to_string(),
                NetworkRequestEntry {
                    current: snapshot.clone(),
                    applied_by_seq: BTreeMap::from([(next_seq - 1, snapshot)]),
                },
            )]);
            store.applied_order = VecDeque::from([(next_seq - 1, "request-1".to_string())]);
            store
        }

        let shared_requests = Arc::new(Mutex::new(store("https://example.test/start", 5)));
        let working_requests = Arc::new(Mutex::new(store("https://example.test/final", 8)));
        let (_sender, receiver) = broadcast::channel(4);
        let receiver_slot = Arc::new(Mutex::new(None));
        let state_slot = Arc::new(Mutex::new(None));
        let cursor_slot = Arc::new(Mutex::new(5));
        let lease = PageEventStreamLease {
            receiver: Some(receiver),
            state: Some(PageEventStreamState::new()),
            rollback_state: Some(PageEventStreamState::new()),
            cursor: 8,
            rollback_cursor: 5,
            delivered: false,
            receiver_slot: Arc::clone(&receiver_slot),
            state_slot: Arc::clone(&state_slot),
            cursor_slot: Arc::clone(&cursor_slot),
            requests: Arc::clone(&shared_requests),
            working_requests: Arc::clone(&working_requests),
        };

        drop(lease);
        assert_eq!(*cursor_slot.lock().unwrap(), 5);
        assert_eq!(shared_requests.lock().unwrap().next_applied_seq, 5);
        assert_eq!(
            shared_requests.lock().unwrap().requests["request-1"]
                .current
                .request["url"],
            "https://example.test/start"
        );

        let receiver = receiver_slot.lock().unwrap().take();
        let state = state_slot.lock().unwrap().take();
        let committed_lease = PageEventStreamLease {
            receiver,
            state,
            rollback_state: Some(PageEventStreamState::new()),
            cursor: 8,
            rollback_cursor: 5,
            delivered: false,
            receiver_slot: Arc::clone(&receiver_slot),
            state_slot: Arc::clone(&state_slot),
            cursor_slot: Arc::clone(&cursor_slot),
            requests: Arc::clone(&shared_requests),
            working_requests,
        };
        {
            let mut live = shared_requests.lock().unwrap();
            let concurrent_request = json!({
                "url": "https://example.test/concurrent",
                "method": "POST",
                "headers": { "x-concurrent": "preserved" },
                "redirected_from": { "url": "https://example.test/start" },
            });
            live.requests.insert(
                "request-1".to_string(),
                NetworkRequestEntry {
                    current: NetworkRequestSnapshot {
                        seq: 9,
                        request: concurrent_request.clone(),
                        redirect_ancestry: vec![4],
                    },
                    applied_by_seq: BTreeMap::from([(
                        9,
                        NetworkRequestSnapshot {
                            seq: 9,
                            request: concurrent_request,
                            redirect_ancestry: vec![4],
                        },
                    )]),
                },
            );
            live.next_applied_seq = 10;
            live.applied_order = VecDeque::from([(9, "request-1".to_string())]);
        }
        committed_lease.deliver();
        assert_eq!(*cursor_slot.lock().unwrap(), 8);
        assert_eq!(shared_requests.lock().unwrap().next_applied_seq, 10);
        assert_eq!(
            shared_requests.lock().unwrap().requests["request-1"]
                .current
                .request["url"],
            "https://example.test/concurrent"
        );
        assert_eq!(
            shared_requests.lock().unwrap().requests["request-1"]
                .current
                .request["method"],
            "POST"
        );
        assert_eq!(
            shared_requests.lock().unwrap().requests["request-1"]
                .current
                .request["headers"]["x-concurrent"],
            "preserved"
        );
        assert_eq!(
            shared_requests.lock().unwrap().requests["request-1"]
                .current
                .request["redirected_from"]["url"],
            "https://example.test/start"
        );
    }

    #[cfg(feature = "python")]
    #[test]
    fn stale_page_event_ack_does_not_restore_requests_cleared_by_overflow() {
        fn store(url: &str, next_seq: u64) -> NetworkRequestStore {
            let mut store = NetworkRequestStore::new(next_seq);
            let snapshot = NetworkRequestSnapshot {
                seq: next_seq - 1,
                request: json!({
                    "url": url,
                    "method": "STALE",
                    "headers": { "x-stale": "must-not-return" },
                }),
                redirect_ancestry: Vec::new(),
            };
            store.requests = HashMap::from([(
                "request-1".to_string(),
                NetworkRequestEntry {
                    current: snapshot.clone(),
                    applied_by_seq: BTreeMap::from([(next_seq - 1, snapshot)]),
                },
            )]);
            store.applied_order = VecDeque::from([(next_seq - 1, "request-1".to_string())]);
            store
        }

        let shared_requests = Arc::new(Mutex::new(store("https://example.test/stale", 8)));
        let working_requests = Arc::new(Mutex::new(shared_requests.lock().unwrap().clone()));
        let (_sender, receiver) = broadcast::channel(4);
        let receiver_slot = Arc::new(Mutex::new(None));
        let state_slot = Arc::new(Mutex::new(None));
        let cursor_slot = Arc::new(Mutex::new(5));
        let lease = PageEventStreamLease {
            receiver: Some(receiver),
            state: Some(PageEventStreamState::new()),
            rollback_state: Some(PageEventStreamState::new()),
            cursor: 8,
            rollback_cursor: 5,
            delivered: false,
            receiver_slot,
            state_slot,
            cursor_slot,
            requests: Arc::clone(&shared_requests),
            working_requests,
        };

        shared_requests.lock().unwrap().reset_after_overflow(20);
        lease.deliver();

        let requests = shared_requests.lock().unwrap();
        assert_eq!(requests.next_applied_seq, 20);
        assert!(requests.requests.is_empty());
        assert!(requests.applied_order.is_empty());
        drop(requests);

        let event_log = Arc::new(Mutex::new(CdpEventLog::new()));
        let mut state = NetworkObservationState::new();
        let initial = json!({
            "sessionId": "page-session",
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "request-1",
                "documentURL": "https://example.test/fresh-start",
                "request": {
                    "url": "https://example.test/fresh-start",
                    "method": "GET",
                    "headers": { "x-fresh-start": "present" },
                },
            },
        });
        let redirect = json!({
            "sessionId": "page-session",
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "request-1",
                "documentURL": "https://example.test/fresh-final",
                "redirectResponse": { "status": 302 },
                "request": {
                    "url": "https://example.test/fresh-final",
                    "method": "POST",
                    "headers": { "x-fresh-final": "present" },
                },
            },
        });
        let response = json!({
            "sessionId": "page-session",
            "method": "Network.responseReceived",
            "params": {
                "requestId": "request-1",
                "response": {
                    "url": "https://example.test/fresh-final",
                    "status": 200,
                    "headers": {},
                },
            },
        });
        process_network_observation_event(
            20,
            &initial,
            &event_log,
            "page-session",
            &shared_requests,
            true,
            &mut state,
        )
        .unwrap();
        process_network_observation_event(
            21,
            &redirect,
            &event_log,
            "page-session",
            &shared_requests,
            true,
            &mut state,
        )
        .unwrap();
        let response = process_network_observation_event(
            22,
            &response,
            &event_log,
            "page-session",
            &shared_requests,
            true,
            &mut state,
        )
        .unwrap()
        .unwrap()
        .2;
        assert_eq!(response["request"]["method"], "POST");
        assert_eq!(response["request"]["headers"]["x-fresh-final"], "present");
        assert_eq!(
            response["request"]["redirected_from"]["url"],
            "https://example.test/fresh-start"
        );
        assert!(response["request"]["headers"]["x-stale"].is_null());
    }

    #[cfg(feature = "python")]
    #[test]
    fn delayed_page_event_ack_merges_only_mutations_at_or_after_reset_cursor() {
        fn request_event(url: &str, method: &str, headers: Value, redirect: bool) -> Value {
            let mut params = json!({
                "requestId": "request-1",
                "documentURL": url,
                "request": {
                    "url": url,
                    "method": method,
                    "headers": headers,
                },
            });
            if redirect {
                params["redirectResponse"] = json!({ "status": 302 });
            }
            json!({
                "sessionId": "page-session",
                "method": "Network.requestWillBeSent",
                "params": params,
            })
        }

        let shared_requests = Arc::new(Mutex::new(NetworkRequestStore::new(7_999)));
        let working_requests = Arc::new(Mutex::new(shared_requests.lock().unwrap().clone()));
        {
            let mut working = working_requests.lock().unwrap();
            let stale = request_event(
                "https://example.test/stale",
                "STALE",
                json!({ "x-stale": "must-not-return" }),
                false,
            );
            let valid_start = request_event(
                "https://example.test/current-start",
                "GET",
                json!({ "x-current-start": "present" }),
                false,
            );
            let valid_redirect = request_event(
                "https://example.test/current-final",
                "POST",
                json!({ "x-current-final": "present" }),
                true,
            );
            apply_network_request_mutation(7_999, &stale, "page-session", &mut working, true)
                .unwrap();
            apply_network_request_mutation(8_999, &valid_start, "page-session", &mut working, true)
                .unwrap();
            apply_network_request_mutation(
                9_000,
                &valid_redirect,
                "page-session",
                &mut working,
                true,
            )
            .unwrap();
            working.next_applied_seq = 9_001;
        }

        let (_sender, receiver) = broadcast::channel(4);
        let lease = PageEventStreamLease {
            receiver: Some(receiver),
            state: Some(PageEventStreamState::new()),
            rollback_state: Some(PageEventStreamState::new()),
            cursor: 9_001,
            rollback_cursor: 7_999,
            delivered: false,
            receiver_slot: Arc::new(Mutex::new(None)),
            state_slot: Arc::new(Mutex::new(None)),
            cursor_slot: Arc::new(Mutex::new(7_999)),
            requests: Arc::clone(&shared_requests),
            working_requests,
        };

        shared_requests.lock().unwrap().reset_after_overflow(8_000);
        lease.deliver();

        {
            let requests = shared_requests.lock().unwrap();
            let entry = &requests.requests["request-1"];
            assert!(!entry.applied_by_seq.contains_key(&7_999));
            assert!(entry.applied_by_seq.contains_key(&8_999));
            assert!(entry.applied_by_seq.contains_key(&9_000));
        }

        let response_event = json!({
            "sessionId": "page-session",
            "method": "Network.responseReceived",
            "params": {
                "requestId": "request-1",
                "hasExtraInfo": false,
                "response": {
                    "url": "https://example.test/current-final",
                    "status": 200,
                    "headers": {},
                },
            },
        });
        let response = process_network_observation_event(
            9_001,
            &response_event,
            &Arc::new(Mutex::new(CdpEventLog::new())),
            "page-session",
            &shared_requests,
            true,
            &mut NetworkObservationState::new(),
        )
        .unwrap()
        .unwrap()
        .2;
        assert_eq!(response["request"]["method"], "POST");
        assert_eq!(response["request"]["headers"]["x-current-final"], "present");
        assert_eq!(
            response["request"]["redirected_from"]["url"],
            "https://example.test/current-start"
        );
        assert!(response["request"]["headers"]["x-stale"].is_null());
    }

    #[cfg(feature = "python")]
    #[test]
    fn delayed_page_event_ack_prunes_redirect_ancestry_before_reset_cursor() {
        fn request_event(url: &str, redirect: bool) -> Value {
            let mut params = json!({
                "requestId": "request-1",
                "documentURL": url,
                "request": {
                    "url": url,
                    "method": "GET",
                    "headers": {},
                },
            });
            if redirect {
                params["redirectResponse"] = json!({ "status": 302 });
            }
            json!({
                "sessionId": "page-session",
                "method": "Network.requestWillBeSent",
                "params": params,
            })
        }

        let shared_requests = Arc::new(Mutex::new(NetworkRequestStore::new(7_999)));
        let working_requests = Arc::new(Mutex::new(shared_requests.lock().unwrap().clone()));
        {
            let mut working = working_requests.lock().unwrap();
            let initial = request_event("https://example.test/pre-reset", false);
            let redirect = request_event("https://example.test/post-reset", true);
            apply_network_request_mutation(7_999, &initial, "page-session", &mut working, true)
                .unwrap();
            apply_network_request_mutation(9_000, &redirect, "page-session", &mut working, true)
                .unwrap();
            working.next_applied_seq = 9_001;
        }

        let (_sender, receiver) = broadcast::channel(4);
        let lease = PageEventStreamLease {
            receiver: Some(receiver),
            state: Some(PageEventStreamState::new()),
            rollback_state: Some(PageEventStreamState::new()),
            cursor: 9_001,
            rollback_cursor: 7_999,
            delivered: false,
            receiver_slot: Arc::new(Mutex::new(None)),
            state_slot: Arc::new(Mutex::new(None)),
            cursor_slot: Arc::new(Mutex::new(7_999)),
            requests: Arc::clone(&shared_requests),
            working_requests,
        };

        shared_requests.lock().unwrap().reset_after_overflow(8_000);
        lease.deliver();

        let requests = shared_requests.lock().unwrap();
        let entry = &requests.requests["request-1"];
        assert!(!entry.applied_by_seq.contains_key(&7_999));
        assert!(entry.applied_by_seq.contains_key(&9_000));
        assert_eq!(
            entry.current.request["url"],
            "https://example.test/post-reset"
        );
        assert!(entry.current.request["redirected_from"].is_null());
    }

    #[test]
    fn combined_page_stream_reuses_network_state_and_preserves_lifecycle_order() {
        let event_log = Arc::new(Mutex::new(CdpEventLog::new()));
        let requests = Arc::new(Mutex::new(NetworkRequestStore::new(1)));
        let mut state = PageEventStreamState::new();
        let request = json!({
            "sessionId": "page-session",
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "request-1",
                "loaderId": "loader-1",
                "documentURL": "https://example.test/",
                "type": "Document",
                "request": {
                    "url": "https://example.test/",
                    "method": "GET",
                    "headers": {}
                }
            }
        });
        let response = json!({
            "sessionId": "page-session",
            "method": "Network.responseReceived",
            "params": {
                "requestId": "request-1",
                "loaderId": "loader-1",
                "type": "Document",
                "hasExtraInfo": true,
                "response": {
                    "url": "https://example.test/",
                    "status": 200,
                    "statusText": "OK",
                    "headers": {}
                }
            }
        });
        let finished = json!({
            "sessionId": "page-session",
            "method": "Network.loadingFinished",
            "params": { "requestId": "request-1", "encodedDataLength": 12 }
        });
        let extra = json!({
            "sessionId": "page-session",
            "method": "Network.responseReceivedExtraInfo",
            "params": {
                "requestId": "request-1",
                "headers": { "content-type": "text/html" },
                "headersText": "HTTP/1.1 200 OK"
            }
        });

        process_page_observation_event(
            1,
            &request,
            &event_log,
            "page-session",
            &requests,
            &mut state,
        );
        let mut batch = Vec::new();
        append_ready_page_events(&mut batch, &mut state, 64);
        assert_eq!(batch[0]["kind"], "request");
        assert!(requests.lock().unwrap().requests.contains_key("request-1"));

        process_page_observation_event(
            2,
            &response,
            &event_log,
            "page-session",
            &requests,
            &mut state,
        );
        process_page_observation_event(
            3,
            &finished,
            &event_log,
            "page-session",
            &requests,
            &mut state,
        );
        append_ready_page_events(&mut batch, &mut state, 64);
        assert_eq!(batch.len(), 1);

        process_page_observation_event(
            4,
            &extra,
            &event_log,
            "page-session",
            &requests,
            &mut state,
        );
        append_ready_page_events(&mut batch, &mut state, 64);
        assert_eq!(batch[1]["seq"], 2);
        assert_eq!(batch[1]["kind"], "response");
        assert_eq!(
            batch[1]["payload"]["all_headers"]["content-type"],
            "text/html"
        );
        assert_eq!(batch[2]["seq"], 3);
        assert_eq!(batch[2]["kind"], "requestfinished");
    }

    #[test]
    fn combined_page_stream_filters_sessions_and_merges_frame_navigation_methods() {
        let event_log = Arc::new(Mutex::new(CdpEventLog::new()));
        let requests = Arc::new(Mutex::new(NetworkRequestStore::new(5)));
        let mut state = PageEventStreamState::new();
        let wrong_session = json!({
            "sessionId": "other-session",
            "method": "Page.loadEventFired",
            "params": { "timestamp": 1 }
        });
        let same_document = json!({
            "sessionId": "page-session",
            "method": "Page.navigatedWithinDocument",
            "params": { "frameId": "frame-1", "url": "https://example.test/#hash" }
        });

        process_page_observation_event(
            5,
            &wrong_session,
            &event_log,
            "page-session",
            &requests,
            &mut state,
        );
        process_page_observation_event(
            6,
            &same_document,
            &event_log,
            "page-session",
            &requests,
            &mut state,
        );
        let mut batch = Vec::new();
        append_ready_page_events(&mut batch, &mut state, 64);
        assert_eq!(batch.len(), 1);
        assert_eq!(batch[0]["kind"], "framenavigated");
        assert_eq!(batch[0]["payload"]["frameId"], "frame-1");
    }

    #[test]
    fn network_request_mutation_is_idempotent_per_event_sequence() {
        let event_log = Arc::new(Mutex::new(CdpEventLog::new()));
        let requests = Arc::new(Mutex::new(NetworkRequestStore::new(10)));
        let mut stream_state = NetworkObservationState::new();
        let mut waiter_state = NetworkObservationState::new();
        let initial = json!({
            "sessionId": "page-session",
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "request-1",
                "documentURL": "https://example.test/start",
                "request": { "url": "https://example.test/start", "method": "GET", "headers": {} }
            }
        });
        let redirect = json!({
            "sessionId": "page-session",
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "request-1",
                "documentURL": "https://example.test/final",
                "redirectResponse": { "status": 302 },
                "request": { "url": "https://example.test/final", "method": "GET", "headers": {} }
            }
        });

        process_network_observation_event(
            10,
            &initial,
            &event_log,
            "page-session",
            &requests,
            true,
            &mut stream_state,
        )
        .unwrap();
        process_network_observation_event(
            10,
            &initial,
            &event_log,
            "page-session",
            &requests,
            false,
            &mut waiter_state,
        )
        .unwrap();
        let first = process_network_observation_event(
            11,
            &redirect,
            &event_log,
            "page-session",
            &requests,
            true,
            &mut stream_state,
        )
        .unwrap()
        .unwrap()
        .2;
        let duplicate = process_network_observation_event(
            11,
            &redirect,
            &event_log,
            "page-session",
            &requests,
            false,
            &mut waiter_state,
        )
        .unwrap()
        .unwrap()
        .2;

        assert_eq!(first, duplicate);
        assert_eq!(duplicate["url"], "https://example.test/final");
        assert_eq!(
            duplicate["redirected_from"]["url"],
            "https://example.test/start"
        );
        assert!(duplicate["redirected_from"]["redirected_from"].is_null());
    }

    #[test]
    fn network_request_mutation_backfills_redirect_when_later_sequence_arrives_first() {
        let mut log = CdpEventLog {
            next_seq: 10,
            events: VecDeque::with_capacity(CDP_EVENT_LOG_LIMIT),
        };
        let initial = json!({
            "sessionId": "page-session",
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "request-1",
                "documentURL": "https://example.test/start",
                "request": { "url": "https://example.test/start", "method": "GET", "headers": {} }
            }
        });
        let redirect = json!({
            "sessionId": "page-session",
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "request-1",
                "documentURL": "https://example.test/final",
                "redirectResponse": { "status": 302 },
                "request": { "url": "https://example.test/final", "method": "GET", "headers": {} }
            }
        });
        log.push(initial.clone());
        log.push(redirect.clone());
        let event_log = Arc::new(Mutex::new(log));
        let requests = Arc::new(Mutex::new(NetworkRequestStore::new(10)));
        let mut consumer_a = NetworkObservationState::new();
        let mut consumer_b = NetworkObservationState::new();

        let first_redirect = process_network_observation_event(
            11,
            &redirect,
            &event_log,
            "page-session",
            &requests,
            false,
            &mut consumer_b,
        )
        .unwrap()
        .unwrap()
        .2;
        process_network_observation_event(
            10,
            &initial,
            &event_log,
            "page-session",
            &requests,
            true,
            &mut consumer_a,
        )
        .unwrap();
        let consumer_b_redirect = process_network_observation_event(
            11,
            &redirect,
            &event_log,
            "page-session",
            &requests,
            false,
            &mut consumer_b,
        )
        .unwrap()
        .unwrap()
        .2;
        let consumer_a_redirect = process_network_observation_event(
            11,
            &redirect,
            &event_log,
            "page-session",
            &requests,
            true,
            &mut consumer_a,
        )
        .unwrap()
        .unwrap()
        .2;

        assert_eq!(first_redirect, consumer_b_redirect);
        assert_eq!(consumer_b_redirect, consumer_a_redirect);
        assert_eq!(
            consumer_a_redirect["redirected_from"]["url"],
            "https://example.test/start"
        );
    }

    #[test]
    fn combined_page_stream_releases_overlapping_responses_in_sequence_order() {
        let event_log = Arc::new(Mutex::new(CdpEventLog::new()));
        let requests = Arc::new(Mutex::new(NetworkRequestStore::new(20)));
        let mut state = PageEventStreamState::new();
        let response = |request_id: &str, url: &str| {
            json!({
                "sessionId": "page-session",
                "method": "Network.responseReceived",
                "params": {
                    "requestId": request_id,
                    "hasExtraInfo": true,
                    "response": { "url": url, "status": 200, "headers": {} }
                }
            })
        };
        let extra = |request_id: &str| {
            json!({
                "sessionId": "page-session",
                "method": "Network.responseReceivedExtraInfo",
                "params": { "requestId": request_id, "headers": {} }
            })
        };
        let console = json!({
            "sessionId": "page-session",
            "method": "Runtime.consoleAPICalled",
            "params": { "type": "log", "args": [{ "type": "string", "value": "ready" }] }
        });

        process_page_observation_event(
            20,
            &response("request-a", "https://example.test/a"),
            &event_log,
            "page-session",
            &requests,
            &mut state,
        );
        process_page_observation_event(
            21,
            &response("request-b", "https://example.test/b"),
            &event_log,
            "page-session",
            &requests,
            &mut state,
        );
        process_page_observation_event(
            22,
            &console,
            &event_log,
            "page-session",
            &requests,
            &mut state,
        );
        process_page_observation_event(
            23,
            &extra("request-b"),
            &event_log,
            "page-session",
            &requests,
            &mut state,
        );
        let mut batch = Vec::new();
        append_ready_page_events(&mut batch, &mut state, 64);
        assert!(batch.is_empty());

        process_page_observation_event(
            24,
            &extra("request-a"),
            &event_log,
            "page-session",
            &requests,
            &mut state,
        );
        append_ready_page_events(&mut batch, &mut state, 64);
        assert_eq!(
            batch
                .iter()
                .map(|envelope| envelope["seq"].as_u64().unwrap())
                .collect::<Vec<_>>(),
            vec![20, 21, 22]
        );
        assert_eq!(batch[0]["payload"]["url"], "https://example.test/a");
        assert_eq!(batch[1]["payload"]["url"], "https://example.test/b");
        assert_eq!(batch[2]["kind"], "console");
    }

    #[test]
    fn launch_options_accept_equivalent_snake_case_and_camel_case_json() {
        let snake_case: LaunchOptions = serde_json::from_value(json!({
            "headless": false,
            "executable_path": "/path/to/chromium",
            "channel": "chromium",
            "args": ["--disable-gpu"],
            "ignore_all_default_args": true,
            "ignore_default_args": ["--mute-audio"],
            "timeout": 12_345.0,
            "user_data_dir": "/path/to/profile",
            "env": {"LANG": "en_CA.UTF-8"},
            "chromium_sandbox": true,
            "proxy": {
                "server": "http://proxy.example:3128",
                "bypass": "localhost",
                "username": "user",
                "password": "password"
            }
        }))
        .unwrap();
        let camel_case: LaunchOptions = serde_json::from_value(json!({
            "headless": false,
            "executablePath": "/path/to/chromium",
            "channel": "chromium",
            "args": ["--disable-gpu"],
            "ignoreAllDefaultArgs": true,
            "ignoreDefaultArgs": ["--mute-audio"],
            "timeout": 12_345.0,
            "userDataDir": "/path/to/profile",
            "env": {"LANG": "en_CA.UTF-8"},
            "chromiumSandbox": true,
            "proxy": {
                "server": "http://proxy.example:3128",
                "bypass": "localhost",
                "username": "user",
                "password": "password"
            }
        }))
        .unwrap();

        assert_eq!(camel_case, snake_case);
    }

    #[test]
    fn launch_options_default_headless_to_true_when_absent() {
        let options: LaunchOptions = serde_json::from_str("{}").unwrap();

        assert!(options.headless);
    }

    #[test]
    fn omitted_launch_timeout_uses_the_existing_thirty_second_core_default() {
        let options: LaunchOptions = serde_json::from_str("{}").unwrap();

        assert_eq!(options.timeout, None);
        assert_eq!(
            BrowserInner::command_timeout(options.timeout),
            Duration::from_secs(30)
        );
        assert_eq!(
            BrowserInner::command_timeout(LaunchOptions::default().timeout),
            Duration::from_secs(30)
        );
    }

    #[test]
    fn launch_options_reject_duplicate_snake_case_and_camel_case_keys() {
        let error = serde_json::from_value::<LaunchOptions>(json!({
            "executable_path": "/first",
            "executablePath": "/second"
        }))
        .unwrap_err();

        assert!(error.to_string().contains("duplicate field"));
    }
}

#[derive(Debug, Deserialize, PartialEq)]
struct LaunchOptions {
    #[serde(default = "default_true")]
    headless: bool,
    #[serde(default, alias = "executablePath")]
    executable_path: Option<String>,
    #[serde(default)]
    channel: Option<String>,
    #[serde(default)]
    args: Vec<String>,
    #[serde(default, alias = "ignoreAllDefaultArgs")]
    ignore_all_default_args: bool,
    #[serde(default, alias = "ignoreDefaultArgs")]
    ignore_default_args: Vec<String>,
    #[serde(default)]
    timeout: Option<f64>,
    #[serde(default, alias = "userDataDir")]
    user_data_dir: Option<String>,
    #[serde(default)]
    env: HashMap<String, String>,
    #[serde(default, alias = "chromiumSandbox")]
    chromium_sandbox: bool,
    #[serde(default)]
    proxy: Option<ProxyOptions>,
}

#[derive(Debug, Deserialize, PartialEq)]
struct ProxyOptions {
    server: String,
    #[serde(default)]
    bypass: Option<String>,
    #[serde(default)]
    username: Option<String>,
    #[serde(default)]
    password: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
struct BrowserContextOptions {
    #[serde(default)]
    proxy: Option<ProxyOptions>,
}

fn proxy_server(proxy: &ProxyOptions) -> RwResult<&str> {
    let server = proxy.server.trim();
    if server.is_empty() {
        return Err(RwError::Message("proxy.server is required".to_string()));
    }
    Ok(server)
}

fn normalized_proxy_bypass(proxy: &ProxyOptions) -> Option<String> {
    let bypass = proxy
        .bypass
        .as_deref()?
        .split(',')
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .collect::<Vec<_>>()
        .join(";");
    if bypass.is_empty() {
        None
    } else {
        Some(bypass)
    }
}

fn default_true() -> bool {
    true
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

enum CdpOutgoing {
    Text(String),
    Close,
}

struct CdpClient {
    write_tx: mpsc::UnboundedSender<CdpOutgoing>,
    pending: CdpPendingMap,
    events: broadcast::Sender<Value>,
    event_log: Arc<Mutex<CdpEventLog>>,
    next_id: AtomicU64,
    sent_runtime_enable_count: AtomicU64,
    sent_target_close_count: AtomicU64,
    sent_context_dispose_count: AtomicU64,
    alive: Arc<AtomicBool>,
    alive_tx: watch::Sender<bool>,
}

struct CdpEventLog {
    next_seq: u64,
    events: VecDeque<(u64, Value)>,
}

impl CdpEventLog {
    fn new() -> Self {
        Self {
            next_seq: 0,
            events: VecDeque::with_capacity(CDP_EVENT_LOG_LIMIT),
        }
    }

    fn cursor(&self) -> u64 {
        self.next_seq
    }

    fn push(&mut self, event: Value) {
        let seq = self.next_seq;
        self.next_seq = self.next_seq.wrapping_add(1);
        self.events.push_back((seq, event));
        while self.events.len() > CDP_EVENT_LOG_LIMIT {
            self.events.pop_front();
        }
    }

    fn entries_since(&self, cursor: u64) -> Vec<(u64, Value)> {
        self.events
            .iter()
            .filter(|(seq, _)| *seq >= cursor)
            .map(|(seq, event)| (*seq, event.clone()))
            .collect()
    }

    fn oldest_seq(&self) -> u64 {
        self.events
            .front()
            .map(|(seq, _)| *seq)
            .unwrap_or(self.next_seq)
    }
}

fn format_websocket_status(status: tokio_tungstenite::tungstenite::http::StatusCode) -> String {
    match status.canonical_reason() {
        Some(reason) if !reason.is_empty() => format!("{} {}", status.as_u16(), reason),
        _ => status.as_u16().to_string(),
    }
}

fn websocket_endpoint_as_http_url(ws_endpoint: &str) -> Option<String> {
    ws_endpoint
        .strip_prefix("ws://")
        .map(|rest| format!("http://{rest}"))
        .or_else(|| {
            ws_endpoint
                .strip_prefix("wss://")
                .map(|rest| format!("https://{rest}"))
        })
}

async fn fetch_websocket_http_error_body(
    ws_endpoint: &str,
    headers: &[(String, String)],
) -> Option<Vec<u8>> {
    let http_endpoint = websocket_endpoint_as_http_url(ws_endpoint)?;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_millis(1_000))
        .no_proxy()
        .build()
        .ok()?;
    let mut request = client.get(http_endpoint);
    for (name, value) in headers {
        request = request.header(name, value);
    }
    let response = request.send().await.ok()?;
    let body = response.bytes().await.ok()?;
    if body.is_empty() {
        None
    } else {
        Some(body.to_vec())
    }
}

fn cdp_websocket_connect_error(
    ws_endpoint: &str,
    error: WsError,
    fallback_body: Option<Vec<u8>>,
) -> RwError {
    match error {
        WsError::Http(response) => {
            let status_text = format_websocket_status(response.status());
            let mut message = format!("WebSocket error: {ws_endpoint} {status_text}");
            let response_body = response
                .body()
                .as_ref()
                .filter(|body| !body.is_empty())
                .map(|body| body.as_slice())
                .or_else(|| fallback_body.as_deref());
            if let Some(body) = response_body {
                message.push('\n');
                message.push_str(&String::from_utf8_lossy(body));
            }
            message.push_str("\nCall log:");
            message.push_str(&format!("\n  - <ws connecting> {ws_endpoint}"));
            message.push_str(&format!(
                "\n  - <ws unexpected response> {ws_endpoint} {status_text}"
            ));
            RwError::Message(message)
        }
        other => RwError::WebSocket(other),
    }
}

fn dispatch_cdp_payload(
    mut payload: Value,
    pending: CdpPendingMap,
    events: broadcast::Sender<Value>,
    event_log: Arc<Mutex<CdpEventLog>>,
) {
    if let Some(id) = payload.get("id").and_then(Value::as_u64) {
        let sender = pending.lock().unwrap().remove(&id);
        if let Some(sender) = sender {
            let result = if let Some(error) = payload.get("error") {
                let message = error
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown CDP error")
                    .to_string();
                Err(RwError::Message(message))
            } else {
                Ok(payload
                    .as_object_mut()
                    .and_then(|object| object.remove("result"))
                    .unwrap_or(Value::Null))
            };
            let _ = sender.send(result);
        }
    } else {
        let mut event_log = event_log.lock().unwrap();
        event_log.push(payload.clone());
        let _ = events.send(payload);
    }
}

fn close_pending_cdp_commands(pending: CdpPendingMap) {
    let senders = {
        let mut pending = pending.lock().unwrap();
        pending
            .drain()
            .map(|(_, sender)| sender)
            .collect::<Vec<_>>()
    };
    for sender in senders {
        let _ = sender.send(Err(RwError::Disconnected));
    }
}

/// Ensure the WebSocket handshake request-target is in origin-form (starts with `/`).
///
/// tungstenite builds the request line directly from `uri.path_and_query()`. For CDP
/// endpoints whose URL has an empty path but a query string — e.g. Anchor Browser's
/// `wss://host?apiKey=...&sessionId=...` — `http::Uri` yields `?apiKey=...` with no
/// leading slash, producing a malformed request line `GET ?apiKey=... HTTP/1.1`.
/// Strict front-ends (nginx) reject that with `400 Bad Request`. Playwright's client
/// always sends origin-form `GET /?...`, so normalize the target to match.
fn ensure_ws_request_path(
    request: &mut tokio_tungstenite::tungstenite::handshake::client::Request,
) {
    let uri = request.uri().clone();
    // tungstenite writes `uri.path_and_query()` verbatim as the request target, so
    // inspect that exact string rather than `uri.path()` (which normalizes "" to "/").
    let current = uri.path_and_query().map(PathAndQuery::as_str).unwrap_or("");
    if current.starts_with('/') {
        return;
    }
    let normalized = format!("/{current}");
    let Ok(path_and_query) = normalized.parse::<PathAndQuery>() else {
        return;
    };
    let mut parts = uri.into_parts();
    parts.path_and_query = Some(path_and_query);
    if let Ok(new_uri) = Uri::from_parts(parts) {
        *request.uri_mut() = new_uri;
    }
}

impl CdpClient {
    async fn connect(ws_endpoint: &str) -> RwResult<Arc<Self>> {
        Self::connect_with_headers(ws_endpoint, &[]).await
    }

    async fn connect_with_headers(
        ws_endpoint: &str,
        headers: &[(String, String)],
    ) -> RwResult<Arc<Self>> {
        let mut request = ws_endpoint.into_client_request()?;
        ensure_ws_request_path(&mut request);
        for (name, value) in headers {
            let header_name = HeaderName::from_bytes(name.as_bytes()).map_err(|error| {
                RwError::Message(format!("invalid CDP header name {name:?}: {error}"))
            })?;
            let header_value = HeaderValue::from_str(value).map_err(|error| {
                RwError::Message(format!("invalid CDP header value for {name:?}: {error}"))
            })?;
            request.headers_mut().insert(header_name, header_value);
        }
        let (mut stream, _) = match connect_async(request).await {
            Ok(result) => result,
            Err(error) => {
                let fallback_body = match &error {
                    WsError::Http(response)
                        if response
                            .body()
                            .as_ref()
                            .map_or(true, |body| body.is_empty()) =>
                    {
                        fetch_websocket_http_error_body(ws_endpoint, headers).await
                    }
                    _ => None,
                };
                return Err(cdp_websocket_connect_error(
                    ws_endpoint,
                    error,
                    fallback_body,
                ));
            }
        };
        if let MaybeTlsStream::Plain(tcp_stream) = stream.get_mut() {
            let _ = tcp_stream.set_nodelay(true);
        }
        let (mut write, mut read) = stream.split();
        let (write_tx, mut write_rx) = mpsc::unbounded_channel::<CdpOutgoing>();
        let pending: CdpPendingMap = Arc::new(Mutex::new(HashMap::new()));
        let pending_reader = Arc::clone(&pending);
        let (events, _) = broadcast::channel(4096);
        let events_reader = events.clone();
        let event_log = Arc::new(Mutex::new(CdpEventLog::new()));
        let event_log_reader = Arc::clone(&event_log);
        let alive = Arc::new(AtomicBool::new(true));
        let alive_writer = Arc::clone(&alive);
        let alive_reader = Arc::clone(&alive);
        let (alive_tx, _) = watch::channel(true);
        let alive_tx_writer = alive_tx.clone();
        let alive_tx_reader = alive_tx.clone();

        tokio::spawn(async move {
            while let Some(message) = write_rx.recv().await {
                match message {
                    CdpOutgoing::Text(text) => {
                        if write.send(Message::Text(text.into())).await.is_err() {
                            alive_writer.store(false, Ordering::SeqCst);
                            alive_tx_writer.send_replace(false);
                            break;
                        }
                    }
                    CdpOutgoing::Close => {
                        let _ = write.send(Message::Close(None)).await;
                        break;
                    }
                }
            }
        });

        tokio::spawn(async move {
            while let Some(next) = read.next().await {
                let Ok(message) = next else {
                    break;
                };
                let Ok(text) = message.into_text() else {
                    continue;
                };
                let Ok(payload) = serde_json::from_str::<Value>(&text) else {
                    continue;
                };

                dispatch_cdp_payload(
                    payload,
                    Arc::clone(&pending_reader),
                    events_reader.clone(),
                    Arc::clone(&event_log_reader),
                );
            }
            alive_reader.store(false, Ordering::SeqCst);
            alive_tx_reader.send_replace(false);
            close_pending_cdp_commands(pending_reader);
        });

        Ok(Arc::new(Self {
            write_tx,
            pending,
            events,
            event_log,
            next_id: AtomicU64::new(1),
            sent_runtime_enable_count: AtomicU64::new(0),
            sent_target_close_count: AtomicU64::new(0),
            sent_context_dispose_count: AtomicU64::new(0),
            alive,
            alive_tx,
        }))
    }

    #[cfg(unix)]
    async fn connect_pipe(
        mut pipe_read: fs::File,
        mut pipe_write: fs::File,
    ) -> RwResult<Arc<Self>> {
        let (write_tx, mut write_rx) = mpsc::unbounded_channel::<CdpOutgoing>();
        let (incoming_tx, mut incoming_rx) = mpsc::unbounded_channel::<Value>();
        let pending: CdpPendingMap = Arc::new(Mutex::new(HashMap::new()));
        let pending_dispatcher = Arc::clone(&pending);
        let (events, _) = broadcast::channel(4096);
        let events_dispatcher = events.clone();
        let event_log = Arc::new(Mutex::new(CdpEventLog::new()));
        let event_log_dispatcher = Arc::clone(&event_log);
        let alive = Arc::new(AtomicBool::new(true));
        let alive_writer = Arc::clone(&alive);
        let alive_dispatcher = Arc::clone(&alive);
        let (alive_tx, _) = watch::channel(true);
        let alive_tx_writer = alive_tx.clone();
        let alive_tx_dispatcher = alive_tx.clone();

        tokio::task::spawn_blocking(move || {
            while let Some(message) = write_rx.blocking_recv() {
                match message {
                    CdpOutgoing::Text(text) => {
                        if pipe_write.write_all(text.as_bytes()).is_err()
                            || pipe_write.write_all(&[0]).is_err()
                        {
                            alive_writer.store(false, Ordering::SeqCst);
                            alive_tx_writer.send_replace(false);
                            break;
                        }
                    }
                    CdpOutgoing::Close => break,
                }
            }
        });

        tokio::task::spawn_blocking(move || {
            let mut buffer = Vec::new();
            let mut chunk = [0_u8; 8192];
            'read_loop: loop {
                let bytes_read = match pipe_read.read(&mut chunk) {
                    Ok(0) => break,
                    Ok(size) => size,
                    Err(_) => break,
                };
                buffer.extend_from_slice(&chunk[..bytes_read]);
                while let Some(index) = buffer.iter().position(|byte| *byte == 0) {
                    let message = buffer.drain(..=index).collect::<Vec<_>>();
                    let payload_bytes = &message[..message.len().saturating_sub(1)];
                    if payload_bytes.is_empty() {
                        continue;
                    }
                    let Ok(payload) = serde_json::from_slice::<Value>(payload_bytes) else {
                        continue;
                    };
                    if incoming_tx.send(payload).is_err() {
                        break 'read_loop;
                    }
                }
            }
        });

        tokio::spawn(async move {
            while let Some(payload) = incoming_rx.recv().await {
                dispatch_cdp_payload(
                    payload,
                    Arc::clone(&pending_dispatcher),
                    events_dispatcher.clone(),
                    Arc::clone(&event_log_dispatcher),
                );
            }
            alive_dispatcher.store(false, Ordering::SeqCst);
            alive_tx_dispatcher.send_replace(false);
            close_pending_cdp_commands(pending_dispatcher);
        });

        Ok(Arc::new(Self {
            write_tx,
            pending,
            events,
            event_log,
            next_id: AtomicU64::new(1),
            sent_runtime_enable_count: AtomicU64::new(0),
            sent_target_close_count: AtomicU64::new(0),
            sent_context_dispose_count: AtomicU64::new(0),
            alive,
            alive_tx,
        }))
    }

    fn subscribe(&self) -> broadcast::Receiver<Value> {
        self.events.subscribe()
    }

    fn event_cursor(&self) -> u64 {
        self.event_log.lock().unwrap().cursor()
    }

    fn is_connected(&self) -> bool {
        self.alive.load(Ordering::SeqCst)
    }

    fn close(&self) {
        self.alive.store(false, Ordering::SeqCst);
        self.alive_tx.send_replace(false);
        let _ = self.write_tx.send(CdpOutgoing::Close);
    }

    fn mark_closed(&self) {
        self.alive.store(false, Ordering::SeqCst);
        self.alive_tx.send_replace(false);
    }

    fn record_sent_command(&self, method: &str) {
        match method {
            "Runtime.enable" => {
                self.sent_runtime_enable_count
                    .fetch_add(1, Ordering::SeqCst);
            }
            "Target.closeTarget" => {
                self.sent_target_close_count.fetch_add(1, Ordering::SeqCst);
            }
            "Target.disposeBrowserContext" => {
                self.sent_context_dispose_count
                    .fetch_add(1, Ordering::SeqCst);
            }
            _ => {}
        }
    }

    fn sent_runtime_enable_count(&self) -> u64 {
        self.sent_runtime_enable_count.load(Ordering::SeqCst)
    }

    fn sent_target_close_count(&self) -> u64 {
        self.sent_target_close_count.load(Ordering::SeqCst)
    }

    fn sent_context_dispose_count(&self) -> u64 {
        self.sent_context_dispose_count.load(Ordering::SeqCst)
    }

    fn pending_command_count(&self) -> usize {
        self.pending.lock().unwrap().len()
    }

    async fn send(
        &self,
        method: &str,
        params: Value,
        session_id: Option<&str>,
        timeout: Duration,
    ) -> RwResult<Value> {
        if !self.is_connected() {
            return Err(RwError::Disconnected);
        }
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id, tx);
        let _pending_guard = PendingCommandGuard::new(id, &self.pending);

        let mut payload = json!({
            "id": id,
            "method": method,
        });
        if !params.is_null() {
            payload["params"] = params;
        }
        if let Some(session_id) = session_id {
            payload["sessionId"] = Value::String(session_id.to_string());
        }

        if self
            .write_tx
            .send(CdpOutgoing::Text(payload.to_string()))
            .is_err()
        {
            self.mark_closed();
            return Err(RwError::Disconnected);
        }
        self.record_sent_command(method);

        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(result)) => result.map_err(|error| match error {
                RwError::Message(message) => RwError::Cdp {
                    method: method.to_string(),
                    message,
                },
                other => other,
            }),
            Ok(Err(_)) => Err(RwError::Disconnected),
            Err(_) => Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }

    async fn send_raw_params_json(
        &self,
        method: &str,
        params_json: String,
        session_id: Option<&str>,
        timeout: Duration,
    ) -> RwResult<Value> {
        if !self.is_connected() {
            return Err(RwError::Disconnected);
        }
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id, tx);
        let _pending_guard = PendingCommandGuard::new(id, &self.pending);

        let method_json = serde_json::to_string(method)?;
        let payload = if let Some(session_id) = session_id {
            let session_id_json = serde_json::to_string(session_id)?;
            format!(
                "{{\"id\":{id},\"method\":{method_json},\"params\":{params_json},\"sessionId\":{session_id_json}}}"
            )
        } else {
            format!("{{\"id\":{id},\"method\":{method_json},\"params\":{params_json}}}")
        };

        if self.write_tx.send(CdpOutgoing::Text(payload)).is_err() {
            self.mark_closed();
            return Err(RwError::Disconnected);
        }
        self.record_sent_command(method);

        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(result)) => result.map_err(|error| match error {
                RwError::Message(message) => RwError::Cdp {
                    method: method.to_string(),
                    message,
                },
                other => other,
            }),
            Ok(Err(_)) => Err(RwError::Disconnected),
            Err(_) => Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }

    async fn send_batch_raw_params_json(
        &self,
        method: &str,
        params_json_list: Vec<String>,
        session_id: Option<&str>,
        timeout: Duration,
    ) -> RwResult<Vec<Value>> {
        if !self.is_connected() {
            return Err(RwError::Disconnected);
        }
        let method_json = serde_json::to_string(method)?;
        let session_id_json = match session_id {
            Some(session_id) => Some(serde_json::to_string(session_id)?),
            None => None,
        };
        let mut receivers = Vec::with_capacity(params_json_list.len());
        for params_json in params_json_list {
            let id = self.next_id.fetch_add(1, Ordering::SeqCst);
            let (tx, rx) = oneshot::channel();
            self.pending.lock().unwrap().insert(id, tx);
            let pending_guard = PendingCommandGuard::new(id, &self.pending);

            let payload = if let Some(session_id_json) = &session_id_json {
                format!(
                    "{{\"id\":{id},\"method\":{method_json},\"params\":{params_json},\"sessionId\":{session_id_json}}}"
                )
            } else {
                format!("{{\"id\":{id},\"method\":{method_json},\"params\":{params_json}}}")
            };

            if self.write_tx.send(CdpOutgoing::Text(payload)).is_err() {
                self.mark_closed();
                return Err(RwError::Disconnected);
            }
            self.record_sent_command(method);
            receivers.push((id, rx, pending_guard));
        }

        let mut results = Vec::with_capacity(receivers.len());
        for (_id, rx, _pending_guard) in receivers {
            match tokio::time::timeout(timeout, rx).await {
                Ok(Ok(result)) => results.push(result.map_err(|error| match error {
                    RwError::Message(message) => RwError::Cdp {
                        method: method.to_string(),
                        message,
                    },
                    other => other,
                })?),
                Ok(Err(_)) => return Err(RwError::Disconnected),
                Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
            }
        }
        Ok(results)
    }
}

struct BrowserInner {
    runtime: OwnedRuntime,
    client: Arc<CdpClient>,
    process: Mutex<Option<Child>>,
    profile_dir: Mutex<Option<TempDir>>,
    owned: bool,
    ws_endpoint: String,
    stealth_user_agent_override: Mutex<Option<Value>>,
    single_process_fallback: bool,
    lifecycle: Arc<CloseLifecycle>,
    attached_pages: AttachedPageRegistry,
}

#[derive(Default)]
struct AttachedPageRegistry {
    entries: Mutex<HashMap<String, AttachedPageEntry>>,
    next_generation: AtomicU64,
}

struct AttachedPageEntry {
    generation: u64,
    page: Weak<PageInner>,
    attach_lock: Weak<tokio::sync::Mutex<()>>,
    registered: bool,
}

enum AttachedPageReservation {
    Existing(Arc<PageInner>),
    Attach {
        generation: u64,
        attach_lock: Arc<tokio::sync::Mutex<()>>,
    },
}

enum AttachedPageRegistration {
    Registered,
    Existing(Arc<PageInner>),
    ReservationLost,
}

enum AttachedPageClaim {
    Existing(Arc<PageInner>),
    Attach { generation: u64 },
    Retry,
}

impl AttachedPageRegistry {
    fn reserve(&self, target_id: &str) -> AttachedPageReservation {
        let mut entries = self.entries.lock().unwrap();
        if let Some(entry) = entries.get(target_id) {
            if let Some(page) = entry.page.upgrade() {
                return AttachedPageReservation::Existing(page);
            }
            if let Some(attach_lock) = entry.attach_lock.upgrade() {
                return AttachedPageReservation::Attach {
                    generation: entry.generation,
                    attach_lock,
                };
            }
        }
        let generation = self.next_generation.fetch_add(1, Ordering::SeqCst) + 1;
        let attach_lock = Arc::new(tokio::sync::Mutex::new(()));
        entries.insert(
            target_id.to_string(),
            AttachedPageEntry {
                generation,
                page: Weak::new(),
                attach_lock: Arc::downgrade(&attach_lock),
                registered: false,
            },
        );
        AttachedPageReservation::Attach {
            generation,
            attach_lock,
        }
    }

    fn claim_after_lock(
        &self,
        target_id: &str,
        generation: u64,
        attach_lock: &Arc<tokio::sync::Mutex<()>>,
    ) -> AttachedPageClaim {
        let mut entries = self.entries.lock().unwrap();
        if let Some(entry) = entries.get(target_id) {
            if let Some(page) = entry.page.upgrade() {
                return AttachedPageClaim::Existing(page);
            }
            let same_reservation = entry.generation == generation
                && Weak::ptr_eq(&entry.attach_lock, &Arc::downgrade(attach_lock));
            if same_reservation && !entry.registered {
                return AttachedPageClaim::Attach { generation };
            }
            if !same_reservation && entry.attach_lock.upgrade().is_some() {
                return AttachedPageClaim::Retry;
            }
        }

        // The page registered by the task that previously owned this lock was
        // dropped before this waiter could inspect it. Claim a fresh generation
        // while retaining the already-acquired target lock so no third observer
        // can start a duplicate attachment.
        let generation = self.next_generation.fetch_add(1, Ordering::SeqCst) + 1;
        entries.insert(
            target_id.to_string(),
            AttachedPageEntry {
                generation,
                page: Weak::new(),
                attach_lock: Arc::downgrade(attach_lock),
                registered: false,
            },
        );
        AttachedPageClaim::Attach { generation }
    }

    fn register(
        &self,
        target_id: &str,
        page: &Arc<PageInner>,
        generation: u64,
        attach_lock: &Arc<tokio::sync::Mutex<()>>,
    ) -> AttachedPageRegistration {
        let mut entries = self.entries.lock().unwrap();
        let Some(entry) = entries.get_mut(target_id) else {
            return AttachedPageRegistration::ReservationLost;
        };
        if let Some(existing) = entry.page.upgrade() {
            return if Arc::ptr_eq(&existing, page) {
                AttachedPageRegistration::Registered
            } else {
                AttachedPageRegistration::Existing(existing)
            };
        }
        if entry.generation != generation
            || !Weak::ptr_eq(&entry.attach_lock, &Arc::downgrade(attach_lock))
        {
            return AttachedPageRegistration::ReservationLost;
        }
        entry.page = Arc::downgrade(page);
        entry.registered = true;
        AttachedPageRegistration::Registered
    }

    fn remove_reservation(
        &self,
        target_id: &str,
        generation: u64,
        attach_lock: &Arc<tokio::sync::Mutex<()>>,
    ) {
        let mut entries = self.entries.lock().unwrap();
        let should_remove = entries.get(target_id).is_some_and(|entry| {
            entry.generation == generation
                && Arc::strong_count(attach_lock) == 1
                && entry.page.upgrade().is_none()
                && Weak::ptr_eq(&entry.attach_lock, &Arc::downgrade(attach_lock))
        });
        if should_remove {
            entries.remove(target_id);
        }
    }

    fn remove_page(&self, target_id: &str, generation: u64, page: *const PageInner) {
        let mut entries = self.entries.lock().unwrap();
        if entries
            .get(target_id)
            .is_some_and(|entry| entry.generation == generation && entry.page.as_ptr() == page)
        {
            entries.remove(target_id);
        }
    }

    fn clear(&self) {
        self.entries.lock().unwrap().clear();
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.entries.lock().unwrap().len()
    }
}

struct OwnedRuntime(Option<tokio::runtime::Runtime>);

impl OwnedRuntime {
    fn new(runtime: tokio::runtime::Runtime) -> Self {
        Self(Some(runtime))
    }
}

impl Deref for OwnedRuntime {
    type Target = tokio::runtime::Runtime;

    fn deref(&self) -> &Self::Target {
        self.0.as_ref().expect("browser runtime is available")
    }
}

impl Drop for OwnedRuntime {
    fn drop(&mut self) {
        let Some(runtime) = self.0.take() else {
            return;
        };
        if tokio::runtime::Handle::try_current().is_ok() {
            let _ = std::thread::Builder::new()
                .name("rustwright-runtime-drop".to_string())
                .spawn(move || drop(runtime));
        } else {
            drop(runtime);
        }
    }
}

enum LaunchedCdpTransport {
    WebSocket(String),
    #[cfg(unix)]
    Pipe {
        read: fs::File,
        write: fs::File,
    },
}

impl LaunchedCdpTransport {
    fn endpoint_label(&self) -> String {
        match self {
            LaunchedCdpTransport::WebSocket(endpoint) => endpoint.clone(),
            #[cfg(unix)]
            LaunchedCdpTransport::Pipe { .. } => "pipe://chromium".to_string(),
        }
    }
}

#[cfg(feature = "python")]
fn run_detached<T, F>(operation: F) -> T
where
    T: Send,
    F: FnOnce() -> T + Send,
{
    let mut operation = Some(operation);
    if let Some(result) = Python::try_attach(|py| {
        let operation = operation
            .take()
            .expect("detached operation should only be consumed once");
        py.detach(operation)
    }) {
        result
    } else {
        operation
            .take()
            .expect("unattached operation should still be available")()
    }
}

#[cfg(not(feature = "python"))]
fn run_detached<T, F>(operation: F) -> T
where
    T: Send,
    F: FnOnce() -> T + Send,
{
    operation()
}

fn run_blocking_detached<Fut>(runtime: &tokio::runtime::Runtime, future: Fut) -> Fut::Output
where
    Fut: Future + Send,
    Fut::Output: Send,
{
    run_detached(|| runtime.block_on(future))
}

impl BrowserInner {
    fn block_on_raw<Fut>(&self, future: Fut) -> Fut::Output
    where
        Fut: Future,
    {
        self.runtime.block_on(future)
    }

    fn block_on<T, Fut>(&self, future: Fut) -> RwResult<T>
    where
        T: Send,
        Fut: Future<Output = RwResult<T>> + Send,
    {
        // PyO3 callers should release the GIL for long CDP operations. Rust and Node callers do
        // not initialize Python, and shutdown may make attachment unavailable, so fall back to the
        // raw runtime path instead of panicking.
        run_blocking_detached(&self.runtime, future)
    }

    fn command_timeout(timeout_ms: Option<f64>) -> Duration {
        match timeout_ms {
            Some(ms) if ms <= 0.0 => Duration::from_secs(24 * 60 * 60),
            Some(ms) => Duration::from_millis(ms.max(1.0) as u64),
            None => Duration::from_millis(30_000),
        }
    }

    fn close_last_owner_without_gil(&self) -> RwResult<()> {
        let sender = match self.lifecycle.start() {
            CloseStart::Done(outcome) => return outcome.into_result(),
            CloseStart::Wait(_) => return Ok(()),
            CloseStart::Lead(sender) => sender,
        };
        let owns_browser_process = self.process.lock().unwrap().is_some();
        if owns_browser_process && tokio::runtime::Handle::try_current().is_err() {
            let client = Arc::clone(&self.client);
            let _ = self.block_on_raw(async move {
                client
                    .send("Browser.close", json!({}), None, Duration::from_secs(3))
                    .await
            });
        }
        self.client.close();

        let mut cleanup_error = None;
        if let Some(mut child) = self.process.lock().unwrap().take() {
            let deadline = Instant::now() + Duration::from_secs(3);
            let mut exited = false;
            while Instant::now() < deadline {
                match child.try_wait() {
                    Ok(Some(_)) => {
                        exited = true;
                        break;
                    }
                    Ok(None) => {}
                    Err(error) => {
                        cleanup_error = Some(error);
                        break;
                    }
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            if !exited {
                let _ = child.kill();
                let _ = child.wait();
            }
        }

        self.profile_dir.lock().unwrap().take();
        let result = cleanup_error.map_or(Ok(()), |error| Err(RwError::Io(error)));
        self.lifecycle.finish(sender, &result, true);
        result
    }
}

impl Drop for BrowserInner {
    fn drop(&mut self) {
        let _ = self.close_last_owner_without_gil();
    }
}

async fn close_browser_cleanup(browser: Arc<BrowserInner>) -> RwResult<()> {
    if browser.process.lock().unwrap().is_some() {
        let client = Arc::clone(&browser.client);
        let _ = client
            .send("Browser.close", json!({}), None, Duration::from_secs(3))
            .await;
    }
    browser.client.close();
    browser.attached_pages.clear();

    tokio::task::spawn_blocking(move || -> RwResult<()> {
        let mut cleanup_error = None;
        if let Some(mut child) = browser.process.lock().unwrap().take() {
            let deadline = Instant::now() + Duration::from_secs(3);
            let mut exited = false;
            while Instant::now() < deadline {
                match child.try_wait() {
                    Ok(Some(_)) => {
                        exited = true;
                        break;
                    }
                    Ok(None) => {}
                    Err(error) => {
                        cleanup_error = Some(error);
                        break;
                    }
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            if !exited {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
        browser.profile_dir.lock().unwrap().take();
        cleanup_error.map_or(Ok(()), |error| Err(RwError::Io(error)))
    })
    .await
    .map_err(|error| RwError::Message(error.to_string()))??;
    Ok(())
}

async fn close_browser_async(browser: Arc<BrowserInner>) -> RwResult<()> {
    let lifecycle = Arc::clone(&browser.lifecycle);
    single_flight_close(lifecycle, true, move || close_browser_cleanup(browser)).await
}

fn close_browser_blocking(browser: Arc<BrowserInner>) -> RwResult<()> {
    let runtime = browser.runtime.handle().clone();
    run_detached(move || runtime.block_on(close_browser_async(browser)))
}

struct ContextInner {
    browser: Arc<BrowserInner>,
    context_id: Option<String>,
    lifecycle: Arc<CloseLifecycle>,
}

fn browser_context_create_params(options_json: Option<&str>) -> RwResult<Value> {
    let options = match options_json {
        Some(value) if !value.trim().is_empty() => {
            serde_json::from_str::<BrowserContextOptions>(value)?
        }
        _ => BrowserContextOptions::default(),
    };
    let mut params = json!({ "disposeOnDetach": true });
    if let Some(proxy) = &options.proxy {
        params["proxyServer"] = Value::String(proxy_server(proxy)?.to_string());
        if let Some(bypass) = normalized_proxy_bypass(proxy) {
            params["proxyBypassList"] = Value::String(bypass);
        }
        if let Some(username) = proxy.username.as_deref() {
            params["proxyUsername"] = Value::String(username.to_string());
        }
        if let Some(password) = proxy.password.as_deref() {
            params["proxyPassword"] = Value::String(password.to_string());
        }
    }
    Ok(params)
}

async fn close_context_cleanup(context: Arc<ContextInner>) -> RwResult<()> {
    if let Some(context_id) = context.context_id.clone() {
        context
            .browser
            .client
            .send(
                "Target.disposeBrowserContext",
                json!({ "browserContextId": context_id }),
                None,
                Duration::from_secs(5),
            )
            .await?;
    }
    Ok(())
}

async fn close_context_async(context: Arc<ContextInner>) -> RwResult<()> {
    let lifecycle = Arc::clone(&context.lifecycle);
    single_flight_close(lifecycle, false, move || close_context_cleanup(context)).await
}

impl ContextInner {
    fn close_in_background(&self) {
        if self.lifecycle.is_closing_or_closed() {
            return;
        }
        let Some(context_id) = self.context_id.clone() else {
            return;
        };
        let browser = Arc::clone(&self.browser);
        let lifecycle = Arc::clone(&self.lifecycle);
        let runtime = browser.runtime.handle().clone();
        runtime.spawn(async move {
            let _ = single_flight_close(lifecycle, false, move || async move {
                browser
                    .client
                    .send(
                        "Target.disposeBrowserContext",
                        json!({ "browserContextId": context_id }),
                        None,
                        Duration::from_secs(5),
                    )
                    .await
                    .map(|_| ())
            })
            .await;
        });
    }
}

impl Drop for ContextInner {
    fn drop(&mut self) {
        self.close_in_background();
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
struct DefaultTimeoutSlots {
    page_default: Option<f64>,
    context_default: Option<f64>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
struct DefaultTimeoutRegister {
    general: DefaultTimeoutSlots,
    navigation: DefaultTimeoutSlots,
}

impl DefaultTimeoutRegister {
    fn resolve(&self, explicit: Option<f64>, navigation: bool) -> Option<f64> {
        explicit.or_else(|| {
            if navigation {
                self.navigation
                    .page_default
                    .or(self.navigation.context_default)
                    .or(self.general.page_default)
                    .or(self.general.context_default)
            } else {
                self.general.page_default.or(self.general.context_default)
            }
        })
    }
}

struct PageInner {
    browser: Arc<BrowserInner>,
    target_id: String,
    registry_generation: u64,
    session_id: String,
    context_id: Option<String>,
    main_frame_id: Mutex<Option<String>>,
    frame_state: Mutex<PageFrameState>,
    network_requests: Arc<Mutex<NetworkRequestStore>>,
    event_stream_start_cursor: u64,
    background_override_active: Arc<AtomicBool>,
    screenshot_lock: Arc<tokio::sync::Mutex<()>>,
    mouse_dispatch_lock: Arc<tokio::sync::Mutex<()>>,
    default_timeouts: Mutex<DefaultTimeoutRegister>,
    lifecycle: Arc<CloseLifecycle>,
    target_closed: AtomicBool,
    crashed: AtomicBool,
    close_target_on_drop: AtomicBool,
}

struct CreatedTargetGuard {
    browser: Arc<BrowserInner>,
    target_id: Option<String>,
}

struct AttachedSessionCleanup {
    browser: Arc<BrowserInner>,
    session_id: Mutex<Option<String>>,
    started: AtomicBool,
    done: watch::Sender<bool>,
}

impl AttachedSessionCleanup {
    fn new(browser: Arc<BrowserInner>) -> Self {
        let (done, _) = watch::channel(false);
        Self {
            browser,
            session_id: Mutex::new(None),
            started: AtomicBool::new(false),
            done,
        }
    }

    fn set_session(&self, session_id: String) {
        *self.session_id.lock().unwrap() = Some(session_id);
    }

    fn disarm(&self) {
        self.session_id.lock().unwrap().take();
    }

    fn start(self: &Arc<Self>) -> bool {
        if self.session_id.lock().unwrap().is_none() {
            return self.started.load(Ordering::SeqCst);
        }
        if self
            .started
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return true;
        }
        let Some(session_id) = self.session_id.lock().unwrap().take() else {
            self.done.send_replace(true);
            return false;
        };
        let cleanup = Arc::clone(self);
        let browser = Arc::clone(&self.browser);
        let runtime = browser.runtime.handle().clone();
        runtime.spawn(async move {
            let _ = browser
                .client
                .send(
                    "Target.detachFromTarget",
                    json!({ "sessionId": session_id }),
                    None,
                    Duration::from_secs(1),
                )
                .await;
            cleanup.done.send_replace(true);
        });
        true
    }

    async fn detach(self: &Arc<Self>) {
        let mut done = self.done.subscribe();
        if !self.start() {
            return;
        }
        while !*done.borrow() {
            if done.changed().await.is_err() {
                break;
            }
        }
    }
}

struct AttachedSessionGuard {
    cleanup: Arc<AttachedSessionCleanup>,
}

impl AttachedSessionGuard {
    fn new(cleanup: Arc<AttachedSessionCleanup>) -> Self {
        Self { cleanup }
    }

    fn disarm(self) {
        self.cleanup.disarm();
    }

    async fn detach(self) {
        self.cleanup.detach().await;
    }
}

impl Drop for AttachedSessionGuard {
    fn drop(&mut self) {
        self.cleanup.start();
    }
}

struct UnregisteredPage {
    page: Arc<PageInner>,
    session_guard: AttachedSessionGuard,
}

impl CreatedTargetGuard {
    fn new(browser: Arc<BrowserInner>, target_id: String) -> Self {
        Self {
            browser,
            target_id: Some(target_id),
        }
    }

    fn disarm(mut self) {
        self.target_id.take();
    }
}

async fn create_target_cancellation_safe(
    browser: Arc<BrowserInner>,
    params: Value,
) -> RwResult<CreatedTargetGuard> {
    let task_browser = Arc::clone(&browser);
    tokio::spawn(async move {
        let target = task_browser
            .client
            .send("Target.createTarget", params, None, Duration::from_secs(10))
            .await?;
        let target_id = target
            .get("targetId")
            .and_then(Value::as_str)
            .ok_or_else(|| RwError::Message("CDP did not return a targetId".to_string()))?
            .to_string();
        Ok(CreatedTargetGuard::new(task_browser, target_id))
    })
    .await
    .map_err(|error| RwError::Message(error.to_string()))?
}

impl Drop for CreatedTargetGuard {
    fn drop(&mut self) {
        let Some(target_id) = self.target_id.take() else {
            return;
        };
        let browser = Arc::clone(&self.browser);
        let runtime = browser.runtime.handle().clone();
        runtime.spawn(async move {
            let _ = browser
                .client
                .send(
                    "Target.closeTarget",
                    json!({ "targetId": target_id }),
                    None,
                    Duration::from_secs(5),
                )
                .await;
        });
    }
}

impl PageInner {
    fn mark_delivered(&self) {
        self.close_target_on_drop.store(false, Ordering::SeqCst);
    }

    fn close_in_background(&self) {
        if !self.close_target_on_drop.swap(false, Ordering::SeqCst)
            || self.lifecycle.is_closing_or_closed()
        {
            return;
        }
        let browser = Arc::clone(&self.browser);
        let target_id = self.target_id.clone();
        let lifecycle = Arc::clone(&self.lifecycle);
        let runtime = browser.runtime.handle().clone();
        runtime.spawn(async move {
            let _ = single_flight_close(lifecycle, false, move || async move {
                browser
                    .client
                    .send(
                        "Target.closeTarget",
                        json!({ "targetId": target_id }),
                        None,
                        Duration::from_secs(5),
                    )
                    .await
                    .map(|_| ())
            })
            .await;
        });
    }
}

impl Drop for PageInner {
    fn drop(&mut self) {
        self.browser.attached_pages.remove_page(
            &self.target_id,
            self.registry_generation,
            self as *const PageInner,
        );
        self.close_in_background();
    }
}

#[derive(Clone)]
struct NetworkRequestStore {
    requests: HashMap<String, NetworkRequestEntry>,
    next_applied_seq: u64,
    applied_order: VecDeque<(u64, String)>,
    reset_clock: Arc<AtomicU64>,
    reset_generation: u64,
    reset_cursor: u64,
}

#[derive(Clone)]
struct NetworkRequestSnapshot {
    seq: u64,
    request: Value,
    redirect_ancestry: Vec<u64>,
}

impl NetworkRequestSnapshot {
    fn clone_pruning_redirects_before(&self, reset_cursor: u64) -> Self {
        let mut snapshot = self.clone();
        let mut current = &mut snapshot.request;
        let mut retained_ancestors = 0;
        for ancestor_seq in &self.redirect_ancestry {
            if *ancestor_seq < reset_cursor {
                current.as_object_mut().unwrap().remove("redirected_from");
                snapshot.redirect_ancestry.truncate(retained_ancestors);
                return snapshot;
            }
            let Some(redirected_from) = current.get_mut("redirected_from") else {
                snapshot.redirect_ancestry.truncate(retained_ancestors);
                return snapshot;
            };
            current = redirected_from;
            retained_ancestors += 1;
        }
        if current.get("redirected_from").is_some() {
            current.as_object_mut().unwrap().remove("redirected_from");
        }
        snapshot
    }
}

impl NetworkRequestStore {
    fn new(next_applied_seq: u64) -> Self {
        Self {
            requests: HashMap::new(),
            next_applied_seq,
            applied_order: VecDeque::new(),
            reset_clock: Arc::new(AtomicU64::new(0)),
            reset_generation: 0,
            reset_cursor: next_applied_seq,
        }
    }

    fn reset_after_overflow(&mut self, next_applied_seq: u64) {
        self.requests.clear();
        self.next_applied_seq = next_applied_seq;
        self.applied_order.clear();
        self.reset_generation = self.reset_clock.fetch_add(1, Ordering::SeqCst) + 1;
        self.reset_cursor = next_applied_seq;
    }

    fn record_applied_request(&mut self, seq: u64, request_id: String) {
        self.applied_order.push_back((seq, request_id));
        while self.applied_order.len() > CDP_EVENT_LOG_LIMIT {
            let Some((oldest_seq, oldest_request_id)) = self.applied_order.pop_front() else {
                break;
            };
            if let Some(entry) = self.requests.get_mut(&oldest_request_id) {
                entry.applied_by_seq.remove(&oldest_seq);
            }
        }
    }

    fn merge_committed(&mut self, committed: &Self) {
        if committed.reset_generation > self.reset_generation {
            let reset_cursor = committed.reset_cursor;
            self.applied_order.retain(|(seq, _)| *seq >= reset_cursor);
            self.requests.retain(|_, entry| {
                entry.applied_by_seq.retain(|seq, _| *seq >= reset_cursor);
                for snapshot in entry.applied_by_seq.values_mut() {
                    *snapshot = snapshot.clone_pruning_redirects_before(reset_cursor);
                }
                if let Some((_, newest)) = entry.applied_by_seq.last_key_value() {
                    entry.current = newest.clone();
                    true
                } else {
                    false
                }
            });
            self.reset_generation = committed.reset_generation;
            self.reset_cursor = reset_cursor;
        }
        let reset_cursor = self.reset_cursor;
        let committed_predates_reset = committed.reset_generation < self.reset_generation;
        for (request_id, committed_entry) in &committed.requests {
            let mut committed_mutations = committed_entry.applied_by_seq.range(reset_cursor..);
            let Some((first_seq, first_snapshot)) = committed_mutations.next() else {
                continue;
            };
            let first_snapshot = if committed_predates_reset {
                first_snapshot.clone_pruning_redirects_before(reset_cursor)
            } else {
                first_snapshot.clone()
            };
            let live_entry =
                self.requests
                    .entry(request_id.clone())
                    .or_insert_with(|| NetworkRequestEntry {
                        current: first_snapshot.clone(),
                        applied_by_seq: BTreeMap::new(),
                    });
            live_entry
                .applied_by_seq
                .entry(*first_seq)
                .or_insert(first_snapshot);
            for (seq, snapshot) in committed_mutations {
                let snapshot = if committed_predates_reset {
                    snapshot.clone_pruning_redirects_before(reset_cursor)
                } else {
                    snapshot.clone()
                };
                live_entry.applied_by_seq.entry(*seq).or_insert(snapshot);
            }
            if let Some((_, newest)) = live_entry.applied_by_seq.last_key_value() {
                live_entry.current = newest.clone();
            }
        }
        self.next_applied_seq = self.next_applied_seq.max(committed.next_applied_seq);

        let mut applied = BTreeMap::new();
        for (seq, request_id) in self
            .applied_order
            .iter()
            .chain(committed.applied_order.iter())
            .filter(|(seq, _)| *seq >= reset_cursor)
        {
            applied.insert((*seq, request_id.clone()), ());
        }
        let mut retained = applied
            .into_keys()
            .rev()
            .take(CDP_EVENT_LOG_LIMIT)
            .collect::<Vec<_>>();
        retained.reverse();
        let retained_set = retained.iter().cloned().collect::<HashSet<_>>();
        self.applied_order = retained.iter().cloned().collect();
        for (request_id, entry) in &mut self.requests {
            entry
                .applied_by_seq
                .retain(|seq, _| retained_set.contains(&(*seq, request_id.clone())));
            if let Some((_, newest)) = entry.applied_by_seq.last_key_value() {
                entry.current = newest.clone();
            }
        }
    }
}

impl Default for NetworkRequestStore {
    fn default() -> Self {
        Self::new(0)
    }
}

#[derive(Clone)]
struct NetworkRequestEntry {
    current: NetworkRequestSnapshot,
    applied_by_seq: BTreeMap<u64, NetworkRequestSnapshot>,
}

#[derive(Clone, Debug)]
struct PageFrameRecord {
    id: String,
    parent_id: Option<String>,
    name: String,
    url: String,
    session_id: String,
}

#[derive(Debug)]
struct PageFrameState {
    main_session_id: String,
    frame_sessions: HashMap<String, String>,
    session_frames: HashMap<String, String>,
    frames: HashMap<String, PageFrameRecord>,
    child_order: HashMap<String, Vec<String>>,
    frame_session_errors: HashMap<String, String>,
    iframe_sessions_armed: HashSet<String>,
    iframe_sessions_ready: HashSet<String>,
    iframe_setup_started: HashSet<String>,
    session_updates: watch::Sender<u64>,
}

impl PageFrameState {
    fn new(main_session_id: String) -> Self {
        let (session_updates, _) = watch::channel(0);
        Self {
            main_session_id,
            frame_sessions: HashMap::new(),
            session_frames: HashMap::new(),
            frames: HashMap::new(),
            child_order: HashMap::new(),
            frame_session_errors: HashMap::new(),
            iframe_sessions_armed: HashSet::new(),
            iframe_sessions_ready: HashSet::new(),
            iframe_setup_started: HashSet::new(),
            session_updates,
        }
    }

    fn subscribe_session_updates(&self) -> watch::Receiver<u64> {
        self.session_updates.subscribe()
    }

    fn notify_session_update(&self) {
        self.session_updates
            .send_modify(|generation| *generation = generation.wrapping_add(1));
    }

    fn record_frame_session_error(&mut self, frame_id: &str, error: String) {
        self.frame_session_errors
            .insert(frame_id.to_string(), error);
        self.notify_session_update();
    }

    fn session_still_owns_frame(&self, frame_id: &str, session_id: &str) -> bool {
        self.session_frames.get(session_id).map(String::as_str) == Some(frame_id)
            && self.frame_sessions.get(frame_id).map(String::as_str) == Some(session_id)
    }

    fn record_frame_session_error_if_current(
        &mut self,
        frame_id: &str,
        session_id: &str,
        error: String,
    ) {
        if self.session_still_owns_frame(frame_id, session_id) {
            self.record_frame_session_error(frame_id, error);
        }
    }

    fn mark_iframe_setup_started(&mut self, session_id: &str) -> bool {
        self.iframe_setup_started.insert(session_id.to_string())
    }

    fn mark_iframe_session_armed(&mut self, session_id: &str) {
        self.iframe_sessions_armed.insert(session_id.to_string());
        self.notify_session_update();
    }

    fn mark_iframe_session_ready(&mut self, frame_id: &str, session_id: &str) {
        if self.session_still_owns_frame(frame_id, session_id) {
            self.iframe_sessions_ready.insert(session_id.to_string());
            self.notify_session_update();
        }
    }

    fn owns_session(&self, session_id: &str) -> bool {
        session_id == self.main_session_id || self.session_frames.contains_key(session_id)
    }

    fn session_ids(&self) -> Vec<String> {
        let mut sessions = vec![self.main_session_id.clone()];
        for session_id in self.session_frames.keys() {
            if !sessions.iter().any(|existing| existing == session_id) {
                sessions.push(session_id.clone());
            }
        }
        sessions
    }

    fn record_session_for_frame(&mut self, frame_id: &str, session_id: &str) {
        self.frame_session_errors.remove(frame_id);
        if let Some(previous_session_id) = self
            .frame_sessions
            .insert(frame_id.to_string(), session_id.to_string())
        {
            if previous_session_id != session_id
                && self
                    .session_frames
                    .get(&previous_session_id)
                    .map(String::as_str)
                    == Some(frame_id)
            {
                self.session_frames.remove(&previous_session_id);
            }
        }
        if let Some(previous_frame_id) = self
            .session_frames
            .insert(session_id.to_string(), frame_id.to_string())
        {
            if previous_frame_id != frame_id
                && self
                    .frame_sessions
                    .get(&previous_frame_id)
                    .map(String::as_str)
                    == Some(session_id)
            {
                self.frame_sessions.remove(&previous_frame_id);
            }
        }
        if let Some(record) = self.frames.get_mut(frame_id) {
            record.session_id = session_id.to_string();
        }
    }

    fn detach_session(&mut self, detached_session_id: &str) {
        let root_frame_id = self.session_frames.remove(detached_session_id);
        let replacement_session_id = root_frame_id
            .as_deref()
            .and_then(|frame_id| self.frames.get(frame_id))
            .and_then(|record| record.parent_id.as_deref())
            .and_then(|parent_id| self.frame_sessions.get(parent_id))
            .filter(|session_id| session_id.as_str() != detached_session_id)
            .cloned()
            .unwrap_or_else(|| self.main_session_id.clone());
        let remapped_frame_ids = self
            .frame_sessions
            .iter()
            .filter_map(|(frame_id, session_id)| {
                (session_id == detached_session_id).then_some(frame_id.clone())
            })
            .collect::<Vec<_>>();
        for frame_id in remapped_frame_ids {
            self.frame_sessions
                .insert(frame_id.clone(), replacement_session_id.clone());
            self.frame_session_errors.remove(&frame_id);
            if let Some(record) = self.frames.get_mut(&frame_id) {
                record.session_id = replacement_session_id.clone();
            }
        }
        self.iframe_sessions_armed.remove(detached_session_id);
        self.iframe_sessions_ready.remove(detached_session_id);
        self.iframe_setup_started.remove(detached_session_id);
        self.notify_session_update();
    }

    fn record_frame(
        &mut self,
        frame_id: String,
        parent_id: Option<String>,
        name: Option<String>,
        url: Option<String>,
        event_session_id: String,
    ) {
        let session_id = self
            .frame_sessions
            .entry(frame_id.clone())
            .or_insert(event_session_id)
            .clone();
        let entry = self
            .frames
            .entry(frame_id.clone())
            .or_insert_with(|| PageFrameRecord {
                id: frame_id.clone(),
                parent_id: parent_id.clone(),
                name: String::new(),
                url: String::new(),
                session_id: session_id.clone(),
            });
        entry.parent_id = parent_id.clone().or_else(|| entry.parent_id.clone());
        if let Some(name) = name {
            entry.name = name;
        }
        if let Some(url) = url {
            entry.url = url;
        }
        entry.session_id = session_id;

        if let Some(parent_id) = parent_id {
            let children = self.child_order.entry(parent_id).or_default();
            if !children.iter().any(|existing| existing == &frame_id) {
                children.push(frame_id);
            }
        }
    }

    fn remove_frame(&mut self, frame_id: &str) {
        let children = self.child_order.remove(frame_id).unwrap_or_default();
        for child in children {
            self.remove_frame(&child);
        }
        self.frames.remove(frame_id);
        if let Some(session_id) = self.frame_sessions.remove(frame_id) {
            if self.session_frames.get(&session_id).map(String::as_str) == Some(frame_id) {
                self.session_frames.remove(&session_id);
            }
        }
        for children in self.child_order.values_mut() {
            children.retain(|child| child != frame_id);
        }
    }
}

fn resolve_cached_main_frame_url(
    main_frame_id: Option<&str>,
    state: &PageFrameState,
) -> Option<String> {
    let frame_id = main_frame_id?;
    state
        .frames
        .get(frame_id)
        .map(|record| record.url.clone())
        .filter(|url| !url.is_empty())
}

impl PageInner {
    async fn main_frame_id(
        &self,
        client: &CdpClient,
        session_id: &str,
        timeout: Duration,
    ) -> RwResult<String> {
        if let Some(frame_id) = self.main_frame_id.lock().unwrap().clone() {
            return Ok(frame_id);
        }
        let frame_tree = client
            .send("Page.getFrameTree", json!({}), Some(session_id), timeout)
            .await?;
        let frame_id = frame_tree
            .pointer("/frameTree/frame/id")
            .and_then(Value::as_str)
            .ok_or_else(|| RwError::Message("CDP did not return a main frame id".to_string()))?
            .to_string();
        *self.main_frame_id.lock().unwrap() = Some(frame_id.clone());
        Ok(frame_id)
    }

    fn session_for_frame_id(&self, frame_id: &str) -> String {
        self.frame_state
            .lock()
            .unwrap()
            .frame_sessions
            .get(frame_id)
            .cloned()
            .unwrap_or_else(|| self.session_id.clone())
    }

    fn cached_main_frame_url(&self) -> Option<String> {
        let main_id = self.main_frame_id.lock().unwrap().clone();
        let state = self.frame_state.lock().unwrap();
        resolve_cached_main_frame_url(main_id.as_deref(), &state)
    }

    fn record_frame_navigation_url(&self, frame_id: &str, url: &str, session_id: &str) {
        self.frame_state.lock().unwrap().record_frame(
            frame_id.to_string(),
            None,
            None,
            Some(url.to_string()),
            session_id.to_string(),
        );
    }

    fn record_main_frame_navigation_url(&self, frame_id: &str, url: &str) {
        *self.main_frame_id.lock().unwrap() = Some(frame_id.to_string());
        self.record_frame_navigation_url(frame_id, url, &self.session_id);
    }

    fn frame_tree_payload(&self) -> Value {
        let state = self.frame_state.lock().unwrap();
        let root_id = self.main_frame_id.lock().unwrap().clone().or_else(|| {
            state
                .frames
                .values()
                .find(|record| record.parent_id.is_none())
                .map(|record| record.id.clone())
        });
        let Some(root_id) = root_id else {
            return json!({ "frameTree": Value::Null });
        };
        let Some(root) = build_frame_tree_node(&state, &root_id) else {
            return json!({ "frameTree": Value::Null });
        };
        json!({ "frameTree": root })
    }
}

fn build_frame_tree_node(state: &PageFrameState, frame_id: &str) -> Option<Value> {
    let mut visited = HashSet::new();
    build_frame_tree_node_guarded(state, frame_id, &mut visited, 0)
}

fn build_frame_tree_node_guarded(
    state: &PageFrameState,
    frame_id: &str,
    visited: &mut HashSet<String>,
    depth: usize,
) -> Option<Value> {
    if depth >= MAX_FRAME_TREE_DEPTH || !visited.insert(frame_id.to_string()) {
        return None;
    }
    let record = state.frames.get(frame_id)?;
    let mut frame = json!({
        "id": record.id,
        "url": record.url,
        "name": record.name,
    });
    if let Some(parent_id) = &record.parent_id {
        frame["parentId"] = Value::String(parent_id.clone());
    }
    let child_ids = state.child_order.get(frame_id).cloned().unwrap_or_default();
    let mut child_frames = Vec::new();
    for child_id in child_ids {
        if let Some(child) = build_frame_tree_node_guarded(state, &child_id, visited, depth + 1) {
            child_frames.push(child);
        }
    }
    let mut node = json!({ "frame": frame });
    if !child_frames.is_empty() {
        node["childFrames"] = Value::Array(child_frames);
    }
    Some(node)
}

#[cfg(feature = "python")]
#[pyclass(name = "Browser")]
struct PyBrowser {
    inner: Arc<BrowserInner>,
}

#[cfg(feature = "python")]
#[pyclass(name = "BrowserContext")]
struct PyBrowserContext {
    inner: Arc<ContextInner>,
}

#[cfg(feature = "python")]
#[pyclass(name = "Page")]
struct PyPage {
    inner: Arc<PageInner>,
}

#[cfg(feature = "python")]
#[pyclass(name = "CDPSession")]
struct PyCdpSession {
    browser: Arc<BrowserInner>,
    session_id: Option<String>,
    detached: AtomicBool,
}

#[cfg(feature = "python")]
#[pyclass(name = "_CdpEventWaiter")]
struct PyCdpEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    session_id: Option<String>,
    method: String,
}

#[cfg(feature = "python")]
#[pyclass(name = "Worker")]
struct PyWorker {
    browser: Arc<BrowserInner>,
    target_id: String,
    session_id: String,
    url: String,
}

#[cfg(feature = "python")]
#[pyclass(name = "_NetworkEventWaiter")]
struct PyNetworkEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    event_log: Arc<Mutex<CdpEventLog>>,
    cursor: Mutex<u64>,
    session_id: String,
    kind: String,
    requests: Arc<Mutex<NetworkRequestStore>>,
}

#[cfg(feature = "python")]
#[pyclass(name = "_PageEventStream")]
struct PyPageEventStream {
    browser: Arc<BrowserInner>,
    receiver: Arc<Mutex<Option<broadcast::Receiver<Value>>>>,
    event_log: Arc<Mutex<CdpEventLog>>,
    cursor: Arc<Mutex<u64>>,
    session_id: String,
    requests: Arc<Mutex<NetworkRequestStore>>,
    state: Arc<Mutex<Option<PageEventStreamState>>>,
    pending_batch: Arc<Mutex<Option<PendingPageEventBatch>>>,
    close_tx: watch::Sender<bool>,
    closed: Arc<AtomicBool>,
    runtime_enabled: Arc<AtomicBool>,
}

#[derive(Clone)]
struct PageEventStreamState {
    network: NetworkObservationState,
    ready: BTreeMap<u64, Value>,
}

#[cfg(feature = "python")]
struct PageEventStreamLease {
    receiver: Option<broadcast::Receiver<Value>>,
    state: Option<PageEventStreamState>,
    rollback_state: Option<PageEventStreamState>,
    cursor: u64,
    rollback_cursor: u64,
    delivered: bool,
    receiver_slot: Arc<Mutex<Option<broadcast::Receiver<Value>>>>,
    state_slot: Arc<Mutex<Option<PageEventStreamState>>>,
    cursor_slot: Arc<Mutex<u64>>,
    requests: Arc<Mutex<NetworkRequestStore>>,
    working_requests: Arc<Mutex<NetworkRequestStore>>,
}

#[cfg(feature = "python")]
struct PendingPageEventBatch {
    lease: PageEventStreamLease,
    terminal: bool,
    closed: Arc<AtomicBool>,
}

#[cfg(feature = "python")]
impl PendingPageEventBatch {
    fn acknowledge(self) {
        let Self {
            lease,
            terminal,
            closed,
        } = self;
        lease.deliver();
        if terminal {
            closed.store(true, Ordering::SeqCst);
        }
    }
}

#[cfg(feature = "python")]
impl PageEventStreamLease {
    fn deliver(mut self) {
        self.delivered = true;
    }
}

#[cfg(feature = "python")]
impl Drop for PageEventStreamLease {
    fn drop(&mut self) {
        *self.receiver_slot.lock().unwrap() = self.receiver.take();
        if self.delivered {
            *self.state_slot.lock().unwrap() = self.state.take();
            *self.cursor_slot.lock().unwrap() = self.cursor;
            let committed = self.working_requests.lock().unwrap().clone();
            self.requests.lock().unwrap().merge_committed(&committed);
        } else {
            *self.state_slot.lock().unwrap() = self.rollback_state.take();
            *self.cursor_slot.lock().unwrap() = self.rollback_cursor;
        }
    }
}

impl PageEventStreamState {
    fn new() -> Self {
        Self {
            network: NetworkObservationState::new(),
            ready: BTreeMap::new(),
        }
    }
}

#[cfg(feature = "python")]
#[pyclass(name = "_RouteEventWaiter")]
struct PyRouteEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    session_id: String,
}

#[cfg(feature = "python")]
#[pyclass(name = "_AuthEventWaiter")]
struct PyAuthEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    session_id: String,
}

#[cfg(feature = "python")]
#[pyclass(name = "_DialogEventWaiter")]
struct PyDialogEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    session_id: String,
}

#[cfg(feature = "python")]
#[pyclass(name = "_ConsoleEventWaiter")]
struct PyConsoleEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    session_id: String,
}

#[cfg(feature = "python")]
#[pyclass(name = "_WebSocketEventWaiter")]
struct PyWebSocketEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    session_id: String,
    kind: String,
    request_id: Option<String>,
}

#[cfg(feature = "python")]
#[pyclass(name = "_BindingEventWaiter")]
struct PyBindingEventWaiter {
    page: Arc<PageInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    name: String,
}

#[cfg(feature = "python")]
#[pyclass(name = "_DownloadEventWaiter")]
struct PyDownloadEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    active_downloads: Mutex<HashMap<String, Value>>,
    download_path: String,
}

#[cfg(feature = "python")]
#[pyclass(name = "_FileChooserEventWaiter")]
struct PyFileChooserEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    session_id: String,
}

#[cfg(feature = "python")]
#[pyclass(name = "_PopupEventWaiter")]
struct PyPopupEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    opener_target_id: String,
    seen_target_ids: Arc<Mutex<HashSet<String>>>,
}

#[cfg(feature = "python")]
#[pyclass(name = "_WorkerEventWaiter")]
struct PyWorkerEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    opener_target_id: String,
}

#[cfg(feature = "python")]
#[pyclass(name = "_WorkerCloseEventWaiter")]
struct PyWorkerCloseEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    target_id: String,
    session_id: String,
}

#[cfg(feature = "python")]
#[pyclass(name = "_ServiceWorkerEventWaiter")]
struct PyServiceWorkerEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    context_id: Option<String>,
}

#[cfg(feature = "python")]
#[pyclass(name = "_BackgroundPageEventWaiter")]
struct PyBackgroundPageEventWaiter {
    browser: Arc<BrowserInner>,
    receiver: Mutex<Option<broadcast::Receiver<Value>>>,
    context_id: Option<String>,
}

#[cfg(feature = "python")]
#[pymethods]
impl PyBrowser {
    fn new_page_async(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let browser = Arc::clone(&self.inner);
        let runtime = browser.runtime.handle().clone();
        let creation = runtime.spawn(create_page_async(browser, None));
        let creation_abort = creation.abort_handle();
        python_future_on(
            py,
            runtime,
            async move {
                let _creation_guard = SpawnedTaskAbortGuard(creation_abort);
                creation
                    .await
                    .map_err(|error| RwError::Message(error.to_string()))?
            },
            |py, inner| Ok(Py::new(py, PyPage { inner })?.into_any()),
        )
    }

    fn new_page(&self) -> PyResult<PyPage> {
        create_page(Arc::clone(&self.inner), None).map_err(py_err)
    }

    #[pyo3(signature = (options_json=None))]
    fn new_context(&self, options_json: Option<&str>) -> PyResult<PyBrowserContext> {
        let params = browser_context_create_params(options_json).map_err(py_err)?;
        let browser = Arc::clone(&self.inner);
        let result = browser
            .block_on(async {
                browser
                    .client
                    .send(
                        "Target.createBrowserContext",
                        params,
                        None,
                        Duration::from_secs(5),
                    )
                    .await
            })
            .map_err(py_err)?;
        let context_id = result
            .get("browserContextId")
            .and_then(Value::as_str)
            .ok_or_else(|| PyRuntimeError::new_err("CDP did not return a browserContextId"))?
            .to_string();
        Ok(PyBrowserContext {
            inner: Arc::new(ContextInner {
                browser: Arc::clone(&self.inner),
                context_id: Some(context_id),
                lifecycle: Arc::new(CloseLifecycle::new()),
            }),
        })
    }

    #[pyo3(signature = (options_json=None))]
    fn new_context_async(&self, py: Python<'_>, options_json: Option<&str>) -> PyResult<Py<PyAny>> {
        let params = browser_context_create_params(options_json).map_err(py_err)?;
        let browser = Arc::clone(&self.inner);
        let runtime = browser.runtime.handle().clone();
        let future_browser = Arc::clone(&browser);
        let creation = runtime.spawn(async move {
            let result = future_browser
                .client
                .send(
                    "Target.createBrowserContext",
                    params,
                    None,
                    Duration::from_secs(5),
                )
                .await?;
            let context_id = result
                .get("browserContextId")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    RwError::Message("CDP did not return a browserContextId".to_string())
                })?
                .to_string();
            Ok(PyBrowserContext {
                inner: Arc::new(ContextInner {
                    browser: future_browser,
                    context_id: Some(context_id),
                    lifecycle: Arc::new(CloseLifecycle::new()),
                }),
            })
        });
        python_future_on(
            py,
            runtime,
            async move {
                creation
                    .await
                    .map_err(|error| RwError::Message(error.to_string()))?
            },
            |py, context| Ok(Py::new(py, context)?.into_any()),
        )
    }

    fn close(&self) -> PyResult<()> {
        close_browser_blocking(Arc::clone(&self.inner)).map_err(py_err)
    }

    fn close_async(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let browser = Arc::clone(&self.inner);
        let runtime = browser.runtime.handle().clone();
        let cleanup = runtime.spawn(close_browser_async(browser));
        python_future_on(
            py,
            runtime,
            async move {
                cleanup
                    .await
                    .map_err(|error| RwError::Message(error.to_string()))?
            },
            |py, ()| Ok(py.None()),
        )
    }

    fn is_connected(&self) -> bool {
        !self.inner.lifecycle.is_closed() && self.inner.client.is_connected()
    }

    fn single_process_fallback(&self) -> bool {
        self.inner.single_process_fallback
    }

    fn sent_runtime_enable_count(&self) -> u64 {
        self.inner.client.sent_runtime_enable_count()
    }

    fn sent_target_close_count(&self) -> u64 {
        self.inner.client.sent_target_close_count()
    }

    fn sent_context_dispose_count(&self) -> u64 {
        self.inner.client.sent_context_dispose_count()
    }

    fn pending_command_count(&self) -> usize {
        self.inner.client.pending_command_count()
    }

    fn version(&self) -> PyResult<String> {
        let browser = Arc::clone(&self.inner);
        let result = browser
            .block_on(async {
                browser
                    .client
                    .send(
                        "Browser.getVersion",
                        json!({}),
                        None,
                        Duration::from_secs(5),
                    )
                    .await
            })
            .map_err(py_err)?;
        Ok(result
            .get("product")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string())
    }

    #[getter]
    fn ws_endpoint(&self) -> String {
        self.inner.ws_endpoint.clone()
    }

    fn browser_cdp_session(&self) -> PyCdpSession {
        PyCdpSession {
            browser: Arc::clone(&self.inner),
            session_id: None,
            detached: AtomicBool::new(false),
        }
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn list_service_workers(&self, timeout_ms: Option<f64>) -> PyResult<Vec<PyWorker>> {
        list_service_workers_for_context(Arc::clone(&self.inner), None, timeout_ms)
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn service_worker_event_waiter(
        &self,
        timeout_ms: Option<f64>,
    ) -> PyResult<PyServiceWorkerEventWaiter> {
        service_worker_event_waiter_for_context(Arc::clone(&self.inner), None, timeout_ms)
    }

    #[pyo3(signature = (context_id, timeout_ms=None))]
    fn list_service_workers_for_context_id(
        &self,
        context_id: String,
        timeout_ms: Option<f64>,
    ) -> PyResult<Vec<PyWorker>> {
        list_service_workers_for_context(Arc::clone(&self.inner), Some(context_id), timeout_ms)
    }

    #[pyo3(signature = (context_id, timeout_ms=None))]
    fn service_worker_event_waiter_for_context_id(
        &self,
        context_id: String,
        timeout_ms: Option<f64>,
    ) -> PyResult<PyServiceWorkerEventWaiter> {
        service_worker_event_waiter_for_context(
            Arc::clone(&self.inner),
            Some(context_id),
            timeout_ms,
        )
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn list_pages(&self, timeout_ms: Option<f64>) -> PyResult<Vec<PyPage>> {
        list_pages_for_context(Arc::clone(&self.inner), None, timeout_ms)
    }

    #[pyo3(signature = (context_id, timeout_ms=None))]
    fn list_pages_for_context_id(
        &self,
        context_id: String,
        timeout_ms: Option<f64>,
    ) -> PyResult<Vec<PyPage>> {
        list_pages_for_context(Arc::clone(&self.inner), Some(context_id), timeout_ms)
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn list_background_pages(&self, timeout_ms: Option<f64>) -> PyResult<Vec<PyPage>> {
        list_background_pages_for_context(Arc::clone(&self.inner), None, timeout_ms)
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn background_page_event_waiter(
        &self,
        timeout_ms: Option<f64>,
    ) -> PyResult<PyBackgroundPageEventWaiter> {
        background_page_event_waiter_for_context(Arc::clone(&self.inner), None, timeout_ms)
    }

    #[pyo3(signature = (context_id, timeout_ms=None))]
    fn list_background_pages_for_context_id(
        &self,
        context_id: String,
        timeout_ms: Option<f64>,
    ) -> PyResult<Vec<PyPage>> {
        list_background_pages_for_context(Arc::clone(&self.inner), Some(context_id), timeout_ms)
    }

    #[pyo3(signature = (context_id, timeout_ms=None))]
    fn background_page_event_waiter_for_context_id(
        &self,
        context_id: String,
        timeout_ms: Option<f64>,
    ) -> PyResult<PyBackgroundPageEventWaiter> {
        background_page_event_waiter_for_context(
            Arc::clone(&self.inner),
            Some(context_id),
            timeout_ms,
        )
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyBrowserContext {
    #[getter]
    fn context_id(&self) -> Option<String> {
        self.inner.context_id.clone()
    }

    fn new_page(&self) -> PyResult<PyPage> {
        create_page(
            Arc::clone(&self.inner.browser),
            self.inner.context_id.clone(),
        )
        .map_err(py_err)
    }

    fn new_page_async(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let browser = Arc::clone(&self.inner.browser);
        let runtime = browser.runtime.handle().clone();
        let context_id = self.inner.context_id.clone();
        let creation = runtime.spawn(create_page_async(browser, context_id));
        let creation_abort = creation.abort_handle();
        python_future_on(
            py,
            runtime,
            async move {
                let _creation_guard = SpawnedTaskAbortGuard(creation_abort);
                creation
                    .await
                    .map_err(|error| RwError::Message(error.to_string()))?
            },
            |py, inner| Ok(Py::new(py, PyPage { inner })?.into_any()),
        )
    }

    fn close(&self) -> PyResult<()> {
        let context = Arc::clone(&self.inner);
        let browser = Arc::clone(&context.browser);
        browser
            .block_on(close_context_async(context))
            .map_err(py_err)
    }

    fn close_async(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let context = Arc::clone(&self.inner);
        let runtime = context.browser.runtime.handle().clone();
        let cleanup = runtime.spawn(close_context_async(context));
        python_future_on(
            py,
            runtime,
            async move {
                cleanup
                    .await
                    .map_err(|error| RwError::Message(error.to_string()))?
            },
            |py, ()| Ok(py.None()),
        )
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn cookies(&self, timeout_ms: Option<f64>) -> PyResult<String> {
        let browser = Arc::clone(&self.inner.browser);
        let client = Arc::clone(&browser.client);
        let context_id = self.inner.context_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let result = browser
            .block_on(async move {
                let mut params = json!({});
                if let Some(context_id) = context_id {
                    params["browserContextId"] = Value::String(context_id);
                }
                client
                    .send("Storage.getCookies", params, None, timeout)
                    .await
            })
            .map_err(py_err)?;
        Ok(result
            .get("cookies")
            .cloned()
            .unwrap_or_else(|| json!([]))
            .to_string())
    }

    #[pyo3(signature = (cookies_json, timeout_ms=None))]
    fn add_cookies(&self, cookies_json: &str, timeout_ms: Option<f64>) -> PyResult<()> {
        let cookies: Value = serde_json::from_str(cookies_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let browser = Arc::clone(&self.inner.browser);
        let client = Arc::clone(&browser.client);
        let context_id = self.inner.context_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        browser
            .block_on(async move {
                let mut params = json!({ "cookies": cookies });
                if let Some(context_id) = context_id {
                    params["browserContextId"] = Value::String(context_id);
                }
                client
                    .send("Storage.setCookies", params, None, timeout)
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn clear_cookies(&self, timeout_ms: Option<f64>) -> PyResult<()> {
        let browser = Arc::clone(&self.inner.browser);
        let client = Arc::clone(&browser.client);
        let context_id = self.inner.context_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        browser
            .block_on(async move {
                let mut params = json!({});
                if let Some(context_id) = context_id {
                    params["browserContextId"] = Value::String(context_id);
                }
                client
                    .send("Storage.clearCookies", params, None, timeout)
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (permissions_json, origin=None, timeout_ms=None))]
    fn grant_permissions(
        &self,
        permissions_json: &str,
        origin: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let permissions: Value = serde_json::from_str(permissions_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let mapped_permissions = map_chromium_permissions(&permissions, false).map_err(py_err)?;
        let fallback_permissions = map_chromium_permissions(&permissions, true).map_err(py_err)?;
        let needs_fallback = fallback_permissions != mapped_permissions;
        let browser = Arc::clone(&self.inner.browser);
        let client = Arc::clone(&browser.client);
        let context_id = self.inner.context_id.clone();
        let origin = origin
            .filter(|value| *value != "*")
            .map(ToString::to_string);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        browser
            .block_on(async move {
                let mut params = json!({ "permissions": mapped_permissions });
                if let Some(context_id) = context_id.as_ref() {
                    params["browserContextId"] = Value::String(context_id.clone());
                }
                if let Some(origin) = origin.as_ref() {
                    params["origin"] = Value::String(origin.clone());
                }
                let grant_result = client
                    .send("Browser.grantPermissions", params, None, timeout)
                    .await;
                if grant_result.is_err() && needs_fallback {
                    let mut fallback_params = json!({ "permissions": fallback_permissions });
                    if let Some(context_id) = context_id.as_ref() {
                        fallback_params["browserContextId"] = Value::String(context_id.clone());
                    }
                    if let Some(origin) = origin.as_ref() {
                        fallback_params["origin"] = Value::String(origin.clone());
                    }
                    client
                        .send("Browser.grantPermissions", fallback_params, None, timeout)
                        .await?;
                } else {
                    grant_result?;
                }
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn clear_permissions(&self, timeout_ms: Option<f64>) -> PyResult<()> {
        let browser = Arc::clone(&self.inner.browser);
        let client = Arc::clone(&browser.client);
        let context_id = self.inner.context_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        browser
            .block_on(async move {
                let mut params = json!({});
                if let Some(context_id) = context_id {
                    params["browserContextId"] = Value::String(context_id);
                }
                client
                    .send("Browser.resetPermissions", params, None, timeout)
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn list_service_workers(&self, timeout_ms: Option<f64>) -> PyResult<Vec<PyWorker>> {
        list_service_workers_for_context(
            Arc::clone(&self.inner.browser),
            self.inner.context_id.clone(),
            timeout_ms,
        )
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn service_worker_event_waiter(
        &self,
        timeout_ms: Option<f64>,
    ) -> PyResult<PyServiceWorkerEventWaiter> {
        service_worker_event_waiter_for_context(
            Arc::clone(&self.inner.browser),
            self.inner.context_id.clone(),
            timeout_ms,
        )
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn list_background_pages(&self, timeout_ms: Option<f64>) -> PyResult<Vec<PyPage>> {
        list_background_pages_for_context(
            Arc::clone(&self.inner.browser),
            self.inner.context_id.clone(),
            timeout_ms,
        )
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn background_page_event_waiter(
        &self,
        timeout_ms: Option<f64>,
    ) -> PyResult<PyBackgroundPageEventWaiter> {
        background_page_event_waiter_for_context(
            Arc::clone(&self.inner.browser),
            self.inner.context_id.clone(),
            timeout_ms,
        )
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyCdpSession {
    #[pyo3(signature = (method, params_json=None, timeout_ms=None))]
    fn send(
        &self,
        py: Python<'_>,
        method: &str,
        params_json: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        if self.detached.load(Ordering::SeqCst) {
            return Err(PyRuntimeError::new_err("CDP session is detached"));
        }
        let params: Value = match params_json {
            Some(value) => serde_json::from_str(value)
                .map_err(|error| PyValueError::new_err(error.to_string()))?,
            None => json!({}),
        };
        let browser = Arc::clone(&self.browser);
        let client = Arc::clone(&browser.client);
        let session_id = self.session_id.clone();
        let method = method.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let result = py
            .detach(move || {
                browser.block_on(async move {
                    client
                        .send(&method, params, session_id.as_deref(), timeout)
                        .await
                })
            })
            .map_err(py_err)?;
        Ok(result.to_string())
    }

    fn event_waiter(&self, method: &str) -> PyResult<PyCdpEventWaiter> {
        if self.detached.load(Ordering::SeqCst) {
            return Err(PyRuntimeError::new_err("CDP session is detached"));
        }
        Ok(PyCdpEventWaiter {
            browser: Arc::clone(&self.browser),
            receiver: Mutex::new(Some(self.browser.client.subscribe())),
            session_id: self.session_id.clone(),
            method: method.to_string(),
        })
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn detach(&self, timeout_ms: Option<f64>) -> PyResult<()> {
        if self.detached.swap(true, Ordering::SeqCst) {
            return Ok(());
        }
        let Some(session_id) = self.session_id.clone() else {
            return Ok(());
        };
        let browser = Arc::clone(&self.browser);
        let client = Arc::clone(&browser.client);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        browser
            .block_on(async move {
                client
                    .send(
                        "Target.detachFromTarget",
                        json!({ "sessionId": session_id }),
                        None,
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyCdpEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<String> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("CDP event waiter is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let session_id = self.session_id.clone();
        let method = self.method.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let result = browser.block_on_raw(wait_for_cdp_event(
                &mut receiver,
                session_id.as_deref(),
                &method,
                timeout,
            ));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

fn evaluate_expression_for_page(
    page: Arc<PageInner>,
    expression: String,
    timeout_ms: Option<f64>,
) -> RwResult<String> {
    let timeout = BrowserInner::command_timeout(timeout_ms);
    let browser = Arc::clone(&page.browser);
    browser.block_on(evaluate_expression_for_page_async(
        page, expression, timeout,
    ))
}

fn evaluate_expression_for_page_raw(
    page: Arc<PageInner>,
    expression: String,
    timeout_ms: Option<f64>,
) -> RwResult<String> {
    evaluate_expression_for_page_raw_cancelable(page, expression, timeout_ms, None)
}

fn evaluate_expression_for_page_raw_cancelable(
    page: Arc<PageInner>,
    expression: String,
    timeout_ms: Option<f64>,
    cancel: Option<&CancelToken>,
) -> RwResult<String> {
    let timeout = BrowserInner::command_timeout(timeout_ms);
    let browser = Arc::clone(&page.browser);
    browser.block_on_raw(cancelable(
        cancel.cloned(),
        evaluate_expression_for_page_async(page, expression, timeout),
    ))
}

fn evaluate_locator_wait_probe_for_page(
    page: Arc<PageInner>,
    expression: String,
    timeout_ms: Option<f64>,
) -> RwResult<String> {
    let timeout = BrowserInner::command_timeout(timeout_ms);
    let browser = Arc::clone(&page.browser);
    browser.block_on(async move {
        let attempt_page = Arc::clone(&page);
        run_locator_wait_retry(page, timeout, move |deadline| {
            let page = Arc::clone(&attempt_page);
            let expression = expression.clone();
            async move {
                evaluate_expression_in_session_before(
                    &page.browser.client,
                    &page.session_id,
                    expression,
                    deadline,
                )
                .await
            }
        })
        .await
    })
}

async fn evaluate_expression_for_page_async(
    page: Arc<PageInner>,
    expression: String,
    timeout: Duration,
) -> RwResult<String> {
    evaluate_expression_in_session(&page.browser.client, &page.session_id, expression, timeout)
        .await
}

#[derive(Clone, Copy)]
struct OperationDeadline {
    at: tokio::time::Instant,
    timeout: Duration,
}

impl OperationDeadline {
    fn new(timeout: Duration) -> Self {
        Self {
            at: tokio::time::Instant::now() + timeout,
            timeout,
        }
    }

    fn remaining(self) -> RwResult<Duration> {
        let remaining = self
            .at
            .saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return Err(RwError::Timeout(
                self.timeout.as_millis().min(u128::from(u64::MAX)) as u64,
            ));
        }
        Ok(remaining)
    }

    fn remaining_capped(self, cap: Duration) -> RwResult<Duration> {
        self.remaining().map(|remaining| remaining.min(cap))
    }
}

#[allow(clippy::too_many_arguments)]
async fn dispatch_mouse_click_sequence_in_session(
    client: &CdpClient,
    session_id: &str,
    start_x: f64,
    start_y: f64,
    target_x: f64,
    target_y: f64,
    step_count: u32,
    button: &str,
    button_mask: i64,
    click_count: i64,
    delay_ms: f64,
    initial_buttons: i64,
    modifiers: i64,
    deadline: OperationDeadline,
) -> RwResult<()> {
    let steps = step_count.max(1);
    for index in 1..=steps {
        let fraction = index as f64 / steps as f64;
        let x = start_x + (target_x - start_x) * fraction;
        let y = start_y + (target_y - start_y) * fraction;
        client
            .send(
                "Input.dispatchMouseEvent",
                mouse_event_payload("mouseMoved", x, y, "none", initial_buttons, 0, modifiers),
                Some(session_id),
                deadline.remaining()?,
            )
            .await?;
    }

    for count in 1..=click_count.max(0) {
        client
            .send(
                "Input.dispatchMouseEvent",
                mouse_event_payload(
                    "mousePressed",
                    target_x,
                    target_y,
                    button,
                    initial_buttons | button_mask,
                    count,
                    modifiers,
                ),
                Some(session_id),
                deadline.remaining()?,
            )
            .await?;
        if delay_ms > 0.0 {
            tokio::time::timeout(
                deadline.remaining()?,
                tokio::time::sleep(Duration::from_secs_f64(delay_ms / 1000.0)),
            )
            .await
            .map_err(|_| {
                RwError::Timeout(deadline.timeout.as_millis().min(u128::from(u64::MAX)) as u64)
            })?;
        }
        client
            .send(
                "Input.dispatchMouseEvent",
                mouse_event_payload(
                    "mouseReleased",
                    target_x,
                    target_y,
                    button,
                    initial_buttons & !button_mask,
                    count,
                    modifiers,
                ),
                Some(session_id),
                deadline.remaining()?,
            )
            .await?;
        if count < click_count && delay_ms > 0.0 {
            tokio::time::timeout(
                deadline.remaining()?,
                tokio::time::sleep(Duration::from_secs_f64(delay_ms / 1000.0)),
            )
            .await
            .map_err(|_| {
                RwError::Timeout(deadline.timeout.as_millis().min(u128::from(u64::MAX)) as u64)
            })?;
        }
    }
    Ok(())
}

#[derive(Debug)]
struct ResolvedLocatorPoint {
    session_id: String,
    frame_id: Option<String>,
    locator_json: String,
    index: usize,
    x: f64,
    y: f64,
}

async fn resolve_locator_point(
    page: Arc<PageInner>,
    locator_json: &str,
    index: usize,
    position_x: Option<f64>,
    position_y: Option<f64>,
    deadline: OperationDeadline,
) -> RwResult<ResolvedLocatorPoint> {
    let resolution = resolve_locator_session(Arc::clone(&page), locator_json, deadline).await?;
    let expression = locator_script(
        &resolution.locator_json,
        index,
        "if (!el) throw new Error('No element matches locator'); return el;",
    );
    let remote = if let Some(frame_id) = resolution.frame_id.as_deref() {
        evaluate_handle_expression_in_frame_context(
            &page.browser.client,
            &resolution.session_id,
            frame_id,
            expression,
            deadline,
        )
        .await?
    } else {
        evaluate_handle_expression_in_session(
            &page.browser.client,
            &resolution.session_id,
            expression,
            deadline,
        )
        .await?
    };
    let object_id = remote
        .get("objectId")
        .and_then(Value::as_str)
        .ok_or_else(|| RwError::Message("locator did not resolve to an element".to_string()))?
        .to_string();
    let box_model = page
        .browser
        .client
        .send(
            "DOM.getBoxModel",
            json!({ "objectId": object_id }),
            Some(&resolution.session_id),
            deadline.remaining()?,
        )
        .await;
    if let Ok(remaining) = deadline.remaining_capped(Duration::from_secs(1)) {
        let _ = page
            .browser
            .client
            .send(
                "Runtime.releaseObject",
                json!({ "objectId": object_id }),
                Some(&resolution.session_id),
                remaining,
            )
            .await;
    }
    let box_model = box_model?;
    let quad = box_model
        .pointer("/model/border")
        .and_then(Value::as_array)
        .filter(|quad| quad.len() >= 8)
        .ok_or_else(|| RwError::Message("locator element has no layout box".to_string()))?;
    let xs = [0, 2, 4, 6]
        .into_iter()
        .map(|index| quad[index].as_f64())
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| RwError::Message("locator element has an invalid layout box".to_string()))?;
    let ys = [1, 3, 5, 7]
        .into_iter()
        .map(|index| quad[index].as_f64())
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| RwError::Message("locator element has an invalid layout box".to_string()))?;
    let min_x = xs.iter().copied().fold(f64::INFINITY, f64::min);
    let max_x = xs.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let min_y = ys.iter().copied().fold(f64::INFINITY, f64::min);
    let max_y = ys.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let x = position_x.map_or((min_x + max_x) / 2.0, |offset| min_x + offset);
    let y = position_y.map_or((min_y + max_y) / 2.0, |offset| min_y + offset);
    Ok(ResolvedLocatorPoint {
        session_id: resolution.session_id,
        frame_id: resolution.frame_id,
        locator_json: resolution.locator_json,
        index,
        x,
        y,
    })
}

async fn evaluate_resolved_locator_body(
    page: &Arc<PageInner>,
    resolved: &ResolvedLocatorPoint,
    body: &str,
    deadline: OperationDeadline,
) -> RwResult<Value> {
    let expression = locator_script(&resolved.locator_json, resolved.index, body);
    let json = if let Some(frame_id) = resolved.frame_id.as_deref() {
        evaluate_expression_in_frame_context(
            &page.browser.client,
            &resolved.session_id,
            frame_id,
            expression,
            deadline,
        )
        .await?
    } else {
        evaluate_expression_in_session_before(
            &page.browser.client,
            &resolved.session_id,
            expression,
            deadline,
        )
        .await?
    };
    Ok(serde_json::from_str(&json)?)
}

async fn wait_for_drag_intercepted(
    events: &mut broadcast::Receiver<Value>,
    session_id: &str,
    deadline: OperationDeadline,
) -> RwResult<Value> {
    loop {
        match tokio::time::timeout(deadline.remaining()?, events.recv()).await {
            Ok(Ok(event)) => {
                if event.get("method").and_then(Value::as_str) == Some("Input.dragIntercepted") {
                    return event.pointer("/params/data").cloned().ok_or_else(|| {
                        RwError::Message("drag interception did not include data".to_string())
                    });
                }
                if event.get("method").and_then(Value::as_str) == Some("Target.detachedFromTarget")
                    && event.pointer("/params/sessionId").and_then(Value::as_str)
                        == Some(session_id)
                {
                    return Err(RwError::Message(
                        "CDP target detached while starting drag".to_string(),
                    ));
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(broadcast::error::RecvError::Closed)) => {
                return Err(RwError::Message("CDP event stream closed".to_string()));
            }
            Err(_) => return Err(RwError::Timeout(deadline.timeout.as_millis() as u64)),
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn run_drag_with_cleanup<Start, Complete, Completion>(
    client: &CdpClient,
    interception_session_id: &str,
    pointer_session_id: &str,
    release_x: f64,
    release_y: f64,
    modifiers: i64,
    start: Start,
    complete: Complete,
) -> RwResult<()>
where
    Start: Future<Output = RwResult<Option<Value>>>,
    Complete: FnOnce(Option<Value>) -> Completion,
    Completion: Future<Output = RwResult<()>>,
{
    const CLEANUP_TIMEOUT: Duration = Duration::from_secs(1);

    let start_result = start.await;
    let _ = client
        .send(
            "Input.setInterceptDrags",
            json!({ "enabled": false }),
            Some(interception_session_id),
            CLEANUP_TIMEOUT,
        )
        .await;
    let result = match start_result {
        Ok(drag_data) => complete(drag_data).await,
        Err(error) => Err(error),
    };
    if result.is_err() {
        let _ = client
            .send(
                "Input.dispatchMouseEvent",
                mouse_event_payload(
                    "mouseReleased",
                    release_x,
                    release_y,
                    "left",
                    0,
                    1,
                    modifiers,
                ),
                Some(pointer_session_id),
                CLEANUP_TIMEOUT,
            )
            .await;
    }
    result
}

async fn dispatch_mouse_move_sequence_in_session(
    client: &CdpClient,
    session_id: &str,
    start_x: f64,
    start_y: f64,
    target_x: f64,
    target_y: f64,
    step_count: u32,
    buttons: i64,
    modifiers: i64,
    deadline: OperationDeadline,
) -> RwResult<()> {
    let steps = step_count.max(1);
    for index in 1..=steps {
        let fraction = index as f64 / steps as f64;
        let x = start_x + (target_x - start_x) * fraction;
        let y = start_y + (target_y - start_y) * fraction;
        let move_button = if buttons == 0 { "none" } else { "left" };
        let mut payload =
            mouse_event_payload("mouseMoved", x, y, move_button, buttons, 0, modifiers);
        if buttons != 0 {
            payload["force"] = json!(0.5);
        }
        client
            .send(
                "Input.dispatchMouseEvent",
                payload,
                Some(session_id),
                deadline.remaining()?,
            )
            .await?;
    }
    Ok(())
}

async fn pointer_action_ordering_barrier(
    client: &CdpClient,
    session_id: &str,
    mut events: broadcast::Receiver<Value>,
    deadline: OperationDeadline,
) -> RwResult<()> {
    let barrier = client.send(
        "Runtime.evaluate",
        json!({
            "expression": "void 0",
            "awaitPromise": true,
            "returnByValue": true,
            "userGesture": true,
        }),
        Some(session_id),
        deadline.remaining()?,
    );
    tokio::pin!(barrier);
    loop {
        tokio::select! {
            biased;
            event = events.recv() => match event {
                Ok(event) => {
                    if event.get("method").and_then(Value::as_str) == Some("Target.detachedFromTarget")
                        && event.pointer("/params/sessionId").and_then(Value::as_str) == Some(session_id)
                    {
                        return Ok(());
                    }
                }
                Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(broadcast::error::RecvError::Closed) => {
                    return Err(RwError::Message("CDP event stream closed".to_string()));
                }
            },
            result = &mut barrier => return result.map(|_| ()),
        }
    }
}

fn evaluate_expression_for_frame(
    page: Arc<PageInner>,
    frame_id: String,
    expression: String,
    timeout_ms: Option<f64>,
) -> RwResult<String> {
    let timeout = BrowserInner::command_timeout(timeout_ms);
    let browser = Arc::clone(&page.browser);
    let client = Arc::clone(&browser.client);
    let session_id = page.session_for_frame_id(&frame_id);
    browser.block_on(async move {
        let world = client
            .send(
                "Page.createIsolatedWorld",
                json!({
                    "frameId": frame_id,
                    "worldName": FRAME_UTILITY_WORLD_NAME,
                }),
                Some(&session_id),
                timeout,
            )
            .await?;
        let context_id = world
            .get("executionContextId")
            .ok_or_else(|| {
                RwError::Message("CDP did not return an executionContextId".to_string())
            })?
            .clone();
        let result = client
            .send(
                "Runtime.evaluate",
                json!({
                    "expression": expression,
                    "contextId": context_id,
                    "awaitPromise": true,
                    "returnByValue": false,
                    "userGesture": true,
                }),
                Some(&session_id),
                timeout,
            )
            .await?;
        runtime_result_to_json_with_serializer(&client, &session_id, &result, timeout).await
    })
}

async fn evaluate_expression_in_session(
    client: &CdpClient,
    session_id: &str,
    expression: String,
    timeout: Duration,
) -> RwResult<String> {
    evaluate_expression_in_session_before(
        client,
        session_id,
        expression,
        OperationDeadline::new(timeout),
    )
    .await
}

async fn evaluate_expression_in_session_before(
    client: &CdpClient,
    session_id: &str,
    expression: String,
    deadline: OperationDeadline,
) -> RwResult<String> {
    let result = client
        .send(
            "Runtime.evaluate",
            json!({
                "expression": expression,
                "awaitPromise": true,
                "returnByValue": false,
                "userGesture": true,
            }),
            Some(session_id),
            deadline.remaining()?,
        )
        .await?;
    runtime_result_to_json_with_serializer(client, session_id, &result, deadline.remaining()?).await
}

async fn evaluate_handle_expression_in_session(
    client: &CdpClient,
    session_id: &str,
    expression: String,
    deadline: OperationDeadline,
) -> RwResult<Value> {
    let result = client
        .send(
            "Runtime.evaluate",
            json!({
                "expression": expression,
                "awaitPromise": true,
                "returnByValue": false,
                "userGesture": true,
            }),
            Some(session_id),
            deadline.remaining()?,
        )
        .await?;
    let remote_json = runtime_result_to_remote_object(&result)?;
    Ok(serde_json::from_str::<Value>(&remote_json)?)
}

async fn evaluate_expression_in_frame_context(
    client: &CdpClient,
    session_id: &str,
    frame_id: &str,
    expression: String,
    deadline: OperationDeadline,
) -> RwResult<String> {
    let context_id =
        create_isolated_world_for_frame(client, session_id, frame_id, deadline.remaining()?)
            .await?;
    evaluate_expression_in_context_before(client, session_id, context_id, expression, deadline)
        .await
}

async fn evaluate_expression_in_context_before(
    client: &CdpClient,
    session_id: &str,
    context_id: Value,
    expression: String,
    deadline: OperationDeadline,
) -> RwResult<String> {
    let result = client
        .send(
            "Runtime.evaluate",
            json!({
                "expression": expression,
                "contextId": context_id,
                "awaitPromise": true,
                "returnByValue": false,
                "userGesture": true,
            }),
            Some(session_id),
            deadline.remaining()?,
        )
        .await?;
    runtime_result_to_json_with_serializer(client, session_id, &result, deadline.remaining()?).await
}

async fn evaluate_handle_expression_in_frame_context(
    client: &CdpClient,
    session_id: &str,
    frame_id: &str,
    expression: String,
    deadline: OperationDeadline,
) -> RwResult<Value> {
    let context_id =
        create_isolated_world_for_frame(client, session_id, frame_id, deadline.remaining()?)
            .await?;
    let result = client
        .send(
            "Runtime.evaluate",
            json!({
                "expression": expression,
                "contextId": context_id,
                "awaitPromise": true,
                "returnByValue": false,
                "userGesture": true,
            }),
            Some(session_id),
            deadline.remaining()?,
        )
        .await?;
    let remote_json = runtime_result_to_remote_object(&result)?;
    Ok(serde_json::from_str::<Value>(&remote_json)?)
}

async fn create_isolated_world_for_frame(
    client: &CdpClient,
    session_id: &str,
    frame_id: &str,
    timeout: Duration,
) -> RwResult<Value> {
    let world = client
        .send(
            "Page.createIsolatedWorld",
            json!({
                "frameId": frame_id,
                "worldName": FRAME_UTILITY_WORLD_NAME,
            }),
            Some(session_id),
            timeout,
        )
        .await?;
    world
        .get("executionContextId")
        .cloned()
        .ok_or_else(|| RwError::Message("CDP did not return an executionContextId".to_string()))
}

struct LocatorSessionResolution {
    session_id: String,
    frame_id: Option<String>,
    locator_json: String,
}

struct FrameOwnerResolution {
    frame_id: Option<String>,
    same_origin_accessible: bool,
}

async fn evaluate_locator_resolution(
    page: &PageInner,
    resolution: &LocatorSessionResolution,
    expression: String,
    setup_deadline: OperationDeadline,
    transport_slack: Duration,
) -> RwResult<String> {
    let context_id = if let Some(frame_id) = &resolution.frame_id {
        Some(
            create_isolated_world_for_frame(
                &page.browser.client,
                &resolution.session_id,
                frame_id,
                setup_deadline.remaining()?,
            )
            .await?,
        )
    } else {
        None
    };
    let transport_deadline = if transport_slack.is_zero() {
        setup_deadline
    } else {
        OperationDeadline::new(setup_deadline.remaining()?.saturating_add(transport_slack))
    };
    if let Some(context_id) = context_id {
        evaluate_expression_in_context_before(
            &page.browser.client,
            &resolution.session_id,
            context_id,
            expression,
            transport_deadline,
        )
        .await
    } else {
        evaluate_expression_in_session_before(
            &page.browser.client,
            &resolution.session_id,
            expression,
            transport_deadline,
        )
        .await
    }
}

async fn evaluate_locator_for_page(
    page: Arc<PageInner>,
    locator_json: String,
    index: usize,
    body: String,
    timeout: Duration,
) -> RwResult<String> {
    let deadline = OperationDeadline::new(timeout);
    let resolution = resolve_locator_session(Arc::clone(&page), &locator_json, deadline).await?;
    let expression = locator_script(&resolution.locator_json, index, &body);
    evaluate_locator_resolution(&page, &resolution, expression, deadline, Duration::ZERO).await
}

async fn evaluate_locator_handle_for_page(
    page: Arc<PageInner>,
    locator_json: String,
    index: usize,
    body: String,
    timeout: Duration,
) -> RwResult<String> {
    let deadline = OperationDeadline::new(timeout);
    let resolution = resolve_locator_session(Arc::clone(&page), &locator_json, deadline).await?;
    let expression = locator_script(&resolution.locator_json, index, &body);
    let mut remote = if let Some(frame_id) = resolution.frame_id {
        evaluate_handle_expression_in_frame_context(
            &page.browser.client,
            &resolution.session_id,
            &frame_id,
            expression,
            deadline,
        )
        .await?
    } else {
        evaluate_handle_expression_in_session(
            &page.browser.client,
            &resolution.session_id,
            expression,
            deadline,
        )
        .await?
    };
    if let Some(object) = remote.as_object_mut() {
        object.insert(
            "__rustwright_session_id".to_string(),
            Value::String(resolution.session_id),
        );
    }
    Ok(remote.to_string())
}

async fn resolve_locator_session(
    page: Arc<PageInner>,
    locator_json: &str,
    deadline: OperationDeadline,
) -> RwResult<LocatorSessionResolution> {
    let original_spec: Value = serde_json::from_str(locator_json)?;
    let _ = refresh_page_frame_tree(
        &page,
        deadline.remaining_capped(Duration::from_millis(250))?,
    )
    .await;
    let mut session_id = page.session_id.clone();
    let mut frame_id = None;
    let mut spec = original_spec.clone();
    let mut consumed_any_oopif = false;

    loop {
        let (candidate_spec, nth_wrapper) = if let Some(base) = nth_base_with_leading_frame(&spec) {
            (base, Some(spec.clone()))
        } else {
            (spec.clone(), None)
        };
        let Some((next_session_id, remaining_spec)) = resolve_next_oopif_frame(
            Arc::clone(&page),
            &session_id,
            frame_id.as_deref(),
            &candidate_spec,
            deadline,
        )
        .await?
        else {
            break;
        };
        session_id = next_session_id;
        frame_id = remaining_spec.0;
        spec = if let Some(mut wrapper) = nth_wrapper {
            if let Some(object) = wrapper.as_object_mut() {
                object.insert("base".to_string(), remaining_spec.1);
            }
            wrapper
        } else {
            remaining_spec.1
        };
        consumed_any_oopif = true;
    }

    Ok(LocatorSessionResolution {
        session_id,
        frame_id,
        locator_json: if consumed_any_oopif {
            spec.to_string()
        } else {
            locator_json.to_string()
        },
    })
}

fn nth_base_with_leading_frame(spec: &Value) -> Option<Value> {
    if spec.get("kind").and_then(Value::as_str) != Some("nth") {
        return None;
    }
    let base = spec.get("base")?;
    if leading_frame_chain(base).is_empty() {
        return None;
    }
    Some(base.clone())
}

async fn resolve_next_oopif_frame(
    page: Arc<PageInner>,
    current_session_id: &str,
    current_frame_id: Option<&str>,
    spec: &Value,
    deadline: OperationDeadline,
) -> RwResult<Option<(String, (Option<String>, Value))>> {
    let frame_chain = leading_frame_chain(spec);
    if frame_chain.is_empty() {
        return Ok(None);
    }

    for index in 0..frame_chain.len() {
        let owner_spec = frame_owner_spec_for_chain(&frame_chain[..=index]);
        let frame_index = frame_chain[index]
            .get("frame_index")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let frame_strict = frame_chain[index]
            .get("frame_strict")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let selector_label = frame_chain[index]
            .get("frame_selector")
            .and_then(Value::as_str)
            .unwrap_or("iframe,frame");
        let Some(owner) = describe_frame_owner(
            &page.browser.client,
            current_session_id,
            current_frame_id,
            &owner_spec,
            frame_index,
            frame_strict,
            selector_label,
            deadline,
        )
        .await?
        else {
            return Ok(None);
        };
        let Some(frame_id) = owner.frame_id else {
            if owner.same_origin_accessible {
                continue;
            }
            return Err(RwError::Message(
                "cross-origin iframe did not expose a CDP frame id".to_string(),
            ));
        };
        let remaining = frame_chain[index]
            .get("inner")
            .cloned()
            .unwrap_or_else(|| json!({ "kind": "css", "selector": "*" }));
        if let Some(mapped_session) =
            frame_session_for_frame(&page, &frame_id, Some(current_session_id))?
        {
            return Ok(Some((mapped_session, (None, remaining))));
        }
        if owner.same_origin_accessible {
            continue;
        }

        let target_info = iframe_target_info_for_frame(&page, &frame_id, deadline).await?;
        if let Some(target_info) = target_info {
            if !target_info
                .get("attached")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                if let Some(attached_session_id) = attach_iframe_target_for_frame(
                    Arc::clone(&page),
                    &frame_id,
                    &target_info,
                    deadline,
                )
                .await?
                {
                    return Ok(Some((attached_session_id, (None, remaining))));
                }
            }
            if let Some(attached_session_id) =
                wait_for_frame_session(&page, &frame_id, Some(current_session_id), deadline).await?
            {
                return Ok(Some((attached_session_id, (None, remaining))));
            }
            return Err(RwError::Message(
                "iframe target attachment ended without a frame session".to_string(),
            ));
        }

        if session_owns_frame(
            &page.browser.client,
            current_session_id,
            &frame_id,
            deadline,
        )
        .await?
        {
            return Ok(Some((
                current_session_id.to_string(),
                (Some(frame_id), remaining),
            )));
        }
        if let Some(attached_session_id) =
            wait_for_frame_session(&page, &frame_id, Some(current_session_id), deadline).await?
        {
            return Ok(Some((attached_session_id, (None, remaining))));
        }
        return Err(RwError::Message(
            "cross-origin iframe did not acquire an execution session".to_string(),
        ));
    }

    Ok(None)
}

async fn attach_iframe_target_for_frame(
    page: Arc<PageInner>,
    frame_id: &str,
    target_info: &Value,
    deadline: OperationDeadline,
) -> RwResult<Option<String>> {
    let Some(target_id) = target_info
        .get("targetId")
        .and_then(Value::as_str)
        .filter(|target_id| *target_id == frame_id)
    else {
        return Ok(None);
    };
    let parent_frame_id = target_info
        .get("parentFrameId")
        .and_then(Value::as_str)
        .map(ToString::to_string);
    let attached = page
        .browser
        .client
        .send(
            "Target.attachToTarget",
            json!({ "targetId": target_id, "flatten": true }),
            None,
            deadline.remaining()?,
        )
        .await?;
    let Some(session_id) = attached.get("sessionId").and_then(Value::as_str) else {
        return Ok(None);
    };
    let session_id = session_id.to_string();
    register_attached_iframe_session(
        Arc::clone(&page),
        frame_id.to_string(),
        parent_frame_id,
        session_id.clone(),
        deadline.remaining()?,
    )
    .await?;
    Ok(Some(session_id))
}

async fn iframe_target_info_for_frame(
    page: &Arc<PageInner>,
    frame_id: &str,
    deadline: OperationDeadline,
) -> RwResult<Option<Value>> {
    let targets = page
        .browser
        .client
        .send("Target.getTargets", json!({}), None, deadline.remaining()?)
        .await?;
    Ok(targets
        .get("targetInfos")
        .and_then(Value::as_array)
        .and_then(|targets| {
            targets.iter().find(|info| {
                info.get("type").and_then(Value::as_str) == Some("iframe")
                    && info.get("targetId").and_then(Value::as_str) == Some(frame_id)
            })
        })
        .cloned())
}

fn leading_frame_chain(spec: &Value) -> Vec<Value> {
    let mut chain = Vec::new();
    let mut current = spec;
    loop {
        if current.get("kind").and_then(Value::as_str) != Some("frame") {
            break;
        }
        chain.push(current.clone());
        let Some(inner) = current.get("inner") else {
            break;
        };
        current = inner;
    }
    chain
}

fn frame_owner_spec_for_chain(chain: &[Value]) -> Value {
    if chain.is_empty() {
        return json!({ "kind": "css", "selector": "iframe,frame" });
    }
    if chain.len() == 1 {
        return frame_owner_selector_spec(&chain[0]);
    }
    let mut outer = chain[0].clone();
    if let Some(object) = outer.as_object_mut() {
        object.insert("inner".to_string(), frame_owner_spec_for_chain(&chain[1..]));
    }
    outer
}

fn frame_owner_selector_spec(frame_spec: &Value) -> Value {
    if let Some(selector_spec) = frame_spec.get("frame_selector_spec") {
        return selector_spec.clone();
    }
    let selector = frame_spec
        .get("frame_selector")
        .and_then(Value::as_str)
        .unwrap_or("iframe,frame");
    json!({ "kind": "css", "selector": selector })
}

async fn describe_frame_owner(
    client: &CdpClient,
    session_id: &str,
    current_frame_id: Option<&str>,
    owner_spec: &Value,
    frame_index: i64,
    _frame_strict: bool,
    _selector_label: &str,
    deadline: OperationDeadline,
) -> RwResult<Option<FrameOwnerResolution>> {
    let body = format!(
        r#"
const isFrameElement = el => el && (el.tagName === 'IFRAME' || el.tagName === 'FRAME');
const candidates = matches.filter(isFrameElement);
let frameIndex = {frame_index};
if (frameIndex < 0) frameIndex = candidates.length + frameIndex;
const frame = candidates[frameIndex] || null;
return frame;
"#,
        frame_index = frame_index,
    );
    let owner_json = owner_spec.to_string();
    let expression = locator_script(&owner_json, 0, &body);
    let remote = if let Some(current_frame_id) = current_frame_id {
        evaluate_handle_expression_in_frame_context(
            client,
            session_id,
            current_frame_id,
            expression,
            deadline,
        )
        .await?
    } else {
        evaluate_handle_expression_in_session(client, session_id, expression, deadline).await?
    };
    let Some(object_id) = remote.get("objectId").and_then(Value::as_str) else {
        return Ok(None);
    };
    let same_origin_accessible = frame_owner_same_origin(client, session_id, object_id, deadline)
        .await
        .unwrap_or(false);
    let described = client
        .send(
            "DOM.describeNode",
            json!({ "objectId": object_id, "depth": 0 }),
            Some(session_id),
            deadline.remaining()?,
        )
        .await;
    if let Ok(remaining) = deadline.remaining_capped(Duration::from_secs(1)) {
        let _ = client
            .send(
                "Runtime.releaseObject",
                json!({ "objectId": object_id }),
                Some(session_id),
                remaining,
            )
            .await;
    }
    let described = described?;
    let frame_id = described
        .pointer("/node/frameId")
        .and_then(Value::as_str)
        .map(ToString::to_string);
    Ok(Some(FrameOwnerResolution {
        frame_id,
        same_origin_accessible,
    }))
}

async fn frame_owner_same_origin(
    client: &CdpClient,
    session_id: &str,
    object_id: &str,
    deadline: OperationDeadline,
) -> RwResult<bool> {
    let result = client
        .send(
            "Runtime.callFunctionOn",
            json!({
                "objectId": object_id,
                "functionDeclaration": "function() { try { return !!(this.contentDocument || (this.contentWindow && this.contentWindow.document)); } catch (_) { return false; } }",
                "awaitPromise": true,
                "returnByValue": true,
                "userGesture": true,
            }),
            Some(session_id),
            deadline.remaining()?,
        )
        .await?;
    Ok(
        serde_json::from_str::<Value>(&runtime_result_to_json(&result)?)?
            .as_bool()
            .unwrap_or(false),
    )
}

async fn session_owns_frame(
    client: &CdpClient,
    session_id: &str,
    frame_id: &str,
    deadline: OperationDeadline,
) -> RwResult<bool> {
    match create_isolated_world_for_frame(client, session_id, frame_id, deadline.remaining()?).await
    {
        Ok(_) => Ok(true),
        Err(RwError::Cdp { method, message })
            if method == "Page.createIsolatedWorld"
                && message.to_ascii_lowercase().contains("no frame") =>
        {
            Ok(false)
        }
        Err(error) => Err(error),
    }
}

fn frame_session_for_frame(
    page: &Arc<PageInner>,
    frame_id: &str,
    different_from_session_id: Option<&str>,
) -> RwResult<Option<String>> {
    let state = page.frame_state.lock().unwrap();
    if let Some(error) = state.frame_session_errors.get(frame_id) {
        return Err(RwError::Message(error.clone()));
    }
    Ok(state.frame_sessions.get(frame_id).and_then(|session_id| {
        let session_is_ready = session_id == &state.main_session_id
            || state.iframe_sessions_ready.contains(session_id);
        (session_is_ready && different_from_session_id != Some(session_id.as_str()))
            .then(|| session_id.clone())
    }))
}

async fn wait_for_frame_session(
    page: &Arc<PageInner>,
    frame_id: &str,
    different_from_session_id: Option<&str>,
    deadline: OperationDeadline,
) -> RwResult<Option<String>> {
    let mut updates = page.frame_state.lock().unwrap().subscribe_session_updates();
    loop {
        if let Some(session_id) =
            frame_session_for_frame(page, frame_id, different_from_session_id)?
        {
            return Ok(Some(session_id));
        }
        let remaining = deadline.remaining()?;
        match tokio::time::timeout(remaining, updates.changed()).await {
            Ok(Ok(())) => {}
            Ok(Err(_)) => return Ok(None),
            Err(_) => {
                deadline.remaining()?;
                return Ok(None);
            }
        }
    }
}

fn is_screenshot_css_viewport_clip(clip: &Value) -> bool {
    clip.get("__rustwrightClip").and_then(Value::as_str) == Some("cssViewport")
}

fn screenshot_clip_number(value: &Value, field: &str) -> RwResult<f64> {
    value
        .get(field)
        .and_then(Value::as_f64)
        .ok_or_else(|| RwError::Message(format!("screenshot viewport clip missing {field}")))
}

fn screenshot_capture_params_json(
    image_type: &str,
    capture_beyond_viewport: bool,
    quality: Option<u32>,
    clip: Option<&Value>,
) -> RwResult<String> {
    let image_type_json = serde_json::to_string(image_type)?;
    let mut params = format!(
        "{{\"format\":{image_type_json},\"captureBeyondViewport\":{capture_beyond_viewport},\"fromSurface\":true,\"optimizeForSpeed\":true"
    );
    if let Some(value) = quality {
        params.push_str(",\"quality\":");
        params.push_str(&value.to_string());
    }
    if let Some(clip) = clip {
        params.push_str(",\"clip\":");
        params.push_str(&clip.to_string());
    }
    params.push('}');
    Ok(params)
}

async fn resolve_screenshot_clip(
    client: &CdpClient,
    session_id: &str,
    clip: Value,
    full_page: bool,
    timeout: Duration,
) -> RwResult<Value> {
    if !is_screenshot_css_viewport_clip(&clip) {
        return Ok(clip);
    }

    let expression = if full_page {
        r#"(() => ({
            x: 0,
            y: 0,
            width: Math.max(
              document.documentElement ? document.documentElement.scrollWidth : 0,
              document.body ? document.body.scrollWidth : 0,
              window.innerWidth
            ),
            height: Math.max(
              document.documentElement ? document.documentElement.scrollHeight : 0,
              document.body ? document.body.scrollHeight : 0,
              window.innerHeight
            )
        }))()"#
    } else {
        r#"(() => ({
            x: window.scrollX,
            y: window.scrollY,
            width: window.innerWidth,
            height: window.innerHeight
        }))()"#
    };
    let result = client
        .send(
            "Runtime.evaluate",
            json!({
                "expression": expression,
                "awaitPromise": false,
                "returnByValue": true,
                "userGesture": true,
            }),
            Some(session_id),
            timeout,
        )
        .await?;
    let geometry: Value = serde_json::from_str(&runtime_result_to_json(&result)?)?;
    Ok(json!({
        "x": screenshot_clip_number(&geometry, "x")?,
        "y": screenshot_clip_number(&geometry, "y")?,
        "width": screenshot_clip_number(&geometry, "width")?,
        "height": screenshot_clip_number(&geometry, "height")?,
        "scale": clip.get("scale").and_then(Value::as_f64).unwrap_or(1.0),
    }))
}

async fn page_goto_async(
    page: Arc<PageInner>,
    url: String,
    wait_until: String,
    timeout: Duration,
    referer: Option<String>,
) -> RwResult<String> {
    let client = Arc::clone(&page.browser.client);
    let session_id = page.session_id.clone();
    let mut events = client.subscribe();
    let target_url = url.clone();
    let mut params = json!({ "url": url });
    if let Some(referer) = referer {
        params["referrer"] = Value::String(referer);
        params["referrerPolicy"] = Value::String("unsafeUrl".to_string());
    }
    let result = client
        .send("Page.navigate", params, Some(&session_id), timeout)
        .await?;
    if let Some(error_text) = result.get("errorText").and_then(Value::as_str) {
        let failed_url = result
            .get("url")
            .and_then(Value::as_str)
            .unwrap_or(target_url.as_str());
        return Err(RwError::Message(format!(
            "Page.goto: {error_text} at {failed_url}"
        )));
    }
    let loader_id = result
        .get("loaderId")
        .and_then(Value::as_str)
        .map(ToString::to_string);
    if loader_id.is_none() {
        if let Some(frame_id) = result.get("frameId").and_then(Value::as_str) {
            page.record_main_frame_navigation_url(frame_id, &target_url);
        }
        return Ok(Value::Null.to_string());
    }
    let response = wait_for_navigation(
        &mut events,
        &session_id,
        &wait_until,
        loader_id.as_deref(),
        None,
        "Page.goto",
        timeout,
    )
    .await?;
    Ok(response
        .unwrap_or_else(|| {
            json!({
                "url": result.get("url").cloned().unwrap_or(Value::Null),
                "loader_id": loader_id,
            })
        })
        .to_string())
}

fn is_locator_wait_context_loss(error: &RwError) -> bool {
    let message = match error {
        RwError::Cdp { message, .. } => message.to_ascii_lowercase(),
        _ => return false,
    };
    // Only retry when the resolved execution environment disappeared; page terminal state is
    // checked separately so an ambiguous navigation-or-close error never masks a closed target.
    [
        "inspected target navigated or closed",
        "execution context was destroyed",
        "cannot find context with specified id",
        "cannot find context with id",
        "session with given id not found",
        "no session with given id",
        "session is detached",
        "session detached",
        "frame with the given id was not found",
        "no frame for given id",
        "no frame with given id",
        "cannot find frame with id",
        "frame was detached",
    ]
    .iter()
    .any(|fragment| message.contains(fragment))
}

fn locator_wait_terminal_error(page: &PageInner) -> Option<RwError> {
    if page.crashed.load(Ordering::SeqCst) {
        return Some(RwError::PageCrashed);
    }
    if page.lifecycle.is_closing_or_closed()
        || page.target_closed.load(Ordering::SeqCst)
        || !page.browser.client.is_connected()
    {
        return Some(RwError::TargetClosed(TargetClosedKind::Page));
    }
    None
}

fn locator_wait_timeout(timeout: Duration) -> RwError {
    RwError::Timeout(timeout.as_millis().min(u128::from(u64::MAX)) as u64)
}

fn wait_for_selector_body(state: &str, strict: bool, timeout: Duration) -> String {
    let state_json = serde_json::to_string(state).unwrap_or_else(|_| "\"visible\"".to_string());
    let strict_json = if strict { "true" } else { "false" };
    let timeout_millis = timeout.as_millis().max(1);
    format!(
        r#"
const targetState = {state_json};
const strict = {strict_json};
const timeoutMs = {timeout_millis};
const snapshot = () => {{
  const currentMatches = all(spec);
  if (strict && currentMatches.length > 1) return {{ strict: true, count: currentMatches.length }};
  const current = currentMatches[index] || null;
  const attached = !!current;
  const isVisible = !!current && visible(current);
  const matched = targetState === 'attached'
    ? attached
    : targetState === 'detached'
      ? !attached
      : targetState === 'hidden'
        ? (!attached || !isVisible)
        : isVisible;
  return {{ attached, matched }};
}};
const first = snapshot();
if (first.strict) return `__rustwright_strict_violation__:${{first.count}}`;
if (first.matched) return first.attached;
return new Promise(resolve => {{
  let settled = false;
  let observer = null;
  let interval = null;
  let timer = null;
  const finish = value => {{
    if (settled) return;
    settled = true;
    if (observer) observer.disconnect();
    if (interval) clearInterval(interval);
    if (timer) clearTimeout(timer);
    resolve(value);
  }};
  const check = () => {{
    const next = snapshot();
    if (next.strict) finish(`__rustwright_strict_violation__:${{next.count}}`);
    else if (next.matched) finish(next.attached);
  }};
  observer = new MutationObserver(check);
  observer.observe(document, {{ subtree: true, childList: true, attributes: true, characterData: true }});
  interval = setInterval(check, 5);
  timer = setTimeout(() => finish('__rustwright_timeout__'), timeoutMs);
}});
"#
    )
}

async fn evaluate_wait_for_selector_attempt(
    page: &Arc<PageInner>,
    locator_json: &str,
    index: usize,
    state: &str,
    strict: bool,
    deadline: OperationDeadline,
) -> RwResult<String> {
    let resolution = resolve_locator_session(Arc::clone(page), locator_json, deadline).await?;
    let body = wait_for_selector_body(state, strict, deadline.remaining()?);
    let expression = locator_script(&resolution.locator_json, index, &body);
    evaluate_locator_resolution(
        page,
        &resolution,
        expression,
        deadline,
        Duration::from_secs(1),
    )
    .await
}

async fn verify_locator_wait_target_liveness(
    page: &PageInner,
    deadline: OperationDeadline,
) -> RwResult<()> {
    if let Some(error) = locator_wait_terminal_error(page) {
        return Err(error);
    }
    let timeout = deadline.remaining_capped(Duration::from_millis(250))?;
    match page
        .browser
        .client
        .send(
            "Target.getTargetInfo",
            json!({ "targetId": page.target_id }),
            None,
            timeout,
        )
        .await
    {
        Ok(_) => Ok(()),
        // Only a protocol rejection proves the target is gone; a probe timeout on a
        // slow or remote connection is inconclusive and must not abort the wait.
        Err(RwError::Cdp { .. }) => Err(RwError::TargetClosed(TargetClosedKind::Page)),
        Err(_) => Ok(()),
    }
}

async fn wait_before_rearming_locator_wait(
    page: &PageInner,
    deadline: OperationDeadline,
) -> RwResult<()> {
    if let Some(error) = locator_wait_terminal_error(page) {
        return Err(error);
    }
    tokio::time::sleep(deadline.remaining_capped(Duration::from_millis(25))?).await;
    if let Some(error) = locator_wait_terminal_error(page) {
        return Err(error);
    }
    deadline.remaining().map(|_| ())
}

async fn run_locator_wait_retry<T, Attempt, AttemptFuture>(
    page: Arc<PageInner>,
    timeout: Duration,
    mut attempt: Attempt,
) -> RwResult<T>
where
    Attempt: FnMut(OperationDeadline) -> AttemptFuture,
    AttemptFuture: Future<Output = RwResult<T>>,
{
    let deadline = OperationDeadline::new(timeout);
    loop {
        if let Some(error) = locator_wait_terminal_error(&page) {
            return Err(error);
        }
        let attempt = attempt(deadline);
        tokio::pin!(attempt);
        let result = loop {
            tokio::select! {
                result = &mut attempt => break result,
                _ = tokio::time::sleep(Duration::from_millis(10)) => {
                    if let Some(error) = locator_wait_terminal_error(&page) {
                        return Err(error);
                    }
                }
            }
        };
        match result {
            Ok(value) => return Ok(value),
            Err(error) if is_locator_wait_context_loss(&error) => {
                verify_locator_wait_target_liveness(&page, deadline).await?;
                wait_before_rearming_locator_wait(&page, deadline).await?;
            }
            Err(RwError::Timeout(_)) => return Err(locator_wait_timeout(timeout)),
            Err(error) => return Err(error),
        }
    }
}

async fn page_wait_for_selector_async(
    page: Arc<PageInner>,
    locator_json: String,
    index: usize,
    state: String,
    timeout: Duration,
    strict: bool,
) -> RwResult<bool> {
    let attempt_page = Arc::clone(&page);
    let json = run_locator_wait_retry(page, timeout, move |deadline| {
        let page = Arc::clone(&attempt_page);
        let locator_json = locator_json.clone();
        let state = state.clone();
        async move {
            evaluate_wait_for_selector_attempt(
                &page,
                &locator_json,
                index,
                &state,
                strict,
                deadline,
            )
            .await
        }
    })
    .await?;
    let value = serde_json::from_str::<Value>(&json).unwrap_or(Value::Null);
    if value.as_str() == Some("__rustwright_timeout__") {
        return Err(locator_wait_timeout(timeout));
    }
    if let Some(count) = value
        .as_str()
        .and_then(|text| text.strip_prefix("__rustwright_strict_violation__:"))
        .and_then(|text| text.parse::<u64>().ok())
    {
        return Err(RwError::Message(format!(
            "strict mode violation: locator resolved to {count} elements while trying to wait_for_selector"
        )));
    }
    Ok(value.as_bool().unwrap_or(false))
}

fn locator_action_body(action: &str, strict: bool, timeout: Duration) -> String {
    let strict_json = if strict { "true" } else { "false" };
    let timeout_millis = timeout.as_millis().max(1);
    format!(
        r#"
const strict = {strict_json};
const timeoutMs = {timeout_millis};
const attempt = () => {{
  const currentMatches = all(spec);
  if (strict && currentMatches.length > 1) return {{ done: true, value: `__rustwright_strict_violation__:${{currentMatches.length}}` }};
  const current = currentMatches[index] || null;
  if (!current || !visible(current) || current.disabled) return {{ done: false }};
  const el = current;
  {action}
  return {{ done: true, value: true }};
}};
const first = attempt();
if (first.done) return first.value;
return new Promise(resolve => {{
  let settled = false;
  let observer = null;
  let interval = null;
  let timer = null;
  const finish = value => {{
    if (settled) return;
    settled = true;
    if (observer) observer.disconnect();
    if (interval) clearInterval(interval);
    if (timer) clearTimeout(timer);
    resolve(value);
  }};
  const check = () => {{
    const next = attempt();
    if (next.done) finish(next.value);
  }};
  observer = new MutationObserver(check);
  observer.observe(document, {{ subtree: true, childList: true, attributes: true, characterData: true }});
  interval = setInterval(check, 5);
  timer = setTimeout(() => finish('__rustwright_timeout__'), timeoutMs);
}});
"#
    )
}

async fn page_locator_action_async(
    page: Arc<PageInner>,
    locator_json: String,
    index: usize,
    action: String,
    timeout: Duration,
    strict: bool,
    method: &'static str,
) -> RwResult<()> {
    let body = locator_action_body(&action, strict, timeout);
    let eval_timeout = Duration::from_millis(timeout.as_millis().saturating_add(1_000) as u64);
    let json = evaluate_locator_for_page(page, locator_json, index, body, eval_timeout).await?;
    let value = serde_json::from_str::<Value>(&json).unwrap_or(Value::Null);
    if value.as_str() == Some("__rustwright_timeout__") {
        return Err(RwError::Timeout(timeout.as_millis() as u64));
    }
    if let Some(count) = value
        .as_str()
        .and_then(|text| text.strip_prefix("__rustwright_strict_violation__:"))
        .and_then(|text| text.parse::<u64>().ok())
    {
        return Err(RwError::Message(format!(
            "strict mode violation: locator resolved to {count} elements while trying to {method}"
        )));
    }
    Ok(())
}

fn native_action_body(template: &str) -> String {
    template
        .replace("__SCROLL__", "true")
        .replace("__STABLE__", "true")
        .replace("__RECEIVES_EVENTS__", "true")
        .replace("__STABLE_POSITION_REQUIRED__", "true")
        .replace("__ACTION_POSITION__", "null")
}

fn native_fill_body(value: &str, strict: bool) -> RwResult<String> {
    let value_json = serde_json::to_string(value)?;
    Ok(LOCATOR_FILL_TEMPLATE
        .replace("__STRICT__", if strict { "true" } else { "false" })
        .replace("__FORCED__", "false")
        .replace("__VALUE__", &value_json))
}

fn decode_runtime_serialized_value(value: Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(
            values
                .into_iter()
                .map(decode_runtime_serialized_value)
                .collect(),
        ),
        Value::Object(mut object) => {
            if object.contains_key("__rustwright_cdp_object__") {
                return object
                    .remove("entries")
                    .map(decode_runtime_serialized_value)
                    .unwrap_or_else(|| json!({}));
            }
            if object.contains_key("__rustwright_cdp_array__") {
                return object
                    .remove("items")
                    .map(decode_runtime_serialized_value)
                    .unwrap_or_else(|| json!([]));
            }
            if object.contains_key("__rustwright_cdp_undefined__") {
                return Value::Null;
            }
            Value::Object(
                object
                    .into_iter()
                    .map(|(key, value)| (key, decode_runtime_serialized_value(value)))
                    .collect(),
            )
        }
        value => value,
    }
}

const DISABLED_ACTION_TIMEOUT: Duration = Duration::from_secs(24 * 60 * 60);
const DISABLED_ACTION_TIMEOUT_MS: f64 = 24.0 * 60.0 * 60.0 * 1_000.0;

fn sanitize_action_timeout_ms(timeout_ms: Option<f64>, nonpositive_is_disabled: bool) -> f64 {
    let timeout_ms = timeout_ms.unwrap_or(30_000.0);
    if !timeout_ms.is_finite() || timeout_ms > DISABLED_ACTION_TIMEOUT_MS {
        DISABLED_ACTION_TIMEOUT_MS
    } else if timeout_ms <= 0.0 && nonpositive_is_disabled {
        DISABLED_ACTION_TIMEOUT_MS
    } else {
        timeout_ms.max(0.0)
    }
}

fn action_timeout_duration(timeout_ms: Option<f64>, nonpositive_is_disabled: bool) -> Duration {
    Duration::from_secs_f64(
        sanitize_action_timeout_ms(timeout_ms, nonpositive_is_disabled) / 1_000.0,
    )
}

fn action_deadline(timeout: Duration) -> Instant {
    let now = Instant::now();
    now.checked_add(timeout)
        .or_else(|| now.checked_add(DISABLED_ACTION_TIMEOUT))
        .unwrap_or(now)
}

fn action_poll_timeout(
    timeout_ms: Option<f64>,
    nonpositive_is_disabled: bool,
    remaining: Duration,
) -> Duration {
    let timeout_disabled = sanitize_action_timeout_ms(timeout_ms, nonpositive_is_disabled)
        >= DISABLED_ACTION_TIMEOUT_MS;
    let minimum = Duration::from_millis(1);
    if timeout_disabled {
        return remaining.min(Duration::from_secs(1)).max(minimum);
    }
    remaining.max(minimum)
}

fn ensure_native_action_owner_available(page: &PageInner, action: &str) -> RwResult<()> {
    if page.crashed.load(Ordering::SeqCst) {
        return Err(RwError::Message("Page crashed".to_string()));
    }
    if page.lifecycle.is_closing_or_closed()
        || page.target_closed.load(Ordering::SeqCst)
        || page.browser.lifecycle.is_closing_or_closed()
        || !page.browser.client.is_connected()
    {
        return Err(RwError::Message(format!(
            "Locator.{action}: Target page, context or browser has been closed"
        )));
    }
    Ok(())
}

fn strict_violation_error(info: &Value, strict: bool, action: &str) -> Option<RwError> {
    if !strict {
        return None;
    }
    if let Some(violation) = info.get("frame_strict_violation") {
        let count = violation.get("count").and_then(Value::as_u64).unwrap_or(0);
        if count > 1 {
            let selector = violation
                .get("selector")
                .and_then(Value::as_str)
                .unwrap_or("iframe");
            return Some(RwError::Message(format!(
                "strict mode violation: locator(\"{selector}\") resolved to {count} elements"
            )));
        }
    }
    let count = info.get("count").and_then(Value::as_u64).unwrap_or(0);
    (count > 1).then(|| {
        RwError::Message(format!(
            "strict mode violation: locator resolved to {count} elements while trying to {action}"
        ))
    })
}

fn actionability_state_succeeds(info: &Value) -> bool {
    [
        "attached",
        "visible",
        "enabled",
        "receives_events",
        "stable",
    ]
    .into_iter()
    .all(|field| info.get(field).and_then(Value::as_bool).unwrap_or(false))
}

fn click_point_from_state(info: &Value) -> Option<(f64, f64)> {
    let rect = info.get("rect")?;
    let x = rect.get("x")?.as_f64()?;
    let y = rect.get("y")?.as_f64()?;
    let width = rect.get("width")?.as_f64()?;
    let height = rect.get("height")?.as_f64()?;
    (width > 0.0 && height > 0.0).then_some((x + width / 2.0, y + height / 2.0))
}

fn unwrap_nth_locator_spec(mut spec: &Value) -> &Value {
    while spec.get("kind").and_then(Value::as_str) == Some("nth") {
        let Some(base) = spec.get("base").filter(|base| base.is_object()) else {
            break;
        };
        spec = base;
    }
    spec
}

fn leading_frame_spec_for_point_translation(spec: &Value) -> Option<Value> {
    let current = unwrap_nth_locator_spec(spec);
    match current.get("kind").and_then(Value::as_str) {
        Some("frame") => Some(current.clone()),
        Some("descendant" | "filtered") => current
            .get("base")
            .and_then(leading_frame_spec_for_point_translation),
        _ => None,
    }
}

fn frame_spec_with_inner(frame_spec: &Value, inner: Value) -> Value {
    let mut result = frame_spec.clone();
    let nested = result
        .get("inner")
        .filter(|value| value.get("kind").and_then(Value::as_str) == Some("frame"))
        .cloned();
    if let Some(object) = result.as_object_mut() {
        object.insert(
            "inner".to_string(),
            nested.map_or(inner.clone(), |nested| {
                frame_spec_with_inner(&nested, inner)
            }),
        );
    }
    result
}

fn frame_owner_specs_for_point_translation(
    spec: &Value,
    parent_scope: Option<&Value>,
) -> Vec<(Value, i64)> {
    let Some(current) = leading_frame_spec_for_point_translation(spec) else {
        return Vec::new();
    };
    let mut owner_spec = frame_owner_selector_spec(&current);
    if let Some(parent_scope) = parent_scope {
        owner_spec = frame_spec_with_inner(parent_scope, owner_spec);
    }
    let owner_index = current
        .get("frame_index")
        .and_then(Value::as_i64)
        .unwrap_or(0);

    let mut current_scope = current.clone();
    if let Some(object) = current_scope.as_object_mut() {
        object.insert(
            "inner".to_string(),
            json!({ "kind": "css", "selector": "*" }),
        );
    }
    let full_scope = parent_scope.map_or(current_scope.clone(), |parent_scope| {
        frame_spec_with_inner(parent_scope, current_scope)
    });

    let mut result = vec![(owner_spec, owner_index)];
    if let Some(inner) = current.get("inner").filter(|value| value.is_object()) {
        result.extend(frame_owner_specs_for_point_translation(
            inner,
            Some(&full_scope),
        ));
    }
    result
}

fn accumulate_frame_offset(offset: &mut (f64, f64), value: &Value) -> bool {
    let Some(owner_x) = value.get("x").and_then(Value::as_f64) else {
        return false;
    };
    let Some(owner_y) = value.get("y").and_then(Value::as_f64) else {
        return false;
    };
    offset.0 += owner_x;
    offset.1 += owner_y;
    true
}

async fn frame_viewport_offset_for_page(
    page: Arc<PageInner>,
    locator_json: &str,
    deadline: Instant,
) -> RwResult<(f64, f64)> {
    let spec = serde_json::from_str::<Value>(locator_json)?;
    let owner_specs = frame_owner_specs_for_point_translation(&spec, None);
    let mut offset = (0.0, 0.0);
    for (owner_spec, owner_index) in owner_specs {
        let timeout = deadline.saturating_duration_since(Instant::now());
        if timeout.is_zero() {
            return Err(RwError::Timeout(0));
        }
        let scoped_owner_spec = if owner_index == 0 {
            owner_spec
        } else {
            json!({ "kind": "nth", "base": owner_spec, "index": owner_index })
        };
        let json = evaluate_locator_for_page(
            Arc::clone(&page),
            scoped_owner_spec.to_string(),
            0,
            r#"
if (!el) return null;
const rect = el.getBoundingClientRect();
return {
  x: rect.x + (Number(el.clientLeft) || 0),
  y: rect.y + (Number(el.clientTop) || 0)
};
"#
            .to_string(),
            timeout,
        )
        .await?;
        let value = decode_runtime_serialized_value(serde_json::from_str::<Value>(&json)?);
        if !accumulate_frame_offset(&mut offset, &value) {
            return Ok(offset);
        }
    }
    Ok(offset)
}

async fn page_click_actionable_wait_async(
    page: Arc<PageInner>,
    locator_json: String,
    index: usize,
    timeout_ms: Option<f64>,
    strict: bool,
) -> RwResult<(f64, f64, f64)> {
    let timeout_ms = Some(sanitize_action_timeout_ms(timeout_ms, true));
    let deadline = action_deadline(action_timeout_duration(timeout_ms, true));
    let body = native_action_body(LOCATOR_TARGET_STATE_TEMPLATE);
    let mut last_info = json!({ "count": 0 });
    let mut last_info_json = last_info.to_string();
    let actionable_info = loop {
        ensure_native_action_owner_available(&page, "click")?;
        let remaining = deadline.saturating_duration_since(Instant::now());
        let command_timeout = action_poll_timeout(timeout_ms, true, remaining);
        let evaluation = evaluate_locator_for_page(
            Arc::clone(&page),
            locator_json.clone(),
            index,
            body.clone(),
            command_timeout,
        )
        .await;
        let json = match evaluation {
            Ok(json) => json,
            Err(RwError::Timeout(_)) => {
                ensure_native_action_owner_available(&page, "click")?;
                if Instant::now() >= deadline {
                    return Err(ActionTimeoutError::from_raw_json(
                        "actionable",
                        "click",
                        last_info_json,
                        &last_info,
                        None,
                    )
                    .into());
                }
                tokio::time::sleep(
                    deadline
                        .saturating_duration_since(Instant::now())
                        .min(Duration::from_millis(20)),
                )
                .await;
                continue;
            }
            Err(error) => return Err(error),
        };
        let info = decode_runtime_serialized_value(serde_json::from_str::<Value>(&json)?);
        if let Some(error) = strict_violation_error(&info, strict, "click") {
            return Err(error);
        }
        if actionability_state_succeeds(&info) {
            break info;
        }
        if Instant::now() >= deadline {
            return Err(ActionTimeoutError::from_raw_json(
                "actionable",
                "click",
                json,
                &info,
                None,
            )
            .into());
        }
        last_info = info;
        last_info_json = json;
        tokio::time::sleep(
            deadline
                .saturating_duration_since(Instant::now())
                .min(Duration::from_millis(20)),
        )
        .await;
    };

    let (local_x, local_y) = click_point_from_state(&actionable_info)
        .ok_or_else(|| RwError::Message("Locator.click: No element matches locator".to_string()))?;
    ensure_native_action_owner_available(&page, "click")?;
    let (offset_x, offset_y) =
        frame_viewport_offset_for_page(Arc::clone(&page), &locator_json, deadline).await?;
    let target_x = local_x + offset_x;
    let target_y = local_y + offset_y;
    let remaining_ms = deadline
        .saturating_duration_since(Instant::now())
        .as_secs_f64()
        * 1_000.0;
    Ok((target_x, target_y, remaining_ms))
}

#[derive(Debug, PartialEq, Eq)]
enum FillAttempt {
    Success,
    Pending,
}

fn classify_fill_attempt(result: &Value) -> RwResult<FillAttempt> {
    if result.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        return Ok(FillAttempt::Success);
    }
    match result.get("type").and_then(Value::as_str).unwrap_or("") {
        "pending" => Ok(FillAttempt::Pending),
        "input-type" => {
            let input_type = result
                .get("inputType")
                .and_then(Value::as_str)
                .or_else(|| result.pointer("/info/input_type").and_then(Value::as_str))
                .unwrap_or("");
            Err(RwError::Message(format!(
                "Locator.fill: Error: Input of type {input_type:?} cannot be filled"
            )))
        }
        "number-text" => Err(RwError::Message(
            "Locator.fill: Error: Cannot type text into input[type=number]".to_string(),
        )),
        "malformed" => Err(RwError::Message(
            "Locator.fill: Error: Malformed value".to_string(),
        )),
        "non-fillable" => Err(RwError::Message(
            "Locator.fill: Error: Element is not an <input>, <textarea>, <select> or [contenteditable] and does not have a role allowing [aria-readonly]".to_string(),
        )),
        "select" | "force-non-fillable" => Err(RwError::Message(
            "Locator.fill: Error: Element is not an <input>, <textarea> or [contenteditable] element"
                .to_string(),
        )),
        result_type => Err(RwError::Message(format!(
            "Locator.fill: unexpected native fill result {result_type:?}"
        ))),
    }
}

async fn page_fill_actionable_async(
    page: Arc<PageInner>,
    locator_json: String,
    index: usize,
    value: String,
    timeout_ms: Option<f64>,
    strict: bool,
) -> RwResult<()> {
    let timeout_ms = Some(sanitize_action_timeout_ms(timeout_ms, false));
    let deadline = action_deadline(action_timeout_duration(timeout_ms, false));
    let body = native_fill_body(&value, strict)?;
    let mut last_info = json!({ "count": 0 });
    let mut last_info_json = last_info.to_string();
    let mut last_info_key = None;
    loop {
        ensure_native_action_owner_available(&page, "fill")?;
        let remaining = deadline.saturating_duration_since(Instant::now());
        let command_timeout = action_poll_timeout(timeout_ms, false, remaining);
        // Sync #106 parity: a probe timeout is transient until the outer action deadline.
        let evaluation = evaluate_locator_for_page(
            Arc::clone(&page),
            locator_json.clone(),
            index,
            body.clone(),
            command_timeout,
        )
        .await;
        let json = match evaluation {
            Ok(json) => json,
            Err(RwError::Timeout(_)) => {
                ensure_native_action_owner_available(&page, "fill")?;
                if Instant::now() >= deadline {
                    return Err(ActionTimeoutError::from_raw_json(
                        "editable",
                        "fill",
                        last_info_json,
                        &last_info,
                        last_info_key,
                    )
                    .into());
                }
                tokio::time::sleep(
                    deadline
                        .saturating_duration_since(Instant::now())
                        .min(Duration::from_millis(20)),
                )
                .await;
                continue;
            }
            Err(error) => return Err(error),
        };
        let result = decode_runtime_serialized_value(serde_json::from_str::<Value>(&json)?);
        let info = result.get("info").cloned().unwrap_or_else(|| json!({}));
        if let Some(error) = strict_violation_error(&info, strict, "fill") {
            return Err(error);
        }
        match classify_fill_attempt(&result)? {
            FillAttempt::Success => return Ok(()),
            FillAttempt::Pending if Instant::now() >= deadline => {
                return Err(ActionTimeoutError::from_raw_json(
                    "editable",
                    "fill",
                    json,
                    &info,
                    Some("info"),
                )
                .into());
            }
            FillAttempt::Pending => {
                last_info = info;
                last_info_json = json;
                last_info_key = Some("info");
                tokio::time::sleep(
                    deadline
                        .saturating_duration_since(Instant::now())
                        .min(Duration::from_millis(20)),
                )
                .await;
            }
        }
    }
}

struct BackgroundOverrideGuard {
    browser: Arc<BrowserInner>,
    session_id: String,
    active_state: Arc<AtomicBool>,
    screenshot_lock: Option<tokio::sync::OwnedMutexGuard<()>>,
    active: bool,
}

impl BackgroundOverrideGuard {
    fn new(page: &PageInner, screenshot_lock: tokio::sync::OwnedMutexGuard<()>) -> Self {
        page.background_override_active
            .store(true, Ordering::SeqCst);
        Self {
            browser: Arc::clone(&page.browser),
            session_id: page.session_id.clone(),
            active_state: Arc::clone(&page.background_override_active),
            screenshot_lock: Some(screenshot_lock),
            active: true,
        }
    }

    async fn restore(&mut self) -> RwResult<()> {
        self.browser
            .client
            .send_raw_params_json(
                "Emulation.setDefaultBackgroundColorOverride",
                "{}".to_string(),
                Some(&self.session_id),
                Duration::from_secs(5),
            )
            .await?;
        self.active = false;
        self.active_state.store(false, Ordering::SeqCst);
        self.screenshot_lock.take();
        Ok(())
    }
}

impl Drop for BackgroundOverrideGuard {
    fn drop(&mut self) {
        if !self.active {
            return;
        }
        let browser = Arc::clone(&self.browser);
        let session_id = self.session_id.clone();
        let active_state = Arc::clone(&self.active_state);
        let screenshot_lock = self.screenshot_lock.take();
        let runtime = browser.runtime.handle().clone();
        runtime.spawn(async move {
            let _ = browser
                .client
                .send_raw_params_json(
                    "Emulation.setDefaultBackgroundColorOverride",
                    "{}".to_string(),
                    Some(&session_id),
                    Duration::from_secs(5),
                )
                .await;
            active_state.store(false, Ordering::SeqCst);
            drop(screenshot_lock);
        });
    }
}

async fn page_screenshot_async(
    page: Arc<PageInner>,
    path: Option<String>,
    capture_beyond_viewport: bool,
    clip: Option<Value>,
    timeout: Duration,
    image_type: String,
    quality: Option<u32>,
    omit_background: bool,
) -> RwResult<Vec<u8>> {
    let client = Arc::clone(&page.browser.client);
    let session_id = page.session_id.clone();
    let transparent_background = omit_background && image_type != "jpeg";
    let mut screenshot_lock = Some(Arc::clone(&page.screenshot_lock).lock_owned().await);
    let mut background_guard = None;
    if transparent_background {
        background_guard = Some(BackgroundOverrideGuard::new(
            &page,
            screenshot_lock.take().unwrap(),
        ));
        client
            .send_raw_params_json(
                "Emulation.setDefaultBackgroundColorOverride",
                "{\"color\":{\"r\":0,\"g\":0,\"b\":0,\"a\":0}}".to_string(),
                Some(&session_id),
                timeout,
            )
            .await?;
    }
    let _screenshot_lock = screenshot_lock;
    let clip = if let Some(clip) = clip {
        Some(
            resolve_screenshot_clip(&client, &session_id, clip, capture_beyond_viewport, timeout)
                .await?,
        )
    } else {
        None
    };
    let params = screenshot_capture_params_json(
        &image_type,
        capture_beyond_viewport || clip.is_some(),
        quality,
        clip.as_ref(),
    )?;
    let capture_result = client
        .send_raw_params_json("Page.captureScreenshot", params, Some(&session_id), timeout)
        .await;
    if let Some(guard) = background_guard.as_mut() {
        let restore_result = guard.restore().await;
        if capture_result.is_ok() {
            restore_result?;
        }
    }
    let result = capture_result?;
    let base64_data = result.get("data").and_then(Value::as_str).unwrap_or("");
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(base64_data)
        .map_err(|error| RwError::Message(error.to_string()))?;
    if let Some(path) = path {
        let path_bytes = bytes.clone();
        tokio::task::spawn_blocking(move || std::fs::write(path, path_bytes))
            .await
            .map_err(|error| RwError::Message(error.to_string()))??;
    }
    Ok(bytes)
}

async fn page_close_cleanup(
    page: Arc<PageInner>,
    timeout: Duration,
    run_before_unload: bool,
) -> RwResult<()> {
    if run_before_unload {
        page.browser
            .client
            .send(
                "Page.close",
                json!({ "runBeforeUnload": true }),
                Some(&page.session_id),
                timeout,
            )
            .await?;
    } else {
        page.browser
            .client
            .send(
                "Target.closeTarget",
                json!({ "targetId": page.target_id }),
                None,
                timeout,
            )
            .await?;
    }
    page.browser.attached_pages.remove_page(
        &page.target_id,
        page.registry_generation,
        Arc::as_ptr(&page),
    );
    page.close_target_on_drop.store(false, Ordering::SeqCst);
    Ok(())
}

async fn page_close_async(
    page: Arc<PageInner>,
    timeout: Duration,
    run_before_unload: bool,
) -> RwResult<()> {
    let lifecycle = Arc::clone(&page.lifecycle);
    single_flight_close(lifecycle, false, move || {
        page_close_cleanup(page, timeout, run_before_unload)
    })
    .await
}

async fn dispatch_mouse_click_sequence_locked_async(
    page: Arc<PageInner>,
    start_x: f64,
    start_y: f64,
    target_x: f64,
    target_y: f64,
    step_count: u32,
    button: String,
    button_mask: i64,
    click_count: i64,
    delay_after_press: f64,
    initial_buttons: i64,
    modifiers: i64,
    timeout: Duration,
) -> RwResult<()> {
    let client = Arc::clone(&page.browser.client);
    let session_id = page.session_id.clone();
    let steps = step_count.max(1);
    if delay_after_press == 0.0 {
        let events = mouse_click_batch_json(
            start_x,
            start_y,
            target_x,
            target_y,
            steps,
            button.as_str(),
            button_mask,
            click_count,
            initial_buttons,
            modifiers,
        )?;
        client
            .send_batch_raw_params_json(
                "Input.dispatchMouseEvent",
                events,
                Some(&session_id),
                timeout,
            )
            .await?;
        return Ok(());
    }

    for index in 1..=steps {
        let fraction = index as f64 / steps as f64;
        let x = start_x + (target_x - start_x) * fraction;
        let y = start_y + (target_y - start_y) * fraction;
        client
            .send(
                "Input.dispatchMouseEvent",
                mouse_event_payload("mouseMoved", x, y, "none", initial_buttons, 0, modifiers),
                Some(&session_id),
                timeout,
            )
            .await?;
    }

    if click_count > 0 {
        for count in 1..=click_count {
            client
                .send(
                    "Input.dispatchMouseEvent",
                    mouse_event_payload(
                        "mousePressed",
                        target_x,
                        target_y,
                        button.as_str(),
                        initial_buttons | button_mask,
                        count,
                        modifiers,
                    ),
                    Some(&session_id),
                    timeout,
                )
                .await?;
            if delay_after_press > 0.0 {
                tokio::time::sleep(Duration::from_secs_f64(delay_after_press / 1_000.0)).await;
            }

            client
                .send(
                    "Input.dispatchMouseEvent",
                    mouse_event_payload(
                        "mouseReleased",
                        target_x,
                        target_y,
                        button.as_str(),
                        initial_buttons & !button_mask,
                        count,
                        modifiers,
                    ),
                    Some(&session_id),
                    timeout,
                )
                .await?;
            if count < click_count && delay_after_press > 0.0 {
                tokio::time::sleep(Duration::from_secs_f64(delay_after_press / 1_000.0)).await;
            }
        }
    }
    Ok(())
}

async fn dispatch_mouse_click_sequence_async(
    page: Arc<PageInner>,
    start_x: f64,
    start_y: f64,
    target_x: f64,
    target_y: f64,
    step_count: u32,
    button: String,
    button_mask: i64,
    click_count: i64,
    delay_after_press: f64,
    initial_buttons: i64,
    modifiers: i64,
    timeout: Duration,
) -> RwResult<()> {
    let _dispatch_guard = page.mouse_dispatch_lock.lock().await;
    dispatch_mouse_click_sequence_locked_async(
        Arc::clone(&page),
        start_x,
        start_y,
        target_x,
        target_y,
        step_count,
        button,
        button_mask,
        click_count,
        delay_after_press,
        initial_buttons,
        modifiers,
        timeout,
    )
    .await
}

async fn dispatch_mouse_click_async(
    page: Arc<PageInner>,
    target_x: f64,
    target_y: f64,
    start_x: f64,
    start_y: f64,
    initial_buttons: i64,
    modifiers: i64,
    remaining_ms: f64,
) -> RwResult<()> {
    let remaining = action_timeout_duration(Some(remaining_ms), false);
    let deadline = action_deadline(remaining);
    let _dispatch_guard = tokio::time::timeout(remaining, page.mouse_dispatch_lock.lock())
        .await
        .map_err(|_| RwError::Timeout(remaining.as_millis() as u64))?;
    ensure_native_action_owner_available(&page, "click")?;
    let timeout = deadline.saturating_duration_since(Instant::now());
    dispatch_mouse_click_sequence_locked_async(
        Arc::clone(&page),
        start_x,
        start_y,
        target_x,
        target_y,
        1,
        "left".to_string(),
        1,
        1,
        0.0,
        initial_buttons,
        modifiers,
        timeout,
    )
    .await
}

#[cfg(feature = "python")]
#[pymethods]
impl PyPage {
    fn background_override_active(&self) -> bool {
        self.inner.background_override_active.load(Ordering::SeqCst)
    }

    #[pyo3(signature = (url, wait_until=None, timeout_ms=None, referer=None))]
    fn goto_async(
        &self,
        py: Python<'_>,
        url: &str,
        wait_until: Option<&str>,
        timeout_ms: Option<f64>,
        referer: Option<&str>,
    ) -> PyResult<Py<PyAny>> {
        let page = Arc::clone(&self.inner);
        let runtime = page.browser.runtime.handle().clone();
        let url = url.to_string();
        let wait_until = wait_until.unwrap_or("load").to_string();
        let referer = referer.map(ToString::to_string);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        python_future_on(
            py,
            runtime,
            page_goto_async(page, url, wait_until, timeout, referer),
            |py, value| Ok(value.into_pyobject(py)?.unbind().into_any()),
        )
    }

    #[pyo3(signature = (expression, arg_json=None, timeout_ms=None))]
    fn evaluate_async(
        &self,
        py: Python<'_>,
        expression: &str,
        arg_json: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<Py<PyAny>> {
        let expression = make_evaluate_expression(expression, arg_json);
        let page = Arc::clone(&self.inner);
        let runtime = page.browser.runtime.handle().clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        python_future_on(
            py,
            runtime,
            evaluate_expression_for_page_async(page, expression, timeout),
            |py, value| Ok(value.into_pyobject(py)?.unbind().into_any()),
        )
    }

    #[pyo3(signature = (locator_json, index, timeout_ms=None, strict=false))]
    fn click_async(
        &self,
        py: Python<'_>,
        locator_json: &str,
        index: usize,
        timeout_ms: Option<f64>,
        strict: bool,
    ) -> PyResult<Py<PyAny>> {
        let page = Arc::clone(&self.inner);
        let runtime = page.browser.runtime.handle().clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let action = r#"
el.scrollIntoView({ block: 'center', inline: 'center' });
if (typeof el.focus === 'function') el.focus({ preventScroll: true });
el.click();
"#
        .to_string();
        python_future_on(
            py,
            runtime,
            page_locator_action_async(
                page,
                locator_json.to_string(),
                index,
                action,
                timeout,
                strict,
                "click",
            ),
            |py, ()| Ok(py.None()),
        )
    }

    #[pyo3(signature = (locator_json, index, timeout_ms=None, strict=false))]
    fn click_actionable_wait_async(
        &self,
        py: Python<'_>,
        locator_json: &str,
        index: usize,
        timeout_ms: Option<f64>,
        strict: bool,
    ) -> PyResult<Py<PyAny>> {
        let page = Arc::clone(&self.inner);
        let runtime = page.browser.runtime.handle().clone();
        python_future_on(
            py,
            runtime,
            page_click_actionable_wait_async(
                page,
                locator_json.to_string(),
                index,
                timeout_ms,
                strict,
            ),
            |py, value| Ok(value.into_pyobject(py)?.unbind().into_any()),
        )
    }

    fn dispatch_mouse_click_async(
        &self,
        py: Python<'_>,
        x: f64,
        y: f64,
        start_x: f64,
        start_y: f64,
        initial_buttons: i64,
        modifiers: i64,
        remaining_ms: f64,
    ) -> PyResult<Py<PyAny>> {
        let page = Arc::clone(&self.inner);
        let runtime = page.browser.runtime.handle().clone();
        python_future_on(
            py,
            runtime,
            dispatch_mouse_click_async(
                page,
                x,
                y,
                start_x,
                start_y,
                initial_buttons,
                modifiers,
                remaining_ms,
            ),
            |py, ()| Ok(py.None()),
        )
    }

    #[pyo3(signature = (locator_json, index, value, timeout_ms=None, strict=false))]
    fn fill_async(
        &self,
        py: Python<'_>,
        locator_json: &str,
        index: usize,
        value: &str,
        timeout_ms: Option<f64>,
        strict: bool,
    ) -> PyResult<Py<PyAny>> {
        let value_json = serde_json::to_string(value)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let action = format!(
            r#"
el.scrollIntoView({{ block: 'center', inline: 'center' }});
if (typeof el.focus === 'function') el.focus({{ preventScroll: true }});
const value = {value_json};
if ('value' in el) el.value = value;
else if (el.isContentEditable) el.textContent = value;
else el.textContent = value;
el.dispatchEvent(new Event('input', {{ bubbles: true }}));
el.dispatchEvent(new Event('change', {{ bubbles: true }}));
"#
        );
        let page = Arc::clone(&self.inner);
        let runtime = page.browser.runtime.handle().clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        python_future_on(
            py,
            runtime,
            page_locator_action_async(
                page,
                locator_json.to_string(),
                index,
                action,
                timeout,
                strict,
                "fill",
            ),
            |py, ()| Ok(py.None()),
        )
    }

    #[pyo3(signature = (locator_json, index, value, timeout_ms=None, strict=false))]
    fn fill_actionable_async(
        &self,
        py: Python<'_>,
        locator_json: &str,
        index: usize,
        value: &str,
        timeout_ms: Option<f64>,
        strict: bool,
    ) -> PyResult<Py<PyAny>> {
        let page = Arc::clone(&self.inner);
        let runtime = page.browser.runtime.handle().clone();
        python_future_on(
            py,
            runtime,
            page_fill_actionable_async(
                page,
                locator_json.to_string(),
                index,
                value.to_string(),
                timeout_ms,
                strict,
            ),
            |py, ()| Ok(py.None()),
        )
    }

    #[pyo3(signature = (locator_json, index, timeout_ms=None))]
    fn inner_text_async(
        &self,
        py: Python<'_>,
        locator_json: &str,
        index: usize,
        timeout_ms: Option<f64>,
    ) -> PyResult<Py<PyAny>> {
        let page = Arc::clone(&self.inner);
        let runtime = page.browser.runtime.handle().clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let locator_json = locator_json.to_string();
        python_future_on(
            py,
            runtime,
            async move {
                let json = evaluate_locator_for_page(
                    page,
                    locator_json,
                    index,
                    "return el ? (el.innerText || el.textContent || '') : null;".to_string(),
                    timeout,
                )
                .await?;
                Ok(serde_json::from_str::<Value>(&json)
                    .ok()
                    .and_then(|value| value.as_str().map(ToString::to_string)))
            },
            |py, value| Ok(value.into_pyobject(py)?.unbind().into_any()),
        )
    }

    #[pyo3(signature = (locator_json, index, state=None, timeout_ms=None, strict=None))]
    fn wait_for_selector_async(
        &self,
        py: Python<'_>,
        locator_json: &str,
        index: usize,
        state: Option<&str>,
        timeout_ms: Option<f64>,
        strict: Option<bool>,
    ) -> PyResult<Py<PyAny>> {
        let page = Arc::clone(&self.inner);
        let runtime = page.browser.runtime.handle().clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        python_future_on(
            py,
            runtime,
            page_wait_for_selector_async(
                page,
                locator_json.to_string(),
                index,
                state.unwrap_or("visible").to_string(),
                timeout,
                strict.unwrap_or(false),
            ),
            |py, value| Ok(value.into_pyobject(py)?.to_owned().unbind().into_any()),
        )
    }

    #[pyo3(signature = (path=None, full_page=None, clip_json=None, timeout_ms=None, image_type=None, quality=None, omit_background=None))]
    fn screenshot_async(
        &self,
        py: Python<'_>,
        path: Option<&str>,
        full_page: Option<bool>,
        clip_json: Option<&str>,
        timeout_ms: Option<f64>,
        image_type: Option<&str>,
        quality: Option<u32>,
        omit_background: Option<bool>,
    ) -> PyResult<Py<PyAny>> {
        let clip = clip_json
            .map(serde_json::from_str::<Value>)
            .transpose()
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let page = Arc::clone(&self.inner);
        let runtime = page.browser.runtime.handle().clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        python_future_on(
            py,
            runtime,
            page_screenshot_async(
                page,
                path.map(ToString::to_string),
                full_page.unwrap_or(false),
                clip,
                timeout,
                image_type.unwrap_or("png").to_string(),
                quality,
                omit_background.unwrap_or(false),
            ),
            |py, bytes| Ok(PyBytes::new(py, &bytes).unbind().into_any()),
        )
    }

    #[pyo3(signature = (timeout_ms=None, run_before_unload=false))]
    fn close_async(
        &self,
        py: Python<'_>,
        timeout_ms: Option<f64>,
        run_before_unload: bool,
    ) -> PyResult<Py<PyAny>> {
        let page = Arc::clone(&self.inner);
        let runtime = page.browser.runtime.handle().clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let cleanup = runtime.spawn(page_close_async(page, timeout, run_before_unload));
        python_future_on(
            py,
            runtime,
            async move {
                cleanup
                    .await
                    .map_err(|error| RwError::Message(error.to_string()))?
            },
            |py, ()| Ok(py.None()),
        )
    }

    #[pyo3(signature = (width, height, device_scale_factor=1.0, mobile=false, timeout_ms=None))]
    fn set_device_metrics_async(
        &self,
        py: Python<'_>,
        width: i64,
        height: i64,
        device_scale_factor: f64,
        mobile: bool,
        timeout_ms: Option<f64>,
    ) -> PyResult<Py<PyAny>> {
        let page = Arc::clone(&self.inner);
        let runtime = page.browser.runtime.handle().clone();
        let client = Arc::clone(&page.browser.client);
        let session_id = page.session_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        python_future_on(
            py,
            runtime,
            async move {
                let screen_orientation = device_screen_orientation(width, height, mobile);
                let mut params = json!({
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": device_scale_factor,
                    "mobile": mobile,
                    "screenOrientation": screen_orientation,
                });
                if width > 0 && height > 0 {
                    params["screenWidth"] = Value::from(width);
                    params["screenHeight"] = Value::from(height);
                }
                client
                    .send(
                        "Emulation.setDeviceMetricsOverride",
                        params,
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            },
            |py, ()| Ok(py.None()),
        )
    }

    #[getter]
    fn target_id(&self) -> String {
        self.inner.target_id.clone()
    }

    #[getter]
    fn context_id(&self) -> PyResult<Option<String>> {
        if let Some(context_id) = &self.inner.context_id {
            return Ok(Some(context_id.clone()));
        }
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let target_id = page.target_id.clone();
        let result = browser
            .block_on(async move {
                client
                    .send(
                        "Target.getTargetInfo",
                        json!({ "targetId": target_id }),
                        None,
                        Duration::from_secs(5),
                    )
                    .await
            })
            .map_err(py_err)?;
        Ok(result
            .pointer("/targetInfo/browserContextId")
            .and_then(Value::as_str)
            .map(ToString::to_string))
    }

    fn cdp_session(&self) -> PyCdpSession {
        PyCdpSession {
            browser: Arc::clone(&self.inner.browser),
            session_id: Some(self.inner.session_id.clone()),
            detached: AtomicBool::new(false),
        }
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn frame_tree(&self, timeout_ms: Option<f64>) -> PyResult<String> {
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        browser
            .block_on(async move {
                let _ = refresh_page_frame_tree(&page, timeout).await;
                Ok(page.frame_tree_payload().to_string())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn new_cdp_session(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<PyCdpSession> {
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let target_id = page.target_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let result = py
            .detach(move || {
                browser.block_on(async move {
                    client
                        .send(
                            "Target.attachToTarget",
                            json!({ "targetId": target_id, "flatten": true }),
                            None,
                            timeout,
                        )
                        .await
                })
            })
            .map_err(py_err)?;
        let session_id = result
            .get("sessionId")
            .and_then(Value::as_str)
            .ok_or_else(|| PyRuntimeError::new_err("CDP did not return a sessionId"))?
            .to_string();
        Ok(PyCdpSession {
            browser: Arc::clone(&self.inner.browser),
            session_id: Some(session_id),
            detached: AtomicBool::new(false),
        })
    }

    #[pyo3(signature = (events_json, timeout_ms=None))]
    fn dispatch_mouse_events(
        &self,
        py: Python<'_>,
        events_json: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let mut events = serde_json::from_str::<Vec<Value>>(events_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        py.detach(move || {
            browser.block_on(async move {
                for event in &mut events {
                    let delay_after_ms = event
                        .get("__delayAfterMs")
                        .and_then(Value::as_f64)
                        .unwrap_or(0.0)
                        .max(0.0);
                    if let Some(object) = event.as_object_mut() {
                        object.remove("__delayAfterMs");
                    }
                    client
                        .send(
                            "Input.dispatchMouseEvent",
                            event.take(),
                            Some(&session_id),
                            timeout,
                        )
                        .await?;
                    if delay_after_ms > 0.0 {
                        tokio::time::sleep(Duration::from_secs_f64(delay_after_ms / 1000.0)).await;
                    }
                }
                Ok(())
            })
        })
        .map_err(py_err)
    }

    #[pyo3(signature = (locator_json, locator_index, action_json, timeout_ms=None))]
    fn dispatch_locator_pointer_action(
        &self,
        py: Python<'_>,
        locator_json: &str,
        locator_index: usize,
        action_json: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let action = serde_json::from_str::<Value>(action_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let locator_json = locator_json.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);

        py.detach(move || {
            browser.block_on(async move {
                let deadline = OperationDeadline::new(timeout);
                let kind = action
                    .get("kind")
                    .and_then(Value::as_str)
                    .ok_or_else(|| RwError::Message("pointer action is missing kind".to_string()))?;
                let number = |name: &str| {
                    action.get(name).and_then(Value::as_f64).ok_or_else(|| {
                        RwError::Message(format!("pointer action is missing {name}"))
                    })
                };
                let integer = |name: &str, default: i64| {
                    action.get(name).and_then(Value::as_i64).unwrap_or(default)
                };
                let position = action.get("position").and_then(Value::as_object);
                let resolved = resolve_locator_point(
                    Arc::clone(&page),
                    &locator_json,
                    locator_index,
                    position.and_then(|value| value.get("x")).and_then(Value::as_f64),
                    position.and_then(|value| value.get("y")).and_then(Value::as_f64),
                    deadline,
                )
                .await?;
                let top_x = number("targetX")?;
                let top_y = number("targetY")?;
                let start_x = number("startX")? - (top_x - resolved.x);
                let start_y = number("startY")? - (top_y - resolved.y);
                let steps = integer("steps", 1).max(1) as u32;
                let initial_buttons = integer("initialButtons", 0);
                let modifiers = integer("modifiers", 0);
                let detach_events = client.subscribe();

                match kind {
                    "click" => {
                        let button = action
                            .get("button")
                            .and_then(Value::as_str)
                            .unwrap_or("left");
                        dispatch_mouse_click_sequence_in_session(
                            &client,
                            &resolved.session_id,
                            start_x,
                            start_y,
                            resolved.x,
                            resolved.y,
                            steps,
                            button,
                            integer("buttonMask", 1),
                            integer("clickCount", 1),
                            action.get("delayMs").and_then(Value::as_f64).unwrap_or(0.0).max(0.0),
                            initial_buttons,
                            modifiers,
                            deadline,
                        )
                        .await?;
                    }
                    "hover" => {
                        dispatch_mouse_move_sequence_in_session(
                            &client,
                            &resolved.session_id,
                            start_x,
                            start_y,
                            resolved.x,
                            resolved.y,
                            steps,
                            initial_buttons,
                            modifiers,
                            deadline,
                        )
                        .await?;
                    }
                    "tap" => {
                        client
                            .send(
                                "Input.dispatchTouchEvent",
                                json!({
                                    "type": "touchStart",
                                    "touchPoints": [{
                                        "x": resolved.x,
                                        "y": resolved.y,
                                        "id": 0,
                                        "radiusX": 1,
                                        "radiusY": 1,
                                        "force": 1,
                                    }],
                                    "modifiers": modifiers,
                                }),
                                Some(&resolved.session_id),
                                deadline.remaining()?,
                            )
                            .await?;
                        client
                            .send(
                                "Input.dispatchTouchEvent",
                                json!({
                                    "type": "touchEnd",
                                    "touchPoints": [],
                                    "modifiers": modifiers,
                                }),
                                Some(&resolved.session_id),
                                deadline.remaining()?,
                            )
                            .await?;
                    }
                    "drag" => {
                        let target_locator_json = action
                            .get("targetLocator")
                            .and_then(Value::as_str)
                            .ok_or_else(|| RwError::Message("drag action is missing target locator".to_string()))?;
                        let target_position = action.get("targetPosition").and_then(Value::as_object);
                        let target = resolve_locator_point(
                            Arc::clone(&page),
                            target_locator_json,
                            integer("targetIndex", 0).max(0) as usize,
                            target_position.and_then(|value| value.get("x")).and_then(Value::as_f64),
                            target_position.and_then(|value| value.get("y")).and_then(Value::as_f64),
                            deadline,
                        )
                        .await?;
                        if target.session_id != resolved.session_id {
                            return Err(RwError::Message(
                                "drag source and target must belong to the same CDP target".to_string(),
                            ));
                        }
                        dispatch_mouse_move_sequence_in_session(
                            &client,
                            &resolved.session_id,
                            start_x,
                            start_y,
                            resolved.x,
                            resolved.y,
                            1,
                            initial_buttons,
                            modifiers,
                            deadline,
                        )
                        .await?;
                        let mut press = mouse_event_payload(
                            "mousePressed",
                            resolved.x,
                            resolved.y,
                            "left",
                            1,
                            1,
                            modifiers,
                        );
                        press["force"] = json!(0.5);
                        client
                            .send(
                                "Input.dispatchMouseEvent",
                                press,
                                Some(&resolved.session_id),
                                deadline.remaining()?,
                            )
                            .await?;
                        run_drag_with_cleanup(
                            &client,
                            &page.session_id,
                            &resolved.session_id,
                            target.x,
                            target.y,
                            modifiers,
                            async {
                                evaluate_resolved_locator_body(
                                    &page,
                                    &resolved,
                                    r#"
if (!el) throw new Error('No element matches locator');
const win = el.ownerDocument.defaultView || window;
let dragEvent = null;
let didStartDrag = Promise.resolve(false);
const dragListener = event => dragEvent = event;
const mouseListener = () => {
  didStartDrag = new Promise(callback => {
    win.addEventListener('dragstart', dragListener, { once: true, capture: true });
    setTimeout(() => callback(dragEvent ? !dragEvent.defaultPrevented : false), 0);
  });
};
win.addEventListener('mousemove', mouseListener, { once: true, capture: true });
win.__rustwrightCleanupDrag = async () => {
  const value = await didStartDrag;
  win.removeEventListener('mousemove', mouseListener, { capture: true });
  win.removeEventListener('dragstart', dragListener, { capture: true });
  delete win.__rustwrightCleanupDrag;
  return value;
};
return true;
"#,
                                    deadline,
                                )
                                .await?;
                                let mut drag_events = client.subscribe();
                                client
                                    .send(
                                        "Input.setInterceptDrags",
                                        json!({ "enabled": true }),
                                        Some(&page.session_id),
                                        deadline.remaining()?,
                                    )
                                    .await?;
                                let first_fraction = 1.0 / f64::from(steps);
                                let first_x =
                                    resolved.x + (target.x - resolved.x) * first_fraction;
                                let first_y =
                                    resolved.y + (target.y - resolved.y) * first_fraction;
                                dispatch_mouse_move_sequence_in_session(
                                    &client,
                                    &resolved.session_id,
                                    resolved.x,
                                    resolved.y,
                                    first_x,
                                    first_y,
                                    1,
                                    1,
                                    modifiers,
                                    deadline,
                                )
                                .await?;
                                let started = evaluate_resolved_locator_body(
                                    &page,
                                    &resolved,
                                    r#"
if (!el) return false;
const win = el.ownerDocument.defaultView || window;
return win.__rustwrightCleanupDrag ? win.__rustwrightCleanupDrag() : false;
"#,
                                    deadline,
                                )
                                .await?
                                .as_bool()
                                .unwrap_or(false);
                                if started {
                                    wait_for_drag_intercepted(
                                        &mut drag_events,
                                        &resolved.session_id,
                                        deadline,
                                    )
                                    .await
                                    .map(Some)
                                } else {
                                    Ok(None)
                                }
                            },
                            |drag_data| async {
                                if let Some(drag_data) = drag_data {
                                    for index in 1..=steps {
                                        let fraction = f64::from(index) / f64::from(steps);
                                        let x = resolved.x + (target.x - resolved.x) * fraction;
                                        let y = resolved.y + (target.y - resolved.y) * fraction;
                                        client
                                            .send(
                                                "Input.dispatchDragEvent",
                                                json!({
                                                    "type": if index == 1 { "dragEnter" } else { "dragOver" },
                                                    "x": x,
                                                    "y": y,
                                                    "data": drag_data,
                                                    "modifiers": modifiers,
                                                }),
                                                Some(&resolved.session_id),
                                                deadline.remaining()?,
                                            )
                                            .await?;
                                    }
                                    client
                                        .send(
                                            "Input.dispatchDragEvent",
                                            json!({
                                                "type": "drop",
                                                "x": target.x,
                                                "y": target.y,
                                                "data": drag_data,
                                                "modifiers": modifiers,
                                            }),
                                            Some(&resolved.session_id),
                                            deadline.remaining()?,
                                        )
                                        .await?;
                                } else {
                                    client
                                        .send(
                                            "Input.dispatchMouseEvent",
                                            mouse_event_payload(
                                                "mouseReleased",
                                                target.x,
                                                target.y,
                                                "left",
                                                0,
                                                1,
                                                modifiers,
                                            ),
                                            Some(&resolved.session_id),
                                            deadline.remaining()?,
                                        )
                                        .await?;
                                }
                                Ok(())
                            },
                        )
                        .await?;
                    }
                    _ => {
                        return Err(RwError::Message(format!(
                            "unsupported locator pointer action: {kind}"
                        )));
                    }
                }

                pointer_action_ordering_barrier(
                    &client,
                    &resolved.session_id,
                    detach_events,
                    deadline,
                )
                .await
            })
        })
        .map_err(py_err)
    }

    #[pyo3(signature = (
        start_x,
        start_y,
        target_x,
        target_y,
        step_count,
        button,
        button_mask,
        click_count,
        delay_ms,
        initial_buttons,
        modifiers,
        timeout_ms=None
    ))]
    fn dispatch_mouse_click_sequence(
        &self,
        py: Python<'_>,
        start_x: f64,
        start_y: f64,
        target_x: f64,
        target_y: f64,
        step_count: u32,
        button: &str,
        button_mask: i64,
        click_count: i64,
        delay_ms: Option<f64>,
        initial_buttons: i64,
        modifiers: i64,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let button = button.to_string();
        let delay_after_press = delay_ms.unwrap_or(0.0).max(0.0);

        py.detach(move || {
            browser.block_on(dispatch_mouse_click_sequence_async(
                page,
                start_x,
                start_y,
                target_x,
                target_y,
                step_count,
                button,
                button_mask,
                click_count,
                delay_after_press,
                initial_buttons,
                modifiers,
                timeout,
            ))
        })
        .map_err(py_err)
    }

    #[pyo3(signature = (url, wait_until=None, timeout_ms=None, referer=None))]
    fn goto(
        &self,
        py: Python<'_>,
        url: &str,
        wait_until: Option<&str>,
        timeout_ms: Option<f64>,
        referer: Option<&str>,
    ) -> PyResult<String> {
        let page = Arc::clone(&self.inner);
        let url = url.to_string();
        let wait_until = wait_until.unwrap_or("load").to_string();
        let referer = referer.map(ToString::to_string);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        py.detach(move || {
            browser.block_on(async move {
                let mut events = client.subscribe();
                let target_url = url.clone();
                let mut params = json!({ "url": url });
                if let Some(referer) = referer {
                    params["referrer"] = Value::String(referer);
                    params["referrerPolicy"] = Value::String("unsafeUrl".to_string());
                }
                let result = client
                    .send("Page.navigate", params, Some(&session_id), timeout)
                    .await?;
                if let Some(error_text) = result.get("errorText").and_then(Value::as_str) {
                    let failed_url = result
                        .get("url")
                        .and_then(Value::as_str)
                        .unwrap_or(target_url.as_str());
                    return Err(RwError::Message(format!(
                        "Page.goto: {error_text} at {failed_url}"
                    )));
                }
                let loader_id = result
                    .get("loaderId")
                    .and_then(Value::as_str)
                    .map(ToString::to_string);
                if loader_id.is_none() {
                    if let Some(frame_id) = result.get("frameId").and_then(Value::as_str) {
                        page.record_main_frame_navigation_url(frame_id, &target_url);
                    }
                    return Ok(Value::Null.to_string());
                }
                let response = wait_for_navigation(
                    &mut events,
                    &session_id,
                    &wait_until,
                    loader_id.as_deref(),
                    None,
                    "Page.goto",
                    timeout,
                )
                .await?;
                let _ = client;
                Ok(response
                    .unwrap_or_else(|| {
                        json!({
                            "url": result.get("url").cloned().unwrap_or(Value::Null),
                            "loader_id": loader_id,
                        })
                    })
                    .to_string())
            })
        })
        .map_err(py_err)
    }

    #[pyo3(signature = (frame_id, url, wait_until=None, timeout_ms=None, referer=None))]
    fn goto_frame(
        &self,
        py: Python<'_>,
        frame_id: &str,
        url: &str,
        wait_until: Option<&str>,
        timeout_ms: Option<f64>,
        referer: Option<&str>,
    ) -> PyResult<String> {
        let page = Arc::clone(&self.inner);
        let frame_id = frame_id.to_string();
        let url = url.to_string();
        let wait_until = wait_until.unwrap_or("load").to_string();
        let referer = referer.map(ToString::to_string);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_for_frame_id(&frame_id);
        py.detach(move || {
            browser.block_on(async move {
                let mut events = client.subscribe();
                let target_url = url.clone();
                let mut params = json!({ "url": url, "frameId": frame_id });
                if let Some(referer) = referer {
                    params["referrer"] = Value::String(referer);
                    params["referrerPolicy"] = Value::String("unsafeUrl".to_string());
                }
                let result = client
                    .send("Page.navigate", params, Some(&session_id), timeout)
                    .await?;
                if let Some(error_text) = result.get("errorText").and_then(Value::as_str) {
                    let failed_url = result
                        .get("url")
                        .and_then(Value::as_str)
                        .unwrap_or(target_url.as_str());
                    return Err(RwError::Message(format!(
                        "Frame.goto: {error_text} at {failed_url}"
                    )));
                }
                let loader_id = result
                    .get("loaderId")
                    .and_then(Value::as_str)
                    .map(ToString::to_string);
                if loader_id.is_none() {
                    if let Some(navigated_frame_id) = result.get("frameId").and_then(Value::as_str)
                    {
                        page.record_frame_navigation_url(
                            navigated_frame_id,
                            &target_url,
                            &session_id,
                        );
                    }
                    return Ok(Value::Null.to_string());
                }
                let response = wait_for_navigation(
                    &mut events,
                    &session_id,
                    &wait_until,
                    loader_id.as_deref(),
                    None,
                    "Frame.goto",
                    timeout,
                )
                .await?;
                let _ = client;
                Ok(response
                    .unwrap_or_else(|| {
                        json!({
                            "url": result.get("url").cloned().unwrap_or(Value::Null),
                            "loader_id": loader_id,
                        })
                    })
                    .to_string())
            })
        })
        .map_err(py_err)
    }

    #[pyo3(signature = (state=None, timeout_ms=None))]
    fn wait_for_load_state(&self, state: Option<&str>, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let state = state.unwrap_or("load").to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let mut events = client.subscribe();
                wait_for_load_state(&client, &mut events, &session_id, &state, timeout).await
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (html, timeout_ms=None))]
    fn set_content(&self, html: &str, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let html = html.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let frame_id = page.main_frame_id(&client, &session_id, timeout).await?;
                let frame_id_json = serde_json::to_string(&frame_id)?;
                let html_json = serde_json::to_string(&html)?;
                let params_json = format!("{{\"frameId\":{frame_id_json},\"html\":{html_json}}}");
                client
                    .send_raw_params_json(
                        "Page.setDocumentContent",
                        params_json,
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                client
                    .send(
                        "Runtime.evaluate",
                        json!({
                            "expression": stealth_init_script(),
                            "awaitPromise": false,
                            "returnByValue": true,
                        }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (source, timeout_ms=None, run_immediately=false))]
    fn add_init_script(
        &self,
        source: &str,
        timeout_ms: Option<f64>,
        run_immediately: bool,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let source = source.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let mut params = json!({ "source": source });
                if run_immediately {
                    params["runImmediately"] = json!(true);
                }
                client
                    .send(
                        "Page.addScriptToEvaluateOnNewDocument",
                        params,
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (enabled, timeout_ms=None))]
    fn set_bypass_csp(&self, enabled: bool, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Page.setBypassCSP",
                        json!({ "enabled": enabled }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (enabled, timeout_ms=None))]
    fn set_bypass_service_worker(&self, enabled: bool, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Network.setBypassServiceWorker",
                        json!({ "bypass": enabled }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (disabled, timeout_ms=None))]
    fn set_script_execution_disabled(
        &self,
        disabled: bool,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Emulation.setScriptExecutionDisabled",
                        json!({ "value": disabled }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (name, timeout_ms=None))]
    fn add_binding(&self, name: &str, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let name = name.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send("Runtime.enable", json!({}), Some(&session_id), timeout)
                    .await?;
                match client
                    .send(
                        "Runtime.addBinding",
                        json!({ "name": name }),
                        Some(&session_id),
                        timeout,
                    )
                    .await
                {
                    Ok(_) => Ok(()),
                    Err(RwError::Cdp { message, .. }) if message.contains("already exists") => {
                        Ok(())
                    }
                    Err(error) => Err(error),
                }
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (headers_json, timeout_ms=None))]
    fn set_extra_http_headers(&self, headers_json: &str, timeout_ms: Option<f64>) -> PyResult<()> {
        let headers: Value = serde_json::from_str(headers_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Network.setExtraHTTPHeaders",
                        json!({ "headers": headers }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (ignore, timeout_ms=None))]
    fn set_ignore_https_errors(&self, ignore: bool, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send("Security.enable", json!({}), Some(&session_id), timeout)
                    .await?;
                client
                    .send(
                        "Security.setIgnoreCertificateErrors",
                        json!({ "ignore": ignore }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (width, height, timeout_ms=None))]
    fn set_viewport_size(&self, width: i64, height: i64, timeout_ms: Option<f64>) -> PyResult<()> {
        self.set_device_metrics(width, height, 1.0, false, timeout_ms)
    }

    #[pyo3(signature = (width, height, device_scale_factor, mobile, timeout_ms=None))]
    fn set_device_metrics(
        &self,
        width: i64,
        height: i64,
        device_scale_factor: f64,
        mobile: bool,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let screen_orientation = device_screen_orientation(width, height, mobile);
                let mut params = json!({
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": device_scale_factor,
                    "mobile": mobile,
                    "screenOrientation": screen_orientation,
                });
                if width > 0 && height > 0 {
                    params["screenWidth"] = Value::from(width);
                    params["screenHeight"] = Value::from(height);
                }
                client
                    .send(
                        "Emulation.setDeviceMetricsOverride",
                        params,
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (width, height, device_scale_factor, mobile, screen_width, screen_height, timeout_ms=None))]
    fn set_device_metrics_with_screen(
        &self,
        width: i64,
        height: i64,
        device_scale_factor: f64,
        mobile: bool,
        screen_width: i64,
        screen_height: i64,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let screen_orientation =
                    device_screen_orientation(screen_width, screen_height, mobile);
                client
                    .send(
                        "Emulation.setDeviceMetricsOverride",
                        json!({
                            "width": width,
                            "height": height,
                            "deviceScaleFactor": device_scale_factor,
                            "mobile": mobile,
                            "screenWidth": screen_width,
                            "screenHeight": screen_height,
                            "screenOrientation": screen_orientation,
                        }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (user_agent, accept_language=None, timeout_ms=None, mobile=None))]
    fn set_user_agent(
        &self,
        user_agent: &str,
        accept_language: Option<&str>,
        timeout_ms: Option<f64>,
        mobile: Option<bool>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let user_agent = user_agent.to_string();
        let accept_language = accept_language.map(ToString::to_string);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let mut params = json!({ "userAgent": user_agent });
                if let Some(accept_language) = accept_language {
                    params["acceptLanguage"] = Value::String(accept_language);
                }
                params["userAgentMetadata"] = stealth_user_agent_metadata(&user_agent, mobile);
                set_user_agent_override(&client, &session_id, params, timeout).await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (enabled, max_touch_points=None, timeout_ms=None))]
    fn set_touch_emulation(
        &self,
        enabled: bool,
        max_touch_points: Option<u32>,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let mut params = json!({ "enabled": enabled });
                if enabled {
                    params["maxTouchPoints"] = Value::from(max_touch_points.unwrap_or(1));
                }
                client
                    .send(
                        "Emulation.setTouchEmulationEnabled",
                        params,
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (latitude, longitude, accuracy=None, timeout_ms=None))]
    fn set_geolocation(
        &self,
        latitude: f64,
        longitude: f64,
        accuracy: Option<f64>,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Emulation.setGeolocationOverride",
                        json!({
                            "latitude": latitude,
                            "longitude": longitude,
                            "accuracy": accuracy.unwrap_or(0.0),
                        }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn set_geolocation_unavailable(&self, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Emulation.setGeolocationOverride",
                        json!({}),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (offline, timeout_ms=None))]
    fn set_offline(&self, offline: bool, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let throughput = if offline { 0 } else { 10_000_000 };
                client
                    .send(
                        "Network.emulateNetworkConditions",
                        json!({
                            "offline": offline,
                            "latency": 0,
                            "downloadThroughput": throughput,
                            "uploadThroughput": throughput,
                        }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (timezone_id, timeout_ms=None))]
    fn set_timezone(&self, timezone_id: &str, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timezone_id = timezone_id.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Emulation.setTimezoneOverride",
                        json!({ "timezoneId": timezone_id }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (locale, timeout_ms=None))]
    fn set_locale(&self, locale: &str, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let locale = locale.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Emulation.setLocaleOverride",
                        json!({ "locale": locale }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (color_scheme, timeout_ms=None))]
    fn set_color_scheme(&self, color_scheme: &str, timeout_ms: Option<f64>) -> PyResult<()> {
        let features = json!([
            { "name": "prefers-color-scheme", "value": color_scheme }
        ])
        .to_string();
        self.set_emulated_media(None, &features, timeout_ms)
    }

    #[pyo3(signature = (media=None, features_json="[]", timeout_ms=None))]
    fn set_emulated_media(
        &self,
        media: Option<&str>,
        features_json: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let media = media.map(ToString::to_string);
        let features: Value = serde_json::from_str(features_json)
            .map_err(RwError::from)
            .map_err(py_err)?;
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let mut params = json!({ "features": features });
                if let Some(media) = media {
                    if media != "no-preference" {
                        if let Some(object) = params.as_object_mut() {
                            object.insert("media".to_string(), Value::String(media));
                        }
                    }
                }
                client
                    .send(
                        "Emulation.setEmulatedMedia",
                        params,
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (expression, arg_json=None, timeout_ms=None))]
    fn evaluate(
        &self,
        py: Python<'_>,
        expression: &str,
        arg_json: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let expression = make_evaluate_expression(expression, arg_json);
        let page = Arc::clone(&self.inner);
        py.detach(move || evaluate_expression_for_page(page, expression, timeout_ms))
            .map_err(py_err)
    }

    #[pyo3(signature = (expression, arg_json=None, timeout_ms=None))]
    fn evaluate_wait_probe(
        &self,
        py: Python<'_>,
        expression: &str,
        arg_json: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let expression = make_evaluate_expression(expression, arg_json);
        let page = Arc::clone(&self.inner);
        py.detach(move || evaluate_locator_wait_probe_for_page(page, expression, timeout_ms))
            .map_err(py_err)
    }

    #[pyo3(signature = (frame_id, expression, arg_json=None, timeout_ms=None))]
    fn evaluate_frame(
        &self,
        py: Python<'_>,
        frame_id: &str,
        expression: &str,
        arg_json: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let expression = make_evaluate_expression(expression, arg_json);
        let page = Arc::clone(&self.inner);
        let frame_id = frame_id.to_string();
        py.detach(move || evaluate_expression_for_frame(page, frame_id, expression, timeout_ms))
            .map_err(py_err)
    }

    #[pyo3(signature = (frame_id, expression, timeout_ms=None))]
    fn evaluate_frame_declaration_helper(
        &self,
        py: Python<'_>,
        frame_id: &str,
        expression: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<Option<String>> {
        let Some(expression) = wrap_declaration_helper_script(expression.trim()) else {
            return Ok(None);
        };
        let page = Arc::clone(&self.inner);
        let frame_id = frame_id.to_string();
        py.detach(move || evaluate_expression_for_frame(page, frame_id, expression, timeout_ms))
            .map(Some)
            .map_err(py_err)
    }

    #[pyo3(signature = (expression, arg_json=None, timeout_ms=None))]
    fn evaluate_handle(
        &self,
        expression: &str,
        arg_json: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let expression = make_evaluate_expression(expression, arg_json);
        self.evaluate_handle_expression(&expression, timeout_ms)
            .map_err(py_err)
    }

    #[pyo3(signature = (function_declaration, arguments_json, return_by_value=true, timeout_ms=None))]
    fn evaluate_with_call_arguments(
        &self,
        function_declaration: &str,
        arguments_json: &str,
        return_by_value: bool,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let arguments = serde_json::from_str::<Value>(arguments_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let global_payload = self
            .evaluate_handle_expression("globalThis", timeout_ms)
            .map_err(py_err)?;
        let global = serde_json::from_str::<Value>(&global_payload)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let object_id = global
            .get("objectId")
            .and_then(Value::as_str)
            .ok_or_else(|| PyRuntimeError::new_err("CDP did not return a global object handle"))?;
        let result = self.call_function_on_handle(
            object_id,
            function_declaration,
            Some(arguments),
            return_by_value,
            timeout_ms,
            None,
        );
        let _ = self.js_handle_dispose(object_id, timeout_ms, None);
        result.map_err(py_err)
    }

    #[pyo3(signature = (object_id, timeout_ms=None, session_id=None))]
    fn js_handle_json_value(
        &self,
        object_id: &str,
        timeout_ms: Option<f64>,
        session_id: Option<&str>,
    ) -> PyResult<String> {
        self.call_function_on_handle(
            object_id,
            "function() { return this; }",
            None,
            true,
            timeout_ms,
            session_id,
        )
        .map_err(py_err)
    }

    #[pyo3(signature = (object_id, name, timeout_ms=None, session_id=None))]
    fn js_handle_get_property(
        &self,
        object_id: &str,
        name: &str,
        timeout_ms: Option<f64>,
        session_id: Option<&str>,
    ) -> PyResult<String> {
        let args = json!([{ "value": name }]);
        self.call_function_on_handle(
            object_id,
            "function(name) { return this == null ? undefined : this[name]; }",
            Some(args),
            false,
            timeout_ms,
            session_id,
        )
        .map_err(py_err)
    }

    #[pyo3(signature = (object_id, timeout_ms=None, session_id=None))]
    fn js_handle_get_properties(
        &self,
        object_id: &str,
        timeout_ms: Option<f64>,
        session_id: Option<&str>,
    ) -> PyResult<String> {
        let page = Arc::clone(&self.inner);
        let object_id = object_id.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = session_id
            .map(ToString::to_string)
            .unwrap_or_else(|| page.session_id.clone());
        browser
            .block_on(async move {
                let result = client
                    .send(
                        "Runtime.getProperties",
                        json!({
                            "objectId": object_id,
                            "ownProperties": true,
                            "accessorPropertiesOnly": false,
                        }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                let mut properties = serde_json::Map::new();
                if let Some(items) = result.get("result").and_then(Value::as_array) {
                    for item in items {
                        if !item
                            .get("enumerable")
                            .and_then(Value::as_bool)
                            .unwrap_or(false)
                        {
                            continue;
                        }
                        let Some(name) = item.get("name").and_then(Value::as_str) else {
                            continue;
                        };
                        let Some(value) = item.get("value") else {
                            continue;
                        };
                        let mut value = value.clone();
                        if let Some(object) = value.as_object_mut() {
                            object.insert(
                                "__rustwright_session_id".to_string(),
                                Value::String(session_id.clone()),
                            );
                        }
                        properties.insert(name.to_string(), value);
                    }
                }
                Ok(Value::Object(properties).to_string())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (object_id, expression, arg_json=None, return_by_value=true, timeout_ms=None, session_id=None))]
    fn js_handle_evaluate(
        &self,
        object_id: &str,
        expression: &str,
        arg_json: Option<&str>,
        return_by_value: bool,
        timeout_ms: Option<f64>,
        session_id: Option<&str>,
    ) -> PyResult<String> {
        let trimmed = expression.trim();
        let function = if arg_json.is_some() {
            format!("function(__rw_arg) {{ const __rw_fn = ({trimmed}); return __rw_fn(this, __rw_arg); }}")
        } else {
            format!("function() {{ const __rw_fn = ({trimmed}); return __rw_fn(this); }}")
        };
        let args = arg_json
            .map(|value| {
                serde_json::from_str::<Value>(value)
                    .map(|parsed| json!([{ "value": parsed }]))
                    .map_err(RwError::from)
            })
            .transpose()
            .map_err(py_err)?;
        self.call_function_on_handle(
            object_id,
            &function,
            args,
            return_by_value,
            timeout_ms,
            session_id,
        )
        .map_err(py_err)
    }

    #[pyo3(signature = (object_id, function_declaration, arguments_json, return_by_value=true, timeout_ms=None, session_id=None))]
    fn js_handle_evaluate_with_call_arguments(
        &self,
        object_id: &str,
        function_declaration: &str,
        arguments_json: &str,
        return_by_value: bool,
        timeout_ms: Option<f64>,
        session_id: Option<&str>,
    ) -> PyResult<String> {
        let arguments = serde_json::from_str::<Value>(arguments_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        self.call_function_on_handle(
            object_id,
            function_declaration,
            Some(arguments),
            return_by_value,
            timeout_ms,
            session_id,
        )
        .map_err(py_err)
    }

    #[pyo3(signature = (object_id, timeout_ms=None, session_id=None))]
    fn js_handle_dispose(
        &self,
        object_id: &str,
        timeout_ms: Option<f64>,
        session_id: Option<&str>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let object_id = object_id.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = session_id
            .map(ToString::to_string)
            .unwrap_or_else(|| page.session_id.clone());
        browser
            .block_on(async move {
                client
                    .send(
                        "Runtime.releaseObject",
                        json!({ "objectId": object_id }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn title(&self, timeout_ms: Option<f64>) -> PyResult<String> {
        let json = self
            .evaluate_expression("document.title", timeout_ms)
            .map_err(py_err)?;
        Ok(serde_json::from_str::<Value>(&json)
            .ok()
            .and_then(|value| value.as_str().map(ToString::to_string))
            .unwrap_or_default())
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn content(&self, timeout_ms: Option<f64>) -> PyResult<String> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let document = client
                    .send(
                        "DOM.getDocument",
                        json!({ "depth": 0, "pierce": true }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                let node_id = document
                    .pointer("/root/nodeId")
                    .and_then(Value::as_i64)
                    .ok_or_else(|| {
                        RwError::Message("CDP did not return a document node id".to_string())
                    })?;
                let outer = client
                    .send(
                        "DOM.getOuterHTML",
                        json!({ "nodeId": node_id }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(outer
                    .get("outerHTML")
                    .and_then(Value::as_str)
                    .map(ToString::to_string)
                    .unwrap_or_default())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn url(&self, timeout_ms: Option<f64>) -> PyResult<String> {
        // Playwright's page.url is a synchronous read of the last-known main-frame
        // url, never a round-trip. Serve it from the frame cache the page already
        // maintains from Page.frameNavigated so the getter cannot block on (or time
        // out against) an execution context that is being torn down mid-navigation.
        if let Some(url) = self.inner.cached_main_frame_url() {
            return Ok(url);
        }
        let json = self
            .evaluate_expression("location.href", timeout_ms)
            .map_err(py_err)?;
        Ok(serde_json::from_str::<Value>(&json)
            .ok()
            .and_then(|value| value.as_str().map(ToString::to_string))
            .unwrap_or_default())
    }

    #[pyo3(signature = (wait_until=None, timeout_ms=None))]
    fn reload(&self, wait_until: Option<&str>, timeout_ms: Option<f64>) -> PyResult<String> {
        let page = Arc::clone(&self.inner);
        let wait_until = wait_until.unwrap_or("load").to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let mut events = client.subscribe();
                client
                    .send("Page.reload", json!({}), Some(&session_id), timeout)
                    .await?;
                if wait_until != "commit" {
                    wait_for_load_state(&client, &mut events, &session_id, &wait_until, timeout)
                        .await?;
                }
                Ok(Value::Null.to_string())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (wait_until=None, timeout_ms=None))]
    fn go_back(&self, wait_until: Option<&str>, timeout_ms: Option<f64>) -> PyResult<String> {
        self.navigate_history(-1, wait_until, timeout_ms)
    }

    #[pyo3(signature = (wait_until=None, timeout_ms=None))]
    fn go_forward(&self, wait_until: Option<&str>, timeout_ms: Option<f64>) -> PyResult<String> {
        self.navigate_history(1, wait_until, timeout_ms)
    }

    #[pyo3(signature = (locator_json, index, timeout_ms=None))]
    fn click(
        &self,
        py: Python<'_>,
        locator_json: &str,
        index: usize,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let body = r#"
if (!el) throw new Error('No element matches locator');
el.scrollIntoView({ block: 'center', inline: 'center' });
if (typeof el.focus === 'function') el.focus({ preventScroll: true });
el.click();
return true;
"#;
        py.detach(|| self.evaluate_locator(locator_json, index, body, timeout_ms))
            .map(|_| ())
            .map_err(py_err)
    }

    #[pyo3(signature = (locator_json, index, body, timeout_ms=None))]
    fn locator_eval(
        &self,
        py: Python<'_>,
        locator_json: &str,
        index: usize,
        body: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        // Release the GIL for the duration of the blocking CDP round-trip, matching
        // `click` and `locator_eval_handle`. `evaluate_locator` already detaches inside
        // `block_on`, but keeping the detach explicit at the binding layer keeps the
        // three locator bindings consistent and guards against a future refactor that
        // adds GIL-holding work before `block_on` or changes the transport.
        py.detach(|| self.evaluate_locator(locator_json, index, body, timeout_ms))
            .map_err(py_err)
    }

    #[pyo3(signature = (locator_json, index, body, timeout_ms=None))]
    fn locator_eval_handle(
        &self,
        py: Python<'_>,
        locator_json: &str,
        index: usize,
        body: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let page = Arc::clone(&self.inner);
        let locator_json = locator_json.to_string();
        let body = body.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        py.detach(move || {
            let browser = Arc::clone(&page.browser);
            browser.block_on(async move {
                evaluate_locator_handle_for_page(page, locator_json, index, body, timeout).await
            })
        })
        .map_err(py_err)
    }

    #[pyo3(signature = (locator_json, index, value, timeout_ms=None))]
    fn fill(
        &self,
        locator_json: &str,
        index: usize,
        value: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let value_json = serde_json::to_string(value)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let body = format!(
            r#"
if (!el) throw new Error('No element matches locator');
el.scrollIntoView({{ block: 'center', inline: 'center' }});
if (typeof el.focus === 'function') el.focus({{ preventScroll: true }});
const value = {value_json};
if ('value' in el) {{
  el.value = value;
}} else if (el.isContentEditable) {{
  el.textContent = value;
}} else {{
  el.textContent = value;
}}
el.dispatchEvent(new Event('input', {{ bubbles: true }}));
el.dispatchEvent(new Event('change', {{ bubbles: true }}));
return true;
"#
        );
        self.evaluate_locator(locator_json, index, &body, timeout_ms)
            .map(|_| ())
            .map_err(py_err)
    }

    #[pyo3(signature = (locator_json, index, text, timeout_ms=None))]
    fn r#type(
        &self,
        locator_json: &str,
        index: usize,
        text: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let text_json = serde_json::to_string(text)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let body = format!(
            r#"
if (!el) throw new Error('No element matches locator');
el.scrollIntoView({{ block: 'center', inline: 'center' }});
if (typeof el.focus === 'function') el.focus({{ preventScroll: true }});
const text = {text_json};
if ('value' in el) {{
  el.value = `${{el.value || ''}}${{text}}`;
}} else if (el.isContentEditable) {{
  el.textContent = `${{el.textContent || ''}}${{text}}`;
}} else {{
  el.textContent = `${{el.textContent || ''}}${{text}}`;
}}
el.dispatchEvent(new Event('input', {{ bubbles: true }}));
el.dispatchEvent(new Event('change', {{ bubbles: true }}));
return true;
"#
        );
        self.evaluate_locator(locator_json, index, &body, timeout_ms)
            .map(|_| ())
            .map_err(py_err)
    }

    #[pyo3(signature = (locator_json, index, timeout_ms=None))]
    fn text_content(
        &self,
        locator_json: &str,
        index: usize,
        timeout_ms: Option<f64>,
    ) -> PyResult<Option<String>> {
        let body = "return el ? el.textContent : null;";
        let json = self
            .evaluate_locator(locator_json, index, body, timeout_ms)
            .map_err(py_err)?;
        Ok(serde_json::from_str::<Value>(&json)
            .ok()
            .and_then(|value| value.as_str().map(ToString::to_string)))
    }

    #[pyo3(signature = (locator_json, index, timeout_ms=None))]
    fn inner_text(
        &self,
        locator_json: &str,
        index: usize,
        timeout_ms: Option<f64>,
    ) -> PyResult<Option<String>> {
        let body = "return el ? (el.innerText || el.textContent || '') : null;";
        let json = self
            .evaluate_locator(locator_json, index, body, timeout_ms)
            .map_err(py_err)?;
        Ok(serde_json::from_str::<Value>(&json)
            .ok()
            .and_then(|value| value.as_str().map(ToString::to_string)))
    }

    #[pyo3(signature = (locator_json, timeout_ms=None))]
    fn count(&self, locator_json: &str, timeout_ms: Option<f64>) -> PyResult<usize> {
        let json = self
            .evaluate_locator(locator_json, 0, "return matches.length;", timeout_ms)
            .map_err(py_err)?;
        Ok(serde_json::from_str::<Value>(&json)
            .ok()
            .and_then(|value| value.as_u64())
            .unwrap_or(0) as usize)
    }

    #[pyo3(signature = (locator_json, index, timeout_ms=None))]
    fn is_visible(
        &self,
        locator_json: &str,
        index: usize,
        timeout_ms: Option<f64>,
    ) -> PyResult<bool> {
        let body = "return !!el && visible(el);";
        let json = self
            .evaluate_locator(locator_json, index, body, timeout_ms)
            .map_err(py_err)?;
        Ok(serde_json::from_str::<Value>(&json)
            .ok()
            .and_then(|value| value.as_bool())
            .unwrap_or(false))
    }

    #[pyo3(signature = (locator_json, index, state=None, timeout_ms=None, strict=None))]
    fn wait_for_selector(
        &self,
        locator_json: &str,
        index: usize,
        state: Option<&str>,
        timeout_ms: Option<f64>,
        strict: Option<bool>,
    ) -> PyResult<bool> {
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        let locator_json = locator_json.to_string();
        let state = state.unwrap_or("visible").to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        browser
            .block_on(page_wait_for_selector_async(
                page,
                locator_json,
                index,
                state,
                timeout,
                strict.unwrap_or(false),
            ))
            .map_err(py_err)
    }

    #[pyo3(signature = (path=None, full_page=None, clip_json=None, timeout_ms=None, image_type=None, quality=None, omit_background=None))]
    fn screenshot(
        &self,
        py: Python<'_>,
        path: Option<&str>,
        full_page: Option<bool>,
        clip_json: Option<&str>,
        timeout_ms: Option<f64>,
        image_type: Option<&str>,
        quality: Option<u32>,
        omit_background: Option<bool>,
    ) -> PyResult<Py<PyBytes>> {
        let page = Arc::clone(&self.inner);
        let path = path.map(ToString::to_string);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let capture_beyond_viewport = full_page.unwrap_or(false);
        let image_type = image_type.unwrap_or("png").to_string();
        let clip = match clip_json {
            Some(value) => Some(
                serde_json::from_str::<Value>(value)
                    .map_err(|error| PyValueError::new_err(error.to_string()))?,
            ),
            None => None,
        };
        let browser = Arc::clone(&page.browser);
        let omit_background = omit_background.unwrap_or(false);
        let bytes = py
            .detach(move || {
                browser.block_on(page_screenshot_async(
                    page,
                    path,
                    capture_beyond_viewport,
                    clip,
                    timeout,
                    image_type,
                    quality,
                    omit_background,
                ))
            })
            .map_err(py_err)?;
        Ok(PyBytes::new(py, &bytes).unbind())
    }

    #[pyo3(signature = (path=None, timeout_ms=None, params_json=None))]
    fn pdf(
        &self,
        py: Python<'_>,
        path: Option<&str>,
        timeout_ms: Option<f64>,
        params_json: Option<&str>,
    ) -> PyResult<String> {
        let page = Arc::clone(&self.inner);
        let path = path.map(ToString::to_string);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let params = match params_json {
            Some(value) => serde_json::from_str::<Value>(value)
                .map_err(|error| PyValueError::new_err(error.to_string()))?,
            None => json!({ "printBackground": false }),
        };
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        let base64_data = py
            .detach(move || {
                browser.block_on(async move {
                    let result = client
                        .send("Page.printToPDF", params, Some(&session_id), timeout)
                        .await?;
                    Ok(result
                        .get("data")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string())
                })
            })
            .map_err(py_err)?;
        if let Some(path) = path {
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(&base64_data)
                .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
            std::fs::write(path, bytes)
                .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        }
        Ok(base64_data)
    }

    #[pyo3(signature = (request_id, timeout_ms=None))]
    fn response_body(
        &self,
        py: Python<'_>,
        request_id: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let page = Arc::clone(&self.inner);
        let request_id = request_id.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        py.detach(move || {
            browser.block_on(async move {
                let deadline = tokio::time::Instant::now() + timeout;
                let mut last_error = None;
                loop {
                    let now = tokio::time::Instant::now();
                    if now >= deadline {
                        return Err(
                            last_error.unwrap_or(RwError::Timeout(timeout.as_millis() as u64))
                        );
                    }
                    let remaining = deadline - now;
                    match client
                        .send(
                            "Network.getResponseBody",
                            json!({ "requestId": request_id }),
                            Some(&session_id),
                            remaining,
                        )
                        .await
                    {
                        Ok(result) => return Ok(result.to_string()),
                        Err(error) => {
                            last_error = Some(error);
                            tokio::time::sleep(Duration::from_millis(20)).await;
                        }
                    }
                }
            })
        })
        .map_err(py_err)
    }

    fn network_event_waiter(&self, kind: &str) -> PyResult<PyNetworkEventWaiter> {
        match kind {
            "request" | "response" | "requestfinished" | "requestfailed" => {
                let client = Arc::clone(&self.inner.browser.client);
                Ok(PyNetworkEventWaiter {
                    browser: Arc::clone(&self.inner.browser),
                    receiver: Mutex::new(Some(client.subscribe())),
                    event_log: Arc::clone(&client.event_log),
                    cursor: Mutex::new(client.event_cursor()),
                    session_id: self.inner.session_id.clone(),
                    kind: kind.to_string(),
                    requests: Arc::clone(&self.inner.network_requests),
                })
            }
            other => Err(PyValueError::new_err(format!(
                "unsupported network event kind: {other}"
            ))),
        }
    }

    fn combined_event_stream(&self) -> PyResult<PyPageEventStream> {
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let event_log = Arc::clone(&client.event_log);
        let (receiver, cursor) = {
            let _log = event_log.lock().unwrap();
            let receiver = client.subscribe();
            (receiver, page.event_stream_start_cursor)
        };
        let (close_tx, _) = watch::channel(false);
        let stream = PyPageEventStream {
            browser: Arc::clone(&browser),
            receiver: Arc::new(Mutex::new(Some(receiver))),
            event_log,
            cursor: Arc::new(Mutex::new(cursor)),
            session_id: page.session_id.clone(),
            requests: Arc::clone(&page.network_requests),
            state: Arc::new(Mutex::new(Some(PageEventStreamState::new()))),
            pending_batch: Arc::new(Mutex::new(None)),
            close_tx,
            closed: Arc::new(AtomicBool::new(false)),
            runtime_enabled: Arc::new(AtomicBool::new(false)),
        };
        Ok(stream)
    }

    fn route_event_waiter(&self) -> PyRouteEventWaiter {
        PyRouteEventWaiter {
            browser: Arc::clone(&self.inner.browser),
            receiver: Mutex::new(Some(self.inner.browser.client.subscribe())),
            session_id: self.inner.session_id.clone(),
        }
    }

    fn auth_event_waiter(&self) -> PyAuthEventWaiter {
        PyAuthEventWaiter {
            browser: Arc::clone(&self.inner.browser),
            receiver: Mutex::new(Some(self.inner.browser.client.subscribe())),
            session_id: self.inner.session_id.clone(),
        }
    }

    fn dialog_event_waiter(&self) -> PyDialogEventWaiter {
        PyDialogEventWaiter {
            browser: Arc::clone(&self.inner.browser),
            receiver: Mutex::new(Some(self.inner.browser.client.subscribe())),
            session_id: self.inner.session_id.clone(),
        }
    }

    fn console_event_waiter(&self) -> PyResult<PyConsoleEventWaiter> {
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Runtime.enable",
                        json!({}),
                        Some(&session_id),
                        Duration::from_secs(5),
                    )
                    .await
                    .map(|_| ())
            })
            .map_err(py_err)?;
        Ok(PyConsoleEventWaiter {
            browser: Arc::clone(&self.inner.browser),
            receiver: Mutex::new(Some(self.inner.browser.client.subscribe())),
            session_id: self.inner.session_id.clone(),
        })
    }

    #[pyo3(signature = (kind, request_id=None))]
    fn websocket_event_waiter(
        &self,
        kind: &str,
        request_id: Option<String>,
    ) -> PyResult<PyWebSocketEventWaiter> {
        match kind {
            "created" | "closed" | "framesent" | "framereceived" | "socketerror" => {
                Ok(PyWebSocketEventWaiter {
                    browser: Arc::clone(&self.inner.browser),
                    receiver: Mutex::new(Some(self.inner.browser.client.subscribe())),
                    session_id: self.inner.session_id.clone(),
                    kind: kind.to_string(),
                    request_id,
                })
            }
            other => Err(PyValueError::new_err(format!(
                "unsupported websocket event kind: {other}"
            ))),
        }
    }

    fn binding_event_waiter(&self, name: &str) -> PyBindingEventWaiter {
        PyBindingEventWaiter {
            page: Arc::clone(&self.inner),
            receiver: Mutex::new(Some(self.inner.browser.client.subscribe())),
            name: name.to_string(),
        }
    }

    #[pyo3(signature = (download_path, timeout_ms=None))]
    fn enable_downloads(&self, download_path: &str, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let download_path = download_path.to_string();
        let context_id = page.context_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        browser
            .block_on(async move {
                let mut params = json!({
                    "behavior": "allowAndName",
                    "downloadPath": download_path,
                    "eventsEnabled": true,
                });
                if let Some(context_id) = context_id {
                    params["browserContextId"] = Value::String(context_id);
                }
                let result = client
                    .send("Browser.setDownloadBehavior", params, None, timeout)
                    .await;
                if matches!(
                    &result,
                    Err(RwError::Cdp { method, message })
                        if method == "Browser.setDownloadBehavior"
                            && message.contains("Failed to find browser context")
                ) {
                    let fallback_params = json!({
                        "behavior": "allowAndName",
                        "downloadPath": download_path,
                        "eventsEnabled": true,
                    });
                    client
                        .send(
                            "Browser.setDownloadBehavior",
                            fallback_params,
                            None,
                            timeout,
                        )
                        .await?;
                    return Ok(());
                }
                result?;
                Ok(())
            })
            .map_err(py_err)
    }

    fn download_event_waiter(&self, download_path: &str) -> PyDownloadEventWaiter {
        PyDownloadEventWaiter {
            browser: Arc::clone(&self.inner.browser),
            receiver: Mutex::new(Some(self.inner.browser.client.subscribe())),
            active_downloads: Mutex::new(HashMap::new()),
            download_path: download_path.to_string(),
        }
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn enable_file_chooser_intercept(&self, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Page.setInterceptFileChooserDialog",
                        json!({ "enabled": true }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    fn file_chooser_event_waiter(&self) -> PyFileChooserEventWaiter {
        PyFileChooserEventWaiter {
            browser: Arc::clone(&self.inner.browser),
            receiver: Mutex::new(Some(self.inner.browser.client.subscribe())),
            session_id: self.inner.session_id.clone(),
        }
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn popup_event_waiter(&self, timeout_ms: Option<f64>) -> PyResult<PyPopupEventWaiter> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        browser
            .block_on(async move {
                client
                    .send(
                        "Target.setDiscoverTargets",
                        json!({ "discover": true }),
                        None,
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)?;
        Ok(PyPopupEventWaiter {
            browser: Arc::clone(&self.inner.browser),
            receiver: Mutex::new(Some(self.inner.browser.client.subscribe())),
            opener_target_id: self.inner.target_id.clone(),
            seen_target_ids: Arc::new(Mutex::new(HashSet::new())),
        })
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn worker_event_waiter(&self, timeout_ms: Option<f64>) -> PyResult<PyWorkerEventWaiter> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Target.setAutoAttach",
                        json!({
                            "autoAttach": true,
                            "waitForDebuggerOnStart": false,
                            "flatten": true,
                        }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)?;
        Ok(PyWorkerEventWaiter {
            browser: Arc::clone(&self.inner.browser),
            receiver: Mutex::new(Some(self.inner.browser.client.subscribe())),
            opener_target_id: self.inner.target_id.clone(),
        })
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn list_workers(&self, timeout_ms: Option<f64>) -> PyResult<Vec<PyWorker>> {
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        let browser_for_task = Arc::clone(&browser);
        let client = Arc::clone(&browser.client);
        let opener_target_id = page.target_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        browser
            .block_on(async move {
                let result = client
                    .send("Target.getTargets", json!({}), None, timeout)
                    .await?;
                let mut workers = Vec::new();
                for info in result
                    .get("targetInfos")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    let target_type = info.get("type").and_then(Value::as_str).unwrap_or("");
                    if target_type != "worker" {
                        continue;
                    }
                    let opener = info.get("openerId").and_then(Value::as_str);
                    if opener != Some(opener_target_id.as_str()) {
                        continue;
                    }
                    let Some(target_id) = info.get("targetId").and_then(Value::as_str) else {
                        continue;
                    };
                    let url = info
                        .get("url")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string();
                    if let Ok(worker) = attach_existing_worker(
                        Arc::clone(&browser_for_task),
                        target_id.to_string(),
                        url,
                        timeout,
                    )
                    .await
                    {
                        workers.push(worker);
                    }
                }
                Ok(workers)
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (backend_node_id, files_json, timeout_ms=None))]
    fn set_file_input_files(
        &self,
        backend_node_id: u64,
        files_json: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let files: Value = serde_json::from_str(files_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "DOM.setFileInputFiles",
                        json!({
                            "backendNodeId": backend_node_id,
                            "files": files,
                        }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (accept, prompt_text=None, timeout_ms=None))]
    fn handle_dialog(
        &self,
        accept: bool,
        prompt_text: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let prompt_text = prompt_text.map(ToString::to_string);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let mut params = json!({ "accept": accept });
                if let Some(prompt_text) = prompt_text {
                    params["promptText"] = Value::String(prompt_text);
                }
                client
                    .send(
                        "Page.handleJavaScriptDialog",
                        params,
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (patterns_json, timeout_ms=None, handle_auth_requests=false))]
    fn enable_fetch(
        &self,
        patterns_json: &str,
        timeout_ms: Option<f64>,
        handle_auth_requests: bool,
    ) -> PyResult<()> {
        let patterns: Option<Value> = if patterns_json.trim().is_empty() {
            None
        } else {
            Some(
                serde_json::from_str(patterns_json)
                    .map_err(|error| PyValueError::new_err(error.to_string()))?,
            )
        };
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let mut params = json!({});
                if let Some(patterns) = patterns {
                    params["patterns"] = patterns;
                }
                if handle_auth_requests {
                    params["handleAuthRequests"] = Value::Bool(true);
                }
                client
                    .send("Fetch.enable", params, Some(&session_id), timeout)
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn disable_fetch(&self, timeout_ms: Option<f64>) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send("Fetch.disable", json!({}), Some(&session_id), timeout)
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (request_id, url=None, method=None, headers_json=None, post_data_base64=None, timeout_ms=None))]
    fn fetch_continue(
        &self,
        request_id: &str,
        url: Option<&str>,
        method: Option<&str>,
        headers_json: Option<&str>,
        post_data_base64: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let headers: Option<Value> = headers_json
            .map(|value| {
                serde_json::from_str(value)
                    .map_err(|error| PyValueError::new_err(error.to_string()))
            })
            .transpose()?;
        let page = Arc::clone(&self.inner);
        let request_id = request_id.to_string();
        let url = url.map(ToString::to_string);
        let method = method.map(ToString::to_string);
        let post_data_base64 = post_data_base64.map(ToString::to_string);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let mut params = json!({ "requestId": request_id });
                if let Some(url) = url {
                    params["url"] = Value::String(url);
                }
                if let Some(method) = method {
                    params["method"] = Value::String(method);
                }
                if let Some(headers) = headers {
                    params["headers"] = headers;
                }
                if let Some(post_data_base64) = post_data_base64 {
                    params["postData"] = Value::String(post_data_base64);
                }
                client
                    .send("Fetch.continueRequest", params, Some(&session_id), timeout)
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (request_id, username, password, timeout_ms=None))]
    fn fetch_continue_with_auth(
        &self,
        request_id: &str,
        username: &str,
        password: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let request_id = request_id.to_string();
        let username = username.to_string();
        let password = password.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Fetch.continueWithAuth",
                        json!({
                            "requestId": request_id,
                            "authChallengeResponse": {
                                "response": "ProvideCredentials",
                                "username": username,
                                "password": password,
                            }
                        }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (request_id, response=None, timeout_ms=None))]
    fn fetch_continue_auth_response(
        &self,
        request_id: &str,
        response: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let request_id = request_id.to_string();
        let response = response.unwrap_or("Default").to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Fetch.continueWithAuth",
                        json!({
                            "requestId": request_id,
                            "authChallengeResponse": {
                                "response": response,
                            }
                        }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (request_id, error_reason=None, timeout_ms=None))]
    fn fetch_fail(
        &self,
        request_id: &str,
        error_reason: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let request_id = request_id.to_string();
        let error_reason = error_reason.unwrap_or("Failed").to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                client
                    .send(
                        "Fetch.failRequest",
                        json!({ "requestId": request_id, "errorReason": error_reason }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (request_id, status, headers_json, body_base64, phrase=None, timeout_ms=None))]
    fn fetch_fulfill(
        &self,
        request_id: &str,
        status: i64,
        headers_json: &str,
        body_base64: &str,
        phrase: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<()> {
        let headers: Value = serde_json::from_str(headers_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let page = Arc::clone(&self.inner);
        let request_id = request_id.to_string();
        let body = body_base64.to_string();
        let phrase = phrase.map(ToString::to_string);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let mut params = json!({
                    "requestId": request_id,
                    "responseCode": status,
                    "responseHeaders": headers,
                    "body": body,
                });
                if let Some(phrase) = phrase {
                    params["responsePhrase"] = Value::String(phrase);
                }
                client
                    .send("Fetch.fulfillRequest", params, Some(&session_id), timeout)
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (timeout_ms=None, run_before_unload=false))]
    fn close(&self, timeout_ms: Option<f64>, run_before_unload: bool) -> PyResult<()> {
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        browser
            .block_on(page_close_async(page, timeout, run_before_unload))
            .map_err(py_err)
    }

    fn mark_delivered(&self) {
        self.inner.mark_delivered();
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyWorker {
    #[getter]
    fn url(&self) -> String {
        self.url.clone()
    }

    #[getter]
    fn target_id(&self) -> String {
        self.target_id.clone()
    }

    #[pyo3(signature = (expression, arg_json=None, timeout_ms=None))]
    fn evaluate(
        &self,
        py: Python<'_>,
        expression: &str,
        arg_json: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let expression = make_evaluate_expression(expression, arg_json);
        let browser = Arc::clone(&self.browser);
        let client = Arc::clone(&browser.client);
        let session_id = self.session_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        py.detach(move || {
            browser.block_on(async move {
                let result = client
                    .send(
                        "Runtime.evaluate",
                        json!({
                            "expression": expression,
                            "awaitPromise": true,
                            "returnByValue": false,
                            "userGesture": true,
                        }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                runtime_result_to_json_with_serializer(&client, &session_id, &result, timeout).await
            })
        })
        .map_err(py_err)
    }

    #[pyo3(signature = (expression, arg_json=None, timeout_ms=None))]
    fn evaluate_handle(
        &self,
        py: Python<'_>,
        expression: &str,
        arg_json: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let expression = make_evaluate_expression(expression, arg_json);
        let browser = Arc::clone(&self.browser);
        let client = Arc::clone(&browser.client);
        let session_id = self.session_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        py.detach(move || {
            browser.block_on(async move {
                let result = client
                    .send(
                        "Runtime.evaluate",
                        json!({
                            "expression": expression,
                            "awaitPromise": true,
                            "returnByValue": false,
                            "userGesture": true,
                        }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                runtime_result_to_remote_object(&result)
            })
        })
        .map_err(py_err)
    }

    #[pyo3(signature = (function_declaration, arguments_json, return_by_value=true, timeout_ms=None))]
    fn evaluate_with_call_arguments(
        &self,
        py: Python<'_>,
        function_declaration: &str,
        arguments_json: &str,
        return_by_value: bool,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let arguments = serde_json::from_str::<Value>(arguments_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let global_payload = self
            .evaluate_handle(py, "globalThis", None, timeout_ms)
            .map_err(|error| error)?;
        let global = serde_json::from_str::<Value>(&global_payload)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let object_id = global
            .get("objectId")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                PyRuntimeError::new_err("CDP did not return a worker global object handle")
            })?;
        let result = self.call_function_on_handle(
            object_id,
            function_declaration,
            Some(arguments),
            return_by_value,
            timeout_ms,
        );
        let _ = self.js_handle_dispose(object_id, timeout_ms);
        result.map_err(py_err)
    }

    #[pyo3(signature = (object_id, timeout_ms=None))]
    fn js_handle_json_value(&self, object_id: &str, timeout_ms: Option<f64>) -> PyResult<String> {
        self.call_function_on_handle(
            object_id,
            "function() { return this; }",
            None,
            true,
            timeout_ms,
        )
        .map_err(py_err)
    }

    #[pyo3(signature = (object_id, name, timeout_ms=None))]
    fn js_handle_get_property(
        &self,
        object_id: &str,
        name: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let name = json!(name);
        self.call_function_on_handle(
            object_id,
            "function(name) { return this == null ? undefined : this[name]; }",
            Some(json!([{ "value": name }])),
            false,
            timeout_ms,
        )
        .map_err(py_err)
    }

    #[pyo3(signature = (object_id, timeout_ms=None))]
    fn js_handle_get_properties(
        &self,
        object_id: &str,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let browser = Arc::clone(&self.browser);
        let client = Arc::clone(&browser.client);
        let session_id = self.session_id.clone();
        let object_id = object_id.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        browser
            .block_on(async move {
                let result = client
                    .send(
                        "Runtime.getProperties",
                        json!({
                            "objectId": object_id,
                            "ownProperties": true,
                            "accessorPropertiesOnly": false,
                        }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                let mut properties = serde_json::Map::new();
                if let Some(items) = result.get("result").and_then(Value::as_array) {
                    for item in items {
                        if !item
                            .get("enumerable")
                            .and_then(Value::as_bool)
                            .unwrap_or(false)
                        {
                            continue;
                        }
                        let Some(name) = item.get("name").and_then(Value::as_str) else {
                            continue;
                        };
                        let Some(value) = item.get("value") else {
                            continue;
                        };
                        properties.insert(name.to_string(), value.clone());
                    }
                }
                Ok(Value::Object(properties).to_string())
            })
            .map_err(py_err)
    }

    #[pyo3(signature = (object_id, expression, arg_json=None, return_by_value=true, timeout_ms=None))]
    fn js_handle_evaluate(
        &self,
        object_id: &str,
        expression: &str,
        arg_json: Option<&str>,
        return_by_value: bool,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let function_declaration = format!(
            "function(arg) {{ const __rw_fn = ({}); return __rw_fn(this, arg); }}",
            expression.trim()
        );
        let arguments = if let Some(arg_json) = arg_json {
            Some(
                json!([{ "value": serde_json::from_str::<Value>(arg_json).map_err(RwError::from).map_err(py_err)? }]),
            )
        } else {
            None
        };
        self.call_function_on_handle(
            object_id,
            &function_declaration,
            arguments,
            return_by_value,
            timeout_ms,
        )
        .map_err(py_err)
    }

    #[pyo3(signature = (object_id, function_declaration, arguments_json, return_by_value=true, timeout_ms=None))]
    fn js_handle_evaluate_with_call_arguments(
        &self,
        object_id: &str,
        function_declaration: &str,
        arguments_json: &str,
        return_by_value: bool,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let arguments = serde_json::from_str::<Value>(arguments_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        self.call_function_on_handle(
            object_id,
            function_declaration,
            Some(arguments),
            return_by_value,
            timeout_ms,
        )
        .map_err(py_err)
    }

    #[pyo3(signature = (object_id, timeout_ms=None))]
    fn js_handle_dispose(&self, object_id: &str, timeout_ms: Option<f64>) -> PyResult<()> {
        let browser = Arc::clone(&self.browser);
        let client = Arc::clone(&browser.client);
        let session_id = self.session_id.clone();
        let object_id = object_id.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        browser
            .block_on(async move {
                client
                    .send(
                        "Runtime.releaseObject",
                        json!({ "objectId": object_id }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                Ok(())
            })
            .map_err(py_err)
    }

    fn close_event_waiter(&self) -> PyWorkerCloseEventWaiter {
        PyWorkerCloseEventWaiter {
            browser: Arc::clone(&self.browser),
            receiver: Mutex::new(Some(self.browser.client.subscribe())),
            target_id: self.target_id.clone(),
            session_id: self.session_id.clone(),
        }
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn console_event_waiter(&self, timeout_ms: Option<f64>) -> PyResult<PyConsoleEventWaiter> {
        let browser = Arc::clone(&self.browser);
        let client = Arc::clone(&browser.client);
        let session_id = self.session_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        browser
            .block_on(async move {
                client
                    .send("Runtime.enable", json!({}), Some(&session_id), timeout)
                    .await?;
                Ok(())
            })
            .map_err(py_err)?;
        Ok(PyConsoleEventWaiter {
            browser: Arc::clone(&self.browser),
            receiver: Mutex::new(Some(self.browser.client.subscribe())),
            session_id: self.session_id.clone(),
        })
    }
}

#[cfg(feature = "python")]
impl PyWorker {
    fn call_function_on_handle(
        &self,
        object_id: &str,
        function_declaration: &str,
        arguments: Option<Value>,
        return_by_value: bool,
        timeout_ms: Option<f64>,
    ) -> RwResult<String> {
        let browser = Arc::clone(&self.browser);
        let client = Arc::clone(&browser.client);
        let session_id = self.session_id.clone();
        let object_id = object_id.to_string();
        let function_declaration = function_declaration.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        browser.block_on(async move {
            let mut params = json!({
                "objectId": object_id,
                "functionDeclaration": function_declaration,
                "awaitPromise": true,
                "returnByValue": false,
                "userGesture": true,
            });
            if let Some(arguments) = arguments {
                params["arguments"] = arguments;
            }
            let result = client
                .send("Runtime.callFunctionOn", params, Some(&session_id), timeout)
                .await?;
            if return_by_value {
                runtime_result_to_json_with_serializer(&client, &session_id, &result, timeout).await
            } else {
                runtime_result_to_remote_object(&result)
            }
        })
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyNetworkEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<String> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("network waiter is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let session_id = self.session_id.clone();
        let kind = self.kind.clone();
        let requests = Arc::clone(&self.requests);
        let event_log = Arc::clone(&self.event_log);
        let cursor = *self.cursor.lock().unwrap();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver, cursor) = py.detach(move || {
            let (result, cursor) = browser.block_on_raw(wait_for_network_event(
                &mut receiver,
                event_log,
                cursor,
                &session_id,
                &kind,
                requests,
                timeout,
            ));
            (result, receiver, cursor)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        *self.cursor.lock().unwrap() = cursor;
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
impl PyPageEventStream {
    fn take_pending_batch(&self) -> Option<PendingPageEventBatch> {
        self.pending_batch.lock().unwrap().take()
    }

    fn rollback_pending_batch(&self) {
        drop(self.take_pending_batch());
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyPageEventStream {
    fn ack_batch(&self) {
        if let Some(batch) = self.take_pending_batch() {
            batch.acknowledge();
        }
    }

    fn rollback_batch(&self) {
        self.rollback_pending_batch();
    }

    fn enable_runtime(&self) -> PyResult<()> {
        if self.runtime_enabled.swap(true, Ordering::SeqCst) {
            return Ok(());
        }
        let browser = Arc::clone(&self.browser);
        let client = Arc::clone(&browser.client);
        let session_id = self.session_id.clone();
        if let Err(error) = browser.block_on(async move {
            client
                .send(
                    "Runtime.enable",
                    json!({}),
                    Some(&session_id),
                    Duration::from_secs(5),
                )
                .await
                .map(|_| ())
        }) {
            self.runtime_enabled.store(false, Ordering::SeqCst);
            return Err(py_err(error));
        }
        Ok(())
    }

    fn enable_runtime_async(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if self.runtime_enabled.swap(true, Ordering::SeqCst) {
            let asyncio = PyModule::import(py, "asyncio")?;
            let event_loop = asyncio.call_method0("get_running_loop")?;
            let future = event_loop.call_method0("create_future")?;
            future.call_method1("set_result", (py.None(),))?;
            return Ok(future.unbind());
        }
        let browser = Arc::clone(&self.browser);
        let runtime = browser.runtime.handle().clone();
        let client = Arc::clone(&browser.client);
        let session_id = self.session_id.clone();
        let runtime_enabled = Arc::clone(&self.runtime_enabled);
        python_future_on(
            py,
            runtime,
            async move {
                let result = client
                    .send(
                        "Runtime.enable",
                        json!({}),
                        Some(&session_id),
                        Duration::from_secs(5),
                    )
                    .await
                    .map(|_| ());
                if result.is_err() {
                    runtime_enabled.store(false, Ordering::SeqCst);
                }
                result
            },
            |py, ()| Ok(py.None()),
        )
    }

    #[pyo3(signature = (timeout_ms=None, max_events=64))]
    fn wait_batch(
        &self,
        py: Python<'_>,
        timeout_ms: Option<f64>,
        max_events: usize,
    ) -> PyResult<String> {
        self.rollback_pending_batch();
        if max_events == 0 {
            return Err(PyValueError::new_err(
                "max_events must be greater than zero",
            ));
        }
        if self.closed.load(Ordering::SeqCst) {
            let cursor = *self.cursor.lock().unwrap();
            return Ok(
                Value::Array(vec![page_event_envelope(cursor, "_closed", Value::Null)]).to_string(),
            );
        }
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("page event stream is already waiting"))?;
        let mut state = self
            .state
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("page event stream is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let event_log = Arc::clone(&self.event_log);
        let mut cursor = *self.cursor.lock().unwrap();
        let session_id = self.session_id.clone();
        let requests = Arc::clone(&self.requests);
        let close_rx = self.close_tx.subscribe();
        let alive_rx = browser.client.alive_tx.subscribe();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (batch, cursor, state, terminal, receiver) = py.detach(move || {
            let (batch, terminal) = browser.block_on_raw(wait_for_page_event_batch(
                &mut receiver,
                event_log,
                &mut cursor,
                &session_id,
                requests,
                &mut state,
                close_rx,
                alive_rx,
                timeout,
                max_events,
            ));
            (batch, cursor, state, terminal, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        *self.cursor.lock().unwrap() = cursor;
        *self.state.lock().unwrap() = Some(state);
        if terminal {
            self.closed.store(true, Ordering::SeqCst);
        }
        Ok(Value::Array(batch).to_string())
    }

    #[pyo3(signature = (timeout_ms=None, max_events=64))]
    fn wait_batch_async(
        &self,
        py: Python<'_>,
        timeout_ms: Option<f64>,
        max_events: usize,
    ) -> PyResult<Py<PyAny>> {
        self.rollback_pending_batch();
        if max_events == 0 {
            return Err(PyValueError::new_err(
                "max_events must be greater than zero",
            ));
        }
        if self.closed.load(Ordering::SeqCst) {
            let cursor = *self.cursor.lock().unwrap();
            let payload =
                Value::Array(vec![page_event_envelope(cursor, "_closed", Value::Null)]).to_string();
            let asyncio = PyModule::import(py, "asyncio")?;
            let event_loop = asyncio.call_method0("get_running_loop")?;
            let future = event_loop.call_method0("create_future")?;
            future.call_method1("set_result", (payload,))?;
            return Ok(future.unbind());
        }
        let receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("page event stream is already waiting"))?;
        let state = self
            .state
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("page event stream is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let runtime = browser.runtime.handle().clone();
        let event_log = Arc::clone(&self.event_log);
        let cursor = *self.cursor.lock().unwrap();
        let session_id = self.session_id.clone();
        let close_rx = self.close_tx.subscribe();
        let alive_rx = browser.client.alive_tx.subscribe();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let working_requests = Arc::new(Mutex::new(self.requests.lock().unwrap().clone()));
        let mut lease = PageEventStreamLease {
            receiver: Some(receiver),
            rollback_state: Some(state.clone()),
            state: Some(state),
            cursor,
            rollback_cursor: cursor,
            delivered: false,
            receiver_slot: Arc::clone(&self.receiver),
            state_slot: Arc::clone(&self.state),
            cursor_slot: Arc::clone(&self.cursor),
            requests: Arc::clone(&self.requests),
            working_requests: Arc::clone(&working_requests),
        };
        let closed = Arc::clone(&self.closed);
        let pending_batch = Arc::clone(&self.pending_batch);
        python_future_on_with_delivery(
            py,
            runtime,
            async move {
                let (batch, terminal) = wait_for_page_event_batch(
                    lease.receiver.as_mut().unwrap(),
                    event_log,
                    &mut lease.cursor,
                    &session_id,
                    working_requests,
                    lease.state.as_mut().unwrap(),
                    close_rx,
                    alive_rx,
                    timeout,
                    max_events,
                )
                .await;
                Ok((Value::Array(batch).to_string(), lease, terminal))
            },
            move |py, (payload, lease, terminal)| {
                let value = payload.into_pyobject(py)?.unbind().into_any();
                Ok(PythonFutureOutput {
                    value,
                    on_delivered: Some(Box::new(move || {
                        *pending_batch.lock().unwrap() = Some(PendingPageEventBatch {
                            lease,
                            terminal,
                            closed,
                        });
                    })),
                })
            },
        )
    }

    fn close(&self) {
        self.rollback_pending_batch();
        self.closed.store(true, Ordering::SeqCst);
        self.close_tx.send_replace(true);
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyRouteEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<String> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("route waiter is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let session_id = self.session_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let result =
                browser.block_on_raw(wait_for_route_event(&mut receiver, &session_id, timeout));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyAuthEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<String> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("auth waiter is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let session_id = self.session_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let result =
                browser.block_on_raw(wait_for_auth_event(&mut receiver, &session_id, timeout));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyDialogEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<String> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("dialog waiter is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let session_id = self.session_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let result =
                browser.block_on_raw(wait_for_dialog_event(&mut receiver, &session_id, timeout));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyConsoleEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<String> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("console waiter is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let session_id = self.session_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let result =
                browser.block_on_raw(wait_for_console_event(&mut receiver, &session_id, timeout));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyWebSocketEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<String> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("websocket waiter is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let session_id = self.session_id.clone();
        let kind = self.kind.clone();
        let request_id = self.request_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let result = browser.block_on_raw(wait_for_websocket_event(
                &mut receiver,
                &session_id,
                &kind,
                request_id.as_deref(),
                timeout,
            ));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyBindingEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<String> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("binding waiter is already waiting"))?;
        let page = Arc::clone(&self.page);
        let name = self.name.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let browser = Arc::clone(&page.browser);
            let result = browser.block_on_raw(wait_for_binding_event_for_page(
                &mut receiver,
                &page,
                &name,
                timeout,
            ));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyDownloadEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<String> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("download waiter is already waiting"))?;
        let mut active_downloads = std::mem::take(&mut *self.active_downloads.lock().unwrap());
        let browser = Arc::clone(&self.browser);
        let download_path = self.download_path.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver, active_downloads) = py.detach(move || {
            let result = browser.block_on_raw(wait_for_download_event(
                &mut receiver,
                &download_path,
                &mut active_downloads,
                timeout,
            ));
            (result, receiver, active_downloads)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        *self.active_downloads.lock().unwrap() = active_downloads;
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyFileChooserEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<String> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("file chooser waiter is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let session_id = self.session_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let result = browser.block_on_raw(wait_for_file_chooser_event(
                &mut receiver,
                &session_id,
                timeout,
            ));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyPopupEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<PyPage> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("popup waiter is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let opener_target_id = self.opener_target_id.clone();
        let seen_target_ids = Arc::clone(&self.seen_target_ids);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let browser_for_wait = Arc::clone(&browser);
            let result = browser.block_on_raw(wait_for_popup_page(
                &mut receiver,
                browser_for_wait,
                &opener_target_id,
                seen_target_ids,
                timeout,
            ));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyWorkerEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<PyWorker> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("worker waiter is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let opener_target_id = self.opener_target_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let browser_for_wait = Arc::clone(&browser);
            let result = browser.block_on_raw(wait_for_worker(
                &mut receiver,
                browser_for_wait,
                &opener_target_id,
                timeout,
            ));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyWorkerCloseEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<()> {
        let mut receiver = self
            .receiver
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("worker close waiter is already waiting"))?;
        let browser = Arc::clone(&self.browser);
        let target_id = self.target_id.clone();
        let session_id = self.session_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let result = browser.block_on_raw(wait_for_worker_close(
                &mut receiver,
                &target_id,
                &session_id,
                timeout,
            ));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyServiceWorkerEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<PyWorker> {
        let mut receiver =
            self.receiver.lock().unwrap().take().ok_or_else(|| {
                PyRuntimeError::new_err("service worker waiter is already waiting")
            })?;
        let browser = Arc::clone(&self.browser);
        let context_id = self.context_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let browser_for_wait = Arc::clone(&browser);
            let result = browser.block_on_raw(wait_for_service_worker(
                &mut receiver,
                browser_for_wait,
                context_id.as_deref(),
                timeout,
            ));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl PyBackgroundPageEventWaiter {
    #[pyo3(signature = (timeout_ms=None))]
    fn wait(&self, py: Python<'_>, timeout_ms: Option<f64>) -> PyResult<PyPage> {
        let mut receiver =
            self.receiver.lock().unwrap().take().ok_or_else(|| {
                PyRuntimeError::new_err("background page waiter is already waiting")
            })?;
        let browser = Arc::clone(&self.browser);
        let context_id = self.context_id.clone();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let (result, receiver) = py.detach(move || {
            let browser_for_wait = Arc::clone(&browser);
            let result = browser.block_on_raw(wait_for_background_page(
                &mut receiver,
                browser_for_wait,
                context_id.as_deref(),
                timeout,
            ));
            (result, receiver)
        });
        *self.receiver.lock().unwrap() = Some(receiver);
        result.map_err(py_err)
    }
}

#[cfg(feature = "python")]
impl PyPage {
    fn navigate_history(
        &self,
        offset: i64,
        wait_until: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> PyResult<String> {
        let page = Arc::clone(&self.inner);
        let wait_until = wait_until.unwrap_or("load").to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser
            .block_on(async move {
                let history = client
                    .send(
                        "Page.getNavigationHistory",
                        json!({}),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                let current_index = history
                    .get("currentIndex")
                    .and_then(Value::as_i64)
                    .ok_or_else(|| {
                        RwError::Message(
                            "CDP did not return a navigation history index".to_string(),
                        )
                    })?;
                let target_index = current_index + offset;
                let entries = history
                    .get("entries")
                    .and_then(Value::as_array)
                    .ok_or_else(|| {
                        RwError::Message(
                            "CDP did not return navigation history entries".to_string(),
                        )
                    })?;
                if target_index < 0 || target_index as usize >= entries.len() {
                    return Ok(Value::Null.to_string());
                }
                let entry_id = entries[target_index as usize]
                    .get("id")
                    .and_then(Value::as_i64)
                    .ok_or_else(|| {
                        RwError::Message(
                            "CDP did not return a navigation history entry id".to_string(),
                        )
                    })?;
                let target_url = entries[target_index as usize]
                    .get("url")
                    .and_then(Value::as_str)
                    .map(ToString::to_string);
                let mut events = client.subscribe();
                client
                    .send(
                        "Page.navigateToHistoryEntry",
                        json!({ "entryId": entry_id }),
                        Some(&session_id),
                        timeout,
                    )
                    .await?;
                let response = wait_for_navigation(
                    &mut events,
                    &session_id,
                    &wait_until,
                    None,
                    target_url.as_deref(),
                    if offset < 0 {
                        "Page.go_back"
                    } else {
                        "Page.go_forward"
                    },
                    timeout,
                )
                .await?;
                Ok(response.unwrap_or(Value::Null).to_string())
            })
            .map_err(py_err)
    }

    fn evaluate_expression(&self, expression: &str, timeout_ms: Option<f64>) -> RwResult<String> {
        evaluate_expression_for_page(Arc::clone(&self.inner), expression.to_string(), timeout_ms)
    }

    fn evaluate_handle_expression(
        &self,
        expression: &str,
        timeout_ms: Option<f64>,
    ) -> RwResult<String> {
        let page = Arc::clone(&self.inner);
        let expression = expression.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser.block_on(async move {
            let result = client
                .send(
                    "Runtime.evaluate",
                    json!({
                        "expression": expression,
                        "awaitPromise": true,
                        "returnByValue": false,
                        "userGesture": true,
                    }),
                    Some(&session_id),
                    timeout,
                )
                .await?;
            runtime_result_to_remote_object_with_session(&result, &session_id)
        })
    }

    fn call_function_on_handle(
        &self,
        object_id: &str,
        function_declaration: &str,
        arguments: Option<Value>,
        return_by_value: bool,
        timeout_ms: Option<f64>,
        session_id: Option<&str>,
    ) -> RwResult<String> {
        let page = Arc::clone(&self.inner);
        let object_id = object_id.to_string();
        let function_declaration = function_declaration.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = session_id
            .map(ToString::to_string)
            .unwrap_or_else(|| page.session_id.clone());
        browser.block_on(async move {
            let mut params = json!({
                "objectId": object_id,
                "functionDeclaration": function_declaration,
                "awaitPromise": true,
                "returnByValue": false,
                "userGesture": true,
            });
            if let Some(arguments) = arguments {
                params["arguments"] = arguments;
            }
            let result = client
                .send("Runtime.callFunctionOn", params, Some(&session_id), timeout)
                .await?;
            if return_by_value {
                runtime_result_to_json_with_serializer(&client, &session_id, &result, timeout).await
            } else {
                runtime_result_to_remote_object_with_session(&result, &session_id)
            }
        })
    }

    fn evaluate_locator(
        &self,
        locator_json: &str,
        index: usize,
        body: &str,
        timeout_ms: Option<f64>,
    ) -> RwResult<String> {
        let page = Arc::clone(&self.inner);
        let locator_json = locator_json.to_string();
        let body = body.to_string();
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        browser.block_on(async move {
            evaluate_locator_for_page(page, locator_json, index, body, timeout).await
        })
    }
}

fn launch_chromium_with_options(options: LaunchOptions) -> RwResult<Arc<BrowserInner>> {
    launch_chromium_with_options_cancelable(options, None)
}

fn launch_chromium_with_options_cancelable(
    options: LaunchOptions,
    cancelled: Option<Arc<AtomicBool>>,
) -> RwResult<Arc<BrowserInner>> {
    launch_chromium_with_options_cancellation(options, cancelled, None)
}

fn launch_chromium_with_options_token(
    options: LaunchOptions,
    cancel: CancelToken,
) -> RwResult<Arc<BrowserInner>> {
    launch_chromium_with_options_cancellation(options, Some(cancel.atomic_flag()), Some(cancel))
}

fn launch_chromium_with_options_cancellation(
    mut options: LaunchOptions,
    cancelled: Option<Arc<AtomicBool>>,
    cancel: Option<CancelToken>,
) -> RwResult<Arc<BrowserInner>> {
    if options.timeout.is_none() {
        options.timeout = Some(30_000.0);
    }
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .worker_threads(2)
        .build()
        .map_err(|error| RwError::Message(error.to_string()))?;
    let timeout = BrowserInner::command_timeout(options.timeout);
    let (mut child, profile_dir, transport, single_process_fallback) =
        launch_chromium_process(&options, &runtime, timeout, cancelled.clone())?;
    let ws_endpoint = transport.endpoint_label();
    let client_result = match transport {
        LaunchedCdpTransport::WebSocket(endpoint) => runtime.block_on(async {
            if let Some(cancelled) = cancelled.clone() {
                tokio::select! {
                    result = CdpClient::connect(&endpoint) => result,
                    () = wait_for_launch_cancellation(cancelled) => {
                        Err(RwError::Message("browser launch was cancelled".to_string()))
                    }
                }
            } else {
                CdpClient::connect(&endpoint).await
            }
        }),
        #[cfg(unix)]
        LaunchedCdpTransport::Pipe { read, write } => {
            runtime.block_on(CdpClient::connect_pipe(read, write))
        }
    };
    let client = match client_result {
        Ok(client) => client,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
    };
    if let Err(error) = start_service_worker_stealth_auto_attach_cancelable(
        &runtime,
        Arc::clone(&client),
        Duration::from_secs(5),
        cancel,
    ) {
        client.close();
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    let browser = Arc::new(BrowserInner {
        runtime: OwnedRuntime::new(runtime),
        client,
        process: Mutex::new(Some(child)),
        profile_dir: Mutex::new(profile_dir),
        owned: true,
        ws_endpoint,
        stealth_user_agent_override: Mutex::new(None),
        single_process_fallback,
        lifecycle: Arc::new(CloseLifecycle::new()),
        attached_pages: AttachedPageRegistry::default(),
    });
    if launch_was_cancelled(cancelled.as_ref()) {
        let _ = close_browser_blocking(Arc::clone(&browser));
        return Err(RwError::Message("browser launch was cancelled".to_string()));
    }
    Ok(browser)
}

fn launch_was_cancelled(cancelled: Option<&Arc<AtomicBool>>) -> bool {
    cancelled
        .map(|cancelled| cancelled.load(Ordering::SeqCst))
        .unwrap_or(false)
}

async fn wait_for_launch_cancellation(cancelled: Arc<AtomicBool>) {
    while !cancelled.load(Ordering::SeqCst) {
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

#[cfg(feature = "python")]
#[pyfunction]
fn launch_chromium(py: Python<'_>, options_json: &str) -> PyResult<PyBrowser> {
    let options: LaunchOptions = serde_json::from_str(options_json)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    let inner = py
        .detach(move || launch_chromium_with_options(options))
        .map_err(py_err)?;
    Ok(PyBrowser { inner })
}

#[cfg(feature = "python")]
#[pyfunction]
fn launch_chromium_async(py: Python<'_>, options_json: &str) -> PyResult<Py<PyAny>> {
    let options: LaunchOptions = serde_json::from_str(options_json)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    python_future_on_thread(
        py,
        move |cancelled| {
            Ok(PyBrowser {
                inner: launch_chromium_with_options_cancelable(options, Some(cancelled))?,
            })
        },
        |py, browser| Ok(Py::new(py, browser)?.into_any()),
    )
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(signature = (endpoint, timeout_ms=None, headers_json=None))]
fn connect_over_cdp(
    py: Python<'_>,
    endpoint: &str,
    timeout_ms: Option<f64>,
    headers_json: Option<&str>,
) -> PyResult<PyBrowser> {
    let timeout = BrowserInner::command_timeout(timeout_ms);
    let headers = parse_header_pairs(headers_json).map_err(py_err)?;
    let endpoint = endpoint.to_string();
    let inner = py
        .detach(move || connect_browser_over_cdp(endpoint, headers, timeout))
        .map_err(py_err)?;
    Ok(PyBrowser { inner })
}

fn connect_browser_over_cdp(
    endpoint: String,
    headers: Vec<(String, String)>,
    timeout: Duration,
) -> RwResult<Arc<BrowserInner>> {
    connect_browser_over_cdp_cancelable(endpoint, headers, timeout, None)
}

fn connect_browser_over_cdp_cancelable(
    endpoint: String,
    headers: Vec<(String, String)>,
    timeout: Duration,
    cancel: Option<CancelToken>,
) -> RwResult<Arc<BrowserInner>> {
    let started = Instant::now();
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .worker_threads(2)
        .build()
        .map_err(|error| RwError::Message(error.to_string()))?;
    let connect = async {
        let ws_endpoint = resolve_ws_endpoint(&endpoint, timeout, &headers).await?;
        let client = CdpClient::connect_with_headers(&ws_endpoint, &headers).await?;
        Ok::<_, RwError>((ws_endpoint, client))
    };
    let (ws_endpoint, client) = runtime.block_on(cancelable(cancel.clone(), async {
        tokio::time::timeout(timeout, connect)
            .await
            .map_err(|_| RwError::Timeout(duration_millis_u64(timeout)))?
    }))?;
    let remaining = timeout.saturating_sub(started.elapsed());
    if remaining.is_zero() {
        client.close();
        return Err(RwError::Timeout(duration_millis_u64(timeout)));
    }
    if let Err(error) = start_service_worker_stealth_auto_attach_cancelable(
        &runtime,
        Arc::clone(&client),
        remaining,
        cancel,
    ) {
        client.close();
        return Err(error);
    }
    Ok(Arc::new(BrowserInner {
        runtime: OwnedRuntime::new(runtime),
        client,
        process: Mutex::new(None),
        profile_dir: Mutex::new(None),
        owned: false,
        ws_endpoint,
        stealth_user_agent_override: Mutex::new(None),
        single_process_fallback: false,
        lifecycle: Arc::new(CloseLifecycle::new()),
        attached_pages: AttachedPageRegistry::default(),
    }))
}

fn duration_millis_u64(duration: Duration) -> u64 {
    duration.as_millis().min(u128::from(u64::MAX)) as u64
}

#[cfg(feature = "python")]
#[pyfunction]
fn chromium_executable_path() -> PyResult<Option<String>> {
    Ok(find_chromium_executable(None, None, true).map(|path| path.to_string_lossy().to_string()))
}

#[derive(Clone)]
pub struct RustwrightBrowser {
    inner: Arc<BrowserInner>,
}

#[derive(Clone)]
pub struct RustwrightPage {
    inner: Arc<PageInner>,
}

const NATIVE_PAGE_EVENT_QUEUE_CAPACITY: usize = 128;

/// The JavaScript dialog category reported by Chromium.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RustwrightDialogKind {
    Alert,
    Confirm,
    Prompt,
    BeforeUnload,
    Other(String),
}

/// A pending JavaScript dialog that can be accepted or dismissed.
#[derive(Clone)]
pub struct RustwrightDialog {
    browser: Weak<BrowserInner>,
    session_id: String,
}

impl std::fmt::Debug for RustwrightDialog {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RustwrightDialog")
            .field("session_id", &self.session_id)
            .finish_non_exhaustive()
    }
}

impl RustwrightDialog {
    /// Accept the dialog, optionally supplying text for a prompt dialog.
    pub fn accept(&self, prompt_text: Option<&str>) -> RwResult<()> {
        self.handle(true, prompt_text)
    }

    /// Dismiss the dialog.
    pub fn dismiss(&self) -> RwResult<()> {
        self.handle(false, None)
    }

    fn handle(&self, accept: bool, prompt_text: Option<&str>) -> RwResult<()> {
        let browser = self.browser.upgrade().ok_or(RwError::Closed)?;
        let client = Arc::clone(&browser.client);
        let session_id = self.session_id.clone();
        let prompt_text = prompt_text.map(ToString::to_string);
        browser.block_on_raw(async move {
            let mut params = json!({ "accept": accept });
            if let Some(prompt_text) = prompt_text {
                params["promptText"] = Value::String(prompt_text);
            }
            client
                .send(
                    "Page.handleJavaScriptDialog",
                    params,
                    Some(&session_id),
                    Duration::from_secs(30),
                )
                .await
                .map(|_| ())
        })
    }
}

/// A typed event emitted by a native page subscription.
#[derive(Clone, Debug)]
pub enum RustwrightPageEvent {
    Dialog {
        kind: RustwrightDialogKind,
        message: String,
        dialog: RustwrightDialog,
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

struct NativePageEventQueueState {
    events: VecDeque<RustwrightPageEvent>,
    dropped: u64,
    closed: bool,
}

struct NativePageEventQueue {
    state: Mutex<NativePageEventQueueState>,
    changed: Condvar,
}

impl NativePageEventQueue {
    fn new() -> Self {
        Self {
            state: Mutex::new(NativePageEventQueueState {
                events: VecDeque::with_capacity(NATIVE_PAGE_EVENT_QUEUE_CAPACITY),
                dropped: 0,
                closed: false,
            }),
            changed: Condvar::new(),
        }
    }

    fn push(&self, event: RustwrightPageEvent, terminal: bool) {
        let mut state = self.state.lock().unwrap();
        if state.events.len() == NATIVE_PAGE_EVENT_QUEUE_CAPACITY {
            state.events.pop_front();
            state.dropped = state.dropped.saturating_add(1);
        }
        state.events.push_back(event);
        state.closed |= terminal;
        self.changed.notify_all();
    }

    fn record_upstream_drop(&self, count: u64) {
        let mut state = self.state.lock().unwrap();
        state.dropped = state.dropped.saturating_add(count);
    }

    fn close(&self) {
        let mut state = self.state.lock().unwrap();
        state.closed = true;
        self.changed.notify_all();
    }
}

/// Pull-based receiver for a page's bounded native event queue.
pub struct RustwrightPageEventReceiver {
    queue: Arc<NativePageEventQueue>,
    close_tx: watch::Sender<bool>,
}

impl RustwrightPageEventReceiver {
    /// Wait for the next event, returning `None` on timeout or after terminal delivery.
    pub fn recv_timeout(&self, timeout: Duration) -> Option<RustwrightPageEvent> {
        let deadline = Instant::now() + timeout;
        let mut state = self.queue.state.lock().unwrap();
        loop {
            if let Some(event) = state.events.pop_front() {
                return Some(event);
            }
            if state.closed {
                return None;
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return None;
            }
            let (next, wait) = self.queue.changed.wait_timeout(state, remaining).unwrap();
            state = next;
            if wait.timed_out() && state.events.is_empty() {
                return None;
            }
        }
    }

    /// Return the number of events discarded because a bounded queue lagged.
    pub fn dropped_count(&self) -> u64 {
        self.queue.state.lock().unwrap().dropped
    }

    /// Return the fixed maximum number of typed events buffered by this receiver.
    pub const fn capacity(&self) -> usize {
        NATIVE_PAGE_EVENT_QUEUE_CAPACITY
    }
}

impl Drop for RustwrightPageEventReceiver {
    fn drop(&mut self) {
        self.close_tx.send_replace(true);
    }
}

pub fn rustwright_launch_chromium(options_json: &str) -> RwResult<RustwrightBrowser> {
    rustwright_launch_chromium_with_cancel(options_json, None)
}

/// Launch Chromium with an optional cancellation signal.
pub fn rustwright_launch_chromium_with_cancel(
    options_json: &str,
    cancel: Option<&CancelToken>,
) -> RwResult<RustwrightBrowser> {
    let options: LaunchOptions = serde_json::from_str(options_json)?;
    let result = match cancel {
        Some(cancel) => launch_chromium_with_options_token(options, cancel.clone()),
        None => launch_chromium_with_options(options),
    };
    match result {
        Ok(inner) => Ok(RustwrightBrowser { inner }),
        Err(_) if cancel.is_some_and(CancelToken::is_cancelled) => Err(RwError::Cancelled),
        Err(error) => Err(error),
    }
}

/// Attach to an existing Chromium CDP endpoint without taking process ownership.
///
/// Connection failures are deliberately sanitized so endpoints, query strings, and
/// header values cannot escape through the public error surface.
pub fn rustwright_connect_over_cdp(
    endpoint: &str,
    headers: &[(String, String)],
    timeout: Duration,
) -> RwResult<RustwrightBrowser> {
    rustwright_connect_over_cdp_with_cancel(endpoint, headers, timeout, None)
}

/// Attach to a CDP endpoint with an optional cancellation signal.
pub fn rustwright_connect_over_cdp_with_cancel(
    endpoint: &str,
    headers: &[(String, String)],
    timeout: Duration,
    cancel: Option<&CancelToken>,
) -> RwResult<RustwrightBrowser> {
    if timeout.is_zero() {
        return Err(RwError::InvalidInput(
            "CDP timeout must be greater than zero".to_string(),
        ));
    }
    if !["ws://", "wss://", "http://", "https://"]
        .iter()
        .any(|scheme| endpoint.starts_with(scheme))
    {
        return Err(RwError::InvalidInput(
            "CDP endpoint must use ws, wss, http, or https".to_string(),
        ));
    }
    for (name, value) in headers {
        if HeaderName::from_bytes(name.as_bytes()).is_err() || HeaderValue::from_str(value).is_err()
        {
            return Err(RwError::InvalidInput(
                "CDP headers contain an invalid name or value".to_string(),
            ));
        }
    }
    match connect_browser_over_cdp_cancelable(
        endpoint.to_string(),
        headers.to_vec(),
        timeout,
        cancel.cloned(),
    ) {
        Ok(inner) => Ok(RustwrightBrowser { inner }),
        Err(error @ (RwError::Timeout(_) | RwError::Cancelled | RwError::InvalidInput(_))) => {
            Err(error)
        }
        Err(_) => Err(RwError::ConnectFailed),
    }
}

pub fn rustwright_chromium_executable_path() -> Option<String> {
    find_chromium_executable(None, None, true).map(|path| path.to_string_lossy().to_string())
}

impl RustwrightBrowser {
    pub fn new_page(&self) -> RwResult<RustwrightPage> {
        self.new_page_with_cancel(None)
    }

    pub fn new_page_with_cancel(&self, cancel: Option<&CancelToken>) -> RwResult<RustwrightPage> {
        let inner = create_page_raw_cancelable(Arc::clone(&self.inner), None, cancel)?;
        inner.mark_delivered();
        Ok(RustwrightPage { inner })
    }

    pub fn close(&self) -> RwResult<()> {
        close_browser_blocking(Arc::clone(&self.inner))
    }

    pub fn pages(&self, timeout: Duration) -> RwResult<Vec<RustwrightPage>> {
        list_pages_raw(Arc::clone(&self.inner), None, timeout).map(|pages| {
            pages
                .into_iter()
                .map(|inner| RustwrightPage { inner })
                .collect()
        })
    }

    pub fn is_connected(&self) -> bool {
        !self.inner.lifecycle.is_closed() && self.inner.client.is_connected()
    }

    pub fn is_owned(&self) -> bool {
        self.inner.owned
    }

    pub fn ws_endpoint(&self) -> String {
        self.inner.ws_endpoint.clone()
    }
}

impl RustwrightPage {
    pub fn target_id(&self) -> String {
        self.inner.target_id.clone()
    }

    pub fn url(&self) -> String {
        self.inner.cached_main_frame_url().unwrap_or_default()
    }

    /// Set or clear this page's general default timeout in milliseconds.
    pub fn set_default_timeout(&self, timeout_ms: Option<f64>) {
        self.inner
            .default_timeouts
            .lock()
            .unwrap()
            .general
            .page_default = timeout_ms.filter(|value| !value.is_nan());
    }

    /// Set or clear this page's navigation default timeout in milliseconds.
    pub fn set_default_navigation_timeout(&self, timeout_ms: Option<f64>) {
        self.inner
            .default_timeouts
            .lock()
            .unwrap()
            .navigation
            .page_default = timeout_ms.filter(|value| !value.is_nan());
    }

    /// Set or clear the inherited context general timeout stored for this page.
    pub fn set_context_default_timeout(&self, timeout_ms: Option<f64>) {
        self.inner
            .default_timeouts
            .lock()
            .unwrap()
            .general
            .context_default = timeout_ms.filter(|value| !value.is_nan());
    }

    /// Set or clear the inherited context navigation timeout stored for this page.
    pub fn set_context_default_navigation_timeout(&self, timeout_ms: Option<f64>) {
        self.inner
            .default_timeouts
            .lock()
            .unwrap()
            .navigation
            .context_default = timeout_ms.filter(|value| !value.is_nan());
    }

    fn resolve_timeout(&self, explicit: Option<f64>, navigation: bool) -> Option<f64> {
        self.inner
            .default_timeouts
            .lock()
            .unwrap()
            .resolve(explicit, navigation)
    }

    /// Subscribe to typed page events through a bounded, drop-oldest queue.
    pub fn events(&self) -> RustwrightPageEventReceiver {
        let queue = Arc::new(NativePageEventQueue::new());
        let page = Arc::downgrade(&self.inner);
        let mut events = self.inner.browser.client.subscribe();
        let task_queue = Arc::clone(&queue);
        let (close_tx, mut close_rx) = watch::channel(false);
        self.inner.browser.runtime.handle().spawn(async move {
            loop {
                let received = tokio::select! {
                    event = events.recv() => event,
                    changed = close_rx.changed() => {
                        if changed.is_err() || *close_rx.borrow() {
                            task_queue.close();
                            break;
                        }
                        continue;
                    }
                };
                let event = match received {
                    Ok(event) => event,
                    Err(broadcast::error::RecvError::Lagged(count)) => {
                        task_queue.record_upstream_drop(count);
                        continue;
                    }
                    Err(broadcast::error::RecvError::Closed) => {
                        task_queue.close();
                        break;
                    }
                };
                let Some(page) = page.upgrade() else {
                    task_queue.close();
                    break;
                };
                if let Some((event, terminal)) = native_page_event_from_cdp(&page, &event) {
                    task_queue.push(event, terminal);
                    if terminal {
                        break;
                    }
                }
            }
        });
        RustwrightPageEventReceiver { queue, close_tx }
    }

    pub fn goto(
        &self,
        url: &str,
        wait_until: Option<&str>,
        timeout_ms: Option<f64>,
        referer: Option<&str>,
    ) -> RwResult<String> {
        self.goto_with_cancel(url, wait_until, timeout_ms, referer, None)
    }

    pub fn goto_with_cancel(
        &self,
        url: &str,
        wait_until: Option<&str>,
        timeout_ms: Option<f64>,
        referer: Option<&str>,
        cancel: Option<&CancelToken>,
    ) -> RwResult<String> {
        let timeout_ms = self.resolve_timeout(timeout_ms, true);
        let page = Arc::clone(&self.inner);
        let url = url.to_string();
        let wait_until = wait_until.unwrap_or("load").to_string();
        let referer = referer.map(ToString::to_string);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser.block_on_raw(cancelable_navigation(
            client,
            session_id,
            cancel.cloned(),
            page_goto_async(page, url, wait_until, timeout, referer),
        ))
    }

    pub fn go_back(&self, wait_until: Option<&str>, timeout: Duration) -> RwResult<String> {
        self.go_back_with_cancel(wait_until, timeout, None)
    }

    pub fn go_back_with_cancel(
        &self,
        wait_until: Option<&str>,
        timeout: Duration,
        cancel: Option<&CancelToken>,
    ) -> RwResult<String> {
        self.navigate_history(-1, wait_until, timeout, cancel)
    }

    pub fn reload(&self, wait_until: Option<&str>, timeout: Duration) -> RwResult<String> {
        self.reload_with_cancel(wait_until, timeout, None)
    }

    pub fn reload_with_cancel(
        &self,
        wait_until: Option<&str>,
        timeout: Duration,
        cancel: Option<&CancelToken>,
    ) -> RwResult<String> {
        let page = Arc::clone(&self.inner);
        let wait_until = wait_until.unwrap_or("load").to_string();
        validate_navigation_wait_state(&wait_until)?;
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        let operation_client = Arc::clone(&client);
        let operation_session_id = session_id.clone();
        browser.block_on_raw(cancelable_navigation(
            client,
            session_id,
            cancel.cloned(),
            async move {
                let deadline = OperationDeadline::new(timeout);
                let mut events = operation_client.subscribe();
                loop {
                    match operation_client
                        .send(
                            "Page.reload",
                            json!({}),
                            Some(&operation_session_id),
                            deadline.remaining()?,
                        )
                        .await
                    {
                        Ok(_) => break,
                        Err(error) if is_page_not_attached_error(&error) => {
                            wait_for_page_attachment_signal(
                                &mut events,
                                &operation_session_id,
                                deadline,
                            )
                            .await?;
                        }
                        Err(error) => return Err(error),
                    }
                }
                if wait_until != "commit" {
                    wait_for_load_state(
                        &operation_client,
                        &mut events,
                        &operation_session_id,
                        &wait_until,
                        deadline.remaining()?,
                    )
                    .await?;
                }
                Ok(Value::Null.to_string())
            },
        ))
    }

    pub fn wait_for_load_state(&self, state: &str, timeout: Duration) -> RwResult<()> {
        self.wait_for_load_state_with_cancel(state, timeout, None)
    }

    pub fn wait_for_load_state_with_cancel(
        &self,
        state: &str,
        timeout: Duration,
        cancel: Option<&CancelToken>,
    ) -> RwResult<()> {
        validate_load_state(state)?;
        let page = Arc::clone(&self.inner);
        let state = state.to_string();
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        browser.block_on_raw(cancelable(cancel.cloned(), async move {
            let mut events = client.subscribe();
            let ready_state = client
                .send(
                    "Runtime.evaluate",
                    json!({
                        "expression": "document.readyState",
                        "returnByValue": true,
                    }),
                    Some(&session_id),
                    timeout,
                )
                .await?
                .pointer("/result/value")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let already_reached = match state.as_str() {
                "load" | "networkidle" => ready_state == "complete",
                "domcontentloaded" => matches!(ready_state.as_str(), "interactive" | "complete"),
                _ => false,
            };
            if already_reached {
                if state == "networkidle" {
                    tokio::time::sleep(Duration::from_millis(500)).await;
                }
                return Ok(());
            }
            wait_for_load_state(&client, &mut events, &session_id, &state, timeout).await
        }))
    }

    pub fn title(&self, timeout_ms: Option<f64>) -> RwResult<String> {
        let json = self.evaluate_expression("document.title", timeout_ms)?;
        Ok(serde_json::from_str::<Value>(&json)
            .ok()
            .and_then(|value| value.as_str().map(ToString::to_string))
            .unwrap_or_default())
    }

    pub fn evaluate(
        &self,
        expression: &str,
        arg_json: Option<&str>,
        timeout_ms: Option<f64>,
    ) -> RwResult<String> {
        self.evaluate_with_cancel(expression, arg_json, timeout_ms, None)
    }

    pub fn evaluate_with_cancel(
        &self,
        expression: &str,
        arg_json: Option<&str>,
        timeout_ms: Option<f64>,
        cancel: Option<&CancelToken>,
    ) -> RwResult<String> {
        let timeout_ms = self.resolve_timeout(timeout_ms, false);
        let expression = make_evaluate_expression(expression, arg_json);
        evaluate_expression_for_page_raw_cancelable(
            Arc::clone(&self.inner),
            expression,
            timeout_ms,
            cancel,
        )
    }

    pub fn click(&self, selector: &str, timeout_ms: Option<f64>) -> RwResult<()> {
        self.click_with_cancel(selector, timeout_ms, None)
    }

    pub fn click_with_cancel(
        &self,
        selector: &str,
        timeout_ms: Option<f64>,
        cancel: Option<&CancelToken>,
    ) -> RwResult<()> {
        let locator_json = selector_to_locator_json(selector)?;
        let body = r#"
if (!el) throw new Error('No element matches locator');
el.scrollIntoView({ block: 'center', inline: 'center' });
if (typeof el.focus === 'function') el.focus({ preventScroll: true });
el.click();
return true;
"#;
        self.evaluate_locator_json_cancelable(locator_json, 0, body.to_string(), timeout_ms, cancel)
            .map(|_| ())
    }

    pub fn fill(&self, selector: &str, value: &str, timeout_ms: Option<f64>) -> RwResult<()> {
        self.fill_with_cancel(selector, value, timeout_ms, None)
    }

    pub fn fill_with_cancel(
        &self,
        selector: &str,
        value: &str,
        timeout_ms: Option<f64>,
        cancel: Option<&CancelToken>,
    ) -> RwResult<()> {
        let locator_json = selector_to_locator_json(selector)?;
        let value_json = serde_json::to_string(value)?;
        let body = format!(
            r#"
if (!el) throw new Error('No element matches locator');
el.scrollIntoView({{ block: 'center', inline: 'center' }});
if (typeof el.focus === 'function') el.focus({{ preventScroll: true }});
const value = {value_json};
if ('value' in el) {{
  el.value = value;
}} else if (el.isContentEditable) {{
  el.textContent = value;
}} else {{
  el.textContent = value;
}}
el.dispatchEvent(new Event('input', {{ bubbles: true }}));
el.dispatchEvent(new Event('change', {{ bubbles: true }}));
return true;
"#
        );
        self.evaluate_locator_json_cancelable(locator_json, 0, body, timeout_ms, cancel)
            .map(|_| ())
    }

    /// Type through Chromium's input domain after focusing the matching element.
    pub fn type_text(&self, selector: &str, text: &str, delay: Option<Duration>) -> RwResult<()> {
        self.type_text_with_cancel(selector, text, delay, None)
    }

    pub fn type_text_with_cancel(
        &self,
        selector: &str,
        text: &str,
        delay: Option<Duration>,
        cancel: Option<&CancelToken>,
    ) -> RwResult<()> {
        let locator_json = selector_to_locator_json(selector)?;
        let page = Arc::clone(&self.inner);
        let text = text.to_string();
        let browser = Arc::clone(&page.browser);
        browser.block_on_raw(cancelable(cancel.cloned(), async move {
            let deadline = OperationDeadline::new(Duration::from_secs(30));
            let resolution = focus_locator_for_native_input(&page, &locator_json, deadline).await?;
            for character in text.chars() {
                dispatch_typed_character(
                    &page.browser.client,
                    &resolution.session_id,
                    character,
                    delay,
                    deadline,
                )
                .await?;
            }
            Ok(())
        }))
    }

    /// Press a key through Chromium's input domain, optionally focusing a selector first.
    pub fn press_key(&self, selector: Option<&str>, key: &str) -> RwResult<()> {
        let locator_json = selector.map(selector_to_locator_json).transpose()?;
        let page = Arc::clone(&self.inner);
        let key = key.to_string();
        let browser = Arc::clone(&page.browser);
        browser.block_on_raw(async move {
            let deadline = OperationDeadline::new(Duration::from_secs(30));
            let session_id = match locator_json {
                Some(locator_json) => {
                    focus_locator_for_native_input(&page, &locator_json, deadline)
                        .await?
                        .session_id
                }
                None => page.session_id.clone(),
            };
            dispatch_key_press(&page.browser.client, &session_id, &key, None, deadline).await
        })
    }

    /// Select options by value using the page DOM and return the resulting values.
    pub fn select_options(&self, selector: &str, values: &[String]) -> RwResult<Vec<String>> {
        self.select_options_with_cancel(selector, values, None)
    }

    pub fn select_options_with_cancel(
        &self,
        selector: &str,
        values: &[String],
        cancel: Option<&CancelToken>,
    ) -> RwResult<Vec<String>> {
        let locator_json = selector_to_locator_json(selector)?;
        let values_json = serde_json::to_string(values)?;
        let body = format!(
            r#"
if (!el) throw new Error('No element matches locator');
if (!(el instanceof HTMLSelectElement)) throw new Error('Element is not a <select> element');
const requested = {values_json};
const options = Array.from(el.options);
const matched = options.filter(option => requested.includes(option.value));
const found = new Set(matched.map(option => option.value));
const missing = requested.filter(value => !found.has(value));
if (missing.length) throw new Error(`Select option values not found: ${{missing.join(', ')}}`);
for (const option of options) option.selected = false;
if (el.multiple) {{
  for (const option of matched) option.selected = true;
}} else if (matched.length) {{
  matched[0].selected = true;
}}
el.dispatchEvent(new Event('input', {{ bubbles: true }}));
el.dispatchEvent(new Event('change', {{ bubbles: true }}));
return JSON.stringify(Array.from(el.selectedOptions).map(option => option.value));
"#
        );
        let json = self.evaluate_locator_json_cancelable(locator_json, 0, body, None, cancel)?;
        let selected_json: String = serde_json::from_str(&json)?;
        Ok(serde_json::from_str(&selected_json)?)
    }

    /// Move the native mouse to the center of the matching element.
    pub fn hover(&self, selector: &str) -> RwResult<()> {
        self.hover_with_cancel(selector, None)
    }

    pub fn hover_with_cancel(&self, selector: &str, cancel: Option<&CancelToken>) -> RwResult<()> {
        let locator_json = selector_to_locator_json(selector)?;
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        browser.block_on_raw(cancelable(cancel.cloned(), async move {
            let deadline = OperationDeadline::new(Duration::from_secs(30));
            scroll_locator_into_view(&page, &locator_json, deadline).await?;
            let resolved =
                resolve_locator_point(Arc::clone(&page), &locator_json, 0, None, None, deadline)
                    .await?;
            dispatch_mouse_move_sequence_in_session(
                &page.browser.client,
                &resolved.session_id,
                0.0,
                0.0,
                resolved.x,
                resolved.y,
                1,
                0,
                0,
                deadline,
            )
            .await
        }))
    }

    /// Check the matching checkbox through a native mouse click.
    pub fn check(&self, selector: &str) -> RwResult<()> {
        self.set_checked(selector, true)
    }

    /// Uncheck the matching checkbox through a native mouse click.
    pub fn uncheck(&self, selector: &str) -> RwResult<()> {
        self.set_checked(selector, false)
    }

    /// Return the rendered inner text of the matching element.
    pub fn inner_text(&self, selector: &str) -> RwResult<Option<String>> {
        let locator_json = selector_to_locator_json(selector)?;
        let body = "return el ? (el.innerText || el.textContent || '') : null;".to_string();
        let json = self.evaluate_locator_json(locator_json, 0, body, None)?;
        Ok(serde_json::from_str::<Value>(&json)
            .ok()
            .and_then(|value| value.as_str().map(ToString::to_string)))
    }

    /// Return an attribute from the matching element.
    pub fn get_attribute(&self, selector: &str, name: &str) -> RwResult<Option<String>> {
        let locator_json = selector_to_locator_json(selector)?;
        let name_json = serde_json::to_string(name)?;
        let body = format!("return el ? el.getAttribute({name_json}) : null;");
        let json = self.evaluate_locator_json(locator_json, 0, body, None)?;
        Ok(serde_json::from_str::<Value>(&json)
            .ok()
            .and_then(|value| value.as_str().map(ToString::to_string)))
    }

    /// Return whether the matching element is visible according to the locator engine.
    pub fn is_visible(&self, selector: &str) -> RwResult<bool> {
        self.evaluate_locator_bool(selector, "return !!el && visible(el);")
    }

    /// Return whether the matching element is enabled according to the locator engine.
    pub fn is_enabled(&self, selector: &str) -> RwResult<bool> {
        self.evaluate_locator_bool(selector, "return !!el && !disabledState(el);")
    }

    /// Return whether the matching checkbox, radio, or ARIA control is checked.
    pub fn is_checked(&self, selector: &str) -> RwResult<bool> {
        let body = format!("return ({NATIVE_CHECKED_STATE_JS})(el).checked;");
        self.evaluate_locator_bool(selector, &body)
    }

    /// Override the page viewport through Chromium's emulation domain.
    pub fn set_viewport_size(&self, width: u32, height: u32) -> RwResult<()> {
        if width == 0 || height == 0 {
            return Err(RwError::InvalidInput(
                "viewport width and height must be greater than zero".to_string(),
            ));
        }
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        browser.block_on_raw(async move {
            page.browser
                .client
                .send(
                    "Emulation.setDeviceMetricsOverride",
                    json!({
                        "width": width,
                        "height": height,
                        "deviceScaleFactor": 1,
                        "mobile": false,
                        "screenWidth": width,
                        "screenHeight": height,
                    }),
                    Some(&page.session_id),
                    Duration::from_secs(30),
                )
                .await
                .map(|_| ())
        })
    }

    /// Scroll the matching element into view using the page DOM.
    pub fn scroll_into_view(&self, selector: &str) -> RwResult<()> {
        let locator_json = selector_to_locator_json(selector)?;
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        browser.block_on_raw(async move {
            scroll_locator_into_view(
                &page,
                &locator_json,
                OperationDeadline::new(Duration::from_secs(30)),
            )
            .await
        })
    }

    pub fn text_content(
        &self,
        selector: &str,
        timeout_ms: Option<f64>,
    ) -> RwResult<Option<String>> {
        let locator_json = selector_to_locator_json(selector)?;
        let body = "return el ? el.textContent : null;".to_string();
        let json = self.evaluate_locator_json(locator_json, 0, body, timeout_ms)?;
        Ok(serde_json::from_str::<Value>(&json)
            .ok()
            .and_then(|value| value.as_str().map(ToString::to_string)))
    }

    pub fn screenshot(
        &self,
        path: Option<&str>,
        full_page: Option<bool>,
        clip_json: Option<&str>,
        timeout_ms: Option<f64>,
        image_type: Option<&str>,
        quality: Option<u32>,
        omit_background: Option<bool>,
    ) -> RwResult<Vec<u8>> {
        self.screenshot_with_cancel(
            path,
            full_page,
            clip_json,
            timeout_ms,
            image_type,
            quality,
            omit_background,
            None,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn screenshot_with_cancel(
        &self,
        path: Option<&str>,
        full_page: Option<bool>,
        clip_json: Option<&str>,
        timeout_ms: Option<f64>,
        image_type: Option<&str>,
        quality: Option<u32>,
        omit_background: Option<bool>,
        cancel: Option<&CancelToken>,
    ) -> RwResult<Vec<u8>> {
        let timeout_ms = self.resolve_timeout(timeout_ms, false);
        let page = Arc::clone(&self.inner);
        let path = path.map(ToString::to_string);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let capture_beyond_viewport = full_page.unwrap_or(false);
        let image_type = image_type.unwrap_or("png").to_string();
        let clip = match clip_json {
            Some(value) => Some(serde_json::from_str::<Value>(value)?),
            None => None,
        };
        let browser = Arc::clone(&page.browser);
        let omit_background = omit_background.unwrap_or(false);
        browser.block_on_raw(cancelable(
            cancel.cloned(),
            page_screenshot_async(
                page,
                path,
                capture_beyond_viewport,
                clip,
                timeout,
                image_type,
                quality,
                omit_background,
            ),
        ))
    }

    pub fn close(&self, timeout_ms: Option<f64>, run_before_unload: bool) -> RwResult<()> {
        let timeout_ms = self.resolve_timeout(timeout_ms, false);
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        browser.block_on_raw(page_close_async(page, timeout, run_before_unload))
    }

    fn evaluate_expression(&self, expression: &str, timeout_ms: Option<f64>) -> RwResult<String> {
        let timeout_ms = self.resolve_timeout(timeout_ms, false);
        evaluate_expression_for_page_raw(
            Arc::clone(&self.inner),
            expression.to_string(),
            timeout_ms,
        )
    }

    fn set_checked(&self, selector: &str, checked: bool) -> RwResult<()> {
        let locator_json = selector_to_locator_json(selector)?;
        let page = Arc::clone(&self.inner);
        let browser = Arc::clone(&page.browser);
        browser.block_on_raw(async move {
            let deadline = OperationDeadline::new(Duration::from_secs(30));
            scroll_locator_into_view(&page, &locator_json, deadline).await?;
            let resolved =
                resolve_locator_point(Arc::clone(&page), &locator_json, 0, None, None, deadline)
                    .await?;
            let state_json = evaluate_resolved_locator_body(
                &page,
                &resolved,
                &format!("return JSON.stringify(({NATIVE_CHECKED_STATE_JS})(el));"),
                deadline,
            )
            .await?;
            let state = serde_json::from_str::<Value>(
                state_json
                    .as_str()
                    .ok_or_else(|| RwError::Message("invalid checked state".to_string()))?,
            )?;
            if !state.get("valid").and_then(Value::as_bool).unwrap_or(false) {
                return Err(RwError::Message(
                    "element is not a checkbox, radio button, or checked ARIA control".to_string(),
                ));
            }
            let current = state
                .get("checked")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            if current == checked {
                return Ok(());
            }
            if !checked
                && state
                    .get("native_radio")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
            {
                return Err(RwError::Message(
                    "radio buttons cannot be unchecked directly".to_string(),
                ));
            }
            dispatch_mouse_click_sequence_in_session(
                &page.browser.client,
                &resolved.session_id,
                resolved.x,
                resolved.y,
                resolved.x,
                resolved.y,
                1,
                "left",
                1,
                1,
                0.0,
                0,
                0,
                deadline,
            )
            .await?;
            let updated = evaluate_resolved_locator_body(
                &page,
                &resolved,
                &format!("return ({NATIVE_CHECKED_STATE_JS})(el).checked;"),
                deadline,
            )
            .await?
            .as_bool()
            .unwrap_or(false);
            if updated != checked {
                return Err(RwError::Message(
                    "native click did not change the checked state".to_string(),
                ));
            }
            Ok(())
        })
    }

    fn evaluate_locator_bool(&self, selector: &str, body: &str) -> RwResult<bool> {
        let locator_json = selector_to_locator_json(selector)?;
        let json = self.evaluate_locator_json(locator_json, 0, body.to_string(), None)?;
        Ok(serde_json::from_str::<Value>(&json)
            .ok()
            .and_then(|value| value.as_bool())
            .unwrap_or(false))
    }

    fn navigate_history(
        &self,
        offset: i64,
        wait_until: Option<&str>,
        timeout: Duration,
        cancel: Option<&CancelToken>,
    ) -> RwResult<String> {
        let page = Arc::clone(&self.inner);
        let wait_until = wait_until.unwrap_or("load").to_string();
        validate_navigation_wait_state(&wait_until)?;
        let browser = Arc::clone(&page.browser);
        let client = Arc::clone(&browser.client);
        let session_id = page.session_id.clone();
        let operation_client = Arc::clone(&client);
        let operation_session_id = session_id.clone();
        browser.block_on_raw(cancelable_navigation(
            client,
            session_id,
            cancel.cloned(),
            async move {
                let deadline = OperationDeadline::new(timeout);
                let history = operation_client
                    .send(
                        "Page.getNavigationHistory",
                        json!({}),
                        Some(&operation_session_id),
                        deadline.remaining()?,
                    )
                    .await?;
                let current_index = history
                    .get("currentIndex")
                    .and_then(Value::as_i64)
                    .ok_or_else(|| RwError::Cdp {
                        method: "Page.getNavigationHistory".to_string(),
                        message: "response did not include currentIndex".to_string(),
                    })?;
                let target_index = current_index + offset;
                let entries = history
                    .get("entries")
                    .and_then(Value::as_array)
                    .ok_or_else(|| RwError::Cdp {
                        method: "Page.getNavigationHistory".to_string(),
                        message: "response did not include entries".to_string(),
                    })?;
                if target_index < 0 || target_index as usize >= entries.len() {
                    return Ok(Value::Null.to_string());
                }
                let entry = &entries[target_index as usize];
                let entry_id =
                    entry
                        .get("id")
                        .and_then(Value::as_i64)
                        .ok_or_else(|| RwError::Cdp {
                            method: "Page.getNavigationHistory".to_string(),
                            message: "entry did not include id".to_string(),
                        })?;
                let target_url = entry
                    .get("url")
                    .and_then(Value::as_str)
                    .map(ToString::to_string);
                let mut events = operation_client.subscribe();
                operation_client
                    .send(
                        "Page.navigateToHistoryEntry",
                        json!({ "entryId": entry_id }),
                        Some(&operation_session_id),
                        deadline.remaining()?,
                    )
                    .await?;
                let response = wait_for_navigation(
                    &mut events,
                    &operation_session_id,
                    &wait_until,
                    None,
                    target_url.as_deref(),
                    "Page.go_back",
                    deadline.remaining()?,
                )
                .await?;
                if wait_until != "commit" {
                    settle_history_navigation(
                        &operation_client,
                        &mut events,
                        &operation_session_id,
                        &wait_until,
                        deadline,
                    )
                    .await?;
                }
                Ok(response.unwrap_or(Value::Null).to_string())
            },
        ))
    }

    fn evaluate_locator_json(
        &self,
        locator_json: String,
        index: usize,
        body: String,
        timeout_ms: Option<f64>,
    ) -> RwResult<String> {
        self.evaluate_locator_json_cancelable(locator_json, index, body, timeout_ms, None)
    }

    fn evaluate_locator_json_cancelable(
        &self,
        locator_json: String,
        index: usize,
        body: String,
        timeout_ms: Option<f64>,
        cancel: Option<&CancelToken>,
    ) -> RwResult<String> {
        let timeout_ms = self.resolve_timeout(timeout_ms, false);
        let page = Arc::clone(&self.inner);
        let timeout = BrowserInner::command_timeout(timeout_ms);
        let browser = Arc::clone(&page.browser);
        browser.block_on_raw(cancelable(cancel.cloned(), async move {
            evaluate_locator_for_page(page, locator_json, index, body, timeout).await
        }))
    }
}

const NATIVE_CHECKED_STATE_JS: &str = r#"(el) => {
const tagName = String(el && el.tagName || '').toUpperCase();
const inputType = tagName === 'INPUT' ? String(el.type || 'text').toLowerCase() : '';
const checkedRoles = new Set(['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio', 'option', 'treeitem']);
const role = el && typeof locatorRoleOf === 'function' ? locatorRoleOf(el) : '';
const aria = String(el && el.getAttribute ? el.getAttribute('aria-checked') || '' : '').toLowerCase();
if (tagName === 'INPUT' && (inputType === 'checkbox' || inputType === 'radio')) {
  const checked = !!el.checked;
  return { valid: true, checked, native_radio: inputType === 'radio' };
}
if (!checkedRoles.has(role)) return { valid: false, checked: false, native_radio: false };
return { valid: true, checked: aria === 'true', native_radio: false };
}"#;

fn native_page_event_from_cdp(
    page: &Arc<PageInner>,
    event: &Value,
) -> Option<(RustwrightPageEvent, bool)> {
    let method = event.get("method").and_then(Value::as_str)?;
    let event_session_id = event.get("sessionId").and_then(Value::as_str);
    match method {
        "Page.javascriptDialogOpening" if event_session_id == Some(page.session_id.as_str()) => {
            let kind = match event.pointer("/params/type").and_then(Value::as_str) {
                Some("alert") => RustwrightDialogKind::Alert,
                Some("confirm") => RustwrightDialogKind::Confirm,
                Some("prompt") => RustwrightDialogKind::Prompt,
                Some("beforeunload") => RustwrightDialogKind::BeforeUnload,
                Some(other) => RustwrightDialogKind::Other(other.to_string()),
                None => RustwrightDialogKind::Other(String::new()),
            };
            let message = event
                .pointer("/params/message")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            Some((
                RustwrightPageEvent::Dialog {
                    kind,
                    message,
                    dialog: RustwrightDialog {
                        browser: Arc::downgrade(&page.browser),
                        session_id: page.session_id.clone(),
                    },
                },
                false,
            ))
        }
        "Page.frameNavigated" if event_session_id == Some(page.session_id.as_str()) => {
            let frame = event.pointer("/params/frame")?;
            if frame.get("parentId").is_some() {
                return None;
            }
            let url = frame.get("url").and_then(Value::as_str)?.to_string();
            Some((RustwrightPageEvent::Navigated { url }, false))
        }
        "Page.navigatedWithinDocument" if event_session_id == Some(page.session_id.as_str()) => {
            let frame_id = event.pointer("/params/frameId").and_then(Value::as_str)?;
            if page.main_frame_id.lock().unwrap().as_deref() != Some(frame_id) {
                return None;
            }
            let url = event
                .pointer("/params/url")
                .and_then(Value::as_str)?
                .to_string();
            Some((RustwrightPageEvent::Navigated { url }, false))
        }
        "Browser.downloadWillBegin" => {
            let frame_id = event.pointer("/params/frameId").and_then(Value::as_str)?;
            if page.main_frame_id.lock().unwrap().as_deref() != Some(frame_id) {
                return None;
            }
            Some((
                RustwrightPageEvent::Download {
                    guid: event
                        .pointer("/params/guid")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                    url: event
                        .pointer("/params/url")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                    suggested_name: event
                        .pointer("/params/suggestedFilename")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                },
                false,
            ))
        }
        "Target.targetCrashed"
            if event.pointer("/params/targetId").and_then(Value::as_str)
                == Some(page.target_id.as_str()) =>
        {
            Some((RustwrightPageEvent::PageCrashed, true))
        }
        "Inspector.targetCrashed" if event_session_id == Some(page.session_id.as_str()) => {
            Some((RustwrightPageEvent::PageCrashed, true))
        }
        "Target.targetDestroyed"
            if event.pointer("/params/targetId").and_then(Value::as_str)
                == Some(page.target_id.as_str()) =>
        {
            Some((RustwrightPageEvent::Closed, true))
        }
        "Inspector.detached" if event_session_id == Some(page.session_id.as_str()) => {
            Some((RustwrightPageEvent::Closed, true))
        }
        "Target.detachedFromTarget"
            if event.pointer("/params/sessionId").and_then(Value::as_str)
                == Some(page.session_id.as_str()) =>
        {
            Some((RustwrightPageEvent::Closed, true))
        }
        _ => None,
    }
}

async fn scroll_locator_into_view(
    page: &Arc<PageInner>,
    locator_json: &str,
    deadline: OperationDeadline,
) -> RwResult<()> {
    let resolution = resolve_locator_session(Arc::clone(page), locator_json, deadline).await?;
    let expression = locator_script(
        &resolution.locator_json,
        0,
        "if (!el) throw new Error('No element matches locator'); el.scrollIntoView({ block: 'center', inline: 'center' }); return true;",
    );
    evaluate_locator_resolution(page, &resolution, expression, deadline, Duration::ZERO)
        .await
        .map(|_| ())
}

async fn focus_locator_for_native_input(
    page: &Arc<PageInner>,
    locator_json: &str,
    deadline: OperationDeadline,
) -> RwResult<LocatorSessionResolution> {
    let resolution = resolve_locator_session(Arc::clone(page), locator_json, deadline).await?;
    let expression = locator_script(
        &resolution.locator_json,
        0,
        "if (!el) throw new Error('No element matches locator'); el.scrollIntoView({ block: 'center', inline: 'center' }); if (typeof el.focus === 'function') el.focus({ preventScroll: true }); return true;",
    );
    evaluate_locator_resolution(page, &resolution, expression, deadline, Duration::ZERO).await?;
    Ok(resolution)
}

#[derive(Clone)]
struct NativeKeyDescriptor {
    key: String,
    code: String,
    virtual_key: u32,
    location: u8,
    text: Option<String>,
}

fn native_key_descriptor(key: &str) -> RwResult<NativeKeyDescriptor> {
    let named = match key {
        "Alt" | "AltLeft" => Some(("Alt", "AltLeft", 18, 1)),
        "AltRight" => Some(("Alt", "AltRight", 18, 2)),
        "Backspace" => Some(("Backspace", "Backspace", 8, 0)),
        "Control" | "ControlLeft" => Some(("Control", "ControlLeft", 17, 1)),
        "ControlRight" => Some(("Control", "ControlRight", 17, 2)),
        "Delete" => Some(("Delete", "Delete", 46, 0)),
        "End" => Some(("End", "End", 35, 0)),
        "Enter" => Some(("Enter", "Enter", 13, 0)),
        "Escape" => Some(("Escape", "Escape", 27, 0)),
        "Home" => Some(("Home", "Home", 36, 0)),
        "Insert" => Some(("Insert", "Insert", 45, 0)),
        "Meta" | "MetaLeft" => Some(("Meta", "MetaLeft", 91, 1)),
        "MetaRight" => Some(("Meta", "MetaRight", 92, 2)),
        "PageDown" => Some(("PageDown", "PageDown", 34, 0)),
        "PageUp" => Some(("PageUp", "PageUp", 33, 0)),
        "Shift" | "ShiftLeft" => Some(("Shift", "ShiftLeft", 16, 1)),
        "ShiftRight" => Some(("Shift", "ShiftRight", 16, 2)),
        "Tab" => Some(("Tab", "Tab", 9, 0)),
        "ArrowDown" => Some(("ArrowDown", "ArrowDown", 40, 0)),
        "ArrowLeft" => Some(("ArrowLeft", "ArrowLeft", 37, 0)),
        "ArrowRight" => Some(("ArrowRight", "ArrowRight", 39, 0)),
        "ArrowUp" => Some(("ArrowUp", "ArrowUp", 38, 0)),
        "Space" => Some((" ", "Space", 32, 0)),
        _ => None,
    };
    if let Some((normalized, code, virtual_key, location)) = named {
        return Ok(NativeKeyDescriptor {
            key: normalized.to_string(),
            code: code.to_string(),
            virtual_key,
            location,
            text: (key == "Space").then(|| " ".to_string()),
        });
    }
    let mut characters = key.chars();
    let Some(character) = characters.next() else {
        return Err(RwError::InvalidInput("key must not be empty".to_string()));
    };
    if characters.next().is_some() || !character.is_ascii() {
        return Err(RwError::InvalidInput(format!("unsupported key: {key}")));
    }
    let (code, virtual_key) = if character.is_ascii_alphabetic() {
        (
            format!("Key{}", character.to_ascii_uppercase()),
            u32::from(character.to_ascii_uppercase()),
        )
    } else if character.is_ascii_digit() {
        (format!("Digit{character}"), u32::from(character))
    } else {
        (String::new(), u32::from(character))
    };
    Ok(NativeKeyDescriptor {
        key: character.to_string(),
        code,
        virtual_key,
        location: 0,
        text: Some(character.to_string()),
    })
}

fn native_modifier_mask(key: &str) -> Option<i64> {
    match key {
        "Alt" | "AltLeft" | "AltRight" => Some(1),
        "Control" | "ControlLeft" | "ControlRight" => Some(2),
        "Meta" | "MetaLeft" | "MetaRight" => Some(4),
        "Shift" | "ShiftLeft" | "ShiftRight" => Some(8),
        _ => None,
    }
}

fn native_key_event_params(
    descriptor: &NativeKeyDescriptor,
    event_type: &str,
    modifiers: i64,
    include_text: bool,
) -> Value {
    let mut params = json!({
        "type": event_type,
        "key": descriptor.key,
        "code": descriptor.code,
        "modifiers": modifiers,
        "windowsVirtualKeyCode": descriptor.virtual_key,
        "nativeVirtualKeyCode": descriptor.virtual_key,
        "location": descriptor.location,
    });
    if descriptor.location == 3 {
        params["isKeypad"] = Value::Bool(true);
    }
    if include_text {
        if let Some(text) = &descriptor.text {
            params["text"] = Value::String(text.clone());
            params["unmodifiedText"] = Value::String(text.clone());
        }
    }
    params
}

async fn wait_input_delay(delay: Option<Duration>, deadline: OperationDeadline) -> RwResult<()> {
    let Some(delay) = delay.filter(|value| !value.is_zero()) else {
        return Ok(());
    };
    tokio::time::timeout(deadline.remaining()?, tokio::time::sleep(delay))
        .await
        .map_err(|_| RwError::Timeout(duration_millis_u64(deadline.timeout)))?;
    Ok(())
}

async fn dispatch_typed_character(
    client: &CdpClient,
    session_id: &str,
    character: char,
    delay: Option<Duration>,
    deadline: OperationDeadline,
) -> RwResult<()> {
    if !character.is_ascii() || character.is_ascii_control() {
        client
            .send(
                "Input.insertText",
                json!({ "text": character.to_string() }),
                Some(session_id),
                deadline.remaining()?,
            )
            .await?;
        return wait_input_delay(delay, deadline).await;
    }
    dispatch_key_press(client, session_id, &character.to_string(), delay, deadline).await
}

async fn dispatch_key_press(
    client: &CdpClient,
    session_id: &str,
    key: &str,
    delay: Option<Duration>,
    deadline: OperationDeadline,
) -> RwResult<()> {
    let parts = if key == "+" {
        vec![key]
    } else {
        key.split('+').collect::<Vec<_>>()
    };
    if parts.iter().any(|part| part.is_empty()) {
        return Err(RwError::InvalidInput(format!("unsupported key: {key}")));
    }
    let (modifier_names, base_name) = parts.split_at(parts.len().saturating_sub(1));
    let base_name = base_name
        .first()
        .copied()
        .ok_or_else(|| RwError::InvalidInput("key must not be empty".to_string()))?;
    let mut modifiers = 0_i64;
    let mut pressed_modifiers = Vec::new();
    for modifier_name in modifier_names {
        let mask = native_modifier_mask(modifier_name).ok_or_else(|| {
            RwError::InvalidInput(format!("unsupported key modifier: {modifier_name}"))
        })?;
        let descriptor = native_key_descriptor(modifier_name)?;
        modifiers |= mask;
        client
            .send(
                "Input.dispatchKeyEvent",
                native_key_event_params(&descriptor, "rawKeyDown", modifiers, false),
                Some(session_id),
                deadline.remaining()?,
            )
            .await?;
        pressed_modifiers.push((descriptor, mask));
    }
    let base = native_key_descriptor(base_name)?;
    let include_text = base.text.is_some() && modifiers & (1 | 2 | 4) == 0;
    client
        .send(
            "Input.dispatchKeyEvent",
            native_key_event_params(
                &base,
                if include_text {
                    "keyDown"
                } else {
                    "rawKeyDown"
                },
                modifiers,
                include_text,
            ),
            Some(session_id),
            deadline.remaining()?,
        )
        .await?;
    wait_input_delay(delay, deadline).await?;
    client
        .send(
            "Input.dispatchKeyEvent",
            native_key_event_params(&base, "keyUp", modifiers, false),
            Some(session_id),
            deadline.remaining()?,
        )
        .await?;
    for (descriptor, mask) in pressed_modifiers.into_iter().rev() {
        modifiers &= !mask;
        client
            .send(
                "Input.dispatchKeyEvent",
                native_key_event_params(&descriptor, "keyUp", modifiers, false),
                Some(session_id),
                deadline.remaining()?,
            )
            .await?;
    }
    Ok(())
}

fn is_page_not_attached_error(error: &RwError) -> bool {
    matches!(
        error,
        RwError::Cdp { message, .. }
            if message
                .to_ascii_lowercase()
                .contains("not attached to an active page")
    )
}

async fn wait_for_page_attachment_signal(
    events: &mut broadcast::Receiver<Value>,
    session_id: &str,
    deadline: OperationDeadline,
) -> RwResult<()> {
    loop {
        match tokio::time::timeout(deadline.remaining()?, events.recv()).await {
            Ok(Ok(event)) => {
                if event.get("sessionId").and_then(Value::as_str) != Some(session_id) {
                    continue;
                }
                if matches!(
                    event.get("method").and_then(Value::as_str),
                    Some(
                        "Page.frameNavigated" | "Page.domContentEventFired" | "Page.loadEventFired"
                    )
                ) {
                    return Ok(());
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(broadcast::error::RecvError::Closed)) => return Err(RwError::Closed),
            Err(_) => return Err(RwError::Timeout(duration_millis_u64(deadline.timeout))),
        }
    }
}

async fn settle_history_navigation(
    client: &CdpClient,
    events: &mut broadcast::Receiver<Value>,
    session_id: &str,
    wait_until: &str,
    deadline: OperationDeadline,
) -> RwResult<()> {
    loop {
        let ready_state = client
            .send(
                "Runtime.evaluate",
                json!({
                    "expression": "document.readyState",
                    "returnByValue": true,
                }),
                Some(session_id),
                deadline.remaining()?,
            )
            .await;
        match ready_state {
            Ok(result) => {
                let ready_state = result
                    .pointer("/result/value")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                let ready = match wait_until {
                    "domcontentloaded" => matches!(ready_state, "interactive" | "complete"),
                    "load" | "networkidle" => ready_state == "complete",
                    _ => true,
                };
                if ready {
                    match client
                        .send(
                            "Page.getFrameTree",
                            json!({}),
                            Some(session_id),
                            deadline.remaining()?,
                        )
                        .await
                    {
                        Ok(_) => return Ok(()),
                        Err(error) if is_page_not_attached_error(&error) => {}
                        Err(error) => return Err(error),
                    }
                }
            }
            Err(error)
                if is_locator_wait_context_loss(&error) || is_page_not_attached_error(&error) => {}
            Err(error) => return Err(error),
        }
        wait_for_page_attachment_signal(events, session_id, deadline).await?;
    }
}

fn validate_navigation_wait_state(state: &str) -> RwResult<()> {
    if matches!(
        state,
        "load" | "domcontentloaded" | "networkidle" | "commit"
    ) {
        Ok(())
    } else {
        Err(RwError::InvalidInput(
            "wait_until must be load, domcontentloaded, networkidle, or commit".to_string(),
        ))
    }
}

fn validate_load_state(state: &str) -> RwResult<()> {
    if matches!(state, "load" | "domcontentloaded" | "networkidle") {
        Ok(())
    } else {
        Err(RwError::InvalidInput(
            "load state must be load, domcontentloaded, or networkidle".to_string(),
        ))
    }
}

fn selector_to_locator_json(selector: &str) -> RwResult<String> {
    let value = selector;
    let spec = if let Some(text) = value.strip_prefix("text=") {
        text_selector_spec(text)
    } else if let Some(css) = value.strip_prefix("css=") {
        json!({ "kind": "css", "selector": css })
    } else if let Some(xpath) = value.strip_prefix("xpath=") {
        json!({ "kind": "xpath", "selector": xpath })
    } else if value.starts_with("//")
        || value.starts_with(".//")
        || value == ".."
        || value.starts_with("../")
    {
        json!({ "kind": "xpath", "selector": value })
    } else {
        json!({ "kind": "css", "selector": value })
    };
    Ok(spec.to_string())
}

fn text_selector_spec(body: &str) -> Value {
    let value = body.trim();
    if value.len() >= 2 && value.starts_with('/') {
        if let Some(slash) = value.rfind('/').filter(|index| *index > 0) {
            return json!({
                "kind": "text_selector",
                "text": {
                    "kind": "regex",
                    "pattern": &value[1..slash],
                    "flags": &value[slash + 1..],
                },
                "exact": false,
            });
        }
    }
    let exact = value.len() >= 2
        && value
            .chars()
            .next()
            .zip(value.chars().last())
            .map(|(first, last)| first == last && (first == '\'' || first == '"'))
            .unwrap_or(false);
    let text = if exact {
        unquote_selector_argument(value)
    } else {
        value.to_string()
    };
    json!({ "kind": "text_selector", "text": text, "exact": exact })
}

fn unquote_selector_argument(value: &str) -> String {
    let text = value.trim();
    let quoted = text.len() >= 2
        && text
            .chars()
            .next()
            .zip(text.chars().last())
            .map(|(first, last)| first == last && (first == '\'' || first == '"'))
            .unwrap_or(false);
    if !quoted {
        return text.to_string();
    }
    let mut result = String::new();
    let mut escaped = false;
    for char in text[1..text.len() - 1].chars() {
        if escaped {
            result.push(char);
            escaped = false;
        } else if char == '\\' {
            escaped = true;
        } else {
            result.push(char);
        }
    }
    if escaped {
        result.push('\\');
    }
    result
}

#[cfg(feature = "python")]
fn create_page(browser: Arc<BrowserInner>, context_id: Option<String>) -> RwResult<PyPage> {
    let browser_for_task = Arc::clone(&browser);
    browser
        .block_on(create_page_async(browser_for_task, context_id))
        .map(|inner| PyPage { inner })
}

fn create_page_raw(
    browser: Arc<BrowserInner>,
    context_id: Option<String>,
) -> RwResult<Arc<PageInner>> {
    create_page_raw_cancelable(browser, context_id, None)
}

fn create_page_raw_cancelable(
    browser: Arc<BrowserInner>,
    context_id: Option<String>,
    cancel: Option<&CancelToken>,
) -> RwResult<Arc<PageInner>> {
    let browser_for_task = Arc::clone(&browser);
    browser.block_on_raw(cancelable(
        cancel.cloned(),
        create_page_async(browser_for_task, context_id),
    ))
}

async fn create_page_async(
    browser: Arc<BrowserInner>,
    context_id: Option<String>,
) -> RwResult<Arc<PageInner>> {
    let mut params = json!({ "url": "about:blank" });
    if let Some(context_id) = &context_id {
        params["browserContextId"] = Value::String(context_id.clone());
    }
    let target_guard = create_target_cancellation_safe(Arc::clone(&browser), params).await?;
    let target_id = target_guard
        .target_id
        .as_deref()
        .expect("a newly created target guard is armed")
        .to_string();
    let page =
        attach_existing_page(browser, target_id, context_id, Duration::from_secs(10)).await?;
    page.close_target_on_drop.store(true, Ordering::SeqCst);
    target_guard.disarm();
    Ok(page)
}

async fn attach_existing_page(
    browser: Arc<BrowserInner>,
    target_id: String,
    context_id: Option<String>,
    timeout: Duration,
) -> RwResult<Arc<PageInner>> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let (reserved_generation, attach_lock) = match browser.attached_pages.reserve(&target_id) {
            AttachedPageReservation::Existing(page) => return Ok(page),
            AttachedPageReservation::Attach {
                generation,
                attach_lock,
            } => (generation, attach_lock),
        };
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            browser.attached_pages.remove_reservation(
                &target_id,
                reserved_generation,
                &attach_lock,
            );
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let target_guard = match tokio::time::timeout(remaining, attach_lock.lock()).await {
            Ok(guard) => guard,
            Err(_) => {
                browser.attached_pages.remove_reservation(
                    &target_id,
                    reserved_generation,
                    &attach_lock,
                );
                return Err(RwError::Timeout(timeout.as_millis() as u64));
            }
        };
        let generation = match browser.attached_pages.claim_after_lock(
            &target_id,
            reserved_generation,
            &attach_lock,
        ) {
            AttachedPageClaim::Existing(page) => return Ok(page),
            AttachedPageClaim::Attach { generation } => generation,
            AttachedPageClaim::Retry => {
                drop(target_guard);
                continue;
            }
        };

        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            browser
                .attached_pages
                .remove_reservation(&target_id, generation, &attach_lock);
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let session_cleanup = Arc::new(AttachedSessionCleanup::new(Arc::clone(&browser)));
        let attach = attach_existing_page_unregistered(
            Arc::clone(&browser),
            target_id.clone(),
            context_id.clone(),
            generation,
            remaining,
            Arc::clone(&session_cleanup),
        );
        let attached_page = match tokio::time::timeout(remaining, attach).await {
            Ok(Ok(page)) => page,
            Ok(Err(error)) => {
                session_cleanup.detach().await;
                browser
                    .attached_pages
                    .remove_reservation(&target_id, generation, &attach_lock);
                return Err(error);
            }
            Err(_) => {
                session_cleanup.detach().await;
                browser
                    .attached_pages
                    .remove_reservation(&target_id, generation, &attach_lock);
                return Err(RwError::Timeout(timeout.as_millis() as u64));
            }
        };
        let UnregisteredPage {
            page,
            session_guard,
        } = attached_page;
        match browser
            .attached_pages
            .register(&target_id, &page, generation, &attach_lock)
        {
            AttachedPageRegistration::Registered => {
                session_guard.disarm();
                return Ok(page);
            }
            AttachedPageRegistration::Existing(existing) => {
                session_guard.detach().await;
                drop(page);
                return Ok(existing);
            }
            AttachedPageRegistration::ReservationLost => {
                session_guard.detach().await;
                drop(page);
                drop(target_guard);
            }
        }
    }
}

async fn attach_existing_page_unregistered(
    browser: Arc<BrowserInner>,
    target_id: String,
    context_id: Option<String>,
    registry_generation: u64,
    timeout: Duration,
    session_cleanup: Arc<AttachedSessionCleanup>,
) -> RwResult<UnregisteredPage> {
    let attached = browser
        .client
        .send(
            "Target.attachToTarget",
            json!({ "targetId": target_id, "flatten": true }),
            None,
            timeout,
        )
        .await?;
    let session_id = attached
        .get("sessionId")
        .and_then(Value::as_str)
        .ok_or_else(|| RwError::Message("CDP did not return a sessionId".to_string()))?
        .to_string();
    session_cleanup.set_session(session_id.clone());
    let session_guard = AttachedSessionGuard::new(session_cleanup);
    let event_stream_start_cursor = browser.client.event_cursor();
    try_join_all(
        ["Page.enable", "DOM.enable", "Network.enable"]
            .into_iter()
            .map(|method| {
                browser
                    .client
                    .send(method, json!({}), Some(&session_id), Duration::from_secs(5))
            }),
    )
    .await?;
    install_stealth_defaults(&browser, &session_id).await?;
    enable_page_iframe_auto_attach(&browser.client, &session_id, Duration::from_secs(5)).await?;
    let page_inner = Arc::new(PageInner {
        browser: Arc::clone(&browser),
        target_id,
        registry_generation,
        session_id: session_id.clone(),
        context_id,
        main_frame_id: Mutex::new(None),
        frame_state: Mutex::new(PageFrameState::new(session_id.clone())),
        network_requests: Arc::new(Mutex::new(NetworkRequestStore::new(
            event_stream_start_cursor,
        ))),
        event_stream_start_cursor,
        background_override_active: Arc::new(AtomicBool::new(false)),
        screenshot_lock: Arc::new(tokio::sync::Mutex::new(())),
        mouse_dispatch_lock: Arc::new(tokio::sync::Mutex::new(())),
        default_timeouts: Mutex::new(DefaultTimeoutRegister::default()),
        lifecycle: Arc::new(CloseLifecycle::new()),
        target_closed: AtomicBool::new(false),
        crashed: AtomicBool::new(false),
        close_target_on_drop: AtomicBool::new(false),
    });
    let _ = refresh_page_frame_tree(&page_inner, Duration::from_secs(5)).await;
    spawn_page_oopif_event_listener(Arc::downgrade(&page_inner));
    Ok(UnregisteredPage {
        page: page_inner,
        session_guard,
    })
}

#[cfg(feature = "python")]
async fn attach_existing_worker(
    browser: Arc<BrowserInner>,
    target_id: String,
    url: String,
    timeout: Duration,
) -> RwResult<PyWorker> {
    let attached = browser
        .client
        .send(
            "Target.attachToTarget",
            json!({ "targetId": target_id, "flatten": true }),
            None,
            timeout,
        )
        .await?;
    let session_id = attached
        .get("sessionId")
        .and_then(Value::as_str)
        .ok_or_else(|| RwError::Message("CDP did not return a worker sessionId".to_string()))?
        .to_string();
    install_worker_stealth_defaults(&browser.client, &session_id).await?;
    Ok(PyWorker {
        browser,
        target_id,
        session_id,
        url,
    })
}

async fn enable_page_iframe_auto_attach(
    client: &CdpClient,
    session_id: &str,
    timeout: Duration,
) -> RwResult<()> {
    client
        .send(
            "Target.setAutoAttach",
            json!({
                "autoAttach": true,
                "waitForDebuggerOnStart": false,
                "flatten": true,
                "filter": [
                    { "type": "iframe", "exclude": false },
                    { "exclude": true },
                ],
            }),
            Some(session_id),
            timeout,
        )
        .await?;
    Ok(())
}

async fn register_attached_iframe_session(
    page: Arc<PageInner>,
    frame_id: String,
    parent_frame_id: Option<String>,
    child_session_id: String,
    timeout: Duration,
) -> RwResult<()> {
    let client = Arc::clone(&page.browser.client);
    {
        let mut state = page.frame_state.lock().unwrap();
        state.record_session_for_frame(&frame_id, &child_session_id);
        state.record_frame(
            frame_id.clone(),
            parent_frame_id,
            None,
            None,
            child_session_id.clone(),
        );
    }
    if let Err(error) = enable_page_iframe_auto_attach(&client, &child_session_id, timeout).await {
        page.frame_state
            .lock()
            .unwrap()
            .record_frame_session_error_if_current(&frame_id, &child_session_id, error.to_string());
        return Err(error);
    }
    page.frame_state
        .lock()
        .unwrap()
        .mark_iframe_session_armed(&child_session_id);
    spawn_attached_iframe_session_setup(page, frame_id, child_session_id);
    Ok(())
}

fn spawn_attached_iframe_session_setup(
    page: Arc<PageInner>,
    frame_id: String,
    child_session_id: String,
) {
    let should_start = page
        .frame_state
        .lock()
        .unwrap()
        .mark_iframe_setup_started(&child_session_id);
    if !should_start {
        return;
    }
    tokio::spawn(async move {
        match setup_attached_iframe_session(Arc::clone(&page), child_session_id.clone()).await {
            Ok(()) => page
                .frame_state
                .lock()
                .unwrap()
                .mark_iframe_session_ready(&frame_id, &child_session_id),
            Err(error) => page
                .frame_state
                .lock()
                .unwrap()
                .record_frame_session_error_if_current(
                    &frame_id,
                    &child_session_id,
                    error.to_string(),
                ),
        }
    });
}

async fn setup_attached_iframe_session(
    page: Arc<PageInner>,
    child_session_id: String,
) -> RwResult<()> {
    let client = Arc::clone(&page.browser.client);
    try_join_all(
        ["Page.enable", "DOM.enable", "Network.enable"]
            .into_iter()
            .map(|method| {
                client.send(
                    method,
                    json!({}),
                    Some(&child_session_id),
                    Duration::from_secs(5),
                )
            }),
    )
    .await?;
    install_stealth_defaults(&page.browser, &child_session_id).await?;
    refresh_page_frame_tree(&page, Duration::from_secs(5)).await
}

fn spawn_page_oopif_event_listener(page: Weak<PageInner>) {
    let Some(initial_page) = page.upgrade() else {
        return;
    };
    let mut events = initial_page.browser.client.subscribe();
    drop(initial_page);
    tokio::spawn(async move {
        loop {
            let event = match events.recv().await {
                Ok(event) => event,
                Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(_) => break,
            };
            let Some(page) = page.upgrade() else {
                break;
            };
            if page.lifecycle.is_closed() {
                break;
            }
            handle_page_oopif_event(Arc::clone(&page), event).await;
        }
    });
}

async fn handle_page_oopif_event(page: Arc<PageInner>, event: Value) {
    let method = event.get("method").and_then(Value::as_str).unwrap_or("");
    if method == "Target.targetCrashed"
        && event.pointer("/params/targetId").and_then(Value::as_str)
            == Some(page.target_id.as_str())
    {
        page.crashed.store(true, Ordering::SeqCst);
        return;
    }
    if method == "Inspector.targetCrashed"
        && event.get("sessionId").and_then(Value::as_str) == Some(page.session_id.as_str())
    {
        page.crashed.store(true, Ordering::SeqCst);
        return;
    }
    if method == "Target.targetDestroyed"
        && event.pointer("/params/targetId").and_then(Value::as_str)
            == Some(page.target_id.as_str())
    {
        page.target_closed.store(true, Ordering::SeqCst);
        return;
    }
    if (method == "Inspector.detached"
        && event.get("sessionId").and_then(Value::as_str) == Some(page.session_id.as_str()))
        || (method == "Target.detachedFromTarget"
            && event.pointer("/params/sessionId").and_then(Value::as_str)
                == Some(page.session_id.as_str()))
    {
        page.target_closed.store(true, Ordering::SeqCst);
        return;
    }
    if method == "Target.attachedToTarget" {
        let parent_session_id = event.get("sessionId").and_then(Value::as_str);
        if !parent_session_id
            .map(|session_id| page.frame_state.lock().unwrap().owns_session(session_id))
            .unwrap_or(false)
        {
            return;
        }
        let Some(child_session_id) = event.pointer("/params/sessionId").and_then(Value::as_str)
        else {
            return;
        };
        let target_info = event.pointer("/params/targetInfo").unwrap_or(&Value::Null);
        let target_type = target_info
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("");
        if target_type != "iframe" {
            let _ = page
                .browser
                .client
                .send(
                    "Runtime.runIfWaitingForDebugger",
                    json!({}),
                    Some(child_session_id),
                    Duration::from_secs(1),
                )
                .await;
            return;
        }
        let Some(frame_id) = target_info.get("targetId").and_then(Value::as_str) else {
            return;
        };
        let parent_frame_id = target_info
            .get("parentFrameId")
            .and_then(Value::as_str)
            .map(ToString::to_string);
        let _ = register_attached_iframe_session(
            Arc::clone(&page),
            frame_id.to_string(),
            parent_frame_id,
            child_session_id.to_string(),
            Duration::from_secs(5),
        )
        .await;
        return;
    }

    if method == "Target.detachedFromTarget" {
        let Some(detached_session_id) = event.pointer("/params/sessionId").and_then(Value::as_str)
        else {
            return;
        };
        page.frame_state
            .lock()
            .unwrap()
            .detach_session(detached_session_id);
        return;
    }

    let Some(event_session_id) = event.get("sessionId").and_then(Value::as_str) else {
        return;
    };
    if !page
        .frame_state
        .lock()
        .unwrap()
        .owns_session(event_session_id)
    {
        return;
    }
    let params = event.get("params").unwrap_or(&Value::Null);
    match method {
        "Page.frameAttached" => {
            let Some(frame_id) = params.get("frameId").and_then(Value::as_str) else {
                return;
            };
            let parent_id = params
                .get("parentFrameId")
                .and_then(Value::as_str)
                .map(ToString::to_string);
            page.frame_state.lock().unwrap().record_frame(
                frame_id.to_string(),
                parent_id,
                None,
                None,
                event_session_id.to_string(),
            );
        }
        "Page.frameNavigated" => {
            let frame = params.get("frame").unwrap_or(&Value::Null);
            let Some(frame_id) = frame.get("id").and_then(Value::as_str) else {
                return;
            };
            let parent_id = frame
                .get("parentId")
                .and_then(Value::as_str)
                .map(ToString::to_string);
            let name = frame
                .get("name")
                .and_then(Value::as_str)
                .map(ToString::to_string);
            let url = frame
                .get("url")
                .and_then(Value::as_str)
                .map(ToString::to_string);
            if parent_id.is_none() && event_session_id == page.session_id {
                *page.main_frame_id.lock().unwrap() = Some(frame_id.to_string());
                page.crashed.store(false, Ordering::SeqCst);
            }
            page.frame_state.lock().unwrap().record_frame(
                frame_id.to_string(),
                parent_id,
                name,
                url,
                event_session_id.to_string(),
            );
        }
        "Page.navigatedWithinDocument" => {
            let Some(frame_id) = params.get("frameId").and_then(Value::as_str) else {
                return;
            };
            let url = params
                .get("url")
                .and_then(Value::as_str)
                .map(ToString::to_string);
            page.frame_state.lock().unwrap().record_frame(
                frame_id.to_string(),
                None,
                None,
                url,
                event_session_id.to_string(),
            );
        }
        "Page.frameDetached" => {
            let reason = params.get("reason").and_then(Value::as_str).unwrap_or("");
            if reason != "swap" {
                if let Some(frame_id) = params.get("frameId").and_then(Value::as_str) {
                    page.frame_state.lock().unwrap().remove_frame(frame_id);
                }
            }
        }
        _ => {}
    }
}

async fn refresh_page_frame_tree(page: &Arc<PageInner>, timeout: Duration) -> RwResult<()> {
    let deadline = OperationDeadline::new(timeout);
    let sessions = page.frame_state.lock().unwrap().session_ids();
    for session_id in sessions {
        let Ok(remaining) = deadline.remaining() else {
            break;
        };
        let result = page
            .browser
            .client
            .send("Page.getFrameTree", json!({}), Some(&session_id), remaining)
            .await;
        let Ok(tree) = result else {
            continue;
        };
        if let Some(root) = tree.get("frameTree") {
            let mut visited = HashSet::new();
            ingest_frame_tree_node(page, root, &session_id, &mut visited, 0);
        }
    }
    Ok(())
}

fn ingest_frame_tree_node(
    page: &Arc<PageInner>,
    node: &Value,
    session_id: &str,
    visited: &mut HashSet<String>,
    depth: usize,
) {
    let frame = node.get("frame").unwrap_or(&Value::Null);
    let Some(frame_id) = frame.get("id").and_then(Value::as_str) else {
        return;
    };
    if depth >= MAX_FRAME_TREE_DEPTH || !visited.insert(frame_id.to_string()) {
        return;
    }
    let parent_id = frame
        .get("parentId")
        .and_then(Value::as_str)
        .map(ToString::to_string);
    let name = frame
        .get("name")
        .and_then(Value::as_str)
        .map(ToString::to_string);
    let url = frame
        .get("url")
        .and_then(Value::as_str)
        .map(ToString::to_string);
    if parent_id.is_none() && session_id == page.session_id {
        *page.main_frame_id.lock().unwrap() = Some(frame_id.to_string());
    }
    page.frame_state.lock().unwrap().record_frame(
        frame_id.to_string(),
        parent_id,
        name,
        url,
        session_id.to_string(),
    );
    if let Some(children) = node.get("childFrames").and_then(Value::as_array) {
        for child in children {
            ingest_frame_tree_node(page, child, session_id, visited, depth + 1);
        }
    }
}

async fn install_stealth_defaults(browser: &BrowserInner, session_id: &str) -> RwResult<()> {
    let cached_override = browser.stealth_user_agent_override.lock().unwrap().clone();
    let user_agent_override = if let Some(cached_override) = cached_override {
        cached_override
    } else {
        let version = browser
            .client
            .send(
                "Browser.getVersion",
                json!({}),
                None,
                Duration::from_secs(5),
            )
            .await?;
        let override_value = version
            .get("userAgent")
            .and_then(Value::as_str)
            .map(|user_agent| {
                let user_agent = user_agent.replace("HeadlessChrome/", "Chrome/");
                let user_agent_metadata = stealth_user_agent_metadata(&user_agent, None);
                json!({
                    "userAgent": user_agent,
                    "acceptLanguage": "en-US,en",
                    "userAgentMetadata": user_agent_metadata,
                })
            })
            .unwrap_or(Value::Null);
        *browser.stealth_user_agent_override.lock().unwrap() = Some(override_value.clone());
        override_value
    };
    if !user_agent_override.is_null() {
        set_user_agent_override(
            &browser.client,
            session_id,
            user_agent_override,
            Duration::from_secs(5),
        )
        .await?;
    }

    let stealth_init_script = stealth_init_script();
    try_join_all([
        browser.client.send(
            "Page.addScriptToEvaluateOnNewDocument",
            json!({ "source": stealth_init_script.clone() }),
            Some(session_id),
            Duration::from_secs(5),
        ),
        browser.client.send(
            "Runtime.evaluate",
            json!({
                "expression": stealth_init_script,
                "awaitPromise": false,
                "returnByValue": true,
            }),
            Some(session_id),
            Duration::from_secs(5),
        ),
    ])
    .await?;
    Ok(())
}

async fn install_worker_stealth_defaults(client: &CdpClient, session_id: &str) -> RwResult<()> {
    let worker_stealth_init_script = worker_stealth_init_script();
    client
        .send(
            "Runtime.evaluate",
            json!({
                "expression": worker_stealth_init_script,
                "awaitPromise": false,
                "returnByValue": true,
            }),
            Some(session_id),
            Duration::from_secs(5),
        )
        .await?;
    Ok(())
}

fn start_service_worker_stealth_auto_attach(
    runtime: &tokio::runtime::Runtime,
    client: Arc<CdpClient>,
    timeout: Duration,
) -> RwResult<()> {
    start_service_worker_stealth_auto_attach_cancelable(runtime, client, timeout, None)
}

fn start_service_worker_stealth_auto_attach_cancelable(
    runtime: &tokio::runtime::Runtime,
    client: Arc<CdpClient>,
    timeout: Duration,
    cancel: Option<CancelToken>,
) -> RwResult<()> {
    runtime.block_on(cancelable(cancel, async {
        client
            .send(
                "Target.setAutoAttach",
                json!({
                    "autoAttach": true,
                    "waitForDebuggerOnStart": true,
                    "flatten": true,
                    "filter": [
                        { "type": "page", "exclude": true },
                        { "type": "iframe", "exclude": true },
                        { "type": "worker", "exclude": true },
                        { "type": "shared_worker", "exclude": true },
                        { "type": "background_page", "exclude": true },
                        { "type": "service_worker", "exclude": false },
                    ],
                }),
                None,
                timeout,
            )
            .await
            .map(|_| ())
    }))?;

    let mut events = client.subscribe();
    runtime.spawn(async move {
        loop {
            let event = match events.recv().await {
                Ok(event) => event,
                Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(_) => break,
            };
            if event.get("method").and_then(Value::as_str) != Some("Target.attachedToTarget") {
                continue;
            }
            let info = event.pointer("/params/targetInfo").unwrap_or(&Value::Null);
            let Some(session_id) = event.pointer("/params/sessionId").and_then(Value::as_str)
            else {
                continue;
            };
            if info.get("type").and_then(Value::as_str) != Some("service_worker") {
                let _ = client
                    .send(
                        "Runtime.runIfWaitingForDebugger",
                        json!({}),
                        Some(session_id),
                        Duration::from_secs(5),
                    )
                    .await;
                continue;
            }
            let _ = install_worker_stealth_defaults(&client, session_id).await;
            let _ = client
                .send(
                    "Runtime.runIfWaitingForDebugger",
                    json!({}),
                    Some(session_id),
                    Duration::from_secs(5),
                )
                .await;
        }
    });
    Ok(())
}

async fn set_user_agent_override(
    client: &CdpClient,
    session_id: &str,
    params: Value,
    timeout: Duration,
) -> RwResult<()> {
    client
        .send(
            "Network.setUserAgentOverride",
            params.clone(),
            Some(session_id),
            timeout,
        )
        .await?;
    client
        .send(
            "Emulation.setUserAgentOverride",
            params,
            Some(session_id),
            timeout,
        )
        .await?;
    Ok(())
}

fn chrome_full_version(user_agent: &str) -> String {
    user_agent
        .split("Chrome/")
        .nth(1)
        .and_then(|tail| tail.split_whitespace().next())
        .unwrap_or("120.0.0.0")
        .to_string()
}

fn chrome_major_version(user_agent: &str) -> String {
    chrome_full_version(user_agent)
        .split('.')
        .next()
        .unwrap_or("120")
        .to_string()
}

fn stealth_architecture() -> &'static str {
    if cfg!(target_arch = "aarch64") {
        "arm"
    } else {
        "x86"
    }
}

fn device_screen_orientation(width: i64, height: i64, mobile: bool) -> Value {
    let landscape = width > height;
    if mobile && !landscape {
        json!({ "angle": 0, "type": "portraitPrimary" })
    } else if mobile && landscape {
        json!({ "angle": 90, "type": "landscapePrimary" })
    } else {
        json!({ "angle": 0, "type": "landscapePrimary" })
    }
}

fn stealth_user_agent_metadata(user_agent: &str, mobile: Option<bool>) -> Value {
    let major_version = chrome_major_version(user_agent);
    let full_version = chrome_full_version(user_agent);
    let platform = if user_agent.contains("Windows") {
        "Windows"
    } else if user_agent.contains("Android") {
        "Android"
    } else if user_agent.contains("iPhone")
        || user_agent.contains("iPad")
        || user_agent.contains("iPod")
    {
        "iOS"
    } else if user_agent.contains("Mac OS X") {
        "macOS"
    } else if user_agent.contains("Linux") || user_agent.contains("X11") {
        "Linux"
    } else {
        ""
    };
    let mobile = mobile.unwrap_or_else(|| {
        user_agent.contains("Mobile")
            || user_agent.contains("Android")
            || user_agent.contains("iPhone")
            || user_agent.contains("iPad")
            || user_agent.contains("iPod")
    });
    json!({
        "brands": [
            { "brand": "Chromium", "version": major_version },
            { "brand": "Google Chrome", "version": major_version },
            { "brand": "Not A(Brand", "version": "24" }
        ],
        "fullVersionList": [
            { "brand": "Chromium", "version": full_version },
            { "brand": "Google Chrome", "version": full_version },
            { "brand": "Not A(Brand", "version": "24.0.0.0" }
        ],
        "platform": platform,
        "platformVersion": "",
        "architecture": stealth_architecture(),
        "model": "",
        "mobile": mobile,
        "bitness": "64"
    })
}

fn stealth_init_script() -> String {
    STEALTH_INIT_SCRIPT_TEMPLATE.replace("__UA_ARCHITECTURE__", stealth_architecture())
}

fn worker_stealth_init_script() -> String {
    WORKER_STEALTH_INIT_SCRIPT_TEMPLATE.replace("__UA_ARCHITECTURE__", stealth_architecture())
}

const STEALTH_INIT_SCRIPT_TEMPLATE: &str = r#"
(() => {
  try {
    delete Navigator.prototype.webdriver;
  } catch (_) {
    try {
      delete navigator.webdriver;
    } catch (_) {}
  }
  if ('webdriver' in navigator) {
    try {
      Object.defineProperty(Navigator.prototype, 'webdriver', {
        get: () => undefined,
        configurable: true
      });
    } catch (_) {
      try {
        Object.defineProperty(navigator, 'webdriver', {
          get: () => undefined,
          configurable: true
        });
      } catch (_) {}
    }
  }
  try {
    const chromeObject = {
      app: {
        isInstalled: false,
        InstallState: {
          DISABLED: 'disabled',
          INSTALLED: 'installed',
          NOT_INSTALLED: 'not_installed'
        },
        RunningState: {
          CANNOT_RUN: 'cannot_run',
          READY_TO_RUN: 'ready_to_run',
          RUNNING: 'running'
        },
        getDetails: () => null,
        getIsInstalled: () => false,
        installState: () => 'not_installed',
        runningState: () => 'cannot_run'
      },
      csi: () => ({
        onloadT: Date.now(),
        pageT: Math.max(0, Math.round(performance.now())),
        startE: Date.now() - Math.max(0, Math.round(performance.now())),
        tran: 15
      }),
      loadTimes: () => ({
        commitLoadTime: 0,
        connectionInfo: 'h2',
        finishDocumentLoadTime: 0,
        finishLoadTime: 0,
        firstPaintAfterLoadTime: 0,
        firstPaintTime: 0,
        navigationType: 'Other',
        npnNegotiatedProtocol: 'h2',
        requestTime: Date.now() / 1000,
        startLoadTime: 0,
        wasAlternateProtocolAvailable: false,
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true
      }),
      runtime: {
        OnInstalledReason: {
          CHROME_UPDATE: 'chrome_update',
          INSTALL: 'install',
          SHARED_MODULE_UPDATE: 'shared_module_update',
          UPDATE: 'update'
        },
        OnRestartRequiredReason: {
          APP_UPDATE: 'app_update',
          OS_UPDATE: 'os_update',
          PERIODIC: 'periodic'
        },
        PlatformArch: {
          ARM: 'arm',
          ARM64: 'arm64',
          MIPS: 'mips',
          MIPS64: 'mips64',
          X86_32: 'x86-32',
          X86_64: 'x86-64'
        },
        PlatformNaclArch: {
          ARM: 'arm',
          MIPS: 'mips',
          MIPS64: 'mips64',
          X86_32: 'x86-32',
          X86_64: 'x86-64'
        },
        PlatformOs: {
          ANDROID: 'android',
          CROS: 'cros',
          LINUX: 'linux',
          MAC: 'mac',
          OPENBSD: 'openbsd',
          WIN: 'win'
        },
        RequestUpdateCheckStatus: {
          NO_UPDATE: 'no_update',
          THROTTLED: 'throttled',
          UPDATE_AVAILABLE: 'update_available'
        },
        connect: () => undefined,
        getManifest: () => undefined,
        getURL: path => String(path || ''),
        sendMessage: () => undefined
      }
    };
    if (!window.chrome) {
      Object.defineProperty(window, 'chrome', {
        get: () => chromeObject,
        configurable: false
      });
    }
  } catch (_) {}
  try {
    const historyName = '__console_history__';
    const wrappedName = '__console_history_wrapped__';
    if (!Array.isArray(console[historyName])) {
      Object.defineProperty(console, historyName, {
        value: [],
        configurable: false,
        enumerable: false,
        writable: false
      });
    }
    const history = console[historyName];
    const textFor = value => {
      try {
        if (typeof value === 'string') return value;
        if (value === null) return 'null';
        if (value === undefined) return 'undefined';
        return String(value);
      } catch (_) {
        return '';
      }
    };
    const valueFor = value => {
      try {
        if (value === null || value === undefined) return value;
        const type = typeof value;
        if (type === 'string' || type === 'number' || type === 'boolean') return value;
        return textFor(value);
      } catch (_) {
        return '';
      }
    };
    const typeFor = method => method === 'warn' ? 'warning' : method;
    for (const method of ['debug', 'error', 'info', 'log', 'warn']) {
      const original = console[method];
      if (typeof original !== 'function' || original[wrappedName]) continue;
      const wrapped = new Proxy(original, {
        apply(target, thisArg, args) {
          try {
            history.push({
              type: typeFor(method),
              text: Array.from(args || []).map(textFor).join(' '),
              timestamp: Date.now(),
              args: Array.from(args || []).map(valueFor),
              location: {
                url: String(location && location.href || ''),
                lineNumber: 0,
                columnNumber: 0
              }
            });
            if (history.length > 200) history.splice(0, history.length - 200);
          } catch (_) {}
          return Reflect.apply(target, thisArg, args);
        }
      });
      try { Object.defineProperty(wrapped, wrappedName, { value: true }); } catch (_) {}
      try { Object.defineProperty(console, method, { value: wrapped, configurable: true, writable: true }); } catch (_) {}
    }
  } catch (_) {}
  try {
    const historyName = '__page_error_history__';
    const errorHandlerName = '__page_error_history_error_handler__';
    const rejectionHandlerName = '__page_error_history_rejection_handler__';
    const ensureHistory = target => {
      if (!target) return null;
      if (!Array.isArray(target[historyName])) {
        try {
          Object.defineProperty(target, historyName, {
            value: [],
            configurable: false,
            enumerable: false,
            writable: false
          });
        } catch (_) {}
      }
      return Array.isArray(target[historyName]) ? target[historyName] : null;
    };
    const localHistory = ensureHistory(window);
    const topHistory = (() => {
      try {
        return window.top && window.top !== window ? ensureHistory(window.top) : localHistory;
      } catch (_) {
        return localHistory;
      }
    })();
    const histories = [];
    if (localHistory) histories.push(localHistory);
    if (topHistory && histories.indexOf(topHistory) === -1) histories.push(topHistory);
    const textFor = value => {
      try {
        if (value === null) return 'null';
        if (value === undefined) return '';
        return String(value);
      } catch (_) {
        return '';
      }
    };
    const dataProperty = (value, name) => {
      try {
        if (!value || (typeof value !== 'object' && typeof value !== 'function')) return '';
        let current = value;
        for (let depth = 0; current && depth < 4; depth += 1) {
          const descriptor = Object.getOwnPropertyDescriptor(current, name);
          if (descriptor) {
            if ('value' in descriptor) return textFor(descriptor.value);
            return '';
          }
          current = Object.getPrototypeOf(current);
        }
      } catch (_) {}
      return '';
    };
    const pushRecord = record => {
      try {
        for (const history of histories) {
          history.push(record);
          if (history.length > 200) history.splice(0, history.length - 200);
        }
      } catch (_) {}
    };
    const recordFor = (value, fallbackMessage, fallbackName) => {
      const message = dataProperty(value, 'message') || textFor(fallbackMessage || value) || 'Page error';
      const name = dataProperty(value, 'name') || fallbackName || 'Error';
      const stack = dataProperty(value, 'stack');
      return {
        type: 'error',
        name,
        message,
        stack,
        timestamp: Date.now(),
        location: {
          url: String(location && location.href || ''),
          lineNumber: 0,
          columnNumber: 0
        }
      };
    };
    const errorHandler = event => {
      try {
        const record = recordFor(event && event.error, event && event.message, 'Error');
        record.location = {
          url: String(event && event.filename || location && location.href || ''),
          lineNumber: Number(event && event.lineno || 0) || 0,
          columnNumber: Number(event && event.colno || 0) || 0
        };
        pushRecord(record);
      } catch (_) {}
    };
    const rejectionHandler = event => {
      try {
        pushRecord(recordFor(event && event.reason, event && event.reason, 'UnhandledRejection'));
      } catch (_) {}
    };
    try {
      if (typeof window[errorHandlerName] === 'function') {
        window.removeEventListener('error', window[errorHandlerName], true);
      }
    } catch (_) {}
    try {
      if (typeof window[rejectionHandlerName] === 'function') {
        window.removeEventListener('unhandledrejection', window[rejectionHandlerName], true);
      }
    } catch (_) {}
    try { Object.defineProperty(window, errorHandlerName, { value: errorHandler, configurable: true, enumerable: false, writable: true }); } catch (_) {}
    try { Object.defineProperty(window, rejectionHandlerName, { value: rejectionHandler, configurable: true, enumerable: false, writable: true }); } catch (_) {}
    window.addEventListener('error', errorHandler, true);
    window.addEventListener('unhandledrejection', rejectionHandler, true);
  } catch (_) {}
  try {
    const workerMarker = Symbol.for('nativeWorkerIdentityWrapped');
    const NativeWorker = window.Worker;
    if (typeof NativeWorker === 'function' && !NativeWorker[workerMarker]) {
      const makeWorkerIdentitySource = () => {
        const ua = String(navigator.userAgent || '');
        const appVersion = String(navigator.appVersion || '');
        const platform = String(navigator.platform || '');
        const language = String(navigator.language || 'en-US');
        const languages = Array.from(navigator.languages || [language]).map(String);
        const chromeFullVersion = (ua.match(/Chrome\/([^\s]+)/) || [])[1] || '';
        const fullVersionForBrand = brand => {
          const name = String(brand.brand || '');
          const version = String(brand.version || '');
          if ((name === 'Chromium' || name === 'Google Chrome') && chromeFullVersion) return chromeFullVersion;
          if (name === 'Not A(Brand') return '24.0.0.0';
          return version;
        };
        const mapBrand = brand => ({
          brand: String(brand.brand || ''),
          version: String(brand.version || '')
        });
        const mapFullVersionBrand = brand => ({
          brand: String(brand.brand || ''),
          version: fullVersionForBrand(brand)
        });
        const uaBrands = navigator.userAgentData ? Array.from(navigator.userAgentData.brands || []).map(mapBrand) : [];
        const uaData = navigator.userAgentData ? {
          brands: uaBrands,
          fullVersionList: uaBrands.map(mapFullVersionBrand),
          mobile: !!navigator.userAgentData.mobile,
          platform: String(navigator.userAgentData.platform || platform),
          architecture: '__UA_ARCHITECTURE__'
        } : null;
        const uaDataJson = JSON.stringify(uaData);
        return [
          '(() => {',
          '  const defineNavigatorValue = (name, value) => {',
          '    try { Object.defineProperty(Object.getPrototypeOf(navigator), name, { get: () => value, configurable: true }); } catch (_) {}',
          '    try { Object.defineProperty(navigator, name, { get: () => value, configurable: true }); } catch (_) {}',
          '  };',
          `  defineNavigatorValue('userAgent', ${JSON.stringify(ua)});`,
          `  defineNavigatorValue('appVersion', ${JSON.stringify(appVersion)});`,
          `  defineNavigatorValue('platform', ${JSON.stringify(platform)});`,
          `  defineNavigatorValue('language', ${JSON.stringify(language)});`,
          `  defineNavigatorValue('languages', ${JSON.stringify(languages)});`,
          '  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (_) {}',
          '  try { delete navigator.webdriver; } catch (_) {}',
          `  const uaData = ${uaDataJson};`,
          '  if (uaData) {',
          '    const data = {',
          '      brands: uaData.brands,',
          '      mobile: uaData.mobile,',
          '      platform: uaData.platform,',
          '      getHighEntropyValues: async hints => {',
          '        const values = { brands: uaData.brands, mobile: uaData.mobile, platform: uaData.platform };',
          '        for (const hint of hints || []) {',
          "          if (hint === 'fullVersionList') values.fullVersionList = uaData.fullVersionList;",
          "          if (hint === 'architecture') values.architecture = uaData.architecture;",
          "          if (hint === 'bitness') values.bitness = '64';",
          "          if (hint === 'model') values.model = '';",
          "          if (hint === 'platformVersion') values.platformVersion = '';",
          '        }',
          '        return values;',
          '      },',
          '      toJSON: () => ({ brands: uaData.brands, mobile: uaData.mobile, platform: uaData.platform })',
          '    };',
          "    defineNavigatorValue('userAgentData', data);",
          '  }',
          '})();'
        ].join('\n');
      };
      const WrappedWorker = function(scriptURL, options) {
        try {
          const workerOptions = options || {};
          const absoluteUrl = new URL(String(scriptURL), location.href).href;
          const identitySource = makeWorkerIdentitySource();
          const source = workerOptions.type === 'module'
            ? `${identitySource}\nimport ${JSON.stringify(absoluteUrl)};`
            : `${identitySource}\nimportScripts(${JSON.stringify(absoluteUrl)});`;
          const blobUrl = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
          return new NativeWorker(blobUrl, workerOptions);
        } catch (_) {
          return new NativeWorker(scriptURL, options);
        }
      };
      WrappedWorker.prototype = NativeWorker.prototype;
      try { Object.setPrototypeOf(WrappedWorker, NativeWorker); } catch (_) {}
      try { Object.defineProperty(WrappedWorker, 'name', { value: 'Worker', configurable: true }); } catch (_) {}
      try { Object.defineProperty(WrappedWorker, 'toString', { value: () => 'function Worker() { [native code] }', configurable: true }); } catch (_) {}
      try { Object.defineProperty(WrappedWorker, workerMarker, { value: true }); } catch (_) {}
      Object.defineProperty(window, 'Worker', {
        value: WrappedWorker,
        configurable: true,
        writable: true
      });
    }
  } catch (_) {}
})();
"#;

const WORKER_STEALTH_INIT_SCRIPT_TEMPLATE: &str = r#"
(() => {
  try {
    const nav = self.navigator;
    if (!nav) return;
    const clean = value => String(value || '').replace(/HeadlessChrome\//g, 'Chrome/');
    const defineNavigatorValue = (name, value) => {
      try { Object.defineProperty(Object.getPrototypeOf(nav), name, { get: () => value, configurable: true }); } catch (_) {}
      try { Object.defineProperty(nav, name, { get: () => value, configurable: true }); } catch (_) {}
    };
    const userAgent = clean(nav.userAgent);
    const appVersion = clean(nav.appVersion);
    defineNavigatorValue('userAgent', userAgent);
    defineNavigatorValue('appVersion', appVersion);
    if (!nav.language) defineNavigatorValue('language', 'en-US');
    if (!nav.languages || !nav.languages.length) defineNavigatorValue('languages', ['en-US', 'en']);
    try { delete Object.getPrototypeOf(nav).webdriver; } catch (_) {}
    try { delete nav.webdriver; } catch (_) {}
    if ('webdriver' in nav) {
      defineNavigatorValue('webdriver', undefined);
    }
    if (nav.userAgentData) {
      const chromeFullVersion = (userAgent.match(/Chrome\/([^\s]+)/) || [])[1] || '';
      const cleanBrand = brand => ({
        brand: String(brand && brand.brand || '').replace(/HeadlessChrome/g, 'Google Chrome'),
        version: String(brand && brand.version || '')
      });
      const fullVersionBrand = brand => {
        const cleaned = cleanBrand(brand);
        if ((cleaned.brand === 'Chromium' || cleaned.brand === 'Google Chrome') && chromeFullVersion) {
          cleaned.version = chromeFullVersion;
        } else if (cleaned.brand === 'Not A(Brand') {
          cleaned.version = '24.0.0.0';
        }
        return cleaned;
      };
      const brands = Array.from(nav.userAgentData.brands || []).map(cleanBrand);
      const fullVersionList = Array.from(nav.userAgentData.fullVersionList || brands).map(fullVersionBrand);
      const data = {
        brands,
        mobile: !!nav.userAgentData.mobile,
        platform: String(nav.userAgentData.platform || nav.platform || ''),
        architecture: '__UA_ARCHITECTURE__',
        getHighEntropyValues: async hints => {
          const values = { brands, mobile: data.mobile, platform: data.platform };
          for (const hint of hints || []) {
            if (hint === 'fullVersionList') values.fullVersionList = fullVersionList;
            if (hint === 'architecture') values.architecture = data.architecture;
            if (hint === 'bitness') values.bitness = '64';
            if (hint === 'model') values.model = '';
            if (hint === 'platformVersion') values.platformVersion = '';
          }
          return values;
        },
        toJSON: () => ({ brands, mobile: data.mobile, platform: data.platform })
      };
      defineNavigatorValue('userAgentData', data);
    }
  } catch (_) {}
})();
"#;

fn chromium_pipe_transport_requested() -> RwResult<bool> {
    match env::var("RUSTWRIGHT_CDP_TRANSPORT") {
        Ok(value) if value == "pipe" => {
            #[cfg(unix)]
            {
                Ok(true)
            }
            #[cfg(not(unix))]
            {
                Err(RwError::Message(
                    "RUSTWRIGHT_CDP_TRANSPORT=pipe is only supported on Unix hosts".to_string(),
                ))
            }
        }
        Ok(value) if value.is_empty() || value == "websocket" => Ok(false),
        Ok(value) => Err(RwError::Message(format!(
            "unsupported RUSTWRIGHT_CDP_TRANSPORT value {value:?}; expected 'websocket' or 'pipe'"
        ))),
        Err(_) => Ok(false),
    }
}

#[cfg(unix)]
struct ChromiumPipeFds {
    parent_read: RawFd,
    parent_write: RawFd,
    child_read: RawFd,
    child_write: RawFd,
}

#[cfg(unix)]
impl ChromiumPipeFds {
    fn close_all(self) {
        close_raw_fd(self.parent_read);
        close_raw_fd(self.parent_write);
        close_raw_fd(self.child_read);
        close_raw_fd(self.child_write);
    }

    fn into_parent_files(self) -> (fs::File, fs::File) {
        close_raw_fd(self.child_read);
        close_raw_fd(self.child_write);
        let read = unsafe { fs::File::from_raw_fd(self.parent_read) };
        let write = unsafe { fs::File::from_raw_fd(self.parent_write) };
        (read, write)
    }
}

#[cfg(unix)]
fn close_raw_fd(fd: RawFd) {
    if fd >= 0 {
        unsafe {
            libc::close(fd);
        }
    }
}

#[cfg(unix)]
fn create_pipe_pair() -> RwResult<[RawFd; 2]> {
    let mut fds = [-1, -1];
    let result = unsafe { libc::pipe(fds.as_mut_ptr()) };
    if result == -1 {
        return Err(RwError::Io(std::io::Error::last_os_error()));
    }
    Ok(fds)
}

#[cfg(unix)]
fn create_chromium_pipe_fds() -> RwResult<ChromiumPipeFds> {
    let to_child = create_pipe_pair()?;
    let from_child = match create_pipe_pair() {
        Ok(fds) => fds,
        Err(error) => {
            close_raw_fd(to_child[0]);
            close_raw_fd(to_child[1]);
            return Err(error);
        }
    };
    Ok(ChromiumPipeFds {
        child_read: to_child[0],
        parent_write: to_child[1],
        parent_read: from_child[0],
        child_write: from_child[1],
    })
}

#[cfg(unix)]
fn duplicate_fd_at_or_above(fd: RawFd, minimum: RawFd) -> std::io::Result<RawFd> {
    let result = unsafe { libc::fcntl(fd, libc::F_DUPFD, minimum) };
    if result == -1 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(result)
    }
}

#[cfg(unix)]
fn install_chromium_pipe_fds(
    child_read: RawFd,
    child_write: RawFd,
    parent_read: RawFd,
    parent_write: RawFd,
) -> std::io::Result<()> {
    let mut read_fd = child_read;
    let mut write_fd = child_write;
    let mut duplicated = Vec::new();
    if read_fd == 4 {
        read_fd = duplicate_fd_at_or_above(read_fd, 5)?;
        duplicated.push(read_fd);
    }
    if write_fd == 3 {
        write_fd = duplicate_fd_at_or_above(write_fd, 5)?;
        duplicated.push(write_fd);
    }
    if unsafe { libc::dup2(read_fd, 3) } == -1 {
        return Err(std::io::Error::last_os_error());
    }
    if unsafe { libc::dup2(write_fd, 4) } == -1 {
        return Err(std::io::Error::last_os_error());
    }
    for fd in [
        child_read,
        child_write,
        parent_read,
        parent_write,
        *duplicated.first().unwrap_or(&-1),
        *duplicated.get(1).unwrap_or(&-1),
    ] {
        if fd != 3 && fd != 4 {
            close_raw_fd(fd);
        }
    }
    Ok(())
}

fn launch_chromium_process(
    options: &LaunchOptions,
    runtime: &tokio::runtime::Runtime,
    timeout: Duration,
    cancelled: Option<Arc<AtomicBool>>,
) -> RwResult<(Child, Option<TempDir>, LaunchedCdpTransport, bool)> {
    let executable = find_chromium_executable(
        options.executable_path.as_deref(),
        options.channel.as_deref(),
        options.headless,
    )
        .ok_or_else(|| {
            let detail = options
                .channel
                .as_deref()
                .map(|channel| format!(" for channel {channel:?}"))
                .unwrap_or_default();
            RwError::Message(format!(
                "Could not find a Chromium executable{detail}. Set executable_path or RUSTWRIGHT_CHROMIUM."
            ))
        })?;
    let user_debugging_port = remote_debugging_port_from_args(&options.args)?;
    let use_pipe_transport = chromium_pipe_transport_requested()?;
    if use_pipe_transport && user_debugging_port.is_some() {
        return Err(RwError::Message(
            "RUSTWRIGHT_CDP_TRANSPORT=pipe cannot be combined with --remote-debugging-port launch args"
                .to_string(),
        ));
    }
    let dynamic_debugging_port = user_debugging_port == Some(0);
    let port = match user_debugging_port {
        Some(0) => 0,
        Some(port) => port,
        None => pick_unused_port()?,
    };
    let (profile_arg, profile_dir) = if let Some(user_data_dir) = &options.user_data_dir {
        (PathBuf::from(user_data_dir), None)
    } else {
        let temp_dir = tempfile::Builder::new()
            .prefix("rustwright-profile-")
            .tempdir()?;
        (temp_dir.path().to_path_buf(), Some(temp_dir))
    };

    match launch_chromium_attempt(
        &executable,
        options,
        &profile_arg,
        port,
        runtime,
        timeout,
        false,
        user_debugging_port.is_some(),
        dynamic_debugging_port,
        use_pipe_transport,
        cancelled.clone(),
    ) {
        Ok((child, transport)) => return Ok((child, profile_dir, transport, false)),
        Err(error) if should_retry_chromium_single_process(options, &error) => {
            match launch_chromium_attempt(
                &executable,
                options,
                &profile_arg,
                port,
                runtime,
                timeout,
                true,
                user_debugging_port.is_some(),
                dynamic_debugging_port,
                use_pipe_transport,
                cancelled,
            ) {
                Ok((child, transport)) => return Ok((child, profile_dir, transport, true)),
                Err(retry_error) => {
                    return Err(RwError::Message(format!(
                        "{error}\nRetrying with --single-process also failed: {retry_error}"
                    )));
                }
            }
        }
        Err(error) => return Err(error),
    }
}

fn launch_chromium_attempt(
    executable: &Path,
    options: &LaunchOptions,
    profile_arg: &Path,
    port: u16,
    runtime: &tokio::runtime::Runtime,
    timeout: Duration,
    single_process_fallback: bool,
    user_supplied_debugging_port: bool,
    dynamic_debugging_port: bool,
    use_pipe_transport: bool,
    cancelled: Option<Arc<AtomicBool>>,
) -> RwResult<(Child, LaunchedCdpTransport)> {
    let stderr_file = NamedTempFile::new()?;
    let mut command = Command::new(executable);
    #[cfg(unix)]
    let cdp_pipes = if use_pipe_transport {
        Some(create_chromium_pipe_fds()?)
    } else {
        None
    };
    command
        .arg(format!("--user-data-dir={}", profile_arg.to_string_lossy()))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr_file.reopen()?));
    if use_pipe_transport {
        command.arg("--remote-debugging-pipe");
    } else if !user_supplied_debugging_port {
        command.arg(format!("--remote-debugging-port={port}"));
    }
    #[cfg(not(unix))]
    if use_pipe_transport {
        return Err(RwError::Message(
            "RUSTWRIGHT_CDP_TRANSPORT=pipe is only supported on Unix hosts".to_string(),
        ));
    }
    #[cfg(unix)]
    if let Some(pipes) = cdp_pipes.as_ref() {
        let child_read = pipes.child_read;
        let child_write = pipes.child_write;
        let parent_read = pipes.parent_read;
        let parent_write = pipes.parent_write;
        unsafe {
            command.pre_exec(move || {
                install_chromium_pipe_fds(child_read, child_write, parent_read, parent_write)
            });
        }
    }

    let mut default_args = vec![
        "--no-first-run".to_string(),
        "--no-default-browser-check".to_string(),
        "--disable-background-networking".to_string(),
        "--disable-background-timer-throttling".to_string(),
        "--disable-dev-shm-usage".to_string(),
        "--disable-blink-features=AutomationControlled".to_string(),
        "--disable-renderer-backgrounding".to_string(),
        "--disable-popup-blocking".to_string(),
        "--disable-prompt-on-repost".to_string(),
        "--enable-features=CDPScreenshotNewSurface".to_string(),
        "--mute-audio".to_string(),
    ];
    if options.headless {
        default_args.push("--headless=new".to_string());
        default_args.push("--hide-scrollbars".to_string());
    }
    if !options.chromium_sandbox {
        default_args.push("--no-sandbox".to_string());
    }
    if !options.ignore_all_default_args {
        for arg in default_args {
            if !launch_default_arg_ignored(&arg, &options.ignore_default_args) {
                command.arg(arg);
            }
        }
    }
    for (key, value) in &options.env {
        command.env(key, value);
    }
    if let Some(proxy) = &options.proxy {
        command.arg(format!("--proxy-server={}", proxy_server(proxy)?));
        if let Some(bypass) = normalized_proxy_bypass(proxy) {
            command.arg(format!("--proxy-bypass-list={bypass}"));
        }
    }
    for arg in &options.args {
        command.arg(arg);
    }
    if single_process_fallback && !has_chromium_arg(&options.args, "--single-process") {
        command.arg("--single-process");
    }
    command.arg("about:blank");

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            #[cfg(unix)]
            if let Some(pipes) = cdp_pipes {
                pipes.close_all();
            }
            return Err(RwError::Io(error));
        }
    };
    if launch_was_cancelled(cancelled.as_ref()) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(RwError::Message("browser launch was cancelled".to_string()));
    }
    #[cfg(unix)]
    let pipe_transport = if let Some(pipes) = cdp_pipes {
        Some(pipes.into_parent_files())
    } else {
        None
    };
    #[cfg(unix)]
    if let Some((read, write)) = pipe_transport {
        return Ok((child, LaunchedCdpTransport::Pipe { read, write }));
    }
    let ws_endpoint_result = if dynamic_debugging_port {
        runtime.block_on(async {
            poll_ws_endpoint_from_devtools_active_port(
                profile_arg,
                timeout,
                &mut child,
                stderr_file.path(),
                cancelled.clone(),
            )
            .await
        })
    } else {
        let version_url = format!("http://127.0.0.1:{port}/json/version");
        runtime.block_on(async {
            poll_ws_endpoint(
                &version_url,
                timeout,
                &mut child,
                stderr_file.path(),
                cancelled,
            )
            .await
        })
    };
    let ws_endpoint = match ws_endpoint_result {
        Ok(endpoint) => endpoint,
        Err(error) => {
            if child.try_wait()?.is_none() {
                let _ = child.kill();
                let _ = child.wait();
            }
            return Err(error);
        }
    };
    Ok((child, LaunchedCdpTransport::WebSocket(ws_endpoint)))
}

fn launch_default_arg_ignored(arg: &str, ignored: &[String]) -> bool {
    ignored
        .iter()
        .any(|value| arg == value || arg.starts_with(&format!("{value}=")))
}

fn has_chromium_arg(args: &[String], name: &str) -> bool {
    args.iter()
        .any(|arg| arg == name || arg.starts_with(&format!("{name}=")))
}

fn remote_debugging_port_from_args(args: &[String]) -> RwResult<Option<u16>> {
    let mut selected: Option<u16> = None;
    for (index, arg) in args.iter().enumerate() {
        if arg == "--remote-debugging-port" {
            let value = args.get(index + 1).ok_or_else(|| {
                RwError::Message(
                    "remote-debugging-port requires a TCP port between 1 and 65535".to_string(),
                )
            })?;
            selected = Some(parse_remote_debugging_port(value)?);
        } else if let Some(value) = arg.strip_prefix("--remote-debugging-port=") {
            selected = Some(parse_remote_debugging_port(value)?);
        }
    }
    Ok(selected)
}

fn parse_remote_debugging_port(value: &str) -> RwResult<u16> {
    let port: u16 = value.parse().map_err(|_| {
        RwError::Message(format!(
            "remote-debugging-port must be a TCP port between 1 and 65535, got {value:?}"
        ))
    })?;
    Ok(port)
}

fn should_retry_chromium_single_process(options: &LaunchOptions, error: &RwError) -> bool {
    let error = error.to_string();
    options.headless
        && !has_chromium_arg(&options.args, "--single-process")
        && ((cfg!(target_os = "macos")
            && (error.contains("MachPortRendezvousServer")
                || error.contains("sandbox_parameters_mac.mm")))
            || (cfg!(target_os = "linux")
                && (error.contains("signal: 11") || error.contains("SIGSEGV"))))
}

async fn poll_ws_endpoint(
    version_url: &str,
    timeout: Duration,
    child: &mut Child,
    stderr_path: &Path,
    cancelled: Option<Arc<AtomicBool>>,
) -> RwResult<String> {
    let deadline = tokio::time::Instant::now() + timeout;
    let mut last_error: Option<RwError> = None;
    loop {
        if launch_was_cancelled(cancelled.as_ref()) {
            return Err(RwError::Message("browser launch was cancelled".to_string()));
        }
        if let Some(status) = child.try_wait()? {
            return Err(RwError::Message(chromium_launch_failure_message(
                Some(status),
                last_error.as_ref(),
                stderr_path,
            )));
        }
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Message(chromium_launch_failure_message(
                None,
                last_error.as_ref(),
                stderr_path,
            )));
        }
        let request_timeout = std::cmp::min(Duration::from_secs(2), deadline - now);
        match resolve_ws_endpoint(version_url, request_timeout, &[]).await {
            Ok(endpoint) => return Ok(endpoint),
            Err(error) => {
                if tokio::time::Instant::now() >= deadline {
                    last_error = Some(error);
                    return Err(RwError::Message(chromium_launch_failure_message(
                        None,
                        last_error.as_ref(),
                        stderr_path,
                    )));
                }
                last_error = Some(error);
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        }
    }
}

async fn poll_ws_endpoint_from_devtools_active_port(
    profile_arg: &Path,
    timeout: Duration,
    child: &mut Child,
    stderr_path: &Path,
    cancelled: Option<Arc<AtomicBool>>,
) -> RwResult<String> {
    let deadline = tokio::time::Instant::now() + timeout;
    let active_port_file = profile_arg.join("DevToolsActivePort");
    let mut last_error: Option<RwError> = None;
    loop {
        if launch_was_cancelled(cancelled.as_ref()) {
            return Err(RwError::Message("browser launch was cancelled".to_string()));
        }
        if let Some(status) = child.try_wait()? {
            return Err(RwError::Message(chromium_launch_failure_message(
                Some(status),
                last_error.as_ref(),
                stderr_path,
            )));
        }
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Message(chromium_launch_failure_message(
                None,
                last_error.as_ref(),
                stderr_path,
            )));
        }
        match fs::read_to_string(&active_port_file) {
            Ok(contents) => {
                let mut lines = contents.lines();
                let port = lines.next().map(str::trim).filter(|line| !line.is_empty());
                let path = lines.next().map(str::trim).filter(|line| !line.is_empty());
                if let (Some(port), Some(path)) = (port, path) {
                    if port.parse::<u16>().is_ok() {
                        let separator = if path.starts_with('/') { "" } else { "/" };
                        return Ok(format!("ws://127.0.0.1:{port}{separator}{path}"));
                    }
                    last_error = Some(RwError::Message(format!(
                        "DevToolsActivePort contained invalid port {port:?}"
                    )));
                } else {
                    last_error = Some(RwError::Message(
                        "DevToolsActivePort did not contain both port and browser path".to_string(),
                    ));
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                last_error = Some(RwError::Message(format!(
                    "Could not read DevToolsActivePort: {error}"
                )));
            }
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

fn chromium_launch_failure_message(
    status: Option<ExitStatus>,
    last_error: Option<&RwError>,
    stderr_path: &Path,
) -> String {
    let mut message = if let Some(status) = status {
        format!(
            "Failed to launch chromium: Chromium process exited before CDP endpoint became available (status: {status})."
        )
    } else {
        "Failed to launch chromium: Chromium did not expose a CDP endpoint before the launch timeout.".to_string()
    };
    if let Some(error) = last_error {
        message.push_str(&format!(" Last CDP polling error: {error}."));
    }
    match fs::read(stderr_path) {
        Ok(stderr) => {
            let stderr = String::from_utf8_lossy(&stderr);
            let stderr = tail_text(&stderr, 4096);
            if stderr.trim().is_empty() {
                message.push_str(" Chromium stderr was empty.");
            } else {
                message.push_str("\nChromium stderr:\n");
                message.push_str(stderr.trim_end());
            }
        }
        Err(error) => {
            message.push_str(&format!(" Could not read Chromium stderr: {error}."));
        }
    }
    message
}

fn tail_text(value: &str, max_chars: usize) -> &str {
    if value.chars().count() <= max_chars {
        return value;
    }
    let start = value
        .char_indices()
        .nth(value.chars().count() - max_chars)
        .map(|(index, _)| index)
        .unwrap_or(0);
    &value[start..]
}

fn parse_header_pairs(headers_json: Option<&str>) -> RwResult<Vec<(String, String)>> {
    let Some(headers_json) = headers_json else {
        return Ok(Vec::new());
    };
    let headers: Value = serde_json::from_str(headers_json)?;
    let Some(object) = headers.as_object() else {
        return Err(RwError::Message(
            "CDP headers must be a JSON object".to_string(),
        ));
    };
    object
        .iter()
        .map(|(name, value)| {
            let Some(value) = value.as_str() else {
                return Err(RwError::Message(format!(
                    "CDP header {name:?} must have a string value"
                )));
            };
            Ok((name.clone(), value.to_string()))
        })
        .collect()
}

async fn resolve_ws_endpoint(
    endpoint: &str,
    timeout: Duration,
    headers: &[(String, String)],
) -> RwResult<String> {
    if endpoint.starts_with("ws://") || endpoint.starts_with("wss://") {
        return Ok(endpoint.to_string());
    }
    let version_url = if endpoint.ends_with("/json/version") {
        endpoint.to_string()
    } else {
        format!("{}/json/version", endpoint.trim_end_matches('/'))
    };
    let client = reqwest::Client::builder()
        .timeout(timeout)
        .no_proxy()
        .build()?;
    let mut request = client.get(&version_url);
    for (name, value) in headers {
        request = request.header(name, value);
    }
    let response = request.send().await?;
    let status = response.status();
    if !status.is_success() {
        return Err(RwError::Message(format!(
            "Unexpected status {} when connecting to {}/.",
            status.as_u16(),
            version_url.trim_end_matches('/')
        )));
    }
    let payload: Value = response.json().await?;
    payload
        .get("webSocketDebuggerUrl")
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .ok_or_else(|| {
            RwError::Message("CDP endpoint did not return webSocketDebuggerUrl".to_string())
        })
}

fn pick_unused_port() -> RwResult<u16> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    Ok(listener.local_addr()?.port())
}

fn find_chromium_executable(
    explicit: Option<&str>,
    channel: Option<&str>,
    headless: bool,
) -> Option<PathBuf> {
    if let Some(path) = explicit {
        return Some(PathBuf::from(path));
    }
    if let Some(channel) = channel {
        if let Some(path) = find_channel_executable(channel) {
            return Some(path);
        }
        if matches!(channel, "chrome" | "msedge") {
            return find_chromium_executable(None, None, headless);
        }
        return None;
    }
    for key in ["RUSTWRIGHT_CHROMIUM", "CHROME", "CHROMIUM"] {
        if let Ok(path) = env::var(key) {
            let path = PathBuf::from(path);
            if is_executable_file(&path) {
                return Some(path);
            }
        }
    }
    if let Some(path) = find_in_playwright_cache(headless) {
        return Some(path);
    }
    for candidate in chromium_candidates() {
        if is_executable_file(&candidate) {
            return Some(candidate);
        }
    }
    for name in [
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "msedge",
        "microsoft-edge",
    ] {
        if let Some(path) = find_on_path(name) {
            return Some(path);
        }
    }
    None
}

fn find_channel_executable(channel: &str) -> Option<PathBuf> {
    for name in channel_binary_names(channel) {
        if let Some(path) = find_on_path(name) {
            return Some(path);
        }
    }
    for candidate in channel_candidates(channel) {
        if is_executable_file(&candidate) {
            return Some(candidate);
        }
    }
    None
}

fn channel_binary_names(channel: &str) -> &'static [&'static str] {
    match channel {
        "chrome" | "chrome-stable" => &["google-chrome", "google-chrome-stable", "chrome"],
        "chrome-beta" => &["google-chrome-beta", "chrome-beta"],
        "chrome-dev" => &["google-chrome-unstable", "google-chrome-dev", "chrome-dev"],
        "chrome-canary" => &["google-chrome-canary", "chrome-canary"],
        "msedge" | "msedge-stable" => &["msedge", "microsoft-edge"],
        "msedge-beta" => &["msedge-beta", "microsoft-edge-beta"],
        "msedge-dev" => &["msedge-dev", "microsoft-edge-dev"],
        "msedge-canary" => &["msedge-canary", "microsoft-edge-canary"],
        "chromium" | "cr" => &["chromium", "chromium-browser"],
        _ => &[],
    }
}

fn channel_candidates(channel: &str) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if cfg!(target_os = "macos") {
        let app_names: &[&str] = match channel {
            "chrome" | "chrome-stable" => &["Google Chrome.app"],
            "chrome-beta" => &["Google Chrome Beta.app"],
            "chrome-dev" => &["Google Chrome Dev.app"],
            "chrome-canary" => &["Google Chrome Canary.app"],
            "msedge" | "msedge-stable" => &["Microsoft Edge.app"],
            "msedge-beta" => &["Microsoft Edge Beta.app"],
            "msedge-dev" => &["Microsoft Edge Dev.app"],
            "msedge-canary" => &["Microsoft Edge Canary.app"],
            "chromium" | "cr" => &["Chromium.app"],
            _ => &[],
        };
        for app_name in app_names {
            let binary_name = app_name.trim_end_matches(".app");
            candidates.push(PathBuf::from(format!(
                "/Applications/{app_name}/Contents/MacOS/{binary_name}"
            )));
            if let Some(home) = home_dir() {
                candidates.push(home.join(format!(
                    "Applications/{app_name}/Contents/MacOS/{binary_name}"
                )));
            }
        }
    } else if cfg!(target_os = "windows") {
        let relative_paths: &[&str] = match channel {
            "chrome" | "chrome-stable" => &["Google/Chrome/Application/chrome.exe"],
            "chrome-beta" => &["Google/Chrome Beta/Application/chrome.exe"],
            "chrome-dev" => &["Google/Chrome Dev/Application/chrome.exe"],
            "chrome-canary" => &["Google/Chrome SxS/Application/chrome.exe"],
            "msedge" | "msedge-stable" => &["Microsoft/Edge/Application/msedge.exe"],
            "msedge-beta" => &["Microsoft/Edge Beta/Application/msedge.exe"],
            "msedge-dev" => &["Microsoft/Edge Dev/Application/msedge.exe"],
            "msedge-canary" => &["Microsoft/Edge SxS/Application/msedge.exe"],
            _ => &[],
        };
        for env_key in ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"] {
            if let Ok(root) = env::var(env_key) {
                for relative_path in relative_paths {
                    candidates.push(PathBuf::from(&root).join(relative_path));
                }
            }
        }
    } else {
        candidates.extend(
            channel_binary_names(channel)
                .iter()
                .map(|name| PathBuf::from("/usr/bin").join(name)),
        );
        candidates.extend(
            channel_binary_names(channel)
                .iter()
                .map(|name| PathBuf::from("/snap/bin").join(name)),
        );
    }
    candidates
}

fn chromium_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if cfg!(target_os = "macos") {
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            ]
            .into_iter()
            .map(PathBuf::from),
        );
        if let Some(home) = home_dir() {
            candidates.extend(
                [
                    "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "Applications/Chromium.app/Contents/MacOS/Chromium",
                    "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                ]
                .into_iter()
                .map(|path| home.join(path)),
            );
        }
    } else if cfg!(target_os = "windows") {
        if let Ok(program_files) = env::var("PROGRAMFILES") {
            candidates
                .push(PathBuf::from(program_files).join("Google/Chrome/Application/chrome.exe"));
        }
        if let Ok(program_files_x86) = env::var("PROGRAMFILES(X86)") {
            candidates.push(
                PathBuf::from(program_files_x86).join("Google/Chrome/Application/chrome.exe"),
            );
        }
    } else {
        candidates.extend(
            [
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/snap/bin/chromium",
            ]
            .into_iter()
            .map(PathBuf::from),
        );
    }
    candidates
}

fn find_on_path(name: &str) -> Option<PathBuf> {
    let path = env::var_os("PATH")?;
    for dir in env::split_paths(&path) {
        let candidate = dir.join(name);
        if is_executable_file(&candidate) {
            return Some(candidate);
        }
    }
    None
}

fn find_in_playwright_cache(headless: bool) -> Option<PathBuf> {
    for cache_dir in browser_cache_dirs() {
        let Ok(read_dir) = std::fs::read_dir(cache_dir) else {
            continue;
        };
        let mut entries: Vec<PathBuf> = read_dir.flatten().map(|entry| entry.path()).collect();
        entries.sort_by(|left, right| right.file_name().cmp(&left.file_name()));
        let mut candidates = Vec::new();
        for path in entries {
            let name = path
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("");
            if !name.starts_with("chromium") {
                continue;
            }
            if cfg!(target_os = "macos") {
                if headless && name.starts_with("chromium_headless_shell") {
                    candidates
                        .push(path.join("chrome-headless-shell-mac-arm64/chrome-headless-shell"));
                    candidates.push(path.join("chromium_headless_shell-mac/headless_shell"));
                }
                if name.starts_with("chromium-") {
                    candidates.push(
                        path.join(
                            "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                        ),
                    );
                    candidates.push(
                        path.join(
                            "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                        ),
                    );
                    candidates.push(path.join("chrome-mac/Chromium.app/Contents/MacOS/Chromium"));
                    candidates.push(
                        path.join(
                            "chrome-mac/Chromium.app/Contents/MacOS/Google Chrome for Testing",
                        ),
                    );
                }
            } else if cfg!(target_os = "windows") {
                if headless && name.starts_with("chromium_headless_shell") {
                    candidates
                        .push(path.join("chrome-headless-shell-win64/chrome-headless-shell.exe"));
                    candidates.push(path.join("chromium_headless_shell-win/headless_shell.exe"));
                }
                if name.starts_with("chromium-") {
                    candidates.push(path.join("chrome-win64/chrome.exe"));
                    candidates.push(path.join("chrome-win32/chrome.exe"));
                    candidates.push(path.join("chrome-win/chrome.exe"));
                }
            } else {
                if headless && name.starts_with("chromium_headless_shell") {
                    candidates
                        .push(path.join("chrome-headless-shell-linux64/chrome-headless-shell"));
                    candidates.push(path.join("chromium_headless_shell-linux/headless_shell"));
                }
                if name.starts_with("chromium-") {
                    candidates.push(path.join("chrome-linux64/chrome"));
                    candidates.push(path.join("chrome-linux/chrome"));
                }
            }
        }
        if let Some(executable) = candidates.into_iter().find(|path| is_executable_file(path)) {
            return Some(executable);
        }
    }
    None
}

fn browser_cache_dirs() -> Vec<PathBuf> {
    let mut dirs = Vec::new();
    if let Ok(path) = env::var("RUSTWRIGHT_BROWSERS_PATH") {
        if !path.is_empty() {
            dirs.push(PathBuf::from(path));
        }
    }
    if let Ok(path) = env::var("PLAYWRIGHT_BROWSERS_PATH") {
        if !path.is_empty() && path != "0" {
            dirs.push(PathBuf::from(path));
        }
    }
    if let Some(home) = home_dir() {
        dirs.push(if cfg!(target_os = "macos") {
            home.join("Library/Caches/ms-playwright")
        } else if cfg!(target_os = "windows") {
            env::var_os("LOCALAPPDATA")
                .map(PathBuf::from)
                .unwrap_or_else(|| home.join("AppData/Local"))
                .join("ms-playwright")
        } else {
            home.join(".cache/ms-playwright")
        });
    }
    let mut seen = HashSet::new();
    dirs.into_iter()
        .filter(|path| seen.insert(path.clone()))
        .collect()
}

fn home_dir() -> Option<PathBuf> {
    env::var_os("HOME").map(PathBuf::from)
}

fn is_executable_file(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        return fs::metadata(path)
            .map(|metadata| metadata.permissions().mode() & 0o111 != 0)
            .unwrap_or(false);
    }
    #[cfg(not(unix))]
    {
        true
    }
}

async fn wait_for_load_state(
    client: &CdpClient,
    events: &mut broadcast::Receiver<Value>,
    session_id: &str,
    state: &str,
    timeout: Duration,
) -> RwResult<()> {
    match state {
        "domcontentloaded" => {
            wait_for_event(events, session_id, &["Page.domContentEventFired"], timeout).await
        }
        "networkidle" => {
            wait_for_event(events, session_id, &["Page.loadEventFired"], timeout).await?;
            tokio::time::sleep(Duration::from_millis(500)).await;
            let _ = client;
            Ok(())
        }
        _ => wait_for_event(events, session_id, &["Page.loadEventFired"], timeout).await,
    }
}

#[derive(Clone)]
struct NetworkObservationState {
    response_extra_infos: HashMap<String, Value>,
    pending_responses: HashMap<String, PendingNetworkResponse>,
}

#[derive(Clone)]
struct PendingNetworkResponse {
    seq: u64,
    response: Value,
    deadline: tokio::time::Instant,
}

impl NetworkObservationState {
    fn new() -> Self {
        Self {
            response_extra_infos: HashMap::new(),
            pending_responses: HashMap::new(),
        }
    }

    fn earliest_pending_seq(&self) -> Option<u64> {
        self.pending_responses
            .values()
            .map(|pending| pending.seq)
            .min()
    }

    fn next_deadline(&self) -> Option<tokio::time::Instant> {
        self.pending_responses
            .values()
            .map(|pending| pending.deadline)
            .min()
    }

    fn take_expired(&mut self) -> Vec<(u64, Value)> {
        let now = tokio::time::Instant::now();
        let expired_ids = self
            .pending_responses
            .iter()
            .filter(|(_, pending)| now >= pending.deadline)
            .map(|(request_id, _)| request_id.clone())
            .collect::<Vec<_>>();
        let mut expired = expired_ids
            .into_iter()
            .filter_map(|request_id| {
                self.pending_responses
                    .remove(&request_id)
                    .map(|pending| (pending.seq, pending.response))
            })
            .collect::<Vec<_>>();
        expired.sort_by_key(|(seq, _)| *seq);
        expired
    }
}

fn apply_network_request_mutation(
    seq: u64,
    event: &Value,
    session_id: &str,
    requests: &mut NetworkRequestStore,
    advance_current: bool,
) -> Option<Value> {
    let matches_session = event.get("sessionId").and_then(Value::as_str) == Some(session_id);
    let method = event.get("method").and_then(Value::as_str).unwrap_or("");
    if !matches_session || method != "Network.requestWillBeSent" {
        return None;
    }
    let request_id = event
        .pointer("/params/requestId")
        .and_then(Value::as_str)?
        .to_string();
    if let Some(applied) = requests
        .requests
        .get(&request_id)
        .and_then(|entry| entry.applied_by_seq.get(&seq))
        .cloned()
    {
        return Some(applied.request);
    }
    let prior_snapshot = requests.requests.get(&request_id).and_then(|entry| {
        if advance_current {
            Some(entry.current.clone())
        } else {
            entry
                .applied_by_seq
                .range(..seq)
                .next_back()
                .map(|(_, request)| request.clone())
        }
    });
    let request = request_from_event(
        event,
        prior_snapshot
            .as_ref()
            .map(|snapshot| snapshot.request.clone()),
    )?;
    let redirect_ancestry = if request.get("redirected_from").is_some() {
        prior_snapshot
            .as_ref()
            .map(|snapshot| {
                std::iter::once(snapshot.seq)
                    .chain(snapshot.redirect_ancestry.iter().copied())
                    .collect()
            })
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    let snapshot = NetworkRequestSnapshot {
        seq,
        request: request.clone(),
        redirect_ancestry,
    };
    match requests.requests.get_mut(&request_id) {
        Some(entry) => {
            if advance_current {
                entry.current = snapshot.clone();
            }
            entry.applied_by_seq.insert(seq, snapshot.clone());
        }
        None => {
            requests.requests.insert(
                request_id.clone(),
                NetworkRequestEntry {
                    current: snapshot.clone(),
                    applied_by_seq: BTreeMap::from([(seq, snapshot)]),
                },
            );
        }
    }
    requests.record_applied_request(seq, request_id);
    Some(request)
}

fn process_network_observation_event(
    seq: u64,
    event: &Value,
    event_log: &Arc<Mutex<CdpEventLog>>,
    session_id: &str,
    requests: &Arc<Mutex<NetworkRequestStore>>,
    track_responses: bool,
    state: &mut NetworkObservationState,
) -> Result<Option<(u64, &'static str, Value)>, u64> {
    let method = event.get("method").and_then(Value::as_str).unwrap_or("");
    let request_payload = {
        let mut requests = requests.lock().unwrap();
        if seq > requests.next_applied_seq {
            let (oldest_seq, entries) = {
                let log = event_log.lock().unwrap();
                (
                    log.oldest_seq(),
                    log.entries_since(requests.next_applied_seq),
                )
            };
            if requests.next_applied_seq < oldest_seq {
                let dropped = oldest_seq - requests.next_applied_seq;
                requests.reset_after_overflow(seq.saturating_add(1));
                return Err(dropped);
            }
            let mut expected_seq = requests.next_applied_seq;
            for (missed_seq, missed_event) in entries {
                if missed_seq >= seq {
                    break;
                }
                if missed_seq != expected_seq {
                    let dropped = missed_seq.saturating_sub(expected_seq).max(1);
                    requests.reset_after_overflow(seq.saturating_add(1));
                    return Err(dropped);
                }
                apply_network_request_mutation(
                    missed_seq,
                    &missed_event,
                    session_id,
                    &mut requests,
                    true,
                );
                expected_seq = missed_seq.saturating_add(1);
            }
            if expected_seq != seq {
                let dropped = seq.saturating_sub(expected_seq).max(1);
                requests.reset_after_overflow(seq.saturating_add(1));
                return Err(dropped);
            }
            requests.next_applied_seq = seq;
        }

        let advance_current = seq == requests.next_applied_seq;
        let request_payload = if method == "Network.requestWillBeSent" {
            apply_network_request_mutation(seq, event, session_id, &mut requests, advance_current)
        } else {
            None
        };
        if advance_current {
            requests.next_applied_seq = seq.saturating_add(1);
        }
        request_payload
    };

    if method == "Network.requestWillBeSent" {
        return Ok(request_payload.map(|request| (seq, "request", request)));
    }
    let matches_session = event
        .get("sessionId")
        .and_then(Value::as_str)
        .map(|value| value == session_id)
        .unwrap_or(false);
    if !matches_session {
        return Ok(None);
    }
    if method == "Network.responseReceived" && track_responses {
        let request_id = event
            .pointer("/params/requestId")
            .and_then(Value::as_str)
            .map(ToString::to_string);
        let response_extra = event
            .pointer("/params/requestId")
            .and_then(Value::as_str)
            .and_then(|request_id| state.response_extra_infos.remove(request_id));
        if let Some(response) = response_from_event(event, requests, response_extra.as_ref()) {
            if response_needs_extra_info(event, response_extra.as_ref()) {
                if let Some(request_id) = request_id {
                    state.pending_responses.insert(
                        request_id,
                        PendingNetworkResponse {
                            seq,
                            response,
                            deadline: tokio::time::Instant::now() + Duration::from_millis(100),
                        },
                    );
                    return Ok(None);
                }
            }
            return Ok(Some((seq, "response", response)));
        }
        return Ok(None);
    }
    if method == "Network.responseReceivedExtraInfo" {
        if let (Some(request_id), Some(params)) = (
            event.pointer("/params/requestId").and_then(Value::as_str),
            event.get("params"),
        ) {
            if let Some(mut pending) = state.pending_responses.remove(request_id) {
                apply_response_extra_info(&mut pending.response, params);
                return Ok(Some((pending.seq, "response", pending.response)));
            }
            state
                .response_extra_infos
                .insert(request_id.to_string(), params.clone());
        }
        return Ok(None);
    }
    if method == "Network.loadingFinished" {
        if let Some(request) = request_lifecycle_from_event(event, requests, None) {
            return Ok(Some((seq, "requestfinished", request)));
        }
        return Ok(None);
    }
    if method == "Network.loadingFailed" {
        let failure_text = event
            .pointer("/params/errorText")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .or_else(|| {
                event
                    .pointer("/params/blockedReason")
                    .and_then(Value::as_str)
                    .map(ToString::to_string)
            });
        if let Some(request) = request_lifecycle_from_event(event, requests, failure_text) {
            return Ok(Some((seq, "requestfailed", request)));
        }
    }
    Ok(None)
}

fn process_network_wait_event(
    seq: u64,
    event: &Value,
    event_log: &Arc<Mutex<CdpEventLog>>,
    session_id: &str,
    kind: &str,
    requests: &Arc<Mutex<NetworkRequestStore>>,
    state: &mut NetworkObservationState,
) -> Result<Option<(u64, String)>, u64> {
    process_network_observation_event(
        seq,
        event,
        event_log,
        session_id,
        requests,
        kind == "response",
        state,
    )
    .map(|matched| {
        matched.and_then(|(matched_seq, matched_kind, payload)| {
            (matched_kind == kind).then(|| (matched_seq, payload.to_string()))
        })
    })
}

fn page_event_envelope(seq: u64, kind: &str, payload: Value) -> Value {
    json!({
        "seq": seq,
        "kind": kind,
        "payload": payload,
    })
}

fn process_page_observation_event(
    seq: u64,
    event: &Value,
    event_log: &Arc<Mutex<CdpEventLog>>,
    session_id: &str,
    requests: &Arc<Mutex<NetworkRequestStore>>,
    state: &mut PageEventStreamState,
) {
    let method = event.get("method").and_then(Value::as_str).unwrap_or("");
    let matched = match process_network_observation_event(
        seq,
        event,
        event_log,
        session_id,
        requests,
        true,
        &mut state.network,
    ) {
        Ok(matched) => matched,
        Err(dropped) => {
            *state = PageEventStreamState::new();
            state.ready.insert(
                seq,
                page_event_envelope(seq, "_overflow", json!({ "dropped": dropped })),
            );
            return;
        }
    };
    if method.starts_with("Network.") {
        if let Some((matched_seq, kind, payload)) = matched {
            state
                .ready
                .insert(matched_seq, page_event_envelope(matched_seq, kind, payload));
        }
        return;
    }

    let matches_session = event.get("sessionId").and_then(Value::as_str) == Some(session_id);
    if !matches_session {
        return;
    }
    let matched = match method {
        "Runtime.consoleAPICalled" => console_from_event(event).map(|payload| ("console", payload)),
        "Runtime.exceptionThrown" => event
            .get("params")
            .cloned()
            .map(|payload| ("pageerror", payload)),
        "Page.javascriptDialogOpening" => {
            dialog_from_event(event).map(|payload| ("dialog", payload))
        }
        "Page.loadEventFired" => event
            .get("params")
            .cloned()
            .map(|payload| ("load", payload)),
        "Page.domContentEventFired" => event
            .get("params")
            .cloned()
            .map(|payload| ("domcontentloaded", payload)),
        "Page.frameNavigated" | "Page.navigatedWithinDocument" => event
            .get("params")
            .cloned()
            .map(|payload| ("framenavigated", payload)),
        "Page.frameAttached" => event
            .get("params")
            .cloned()
            .map(|payload| ("frameattached", payload)),
        "Page.frameDetached" => event
            .get("params")
            .cloned()
            .map(|payload| ("framedetached", payload)),
        _ => None,
    };
    if let Some((kind, payload)) = matched {
        state
            .ready
            .insert(seq, page_event_envelope(seq, kind, payload));
    }
}

fn expire_page_responses(state: &mut PageEventStreamState) {
    for (seq, response) in state.network.take_expired() {
        state
            .ready
            .insert(seq, page_event_envelope(seq, "response", response));
    }
}

fn append_ready_page_events(
    batch: &mut Vec<Value>,
    state: &mut PageEventStreamState,
    max_events: usize,
) {
    while batch.len() < max_events {
        let Some((&seq, _)) = state.ready.first_key_value() else {
            break;
        };
        if state
            .network
            .earliest_pending_seq()
            .map(|pending_seq| seq > pending_seq)
            .unwrap_or(false)
        {
            break;
        }
        let (_, envelope) = state.ready.pop_first().unwrap();
        batch.push(envelope);
    }
}

async fn wait_for_page_event_batch(
    events: &mut broadcast::Receiver<Value>,
    event_log: Arc<Mutex<CdpEventLog>>,
    cursor: &mut u64,
    session_id: &str,
    requests: Arc<Mutex<NetworkRequestStore>>,
    state: &mut PageEventStreamState,
    mut close_rx: watch::Receiver<bool>,
    mut alive_rx: watch::Receiver<bool>,
    timeout: Duration,
    max_events: usize,
) -> (Vec<Value>, bool) {
    let deadline = tokio::time::Instant::now() + timeout;
    let mut batch = Vec::with_capacity(max_events);
    loop {
        if *close_rx.borrow() {
            batch.push(page_event_envelope(*cursor, "_closed", Value::Null));
            return (batch, true);
        }
        if !*alive_rx.borrow() {
            batch.push(page_event_envelope(*cursor, "_closed", Value::Null));
            return (batch, true);
        }
        expire_page_responses(state);
        append_ready_page_events(&mut batch, state, max_events);
        if batch.len() >= max_events {
            return (batch, false);
        }

        let (oldest_seq, entries) = {
            let log = event_log.lock().unwrap();
            (log.oldest_seq(), log.entries_since(*cursor))
        };
        if *cursor < oldest_seq {
            let dropped = oldest_seq - *cursor;
            *cursor = oldest_seq;
            *state = PageEventStreamState::new();
            requests.lock().unwrap().reset_after_overflow(oldest_seq);
            batch.push(page_event_envelope(
                *cursor,
                "_overflow",
                json!({ "dropped": dropped }),
            ));
            if batch.len() >= max_events {
                return (batch, false);
            }
        }
        for (seq, event) in entries {
            if seq < *cursor {
                continue;
            }
            *cursor = seq.saturating_add(1);
            process_page_observation_event(seq, &event, &event_log, session_id, &requests, state);
            expire_page_responses(state);
            append_ready_page_events(&mut batch, state, max_events);
            if batch.len() >= max_events {
                return (batch, false);
            }
        }
        if !batch.is_empty() {
            return (batch, false);
        }

        let now = tokio::time::Instant::now();
        if now >= deadline {
            return (batch, false);
        }
        let mut remaining = deadline - now;
        if let Some(response_deadline) = state.network.next_deadline() {
            remaining = remaining.min(response_deadline.saturating_duration_since(now));
        }
        tokio::select! {
            changed = close_rx.changed() => {
                if changed.is_err() || *close_rx.borrow() {
                    batch.push(page_event_envelope(*cursor, "_closed", Value::Null));
                    return (batch, true);
                }
            }
            changed = alive_rx.changed() => {
                if changed.is_err() || !*alive_rx.borrow() {
                    batch.push(page_event_envelope(*cursor, "_closed", Value::Null));
                    return (batch, true);
                }
            }
            received = tokio::time::timeout(remaining, events.recv()) => {
                match received {
                    Ok(Ok(_)) | Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
                    Ok(Err(broadcast::error::RecvError::Closed)) => {
                        batch.push(page_event_envelope(*cursor, "_closed", Value::Null));
                        return (batch, true);
                    }
                    Err(_) => {
                        if state.network.next_deadline()
                            .map(|pending_deadline| tokio::time::Instant::now() >= pending_deadline)
                            .unwrap_or(false)
                        {
                            continue;
                        }
                        return (batch, false);
                    }
                }
            }
        }
    }
}

async fn wait_for_network_event(
    events: &mut broadcast::Receiver<Value>,
    event_log: Arc<Mutex<CdpEventLog>>,
    cursor: u64,
    session_id: &str,
    kind: &str,
    requests: Arc<Mutex<NetworkRequestStore>>,
    timeout: Duration,
) -> (RwResult<String>, u64) {
    let deadline = tokio::time::Instant::now() + timeout;
    let mut cursor = cursor;
    let mut state = NetworkObservationState::new();
    let mut ready_responses = BTreeMap::<u64, String>::new();
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return (Err(RwError::Timeout(timeout.as_millis() as u64)), cursor);
        }
        for (seq, response) in state.take_expired() {
            ready_responses.insert(seq, response.to_string());
        }
        if kind == "response" {
            if let Some((&seq, _)) = ready_responses.first_key_value() {
                if state
                    .earliest_pending_seq()
                    .map(|pending_seq| seq > pending_seq)
                    .unwrap_or(false)
                {
                    // An earlier response is still waiting for its own extra-info deadline.
                } else {
                    return (Ok(ready_responses.pop_first().unwrap().1), cursor);
                }
            }
        }
        let (oldest_seq, entries) = {
            let log = event_log.lock().unwrap();
            (log.oldest_seq(), log.entries_since(cursor))
        };
        if cursor < oldest_seq {
            let dropped = oldest_seq - cursor;
            requests.lock().unwrap().reset_after_overflow(oldest_seq);
            return (
                Err(RwError::Message(format!(
                    "CDP event log overflow: dropped {dropped} event(s)"
                ))),
                oldest_seq,
            );
        }
        for (seq, event) in entries {
            cursor = seq.saturating_add(1);
            let matched = match process_network_wait_event(
                seq, &event, &event_log, session_id, kind, &requests, &mut state,
            ) {
                Ok(matched) => matched,
                Err(dropped) => {
                    return (
                        Err(RwError::Message(format!(
                            "CDP event log overflow: dropped {dropped} event(s)"
                        ))),
                        cursor,
                    )
                }
            };
            if let Some((matched_seq, result)) = matched {
                if kind == "response" {
                    ready_responses.insert(matched_seq, result);
                } else {
                    return (Ok(result), cursor);
                }
            }
            for (expired_seq, response) in state.take_expired() {
                ready_responses.insert(expired_seq, response.to_string());
            }
            if kind == "response" {
                if let Some((&ready_seq, _)) = ready_responses.first_key_value() {
                    if state
                        .earliest_pending_seq()
                        .map(|pending_seq| ready_seq > pending_seq)
                        .unwrap_or(false)
                    {
                        continue;
                    }
                    return (Ok(ready_responses.pop_first().unwrap().1), cursor);
                }
            }
        }
        let mut remaining = deadline - now;
        if let Some(extra_deadline) = state.next_deadline() {
            remaining = remaining.min(extra_deadline.saturating_duration_since(now));
        }
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(_)) => continue,
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => {
                return (
                    Err(RwError::Message("CDP event stream closed".to_string())),
                    cursor,
                )
            }
            Err(_) => {
                for (seq, response) in state.take_expired() {
                    ready_responses.insert(seq, response.to_string());
                }
                if let Some((_, response)) = ready_responses.pop_first() {
                    return (Ok(response), cursor);
                }
                return (Err(RwError::Timeout(timeout.as_millis() as u64)), cursor);
            }
        }
    }
}

async fn wait_for_route_event(
    events: &mut broadcast::Receiver<Value>,
    session_id: &str,
    timeout: Duration,
) -> RwResult<String> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                let matches_session = event
                    .get("sessionId")
                    .and_then(Value::as_str)
                    .map(|value| value == session_id)
                    .unwrap_or(false);
                if !matches_session {
                    continue;
                }
                if event.get("method").and_then(Value::as_str) != Some("Fetch.requestPaused") {
                    continue;
                }
                if let Some(route) = route_from_event(&event) {
                    return Ok(route.to_string());
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

async fn wait_for_auth_event(
    events: &mut broadcast::Receiver<Value>,
    session_id: &str,
    timeout: Duration,
) -> RwResult<String> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                let matches_session = event
                    .get("sessionId")
                    .and_then(Value::as_str)
                    .map(|value| value == session_id)
                    .unwrap_or(false);
                if !matches_session {
                    continue;
                }
                let method = event.get("method").and_then(Value::as_str);
                if !matches!(
                    method,
                    Some("Fetch.authRequired") | Some("Fetch.requestPaused")
                ) {
                    continue;
                }
                if let Some(params) = event.get("params") {
                    let mut payload = params.clone();
                    if let Some(object) = payload.as_object_mut() {
                        object.insert(
                            "_event".to_string(),
                            Value::String(
                                method
                                    .unwrap_or("")
                                    .strip_prefix("Fetch.")
                                    .unwrap_or("")
                                    .to_string(),
                            ),
                        );
                    }
                    return Ok(payload.to_string());
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

async fn wait_for_dialog_event(
    events: &mut broadcast::Receiver<Value>,
    session_id: &str,
    timeout: Duration,
) -> RwResult<String> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                let matches_session = event
                    .get("sessionId")
                    .and_then(Value::as_str)
                    .map(|value| value == session_id)
                    .unwrap_or(false);
                if !matches_session {
                    continue;
                }
                if event.get("method").and_then(Value::as_str)
                    != Some("Page.javascriptDialogOpening")
                {
                    continue;
                }
                if let Some(dialog) = dialog_from_event(&event) {
                    return Ok(dialog.to_string());
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

async fn wait_for_console_event(
    events: &mut broadcast::Receiver<Value>,
    session_id: &str,
    timeout: Duration,
) -> RwResult<String> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                let matches_session = event
                    .get("sessionId")
                    .and_then(Value::as_str)
                    .map(|value| value == session_id)
                    .unwrap_or(false);
                if !matches_session {
                    continue;
                }
                if event.get("method").and_then(Value::as_str) != Some("Runtime.consoleAPICalled") {
                    continue;
                }
                if let Some(message) = console_from_event(&event) {
                    return Ok(message.to_string());
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

async fn wait_for_websocket_event(
    events: &mut broadcast::Receiver<Value>,
    session_id: &str,
    kind: &str,
    request_id: Option<&str>,
    timeout: Duration,
) -> RwResult<String> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                let matches_session = event
                    .get("sessionId")
                    .and_then(Value::as_str)
                    .map(|value| value == session_id)
                    .unwrap_or(false);
                if !matches_session {
                    continue;
                }
                let method = event.get("method").and_then(Value::as_str).unwrap_or("");
                if kind == "created" && method == "Network.webSocketCreated" {
                    if let Some(socket) = websocket_from_created_event(&event) {
                        return Ok(socket.to_string());
                    }
                    continue;
                }
                if kind == "closed" && method == "Network.webSocketClosed" {
                    let params = event.get("params").unwrap_or(&Value::Null);
                    let event_request_id = params.get("requestId").and_then(Value::as_str);
                    if request_id.is_some() && request_id != event_request_id {
                        continue;
                    }
                    return Ok(json!({
                        "request_id": event_request_id,
                        "closed": true,
                        "timestamp": params.get("timestamp").cloned().unwrap_or(Value::Null),
                    })
                    .to_string());
                }
                if kind == "framesent" && method == "Network.webSocketFrameSent" {
                    if let Some(frame) = websocket_frame_from_event(&event, request_id) {
                        return Ok(frame.to_string());
                    }
                    continue;
                }
                if kind == "framereceived" && method == "Network.webSocketFrameReceived" {
                    if let Some(frame) = websocket_frame_from_event(&event, request_id) {
                        return Ok(frame.to_string());
                    }
                    continue;
                }
                if kind == "socketerror" && method == "Network.webSocketFrameError" {
                    let params = event.get("params").unwrap_or(&Value::Null);
                    let event_request_id = params.get("requestId").and_then(Value::as_str);
                    if request_id.is_some() && request_id != event_request_id {
                        continue;
                    }
                    return Ok(json!({
                        "request_id": event_request_id,
                        "error": params.get("errorMessage").cloned().unwrap_or(Value::Null),
                        "timestamp": params.get("timestamp").cloned().unwrap_or(Value::Null),
                    })
                    .to_string());
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

async fn wait_for_binding_event_for_page(
    events: &mut broadcast::Receiver<Value>,
    page: &Arc<PageInner>,
    name: &str,
    timeout: Duration,
) -> RwResult<String> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                let matches_session = event
                    .get("sessionId")
                    .and_then(Value::as_str)
                    .map(|value| page.frame_state.lock().unwrap().owns_session(value))
                    .unwrap_or(false);
                if !matches_session {
                    continue;
                }
                if event.get("method").and_then(Value::as_str) != Some("Runtime.bindingCalled") {
                    continue;
                }
                if let Some(binding) = binding_from_event(&event, name) {
                    return Ok(binding.to_string());
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

async fn wait_for_download_event(
    events: &mut broadcast::Receiver<Value>,
    download_path: &str,
    active_downloads: &mut HashMap<String, Value>,
    timeout: Duration,
) -> RwResult<String> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                let method = event.get("method").and_then(Value::as_str).unwrap_or("");
                if method == "Browser.downloadWillBegin" {
                    if let Some(payload) = download_from_begin_event(&event, download_path) {
                        if let Some(guid) = payload.get("guid").and_then(Value::as_str) {
                            if !guid.is_empty() {
                                active_downloads.insert(guid.to_string(), payload);
                            }
                        }
                    }
                    continue;
                }
                if method == "Browser.downloadProgress" {
                    let params = event.get("params").unwrap_or(&Value::Null);
                    let event_guid = params.get("guid").and_then(Value::as_str);
                    let state = params.get("state").and_then(Value::as_str).unwrap_or("");
                    if state == "completed" || state == "canceled" {
                        let mut payload = event_guid
                            .and_then(|guid| active_downloads.remove(guid))
                            .unwrap_or_else(|| {
                                json!({
                                    "guid": event_guid,
                                    "url": "",
                                    "suggested_filename": "",
                                    "path": event_guid
                                        .map(|value| PathBuf::from(download_path).join(value).to_string_lossy().to_string())
                                        .unwrap_or_default(),
                                })
                            });
                        payload["state"] = Value::String(state.to_string());
                        payload["received_bytes"] =
                            params.get("receivedBytes").cloned().unwrap_or(Value::Null);
                        payload["total_bytes"] =
                            params.get("totalBytes").cloned().unwrap_or(Value::Null);
                        if state == "canceled" {
                            payload["failure"] = Value::String("canceled".to_string());
                        }
                        return Ok(payload.to_string());
                    }
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

async fn wait_for_file_chooser_event(
    events: &mut broadcast::Receiver<Value>,
    session_id: &str,
    timeout: Duration,
) -> RwResult<String> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                let matches_session = event
                    .get("sessionId")
                    .and_then(Value::as_str)
                    .map(|value| value == session_id)
                    .unwrap_or(false);
                if !matches_session {
                    continue;
                }
                if event.get("method").and_then(Value::as_str) != Some("Page.fileChooserOpened") {
                    continue;
                }
                if let Some(file_chooser) = file_chooser_from_event(&event) {
                    return Ok(file_chooser.to_string());
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

#[cfg(feature = "python")]
async fn wait_for_popup_page(
    events: &mut broadcast::Receiver<Value>,
    browser: Arc<BrowserInner>,
    opener_target_id: &str,
    seen_target_ids: Arc<Mutex<HashSet<String>>>,
    timeout: Duration,
) -> RwResult<PyPage> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                let method = event.get("method").and_then(Value::as_str);
                if method != Some("Target.targetCreated")
                    && method != Some("Target.targetInfoChanged")
                {
                    continue;
                }
                let info = event.pointer("/params/targetInfo").unwrap_or(&Value::Null);
                let target_type = info.get("type").and_then(Value::as_str).unwrap_or("");
                if target_type != "page" {
                    continue;
                }
                let opener = info.get("openerId").and_then(Value::as_str);
                if opener != Some(opener_target_id) {
                    continue;
                }
                let target_id = info
                    .get("targetId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        RwError::Message("popup target did not include targetId".to_string())
                    })?
                    .to_string();
                if !seen_target_ids.lock().unwrap().insert(target_id.clone()) {
                    continue;
                }
                let context_id = info
                    .get("browserContextId")
                    .and_then(Value::as_str)
                    .map(ToString::to_string);
                return match attach_existing_page(browser, target_id.clone(), context_id, remaining)
                    .await
                {
                    Ok(inner) => Ok(PyPage { inner }),
                    Err(error) => {
                        seen_target_ids.lock().unwrap().remove(&target_id);
                        Err(error)
                    }
                };
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

async fn wait_for_cdp_event(
    events: &mut broadcast::Receiver<Value>,
    session_id: Option<&str>,
    method: &str,
    timeout: Duration,
) -> RwResult<String> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                if event.get("method").and_then(Value::as_str) != Some(method) {
                    continue;
                }
                let event_session_id = event.get("sessionId").and_then(Value::as_str);
                if event_session_id != session_id {
                    continue;
                }
                return Ok(event
                    .get("params")
                    .cloned()
                    .unwrap_or_else(|| json!({}))
                    .to_string());
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

fn target_context_matches(info: &Value, context_id: Option<&str>) -> bool {
    match context_id {
        Some(expected) => info.get("browserContextId").and_then(Value::as_str) == Some(expected),
        None => info
            .get("browserContextId")
            .and_then(Value::as_str)
            .is_none(),
    }
}

fn list_pages_raw(
    browser: Arc<BrowserInner>,
    context_id: Option<String>,
    timeout: Duration,
) -> RwResult<Vec<Arc<PageInner>>> {
    let browser_for_task = Arc::clone(&browser);
    let client = Arc::clone(&browser.client);
    browser.block_on_raw(async move {
        let non_default_contexts = if context_id.is_none() {
            client
                .send("Target.getBrowserContexts", json!({}), None, timeout)
                .await?
                .get("browserContextIds")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(ToString::to_string)
                .collect::<HashSet<_>>()
        } else {
            HashSet::new()
        };
        let result = client
            .send("Target.getTargets", json!({}), None, timeout)
            .await?;
        let mut pages = Vec::new();
        for info in result
            .get("targetInfos")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if info.get("type").and_then(Value::as_str) != Some("page") {
                continue;
            }
            let target_context = info.get("browserContextId").and_then(Value::as_str);
            let context_matches = match context_id.as_deref() {
                Some(expected) => target_context == Some(expected),
                None => target_context
                    .map(|id| !non_default_contexts.contains(id))
                    .unwrap_or(true),
            };
            if !context_matches {
                continue;
            }
            let Some(target_id) = info.get("targetId").and_then(Value::as_str) else {
                continue;
            };
            let page_context_id = info
                .get("browserContextId")
                .and_then(Value::as_str)
                .map(ToString::to_string);
            pages.push(
                attach_existing_page(
                    Arc::clone(&browser_for_task),
                    target_id.to_string(),
                    page_context_id,
                    timeout,
                )
                .await?,
            );
        }
        Ok(pages)
    })
}

#[cfg(feature = "python")]
fn list_pages_for_context(
    browser: Arc<BrowserInner>,
    context_id: Option<String>,
    timeout_ms: Option<f64>,
) -> PyResult<Vec<PyPage>> {
    let browser_for_task = Arc::clone(&browser);
    let client = Arc::clone(&browser.client);
    let timeout = BrowserInner::command_timeout(timeout_ms);
    browser
        .block_on(async move {
            let result = client
                .send("Target.getTargets", json!({}), None, timeout)
                .await?;
            let mut pages = Vec::new();
            for info in result
                .get("targetInfos")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                let target_type = info.get("type").and_then(Value::as_str).unwrap_or("");
                if target_type != "page" {
                    continue;
                }
                if context_id.is_some() && !target_context_matches(info, context_id.as_deref()) {
                    continue;
                }
                let Some(target_id) = info.get("targetId").and_then(Value::as_str) else {
                    continue;
                };
                let page_context_id = info
                    .get("browserContextId")
                    .and_then(Value::as_str)
                    .map(ToString::to_string);
                if let Ok(inner) = attach_existing_page(
                    Arc::clone(&browser_for_task),
                    target_id.to_string(),
                    page_context_id,
                    timeout,
                )
                .await
                {
                    pages.push(PyPage { inner });
                }
            }
            Ok(pages)
        })
        .map_err(py_err)
}

#[cfg(feature = "python")]
fn list_service_workers_for_context(
    browser: Arc<BrowserInner>,
    context_id: Option<String>,
    timeout_ms: Option<f64>,
) -> PyResult<Vec<PyWorker>> {
    let browser_for_task = Arc::clone(&browser);
    let client = Arc::clone(&browser.client);
    let timeout = BrowserInner::command_timeout(timeout_ms);
    browser
        .block_on(async move {
            let result = client
                .send("Target.getTargets", json!({}), None, timeout)
                .await?;
            let mut workers = Vec::new();
            for info in result
                .get("targetInfos")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                let target_type = info.get("type").and_then(Value::as_str).unwrap_or("");
                if target_type != "service_worker" {
                    continue;
                }
                if !target_context_matches(info, context_id.as_deref()) {
                    continue;
                }
                let Some(target_id) = info.get("targetId").and_then(Value::as_str) else {
                    continue;
                };
                let url = info
                    .get("url")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                if let Ok(worker) = attach_existing_worker(
                    Arc::clone(&browser_for_task),
                    target_id.to_string(),
                    url,
                    timeout,
                )
                .await
                {
                    workers.push(worker);
                }
            }
            Ok(workers)
        })
        .map_err(py_err)
}

#[cfg(feature = "python")]
fn service_worker_event_waiter_for_context(
    browser: Arc<BrowserInner>,
    context_id: Option<String>,
    timeout_ms: Option<f64>,
) -> PyResult<PyServiceWorkerEventWaiter> {
    let client = Arc::clone(&browser.client);
    let timeout = BrowserInner::command_timeout(timeout_ms);
    browser
        .block_on(async {
            client
                .send(
                    "Target.setDiscoverTargets",
                    json!({ "discover": true }),
                    None,
                    timeout,
                )
                .await?;
            Ok(())
        })
        .map_err(py_err)?;
    Ok(PyServiceWorkerEventWaiter {
        browser: Arc::clone(&browser),
        receiver: Mutex::new(Some(browser.client.subscribe())),
        context_id,
    })
}

#[cfg(feature = "python")]
fn list_background_pages_for_context(
    browser: Arc<BrowserInner>,
    context_id: Option<String>,
    timeout_ms: Option<f64>,
) -> PyResult<Vec<PyPage>> {
    let browser_for_task = Arc::clone(&browser);
    let client = Arc::clone(&browser.client);
    let timeout = BrowserInner::command_timeout(timeout_ms);
    browser
        .block_on(async move {
            let result = client
                .send("Target.getTargets", json!({}), None, timeout)
                .await?;
            let mut pages = Vec::new();
            for info in result
                .get("targetInfos")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                let target_type = info.get("type").and_then(Value::as_str).unwrap_or("");
                if target_type != "background_page" {
                    continue;
                }
                if !target_context_matches(info, context_id.as_deref()) {
                    continue;
                }
                let Some(target_id) = info.get("targetId").and_then(Value::as_str) else {
                    continue;
                };
                let page_context_id = info
                    .get("browserContextId")
                    .and_then(Value::as_str)
                    .map(ToString::to_string);
                if let Ok(inner) = attach_existing_page(
                    Arc::clone(&browser_for_task),
                    target_id.to_string(),
                    page_context_id,
                    timeout,
                )
                .await
                {
                    pages.push(PyPage { inner });
                }
            }
            Ok(pages)
        })
        .map_err(py_err)
}

#[cfg(feature = "python")]
fn background_page_event_waiter_for_context(
    browser: Arc<BrowserInner>,
    context_id: Option<String>,
    timeout_ms: Option<f64>,
) -> PyResult<PyBackgroundPageEventWaiter> {
    let client = Arc::clone(&browser.client);
    let timeout = BrowserInner::command_timeout(timeout_ms);
    browser
        .block_on(async {
            client
                .send(
                    "Target.setDiscoverTargets",
                    json!({ "discover": true }),
                    None,
                    timeout,
                )
                .await?;
            Ok(())
        })
        .map_err(py_err)?;
    Ok(PyBackgroundPageEventWaiter {
        browser: Arc::clone(&browser),
        receiver: Mutex::new(Some(browser.client.subscribe())),
        context_id,
    })
}

#[cfg(feature = "python")]
async fn wait_for_worker(
    events: &mut broadcast::Receiver<Value>,
    browser: Arc<BrowserInner>,
    opener_target_id: &str,
    timeout: Duration,
) -> RwResult<PyWorker> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                if event.get("method").and_then(Value::as_str) != Some("Target.attachedToTarget") {
                    continue;
                }
                let info = event.pointer("/params/targetInfo").unwrap_or(&Value::Null);
                let target_type = info.get("type").and_then(Value::as_str).unwrap_or("");
                if target_type != "worker" {
                    continue;
                }
                let opener = info.get("openerId").and_then(Value::as_str);
                if opener.is_some() && opener != Some(opener_target_id) {
                    continue;
                }
                let target_id = info
                    .get("targetId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        RwError::Message("worker target did not include targetId".to_string())
                    })?
                    .to_string();
                let session_id = event
                    .pointer("/params/sessionId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        RwError::Message("worker target did not include sessionId".to_string())
                    })?
                    .to_string();
                let url = info
                    .get("url")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                return Ok(PyWorker {
                    browser,
                    target_id,
                    session_id,
                    url,
                });
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

#[cfg(feature = "python")]
async fn wait_for_background_page(
    events: &mut broadcast::Receiver<Value>,
    browser: Arc<BrowserInner>,
    context_id: Option<&str>,
    timeout: Duration,
) -> RwResult<PyPage> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                if event.get("method").and_then(Value::as_str) != Some("Target.targetCreated") {
                    continue;
                }
                let info = event.pointer("/params/targetInfo").unwrap_or(&Value::Null);
                let target_type = info.get("type").and_then(Value::as_str).unwrap_or("");
                if target_type != "background_page" {
                    continue;
                }
                if !target_context_matches(info, context_id) {
                    continue;
                }
                let target_id = info
                    .get("targetId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        RwError::Message(
                            "background page target did not include targetId".to_string(),
                        )
                    })?
                    .to_string();
                let page_context_id = info
                    .get("browserContextId")
                    .and_then(Value::as_str)
                    .map(ToString::to_string);
                return attach_existing_page(browser, target_id, page_context_id, remaining)
                    .await
                    .map(|inner| PyPage { inner });
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

#[cfg(feature = "python")]
async fn wait_for_service_worker(
    events: &mut broadcast::Receiver<Value>,
    browser: Arc<BrowserInner>,
    context_id: Option<&str>,
    timeout: Duration,
) -> RwResult<PyWorker> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                if event.get("method").and_then(Value::as_str) != Some("Target.targetCreated") {
                    continue;
                }
                let info = event.pointer("/params/targetInfo").unwrap_or(&Value::Null);
                let target_type = info.get("type").and_then(Value::as_str).unwrap_or("");
                if target_type != "service_worker" {
                    continue;
                }
                if !target_context_matches(info, context_id) {
                    continue;
                }
                let target_id = info
                    .get("targetId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        RwError::Message(
                            "service worker target did not include targetId".to_string(),
                        )
                    })?
                    .to_string();
                let url = info
                    .get("url")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                match attach_existing_worker(Arc::clone(&browser), target_id, url, remaining).await
                {
                    Ok(worker) => return Ok(worker),
                    Err(_) => continue,
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

async fn wait_for_worker_close(
    events: &mut broadcast::Receiver<Value>,
    target_id: &str,
    session_id: &str,
    timeout: Duration,
) -> RwResult<()> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                let method = event.get("method").and_then(Value::as_str).unwrap_or("");
                if method == "Target.detachedFromTarget" {
                    let detached_session =
                        event.pointer("/params/sessionId").and_then(Value::as_str);
                    if detached_session == Some(session_id) {
                        return Ok(());
                    }
                }
                if method == "Target.targetDestroyed" {
                    let destroyed_target =
                        event.pointer("/params/targetId").and_then(Value::as_str);
                    if destroyed_target == Some(target_id) {
                        return Ok(());
                    }
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

fn dialog_from_event(event: &Value) -> Option<Value> {
    let params = event.get("params")?;
    Some(json!({
        "url": params.get("url").cloned().unwrap_or(Value::Null),
        "type": params.get("type").cloned().unwrap_or(Value::Null),
        "message": params.get("message").cloned().unwrap_or(Value::Null),
        "default_value": params.get("defaultPrompt").cloned().unwrap_or(Value::Null),
        "has_browser_handler": params.get("hasBrowserHandler").cloned().unwrap_or(Value::Null),
    }))
}

fn websocket_from_created_event(event: &Value) -> Option<Value> {
    let params = event.get("params")?;
    Some(json!({
        "request_id": params.get("requestId").cloned().unwrap_or(Value::Null),
        "url": params.get("url").cloned().unwrap_or(Value::Null),
        "initiator": params.get("initiator").cloned().unwrap_or(Value::Null),
        "closed": false,
    }))
}

fn websocket_frame_from_event(event: &Value, request_id: Option<&str>) -> Option<Value> {
    let params = event.get("params")?;
    let event_request_id = params.get("requestId").and_then(Value::as_str);
    if request_id.is_some() && request_id != event_request_id {
        return None;
    }
    let response = params.get("response")?;
    Some(json!({
        "request_id": event_request_id,
        "opcode": response.get("opcode").cloned().unwrap_or(Value::Null),
        "data": response.get("payloadData").cloned().unwrap_or(Value::Null),
        "timestamp": params.get("timestamp").cloned().unwrap_or(Value::Null),
    }))
}

fn file_chooser_from_event(event: &Value) -> Option<Value> {
    let params = event.get("params")?;
    Some(json!({
        "frame_id": params.get("frameId").cloned().unwrap_or(Value::Null),
        "backend_node_id": params.get("backendNodeId").cloned().unwrap_or(Value::Null),
        "mode": params.get("mode").cloned().unwrap_or(Value::Null),
    }))
}

fn download_from_begin_event(event: &Value, download_path: &str) -> Option<Value> {
    let params = event.get("params")?;
    let guid = params.get("guid").and_then(Value::as_str).unwrap_or("");
    let suggested_filename = params
        .get("suggestedFilename")
        .and_then(Value::as_str)
        .unwrap_or("");
    Some(json!({
        "guid": guid,
        "url": params.get("url").cloned().unwrap_or(Value::String(String::new())),
        "frame_id": params.get("frameId").cloned().unwrap_or(Value::Null),
        "suggested_filename": suggested_filename,
        "path": PathBuf::from(download_path).join(guid).to_string_lossy().to_string(),
    }))
}

fn console_from_event(event: &Value) -> Option<Value> {
    let params = event.get("params")?;
    let args = params
        .get("args")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let values: Vec<Value> = args.iter().map(console_arg_value).collect();
    let handle_args: Vec<Value> = args
        .iter()
        .map(|arg| {
            json!({
                "__rustwright_cdp_remote_object__": arg,
            })
        })
        .collect();
    let text = values
        .iter()
        .map(console_value_text)
        .collect::<Vec<_>>()
        .join(" ");
    let location = params
        .pointer("/stackTrace/callFrames/0")
        .map(|frame| {
            json!({
                "url": frame.get("url").cloned().unwrap_or(Value::String(String::new())),
                "lineNumber": frame.get("lineNumber").cloned().unwrap_or(Value::Number(0.into())),
                "columnNumber": frame.get("columnNumber").cloned().unwrap_or(Value::Number(0.into())),
            })
        })
        .unwrap_or_else(|| {
            json!({
                "url": "",
                "lineNumber": 0,
                "columnNumber": 0,
            })
        });

    Some(json!({
        "type": params.get("type").cloned().unwrap_or(Value::String("log".to_string())),
        "text": text,
        "args": handle_args,
        "location": location,
        "timestamp": params.get("timestamp").cloned().unwrap_or(Value::Null),
    }))
}

fn binding_from_event(event: &Value, expected_name: &str) -> Option<Value> {
    let params = event.get("params")?;
    let name = params.get("name").and_then(Value::as_str)?;
    if name != expected_name {
        return None;
    }
    let payload = params.get("payload").and_then(Value::as_str).unwrap_or("");
    serde_json::from_str::<Value>(payload).ok().or_else(|| {
        Some(json!({
            "name": name,
            "payload": payload,
        }))
    })
}

fn console_arg_value(arg: &Value) -> Value {
    if arg
        .get("type")
        .and_then(Value::as_str)
        .map(|value| value == "undefined")
        .unwrap_or(false)
    {
        return Value::Null;
    }
    if let Some(value) = arg.get("value") {
        return value.clone();
    }
    if let Some(value) = arg.get("unserializableValue").and_then(Value::as_str) {
        return Value::String(value.to_string());
    }
    if let Some(value) = arg.get("description").and_then(Value::as_str) {
        return Value::String(value.to_string());
    }
    Value::Null
}

fn console_value_text(value: &Value) -> String {
    match value {
        Value::Null => "null".to_string(),
        Value::String(text) => text.clone(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        other => other.to_string(),
    }
}

fn route_from_event(event: &Value) -> Option<Value> {
    let params = event.get("params")?;
    let request = params.get("request")?;
    Some(json!({
        "route_request_id": params.get("requestId").cloned().unwrap_or(Value::Null),
        "network_id": params.get("networkId").cloned().unwrap_or(Value::Null),
        "frame_id": params.get("frameId").cloned().unwrap_or(Value::Null),
        "resource_type": params.get("resourceType").cloned().unwrap_or(Value::Null),
        "url": request.get("url").cloned().unwrap_or(Value::Null),
        "method": request.get("method").cloned().unwrap_or(Value::Null),
        "headers": request.get("headers").cloned().unwrap_or_else(|| json!({})),
        "post_data": request.get("postData").cloned().unwrap_or(Value::Null),
        "post_data_entries": request.get("postDataEntries").cloned().unwrap_or(Value::Null),
    }))
}

fn request_from_event(event: &Value, redirected_from: Option<Value>) -> Option<Value> {
    let params = event.get("params")?;
    let request = params.get("request")?;
    let mut payload = json!({
        "request_id": params.get("requestId").cloned().unwrap_or(Value::Null),
        "loader_id": params.get("loaderId").cloned().unwrap_or(Value::Null),
        "frame_id": params.get("frameId").cloned().unwrap_or(Value::Null),
        "timestamp": params.get("timestamp").cloned().unwrap_or(Value::Null),
        "wall_time": params.get("wallTime").cloned().unwrap_or(Value::Null),
        "resource_type": params.get("type").cloned().unwrap_or(Value::Null),
        "is_navigation_request": params.get("documentURL") == request.get("url"),
        "url": request.get("url").cloned().unwrap_or(Value::Null),
        "method": request.get("method").cloned().unwrap_or(Value::Null),
        "headers": request.get("headers").cloned().unwrap_or_else(|| json!({})),
        "post_data": request.get("postData").cloned().unwrap_or(Value::Null),
        "post_data_entries": request.get("postDataEntries").cloned().unwrap_or(Value::Null),
        "timing": Value::Null,
    });
    if params.get("redirectResponse").is_some() {
        if let Some(previous) = redirected_from {
            payload["redirected_from"] = previous;
        }
    }
    Some(payload)
}

fn response_from_event(
    event: &Value,
    requests: &Arc<Mutex<NetworkRequestStore>>,
    response_extra: Option<&Value>,
) -> Option<Value> {
    let params = event.get("params")?;
    let response = params.get("response")?;
    let request_id = params.get("requestId").and_then(Value::as_str);
    let mut request = request_id
        .and_then(|id| {
            requests
                .lock()
                .unwrap()
                .requests
                .get(id)
                .map(|entry| entry.current.request.clone())
        })
        .unwrap_or_else(|| {
            json!({
                "request_id": params.get("requestId").cloned().unwrap_or(Value::Null),
                "loader_id": params.get("loaderId").cloned().unwrap_or(Value::Null),
                "frame_id": params.get("frameId").cloned().unwrap_or(Value::Null),
                "timestamp": Value::Null,
                "wall_time": Value::Null,
                "resource_type": params.get("type").cloned().unwrap_or(Value::Null),
                "url": response.get("url").cloned().unwrap_or(Value::Null),
                "method": Value::Null,
                "headers": {},
                "post_data": Value::Null,
                "timing": Value::Null,
            })
        });
    request["timing"] = response.get("timing").cloned().unwrap_or(Value::Null);
    let mut payload = json!({
        "request_id": params.get("requestId").cloned().unwrap_or(Value::Null),
        "loader_id": params.get("loaderId").cloned().unwrap_or(Value::Null),
        "frame_id": params.get("frameId").cloned().unwrap_or(Value::Null),
        "resource_type": params.get("type").cloned().unwrap_or(Value::Null),
        "url": response.get("url").cloned().unwrap_or(Value::Null),
        "status": response.get("status").cloned().unwrap_or(Value::Null),
        "status_text": response.get("statusText").cloned().unwrap_or(Value::Null),
        "headers": response.get("headers").cloned().unwrap_or_else(|| json!({})),
        "encoded_data_length": response.get("encodedDataLength").cloned().unwrap_or(Value::Null),
        "protocol": response.get("protocol").cloned().unwrap_or(Value::Null),
        "remote_ip_address": response.get("remoteIPAddress").cloned().unwrap_or(Value::Null),
        "remote_port": response.get("remotePort").cloned().unwrap_or(Value::Null),
        "security_details": response.get("securityDetails").cloned().unwrap_or(Value::Null),
        "from_disk_cache": response.get("fromDiskCache").cloned().unwrap_or(Value::Bool(false)),
        "from_service_worker": response.get("fromServiceWorker").cloned().unwrap_or(Value::Bool(false)),
        "request": request,
    });
    if let Some(extra) = response_extra {
        payload["all_headers"] = extra.get("headers").cloned().unwrap_or(Value::Null);
        payload["headers_text"] = extra.get("headersText").cloned().unwrap_or(Value::Null);
    }
    Some(payload)
}

fn response_needs_extra_info(event: &Value, response_extra: Option<&Value>) -> bool {
    response_extra.is_none()
        && event
            .pointer("/params/hasExtraInfo")
            .and_then(Value::as_bool)
            .unwrap_or(false)
}

fn apply_response_extra_info(payload: &mut Value, response_extra: &Value) {
    payload["all_headers"] = response_extra
        .get("headers")
        .cloned()
        .unwrap_or(Value::Null);
    payload["headers_text"] = response_extra
        .get("headersText")
        .cloned()
        .unwrap_or(Value::Null);
}

fn request_lifecycle_from_event(
    event: &Value,
    requests: &Arc<Mutex<NetworkRequestStore>>,
    failure_text: Option<String>,
) -> Option<Value> {
    let params = event.get("params")?;
    let request_id = params.get("requestId").and_then(Value::as_str);
    let mut request = request_id
        .and_then(|id| {
            requests
                .lock()
                .unwrap()
                .requests
                .get(id)
                .map(|entry| entry.current.request.clone())
        })
        .unwrap_or_else(|| {
            json!({
                "request_id": params.get("requestId").cloned().unwrap_or(Value::Null),
                "loader_id": Value::Null,
                "frame_id": Value::Null,
                "resource_type": Value::Null,
                "url": Value::Null,
                "method": Value::Null,
                "headers": {},
                "post_data": Value::Null,
            })
        });
    if let Some(failure_text) = failure_text {
        request["failure_text"] = Value::String(failure_text);
    }
    if let Some(encoded_data_length) = params.get("encodedDataLength") {
        request["encoded_data_length"] = encoded_data_length.clone();
    }
    Some(request)
}

async fn wait_for_navigation(
    events: &mut broadcast::Receiver<Value>,
    session_id: &str,
    state: &str,
    loader_id: Option<&str>,
    expected_url: Option<&str>,
    method_label: &str,
    timeout: Duration,
) -> RwResult<Option<Value>> {
    let deadline = tokio::time::Instant::now() + timeout;
    let mut response = None;
    let mut requests: HashMap<String, Value> = HashMap::new();
    let mut response_extra_infos: HashMap<String, Value> = HashMap::new();
    let mut response_extra_request_id: Option<String> = None;
    let mut response_extra_deadline: Option<tokio::time::Instant> = None;
    let mut state_ready_to_return = false;
    let mut active_requests: HashSet<String> = HashSet::new();
    let mut load_reached = false;
    let mut network_idle_deadline: Option<tokio::time::Instant> = None;
    let mut state_reached_deadline: Option<tokio::time::Instant> = None;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        if state == "networkidle"
            && load_reached
            && response.is_some()
            && active_requests.is_empty()
            && network_idle_deadline
                .map(|idle| now >= idle)
                .unwrap_or(false)
        {
            return Ok(response);
        }
        if let Some(grace_deadline) = state_reached_deadline {
            if now >= grace_deadline {
                return Ok(response);
            }
        }
        if response_extra_deadline
            .map(|extra_deadline| now >= extra_deadline)
            .unwrap_or(false)
        {
            response_extra_request_id = None;
            response_extra_deadline = None;
        }
        if state_ready_to_return {
            if let Some(extra_deadline) = response_extra_deadline {
                if now >= extra_deadline {
                    return Ok(response);
                }
            } else {
                return Ok(response);
            }
        }
        let mut remaining = deadline - now;
        if state == "networkidle" {
            if let Some(idle_deadline) = network_idle_deadline {
                remaining = remaining.min(idle_deadline - now);
            }
        }
        if let Some(grace_deadline) = state_reached_deadline {
            remaining = remaining.min(grace_deadline - now);
        }
        if let Some(extra_deadline) = response_extra_deadline {
            remaining = remaining.min(extra_deadline - now);
        }
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                let matches_session = event
                    .get("sessionId")
                    .and_then(Value::as_str)
                    .map(|value| value == session_id)
                    .unwrap_or(false);
                if !matches_session {
                    continue;
                }
                let method = event.get("method").and_then(Value::as_str).unwrap_or("");
                if method == "Network.requestWillBeSent" {
                    if let Some(request_id) =
                        event.pointer("/params/requestId").and_then(Value::as_str)
                    {
                        active_requests.insert(request_id.to_string());
                        network_idle_deadline = None;
                    }
                    let prior_request = event
                        .pointer("/params/requestId")
                        .and_then(Value::as_str)
                        .and_then(|request_id| requests.get(request_id).cloned());
                    if let Some(request) = request_from_event(&event, prior_request) {
                        if let Some(request_id) = request.get("request_id").and_then(Value::as_str)
                        {
                            requests.insert(request_id.to_string(), request);
                        }
                    }
                    continue;
                }
                if method == "Network.responseReceived" {
                    let request_id = event
                        .pointer("/params/requestId")
                        .and_then(Value::as_str)
                        .map(ToString::to_string);
                    let response_extra = event
                        .pointer("/params/requestId")
                        .and_then(Value::as_str)
                        .and_then(|request_id| response_extra_infos.get(request_id));
                    let request = event
                        .pointer("/params/requestId")
                        .and_then(Value::as_str)
                        .and_then(|request_id| requests.get(request_id).cloned());
                    if let Some(candidate) = navigation_response_from_event(
                        &event,
                        loader_id,
                        request,
                        expected_url,
                        response_extra,
                    ) {
                        let waiting_for_extra = response_needs_extra_info(&event, response_extra);
                        let is_non_document = candidate
                            .get("resource_type")
                            .and_then(Value::as_str)
                            .map(|resource_type| resource_type != "Document")
                            .unwrap_or(false);
                        response = Some(candidate);
                        if waiting_for_extra {
                            response_extra_request_id = request_id;
                            response_extra_deadline =
                                Some(tokio::time::Instant::now() + Duration::from_millis(100));
                        } else {
                            response_extra_request_id = None;
                            response_extra_deadline = None;
                        }
                        if state == "commit" {
                            if response_extra_deadline.is_some() {
                                state_ready_to_return = true;
                                continue;
                            }
                            return Ok(response);
                        }
                        if state != "networkidle" && is_non_document {
                            if response_extra_deadline.is_some() {
                                state_ready_to_return = true;
                                continue;
                            }
                            return Ok(response);
                        }
                    }
                    continue;
                }
                if method == "Network.responseReceivedExtraInfo" {
                    if let (Some(request_id), Some(params)) = (
                        event.pointer("/params/requestId").and_then(Value::as_str),
                        event.get("params"),
                    ) {
                        response_extra_infos.insert(request_id.to_string(), params.clone());
                        if response_extra_request_id
                            .as_deref()
                            .map(|pending_id| pending_id == request_id)
                            .unwrap_or(false)
                        {
                            if let Some(existing_response) = response.as_mut() {
                                apply_response_extra_info(existing_response, params);
                            }
                            response_extra_request_id = None;
                            response_extra_deadline = None;
                            if state_ready_to_return {
                                return Ok(response);
                            }
                        }
                    }
                    continue;
                }
                if method == "Network.loadingFinished" || method == "Network.loadingFailed" {
                    if method == "Network.loadingFailed" {
                        if let Some(message) = navigation_failure_message(
                            &event,
                            loader_id,
                            expected_url,
                            &requests,
                            response.as_ref(),
                            method_label,
                        ) {
                            return Err(RwError::Message(message));
                        }
                    }
                    if let Some(request_id) =
                        event.pointer("/params/requestId").and_then(Value::as_str)
                    {
                        active_requests.remove(request_id);
                    }
                    if state == "networkidle" && load_reached && active_requests.is_empty() {
                        network_idle_deadline =
                            Some(tokio::time::Instant::now() + Duration::from_millis(500));
                    }
                    continue;
                }

                let frame_navigated_to_expected = method == "Page.frameNavigated"
                    && expected_url
                        .zip(event.pointer("/params/frame/url").and_then(Value::as_str))
                        .map(|(expected, actual)| expected == actual)
                        .unwrap_or(false);
                let reached_state = match state {
                    "domcontentloaded" => {
                        method == "Page.domContentEventFired" || frame_navigated_to_expected
                    }
                    "networkidle" | "load" => {
                        method == "Page.loadEventFired" || frame_navigated_to_expected
                    }
                    _ => method == "Page.loadEventFired" || frame_navigated_to_expected,
                };
                if reached_state {
                    if state == "networkidle" {
                        load_reached = true;
                        if active_requests.is_empty() {
                            network_idle_deadline =
                                Some(tokio::time::Instant::now() + Duration::from_millis(500));
                        }
                        continue;
                    }
                    if response.is_some() {
                        if response_extra_deadline.is_some() {
                            state_ready_to_return = true;
                            continue;
                        }
                        return Ok(response);
                    }
                    state_reached_deadline =
                        Some(tokio::time::Instant::now() + Duration::from_millis(250));
                    continue;
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => {
                if state == "networkidle"
                    && load_reached
                    && response.is_some()
                    && active_requests.is_empty()
                {
                    return Ok(response);
                }
                if state_reached_deadline.is_some() {
                    return Ok(response);
                }
                return Err(RwError::Timeout(timeout.as_millis() as u64));
            }
        }
    }
}

fn navigation_failure_message(
    event: &Value,
    loader_id: Option<&str>,
    expected_url: Option<&str>,
    requests: &HashMap<String, Value>,
    response: Option<&Value>,
    method: &str,
) -> Option<String> {
    let params = event.get("params")?;
    let request_id = params.get("requestId").and_then(Value::as_str)?;
    let request = requests.get(request_id);
    let resource_type = params
        .get("type")
        .and_then(Value::as_str)
        .or_else(|| request?.get("resource_type").and_then(Value::as_str))
        .or_else(|| response?.get("resource_type").and_then(Value::as_str))?;
    if resource_type != "Document" {
        return None;
    }

    let request_loader = request.and_then(|value| value.get("loader_id").and_then(Value::as_str));
    let response_loader = response.and_then(|value| value.get("loader_id").and_then(Value::as_str));
    let request_url = request.and_then(|value| value.get("url").and_then(Value::as_str));
    let response_url = response.and_then(|value| value.get("url").and_then(Value::as_str));
    let response_request_id =
        response.and_then(|value| value.get("request_id").and_then(Value::as_str));

    let matches_navigation = if let Some(expected_loader_id) = loader_id {
        request_loader == Some(expected_loader_id) || response_loader == Some(expected_loader_id)
    } else if let Some(expected) = expected_url {
        request_url == Some(expected) || response_url == Some(expected)
    } else {
        response_request_id == Some(request_id)
    };
    if !matches_navigation {
        return None;
    }
    if method == "Page.goto" && response_has_attachment_disposition(response) {
        return Some(format!("{method}: Download is starting"));
    }

    let error_text = params
        .get("errorText")
        .and_then(Value::as_str)
        .or_else(|| params.get("blockedReason").and_then(Value::as_str))
        .unwrap_or("net::ERR_FAILED");
    let url = request_url.or(response_url).or(expected_url)?;
    Some(format!("{method}: {error_text} at {url}"))
}

fn response_has_attachment_disposition(response: Option<&Value>) -> bool {
    let Some(headers) = response
        .and_then(|value| value.get("headers"))
        .and_then(Value::as_object)
    else {
        return false;
    };
    headers.iter().any(|(name, value)| {
        name.eq_ignore_ascii_case("content-disposition")
            && value
                .as_str()
                .map(|text| text.to_ascii_lowercase().contains("attachment"))
                .unwrap_or(false)
    })
}

fn navigation_response_from_event(
    event: &Value,
    loader_id: Option<&str>,
    request: Option<Value>,
    expected_url: Option<&str>,
    response_extra: Option<&Value>,
) -> Option<Value> {
    let params = event.get("params")?;
    let response_type = params.get("type").and_then(Value::as_str).unwrap_or("");
    let response = params.get("response")?;
    if let Some(expected_loader_id) = loader_id {
        let actual_loader_id = params.get("loaderId").and_then(Value::as_str);
        if actual_loader_id != Some(expected_loader_id) {
            return None;
        }
        if response_type != "Document" {
            return None;
        }
    } else if response_type != "Document" {
        let matches_expected_url = expected_url
            .zip(response.get("url").and_then(Value::as_str))
            .map(|(expected, actual)| expected == actual)
            .unwrap_or(false);
        if !matches_expected_url {
            return None;
        }
    }
    let mut payload = json!({
        "request_id": params.get("requestId").cloned().unwrap_or(Value::Null),
        "loader_id": params.get("loaderId").cloned().unwrap_or(Value::Null),
        "frame_id": params.get("frameId").cloned().unwrap_or(Value::Null),
        "resource_type": response_type,
        "url": response.get("url").cloned().unwrap_or(Value::Null),
        "status": response.get("status").cloned().unwrap_or(Value::Null),
        "status_text": response.get("statusText").cloned().unwrap_or(Value::Null),
        "headers": response.get("headers").cloned().unwrap_or_else(|| json!({})),
        "encoded_data_length": response.get("encodedDataLength").cloned().unwrap_or(Value::Null),
        "protocol": response.get("protocol").cloned().unwrap_or(Value::Null),
        "remote_ip_address": response.get("remoteIPAddress").cloned().unwrap_or(Value::Null),
        "remote_port": response.get("remotePort").cloned().unwrap_or(Value::Null),
        "security_details": response.get("securityDetails").cloned().unwrap_or(Value::Null),
        "from_disk_cache": response.get("fromDiskCache").cloned().unwrap_or(Value::Bool(false)),
        "from_service_worker": response.get("fromServiceWorker").cloned().unwrap_or(Value::Bool(false)),
    });
    if let Some(request) = request {
        payload["request"] = request;
    }
    if let Some(extra) = response_extra {
        payload["all_headers"] = extra.get("headers").cloned().unwrap_or(Value::Null);
        payload["headers_text"] = extra.get("headersText").cloned().unwrap_or(Value::Null);
    }
    Some(payload)
}

async fn wait_for_event(
    events: &mut broadcast::Receiver<Value>,
    session_id: &str,
    methods: &[&str],
    timeout: Duration,
) -> RwResult<()> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(RwError::Timeout(timeout.as_millis() as u64));
        }
        let remaining = deadline - now;
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(event)) => {
                let matches_session = event
                    .get("sessionId")
                    .and_then(Value::as_str)
                    .map(|value| value == session_id)
                    .unwrap_or(false);
                let matches_method = event
                    .get("method")
                    .and_then(Value::as_str)
                    .map(|method| methods.iter().any(|candidate| candidate == &method))
                    .unwrap_or(false);
                if matches_session && matches_method {
                    return Ok(());
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Err(RwError::Message("CDP event stream closed".to_string())),
            Err(_) => return Err(RwError::Timeout(timeout.as_millis() as u64)),
        }
    }
}

const WIRE_ARRAY_TAG: &str = "__rustwright_cdp_array__";
const WIRE_OBJECT_TAG: &str = "__rustwright_cdp_object__";
const WIRE_REF_TAG: &str = "__rustwright_cdp_ref__";
const WIRE_LEAF_TAGS: [&str; 9] = [
    "__rustwright_cdp_unserializable_value__",
    "__rustwright_cdp_bigint__",
    "__rustwright_cdp_date__",
    "__rustwright_cdp_regexp__",
    "__rustwright_cdp_url__",
    "__rustwright_cdp_error__",
    "__rustwright_cdp_undefined__",
    "__rustwright_cdp_symbol__",
    "__rustwright_cdp_function__",
];

#[derive(Clone)]
enum WireDefinition {
    Array(Vec<Value>),
    Object(serde_json::Map<String, Value>),
}

struct WireValueDecoder {
    definitions: HashMap<String, WireDefinition>,
    active: HashSet<String>,
}

impl WireValueDecoder {
    fn new(wire: &Value) -> RwResult<Self> {
        let mut decoder = Self {
            definitions: HashMap::new(),
            active: HashSet::new(),
        };
        decoder.collect_definitions(wire)?;
        Ok(decoder)
    }

    fn collect_definitions(&mut self, value: &Value) -> RwResult<()> {
        match value {
            Value::Array(values) => {
                for value in values {
                    self.collect_definitions(value)?;
                }
            }
            Value::Object(object) => {
                if object.contains_key(WIRE_REF_TAG) || is_wire_leaf(object) {
                    return Ok(());
                }
                if let Some(id) = object.get(WIRE_ARRAY_TAG) {
                    let key = wire_reference_key(id)?;
                    let items = object
                        .get("items")
                        .and_then(Value::as_array)
                        .ok_or_else(|| {
                            RwError::InvalidInput(
                                "wire array wrapper must contain an items array".to_string(),
                            )
                        })?;
                    self.insert_definition(key, WireDefinition::Array(items.clone()))?;
                    for item in items {
                        self.collect_definitions(item)?;
                    }
                    return Ok(());
                }
                if let Some(id) = object.get(WIRE_OBJECT_TAG) {
                    let key = wire_reference_key(id)?;
                    let entries = object
                        .get("entries")
                        .and_then(Value::as_object)
                        .ok_or_else(|| {
                            RwError::InvalidInput(
                                "wire object wrapper must contain an entries object".to_string(),
                            )
                        })?;
                    self.insert_definition(key, WireDefinition::Object(entries.clone()))?;
                    for entry in entries.values() {
                        self.collect_definitions(entry)?;
                    }
                    return Ok(());
                }
                for value in object.values() {
                    self.collect_definitions(value)?;
                }
            }
            _ => {}
        }
        Ok(())
    }

    fn insert_definition(&mut self, key: String, definition: WireDefinition) -> RwResult<()> {
        if self.definitions.insert(key.clone(), definition).is_some() {
            return Err(RwError::InvalidInput(format!(
                "wire reference id is defined more than once: {key}"
            )));
        }
        Ok(())
    }

    fn decode(&mut self, value: Value) -> RwResult<Value> {
        match value {
            Value::Array(values) => values
                .into_iter()
                .map(|value| self.decode(value))
                .collect::<RwResult<Vec<_>>>()
                .map(Value::Array),
            Value::Object(object) => {
                if let Some(id) = object.get(WIRE_REF_TAG) {
                    return self.resolve(&wire_reference_key(id)?);
                }
                if let Some(id) = object.get(WIRE_ARRAY_TAG) {
                    return self.resolve(&wire_reference_key(id)?);
                }
                if let Some(id) = object.get(WIRE_OBJECT_TAG) {
                    return self.resolve(&wire_reference_key(id)?);
                }
                if is_wire_leaf(&object) {
                    return Ok(Value::Object(object));
                }
                object
                    .into_iter()
                    .map(|(key, value)| self.decode(value).map(|value| (key, value)))
                    .collect::<RwResult<serde_json::Map<_, _>>>()
                    .map(Value::Object)
            }
            value => Ok(value),
        }
    }

    fn resolve(&mut self, key: &str) -> RwResult<Value> {
        if self.active.contains(key) {
            return Ok(json!({"__rustwright_cdp_cycle__": true}));
        }
        let definition = self.definitions.get(key).cloned().ok_or_else(|| {
            RwError::InvalidInput(format!("wire reference points to unknown id: {key}"))
        })?;
        self.active.insert(key.to_string());
        let result = match definition {
            WireDefinition::Array(items) => self.decode(Value::Array(items)),
            WireDefinition::Object(entries) => self.decode(Value::Object(entries)),
        };
        self.active.remove(key);
        result
    }
}

fn wire_reference_key(value: &Value) -> RwResult<String> {
    if !matches!(value, Value::Number(_) | Value::String(_)) {
        return Err(RwError::InvalidInput(
            "wire reference id must be a number or string".to_string(),
        ));
    }
    serde_json::to_string(value).map_err(RwError::from)
}

fn is_wire_leaf(object: &serde_json::Map<String, Value>) -> bool {
    WIRE_LEAF_TAGS.iter().any(|tag| object.contains_key(*tag))
}

/// Decode the core evaluate wire format into a plain JSON tree.
///
/// Array and object wrappers are removed and references are expanded. Repeated
/// non-cyclic references are duplicated in the output. Because JSON cannot
/// represent object identity, a reference to an active ancestor (a true cycle)
/// is replaced with `{"__rustwright_cdp_cycle__": true}`. Leaf scalar tags are
/// preserved verbatim for language bindings to map to native values.
pub fn decode_wire_value(json: &str) -> Result<String, RwError> {
    let wire = serde_json::from_str::<Value>(json)?;
    let mut decoder = WireValueDecoder::new(&wire)?;
    let decoded = decoder.decode(wire)?;
    serde_json::to_string(&decoded).map_err(RwError::from)
}

#[cfg(test)]
mod wire_decode_tests {
    use super::*;

    #[test]
    fn resolves_nested_arrays_and_objects() {
        let decoded = decode_wire_value(
            r#"{
                "__rustwright_cdp_object__": 1,
                "entries": {
                    "nested": {
                        "__rustwright_cdp_array__": 2,
                        "items": [1, {
                            "__rustwright_cdp_object__": 3,
                            "entries": {"ok": true}
                        }]
                    }
                }
            }"#,
        )
        .unwrap();

        assert_eq!(
            serde_json::from_str::<Value>(&decoded).unwrap(),
            json!({"nested": [1, {"ok": true}]})
        );
    }

    #[test]
    fn duplicates_repeated_references() {
        let decoded = decode_wire_value(
            r#"{
                "__rustwright_cdp_array__": 1,
                "items": [
                    {
                        "__rustwright_cdp_object__": 2,
                        "entries": {"value": [1, 2, 3]}
                    },
                    {"__rustwright_cdp_ref__": 2}
                ]
            }"#,
        )
        .unwrap();

        assert_eq!(
            serde_json::from_str::<Value>(&decoded).unwrap(),
            json!([
                {"value": [1, 2, 3]},
                {"value": [1, 2, 3]},
            ])
        );
    }

    #[test]
    fn marks_true_cycles() {
        let decoded = decode_wire_value(
            r#"{
                "__rustwright_cdp_object__": 1,
                "entries": {
                    "name": "root",
                    "self": {"__rustwright_cdp_ref__": 1}
                }
            }"#,
        )
        .unwrap();

        assert_eq!(
            serde_json::from_str::<Value>(&decoded).unwrap(),
            json!({
                "name": "root",
                "self": {"__rustwright_cdp_cycle__": true},
            })
        );
    }

    #[test]
    fn preserves_every_leaf_tag_verbatim() {
        let wire = json!([
            {"__rustwright_cdp_unserializable_value__": "NaN"},
            {"__rustwright_cdp_bigint__": "123"},
            {"__rustwright_cdp_date__": "2026-07-21T12:34:56.789Z"},
            {"__rustwright_cdp_regexp__": {"p": "a+b", "f": "gi"}},
            {"__rustwright_cdp_url__": "https://example.com/path?q=1"},
            {"__rustwright_cdp_error__": {
                "name": "TypeError",
                "message": "broken",
                "stack": "TypeError: broken",
            }},
            {"__rustwright_cdp_undefined__": true},
            {"__rustwright_cdp_symbol__": true},
            {"__rustwright_cdp_function__": true},
        ]);

        let decoded = decode_wire_value(&wire.to_string()).unwrap();

        assert_eq!(serde_json::from_str::<Value>(&decoded).unwrap(), wire);
    }

    #[test]
    fn reports_malformed_json() {
        let error = decode_wire_value(r#"{"unterminated": [1, 2}"#).unwrap_err();

        assert!(matches!(error, RwError::Json(_)));
    }
}

const RUNTIME_VALUE_SERIALIZER: &str = r#"(function __rw_serialize(value) {
    const marker = "__rustwright_cdp_unserializable_value__";
    const seen = new WeakMap();
    let nextRef = 0;
    const serialize = (item) => {
        if (typeof item === "number") {
            if (Number.isNaN(item)) return { [marker]: "NaN" };
            if (item === Infinity) return { [marker]: "Infinity" };
            if (item === -Infinity) return { [marker]: "-Infinity" };
            if (Object.is(item, -0)) return { [marker]: "-0" };
            return item;
        }
        if (typeof item === "undefined") return { "__rustwright_cdp_undefined__": true };
        if (typeof item === "bigint") return { [marker]: `${item.toString()}n` };
        if (typeof item === "symbol") return { "__rustwright_cdp_symbol__": true };
        if (typeof item === "function") return { "__rustwright_cdp_function__": true };
        if (item === null || typeof item !== "object") return item;
        try {
            if (item === globalThis || (typeof Window !== "undefined" && item instanceof Window)) {
                return "ref: <Window>";
            }
        } catch (_) {
            return "ref: <Window>";
        }
        if (item instanceof Date) return { "__rustwright_cdp_date__": item.toISOString() };
        if (item instanceof RegExp) {
            return { "__rustwright_cdp_regexp__": { p: item.source, f: item.flags } };
        }
        if (item instanceof URL) return { "__rustwright_cdp_url__": item.href };
        if (item instanceof Error) {
            return { "__rustwright_cdp_error__": {
                name: item.name || "Error",
                message: item.message || "",
                stack: item.stack || "",
            } };
        }
        if (ArrayBuffer.isView(item) && !(item instanceof DataView)) {
            return Array.from(item, value => serialize(value));
        }
        if (typeof DOMRectReadOnly !== "undefined" && item instanceof DOMRectReadOnly) {
            const mapped = {};
            for (const key of ["x", "y", "width", "height", "top", "right", "bottom", "left"]) {
                mapped[key] = serialize(item[key]);
            }
            return mapped;
        }
        if (typeof DOMPointReadOnly !== "undefined" && item instanceof DOMPointReadOnly) {
            const mapped = {};
            for (const key of ["x", "y", "z", "w"]) mapped[key] = serialize(item[key]);
            return mapped;
        }
        if (typeof DOMMatrixReadOnly !== "undefined" && item instanceof DOMMatrixReadOnly) {
            const mapped = {};
            for (const key of [
                "a", "b", "c", "d", "e", "f",
                "m11", "m12", "m13", "m14",
                "m21", "m22", "m23", "m24",
                "m31", "m32", "m33", "m34",
                "m41", "m42", "m43", "m44",
                "is2D", "isIdentity",
            ]) {
                mapped[key] = serialize(item[key]);
            }
            return mapped;
        }
        if (typeof DOMQuad !== "undefined" && item instanceof DOMQuad) {
            return {
                p1: serialize(item.p1),
                p2: serialize(item.p2),
                p3: serialize(item.p3),
                p4: serialize(item.p4),
            };
        }
        if (
            (typeof Node !== "undefined" && item instanceof Node) ||
            (typeof item.nodeType === "number" && typeof item.nodeName === "string")
        ) return "ref: <Node>";
        const tag = Object.prototype.toString.call(item);
        if (tag === "[object BigInt]") return { [marker]: `${item.valueOf().toString()}n` };
        if (tag === "[object Symbol]") return { "__rustwright_cdp_symbol__": true };
        if (seen.has(item)) return { "__rustwright_cdp_ref__": seen.get(item) };
        const ref = ++nextRef;
        seen.set(item, ref);
        if (Array.isArray(item)) {
            return { "__rustwright_cdp_array__": ref, items: item.map(value => serialize(value)) };
        }
        const prototype = Object.getPrototypeOf(item);
        if (prototype === Object.prototype || prototype === null) {
            const mapped = {};
            for (const key of Object.keys(item)) {
                try {
                    mapped[key] = serialize(item[key]);
                } catch (_) {
                }
            }
            return { "__rustwright_cdp_object__": ref, entries: mapped };
        }
        return item;
    };
    return serialize(value);
})"#;

fn make_evaluate_expression(expression: &str, arg_json: Option<&str>) -> String {
    let trimmed = expression.trim();
    if let Some(arg_json) = arg_json {
        format!(
            "(async () => {{ const __rw_fn = ({trimmed}); return await __rw_fn({arg_json}); }})()"
        )
    } else if looks_like_function(trimmed) {
        format!("(async () => {{ const __rw_fn = ({trimmed}); return await __rw_fn(); }})()")
    } else if let Some(wrapped) = wrap_declaration_helper_script(trimmed) {
        wrapped
    } else {
        // Plain expression/statement string. Run it through an indirect `eval`
        // so that top-level `let`/`const`/`class` declarations are scoped to
        // this single evaluation instead of leaking into the global lexical
        // environment. Passing the raw source to `Runtime.evaluate` executes it
        // at the REPL-style global top level, where those declarations persist
        // across calls and make repeated evaluation of the same script fail
        // with "Identifier '...' has already been declared". Playwright avoids
        // this by running non-function expressions via indirect `eval` in its
        // utility script; this mirrors that, including preserving the script's
        // completion value and the standard `var`/`function` global hoisting.
        let literal = serde_json::to_string(trimmed).unwrap_or_else(|_| "\"\"".to_string());
        format!("(0, eval)({literal})")
    }
}

fn wrap_declaration_helper_script(expression: &str) -> Option<String> {
    if !starts_with_lexical_declaration(expression) {
        return None;
    }
    let function_names = statement_function_declaration_names(expression);
    if function_names.is_empty() {
        return None;
    }
    let exports = function_names
        .iter()
        .map(|name| format!("if (typeof {name} !== \"undefined\") globalThis.{name} = {name};"))
        .collect::<Vec<_>>()
        .join("\n");
    Some(format!("{{\n{expression}\n{exports}\n}}"))
}

fn starts_with_lexical_declaration(expression: &str) -> bool {
    let expression = trim_leading_js_comments(expression);
    starts_with_js_keyword(expression, "let") || starts_with_js_keyword(expression, "const")
}

fn trim_leading_js_comments(mut expression: &str) -> &str {
    loop {
        expression = expression.trim_start();
        if let Some(rest) = expression.strip_prefix("//") {
            if let Some(newline_index) = rest.find('\n') {
                expression = &rest[newline_index + 1..];
                continue;
            }
            return "";
        }
        if let Some(rest) = expression.strip_prefix("/*") {
            if let Some(end_index) = rest.find("*/") {
                expression = &rest[end_index + 2..];
                continue;
            }
            return "";
        }
        return expression;
    }
}

fn starts_with_js_keyword(expression: &str, keyword: &str) -> bool {
    let Some(rest) = expression.strip_prefix(keyword) else {
        return false;
    };
    rest.chars()
        .next()
        .map(|ch| !is_js_identifier_continue(ch))
        .unwrap_or(true)
}

fn statement_function_declaration_names(expression: &str) -> Vec<String> {
    let mut names = Vec::new();
    let mut index = 0usize;

    while index < expression.len() {
        let rest = &expression[index..];
        if rest.starts_with("//") {
            index += rest.find('\n').unwrap_or(rest.len());
            continue;
        }
        if rest.starts_with("/*") {
            index += rest
                .find("*/")
                .map(|comment_end| comment_end + 2)
                .unwrap_or(rest.len());
            continue;
        }
        let Some(ch) = rest.chars().next() else {
            break;
        };
        if matches!(ch, '"' | '\'' | '`') {
            index = skip_js_string(expression, index, ch);
            continue;
        }
        if let Some(function_index) = function_keyword_index(expression, index) {
            if let Some(name) =
                function_declaration_name(&expression[function_index + "function".len()..])
            {
                names.push(name);
            }
        }
        index += ch.len_utf8();
    }

    names.sort();
    names.dedup();
    names
}

fn function_keyword_index(expression: &str, index: usize) -> Option<usize> {
    if is_js_keyword_at(expression, index, "function") {
        return Some(index);
    }
    if is_js_keyword_at(expression, index, "async") {
        let after_async = index + "async".len();
        let function_index = expression[after_async..]
            .char_indices()
            .find(|(_, ch)| !ch.is_whitespace())
            .map(|(offset, _)| after_async + offset)?;
        if is_js_keyword_at(expression, function_index, "function") {
            return Some(function_index);
        }
    }
    None
}

fn is_js_keyword_at(expression: &str, index: usize, keyword: &str) -> bool {
    let Some(rest) = expression.get(index..) else {
        return false;
    };
    if !rest.starts_with(keyword) {
        return false;
    }
    let before_boundary = expression[..index]
        .chars()
        .next_back()
        .map(|ch| !is_js_identifier_continue(ch))
        .unwrap_or(true);
    let after_boundary = expression[index + keyword.len()..]
        .chars()
        .next()
        .map(|ch| !is_js_identifier_continue(ch))
        .unwrap_or(true);
    before_boundary && after_boundary
}

fn skip_js_string(expression: &str, start: usize, quote: char) -> usize {
    let mut escaped = false;
    let mut index = start + quote.len_utf8();
    while index < expression.len() {
        let rest = &expression[index..];
        let Some(ch) = rest.chars().next() else {
            return expression.len();
        };
        index += ch.len_utf8();
        if escaped {
            escaped = false;
            continue;
        }
        if ch == '\\' {
            escaped = true;
            continue;
        }
        if ch == quote {
            break;
        }
    }
    index
}

fn function_declaration_name(after_function: &str) -> Option<String> {
    let mut rest = after_function.trim_start();
    if let Some(after_generator) = rest.strip_prefix('*') {
        rest = after_generator.trim_start();
    }
    parse_js_identifier_prefix(rest)
}

fn parse_js_identifier_prefix(value: &str) -> Option<String> {
    let mut chars = value.chars();
    let first = chars.next()?;
    if !is_js_identifier_start(first) {
        return None;
    }
    let mut identifier = String::from(first);
    for ch in chars {
        if !is_js_identifier_continue(ch) {
            break;
        }
        identifier.push(ch);
    }
    Some(identifier)
}

fn looks_like_function(expression: &str) -> bool {
    expression.starts_with("function")
        || expression.starts_with("async function")
        || expression
            .find("=>")
            .map(|index| {
                let before_arrow = expression[..index].trim();
                if before_arrow.starts_with('(') {
                    return true;
                }
                if let Some(parameter) = before_arrow.strip_prefix("async ") {
                    let parameter = parameter.trim();
                    return parameter.starts_with('(') || is_js_identifier(parameter);
                }
                is_js_identifier(before_arrow)
            })
            .unwrap_or(false)
}

fn is_js_identifier(value: &str) -> bool {
    let mut chars = value.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    if !is_js_identifier_start(first) {
        return false;
    }
    chars.all(is_js_identifier_continue)
}

fn is_js_identifier_start(ch: char) -> bool {
    ch == '_' || ch == '$' || ch.is_ascii_alphabetic()
}

fn is_js_identifier_continue(ch: char) -> bool {
    ch == '_' || ch == '$' || ch.is_ascii_alphanumeric()
}

fn runtime_exception_message(exception: &Value) -> String {
    if let Some(description) = exception
        .pointer("/exception/description")
        .and_then(Value::as_str)
    {
        return description.to_string();
    }
    if let Some(remote_exception) = exception.get("exception") {
        if remote_exception
            .get("type")
            .and_then(Value::as_str)
            .map(|value| value == "undefined")
            .unwrap_or(false)
        {
            return "undefined".to_string();
        }
        if let Some(value) = remote_exception.get("value") {
            if value.is_null() {
                return "null".to_string();
            }
            if let Some(text) = value.as_str() {
                return text.to_string();
            }
            return value.to_string();
        }
        if let Some(class_name) = remote_exception.get("className").and_then(Value::as_str) {
            return class_name.to_string();
        }
    }
    exception
        .get("text")
        .and_then(Value::as_str)
        .unwrap_or("JavaScript evaluation failed")
        .to_string()
}

fn runtime_result_to_json(result: &Value) -> RwResult<String> {
    if let Some(exception) = result.get("exceptionDetails") {
        return Err(RwError::Message(runtime_exception_message(exception)));
    }
    let remote = result.get("result").unwrap_or(&Value::Null);
    let value = if remote
        .get("type")
        .and_then(Value::as_str)
        .map(|value| value == "undefined")
        .unwrap_or(false)
    {
        Value::Null
    } else if let Some(value) = remote.get("value") {
        value.clone()
    } else if let Some(value) = remote.get("unserializableValue").and_then(Value::as_str) {
        json!({ "__rustwright_cdp_unserializable_value__": value })
    } else {
        Value::Null
    };
    Ok(value.to_string())
}

async fn runtime_result_to_json_with_serializer(
    client: &CdpClient,
    session_id: &str,
    result: &Value,
    timeout: Duration,
) -> RwResult<String> {
    if result.get("exceptionDetails").is_some() {
        return runtime_result_to_json(result);
    }
    let remote = result.get("result").unwrap_or(&Value::Null);
    let Some(object_id) = remote.get("objectId").and_then(Value::as_str) else {
        return runtime_result_to_json(result);
    };
    let serialized = client
        .send(
            "Runtime.callFunctionOn",
            json!({
                "objectId": object_id,
                "functionDeclaration": format!(
                    "function() {{ return ({RUNTIME_VALUE_SERIALIZER})(this); }}"
                ),
                "awaitPromise": true,
                "returnByValue": true,
                "userGesture": true,
            }),
            Some(session_id),
            timeout,
        )
        .await;
    let _ = client
        .send(
            "Runtime.releaseObject",
            json!({ "objectId": object_id }),
            Some(session_id),
            Duration::from_secs(1),
        )
        .await;
    runtime_result_to_json(&serialized?)
}

fn runtime_result_to_remote_object(result: &Value) -> RwResult<String> {
    if let Some(exception) = result.get("exceptionDetails") {
        return Err(RwError::Message(runtime_exception_message(exception)));
    }
    Ok(result
        .get("result")
        .cloned()
        .unwrap_or(Value::Null)
        .to_string())
}

fn runtime_result_to_remote_object_with_session(
    result: &Value,
    session_id: &str,
) -> RwResult<String> {
    if let Some(exception) = result.get("exceptionDetails") {
        return Err(RwError::Message(runtime_exception_message(exception)));
    }
    let mut remote = result.get("result").cloned().unwrap_or(Value::Null);
    if let Some(object) = remote.as_object_mut() {
        object.insert(
            "__rustwright_session_id".to_string(),
            Value::String(session_id.to_string()),
        );
    }
    Ok(remote.to_string())
}

fn simple_css_locator_spec(value: &Value) -> bool {
    let Some(object) = value.as_object() else {
        return false;
    };
    match object.get("kind").and_then(Value::as_str) {
        Some("css") => {
            object.len() == 2 && object.get("selector").and_then(Value::as_str).is_some()
        }
        Some("nth") => {
            object.len() == 3
                && object.get("index").and_then(Value::as_i64).is_some()
                && object
                    .get("base")
                    .map(simple_css_locator_spec)
                    .unwrap_or(false)
        }
        _ => false,
    }
}

fn fast_css_locator_script(locator_json: &str, index: usize, body: &str) -> String {
    let needs_visible = body.contains("visible(") || body.contains("visibleForRole(");
    let needs_visible_for_role = body.contains("visibleForRole(");
    let needs_disabled = body.contains("disabledState(");
    let needs_role = needs_disabled || needs_visible_for_role || body.contains("locatorRoleOf(");
    let needs_common_accessibility = body_can_use_fast_common_accessibility(body);
    let visible_helper = if needs_visible {
        r#"
  const visible = el => {
    if (!el || !el.isConnected) return false;
    if ((el.tagName || '') === 'OPTION') return el.parentElement ? visible(el.parentElement) : false;
    const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
    const style = view.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
"#
    } else {
        ""
    };
    let common_accessibility_helper = if needs_common_accessibility && needs_role {
        r#"
  const booleanAttribute = (el, property, attribute) => {
    if (property in el) return !!el[property];
    const value = String(el.getAttribute(attribute) || '').toLowerCase();
    if (value === 'true') return true;
    if (value === 'false') return false;
    return null;
  };
  const headingLevel = el => {
    const match = /^H([1-6])$/.exec(el.tagName || '');
    if (match) return Number(match[1]);
    const raw = el.getAttribute('aria-level');
    if (raw == null || String(raw).trim() === '') return 0;
    const value = Number(raw);
    return Number.isFinite(value) ? value : 0;
  };
"#
    } else if needs_common_accessibility {
        r#"
  const normalize = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const fastAccessibilityKnownRoles = new Set([
    'alert', 'alertdialog', 'application', 'article', 'banner', 'blockquote', 'button',
    'caption', 'cell', 'checkbox', 'code', 'columnheader', 'combobox', 'complementary',
    'contentinfo', 'definition', 'deletion', 'dialog', 'directory', 'document', 'emphasis',
    'feed', 'figure', 'form', 'generic', 'grid', 'gridcell', 'group', 'heading', 'img',
    'insertion', 'link', 'list', 'listbox', 'listitem', 'log', 'main', 'mark', 'marquee', 'math',
    'meter', 'menu', 'menubar', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'navigation',
    'none', 'note', 'option', 'paragraph', 'presentation', 'progressbar', 'radio',
    'radiogroup', 'region', 'row', 'rowgroup', 'rowheader', 'scrollbar', 'search',
    'searchbox', 'separator', 'slider', 'spinbutton', 'status', 'strong', 'subscript',
    'superscript', 'switch', 'tab', 'table', 'tablist', 'tabpanel', 'term', 'textbox',
    'time', 'timer', 'toolbar', 'tooltip', 'tree', 'treegrid', 'treeitem'
  ]);
  const explicitRoleOf = node => {
    for (const token of String(node && node.getAttribute ? node.getAttribute('role') || '' : '').trim().split(/\s+/).filter(Boolean)) {
      if (fastAccessibilityKnownRoles.has(token)) return token;
    }
    return '';
  };
  const hiddenForName = (node, root) => {
    if (!node || node.nodeType !== 1 || node === root) return false;
    if (node.hasAttribute('hidden')) return true;
    if (String(node.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return true;
    const view = (node.ownerDocument && node.ownerDocument.defaultView) || window;
    const style = view.getComputedStyle(node);
    return style.visibility === 'hidden' || style.display === 'none';
  };
  const descendantText = (node, root) => {
    if (!node) return '';
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
    if (node.nodeType !== Node.ELEMENT_NODE || hiddenForName(node, root)) return '';
    const tag = node.tagName || '';
    const type = String(node.getAttribute('type') || '').toLowerCase();
    if (tag === 'IMG') return node.getAttribute('alt') || node.getAttribute('title') || '';
    if (String(tag).toLowerCase() === 'svg') {
      const title = node.querySelector('title');
      return title ? title.textContent || '' : node.getAttribute('title') || '';
    }
    if (explicitRoleOf(node) === 'img') return node.getAttribute('aria-label') || node.getAttribute('title') || '';
    if (tag === 'INPUT' && type === 'image') return node.getAttribute('alt') || node.getAttribute('title') || 'Submit';
    if (tag === 'INPUT' && type === 'file') return 'Choose File';
    if (tag === 'INPUT' && ['button', 'submit', 'reset'].includes(type)) {
      if (node.value) return node.value;
      if (node.getAttribute('title')) return node.getAttribute('title') || '';
      if (type === 'submit') return 'Submit';
      if (type === 'reset') return 'Reset';
      return '';
    }
    return Array.from(node.childNodes || []).map(child => descendantText(child, root)).join(' ');
  };
  const referencedText = (el, attribute) => {
    const doc = el.ownerDocument || document;
    const referencedControlText = node => {
      const tag = node && node.tagName || '';
      const type = String(node && node.getAttribute ? node.getAttribute('type') || '' : '').toLowerCase();
      if (tag === 'TEXTAREA') return node.value || node.textContent || '';
      if (tag === 'SELECT') {
        return Array.from(node.selectedOptions || [])
          .map(option => option.label || option.innerText || option.textContent || '')
          .join(' ');
      }
      if (tag === 'INPUT') {
        if (type === 'image') return node.getAttribute('alt') || node.getAttribute('title') || node.value || '';
        if (['button', 'submit', 'reset'].includes(type)) {
          if (node.value) return node.value;
          if (type === 'submit') return 'Submit';
          if (type === 'reset') return 'Reset';
          return '';
        }
        if (type === 'hidden') return '';
        return node.value || '';
      }
      return '';
    };
    const referencedNodeText = node => {
      if (!node || node.nodeType !== 1) return '';
      const controlText = referencedControlText(node);
      if (controlText) return controlText;
      const ariaLabel = node.getAttribute('aria-label');
      if (ariaLabel) return ariaLabel;
      const tag = node.tagName || '';
      if (tag === 'IMG') return node.getAttribute('alt') || node.getAttribute('title') || '';
      if (String(tag).toLowerCase() === 'svg') {
        const title = node.querySelector('title');
        return title ? title.textContent || '' : node.getAttribute('title') || '';
      }
      return descendantText(node, node) || node.innerText || node.textContent || '';
    };
    return String(el.getAttribute(attribute) || '')
      .split(/\s+/)
      .filter(Boolean)
      .map(id => referencedNodeText(doc.getElementById(id)))
      .join(' ');
  };
  const booleanAttribute = (el, property, attribute) => {
    if (property in el) return !!el[property];
    const value = String(el.getAttribute(attribute) || '').toLowerCase();
    if (value === 'true') return true;
    if (value === 'false') return false;
    return null;
  };
  const headingLevel = el => {
    const match = /^H([1-6])$/.exec(el.tagName || '');
    if (match) return Number(match[1]);
    const raw = el.getAttribute('aria-level');
    if (raw == null || String(raw).trim() === '') return 0;
    const value = Number(raw);
    return Number.isFinite(value) ? value : 0;
  };
"#
    } else {
        ""
    };
    let role_helper = if needs_role {
        r#"
  const knownRoles = new Set([
    'alert', 'alertdialog', 'application', 'article', 'banner', 'blockquote', 'button',
    'caption', 'cell', 'checkbox', 'code', 'columnheader', 'combobox', 'complementary',
    'contentinfo', 'definition', 'deletion', 'dialog', 'directory', 'document', 'emphasis',
    'feed', 'figure', 'form', 'generic', 'grid', 'gridcell', 'group', 'heading', 'img',
    'insertion', 'link', 'list', 'listbox', 'listitem', 'log', 'main', 'mark', 'marquee', 'math',
    'meter', 'menu', 'menubar', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'navigation',
    'none', 'note', 'option', 'paragraph', 'presentation', 'progressbar', 'radio',
    'radiogroup', 'region', 'row', 'rowgroup', 'rowheader', 'scrollbar', 'search',
    'searchbox', 'separator', 'slider', 'spinbutton', 'status', 'strong', 'subscript',
    'superscript', 'switch', 'tab', 'table', 'tablist', 'tabpanel', 'term', 'textbox',
    'time', 'timer', 'toolbar', 'tooltip', 'tree', 'treegrid', 'treeitem'
  ]);
  const explicitRoleOf = el => {
    for (const token of String(el.getAttribute('role') || '').trim().split(/\s+/).filter(Boolean)) {
      if (knownRoles.has(token)) return token;
    }
    return '';
  };
  const normalize = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const hiddenForName = (node, root) => {
    if (!node || node.nodeType !== 1 || node === root) return false;
    if (node.hasAttribute('hidden')) return true;
    if (String(node.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return true;
    const view = (node.ownerDocument && node.ownerDocument.defaultView) || window;
    const style = view.getComputedStyle(node);
    return style.visibility === 'hidden' || style.display === 'none';
  };
  const descendantText = (node, root) => {
    if (!node) return '';
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
    if (node.nodeType !== Node.ELEMENT_NODE || hiddenForName(node, root)) return '';
    const tag = node.tagName || '';
    const type = String(node.getAttribute('type') || '').toLowerCase();
    if (tag === 'IMG') return node.getAttribute('alt') || node.getAttribute('title') || '';
    if (String(tag).toLowerCase() === 'svg') {
      const title = node.querySelector('title');
      return title ? title.textContent || '' : '';
    }
    if (explicitRoleOf(node) === 'img') return node.getAttribute('aria-label') || node.getAttribute('title') || '';
    if (tag === 'INPUT' && type === 'image') return node.getAttribute('alt') || node.getAttribute('title') || 'Submit';
    if (tag === 'INPUT' && type === 'file') return 'Choose File';
    if (tag === 'INPUT' && ['button', 'submit', 'reset'].includes(type)) {
      if (node.value) return node.value;
      if (node.getAttribute('title')) return node.getAttribute('title') || '';
      if (type === 'submit') return 'Submit';
      if (type === 'reset') return 'Reset';
      return '';
    }
    return Array.from(node.childNodes || []).map(child => descendantText(child, root)).join(' ');
  };
  const referencedText = (el, attribute) => {
    const doc = el.ownerDocument || document;
    const referencedControlText = node => {
      const tag = node && node.tagName || '';
      const type = String(node && node.getAttribute('type') || '').toLowerCase();
      if (tag === 'TEXTAREA') return node.value || node.textContent || '';
      if (tag === 'SELECT') {
        return Array.from(node.selectedOptions || [])
          .map(option => option.label || option.innerText || option.textContent || '')
          .join(' ');
      }
      if (tag === 'INPUT') {
        if (type === 'image') return node.getAttribute('alt') || node.getAttribute('title') || node.value || '';
        if (['button', 'submit', 'reset'].includes(type)) {
          if (node.value) return node.value;
          if (type === 'submit') return 'Submit';
          if (type === 'reset') return 'Reset';
          return '';
        }
        if (type === 'hidden') return '';
        return node.value || '';
      }
      return '';
    };
    const referencedNodeText = node => {
      if (!node || node.nodeType !== 1) return '';
      const controlText = referencedControlText(node);
      if (controlText) return controlText;
      const ariaLabel = node.getAttribute('aria-label');
      if (ariaLabel) return ariaLabel;
      const tag = node.tagName || '';
      if (tag === 'IMG') return node.getAttribute('alt') || node.getAttribute('title') || '';
      if (String(tag).toLowerCase() === 'svg') {
        const title = node.querySelector('title');
        return title ? title.textContent || '' : node.getAttribute('title') || '';
      }
      return descendantText(node, node) || node.innerText || node.textContent || '';
    };
    return String(el.getAttribute(attribute) || '')
      .split(/\s+/)
      .filter(Boolean)
      .map(id => referencedNodeText(doc.getElementById(id)))
      .join(' ');
  };
  const explicitAccessibleName = el => normalize(referencedText(el, 'aria-labelledby') || el.getAttribute('aria-label') || '');
  const presentationalRoleOf = el => {
    const role = explicitRoleOf(el);
    return role === 'none' || role === 'presentation' ? role : '';
  };
  const presentationalTableConflictNativeRoleOf = el => {
    if ((el.tagName || '') !== 'TABLE') return '';
    if (!presentationalRoleOf(el)) return '';
    return el.hasAttribute('tabindex') || explicitAccessibleName(el) ? 'table' : '';
  };
  const presentationalTableAncestorRoleOf = el => {
    const tag = el.tagName || '';
    if (!['THEAD', 'TBODY', 'TFOOT', 'TR', 'TD', 'TH'].includes(tag)) return '';
    let current = el.parentElement;
    while (current && current.nodeType === 1) {
      if ((current.tagName || '') === 'TABLE') {
        const role = presentationalRoleOf(current);
        return role && !presentationalTableConflictNativeRoleOf(current) ? role : '';
      }
      current = current.parentElement;
    }
    return '';
  };
  const presentationalConflictNativeRoleOf = el => {
    const tag = el.tagName || '';
    const type = String(el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'BUTTON') return 'button';
    if (tag === 'A' && el.hasAttribute('href')) return 'link';
    if (tag === 'IMG' || String(tag).toLowerCase() === 'svg') return el.hasAttribute('tabindex') || explicitAccessibleName(el) ? 'img' : '';
    if (tag === 'TABLE') return presentationalTableConflictNativeRoleOf(el);
    if (tag === 'SELECT') return el.multiple || Number(el.getAttribute('size') || 0) > 1 ? 'listbox' : 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'range') return 'slider';
      if (type === 'search') return 'searchbox';
      if (type === 'number') return 'spinbutton';
      if (['button', 'submit', 'reset', 'image', 'file'].includes(type)) return 'button';
      if (!['hidden', 'file', 'image'].includes(type)) return 'textbox';
    }
    return '';
  };
  const hasScopedLandmarkAncestor = el => {
    let current = el.parentElement;
    while (current && current.nodeType === 1) {
      const tag = current.tagName || '';
      if (['ARTICLE', 'ASIDE', 'MAIN', 'NAV', 'SECTION'].includes(tag)) return true;
      const role = explicitRoleOf(current);
      if (['article', 'complementary', 'main', 'navigation', 'region'].includes(role)) return true;
      current = current.parentElement;
    }
    return false;
  };
  const computeLocatorRoleOf = el => {
    if (!el || el.nodeType !== 1) return '';
    const explicit = explicitRoleOf(el);
    if (explicit === 'none' || explicit === 'presentation') {
      const nativeRole = presentationalConflictNativeRoleOf(el);
      if (nativeRole) return nativeRole;
      if (explicitAccessibleName(el)) return '';
      return explicit;
    }
    if (explicit) return explicit;
    const tag = el.tagName || '';
    const type = String(el.getAttribute('type') || 'text').toLowerCase();
    const tablePresentationRole = presentationalTableAncestorRoleOf(el);
    if (tablePresentationRole) return tablePresentationRole;
    if (tag === 'HTML') return 'document';
    if (tag === 'BUTTON') return 'button';
    if (tag === 'A' && el.hasAttribute('href')) return 'link';
    if (/^H[1-6]$/.test(tag)) return 'heading';
    if (tag === 'IMG') {
      if (el.hasAttribute('alt') && el.getAttribute('alt') === '') {
        const nameSource = String(el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.getAttribute('title') || '').trim();
        if (!nameSource && !el.hasAttribute('tabindex')) return '';
      }
      return 'img';
    }
    if (String(tag).toLowerCase() === 'svg') return 'img';
    if (tag === 'FIGURE') return 'figure';
    if (tag === 'DFN' || tag === 'DT') return 'term';
    if (tag === 'DD') return 'definition';
    if (String(tag).toLowerCase() === 'math') return 'math';
    if (tag === 'MARK') return 'mark';
    if (tag === 'HEADER') return hasScopedLandmarkAncestor(el) ? '' : 'banner';
    if (tag === 'FOOTER') return hasScopedLandmarkAncestor(el) ? '' : 'contentinfo';
    if (tag === 'ASIDE') return 'complementary';
    if (tag === 'ARTICLE') return 'article';
    if (tag === 'BLOCKQUOTE') return 'blockquote';
    if (tag === 'CAPTION') return 'caption';
    if (tag === 'CODE') return 'code';
    if (tag === 'DEL') return 'deletion';
    if (tag === 'EM') return 'emphasis';
    if (tag === 'INS') return 'insertion';
    if (tag === 'FORM') return explicitAccessibleName(el) ? 'form' : '';
    if (tag === 'SECTION') return explicitAccessibleName(el) ? 'region' : '';
    if (tag === 'UL' || tag === 'OL') return 'list';
    if (tag === 'LI') return 'listitem';
    if (tag === 'TABLE') return 'table';
    if (['THEAD', 'TBODY', 'TFOOT'].includes(tag)) return 'rowgroup';
    if (tag === 'TR') return 'row';
    if (tag === 'TD') return 'cell';
    if (tag === 'TH') {
      const scope = String(el.getAttribute('scope') || '').toLowerCase();
      if (scope === 'col' || scope === 'colgroup') return 'columnheader';
      if (scope === 'row' || scope === 'rowgroup') return 'rowheader';
      return el.closest('thead') ? 'columnheader' : 'rowheader';
    }
    if (tag === 'OPTION') return 'option';
    if (tag === 'P') return 'paragraph';
    if (tag === 'PROGRESS') return 'progressbar';
    if (tag === 'METER') return 'meter';
    if (tag === 'OUTPUT') return 'status';
    if (tag === 'SEARCH') return 'search';
    if (tag === 'STRONG') return 'strong';
    if (tag === 'SUB') return 'subscript';
    if (tag === 'SUP') return 'superscript';
    if (tag === 'TIME') return 'time';
    if (tag === 'HR') return 'separator';
    if (tag === 'DETAILS' || tag === 'FIELDSET') return 'group';
    if (tag === 'DIALOG') return 'dialog';
    if (tag === 'NAV') return 'navigation';
    if (tag === 'MAIN') return 'main';
    if (tag === 'SELECT') return el.multiple || Number(el.getAttribute('size') || 0) > 1 ? 'listbox' : 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'range') return 'slider';
      if (type === 'search') return 'searchbox';
      if (type === 'number') return 'spinbutton';
      if (['button', 'submit', 'reset', 'image', 'file'].includes(type)) return 'button';
      if (!['hidden', 'file', 'image'].includes(type)) return 'textbox';
    }
    return '';
  };
  const locatorRoleCache = new WeakMap();
  const locatorRoleOf = el => {
    if (!el || el.nodeType !== 1) return '';
    if (locatorRoleCache.has(el)) return locatorRoleCache.get(el);
    const role = computeLocatorRoleOf(el);
    locatorRoleCache.set(el, role);
    return role;
  };
"#
    } else {
        ""
    };
    let disabled_helper = if needs_disabled {
        r#"
  const disabledState = el => {
    const tag = el.tagName || '';
    const nativeDisableable = ['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'OPTION', 'OPTGROUP'].includes(tag);
    if (nativeDisableable && typeof el.matches === 'function' && el.matches(':disabled')) return true;
    const role = locatorRoleOf(el);
    if (!role || role === 'none' || role === 'presentation') return false;
    let current = el;
    while (current && current.nodeType === 1) {
      if (String(current.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return true;
      current = current.parentElement;
    }
    return false;
  };
"#
    } else {
        ""
    };
    let visible_for_role_helper = if needs_visible_for_role {
        r#"
  const visibleForRole = el => {
    if (!el || !el.isConnected) return false;
    if ((el.tagName || '') === 'OPTION') return el.parentElement ? visibleForRole(el.parentElement) : false;
    return visible(el);
  };
"#
    } else {
        ""
    };
    let template = r#"
(() => {
  const spec = __SPEC__;
  const index = __INDEX__;
  const queryAllDeep = (root, selector) => {
    root = root || document;
    const result = [];
    const seen = new Set();
    const add = el => {
      if (el && el.nodeType === 1 && !seen.has(el)) {
        seen.add(el);
        result.push(el);
      }
    };
    const visit = scope => {
      for (const el of Array.from(scope.querySelectorAll(selector))) add(el);
      for (const el of Array.from(scope.querySelectorAll('*'))) {
        if (el.shadowRoot) visit(el.shadowRoot);
      }
    };
    visit(root);
    return result;
  };
  __VISIBLE_HELPER__
  __COMMON_ACCESSIBILITY_HELPER__
  __ROLE_HELPER__
  __DISABLED_HELPER__
  __VISIBLE_FOR_ROLE_HELPER__
  const all = current => allIn(current, document);
  const allIn = (current, root) => {
    root = root || document;
    if (!current || current.kind === 'css') {
      return queryAllDeep(root, current ? current.selector : '*');
    }
    if (current.kind === 'nth') {
      const elements = current.base ? allIn(current.base, root) : [root];
      if (!Number.isInteger(current.index)) return [];
      const nth = current.index < 0 ? elements.length + current.index : current.index;
      const element = elements[nth] || null;
      return element ? [element] : [];
    }
    return [];
  };
  const strictFrameViolation = null;
  const matches = all(spec);
  const el = matches[index] || null;
  __BODY__
})()
"#;
    template
        .replace("__SPEC__", locator_json)
        .replace("__INDEX__", &index.to_string())
        .replace("__VISIBLE_HELPER__", visible_helper)
        .replace(
            "__COMMON_ACCESSIBILITY_HELPER__",
            common_accessibility_helper,
        )
        .replace("__ROLE_HELPER__", role_helper)
        .replace("__DISABLED_HELPER__", disabled_helper)
        .replace("__VISIBLE_FOR_ROLE_HELPER__", visible_for_role_helper)
        .replace("__BODY__", body)
}

fn body_can_use_fast_common_accessibility(body: &str) -> bool {
    body.contains("ariaSnapshotKnownRoles") && body.contains("snapshotNodes")
}

fn body_requires_full_locator_runtime(body: &str) -> bool {
    (body.contains("referencedText(")
        || body.contains("headingLevel(")
        || body.contains("booleanAttribute("))
        && !body_can_use_fast_common_accessibility(body)
}

fn locator_script(locator_json: &str, index: usize, body: &str) -> String {
    if !body_requires_full_locator_runtime(body)
        && serde_json::from_str::<Value>(locator_json)
            .ok()
            .filter(simple_css_locator_spec)
            .is_some()
    {
        return fast_css_locator_script(locator_json, index, body);
    }

    let template = r#"
(() => {
  const spec = __SPEC__;
  const index = __INDEX__;

  const normalize = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const includesText = (value, needle, exact) => {
    const raw = String(value ?? '');
    if (needle && typeof needle === 'object' && needle.kind === 'regex') {
      try {
        return new RegExp(String(needle.pattern || ''), String(needle.flags || '')).test(raw);
      } catch (_) {
        return false;
      }
    }
    const left = normalize(raw);
    const right = normalize(needle);
    return exact ? left === right : left.toLowerCase().includes(right.toLowerCase());
  };
  const isEmptyStringTextMatcher = needle => (
    typeof needle === 'string' && normalize(needle) === ''
  );
  const textCandidate = el => ![
    'HTML', 'HEAD', 'BODY', 'SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE',
    'TITLE', 'META', 'LINK', 'BASE'
  ].includes(el && el.tagName || '');
  const visible = el => {
    if (!el || !el.isConnected) return false;
    if ((el.tagName || '') === 'OPTION') return el.parentElement ? visible(el.parentElement) : false;
    const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
    const style = view.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const visibleForRole = el => {
    if (!el || !el.isConnected) return false;
    if ((el.tagName || '') === 'OPTION') return el.parentElement ? visibleForRole(el.parentElement) : false;
    const hiddenByClosedDetails = node => {
      let current = node;
      while (current && current.nodeType === 1) {
        if (current !== node && (current.tagName || '') === 'DETAILS' && !current.open) {
          const summary = Array.from(current.children || []).find(child => (child.tagName || '') === 'SUMMARY') || null;
          if (!summary || (node !== summary && !summary.contains(node))) return true;
        }
        current = current.parentElement;
      }
      return false;
    };
    if (hiddenByClosedDetails(el)) return false;
    let current = el;
    while (current && current.nodeType === 1) {
      if (current.hasAttribute('hidden')) return false;
      if (String(current.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return false;
      const view = (current.ownerDocument && current.ownerDocument.defaultView) || window;
      const style = view.getComputedStyle(current);
      if (style.visibility === 'hidden' || style.display === 'none') return false;
      current = current.parentElement;
    }
    return true;
  };
  const queryAllDeep = (root, selector) => {
    root = root || document;
    const result = [];
    const seen = new Set();
    const add = el => {
      if (el && el.nodeType === 1 && !seen.has(el)) {
        seen.add(el);
        result.push(el);
      }
    };
    const visit = scope => {
      for (const el of Array.from(scope.querySelectorAll(selector))) add(el);
      for (const el of Array.from(scope.querySelectorAll('*'))) {
        if (el.shadowRoot) visit(el.shadowRoot);
      }
    };
    visit(root);
    return result;
  };
  const elementChildren = el => [
    ...Array.from((el && el.children) || []),
    ...Array.from((el && el.shadowRoot && el.shadowRoot.children) || []),
  ];
  const textCandidatesInScope = root => {
    const elements = queryAllDeep(root, '*').filter(textCandidate);
    if (root && root.nodeType === 1 && textCandidate(root) && !elements.includes(root)) {
      return [root, ...elements];
    }
    return elements;
  };
  const xpathAll = (selector, root) => {
    root = root || document;
    const doc = root.nodeType === 9 ? root : (root.ownerDocument || document);
    const context = root.nodeType === 9 ? doc : root;
    let expression = String(selector || '');
    if (root.nodeType !== 9 && expression.startsWith('//')) expression = `.${expression}`;
    const snapshot = doc.evaluate(expression, context, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    const result = [];
    for (let index = 0; index < snapshot.snapshotLength; index++) {
      const node = snapshot.snapshotItem(index);
      if (node && node.nodeType === 1) result.push(node);
    }
    return result;
  };
  const customAll = (current, root) => {
    const source = String(current.source || '');
    const engine = Function(`"use strict"; return (${source});`)();
    if (!engine || (typeof engine.queryAll !== 'function' && typeof engine.query !== 'function')) {
      throw new Error(`Custom selector engine ${current.engine || ''} must define queryAll() or query()`);
    }
    if (typeof engine.queryAll === 'function') {
      return Array.from(engine.queryAll(root, current.selector) || []).filter(node => node && node.nodeType === 1);
    }
    const node = engine.query(root, current.selector);
    return node && node.nodeType === 1 ? [node] : [];
  };
  const referencedText = (el, attribute) => {
    const doc = el.ownerDocument || document;
    const referencedControlText = node => {
      const tag = node.tagName || '';
      const type = String(node.getAttribute('type') || '').toLowerCase();
      if (tag === 'TEXTAREA') return node.value || node.textContent || '';
      if (tag === 'SELECT') {
        return Array.from(node.selectedOptions || [])
          .map(option => option.label || option.innerText || option.textContent || '')
          .join(' ');
      }
      if (tag === 'INPUT') {
        if (type === 'image') return node.getAttribute('alt') || node.getAttribute('title') || node.value || '';
        if (['button', 'submit', 'reset'].includes(type)) {
          if (node.value) return node.value;
          if (type === 'submit') return 'Submit';
          if (type === 'reset') return 'Reset';
          return '';
        }
        if (type === 'hidden') return '';
        return node.value || '';
      }
      return '';
    };
    const referencedNodeText = node => {
      if (!node || node.nodeType !== 1) return '';
      const controlText = referencedControlText(node);
      if (controlText) return controlText;
      const ariaLabel = node.getAttribute('aria-label');
      if (ariaLabel) return ariaLabel;
      const tag = node.tagName || '';
      if (tag === 'IMG') return node.getAttribute('alt') || node.getAttribute('title') || '';
      if (String(tag).toLowerCase() === 'svg') {
        const title = node.querySelector('title');
        return title ? title.textContent || '' : node.getAttribute('title') || '';
      }
      return descendantText(node, node) || node.innerText || node.textContent || '';
    };
    return String(el.getAttribute(attribute) || '')
      .split(/\s+/)
      .filter(Boolean)
      .map(id => {
        const node = doc.getElementById(id);
        return referencedNodeText(node);
      })
      .join(' ');
  };
  const hiddenForName = (node, root) => {
    if (!node || node.nodeType !== 1 || node === root) return false;
    if (node.hasAttribute('hidden')) return true;
    if (String(node.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return true;
    const view = (node.ownerDocument && node.ownerDocument.defaultView) || window;
    const style = view.getComputedStyle(node);
    return style.visibility === 'hidden' || style.display === 'none';
  };
  const descendantText = (node, root) => {
    if (!node) return '';
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
    if (node.nodeType !== Node.ELEMENT_NODE || hiddenForName(node, root)) return '';
    const tag = node.tagName || '';
    const type = String(node.getAttribute('type') || '').toLowerCase();
    if (tag === 'IMG') return node.getAttribute('alt') || node.getAttribute('title') || '';
    if (String(tag).toLowerCase() === 'svg') {
      const title = node.querySelector('title');
      return title ? title.textContent || '' : '';
    }
    if (explicitRoleOf(node) === 'img') return node.getAttribute('aria-label') || node.getAttribute('title') || '';
    if (tag === 'INPUT' && type === 'image') {
      return node.getAttribute('alt') || node.getAttribute('title') || 'Submit';
    }
    if (tag === 'INPUT' && type === 'file') {
      return 'Choose File';
    }
    if (tag === 'INPUT' && ['button', 'submit', 'reset'].includes(type)) {
      if (node.value) return node.value;
      if (node.getAttribute('title')) return node.getAttribute('title') || '';
      if (type === 'submit') return 'Submit';
      if (type === 'reset') return 'Reset';
      return '';
    }
    return Array.from(node.childNodes || []).map(child => descendantText(child, root)).join(' ');
  };
  const explicitAccessibleName = el => normalize(referencedText(el, 'aria-labelledby') || el.getAttribute('aria-label') || '');
  const computeAccessibleName = el => {
    const tag = el.tagName || '';
    const role = locatorRoleOf(el);
    if (role === 'term' || role === 'definition' || role === 'generic' || role === 'mark' || role === 'none' || role === 'presentation') return '';
    if (role === 'table') {
      const explicit = explicitAccessibleName(el);
      if (explicit) return explicit;
      if (tag === 'TABLE') {
        const caption = el.querySelector('caption');
        return normalize(caption ? caption.innerText || caption.textContent || '' : '');
      }
      return '';
    }
    if (role === 'group') {
      const explicit = explicitAccessibleName(el);
      if (explicit) return explicit;
      if (tag === 'FIELDSET') {
        const legend = el.querySelector('legend');
        return normalize(legend ? legend.innerText || legend.textContent || '' : '');
      }
      return '';
    }
    if ([
      'alert', 'alertdialog', 'application', 'article', 'banner', 'combobox',
      'complementary', 'contentinfo', 'dialog', 'directory', 'document', 'feed',
      'form', 'grid', 'list', 'listbox', 'listitem', 'log', 'main', 'marquee',
      'menubar', 'menu', 'navigation', 'note', 'radiogroup', 'region',
      'rowgroup', 'scrollbar', 'search', 'separator', 'status', 'tablist',
      'tabpanel', 'timer', 'toolbar', 'tree', 'treegrid',
    ].includes(role)) return explicitAccessibleName(el);
    if (role === 'figure') {
      const explicit = explicitAccessibleName(el);
      if (explicit) return explicit;
      const caption = el.querySelector('figcaption');
      if (caption) return normalize(caption.innerText || caption.textContent || '');
      return normalize(el.getAttribute('title') || '');
    }
    if (role === 'math') {
      const explicit = explicitAccessibleName(el);
      if (explicit) return explicit;
      return normalize(el.getAttribute('title') || '');
    }
    const explicitName =
      explicitAccessibleName(el) ||
      el.getAttribute('alt') ||
      (el.labels && Array.from(el.labels).map(label => descendantText(label, label) || label.innerText || label.textContent).join(' ')) ||
      '';
    if (explicitName) return normalize(explicitName);
    if (tag === 'FIELDSET') {
      const legend = el.querySelector('legend');
      return normalize(legend ? legend.innerText || legend.textContent || '' : '');
    }
    if (tag === 'DETAILS') return '';
    if (tag === 'INPUT' && String(el.getAttribute('type') || '').toLowerCase() === 'image') {
      return normalize(el.getAttribute('alt') || el.getAttribute('title') || 'Submit');
    }
    if (tag === 'INPUT' && String(el.getAttribute('type') || '').toLowerCase() === 'file') {
      return 'Choose File';
    }
    if (tag === 'INPUT') {
      const type = String(el.getAttribute('type') || 'text').toLowerCase();
      if (['button', 'submit', 'reset'].includes(type)) {
        if (el.value) return normalize(el.value);
        if (el.getAttribute('title')) return normalize(el.getAttribute('title') || '');
        if (type === 'submit') return 'Submit';
        if (type === 'reset') return 'Reset';
        return '';
      }
      if (['text', 'email', 'password', 'url', 'tel', 'search', 'color', 'date', 'datetime-local', 'month', 'time', 'week'].includes(type) || !el.hasAttribute('type')) {
        return normalize(el.getAttribute('title') || el.getAttribute('placeholder') || '');
      }
      return normalize(el.getAttribute('title') || '');
    }
    if (tag === 'TEXTAREA') {
      return normalize(el.getAttribute('title') || el.getAttribute('placeholder') || '');
    }
    return normalize(el.value || descendantText(el, el) || el.getAttribute('title') || '');
  };
  const accessibleNameCache = new WeakMap();
  const accessibleName = el => {
    if (!el || el.nodeType !== 1) return '';
    if (accessibleNameCache.has(el)) return accessibleNameCache.get(el);
    const name = computeAccessibleName(el);
    accessibleNameCache.set(el, name);
    return name;
  };
  const knownRoles = new Set([
    'alert', 'alertdialog', 'application', 'article', 'banner', 'blockquote', 'button',
    'caption', 'cell', 'checkbox', 'code', 'columnheader', 'combobox', 'complementary',
    'contentinfo', 'definition', 'deletion', 'dialog', 'directory', 'document', 'emphasis',
    'feed', 'figure', 'form', 'generic', 'grid', 'gridcell', 'group', 'heading', 'img',
    'insertion', 'link', 'list', 'listbox', 'listitem', 'log', 'main', 'mark', 'marquee', 'math',
    'meter', 'menu', 'menubar', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'navigation',
    'none', 'note', 'option', 'paragraph', 'presentation', 'progressbar', 'radio',
    'radiogroup', 'region', 'row', 'rowgroup', 'rowheader', 'scrollbar', 'search',
    'searchbox', 'separator', 'slider', 'spinbutton', 'status', 'strong', 'subscript',
    'superscript', 'switch', 'tab', 'table', 'tablist', 'tabpanel', 'term', 'textbox',
    'time', 'timer', 'toolbar', 'tooltip', 'tree', 'treegrid', 'treeitem'
  ]);
  const explicitRoleOf = el => {
    for (const token of String(el.getAttribute('role') || '').trim().split(/\s+/).filter(Boolean)) {
      if (knownRoles.has(token)) return token;
    }
    return '';
  };
  const presentationalRoleOf = el => {
    const role = explicitRoleOf(el);
    return role === 'none' || role === 'presentation' ? role : '';
  };
  const presentationalTableConflictNativeRoleOf = el => {
    if ((el.tagName || '') !== 'TABLE') return '';
    if (!presentationalRoleOf(el)) return '';
    return el.hasAttribute('tabindex') || explicitAccessibleName(el) ? 'table' : '';
  };
  const presentationalTableAncestorRoleOf = el => {
    const tag = el.tagName || '';
    if (!['THEAD', 'TBODY', 'TFOOT', 'TR', 'TD', 'TH'].includes(tag)) return '';
    let current = el.parentElement;
    while (current && current.nodeType === 1) {
      if ((current.tagName || '') === 'TABLE') {
        const role = presentationalRoleOf(current);
        return role && !presentationalTableConflictNativeRoleOf(current) ? role : '';
      }
      current = current.parentElement;
    }
    return '';
  };
  const presentationalConflictNativeRoleOf = el => {
    const tag = el.tagName || '';
    const type = String(el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'BUTTON') return 'button';
    if (tag === 'A' && el.hasAttribute('href')) return 'link';
    if (tag === 'IMG' || String(tag).toLowerCase() === 'svg') {
      return el.hasAttribute('tabindex') || explicitAccessibleName(el) ? 'img' : '';
    }
    if (tag === 'TABLE') return presentationalTableConflictNativeRoleOf(el);
    if (tag === 'SELECT') return el.multiple || Number(el.getAttribute('size') || 0) > 1 ? 'listbox' : 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'range') return 'slider';
      if (type === 'search') return 'searchbox';
      if (type === 'number') return 'spinbutton';
      if (['button', 'submit', 'reset', 'image', 'file'].includes(type)) return 'button';
      if (!['hidden', 'file', 'image'].includes(type)) return 'textbox';
    }
    return '';
  };
  const hasScopedLandmarkAncestor = el => {
    let current = el.parentElement;
    while (current && current.nodeType === 1) {
      const tag = current.tagName || '';
      if (['ARTICLE', 'ASIDE', 'MAIN', 'NAV', 'SECTION'].includes(tag)) return true;
      const role = explicitRoleOf(current);
      if (['article', 'complementary', 'main', 'navigation', 'region'].includes(role)) return true;
      current = current.parentElement;
    }
    return false;
  };
  const computeLocatorRoleOf = el => {
    if (!el || el.nodeType !== 1) return '';
    const explicit = explicitRoleOf(el);
    if (explicit === 'none' || explicit === 'presentation') {
      const nativeRole = presentationalConflictNativeRoleOf(el);
      if (nativeRole) return nativeRole;
      if (explicitAccessibleName(el)) return '';
      return explicit;
    }
    if (explicit) return explicit;
    const tag = el.tagName || '';
    const type = String(el.getAttribute('type') || 'text').toLowerCase();
    const tablePresentationRole = presentationalTableAncestorRoleOf(el);
    if (tablePresentationRole) return tablePresentationRole;
    if (tag === 'HTML') return 'document';
    if (tag === 'BUTTON') return 'button';
    if (tag === 'A' && el.hasAttribute('href')) return 'link';
    if (/^H[1-6]$/.test(tag)) return 'heading';
    if (tag === 'IMG') {
      if (el.hasAttribute('alt') && el.getAttribute('alt') === '') {
        const nameSource = String(el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.getAttribute('title') || '').trim();
        if (!nameSource && !el.hasAttribute('tabindex')) return '';
      }
      return 'img';
    }
    if (String(tag).toLowerCase() === 'svg') return 'img';
    if (tag === 'FIGURE') return 'figure';
    if (tag === 'DFN' || tag === 'DT') return 'term';
    if (tag === 'DD') return 'definition';
    if (String(tag).toLowerCase() === 'math') return 'math';
    if (tag === 'MARK') return 'mark';
    if (tag === 'HEADER') return hasScopedLandmarkAncestor(el) ? '' : 'banner';
    if (tag === 'FOOTER') return hasScopedLandmarkAncestor(el) ? '' : 'contentinfo';
    if (tag === 'ASIDE') return 'complementary';
    if (tag === 'ARTICLE') return 'article';
    if (tag === 'BLOCKQUOTE') return 'blockquote';
    if (tag === 'CAPTION') return 'caption';
    if (tag === 'CODE') return 'code';
    if (tag === 'DEL') return 'deletion';
    if (tag === 'EM') return 'emphasis';
    if (tag === 'INS') return 'insertion';
    if (tag === 'FORM') return explicitAccessibleName(el) ? 'form' : '';
    if (tag === 'SECTION') return explicitAccessibleName(el) ? 'region' : '';
    if (tag === 'UL' || tag === 'OL') return 'list';
    if (tag === 'LI') return 'listitem';
    if (tag === 'TABLE') return 'table';
    if (['THEAD', 'TBODY', 'TFOOT'].includes(tag)) return 'rowgroup';
    if (tag === 'TR') return 'row';
    if (tag === 'TD') return 'cell';
    if (tag === 'TH') {
      const scope = String(el.getAttribute('scope') || '').toLowerCase();
      if (scope === 'col' || scope === 'colgroup') return 'columnheader';
      if (scope === 'row' || scope === 'rowgroup') return 'rowheader';
      return el.closest('thead') ? 'columnheader' : 'rowheader';
    }
    if (tag === 'OPTION') return 'option';
    if (tag === 'P') return 'paragraph';
    if (tag === 'PROGRESS') return 'progressbar';
    if (tag === 'METER') return 'meter';
    if (tag === 'OUTPUT') return 'status';
    if (tag === 'SEARCH') return 'search';
    if (tag === 'STRONG') return 'strong';
    if (tag === 'SUB') return 'subscript';
    if (tag === 'SUP') return 'superscript';
    if (tag === 'TIME') return 'time';
    if (tag === 'HR') return 'separator';
    if (tag === 'DETAILS' || tag === 'FIELDSET') return 'group';
    if (tag === 'DIALOG') return 'dialog';
    if (tag === 'NAV') return 'navigation';
    if (tag === 'MAIN') return 'main';
    if (tag === 'SELECT') return el.multiple || Number(el.getAttribute('size') || 0) > 1 ? 'listbox' : 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'range') return 'slider';
      if (type === 'search') return 'searchbox';
      if (type === 'number') return 'spinbutton';
      if (['button', 'submit', 'reset', 'image', 'file'].includes(type)) return 'button';
      if (!['hidden', 'file', 'image'].includes(type)) return 'textbox';
    }
    return '';
  };
  const locatorRoleCache = new WeakMap();
  const locatorRoleOf = el => {
    if (!el || el.nodeType !== 1) return '';
    if (locatorRoleCache.has(el)) return locatorRoleCache.get(el);
    const role = computeLocatorRoleOf(el);
    locatorRoleCache.set(el, role);
    return role;
  };
  const roleSelector = role => {
    if (role === 'none' || role === 'presentation') {
      return `[role~="${role}"],table[role~="${role}"] thead,table[role~="${role}"] tbody,table[role~="${role}"] tfoot,table[role~="${role}"] tr,table[role~="${role}"] td,table[role~="${role}"] th`;
    }
    const map = {
      button: 'button,input[type="button"],input[type="submit"],input[type="reset"],input[type="image"],input[type="file"],[role="button"]',
      link: 'a[href],[role="link"]',
      textbox: 'textarea,input:not([type]),input[type="text"],input[type="email"],input[type="password"],input[type="url"],input[type="tel"],input[type="color"],input[type="date"],input[type="datetime-local"],input[type="month"],input[type="time"],input[type="week"],[role="textbox"]',
      checkbox: 'input[type="checkbox"],[role="checkbox"]',
      radio: 'input[type="radio"],[role="radio"]',
      heading: 'h1,h2,h3,h4,h5,h6,[role="heading"]',
      img: 'img,svg,[role="img"]',
      banner: 'header,[role="banner"]',
      blockquote: 'blockquote,[role="blockquote"]',
      caption: 'caption,[role="caption"]',
      contentinfo: 'footer,[role="contentinfo"]',
      code: 'code,[role="code"]',
      complementary: 'aside,[role="complementary"]',
      article: 'article,[role="article"]',
      deletion: 'del,[role="deletion"]',
      emphasis: 'em,[role="emphasis"]',
      figure: 'figure,[role="figure"]',
      form: 'form,[role="form"]',
      definition: 'dd,[role="definition"]',
      insertion: 'ins,[role="insertion"]',
      mark: 'mark,[role="mark"]',
      math: 'math,[role="math"]',
      region: 'section,[role="region"]',
      term: 'dfn,dt,[role="term"]',
      list: 'ul,ol,[role="list"]',
      listitem: 'li,[role="listitem"]',
      table: 'table,[role="table"]',
      rowgroup: 'thead,tbody,tfoot,[role="rowgroup"]',
      row: 'tr,[role="row"]',
      cell: 'td,[role="cell"]',
      columnheader: 'th,[role="columnheader"]',
      rowheader: 'th,[role="rowheader"]',
      searchbox: 'input[type="search"],[role="searchbox"]',
      spinbutton: 'input[type="number"],[role="spinbutton"]',
      slider: 'input[type="range"],[role="slider"]',
      progressbar: 'progress,[role="progressbar"]',
      meter: 'meter,[role="meter"]',
      status: 'output,[role="status"]',
      separator: 'hr,[role="separator"]',
      group: 'details,fieldset,[role="group"]',
      dialog: 'dialog,[role="dialog"]',
      document: 'html,[role="document"]',
      combobox: 'select,[role="combobox"]',
      listbox: 'select[multiple],select[size],[role="listbox"]',
      option: 'option,[role="option"]',
      navigation: 'nav,[role="navigation"]',
      main: 'main,[role="main"]',
      paragraph: 'p,[role="paragraph"]',
      search: 'search,[role="search"]',
      strong: 'strong,[role="strong"]',
      subscript: 'sub,[role="subscript"]',
      superscript: 'sup,[role="superscript"]',
      time: 'time,[role="time"]',
    };
    return map[role] ? `${map[role]},[role]` : '[role]';
  };
  const booleanAttribute = (el, property, attribute) => {
    if (property in el) return !!el[property];
    const value = String(el.getAttribute(attribute) || '').toLowerCase();
    if (value === 'true') return true;
    if (value === 'false') return false;
    return null;
  };
  const checkedStateForRoleFilter = el => {
    const tag = el.tagName || '';
    const type = String(el.getAttribute('type') || '').toLowerCase();
    if (tag === 'INPUT' && type === 'checkbox' && el.indeterminate) return 'mixed';
    const value = String(el.getAttribute('aria-checked') || '').toLowerCase();
    if (value === 'mixed') return 'mixed';
    return booleanAttribute(el, 'checked', 'aria-checked');
  };
  const expandedStateForRoleFilter = el => {
    const value = String(el.getAttribute('aria-expanded') || '').toLowerCase();
    if (value === 'true') return true;
    if (value === 'false') return false;
    if (el.hasAttribute('aria-expanded')) return false;
    return null;
  };
  const pressedStateForRoleFilter = el => {
    const value = String(el.getAttribute('aria-pressed') || '').toLowerCase();
    if (value === 'true') return true;
    if (value === 'false') return false;
    if (value === 'mixed') return 'mixed';
    return null;
  };
  const disabledState = el => {
    const tag = el.tagName || '';
    const nativeDisableable = ['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'OPTION', 'OPTGROUP'].includes(tag);
    if (nativeDisableable && typeof el.matches === 'function' && el.matches(':disabled')) return true;
    const role = locatorRoleOf(el);
    if (!role || role === 'none' || role === 'presentation') return false;
    let current = el;
    while (current && current.nodeType === 1) {
      if (String(current.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return true;
      current = current.parentElement;
    }
    return false;
  };
  const headingLevel = el => {
    const match = /^H([1-6])$/.exec(el.tagName || '');
    if (match) return Number(match[1]);
    const raw = el.getAttribute('aria-level');
    if (raw == null || String(raw).trim() === '') return 0;
    const value = Number(raw);
    return Number.isFinite(value) ? value : 0;
  };
  const roleLevelValue = current => {
    const value = current.level;
    if (value == null) return null;
    if (value && typeof value === 'object' && value.kind === 'invalid_role_level') {
      const selector = String(value.selector || '');
      const symbol = String(value.symbol || '');
      const searchFrom = selector.indexOf('[level=') + '[level='.length;
      const position = selector.indexOf(symbol, searchFrom >= '[level='.length ? searchFrom : 0);
      throw new Error(`InvalidSelectorError: Error while parsing selector \`${selector}\` - unexpected symbol "${symbol}" at position ${position} during parsing attribute value`);
    }
    if (typeof value === 'boolean') throw new Error('"level" attribute must be compared to a number');
    if (typeof value === 'number') {
      if (Number.isFinite(value)) return value;
      throw new Error('"level" attribute must be compared to a number');
    }
    if (typeof value === 'string' && value.trim() !== '') {
      const number = Number(value);
      if (Number.isFinite(number)) return number;
    }
    throw new Error('"level" attribute must be compared to a number');
  };
  const controlText = el => {
    const tag = el && el.tagName;
    if (tag === 'TEXTAREA') return el.value || el.textContent || '';
    if (tag === 'INPUT' && ['button', 'submit'].includes(String(el.type || '').toLowerCase())) {
      return el.value || '';
    }
    return '';
  };
  const selectorText = el => {
    const control = controlText(el);
    if (control) return control;
    return (el && (el.textContent || el.innerText)) || '';
  };
  const directSelectorText = el => {
    const control = controlText(el);
    if (control) return control;
    return Array.from((el && el.childNodes) || [])
      .filter(node => node.nodeType === Node.TEXT_NODE)
      .map(node => node.textContent || '')
      .join(' ');
  };
  const subtreeText = node => {
    if (!node) return '';
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const control = controlText(node);
    if (control) return control;
    return Array.from(node.childNodes || []).map(child => subtreeText(child)).filter(Boolean).join(' ');
  };
  const matchesTextPseudo = (el, matcher) => {
    if (!matcher) return true;
    const value = matcher.exact ? directSelectorText(el) : selectorText(el);
    return includesText(value, matcher.text, matcher.exact);
  };
  const rectDistance = (left, right) => {
    const dx = left.left > right.right ? left.left - right.right : right.left > left.right ? right.left - left.right : 0;
    const dy = left.top > right.bottom ? left.top - right.bottom : right.top > left.bottom ? right.top - left.bottom : 0;
    return Math.hypot(dx, dy);
  };
  const layoutScore = (candidate, anchor, kind) => {
    const candidateCenterX = (candidate.left + candidate.right) / 2;
    const candidateCenterY = (candidate.top + candidate.bottom) / 2;
    const anchorCenterX = (anchor.left + anchor.right) / 2;
    const anchorCenterY = (anchor.top + anchor.bottom) / 2;
    if (kind === 'right_of') {
      if (candidate.left < anchor.right) return null;
      return (candidate.left - anchor.right) + Math.abs(candidateCenterY - anchorCenterY);
    }
    if (kind === 'left_of') {
      if (candidate.right > anchor.left) return null;
      return (anchor.left - candidate.right) + Math.abs(candidateCenterY - anchorCenterY);
    }
    if (kind === 'above') {
      if (candidate.bottom > anchor.top) return null;
      return (anchor.top - candidate.bottom) + Math.abs(candidateCenterX - anchorCenterX);
    }
    if (kind === 'below') {
      if (candidate.top < anchor.bottom) return null;
      return (candidate.top - anchor.bottom) + Math.abs(candidateCenterX - anchorCenterX);
    }
    if (kind === 'near') return rectDistance(candidate, anchor);
    return null;
  };
  const applyLayoutSelectors = (elements, layout, root) => {
    if (!layout || !layout.length) return elements;
    let currentElements = elements;
    for (const condition of layout) {
      const kind = String(condition.kind || '');
      const defaultDistance = kind === 'near' ? 50 : Number.POSITIVE_INFINITY;
      const maxDistance = condition.distance == null ? defaultDistance : Number(condition.distance);
      const anchors = allIn(condition.selector, root)
        .filter(anchor => visible(anchor))
        .map(anchor => ({ element: anchor, rect: anchor.getBoundingClientRect() }))
        .filter(anchor => anchor.rect.width > 0 && anchor.rect.height > 0);
      const scored = [];
      currentElements.forEach((element, ordinal) => {
        if (!visible(element)) return;
        const candidate = element.getBoundingClientRect();
        if (candidate.width <= 0 || candidate.height <= 0) return;
        let best = null;
        for (const anchor of anchors) {
          if (anchor.element === element) continue;
          const distance = rectDistance(candidate, anchor.rect);
          if (distance > maxDistance) continue;
          const score = layoutScore(candidate, anchor.rect, kind);
          if (score == null) continue;
          if (best == null || score < best) best = score;
        }
        if (best != null) scored.push({ element, score: best, ordinal });
      });
      scored.sort((left, right) => (left.score - right.score) || (left.ordinal - right.ordinal));
      currentElements = scored.map(item => item.element);
    }
    return currentElements;
  };
  const all = current => allIn(current, document);
  const allIn = (current, root) => {
    root = root || document;
    if (!current || current.kind === 'css') {
      let elements = queryAllDeep(root, current.selector);
      if (current && current.has_text != null) {
        elements = elements.filter(el => includesText(subtreeText(el), current.has_text, false));
      }
      if (current && current.text_pseudo != null) {
        elements = elements.filter(el => {
          if (!matchesTextPseudo(el, current.text_pseudo)) return false;
          return !elementChildren(el).some(child => matchesTextPseudo(child, current.text_pseudo));
        });
      }
      if (current && current.visible != null) {
        elements = elements.filter(el => visible(el) === current.visible);
      }
      if (current && current.layout != null) {
        elements = applyLayoutSelectors(elements, current.layout, root);
      }
      return elements;
    }
    if (current.kind === 'xpath') {
      return xpathAll(current.selector, root);
    }
    if (current.kind === 'custom') {
      return customAll(current, root);
    }
    if (current.kind === 'frame') {
      const frames = frameElementsFor(current, root);
      const frame = frames[current.frame_index || 0] || null;
      if (!frame) return [];
      let frameDocument = null;
      try {
        frameDocument = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
      } catch (error) {
        frameDocument = null;
      }
      if (!frameDocument) return [];
      return allIn(current.inner || { kind: 'css', selector: '*' }, frameDocument);
    }
    if (current.kind === 'text') {
      const elements = textCandidatesInScope(root);
      return elements.filter(el => {
        if (!includesText(selectorText(el), current.text, current.exact)) return false;
        return !elementChildren(el).some(child => includesText(selectorText(child), current.text, current.exact));
      });
    }
    if (current.kind === 'text_selector') {
      const elements = textCandidatesInScope(root);
      return elements.filter(el => {
        const text = current.exact ? directSelectorText(el) : selectorText(el);
        if (!includesText(text, current.text, current.exact)) return false;
        return !elementChildren(el).some(child => {
          const childText = current.exact ? directSelectorText(child) : selectorText(child);
          return includesText(childText, current.text, current.exact);
        });
      });
    }
    if (current.kind === 'invalid_role') {
      throw new Error(String(current.message || 'Role must not be empty'));
    }
    if (current.kind === 'role') {
      const expectedLevel = roleLevelValue(current);
      return queryAllDeep(root, roleSelector(current.role)).filter(el => {
        if (locatorRoleOf(el) !== current.role) return false;
        if (!current.include_hidden && !visibleForRole(el)) return false;
        if (current.name != null && !includesText(accessibleName(el), current.name, current.exact)) return false;
        if (current.checked != null) {
          const checked = checkedStateForRoleFilter(el);
          if (current.checked === true && checked !== true) return false;
          if (current.checked === false && (checked === true || checked === 'mixed')) return false;
          if (current.checked === 'mixed' && checked !== 'mixed') return false;
        }
        if (current.selected != null) {
          const selected = booleanAttribute(el, 'selected', 'aria-selected');
          if (current.selected === true && selected !== true) return false;
          if (current.selected === false && selected === true) return false;
        }
        if (current.expanded != null) {
          const expanded = expandedStateForRoleFilter(el);
          if (current.expanded === true && expanded !== true) return false;
          if (current.expanded === false && expanded !== false) return false;
        }
        if (current.pressed != null) {
          const pressed = pressedStateForRoleFilter(el);
          if (current.pressed === true && pressed !== true) return false;
          if (current.pressed === false && (pressed === true || pressed === 'mixed')) return false;
          if (current.pressed === 'mixed' && pressed !== 'mixed') return false;
        }
        if (current.disabled != null && disabledState(el) !== current.disabled) return false;
        if (expectedLevel != null && headingLevel(el) !== expectedLevel) return false;
        return true;
      });
    }
    if (current.kind === 'test_id') {
      const attr = current.attribute || 'data-testid';
      return queryAllDeep(root, `[${attr}]`).filter(el => {
        const value = el.getAttribute(attr);
        if (current.value && typeof current.value === 'object' && current.value.kind === 'regex') {
          return includesText(value, current.value, false);
        }
        return value === current.value;
      });
    }
    if (current.kind === 'attribute') {
      const attr = current.attribute || '';
      return queryAllDeep(root, `[${attr}]`).filter(el => el.getAttribute(attr) === current.value);
    }
    if (current.kind === 'nth') {
      const elements = current.base ? allIn(current.base, root) : [root];
      if (!Number.isInteger(current.index)) return [];
      const index = current.index < 0 ? elements.length + current.index : current.index;
      const element = elements[index] || null;
      return element ? [element] : [];
    }
    if (current.kind === 'nth_match') {
      const elements = allIn(current.selector, root);
      const index = Number(current.index || 0) - 1;
      const element = index >= 0 ? elements[index] : null;
      return element ? [element] : [];
    }
    if (current.kind === 'placeholder') {
      return queryAllDeep(root, '[placeholder]').filter(el => includesText(el.getAttribute('placeholder'), current.value, current.exact));
    }
    if (current.kind === 'alt') {
      return queryAllDeep(root, '[alt]').filter(el => includesText(el.getAttribute('alt'), current.value, current.exact));
    }
    if (current.kind === 'title') {
      return queryAllDeep(root, '[title]').filter(el => includesText(el.getAttribute('title'), current.value, current.exact));
    }
    if (current.kind === 'label') {
      const labelledByMatches = el => {
        const doc = el.ownerDocument || document;
        const labelledReferenceText = node => {
          const tag = node && node.tagName || '';
          const type = String(node && node.getAttribute('type') || '').toLowerCase();
          if (tag === 'INPUT' && ['button', 'submit', 'reset'].includes(type)) return node.value || '';
          return node ? node.textContent || node.innerText || '' : '';
        };
        return String(el.getAttribute('aria-labelledby') || '')
          .split(/\s+/)
          .filter(Boolean)
          .some(id => {
            const node = doc.getElementById(id);
            return node && includesText(labelledReferenceText(node), current.value, current.exact);
          });
      };
      const nativeLabelMatches = el => {
        if (!el.labels || !el.labels.length) return false;
        return Array.from(el.labels).some(label => includesText(label.textContent || label.innerText || '', current.value, current.exact));
      };
      return queryAllDeep(root, '*').filter(el => {
        if (el.hasAttribute('aria-label') && includesText(el.getAttribute('aria-label'), current.value, current.exact)) return true;
        if (el.hasAttribute('aria-labelledby') && labelledByMatches(el)) return true;
        return nativeLabelMatches(el);
      });
    }
    if (current.kind === 'descendant') {
      const result = [];
      for (const base of allIn(current.base, root)) {
        for (const child of allIn(current.inner, base)) {
          if (!result.includes(child)) result.push(child);
        }
      }
      return result;
    }
    if (current.kind === 'filtered') {
      return allIn(current.base, root).filter(el => {
        if (current.has != null && allIn(current.has, el).length === 0) return false;
        if (current.has_not != null && allIn(current.has_not, el).length > 0) return false;
        if (current.has_text != null && !includesText(subtreeText(el), current.has_text, false)) return false;
        if (
          current.has_not_text != null &&
          !isEmptyStringTextMatcher(current.has_not_text) &&
          includesText(subtreeText(el), current.has_not_text, false)
        ) return false;
        if (current.visible != null && visible(el) !== current.visible) return false;
        return true;
      });
    }
    if (current.kind === 'and') {
      const right = allIn(current.right, root);
      return allIn(current.left, root).filter(el => right.includes(el));
    }
    if (current.kind === 'or') {
      const result = [];
      for (const el of [...allIn(current.left, root), ...allIn(current.right, root)]) {
        if (!result.includes(el)) result.push(el);
      }
      return result;
    }
    throw new Error(`Unsupported locator kind: ${current.kind}`);
  };
  const frameElementsFor = (current, root) => {
    const isFrameElement = el => el && (el.tagName === 'IFRAME' || el.tagName === 'FRAME');
    if (current.frame_selector_spec != null) {
      return allIn(current.frame_selector_spec, root).filter(isFrameElement);
    }
    const selector = current.frame_selector != null ? String(current.frame_selector) : 'iframe,frame';
    return queryAllDeep(root, selector).filter(isFrameElement);
  };
  const findStrictFrameViolation = (current, root) => {
    if (!current || typeof current !== 'object') return null;
    if (current.kind === 'frame') {
      const selector = current.frame_selector != null ? String(current.frame_selector) : 'iframe,frame';
      const frames = frameElementsFor(current, root);
      if (current.frame_strict && frames.length > 1) return { selector, count: frames.length };
      const frameIndex = Number.isInteger(current.frame_index) ? current.frame_index : 0;
      const frame = frames[frameIndex] || null;
      if (!frame) return null;
      let frameDocument = null;
      try {
        frameDocument = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
      } catch (error) {
        frameDocument = null;
      }
      if (!frameDocument) return null;
      return findStrictFrameViolation(current.inner || { kind: 'css', selector: '*' }, frameDocument);
    }
    for (const key of ['base', 'inner', 'has', 'has_not', 'left', 'right', 'selector']) {
      if (current[key] && typeof current[key] === 'object') {
        const violation = findStrictFrameViolation(current[key], root);
        if (violation) return violation;
      }
    }
    return null;
  };
  const strictFrameViolation = findStrictFrameViolation(spec, document);
  const matches = all(spec);
  const el = matches[index] || null;
  __BODY__
})()
"#;
    template
        .replace("__SPEC__", locator_json)
        .replace("__INDEX__", &index.to_string())
        .replace("__BODY__", body)
}

#[cfg(feature = "python")]
#[pymodule]
fn _rustwright(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    PYTHON_SETTLEMENTS_ENABLED.store(true, Ordering::SeqCst);
    let shutdown_gate = Py::new(py, PyRustShutdownGate)?;
    PyModule::import(py, "atexit")?.call_method1("register", (shutdown_gate,))?;
    module.add_class::<PyRustFutureAbort>()?;
    module.add_class::<PyRustFutureSettler>()?;
    module.add_class::<PyRustShutdownGate>()?;
    module.add_class::<PyBrowser>()?;
    module.add_class::<PyBrowserContext>()?;
    module.add_class::<PyPage>()?;
    module.add_class::<PyCdpSession>()?;
    module.add_class::<PyCdpEventWaiter>()?;
    module.add_class::<PyWorker>()?;
    module.add_class::<PyNetworkEventWaiter>()?;
    module.add_class::<PyPageEventStream>()?;
    module.add_class::<PyRouteEventWaiter>()?;
    module.add_class::<PyAuthEventWaiter>()?;
    module.add_class::<PyDialogEventWaiter>()?;
    module.add_class::<PyConsoleEventWaiter>()?;
    module.add_class::<PyWebSocketEventWaiter>()?;
    module.add_class::<PyBindingEventWaiter>()?;
    module.add_class::<PyDownloadEventWaiter>()?;
    module.add_class::<PyFileChooserEventWaiter>()?;
    module.add_class::<PyPopupEventWaiter>()?;
    module.add_class::<PyWorkerEventWaiter>()?;
    module.add_class::<PyWorkerCloseEventWaiter>()?;
    module.add_class::<PyServiceWorkerEventWaiter>()?;
    module.add_class::<PyBackgroundPageEventWaiter>()?;
    module.add_function(wrap_pyfunction!(launch_chromium, module)?)?;
    module.add_function(wrap_pyfunction!(launch_chromium_async, module)?)?;
    module.add_function(wrap_pyfunction!(connect_over_cdp, module)?)?;
    module.add_function(wrap_pyfunction!(chromium_executable_path, module)?)?;
    module.add(
        "_LOCATOR_TARGET_STATE_TEMPLATE",
        LOCATOR_TARGET_STATE_TEMPLATE,
    )?;
    module.add("_LOCATOR_FILL_TEMPLATE", LOCATOR_FILL_TEMPLATE)?;
    Ok(())
}
