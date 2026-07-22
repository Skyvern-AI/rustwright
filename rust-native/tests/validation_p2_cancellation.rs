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

use rustwright::{chromium, ActionOptions, CancelToken, Error, GotoOptions, LaunchOptions};

struct HangingServer {
    addr: SocketAddr,
    accepted: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl HangingServer {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind validation hanging server");
        listener
            .set_nonblocking(true)
            .expect("set validation hanging server nonblocking");
        let addr = listener.local_addr().expect("validation hanging address");
        let accepted = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(AtomicBool::new(false));
        let thread_accepted = Arc::clone(&accepted);
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            let mut connections = Vec::new();
            while !thread_stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        let mut request = [0_u8; 1024];
                        let _ = stream.read(&mut request);
                        thread_accepted.store(true, Ordering::SeqCst);
                        connections.push(stream);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(error) => panic!("validation hanging accept failed: {error}"),
                }
            }
        });
        Self {
            addr,
            accepted,
            stop,
            thread: Some(thread),
        }
    }

    fn url(&self) -> String {
        format!("http://{}/never-finishes", self.addr)
    }

    fn wait_for_request(&self) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !self.accepted.load(Ordering::SeqCst) {
            assert!(
                Instant::now() < deadline,
                "hanging navigation never reached validation fixture"
            );
            thread::sleep(Duration::from_millis(2));
        }
    }
}

impl Drop for HangingServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let _ = TcpStream::connect(self.addr);
        if let Some(thread) = self.thread.take() {
            thread.join().expect("join validation hanging server");
        }
    }
}

struct SignalServer {
    addr: SocketAddr,
    received: Arc<AtomicBool>,
    released: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl SignalServer {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind validation signal server");
        listener
            .set_nonblocking(true)
            .expect("set validation signal server nonblocking");
        let addr = listener.local_addr().expect("validation signal address");
        let received = Arc::new(AtomicBool::new(false));
        let released = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(AtomicBool::new(false));
        let thread_received = Arc::clone(&received);
        let thread_released = Arc::clone(&released);
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !thread_stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        if thread_stop.load(Ordering::Relaxed) {
                            break;
                        }
                        let mut request = [0_u8; 1024];
                        let _ = stream.read(&mut request);
                        thread_received.store(true, Ordering::SeqCst);
                        while !thread_released.load(Ordering::SeqCst)
                            && !thread_stop.load(Ordering::Relaxed)
                        {
                            thread::sleep(Duration::from_millis(2));
                        }
                        let _ = stream.write_all(
                            b"HTTP/1.1 204 No Content\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                        );
                        break;
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(error) => panic!("validation signal accept failed: {error}"),
                }
            }
        });
        Self {
            addr,
            received,
            released,
            stop,
            thread: Some(thread),
        }
    }

    fn url(&self) -> String {
        format!("http://{}/signal", self.addr)
    }

    fn wait_for_request(&self) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !self.received.load(Ordering::SeqCst) {
            assert!(
                Instant::now() < deadline,
                "browser operation never reached validation signal"
            );
            thread::sleep(Duration::from_millis(2));
        }
    }

    fn release(&self) {
        self.released.store(true, Ordering::SeqCst);
    }
}

impl Drop for SignalServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.release();
        let _ = TcpStream::connect(self.addr);
        if let Some(thread) = self.thread.take() {
            thread.join().expect("join validation signal server");
        }
    }
}

#[test]
fn validation_p2_goto_load_state_and_evaluate_report_cancel_depth() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping validation P2 cancellation depth: Chromium unavailable");
        return;
    }

    let browser = chromium()
        .launch(LaunchOptions::default())
        .expect("launch validation cancellation browser");

    let hanging = HangingServer::start();
    let goto_page = browser.new_page().expect("create goto validation page");
    let goto_token = CancelToken::new();
    let goto_worker_token = goto_token.clone();
    let goto_worker_page = goto_page.clone();
    let goto_url = hanging.url();
    let (goto_tx, goto_rx) = mpsc::sync_channel(1);
    let goto_worker = thread::spawn(move || {
        goto_tx
            .send(goto_worker_page.goto_with_cancel(
                &goto_url,
                GotoOptions::default().wait_until("load").timeout(5_000.0),
                Some(&goto_worker_token),
            ))
            .expect("send goto validation result");
    });
    hanging.wait_for_request();
    let goto_cancelled_at = Instant::now();
    goto_token.cancel();
    let goto_result = goto_rx
        .recv_timeout(Duration::from_secs(2))
        .expect("goto cancellation did not resolve");
    let goto_latency = goto_cancelled_at.elapsed();
    goto_worker.join().expect("join goto validation worker");
    assert!(matches!(goto_result, Err(Error::Cancelled)));
    let goto_probe_at = Instant::now();
    goto_page
        .evaluate("1 + 1", None, ActionOptions::timeout(2_000.0))
        .expect("goto page should recover after Page.stopLoading");
    let goto_probe = goto_probe_at.elapsed();

    let load_page = browser
        .new_page()
        .expect("create load-state validation page");
    let load_token = CancelToken::new();
    load_token.cancel();
    let load_cancelled_at = Instant::now();
    let load_result = load_page.wait_for_load_state_with_cancel(
        "networkidle",
        Duration::from_secs(2),
        Some(&load_token),
    );
    let load_latency = load_cancelled_at.elapsed();
    assert!(matches!(load_result, Err(Error::Cancelled)));
    let load_probe_at = Instant::now();
    load_page
        .evaluate("1 + 1", None, ActionOptions::timeout(2_000.0))
        .expect("load-state cancellation should leave page responsive");
    let load_probe = load_probe_at.elapsed();

    let evaluate_page = browser.new_page().expect("create evaluate validation page");
    let evaluate_signal = SignalServer::start();
    let evaluate_signal_url =
        serde_json::to_string(&evaluate_signal.url()).expect("encode evaluate signal URL");
    let evaluate_token = CancelToken::new();
    let evaluate_worker_token = evaluate_token.clone();
    let evaluate_worker_page = evaluate_page.clone();
    let (evaluate_tx, evaluate_rx) = mpsc::sync_channel(1);
    let evaluate_worker = thread::spawn(move || {
        evaluate_tx
            .send(evaluate_worker_page.evaluate_with_cancel(
                &format!(
                    r#"() => {{
                globalThis.validationEvaluateStarted = true;
                const signal = new XMLHttpRequest();
                signal.open('GET', {evaluate_signal_url}, false);
                signal.send();
                globalThis.validationEvaluateFinished = true;
                return 42;
            }}"#
                ),
                None,
                ActionOptions::timeout(2_000.0),
                Some(&evaluate_worker_token),
            ))
            .expect("send evaluate validation result");
    });
    evaluate_signal.wait_for_request();
    let evaluate_cancelled_at = Instant::now();
    evaluate_token.cancel();
    evaluate_signal.release();
    let evaluate_result = evaluate_rx
        .recv_timeout(Duration::from_secs(2))
        .expect("evaluate cancellation did not resolve");
    let evaluate_latency = evaluate_cancelled_at.elapsed();
    evaluate_worker
        .join()
        .expect("join evaluate validation worker");
    assert!(matches!(evaluate_result, Err(Error::Cancelled)));
    let evaluate_probe_at = Instant::now();
    let evaluate_finished = evaluate_page
        .evaluate(
            "globalThis.validationEvaluateFinished === true",
            None,
            ActionOptions::timeout(2_000.0),
        )
        .expect("probe remote evaluate completion");
    let evaluate_probe = evaluate_probe_at.elapsed();
    assert_eq!(evaluate_finished, serde_json::Value::Bool(true));

    println!("validation cancellation-depth table:");
    println!(
        "op=goto cancel={goto_latency:?} recovery_probe={goto_probe:?} remote=Page.stopLoading"
    );
    println!(
        "op=load-state cancel={load_latency:?} recovery_probe={load_probe:?} remote=no remote work"
    );
    println!("op=evaluate cancel={evaluate_latency:?} recovery_probe={evaluate_probe:?} remote=continued after synchronized signal");

    browser
        .close()
        .expect("close validation cancellation browser");
}

#[test]
fn validation_p2_click_cancel_before_dispatch_is_deterministic() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping validation P2 pre-dispatch cancellation: Chromium unavailable");
        return;
    }

    let browser = chromium()
        .launch(LaunchOptions::default().arg("--no-proxy-server"))
        .expect("launch pre-dispatch cancellation browser");
    let page = browser
        .new_page()
        .expect("create pre-dispatch cancellation page");
    let signal = SignalServer::start();
    let signal_url = serde_json::to_string(&signal.url()).expect("encode pre-dispatch signal URL");
    page.evaluate(
        &format!(
            r#"() => {{
                document.body.innerHTML = `
                    <div id="cancel-wrap" style="position:absolute;left:20px;top:1800px;width:180px;height:50px">
                      <button id="cancel-before" style="width:180px;height:50px">Cancel before dispatch</button>
                      <div id="cover" style="position:absolute;inset:0;z-index:2"></div>
                    </div>`;
                globalThis.validationPreDispatchClicks = 0;
                document.querySelector('#cancel-before').addEventListener('click', () => {{
                    globalThis.validationPreDispatchClicks += 1;
                }});
                addEventListener('scroll', () => fetch({signal_url}), {{ once: true }});
            }}"#
        ),
        None,
        ActionOptions::timeout(2_000.0),
    )
    .expect("install pre-dispatch cancellation fixture");

    let token = CancelToken::new();
    let worker_token = token.clone();
    let worker_page = page.clone();
    let (result_tx, result_rx) = mpsc::sync_channel(1);
    let worker = thread::spawn(move || {
        result_tx
            .send(worker_page.click_with_cancel(
                "#cancel-before",
                ActionOptions::timeout(5_000.0),
                Some(&worker_token),
            ))
            .expect("send pre-dispatch cancellation result");
    });

    signal.wait_for_request();
    assert!(
        token.try_cancel(),
        "cancellation must win while the target remains covered"
    );
    signal.release();
    page.evaluate(
        "document.querySelector('#cover').remove()",
        None,
        ActionOptions::timeout(2_000.0),
    )
    .expect("uncover target after cancellation");
    let result = result_rx
        .recv_timeout(Duration::from_secs(2))
        .expect("pre-dispatch cancellation did not resolve");
    worker
        .join()
        .expect("join pre-dispatch cancellation worker");
    assert!(matches!(result, Err(Error::Cancelled)));
    assert_eq!(
        page.evaluate(
            "globalThis.validationPreDispatchClicks",
            None,
            ActionOptions::timeout(2_000.0),
        )
        .expect("read pre-dispatch click count"),
        serde_json::json!(0)
    );

    browser
        .close()
        .expect("close pre-dispatch cancellation browser");
}

#[test]
fn validation_p2_click_cancel_after_commit_is_rejected() {
    if chromium().executable_path().is_none() {
        eprintln!("skipping validation P2 post-commit cancellation: Chromium unavailable");
        return;
    }

    let browser = chromium()
        .launch(LaunchOptions::default().arg("--no-proxy-server"))
        .expect("launch post-commit cancellation browser");
    let page = browser
        .new_page()
        .expect("create post-commit cancellation page");
    let signal = SignalServer::start();
    let signal_url = serde_json::to_string(&signal.url()).expect("encode post-commit signal URL");
    page.evaluate(
        &format!(
            r#"() => {{
                document.body.innerHTML = '<button id="cancel-after">Cancel after commit</button>';
                globalThis.validationCommittedEvents = [];
                const target = document.querySelector('#cancel-after');
                target.addEventListener('mousedown', event => {{
                    globalThis.validationCommittedEvents.push({{ type: event.type, trusted: event.isTrusted }});
                    const signal = new XMLHttpRequest();
                    signal.open('GET', {signal_url}, false);
                    signal.send();
                }});
                for (const name of ['mouseup', 'click']) {{
                    target.addEventListener(name, event => {{
                        globalThis.validationCommittedEvents.push({{ type: event.type, trusted: event.isTrusted }});
                    }});
                }}
            }}"#
        ),
        None,
        ActionOptions::timeout(2_000.0),
    )
    .expect("install post-commit cancellation fixture");

    let token = CancelToken::new();
    let worker_token = token.clone();
    let worker_page = page.clone();
    let (result_tx, result_rx) = mpsc::sync_channel(1);
    let worker = thread::spawn(move || {
        result_tx
            .send(worker_page.click_with_cancel(
                "#cancel-after",
                ActionOptions::timeout(5_000.0),
                Some(&worker_token),
            ))
            .expect("send post-commit cancellation result");
    });

    signal.wait_for_request();
    assert!(
        !token.try_cancel(),
        "cancellation must be rejected after mouse dispatch commits"
    );
    assert!(
        token.is_cancelled(),
        "late cancellation must remain visible to later operations"
    );
    signal.release();
    result_rx
        .recv_timeout(Duration::from_secs(2))
        .expect("post-commit click did not resolve")
        .expect("committed click result must stand");
    worker.join().expect("join post-commit click worker");

    let events = page
        .evaluate(
            "globalThis.validationCommittedEvents",
            None,
            ActionOptions::timeout(2_000.0),
        )
        .expect("read post-commit click evidence");
    let events = events.as_array().expect("post-commit click events");
    assert_eq!(
        events
            .iter()
            .map(|event| event["type"].as_str().expect("post-commit event type"))
            .collect::<Vec<_>>(),
        ["mousedown", "mouseup", "click"]
    );
    assert!(events
        .iter()
        .all(|event| event["trusted"] == serde_json::Value::Bool(true)));
    assert!(matches!(
        page.evaluate_with_cancel("1 + 1", None, ActionOptions::timeout(2_000.0), Some(&token),),
        Err(Error::Cancelled)
    ));

    browser
        .close()
        .expect("close post-commit cancellation browser");
}
