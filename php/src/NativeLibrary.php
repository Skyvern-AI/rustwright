<?php

declare(strict_types=1);

namespace Rustwright;

final class NativeLibrary
{
    private const C_DECLARATIONS = <<<'CDEF'
typedef signed int int32_t;
typedef signed long long int64_t;
typedef unsigned long long uint64_t;
typedef unsigned char uint8_t;

typedef struct RwBrowser RwBrowser;
typedef struct RwPage RwPage;
typedef struct RwWireGraph RwWireGraph;
typedef size_t RwWireNodeId;
typedef int32_t RwWireNodeKind;
typedef int32_t RwWireLeafKind;

const char *rw_last_error(void);
void rw_string_free(char *s);
void rw_bytes_free(uint8_t *buf, size_t len);
int32_t rw_wire_graph_parse(const char *wire_json, RwWireGraph **out_graph);
void rw_wire_graph_free(RwWireGraph *graph);
int32_t rw_wire_graph_node_count(const RwWireGraph *graph, size_t *out_count);
int32_t rw_wire_graph_root(const RwWireGraph *graph, RwWireNodeId *out_root);
int32_t rw_wire_graph_node_kind(const RwWireGraph *graph, RwWireNodeId node, RwWireNodeKind *out_kind);
int32_t rw_wire_graph_get_bool(const RwWireGraph *graph, RwWireNodeId node, int32_t *out_value);
int32_t rw_wire_graph_get_signed(const RwWireGraph *graph, RwWireNodeId node, int64_t *out_value);
int32_t rw_wire_graph_get_unsigned(const RwWireGraph *graph, RwWireNodeId node, uint64_t *out_value);
int32_t rw_wire_graph_get_float(const RwWireGraph *graph, RwWireNodeId node, double *out_value);
int32_t rw_wire_graph_get_string(const RwWireGraph *graph, RwWireNodeId node, const uint8_t **out_ptr, size_t *out_len);
int32_t rw_wire_graph_array_length(const RwWireGraph *graph, RwWireNodeId node, size_t *out_len);
int32_t rw_wire_graph_array_child(const RwWireGraph *graph, RwWireNodeId node, size_t index, RwWireNodeId *out_child);
int32_t rw_wire_graph_object_length(const RwWireGraph *graph, RwWireNodeId node, size_t *out_len);
int32_t rw_wire_graph_object_key(const RwWireGraph *graph, RwWireNodeId node, size_t index, const uint8_t **out_ptr, size_t *out_len);
int32_t rw_wire_graph_object_child(const RwWireGraph *graph, RwWireNodeId node, size_t index, RwWireNodeId *out_child);
int32_t rw_wire_graph_leaf_kind(const RwWireGraph *graph, RwWireNodeId node, RwWireLeafKind *out_kind);
int32_t rw_wire_graph_leaf_field_count(const RwWireGraph *graph, RwWireNodeId node, size_t *out_count);
int32_t rw_wire_graph_leaf_field(const RwWireGraph *graph, RwWireNodeId node, size_t index, const uint8_t **out_ptr, size_t *out_len);
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
    ): string {
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
        return $json;
    }
    public function wireGraphParse(string $wireJson): \FFI\CData
    {
        $out = $this->ffi->new('RwWireGraph *');
        $status = $this->ffi->rw_wire_graph_parse(self::cAbiString($wireJson), \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_parse');
        if ($this->isNull($out)) {
            throw new RustwrightException('rw_wire_graph_parse succeeded without returning a graph');
        }
        return $out;
    }

    public function wireGraphFree(\FFI\CData $graph): void
    {
        $this->ffi->rw_wire_graph_free($graph);
    }

    public function wireGraphNodeCount(\FFI\CData $graph): int
    {
        $out = $this->ffi->new('size_t');
        $status = $this->ffi->rw_wire_graph_node_count($graph, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_node_count');
        return (int) $out->cdata;
    }

    public function wireGraphRoot(\FFI\CData $graph): int
    {
        $out = $this->ffi->new('RwWireNodeId');
        $status = $this->ffi->rw_wire_graph_root($graph, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_root');
        return (int) $out->cdata;
    }

    public function wireGraphNodeKind(\FFI\CData $graph, int $node): int
    {
        $out = $this->ffi->new('RwWireNodeKind');
        $status = $this->ffi->rw_wire_graph_node_kind($graph, $node, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_node_kind');
        return (int) $out->cdata;
    }

    public function wireGraphGetBool(\FFI\CData $graph, int $node): bool
    {
        $out = $this->ffi->new('int32_t');
        $status = $this->ffi->rw_wire_graph_get_bool($graph, $node, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_get_bool');
        return (int) $out->cdata !== 0;
    }

    public function wireGraphGetSigned(\FFI\CData $graph, int $node): int
    {
        $out = $this->ffi->new('int64_t');
        $status = $this->ffi->rw_wire_graph_get_signed($graph, $node, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_get_signed');
        return (int) $out->cdata;
    }

    public function wireGraphGetUnsigned(\FFI\CData $graph, int $node): int|float
    {
        $out = $this->ffi->new('uint64_t');
        $status = $this->ffi->rw_wire_graph_get_unsigned($graph, $node, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_get_unsigned');
        return is_int($out->cdata) ? $out->cdata : (float) $out->cdata;
    }

    public function wireGraphGetFloat(\FFI\CData $graph, int $node): float
    {
        $out = $this->ffi->new('double');
        $status = $this->ffi->rw_wire_graph_get_float($graph, $node, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_get_float');
        return (float) $out->cdata;
    }

    public function wireGraphGetString(\FFI\CData $graph, int $node): string
    {
        $pointer = $this->ffi->new('uint8_t *');
        $length = $this->ffi->new('size_t');
        $status = $this->ffi->rw_wire_graph_get_string(
            $graph,
            $node,
            \FFI::addr($pointer),
            \FFI::addr($length),
        );
        $this->checkStatus($status, 'rw_wire_graph_get_string');
        return $this->copyBorrowedBytes($pointer, (int) $length->cdata);
    }

    public function wireGraphArrayLength(\FFI\CData $graph, int $node): int
    {
        $out = $this->ffi->new('size_t');
        $status = $this->ffi->rw_wire_graph_array_length($graph, $node, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_array_length');
        return (int) $out->cdata;
    }

    public function wireGraphArrayChild(\FFI\CData $graph, int $node, int $index): int
    {
        $out = $this->ffi->new('RwWireNodeId');
        $status = $this->ffi->rw_wire_graph_array_child($graph, $node, $index, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_array_child');
        return (int) $out->cdata;
    }

    public function wireGraphObjectLength(\FFI\CData $graph, int $node): int
    {
        $out = $this->ffi->new('size_t');
        $status = $this->ffi->rw_wire_graph_object_length($graph, $node, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_object_length');
        return (int) $out->cdata;
    }

    public function wireGraphObjectKey(\FFI\CData $graph, int $node, int $index): string
    {
        $pointer = $this->ffi->new('uint8_t *');
        $length = $this->ffi->new('size_t');
        $status = $this->ffi->rw_wire_graph_object_key(
            $graph,
            $node,
            $index,
            \FFI::addr($pointer),
            \FFI::addr($length),
        );
        $this->checkStatus($status, 'rw_wire_graph_object_key');
        return $this->copyBorrowedBytes($pointer, (int) $length->cdata);
    }

    public function wireGraphObjectChild(\FFI\CData $graph, int $node, int $index): int
    {
        $out = $this->ffi->new('RwWireNodeId');
        $status = $this->ffi->rw_wire_graph_object_child($graph, $node, $index, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_object_child');
        return (int) $out->cdata;
    }

    public function wireGraphLeafKind(\FFI\CData $graph, int $node): int
    {
        $out = $this->ffi->new('RwWireLeafKind');
        $status = $this->ffi->rw_wire_graph_leaf_kind($graph, $node, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_leaf_kind');
        return (int) $out->cdata;
    }

    public function wireGraphLeafFieldCount(\FFI\CData $graph, int $node): int
    {
        $out = $this->ffi->new('size_t');
        $status = $this->ffi->rw_wire_graph_leaf_field_count($graph, $node, \FFI::addr($out));
        $this->checkStatus($status, 'rw_wire_graph_leaf_field_count');
        return (int) $out->cdata;
    }

    public function wireGraphLeafField(\FFI\CData $graph, int $node, int $index): string
    {
        $pointer = $this->ffi->new('uint8_t *');
        $length = $this->ffi->new('size_t');
        $status = $this->ffi->rw_wire_graph_leaf_field(
            $graph,
            $node,
            $index,
            \FFI::addr($pointer),
            \FFI::addr($length),
        );
        $this->checkStatus($status, 'rw_wire_graph_leaf_field');
        return $this->copyBorrowedBytes($pointer, (int) $length->cdata);
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
    private function copyBorrowedBytes(mixed $pointer, int $length): string
    {
        if ($length === 0) {
            return '';
        }
        if ($this->isNull($pointer)) {
            throw new RustwrightException('Rustwright returned NULL bytes with a nonzero length');
        }
        return \FFI::string($pointer, $length);
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
