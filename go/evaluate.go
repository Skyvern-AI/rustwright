package rustwright

import (
	"encoding/json"
	"fmt"
	"math"
	"net/url"
	"time"
)

// RegExpValue preserves a JavaScript regular expression's source and flags.
type RegExpValue struct {
	Pattern string `json:"pattern"`
	Flags   string `json:"flags"`
}

// JavaScriptError is the native representation of an evaluated Error object.
type JavaScriptError struct {
	Name    string `json:"name,omitempty"`
	Message string `json:"message,omitempty"`
	Stack   string `json:"stack,omitempty"`
}

func (e JavaScriptError) Error() string {
	if e.Name == "" {
		return e.Message
	}
	if e.Message == "" {
		return e.Name
	}
	return e.Name + ": " + e.Message
}

func decodeEvaluateJSON(data []byte) (any, error) {
	native, err := currentWireDecodeNative()
	if err != nil {
		return nil, err
	}
	data, err = native.decodeWireJSON(data)
	if err != nil {
		return nil, err
	}
	var raw any
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("decode evaluate JSON: %w", err)
	}
	return mapEvaluateLeaves(raw)
}

func mapEvaluateLeaves(value any) (any, error) {
	switch value := value.(type) {
	case []any:
		decoded := make([]any, len(value))
		for i := range value {
			item, err := mapEvaluateLeaves(value[i])
			if err != nil {
				return nil, err
			}
			decoded[i] = item
		}
		return decoded, nil
	case map[string]any:
		return mapEvaluateObjectLeaves(value)
	default:
		return value, nil
	}
}

func mapEvaluateObjectLeaves(value map[string]any) (any, error) {
	if number, ok := value["__rustwright_cdp_unserializable_value__"].(string); ok {
		switch number {
		case "NaN":
			return math.NaN(), nil
		case "Infinity":
			return math.Inf(1), nil
		case "-Infinity":
			return math.Inf(-1), nil
		}
	}
	if _, ok := value["__rustwright_cdp_undefined__"]; ok {
		return nil, nil
	}
	if _, ok := value["__rustwright_cdp_symbol__"]; ok {
		return nil, nil
	}
	if _, ok := value["__rustwright_cdp_function__"]; ok {
		return nil, nil
	}
	if encoded, ok := value["__rustwright_cdp_date__"].(string); ok {
		if parsed, err := time.Parse(time.RFC3339Nano, encoded); err == nil {
			return parsed, nil
		}
		return encoded, nil
	}
	if encoded, ok := value["__rustwright_cdp_url__"].(string); ok {
		if parsed, err := url.Parse(encoded); err == nil {
			return parsed, nil
		}
		return encoded, nil
	}
	if encoded, ok := value["__rustwright_cdp_regexp__"].(map[string]any); ok {
		pattern, _ := encoded["p"].(string)
		flags, _ := encoded["f"].(string)
		return RegExpValue{Pattern: pattern, Flags: flags}, nil
	}
	if encoded, ok := value["__rustwright_cdp_error__"].(map[string]any); ok {
		name, _ := encoded["name"].(string)
		message, _ := encoded["message"].(string)
		stack, _ := encoded["stack"].(string)
		return JavaScriptError{Name: name, Message: message, Stack: stack}, nil
	}

	decoded := make(map[string]any, len(value))
	for key, entry := range value {
		item, err := mapEvaluateLeaves(entry)
		if err != nil {
			return nil, err
		}
		decoded[key] = item
	}
	return decoded, nil
}
