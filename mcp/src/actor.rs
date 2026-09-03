use std::{env, sync::Arc, time::Duration};

use rmcp::model::RequestId as McpRequestId;
use rustwright::ConnectOptions;
use rustwright_agent::{
    ActorConfig, BrowserActor as NativeBrowserActor, BrowserStartup, RequestId as ActorRequestId,
};

use crate::config::FeatureConfig;

pub(crate) use rustwright_agent::{
    BrowserError, BrowserOp, BrowserOutput, BrowserResult, FillField, FillFieldKind, RegexSpec,
    ScreenshotType, TabAction,
};
#[cfg(test)]
pub(crate) use rustwright_agent::{
    production_form_fill_safe_later_failure_result, production_form_fill_unknown_outcome_result,
    production_type_submit_safe_failure_result, production_type_submit_unknown_outcome_result,
};

const DEFAULT_CDP_TIMEOUT_MS: u64 = 60_000;
const DEFAULT_TOOL_TIMEOUT_MS: u64 = 60_000;
const MIN_TOOL_TIMEOUT_MS: u64 = 1_000;
const MAX_TOOL_TIMEOUT_MS: u64 = 600_000;

pub(crate) struct BrowserActor {
    inner: Arc<NativeBrowserActor>,
}

impl BrowserActor {
    pub(crate) fn spawn_with_features(features: FeatureConfig) -> Self {
        let startup = browser_startup_from_env();
        let config = ActorConfig {
            distill: features.distill,
            header: features.header,
            console_dedup: features.console_dedup,
            net_note: features.net_note,
            default_timeout: tool_timeout_from_env(),
            workspace: env::var_os("RUSTWRIGHT_MCP_WORKSPACE").map(Into::into),
        };
        Self {
            inner: Arc::new(NativeBrowserActor::spawn_with_startup_and_config(
                startup, config,
            )),
        }
    }

    pub(crate) async fn execute(&self, request_id: McpRequestId, op: BrowserOp) -> BrowserResult {
        self.inner.execute(actor_request_id(request_id), op).await
    }

    pub(crate) fn cancel(&self, request_id: &McpRequestId) -> bool {
        self.inner.cancel(&actor_request_id(request_id.clone()))
    }
}

fn actor_request_id(value: McpRequestId) -> ActorRequestId {
    match value {
        McpRequestId::Number(value) => ActorRequestId::Number(value),
        McpRequestId::String(value) => ActorRequestId::String(value.to_string()),
    }
}

fn tool_timeout_from_env() -> Duration {
    let timeout_ms = env::var("RUSTWRIGHT_MCP_TOOL_TIMEOUT_MS")
        .ok()
        .as_deref()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(DEFAULT_TOOL_TIMEOUT_MS)
        .clamp(MIN_TOOL_TIMEOUT_MS, MAX_TOOL_TIMEOUT_MS);
    Duration::from_millis(timeout_ms)
}

fn browser_startup_from_env() -> BrowserStartup {
    let endpoint = match env::var("RUSTWRIGHT_MCP_CDP_ENDPOINT") {
        Ok(endpoint) => endpoint,
        Err(env::VarError::NotPresent) => return BrowserStartup::Local,
        Err(env::VarError::NotUnicode(_)) => return BrowserStartup::InvalidRemote,
    };
    if endpoint.trim().is_empty() {
        return BrowserStartup::Local;
    }
    let timeout_ms = match env::var("RUSTWRIGHT_MCP_CDP_TIMEOUT_MS") {
        Ok(value) => match value.parse::<u64>() {
            Ok(value) if value > 0 => value,
            _ => return BrowserStartup::InvalidRemote,
        },
        Err(env::VarError::NotPresent) => DEFAULT_CDP_TIMEOUT_MS,
        Err(env::VarError::NotUnicode(_)) => return BrowserStartup::InvalidRemote,
    };
    let headers = match env::var("RUSTWRIGHT_MCP_CDP_HEADERS") {
        Ok(value) => match decode_headers(&value) {
            Some(headers) => headers,
            None => return BrowserStartup::InvalidRemote,
        },
        Err(env::VarError::NotPresent) => Vec::new(),
        Err(env::VarError::NotUnicode(_)) => return BrowserStartup::InvalidRemote,
    };
    BrowserStartup::Remote(ConnectOptions {
        endpoint,
        headers,
        timeout: Duration::from_millis(timeout_ms),
    })
}

fn decode_headers(value: &str) -> Option<Vec<(String, String)>> {
    let object = serde_json::from_str::<serde_json::Value>(value).ok()?;
    object.as_object().and_then(|object| {
        object
            .iter()
            .map(|(name, value)| Some((name.clone(), value.as_str()?.to_owned())))
            .collect()
    })
}
