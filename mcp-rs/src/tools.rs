use rmcp::model::{JsonObject, Tool};
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::actor::BrowserOp;

#[derive(Clone, Copy, Debug)]
pub(crate) enum ToolKind {
    Navigate,
    NavigateBack,
    NavigateForward,
    Snapshot,
    Click,
    Scroll,
    TakeScreenshot,
}

#[derive(Clone, Copy)]
pub(crate) struct ToolSpec {
    pub(crate) kind: ToolKind,
    pub(crate) name: &'static str,
    pub(crate) description: &'static str,
}

pub(crate) const TOOL_SPECS: [ToolSpec; 7] = [
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
        kind: ToolKind::Snapshot,
        name: "browser_snapshot",
        description: "Return a fresh compact snapshot of the current page.",
    },
    ToolSpec {
        kind: ToolKind::Click,
        name: "browser_click",
        description: "Click a ref from the latest snapshot and return a fresh snapshot.",
    },
    ToolSpec {
        kind: ToolKind::Scroll,
        name: "browser_scroll",
        description: "Scroll a snapshot ref into view or move the viewport, then return a fresh snapshot.",
    },
    ToolSpec {
        kind: ToolKind::TakeScreenshot,
        name: "browser_take_screenshot",
        description: "Capture the current browser viewport as a PNG image. Oversized fallback files remain available until this server process shuts down.",
    },
];

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NavigateArgs {
    url: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SnapshotArgs {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ClickArgs {
    target: String,
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

pub(crate) fn find_tool(name: &str) -> Option<ToolSpec> {
    TOOL_SPECS.iter().copied().find(|spec| spec.name == name)
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
            let _: SnapshotArgs = decode(arguments)?;
            Ok(BrowserOp::NavigateBack)
        }
        ToolKind::NavigateForward => {
            let _: SnapshotArgs = decode(arguments)?;
            Ok(BrowserOp::NavigateForward)
        }
        ToolKind::Snapshot => {
            let _: SnapshotArgs = decode(arguments)?;
            Ok(BrowserOp::Snapshot)
        }
        ToolKind::Click => {
            let args: ClickArgs = decode(arguments)?;
            if !is_valid_ref(&args.target) {
                return Err("target must match ^e[1-9][0-9]*$".to_owned());
            }
            Ok(BrowserOp::Click(args.target))
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
                    if !is_valid_ref(&target) {
                        return Err("target must match ^e[1-9][0-9]*$".to_owned());
                    }
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
        ToolKind::TakeScreenshot => {
            let _: SnapshotArgs = decode(arguments)?;
            Ok(BrowserOp::TakeScreenshot)
        }
    }
}

fn decode<T: for<'de> Deserialize<'de>>(arguments: Value) -> Result<T, String> {
    serde_json::from_value(arguments).map_err(|error| format!("invalid tool arguments: {error}"))
}

fn is_valid_ref(target: &str) -> bool {
    let Some(digits) = target.strip_prefix('e') else {
        return false;
    };
    !digits.is_empty()
        && !digits.starts_with('0')
        && digits.bytes().all(|byte| byte.is_ascii_digit())
}

fn schema(kind: ToolKind) -> JsonObject {
    let value = match kind {
        ToolKind::Navigate => json!({
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"}
            },
            "required": ["url"],
            "additionalProperties": false
        }),
        ToolKind::NavigateBack
        | ToolKind::NavigateForward
        | ToolKind::Snapshot
        | ToolKind::TakeScreenshot => json!({
            "type": "object",
            "properties": {},
            "additionalProperties": false
        }),
        ToolKind::Click => json!({
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "pattern": "^e[1-9][0-9]*$",
                    "description": "Ref from the latest snapshot, such as e3"
                }
            },
            "required": ["target"],
            "additionalProperties": false
        }),
        ToolKind::Scroll => json!({
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "pattern": "^e[1-9][0-9]*$",
                    "description": "Ref from the latest snapshot to scroll into view"
                },
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "Viewport scroll direction"
                },
                "pixels": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "Viewport scroll distance; defaults to 500"
                }
            },
            "oneOf": [
                {
                    "required": ["target"],
                    "not": {
                        "anyOf": [
                            {"required": ["direction"]},
                            {"required": ["pixels"]}
                        ]
                    }
                },
                {
                    "required": ["direction"],
                    "not": {"required": ["target"]}
                }
            ],
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
    fn history_navigation_tools_resolve_parse_and_reject_arguments() {
        let back = find_tool("browser_navigate_back").expect("navigate back tool");
        assert!(matches!(
            parse_op(back, None).expect("parse navigate back"),
            BrowserOp::NavigateBack
        ));

        let forward = find_tool("browser_navigate_forward").expect("navigate forward tool");
        assert!(matches!(
            parse_op(forward, Some(Map::new())).expect("parse navigate forward"),
            BrowserOp::NavigateForward
        ));

        for spec in [back, forward] {
            let tool = descriptor(spec);
            assert_eq!(tool.input_schema["properties"], json!({}));
            assert_eq!(tool.input_schema["additionalProperties"], false);
            assert!(tool.input_schema.get("required").is_none());

            let arguments = Map::from_iter([("unexpected".to_owned(), Value::Bool(true))]);
            let error = parse_op(spec, Some(arguments)).expect_err("reject unknown argument");
            assert!(error.contains("unknown field `unexpected`"), "{error}");
        }
    }

    #[test]
    fn scroll_tool_resolves_and_descriptor_publishes_argument_contract() {
        let scroll = find_tool("browser_scroll").expect("scroll tool");
        let tool = descriptor(scroll);

        assert_eq!(
            tool.input_schema["properties"]["target"]["pattern"],
            "^e[1-9][0-9]*$"
        );
        assert_eq!(
            tool.input_schema["properties"]["direction"]["enum"],
            json!(["up", "down"])
        );
        assert_eq!(tool.input_schema["properties"]["pixels"]["type"], "number");
        assert_eq!(
            tool.input_schema["properties"]["pixels"]["exclusiveMinimum"],
            0
        );
        assert_eq!(
            tool.input_schema["oneOf"],
            json!([
                {
                    "required": ["target"],
                    "not": {
                        "anyOf": [
                            {"required": ["direction"]},
                            {"required": ["pixels"]}
                        ]
                    }
                },
                {
                    "required": ["direction"],
                    "not": {"required": ["target"]}
                }
            ])
        );
        assert_eq!(tool.input_schema["additionalProperties"], false);
    }

    #[test]
    fn scroll_parse_accepts_target_or_direction() {
        let scroll = find_tool("browser_scroll").expect("scroll tool");
        let parse = |arguments: Value| {
            parse_op(
                scroll,
                Some(arguments.as_object().expect("object arguments").clone()),
            )
        };

        assert!(parse(json!({"target": "e12"})).is_ok());
        assert!(parse(json!({"direction": "down"})).is_ok());
        assert!(
            parse(json!({
                "direction": "up",
                "pixels": 250.5
            }))
            .is_ok()
        );
    }

    #[test]
    fn scroll_parse_rejects_invalid_argument_matrix() {
        let scroll = find_tool("browser_scroll").expect("scroll tool");
        let parse = |arguments: Value| {
            parse_op(
                scroll,
                Some(arguments.as_object().expect("object arguments").clone()),
            )
        };

        assert!(parse_op(scroll, None).is_err(), "accepted absent arguments");
        for (case, arguments) in [
            (
                "target and direction",
                json!({"target": "e12", "direction": "down"}),
            ),
            ("neither target nor direction", json!({})),
            ("pixels without direction", json!({"pixels": 100})),
            (
                "pixels with target",
                json!({"target": "e12", "pixels": 100}),
            ),
            ("bad direction", json!({"direction": "left"})),
            ("zero pixels", json!({"direction": "down", "pixels": 0})),
            (
                "negative pixels",
                json!({"direction": "down", "pixels": -1}),
            ),
            // JSON has no non-finite number values, so reject their possible wire spellings.
            ("NaN pixels", json!({"direction": "down", "pixels": "NaN"})),
            (
                "infinite pixels",
                json!({"direction": "down", "pixels": "Infinity"}),
            ),
            (
                "negative infinite pixels",
                json!({"direction": "down", "pixels": "-Infinity"}),
            ),
            ("malformed ref", json!({"target": "e0"})),
            (
                "unknown field",
                json!({"direction": "down", "unexpected": true}),
            ),
        ] {
            assert!(parse(arguments).is_err(), "accepted invalid case: {case}");
        }
    }

    #[test]
    fn screenshot_tool_resolves_parses_empty_arguments_and_rejects_unknown_fields() {
        let screenshot = find_tool("browser_take_screenshot").expect("screenshot tool");
        let tool = descriptor(screenshot);

        assert!(screenshot.description.contains("server process shuts down"));
        assert_eq!(tool.input_schema["properties"], json!({}));
        assert_eq!(tool.input_schema["additionalProperties"], false);
        assert!(tool.input_schema.get("required").is_none());

        assert!(parse_op(screenshot, None).is_ok());
        assert!(parse_op(screenshot, Some(Map::new())).is_ok());

        let arguments = Map::from_iter([("unexpected".to_owned(), Value::Bool(true))]);
        let error = parse_op(screenshot, Some(arguments)).expect_err("reject unknown argument");
        assert!(error.contains("unknown field `unexpected`"), "{error}");
    }

    #[test]
    fn table_builds_all_seven_descriptors() {
        let tools: Vec<Tool> = TOOL_SPECS.iter().copied().map(descriptor).collect();
        assert_eq!(tools.len(), 7);
        let names: Vec<&str> = tools.iter().map(|tool| tool.name.as_ref()).collect();
        assert_eq!(
            names,
            [
                "browser_navigate",
                "browser_navigate_back",
                "browser_navigate_forward",
                "browser_snapshot",
                "browser_click",
                "browser_scroll",
                "browser_take_screenshot",
            ]
        );
        assert!(
            tools
                .iter()
                .all(|tool| tool.input_schema["type"] == "object")
        );
    }
}
