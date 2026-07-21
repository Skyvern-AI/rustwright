use std::{
    io::Read,
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

fn cancel_thread<T: Send + 'static>(
    token: &CancelToken,
    operation: impl FnOnce() -> rustwright::Result<T> + Send + 'static,
) -> (rustwright::Result<T>, Duration) {
    let (result_tx, result_rx) = mpsc::sync_channel(1);
    let worker = thread::spawn(move || {
        result_tx
            .send(operation())
            .expect("send validation operation result");
    });
    thread::sleep(Duration::from_millis(75));
    let cancelled_at = Instant::now();
    token.cancel();
    let result = result_rx
        .recv_timeout(Duration::from_secs(2))
        .expect("cancelled validation operation did not resolve");
    let latency = cancelled_at.elapsed();
    worker.join().expect("join validation operation thread");
    (result, latency)
}

#[test]
fn validation_p2_promoted_operations_report_local_and_remote_cancel_depth() {
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
    let load_worker_token = load_token.clone();
    let load_worker_page = load_page.clone();
    let (load_result, load_latency) = cancel_thread(&load_token, move || {
        load_worker_page.wait_for_load_state_with_cancel(
            "networkidle",
            Duration::from_secs(2),
            Some(&load_worker_token),
        )
    });
    assert!(matches!(load_result, Err(Error::Cancelled)));
    let load_probe_at = Instant::now();
    load_page
        .evaluate("1 + 1", None, ActionOptions::timeout(2_000.0))
        .expect("load-state cancellation should leave page responsive");
    let load_probe = load_probe_at.elapsed();

    let click_page = browser.new_page().expect("create click validation page");
    click_page
        .evaluate(
            r#"() => {
                document.body.innerHTML = '<button id="hang">hang</button>';
                document.querySelector('#hang').addEventListener('click', () => {
                    globalThis.validationClickStarted = true;
                    const end = performance.now() + 800;
                    while (performance.now() < end) {}
                    globalThis.validationClickFinished = true;
                });
            }"#,
            None,
            ActionOptions::timeout(2_000.0),
        )
        .expect("install hanging click handler");
    let click_token = CancelToken::new();
    let click_worker_token = click_token.clone();
    let click_worker_page = click_page.clone();
    let (click_result, click_latency) = cancel_thread(&click_token, move || {
        click_worker_page.click_with_cancel(
            "#hang",
            ActionOptions::timeout(2_000.0),
            Some(&click_worker_token),
        )
    });
    assert!(matches!(click_result, Err(Error::Cancelled)));
    let click_probe_at = Instant::now();
    let click_finished = click_page
        .evaluate(
            "globalThis.validationClickFinished === true",
            None,
            ActionOptions::timeout(2_000.0),
        )
        .expect("probe remote click completion");
    let click_probe = click_probe_at.elapsed();
    assert_eq!(click_finished, serde_json::Value::Bool(true));

    let evaluate_page = browser.new_page().expect("create evaluate validation page");
    let evaluate_token = CancelToken::new();
    let evaluate_worker_token = evaluate_token.clone();
    let evaluate_worker_page = evaluate_page.clone();
    let (evaluate_result, evaluate_latency) = cancel_thread(&evaluate_token, move || {
        evaluate_worker_page.evaluate_with_cancel(
            r#"() => {
                globalThis.validationEvaluateStarted = true;
                const end = performance.now() + 800;
                while (performance.now() < end) {}
                globalThis.validationEvaluateFinished = true;
                return 42;
            }"#,
            None,
            ActionOptions::timeout(2_000.0),
            Some(&evaluate_worker_token),
        )
    });
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
    println!("op=click cancel={click_latency:?} recovery_probe={click_probe:?} remote=continued to bounded loop completion");
    println!("op=evaluate cancel={evaluate_latency:?} recovery_probe={evaluate_probe:?} remote=continued to bounded loop completion");

    browser
        .close()
        .expect("close validation cancellation browser");
}
