# Cross-language benchmark cases

- `manifest.schema.json` — normative JSON Schema (draft 2020-12) for a case manifest.
- `smoke.json` — 5-case subset, run on every PR by `.github/workflows/bindings.yml`.
- `full.json` — 150-case suite, run by `.github/workflows/bindings-benchmark.yml`
  (weekly + on demand). A lighter per-version functional matrix runs in
  `.github/workflows/bindings-functional.yml`.

`full.json` is **generated** by `tools/gen_binding_cases.py` — deterministic,
hermetic (every case navigates to inline HTML), and equivalence-safe (every
capture is structural JSON, so all language runners must agree byte-for-byte).
It spans ten case families: title/form/eval/text/screenshot basics plus
deep-DOM targets (`dom-*`), multi-step mutation sequences (`seq-*`), unicode
and long-string fills (`uni-*`), nested structural-JSON evaluates (`evalx-*`),
and additional deterministic renders (`shotx-*`). Regenerate with
`python3 tools/gen_binding_cases.py`; case ids are stable across regenerations.

To benchmark a different manifest, pass it via the `bindings-benchmark.yml`
`manifest` input — the schema, the per-language runners, and the CI workflows
are manifest-agnostic.

Every runner (`go`, `java`, `csharp`, `ruby`, `php`, `rust`) consumes these via
the CLI documented in [`../CONTRACT.md`](../CONTRACT.md), and
`tools/compare_binding_results.py` asserts all languages produce identical
captures (including screenshot byte lengths).
