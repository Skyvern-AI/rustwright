use std::{
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::{Duration, Instant},
};

use rustwright::{chromium, ConnectOptions, Error, GotoOptions, LaunchOptions};

struct PageServer {
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl PageServer {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind page server");
        listener
            .set_nonblocking(true)
            .expect("nonblocking listener");
        let addr = listener.local_addr().expect("page server address");
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !thread_stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut stream, _)) => serve_page(&mut stream),
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("page server accept failed: {error}"),
                }
            }
        });
        Self {
            addr,
            stop,
            thread: Some(thread),
        }
    }

    fn url(&self, path: &str) -> String {
        format!("http://{}{}", self.addr, path)
    }
}

impl Drop for PageServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let _ = TcpStream::connect(self.addr);
        if let Some(thread) = self.thread.take() {
            thread.join().expect("page server thread");
        }
    }
}

fn serve_page(stream: &mut TcpStream) {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("request timeout");
    let mut request = [0_u8; 2048];
    let read = stream.read(&mut request).unwrap_or(0);
    let request = String::from_utf8_lossy(&request[..read]);
    let path = request
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .unwrap_or("/");
    let title = if path == "/two" {
        "Page two"
    } else {
        "Page one"
    };
    let body = format!("<!doctype html><title>{title}</title><p>{path}</p>");
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream
        .write_all(response.as_bytes())
        .expect("write page response");
}

#[test]
fn attach_adopts_pages_promotes_navigation_and_detaches_without_killing() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping live CDP test: Chromium executable unavailable");
        return;
    }

    let page_server = PageServer::start();
    let owner = chromium()
        .launch(LaunchOptions::default().arg("--remote-debugging-port=0"))
        .expect("launch owned remote Chromium");
    assert!(owner.is_owned());
    assert!(owner.is_connected());

    let owner_page = owner.new_page().expect("create existing remote page");
    let first_url = page_server.url("/one");
    owner_page
        .goto(
            &first_url,
            GotoOptions::default().wait_until("load").timeout(10_000.0),
        )
        .expect("navigate existing page");

    let attached = chromium()
        .connect_over_cdp(ConnectOptions::new(owner.ws_endpoint()).timeout(Duration::from_secs(10)))
        .expect("attach over CDP");
    assert!(!attached.is_owned());
    assert!(attached.is_connected());

    let pages = attached
        .pages()
        .expect("list and adopt default-context pages");
    assert!(!pages.is_empty());
    let adopted = pages
        .into_iter()
        .find(|page| page.target_id() == owner_page.target_id())
        .expect("existing owner page was adopted");
    assert_eq!(adopted.url(), first_url);

    let second_url = page_server.url("/two");
    adopted
        .goto(
            &second_url,
            GotoOptions::default().wait_until("load").timeout(10_000.0),
        )
        .expect("navigate adopted page");
    assert_eq!(adopted.url(), second_url);
    adopted
        .go_back(GotoOptions::default().wait_until("load").timeout(10_000.0))
        .expect("navigate adopted page history");
    assert_eq!(adopted.url(), first_url);
    adopted
        .reload(GotoOptions::default().wait_until("load").timeout(10_000.0))
        .expect("reload adopted page");
    adopted
        .wait_for_load_state("load", Duration::from_secs(5))
        .expect("observe current load state");

    attached.close().expect("detach remote CDP browser");
    assert!(!attached.is_connected());
    let closed_error = match attached.pages() {
        Ok(_) => panic!("closed attachment unexpectedly listed pages"),
        Err(error) => error,
    };
    assert!(matches!(closed_error, Error::Closed));
    assert!(owner.is_connected());
    assert_eq!(owner_page.title(Default::default()).unwrap(), "Page one");

    owner.close().expect("close owned remote Chromium");
}

#[test]
fn dead_endpoint_is_typed_sanitized_and_bounded() {
    let endpoint = "ws://127.0.0.1:1/test-path?marker=endpoint-value";
    let started = Instant::now();
    let error = match chromium().connect_over_cdp(
        ConnectOptions::new(endpoint)
            .header("x-rustwright-test", "header-marker-value")
            .timeout(Duration::from_millis(500)),
    ) {
        Ok(_) => panic!("dead endpoint unexpectedly connected"),
        Err(error) => error,
    };
    assert!(matches!(error, Error::ConnectFailed | Error::Timeout(_)));
    assert!(started.elapsed() < Duration::from_secs(3));
    let display = error.to_string();
    assert!(!display.contains(endpoint));
    assert!(!display.contains("header-marker-value"));
    assert!(!display.contains("test-path"));
}
