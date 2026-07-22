use std::{
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
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
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind validation fixture");
        let addr = listener.local_addr().expect("validation fixture address");
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            for connection in listener.incoming() {
                let mut stream = connection.expect("accept validation fixture connection");
                if thread_stop.load(Ordering::Relaxed) {
                    break;
                }
                serve_fixture(&mut stream);
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
            thread.join().expect("join validation fixture");
        }
    }
}

fn serve_fixture(stream: &mut TcpStream) {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("set validation fixture read timeout");
    let mut bytes = [0_u8; 4096];
    let read = stream.read(&mut bytes).unwrap_or(0);
    let request = String::from_utf8_lossy(&bytes[..read]);
    let path = request
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .unwrap_or("/");
    let body = match path.split('?').next().unwrap_or(path) {
        "/actions" => action_fixture(),
        "/dialogs" => dialog_fixture(),
        other => format!(
            "<!doctype html><meta charset=utf-8><title>{other}</title><main id=marker>{other}</main>"
        ),
    };
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream
        .write_all(response.as_bytes())
        .expect("write validation fixture response");
}

fn dialog_fixture() -> String {
    r#"<!doctype html>
<button id="alert" onclick="globalThis.alertBefore='before'; alert('validator alert'); globalThis.alertAfter='after'">alert</button>
<button id="confirm" onclick="globalThis.confirmResult=confirm('validator confirm')">confirm</button>"#
        .to_string()
}

fn action_fixture() -> String {
    r#"<!doctype html>
<style>
  body { margin: 0; }
  #hidden, #display-none { display: none; }
  #visibility-hidden { visibility: hidden; }
  #hover { width: 140px; height: 50px; background: rgb(0, 0, 255); }
  #hover:hover { background: rgb(0, 128, 0); }
  #spacer { height: 2200px; }
  @media (max-width: 700px) { body { --validator-narrow: yes; } }
</style>
<input id="text" data-purpose="event-capture">
<select id="multi" multiple>
  <option value="alpha">Alpha</option>
  <option value="beta">Beta</option>
  <option value="gamma">Gamma</option>
</select>
<button id="hover">Hover target</button>
<input id="checkbox" type="checkbox">
<div id="copy">Rendered <span>inner</span> text</div>
<button id="enabled">Enabled</button>
<button id="disabled" disabled>Disabled</button>
<div id="display-none">Display none</div>
<div id="visibility-hidden">Visibility hidden</div>
<div id="spacer"></div>
<div id="bottom">Bottom target</div>
<script>
  globalThis.keyEvents = [];
  globalThis.hoverEvents = [];
  globalThis.checkEvents = [];
  globalThis.selectEvents = [];
  const record = (target, event) => target.push({
    type: event.type,
    key: event.key || '',
    trusted: event.isTrusted,
    time: performance.now()
  });
  const text = document.getElementById('text');
  for (const name of ['keydown', 'beforeinput', 'input', 'keyup']) {
    text.addEventListener(name, event => record(globalThis.keyEvents, event));
  }
  const hover = document.getElementById('hover');
  for (const name of ['pointerover', 'mouseover', 'mouseenter', 'mousemove']) {
    hover.addEventListener(name, event => record(globalThis.hoverEvents, event));
  }
  const checkbox = document.getElementById('checkbox');
  for (const name of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'input', 'change']) {
    checkbox.addEventListener(name, event => record(globalThis.checkEvents, event));
  }
  const multi = document.getElementById('multi');
  for (const name of ['input', 'change']) {
    multi.addEventListener(name, event => record(globalThis.selectEvents, event));
  }
</script>"#
        .to_string()
}

fn launch() -> Option<rustwright::Browser> {
    if chromium().executable_path().is_none() {
        eprintln!("skipping independent P1-c validation: Chromium unavailable");
        return None;
    }
    Some(
        chromium()
            .launch(LaunchOptions::default())
            .expect("launch independent validation browser"),
    )
}

fn goto(page: &rustwright::Page, url: &str) {
    page.goto(
        url,
        GotoOptions::default().wait_until("load").timeout(10_000.0),
    )
    .unwrap_or_else(|error| panic!("navigate {url}: {error}"));
}

fn evaluate(page: &rustwright::Page, expression: &str) -> Value {
    page.evaluate(expression, None, ActionOptions::timeout(10_000.0))
        .unwrap_or_else(|error| panic!("evaluate {expression}: {error}"))
}

fn event_types(events: &Value) -> Vec<&str> {
    events
        .as_array()
        .expect("event capture array")
        .iter()
        .map(|event| event["type"].as_str().expect("event type"))
        .collect()
}

fn assert_all_trusted(events: &Value, label: &str) {
    let events = events.as_array().expect("event capture array");
    assert!(!events.is_empty(), "{label} should capture events");
    assert!(
        events
            .iter()
            .all(|event| event["trusted"] == Value::Bool(true)),
        "{label} contained an untrusted event: {events:?}"
    );
}

fn next_navigation(events: &rustwright::EventReceiver, timeout: Duration) -> String {
    let deadline = Instant::now() + timeout;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(!remaining.is_zero(), "timed out waiting for navigation");
        match events.recv_timeout(remaining) {
            Some(PageEvent::Navigated { url }) => return url,
            Some(_) => continue,
            None => panic!("timed out waiting for navigation"),
        }
    }
}

#[test]
fn validation_p1c_nasty_history_interleaves_two_pages_for_48_rounds_without_think_time() {
    let Some(browser) = launch() else {
        return;
    };
    let server = FixtureServer::start();
    let pages = [
        browser.new_page().expect("create nasty page zero"),
        browser.new_page().expect("create nasty page one"),
    ];
    for (index, page) in pages.iter().enumerate() {
        goto(page, &server.url(&format!("/nasty/{index}/a")));
        goto(page, &server.url(&format!("/nasty/{index}/b")));
    }

    let started = Instant::now();
    for round in 0..48 {
        let first = &pages[round % 2];
        let second = &pages[(round + 1) % 2];
        first
            .go_back(GotoOptions::default().wait_until("load").timeout(10_000.0))
            .unwrap_or_else(|error| panic!("round {round} first go_back: {error}"));
        second
            .reload(
                GotoOptions::default()
                    .wait_until("domcontentloaded")
                    .timeout(10_000.0),
            )
            .unwrap_or_else(|error| panic!("round {round} second reload: {error}"));
        first
            .wait_for_load_state("load", Duration::from_secs(10))
            .unwrap_or_else(|error| panic!("round {round} first wait: {error}"));
        goto(first, &server.url(&format!("/nasty/first/{round}")));
        goto(second, &server.url(&format!("/nasty/second/{round}")));
        second
            .wait_for_load_state("load", Duration::from_secs(10))
            .unwrap_or_else(|error| panic!("round {round} second wait: {error}"));
        first
            .reload(GotoOptions::default().wait_until("load").timeout(10_000.0))
            .unwrap_or_else(|error| panic!("round {round} first reload: {error}"));
        second
            .go_back(GotoOptions::default().wait_until("load").timeout(10_000.0))
            .unwrap_or_else(|error| panic!("round {round} second go_back: {error}"));
        second
            .wait_for_load_state("load", Duration::from_secs(10))
            .unwrap_or_else(|error| panic!("round {round} second final wait: {error}"));
        goto(second, &server.url(&format!("/nasty/restore/{round}")));
    }
    println!(
        "validator nasty hammer: 48 rounds, 2 pages, completed in {:?}",
        started.elapsed()
    );

    browser.close().expect("close nasty validation browser");
}

#[test]
fn validation_p1c_dialogs_navigation_unsubscribe_and_sync_inflight_are_live() {
    let Some(browser) = launch() else {
        return;
    };
    let server = FixtureServer::start();
    let page = browser.new_page().expect("create event validation page");
    goto(&page, &server.url("/dialogs"));
    let events = page.events();

    let (alert_done_tx, alert_done_rx) = mpsc::sync_channel(1);
    let alert_page = page.clone();
    let alert_thread = thread::spawn(move || {
        let result = alert_page.click("#alert", ActionOptions::timeout(10_000.0));
        alert_done_tx
            .send(result)
            .expect("send alert click completion");
    });
    let alert = loop {
        match events.recv_timeout(Duration::from_secs(10)) {
            Some(PageEvent::Dialog {
                kind,
                message,
                dialog,
            }) => break (kind, message, dialog),
            Some(_) => continue,
            None => panic!("timed out waiting for typed alert"),
        }
    };
    assert_eq!(alert.0, DialogKind::Alert);
    assert_eq!(alert.1, "validator alert");
    assert!(
        alert_done_rx
            .recv_timeout(Duration::from_millis(50))
            .is_err(),
        "sync facade click unexpectedly completed before dialog resolution"
    );
    alert.2.accept(None).expect("accept typed alert");
    alert_done_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("alert click remained blocked after accept")
        .expect("alert click failed");
    alert_thread.join().expect("join alert click thread");
    assert_eq!(
        evaluate(&page, "globalThis.alertAfter"),
        Value::String("after".to_string())
    );

    let (confirm_done_tx, confirm_done_rx) = mpsc::sync_channel(1);
    let confirm_page = page.clone();
    let confirm_thread = thread::spawn(move || {
        let result = confirm_page.click("#confirm", ActionOptions::timeout(10_000.0));
        confirm_done_tx
            .send(result)
            .expect("send confirm click completion");
    });
    let confirm = loop {
        match events.recv_timeout(Duration::from_secs(10)) {
            Some(PageEvent::Dialog {
                kind,
                message,
                dialog,
            }) => break (kind, message, dialog),
            Some(_) => continue,
            None => panic!("timed out waiting for typed confirm"),
        }
    };
    assert_eq!(confirm.0, DialogKind::Confirm);
    assert_eq!(confirm.1, "validator confirm");
    confirm.2.dismiss().expect("dismiss typed confirm");
    confirm_done_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("confirm click remained blocked after dismiss")
        .expect("confirm click failed");
    confirm_thread.join().expect("join confirm click thread");
    assert_eq!(
        evaluate(&page, "globalThis.confirmResult"),
        Value::Bool(false)
    );

    let first = server.url("/event/first");
    let second = server.url("/event/second");
    goto(&page, &first);
    assert_eq!(next_navigation(&events, Duration::from_secs(10)), first);
    goto(&page, &second);
    assert_eq!(next_navigation(&events, Duration::from_secs(10)), second);
    page.go_back(GotoOptions::default().wait_until("load").timeout(10_000.0))
        .expect("event validation go_back");
    assert_eq!(next_navigation(&events, Duration::from_secs(10)), first);

    let abandoned = page.events();
    drop(abandoned);
    goto(&page, &server.url("/event/while-unsubscribed"));
    let replacement = page.events();
    assert!(
        replacement
            .recv_timeout(Duration::from_millis(100))
            .is_none(),
        "replacement subscriber replayed an event emitted while unsubscribed"
    );
    let after = server.url("/event/after-resubscribe");
    goto(&page, &after);
    assert_eq!(
        next_navigation(&replacement, Duration::from_secs(10)),
        after
    );

    page.close(Default::default()).expect("close event page");
    loop {
        match replacement.recv_timeout(Duration::from_secs(10)) {
            Some(PageEvent::Closed) => break,
            Some(_) => continue,
            None => panic!("closed event missing"),
        }
    }
    assert!(
        replacement
            .recv_timeout(Duration::from_millis(100))
            .is_none(),
        "terminal subscription delivered an event after closure"
    );

    browser.close().expect("close event validation browser");
}

#[test]
fn validation_p1c_event_queue_drops_oldest_and_counts_exact_evictions() {
    let Some(browser) = launch() else {
        return;
    };
    let server = FixtureServer::start();
    let page = browser.new_page().expect("create overflow validation page");
    goto(&page, &server.url("/overflow/setup"));
    let events = page.events();
    assert_eq!(events.capacity(), 128);

    for index in 0..160 {
        goto(&page, &server.url(&format!("/overflow/{index}")));
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while events.dropped_count() < 32 && Instant::now() < deadline {
        thread::yield_now();
    }
    assert_eq!(
        events.dropped_count(),
        32,
        "160 navigation events in a 128-entry queue must evict exactly 32"
    );

    let mut urls = Vec::new();
    for _ in 0..events.capacity() {
        match events.recv_timeout(Duration::from_secs(2)) {
            Some(PageEvent::Navigated { url }) => urls.push(url),
            Some(other) => panic!("unexpected overflow event: {other:?}"),
            None => panic!("overflow queue drained before capacity"),
        }
    }
    assert_eq!(urls.len(), 128);
    assert_eq!(urls.first(), Some(&server.url("/overflow/32")));
    assert_eq!(urls.last(), Some(&server.url("/overflow/159")));
    assert!(events.recv_timeout(Duration::from_millis(100)).is_none());
    assert_eq!(events.dropped_count(), 32);

    browser.close().expect("close overflow validation browser");
}

#[test]
fn validation_p1c_promoted_methods_match_physical_and_dom_dispositions() {
    let Some(browser) = launch() else {
        return;
    };
    let server = FixtureServer::start();
    let page = browser.new_page().expect("create promoted-method page");
    goto(&page, &server.url("/actions"));

    let typed_at = Instant::now();
    page.type_text("#text", "Ab?", Some(Duration::from_millis(25)))
        .expect("type physical text with delay");
    assert!(
        typed_at.elapsed() >= Duration::from_millis(60),
        "per-character typing delay was not reflected in elapsed time"
    );
    assert_eq!(
        evaluate(&page, "document.getElementById('text').value"),
        Value::String("Ab?".to_string())
    );
    let typed_events = evaluate(&page, "globalThis.keyEvents");
    assert_all_trusted(&typed_events, "type_text");
    let typed_types = event_types(&typed_events);
    assert_eq!(
        typed_types
            .iter()
            .filter(|event_type| **event_type == "keydown")
            .count(),
        3
    );
    assert_eq!(
        typed_types
            .iter()
            .filter(|event_type| **event_type == "keyup")
            .count(),
        3
    );
    let typed_array = typed_events.as_array().expect("typed event array");
    for key in ["A", "b", "?"] {
        let down = typed_array
            .iter()
            .find(|event| event["type"] == "keydown" && event["key"] == key)
            .expect("typed keydown");
        let up = typed_array
            .iter()
            .find(|event| event["type"] == "keyup" && event["key"] == key)
            .expect("typed keyup");
        let delay = up["time"].as_f64().expect("keyup timestamp")
            - down["time"].as_f64().expect("keydown timestamp");
        assert!(
            delay >= 18.0,
            "{key} keydown/keyup delay was only {delay}ms"
        );
    }

    let before_press = typed_array.len();
    page.press_key(Some("#text"), "Enter")
        .expect("press physical Enter");
    let with_press = evaluate(&page, "globalThis.keyEvents");
    let press_events =
        Value::Array(with_press.as_array().expect("key event array")[before_press..].to_vec());
    assert_all_trusted(&press_events, "press_key");
    assert_eq!(event_types(&press_events), vec!["keydown", "keyup"]);
    assert!(press_events
        .as_array()
        .expect("press events")
        .iter()
        .all(|event| event["key"] == "Enter"));
    page.press_key(None, "!")
        .expect("press physical key without selector");
    assert_eq!(
        evaluate(&page, "document.getElementById('text').value"),
        Value::String("Ab?!".to_string())
    );

    assert_eq!(
        page.select_options("#multi", &["beta", "gamma"])
            .expect("select multiple options"),
        vec!["beta".to_string(), "gamma".to_string()]
    );
    assert_eq!(
        evaluate(
            &page,
            "Array.from(document.getElementById('multi').selectedOptions, option => option.value)"
        ),
        Value::Array(vec![
            Value::String("beta".to_string()),
            Value::String("gamma".to_string())
        ])
    );
    let select_events = evaluate(&page, "globalThis.selectEvents");
    assert_eq!(event_types(&select_events), vec!["input", "change"]);
    assert!(
        select_events
            .as_array()
            .expect("select event array")
            .iter()
            .all(|event| event["trusted"] == Value::Bool(false)),
        "documented DOM selection shortcut unexpectedly emitted trusted events"
    );

    assert_eq!(
        evaluate(
            &page,
            "getComputedStyle(document.getElementById('hover')).backgroundColor"
        ),
        Value::String("rgb(0, 0, 255)".to_string())
    );
    page.hover("#hover")
        .expect("hover through physical mouse path");
    assert_eq!(
        evaluate(
            &page,
            "getComputedStyle(document.getElementById('hover')).backgroundColor"
        ),
        Value::String("rgb(0, 128, 0)".to_string())
    );
    let hover_events = evaluate(&page, "globalThis.hoverEvents");
    assert_all_trusted(&hover_events, "hover");
    let hover_types = event_types(&hover_events);
    assert!(hover_types.contains(&"mouseover"));
    assert!(hover_types.contains(&"mousemove"));

    page.check("#checkbox").expect("physical check");
    assert!(page.is_checked("#checkbox").expect("checked state"));
    let after_check = evaluate(&page, "globalThis.checkEvents");
    assert_all_trusted(&after_check, "check");
    let check_types = event_types(&after_check);
    for required in [
        "pointerdown",
        "mousedown",
        "pointerup",
        "mouseup",
        "click",
        "input",
        "change",
    ] {
        assert!(
            check_types.contains(&required),
            "check missed {required}: {check_types:?}"
        );
    }
    let checked_event_count = after_check.as_array().expect("check events").len();
    page.check("#checkbox").expect("idempotent check");
    assert_eq!(
        evaluate(&page, "globalThis.checkEvents")
            .as_array()
            .expect("idempotent check events")
            .len(),
        checked_event_count,
        "idempotent check dispatched another physical click"
    );

    page.uncheck("#checkbox").expect("physical uncheck");
    assert!(!page.is_checked("#checkbox").expect("unchecked state"));
    let after_uncheck = evaluate(&page, "globalThis.checkEvents");
    assert_all_trusted(&after_uncheck, "uncheck");
    let unchecked_event_count = after_uncheck.as_array().expect("uncheck events").len();
    assert!(unchecked_event_count > checked_event_count);
    page.uncheck("#checkbox").expect("idempotent uncheck");
    assert_eq!(
        evaluate(&page, "globalThis.checkEvents")
            .as_array()
            .expect("idempotent uncheck events")
            .len(),
        unchecked_event_count,
        "idempotent uncheck dispatched another physical click"
    );

    assert_eq!(
        page.inner_text("#copy").expect("inner text"),
        Some("Rendered inner text".to_string())
    );
    assert_eq!(
        page.get_attribute("#text", "data-purpose")
            .expect("present attribute"),
        Some("event-capture".to_string())
    );
    assert_eq!(
        page.get_attribute("#text", "data-missing")
            .expect("missing attribute"),
        None
    );
    assert!(page.is_visible("#copy").expect("visible fixture"));
    assert!(!page
        .is_visible("#display-none")
        .expect("display none fixture"));
    assert!(!page
        .is_visible("#visibility-hidden")
        .expect("visibility hidden fixture"));
    assert!(page.is_enabled("#enabled").expect("enabled fixture"));
    assert!(!page.is_enabled("#disabled").expect("disabled fixture"));

    page.set_viewport_size(900, 600).expect("set wide viewport");
    assert_eq!(evaluate(&page, "window.innerWidth"), Value::from(900));
    assert_eq!(evaluate(&page, "window.innerHeight"), Value::from(600));
    assert_eq!(
        evaluate(&page, "matchMedia('(max-width: 700px)').matches"),
        Value::Bool(false)
    );
    page.set_viewport_size(640, 480)
        .expect("set narrow viewport");
    assert_eq!(evaluate(&page, "window.innerWidth"), Value::from(640));
    assert_eq!(evaluate(&page, "window.innerHeight"), Value::from(480));
    assert_eq!(
        evaluate(&page, "matchMedia('(max-width: 700px)').matches"),
        Value::Bool(true)
    );

    evaluate(&page, "window.scrollTo(0, 0)");
    assert_eq!(evaluate(&page, "window.scrollY"), Value::from(0));
    page.scroll_into_view("#bottom")
        .expect("documented DOM scroll shortcut");
    assert!(
        evaluate(&page, "window.scrollY")
            .as_f64()
            .unwrap_or_default()
            > 1000.0
    );

    browser.close().expect("close promoted-method browser");
}
