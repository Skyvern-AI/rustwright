package runner

import "testing"

func TestCaseHTMLDataURL(t *testing.T) {
	got := CaseHTMLDataURL("A z+~é")
	want := "data:text/html;charset=utf-8,A%20z%2B~%C3%A9"
	if got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}

func TestManifestRejectsContractViolations(t *testing.T) {
	tests := []struct {
		name string
		json string
	}{
		{"unknown version", `{"version":2,"cases":[{"id":"a","steps":[{"op":"title","capture":"x"}]}]}`},
		{"duplicate id", `{"version":1,"cases":[{"id":"a","steps":[{"op":"title","capture":"x"}]},{"id":"a","steps":[{"op":"title","capture":"y"}]}]}`},
		{"duplicate capture", `{"version":1,"cases":[{"id":"a","steps":[{"op":"title","capture":"x"},{"op":"screenshot","capture":"x"}]}]}`},
		{"unknown op", `{"version":1,"cases":[{"id":"a","steps":[{"op":"wat"}]}]}`},
		{"unknown field", `{"version":1,"extra":true,"cases":[{"id":"a","steps":[{"op":"title","capture":"x"}]}]}`},
		{"empty waitUntil", `{"version":1,"cases":[{"id":"a","steps":[{"op":"goto","url":"about:blank","waitUntil":""}]}]}`},
		{"null optional string", `{"version":1,"cases":[{"id":"a","html":null,"steps":[{"op":"title","capture":"x"}]}]}`},
		{"null fill value", `{"version":1,"cases":[{"id":"a","steps":[{"op":"fill","selector":"#x","value":null}]}]}`},
		{"null assertion predicate", `{"version":1,"cases":[{"id":"a","steps":[{"op":"assertTitle","equals":null}]}]}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := ParseManifest([]byte(test.json)); err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}
