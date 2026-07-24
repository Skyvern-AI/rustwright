use std::fs;
use std::process::{Command, Output};

use serde_json::Value;
use tempfile::TempDir;

#[test]
#[ignore = "requires a Chromium executable"]
fn daemon_persists_across_processes_and_serializes_startup() {
    let temporary = TempDir::new().unwrap();
    let state_dir = temporary.path().join("state");
    let caller_dir = temporary.path().join("caller");
    fs::create_dir_all(&caller_dir).unwrap();
    let session = "daemon-e2e";
    let url = "data:text/html,<title>Persistent</title><h1>Ready</h1>";

    let mut first = cli(&state_dir);
    first
        .args(["--session", session, "--json", "open", url])
        .current_dir(temporary.path());
    let mut second = cli(&state_dir);
    second
        .args(["--session", session, "--json", "open", url])
        .current_dir(temporary.path());
    let first = first.spawn().unwrap();
    let second = second.spawn().unwrap();
    assert_success(first.wait_with_output().unwrap());
    assert_success(second.wait_with_output().unwrap());

    let status = output(
        &state_dir,
        temporary.path(),
        &["--session", session, "--json", "status"],
    );
    let status: Value = serde_json::from_slice(&status.stdout).unwrap();
    assert_eq!(status["data"]["running"], true);
    assert_eq!(status["data"]["url"], url);

    let screenshot = output(
        &state_dir,
        &caller_dir,
        &["--session", session, "--json", "screenshot", "page.png"],
    );
    let screenshot: Value = serde_json::from_slice(&screenshot.stdout).unwrap();
    assert_eq!(
        screenshot["data"]["path"],
        caller_dir.join("page.png").to_string_lossy().as_ref()
    );
    assert!(caller_dir.join("page.png").is_file());
    assert!(!temporary.path().join("page.png").exists());

    output(
        &state_dir,
        temporary.path(),
        &["--session", session, "close"],
    );
}

#[test]
#[ignore = "requires a Chromium executable"]
fn failed_launch_configuration_can_be_retried() {
    let temporary = TempDir::new().unwrap();
    let state_dir = temporary.path().join("state");
    let session = "daemon-retry";
    let failed = cli(&state_dir)
        .args([
            "--session",
            session,
            "--json",
            "open",
            "--executable-path",
            "/missing/rustwright-chromium",
        ])
        .output()
        .unwrap();
    assert!(!failed.status.success());

    output(
        &state_dir,
        temporary.path(),
        &[
            "--session",
            session,
            "open",
            "data:text/html,<title>Retry worked</title>",
        ],
    );
    output(
        &state_dir,
        temporary.path(),
        &["--session", session, "close"],
    );
}

#[test]
fn json_mode_reports_local_validation_errors_as_json() {
    let temporary = TempDir::new().unwrap();
    let output = cli(&temporary.path().join("state"))
        .args(["--json", "wait", "120001"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    let response: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(response["success"], false);
    assert!(response["error"]
        .as_str()
        .unwrap()
        .contains("must not exceed"));
}

fn cli(state_dir: &std::path::Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_rustwright-cli"));
    command.env("RUSTWRIGHT_AGENT_STATE_DIR", state_dir);
    command
}

fn output(state_dir: &std::path::Path, cwd: &std::path::Path, args: &[&str]) -> Output {
    let output = cli(state_dir).args(args).current_dir(cwd).output().unwrap();
    assert_success(output)
}

fn assert_success(output: Output) -> Output {
    assert!(
        output.status.success(),
        "command failed: stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    output
}
