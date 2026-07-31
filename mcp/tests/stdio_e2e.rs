use std::{
    collections::{HashMap, HashSet, VecDeque},
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::PathBuf,
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::{
        Arc, Mutex, MutexGuard,
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
static STDIO_SERVER_LOCK: Mutex<()> = Mutex::new(());
static STDIO_WORKSPACE_COUNTER: AtomicUsize = AtomicUsize::new(1);

const PAGE_HTML: &str = r#"<!doctype html>
<html><head><title>MCP test page</title></head>
<body>
  <label for="name">Test input</label>
  <input id="name" value="sample value">
  <input aria-label="Secret input" type="password" value="do-not-render">
  <button onclick="this.textContent='Clicked button'; document.getElementById('status').textContent='Clicked successfully'">Activate feature</button>
  <div id="status">Waiting</div>
</body></html>"#;

const PARITY_PAGE_HTML: &str = r#"<!doctype html>
<html><head><title>Parity controls</title>
<style>#drop { width: 180px; height: 60px; border: 1px solid black; }</style>
</head>
<body>
  <main>
    <h1>Parity controls</h1>
    <label for="text">Text target</label>
    <input id="text" value="seed">
    <label for="choice">Choice target</label>
    <select id="choice">
      <option value="alpha">Alpha</option>
      <option value="beta">Beta</option>
    </select>
    <input id="check" type="checkbox"><label for="check">Check target</label>
    <input id="radio" type="radio" name="r"><label for="radio">Radio target</label>
    <label for="range">Range target</label>
    <input id="range" type="range" value="10">
    <button id="hover">Hover target</button>
    <button id="dialog">Dialog target</button>
    <label for="upload">Upload target</label>
    <input id="upload" type="file">
    <div id="drop" role="button" tabindex="0">Drop target</div>
    <div id="status" role="status">Status waiting</div>
    <div id="delayed" role="status">Delayed absent</div>
  </main>
  <script>
    console.error('Parity console error');
    console.info('Parity console info');
    const status = document.getElementById('status');
    document.getElementById('hover').addEventListener('mouseover', () => {
      status.textContent = 'Hover observed';
    });
    document.getElementById('dialog').addEventListener('click', () => {
      alert('Parity dialog');
      status.textContent = 'Dialog handled';
    });
    document.getElementById('upload').addEventListener('change', (event) => {
      const files = Array.from(event.target.files);
      status.textContent = files.length
        ? `Uploaded ${files.map((file) => file.name).join(', ')}`
        : 'Upload empty';
    });
    document.getElementById('drop').addEventListener('drop', (event) => {
      event.preventDefault();
      status.textContent = `Dropped ${event.dataTransfer.getData('text/plain')}`;
    });
  </script>
</body></html>"#;

const INPUT_PAGE_HTML: &str = r#"<!doctype html>
<html><head><title>Input controls</title></head>
<body>
  <label for="name">Test input</label>
  <input id="name" value="sample value">
  <input id="secret" aria-label="Secret input" type="password">
  <div id="password-length-readout" role="status">Password length: 0</div>
  <div id="input-readout" role="status">Input value: sample value</div>
  <div id="key-readout" role="status">Key pressed: none</div>
  <div id="focus-readout" role="status">Focused: none; focus changes: 0</div>
  <label for="choice">Test choice</label>
  <select id="choice">
    <option value="alpha">Alpha</option>
    <option value="beta">Beta</option>
  </select>
  <div id="select-readout" role="status">Selected value: alpha; changes: 0</div>
  <script>
    const input = document.getElementById('name');
    const secret = document.getElementById('secret');
    const updatePasswordLength = () => {
      document.getElementById('password-length-readout').textContent =
        `Password length: ${secret.value.length}`;
    };
    secret.addEventListener('input', updatePasswordLength);
    secret.addEventListener('change', updatePasswordLength);
    input.addEventListener('input', () => {
      document.getElementById('input-readout').textContent = `Input value: ${input.value}`;
    });
    document.addEventListener('keydown', (event) => {
      // The listener is on document, so it fires wherever the key lands. Recording
      // the target is what makes the readout target-sensitive: without it a key
      // delivered to the body or the wrong field is indistinguishable from a key
      // delivered to the element the test aimed at.
      const node = event.target;
      const target = node instanceof Element
        ? (node.id || node.tagName.toLowerCase())
        : 'none';
      document.getElementById('key-readout').textContent =
        `Key pressed: ${event.key}; trusted: ${event.isTrusted}; target: ${target}`;
    });
    let focusChanges = 0;
    // Focus is the page state a rejected key press must not disturb. Counting
    // the changes as well as naming the focused element makes a spurious focus
    // observable even when it lands back on the element that already had it.
    document.addEventListener('focusin', (event) => {
      focusChanges += 1;
      const node = event.target;
      const id = node instanceof Element
        ? (node.id || node.tagName.toLowerCase())
        : 'none';
      document.getElementById('focus-readout').textContent =
        `Focused: ${id}; focus changes: ${focusChanges}`;
    });
    let changes = 0;
    const choice = document.getElementById('choice');
    choice.addEventListener('change', () => {
      changes += 1;
      document.getElementById('select-readout').textContent =
        `Selected value: ${choice.value}; changes: ${changes}`;
    });
  </script>
</body></html>"#;

const DRAG_PAGE_HTML: &str = r#"<!doctype html>
<html><head><title>Physical drag</title>
<style>
  #drag-row { display: flex; align-items: center; gap: 180px; padding: 80px; }
  #drag-source, #drag-target {
    width: 180px;
    height: 80px;
    border: 2px solid black;
    display: grid;
    place-items: center;
    user-select: none;
  }
  #drag-source { background: lightblue; }
  #drag-target { background: lightgoldenrodyellow; }
</style>
</head>
<body>
  <main>
    <h1>Physical drag controls</h1>
    <div id="drag-row">
      <div id="drag-source" role="button" draggable="true">Draggable card</div>
      <div id="drag-target" role="button">Physical drop zone</div>
    </div>
    <div id="drag-status" role="status">Not dropped</div>
  </main>
  <script>
    const source = document.getElementById('drag-source');
    const target = document.getElementById('drag-target');
    const status = document.getElementById('drag-status');
    source.addEventListener('dragstart', (event) => {
      event.dataTransfer.setData('text/plain', 'physical-card');
    });
    target.addEventListener('dragover', (event) => event.preventDefault());
    target.addEventListener('drop', (event) => {
      event.preventDefault();
      status.textContent =
        `Physically dropped ${event.dataTransfer.getData('text/plain')}; trusted=${event.isTrusted}`;
    });

    const ambiguous = new URLSearchParams(location.search).get('ambiguous');
    const duplicateId =
      ambiguous === 'start' ? 'drag-source' :
      ambiguous === 'end' ? 'drag-target' :
      null;
    if (duplicateId) {
      const observer = new MutationObserver((records) => {
        const stamped = records
          .map((record) => record.target)
          .find((element) =>
            element.id === duplicateId && element.hasAttribute('data-mcp-ref')
          );
        if (!stamped) return;
        observer.disconnect();
        const duplicate = stamped.cloneNode(true);
        duplicate.removeAttribute('id');
        stamped.after(duplicate);
      });
      observer.observe(document.body, {
        attributes: true,
        subtree: true,
        attributeFilter: ['data-mcp-ref'],
      });
    }
  </script>
</body></html>"#;

const NETWORK_PAGE_HTML: &str = r#"<!doctype html>
<html><head><title>Network records</title></head>
<body>
  <main>
    <h1>Network records</h1>
    <div id="network-status" role="status">Network pending</div>
    <img src="/network-static.svg" alt="Network static asset">
  </main>
  <script>
    Promise.all([
      fetch('/api/data', {
        method: 'POST',
        headers: { 'X-Network-Request': 'captured' },
        body: 'request-payload-123'
      }).then(async (response) => `${response.status} ${await response.text()}`),
      fetch('/large-text').then((response) => response.text())
    ]).then(([api]) => {
      document.getElementById('network-status').textContent = `Network ready: ${api}`;
    }).catch((error) => {
      document.getElementById('network-status').textContent = `Network failed: ${error}`;
    });
  </script>
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
                    Ok((stream, _)) => {
                        // Serve each connection on its own thread. Under full-suite load
                        // Chromium opens speculative/preconnect sockets that never send a
                        // request; on a single serial accept loop such a socket blocks the
                        // loop for its whole read timeout, the OS accept backlog overflows,
                        // and the real navigation's SYN is dropped -- surfacing as
                        // ERR_SOCKET_NOT_CONNECTED / ERR_CONNECTION_RESET. A per-connection
                        // thread drains the backlog immediately so one idle client can never
                        // starve the others; catch_unwind still contains any panic so a
                        // misbehaving connection can't abort the process.
                        thread::spawn(move || {
                            let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                                serve_connection(stream);
                            }));
                        });
                    }
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

    fn parity_url(&self) -> String {
        format!("http://{}/parity", self.addr)
    }

    fn input_url(&self) -> String {
        format!("http://{}/input", self.addr)
    }

    fn network_url(&self) -> String {
        format!("http://{}/network", self.addr)
    }

    fn drag_url(&self) -> String {
        format!("http://{}/drag", self.addr)
    }

    fn ambiguous_drag_url(&self, endpoint: &str) -> String {
        format!("{}?ambiguous={endpoint}", self.drag_url())
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
    // The listener runs non-blocking so the accept loop can poll its stop flag, but on
    // macOS/BSD an accepted socket inherits O_NONBLOCK from that listener and Rust does not
    // clear it. Left non-blocking, the read below returns `WouldBlock` before Chromium's
    // request bytes have landed, so we read an empty request, send no response, and the
    // navigation fails with ERR_SOCKET_NOT_CONNECTED -- intermittently, as a scheduling
    // race that worsens under full-suite load. Force the connection blocking so the read
    // timeout actually governs and we wait for the request.
    if stream.set_nonblocking(false).is_err() {
        return;
    }
    // The client (Chromium) frequently opens speculative/preconnect sockets under load
    // and abandons them; treat every client-side I/O failure as "this connection went
    // away" and return quietly rather than panicking the shared accept loop.
    if stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .is_err()
    {
        return;
    }
    let mut request = Vec::new();
    let mut buffer = [0_u8; 2048];
    while request.len() < 8192 {
        let read = stream.read(&mut buffer).unwrap_or(0);
        if read == 0 {
            break;
        }
        request.extend_from_slice(&buffer[..read]);
        if request.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }
    let request = String::from_utf8_lossy(&request);
    let Some(request_target) = request.split_whitespace().nth(1) else {
        return;
    };
    let path = request_target
        .strip_prefix("http://")
        .and_then(|rest| rest.find('/').map(|offset| &rest[offset..]))
        .unwrap_or(request_target)
        .split('?')
        .next()
        .unwrap_or("/");
    if path == "/slow" {
        thread::sleep(Duration::from_millis(450));
    }
    let (content_type, extra_headers, body) = if path == "/api/data" {
        (
            "application/json; charset=utf-8",
            "X-Network-Response: captured\r\n",
            br#"{"message":"network-response-body"}"#.to_vec(),
        )
    } else if path == "/large-text" {
        ("text/plain; charset=utf-8", "", vec![b'x'; 70 * 1024])
    } else if path == "/network-static.svg" {
        (
            "image/svg+xml",
            "",
            br#"<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>"#.to_vec(),
        )
    } else if path == "/history-a" {
        (
            "text/html; charset=utf-8",
            "",
            HISTORY_PAGE_A_HTML.as_bytes().to_vec(),
        )
    } else if path == "/history-b" {
        (
            "text/html; charset=utf-8",
            "",
            HISTORY_PAGE_B_HTML.as_bytes().to_vec(),
        )
    } else if path == "/history-spa" {
        (
            "text/html; charset=utf-8",
            "",
            SPA_HISTORY_PAGE_HTML.as_bytes().to_vec(),
        )
    } else if path == "/history-hash" {
        (
            "text/html; charset=utf-8",
            "",
            HASH_HISTORY_PAGE_HTML.as_bytes().to_vec(),
        )
    } else if path == "/background-scroll" {
        (
            "text/html; charset=utf-8",
            "",
            BACKGROUND_SCROLL_PAGE_HTML.as_bytes().to_vec(),
        )
    } else if path == "/scroll" {
        (
            "text/html; charset=utf-8",
            "",
            SCROLL_PAGE_HTML.as_bytes().to_vec(),
        )
    } else if path == "/parity" {
        (
            "text/html; charset=utf-8",
            "",
            PARITY_PAGE_HTML.as_bytes().to_vec(),
        )
    } else if path == "/input" {
        (
            "text/html; charset=utf-8",
            "",
            INPUT_PAGE_HTML.as_bytes().to_vec(),
        )
    } else if path == "/network" {
        (
            "text/html; charset=utf-8",
            "",
            NETWORK_PAGE_HTML.as_bytes().to_vec(),
        )
    } else if path == "/drag" {
        (
            "text/html; charset=utf-8",
            "",
            DRAG_PAGE_HTML.as_bytes().to_vec(),
        )
    } else {
        (
            "text/html; charset=utf-8",
            "",
            PAGE_HTML.as_bytes().to_vec(),
        )
    };
    let response_headers = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n{extra_headers}Content-Length: {}\r\nConnection: close\r\n\r\n",
        body.len(),
    );
    if stream.write_all(response_headers.as_bytes()).is_err() {
        return;
    }
    let _ = stream.write_all(&body);
}

struct ServerProcess {
    _test_guard: MutexGuard<'static, ()>,
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
        let test_guard = STDIO_SERVER_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let mut command = Command::new(env!("CARGO_BIN_EXE_rustwright-mcp"));
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
            _test_guard: test_guard,
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

fn role_ref(snapshot: &str, role: &str, name: &str) -> String {
    let line = snapshot
        .lines()
        .find(|line| line.contains(&format!("- {role}")) && line.contains(name))
        .unwrap_or_else(|| panic!("{role} {name:?} missing from snapshot:\n{snapshot}"));
    let marker = "[ref=";
    let start = line.find(marker).expect("role ref start") + marker.len();
    let end = line[start..].find(']').expect("role ref end") + start;
    line[start..end].to_owned()
}

// Extract a rendered readout's full text so two snapshots can be compared for
// equality. `prefix` starts with the opening quote the renderer wraps names in;
// returning the text up to the closing quote keeps ref markers -- whose numbers
// are reassigned on every snapshot -- out of the comparison.
fn status_text(snapshot: &str, prefix: &str) -> String {
    assert!(
        prefix.starts_with('"'),
        "prefix must start at the renderer's opening quote: {prefix:?}"
    );
    let mut matches = snapshot.match_indices(prefix);
    let (start, _) = matches
        .next()
        .unwrap_or_else(|| panic!("{prefix:?} missing from snapshot:\n{snapshot}"));
    assert!(
        matches.next().is_none(),
        "{prefix:?} matched more than once, so equality would be ambiguous:\n{snapshot}"
    );
    let rest = &snapshot[start + 1..];
    let end = rest
        .find('"')
        .unwrap_or_else(|| panic!("unterminated readout for {prefix:?}:\n{snapshot}"));
    rest[..end].to_owned()
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

fn call_tool(server: &mut ServerProcess, id: i64, name: &str, arguments: Value) -> Value {
    server.send(json!({
        "jsonrpc": "2.0",
        "id": id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments}
    }));
    let response = server.receive();
    assert_eq!(response["id"], id);
    response
}

fn converge_snapshot_text(
    server: &mut ServerProcess,
    mut response: Value,
    mut poll_id: i64,
    expected: &str,
) -> Value {
    let deadline = Instant::now() + Duration::from_secs(5);
    while !result_text(&response).contains(expected) {
        assert!(
            Instant::now() < deadline,
            "snapshot did not converge to {expected:?}: {}",
            result_text(&response)
        );
        thread::sleep(Duration::from_millis(25));
        response = call_tool(server, poll_id, "browser_snapshot", json!({}));
        poll_id += 1;
    }
    response
}

fn named_ref(snapshot: &str, role: &str, name: &str) -> String {
    let line = snapshot
        .lines()
        .find(|line| line.contains(&format!("- {role}")) && line.contains(name))
        .unwrap_or_else(|| panic!("{role} {name:?} missing from snapshot:\n{snapshot}"));
    let marker = "[ref=";
    let start = line.find(marker).expect("named ref start") + marker.len();
    let end = line[start..].find(']').expect("named ref end") + start;
    line[start..end].to_owned()
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

fn poll_snapshot_until(
    server: &mut ServerProcess,
    latest: String,
    next_id: &mut i64,
    needle: &str,
) -> String {
    poll_snapshot_state(
        server,
        latest,
        next_id,
        &format!("{needle:?}"),
        |snapshot| snapshot.contains(needle),
    )
}

// Masking is a property of EVERY snapshot, not just the one a poll settles on.
// Checking only the returned snapshot would let a plaintext leak in the tool's
// immediate response or in any intermediate poll pass unnoticed, because a later
// masked snapshot would overwrite it. `poll_snapshot_state` runs its predicate on
// the initial value and on each snapshot it fetches, so asserting inside the
// predicate covers every snapshot this poll observes.
fn poll_snapshot_until_never_leaking(
    server: &mut ServerProcess,
    latest: String,
    next_id: &mut i64,
    needle: &str,
    secret: &str,
) -> String {
    poll_snapshot_state(
        server,
        latest,
        next_id,
        &format!("{needle:?}"),
        |snapshot| {
            assert!(
                !snapshot.contains(secret),
                "snapshot leaked the secret {secret:?}:\n{snapshot}"
            );
            // The positive complement of the leak check: absence of the plaintext
            // is also satisfied by a snapshot that dropped the field entirely, so
            // any snapshot still rendering the password row has to render it
            // masked. Responses that carry no snapshot mention neither string and
            // pass, which is what lets the poll keep going instead of failing on
            // an intermediate tool reply.
            assert!(
                !snapshot.contains("Secret input") || snapshot.contains("[value=••••••]"),
                "snapshot rendered the password row unmasked:\n{snapshot}"
            );
            snapshot.contains(needle)
        },
    )
}

// Page-side readouts are written by the page's own event listeners at the
// renderer's next rendering step, so a tool response's snapshot cannot promise
// they are already current; converge on the expected state instead of
// asserting one unguaranteed interleaving.
fn poll_snapshot_state(
    server: &mut ServerProcess,
    mut latest: String,
    next_id: &mut i64,
    what: &str,
    predicate: impl Fn(&str) -> bool,
) -> String {
    let deadline = Instant::now() + Duration::from_secs(15);
    while !predicate(&latest) {
        assert!(
            Instant::now() < deadline,
            "snapshot did not converge on {what}:\n{latest}"
        );
        let id = *next_id;
        *next_id += 1;
        server.send(json!({
            "jsonrpc":"2.0","id":id,"method":"tools/call",
            "params":{"name":"browser_snapshot","arguments":{}}
        }));
        latest = result_text(&server.receive()).to_owned();
        thread::sleep(Duration::from_millis(20));
    }
    latest
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

#[derive(Clone)]
struct ProcessInfo {
    pid: u32,
    started: String,
    command: String,
}

fn malformed_process_row(line: &str) -> ! {
    panic!("unexpected ps row format: {line:?}");
}

fn process_rows() -> Vec<(ProcessInfo, u32)> {
    let output = Command::new("ps")
        .args(["-axo", "pid=,ppid=,lstart=,comm="])
        .env("LC_ALL", "C")
        .env("LANG", "C")
        .output()
        .expect("run ps");
    // A failed ps yields no rows, and no rows reads as "every captured process exited" --
    // the same silent false negative this whole check exists to rule out.
    assert!(
        output.status.success(),
        "ps failed with {}: {}",
        output.status,
        String::from_utf8_lossy(&output.stderr).trim(),
    );
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(|line| {
            let mut fields = line.split_whitespace();
            let pid = fields
                .next()
                .and_then(|field| field.parse().ok())
                .unwrap_or_else(|| malformed_process_row(line));
            let ppid = fields
                .next()
                .and_then(|field| field.parse().ok())
                .unwrap_or_else(|| malformed_process_row(line));
            let started = (0..5)
                .map(|_| fields.next().unwrap_or_else(|| malformed_process_row(line)))
                .collect::<Vec<_>>();
            let year = started[4];
            if year.len() != 4 || !year.bytes().all(|byte| byte.is_ascii_digit()) {
                malformed_process_row(line);
            }
            let started = started.join(" ");
            // comm may legitimately be empty: procps-ng prints the kernel command name with no
            // non-empty fallback, and PR_SET_NAME lets a process set it to "". Since ps scans
            // every process on the runner, panicking here would fail the test because of an
            // unrelated process. The four-digit year above is the structural check; the command
            // is only used to make a real leak diagnosable from the failure message.
            let command = fields.collect::<Vec<_>>().join(" ");
            (
                ProcessInfo {
                    pid,
                    started,
                    command,
                },
                ppid,
            )
        })
        .collect()
}

fn descendants(root: u32) -> Vec<ProcessInfo> {
    let rows = process_rows();
    let mut by_parent: HashMap<u32, Vec<ProcessInfo>> = HashMap::new();
    for (process, ppid) in rows {
        by_parent.entry(ppid).or_default().push(process);
    }
    let mut queue = VecDeque::from([root]);
    let mut found = Vec::new();
    while let Some(parent) = queue.pop_front() {
        if let Some(children) = by_parent.get(&parent) {
            for process in children {
                found.push(process.clone());
                queue.push_back(process.pid);
            }
        }
    }
    found
}

fn wait_for_processes_to_exit(processes: &[ProcessInfo], timeout: Duration) -> Vec<ProcessInfo> {
    let deadline = Instant::now() + timeout;
    loop {
        // A PID can be reused after the captured process exits, so include its
        // start time when deciding whether the same process is still alive.
        let live_processes: HashMap<u32, String> = process_rows()
            .into_iter()
            .map(|(process, _)| (process.pid, process.started))
            .collect();
        let survivors: Vec<ProcessInfo> = processes
            .iter()
            .filter(|process| live_processes.get(&process.pid) == Some(&process.started))
            .cloned()
            .collect();
        if survivors.is_empty() || Instant::now() >= deadline {
            return survivors;
        }
        thread::sleep(Duration::from_millis(25));
    }
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
    assert_eq!(tools.len(), 27);
    assert_eq!(
        tools
            .iter()
            .map(|tool| tool["name"].as_str().expect("tool name"))
            .collect::<Vec<_>>(),
        [
            "browser_navigate",
            "browser_navigate_back",
            "browser_navigate_forward",
            "browser_reload",
            "browser_resize",
            "browser_snapshot",
            "browser_find",
            "browser_click",
            "browser_scroll",
            "browser_type",
            "browser_select_option",
            "browser_fill_form",
            "browser_hover",
            "browser_press_key",
            "browser_drag",
            "browser_drop",
            "browser_console_messages",
            "browser_network_requests",
            "browser_network_request",
            "browser_tabs",
            "browser_handle_dialog",
            "browser_file_upload",
            "browser_wait_for",
            "browser_get_text",
            "browser_evaluate",
            "browser_take_screenshot",
            "browser_close",
        ]
    );
    let tool = |name: &str| {
        tools
            .iter()
            .find(|tool| tool["name"] == name)
            .unwrap_or_else(|| panic!("missing tool {name}"))
    };
    assert_eq!(
        tool("browser_navigate")["inputSchema"]["required"],
        json!(["url"])
    );
    assert_eq!(
        tool("browser_navigate")["inputSchema"]["properties"]["url"]["type"],
        "string"
    );
    assert_eq!(
        tool("browser_navigate_back")["inputSchema"]["properties"],
        json!({})
    );
    assert_eq!(
        tool("browser_navigate_forward")["inputSchema"]["properties"],
        json!({})
    );
    assert_eq!(
        tool("browser_click")["inputSchema"]["properties"]["target"]["pattern"],
        "^e[1-9][0-9]*$"
    );
    assert_eq!(
        tool("browser_press_key")["inputSchema"]["required"],
        json!(["key"])
    );
    // The optional target is a snapshot ref, constrained exactly like every
    // other ref-taking tool so a caller cannot smuggle a selector through it.
    assert_eq!(
        tool("browser_press_key")["inputSchema"]["properties"]["target"]["pattern"],
        "^e[1-9][0-9]*$"
    );
    assert_eq!(
        tool("browser_hover")["inputSchema"]["required"],
        json!(["target"])
    );
    assert_eq!(
        tool("browser_select_option")["inputSchema"]["required"],
        json!(["target"])
    );
    for field in ["values", "value"] {
        assert_eq!(
            tool("browser_select_option")["inputSchema"]["properties"][field]["oneOf"],
            json!([
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}}
            ])
        );
    }
    assert_eq!(
        tool("browser_select_option")["inputSchema"]["oneOf"],
        json!([
            {
                "required": ["values"],
                "not": {"required": ["value"]}
            },
            {
                "required": ["value"],
                "not": {"required": ["values"]}
            }
        ])
    );
    assert_eq!(
        tool("browser_scroll")["inputSchema"]["properties"]["direction"]["enum"],
        json!(["up", "down"])
    );
    assert_eq!(
        tool("browser_take_screenshot")["inputSchema"]["properties"]["type"]["enum"],
        json!(["png", "jpeg"])
    );
    assert_eq!(
        tool("browser_network_requests")["inputSchema"]["properties"]["static"]["default"],
        false
    );
    assert_eq!(
        tool("browser_drag")["inputSchema"]["required"],
        json!(["startTarget", "endTarget"])
    );
    assert_eq!(
        tool("browser_drag")["inputSchema"]["properties"]["startElement"]["type"],
        json!(["string", "null"])
    );
    assert_eq!(
        tool("browser_network_request")["inputSchema"]["required"],
        json!(["index"])
    );
    assert_eq!(
        tool("browser_network_request")["inputSchema"]["properties"]["index"]["minimum"],
        1
    );
    assert_eq!(
        tool("browser_network_request")["inputSchema"]["properties"]["part"]["enum"],
        json!([
            "request-headers",
            "request-body",
            "response-headers",
            "response-body",
            null
        ])
    );
    assert_eq!(
        tool("browser_file_upload")["inputSchema"]["properties"]["paths"],
        json!({
            "type": ["array", "null"],
            "items": {"type": "string"}
        })
    );
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
    let browser_pids: Vec<u32> = browser_processes
        .iter()
        .map(|process| process.pid)
        .collect();
    let (transcript, diagnostics) = server.finish();

    // The server can exit before Chromium finishes tearing down its helpers.
    // A bounded wait distinguishes that handoff from a process that was leaked.
    let browser_exit_timeout = Duration::from_secs(5);
    let browser_exit_started = Instant::now();
    let orphan_processes = wait_for_processes_to_exit(&browser_processes, browser_exit_timeout);
    let browser_exit_wait = browser_exit_started.elapsed();
    let orphan_pids: Vec<u32> = orphan_processes.iter().map(|process| process.pid).collect();
    let orphan_details: Vec<String> = orphan_processes
        .iter()
        .map(|process| {
            format!(
                "pid {} (started {}, command {})",
                process.pid, process.started, process.command
            )
        })
        .collect();
    assert!(
        orphan_processes.is_empty(),
        "orphan browser processes after waiting {browser_exit_wait:?} \
         (timeout {browser_exit_timeout:?}): {orphan_details:?}"
    );
    assert!(diagnostics.contains("browser actor: stopped"));

    println!("--- stdio e2e transcript ---");
    for line in transcript {
        println!("{line}");
    }
    println!("--- shutdown evidence ---");
    println!("captured browser descendants: {browser_pids:?}");
    println!("orphan browser descendants after waiting {browser_exit_wait:?}: {orphan_pids:?}");
}

#[test]
fn real_stdio_tool_profiles_and_evaluation_gate_match_contract() {
    fn names(environment: &[(&str, &str)]) -> Vec<String> {
        let mut server = ServerProcess::spawn_with_env(environment);
        server.initialize();
        server.send(json!({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}));
        let listed = server.receive();
        let names = listed["result"]["tools"]
            .as_array()
            .expect("tools array")
            .iter()
            .map(|tool| tool["name"].as_str().expect("tool name").to_owned())
            .collect();
        server.finish();
        names
    }

    let mirror = names(&[("RUSTWRIGHT_MCP_TOOLSET", "mirror")]);
    assert_eq!(mirror.len(), 27);
    assert!(mirror.contains(&"browser_fill_form".to_owned()));
    assert!(mirror.contains(&"browser_console_messages".to_owned()));
    assert!(mirror.contains(&"browser_network_requests".to_owned()));
    assert!(mirror.contains(&"browser_network_request".to_owned()));
    assert!(mirror.contains(&"browser_file_upload".to_owned()));
    assert!(mirror.contains(&"browser_drag".to_owned()));
    assert!(mirror.contains(&"browser_evaluate".to_owned()));

    let lean = names(&[("RUSTWRIGHT_MCP_TOOLSET", "lean")]);
    assert_eq!(lean.len(), 17);
    assert!(!lean.contains(&"browser_fill_form".to_owned()));
    assert!(!lean.contains(&"browser_console_messages".to_owned()));
    assert!(!lean.contains(&"browser_network_requests".to_owned()));
    assert!(!lean.contains(&"browser_network_request".to_owned()));
    assert!(!lean.contains(&"browser_file_upload".to_owned()));
    assert!(!lean.contains(&"browser_drag".to_owned()));
    assert!(lean.contains(&"browser_evaluate".to_owned()));

    let no_eval = names(&[
        ("RUSTWRIGHT_MCP_TOOLSET", "lean"),
        ("RUSTWRIGHT_MCP_ALLOW_EVAL", "false"),
    ]);
    assert_eq!(no_eval.len(), 16);
    assert!(!no_eval.contains(&"browser_evaluate".to_owned()));
}

#[test]
fn real_stdio_type_press_enter_and_select_option_round_trip() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping input tools MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let mut server = ServerProcess::spawn();
    server.initialize();

    server.send(json!({
        "jsonrpc":"2.0","id":201,"method":"tools/call",
        "params":{"name":"browser_navigate","arguments":{"url":page_server.input_url()}}
    }));
    let navigated = result_text(&server.receive()).to_owned();
    assert!(navigated.contains("Test input"), "{navigated}");

    server.send(json!({
        "jsonrpc":"2.0","id":202,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    let snapshot = result_text(&server.receive()).to_owned();
    assert!(snapshot.contains(r#""Password length: 0""#), "{snapshot}");
    // Nothing has been typed yet, and the renderer omits the value part entirely
    // for an empty field, so the mask is absent here. Pinning that keeps the mask
    // assertions below honest: they observe a transition rather than a constant
    // that would hold even if masking were removed from the renderer.
    assert!(!snapshot.contains("[value=••••••]"), "{snapshot}");
    let password_target = role_ref(&snapshot, "textbox", "Secret input");
    let password_sentinel = "stdio-password-sentinel-203";
    let mut next_id = 203;

    let typed_id = next_id;
    next_id += 1;
    server.send(json!({
        "jsonrpc":"2.0","id":typed_id,"method":"tools/call",
        "params":{
            "name":"browser_type",
            "arguments":{"target":password_target,"text":password_sentinel}
        }
    }));
    let password_typed = result_text(&server.receive()).to_owned();
    // The page reports the length it received, so the secret round-tripped
    // intact even though every snapshot renders it masked. Every snapshot
    // observed from here on is checked for the sentinel, so a leak at any point
    // fails the test rather than being overwritten by a later masked snapshot.
    let password_typed = poll_snapshot_until_never_leaking(
        &mut server,
        password_typed,
        &mut next_id,
        &format!(
            r#""Password length: {}""#,
            password_sentinel.chars().count()
        ),
        password_sentinel,
    );
    assert!(
        password_typed.contains("[value=••••••]"),
        "{password_typed}"
    );

    let input_target = role_ref(&password_typed, "textbox", "Test input");
    let input_id = next_id;
    next_id += 1;
    server.send(json!({
        "jsonrpc":"2.0","id":input_id,"method":"tools/call",
        "params":{
            "name":"browser_type",
            "arguments":{"target":input_target,"text":"stdio typed"}
        }
    }));
    let typed = result_text(&server.receive()).to_owned();
    assert!(typed.contains(r#"[value="stdio typed"]"#), "{typed}");
    assert!(typed.contains("[value=••••••]"), "{typed}");
    assert!(!typed.contains(password_sentinel), "{typed}");

    let press_id = next_id;
    next_id += 1;
    server.send(json!({
        "jsonrpc":"2.0","id":press_id,"method":"tools/call",
        "params":{"name":"browser_press_key","arguments":{"key":"Enter"}}
    }));
    let pressed = result_text(&server.receive()).to_owned();
    // An untargeted press goes wherever focus already is, and the typing above
    // left focus on the text input -- so the readout naming `name` as the target
    // is what distinguishes a key that reached the focused element from one that
    // fell through to the body.
    let observed = poll_snapshot_until_never_leaking(
        &mut server,
        pressed,
        &mut next_id,
        r#""Key pressed: Enter; trusted: true; target: name""#,
        password_sentinel,
    );
    assert!(
        observed.contains(r#""Input value: stdio typed""#),
        "{observed}"
    );

    let select_target = role_ref(&observed, "combobox", "Test choice");
    let select_id = next_id;
    next_id += 1;
    server.send(json!({
        "jsonrpc":"2.0","id":select_id,"method":"tools/call",
        "params":{
            "name":"browser_select_option",
            "arguments":{"target":select_target,"values":"beta"}
        }
    }));
    let selected = result_text(&server.receive()).to_owned();
    let selected = poll_snapshot_until_never_leaking(
        &mut server,
        selected,
        &mut next_id,
        r#""Selected value: beta; changes: 1""#,
        password_sentinel,
    );
    assert!(
        selected.contains(r#""Input value: stdio typed""#),
        "{selected}"
    );
    assert!(selected.contains("[value=••••••]"), "{selected}");
    assert!(!selected.contains(password_sentinel), "{selected}");

    // A targeted press focuses the ref before dispatching, so the key has to
    // land on the password field rather than on whatever held focus (the text
    // input, from the typing above). The length readout is caret-agnostic --
    // an insertion anywhere still moves the count by one -- so a dropped target
    // leaves the length unchanged and stalls this poll instead of passing.
    let secret_target = role_ref(&selected, "textbox", "Secret input");
    let targeted_press_id = next_id;
    next_id += 1;
    server.send(json!({
        "jsonrpc":"2.0","id":targeted_press_id,"method":"tools/call",
        "params":{
            "name":"browser_press_key",
            "arguments":{"target":secret_target,"key":"7"}
        }
    }));
    let targeted = result_text(&server.receive()).to_owned();
    let targeted = poll_snapshot_until_never_leaking(
        &mut server,
        targeted,
        &mut next_id,
        &format!(
            r#""Password length: {}""#,
            password_sentinel.chars().count() + 1
        ),
        password_sentinel,
    );
    assert!(
        targeted.contains(r#""Key pressed: 7; trusted: true; target: secret""#),
        "{targeted}"
    );
    // The key went to the targeted ref and nowhere else: the text input's readout
    // still reads exactly what the earlier typing left it, closing quote included,
    // so a stray "7" delivered there would fail this.
    assert!(
        targeted.contains(r#""Input value: stdio typed""#),
        "{targeted}"
    );
    assert!(targeted.contains("[value=••••••]"), "{targeted}");

    // Rejecting a key must reject it before anything touches the page. The
    // targeted press above focused the password field to deliver its key, so
    // focus now names `secret` -- which also proves the readout is live rather
    // than a constant, since it started at `none`.
    let targeted = poll_snapshot_until_never_leaking(
        &mut server,
        targeted,
        &mut next_id,
        r#""Focused: secret; focus changes: "#,
        password_sentinel,
    );
    let focus_before = status_text(&targeted, r#""Focused: secret"#);

    // Aim the bad key at the *text* input, not the password field that currently
    // holds focus: validating after resolving the ref would run the text input's
    // focus handlers and step the counter, so an unchanged readout is what
    // distinguishes "rejected before touching the page" from "rejected after".
    let stale_focus_target = role_ref(&targeted, "textbox", "Test input");
    let rejected_id = next_id;
    next_id += 1;
    let rejected = call_tool(
        &mut server,
        rejected_id,
        "browser_press_key",
        json!({"target": stale_focus_target, "key": "NoSuchKey"}),
    );
    let rejected = error_result_text(&rejected).to_owned();
    assert!(rejected.contains("NoSuchKey"), "{rejected}");

    let after_reject_id = next_id;
    next_id += 1;
    let after_reject = call_tool(&mut server, after_reject_id, "browser_snapshot", json!({}));
    let after_reject = result_text(&after_reject).to_owned();
    assert_eq!(
        status_text(&after_reject, r#""Focused: "#),
        focus_before,
        "a rejected key press moved focus:\n{after_reject}"
    );
    assert!(
        after_reject.contains(r#""Key pressed: 7; trusted: true; target: secret""#),
        "a rejected key press reached the page:\n{after_reject}"
    );
    assert!(!after_reject.contains(password_sentinel), "{after_reject}");

    // Filling a combobox resolves the visible label: the options are labelled
    // "Alpha" and "Beta" over lowercase values, so a value-only matcher finds
    // nothing and fails the call rather than reporting a new selection. The
    // change counter reaching 2 proves exactly one further change event fired.
    //
    // The refs come from the post-rejection snapshot: a rejected ref action
    // still clears the ref table on its way out, so every ref taken before it
    // is stale by now.
    let choice_target = role_ref(&after_reject, "combobox", "Test choice");
    let fill_id = next_id;
    next_id += 1;
    server.send(json!({
        "jsonrpc":"2.0","id":fill_id,"method":"tools/call",
        "params":{
            "name":"browser_fill_form",
            "arguments":{"fields":[
                {"target":choice_target,"name":"choice","type":"combobox","value":"Alpha"}
            ]}
        }
    }));
    let filled = result_text(&server.receive()).to_owned();
    let filled = poll_snapshot_until_never_leaking(
        &mut server,
        filled,
        &mut next_id,
        r#""Selected value: alpha; changes: 2""#,
        password_sentinel,
    );
    assert!(filled.contains(r#""Input value: stdio typed""#), "{filled}");
    assert!(filled.contains("[value=••••••]"), "{filled}");
    assert!(!filled.contains(password_sentinel), "{filled}");

    server.finish();
}

#[test]
fn real_stdio_physical_drag_is_trusted_strict_and_updates_live_dom() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping physical drag MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let mut server = ServerProcess::spawn();
    server.initialize();

    let navigated = call_tool(
        &mut server,
        170,
        "browser_navigate",
        json!({"url": page_server.drag_url()}),
    );
    let snapshot = result_text(&navigated).to_owned();
    let start = named_ref(&snapshot, "button", "Draggable card");
    let end = named_ref(&snapshot, "button", "Physical drop zone");

    let stale_start = call_tool(
        &mut server,
        171,
        "browser_drag",
        json!({"startTarget": "e999999", "endTarget": end.clone()}),
    );
    assert!(
        error_result_text(&stale_start).contains("unknown or stale ref e999999"),
        "{stale_start}"
    );
    let stale_end = call_tool(
        &mut server,
        172,
        "browser_drag",
        json!({"startTarget": start.clone(), "endTarget": "e999999"}),
    );
    assert!(
        error_result_text(&stale_end).contains("unknown or stale ref e999999"),
        "{stale_end}"
    );

    let dragged = call_tool(
        &mut server,
        173,
        "browser_drag",
        json!({
            "startTarget": start,
            "endTarget": end,
            "startElement": "Draggable card",
            "endElement": "Physical drop zone"
        }),
    );
    let dragged = result_text(&dragged);
    assert!(
        dragged.contains("### Result\nDragged Draggable card to Physical drop zone."),
        "{dragged}"
    );
    assert!(dragged.contains("### Snapshot"), "{dragged}");
    assert!(
        dragged.contains("Physically dropped physical-card; trusted=true"),
        "{dragged}"
    );

    for (id, endpoint) in [(174, "start"), (176, "end")] {
        let navigated = call_tool(
            &mut server,
            id,
            "browser_navigate",
            json!({"url": page_server.ambiguous_drag_url(endpoint)}),
        );
        let snapshot = result_text(&navigated);
        let start = named_ref(snapshot, "button", "Draggable card");
        let end = named_ref(snapshot, "button", "Physical drop zone");
        let ambiguous = call_tool(
            &mut server,
            id + 1,
            "browser_drag",
            json!({"startTarget": start, "endTarget": end}),
        );
        let error = error_result_text(&ambiguous);
        assert!(
            error.contains("strict mode violation")
                && error.contains("2 elements")
                && error.contains("trying to drag"),
            "{endpoint} endpoint ambiguity did not preserve strict resolution: {error}"
        );
    }

    let (_, diagnostics) = server.finish();
    assert!(diagnostics.contains("browser actor: stopped"));
}

#[test]
fn real_stdio_parity_actions_inspection_wait_reload_and_close() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping parity action MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let mut server = ServerProcess::spawn();
    server.initialize();

    let navigated = call_tool(
        &mut server,
        200,
        "browser_navigate",
        json!({"url": page_server.parity_url()}),
    );
    let mut snapshot = result_text(&navigated).to_owned();
    assert!(snapshot.contains("Parity controls"));

    let resized = call_tool(
        &mut server,
        201,
        "browser_resize",
        json!({"width": 900.5, "height": 700.4}),
    );
    snapshot = result_text(&resized).to_owned();
    assert!(snapshot.contains("Parity controls"));

    let boxed = call_tool(
        &mut server,
        202,
        "browser_snapshot",
        json!({"boxes": true, "depth": 4}),
    );
    snapshot = result_text(&boxed).to_owned();
    assert!(snapshot.contains("[box="), "{snapshot}");

    let found = call_tool(
        &mut server,
        203,
        "browser_find",
        json!({"regex": "/Text target/i"}),
    );
    assert!(result_text(&found).contains("Match 1"));

    let refreshed = call_tool(&mut server, 204, "browser_snapshot", json!({}));
    snapshot = result_text(&refreshed).to_owned();
    let text_ref = named_ref(&snapshot, "textbox", "Text target");
    let typed = call_tool(
        &mut server,
        205,
        "browser_type",
        json!({"target": text_ref, "text": "native", "clear": true}),
    );
    snapshot = result_text(&typed).to_owned();
    assert!(snapshot.contains(r#"[value="native"]"#), "{snapshot}");

    let pressed = call_tool(&mut server, 206, "browser_press_key", json!({"key": "A"}));
    snapshot = result_text(&pressed).to_owned();
    assert!(snapshot.contains("native"), "{snapshot}");

    let select_ref = named_ref(&snapshot, "combobox", "Choice target");
    let selected = call_tool(
        &mut server,
        207,
        "browser_select_option",
        json!({"target": select_ref, "values": "beta"}),
    );
    snapshot = result_text(&selected).to_owned();
    let select_ref = named_ref(&snapshot, "combobox", "Choice target");
    let selected_value = call_tool(
        &mut server,
        208,
        "browser_evaluate",
        json!({"target": select_ref, "function": "(element) => element.value"}),
    );
    snapshot = result_text(&selected_value).to_owned();
    assert!(snapshot.starts_with("\"beta\""), "{snapshot}");

    let textbox = named_ref(&snapshot, "textbox", "Text target");
    let checkbox = named_ref(&snapshot, "checkbox", "Check target");
    let radio = named_ref(&snapshot, "radio", "Radio target");
    let slider = named_ref(&snapshot, "slider", "Range target");
    let filled = call_tool(
        &mut server,
        209,
        "browser_fill_form",
        json!({
            "fields": [
                {"target": textbox, "name": "text", "type": "textbox", "value": "batch"},
                {"target": checkbox, "name": "check", "type": "checkbox", "value": "true"},
                {"target": radio, "name": "radio", "type": "radio", "value": "true"},
                {"target": slider, "name": "range", "type": "slider", "value": "35"}
            ]
        }),
    );
    snapshot = result_text(&filled).to_owned();
    assert!(snapshot.contains(r#"[value="batch"]"#), "{snapshot}");
    assert!(
        snapshot
            .lines()
            .any(|line| line.contains("checkbox") && line.contains("[checked]")),
        "{snapshot}"
    );
    assert!(
        snapshot
            .lines()
            .any(|line| line.contains("radio") && line.contains("[checked]")),
        "{snapshot}"
    );

    let hover_ref = named_ref(&snapshot, "button", "Hover target");
    let hovered = call_tool(
        &mut server,
        210,
        "browser_hover",
        json!({"target": hover_ref}),
    );
    snapshot = result_text(&hovered).to_owned();
    let convergence_deadline = Instant::now() + Duration::from_secs(5);
    let mut poll_id = 211;
    while !snapshot.contains("Hover observed") {
        assert!(
            Instant::now() < convergence_deadline,
            "hover state did not converge: {snapshot}"
        );
        let polled = call_tool(&mut server, poll_id, "browser_snapshot", json!({}));
        poll_id += 1;
        snapshot = result_text(&polled).to_owned();
    }

    let drop_ref = named_ref(&snapshot, "button", "Drop target");
    let dropped = call_tool(
        &mut server,
        poll_id,
        "browser_drop",
        json!({"target": drop_ref, "data": {"text/plain": "payload"}}),
    );
    poll_id += 1;
    snapshot = result_text(&dropped).to_owned();
    let convergence_deadline = Instant::now() + Duration::from_secs(5);
    while !snapshot.contains("Dropped payload") {
        assert!(
            Instant::now() < convergence_deadline,
            "drop state did not converge: {snapshot}"
        );
        let polled = call_tool(&mut server, poll_id, "browser_snapshot", json!({}));
        poll_id += 1;
        snapshot = result_text(&polled).to_owned();
    }

    let status = call_tool(
        &mut server,
        poll_id,
        "browser_get_text",
        json!({"selector": "#status"}),
    );
    poll_id += 1;
    assert_eq!(result_text(&status), "Dropped payload");

    let scheduled = call_tool(
        &mut server,
        poll_id,
        "browser_evaluate",
        json!({
            "function": "() => { setTimeout(() => { document.querySelector('#delayed').textContent = 'Delayed ready'; }, 150); return 'scheduled'; }"
        }),
    );
    poll_id += 1;
    assert!(result_text(&scheduled).starts_with("\"scheduled\""));

    let waited = call_tool(
        &mut server,
        poll_id,
        "browser_wait_for",
        json!({"text": "Delayed ready", "timeout_ms": 5000}),
    );
    poll_id += 1;
    assert!(result_text(&waited).contains("Delayed ready"));

    let reloaded = call_tool(&mut server, poll_id, "browser_reload", json!({}));
    poll_id += 1;
    assert!(result_text(&reloaded).contains("Status waiting"));

    let closed = call_tool(&mut server, poll_id, "browser_close", json!({}));
    assert_eq!(result_text(&closed), "Browser closed.");
    server.finish();
}

#[test]
fn real_stdio_console_messages_filter_and_file_output_converge() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping console MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let workspace = std::env::temp_dir().join(format!(
        "rustwright-mcp-console-{}-{}",
        std::process::id(),
        Instant::now().elapsed().as_nanos()
    ));
    fs::create_dir(&workspace).expect("create console workspace");
    let workspace_text = workspace.to_string_lossy().to_string();
    let mut server =
        ServerProcess::spawn_with_env(&[("RUSTWRIGHT_MCP_WORKSPACE", &workspace_text)]);
    server.initialize();
    let _ = call_tool(
        &mut server,
        250,
        "browser_navigate",
        json!({"url": page_server.parity_url()}),
    );

    let deadline = Instant::now() + Duration::from_secs(5);
    let mut request_id = 251;
    let errors = loop {
        let response = call_tool(
            &mut server,
            request_id,
            "browser_console_messages",
            json!({"level": "error"}),
        );
        request_id += 1;
        if result_text(&response).contains("Parity console error") {
            break response;
        }
        assert!(
            Instant::now() < deadline,
            "console records did not converge: {}",
            result_text(&response)
        );
        thread::sleep(Duration::from_millis(25));
    };
    assert!(
        !result_text(&errors).contains("Parity console info"),
        "{}",
        result_text(&errors)
    );

    let all_visible = call_tool(
        &mut server,
        request_id,
        "browser_console_messages",
        json!({"level": "info"}),
    );
    request_id += 1;
    assert!(
        result_text(&all_visible).contains("ERROR")
            && result_text(&all_visible).contains("Parity console error")
            && result_text(&all_visible).contains("Parity console info"),
        "{}",
        result_text(&all_visible)
    );

    let written = call_tool(
        &mut server,
        request_id,
        "browser_console_messages",
        json!({"level": "info", "filename": "console.txt"}),
    );
    assert!(
        result_text(&written).contains("Console messages written"),
        "{}",
        result_text(&written)
    );
    let artifact = fs::read_to_string(workspace.join("console.txt")).expect("read console output");
    assert!(artifact.contains("Parity console error"), "{artifact}");
    server.finish();
    fs::remove_file(workspace.join("console.txt")).expect("remove console output");
    fs::remove_dir(workspace).expect("remove console workspace");
}

#[test]
fn real_stdio_network_list_detail_body_bounds_and_file_output_converge() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping network MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let workspace = std::env::temp_dir().join(format!(
        "rustwright-mcp-network-{}-{}",
        std::process::id(),
        STDIO_WORKSPACE_COUNTER.fetch_add(1, Ordering::SeqCst)
    ));
    fs::create_dir(&workspace).expect("create network workspace");
    let workspace_text = workspace.to_string_lossy().to_string();
    let mut server =
        ServerProcess::spawn_with_env(&[("RUSTWRIGHT_MCP_WORKSPACE", &workspace_text)]);
    server.initialize();
    let navigated = call_tool(
        &mut server,
        270,
        "browser_navigate",
        json!({"url": page_server.network_url()}),
    );
    assert!(
        result_text(&navigated).contains("Network records"),
        "{}",
        result_text(&navigated)
    );

    let deadline = Instant::now() + Duration::from_secs(10);
    let mut request_id = 271;
    let all_requests = loop {
        let response = call_tool(
            &mut server,
            request_id,
            "browser_network_requests",
            json!({"static": true}),
        );
        request_id += 1;
        let text = result_text(&response);
        if text
            .lines()
            .any(|line| line.contains(" 200 ") && line.contains("/network-static.svg"))
            && text
                .lines()
                .any(|line| line.contains(" 200 ") && line.contains("/api/data"))
            && text
                .lines()
                .any(|line| line.contains(" 200 ") && line.contains("/large-text"))
        {
            break response;
        }
        assert!(
            Instant::now() < deadline,
            "network records did not converge: {text}"
        );
        thread::sleep(Duration::from_millis(25));
    };
    let all_text = result_text(&all_requests);
    let index_for = |needle: &str| {
        all_text
            .lines()
            .find(|line| line.contains(needle))
            .and_then(|line| line.strip_prefix('['))
            .and_then(|line| line.split_once(']'))
            .and_then(|(index, _)| index.parse::<u64>().ok())
            .unwrap_or_else(|| panic!("missing network index for {needle}: {all_text}"))
    };
    let api_index = index_for("/api/data");
    let large_index = index_for("/large-text");

    let filtered = call_tool(
        &mut server,
        request_id,
        "browser_network_requests",
        json!({"filter": "api/data$|large-text$"}),
    );
    request_id += 1;
    let filtered_text = result_text(&filtered);
    assert!(filtered_text.contains("/api/data"), "{filtered_text}");
    assert!(filtered_text.contains("/large-text"), "{filtered_text}");
    assert!(
        !filtered_text.contains("/network-static.svg"),
        "{filtered_text}"
    );

    let without_static = call_tool(
        &mut server,
        request_id,
        "browser_network_requests",
        json!({}),
    );
    request_id += 1;
    assert!(
        !result_text(&without_static).contains("/network-static.svg"),
        "{}",
        result_text(&without_static)
    );

    let invalid_filter = call_tool(
        &mut server,
        request_id,
        "browser_network_requests",
        json!({"filter": "["}),
    );
    request_id += 1;
    assert!(
        error_result_text(&invalid_filter).contains("invalid network filter regex"),
        "{}",
        error_result_text(&invalid_filter)
    );

    let detail = loop {
        let response = call_tool(
            &mut server,
            request_id,
            "browser_network_request",
            json!({"index": api_index}),
        );
        request_id += 1;
        if result_text(&response).contains("network-response-body") {
            break response;
        }
        assert!(
            Instant::now() < deadline,
            "network detail did not converge: {}",
            result_text(&response)
        );
        thread::sleep(Duration::from_millis(25));
    };
    let detail_text = result_text(&detail);
    let detail_lower = detail_text.to_ascii_lowercase();
    assert!(
        detail_text.contains("#### request-headers"),
        "{detail_text}"
    );
    assert!(
        detail_lower.contains("x-network-request") && detail_text.contains("request-payload-123"),
        "{detail_text}"
    );
    assert!(
        detail_text.contains("#### response-headers")
            && detail_lower.contains("x-network-response"),
        "{detail_text}"
    );
    assert!(
        detail_text.contains("#### response-body") && detail_text.contains("network-response-body"),
        "{detail_text}"
    );

    let bounded = loop {
        let response = call_tool(
            &mut server,
            request_id,
            "browser_network_request",
            json!({"index": large_index, "part": "response-body"}),
        );
        request_id += 1;
        if result_text(&response).contains("truncated to 65536 bytes inline") {
            break response;
        }
        assert!(
            Instant::now() < deadline,
            "bounded network body did not converge: {}",
            result_text(&response)
        );
        thread::sleep(Duration::from_millis(25));
    };
    let bounded_text = result_text(&bounded);
    assert!(bounded_text.starts_with("#### response-body\n"));
    assert!(
        bounded_text.len() < 66 * 1024,
        "inline body was not bounded"
    );

    let written_list = call_tool(
        &mut server,
        request_id,
        "browser_network_requests",
        json!({"static": true, "filename": "network-list.txt"}),
    );
    request_id += 1;
    assert!(
        result_text(&written_list).contains("Network requests written"),
        "{}",
        result_text(&written_list)
    );
    let list_artifact =
        fs::read_to_string(workspace.join("network-list.txt")).expect("read network list output");
    assert!(list_artifact.contains("/api/data"), "{list_artifact}");
    assert!(
        list_artifact.contains("/network-static.svg"),
        "{list_artifact}"
    );

    let written_detail = call_tool(
        &mut server,
        request_id,
        "browser_network_request",
        json!({
            "index": large_index,
            "part": "response-body",
            "filename": "network-detail.txt"
        }),
    );
    request_id += 1;
    assert!(
        result_text(&written_detail).contains("Network request"),
        "{}",
        result_text(&written_detail)
    );
    let detail_artifact = fs::read_to_string(workspace.join("network-detail.txt"))
        .expect("read network detail output");
    assert!(
        detail_artifact.len() > 70 * 1024 && !detail_artifact.contains("truncated"),
        "filename body should contain the complete 70 KiB fixture"
    );

    let _ = call_tool(
        &mut server,
        request_id,
        "browser_navigate",
        json!({"url": page_server.history_url("a")}),
    );
    request_id += 1;
    let previous = call_tool(
        &mut server,
        request_id,
        "browser_network_request",
        json!({"index": api_index, "part": "request-headers"}),
    );
    assert!(
        error_result_text(&previous).contains("from a previous navigation"),
        "{}",
        error_result_text(&previous)
    );

    server.finish();
    fs::remove_file(workspace.join("network-list.txt")).expect("remove network list output");
    fs::remove_file(workspace.join("network-detail.txt")).expect("remove network detail output");
    fs::remove_dir(workspace).expect("remove network workspace");
}

#[test]
fn real_stdio_dialog_returns_fast_surfaces_modal_and_converges_after_accept() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping dialog MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let mut server = ServerProcess::spawn();
    server.initialize();
    let navigated = call_tool(
        &mut server,
        300,
        "browser_navigate",
        json!({"url": page_server.parity_url()}),
    );
    let dialog_ref = named_ref(result_text(&navigated), "button", "Dialog target");

    let started = Instant::now();
    let clicked = call_tool(
        &mut server,
        301,
        "browser_click",
        json!({"target": dialog_ref}),
    );
    let click_elapsed = started.elapsed();
    assert!(
        click_elapsed < Duration::from_secs(5),
        "dialog-triggering click did not return promptly: {click_elapsed:?}"
    );
    let modal = result_text(&clicked);
    assert!(modal.contains("### Modal"), "{modal}");
    assert!(modal.contains("Parity dialog"), "{modal}");

    let deferred_started = Instant::now();
    let deferred = call_tool(&mut server, 302, "browser_snapshot", json!({}));
    assert!(
        deferred_started.elapsed() < Duration::from_secs(2),
        "snapshot did not defer promptly while dialog was pending"
    );
    assert!(
        result_text(&deferred).contains("deferred until the pending modal"),
        "{}",
        result_text(&deferred)
    );

    let handled = call_tool(
        &mut server,
        303,
        "browser_handle_dialog",
        json!({"accept": true}),
    );
    assert_eq!(result_text(&handled), "Accepted the pending dialog.");

    let deadline = Instant::now() + Duration::from_secs(10);
    let mut id = 304;
    loop {
        let snapshot = call_tool(&mut server, id, "browser_snapshot", json!({}));
        id += 1;
        if result_text(&snapshot).contains("Dialog handled") {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "post-dialog page state did not converge: {}",
            result_text(&snapshot)
        );
        thread::sleep(Duration::from_millis(25));
    }
    server.finish();
}

#[test]
fn real_stdio_file_upload_sets_confined_file_releases_failure_and_cancels_with_empty_list() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping file-upload MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let workspace = std::env::temp_dir().join(format!(
        "rustwright-mcp-upload-{}-{}",
        std::process::id(),
        STDIO_WORKSPACE_COUNTER.fetch_add(1, Ordering::SeqCst)
    ));
    fs::create_dir(&workspace).expect("create upload workspace");
    fs::write(workspace.join("upload.txt"), b"uploaded through chooser")
        .expect("write upload fixture");
    fs::write(workspace.join("second.txt"), b"second chooser file")
        .expect("write second upload fixture");
    let workspace_text = workspace.to_string_lossy().to_string();
    let mut server =
        ServerProcess::spawn_with_env(&[("RUSTWRIGHT_MCP_WORKSPACE", &workspace_text)]);
    server.initialize();
    let navigated = call_tool(
        &mut server,
        330,
        "browser_navigate",
        json!({"url": page_server.parity_url()}),
    );
    let upload_ref = named_ref(result_text(&navigated), "textbox", "Upload target");

    let clicked = call_tool(
        &mut server,
        331,
        "browser_click",
        json!({"target": upload_ref}),
    );
    assert!(
        result_text(&clicked).contains("File chooser pending: single file only"),
        "{}",
        result_text(&clicked)
    );

    let multiplicity = call_tool(
        &mut server,
        332,
        "browser_file_upload",
        json!({"paths": ["upload.txt", "second.txt"]}),
    );
    assert!(
        error_result_text(&multiplicity)
            .starts_with("the pending file chooser accepts only one file"),
        "{}",
        error_result_text(&multiplicity)
    );

    let after_failure = call_tool(&mut server, 333, "browser_snapshot", json!({}));
    let upload_ref = named_ref(result_text(&after_failure), "textbox", "Upload target");
    let reopened = call_tool(
        &mut server,
        334,
        "browser_click",
        json!({"target": upload_ref}),
    );
    assert!(
        result_text(&reopened).contains("Call browser_file_upload"),
        "{}",
        result_text(&reopened)
    );
    let uploaded = call_tool(
        &mut server,
        335,
        "browser_file_upload",
        json!({"paths": ["upload.txt"]}),
    );
    assert!(
        result_text(&uploaded).starts_with("Uploaded 1 file(s) through the pending chooser."),
        "{}",
        result_text(&uploaded)
    );
    let received = call_tool(
        &mut server,
        336,
        "browser_evaluate",
        json!({
            "function": "() => { const files = document.querySelector('#upload').files; return { count: files.length, name: files[0] && files[0].name }; }"
        }),
    );
    assert!(
        result_text(&received).starts_with(r#"{"count":1,"name":"upload.txt"}"#),
        "{}",
        result_text(&received)
    );

    let upload_ref = named_ref(result_text(&received), "textbox", "Upload target");
    let cancel_opened = call_tool(
        &mut server,
        337,
        "browser_click",
        json!({"target": upload_ref}),
    );
    assert!(
        result_text(&cancel_opened).contains("File chooser pending"),
        "{}",
        result_text(&cancel_opened)
    );
    let cancelled = call_tool(
        &mut server,
        338,
        "browser_file_upload",
        json!({"paths": []}),
    );
    assert!(
        result_text(&cancelled).starts_with("Cancelled the pending file chooser."),
        "{}",
        result_text(&cancelled)
    );
    let upload_ref = named_ref(result_text(&cancelled), "textbox", "Upload target");
    let reopened_after_cancel = call_tool(
        &mut server,
        339,
        "browser_click",
        json!({"target": upload_ref}),
    );
    assert!(
        result_text(&reopened_after_cancel).contains("File chooser pending"),
        "{}",
        result_text(&reopened_after_cancel)
    );
    let cancelled_again = call_tool(
        &mut server,
        340,
        "browser_file_upload",
        json!({"paths": null}),
    );
    assert!(
        result_text(&cancelled_again).starts_with("Cancelled the pending file chooser."),
        "{}",
        result_text(&cancelled_again)
    );

    server.finish();
    fs::remove_file(workspace.join("upload.txt")).expect("remove upload fixture");
    fs::remove_file(workspace.join("second.txt")).expect("remove second upload fixture");
    fs::remove_dir(workspace).expect("remove upload workspace");
}

#[test]
fn real_stdio_tabs_list_new_select_and_close() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping tabs MCP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let mut server = ServerProcess::spawn();
    server.initialize();
    call_tool(
        &mut server,
        400,
        "browser_navigate",
        json!({"url": page_server.url()}),
    );

    let listed = call_tool(&mut server, 401, "browser_tabs", json!({"action": "list"}));
    assert!(result_text(&listed).contains("- 0:"));
    assert!(
        !result_text(&listed).contains("- 1:"),
        "{}",
        result_text(&listed)
    );

    let opened = call_tool(
        &mut server,
        402,
        "browser_tabs",
        json!({"action": "new", "url": page_server.parity_url()}),
    );
    assert!(result_text(&opened).contains("Parity controls"));
    assert!(result_text(&opened).contains("- 1:"));
    assert!(
        !result_text(&opened).contains("- 2:"),
        "{}",
        result_text(&opened)
    );

    let selected = call_tool(
        &mut server,
        403,
        "browser_tabs",
        json!({"action": "select", "index": 0}),
    );
    assert!(result_text(&selected).contains("Activate feature"));

    let closed = call_tool(
        &mut server,
        404,
        "browser_tabs",
        json!({"action": "close", "index": 1}),
    );
    assert!(result_text(&closed).contains("- 0:"));
    assert!(
        !result_text(&closed).contains("- 1:"),
        "{}",
        result_text(&closed)
    );
    server.finish();
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
    let _page_a = converge_snapshot_text(&mut server, page_a, 210, "History page A");

    server.send(json!({
        "jsonrpc":"2.0","id":22,"method":"tools/call",
        "params":{
            "name":"browser_navigate",
            "arguments":{"url":page_server.history_url("b")}
        }
    }));
    let page_b = server.receive();
    let _page_b = converge_snapshot_text(&mut server, page_b, 220, "History page B");

    server.send(json!({
        "jsonrpc":"2.0","id":23,"method":"tools/call",
        "params":{"name":"browser_navigate_back","arguments":{}}
    }));
    let back = server.receive();
    let back = converge_snapshot_text(&mut server, back, 230, "History page A");
    let back_snapshot = result_text(&back);
    assert!(back_snapshot.contains("History page A"), "{back_snapshot}");
    assert!(!back_snapshot.contains("History page B"), "{back_snapshot}");

    server.send(json!({
        "jsonrpc":"2.0","id":24,"method":"tools/call",
        "params":{"name":"browser_navigate_forward","arguments":{}}
    }));
    let forward = server.receive();
    let forward = converge_snapshot_text(&mut server, forward, 240, "History page B");
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
    // Generous tool budget and elapsed bounds at half of it: under a loaded,
    // fully parallel suite the tight budget produced false failures while a
    // genuine frame-wait regression still blows through the halved bound.
    let mut server = ServerProcess::spawn_with_env(&[("RUSTWRIGHT_MCP_TOOL_TIMEOUT_MS", "10000")]);
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
        result_text(&spa_second).contains("SPA location:"),
        "{}",
        result_text(&spa_second)
    );
    let mut poll_id = 2600;
    poll_snapshot_until(
        &mut server,
        result_text(&spa_second).to_owned(),
        &mut poll_id,
        "SPA location: /history-spa?step=two",
    );

    let started = Instant::now();
    server.send(json!({
        "jsonrpc":"2.0","id":27,"method":"tools/call",
        "params":{"name":"browser_navigate_back","arguments":{}}
    }));
    let spa_back = server.receive();
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "pushState back navigation ran toward the 10-second tool timeout: {:?}",
        started.elapsed()
    );
    poll_snapshot_until(
        &mut server,
        result_text(&spa_back).to_owned(),
        &mut poll_id,
        "SPA location: /history-spa?step=one",
    );

    server.send(json!({
        "jsonrpc":"2.0","id":28,"method":"tools/call",
        "params":{"name":"browser_navigate_forward","arguments":{}}
    }));
    let spa_forward = server.receive();
    poll_snapshot_until(
        &mut server,
        result_text(&spa_forward).to_owned(),
        &mut poll_id,
        "SPA location: /history-spa?step=two",
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
    poll_snapshot_until(
        &mut server,
        result_text(&hash_first).to_owned(),
        &mut poll_id,
        "Hash location: #first",
    );

    server.send(json!({
        "jsonrpc":"2.0","id":31,"method":"tools/call",
        "params":{
            "name":"browser_navigate",
            "arguments":{"url":format!("{}#second", page_server.hash_history_url())}
        }
    }));
    let hash_second = server.receive();
    poll_snapshot_until(
        &mut server,
        result_text(&hash_second).to_owned(),
        &mut poll_id,
        "Hash location: #second",
    );

    server.send(json!({
        "jsonrpc":"2.0","id":32,"method":"tools/call",
        "params":{"name":"browser_navigate_back","arguments":{}}
    }));
    let hash_back = server.receive();
    poll_snapshot_until(
        &mut server,
        result_text(&hash_back).to_owned(),
        &mut poll_id,
        "Hash location: #first",
    );

    server.send(json!({
        "jsonrpc":"2.0","id":33,"method":"tools/call",
        "params":{"name":"browser_navigate_forward","arguments":{}}
    }));
    let hash_forward = server.receive();
    poll_snapshot_until(
        &mut server,
        result_text(&hash_forward).to_owned(),
        &mut poll_id,
        "Hash location: #second",
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
    let mut poll_id = 3200;
    let down_snapshot = poll_snapshot_state(
        &mut server,
        result_text(&down).to_owned(),
        &mut poll_id,
        "scroll position near 600",
        |snapshot| scroll_y(snapshot).abs_diff(600) <= 75,
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
    let target_snapshot = poll_snapshot_state(
        &mut server,
        result_text(&target).to_owned(),
        &mut poll_id,
        "far target scrolled beyond 4000",
        |snapshot| scroll_y(snapshot) >= 4000,
    );
    let target_y = scroll_y(&target_snapshot);

    server.send(json!({
        "jsonrpc":"2.0","id":34,"method":"tools/call",
        "params":{
            "name":"browser_scroll",
            "arguments":{"direction":"up"}
        }
    }));
    let up = server.receive();
    let up_snapshot = poll_snapshot_state(
        &mut server,
        result_text(&up).to_owned(),
        &mut poll_id,
        "default upward scroll decreased the position",
        |snapshot| scroll_y(snapshot) < target_y,
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
    // The regression this guards is a settle that waits on animation frames a
    // page reporting itself hidden never delivers, which surfaces as the
    // scroll consuming the entire tool timeout. Keep the timeout generous and
    // the elapsed bound at half of it so a loaded runner cannot produce a
    // false failure: a genuine frame wait still blows through the bound.
    let mut server = ServerProcess::spawn_with_options(
        Some((&endpoint, &headers, 10_000)),
        &[("RUSTWRIGHT_MCP_TOOL_TIMEOUT_MS", "10000")],
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
        elapsed < Duration::from_secs(5),
        "hidden-page viewport scroll ran toward the 10-second tool timeout, \
         which means the settle waited on animation frames: {elapsed:?}"
    );
    assert!(
        scrolled_snapshot.contains("Scroll Y: "),
        "post-scroll snapshot lost the scroll readout:\n{scrolled_snapshot}"
    );

    // The fixture readout is updated by the page's own scroll listener, which
    // runs at the renderer's next rendering step. A page that claims to be
    // hidden gets a zero-length settle by design (real hidden pages produce no
    // frames, so waiting would recreate the hang this test guards against), so
    // the scroll response cannot promise the listener has run yet. Assert the
    // readout converges instead of asserting one unguaranteed interleaving.
    let mut readout = scroll_y(&scrolled_snapshot);
    let mut next_id = 37;
    let convergence_deadline = Instant::now() + Duration::from_secs(15);
    while readout.abs_diff(600) > 75 {
        assert!(
            Instant::now() < convergence_deadline,
            "hidden-page viewport scroll never became visible to the page's \
             scroll listener; last readout {readout}"
        );
        thread::sleep(Duration::from_millis(100));
        server.send(json!({
            "jsonrpc":"2.0","id":next_id,"method":"tools/call",
            "params":{"name":"browser_snapshot","arguments":{}}
        }));
        let refreshed = server.receive();
        readout = scroll_y(result_text(&refreshed));
        next_id += 1;
    }

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
