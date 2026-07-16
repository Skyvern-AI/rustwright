<?php

declare(strict_types=1);

namespace Rustwright;

final class NativeLibrary
{
    private const C_DECLARATIONS = <<<'CDEF'
typedef signed int int32_t;
typedef unsigned char uint8_t;

typedef struct RwBrowser RwBrowser;
typedef struct RwPage RwPage;

const char *rw_last_error(void);
void rw_string_free(char *s);
void rw_bytes_free(uint8_t *buf, size_t len);
int32_t rw_chromium_executable_path(char **out_path);
int32_t rw_chromium_launch(const char *options_json, RwBrowser **out_browser);
int32_t rw_browser_new_page(RwBrowser *b, RwPage **out_page);
int32_t rw_browser_close(RwBrowser *b);
char *rw_browser_ws_endpoint(RwBrowser *b);
void rw_browser_free(RwBrowser *b);
char *rw_page_target_id(RwPage *p);
int32_t rw_page_goto(RwPage *p, const char *url, const char *wait_until,
                     double timeout_ms_or_nan, const char *referer,
                     char **out_response_json);
int32_t rw_page_click(RwPage *p, const char *selector, double timeout_ms_or_nan);
int32_t rw_page_fill(RwPage *p, const char *selector, const char *value,
                     double timeout_ms_or_nan);
int32_t rw_page_title(RwPage *p, double timeout_ms_or_nan, char **out_title);
int32_t rw_page_text_content(RwPage *p, const char *selector,
                             double timeout_ms_or_nan, char **out_text);
int32_t rw_page_evaluate(RwPage *p, const char *expression, const char *arg_json,
                         double timeout_ms_or_nan, char **out_json);
int32_t rw_page_screenshot(RwPage *p, const char *options_json,
                           uint8_t **out_buf, size_t *out_len);
int32_t rw_page_close(RwPage *p, double timeout_ms_or_nan, int run_before_unload);
void rw_page_free(RwPage *p);
CDEF;

    private \FFI $ffi;

    public function __construct(private readonly string $libraryPath)
    {
        if (!extension_loaded('FFI')) {
            throw new RustwrightException('The PHP FFI extension is required (run PHP with -d ffi.enable=1).');
        }

        try {
            $this->ffi = \FFI::cdef(self::C_DECLARATIONS, $libraryPath);
        } catch (\Throwable $error) {
            throw new RustwrightException(
                sprintf('Could not load Rustwright C API library at %s: %s', $libraryPath, $error->getMessage()),
                0,
                $error,
            );
        }
    }

    public function path(): string
    {
        return $this->libraryPath;
    }

    public function chromiumExecutablePath(): ?string
    {
        $out = $this->ffi->new('char *');
        $status = $this->ffi->rw_chromium_executable_path(\FFI::addr($out));
        $this->checkStatus($status, 'rw_chromium_executable_path');
        return $this->copyNullableStringAndFree($out);
    }

    public function chromiumLaunch(string $optionsJson): \FFI\CData
    {
        $out = $this->ffi->new('RwBrowser *');
        $status = $this->ffi->rw_chromium_launch(self::cAbiString($optionsJson), \FFI::addr($out));
        $this->checkStatus($status, 'rw_chromium_launch');
        if ($this->isNull($out)) {
            throw new RustwrightException('rw_chromium_launch succeeded without returning a browser handle');
        }
        return $out;
    }

    public function browserNewPage(\FFI\CData $browser): \FFI\CData
    {
        $out = $this->ffi->new('RwPage *');
        $status = $this->ffi->rw_browser_new_page($browser, \FFI::addr($out));
        $this->checkStatus($status, 'rw_browser_new_page');
        if ($this->isNull($out)) {
            throw new RustwrightException('rw_browser_new_page succeeded without returning a page handle');
        }
        return $out;
    }

    public function browserClose(\FFI\CData $browser): void
    {
        $status = $this->ffi->rw_browser_close($browser);
        $this->checkStatus($status, 'rw_browser_close');
    }

    public function browserWsEndpoint(\FFI\CData $browser): string
    {
        $out = $this->ffi->rw_browser_ws_endpoint($browser);
        if ($this->isNull($out)) {
            throw $this->lastErrorException('rw_browser_ws_endpoint');
        }
        return $this->copyStringAndFree($out);
    }

    public function browserFree(\FFI\CData $browser): void
    {
        $this->ffi->rw_browser_free($browser);
    }

    public function pageTargetId(\FFI\CData $page): string
    {
        $out = $this->ffi->rw_page_target_id($page);
        if ($this->isNull($out)) {
            throw $this->lastErrorException('rw_page_target_id');
        }
        return $this->copyStringAndFree($out);
    }

    public function pageGoto(
        \FFI\CData $page,
        string $url,
        ?string $waitUntil,
        ?float $timeout,
        ?string $referer,
    ): mixed {
        $out = $this->ffi->new('char *');
        $status = $this->ffi->rw_page_goto(
            $page,
            self::cAbiString($url),
            self::cAbiString($waitUntil),
            self::timeout($timeout),
            self::cAbiString($referer),
            \FFI::addr($out),
        );
        $this->checkStatus($status, 'rw_page_goto');
        $json = $this->copyNullableStringAndFree($out);
        return $json === null ? null : Json::decode($json);
    }

    public function pageClick(\FFI\CData $page, string $selector, ?float $timeout): void
    {
        $status = $this->ffi->rw_page_click($page, self::cAbiString($selector), self::timeout($timeout));
        $this->checkStatus($status, 'rw_page_click');
    }

    public function pageFill(\FFI\CData $page, string $selector, string $value, ?float $timeout): void
    {
        $status = $this->ffi->rw_page_fill(
            $page,
            self::cAbiString($selector),
            self::cAbiString($value),
            self::timeout($timeout),
        );
        $this->checkStatus($status, 'rw_page_fill');
    }

    public function pageTitle(\FFI\CData $page, ?float $timeout): string
    {
        $out = $this->ffi->new('char *');
        $status = $this->ffi->rw_page_title($page, self::timeout($timeout), \FFI::addr($out));
        $this->checkStatus($status, 'rw_page_title');
        $title = $this->copyNullableStringAndFree($out);
        if ($title === null) {
            throw new RustwrightException('rw_page_title succeeded without returning a title');
        }
        return $title;
    }

    public function pageTextContent(\FFI\CData $page, string $selector, ?float $timeout): ?string
    {
        $out = $this->ffi->new('char *');
        $status = $this->ffi->rw_page_text_content(
            $page,
            self::cAbiString($selector),
            self::timeout($timeout),
            \FFI::addr($out),
        );
        $this->checkStatus($status, 'rw_page_text_content');
        return $this->copyNullableStringAndFree($out);
    }

    public function pageEvaluate(
        \FFI\CData $page,
        string $expression,
        ?string $argumentJson,
        ?float $timeout,
    ): mixed {
        $out = $this->ffi->new('char *');
        $status = $this->ffi->rw_page_evaluate(
            $page,
            self::cAbiString($expression),
            self::cAbiString($argumentJson),
            self::timeout($timeout),
            \FFI::addr($out),
        );
        $this->checkStatus($status, 'rw_page_evaluate');
        $json = $this->copyNullableStringAndFree($out);
        if ($json === null) {
            throw new RustwrightException('rw_page_evaluate succeeded without returning JSON');
        }
        return WireDecoder::decode($json);
    }

    public function pageScreenshot(\FFI\CData $page, ?string $optionsJson): string
    {
        $buffer = $this->ffi->new('uint8_t *');
        $length = $this->ffi->new('size_t');
        $status = $this->ffi->rw_page_screenshot(
            $page,
            self::cAbiString($optionsJson),
            \FFI::addr($buffer),
            \FFI::addr($length),
        );
        $this->checkStatus($status, 'rw_page_screenshot');

        $size = (int) $length->cdata;
        if ($this->isNull($buffer)) {
            if ($size !== 0) {
                throw new RustwrightException('rw_page_screenshot returned a null buffer with a nonzero length');
            }
            return '';
        }

        try {
            return $size === 0 ? '' : \FFI::string($buffer, $size);
        } finally {
            $this->ffi->rw_bytes_free($buffer, $size);
        }
    }

    public function pageClose(\FFI\CData $page, ?float $timeout, bool $runBeforeUnload): void
    {
        $status = $this->ffi->rw_page_close($page, self::timeout($timeout), $runBeforeUnload ? 1 : 0);
        $this->checkStatus($status, 'rw_page_close');
    }

    public function pageFree(\FFI\CData $page): void
    {
        $this->ffi->rw_page_free($page);
    }

    private function checkStatus(int $status, string $operation): void
    {
        if ($status !== 0) {
            throw $this->lastErrorException($operation);
        }
    }

    private function lastErrorException(string $operation): RustwrightException
    {
        // This must remain the first ABI call after a failed/null-returning call.
        $error = $this->ffi->rw_last_error();
        $message = $this->isNull($error) ? 'unknown native error' : \FFI::string($error);
        return new RustwrightException($operation . ': ' . $message);
    }

    private function copyNullableStringAndFree(mixed $value): ?string
    {
        return $this->isNull($value) ? null : $this->copyStringAndFree($value);
    }

    private function copyStringAndFree(\FFI\CData $value): string
    {
        try {
            return \FFI::string($value);
        } finally {
            $this->ffi->rw_string_free($value);
        }
    }

    private function isNull(mixed $value): bool
    {
        return $value === null || \FFI::isNull($value);
    }

    private static function timeout(?float $timeout): float
    {
        return $timeout ?? NAN;
    }

    private static function cAbiString(?string $value): ?string
    {
        if ($value !== null && str_contains($value, "\0")) {
            throw new RustwrightException('strings passed to the C ABI cannot contain NUL');
        }
        return $value;
    }
}
