<?php

declare(strict_types=1);

namespace Rustwright;

final class DataUrl
{
    public static function fromHtml(string $html): string
    {
        $encoded = '';
        $length = strlen($html);
        for ($index = 0; $index < $length; $index++) {
            $byte = ord($html[$index]);
            $unreserved = ($byte >= 0x41 && $byte <= 0x5A)
                || ($byte >= 0x61 && $byte <= 0x7A)
                || ($byte >= 0x30 && $byte <= 0x39)
                || $byte === 0x2D
                || $byte === 0x2E
                || $byte === 0x5F
                || $byte === 0x7E;
            $encoded .= $unreserved ? chr($byte) : sprintf('%%%02X', $byte);
        }

        return 'data:text/html;charset=utf-8,' . $encoded;
    }
}
