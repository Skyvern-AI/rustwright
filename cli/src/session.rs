use std::{fs, thread, time::Duration};

use anyhow::{anyhow, bail, Context, Result};
use rustwright::LaunchOptions;
use rustwright_agent::{
    ActorConfig, BrowserActor, BrowserOp, BrowserOutput, BrowserStartup, RequestId, ResponseShape,
    ScreenshotType,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

const DEFAULT_SNAPSHOT_ITEMS: usize = 200;
const MAX_SNAPSHOT_ITEMS: usize = 1_000;
// Preserve the published 30-second action budget. The daemon socket allows
// 125 seconds, so cancellation and structured error delivery have ample margin.
const DEFAULT_COMMAND_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct LaunchConfig {
    pub headed: bool,
    pub executable_path: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum BrowserAction {
    Ping,
    Open { url: Option<String> },
    Snapshot { max_items: Option<usize> },
    Click { target: String },
    Fill { target: String, text: String },
    Text { target: Option<String> },
    Title,
    Url,
    Evaluate { expression: String },
    Screenshot { path: String, full_page: bool },
    Wait { milliseconds: u64 },
    Status,
    Close,
}

impl BrowserAction {
    pub fn shuts_down_daemon(&self) -> bool {
        matches!(self, Self::Close)
    }
}

pub struct BrowserSession {
    actor: BrowserActor,
    next_request_id: i64,
    closed: bool,
}

impl BrowserSession {
    pub fn new(launch: LaunchConfig) -> Self {
        let mut options = LaunchOptions::default().headless(!launch.headed);
        if let Some(path) = launch.executable_path {
            options = options.executable_path(path);
        }
        let actor = BrowserActor::spawn_with_startup_and_config(
            BrowserStartup::LocalWithOptions(options),
            ActorConfig {
                default_timeout: DEFAULT_COMMAND_TIMEOUT,
                ..ActorConfig::default()
            },
        );
        Self {
            actor,
            next_request_id: 1,
            closed: false,
        }
    }

    pub fn execute(&mut self, action: BrowserAction) -> Result<Value> {
        if self.closed
            && !matches!(
                action,
                BrowserAction::Ping
                    | BrowserAction::Open { .. }
                    | BrowserAction::Status
                    | BrowserAction::Close
            )
        {
            bail!("browser session is closed; call open before another browser command");
        }

        match action {
            BrowserAction::Ping => Ok(json!({ "status": "ready" })),
            BrowserAction::Open { url } => {
                self.closed = false;
                if let Some(url) = url {
                    self.request(BrowserOp::Navigate(url))?;
                }
                let output = self.request(snapshot_op(DEFAULT_SNAPSHOT_ITEMS))?;
                let snapshot = snapshot_from_output(output, DEFAULT_SNAPSHOT_ITEMS)?;
                let info = self.page_info()?;
                Ok(json!({
                    "url": info["url"],
                    "title": info["title"],
                    "snapshot": snapshot,
                }))
            }
            BrowserAction::Snapshot { max_items } => {
                let max_items = snapshot_item_limit(max_items)?;
                let snapshot =
                    snapshot_from_output(self.request(snapshot_op(max_items))?, max_items)?;
                Ok(json!({ "snapshot": snapshot }))
            }
            BrowserAction::Click { target } => {
                let op = match parse_target(&target)? {
                    ActorTarget::Ref(reference) => BrowserOp::Click {
                        target: reference,
                        double_click: false,
                    },
                    ActorTarget::Selector(selector) => BrowserOp::ClickSelector {
                        selector,
                        double_click: false,
                    },
                };
                self.request(op)?;
                let snapshot = snapshot_from_output(
                    self.request(snapshot_op(DEFAULT_SNAPSHOT_ITEMS))?,
                    DEFAULT_SNAPSHOT_ITEMS,
                )?;
                Ok(json!({ "clicked": target, "snapshot": snapshot }))
            }
            BrowserAction::Fill { target, text } => {
                let op = match parse_target(&target)? {
                    ActorTarget::Ref(reference) => BrowserOp::Type {
                        target: reference,
                        text,
                        submit: false,
                        slowly: false,
                        clear: true,
                    },
                    ActorTarget::Selector(selector) => BrowserOp::FillSelector { selector, text },
                };
                self.request(op)?;
                let snapshot = snapshot_from_output(
                    self.request(snapshot_op(DEFAULT_SNAPSHOT_ITEMS))?,
                    DEFAULT_SNAPSHOT_ITEMS,
                )?;
                Ok(json!({ "filled": target, "snapshot": snapshot }))
            }
            BrowserAction::Text { target } => {
                let op = match target.as_deref() {
                    Some(target) => match parse_target(target)? {
                        ActorTarget::Ref(reference) => BrowserOp::GetTextRef {
                            target: reference,
                            max_chars: usize::MAX,
                        },
                        ActorTarget::Selector(selector) => BrowserOp::GetTextStrict {
                            selector,
                            max_chars: usize::MAX,
                        },
                    },
                    None => BrowserOp::GetTextStrict {
                        selector: "body".to_owned(),
                        max_chars: usize::MAX,
                    },
                };
                let text = text_from_output(self.request(op)?)?;
                Ok(json!({ "target": target, "text": text }))
            }
            BrowserAction::Title => Ok(json!({ "title": self.page_info()?["title"] })),
            BrowserAction::Url => Ok(json!({ "url": self.page_info()?["url"] })),
            BrowserAction::Evaluate { expression } => {
                let output = self.request(BrowserOp::EvaluateWire { expression })?;
                let value = result_value(output)?;
                Ok(json!({ "value": value }))
            }
            BrowserAction::Screenshot { path, full_page } => {
                let output = self.request(BrowserOp::TakeScreenshot {
                    full_page,
                    image_type: ScreenshotType::Png,
                })?;
                let bytes = image_from_output(output)?;
                fs::write(&path, &bytes).with_context(|| format!("failed to write {path}"))?;
                Ok(json!({ "path": path, "bytes": bytes.len() }))
            }
            BrowserAction::Wait { milliseconds } => {
                thread::sleep(Duration::from_millis(milliseconds));
                Ok(json!({ "waited_ms": milliseconds }))
            }
            BrowserAction::Status => json_from_text(self.request(BrowserOp::Status)?),
            BrowserAction::Close => {
                self.request(BrowserOp::Close)?;
                self.closed = true;
                Ok(json!({ "closed": true }))
            }
        }
    }

    pub fn close(&mut self) -> Result<()> {
        if !self.closed {
            self.request(BrowserOp::Close)?;
            self.closed = true;
        }
        Ok(())
    }

    fn request(&mut self, op: BrowserOp) -> Result<BrowserOutput> {
        let request_id = RequestId::Number(self.next_request_id);
        self.next_request_id = self.next_request_id.saturating_add(1);
        self.actor
            .execute_blocking(request_id, op)
            .map_err(|error| anyhow!(error.to_string()))
    }

    fn page_info(&mut self) -> Result<Value> {
        json_from_text(self.request(BrowserOp::PageInfo)?)
    }
}

impl Drop for BrowserSession {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

fn snapshot_item_limit(max_items: Option<usize>) -> Result<usize> {
    let max_items = max_items.unwrap_or(DEFAULT_SNAPSHOT_ITEMS);
    if max_items == 0 {
        bail!("max_items must be greater than zero");
    }
    Ok(max_items.min(MAX_SNAPSHOT_ITEMS))
}

fn snapshot_op(max_items: usize) -> BrowserOp {
    BrowserOp::SnapshotLimited { max_items }
}

fn snapshot_from_output(output: BrowserOutput, max_items: usize) -> Result<String> {
    if max_items == 0 {
        bail!("max_items must be greater than zero");
    }
    let (text, shape) = text_and_shape(output)?;
    let snapshot = shape
        .and_then(|shape| shape.snapshot)
        .map(|snapshot| snapshot.legacy)
        .or_else(|| {
            text.rsplit_once("\n\n### Snapshot\n")
                .map(|(_, value)| value.to_owned())
        })
        .unwrap_or(text);
    let limit = max_items.min(MAX_SNAPSHOT_ITEMS);
    let truncated = snapshot.lines().count() > limit;
    let mut visible = snapshot
        .lines()
        .take(limit)
        .map(render_cli_refs)
        .collect::<Vec<_>>();
    if truncated {
        visible.push("  - note: snapshot truncated".to_owned());
    }
    Ok(visible.join("\n"))
}

fn render_cli_refs(line: &str) -> String {
    if line.trim_start().starts_with("- text:") {
        return line.to_owned();
    }

    let characters = line.chars().collect::<Vec<_>>();
    let mut output = String::with_capacity(line.len() + 1);
    let mut index = 0;
    let mut quoted = false;
    while index < characters.len() {
        if characters[index] == '\\' && quoted && index + 1 < characters.len() {
            output.push(characters[index]);
            output.push(characters[index + 1]);
            index += 2;
            continue;
        }
        if characters[index] == '"' {
            quoted = !quoted;
        }
        let marker = ['[', 'r', 'e', 'f', '=', 'e'];
        if !quoted && characters[index..].starts_with(&marker) {
            let mut end = index + marker.len();
            while end < characters.len() && characters[end].is_ascii_digit() {
                end += 1;
            }
            if end > index + marker.len()
                && characters
                    .get(end)
                    .is_some_and(|character| *character == ']')
            {
                output.push_str("[ref=@e");
                output.extend(characters[index + marker.len()..end].iter());
                output.push(']');
                index = end + 1;
                continue;
            }
        }
        output.push(characters[index]);
        index += 1;
    }
    output
}

fn result_value(output: BrowserOutput) -> Result<Value> {
    let (text, shape) = text_and_shape(output)?;
    let value = shape
        .and_then(|shape| shape.result_prefix)
        .or_else(|| {
            text.split_once("\n\n### Snapshot\n")
                .map(|(value, _)| value.to_owned())
        })
        .unwrap_or(text);
    let value = serde_json::from_str(&value).context("actor evaluation result was not JSON")?;
    Ok(decode_legacy_runtime_value(value))
}

fn decode_legacy_runtime_value(value: Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(
            values
                .into_iter()
                .map(decode_legacy_runtime_value)
                .collect(),
        ),
        Value::Object(mut object) => {
            if object.contains_key("__rustwright_cdp_array__") {
                if let Some(Value::Array(items)) = object.remove("items") {
                    return Value::Array(
                        items.into_iter().map(decode_legacy_runtime_value).collect(),
                    );
                }
            }
            if object.contains_key("__rustwright_cdp_object__") {
                if let Some(Value::Object(entries)) = object.remove("entries") {
                    return Value::Object(
                        entries
                            .into_iter()
                            .map(|(key, value)| (key, decode_legacy_runtime_value(value)))
                            .collect(),
                    );
                }
            }
            Value::Object(
                object
                    .into_iter()
                    .map(|(key, value)| (key, decode_legacy_runtime_value(value)))
                    .collect(),
            )
        }
        value => value,
    }
}

fn json_from_text(output: BrowserOutput) -> Result<Value> {
    let text = text_from_output(output)?;
    serde_json::from_str(&text).context("actor response was not JSON")
}

fn text_from_output(output: BrowserOutput) -> Result<String> {
    text_and_shape(output).map(|(text, _)| text)
}

fn text_and_shape(output: BrowserOutput) -> Result<(String, Option<ResponseShape>)> {
    match output {
        BrowserOutput::Text(text) => Ok((text, None)),
        BrowserOutput::ShapedText { text, shape } => Ok((text, Some(shape))),
        BrowserOutput::Image { .. } => bail!("actor returned an image for a text operation"),
    }
}

fn image_from_output(output: BrowserOutput) -> Result<Vec<u8>> {
    match output {
        BrowserOutput::Image { bytes, .. } => Ok(bytes),
        BrowserOutput::Text(_) | BrowserOutput::ShapedText { .. } => {
            bail!("actor returned text for a screenshot operation")
        }
    }
}

enum ActorTarget {
    Ref(String),
    Selector(String),
}

fn parse_target(target: &str) -> Result<ActorTarget> {
    if let Some(reference) = target.strip_prefix('@') {
        validate_reference(reference)?;
        Ok(ActorTarget::Ref(reference.to_owned()))
    } else if target.is_empty() {
        bail!("target must not be empty")
    } else {
        Ok(ActorTarget::Selector(target.to_owned()))
    }
}

#[cfg(test)]
fn selector_for_target(target: &str) -> Result<String> {
    match parse_target(target)? {
        ActorTarget::Ref(reference) => Ok(format!(r#"[data-rustwright-ref="{reference}"]"#)),
        ActorTarget::Selector(selector) => Ok(selector),
    }
}

fn validate_reference(reference: &str) -> Result<()> {
    let Some(number) = reference.strip_prefix('e') else {
        bail!("snapshot references must use @eN")
    };
    if number.is_empty()
        || number.starts_with('0')
        || !number.bytes().all(|byte| byte.is_ascii_digit())
    {
        bail!("snapshot references must use @eN")
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshot_references_become_shared_actor_targets() {
        assert_eq!(
            selector_for_target("@e42").unwrap(),
            r#"[data-rustwright-ref="e42"]"#
        );
    }

    #[test]
    fn selectors_pass_through_unchanged() {
        assert_eq!(
            selector_for_target("button.submit").unwrap(),
            "button.submit"
        );
    }

    #[test]
    fn legacy_evaluation_unwraps_containers_but_preserves_leaves() {
        let value = json!({
            "__rustwright_cdp_object__": 1,
            "entries": {
                "values": {
                    "__rustwright_cdp_array__": 2,
                    "items": [
                        1,
                        {"__rustwright_cdp_unserializable_value__": "NaN"}
                    ]
                }
            }
        });
        assert_eq!(
            decode_legacy_runtime_value(value),
            json!({
                "values": [
                    1,
                    {"__rustwright_cdp_unserializable_value__": "NaN"}
                ]
            })
        );
    }

    #[test]
    fn cli_ref_rendering_does_not_mutate_page_text() {
        assert_eq!(
            render_cli_refs(r#"- button "literal [ref=e42]" [ref=e7]"#),
            r#"- button "literal [ref=e42]" [ref=@e7]"#
        );
        assert_eq!(
            render_cli_refs("- text: literal [ref=e42]"),
            "- text: literal [ref=e42]"
        );
    }

    #[test]
    fn snapshot_limit_is_validated_before_actor_dispatch() {
        assert_eq!(
            snapshot_item_limit(Some(MAX_SNAPSHOT_ITEMS + 1)).unwrap(),
            MAX_SNAPSHOT_ITEMS
        );
        assert!(snapshot_item_limit(Some(0)).is_err());
    }

    #[test]
    fn truncated_snapshot_keeps_legacy_notice() {
        assert_eq!(
            snapshot_from_output(BrowserOutput::Text("first\nsecond".to_owned()), 1).unwrap(),
            "first\n  - note: snapshot truncated"
        );
    }

    #[test]
    fn malformed_snapshot_references_are_rejected() {
        assert!(selector_for_target("@x1").is_err());
        assert!(selector_for_target("@e1]").is_err());
    }
}
