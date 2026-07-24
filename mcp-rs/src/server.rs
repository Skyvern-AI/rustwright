use std::{
    env, fs,
    io::{self, Write},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};

use base64::{Engine as _, encoded_len, engine::general_purpose::STANDARD};
use rmcp::{
    ErrorData, ServerHandler,
    model::{
        CallToolRequestParams, CallToolResult, CancelledNotificationParam, ContentBlock,
        Implementation, ListToolsResult, PaginatedRequestParams, ServerCapabilities, ServerInfo,
    },
    service::{NotificationContext, RequestContext, RoleServer},
};

use crate::{
    actor::{BrowserActor, BrowserError, BrowserOutput},
    tools::{TOOL_SPECS, descriptor, find_tool, parse_op},
};

const DEFAULT_SCREENSHOT_MAX_BYTES: usize = 5 * 1024 * 1024;
const MAX_SCREENSHOT_MAX_BYTES: usize = 64 * 1024 * 1024;
static NEXT_SCREENSHOT_DIR: AtomicU64 = AtomicU64::new(1);
static NEXT_SCREENSHOT_FILE: AtomicU64 = AtomicU64::new(1);

pub(crate) struct BrowserServer {
    actor: Arc<BrowserActor>,
    screenshot_max_bytes: usize,
    screenshot_temp_dir: ScreenshotTempDir,
}

impl BrowserServer {
    pub(crate) fn new() -> io::Result<Self> {
        let screenshot_temp_dir = ScreenshotTempDir::new()?;
        Ok(Self {
            actor: Arc::new(BrowserActor::spawn()),
            screenshot_max_bytes: screenshot_max_bytes_from_env(),
            screenshot_temp_dir,
        })
    }
}

struct ScreenshotTempDir {
    path: PathBuf,
}

impl ScreenshotTempDir {
    fn new() -> io::Result<Self> {
        #[cfg(unix)]
        use std::os::unix::fs::DirBuilderExt;

        let mut temp_dir = env::temp_dir();
        if !temp_dir.is_absolute() {
            temp_dir = env::current_dir()?.join(temp_dir);
        }
        for _ in 0..100 {
            let sequence = NEXT_SCREENSHOT_DIR.fetch_add(1, Ordering::Relaxed);
            let path = temp_dir.join(format!(
                "rustwright-mcp-screenshots-{}-{sequence}",
                std::process::id()
            ));
            let mut builder = fs::DirBuilder::new();
            #[cfg(unix)]
            builder.mode(0o700);
            match builder.create(&path) {
                Ok(()) => return Ok(Self { path }),
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
        Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "could not create a unique screenshot temp directory",
        ))
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for ScreenshotTempDir {
    fn drop(&mut self) {
        if let Err(error) = fs::remove_dir_all(&self.path)
            && error.kind() != io::ErrorKind::NotFound
        {
            eprintln!("screenshot temp directory cleanup failed: {error}");
        }
    }
}

fn screenshot_max_bytes_from_env() -> usize {
    env::var("RUSTWRIGHT_MCP_SCREENSHOT_MAX_BYTES")
        .ok()
        .as_deref()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(DEFAULT_SCREENSHOT_MAX_BYTES)
        .clamp(1, MAX_SCREENSHOT_MAX_BYTES)
}

fn write_temp_png(temp_dir: &Path, bytes: &[u8]) -> Result<PathBuf, BrowserError> {
    #[cfg(unix)]
    use std::os::unix::fs::OpenOptionsExt;

    for _ in 0..100 {
        let sequence = NEXT_SCREENSHOT_FILE.fetch_add(1, Ordering::Relaxed);
        let path = temp_dir.join(format!("screenshot-{sequence}.png"));
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o600);
        let file = options.open(&path);
        let mut file = match file {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                return Err(BrowserError::Message(format!(
                    "screenshot temp file creation failed: {error}"
                )));
            }
        };
        if let Err(error) = file.write_all(bytes) {
            let _ = fs::remove_file(&path);
            return Err(BrowserError::Message(format!(
                "screenshot temp file write failed: {error}"
            )));
        }
        return Ok(path);
    }

    Err(BrowserError::Message(
        "screenshot temp file creation failed: no unique name available".to_owned(),
    ))
}

fn output_content(
    output: BrowserOutput,
    screenshot_max_bytes: usize,
    screenshot_temp_dir: &Path,
) -> Result<ContentBlock, BrowserError> {
    match output {
        BrowserOutput::Text(text) => Ok(ContentBlock::text(text)),
        BrowserOutput::Png(bytes) => {
            let payload_bytes = encoded_len(bytes.len(), true).unwrap_or(usize::MAX);
            if payload_bytes <= screenshot_max_bytes {
                return Ok(ContentBlock::image(STANDARD.encode(bytes), "image/png"));
            }
            let path = write_temp_png(screenshot_temp_dir, &bytes)?;
            Ok(ContentBlock::text(format!(
                "Screenshot exceeded the inline size cap ({payload_bytes} > {screenshot_max_bytes} bytes); PNG saved to `{}`.",
                path.display()
            )))
        }
    }
}

impl ServerHandler for BrowserServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new("mcp-rs", env!("CARGO_PKG_VERSION")))
            .with_instructions("Browser commands execute in order on one dedicated owner thread.")
    }

    fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<ListToolsResult, ErrorData>> + Send + '_ {
        std::future::ready(Ok(ListToolsResult::with_all_items(
            TOOL_SPECS.iter().copied().map(descriptor).collect(),
        )))
    }

    fn get_tool(&self, name: &str) -> Option<rmcp::model::Tool> {
        find_tool(name).map(descriptor)
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, ErrorData> {
        let spec = find_tool(&request.name).ok_or_else(|| {
            ErrorData::invalid_params(format!("unknown tool: {}", request.name), None)
        })?;
        let op = parse_op(spec, request.arguments)
            .map_err(|message| ErrorData::invalid_params(message, None))?;
        let request_id = context.id.clone();
        let cancellation = context.ct.clone();
        let execute = self.actor.execute(request_id.clone(), op);
        tokio::pin!(execute);
        let result = tokio::select! {
            biased;
            result = &mut execute => result,
            () = cancellation.cancelled() => {
                self.actor.cancel(&request_id);
                execute.await
            }
        };
        Ok(
            match result.and_then(|output| {
                output_content(
                    output,
                    self.screenshot_max_bytes,
                    self.screenshot_temp_dir.path(),
                )
            }) {
                Ok(content) => CallToolResult::success(vec![content]),
                Err(error) => CallToolResult::error(vec![ContentBlock::text(error.to_string())]),
            },
        )
    }

    async fn on_cancelled(
        &self,
        notification: CancelledNotificationParam,
        _context: NotificationContext<RoleServer>,
    ) {
        if let Some(request_id) = notification.request_id {
            self.actor.cancel(&request_id);
        }
    }
}
