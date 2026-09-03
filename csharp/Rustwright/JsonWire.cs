using System.Globalization;
using System.Numerics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
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
        NativeLibraryLoader.EnsureConfigured();
        var status = NativeMethods.WireGraphParse(json, out var graph);
        NativeError.ThrowIfFailed(status);
        return DecodeGraph(graph);
    }

    internal static object? DecodeWire(IntPtr wireJson)
    {
        NativeLibraryLoader.EnsureConfigured();
        var status = NativeMethods.WireGraphParse(wireJson, out var graph);
        NativeError.ThrowIfFailed(status);
        return DecodeGraph(graph);
    }

    private static object? DecodeGraph(IntPtr graph)
    {
        if (graph == IntPtr.Zero)
        {
            throw new RustwrightException("rw_wire_graph_parse succeeded but returned NULL.");
        }

        try
        {
            return new Materializer(graph).Materialize();
        }
        finally
        {
            NativeMethods.WireGraphFree(graph);
        }
    }

    private sealed class Materializer
    {
        private const int NodeNull = 0;
        private const int NodeBool = 1;
        private const int NodeSigned = 2;
        private const int NodeUnsigned = 3;
        private const int NodeFloat = 4;
        private const int NodeString = 5;
        private const int NodeArray = 6;
        private const int NodeObject = 7;
        private const int NodeLeaf = 8;

        private const int LeafUnserializable = 0;
        private const int LeafBigInt = 1;
        private const int LeafDate = 2;
        private const int LeafRegexp = 3;
        private const int LeafUrl = 4;
        private const int LeafError = 5;
        private const int LeafUndefined = 6;
        private const int LeafSymbol = 7;
        private const int LeafFunction = 8;

        private readonly IntPtr graph;
        private object?[] values = [];
        private int[] kinds = [];

        internal Materializer(IntPtr graph)
        {
            this.graph = graph;
        }

        internal object? Materialize()
        {
            var count = GetNodeCount();
            var root = GetRoot();
            values = new object?[count];
            kinds = new int[count];

            // First pass allocates every identity-bearing placeholder.
            for (var node = 0; node < count; node++)
            {
                var nodeId = (nuint)node;
                var kind = GetNodeKind(nodeId);
                kinds[node] = kind;
                values[node] = kind switch
                {
                    NodeNull => null,
                    NodeBool => GetBool(nodeId),
                    NodeSigned => GetSigned(nodeId),
                    NodeUnsigned => DecodeUnsigned(GetUnsigned(nodeId)),
                    NodeFloat => GetFloat(nodeId),
                    NodeString => GetString(nodeId),
                    NodeArray => EmptyArray(GetArrayLength(nodeId)),
                    NodeObject => new Dictionary<string, object?>(StringComparer.Ordinal),
                    NodeLeaf => DecodeLeaf(nodeId),
                    _ => throw new RustwrightException($"Native wire graph returned unknown node kind {kind}."),
                };
            }

            // Second pass fills edges using the already allocated values.
            for (var node = 0; node < count; node++)
            {
                var nodeId = (nuint)node;
                if (kinds[node] == NodeArray)
                {
                    var array = (List<object?>)values[node]!;
                    var length = GetArrayLength(nodeId);
                    for (nuint index = 0; index < length; index++)
                    {
                        array[CheckedIndex(index, array.Count, "array child")] =
                            values[NodeIndex(GetArrayChild(nodeId, index))];
                    }
                }
                else if (kinds[node] == NodeObject)
                {
                    var dictionary = (Dictionary<string, object?>)values[node]!;
                    var length = GetObjectLength(nodeId);
                    for (nuint index = 0; index < length; index++)
                    {
                        dictionary[GetObjectKey(nodeId, index)] =
                            values[NodeIndex(GetObjectChild(nodeId, index))];
                    }
                }
            }

            return values[NodeIndex(root)];
        }

        private object? DecodeLeaf(nuint node)
        {
            var leafKind = GetLeafKind(node);
            var fields = GetLeafFields(node);
            return leafKind switch
            {
                LeafUnserializable => RequireFields(fields, 1, "unserializable",
                    () => DecodeUnserializable(fields[0])),
                LeafBigInt => RequireFields(fields, 1, "bigint",
                    () => ParseBigIntegerOrString(fields[0])),
                LeafDate => RequireFields(fields, 1, "date",
                    () => DecodeDate(fields[0])),
                LeafRegexp => RequireFields(fields, 2, "regexp",
                    () => new RustwrightRegularExpression(fields[0], fields[1])),
                LeafUrl => RequireFields(fields, 1, "url",
                    () => DecodeUrl(fields[0])),
                LeafError => RequireFields(fields, 3, "error",
                    () => new RustwrightJavaScriptError(fields[0], fields[1], fields[2])),
                LeafUndefined or LeafSymbol or LeafFunction => fields.Count == 0
                    ? null
                    : throw new RustwrightException("Native wire graph leaf has unexpected fields."),
                _ => throw new RustwrightException($"Native wire graph returned unknown leaf kind {leafKind}."),
            };
        }

        private int GetNodeCount()
        {
            var status = NativeMethods.WireGraphNodeCount(graph, out var count);
            NativeError.ThrowIfFailed(status);
            return CheckedLength(count, "node count");
        }

        private int NodeIndex(nuint node) => CheckedIndex(node, values.Length, "node id");

        private nuint GetRoot()
        {
            var status = NativeMethods.WireGraphRoot(graph, out var root);
            NativeError.ThrowIfFailed(status);
            return root;
        }

        private int GetNodeKind(nuint node)
        {
            var status = NativeMethods.WireGraphNodeKind(graph, node, out var kind);
            NativeError.ThrowIfFailed(status);
            return kind;
        }

        private bool GetBool(nuint node)
        {
            var status = NativeMethods.WireGraphGetBool(graph, node, out var value);
            NativeError.ThrowIfFailed(status);
            return value != 0;
        }

        private long GetSigned(nuint node)
        {
            var status = NativeMethods.WireGraphGetSigned(graph, node, out var value);
            NativeError.ThrowIfFailed(status);
            return value;
        }

        private ulong GetUnsigned(nuint node)
        {
            var status = NativeMethods.WireGraphGetUnsigned(graph, node, out var value);
            NativeError.ThrowIfFailed(status);
            return value;
        }

        private double GetFloat(nuint node)
        {
            var status = NativeMethods.WireGraphGetFloat(graph, node, out var value);
            NativeError.ThrowIfFailed(status);
            return value;
        }

        private string GetString(nuint node)
        {
            var status = NativeMethods.WireGraphGetString(graph, node, out var data, out var length);
            NativeError.ThrowIfFailed(status);
            return CopyUtf8(data, length);
        }

        private nuint GetArrayLength(nuint node)
        {
            var status = NativeMethods.WireGraphArrayLength(graph, node, out var length);
            NativeError.ThrowIfFailed(status);
            return length;
        }

        private nuint GetArrayChild(nuint node, nuint index)
        {
            var status = NativeMethods.WireGraphArrayChild(graph, node, index, out var child);
            NativeError.ThrowIfFailed(status);
            return child;
        }

        private nuint GetObjectLength(nuint node)
        {
            var status = NativeMethods.WireGraphObjectLength(graph, node, out var length);
            NativeError.ThrowIfFailed(status);
            return length;
        }

        private string GetObjectKey(nuint node, nuint index)
        {
            var status = NativeMethods.WireGraphObjectKey(
                graph, node, index, out var data, out var length);
            NativeError.ThrowIfFailed(status);
            return CopyUtf8(data, length);
        }

        private nuint GetObjectChild(nuint node, nuint index)
        {
            var status = NativeMethods.WireGraphObjectChild(graph, node, index, out var child);
            NativeError.ThrowIfFailed(status);
            return child;
        }

        private int GetLeafKind(nuint node)
        {
            var status = NativeMethods.WireGraphLeafKind(graph, node, out var kind);
            NativeError.ThrowIfFailed(status);
            return kind;
        }

        private List<string> GetLeafFields(nuint node)
        {
            var status = NativeMethods.WireGraphLeafFieldCount(graph, node, out var count);
            NativeError.ThrowIfFailed(status);
            var fields = new List<string>(CheckedLength(count, "leaf field count"));
            for (nuint index = 0; index < count; index++)
            {
                status = NativeMethods.WireGraphLeafField(
                    graph, node, index, out var data, out var length);
                NativeError.ThrowIfFailed(status);
                fields.Add(CopyUtf8(data, length));
            }

            return fields;
        }
    }

    private static List<object?> EmptyArray(nuint length)
    {
        var count = CheckedLength(length, "array length");
        var array = new List<object?>(count);
        for (var index = 0; index < count; index++)
        {
            array.Add(null);
        }

        return array;
    }

    private static object DecodeUnsigned(ulong value) =>
        value <= long.MaxValue ? (object)(long)value : new BigInteger(value);

    private static object DecodeUnserializable(string value) => value switch
    {
        "NaN" => double.NaN,
        "Infinity" => double.PositiveInfinity,
        "-Infinity" => double.NegativeInfinity,
        "-0" => BitConverter.Int64BitsToDouble(long.MinValue),
        _ when value.EndsWith('n') => ParseBigIntegerOrString(value[..^1], value),
        _ => value,
    };

    private static object ParseBigIntegerOrString(string digits, string? fallback = null)
    {
        return BigInteger.TryParse(
            digits,
            NumberStyles.Integer,
            CultureInfo.InvariantCulture,
            out var integer)
            ? integer
            : fallback ?? digits;
    }

    private static object DecodeDate(string date) =>
        DateTimeOffset.TryParse(
            date,
            CultureInfo.InvariantCulture,
            DateTimeStyles.RoundtripKind,
            out var parsedDate)
            ? parsedDate
            : date;

    private static object DecodeUrl(string url) =>
        Uri.TryCreate(url, UriKind.RelativeOrAbsolute, out var parsedUrl) ? parsedUrl : url;

    private static T RequireFields<T>(
        IReadOnlyList<string> fields,
        int expected,
        string leaf,
        Func<T> decoder)
    {
        if (fields.Count != expected)
        {
            throw new RustwrightException(
                $"Native wire graph {leaf} leaf has {fields.Count} fields; expected {expected}.");
        }

        return decoder();
    }

    private static string CopyUtf8(IntPtr data, nuint length)
    {
        var count = CheckedLength(length, "UTF-8 byte length");
        if (count == 0)
        {
            return string.Empty;
        }

        if (data == IntPtr.Zero)
        {
            throw new RustwrightException($"Native returned NULL UTF-8 data with nonzero length {count}.");
        }

        var bytes = new byte[count];
        Marshal.Copy(data, bytes, 0, count);
        return Encoding.UTF8.GetString(bytes);
    }

    private static int CheckedLength(nuint value, string what)
    {
        if (value > (nuint)int.MaxValue)
        {
            throw new RustwrightException($"Native wire graph returned invalid {what}: {value}.");
        }

        return (int)value;
    }

    private static int CheckedIndex(nuint value, int size, string what)
    {
        if (value >= (nuint)size)
        {
            throw new RustwrightException($"Native wire graph returned invalid {what}: {value}.");
        }

        return (int)value;
    }
}
