using System.Text.Json.Serialization;

namespace Rustwright;

public sealed class LaunchOptions
{
    [JsonPropertyName("headless")]
    public bool? Headless { get; init; }

    [JsonPropertyName("executable_path")]
    public string? ExecutablePath { get; init; }

    [JsonPropertyName("channel")]
    public string? Channel { get; init; }

    [JsonPropertyName("args")]
    public IReadOnlyList<string>? Args { get; init; }

    [JsonPropertyName("ignore_all_default_args")]
    public bool? IgnoreAllDefaultArgs { get; init; }

    [JsonPropertyName("ignore_default_args")]
    public IReadOnlyList<string>? IgnoreDefaultArgs { get; init; }

    [JsonPropertyName("timeout")]
    public double? Timeout { get; init; }

    [JsonPropertyName("user_data_dir")]
    public string? UserDataDir { get; init; }

    [JsonPropertyName("env")]
    public IReadOnlyDictionary<string, string>? Env { get; init; }

    [JsonPropertyName("chromium_sandbox")]
    public bool? ChromiumSandbox { get; init; }

    [JsonPropertyName("proxy")]
    public ProxyOptions? Proxy { get; init; }
}

public sealed class ProxyOptions
{
    [JsonPropertyName("server")]
    public required string Server { get; init; }

    [JsonPropertyName("bypass")]
    public string? Bypass { get; init; }

    [JsonPropertyName("username")]
    public string? Username { get; init; }

    [JsonPropertyName("password")]
    public string? Password { get; init; }
}

public sealed class GotoOptions
{
    public string? WaitUntil { get; init; }

    public double? Timeout { get; init; }

    public string? Referer { get; init; }
}

public sealed class ScreenshotOptions
{
    [JsonPropertyName("path")]
    public string? Path { get; init; }

    [JsonPropertyName("fullPage")]
    public bool? FullPage { get; init; }

    [JsonPropertyName("clip")]
    public ScreenshotClip? Clip { get; init; }

    [JsonPropertyName("timeout")]
    public double? Timeout { get; init; }

    [JsonPropertyName("type")]
    public string? Type { get; init; }

    [JsonPropertyName("quality")]
    public int? Quality { get; init; }

    [JsonPropertyName("omitBackground")]
    public bool? OmitBackground { get; init; }
}

public sealed class ScreenshotClip
{
    [JsonPropertyName("x")]
    public required double X { get; init; }

    [JsonPropertyName("y")]
    public required double Y { get; init; }

    [JsonPropertyName("width")]
    public required double Width { get; init; }

    [JsonPropertyName("height")]
    public required double Height { get; init; }
}
