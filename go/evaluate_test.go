package rustwright

import (
	"math"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"testing"
	"time"
)

func loadWireDecodeLibrary(t *testing.T) {
	t.Helper()
	path := os.Getenv("RUSTWRIGHT_LIB")
	if path == "" {
		name := "librustwright_capi.so"
		if runtime.GOOS == "darwin" {
			name = "librustwright_capi.dylib"
		}
		path = filepath.Join("..", "target", "release", name)
	}
	if _, err := loadNative(path); err != nil {
		t.Fatalf("load wire decoder library %q: %v", path, err)
	}
}

func TestDecodeEvaluateJSONWrappersReferencesAndCycles(t *testing.T) {
	loadWireDecodeLibrary(t)
	decoded, err := decodeEvaluateJSON([]byte(`{
          "__rustwright_cdp_object__": 1,
          "entries": {
            "items": {"__rustwright_cdp_array__": 2, "items": [1, {"nested": true}]},
            "again": {"__rustwright_cdp_ref__": 2},
            "self": {"__rustwright_cdp_ref__": 1}
          }
        }`))
	if err != nil {
		t.Fatal(err)
	}
	wantItems := []any{float64(1), map[string]any{"nested": true}}
	want := map[string]any{
		"items": wantItems,
		"again": wantItems,
		"self":  map[string]any{"__rustwright_cdp_cycle__": true},
	}
	if !reflect.DeepEqual(decoded, want) {
		t.Fatalf("decoded wrappers = %#v, want %#v", decoded, want)
	}
}

func TestDecodeEvaluateJSONLeafRepresentations(t *testing.T) {
	loadWireDecodeLibrary(t)
	parsedURL, err := url.Parse("https://example.com/path?q=1")
	if err != nil {
		t.Fatal(err)
	}
	parsedDate := time.Date(2026, time.July, 21, 12, 34, 56, 789000000, time.UTC)

	tests := []struct {
		name  string
		wire  string
		check func(*testing.T, any)
	}{
		{
			name: "undefined",
			wire: `{"__rustwright_cdp_undefined__":true}`,
			check: func(t *testing.T, got any) {
				if got != nil {
					t.Fatalf("got %#v, want nil", got)
				}
			},
		},
		{
			name: "symbol",
			wire: `{"__rustwright_cdp_symbol__":true}`,
			check: func(t *testing.T, got any) {
				if got != nil {
					t.Fatalf("got %#v, want nil", got)
				}
			},
		},
		{
			name: "function",
			wire: `{"__rustwright_cdp_function__":true}`,
			check: func(t *testing.T, got any) {
				if got != nil {
					t.Fatalf("got %#v, want nil", got)
				}
			},
		},
		{
			name: "NaN",
			wire: `{"__rustwright_cdp_unserializable_value__":"NaN"}`,
			check: func(t *testing.T, got any) {
				value, ok := got.(float64)
				if !ok || !math.IsNaN(value) {
					t.Fatalf("got %#v, want math.NaN()", got)
				}
			},
		},
		{
			name: "positive infinity",
			wire: `{"__rustwright_cdp_unserializable_value__":"Infinity"}`,
			check: func(t *testing.T, got any) {
				value, ok := got.(float64)
				if !ok || !math.IsInf(value, 1) {
					t.Fatalf("got %#v, want math.Inf(1)", got)
				}
			},
		},
		{
			name: "negative infinity",
			wire: `{"__rustwright_cdp_unserializable_value__":"-Infinity"}`,
			check: func(t *testing.T, got any) {
				value, ok := got.(float64)
				if !ok || !math.IsInf(value, -1) {
					t.Fatalf("got %#v, want math.Inf(-1)", got)
				}
			},
		},
		{
			name: "negative zero wrapper preserved",
			wire: `{"__rustwright_cdp_unserializable_value__":"-0"}`,
			check: func(t *testing.T, got any) {
				want := map[string]any{"__rustwright_cdp_unserializable_value__": "-0"}
				if !reflect.DeepEqual(got, want) {
					t.Fatalf("got %#v, want %#v", got, want)
				}
			},
		},
		{
			name: "bigint wrapper preserved",
			wire: `{"__rustwright_cdp_unserializable_value__":"123n"}`,
			check: func(t *testing.T, got any) {
				want := map[string]any{"__rustwright_cdp_unserializable_value__": "123n"}
				if !reflect.DeepEqual(got, want) {
					t.Fatalf("got %#v, want %#v", got, want)
				}
			},
		},
		{
			name: "date",
			wire: `{"__rustwright_cdp_date__":"2026-07-21T12:34:56.789Z"}`,
			check: func(t *testing.T, got any) {
				if !reflect.DeepEqual(got, parsedDate) {
					t.Fatalf("got %#v, want %#v", got, parsedDate)
				}
			},
		},
		{
			name: "URL",
			wire: `{"__rustwright_cdp_url__":"https://example.com/path?q=1"}`,
			check: func(t *testing.T, got any) {
				if !reflect.DeepEqual(got, parsedURL) {
					t.Fatalf("got %#v, want %#v", got, parsedURL)
				}
			},
		},
		{
			name: "regexp p and f payload",
			wire: `{"__rustwright_cdp_regexp__":{"p":"a+b","f":"gi"}}`,
			check: func(t *testing.T, got any) {
				want := RegExpValue{Pattern: "a+b", Flags: "gi"}
				if !reflect.DeepEqual(got, want) {
					t.Fatalf("got %#v, want %#v", got, want)
				}
			},
		},
		{
			name: "error",
			wire: `{"__rustwright_cdp_error__":{"name":"TypeError","message":"broken","stack":"trace"}}`,
			check: func(t *testing.T, got any) {
				want := JavaScriptError{Name: "TypeError", Message: "broken", Stack: "trace"}
				if !reflect.DeepEqual(got, want) {
					t.Fatalf("got %#v, want %#v", got, want)
				}
			},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := decodeEvaluateJSON([]byte(test.wire))
			if err != nil {
				t.Fatal(err)
			}
			test.check(t, got)
		})
	}
}
