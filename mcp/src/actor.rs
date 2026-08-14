use std::{
    cell::{Cell, RefCell},
    collections::{HashMap, HashSet, VecDeque},
    env, fmt, fs,
    io::Write as _,
    path::{Path, PathBuf},
    sync::{
        Arc, Condvar, Mutex, Weak,
        atomic::{AtomicU8, Ordering},
        mpsc::{TryRecvError, sync_channel},
    },
    thread,
    time::{Duration, Instant},
};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use rmcp::model::RequestId;
use rustwright::{
    ActionOptions, Browser, CancelToken, CloseOptions, ConnectOptions, ConsoleRecord,
    ConsoleRecords, Dialog, DialogKind, Error, EventReceiver, FileChooser, GotoOptions,
    LaunchOptions, NavigationDetail, NavigationDetailReceiver, NavigationObservation, NetworkBody,
    NetworkRecord, NetworkRecords, Page, PageEvent, ScreenshotOptions, TargetLifecycleEvent,
    TargetLifecycleReceiver, chromium,
};
use serde_json::{Value, json};
use tokio::sync::oneshot;

use crate::{
    config::FeatureConfig,
    shaping::{
        FindStructure, ModalRecovery, NetworkSection, NetworkStructure, ResponseShape,
        SnapshotStructure, TabEntry, TabsStructure,
    },
    tools::{ConsoleLevel, NetworkPart},
};

const SNAPSHOT_JS: &str = include_str!("snapshot.js");
const SNAPSHOT_LEGACY_JS: &str = include_str!("snapshot_legacy.js");

fn selected_snapshot_script(distill: bool) -> &'static str {
    if distill {
        SNAPSHOT_JS
    } else {
        SNAPSHOT_LEGACY_JS
    }
}
const BEGIN_SENSITIVE_SNAPSHOT_TRACKING_JS: &str = r#"(input) => {
  const trackingKey = Symbol.for('rustwright.mcp.sensitiveSnapshot');
  const tracking = globalThis[trackingKey] || {};
  if (!(tracking.sensitiveNodes instanceof WeakSet)) {
    tracking.sensitiveNodes = new WeakSet();
    tracking.sensitiveNodeRefs = new Set();
  }
  if (!(tracking.sensitiveNodeRefs instanceof Set)) tracking.sensitiveNodeRefs = new Set();
  if (tracking.pending) tracking.pending.stop();
  delete tracking.pending;
  globalThis[trackingKey] = tracking;

  const target = document.querySelector(input.selector);
  const isPassword = target
    && target.tagName === 'INPUT'
    && String(target.getAttribute('type') || 'text').toLowerCase() === 'password';
  if (!isPassword) return false;

  const touched = new Set();
  const valueBaseline = new Map();
  const visibilityBaseline = new Map();
  const isSnapshotVisible = (node) => {
    for (let current = node; current; current = current.parentElement) {
      const style = getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      if (current.getAttribute('aria-hidden') === 'true') return false;
      const rect = current.getBoundingClientRect();
      if (rect.width <= 0 && rect.height <= 0 && current.tagName !== 'OPTION') return false;
    }
    return true;
  };
  for (const node of document.querySelectorAll('input, textarea')) {
    valueBaseline.set(node, String(node.value || ''));
  }
  for (const node of document.querySelectorAll('*')) {
    visibilityBaseline.set(node, isSnapshotVisible(node));
  }
  const mark = (node) => {
    const element = node && node.nodeType === Node.ELEMENT_NODE
      ? node
      : node && node.parentElement;
    if (!element) return;
    touched.add(element);
    const liveRegion = element.closest('[role="status"],[role="alert"],[aria-live]');
    if (liveRegion) touched.add(liveRegion);
  };
  const markTree = (node) => {
    mark(node);
    if (node && node.querySelectorAll) {
      for (const descendant of node.querySelectorAll('*')) mark(descendant);
    }
  };
  const record = (mutations) => {
    for (const mutation of mutations) {
      mark(mutation.target);
      if (mutation.type === 'childList') {
        for (const node of mutation.addedNodes) markTree(node);
      }
    }
  };
  let observer;
  let expiry;
  const stop = () => {
    observer.disconnect();
    clearTimeout(expiry);
  };
  observer = new MutationObserver(record);
  observer.observe(document, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: [
      'aria-label', 'aria-labelledby', 'alt', 'title', 'placeholder', 'value', 'name', 'for',
      'href', 'src', 'type', 'role', 'id', 'aria-hidden', 'hidden', 'class', 'style',
    ],
  });
  const pending = {
    observer,
    record,
    touched,
    valueBaseline,
    visibilityBaseline,
    isSnapshotVisible,
    target,
    stop,
  };
  tracking.pending = pending;
  // A failed or timed-out caller normally discards this observer explicitly.
  // Bound its lifetime anyway so an evaluation failure cannot retain touched
  // elements indefinitely if cleanup cannot reach this document. The caller
  // supplies its remaining request deadline plus a bounded cleanup grace.
  expiry = setTimeout(() => {
    if (tracking.pending !== pending) return;
    stop();
    touched.clear();
    valueBaseline.clear();
    visibilityBaseline.clear();
    delete tracking.pending;
  }, Math.max(1_000, Number(input.expiryMs) || 1_000));
  return true;
}"#;
const RESOLVE_SENSITIVE_SNAPSHOT_TRACKING_JS: &str = r#"(input) => {
  const trackingKey = Symbol.for('rustwright.mcp.sensitiveSnapshot');
  const tracking = globalThis[trackingKey];
  const pending = tracking && tracking.pending;
  if (!pending) return false;

  pending.observer.disconnect();
  pending.record(pending.observer.takeRecords());
  pending.stop();
  for (const node of document.querySelectorAll('*')) {
    if (pending.visibilityBaseline.has(node)
        && pending.visibilityBaseline.get(node) !== pending.isSnapshotVisible(node)) {
      pending.touched.add(node);
    }
  }
  const ROLE_BY_TAG = {
    A: 'link', BUTTON: 'button', SELECT: 'combobox', TEXTAREA: 'textbox',
    H1: 'heading', H2: 'heading', H3: 'heading', H4: 'heading', H5: 'heading',
    H6: 'heading', IMG: 'img', NAV: 'navigation', MAIN: 'main', HEADER: 'banner',
    FOOTER: 'contentinfo', FORM: 'form', TABLE: 'table', UL: 'list', OL: 'list',
    LI: 'listitem', DIALOG: 'dialog', SUMMARY: 'button', LABEL: 'label',
    OPTION: 'option', ARTICLE: 'article', SECTION: 'region', ASIDE: 'complementary',
  };
  const INPUT_ROLES = {
    button: 'button', submit: 'button', reset: 'button', checkbox: 'checkbox',
    radio: 'radio', range: 'slider', search: 'searchbox',
  };
  const roleOf = (node) => {
    const explicit = node.getAttribute('role');
    if (explicit) return explicit;
    if (node.tagName === 'INPUT') {
      const type = String(node.getAttribute('type') || 'text').toLowerCase();
      return INPUT_ROLES[type] || 'textbox';
    }
    return ROLE_BY_TAG[node.tagName] || null;
  };
  const nameOf = (node) => {
    const labelled = node.getAttribute('aria-labelledby');
    if (labelled) {
      const parts = labelled.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((labelledNode) => labelledNode.textContent.trim());
      if (parts.length) return parts.join(' ');
    }
    const ariaLabel = node.getAttribute('aria-label');
    if (ariaLabel) return ariaLabel;
    if (node.labels && node.labels.length) return node.labels[0].textContent.trim();
    const direct = node.getAttribute('alt') || node.getAttribute('title')
      || node.getAttribute('placeholder');
    if (direct) return direct;
    if (node.tagName === 'INPUT' || node.tagName === 'SELECT' || node.tagName === 'TEXTAREA') {
      return node.getAttribute('name') || '';
    }
    return String(node.textContent || '').trim().replace(/\s+/g, ' ');
  };
  const labelledbyConsumers = Array.from(document.querySelectorAll('[aria-labelledby]'));
  const labelledbyIds = new Set(
    labelledbyConsumers.flatMap(
      (consumer) => String(consumer.getAttribute('aria-labelledby') || '')
        .split(/\s+/)
        .filter(Boolean),
    ),
  );
  const targetBaseline = pending.valueBaseline.get(pending.target);
  const targetValue = String(pending.target.value || '');
  const writeStatus = targetValue.includes(input.value)
    ? 'complete'
    : targetBaseline !== undefined && targetValue !== targetBaseline
      ? 'partial'
      : 'unchanged';
  const sensitiveValues = input.value.length > 0 ? [input.value] : [];
  if (writeStatus === 'partial' && targetValue.length > 0) {
    sensitiveValues.push(targetValue);
  }
  const containsSensitiveValue = (node) => {
    const tag = node.tagName;
    const role = roleOf(node);
    const isPassword = node.tagName === 'INPUT'
      && String(node.getAttribute('type') || 'text').toLowerCase() === 'password';
    const values = [];
    if (tag === 'IFRAME' || tag === 'FRAME') {
      values.push(node.getAttribute('title') || node.getAttribute('name')
        || node.getAttribute('src') || '');
    } else if (role) {
      values.push(role, nameOf(node));
      if (tag === 'A') values.push(node.getAttribute('href'));
      if ((tag === 'INPUT' || tag === 'TEXTAREA') && !isPassword) {
        values.push(String(node.value || ''));
      }
    } else if (node.children.length === 0) {
      values.push(String(node.textContent || '').trim().replace(/\s+/g, ' '));
    }
    if (node.id && labelledbyIds.has(node.id)) {
      values.push(String(node.textContent || '').trim().replace(/\s+/g, ' '));
    }
    return values.some((value) => sensitiveValues.some(
      (sensitiveValue) => String(value || '').includes(sensitiveValue),
    ));
  };
  const taint = (node) => {
    if (tracking.sensitiveNodes.has(node)) return;
    tracking.sensitiveNodes.add(node);
    tracking.sensitiveNodeRefs.add(new WeakRef(node));
  };
  try {
    // This deliberately catches only exact requested or actually-landed echoes
    // on renderer-visible candidates affected during the password write.
    // Transformed or encoded echoes are not detectable without over-redacting
    // legitimate content.
    if (sensitiveValues.length > 0) {
      const candidates = new Set(pending.touched);
      for (const node of pending.touched) {
        for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
          if (roleOf(ancestor)
              || ancestor.tagName === 'IFRAME'
              || ancestor.tagName === 'FRAME') {
            candidates.add(ancestor);
          }
        }
      }
      const touchedNodes = Array.from(pending.touched);
      for (const consumer of labelledbyConsumers) {
        const referencesTouchedNode = String(consumer.getAttribute('aria-labelledby') || '')
          .split(/\s+/)
          .filter(Boolean)
          .map((id) => document.getElementById(id))
          .filter(Boolean)
          .some((labelledNode) => touchedNodes.some(
            (touchedNode) => labelledNode === touchedNode || labelledNode.contains(touchedNode),
          ));
        if (referencesTouchedNode) candidates.add(consumer);
      }
      for (const node of candidates) {
        if (containsSensitiveValue(node)) taint(node);
      }
      const valueCandidates = new Set([
        ...pending.valueBaseline.keys(),
        ...document.querySelectorAll('input, textarea'),
      ]);
      for (const node of valueCandidates) {
        // A password field's own value is never rendered, so tainting it buys
        // nothing and costs its accessible name -- which is the caller's only
        // handle on the field. A password field whose *name* echoes the secret
        // is still caught above by containsSensitiveValue.
        if (node.tagName === 'INPUT'
            && String(node.getAttribute('type') || 'text').toLowerCase() === 'password') {
          continue;
        }
        const baseline = pending.valueBaseline.get(node);
        const liveValue = String(node.value || '');
        if (liveValue !== baseline
            && sensitiveValues.some((sensitiveValue) => liveValue.includes(sensitiveValue))) {
          taint(node);
        }
      }
    }
    return { resolved: true, writeStatus };
  } finally {
    pending.touched.clear();
    pending.valueBaseline.clear();
    pending.visibilityBaseline.clear();
    if (tracking.pending === pending) delete tracking.pending;
  }
  // Do not keep observing: without retaining input.value, later mutations
  // cannot be re-classified. The resolved node set persists for this document.
  // Known limitations: cross-document navigation destroys this document-scoped
  // state, and main-world page JavaScript can tamper with the Symbol.for global.
}"#;
const DISCARD_SENSITIVE_SNAPSHOT_TRACKING_JS: &str = r#"() => {
  const trackingKey = Symbol.for('rustwright.mcp.sensitiveSnapshot');
  const tracking = globalThis[trackingKey];
  const pending = tracking && tracking.pending;
  if (!pending) return;
  pending.stop();
  pending.touched.clear();
  pending.valueBaseline.clear();
  pending.visibilityBaseline.clear();
  if (tracking.pending === pending) delete tracking.pending;
}"#;
const FIND_REGEX_JS: &str = r#"(input) => {
  const expression = new RegExp(input.pattern, input.flags);
  return input.lines
    .map((line, index) => expression.test(line) ? index : null)
    .filter((index) => index !== null);
}"#;
const WAIT_FOR_JS: &str = r#"async (options) => {
  if (options.delayMs > 0) {
    await new Promise((resolve) => setTimeout(resolve, options.delayMs));
  }
  if (options.text === null && options.textGone === null) return true;
  const deadline = Date.now() + options.timeoutMs;
  while (true) {
    const visibleText = document.body ? (document.body.innerText || '') : '';
    const textReady = options.text === null || visibleText.includes(options.text);
    const goneReady = options.textGone === null || !visibleText.includes(options.textGone);
    if (textReady && goneReady) return true;
    if (Date.now() >= deadline) {
      throw new Error('browser_wait_for timed out waiting for text state');
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}"#;
const ELEMENT_EVALUATE_JS: &str = r#"async (input) => {
  const target = document.querySelector(input.selector);
  if (!target) throw new Error(`target not found: ${input.selector}`);
  const callable = (0, eval)(`(${input.function})`);
  return await callable(target);
}"#;
const SYNTHETIC_DROP_JS: &str = r#"async (input) => {
  const target = document.querySelector(input.selector);
  if (!target) throw new Error(`target not found: ${input.selector}`);
  const transfer = new DataTransfer();
  transfer.effectAllowed = 'copy';
  transfer.dropEffect = 'copy';
  for (const entry of input.files) {
    const binary = atob(entry.base64);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    transfer.items.add(new File([bytes], entry.name, {
      type: entry.mime,
      lastModified: 0,
    }));
  }
  for (const [mime, value] of input.data) transfer.setData(mime, value);
  const bounds = target.getBoundingClientRect();
  const eventOptions = {
    bubbles: true,
    cancelable: true,
    composed: true,
    clientX: bounds.left + bounds.width / 2,
    clientY: bounds.top + bounds.height / 2,
    dataTransfer: transfer,
  };
  for (const type of ['dragenter', 'dragover', 'drop']) {
    target.dispatchEvent(new DragEvent(type, eventOptions));
  }
  await new Promise((resolve) => setTimeout(resolve, 0));
}"#;
const REMOTE_UNREACHABLE: &str = "remote CDP session unreachable — restart or reconfigure";
const DEFAULT_CDP_TIMEOUT_MS: u64 = 60_000;
const DEFAULT_TOOL_TIMEOUT_MS: u64 = 60_000;
const MIN_TOOL_TIMEOUT_MS: u64 = 1_000;
const MAX_TOOL_TIMEOUT_MS: u64 = 600_000;
const SENSITIVE_TRACKING_CLEANUP_GRACE_MS: u64 = 5_000;
/// What a masked secret renders as, for every site that masks one.
///
/// The snapshot renderer is JavaScript and cannot read this constant, so it is
/// passed in as a snapshot option rather than spelled a second time there. The
/// two sites mask different things -- a password field's value, and a secret
/// echoed inside a pending dialog's text -- but a caller cannot tell them apart
/// in the output, so they must not drift.
const SECRET_MASK: &str = "••••••";
const ENGINE_TIMEOUT_CUSHION: Duration = Duration::from_secs(1);
const MAX_FILE_INPUTS: usize = 50;
const MAX_FILE_INPUT_BYTES: u64 = 20 * 1024 * 1024;
pub(crate) const COMMAND_QUEUE_CAPACITY: usize = 64;

#[derive(Debug)]
pub(crate) enum BrowserOp {
    Navigate(String),
    NavigateBack,
    NavigateForward,
    Reload,
    Resize {
        width: u32,
        height: u32,
    },
    Snapshot {
        target: Option<String>,
        depth: Option<u32>,
        boxes: bool,
    },
    Find {
        text: Option<String>,
        regex: Option<RegexSpec>,
    },
    Click {
        target: String,
        double_click: bool,
    },
    ScrollTarget(String),
    ScrollViewport(f64),
    Type {
        target: String,
        text: String,
        submit: bool,
        slowly: bool,
        clear: bool,
    },
    SelectOption {
        target: String,
        values: Vec<String>,
    },
    FillForm(Vec<FillField>),
    Hover(String),
    PressKey {
        target: Option<String>,
        key: String,
    },
    Drag {
        start_target: String,
        end_target: String,
        start_element: Option<String>,
        end_element: Option<String>,
    },
    Drop {
        target: String,
        paths: Vec<String>,
        data: Vec<(String, String)>,
    },
    ConsoleMessages {
        level: ConsoleLevel,
        all: bool,
        filename: Option<String>,
    },
    NetworkRequests {
        include_static: bool,
        filter: Option<String>,
        filename: Option<String>,
    },
    NetworkRequest {
        index: u64,
        part: Option<NetworkPart>,
        filename: Option<String>,
    },
    Tabs {
        action: TabAction,
        index: Option<usize>,
        url: Option<String>,
    },
    HandleDialog {
        accept: bool,
        prompt_text: Option<String>,
    },
    FileUpload(Vec<String>),
    WaitFor {
        time_seconds: Option<f64>,
        text: Option<String>,
        text_gone: Option<String>,
        timeout_ms: f64,
    },
    GetText {
        selector: String,
        max_chars: usize,
    },
    Evaluate {
        function: String,
        target: Option<String>,
    },
    TakeScreenshot {
        full_page: bool,
        image_type: ScreenshotType,
    },
    Close,
}

impl BrowserOp {
    /// Explicit file acknowledgements and screenshot-to-file fallbacks are legacy
    /// output contracts, not observation payloads. Capture this provenance before
    /// the operation is moved to the actor thread.
    pub(crate) fn bypass_response_shaping(&self) -> bool {
        matches!(
            self,
            Self::ConsoleMessages {
                filename: Some(_),
                ..
            } | Self::NetworkRequests {
                filename: Some(_),
                ..
            } | Self::NetworkRequest {
                filename: Some(_),
                ..
            } | Self::TakeScreenshot { .. }
        )
    }
}

#[derive(Debug)]
pub(crate) struct RegexSpec {
    pub(crate) pattern: String,
    pub(crate) flags: String,
}

#[derive(Debug)]
pub(crate) struct FillField {
    pub(crate) target: String,
    pub(crate) name: String,
    pub(crate) kind: FillFieldKind,
    pub(crate) value: String,
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum FillFieldKind {
    Textbox,
    Checkbox,
    Radio,
    Combobox,
    Slider,
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum TabAction {
    List,
    New,
    Close,
    Select,
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum ScreenshotType {
    Png,
    Jpeg,
}

impl ScreenshotType {
    pub(crate) fn mime(self) -> &'static str {
        match self {
            Self::Png => "image/png",
            Self::Jpeg => "image/jpeg",
        }
    }

    fn engine_name(self) -> &'static str {
        match self {
            Self::Png => "png",
            Self::Jpeg => "jpeg",
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum BrowserOutput {
    Text(String),
    ShapedText {
        text: String,
        shape: ResponseShape,
    },
    Image {
        bytes: Vec<u8>,
        mime: &'static str,
        extension: &'static str,
    },
}

impl From<String> for BrowserOutput {
    fn from(text: String) -> Self {
        Self::Text(text)
    }
}

impl PartialEq<String> for BrowserOutput {
    fn eq(&self, other: &String) -> bool {
        matches!(self, Self::Text(text) if text == other)
            || matches!(self, Self::ShapedText { text, .. } if text == other)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum BrowserError {
    Busy,
    Cancelled,
    Timeout(u64),
    Stopped,
    Message(String),
}

impl fmt::Display for BrowserError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Busy => write!(
                formatter,
                "browser actor is busy (queue capacity {COMMAND_QUEUE_CAPACITY})"
            ),
            Self::Cancelled => formatter.write_str("browser command cancelled"),
            Self::Timeout(timeout_ms) => {
                write!(formatter, "browser command timed out after {timeout_ms} ms")
            }
            Self::Stopped => formatter.write_str("browser actor stopped"),
            Self::Message(message) => formatter.write_str(message),
        }
    }
}

pub(crate) type BrowserResult = Result<BrowserOutput, BrowserError>;
type TextResult = Result<String, BrowserError>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SensitiveWriteProgress {
    Unchanged,
    Partial,
    Complete,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
enum CancellationReason {
    Active = 0,
    Cancelled = 1,
    Deadline = 2,
}

impl CancellationReason {
    fn from_u8(value: u8) -> Self {
        match value {
            1 => Self::Cancelled,
            2 => Self::Deadline,
            _ => Self::Active,
        }
    }

    fn error(self, timeout_ms: u64) -> Option<BrowserError> {
        match self {
            Self::Active => None,
            Self::Cancelled => Some(BrowserError::Cancelled),
            Self::Deadline => Some(BrowserError::Timeout(timeout_ms)),
        }
    }
}

struct CommandCancellation {
    reason: AtomicU8,
    engine: CancelToken,
    detail: Mutex<Option<String>>,
}

impl CommandCancellation {
    fn new() -> Self {
        Self {
            reason: AtomicU8::new(CancellationReason::Active as u8),
            engine: CancelToken::new(),
            detail: Mutex::new(None),
        }
    }

    fn cancel(&self, reason: CancellationReason) -> bool {
        if self
            .reason
            .compare_exchange(
                CancellationReason::Active as u8,
                reason as u8,
                Ordering::SeqCst,
                Ordering::SeqCst,
            )
            .is_err()
        {
            return false;
        }
        self.engine.try_cancel()
    }

    fn reason(&self) -> CancellationReason {
        CancellationReason::from_u8(self.reason.load(Ordering::SeqCst))
    }

    fn is_committed(&self) -> bool {
        self.engine.is_physical_action_committed()
    }

    fn set_detail(&self, detail: String) {
        *self.detail.lock().unwrap() = Some(detail);
    }

    fn detail(&self) -> Option<String> {
        self.detail.lock().unwrap().clone()
    }
}

struct ActorRequest {
    request_id: RequestId,
    op: BrowserOp,
    cancellation: Arc<CommandCancellation>,
    deadline: Instant,
    timeout_ms: u64,
    reply: oneshot::Sender<BrowserResult>,
}

struct InFlight {
    request_id: RequestId,
    cancellation: Arc<CommandCancellation>,
}

struct ActorQueue {
    queued: VecDeque<ActorRequest>,
    in_flight: Option<InFlight>,
    closed: bool,
}

struct ActorShared {
    queue: Mutex<ActorQueue>,
    ready: Condvar,
}

impl ActorShared {
    fn new() -> Self {
        Self {
            queue: Mutex::new(ActorQueue {
                queued: VecDeque::with_capacity(COMMAND_QUEUE_CAPACITY),
                in_flight: None,
                closed: false,
            }),
            ready: Condvar::new(),
        }
    }

    fn submit(&self, request: ActorRequest) -> Result<(), BrowserError> {
        let mut queue = self.queue.lock().unwrap();
        if queue.closed {
            return Err(BrowserError::Stopped);
        }
        if queue.queued.len() >= COMMAND_QUEUE_CAPACITY {
            return Err(BrowserError::Busy);
        }
        queue.queued.push_back(request);
        self.ready.notify_one();
        Ok(())
    }

    fn next(&self) -> Option<ActorRequest> {
        let mut queue = self.queue.lock().unwrap();
        loop {
            if let Some(request) = queue.queued.pop_front() {
                queue.in_flight = Some(InFlight {
                    request_id: request.request_id.clone(),
                    cancellation: Arc::clone(&request.cancellation),
                });
                return Some(request);
            }
            if queue.closed {
                return None;
            }
            queue = self.ready.wait(queue).unwrap();
        }
    }

    fn complete<T>(&self, request: &ActorRequest, result: Result<T, BrowserError>) -> BrowserResult
    where
        T: Into<BrowserOutput>,
    {
        let result = result.map(Into::into);
        let mut queue = self.queue.lock().unwrap();
        if queue
            .in_flight
            .as_ref()
            .is_some_and(|in_flight| in_flight.request_id == request.request_id)
        {
            queue.in_flight.take();
        }
        // Successful text-write tools own their truthful complete/partial
        // result without claiming request-level physical commitment. Their
        // dispatched paths remain cancellable, but return `Ok` after checking
        // live write progress and resolving password taint.
        if request.cancellation.is_committed()
            || (result.is_ok()
                && matches!(&request.op, BrowserOp::Type { .. } | BrowserOp::FillForm(_)))
        {
            result
        } else {
            match request.cancellation.reason().error(request.timeout_ms) {
                Some(error) => request
                    .cancellation
                    .detail()
                    .map_or(Err(error), |detail| Err(BrowserError::Message(detail))),
                None => result,
            }
        }
    }

    fn cancel(&self, request_id: &RequestId, reason: CancellationReason) -> bool {
        let queued = {
            let mut queue = self.queue.lock().unwrap();
            if let Some(index) = queue
                .queued
                .iter()
                .position(|request| &request.request_id == request_id)
            {
                queue.queued.remove(index)
            } else {
                if let Some(in_flight) = queue
                    .in_flight
                    .as_ref()
                    .filter(|in_flight| &in_flight.request_id == request_id)
                {
                    return in_flight.cancellation.cancel(reason);
                }
                return false;
            }
        };
        if let Some(request) = queued {
            let _ = request.cancellation.cancel(reason);
            let error = request
                .cancellation
                .reason()
                .error(request.timeout_ms)
                .unwrap_or(BrowserError::Cancelled);
            let _ = request.reply.send(Err(error));
            true
        } else {
            false
        }
    }

    fn shutdown(&self) {
        let queued = {
            let mut queue = self.queue.lock().unwrap();
            queue.closed = true;
            if let Some(in_flight) = &queue.in_flight {
                let _ = in_flight.cancellation.cancel(CancellationReason::Cancelled);
            }
            self.ready.notify_all();
            queue.queued.drain(..).collect::<Vec<_>>()
        };
        for request in queued {
            let _ = request.reply.send(Err(BrowserError::Stopped));
        }
    }

    #[cfg(test)]
    fn queued_len(&self) -> usize {
        self.queue.lock().unwrap().queued.len()
    }
}

struct ExecuteGuard {
    shared: Weak<ActorShared>,
    request_id: RequestId,
    armed: bool,
}

impl ExecuteGuard {
    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for ExecuteGuard {
    fn drop(&mut self) {
        if self.armed
            && let Some(shared) = self.shared.upgrade()
        {
            shared.cancel(&self.request_id, CancellationReason::Cancelled);
        }
    }
}

pub(crate) struct BrowserActor {
    shared: Arc<ActorShared>,
    default_timeout: Duration,
    thread: Mutex<Option<thread::JoinHandle<()>>>,
}

impl BrowserActor {
    #[cfg(test)]
    pub(crate) fn spawn() -> Self {
        Self::spawn_with_features(FeatureConfig::default())
    }

    pub(crate) fn spawn_with_features(features: FeatureConfig) -> Self {
        Self::spawn_with_startup_and_features(BrowserStartup::from_env(), features)
    }

    fn spawn_with_startup(startup: BrowserStartup) -> Self {
        Self::spawn_with_startup_and_features(startup, FeatureConfig::default())
    }

    fn spawn_with_startup_and_features(startup: BrowserStartup, features: FeatureConfig) -> Self {
        let shared = Arc::new(ActorShared::new());
        let actor_shared = Arc::clone(&shared);
        let thread = thread::Builder::new()
            .name("mcp-browser-actor".to_owned())
            .spawn(move || actor_main(actor_shared, startup, features))
            .expect("failed to spawn browser actor");
        Self {
            shared,
            default_timeout: tool_timeout_from_env(),
            thread: Mutex::new(Some(thread)),
        }
    }

    pub(crate) async fn execute(&self, request_id: RequestId, op: BrowserOp) -> BrowserResult {
        self.execute_with_timeout(request_id, op, self.default_timeout)
            .await
    }

    async fn execute_with_timeout(
        &self,
        request_id: RequestId,
        op: BrowserOp,
        timeout: Duration,
    ) -> BrowserResult {
        let timeout_ms = duration_millis(timeout);
        let deadline = Instant::now() + timeout;
        let cancellation = Arc::new(CommandCancellation::new());
        let (reply, response) = oneshot::channel();
        self.shared.submit(ActorRequest {
            request_id: request_id.clone(),
            op,
            cancellation,
            deadline,
            timeout_ms,
            reply,
        })?;

        let mut guard = ExecuteGuard {
            shared: Arc::downgrade(&self.shared),
            request_id: request_id.clone(),
            armed: true,
        };
        let sleep = tokio::time::sleep_until(tokio::time::Instant::from_std(deadline));
        tokio::pin!(sleep);
        tokio::pin!(response);
        let result = tokio::select! {
            biased;
            response = &mut response => response.map_err(|_| BrowserError::Stopped)?,
            () = &mut sleep => {
                self.shared.cancel(&request_id, CancellationReason::Deadline);
                response.await.map_err(|_| BrowserError::Stopped)?
            }
        };
        guard.disarm();
        result
    }

    pub(crate) fn cancel(&self, request_id: &RequestId) -> bool {
        self.shared
            .cancel(request_id, CancellationReason::Cancelled)
    }
}

fn duration_millis(duration: Duration) -> u64 {
    duration.as_millis().min(u128::from(u64::MAX)) as u64
}

fn sensitive_tracking_expiry_ms(remaining: Duration) -> u64 {
    duration_millis(remaining).saturating_add(SENSITIVE_TRACKING_CLEANUP_GRACE_MS)
}

fn tool_timeout_from_env() -> Duration {
    tool_timeout_from_value(env::var("RUSTWRIGHT_MCP_TOOL_TIMEOUT_MS").ok().as_deref())
}

fn tool_timeout_from_value(value: Option<&str>) -> Duration {
    let timeout_ms = value
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(DEFAULT_TOOL_TIMEOUT_MS)
        .clamp(MIN_TOOL_TIMEOUT_MS, MAX_TOOL_TIMEOUT_MS);
    Duration::from_millis(timeout_ms)
}

enum BrowserStartup {
    Local,
    Remote(ConnectOptions),
    InvalidRemote,
}

impl BrowserStartup {
    fn from_env() -> Self {
        let Ok(endpoint) = env::var("RUSTWRIGHT_MCP_CDP_ENDPOINT") else {
            return Self::Local;
        };
        if endpoint.trim().is_empty() {
            return Self::Local;
        }
        let timeout_ms = match env::var("RUSTWRIGHT_MCP_CDP_TIMEOUT_MS") {
            Ok(value) => match value.parse::<u64>() {
                Ok(value) if value > 0 => value,
                _ => return Self::InvalidRemote,
            },
            Err(env::VarError::NotPresent) => DEFAULT_CDP_TIMEOUT_MS,
            Err(env::VarError::NotUnicode(_)) => return Self::InvalidRemote,
        };
        let headers = match env::var("RUSTWRIGHT_MCP_CDP_HEADERS") {
            Ok(value) => match decode_headers(&value) {
                Some(headers) => headers,
                None => return Self::InvalidRemote,
            },
            Err(env::VarError::NotPresent) => Vec::new(),
            Err(env::VarError::NotUnicode(_)) => return Self::InvalidRemote,
        };
        Self::Remote(ConnectOptions {
            endpoint,
            headers,
            timeout: Duration::from_millis(timeout_ms),
        })
    }
}

fn decode_headers(value: &str) -> Option<Vec<(String, String)>> {
    let object = serde_json::from_str::<Value>(value).ok()?;
    object.as_object().and_then(|object| {
        object
            .iter()
            .map(|(name, value)| Some((name.clone(), value.as_str()?.to_owned())))
            .collect()
    })
}

impl Drop for BrowserActor {
    fn drop(&mut self) {
        self.shared.shutdown();
        if let Ok(slot) = self.thread.get_mut()
            && let Some(handle) = slot.take()
            && handle.join().is_err()
        {
            eprintln!("browser actor panicked during shutdown");
        }
    }
}

struct BrowserState {
    features: FeatureConfig,
    browser: Option<Browser>,
    page: Option<ActivePageHandle>,
    active_target_id: Option<String>,
    pages: HashMap<String, PageRuntime>,
    tab_order: Vec<String>,
    tab_inventory: HashMap<String, String>,
    target_lifecycle: Option<Box<dyn LifecycleReceiver>>,
    closing_targets: HashSet<String>,
    remote: bool,
    remote_options: Option<ConnectOptions>,
    startup_error: Option<&'static str>,
    next_ref: u64,
    current_refs: HashSet<String>,
    response_shape: Option<ResponseShape>,
    header_state: Option<BrowserHeaderState>,
    inventory_stale: bool,
    snapshot_evaluator: Option<SnapshotEvaluationSeam>,
    page_record_source: Option<Box<dyn PageRecordSource>>,
    lifecycle_subscription_provider: LifecycleSubscriptionProvider,
    browser_query_provider: Box<dyn BrowserQueryProvider>,
    page_lifecycle_seam: Option<Box<dyn PageLifecycleSeam>>,
}

type SnapshotEvaluationSeam = Box<dyn FnMut(&'static str, &Value) -> Result<Value, BrowserError>>;
type PageObservation = (Option<String>, Option<(usize, usize)>);
type LifecycleSubscriptionProvider =
    Box<dyn FnMut(Option<&Browser>) -> Option<Box<dyn LifecycleReceiver>>>;

trait PageRecordSource {
    fn console_records(
        &mut self,
        include_previous_navigations: bool,
        clear: bool,
    ) -> Result<ConsoleRecords, Error>;

    fn network_records(
        &mut self,
        include_previous_navigations: bool,
        clear: bool,
    ) -> NetworkRecords;
}

#[derive(Clone)]
struct ActivePageHandle {
    page: Option<Page>,
    target_id: String,
    url: String,
}

struct PageCandidate {
    registration: Box<dyn PageRegistration>,
    handle: ActivePageHandle,
}

trait PageLifecycleSeam {
    fn attach_remote(&mut self, request: &ActorRequest) -> Result<PageCandidate, BrowserError>;

    fn discover_pages(
        &mut self,
        request: &ActorRequest,
    ) -> Result<Vec<PageCandidate>, BrowserError>;

    fn close_page(
        &mut self,
        page: &ActivePageHandle,
        request: &ActorRequest,
    ) -> Result<(), BrowserError>;

    fn new_page(&mut self, request: &ActorRequest) -> Result<PageCandidate, BrowserError>;
}

impl ActivePageHandle {
    fn live(page: Page) -> Self {
        Self {
            target_id: page.target_id(),
            url: page.url(),
            page: Some(page),
        }
    }

    fn target_id(&self) -> String {
        self.page
            .as_ref()
            .map_or_else(|| self.target_id.clone(), Page::target_id)
    }

    fn url(&self) -> String {
        self.page
            .as_ref()
            .map_or_else(|| self.url.clone(), Page::url)
    }

    fn live_page(&self) -> Option<&Page> {
        self.page.as_ref()
    }
}

impl std::ops::Deref for ActivePageHandle {
    type Target = Page;

    fn deref(&self) -> &Self::Target {
        self.live_page()
            .expect("test active-page handles only support lifecycle processing")
    }
}

struct BrowserInventoryEntry {
    target_id: String,
    url: String,
    page: Option<Page>,
}

trait BrowserQueryProvider {
    fn inventory(
        &mut self,
        browser: Option<&Browser>,
        request: &ActorRequest,
    ) -> Result<Vec<BrowserInventoryEntry>, BrowserError>;

    fn active_page(&mut self, page: Option<&ActivePageHandle>) -> Option<(String, String)>;

    fn pending_modal(&mut self, pages: &HashMap<String, PageRuntime>, target_id: &str) -> bool;

    fn observe(&mut self, page: Option<&Page>, request: &ActorRequest) -> PageObservation;
}

struct LiveBrowserQueryProvider;

impl BrowserQueryProvider for LiveBrowserQueryProvider {
    fn inventory(
        &mut self,
        browser: Option<&Browser>,
        request: &ActorRequest,
    ) -> Result<Vec<BrowserInventoryEntry>, BrowserError> {
        let remaining = BrowserState::remaining(request)?;
        browser
            .ok_or_else(|| BrowserError::Message("browser is not initialized".to_owned()))?
            .pages_with_cancel(
                remaining.saturating_add(ENGINE_TIMEOUT_CUSHION),
                Some(&request.cancellation.engine),
            )
            .map_err(|error| {
                if matches!(error, Error::Cancelled) {
                    request
                        .cancellation
                        .reason()
                        .error(request.timeout_ms)
                        .unwrap_or(BrowserError::Cancelled)
                } else if matches!(error, Error::Timeout(_)) {
                    BrowserError::Timeout(request.timeout_ms)
                } else {
                    BrowserError::Message(format!("tab listing failed: {error}"))
                }
            })
            .map(|pages| {
                pages
                    .into_iter()
                    .map(|page| BrowserInventoryEntry {
                        target_id: page.target_id(),
                        url: page.url(),
                        page: Some(page),
                    })
                    .collect()
            })
    }

    fn active_page(&mut self, page: Option<&ActivePageHandle>) -> Option<(String, String)> {
        page.map(|page| (page.target_id(), page.url()))
    }

    fn pending_modal(&mut self, pages: &HashMap<String, PageRuntime>, target_id: &str) -> bool {
        pages.get(target_id).is_some_and(|runtime| {
            runtime.pending_dialog.is_some() || runtime.pending_file_chooser.is_some()
        })
    }

    fn observe(&mut self, page: Option<&Page>, request: &ActorRequest) -> PageObservation {
        live_page_observation(page, request)
    }
}

fn live_page_observation(page: Option<&Page>, request: &ActorRequest) -> PageObservation {
    let Some(page) = page else {
        return (None, None);
    };
    let title = BrowserState::remaining(request).ok().and_then(|remaining| {
        page.title(ActionOptions::timeout(BrowserState::engine_timeout(
            remaining,
        )))
        .ok()
    });
    let console_counts = page.console_records(false, false).ok().map(|records| {
        records
            .records
            .iter()
            .fold((0, 0), |(errors, warnings), record| {
                match record.message_type.as_str() {
                    "error" | "assert" => (errors + 1, warnings),
                    "warning" | "warn" => (errors, warnings + 1),
                    _ => (errors, warnings),
                }
            })
    });
    (title, console_counts)
}

impl Default for BrowserState {
    fn default() -> Self {
        Self {
            features: FeatureConfig::default(),
            browser: None,
            page: None,
            active_target_id: None,
            pages: HashMap::new(),
            tab_order: Vec::new(),
            tab_inventory: HashMap::new(),
            target_lifecycle: None,
            closing_targets: HashSet::new(),
            remote: false,
            remote_options: None,
            startup_error: None,
            next_ref: 0,
            current_refs: HashSet::new(),
            response_shape: None,
            header_state: None,
            inventory_stale: false,
            snapshot_evaluator: None,
            page_record_source: None,
            lifecycle_subscription_provider: Box::new(|browser| {
                browser.map(|browser| {
                    Box::new(browser.target_lifecycle()) as Box<dyn LifecycleReceiver>
                })
            }),
            browser_query_provider: Box::new(LiveBrowserQueryProvider),
            page_lifecycle_seam: None,
        }
    }
}

trait LifecycleReceiver {
    fn try_recv_lifecycle(&self) -> Result<Option<TargetLifecycleEvent>, ()>;
}

impl LifecycleReceiver for TargetLifecycleReceiver {
    fn try_recv_lifecycle(&self) -> Result<Option<TargetLifecycleEvent>, ()> {
        self.try_recv().map_err(|_| ())
    }
}

impl LifecycleReceiver for std::sync::mpsc::Receiver<TargetLifecycleEvent> {
    fn try_recv_lifecycle(&self) -> Result<Option<TargetLifecycleEvent>, ()> {
        match self.try_recv() {
            Ok(event) => Ok(Some(event)),
            Err(std::sync::mpsc::TryRecvError::Empty) => Ok(None),
            Err(std::sync::mpsc::TryRecvError::Disconnected) => Err(()),
        }
    }
}

trait DetailReceiver {
    fn dropped_count(&self) -> u64;
    fn latest_sequence(&self) -> u64;
    fn try_recv_detail(&self) -> Option<(u64, NavigationDetail)>;
}

impl DetailReceiver for NavigationDetailReceiver {
    fn dropped_count(&self) -> u64 {
        NavigationDetailReceiver::dropped_count(self)
    }

    fn latest_sequence(&self) -> u64 {
        NavigationDetailReceiver::latest_sequence(self)
    }

    fn try_recv_detail(&self) -> Option<(u64, NavigationDetail)> {
        self.recv_timeout_sequenced(Duration::ZERO)
    }
}

trait PageEventReceiver {
    fn try_recv_page_event(&self) -> Option<PageEvent>;
}

impl PageEventReceiver for EventReceiver {
    fn try_recv_page_event(&self) -> Option<PageEvent> {
        self.recv_timeout(Duration::ZERO)
    }
}

impl PageEventReceiver for std::sync::mpsc::Receiver<PageEvent> {
    fn try_recv_page_event(&self) -> Option<PageEvent> {
        self.try_recv().ok()
    }
}

struct PageRuntime {
    events: Option<Box<dyn PageEventReceiver>>,
    navigation_details: Option<Box<dyn DetailReceiver>>,
    detail_dropped_count: u64,
    pending_dialog: Option<PendingDialog>,
    pending_file_chooser: Option<PendingFileChooser>,
    title: Option<String>,
    header: Option<PageHeaderRuntime>,
}

struct NavigationDetailReceiverSeam {
    receiver: std::sync::mpsc::Receiver<(u64, NavigationDetail)>,
    latest_sequence: Arc<std::sync::atomic::AtomicU64>,
    dropped_count: Arc<std::sync::atomic::AtomicU64>,
}

impl DetailReceiver for NavigationDetailReceiverSeam {
    fn dropped_count(&self) -> u64 {
        self.dropped_count.load(Ordering::SeqCst)
    }

    fn latest_sequence(&self) -> u64 {
        self.latest_sequence.load(Ordering::SeqCst)
    }

    fn try_recv_detail(&self) -> Option<(u64, NavigationDetail)> {
        self.receiver.try_recv().ok()
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct PageHeader {
    url: String,
    title: Option<String>,
    status: Option<u16>,
    console_err: usize,
    console_warn: usize,
}

#[derive(Default)]
struct PageHeaderRuntime {
    current: PageHeader,
    last_rendered: Option<PageHeader>,
    last_observed_title: Option<String>,
    pending_observed_url: Option<String>,
    pending_observed_after_sequence: Option<u64>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct TabSignature {
    tabs: Vec<(String, String)>,
    active_id: Option<String>,
    stale: bool,
}

#[derive(Default)]
struct BrowserHeaderState {
    last_rendered_tab_signature: Option<TabSignature>,
}

fn apply_navigation_detail(
    runtime: &mut PageHeaderRuntime,
    sequence: u64,
    detail: NavigationDetail,
) {
    let after_boundary = runtime
        .pending_observed_after_sequence
        .is_some_and(|boundary| sequence > boundary);
    if runtime.pending_observed_after_sequence.is_some() && !after_boundary {
        return;
    }
    let initiated =
        after_boundary && runtime.pending_observed_url.as_deref() == Some(detail.url.as_str());
    if after_boundary {
        runtime.pending_observed_url = None;
        runtime.pending_observed_after_sequence = None;
    }
    runtime.current.url = detail.url;
    if !detail.same_document {
        runtime.current.title = None;
        runtime.last_observed_title = None;
        if !initiated {
            runtime.current.status = None;
        }
    }
}

fn apply_observed_navigation(
    runtime: &mut PageHeaderRuntime,
    url: String,
    observation: &NavigationObservation,
    replace_same_document_status: bool,
) {
    runtime.current.url = url.clone();
    if replace_same_document_status || !observation.same_document {
        runtime.current.status = observation.main_status;
    }
    if !observation.same_document {
        runtime.current.title = None;
        runtime.last_observed_title = None;
    }
}

fn page_runtime_for_registration(
    header_enabled: bool,
    events: impl FnOnce() -> Option<Box<dyn PageEventReceiver>>,
    details: impl FnOnce() -> Option<Box<dyn DetailReceiver>>,
    url: impl FnOnce() -> String,
) -> PageRuntime {
    PageRuntime {
        events: events(),
        navigation_details: if header_enabled { details() } else { None },
        detail_dropped_count: 0,
        pending_dialog: None,
        pending_file_chooser: None,
        title: None,
        header: header_enabled.then(|| PageHeaderRuntime {
            current: PageHeader {
                url: url(),
                ..PageHeader::default()
            },
            ..PageHeaderRuntime::default()
        }),
    }
}

trait PageRegistration {
    fn registration_target_id(&self) -> String;
    fn registration_arm_console_capture(&self) -> Result<(), Error> {
        Ok(())
    }
    fn registration_events(&self) -> Option<Box<dyn PageEventReceiver>>;
    fn registration_details(&self) -> Option<Box<dyn DetailReceiver>>;
    fn registration_url(&self) -> String;
}

impl PageRegistration for Page {
    fn registration_target_id(&self) -> String {
        self.target_id()
    }

    fn registration_arm_console_capture(&self) -> Result<(), Error> {
        self.arm_console_capture()
    }

    fn registration_events(&self) -> Option<Box<dyn PageEventReceiver>> {
        Some(Box::new(self.events()))
    }

    fn registration_details(&self) -> Option<Box<dyn DetailReceiver>> {
        Some(Box::new(self.navigation_details()))
    }

    fn registration_url(&self) -> String {
        self.url()
    }
}

fn apply_observed_title(
    runtime: &mut PageHeaderRuntime,
    pending_modal: bool,
    observed_title: Option<String>,
) {
    if let Some(title) = observed_title {
        runtime.last_observed_title = Some(title.clone());
        runtime.current.title = Some(title);
    } else if pending_modal {
        runtime.current.title = runtime.last_observed_title.clone();
    }
}

fn modal_safe_page_observation(
    pending_modal: bool,
    query: impl FnOnce() -> (Option<String>, Option<(usize, usize)>),
) -> (Option<String>, Option<(usize, usize)>) {
    if pending_modal { (None, None) } else { query() }
}

const MAX_DIGEST_URL_BYTES: usize = 1_536;
const MAX_DIGEST_TITLE_BYTES: usize = 512;

fn digest_field(value: &str, max_bytes: usize) -> String {
    let value = value.replace(['\r', '\n'], " ");
    if value.len() <= max_bytes {
        return value;
    }
    let mut cut = max_bytes.min(value.len());
    while cut > 0 && !value.is_char_boundary(cut) {
        cut -= 1;
    }
    let omitted = value.len() - cut;
    format!("{}… ({omitted} bytes omitted)", &value[..cut])
}

fn render_page_header(header: &PageHeader) -> String {
    let mut lines = vec![
        "### Page".to_owned(),
        format!("URL: {}", digest_field(&header.url, MAX_DIGEST_URL_BYTES)),
    ];
    if let Some(title) = header.title.as_deref() {
        lines.push(format!(
            "Title: {}",
            digest_field(title, MAX_DIGEST_TITLE_BYTES)
        ));
    }
    lines.push(format!(
        "Status: {}",
        header
            .status
            .map_or_else(|| "unknown".to_owned(), |status| status.to_string())
    ));
    lines.push(format!(
        "Console: {} errors, {} warnings",
        header.console_err, header.console_warn
    ));
    lines.join("\n")
}

fn record_page_render(
    runtime: &mut PageHeaderRuntime,
    browser: &mut BrowserHeaderState,
    current: &PageHeader,
    signature: TabSignature,
) -> bool {
    let changed = runtime.last_rendered.as_ref() != Some(current)
        || browser.last_rendered_tab_signature.as_ref() != Some(&signature);
    if changed {
        runtime.last_rendered = Some(current.clone());
        browser.last_rendered_tab_signature = Some(signature);
    }
    changed
}

struct PendingDialog {
    kind: DialogKind,
    message: String,
    dialog: Dialog,
}

struct PendingFileChooser {
    multiple: bool,
    chooser: FileChooser,
}

impl BrowserState {
    fn new(startup: BrowserStartup, features: FeatureConfig) -> Self {
        let header_state = features.header.then(BrowserHeaderState::default);
        let mut state = Self {
            features,
            header_state,
            inventory_stale: true,
            ..Self::default()
        };
        match startup {
            BrowserStartup::Local => {}
            BrowserStartup::InvalidRemote => {
                state.remote = true;
                state.startup_error = Some(REMOTE_UNREACHABLE);
                eprintln!("browser actor: remote CDP configuration is invalid");
            }
            BrowserStartup::Remote(options) => {
                state.remote = true;
                state.remote_options = Some(options);
            }
        }
        state
    }

    fn attach_remote(
        &mut self,
        mut options: ConnectOptions,
        request: &ActorRequest,
    ) -> Result<(), BrowserError> {
        if let Some(mut seam) = self.page_lifecycle_seam.take() {
            let candidate = seam.attach_remote(request);
            self.page_lifecycle_seam = Some(seam);
            let candidate = candidate?;
            return self.commit_remote_attach(candidate, None, request);
        }
        let remaining = Self::remaining(request)?;
        options.timeout = options
            .timeout
            .min(remaining.saturating_add(ENGINE_TIMEOUT_CUSHION));
        let browser = chromium()
            .connect_over_cdp_with_cancel(options, Some(&request.cancellation.engine))
            .map_err(|error| Self::remote_attach_error(error, request))?;
        let remaining = Self::remaining(request)?;
        let page = browser
            .pages_with_cancel(
                remaining.saturating_add(ENGINE_TIMEOUT_CUSHION),
                Some(&request.cancellation.engine),
            )
            .map_err(|error| Self::remote_attach_error(error, request))?
            .into_iter()
            .next()
            .map(Ok)
            .unwrap_or_else(|| {
                browser
                    .new_page_with_cancel(Some(&request.cancellation.engine))
                    .map_err(|error| Self::remote_attach_error(error, request))
            })?;
        self.commit_remote_attach(
            PageCandidate {
                registration: Box::new(page.clone()),
                handle: ActivePageHandle::live(page),
            },
            Some(browser),
            request,
        )
    }

    fn commit_remote_attach(
        &mut self,
        candidate: PageCandidate,
        browser: Option<Browser>,
        request: &ActorRequest,
    ) -> Result<(), BrowserError> {
        self.register_page_with_browser(candidate.registration.as_ref(), browser.as_ref())
            .map_err(|error| Self::remote_attach_error(error, request))?;
        self.page = Some(candidate.handle);
        self.browser = browser;
        Ok(())
    }

    fn remote_attach_error(error: Error, request: &ActorRequest) -> BrowserError {
        if matches!(error, Error::Cancelled) {
            return request
                .cancellation
                .reason()
                .error(request.timeout_ms)
                .unwrap_or(BrowserError::Cancelled);
        }
        if matches!(error, Error::Timeout(_)) {
            return BrowserError::Timeout(request.timeout_ms);
        }
        BrowserError::Message(REMOTE_UNREACHABLE.to_owned())
    }

    fn ensure_page(&mut self, request: &ActorRequest) -> Result<&ActivePageHandle, BrowserError> {
        self.ensure_page_for(request, false)
    }

    /// `committed_observation` marks a call that only observes state a committed
    /// action already produced.
    ///
    /// The engine raises its own physical-action-committed flag for pointer and key
    /// input, but a landed text write never raises it. Without this the request-level
    /// cancellation gate rejects the post-action snapshot before its grace budget is
    /// consulted, so a write that lands at the deadline can never return the masked
    /// state `capture_committed_action_state` promises.
    fn ensure_page_for(
        &mut self,
        request: &ActorRequest,
        committed_observation: bool,
    ) -> Result<&ActivePageHandle, BrowserError> {
        if !committed_observation
            && !request.cancellation.is_committed()
            && let Some(error) = request.cancellation.reason().error(request.timeout_ms)
        {
            return Err(error);
        }
        if let Some(error) = self.startup_error {
            return Err(BrowserError::Message(error.to_owned()));
        }
        if self.remote {
            if self.page.is_none() {
                let options = self
                    .remote_options
                    .clone()
                    .ok_or_else(|| BrowserError::Message(REMOTE_UNREACHABLE.to_owned()))?;
                eprintln!("browser actor: attaching remote CDP session lazily");
                if let Err(error) = self.attach_remote(options, request) {
                    if !matches!(error, BrowserError::Cancelled | BrowserError::Timeout(_)) {
                        self.remote_options = None;
                        self.startup_error = Some(REMOTE_UNREACHABLE);
                    }
                    eprintln!("browser actor: remote CDP attach failed");
                    return Err(error);
                }
                self.remote_options = None;
            }
            return self
                .page
                .as_ref()
                .ok_or_else(|| BrowserError::Message(REMOTE_UNREACHABLE.to_owned()));
        }
        if self.page.is_none()
            && let Some(mut seam) = self.page_lifecycle_seam.take()
        {
            let discovered = seam.discover_pages(request);
            self.page_lifecycle_seam = Some(seam);
            let mut discovered = discovered?;
            let candidate = if discovered.is_empty() {
                let mut seam = self
                    .page_lifecycle_seam
                    .take()
                    .expect("page lifecycle seam was restored");
                let candidate = seam.new_page(request);
                self.page_lifecycle_seam = Some(seam);
                candidate?
            } else {
                discovered.remove(0)
            };
            self.install_active_page(candidate.registration.as_ref(), candidate.handle, None)
                .map_err(|error| {
                    self.operation_error(
                        "console capture arm failed",
                        error,
                        &request.cancellation,
                        request.timeout_ms,
                    )
                })?;
            return Ok(self.page.as_ref().expect("test page was installed"));
        }
        if self.page_lifecycle_seam.is_some() && self.page.is_some() {
            return Ok(self.page.as_ref().expect("test page is already installed"));
        }
        if self.browser.is_none() {
            eprintln!("browser actor: launching Chromium lazily");
            let remaining = Self::remaining(request)?;
            let launched = chromium().launch_with_cancel(
                LaunchOptions::default().timeout(Some(Self::engine_timeout(remaining))),
                Some(&request.cancellation.engine),
            );
            self.browser = Some(launched.map_err(|error| {
                self.operation_error(
                    "browser launch failed",
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                )
            })?);
        }
        if self.page.is_none() {
            let remaining = Self::remaining(request)?;
            let existing = self
                .browser
                .as_ref()
                .expect("browser was initialized")
                .pages_with_cancel(
                    remaining.saturating_add(ENGINE_TIMEOUT_CUSHION),
                    Some(&request.cancellation.engine),
                )
                .map_err(|error| {
                    self.operation_error(
                        "initial page listing failed",
                        error,
                        &request.cancellation,
                        request.timeout_ms,
                    )
                })?
                .into_iter()
                .next();
            self.page = match existing {
                Some(page) => Some(ActivePageHandle::live(page)),
                None => {
                    let created = self
                        .browser
                        .as_ref()
                        .expect("browser was initialized")
                        .new_page_with_cancel(Some(&request.cancellation.engine));
                    Some(ActivePageHandle::live(created.map_err(|error| {
                        self.operation_error(
                            "new page failed",
                            error,
                            &request.cancellation,
                            request.timeout_ms,
                        )
                    })?))
                }
            };
        }
        if let Some(page) = self.page.clone() {
            if let Err(error) = self.register_page(
                page.live_page()
                    .expect("local active page should contain a live page"),
            ) {
                return Err(self.operation_error(
                    "console capture arm failed",
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                ));
            }
        }
        Ok(self.page.as_ref().expect("page was initialized"))
    }

    fn register_page(&mut self, page: &(impl PageRegistration + ?Sized)) -> Result<(), Error> {
        self.register_page_with_browser(page, None)
    }

    fn register_page_with_browser(
        &mut self,
        page: &(impl PageRegistration + ?Sized),
        registration_browser: Option<&Browser>,
    ) -> Result<(), Error> {
        let target_id = page.registration_target_id();
        let header_enabled = self.header_state.is_some();
        if !self.pages.contains_key(&target_id) {
            // The MCP actor owns navigation-scoped console presentation, so it
            // arms capture at registration before an operation can navigate the
            // page. Nothing actor-visible is published before this succeeds, so
            // failed arms leave the page unregistered and retryable.
            page.registration_arm_console_capture()?;
            if header_enabled && self.target_lifecycle.is_none() {
                self.target_lifecycle = (self.lifecycle_subscription_provider)(
                    registration_browser.or(self.browser.as_ref()),
                );
            }
            self.tab_order.push(target_id.clone());
            let url = header_enabled.then(|| page.registration_url());
            if let Some(url) = url.as_ref() {
                self.tab_inventory.insert(target_id.clone(), url.clone());
            }
            self.pages.insert(
                target_id,
                page_runtime_for_registration(
                    header_enabled,
                    || page.registration_events(),
                    || page.registration_details(),
                    || url.expect("header-enabled registration captured a URL"),
                ),
            );
        }
        Ok(())
    }

    fn install_active_page(
        &mut self,
        registration: &(impl PageRegistration + ?Sized),
        handle: ActivePageHandle,
        registration_browser: Option<&Browser>,
    ) -> Result<(), Error> {
        self.register_page_with_browser(registration, registration_browser)?;
        self.page = Some(handle);
        Ok(())
    }

    fn clear_active_target(&mut self, target_id: &str) {
        if self.active_target_id.as_deref() == Some(target_id)
            || self
                .page
                .as_ref()
                .is_some_and(|page| page.target_id() == target_id)
        {
            self.page = None;
            self.active_target_id = None;
        }
    }

    fn retire_closed_target(&mut self, target_id: &str) {
        self.clear_active_target(target_id);
        self.closing_targets.insert(target_id.to_owned());
        self.pages.remove(target_id);
        self.tab_order.retain(|registered| registered != target_id);
    }

    fn poll_events(&mut self) {
        if let Some(page) = self.page.as_ref() {
            self.active_target_id = Some(page.target_id());
        }
        while self.header_state.is_some() {
            let lifecycle = self
                .target_lifecycle
                .as_ref()
                .map(|events| events.try_recv_lifecycle())
                .transpose()
                .map(|event| event.flatten())
                .map_err(|_| ());
            let lifecycle = match lifecycle {
                Ok(Some(lifecycle)) => lifecycle,
                Ok(None) => break,
                Err(()) => {
                    self.inventory_stale = true;
                    self.target_lifecycle = None;
                    break;
                }
            };
            match lifecycle {
                TargetLifecycleEvent::Upsert { target_id, url } => {
                    if !self.tab_order.contains(&target_id) {
                        self.tab_order.push(target_id.clone());
                    }
                    self.tab_inventory.insert(target_id, url);
                }
                TargetLifecycleEvent::Destroyed { target_id } => {
                    self.pages.remove(&target_id);
                    self.tab_inventory.remove(&target_id);
                    self.tab_order.retain(|candidate| candidate != &target_id);
                    self.clear_active_target(&target_id);
                }
            }
        }
        let mut closed_targets = Vec::new();
        for (target_id, runtime) in self.pages.iter_mut() {
            if let Some(header) = runtime.header.as_mut() {
                if let Some(details) = runtime.navigation_details.as_ref() {
                    let dropped = details.dropped_count();
                    if dropped != runtime.detail_dropped_count {
                        runtime.detail_dropped_count = dropped;
                        header.pending_observed_url = None;
                        header.pending_observed_after_sequence = None;
                    }
                    while let Some((sequence, detail)) = details.try_recv_detail() {
                        apply_navigation_detail(header, sequence, detail);
                    }
                }
            }
            loop {
                let event = runtime
                    .events
                    .as_ref()
                    .and_then(|events| events.try_recv_page_event());
                let Some(event) = event else { break };
                match event {
                    PageEvent::Dialog {
                        kind,
                        message,
                        dialog,
                    } => {
                        runtime.pending_dialog = Some(PendingDialog {
                            kind,
                            message,
                            dialog,
                        });
                    }
                    PageEvent::FileChooser { multiple, chooser } => {
                        runtime.pending_file_chooser =
                            Some(PendingFileChooser { multiple, chooser });
                    }
                    PageEvent::Closed => closed_targets.push(target_id.clone()),
                    PageEvent::PageCrashed => {}
                    PageEvent::Navigated { .. } | PageEvent::Download { .. } => {}
                }
            }
        }
        for target_id in closed_targets {
            self.pages.remove(&target_id);
            self.tab_inventory.remove(&target_id);
            self.tab_order.retain(|candidate| candidate != &target_id);
            self.clear_active_target(&target_id);
        }
        for (target_id, runtime) in &self.pages {
            if let Some(header) = runtime.header.as_ref() {
                self.tab_inventory
                    .insert(target_id.clone(), header.current.url.clone());
            }
        }
    }

    fn begin_observed_navigation(&mut self, target_id: &str, url: Option<String>) {
        self.poll_events();
        let Some(runtime) = self.pages.get_mut(target_id) else {
            return;
        };
        let Some(header) = runtime.header.as_mut() else {
            return;
        };
        let Some((dropped_count, latest_sequence)) = runtime
            .navigation_details
            .as_ref()
            .map(|details| (details.dropped_count(), details.latest_sequence()))
        else {
            return;
        };
        runtime.detail_dropped_count = dropped_count;
        header.pending_observed_url = url;
        header.pending_observed_after_sequence = Some(latest_sequence);
    }

    fn cancel_observed_navigation(&mut self, target_id: &str) {
        if let Some(header) = self
            .pages
            .get_mut(target_id)
            .and_then(|runtime| runtime.header.as_mut())
        {
            header.pending_observed_url = None;
            header.pending_observed_after_sequence = None;
        }
    }

    fn record_observed_navigation(
        &mut self,
        target_id: &str,
        url: String,
        observation: &NavigationObservation,
        replace_same_document_status: bool,
    ) {
        let Some(header) = self
            .pages
            .get_mut(target_id)
            .and_then(|runtime| runtime.header.as_mut())
        else {
            return;
        };
        apply_observed_navigation(header, url, observation, replace_same_document_status);
        if header.pending_observed_after_sequence.is_some() {
            header.pending_observed_url = Some(header.current.url.clone());
        }
    }

    fn tab_signature(&self) -> TabSignature {
        TabSignature {
            tabs: self
                .tab_order
                .iter()
                .filter_map(|target_id| {
                    Some((
                        target_id.clone(),
                        self.tab_inventory.get(target_id)?.clone(),
                    ))
                })
                .collect(),
            active_id: self.page.as_ref().map(ActivePageHandle::target_id),
            stale: self.inventory_stale,
        }
    }

    fn reconcile_digest_inventory(&mut self, inventory: Vec<BrowserInventoryEntry>) {
        let discovered_targets = inventory
            .iter()
            .map(|entry| entry.target_id.clone())
            .collect::<HashSet<_>>();
        self.closing_targets
            .retain(|target_id| discovered_targets.contains(target_id));
        let reconciled_tab_order = inventory
            .iter()
            .filter(|entry| !self.closing_targets.contains(&entry.target_id))
            .map(|entry| entry.target_id.clone())
            .collect();
        self.tab_inventory
            .retain(|target_id, _| discovered_targets.contains(target_id));
        self.pages
            .retain(|target_id, _| discovered_targets.contains(target_id));

        for entry in inventory {
            if self.closing_targets.contains(&entry.target_id) {
                continue;
            }
            if let Some(page) = entry.page.as_ref() {
                if let Err(error) = self.register_page(page) {
                    eprintln!("browser actor: digest console capture arm failed: {error}");
                    self.inventory_stale = true;
                    continue;
                }
            } else if !self.pages.contains_key(&entry.target_id) {
                self.pages.insert(
                    entry.target_id.clone(),
                    PageRuntime {
                        events: None,
                        navigation_details: None,
                        detail_dropped_count: 0,
                        pending_dialog: None,
                        pending_file_chooser: None,
                        title: None,
                        header: Some(PageHeaderRuntime {
                            current: PageHeader {
                                url: entry.url.clone(),
                                ..PageHeader::default()
                            },
                            ..PageHeaderRuntime::default()
                        }),
                    },
                );
            }
            self.tab_inventory
                .insert(entry.target_id.clone(), entry.url.clone());
            if let Some(header) = self
                .pages
                .get_mut(&entry.target_id)
                .and_then(|runtime| runtime.header.as_mut())
            {
                header.current.url = entry.url;
            }
        }
        self.tab_order = reconciled_tab_order;
    }

    fn page_digest(&mut self, request: &ActorRequest) -> Option<String> {
        self.header_state.as_ref()?;
        if self.inventory_stale {
            let inventory = self
                .browser_query_provider
                .inventory(self.browser.as_ref(), request);
            self.inventory_stale = inventory.is_err();
            if let Ok(inventory) = inventory {
                self.reconcile_digest_inventory(inventory);
            }
        }
        self.poll_events();
        let (target_id, active_url) = self
            .browser_query_provider
            .active_page(self.page.as_ref())?;
        let pending_modal = self
            .browser_query_provider
            .pending_modal(&self.pages, &target_id);
        let live_page = self
            .page
            .as_ref()
            .and_then(ActivePageHandle::live_page)
            .filter(|page| page.target_id() == target_id);
        let (title, console_counts) = modal_safe_page_observation(pending_modal, || {
            self.browser_query_provider.observe(live_page, request)
        });
        let current = {
            let runtime = self.pages.get_mut(&target_id)?;
            let header = runtime.header.as_mut()?;
            header.current.url = active_url;
            apply_observed_title(header, pending_modal, title);
            if let Some((errors, warnings)) = console_counts {
                header.current.console_err = errors;
                header.current.console_warn = warnings;
            }
            header.current.clone()
        };
        self.tab_inventory
            .insert(target_id.clone(), current.url.clone());
        let mut signature = self.tab_signature();
        signature.active_id = Some(target_id.clone());
        let header = self
            .pages
            .get_mut(&target_id)
            .and_then(|runtime| runtime.header.as_mut())?;
        let state = self.header_state.as_mut()?;
        if !record_page_render(header, state, &current, signature) {
            return None;
        }
        Some(render_page_header(&current))
    }

    fn add_page_digest(&mut self, output: BrowserOutput, request: &ActorRequest) -> BrowserOutput {
        if matches!(output, BrowserOutput::Image { .. }) {
            return output;
        }
        let Some(page) = self.page_digest(request) else {
            return output;
        };
        match output {
            BrowserOutput::Text(text) => BrowserOutput::ShapedText {
                text: format!("{page}\n\n{text}"),
                shape: ResponseShape {
                    page: Some(page),
                    ..ResponseShape::default()
                },
            },
            BrowserOutput::ShapedText { text, mut shape } => {
                shape.page = Some(page.clone());
                BrowserOutput::ShapedText {
                    text: format!("{page}\n\n{text}"),
                    shape,
                }
            }
            image => image,
        }
    }

    fn has_pending_modal(&mut self) -> bool {
        self.poll_events();
        self.pages.values().any(|runtime| {
            runtime.pending_dialog.is_some() || runtime.pending_file_chooser.is_some()
        })
    }

    fn dialog_kind_name(kind: &DialogKind) -> &str {
        match kind {
            DialogKind::Alert => "alert",
            DialogKind::Confirm => "confirm",
            DialogKind::Prompt => "prompt",
            DialogKind::BeforeUnload => "beforeunload",
            DialogKind::Other(value) => value.as_str(),
        }
    }

    /// Render the pending modals as a caller-facing notice.
    ///
    /// This renders stored text verbatim, and deliberately takes no secret. A
    /// dialog carrying one is masked in `pending_dialog` before any render
    /// reaches it, so the guarantee holds for the lifetime of the dialog rather
    /// than for one reply -- see [`Self::redact_pending_dialogs`]. Masking here
    /// as well would be a second implementation of the same rule that no
    /// reachable route exercises, free to drift from the first.
    fn modal_response(&mut self, result: &str, _request: &ActorRequest) -> String {
        self.poll_events();
        let active_target = self.page.as_ref().map(ActivePageHandle::target_id);
        let mut lines = Vec::new();
        let mut recovery = Vec::new();
        for target_id in &self.tab_order {
            let Some(runtime) = self.pages.get(target_id) else {
                continue;
            };
            if let Some(pending) = &runtime.pending_dialog {
                // Browser-wide page enumeration can itself wait behind a
                // JavaScript modal. Keep the modal response independent of
                // renderer commands so every blocked operation returns
                // promptly and directs the caller to the recovery tool.
                let owner = if active_target.as_ref() == Some(target_id) {
                    "Current tab"
                } else {
                    "Registered tab"
                };
                let message = &pending.message;
                lines.push(format!(
                    "- {owner}: Dialog pending: type={}; message={:?}. Call browser_handle_dialog.",
                    Self::dialog_kind_name(&pending.kind),
                    message
                ));
                recovery.push(ModalRecovery {
                    owner,
                    kind: Self::dialog_kind_name(&pending.kind).to_owned(),
                    message: message.clone(),
                    instruction: "Call browser_handle_dialog.",
                });
            }
            if let Some(pending) = &runtime.pending_file_chooser {
                let owner = if active_target.as_ref() == Some(target_id) {
                    "Current tab"
                } else {
                    "Registered tab"
                };
                let hint = if pending.multiple {
                    "multiple files allowed"
                } else {
                    "single file only"
                };
                lines.push(format!(
                    "- {owner}: File chooser pending: {hint}. Call browser_file_upload."
                ));
                recovery.push(ModalRecovery {
                    owner,
                    kind: "file chooser".to_owned(),
                    message: hint.to_owned(),
                    instruction: "Call browser_file_upload.",
                });
            }
        }
        if lines.is_empty() {
            result.to_owned()
        } else {
            self.response_shape
                .get_or_insert_with(ResponseShape::default)
                .modal_recovery = recovery;
            format!("{result}\n\n### Modal\n{}", lines.join("\n"))
        }
    }

    /// Mask a secret inside every stored pending-dialog message.
    ///
    /// Redacting only the writing tool's own reply protects one response. The
    /// message itself stays in the page runtime until `browser_handle_dialog`
    /// retires it, and every other tool is fronted by the generic modal gate,
    /// which renders that stored text with no secret in hand -- so a plain
    /// `browser_snapshot` issued while the dialog is still up would hand back
    /// verbatim what the write had just masked. Masking at the point of storage
    /// is what makes the guarantee hold for the lifetime of the dialog instead
    /// of for a single reply, and it holds for readers added later without
    /// their having to know a secret was involved.
    fn redact_pending_dialogs(&mut self, sensitive_value: &str) {
        if sensitive_value.is_empty() {
            return;
        }
        for runtime in self.pages.values_mut() {
            if let Some(pending) = runtime.pending_dialog.as_mut() {
                pending.message = pending.message.replace(sensitive_value, SECRET_MASK);
            }
        }
    }

    fn operation_error(
        &self,
        context: &str,
        error: Error,
        cancellation: &CommandCancellation,
        timeout_ms: u64,
    ) -> BrowserError {
        if matches!(error, Error::Cancelled) {
            return cancellation
                .reason()
                .error(timeout_ms)
                .unwrap_or(BrowserError::Cancelled);
        }
        if matches!(error, Error::Timeout(_)) {
            return BrowserError::Timeout(timeout_ms);
        }
        let disconnected = self
            .browser
            .as_ref()
            .is_some_and(|browser| !browser.is_connected());
        if self.remote && (disconnected || matches!(error, Error::ConnectFailed | Error::Closed)) {
            BrowserError::Message(REMOTE_UNREACHABLE.to_owned())
        } else {
            BrowserError::Message(format!("{context}: {error}"))
        }
    }

    fn remaining(request: &ActorRequest) -> Result<Duration, BrowserError> {
        let remaining = request.deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            Err(BrowserError::Timeout(request.timeout_ms))
        } else {
            Ok(remaining)
        }
    }

    fn engine_timeout(remaining: Duration) -> f64 {
        duration_millis(remaining.saturating_add(ENGINE_TIMEOUT_CUSHION)) as f64
    }

    fn navigate(&mut self, url: &str, request: &ActorRequest) -> TextResult {
        self.poll_events();
        self.current_refs.clear();
        let remaining = Self::remaining(request)?;
        let started_at = Instant::now();
        let page = self.ensure_page(request)?.clone();
        let target_id = page.target_id();
        self.begin_observed_navigation(&target_id, Some(url.to_owned()));
        let result = page.goto_with_cancel_observed(
            url,
            GotoOptions::default()
                .wait_until("load")
                .timeout(Self::engine_timeout(remaining)),
            Some(&request.cancellation.engine),
        );
        let observation = match result {
            Ok(observation) => observation,
            Err(error) => {
                self.cancel_observed_navigation(&target_id);
                if matches!(error, Error::Cancelled)
                    && request.cancellation.reason() == CancellationReason::Deadline
                {
                    if let Some(page) = self.page.as_ref() {
                        page.emit_navigation_timeout_diagnostic(started_at.elapsed());
                    }
                }
                return Err(self.operation_error(
                    "navigation failed",
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                ));
            }
        };
        self.record_observed_navigation(&target_id, page.url(), &observation, false);
        self.snapshot(request)
    }

    fn navigate_back(&mut self, request: &ActorRequest) -> TextResult {
        self.poll_events();
        self.current_refs.clear();
        let remaining = Self::remaining(request)?;
        let page = self.ensure_page(request)?.clone();
        let target_id = page.target_id();
        self.begin_observed_navigation(&target_id, None);
        let result = page.go_back_with_cancel_observed(
            GotoOptions::default()
                .wait_until("load")
                .timeout(Self::engine_timeout(remaining)),
            Some(&request.cancellation.engine),
        );
        let observation = match result {
            Ok(observation) => observation,
            Err(error) => {
                self.cancel_observed_navigation(&target_id);
                return Err(self.operation_error(
                    "back navigation failed",
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                ));
            }
        };
        if !observation.had_entry {
            self.cancel_observed_navigation(&target_id);
            return Err(BrowserError::Message("no back history".to_owned()));
        }
        self.record_observed_navigation(&target_id, page.url(), &observation.navigation, false);
        self.snapshot(request)
    }

    fn navigate_forward(&mut self, request: &ActorRequest) -> TextResult {
        self.poll_events();
        self.current_refs.clear();
        let remaining = Self::remaining(request)?;
        let page = self.ensure_page(request)?.clone();
        let target_id = page.target_id();
        self.begin_observed_navigation(&target_id, None);
        let result = page.go_forward_with_cancel_observed(
            GotoOptions::default()
                .wait_until("load")
                .timeout(Self::engine_timeout(remaining)),
            Some(&request.cancellation.engine),
        );
        let observation = match result {
            Ok(observation) => observation,
            Err(error) => {
                self.cancel_observed_navigation(&target_id);
                return Err(self.operation_error(
                    "forward navigation failed",
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                ));
            }
        };
        if !observation.had_entry {
            self.cancel_observed_navigation(&target_id);
            return Err(BrowserError::Message("no forward history".to_owned()));
        }
        self.record_observed_navigation(&target_id, page.url(), &observation.navigation, false);
        self.snapshot(request)
    }

    fn reload(&mut self, request: &ActorRequest) -> TextResult {
        self.poll_events();
        self.current_refs.clear();
        let remaining = Self::remaining(request)?;
        let page = self.ensure_page(request)?.clone();
        let target_id = page.target_id();
        self.begin_observed_navigation(&target_id, Some(page.url()));
        let result = page.reload_with_cancel_observed(
            GotoOptions::default()
                .wait_until("load")
                .timeout(Self::engine_timeout(remaining)),
            Some(&request.cancellation.engine),
        );
        let observation = match result {
            Ok(observation) => observation,
            Err(error) => {
                self.cancel_observed_navigation(&target_id);
                return Err(self.operation_error(
                    "reload failed",
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                ));
            }
        };
        self.record_observed_navigation(&target_id, page.url(), &observation, true);
        self.snapshot(request)
    }

    fn resize(&mut self, width: u32, height: u32, request: &ActorRequest) -> TextResult {
        let result = self.ensure_page(request)?.set_viewport_size(width, height);
        result.map_err(|error| {
            self.operation_error(
                "viewport resize failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            )
        })?;
        self.current_refs.clear();
        self.snapshot(request)
    }

    fn snapshot(&mut self, request: &ActorRequest) -> TextResult {
        self.snapshot_options(
            request,
            None,
            None,
            false,
            Some(&request.cancellation.engine),
            None,
            None,
        )
    }

    fn targeted_snapshot(
        &mut self,
        target: Option<&str>,
        depth: Option<u32>,
        boxes: bool,
        request: &ActorRequest,
    ) -> TextResult {
        if let Some(target) = target
            && !self.current_refs.contains(target)
        {
            return Err(BrowserError::Message(format!(
                "unknown or stale ref {target}; call browser_snapshot and use its latest refs"
            )));
        }
        self.snapshot_options(
            request,
            target,
            depth,
            boxes,
            Some(&request.cancellation.engine),
            None,
            None,
        )
    }

    fn snapshot_with_cancel(
        &mut self,
        request: &ActorRequest,
        cancel: Option<&CancelToken>,
    ) -> TextResult {
        let committed_budget = cancel
            .is_none()
            .then_some(Duration::from_millis(SENSITIVE_TRACKING_CLEANUP_GRACE_MS));
        self.snapshot_options(request, None, None, false, cancel, committed_budget, None)
    }

    fn snapshot_with_sensitive_modal_redaction(
        &mut self,
        sensitive_value: &str,
        request: &ActorRequest,
    ) -> TextResult {
        self.snapshot_options(
            request,
            None,
            None,
            false,
            None,
            Some(Duration::from_millis(SENSITIVE_TRACKING_CLEANUP_GRACE_MS)),
            Some(sensitive_value),
        )
    }

    // The post-action snapshot of a committed physical action is an OBSERVATION,
    // not part of the action. Passing `None` for the cancel token keeps a late
    // cancel from killing it. It receives the same bounded grace used for
    // sensitive cleanup so a write that lands at the request deadline can still
    // return its masked state. Callers reach the post-snapshot only after an
    // action completed or live progress was observed, so any degradation in
    // `committed_snapshot_result` applies to state the page may already contain.
    fn capture_committed_action_state(&mut self, request: &ActorRequest) -> TextResult {
        committed_snapshot_result(self.snapshot_with_cancel(request, None))
    }

    fn committed_post_action_snapshot(&mut self, request: &ActorRequest) -> TextResult {
        self.capture_committed_action_state(request)
    }

    fn committed_sensitive_post_action_snapshot(
        &mut self,
        sensitive_value: &str,
        request: &ActorRequest,
    ) -> TextResult {
        // Both steps below evaluate in the page, so a dialog opened by the write
        // itself blocks either one. The resolve runs first and fails first, which
        // is why the modal check has to cover both rather than sitting after a
        // `?` on the resolve.
        // The resolved progress only matters on the error paths that have to
        // classify how much of the write landed; here the write already returned
        // `Ok`, so the resolve is called for the taints it applies.
        let snapshot = self
            .resolve_sensitive_snapshot_tracking(sensitive_value, request)
            .and_then(|_progress| {
                self.snapshot_with_sensitive_modal_redaction(sensitive_value, request)
            });
        // A dialog opened by the write races the modal check at the top of
        // `snapshot_options`: the renderer blocks before the CDP dialog event has
        // been polled in, so the attempt evaluates into the block and can only
        // fail. Re-check once it returns, the same way the click path does, so the
        // caller gets the redacted modal notice instead of a bare capture failure,
        // and the secret the dialog is displaying is masked while the value is
        // still in hand.
        //
        // Taint resolution cannot have run in that case -- the renderer stays
        // blocked until the dialog is handled, and the secret is not retained past
        // this call -- so an echo left in the DOM is not masked in a snapshot taken
        // after `browser_handle_dialog`. That is the same point-in-time limit
        // documented for echoes appearing after the resolve, reached by a different
        // route, and it is why the dialog message itself is redacted here rather
        // than left to the snapshot renderer.
        if self.has_pending_modal() {
            // Mask the stored message, not just the reply below. This is the
            // only moment the secret and the dialog are both in hand: the
            // message outlives this call, and the generic modal gate that
            // fronts every other tool renders it knowing nothing about
            // secrets. Redacting here is what stops a plain `browser_snapshot`
            // issued before `browser_handle_dialog` from handing back verbatim
            // what this write just masked.
            self.redact_pending_dialogs(sensitive_value);
            if snapshot.is_err() {
                return Ok(self.modal_response(
                    "Snapshot deferred until the pending modal is handled.",
                    request,
                ));
            }
        }
        committed_snapshot_result(snapshot)
    }

    fn snapshot_options(
        &mut self,
        request: &ActorRequest,
        target: Option<&str>,
        depth: Option<u32>,
        boxes: bool,
        cancel: Option<&CancelToken>,
        remaining_override: Option<Duration>,
        sensitive_value: Option<&str>,
    ) -> TextResult {
        if self.has_pending_modal() {
            // A dialog opened after taint resolution returned reaches this gate
            // before the caller's own post-action modal check, so the secret is
            // masked in storage here too rather than only in the reply. Both
            // sites call the one masking rule; neither renders its own.
            if let Some(value) = sensitive_value {
                self.redact_pending_dialogs(value);
            }
            return Ok(self.modal_response(
                "Snapshot deferred until the pending modal is handled.",
                request,
            ));
        }
        let (value, start_ref) = self.evaluate_snapshot(
            request,
            target,
            depth,
            boxes,
            cancel,
            remaining_override,
            None,
        )?;
        let outline = value
            .get("outline")
            .and_then(Value::as_str)
            .ok_or_else(|| BrowserError::Message(format!("snapshot returned no outline: {value}")))?
            .to_owned();
        let units: Vec<String> = value
            .get("units")
            .and_then(Value::as_array)
            .map(|units| {
                units
                    .iter()
                    .filter_map(Value::as_str)
                    .map(ToOwned::to_owned)
                    .collect()
            })
            .unwrap_or_else(|| outline.lines().map(ToOwned::to_owned).collect());
        let renderer_incomplete = value
            .get("rendererIncomplete")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
        let renderer_incomplete_index = renderer_incomplete
            .as_ref()
            .map(|_| units.len().saturating_sub(1));
        self.response_shape
            .get_or_insert_with(ResponseShape::default)
            .snapshot = Some(SnapshotStructure {
            legacy: outline.clone(),
            head: units.first().cloned(),
            units,
            renderer_incomplete,
            renderer_incomplete_index,
        });
        self.commit_snapshot_refs(&value, start_ref)?;
        Ok(outline)
    }

    #[allow(clippy::too_many_arguments)]
    fn evaluate_snapshot(
        &mut self,
        request: &ActorRequest,
        target: Option<&str>,
        depth: Option<u32>,
        boxes: bool,
        cancel: Option<&CancelToken>,
        remaining_override: Option<Duration>,
        find: Option<Value>,
    ) -> Result<(Value, u64), BrowserError> {
        let start_ref = self.next_ref.max(1);
        let remaining = match remaining_override {
            Some(remaining) => remaining,
            None => Self::remaining(request)?,
        };
        // Select legacy before entering the page. The W2 script therefore cannot
        // clear or replace refs while the experiment switch is off.
        let script = selected_snapshot_script(self.features.distill);
        let input = json!({
            "startRef": start_ref,
            "target": target.map(|target| format!(r#"[data-mcp-ref="{target}"]"#)),
            "maxDepth": depth,
            "boxes": boxes,
            "mask": SECRET_MASK,
            "find": find,
        });
        if let Some(evaluate) = self.snapshot_evaluator.as_mut() {
            return evaluate(script, &input).map(|value| (value, start_ref));
        }
        // A `remaining_override` is only ever supplied by the committed post-action
        // path, which also passes no cancel token; both say the same thing, that this
        // snapshot observes an action that already landed.
        let page = self.ensure_page_for(request, remaining_override.is_some())?;
        let result = page.evaluate_with_cancel(
            script,
            Some(&input),
            ActionOptions::timeout(Self::engine_timeout(remaining)),
            cancel,
        );
        let value = result.map_err(|error| {
            self.operation_error(
                "snapshot evaluation failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            )
        })?;
        Ok((value, start_ref))
    }

    fn commit_snapshot_refs(&mut self, value: &Value, start_ref: u64) -> Result<(), BrowserError> {
        let next_ref = value
            .get("nextRef")
            .and_then(Value::as_u64)
            .ok_or_else(|| {
                BrowserError::Message(format!("snapshot returned no nextRef: {value}"))
            })?;
        if next_ref < start_ref {
            return Err(BrowserError::Message(format!(
                "snapshot ref counter regressed from {start_ref} to {next_ref}"
            )));
        }
        self.current_refs = value
            .get("refs")
            .and_then(Value::as_array)
            .map(|refs| {
                refs.iter()
                    .filter_map(Value::as_str)
                    .map(ToOwned::to_owned)
                    .collect()
            })
            .unwrap_or_else(|| (start_ref..next_ref).map(|n| format!("e{n}")).collect());
        self.next_ref = next_ref;
        Ok(())
    }

    fn begin_sensitive_snapshot_tracking(
        &mut self,
        selector: &str,
        target: &str,
        request: &ActorRequest,
    ) -> Result<bool, BrowserError> {
        let remaining = Self::remaining(request)?;
        let result = self.ensure_page(request)?.evaluate_with_cancel(
            BEGIN_SENSITIVE_SNAPSHOT_TRACKING_JS,
            Some(&json!({
                "selector": selector,
                "expiryMs": sensitive_tracking_expiry_ms(remaining),
            })),
            ActionOptions::timeout(Self::engine_timeout(remaining)),
            Some(&request.cancellation.engine),
        );
        let value = match result {
            Ok(value) => value,
            Err(error) => {
                let error = self.operation_error(
                    &format!("sensitive snapshot tracking failed for {target}"),
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                );
                // The page function may have installed its observer before the
                // evaluation timed out or was cancelled.
                self.discard_sensitive_snapshot_tracking();
                return Err(error);
            }
        };
        match value.as_bool() {
            Some(tracks_password) => Ok(tracks_password),
            None => {
                self.discard_sensitive_snapshot_tracking();
                Err(BrowserError::Message(
                    "sensitive snapshot tracking returned no password state".to_owned(),
                ))
            }
        }
    }

    fn resolve_sensitive_snapshot_tracking(
        &mut self,
        sensitive_value: &str,
        request: &ActorRequest,
    ) -> Result<SensitiveWriteProgress, BrowserError> {
        let page = self.page.as_ref().ok_or_else(|| {
            BrowserError::Message(
                "sensitive snapshot tracking resolution found no active page".to_owned(),
            )
        })?;
        let result = page.evaluate_with_cancel(
            RESOLVE_SENSITIVE_SNAPSHOT_TRACKING_JS,
            Some(&json!({ "value": sensitive_value })),
            // The password write has already committed. Resolution is cleanup
            // for that committed write and must still run when a later submit
            // or form step exhausted the request's original deadline.
            ActionOptions::timeout(1_000.0),
            None,
        );
        let value = result.map_err(|error| {
            self.operation_error(
                "sensitive snapshot tracking resolution failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            )
        })?;
        if value.get("resolved").and_then(Value::as_bool) != Some(true) {
            return Err(BrowserError::Message(
                "sensitive snapshot tracking was no longer available".to_owned(),
            ));
        }
        match value.get("writeStatus").and_then(Value::as_str) {
            Some("complete") => Ok(SensitiveWriteProgress::Complete),
            Some("partial") => Ok(SensitiveWriteProgress::Partial),
            Some("unchanged") => Ok(SensitiveWriteProgress::Unchanged),
            _ => Err(BrowserError::Message(
                "sensitive snapshot tracking returned no write status".to_owned(),
            )),
        }
    }

    fn discard_sensitive_snapshot_tracking(&mut self) {
        let Some(page) = self.page.as_ref() else {
            return;
        };
        if let Err(error) = page.evaluate_with_cancel(
            DISCARD_SENSITIVE_SNAPSHOT_TRACKING_JS,
            None,
            ActionOptions::timeout(1_000.0),
            None,
        ) {
            // Cleanup is best-effort because the document may already be gone.
            // If it is still alive but temporarily unreachable, the observer's
            // page-side expiry bounds retained nodes to the request deadline
            // plus a short cleanup grace.
            eprintln!("browser actor: sensitive snapshot tracking cleanup failed: {error}");
        }
    }

    fn find(
        &mut self,
        text: Option<&str>,
        regex: Option<&RegexSpec>,
        request: &ActorRequest,
    ) -> TextResult {
        if self.features.distill {
            return self.find_constructed(text, regex, request);
        }
        let outline = self.snapshot(request)?;
        if self.has_pending_modal() {
            return Ok(outline);
        }
        let lines = outline.lines().map(ToOwned::to_owned).collect::<Vec<_>>();
        let matching_indices = if let Some(text) = text {
            let needle = text.to_lowercase();
            lines
                .iter()
                .enumerate()
                .filter_map(|(index, line)| line.to_lowercase().contains(&needle).then_some(index))
                .collect::<Vec<_>>()
        } else if let Some(regex) = regex {
            let remaining = Self::remaining(request)?;
            let value = self.ensure_page(request)?.evaluate_with_cancel(
                FIND_REGEX_JS,
                Some(&json!({
                    "lines": lines,
                    "pattern": regex.pattern,
                    "flags": regex.flags,
                })),
                ActionOptions::timeout(Self::engine_timeout(remaining)),
                Some(&request.cancellation.engine),
            );
            value
                .map_err(|error| {
                    self.operation_error(
                        "find regex failed",
                        error,
                        &request.cancellation,
                        request.timeout_ms,
                    )
                })?
                .as_array()
                .ok_or_else(|| {
                    BrowserError::Message("find regex returned no match indices".to_owned())
                })?
                .iter()
                .filter_map(Value::as_u64)
                .map(|index| index as usize)
                .collect()
        } else {
            Vec::new()
        };
        let (rendered, structure) = render_find_matches(&lines, &matching_indices);
        self.response_shape
            .get_or_insert_with(ResponseShape::default)
            .find = Some(structure);
        Ok(rendered)
    }

    fn find_constructed(
        &mut self,
        text: Option<&str>,
        regex: Option<&RegexSpec>,
        request: &ActorRequest,
    ) -> TextResult {
        if self.has_pending_modal() {
            return self.snapshot(request);
        }
        let query = if let Some(text) = text {
            json!({"kind": "text", "value": text})
        } else if let Some(regex) = regex {
            json!({"kind": "regex", "pattern": regex.pattern, "flags": regex.flags})
        } else {
            json!({"kind": "text", "value": ""})
        };
        let (value, start_ref) = self.evaluate_snapshot(
            request,
            None,
            None,
            false,
            Some(&request.cancellation.engine),
            None,
            Some(query),
        )?;
        self.commit_snapshot_refs(&value, start_ref)?;
        let find = value.get("find").ok_or_else(|| {
            BrowserError::Message(format!("constructed find returned no result: {value}"))
        })?;
        let matches = find
            .get("matches")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                BrowserError::Message("constructed find returned no matches".to_owned())
            })?;
        let total = find
            .get("totalMatches")
            .and_then(Value::as_u64)
            .ok_or_else(|| {
                BrowserError::Message("constructed find returned no total match count".to_owned())
            })? as usize;
        let blocks = matches
            .iter()
            .enumerate()
            .map(|(index, item)| {
                let path = item.get("path").and_then(Value::as_str).unwrap_or("(root)");
                let line = item.get("line").and_then(Value::as_str).unwrap_or("");
                format!("Match {}\nPath: {path}\n> {line}", index + 1)
            })
            .collect::<Vec<_>>();
        let incomplete = if find.get("incomplete").and_then(Value::as_bool) == Some(true) {
            let covered = find
                .get("coveredElements")
                .and_then(Value::as_u64)
                .unwrap_or_default();
            let reason = find
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("construction valve");
            Some(format!(
                "… snapshot construction incomplete after covering {covered} elements ({reason})."
            ))
        } else {
            None
        };
        let mut rendered = if blocks.is_empty() {
            "No matches in the constructed page subset.".to_owned()
        } else {
            blocks.join("\n\n")
        };
        let actor_omitted = total.saturating_sub(blocks.len());
        if actor_omitted > 0 {
            rendered.push_str(&format!(
                "\n\n… {actor_omitted} additional matches truncated; refine the text or regex query."
            ));
        }
        if let Some(marker) = incomplete.as_deref() {
            rendered.push_str("\n\n");
            rendered.push_str(marker);
        }
        self.response_shape
            .get_or_insert_with(ResponseShape::default)
            .find = Some(FindStructure {
            blocks,
            actor_omitted,
            incomplete,
        });
        Ok(rendered)
    }

    fn dispatch_ref_action<Action, PostSnapshot>(
        &mut self,
        target: &str,
        action: Action,
        post_snapshot: PostSnapshot,
    ) -> TextResult
    where
        Action: FnOnce(&mut Self) -> Result<(), BrowserError>,
        PostSnapshot: FnOnce(&mut Self) -> TextResult,
    {
        if !self.current_refs.contains(target) {
            return Err(BrowserError::Message(format!(
                "unknown or stale ref {target}; call browser_snapshot and use its latest refs"
            )));
        }
        self.current_refs.clear();
        action(self)?;
        post_snapshot(self)
    }

    fn dispatch_ref_pair_action<Action, PostSnapshot>(
        &mut self,
        start_target: &str,
        end_target: &str,
        action: Action,
        post_snapshot: PostSnapshot,
    ) -> TextResult
    where
        Action: FnOnce(&mut Self) -> Result<(), BrowserError>,
        PostSnapshot: FnOnce(&mut Self) -> TextResult,
    {
        for target in [start_target, end_target] {
            if !self.current_refs.contains(target) {
                return Err(BrowserError::Message(format!(
                    "unknown or stale ref {target}; call browser_snapshot and use its latest refs"
                )));
            }
        }
        self.current_refs.clear();
        action(self)?;
        post_snapshot(self)
    }

    fn click(&mut self, target: &str, double_click: bool, request: &ActorRequest) -> TextResult {
        let selector = format!(r#"[data-mcp-ref="{target}"]"#);
        self.dispatch_ref_action(
            target,
            |state| {
                let remaining = Self::remaining(request)?;
                // A JavaScript dialog or intercepted file chooser can stall the
                // engine's post-dispatch action wait. Run the physical click on
                // a worker so this actor can surface the already-subscribed
                // modal event as soon as Chromium emits it. The committed click
                // worker still owns its release cleanup and is not cancelled or
                // abandoned mid-input.
                let page = state.ensure_page(request)?.clone();
                let selector = selector.clone();
                let cancellation = request.cancellation.engine.clone();
                let options = ActionOptions::timeout(Self::engine_timeout(remaining));
                let (result_tx, result_rx) = sync_channel(1);
                thread::Builder::new()
                    .name("mcp-browser-click".to_owned())
                    .spawn(move || {
                        let result = if double_click {
                            page.dblclick_with_cancel(&selector, options, Some(&cancellation))
                        } else {
                            page.click_with_cancel(&selector, options, Some(&cancellation))
                        };
                        let _ = result_tx.send(result);
                    })
                    .map_err(|error| {
                        BrowserError::Message(format!("failed to start click worker: {error}"))
                    })?;
                loop {
                    if state.has_pending_modal() {
                        return Ok(());
                    }
                    match result_rx.try_recv() {
                        Ok(Ok(())) => return Ok(()),
                        Ok(Err(error)) => {
                            if state.has_pending_modal() {
                                return Ok(());
                            }
                            return Err(state.operation_error(
                                &format!("click failed for {target}"),
                                error,
                                &request.cancellation,
                                request.timeout_ms,
                            ));
                        }
                        Err(TryRecvError::Disconnected) => {
                            return Err(BrowserError::Message(
                                "click worker stopped without a result".to_owned(),
                            ));
                        }
                        Err(TryRecvError::Empty) => {
                            thread::sleep(Duration::from_millis(2));
                        }
                    }
                }
            },
            // The physical click has committed, so cancellation is too late:
            // finish the post-click snapshot and preserve its owned result.
            |state| state.committed_post_action_snapshot(request),
        )
    }

    fn drag(
        &mut self,
        start_target: &str,
        end_target: &str,
        start_element: Option<&str>,
        end_element: Option<&str>,
        request: &ActorRequest,
    ) -> TextResult {
        let start_selector = format!(r#"[data-mcp-ref="{start_target}"]"#);
        let end_selector = format!(r#"[data-mcp-ref="{end_target}"]"#);
        let start_display = start_element.unwrap_or(start_target).to_owned();
        let end_display = end_element.unwrap_or(end_target).to_owned();
        let snapshot = self.dispatch_ref_pair_action(
            start_target,
            end_target,
            |state| {
                let remaining = Self::remaining(request)?;
                let page = state.ensure_page(request)?.clone();
                let cancellation = request.cancellation.engine.clone();
                let options = ActionOptions::timeout(Self::engine_timeout(remaining));
                let (result_tx, result_rx) = sync_channel(1);
                thread::Builder::new()
                    .name("mcp-browser-drag".to_owned())
                    .spawn(move || {
                        let result = page.drag_and_drop_with_cancel(
                            &start_selector,
                            &end_selector,
                            options,
                            Some(&cancellation),
                        );
                        let _ = result_tx.send(result);
                    })
                    .map_err(|error| {
                        BrowserError::Message(format!("failed to start drag worker: {error}"))
                    })?;
                loop {
                    if state.has_pending_modal() {
                        return Ok(());
                    }
                    match result_rx.try_recv() {
                        Ok(Ok(())) => return Ok(()),
                        Ok(Err(error)) => {
                            if state.has_pending_modal() {
                                return Ok(());
                            }
                            return Err(state.operation_error(
                                &format!("drag failed from {start_target} to {end_target}"),
                                error,
                                &request.cancellation,
                                request.timeout_ms,
                            ));
                        }
                        Err(TryRecvError::Disconnected) => {
                            return Err(BrowserError::Message(
                                "drag worker stopped without a result".to_owned(),
                            ));
                        }
                        Err(TryRecvError::Empty) => {
                            thread::sleep(Duration::from_millis(2));
                        }
                    }
                }
            },
            // Drag commits physical pointer input: by the time this post-action
            // snapshot runs the drop has already landed, so route it through the
            // committed-action helper. A failed observation must degrade to a
            // truthful success instead of reporting the committed drag as failed.
            |state| state.committed_post_action_snapshot(request),
        )?;
        Ok(render_drag_result(&start_display, &end_display, &snapshot))
    }

    fn scroll_target(&mut self, target: &str, request: &ActorRequest) -> TextResult {
        let selector = format!(r#"[data-mcp-ref="{target}"]"#);
        self.dispatch_ref_action(
            target,
            |state| {
                let remaining = Self::remaining(request)?;
                let result = state.ensure_page(request)?.scroll_into_view_with_cancel(
                    &selector,
                    ActionOptions::timeout(Self::engine_timeout(remaining)),
                    Some(&request.cancellation.engine),
                );
                result.map_err(|error| {
                    state.operation_error(
                        &format!("scroll failed for {target}"),
                        error,
                        &request.cancellation,
                        request.timeout_ms,
                    )
                })
            },
            |state| state.snapshot(request),
        )
    }

    fn scroll_viewport(&mut self, delta_y: f64, request: &ActorRequest) -> TextResult {
        self.current_refs.clear();
        let remaining = Self::remaining(request)?;
        let result = self.ensure_page(request)?.scroll_viewport_with_cancel(
            delta_y,
            ActionOptions::timeout(Self::engine_timeout(remaining)),
            Some(&request.cancellation.engine),
        );
        result.map_err(|error| {
            self.operation_error(
                "viewport scroll failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            )
        })?;
        self.snapshot(request)
    }

    fn type_text(
        &mut self,
        target: &str,
        text: &str,
        submit: bool,
        slowly: bool,
        clear: bool,
        request: &ActorRequest,
    ) -> TextResult {
        let selector = format!(r#"[data-mcp-ref="{target}"]"#);
        let tracks_sensitive_value = Cell::new(false);
        let write_completed = Cell::new(false);
        let write_partially_completed = Cell::new(false);
        let sensitive_tracking_resolved = Cell::new(false);
        let post_write_error = RefCell::new(None);
        self.dispatch_ref_action(
            target,
            |state| {
                let tracks_password =
                    state.begin_sensitive_snapshot_tracking(&selector, target, request)?;
                tracks_sensitive_value.set(tracks_password);
                let result = (|| {
                    let remaining = Self::remaining(request)?;
                    let options = ActionOptions::timeout(Self::engine_timeout(remaining));
                    let page = state.ensure_page(request)?.clone();
                    let result = if clear && !slowly {
                        page.fill_with_cancel(
                            &selector,
                            text,
                            options,
                            Some(&request.cancellation.engine),
                        )
                    } else {
                        if clear {
                            page.fill_with_cancel(
                                &selector,
                                "",
                                options,
                                Some(&request.cancellation.engine),
                            )
                            .map_err(|error| {
                                state.operation_error(
                                    &format!("clear failed for {target}"),
                                    error,
                                    &request.cancellation,
                                    request.timeout_ms,
                                )
                            })?;
                        }
                        let typing_budget = Self::remaining(request)?;
                        page.type_text_with_options_and_cancel(
                            &selector,
                            text,
                            slowly.then_some(Duration::from_millis(50)),
                            ActionOptions::timeout(Self::engine_timeout(typing_budget)),
                            Some(&request.cancellation.engine),
                        )
                    };
                    result.map_err(|error| {
                        state.operation_error(
                            &format!("type failed for {target}"),
                            error,
                            &request.cancellation,
                            request.timeout_ms,
                        )
                    })?;
                    write_completed.set(true);
                    if submit {
                        let submit_budget = Self::remaining(request)?;
                        state
                            .ensure_page(request)?
                            .press_key_with_options_and_cancel(
                                Some(&selector),
                                "Enter",
                                ActionOptions::timeout(Self::engine_timeout(submit_budget)),
                                Some(&request.cancellation.engine),
                            )
                            .map_err(|error| {
                                state.operation_error(
                                    &format!("submit failed for {target}"),
                                    error,
                                    &request.cancellation,
                                    request.timeout_ms,
                                )
                            })?;
                    }
                    Ok(())
                })();
                match result {
                    Err(error) if write_completed.get() => {
                        post_write_error.replace(Some(error));
                        Ok(())
                    }
                    Err(error) if tracks_password => {
                        match state.resolve_sensitive_snapshot_tracking(text, request) {
                            Ok(progress) => {
                                sensitive_tracking_resolved.set(true);
                                match progress {
                                    SensitiveWriteProgress::Complete => {
                                        write_completed.set(true);
                                        post_write_error.replace(Some(error));
                                        Ok(())
                                    }
                                    SensitiveWriteProgress::Partial => {
                                        write_partially_completed.set(true);
                                        post_write_error.replace(Some(error));
                                        Ok(())
                                    }
                                    SensitiveWriteProgress::Unchanged => Err(error),
                                }
                            }
                            Err(_) => {
                                // The resolver may itself have timed out after
                                // running in the document. Retry ephemerally in
                                // the committed post-action path and leave its
                                // deadline-derived expiry armed if unreachable.
                                write_partially_completed.set(true);
                                post_write_error.replace(Some(error));
                                Ok(())
                            }
                        }
                    }
                    Err(error) => Err(error),
                    Ok(()) => Ok(()),
                }
            },
            |state| {
                let snapshot = if tracks_sensitive_value.get() && !sensitive_tracking_resolved.get()
                {
                    state.committed_sensitive_post_action_snapshot(text, request)
                } else {
                    state.committed_post_action_snapshot(request)
                };
                if let Some(error) = post_write_error.borrow_mut().take() {
                    let completed = if write_completed.get() {
                        "the text write completed"
                    } else if write_partially_completed.get() {
                        "the text write may have partially completed"
                    } else {
                        "the text write reached an unknown state"
                    };
                    partial_completion_result(completed, &error.to_string(), snapshot)
                } else {
                    snapshot
                }
            },
        )
    }

    fn select_option(
        &mut self,
        target: &str,
        values: &[String],
        request: &ActorRequest,
    ) -> TextResult {
        let selector = format!(r#"[data-mcp-ref="{target}"]"#);
        self.dispatch_ref_action(
            target,
            |state| {
                let remaining = Self::remaining(request)?;
                state
                    .ensure_page(request)?
                    .select_options_by_value_or_label_with_options_and_cancel(
                        &selector,
                        values,
                        ActionOptions::timeout(Self::engine_timeout(remaining)),
                        Some(&request.cancellation.engine),
                    )
                    .map_err(|error| {
                        state.operation_error(
                            &format!("select option failed for {target}"),
                            error,
                            &request.cancellation,
                            request.timeout_ms,
                        )
                    })?;
                Ok(())
            },
            |state| state.snapshot(request),
        )
    }

    fn fill_form(&mut self, fields: &[FillField], request: &ActorRequest) -> TextResult {
        let mut completed_fields = Vec::with_capacity(fields.len());
        for field in fields {
            // Defence in depth, and deliberately not distinguishable by a test.
            // Once the request is interrupted the engine cancel token is already
            // tripped, so the next field's `*_with_cancel` call short-circuits
            // and writes nothing even without this check -- reverting it leaves
            // every observable identical. It stays because that short-circuit is
            // a property of the core crate rather than of this loop, and because
            // without it an interrupted request still reaches `ensure_page`,
            // which may do work on behalf of a request that is already over.
            let reason = request.cancellation.reason();
            if reason != CancellationReason::Active {
                return Err(Self::fill_form_interruption(
                    field,
                    &completed_fields,
                    reason,
                    request,
                ));
            }
            if !self.current_refs.contains(&field.target) {
                let error = BrowserError::Message(format!(
                    "Field {:?} failed: unknown or stale ref {}; call browser_snapshot and use its latest refs",
                    field.name, field.target
                ));
                return self.fill_form_field_failure(
                    field,
                    &completed_fields,
                    completed_fields.len(),
                    fields.len(),
                    error,
                    request,
                );
            }
            let selector = format!(r#"[data-mcp-ref="{}"]"#, field.target);
            let remaining = match Self::remaining(request) {
                Ok(remaining) => remaining,
                Err(error) => {
                    let error = Self::fill_form_pre_dispatch_error(field, error);
                    return self.fill_form_field_failure(
                        field,
                        &completed_fields,
                        completed_fields.len(),
                        fields.len(),
                        error,
                        request,
                    );
                }
            };
            let options = ActionOptions::timeout(Self::engine_timeout(remaining));
            let page = match self.ensure_page(request) {
                Ok(page) => page.clone(),
                Err(error) => {
                    let error = Self::fill_form_pre_dispatch_error(field, error);
                    return self.fill_form_field_failure(
                        field,
                        &completed_fields,
                        completed_fields.len(),
                        fields.len(),
                        error,
                        request,
                    );
                }
            };
            let tracks_sensitive_value =
                if matches!(field.kind, FillFieldKind::Textbox | FillFieldKind::Slider) {
                    match self.begin_sensitive_snapshot_tracking(&selector, &field.target, request)
                    {
                        Ok(tracks_sensitive_value) => tracks_sensitive_value,
                        Err(error) => {
                            let error = Self::fill_form_pre_dispatch_error(field, error);
                            return self.fill_form_field_failure(
                                field,
                                &completed_fields,
                                completed_fields.len(),
                                fields.len(),
                                error,
                                request,
                            );
                        }
                    }
                } else {
                    false
                };
            // Engine errors become `Message` here, so the only `Timeout` that can
            // reach the arbitration below is a budget check propagating its own
            // variant -- which is exactly the case that arbitration exists for.
            let result: Result<(), BrowserError> = match field.kind {
                FillFieldKind::Textbox | FillFieldKind::Slider => page
                    .fill_with_cancel(
                        &selector,
                        &field.value,
                        options,
                        Some(&request.cancellation.engine),
                    )
                    .map(|_| ())
                    .map_err(|error| BrowserError::Message(error.to_string())),
                FillFieldKind::Checkbox => match field.value.as_str() {
                    "true" => page
                        .check_with_cancel(&selector, options, Some(&request.cancellation.engine))
                        .map_err(|error| BrowserError::Message(error.to_string())),
                    "false" => page
                        .uncheck_with_cancel(&selector, options, Some(&request.cancellation.engine))
                        .map_err(|error| BrowserError::Message(error.to_string())),
                    _ => Err(BrowserError::Message(
                        "checkbox value must be 'true' or 'false'".to_owned(),
                    )),
                },
                FillFieldKind::Radio => {
                    if field.value != "true" {
                        let detail = if field.value == "false" {
                            "unchecking a radio is not supported"
                        } else {
                            "radio value must be 'true'"
                        };
                        Err(BrowserError::Message(detail.to_owned()))
                    } else {
                        page.check_with_cancel(
                            &selector,
                            options,
                            Some(&request.cancellation.engine),
                        )
                        .map_err(|error| BrowserError::Message(error.to_string()))
                    }
                }
                FillFieldKind::Combobox => {
                    let values = [field.value.clone()];
                    page.select_options_by_value_or_label_with_options_and_cancel(
                        &selector,
                        &values,
                        options,
                        Some(&request.cancellation.engine),
                    )
                    .map(|_| ())
                    .map_err(|error| BrowserError::Message(error.to_string()))
                }
            };
            if let Err(error) = result {
                if tracks_sensitive_value {
                    let write_error =
                        BrowserError::Message(format!("Field {:?} failed: {error}", field.name));
                    // Resolve before reporting, on every exit below. The taint
                    // this field's tracker installed outlives the call, and the
                    // interruption exits return without taking a snapshot, so
                    // nothing else would clear it before the next one.
                    //
                    // Once resolution says a secret reached the page, that fact
                    // outranks the interruption report: the masked snapshot is
                    // the only artifact that carries it, an interruption carries
                    // no snapshot at all, and `complete` preserves a successful
                    // `FillForm` result under cancellation precisely so these
                    // exits survive. Only `Unchanged` -- nothing written, nothing
                    // to mask -- defers to the arbitration.
                    match self.resolve_sensitive_snapshot_tracking(&field.value, request) {
                        Ok(SensitiveWriteProgress::Complete) => {
                            let completed_count = completed_fields.len() + 1;
                            return self.fill_form_failure(
                                completed_count,
                                fields.len(),
                                write_error,
                                request,
                            );
                        }
                        Ok(SensitiveWriteProgress::Partial) => {
                            self.current_refs.clear();
                            let snapshot = self.committed_post_action_snapshot(request);
                            return partial_completion_result(
                                &format!(
                                    "{} of {} form fields completed; field {:?} \
                                     was partially written",
                                    completed_fields.len(),
                                    fields.len(),
                                    field.name
                                ),
                                &write_error.to_string(),
                                snapshot,
                            );
                        }
                        Ok(SensitiveWriteProgress::Unchanged) => {
                            return self.fill_form_field_failure(
                                field,
                                &completed_fields,
                                completed_fields.len(),
                                fields.len(),
                                write_error,
                                request,
                            );
                        }
                        Err(_) => {
                            self.current_refs.clear();
                            let snapshot = self
                                .committed_sensitive_post_action_snapshot(&field.value, request);
                            return partial_completion_result(
                                &format!(
                                    "{} of {} form fields completed; field {:?} \
                                     may have been partially written",
                                    completed_fields.len(),
                                    fields.len(),
                                    field.name
                                ),
                                &write_error.to_string(),
                                snapshot,
                            );
                        }
                    }
                }
                return self.fill_form_field_failure(
                    field,
                    &completed_fields,
                    completed_fields.len(),
                    fields.len(),
                    BrowserError::Message(format!("Field {:?} failed: {error}", field.name)),
                    request,
                );
            }
            completed_fields.push(field.name.as_str());
            if tracks_sensitive_value
                && self
                    .resolve_sensitive_snapshot_tracking(&field.value, request)
                    .is_err()
            {
                self.current_refs.clear();
                let snapshot = self.committed_sensitive_post_action_snapshot(&field.value, request);
                if completed_fields.len() == fields.len() {
                    return snapshot;
                }
                return partial_completion_result(
                    &format!(
                        "{} of {} form fields were written",
                        completed_fields.len(),
                        fields.len()
                    ),
                    "sensitive redaction resolution failed",
                    snapshot,
                );
            }
        }
        self.current_refs.clear();
        self.committed_post_action_snapshot(request)
    }

    /// Keep a pre-dispatch failure's variant when it is the request deadline
    /// arriving as itself, so the arbitration can still recognise it; otherwise
    /// name the stage, which is the only thing that distinguishes a setup
    /// failure from a failed write in the message.
    fn fill_form_pre_dispatch_error(field: &FillField, error: BrowserError) -> BrowserError {
        if matches!(error, BrowserError::Timeout(_)) {
            return error;
        }
        BrowserError::Message(format!(
            "Field {:?} failed before dispatch: {error}",
            field.name
        ))
    }

    /// Decide how a failed field is reported, in one place, so the interruption
    /// arbitration and the partial-write accounting cannot drift apart.
    ///
    /// An interruption outranks a partial report: once the request is cancelled
    /// or its deadline has passed, `complete` converts this into a
    /// `Cancelled`/`Timeout` anyway, and only the detail set here survives to
    /// name the fields that landed.
    fn fill_form_field_failure(
        &mut self,
        field: &FillField,
        completed_before: &[&str],
        completed_count: usize,
        total_fields: usize,
        error: BrowserError,
        request: &ActorRequest,
    ) -> TextResult {
        let reason = request.cancellation.reason();
        if reason != CancellationReason::Active {
            return Err(Self::fill_form_interruption(
                field,
                completed_before,
                reason,
                request,
            ));
        }
        // The budget can expire without `reason()` saying so yet. `remaining()`
        // reads this thread's clock, while `CancellationReason::Deadline` is
        // published by the deadline task on the runtime thread, and nothing
        // orders the two -- so a field can observe an expired budget while
        // `reason()` still reads `Active` and the branch above declines to fire.
        // That `Timeout` arrives here as itself: every engine error is converted
        // to `Message` before reaching this point, but a budget check propagates
        // its own variant untouched. Report it as the deadline it is, or the
        // partial-fill detail is lost in exactly the case the deadline is what
        // stopped the fill.
        if matches!(error, BrowserError::Timeout(_)) {
            return Err(Self::fill_form_interruption(
                field,
                completed_before,
                CancellationReason::Deadline,
                request,
            ));
        }
        self.fill_form_failure(completed_count, total_fields, error, request)
    }

    fn fill_form_failure(
        &mut self,
        completed_fields: usize,
        total_fields: usize,
        error: BrowserError,
        request: &ActorRequest,
    ) -> TextResult {
        if completed_fields == 0 {
            return Err(error);
        }
        self.current_refs.clear();
        let snapshot = self.committed_post_action_snapshot(request);
        partial_completion_result(
            &format!("{completed_fields} of {total_fields} form fields were written"),
            &error.to_string(),
            snapshot,
        )
    }

    fn fill_form_interruption(
        field: &FillField,
        completed_fields: &[&str],
        reason: CancellationReason,
        request: &ActorRequest,
    ) -> BrowserError {
        let stopped_by = match reason {
            CancellationReason::Cancelled => "cancellation",
            CancellationReason::Deadline => "timeout",
            CancellationReason::Active => unreachable!(),
        };
        let confirmed = if completed_fields.is_empty() {
            "none".to_owned()
        } else {
            completed_fields.join(", ")
        };
        let detail = format!(
            "Partial form fill stopped by {stopped_by} while processing field {:?}; \
             fields confirmed complete before it: {confirmed}. The stopped field may also \
             have been written; reconcile it before retrying.",
            field.name
        );
        request.cancellation.set_detail(detail.clone());
        BrowserError::Message(detail)
    }

    fn hover(&mut self, target: &str, request: &ActorRequest) -> TextResult {
        let selector = format!(r#"[data-mcp-ref="{target}"]"#);
        self.dispatch_ref_action(
            target,
            |state| {
                let remaining = Self::remaining(request)?;
                state
                    .ensure_page(request)?
                    .hover_with_options_and_cancel(
                        &selector,
                        ActionOptions::timeout(Self::engine_timeout(remaining)),
                        Some(&request.cancellation.engine),
                    )
                    .map_err(|error| {
                        state.operation_error(
                            &format!("hover failed for {target}"),
                            error,
                            &request.cancellation,
                            request.timeout_ms,
                        )
                    })
            },
            // Hover dispatches committed physical pointer input; like click,
            // its post-action snapshot must survive cancellation.
            |state| state.committed_post_action_snapshot(request),
        )
    }

    fn press_key_on_page(
        &mut self,
        selector: Option<&str>,
        key: &str,
        context: &str,
        request: &ActorRequest,
    ) -> Result<(), BrowserError> {
        let remaining = Self::remaining(request)?;
        let result = self
            .ensure_page(request)?
            .press_key_with_options_and_cancel(
                selector,
                key,
                ActionOptions::timeout(Self::engine_timeout(remaining)),
                Some(&request.cancellation.engine),
            );
        result.map_err(|error| {
            self.operation_error(context, error, &request.cancellation, request.timeout_ms)
        })
    }

    fn press_key(&mut self, target: Option<&str>, key: &str, request: &ActorRequest) -> TextResult {
        if let Some(target) = target {
            let selector = format!(r#"[data-mcp-ref="{target}"]"#);
            return self.dispatch_ref_action(
                target,
                |state| {
                    state.press_key_on_page(
                        Some(&selector),
                        key,
                        &format!("key press failed for {target}"),
                        request,
                    )
                },
                // The key-down dispatch has committed, so cancellation is too
                // late for both the key-up cleanup and post-action snapshot.
                |state| state.committed_post_action_snapshot(request),
            );
        }

        self.current_refs.clear();
        self.press_key_on_page(None, key, "key press failed", request)?;
        self.committed_post_action_snapshot(request)
    }

    fn drop_data(
        &mut self,
        target: &str,
        paths: &[String],
        data: &[(String, String)],
        request: &ActorRequest,
    ) -> TextResult {
        if !self.current_refs.contains(target) {
            return Err(BrowserError::Message(format!(
                "unknown or stale ref {target}; call browser_snapshot and use its latest refs"
            )));
        }
        let files = read_drop_files(paths)?;
        let selector = format!(r#"[data-mcp-ref="{target}"]"#);
        let remaining = Self::remaining(request)?;
        self.ensure_page(request)?
            .evaluate_with_cancel(
                SYNTHETIC_DROP_JS,
                Some(&json!({
                    "selector": selector,
                    "files": files,
                    "data": data,
                })),
                ActionOptions::timeout(Self::engine_timeout(remaining)),
                Some(&request.cancellation.engine),
            )
            .map_err(|error| {
                self.operation_error(
                    &format!("drop failed for {target}"),
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                )
            })?;
        self.current_refs.clear();
        self.snapshot(request)
    }

    fn console_messages(
        &mut self,
        level: ConsoleLevel,
        all: bool,
        filename: Option<&str>,
        request: &ActorRequest,
    ) -> TextResult {
        let records = if let Some(source) = self.page_record_source.as_mut() {
            source.console_records(all, false)
        } else {
            self.ensure_page(request)?.console_records(all, false)
        }
        .map_err(|error| {
            self.operation_error(
                "console capture failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            )
        })?;
        let presentation = console_records_presentation(
            &records,
            level,
            self.features.console_dedup && filename.is_none(),
        );
        if let Some(filename) = filename {
            let artifact = write_text_output(&presentation.text, filename, "console")?;
            Ok(format!(
                "Console messages written to `{}`.",
                artifact.display()
            ))
        } else {
            Ok(presentation.text)
        }
    }

    fn network_requests(
        &mut self,
        include_static: bool,
        filter: Option<&str>,
        filename: Option<&str>,
        request: &ActorRequest,
    ) -> TextResult {
        let records = if let Some(source) = self.page_record_source.as_mut() {
            source.network_records(false, false)
        } else {
            self.ensure_page(request)?.network_records(false, false)
        };
        let filter = filter
            .map(NetworkRegex::compile)
            .transpose()
            .map_err(|message| {
                BrowserError::Message(format!("invalid network filter regex: {message}"))
            })?;
        let presentation = network_records_presentation(
            &records,
            include_static,
            filter.as_ref(),
            self.features.net_note,
            filename.is_some(),
        );
        let legacy_content = network_list_presentation(&presentation.legacy_lines, None);
        if let Some(filename) = filename {
            let artifact = write_text_output(&legacy_content, filename, "network")?;
            Ok(format!(
                "Network requests written to `{}`.",
                artifact.display()
            ))
        } else {
            let (entries, mut tail_notices): (Vec<_>, Vec<_>) = presentation
                .legacy_lines
                .iter()
                .cloned()
                .partition(|line| line.starts_with('['));
            if let Some(hidden_static) = presentation.hidden_static.filter(|count| *count > 0) {
                tail_notices.push(format!("({hidden_static} successful static requests hidden; use static:true to include them)"));
            }
            self.response_shape
                .get_or_insert_with(ResponseShape::default)
                .network = Some(NetworkStructure::List {
                entries,
                tail_notices,
            });
            Ok(network_list_presentation(
                &presentation.legacy_lines,
                presentation.hidden_static,
            ))
        }
    }

    fn network_request(
        &mut self,
        index: u64,
        part: Option<NetworkPart>,
        filename: Option<&str>,
        request: &ActorRequest,
    ) -> TextResult {
        const INLINE_BODY_BYTES: usize = 64 * 1024;
        const FILE_BODY_BYTES: usize = 20 * 1024 * 1024;

        let page = self.ensure_page(request)?.clone();
        let current = page.network_records(false, false);
        let Some(record) = current.records.iter().find(|record| record.index == index) else {
            let all = page.network_records(true, false);
            let valid = current
                .records
                .first()
                .zip(current.records.last())
                .map_or_else(
                    || "none".to_owned(),
                    |(first, last)| format!("{}-{}", first.index, last.index),
                );
            if index < current.navigation_start_index
                || all.records.iter().any(|record| record.index == index)
            {
                return Err(BrowserError::Message(format!(
                    "Request index {index} is from a previous navigation; current requests are {valid} (current navigation epoch)."
                )));
            }
            return Err(BrowserError::Message(format!(
                "Network request index {index} is unavailable in the current navigation epoch; valid range: {valid}."
            )));
        };

        let parts = part.map_or_else(
            || {
                vec![
                    NetworkPart::RequestHeaders,
                    NetworkPart::RequestBody,
                    NetworkPart::ResponseHeaders,
                    NetworkPart::ResponseBody,
                ]
            },
            |part| vec![part],
        );
        let mut rendered = Vec::new();
        let mut structured = Vec::new();
        for selected in parts {
            let (name, value, body_marker) = match selected {
                NetworkPart::RequestHeaders => (
                    "request-headers",
                    headers_json(&record.request_headers),
                    None,
                ),
                NetworkPart::RequestBody => {
                    let body = record
                        .request_body
                        .as_deref()
                        .filter(|body| !body.is_empty())
                        .unwrap_or("");
                    let max = if filename.is_some() {
                        FILE_BODY_BYTES
                    } else {
                        INLINE_BODY_BYTES
                    };
                    let value = if body.is_empty() {
                        "(empty request body)".to_owned()
                    } else {
                        bounded_network_detail_text(body, max, "request body", filename.is_none())
                    };
                    let marker = (filename.is_none() && body.len() > max).then(||
                        format!("(request body truncated to {max} bytes inline; use filename for a larger bounded body)"));
                    ("request-body", value, marker)
                }
                NetworkPart::ResponseHeaders => (
                    "response-headers",
                    if record.response_status.is_none() {
                        "(response not received)".to_owned()
                    } else {
                        headers_json(&record.response_headers)
                    },
                    None,
                ),
                NetworkPart::ResponseBody => {
                    let max_bytes = if filename.is_some() {
                        FILE_BODY_BYTES
                    } else {
                        INLINE_BODY_BYTES
                    };
                    let body = page
                        .network_response_body(index, max_bytes)
                        .map_err(|error| {
                            self.operation_error(
                                "network response body failed",
                                error,
                                &request.cancellation,
                                request.timeout_ms,
                            )
                        })?;
                    let mut actor_marker = None;
                    let value = match body {
                        NetworkBody::Text {
                            text,
                            total_bytes,
                            truncated,
                        } => {
                            let mut value = if text.is_empty() {
                                "(empty response body)".to_owned()
                            } else {
                                text
                            };
                            if truncated {
                                if filename.is_some() {
                                    value.push_str(&format!(
                                        "\n(response body truncated to {max_bytes} of {total_bytes} bytes)"
                                    ));
                                } else {
                                    let marker = format!(
                                        "(response body truncated to {max_bytes} bytes inline; use filename for a larger bounded body)"
                                    );
                                    value.push('\n');
                                    value.push_str(&marker);
                                    actor_marker = Some(marker);
                                }
                            }
                            value
                        }
                        NetworkBody::Unavailable { reason }
                            if reason == "response not received" =>
                        {
                            "(response not received)".to_owned()
                        }
                        NetworkBody::Unavailable { reason } => {
                            format!("(body unavailable: {reason})")
                        }
                    };
                    ("response-body", value, actor_marker)
                }
            };
            structured.push(NetworkSection {
                name,
                payload: value.clone(),
                body_marker,
            });
            rendered.push(format!("#### {name}\n{value}"));
        }
        let content = rendered.join("\n\n");
        if let Some(filename) = filename {
            let artifact = write_text_output(&content, filename, "network-request")?;
            Ok(format!(
                "Network request {index} written to `{}`.",
                artifact.display()
            ))
        } else {
            self.response_shape
                .get_or_insert_with(ResponseShape::default)
                .network = Some(NetworkStructure::Detail {
                sections: structured,
            });
            Ok(content)
        }
    }

    fn list_pages(
        &mut self,
        request: &ActorRequest,
    ) -> Result<Vec<ActivePageHandle>, BrowserError> {
        if let Some(mut seam) = self.page_lifecycle_seam.take() {
            let discovered = seam.discover_pages(request);
            self.page_lifecycle_seam = Some(seam);
            let discovered = discovered?;
            let discovered_targets = discovered
                .iter()
                .map(|candidate| candidate.handle.target_id())
                .collect::<HashSet<_>>();
            self.closing_targets
                .retain(|target_id| discovered_targets.contains(target_id));
            self.tab_order
                .retain(|target_id| discovered_targets.contains(target_id));
            self.tab_inventory
                .retain(|target_id, _| discovered_targets.contains(target_id));
            self.pages
                .retain(|target_id, _| discovered_targets.contains(target_id));
            for candidate in &discovered {
                self.register_page(candidate.registration.as_ref())
                    .map_err(|error| {
                        self.operation_error(
                            "console capture arm failed",
                            error,
                            &request.cancellation,
                            request.timeout_ms,
                        )
                    })?;
            }
            let mut by_target = discovered
                .into_iter()
                .map(|candidate| (candidate.handle.target_id(), candidate.handle))
                .collect::<HashMap<_, _>>();
            self.inventory_stale = false;
            return Ok(self
                .tab_order
                .iter()
                .filter_map(|target_id| by_target.remove(target_id))
                .collect());
        }
        let remaining = Self::remaining(request)?;
        let discovered = self
            .browser
            .as_ref()
            .ok_or_else(|| BrowserError::Message("browser is not initialized".to_owned()))?
            .pages_with_cancel(
                remaining.saturating_add(ENGINE_TIMEOUT_CUSHION),
                Some(&request.cancellation.engine),
            )
            .map_err(|error| {
                self.operation_error(
                    "tab listing failed",
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                )
            })?;
        let browser_targets = discovered
            .iter()
            .map(Page::target_id)
            .collect::<HashSet<_>>();
        self.closing_targets
            .retain(|target_id| browser_targets.contains(target_id));
        let discovered = discovered
            .into_iter()
            .filter(|page| !self.closing_targets.contains(&page.target_id()))
            .collect::<Vec<_>>();
        let discovered_targets = discovered
            .iter()
            .map(Page::target_id)
            .collect::<HashSet<_>>();
        self.tab_order
            .retain(|target_id| discovered_targets.contains(target_id));
        self.tab_inventory
            .retain(|target_id, _| discovered_targets.contains(target_id));
        self.pages
            .retain(|target_id, _| discovered_targets.contains(target_id));
        for page in &discovered {
            self.register_page(page).map_err(|error| {
                self.operation_error(
                    "console capture arm failed",
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                )
            })?;
        }
        let mut by_target = discovered
            .into_iter()
            .map(|page| (page.target_id(), ActivePageHandle::live(page)))
            .collect::<HashMap<_, _>>();
        self.inventory_stale = false;
        Ok(self
            .tab_order
            .iter()
            .filter_map(|target_id| by_target.remove(target_id))
            .collect())
    }

    fn render_tabs(
        &mut self,
        pages: &[ActivePageHandle],
        selected_index: Option<usize>,
        request: &ActorRequest,
    ) -> (String, TabsStructure) {
        self.poll_events();
        let active_target = self.page.as_ref().map(ActivePageHandle::target_id);
        let mut lines = Vec::new();
        let mut entries = Vec::new();
        let mut exact_selected_url = None;
        for (index, page) in pages.iter().enumerate() {
            let target_id = page.target_id();
            let pending = self.pages.get(&target_id).is_some_and(|runtime| {
                runtime.pending_dialog.is_some() || runtime.pending_file_chooser.is_some()
            });
            let cached = self
                .pages
                .get(&target_id)
                .and_then(|runtime| runtime.title.clone());
            let title = if pending {
                cached.unwrap_or_else(|| "(modal pending)".to_owned())
            } else {
                let remaining = Self::remaining(request).ok();
                let title = remaining
                    .and_then(|remaining| {
                        page.title(ActionOptions::timeout(Self::engine_timeout(remaining)))
                            .ok()
                    })
                    .or(cached)
                    .unwrap_or_else(|| "(unavailable)".to_owned());
                if let Some(runtime) = self.pages.get_mut(&target_id) {
                    runtime.title = Some(title.clone());
                }
                title
            };
            let marker = (Some(target_id) == active_target)
                .then_some(" (active)")
                .unwrap_or("");
            let title = title.replace(['\r', '\n'], " ");
            let raw_url = page.url();
            let (url, selected_exact_url) = tab_url_values(index, selected_index, &raw_url);
            lines.push(format!("- {index}: {title} — {url}{marker}"));
            entries.push(TabEntry {
                index,
                title,
                url,
                active: !marker.is_empty(),
            });
            if let Some(selected_exact_url) = selected_exact_url {
                exact_selected_url = Some(selected_exact_url);
            }
        }
        let active_index = pages
            .iter()
            .position(|page| Some(page.target_id()) == active_target);
        (
            format!("### Tabs\n{}", lines.join("\n")),
            TabsStructure {
                entries,
                active_index,
                selected_exact_url: exact_selected_url,
            },
        )
    }

    fn tabs(
        &mut self,
        action: TabAction,
        index: Option<usize>,
        url: Option<&str>,
        request: &ActorRequest,
    ) -> TextResult {
        self.ensure_page(request)?;
        let mut pages = self.list_pages(request)?;
        let mut snapshot = None;
        match action {
            TabAction::List => {}
            TabAction::New => {
                let page = self
                    .browser
                    .as_ref()
                    .expect("browser initialized")
                    .new_page_with_cancel(Some(&request.cancellation.engine))
                    .map_err(|error| {
                        self.operation_error(
                            "new tab failed",
                            error,
                            &request.cancellation,
                            request.timeout_ms,
                        )
                    })?;
                self.register_page(&page).map_err(|error| {
                    self.operation_error(
                        "console capture arm failed",
                        error,
                        &request.cancellation,
                        request.timeout_ms,
                    )
                })?;
                self.page = Some(ActivePageHandle::live(page.clone()));
                if let Some(url) = url {
                    let remaining = Self::remaining(request)?;
                    let target_id = page.target_id();
                    self.begin_observed_navigation(&target_id, Some(url.to_owned()));
                    let observation = match page.goto_with_cancel_observed(
                        url,
                        GotoOptions::default()
                            .wait_until("load")
                            .timeout(Self::engine_timeout(remaining)),
                        Some(&request.cancellation.engine),
                    ) {
                        Ok(observation) => observation,
                        Err(error) => {
                            self.cancel_observed_navigation(&target_id);
                            return Err(self.operation_error(
                                "new tab navigation failed",
                                error,
                                &request.cancellation,
                                request.timeout_ms,
                            ));
                        }
                    };
                    self.record_observed_navigation(&target_id, page.url(), &observation, false);
                }
                self.current_refs.clear();
                snapshot = Some(self.snapshot(request)?);
                pages = self.list_pages(request)?;
            }
            TabAction::Select => {
                let index = index.ok_or_else(|| {
                    BrowserError::Message("index is required for tab select".to_owned())
                })?;
                let page = pages.get(index).cloned().ok_or_else(|| {
                    BrowserError::Message(format!(
                        "Invalid tab index {index}; expected 0 through {}",
                        pages.len().saturating_sub(1)
                    ))
                })?;
                self.page = Some(page);
                self.current_refs.clear();
                snapshot = Some(self.snapshot(request)?);
            }
            TabAction::Close => {
                let active_target = self
                    .page
                    .as_ref()
                    .map(ActivePageHandle::target_id)
                    .ok_or_else(|| BrowserError::Message("no active tab".to_owned()))?;
                let closing_index = index.unwrap_or_else(|| {
                    pages
                        .iter()
                        .position(|page| page.target_id() == active_target)
                        .unwrap_or(0)
                });
                let closing = pages.get(closing_index).cloned().ok_or_else(|| {
                    BrowserError::Message(format!(
                        "Invalid tab index {closing_index}; expected 0 through {}",
                        pages.len().saturating_sub(1)
                    ))
                })?;
                let target_id = closing.target_id();
                self.poll_events();
                if self.pages.get(&target_id).is_some_and(|runtime| {
                    runtime.pending_dialog.is_some() || runtime.pending_file_chooser.is_some()
                }) {
                    return Err(BrowserError::Message(format!(
                        "Tab {closing_index} has a pending modal; handle it before closing."
                    )));
                }
                if let Some(mut seam) = self.page_lifecycle_seam.take() {
                    let closed = seam.close_page(&closing, request);
                    self.page_lifecycle_seam = Some(seam);
                    closed?;
                } else {
                    closing.close(CloseOptions::default()).map_err(|error| {
                        self.operation_error(
                            "tab close failed",
                            error,
                            &request.cancellation,
                            request.timeout_ms,
                        )
                    })?;
                }
                self.retire_closed_target(&target_id);
                pages = self.list_pages(request)?;
                if pages.is_empty() {
                    let candidate = if let Some(mut seam) = self.page_lifecycle_seam.take() {
                        let candidate = seam.new_page(request);
                        self.page_lifecycle_seam = Some(seam);
                        candidate?
                    } else {
                        let page = self
                            .browser
                            .as_ref()
                            .expect("browser initialized")
                            .new_page_with_cancel(Some(&request.cancellation.engine))
                            .map_err(|error| {
                                self.operation_error(
                                    "replacement tab failed",
                                    error,
                                    &request.cancellation,
                                    request.timeout_ms,
                                )
                            })?;
                        PageCandidate {
                            registration: Box::new(page.clone()),
                            handle: ActivePageHandle::live(page),
                        }
                    };
                    let handle = candidate.handle.clone();
                    self.install_active_page(candidate.registration.as_ref(), handle.clone(), None)
                        .map_err(|error| {
                            self.operation_error(
                                "console capture arm failed",
                                error,
                                &request.cancellation,
                                request.timeout_ms,
                            )
                        })?;
                    pages.push(handle);
                }
                if target_id == active_target && self.page.is_none() {
                    self.page = Some(pages[closing_index.min(pages.len() - 1)].clone());
                }
                self.current_refs.clear();
                snapshot = Some(self.snapshot(request)?);
            }
        }
        let selected_index = matches!(action, TabAction::Select)
            .then_some(index)
            .flatten();
        let (tabs, tabs_structure) = self.render_tabs(&pages, selected_index, request);
        self.response_shape
            .get_or_insert_with(ResponseShape::default)
            .tabs = Some(tabs_structure);
        Ok(snapshot.map_or(tabs.clone(), |snapshot| {
            format!("{tabs}\n\n### Snapshot\n{snapshot}")
        }))
    }

    fn handle_dialog(
        &mut self,
        accept: bool,
        prompt_text: Option<&str>,
        request: &ActorRequest,
    ) -> TextResult {
        self.poll_events();
        let active_target = self.page.as_ref().map(ActivePageHandle::target_id);
        let pending_target = active_target
            .filter(|target_id| {
                self.pages
                    .get(target_id)
                    .is_some_and(|runtime| runtime.pending_dialog.is_some())
            })
            .or_else(|| {
                self.tab_order.iter().find_map(|target_id| {
                    self.pages
                        .get(target_id)
                        .is_some_and(|runtime| runtime.pending_dialog.is_some())
                        .then_some(target_id.clone())
                })
            })
            .ok_or_else(|| BrowserError::Message("no dialog is pending".to_owned()))?;
        if !accept && prompt_text.is_some() {
            return Err(BrowserError::Message(
                "promptText cannot be honored when dismissing a dialog".to_owned(),
            ));
        }
        let pending = self
            .pages
            .get_mut(&pending_target)
            .and_then(|runtime| runtime.pending_dialog.take())
            .expect("pending dialog disappeared");
        let result = if accept {
            pending.dialog.accept(prompt_text)
        } else {
            pending.dialog.dismiss()
        };
        result.map_err(|error| {
            self.operation_error(
                "dialog handling failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            )
        })?;
        let action = if accept { "Accepted" } else { "Dismissed" };
        Ok(format!("{action} the pending dialog."))
    }

    fn file_upload(&mut self, paths: &[String], request: &ActorRequest) -> TextResult {
        self.poll_events();
        let active_target = self.page.as_ref().map(ActivePageHandle::target_id);
        let pending_target = active_target
            .filter(|target_id| {
                self.pages
                    .get(target_id)
                    .is_some_and(|runtime| runtime.pending_file_chooser.is_some())
            })
            .or_else(|| {
                self.tab_order.iter().find_map(|target_id| {
                    self.pages
                        .get(target_id)
                        .is_some_and(|runtime| runtime.pending_file_chooser.is_some())
                        .then_some(target_id.clone())
                })
            });
        let multiple = pending_target.as_ref().and_then(|target_id| {
            self.pages
                .get(target_id)
                .and_then(|runtime| runtime.pending_file_chooser.as_ref())
                .map(|pending| pending.multiple)
        });
        let dialog_pending = self
            .pages
            .values()
            .any(|runtime| runtime.pending_dialog.is_some());
        let multiple = validate_file_upload_preconditions(multiple, dialog_pending)?;
        let pending = self
            .pages
            .get_mut(pending_target.as_ref().expect("validated chooser target"))
            .and_then(|runtime| runtime.pending_file_chooser.take())
            .expect("pending file chooser disappeared");

        let confined = validate_file_upload_multiplicity(multiple, paths.len())
            .and_then(|()| confine_workspace_files(paths));
        let confined = match confined {
            Ok(confined) => confined,
            Err(error) => {
                let _ = pending.chooser.cancel();
                return Err(file_upload_retry_error(error));
            }
        };
        let result = if confined.is_empty() {
            pending.chooser.cancel()
        } else {
            pending.chooser.set_files(&confined)
        };
        if let Err(error) = result {
            let error = self.operation_error(
                "file chooser handling failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            );
            let _ = pending.chooser.cancel();
            return Err(file_upload_retry_error(error));
        }

        self.current_refs.clear();
        let snapshot = self.snapshot(request)?;
        let result = if confined.is_empty() {
            "Cancelled the pending file chooser.".to_owned()
        } else {
            format!(
                "Uploaded {} file(s) through the pending chooser.",
                confined.len()
            )
        };
        Ok(format!("{result}\n\n### Snapshot\n{snapshot}"))
    }

    fn wait_for(
        &mut self,
        time_seconds: Option<f64>,
        text: Option<&str>,
        text_gone: Option<&str>,
        timeout_ms: f64,
        request: &ActorRequest,
    ) -> TextResult {
        let remaining = Self::remaining(request)?;
        self.ensure_page(request)?
            .evaluate_with_cancel(
                WAIT_FOR_JS,
                Some(&json!({
                    "delayMs": time_seconds.unwrap_or_default().min(30.0) * 1000.0,
                    "text": text,
                    "textGone": text_gone,
                    "timeoutMs": timeout_ms,
                })),
                ActionOptions::timeout(Self::engine_timeout(remaining)),
                Some(&request.cancellation.engine),
            )
            .map_err(|error| {
                self.operation_error(
                    "wait failed",
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                )
            })?;
        self.current_refs.clear();
        self.snapshot(request)
    }

    fn get_text(&mut self, selector: &str, max_chars: usize, request: &ActorRequest) -> TextResult {
        let text = self
            .ensure_page(request)?
            .inner_text(selector)
            .map_err(|error| {
                self.operation_error(
                    "get text failed",
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                )
            })?
            .unwrap_or_default();
        Ok(text.chars().take(max_chars).collect())
    }

    fn evaluate(
        &mut self,
        function: &str,
        target: Option<&str>,
        request: &ActorRequest,
    ) -> TextResult {
        let remaining = Self::remaining(request)?;
        let value = if let Some(target) = target {
            if !self.current_refs.contains(target) {
                return Err(BrowserError::Message(format!(
                    "unknown or stale ref {target}; call browser_snapshot and use its latest refs"
                )));
            }
            let selector = format!(r#"[data-mcp-ref="{target}"]"#);
            self.ensure_page(request)?.evaluate_with_cancel(
                ELEMENT_EVALUATE_JS,
                Some(&json!({"selector": selector, "function": function})),
                ActionOptions::timeout(Self::engine_timeout(remaining)),
                Some(&request.cancellation.engine),
            )
        } else {
            self.ensure_page(request)?.evaluate_with_cancel(
                function,
                None,
                ActionOptions::timeout(Self::engine_timeout(remaining)),
                Some(&request.cancellation.engine),
            )
        };
        let value = value.map_err(|error| {
            self.operation_error(
                "evaluation failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            )
        })?;
        let serialized = serde_json::to_string(&value).map_err(|error| {
            BrowserError::Message(format!("evaluation serialization failed: {error}"))
        })?;
        self.current_refs.clear();
        let snapshot = self.snapshot(request)?;
        Ok(format!("{serialized}\n\n### Snapshot\n{snapshot}"))
    }

    fn take_screenshot(
        &mut self,
        full_page: bool,
        image_type: ScreenshotType,
        request: &ActorRequest,
    ) -> Result<BrowserOutput, BrowserError> {
        let remaining = Self::remaining(request)?;
        let result = self.ensure_page(request)?.screenshot_with_cancel(
            ScreenshotOptions {
                timeout: Some(Self::engine_timeout(remaining)),
                full_page: Some(full_page),
                image_type: Some(image_type.engine_name().to_owned()),
                ..ScreenshotOptions::default()
            },
            Some(&request.cancellation.engine),
        );
        let bytes = result.map_err(|error| {
            self.operation_error(
                "screenshot failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            )
        })?;
        Ok(BrowserOutput::Image {
            bytes,
            mime: image_type.mime(),
            extension: image_type.engine_name(),
        })
    }

    fn run(&mut self, request: &ActorRequest) -> BrowserResult {
        self.response_shape = None;
        if !matches!(
            request.op,
            BrowserOp::HandleDialog { .. }
                | BrowserOp::FileUpload(_)
                | BrowserOp::ConsoleMessages { .. }
                | BrowserOp::NetworkRequests { .. }
                | BrowserOp::NetworkRequest { .. }
                | BrowserOp::Tabs { .. }
                | BrowserOp::Close
        ) && self.has_pending_modal()
        {
            let text = self.modal_response(
                &format!(
                    "{} deferred until the pending modal is handled.",
                    browser_op_name(&request.op)
                ),
                request,
            );
            let output = match self.response_shape.take() {
                Some(shape) => BrowserOutput::ShapedText { text, shape },
                None => BrowserOutput::Text(text),
            };
            return Ok(self.add_page_digest(output, request));
        }
        let result = match &request.op {
            BrowserOp::Navigate(url) => self.navigate(url, request).map(BrowserOutput::Text),
            BrowserOp::NavigateBack => self.navigate_back(request).map(BrowserOutput::Text),
            BrowserOp::NavigateForward => self.navigate_forward(request).map(BrowserOutput::Text),
            BrowserOp::Reload => self.reload(request).map(BrowserOutput::Text),
            BrowserOp::Resize { width, height } => self
                .resize(*width, *height, request)
                .map(BrowserOutput::Text),
            BrowserOp::Snapshot {
                target,
                depth,
                boxes,
            } => self
                .targeted_snapshot(target.as_deref(), *depth, *boxes, request)
                .map(BrowserOutput::Text),
            BrowserOp::Find { text, regex } => self
                .find(text.as_deref(), regex.as_ref(), request)
                .map(BrowserOutput::Text),
            BrowserOp::Click {
                target,
                double_click,
            } => self
                .click(target, *double_click, request)
                .map(BrowserOutput::Text),
            BrowserOp::ScrollTarget(target) => {
                self.scroll_target(target, request).map(BrowserOutput::Text)
            }
            BrowserOp::ScrollViewport(delta_y) => self
                .scroll_viewport(*delta_y, request)
                .map(BrowserOutput::Text),
            BrowserOp::Type {
                target,
                text,
                submit,
                slowly,
                clear,
            } => self
                .type_text(target, text, *submit, *slowly, *clear, request)
                .map(BrowserOutput::Text),
            BrowserOp::SelectOption { target, values } => self
                .select_option(target, values, request)
                .map(BrowserOutput::Text),
            BrowserOp::FillForm(fields) => self.fill_form(fields, request).map(BrowserOutput::Text),
            BrowserOp::Hover(target) => self.hover(target, request).map(BrowserOutput::Text),
            BrowserOp::PressKey { target, key } => self
                .press_key(target.as_deref(), key, request)
                .map(BrowserOutput::Text),
            BrowserOp::Drag {
                start_target,
                end_target,
                start_element,
                end_element,
            } => self
                .drag(
                    start_target,
                    end_target,
                    start_element.as_deref(),
                    end_element.as_deref(),
                    request,
                )
                .map(BrowserOutput::Text),
            BrowserOp::Drop {
                target,
                paths,
                data,
            } => self
                .drop_data(target, paths, data, request)
                .map(BrowserOutput::Text),
            BrowserOp::ConsoleMessages {
                level,
                all,
                filename,
            } => self
                .console_messages(*level, *all, filename.as_deref(), request)
                .map(BrowserOutput::Text),
            BrowserOp::NetworkRequests {
                include_static,
                filter,
                filename,
            } => self
                .network_requests(
                    *include_static,
                    filter.as_deref(),
                    filename.as_deref(),
                    request,
                )
                .map(BrowserOutput::Text),
            BrowserOp::NetworkRequest {
                index,
                part,
                filename,
            } => self
                .network_request(*index, *part, filename.as_deref(), request)
                .map(BrowserOutput::Text),
            BrowserOp::Tabs { action, index, url } => self
                .tabs(*action, *index, url.as_deref(), request)
                .map(BrowserOutput::Text),
            BrowserOp::HandleDialog {
                accept,
                prompt_text,
            } => self
                .handle_dialog(*accept, prompt_text.as_deref(), request)
                .map(BrowserOutput::Text),
            BrowserOp::FileUpload(paths) => {
                self.file_upload(paths, request).map(BrowserOutput::Text)
            }
            BrowserOp::WaitFor {
                time_seconds,
                text,
                text_gone,
                timeout_ms,
            } => self
                .wait_for(
                    *time_seconds,
                    text.as_deref(),
                    text_gone.as_deref(),
                    *timeout_ms,
                    request,
                )
                .map(BrowserOutput::Text),
            BrowserOp::GetText {
                selector,
                max_chars,
            } => self
                .get_text(selector, *max_chars, request)
                .map(BrowserOutput::Text),
            BrowserOp::Evaluate { function, target } => self
                .evaluate(function, target.as_deref(), request)
                .map(BrowserOutput::Text),
            BrowserOp::TakeScreenshot {
                full_page,
                image_type,
            } => self.take_screenshot(*full_page, *image_type, request),
            BrowserOp::Close => {
                let had_browser = self.browser.is_some();
                self.close();
                Ok(BrowserOutput::Text(if had_browser {
                    "Browser closed.".to_owned()
                } else {
                    "No browser session was open.".to_owned()
                }))
            }
        };
        let output = result.map(|output| match (output, self.response_shape.take()) {
            (BrowserOutput::Text(text), Some(mut shape)) => {
                if let Some(snapshot) = shape.snapshot.as_ref() {
                    let suffix = format!("\n\n### Snapshot\n{}", snapshot.legacy);
                    shape.result_prefix = text.strip_suffix(&suffix).map(ToOwned::to_owned);
                }
                BrowserOutput::ShapedText { text, shape }
            }
            (output, _) => output,
        })?;
        Ok(self.add_page_digest(output, request))
    }

    fn close(&mut self) {
        self.current_refs.clear();
        self.pages.clear();
        self.tab_order.clear();
        self.closing_targets.clear();
        if let Some(page) = self.page.take()
            && !self.remote
            && let Err(error) = page.close(Default::default())
        {
            eprintln!("browser actor: page close failed: {error}");
        }
        if let Some(browser) = self.browser.take()
            && let Err(error) = browser.close()
        {
            eprintln!("browser actor: browser close failed: {error}");
        }
    }
}

/// Degrade a failed post-action observation into a successful response.
///
/// A committed physical action has already changed the page by the time its
/// post-action snapshot runs, so reporting the snapshot's error as the action's
/// error would tell the caller a key press or click failed when the page has
/// already processed it. The caller learns the state is unavailable instead,
/// and `dispatch_ref_action` has already cleared `current_refs`, so the stale
/// refs cannot be reused -- the response fails closed on refs while staying
/// truthful about the action.
fn committed_snapshot_result(snapshot: TextResult) -> TextResult {
    match snapshot {
        Ok(text) => Ok(text),
        Err(error) => Ok(format!(
            "Action completed, but the page state could not be captured: {error}\n\
             Call browser_snapshot for the current page state."
        )),
    }
}

fn partial_completion_result(
    completed: &str,
    later_failure: &str,
    snapshot: TextResult,
) -> TextResult {
    let snapshot = snapshot?;
    Ok(format!(
        "Action partially completed: {completed}. Later step failed: {later_failure}\n\n\
         ### Snapshot\n{snapshot}"
    ))
}

fn render_drag_result(start_display: &str, end_display: &str, snapshot: &str) -> String {
    format!("### Result\nDragged {start_display} to {end_display}.\n\n### Snapshot\n{snapshot}")
}

fn tab_url_values(
    index: usize,
    selected_index: Option<usize>,
    raw_url: &str,
) -> (String, Option<String>) {
    (
        raw_url.replace(['\r', '\n'], " "),
        (Some(index) == selected_index).then(|| raw_url.to_owned()),
    )
}

fn browser_op_name(op: &BrowserOp) -> &'static str {
    match op {
        BrowserOp::Navigate(_) => "browser_navigate",
        BrowserOp::NavigateBack => "browser_navigate_back",
        BrowserOp::NavigateForward => "browser_navigate_forward",
        BrowserOp::Reload => "browser_reload",
        BrowserOp::Resize { .. } => "browser_resize",
        BrowserOp::Snapshot { .. } => "browser_snapshot",
        BrowserOp::Find { .. } => "browser_find",
        BrowserOp::Click { .. } => "browser_click",
        BrowserOp::ScrollTarget(_) | BrowserOp::ScrollViewport(_) => "browser_scroll",
        BrowserOp::Type { .. } => "browser_type",
        BrowserOp::SelectOption { .. } => "browser_select_option",
        BrowserOp::FillForm(_) => "browser_fill_form",
        BrowserOp::Hover(_) => "browser_hover",
        BrowserOp::PressKey { .. } => "browser_press_key",
        BrowserOp::Drag { .. } => "browser_drag",
        BrowserOp::Drop { .. } => "browser_drop",
        BrowserOp::ConsoleMessages { .. } => "browser_console_messages",
        BrowserOp::NetworkRequests { .. } => "browser_network_requests",
        BrowserOp::NetworkRequest { .. } => "browser_network_request",
        BrowserOp::Tabs { .. } => "browser_tabs",
        BrowserOp::HandleDialog { .. } => "browser_handle_dialog",
        BrowserOp::FileUpload(_) => "browser_file_upload",
        BrowserOp::WaitFor { .. } => "browser_wait_for",
        BrowserOp::GetText { .. } => "browser_get_text",
        BrowserOp::Evaluate { .. } => "browser_evaluate",
        BrowserOp::TakeScreenshot { .. } => "browser_take_screenshot",
        BrowserOp::Close => "browser_close",
    }
}

fn validate_file_upload_preconditions(
    chooser_multiple: Option<bool>,
    dialog_pending: bool,
) -> Result<bool, BrowserError> {
    let multiple = chooser_multiple
        .ok_or_else(|| BrowserError::Message("no file chooser is pending".to_owned()))?;
    if dialog_pending {
        return Err(BrowserError::Message(
            "a dialog is pending; handle it before the file chooser".to_owned(),
        ));
    }
    Ok(multiple)
}

fn validate_file_upload_multiplicity(
    multiple: bool,
    path_count: usize,
) -> Result<(), BrowserError> {
    if path_count > 1 && !multiple {
        return Err(BrowserError::Message(
            "the pending file chooser accepts only one file".to_owned(),
        ));
    }
    Ok(())
}

fn file_upload_retry_error(error: BrowserError) -> BrowserError {
    BrowserError::Message(format!(
        "{error} Retry by clicking the same file input again."
    ))
}

fn render_find_matches(lines: &[String], matching_indices: &[usize]) -> (String, FindStructure) {
    const LIMIT: usize = 20;
    if matching_indices.is_empty() {
        return (
            "No matches in the current snapshot outline.".to_owned(),
            FindStructure {
                blocks: Vec::new(),
                actor_omitted: 0,
                incomplete: None,
            },
        );
    }
    let mut rendered = Vec::new();
    for (match_number, &index) in matching_indices.iter().take(LIMIT).enumerate() {
        let indent = outline_indent(&lines[index]);
        let mut ancestors = Vec::new();
        let mut wanted_indent = indent.checked_sub(2);
        for candidate in (0..index).rev() {
            let candidate_indent = outline_indent(&lines[candidate]);
            if Some(candidate_indent) == wanted_indent {
                ancestors.push(lines[candidate].trim().to_owned());
                wanted_indent = candidate_indent.checked_sub(2);
                if wanted_indent.is_none() {
                    break;
                }
            }
        }
        ancestors.reverse();
        let path = if ancestors.is_empty() {
            "(root)".to_owned()
        } else {
            ancestors.join(" > ")
        };
        let mut context = vec![index];
        for direction in [-1_isize, 1] {
            let mut candidate = index as isize + direction;
            while candidate >= 0 && (candidate as usize) < lines.len() {
                let candidate_index = candidate as usize;
                let candidate_indent = outline_indent(&lines[candidate_index]);
                if candidate_indent < indent {
                    break;
                }
                if candidate_indent == indent {
                    context.push(candidate_index);
                    break;
                }
                candidate += direction;
            }
        }
        context.sort_unstable();
        context.dedup();
        let snippets = context
            .into_iter()
            .map(|candidate| {
                let prefix = if candidate == index { "> " } else { "  " };
                format!("{prefix}{}", lines[candidate])
            })
            .collect::<Vec<_>>()
            .join("\n");
        rendered.push(format!(
            "Match {}\nPath: {path}\n{snippets}",
            match_number + 1
        ));
    }
    let truncated = matching_indices.len().saturating_sub(LIMIT);
    let mut result = rendered.join("\n\n");
    if truncated > 0 {
        result.push_str(&format!("\n\n… {truncated} additional matches truncated."));
    }
    (
        result,
        FindStructure {
            blocks: rendered,
            actor_omitted: truncated,
            incomplete: None,
        },
    )
}

fn outline_indent(line: &str) -> usize {
    line.len() - line.trim_start_matches(' ').len()
}

fn console_level_rank(level: ConsoleLevel) -> u8 {
    match level {
        ConsoleLevel::Error => 0,
        ConsoleLevel::Warning => 1,
        ConsoleLevel::Info => 2,
        ConsoleLevel::Debug => 3,
    }
}

#[derive(Debug, PartialEq, Eq)]
struct ConsoleRecordsPresentation {
    text: String,
    w4_state_writes: usize,
}

#[derive(Debug, PartialEq, Eq)]
struct ConsolePresentationKey {
    navigation_epoch: u64,
    severity: &'static str,
    normalized_text: String,
}

fn render_console_record(record: &ConsoleRecord, use_attributed_location: bool) -> String {
    let level = console_level_name(&record.message_type);
    let selected_location = if use_attributed_location {
        record
            .attributed_location
            .as_ref()
            .or(record.location.as_ref())
    } else {
        record.location.as_ref()
    };
    let location = selected_location.map_or_else(
        || "(unknown)".to_owned(),
        |location| {
            let url = if location.url.is_empty() {
                "(unknown)"
            } else {
                &location.url
            };
            format!("{url}:{}", location.line_number)
        },
    );
    format!(
        "{level} {location} {}",
        record.text.replace(['\r', '\n'], " ")
    )
}

fn console_records_presentation(
    records: &ConsoleRecords,
    level: ConsoleLevel,
    deduplicate: bool,
) -> ConsoleRecordsPresentation {
    let threshold = console_level_rank(level);
    if !deduplicate {
        let mut lines = records
            .records
            .iter()
            .filter(|record| console_message_rank(&record.message_type) <= threshold)
            .map(|record| render_console_record(record, false))
            .collect::<Vec<_>>();
        append_console_eviction_notice(&mut lines, records.evicted);
        return ConsoleRecordsPresentation {
            text: console_lines_presentation(&lines),
            w4_state_writes: 0,
        };
    }

    let mut lines = Vec::new();
    let mut state_writes = 0;
    let mut index = 0;
    while index < records.records.len() {
        let first = &records.records[index];
        let key = console_presentation_key(first);
        state_writes += 1;
        let mut end = index + 1;
        while end < records.records.len() && console_presentation_key(&records.records[end]) == key
        {
            end += 1;
            state_writes += 1;
        }
        let count = end - index;
        if console_message_rank(&first.message_type) <= threshold {
            let rendered = render_console_record(first, true);
            lines.push(if count == 1 {
                rendered
            } else {
                format!("{rendered} (repeated {count} times)")
            });
        }
        index = end;
    }
    // Adjacency is evaluated on the unfiltered structured stream. Therefore a
    // hidden-severity record and every navigation boundary separate otherwise
    // identical visible records, as required by the W4 contract.
    append_console_eviction_notice(&mut lines, records.evicted);
    ConsoleRecordsPresentation {
        text: console_lines_presentation(&lines),
        w4_state_writes: state_writes,
    }
}

fn append_console_eviction_notice(lines: &mut Vec<String>, evicted: u64) {
    if evicted > 0 {
        lines.push(format!(
            "(console ring buffer evicted {evicted} earlier matching-scope records)"
        ));
    }
}

fn console_lines_presentation(lines: &[String]) -> String {
    if lines.is_empty() {
        "(no console messages)".to_owned()
    } else {
        lines.join("\n")
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum NetworkListPresentation {
    Legacy,
    StaticNote {
        hidden_static: usize,
        state_writes: usize,
    },
}

impl NetworkListPresentation {
    fn new(net_note: bool, include_static: bool, has_filename: bool) -> Self {
        if net_note && !include_static && !has_filename {
            Self::StaticNote {
                hidden_static: 0,
                state_writes: 0,
            }
        } else {
            Self::Legacy
        }
    }

    fn hide_filtered_static(&mut self) {
        if let Self::StaticNote {
            hidden_static,
            state_writes,
        } = self
        {
            *hidden_static += 1;
            *state_writes += 1;
        }
    }

    fn hidden_static(&self) -> Option<usize> {
        match self {
            Self::Legacy => None,
            Self::StaticNote { hidden_static, .. } => Some(*hidden_static),
        }
    }

    fn state_writes(&self) -> usize {
        match self {
            Self::Legacy => 0,
            Self::StaticNote { state_writes, .. } => *state_writes,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
struct NetworkRecordsPresentation {
    legacy_lines: Vec<String>,
    hidden_static: Option<usize>,
    w4_state_writes: usize,
}

fn network_records_presentation(
    records: &NetworkRecords,
    include_static: bool,
    filter: Option<&NetworkRegex>,
    net_note: bool,
    has_filename: bool,
) -> NetworkRecordsPresentation {
    // Legacy mode has no hidden-count field, so an off switch cannot mutate W4 state.
    let mut note = NetworkListPresentation::new(net_note, include_static, has_filename);
    let mut legacy_lines = Vec::new();
    for record in &records.records {
        let successful_static = is_successful_static_record(record);
        if filter.is_some_and(|filter| !filter.is_match(&record.url)) {
            continue;
        }
        if !include_static && successful_static {
            note.hide_filtered_static();
            continue;
        }
        let status = record.response_status.map_or_else(
            || {
                if record.failure.is_some() {
                    "FAILED".to_owned()
                } else {
                    "PENDING".to_owned()
                }
            },
            |status| status.to_string(),
        );
        legacy_lines.push(format!(
            "[{}] {} {status} {} ({})",
            record.index, record.method, record.url, record.resource_type
        ));
    }
    if records.evicted > 0 {
        legacy_lines.push(format!(
            "(network ring buffer evicted {} earlier current-epoch records)",
            records.evicted
        ));
    }
    NetworkRecordsPresentation {
        legacy_lines,
        hidden_static: note.hidden_static(),
        w4_state_writes: note.state_writes(),
    }
}

fn is_successful_static_record(record: &NetworkRecord) -> bool {
    matches!(
        record.resource_type.as_str(),
        "image" | "media" | "font" | "stylesheet"
    ) && record
        .response_status
        .is_some_and(|status| (200..400).contains(&status))
}

fn network_list_presentation(lines: &[String], hidden_static: Option<usize>) -> String {
    let mut output = if lines.is_empty() {
        "(no matching network requests)".to_owned()
    } else {
        lines.join("\n")
    };
    if let Some(hidden_static) = hidden_static.filter(|count| *count > 0) {
        output.push_str(&format!(
            "\n({hidden_static} successful static requests hidden; use static:true to include them)"
        ));
    }
    output
}

fn console_presentation_key(record: &ConsoleRecord) -> ConsolePresentationKey {
    ConsolePresentationKey {
        navigation_epoch: record.navigation_epoch,
        severity: console_level_name(&record.message_type),
        normalized_text: normalize_console_text(&record.text),
    }
}

fn normalize_console_text(text: &str) -> String {
    // W4 normalization is deliberately limited to ASCII space and tab: trim
    // them at the edges and collapse interior runs to one ASCII space. Other
    // whitespace (including NBSP, em-space, and line breaks) remains distinct.
    let mut normalized = String::with_capacity(text.len());
    let mut pending_ascii_space = false;
    for character in text.chars() {
        if matches!(character, ' ' | '\t') {
            if !normalized.is_empty() {
                pending_ascii_space = true;
            }
        } else {
            if pending_ascii_space {
                normalized.push(' ');
                pending_ascii_space = false;
            }
            normalized.push(character);
        }
    }
    normalized
}

fn console_message_rank(message_type: &str) -> u8 {
    match message_type.to_ascii_lowercase().as_str() {
        "error" => 0,
        "warning" | "warn" => 1,
        "debug" => 3,
        _ => 2,
    }
}

fn console_level_name(message_type: &str) -> &'static str {
    match console_message_rank(message_type) {
        0 => "ERROR",
        1 => "WARNING",
        3 => "DEBUG",
        _ => "INFO",
    }
}

fn headers_json(headers: &[(String, String)]) -> String {
    let headers = headers
        .iter()
        .map(|(name, value)| (name.clone(), Value::String(value.clone())))
        .collect::<serde_json::Map<_, _>>();
    serde_json::to_string_pretty(&Value::Object(headers))
        .expect("string-only network headers must serialize")
}

fn bounded_network_detail_text(text: &str, max_bytes: usize, label: &str, inline: bool) -> String {
    let bytes = text.as_bytes();
    if bytes.len() <= max_bytes {
        return text.to_owned();
    }
    let mut rendered = String::from_utf8_lossy(&bytes[..max_bytes]).into_owned();
    if inline {
        rendered.push_str(&format!(
            "\n({label} truncated to {max_bytes} bytes inline; use filename for a larger bounded body)"
        ));
    } else {
        rendered.push_str(&format!(
            "\n({label} truncated to {max_bytes} of {} bytes)",
            bytes.len()
        ));
    }
    rendered
}

#[derive(Clone)]
enum NetworkRegexAtom {
    Literal(char),
    Any,
    Digit,
    Word,
    Space,
    Class {
        negated: bool,
        ranges: Vec<(char, char)>,
    },
}

#[derive(Clone, Copy)]
enum NetworkRegexQuantifier {
    One,
    ZeroOrOne,
    ZeroOrMore,
    OneOrMore,
}

#[derive(Clone)]
struct NetworkRegexToken {
    atom: NetworkRegexAtom,
    quantifier: NetworkRegexQuantifier,
}

struct NetworkRegexBranch {
    tokens: Vec<NetworkRegexToken>,
    start_anchor: bool,
    end_anchor: bool,
}

struct NetworkRegex {
    branches: Vec<NetworkRegexBranch>,
    case_insensitive: bool,
}

impl NetworkRegex {
    fn compile(pattern: &str) -> Result<Self, String> {
        let (pattern, case_insensitive) = pattern
            .strip_prefix("(?i)")
            .map_or((pattern, false), |pattern| (pattern, true));
        let pattern = if case_insensitive {
            pattern.to_ascii_lowercase()
        } else {
            pattern.to_owned()
        };
        let branches = split_network_regex_alternatives(&pattern)?
            .into_iter()
            .map(|branch| parse_network_regex_branch(&branch))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Self {
            branches,
            case_insensitive,
        })
    }

    fn is_match(&self, text: &str) -> bool {
        let text = if self.case_insensitive {
            text.to_ascii_lowercase()
        } else {
            text.to_owned()
        };
        self.branches
            .iter()
            .any(|branch| network_regex_branch_matches(branch, &text))
    }
}

fn split_network_regex_alternatives(pattern: &str) -> Result<Vec<String>, String> {
    let mut alternatives = Vec::new();
    let mut current = String::new();
    let mut escaped = false;
    let mut in_class = false;
    for character in pattern.chars() {
        if escaped {
            current.push(character);
            escaped = false;
            continue;
        }
        match character {
            '\\' => {
                current.push(character);
                escaped = true;
            }
            '[' => {
                in_class = true;
                current.push(character);
            }
            ']' if in_class => {
                in_class = false;
                current.push(character);
            }
            '|' if !in_class => alternatives.push(std::mem::take(&mut current)),
            '(' | ')' if !in_class => {
                return Err("groups and lookarounds are not supported".to_owned());
            }
            _ => current.push(character),
        }
    }
    if escaped {
        return Err("trailing escape".to_owned());
    }
    if in_class {
        return Err("unterminated character class".to_owned());
    }
    alternatives.push(current);
    Ok(alternatives)
}

fn parse_network_regex_branch(pattern: &str) -> Result<NetworkRegexBranch, String> {
    let mut chars = pattern.chars().peekable();
    let start_anchor = chars.next_if_eq(&'^').is_some();
    let mut tokens = Vec::new();
    let mut end_anchor = false;
    while let Some(character) = chars.next() {
        if character == '$' && chars.peek().is_none() {
            end_anchor = true;
            break;
        }
        let atom = match character {
            '.' => NetworkRegexAtom::Any,
            '\\' => match chars.next().ok_or_else(|| "trailing escape".to_owned())? {
                'd' => NetworkRegexAtom::Digit,
                'w' => NetworkRegexAtom::Word,
                's' => NetworkRegexAtom::Space,
                unsupported if unsupported.is_ascii_alphanumeric() => {
                    return Err(format!("escape \\{unsupported} is not supported"));
                }
                escaped => NetworkRegexAtom::Literal(escaped),
            },
            '[' => parse_network_regex_class(&mut chars)?,
            '*' | '+' | '?' => {
                return Err(format!("quantifier {character:?} has no preceding atom"));
            }
            '{' | '}' => {
                return Err("counted quantifiers are not supported".to_owned());
            }
            '^' | '$' => {
                return Err("anchors are only supported at branch boundaries".to_owned());
            }
            literal => NetworkRegexAtom::Literal(literal),
        };
        let quantifier = match chars.peek().copied() {
            Some('*') => {
                chars.next();
                NetworkRegexQuantifier::ZeroOrMore
            }
            Some('+') => {
                chars.next();
                NetworkRegexQuantifier::OneOrMore
            }
            Some('?') => {
                chars.next();
                NetworkRegexQuantifier::ZeroOrOne
            }
            _ => NetworkRegexQuantifier::One,
        };
        tokens.push(NetworkRegexToken { atom, quantifier });
    }
    Ok(NetworkRegexBranch {
        tokens,
        start_anchor,
        end_anchor,
    })
}

fn parse_network_regex_class(
    chars: &mut std::iter::Peekable<impl Iterator<Item = char>>,
) -> Result<NetworkRegexAtom, String> {
    let negated = chars.next_if_eq(&'^').is_some();
    let mut values = Vec::new();
    let mut closed = false;
    while let Some(character) = chars.next() {
        if character == ']' && !values.is_empty() {
            closed = true;
            break;
        }
        let character = if character == '\\' {
            let escaped = chars
                .next()
                .ok_or_else(|| "trailing escape in character class".to_owned())?;
            if escaped.is_ascii_alphanumeric() {
                return Err(format!(
                    "escape \\{escaped} in a character class is not supported"
                ));
            }
            escaped
        } else {
            character
        };
        values.push(character);
    }
    if !closed {
        return Err("unterminated or empty character class".to_owned());
    }
    let mut ranges = Vec::new();
    let mut index = 0;
    while index < values.len() {
        if index + 2 < values.len() && values[index + 1] == '-' {
            if values[index] > values[index + 2] {
                return Err("character class range is reversed".to_owned());
            }
            ranges.push((values[index], values[index + 2]));
            index += 3;
        } else {
            ranges.push((values[index], values[index]));
            index += 1;
        }
    }
    Ok(NetworkRegexAtom::Class { negated, ranges })
}

fn network_regex_branch_matches(branch: &NetworkRegexBranch, text: &str) -> bool {
    let chars = text.chars().collect::<Vec<_>>();
    let starts: Box<dyn Iterator<Item = usize>> = if branch.start_anchor {
        Box::new(std::iter::once(0))
    } else {
        Box::new(0..=chars.len())
    };
    starts.into_iter().any(|start| {
        match_network_regex_tokens(&branch.tokens, 0, &chars, start, branch.end_anchor)
    })
}

fn match_network_regex_tokens(
    tokens: &[NetworkRegexToken],
    token_index: usize,
    text: &[char],
    text_index: usize,
    end_anchor: bool,
) -> bool {
    if token_index == tokens.len() {
        return !end_anchor || text_index == text.len();
    }
    let token = &tokens[token_index];
    let matches_one = |index: usize| {
        text.get(index)
            .is_some_and(|character| network_regex_atom_matches(&token.atom, *character))
    };
    match token.quantifier {
        NetworkRegexQuantifier::One => {
            matches_one(text_index)
                && match_network_regex_tokens(
                    tokens,
                    token_index + 1,
                    text,
                    text_index + 1,
                    end_anchor,
                )
        }
        NetworkRegexQuantifier::ZeroOrOne => {
            match_network_regex_tokens(tokens, token_index + 1, text, text_index, end_anchor)
                || (matches_one(text_index)
                    && match_network_regex_tokens(
                        tokens,
                        token_index + 1,
                        text,
                        text_index + 1,
                        end_anchor,
                    ))
        }
        NetworkRegexQuantifier::ZeroOrMore | NetworkRegexQuantifier::OneOrMore => {
            let minimum = matches!(token.quantifier, NetworkRegexQuantifier::OneOrMore) as usize;
            let mut end = text_index;
            while matches_one(end) {
                end += 1;
            }
            if end.saturating_sub(text_index) < minimum {
                return false;
            }
            (text_index + minimum..=end).rev().any(|next| {
                match_network_regex_tokens(tokens, token_index + 1, text, next, end_anchor)
            })
        }
    }
}

fn network_regex_atom_matches(atom: &NetworkRegexAtom, character: char) -> bool {
    match atom {
        NetworkRegexAtom::Literal(literal) => character == *literal,
        NetworkRegexAtom::Any => character != '\n',
        NetworkRegexAtom::Digit => character.is_ascii_digit(),
        NetworkRegexAtom::Word => character.is_ascii_alphanumeric() || character == '_',
        NetworkRegexAtom::Space => character.is_whitespace(),
        NetworkRegexAtom::Class { negated, ranges } => {
            let contained = ranges
                .iter()
                .any(|(start, end)| (*start..=*end).contains(&character));
            contained != *negated
        }
    }
}

fn write_text_output(
    content: &str,
    filename: &str,
    purpose: &str,
) -> Result<PathBuf, BrowserError> {
    if filename.is_empty() {
        return Err(BrowserError::Message(format!(
            "{purpose} filename must be non-empty"
        )));
    }
    let workspace = env::var_os("RUSTWRIGHT_MCP_WORKSPACE")
        .ok_or_else(|| {
            BrowserError::Message(format!(
                "RUSTWRIGHT_MCP_WORKSPACE must be set for {purpose} file output"
            ))
        })
        .map(PathBuf::from)?;
    if !workspace.is_absolute() {
        return Err(BrowserError::Message(
            "RUSTWRIGHT_MCP_WORKSPACE must be an absolute path".to_owned(),
        ));
    }
    let workspace = workspace.canonicalize().map_err(|error| {
        BrowserError::Message(format!("RUSTWRIGHT_MCP_WORKSPACE is unavailable: {error}"))
    })?;
    let requested = Path::new(filename);
    let candidate = if requested.is_absolute() {
        requested.to_path_buf()
    } else {
        workspace.join(requested)
    };
    let file_name = candidate
        .file_name()
        .ok_or_else(|| BrowserError::Message(format!("{purpose} filename must name a file")))?;
    let parent = candidate.parent().ok_or_else(|| {
        BrowserError::Message(format!("{purpose} filename must have a parent directory"))
    })?;
    let parent = parent.canonicalize().map_err(|error| {
        BrowserError::Message(format!(
            "{purpose} output directory is unavailable: {error}"
        ))
    })?;
    if !parent.starts_with(&workspace) {
        return Err(BrowserError::Message(format!(
            "{purpose} output is confined to RUSTWRIGHT_MCP_WORKSPACE ({})",
            workspace.display()
        )));
    }
    let resolved = parent.join(file_name);
    if resolved.symlink_metadata().is_ok() {
        return Err(BrowserError::Message(format!(
            "{purpose} output file already exists"
        )));
    }
    let mut output = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&resolved)
        .map_err(|error| BrowserError::Message(format!("{purpose} output failed: {error}")))?;
    if let Err(error) = output.write_all(content.as_bytes()) {
        drop(output);
        let _ = fs::remove_file(&resolved);
        return Err(BrowserError::Message(format!(
            "{purpose} output failed: {error}"
        )));
    }
    Ok(resolved)
}

fn read_drop_files(paths: &[String]) -> Result<Vec<Value>, BrowserError> {
    let confined = confine_workspace_files(paths)?;
    let mut files = Vec::with_capacity(paths.len());
    for (requested, resolved) in paths.iter().zip(confined) {
        let bytes = fs::read(&resolved).map_err(|error| {
            BrowserError::Message(format!("file input read failed: {requested}: {error}"))
        })?;
        let name = resolved
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("upload.bin");
        files.push(json!({
            "name": name,
            "mime": mime_for_path(&resolved),
            "base64": STANDARD.encode(bytes),
        }));
    }
    Ok(files)
}

fn confine_workspace_files(paths: &[String]) -> Result<Vec<PathBuf>, BrowserError> {
    if paths.len() > MAX_FILE_INPUTS {
        return Err(BrowserError::Message(format!(
            "file paths are limited to {MAX_FILE_INPUTS} files"
        )));
    }
    paths
        .iter()
        .map(|requested| confine_workspace_file(requested))
        .collect()
}

fn confine_workspace_file(requested: &str) -> Result<PathBuf, BrowserError> {
    let workspace = env::var_os("RUSTWRIGHT_MCP_WORKSPACE")
        .ok_or_else(|| {
            BrowserError::Message("RUSTWRIGHT_MCP_WORKSPACE must be set for file inputs".to_owned())
        })
        .map(PathBuf::from)?;
    if !workspace.is_absolute() {
        return Err(BrowserError::Message(
            "RUSTWRIGHT_MCP_WORKSPACE must be an absolute path".to_owned(),
        ));
    }
    let workspace = workspace.canonicalize().map_err(|error| {
        BrowserError::Message(format!("RUSTWRIGHT_MCP_WORKSPACE is unavailable: {error}"))
    })?;
    let path = Path::new(requested);
    let candidate = if path.is_absolute() {
        path.to_path_buf()
    } else {
        workspace.join(path)
    };
    let resolved = candidate.canonicalize().map_err(|error| {
        BrowserError::Message(format!("file input is unavailable: {requested}: {error}"))
    })?;
    if !resolved.starts_with(&workspace) {
        return Err(BrowserError::Message(format!(
            "file inputs are confined to RUSTWRIGHT_MCP_WORKSPACE ({}); got {requested}",
            workspace.display()
        )));
    }
    let metadata = resolved.metadata().map_err(|error| {
        BrowserError::Message(format!("file input metadata failed: {requested}: {error}"))
    })?;
    if !metadata.is_file() {
        return Err(BrowserError::Message(format!(
            "file input is not a regular file: {requested}"
        )));
    }
    if metadata.len() > MAX_FILE_INPUT_BYTES {
        return Err(BrowserError::Message(format!(
            "file input exceeds the {MAX_FILE_INPUT_BYTES}-byte per-file cap: {requested}"
        )));
    }
    Ok(resolved)
}

fn mime_for_path(path: &Path) -> &'static str {
    match path
        .extension()
        .and_then(|extension| extension.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase()
        .as_str()
    {
        "txt" => "text/plain",
        "html" | "htm" => "text/html",
        "json" => "application/json",
        "csv" => "text/csv",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "pdf" => "application/pdf",
        _ => "application/octet-stream",
    }
}

fn actor_main(shared: Arc<ActorShared>, startup: BrowserStartup, features: FeatureConfig) {
    let mut state = BrowserState::new(startup, features);
    eprintln!("browser actor: ready");
    while let Some(request) = shared.next() {
        let result = state.run(&request);
        let result = shared.complete(&request, result);
        let _ = request.reply.send(result);
    }
    state.close();
    eprintln!("browser actor: stopped");
}

#[cfg(test)]
mod tests {
    use std::{
        io::{Read, Write},
        net::{SocketAddr, TcpListener, TcpStream},
        process::Command,
        sync::{
            OnceLock,
            atomic::{AtomicBool, AtomicU64, AtomicUsize},
            mpsc,
        },
    };

    use super::*;

    #[test]
    fn header_switch_off_allocates_no_header_state() {
        let name = "RUSTWRIGHT_MCP_HEADER";
        let previous = std::env::var_os(name);
        // SAFETY: no other test in this crate mutates this variable, and it is
        // restored before any assertion can panic.
        unsafe { std::env::set_var(name, "off") };
        let off_features = FeatureConfig::from_env();
        match previous {
            Some(value) => unsafe { std::env::set_var(name, value) },
            None => unsafe { std::env::remove_var(name) },
        }
        let off = BrowserState::new(BrowserStartup::Local, off_features);
        assert!(off.header_state.is_none());

        let mut features = FeatureConfig::default();
        features.header = true;
        let on = BrowserState::new(BrowserStartup::Local, features);
        assert!(on.header_state.is_some());
    }

    #[test]
    fn header_off_registration_and_polling_perform_no_detail_subscription_or_header_write() {
        struct RegistrationSeam {
            console_arms: Arc<AtomicUsize>,
            detail_subscriptions: Arc<AtomicUsize>,
            header_writes: Arc<AtomicUsize>,
        }
        impl PageRegistration for RegistrationSeam {
            fn registration_target_id(&self) -> String {
                "page".to_owned()
            }
            fn registration_arm_console_capture(&self) -> Result<(), Error> {
                self.console_arms.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }
            fn registration_events(&self) -> Option<Box<dyn PageEventReceiver>> {
                None
            }
            fn registration_details(&self) -> Option<Box<dyn DetailReceiver>> {
                self.detail_subscriptions.fetch_add(1, Ordering::SeqCst);
                None
            }
            fn registration_url(&self) -> String {
                self.header_writes.fetch_add(1, Ordering::SeqCst);
                "https://example.test/".to_owned()
            }
        }
        let console_arms = Arc::new(AtomicUsize::new(0));
        let detail_subscriptions = Arc::new(AtomicUsize::new(0));
        let header_writes = Arc::new(AtomicUsize::new(0));
        let lifecycle_subscriptions = Arc::new(AtomicUsize::new(0));
        let lifecycle_count = Arc::clone(&lifecycle_subscriptions);
        let (lifecycle_tx, lifecycle_rx) = std::sync::mpsc::channel();
        let lifecycle_rx = Arc::new(Mutex::new(Some(lifecycle_rx)));
        let mut state = BrowserState::default();
        state.lifecycle_subscription_provider = Box::new(move |_| {
            lifecycle_count.fetch_add(1, Ordering::SeqCst);
            lifecycle_rx
                .lock()
                .unwrap()
                .take()
                .map(|receiver| Box::new(receiver) as Box<dyn LifecycleReceiver>)
        });
        state
            .register_page(&RegistrationSeam {
                console_arms: Arc::clone(&console_arms),
                detail_subscriptions: Arc::clone(&detail_subscriptions),
                header_writes: Arc::clone(&header_writes),
            })
            .unwrap();
        state
            .register_page(&RegistrationSeam {
                console_arms: Arc::clone(&console_arms),
                detail_subscriptions: Arc::clone(&detail_subscriptions),
                header_writes: Arc::clone(&header_writes),
            })
            .unwrap();
        lifecycle_tx
            .send(TargetLifecycleEvent::Upsert {
                target_id: "background".to_owned(),
                url: "https://example.test/background".to_owned(),
            })
            .unwrap();
        state.poll_events();
        assert_eq!(console_arms.load(Ordering::SeqCst), 1);
        assert_eq!(lifecycle_subscriptions.load(Ordering::SeqCst), 0);
        assert_eq!(detail_subscriptions.load(Ordering::SeqCst), 0);
        assert_eq!(header_writes.load(Ordering::SeqCst), 0);
        assert!(state.tab_inventory.is_empty());
        assert!(state.pages["page"].header.is_none());
    }

    #[derive(Clone, Copy)]
    enum RegistrationFailure {
        Timeout,
        Cancelled,
    }

    #[derive(Clone)]
    struct RetryRegistrationSeam {
        control: FakePageLifecycleControl,
        target_id: String,
    }

    impl PageRegistration for RetryRegistrationSeam {
        fn registration_target_id(&self) -> String {
            self.target_id.clone()
        }

        fn registration_arm_console_capture(&self) -> Result<(), Error> {
            let mut state = self.control.0.lock().unwrap();
            let arms = state.arms;
            state.arms += 1;
            if arms == 0 {
                return Err(match state.failure {
                    RegistrationFailure::Timeout => Error::Timeout(1),
                    RegistrationFailure::Cancelled => Error::Cancelled,
                });
            }
            Ok(())
        }

        fn registration_events(&self) -> Option<Box<dyn PageEventReceiver>> {
            None
        }

        fn registration_details(&self) -> Option<Box<dyn DetailReceiver>> {
            None
        }

        fn registration_url(&self) -> String {
            "https://example.test/".to_owned()
        }
    }

    #[derive(Clone)]
    struct FakePageLifecycleControl(Arc<Mutex<FakePageLifecycleState>>);

    struct FakePageLifecycleState {
        failure: RegistrationFailure,
        inventory: Vec<String>,
        arms: usize,
        attach_calls: usize,
        discovery_calls: usize,
        close_calls: usize,
        new_calls: usize,
    }

    impl FakePageLifecycleControl {
        fn new(failure: RegistrationFailure, inventory: Vec<&str>) -> Self {
            Self(Arc::new(Mutex::new(FakePageLifecycleState {
                failure,
                inventory: inventory.into_iter().map(str::to_owned).collect(),
                arms: 0,
                attach_calls: 0,
                discovery_calls: 0,
                close_calls: 0,
                new_calls: 0,
            })))
        }

        fn candidate(&self, target_id: &str) -> PageCandidate {
            PageCandidate {
                registration: Box::new(RetryRegistrationSeam {
                    control: self.clone(),
                    target_id: target_id.to_owned(),
                }),
                handle: inactive_page_handle(target_id),
            }
        }
    }

    impl PageLifecycleSeam for FakePageLifecycleControl {
        fn attach_remote(
            &mut self,
            _request: &ActorRequest,
        ) -> Result<PageCandidate, BrowserError> {
            self.0.lock().unwrap().attach_calls += 1;
            Ok(self.candidate("remote"))
        }

        fn discover_pages(
            &mut self,
            _request: &ActorRequest,
        ) -> Result<Vec<PageCandidate>, BrowserError> {
            let inventory = {
                let mut state = self.0.lock().unwrap();
                state.discovery_calls += 1;
                state.inventory.clone()
            };
            Ok(inventory
                .iter()
                .map(|target_id| self.candidate(target_id))
                .collect())
        }

        fn close_page(
            &mut self,
            page: &ActivePageHandle,
            _request: &ActorRequest,
        ) -> Result<(), BrowserError> {
            let mut state = self.0.lock().unwrap();
            state.close_calls += 1;
            state
                .inventory
                .retain(|target_id| target_id != &page.target_id());
            Ok(())
        }

        fn new_page(&mut self, _request: &ActorRequest) -> Result<PageCandidate, BrowserError> {
            let mut state = self.0.lock().unwrap();
            state.new_calls += 1;
            state.inventory.push("replacement".to_owned());
            drop(state);
            Ok(self.candidate("replacement"))
        }
    }

    fn inactive_page_handle(target_id: &str) -> ActivePageHandle {
        ActivePageHandle {
            page: None,
            target_id: target_id.to_owned(),
            url: "https://example.test/".to_owned(),
        }
    }

    #[test]
    fn remote_ensure_page_retries_timeout_and_cancellation_before_publishing_page() {
        for failure in [RegistrationFailure::Timeout, RegistrationFailure::Cancelled] {
            let control = FakePageLifecycleControl::new(failure, vec!["remote"]);
            let mut state = BrowserState::new(
                BrowserStartup::Remote(ConnectOptions::new("ws://test.invalid")),
                FeatureConfig::default(),
            );
            state.page_lifecycle_seam = Some(Box::new(control.clone()));

            let first_request = digest_request();
            assert!(state.ensure_page(&first_request).is_err());
            assert!(state.page.is_none());
            assert!(state.browser.is_none());
            assert!(!state.pages.contains_key("remote"));
            assert!(state.tab_order.is_empty());

            let second_request = digest_request();
            let target_id = state
                .ensure_page(&second_request)
                .expect("the second real request must retry remote attachment")
                .target_id();
            assert_eq!(target_id, "remote");
            assert!(state.pages.contains_key("remote"));
            let observed = control.0.lock().unwrap();
            assert_eq!(observed.attach_calls, 2);
            assert_eq!(observed.arms, 2);
        }
    }

    #[test]
    fn close_replacement_arm_failure_is_retried_by_next_ordinary_request() {
        let control = FakePageLifecycleControl::new(RegistrationFailure::Timeout, vec!["closed"]);
        let mut state = BrowserState {
            page: Some(inactive_page_handle("closed")),
            active_target_id: Some("closed".to_owned()),
            tab_order: vec!["closed".to_owned()],
            page_lifecycle_seam: Some(Box::new(control.clone())),
            ..BrowserState::default()
        };
        state.pages.insert(
            "closed".to_owned(),
            page_runtime_for_registration(false, || None, || None, String::new),
        );
        let close_request = digest_request();
        assert!(
            state
                .tabs(TabAction::Close, None, None, &close_request)
                .is_err(),
            "replacement arming must fail the close request"
        );
        assert!(state.page.is_none());
        assert!(state.active_target_id.is_none());
        assert!(!state.pages.contains_key("closed"));
        assert!(!state.pages.contains_key("replacement"));

        let next_request = digest_request();
        let target_id = state
            .ensure_page(&next_request)
            .expect("an ordinary request must rediscover and arm the replacement")
            .target_id();
        assert_eq!(target_id, "replacement");
        assert!(state.pages.contains_key("replacement"));
        let observed = control.0.lock().unwrap();
        assert_eq!(observed.close_calls, 1);
        assert_eq!(observed.new_calls, 1);
        assert_eq!(observed.arms, 2);
        assert!(observed.discovery_calls >= 3);
    }

    fn digest_request() -> ActorRequest {
        let (reply, _response) = oneshot::channel();
        ActorRequest {
            request_id: request_id(51_540),
            op: BrowserOp::Snapshot {
                target: None,
                depth: None,
                boxes: false,
            },
            cancellation: Arc::new(CommandCancellation::new()),
            deadline: Instant::now() + Duration::from_secs(1),
            timeout_ms: 1_000,
            reply,
        }
    }

    #[derive(Clone)]
    struct FakeBrowserQueryControl(Arc<Mutex<FakeBrowserQueryState>>);

    struct FakeBrowserQueryState {
        inventory: Vec<(String, String)>,
        inventory_error: bool,
        inventory_queries: usize,
        active: Option<(String, String)>,
        modal_targets: HashSet<String>,
        observation: PageObservation,
        observation_queries: usize,
    }

    impl FakeBrowserQueryControl {
        fn new(inventory: Vec<(String, String)>, active: Option<(String, String)>) -> Self {
            Self(Arc::new(Mutex::new(FakeBrowserQueryState {
                inventory,
                inventory_error: false,
                inventory_queries: 0,
                active,
                modal_targets: HashSet::new(),
                observation: (None, None),
                observation_queries: 0,
            })))
        }

        fn provider(&self) -> Box<dyn BrowserQueryProvider> {
            Box::new(FakeBrowserQueryProvider(self.clone()))
        }
    }

    struct FakeBrowserQueryProvider(FakeBrowserQueryControl);

    impl BrowserQueryProvider for FakeBrowserQueryProvider {
        fn inventory(
            &mut self,
            _browser: Option<&Browser>,
            _request: &ActorRequest,
        ) -> Result<Vec<BrowserInventoryEntry>, BrowserError> {
            let mut state = self.0.0.lock().unwrap();
            state.inventory_queries += 1;
            if state.inventory_error {
                return Err(BrowserError::Message("inventory unavailable".to_owned()));
            }
            Ok(state
                .inventory
                .iter()
                .map(|(target_id, url)| BrowserInventoryEntry {
                    target_id: target_id.clone(),
                    url: url.clone(),
                    page: None,
                })
                .collect())
        }

        fn active_page(&mut self, _page: Option<&ActivePageHandle>) -> Option<(String, String)> {
            self.0.0.lock().unwrap().active.clone()
        }

        fn pending_modal(
            &mut self,
            _pages: &HashMap<String, PageRuntime>,
            target_id: &str,
        ) -> bool {
            self.0.0.lock().unwrap().modal_targets.contains(target_id)
        }

        fn observe(&mut self, _page: Option<&Page>, _request: &ActorRequest) -> PageObservation {
            let mut state = self.0.0.lock().unwrap();
            state.observation_queries += 1;
            state.observation.clone()
        }
    }

    struct ReceiverRegistrationSeam {
        target_id: String,
        url: String,
        events: Mutex<Option<Box<dyn PageEventReceiver>>>,
        details: Mutex<Option<Box<dyn DetailReceiver>>>,
    }

    impl ReceiverRegistrationSeam {
        fn new(target_id: &str, url: &str) -> Self {
            Self {
                target_id: target_id.to_owned(),
                url: url.to_owned(),
                events: Mutex::new(None),
                details: Mutex::new(None),
            }
        }
    }

    impl PageRegistration for ReceiverRegistrationSeam {
        fn registration_target_id(&self) -> String {
            self.target_id.clone()
        }

        fn registration_events(&self) -> Option<Box<dyn PageEventReceiver>> {
            self.events.lock().unwrap().take()
        }

        fn registration_details(&self) -> Option<Box<dyn DetailReceiver>> {
            self.details.lock().unwrap().take()
        }

        fn registration_url(&self) -> String {
            self.url.clone()
        }
    }

    fn install_lifecycle_subscription(
        state: &mut BrowserState,
    ) -> std::sync::mpsc::Sender<TargetLifecycleEvent> {
        let (sender, receiver) = std::sync::mpsc::channel();
        let receiver = Arc::new(Mutex::new(Some(receiver)));
        state.lifecycle_subscription_provider = Box::new(move |_| {
            receiver
                .lock()
                .unwrap()
                .take()
                .map(|receiver| Box::new(receiver) as Box<dyn LifecycleReceiver>)
        });
        sender
    }

    #[test]
    fn page_digest_provider_covers_active_order_stale_current_and_modal_bypass() {
        let inventory = vec![
            (
                "active".to_owned(),
                "https://example.test/active".to_owned(),
            ),
            (
                "background".to_owned(),
                "https://example.test/background".to_owned(),
            ),
        ];
        let mut features = FeatureConfig::default();
        features.header = true;
        let mut state = BrowserState::new(BrowserStartup::Local, features);
        let query = FakeBrowserQueryControl::new(inventory.clone(), Some(inventory[0].clone()));
        query.0.lock().unwrap().observation = (Some("Active".to_owned()), Some((1, 2)));
        state.browser_query_provider = query.provider();
        let request = digest_request();
        assert_eq!(
            state.page_digest(&request).as_deref(),
            Some(
                "### Page\nURL: https://example.test/active\nTitle: Active\nStatus: unknown\nConsole: 1 errors, 2 warnings"
            )
        );
        assert_eq!(state.page_digest(&request), None);

        query.0.lock().unwrap().active = Some(inventory[1].clone());
        assert!(state.page_digest(&request).unwrap().contains("/background"));
        assert_eq!(
            state
                .header_state
                .as_ref()
                .unwrap()
                .last_rendered_tab_signature
                .as_ref()
                .unwrap()
                .active_id
                .as_deref(),
            Some("background")
        );

        let previous_signature = state
            .header_state
            .as_ref()
            .unwrap()
            .last_rendered_tab_signature
            .clone()
            .unwrap();
        state.inventory_stale = true;
        query.0.lock().unwrap().inventory.reverse();
        assert!(state.page_digest(&request).is_some());
        assert_eq!(
            state.tab_order,
            vec!["background".to_owned(), "active".to_owned()]
        );
        let reordered_signature = state
            .header_state
            .as_ref()
            .unwrap()
            .last_rendered_tab_signature
            .clone()
            .unwrap();
        assert_ne!(reordered_signature, previous_signature);
        assert_eq!(
            reordered_signature.tabs,
            vec![
                (
                    "background".to_owned(),
                    "https://example.test/background".to_owned()
                ),
                (
                    "active".to_owned(),
                    "https://example.test/active".to_owned()
                ),
            ]
        );
        assert_eq!(query.0.lock().unwrap().inventory_queries, 2);

        state.inventory_stale = true;
        query
            .0
            .lock()
            .unwrap()
            .inventory
            .iter_mut()
            .find(|(target_id, _)| target_id == "active")
            .unwrap()
            .1 = "https://example.test/active/reconciled".to_owned();
        assert!(state.page_digest(&request).is_some());
        assert_eq!(
            state.tab_inventory["active"],
            "https://example.test/active/reconciled"
        );
        assert_eq!(query.0.lock().unwrap().inventory_queries, 3);

        state
            .pages
            .get_mut("background")
            .unwrap()
            .header
            .as_mut()
            .unwrap()
            .current
            .title = Some("Stale".to_owned());
        query.0.lock().unwrap().observation = (Some("Fresh".to_owned()), None);
        let fresh = state.page_digest(&request).unwrap();
        assert!(fresh.contains("Title: Fresh"));
        assert!(!fresh.contains("Title: Stale"));

        state.inventory_stale = true;
        query.0.lock().unwrap().inventory_error = true;
        assert!(state.page_digest(&request).is_some());
        assert!(
            state
                .header_state
                .as_ref()
                .unwrap()
                .last_rendered_tab_signature
                .as_ref()
                .unwrap()
                .stale
        );

        query.0.lock().unwrap().inventory_error = false;
        query.0.lock().unwrap().observation = (Some("blocked".to_owned()), None);
        let observation_queries = query.0.lock().unwrap().observation_queries;
        query
            .0
            .lock()
            .unwrap()
            .modal_targets
            .insert("background".to_owned());
        state
            .pages
            .get_mut("background")
            .unwrap()
            .header
            .as_mut()
            .unwrap()
            .current
            .status = Some(204);
        let modal = state.page_digest(&request).unwrap();
        assert_eq!(
            query.0.lock().unwrap().observation_queries,
            observation_queries
        );
        assert!(modal.contains("Status: 204"));
    }

    #[test]
    fn screenshot_does_not_consume_changed_page_digest_before_next_text_response() {
        let initial = (
            "active".to_owned(),
            "https://example.test/before".to_owned(),
        );
        let mut features = FeatureConfig::default();
        features.header = true;
        let mut state = BrowserState::new(BrowserStartup::Local, features);
        let query = FakeBrowserQueryControl::new(vec![initial.clone()], Some(initial));
        state.browser_query_provider = query.provider();
        let request = digest_request();

        let first = state.add_page_digest(BrowserOutput::Text("first".to_owned()), &request);
        assert!(output_text(&first).contains("URL: https://example.test/before"));

        query.0.lock().unwrap().active =
            Some(("active".to_owned(), "https://example.test/after".to_owned()));
        let screenshot = state.add_page_digest(
            BrowserOutput::Image {
                bytes: vec![1, 2, 3],
                mime: "image/png",
                extension: "png",
            },
            &request,
        );
        assert!(matches!(screenshot, BrowserOutput::Image { .. }));

        let next = state.add_page_digest(BrowserOutput::Text("next".to_owned()), &request);
        assert!(output_text(&next).contains("URL: https://example.test/after"));
    }

    #[test]
    fn background_tab_lifecycle_navigation_and_close_change_digest_signature() {
        let mut features = FeatureConfig::default();
        features.header = true;
        let mut state = BrowserState::new(BrowserStartup::Local, features);
        let inventory = vec![(
            "active".to_owned(),
            "https://example.test/active".to_owned(),
        )];
        let query = FakeBrowserQueryControl::new(inventory.clone(), Some(inventory[0].clone()));
        state.browser_query_provider = query.provider();
        let lifecycle_tx = install_lifecycle_subscription(&mut state);
        state
            .register_page(&ReceiverRegistrationSeam::new(
                "active",
                "https://example.test/active",
            ))
            .unwrap();
        assert!(state.target_lifecycle.is_some());
        let request = digest_request();
        assert!(state.page_digest(&request).is_some());

        lifecycle_tx
            .send(TargetLifecycleEvent::Upsert {
                target_id: "background".to_owned(),
                url: "https://example.test/background".to_owned(),
            })
            .unwrap();
        assert!(state.page_digest(&request).is_some());
        lifecycle_tx
            .send(TargetLifecycleEvent::Upsert {
                target_id: "background".to_owned(),
                url: "https://example.test/background/next".to_owned(),
            })
            .unwrap();
        assert!(state.page_digest(&request).is_some());
        assert_eq!(
            state.tab_inventory["background"],
            "https://example.test/background/next"
        );

        lifecycle_tx
            .send(TargetLifecycleEvent::Destroyed {
                target_id: "background".to_owned(),
            })
            .unwrap();
        assert!(state.page_digest(&request).is_some());
        assert!(!state.tab_inventory.contains_key("background"));
    }

    #[test]
    fn active_target_destruction_clears_actor_page_identity() {
        let mut features = FeatureConfig::default();
        features.header = true;
        let mut state = BrowserState::new(BrowserStartup::Local, features);
        let lifecycle_tx = install_lifecycle_subscription(&mut state);
        state
            .register_page(&ReceiverRegistrationSeam::new(
                "active",
                "https://example.test/active",
            ))
            .unwrap();
        state.page = Some(ActivePageHandle {
            page: None,
            target_id: "active".to_owned(),
            url: "https://example.test/active".to_owned(),
        });
        assert!(state.page.is_some());
        lifecycle_tx
            .send(TargetLifecycleEvent::Destroyed {
                target_id: "active".to_owned(),
            })
            .unwrap();

        state.poll_events();

        assert!(state.page.is_none());
        assert!(state.active_target_id.is_none());
        assert!(!state.tab_order.iter().any(|id| id == "active"));
        assert!(!state.tab_inventory.contains_key("active"));
    }

    #[test]
    fn lifecycle_disconnect_marks_inventory_stale_for_reconciliation() {
        let mut features = FeatureConfig::default();
        features.header = true;
        let mut state = BrowserState::new(BrowserStartup::Local, features);
        state.inventory_stale = false;
        let lifecycle_tx = install_lifecycle_subscription(&mut state);
        state
            .register_page(&ReceiverRegistrationSeam::new(
                "active",
                "https://example.test/active",
            ))
            .unwrap();
        assert!(state.target_lifecycle.is_some());
        drop(lifecycle_tx);

        state.poll_events();

        assert!(state.inventory_stale);
        assert!(state.target_lifecycle.is_none());
    }

    #[test]
    fn page_closed_event_uses_production_receiver_loop_and_clears_active_identity() {
        let (event_tx, event_rx) = std::sync::mpsc::channel();
        let mut features = FeatureConfig::default();
        features.header = true;
        let mut state = BrowserState::new(BrowserStartup::Local, features);
        let _lifecycle_tx = install_lifecycle_subscription(&mut state);
        let registration = ReceiverRegistrationSeam::new("active", "https://example.test/active");
        *registration.events.lock().unwrap() = Some(Box::new(event_rx));
        state.register_page(&registration).unwrap();
        state.active_target_id = Some("active".to_owned());
        event_tx.send(PageEvent::Closed).unwrap();

        state.poll_events();

        assert!(state.page.is_none());
        assert!(state.active_target_id.is_none());
        assert!(!state.pages.contains_key("active"));
        assert!(!state.tab_order.iter().any(|id| id == "active"));
    }

    #[test]
    fn navigation_details_retain_same_document_status_and_clear_unobserved_documents() {
        let mut runtime = PageHeaderRuntime {
            current: PageHeader {
                url: "https://example.test/a".to_owned(),
                title: Some("A".to_owned()),
                status: Some(200),
                ..PageHeader::default()
            },
            last_observed_title: Some("A".to_owned()),
            ..PageHeaderRuntime::default()
        };
        apply_navigation_detail(
            &mut runtime,
            1,
            NavigationDetail {
                url: "https://example.test/a#same".to_owned(),
                same_document: true,
            },
        );
        assert_eq!(runtime.current.status, Some(200));

        apply_navigation_detail(
            &mut runtime,
            2,
            NavigationDetail {
                url: "https://example.test/b".to_owned(),
                same_document: false,
            },
        );
        assert_eq!(runtime.current.status, None);
        assert_eq!(runtime.last_observed_title, None);

        runtime.current.status = Some(204);
        runtime.pending_observed_url = Some("https://example.test/c".to_owned());
        runtime.pending_observed_after_sequence = Some(2);
        apply_navigation_detail(
            &mut runtime,
            3,
            NavigationDetail {
                url: "https://example.test/c".to_owned(),
                same_document: false,
            },
        );
        assert_eq!(runtime.current.status, Some(204));
    }

    struct NavigationDetailSeamControl {
        sender: std::sync::mpsc::Sender<(u64, NavigationDetail)>,
        latest_sequence: Arc<AtomicU64>,
        dropped_count: Arc<AtomicU64>,
        _lifecycle_sender: std::sync::mpsc::Sender<TargetLifecycleEvent>,
    }

    impl NavigationDetailSeamControl {
        fn send(&self, detail: NavigationDetail) {
            let sequence = self.publish();
            self.deliver(sequence, detail);
        }

        fn publish(&self) -> u64 {
            self.latest_sequence.fetch_add(1, Ordering::SeqCst) + 1
        }

        fn deliver(&self, sequence: u64, detail: NavigationDetail) {
            self.sender.send((sequence, detail)).unwrap();
        }

        fn record_drop(&self) {
            self.dropped_count.fetch_add(1, Ordering::SeqCst);
        }
    }

    fn polling_state_with_header(
        header: PageHeaderRuntime,
    ) -> (BrowserState, NavigationDetailSeamControl) {
        let (tx, rx) = std::sync::mpsc::channel();
        let latest_sequence = Arc::new(AtomicU64::new(0));
        let dropped_count = Arc::new(AtomicU64::new(0));
        let mut features = FeatureConfig::default();
        features.header = true;
        let mut state = BrowserState::new(BrowserStartup::Local, features);
        let lifecycle_sender = install_lifecycle_subscription(&mut state);
        let registration = ReceiverRegistrationSeam::new("page", "https://example.test/start");
        *registration.details.lock().unwrap() = Some(Box::new(NavigationDetailReceiverSeam {
            receiver: rx,
            latest_sequence: Arc::clone(&latest_sequence),
            dropped_count: Arc::clone(&dropped_count),
        }));
        state.register_page(&registration).unwrap();
        *state
            .pages
            .get_mut("page")
            .unwrap()
            .header
            .as_mut()
            .unwrap() = header;
        assert!(state.target_lifecycle.is_some());
        assert!(state.pages["page"].navigation_details.is_some());
        (
            state,
            NavigationDetailSeamControl {
                sender: tx,
                latest_sequence,
                dropped_count,
                _lifecycle_sender: lifecycle_sender,
            },
        )
    }

    #[test]
    fn poll_events_correlates_delayed_mismatched_dropped_and_pre_operation_details() {
        let observation = NavigationObservation {
            response_json: Value::Null,
            main_status: Some(204),
            same_document: false,
        };

        let (mut delayed, delayed_tx) = polling_state_with_header(PageHeaderRuntime::default());
        delayed.begin_observed_navigation("page", Some("https://example.test/owned".to_owned()));
        delayed.record_observed_navigation(
            "page",
            "https://example.test/owned".to_owned(),
            &observation,
            false,
        );
        delayed.poll_events();
        assert_eq!(
            delayed.pages["page"]
                .header
                .as_ref()
                .unwrap()
                .current
                .status,
            Some(204)
        );
        delayed_tx.send(NavigationDetail {
            url: "https://example.test/owned".to_owned(),
            same_document: false,
        });
        delayed.poll_events();
        assert_eq!(
            delayed.pages["page"]
                .header
                .as_ref()
                .unwrap()
                .current
                .status,
            Some(204)
        );

        let (mut mismatched, mismatched_tx) =
            polling_state_with_header(PageHeaderRuntime::default());
        mismatched.begin_observed_navigation("page", Some("https://example.test/owned".to_owned()));
        mismatched.record_observed_navigation(
            "page",
            "https://example.test/owned".to_owned(),
            &observation,
            false,
        );
        mismatched_tx.send(NavigationDetail {
            url: "https://example.test/click".to_owned(),
            same_document: false,
        });
        mismatched.poll_events();
        assert_eq!(
            mismatched.pages["page"]
                .header
                .as_ref()
                .unwrap()
                .current
                .status,
            None
        );

        let (mut dropped, dropped_tx) = polling_state_with_header(PageHeaderRuntime::default());
        dropped.begin_observed_navigation("page", Some("https://example.test/dropped".to_owned()));
        dropped.record_observed_navigation(
            "page",
            "https://example.test/dropped".to_owned(),
            &observation,
            false,
        );
        dropped_tx.record_drop();
        dropped_tx.send(NavigationDetail {
            url: "https://example.test/later-click".to_owned(),
            same_document: false,
        });
        dropped.poll_events();
        assert_eq!(
            dropped.pages["page"]
                .header
                .as_ref()
                .unwrap()
                .current
                .status,
            None
        );

        let (mut stale, stale_tx) = polling_state_with_header(PageHeaderRuntime::default());
        stale_tx.send(NavigationDetail {
            url: "https://example.test/owned".to_owned(),
            same_document: false,
        });
        stale.begin_observed_navigation("page", Some("https://example.test/owned".to_owned()));
        stale.record_observed_navigation(
            "page",
            "https://example.test/owned".to_owned(),
            &observation,
            false,
        );
        assert_eq!(
            stale.pages["page"]
                .header
                .as_ref()
                .unwrap()
                .pending_observed_after_sequence,
            Some(1)
        );
        stale_tx.send(NavigationDetail {
            url: "https://example.test/owned".to_owned(),
            same_document: false,
        });
        stale.poll_events();
        assert_eq!(
            stale.pages["page"].header.as_ref().unwrap().current.status,
            Some(204)
        );
    }

    #[test]
    fn pre_boundary_detail_delivered_late_stays_pre_boundary_in_receiver_loop() {
        let observation = NavigationObservation {
            response_json: Value::Null,
            main_status: Some(204),
            same_document: false,
        };
        let (mut state, details) = polling_state_with_header(PageHeaderRuntime::default());
        let pre_boundary_sequence = details.publish();

        state.begin_observed_navigation("page", Some("https://example.test/owned".to_owned()));
        state.record_observed_navigation(
            "page",
            "https://example.test/owned".to_owned(),
            &observation,
            false,
        );
        details.deliver(
            pre_boundary_sequence,
            NavigationDetail {
                url: "https://example.test/unrelated-before".to_owned(),
                same_document: false,
            },
        );
        state.poll_events();

        let header = state.pages["page"].header.as_ref().unwrap();
        assert_eq!(header.current.url, "https://example.test/owned");
        assert_eq!(header.current.status, Some(204));
        assert_eq!(header.pending_observed_after_sequence, Some(1));
    }

    #[test]
    fn actor_goto_new_tab_and_reload_map_observed_status() {
        let mut goto = PageHeaderRuntime::default();
        let goto_observation = NavigationObservation {
            response_json: Value::Null,
            main_status: Some(201),
            same_document: false,
        };
        apply_observed_navigation(
            &mut goto,
            "https://example.test/goto".to_owned(),
            &goto_observation,
            false,
        );
        assert_eq!(goto.current.status, Some(201));

        let mut new_tab = PageHeaderRuntime::default();
        let new_tab_observation = NavigationObservation {
            main_status: Some(202),
            ..goto_observation.clone()
        };
        apply_observed_navigation(
            &mut new_tab,
            "https://example.test/new-tab".to_owned(),
            &new_tab_observation,
            false,
        );
        assert_eq!(new_tab.current.status, Some(202));

        let mut reload = PageHeaderRuntime {
            current: PageHeader {
                status: Some(200),
                ..PageHeader::default()
            },
            ..PageHeaderRuntime::default()
        };
        let same_document_reload = NavigationObservation {
            response_json: Value::Null,
            main_status: None,
            same_document: true,
        };
        apply_observed_navigation(
            &mut reload,
            "https://example.test/reload#same".to_owned(),
            &same_document_reload,
            true,
        );
        assert_eq!(reload.current.status, None);
    }

    #[test]
    fn absent_cached_title_is_omitted_from_modal_safe_header() {
        let mut cached = PageHeaderRuntime {
            last_observed_title: Some("Cached title".to_owned()),
            ..PageHeaderRuntime::default()
        };
        apply_observed_title(&mut cached, true, None);
        assert_eq!(cached.current.title.as_deref(), Some("Cached title"));

        let mut absent = PageHeaderRuntime::default();
        apply_observed_title(&mut absent, true, None);
        let rendered = render_page_header(&PageHeader {
            url: "https://example.test/modal".to_owned(),
            title: absent.current.title,
            status: None,
            console_err: 0,
            console_warn: 0,
        });
        assert!(!rendered.contains("Title:"));
        assert!(rendered.contains("Status: unknown"));

        let observed = modal_safe_page_observation(true, || {
            panic!("title or console query ran while a modal was pending")
        });
        assert_eq!(observed, (None, None));
    }

    #[test]
    fn page_digest_fields_are_utf8_safe_and_bounded_with_truthful_omissions() {
        let url = "🙂".repeat(2_000);
        let title = "é".repeat(2_000);
        let rendered = render_page_header(&PageHeader {
            url: url.clone(),
            title: Some(title.clone()),
            status: Some(200),
            console_err: 0,
            console_warn: 0,
        });
        assert!(rendered.len() < 2_200, "{}", rendered.len());
        let url_cut = MAX_DIGEST_URL_BYTES - (MAX_DIGEST_URL_BYTES % '🙂'.len_utf8());
        let title_cut = MAX_DIGEST_TITLE_BYTES - (MAX_DIGEST_TITLE_BYTES % 'é'.len_utf8());
        assert!(rendered.contains(&format!("{} bytes omitted", url.len() - url_cut)));
        assert!(rendered.contains(&format!("{} bytes omitted", title.len() - title_cut)));
    }

    #[test]
    fn distill_switch_selects_legacy_before_page_evaluation() {
        assert_ne!(SNAPSHOT_JS, SNAPSHOT_LEGACY_JS);
        for (distill, expected) in [(false, SNAPSHOT_LEGACY_JS), (true, SNAPSHOT_JS)] {
            let captured = Arc::new(Mutex::new(None));
            let capture = Arc::clone(&captured);
            let mut features = FeatureConfig::default();
            features.distill = distill;
            let mut state = BrowserState {
                features,
                snapshot_evaluator: Some(Box::new(move |script, _input| {
                    *capture.lock().unwrap() = Some(script);
                    Ok(json!({ "outline": "captured", "nextRef": 1, "refs": [] }))
                })),
                ..BrowserState::default()
            };
            let (reply, _response) = oneshot::channel();
            let request = ActorRequest {
                request_id: request_id(if distill { 47_876 } else { 47_875 }),
                op: BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                cancellation: Arc::new(CommandCancellation::new()),
                deadline: Instant::now() + Duration::from_secs(1),
                timeout_ms: 1_000,
                reply,
            };

            let (value, start_ref) = state
                .evaluate_snapshot(&request, None, None, false, None, None, None)
                .expect("capturing page evaluator should return its fixture value");
            assert_eq!(value["outline"], "captured");
            assert_eq!(start_ref, 1);
            assert_eq!(*captured.lock().unwrap(), Some(expected));
        }
    }

    #[test]
    fn selected_tab_url_provenance_preserves_raw_value_for_only_the_selected_index() {
        let raw = "https://example.invalid/a\r\nb\t\0\\\"\u{2028}\u{2029}";
        let (preview, exact) = tab_url_values(7, Some(7), raw);
        assert_eq!(
            preview,
            "https://example.invalid/a  b\t\0\\\"\u{2028}\u{2029}"
        );
        assert_eq!(exact.as_deref(), Some(raw));

        let (_, non_selected) = tab_url_values(6, Some(7), raw);
        assert_eq!(non_selected, None);
        let (_, list_action) = tab_url_values(7, None, raw);
        assert_eq!(list_action, None);
    }

    fn console_record(epoch: u64, severity: &str, location: &str, text: &str) -> ConsoleRecord {
        ConsoleRecord {
            message_type: severity.to_owned(),
            text: text.to_owned(),
            args: Vec::new(),
            location: Some(rustwright::ConsoleLocation {
                url: location.to_owned(),
                line_number: epoch + 10,
                column_number: 0,
            }),
            attributed_location: None,
            navigation_epoch: epoch,
        }
    }

    fn console_records(records: Vec<ConsoleRecord>) -> ConsoleRecords {
        ConsoleRecords {
            records,
            navigation_epoch: 2,
            evicted: 0,
        }
    }

    fn network_record(index: u64, url: &str, resource_type: &str) -> NetworkRecord {
        NetworkRecord {
            index,
            method: "GET".to_owned(),
            url: url.to_owned(),
            resource_type: resource_type.to_owned(),
            response_status: Some(200),
            failure: None,
            request_headers: Vec::new(),
            request_body: None,
            response_headers: Vec::new(),
            navigation_epoch: 0,
            completed: true,
        }
    }

    fn network_records(records: Vec<NetworkRecord>) -> NetworkRecords {
        NetworkRecords {
            records,
            navigation_epoch: 0,
            navigation_start_index: 1,
            evicted: 0,
        }
    }

    struct FixturePageRecordSource {
        console: ConsoleRecords,
        network: NetworkRecords,
    }

    impl PageRecordSource for FixturePageRecordSource {
        fn console_records(
            &mut self,
            _include_previous_navigations: bool,
            _clear: bool,
        ) -> Result<ConsoleRecords, Error> {
            Ok(self.console.clone())
        }

        fn network_records(
            &mut self,
            _include_previous_navigations: bool,
            _clear: bool,
        ) -> NetworkRecords {
            self.network.clone()
        }
    }

    fn actor_with_record_source(
        features: FeatureConfig,
        console: ConsoleRecords,
        network: NetworkRecords,
    ) -> BrowserState {
        BrowserState {
            features,
            page_record_source: Some(Box::new(FixturePageRecordSource { console, network })),
            ..BrowserState::default()
        }
    }

    #[test]
    fn console_messages_deduplicates_structured_unfiltered_adjacent_runs() {
        let records = console_records(vec![
            console_record(0, "warning", "first location", "repeat   text"),
            console_record(0, "warn", "second location", "repeat\ttext"),
            console_record(0, "debug", "hidden boundary", "not visible"),
            console_record(0, "warning", "third location", "repeat text"),
            console_record(0, "error", "severity boundary", "repeat text"),
            console_record(0, "warning", "nbsp", "repeat\u{a0}text"),
            console_record(0, "warning", "space", "repeat text"),
            console_record(1, "warning", "next navigation", "repeat text"),
            console_record(1, "warning", "next duplicate", "repeat\ttext"),
        ]);
        let mut features = FeatureConfig::default();
        features.console_dedup = true;
        let mut state = actor_with_record_source(features, records, network_records(Vec::new()));
        let request = digest_request();
        let rendered = state
            .console_messages(ConsoleLevel::Info, false, None, &request)
            .unwrap();
        assert_eq!(
            rendered,
            "WARNING first location:10 repeat   text (repeated 2 times)\n\
             WARNING third location:10 repeat text\n\
             ERROR severity boundary:10 repeat text\n\
             WARNING nbsp:10 repeat\u{a0}text\n\
             WARNING space:10 repeat text\n\
             WARNING next navigation:11 repeat text (repeated 2 times)"
        );

        state.page_record_source = Some(Box::new(FixturePageRecordSource {
            console: console_records(Vec::new()),
            network: network_records(Vec::new()),
        }));
        assert_eq!(
            state
                .console_messages(ConsoleLevel::Info, false, None, &request)
                .unwrap(),
            "(no console messages)"
        );
    }

    #[test]
    fn treated_console_uses_first_records_attributed_location_while_legacy_stays_raw() {
        let mut first = console_record(0, "warning", "", "repeat text");
        let first_raw = first.location.as_mut().expect("first raw location");
        first_raw.line_number = 169;
        first.attributed_location = Some(rustwright::ConsoleLocation {
            url: "file:///fixture/console.js".to_owned(),
            line_number: 1,
            column_number: 0,
        });
        let mut second = console_record(0, "warning", "", "repeat\ttext");
        let second_raw = second.location.as_mut().expect("second raw location");
        second_raw.line_number = 169;
        second.attributed_location = Some(rustwright::ConsoleLocation {
            url: "file:///fixture/console.js".to_owned(),
            line_number: 2,
            column_number: 0,
        });
        let records = console_records(vec![first, second]);

        assert_eq!(
            console_records_presentation(&records, ConsoleLevel::Info, false).text,
            "WARNING (unknown):169 repeat text\nWARNING (unknown):169 repeat\ttext"
        );
        assert_eq!(
            console_records_presentation(&records, ConsoleLevel::Info, true).text,
            "WARNING file:///fixture/console.js:1 repeat text (repeated 2 times)"
        );
    }

    #[test]
    fn w4_off_modes_run_production_pipelines_with_zero_state_writes() {
        let console = console_records(vec![
            console_record(0, "warning", "first", "same text"),
            console_record(0, "warning", "second", "same\ttext"),
        ]);
        let legacy_console = console_records_presentation(&console, ConsoleLevel::Info, false);
        assert_eq!(
            legacy_console.text,
            "WARNING first:10 same text\nWARNING second:10 same\ttext"
        );
        assert_eq!(legacy_console.w4_state_writes, 0);

        let network = network_records(vec![network_record(
            1,
            "https://example.invalid/asset.png",
            "image",
        )]);
        for (net_note, include_static, has_filename) in [
            (false, false, false),
            (true, true, false),
            (true, false, true),
        ] {
            let legacy = network_records_presentation(
                &network,
                include_static,
                None,
                net_note,
                has_filename,
            );
            assert_eq!(legacy.hidden_static, None);
            assert_eq!(legacy.w4_state_writes, 0);
        }
    }

    #[test]
    fn network_requests_counts_hidden_static_after_caller_regex_filtering() {
        let records = network_records(vec![
            network_record(1, "https://example.invalid/keep.png", "image"),
            network_record(2, "https://example.invalid/drop.png", "image"),
            network_record(3, "https://example.invalid/data", "xhr"),
        ]);
        let mut features = FeatureConfig::default();
        features.net_note = true;
        let mut state = actor_with_record_source(features, console_records(Vec::new()), records);
        let request = digest_request();
        assert_eq!(
            state
                .network_requests(false, Some("keep\\.png$|data$"), None, &request)
                .unwrap(),
            "[3] GET 200 https://example.invalid/data (xhr)\n\
             (1 successful static requests hidden; use static:true to include them)"
        );

        assert_eq!(
            state
                .network_requests(false, Some("missing$"), None, &request)
                .unwrap(),
            "(no matching network requests)"
        );
    }

    #[test]
    fn file_and_screenshot_output_provenance_bypasses_shaping() {
        let file = Some("artifact.txt".to_owned());
        assert!(
            BrowserOp::ConsoleMessages {
                level: ConsoleLevel::Info,
                all: true,
                filename: file.clone(),
            }
            .bypass_response_shaping()
        );
        assert!(
            BrowserOp::NetworkRequests {
                include_static: false,
                filter: Some("api".to_owned()),
                filename: file.clone(),
            }
            .bypass_response_shaping()
        );
        assert!(
            BrowserOp::NetworkRequest {
                index: 1,
                part: None,
                filename: file,
            }
            .bypass_response_shaping()
        );
        assert!(
            BrowserOp::TakeScreenshot {
                full_page: false,
                image_type: ScreenshotType::Png,
            }
            .bypass_response_shaping()
        );
        assert!(
            !BrowserOp::NetworkRequests {
                include_static: true,
                filter: None,
                filename: None,
            }
            .bypass_response_shaping()
        );
    }
    use rustwright::ActionabilityError;

    struct WorkerGuard<T> {
        cancel: CancelToken,
        worker: Option<thread::JoinHandle<T>>,
    }

    impl<T> WorkerGuard<T> {
        fn join(mut self) -> thread::Result<T> {
            self.worker
                .take()
                .expect("worker handle must be present")
                .join()
        }
    }

    impl<T> Drop for WorkerGuard<T> {
        fn drop(&mut self) {
            if let Some(worker) = self.worker.take() {
                self.cancel.cancel();
                let _ = worker.join();
            }
        }
    }

    struct HangingServer {
        addr: SocketAddr,
        stop: Arc<std::sync::atomic::AtomicBool>,
        thread: Option<thread::JoinHandle<()>>,
    }

    impl HangingServer {
        fn start() -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind hanging endpoint");
            listener
                .set_nonblocking(true)
                .expect("set hanging endpoint nonblocking");
            let addr = listener.local_addr().expect("hanging endpoint address");
            let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
            let thread_stop = Arc::clone(&stop);
            let thread = thread::spawn(move || {
                let mut connections = Vec::new();
                while !thread_stop.load(Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((stream, _)) => connections.push(stream),
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(2));
                        }
                        Err(error) => panic!("hanging endpoint accept failed: {error}"),
                    }
                }
            });
            Self {
                addr,
                stop,
                thread: Some(thread),
            }
        }

        fn url(&self) -> String {
            format!("http://{}/never-responds", self.addr)
        }
    }

    impl Drop for HangingServer {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::Relaxed);
            let _ = TcpStream::connect(self.addr);
            if let Some(thread) = self.thread.take() {
                thread.join().expect("join hanging endpoint");
            }
        }
    }

    struct StallingCdpProxy {
        endpoint: String,
        stalled: Arc<AtomicBool>,
        stop: Arc<AtomicBool>,
        thread: Option<thread::JoinHandle<()>>,
    }

    impl StallingCdpProxy {
        fn start(upstream_endpoint: &str) -> Self {
            let upstream = upstream_endpoint
                .strip_prefix("ws://")
                .expect("test browser endpoint should use ws");
            let (upstream_addr, path) = upstream
                .split_once('/')
                .expect("test browser endpoint should contain a path");
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind CDP test proxy");
            listener
                .set_nonblocking(true)
                .expect("set CDP test proxy nonblocking");
            let addr = listener.local_addr().expect("CDP test proxy address");
            let stalled = Arc::new(AtomicBool::new(false));
            let stop = Arc::new(AtomicBool::new(false));
            let thread_stalled = Arc::clone(&stalled);
            let thread_stop = Arc::clone(&stop);
            let upstream_addr = upstream_addr.to_owned();
            let thread = thread::spawn(move || {
                let mut connection_index = 0;
                let mut handlers = Vec::new();
                while !thread_stop.load(Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((client, _)) => {
                            if thread_stop.load(Ordering::Relaxed) {
                                break;
                            }
                            let stall = connection_index == 0;
                            connection_index += 1;
                            let handler_upstream = upstream_addr.clone();
                            let handler_stalled = Arc::clone(&thread_stalled);
                            handlers.push(thread::spawn(move || {
                                proxy_cdp_connection(
                                    client,
                                    &handler_upstream,
                                    stall,
                                    &handler_stalled,
                                )
                            }));
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(2));
                        }
                        Err(error) => panic!("CDP test proxy accept failed: {error}"),
                    }
                }
                for handler in handlers {
                    handler.join().expect("join CDP test proxy connection");
                }
            });
            Self {
                endpoint: format!("ws://{addr}/{path}"),
                stalled,
                stop,
                thread: Some(thread),
            }
        }

        fn endpoint(&self) -> &str {
            &self.endpoint
        }

        fn stalled(&self) -> bool {
            self.stalled.load(Ordering::SeqCst)
        }
    }

    impl Drop for StallingCdpProxy {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::Relaxed);
            let _ = TcpStream::connect(
                self.endpoint
                    .strip_prefix("ws://")
                    .and_then(|endpoint| endpoint.split_once('/'))
                    .map(|(addr, _)| addr)
                    .expect("CDP test proxy endpoint address"),
            );
            if let Some(thread) = self.thread.take() {
                thread.join().expect("join CDP test proxy");
            }
        }
    }

    struct InputRestoringCdpProxy {
        endpoint: String,
        restored: Arc<AtomicBool>,
        stop: Arc<AtomicBool>,
        thread: Option<thread::JoinHandle<()>>,
    }

    impl InputRestoringCdpProxy {
        fn start(upstream_endpoint: &str, window_id: i64) -> Self {
            let upstream = upstream_endpoint
                .strip_prefix("ws://")
                .expect("test browser endpoint should use ws");
            let (upstream_addr, path) = upstream
                .split_once('/')
                .expect("test browser endpoint should contain a path");
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind input CDP test proxy");
            listener
                .set_nonblocking(true)
                .expect("set input CDP test proxy nonblocking");
            let addr = listener.local_addr().expect("input CDP test proxy address");
            let restored = Arc::new(AtomicBool::new(false));
            let stop = Arc::new(AtomicBool::new(false));
            let thread_restored = Arc::clone(&restored);
            let thread_stop = Arc::clone(&stop);
            let upstream_addr = upstream_addr.to_owned();
            let upstream_endpoint = upstream_endpoint.to_owned();
            let thread = thread::spawn(move || {
                let mut handlers = Vec::new();
                while !thread_stop.load(Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((client, _)) => {
                            if thread_stop.load(Ordering::Relaxed) {
                                break;
                            }
                            let handler_upstream_addr = upstream_addr.clone();
                            let handler_upstream_endpoint = upstream_endpoint.clone();
                            let handler_restored = Arc::clone(&thread_restored);
                            handlers.push(thread::spawn(move || {
                                proxy_cdp_restoring_before_input(
                                    client,
                                    &handler_upstream_addr,
                                    &handler_upstream_endpoint,
                                    window_id,
                                    &handler_restored,
                                )
                            }));
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(2));
                        }
                        Err(error) => panic!("input CDP test proxy accept failed: {error}"),
                    }
                }
                for handler in handlers {
                    handler
                        .join()
                        .expect("join input CDP test proxy connection");
                }
            });
            Self {
                endpoint: format!("ws://{addr}/{path}"),
                restored,
                stop,
                thread: Some(thread),
            }
        }

        fn endpoint(&self) -> &str {
            &self.endpoint
        }

        fn restored(&self) -> bool {
            self.restored.load(Ordering::SeqCst)
        }
    }

    impl Drop for InputRestoringCdpProxy {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::Relaxed);
            let _ = TcpStream::connect(
                self.endpoint
                    .strip_prefix("ws://")
                    .and_then(|endpoint| endpoint.split_once('/'))
                    .map(|(addr, _)| addr)
                    .expect("input CDP test proxy endpoint address"),
            );
            if let Some(thread) = self.thread.take() {
                thread.join().expect("join input CDP test proxy");
            }
        }
    }

    fn proxy_cdp_restoring_before_input(
        mut client: TcpStream,
        upstream_addr: &str,
        upstream_endpoint: &str,
        window_id: i64,
        restored: &AtomicBool,
    ) {
        client
            .set_nonblocking(false)
            .expect("set input CDP test client blocking");
        let mut upstream = TcpStream::connect(upstream_addr).expect("connect input CDP upstream");
        let request = read_http_headers(&mut client).expect("read input CDP upgrade request");
        upstream
            .write_all(&request)
            .expect("forward input CDP upgrade request");
        let response = read_http_headers(&mut upstream).expect("read input CDP upgrade response");
        client
            .write_all(&response)
            .expect("forward input CDP upgrade response");

        let mut upstream_reader = upstream
            .try_clone()
            .expect("clone input CDP upstream stream");
        let mut client_writer = client.try_clone().expect("clone input CDP client stream");
        let upstream_to_client = thread::spawn(move || {
            let _ = std::io::copy(&mut upstream_reader, &mut client_writer);
        });
        while let Ok(frame) = read_websocket_frame(&mut client) {
            if frame[0] & 0x0f == 1
                && serde_json::from_slice::<Value>(&test_websocket_payload(&frame))
                    .ok()
                    .and_then(|command| command["method"].as_str().map(str::to_owned))
                    .is_some_and(|method| method == "Input.dispatchMouseEvent")
                && !restored.swap(true, Ordering::SeqCst)
            {
                // This Chromium does not route physical input to a minimized window. Keep the
                // page hidden through actionability, then restore only when that real path emits
                // its first input command. A stalled actionability probe never reaches this point.
                let mut control = connect_test_websocket(upstream_endpoint);
                send_test_cdp_command(
                    &mut control,
                    1,
                    "Browser.setWindowBounds",
                    json!({
                        "windowId": window_id,
                        "bounds": { "windowState": "normal" },
                    }),
                );
            }
            upstream
                .write_all(&frame)
                .expect("forward input CDP websocket frame");
        }
        let _ = upstream.shutdown(std::net::Shutdown::Both);
        upstream_to_client
            .join()
            .expect("join input CDP upstream relay");
    }

    fn proxy_cdp_connection(
        mut client: TcpStream,
        upstream_addr: &str,
        stall_second_data_frame: bool,
        stalled: &AtomicBool,
    ) {
        client
            .set_nonblocking(false)
            .expect("set CDP test client blocking");
        let mut upstream = TcpStream::connect(upstream_addr).expect("connect CDP test upstream");
        let request = read_http_headers(&mut client).expect("read CDP test upgrade request");
        upstream
            .write_all(&request)
            .expect("forward CDP test upgrade request");
        let response = read_http_headers(&mut upstream).expect("read CDP test upgrade response");
        client
            .write_all(&response)
            .expect("forward CDP test upgrade response");

        if !stall_second_data_frame {
            relay_bidirectionally(client, upstream);
            return;
        }

        client
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("set CDP test client timeout");
        let mut upstream_reader = upstream
            .try_clone()
            .expect("clone CDP test upstream stream");
        let mut client_writer = client.try_clone().expect("clone CDP test client stream");
        let upstream_to_client = thread::spawn(move || {
            let _ = std::io::copy(&mut upstream_reader, &mut client_writer);
        });

        let mut data_frames = 0;
        while let Ok(frame) = read_websocket_frame(&mut client) {
            let opcode = frame[0] & 0x0f;
            if matches!(opcode, 1 | 2) {
                data_frames += 1;
            }
            if data_frames == 2 {
                stalled.store(true, Ordering::SeqCst);
                let mut discard = [0_u8; 1024];
                while client.read(&mut discard).is_ok_and(|read| read > 0) {}
                break;
            }
            upstream
                .write_all(&frame)
                .expect("forward CDP test websocket frame");
        }
        let _ = upstream.shutdown(std::net::Shutdown::Both);
        upstream_to_client
            .join()
            .expect("join CDP test upstream relay");
    }

    fn read_http_headers(stream: &mut TcpStream) -> std::io::Result<Vec<u8>> {
        let mut headers = Vec::new();
        while !headers.ends_with(b"\r\n\r\n") {
            if headers.len() >= 64 * 1024 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "CDP test upgrade headers are too large",
                ));
            }
            let mut byte = [0_u8; 1];
            stream.read_exact(&mut byte)?;
            headers.push(byte[0]);
        }
        Ok(headers)
    }

    fn read_websocket_frame(stream: &mut TcpStream) -> std::io::Result<Vec<u8>> {
        let mut header = [0_u8; 2];
        stream.read_exact(&mut header)?;
        let masked = header[1] & 0x80 != 0;
        let mut frame = header.to_vec();
        let payload_len = match header[1] & 0x7f {
            126 => {
                let mut extended = [0_u8; 2];
                stream.read_exact(&mut extended)?;
                frame.extend_from_slice(&extended);
                u64::from(u16::from_be_bytes(extended))
            }
            127 => {
                let mut extended = [0_u8; 8];
                stream.read_exact(&mut extended)?;
                frame.extend_from_slice(&extended);
                u64::from_be_bytes(extended)
            }
            payload_len => u64::from(payload_len),
        };
        if masked {
            let mut mask = [0_u8; 4];
            stream.read_exact(&mut mask)?;
            frame.extend_from_slice(&mask);
        }
        let payload_len = usize::try_from(payload_len).map_err(|_| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "CDP test websocket frame is too large",
            )
        })?;
        let frame_start = frame.len();
        frame.resize(frame_start + payload_len, 0);
        stream.read_exact(&mut frame[frame_start..])?;
        Ok(frame)
    }

    fn relay_bidirectionally(mut client: TcpStream, mut upstream: TcpStream) {
        let mut upstream_reader = upstream
            .try_clone()
            .expect("clone transparent CDP test upstream stream");
        let mut client_writer = client
            .try_clone()
            .expect("clone transparent CDP test client stream");
        let upstream_to_client = thread::spawn(move || {
            let _ = std::io::copy(&mut upstream_reader, &mut client_writer);
        });
        let _ = std::io::copy(&mut client, &mut upstream);
        let _ = client.shutdown(std::net::Shutdown::Both);
        let _ = upstream.shutdown(std::net::Shutdown::Both);
        upstream_to_client
            .join()
            .expect("join transparent CDP test relay");
    }

    fn connect_test_websocket(ws_endpoint: &str) -> TcpStream {
        let (authority, path) = ws_endpoint
            .strip_prefix("ws://")
            .and_then(|endpoint| endpoint.split_once('/'))
            .expect("test browser should expose a local WebSocket endpoint");
        let mut stream = TcpStream::connect(authority).expect("connect test browser endpoint");
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .expect("set test browser endpoint read timeout");
        write!(
            stream,
            "GET /{path} HTTP/1.1\r\nHost: {authority}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        .expect("request test browser WebSocket upgrade");
        let headers =
            read_http_headers(&mut stream).expect("read test browser WebSocket upgrade response");
        let headers = String::from_utf8_lossy(&headers);
        assert!(
            headers.starts_with("HTTP/1.1 101"),
            "test browser WebSocket upgrade failed: {headers}"
        );
        stream
    }

    fn write_test_websocket_text(stream: &mut TcpStream, text: &str) {
        let payload = text.as_bytes();
        let mut frame = Vec::with_capacity(payload.len() + 8);
        frame.push(0x81);
        if payload.len() < 126 {
            frame.push(0x80 | payload.len() as u8);
        } else {
            frame.push(0x80 | 126);
            frame.extend_from_slice(
                &u16::try_from(payload.len())
                    .expect("test WebSocket payload should fit in u16")
                    .to_be_bytes(),
            );
        }
        let mask = [0x12_u8, 0x34, 0x56, 0x78];
        frame.extend_from_slice(&mask);
        frame.extend(
            payload
                .iter()
                .enumerate()
                .map(|(index, byte)| byte ^ mask[index % mask.len()]),
        );
        stream
            .write_all(&frame)
            .expect("write test browser WebSocket frame");
    }

    fn test_websocket_payload(frame: &[u8]) -> Vec<u8> {
        assert!(frame.len() >= 2, "test WebSocket frame is truncated");
        let mut cursor = 2;
        let payload_len = match frame[1] & 0x7f {
            126 => {
                let bytes: [u8; 2] = frame[cursor..cursor + 2]
                    .try_into()
                    .expect("test WebSocket extended length");
                cursor += 2;
                usize::from(u16::from_be_bytes(bytes))
            }
            127 => {
                let bytes: [u8; 8] = frame[cursor..cursor + 8]
                    .try_into()
                    .expect("test WebSocket extended length");
                cursor += 8;
                usize::try_from(u64::from_be_bytes(bytes))
                    .expect("test WebSocket payload should fit in usize")
            }
            len => usize::from(len),
        };
        let mask = if frame[1] & 0x80 != 0 {
            let mask: [u8; 4] = frame[cursor..cursor + 4]
                .try_into()
                .expect("test WebSocket mask");
            cursor += 4;
            Some(mask)
        } else {
            None
        };
        let payload = &frame[cursor..cursor + payload_len];
        mask.map_or_else(
            || payload.to_vec(),
            |mask| {
                payload
                    .iter()
                    .enumerate()
                    .map(|(index, byte)| byte ^ mask[index % mask.len()])
                    .collect()
            },
        )
    }

    fn send_test_cdp_command(
        stream: &mut TcpStream,
        id: u64,
        method: &str,
        params: Value,
    ) -> Value {
        write_test_websocket_text(
            stream,
            &json!({ "id": id, "method": method, "params": params }).to_string(),
        );
        loop {
            let frame = read_websocket_frame(stream).expect("read test browser CDP response");
            if frame[0] & 0x0f != 1 {
                continue;
            }
            let response: Value = serde_json::from_slice(&test_websocket_payload(&frame))
                .expect("decode test browser CDP response");
            if response["id"] != json!(id) {
                continue;
            }
            assert!(
                response.get("error").is_none(),
                "test browser CDP command {method} failed: {response}"
            );
            return response["result"].clone();
        }
    }

    fn minimize_test_page(browser: &Browser, page: &Page) -> i64 {
        let mut stream = connect_test_websocket(&browser.ws_endpoint());
        let window = send_test_cdp_command(
            &mut stream,
            1,
            "Browser.getWindowForTarget",
            json!({ "targetId": page.target_id() }),
        );
        let window_id = window["windowId"]
            .as_i64()
            .expect("test page should belong to a browser window");
        send_test_cdp_command(
            &mut stream,
            2,
            "Browser.setWindowBounds",
            json!({
                "windowId": window_id,
                "bounds": { "windowState": "minimized" },
            }),
        );
        window_id
    }

    fn browser_test_lock() -> &'static tokio::sync::Mutex<()> {
        static LOCK: OnceLock<tokio::sync::Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| tokio::sync::Mutex::new(()))
    }

    fn request_id(value: i64) -> RequestId {
        RequestId::Number(value)
    }

    fn snapshot_ref(snapshot: &str, role: &str, name: &str) -> String {
        let line = snapshot
            .lines()
            .find(|line| line.contains(&format!("- {role}")) && line.contains(name))
            .unwrap_or_else(|| panic!("{role} {name:?} missing from snapshot:\n{snapshot}"));
        let marker = "[ref=";
        let start = line.find(marker).expect("snapshot ref start") + marker.len();
        let end = line[start..].find(']').expect("snapshot ref end") + start;
        line[start..end].to_owned()
    }

    fn output_text(output: &BrowserOutput) -> &str {
        match output {
            BrowserOutput::Text(text) | BrowserOutput::ShapedText { text, .. } => text,
            BrowserOutput::Image { bytes, mime, .. } => {
                panic!("expected a text output, got {} {mime} bytes", bytes.len())
            }
        }
    }

    #[test]
    fn network_detail_text_bounding_reports_inline_and_file_caps() {
        let inline = bounded_network_detail_text("éclair", 2, "request body", true);
        assert_eq!(
            inline,
            "é\n(request body truncated to 2 bytes inline; use filename for a larger bounded body)"
        );
        let file = bounded_network_detail_text("abcdef", 3, "request body", false);
        assert_eq!(file, "abc\n(request body truncated to 3 of 6 bytes)");
    }

    #[test]
    fn network_filter_compiles_once_and_matches_supported_regex_surface() {
        let regex = NetworkRegex::compile("(?i)api/data$|large-text$").unwrap();
        assert!(regex.is_match("https://example.test/API/DATA"));
        assert!(regex.is_match("https://example.test/large-text"));
        assert!(!regex.is_match("https://example.test/image.png"));
        assert!(NetworkRegex::compile("[").is_err());
        assert!(NetworkRegex::compile("(group)").is_err());
        assert!(NetworkRegex::compile(r"\bapi\b").is_err());
        assert!(NetworkRegex::compile("api{2}").is_err());
    }

    #[test]
    fn file_upload_preconditions_preserve_error_precedence_and_multiplicity() {
        assert_eq!(
            validate_file_upload_preconditions(None, true)
                .unwrap_err()
                .to_string(),
            "no file chooser is pending"
        );
        assert_eq!(
            validate_file_upload_preconditions(Some(false), true)
                .unwrap_err()
                .to_string(),
            "a dialog is pending; handle it before the file chooser"
        );
        assert_eq!(
            validate_file_upload_multiplicity(false, 2)
                .unwrap_err()
                .to_string(),
            "the pending file chooser accepts only one file"
        );
        assert!(validate_file_upload_multiplicity(false, 1).is_ok());
        assert!(validate_file_upload_multiplicity(true, 2).is_ok());
    }

    #[test]
    fn file_input_confinement_rejects_outside_non_file_oversized_and_too_many() {
        static WORKSPACE_ENV_LOCK: Mutex<()> = Mutex::new(());
        static COUNTER: AtomicUsize = AtomicUsize::new(1);

        let _environment = WORKSPACE_ENV_LOCK.lock().unwrap();
        let root = env::temp_dir().join(format!(
            "rustwright-mcp-confinement-{}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::SeqCst)
        ));
        let workspace = root.join("workspace");
        fs::create_dir_all(workspace.join("directory")).expect("create confinement workspace");
        fs::write(workspace.join("valid.txt"), b"confined").expect("write confined file");
        fs::write(root.join("outside.txt"), b"outside").expect("write outside file");
        let oversized = fs::File::create(workspace.join("oversized.bin"))
            .expect("create oversized sparse file");
        oversized
            .set_len(MAX_FILE_INPUT_BYTES + 1)
            .expect("size oversized sparse file");
        drop(oversized);

        let previous = env::var_os("RUSTWRIGHT_MCP_WORKSPACE");
        // SAFETY: this test holds its dedicated process-wide environment lock,
        // and no other actor unit test reads or writes this variable.
        unsafe { env::set_var("RUSTWRIGHT_MCP_WORKSPACE", &workspace) };

        assert_eq!(
            confine_workspace_file("valid.txt").unwrap(),
            workspace.join("valid.txt").canonicalize().unwrap()
        );
        assert!(
            confine_workspace_file(root.join("outside.txt").to_str().unwrap())
                .unwrap_err()
                .to_string()
                .contains("confined to RUSTWRIGHT_MCP_WORKSPACE")
        );
        assert!(
            confine_workspace_file("directory")
                .unwrap_err()
                .to_string()
                .contains("not a regular file")
        );
        assert!(
            confine_workspace_file("oversized.bin")
                .unwrap_err()
                .to_string()
                .contains("per-file cap")
        );
        assert!(
            confine_workspace_files(&vec!["valid.txt".to_owned(); MAX_FILE_INPUTS + 1])
                .unwrap_err()
                .to_string()
                .contains("limited to 50 files")
        );

        // SAFETY: the same dedicated lock remains held until restoration.
        unsafe {
            if let Some(previous) = previous {
                env::set_var("RUSTWRIGHT_MCP_WORKSPACE", previous);
            } else {
                env::remove_var("RUSTWRIGHT_MCP_WORKSPACE");
            }
        }
        fs::remove_dir_all(root).expect("remove confinement fixture");
    }

    fn process_rows() -> Vec<(u32, u32)> {
        let output = Command::new("ps")
            .args(["-axo", "pid=,ppid="])
            .output()
            .expect("run process listing");
        String::from_utf8_lossy(&output.stdout)
            .lines()
            .filter_map(|line| {
                let mut fields = line.split_whitespace();
                Some((fields.next()?.parse().ok()?, fields.next()?.parse().ok()?))
            })
            .collect()
    }

    fn descendants(root: u32) -> Vec<u32> {
        let rows = process_rows();
        let mut descendants = Vec::new();
        let mut parents = vec![root];
        while let Some(parent) = parents.pop() {
            for (pid, ppid) in &rows {
                if *ppid == parent && !descendants.contains(pid) {
                    descendants.push(*pid);
                    parents.push(*pid);
                }
            }
        }
        descendants
    }

    async fn actor() -> Option<Arc<BrowserActor>> {
        if chromium().executable_path().is_none() {
            eprintln!("skipping actor cancellation test: Chromium executable unavailable");
            return None;
        }
        let actor = Arc::new(BrowserActor::spawn());
        actor
            .execute_with_timeout(
                request_id(0),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(30),
            )
            .await
            .expect("warm browser actor");
        Some(actor)
    }

    async fn wait_until_in_flight(actor: &BrowserActor, id: &RequestId) {
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                if actor
                    .shared
                    .queue
                    .lock()
                    .unwrap()
                    .in_flight
                    .as_ref()
                    .is_some_and(|in_flight| &in_flight.request_id == id)
                {
                    return;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("command should become in-flight");
    }

    async fn cancel_hanging_navigation(
        actor: &Arc<BrowserActor>,
        server: &HangingServer,
        id: i64,
    ) -> (BrowserResult, Duration) {
        let command_id = request_id(id);
        let command_actor = Arc::clone(actor);
        let url = server.url();
        let command = tokio::spawn(async move {
            command_actor
                .execute_with_timeout(
                    command_id,
                    BrowserOp::Navigate(url),
                    Duration::from_secs(30),
                )
                .await
        });
        wait_until_in_flight(actor, &request_id(id)).await;
        tokio::time::sleep(Duration::from_millis(50)).await;
        let started = Instant::now();
        assert!(actor.cancel(&request_id(id)));
        let result = tokio::time::timeout(Duration::from_secs(1), command)
            .await
            .expect("cancelled navigation should return within one second")
            .expect("navigation task should not panic");
        (result, started.elapsed())
    }

    #[test]
    fn tool_timeout_defaults_and_clamps() {
        assert_eq!(
            tool_timeout_from_value(None),
            Duration::from_millis(DEFAULT_TOOL_TIMEOUT_MS)
        );
        assert_eq!(
            tool_timeout_from_value(Some("invalid")),
            Duration::from_millis(DEFAULT_TOOL_TIMEOUT_MS)
        );
        assert_eq!(
            tool_timeout_from_value(Some("100")),
            Duration::from_millis(MIN_TOOL_TIMEOUT_MS)
        );
        assert_eq!(
            tool_timeout_from_value(Some("900000")),
            Duration::from_millis(MAX_TOOL_TIMEOUT_MS)
        );
        assert_eq!(
            tool_timeout_from_value(Some("42000")),
            Duration::from_millis(42_000)
        );
        assert_eq!(
            sensitive_tracking_expiry_ms(Duration::from_millis(MAX_TOOL_TIMEOUT_MS)),
            MAX_TOOL_TIMEOUT_MS + SENSITIVE_TRACKING_CLEANUP_GRACE_MS
        );
        assert!(
            BEGIN_SENSITIVE_SNAPSHOT_TRACKING_JS
                .contains("Math.max(1_000, Number(input.expiryMs) || 1_000)"),
            "page-side sensitive tracking must use the caller's deadline-derived expiry"
        );
    }

    #[test]
    fn engine_timeout_preserves_request_budget_above_thirty_seconds() {
        assert_eq!(
            BrowserState::engine_timeout(Duration::from_secs(45)),
            46_000.0
        );
    }

    #[test]
    fn failed_post_click_snapshot_leaves_old_ref_stale() {
        let mut state = BrowserState::default();
        state.current_refs.insert("e7".to_owned());
        let dispatches = std::cell::Cell::new(0);

        let result = state.dispatch_ref_action(
            "e7",
            |_| {
                dispatches.set(dispatches.get() + 1);
                Ok(())
            },
            |_| {
                Err(BrowserError::Message(
                    "post-click snapshot failed".to_owned(),
                ))
            },
        );
        assert_eq!(
            result,
            Err(BrowserError::Message(
                "post-click snapshot failed".to_owned()
            ))
        );
        assert!(state.current_refs.is_empty());

        let retry = state.dispatch_ref_action(
            "e7",
            |_| {
                dispatches.set(dispatches.get() + 1);
                Ok(())
            },
            |_| Ok("unexpected snapshot".to_owned()),
        );
        assert!(matches!(
            retry,
            Err(BrowserError::Message(message)) if message.contains("unknown or stale ref e7")
        ));
        assert_eq!(
            dispatches.get(),
            1,
            "stale retry must not re-dispatch click"
        );
    }

    #[test]
    fn committed_action_survives_a_failed_post_action_snapshot() {
        assert_eq!(
            committed_snapshot_result(Ok("- page snapshot".to_owned())),
            Ok("- page snapshot".to_owned()),
            "a successful observation must pass through untouched"
        );

        // `Timeout` is the failure this exists for. The post-action snapshot
        // passes `None` for the cancel token, so a late cancel cannot reach it,
        // but `BrowserState::remaining` still turns an elapsed request budget
        // into `Timeout` -- so a key press that commits microseconds before the
        // deadline used to be reported as a failed key press.
        let degraded = committed_snapshot_result(Err(BrowserError::Timeout(5_000)))
            .expect("a committed action must not fail because its observation did");
        assert!(
            degraded.contains("Action completed"),
            "response must say the action succeeded: {degraded}"
        );
        assert!(
            degraded.contains("browser command timed out after 5000 ms"),
            "response must keep the observation's cause: {degraded}"
        );
        assert!(
            degraded.contains("browser_snapshot"),
            "response must tell the caller how to recover state: {degraded}"
        );

        assert!(
            committed_snapshot_result(Err(BrowserError::Cancelled)).is_ok(),
            "cancellation is equally too late once the action has committed"
        );
    }

    #[test]
    fn every_committed_input_tool_takes_its_post_action_snapshot_through_the_helper() {
        // `committed_snapshot_result` is only worth anything if the committed
        // tools actually route through it, and that wiring is what a later
        // refactor would quietly undo -- the degradation itself would keep
        // passing its own test while a key press committing just before its
        // deadline went back to reporting `Timeout`. Asserting over the source
        // pins the wiring without a browser.
        //
        // Split at the test module so the literals below -- which live in it --
        // cannot match themselves. That self-match is exactly what made an
        // earlier attempt at this test count two phantom call sites.
        //
        // Match the whole unindented module header, not a bare `#[cfg(test)]`:
        // there is a test-only field earlier in the file carrying that
        // attribute, and splitting on it silently truncated production to a
        // prefix holding none of the call sites -- which reads as "zero
        // bypasses" for the assertion below rather than as a broken test.
        const TEST_MODULE: &str = "\n#[cfg(test)]\nmod tests {";
        let source = include_str!("actor.rs");
        assert_eq!(
            source.matches(TEST_MODULE).count(),
            1,
            "the production/test split point must be unambiguous"
        );
        let production = source
            .split_once(TEST_MODULE)
            .expect("actor.rs must have a test module to split on")
            .0;

        // Only the helper is allowed to take a post-action snapshot that ignores
        // the cancel token. A tool that reached past it would have to spell this
        // out itself, which shows up here as a second occurrence.
        assert_eq!(
            production
                .matches("snapshot_with_cancel(request, None)")
                .count(),
            1,
            "an uncancellable post-action snapshot must only be taken inside \
             committed_post_action_snapshot"
        );

        // The other way to bypass the helper is to call the cancellable snapshot
        // directly, which leaves this count short instead. Nine committed
        // physical-action paths: click, type-with-submit, hover, drag, press_key
        // in both its targeted and untargeted forms, fill_form's complete and
        // partial-completion exits, and the exit where a password field was only
        // partially written. A tenth path should fail here until someone confirms
        // it wants the same treatment.
        assert_eq!(
            production
                .matches("committed_post_action_snapshot(request)")
                .count(),
            9,
            "every committed physical action must observe through the helper"
        );
    }

    #[test]
    fn drag_ref_pair_rejects_stale_start_and_end_before_dispatch() {
        for (start, end, stale) in [("e9", "e2", "e9"), ("e1", "e9", "e9")] {
            let mut state = BrowserState::default();
            state
                .current_refs
                .extend(["e1".to_owned(), "e2".to_owned()]);
            let dispatches = std::cell::Cell::new(0);
            let result = state.dispatch_ref_pair_action(
                start,
                end,
                |_| {
                    dispatches.set(dispatches.get() + 1);
                    Ok(())
                },
                |_| Ok("unexpected snapshot".to_owned()),
            );
            assert!(matches!(
                result,
                Err(BrowserError::Message(message))
                    if message.contains(&format!("unknown or stale ref {stale}"))
            ));
            assert_eq!(dispatches.get(), 0);
            assert_eq!(
                state.current_refs,
                HashSet::from(["e1".to_owned(), "e2".to_owned()])
            );
        }
    }

    #[test]
    fn drag_result_uses_endpoint_descriptions_or_refs_and_includes_snapshot() {
        assert_eq!(
            render_drag_result("Source card", "Destination lane", "- status \"done\""),
            "### Result\nDragged Source card to Destination lane.\n\n### Snapshot\n- status \"done\""
        );
        assert_eq!(
            render_drag_result("e4", "e7", "- status \"done\""),
            "### Result\nDragged e4 to e7.\n\n### Snapshot\n- status \"done\""
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn queued_cancel_removes_command_immediately_without_touching_navigation() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = HangingServer::start();
        let navigation_actor = Arc::clone(&actor);
        let navigation_url = server.url();
        let navigation = tokio::spawn(async move {
            navigation_actor
                .execute_with_timeout(
                    request_id(1),
                    BrowserOp::Navigate(navigation_url),
                    Duration::from_secs(30),
                )
                .await
        });
        wait_until_in_flight(&actor, &request_id(1)).await;

        let snapshot_actor = Arc::clone(&actor);
        let snapshot = tokio::spawn(async move {
            snapshot_actor
                .execute_with_timeout(
                    request_id(2),
                    BrowserOp::Snapshot {
                        target: None,
                        depth: None,
                        boxes: false,
                    },
                    Duration::from_secs(30),
                )
                .await
        });
        tokio::time::timeout(Duration::from_secs(1), async {
            while actor.shared.queued_len() != 1 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("snapshot should queue behind navigation");

        let started = Instant::now();
        assert!(actor.cancel(&request_id(2)));
        let snapshot_result = tokio::time::timeout(Duration::from_millis(250), snapshot)
            .await
            .expect("queued cancellation should resolve immediately")
            .expect("snapshot task should not panic");
        assert_eq!(snapshot_result, Err(BrowserError::Cancelled));
        assert!(started.elapsed() < Duration::from_millis(250));
        assert!(
            !navigation.is_finished(),
            "navigation must remain unaffected"
        );

        assert!(actor.cancel(&request_id(1)));
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(1), navigation)
                .await
                .expect("navigation cleanup should be prompt")
                .expect("navigation task should not panic"),
            Err(BrowserError::Cancelled)
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn in_flight_cancel_is_prompt_and_actor_remains_healthy() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = HangingServer::start();
        let (result, latency) = cancel_hanging_navigation(&actor, &server, 10).await;
        eprintln!("measured in-flight cancellation latency: {latency:?}");
        assert_eq!(result, Err(BrowserError::Cancelled));
        assert!(
            latency < Duration::from_millis(250),
            "cancel latency was {latency:?}"
        );
        actor
            .execute_with_timeout(
                request_id(11),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("snapshot should succeed after cancellation");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn deadline_expiry_is_typed_and_actor_remains_healthy() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = HangingServer::start();
        let started = Instant::now();
        let result = actor
            .execute_with_timeout(
                request_id(20),
                BrowserOp::Navigate(server.url()),
                Duration::from_millis(1),
            )
            .await;
        assert_eq!(result, Err(BrowserError::Timeout(1)));
        assert!(started.elapsed() < Duration::from_secs(1));
        actor
            .execute_with_timeout(
                request_id(21),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("snapshot should succeed after deadline");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn deadline_engine_wakeup_before_reason_publication_surfaces_timeout() {
        let _guard = browser_test_lock().lock().await;
        let shared = Arc::new(ActorShared::new());
        let cancellation = Arc::new(CommandCancellation::new());
        let (reply, _response) = oneshot::channel();
        shared
            .submit(ActorRequest {
                request_id: request_id(67_200),
                op: BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                cancellation: Arc::clone(&cancellation),
                deadline: Instant::now(),
                timeout_ms: 73,
                reply,
            })
            .expect("submit deterministic deadline request");
        let request = shared.next().expect("take deterministic deadline request");

        // Drive the verified race ordering directly. The worker stands in for
        // an engine operation waiting on the token: it observes the persistent
        // engine wake first, but cannot complete until the deadline path has
        // attempted to publish its request-level reason. No scheduler timing or
        // sleep determines the ordering in this regression.
        let engine_awake = Arc::new(std::sync::Barrier::new(2));
        let finish_engine_error = Arc::new(std::sync::Barrier::new(2));
        let worker_awake = Arc::clone(&engine_awake);
        let worker_finish = Arc::clone(&finish_engine_error);
        let worker_cancellation = Arc::clone(&cancellation);
        let worker_shared = Arc::clone(&shared);
        let worker = thread::spawn(move || {
            while !worker_cancellation.engine.is_cancelled() {
                thread::yield_now();
            }
            worker_awake.wait();
            worker_finish.wait();
            worker_shared.complete::<String>(&request, Err(BrowserError::Cancelled))
        });

        assert!(
            cancellation.engine.try_cancel(),
            "the engine wake must win the initial cancellation arbitration"
        );
        engine_awake.wait();
        let _ = cancellation.cancel(CancellationReason::Deadline);
        finish_engine_error.wait();

        let result = worker.join().expect("join deterministic engine waiter");
        assert_eq!(
            result,
            Err(BrowserError::Timeout(73)),
            "a deadline-triggered engine wake must never surface as operator cancellation"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn remote_pages_deadline_is_prompt_and_actor_remains_healthy() {
        let _guard = browser_test_lock().lock().await;
        if chromium().executable_path().is_none() {
            eprintln!("skipping remote pages deadline test: Chromium executable unavailable");
            return;
        }
        let (owner, page) = tokio::task::spawn_blocking(|| {
            let owner = chromium()
                .launch(LaunchOptions::default().arg("--remote-debugging-port=0"))
                .expect("launch remote pages deadline browser");
            let page = owner.new_page().expect("create remote pages deadline page");
            (owner, page)
        })
        .await
        .expect("join remote pages deadline browser launch");

        let already_cancelled = CancelToken::new();
        already_cancelled.cancel();
        let cancelled_at = Instant::now();
        assert!(matches!(
            owner.pages_with_cancel(Duration::from_secs(30), Some(&already_cancelled)),
            Err(Error::Cancelled)
        ));
        assert!(
            cancelled_at.elapsed() < Duration::from_millis(250),
            "an already-cancelled page listing should return promptly"
        );

        let proxy = StallingCdpProxy::start(&owner.ws_endpoint());
        let actor = BrowserActor::spawn_with_startup(BrowserStartup::Remote(
            ConnectOptions::new(proxy.endpoint()).timeout(Duration::from_secs(10)),
        ));
        let started = Instant::now();
        let result = actor
            .execute_with_timeout(
                request_id(22),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(1),
            )
            .await;
        assert_eq!(result, Err(BrowserError::Timeout(1_000)));
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "the page-listing deadline must not inherit the 30-second facade timeout"
        );
        assert!(
            proxy.stalled(),
            "the first remote connection must reach the page-listing CDP request"
        );

        actor
            .execute_with_timeout(
                request_id(23),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(10),
            )
            .await
            .expect("actor should recover after a remote page-listing deadline");

        drop(actor);
        tokio::task::spawn_blocking(move || {
            drop(page);
            owner.close().expect("close remote pages deadline browser");
        })
        .await
        .expect("join remote pages deadline browser close");
        drop(proxy);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn cold_start_deadline_covers_lazy_browser_initialization() {
        let _guard = browser_test_lock().lock().await;
        if chromium().executable_path().is_none() {
            eprintln!("skipping cold-start deadline test: Chromium executable unavailable");
            return;
        }
        let actor = BrowserActor::spawn();
        let result = actor
            .execute_with_timeout(
                request_id(25),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_millis(100),
            )
            .await;
        assert_eq!(result, Err(BrowserError::Timeout(100)));
        actor
            .execute_with_timeout(
                request_id(26),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(30),
            )
            .await
            .expect("actor should recover after a cold-start deadline");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn queue_overflow_returns_busy_without_waiting() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = HangingServer::start();
        let navigation_actor = Arc::clone(&actor);
        let navigation = tokio::spawn(async move {
            navigation_actor
                .execute_with_timeout(
                    request_id(30),
                    BrowserOp::Navigate(server.url()),
                    Duration::from_secs(30),
                )
                .await
        });
        wait_until_in_flight(&actor, &request_id(30)).await;

        let mut queued = Vec::new();
        for offset in 0..COMMAND_QUEUE_CAPACITY as i64 {
            let queued_actor = Arc::clone(&actor);
            queued.push(tokio::spawn(async move {
                queued_actor
                    .execute_with_timeout(
                        request_id(100 + offset),
                        BrowserOp::Snapshot {
                            target: None,
                            depth: None,
                            boxes: false,
                        },
                        Duration::from_secs(30),
                    )
                    .await
            }));
        }
        tokio::time::timeout(Duration::from_secs(1), async {
            while actor.shared.queued_len() != COMMAND_QUEUE_CAPACITY {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("queue should fill to capacity");

        let started = Instant::now();
        assert_eq!(
            actor
                .execute_with_timeout(
                    request_id(999),
                    BrowserOp::Snapshot {
                        target: None,
                        depth: None,
                        boxes: false
                    },
                    Duration::from_secs(30),
                )
                .await,
            Err(BrowserError::Busy)
        );
        assert!(started.elapsed() < Duration::from_millis(100));

        for offset in 0..COMMAND_QUEUE_CAPACITY as i64 {
            assert!(actor.cancel(&request_id(100 + offset)));
        }
        assert!(actor.cancel(&request_id(30)));
        for task in queued {
            assert_eq!(task.await.unwrap(), Err(BrowserError::Cancelled));
        }
        assert_eq!(navigation.await.unwrap(), Err(BrowserError::Cancelled));
    }

    #[tokio::test(flavor = "current_thread")]
    async fn cancel_after_complete_is_a_no_op() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        actor
            .execute_with_timeout(
                request_id(40),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("completed snapshot");
        assert!(!actor.cancel(&request_id(40)));
        actor
            .execute_with_timeout(
                request_id(41),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("actor should remain healthy");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn twenty_rapid_cancel_submit_cycles_do_not_deadlock() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = HangingServer::start();
        for cycle in 0..20_i64 {
            let (result, _) = cancel_hanging_navigation(&actor, &server, 1_000 + cycle).await;
            assert_eq!(result, Err(BrowserError::Cancelled));
            actor
                .execute_with_timeout(
                    request_id(2_000 + cycle),
                    BrowserOp::Snapshot {
                        target: None,
                        depth: None,
                        boxes: false,
                    },
                    Duration::from_secs(5),
                )
                .await
                .unwrap_or_else(|error| panic!("cycle {cycle} snapshot failed: {error}"));
        }
        let browser_pids = descendants(std::process::id());
        assert!(
            !browser_pids.is_empty(),
            "expected browser subprocesses before actor shutdown"
        );
        drop(actor);
        let shutdown_deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let live: HashSet<u32> = process_rows().into_iter().map(|(pid, _)| pid).collect();
            let orphans = browser_pids
                .iter()
                .copied()
                .filter(|pid| live.contains(pid))
                .collect::<Vec<_>>();
            if orphans.is_empty() {
                break;
            }
            assert!(
                Instant::now() < shutdown_deadline,
                "orphan browser processes after hammer test: {orphans:?}"
            );
            thread::sleep(Duration::from_millis(25));
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn validation_fifty_cancel_recover_cycles_measure_distribution_and_leave_no_orphans() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = HangingServer::start();
        let mut latencies = Vec::with_capacity(50);
        for cycle in 0..50_i64 {
            let (result, latency) =
                cancel_hanging_navigation(&actor, &server, 10_000 + cycle).await;
            assert_eq!(result, Err(BrowserError::Cancelled));
            latencies.push(latency);
            actor
                .execute_with_timeout(
                    request_id(20_000 + cycle),
                    BrowserOp::Snapshot {
                        target: None,
                        depth: None,
                        boxes: false,
                    },
                    Duration::from_secs(5),
                )
                .await
                .unwrap_or_else(|error| panic!("cycle {cycle} recovery failed: {error}"));
        }
        latencies.sort_unstable();
        let p50 = latencies[24];
        let p95 = latencies[47];
        println!(
            "validation 50-cycle cancellation latency: p50={p50:?} p95={p95:?} min={:?} max={:?}",
            latencies[0], latencies[49]
        );

        let browser_pids = descendants(std::process::id());
        assert!(
            !browser_pids.is_empty(),
            "expected owned browser descendants"
        );
        drop(actor);
        let shutdown_deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let live: HashSet<u32> = process_rows().into_iter().map(|(pid, _)| pid).collect();
            let orphans = browser_pids
                .iter()
                .copied()
                .filter(|pid| live.contains(pid))
                .collect::<Vec<_>>();
            if orphans.is_empty() {
                println!("validation orphan browser descendants after shutdown: []");
                break;
            }
            assert!(
                Instant::now() < shutdown_deadline,
                "validation orphan browser descendants: {orphans:?}"
            );
            thread::sleep(Duration::from_millis(25));
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn validation_cancel_complete_race_twenty_times_has_only_owned_result() {
        use std::sync::Barrier;

        let mut successes = 0;
        let mut cancellations = 0;
        for cycle in 0..20_i64 {
            let shared = Arc::new(ActorShared::new());
            let id = request_id(30_000 + cycle);
            let cancellation = Arc::new(CommandCancellation::new());
            let (reply, response) = oneshot::channel();
            shared
                .submit(ActorRequest {
                    request_id: id.clone(),
                    op: BrowserOp::Snapshot {
                        target: None,
                        depth: None,
                        boxes: false,
                    },
                    cancellation,
                    deadline: Instant::now() + Duration::from_secs(1),
                    timeout_ms: 1_000,
                    reply,
                })
                .expect("submit validation race request");
            let request = shared.next().expect("take validation race request");
            let barrier = Arc::new(Barrier::new(2));
            let worker_barrier = Arc::clone(&barrier);
            let worker_shared = Arc::clone(&shared);
            let worker = thread::spawn(move || {
                worker_barrier.wait();
                if cycle % 2 == 0 {
                    thread::sleep(Duration::from_micros(100));
                }
                let result =
                    worker_shared.complete(&request, Ok(format!("validation-success-{cycle}")));
                let _ = request.reply.send(result);
            });
            barrier.wait();
            if cycle % 2 != 0 {
                thread::sleep(Duration::from_micros(100));
            }
            shared.cancel(&id, CancellationReason::Cancelled);
            let result = tokio::time::timeout(Duration::from_secs(1), response)
                .await
                .expect("validation race must not hang")
                .expect("validation race sender must survive");
            worker.join().expect("join validation race worker");
            match result {
                Ok(value) => {
                    assert_eq!(value, format!("validation-success-{cycle}"));
                    successes += 1;
                }
                Err(BrowserError::Cancelled) => cancellations += 1,
                other => panic!("wrong validation race result: {other:?}"),
            }
        }
        assert!(successes > 0 && cancellations > 0);
        println!(
            "validation cancel/complete races: success={successes} cancelled={cancellations} wrong=0 hangs=0"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn validation_three_queued_deadlines_expire_and_later_command_runs() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = HangingServer::start();
        let navigation_actor = Arc::clone(&actor);
        let navigation = tokio::spawn(async move {
            navigation_actor
                .execute_with_timeout(
                    request_id(40_000),
                    BrowserOp::Navigate(server.url()),
                    Duration::from_secs(30),
                )
                .await
        });
        wait_until_in_flight(&actor, &request_id(40_000)).await;

        let mut queued = Vec::new();
        for (offset, timeout_ms) in [80_u64, 120, 160].into_iter().enumerate() {
            let queued_actor = Arc::clone(&actor);
            queued.push((
                timeout_ms,
                tokio::spawn(async move {
                    queued_actor
                        .execute_with_timeout(
                            request_id(40_100 + offset as i64),
                            BrowserOp::Snapshot {
                                target: None,
                                depth: None,
                                boxes: false,
                            },
                            Duration::from_millis(timeout_ms),
                        )
                        .await
                }),
            ));
        }
        for (timeout_ms, task) in queued {
            let result = tokio::time::timeout(Duration::from_secs(1), task)
                .await
                .expect("queued deadline must resolve")
                .expect("queued deadline task must not panic");
            assert_eq!(result, Err(BrowserError::Timeout(timeout_ms)));
        }
        assert_eq!(actor.shared.queued_len(), 0);
        assert!(actor.cancel(&request_id(40_000)));
        assert_eq!(navigation.await.unwrap(), Err(BrowserError::Cancelled));
        actor
            .execute_with_timeout(
                request_id(40_200),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("later command must run after queued deadlines");
        println!("validation queued deadlines: [80, 120, 160] ms all typed Timeout; recovery=ok");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn validation_queue_overflow_is_immediate_drains_and_does_not_starve_later_work() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = HangingServer::start();
        let navigation_actor = Arc::clone(&actor);
        let navigation = tokio::spawn(async move {
            navigation_actor
                .execute_with_timeout(
                    request_id(50_000),
                    BrowserOp::Navigate(server.url()),
                    Duration::from_secs(30),
                )
                .await
        });
        wait_until_in_flight(&actor, &request_id(50_000)).await;

        let mut queued = Vec::new();
        for offset in 0..COMMAND_QUEUE_CAPACITY as i64 {
            let queued_actor = Arc::clone(&actor);
            queued.push(tokio::spawn(async move {
                queued_actor
                    .execute_with_timeout(
                        request_id(50_100 + offset),
                        BrowserOp::Snapshot {
                            target: None,
                            depth: None,
                            boxes: false,
                        },
                        Duration::from_secs(30),
                    )
                    .await
            }));
        }
        tokio::time::timeout(Duration::from_secs(1), async {
            while actor.shared.queued_len() != COMMAND_QUEUE_CAPACITY {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("validation queue should fill to 64");
        let overflow_at = Instant::now();
        assert_eq!(
            actor
                .execute_with_timeout(
                    request_id(50_999),
                    BrowserOp::Snapshot {
                        target: None,
                        depth: None,
                        boxes: false
                    },
                    Duration::from_secs(30),
                )
                .await,
            Err(BrowserError::Busy)
        );
        let overflow_latency = overflow_at.elapsed();
        assert!(overflow_latency < Duration::from_millis(100));

        for offset in 0..60_i64 {
            assert!(actor.cancel(&request_id(50_100 + offset)));
        }
        assert!(actor.cancel(&request_id(50_000)));
        assert_eq!(navigation.await.unwrap(), Err(BrowserError::Cancelled));
        for (offset, task) in queued.into_iter().enumerate() {
            let result = task.await.expect("validation queued task must not panic");
            if offset < 60 {
                assert_eq!(result, Err(BrowserError::Cancelled));
            } else {
                result.unwrap_or_else(|error| panic!("drained task {offset} starved: {error}"));
            }
        }
        assert_eq!(actor.shared.queued_len(), 0);
        actor
            .execute_with_timeout(
                request_id(51_000),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("post-drain validation command must not starve");
        println!(
            "validation queue: capacity=64 overflow={overflow_latency:?} typed=Busy drained=64 later=ok"
        );
    }

    struct ActionFixtureServer {
        addr: SocketAddr,
        captures: mpsc::Receiver<String>,
        stop: Arc<AtomicBool>,
        thread: Option<thread::JoinHandle<()>>,
        started: std::time::Instant,
        arm_gen: Arc<AtomicU64>,
        probe_resolved: Arc<AtomicU64>,
        arm_deadline_ms: Arc<AtomicU64>,
        arm_armed_ms: Arc<AtomicU64>,
        arm_path: Arc<Mutex<(u64, String)>>,
        /// Connection-level trace, printed only when the owning test is
        /// unwinding. This is the discriminator for the intermittent CI
        /// navigation stall: if Chromium reports `Network.requestWillBeSent`
        /// and this log shows no accept in the same window, the request never
        /// reached the fixture and the stall is browser-side, not server-side.
        /// Timings and connection indices only — never paths, headers, or
        /// bodies, so the password-masking properties stay intact.
        trace: Arc<std::sync::Mutex<Vec<String>>>,
    }

    // Bounds how long a handler can sit on a connection, and therefore how long
    // `Drop` can hold the browser test lock while joining handlers. The old 30s
    // let a single idle speculative socket stall teardown. Kept well above the
    // sub-millisecond loopback norm: this runner demonstrably starves for seconds
    // under load, and cutting a legitimately slow request would trade an
    // intermittent hang for a connection reset, which is no better.
    const ACTION_FIXTURE_IO_TIMEOUT: Duration = Duration::from_secs(5);

    // The clock runs from each navigation arm so the snapshot lands while that
    // navigation is still stalled and the fixture is alive to record it.
    const ACTION_FIXTURE_STALL_PROBE: Duration = Duration::from_secs(8);

    impl ActionFixtureServer {
        fn start() -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind action fixture");
            listener
                .set_nonblocking(true)
                .expect("set action fixture nonblocking");
            let addr = listener.local_addr().expect("action fixture address");
            let (captures_tx, captures) = mpsc::channel();
            let stop = Arc::new(AtomicBool::new(false));
            let thread_stop = Arc::clone(&stop);
            let trace = Arc::new(std::sync::Mutex::new(Vec::new()));
            let loop_trace = Arc::clone(&trace);
            let started = std::time::Instant::now();
            let arm_gen = Arc::new(AtomicU64::new(0));
            let thread_arm_gen = Arc::clone(&arm_gen);
            let probe_resolved = Arc::new(AtomicU64::new(0));
            let thread_probe_resolved = Arc::clone(&probe_resolved);
            let arm_deadline_ms = Arc::new(AtomicU64::new(0));
            let thread_arm_deadline_ms = Arc::clone(&arm_deadline_ms);
            let arm_armed_ms = Arc::new(AtomicU64::new(0));
            let thread_arm_armed_ms = Arc::clone(&arm_armed_ms);
            let arm_path = Arc::new(Mutex::new((0, String::new())));
            let thread_arm_path = Arc::clone(&arm_path);
            // Recorded so that a port reused across tests in one run is visible
            // by inspection of a single job log rather than by inference.
            action_fixture_trace(&loop_trace, format!("t=0ms bound {addr}"));
            let thread = thread::spawn(move || {
                let mut handlers = Vec::new();
                let mut accepted = 0usize;
                while !thread_stop.load(Ordering::Relaxed) {
                    let elapsed_ms = started.elapsed().as_millis() as u64;
                    // This Acquire pairs with arm's Release store, so the
                    // deadline and armed timestamp reads are no older than the arm.
                    let generation = thread_arm_gen.load(Ordering::Acquire);
                    if generation != 0
                        && thread_probe_resolved.load(Ordering::Relaxed) < generation
                        && elapsed_ms >= thread_arm_deadline_ms.load(Ordering::Relaxed)
                        && thread_probe_resolved.fetch_max(generation, Ordering::Relaxed)
                            < generation
                    {
                        let armed_ms = thread_arm_armed_ms.load(Ordering::Relaxed);
                        let probe_trace = Arc::clone(&loop_trace);
                        let spawn_trace = Arc::clone(&probe_trace);
                        match thread::Builder::new().spawn(move || {
                            let walk_started = std::time::Instant::now();
                            let states = action_fixture_socket_states(addr.port());
                            let walk_ms = walk_started.elapsed().as_millis();
                            action_fixture_trace(
                                &spawn_trace,
                                format!(
                                    "t={}ms kernel sockets (arm#{generation} armed t={armed_ms}ms, walk={walk_ms}ms): {states}",
                                    started.elapsed().as_millis(),
                                ),
                            );
                        }) {
                            Ok(handler) => handlers.push(handler),
                            Err(error) => {
                                let walk_started = std::time::Instant::now();
                                let states = action_fixture_socket_states(addr.port());
                                let walk_ms = walk_started.elapsed().as_millis();
                                action_fixture_trace(
                                    &probe_trace,
                                    format!(
                                        "t={}ms kernel sockets (arm#{generation} armed t={armed_ms}ms, walk={walk_ms}ms): {states} (walker spawn failed: {error}, walked inline)",
                                        started.elapsed().as_millis(),
                                    ),
                                );
                            }
                        }
                    }
                    match listener.accept() {
                        Ok((mut stream, peer)) => {
                            if thread_stop.load(Ordering::Relaxed) {
                                break;
                            }
                            // The single lock keeps the generation and path coherent. The pair may
                            // lag the newest arm during its tiny pre-publish window, which is stale
                            // but coherent and harmless for this accepted connection.
                            let (accepted_gen, accepted_path) = thread_arm_path
                                .lock()
                                .expect("lock action fixture arm path")
                                .clone();
                            accepted += 1;
                            let index = accepted;
                            action_fixture_trace(
                                &loop_trace,
                                format!(
                                    "t={}ms conn#{index} accepted from {peer}",
                                    started.elapsed().as_millis()
                                ),
                            );
                            let captures = captures_tx.clone();
                            let handler_trace = Arc::clone(&loop_trace);
                            let handler_probe_resolved = Arc::clone(&thread_probe_resolved);
                            handlers.push(thread::spawn(move || {
                                let outcome =
                                    serve_action_fixture(&mut stream, addr.port(), &captures);
                                if let ActionFixtureOutcome::Served(served_path) = &outcome
                                    && accepted_gen != 0
                                    && served_path == &accepted_path
                                {
                                    // Serving the navigation document does not prove the browser-side load
                                    // completed, and a retried request for the same path on an old connection
                                    // could still resolve a newer same-path arm. This is accepted because the
                                    // alternative (any-served disarm) suppressed genuine stalls.
                                    handler_probe_resolved
                                        .fetch_max(accepted_gen, Ordering::Relaxed);
                                    action_fixture_trace(
                                        &handler_trace,
                                        format!(
                                            "t={}ms conn#{index} resolved arm#{accepted_gen}",
                                            started.elapsed().as_millis()
                                        ),
                                    );
                                }
                                action_fixture_trace(
                                    &handler_trace,
                                    format!(
                                        "t={}ms conn#{index} handler-returned: {outcome}",
                                        started.elapsed().as_millis()
                                    ),
                                );
                            }));
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(2));
                        }
                        Err(error) => panic!("action fixture accept failed: {error}"),
                    }
                }
                let elapsed_ms = started.elapsed().as_millis() as u64;
                let generation = thread_arm_gen.load(Ordering::Acquire);
                let resolved_generation = thread_probe_resolved.load(Ordering::Relaxed);
                let deadline_ms = thread_arm_deadline_ms.load(Ordering::Relaxed);
                if generation != 0
                    && resolved_generation < generation
                    && elapsed_ms >= deadline_ms
                    && thread_probe_resolved.fetch_max(generation, Ordering::Relaxed) < generation
                {
                    let armed_ms = thread_arm_armed_ms.load(Ordering::Relaxed);
                    let walk_started = std::time::Instant::now();
                    let states = action_fixture_socket_states(addr.port());
                    let walk_ms = walk_started.elapsed().as_millis();
                    action_fixture_trace(
                        &loop_trace,
                        format!(
                            "t={}ms kernel sockets at teardown (arm#{generation} armed t={armed_ms}ms, walk={walk_ms}ms): {states}",
                            started.elapsed().as_millis(),
                        ),
                    );
                }
                action_fixture_trace(
                    &loop_trace,
                    format!(
                        "t={}ms accept loop exiting, {accepted} accepted, joining {} handler(s)",
                        started.elapsed().as_millis(),
                        handlers.len()
                    ),
                );
                for handler in handlers {
                    handler.join().expect("join action fixture connection");
                }
            });
            Self {
                addr,
                captures,
                stop,
                thread: Some(thread),
                started,
                arm_gen,
                probe_resolved,
                arm_deadline_ms,
                arm_armed_ms,
                arm_path,
                trace,
            }
        }

        fn arm(&self, path: &str, probe_after: Duration) {
            // Arms are issued only by the single test thread, so this relaxed
            // read plus one computes the unique next generation.
            let next = self.arm_gen.load(Ordering::Relaxed) + 1;
            *self.arm_path.lock().expect("lock action fixture arm path") = (next, path.to_owned());
            let now_ms = self.started.elapsed().as_millis() as u64;
            self.arm_armed_ms.store(now_ms, Ordering::Relaxed);
            self.arm_deadline_ms
                .store(now_ms + probe_after.as_millis() as u64, Ordering::Relaxed);
            // Store the generation last: this Release publishes the coherent
            // pair, armed timestamp, and deadline to the loop's Acquire load.
            self.arm_gen.store(next, Ordering::Release);
        }

        fn url(&self, path: &str) -> String {
            self.arm(path, ACTION_FIXTURE_STALL_PROBE);
            format!("http://{}{path}", self.addr)
        }

        fn capture(&self) -> String {
            self.captures
                .recv_timeout(Duration::from_secs(5))
                .expect("receive action fixture capture")
        }
    }

    /// Appends to the fixture connection trace. A poisoned lock is ignored
    /// rather than propagated: this is diagnostic bookkeeping, and panicking
    /// here would replace the failure under investigation with its own.
    fn action_fixture_trace(trace: &Arc<std::sync::Mutex<Vec<String>>>, entry: String) {
        if let Ok(mut entries) = trace.lock() {
            entries.push(entry);
        }
    }

    /// What became of one accepted fixture connection.
    ///
    /// The byte count is the point. A stalled navigation that leaves the
    /// handler at zero bytes means Chromium completed the TCP handshake and
    /// then never wrote the request; a partial count means it began writing
    /// and stopped. Those have different causes, and until now the trace
    /// could not tell them apart because `read_http_headers` discards its
    /// partial buffer on error.
    enum ActionFixtureOutcome {
        Served(String),
        ReadFailed {
            bytes: usize,
            kind: std::io::ErrorKind,
        },
    }

    impl fmt::Display for ActionFixtureOutcome {
        fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            match self {
                Self::Served(_) => write!(formatter, "served"),
                Self::ReadFailed { bytes, kind } => {
                    write!(formatter, "read-failed after {bytes} byte(s): {kind:?}")
                }
            }
        }
    }

    /// Header read that reports how far it got. Behaviourally identical to
    /// `read_http_headers`; it differs only in surfacing the partial length
    /// instead of dropping it, so the caller can record it.
    fn read_action_fixture_headers(
        stream: &mut TcpStream,
    ) -> Result<Vec<u8>, (usize, std::io::ErrorKind)> {
        let mut headers = Vec::new();
        while !headers.ends_with(b"\r\n\r\n") {
            if headers.len() >= 64 * 1024 {
                return Err((headers.len(), std::io::ErrorKind::InvalidData));
            }
            let mut byte = [0_u8; 1];
            if let Err(error) = stream.read_exact(&mut byte) {
                return Err((headers.len(), error.kind()));
            }
            headers.push(byte[0]);
        }
        Ok(headers)
    }

    fn action_fixture_parse_proc_tcp(table: &str, contents: &str, port: u16) -> Vec<String> {
        fn state_name(state: u8) -> &'static str {
            match state {
                0x01 => "ESTABLISHED",
                0x02 => "SYN_SENT",
                0x03 => "SYN_RECV",
                0x04 => "FIN_WAIT1",
                0x05 => "FIN_WAIT2",
                0x06 => "TIME_WAIT",
                0x07 => "CLOSE",
                0x08 => "CLOSE_WAIT",
                0x09 => "LAST_ACK",
                0x0A => "LISTEN",
                0x0B => "CLOSING",
                0x0C => "NEW_SYN_RECV",
                0x0D => "BOUND_INACTIVE",
                _ => "UNKNOWN",
            }
        }

        // `sl local:port rem:port st tx:rx tr:when retrnsmt uid timeout inode`
        fn endpoint_port(raw: &str) -> Option<u16> {
            let (_, port) = raw.rsplit_once(':')?;
            u16::from_str_radix(port, 16).ok()
        }

        let mut rows = Vec::new();
        let mut unparsed = 0usize;
        let mut first_unparsed = None;
        for line in contents.lines().skip(1) {
            if line.trim().is_empty() {
                continue;
            }
            let fields = line.split_whitespace().collect::<Vec<_>>();
            if fields.len() < 10 {
                unparsed += 1;
                first_unparsed.get_or_insert_with(|| line.chars().take(160).collect::<String>());
                continue;
            }
            let (Some(local), Some(remote)) = (endpoint_port(fields[1]), endpoint_port(fields[2]))
            else {
                unparsed += 1;
                first_unparsed.get_or_insert_with(|| line.chars().take(160).collect::<String>());
                continue;
            };
            // Match ports only so address decoding cannot hide the true socket; emit raw endpoints instead.
            if local != port && remote != port {
                continue;
            }
            let state = u8::from_str_radix(fields[3], 16).unwrap_or(0);
            let side = if local == port { "local" } else { "peer" };
            rows.push(format!(
                "{table}/{side} local={} peer={} local_port={local} peer_port={remote} {} queues={} timer={} retransmits={} inode={}",
                fields[1],
                fields[2],
                state_name(state),
                fields[4],
                fields[5],
                fields[6],
                fields[9]
            ));
        }
        if unparsed != 0 {
            rows.push(format!(
                "unparsed={unparsed} sample=\"{}\"",
                first_unparsed.expect("unparsed line sample")
            ));
        }
        rows
    }

    /// Kernel-side view of every TCP socket touching `port`, read straight out
    /// of `/proc/net/tcp{,6}`.
    ///
    /// This exists because the stall has now survived three explanations that
    /// were argued rather than measured. It answers the one question the
    /// userspace trace cannot: at the moment the navigation is stuck, has
    /// Chromium opened a socket to the fixture at all, and what state is it in?
    ///
    /// Deliberately `/proc` rather than `ss`: `ss` is not guaranteed installed
    /// on the runner image, and a missing binary would silently cost a whole
    /// CI cycle. Metadata only — states, counts, queue depths, retransmit
    /// counters — never payload, so the password-masking properties of these
    /// tests are untouched.
    #[cfg(target_os = "linux")]
    fn action_fixture_socket_states(port: u16) -> String {
        let mut rows = Vec::new();
        let mut unreadable = Vec::new();
        for (table, path) in [("tcp", "/proc/net/tcp"), ("tcp6", "/proc/net/tcp6")] {
            match std::fs::read_to_string(path) {
                Ok(contents) => {
                    rows.extend(action_fixture_parse_proc_tcp(table, &contents, port));
                }
                Err(_) => unreadable.push(table),
            }
        }

        if rows.is_empty() && unreadable.is_empty() {
            rows.push("no socket for this port".to_owned());
        }
        if !unreadable.is_empty() {
            rows.push(format!("unreadable: {}", unreadable.join(",")));
        }
        rows.join(" | ")
    }

    #[cfg(not(target_os = "linux"))]
    fn action_fixture_socket_states(_port: u16) -> String {
        "unavailable off linux".to_owned()
    }

    impl Drop for ActionFixtureServer {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::Relaxed);
            let _ = TcpStream::connect(self.addr);
            if let Some(thread) = self.thread.take() {
                thread.join().expect("join action fixture");
            }
            if thread::panicking() {
                let entries = match self.trace.lock() {
                    Ok(entries) => entries.clone(),
                    Err(poisoned) => poisoned.into_inner().clone(),
                };
                eprintln!(
                    "===== ACTION FIXTURE CONNECTION TRACE ({}) =====",
                    self.addr
                );
                if entries.is_empty() {
                    eprintln!("  (no connection was ever accepted)");
                }
                for entry in entries {
                    eprintln!("  {entry}");
                }
                eprintln!("===== ACTION FIXTURE CONNECTION TRACE END =====");
            }
        }
    }

    fn serve_action_fixture(
        stream: &mut TcpStream,
        port: u16,
        captures: &mpsc::Sender<String>,
    ) -> ActionFixtureOutcome {
        stream
            .set_nonblocking(false)
            .expect("set action fixture connection blocking");
        stream
            .set_read_timeout(Some(ACTION_FIXTURE_IO_TIMEOUT))
            .expect("set action fixture read timeout");
        stream
            .set_write_timeout(Some(ACTION_FIXTURE_IO_TIMEOUT))
            .expect("set action fixture write timeout");
        let request = match read_action_fixture_headers(stream) {
            Ok(request) => request,
            Err((bytes, kind)) => return ActionFixtureOutcome::ReadFailed { bytes, kind },
        };
        let request = String::from_utf8_lossy(&request);
        let target = request
            .lines()
            .next()
            .and_then(|line| line.split_whitespace().nth(1))
            .unwrap_or("/");
        let origin_form = target
            .split_once("://")
            .and_then(|(_, authority_and_path)| {
                authority_and_path
                    .find('/')
                    .map(|at| &authority_and_path[at..])
            })
            .unwrap_or(target);
        let path = origin_form.split('?').next().unwrap_or(origin_form);
        let body = match path {
            "/actionability" => actionability_fixture(),
            "/cancel" => cancellation_fixture(),
            "/fill-cancel" => fill_cancellation_fixture(),
            "/fill-final-commit" => fill_final_commit_fixture(),
            "/fill-nonphysical-cancel" => fill_nonphysical_cancellation_fixture(),
            "/input" => input_fixture(false),
            "/input-slow-cancel" => input_fixture(true),
            "/input-aria-echo" => input_aria_echo_fixture(),
            "/input-aria-echo-common" => input_aria_echo_common_fixture(),
            "/input-leaf-echo" => input_leaf_echo_fixture(),
            "/input-fill-form-echo" => input_fill_form_echo_fixture(),
            "/input-property-only-echo" => input_property_only_echo_fixture(),
            "/input-post-dispatch-timeout-echo" => input_post_dispatch_timeout_echo_fixture(),
            "/input-submit-failure-echo" => input_submit_failure_echo_fixture(),
            "/input-partial-fill-echo" => input_partial_fill_echo_fixture(),
            "/input-partial-type-value-echo" => input_partial_type_value_echo_fixture(),
            "/input-partial-fill-value-echo" => input_partial_fill_value_echo_fixture(),
            "/input-split-ancestor-echo" => input_split_ancestor_echo_fixture(),
            "/input-split-labelledby-echo" => input_split_labelledby_echo_fixture(),
            "/input-id-relationship-echo" => input_id_relationship_echo_fixture(),
            "/input-labelled-container-echo" => input_labelled_container_echo_fixture(),
            "/input-visibility-echo" => input_visibility_echo_fixture(),
            "/input-aria-hidden-ancestor-echo" => input_aria_hidden_ancestor_echo_fixture(),
            "/input-labelledby-echo" => input_labelledby_echo_fixture(),
            "/input-label-echo" => input_label_echo_fixture(),
            "/input-output-branch-echo" => input_output_branch_echo_fixture(),
            "/input-role-echo" => input_role_echo_fixture(),
            "/input-custom-role-echo" => input_custom_role_echo_fixture(),
            "/input-dialog-echo" => input_dialog_echo_fixture(),
            "/input-root-replacement-echo" => input_root_replacement_echo_fixture(),
            "/input-reactive-safe" => input_reactive_safe_fixture(),
            "/atomic-click" => atomic_click_fixture(),
            "/physical" => physical_fixture(),
            "/oopif-top" => oopif_top_fixture(port),
            "/oopif-child" => oopif_child_fixture(),
            "/arrived" => "<!doctype html><title>arrived</title><main>arrived</main>".to_owned(),
            "/capture" => {
                let value = target
                    .split_once("events=")
                    .map(|(_, value)| value.to_owned())
                    .unwrap_or_default();
                captures.send(value).expect("send action fixture capture");
                "ok".to_owned()
            }
            _ => "<!doctype html><title>missing</title>".to_owned(),
        };
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        stream
            .write_all(response.as_bytes())
            .expect("write action fixture response");
        ActionFixtureOutcome::Served(path.to_owned())
    }

    fn actionability_fixture() -> String {
        r#"<!doctype html>
<style>
  body { margin: 0; }
  #hidden { display: none; }
  #covered-wrap { position: relative; width: 180px; height: 50px; }
  #covered { width: 180px; height: 50px; }
  #cover { position: absolute; inset: 0; z-index: 2; background: rgba(0, 0, 0, .4); }
  #moving { width: 140px; height: 44px; }
  #moving.run { animation: move 320ms linear; }
  #partially-offscreen { position: fixed; left: -1000px; top: 120px; width: 1100px; height: 44px; }
  #detach { position: absolute; left: 0; top: 100000px; width: 140px; height: 44px; }
  @keyframes move { from { transform: translateX(0); } to { transform: translateX(240px); } }
</style>
<button id="hidden">Hidden</button>
<div id="covered-wrap"><button id="covered">Covered</button><div id="cover"></div></div>
<button id="disabled" disabled>Disabled</button>
<button id="moving">Moving</button>
<button id="partially-offscreen">Partially offscreen</button>
<button id="detach">Detach</button>
<script>
  globalThis.movingEvents = [];
  globalThis.partiallyOffscreenEvents = [];
  globalThis.animationEnded = false;
  const moving = document.querySelector('#moving');
  moving.addEventListener('animationend', () => globalThis.animationEnded = true);
  for (const name of ['mousedown', 'mouseup', 'click']) {
    moving.addEventListener(name, event => {
      const rect = moving.getBoundingClientRect();
      globalThis.movingEvents.push({
        type: event.type,
        trusted: event.isTrusted,
        hit: event.clientX >= rect.left && event.clientX <= rect.right &&
          event.clientY >= rect.top && event.clientY <= rect.bottom,
      });
    });
  }
  const partiallyOffscreen = document.querySelector('#partially-offscreen');
  for (const name of ['mousedown', 'mouseup', 'click']) {
    partiallyOffscreen.addEventListener(name, event => {
      globalThis.partiallyOffscreenEvents.push({
        type: event.type,
        trusted: event.isTrusted,
        clientX: event.clientX,
        clientY: event.clientY,
      });
    });
  }
  const detached = document.querySelector('#detach');
  globalThis.detachedPointerEvents = [];
  globalThis.detachedScroll = null;
  globalThis.detachArmed = false;
  globalThis.detachStartScrollY = 0;
  globalThis.detachMotionTick = 0;
  for (const name of ['mousedown', 'mouseup', 'click']) {
    detached.addEventListener(name, event => {
      globalThis.detachedPointerEvents.push({
        type: event.type,
        trusted: event.isTrusted,
      });
    });
  }
  // DOM geometry is frame-discrete, so two samples in the same frame are
  // identical for any fixture. While the element exists, this fixture guarantees
  // any two samples at least one frame (~16ms) apart differ: motion ticks run
  // under both rAF and timer scheduling, and every tick moves the target by one
  // CSS pixel (above the engine's 0.5px tolerance). A stability sampler that
  // spaces samples closer than one frame measures sub-frame stability, which
  // cannot observe motion by construction. Removal is scheduling-independent:
  // the synchronous scroll listener and the interval and rAF monitors all call
  // detachAfterScroll.
  function advanceDetachMotion() {
    if (!globalThis.detachArmed || !detached.isConnected) return;
    globalThis.detachMotionTick += 1;
    detached.style.transform = `translateY(${globalThis.detachMotionTick}px)`;
  }
  const detachAfterScroll = () => {
    if (!globalThis.detachArmed || !detached.isConnected) return;
    if (scrollY === globalThis.detachStartScrollY) return;
    globalThis.detachedScroll = {
      from: globalThis.detachStartScrollY,
      to: scrollY,
    };
    detached.remove();
  };
  addEventListener('scroll', detachAfterScroll, { passive: true });
  // This harness owns a --headless=new browser with no user-visible window, so
  // another application cannot occlude it and requestAnimationFrame stays live.
  // The interval monitor still covers throttled frames, while the scroll listener
  // removes synchronously during event dispatch regardless of either scheduler.
  const monitorDetach = () => {
    advanceDetachMotion();
    detachAfterScroll();
    if (detached.isConnected) requestAnimationFrame(monitorDetach);
  };
  requestAnimationFrame(monitorDetach);
  const detachMonitorTimer = setInterval(() => {
    advanceDetachMotion();
    detachAfterScroll();
    if (!detached.isConnected) clearInterval(detachMonitorTimer);
  }, 16);
  fetch('/capture?events=actionability-ready');
</script>"#
            .to_owned()
    }

    fn cancellation_fixture() -> String {
        r#"<!doctype html>
<style>body { margin: 0; } #cancel { display: block; margin-top: 2000px; width: 160px; height: 48px; }</style>
<button id="cancel" disabled>Cancel target</button>
<script>
  let reported = false;
  fetch('/capture?events=cancel-ready');
  addEventListener('scroll', () => {
    if (reported) return;
    reported = true;
    fetch('/capture?events=actionability-started');
  });
</script>"#
            .to_owned()
    }

    fn atomic_click_fixture() -> String {
        r#"<!doctype html>
<button id="atomic">Atomic click</button>
<button id="following">Following click</button>
<button id="background">Background click</button>
<script>
  globalThis.atomicEvents = [];
  globalThis.atomicEffectCount = 0;
  globalThis.followingEffectCount = 0;
  globalThis.buttonDown = false;
  globalThis.backgroundEvents = [];
  globalThis.backgroundEffectCount = 0;
  const atomic = document.querySelector('#atomic');
  atomic.addEventListener('mousedown', event => {
    globalThis.buttonDown = true;
    globalThis.atomicEvents.push({ type: event.type, trusted: event.isTrusted });
    const signal = new XMLHttpRequest();
    signal.open('GET', '/capture?events=atomic-mousedown', false);
    signal.send();
    const releaseWindow = performance.now() + 500;
    while (performance.now() < releaseWindow) {}
  });
  atomic.addEventListener('mouseup', event => {
    globalThis.buttonDown = false;
    globalThis.atomicEvents.push({ type: event.type, trusted: event.isTrusted });
  });
  atomic.addEventListener('click', event => {
    globalThis.atomicEffectCount += 1;
    atomic.textContent = `Atomic click effect ${globalThis.atomicEffectCount}`;
    globalThis.atomicEvents.push({ type: event.type, trusted: event.isTrusted });
  });
  document.querySelector('#following').addEventListener('click', () => {
    globalThis.followingEffectCount += 1;
  });
  const background = document.querySelector('#background');
  for (const name of ['mousedown', 'mouseup', 'click']) {
    background.addEventListener(name, event => {
      globalThis.backgroundEvents.push({ type: event.type, trusted: event.isTrusted });
      if (event.type === 'click') {
        globalThis.backgroundEffectCount += 1;
        background.textContent = `Background click effect ${globalThis.backgroundEffectCount}`;
      }
    });
  }
  fetch('/capture?events=atomic-ready');
</script>"#
            .to_owned()
    }

    fn fill_cancellation_fixture() -> String {
        r#"<!doctype html>
<label for="commit-checkbox">Commit checkbox</label>
<input id="commit-checkbox" type="checkbox">
<label for="later-01">Later field 01</label>
<input id="later-01">
<label for="later-02">Later field 02</label>
<input id="later-02">
<label for="later-03">Later field 03</label>
<input id="later-03">
<label for="later-04">Later field 04</label>
<input id="later-04">
<label for="later-05">Later field 05</label>
<input id="later-05">
<label for="later-06">Later field 06</label>
<input id="later-06">
<label for="later-07">Later field 07</label>
<input id="later-07">
<label for="later-08">Later field 08</label>
<input id="later-08">
<label for="later-09">Later field 09</label>
<input id="later-09">
<label for="later-10">Later field 10</label>
<input id="later-10">
<label for="later-11">Later field 11</label>
<input id="later-11">
<label for="later-12">Later field 12</label>
<input id="later-12">
<div id="written-fields" role="status">Written fields: none</div>
<script>
  const written = [];
  const renderWritten = () => {
    document.querySelector('#written-fields').textContent =
      `Written fields: ${written.join(', ') || 'none'}`;
  };
  const checkbox = document.querySelector('#commit-checkbox');
  checkbox.addEventListener('click', () => {
    written.push('Commit checkbox');
    renderWritten();
    const signal = new XMLHttpRequest();
    signal.open('GET', '/capture?events=fill-cancel-point', false);
    signal.send();
    const releaseWindow = performance.now() + 750;
    while (performance.now() < releaseWindow) {}
  });
  for (let index = 1; index <= 12; index += 1) {
    const name = `Later field ${String(index).padStart(2, '0')}`;
    document.querySelector(`#later-${String(index).padStart(2, '0')}`)
      .addEventListener('input', () => {
        written.push(name);
        renderWritten();
      });
  }
  fetch('/capture?events=fill-cancel-ready');
</script>"#
            .to_owned()
    }

    fn fill_final_commit_fixture() -> String {
        r#"<!doctype html>
<label for="first-field">First field</label>
<input id="first-field">
<label for="final-checkbox">Final checkbox</label>
<input id="final-checkbox" type="checkbox">
<div id="written-fields" role="status">Written fields: none</div>
<script>
  const written = [];
  const renderWritten = () => {
    document.querySelector('#written-fields').textContent =
      `Written fields: ${written.join(', ') || 'none'}`;
  };
  document.querySelector('#first-field').addEventListener('input', () => {
    written.push('First field');
    renderWritten();
  });
  document.querySelector('#final-checkbox').addEventListener('click', () => {
    written.push('Final checkbox');
    renderWritten();
    const signal = new XMLHttpRequest();
    signal.open('GET', '/capture?events=fill-final-commit-point', false);
    signal.send();
    const releaseWindow = performance.now() + 750;
    while (performance.now() < releaseWindow) {}
  });
  fetch('/capture?events=fill-final-commit-ready');
</script>"#
            .to_owned()
    }

    fn fill_nonphysical_cancellation_fixture() -> String {
        r#"<!doctype html>
<label for="field-a">Field A</label>
<input id="field-a">
<label for="field-b">Field B</label>
<input id="field-b">
<label for="field-c">Field C</label>
<input id="field-c">
<div id="written-fields" role="status">Written fields: none</div>
<script>
  const written = [];
  const renderWritten = () => {
    document.querySelector('#written-fields').textContent =
      `Written fields: ${written.join(', ') || 'none'}`;
  };
  document.querySelector('#field-a').addEventListener('input', () => {
    written.push('Field A');
    renderWritten();
  });
  document.querySelector('#field-b').addEventListener('input', () => {
    written.push('Field B');
    renderWritten();
    const signal = new XMLHttpRequest();
    signal.open('GET', '/capture?events=fill-nonphysical-cancel-point', false);
    signal.send();
    const releaseWindow = performance.now() + 750;
    while (performance.now() < releaseWindow) {}
  });
  document.querySelector('#field-c').addEventListener('input', () => {
    written.push('Field C');
    renderWritten();
  });
  fetch('/capture?events=fill-nonphysical-cancel-ready');
</script>"#
            .to_owned()
    }

    fn physical_fixture() -> String {
        r#"<!doctype html>
<style>
  body { margin: 0; }
  #physical { display: block; margin-top: 1800px; width: 180px; height: 50px; }
  #hover-target { display: block; width: 180px; height: 50px; }
</style>
<button id="physical">Physical</button>
<button id="hover-target" disabled>Hover disabled target</button>
<label><input id="check-target" type="checkbox">Check target</label>
<a id="navigate" href="/arrived">Navigate</a>
<script>
  globalThis.physicalEvents = [];
  globalThis.hoverEvents = [];
  globalThis.checkEvents = [];
  const target = document.querySelector('#physical');
  for (const name of ['mousedown', 'mouseup', 'click', 'dblclick']) {
    target.addEventListener(name, event => globalThis.physicalEvents.push({
      type: event.type,
      trusted: event.isTrusted,
      button: event.button,
      detail: event.detail,
    }));
  }
  document.querySelector('#hover-target').addEventListener('mouseover', event => {
    globalThis.hoverEvents.push({ type: event.type, trusted: event.isTrusted });
  });
  const checkTarget = document.querySelector('#check-target');
  for (const name of ['mousedown', 'mouseup', 'click']) {
    checkTarget.addEventListener(name, event => globalThis.checkEvents.push({
      type: event.type,
      trusted: event.isTrusted,
      checked: checkTarget.checked,
    }));
  }
  fetch('/capture?events=physical-ready');
</script>"#
            .to_owned()
    }

    fn input_fixture(capture_on_input: bool) -> String {
        r#"<!doctype html>
<label for="type-target">Type target</label>
<input id="type-target" value="old value">
<label for="secret-target">Secret input</label>
<input id="secret-target" type="password">
<div id="secret-length-readout" role="status">Secret length: 0</div>
<div id="type-readout" role="status">Typed value: old value</div>
<div id="type-change-readout" role="status">Type change value: none; trusted: none</div>
<div id="key-readout" role="status">Key pressed: none</div>
<div id="submit-readout" role="status">Submit effects: 0</div>
<button id="hover-target">Hover target</button>
<div id="hover-readout" role="status">Hover observed: false</div>
<label for="select-target">Select target</label>
<select id="select-target">
  <option value="alpha">Alpha</option>
  <option value="beta">Beta</option>
</select>
<div id="select-readout" role="status">Selected value: alpha; changes: 0</div>
<label for="multi-select-target">Multi select target</label>
<select id="multi-select-target" multiple>
  <option value="alpha" selected>Alpha</option>
  <option value="beta" selected>Beta</option>
</select>
<div id="multi-select-readout" role="status">Selected values: alpha,beta; changes: 0</div>
<label for="ambiguous-select-target">Ambiguous select target</label>
<select id="ambiguous-select-target">
  <option value="other">X</option>
  <option value="X">Y</option>
</select>
<div id="ambiguous-select-readout" role="status">Ambiguous selected value: other; changes: 0</div>
<label for="ambiguous-multi-select-target">Ambiguous multi select target</label>
<select id="ambiguous-multi-select-target" multiple>
  <option value="other">X</option>
  <option value="X">Y</option>
</select>
<div id="ambiguous-multi-select-readout" role="status">Ambiguous selected values: none; changes: 0</div>
<script>
  const typeTarget = document.querySelector('#type-target');
  const secretTarget = document.querySelector('#secret-target');
  const typeReadout = document.querySelector('#type-readout');
  const typeChangeReadout = document.querySelector('#type-change-readout');
  const keyReadout = document.querySelector('#key-readout');
  const submitReadout = document.querySelector('#submit-readout');
  const captureOnInput = __CAPTURE_ON_INPUT__;
  const updateSecretLength = () => {
    document.querySelector('#secret-length-readout').textContent =
      `Secret length: ${secretTarget.value.length}`;
  };
  secretTarget.addEventListener('input', updateSecretLength);
  secretTarget.addEventListener('change', updateSecretLength);
  typeTarget.addEventListener('input', () => {
    typeReadout.textContent = `Typed value: ${typeTarget.value}`;
    if (captureOnInput && typeTarget.value !== '') {
      const signal = new XMLHttpRequest();
      signal.open('GET', '/capture?events=input-keydown', false);
      signal.send();
      const releaseWindow = performance.now() + 500;
      while (performance.now() < releaseWindow) {}
    }
  });
  typeTarget.addEventListener('change', event => {
    typeChangeReadout.textContent =
      `Type change value: ${typeTarget.value || '(empty)'}; trusted: ${event.isTrusted}`;
  });
  let keyEffects = 0;
  let submitEffects = 0;
  typeTarget.addEventListener('keydown', event => {
    keyEffects += 1;
    if (event.key === 'Enter') {
      submitEffects += 1;
      submitReadout.textContent = `Submit effects: ${submitEffects}`;
    }
    keyReadout.textContent = `Key pressed: ${event.key}; trusted: ${event.isTrusted}`;
    if (!captureOnInput) {
      const signal = new XMLHttpRequest();
      signal.open('GET', '/capture?events=input-keydown', false);
      signal.send();
      const releaseWindow = performance.now() + 500;
      while (performance.now() < releaseWindow) {}
    }
  });
  typeTarget.addEventListener('keyup', event => {
    keyReadout.textContent =
      `Key pressed: ${event.key}; trusted: ${event.isTrusted}; state: up; effects: ${keyEffects}`;
  });
  document.querySelector('#hover-target').addEventListener('mouseover', event => {
    document.querySelector('#hover-readout').textContent =
      `Hover observed: true; trusted: ${event.isTrusted}`;
  });
  let changes = 0;
  const selectTarget = document.querySelector('#select-target');
  selectTarget.addEventListener('change', () => {
    changes += 1;
    document.querySelector('#select-readout').textContent =
      `Selected value: ${selectTarget.value}; changes: ${changes}`;
  });
  let multiChanges = 0;
  const multiSelectTarget = document.querySelector('#multi-select-target');
  multiSelectTarget.addEventListener('change', () => {
    multiChanges += 1;
    const selected = Array.from(multiSelectTarget.selectedOptions)
      .map(option => option.value)
      .join(',') || 'none';
    document.querySelector('#multi-select-readout').textContent =
      `Selected values: ${selected}; changes: ${multiChanges}`;
  });
  let ambiguousChanges = 0;
  const ambiguousSelectTarget = document.querySelector('#ambiguous-select-target');
  ambiguousSelectTarget.addEventListener('change', () => {
    ambiguousChanges += 1;
    document.querySelector('#ambiguous-select-readout').textContent =
      `Ambiguous selected value: ${ambiguousSelectTarget.value}; changes: ${ambiguousChanges}`;
  });
  let ambiguousMultiChanges = 0;
  const ambiguousMultiSelectTarget =
    document.querySelector('#ambiguous-multi-select-target');
  ambiguousMultiSelectTarget.addEventListener('change', () => {
    ambiguousMultiChanges += 1;
    const selected = Array.from(ambiguousMultiSelectTarget.selectedOptions)
      .map(option => option.value)
      .join(',') || 'none';
    document.querySelector('#ambiguous-multi-select-readout').textContent =
      `Ambiguous selected values: ${selected}; changes: ${ambiguousMultiChanges}`;
  });
</script>"#
            .replace(
                "__CAPTURE_ON_INPUT__",
                if capture_on_input { "true" } else { "false" },
            )
    }

    fn input_aria_echo_fixture() -> String {
        r#"<!doctype html>
<input
  id="secret-echo-target"
  type="password"
  aria-label="Secret echo input"
  oninput="
    this.setAttribute('aria-label', this.value);
    document.querySelector('#secret-echo-status').textContent = this.value;
  "
>
<div id="secret-echo-status" role="status">No secret entered</div>"#
            .to_owned()
    }

    fn input_aria_echo_common_fixture() -> String {
        format!(
            "{}\n<div id=\"unrelated-common-status\" role=\"status\">a</div>",
            input_aria_echo_fixture()
        )
    }

    fn input_leaf_echo_fixture() -> String {
        r#"<!doctype html>
<input
  id="secret-leaf-target"
  type="password"
  aria-label="Secret leaf input"
  oninput="
    document.querySelector('#secret-leaf-echo').textContent = this.value;
  "
>
<span id="secret-leaf-echo">No secret entered</span>"#
            .to_owned()
    }

    fn input_fill_form_echo_fixture() -> String {
        r#"<!doctype html>
<input
  id="first-fill-secret"
  type="password"
  aria-label="First fill secret"
  oninput="
    document.querySelector('#first-fill-echo').textContent = this.value;
  "
>
<div id="first-fill-echo" role="status">First secret empty</div>
<input
  id="second-fill-secret"
  type="password"
  aria-label="Second fill secret"
  oninput="
    document.querySelector('#second-fill-echo').textContent = this.value;
  "
>
<div id="second-fill-echo" role="status">Second secret empty</div>"#
            .to_owned()
    }

    fn input_property_only_echo_fixture() -> String {
        r#"<!doctype html>
<input
  id="property-only-secret"
  type="password"
  aria-label="Property-only secret"
  oninput="document.querySelector('#property-only-mirror').value = this.value"
>
<input id="property-only-mirror" aria-label="Property-only mirror" value="public">"#
            .to_owned()
    }

    fn input_post_dispatch_timeout_echo_fixture() -> String {
        r#"<!doctype html>
<label for="post-dispatch-timeout-secret">Post-dispatch timeout secret</label>
<input
  id="post-dispatch-timeout-secret"
  type="password"
  oninput="
    document.querySelector('#post-dispatch-timeout-mirror').value = this.value;
    const stallUntil = performance.now() + 500;
    while (performance.now() < stallUntil) {}
  "
>
<label for="post-dispatch-timeout-mirror">Post-dispatch timeout mirror</label>
<input id="post-dispatch-timeout-mirror" value="public">"#
            .to_owned()
    }

    fn input_submit_failure_echo_fixture() -> String {
        r#"<!doctype html>
<input
  id="submit-failure-secret"
  type="password"
  aria-label="Submit failure secret"
  oninput="
    document.querySelector('#submit-failure-mirror').value = this.value;
    this.removeAttribute('data-mcp-ref');
  "
>
<input id="submit-failure-mirror" aria-label="Submit failure mirror" value="public">"#
            .to_owned()
    }

    fn input_partial_fill_echo_fixture() -> String {
        r#"<!doctype html>
<input
  id="partial-fill-secret"
  type="password"
  aria-label="Partial fill secret"
  oninput="document.querySelector('#partial-fill-mirror').value = this.value"
>
<input id="partial-fill-mirror" aria-label="Partial fill mirror" value="public">
<input id="partial-fill-checkbox" type="checkbox">
<label for="partial-fill-checkbox">Partial fill checkbox</label>"#
            .to_owned()
    }

    fn input_partial_type_value_echo_fixture() -> String {
        r#"<!doctype html>
<label for="partial-type-secret">Partial type secret</label>
<input
  id="partial-type-secret"
  type="password"
  maxlength="4"
  oninput="document.querySelector('#partial-type-status').textContent = this.value"
>
<div id="partial-type-status" role="status">No partial value</div>"#
            .to_owned()
    }

    fn input_partial_fill_value_echo_fixture() -> String {
        r#"<!doctype html>
<label for="partial-fill-value-secret">Partial fill value secret</label>
<input
  id="partial-fill-value-secret"
  type="password"
  oninput="
    this.value = this.value.slice(0, 4);
    document.querySelector('#partial-fill-value-status').textContent = this.value;
  "
>
<div id="partial-fill-value-status" role="status">No partial value</div>"#
            .to_owned()
    }

    fn input_split_ancestor_echo_fixture() -> String {
        r#"<!doctype html>
<label for="split-ancestor-secret">Split ancestor secret</label>
<input
  id="split-ancestor-secret"
  type="password"
  oninput="
    const midpoint = Math.floor(this.value.length / 2);
    document.querySelector('#split-first').textContent = this.value.slice(0, midpoint);
    document.querySelector('#split-second').textContent = this.value.slice(midpoint);
  "
>
<button><span id="split-first">Public</span><span id="split-second"> button</span></button>"#
            .to_owned()
    }

    fn input_split_labelledby_echo_fixture() -> String {
        r#"<!doctype html>
<label for="split-labelledby-secret">Split labelledby secret</label>
<input
  id="split-labelledby-secret"
  type="password"
  oninput="
    const [first, second] = this.value.split(' ');
    document.querySelector('#split-labelledby-first').textContent = first;
    document.querySelector('#split-labelledby-second').textContent = second;
  "
>
<span id="split-labelledby-first">Public</span>
<span id="split-labelledby-second">control</span>
<button aria-labelledby="split-labelledby-first split-labelledby-second">
  Fallback control
</button>"#
            .to_owned()
    }

    fn input_labelled_container_echo_fixture() -> String {
        r#"<!doctype html>
<section aria-label="Account details">
  <label for="labelled-container-secret">Labelled container secret</label>
  <input
    id="labelled-container-secret"
    type="password"
    oninput="document.querySelector('#container-echo').textContent = this.value;"
  >
  <div id="container-echo"></div>
</section>"#
            .to_owned()
    }

    fn input_id_relationship_echo_fixture() -> String {
        r#"<!doctype html>
<label for="id-relationship-secret">ID relationship secret</label>
<input
  id="id-relationship-secret"
  type="password"
  oninput="document.querySelector('#relationship-decoy').id = 'relationship-echo'"
>
<span id="relationship-decoy" aria-hidden="true">id-relationship-password-canary-66871</span>
<button aria-labelledby="relationship-echo">Public relationship button</button>"#
            .to_owned()
    }

    fn input_visibility_echo_fixture() -> String {
        r#"<!doctype html>
<style>
  .visibility-secret { display: none; }
  body.show-visibility-secret .visibility-secret { display: block; }
</style>
<label for="visibility-secret-input">Visibility secret input</label>
<input
  id="visibility-secret-input"
  type="password"
  oninput="document.body.className = 'show-visibility-secret'"
>
<div class="visibility-secret" role="status">visibility-password-canary-66881</div>"#
            .to_owned()
    }

    fn input_aria_hidden_ancestor_echo_fixture() -> String {
        r#"<!doctype html>
<label for="aria-hidden-ancestor-secret">ARIA-hidden ancestor secret</label>
<input
  id="aria-hidden-ancestor-secret"
  type="password"
  oninput="document.querySelector('#aria-hidden-secret-container').removeAttribute('aria-hidden')"
>
<div id="aria-hidden-secret-container" aria-hidden="true">
  <span>aria-hidden-password-canary-68031</span>
</div>"#
            .to_owned()
    }

    fn input_labelledby_echo_fixture() -> String {
        r#"<!doctype html>
<input
  id="secret-labelledby-target"
  type="password"
  aria-label="Secret labelledby input"
  oninput="
    document.querySelector('#dynamic-labelledby').textContent = this.value;
  "
>
<span id="dynamic-labelledby">Public button name</span>
<button aria-labelledby="dynamic-labelledby">Fallback button name</button>"#
            .to_owned()
    }

    fn input_label_echo_fixture() -> String {
        r#"<!doctype html>
<input
  id="secret-label-target"
  type="password"
  aria-label="Secret label input"
  oninput="
    document.querySelector('#dynamic-label').textContent = this.value;
  "
>
<label id="dynamic-label" for="labelled-textbox">Public textbox name</label>
<input id="labelled-textbox" value="safe echo value">"#
            .to_owned()
    }

    fn input_output_branch_echo_fixture() -> String {
        r#"<!doctype html>
<input
  id="secret-output-target"
  type="password"
  aria-label="Secret output input"
  oninput="
    document.querySelector('#echo-frame').setAttribute('title', this.value);
    document.querySelector('#echo-link').setAttribute('href', '/echo/' + this.value);
    const mirror = document.querySelector('#echo-value');
    mirror.value = this.value;
    mirror.setAttribute('title', 'Updated mirror');
    this.setAttribute('type', 'text');
  "
>
<iframe id="echo-frame" title="Public frame" style="width: 100px; height: 40px"></iframe>
<a id="echo-link" href="/echo/public">Public link</a>
<input id="echo-value" aria-label="Echo value" value="public">"#
            .to_owned()
    }

    fn input_role_echo_fixture() -> String {
        r#"<!doctype html>
<input
  id="role-secret-target"
  type="password"
  aria-label="Role secret input"
  oninput="document.querySelector('#role-echo').setAttribute('role', this.value)"
>
<button id="role-echo" role="button">Public role target</button>"#
            .to_owned()
    }

    fn input_custom_role_echo_fixture() -> String {
        r#"<!doctype html>
<label for="custom-role-secret">Custom role secret input</label>
<input
  id="custom-role-secret"
  type="password"
  oninput="document.querySelector('#custom-role-echo').setAttribute('role', this.value)"
>
<div id="custom-role-echo" role="button" tabindex="0">Public custom control</div>"#
            .to_owned()
    }

    fn input_dialog_echo_fixture() -> String {
        // The dialog has to be pending while the write is still finishing, which
        // is the only moment the secret is in hand to redact with. Opening it
        // from a timer makes that a race against the actor's next CDP round trip
        // -- reliable when the test runs alone, roughly one failure in six under
        // full-suite load. Hook the tracking teardown the resolve step itself
        // calls instead, so the dialog is causally ordered ahead of the
        // post-write snapshot rather than merely usually ahead of it. If the
        // tracking object stops exposing `stop`, the hook silently does not
        // install and the test fails loudly rather than passing vacuously.
        r#"<!doctype html>
<label for="dialog-secret">Dialog secret input</label>
<input
  id="dialog-secret"
  type="password"
  oninput="
    const tracking = globalThis[Symbol.for('rustwright.mcp.sensitiveSnapshot')];
    const pending = tracking && tracking.pending;
    if (pending && typeof pending.stop === 'function' && !pending.dialogHookInstalled) {
      pending.dialogHookInstalled = true;
      const field = this;
      const teardown = pending.stop;
      pending.stop = function () {
        teardown.call(this);
        alert(field.value);
      };
    }
  "
>"#
        .to_owned()
    }

    fn input_root_replacement_echo_fixture() -> String {
        r#"<!doctype html>
<input
  id="root-replacement-secret"
  type="password"
  aria-label="Root replacement secret"
  oninput="
    const replacement = document.createElement('html');
    const body = document.createElement('body');
    const mirror = document.createElement('input');
    mirror.setAttribute('aria-label', 'Root replacement mirror');
    mirror.setAttribute('type', 'password');
    mirror.value = this.value;
    const echo = document.createElement('span');
    echo.textContent = this.value;
    body.appendChild(mirror);
    body.appendChild(echo);
    replacement.appendChild(body);
    document.replaceChild(replacement, document.documentElement);
  "
>"#
        .to_owned()
    }

    fn input_reactive_safe_fixture() -> String {
        r#"<!doctype html>
<input
  id="reactive-safe-target"
  type="password"
  aria-label="Reactive secret input"
  oninput="
    document.querySelector('#reactive-safe-status').textContent =
      this.value.length > 0 ? 'Password strength: measured' : 'Password strength: waiting';
  "
>
<div id="reactive-safe-status" role="status">Password strength: waiting</div>"#
            .to_owned()
    }

    fn oopif_top_fixture(port: u16) -> String {
        format!(
            r#"<!doctype html>
<title>isolated frame top</title>
<iframe id="child" src="http://localhost:{port}/oopif-child"
  style="position:absolute;left:140px;top:90px;width:480px;height:300px;border:0"></iframe>"#
        )
    }

    fn oopif_child_fixture() -> String {
        r#"<!doctype html>
<style>body { margin: 0; } #frame-button { position: absolute; left: 110px; top: 80px; width: 160px; height: 48px; }</style>
<button id="frame-button">Frame physical</button>
<script>
  const events = [];
  const target = document.querySelector('#frame-button');
  for (const name of ['mousedown', 'mouseup', 'click']) {
    target.addEventListener(name, event => {
      events.push(`${event.type}:${event.isTrusted}`);
      if (event.type === 'click') fetch(`/capture?events=${events.join(',')}`);
    });
  }
  fetch('/capture?events=oopif-ready');
</script>"#
            .to_owned()
    }

    fn assert_actionability(error: Error, expected: ActionabilityError) {
        assert!(
            matches!(error, Error::Actionability(actual) if actual == expected),
            "expected {expected:?}, got {error}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn physical_click_actionability_negatives_stability_and_cancellation() {
        let _guard = browser_test_lock().lock().await;
        if chromium().executable_path().is_none() {
            eprintln!("skipping physical actionability test: Chromium executable unavailable");
            return;
        }
        tokio::task::spawn_blocking(|| {
            let server = ActionFixtureServer::start();
            let browser = chromium()
                .launch(LaunchOptions::default().arg("--no-proxy-server"))
                .expect("launch actionability browser");
            let page = browser.new_page().expect("create actionability page");
            page.goto(
                &server.url("/actionability"),
                GotoOptions::default().wait_until("load").timeout(10_000.0),
            )
            .expect("navigate actionability fixture");
            assert_eq!(server.capture(), "actionability-ready");
            let fixture_state = page
                .evaluate(
                    "({ href: location.href, hidden: !!document.querySelector('#hidden') })",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("inspect actionability fixture");
            assert_eq!(
                fixture_state["hidden"],
                Value::Bool(true),
                "{fixture_state}"
            );

            assert_actionability(
                page.click("#hidden", ActionOptions::timeout(250.0))
                    .expect_err("hidden target must not click"),
                ActionabilityError::NotVisible,
            );
            assert_actionability(
                page.click("#covered", ActionOptions::timeout(250.0))
                    .expect_err("covered target must not click"),
                ActionabilityError::NotReceivingEvents,
            );
            assert_actionability(
                page.click("#disabled", ActionOptions::timeout(250.0))
                    .expect_err("disabled target must not click"),
                ActionabilityError::Disabled,
            );

            page.evaluate(
                "document.querySelector('#moving').classList.add('run')",
                None,
                ActionOptions::timeout(1_000.0),
            )
            .expect("start moving target animation");
            page.click("#moving", ActionOptions::timeout(3_000.0))
                .expect("click moving target after it stabilizes");
            let motion = page
                .evaluate(
                    "({ ended: globalThis.animationEnded, events: globalThis.movingEvents })",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("read moving target evidence");
            assert_eq!(motion["ended"], Value::Bool(true));
            assert_eq!(
                motion["events"]
                    .as_array()
                    .expect("moving target events")
                    .iter()
                    .map(|event| event["type"].as_str().expect("moving event type"))
                    .collect::<Vec<_>>(),
                ["mousedown", "mouseup", "click"]
            );
            assert!(
                motion["events"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .all(|event| event["trusted"] == Value::Bool(true)
                        && event["hit"] == Value::Bool(true))
            );

            page.click("#partially-offscreen", ActionOptions::timeout(3_000.0))
                .expect("click partially-offscreen target at its hit-tested viewport point");
            let partially_offscreen = page
                .evaluate(
                    "globalThis.partiallyOffscreenEvents",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("read partially-offscreen click evidence");
            let partially_offscreen = partially_offscreen
                .as_array()
                .expect("partially-offscreen events");
            assert_eq!(
                partially_offscreen
                    .iter()
                    .map(|event| event["type"].as_str().expect("offscreen event type"))
                    .collect::<Vec<_>>(),
                ["mousedown", "mouseup", "click"]
            );
            assert!(
                partially_offscreen
                    .iter()
                    .all(|event| event["trusted"] == Value::Bool(true)
                        && event["clientX"] == json!(0))
            );

            let detached_precondition = page
                .evaluate(
                    "(() => {
                      const target = document.querySelector('#detach');
                      const rect = target.getBoundingClientRect();
                      globalThis.detachStartScrollY = scrollY;
                      globalThis.detachArmed = true;
                      advanceDetachMotion();
                      return {
                        top: rect.top,
                        bottom: rect.bottom,
                        viewportHeight: innerHeight,
                        scrollY,
                      };
                    })()",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("arm detached target fixture");
            let detached_top = detached_precondition["top"]
                .as_f64()
                .expect("detached fixture top");
            let viewport_height = detached_precondition["viewportHeight"]
                .as_f64()
                .expect("detached fixture viewport height");
            assert!(
                detached_top > viewport_height * 2.0,
                "detached fixture precondition failed: target must start far below the viewport: \
                 {detached_precondition}"
            );

            let detached_click = page.click("#detach", ActionOptions::timeout(3_000.0));
            let detached_evidence = page
                .evaluate(
                    "({
                      scroll: globalThis.detachedScroll,
                      pointerEvents: globalThis.detachedPointerEvents,
                      connected: document.querySelector('#detach')?.isConnected ?? false,
                    })",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("read detached target evidence");
            assert!(
                detached_evidence["scroll"].is_object(),
                "detached fixture precondition failed: actionability never scrolled the target: \
                 {detached_evidence}"
            );
            let detached_error = match detached_click {
                Err(error) => error,
                Ok(()) => panic!("detached target must not click: {detached_evidence}"),
            };
            assert_actionability(detached_error, ActionabilityError::Detached);
            assert_eq!(
                detached_evidence["pointerEvents"],
                json!([]),
                "detached target must not receive pointer events"
            );

            page.goto(
                &server.url("/cancel"),
                GotoOptions::default().wait_until("load").timeout(10_000.0),
            )
            .expect("navigate cancellation fixture");
            assert_eq!(server.capture(), "cancel-ready");
            let cancel = CancelToken::new();
            let click_cancel = cancel.clone();
            let click_page = page.clone();
            // Declared after `browser` so unwind cancels and joins the worker before browser cleanup.
            let click = WorkerGuard {
                cancel: cancel.clone(),
                worker: Some(thread::spawn(move || {
                    click_page.click_with_cancel(
                        "#cancel",
                        ActionOptions::timeout(10_000.0),
                        Some(&click_cancel),
                    )
                })),
            };
            assert_eq!(server.capture(), "actionability-started");
            cancel.cancel();
            assert!(matches!(
                click.join().expect("join cancelled click"),
                Err(Error::Cancelled)
            ));

            browser.close().expect("close actionability browser");
        })
        .await
        .expect("join physical actionability test");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn cancel_between_mouse_press_and_release_still_releases_button() {
        let _guard = browser_test_lock().lock().await;
        if chromium().executable_path().is_none() {
            eprintln!("skipping atomic release test: Chromium executable unavailable");
            return;
        }
        tokio::task::spawn_blocking(|| {
            let server = ActionFixtureServer::start();
            let browser = chromium()
                .launch(LaunchOptions::default().arg("--no-proxy-server"))
                .expect("launch atomic release browser");
            let page = browser.new_page().expect("create atomic release page");
            page.goto(
                &server.url("/atomic-click"),
                GotoOptions::default().wait_until("load").timeout(10_000.0),
            )
            .expect("navigate atomic release fixture");
            assert_eq!(server.capture(), "atomic-ready");

            let cancel = CancelToken::new();
            let click_cancel = cancel.clone();
            let click_page = page.clone();
            // Declared after `browser` so unwind cancels and joins the worker before browser cleanup.
            let click = WorkerGuard {
                cancel: cancel.clone(),
                worker: Some(thread::spawn(move || {
                    click_page.click_with_cancel(
                        "#atomic",
                        ActionOptions::timeout(5_000.0),
                        Some(&click_cancel),
                    )
                })),
            };
            assert_eq!(server.capture(), "atomic-mousedown");
            cancel.cancel();
            click
                .join()
                .expect("join atomic release click")
                .expect("late cancellation must finish the committed click");

            let evidence = page
                .evaluate(
                    "({ events: globalThis.atomicEvents, buttonDown: globalThis.buttonDown })",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("read atomic release evidence");
            assert_eq!(
                evidence["events"]
                    .as_array()
                    .expect("atomic release events")
                    .iter()
                    .map(|event| event["type"].as_str().expect("atomic release event type"))
                    .collect::<Vec<_>>(),
                ["mousedown", "mouseup", "click"]
            );
            assert_eq!(evidence["buttonDown"], Value::Bool(false));

            page.click("#following", ActionOptions::timeout(3_000.0))
                .expect("following click must work after late cancellation");
            assert_eq!(
                page.evaluate(
                    "globalThis.followingEffectCount",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("read following click effect"),
                json!(1)
            );
            browser.close().expect("close atomic release browser");
        })
        .await
        .expect("join atomic release test");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn cancel_after_committed_click_reports_success_and_effect_once() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let initial = actor
            .execute_with_timeout(
                request_id(60_000),
                BrowserOp::Navigate(server.url("/atomic-click")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to committed click fixture");
        assert!(output_text(&initial).contains("Atomic click"));
        assert!(output_text(&initial).contains("[ref=e1]"));
        assert_eq!(server.capture(), "atomic-ready");

        let click_id = request_id(60_001);
        let click_actor = Arc::clone(&actor);
        let click_request_id = click_id.clone();
        let click = tokio::spawn(async move {
            click_actor
                .execute_with_timeout(
                    click_request_id,
                    BrowserOp::Click {
                        target: "e1".to_owned(),
                        double_click: false,
                    },
                    Duration::from_secs(5),
                )
                .await
        });
        wait_until_in_flight(&actor, &click_id).await;
        assert_eq!(server.capture(), "atomic-mousedown");
        assert!(
            !actor.cancel(&click_id),
            "cancellation after physical dispatch must be a no-op-too-late"
        );

        let result = click
            .await
            .expect("join committed actor click")
            .expect("a committed actor click must not report cancellation");
        let result = output_text(&result);
        assert!(
            result.contains("Atomic click effect 1"),
            "the post-click snapshot must observe exactly one effect: {result}"
        );

        let second_target = snapshot_ref(result, "button", "Atomic click effect 1");
        let second_click_id = request_id(69_300);
        let second_click_actor = Arc::clone(&actor);
        let second_click_request_id = second_click_id.clone();
        let second_click = tokio::spawn(async move {
            second_click_actor
                .execute_with_timeout(
                    second_click_request_id,
                    BrowserOp::Click {
                        target: second_target,
                        double_click: false,
                    },
                    Duration::from_secs(5),
                )
                .await
        });
        wait_until_in_flight(&actor, &second_click_id).await;
        assert_eq!(server.capture(), "atomic-mousedown");
        assert!(
            !actor.cancel(&second_click_id),
            "the second cancellation must also lose physical-action arbitration"
        );
        let second_result = second_click
            .await
            .expect("join second committed actor click")
            .expect("a second committed actor click must not report cancellation");
        let second_result = output_text(&second_result);
        assert!(
            second_result.contains("Atomic click effect 2"),
            "the second click must land exactly once: {second_result}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn fill_form_cancel_during_trailing_committed_checkbox_reports_success_with_every_field_written()
     {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(69_000),
                BrowserOp::Navigate(server.url("/fill-final-commit")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to trailing committed checkbox fixture");
        assert_eq!(server.capture(), "fill-final-commit-ready");
        let snapshot = output_text(&snapshot);
        let fields = vec![
            FillField {
                target: snapshot_ref(snapshot, "textbox", "First field"),
                name: "First field".to_owned(),
                kind: FillFieldKind::Textbox,
                value: "written-before-final".to_owned(),
            },
            FillField {
                target: snapshot_ref(snapshot, "checkbox", "Final checkbox"),
                name: "Final checkbox".to_owned(),
                kind: FillFieldKind::Checkbox,
                value: "true".to_owned(),
            },
        ];

        let fill_id = request_id(69_001);
        let fill_actor = Arc::clone(&actor);
        let fill_request_id = fill_id.clone();
        let fill = tokio::spawn(async move {
            fill_actor
                .execute_with_timeout(
                    fill_request_id,
                    BrowserOp::FillForm(fields),
                    Duration::from_secs(5),
                )
                .await
        });
        wait_until_in_flight(&actor, &fill_id).await;
        assert_eq!(server.capture(), "fill-final-commit-point");
        assert!(
            !actor.cancel(&fill_id),
            "cancellation during the final committed checkbox must lose arbitration"
        );

        let filled = fill
            .await
            .expect("join trailing committed checkbox fill")
            .expect("a fully written form must report success");
        let filled = output_text(&filled);
        assert!(
            filled.contains(r#"[value="written-before-final"]"#),
            "{filled}"
        );
        assert!(
            filled.contains(r#"- status "Written fields: First field, Final checkbox""#),
            "{filled}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn fill_form_cancel_during_nonphysical_field_preserves_conservative_partial_detail() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(69_100),
                BrowserOp::Navigate(server.url("/fill-nonphysical-cancel")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to nonphysical fill cancellation fixture");
        assert_eq!(server.capture(), "fill-nonphysical-cancel-ready");
        let snapshot = output_text(&snapshot);
        let fields = vec![
            FillField {
                target: snapshot_ref(snapshot, "textbox", "Field A"),
                name: "Field A".to_owned(),
                kind: FillFieldKind::Textbox,
                value: "confirmed-a".to_owned(),
            },
            FillField {
                target: snapshot_ref(snapshot, "textbox", "Field B"),
                name: "Field B".to_owned(),
                kind: FillFieldKind::Textbox,
                value: "possibly-written-b".to_owned(),
            },
            FillField {
                target: snapshot_ref(snapshot, "textbox", "Field C"),
                name: "Field C".to_owned(),
                kind: FillFieldKind::Textbox,
                value: "must-not-write-c".to_owned(),
            },
        ];

        let fill_id = request_id(69_101);
        let fill_actor = Arc::clone(&actor);
        let fill_request_id = fill_id.clone();
        let fill = tokio::spawn(async move {
            fill_actor
                .execute_with_timeout(
                    fill_request_id,
                    BrowserOp::FillForm(fields),
                    Duration::from_secs(5),
                )
                .await
        });
        wait_until_in_flight(&actor, &fill_id).await;
        assert_eq!(server.capture(), "fill-nonphysical-cancel-point");
        assert!(
            actor.cancel(&fill_id),
            "a nonphysical fill must remain cancellable"
        );

        let partial = fill
            .await
            .expect("join nonphysical cancelled fill")
            .expect_err("the cancelled fill must report a partial error");
        let BrowserError::Message(partial) = partial else {
            panic!("partial detail must survive completion, got {partial:?}");
        };
        assert!(
            partial.contains("stopped by cancellation while processing field \"Field B\""),
            "{partial}"
        );
        assert!(
            partial.contains("fields confirmed complete before it: Field A"),
            "{partial}"
        );
        assert!(
            partial.contains("The stopped field may also have been written"),
            "{partial}"
        );

        let snapshot = actor
            .execute_with_timeout(
                request_id(69_102),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("snapshot after nonphysical partial fill");
        let snapshot = output_text(&snapshot);
        assert!(snapshot.contains(r#"[value="confirmed-a"]"#), "{snapshot}");
        assert!(
            snapshot.contains(r#"[value="possibly-written-b"]"#),
            "{snapshot}"
        );
        assert!(
            !snapshot.contains(r#"[value="must-not-write-c"]"#),
            "the field after the cancellation point must never be written: {snapshot}"
        );
        assert!(
            snapshot.contains(r#"- status "Written fields: Field A, Field B""#),
            "the page must record no write after Field B: {snapshot}"
        );

        let field_c = snapshot_ref(snapshot, "textbox", "Field C");
        actor
            .execute_with_timeout(
                request_id(69_110),
                BrowserOp::Type {
                    target: field_c,
                    text: "trusted-after-cancel".to_owned(),
                    submit: false,
                    slowly: false,
                    clear: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("trusted type into a different field after cancellation");
        let snapshot = actor
            .execute_with_timeout(
                request_id(69_111),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("snapshot after trusted post-cancellation type");
        let snapshot = output_text(&snapshot);
        assert!(
            snapshot.contains(r#"[value="trusted-after-cancel"]"#),
            "trusted typing must land after a cancelled fill: {snapshot}"
        );
        assert!(
            snapshot.contains(r#"- status "Written fields: Field A, Field B, Field C"#),
            "the page must observe the trusted post-cancellation input, \
             which the fixture records once per typed character: {snapshot}"
        );

        let snapshot = actor
            .execute_with_timeout(
                request_id(69_103),
                BrowserOp::Navigate(server.url("/fill-nonphysical-cancel")),
                Duration::from_secs(10),
            )
            .await
            .expect("reset actor for first-field cancellation fixture");
        assert_eq!(server.capture(), "fill-nonphysical-cancel-ready");
        let snapshot = output_text(&snapshot);
        let fields = vec![
            FillField {
                target: snapshot_ref(snapshot, "textbox", "Field B"),
                name: "Field B".to_owned(),
                kind: FillFieldKind::Textbox,
                value: "possibly-written-first".to_owned(),
            },
            FillField {
                target: snapshot_ref(snapshot, "textbox", "Field C"),
                name: "Field C".to_owned(),
                kind: FillFieldKind::Textbox,
                value: "must-not-write-after-first".to_owned(),
            },
        ];

        let fill_id = request_id(69_104);
        let fill_actor = Arc::clone(&actor);
        let fill_request_id = fill_id.clone();
        let fill = tokio::spawn(async move {
            fill_actor
                .execute_with_timeout(
                    fill_request_id,
                    BrowserOp::FillForm(fields),
                    Duration::from_secs(5),
                )
                .await
        });
        wait_until_in_flight(&actor, &fill_id).await;
        assert_eq!(server.capture(), "fill-nonphysical-cancel-point");
        assert!(
            actor.cancel(&fill_id),
            "a first nonphysical field must remain cancellable"
        );

        let partial = fill
            .await
            .expect("join first-field cancelled fill")
            .expect_err("the first-field cancellation must report a partial error");
        let BrowserError::Message(partial) = partial else {
            panic!("first-field partial detail must survive completion, got {partial:?}");
        };
        assert!(
            partial.contains("stopped by cancellation while processing field \"Field B\""),
            "{partial}"
        );
        assert!(
            partial.contains("fields confirmed complete before it: none"),
            "{partial}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn fill_form_timeout_between_fields_preserves_partial_detail() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(69_200),
                BrowserOp::Navigate(server.url("/fill-final-commit")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to between-field timeout fixture");
        assert_eq!(server.capture(), "fill-final-commit-ready");
        let snapshot = output_text(&snapshot);
        let fields = vec![
            FillField {
                target: snapshot_ref(snapshot, "checkbox", "Final checkbox"),
                name: "Final checkbox".to_owned(),
                kind: FillFieldKind::Checkbox,
                value: "true".to_owned(),
            },
            FillField {
                target: snapshot_ref(snapshot, "textbox", "First field"),
                name: "First field".to_owned(),
                kind: FillFieldKind::Textbox,
                value: "must-not-write".to_owned(),
            },
        ];

        let partial = actor
            .execute_with_timeout(
                request_id(69_201),
                BrowserOp::FillForm(fields),
                Duration::from_millis(200),
            )
            .await
            .expect_err("the between-field timeout must report a partial error");
        let BrowserError::Message(partial) = partial else {
            panic!("between-field timeout must preserve partial detail, got {partial:?}");
        };
        assert!(
            partial.contains("stopped by timeout while processing field \"First field\""),
            "{partial}"
        );
        assert!(
            partial.contains("fields confirmed complete before it: Final checkbox"),
            "{partial}"
        );

        assert_eq!(server.capture(), "fill-final-commit-point");
        let snapshot = actor
            .execute_with_timeout(
                request_id(69_202),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("snapshot after between-field timeout");
        let snapshot = output_text(&snapshot);
        assert!(
            !snapshot.contains(r#"[value="must-not-write"]"#),
            "{snapshot}"
        );
        assert!(
            snapshot.contains(r#"- status "Written fields: Final checkbox""#),
            "{snapshot}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn fill_form_cancel_after_physical_commit_stops_later_fields_and_reports_partial_result()
    {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(67_100),
                BrowserOp::Navigate(server.url("/fill-cancel")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to fill cancellation fixture");
        assert_eq!(server.capture(), "fill-cancel-ready");
        let snapshot = output_text(&snapshot);

        let mut fields = vec![FillField {
            target: snapshot_ref(snapshot, "checkbox", "Commit checkbox"),
            name: "Commit checkbox".to_owned(),
            kind: FillFieldKind::Checkbox,
            value: "true".to_owned(),
        }];
        for index in 1..=12 {
            let name = format!("Later field {index:02}");
            fields.push(FillField {
                target: snapshot_ref(snapshot, "textbox", &name),
                name,
                kind: FillFieldKind::Textbox,
                value: format!("cancelled-write-{index:02}"),
            });
        }

        let fill_id = request_id(67_101);
        let fill_actor = Arc::clone(&actor);
        let fill_request_id = fill_id.clone();
        let fill = tokio::spawn(async move {
            fill_actor
                .execute_with_timeout(
                    fill_request_id,
                    BrowserOp::FillForm(fields),
                    Duration::from_secs(10),
                )
                .await
        });
        wait_until_in_flight(&actor, &fill_id).await;
        assert_eq!(
            server.capture(),
            "fill-cancel-point",
            "the cancellation point must occur inside the committed checkbox dispatch"
        );
        let cancellation_won_current_action = actor.cancel(&fill_id);
        let fill_result = tokio::time::timeout(Duration::from_secs(5), fill)
            .await
            .expect("cancelled fill form should finish promptly")
            .expect("fill form task should not panic");

        let snapshot = actor
            .execute_with_timeout(
                request_id(67_102),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("snapshot after partially cancelled fill form");
        let snapshot = output_text(&snapshot);
        for index in 1..=12 {
            let unexpected = format!(r#"[value="cancelled-write-{index:02}"]"#);
            assert!(
                !snapshot.contains(&unexpected),
                "Later field {index:02} was written after cancellation \
                 (current-action arbitration won={cancellation_won_current_action}):\n{snapshot}"
            );
        }
        assert!(
            snapshot.contains(r#"- status "Written fields: Commit checkbox""#),
            "only the already-committed checkbox may be written:\n{snapshot}"
        );

        let partial = match fill_result {
            Ok(BrowserOutput::Text(text)) | Err(BrowserError::Message(text)) => text,
            other => panic!("fill form must report a named partial result, got {other:?}"),
        };
        assert!(
            partial.to_ascii_lowercase().contains("partial") && partial.contains("Commit checkbox"),
            "partial fill result must name the committed field: {partial}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_tool_type_password_snapshot_masks_new_sentinel() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(61_000),
                BrowserOp::Navigate(server.url("/input")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to input fixture");
        let snapshot_text = output_text(&snapshot);
        assert!(!snapshot_text.contains("[value=••••••]"), "{snapshot_text}");
        assert!(
            snapshot_text.contains("Secret length: 0"),
            "{snapshot_text}"
        );
        let target = snapshot_ref(snapshot_text, "textbox", "Secret input");
        let sentinel = "actor-password-sentinel-61001";

        let typed = actor
            .execute_with_timeout(
                request_id(61_001),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into snapshot ref");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert!(
            typed.contains(&format!("Secret length: {}", sentinel.chars().count())),
            "{typed}"
        );
        assert!(!typed.contains(sentinel), "{typed}");
        assert!(!typed.contains("do-not-render"), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_tool_type_password_snapshot_masks_aria_and_status_echoes() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_000),
                BrowserOp::Navigate(server.url("/input-aria-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to password echo fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Secret echo input");
        let sentinel = "synthetic-password-echo-canary-66001";

        let typed = actor
            .execute_with_timeout(
                request_id(66_001),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into password echo fixture");
        let typed = output_text(&typed);
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_tool_type_password_snapshot_masks_roleless_leaf_echo() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_300),
                BrowserOp::Navigate(server.url("/input-leaf-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to password leaf echo fixture");
        let snapshot = output_text(&snapshot);
        assert!(snapshot.contains("- text: No secret entered"), "{snapshot}");
        let target = snapshot_ref(snapshot, "textbox", "Secret leaf input");
        let sentinel = "synthetic-password-leaf-canary-66301";

        let typed = actor
            .execute_with_timeout(
                request_id(66_301),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into password leaf echo fixture");
        let typed = output_text(&typed);
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_aria_status_echo_snapshot_has_structure() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_310),
                BrowserOp::Navigate(server.url("/input-aria-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to structural aria/status echo fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Secret echo input");
        let sentinel = "structural-aria-status-canary-66311";

        let typed = actor
            .execute_with_timeout(
                request_id(66_311),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into structural aria/status echo fixture");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_roleless_leaf_echo_snapshot_has_structure() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_320),
                BrowserOp::Navigate(server.url("/input-leaf-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to structural roleless-leaf fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Secret leaf input");
        let sentinel = "structural-roleless-leaf-canary-66321";

        let typed = actor
            .execute_with_timeout(
                request_id(66_321),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into structural roleless-leaf fixture");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_taint_persists_across_later_snapshots() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_400),
                BrowserOp::Navigate(server.url("/input-aria-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to persistent password echo fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Secret echo input");
        let sentinel = "persistent-password-echo-canary-66401";

        let typed = actor
            .execute_with_timeout(
                request_id(66_401),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into persistent password echo fixture");
        assert!(
            !output_text(&typed).contains(sentinel),
            "{}",
            output_text(&typed)
        );

        let first_snapshot = actor
            .execute_with_timeout(
                request_id(66_402),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("take first later password snapshot");
        assert!(
            output_text(&first_snapshot).contains("[value=••••••]"),
            "{}",
            output_text(&first_snapshot)
        );

        let second_snapshot = actor
            .execute_with_timeout(
                request_id(66_403),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("take second later password snapshot");
        let second_snapshot = output_text(&second_snapshot);
        assert!(
            second_snapshot.contains("[value=••••••]"),
            "{second_snapshot}"
        );
        assert!(!second_snapshot.contains(sentinel), "{second_snapshot}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn fill_form_password_snapshot_masks_reactive_echo() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_500),
                BrowserOp::Navigate(server.url("/input-fill-form-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to fill-form password echo fixture");
        let snapshot = output_text(&snapshot);
        let first_target = snapshot_ref(snapshot, "textbox", "First fill secret");
        let second_target = snapshot_ref(snapshot, "textbox", "Second fill secret");
        let first_sentinel = "first-fill-form-password-canary-66501";
        let second_sentinel = "second-fill-form-password-canary-66502";

        let filled = actor
            .execute_with_timeout(
                request_id(66_501),
                BrowserOp::FillForm(vec![
                    FillField {
                        target: first_target,
                        name: "first password".to_owned(),
                        kind: FillFieldKind::Textbox,
                        value: first_sentinel.to_owned(),
                    },
                    FillField {
                        target: second_target,
                        name: "second password".to_owned(),
                        kind: FillFieldKind::Textbox,
                        value: second_sentinel.to_owned(),
                    },
                ]),
                Duration::from_secs(5),
            )
            .await
            .expect("fill password through browser_fill_form");
        let filled = output_text(&filled);
        assert_eq!(filled.matches("[value=••••••]").count(), 2, "{filled}");
        assert!(!filled.contains(first_sentinel), "{filled}");
        assert!(!filled.contains(second_sentinel), "{filled}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_aria_labelledby_echo() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_600),
                BrowserOp::Navigate(server.url("/input-labelledby-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to aria-labelledby password echo fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Secret labelledby input");
        let sentinel = "labelledby-password-echo-canary-66601";

        let typed = actor
            .execute_with_timeout(
                request_id(66_601),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into aria-labelledby password echo fixture");
        let typed = output_text(&typed);
        assert!(
            typed
                .lines()
                .any(|line| line.trim().starts_with("- button [ref=")),
            "{typed}"
        );
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_label_echo() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_700),
                BrowserOp::Navigate(server.url("/input-label-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to label password echo fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Secret label input");
        let sentinel = "label-password-echo-canary-66701";

        let typed = actor
            .execute_with_timeout(
                request_id(66_701),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into label password echo fixture");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=\"safe echo value\"]"), "{typed}");
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_frame_href_and_cleartext_values() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_800),
                BrowserOp::Navigate(server.url("/input-output-branch-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to password output-branch fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Secret output input");
        let sentinel = "output-branch-password-echo-canary-66801";

        let typed = actor
            .execute_with_timeout(
                request_id(66_801),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into password output-branch fixture");
        let typed = output_text(&typed);
        assert!(
            typed.contains("- iframe \"\" (content not captured)"),
            "{typed}"
        );
        assert_eq!(typed.matches("[value=••••••]").count(), 2, "{typed}");
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_property_only_value_echo() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_810),
                BrowserOp::Navigate(server.url("/input-property-only-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to property-only password echo fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Property-only secret");
        let sentinel = "property-only-password-echo-canary-66811";

        let typed = actor
            .execute_with_timeout(
                request_id(66_811),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into property-only password echo fixture");
        let typed = output_text(&typed);
        assert_eq!(typed.matches("[value=••••••]").count(), 2, "{typed}");
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn post_dispatch_password_type_timeout_reports_masked_completion() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_812),
                BrowserOp::Navigate(server.url("/input-post-dispatch-timeout-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to post-dispatch timeout fixture");
        let target = snapshot_ref(
            output_text(&snapshot),
            "textbox",
            "Post-dispatch timeout secret",
        );
        let sentinel = "post-dispatch-timeout-password-canary-66813";

        let typed = actor
            .execute_with_timeout(
                request_id(66_813),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_millis(200),
            )
            .await
            .expect("landed password type must report complete or partial success");
        let typed = output_text(&typed);
        assert!(typed.contains("Action partially completed"), "{typed}");
        assert_eq!(typed.matches("[value=••••••]").count(), 2, "{typed}");
        assert!(!typed.contains(sentinel), "{typed}");

        let later = actor
            .execute_with_timeout(
                request_id(66_814),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("snapshot after post-dispatch type timeout");
        let later = output_text(&later);
        assert_eq!(later.matches("[value=••••••]").count(), 2, "{later}");
        assert!(!later.contains(sentinel), "{later}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn post_dispatch_password_fill_timeout_reports_masked_completion() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_815),
                BrowserOp::Navigate(server.url("/input-post-dispatch-timeout-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to post-dispatch fill timeout fixture");
        let target = snapshot_ref(
            output_text(&snapshot),
            "textbox",
            "Post-dispatch timeout secret",
        );
        let sentinel = "post-dispatch-fill-timeout-password-canary-66816";

        let filled = actor
            .execute_with_timeout(
                request_id(66_816),
                BrowserOp::FillForm(vec![FillField {
                    target,
                    name: "post-dispatch password".to_owned(),
                    kind: FillFieldKind::Textbox,
                    value: sentinel.to_owned(),
                }]),
                Duration::from_millis(200),
            )
            .await
            .expect("landed password fill must report complete or partial success");
        let filled = output_text(&filled);
        assert!(filled.contains("Action partially completed"), "{filled}");
        assert_eq!(filled.matches("[value=••••••]").count(), 2, "{filled}");
        assert!(!filled.contains(sentinel), "{filled}");

        let later = actor
            .execute_with_timeout(
                request_id(66_817),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("snapshot after post-dispatch fill timeout");
        let later = output_text(&later);
        assert_eq!(later.matches("[value=••••••]").count(), 2, "{later}");
        assert!(!later.contains(sentinel), "{later}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_submit_failure_resolves_taint_and_reports_partial_completion() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_820),
                BrowserOp::Navigate(server.url("/input-submit-failure-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to submit-failure password echo fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Submit failure secret");
        let sentinel = "submit-failure-password-echo-canary-66821";

        let typed = actor
            .execute_with_timeout(
                request_id(66_821),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: true,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await;
        let later_snapshot = actor
            .execute_with_timeout(
                request_id(66_822),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("snapshot after the password write and failed submit");
        let later_snapshot = output_text(&later_snapshot);
        assert!(
            later_snapshot.contains("[value=••••••]"),
            "{later_snapshot}"
        );
        assert!(!later_snapshot.contains(sentinel), "{later_snapshot}");

        assert!(
            typed.is_ok(),
            "password write must remain successful when the later submit fails: {typed:?}"
        );
        let typed = typed.expect("checked successful partial completion above");
        let typed = output_text(&typed);
        assert!(typed.contains("Action partially completed"), "{typed}");
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn fill_form_later_field_failure_reports_masked_partial_completion() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_830),
                BrowserOp::Navigate(server.url("/input-partial-fill-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to partial fill-form fixture");
        let snapshot = output_text(&snapshot);
        let password_target = snapshot_ref(snapshot, "textbox", "Partial fill secret");
        let checkbox_target = snapshot_ref(snapshot, "checkbox", "Partial fill checkbox");
        let sentinel = "partial-fill-password-echo-canary-66831";

        let filled = actor
            .execute_with_timeout(
                request_id(66_831),
                BrowserOp::FillForm(vec![
                    FillField {
                        target: password_target,
                        name: "partial password".to_owned(),
                        kind: FillFieldKind::Textbox,
                        value: sentinel.to_owned(),
                    },
                    FillField {
                        target: checkbox_target,
                        name: "invalid checkbox".to_owned(),
                        kind: FillFieldKind::Checkbox,
                        value: "not-a-boolean".to_owned(),
                    },
                ]),
                Duration::from_secs(5),
            )
            .await;
        assert!(
            filled.is_ok(),
            "earlier form write must remain successful when a later field fails: {filled:?}"
        );
        let filled = filled.expect("checked successful partial completion above");
        let filled = output_text(&filled);
        assert!(filled.contains("Action partially completed"), "{filled}");
        assert!(
            filled.contains("1 of 2 form fields were written"),
            "{filled}"
        );
        assert_eq!(filled.matches("[value=••••••]").count(), 2, "{filled}");
        assert!(!filled.contains(sentinel), "{filled}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_partial_type_value_that_landed() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(68_000),
                BrowserOp::Navigate(server.url("/input-partial-type-value-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to partial type-value fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Partial type secret");

        let typed = actor
            .execute_with_timeout(
                request_id(68_001),
                BrowserOp::Type {
                    target,
                    text: "correct-horse".to_owned(),
                    submit: false,
                    slowly: false,
                    clear: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type a truncated password");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert!(!typed.contains("corr"), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_partial_fill_form_value_that_landed() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(68_010),
                BrowserOp::Navigate(server.url("/input-partial-fill-value-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to partial fill-form value fixture");
        let target = snapshot_ref(
            output_text(&snapshot),
            "textbox",
            "Partial fill value secret",
        );

        let filled = actor
            .execute_with_timeout(
                request_id(68_011),
                BrowserOp::FillForm(vec![FillField {
                    target,
                    name: "partial password".to_owned(),
                    kind: FillFieldKind::Textbox,
                    value: "truncated-secret".to_owned(),
                }]),
                Duration::from_secs(5),
            )
            .await
            .expect("fill a truncated password");
        let filled = output_text(&filled);
        assert!(filled.contains("[value=••••••]"), "{filled}");
        assert!(!filled.contains("trun"), "{filled}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_secret_composed_across_labelledby_targets() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(68_020),
                BrowserOp::Navigate(server.url("/input-split-labelledby-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to split labelledby fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Split labelledby secret");

        let typed = actor
            .execute_with_timeout(
                request_id(68_021),
                BrowserOp::Type {
                    target,
                    text: "secret phrase".to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into split labelledby fixture");
        let typed = output_text(&typed);
        assert!(
            typed
                .lines()
                .any(|line| line.trim().starts_with("- button [ref=")),
            "{typed}"
        );
        assert!(!typed.contains("secret phrase"), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_secret_revealed_by_aria_hidden_ancestor() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(68_030),
                BrowserOp::Navigate(server.url("/input-aria-hidden-ancestor-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to ARIA-hidden ancestor fixture");
        let target = snapshot_ref(
            output_text(&snapshot),
            "textbox",
            "ARIA-hidden ancestor secret",
        );
        let sentinel = "aria-hidden-password-canary-68031";

        let typed = actor
            .execute_with_timeout(
                request_id(68_031),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into ARIA-hidden ancestor fixture");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_write_masks_pending_dialog_message() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(68_040),
                BrowserOp::Navigate(server.url("/input-dialog-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to dialog password echo fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Dialog secret input");
        let sentinel = "dialog-password-canary-68041";

        let typed = actor
            .execute_with_timeout(
                request_id(68_041),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: true,
                    clear: true,
                },
                Duration::from_secs(10),
            )
            .await
            .expect("type into dialog password echo fixture");
        let typed = output_text(&typed).to_owned();
        actor
            .execute_with_timeout(
                request_id(68_042),
                BrowserOp::HandleDialog {
                    accept: true,
                    prompt_text: None,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("dismiss password echo dialog");

        assert!(typed.contains("Dialog pending"), "{typed}");
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_dialog_message_stays_masked_for_later_unrelated_tools() {
        // Masking the write's own reply protects one response. The dialog text
        // outlives that reply, and every other tool reaches it through the
        // generic modal gate, which has no secret in hand -- so the guarantee
        // only holds if the stored message was masked, not the rendering.
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(68_060),
                BrowserOp::Navigate(server.url("/input-dialog-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to dialog password echo fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Dialog secret input");
        let sentinel = "dialog-password-canary-68061";

        let typed = actor
            .execute_with_timeout(
                request_id(68_061),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: true,
                    clear: true,
                },
                Duration::from_secs(10),
            )
            .await
            .expect("type into dialog password echo fixture");
        let typed = output_text(&typed).to_owned();

        // The dialog is still up. A tool that knows nothing about the write now
        // renders the same stored message.
        let blocked = actor
            .execute_with_timeout(
                request_id(68_062),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(10),
            )
            .await
            .expect("snapshot must defer behind the pending dialog rather than fail");
        let blocked = output_text(&blocked).to_owned();

        actor
            .execute_with_timeout(
                request_id(68_063),
                BrowserOp::HandleDialog {
                    accept: true,
                    prompt_text: None,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("dismiss password echo dialog");

        assert!(!typed.contains(sentinel), "the write's own reply: {typed}");
        assert!(
            blocked.contains("Dialog pending"),
            "the later call must be fronted by the modal gate: {blocked}"
        );
        assert!(
            !blocked.contains(sentinel),
            "a later tool must not echo the secret the dialog is displaying: {blocked}"
        );
        assert!(
            blocked.contains(SECRET_MASK),
            "the later call must render the masked message: {blocked}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_keeps_tainted_custom_aria_control_addressable() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(68_050),
                BrowserOp::Navigate(server.url("/input-custom-role-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to custom role password echo fixture");
        let target = snapshot_ref(
            output_text(&snapshot),
            "textbox",
            "Custom role secret input",
        );
        let sentinel = "custom-role-password-canary-68051";

        let typed = actor
            .execute_with_timeout(
                request_id(68_051),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into custom role password echo fixture");
        let typed = output_text(&typed);
        assert!(
            typed
                .lines()
                .any(|line| line.trim().starts_with("- generic [ref=")),
            "{typed}"
        );
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_secret_composed_by_descendant_mutations() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_860),
                BrowserOp::Navigate(server.url("/input-split-ancestor-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to split-ancestor fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Split ancestor secret");
        let sentinel = "split-ancestor-password-canary-66861";

        let typed = actor
            .execute_with_timeout(
                request_id(66_861),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into split-ancestor fixture");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert!(
            typed
                .lines()
                .any(|line| line.trim().starts_with("- button [ref=")),
            "{typed}"
        );
        assert!(!typed.contains(sentinel), "{typed}");
    }

    /// A container's own name can only come from author-written markup, so it
    /// cannot hold a secret typed after the page was written. Blanking every
    /// ancestor of a tainted node erased those labels too and left the caller
    /// with an unnamed region it could no longer address. The resolver never
    /// taints such a container -- `containsSensitiveValue` reads its
    /// `aria-label`, not its text -- so masking it is pure loss.
    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_preserves_author_static_container_label() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_960),
                BrowserOp::Navigate(server.url("/input-labelled-container-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to labelled container fixture");
        let target = snapshot_ref(
            output_text(&snapshot),
            "textbox",
            "Labelled container secret",
        );
        let sentinel = "labelled-container-password-canary-66961";

        let typed = actor
            .execute_with_timeout(
                request_id(66_961),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into labelled container fixture");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert!(!typed.contains(sentinel), "{typed}");
        assert!(
            typed.contains("- region \"Account details\""),
            "an author-static container label survives a password write: {typed}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_name_changed_only_by_id_relationship() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_870),
                BrowserOp::Navigate(server.url("/input-id-relationship-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to ID relationship fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "ID relationship secret");
        let sentinel = "id-relationship-password-canary-66871";

        let typed = actor
            .execute_with_timeout(
                request_id(66_871),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into ID relationship fixture");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert!(
            typed
                .lines()
                .any(|line| line.trim().starts_with("- button [ref=")),
            "{typed}"
        );
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_css_visibility_transition_echo() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_880),
                BrowserOp::Navigate(server.url("/input-visibility-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to visibility transition fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Visibility secret input");
        let sentinel = "visibility-password-canary-66881";

        let typed = actor
            .execute_with_timeout(
                request_id(66_881),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into visibility transition fixture");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_exact_role_echo() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_840),
                BrowserOp::Navigate(server.url("/input-role-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to role password echo fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Role secret input");
        let sentinel = "role-password-echo-canary-66841";

        let typed = actor
            .execute_with_timeout(
                request_id(66_841),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into role password echo fixture");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert!(
            typed
                .lines()
                .any(|line| line.trim().starts_with("- button [ref=")),
            "{typed}"
        );
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn password_snapshot_masks_same_document_root_replacement_echo() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_850),
                BrowserOp::Navigate(server.url("/input-root-replacement-echo")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to root-replacement password echo fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Root replacement secret");
        let sentinel = "root-replacement-password-echo-canary-66851";

        let typed = actor
            .execute_with_timeout(
                request_id(66_851),
                BrowserOp::Type {
                    target,
                    text: sentinel.to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into root-replacement password echo fixture");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert!(!typed.contains(sentinel), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_tool_type_password_snapshot_preserves_unrelated_short_secret_text() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_100),
                BrowserOp::Navigate(server.url("/input-aria-echo-common")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to password echo fixture with common text");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Secret echo input");

        let typed = actor
            .execute_with_timeout(
                request_id(66_101),
                BrowserOp::Type {
                    target,
                    text: "a".to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type common password into echo fixture");
        let typed = output_text(&typed);
        assert!(typed.contains("[value=••••••]"), "{typed}");
        assert_eq!(
            typed
                .lines()
                .filter(|line| line.trim() == "- status \"a\"")
                .count(),
            1,
            "{typed}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_tool_type_password_snapshot_preserves_reactive_safe_text() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(66_200),
                BrowserOp::Navigate(server.url("/input-reactive-safe")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to reactive safe fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Reactive secret input");

        let typed = actor
            .execute_with_timeout(
                request_id(66_201),
                BrowserOp::Type {
                    target,
                    text: "xy".to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("type into reactive safe fixture");
        let typed = output_text(&typed);
        assert!(typed.contains("Password strength: measured"), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_tool_default_clear_type_leaves_change_pending_until_trusted_blur() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(61_100),
                BrowserOp::Navigate(server.url("/input")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to input fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Type target");

        let typed = actor
            .execute_with_timeout(
                request_id(61_101),
                BrowserOp::Type {
                    target,
                    text: "typed value".to_owned(),
                    submit: false,
                    slowly: false,
                    clear: true,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("fill snapshot ref");
        let typed = output_text(&typed);
        assert!(typed.contains(r#"[value="typed value"]"#), "{typed}");
        assert!(typed.contains("Typed value: typed value"), "{typed}");
        assert!(
            typed.contains("Type change value: none; trusted: none"),
            "{typed}"
        );
        assert!(!typed.contains("Type change value: (empty)"), "{typed}");
        let blur_target = snapshot_ref(&typed, "button", "Hover target");

        let blurred = actor
            .execute_with_timeout(
                request_id(61_102),
                BrowserOp::Click {
                    target: blur_target,
                    double_click: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("blur typed field through a trusted click");
        let blurred = output_text(&blurred);
        assert!(
            blurred.contains("Type change value: typed value; trusted: true"),
            "{blurred}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn cancel_during_slow_type_stops_between_characters_and_before_submit() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(61_200),
                BrowserOp::Navigate(server.url("/input-slow-cancel")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to input fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Type target");

        let type_id = request_id(61_201);
        let type_actor = Arc::clone(&actor);
        let type_request_id = type_id.clone();
        let typed = tokio::spawn(async move {
            type_actor
                .execute_with_timeout(
                    type_request_id,
                    BrowserOp::Type {
                        target,
                        text: "slow".to_owned(),
                        submit: true,
                        slowly: true,
                        clear: true,
                    },
                    Duration::from_secs(5),
                )
                .await
        });
        wait_until_in_flight(&actor, &type_id).await;
        assert_eq!(
            server.capture(),
            "input-keydown",
            "the first typed character must land before cancellation"
        );
        assert!(
            actor.cancel(&type_id),
            "typing characters must not claim request-level commitment"
        );
        assert_eq!(
            typed.await.expect("join cancelled slow type"),
            Err(BrowserError::Cancelled)
        );

        let snapshot = actor
            .execute_with_timeout(
                request_id(61_202),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("snapshot after cancelled slow type");
        let snapshot = output_text(&snapshot);
        assert!(snapshot.contains(r#"[value="s"]"#), "{snapshot}");
        assert!(snapshot.contains("Typed value: s"), "{snapshot}");
        assert!(
            snapshot.contains("Type change value: none; trusted: none"),
            "{snapshot}"
        );
        assert!(snapshot.contains("Submit effects: 0"), "{snapshot}");
    }

    #[test]
    fn cancel_during_final_type_character_reports_success_without_commit() {
        let shared = Arc::new(ActorShared::new());
        let type_id = request_id(61_251);
        let (reply, _response) = oneshot::channel();
        shared
            .submit(ActorRequest {
                request_id: type_id.clone(),
                op: BrowserOp::Type {
                    target: "e1".to_owned(),
                    text: "z".to_owned(),
                    submit: false,
                    slowly: true,
                    clear: true,
                },
                cancellation: Arc::new(CommandCancellation::new()),
                deadline: Instant::now() + Duration::from_secs(5),
                timeout_ms: 5_000,
                reply,
            })
            .expect("submit final-character type");
        let request = shared.next().expect("take final-character type");
        let mut state = BrowserState::default();
        state.current_refs.insert("e1".to_owned());
        let full_text_delivered = std::cell::Cell::new(false);
        let cancellation_won = std::cell::Cell::new(false);

        let result = state.dispatch_ref_action(
            "e1",
            |_| {
                full_text_delivered.set(true);
                Ok(())
            },
            |state| {
                assert!(
                    full_text_delivered.get(),
                    "post-action snapshot must follow the completed type"
                );
                cancellation_won.set(shared.cancel(&type_id, CancellationReason::Cancelled));
                state.committed_post_action_snapshot(&request)
            },
        );
        assert!(
            cancellation_won.get(),
            "the final typing character must not claim request-level commitment"
        );
        assert!(
            !request.cancellation.is_committed(),
            "typing must not set request-level physical commitment"
        );

        let completed = shared.complete(&request, result);
        assert!(
            completed.is_ok(),
            "a fully delivered type must not report cancellation: {completed:?}"
        );

        const TEST_MODULE: &str = "\n#[cfg(test)]\nmod tests {";
        let production = include_str!("actor.rs")
            .split_once(TEST_MODULE)
            .expect("actor.rs must have a test module")
            .0;
        let type_path = production
            .split_once("    fn type_text(")
            .expect("actor type path must exist")
            .1
            .split_once("    fn select_option(")
            .expect("actor type path must end before select_option")
            .0;
        // Password targets take a second post-action branch that resolves the
        // sensitive-node taint first, so both branches must stay committed.
        assert!(
            type_path.contains("state.committed_sensitive_post_action_snapshot(text, request)")
                && type_path.contains("state.committed_post_action_snapshot(request)"),
            "completed typing must use the completed-action snapshot path on both the \
             sensitive and the ordinary branch"
        );
        assert!(
            !type_path.contains("state.snapshot(request)"),
            "completed typing must not return to a cancellable snapshot"
        );
        assert!(
            type_path.contains("write_completed.set(true)")
                && type_path.contains("Err(error) if write_completed.get()")
                && type_path.contains("post_write_error.replace(Some(error))"),
            "an error after the text write must resolve through the committed \
             post-action path instead of discarding sensitive tracking"
        );
        let password_write_error_path = type_path
            .split_once("Err(error) if tracks_password")
            .expect("password helper-error path must exist")
            .1
            .split_once("Err(error) =>")
            .expect("password helper-error path must end before ordinary errors")
            .0;
        assert!(
            password_write_error_path
                .contains("state.resolve_sensitive_snapshot_tracking(text, request)")
                && password_write_error_path.contains("SensitiveWriteProgress::Complete")
                && password_write_error_path.contains("write_completed.set(true)")
                && password_write_error_path.contains("SensitiveWriteProgress::Partial")
                && !password_write_error_path.contains("discard_sensitive_snapshot_tracking"),
            "a password helper error must resolve live write progress before classifying \
             completion, and must never discard a possibly landed write"
        );
        let sensitive_snapshot_path = production
            .split_once("    fn committed_sensitive_post_action_snapshot(")
            .expect("sensitive committed snapshot path must exist")
            .1
            .split_once("\n    fn ")
            .expect("sensitive committed snapshot path must end before the next method")
            .0;
        assert!(
            sensitive_snapshot_path
                .contains("self.snapshot_with_sensitive_modal_redaction(sensitive_value, request)",)
                && sensitive_snapshot_path.contains("committed_snapshot_result("),
            "the sensitive snapshot path must commit the completed action while carrying \
             ephemeral modal redaction"
        );
        assert!(
            !sensitive_snapshot_path.contains("self.snapshot(request)"),
            "the sensitive snapshot path must not fall back to a cancellable snapshot"
        );
        let sensitive_resolver_path = production
            .split_once("    fn resolve_sensitive_snapshot_tracking(")
            .expect("sensitive resolver path must exist")
            .1
            .split_once("\n    fn ")
            .expect("sensitive resolver path must end before the next method")
            .0;
        assert!(
            sensitive_resolver_path.contains("let page = self.page.as_ref()")
                && sensitive_resolver_path.contains("ActionOptions::timeout(1_000.0)")
                && !sensitive_resolver_path.contains("Self::remaining(request)")
                && !sensitive_resolver_path.contains("self.ensure_page(request)"),
            "committed sensitive resolution must survive a later step exhausting \
             the request deadline"
        );

        let fill_form_path = production
            .split_once("    fn fill_form(")
            .expect("actor fill_form path must exist")
            .1
            .split_once("    fn hover(")
            .expect("actor fill_form path must end before hover")
            .0;
        assert!(
            fill_form_path.contains("self.committed_post_action_snapshot(request)")
                && fill_form_path.contains("partial_completion_result("),
            "fill_form must use committed snapshots for both complete and partial writes"
        );
        assert!(
            !fill_form_path.contains("self.snapshot(request)"),
            "fill_form must not use a cancellable snapshot after any writes"
        );
        let fill_helper_error_path = fill_form_path
            .split_once("if let Err(error) = result")
            .expect("fill_form helper-error path must exist")
            .1
            .split_once("\n            }\n            completed_fields.push(field.name.as_str());")
            .expect("fill_form helper-error path must precede normal completion")
            .0;
        assert!(
            fill_helper_error_path
                .contains("self.resolve_sensitive_snapshot_tracking(&field.value, request)")
                && fill_helper_error_path.contains("SensitiveWriteProgress::Complete")
                && fill_helper_error_path.contains("SensitiveWriteProgress::Partial")
                && !fill_helper_error_path.contains("discard_sensitive_snapshot_tracking"),
            "fill_form must resolve every possibly started password write before \
             reporting a field error"
        );

        let completion_path = production
            .split_once("    fn complete<T>(")
            .expect("actor completion path must exist")
            .1
            .split_once("\n    fn ")
            .expect("actor completion path must end before the next method")
            .0;
        assert!(
            completion_path.contains("BrowserOp::Type { .. } | BrowserOp::FillForm(_)",),
            "request deadline handling must preserve truthful successful results from \
             both text-write tools"
        );

        assert!(
            BEGIN_SENSITIVE_SNAPSHOT_TRACKING_JS
                .contains("tracking.sensitiveNodes instanceof WeakSet")
                && RESOLVE_SENSITIVE_SNAPSHOT_TRACKING_JS.contains("new WeakRef(node)"),
            "persistent sensitive-node tracking must not strongly retain tainted nodes"
        );
        assert!(
            BEGIN_SENSITIVE_SNAPSHOT_TRACKING_JS.contains("'id', 'aria-hidden', 'hidden', 'class', 'style'")
                && BEGIN_SENSITIVE_SNAPSHOT_TRACKING_JS.contains("visibilityBaseline")
                && RESOLVE_SENSITIVE_SNAPSHOT_TRACKING_JS
                    .contains("for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement)"),
            "sensitive resolution must cover renderer relationships, visibility transitions, \
             and aggregate-rendering ancestors"
        );
        assert!(
            SNAPSHOT_JS.contains("if (!node.isConnected) continue;")
                && SNAPSHOT_JS.contains(
                    "for (let ancestor = node; ancestor; ancestor = ancestor.parentElement)",
                )
                && !SNAPSHOT_JS.contains("el.contains(node)"),
            "each snapshot must precompute connected taints and ancestors once"
        );
    }

    #[test]
    fn fill_form_expired_budget_reports_the_deadline_before_the_reason_is_published() {
        // `remaining()` reads the clock on the worker thread; the `Deadline`
        // reason is published by the deadline task on the runtime thread, and
        // nothing orders the two. This request reproduces the window between
        // them exactly: the budget is already spent, but nobody has published a
        // reason, so `reason()` still says `Active`. The field error path must
        // still recognise the expiry, or the partial-fill detail is dropped in
        // precisely the case the deadline is what stopped the fill.
        let mut state = BrowserState::default();
        state.current_refs.insert("e1".to_owned());
        state.current_refs.insert("e2".to_owned());
        let fields = || {
            vec![
                FillField {
                    target: "e1".to_owned(),
                    name: "Field A".to_owned(),
                    kind: FillFieldKind::Textbox,
                    value: "never-written-a".to_owned(),
                },
                FillField {
                    target: "e2".to_owned(),
                    name: "Field B".to_owned(),
                    kind: FillFieldKind::Textbox,
                    value: "never-written-b".to_owned(),
                },
            ]
        };
        let (reply, _response) = oneshot::channel();
        let request = ActorRequest {
            request_id: request_id(69_106),
            op: BrowserOp::FillForm(fields()),
            cancellation: Arc::new(CommandCancellation::new()),
            deadline: Instant::now(),
            timeout_ms: 5_000,
            reply,
        };
        assert_eq!(
            request.cancellation.reason(),
            CancellationReason::Active,
            "the window under test is an expired budget with no reason published yet"
        );

        let error = state
            .fill_form(&fields(), &request)
            .expect_err("an expired budget must stop the fill");
        let message = error.to_string();
        assert!(
            message.contains("stopped by timeout while processing field \"Field A\""),
            "an expired budget must be reported as the timeout it is: {message}"
        );
        assert!(
            message.contains("fields confirmed complete before it: none"),
            "the partial-fill detail must survive the deadline race: {message}"
        );
        assert!(
            !message.contains("Field \"Field A\" failed:"),
            "a spent budget is not a field-specific failure: {message}"
        );
        assert_eq!(
            request.cancellation.detail(),
            Some(message),
            "the detail must be published so `complete` can substitute it"
        );
    }

    #[test]
    fn complete_keeps_a_fully_written_nonphysical_form_that_a_late_cancel_raced() {
        // A form of textboxes and comboboxes commits no physical action, so
        // `is_committed()` stays false for the whole request. The closing
        // snapshot is a real round trip, so a cancel can land after every field
        // is written but before the result is handed back. The form is written;
        // saying it was cancelled would send the caller to reconcile a form
        // that needs no reconciling.
        let shared = Arc::new(ActorShared::new());
        let fill_id = request_id(69_107);
        let (reply, _response) = oneshot::channel();
        let fields = vec![FillField {
            target: "e1".to_owned(),
            name: "Field A".to_owned(),
            kind: FillFieldKind::Textbox,
            value: "written-a".to_owned(),
        }];
        shared
            .submit(ActorRequest {
                request_id: fill_id.clone(),
                op: BrowserOp::FillForm(fields),
                cancellation: Arc::new(CommandCancellation::new()),
                deadline: Instant::now() + Duration::from_secs(5),
                timeout_ms: 5_000,
                reply,
            })
            .expect("submit nonphysical fill");
        let request = shared.next().expect("take nonphysical fill");

        let result: TextResult = Ok("- page snapshot".to_owned());
        assert!(
            shared.cancel(&fill_id, CancellationReason::Cancelled),
            "a form of textboxes must not claim request-level commitment"
        );
        assert!(
            !request.cancellation.is_committed(),
            "filling textboxes must not set request-level physical commitment"
        );

        let completed = shared.complete(&request, result);
        assert!(
            completed.is_ok(),
            "a fully written form must not report cancellation: {completed:?}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn cancel_during_append_type_stops_between_characters() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(61_300),
                BrowserOp::Navigate(server.url("/input")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to input fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Type target");

        let type_id = request_id(61_301);
        let type_actor = Arc::clone(&actor);
        let type_request_id = type_id.clone();
        let typed = tokio::spawn(async move {
            type_actor
                .execute_with_timeout(
                    type_request_id,
                    BrowserOp::Type {
                        target,
                        text: "append".to_owned(),
                        submit: false,
                        slowly: false,
                        clear: false,
                    },
                    Duration::from_secs(5),
                )
                .await
        });
        wait_until_in_flight(&actor, &type_id).await;
        assert_eq!(server.capture(), "input-keydown");
        assert!(
            actor.cancel(&type_id),
            "append typing must remain request-cancellable"
        );
        assert_eq!(
            typed.await.expect("join cancelled append type"),
            Err(BrowserError::Cancelled)
        );

        let snapshot = actor
            .execute_with_timeout(
                request_id(61_302),
                BrowserOp::Snapshot {
                    target: None,
                    depth: None,
                    boxes: false,
                },
                Duration::from_secs(5),
            )
            .await
            .expect("snapshot after cancelled append type");
        let snapshot = output_text(&snapshot);
        // Cancellation between characters must leave exactly the one key
        // effect that had already dispatched; caret placement after focus is
        // engine behavior this test does not pin down.
        assert!(
            snapshot.contains("Key pressed: a; trusted: true; state: up; effects: 1"),
            "{snapshot}"
        );
        assert!(!snapshot.contains(r#"[value="old value"]"#), "{snapshot}");
        assert!(snapshot.contains("Submit effects: 0"), "{snapshot}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_tool_press_key_targets_ref_and_snapshot_observes_trusted_listener() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(62_000),
                BrowserOp::Navigate(server.url("/input")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to input fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Type target");

        let pressed = actor
            .execute_with_timeout(
                request_id(62_001),
                BrowserOp::PressKey {
                    target: Some(target),
                    key: "Enter".to_owned(),
                },
                Duration::from_secs(5),
            )
            .await
            .expect("press key on snapshot ref");
        let pressed = output_text(&pressed);
        assert!(
            pressed.contains("Key pressed: Enter; trusted: true"),
            "{pressed}"
        );
        assert!(pressed.contains("state: up; effects: 1"), "{pressed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn cancel_after_committed_key_press_reports_success_and_releases_key() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(62_100),
                BrowserOp::Navigate(server.url("/input")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to input fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Type target");

        let press_id = request_id(62_101);
        let press_actor = Arc::clone(&actor);
        let press_request_id = press_id.clone();
        let press = tokio::spawn(async move {
            press_actor
                .execute_with_timeout(
                    press_request_id,
                    BrowserOp::PressKey {
                        target: Some(target),
                        key: "Enter".to_owned(),
                    },
                    Duration::from_secs(5),
                )
                .await
        });
        wait_until_in_flight(&actor, &press_id).await;
        assert_eq!(server.capture(), "input-keydown");
        assert!(
            !actor.cancel(&press_id),
            "cancellation after key-down must be a no-op-too-late"
        );

        let pressed = press
            .await
            .expect("join committed actor key press")
            .expect("a committed key press must not report cancellation");
        let pressed = output_text(&pressed);
        assert!(pressed.contains("state: up; effects: 1"), "{pressed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn cancel_after_committed_type_submit_reports_success_once() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(62_200),
                BrowserOp::Navigate(server.url("/input")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to input fixture");
        let target = snapshot_ref(output_text(&snapshot), "textbox", "Type target");

        let type_id = request_id(62_201);
        let type_actor = Arc::clone(&actor);
        let type_request_id = type_id.clone();
        let typed = tokio::spawn(async move {
            type_actor
                .execute_with_timeout(
                    type_request_id,
                    BrowserOp::Type {
                        target,
                        text: "submit once".to_owned(),
                        submit: true,
                        slowly: false,
                        clear: true,
                    },
                    Duration::from_secs(5),
                )
                .await
        });
        wait_until_in_flight(&actor, &type_id).await;
        assert_eq!(server.capture(), "input-keydown");
        assert!(
            !actor.cancel(&type_id),
            "cancellation after submit key-down must be a no-op-too-late"
        );

        let typed = typed
            .await
            .expect("join committed actor type submit")
            .expect("a committed type submit must not report cancellation");
        let typed = output_text(&typed);
        assert!(typed.contains(r#"[value="submit once"]"#), "{typed}");
        assert!(typed.contains("state: up; effects: 1"), "{typed}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_tool_hover_snapshot_observes_mouseover_listener() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(63_000),
                BrowserOp::Navigate(server.url("/input")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to input fixture");
        let target = snapshot_ref(output_text(&snapshot), "button", "Hover target");

        let hovered = actor
            .execute_with_timeout(
                request_id(63_001),
                BrowserOp::Hover(target),
                Duration::from_secs(5),
            )
            .await
            .expect("hover snapshot ref");
        let hovered = output_text(&hovered);
        assert!(
            hovered.contains("Hover observed: true; trusted: true"),
            "{hovered}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_tool_select_option_matches_visible_label() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(64_000),
                BrowserOp::Navigate(server.url("/input")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to input fixture");
        let target = snapshot_ref(output_text(&snapshot), "combobox", "Select target");

        let selected = actor
            .execute_with_timeout(
                request_id(64_001),
                BrowserOp::SelectOption {
                    target,
                    values: vec!["Beta".to_owned()],
                },
                Duration::from_secs(5),
            )
            .await
            .expect("select option by snapshot ref");
        let selected = output_text(&selected);
        assert!(
            selected.contains("Selected value: beta; changes: 1"),
            "{selected}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_tool_single_select_ambiguity_uses_first_dom_value_or_label_match() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(64_100),
                BrowserOp::Navigate(server.url("/input")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to input fixture");
        let target = snapshot_ref(
            output_text(&snapshot),
            "combobox",
            "Ambiguous select target",
        );

        let selected = actor
            .execute_with_timeout(
                request_id(64_101),
                BrowserOp::SelectOption {
                    target,
                    values: vec!["X".to_owned()],
                },
                Duration::from_secs(5),
            )
            .await
            .expect("select ambiguous single option");
        let selected = output_text(&selected);
        assert!(
            selected.contains("Ambiguous selected value: other; changes: 1"),
            "{selected}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_tool_multiple_select_ambiguity_selects_all_dom_value_or_label_matches() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(64_200),
                BrowserOp::Navigate(server.url("/input")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to input fixture");
        let target = snapshot_ref(
            output_text(&snapshot),
            "combobox",
            "Ambiguous multi select target",
        );

        let selected = actor
            .execute_with_timeout(
                request_id(64_201),
                BrowserOp::SelectOption {
                    target,
                    values: vec!["X".to_owned()],
                },
                Duration::from_secs(5),
            )
            .await
            .expect("select ambiguous multiple options");
        let selected = output_text(&selected);
        assert!(
            selected.contains("Ambiguous selected values: other,X; changes: 1"),
            "{selected}"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_tool_empty_values_clear_multi_select() {
        let _guard = browser_test_lock().lock().await;
        let Some(actor) = actor().await else {
            return;
        };
        let server = ActionFixtureServer::start();
        let snapshot = actor
            .execute_with_timeout(
                request_id(64_300),
                BrowserOp::Navigate(server.url("/input")),
                Duration::from_secs(10),
            )
            .await
            .expect("navigate actor to input fixture");
        let target = snapshot_ref(output_text(&snapshot), "combobox", "Multi select target");

        let selected = actor
            .execute_with_timeout(
                request_id(64_301),
                BrowserOp::SelectOption {
                    target,
                    values: Vec::new(),
                },
                Duration::from_secs(5),
            )
            .await
            .expect("clear multi-select by snapshot ref");
        let selected = output_text(&selected);
        assert!(
            selected.contains("Selected values: none; changes: 1"),
            "{selected}"
        );
    }

    #[test]
    fn input_tool_type_rejects_unknown_and_stale_refs() {
        let mut state = BrowserState::default();
        state.current_refs.insert("e1".to_owned());
        let (reply, _response) = oneshot::channel();
        let request = ActorRequest {
            request_id: request_id(65_000),
            op: BrowserOp::Type {
                target: "e1".to_owned(),
                text: "not typed".to_owned(),
                submit: false,
                slowly: false,
                clear: true,
            },
            cancellation: Arc::new(CommandCancellation::new()),
            deadline: Instant::now() + Duration::from_secs(5),
            timeout_ms: 5_000,
            reply,
        };

        let unknown = state.type_text("e999999", "not typed", false, false, true, &request);
        assert!(matches!(
            unknown,
            Err(BrowserError::Message(message))
                if message.contains("unknown or stale ref e999999")
        ));

        state.current_refs = HashSet::from(["e2".to_owned()]);
        let stale = state.type_text("e1", "not typed", false, false, true, &request);
        assert!(matches!(
            stale,
            Err(BrowserError::Message(message))
                if message.contains("unknown or stale ref e1")
        ));
    }

    #[tokio::test(flavor = "current_thread")]
    async fn physical_click_succeeds_when_background_page_pauses_animation_frames() {
        let _guard = browser_test_lock().lock().await;
        if chromium().executable_path().is_none() {
            eprintln!("skipping background physical click test: Chromium executable unavailable");
            return;
        }
        tokio::task::spawn_blocking(|| {
            let server = ActionFixtureServer::start();
            let mut launch_options = LaunchOptions::default().arg("--no-proxy-server");
            launch_options.ignore_default_args.extend([
                "--disable-background-timer-throttling".to_owned(),
                "--disable-renderer-backgrounding".to_owned(),
            ]);
            let owner = chromium()
                .launch(launch_options)
                .expect("launch background physical click browser");
            let owner_page = owner
                .new_page()
                .expect("create background physical click page");
            owner_page
                .goto(
                &server.url("/atomic-click"),
                GotoOptions::default().wait_until("load").timeout(10_000.0),
            )
            .expect("navigate background physical click fixture");
            assert_eq!(server.capture(), "atomic-ready");

            let window_id = minimize_test_page(&owner, &owner_page);
            let proxy = InputRestoringCdpProxy::start(&owner.ws_endpoint(), window_id);
            let browser = chromium()
                .connect_over_cdp(ConnectOptions::new(proxy.endpoint()).timeout(Duration::from_secs(10)))
                .expect("connect physical click through input-observing test proxy");
            let page = browser
                .pages()
                .expect("list remotely attached physical click pages")
                .into_iter()
                .find(|candidate| candidate.target_id() == owner_page.target_id())
                .expect("find remotely attached physical click page");
            assert_eq!(
                page.evaluate(
                    r#"
globalThis.backgroundScheduling = { animationFrameRan: false, timerRan: false };
requestAnimationFrame(() => { backgroundScheduling.animationFrameRan = true; });
setTimeout(() => { backgroundScheduling.timerRan = true; }, 0);
document.visibilityState
"#,
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("start background scheduling proof"),
                json!("hidden")
            );

            let scheduling_deadline = Instant::now() + Duration::from_secs(10);
            loop {
                let scheduling = page
                    .evaluate(
                        "({ visibility: document.visibilityState, ...backgroundScheduling })",
                        None,
                        ActionOptions::timeout(1_000.0),
                    )
                    .expect("read background scheduling proof");
                if scheduling["timerRan"] == Value::Bool(true) {
                    break;
                }
                assert!(
                    Instant::now() < scheduling_deadline,
                    "background timer did not run within the bounded proof wait: {scheduling}"
                );
                thread::sleep(Duration::from_millis(25));
            }
            thread::sleep(Duration::from_millis(400));
            let scheduling = page
                .evaluate(
                    "({ visibility: document.visibilityState, ...backgroundScheduling })",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("finish bounded background scheduling proof");
            assert_eq!(scheduling["visibility"], json!("hidden"));
            assert_eq!(scheduling["animationFrameRan"], Value::Bool(false));

            let click_started = Instant::now();
            // Generous ceiling so a slow CI runner can't time out a click that DOES
            // complete; without the stability-probe fallback the click still hangs to
            // this deadline and fails, which is what proves the fix.
            page.click("#background", ActionOptions::timeout(15_000.0))
                .expect("physical click must complete after hidden-page actionability");
            let click_elapsed = click_started.elapsed();
            let evidence = page
                .evaluate(
                    "({ visibility: document.visibilityState, animationFrameRan: backgroundScheduling.animationFrameRan, events: globalThis.backgroundEvents, effectCount: globalThis.backgroundEffectCount, text: document.querySelector('#background').textContent })",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("read background physical click evidence");
            assert!(
                click_elapsed < Duration::from_millis(12_000),
                "background physical click stalled for {click_elapsed:?}"
            );
            assert!(proxy.restored(), "physical input should trigger window restoration");
            assert_eq!(evidence["visibility"], json!("visible"));
            let events = evidence["events"].as_array().expect("physical click events");
            assert_eq!(
                events
                    .iter()
                    .map(|event| event["type"].as_str().expect("physical event type"))
                    .collect::<Vec<_>>(),
                ["mousedown", "mouseup", "click"]
            );
            assert!(
                events
                    .iter()
                    .all(|event| event["trusted"] == Value::Bool(true))
            );
            assert_eq!(evidence["effectCount"], json!(1));
            assert_eq!(evidence["text"], json!("Background click effect 1"));
            println!(
                "background physical click: precondition=400ms actionability_visibility=hidden timer=true rAF=false click={click_elapsed:?} trusted_events=3"
            );
            drop(page);
            drop(browser);
            drop(proxy);
            owner
                .close()
                .expect("close background physical click browser");
        })
        .await
        .expect("join background physical click test");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn physical_click_is_trusted_ordered_scrolls_and_reaches_forced_oopif() {
        let _guard = browser_test_lock().lock().await;
        if chromium().executable_path().is_none() {
            eprintln!("skipping physical proof test: Chromium executable unavailable");
            return;
        }
        tokio::task::spawn_blocking(|| {
            let server = ActionFixtureServer::start();
            let browser = chromium()
                .launch(
                    LaunchOptions::default()
                        .arg("--site-per-process")
                        .arg("--no-proxy-server"),
                )
                .expect("launch physical proof browser");
            let page = browser.new_page().expect("create physical proof page");
            page.goto(
                &server.url("/physical"),
                GotoOptions::default().wait_until("load").timeout(10_000.0),
            )
            .expect("navigate physical proof fixture");
            assert_eq!(server.capture(), "physical-ready");
            let fixture_state = page
                .evaluate(
                    "({ href: location.href, physical: !!document.querySelector('#physical') })",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("inspect physical fixture");
            assert_eq!(
                fixture_state["physical"],
                Value::Bool(true),
                "{fixture_state}"
            );
            page.click("#physical", ActionOptions::timeout(3_000.0))
                .expect("click off-screen physical target");
            let evidence = page
                .evaluate(
                    "({ events: globalThis.physicalEvents, scrollY })",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("read main-frame physical evidence");
            let events = evidence["events"].as_array().expect("main physical events");
            assert_eq!(
                events
                    .iter()
                    .map(|event| event["type"].as_str().expect("main event type"))
                    .collect::<Vec<_>>(),
                ["mousedown", "mouseup", "click"]
            );
            assert!(events.iter().all(|event| {
                event["trusted"] == Value::Bool(true)
                    && event["button"] == 0
                    && event["detail"] == 1
            }));
            assert!(evidence["scrollY"].as_f64().unwrap_or_default() > 0.0);

            page.evaluate(
                "globalThis.physicalEvents = []",
                None,
                ActionOptions::timeout(1_000.0),
            )
            .expect("clear main-frame physical evidence");
            page.dblclick("#physical", ActionOptions::timeout(3_000.0))
                .expect("physically double-click target");
            let double_events = page
                .evaluate(
                    "globalThis.physicalEvents",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("read double-click evidence");
            let double_events = double_events.as_array().expect("double-click events");
            assert_eq!(
                double_events
                    .iter()
                    .map(|event| event["type"].as_str().expect("double-click event type"))
                    .collect::<Vec<_>>(),
                [
                    "mousedown",
                    "mouseup",
                    "click",
                    "mousedown",
                    "mouseup",
                    "click",
                    "dblclick",
                ]
            );
            assert!(
                double_events
                    .iter()
                    .all(|event| event["trusted"] == Value::Bool(true))
            );

            page.hover_with_options("#hover-target", ActionOptions::timeout(3_000.0))
                .expect("physically hover disabled target");
            let hover_events = page
                .evaluate(
                    "globalThis.hoverEvents",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("read hover evidence");
            assert_eq!(
                hover_events,
                json!([{ "type": "mouseover", "trusted": true }])
            );

            page.check_with_cancel("#check-target", ActionOptions::timeout(3_000.0), None)
                .expect("physically check target");
            page.uncheck_with_cancel("#check-target", ActionOptions::timeout(3_000.0), None)
                .expect("physically uncheck target");
            let check_events = page
                .evaluate(
                    "globalThis.checkEvents",
                    None,
                    ActionOptions::timeout(1_000.0),
                )
                .expect("read checked-action evidence");
            let check_events = check_events.as_array().expect("checked-action events");
            assert_eq!(
                check_events
                    .iter()
                    .map(|event| event["type"].as_str().expect("checked-action event type"))
                    .collect::<Vec<_>>(),
                [
                    "mousedown",
                    "mouseup",
                    "click",
                    "mousedown",
                    "mouseup",
                    "click",
                ]
            );
            assert!(
                check_events
                    .iter()
                    .all(|event| event["trusted"] == Value::Bool(true))
            );
            assert_eq!(check_events[2]["checked"], Value::Bool(true));
            assert_eq!(check_events[5]["checked"], Value::Bool(false));

            // The click budget is 3s, so the probe must fire inside 2s.
            server.arm("/arrived", Duration::from_secs(2));
            page.click("#navigate", ActionOptions::timeout(3_000.0))
                .expect("click navigation link");
            assert_eq!(
                page.title(ActionOptions::timeout(1_000.0))
                    .expect("read post-click title"),
                "arrived"
            );

            page.goto(
                &server.url("/oopif-top"),
                GotoOptions::default().wait_until("load").timeout(10_000.0),
            )
            .expect("navigate isolated frame fixture");
            assert_eq!(server.capture(), "oopif-ready");
            page.click_in_frame("#child", "#frame-button", ActionOptions::timeout(5_000.0))
                .expect("click isolated frame target");
            assert_eq!(server.capture(), "mousedown:true,mouseup:true,click:true");

            browser.close().expect("close physical proof browser");
        })
        .await
        .expect("join physical proof test");
    }
}
