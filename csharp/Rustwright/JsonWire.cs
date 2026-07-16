using System.Globalization;
using System.Numerics;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace Rustwright;

public sealed record RustwrightRegularExpression(string Pattern, string Flags);

public sealed record RustwrightJavaScriptError(string Name, string Message, string Stack);

internal static class JsonWire
{
    internal static readonly JsonSerializerOptions SerializerOptions = new()
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        PropertyNamingPolicy = null,
    };

    internal static object? Decode(string json)
    {
        var root = JsonNode.Parse(json);
        var references = new Dictionary<int, object>();
        return DecodeNode(root, references);
    }

    private static object? DecodeNode(JsonNode? node, Dictionary<int, object> references)
    {
        if (node is null)
        {
            return null;
        }

        if (node is JsonValue value)
        {
            return DecodeValue(value);
        }

        if (node is JsonArray array)
        {
            return array.Select(item => DecodeNode(item, references)).ToList();
        }

        var obj = node.AsObject();
        if (TryGetRefId(obj, "__rustwright_cdp_ref__", out var existingId))
        {
            if (!references.TryGetValue(existingId, out var existing))
            {
                throw new RustwrightException($"Evaluate wire referenced unknown object id {existingId}.");
            }

            return existing;
        }

        if (TryGetRefId(obj, "__rustwright_cdp_array__", out var arrayId) &&
            obj["items"] is JsonArray wrappedItems)
        {
            var decoded = new List<object?>(wrappedItems.Count);
            references[arrayId] = decoded;
            foreach (var item in wrappedItems)
            {
                decoded.Add(DecodeNode(item, references));
            }

            return decoded;
        }

        if (TryGetRefId(obj, "__rustwright_cdp_object__", out var objectId) &&
            obj["entries"] is JsonObject wrappedEntries)
        {
            var decoded = new Dictionary<string, object?>(StringComparer.Ordinal);
            references[objectId] = decoded;
            foreach (var entry in wrappedEntries)
            {
                decoded[entry.Key] = DecodeNode(entry.Value, references);
            }

            return decoded;
        }

        if (obj.TryGetPropertyValue("__rustwright_cdp_unserializable_value__", out var specialNode) &&
            specialNode is JsonValue specialValue &&
            specialValue.TryGetValue<string>(out var special))
        {
            return DecodeUnserializable(special);
        }

        // JavaScript has no direct C# equivalents for these values. The binding's
        // documented fallback follows the contract and maps them to null.
        if (obj.ContainsKey("__rustwright_cdp_undefined__") ||
            obj.ContainsKey("__rustwright_cdp_symbol__") ||
            obj.ContainsKey("__rustwright_cdp_function__"))
        {
            return null;
        }

        if (TryGetString(obj, "__rustwright_cdp_date__", out var date))
        {
            return DateTimeOffset.TryParse(
                date,
                CultureInfo.InvariantCulture,
                DateTimeStyles.RoundtripKind,
                out var parsedDate)
                ? parsedDate
                : date;
        }

        if (TryGetString(obj, "__rustwright_cdp_url__", out var url))
        {
            return Uri.TryCreate(url, UriKind.RelativeOrAbsolute, out var parsedUrl) ? parsedUrl : url;
        }

        if (obj["__rustwright_cdp_regexp__"] is JsonObject regularExpression)
        {
            return new RustwrightRegularExpression(
                regularExpression["p"]?.GetValue<string>() ?? string.Empty,
                regularExpression["f"]?.GetValue<string>() ?? string.Empty);
        }

        if (obj["__rustwright_cdp_error__"] is JsonObject error)
        {
            return new RustwrightJavaScriptError(
                error["name"]?.GetValue<string>() ?? "Error",
                error["message"]?.GetValue<string>() ?? string.Empty,
                error["stack"]?.GetValue<string>() ?? string.Empty);
        }

        var ordinary = new Dictionary<string, object?>(StringComparer.Ordinal);
        foreach (var property in obj)
        {
            ordinary[property.Key] = DecodeNode(property.Value, references);
        }

        return ordinary;
    }

    private static object DecodeValue(JsonValue value)
    {
        if (value.TryGetValue<bool>(out var boolean))
        {
            return boolean;
        }

        if (value.TryGetValue<string>(out var text))
        {
            return text;
        }

        if (value.TryGetValue<long>(out var integer))
        {
            return integer;
        }

        if (value.TryGetValue<decimal>(out var decimalValue))
        {
            return decimalValue;
        }

        if (value.TryGetValue<double>(out var doubleValue))
        {
            return doubleValue;
        }

        return value.ToJsonString();
    }

    private static object DecodeUnserializable(string value) => value switch
    {
        "NaN" => double.NaN,
        "Infinity" => double.PositiveInfinity,
        "-Infinity" => double.NegativeInfinity,
        "-0" => BitConverter.Int64BitsToDouble(long.MinValue),
        _ when value.EndsWith('n') &&
               BigInteger.TryParse(value[..^1], NumberStyles.Integer, CultureInfo.InvariantCulture, out var integer)
            => integer,
        _ => value,
    };

    private static bool TryGetRefId(JsonObject obj, string propertyName, out int id)
    {
        id = default;
        return obj[propertyName] is JsonValue value && value.TryGetValue<int>(out id);
    }

    private static bool TryGetString(JsonObject obj, string propertyName, out string value)
    {
        if (obj[propertyName] is JsonValue node &&
            node.TryGetValue<string>(out var decoded) &&
            decoded is not null)
        {
            value = decoded;
            return true;
        }

        value = string.Empty;
        return false;
    }
}
