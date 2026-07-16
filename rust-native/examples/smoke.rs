use rustwright::{chromium, ActionOptions, GotoOptions, LaunchOptions, ScreenshotOptions};
use serde_json::json;

fn main() -> rustwright::Result<()> {
    let html = r#"<!doctype html><html><head><title>Rustwright Rust Smoke</title></head><body><h1 id="message">ready</h1><input id="name"><button id="go" onclick="document.querySelector('#message').textContent=document.querySelector('#name').value">Go</button></body></html>"#;
    let url = format!("data:text/html;charset=utf-8,{}", percent_encode(html));
    let browser = chromium().launch(LaunchOptions::default())?;
    let result = (|| {
        let page = browser.new_page()?;
        page.goto(&url, GotoOptions::default())?;
        let title = page.title(ActionOptions::default())?;
        let before = page.text_content("#message", ActionOptions::default())?;
        page.fill("#name", "Rustwright for Rust", ActionOptions::default())?;
        page.click("#go", ActionOptions::default())?;
        let after = page.text_content("#message", ActionOptions::default())?;
        let value = page.evaluate(
            "document.querySelector('#name').value",
            None,
            ActionOptions::default(),
        )?;
        let screenshot = page.screenshot(ScreenshotOptions::default())?;
        println!(
            "{}",
            json!({
                "title": title,
                "before": before,
                "after": after,
                "value": value,
                "screenshotBytes": screenshot.len(),
            })
        );
        page.close(Default::default())
    })();
    let close_result = browser.close();
    result.and(close_result)
}

fn percent_encode(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}
