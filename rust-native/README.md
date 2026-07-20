# rustwright

Idiomatic **native Rust API** for the [Rustwright](https://github.com/Skyvern-AI/rustwright)
Chromium CDP engine — a Rust rewrite of Playwright that drives Chromium from an
in-process async CDP client (no Node driver subprocess).

This crate is a thin, ergonomic wrapper over `rustwright-core`. It runs the engine
in-process; there is no separate binding library to load.

```rust
use rustwright::{chromium, LaunchOptions};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let browser = chromium().launch(LaunchOptions::default())?;
    let page = browser.new_page()?;
    page.goto("https://example.com", None)?;
    println!("{}", page.title(None)?);
    browser.close()?;
    Ok(())
}
```

Alpha; Chromium-only. See the [main project](https://github.com/Skyvern-AI/rustwright)
for the full API surface, the shared binding contract, and the other language
bindings (Python, Node, Go, Java, C#/.NET, Ruby, PHP).

## License

[MIT](https://github.com/Skyvern-AI/rustwright/blob/main/LICENSE)
