package runner

import (
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"strings"
	"time"

	rustwright "github.com/Skyvern-AI/rustwright/go"
)

type Output struct {
	Lang    string       `json:"lang"`
	Results []CaseResult `json:"results"`
}

type CaseResult struct {
	ID       string         `json:"id"`
	OK       bool           `json:"ok"`
	Captures map[string]any `json:"captures"`
	MS       float64        `json:"ms"`
	Error    string         `json:"error,omitempty"`
}

func SelectCases(manifest *Manifest, requested string, specified bool) ([]Case, error) {
	if !specified {
		return append([]Case(nil), manifest.Cases...), nil
	}
	if requested == "" {
		return nil, fmt.Errorf("--cases must contain at least one id")
	}
	wanted := make(map[string]struct{})
	for _, id := range strings.Split(requested, ",") {
		if id == "" {
			return nil, fmt.Errorf("--cases contains an empty id")
		}
		if _, duplicate := wanted[id]; duplicate {
			return nil, fmt.Errorf("--cases contains duplicate id %q", id)
		}
		wanted[id] = struct{}{}
	}
	found := make(map[string]struct{}, len(wanted))
	selected := make([]Case, 0, len(wanted))
	for _, testCase := range manifest.Cases {
		if _, ok := wanted[testCase.ID]; ok {
			selected = append(selected, testCase)
			found[testCase.ID] = struct{}{}
		}
	}
	for id := range wanted {
		if _, ok := found[id]; !ok {
			return nil, fmt.Errorf("--cases requested unknown id %q", id)
		}
	}
	return selected, nil
}

func Execute(chromium *rustwright.Chromium, cases []Case) (Output, error) {
	browser, err := chromium.Launch(rustwright.LaunchOptions{})
	if err != nil {
		return Output{}, err
	}
	output := Output{Lang: "go", Results: make([]CaseResult, 0, len(cases))}
	for _, testCase := range cases {
		output.Results = append(output.Results, executeCase(browser, testCase))
	}
	if err := browser.Close(); err != nil {
		return output, err
	}
	return output, nil
}

func executeCase(browser *rustwright.Browser, testCase Case) CaseResult {
	started := time.Now()
	result := CaseResult{
		ID:       testCase.ID,
		Captures: make(map[string]any),
	}
	page, err := browser.NewPage()
	if err != nil {
		result.Error = "new page: " + err.Error()
		result.MS = elapsedMilliseconds(started)
		return result
	}
	for i, step := range testCase.Steps {
		if err := executeStep(page, testCase, step, result.Captures); err != nil {
			result.Error = fmt.Sprintf("step %d: %v", i+1, err)
			break
		}
	}
	if err := page.Close(nil); err != nil && result.Error == "" {
		result.Error = "close page: " + err.Error()
	}
	result.OK = result.Error == ""
	result.MS = elapsedMilliseconds(started)
	return result
}

func executeStep(page *rustwright.Page, testCase Case, step Step, captures map[string]any) error {
	switch step.Op {
	case "goto":
		target := step.URL
		if step.UseCaseHTML {
			target = CaseHTMLDataURL(testCase.HTML)
		}
		var options *rustwright.GotoOptions
		if step.WaitUntil != "" {
			options = &rustwright.GotoOptions{WaitUntil: step.WaitUntil}
		}
		_, err := page.Goto(target, options)
		return err
	case "click":
		return page.Click(step.Selector, nil)
	case "fill":
		return page.Fill(step.Selector, step.Value, nil)
	case "title":
		value, err := page.Title(nil)
		if err == nil {
			captures[step.Capture] = value
		}
		return err
	case "textContent":
		value, err := page.TextContent(step.Selector, nil)
		if err != nil {
			return err
		}
		if value == nil {
			captures[step.Capture] = nil
		} else {
			captures[step.Capture] = *value
		}
		return nil
	case "evaluate":
		var arg any
		if step.HasArg {
			arg = step.Arg
		}
		value, err := page.Evaluate(step.Expression, arg, nil)
		if err == nil {
			captures[step.Capture] = value
		}
		return err
	case "screenshot":
		value, err := page.Screenshot(nil)
		if err == nil {
			captures[step.Capture] = len(value)
		}
		return err
	case "assertTitle":
		value, err := page.Title(nil)
		if err != nil {
			return err
		}
		return assertString(value, step)
	case "assertText":
		value, err := page.TextContent(step.Selector, nil)
		if err != nil {
			return err
		}
		if value == nil {
			return fmt.Errorf("textContent(%q) was null", step.Selector)
		}
		return assertString(*value, step)
	case "assertEval":
		value, err := page.Evaluate(step.Expression, nil, nil)
		if err != nil {
			return err
		}
		if !reflect.DeepEqual(value, step.Equals) {
			return fmt.Errorf("evaluation mismatch: got %s, want %s", jsonValue(value), jsonValue(step.Equals))
		}
		return nil
	default:
		return fmt.Errorf("unknown operation %q", step.Op)
	}
}

func assertString(value string, step Step) error {
	if step.HasEquals {
		if value != step.EqualsString {
			return fmt.Errorf("string mismatch: got %q, want %q", value, step.EqualsString)
		}
		return nil
	}
	if !strings.Contains(value, step.Contains) {
		return fmt.Errorf("string %q does not contain %q", value, step.Contains)
	}
	return nil
}

func CaseHTMLDataURL(html string) string {
	const hex = "0123456789ABCDEF"
	encoded := make([]byte, 0, len(html)*3)
	for _, b := range []byte(html) {
		if (b >= 'A' && b <= 'Z') || (b >= 'a' && b <= 'z') ||
			(b >= '0' && b <= '9') || b == '-' || b == '.' || b == '_' || b == '~' {
			encoded = append(encoded, b)
		} else {
			encoded = append(encoded, '%', hex[b>>4], hex[b&15])
		}
	}
	return "data:text/html;charset=utf-8," + string(encoded)
}

func WriteOutput(path string, output Output) error {
	encoded, err := json.MarshalIndent(output, "", "  ")
	if err != nil {
		return fmt.Errorf("encode results: %w", err)
	}
	encoded = append(encoded, '\n')
	if err := os.WriteFile(path, encoded, 0o644); err != nil {
		return fmt.Errorf("write results: %w", err)
	}
	return nil
}

func AllPassed(output Output) bool {
	for _, result := range output.Results {
		if !result.OK {
			return false
		}
	}
	return true
}

func elapsedMilliseconds(started time.Time) float64 {
	return float64(time.Since(started)) / float64(time.Millisecond)
}

func jsonValue(value any) string {
	encoded, err := json.Marshal(value)
	if err != nil {
		return fmt.Sprintf("%#v", value)
	}
	return string(encoded)
}
