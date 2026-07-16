package runner

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"sort"
	"strings"
)

type Manifest struct {
	Version int
	Cases   []Case
}

type Case struct {
	ID          string
	Description string
	HTML        string
	URL         string
	Steps       []Step
}

type Step struct {
	Op           string
	URL          string
	UseCaseHTML  bool
	WaitUntil    string
	Selector     string
	Value        string
	Capture      string
	Expression   string
	Arg          json.RawMessage
	HasArg       bool
	Equals       any
	HasEquals    bool
	EqualsString string
	Contains     string
	HasContains  bool
}

func LoadManifest(path string) (*Manifest, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read manifest: %w", err)
	}
	return ParseManifest(data)
}

func ParseManifest(data []byte) (*Manifest, error) {
	root, err := parseObject(data, "manifest")
	if err != nil {
		return nil, err
	}
	if err := fields(root, "manifest", []string{"version", "cases"}, []string{"version", "cases"}); err != nil {
		return nil, err
	}
	var version float64
	if err := json.Unmarshal(root["version"], &version); err != nil || version != 1 {
		return nil, fmt.Errorf("manifest.version: expected supported version 1")
	}
	var rawCases []json.RawMessage
	if err := json.Unmarshal(root["cases"], &rawCases); err != nil {
		return nil, fmt.Errorf("manifest.cases: expected array")
	}
	if len(rawCases) == 0 {
		return nil, fmt.Errorf("manifest.cases: expected at least one case")
	}

	manifest := &Manifest{Version: 1, Cases: make([]Case, 0, len(rawCases))}
	ids := make(map[string]struct{}, len(rawCases))
	for i, raw := range rawCases {
		parsed, err := parseCase(raw, i)
		if err != nil {
			return nil, err
		}
		if _, exists := ids[parsed.ID]; exists {
			return nil, fmt.Errorf("manifest.cases[%d].id: duplicate id %q", i, parsed.ID)
		}
		ids[parsed.ID] = struct{}{}
		manifest.Cases = append(manifest.Cases, parsed)
	}
	return manifest, nil
}

func parseCase(data []byte, index int) (Case, error) {
	context := fmt.Sprintf("manifest.cases[%d]", index)
	object, err := parseObject(data, context)
	if err != nil {
		return Case{}, err
	}
	if err := fields(object, context,
		[]string{"id", "description", "html", "url", "steps"},
		[]string{"id", "steps"}); err != nil {
		return Case{}, err
	}
	var parsed Case
	if parsed.ID, err = requiredNonemptyString(object, "id", context); err != nil {
		return Case{}, err
	}
	if parsed.Description, err = optionalString(object, "description", context); err != nil {
		return Case{}, err
	}
	if parsed.HTML, err = optionalString(object, "html", context); err != nil {
		return Case{}, err
	}
	if parsed.URL, err = optionalString(object, "url", context); err != nil {
		return Case{}, err
	}
	if _, present := object["url"]; present {
		if _, err := url.Parse(parsed.URL); err != nil {
			return Case{}, fmt.Errorf("%s.url: invalid URI reference: %v", context, err)
		}
	}
	var rawSteps []json.RawMessage
	if err := json.Unmarshal(object["steps"], &rawSteps); err != nil {
		return Case{}, fmt.Errorf("%s.steps: expected array", context)
	}
	if len(rawSteps) == 0 {
		return Case{}, fmt.Errorf("%s.steps: expected at least one step", context)
	}
	parsed.Steps = make([]Step, 0, len(rawSteps))
	captures := make(map[string]struct{})
	for i, raw := range rawSteps {
		step, err := parseStep(raw, fmt.Sprintf("%s.steps[%d]", context, i))
		if err != nil {
			return Case{}, err
		}
		if step.Capture != "" {
			if _, exists := captures[step.Capture]; exists {
				return Case{}, fmt.Errorf("%s.steps[%d].capture: duplicate capture %q", context, i, step.Capture)
			}
			captures[step.Capture] = struct{}{}
		}
		parsed.Steps = append(parsed.Steps, step)
	}
	return parsed, nil
}

func parseStep(data []byte, context string) (Step, error) {
	object, err := parseObject(data, context)
	if err != nil {
		return Step{}, err
	}
	op, err := requiredNonemptyString(object, "op", context)
	if err != nil {
		return Step{}, err
	}
	step := Step{Op: op}
	switch op {
	case "goto":
		if err := fields(object, context, []string{"op", "url", "useCaseHtml", "waitUntil"}, []string{"op"}); err != nil {
			return Step{}, err
		}
		urlRaw, hasURL := object["url"]
		useRaw, hasUse := object["useCaseHtml"]
		if hasURL == hasUse {
			return Step{}, fmt.Errorf("%s: goto requires exactly one of url or useCaseHtml", context)
		}
		if hasURL {
			if err := json.Unmarshal(urlRaw, &step.URL); err != nil || step.URL == "" {
				return Step{}, fmt.Errorf("%s.url: expected nonempty string", context)
			}
		} else if err := json.Unmarshal(useRaw, &step.UseCaseHTML); err != nil || !step.UseCaseHTML {
			return Step{}, fmt.Errorf("%s.useCaseHtml: expected true", context)
		}
		if _, present := object["waitUntil"]; present {
			if step.WaitUntil, err = requiredString(object, "waitUntil", context); err != nil {
				return Step{}, err
			}
			if step.WaitUntil != "load" && step.WaitUntil != "domcontentloaded" && step.WaitUntil != "networkidle" && step.WaitUntil != "commit" {
				return Step{}, fmt.Errorf("%s.waitUntil: unsupported value %q", context, step.WaitUntil)
			}
		}
	case "click":
		if err := exactStepFields(object, context, []string{"op", "selector"}); err != nil {
			return Step{}, err
		}
		step.Selector, err = requiredNonemptyString(object, "selector", context)
	case "fill":
		if err := exactStepFields(object, context, []string{"op", "selector", "value"}); err != nil {
			return Step{}, err
		}
		if step.Selector, err = requiredNonemptyString(object, "selector", context); err == nil {
			step.Value, err = requiredString(object, "value", context)
		}
	case "title":
		if err := exactStepFields(object, context, []string{"op", "capture"}); err != nil {
			return Step{}, err
		}
		step.Capture, err = requiredNonemptyString(object, "capture", context)
	case "textContent":
		if err := exactStepFields(object, context, []string{"op", "selector", "capture"}); err != nil {
			return Step{}, err
		}
		if step.Selector, err = requiredNonemptyString(object, "selector", context); err == nil {
			step.Capture, err = requiredNonemptyString(object, "capture", context)
		}
	case "evaluate":
		if err := fields(object, context, []string{"op", "expression", "arg", "capture"}, []string{"op", "expression", "capture"}); err != nil {
			return Step{}, err
		}
		if step.Expression, err = requiredNonemptyString(object, "expression", context); err == nil {
			step.Capture, err = requiredNonemptyString(object, "capture", context)
		}
		if raw, ok := object["arg"]; ok {
			step.HasArg = true
			step.Arg = append(json.RawMessage(nil), raw...)
		}
	case "screenshot":
		if err := exactStepFields(object, context, []string{"op", "capture"}); err != nil {
			return Step{}, err
		}
		step.Capture, err = requiredNonemptyString(object, "capture", context)
	case "assertTitle":
		if err := fields(object, context, []string{"op", "equals", "contains"}, []string{"op"}); err != nil {
			return Step{}, err
		}
		err = parseStringPredicate(object, context, &step)
	case "assertText":
		if err := fields(object, context, []string{"op", "selector", "equals", "contains"}, []string{"op", "selector"}); err != nil {
			return Step{}, err
		}
		if step.Selector, err = requiredNonemptyString(object, "selector", context); err == nil {
			err = parseStringPredicate(object, context, &step)
		}
	case "assertEval":
		if err := exactStepFields(object, context, []string{"op", "expression", "equals"}); err != nil {
			return Step{}, err
		}
		if step.Expression, err = requiredNonemptyString(object, "expression", context); err != nil {
			break
		}
		step.HasEquals = true
		if err = json.Unmarshal(object["equals"], &step.Equals); err != nil {
			err = fmt.Errorf("%s.equals: invalid JSON value", context)
		}
	default:
		return Step{}, fmt.Errorf("%s.op: unknown operation %q", context, op)
	}
	if err != nil {
		return Step{}, err
	}
	return step, nil
}

func parseStringPredicate(object map[string]json.RawMessage, context string, step *Step) error {
	_, hasEquals := object["equals"]
	_, hasContains := object["contains"]
	if hasEquals == hasContains {
		return fmt.Errorf("%s: assertion requires exactly one of equals or contains", context)
	}
	if hasEquals {
		step.HasEquals = true
		var err error
		step.EqualsString, err = requiredString(object, "equals", context)
		return err
	}
	step.HasContains = true
	var err error
	step.Contains, err = requiredString(object, "contains", context)
	return err
}

func parseObject(data []byte, context string) (map[string]json.RawMessage, error) {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(data, &object); err != nil || object == nil {
		return nil, fmt.Errorf("%s: expected JSON object", context)
	}
	return object, nil
}

func fields(object map[string]json.RawMessage, context string, allowed, required []string) error {
	allow := make(map[string]struct{}, len(allowed))
	for _, key := range allowed {
		allow[key] = struct{}{}
	}
	var unknown []string
	for key := range object {
		if _, ok := allow[key]; !ok {
			unknown = append(unknown, key)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return fmt.Errorf("%s: unknown field(s): %s", context, strings.Join(unknown, ", "))
	}
	for _, key := range required {
		if _, ok := object[key]; !ok {
			return fmt.Errorf("%s: missing required field %q", context, key)
		}
	}
	return nil
}

func exactStepFields(object map[string]json.RawMessage, context string, required []string) error {
	return fields(object, context, required, required)
}

func requiredNonemptyString(object map[string]json.RawMessage, key, context string) (string, error) {
	value, err := requiredString(object, key, context)
	if err != nil {
		return "", err
	}
	if value == "" {
		return "", fmt.Errorf("%s.%s: expected nonempty string", context, key)
	}
	return value, nil
}

func requiredString(object map[string]json.RawMessage, key, context string) (string, error) {
	raw, ok := object[key]
	if !ok {
		return "", fmt.Errorf("%s: missing required field %q", context, key)
	}
	var value *string
	if err := json.Unmarshal(raw, &value); err != nil || value == nil {
		return "", fmt.Errorf("%s.%s: expected string", context, key)
	}
	return *value, nil
}

func optionalString(object map[string]json.RawMessage, key, context string) (string, error) {
	if _, ok := object[key]; !ok {
		return "", nil
	}
	return requiredString(object, key, context)
}
