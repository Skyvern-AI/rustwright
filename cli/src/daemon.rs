use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

use crate::session::{BrowserAction, BrowserSession, LaunchConfig};

const STARTUP_TIMEOUT: Duration = Duration::from_secs(10);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(125);
const REQUEST_READ_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_REQUEST_BYTES: usize = 1024 * 1024;
const MAX_RESPONSE_BYTES: usize = 16 * 1024 * 1024;

#[derive(Debug, Deserialize, Serialize)]
struct DaemonState {
    pid: u32,
    port: u16,
    token: String,
}

#[derive(Debug, Deserialize, Serialize)]
struct DaemonRequest {
    token: String,
    #[serde(flatten)]
    action: BrowserAction,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct CommandResponse {
    pub success: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl CommandResponse {
    fn success(data: Value) -> Self {
        Self {
            success: true,
            data: Some(data),
            error: None,
        }
    }

    fn error(error: impl Into<String>) -> Self {
        Self {
            success: false,
            data: None,
            error: Some(error.into()),
        }
    }
}

pub struct DaemonConnection {
    state: DaemonState,
}

impl DaemonConnection {
    pub fn request(&self, action: BrowserAction) -> Result<CommandResponse> {
        send_request(&self.state, action)
    }
}

pub fn ensure_daemon(session: &str, launch: LaunchConfig) -> Result<DaemonConnection> {
    validate_session_name(session)?;
    let state_path = state_path(session)?;
    let state_dir = state_path
        .parent()
        .ok_or_else(|| anyhow!("invalid daemon state path"))?;
    fs::create_dir_all(state_dir).context("failed to create daemon state directory")?;
    secure_directory(state_dir)?;
    let startup_lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(state_dir.join(format!("{session}.lock")))
        .context("failed to open daemon startup lock")?;
    secure_file(&state_dir.join(format!("{session}.lock")))?;
    startup_lock
        .lock_exclusive()
        .context("failed to lock daemon startup")?;

    if let Ok(state) = read_state(&state_path) {
        if send_request(&state, BrowserAction::Ping).is_ok() {
            let launch_failed = send_request(&state, BrowserAction::Status)
                .ok()
                .and_then(|response| response.data)
                .and_then(|data| data.get("launch_failed").and_then(Value::as_bool))
                .unwrap_or(false);
            if !launch_failed {
                return Ok(DaemonConnection { state });
            }
            let _ = send_request(&state, BrowserAction::Close);
            remove_owned_state(&state_path, &state.token);
        }
        let _ = fs::remove_file(&state_path);
    }

    let token = Uuid::new_v4().simple().to_string();
    let current_exe = std::env::current_exe().context("failed to locate rustwright-cli")?;

    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(state_dir.join(format!("{session}.log")))
        .context("failed to open daemon log")?;
    let stderr = log.try_clone().context("failed to clone daemon log")?;

    let mut command = Command::new(current_exe);
    command
        .arg("__daemon")
        .arg("--session")
        .arg(session)
        .stdin(Stdio::piped())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(stderr));
    if launch.headed {
        command.arg("--headed");
    }
    if let Some(executable_path) = launch.executable_path {
        command.arg("--executable-path").arg(executable_path);
    }
    detach_process(&mut command);
    let mut child = command.spawn().context("failed to start browser daemon")?;
    let mut child_stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow!("browser daemon token channel is unavailable"))?;
    if let Err(error) = child_stdin.write_all(format!("{token}\n").as_bytes()) {
        cleanup_startup_child(&mut child, &state_path, &token);
        return Err(error).context("failed to initialize browser daemon");
    }
    drop(child_stdin);

    let deadline = Instant::now() + STARTUP_TIMEOUT;
    let mut last_error = None;
    while Instant::now() < deadline {
        if let Some(status) = child
            .try_wait()
            .context("failed to inspect browser daemon startup")?
        {
            cleanup_startup_child(&mut child, &state_path, &token);
            bail!("browser daemon exited before becoming ready: {status}");
        }
        if let Ok(state) = read_state(&state_path) {
            if state.token == token {
                match send_request(&state, BrowserAction::Ping) {
                    Ok(response) if response.success => return Ok(DaemonConnection { state }),
                    Ok(_) => last_error = Some("daemon rejected readiness check".to_string()),
                    Err(error) => last_error = Some(error.to_string()),
                }
            }
        }
        thread::sleep(Duration::from_millis(50));
    }

    cleanup_startup_child(&mut child, &state_path, &token);
    bail!(
        "browser daemon did not become ready within {} seconds{}",
        STARTUP_TIMEOUT.as_secs(),
        last_error
            .map(|error| format!(": {error}"))
            .unwrap_or_default()
    )
}

fn cleanup_startup_child(child: &mut Child, state_path: &Path, token: &str) {
    let temporary_state = state_path.with_extension(format!("{}.tmp", child.id()));
    let _ = child.kill();
    let _ = child.wait();
    remove_owned_state(state_path, token);
    let _ = fs::remove_file(temporary_state);
}

#[cfg(unix)]
fn detach_process(command: &mut Command) {
    use std::os::unix::process::CommandExt;

    // SAFETY: setsid is async-signal-safe and does not access memory shared with
    // other threads. It only starts the child in a new session before exec.
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
}

#[cfg(not(unix))]
fn detach_process(_command: &mut Command) {}

pub fn request_existing(session: &str, action: BrowserAction) -> Result<Option<CommandResponse>> {
    validate_session_name(session)?;
    let path = state_path(session)?;
    let Ok(state) = read_state(&path) else {
        return Ok(None);
    };
    match send_request(&state, action) {
        Ok(response) => Ok(Some(response)),
        Err(_) => {
            let _ = fs::remove_file(path);
            Ok(None)
        }
    }
}

pub fn run_daemon(session_name: &str, token: String, launch: LaunchConfig) -> Result<()> {
    validate_session_name(session_name)?;
    let path = state_path(session_name)?;
    let listener =
        TcpListener::bind(("127.0.0.1", 0)).context("failed to bind browser daemon socket")?;
    let port = listener.local_addr()?.port();
    let state = DaemonState {
        pid: std::process::id(),
        port,
        token: token.clone(),
    };
    write_state(&path, &state)?;

    let mut browser = BrowserSession::new(launch);
    for stream in listener.incoming() {
        let response_and_shutdown = match stream {
            Ok(stream) => handle_connection(stream, &token, &mut browser),
            Err(error) => Err(anyhow!(error).context("failed to accept daemon connection")),
        };
        match response_and_shutdown {
            Ok(true) => break,
            Ok(false) => {}
            Err(error) => eprintln!("rustwright daemon request failed: {error:#}"),
        }
    }

    let _ = browser.close();
    remove_owned_state(&path, &token);
    Ok(())
}

fn handle_connection(
    mut stream: TcpStream,
    expected_token: &str,
    browser: &mut BrowserSession,
) -> Result<bool> {
    stream.set_write_timeout(Some(REQUEST_TIMEOUT))?;
    let Some(request_line) = read_request_line(&mut stream)? else {
        write_response(&mut stream, &CommandResponse::error("request is too large"))?;
        return Ok(false);
    };
    let request: DaemonRequest = match serde_json::from_str(&request_line) {
        Ok(request) => request,
        Err(error) => {
            write_response(
                &mut stream,
                &CommandResponse::error(format!("invalid request: {error}")),
            )?;
            return Ok(false);
        }
    };
    if request.token != expected_token {
        write_response(&mut stream, &CommandResponse::error("unauthorized"))?;
        return Ok(false);
    }
    let shutdown = request.action.shuts_down_daemon();
    let response = match browser.execute(request.action) {
        Ok(value) => CommandResponse::success(value),
        Err(error) => CommandResponse::error(format!("{error:#}")),
    };
    write_response(&mut stream, &response)?;
    Ok(shutdown)
}

fn read_request_line(stream: &mut TcpStream) -> Result<Option<String>> {
    let deadline = Instant::now() + REQUEST_READ_TIMEOUT;
    let mut request = Vec::with_capacity(8 * 1024);
    let mut buffer = [0_u8; 8 * 1024];
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            bail!("request authentication timed out");
        }
        stream.set_read_timeout(Some(remaining))?;
        let bytes_read = stream.read(&mut buffer)?;
        if bytes_read == 0 {
            break;
        }
        let frame_end = buffer[..bytes_read]
            .iter()
            .position(|byte| *byte == b'\n')
            .map(|position| position + 1)
            .unwrap_or(bytes_read);
        if request.len() + frame_end > MAX_REQUEST_BYTES {
            return Ok(None);
        }
        request.extend_from_slice(&buffer[..frame_end]);
        if frame_end < bytes_read || request.last() == Some(&b'\n') {
            break;
        }
    }
    String::from_utf8(request)
        .map(Some)
        .context("request was not valid UTF-8")
}

fn send_request(state: &DaemonState, action: BrowserAction) -> Result<CommandResponse> {
    let mut stream = TcpStream::connect_timeout(
        &format!("127.0.0.1:{}", state.port).parse()?,
        Duration::from_secs(2),
    )
    .context("browser daemon is unavailable")?;
    stream.set_read_timeout(Some(REQUEST_TIMEOUT))?;
    stream.set_write_timeout(Some(REQUEST_TIMEOUT))?;
    let request = DaemonRequest {
        token: state.token.clone(),
        action,
    };
    serde_json::to_writer(&mut stream, &request)?;
    stream.write_all(b"\n")?;
    stream.flush()?;
    stream.shutdown(Shutdown::Write)?;

    let mut response = String::new();
    let mut reader = BufReader::new(stream).take((MAX_RESPONSE_BYTES + 1) as u64);
    let bytes_read = reader.read_line(&mut response)?;
    if bytes_read > MAX_RESPONSE_BYTES {
        bail!("browser daemon response exceeded {MAX_RESPONSE_BYTES} bytes");
    }
    if response.is_empty() {
        bail!("browser daemon returned an empty response");
    }
    serde_json::from_str(&response).context("browser daemon returned invalid JSON")
}

fn write_response(stream: &mut TcpStream, response: &CommandResponse) -> Result<()> {
    serde_json::to_writer(&mut *stream, response)?;
    stream.write_all(b"\n")?;
    stream.flush()?;
    Ok(())
}

fn read_state(path: &Path) -> Result<DaemonState> {
    let file = File::open(path)?;
    Ok(serde_json::from_reader(file)?)
}

fn write_state(path: &Path, state: &DaemonState) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("invalid daemon state path"))?;
    fs::create_dir_all(parent)?;
    secure_directory(parent)?;
    let temporary = path.with_extension(format!("{}.tmp", std::process::id()));
    let file = File::create(&temporary)?;
    serde_json::to_writer(file, state)?;
    secure_file(&temporary)?;
    fs::rename(temporary, path)?;
    Ok(())
}

fn remove_owned_state(path: &Path, token: &str) {
    if read_state(path)
        .map(|state| state.token == token)
        .unwrap_or(false)
    {
        let _ = fs::remove_file(path);
    }
}

fn state_path(session: &str) -> Result<PathBuf> {
    let directory = match std::env::var_os("RUSTWRIGHT_AGENT_STATE_DIR") {
        Some(path) => PathBuf::from(path),
        None => std::env::temp_dir().join(format!("rustwright-agent-{}", user_key())),
    };
    Ok(directory.join(format!("{session}.json")))
}

#[cfg(unix)]
fn user_key() -> String {
    // SAFETY: geteuid has no preconditions and does not dereference pointers.
    unsafe { libc::geteuid() }.to_string()
}

#[cfg(not(unix))]
fn user_key() -> String {
    let key = std::env::var("USER")
        .or_else(|_| std::env::var("USERNAME"))
        .unwrap_or_else(|_| "user".to_string())
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_'))
        .collect::<String>()
        .chars()
        .take(64)
        .collect::<String>();
    if key.is_empty() {
        "user".to_string()
    } else {
        key
    }
}

fn validate_session_name(session: &str) -> Result<()> {
    if session.is_empty()
        || session.len() > 64
        || !session
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_'))
    {
        bail!("session names may contain only letters, numbers, '-' and '_' (maximum 64)");
    }
    Ok(())
}

#[cfg(unix)]
fn secure_directory(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}

#[cfg(not(unix))]
fn secure_directory(_path: &Path) -> Result<()> {
    Ok(())
}

#[cfg(unix)]
fn secure_file(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

#[cfg(not(unix))]
fn secure_file(_path: &Path) -> Result<()> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::io::Read as _;
    use std::net::TcpListener;

    use super::*;

    #[test]
    fn session_names_reject_traversal_and_separators() {
        assert!(validate_session_name("default").is_ok());
        assert!(validate_session_name("agent_1-test").is_ok());
        assert!(validate_session_name("../escape").is_err());
        assert!(validate_session_name("nested/session").is_err());
        assert!(validate_session_name("").is_err());
    }

    #[test]
    fn command_response_omits_empty_fields() {
        let encoded = serde_json::to_value(CommandResponse::success(serde_json::json!({
            "ready": true
        })))
        .unwrap();
        assert_eq!(encoded.get("success"), Some(&Value::Bool(true)));
        assert!(encoded.get("error").is_none());
    }

    #[test]
    fn oversized_requests_are_rejected_before_parsing() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let client = thread::spawn(move || {
            let mut stream = TcpStream::connect(address).unwrap();
            stream
                .write_all(&vec![b'x'; MAX_REQUEST_BYTES + 1])
                .unwrap();
            stream.shutdown(Shutdown::Write).unwrap();
            let mut response = String::new();
            stream.read_to_string(&mut response).unwrap();
            response
        });
        let (stream, _) = listener.accept().unwrap();
        let mut browser = BrowserSession::new(LaunchConfig::default());
        assert!(!handle_connection(stream, "unused", &mut browser).unwrap());
        let response: CommandResponse = serde_json::from_str(&client.join().unwrap()).unwrap();
        assert!(!response.success);
        assert_eq!(response.error.as_deref(), Some("request is too large"));
    }
}
