<?php

declare(strict_types=1);

namespace Rustwright;

final class Chromium
{
    /** @param array<string, mixed> $options */
    public static function launch(array $options = [], ?string $libraryPath = null): Browser
    {
        $native = new NativeLibrary($libraryPath ?? self::defaultLibraryPath());
        $wireOptions = OptionNormalizer::launch($options);
        $handle = $native->chromiumLaunch(Json::encodeObject($wireOptions));
        return new Browser($native, $handle);
    }

    public static function executablePath(?string $libraryPath = null): ?string
    {
        $native = new NativeLibrary($libraryPath ?? self::defaultLibraryPath());
        return $native->chromiumExecutablePath();
    }

    public static function defaultLibraryPath(): string
    {
        $override = getenv('RUSTWRIGHT_CAPI_LIB');
        if (is_string($override) && $override !== '') {
            return $override;
        }

        $extension = PHP_OS_FAMILY === 'Darwin' ? 'dylib' : 'so';
        return 'target/release/librustwright_capi.' . $extension;
    }
}
