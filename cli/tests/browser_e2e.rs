use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;

use rustwright_cli::session::{BrowserAction, BrowserSession, LaunchConfig};
use serde_json::Value;

#[test]
#[ignore = "requires a Chromium executable"]
fn browser_session_navigates_snapshots_and_acts_on_refs() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let server = thread::spawn(move || {
        for _ in 0..4 {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 4096];
            let _ = stream.read(&mut request);
            let body = r#"<!doctype html>
<title>Rustwright agent test</title>
<h1>Test form</h1>
<label for="name">Full name</label><input id="name">
<span id="submit-label">Submit form</span>
<button id="submit" aria-labelledby="submit-label" onclick="window.lastClickTrusted = event.isTrusted; document.querySelector('h1').textContent = document.querySelector('#name').value"></button>"#;
            let _ = write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
        }
    });

    let executable_path = std::env::var("RUSTWRIGHT_CHROMIUM").ok();
    let mut session = BrowserSession::new(LaunchConfig {
        headed: false,
        executable_path,
    });
    let opened = session
        .execute(BrowserAction::Open {
            url: Some(format!("http://{address}")),
        })
        .unwrap();
    let snapshot = opened["snapshot"].as_str().unwrap();
    assert!(snapshot.contains("Test form"));
    assert!(snapshot.contains("textbox \"Full name\" [ref=@e1]"));
    assert!(snapshot.contains("button \"Submit form\" [ref=@e2]"));

    session
        .execute(BrowserAction::Fill {
            target: "@e1".to_string(),
            text: "Ada".to_string(),
        })
        .unwrap();
    session
        .execute(BrowserAction::Click {
            target: "@e2".to_string(),
        })
        .unwrap();
    let trusted = session
        .execute(BrowserAction::Evaluate {
            expression: "window.lastClickTrusted".to_string(),
        })
        .unwrap();
    assert_eq!(trusted["value"], true);
    let text = session
        .execute(BrowserAction::Text {
            target: Some("h1".to_string()),
        })
        .unwrap();
    assert_eq!(text["text"], Value::String("Ada".to_string()));
    let evaluated = session
        .execute(BrowserAction::Evaluate {
            expression: "({ name: 'Ada', values: [1, 2] })".to_string(),
        })
        .unwrap();
    assert_eq!(
        evaluated["value"],
        serde_json::json!({ "name": "Ada", "values": [1, 2] })
    );
    assert!(session
        .execute(BrowserAction::Text {
            target: Some("#missing".to_string()),
        })
        .is_err());
    session.execute(BrowserAction::Close).unwrap();
    assert!(session.execute(BrowserAction::Title).is_err());

    drop(server);
}
