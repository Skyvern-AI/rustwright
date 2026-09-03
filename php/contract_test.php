#!/usr/bin/env php
<?php

declare(strict_types=1);

require_once __DIR__ . '/bootstrap.php';

use Rustwright\Chromium;
use Rustwright\NativeLibrary;
use Rustwright\WireDecoder;

$native = new NativeLibrary(Chromium::defaultLibraryPath());
$wire = <<<'JSON'
{
  "__rustwright_cdp_object__": 1,
  "entries": {
    "self": {"__rustwright_cdp_ref__": 1},
    "shared": {
      "__rustwright_cdp_object__": 2,
      "entries": {"value": "a\u0000b"}
    },
    "again": {"__rustwright_cdp_ref__": 2},
    "bigint": {"__rustwright_cdp_unserializable_value__": "123n"}
  }
}
JSON;

$decoded = WireDecoder::decode($wire, $native);
if (!is_object($decoded) || $decoded->self !== $decoded) {
    throw new RuntimeException('wire graph did not preserve its self-cycle');
}
if ($decoded->shared !== $decoded->again) {
    throw new RuntimeException('wire graph did not preserve repeated object identity');
}
if ($decoded->shared->value !== "a\0b") {
    throw new RuntimeException('wire graph did not preserve an embedded NUL');
}
if ($decoded->bigint !== null) {
    throw new RuntimeException('BigInt did not use the PHP null fallback');
}

echo "graph contract ok\n";
