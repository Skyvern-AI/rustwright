<?php

declare(strict_types=1);

namespace Rustwright;

final class Page
{
    private bool $closed = false;
    private bool $freed = false;

    public function __construct(
        private readonly NativeLibrary $native,
        private mixed $handle,
    ) {
    }

    /** @param array<string, mixed> $options */
    public function goto(string $url, array $options = []): mixed
    {
        $this->ensureOpen();
        self::assertOptionKeys($options, ['waitUntil', 'wait_until', 'timeout', 'referer'], 'goto');
        $waitUntil = self::aliasedString($options, 'waitUntil', 'wait_until');
        $referer = self::optionalString($options, 'referer');
        return $this->native->pageGoto($this->handle, $url, $waitUntil, self::timeout($options), $referer);
    }

    /** @param array<string, mixed> $options */
    public function click(string $selector, array $options = []): void
    {
        $this->ensureOpen();
        self::assertOptionKeys($options, ['timeout'], 'click');
        $this->native->pageClick($this->handle, $selector, self::timeout($options));
    }

    /** @param array<string, mixed> $options */
    public function fill(string $selector, string $value, array $options = []): void
    {
        $this->ensureOpen();
        self::assertOptionKeys($options, ['timeout'], 'fill');
        $this->native->pageFill($this->handle, $selector, $value, self::timeout($options));
    }

    /** @param array<string, mixed> $options */
    public function title(array $options = []): string
    {
        $this->ensureOpen();
        self::assertOptionKeys($options, ['timeout'], 'title');
        return $this->native->pageTitle($this->handle, self::timeout($options));
    }

    /** @param array<string, mixed> $options */
    public function textContent(string $selector, array $options = []): ?string
    {
        $this->ensureOpen();
        self::assertOptionKeys($options, ['timeout'], 'textContent');
        return $this->native->pageTextContent($this->handle, $selector, self::timeout($options));
    }

    /**
     * The second argument is omitted when this method is called with only the expression.
     * Passing null explicitly sends JSON null to JavaScript.
     *
     * @param array<string, mixed> $options
     */
    public function evaluate(string $expression, mixed $argument = null, array $options = []): mixed
    {
        $argumentWasProvided = func_num_args() >= 2;
        $this->ensureOpen();
        self::assertOptionKeys($options, ['timeout'], 'evaluate');
        $argumentJson = $argumentWasProvided ? Json::encodeValue($argument) : null;
        return $this->native->pageEvaluate($this->handle, $expression, $argumentJson, self::timeout($options));
    }

    /** @param array<string, mixed> $options */
    public function screenshot(array $options = []): string
    {
        $this->ensureOpen();
        $wireOptions = OptionNormalizer::screenshot($options);
        $optionsJson = $wireOptions === [] ? null : Json::encodeObject($wireOptions);
        return $this->native->pageScreenshot($this->handle, $optionsJson);
    }

    /** Exposed for ABI diagnostics in addition to the required alpha surface. */
    public function targetId(): string
    {
        $this->ensureOpen();
        return $this->native->pageTargetId($this->handle);
    }

    /** @param array<string, mixed> $options */
    public function close(array $options = []): void
    {
        if ($this->freed) {
            return;
        }

        self::assertOptionKeys($options, ['timeout', 'runBeforeUnload', 'run_before_unload'], 'close');
        $runBeforeUnload = self::aliasedBool($options, 'runBeforeUnload', 'run_before_unload') ?? false;

        try {
            if (!$this->closed) {
                $this->native->pageClose($this->handle, self::timeout($options), $runBeforeUnload);
                $this->closed = true;
            }
        } finally {
            $this->native->pageFree($this->handle);
            $this->handle = null;
            $this->freed = true;
        }
    }

    public function __destruct()
    {
        try {
            $this->close();
        } catch (\Throwable) {
            // Destructors cannot safely surface lifecycle errors.
        }
    }

    private function ensureOpen(): void
    {
        if ($this->closed || $this->freed) {
            throw new RustwrightException('Page is closed');
        }
    }

    /** @param array<string, mixed> $options */
    private static function timeout(array $options): ?float
    {
        if (!array_key_exists('timeout', $options) || $options['timeout'] === null) {
            return null;
        }
        if (!is_int($options['timeout']) && !is_float($options['timeout'])) {
            throw new \InvalidArgumentException('timeout must be a number or null');
        }
        return (float) $options['timeout'];
    }

    /** @param array<string, mixed> $options */
    private static function optionalString(array $options, string $key): ?string
    {
        if (!array_key_exists($key, $options) || $options[$key] === null) {
            return null;
        }
        if (!is_string($options[$key])) {
            throw new \InvalidArgumentException($key . ' must be a string or null');
        }
        return $options[$key];
    }

    /** @param array<string, mixed> $options */
    private static function aliasedString(array $options, string $camel, string $snake): ?string
    {
        self::rejectAliasCollision($options, $camel, $snake);
        return self::optionalString($options, array_key_exists($camel, $options) ? $camel : $snake);
    }

    /** @param array<string, mixed> $options */
    private static function aliasedBool(array $options, string $camel, string $snake): ?bool
    {
        self::rejectAliasCollision($options, $camel, $snake);
        $key = array_key_exists($camel, $options) ? $camel : $snake;
        if (!array_key_exists($key, $options)) {
            return null;
        }
        if (!is_bool($options[$key])) {
            throw new \InvalidArgumentException($key . ' must be a boolean');
        }
        return $options[$key];
    }

    /** @param array<string, mixed> $options */
    private static function rejectAliasCollision(array $options, string $camel, string $snake): void
    {
        if (array_key_exists($camel, $options) && array_key_exists($snake, $options)) {
            throw new \InvalidArgumentException(sprintf('Options %s and %s are aliases; provide only one', $camel, $snake));
        }
    }

    /** @param array<string, mixed> $options
     *  @param list<string> $allowed
     */
    private static function assertOptionKeys(array $options, array $allowed, string $operation): void
    {
        foreach (array_keys($options) as $key) {
            if (!is_string($key) || !in_array($key, $allowed, true)) {
                throw new \InvalidArgumentException(sprintf('Unknown %s option: %s', $operation, (string) $key));
            }
        }
    }
}
