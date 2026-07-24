use std::{
    collections::{HashMap, VecDeque},
    io::{BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

use rustwright::{GotoOptions, LaunchOptions, chromium};
use serde_json::{Value, json};

const PAGE_HTML: &str = r#"<!doctype html>
<html><head><title>Independent remote validation</title></head>
<body>
  <input aria-label="Validation secret" type="password" value="validation-password-marker">
  <button onclick="this.textContent='Validation clicked'; document.getElementById('state').textContent='done'">Validate action</button>
  <div id="state">waiting</div>
</body></html>"#;
const REMOTE_STATE_DEADLINE: Duration = Duration::from_secs(45);
const REMOTE_STATE_POLL_INTERVAL: Duration = Duration::from_millis(100);

struct PageServer {
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl PageServer {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind validation MCP page server");
        listener
            .set_nonblocking(true)
            .expect("set validation MCP page server nonblocking");
        let addr = listener.local_addr().expect("validation MCP page address");
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !thread_stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut stream, _)) => serve_page(&mut stream),
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("validation MCP page accept failed: {error}"),
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
        format!("http://{}/validation", self.addr)
    }
}

impl Drop for PageServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let _ = TcpStream::connect(self.addr);
        if let Some(thread) = self.thread.take() {
            thread.join().expect("join validation MCP page server");
        }
    }
}

fn serve_page(stream: &mut TcpStream) {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("set validation MCP page read timeout");
    let mut request = [0_u8; 2048];
    let _ = stream.read(&mut request);
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{PAGE_HTML}",
        PAGE_HTML.len()
    );
    stream
        .write_all(response.as_bytes())
        .expect("write validation MCP page response");
}

struct VersionStub {
    endpoint: String,
    request: mpsc::Receiver<String>,
    thread: Option<thread::JoinHandle<()>>,
}

impl VersionStub {
    fn start(ws_endpoint: String) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind validation MCP resolver");
        let addr = listener
            .local_addr()
            .expect("validation MCP resolver address");
        let (request_tx, request) = mpsc::sync_channel(1);
        let thread = thread::spawn(move || {
            let (mut stream, _) = listener
                .accept()
                .expect("accept validation MCP resolver request");
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("set validation MCP resolver timeout");
            let mut bytes = Vec::new();
            let mut buffer = [0_u8; 1024];
            loop {
                let read = stream
                    .read(&mut buffer)
                    .expect("read validation MCP resolver request");
                if read == 0 {
                    break;
                }
                bytes.extend_from_slice(&buffer[..read]);
                if bytes.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            request_tx
                .send(String::from_utf8(bytes).expect("validation MCP HTTP request is UTF-8"))
                .expect("record validation MCP resolver request");
            let body = json!({"webSocketDebuggerUrl": ws_endpoint}).to_string();
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream
                .write_all(response.as_bytes())
                .expect("write validation MCP resolver response");
        });
        Self {
            endpoint: format!("http://{addr}"),
            request,
            thread: Some(thread),
        }
    }

    fn finish(mut self) -> String {
        let deadline = Instant::now() + REMOTE_STATE_DEADLINE;
        let request = loop {
            match self.request.try_recv() {
                Ok(request) => break request,
                Err(mpsc::TryRecvError::Empty) if Instant::now() < deadline => {
                    thread::sleep(REMOTE_STATE_POLL_INTERVAL);
                }
                Err(mpsc::TryRecvError::Empty) => {
                    panic!("validation MCP resolver request not observed before deadline")
                }
                Err(mpsc::TryRecvError::Disconnected) => {
                    panic!("validation MCP resolver stopped before recording request")
                }
            }
        };
        self.thread
            .take()
            .expect("validation MCP resolver thread")
            .join()
            .expect("join validation MCP resolver");
        request
    }
}

struct ServerProcess {
    child: Child,
    input: Option<ChildStdin>,
    output: BufReader<ChildStdout>,
    transcript: Vec<String>,
}

impl ServerProcess {
    fn spawn_remote(endpoint: &str, headers: &Value) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_mcp-rs"))
            .env_remove("RUSTWRIGHT_MCP_CDP_ENDPOINT")
            .env_remove("RUSTWRIGHT_MCP_CDP_HEADERS")
            .env_remove("RUSTWRIGHT_MCP_CDP_TIMEOUT_MS")
            .env("RUSTWRIGHT_MCP_CDP_ENDPOINT", endpoint)
            .env("RUSTWRIGHT_MCP_CDP_HEADERS", headers.to_string())
            .env("RUSTWRIGHT_MCP_CDP_TIMEOUT_MS", "10000")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn built validation MCP binary");
        let input = child.stdin.take().expect("validation MCP stdin");
        let output = BufReader::new(child.stdout.take().expect("validation MCP stdout"));
        Self {
            child,
            input: Some(input),
            output,
            transcript: Vec::new(),
        }
    }

    fn send(&mut self, message: Value) {
        let line = serde_json::to_string(&message).expect("serialize validation MCP frame");
        self.transcript.push(format!("C> {line}"));
        let input = self.input.as_mut().expect("validation MCP stdin open");
        writeln!(input, "{line}").expect("send validation MCP frame");
        input.flush().expect("flush validation MCP frame");
    }

    fn receive(&mut self) -> Value {
        let mut line = String::new();
        let bytes = self
            .output
            .read_line(&mut line)
            .expect("read validation MCP frame");
        assert!(bytes > 0, "validation MCP stdout closed before response");
        let line = line.trim_end();
        self.transcript.push(format!("S> {line}"));
        let message: Value = serde_json::from_str(line).expect("validation MCP stdout is JSON");
        assert_eq!(message["jsonrpc"], "2.0");
        message
    }

    fn initialize(&mut self) {
        self.send(json!({
            "jsonrpc":"2.0",
            "id":1,
            "method":"initialize",
            "params":{
                "protocolVersion":"2025-06-18",
                "capabilities":{},
                "clientInfo":{"name":"independent-validator","version":"1"}
            }
        }));
        let initialized = self.receive();
        assert_eq!(initialized["id"], 1);
        assert!(initialized["result"]["capabilities"]["tools"].is_object());
        self.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
    }

    fn finish(mut self) -> (Vec<String>, String) {
        self.input.take();
        let deadline = Instant::now() + Duration::from_secs(15);
        loop {
            if let Some(status) = self.child.try_wait().expect("poll validation MCP binary") {
                assert!(status.success(), "validation MCP binary failed: {status}");
                break;
            }
            if Instant::now() >= deadline {
                let _ = self.child.kill();
                panic!("validation MCP binary did not stop after stdin EOF");
            }
            thread::sleep(Duration::from_millis(25));
        }
        let mut diagnostics = String::new();
        self.child
            .stderr
            .take()
            .expect("validation MCP stderr")
            .read_to_string(&mut diagnostics)
            .expect("read validation MCP diagnostics");
        (std::mem::take(&mut self.transcript), diagnostics)
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

fn process_rows() -> Vec<(u32, u32, String)> {
    let output = Command::new("ps")
        .args(["-axo", "pid=,ppid=,comm="])
        .output()
        .expect("run validation MCP process accounting");
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
    let mut by_parent: HashMap<u32, Vec<(u32, String)>> = HashMap::new();
    for (pid, ppid, command) in process_rows() {
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

fn result_text(message: &Value) -> &str {
    assert_eq!(
        message["result"]["isError"], false,
        "expected a success frame but the tool returned an error frame: {message}"
    );
    message["result"]["content"][0]["text"]
        .as_str()
        .expect("validation MCP success text")
}

fn button_ref(snapshot: &str) -> String {
    let line = snapshot
        .lines()
        .find(|line| line.contains("- button") && line.contains("Validate action"))
        .unwrap_or_else(|| panic!("validation button absent from snapshot:\n{snapshot}"));
    let start = line.find("[ref=").expect("validation button ref start") + 5;
    let end = line[start..].find(']').expect("validation button ref end") + start;
    line[start..end].to_owned()
}

#[test]
fn validation_built_binary_remote_mode_survives_death_never_falls_back_and_sanitizes_full_frame() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping validation MCP remote test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let owner = chromium()
        .launch(LaunchOptions::default().arg("--remote-debugging-port=0"))
        .expect("launch independent remote browser");
    for page in owner
        .pages()
        .expect("list independent remote startup pages")
    {
        page.close(Default::default())
            .expect("close independent remote startup page");
    }
    let existing = owner
        .new_page()
        .expect("create adopted independent remote page");
    let other = owner
        .new_page()
        .expect("create other independent remote page");
    existing
        .goto(
            &page_server.url(),
            GotoOptions::default().wait_until("load").timeout(10_000.0),
        )
        .expect("prime independent remote page");
    assert_eq!(
        owner.pages().expect("count independent remote pages").len(),
        2
    );

    let ws_endpoint = owner.ws_endpoint();
    let stub = VersionStub::start(ws_endpoint.clone());
    let configured_endpoint = stub.endpoint.clone();
    let header_name = "x-validation-mcp-header";
    let header_value = "validation-mcp-header-secret";
    let mut server = ServerProcess::spawn_remote(
        &configured_endpoint,
        &json!({"x-validation-mcp-header": header_value}),
    );
    server.initialize();

    server.send(json!({
        "jsonrpc":"2.0","id":2,"method":"tools/call",
        "params":{"name":"browser_navigate","arguments":{"url":page_server.url()}}
    }));
    let recorded_request = stub.finish().to_ascii_lowercase();
    assert!(recorded_request.starts_with("get /json/version http/1.1"));
    assert!(recorded_request.contains(&format!("{header_name}: {header_value}")));
    assert!(
        descendants(server.child.id()).is_empty(),
        "remote MCP mode must have no browser subprocess descendants after attach"
    );
    assert_eq!(
        owner.pages().expect("count pages after MCP adoption").len(),
        2,
        "MCP adoption must not create a page when existing pages are present"
    );

    let navigated = server.receive();
    let navigated_text = result_text(&navigated);
    assert!(navigated_text.contains("Validate action"));
    assert!(navigated_text.contains("[ref=e"));
    assert!(navigated_text.contains("[value=••••••]"));
    assert!(!navigated_text.contains("validation-password-marker"));

    server.send(json!({
        "jsonrpc":"2.0","id":3,"method":"tools/call",
        "params":{"name":"browser_snapshot","arguments":{}}
    }));
    let snapshot = server.receive();
    let target = button_ref(result_text(&snapshot));
    server.send(json!({
        "jsonrpc":"2.0","id":4,"method":"tools/call",
        "params":{"name":"browser_click","arguments":{"target":target}}
    }));
    let clicked = server.receive();
    assert!(result_text(&clicked).contains("Validation clicked"));
    assert!(result_text(&clicked).contains("done"));
    assert!(
        descendants(server.child.id()).is_empty(),
        "remote MCP mode must still have no browser subprocess descendants after tools"
    );
    let other_title = other
        .title(Default::default())
        .expect("other remote page responds before death");
    assert!(other_title.is_empty() || other_title == "Independent remote validation");

    owner
        .close()
        .expect("terminate independent remote browser owner");
    let remote_death_deadline = Instant::now() + REMOTE_STATE_DEADLINE;
    let server_pid = server.child.id();
    let unreachable = loop {
        assert!(
            descendants(server_pid).is_empty(),
            "remote MCP mode launched a subprocess while polling for remote death"
        );
        server.send(json!({
            "jsonrpc":"2.0","id":5,"method":"tools/call",
            "params":{"name":"browser_snapshot","arguments":{}}
        }));
        let response = server.receive();
        assert_eq!(response["id"], 5);
        assert!(
            descendants(server_pid).is_empty(),
            "remote MCP mode launched a subprocess while polling for remote death"
        );
        if response["result"]["isError"] == true
            && response["result"]["content"][0]["text"]
                == "remote CDP session unreachable — restart or reconfigure"
        {
            break response;
        }
        assert!(
            Instant::now() < remote_death_deadline,
            "remote MCP mode did not observe remote browser death before deadline; last response: {response}"
        );
        thread::sleep(REMOTE_STATE_POLL_INTERVAL);
    };
    assert_eq!(unreachable["result"]["isError"], true);
    assert_eq!(
        unreachable["result"]["content"][0]["text"],
        "remote CDP session unreachable — restart or reconfigure"
    );
    let full_response = serde_json::to_string(&unreachable).expect("serialize full error response");
    for marker in [
        configured_endpoint.as_str(),
        ws_endpoint.as_str(),
        header_name,
        header_value,
    ] {
        assert!(
            !full_response.contains(marker),
            "full MCP error response leaked validation marker"
        );
    }
    assert!(
        descendants(server.child.id()).is_empty(),
        "remote MCP mode must have no subprocess descendants after remote death"
    );

    server.send(json!({"jsonrpc":"2.0","id":6,"method":"ping","params":{}}));
    let pong = server.receive();
    assert_eq!(pong["id"], 6);
    assert!(pong["result"].is_object());

    let (_transcript, diagnostics) = server.finish();
    for marker in [
        configured_endpoint.as_str(),
        ws_endpoint.as_str(),
        header_name,
        header_value,
    ] {
        assert!(!diagnostics.contains(marker));
    }
    assert!(!diagnostics.contains("launching Chromium"));
    assert!(diagnostics.contains("browser actor: stopped"));

    println!("validation remote HTTP header observed: true");
    println!("validation remote page count before/after adoption: 2/2");
    println!("validation server descendant counts after attach/tools/death: 0/0/0");
    println!("validation full unreachable frame excluded endpoint and header name/value: true");
    println!("validation ping after remote death: true");
    println!("validation diagnostics confirmed no local launch: true");
}
