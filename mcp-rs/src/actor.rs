use std::{
    collections::{HashSet, VecDeque},
    env, fmt,
    sync::{
        Arc, Condvar, Mutex, Weak,
        atomic::{AtomicU8, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

use rmcp::model::RequestId;
use rustwright::{
    ActionOptions, Browser, CancelToken, ConnectOptions, Error, GotoOptions, LaunchOptions, Page,
    ScreenshotOptions, chromium,
};
use serde_json::{Value, json};
use tokio::sync::oneshot;

const SNAPSHOT_JS: &str = include_str!("snapshot.js");
const REMOTE_UNREACHABLE: &str = "remote CDP session unreachable — restart or reconfigure";
const DEFAULT_CDP_TIMEOUT_MS: u64 = 60_000;
const DEFAULT_TOOL_TIMEOUT_MS: u64 = 60_000;
const MIN_TOOL_TIMEOUT_MS: u64 = 1_000;
const MAX_TOOL_TIMEOUT_MS: u64 = 600_000;
const ENGINE_TIMEOUT_CUSHION: Duration = Duration::from_secs(1);
pub(crate) const COMMAND_QUEUE_CAPACITY: usize = 64;

#[derive(Debug)]
pub(crate) enum BrowserOp {
    Navigate(String),
    NavigateBack,
    NavigateForward,
    Snapshot,
    Click(String),
    ScrollTarget(String),
    ScrollViewport(f64),
    TakeScreenshot,
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum BrowserOutput {
    Text(String),
    Png(Vec<u8>),
}

impl From<String> for BrowserOutput {
    fn from(text: String) -> Self {
        Self::Text(text)
    }
}

impl PartialEq<String> for BrowserOutput {
    fn eq(&self, other: &String) -> bool {
        matches!(self, Self::Text(text) if text == other)
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
}

impl CommandCancellation {
    fn new() -> Self {
        Self {
            reason: AtomicU8::new(CancellationReason::Active as u8),
            engine: CancelToken::new(),
        }
    }

    fn cancel(&self, reason: CancellationReason) -> bool {
        if self.reason() != CancellationReason::Active
            || self.engine.is_physical_action_committed()
            || !self.engine.try_cancel()
        {
            return false;
        }
        self.reason
            .compare_exchange(
                CancellationReason::Active as u8,
                reason as u8,
                Ordering::SeqCst,
                Ordering::SeqCst,
            )
            .is_ok()
    }

    fn reason(&self) -> CancellationReason {
        CancellationReason::from_u8(self.reason.load(Ordering::SeqCst))
    }

    fn is_committed(&self) -> bool {
        self.engine.is_physical_action_committed()
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
        if request.cancellation.is_committed() {
            result
        } else {
            request
                .cancellation
                .reason()
                .error(request.timeout_ms)
                .map_or(result, Err)
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
                let _ = in_flight
                    .cancellation
                    .cancel(CancellationReason::Cancelled);
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
    pub(crate) fn spawn() -> Self {
        Self::spawn_with_startup(BrowserStartup::from_env())
    }

    fn spawn_with_startup(startup: BrowserStartup) -> Self {
        let shared = Arc::new(ActorShared::new());
        let actor_shared = Arc::clone(&shared);
        let thread = thread::Builder::new()
            .name("mcp-browser-actor".to_owned())
            .spawn(move || actor_main(actor_shared, startup))
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

#[derive(Default)]
struct BrowserState {
    browser: Option<Browser>,
    page: Option<Page>,
    remote: bool,
    remote_options: Option<ConnectOptions>,
    startup_error: Option<&'static str>,
    next_ref: u64,
    current_refs: HashSet<String>,
}

impl BrowserState {
    fn new(startup: BrowserStartup) -> Self {
        let mut state = Self::default();
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
        self.page = Some(page);
        self.browser = Some(browser);
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

    fn ensure_page(&mut self, request: &ActorRequest) -> Result<&Page, BrowserError> {
        if !request.cancellation.is_committed()
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
            let created = self
                .browser
                .as_ref()
                .expect("browser was initialized")
                .new_page_with_cancel(Some(&request.cancellation.engine));
            self.page = Some(created.map_err(|error| {
                self.operation_error(
                    "new page failed",
                    error,
                    &request.cancellation,
                    request.timeout_ms,
                )
            })?);
        }
        Ok(self.page.as_ref().expect("page was initialized"))
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
        self.current_refs.clear();
        let remaining = Self::remaining(request)?;
        let result = self.ensure_page(request)?.goto_with_cancel(
            url,
            GotoOptions::default()
                .wait_until("load")
                .timeout(Self::engine_timeout(remaining)),
            Some(&request.cancellation.engine),
        );
        result.map_err(|error| {
            self.operation_error(
                "navigation failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            )
        })?;
        self.snapshot(request)
    }

    fn navigate_back(&mut self, request: &ActorRequest) -> TextResult {
        self.current_refs.clear();
        let remaining = Self::remaining(request)?;
        let result = self.ensure_page(request)?.go_back_with_cancel_status(
            GotoOptions::default()
                .wait_until("load")
                .timeout(Self::engine_timeout(remaining)),
            Some(&request.cancellation.engine),
        );
        let (had_entry, _response) = result.map_err(|error| {
            self.operation_error(
                "back navigation failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            )
        })?;
        if !had_entry {
            return Err(BrowserError::Message("no back history".to_owned()));
        }
        self.snapshot(request)
    }

    fn navigate_forward(&mut self, request: &ActorRequest) -> TextResult {
        self.current_refs.clear();
        let remaining = Self::remaining(request)?;
        let result = self.ensure_page(request)?.go_forward_with_cancel_status(
            GotoOptions::default()
                .wait_until("load")
                .timeout(Self::engine_timeout(remaining)),
            Some(&request.cancellation.engine),
        );
        let (had_entry, _response) = result.map_err(|error| {
            self.operation_error(
                "forward navigation failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            )
        })?;
        if !had_entry {
            return Err(BrowserError::Message("no forward history".to_owned()));
        }
        self.snapshot(request)
    }

    fn snapshot(&mut self, request: &ActorRequest) -> TextResult {
        self.snapshot_with_cancel(request, Some(&request.cancellation.engine))
    }

    fn snapshot_with_cancel(
        &mut self,
        request: &ActorRequest,
        cancel: Option<&CancelToken>,
    ) -> TextResult {
        let start_ref = self.next_ref.max(1);
        let remaining = Self::remaining(request)?;
        let result = self.ensure_page(request)?.evaluate_with_cancel(
            SNAPSHOT_JS,
            Some(&json!(start_ref)),
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
        let outline = value
            .get("outline")
            .and_then(Value::as_str)
            .ok_or_else(|| BrowserError::Message(format!("snapshot returned no outline: {value}")))?
            .to_owned();
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
        self.current_refs = (start_ref..next_ref).map(|n| format!("e{n}")).collect();
        self.next_ref = next_ref;
        Ok(outline)
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

    fn click(&mut self, target: &str, request: &ActorRequest) -> TextResult {
        let selector = format!(r#"[data-mcp-ref="{target}"]"#);
        self.dispatch_ref_action(
            target,
            |state| {
                let remaining = Self::remaining(request)?;
                let result = state.ensure_page(request)?.click_with_cancel(
                    &selector,
                    ActionOptions::timeout(Self::engine_timeout(remaining)),
                    Some(&request.cancellation.engine),
                );
                result.map_err(|error| {
                    state.operation_error(
                        &format!("click failed for {target}"),
                        error,
                        &request.cancellation,
                        request.timeout_ms,
                    )
                })?;
                Ok(())
            },
            // The physical click has committed, so cancellation is too late:
            // finish the post-click snapshot and preserve its owned result.
            |state| state.snapshot_with_cancel(request, None),
        )
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

    fn take_screenshot(&mut self, request: &ActorRequest) -> Result<Vec<u8>, BrowserError> {
        let remaining = Self::remaining(request)?;
        let result = self.ensure_page(request)?.screenshot_with_cancel(
            ScreenshotOptions {
                timeout: Some(Self::engine_timeout(remaining)),
                image_type: Some("png".to_owned()),
                ..ScreenshotOptions::default()
            },
            Some(&request.cancellation.engine),
        );
        result.map_err(|error| {
            self.operation_error(
                "screenshot failed",
                error,
                &request.cancellation,
                request.timeout_ms,
            )
        })
    }

    fn run(&mut self, request: &ActorRequest) -> BrowserResult {
        match &request.op {
            BrowserOp::Navigate(url) => self.navigate(url, request).map(BrowserOutput::Text),
            BrowserOp::NavigateBack => self.navigate_back(request).map(BrowserOutput::Text),
            BrowserOp::NavigateForward => self.navigate_forward(request).map(BrowserOutput::Text),
            BrowserOp::Snapshot => self.snapshot(request).map(BrowserOutput::Text),
            BrowserOp::Click(target) => self.click(target, request).map(BrowserOutput::Text),
            BrowserOp::ScrollTarget(target) => {
                self.scroll_target(target, request).map(BrowserOutput::Text)
            }
            BrowserOp::ScrollViewport(delta_y) => self
                .scroll_viewport(*delta_y, request)
                .map(BrowserOutput::Text),
            BrowserOp::TakeScreenshot => self.take_screenshot(request).map(BrowserOutput::Png),
        }
    }

    fn close(&mut self) {
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

fn actor_main(shared: Arc<ActorShared>, startup: BrowserStartup) {
    let mut state = BrowserState::new(startup);
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
        sync::{OnceLock, atomic::AtomicBool, mpsc},
    };

    use super::*;
    use rustwright::ActionabilityError;

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

    fn output_text(output: &BrowserOutput) -> &str {
        match output {
            BrowserOutput::Text(text) => text,
            BrowserOutput::Png(bytes) => {
                panic!("expected a text output, got {} PNG bytes", bytes.len())
            }
        }
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
            .execute_with_timeout(request_id(0), BrowserOp::Snapshot, Duration::from_secs(30))
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
                .execute_with_timeout(request_id(2), BrowserOp::Snapshot, Duration::from_secs(30))
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
            .execute_with_timeout(request_id(11), BrowserOp::Snapshot, Duration::from_secs(5))
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
                Duration::from_millis(100),
            )
            .await;
        assert_eq!(result, Err(BrowserError::Timeout(100)));
        assert!(started.elapsed() < Duration::from_secs(1));
        actor
            .execute_with_timeout(request_id(21), BrowserOp::Snapshot, Duration::from_secs(5))
            .await
            .expect("snapshot should succeed after deadline");
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
            .execute_with_timeout(request_id(22), BrowserOp::Snapshot, Duration::from_secs(1))
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
            .execute_with_timeout(request_id(23), BrowserOp::Snapshot, Duration::from_secs(10))
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
                BrowserOp::Snapshot,
                Duration::from_millis(100),
            )
            .await;
        assert_eq!(result, Err(BrowserError::Timeout(100)));
        actor
            .execute_with_timeout(request_id(26), BrowserOp::Snapshot, Duration::from_secs(30))
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
                        BrowserOp::Snapshot,
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
                    BrowserOp::Snapshot,
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
            .execute_with_timeout(request_id(40), BrowserOp::Snapshot, Duration::from_secs(5))
            .await
            .expect("completed snapshot");
        assert!(!actor.cancel(&request_id(40)));
        actor
            .execute_with_timeout(request_id(41), BrowserOp::Snapshot, Duration::from_secs(5))
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
                    BrowserOp::Snapshot,
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
                    BrowserOp::Snapshot,
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
                    op: BrowserOp::Snapshot,
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
                            BrowserOp::Snapshot,
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
                BrowserOp::Snapshot,
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
                        BrowserOp::Snapshot,
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
                    BrowserOp::Snapshot,
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
                BrowserOp::Snapshot,
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
    }

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
            let thread = thread::spawn(move || {
                let mut handlers = Vec::new();
                while !thread_stop.load(Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((mut stream, _)) => {
                            let captures = captures_tx.clone();
                            handlers.push(thread::spawn(move || {
                                serve_action_fixture(&mut stream, addr.port(), &captures)
                            }));
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::yield_now();
                        }
                        Err(error) => panic!("action fixture accept failed: {error}"),
                    }
                }
                for handler in handlers {
                    handler.join().expect("join action fixture connection");
                }
            });
            Self {
                addr,
                captures,
                stop,
                thread: Some(thread),
            }
        }

        fn url(&self, path: &str) -> String {
            format!("http://{}{path}", self.addr)
        }

        fn capture(&self) -> String {
            self.captures
                .recv_timeout(Duration::from_secs(5))
                .expect("receive action fixture capture")
        }
    }

    impl Drop for ActionFixtureServer {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::Relaxed);
            let _ = TcpStream::connect(self.addr);
            if let Some(thread) = self.thread.take() {
                thread.join().expect("join action fixture");
            }
        }
    }

    fn serve_action_fixture(stream: &mut TcpStream, port: u16, captures: &mpsc::Sender<String>) {
        stream
            .set_nonblocking(false)
            .expect("set action fixture connection blocking");
        stream
            .set_read_timeout(Some(Duration::from_secs(30)))
            .expect("set action fixture read timeout");
        let Ok(request) = read_http_headers(stream) else {
            return;
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
  #detach { display: block; margin-top: 1800px; width: 140px; height: 44px; }
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
  new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) detached.remove();
  }).observe(detached);
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

            page.click(
                "#partially-offscreen",
                ActionOptions::timeout(1_000.0),
            )
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
            assert!(partially_offscreen
                .iter()
                .all(|event| event["trusted"] == Value::Bool(true)
                    && event["clientX"] == json!(0)));

            assert_actionability(
                page.click("#detach", ActionOptions::timeout(500.0))
                    .expect_err("detached target must not click"),
                ActionabilityError::Detached,
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
            let click = thread::spawn(move || {
                click_page.click_with_cancel(
                    "#cancel",
                    ActionOptions::timeout(10_000.0),
                    Some(&click_cancel),
                )
            });
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
            let click = thread::spawn(move || {
                click_page.click_with_cancel(
                    "#atomic",
                    ActionOptions::timeout(5_000.0),
                    Some(&click_cancel),
                )
            });
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

            page.click("#following", ActionOptions::timeout(1_000.0))
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
                    BrowserOp::Click("e1".to_owned()),
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
        assert!(!actor.cancel(&click_id));
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
