package rustwright

import "testing"

func TestLaunchOptionsHeadlessWireJSON(t *testing.T) {
	tests := []struct {
		name    string
		options LaunchOptions
		want    string
	}{
		{name: "default", options: LaunchOptions{}, want: `{"headless":true}`},
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
