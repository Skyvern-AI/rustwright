# Cross-language benchmark cases

- `manifest.schema.json` — normative JSON Schema (draft 2020-12) for a case manifest.
- `smoke.json` — 5-case subset, run on every PR by `.github/workflows/bindings.yml`.
- `full.json` — 100-case suite, run by `.github/workflows/bindings-benchmark.yml`.

`full.json` is currently a **generated placeholder** produced by
`tools/gen_binding_cases.py` — deterministic, hermetic (every case navigates to
inline HTML), and equivalence-safe (every capture is structural JSON, so all six
language runners must agree byte-for-byte). It exists so the full cross-language
benchmark is runnable today and the harness is proven at 100-case scale.

Replace `full.json` with the curated 100-case suite when it lands: the schema,
the per-language runners, and the CI workflows do not change — only the manifest
file does. Point the benchmark at a different manifest via the
`bindings-benchmark.yml` `manifest` input.

Every runner (`go`, `java`, `csharp`, `ruby`, `php`, `rust`) consumes these via
the CLI documented in [`../CONTRACT.md`](../CONTRACT.md), and
`tools/compare_binding_results.py` asserts all languages produce identical
captures (including screenshot byte lengths).
