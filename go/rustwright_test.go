package rustwright

import "testing"

func TestLaunchOptionsHeadlessWireJSON(t *testing.T) {
	tests := []struct {
		name    string
		options LaunchOptions
		want    string
	}{
		{name: "default omits headless so the core default applies", options: LaunchOptions{}, want: `{}`},
		{name: "explicit true", options: LaunchOptions{Headless: Bool(true)}, want: `{"headless":true}`},
		{name: "explicit false", options: LaunchOptions{Headless: Bool(false)}, want: `{"headless":false}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := test.options.wireJSON()
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != test.want {
				t.Fatalf("wireJSON() = %s, want %s", got, test.want)
			}
		})
	}
}
