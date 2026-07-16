#!/usr/bin/env php
<?php

declare(strict_types=1);

require_once __DIR__ . '/bootstrap.php';

use Rustwright\Chromium;
use Rustwright\DataUrl;

$html = <<<'HTML'
<!doctype html>
<html>
  <head><title>Rustwright PHP Smoke</title></head>
  <body>
    <h1 id="message">ready</h1>
    <input id="name" />
    <button id="go" onclick="document.querySelector('#message').textContent = document.querySelector('#name').value">Go</button>
  </body>
</html>
HTML;

$screenshotPath = sys_get_temp_dir() . '/rustwright-php-smoke-' . getmypid() . '.png';
$browser = null;
$page = null;

try {
    $browser = Chromium::launch(['headless' => true]);
    $page = $browser->newPage();
    $page->goto(DataUrl::fromHtml($html));
    $title = $page->title();
    $before = $page->textContent('#message');
    $page->fill('#name', 'Rustwright for PHP');
    $page->click('#go');
    $after = $page->textContent('#message');
    $value = $page->evaluate("document.querySelector('#name').value");
    $screenshot = $page->screenshot(['path' => $screenshotPath]);
    $page->close();
    $page = null;

    echo json_encode([
        'title' => $title,
        'before' => $before,
        'after' => $after,
        'value' => $value,
        'screenshotBytes' => strlen($screenshot),
    ], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR), PHP_EOL;
} finally {
    if ($page !== null) {
        $page->close();
    }
    if ($browser !== null) {
        $browser->close();
    }
    if (is_file($screenshotPath)) {
        @unlink($screenshotPath);
    }
}
