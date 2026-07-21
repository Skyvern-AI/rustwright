use std::{
    collections::{HashMap, HashSet, VecDeque},
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    process::Command,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};

use rustwright::{chromium, ActionOptions, ConnectOptions, Error, GotoOptions, LaunchOptions};
use serde_json::json;

struct PageServer {
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl PageServer {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind validation page server");
        listener
            .set_nonblocking(true)
            .expect("set validation page server nonblocking");
        let addr = listener
            .local_addr()
            .expect("validation page server address");
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !thread_stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut stream, _)) => serve_page(&mut stream),
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("validation page server accept failed: {error}"),
                }
            }
        });
        Self {
            addr,
            stop,
            thread: Some(thread),
        }
    }

    fn url(&self, name: &str) -> String {
        format!("http://{}/{}", self.addr, name)
    }
}

impl Drop for PageServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let _ = TcpStream::connect(self.addr);
        if let Some(thread) = self.thread.take() {
            thread.join().expect("join validation page server");
        }
    }
}

fn serve_page(stream: &mut TcpStream) {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("set validation page read timeout");
    let mut bytes = [0_u8; 2048];
    let read = stream.read(&mut bytes).unwrap_or(0);
    let request = String::from_utf8_lossy(&bytes[..read]);
    let path = request
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .unwrap_or("/");
    let name = path.trim_start_matches('/');
    let body = format!("<!doctype html><title>{name}</title><main>{name}</main>");
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream
        .write_all(response.as_bytes())
        .expect("write validation page response");
}

struct VersionStub {
    endpoint: String,
    request: mpsc::Receiver<String>,
    thread: Option<thread::JoinHandle<()>>,
}

impl VersionStub {
    fn start(ws_endpoint: String) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind validation version stub");
        let addr = listener
            .local_addr()
            .expect("validation version stub address");
        let (request_tx, request) = mpsc::sync_channel(1);
        let thread = thread::spawn(move || {
            let (mut stream, _) = listener
                .accept()
                .expect("accept validation version request");
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("set validation version request timeout");
            let request = read_http_headers(&mut stream);
            request_tx
                .send(request)
                .expect("record validation version request");
            let body = json!({"webSocketDebuggerUrl": ws_endpoint}).to_string();
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream
                .write_all(response.as_bytes())
                .expect("write validation version response");
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
            .expect("receive validation version request");
        self.thread
            .take()
            .expect("validation version stub thread")
            .join()
            .expect("join validation version stub");
        request
    }
}

fn read_http_headers(stream: &mut TcpStream) -> String {
    let mut request = Vec::new();
    let mut buffer = [0_u8; 1024];
    loop {
        let read = stream.read(&mut buffer).expect("read HTTP request");
        if read == 0 {
            break;
        }
        request.extend_from_slice(&buffer[..read]);
        if request.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }
    String::from_utf8(request).expect("HTTP request is UTF-8")
}

fn process_rows() -> Vec<(u32, u32, String)> {
    let output = Command::new("ps")
        .args(["-axo", "pid=,ppid=,comm="])
        .output()
        .expect("run process accounting");
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

fn live_pids() -> HashSet<u32> {
    process_rows().into_iter().map(|(pid, _, _)| pid).collect()
}

fn wait_until(timeout: Duration, condition: impl Fn() -> bool) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if condition() {
            return true;
        }
        thread::sleep(Duration::from_millis(25));
    }
    condition()
}

#[test]
fn validation_facade_detach_preserves_process_all_pages_and_both_navigation_paths() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping validation facade test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let before_launch: HashSet<u32> = descendants(std::process::id())
        .into_iter()
        .map(|(pid, _)| pid)
        .collect();
    let owner = chromium()
        .launch(LaunchOptions::default().arg("--remote-debugging-port=0"))
        .expect("launch validation remote owner");
    assert!(owner.is_owned());
    assert!(owner.is_connected());

    let launched_descendants: Vec<(u32, String)> = descendants(std::process::id())
        .into_iter()
        .filter(|(pid, command)| {
            !before_launch.contains(pid) && command.to_ascii_lowercase().contains("chrom")
        })
        .collect();
    assert!(
        !launched_descendants.is_empty(),
        "owned launch must create an observable browser process"
    );
    let launched_pids: HashSet<u32> = launched_descendants.iter().map(|(pid, _)| *pid).collect();

    for page in owner.pages().expect("list startup pages") {
        page.close(Default::default()).expect("close startup page");
    }
    let first = owner.new_page().expect("create first owner page");
    let other = owner.new_page().expect("create other owner page");
    first
        .goto(
            &page_server.url("owned-first"),
            GotoOptions::default().wait_until("load").timeout(10_000.0),
        )
        .expect("navigate owned page first URL");
    first
        .goto(
            &page_server.url("owned-second"),
            GotoOptions::default().wait_until("load").timeout(10_000.0),
        )
        .expect("navigate owned page second URL");
    first
        .go_back(GotoOptions::default().wait_until("load").timeout(10_000.0))
        .expect("go back on owned page");
    assert_eq!(first.url(), page_server.url("owned-first"));
    first
        .reload(GotoOptions::default().wait_until("load").timeout(10_000.0))
        .expect("reload owned page");
    first
        .wait_for_load_state("load", Duration::from_secs(5))
        .expect("wait for owned page load state");
    other
        .goto(
            &page_server.url("other-page"),
            GotoOptions::default().wait_until("load").timeout(10_000.0),
        )
        .expect("navigate other owner page");

    let before_attach = owner.pages().expect("count pages before attach");
    assert_eq!(before_attach.len(), 2);
    let before_targets: HashSet<String> =
        before_attach.iter().map(|page| page.target_id()).collect();

    let attached = chromium()
        .connect_over_cdp(ConnectOptions::new(owner.ws_endpoint()).timeout(Duration::from_secs(10)))
        .expect("attach validation browser");
    assert!(!attached.is_owned());
    assert!(attached.is_connected());
    assert_eq!(
        owner
            .pages()
            .expect("count pages immediately after attach")
            .len(),
        2,
        "attach itself must not create a page"
    );

    let adopted_pages = attached.pages().expect("adopt existing pages");
    assert_eq!(adopted_pages.len(), 2);
    let adopted_targets: HashSet<String> =
        adopted_pages.iter().map(|page| page.target_id()).collect();
    assert_eq!(adopted_targets, before_targets);
    assert_eq!(attached.pages().expect("re-list adopted pages").len(), 2);

    let adopted = adopted_pages
        .into_iter()
        .find(|page| page.target_id() == first.target_id())
        .expect("find adopted first page");
    assert_eq!(adopted.url(), page_server.url("owned-first"));
    adopted
        .goto(
            &page_server.url("attached-second"),
            GotoOptions::default().wait_until("load").timeout(10_000.0),
        )
        .expect("navigate adopted page");
    adopted
        .go_back(GotoOptions::default().wait_until("load").timeout(10_000.0))
        .expect("go back on adopted page");
    assert_eq!(adopted.url(), page_server.url("owned-first"));
    adopted
        .reload(GotoOptions::default().wait_until("load").timeout(10_000.0))
        .expect("reload adopted page");
    adopted
        .wait_for_load_state("domcontentloaded", Duration::from_secs(5))
        .expect("wait for adopted page load state");
    let invalid_state = adopted
        .wait_for_load_state("validation-invalid-state", Duration::from_millis(50))
        .expect_err("unsupported load state must fail");
    assert!(matches!(invalid_state, Error::InvalidInput(_)));

    let attached_new = attached.new_page().expect("new page on attached browser");
    attached_new
        .goto(
            &page_server.url("attached-new"),
            GotoOptions::default().wait_until("load").timeout(10_000.0),
        )
        .expect("navigate attached new page");
    attached_new
        .wait_for_load_state("load", Duration::from_secs(5))
        .expect("wait for attached new page");
    assert_eq!(
        owner
            .pages()
            .expect("count page created by attachment")
            .len(),
        3
    );

    attached.close().expect("detach validation browser");
    assert!(!attached.is_connected());
    let closed = match attached.pages() {
        Ok(_) => panic!("closed attachment unexpectedly accepted an operation"),
        Err(error) => error,
    };
    assert!(matches!(closed, Error::Closed));

    let live_after_detach = live_pids();
    assert!(
        launched_pids
            .iter()
            .any(|pid| live_after_detach.contains(pid)),
        "the browser process must still be alive after attached close"
    );
    assert!(owner.is_connected());
    assert_eq!(owner.pages().expect("list pages after detach").len(), 3);
    assert_eq!(
        other
            .title(ActionOptions::timeout(5_000.0))
            .expect("read other page after detach"),
        "other-page"
    );
    other
        .goto(
            &page_server.url("owner-after-detach"),
            GotoOptions::default().wait_until("load").timeout(10_000.0),
        )
        .expect("navigate through owner after detach");
    assert_eq!(
        other
            .title(ActionOptions::timeout(5_000.0))
            .expect("read owner page after post-detach navigation"),
        "owner-after-detach"
    );

    owner.close().expect("clean up validation remote owner");
    assert!(wait_until(Duration::from_secs(5), || {
        let live = live_pids();
        launched_pids.iter().all(|pid| !live.contains(pid))
    }));

    println!("validation launched browser PIDs: {launched_pids:?}");
    println!("validation page count before attach: 2");
    println!("validation page count after attach/new page: 3");
    println!("validation launched process survived detach: true");
    println!("validation all launched processes exited after owner close: true");
}

#[test]
fn validation_headers_reach_http_resolution_and_failures_are_sanitized() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping validation header test: Chromium executable unavailable");
        return;
    }

    let owner = chromium()
        .launch(LaunchOptions::default().arg("--remote-debugging-port=0"))
        .expect("launch owner for validation header test");
    let header_name = "x-validation-cdp-marker";
    let header_value = "validation-header-secret-value";
    let stub = VersionStub::start(owner.ws_endpoint());
    let attached = chromium()
        .connect_over_cdp(
            ConnectOptions::new(&stub.endpoint)
                .header(header_name, header_value)
                .timeout(Duration::from_secs(10)),
        )
        .expect("connect through validation HTTP resolver");
    let request = stub.finish().to_ascii_lowercase();
    assert!(request.starts_with("get /json/version http/1.1"));
    assert!(request.contains(&format!("{header_name}: {header_value}")));
    attached
        .close()
        .expect("close validation header attachment");
    owner.close().expect("close validation header owner");

    let dead_endpoint = "ws://127.0.0.1:1/validation-endpoint-secret";
    let error = match chromium().connect_over_cdp(
        ConnectOptions::new(dead_endpoint)
            .header(header_name, header_value)
            .timeout(Duration::from_millis(700)),
    ) {
        Ok(_) => panic!("validation dead endpoint unexpectedly connected"),
        Err(error) => error,
    };
    assert!(matches!(error, Error::ConnectFailed));
    let text = error.to_string().to_ascii_lowercase();
    assert!(!text.contains(dead_endpoint));
    assert!(!text.contains(header_name));
    assert!(!text.contains(header_value));
    assert!(!text.contains("validation-endpoint-secret"));

    println!("validation HTTP resolution header observed: true");
    println!("validation failed-connect error: {text}");
    println!("validation failed-connect error excluded endpoint/header markers: true");
}

#[test]
fn validation_dead_endpoint_is_connect_failed_bounded_and_invalid_input_is_typed() {
    let started = Instant::now();
    let error = match chromium().connect_over_cdp(
        ConnectOptions::new("ws://127.0.0.1:1/validation-dead").timeout(Duration::from_millis(500)),
    ) {
        Ok(_) => panic!("dead validation endpoint unexpectedly connected"),
        Err(error) => error,
    };
    let elapsed = started.elapsed();
    assert!(matches!(error, Error::ConnectFailed));
    assert!(elapsed < Duration::from_secs(3));

    let invalid = match chromium().connect_over_cdp(
        ConnectOptions::new("file:///validation-not-cdp").timeout(Duration::from_millis(500)),
    ) {
        Ok(_) => panic!("unsupported endpoint scheme unexpectedly connected"),
        Err(error) => error,
    };
    assert!(matches!(invalid, Error::InvalidInput(_)));

    println!("validation dead endpoint error: {error}");
    println!("validation dead endpoint elapsed: {elapsed:?}");
    println!("validation unsupported scheme error: {invalid}");
}
