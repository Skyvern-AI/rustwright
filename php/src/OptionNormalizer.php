<?php

declare(strict_types=1);

namespace Rustwright;

final class OptionNormalizer
{
    /** @param array<string, mixed> $options
     *  @return array<string, mixed>
     */
    public static function launch(array $options): array
    {
        return self::normalize($options, [
            'headless' => 'headless',
            'executablePath' => 'executable_path',
            'executable_path' => 'executable_path',
            'channel' => 'channel',
            'args' => 'args',
            'ignoreAllDefaultArgs' => 'ignore_all_default_args',
            'ignore_all_default_args' => 'ignore_all_default_args',
            'ignoreDefaultArgs' => 'ignore_default_args',
            'ignore_default_args' => 'ignore_default_args',
            'timeout' => 'timeout',
            'userDataDir' => 'user_data_dir',
            'user_data_dir' => 'user_data_dir',
            'env' => 'env',
            'chromiumSandbox' => 'chromium_sandbox',
            'chromium_sandbox' => 'chromium_sandbox',
            'proxy' => 'proxy',
        ], 'launch');
    }

    /** @param array<string, mixed> $options
     *  @return array<string, mixed>
     */
    public static function screenshot(array $options): array
    {
        $normalized = self::normalize($options, [
            'path' => 'path',
            'fullPage' => 'fullPage',
            'full_page' => 'fullPage',
            'clip' => 'clip',
            'timeout' => 'timeout',
            'type' => 'type',
            'quality' => 'quality',
            'omitBackground' => 'omitBackground',
            'omit_background' => 'omitBackground',
        ], 'screenshot');

        if (isset($normalized['clip']) && is_array($normalized['clip'])) {
            $normalized['clip'] = (object) $normalized['clip'];
        }

        return $normalized;
    }

    /**
     * @param array<string, mixed> $options
     * @param array<string, string> $mapping
     * @return array<string, mixed>
     */
    private static function normalize(array $options, array $mapping, string $kind): array
    {
        $normalized = [];
        foreach ($options as $key => $value) {
            if (!is_string($key) || !array_key_exists($key, $mapping)) {
                throw new \InvalidArgumentException(sprintf('Unknown %s option: %s', $kind, (string) $key));
            }

            $wireKey = $mapping[$key];
            if (array_key_exists($wireKey, $normalized)) {
                throw new \InvalidArgumentException(sprintf('Duplicate %s option alias for %s', $kind, $wireKey));
            }

            if (($wireKey === 'env' || $wireKey === 'proxy') && is_array($value)) {
                $value = (object) $value;
            }
            $normalized[$wireKey] = $value;
        }

        return $normalized;
    }
}
