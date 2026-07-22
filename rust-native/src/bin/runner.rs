//! Reference runner for the language-neutral Rustwright manifest contract.

use rustwright::{
    chromium, ActionOptions, Browser, GotoOptions, LaunchOptions, Page, ScreenshotOptions,
};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::HashSet;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Instant;

#[derive(Debug)]
struct Cli {
    manifest: PathBuf,
    _lib: PathBuf,
    out: PathBuf,
    cases: Option<Vec<String>>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    version: u32,
    cases: Vec<Case>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Case {
    id: String,
    #[allow(dead_code)]
    description: Option<String>,
    html: Option<String>,
    #[allow(dead_code)]
    url: Option<String>,
    steps: Vec<Step>,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "op")]
enum Step {
    #[serde(rename = "goto")]
    Goto {
        url: Option<String>,
        #[serde(rename = "useCaseHtml")]
        use_case_html: Option<bool>,
        #[serde(rename = "waitUntil")]
        wait_until: Option<String>,
    },
    #[serde(rename = "click")]
    Click { selector: String },
    #[serde(rename = "fill")]
    Fill { selector: String, value: String },
    #[serde(rename = "title")]
    Title { capture: String },
    #[serde(rename = "textContent")]
    TextContent { selector: String, capture: String },
    #[serde(rename = "evaluate")]
    Evaluate {
        expression: String,
        #[serde(default, deserialize_with = "deserialize_present_value")]
        arg: Option<Value>,
        capture: String,
    },
    #[serde(rename = "screenshot")]
    Screenshot { capture: String },
    #[serde(rename = "assertTitle")]
    AssertTitle {
        equals: Option<String>,
        contains: Option<String>,
    },
    #[serde(rename = "assertText")]
    AssertText {
        selector: String,
        equals: Option<String>,
        contains: Option<String>,
    },
    #[serde(rename = "assertEval")]
    AssertEval { expression: String, equals: Value },
}

#[derive(Debug, Serialize)]
struct Output {
    lang: &'static str,
    results: Vec<CaseResult>,
}

#[derive(Debug, Serialize)]
struct CaseResult {
    id: String,
    ok: bool,
    captures: Map<String, Value>,
    ms: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

fn main() -> ExitCode {
    match run() {
        Ok(all_ok) if all_ok => ExitCode::SUCCESS,
        Ok(_) => ExitCode::from(1),
        Err(error) => {
            eprintln!("runner: {error}");
            ExitCode::from(2)
        }
    }
}

fn run() -> Result<bool, String> {
    let cli = parse_cli()?;
    let bytes = fs::read(&cli.manifest)
        .map_err(|error| format!("cannot read {}: {error}", cli.manifest.display()))?;
    let raw: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("invalid manifest JSON: {error}"))?;
    validate_raw_step_fields(&raw)?;
    let manifest: Manifest =
        serde_json::from_value(raw).map_err(|error| format!("invalid manifest: {error}"))?;
    if manifest.version != 1 {
        return Err(format!(
            "unsupported manifest version {}; expected 1",
            manifest.version
        ));
    }
    validate_manifest(&manifest)?;
    let selected = select_cases(&manifest, cli.cases.as_deref())?;
    let results = match chromium().launch(LaunchOptions::default()) {
        Ok(browser) => run_cases(&browser, selected),
        Err(error) => selected
            .into_iter()
            .map(|case| CaseResult {
                id: case.id.clone(),
                ok: false,
                captures: Map::new(),
                ms: 0.0,
                error: Some(format!("browser launch failed: {error}")),
            })
            .collect(),
    };
    let output = Output {
        lang: "rust",
        results,
    };
    write_output(&cli.out, &output)?;
    Ok(output.results.iter().all(|result| result.ok))
}

fn deserialize_present_value<'de, D>(deserializer: D) -> Result<Option<Value>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    Value::deserialize(deserializer).map(Some)
}

/// Serde's internally-tagged `Step` enum silently ignores unknown fields, but
/// manifest v1 requires rejecting fields the schema does not allow (a typo like
/// `waitUntill` must be an error, not a no-op). Validate raw step objects
/// against the per-op field whitelist before the typed parse.
fn validate_raw_step_fields(raw: &Value) -> Result<(), String> {
    let Some(cases) = raw.get("cases").and_then(Value::as_array) else {
        return Ok(()); // shape errors surface via the typed parse
    };
    for (case_index, case) in cases.iter().enumerate() {
        let case_label = case
            .get("id")
            .and_then(Value::as_str)
            .map(|id| format!("case {id:?}"))
            .unwrap_or_else(|| format!("cases[{case_index}]"));
        let Some(steps) = case.get("steps").and_then(Value::as_array) else {
            continue;
        };
        for (step_index, step) in steps.iter().enumerate() {
            let Some(step_object) = step.as_object() else {
                return Err(format!("{case_label} step {step_index}: not an object"));
            };
            let Some(op) = step_object.get("op").and_then(Value::as_str) else {
                return Err(format!(
                    "{case_label} step {step_index}: missing string \"op\""
                ));
            };
            let allowed: &[&str] = match op {
                "goto" => &["op", "url", "useCaseHtml", "waitUntil"],
                "click" => &["op", "selector"],
                "fill" => &["op", "selector", "value"],
                "title" => &["op", "capture"],
                "textContent" => &["op", "selector", "capture"],
                "evaluate" => &["op", "expression", "arg", "capture"],
                "screenshot" => &["op", "capture"],
                "assertTitle" => &["op", "equals", "contains"],
                "assertText" => &["op", "selector", "equals", "contains"],
                "assertEval" => &["op", "expression", "equals"],
                unknown => {
                    return Err(format!(
                        "{case_label} step {step_index}: unknown op {unknown:?}"
                    ))
                }
            };
            for key in step_object.keys() {
                if !allowed.contains(&key.as_str()) {
                    return Err(format!(
                        "{case_label} step {step_index} (op {op:?}): unknown field {key:?}"
                    ));
                }
            }
        }
    }
    Ok(())
}

fn validate_manifest(manifest: &Manifest) -> Result<(), String> {
    if manifest.cases.is_empty() {
        return Err("manifest cases must not be empty".to_string());
    }
    let mut ids = HashSet::new();
    for case in &manifest.cases {
        if case.id.is_empty() {
            return Err("case id must not be empty".to_string());
        }
        if !ids.insert(case.id.as_str()) {
            return Err(format!("duplicate case id {:?}", case.id));
        }
        if case.steps.is_empty() {
            return Err(format!("case {:?} has no steps", case.id));
        }
    }
    Ok(())
}

fn parse_cli() -> Result<Cli, String> {
    let mut args = env::args().skip(1);
    let mut manifest = None;
    let mut library = None;
    let mut out = None;
    let mut cases = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--manifest" => manifest = Some(PathBuf::from(next_value(&mut args, "--manifest")?)),
            "--lib" => library = Some(PathBuf::from(next_value(&mut args, "--lib")?)),
            "--out" => out = Some(PathBuf::from(next_value(&mut args, "--out")?)),
            "--cases" => {
                let value = next_value(&mut args, "--cases")?;
                let ids = value
                    .split(',')
                    .map(str::trim)
                    .filter(|id| !id.is_empty())
                    .map(ToOwned::to_owned)
                    .collect::<Vec<_>>();
                if ids.is_empty() {
                    return Err("--cases must contain at least one case id".to_string());
                }
                cases = Some(ids);
            }
            "-h" | "--help" => {
                println!(
                    "Rustwright manifest runner\n\nUSAGE:\n    runner --manifest <path.json> --lib <shared-library> --out <results.json> [--cases id1,id2]\n\nThe Rust binding runs the engine in-process. --lib is required for CLI parity with other bindings but is accepted and ignored."
                );
                std::process::exit(0);
            }
            unknown => return Err(format!("unknown argument {unknown:?}; use --help")),
        }
    }
    Ok(Cli {
        manifest: manifest.ok_or_else(|| "missing --manifest".to_string())?,
        _lib: library.ok_or_else(|| "missing --lib".to_string())?,
        out: out.ok_or_else(|| "missing --out".to_string())?,
        cases,
    })
}

fn next_value(args: &mut impl Iterator<Item = String>, flag: &str) -> Result<String, String> {
    args.next()
        .ok_or_else(|| format!("missing value after {flag}"))
}

fn select_cases<'a>(
    manifest: &'a Manifest,
    requested: Option<&[String]>,
) -> Result<Vec<&'a Case>, String> {
    let Some(requested) = requested else {
        return Ok(manifest.cases.iter().collect());
    };
    let wanted: HashSet<&str> = requested.iter().map(String::as_str).collect();
    let available: HashSet<&str> = manifest.cases.iter().map(|case| case.id.as_str()).collect();
    let missing: Vec<&str> = wanted.difference(&available).copied().collect();
    if !missing.is_empty() {
        return Err(format!("unknown case id(s): {}", missing.join(", ")));
    }
    Ok(manifest
        .cases
        .iter()
        .filter(|case| wanted.contains(case.id.as_str()))
        .collect())
}

fn run_cases(browser: &Browser, cases: Vec<&Case>) -> Vec<CaseResult> {
    let mut results = Vec::with_capacity(cases.len());
    for case in cases {
        results.push(run_case(browser, case));
    }
    if let Err(error) = browser.close() {
        if let Some(last) = results.last_mut() {
            if last.ok {
                last.ok = false;
                last.error = Some(format!("browser close failed: {error}"));
            }
        } else {
            eprintln!("runner: browser close failed: {error}");
        }
    }
    results
}

fn run_case(browser: &Browser, case: &Case) -> CaseResult {
    let started = Instant::now();
    let mut captures = Map::new();
    let outcome = match browser.new_page() {
        Ok(page) => {
            let result = execute_steps(&page, case, &mut captures);
            match page.close(Default::default()) {
                Ok(()) => result,
                Err(error) if result.is_ok() => Err(format!("page close failed: {error}")),
                Err(_) => result,
            }
        }
        Err(error) => Err(format!("new page failed: {error}")),
    };
    CaseResult {
        id: case.id.clone(),
        ok: outcome.is_ok(),
        captures,
        ms: started.elapsed().as_secs_f64() * 1000.0,
        error: outcome.err(),
    }
}

fn execute_steps(
    page: &Page,
    case: &Case,
    captures: &mut Map<String, Value>,
) -> Result<(), String> {
    for (index, step) in case.steps.iter().enumerate() {
        execute_step(page, case, step, captures)
            .map_err(|error| format!("step {}: {error}", index + 1))?;
    }
    Ok(())
}

fn execute_step(
    page: &Page,
    case: &Case,
    step: &Step,
    captures: &mut Map<String, Value>,
) -> Result<(), String> {
    let action = ActionOptions::default();
    match step {
        Step::Goto {
            url,
            use_case_html,
            wait_until,
        } => {
            let target = match (url.as_deref(), use_case_html.unwrap_or(false)) {
                (Some(url), false) => url.to_string(),
                (None, true) => format!(
                    "data:text/html;charset=utf-8,{}",
                    percent_encode(case.html.as_deref().ok_or_else(|| {
                        "goto.useCaseHtml requires the case to define html".to_string()
                    })?)
                ),
                _ => return Err("goto requires exactly one of url or useCaseHtml:true".to_string()),
            };
            let options = GotoOptions {
                wait_until: wait_until.clone(),
                ..Default::default()
            };
            page.goto(&target, options)
                .map(|_| ())
                .map_err(|error| error.to_string())
        }
        Step::Click { selector } => page
            .click(selector, action)
            .map_err(|error| error.to_string()),
        Step::Fill { selector, value } => page
            .fill(selector, value, action)
            .map_err(|error| error.to_string()),
        Step::Title { capture } => {
            let value = page.title(action).map_err(|error| error.to_string())?;
            insert_capture(captures, capture, Value::String(value))
        }
        Step::TextContent { selector, capture } => {
            let value = page
                .text_content(selector, action)
                .map_err(|error| error.to_string())?
                .map(Value::String)
                .unwrap_or(Value::Null);
            insert_capture(captures, capture, value)
        }
        Step::Evaluate {
            expression,
            arg,
            capture,
        } => {
            let value = page
                .evaluate(expression, arg.as_ref(), action)
                .map_err(|error| error.to_string())?;
            insert_capture(captures, capture, value)
        }
        Step::Screenshot { capture } => {
            let bytes = page
                .screenshot(ScreenshotOptions::default())
                .map_err(|error| error.to_string())?;
            insert_capture(captures, capture, Value::from(bytes.len() as u64))
        }
        Step::AssertTitle { equals, contains } => {
            let actual = page.title(action).map_err(|error| error.to_string())?;
            assert_string("title", &actual, equals.as_deref(), contains.as_deref())
        }
        Step::AssertText {
            selector,
            equals,
            contains,
        } => {
            let actual = page
                .text_content(selector, action)
                .map_err(|error| error.to_string())?
                .ok_or_else(|| format!("textContent for {selector:?} was null"))?;
            assert_string(
                &format!("textContent for {selector:?}"),
                &actual,
                equals.as_deref(),
                contains.as_deref(),
            )
        }
        Step::AssertEval { expression, equals } => {
            let actual = page
                .evaluate(expression, None, action)
                .map_err(|error| error.to_string())?;
            if actual == *equals {
                Ok(())
            } else {
                Err(format!(
                    "evaluate mismatch: expected {}, got {}",
                    equals, actual
                ))
            }
        }
    }
}

fn insert_capture(
    captures: &mut Map<String, Value>,
    name: &str,
    value: Value,
) -> Result<(), String> {
    if captures.insert(name.to_string(), value).is_some() {
        Err(format!("duplicate capture name {name:?}"))
    } else {
        Ok(())
    }
}

fn assert_string(
    label: &str,
    actual: &str,
    equals: Option<&str>,
    contains: Option<&str>,
) -> Result<(), String> {
    match (equals, contains) {
        (Some(expected), None) if actual == expected => Ok(()),
        (Some(expected), None) => Err(format!(
            "{label} mismatch: expected {expected:?}, got {actual:?}"
        )),
        (None, Some(expected)) if actual.contains(expected) => Ok(()),
        (None, Some(expected)) => Err(format!(
            "{label} did not contain {expected:?}; got {actual:?}"
        )),
        _ => Err("assertion requires exactly one of equals or contains".to_string()),
    }
}

fn percent_encode(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

fn write_output(path: &Path, output: &Output) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("cannot create {}: {error}", parent.display()))?;
    }
    let json = serde_json::to_vec_pretty(output).map_err(|error| error.to_string())?;
    fs::write(path, json).map_err(|error| format!("cannot write {}: {error}", path.display()))
}
