# Rustwright for .NET

Rustwright is a synchronous .NET 8 binding for Rustwright's Rust-powered Chromium CDP engine. The package includes the matching native library for supported macOS, Linux, and Windows runtime identifiers.

```csharp
using Rustwright;

using var browser = Chromium.Launch(new LaunchOptions { Headless = true });
using var page = browser.NewPage();
page.Goto("data:text/html,<title>Hello from Rustwright</title>");
Console.WriteLine(page.Title());
```

For source builds, API details, and the binding runner, see the [repository](https://github.com/Skyvern-AI/rustwright/tree/main/csharp).
