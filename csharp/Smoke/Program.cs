using System.Text.Json;
using Rustwright;

const string html = """
    <!doctype html>
    <html>
      <head><title>Rustwright C# Smoke</title></head>
      <body>
        <h1 id="message">ready</h1>
        <input id="name" />
        <button id="go" onclick="document.querySelector('#message').textContent = document.querySelector('#name').value">Go</button>
      </body>
    </html>
    """;

try
{
    var libraryPath = ParseLibraryPath(args);
    if (libraryPath is not null)
    {
        NativeLibraryLoader.Configure(libraryPath);
    }

    using var browser = Chromium.Launch(new LaunchOptions { Headless = true });
    using var page = browser.NewPage();
    page.Goto(DataUrls.FromHtml(html));

    var title = page.Title();
    var before = page.TextContent("#message");
    page.Fill("#name", "Rustwright for C#");
    page.Click("#go");
    var after = page.TextContent("#message");
    var value = page.Evaluate("document.querySelector('#name').value");
    var screenshotPath = Path.Combine(Path.GetTempPath(), $"rustwright-csharp-smoke-{Environment.ProcessId}.png");
    var screenshot = page.Screenshot(new ScreenshotOptions { Path = screenshotPath });

    Console.WriteLine(JsonSerializer.Serialize(new
    {
        title,
        before,
        after,
        value,
        screenshotBytes = screenshot.Length,
    }));

    return 0;
}
catch (Exception error)
{
    Console.Error.WriteLine(error.Message);
    return 1;
}

static string? ParseLibraryPath(string[] arguments)
{
    if (arguments.Length == 0)
    {
        return null;
    }

    if (arguments.Length == 2 && arguments[0] == "--lib")
    {
        return arguments[1];
    }

    throw new ArgumentException("usage: smoke [--lib <path>]");
}
