use std::{
    collections::{HashMap, HashSet, VecDeque},
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::PathBuf,
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use rustwright::{ActionOptions, GotoOptions, LaunchOptions, chromium};
use serde_json::{Value, json};

const REMOTE_UNREACHABLE: &str = "remote CDP session unreachable — restart or reconfigure";

const PAGE_HTML: &str = r#"<!doctype html>
<html><head><title>MCP test page</title></head>
<body>
  <label for="name">Test input</label>
  <input id="name" value="sample value">
  <input aria-label="Secret input" type="password" value="do-not-render">
  <button onclick="this.textContent='Clicked button'; document.getElementById('status').textContent='Clicked successfully'">Activate feature</button>
  <div id="status">Waiting</div>
</body></html>"#;

const HISTORY_PAGE_A_HTML: &str = r#"<!doctype html>
<html><head><title>History page A</title></head>
<body><main><h1>History page A</h1></main></body></html>"#;

const HISTORY_PAGE_B_HTML: &str = r#"<!doctype html>
<html><head><title>History page B</title></head>
<body><main><h1>History page B</h1></main></body></html>"#;

const SPA_HISTORY_PAGE_HTML: &str = r#"<!doctype html>
<html><head><title>SPA history</title></head>
<body><main><h1>SPA history</h1><div id="history-readout" role="status"></div></main>
<script>
  const readout = document.getElementById('history-readout');
  const render = () => {
    readout.textContent = `SPA location: ${location.pathname}${location.search}`;
  };
  window.addEventListener('popstate', render);
  history.replaceState({ step: 0 }, '', '/history-spa?step=zero');
  history.pushState({ step: 1 }, '', '/history-spa?step=one');
  history.pushState({ step: 2 }, '', '/history-spa?step=two');
  render();
</script></body></html>"#;

const HASH_HISTORY_PAGE_HTML: &str = r#"<!doctype html>
<html><head><title>Hash history</title></head>
<body><main><h1>Hash history</h1><div id="hash-readout" role="status"></div></main>
<script>
  const readout = document.getElementById('hash-readout');
  const render = () => { readout.textContent = `Hash location: ${location.hash}`; };
  window.addEventListener('popstate', render);
  window.addEventListener('hashchange', render);
  render();
</script></body></html>"#;

const SCROLL_PAGE_HTML: &str = r#"<!doctype html>
<html><head><title>Scroll test page</title>
<style>
  html, body { margin: 0; min-height: 7000px; }
  #scroll-readout {
    position: fixed;
    top: 0;
    left: 0;
    z-index: 10;
    background: white;
  }
  #far-target { display: block; margin-top: 5000px; }
</style></head>
<body>
  <div id="scroll-readout" role="status">Scroll Y: 0</div>
  <button id="far-target">Far below fold target</button>
  <script>
    const readout = document.getElementById('scroll-readout');
    const updateReadout = () => {
      readout.textContent = `Scroll Y: ${Math.round(window.scrollY)}`;
    };
    window.addEventListener('scroll', updateReadout, { passive: true });
    updateReadout();
  </script>
</body></html>"#;

const BACKGROUND_SCROLL_PAGE_HTML: &str = r#"<!doctype html>
<html><head><title>Background scroll test page</title>
<style>
  html, body { margin: 0; min-height: 7000px; }
  #readouts { position: fixed; top: 0; left: 0; z-index: 10; background: white; }
</style></head>
<body>
  <div id="readouts">
    <div id="scroll-readout" role="status">Scroll Y: 0</div>
    <div id="visibility-readout" role="status">Visibility: visible</div>
  </div>
  <script>
    const scrollReadout = document.getElementById('scroll-readout');
    const visibilityReadout = document.getElementById('visibility-readout');
    const updateScroll = () => {
      scrollReadout.textContent = `Scroll Y: ${Math.round(window.scrollY)}`;
    };
    const updateVisibility = () => {
      visibilityReadout.textContent = `Visibility: ${document.visibilityState}`;
    };
    window.addEventListener('scroll', updateScroll, { passive: true });
    document.addEventListener('visibilitychange', updateVisibility);
    updateScroll();
    updateVisibility();
  </script>
</body></html>"#;

struct PageServer {
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

struct HangingServer {
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

struct AttachProbe {
    addr: SocketAddr,
    attempts: Arc<AtomicUsize>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl AttachProbe {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind attach probe");
        listener
            .set_nonblocking(true)
            .expect("set attach probe nonblocking");
        let addr = listener.local_addr().expect("attach probe address");
        let attempts = Arc::new(AtomicUsize::new(0));
        let thread_attempts = Arc::clone(&attempts);
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !thread_stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((_stream, _)) => {
                        thread_attempts.fetch_add(1, Ordering::Relaxed);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(error) => panic!("attach probe accept failed: {error}"),
                }
            }
        });
        Self {
            addr,
            attempts,
            stop,
            thread: Some(thread),
        }
    }

    fn endpoint(&self) -> String {
        format!("http://{}", self.addr)
    }

    fn attempts(&self) -> usize {
        self.attempts.load(Ordering::Relaxed)
    }
}

impl Drop for AttachProbe {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let _ = TcpStream::connect(self.addr);
        if let Some(thread) = self.thread.take() {
            thread.join().expect("join attach probe");
        }
    }
}

impl HangingServer {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind hanging endpoint");
        listener
            .set_nonblocking(true)
            .expect("set hanging endpoint nonblocking");
        let addr = listener.local_addr().expect("hanging endpoint address");
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            let mut connections = Vec::new();
            while !thread_stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((stream, _)) => connections.push(stream),
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(error) => panic!("hanging endpoint accept failed: {error}"),
                }
            }
        });
        Self {
            addr,
            stop,
            thread: Some(thread),
        }
    }

    fn url(&self) -> String {
        format!("http://{}/never-responds", self.addr)
    }
}

impl Drop for HangingServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let _ = TcpStream::connect(self.addr);
        if let Some(thread) = self.thread.take() {
            thread.join().expect("join hanging endpoint");
        }
    }
}

impl PageServer {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind local page server");
        listener
            .set_nonblocking(true)
            .expect("set local page server nonblocking");
        let addr = listener.local_addr().expect("local page address");
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !thread_stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((stream, _)) => serve_connection(stream),
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("local page accept failed: {error}"),
                }
            }
        });
        Self {
            addr,
            stop,
            thread: Some(thread),
        }
    }

    fn url(&self) -> String {
        format!("http://{}/slow", self.addr)
    }

    fn history_url(&self, page: &str) -> String {
        format!("http://{}/history-{page}", self.addr)
    }

    fn scroll_url(&self) -> String {
        format!("http://{}/scroll", self.addr)
    }

    fn spa_history_url(&self) -> String {
        format!("http://{}/history-spa", self.addr)
    }

    fn hash_history_url(&self) -> String {
        format!("http://{}/history-hash", self.addr)
    }

    fn background_scroll_url(&self) -> String {
        format!("http://{}/background-scroll", self.addr)
    }
}

impl Drop for PageServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let _ = TcpStream::connect(self.addr);
        if let Some(thread) = self.thread.take() {
            thread.join().expect("local page server thread");
        }
    }
}

fn serve_connection(mut stream: TcpStream) {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("set request read timeout");
    let mut request = [0_u8; 2048];
    let read = stream.read(&mut request).unwrap_or(0);
    let request = String::from_utf8_lossy(&request[..read]);
    if request.starts_with("GET /slow ") {
        thread::sleep(Duration::from_millis(450));
    }
    let body = if request.starts_with("GET /history-a ") {
        HISTORY_PAGE_A_HTML
    } else if request.starts_with("GET /history-b ") {
        HISTORY_PAGE_B_HTML
    } else if request.starts_with("GET /history-spa ") {
        SPA_HISTORY_PAGE_HTML
    } else if request.starts_with("GET /history-hash ") {
        HASH_HISTORY_PAGE_HTML
    } else if request.starts_with("GET /background-scroll ") {
        BACKGROUND_SCROLL_PAGE_HTML
    } else if request.starts_with("GET /scroll ") {
        SCROLL_PAGE_HTML
    } else {
        PAGE_HTML
    };
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    stream
        .write_all(response.as_bytes())
        .expect("write test page");
}

struct ServerProcess {
    child: Child,
    input: Option<ChildStdin>,
    output: BufReader<ChildStdout>,
    transcript: Vec<String>,
}

impl ServerProcess {
    fn spawn() -> Self {
        Self::spawn_with_options(None, &[])
    }

    fn spawn_with_env(environment: &[(&str, &str)]) -> Self {
        Self::spawn_with_options(None, environment)
    }

    fn spawn_remote(endpoint: &str, headers: &Value, timeout_ms: u64) -> Self {
        Self::spawn_with_options(Some((endpoint, headers, timeout_ms)), &[])
    }

    fn spawn_with_options(
        remote: Option<(&str, &Value, u64)>,
        environment: &[(&str, &str)],
    ) -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-rs"));
        command
            .env_remove("RUSTWRIGHT_MCP_CDP_ENDPOINT")
            .env_remove("RUSTWRIGHT_MCP_CDP_HEADERS")
            .env_remove("RUSTWRIGHT_MCP_CDP_TIMEOUT_MS")
            .env_remove("RUSTWRIGHT_MCP_SCREENSHOT_MAX_BYTES")
            .env_remove("RUSTWRIGHT_MCP_TOOL_TIMEOUT_MS")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some((endpoint, headers, timeout_ms)) = remote {
            command
                .env("RUSTWRIGHT_MCP_CDP_ENDPOINT", endpoint)
                .env("RUSTWRIGHT_MCP_CDP_HEADERS", headers.to_string())
                .env("RUSTWRIGHT_MCP_CDP_TIMEOUT_MS", timeout_ms.to_string());
        }
        for (name, value) in environment {
            command.env(name, value);
        }
        let mut child = command.spawn().expect("spawn MCP server");
        let input = child.stdin.take().expect("server stdin");
        let output = BufReader::new(child.stdout.take().expect("server stdout"));
        Self {
            child,
            input: Some(input),
            output,
            transcript: Vec::new(),
        }
    }

    fn send(&mut self, message: Value) {
        self.send_raw(&serde_json::to_string(&message).expect("serialize client frame"));
    }

    fn send_raw(&mut self, line: &str) {
        self.transcript.push(format!("C> {line}"));
        let input = self.input.as_mut().expect("server input is open");
        writeln!(input, "{line}").expect("send client frame");
        input.flush().expect("flush client frame");
    }

    fn receive(&mut self) -> Value {
        let mut line = String::new();
        let bytes = self.output.read_line(&mut line).expect("read server frame");
        assert!(bytes > 0, "server stdout closed before a response");
        let trimmed = line.trim_end();
        self.transcript.push(format!("S> {trimmed}"));
        let message: Value = serde_json::from_str(trimmed).unwrap_or_else(|error| {
            panic!("stdout contained a non-JSON protocol line: {error}: {trimmed:?}")
        });
        assert_eq!(message["jsonrpc"], "2.0");
        message
    }

    fn initialize(&mut self) -> Value {
        self.send(json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "stdio-e2e", "version": "0"}
            }
        }));
        let initialized = self.receive();
        assert_eq!(initialized["id"], 1);
        assert!(initialized["result"]["capabilities"]["tools"].is_object());
        self.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
        initialized
    }

    fn finish(mut self) -> (Vec<String>, String) {
        self.input.take();
        wait_for_exit(&mut self.child, Duration::from_secs(15));

        let mut remaining_stdout = String::new();
        self.output
            .read_to_string(&mut remaining_stdout)
            .expect("read remaining server stdout");
        for line in remaining_stdout
            .lines()
            .filter(|line| !line.trim().is_empty())
        {
            let message: Value = serde_json::from_str(line).unwrap_or_else(|error| {
                panic!("stdout contained a non-JSON trailing line: {error}: {line:?}")
            });
            assert_eq!(message["jsonrpc"], "2.0");
            self.transcript.push(format!("S> {line}"));
        }

        let mut diagnostics = String::new();
        self.child
            .stderr
            .take()
            .expect("server stderr")
            .read_to_string(&mut diagnostics)
            .expect("read server diagnostics");
        (std::mem::take(&mut self.transcript), diagnostics)
    }
}

struct VersionStub {
    endpoint: String,
    request: mpsc::Receiver<String>,
    thread: Option<thread::JoinHandle<()>>,
}

impl VersionStub {
    fn start(ws_endpoint: String) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind version stub");
        let addr = listener.local_addr().expect("version stub address");
        let (request_tx, request) = mpsc::sync_channel(1);
        let thread = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept version request");
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("version request timeout");
            let mut bytes = Vec::new();
            let mut buffer = [0_u8; 1024];
            loop {
                let read = stream.read(&mut buffer).expect("read version request");
                if read == 0 {
                    break;
                }
                bytes.extend_from_slice(&buffer[..read]);
                if bytes.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let request = String::from_utf8(bytes).expect("ASCII HTTP request");
            request_tx.send(request).expect("record version request");
            let body = json!({"webSocketDebuggerUrl": ws_endpoint}).to_string();
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream
                .write_all(response.as_bytes())
                .expect("write version response");
        });
        Self {
            endpoint: format!("http://{addr}"),
            request,
            thread: Some(thread),
        }
    }

    fn finish(mut self) -> String {
        let request = self
            .request
            .recv_timeout(Duration::from_secs(10))
            .expect("recorded version request");
        self.thread
            .take()
            .expect("version stub thread")
            .join()
            .expect("version stub join");
        request
    }
}

impl Drop for ServerProcess {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

fn result_text(message: &Value) -> &str {
    assert_eq!(
        message["result"]["isError"], false,
        "expected successful tool response: {message}"
    );
    message["result"]["content"][0]["text"]
        .as_str()
        .expect("tool response text")
}

fn error_result_text(message: &Value) -> &str {
    assert_eq!(message["result"]["isError"], true);
    message["result"]["content"][0]["text"]
        .as_str()
        .expect("tool error response text")
}

fn png_path_from_fallback(text: &str) -> PathBuf {
    text.split_whitespace()
        .map(|candidate| {
            candidate.trim_matches(|character: char| {
                matches!(character, '`' | '\'' | '"' | '(' | ')' | ',' | '.')
            })
        })
        .map(PathBuf::from)
        .find(|path| {
            path.is_absolute()
                && path
                    .extension()
                    .and_then(|extension| extension.to_str())
                    .is_some_and(|extension| extension.eq_ignore_ascii_case("png"))
        })
        .unwrap_or_else(|| panic!("fallback did not contain an absolute PNG path: {text}"))
}

fn button_ref(snapshot: &str, name: &str) -> String {
    let line = snapshot
        .lines()
        .find(|line| line.contains("- button") && line.contains(name))
        .unwrap_or_else(|| panic!("button {name:?} missing from snapshot:\n{snapshot}"));
    let marker = "[ref=";
    let start = line.find(marker).expect("button ref start") + marker.len();
    let end = line[start..].find(']').expect("button ref end") + start;
    line[start..end].to_owned()
}

fn scroll_y(snapshot: &str) -> u64 {
    let marker = "Scroll Y: ";
    let suffix = snapshot
        .lines()
        .find_map(|line| line.split_once(marker).map(|(_, suffix)| suffix))
        .unwrap_or_else(|| panic!("scroll readout missing from snapshot:\n{snapshot}"));
    let digits: String = suffix
        .chars()
        .take_while(|character| character.is_ascii_digit())
        .collect();
    assert!(!digits.is_empty(), "invalid scroll readout: {suffix:?}");
    digits.parse().expect("numeric scroll readout")
}

fn snapshot_ref_numbers(snapshot: &str) -> Vec<u64> {
    let mut refs: Vec<u64> = snapshot
        .split("[ref=")
        .skip(1)
        .filter_map(|suffix| suffix.split(']').next())
        .map(|reference| {
            reference
                .strip_prefix('e')
                .expect("snapshot ref prefix")
                .parse()
                .expect("numeric snapshot ref")
        })
        .collect();
    refs.sort_unstable();
    refs
}

fn assert_refs_strictly_increase(snapshots: &[&str]) {
    let mut previous = 0;
    let mut seen = HashSet::new();
    for snapshot in snapshots {
        for reference in snapshot_ref_numbers(snapshot) {
            assert!(reference > previous, "refs must increase across snapshots");
            assert!(seen.insert(reference), "ref e{reference} was reused");
            previous = reference;
        }
    }
}

fn assert_password_is_masked(snapshot: &str) {
    assert!(snapshot.contains("[value=••••••]"));
    assert!(!snapshot.contains("do-not-render"));
}

fn process_rows() -> Vec<(u32, u32, String)> {
    let output = Command::new("ps")
        .args(["-axo", "pid=,ppid=,comm="])
        .output()
        .expect("run ps");
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| {
            let mut fields = line.split_whitespace();
            let pid = fields.next()?.parse().ok()?;
            let ppid = fields.next()?.parse().ok()?;
            let command = fields.collect::<Vec<_>>().join(" ");
            Some((pid, ppid, command))
        })
        .collect()
}

fn descendants(root: u32) -> Vec<(u32, String)> {
    let rows = process_rows();
    let mut by_parent: HashMap<u32, Vec<(u32, String)>> = HashMap::new();
    for (pid, ppid, command) in rows {
        by_parent.entry(ppid).or_default().push((pid, command));
    }
    let mut queue = VecDeque::from([root]);
    let mut found = Vec::new();
    while let Some(parent) = queue.pop_front() {
        if let Some(children) = by_parent.get(&parent) {
            for (pid, command) in children {
                found.push((*pid, command.clone()));
                queue.push_back(*pid);
            }
        }
    }
    found
}

fn wait_for_exit(child: &mut Child, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(status) = child.try_wait().expect("poll server process") {
            assert!(status.success(), "server exited unsuccessfully: {status}");
            return;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            panic!("server did not exit after stdin EOF");
        }
        thread::sleep(Duration::from_millis(25));
    }
}

#[test]
fn real_stdio_snapshot_click_monotonic_refs_and_clean_shutdown() {
    let page_server = PageServer::start();
    let mut server = ServerProcess::spawn();
    server.initialize();

    server.send(json!({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}));
    let listed = server.receive();
    let tools = listed["result"]["tools"].as_array().expect("tools array");
    assert_eq!(tools.len(), 7);
    assert_eq!(
        tools
            .iter()
            .map(|tool| tool["name"].as_str().expect("tool name"))
            .collect::<Vec<_>>(),
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
    assert_eq!(tools[0]["inputSchema"]["required"], json!(["url"]));
    assert_eq!(
        tools[0]["inputSchema"]["properties"]["url"]["type"],
        "string"
    );
    assert_eq!(tools[1]["inputSchema"]["properties"], json!({}));
    assert_eq!(tools[2]["inputSchema"]["properties"], json!({}));
    assert_eq!(tools[3]["inputSchema"]["properties"], json!({}));
    assert_eq!(
        tools[4]["inputSchema"]["properties"]["target"]["pattern"],
        "^e[1-9][0-9]*$"
    );
    assert_eq!(
        tools[5]["inputSchema"]["properties"]["direction"]["enum"],
        json!(["up", "down"])
    );
    assert_eq!(tools[6]["inputSchema"]["properties"], json!({}));
    assert!(
        tools
            .iter()
            .all(|tool| tool["inputSchema"]["additionalProperties"] == false)
    );

    server.send(json!({
        "jsonrpc":"2.0","id":3,"method":"tools/call",
        "params":{"name":"browser_navigate","arguments":{"url":page_server.url()}}
    }));
    thread::sleep(Duration::from_millis(40));
    server.send(json!({
        "jsonrpc":"2.0","id":4,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    let first = server.receive();
    let second = server.receive();
    let responses = HashMap::from([
        (first["id"].as_i64().expect("numeric response id"), first),
        (second["id"].as_i64().expect("numeric response id"), second),
    ]);
    let navigate_text = result_text(&responses[&3]).to_owned();
    assert!(navigate_text.contains("Activate feature"));
    assert!(navigate_text.contains("[ref=e"));
    assert_password_is_masked(&navigate_text);

    let snapshot_text = result_text(&responses[&4]).to_owned();
    assert!(snapshot_text.contains("Activate feature"));
    assert_password_is_masked(&snapshot_text);

    let stale_target = button_ref(&navigate_text, "Activate feature");
    server.send(json!({
        "jsonrpc":"2.0","id":5,"method":"tools/call",
        "params":{"name":"browser_click","arguments":{"target":stale_target}}
    }));
    let stale = server.receive();
    assert!(error_result_text(&stale).contains("unknown or stale ref"));

    let target = button_ref(&snapshot_text, "Activate feature");
    server.send(json!({
        "jsonrpc":"2.0","id":6,"method":"tools/call",
        "params":{"name":"browser_click","arguments":{"target":target}}
    }));
    let clicked = server.receive();
    let clicked_text = result_text(&clicked).to_owned();
    assert!(clicked_text.contains("Clicked button"));
    assert!(clicked_text.contains("Clicked successfully"));
    assert_password_is_masked(&clicked_text);
    assert_refs_strictly_increase(&[&navigate_text, &snapshot_text, &clicked_text]);

    let browser_processes = descendants(server.child.id());
    assert!(
        !browser_processes.is_empty(),
        "expected browser subprocesses before shutdown"
    );
    let browser_pids: Vec<u32> = browser_processes.iter().map(|(pid, _)| *pid).collect();
    let (transcript, diagnostics) = server.finish();

    let live_pids: HashSet<u32> = process_rows().into_iter().map(|(pid, _, _)| pid).collect();
    let orphans: Vec<u32> = browser_pids
        .iter()
        .copied()
        .filter(|pid| live_pids.contains(pid))
        .collect();
    assert!(orphans.is_empty(), "orphan browser processes: {orphans:?}");
    assert!(diagnostics.contains("browser actor: stopped"));

    println!("--- stdio e2e transcript ---");
    for line in transcript {
        println!("{line}");
    }
    println!("--- shutdown evidence ---");
    println!("captured browser descendants: {browser_pids:?}");
    println!("orphan browser descendants after exit: {orphans:?}");
}

#[test]
fn real_stdio_history_navigation_returns_snapshots_and_errors_at_forward_boundary() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping history navigation MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let mut server = ServerProcess::spawn();
    server.initialize();

    server.send(json!({
        "jsonrpc":"2.0","id":21,"method":"tools/call",
        "params":{
            "name":"browser_navigate",
            "arguments":{"url":page_server.history_url("a")}
        }
    }));
    let page_a = server.receive();
    assert!(result_text(&page_a).contains("History page A"));

    server.send(json!({
        "jsonrpc":"2.0","id":22,"method":"tools/call",
        "params":{
            "name":"browser_navigate",
            "arguments":{"url":page_server.history_url("b")}
        }
    }));
    let page_b = server.receive();
    assert!(result_text(&page_b).contains("History page B"));

    server.send(json!({
        "jsonrpc":"2.0","id":23,"method":"tools/call",
        "params":{"name":"browser_navigate_back","arguments":{}}
    }));
    let back = server.receive();
    let back_snapshot = result_text(&back);
    assert!(back_snapshot.contains("History page A"), "{back_snapshot}");
    assert!(!back_snapshot.contains("History page B"), "{back_snapshot}");

    server.send(json!({
        "jsonrpc":"2.0","id":24,"method":"tools/call",
        "params":{"name":"browser_navigate_forward","arguments":{}}
    }));
    let forward = server.receive();
    let forward_snapshot = result_text(&forward);
    assert!(
        forward_snapshot.contains("History page B"),
        "{forward_snapshot}"
    );
    assert!(
        !forward_snapshot.contains("History page A"),
        "{forward_snapshot}"
    );

    server.send(json!({
        "jsonrpc":"2.0","id":25,"method":"tools/call",
        "params":{"name":"browser_navigate_forward","arguments":{}}
    }));
    let no_forward_history = server.receive();
    assert!(
        error_result_text(&no_forward_history).contains("no forward history"),
        "{}",
        error_result_text(&no_forward_history)
    );

    server.finish();
}

#[test]
fn real_stdio_same_document_push_state_and_hash_history_complete_with_snapshots() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping same-document history MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let mut server = ServerProcess::spawn_with_env(&[("RUSTWRIGHT_MCP_TOOL_TIMEOUT_MS", "3000")]);
    server.initialize();

    server.send(json!({
        "jsonrpc":"2.0","id":26,"method":"tools/call",
        "params":{
            "name":"browser_navigate",
            "arguments":{"url":page_server.spa_history_url()}
        }
    }));
    let spa_second = server.receive();
    assert!(
        result_text(&spa_second).contains("SPA location: /history-spa?step=two"),
        "{}",
        result_text(&spa_second)
    );

    let started = Instant::now();
    server.send(json!({
        "jsonrpc":"2.0","id":27,"method":"tools/call",
        "params":{"name":"browser_navigate_back","arguments":{}}
    }));
    let spa_back = server.receive();
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "pushState back navigation approached the tool timeout: {:?}",
        started.elapsed()
    );
    assert!(
        result_text(&spa_back).contains("SPA location: /history-spa?step=one"),
        "{}",
        result_text(&spa_back)
    );

    server.send(json!({
        "jsonrpc":"2.0","id":28,"method":"tools/call",
        "params":{"name":"browser_navigate_forward","arguments":{}}
    }));
    let spa_forward = server.receive();
    assert!(
        result_text(&spa_forward).contains("SPA location: /history-spa?step=two"),
        "{}",
        result_text(&spa_forward)
    );

    server.send(json!({
        "jsonrpc":"2.0","id":29,"method":"tools/call",
        "params":{
            "name":"browser_navigate",
            "arguments":{"url":page_server.hash_history_url()}
        }
    }));
    let hash_initial = server.receive();
    assert!(
        result_text(&hash_initial).contains("Hash location:"),
        "{hash_initial}"
    );

    server.send(json!({
        "jsonrpc":"2.0","id":30,"method":"tools/call",
        "params":{
            "name":"browser_navigate",
            "arguments":{"url":format!("{}#first", page_server.hash_history_url())}
        }
    }));
    let hash_first = server.receive();
    assert!(
        result_text(&hash_first).contains("Hash location: #first"),
        "{}",
        result_text(&hash_first)
    );

    server.send(json!({
        "jsonrpc":"2.0","id":31,"method":"tools/call",
        "params":{
            "name":"browser_navigate",
            "arguments":{"url":format!("{}#second", page_server.hash_history_url())}
        }
    }));
    let hash_second = server.receive();
    assert!(
        result_text(&hash_second).contains("Hash location: #second"),
        "{}",
        result_text(&hash_second)
    );

    server.send(json!({
        "jsonrpc":"2.0","id":32,"method":"tools/call",
        "params":{"name":"browser_navigate_back","arguments":{}}
    }));
    let hash_back = server.receive();
    assert!(
        result_text(&hash_back).contains("Hash location: #first"),
        "{}",
        result_text(&hash_back)
    );

    server.send(json!({
        "jsonrpc":"2.0","id":33,"method":"tools/call",
        "params":{"name":"browser_navigate_forward","arguments":{}}
    }));
    let hash_forward = server.receive();
    assert!(
        result_text(&hash_forward).contains("Hash location: #second"),
        "{}",
        result_text(&hash_forward)
    );

    server.finish();
}

#[test]
fn real_stdio_scrolls_viewport_and_target_returns_snapshots_and_invalidates_refs() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping scroll MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let mut server = ServerProcess::spawn();
    server.initialize();

    server.send(json!({
        "jsonrpc":"2.0","id":31,"method":"tools/call",
        "params":{
            "name":"browser_navigate",
            "arguments":{"url":page_server.scroll_url()}
        }
    }));
    let initial = server.receive();
    let initial_snapshot = result_text(&initial).to_owned();
    assert!(initial_snapshot.contains("Far below fold target"));
    assert!(scroll_y(&initial_snapshot) <= 1, "{initial_snapshot}");

    server.send(json!({
        "jsonrpc":"2.0","id":32,"method":"tools/call",
        "params":{
            "name":"browser_scroll",
            "arguments":{"direction":"down","pixels":600}
        }
    }));
    let down = server.receive();
    let down_snapshot = result_text(&down).to_owned();
    let down_y = scroll_y(&down_snapshot);
    assert!(
        down_y.abs_diff(600) <= 75,
        "expected scroll position near 600, got {down_y}:\n{down_snapshot}"
    );
    let far_target = button_ref(&down_snapshot, "Far below fold target");

    server.send(json!({
        "jsonrpc":"2.0","id":33,"method":"tools/call",
        "params":{
            "name":"browser_scroll",
            "arguments":{"target":far_target}
        }
    }));
    let target = server.receive();
    let target_snapshot = result_text(&target).to_owned();
    let target_y = scroll_y(&target_snapshot);
    assert!(
        target_y >= 4000,
        "expected far target to scroll beyond 4000, got {target_y}:\n{target_snapshot}"
    );

    server.send(json!({
        "jsonrpc":"2.0","id":34,"method":"tools/call",
        "params":{
            "name":"browser_scroll",
            "arguments":{"direction":"up"}
        }
    }));
    let up = server.receive();
    let up_snapshot = result_text(&up).to_owned();
    let up_y = scroll_y(&up_snapshot);
    assert!(
        up_y < target_y,
        "default upward scroll did not decrease {target_y}:\n{up_snapshot}"
    );

    server.send(json!({
        "jsonrpc":"2.0","id":35,"method":"tools/call",
        "params":{
            "name":"browser_scroll",
            "arguments":{"target":far_target}
        }
    }));
    let stale = server.receive();
    assert!(
        error_result_text(&stale).contains("unknown or stale ref"),
        "{}",
        error_result_text(&stale)
    );

    assert_refs_strictly_increase(&[
        &initial_snapshot,
        &down_snapshot,
        &target_snapshot,
        &up_snapshot,
    ]);
    server.finish();
}

#[test]
fn real_stdio_viewport_scroll_completes_when_hidden_page_has_no_animation_frames() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping background viewport scroll MCP test: Chromium unavailable");
        return;
    }

    let page_server = PageServer::start();
    let owner = chromium()
        .launch(LaunchOptions::default().arg("--remote-debugging-port=0"))
        .expect("launch remote background-scroll browser");
    let controlled = owner
        .new_page()
        .expect("create remote background-scroll page");
    controlled
        .goto(
            &page_server.background_scroll_url(),
            GotoOptions::default().wait_until("load").timeout(10_000.0),
        )
        .expect("navigate remote background-scroll page");
    for page in owner.pages().expect("list remote background-scroll pages") {
        if page.target_id() != controlled.target_id() {
            page.close(Default::default())
                .expect("close remote startup page");
        }
    }
    let visibility = controlled
        .evaluate(
            r#"() => {
              Object.defineProperty(document, 'visibilityState', {
                configurable: true,
                get: () => 'hidden',
              });
              globalThis.requestAnimationFrame = () => 1;
              document.getElementById('visibility-readout').textContent =
                `Visibility: ${document.visibilityState}`;
              return document.visibilityState;
            }"#,
            None,
            ActionOptions::timeout(5_000.0),
        )
        .expect("stub hidden page without animation frames");
    assert_eq!(visibility, Value::String("hidden".to_owned()));

    let endpoint = owner.ws_endpoint();
    let headers = json!({});
    let mut server = ServerProcess::spawn_with_options(
        Some((&endpoint, &headers, 10_000)),
        &[("RUSTWRIGHT_MCP_TOOL_TIMEOUT_MS", "3000")],
    );
    server.initialize();

    server.send(json!({
        "jsonrpc":"2.0","id":35,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    let initial = server.receive();
    let initial_snapshot = result_text(&initial).to_owned();
    assert!(initial_snapshot.contains("Visibility: hidden"));

    let started = Instant::now();
    server.send(json!({
        "jsonrpc":"2.0","id":36,"method":"tools/call",
        "params":{
            "name":"browser_scroll",
            "arguments":{"direction":"down","pixels":600}
        }
    }));
    let scrolled = server.receive();
    let elapsed = started.elapsed();
    let scrolled_snapshot = result_text(&scrolled).to_owned();
    assert!(
        elapsed < Duration::from_secs(2),
        "hidden-page viewport scroll approached the 3-second tool timeout: {elapsed:?}"
    );
    assert!(
        scroll_y(&scrolled_snapshot).abs_diff(600) <= 75,
        "hidden-page viewport did not scroll near 600:\n{scrolled_snapshot}"
    );

    server.finish();
    owner
        .close()
        .expect("close remote background-scroll browser");
}

#[test]
fn real_stdio_screenshot_returns_inline_png_image_content() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping screenshot MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let mut server = ServerProcess::spawn();
    server.initialize();

    server.send(json!({
        "jsonrpc":"2.0","id":36,"method":"tools/call",
        "params":{
            "name":"browser_navigate",
            "arguments":{"url":page_server.url()}
        }
    }));
    let navigated = server.receive();
    assert!(result_text(&navigated).contains("Activate feature"));

    server.send(json!({
        "jsonrpc":"2.0","id":37,"method":"tools/call",
        "params":{"name":"browser_take_screenshot","arguments":{}}
    }));
    let screenshot = server.receive();
    assert_eq!(
        screenshot["result"]["isError"], false,
        "expected successful screenshot response: {screenshot}"
    );
    let content = screenshot["result"]["content"]
        .as_array()
        .expect("screenshot content array");
    let image = content
        .iter()
        .find(|item| item["type"] == "image")
        .unwrap_or_else(|| {
            panic!("screenshot response did not contain image content: {screenshot}")
        });
    assert_eq!(image["mimeType"], "image/png");
    let encoded = image["data"].as_str().expect("base64 screenshot data");
    let bytes = STANDARD
        .decode(encoded)
        .expect("valid base64 screenshot data");
    assert!(
        bytes.starts_with(b"\x89PNG\r\n\x1a\n"),
        "screenshot content did not decode to a PNG"
    );

    server.finish();
}

#[test]
fn real_stdio_screenshot_over_cap_falls_back_to_temp_png_path() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping screenshot cap MCP test: Chromium executable unavailable");
        return;
    }

    // Contract: RUSTWRIGHT_MCP_SCREENSHOT_MAX_BYTES caps the base64-encoded MCP image
    // payload at 5 MiB by default. A 32-byte cap deterministically forces this branch.
    let page_server = PageServer::start();
    let mut server =
        ServerProcess::spawn_with_env(&[("RUSTWRIGHT_MCP_SCREENSHOT_MAX_BYTES", "32")]);
    server.initialize();

    server.send(json!({
        "jsonrpc":"2.0","id":38,"method":"tools/call",
        "params":{
            "name":"browser_navigate",
            "arguments":{"url":page_server.url()}
        }
    }));
    let navigated = server.receive();
    assert!(result_text(&navigated).contains("Activate feature"));

    server.send(json!({
        "jsonrpc":"2.0","id":39,"method":"tools/call",
        "params":{"name":"browser_take_screenshot","arguments":{}}
    }));
    let fallback = server.receive();
    assert_eq!(
        fallback["result"]["isError"], false,
        "expected successful screenshot fallback: {fallback}"
    );
    let content = fallback["result"]["content"]
        .as_array()
        .expect("screenshot fallback content array");
    assert!(
        content.iter().all(|item| item["type"] != "image"),
        "over-cap screenshot must not include inline image data: {fallback}"
    );
    assert!(
        serde_json::to_vec(&fallback)
            .expect("serialize screenshot fallback")
            .len()
            < 16 * 1024,
        "over-cap screenshot response was unexpectedly large"
    );
    let text = content
        .iter()
        .find(|item| item["type"] == "text")
        .and_then(|item| item["text"].as_str())
        .unwrap_or_else(|| panic!("screenshot fallback did not contain text: {fallback}"));
    let reason = text.to_ascii_lowercase();
    assert!(
        ["cap", "exceed", "large", "limit", "size"]
            .iter()
            .any(|term| reason.contains(term)),
        "screenshot fallback did not explain the size limit: {text}"
    );

    let path = png_path_from_fallback(text);
    assert!(path.is_absolute());
    let canonical_path = fs::canonicalize(&path).unwrap_or_else(|error| {
        panic!(
            "screenshot fallback path is not readable ({}): {error}",
            path.display()
        )
    });
    let screenshot_temp_dir = canonical_path
        .parent()
        .expect("fallback screenshot parent directory")
        .to_path_buf();
    let canonical_temp = fs::canonicalize(std::env::temp_dir()).expect("canonical OS temp dir");
    assert!(
        screenshot_temp_dir.parent() == Some(canonical_temp.as_path()),
        "fallback file was not written in a per-server directory under the OS temp dir: {}",
        screenshot_temp_dir.display()
    );
    let bytes = fs::read(&path).expect("read fallback screenshot");
    assert!(
        bytes.starts_with(b"\x89PNG\r\n\x1a\n"),
        "fallback file did not contain a PNG"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        let mode = fs::metadata(&path)
            .expect("fallback screenshot metadata")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600, "fallback screenshot permissions changed");
    }

    server.finish();
    assert!(
        !screenshot_temp_dir.exists(),
        "server screenshot directory survived graceful shutdown: {}",
        screenshot_temp_dir.display()
    );
}

#[test]
fn pre_initialize_request_is_rejected_without_browser() {
    let mut server = ServerProcess::spawn();
    server.send(json!({
        "jsonrpc":"2.0","id":41,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    let rejected = server.receive();
    assert_eq!(rejected["id"], 41);
    assert_eq!(rejected["error"]["code"], -32002);
    assert_eq!(rejected["error"]["message"], "Server not initialized");
    assert!(
        descendants(server.child.id()).is_empty(),
        "pre-initialize request must not launch browser processes"
    );

    server.initialize();
    let (_, diagnostics) = server.finish();
    assert!(!diagnostics.contains("launching Chromium"));
}

#[test]
fn idle_remote_server_does_not_attempt_attach_without_initialize() {
    let probe = AttachProbe::start();
    let server = ServerProcess::spawn_remote(&probe.endpoint(), &json!({}), 100);

    thread::sleep(Duration::from_millis(250));
    assert_eq!(
        probe.attempts(),
        0,
        "idle remote server must not open a CDP session"
    );
    drop(server);
    assert_eq!(probe.attempts(), 0);
}

#[test]
fn pre_initialize_remote_tool_call_is_rejected_without_attach() {
    let probe = AttachProbe::start();
    let mut server = ServerProcess::spawn_remote(&probe.endpoint(), &json!({}), 100);
    server.send(json!({
        "jsonrpc":"2.0","id":44,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    let rejected = server.receive();
    assert_eq!(rejected["id"], 44);
    assert_eq!(rejected["error"]["code"], -32002);

    thread::sleep(Duration::from_millis(150));
    assert_eq!(
        probe.attempts(),
        0,
        "pre-initialize rejection must not attempt remote attach"
    );
    server.initialize();
    server.finish();
    assert_eq!(probe.attempts(), 0);
}

#[test]
fn cancelled_notification_aborts_work_and_rmcp_drops_the_cancelled_response() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping cancellation MCP test: Chromium executable unavailable");
        return;
    }

    let hanging = HangingServer::start();
    let mut server = ServerProcess::spawn();
    server.initialize();
    server.send(json!({
        "jsonrpc":"2.0","id":89,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    assert_eq!(server.receive()["id"], 89);

    server.send(json!({
        "jsonrpc":"2.0","id":90,"method":"tools/call",
        "params":{"name":"browser_navigate","arguments":{"url":hanging.url()}}
    }));
    thread::sleep(Duration::from_millis(100));
    let cancelled_at = Instant::now();
    server.send(json!({
        "jsonrpc":"2.0",
        "method":"notifications/cancelled",
        "params":{"requestId":90,"reason":"test cancellation"}
    }));
    server.send(json!({
        "jsonrpc":"2.0","id":91,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    let snapshot = server.receive();
    assert_eq!(
        snapshot["id"], 91,
        "cancelled request must emit no response"
    );
    assert_eq!(snapshot["result"]["isError"], false);
    assert!(cancelled_at.elapsed() < Duration::from_secs(1));

    let (transcript, diagnostics) = server.finish();
    assert!(
        !transcript
            .iter()
            .filter(|line| line.starts_with("S> "))
            .any(|line| line.contains("\"id\":90"))
    );
    assert!(diagnostics.contains("browser actor: stopped"));
}

#[test]
fn validation_cancelled_stdio_response_is_suppressed_unknown_id_is_noop_and_next_call_works() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping validation cancellation MCP test: Chromium unavailable");
        return;
    }

    let hanging = HangingServer::start();
    let mut server = ServerProcess::spawn();
    server.initialize();
    server.send(json!({
        "jsonrpc":"2.0","id":189,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    assert_eq!(server.receive()["id"], 189);

    server.send(json!({
        "jsonrpc":"2.0",
        "method":"notifications/cancelled",
        "params":{"requestId":999999,"reason":"unknown validation id"}
    }));
    server.send(json!({"jsonrpc":"2.0","id":190,"method":"ping","params":{}}));
    assert_eq!(server.receive()["id"], 190);

    server.send(json!({
        "jsonrpc":"2.0","id":191,"method":"tools/call",
        "params":{"name":"browser_navigate","arguments":{"url":hanging.url()}}
    }));
    thread::sleep(Duration::from_millis(100));
    let cancelled_at = Instant::now();
    server.send(json!({
        "jsonrpc":"2.0",
        "method":"notifications/cancelled",
        "params":{"requestId":191,"reason":"validation cancellation"}
    }));
    server.send(json!({
        "jsonrpc":"2.0","id":192,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    let next = server.receive();
    let next_latency = cancelled_at.elapsed();
    assert_eq!(next["id"], 192, "cancelled response must be suppressed");
    assert_eq!(next["result"]["isError"], false);
    assert!(next_latency < Duration::from_secs(1));

    server.send(json!({"jsonrpc":"2.0","id":193,"method":"ping","params":{}}));
    assert_eq!(server.receive()["id"], 193);
    let (transcript, diagnostics) = server.finish();
    assert!(
        !transcript
            .iter()
            .filter(|line| line.starts_with("S> "))
            .any(|line| line.contains("\"id\":191"))
    );
    assert!(diagnostics.contains("browser actor: stopped"));
    println!(
        "validation rmcp cancellation: suppressed id=191 unknown-id=no-op next-call={next_latency:?} final-ping=ok"
    );
}

#[test]
fn malformed_json_and_unknown_method_return_errors_and_server_recovers() {
    let mut server = ServerProcess::spawn();
    server.send_raw(r#"{"jsonrpc":"2.0","id":"#);
    let malformed = server.receive();
    assert!(malformed.get("id").is_none() || malformed["id"].is_null());
    assert_eq!(malformed["error"]["code"], -32700);

    server.initialize();
    server.send(json!({
        "jsonrpc":"2.0","id":42,"method":"unknown/method","params":{}
    }));
    let unknown = server.receive();
    assert_eq!(unknown["id"], 42);
    assert_eq!(unknown["error"]["code"], -32601);

    server.send(json!({"jsonrpc":"2.0","id":43,"method":"ping","params":{}}));
    let pong = server.receive();
    assert_eq!(pong["id"], 43);
    assert!(pong["result"].is_object());
    server.finish();
}

#[test]
fn remote_mode_e2e_transmits_headers_and_reports_mid_session_death() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping remote MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let owner = chromium()
        .launch(LaunchOptions::default().arg("--remote-debugging-port=0"))
        .expect("launch remote browser owner");
    let existing = owner.new_page().expect("create existing remote page");
    existing
        .goto(
            &page_server.url(),
            GotoOptions::default().wait_until("load").timeout(10_000.0),
        )
        .expect("prime existing remote page");
    for page in owner.pages().expect("list owner pages") {
        if page.target_id() != existing.target_id() {
            page.close(Default::default()).expect("close startup page");
        }
    }

    let version_stub = VersionStub::start(owner.ws_endpoint());
    let resolver_endpoint = version_stub.endpoint.clone();
    let header_value = "recorded-header-value";
    let headers = json!({"x-rustwright-test": header_value});
    let mut server = ServerProcess::spawn_remote(&resolver_endpoint, &headers, 10_000);
    server.initialize();

    server.send(json!({
        "jsonrpc":"2.0","id":60,"method":"tools/call",
        "params":{"name":"browser_navigate","arguments":{"url":page_server.url()}}
    }));
    let request = version_stub.finish().to_ascii_lowercase();
    assert!(request.starts_with("get /json/version http/1.1"));
    assert!(request.contains("x-rustwright-test: recorded-header-value"));

    let navigated = server.receive();
    let navigated_text = result_text(&navigated).to_owned();
    assert!(navigated_text.contains("Activate feature"));

    server.send(json!({
        "jsonrpc":"2.0","id":61,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    let snapshot = server.receive();
    let snapshot_text = result_text(&snapshot).to_owned();
    let target = button_ref(&snapshot_text, "Activate feature");

    server.send(json!({
        "jsonrpc":"2.0","id":62,"method":"tools/call",
        "params":{"name":"browser_click","arguments":{"target":target}}
    }));
    let clicked = server.receive();
    assert!(result_text(&clicked).contains("Clicked successfully"));

    owner.close().expect("kill remote browser through owner");
    server.send(json!({
        "jsonrpc":"2.0","id":63,"method":"tools/call",
        "params":{
            "name":"browser_scroll",
            "arguments":{"direction":"down","pixels":100}
        }
    }));
    let unreachable = server.receive();
    let error = error_result_text(&unreachable);
    assert_eq!(error, REMOTE_UNREACHABLE);
    assert!(!error.contains(&resolver_endpoint));
    assert!(!error.contains(header_value));

    server.send(json!({"jsonrpc":"2.0","id":64,"method":"ping","params":{}}));
    let pong = server.receive();
    assert_eq!(pong["id"], 64);
    assert!(pong["result"].is_object());

    let (_, diagnostics) = server.finish();
    assert!(!diagnostics.contains(&resolver_endpoint));
    assert!(!diagnostics.contains(header_value));
}

#[test]
fn remote_shutdown_detaches_and_leaves_other_pages_alive() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping remote MCP shutdown test: Chromium executable unavailable");
        return;
    }

    let owner = chromium()
        .launch(LaunchOptions::default().arg("--remote-debugging-port=0"))
        .expect("launch remote browser owner");
    let first = owner.new_page().expect("create first remote page");
    let second = owner.new_page().expect("create second remote page");
    first
        .evaluate(
            "document.title = 'first remote page'",
            None,
            ActionOptions::timeout(5_000.0),
        )
        .expect("title first page");
    second
        .evaluate(
            "document.title = 'second remote page'",
            None,
            ActionOptions::timeout(5_000.0),
        )
        .expect("title second page");
    for page in owner.pages().expect("list owner pages") {
        if page.target_id() != first.target_id() && page.target_id() != second.target_id() {
            page.close(Default::default()).expect("close startup page");
        }
    }

    let endpoint = owner.ws_endpoint();
    let mut server = ServerProcess::spawn_remote(&endpoint, &json!({}), 10_000);
    server.initialize();
    server.send(json!({
        "jsonrpc":"2.0","id":70,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    let snapshot = server.receive();
    assert_eq!(snapshot["result"]["isError"], false);
    server.finish();

    assert!(
        owner.is_connected(),
        "remote owner process must survive detach"
    );
    assert_eq!(
        first.title(ActionOptions::timeout(5_000.0)).unwrap(),
        "first remote page"
    );
    assert_eq!(
        second.title(ActionOptions::timeout(5_000.0)).unwrap(),
        "second remote page"
    );
    assert!(owner.pages().expect("list surviving pages").len() >= 2);
    owner.close().expect("clean up owned remote browser");
}

#[test]
fn dead_remote_attach_is_sanitized_without_local_fallback() {
    let endpoint = "ws://127.0.0.1:1/test-cdp-path?marker=endpoint-value";
    let header_value = "header-marker-value";
    let mut server =
        ServerProcess::spawn_remote(endpoint, &json!({"x-rustwright-test": header_value}), 500);
    server.initialize();
    server.send(json!({
        "jsonrpc":"2.0","id":80,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    let response = server.receive();
    let error = error_result_text(&response);
    assert_eq!(error, REMOTE_UNREACHABLE);
    assert!(!error.contains(endpoint));
    assert!(!error.contains(header_value));
    assert!(descendants(server.child.id()).is_empty());

    server.send(json!({"jsonrpc":"2.0","id":81,"method":"ping","params":{}}));
    assert_eq!(server.receive()["id"], 81);
    let (_, diagnostics) = server.finish();
    assert!(!diagnostics.contains(endpoint));
    assert!(!diagnostics.contains(header_value));
}
