package rustwright

import (
	"math"
	"reflect"
	"testing"
)

func TestDecodeEvaluateJSONWrappers(t *testing.T) {
	decoded, err := decodeEvaluateJSON([]byte(`{
          "__rustwright_cdp_object__": 1,
          "entries": {
            "items": {"__rustwright_cdp_array__": 2, "items": [1, {"nested": true}]},
            "again": {"__rustwright_cdp_ref__": 2}
          }
        }`))
	if err != nil {
		t.Fatal(err)
	}
	object := decoded.(map[string]any)
	want := []any{float64(1), map[string]any{"nested": true}}
	if !reflect.DeepEqual(object["items"], want) || !reflect.DeepEqual(object["again"], want) {
		t.Fatalf("decoded wrappers = %#v", decoded)
	}
}

func TestDecodeEvaluateJSONTags(t *testing.T) {
	decoded, err := decodeEvaluateJSON([]byte(`[
          {"__rustwright_cdp_undefined__": true},
          {"__rustwright_cdp_symbol__": true},
          {"__rustwright_cdp_function__": true},
          {"__rustwright_cdp_unserializable_value__": "NaN"}
        ]`))
	if err != nil {
		t.Fatal(err)
	}
	values := decoded.([]any)
	if values[0] != nil || values[1] != nil || values[2] != nil || !math.IsNaN(values[3].(float64)) {
		t.Fatalf("decoded tags = %#v", values)
	}
}
