using System.Text.Json;

namespace Rustwright.Runner;

internal sealed record Manifest(IReadOnlyList<ManifestCase> Cases);

internal sealed record ManifestCase(string Id, string? Html, IReadOnlyList<JsonElement> Steps);

internal static class ManifestParser
{
    private static readonly HashSet<string> RootProperties = ["version", "cases"];
    private static readonly HashSet<string> CaseProperties = ["id", "description", "html", "url", "steps"];

    internal static Manifest Parse(string path)
    {
        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(File.ReadAllText(path), new JsonDocumentOptions
            {
                AllowTrailingCommas = false,
                CommentHandling = JsonCommentHandling.Disallow,
                MaxDepth = 128,
            });
        }
        catch (Exception error) when (error is IOException or JsonException)
        {
            throw new ManifestException($"cannot read manifest: {error.Message}");
        }

        using (document)
        {
            var root = document.RootElement;
            RequireObject(root, "manifest");
            ValidatePropertySet(root, RootProperties, ["version", "cases"], "manifest");

            var version = root.GetProperty("version");
            if (version.ValueKind != JsonValueKind.Number || !version.TryGetInt32(out var versionNumber) || versionNumber != 1)
            {
                throw new ManifestException("manifest.version must be 1");
            }

            var casesElement = root.GetProperty("cases");
            if (casesElement.ValueKind != JsonValueKind.Array || casesElement.GetArrayLength() == 0)
            {
                throw new ManifestException("manifest.cases must be a nonempty array");
            }

            var ids = new HashSet<string>(StringComparer.Ordinal);
            var cases = new List<ManifestCase>(casesElement.GetArrayLength());
            var caseIndex = 0;
            foreach (var caseElement in casesElement.EnumerateArray())
            {
                cases.Add(ParseCase(caseElement, caseIndex, ids));
                caseIndex++;
            }

            return new Manifest(cases);
        }
    }

    private static ManifestCase ParseCase(JsonElement element, int caseIndex, HashSet<string> ids)
    {
        var context = $"cases[{caseIndex}]";
        RequireObject(element, context);
        ValidatePropertySet(element, CaseProperties, ["id", "steps"], context);

        var id = RequiredNonemptyString(element, "id", context);
        if (!ids.Add(id))
        {
            throw new ManifestException($"duplicate case id '{id}'");
        }

        OptionalString(element, "description", context);
        OptionalString(element, "html", context);
        OptionalString(element, "url", context);
        var html = element.TryGetProperty("html", out var htmlElement) ? htmlElement.GetString() : null;

        var stepsElement = element.GetProperty("steps");
        if (stepsElement.ValueKind != JsonValueKind.Array || stepsElement.GetArrayLength() == 0)
        {
            throw new ManifestException($"{context}.steps must be a nonempty array");
        }

        var captures = new HashSet<string>(StringComparer.Ordinal);
        var steps = new List<JsonElement>(stepsElement.GetArrayLength());
        var stepIndex = 0;
        foreach (var step in stepsElement.EnumerateArray())
        {
            ValidateStep(step, html is not null, context, stepIndex, captures);
            steps.Add(step.Clone());
            stepIndex++;
        }

        return new ManifestCase(id, html, steps);
    }

    private static void ValidateStep(
        JsonElement step,
        bool hasCaseHtml,
        string caseContext,
        int stepIndex,
        HashSet<string> captures)
    {
        var context = $"{caseContext}.steps[{stepIndex}]";
        RequireObject(step, context);
        if (!step.TryGetProperty("op", out var operationElement) || operationElement.ValueKind != JsonValueKind.String)
        {
            throw new ManifestException($"{context}.op must be a string");
        }

        var operation = operationElement.GetString()!;
        switch (operation)
        {
            case "goto":
                ValidateGoto(step, hasCaseHtml, context);
                break;
            case "click":
                ValidatePropertySet(step, ["op", "selector"], ["op", "selector"], context);
                _ = RequiredNonemptyString(step, "selector", context);
                break;
            case "fill":
                ValidatePropertySet(step, ["op", "selector", "value"], ["op", "selector", "value"], context);
                _ = RequiredNonemptyString(step, "selector", context);
                RequiredString(step, "value", context);
                break;
            case "title":
                ValidateCapture(step, ["op", "capture"], context, captures);
                break;
            case "textContent":
                ValidateCapture(step, ["op", "selector", "capture"], context, captures);
                _ = RequiredNonemptyString(step, "selector", context);
                break;
            case "evaluate":
                ValidatePropertySet(step, ["op", "expression", "arg", "capture"], ["op", "expression", "capture"], context);
                _ = RequiredNonemptyString(step, "expression", context);
                AddCapture(RequiredNonemptyString(step, "capture", context), captures, context);
                break;
            case "screenshot":
                ValidateCapture(step, ["op", "capture"], context, captures);
                break;
            case "assertTitle":
                ValidateAssertion(step, ["op", "equals", "contains"], context, selectorRequired: false);
                break;
            case "assertText":
                ValidateAssertion(step, ["op", "selector", "equals", "contains"], context, selectorRequired: true);
                break;
            case "assertEval":
                ValidatePropertySet(step, ["op", "expression", "equals"], ["op", "expression", "equals"], context);
                _ = RequiredNonemptyString(step, "expression", context);
                break;
            default:
                throw new ManifestException($"{context}: unknown op '{operation}'");
        }
    }

    private static void ValidateGoto(JsonElement step, bool hasCaseHtml, string context)
    {
        ValidatePropertySet(step, ["op", "url", "useCaseHtml", "waitUntil"], ["op"], context);
        var hasUrl = step.TryGetProperty("url", out var url);
        var usesCaseHtml = step.TryGetProperty("useCaseHtml", out var useCaseHtml);
        if (hasUrl == usesCaseHtml)
        {
            throw new ManifestException($"{context} must have exactly one of url or useCaseHtml");
        }

        if (hasUrl)
        {
            if (url.ValueKind != JsonValueKind.String || string.IsNullOrEmpty(url.GetString()))
            {
                throw new ManifestException($"{context}.url must be a nonempty string");
            }
        }
        else if (useCaseHtml.ValueKind != JsonValueKind.True)
        {
            throw new ManifestException($"{context}.useCaseHtml must be true");
        }
        else if (!hasCaseHtml)
        {
            throw new ManifestException($"{context} uses case HTML but the case has no html property");
        }

        if (step.TryGetProperty("waitUntil", out var waitUntil))
        {
            if (waitUntil.ValueKind != JsonValueKind.String ||
                waitUntil.GetString() is not ("load" or "domcontentloaded" or "networkidle" or "commit"))
            {
                throw new ManifestException($"{context}.waitUntil is invalid");
            }
        }
    }

    private static void ValidateCapture(
        JsonElement step,
        HashSet<string> allowed,
        string context,
        HashSet<string> captures)
    {
        ValidatePropertySet(step, allowed, allowed, context);
        AddCapture(RequiredNonemptyString(step, "capture", context), captures, context);
    }

    private static void ValidateAssertion(
        JsonElement step,
        HashSet<string> allowed,
        string context,
        bool selectorRequired)
    {
        var required = selectorRequired ? new HashSet<string>(["op", "selector"]) : new HashSet<string>(["op"]);
        ValidatePropertySet(step, allowed, required, context);
        if (selectorRequired)
        {
            _ = RequiredNonemptyString(step, "selector", context);
        }

        var hasEquals = step.TryGetProperty("equals", out var equals);
        var hasContains = step.TryGetProperty("contains", out var contains);
        if (hasEquals == hasContains)
        {
            throw new ManifestException($"{context} must have exactly one of equals or contains");
        }

        var predicate = hasEquals ? equals : contains;
        if (predicate.ValueKind != JsonValueKind.String)
        {
            throw new ManifestException($"{context} assertion predicate must be a string");
        }
    }

    private static void ValidatePropertySet(
        JsonElement element,
        HashSet<string> allowed,
        HashSet<string> required,
        string context)
    {
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in element.EnumerateObject())
        {
            if (!seen.Add(property.Name))
            {
                throw new ManifestException($"{context} has duplicate property '{property.Name}'");
            }

            if (!allowed.Contains(property.Name))
            {
                throw new ManifestException($"{context} has unknown property '{property.Name}'");
            }
        }

        foreach (var property in required)
        {
            if (!seen.Contains(property))
            {
                throw new ManifestException($"{context} is missing required property '{property}'");
            }
        }
    }

    private static string RequiredNonemptyString(JsonElement element, string property, string context)
    {
        var value = RequiredString(element, property, context);
        if (value.Length == 0)
        {
            throw new ManifestException($"{context}.{property} must be nonempty");
        }

        return value;
    }

    private static string RequiredString(JsonElement element, string property, string context)
    {
        var value = element.GetProperty(property);
        if (value.ValueKind != JsonValueKind.String)
        {
            throw new ManifestException($"{context}.{property} must be a string");
        }

        return value.GetString()!;
    }

    private static void OptionalString(JsonElement element, string property, string context)
    {
        if (element.TryGetProperty(property, out var value) && value.ValueKind != JsonValueKind.String)
        {
            throw new ManifestException($"{context}.{property} must be a string");
        }
    }

    private static void AddCapture(string capture, HashSet<string> captures, string context)
    {
        if (!captures.Add(capture))
        {
            throw new ManifestException($"{context}: duplicate capture '{capture}'");
        }
    }

    private static void RequireObject(JsonElement element, string context)
    {
        if (element.ValueKind != JsonValueKind.Object)
        {
            throw new ManifestException($"{context} must be an object");
        }
    }
}

internal sealed class ManifestException : Exception
{
    internal ManifestException(string message)
        : base(message)
    {
    }
}
