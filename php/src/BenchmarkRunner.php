<?php

declare(strict_types=1);

namespace Rustwright;

final class BenchmarkRunner
{
    /**
     * @param list<\stdClass> $cases
     * @return list<array<string, mixed>>
     */
    public static function run(array $cases, string $libraryPath): array
    {
        $browser = Chromium::launch(['headless' => true], $libraryPath);
        $results = [];
        try {
            foreach ($cases as $case) {
                $results[] = self::runCase($browser, $case);
            }
        } finally {
            $browser->close();
        }
        return $results;
    }

    /** @return array<string, mixed> */
    private static function runCase(Browser $browser, \stdClass $case): array
    {
        $started = hrtime(true);
        $captures = [];
        $page = null;
        $errorMessage = null;
        $stepNumber = null;

        try {
            $page = $browser->newPage();
            foreach ($case->steps as $index => $step) {
                $stepNumber = $index + 1;
                self::executeStep($page, $case, $step, $captures);
            }
            $stepNumber = null;
        } catch (\Throwable $error) {
            $prefix = $stepNumber === null ? 'page setup: ' : 'step ' . $stepNumber . ': ';
            $errorMessage = $prefix . $error->getMessage();
        } finally {
            if ($page !== null) {
                try {
                    $page->close();
                } catch (\Throwable $closeError) {
                    $errorMessage ??= 'page close: ' . $closeError->getMessage();
                }
            }
        }

        $result = [
            'id' => $case->id,
            'ok' => $errorMessage === null,
            'captures' => (object) $captures,
            'ms' => (hrtime(true) - $started) / 1_000_000,
        ];
        if ($errorMessage !== null) {
            $result['error'] = $errorMessage;
        }
        return $result;
    }

    /** @param array<string, mixed> $captures */
    private static function executeStep(Page $page, \stdClass $case, \stdClass $step, array &$captures): void
    {
        switch ($step->op) {
            case 'goto':
                $url = property_exists($step, 'useCaseHtml')
                    ? DataUrl::fromHtml($case->html)
                    : $step->url;
                $options = property_exists($step, 'waitUntil') ? ['waitUntil' => $step->waitUntil] : [];
                $page->goto($url, $options);
                return;

            case 'click':
                $page->click($step->selector);
                return;

            case 'fill':
                $page->fill($step->selector, $step->value);
                return;

            case 'title':
                $captures[$step->capture] = $page->title();
                return;

            case 'textContent':
                $captures[$step->capture] = $page->textContent($step->selector);
                return;

            case 'evaluate':
                $captures[$step->capture] = property_exists($step, 'arg')
                    ? $page->evaluate($step->expression, $step->arg)
                    : $page->evaluate($step->expression);
                return;

            case 'screenshot':
                $captures[$step->capture] = strlen($page->screenshot());
                return;

            case 'assertTitle':
                self::assertString($page->title(), $step, 'title');
                return;

            case 'assertText':
                $text = $page->textContent($step->selector);
                if ($text === null) {
                    throw new RustwrightException('textContent was null for selector ' . $step->selector);
                }
                self::assertString($text, $step, 'textContent');
                return;

            case 'assertEval':
                $actual = $page->evaluate($step->expression);
                if (!JsonEquality::equals($actual, $step->equals)) {
                    throw new RustwrightException(sprintf(
                        'evaluation assertion failed: expected %s, got %s',
                        self::display($step->equals),
                        self::display($actual),
                    ));
                }
                return;

            default:
                // ManifestValidator rejects this before Chromium launches.
                throw new \LogicException('Unknown operation: ' . $step->op);
        }
    }

    private static function assertString(string $actual, \stdClass $step, string $label): void
    {
        if (property_exists($step, 'equals')) {
            if ($actual !== $step->equals) {
                throw new RustwrightException(sprintf('%s assertion failed: expected %s, got %s', $label, self::display($step->equals), self::display($actual)));
            }
            return;
        }
        if (!str_contains($actual, $step->contains)) {
            throw new RustwrightException(sprintf('%s assertion failed: expected %s to contain %s', $label, self::display($actual), self::display($step->contains)));
        }
    }

    private static function display(mixed $value): string
    {
        try {
            return Json::encodeValue($value);
        } catch (\Throwable) {
            return get_debug_type($value);
        }
    }
}
