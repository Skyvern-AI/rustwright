# Remote browsers

Rustwright supports remote Chromium through raw Chrome DevTools Protocol (CDP).
Use `chromium.connect_over_cdp()` with an HTTP discovery URL or a direct CDP
WebSocket URL. Rustwright does not implement Playwright's internal wire protocol.

## API contract

```python
from rustwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.connect_over_cdp("http://browser:9222")
```

```python
from rustwright.async_api import async_playwright

async with async_playwright() as playwright:
    browser = await playwright.chromium.connect_over_cdp("http://browser:9222")
```

`BrowserType.connect()` is intentionally unsupported. It rejects every endpoint
before network I/O. This is a breaking change for Rustwright 0.3.0. Replace:

```python
browser = playwright.chromium.connect(endpoint)
```

with:

```python
browser = playwright.chromium.connect_over_cdp(endpoint)
```

The remote service must expose raw Chromium CDP. Endpoints from
`playwright run-server` and `BrowserType.launchServer()` use the Playwright wire
protocol and are unsupported. Direct WebSocket protocol detection is heuristic.
Use an HTTP discovery URL when you need a definitive protocol diagnosis.

For HTTP endpoints, Rustwright requests `/json/version` first. It uses a valid
`webSocketDebuggerUrl` from that response. Otherwise, it requests `/json` only
to detect Playwright's `wsEndpointPath`. Both requests share one connection
deadline and receive the caller's headers.

## Open WebUI migration

Change both Open WebUI loader calls:

```python
browser = p.chromium.connect_over_cdp(self.playwright_ws_url)
browser = await p.chromium.connect_over_cdp(self.playwright_ws_url)
```

Use this demo-grade compose profile. The image runs as root and its default
entrypoint starts Chrome with `--no-sandbox`. Do not use this default profile as
a production security claim.

```yaml
services:
  playwright:
    image: docker.io/chromedp/headless-shell:148.0.7778.96@sha256:9ca10461026046ce0e304b9d6e0460257ea60d7d77987f0659562c0e29779c4d
    init: true
    shm_size: "2gb"
    expose:
      - "9222"
    healthcheck:
      test:
        [
          "CMD",
          "/bin/bash",
          "-c",
          "exec 3<>/dev/tcp/127.0.0.1/9222 && printf 'GET /json/version HTTP/1.0\r\nHost: localhost\r\n\r\n' >&3 && IFS= read -r status <&3 && [[ "$$status" == *' 200 '* ]]",
        ]
      interval: 2s
      timeout: 2s
      retries: 30
      start_period: 5s
    networks:
      - remote-browser

  open-webui:
    depends_on:
      playwright:
        condition: service_healthy
    environment:
      - "WEB_LOADER_ENGINE=playwright"
      - "PLAYWRIGHT_WS_URL=http://playwright:9222"
    networks:
      - remote-browser

networks:
  remote-browser:
```

The profile does not publish CDP to the host. The HTTP URL is intentional.
Rustwright reads the advertised browser WebSocket from `/json/version`.

## Production-evaluation variant

The following override removes `--no-sandbox` and runs the image as an
unprivileged user. It remains an evaluation candidate until the seccomp and
native-architecture gates below pass.

```yaml
services:
  playwright:
    user: "65534:65534"
    entrypoint: ["/headless-shell/headless-shell"]
    command:
      - "--remote-debugging-address=0.0.0.0"
      - "--remote-debugging-port=9222"
      - "--disable-gpu"
      - "--enable-unsafe-swiftshader"
      - "--headless"
    security_opt:
      - "seccomp=./docs/remote-browser/chrome-seccomp.json"
    cap_drop:
      - ALL
    read_only: true
    tmpfs:
      - "/tmp:size=512m,mode=1777"
```

The vendored `docs/remote-browser/chrome-seccomp.json` comes from
`jfrazelle/dotfiles` commit `94c5f2bc4178d4000ff6f1ae5cc585799ef25d37`,
file `etc/docker/seccomp/chrome.json`. Jessie Frazelle published the source
under the MIT License. The complete notice is preserved in
[`docs/remote-browser/LICENSE.chrome-seccomp`](remote-browser/LICENSE.chrome-seccomp).
The immutable source is
<https://raw.githubusercontent.com/jfrazelle/dotfiles/94c5f2bc4178d4000ff6f1ae5cc585799ef25d37/etc/docker/seccomp/chrome.json>.
The allowlist still requires review against the pinned browser before a
production claim.

Keep CDP on a private network. Deny access to cloud metadata and internal
control planes. Restrict browser egress where possible. Use a disposable,
single-tenant browser container. Put authentication in front of any intentional
public exposure. User-supplied URLs still create browser and SSRF risk.

## Compose verification gate

**VERIFICATION-PENDING.** No container was launched for this change. Run this
complete, fail-closed verifier on native Linux. Do not split it into manual
steps or continue after a failure.

Required inputs:

- `RUSTWRIGHT_ROOT`: the Rustwright checkout with this change.
- `OPEN_WEBUI_ROOT`: an Open WebUI checkout with both loader calls migrated.
- `RW_PLATFORM`: exactly `linux/arm64` or `linux/amd64`, matching the host.
- `RW_OPEN_WEBUI_IMAGE`: a pinned Open WebUI image digest whose installed
  dependencies match the checkout.

```bash
#!/usr/bin/env bash
set -euo pipefail

: "${RUSTWRIGHT_ROOT:?set RUSTWRIGHT_ROOT to the Rustwright checkout}"
: "${OPEN_WEBUI_ROOT:?set OPEN_WEBUI_ROOT to the revised Open WebUI checkout}"
: "${RW_PLATFORM:?set RW_PLATFORM to linux/arm64 or linux/amd64}"
: "${RW_OPEN_WEBUI_IMAGE:?set RW_OPEN_WEBUI_IMAGE to a pinned image digest}"
[[ "$RW_OPEN_WEBUI_IMAGE" == *@sha256:* ]]

RW_IMAGE='docker.io/chromedp/headless-shell:148.0.7778.96@sha256:9ca10461026046ce0e304b9d6e0460257ea60d7d77987f0659562c0e29779c4d'
RW_ROOT="$(cd "$RUSTWRIGHT_ROOT" && pwd -P)"
OWUI_ROOT="$(cd "$OPEN_WEBUI_ROOT" && pwd -P)"

case "$RW_PLATFORM:$(uname -m)" in
  linux/arm64:arm64|linux/arm64:aarch64|linux/amd64:x86_64|linux/amd64:amd64) ;;
  *)
    printf 'RW_PLATFORM must match the native host architecture; emulation does not close this gate\n' >&2
    exit 2
    ;;
esac

for command_name in cargo docker python3; do
  command -v "$command_name" >/dev/null
done
docker compose version >/dev/null
docker version >/dev/null

[[ -f "$RW_ROOT/pyproject.toml" ]]
[[ -f "$RW_ROOT/docs/remote-browser/chrome-seccomp.json" ]]
[[ -f "$OWUI_ROOT/pyproject.toml" ]]
[[ -f "$OWUI_ROOT/backend/open_webui/retrieval/web/utils.py" ]]

PROJECT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rw-connect-verifier.XXXXXX")"
PROJECT_NAME="rw-connect-verifier-$$"
ACTIVE_PROJECT=''
ACTIVE_FILES=()

compose() {
  docker compose --project-name "$ACTIVE_PROJECT" "${ACTIVE_FILES[@]}" "$@"
}

cleanup() {
  status=$?
  trap - EXIT INT TERM
  set +e
  if [[ -n "$ACTIVE_PROJECT" && ${#ACTIVE_FILES[@]} -gt 0 ]]; then
    compose down --volumes --remove-orphans
  fi
  rm -rf -- "$PROJECT_DIR"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

BUILD_VENV="$PROJECT_DIR/rustwright-build-venv"
WHEEL_DIR="$PROJECT_DIR/wheels"
python3 -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/python" -m pip install --disable-pip-version-check 'maturin>=1.7,<2'
mkdir -p "$WHEEL_DIR"
(
  cd "$RW_ROOT"
  "$BUILD_VENV/bin/maturin" build --release --out "$WHEEL_DIR"
)
wheels=("$WHEEL_DIR"/rustwright-*.whl)
[[ ${#wheels[@]} -eq 1 && -f "${wheels[0]}" ]]

# Compose resolves seccomp paths from the first compose file's directory.
# Copy the vendored profile into that project so the daemon can load it.
cp "$RW_ROOT/docs/remote-browser/chrome-seccomp.json" "$PROJECT_DIR/chrome-seccomp.json"

cat >"$PROJECT_DIR/probe-entrypoint.sh" <<'ENTRYPOINT'
#!/usr/bin/env bash
set -euo pipefail
wheels=(/wheels/rustwright-*.whl)
[[ ${#wheels[@]} -eq 1 && -f "${wheels[0]}" ]]
PROBE_VENV=/tmp/rustwright-probe-venv
python3 -m venv --system-site-packages "$PROBE_VENV"
"$PROBE_VENV/bin/python" -m pip install --disable-pip-version-check --no-deps "${wheels[0]}"
exec "$PROBE_VENV/bin/python" /project/probe.py
ENTRYPOINT

cat >"$PROJECT_DIR/probe.py" <<'PY'
import asyncio
import json
import socket
import urllib.request

ENDPOINT = "http://playwright:9222"
FIXTURE_URL = "http://fixture:8000/index.html"
MARKER = "rustwright-open-webui-loader-marker"

addresses = sorted(
    {
        address[4][0]
        for address in socket.getaddrinfo("playwright", 9222, type=socket.SOCK_STREAM)
    }
)
assert addresses, "compose hostname playwright did not resolve"
print("RESOLVED_PLAYWRIGHT", ",".join(addresses))

with urllib.request.urlopen(f"{ENDPOINT}/json/version", timeout=5) as response:
    discovery = json.load(response)
assert discovery["webSocketDebuggerUrl"].startswith(("ws://", "wss://"))
print("HTTP_DISCOVERY", json.dumps(discovery, sort_keys=True))

from rustwright.async_api import async_playwright
from rustwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.connect_over_cdp(ENDPOINT)
    page = browser.new_page()
    page.goto(FIXTURE_URL)
    assert page.title() == "Rustwright remote fixture"
    browser.close()
    assert not browser.is_connected()
print("SYNC_CONNECT_NAVIGATE_DISCONNECT ok")


async def direct_async_probe() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(ENDPOINT)
        page = await browser.new_page()
        await page.goto(FIXTURE_URL)
        assert await page.title() == "Rustwright remote fixture"
        await browser.close()
        assert not browser.is_connected()


asyncio.run(direct_async_probe())
print("ASYNC_CONNECT_NAVIGATE_DISCONNECT ok")

import rustwright

rustwright.enable_playwright_compat()
from open_webui.retrieval.web.utils import SafePlaywrightURLLoader

sync_loader = SafePlaywrightURLLoader(
    web_paths=[FIXTURE_URL],
    continue_on_failure=False,
    playwright_ws_url=ENDPOINT,
)
sync_documents = list(sync_loader.lazy_load())
assert sync_documents
assert MARKER in "\n".join(document.page_content for document in sync_documents)
print("OPEN_WEBUI_SYNC_LOADER ok")


async def open_webui_async_probe() -> None:
    loader = SafePlaywrightURLLoader(
        web_paths=[FIXTURE_URL],
        continue_on_failure=False,
        playwright_ws_url=ENDPOINT,
    )
    documents = [document async for document in loader.alazy_load()]
    assert documents
    assert MARKER in "\n".join(document.page_content for document in documents)


asyncio.run(open_webui_async_probe())
print("OPEN_WEBUI_ASYNC_LOADER ok")
PY

cat >"$PROJECT_DIR/compose.yaml" <<'YAML'
services:
  playwright:
    image: ${RW_IMAGE}
    platform: ${RW_PLATFORM}
    init: true
    shm_size: "2gb"
    expose:
      - "9222"
    healthcheck:
      test:
        [
          "CMD",
          "/bin/bash",
          "-c",
          "exec 3<>/dev/tcp/127.0.0.1/9222 && printf 'GET /json/version HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n' >&3 && IFS= read -r status <&3 && [[ \"$$status\" == *' 200 '* ]]",
        ]
      interval: 2s
      timeout: 2s
      retries: 45
      start_period: 5s
    networks: [remote-browser]

  fixture:
    image: docker.io/library/python:3.11-slim-bookworm
    platform: ${RW_PLATFORM}
    command:
      - /bin/sh
      - -euc
      - |
        mkdir -p /tmp/site
        printf '%s\n' '<html><head><title>Rustwright remote fixture</title></head><body>rustwright-open-webui-loader-marker</body></html>' >/tmp/site/index.html
        exec python3 -m http.server 8000 --bind 0.0.0.0 --directory /tmp/site
    networks: [remote-browser]

  rustwright-probe:
    image: ${RW_OPEN_WEBUI_IMAGE}
    platform: ${RW_PLATFORM}
    init: true
    environment:
      ENABLE_RAG_LOCAL_WEB_FETCH: "true"
    entrypoint: ["/bin/bash", "/project/probe-entrypoint.sh"]
    volumes:
      - type: bind
        source: ${RW_WHEEL_DIR}
        target: /wheels
        read_only: true
      - type: bind
        source: ${RW_OPEN_WEBUI_LOADER}
        target: /app/backend/open_webui/retrieval/web/utils.py
        read_only: true
      - type: bind
        source: ${RW_PROJECT_DIR}
        target: /project
        read_only: true
    networks: [remote-browser]

networks:
  remote-browser:
    internal: true
YAML

cat >"$PROJECT_DIR/compose.unprivileged.yaml" <<'YAML'
services:
  playwright:
    user: "65534:65534"
    entrypoint: ["/headless-shell/headless-shell"]
    command:
      - "--remote-debugging-address=0.0.0.0"
      - "--remote-debugging-port=9222"
      - "--disable-gpu"
      - "--enable-unsafe-swiftshader"
      - "--headless"
    security_opt:
      - "seccomp=./chrome-seccomp.json"
    cap_drop:
      - ALL
    read_only: true
    tmpfs:
      - "/tmp:size=512m,mode=1777"
YAML

export RW_IMAGE RW_PLATFORM RW_OPEN_WEBUI_IMAGE
export RW_WHEEL_DIR="$WHEEL_DIR"
export RW_OPEN_WEBUI_LOADER="$OWUI_ROOT/backend/open_webui/retrieval/web/utils.py"
export RW_PROJECT_DIR="$PROJECT_DIR"

docker pull --platform "$RW_PLATFORM" "$RW_IMAGE"
docker image inspect "$RW_IMAGE" --format 'IMAGE={{.Id}} OS={{.Os}} ARCH={{.Architecture}}'
expected_arch="${RW_PLATFORM#linux/}"
actual_arch="$(docker image inspect "$RW_IMAGE" --format '{{.Architecture}}')"
[[ "$actual_arch" == "$expected_arch" ]]

run_variant() {
  label="$1"
  shift
  ACTIVE_PROJECT="$PROJECT_NAME-$label"
  ACTIVE_FILES=("$@")
  compose config --quiet
  compose pull fixture rustwright-probe
  compose up --detach playwright fixture

  deadline=$((SECONDS + 120))
  discovery_file="$PROJECT_DIR/$label-json-version.json"
  readiness_error="$PROJECT_DIR/$label-readiness.err"
  while true; do
    playwright_id="$(compose ps --quiet playwright 2>/dev/null || true)"
    health=''
    if [[ -n "$playwright_id" ]]; then
      health="$(docker inspect --format '{{.State.Health.Status}}' "$playwright_id" 2>/dev/null || true)"
    fi
    if [[ "$health" == "healthy" ]] &&
      compose exec -T fixture python3 -c \
        'import urllib.request; print(urllib.request.urlopen("http://playwright:9222/json/version", timeout=3).read().decode())' \
        >"$discovery_file" 2>"$readiness_error"; then
      break
    fi
    if ((SECONDS >= deadline)); then
      printf 'readiness timeout for %s\n' "$label" >&2
      compose ps >&2 || true
      compose logs --no-color playwright fixture >&2 || true
      cat "$readiness_error" >&2 || true
      return 1
    fi
    sleep 2
  done

  printf 'VARIANT=%s HEALTH=healthy\n' "$label"
  cat "$discovery_file"
  compose run --rm --no-deps rustwright-probe | tee "$PROJECT_DIR/$label-probe.log"
  compose ps
  playwright_id="$(compose ps --quiet playwright)"
  health_json="$(docker inspect --format '{{json .State.Health}}' "$playwright_id")"
  printf '%s\n' "$health_json"
  final_health="$(docker inspect --format '{{.State.Health.Status}}' "$playwright_id")"
  printf 'FINAL_HEALTH=%s\n' "$final_health"
  [[ "$final_health" == "healthy" ]]
  compose down --volumes --remove-orphans
  ACTIVE_PROJECT=''
  ACTIVE_FILES=()
}

run_variant default \
  --file "$PROJECT_DIR/compose.yaml"
run_variant unprivileged \
  --file "$PROJECT_DIR/compose.yaml" \
  --file "$PROJECT_DIR/compose.unprivileged.yaml"
```

Runtime status:

- Native `linux/arm64`: **VERIFICATION-PENDING**. Container execution was
  prohibited on the arm64 implementation workstation.
- Native `linux/amd64`: **VERIFICATION-PENDING**. A native amd64 verifier must
  run the same script with `RW_PLATFORM=linux/amd64`.
- Emulation does not close either architecture gate.

A passing transcript must include the image OS and architecture,
`HEALTH=healthy`, `RESOLVED_PLAYWRIGHT`, `HTTP_DISCOVERY`, both
`CONNECT_NAVIGATE_DISCONNECT ok` lines, both `OPEN_WEBUI_*_LOADER ok` lines,
the full final health JSON, `FINAL_HEALTH=healthy`, and clean teardown.

## Troubleshooting

| Error | Meaning | Action |
|---|---|---|
| `This endpoint speaks the Playwright wire protocol, not CDP.` | Ordered HTTP discovery found `wsEndpointPath`. | Replace the service with raw Chromium CDP and use its HTTP discovery URL. |
| `The endpoint's first response resembles the Playwright wire protocol, not CDP.` | A direct WebSocket first reply had a Playwright-like nested error shape. This is not conclusive. | Use an HTTP discovery URL or confirm the service protocol. |
| `Remote browser discovery failed.` | Neither discovery route produced a valid raw-CDP URL or Playwright discovery evidence. | Check the service, base path, authentication headers, and `/json/version`. |
| `CDP connection failed` | Discovery succeeded, or a direct WebSocket was used, but the Upgrade or setup failed. | Check reachability, TLS, authentication, and whether the URL is a raw-CDP endpoint. |

Rustwright removes URL userinfo, query data, credential headers, discovery
bodies, Upgrade rejection bodies, and nested peer text from public connection
errors.
