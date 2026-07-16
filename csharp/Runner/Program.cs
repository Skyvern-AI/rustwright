using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using Rustwright;
using Rustwright.Runner;

return RunnerApplication.Run(args);

internal static class RunnerApplication
{
    private static readonly JsonSerializerOptions OutputOptions = new()
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    internal static int Run(string[] arguments)
    {
        try
        {
            var options = RunnerOptions.Parse(arguments);
            var manifest = ManifestParser.Parse(options.ManifestPath);
            var selectedCases = SelectCases(manifest.Cases, options.CaseIds);
            NativeLibraryLoader.Configure(options.LibraryPath);

            var results = new List<CaseResult>(selectedCases.Count);
            Exception? browserCloseError = null;
            var browser = Chromium.Launch(new LaunchOptions { Headless = true });
            try
            {
                foreach (var benchmarkCase in selectedCases)
                {
                    results.Add(RunCase(browser, benchmarkCase));
                }
            }
            finally
            {
                try
                {
                    browser.Close();
                }
                catch (Exception error)
                {
                    browserCloseError = error;
                }
            }

            WriteResults(options.OutputPath, results);
            if (browserCloseError is not null)
            {
                Console.Error.WriteLine($"browser close failed: {browserCloseError.Message}");
                return 2;
            }

            return results.All(result => result.Ok) ? 0 : 1;
        }
        catch (Exception error) when (error is ArgumentException or ManifestException or IOException or JsonException)
        {
            Console.Error.WriteLine($"runner: {error.Message}");
            return 2;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine($"runner: {error.Message}");
            return 2;
        }
    }

    private static CaseResult RunCase(Browser browser, ManifestCase benchmarkCase)
    {
        var stopwatch = Stopwatch.StartNew();
        var captures = new Dictionary<string, object?>(StringComparer.Ordinal);
        Page? page = null;
        string? error = null;

        try
        {
            page = browser.NewPage();
            for (var index = 0; index < benchmarkCase.Steps.Count; index++)
            {
                try
                {
                    ExecuteStep(page, benchmarkCase, benchmarkCase.Steps[index], captures);
                }
                catch (Exception stepError)
                {
                    error = $"step {index + 1}: {stepError.Message}";
                    break;
                }
            }
        }
        catch (Exception pageError)
        {
            error = $"page creation: {pageError.Message}";
        }
        finally
        {
            if (page is not null)
            {
                try
                {
                    page.Close();
                }
                catch (Exception closeError)
                {
                    error ??= $"page close: {closeError.Message}";
                }
            }
        }

        stopwatch.Stop();
        return new CaseResult(benchmarkCase.Id, error is null, captures, stopwatch.Elapsed.TotalMilliseconds, error);
    }

    private static void ExecuteStep(
        Page page,
        ManifestCase benchmarkCase,
        JsonElement step,
        Dictionary<string, object?> captures)
    {
        var operation = step.GetProperty("op").GetString()!;
        switch (operation)
        {
            case "goto":
                var url = step.TryGetProperty("url", out var literalUrl)
                    ? literalUrl.GetString()!
                    : DataUrls.FromHtml(benchmarkCase.Html!);
                var gotoOptions = step.TryGetProperty("waitUntil", out var waitUntil)
                    ? new GotoOptions { WaitUntil = waitUntil.GetString() }
                    : null;
                _ = page.Goto(url, gotoOptions);
                break;
            case "click":
                page.Click(step.GetProperty("selector").GetString()!);
                break;
            case "fill":
                page.Fill(
                    step.GetProperty("selector").GetString()!,
                    step.GetProperty("value").GetString()!);
                break;
            case "title":
                captures[step.GetProperty("capture").GetString()!] = page.Title();
                break;
            case "textContent":
                captures[step.GetProperty("capture").GetString()!] =
                    page.TextContent(step.GetProperty("selector").GetString()!);
                break;
            case "evaluate":
                captures[step.GetProperty("capture").GetString()!] =
                    step.TryGetProperty("arg", out var argument)
                        ? page.Evaluate(step.GetProperty("expression").GetString()!, argument)
                        : page.Evaluate(step.GetProperty("expression").GetString()!);
                break;
            case "screenshot":
                captures[step.GetProperty("capture").GetString()!] = page.Screenshot().Length;
                break;
            case "assertTitle":
                AssertString(page.Title(), step, "title");
                break;
            case "assertText":
                AssertString(
                    page.TextContent(step.GetProperty("selector").GetString()!),
                    step,
                    $"textContent({step.GetProperty("selector").GetString()})");
                break;
            case "assertEval":
                var actual = page.Evaluate(step.GetProperty("expression").GetString()!);
                AssertJsonEqual(actual, step.GetProperty("equals"));
                break;
            default:
                throw new InvalidOperationException($"unknown op '{operation}'");
        }
    }

    private static void AssertString(string? actual, JsonElement step, string description)
    {
        if (actual is null)
        {
            throw new InvalidOperationException($"{description} was null");
        }

        if (step.TryGetProperty("equals", out var equals))
        {
            var expected = equals.GetString()!;
            if (!string.Equals(actual, expected, StringComparison.Ordinal))
            {
                throw new InvalidOperationException(
                    $"expected {description} to equal {JsonSerializer.Serialize(expected)}, got {JsonSerializer.Serialize(actual)}");
            }
        }
        else
        {
            var expected = step.GetProperty("contains").GetString()!;
            if (!actual.Contains(expected, StringComparison.Ordinal))
            {
                throw new InvalidOperationException(
                    $"expected {description} to contain {JsonSerializer.Serialize(expected)}, got {JsonSerializer.Serialize(actual)}");
            }
        }
    }

    private static void AssertJsonEqual(object? actual, JsonElement expected)
    {
        var actualNode = JsonSerializer.SerializeToNode(actual);
        var expectedNode = JsonNode.Parse(expected.GetRawText());
        if (!JsonNode.DeepEquals(actualNode, expectedNode))
        {
            throw new InvalidOperationException(
                $"expected evaluate result {expected.GetRawText()}, got {JsonSerializer.Serialize(actual)}");
        }
    }

    private static IReadOnlyList<ManifestCase> SelectCases(
        IReadOnlyList<ManifestCase> cases,
        HashSet<string>? requestedIds)
    {
        if (requestedIds is null)
        {
            return cases;
        }

        var knownIds = cases.Select(benchmarkCase => benchmarkCase.Id).ToHashSet(StringComparer.Ordinal);
        var unknownIds = requestedIds.Where(id => !knownIds.Contains(id)).Order(StringComparer.Ordinal).ToArray();
        if (unknownIds.Length > 0)
        {
            throw new ArgumentException($"unknown case id(s): {string.Join(", ", unknownIds)}");
        }

        return cases.Where(benchmarkCase => requestedIds.Contains(benchmarkCase.Id)).ToArray();
    }

    private static void WriteResults(string outputPath, IReadOnlyList<CaseResult> results)
    {
        var fullPath = Path.GetFullPath(outputPath);
        var directory = Path.GetDirectoryName(fullPath);
        if (!string.IsNullOrEmpty(directory))
        {
            Directory.CreateDirectory(directory);
        }

        var output = new RunnerResult("csharp", results);
        File.WriteAllText(fullPath, JsonSerializer.Serialize(output, OutputOptions) + Environment.NewLine);
    }
}

internal sealed record RunnerResult(
    [property: JsonPropertyName("lang")] string Lang,
    [property: JsonPropertyName("results")] IReadOnlyList<CaseResult> Results);

internal sealed record CaseResult(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("ok")] bool Ok,
    [property: JsonPropertyName("captures")] IReadOnlyDictionary<string, object?> Captures,
    [property: JsonPropertyName("ms")] double Milliseconds,
    [property: JsonPropertyName("error")]
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Error);

internal sealed record RunnerOptions(
    string ManifestPath,
    string LibraryPath,
    string OutputPath,
    HashSet<string>? CaseIds)
{
    internal static RunnerOptions Parse(string[] arguments)
    {
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var index = 0; index < arguments.Length; index += 2)
        {
            if (index + 1 >= arguments.Length)
            {
                throw Usage($"missing value for '{arguments[index]}'");
            }

            var option = arguments[index];
            if (option is not ("--manifest" or "--lib" or "--out" or "--cases"))
            {
                throw Usage($"unknown option '{option}'");
            }

            if (!values.TryAdd(option, arguments[index + 1]))
            {
                throw Usage($"duplicate option '{option}'");
            }
        }

        foreach (var required in new[] { "--manifest", "--lib", "--out" })
        {
            if (!values.TryGetValue(required, out var value) || string.IsNullOrWhiteSpace(value))
            {
                throw Usage($"{required} is required");
            }
        }

        HashSet<string>? caseIds = null;
        if (values.TryGetValue("--cases", out var casesValue))
        {
            var ids = casesValue.Split(',', StringSplitOptions.None);
            if (ids.Any(string.IsNullOrEmpty))
            {
                throw Usage("--cases must be a comma-separated list of nonempty ids");
            }

            caseIds = ids.ToHashSet(StringComparer.Ordinal);
        }

        return new RunnerOptions(values["--manifest"], values["--lib"], values["--out"], caseIds);
    }

    private static ArgumentException Usage(string message) => new(
        $"{message}{Environment.NewLine}" +
        "usage: runner --manifest <path.json> --lib <native-library> --out <results.json> [--cases id1,id2]");
}
