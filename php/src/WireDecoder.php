<?php

declare(strict_types=1);

namespace Rustwright;

final class WireDecoder
{
    public static function decode(string $json): mixed
    {
        $value = Json::decode($json);
        $references = [];
        return self::decodeValue($value, $references);
    }

    /** @param array<string, mixed> $references */
    private static function decodeValue(mixed $value, array &$references): mixed
    {
        if (is_array($value)) {
            $decoded = [];
            foreach ($value as $item) {
                $decoded[] = self::decodeValue($item, $references);
            }
            return $decoded;
        }

        if (!$value instanceof \stdClass) {
            return $value;
        }

        $fields = get_object_vars($value);

        if (array_key_exists('__rustwright_cdp_ref__', $fields)) {
            $id = (string) $fields['__rustwright_cdp_ref__'];
            return $references[$id] ?? null;
        }

        if (array_key_exists('__rustwright_cdp_array__', $fields) && isset($fields['items']) && is_array($fields['items'])) {
            $id = (string) $fields['__rustwright_cdp_array__'];
            $decoded = [];
            $references[$id] =& $decoded;
            foreach ($fields['items'] as $item) {
                $decoded[] = self::decodeValue($item, $references);
            }
            return $decoded;
        }

        if (array_key_exists('__rustwright_cdp_object__', $fields) && isset($fields['entries']) && $fields['entries'] instanceof \stdClass) {
            $id = (string) $fields['__rustwright_cdp_object__'];
            $decoded = new \stdClass();
            $references[$id] = $decoded;
            foreach (get_object_vars($fields['entries']) as $key => $item) {
                $decoded->{$key} = self::decodeValue($item, $references);
            }
            return $decoded;
        }

        if (array_key_exists('__rustwright_cdp_undefined__', $fields)
            || array_key_exists('__rustwright_cdp_symbol__', $fields)
            || array_key_exists('__rustwright_cdp_function__', $fields)) {
            return null;
        }

        if (array_key_exists('__rustwright_cdp_unserializable_value__', $fields)) {
            return match ($fields['__rustwright_cdp_unserializable_value__']) {
                'NaN' => NAN,
                'Infinity' => INF,
                '-Infinity' => -INF,
                '-0' => -0.0,
                default => null,
            };
        }

        if (array_key_exists('__rustwright_cdp_date__', $fields)) {
            try {
                return new \DateTimeImmutable((string) $fields['__rustwright_cdp_date__']);
            } catch (\Exception) {
                return (string) $fields['__rustwright_cdp_date__'];
            }
        }

        if (array_key_exists('__rustwright_cdp_url__', $fields)) {
            return (string) $fields['__rustwright_cdp_url__'];
        }

        if (array_key_exists('__rustwright_cdp_regexp__', $fields)) {
            $regexp = $fields['__rustwright_cdp_regexp__'];
            if ($regexp instanceof \stdClass) {
                return (object) [
                    'pattern' => property_exists($regexp, 'p') ? $regexp->p : '',
                    'flags' => property_exists($regexp, 'f') ? $regexp->f : '',
                ];
            }
        }

        if (array_key_exists('__rustwright_cdp_error__', $fields)) {
            return self::decodeValue($fields['__rustwright_cdp_error__'], $references);
        }

        $decoded = new \stdClass();
        foreach ($fields as $key => $item) {
            $decoded->{$key} = self::decodeValue($item, $references);
        }
        return $decoded;
    }
}
