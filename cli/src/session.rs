use std::thread;
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use rustwright_core::{rustwright_launch_chromium, RustwrightBrowser, RustwrightPage};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

const DEFAULT_TIMEOUT_MS: f64 = 30_000.0;
const DEFAULT_SNAPSHOT_ITEMS: usize = 200;
const MAX_SNAPSHOT_ITEMS: usize = 1_000;
const REF_ATTRIBUTE: &str = "data-rustwright-agent-ref";

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
    launch: LaunchConfig,
    browser: Option<RustwrightBrowser>,
    page: Option<RustwrightPage>,
    closed: bool,
    launch_failed: bool,
}

impl BrowserSession {
    pub fn new(launch: LaunchConfig) -> Self {
        Self {
            launch,
            browser: None,
            page: None,
            closed: false,
            launch_failed: false,
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
                self.open(url.as_deref())
            }
            BrowserAction::Snapshot { max_items } => {
                let snapshot = self.snapshot(max_items.unwrap_or(DEFAULT_SNAPSHOT_ITEMS))?;
                Ok(json!({ "snapshot": snapshot }))
            }
            BrowserAction::Click { target } => {
                let selector = selector_for_target(&target)?;
                self.page()?.click(&selector, Some(DEFAULT_TIMEOUT_MS))?;
                Ok(json!({
                    "clicked": target,
                    "snapshot": self.snapshot(DEFAULT_SNAPSHOT_ITEMS)?,
                }))
            }
            BrowserAction::Fill { target, text } => {
                let selector = selector_for_target(&target)?;
                self.page()?
                    .fill(&selector, &text, Some(DEFAULT_TIMEOUT_MS))?;
                Ok(json!({
                    "filled": target,
                    "snapshot": self.snapshot(DEFAULT_SNAPSHOT_ITEMS)?,
                }))
            }
            BrowserAction::Text { target } => {
                let selector = match target.as_deref() {
                    Some(target) => selector_for_target(target)?,
                    None => "body".to_string(),
                };
                let text = self
                    .page()?
                    .text_content(&selector, Some(DEFAULT_TIMEOUT_MS))?
                    .ok_or_else(|| anyhow!("target {selector:?} was not found"))?;
                Ok(json!({ "target": target, "text": text }))
            }
            BrowserAction::Title => Ok(json!({
                "title": self.page()?.title(Some(DEFAULT_TIMEOUT_MS))?,
            })),
            BrowserAction::Url => Ok(json!({ "url": self.current_url()? })),
            BrowserAction::Evaluate { expression } => {
                let raw = self
                    .page()?
                    .evaluate(&expression, None, Some(DEFAULT_TIMEOUT_MS))?;
                Ok(json!({ "value": decode_evaluation(&raw) }))
            }
            BrowserAction::Screenshot { path, full_page } => {
                let bytes = self.page()?.screenshot(
                    Some(&path),
                    Some(full_page),
                    None,
                    Some(DEFAULT_TIMEOUT_MS),
                    None,
                    None,
                    None,
                )?;
                Ok(json!({ "path": path, "bytes": bytes.len() }))
            }
            BrowserAction::Wait { milliseconds } => {
                thread::sleep(Duration::from_millis(milliseconds));
                Ok(json!({ "waited_ms": milliseconds }))
            }
            BrowserAction::Status => Ok(json!({
                "running": self.browser.is_some(),
                "launch_failed": self.launch_failed,
                "url": if self.page.is_some() { Some(self.current_url()?) } else { None },
            })),
            BrowserAction::Close => {
                self.close()?;
                Ok(json!({ "closed": true }))
            }
        }
    }

    pub fn screenshot_bytes(&mut self, full_page: bool) -> Result<Vec<u8>> {
        if self.closed {
            bail!("browser session is closed; call browser_open before another browser command");
        }
        Ok(self.page()?.screenshot(
            None,
            Some(full_page),
            None,
            Some(DEFAULT_TIMEOUT_MS),
            None,
            None,
            None,
        )?)
    }

    pub fn close(&mut self) -> Result<()> {
        self.page.take();
        if let Some(browser) = self.browser.take() {
            browser.close()?;
        }
        self.closed = true;
        self.launch_failed = false;
        Ok(())
    }

    fn ensure_page(&mut self) -> Result<()> {
        if self.page.is_some() {
            return Ok(());
        }

        let mut options = json!({
            "headless": !self.launch.headed,
            "timeout": DEFAULT_TIMEOUT_MS,
        });
        if let Some(executable_path) = &self.launch.executable_path {
            options["executable_path"] = Value::String(executable_path.clone());
        }
        let browser = match rustwright_launch_chromium(&options.to_string()) {
            Ok(browser) => browser,
            Err(error) => {
                self.launch_failed = true;
                return Err(error).context("failed to launch Chromium");
            }
        };
        let page = match browser.new_page() {
            Ok(page) => page,
            Err(error) => {
                self.launch_failed = true;
                let _ = browser.close();
                return Err(error).context("failed to create page");
            }
        };
        self.browser = Some(browser);
        self.page = Some(page);
        self.launch_failed = false;
        Ok(())
    }

    fn page(&mut self) -> Result<RustwrightPage> {
        self.ensure_page()?;
        self.page
            .as_ref()
            .cloned()
            .ok_or_else(|| anyhow!("browser page is unavailable"))
    }

    fn open(&mut self, url: Option<&str>) -> Result<Value> {
        let page = self.page()?;
        if let Some(url) = url {
            page.goto(url, Some("load"), Some(DEFAULT_TIMEOUT_MS), None)
                .with_context(|| format!("failed to open {url}"))?;
        }
        Ok(json!({
            "url": self.current_url()?,
            "title": page.title(Some(DEFAULT_TIMEOUT_MS))?,
            "snapshot": self.snapshot(DEFAULT_SNAPSHOT_ITEMS)?,
        }))
    }

    fn current_url(&mut self) -> Result<String> {
        let raw =
            self.page()?
                .evaluate("document.location.href", None, Some(DEFAULT_TIMEOUT_MS))?;
        Ok(serde_json::from_str::<String>(&raw).unwrap_or(raw))
    }

    fn snapshot(&mut self, max_items: usize) -> Result<String> {
        if max_items == 0 {
            bail!("max_items must be greater than zero");
        }
        let max_items = max_items.min(MAX_SNAPSHOT_ITEMS);
        let script = snapshot_script(max_items);
        let raw = self
            .page()?
            .evaluate(&script, None, Some(DEFAULT_TIMEOUT_MS))?;
        let encoded = serde_json::from_str::<String>(&raw)
            .context("snapshot script did not return a JSON string")?;
        let payload: SnapshotPayload =
            serde_json::from_str(&encoded).context("snapshot payload was invalid")?;
        let mut output = format!("- page {:?}\n  - url: {}", payload.title, payload.url);
        for line in payload.lines {
            output.push_str("\n  ");
            output.push_str(&line);
        }
        if payload.truncated {
            output.push_str("\n  - note: snapshot truncated");
        }
        Ok(output)
    }
}

impl Drop for BrowserSession {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

#[derive(Debug, Deserialize)]
struct SnapshotPayload {
    url: String,
    title: String,
    lines: Vec<String>,
    truncated: bool,
}

fn snapshot_script(max_items: usize) -> String {
    format!(
        r#"() => {{
  const refAttr = {ref_attribute};
  const maxItems = {max_items};
  document.querySelectorAll(`[${{refAttr}}]`).forEach(el => el.removeAttribute(refAttr));
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = el => {{
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
  }};
  const explicitRole = el => el.getAttribute('role');
  const implicitRole = el => {{
    const tag = el.tagName.toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'summary') return 'button';
    if (tag === 'img') return 'img';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'input') {{
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (['button', 'submit', 'reset'].includes(type)) return 'button';
      return 'textbox';
    }}
    return '';
  }};
  const interactive = el => {{
    const tag = el.tagName.toLowerCase();
    return ['a', 'button', 'input', 'textarea', 'select', 'summary'].includes(tag) ||
      el.hasAttribute('role') || el.hasAttribute('onclick') ||
      el.hasAttribute('contenteditable') || el.tabIndex >= 0;
  }};
  const name = el => normalize(
    (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => document.getElementById(id)).filter(Boolean)
      .map(node => node.innerText || node.textContent).join(' ') ||
    el.getAttribute('aria-label') ||
    (el.labels ? Array.from(el.labels).map(label => label.innerText || label.textContent).join(' ') : '') ||
    el.getAttribute('alt') ||
    el.getAttribute('placeholder') || el.getAttribute('title') ||
    (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) ? el.value : '') ||
    el.innerText || el.textContent
  ).slice(0, 160);
  const lines = [];
  let refIndex = 0;
  let truncated = false;
  const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,a,button,input,textarea,select,summary,[role],[contenteditable],[onclick],[tabindex]'));
  for (const el of nodes) {{
    if (!visible(el)) continue;
    const isInteractive = interactive(el);
    const role = explicitRole(el) || implicitRole(el) || el.tagName.toLowerCase();
    const label = name(el);
    if (!isInteractive && !label) continue;
    if (lines.length >= maxItems) {{
      truncated = true;
      break;
    }}
    let ref = '';
    if (isInteractive) {{
      ref = `e${{++refIndex}}`;
      el.setAttribute(refAttr, ref);
    }}
    const checked = 'checked' in el && el.checked ? ' [checked]' : '';
    const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true' ? ' [disabled]' : '';
    const refText = ref ? ` [ref=@${{ref}}]` : '';
    lines.push(`- ${{role}}${{label ? ` "${{label.replace(/"/g, '\\"')}}"` : ''}}${{refText}}${{checked}}${{disabled}}`);
  }}
  return JSON.stringify({{
    url: document.location.href,
    title: document.title,
    lines,
    truncated,
  }});
}}"#,
        ref_attribute = serde_json::to_string(REF_ATTRIBUTE).expect("constant is valid JSON"),
    )
}

pub fn selector_for_target(target: &str) -> Result<String> {
    let Some(reference) = target.strip_prefix('@') else {
        if target.trim().is_empty() {
            bail!("target must not be empty");
        }
        return Ok(target.to_string());
    };
    if reference.len() < 2
        || !reference.starts_with('e')
        || !reference[1..]
            .chars()
            .all(|character| character.is_ascii_digit())
    {
        bail!("invalid snapshot reference {target:?}; expected @e followed by digits");
    }
    Ok(format!(r#"[{REF_ATTRIBUTE}="{reference}"]"#))
}

fn decode_evaluation(raw: &str) -> Value {
    let value = serde_json::from_str(raw).unwrap_or_else(|_| Value::String(raw.to_string()));
    decode_runtime_value(value)
}

fn decode_runtime_value(value: Value) -> Value {
    match value {
        Value::Array(values) => {
            Value::Array(values.into_iter().map(decode_runtime_value).collect())
        }
        Value::Object(mut object) => {
            if object.contains_key("__rustwright_cdp_array__") {
                if let Some(Value::Array(items)) = object.remove("items") {
                    return Value::Array(items.into_iter().map(decode_runtime_value).collect());
                }
            }
            if object.contains_key("__rustwright_cdp_object__") {
                if let Some(Value::Object(entries)) = object.remove("entries") {
                    return Value::Object(
                        entries
                            .into_iter()
                            .map(|(key, value)| (key, decode_runtime_value(value)))
                            .collect(),
                    );
                }
            }
            Value::Object(
                object
                    .into_iter()
                    .map(|(key, value)| (key, decode_runtime_value(value)))
                    .collect(),
            )
        }
        value => value,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshot_references_become_scoped_css_selectors() {
        assert_eq!(
            selector_for_target("@e42").unwrap(),
            r#"[data-rustwright-agent-ref="e42"]"#
        );
    }

    #[test]
    fn css_and_text_targets_pass_through() {
        assert_eq!(selector_for_target("#submit").unwrap(), "#submit");
        assert_eq!(
            selector_for_target("text=Continue").unwrap(),
            "text=Continue"
        );
    }

    #[test]
    fn malformed_references_are_rejected() {
        assert!(selector_for_target("@e").is_err());
        assert!(selector_for_target("@x1").is_err());
        assert!(selector_for_target("@e1]").is_err());
    }

    #[test]
    fn evaluation_objects_and_arrays_become_plain_json() {
        let raw = r#"{"__rustwright_cdp_object__":1,"entries":{"name":"Ada","values":{"__rustwright_cdp_array__":2,"items":[1,2]}}}"#;
        assert_eq!(
            decode_evaluation(raw),
            json!({ "name": "Ada", "values": [1, 2] })
        );
    }

    #[test]
    fn snapshot_script_embeds_limits_and_reference_attribute() {
        let script = snapshot_script(17);
        assert!(script.contains("const maxItems = 17"));
        assert!(script.contains(REF_ATTRIBUTE));
    }
}
