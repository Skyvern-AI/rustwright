package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	rustwright "github.com/Skyvern-AI/rustwright/go"
	"github.com/Skyvern-AI/rustwright/go/internal/runner"
)

const smokeHTML = `<!doctype html>
<html>
  <head><title>Rustwright Go Smoke</title></head>
  <body>
    <h1 id="message">ready</h1>
    <input id="name" />
    <button id="go" onclick="document.querySelector('#message').textContent = document.querySelector('#name').value">Go</button>
  </body>
</html>`

type smokeRecord struct {
	Title           string `json:"title"`
	Before          string `json:"before"`
	After           string `json:"after"`
	Value           any    `json:"value"`
	ScreenshotBytes int    `json:"screenshotBytes"`
}

func main() {
	args := os.Args[1:]
	if len(args) > 0 && args[0] == "--" {
		args = args[1:]
	}
	flags := flag.NewFlagSet("smoke", flag.ContinueOnError)
	library := flags.String("lib", rustwright.DefaultLibraryPath(), "path to librustwright_capi shared library")
	if err := flags.Parse(args); err != nil {
		os.Exit(2)
	}
	if flags.NArg() != 0 {
		fmt.Fprintf(os.Stderr, "smoke: unexpected arguments: %v\n", flags.Args())
		os.Exit(2)
	}

	record, err := run(*library)
	if err != nil {
		fmt.Fprintln(os.Stderr, "smoke:", err)
		os.Exit(1)
	}
	encoded, err := json.Marshal(record)
	if err != nil {
		fmt.Fprintln(os.Stderr, "smoke: encode output:", err)
		os.Exit(1)
	}
	fmt.Println(string(encoded))
}

func run(library string) (_ smokeRecord, err error) {
	chromium, err := rustwright.Open(library)
	if err != nil {
		return smokeRecord{}, err
	}
	browser, err := chromium.Launch(rustwright.LaunchOptions{})
	if err != nil {
		return smokeRecord{}, err
	}
	browserClosed := false
	defer func() {
		if !browserClosed {
			_ = browser.Close()
		}
	}()

	page, err := browser.NewPage()
	if err != nil {
		return smokeRecord{}, err
	}
	pageClosed := false
	defer func() {
		if !pageClosed {
			_ = page.Close(nil)
		}
	}()

	if _, err := page.Goto(runner.CaseHTMLDataURL(smokeHTML), nil); err != nil {
		return smokeRecord{}, err
	}
	title, err := page.Title(nil)
	if err != nil {
		return smokeRecord{}, err
	}
	before, err := page.TextContent("#message", nil)
	if err != nil {
		return smokeRecord{}, err
	}
	if before == nil {
		return smokeRecord{}, fmt.Errorf("#message textContent was null before click")
	}
	const filledValue = "Rustwright for Go"
	if err := page.Fill("#name", filledValue, nil); err != nil {
		return smokeRecord{}, err
	}
	if err := page.Click("#go", nil); err != nil {
		return smokeRecord{}, err
	}
	after, err := page.TextContent("#message", nil)
	if err != nil {
		return smokeRecord{}, err
	}
	if after == nil {
		return smokeRecord{}, fmt.Errorf("#message textContent was null after click")
	}
	value, err := page.Evaluate("document.querySelector('#name').value", nil, nil)
	if err != nil {
		return smokeRecord{}, err
	}
	screenshotPath := filepath.Join(os.TempDir(), fmt.Sprintf("rustwright-go-smoke-%d.png", os.Getpid()))
	defer os.Remove(screenshotPath)
	screenshot, err := page.Screenshot(&rustwright.ScreenshotOptions{Path: screenshotPath})
	if err != nil {
		return smokeRecord{}, err
	}
	if err := page.Close(nil); err != nil {
		return smokeRecord{}, err
	}
	pageClosed = true
	if err := browser.Close(); err != nil {
		return smokeRecord{}, err
	}
	browserClosed = true
	return smokeRecord{
		Title:           title,
		Before:          *before,
		After:           *after,
		Value:           value,
		ScreenshotBytes: len(screenshot),
	}, nil
}
