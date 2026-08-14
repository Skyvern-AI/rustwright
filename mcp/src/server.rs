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
        Implementation, ListToolsResult, PaginatedRequestParams, RequestId, ServerCapabilities,
        ServerInfo,
    },
    service::{NotificationContext, RequestContext, RoleServer},
};

use crate::{
    actor::{BrowserActor, BrowserError, BrowserOutput},
    config::{FeatureConfig, ResponseBudget},
    shaping::{ResponseShape, shape_error, shape_tool_text, shape_tool_text_with_shape},
    tools::{
        descriptor_with_profile, enabled_tool_specs, find_tool, parse_op,
        validate_tool_configuration,
    },
};

const DEFAULT_SCREENSHOT_MAX_BYTES: usize = 5 * 1024 * 1024;
const MAX_SCREENSHOT_MAX_BYTES: usize = 64 * 1024 * 1024;
static NEXT_SCREENSHOT_DIR: AtomicU64 = AtomicU64::new(1);
static NEXT_SCREENSHOT_FILE: AtomicU64 = AtomicU64::new(1);

pub(crate) struct BrowserServer {
    actor: Arc<BrowserActor>,
    screenshot_max_bytes: usize,
    screenshot_temp_dir: ScreenshotTempDir,
    features: FeatureConfig,
}

impl BrowserServer {
    pub(crate) fn new() -> io::Result<Self> {
        validate_tool_configuration()
            .map_err(|message| io::Error::new(io::ErrorKind::InvalidInput, message))?;
        let screenshot_temp_dir = ScreenshotTempDir::new()?;
        let features = FeatureConfig::from_env();
        Ok(Self {
            actor: Arc::new(BrowserActor::spawn_with_features(features.clone())),
            screenshot_max_bytes: screenshot_max_bytes_from_env(),
            screenshot_temp_dir,
            features,
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

fn write_temp_image(
    temp_dir: &Path,
    bytes: &[u8],
    extension: &str,
) -> Result<PathBuf, BrowserError> {
    #[cfg(unix)]
    use std::os::unix::fs::OpenOptionsExt;

    for _ in 0..100 {
        let sequence = NEXT_SCREENSHOT_FILE.fetch_add(1, Ordering::Relaxed);
        let path = temp_dir.join(format!("screenshot-{sequence}.{extension}"));
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
) -> Result<(ContentBlock, Option<ResponseShape>), BrowserError> {
    match output {
        BrowserOutput::Text(text) => Ok((ContentBlock::text(text), None)),
        BrowserOutput::ShapedText { text, shape } => Ok((ContentBlock::text(text), Some(shape))),
        BrowserOutput::Image {
            bytes,
            mime,
            extension,
        } => {
            let payload_bytes = encoded_len(bytes.len(), true).unwrap_or(usize::MAX);
            if payload_bytes <= screenshot_max_bytes {
                return Ok((ContentBlock::image(STANDARD.encode(bytes), mime), None));
            }
            let path = write_temp_image(screenshot_temp_dir, &bytes, extension)?;
            Ok((
                ContentBlock::text(format!(
                    "Screenshot exceeded the inline size cap ({payload_bytes} > {screenshot_max_bytes} bytes); image saved to `{}`.",
                    path.display()
                )),
                None,
            ))
        }
    }
}

fn production_tool_result(
    result: Result<BrowserOutput, BrowserError>,
    tool_name: &str,
    request_id: &RequestId,
    budget: ResponseBudget,
    bypass_response_shaping: bool,
    screenshot_max_bytes: usize,
    screenshot_temp_dir: &Path,
) -> CallToolResult {
    match result
        .and_then(|output| output_content(output, screenshot_max_bytes, screenshot_temp_dir))
    {
        Ok((ContentBlock::Text(mut text), shape)) => {
            if !bypass_response_shaping {
                text.text = shape_tool_text_with_shape(
                    tool_name,
                    text.text,
                    false,
                    request_id,
                    budget,
                    shape.as_ref(),
                );
            }
            CallToolResult::success(vec![ContentBlock::Text(text)])
        }
        Ok((content, _)) => CallToolResult::success(vec![content]),
        Err(error) => {
            let metadata = error.structured_metadata();
            let mut result = CallToolResult::error(vec![ContentBlock::text(shape_tool_text(
                tool_name,
                error.to_string(),
                true,
                request_id,
                budget,
            ))]);
            result.structured_content =
                metadata.map(|metadata| serde_json::to_value(metadata).expect("metadata is JSON"));
            result
        }
    }
}

impl ServerHandler for BrowserServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new(
                "rustwright-mcp",
                env!("CARGO_PKG_VERSION"),
            ))
            .with_instructions("Browser commands execute in order on one dedicated owner thread.")
    }

    fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<ListToolsResult, ErrorData>> + Send + '_ {
        let client_name = context
            .peer
            .peer_info()
            .map(|peer| peer.client_info.name.clone());
        let lean_descriptions = self.features.lean_descriptions(client_name.as_deref());
        std::future::ready(Ok(ListToolsResult::with_all_items(
            enabled_tool_specs()
                .into_iter()
                .map(|spec| descriptor_with_profile(spec, lean_descriptions))
                .collect(),
        )))
    }

    fn get_tool(&self, name: &str) -> Option<rmcp::model::Tool> {
        let lean_descriptions = self.features.lean_descriptions(None);
        find_tool(name).map(|spec| descriptor_with_profile(spec, lean_descriptions))
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, ErrorData> {
        let request_id = context.id.clone();
        let client_name = context
            .peer
            .peer_info()
            .map(|peer| peer.client_info.name.clone());
        let budget = self.features.response_budget(client_name.as_deref());
        let tool_name = request.name.to_string();
        let spec = match find_tool(&request.name) {
            Some(spec) => spec,
            None => {
                let error =
                    ErrorData::invalid_params(format!("unknown tool: {}", request.name), None);
                return Err(shape_error(error, &request_id, budget));
            }
        };
        let op = match parse_op(spec, request.arguments) {
            Ok(op) => op,
            Err(message) => {
                return Err(shape_error(
                    ErrorData::invalid_params(message, None),
                    &request_id,
                    budget,
                ));
            }
        };
        let bypass_response_shaping = op.bypass_response_shaping();
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
        Ok(production_tool_result(
            result,
            &tool_name,
            &request_id,
            budget,
            bypass_response_shaping,
            self.screenshot_max_bytes,
            self.screenshot_temp_dir.path(),
        ))
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

#[cfg(test)]
mod tests {
    use rustwright::{
        CommandWritten, FailureKind, FailureMetadata, FailurePhase, FailureTargetKind,
    };
    use serde_json::{Value, json};

    fn classified_error(metadata: FailureMetadata) -> BrowserError {
        BrowserError::Classified {
            message: "classified action failure".to_owned(),
            metadata,
        }
    }

    fn unknown_outcome_metadata() -> FailureMetadata {
        FailureMetadata {
            kind: Some(FailureKind::UnknownOutcome),
            phase: Some(FailurePhase::Dispatch),
            target_kind: Some(FailureTargetKind::Page),
            command_written: Some(CommandWritten::Indeterminate),
            retryable: Some(false),
        }
    }

    fn test_error_result(error: BrowserError) -> CallToolResult {
        production_tool_result(
            Err(error),
            "browser_type",
            &RequestId::Number(1),
            ResponseBudget {
                max_bytes: None,
                max_lines: None,
            },
            false,
            usize::MAX,
            Path::new("."),
        )
    }

    #[test]
    fn production_tool_result_exposes_unknown_outcome_structured_content() {
        let result = test_error_result(classified_error(unknown_outcome_metadata()));
        assert_eq!(result.is_error, Some(true));
        assert_eq!(
            result.structured_content,
            Some(json!({
                "kind": "unknown_outcome",
                "phase": "dispatch",
                "target_kind": "page",
                "command_written": "indeterminate",
                "retryable": false,
            }))
        );
        assert!(matches!(result.content.as_slice(), [ContentBlock::Text(_)]));
    }

    #[test]
    fn fill_form_unknown_outcome_reaches_server_structured_content() {
        let result = production_tool_result(
            crate::actor::production_form_fill_unknown_outcome_result(),
            "browser_fill_form",
            &RequestId::Number(70_001),
            ResponseBudget {
                max_bytes: None,
                max_lines: None,
            },
            false,
            usize::MAX,
            Path::new("."),
        );
        assert_eq!(result.is_error, Some(true));
        assert_eq!(
            result.structured_content,
            Some(json!({
                "kind": "unknown_outcome",
                "phase": "dispatch",
                "target_kind": "page",
                "command_written": "indeterminate",
                "retryable": false,
            }))
        );
        let [ContentBlock::Text(text)] = result.content.as_slice() else {
            panic!("expected one shaped text content block");
        };
        assert_eq!(
            text.text,
            "Field \"empty textbox\" failed: form field dispatch failed: input command may have reached the browser, but its outcome is unknown; retrying may repeat the action"
        );
    }

    #[test]
    fn fill_form_safe_later_substep_is_not_composite_retryable() {
        let result = production_tool_result(
            crate::actor::production_form_fill_safe_later_failure_result(),
            "browser_fill_form",
            &RequestId::Number(70_002),
            ResponseBudget {
                max_bytes: None,
                max_lines: None,
            },
            false,
            usize::MAX,
            Path::new("."),
        );
        assert_eq!(
            result.structured_content,
            Some(json!({
                "kind": "timeout",
                "phase": "dispatch",
                "target_kind": "page",
                "command_written": "no",
                "retryable": false,
            }))
        );
        let [ContentBlock::Text(text)] = result.content.as_slice() else {
            panic!("expected one shaped text content block");
        };
        assert!(text.text.contains("1 of 2 form fields were written"));
        assert!(text.text.contains("Field \"second\" failed"));
    }

    #[test]
    fn type_submit_unknown_outcome_reaches_server_structured_content() {
        let result = production_tool_result(
            crate::actor::production_type_submit_unknown_outcome_result(),
            "browser_type",
            &RequestId::Number(70_003),
            ResponseBudget {
                max_bytes: None,
                max_lines: None,
            },
            false,
            usize::MAX,
            Path::new("."),
        );
        assert_eq!(
            result.structured_content,
            Some(json!({
                "kind": "unknown_outcome",
                "phase": "dispatch",
                "target_kind": "page",
                "command_written": "indeterminate",
                "retryable": false,
            }))
        );
        let [ContentBlock::Text(text)] = result.content.as_slice() else {
            panic!("expected one shaped text content block");
        };
        assert!(text.text.contains("the text write completed"));
        assert!(text.text.contains("submit failed for e1"));
    }

    #[test]
    fn type_submit_safe_enter_is_not_composite_retryable() {
        let result = production_tool_result(
            crate::actor::production_type_submit_safe_failure_result(),
            "browser_type",
            &RequestId::Number(70_004),
            ResponseBudget {
                max_bytes: None,
                max_lines: None,
            },
            false,
            usize::MAX,
            Path::new("."),
        );
        assert_eq!(
            result.structured_content,
            Some(json!({
                "kind": "timeout",
                "phase": "dispatch",
                "target_kind": "page",
                "command_written": "no",
                "retryable": false,
            }))
        );
        let [ContentBlock::Text(text)] = result.content.as_slice() else {
            panic!("expected one shaped text content block");
        };
        assert!(text.text.contains("the text write completed"));
        assert!(text.text.contains("submit failed for e1"));
    }
    use super::*;
    use rmcp::model::{ServerJsonRpcMessage, ServerResult};

    use crate::shaping::{
        ModalRecovery, ResponseShape, SnapshotStructure, TabEntry, TabsStructure,
    };

    fn wire(result: CallToolResult, id: &RequestId) -> Vec<u8> {
        let frame =
            ServerJsonRpcMessage::response(ServerResult::CallToolResult(result), id.clone());
        let mut bytes = serde_json::to_vec(&frame).unwrap();
        bytes.push(b'\n');
        bytes
    }

    #[test]
    fn production_output_path_shapes_actor_success_error_and_bypasses_image() {
        let budget = ResponseBudget {
            max_bytes: Some(4096),
            max_lines: Some(16),
        };
        let id = RequestId::String("i".repeat(256).into());
        let huge = "🙂".repeat(3000);
        let shape = ResponseShape {
            modal_recovery: vec![ModalRecovery {
                owner: "Current tab",
                kind: "alert".to_owned(),
                message: huge.clone(),
                instruction: "Call browser_handle_dialog.",
            }],
            snapshot: Some(SnapshotStructure {
                legacy: huge.clone(),
                units: vec![huge.clone(), huge.clone()],
                head: Some(huge.clone()),
                renderer_incomplete: Some(huge.clone()),
                renderer_incomplete_index: Some(1),
            }),
            result_prefix: Some(huge.clone()),
            ..Default::default()
        };
        let success = production_tool_result(
            Ok(BrowserOutput::ShapedText {
                text: format!("{huge}\n\n### Modal\n{huge}"),
                shape,
            }),
            "browser_evaluate",
            &id,
            budget,
            false,
            usize::MAX,
            Path::new("."),
        );
        assert_ne!(success.is_error, Some(true));
        assert!(wire(success, &id).len() <= 4096);

        let error = production_tool_result(
            Err(BrowserError::Message(huge)),
            "browser_evaluate",
            &id,
            budget,
            false,
            usize::MAX,
            Path::new("."),
        );
        assert_eq!(error.is_error, Some(true));
        assert!(wire(error, &id).len() <= 4096);

        let image = production_tool_result(
            Ok(BrowserOutput::Image {
                bytes: vec![1, 2, 3],
                mime: "image/png",
                extension: "png",
            }),
            "browser_take_screenshot",
            &RequestId::Number(1),
            budget,
            false,
            usize::MAX,
            Path::new("."),
        );
        assert!(matches!(image.content.as_slice(), [ContentBlock::Image(_)]));
    }

    #[test]
    fn production_output_path_preserves_fitting_selected_url_as_json() {
        let exact_url = "https://example.invalid/a\r\nb\t\0\\\"\u{2028}\u{2029}";
        let shape = ResponseShape {
            tabs: Some(TabsStructure {
                entries: vec![TabEntry {
                    index: 3,
                    title: "selected".to_owned(),
                    url: exact_url.replace(['\r', '\n'], " "),
                    active: true,
                }],
                active_index: Some(0),
                selected_exact_url: Some(exact_url.to_owned()),
            }),
            ..Default::default()
        };
        let id = RequestId::Number(81);
        let result = production_tool_result(
            Ok(BrowserOutput::ShapedText {
                text: "### Tabs\n- 3: selected".to_owned(),
                shape,
            }),
            "browser_tabs",
            &id,
            ResponseBudget {
                max_bytes: Some(4096),
                max_lines: Some(16),
            },
            false,
            usize::MAX,
            Path::new("."),
        );
        let [ContentBlock::Text(text)] = result.content.as_slice() else {
            panic!("expected one text content block");
        };
        let encoded = text
            .text
            .lines()
            .find_map(|line| line.strip_prefix("Exact active URL JSON: "))
            .expect("exact URL record");
        assert_eq!(serde_json::from_str::<String>(encoded).unwrap(), exact_url);
        assert_eq!(text.text.matches("### Tabs").count(), 1);
        assert!(wire(result, &id).len() <= 4096);
    }

    #[test]
    fn production_output_path_is_byte_identical_when_budget_is_off_or_bypassed() {
        let text = "legacy\n### Snapshot\nforged".repeat(100);
        let id = RequestId::Number(9);
        let off = production_tool_result(
            Ok(BrowserOutput::Text(text.clone())),
            "browser_snapshot",
            &id,
            ResponseBudget::default(),
            false,
            1,
            Path::new("."),
        );
        let bypassed = production_tool_result(
            Ok(BrowserOutput::Text(text.clone())),
            "browser_snapshot",
            &id,
            ResponseBudget {
                max_bytes: Some(4096),
                max_lines: Some(16),
            },
            true,
            1,
            Path::new("."),
        );
        for result in [off, bypassed] {
            let ContentBlock::Text(block) = &result.content[0] else {
                panic!("expected text")
            };
            assert_eq!(block.text, text);
        }
    }
}
