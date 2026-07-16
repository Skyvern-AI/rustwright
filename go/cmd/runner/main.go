package main

import (
	"flag"
	"fmt"
	"os"

	rustwright "github.com/Skyvern-AI/rustwright/go"
	benchmark "github.com/Skyvern-AI/rustwright/go/internal/runner"
)

type optionalString struct {
	value string
	set   bool
}

func (s *optionalString) String() string { return s.value }
func (s *optionalString) Set(value string) error {
	s.value = value
	s.set = true
	return nil
}

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	if len(args) > 0 && args[0] == "--" {
		args = args[1:]
	}
	flags := flag.NewFlagSet("runner", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	manifestPath := flags.String("manifest", "", "benchmark manifest JSON path (required)")
	libraryPath := flags.String("lib", "", "librustwright_capi shared library path (required)")
	outputPath := flags.String("out", "", "results JSON output path (required)")
	var cases optionalString
	flags.Var(&cases, "cases", "comma-separated exact case ids")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if flags.NArg() != 0 {
		fmt.Fprintf(os.Stderr, "runner: unexpected arguments: %v\n", flags.Args())
		return 2
	}
	if *manifestPath == "" || *libraryPath == "" || *outputPath == "" {
		fmt.Fprintln(os.Stderr, "runner: --manifest, --lib, and --out are required")
		return 2
	}

	manifest, err := benchmark.LoadManifest(*manifestPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "runner:", err)
		return 2
	}
	selected, err := benchmark.SelectCases(manifest, cases.value, cases.set)
	if err != nil {
		fmt.Fprintln(os.Stderr, "runner:", err)
		return 2
	}
	chromium, err := rustwright.Open(*libraryPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "runner:", err)
		return 2
	}
	output, executeErr := benchmark.Execute(chromium, selected)
	if output.Lang != "" {
		if err := benchmark.WriteOutput(*outputPath, output); err != nil {
			fmt.Fprintln(os.Stderr, "runner:", err)
			return 2
		}
	}
	if executeErr != nil {
		fmt.Fprintln(os.Stderr, "runner:", executeErr)
		return 2
	}
	if !benchmark.AllPassed(output) {
		return 1
	}
	return 0
}
