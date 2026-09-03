<?php

declare(strict_types=1);

namespace Rustwright;

final class WireDecoder
{
    private const NODE_NULL = 0;
    private const NODE_BOOL = 1;
    private const NODE_SIGNED = 2;
    private const NODE_UNSIGNED = 3;
    private const NODE_FLOAT = 4;
    private const NODE_STRING = 5;
    private const NODE_ARRAY = 6;
    private const NODE_OBJECT = 7;
    private const NODE_LEAF = 8;

    private const LEAF_UNSERIALIZABLE = 0;
    private const LEAF_BIGINT = 1;
    private const LEAF_DATE = 2;
    private const LEAF_REGEXP = 3;
    private const LEAF_URL = 4;
    private const LEAF_ERROR = 5;
    private const LEAF_UNDEFINED = 6;
    private const LEAF_SYMBOL = 7;
    private const LEAF_FUNCTION = 8;

    public static function decode(string $json, NativeLibrary $native): mixed
    {
        $graph = $native->wireGraphParse($json);
        try {
            return self::materialize($graph, $native);
        } finally {
            $native->wireGraphFree($graph);
        }
    }

    private static function materialize(\FFI\CData $graph, NativeLibrary $native): mixed
    {
        $count = $native->wireGraphNodeCount($graph);
        $values = array_fill(0, $count, null);
        $kinds = array_fill(0, $count, null);

        for ($node = 0; $node < $count; $node++) {
            $kind = $native->wireGraphNodeKind($graph, $node);
            $kinds[$node] = $kind;
            $values[$node] = self::allocateNode($graph, $native, $node, $kind);
        }

        for ($node = 0; $node < $count; $node++) {
            if ($kinds[$node] === self::NODE_ARRAY) {
                $length = $native->wireGraphArrayLength($graph, $node);
                for ($index = 0; $index < $length; $index++) {
                    $child = $native->wireGraphArrayChild($graph, $node, $index);
                    $values[$node][] =& $values[$child];
                }
                continue;
            }

            if ($kinds[$node] === self::NODE_OBJECT) {
                $length = $native->wireGraphObjectLength($graph, $node);
                for ($index = 0; $index < $length; $index++) {
                    $key = $native->wireGraphObjectKey($graph, $node, $index);
                    $child = $native->wireGraphObjectChild($graph, $node, $index);
                    $values[$node]->{$key} =& $values[$child];
                }
            }
        }

        return $values[$native->wireGraphRoot($graph)];
    }

    private static function allocateNode(
        \FFI\CData $graph,
        NativeLibrary $native,
        int $node,
        int $kind,
    ): mixed {
        return match ($kind) {
            self::NODE_NULL => null,
            self::NODE_BOOL => $native->wireGraphGetBool($graph, $node),
            self::NODE_SIGNED => $native->wireGraphGetSigned($graph, $node),
            self::NODE_UNSIGNED => $native->wireGraphGetUnsigned($graph, $node),
            self::NODE_FLOAT => $native->wireGraphGetFloat($graph, $node),
            self::NODE_STRING => $native->wireGraphGetString($graph, $node),
            self::NODE_ARRAY => [],
            self::NODE_OBJECT => new \stdClass(),
            self::NODE_LEAF => self::decodeLeaf($graph, $native, $node),
            default => throw new RustwrightException('Unknown wire node kind: ' . $kind),
        };
    }

    private static function decodeLeaf(\FFI\CData $graph, NativeLibrary $native, int $node): mixed
    {
        $kind = $native->wireGraphLeafKind($graph, $node);
        $fields = [];
        $count = $native->wireGraphLeafFieldCount($graph, $node);
        for ($index = 0; $index < $count; $index++) {
            $fields[] = $native->wireGraphLeafField($graph, $node, $index);
        }

        return match ($kind) {
            self::LEAF_UNSERIALIZABLE => self::decodeUnserializable($fields[0] ?? ''),
            self::LEAF_BIGINT,
            self::LEAF_UNDEFINED,
            self::LEAF_SYMBOL,
            self::LEAF_FUNCTION => null,
            self::LEAF_DATE => self::decodeDate($fields[0] ?? ''),
            self::LEAF_REGEXP => (object) [
                'pattern' => $fields[0] ?? '',
                'flags' => $fields[1] ?? '',
            ],
            self::LEAF_URL => $fields[0] ?? '',
            self::LEAF_ERROR => (object) [
                'name' => $fields[0] ?? '',
                'message' => $fields[1] ?? '',
                'stack' => $fields[2] ?? '',
            ],
            default => throw new RustwrightException('Unknown wire leaf kind: ' . $kind),
        };
    }

    private static function decodeUnserializable(string $value): ?float
    {
        return match ($value) {
            'NaN' => NAN,
            'Infinity' => INF,
            '-Infinity' => -INF,
            '-0' => -0.0,
            default => null,
        };
    }

    private static function decodeDate(string $value): \DateTimeImmutable|string
    {
        try {
            return new \DateTimeImmutable($value);
        } catch (\Exception) {
            return $value;
        }
    }
}
