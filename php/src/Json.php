<?php

declare(strict_types=1);

namespace Rustwright;

final class Json
{
    private const ENCODE_FLAGS = JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR;

    public static function encodeValue(mixed $value): string
    {
        try {
            return json_encode($value, self::ENCODE_FLAGS);
        } catch (\JsonException $error) {
            throw new RustwrightException('Could not encode JSON: ' . $error->getMessage(), 0, $error);
        }
    }

    /** @param array<string, mixed> $value */
    public static function encodeObject(array $value): string
    {
        return self::encodeValue((object) $value);
    }

    public static function decode(string $json): mixed
    {
        try {
            return json_decode($json, false, 512, JSON_THROW_ON_ERROR);
        } catch (\JsonException $error) {
            throw new RustwrightException('Rustwright returned invalid JSON: ' . $error->getMessage(), 0, $error);
        }
    }
}
