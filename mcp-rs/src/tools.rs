use std::{collections::HashSet, env, sync::OnceLock};

use rmcp::model::{JsonObject, Tool};
use serde::{Deserialize, Deserializer};
use serde_json::{Map, Value, json};

use crate::actor::{BrowserOp, FillField, FillFieldKind, RegexSpec, ScreenshotType, TabAction};

#[derive(Clone, Copy, Debug)]
pub(crate) enum ToolKind {
    Navigate,
    NavigateBack,
    NavigateForward,
    Reload,
    Resize,
    Snapshot,
    Find,
    Click,
    Scroll,
    Type,
    SelectOption,
    FillForm,
    Hover,
    PressKey,
    Drag,
    Drop,
    ConsoleMessages,
    NetworkRequests,
    NetworkRequest,
    Tabs,
    HandleDialog,
    FileUpload,
    WaitFor,
    GetText,
    Evaluate,
    TakeScreenshot,
    Close,
}

#[derive(Clone, Copy)]
pub(crate) struct ToolSpec {
    pub(crate) kind: ToolKind,
    pub(crate) name: &'static str,
    pub(crate) description: &'static str,
}

pub(crate) const TOOL_SPECS: &[ToolSpec] = &[
    ToolSpec {
        kind: ToolKind::Navigate,
        name: "browser_navigate",
        description: "Navigate the browser and return a compact page snapshot.",
    },
    ToolSpec {
        kind: ToolKind::NavigateBack,
        name: "browser_navigate_back",
        description: "Navigate back in browser history and return a compact page snapshot.",
    },
    ToolSpec {
        kind: ToolKind::NavigateForward,
        name: "browser_navigate_forward",
        description: "Navigate forward in browser history and return a compact page snapshot.",
    },
    ToolSpec {
        kind: ToolKind::Reload,
        name: "browser_reload",
        description: "Reload the active page and return a compact page snapshot.",
    },
    ToolSpec {
        kind: ToolKind::Resize,
        name: "browser_resize",
        description: "Resize the active page viewport in CSS pixels and return a fresh snapshot.",
    },
    ToolSpec {
        kind: ToolKind::Snapshot,
        name: "browser_snapshot",
        description: "Return a fresh full or targeted compact snapshot of the current page.",
    },
    ToolSpec {
        kind: ToolKind::Find,
        name: "browser_find",
        description: "Search one fresh snapshot and return compact actionable context.",
    },
    ToolSpec {
        kind: ToolKind::Click,
        name: "browser_click",
        description: "Click or double-click a ref from the latest snapshot and return a fresh snapshot.",
    },
    ToolSpec {
        kind: ToolKind::Scroll,
        name: "browser_scroll",
        description: "Scroll a snapshot ref into view or move the viewport, then return a fresh snapshot.",
    },
    ToolSpec {
        kind: ToolKind::Type,
        name: "browser_type",
        description: "Enter text into a snapshot ref, optionally slowly or followed by Enter.",
    },
    ToolSpec {
        kind: ToolKind::SelectOption,
        name: "browser_select_option",
        description: "Select one or more option values or labels in a snapshot ref.",
    },
    ToolSpec {
        kind: ToolKind::FillForm,
        name: "browser_fill_form",
        description: "Fill up to 50 fields sequentially as a non-transactional batch.",
    },
    ToolSpec {
        kind: ToolKind::Hover,
        name: "browser_hover",
        description: "Move Chromium's native mouse to a snapshot ref and return a fresh snapshot.",
    },
    ToolSpec {
        kind: ToolKind::PressKey,
        name: "browser_press_key",
        description: "Press a native browser key, optionally focusing a snapshot ref first, and return a fresh snapshot.",
    },
    ToolSpec {
        kind: ToolKind::Drag,
        name: "browser_drag",
        description: "Physically drag one snapshot ref to another through Chromium's native mouse input.",
    },
    ToolSpec {
        kind: ToolKind::Drop,
        name: "browser_drop",
        description: "Synthesize a DataTransfer drop of confined files and/or MIME strings on a snapshot ref.",
    },
    ToolSpec {
        kind: ToolKind::ConsoleMessages,
        name: "browser_console_messages",
        description: "List bounded console records at a severity threshold.",
    },
    ToolSpec {
        kind: ToolKind::NetworkRequests,
        name: "browser_network_requests",
        description: "List bounded current-navigation network lifecycle records.",
    },
    ToolSpec {
        kind: ToolKind::NetworkRequest,
        name: "browser_network_request",
        description: "Return request/response details and a bounded lazy response body by stable index.",
    },
    ToolSpec {
        kind: ToolKind::Tabs,
        name: "browser_tabs",
        description: "List, open, select, or close browser tabs.",
    },
    ToolSpec {
        kind: ToolKind::HandleDialog,
        name: "browser_handle_dialog",
        description: "Accept or dismiss the JavaScript dialog that is pending now.",
    },
    ToolSpec {
        kind: ToolKind::FileUpload,
        name: "browser_file_upload",
        description: "Resolve the pending file chooser with confined local files, or cancel it.",
    },
    ToolSpec {
        kind: ToolKind::WaitFor,
        name: "browser_wait_for",
        description: "Wait for elapsed time and/or visible-text state, then return a fresh snapshot.",
    },
    ToolSpec {
        kind: ToolKind::GetText,
        name: "browser_get_text",
        description: "Return rendered inner text for a unique CSS selector.",
    },
    ToolSpec {
        kind: ToolKind::Evaluate,
        name: "browser_evaluate",
        description: "Evaluate a JavaScript function in the page or snapshot-ref context.",
    },
    ToolSpec {
        kind: ToolKind::TakeScreenshot,
        name: "browser_take_screenshot",
        description: "Capture the current page as an inline PNG or JPEG image. Oversized fallback files remain available until this server process shuts down.",
    },
    ToolSpec {
        kind: ToolKind::Close,
        name: "browser_close",
        description: "Close the browser; the next browser tool starts a fresh session.",
    },
];

const LEAN_TOOLS: &[&str] = &[
    "browser_navigate",
    "browser_navigate_back",
    "browser_navigate_forward",
    "browser_reload",
    "browser_resize",
    "browser_snapshot",
    "browser_click",
    "browser_scroll",
    "browser_type",
    "browser_select_option",
    "browser_hover",
    "browser_press_key",
    "browser_wait_for",
    "browser_tabs",
    "browser_evaluate",
    "browser_take_screenshot",
    "browser_close",
];

#[derive(Clone, Copy, PartialEq, Eq)]
enum ToolsetProfile {
    Mirror,
    Lean,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NavigateArgs {
    url: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EmptyArgs {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ResizeArgs {
    width: f64,
    height: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SnapshotArgs {
    target: Option<String>,
    depth: Option<f64>,
    #[serde(default)]
    boxes: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FindArgs {
    text: Option<String>,
    regex: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[serde(rename_all = "camelCase")]
struct ClickArgs {
    target: String,
    #[serde(default, alias = "double_click")]
    double_click: bool,
    #[serde(default)]
    element: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum ScrollDirection {
    Up,
    Down,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScrollArgs {
    target: Option<String>,
    direction: Option<ScrollDirection>,
    pixels: Option<f64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TypeArgs {
    target: String,
    text: String,
    #[serde(default)]
    element: Option<String>,
    #[serde(default)]
    submit: bool,
    #[serde(default)]
    slowly: bool,
    #[serde(default = "default_true")]
    clear: bool,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum OneOrMany {
    One(String),
    Many(Vec<String>),
}

impl OneOrMany {
    fn into_vec(self) -> Vec<String> {
        match self {
            Self::One(value) => vec![value],
            Self::Many(values) => values,
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SelectOptionArgs {
    target: String,
    #[serde(alias = "value")]
    values: OneOrMany,
    #[serde(default)]
    element: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum RawFillKind {
    Textbox,
    Checkbox,
    Radio,
    Combobox,
    Slider,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawFillField {
    target: String,
    name: String,
    #[serde(rename = "type")]
    kind: RawFillKind,
    value: String,
    #[serde(default)]
    element: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FillFormArgs {
    fields: Vec<RawFillField>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TargetArgs {
    target: String,
    #[serde(default)]
    element: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PressKeyArgs {
    key: String,
    #[serde(default)]
    target: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[serde(rename_all = "camelCase")]
struct DragArgs {
    start_target: String,
    end_target: String,
    #[serde(default)]
    start_element: Option<String>,
    #[serde(default)]
    end_element: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DropArgs {
    target: String,
    #[serde(default)]
    element: Option<String>,
    #[serde(default)]
    paths: Vec<String>,
    #[serde(default, deserialize_with = "deserialize_data_map")]
    data: Vec<(String, String)>,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum ConsoleLevel {
    Error,
    Warning,
    Info,
    Debug,
}

fn default_console_level() -> ConsoleLevel {
    ConsoleLevel::Info
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConsoleMessagesArgs {
    #[serde(default = "default_console_level")]
    level: ConsoleLevel,
    #[serde(default)]
    all: bool,
    filename: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NetworkRequestsArgs {
    #[serde(default, rename = "static")]
    include_static: bool,
    filter: Option<String>,
    filename: Option<String>,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub(crate) enum NetworkPart {
    RequestHeaders,
    RequestBody,
    ResponseHeaders,
    ResponseBody,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NetworkRequestArgs {
    index: u64,
    part: Option<NetworkPart>,
    filename: Option<String>,
}

fn deserialize_data_map<'de, D>(deserializer: D) -> Result<Vec<(String, String)>, D::Error>
where
    D: Deserializer<'de>,
{
    Map::<String, Value>::deserialize(deserializer)?
        .into_iter()
        .map(|(name, value)| {
            value
                .as_str()
                .map(|value| (name, value.to_owned()))
                .ok_or_else(|| serde::de::Error::custom("drop data values must be strings"))
        })
        .collect()
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum RawTabAction {
    List,
    New,
    Close,
    Select,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TabsArgs {
    action: RawTabAction,
    index: Option<usize>,
    url: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[serde(rename_all = "camelCase")]
struct HandleDialogArgs {
    accept: bool,
    #[serde(default, alias = "prompt_text")]
    prompt_text: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FileUploadArgs {
    paths: Option<Vec<String>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[serde(rename_all = "camelCase")]
struct WaitForArgs {
    time: Option<f64>,
    text: Option<String>,
    #[serde(default, alias = "text_gone")]
    text_gone: Option<String>,
    #[serde(
        default = "default_wait_timeout",
        rename = "timeout_ms",
        alias = "timeoutMs"
    )]
    timeout_ms: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetTextArgs {
    #[serde(default = "default_body")]
    selector: String,
    #[serde(default = "default_max_chars")]
    max_chars: usize,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EvaluateArgs {
    #[serde(alias = "expression")]
    function: String,
    target: Option<String>,
    element: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum RawScreenshotType {
    Png,
    Jpeg,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[serde(rename_all = "camelCase")]
struct ScreenshotArgs {
    #[serde(default, alias = "full_page")]
    full_page: bool,
    #[serde(default = "default_screenshot_type")]
    r#type: RawScreenshotType,
}

fn default_true() -> bool {
    true
}

fn default_wait_timeout() -> f64 {
    10_000.0
}

fn default_body() -> String {
    "body".to_owned()
}

fn default_max_chars() -> usize {
    20_000
}

fn default_screenshot_type() -> RawScreenshotType {
    RawScreenshotType::Png
}

pub(crate) fn validate_tool_configuration() -> Result<(), String> {
    let _ = toolset_profile();
    eval_allowed().map(|_| ())
}

pub(crate) fn enabled_tool_specs() -> Vec<ToolSpec> {
    TOOL_SPECS
        .iter()
        .copied()
        .filter(|spec| tool_enabled(spec.name))
        .collect()
}

pub(crate) fn find_tool(name: &str) -> Option<ToolSpec> {
    TOOL_SPECS
        .iter()
        .copied()
        .find(|spec| spec.name == name && tool_enabled(spec.name))
}

fn tool_enabled(name: &str) -> bool {
    let profile = toolset_profile();
    let in_profile = profile == ToolsetProfile::Mirror || LEAN_TOOLS.contains(&name);
    in_profile && (name != "browser_evaluate" || eval_allowed().unwrap_or(false))
}

fn toolset_profile() -> ToolsetProfile {
    static PROFILE: OnceLock<ToolsetProfile> = OnceLock::new();
    *PROFILE.get_or_init(|| match env::var("RUSTWRIGHT_MCP_TOOLSET") {
        Ok(profile) if profile.trim().eq_ignore_ascii_case("lean") => ToolsetProfile::Lean,
        Ok(profile) if profile.trim().eq_ignore_ascii_case("mirror") => ToolsetProfile::Mirror,
        Ok(profile) => {
            eprintln!("warning: unknown RUSTWRIGHT_MCP_TOOLSET={profile:?}; using 'mirror'");
            ToolsetProfile::Mirror
        }
        Err(_) => ToolsetProfile::Mirror,
    })
}

fn eval_allowed() -> Result<bool, String> {
    let Ok(raw) = env::var("RUSTWRIGHT_MCP_ALLOW_EVAL") else {
        return Ok(true);
    };
    match raw.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Ok(true),
        "0" | "false" | "no" | "off" => Ok(false),
        _ => Err(format!(
            "RUSTWRIGHT_MCP_ALLOW_EVAL must be one of 0, 1, false, no, off, on, true, yes; got {raw:?}"
        )),
    }
}

pub(crate) fn descriptor(spec: ToolSpec) -> Tool {
    Tool::new(spec.name, spec.description, schema(spec.kind))
}

pub(crate) fn parse_op(
    spec: ToolSpec,
    arguments: Option<Map<String, Value>>,
) -> Result<BrowserOp, String> {
    let arguments = Value::Object(arguments.unwrap_or_default());
    match spec.kind {
        ToolKind::Navigate => {
            let args: NavigateArgs = decode(arguments)?;
            if args.url.is_empty() {
                return Err("url must be a non-empty string".to_owned());
            }
            Ok(BrowserOp::Navigate(args.url))
        }
        ToolKind::NavigateBack => {
            let _: EmptyArgs = decode(arguments)?;
            Ok(BrowserOp::NavigateBack)
        }
        ToolKind::NavigateForward => {
            let _: EmptyArgs = decode(arguments)?;
            Ok(BrowserOp::NavigateForward)
        }
        ToolKind::Reload => {
            let _: EmptyArgs = decode(arguments)?;
            Ok(BrowserOp::Reload)
        }
        ToolKind::Resize => {
            let args: ResizeArgs = decode(arguments)?;
            Ok(BrowserOp::Resize {
                width: dimension(args.width, "width")?,
                height: dimension(args.height, "height")?,
            })
        }
        ToolKind::Snapshot => {
            let args: SnapshotArgs = decode(arguments)?;
            if let Some(target) = args.target.as_deref() {
                validate_ref(target)?;
            }
            let depth = args
                .depth
                .map(|depth| {
                    if !depth.is_finite() || depth < 0.0 || depth > f64::from(u32::MAX) {
                        return Err("depth must be a finite non-negative number".to_owned());
                    }
                    Ok(depth.floor() as u32)
                })
                .transpose()?;
            Ok(BrowserOp::Snapshot {
                target: args.target,
                depth,
                boxes: args.boxes,
            })
        }
        ToolKind::Find => {
            let args: FindArgs = decode(arguments)?;
            if (args.text.is_some()) == (args.regex.is_some()) {
                return Err("exactly one of text or regex is required".to_owned());
            }
            Ok(BrowserOp::Find {
                text: args.text,
                regex: args.regex.map(|regex| parse_regex(&regex)).transpose()?,
            })
        }
        ToolKind::Click => {
            let args: ClickArgs = decode(arguments)?;
            validate_ref(&args.target)?;
            let _ = args.element;
            Ok(BrowserOp::Click {
                target: args.target,
                double_click: args.double_click,
            })
        }
        ToolKind::Scroll => {
            let args: ScrollArgs = decode(arguments)?;
            match (args.target, args.direction) {
                (Some(_), Some(_)) => Err("target and direction are mutually exclusive".to_owned()),
                (None, None) => Err("exactly one of target or direction is required".to_owned()),
                (Some(target), None) => {
                    if args.pixels.is_some() {
                        return Err("pixels can only be used with direction".to_owned());
                    }
                    validate_ref(&target)?;
                    Ok(BrowserOp::ScrollTarget(target))
                }
                (None, Some(direction)) => {
                    let pixels = args.pixels.unwrap_or(500.0);
                    if !pixels.is_finite() || pixels <= 0.0 {
                        return Err("pixels must be a finite number greater than 0".to_owned());
                    }
                    let delta_y = match direction {
                        ScrollDirection::Up => -pixels,
                        ScrollDirection::Down => pixels,
                    };
                    Ok(BrowserOp::ScrollViewport(delta_y))
                }
            }
        }
        ToolKind::Type => {
            let args: TypeArgs = decode(arguments)?;
            validate_ref(&args.target)?;
            let _ = args.element;
            Ok(BrowserOp::Type {
                target: args.target,
                text: args.text,
                submit: args.submit,
                slowly: args.slowly,
                clear: args.clear,
            })
        }
        ToolKind::SelectOption => {
            let args: SelectOptionArgs = decode(arguments)?;
            validate_ref(&args.target)?;
            // An empty list is a deliberate request: it clears every selected
            // option, which is the only way to deselect a multi-select.
            let values = args.values.into_vec();
            let _ = args.element;
            Ok(BrowserOp::SelectOption {
                target: args.target,
                values,
            })
        }
        ToolKind::FillForm => {
            let args: FillFormArgs = decode(arguments)?;
            if args.fields.is_empty() || args.fields.len() > 50 {
                return Err("fields must contain between 1 and 50 entries".to_owned());
            }
            let fields = args
                .fields
                .into_iter()
                .map(|field| {
                    validate_ref(&field.target)?;
                    let _ = field.element;
                    Ok(FillField {
                        target: field.target,
                        name: field.name,
                        kind: match field.kind {
                            RawFillKind::Textbox => FillFieldKind::Textbox,
                            RawFillKind::Checkbox => FillFieldKind::Checkbox,
                            RawFillKind::Radio => FillFieldKind::Radio,
                            RawFillKind::Combobox => FillFieldKind::Combobox,
                            RawFillKind::Slider => FillFieldKind::Slider,
                        },
                        value: field.value,
                    })
                })
                .collect::<Result<Vec<_>, String>>()?;
            Ok(BrowserOp::FillForm(fields))
        }
        ToolKind::Hover => {
            let args: TargetArgs = decode(arguments)?;
            validate_ref(&args.target)?;
            let _ = args.element;
            Ok(BrowserOp::Hover(args.target))
        }
        ToolKind::PressKey => {
            let args: PressKeyArgs = decode(arguments)?;
            if args.key.is_empty() {
                return Err("key must be a non-empty string".to_owned());
            }
            if let Some(target) = &args.target {
                validate_ref(target)?;
            }
            Ok(BrowserOp::PressKey {
                target: args.target,
                key: args.key,
            })
        }
        ToolKind::Drag => {
            let args: DragArgs = decode(arguments)?;
            validate_ref(&args.start_target)?;
            validate_ref(&args.end_target)?;
            Ok(BrowserOp::Drag {
                start_target: args.start_target,
                end_target: args.end_target,
                start_element: args.start_element,
                end_element: args.end_element,
            })
        }
        ToolKind::Drop => {
            let args: DropArgs = decode(arguments)?;
            validate_ref(&args.target)?;
            if args.paths.is_empty() && args.data.is_empty() {
                return Err(
                    "browser_drop requires at least one non-empty paths or data source".to_owned(),
                );
            }
            if args.paths.len() > 50 {
                return Err("paths may contain at most 50 entries".to_owned());
            }
            let mut mimes = HashSet::new();
            for (mime, _) in &args.data {
                if mime.trim().is_empty() {
                    return Err("browser_drop data MIME keys must be non-empty".to_owned());
                }
                if !mimes.insert(mime.to_lowercase()) {
                    return Err(
                        "browser_drop data MIME keys must be unique ignoring case".to_owned()
                    );
                }
            }
            let _ = args.element;
            Ok(BrowserOp::Drop {
                target: args.target,
                paths: args.paths,
                data: args.data,
            })
        }
        ToolKind::ConsoleMessages => {
            let args: ConsoleMessagesArgs = decode(arguments)?;
            Ok(BrowserOp::ConsoleMessages {
                level: args.level,
                all: args.all,
                filename: args.filename,
            })
        }
        ToolKind::NetworkRequests => {
            let args: NetworkRequestsArgs = decode(arguments)?;
            Ok(BrowserOp::NetworkRequests {
                include_static: args.include_static,
                filter: args.filter,
                filename: args.filename,
            })
        }
        ToolKind::NetworkRequest => {
            let args: NetworkRequestArgs = decode(arguments)?;
            if args.index == 0 {
                return Err("index must be greater than or equal to 1".to_owned());
            }
            Ok(BrowserOp::NetworkRequest {
                index: args.index,
                part: args.part,
                filename: args.filename,
            })
        }
        ToolKind::Tabs => {
            let args: TabsArgs = decode(arguments)?;
            let action = match args.action {
                RawTabAction::List => TabAction::List,
                RawTabAction::New => TabAction::New,
                RawTabAction::Close => TabAction::Close,
                RawTabAction::Select => TabAction::Select,
            };
            if matches!(action, TabAction::Select) && args.index.is_none() {
                return Err("index is required for tab select".to_owned());
            }
            if !matches!(action, TabAction::New) && args.url.is_some() {
                return Err("url can only be used with action=new".to_owned());
            }
            Ok(BrowserOp::Tabs {
                action,
                index: args.index,
                url: args.url,
            })
        }
        ToolKind::HandleDialog => {
            let args: HandleDialogArgs = decode(arguments)?;
            if !args.accept && args.prompt_text.is_some() {
                return Err("promptText cannot be honored when dismissing a dialog".to_owned());
            }
            Ok(BrowserOp::HandleDialog {
                accept: args.accept,
                prompt_text: args.prompt_text,
            })
        }
        ToolKind::FileUpload => {
            let args: FileUploadArgs = decode(arguments)?;
            Ok(BrowserOp::FileUpload(args.paths.unwrap_or_default()))
        }
        ToolKind::WaitFor => {
            let args: WaitForArgs = decode(arguments)?;
            if args.time.is_none() && args.text.is_none() && args.text_gone.is_none() {
                return Err("at least one of time, text, or textGone is required".to_owned());
            }
            if args
                .time
                .is_some_and(|time| !time.is_finite() || time < 0.0)
            {
                return Err("time must be a finite non-negative number".to_owned());
            }
            if !args.timeout_ms.is_finite() || args.timeout_ms < 0.0 {
                return Err("timeout_ms must be a finite non-negative number".to_owned());
            }
            Ok(BrowserOp::WaitFor {
                time_seconds: args.time,
                text: args.text,
                text_gone: args.text_gone,
                timeout_ms: args.timeout_ms,
            })
        }
        ToolKind::GetText => {
            let args: GetTextArgs = decode(arguments)?;
            if args.selector.is_empty() {
                return Err("selector must be a non-empty string".to_owned());
            }
            Ok(BrowserOp::GetText {
                selector: args.selector,
                max_chars: args.max_chars,
            })
        }
        ToolKind::Evaluate => {
            let args: EvaluateArgs = decode(arguments)?;
            if args.function.is_empty() {
                return Err("function must be a non-empty string".to_owned());
            }
            if args.element.is_some() && args.target.is_none() {
                return Err("element requires target for browser_evaluate".to_owned());
            }
            if let Some(target) = args.target.as_deref() {
                validate_ref(target)?;
            }
            Ok(BrowserOp::Evaluate {
                function: args.function,
                target: args.target,
            })
        }
        ToolKind::TakeScreenshot => {
            let args: ScreenshotArgs = decode(arguments)?;
            Ok(BrowserOp::TakeScreenshot {
                full_page: args.full_page,
                image_type: match args.r#type {
                    RawScreenshotType::Png => ScreenshotType::Png,
                    RawScreenshotType::Jpeg => ScreenshotType::Jpeg,
                },
            })
        }
        ToolKind::Close => {
            let _: EmptyArgs = decode(arguments)?;
            Ok(BrowserOp::Close)
        }
    }
}

fn decode<T: for<'de> Deserialize<'de>>(arguments: Value) -> Result<T, String> {
    serde_json::from_value(arguments).map_err(|error| format!("invalid tool arguments: {error}"))
}

fn dimension(value: f64, name: &str) -> Result<u32, String> {
    if !value.is_finite() || value <= 0.0 || value > f64::from(u32::MAX) {
        return Err(format!("{name} must be a finite number greater than 0"));
    }
    Ok(value.round() as u32)
}

fn validate_ref(target: &str) -> Result<(), String> {
    if is_valid_ref(target) {
        Ok(())
    } else {
        Err("target must match ^e[1-9][0-9]*$".to_owned())
    }
}

fn is_valid_ref(target: &str) -> bool {
    let Some(digits) = target.strip_prefix('e') else {
        return false;
    };
    !digits.is_empty()
        && !digits.starts_with('0')
        && digits.bytes().all(|byte| byte.is_ascii_digit())
}

fn parse_regex(expression: &str) -> Result<RegexSpec, String> {
    if !expression.starts_with('/') {
        return Ok(RegexSpec {
            pattern: expression.to_owned(),
            flags: String::new(),
        });
    }
    let closing = expression
        .rfind('/')
        .ok_or_else(|| "regex slash form must be /pattern/flags (flags: i, m, s)".to_owned())?;
    if closing == 0 {
        return Err("regex slash form must be /pattern/flags (flags: i, m, s)".to_owned());
    }
    let flags = &expression[closing + 1..];
    let mut seen = HashSet::new();
    for flag in flags.chars() {
        if !matches!(flag, 'i' | 'm' | 's') {
            return Err(format!("invalid regex flags: {flag}; supported: i, m, s"));
        }
        if !seen.insert(flag) {
            return Err("regex flags must not be repeated".to_owned());
        }
    }
    Ok(RegexSpec {
        pattern: expression[1..closing].to_owned(),
        flags: flags.to_owned(),
    })
}

fn schema(kind: ToolKind) -> JsonObject {
    let ref_property = || {
        json!({
            "type": "string",
            "pattern": "^e[1-9][0-9]*$",
            "description": "Ref from the latest snapshot, such as e3"
        })
    };
    let value = match kind {
        ToolKind::Navigate => json!({
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to navigate to"}},
            "required": ["url"],
            "additionalProperties": false
        }),
        ToolKind::NavigateBack | ToolKind::NavigateForward | ToolKind::Reload | ToolKind::Close => {
            json!({"type": "object", "properties": {}, "additionalProperties": false})
        }
        ToolKind::Resize => json!({
            "type": "object",
            "properties": {
                "width": {"type": "number", "exclusiveMinimum": 0},
                "height": {"type": "number", "exclusiveMinimum": 0}
            },
            "required": ["width", "height"],
            "additionalProperties": false
        }),
        ToolKind::Snapshot => json!({
            "type": "object",
            "properties": {
                "target": ref_property(),
                "depth": {"type": "number", "minimum": 0},
                "boxes": {"type": "boolean", "default": false}
            },
            "additionalProperties": false
        }),
        ToolKind::Find => json!({
            "type": "object",
            "properties": {"text": {"type": "string"}, "regex": {"type": "string"}},
            "oneOf": [
                {"required": ["text"], "not": {"required": ["regex"]}},
                {"required": ["regex"], "not": {"required": ["text"]}}
            ],
            "additionalProperties": false
        }),
        ToolKind::Click => json!({
            "type": "object",
            "properties": {
                "target": ref_property(),
                "element": {"type": "string"},
                "doubleClick": {"type": "boolean", "default": false}
            },
            "required": ["target"],
            "additionalProperties": false
        }),
        ToolKind::Scroll => json!({
            "type": "object",
            "properties": {
                "target": ref_property(),
                "direction": {"type": "string", "enum": ["up", "down"]},
                "pixels": {"type": "number", "exclusiveMinimum": 0}
            },
            "oneOf": [
                {"required": ["target"], "not": {"anyOf": [{"required": ["direction"]}, {"required": ["pixels"]}]}},
                {"required": ["direction"], "not": {"required": ["target"]}}
            ],
            "additionalProperties": false
        }),
        ToolKind::Type => json!({
            "type": "object",
            "properties": {
                "target": ref_property(),
                "text": {"type": "string"},
                "element": {"type": "string"},
                "submit": {"type": "boolean", "default": false},
                "slowly": {"type": "boolean", "default": false},
                "clear": {"type": "boolean", "default": true}
            },
            "required": ["target", "text"],
            "additionalProperties": false
        }),
        ToolKind::SelectOption => json!({
            "type": "object",
            "properties": {
                "target": ref_property(),
                "values": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}}
                    ]
                },
                "value": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}}
                    ]
                },
                "element": {"type": "string"}
            },
            "required": ["target"],
            "oneOf": [
                {"required": ["values"], "not": {"required": ["value"]}},
                {"required": ["value"], "not": {"required": ["values"]}}
            ],
            "additionalProperties": false
        }),
        ToolKind::FillForm => json!({
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {
                        "type": "object",
                        "properties": {
                            "target": ref_property(),
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": ["textbox", "checkbox", "radio", "combobox", "slider"]},
                            "value": {"type": "string"},
                            "element": {"type": "string"}
                        },
                        "required": ["target", "name", "type", "value"],
                        "additionalProperties": false
                    }
                }
            },
            "required": ["fields"],
            "additionalProperties": false
        }),
        ToolKind::Hover => json!({
            "type": "object",
            "properties": {"target": ref_property(), "element": {"type": "string"}},
            "required": ["target"],
            "additionalProperties": false
        }),
        ToolKind::PressKey => json!({
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "target": ref_property()
            },
            "required": ["key"],
            "additionalProperties": false
        }),
        ToolKind::Drag => json!({
            "type": "object",
            "properties": {
                "startTarget": {"type": "string"},
                "endTarget": {"type": "string"},
                "startElement": {"type": ["string", "null"]},
                "endElement": {"type": ["string", "null"]}
            },
            "required": ["startTarget", "endTarget"],
            "additionalProperties": false
        }),
        ToolKind::Drop => json!({
            "type": "object",
            "properties": {
                "target": ref_property(),
                "element": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                "data": {"type": "object", "additionalProperties": {"type": "string"}}
            },
            "required": ["target"],
            "additionalProperties": false
        }),
        ToolKind::ConsoleMessages => json!({
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["error", "warning", "info", "debug"],
                    "default": "info"
                },
                "all": {"type": "boolean", "default": false},
                "filename": {"type": ["string", "null"]}
            },
            "additionalProperties": false
        }),
        ToolKind::NetworkRequests => json!({
            "type": "object",
            "properties": {
                "static": {"type": "boolean", "default": false},
                "filter": {"type": ["string", "null"]},
                "filename": {"type": ["string", "null"]}
            },
            "additionalProperties": false
        }),
        ToolKind::NetworkRequest => json!({
            "type": "object",
            "properties": {
                "index": {"type": "integer", "minimum": 1},
                "part": {
                    "type": ["string", "null"],
                    "enum": [
                        "request-headers",
                        "request-body",
                        "response-headers",
                        "response-body",
                        null
                    ]
                },
                "filename": {"type": ["string", "null"]}
            },
            "required": ["index"],
            "additionalProperties": false
        }),
        ToolKind::Tabs => json!({
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "new", "close", "select"]},
                "index": {"type": "integer", "minimum": 0},
                "url": {"type": "string"}
            },
            "required": ["action"],
            "additionalProperties": false
        }),
        ToolKind::HandleDialog => json!({
            "type": "object",
            "properties": {
                "accept": {"type": "boolean"},
                "promptText": {"type": ["string", "null"]}
            },
            "required": ["accept"],
            "additionalProperties": false
        }),
        ToolKind::FileUpload => json!({
            "type": "object",
            "properties": {
                "paths": {
                    "type": ["array", "null"],
                    "items": {"type": "string"}
                }
            },
            "additionalProperties": false
        }),
        ToolKind::WaitFor => json!({
            "type": "object",
            "properties": {
                "time": {"type": "number", "minimum": 0},
                "text": {"type": "string"},
                "textGone": {"type": "string"},
                "timeout_ms": {"type": "number", "minimum": 0, "default": 10000}
            },
            "anyOf": [{"required": ["time"]}, {"required": ["text"]}, {"required": ["textGone"]}],
            "additionalProperties": false
        }),
        ToolKind::GetText => json!({
            "type": "object",
            "properties": {
                "selector": {"type": "string", "default": "body"},
                "max_chars": {"type": "integer", "minimum": 0, "default": 20000}
            },
            "additionalProperties": false
        }),
        ToolKind::Evaluate => json!({
            "type": "object",
            "properties": {
                "function": {"type": "string"},
                "target": ref_property(),
                "element": {"type": "string"}
            },
            "required": ["function"],
            "additionalProperties": false
        }),
        ToolKind::TakeScreenshot => json!({
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["png", "jpeg"], "default": "png"},
                "fullPage": {"type": "boolean", "default": false}
            },
            "additionalProperties": false
        }),
    };
    value
        .as_object()
        .expect("tool schema must be an object")
        .clone()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ref_validation_matches_published_schema() {
        for valid in ["e1", "e9", "e10", "e999"] {
            assert!(is_valid_ref(valid), "expected {valid} to be valid");
        }
        for invalid in ["", "e", "e0", "e01", "E1", "e-1", "e1x"] {
            assert!(!is_valid_ref(invalid), "expected {invalid} to be invalid");
        }
    }

    #[test]
    fn all_descriptors_are_strict_objects() {
        let tools: Vec<Tool> = TOOL_SPECS.iter().copied().map(descriptor).collect();
        assert_eq!(tools.len(), 27);
        assert!(
            tools
                .iter()
                .all(|tool| tool.input_schema["type"] == "object"
                    && tool.input_schema["additionalProperties"] == false)
        );
    }

    #[test]
    fn physical_drag_schema_and_arguments_are_strict_and_nullable() {
        let drag = TOOL_SPECS
            .iter()
            .copied()
            .find(|spec| spec.name == "browser_drag")
            .unwrap();
        let descriptor = descriptor(drag);
        let expected_schema = json!({
            "type": "object",
            "properties": {
                "startTarget": {"type": "string"},
                "endTarget": {"type": "string"},
                "startElement": {"type": ["string", "null"]},
                "endElement": {"type": ["string", "null"]}
            },
            "required": ["startTarget", "endTarget"],
            "additionalProperties": false
        });
        assert_eq!(
            descriptor.input_schema.as_ref(),
            expected_schema.as_object().unwrap()
        );
        for valid in [
            json!({"startTarget": "e1", "endTarget": "e2"}),
            json!({
                "startTarget": "e1",
                "endTarget": "e2",
                "startElement": null,
                "endElement": null
            }),
            json!({
                "startTarget": "e1",
                "endTarget": "e2",
                "startElement": "Source card",
                "endElement": "Destination lane"
            }),
        ] {
            assert!(
                matches!(
                    parse_op(drag, Some(valid.as_object().unwrap().clone())),
                    Ok(BrowserOp::Drag { .. })
                ),
                "{valid}"
            );
        }
        for invalid in [
            json!({"endTarget": "e2"}),
            json!({"startTarget": "e1"}),
            json!({"startTarget": "source", "endTarget": "e2"}),
            json!({"startTarget": "e1", "endTarget": "target"}),
            json!({"startTarget": "e1", "endTarget": "e2", "startElement": 1}),
            json!({"startTarget": "e1", "endTarget": "e2", "unknown": true}),
        ] {
            assert!(
                parse_op(drag, Some(invalid.as_object().unwrap().clone())).is_err(),
                "{invalid}"
            );
        }
    }

    #[test]
    fn aliases_and_argument_constraints_parse() {
        let select = TOOL_SPECS
            .iter()
            .copied()
            .find(|spec| spec.name == "browser_select_option")
            .unwrap();
        assert!(
            parse_op(
                select,
                Some(
                    json!({"target": "e1", "value": "One"})
                        .as_object()
                        .unwrap()
                        .clone()
                )
            )
            .is_ok()
        );

        let wait = TOOL_SPECS
            .iter()
            .copied()
            .find(|spec| spec.name == "browser_wait_for")
            .unwrap();
        assert!(parse_op(wait, Some(Map::new())).is_err());
    }

    #[test]
    fn regex_slash_form_validates_flags() {
        let parsed = parse_regex("/hello.*/ims").expect("valid slash regex");
        assert_eq!(parsed.pattern, "hello.*");
        assert_eq!(parsed.flags, "ims");
        assert!(parse_regex("/x/ii").is_err());
        assert!(parse_regex("/x/g").is_err());
    }

    #[test]
    fn network_arguments_enforce_the_strict_published_surface() {
        let requests = TOOL_SPECS
            .iter()
            .copied()
            .find(|spec| spec.name == "browser_network_requests")
            .unwrap();
        assert!(
            parse_op(
                requests,
                Some(
                    json!({"static": true, "filter": "api/one$|api/two$"})
                        .as_object()
                        .unwrap()
                        .clone()
                )
            )
            .is_ok()
        );
        assert!(
            parse_op(
                requests,
                Some(json!({"all": true}).as_object().unwrap().clone())
            )
            .is_err()
        );

        let request = TOOL_SPECS
            .iter()
            .copied()
            .find(|spec| spec.name == "browser_network_request")
            .unwrap();
        assert!(
            parse_op(
                request,
                Some(
                    json!({"index": 1, "part": "response-body"})
                        .as_object()
                        .unwrap()
                        .clone()
                )
            )
            .is_ok()
        );
        for invalid in [
            json!({"index": 0}),
            json!({"index": 1, "part": "body"}),
            json!({"index": 1, "unknown": true}),
        ] {
            assert!(
                parse_op(request, Some(invalid.as_object().unwrap().clone())).is_err(),
                "{invalid}"
            );
        }
    }

    #[test]
    fn file_upload_schema_and_arguments_are_strict_and_nullable() {
        let upload = TOOL_SPECS
            .iter()
            .copied()
            .find(|spec| spec.name == "browser_file_upload")
            .unwrap();
        let descriptor = descriptor(upload);
        assert_eq!(
            descriptor.input_schema["properties"]["paths"],
            json!({
                "type": ["array", "null"],
                "items": {"type": "string"}
            })
        );
        assert_eq!(descriptor.input_schema["additionalProperties"], false);
        for valid in [
            json!({}),
            json!({"paths": null}),
            json!({"paths": []}),
            json!({"paths": ["one.txt", "two.txt"]}),
        ] {
            assert!(
                parse_op(upload, Some(valid.as_object().unwrap().clone())).is_ok(),
                "{valid}"
            );
        }
        for invalid in [
            json!({"paths": "one.txt"}),
            json!({"paths": [1]}),
            json!({"paths": [], "unknown": true}),
        ] {
            assert!(
                parse_op(upload, Some(invalid.as_object().unwrap().clone())).is_err(),
                "{invalid}"
            );
        }
    }
}
