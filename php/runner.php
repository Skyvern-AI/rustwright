#!/usr/bin/env php
<?php

declare(strict_types=1);

require_once __DIR__ . '/bootstrap.php';

use Rustwright\BenchmarkRunner;
use Rustwright\Chromium;
use Rustwright\ManifestValidator;

/** @return array{manifest:string,lib:string,out:string,cases:?string} */
function parseArguments(array $argv): array
{
    $values = [];
    $arguments = array_slice($argv, 1);
    for ($index = 0; $index < count($arguments); $index++) {
        $flag = $arguments[$index];
        if ($flag === '--') {
            continue;
        }
        if (!in_array($flag, ['--manifest', '--lib', '--out', '--cases'], true)) {
            throw new InvalidArgumentException('Unknown argument: ' . $flag);
        }
        if (array_key_exists($flag, $values)) {
            throw new InvalidArgumentException('Duplicate argument: ' . $flag);
        }
        if (!array_key_exists($index + 1, $arguments) || str_starts_with($arguments[$index + 1], '--')) {
            throw new InvalidArgumentException('Missing value for ' . $flag);
        }
        $values[$flag] = $arguments[++$index];
    }

    foreach (['--manifest', '--lib', '--out'] as $required) {
        if (!isset($values[$required]) || $values[$required] === '') {
            throw new InvalidArgumentException('Missing required argument: ' . $required);
        }
    }

    return [
        'manifest' => $values['--manifest'],
        'lib' => $values['--lib'],
        'out' => $values['--out'],
        'cases' => $values['--cases'] ?? null,
    ];
}

/** @param list<stdClass> $cases
 *  @return list<stdClass>
 */
function selectCases(array $cases, ?string $selection): array
{
    if ($selection === null) {
        return $cases;
    }
    if ($selection === '') {
        throw new InvalidArgumentException('--cases must contain at least one case id');
    }

    $requested = explode(',', $selection);
    $requestedSet = [];
    foreach ($requested as $id) {
        if ($id === '') {
            throw new InvalidArgumentException('--cases contains an empty case id');
        }
        if (isset($requestedSet[$id])) {
            throw new InvalidArgumentException('Duplicate requested case id: ' . $id);
        }
        $requestedSet[$id] = true;
    }

    $selected = [];
    foreach ($cases as $case) {
        if (isset($requestedSet[$case->id])) {
            $selected[] = $case;
            unset($requestedSet[$case->id]);
        }
    }
    if ($requestedSet !== []) {
        throw new InvalidArgumentException('Unknown requested case id: ' . array_key_first($requestedSet));
    }
    return $selected;
}

try {
    $options = parseArguments($argv);
    $cases = selectCases(ManifestValidator::load($options['manifest']), $options['cases']);
    $results = BenchmarkRunner::run($cases, $options['lib']);
    $payload = ['lang' => 'php', 'results' => $results];
    $json = json_encode($payload, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR) . PHP_EOL;
    if (@file_put_contents($options['out'], $json) === false) {
        throw new RuntimeException('Could not write results file: ' . $options['out']);
    }
    echo $json;
    foreach ($results as $result) {
        if ($result['ok'] !== true) {
            exit(1);
        }
    }
    exit(0);
} catch (InvalidArgumentException $error) {
    // Invalid CLI usage or manifest: exit 2, matching the other language runners.
    fwrite(STDERR, 'runner: ' . $error->getMessage() . PHP_EOL);
    fwrite(STDERR, 'usage: php -d ffi.enable=1 php/runner.php --manifest <path> --lib <path> --out <path> [--cases id1,id2]' . PHP_EOL);
    exit(2);
} catch (Throwable $error) {
    // Runtime failure (browser launch, FFI, output write): exit 1.
    fwrite(STDERR, 'runner: ' . $error->getMessage() . PHP_EOL);
    exit(1);
}
