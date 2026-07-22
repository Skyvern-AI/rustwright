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

use rustwright::{chromium, ActionOptions, DialogKind, GotoOptions, LaunchOptions, PageEvent};
use serde_json::Value;

struct FixtureServer {
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl FixtureServer {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind fixture server");
        listener
            .set_nonblocking(true)
            .expect("set fixture server nonblocking");
        let addr = listener.local_addr().expect("fixture server address");
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !thread_stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut stream, _)) => serve_fixture(&mut stream),
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(error) => panic!("fixture server accept failed: {error}"),
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
        format!("http://{}{path}", self.addr)
    }
}

impl Drop for FixtureServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let _ = TcpStream::connect(self.addr);
        if let Some(thread) = self.thread.take() {
            thread.join().expect("join fixture server");
        }
    }
}

fn serve_fixture(stream: &mut TcpStream) {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("set fixture request timeout");
    let mut bytes = [0_u8; 4096];
    let read = stream.read(&mut bytes).unwrap_or(0);
    let request = String::from_utf8_lossy(&bytes[..read]);
    let path = request
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .unwrap_or("/");
    let body = match path.split('?').next().unwrap_or(path) {
        "/input" => input_fixture(),
        "/dialogs" => dialog_fixture(),
        other => format!("<!doctype html><title>{other}</title><main>{other}</main>"),
    };
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream
        .write_all(response.as_bytes())
        .expect("write fixture response");
}

fn dialog_fixture() -> String {
    r#"<!doctype html>
<button id="alert" onclick="alert('event alert')">alert</button>
<button id="confirm" onclick="globalThis.confirmResult = confirm('event confirm')">confirm</button>"#
        .to_string()
}

fn input_fixture() -> String {
    r#"<!doctype html>
<style>
  #hidden { display: none; }
  #spacer { height: 1800px; }
  #hover { width: 120px; height: 40px; }
</style>
<input id="text" data-kind="entry">
<div id="key-result"></div>
<select id="choices" multiple>
  <option value="a">A</option>
  <option value="b">B</option>
  <option value="c">C</option>
</select>
<button id="hover">Hover</button>
<input id="check" type="checkbox">
<div id="copy">Rendered text</div>
<button id="enabled">Enabled</button>
<button id="disabled" disabled>Disabled</button>
<div id="hidden">Hidden</div>
<div id="spacer"></div>
<div id="bottom">Bottom</div>
<script>
  const textInput = document.getElementById('text');
  const keyResultNode = document.getElementById('key-result');
  const hoverTarget = document.getElementById('hover');
  const checkbox = document.getElementById('check');
  textInput.addEventListener('input', event => globalThis.inputTrusted = event.isTrusted);
  textInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') keyResultNode.textContent = String(event.isTrusted);
  });
  hoverTarget.addEventListener('mouseover', event => {
    hoverTarget.dataset.hovered = 'yes';
    globalThis.hoverTrusted = event.isTrusted;
  });
  checkbox.addEventListener('click', event => globalThis.checkTrusted = event.isTrusted);
</script>"#
        .to_string()
}

fn launch() -> Option<rustwright::Browser> {
    if chromium().executable_path().is_none() {
        eprintln!("skipping P1-c facade test: Chromium executable unavailable");
        return None;
    }
    Some(
        chromium()
            .launch(LaunchOptions::default())
            .expect("launch browser"),
    )
}

fn goto(page: &rustwright::Page, url: &str) {
    page.goto(
        url,
        GotoOptions::default().wait_until("load").timeout(10_000.0),
    )
    .expect("navigate fixture page");
}

fn wait_for_actionable(page: &rustwright::Page, selector: &str, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    page.wait_for_load_state("load", timeout)
        .unwrap_or_else(|error| panic!("wait for fixture load before {selector}: {error}"));
    loop {
        assert!(
            Instant::now() < deadline,
            "timed out waiting for {selector} to become actionable"
        );
        if page
            .is_visible(selector)
            .unwrap_or_else(|error| panic!("check {selector} visibility: {error}"))
            && page
                .is_enabled(selector)
                .unwrap_or_else(|error| panic!("check {selector} enabled state: {error}"))
        {
            return;
        }
        thread::yield_now();
    }
}

fn evaluate(page: &rustwright::Page, expression: &str) -> Value {
    page.evaluate(expression, None, ActionOptions::timeout(10_000.0))
        .expect("evaluate fixture expression")
}

fn next_dialog(
    events: &rustwright::EventReceiver,
    timeout: Duration,
) -> (DialogKind, String, rustwright::Dialog) {
    let deadline = Instant::now() + timeout;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(!remaining.is_zero(), "timed out waiting for dialog event");
        match events.recv_timeout(remaining) {
            Some(PageEvent::Dialog {
                kind,
                message,
                dialog,
            }) => return (kind, message, dialog),
            Some(_) => continue,
            None => panic!("timed out waiting for dialog event"),
        }
    }
}

fn next_navigation(events: &rustwright::EventReceiver, timeout: Duration) -> String {
    let deadline = Instant::now() + timeout;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(
            !remaining.is_zero(),
            "timed out waiting for navigation event"
        );
        match events.recv_timeout(remaining) {
            Some(PageEvent::Navigated { url }) => return url,
            Some(_) => continue,
            None => panic!("timed out waiting for navigation event"),
        }
    }
}

fn expect_closed(events: &rustwright::EventReceiver, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(!remaining.is_zero(), "timed out waiting for closed event");
        match events.recv_timeout(remaining) {
            Some(PageEvent::Closed) => return,
            Some(_) => continue,
            None => panic!("timed out waiting for closed event"),
        }
    }
}

#[test]
fn history_back_then_reload_settles_across_two_pages_for_25_rounds() {
    let Some(browser) = launch() else {
        return;
    };
    let server = FixtureServer::start();
    let first = browser.new_page().expect("create first stress page");
    let second = browser.new_page().expect("create second stress page");
    let pages = [
        (
            first,
            server.url("/history-a-1"),
            server.url("/history-b-1"),
        ),
        (
            second,
            server.url("/history-a-2"),
            server.url("/history-b-2"),
        ),
    ];
    for (page, first_url, second_url) in &pages {
        goto(page, first_url);
        goto(page, second_url);
    }

    for round in 0..25 {
        for (page, first_url, second_url) in &pages {
            page.go_back(GotoOptions::default().wait_until("load").timeout(10_000.0))
                .unwrap_or_else(|error| panic!("go_back failed in round {round}: {error}"));
            assert_eq!(
                page.url(),
                *first_url,
                "wrong URL after back in round {round}"
            );
            page.reload(GotoOptions::default().wait_until("load").timeout(10_000.0))
                .unwrap_or_else(|error| panic!("reload failed in round {round}: {error}"));
            assert_eq!(
                page.url(),
                *first_url,
                "wrong URL after reload in round {round}"
            );
            goto(page, second_url);
        }
    }

    browser.close().expect("close stress browser");
}

#[test]
fn dialog_events_can_accept_alert_and_dismiss_confirm() {
    let Some(browser) = launch() else {
        return;
    };
    let server = FixtureServer::start();
    let page = browser.new_page().expect("create dialog page");
    goto(&page, &server.url("/dialogs"));
    let events = page.events();
    assert_eq!(events.capacity(), 128);

    wait_for_actionable(&page, "#alert", Duration::from_secs(30));
    let alert_page = page.clone();
    let alert_click = thread::spawn(move || {
        alert_page
            .click("#alert", ActionOptions::timeout(10_000.0))
            .expect("click alert button");
    });
    let (kind, message, dialog) = next_dialog(&events, Duration::from_secs(10));
    assert_eq!(kind, DialogKind::Alert);
    assert_eq!(message, "event alert");
    dialog.accept(None).expect("accept alert");
    alert_click.join().expect("join alert click");

    wait_for_actionable(&page, "#confirm", Duration::from_secs(30));
    let confirm_page = page.clone();
    let confirm_click = thread::spawn(move || {
        confirm_page
            .click("#confirm", ActionOptions::timeout(10_000.0))
            .expect("click confirm button");
    });
    let (kind, message, dialog) = next_dialog(&events, Duration::from_secs(10));
    assert_eq!(kind, DialogKind::Confirm);
    assert_eq!(message, "event confirm");
    dialog.dismiss().expect("dismiss confirm");
    confirm_click.join().expect("join confirm click");
    assert_eq!(
        evaluate(&page, "globalThis.confirmResult"),
        Value::Bool(false)
    );
    assert_eq!(events.dropped_count(), 0);

    browser.close().expect("close dialog browser");
}

#[test]
fn navigated_events_arrive_for_goto_and_back() {
    let Some(browser) = launch() else {
        return;
    };
    let server = FixtureServer::start();
    let page = browser.new_page().expect("create navigation event page");
    let events = page.events();
    let first = server.url("/event-a");
    let second = server.url("/event-b");

    goto(&page, &first);
    assert_eq!(next_navigation(&events, Duration::from_secs(10)), first);
    goto(&page, &second);
    assert_eq!(next_navigation(&events, Duration::from_secs(10)), second);
    page.go_back(GotoOptions::default().wait_until("load").timeout(10_000.0))
        .expect("navigate back for event");
    assert_eq!(next_navigation(&events, Duration::from_secs(10)), first);
    page.close(Default::default()).expect("close event page");
    expect_closed(&events, Duration::from_secs(10));

    browser.close().expect("close navigation event browser");
}

#[test]
fn promoted_input_read_and_viewport_surface_uses_fixture_page() {
    let Some(browser) = launch() else {
        return;
    };
    let server = FixtureServer::start();
    let page = browser.new_page().expect("create input page");
    goto(&page, &server.url("/input"));

    page.type_text("#text", "typed", Some(Duration::from_millis(1)))
        .expect("type text");
    assert_eq!(
        evaluate(&page, "document.getElementById('text').value"),
        Value::String("typed".into())
    );
    assert_eq!(
        evaluate(&page, "globalThis.inputTrusted"),
        Value::Bool(true)
    );

    page.press_key(Some("#text"), "Enter").expect("press Enter");
    assert_eq!(
        page.inner_text("#key-result").expect("read key result"),
        Some("true".to_string())
    );
    page.press_key(None, "!")
        .expect("press key without selector");
    assert_eq!(
        evaluate(&page, "document.getElementById('text').value"),
        Value::String("typed!".into())
    );

    assert_eq!(
        page.select_options("#choices", &["b", "c"])
            .expect("select options"),
        vec!["b".to_string(), "c".to_string()]
    );
    page.hover("#hover").expect("hover element");
    assert_eq!(
        page.get_attribute("#hover", "data-hovered")
            .expect("read hover attribute"),
        Some("yes".to_string())
    );
    assert_eq!(
        evaluate(&page, "globalThis.hoverTrusted"),
        Value::Bool(true)
    );

    page.check("#check").expect("check checkbox");
    assert!(page.is_checked("#check").expect("read checked state"));
    assert_eq!(
        evaluate(&page, "globalThis.checkTrusted"),
        Value::Bool(true)
    );
    page.uncheck("#check").expect("uncheck checkbox");
    assert!(!page.is_checked("#check").expect("read unchecked state"));

    assert_eq!(
        page.inner_text("#copy").expect("read inner text"),
        Some("Rendered text".to_string())
    );
    assert_eq!(
        page.get_attribute("#text", "data-kind")
            .expect("read attribute"),
        Some("entry".to_string())
    );
    assert!(page.is_visible("#copy").expect("read visible state"));
    assert!(!page.is_visible("#hidden").expect("read hidden state"));
    assert!(page.is_enabled("#enabled").expect("read enabled state"));
    assert!(!page.is_enabled("#disabled").expect("read disabled state"));

    page.set_viewport_size(640, 480).expect("set viewport size");
    assert_eq!(evaluate(&page, "window.innerWidth"), Value::from(640));
    assert_eq!(evaluate(&page, "window.innerHeight"), Value::from(480));
    page.scroll_into_view("#bottom")
        .expect("scroll bottom into view");
    assert!(
        evaluate(&page, "window.scrollY")
            .as_f64()
            .unwrap_or_default()
            > 0.0
    );

    browser.close().expect("close input browser");
}
