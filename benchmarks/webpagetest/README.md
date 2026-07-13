# WebPageTest

WebPageTest is an external synthetic page-performance service. Use it for
page-load diagnostics under controlled browsers, locations, and network
conditions.

This is not a local automation-library overhead benchmark. It is useful when we
want an independent page-load baseline or waterfall/HAR diagnostics.

## Dry Run

```bash
python benchmarks/webpagetest/run.py --url https://example.com --dry-run
```

## Submit a Test

```bash
WEBPAGETEST_API_KEY=... python benchmarks/webpagetest/run.py --url https://example.com --run
```

The wrapper writes submission responses under ignored
`.benchmark-data/browser-speed/results/webpagetest/`.
