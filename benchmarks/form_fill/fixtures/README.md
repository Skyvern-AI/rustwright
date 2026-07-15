# Controlled fixtures

These pages provide a deterministic benchmark lane for library-latency evidence.
They contain no external resources or requests. Stable selectors, fixed data,
non-navigating click handlers, and a fixed 300 ms dynamic delay remove the
live-site and implicit-navigation variance described in `cases/WEAKNESSES.md`
M2, C2, and R6.

## Remote browsers: use data URLs

The remote Skyvern browser cannot reach the benchmark runner's `127.0.0.1`.
Load the fixture bytes directly instead:

```python
from harness.serve_fixtures import fixture_data_url

page.goto(
    fixture_data_url("click.html"),
    wait_until="domcontentloaded",
)
```

`fixture_data_url()` reads the checked-in file as bytes and returns a
`data:text/html;base64,...` URL. There is no fixture-server request, DNS lookup,
TLS handshake, redirect, or third-party page variation. `cases/controlled.json`
embeds URLs produced by this helper so both backends load identical bytes. Page
setup is a separate `goto` span; the repeated operation spans contain no page
navigation or server rendering.

The current controlled cases use only the `nav_workload.py` operations `goto`,
`wait`, `click`, and `eval`. `form.html` has twelve deterministic controls and
an inert, disabled submit button, but measuring native `fill`, `check`, or
`select_option` calls will require adding those operations to `nav_workload.py`.
Do not substitute `eval` assignments when making native form-operation claims.

## Local fallback

For a browser running on the same host, start the standard-library server:

```console
$ python benchmarks/form_fill/harness/serve_fixtures.py
Serving controlled fixtures at http://127.0.0.1:8099/
```

Set `FIXTURE_PORT` to choose another port. The server is also a context manager:

```python
from harness.serve_fixtures import FixtureServer

with FixtureServer(port=0) as server:
    page.goto(server.fixture_url("index.html"))
```

Port `0` asks the operating system for an unused local port. The localhost
server is only a development fallback; use data URLs for remote benchmark runs.
