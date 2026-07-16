<?php

declare(strict_types=1);

namespace Rustwright;

final class ManifestValidator
{
    /** @return list<\stdClass> */
    public static function load(string $path): array
    {
        $json = @file_get_contents($path);
        if ($json === false) {
            throw new \InvalidArgumentException('Could not read manifest: ' . $path);
        }

        try {
            $manifest = json_decode($json, false, 512, JSON_THROW_ON_ERROR);
        } catch (\JsonException $error) {
            throw new \InvalidArgumentException('Manifest is not valid JSON: ' . $error->getMessage(), 0, $error);
        }

        if (!$manifest instanceof \stdClass) {
            throw new \InvalidArgumentException('Manifest must be a JSON object');
        }
        self::keys($manifest, ['version', 'cases'], ['version', 'cases'], 'manifest');
        if (!is_int($manifest->version) || $manifest->version !== 1) {
            throw new \InvalidArgumentException('Unsupported manifest version; expected 1');
        }
        if (!is_array($manifest->cases) || $manifest->cases === []) {
            throw new \InvalidArgumentException('manifest.cases must be a nonempty array');
        }

        $ids = [];
        $cases = [];
        foreach ($manifest->cases as $caseIndex => $case) {
            $context = sprintf('case %d', $caseIndex + 1);
            if (!$case instanceof \stdClass) {
                throw new \InvalidArgumentException($context . ' must be an object');
            }
            self::keys($case, ['id', 'description', 'html', 'url', 'steps'], ['id', 'steps'], $context);
            self::nonEmptyString($case->id, $context . '.id');
            if (isset($ids[$case->id])) {
                throw new \InvalidArgumentException('Duplicate case id: ' . $case->id);
            }
            $ids[$case->id] = true;
            self::optionalStringProperty($case, 'description', $context);
            self::optionalStringProperty($case, 'html', $context);
            self::optionalStringProperty($case, 'url', $context);
            if (!is_array($case->steps) || $case->steps === []) {
                throw new \InvalidArgumentException($context . '.steps must be a nonempty array');
            }

            $captures = [];
            foreach ($case->steps as $stepIndex => $step) {
                $stepContext = sprintf('%s step %d', $context, $stepIndex + 1);
                self::validateStep($step, $case, $captures, $stepContext);
            }
            $cases[] = $case;
        }

        return $cases;
    }

    /**
     * @param array<string, true> $captures
     */
    private static function validateStep(mixed $step, \stdClass $case, array &$captures, string $context): void
    {
        if (!$step instanceof \stdClass) {
            throw new \InvalidArgumentException($context . ' must be an object');
        }
        if (!property_exists($step, 'op') || !is_string($step->op)) {
            throw new \InvalidArgumentException($context . '.op must be a string');
        }

        switch ($step->op) {
            case 'goto':
                self::keys($step, ['op', 'url', 'useCaseHtml', 'waitUntil'], ['op'], $context);
                $hasUrl = property_exists($step, 'url');
                $hasHtml = property_exists($step, 'useCaseHtml');
                if ($hasUrl === $hasHtml) {
                    throw new \InvalidArgumentException($context . ' goto requires exactly one of url or useCaseHtml');
                }
                if ($hasUrl) {
                    self::nonEmptyString($step->url, $context . '.url');
                } else {
                    if ($step->useCaseHtml !== true) {
                        throw new \InvalidArgumentException($context . '.useCaseHtml must be true');
                    }
                    if (!property_exists($case, 'html') || !is_string($case->html)) {
                        throw new \InvalidArgumentException($context . ' uses case HTML, but case.html is missing');
                    }
                }
                if (property_exists($step, 'waitUntil')) {
                    if (!is_string($step->waitUntil)
                        || !in_array($step->waitUntil, ['load', 'domcontentloaded', 'networkidle', 'commit'], true)) {
                        throw new \InvalidArgumentException($context . '.waitUntil is invalid');
                    }
                }
                return;

            case 'click':
                self::keys($step, ['op', 'selector'], ['op', 'selector'], $context);
                self::nonEmptyString($step->selector, $context . '.selector');
                return;

            case 'fill':
                self::keys($step, ['op', 'selector', 'value'], ['op', 'selector', 'value'], $context);
                self::nonEmptyString($step->selector, $context . '.selector');
                if (!is_string($step->value)) {
                    throw new \InvalidArgumentException($context . '.value must be a string');
                }
                return;

            case 'title':
                self::keys($step, ['op', 'capture'], ['op', 'capture'], $context);
                self::capture($step->capture, $captures, $context);
                return;

            case 'textContent':
                self::keys($step, ['op', 'selector', 'capture'], ['op', 'selector', 'capture'], $context);
                self::nonEmptyString($step->selector, $context . '.selector');
                self::capture($step->capture, $captures, $context);
                return;

            case 'evaluate':
                self::keys($step, ['op', 'expression', 'arg', 'capture'], ['op', 'expression', 'capture'], $context);
                self::nonEmptyString($step->expression, $context . '.expression');
                self::capture($step->capture, $captures, $context);
                return;

            case 'screenshot':
                self::keys($step, ['op', 'capture'], ['op', 'capture'], $context);
                self::capture($step->capture, $captures, $context);
                return;

            case 'assertTitle':
                self::keys($step, ['op', 'equals', 'contains'], ['op'], $context);
                self::predicate($step, $context);
                return;

            case 'assertText':
                self::keys($step, ['op', 'selector', 'equals', 'contains'], ['op', 'selector'], $context);
                self::nonEmptyString($step->selector, $context . '.selector');
                self::predicate($step, $context);
                return;

            case 'assertEval':
                self::keys($step, ['op', 'expression', 'equals'], ['op', 'expression', 'equals'], $context);
                self::nonEmptyString($step->expression, $context . '.expression');
                return;

            default:
                throw new \InvalidArgumentException($context . ': unknown operation ' . $step->op);
        }
    }

    /** @param array<string, true> $captures */
    private static function capture(mixed $name, array &$captures, string $context): void
    {
        self::nonEmptyString($name, $context . '.capture');
        if (isset($captures[$name])) {
            throw new \InvalidArgumentException($context . ': duplicate capture name ' . $name);
        }
        $captures[$name] = true;
    }

    private static function predicate(\stdClass $step, string $context): void
    {
        $hasEquals = property_exists($step, 'equals');
        $hasContains = property_exists($step, 'contains');
        if ($hasEquals === $hasContains) {
            throw new \InvalidArgumentException($context . ' requires exactly one of equals or contains');
        }
        $value = $hasEquals ? $step->equals : $step->contains;
        if (!is_string($value)) {
            throw new \InvalidArgumentException($context . ' predicate must be a string');
        }
    }

    private static function nonEmptyString(mixed $value, string $context): void
    {
        if (!is_string($value) || $value === '') {
            throw new \InvalidArgumentException($context . ' must be a nonempty string');
        }
    }

    private static function optionalStringProperty(\stdClass $object, string $property, string $context): void
    {
        if (property_exists($object, $property) && !is_string($object->{$property})) {
            throw new \InvalidArgumentException($context . '.' . $property . ' must be a string');
        }
    }

    /**
     * @param list<string> $allowed
     * @param list<string> $required
     */
    private static function keys(\stdClass $object, array $allowed, array $required, string $context): void
    {
        foreach (array_keys(get_object_vars($object)) as $key) {
            if (!in_array($key, $allowed, true)) {
                throw new \InvalidArgumentException($context . ': unknown property ' . $key);
            }
        }
        foreach ($required as $key) {
            if (!property_exists($object, $key)) {
                throw new \InvalidArgumentException($context . ': missing required property ' . $key);
            }
        }
    }
}
